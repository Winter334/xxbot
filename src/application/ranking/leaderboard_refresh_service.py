"""阶段 8 榜单刷新与后台任务编排。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any

from application.ranking.leaderboard_query_service import LeaderboardRefreshRequestPort
from domain.dungeon import EndlessDungeonProgression
from domain.ranking import CharacterScoreRuleService, LeaderboardBoardType
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import CharacterScoreSnapshot, LeaderboardEntrySnapshot, LeaderboardSnapshot
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, SnapshotRepository
from infrastructure.db.session import session_scope

logger = logging.getLogger(__name__)

_DEFAULT_STALE_AFTER_SECONDS = 180
_DEFAULT_REFRESH_INTERVAL_SECONDS = 120
_DEFAULT_PVP_SEGMENT = 150
_PERIODIC_REFRESH_BOARD_TYPES: tuple[LeaderboardBoardType, ...] = (
    LeaderboardBoardType.POWER,
    LeaderboardBoardType.ENDLESS_DEPTH,
)
_PVP_TIER_NAMES: tuple[str, ...] = (
    "试锋",
    "争鸣",
    "凌云",
    "破军",
    "天极",
    "问鼎",
)
_REALM_NAME_BY_ID: dict[str, str] = {
    "mortal": "凡人",
    "qi_refining": "炼气",
    "foundation": "筑基",
    "core": "结丹",
    "nascent_soul": "元婴",
    "deity_transformation": "化神",
    "void_refinement": "炼虚",
    "body_integration": "合体",
    "great_vehicle": "大乘",
    "tribulation": "渡劫",
}
_STAGE_NAME_BY_ID: dict[str, str] = {
    "early": "初期",
    "middle": "中期",
    "late": "后期",
    "perfect": "圆满",
}


@dataclass(frozen=True, slots=True)
class LeaderboardRefreshResult:
    """单次榜单刷新结果。"""

    board_type: str
    generated_at: datetime
    entry_count: int
    score_version: str


class LeaderboardRefreshService:
    """基于角色评分缓存与快照生成榜单快照。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        snapshot_repository: SnapshotRepository,
        static_config: StaticGameConfig | None = None,
        stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        self._character_repository = character_repository
        self._snapshot_repository = snapshot_repository
        self._static_config = static_config or get_static_config()
        self._stale_after_seconds = max(30, stale_after_seconds)
        self._score_rule_service = CharacterScoreRuleService(self._static_config)
        self._endless_progression = EndlessDungeonProgression(self._static_config)

    def refresh_board(self, *, board_type: LeaderboardBoardType | str) -> LeaderboardRefreshResult:
        """刷新指定榜单。"""
        resolved_board_type = self._normalize_board_type(board_type)
        if resolved_board_type is LeaderboardBoardType.PVP_CHALLENGE:
            return self.seed_pvp_board_if_missing()
        return self._refresh_scored_board(board_type=resolved_board_type)

    def seed_pvp_board_if_missing(self) -> LeaderboardRefreshResult:
        """仅在 PVP 正式榜缺失时生成首份种子榜。"""
        latest_snapshot = self._snapshot_repository.get_latest_leaderboard(LeaderboardBoardType.PVP_CHALLENGE.value)
        if latest_snapshot is not None:
            return self._build_result_from_existing_snapshot(
                board_type=LeaderboardBoardType.PVP_CHALLENGE,
                snapshot=latest_snapshot,
            )
        return self.repair_pvp_seed_board()

    def repair_pvp_seed_board(self) -> LeaderboardRefreshResult:
        """按隐藏评分重建 PVP 种子榜，仅供冷启动或显式修复入口调用。"""
        generated_at = datetime.now(UTC).replace(tzinfo=None)
        aggregates = tuple(self._character_repository.list_aggregates_for_ranking())
        entries = self._build_entries(
            board_type=LeaderboardBoardType.PVP_CHALLENGE,
            aggregates=aggregates,
        )
        score_version = self._resolve_score_version(aggregates)
        snapshot = LeaderboardSnapshot(
            board_type=LeaderboardBoardType.PVP_CHALLENGE.value,
            generated_at=generated_at,
            scope_json={
                "schema_version": "stage9.pvp.seed.v1",
                "score_version": score_version,
                "entry_count": len(entries),
                "generated_by": "leaderboard_refresh_service.seed_pvp_board",
                "board_type": LeaderboardBoardType.PVP_CHALLENGE.value,
                "auto_refresh_enabled": False,
            },
            entries=entries,
        )
        self._snapshot_repository.replace_leaderboard_snapshot(snapshot)
        return LeaderboardRefreshResult(
            board_type=LeaderboardBoardType.PVP_CHALLENGE.value,
            generated_at=generated_at,
            entry_count=len(entries),
            score_version=score_version,
        )

    def refresh_launch_boards(self) -> tuple[LeaderboardRefreshResult, ...]:
        """刷新首发榜单，并在缺失时补齐 PVP 种子榜。"""
        return (
            self.refresh_board(board_type=LeaderboardBoardType.POWER),
            self.refresh_board(board_type=LeaderboardBoardType.PVP_CHALLENGE),
            self.refresh_board(board_type=LeaderboardBoardType.ENDLESS_DEPTH),
        )

    @staticmethod
    def _normalize_board_type(board_type: LeaderboardBoardType | str) -> LeaderboardBoardType:
        if isinstance(board_type, LeaderboardBoardType):
            return board_type
        return LeaderboardBoardType(board_type)

    def _refresh_scored_board(self, *, board_type: LeaderboardBoardType) -> LeaderboardRefreshResult:
        generated_at = datetime.now(UTC).replace(tzinfo=None)
        aggregates = tuple(self._character_repository.list_aggregates_for_ranking())
        entries = self._build_entries(board_type=board_type, aggregates=aggregates)
        score_version = self._resolve_score_version(aggregates)
        snapshot = LeaderboardSnapshot(
            board_type=board_type.value,
            generated_at=generated_at,
            scope_json={
                "schema_version": "stage8.launch.v1",
                "score_version": score_version,
                "entry_count": len(entries),
                "generated_by": "leaderboard_refresh_service",
                "stale_after_seconds": self._stale_after_seconds,
                "board_type": board_type.value,
            },
            entries=entries,
        )
        self._snapshot_repository.replace_leaderboard_snapshot(snapshot)
        return LeaderboardRefreshResult(
            board_type=board_type.value,
            generated_at=generated_at,
            entry_count=len(entries),
            score_version=score_version,
        )

    def _build_result_from_existing_snapshot(
        self,
        *,
        board_type: LeaderboardBoardType,
        snapshot: LeaderboardSnapshot,
    ) -> LeaderboardRefreshResult:
        scope_json = snapshot.scope_json if isinstance(snapshot.scope_json, dict) else {}
        score_version = scope_json.get("score_version")
        if not isinstance(score_version, str) or not score_version:
            score_version = self._score_rule_service.score_version
        entry_count = scope_json.get("entry_count")
        if not isinstance(entry_count, int) or entry_count < 0:
            entry_count = len(snapshot.entries)
        return LeaderboardRefreshResult(
            board_type=board_type.value,
            generated_at=snapshot.generated_at,
            entry_count=entry_count,
            score_version=score_version,
        )

    def _resolve_score_version(self, aggregates: tuple[CharacterAggregate, ...]) -> str:
        for aggregate in aggregates:
            snapshot = aggregate.score_snapshot
            if snapshot is not None and snapshot.score_version:
                return snapshot.score_version
        return self._score_rule_service.score_version

    def _build_entries(
        self,
        *,
        board_type: LeaderboardBoardType,
        aggregates: tuple[CharacterAggregate, ...],
    ) -> list[LeaderboardEntrySnapshot]:
        ranked_aggregates = [
            aggregate
            for aggregate in aggregates
            if aggregate.progress is not None and aggregate.score_snapshot is not None
        ]
        if board_type is LeaderboardBoardType.POWER:
            ranked_aggregates.sort(
                key=lambda aggregate: (
                    -aggregate.character.total_power_score,
                    -aggregate.progress.highest_endless_floor,
                    aggregate.character.id,
                )
            )
        elif board_type is LeaderboardBoardType.PVP_CHALLENGE:
            ranked_aggregates.sort(
                key=lambda aggregate: (
                    -aggregate.character.hidden_pvp_score,
                    -aggregate.character.public_power_score,
                    aggregate.character.id,
                )
            )
        else:
            ranked_aggregates.sort(
                key=lambda aggregate: (
                    -aggregate.progress.highest_endless_floor,
                    -aggregate.character.public_power_score,
                    aggregate.character.id,
                )
            )

        entries: list[LeaderboardEntrySnapshot] = []
        for index, aggregate in enumerate(ranked_aggregates, start=1):
            score_snapshot = aggregate.score_snapshot
            progress = aggregate.progress
            assert score_snapshot is not None
            assert progress is not None
            score_value, summary = self._build_entry_payload(
                board_type=board_type,
                aggregate=aggregate,
                score_snapshot=score_snapshot,
            )
            entries.append(
                LeaderboardEntrySnapshot(
                    character_id=aggregate.character.id,
                    rank_position=index,
                    score=score_value,
                    summary_json=summary,
                )
            )
        return entries

    def _build_entry_payload(
        self,
        *,
        board_type: LeaderboardBoardType,
        aggregate: CharacterAggregate,
        score_snapshot: CharacterScoreSnapshot,
    ) -> tuple[int, dict[str, Any]]:
        progress = aggregate.progress
        assert progress is not None
        breakdown = score_snapshot.breakdown_json if isinstance(score_snapshot.breakdown_json, dict) else {}
        skill_breakdown = breakdown.get("skill") if isinstance(breakdown.get("skill"), dict) else {}
        main_skill = self._normalize_mapping(skill_breakdown.get("main_skill"))
        guard_skill = self._normalize_mapping(skill_breakdown.get("guard_skill"))
        movement_skill = self._normalize_mapping(skill_breakdown.get("movement_skill"))
        spirit_skill = self._normalize_mapping(skill_breakdown.get("spirit_skill"))
        auxiliary_skills = tuple(
            skill
            for skill in (guard_skill, movement_skill, spirit_skill)
            if skill
        )
        summary = {
            "character_name": aggregate.character.name,
            "character_title": aggregate.character.title,
            "realm_id": progress.realm_id,
            "realm_name": _REALM_NAME_BY_ID.get(progress.realm_id, progress.realm_id),
            "stage_id": progress.stage_id,
            "stage_name": _STAGE_NAME_BY_ID.get(progress.stage_id, progress.stage_id),
            "main_path_id": skill_breakdown.get("main_path_id", None),
            "main_path_name": skill_breakdown.get("main_path_name", None),
            "main_skill_name": skill_breakdown.get("main_skill_name", skill_breakdown.get("main_path_name", None)),
            "main_skill": main_skill,
            "auxiliary_skills": list(auxiliary_skills),
            "public_power_score": score_snapshot.public_power_score,
            "highest_endless_floor": progress.highest_endless_floor,
            "score_version": score_snapshot.score_version,
        }
        if board_type is LeaderboardBoardType.POWER:
            summary.update(
                {
                    "growth_score": score_snapshot.growth_score,
                    "equipment_score": score_snapshot.equipment_score,
                    "skill_score": score_snapshot.skill_score,
                    "artifact_score": score_snapshot.artifact_score,
                    "display_score": str(score_snapshot.public_power_score),
                }
            )
            return score_snapshot.public_power_score, summary
        if board_type is LeaderboardBoardType.PVP_CHALLENGE:
            challenge_tier = self._resolve_pvp_tier(score_snapshot.hidden_pvp_score)
            build_summary = self._build_pvp_build_summary(score_snapshot=score_snapshot, summary=summary)
            summary.update(
                {
                    "challenge_tier": challenge_tier,
                    "display_score": f"{challenge_tier}·{score_snapshot.public_power_score}",
                    "build_summary": build_summary,
                    "hidden_score_exposed": False,
                    "latest_defense_snapshot_version": None,
                    "best_rank": summary.get("best_rank", None),
                    "protected_until": summary.get("protected_until", None),
                    "reward_preview_tier": challenge_tier,
                }
            )
            return score_snapshot.hidden_pvp_score, summary
        region_snapshot = self._endless_progression.resolve_region(max(1, progress.highest_endless_floor or 1))
        summary.update(
            {
                "highest_region_id": region_snapshot.region_id,
                "highest_region_name": region_snapshot.region_name,
                "display_score": f"第 {progress.highest_endless_floor} 层",
            }
        )
        return progress.highest_endless_floor, summary

    @staticmethod
    def _build_pvp_build_summary(*, score_snapshot: CharacterScoreSnapshot, summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "main_skill_name": summary.get("main_skill_name"),
            "main_skill": summary.get("main_skill"),
            "auxiliary_skills": summary.get("auxiliary_skills"),
            "public_power_score": score_snapshot.public_power_score,
            "pvp_adjustment_score": score_snapshot.pvp_adjustment_score,
            "highest_endless_floor": summary.get("highest_endless_floor"),
        }

    @staticmethod
    def _normalize_mapping(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {str(key): item for key, item in value.items()}

    @staticmethod
    def _resolve_pvp_tier(hidden_pvp_score: int) -> str:
        tier_index = min(len(_PVP_TIER_NAMES) - 1, max(0, hidden_pvp_score // _DEFAULT_PVP_SEGMENT))
        return _PVP_TIER_NAMES[tier_index]


class AsyncLeaderboardRefreshCoordinator(LeaderboardRefreshRequestPort):
    """进程内异步榜单刷新协调器。"""

    def __init__(
        self,
        *,
        session_factory,
        static_config: StaticGameConfig | None = None,
        refresh_interval_seconds: int = _DEFAULT_REFRESH_INTERVAL_SECONDS,
        stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._static_config = static_config or get_static_config()
        self._refresh_interval_seconds = max(30, refresh_interval_seconds)
        self._stale_after_seconds = max(30, stale_after_seconds)
        self._loop = loop
        self._refresh_all_lock = asyncio.Lock()
        self._refresh_lock_by_board = {
            board_type: asyncio.Lock()
            for board_type in LeaderboardBoardType.launch_board_types()
        }
        self._worker_task: asyncio.Task[None] | None = None
        self._stopped = False

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定事件循环。"""
        self._loop = loop

    def start(self) -> None:
        """启动后台刷新循环。"""
        if self._worker_task is not None:
            return
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        self._worker_task = self._loop.create_task(self._run_periodic_refresh(), name="leaderboard-refresh-worker")
        self.request_refresh_all()

    async def shutdown(self) -> None:
        """停止后台刷新循环。"""
        self._stopped = True
        worker_task = self._worker_task
        if worker_task is None:
            return
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        finally:
            self._worker_task = None

    def request_refresh(self, *, board_type: LeaderboardBoardType) -> None:
        """请求刷新单张榜单。"""
        if self._loop is None:
            return
        self._loop.create_task(self._refresh_board_if_idle(board_type=board_type))

    def request_refresh_all(self) -> None:
        """请求刷新可自动刷新的首发榜单。"""
        if self._loop is None:
            return
        self._loop.create_task(self._refresh_all_if_idle())

    async def _run_periodic_refresh(self) -> None:
        while not self._stopped:
            await self._refresh_all_if_idle()
            await asyncio.sleep(self._refresh_interval_seconds)

    async def _refresh_all_if_idle(self) -> None:
        if self._refresh_all_lock.locked():
            return
        async with self._refresh_all_lock:
            for board_type in _PERIODIC_REFRESH_BOARD_TYPES:
                await self._refresh_board_under_lock(board_type=board_type)

    async def _refresh_board_if_idle(self, *, board_type: LeaderboardBoardType) -> None:
        refresh_lock = self._refresh_lock_by_board[board_type]
        if refresh_lock.locked():
            return
        await self._refresh_board_under_lock(board_type=board_type)

    async def _refresh_board_under_lock(self, *, board_type: LeaderboardBoardType) -> None:
        refresh_lock = self._refresh_lock_by_board[board_type]
        async with refresh_lock:
            with session_scope(self._session_factory) as session:
                character_repository = CharacterRepository.__subclasses__()[0](session)
                snapshot_repository = SnapshotRepository.__subclasses__()[0](session)
                refresh_service = LeaderboardRefreshService(
                    character_repository=character_repository,
                    snapshot_repository=snapshot_repository,
                    static_config=self._static_config,
                    stale_after_seconds=self._stale_after_seconds,
                )
                try:
                    refresh_service.refresh_board(board_type=board_type)
                except Exception:
                    logger.exception("榜单后台刷新失败", extra={"board_type": board_type.value})


__all__ = [
    "AsyncLeaderboardRefreshCoordinator",
    "LeaderboardRefreshResult",
    "LeaderboardRefreshService",
]
