"""Discord 排行榜私有面板。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character.panel_query_service import CharacterPanelQueryService, CharacterPanelQueryServiceError
from application.ranking.leaderboard_panel_service import (
    LeaderboardPanelEntryView,
    LeaderboardPanelSelfSummary,
    LeaderboardPanelService,
    LeaderboardPanelServiceError,
    LeaderboardPanelSnapshot,
)
from domain.ranking import LeaderboardBoardType
from infrastructure.db.session import session_scope
from infrastructure.discord.character_panel import (
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)

_PANEL_TIMEOUT_SECONDS = 20 * 60
_SHARE_TOP_LIMIT = 3
_STATUS_LABEL_BY_VALUE = {
    "preparing": "准备中",
    "ready": "已就绪",
}
_BOARD_COLOR_BY_TYPE = {
    LeaderboardBoardType.POWER.value: discord.Color.gold(),
    LeaderboardBoardType.PVP_CHALLENGE.value: discord.Color.dark_magenta(),
    LeaderboardBoardType.ENDLESS_DEPTH.value: discord.Color.dark_teal(),
}


class LeaderboardPanelServiceBundle(Protocol):
    """排行榜面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    leaderboard_panel_service: LeaderboardPanelService


@dataclass(frozen=True, slots=True)
class LeaderboardActionNote:
    """排行榜面板动作反馈。"""

    title: str
    lines: tuple[str, ...]


