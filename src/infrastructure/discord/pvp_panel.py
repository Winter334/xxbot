"""Discord PVP 私有入口与结算面板。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from application.pvp.panel_service import (
    PvpPanelService,
    PvpPanelServiceError,
    PvpPanelSnapshot,
    PvpRecentSettlementSnapshot,
)
from application.pvp.pvp_service import (
    PvpChallengeNotAllowedError,
    PvpChallengeResult,
    PvpService,
    PvpServiceError,
)
from infrastructure.config.static import get_static_config
from infrastructure.db.session import session_scope
from infrastructure.discord.character_panel import (
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)

_PANEL_TIMEOUT_SECONDS = 20 * 60
_PUBLIC_RANK_SHIFT_THRESHOLD = 5
_PUBLIC_TOP_RANK_THRESHOLD = 10
_VISIBLE_REWARD_TYPES = frozenset({"title", "badge"})
_OUTCOME_NAME_BY_VALUE = {
    "ally_victory": "胜利",
    "enemy_victory": "失败",
    "draw": "平局",
}
_REJECTION_REASON_NAME_BY_VALUE = {
    "self_target": "自己不能作为目标",
    "defender_protected": "目标仍处于保护期",
    "missing_active_snapshot": "目标缺少有效防守快照",
    "outside_rank_window": "不在当前名次窗口内",
    "realm_gap_exceeded": "境界差距超出限制",
    "public_power_gap_exceeded": "公开战力差距过大",
    "hidden_score_gap_exceeded": "论道分数差距过大",
    "defense_failure_cap_reached": "目标防守失败保护已达上限",
}
_ANTI_ABUSE_FLAG_NAME_BY_VALUE = {
    "daily_quota_exhausted": "本次后今日有效挑战次数耗尽",
    "repeat_target_limit_reached": "本次后同目标次数达到上限",
    "defense_failure_cap_reached": "本次后目标触发防守失败保护上限",
    "rank_unchanged": "胜利但名次未发生变化",
}
_REWARD_TYPE_NAME_BY_VALUE = {
    "title": "称号",
    "badge": "徽记",
}


class PvpDisplayMode(StrEnum):
    """PVP 私有面板展示模式。"""

    HUB = "hub"
    SETTLEMENT = "settlement"


class PvpPanelServiceBundle(Protocol):
    """PVP 面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    pvp_panel_service: PvpPanelService
    pvp_service: PvpService


@dataclass(frozen=True, slots=True)
class PvpActionNote:
    """PVP 面板动作反馈。"""

    title: str
    lines: tuple[str, ...]


