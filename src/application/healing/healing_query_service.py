"""恢复状态面板查询与恢复动作适配服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import HealingState
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, StateRepository

_COMPLETED_HEALING_STATUS = "completed"
_RUNNING_HEALING_STATUS = "running"
_RUNNING_RETREAT_STATUS = "running"
_DECIMAL_FULL_RATIO = Decimal("1.0000")
_DECIMAL_ZERO = Decimal("0.0000")
_DECIMAL_RATIO_QUANTIZER = Decimal("0.0001")
_HP_HEAVY_THRESHOLD = Decimal("0.2500")
_HP_MEDIUM_THRESHOLD = Decimal("0.5000")
_HP_LIGHT_THRESHOLD = Decimal("0.8000")
_MP_DRY_THRESHOLD = Decimal("0.1000")
_MP_LIGHT_THRESHOLD = Decimal("0.3500")


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
    can_interrupt_recovery: bool
    retreat_running: bool
    endless_running: bool
    recovery_full_seconds: int
    elapsed_recovery_seconds: int
    remaining_recovery_seconds: int
    recovery_progress: Decimal
    recovery_progress_percent: Decimal
    start_hp_ratio: Decimal | None
    start_mp_ratio: Decimal | None
    expected_hp_ratio: Decimal
    expected_mp_ratio: Decimal
    status_hint: str


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
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._state_repository = state_repository
        self._static_config = static_config or get_static_config()

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
        retreat_running = retreat_state is not None and retreat_state.status == _RUNNING_RETREAT_STATUS and retreat_state.settled_at is None
        endless_running = self._state_repository.has_running_endless_run(character_id)
        recovery_full_seconds = int(self._recovery_full_duration().total_seconds())
        healing_status = self._resolve_healing_status(healing_state=healing_state)
        start_hp_ratio, start_mp_ratio = self._extract_start_ratios(
            healing_state=healing_state,
            current_hp_ratio=progress.current_hp_ratio,
            current_mp_ratio=progress.current_mp_ratio,
        )
        elapsed_recovery_seconds = self._resolve_elapsed_recovery_seconds(healing_state=healing_state, now=current_time)
        recovery_progress = self._build_recovery_progress(
            elapsed_seconds=elapsed_recovery_seconds,
            recovery_full_seconds=recovery_full_seconds,
        )
        expected_hp_ratio = self._interpolate_ratio(start_ratio=start_hp_ratio, progress=recovery_progress)
        expected_mp_ratio = self._interpolate_ratio(start_ratio=start_mp_ratio, progress=recovery_progress)
        can_complete_recovery = self._can_complete_healing_state(healing_state=healing_state, now=current_time)
        can_interrupt_recovery = healing_status == _RUNNING_HEALING_STATUS and not can_complete_recovery
        recovery_needed = progress.current_hp_ratio < _DECIMAL_FULL_RATIO or progress.current_mp_ratio < _DECIMAL_FULL_RATIO
        has_running_healing = healing_status == _RUNNING_HEALING_STATUS
        can_start_recovery = recovery_needed and not has_running_healing
        if can_start_recovery and (retreat_running or endless_running):
            can_start_recovery = False
        remaining_recovery_seconds = 0 if can_complete_recovery else max(0, recovery_full_seconds - elapsed_recovery_seconds)
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
            can_interrupt_recovery=can_interrupt_recovery,
            retreat_running=retreat_running,
            endless_running=endless_running,
            recovery_full_seconds=recovery_full_seconds,
            elapsed_recovery_seconds=elapsed_recovery_seconds,
            remaining_recovery_seconds=remaining_recovery_seconds,
            recovery_progress=recovery_progress,
            recovery_progress_percent=(recovery_progress * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_DOWN),
            start_hp_ratio=start_hp_ratio if healing_status == _RUNNING_HEALING_STATUS else None,
            start_mp_ratio=start_mp_ratio if healing_status == _RUNNING_HEALING_STATUS else None,
            expected_hp_ratio=expected_hp_ratio,
            expected_mp_ratio=expected_mp_ratio,
            status_hint=self._build_status_hint(
                healing_status=healing_status,
                can_complete_recovery=can_complete_recovery,
                can_interrupt_recovery=can_interrupt_recovery,
                can_start_recovery=can_start_recovery,
                retreat_running=retreat_running,
                endless_running=endless_running,
                recovery_needed=recovery_needed,
                recovery_progress=recovery_progress,
                remaining_recovery_seconds=remaining_recovery_seconds,
            ),
        )

    def execute_recovery_action(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> RecoveryActionResult:
        """执行单次恢复动作：未开始时启动恢复，恢复中时可中断或完成。"""
        current_time = now or datetime.utcnow()
        snapshot = self.get_panel_snapshot(character_id=character_id, now=current_time)
        if snapshot.can_complete_recovery:
            self._settle_recovery(character_id=character_id, now=current_time, progress=snapshot.recovery_progress)
            return RecoveryActionResult(
                action_type="complete",
                snapshot=self.get_panel_snapshot(character_id=character_id, now=current_time),
            )
        if snapshot.can_interrupt_recovery:
            self._settle_recovery(character_id=character_id, now=current_time, progress=snapshot.recovery_progress)
            return RecoveryActionResult(
                action_type="interrupt",
                snapshot=self.get_panel_snapshot(character_id=character_id, now=current_time),
            )
        if snapshot.can_start_recovery:
            self._start_recovery(character_id=character_id, snapshot=snapshot, now=current_time)
            return RecoveryActionResult(
                action_type="start",
                snapshot=self.get_panel_snapshot(character_id=character_id, now=current_time),
            )
        if snapshot.retreat_running:
            raise RecoveryActionBlockedError(f"角色闭关中，无法开始恢复：{character_id}")
        if snapshot.endless_running:
            raise RecoveryActionBlockedError(f"角色正在无尽副本中，无法开始恢复：{character_id}")
        raise RecoveryActionUnavailableError(f"当前无需恢复：{character_id}")

    def is_recovery_running(self, *, character_id: int) -> bool:
        """判断角色是否仍处于打坐恢复中。"""
        healing_state = self._state_repository.get_healing_state(character_id)
        if healing_state is None:
            return False
        return healing_state.status == _RUNNING_HEALING_STATUS and healing_state.settled_at is None

    def ensure_action_not_blocked_by_recovery(self, *, character_id: int, action_label: str) -> None:
        """在核心动作执行前阻止恢复中的角色继续操作。"""
        if self.is_recovery_running(character_id=character_id):
            raise RecoveryActionBlockedError(
                f"角色正在打坐恢复中，无法进行{action_label}，请先等待完成或主动结束恢复：{character_id}"
            )

    def _start_recovery(
        self,
        *,
        character_id: int,
        snapshot: HealingPanelSnapshot,
        now: datetime,
    ) -> None:
        duration = self._recovery_full_duration()
        healing_state = self._state_repository.get_healing_state(character_id)
        context_json = {
            "start_hp_ratio": format(snapshot.current_hp_ratio, ".4f"),
            "start_mp_ratio": format(snapshot.current_mp_ratio, ".4f"),
        }
        if healing_state is None:
            healing_state = HealingState(
                character_id=character_id,
                status=_RUNNING_HEALING_STATUS,
                injury_level=snapshot.inferred_injury_level,
                started_at=now,
                scheduled_end_at=now + duration,
                settled_at=None,
                context_json=context_json,
            )
        else:
            healing_state.status = _RUNNING_HEALING_STATUS
            healing_state.injury_level = snapshot.inferred_injury_level
            healing_state.started_at = now
            healing_state.scheduled_end_at = now + duration
            healing_state.settled_at = None
            healing_state.context_json = context_json
        self._state_repository.save_healing_state(healing_state)

    def _settle_recovery(self, *, character_id: int, now: datetime, progress: Decimal) -> None:
        aggregate = self._require_aggregate(character_id)
        progress_state = self._require_progress(aggregate)
        healing_state = self._require_healing_state(character_id)
        start_hp_ratio, start_mp_ratio = self._extract_start_ratios(
            healing_state=healing_state,
            current_hp_ratio=progress_state.current_hp_ratio,
            current_mp_ratio=progress_state.current_mp_ratio,
        )
        progress_state.current_hp_ratio = self._interpolate_ratio(start_ratio=start_hp_ratio, progress=progress)
        progress_state.current_mp_ratio = self._interpolate_ratio(start_ratio=start_mp_ratio, progress=progress)
        self._character_repository.save_progress(progress_state)
        healing_state.status = _COMPLETED_HEALING_STATUS
        healing_state.injury_level = "none"
        healing_state.settled_at = now
        self._state_repository.save_healing_state(healing_state)

    def _recovery_full_duration(self) -> timedelta:
        minutes = self._static_config.cultivation_sources.closed_door_yield.recovery_full_minutes
        return timedelta(minutes=minutes)

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
        if healing_state.started_at is None:
            return False
        if healing_state.scheduled_end_at is None:
            return False
        return now >= healing_state.scheduled_end_at

    def _resolve_elapsed_recovery_seconds(self, *, healing_state: HealingState | None, now: datetime) -> int:
        if healing_state is None:
            return 0
        if healing_state.status != _RUNNING_HEALING_STATUS:
            return 0
        if healing_state.started_at is None:
            return 0
        if now <= healing_state.started_at:
            return 0
        recovery_full_seconds = int(self._recovery_full_duration().total_seconds())
        elapsed_seconds = int((now - healing_state.started_at).total_seconds())
        return min(max(0, elapsed_seconds), recovery_full_seconds)

    @staticmethod
    def _build_recovery_progress(*, elapsed_seconds: int, recovery_full_seconds: int) -> Decimal:
        if elapsed_seconds <= 0 or recovery_full_seconds <= 0:
            return _DECIMAL_ZERO
        ratio = (Decimal(elapsed_seconds) / Decimal(recovery_full_seconds)).quantize(
            _DECIMAL_RATIO_QUANTIZER,
            rounding=ROUND_DOWN,
        )
        return min(_DECIMAL_FULL_RATIO, ratio)

    @staticmethod
    def _interpolate_ratio(*, start_ratio: Decimal, progress: Decimal) -> Decimal:
        safe_progress = min(_DECIMAL_FULL_RATIO, max(_DECIMAL_ZERO, progress))
        restored = start_ratio + ((_DECIMAL_FULL_RATIO - start_ratio) * safe_progress)
        return restored.quantize(_DECIMAL_RATIO_QUANTIZER, rounding=ROUND_DOWN)

    def _extract_start_ratios(
        self,
        *,
        healing_state: HealingState | None,
        current_hp_ratio: Decimal,
        current_mp_ratio: Decimal,
    ) -> tuple[Decimal, Decimal]:
        if healing_state is None or not isinstance(healing_state.context_json, dict):
            return current_hp_ratio, current_mp_ratio
        return (
            self._read_ratio(healing_state.context_json.get("start_hp_ratio"), fallback=current_hp_ratio),
            self._read_ratio(healing_state.context_json.get("start_mp_ratio"), fallback=current_mp_ratio),
        )

    @staticmethod
    def _read_ratio(value: object, *, fallback: Decimal) -> Decimal:
        if isinstance(value, Decimal):
            return value.quantize(_DECIMAL_RATIO_QUANTIZER, rounding=ROUND_DOWN)
        if isinstance(value, str):
            try:
                return Decimal(value).quantize(_DECIMAL_RATIO_QUANTIZER, rounding=ROUND_DOWN)
            except Exception:  # noqa: BLE001
                return fallback
        return fallback

    def _build_status_hint(
        self,
        *,
        healing_status: str,
        can_complete_recovery: bool,
        can_interrupt_recovery: bool,
        can_start_recovery: bool,
        retreat_running: bool,
        endless_running: bool,
        recovery_needed: bool,
        recovery_progress: Decimal,
        remaining_recovery_seconds: int,
    ) -> str:
        if healing_status == _RUNNING_HEALING_STATUS:
            if can_complete_recovery:
                return "当前已恢复完成，可结束打坐并回满状态。"
            return (
                f"当前恢复进度 {self._format_percent(recovery_progress)}，"
                f"还需 {self._format_duration(remaining_recovery_seconds)}；也可现在结束按比例恢复。"
            )
        if retreat_running:
            return "当前正在闭关，无法开始打坐恢复。"
        if endless_running:
            return "当前正在无尽副本中，无法开始打坐恢复。"
        if can_start_recovery:
            return "打坐恢复规则：20 分钟恢复 100%，中途结束按比例恢复。"
        if not recovery_needed:
            return "当前状态完好，无需打坐恢复。"
        if can_interrupt_recovery or can_complete_recovery:
            return "当前可处理恢复动作。"
        return "当前无法进行打坐恢复。"

    @staticmethod
    def _format_percent(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.1'), rounding=ROUND_DOWN)}%"

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
