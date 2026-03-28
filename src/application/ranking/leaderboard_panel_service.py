"""排行榜面板查询适配服务。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from application.character.panel_query_service import CharacterPanelOverview, CharacterPanelQueryService
from application.ranking.leaderboard_query_service import LeaderboardEntryDTO, LeaderboardQueryService
from domain.pvp import PvpRewardDisplayType, PvpRuleService
from domain.ranking import LeaderboardBoardType
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import LeaderboardEntrySnapshot
from infrastructure.db.repositories import SnapshotRepository

_DEFAULT_PAGE_SIZE = 5
_DEFAULT_SHARE_TOP_LIMIT = 3
_BOARD_DISPLAY_NAME_BY_TYPE: dict[LeaderboardBoardType, str] = {
    LeaderboardBoardType.POWER: "天榜",
    LeaderboardBoardType.PVP_CHALLENGE: "仙榜",
    LeaderboardBoardType.ENDLESS_DEPTH: "渊境榜",
}


@dataclass(frozen=True, slots=True)
class LeaderboardPanelEntryView:
    """排行榜面板中的单条展示结果。"""

    rank: int
    character_id: int
    character_name: str
    display_score: str
    summary: dict[str, Any]
    title_name: str | None
    badge_name: str | None


@dataclass(frozen=True, slots=True)
class LeaderboardPanelSelfSummary:
    """操作者在当前榜单中的个人摘要。"""

    character_id: int
    character_name: str
    rank: int | None
    display_score: str | None
    summary: dict[str, Any]
    title_name: str | None
    badge_name: str | None


@dataclass(frozen=True, slots=True)
class LeaderboardPanelSnapshot:
    """排行榜私有面板所需聚合快照。"""

    overview: CharacterPanelOverview
    board_type: str
    board_name: str
    status: str
    stale: bool
    page: int
    page_size: int
    total_entries: int
    total_pages: int
    has_previous_page: bool
    has_next_page: bool
    snapshot_generated_at: datetime | None
    entries: tuple[LeaderboardPanelEntryView, ...]
    self_summary: LeaderboardPanelSelfSummary


class LeaderboardPanelServiceError(RuntimeError):
    """排行榜面板查询服务基础异常。"""


class LeaderboardPanelService:
    """聚合角色总览、榜单分页与可见展示身份。"""

    def __init__(
        self,
        *,
        character_panel_query_service: CharacterPanelQueryService,
        leaderboard_query_service: LeaderboardQueryService,
        snapshot_repository: SnapshotRepository,
        static_config: StaticGameConfig | None = None,
        pvp_rule_service: PvpRuleService | None = None,
    ) -> None:
        self._character_panel_query_service = character_panel_query_service
        self._leaderboard_query_service = leaderboard_query_service
        self._snapshot_repository = snapshot_repository
        self._static_config = static_config or get_static_config()
        self._pvp_rule_service = pvp_rule_service or PvpRuleService(self._static_config)

    def get_panel_snapshot(
        self,
        *,
        character_id: int,
        board_type: LeaderboardBoardType | str = LeaderboardBoardType.POWER,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> LeaderboardPanelSnapshot:
        """读取排行榜私有面板所需聚合数据。"""
        overview = self._character_panel_query_service.get_overview(character_id=character_id)
        resolved_board_type = self._normalize_board_type(board_type)
        resolved_page_size = max(1, page_size)
        page_result = self._leaderboard_query_service.query_leaderboard(
            board_type=resolved_board_type,
            page=max(1, page),
            page_size=resolved_page_size,
        )
        total_pages = self._resolve_total_pages(
            total_entries=page_result.total_entries,
            page_size=page_result.page_size,
        )
        if page_result.total_entries > 0 and page_result.page > total_pages:
            page_result = self._leaderboard_query_service.query_leaderboard(
                board_type=resolved_board_type,
                page=total_pages,
                page_size=resolved_page_size,
            )
            total_pages = self._resolve_total_pages(
                total_entries=page_result.total_entries,
                page_size=page_result.page_size,
            )
        entries = tuple(
            self._to_entry_view(board_type=resolved_board_type, entry=dto)
            for dto in page_result.entries
        )
        self_summary = self._build_self_summary(
            overview=overview,
            board_type=resolved_board_type,
            character_id=character_id,
        )
        return LeaderboardPanelSnapshot(
            overview=overview,
            board_type=resolved_board_type.value,
            board_name=_BOARD_DISPLAY_NAME_BY_TYPE[resolved_board_type],
            status=page_result.status,
            stale=page_result.stale,
            page=page_result.page,
            page_size=page_result.page_size,
            total_entries=page_result.total_entries,
            total_pages=total_pages,
            has_previous_page=page_result.page > 1,
            has_next_page=page_result.page < total_pages and page_result.total_entries > 0,
            snapshot_generated_at=page_result.snapshot_generated_at,
            entries=entries,
            self_summary=self_summary,
        )

    def get_share_snapshot(
        self,
        *,
        character_id: int,
        board_type: LeaderboardBoardType | str,
        top_limit: int = _DEFAULT_SHARE_TOP_LIMIT,
    ) -> LeaderboardPanelSnapshot:
        """读取公开分享所需的前列榜单摘要。"""
        return self.get_panel_snapshot(
            character_id=character_id,
            board_type=board_type,
            page=1,
            page_size=max(1, top_limit),
        )

    @staticmethod
    def _resolve_total_pages(*, total_entries: int, page_size: int) -> int:
        if total_entries <= 0:
            return 1
        return max(1, (total_entries + max(1, page_size) - 1) // max(1, page_size))

    @staticmethod
    def _normalize_board_type(board_type: LeaderboardBoardType | str) -> LeaderboardBoardType:
        if isinstance(board_type, LeaderboardBoardType):
            return board_type
        try:
            return LeaderboardBoardType(board_type)
        except ValueError as exc:
            raise LeaderboardPanelServiceError(f"未支持的榜单类型：{board_type}") from exc

    def _to_entry_view(
        self,
        *,
        board_type: LeaderboardBoardType,
        entry: LeaderboardEntryDTO,
    ) -> LeaderboardPanelEntryView:
        summary = self._normalize_summary(entry.summary)
        title_name, badge_name = self._resolve_visible_identity(
            board_type=board_type,
            character_id=entry.character_id,
            rank_position=entry.rank,
            summary=summary,
        )
        return LeaderboardPanelEntryView(
            rank=entry.rank,
            character_id=entry.character_id,
            character_name=self._resolve_character_name(entry=entry, summary=summary),
            display_score=entry.display_score,
            summary=summary,
            title_name=title_name,
            badge_name=badge_name,
        )

    def _build_self_summary(
        self,
        *,
        overview: CharacterPanelOverview,
        board_type: LeaderboardBoardType,
        character_id: int,
    ) -> LeaderboardPanelSelfSummary:
        entry_model = self._snapshot_repository.get_latest_leaderboard_entry(board_type.value, character_id)
        if entry_model is None:
            return LeaderboardPanelSelfSummary(
                character_id=overview.character_id,
                character_name=overview.character_name,
                rank=None,
                display_score=None,
                summary={
                    "realm_name": overview.realm_name,
                    "stage_name": overview.stage_name,
                    "main_path_name": overview.main_path_name,
                    "main_skill_name": overview.main_skill.skill_name,
                    "main_skill": {
                        "skill_name": overview.main_skill.skill_name,
                        "rank_name": overview.main_skill.rank_name,
                        "quality_name": overview.main_skill.quality_name,
                        "path_name": overview.main_skill.path_name,
                    },
                    "public_power_score": overview.public_power_score,
                },
                title_name=overview.character_title,
                badge_name=overview.badge_name,
            )
        summary = self._normalize_summary(entry_model.summary_json)
        title_name, badge_name = self._resolve_visible_identity(
            board_type=board_type,
            character_id=character_id,
            rank_position=entry_model.rank_position,
            summary=summary,
        )
        return LeaderboardPanelSelfSummary(
            character_id=overview.character_id,
            character_name=overview.character_name,
            rank=entry_model.rank_position,
            display_score=_read_optional_str(summary.get("display_score")) or str(entry_model.score),
            summary=summary,
            title_name=title_name or overview.character_title,
            badge_name=badge_name or overview.badge_name,
        )

    def _resolve_visible_identity(
        self,
        *,
        board_type: LeaderboardBoardType,
        character_id: int,
        rank_position: int,
        summary: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        title_name = _read_optional_str(summary.get("character_title"))
        badge_name = None
        pvp_rank_position: int | None = None
        if board_type is LeaderboardBoardType.PVP_CHALLENGE:
            pvp_rank_position = rank_position
        else:
            pvp_entry = self._snapshot_repository.get_latest_leaderboard_entry(
                LeaderboardBoardType.PVP_CHALLENGE.value,
                character_id,
            )
            if pvp_entry is not None:
                pvp_rank_position = pvp_entry.rank_position
                pvp_summary = self._normalize_summary(pvp_entry.summary_json)
                if title_name is None:
                    title_name = _read_optional_str(pvp_summary.get("character_title"))
        if pvp_rank_position is None:
            return title_name, badge_name
        reward_preview = self._pvp_rule_service.build_reward_preview(
            rank_position=pvp_rank_position,
            honor_coin_on_win=0,
            honor_coin_on_loss=0,
        )
        for reward_item in reward_preview.display_items:
            if reward_item.reward_type is PvpRewardDisplayType.TITLE:
                title_name = reward_item.name
            elif reward_item.reward_type is PvpRewardDisplayType.BADGE:
                badge_name = reward_item.name
        return title_name, badge_name

    @staticmethod
    def _normalize_summary(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}
        return {}

    @staticmethod
    def _resolve_character_name(*, entry: LeaderboardEntryDTO, summary: dict[str, Any]) -> str:
        return _read_optional_str(summary.get("character_name")) or f"角色{entry.character_id}"



def _read_optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "LeaderboardPanelEntryView",
    "LeaderboardPanelSelfSummary",
    "LeaderboardPanelService",
    "LeaderboardPanelServiceError",
    "LeaderboardPanelSnapshot",
]