class PvpPanelPresenter:
    """负责把 PVP 聚合快照投影为 Discord Embed。"""

    @classmethod
    def build_hub_embed(
        cls,
        *,
        snapshot: PvpPanelSnapshot,
        selected_target_character_id: int | None,
        action_note: PvpActionNote | None = None,
    ) -> discord.Embed:
        del action_note
        selected_target = cls._resolve_selected_target_card(
            snapshot=snapshot,
            selected_target_character_id=selected_target_character_id,
        )
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜仙榜论道",
            description="仅操作者可见",
            color=discord.Color.dark_magenta(),
        )
        embed.add_field(name="🏆 我方", value=cls._build_status_block(snapshot=snapshot), inline=False)
        embed.add_field(
            name="🎯 当前对手",
            value=cls._build_target_block(snapshot=snapshot, opponent_card=selected_target),
            inline=False,
        )
        embed.add_field(
            name="🎁 本场奖励",
            value=cls._build_reward_block(
                reward_card=None if selected_target is None else selected_target.reward_card,
                anti_abuse_flags=(),
                empty_message="待选择对手后显示本场奖励。",
            ),
            inline=False,
        )
        embed.add_field(name="🏁 最近结果", value=cls._build_recent_result_block(snapshot.recent_result_card), inline=False)
        embed.set_footer(
            text=(
                f"目标刷新：{_format_datetime(snapshot.hub.target_list.generated_at)}"
                f"｜循环锚点：{snapshot.hub.cycle_anchor_date.isoformat()}"
            )
        )
        return embed

    @classmethod
    def build_settlement_embed(
        cls,
        *,
        snapshot: PvpPanelSnapshot,
        selected_target_character_id: int | None,
        action_note: PvpActionNote | None = None,
    ) -> discord.Embed:
        del selected_target_character_id, action_note
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is None:
            return cls.build_hub_embed(
                snapshot=snapshot,
                selected_target_character_id=None,
                action_note=None,
            )
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜仙榜论道结算",
            description="仅操作者可见",
            color=discord.Color.purple(),
        )
        embed.add_field(name="🏆 我方", value=cls._build_status_block(snapshot=snapshot), inline=False)
        embed.add_field(
            name="🎯 当前对手",
            value=cls._build_target_block(snapshot=snapshot, opponent_card=recent_settlement.opponent_card),
            inline=False,
        )
        embed.add_field(
            name="🎁 本场奖励",
            value=cls._build_reward_block(
                reward_card=recent_settlement.reward_card,
                anti_abuse_flags=recent_settlement.anti_abuse_flags,
                empty_message="本场没有可展示奖励。",
            ),
            inline=False,
        )
        embed.add_field(
            name="🏁 最近结果",
            value=cls._build_recent_result_block(recent_settlement.recent_result_card),
            inline=False,
        )
        embed.set_footer(text=f"最近结算时间：{_format_datetime(recent_settlement.occurred_at)}")
        return embed

    @classmethod
    def _resolve_selected_target_card(
        cls,
        *,
        snapshot: PvpPanelSnapshot,
        selected_target_character_id: int | None,
    ):
        if not snapshot.target_cards:
            return None
        if selected_target_character_id is None:
            return snapshot.target_cards[0]
        for target_card in snapshot.target_cards:
            if target_card.character_id == selected_target_character_id:
                return target_card
        return snapshot.target_cards[0]

    @classmethod
    def _build_status_block(cls, *, snapshot: PvpPanelSnapshot) -> str:
        status_card = snapshot.status_card
        main_skill_name = snapshot.overview.main_skill.skill_name or snapshot.overview.main_path_name or "未定功法"
        lines = [
            f"{snapshot.overview.character_name}｜{snapshot.overview.realm_name}·{snapshot.overview.stage_name}",
            f"主修：{main_skill_name}",
            "```text\n"
            f"排名  #{status_card.rank_position}    历史  #{status_card.best_rank}\n"
            f"次数  {status_card.remaining_challenge_count}/{status_card.daily_challenge_limit}   荣誉  {status_card.honor_coin_balance}\n"
            f"战力  {status_card.public_power_score}   论道  {status_card.hidden_pvp_score}\n"
            "```",
        ]
        if status_card.reward_tier_name:
            lines.append(f"档位：{status_card.reward_tier_name}")
        protected_until = _format_optional_datetime(status_card.protected_until)
        if protected_until:
            lines.append(f"保护：{protected_until}")
        return "\n".join(lines)

    @classmethod
    def _build_target_block(cls, *, snapshot: PvpPanelSnapshot, opponent_card) -> str:
        if opponent_card is None:
            return "暂无可挑战对手。"
        power_diff = opponent_card.public_power_score - snapshot.status_card.public_power_score
        title = opponent_card.character_name
        if opponent_card.character_title:
            title = f"{title}｜{opponent_card.character_title}"
        realm_stage = f"{opponent_card.realm_name or '-'}·{opponent_card.stage_name or '-'}"
        if opponent_card.main_path_name:
            realm_stage = f"{realm_stage}｜{opponent_card.main_path_name}"
        lines = [
            title,
            realm_stage,
            "```text\n"
            f"排名  #{opponent_card.rank_position}    名差  {_format_signed(opponent_card.rank_gap)}\n"
            f"战力  {opponent_card.public_power_score}   差值  {_format_signed(power_diff)}\n"
            "```",
        ]
        if opponent_card.display_summary:
            lines.append(f"印象：{opponent_card.display_summary}")
        return "\n".join(lines)

    @classmethod
    def _build_reward_block(
        cls,
        *,
        reward_card,
        anti_abuse_flags: Sequence[str],
        empty_message: str,
    ) -> str:
        if reward_card is None:
            return empty_message
        lines: list[str] = []
        tier_name = reward_card.tier_name or reward_card.summary
        if tier_name:
            lines.append(f"档位：{tier_name}")
        lines.append(
            "```text\n"
            f"胜利  +{reward_card.honor_coin_on_win} 荣誉币\n"
            f"失败  +{reward_card.honor_coin_on_loss} 荣誉币\n"
            "```"
        )
        if reward_card.visible_reward_lines:
            lines.append("展示：")
            lines.extend(f"- {line}" for line in reward_card.visible_reward_lines[:3])
        else:
            lines.append("展示：无")
        flag_lines = cls._format_flag_lines(anti_abuse_flags)
        if flag_lines:
            lines.append("标记：" + "｜".join(flag_lines))
        return "\n".join(lines)

    @classmethod
    def _build_recent_result_block(cls, recent_result_card) -> str:
        if recent_result_card is None:
            return "暂无最近结果。"
        return "\n".join(
            (
                f"对手：{recent_result_card.opponent_name}",
                "```text\n"
                f"结果  {_OUTCOME_NAME_BY_VALUE.get(recent_result_card.outcome, recent_result_card.outcome)}\n"
                f"名次  #{recent_result_card.rank_before} → #{recent_result_card.rank_after}\n"
                f"变化  {_format_signed(recent_result_card.rank_shift, suffix=' 名')}\n"
                f"荣誉  {_format_signed(recent_result_card.honor_coin_delta)}\n"
                "```",
                f"时间：{_format_datetime(recent_result_card.occurred_at)}",
            )
        )

    @classmethod
    def _format_visible_rewards(cls, rewards: Sequence[Mapping[str, Any]]) -> list[str]:
        visible_rewards = _filter_visible_rewards(rewards)
        if not visible_rewards:
            return []
        lines: list[str] = []
        for reward in visible_rewards:
            reward_type = _read_optional_str(reward.get("reward_type")) or "reward"
            state = _read_optional_str(reward.get("state"))
            state_suffix = ""
            if state == "unlocked_now":
                state_suffix = "（本次获得）"
            elif state == "owned":
                state_suffix = "（已持有）"
            lines.append(
                f"{_REWARD_TYPE_NAME_BY_VALUE.get(reward_type, reward_type)}：{reward.get('name') or '-'}{state_suffix}"
            )
        return lines

    @classmethod
    def _format_flag_lines(cls, flags: Sequence[str]) -> list[str]:
        return [_ANTI_ABUSE_FLAG_NAME_BY_VALUE.get(flag, flag) for flag in flags if isinstance(flag, str) and flag]

    @staticmethod
    def _format_ratio(value: Any) -> str:
        normalized = _read_decimal_ratio(value)
        if normalized is None:
            return "0.0%"
        return f"{normalized * 100:.1f}%"