class LeaderboardPanelPresenter:
    """负责把排行榜聚合快照投影为 Discord Embed。"""

    @classmethod
    def build_embed(
        cls,
        *,
        snapshot: LeaderboardPanelSnapshot,
        action_note: LeaderboardActionNote | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{snapshot.overview.character_name}｜{snapshot.board_name}",
            description="仅操作者可见",
            color=_BOARD_COLOR_BY_TYPE.get(snapshot.board_type, discord.Color.blurple()),
        )
        embed.add_field(name="榜单状态", value=cls._build_status_block(snapshot=snapshot), inline=False)
        embed.add_field(name="当前页摘要", value=cls._build_page_block(snapshot=snapshot), inline=False)
        embed.add_field(name="我的当前排名", value=cls._build_self_block(snapshot=snapshot), inline=False)
        if action_note is not None and action_note.lines:
            embed.add_field(name=action_note.title, value="\n".join(action_note.lines), inline=False)
        embed.set_footer(text="分类切换、分页、刷新默认仅自己可见；公开频道只发送精简榜单摘要")
        return embed

    @classmethod
    def _build_status_block(cls, *, snapshot: LeaderboardPanelSnapshot) -> str:
        status_label = _STATUS_LABEL_BY_VALUE.get(snapshot.status, snapshot.status)
        lines = [
            f"当前榜单：{snapshot.board_name}",
            f"状态：{status_label}",
            f"页码：第 {snapshot.page}/{snapshot.total_pages} 页｜每页 {snapshot.page_size} 条｜共 {snapshot.total_entries} 名",
            (
                f"快照时间：{_format_datetime(snapshot.snapshot_generated_at)}"
                if snapshot.snapshot_generated_at is not None
                else "快照时间：暂无可用快照"
            ),
        ]
        if snapshot.status == "preparing":
            lines.append("说明：当前榜单准备中，已走现有数据能力，稍后可点击刷新重试。")
        elif snapshot.stale:
            lines.append("说明：当前展示最近一次快照，后台正在刷新数据。")
        else:
            lines.append("说明：当前展示最新可用快照。")
        return "\n".join(lines)

    @classmethod
    def _build_page_block(cls, *, snapshot: LeaderboardPanelSnapshot) -> str:
        if snapshot.status == "preparing" and not snapshot.entries:
            return "当前榜单仍在准备中，暂时没有可展示的分页结果。"
        if not snapshot.entries:
            return "当前榜单暂无可展示条目。"
        lines: list[str] = []
        for entry in snapshot.entries:
            lines.extend(cls._build_entry_lines(snapshot=snapshot, entry=entry))
        return "\n".join(lines)

    @classmethod
    def _build_entry_lines(
        cls,
        *,
        snapshot: LeaderboardPanelSnapshot,
        entry: LeaderboardPanelEntryView,
    ) -> list[str]:
        identity = cls._build_identity_suffix(title_name=entry.title_name, badge_name=entry.badge_name)
        head_line = f"#{entry.rank} {entry.character_name}"
        if identity:
            head_line += f"｜{identity}"
        main_skill_name = (
            _read_optional_str(entry.summary.get("main_skill_name"))
            or _read_optional_str(entry.summary.get("main_path_name"))
            or "未定功法"
        )
        main_skill_summary = _build_main_skill_summary(entry.summary)
        detail_line = (
            f"{_format_realm(summary=entry.summary)}｜主修 {main_skill_name}｜"
            f"{cls._format_score_label(board_type=snapshot.board_type, display_score=entry.display_score)}"
        )
        extra_line = cls._build_entry_extra_line(snapshot=snapshot, entry=entry)
        if extra_line:
            return [head_line, f"{detail_line}｜{extra_line}", main_skill_summary]
        return [head_line, detail_line, main_skill_summary]

    @classmethod
    def _build_entry_extra_line(
        cls,
        *,
        snapshot: LeaderboardPanelSnapshot,
        entry: LeaderboardPanelEntryView,
    ) -> str:
        summary = entry.summary
        if snapshot.board_type == LeaderboardBoardType.PVP_CHALLENGE.value:
            best_rank = _read_int(summary.get("best_rank"), default=entry.rank)
            challenge_tier = _read_optional_str(summary.get("challenge_tier")) or "-"
            return f"奖励档位 {challenge_tier}｜历史最佳 #{best_rank}"
        if snapshot.board_type == LeaderboardBoardType.ENDLESS_DEPTH.value:
            region_name = _read_optional_str(summary.get("highest_region_name")) or "未知区域"
            return f"区域 {region_name}"
        highest_endless_floor = _read_int(summary.get("highest_endless_floor"))
        if highest_endless_floor > 0:
            return f"无涯渊境纪录 第 {highest_endless_floor} 层"
        return ""

    @classmethod
    def _build_self_block(cls, *, snapshot: LeaderboardPanelSnapshot) -> str:
        self_summary = snapshot.self_summary
        summary = self_summary.summary
        lines = [f"角色：{self_summary.character_name}"]
        if self_summary.rank is None:
            lines.append("当前名次：暂未上榜")
        else:
            lines.append(f"当前名次：第 {self_summary.rank} 名")
        lines.append(
            "当前值：" + (
                cls._format_score_label(board_type=snapshot.board_type, display_score=self_summary.display_score)
                if self_summary.display_score is not None
                else "暂无"
            )
        )
        lines.append(f"称号：{self_summary.title_name or '无'}")
        lines.append(f"徽记：{self_summary.badge_name or '无'}")
        lines.append(f"境界：{_format_realm(summary=summary)}")
        lines.append(
            f"主修：{_read_optional_str(summary.get('main_skill_name')) or summary.get('main_path_name') or snapshot.overview.main_skill.skill_name}"
        )
        lines.append(_build_main_skill_summary(summary))
        lines.extend(cls._build_self_extra_lines(snapshot=snapshot, self_summary=self_summary))
        return "\n".join(lines)

    @classmethod
    def _build_self_extra_lines(
        cls,
        *,
        snapshot: LeaderboardPanelSnapshot,
        self_summary: LeaderboardPanelSelfSummary,
    ) -> list[str]:
        summary = self_summary.summary
        if snapshot.board_type == LeaderboardBoardType.PVP_CHALLENGE.value:
            challenge_tier = _read_optional_str(summary.get("challenge_tier")) or "-"
            best_rank = self_summary.rank if self_summary.rank is not None else None
            best_rank = _read_int(summary.get("best_rank"), default=0) or best_rank
            return [
                f"当前奖励档位：{challenge_tier}",
                (
                    f"历史最佳：第 {best_rank} 名"
                    if isinstance(best_rank, int) and best_rank > 0
                    else "历史最佳：暂无"
                ),
                f"公开战力：{_read_int(summary.get('public_power_score'), default=snapshot.overview.public_power_score)}",
            ]
        if snapshot.board_type == LeaderboardBoardType.ENDLESS_DEPTH.value:
            highest_floor = _read_int(summary.get("highest_endless_floor"))
            region_name = _read_optional_str(summary.get("highest_region_name")) or "未知区域"
            return [
                f"最高层数：第 {highest_floor} 层",
                f"当前区域：{region_name}",
                f"公开战力：{_read_int(summary.get('public_power_score'), default=snapshot.overview.public_power_score)}",
            ]
        return [
            f"公开战力：{_read_int(summary.get('public_power_score'), default=snapshot.overview.public_power_score)}",
            f"无涯渊境纪录：第 {_read_int(summary.get('highest_endless_floor'))} 层",
        ]

    @staticmethod
    def _format_score_label(*, board_type: str, display_score: str | None) -> str:
        resolved = display_score or "暂无"
        if board_type == LeaderboardBoardType.POWER.value:
            return f"天榜战力 {resolved}"
        if board_type == LeaderboardBoardType.PVP_CHALLENGE.value:
            return f"仙榜档位/战力 {resolved}"
        return f"渊境进度 {resolved}"

    @staticmethod
    def _build_identity_suffix(*, title_name: str | None, badge_name: str | None) -> str:
        segments: list[str] = []
        if title_name:
            segments.append(f"称号：{title_name}")
        if badge_name:
            segments.append(f"徽记：{badge_name}")
        return "｜".join(segments)


