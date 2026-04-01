"""Discord 突破三问交互。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.breakthrough import (
    BreakthroughMaterialPageSnapshot,
    BreakthroughPanelService,
    BreakthroughPanelServiceError,
    BreakthroughPanelSnapshot,
    BreakthroughTrialChallengeResult,
    BreakthroughTrialService,
    BreakthroughTrialServiceError,
)
from application.character import (
    BreakthroughExecutionBlockedError,
    BreakthroughExecutionResult,
    CharacterProgressionService,
    CharacterProgressionServiceError,
)
from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from infrastructure.db.session import session_scope
from infrastructure.discord.battle_replay_message import BattleReplayMessagePlayer
from infrastructure.discord.character_panel import (
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)

logger = logging.getLogger(__name__)

_PANEL_TIMEOUT_SECONDS = 20 * 60
_RESOURCE_NAME_BY_ID = {
    "qi_condensation_grass": "凝气草",
    "foundation_pill": "筑基丹",
    "spirit_pattern_stone": "灵纹石",
    "core_congealing_pellet": "凝丹丸",
    "fire_essence_sand": "离火砂",
    "nascent_soul_flower": "元婴花",
    "soul_binding_jade": "缚魂玉",
    "deity_heart_seed": "化神心种",
    "thunder_pattern_branch": "雷纹枝",
    "void_break_crystal": "破虚晶",
    "star_soul_dust": "星魂尘",
    "body_integration_bone": "合体骨",
    "myriad_gold_paste": "万金膏",
    "great_vehicle_core": "大乘核",
    "heaven_pattern_silk": "天纹丝",
    "tribulation_lightning_talisman": "劫雷符",
    "immortal_marrow_liquid": "仙髓液",
}


class BreakthroughPanelServiceBundle(Protocol):
    """突破三问控制器依赖的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    breakthrough_panel_service: BreakthroughPanelService
    breakthrough_trial_service: BreakthroughTrialService
    character_progression_service: CharacterProgressionService


@dataclass(frozen=True, slots=True)
class BreakthroughActionResult:
    """正式叩天门后的结果投影。"""

    snapshot: BreakthroughPanelSnapshot | None
    execution_result: BreakthroughExecutionResult | None = None
    blocked_message: str | None = None


