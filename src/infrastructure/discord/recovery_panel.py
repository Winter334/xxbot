"""Discord 恢复状态私有面板。"""

from __future__ import annotations

from dataclasses import dataclass

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from application.healing import HealingPanelService, HealingPanelServiceError, HealingPanelSnapshot, RecoveryActionResult
from infrastructure.db.session import session_scope
from infrastructure.discord.character_panel import (
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)

_PANEL_TIMEOUT_SECONDS = 20 * 60
_HEALING_STATUS_RUNNING = "running"
_HEALING_STATUS_COMPLETED = "completed"


class RecoveryPanelServiceBundle:
    """恢复面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    healing_panel_service: HealingPanelService


@dataclass(frozen=True, slots=True)
class RecoveryActionNote:
    """恢复动作反馈。"""

    title: str
    lines: tuple[str, ...]


class RecoveryPanelPresenter:
    """负责把恢复状态快照投影为 Discord Embed。"""

    @classmethod
    def build_embed(
        cls,
        *,
        snapshot: HealingPanelSnapshot,
        action_note: RecoveryActionNote | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"角色 {snapshot.character_id}｜恢复状态",
            description="仅操作者可见",
            color=discord.Color.teal(),
        )
        embed.add_field(name="恢复摘要", value=cls._build_summary_block(snapshot=snapshot), inline=False)
        embed.add_field(name="相关状态", value=cls._build_related_block(snapshot=snapshot), inline=False)
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        embed.set_footer(text="规则：20 分钟恢复 100%，中途结束按比例恢复")
        return embed

    @classmethod
    def _build_summary_block(cls, *, snapshot: HealingPanelSnapshot) -> str:
        lines = [
            f"当前生命：{cls._format_ratio(snapshot.current_hp_ratio)}",
            f"当前灵力：{cls._format_ratio(snapshot.current_mp_ratio)}",
            f"推断伤势：{cls._format_injury(snapshot.inferred_injury_level)}",
            f"恢复状态：{cls._format_healing_status(snapshot.healing_status)}",
        ]
        if snapshot.healing_status == _HEALING_STATUS_RUNNING:
            lines.extend(
                (
                    f"开始时间：{cls._format_datetime(snapshot.started_at)}",
                    f"满恢复时间：{cls._format_datetime(snapshot.scheduled_end_at)}",
                    f"恢复进度：{float(snapshot.recovery_progress) * 100:.1f}%",
                    f"预计生命：{cls._format_ratio(snapshot.expected_hp_ratio)}",
                    f"预计灵力：{cls._format_ratio(snapshot.expected_mp_ratio)}",
                    f"剩余时间：{cls._format_duration(snapshot.remaining_recovery_seconds)}",
                    f"提示：{snapshot.status_hint}",
                )
            )
        else:
            lines.append(f"提示：{snapshot.status_hint}")
        return "\n".join(lines)

    @classmethod
    def _build_related_block(cls, *, snapshot: HealingPanelSnapshot) -> str:
        if snapshot.can_complete_recovery:
            action_text = "可完成恢复并回满状态"
        elif snapshot.can_interrupt_recovery:
            action_text = "可结束恢复并按比例结算"
        elif snapshot.can_start_recovery:
            action_text = "可开始恢复"
        else:
            action_text = "当前不可执行"
        lines = [
            f"闭关中：{'是' if snapshot.retreat_running else '否'}",
            f"无尽运行中：{'是' if snapshot.endless_running else '否'}",
            f"当前动作：{action_text}",
        ]
        if snapshot.healing_status == _HEALING_STATUS_RUNNING:
            lines.append(
                f"按钮行为：{'完成恢复' if snapshot.can_complete_recovery else '结束恢复并按比例结算'}"
            )
        else:
            lines.append("按钮行为：开始恢复")
        return "\n".join(lines)

    @staticmethod
    def _format_ratio(value) -> str:
        return f"{float(value) * 100:.1f}%"

    @staticmethod
    def _format_datetime(value) -> str:
        if value is None:
            return "-"
        return f"{discord.utils.format_dt(value, style='f')}｜{discord.utils.format_dt(value, style='R')}"

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        if total_seconds <= 0:
            return "0 分钟"
        minutes = total_seconds // 60
        if total_seconds % 60:
            minutes += 1
        hours, remaining_minutes = divmod(minutes, 60)
        parts: list[str] = []
        if hours > 0:
            parts.append(f"{hours} 小时")
        if remaining_minutes > 0 or not parts:
            parts.append(f"{remaining_minutes} 分钟")
        return "".join(parts)

    @staticmethod
    def _format_injury(value: str | None) -> str:
        mapping = {
            None: "-",
            "none": "无伤",
            "light": "轻伤",
            "medium": "中伤",
            "heavy": "重伤",
            "defeated": "濒死",
        }
        return mapping.get(value, value or "-")

    @staticmethod
    def _format_healing_status(value: str) -> str:
        mapping = {
            "none": "无疗伤记录",
            _HEALING_STATUS_RUNNING: "打坐恢复中",
            _HEALING_STATUS_COMPLETED: "已完成",
        }
        return mapping.get(value, value)


class RecoveryPanelView(discord.ui.View):
    """恢复状态私有面板视图。"""

    def __init__(
        self,
        *,
        controller: RecoveryPanelController,
        owner_user_id: int,
        character_id: int,
        snapshot: HealingPanelSnapshot,
        timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = character_id
        self.snapshot = snapshot
        self._sync_component_state()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self._controller.responder.send_private_error(interaction, message="该私有面板仅允许发起者操作。")
        return False

    def _sync_component_state(self) -> None:
        self.execute_recovery.disabled = not (
            self.snapshot.can_start_recovery
            or self.snapshot.can_complete_recovery
            or self.snapshot.can_interrupt_recovery
        )
        if self.snapshot.can_complete_recovery:
            self.execute_recovery.label = "完成恢复"
            self.execute_recovery.style = discord.ButtonStyle.success
        elif self.snapshot.can_interrupt_recovery:
            self.execute_recovery.label = "结束恢复"
            self.execute_recovery.style = discord.ButtonStyle.danger
        else:
            self.execute_recovery.label = "开始恢复"
            self.execute_recovery.style = discord.ButtonStyle.success

    @discord.ui.button(label="执行恢复", style=discord.ButtonStyle.success)
    async def execute_recovery(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.execute_recovery(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
        )

    @discord.ui.button(label="刷新状态", style=discord.ButtonStyle.secondary)
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
        )


class RecoveryPanelController:
    """组织恢复状态私有面板交互。"""

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
        """按 Discord 用户标识打开恢复状态面板。"""
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_snapshot(character_id=character_id)
        except (CharacterPanelQueryServiceError, HealingPanelServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(interaction, snapshot=snapshot, owner_user_id=interaction.user.id)

    async def open_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """按角色标识打开恢复状态面板。"""
        try:
            snapshot = self._load_snapshot(character_id=character_id)
        except HealingPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(interaction, snapshot=snapshot, owner_user_id=interaction.user.id)

    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
    ) -> None:
        """刷新恢复状态面板。"""
        try:
            snapshot = self._load_snapshot(character_id=character_id)
        except HealingPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(interaction, snapshot=snapshot, owner_user_id=owner_user_id)

    async def execute_recovery(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
    ) -> None:
        """执行单次恢复动作。"""
        try:
            result = self._execute_recovery(character_id=character_id)
        except HealingPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_note = RecoveryActionNote(
            title="恢复反馈",
            lines=self._build_action_lines(result=result),
        )
        await self._edit_panel(
            interaction,
            snapshot=result.snapshot,
            owner_user_id=owner_user_id,
            action_note=action_note,
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_snapshot(self, *, character_id: int) -> HealingPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.healing_panel_service.get_panel_snapshot(character_id=character_id)

    def _execute_recovery(self, *, character_id: int) -> RecoveryActionResult:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.healing_panel_service.execute_recovery_action(character_id=character_id)

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: HealingPanelSnapshot,
        owner_user_id: int,
        action_note: RecoveryActionNote | None = None,
    ) -> None:
        payload = self._build_payload(snapshot=snapshot, owner_user_id=owner_user_id, action_note=action_note)
        await self.responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PRIVATE)

    async def _edit_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: HealingPanelSnapshot,
        owner_user_id: int,
        action_note: RecoveryActionNote | None = None,
    ) -> None:
        payload = self._build_payload(snapshot=snapshot, owner_user_id=owner_user_id, action_note=action_note)
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: HealingPanelSnapshot,
        owner_user_id: int,
        action_note: RecoveryActionNote | None,
    ) -> PanelMessagePayload:
        view = RecoveryPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=snapshot.character_id,
            snapshot=snapshot,
            timeout=self._panel_timeout,
        )
        embed = RecoveryPanelPresenter.build_embed(snapshot=snapshot, action_note=action_note)
        return PanelMessagePayload(embed=embed, view=view)

    @staticmethod
    def _build_action_lines(*, result: RecoveryActionResult) -> tuple[str, ...]:
        if result.action_type == "start":
            return (
                "已开始打坐恢复。",
                "规则：20 分钟恢复 100%，中途结束按比例恢复。",
                f"预计结束：{RecoveryPanelPresenter._format_datetime(result.snapshot.scheduled_end_at)}",
            )
        if result.action_type == "interrupt":
            return (
                "已结束打坐恢复，并按当前进度结算。",
                f"当前生命：{RecoveryPanelPresenter._format_ratio(result.snapshot.current_hp_ratio)}",
                f"当前灵力：{RecoveryPanelPresenter._format_ratio(result.snapshot.current_mp_ratio)}",
            )
        return (
            "恢复已完成。",
            f"当前生命：{RecoveryPanelPresenter._format_ratio(result.snapshot.current_hp_ratio)}",
            f"当前灵力：{RecoveryPanelPresenter._format_ratio(result.snapshot.current_mp_ratio)}",
        )


__all__ = [
    "RecoveryPanelController",
    "RecoveryPanelPresenter",
    "RecoveryPanelView",
]
