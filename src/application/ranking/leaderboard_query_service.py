"""阶段 8 榜单查询应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from domain.ranking import LeaderboardBoardType
from infrastructure.db.models import LeaderboardEntrySnapshot, LeaderboardSnapshot
from infrastructure.db.repositories import SnapshotRepository

_DEFAULT_PAGE_SIZE = 20
_DEFAULT_STALE_AFTER_SECONDS = 180
_PREPARING_STATUS = "preparing"
_READY_STATUS = "ready"


@dataclass(frozen=True, slots=True)
class LeaderboardEntryDTO:
    """榜单单条展示 DTO。"""

    rank: int
    character_id: int
    score: int
    display_score: str
    summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LeaderboardPageDTO:
    """榜单分页结果 DTO。"""

    board_type: str
    snapshot_generated_at: datetime | None
    stale: bool
    status: str
    page: int
    page_size: int
    total_entries: int
    entries: tuple[LeaderboardEntryDTO, ...]


class LeaderboardQueryService:
    """读取最新榜单快照并投影为查询 DTO。"""

    def __init__(
        self,
        *,
        snapshot_repository: SnapshotRepository,
        refresh_coordinator: LeaderboardRefreshRequestPort | None = None,
        stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        self._snapshot_repository = snapshot_repository
        self._refresh_coordinator = refresh_coordinator
        self._stale_after_seconds = max(30, stale_after_seconds)

    def query_leaderboard(
        self,
        *,
        board_type: LeaderboardBoardType | str,
        page: int = 1,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> LeaderboardPageDTO:
        resolved_board_type = self._normalize_board_type(board_type)
        resolved_page = max(1, page)
        resolved_page_size = max(1, page_size)
        snapshot = self._snapshot_repository.get_latest_leaderboard(resolved_board_type.value)
        if snapshot is None:
            self._request_refresh(board_type=resolved_board_type)
            return LeaderboardPageDTO(
                board_type=resolved_board_type.value,
                snapshot_generated_at=None,
                stale=True,
                status=_PREPARING_STATUS,
                page=resolved_page,
                page_size=resolved_page_size,
                total_entries=0,
                entries=(),
            )

        entry_count = self._resolve_entry_count(snapshot)
        offset = (resolved_page - 1) * resolved_page_size
        entries = self._snapshot_repository.list_leaderboard_entries(
            snapshot.id,
            limit=resolved_page_size,
            offset=offset,
        )
        stale = self._is_stale(board_type=resolved_board_type, snapshot=snapshot)
        if stale:
            self._request_refresh(board_type=resolved_board_type)
        return LeaderboardPageDTO(
            board_type=resolved_board_type.value,
            snapshot_generated_at=snapshot.generated_at,
            stale=stale,
            status=_READY_STATUS,
            page=resolved_page,
            page_size=resolved_page_size,
            total_entries=entry_count,
            entries=tuple(self._to_entry_dto(board_type=resolved_board_type, entry=entry) for entry in entries),
        )

    @staticmethod
    def _normalize_board_type(board_type: LeaderboardBoardType | str) -> LeaderboardBoardType:
        if isinstance(board_type, LeaderboardBoardType):
            return board_type
        return LeaderboardBoardType(board_type)

    def _request_refresh(self, *, board_type: LeaderboardBoardType) -> None:
        if self._refresh_coordinator is None:
            return
        self._refresh_coordinator.request_refresh(board_type=board_type)

    def _is_stale(self, *, board_type: LeaderboardBoardType, snapshot: LeaderboardSnapshot) -> bool:
        if board_type is LeaderboardBoardType.PVP_CHALLENGE:
            return False
        scope_json = snapshot.scope_json if isinstance(snapshot.scope_json, dict) else {}
        stale_after_seconds = scope_json.get("stale_after_seconds", self._stale_after_seconds)
        stale_deadline = snapshot.generated_at + timedelta(seconds=max(1, int(stale_after_seconds)))
        current_time = datetime.now(UTC).replace(tzinfo=None)
        return current_time >= stale_deadline

    @staticmethod
    def _resolve_entry_count(snapshot: LeaderboardSnapshot) -> int:
        scope_json = snapshot.scope_json if isinstance(snapshot.scope_json, dict) else {}
        entry_count = scope_json.get("entry_count")
        if isinstance(entry_count, int) and entry_count >= 0:
            return entry_count
        return len(snapshot.entries)

    @staticmethod
    def _to_entry_dto(*, board_type: LeaderboardBoardType, entry: LeaderboardEntrySnapshot) -> LeaderboardEntryDTO:
        summary = dict(entry.summary_json) if isinstance(entry.summary_json, dict) else {}
        display_score = str(summary.get("display_score", entry.score))
        if board_type is LeaderboardBoardType.PVP_CHALLENGE:
            summary.pop("hidden_pvp_score", None)
            summary.setdefault("rank_position", entry.rank_position)
            if "best_rank" not in summary:
                summary["best_rank"] = entry.rank_position
            summary.setdefault("latest_defense_snapshot_version", None)
        return LeaderboardEntryDTO(
            rank=entry.rank_position,
            character_id=entry.character_id,
            score=entry.score,
            display_score=display_score,
            summary=summary,
        )


class LeaderboardRefreshRequestPort:
    """榜单后台刷新请求端口。"""

    def request_refresh(self, *, board_type: LeaderboardBoardType) -> None:
        """发起后台刷新请求。"""
        raise NotImplementedError


__all__ = [
    "LeaderboardEntryDTO",
    "LeaderboardPageDTO",
    "LeaderboardQueryService",
    "LeaderboardRefreshRequestPort",
]