class PvpPublicSettlementMode(StrEnum):
    """PVP 公开结算播报模式。"""

    NONE = "none"
    NORMAL = "normal"
    HIGHLIGHT = "highlight"


class PvpPublicSettlementPresenter:
    """负责生成公开频道中的论道结算播报。"""

    @classmethod
    def build_embed(
        cls,
        *,
        snapshot: PvpPanelSnapshot,
        reward_tiers: Sequence[Any],
    ) -> discord.Embed | None:
        recent_settlement = snapshot.recent_settlement
        if recent_settlement is None:
            return None
        highlight_lines, unlocked_rewards = cls._collect_highlight_lines(
            snapshot=snapshot,
            recent_settlement=recent_settlement,
            reward_tiers=reward_tiers,
        )
        broadcast_mode = cls._resolve_broadcast_mode(
            recent_settlement=recent_settlement,
            highlight_lines=highlight_lines,
        )
        if broadcast_mode is PvpPublicSettlementMode.HIGHLIGHT:
            return cls._build_highlight_embed(
                snapshot=snapshot,
                recent_settlement=recent_settlement,
                highlight_lines=highlight_lines,
                unlocked_rewards=unlocked_rewards,
            )
        if broadcast_mode is PvpPublicSettlementMode.NORMAL:
            return cls._build_normal_embed(snapshot=snapshot, recent_settlement=recent_settlement)
        return None

    @classmethod
    def _build_highlight_embed(
        cls,
        *,
        snapshot: PvpPanelSnapshot,
        recent_settlement: PvpRecentSettlementSnapshot,
        highlight_lines: Sequence[str],
        unlocked_rewards: Sequence[Mapping[str, Any]],
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜仙榜论道高光播报",
            description="公开频道播报",
            color=discord.Color.orange(),
        )
        embed.add_field(name="高光结果", value="\n".join(highlight_lines), inline=False)
        embed.add_field(
            name="本次结算摘要",
            value=cls._build_public_result_block(snapshot=snapshot, recent_settlement=recent_settlement),
            inline=False,
        )
        if unlocked_rewards:
            embed.add_field(
                name="新获得展示奖励",
                value="\n".join(PvpPanelPresenter._format_visible_rewards(unlocked_rewards)),
                inline=False,
            )
        return embed

    @classmethod
    def _build_normal_embed(
        cls,
        *,
        snapshot: PvpPanelSnapshot,
        recent_settlement: PvpRecentSettlementSnapshot,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜仙榜论道结果播报",
            description="公开频道播报",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="本次结算摘要",
            value=cls._build_public_result_block(snapshot=snapshot, recent_settlement=recent_settlement),
            inline=False,
        )
        return embed

    @staticmethod
    def _resolve_broadcast_mode(
        *,
        recent_settlement: PvpRecentSettlementSnapshot,
        highlight_lines: Sequence[str],
    ) -> PvpPublicSettlementMode:
        if recent_settlement.battle_outcome != "ally_victory":
            return PvpPublicSettlementMode.NONE
        if highlight_lines:
            return PvpPublicSettlementMode.HIGHLIGHT
        rank_shift = recent_settlement.rank_before_attacker - recent_settlement.rank_after_attacker
        if rank_shift > 0:
            return PvpPublicSettlementMode.NORMAL
        return PvpPublicSettlementMode.NONE

    @classmethod
    def _collect_highlight_lines(
        cls,
        *,
        snapshot: PvpPanelSnapshot,
        recent_settlement: PvpRecentSettlementSnapshot,
        reward_tiers: Sequence[Any],
    ) -> tuple[tuple[str, ...], list[dict[str, object]]]:
        if recent_settlement.battle_outcome != "ally_victory":
            return (), []
        lines: list[str] = []
        unlocked_rewards = _filter_visible_rewards(recent_settlement.display_rewards, unlocked_only=True)
        before_tier = _resolve_reward_tier_definition(
            recent_settlement.rank_before_attacker,
            reward_tiers=reward_tiers,
        )
        after_tier = _resolve_reward_tier_definition(
            recent_settlement.rank_after_attacker,
            reward_tiers=reward_tiers,
        )
        if (
            before_tier is not None
            and after_tier is not None
            and getattr(after_tier, "order", 0) < getattr(before_tier, "order", 0)
        ):
            lines.append(
                f"奖励档位提升：{getattr(before_tier, 'name', before_tier.reward_tier_id)} → "
                f"{getattr(after_tier, 'name', after_tier.reward_tier_id)}"
            )
        rank_shift = recent_settlement.rank_before_attacker - recent_settlement.rank_after_attacker
        if rank_shift >= _PUBLIC_RANK_SHIFT_THRESHOLD or (
            rank_shift > 0 and recent_settlement.rank_after_attacker <= _PUBLIC_TOP_RANK_THRESHOLD
        ):
            lines.append(
                f"排名显著上升：第 {recent_settlement.rank_before_attacker} 名 → 第 {recent_settlement.rank_after_attacker} 名"
            )
        if unlocked_rewards:
            lines.append("获得可公开展示的称号或徽记")
        return tuple(lines), unlocked_rewards

    @staticmethod
    def _build_public_result_block(
        *,
        snapshot: PvpPanelSnapshot,
        recent_settlement: PvpRecentSettlementSnapshot,
    ) -> str:
        defender_name = recent_settlement.defender_summary.get("character_name") or f"角色{recent_settlement.defender_character_id}"
        return "\n".join(
            (
                f"结果：{_OUTCOME_NAME_BY_VALUE.get(recent_settlement.battle_outcome, recent_settlement.battle_outcome)}",
                f"对手：{defender_name}｜挑战前第 {recent_settlement.rank_before_defender} 名",
                f"名次：第 {recent_settlement.rank_before_attacker} 名 → 第 {recent_settlement.rank_after_attacker} 名",
                f"当前奖励档位：{snapshot.current_reward_tier_name or snapshot.current_challenge_tier or '-'}",
                f"荣誉币：{_format_signed(recent_settlement.honor_coin_delta)}",
            )
        )


