"""Discord 修炼与闭关私有面板。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character.cultivation_panel_service import (
    CultivationPanelService,
    CultivationPanelServiceError,
    CultivationPanelSnapshot,
    PracticeOnceResult,
)
from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from application.character.retreat_service import RetreatService, RetreatServiceError, RetreatSettlementResult
from infrastructure.db.session import session_scope
from infrastructure.discord.character_panel import (
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)

_PANEL_TIMEOUT_SECONDS = 20 * 60
_DEFAULT_RETREAT_DURATION_HOURS = 24
_RETREAT_DURATION_HOURS_OPTIONS = (12, 24, 48)
_RETREAT_STATUS_RUNNING = "running"
_STAGE_NAME_BY_ID = {
    "early": "初期",
    "middle": "中期",
    "mid": "中期",
    "late": "后期",
    "peak": "圆满",
    "perfect": "圆满",
    "perfected": "圆满",
}


class CultivationPanelServiceBundle(Protocol):
    """修炼面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    cultivation_panel_service: CultivationPanelService
    retreat_service: RetreatService


@dataclass(frozen=True, slots=True)
class CultivationActionNote:
    """修炼面板动作反馈。"""

    title: str
    lines: tuple[str, ...]


class CultivationPanelPresenter:
    """负责把修炼面板快照投影为 Discord Embed。"""

    @classmethod
    def build_embed(
        cls,
        *,
        snapshot: CultivationPanelSnapshot,
        selected_duration_hours: int,
        action_note: CultivationActionNote | None = None,
    ) -> discord.Embed:
        growth_snapshot = snapshot.growth_snapshot
        embed = discord.Embed(
            title=f"{growth_snapshot.character_name}｜修炼与闭关",
            description="仅操作者可见",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="修炼概览",
            value=cls._build_overview_block(snapshot=snapshot),
            inline=False,
        )
        embed.add_field(
            name="闭关状态",
            value=cls._build_retreat_block(snapshot=snapshot, selected_duration_hours=selected_duration_hours),
            inline=False,
        )
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        embed.set_footer(text=f"当前闭关时长：{selected_duration_hours} 小时")
        return embed

    @classmethod
    def _build_overview_block(cls, *, snapshot: CultivationPanelSnapshot) -> str:
        growth_snapshot = snapshot.growth_snapshot
        breakthrough_precheck = snapshot.breakthrough_precheck
        stage_name = _STAGE_NAME_BY_ID.get(growth_snapshot.stage_id, growth_snapshot.stage_id)
        current_value = growth_snapshot.cultivation_value
        current_stage_entry = growth_snapshot.current_stage_entry_cultivation
        next_stage_entry = growth_snapshot.next_stage_entry_cultivation
        lines = [
            f"境界：{breakthrough_precheck.current_realm_name}·{stage_name}",
            f"总修为：{current_value}/{growth_snapshot.realm_total_cultivation}",
            f"感悟：{growth_snapshot.comprehension_value}",
            f"灵石：{growth_snapshot.spirit_stone}",
            f"单次修炼：+{snapshot.practice_cultivation_amount} 修为",
        ]
        if next_stage_entry is None:
            lines.append("小阶段：已到当前大境界上限")
        else:
            lines.append(
                "小阶段进度："
                f"{max(0, current_value - current_stage_entry)}/{max(0, next_stage_entry - current_stage_entry)}"
            )
            lines.append(f"下一小阶段还差：{max(0, next_stage_entry - current_value)} 修为")
        lines.append(f"突破前置：{cls._build_breakthrough_gap_summary(snapshot=snapshot)}")
        return "\n".join(lines)

    @classmethod
    def _build_retreat_block(cls, *, snapshot: CultivationPanelSnapshot, selected_duration_hours: int) -> str:
        retreat_status = snapshot.retreat_status
        if retreat_status is None:
            return (
                "状态：未开始\n"
                f"建议时长：{selected_duration_hours} 小时\n"
                "说明：开始后按到期时间结算闭关收益"
            )
        if cls._is_retreat_running(retreat_status):
            lines = [
                "状态：闭关中",
                f"开始时间：{cls._format_datetime(retreat_status.started_at)}",
                f"预计结束：{cls._format_datetime(retreat_status.scheduled_end_at)}",
            ]
            if retreat_status.can_settle:
                lines.extend(
                    (
                        "当前可结束闭关并结算收益",
                        f"待结算修为：{retreat_status.pending_cultivation}",
                        f"待结算感悟：{retreat_status.pending_comprehension}",
                        f"待结算灵石：{retreat_status.pending_spirit_stone}",
                    )
                )
            else:
                lines.append("结算状态：尚未到可结算时间")
            return "\n".join(lines)
        return (
            "状态：已结束\n"
            f"上次结束：{cls._format_datetime(retreat_status.settled_at)}\n"
            f"建议时长：{selected_duration_hours} 小时"
        )

    @classmethod
    def _build_breakthrough_gap_summary(cls, *, snapshot: CultivationPanelSnapshot) -> str:
        precheck = snapshot.breakthrough_precheck
        if precheck.passed:
            target = precheck.target_realm_name or "下一境界"
            return f"已满足前置，可准备突破 {target}"
        parts: list[str] = []
        for gap in precheck.gaps:
            if gap.gap_type == "open_limit":
                parts.append("当前已到开放上限")
            elif gap.gap_type == "cultivation_insufficient":
                parts.append(f"修为还差 {gap.missing_value}")
            elif gap.gap_type == "comprehension_insufficient":
                parts.append(f"感悟还差 {gap.missing_value}")
            elif gap.gap_type == "qualification_missing":
                parts.append("缺少突破资格")
            elif gap.gap_type == "material_insufficient":
                item_name = gap.item_id or "材料"
                parts.append(f"{item_name} 还差 {gap.missing_value}")
        if not parts:
            return "仍有未满足前置"
        return "；".join(parts)

    @staticmethod
    def _is_retreat_running(retreat_status) -> bool:
        if retreat_status is None:
            return False
        return retreat_status.status == _RETREAT_STATUS_RUNNING and retreat_status.settled_at is None

    @staticmethod
    def _format_datetime(value) -> str:
        if value is None:
            return "-"
        return f"{discord.utils.format_dt(value, style='f')}｜{discord.utils.format_dt(value, style='R')}"


