"""恢复状态面板查询与恢复动作适配服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from infrastructure.db.models import HealingState
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, StateRepository

_COMPLETED_HEALING_STATUS = "completed"
_RUNNING_HEALING_STATUS = "running"
_DECIMAL_FULL_RATIO = Decimal("1.0000")
_HP_HEAVY_THRESHOLD = Decimal("0.2500")
_HP_MEDIUM_THRESHOLD = Decimal("0.5000")
_HP_LIGHT_THRESHOLD = Decimal("0.8000")
_MP_DRY_THRESHOLD = Decimal("0.1000")
_MP_LIGHT_THRESHOLD = Decimal("0.3500")
_RECOVERY_DURATION_BY_INJURY = {
    "none": timedelta(minutes=5),
    "light": timedelta(minutes=10),
    "medium": timedelta(minutes=15),
    "heavy": timedelta(minutes=20),
    "defeated": timedelta(minutes=20),
}


@dataclass(frozen=True, slots=True)
class HealingPanelSnapshot:
    """恢复面板所需的聚合快照。"""

    character_id: int
    current_hp_ratio: Decimal
    current_mp_ratio: Decimal
    inferred_injury_level: str
    healing_status: str
    healing_injury_level: str | None
    started_at: datetime | None
    scheduled_end_at: datetime | None
    settled_at: datetime | None
    can_start_recovery: bool
    can_complete_recovery: bool
    retreat_running: bool
    endless_running: bool


@dataclass(frozen=True, slots=True)
class RecoveryActionResult:
    """单次恢复动作结果。"""

    action_type: str
    snapshot: HealingPanelSnapshot


class HealingPanelServiceError(RuntimeError):
    """恢复面板服务基础异常。"""


class RecoveryActionBlockedError(HealingPanelServiceError):
    """当前状态不允许开始恢复。"""


class RecoveryActionUnavailableError(HealingPanelServiceError):
    """当前不存在可执行的恢复动作。"""


class HealingPanelStateError(HealingPanelServiceError):
    """恢复面板依赖的角色状态不完整。"""


class HealingPanelService:
    """读取恢复状态，并执行开始或完成恢复动作。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        state_repository: StateRepository,
    ) -> None:
        self._character_repository = character_repository
        self._state_repository = state_repository

    def get_panel_snapshot(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> HealingPanelSnapshot:
        """读取恢复状态面板快照。"""
        aggregate = self._require_aggregate(character_id)
        progress = self._require_progress(aggregate)
        current_time = now or datetime.utcnow()
        healing_state = self._state_repository.get_healing_state(character_id)
        retreat_state = self._state_repository.get_retreat_state(character_id)
        retreat_running = retreat_state is not None and retreat_state.status == _RUNNING_HEALING_STATUS and retreat_state.settled_at is None
        endless_running = self._state_repository.has_running_endless_run(character_id)
        can_complete_recovery = self._can_complete_healing_state(healing_state=healing_state, now=current_time)
        healing_status = self._resolve_healing_status(healing_state=healing_state)
        recovery_needed = progress.current_hp_ratio < _DECIMAL_FULL_RATIO or progress.current_mp_ratio < _DECIMAL_FULL_RATIO
        has_running_healing = healing_status == _RUNNING_HEALING_STATUS and not can_complete_recovery
        can_start_recovery = recovery_needed and not has_running_healing and not can_complete_recovery
        if can_start_recovery and (retreat_running or endless_running):
            can_start_recovery = False
        return HealingPanelSnapshot(
            character_id=character_id,
            current_hp_ratio=progress.current_hp_ratio,
            current_mp_ratio=progress.current_mp_ratio,
            inferred_injury_level=self._classify_injury_level(
                current_hp_ratio=progress.current_hp_ratio,
                current_mp_ratio=progress.current_mp_ratio,
            ),
            healing_status=healing_status,
            healing_injury_level=None if healing_state is None else healing_state.injury_level,
            started_at=None if healing_state is None else healing_state.started_at,
            scheduled_end_at=None if healing_state is None else healing_state.scheduled_end_at,
            settled_at=None if healing_state is None else healing_state.settled_at,
            can_start_recovery=can_start_recovery,
            can_complete_recovery=can_complete_recovery,
            retreat_running=retreat_running,
            endless_running=endless_running,
        )

    def execute_recovery_action(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> RecoveryActionResult:
        """执行单次恢复动作：未开始时启动恢复，已完成时结算恢复。"""
        current_time = now or datetime.utcnow()
        snapshot = self.get_panel_snapshot(character_id=character_id, now=current_time)
        if snapshot.can_complete_recovery:
            self._complete_recovery(character_id=character_id, now=current_time)
            return RecoveryActionResult(
                action_type="complete",
                snapshot=self.get_panel_snapshot(character_id=character_id, now=current_time),
            )
        if snapshot.can_start_recovery:
            self._start_recovery(character_id=character_id, snapshot=snapshot, now=current_time)
            return RecoveryActionResult(
                action_type="start",
                snapshot=self.get_panel_snapshot(character_id=character_id, now=current_time),
            )
        if snapshot.healing_status == _RUNNING_HEALING_STATUS:
            raise RecoveryActionBlockedError(f"疗伤尚未完成：{character_id}")
        if snapshot.retreat_running:
            raise RecoveryActionBlockedError(f"角色闭关中，无法开始恢复：{character_id}")
        if snapshot.endless_running:
            raise RecoveryActionBlockedError(f"角色正在无尽副本中，无法开始恢复：{character_id}")
        raise RecoveryActionUnavailableError(f"当前无需恢复：{character_id}")

    def _start_recovery(
        self,
        *,
        character_id: int,
        snapshot: HealingPanelSnapshot,
        now: datetime,
    ) -> None:
        duration = _RECOVERY_DURATION_BY_INJURY[snapshot.inferred_injury_level]
        healing_state = self._state_repository.get_healing_state(character_id)
        if healing_state is None:
            healing_state = HealingState(
                character_id=character_id,
                status=_RUNNING_HEALING_STATUS,
                injury_level=snapshot.inferred_injury_level,
                started_at=now,
                scheduled_end_at=now + duration,
                settled_at=None,
                context_json={
                    "start_hp_ratio": format(snapshot.current_hp_ratio, ".4f"),
                    "start_mp_ratio": format(snapshot.current_mp_ratio, ".4f"),
                },
            )
        else:
            healing_state.status = _RUNNING_HEALING_STATUS
            healing_state.injury_level = snapshot.inferred_injury_level
            healing_state.started_at = now
            healing_state.scheduled_end_at = now + duration
            healing_state.settled_at = None
            healing_state.context_json = {
                "start_hp_ratio": format(snapshot.current_hp_ratio, ".4f"),
                "start_mp_ratio": format(snapshot.current_mp_ratio, ".4f"),
            }
        self._state_repository.save_healing_state(healing_state)

    def _complete_recovery(self, *, character_id: int, now: datetime) -> None:
        aggregate = self._require_aggregate(character_id)
        progress = self._require_progress(aggregate)
        healing_state = self._require_healing_state(character_id)
        progress.current_hp_ratio = _DECIMAL_FULL_RATIO
        progress.current_mp_ratio = _DECIMAL_FULL_RATIO
        self._character_repository.save_progress(progress)
        healing_state.status = _COMPLETED_HEALING_STATUS
        healing_state.injury_level = "none"
        healing_state.settled_at = now if healing_state.scheduled_end_at is None else min(now, healing_state.scheduled_end_at)
        self._state_repository.save_healing_state(healing_state)

    @staticmethod
    def _resolve_healing_status(*, healing_state: HealingState | None) -> str:
        if healing_state is None:
            return "none"
        return healing_state.status

    @staticmethod
    def _can_complete_healing_state(*, healing_state: HealingState | None, now: datetime) -> bool:
        if healing_state is None:
            return False
        if healing_state.status != _RUNNING_HEALING_STATUS:
            return False
        if healing_state.scheduled_end_at is None:
            return False
        return now >= healing_state.scheduled_end_at

    @staticmethod
    def _classify_injury_level(*, current_hp_ratio: Decimal, current_mp_ratio: Decimal) -> str:
        if current_hp_ratio <= Decimal("0"):
            return "defeated"
        if current_hp_ratio < _HP_HEAVY_THRESHOLD:
            return "heavy"
        if current_hp_ratio < _HP_MEDIUM_THRESHOLD or current_mp_ratio < _MP_DRY_THRESHOLD:
            return "medium"
        if current_hp_ratio < _HP_LIGHT_THRESHOLD or current_mp_ratio < _MP_LIGHT_THRESHOLD:
            return "light"
        return "none"

    def _require_healing_state(self, character_id: int) -> HealingState:
        healing_state = self._state_repository.get_healing_state(character_id)
        if healing_state is None:
            raise RecoveryActionUnavailableError(f"角色不存在疗伤记录：{character_id}")
        return healing_state

    def _require_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise HealingPanelStateError(f"角色不存在：{character_id}")
        return aggregate

    @staticmethod
    def _require_progress(aggregate: CharacterAggregate):
        if aggregate.progress is None:
            raise HealingPanelStateError(f"角色缺少成长状态：{aggregate.character.id}")
        return aggregate.progress


__all__ = [
    "HealingPanelService",
    "HealingPanelServiceError",
    "HealingPanelSnapshot",
    "HealingPanelStateError",
    "RecoveryActionBlockedError",
    "RecoveryActionResult",
    "RecoveryActionUnavailableError",
]