class BreakthroughPanelPresenter:
    """负责把突破三问快照投影为 Discord Embed。"""

    @classmethod
    def build_root_embed(cls, *, snapshot: BreakthroughPanelSnapshot) -> discord.Embed:
        status = snapshot.root_status
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜破境三问",
            description="\n".join(
                (
                    f"当前境界：{status.current_realm_display} → {status.next_realm_name}",
                    f"资格状态：{'已得资格' if status.qualification_obtained else '未得资格'}",
                    f"材料状态：{'已齐' if status.material_ready else '仍缺若干'}",
                    f"破境状态：{'可叩门' if status.can_breakthrough else '条件未满'}",
                    "此地只问玄关，不产材料机缘。",
                )
            ),
            color=discord.Color.dark_blue(),
        )
        embed.set_footer(text="此页只留三问：问玄关、检灵材、叩天门")
        return embed

    @classmethod
    def build_qualification_embed(cls, *, snapshot: BreakthroughPanelSnapshot) -> discord.Embed:
        page = snapshot.qualification_page
        trial_name = page.trial_name or "当前暂无可问玄关"
        material_gap = "已无缺漏" if snapshot.material_page.all_satisfied else snapshot.material_page.gap_summary
        description_lines = [f"今番所问：{trial_name}"]
        if page.environment_rule:
            description_lines.append(f"关前气象：{page.environment_rule}")
        description_lines.extend(
            (
                "",
                page.atmosphere_text,
                "",
                f"当前是否通过：{'已通过' if page.passed else '未通过'}",
                f"材料缺口：{material_gap}",
            )
        )
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜问玄关",
            description="\n".join(description_lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="此页只问玄关，不写杂项，不述旁枝。")
        return embed

    @classmethod
    def build_material_embed(cls, *, snapshot: BreakthroughPanelSnapshot) -> discord.Embed:
        page = snapshot.material_page
        if not page.requirements:
            material_block = "当前这一境暂无额外灵材可验。"
        else:
            material_block = "\n".join(cls._build_material_line(item) for item in page.requirements)
        opening_line = "灵材俱在袖中，待你定息之后，自可再叩天门。" if page.all_satisfied else f"仍缺：{page.gap_summary}"
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜检灵材",
            description="\n".join(
                (
                    opening_line,
                    "此页仅作校验，材料机缘后续另开秘境。",
                )
            ),
            color=discord.Color.dark_teal(),
        )
        embed.add_field(name="所需灵材", value=material_block, inline=False)
        return embed

    @classmethod
    def build_trial_result_embed(
        cls,
        *,
        snapshot: BreakthroughPanelSnapshot,
        result: BreakthroughTrialChallengeResult,
    ) -> discord.Embed:
        if result.settlement.victory and result.settlement.qualification_granted:
            body_lines = (
                f"你自“{result.trial_name}”中回身而出，门前压着的那一道灵势终于松开。",
                "此番玄关已明，突破资格已落掌中。",
            )
            color = discord.Color.gold()
        elif result.settlement.victory:
            body_lines = (
                f"你已从“{result.trial_name}”中闯出一线生门。",
                "但这一回只是留下回响，尚未真正换来新的破境资格。",
            )
            color = discord.Color.blurple()
        else:
            body_lines = (
                f"你在“{result.trial_name}”前被迫收势，门内威压仍未让开。",
                "此番玄关未开，资格仍未落定。",
            )
            color = discord.Color.red()
        material_line = (
            "材料已齐，只待心火与时机同到。"
            if snapshot.material_page.all_satisfied
            else f"余下材料仍缺：{snapshot.material_page.gap_summary}。"
        )
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜叩关余响",
            description="\n".join((*body_lines, material_line, "此地只证玄关，不赐材料机缘。")),
            color=color,
        )
        return embed

    @classmethod
    def build_execution_result_embed(
        cls,
        *,
        snapshot: BreakthroughPanelSnapshot,
        action_result: BreakthroughActionResult,
    ) -> discord.Embed:
        if action_result.execution_result is None:
            embed = discord.Embed(
                title=f"{snapshot.overview.character_name}｜叩天门",
                description="\n".join(
                    (
                        "天门未应。",
                        action_result.blocked_message or "此番条件未满，仍需回身自整。",
                    )
                ),
                color=discord.Color.red(),
            )
            return embed
        result = action_result.execution_result
        consumed_items = [
            f"{_RESOURCE_NAME_BY_ID.get(item.item_id, item.item_id)} ×{item.quantity}"
            for item in result.consumed_items
            if item.quantity > 0
        ]
        material_line = "所耗灵材：无。" if not consumed_items else f"所耗灵材：{'、'.join(consumed_items)}。"
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜叩天门",
            description="\n".join(
                (
                    f"{result.from_realm_name}旧壁已裂，{result.to_realm_name}的新气终于落入经脉。",
                    f"你已踏入 {result.to_realm_name}·{result.new_stage_name}。",
                    material_line,
                    "此身境路，自此另开一重。",
                )
            ),
            color=discord.Color.gold(),
        )
        return embed

    @staticmethod
    def _build_material_line(item) -> str:
        if item.missing_quantity <= 0:
            return f"{item.item_name}：持有 {item.owned_quantity} / 所需 {item.required_quantity} / 已齐"
        return (
            f"{item.item_name}：持有 {item.owned_quantity} / 所需 {item.required_quantity} / 缺 {item.missing_quantity}"
        )