class LeaderboardSharePresenter:
    """负责生成公开频道中的榜单摘要。"""

    @classmethod
    def build_embed(cls, *, snapshot: LeaderboardPanelSnapshot) -> discord.Embed | None:
        if snapshot.status != "ready" or not snapshot.entries:
            return None
        embed = discord.Embed(
            title=f"{snapshot.board_name}｜公开榜单摘要",
            description="公开频道分享",
            color=_BOARD_COLOR_BY_TYPE.get(snapshot.board_type, discord.Color.blurple()),
        )
        embed.add_field(name="前列名次", value=cls._build_share_block(snapshot=snapshot), inline=False)
        embed.add_field(name="快照说明", value=cls._build_meta_block(snapshot=snapshot), inline=False)
        embed.set_footer(text=f"仅公开前 {len(snapshot.entries)} 名精简摘要，不包含完整分页明细")
        return embed

    @classmethod
    def _build_share_block(cls, *, snapshot: LeaderboardPanelSnapshot) -> str:
        lines: list[str] = []
        for entry in snapshot.entries:
            identity = LeaderboardPanelPresenter._build_identity_suffix(
                title_name=entry.title_name,
                badge_name=entry.badge_name,
            )
            head_line = f"#{entry.rank} {entry.character_name}"
            if identity:
                head_line += f"｜{identity}"
            lines.append(head_line)
            main_skill_name = _read_optional_str(entry.summary.get("main_skill_name")) or _read_optional_str(entry.summary.get("main_path_name")) or "未定功法"
            lines.append(
                (
                    f"{_format_realm(summary=entry.summary)}｜主修 {main_skill_name}｜"
                    f"{LeaderboardPanelPresenter._format_score_label(board_type=snapshot.board_type, display_score=entry.display_score)}"
                )
            )
            lines.append(_build_main_skill_summary(entry.summary))
        return "\n".join(lines)

    @staticmethod
    def _build_meta_block(*, snapshot: LeaderboardPanelSnapshot) -> str:
        status_line = "当前展示最新可用快照"
        if snapshot.stale:
            status_line = "数据刷新中，以下摘要基于最近一次快照"
        return "\n".join(
            (
                f"榜单：{snapshot.board_name}",
                status_line,
                (
                    f"快照时间：{_format_datetime(snapshot.snapshot_generated_at)}"
                    if snapshot.snapshot_generated_at is not None
                    else "快照时间：暂无"
                ),
            )
        )