class RetreatDurationSelect(discord.ui.Select):
    """闭关时长选择器。"""

    def __init__(self, *, selected_duration_hours: int) -> None:
        normalized_duration = (
            selected_duration_hours
            if selected_duration_hours in _RETREAT_DURATION_HOURS_OPTIONS
            else _DEFAULT_RETREAT_DURATION_HOURS
        )
        options = [
            discord.SelectOption(
                label=f"{hours} 小时",
                value=str(hours),
                default=hours == normalized_duration,
            )
            for hours in _RETREAT_DURATION_HOURS_OPTIONS
        ]
        super().__init__(
            placeholder="选择闭关时长",
            min_values=1,
            max_values=1,
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CultivationPanelView):
            await interaction.response.defer()
            return
        view.selected_duration_hours = int(self.values[0])
        embed = CultivationPanelPresenter.build_embed(
            snapshot=view.snapshot,
            selected_duration_hours=view.selected_duration_hours,
        )
        await interaction.response.edit_message(embed=embed, view=view)


class CultivationPanelView(discord.ui.View):
    """修炼与闭关私有面板视图。"""

    def __init__(
        self,
        *,
        controller: CultivationPanelController,
        owner_user_id: int,
        character_id: int,
        snapshot: CultivationPanelSnapshot,
        selected_duration_hours: int = _DEFAULT_RETREAT_DURATION_HOURS,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = character_id
        self.snapshot = snapshot
        self.selected_duration_hours = selected_duration_hours
        self.add_item(RetreatDurationSelect(selected_duration_hours=selected_duration_hours))
        self._sync_component_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self._controller.responder.send_private_error(interaction, message="该私有面板仅允许发起者操作。")
        return False

    def _sync_component_state(self) -> None:
        is_retreat_running = CultivationPanelPresenter._is_retreat_running(self.snapshot.retreat_status)
        can_settle = self.snapshot.retreat_status is not None and self.snapshot.retreat_status.can_settle
        self.practice_once.disabled = is_retreat_running
        self.start_retreat.disabled = is_retreat_running
        self.finish_retreat.disabled = not can_settle
        for item in self.children:
            if isinstance(item, RetreatDurationSelect):
                item.disabled = is_retreat_running

    @discord.ui.button(label="修炼一次", style=discord.ButtonStyle.primary, row=0)
    async def practice_once(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.practice_once(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_duration_hours=self.selected_duration_hours,
        )

    @discord.ui.button(label="开始闭关", style=discord.ButtonStyle.success, row=0)
    async def start_retreat(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.start_retreat(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_duration_hours=self.selected_duration_hours,
        )

    @discord.ui.button(label="结束闭关", style=discord.ButtonStyle.danger, row=0)
    async def finish_retreat(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.settle_retreat(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_duration_hours=self.selected_duration_hours,
        )

    @discord.ui.button(label="刷新状态", style=discord.ButtonStyle.secondary, row=1)
    async def refresh_panel(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.refresh_panel(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            selected_duration_hours=self.selected_duration_hours,
        )


class CultivationPanelController:
    """组织修炼与闭关私有面板交互。"""

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

    async def open_panel_by_discord_user_id(self, interaction: discord.Interaction) -> None:
        """按 Discord 用户标识打开修炼面板。"""
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_snapshot(character_id=character_id)
        except (CharacterPanelQueryServiceError, CultivationPanelServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            selected_duration_hours=_DEFAULT_RETREAT_DURATION_HOURS,
        )

    async def open_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """按角色标识打开修炼面板。"""
        try:
            snapshot = self._load_snapshot(character_id=character_id)
        except CultivationPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
            selected_duration_hours=_DEFAULT_RETREAT_DURATION_HOURS,
        )

    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_duration_hours: int,
    ) -> None:
        """刷新修炼面板。"""
        try:
            snapshot = self._load_snapshot(character_id=character_id)
        except CultivationPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_duration_hours=selected_duration_hours,
        )

    async def practice_once(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_duration_hours: int,
    ) -> None:
        """执行一次主动修炼。"""
        try:
            result = self._practice_once(character_id=character_id)
        except CultivationPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = CultivationActionNote(
            title="本次修炼",
            lines=self._build_practice_lines(result=result),
        )
        await self._edit_panel(
            interaction,
            snapshot=result.snapshot,
            owner_user_id=owner_user_id,
            selected_duration_hours=selected_duration_hours,
            action_note=action_note,
        )

    async def start_retreat(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_duration_hours: int,
    ) -> None:
        """开始一段新的闭关。"""
        try:
            snapshot = self._start_retreat(character_id=character_id, selected_duration_hours=selected_duration_hours)
        except (CultivationPanelServiceError, RetreatServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = CultivationActionNote(
            title="闭关已开始",
            lines=(
                f"闭关时长：{selected_duration_hours} 小时",
                f"预计结束：{CultivationPanelPresenter._format_datetime(snapshot.retreat_status.scheduled_end_at if snapshot.retreat_status else None)}",
            ),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_duration_hours=selected_duration_hours,
            action_note=action_note,
        )

    async def settle_retreat(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        selected_duration_hours: int,
    ) -> None:
        """结束闭关并结算收益。"""
        try:
            settlement, snapshot = self._settle_retreat(character_id=character_id)
        except (CultivationPanelServiceError, RetreatServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = CultivationActionNote(
            title="闭关结算",
            lines=self._build_settlement_lines(settlement=settlement),
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            selected_duration_hours=selected_duration_hours,
            action_note=action_note,
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_snapshot(self, *, character_id: int) -> CultivationPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.cultivation_panel_service.get_panel_snapshot(character_id=character_id)

    def _practice_once(self, *, character_id: int) -> PracticeOnceResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.cultivation_panel_service.practice_once(character_id=character_id)

    def _start_retreat(self, *, character_id: int, selected_duration_hours: int) -> CultivationPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            services.retreat_service.start_retreat(
                character_id=character_id,
                duration=timedelta(hours=selected_duration_hours),
            )
            return services.cultivation_panel_service.get_panel_snapshot(character_id=character_id)

    def _settle_retreat(self, *, character_id: int) -> tuple[RetreatSettlementResult, CultivationPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            settlement = services.retreat_service.settle_retreat(character_id=character_id)
            snapshot = services.cultivation_panel_service.get_panel_snapshot(character_id=character_id)
            return settlement, snapshot

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: CultivationPanelSnapshot,
        owner_user_id: int,
        selected_duration_hours: int,
        action_note: CultivationActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            action_note=action_note,
            selected_duration_hours=selected_duration_hours,
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
        snapshot: CultivationPanelSnapshot,
        owner_user_id: int,
        selected_duration_hours: int,
        action_note: CultivationActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            action_note=action_note,
            selected_duration_hours=selected_duration_hours,
        )
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: CultivationPanelSnapshot,
        owner_user_id: int,
        selected_duration_hours: int,
        action_note: CultivationActionNote | None,
    ) -> PanelMessagePayload:
        view = CultivationPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=snapshot.growth_snapshot.character_id,
            snapshot=snapshot,
            selected_duration_hours=selected_duration_hours,
            timeout=self._panel_timeout,
        )
        embed = CultivationPanelPresenter.build_embed(
            snapshot=snapshot,
            selected_duration_hours=selected_duration_hours,
            action_note=action_note,
        )
        return PanelMessagePayload(embed=embed, view=view)

    @staticmethod
    def _build_practice_lines(*, result: PracticeOnceResult) -> tuple[str, ...]:
        lines = [
            f"请求修为：{result.requested_amount}",
            f"实际增加：{result.applied_amount}",
            f"当前总修为：{result.snapshot.growth_snapshot.cultivation_value}",
        ]
        if result.stage_changed:
            lines.append(
                "小阶段变化："
                f"{_STAGE_NAME_BY_ID.get(result.previous_stage_id, result.previous_stage_id)}"
                f" → {_STAGE_NAME_BY_ID.get(result.snapshot.growth_snapshot.stage_id, result.snapshot.growth_snapshot.stage_id)}"
            )
        return tuple(lines)

    @staticmethod
    def _build_settlement_lines(*, settlement: RetreatSettlementResult) -> tuple[str, ...]:
        lines = [
            f"闭关区间：{CultivationPanelPresenter._format_datetime(settlement.started_at)} 至 {CultivationPanelPresenter._format_datetime(settlement.scheduled_end_at)}",
            f"修为：+{settlement.applied_cultivation}",
            f"感悟：+{settlement.reward.comprehension_amount}",
            f"灵石：+{settlement.reward.spirit_stone_amount}",
            f"当前总修为：{settlement.growth_snapshot.cultivation_value}",
        ]
        return tuple(lines)


__all__ = [
    "CultivationPanelController",
    "CultivationPanelPresenter",
    "CultivationPanelView",
]