class _OwnerLockedView(discord.ui.View):
    """仅允许发起者操作的私有视图。"""

    def __init__(
        self,
        *,
        controller: BreakthroughPanelController,
        owner_user_id: int,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller
        self.owner_user_id = owner_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self._controller.responder.send_private_error(interaction, message="该私有消息仅允许发起者操作。")
        return False


class BreakthroughRootView(_OwnerLockedView):
    """破境三问根页视图。"""

    def __init__(
        self,
        *,
        controller: BreakthroughPanelController,
        owner_user_id: int,
        snapshot: BreakthroughPanelSnapshot,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(controller=controller, owner_user_id=owner_user_id, timeout=timeout)
        self.character_id = snapshot.overview.character_id
        self.snapshot = snapshot
        self.sync_component_state()

    def sync_component_state(self) -> None:
        self.ask_gate.disabled = self.snapshot.qualification_page.mapping_id is None
        self.check_materials.disabled = (
            not self.snapshot.material_page.requirements and self.snapshot.precheck.target_realm_id is None
        )
        self.knock_heaven_gate.disabled = not self.snapshot.root_status.can_breakthrough

    @discord.ui.button(label="问玄关", style=discord.ButtonStyle.primary)
    async def ask_gate(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.show_qualification_page(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
        )

    @discord.ui.button(label="检灵材", style=discord.ButtonStyle.secondary)
    async def check_materials(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.show_material_page(
            interaction,
            character_id=self.character_id,
        )

    @discord.ui.button(label="叩天门", style=discord.ButtonStyle.success)
    async def knock_heaven_gate(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.execute_breakthrough(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
        )


class BreakthroughQualificationView(_OwnerLockedView):
    """问玄关页面视图。"""

    def __init__(
        self,
        *,
        controller: BreakthroughPanelController,
        owner_user_id: int,
        snapshot: BreakthroughPanelSnapshot,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(controller=controller, owner_user_id=owner_user_id, timeout=timeout)
        self.character_id = snapshot.overview.character_id
        self.mapping_id = snapshot.qualification_page.mapping_id
        self.start_trial.disabled = not snapshot.qualification_page.start_trial_enabled

    @discord.ui.button(label="开始突破试炼", style=discord.ButtonStyle.success)
    async def start_trial(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.start_trial(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            mapping_id=self.mapping_id,
        )


class BreakthroughPanelController:
    """组织突破三问与叩关行记交互。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        service_bundle_factory,
        responder: DiscordInteractionVisibilityResponder | None = None,
        replay_message_player: BattleReplayMessagePlayer | None = None,
        panel_timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._service_bundle_factory = service_bundle_factory
        self.responder = responder or DiscordInteractionVisibilityResponder()
        self._battle_replay_message_player = replay_message_player or BattleReplayMessagePlayer(
            responder=self.responder,
        )
        self._panel_timeout = panel_timeout

    async def open_panel_by_discord_user_id(self, interaction: discord.Interaction) -> None:
        """按 Discord 用户标识打开突破三问根页。"""
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterPanelQueryServiceError,
            CharacterProgressionServiceError,
            BreakthroughPanelServiceError,
            BreakthroughTrialServiceError,
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_root_panel(interaction, snapshot=snapshot, owner_user_id=interaction.user.id)

    async def open_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """按角色标识打开突破三问根页。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterProgressionServiceError,
            BreakthroughPanelServiceError,
            BreakthroughTrialServiceError,
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_root_panel(interaction, snapshot=snapshot, owner_user_id=interaction.user.id)

    async def show_qualification_page(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
    ) -> None:
        """发送问玄关页面。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterProgressionServiceError,
            BreakthroughPanelServiceError,
            BreakthroughTrialServiceError,
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self.responder.send_message(
            interaction,
            payload=self._build_qualification_payload(snapshot=snapshot, owner_user_id=owner_user_id),
            visibility=PanelVisibility.PRIVATE,
        )

    async def show_material_page(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
    ) -> None:
        """发送检灵材页面。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (
            CharacterProgressionServiceError,
            BreakthroughPanelServiceError,
            BreakthroughTrialServiceError,
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self.responder.send_message(
            interaction,
            payload=PanelMessagePayload(embed=BreakthroughPanelPresenter.build_material_embed(snapshot=snapshot)),
            visibility=PanelVisibility.PRIVATE,
        )

    async def start_trial(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        mapping_id: str | None,
    ) -> None:
        """执行一次突破试炼，并发送独立叩关行记。"""
        try:
            result, snapshot = self._challenge_trial(character_id=character_id, mapping_id=mapping_id)
        except (
            CharacterProgressionServiceError,
            BreakthroughPanelServiceError,
            BreakthroughTrialServiceError,
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self.responder.edit_message(
            interaction,
            payload=self._build_qualification_payload(snapshot=snapshot, owner_user_id=owner_user_id),
        )
        await self._play_trial_journey_if_available(interaction, snapshot=snapshot)
        await self._send_trial_result_message(interaction, snapshot=snapshot, result=result)

    async def execute_breakthrough(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
    ) -> None:
        """执行正式叩天门，并回写根页状态。"""
        try:
            action_result = self._execute_breakthrough(character_id=character_id)
        except (
            CharacterProgressionServiceError,
            BreakthroughPanelServiceError,
            BreakthroughTrialServiceError,
        ) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        if action_result.snapshot is None:
            await self.responder.send_private_error(interaction, message="突破后状态回读失败。")
            return
        await self.responder.edit_message(
            interaction,
            payload=self._build_root_payload(snapshot=action_result.snapshot, owner_user_id=owner_user_id),
        )
        await self._send_execution_result_message(
            interaction,
            snapshot=action_result.snapshot,
            action_result=action_result,
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services: BreakthroughPanelServiceBundle = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_panel_snapshot(self, *, character_id: int) -> BreakthroughPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services: BreakthroughPanelServiceBundle = self._service_bundle_factory(session)
            return services.breakthrough_panel_service.get_panel_snapshot(character_id=character_id)

    def _challenge_trial(
        self,
        *,
        character_id: int,
        mapping_id: str | None,
    ) -> tuple[BreakthroughTrialChallengeResult, BreakthroughPanelSnapshot]:
        with session_scope(self._session_factory) as session:
            services: BreakthroughPanelServiceBundle = self._service_bundle_factory(session)
            result = services.breakthrough_trial_service.challenge_trial(
                character_id=character_id,
                mapping_id=mapping_id,
            )
            snapshot = services.breakthrough_panel_service.get_panel_snapshot(character_id=character_id)
            return result, snapshot

    def _execute_breakthrough(self, *, character_id: int) -> BreakthroughActionResult:
        with session_scope(self._session_factory) as session:
            services: BreakthroughPanelServiceBundle = self._service_bundle_factory(session)
            try:
                result = services.character_progression_service.execute_breakthrough(character_id=character_id)
            except BreakthroughExecutionBlockedError as exc:
                snapshot = services.breakthrough_panel_service.get_panel_snapshot(character_id=character_id)
                return BreakthroughActionResult(snapshot=snapshot, blocked_message=str(exc))
            snapshot = services.breakthrough_panel_service.get_panel_snapshot(character_id=character_id)
            return BreakthroughActionResult(snapshot=snapshot, execution_result=result)

    async def _send_root_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BreakthroughPanelSnapshot,
        owner_user_id: int,
    ) -> None:
        await self.responder.send_message(
            interaction,
            payload=self._build_root_payload(snapshot=snapshot, owner_user_id=owner_user_id),
            visibility=PanelVisibility.PRIVATE,
        )

    def _build_root_payload(
        self,
        *,
        snapshot: BreakthroughPanelSnapshot,
        owner_user_id: int,
    ) -> PanelMessagePayload:
        return PanelMessagePayload(
            embed=BreakthroughPanelPresenter.build_root_embed(snapshot=snapshot),
            view=BreakthroughRootView(
                controller=self,
                owner_user_id=owner_user_id,
                snapshot=snapshot,
                timeout=self._panel_timeout,
            ),
        )

    def _build_qualification_payload(
        self,
        *,
        snapshot: BreakthroughPanelSnapshot,
        owner_user_id: int,
    ) -> PanelMessagePayload:
        return PanelMessagePayload(
            embed=BreakthroughPanelPresenter.build_qualification_embed(snapshot=snapshot),
            view=BreakthroughQualificationView(
                controller=self,
                owner_user_id=owner_user_id,
                snapshot=snapshot,
                timeout=self._panel_timeout,
            ),
        )

    async def _play_trial_journey_if_available(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BreakthroughPanelSnapshot,
    ) -> None:
        recent_trial = snapshot.recent_trial
        if recent_trial is None or recent_trial.battle_replay_presentation is None:
            return
        try:
            await self._battle_replay_message_player.play(
                interaction,
                presentation=recent_trial.battle_replay_presentation,
            )
        except (discord.Forbidden, discord.HTTPException):
            return
        except Exception:
            logger.exception(
                "叩关行记回放发送失败",
                extra={
                    "character_id": snapshot.overview.character_id,
                    "battle_report_id": recent_trial.battle_report_id,
                },
            )

    async def _send_trial_result_message(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BreakthroughPanelSnapshot,
        result: BreakthroughTrialChallengeResult,
    ) -> None:
        await self.responder.send_private_followup_message(
            interaction,
            payload=PanelMessagePayload(
                embed=BreakthroughPanelPresenter.build_trial_result_embed(snapshot=snapshot, result=result)
            ),
        )

    async def _send_execution_result_message(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: BreakthroughPanelSnapshot,
        action_result: BreakthroughActionResult,
    ) -> None:
        await self.responder.send_private_followup_message(
            interaction,
            payload=PanelMessagePayload(
                embed=BreakthroughPanelPresenter.build_execution_result_embed(
                    snapshot=snapshot,
                    action_result=action_result,
                )
            ),
        )


__all__ = [
    "BreakthroughPanelController",
    "BreakthroughPanelPresenter",
    "BreakthroughQualificationView",
    "BreakthroughRootView",
]