class PvpTargetSelect(discord.ui.Select):
    """PVP 目标选择器。"""

    def __init__(self, *, snapshot: PvpPanelSnapshot, selected_target_character_id: int | None) -> None:
        options = []
        for target in snapshot.hub.target_list.targets[:25]:
            summary = target.summary
            label = _truncate_text(
                f"第 {target.rank_position} 名｜{summary.get('character_name') or target.display_summary}",
                limit=100,
            )
            description = _truncate_text(
                f"{summary.get('realm_name') or '-'}·{summary.get('stage_name') or '-'}｜"
                f"{summary.get('main_skill_name') or summary.get('main_path_name') or '未定功法'}｜战力 {target.public_power_score}",
                limit=100,
            )
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(target.character_id),
                    description=description,
                    default=target.character_id == selected_target_character_id,
                )
            )
        super().__init__(
            placeholder="选择论道目标",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, PvpPanelView):
            await interaction.response.defer()
            return
        view.selected_target_character_id = int(self.values[0])
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PvpPanelView(discord.ui.View):
    """PVP 私有面板视图。"""

    def __init__(
        self,
        *,
        controller: PvpPanelController,
        owner_user_id: int,
        character_id: int,
        snapshot: PvpPanelSnapshot,
        selected_target_character_id: int | None,
        display_mode: PvpDisplayMode,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = character_id
        self.snapshot = snapshot
        self.selected_target_character_id = selected_target_character_id
        self.display_mode = display_mode
        if snapshot.hub.target_list.targets:
            self.add_item(
                PvpTargetSelect(
                    snapshot=snapshot,
                    selected_target_character_id=selected_target_character_id,
                )
            )
        self._sync_component_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self._controller.responder.send_private_error(interaction, message="该私有面板仅允许发起者操作。")
        return False

    def build_embed(self) -> discord.Embed:
        if self.display_mode is PvpDisplayMode.SETTLEMENT and self.snapshot.recent_settlement is not None:
            return PvpPanelPresenter.build_settlement_embed(
                snapshot=self.snapshot,
                selected_target_character_id=self.selected_target_character_id,
            )
        return PvpPanelPresenter.build_hub_embed(
            snapshot=self.snapshot,
            selected_target_character_id=self.selected_target_character_id,
        )

    def _sync_component_state(self) -> None:
        has_targets = bool(self.snapshot.hub.target_list.targets)
        self.challenge_target.disabled = (
            self.display_mode is not PvpDisplayMode.HUB or not has_targets or self.selected_target_character_id is None
        )
        self.refresh_targets.disabled = self.display_mode is not PvpDisplayMode.HUB
        self.view_recent_settlement.disabled = (
            self.snapshot.recent_settlement is None or self.display_mode is PvpDisplayMode.SETTLEMENT
        )
        self.return_to_hub.disabled = self.display_mode is PvpDisplayMode.HUB
        for item in self.children:
            if isinstance(item, PvpTargetSelect):
                item.disabled = self.display_mode is not PvpDisplayMode.HUB or not has_targets

    @discord.ui.button(label="发起论道", style=discord.ButtonStyle.success, row=0)
    async def challenge_target(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        if self.selected_target_character_id is None:
            await self._controller.responder.send_private_error(interaction, message="请先选择一个论道目标。")
            return
        await self._controller.challenge_target(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_target_character_id=self.selected_target_character_id,
        )

    @discord.ui.button(label="刷新仙榜目标", style=discord.ButtonStyle.secondary, row=0)
    async def refresh_targets(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.refresh_panel(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_target_character_id=self.selected_target_character_id,
            display_mode=self.display_mode,
        )

    @discord.ui.button(label="查看最近结算", style=discord.ButtonStyle.secondary, row=1)
    async def view_recent_settlement(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.show_recent_settlement(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_target_character_id=self.selected_target_character_id,
        )

    @discord.ui.button(label="返回总览", style=discord.ButtonStyle.secondary, row=1)
    async def return_to_hub(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.show_hub(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_target_character_id=self.selected_target_character_id,
        )


class PvpPanelController:
    """组织 PVP 私有面板交互。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        service_bundle_factory,
        responder: DiscordInteractionVisibilityResponder | None = None,
        panel_timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._service_bundle_factory = service_bundle_factory
        self.responder = responder or DiscordInteractionVisibilityResponder()
        self._panel_timeout = panel_timeout
        static_config = get_static_config()
        self._reward_tiers = tuple(static_config.pvp.ordered_reward_tiers)

    async def open_panel_by_discord_user_id(self, interaction: discord.Interaction) -> None:
        """按 Discord 用户标识打开 PVP 面板。"""
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (CharacterPanelQueryServiceError, PvpPanelServiceError, PvpServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            selected_target_character_id=None,
            display_mode=PvpDisplayMode.HUB,
        )

    async def open_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """按角色标识打开 PVP 面板。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (PvpPanelServiceError, PvpServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            selected_target_character_id=None,
            display_mode=PvpDisplayMode.HUB,
        )

    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_target_character_id: int | None,
        display_mode: PvpDisplayMode,
    ) -> None:
        """刷新 PVP 面板。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (PvpPanelServiceError, PvpServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = PvpActionNote(
            title="当前论道状态",
            lines=(
                f"仙榜目标已刷新：当前可论道 {len(snapshot.hub.target_list.targets)} 个目标",
                f"今日剩余有效次数：{snapshot.hub.remaining_challenge_count}",
            ),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_target_character_id=selected_target_character_id,
            display_mode=display_mode,
            action_note=action_note,
        )

    async def challenge_target(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_target_character_id: int,
    ) -> None:
        """执行一次完整 PVP 挑战，并按条件公开播报。"""
        try:
            result, snapshot = self._challenge_target(
                character_id=character_id,
                target_character_id=selected_target_character_id,
            )
        except (PvpChallengeNotAllowedError, PvpPanelServiceError, PvpServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = PvpActionNote(
            title="本次论道结果",
            lines=self._build_challenge_lines(result=result),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_target_character_id=selected_target_character_id,
            display_mode=PvpDisplayMode.SETTLEMENT,
            action_note=action_note,
        )
        await self._send_public_highlight_if_needed(interaction, snapshot=snapshot)

    async def show_recent_settlement(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_target_character_id: int | None,
    ) -> None:
        """切换到最近一次 PVP 结算详情视图。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (PvpPanelServiceError, PvpServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        if snapshot.recent_settlement is None:
            await self.responder.send_private_error(interaction, message="当前没有可复读的论道结算结果。")
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_target_character_id=selected_target_character_id,
            display_mode=PvpDisplayMode.SETTLEMENT,
        )

    async def show_hub(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_target_character_id: int | None,
    ) -> None:
        """切换回 PVP 总览视图。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (PvpPanelServiceError, PvpServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_target_character_id=selected_target_character_id,
            display_mode=PvpDisplayMode.HUB,
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services: PvpPanelServiceBundle = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_panel_snapshot(self, *, character_id: int) -> PvpPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services: PvpPanelServiceBundle = self._service_bundle_factory(session)
            return services.pvp_panel_service.get_panel_snapshot(character_id=character_id)

    def _challenge_target(
        self,
        *,
        character_id: int,
        target_character_id: int,
    ) -> tuple[PvpChallengeResult, PvpPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: PvpPanelServiceBundle = self._service_bundle_factory(session)
            result = services.pvp_service.challenge_target(
                character_id=character_id,
                target_character_id=target_character_id,
            )
            snapshot = services.pvp_panel_service.get_panel_snapshot(character_id=character_id)
            return result, snapshot

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: PvpPanelSnapshot,
        owner_user_id: int,
        selected_target_character_id: int | None,
        display_mode: PvpDisplayMode,
        action_note: PvpActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_target_character_id=selected_target_character_id,
            display_mode=display_mode,
            action_note=action_note,
        )
        await self.responder.send_message(
            interaction,
            payload=payload,
            visibility=PanelVisibility.PRIVATE,
        )

    async def _edit_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: PvpPanelSnapshot,
        owner_user_id: int,
        selected_target_character_id: int | None,
        display_mode: PvpDisplayMode,
        action_note: PvpActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_target_character_id=selected_target_character_id,
            display_mode=display_mode,
            action_note=action_note,
        )
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: PvpPanelSnapshot,
        owner_user_id: int,
        selected_target_character_id: int | None,
        display_mode: PvpDisplayMode,
        action_note: PvpActionNote | None,
    ) -> PanelMessagePayload:
        normalized_selected_target = self._resolve_selected_target_character_id(
            snapshot=snapshot,
            selected_target_character_id=selected_target_character_id,
        )
        normalized_display_mode = self._resolve_display_mode(snapshot=snapshot, display_mode=display_mode)
        view = PvpPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=snapshot.overview.character_id,
            snapshot=snapshot,
            selected_target_character_id=normalized_selected_target,
            display_mode=normalized_display_mode,
            timeout=self._panel_timeout,
        )
        if normalized_display_mode is PvpDisplayMode.SETTLEMENT:
            embed = PvpPanelPresenter.build_settlement_embed(
                snapshot=snapshot,
                selected_target_character_id=normalized_selected_target,
                action_note=action_note,
            )
        else:
            embed = PvpPanelPresenter.build_hub_embed(
                snapshot=snapshot,
                selected_target_character_id=normalized_selected_target,
                action_note=action_note,
            )
        return PanelMessagePayload(embed=embed, view=view)

    @staticmethod
    def _resolve_display_mode(*, snapshot: PvpPanelSnapshot, display_mode: PvpDisplayMode) -> PvpDisplayMode:
        if display_mode is PvpDisplayMode.SETTLEMENT and snapshot.recent_settlement is None:
            return PvpDisplayMode.HUB
        return display_mode

    @staticmethod
    def _resolve_selected_target_character_id(
        *,
        snapshot: PvpPanelSnapshot,
        selected_target_character_id: int | None,
    ) -> int | None:
        target_character_ids = {target.character_id for target in snapshot.hub.target_list.targets}
        if selected_target_character_id in target_character_ids:
            return selected_target_character_id
        if snapshot.hub.target_list.targets:
            return snapshot.hub.target_list.targets[0].character_id
        return None

    async def _send_public_highlight_if_needed(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: PvpPanelSnapshot,
    ) -> None:
        embed = PvpPublicSettlementPresenter.build_embed(
            snapshot=snapshot,
            reward_tiers=self._reward_tiers,
        )
        if embed is None or interaction.channel is None:
            return
        try:
            await interaction.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return

    @staticmethod
    def _build_challenge_lines(*, result: PvpChallengeResult) -> tuple[str, ...]:
        rank_shift = result.rank_before_attacker - result.rank_after_attacker
        lines = [
            f"结果：{_OUTCOME_NAME_BY_VALUE.get(result.battle_outcome, result.battle_outcome)}",
            f"名次：第 {result.rank_before_attacker} 名 → 第 {result.rank_after_attacker} 名",
            f"名次变化：{_format_signed(rank_shift, suffix=' 名')}",
            f"荣誉币：{_format_signed(result.honor_coin_delta)}｜余额 {result.honor_coin_balance_after}",
        ]
        reward_lines = PvpPanelPresenter._format_visible_rewards(result.display_rewards)
        if reward_lines:
            lines.append("展示奖励：" + "；".join(reward_lines))
        flag_lines = PvpPanelPresenter._format_flag_lines(result.anti_abuse_flags)
        if flag_lines:
            lines.append("结算标记：" + "；".join(flag_lines))
        return tuple(lines)



def _find_target(*, snapshot: PvpPanelSnapshot, character_id: int | None):
    if character_id is None:
        return None
    for target in snapshot.hub.target_list.targets:
        if target.character_id == character_id:
            return target
    return None



def _resolve_reward_tier_definition(rank_position: int, *, reward_tiers: Sequence[Any]):
    if rank_position <= 0:
        return None
    for tier in reward_tiers:
        rank_start = _read_int(getattr(tier, "rank_start", 0))
        rank_end = _read_int(getattr(tier, "rank_end", 0))
        if rank_start <= rank_position <= rank_end:
            return tier
    return None



def _filter_visible_rewards(
    rewards: Sequence[Mapping[str, Any]],
    *,
    unlocked_only: bool = False,
) -> list[dict[str, object]]:
    visible_rewards: list[dict[str, object]] = []
    for reward in rewards:
        reward_type = _read_optional_str(reward.get("reward_type"))
        if reward_type not in _VISIBLE_REWARD_TYPES:
            continue
        if unlocked_only and _read_optional_str(reward.get("state")) != "unlocked_now":
            continue
        visible_rewards.append({str(key): value for key, value in reward.items()})
    return visible_rewards



def _normalize_mapping_sequence(value: Any) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    normalized: list[dict[str, object]] = []
    for entry in value:
        if isinstance(entry, Mapping):
            normalized.append({str(key): item for key, item in entry.items()})
    return normalized



def _normalize_optional_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return None



def _read_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default



def _read_nested_int(payload: Mapping[str, Any], key: str) -> int:
    return _read_int(payload.get(key))



def _read_optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None



def _read_decimal_ratio(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str) and value:
        try:
            return max(0.0, min(1.0, float(value)))
        except ValueError:
            return None
    return None



def _format_signed(value: int, *, suffix: str = "") -> str:
    normalized = int(value)
    prefix = "+" if normalized >= 0 else ""
    return f"{prefix}{normalized}{suffix}"



def _format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")



def _format_optional_datetime(value: Any) -> str | None:
    if isinstance(value, datetime):
        return _format_datetime(value)
    if isinstance(value, str) and value:
        try:
            return _format_datetime(datetime.fromisoformat(value))
        except ValueError:
            return value
    return None



def _truncate_text(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


__all__ = [
    "PvpPanelController",
    "PvpPanelPresenter",
    "PvpPublicSettlementPresenter",
]