class LeaderboardPanelView(discord.ui.View):
    """排行榜私有面板视图。"""

    def __init__(
        self,
        *,
        controller: LeaderboardPanelController,
        owner_user_id: int,
        character_id: int,
        snapshot: LeaderboardPanelSnapshot,
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

    def build_embed(self) -> discord.Embed:
        return LeaderboardPanelPresenter.build_embed(snapshot=self.snapshot)

    def _sync_component_state(self) -> None:
        current_board = self.snapshot.board_type
        self.show_power.disabled = current_board == LeaderboardBoardType.POWER.value
        self.show_pvp.disabled = current_board == LeaderboardBoardType.PVP_CHALLENGE.value
        self.show_endless.disabled = current_board == LeaderboardBoardType.ENDLESS_DEPTH.value
        self.previous_page.disabled = not self.snapshot.has_previous_page
        self.next_page.disabled = not self.snapshot.has_next_page
        self.share_summary.disabled = self.snapshot.status != "ready" or not self.snapshot.entries

    @discord.ui.button(label="天榜", style=discord.ButtonStyle.primary, row=0)
    async def show_power(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.switch_board(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            board_type=LeaderboardBoardType.POWER,
        )

    @discord.ui.button(label="仙榜", style=discord.ButtonStyle.primary, row=0)
    async def show_pvp(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.switch_board(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            board_type=LeaderboardBoardType.PVP_CHALLENGE,
        )

    @discord.ui.button(label="渊境榜", style=discord.ButtonStyle.primary, row=0)
    async def show_endless(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.switch_board(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            board_type=LeaderboardBoardType.ENDLESS_DEPTH,
        )

    @discord.ui.button(label="上一页", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.change_page(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            board_type=self.snapshot.board_type,
            page=self.snapshot.page - 1,
        )

    @discord.ui.button(label="下一页", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.change_page(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            board_type=self.snapshot.board_type,
            page=self.snapshot.page + 1,
        )

    @discord.ui.button(label="刷新榜单", style=discord.ButtonStyle.secondary, row=1)
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
            board_type=self.snapshot.board_type,
            page=self.snapshot.page,
        )

    @discord.ui.button(label="分享当前榜单", style=discord.ButtonStyle.success, row=2)
    async def share_summary(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.share_board(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
            board_type=self.snapshot.board_type,
            page=self.snapshot.page,
        )


class LeaderboardPanelController:
    """组织排行榜私有面板交互。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        service_bundle_factory: Callable[[Session], LeaderboardPanelServiceBundle],
        responder: DiscordInteractionVisibilityResponder | None = None,
        panel_timeout: float = _PANEL_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._service_bundle_factory = service_bundle_factory
        self.responder = responder or DiscordInteractionVisibilityResponder()
        self._panel_timeout = panel_timeout

    async def open_panel_by_discord_user_id(self, interaction: discord.Interaction) -> None:
        """按 Discord 用户标识打开排行榜面板。"""
        try:
            character_id = self._load_character_id_by_discord_user_id(discord_user_id=str(interaction.user.id))
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except (CharacterPanelQueryServiceError, LeaderboardPanelServiceError) as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
        )

    async def open_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """按角色标识打开排行榜面板。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id)
        except LeaderboardPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=interaction.user.id,
        )

    async def switch_board(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        board_type: LeaderboardBoardType | str,
    ) -> None:
        """切换排行榜分类。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id, board_type=board_type, page=1)
        except LeaderboardPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            action_note=LeaderboardActionNote(
                title="分类切换",
                lines=(f"已切换到 {snapshot.board_name}。",),
            ),
        )

    async def change_page(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        board_type: LeaderboardBoardType | str,
        page: int,
    ) -> None:
        """切换当前分页。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id, board_type=board_type, page=page)
        except LeaderboardPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            action_note=LeaderboardActionNote(
                title="分页切换",
                lines=(f"当前位于第 {snapshot.page}/{snapshot.total_pages} 页。",),
            ),
        )

    async def refresh_panel(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        board_type: LeaderboardBoardType | str,
        page: int,
    ) -> None:
        """刷新当前榜单页。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id, board_type=board_type, page=page)
        except LeaderboardPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        action_lines = [f"已刷新 {snapshot.board_name} 第 {snapshot.page}/{snapshot.total_pages} 页。"]
        if snapshot.status == "preparing":
            action_lines.append("榜单仍在准备中，请稍后再次刷新。")
        elif snapshot.stale:
            action_lines.append("当前展示最近快照，后台刷新已触发。")
        else:
            action_lines.append("当前已展示最新可用快照。")
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            action_note=LeaderboardActionNote(
                title="刷新结果",
                lines=tuple(action_lines),
            ),
        )

    async def share_board(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
        board_type: LeaderboardBoardType | str,
        page: int,
    ) -> None:
        """把当前榜单的精简摘要公开分享到频道。"""
        try:
            snapshot = self._load_panel_snapshot(character_id=character_id, board_type=board_type, page=page)
            share_snapshot = self._load_share_snapshot(character_id=character_id, board_type=board_type)
        except LeaderboardPanelServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        share_embed = LeaderboardSharePresenter.build_embed(snapshot=share_snapshot)
        if share_embed is None:
            await self.responder.send_private_error(interaction, message="当前榜单没有可公开分享的摘要，请稍后再试。")
            return
        shared = await self._send_public_summary(interaction, embed=share_embed)
        note_lines = (
            f"已向频道公开分享 {share_snapshot.board_name} 前 {_SHARE_TOP_LIMIT} 名摘要。",
            "公开版仅保留前列名次与关键字段，不包含完整分页明细。",
        ) if shared else (
            "公开分享失败：当前频道不可发送榜单摘要，私有面板已保留当前浏览内容。",
        )
        await self._edit_panel(
            interaction,
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            action_note=LeaderboardActionNote(title="公开分享", lines=note_lines),
        )

    def _load_character_id_by_discord_user_id(self, *, discord_user_id: str) -> int:
        with session_scope(self._session_factory) as session:
            services: LeaderboardPanelServiceBundle = self._service_bundle_factory(session)
            overview = services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )
            return overview.character_id

    def _load_panel_snapshot(
        self,
        *,
        character_id: int,
        board_type: LeaderboardBoardType | str = LeaderboardBoardType.POWER,
        page: int = 1,
    ) -> LeaderboardPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services: LeaderboardPanelServiceBundle = self._service_bundle_factory(session)
            return services.leaderboard_panel_service.get_panel_snapshot(
                character_id=character_id,
                board_type=board_type,
                page=page,
            )

    def _load_share_snapshot(
        self,
        *,
        character_id: int,
        board_type: LeaderboardBoardType | str,
    ) -> LeaderboardPanelSnapshot:
        with session_scope(self._session_factory) as session:
            services: LeaderboardPanelServiceBundle = self._service_bundle_factory(session)
            return services.leaderboard_panel_service.get_share_snapshot(
                character_id=character_id,
                board_type=board_type,
                top_limit=_SHARE_TOP_LIMIT,
            )

    async def _send_panel(
        self,
        interaction: discord.Interaction,
        *,
        snapshot: LeaderboardPanelSnapshot,
        owner_user_id: int,
        action_note: LeaderboardActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
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
        snapshot: LeaderboardPanelSnapshot,
        owner_user_id: int,
        action_note: LeaderboardActionNote | None = None,
    ) -> None:
        payload = self._build_payload(
            snapshot=snapshot,
            owner_user_id=owner_user_id,
            action_note=action_note,
        )
        await self.responder.edit_message(interaction, payload=payload)

    def _build_payload(
        self,
        *,
        snapshot: LeaderboardPanelSnapshot,
        owner_user_id: int,
        action_note: LeaderboardActionNote | None,
    ) -> PanelMessagePayload:
        view = LeaderboardPanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=snapshot.overview.character_id,
            snapshot=snapshot,
            timeout=self._panel_timeout,
        )
        embed = LeaderboardPanelPresenter.build_embed(snapshot=snapshot, action_note=action_note)
        return PanelMessagePayload(embed=embed, view=view)

    async def _send_public_summary(self, interaction: discord.Interaction, *, embed: discord.Embed) -> bool:
        if interaction.channel is None:
            return False
        try:
            await interaction.channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            return False
        return True



def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")



def _format_realm(*, summary: Mapping[str, Any]) -> str:
    realm_name = _read_optional_str(summary.get("realm_name")) or "-"
    stage_name = _read_optional_str(summary.get("stage_name")) or "-"
    return f"{realm_name}·{stage_name}"



def _read_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None



def _read_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _build_main_skill_summary(summary: Mapping[str, Any]) -> str:
    main_skill = summary.get("main_skill")
    if not isinstance(main_skill, Mapping):
        main_skill_name = _read_optional_str(summary.get("main_skill_name")) or _read_optional_str(summary.get("main_path_name"))
        if main_skill_name is None:
            return "主修详情：未记录"
        return f"主修详情：{main_skill_name}"
    skill_name = _read_optional_str(main_skill.get("skill_name")) or _read_optional_str(summary.get("main_skill_name")) or "未定功法"
    rank_name = _read_optional_str(main_skill.get("rank_name")) or "未知阶级"
    quality_name = _read_optional_str(main_skill.get("quality_name")) or "未知品质"
    path_name = _read_optional_str(main_skill.get("path_name")) or _read_optional_str(summary.get("main_path_name")) or "未知流派"
    return f"主修详情：{skill_name}｜{rank_name}｜{quality_name}｜{path_name}"


__all__ = [
    "LeaderboardActionNote",
    "LeaderboardPanelController",
    "LeaderboardPanelPresenter",
    "LeaderboardPanelView",
    "LeaderboardSharePresenter",
]
