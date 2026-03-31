"""闭关修炼服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from application.character.growth_service import (
    CharacterGrowthService,
    CharacterGrowthSnapshot,
    CharacterGrowthStateError,
    CharacterNotFoundError,
)
from domain.character import resolve_spirit_stone_economy_multiplier
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import CurrencyBalance, RetreatState
from infrastructure.db.repositories import CharacterRepository, StateRepository

_CLOSED_DOOR_SOURCE_CATEGORY = "closed_door"
_DECIMAL_INTEGER = Decimal("1")
_DECIMAL_ONE = Decimal("1.0000")
_DECIMAL_RATIO_QUANTIZER = Decimal("0.0001")
_RETREAT_STATUS_COMPLETED = "completed"
_RETREAT_STATUS_RUNNING = "running"
_RUNNING_HEALING_STATUS = "running"
_DEFAULT_RETREAT_DURATION = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class RetreatRewardBreakdown:
    """闭关收益拆分结果。"""

    realm_id: str
    actual_elapsed_seconds: int
    settlement_seconds: int
    reward_seconds: int
    cultivation_amount: int
    comprehension_amount: int
    spirit_stone_amount: int

    @property
    def elapsed_seconds(self) -> int:
        """兼容旧字段名，返回本次结算秒数。"""
        return self.settlement_seconds


@dataclass(frozen=True, slots=True)
class RetreatStatusSnapshot:
    """闭关状态快照。"""

    character_id: int
    status: str
    realm_id: str | None
    started_at: datetime | None
    scheduled_end_at: datetime | None
    settled_at: datetime | None
    can_settle: bool
    reward_available: bool
    elapsed_seconds: int
    settlement_seconds: int
    minimum_reward_seconds: int
    full_yield_seconds: int
    yield_progress: Decimal
    status_hint: str
    pending_cultivation: int
    pending_comprehension: int
    pending_spirit_stone: int


@dataclass(frozen=True, slots=True)
class RetreatSettlementResult:
    """闭关结算结果。"""

    character_id: int
    realm_id: str
    started_at: datetime
    scheduled_end_at: datetime
    settled_at: datetime
    reward: RetreatRewardBreakdown
    applied_cultivation: int
    growth_snapshot: CharacterGrowthSnapshot
    reward_available: bool
    minimum_reward_seconds: int
    full_yield_seconds: int


@dataclass(frozen=True, slots=True)
class _RetreatProgressMetrics:
    """闭关结算进度指标。"""

    actual_elapsed_seconds: int
    settlement_seconds: int
    reward_seconds: int
    minimum_reward_seconds: int
    full_yield_seconds: int
    reward_available: bool
    yield_progress: Decimal


class RetreatServiceError(RuntimeError):
    """闭关服务基础异常。"""


class RetreatAlreadyRunningError(RetreatServiceError):
    """角色已经处于闭关中。"""


class RetreatNotFoundError(RetreatServiceError):
    """角色不存在闭关记录。"""


class RetreatNotReadyError(RetreatServiceError):
    """当前闭关尚未达到可结算时间。"""


class RetreatStateError(RetreatServiceError):
    """闭关状态数据不完整。"""


class InvalidRetreatDurationError(RetreatServiceError):
    """闭关持续时间非法。"""


class RetreatService:
    """负责编排闭关开始、状态读取与收益结算。"""

    def __init__(
        self,
        *,
        state_repository: StateRepository,
        character_repository: CharacterRepository,
        growth_service: CharacterGrowthService,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._state_repository = state_repository
        self._character_repository = character_repository
        self._growth_service = growth_service
        self._static_config = static_config or get_static_config()

    def start_retreat(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
        duration: timedelta | None = None,
    ) -> RetreatStatusSnapshot:
        """开始一段新的闭关。"""
        current_time = now or datetime.utcnow()
        normalized_duration = self._normalize_duration(duration)
        retreat_state = self._state_repository.get_retreat_state(character_id)
        minimum_reward_seconds = int(self._minimum_reward_duration().total_seconds())

        if retreat_state is not None and retreat_state.status == _RETREAT_STATUS_RUNNING and retreat_state.settled_at is None:
            raise RetreatAlreadyRunningError(f"角色已在闭关中：{character_id}")
        if self._has_running_healing(character_id=character_id):
            raise RetreatServiceError(f"角色正在打坐恢复中，需先等待完成或主动结束恢复：{character_id}")

        growth_snapshot = self._growth_service.get_snapshot(character_id=character_id)
        if retreat_state is None:
            retreat_state = RetreatState(
                character_id=character_id,
                status=_RETREAT_STATUS_RUNNING,
                started_at=current_time,
                scheduled_end_at=current_time + normalized_duration,
                settled_at=None,
                context_json={"realm_id": growth_snapshot.realm_id},
            )
        else:
            retreat_state.status = _RETREAT_STATUS_RUNNING
            retreat_state.started_at = current_time
            retreat_state.scheduled_end_at = current_time + normalized_duration
            retreat_state.settled_at = None
            retreat_state.context_json = {"realm_id": growth_snapshot.realm_id}

        self._state_repository.save_retreat_state(retreat_state)
        if minimum_reward_seconds > 0:
            reward_preview_time = current_time + timedelta(seconds=minimum_reward_seconds)
            if reward_preview_time <= self._require_scheduled_end_at(retreat_state):
                preview_metrics = self._build_progress_metrics(retreat_state=retreat_state, now=reward_preview_time)
                preview_reward = self._calculate_reward(retreat_state=retreat_state, metrics=preview_metrics)
                if preview_reward.cultivation_amount <= 0:
                    forced_reward_seconds = min(preview_metrics.settlement_seconds, preview_metrics.full_yield_seconds)
                    if forced_reward_seconds > 0:
                        fallback_cultivation = max(
                            1,
                            self._scale_full_yield_amount(
                                daily_amount=self._get_daily_entry(growth_snapshot.realm_id).daily_cultivation,
                                ratio=self._get_closed_door_source(growth_snapshot.realm_id).ratio,
                                reward_seconds=forced_reward_seconds,
                                full_yield_seconds=preview_metrics.full_yield_seconds,
                            ),
                        )
                        if fallback_cultivation > 0:
                            yield_config = self._static_config.cultivation_sources.closed_door_yield
                            adjusted_context = dict(retreat_state.context_json)
                            adjusted_context["minimum_reward_bonus"] = {
                                "cultivation_amount": fallback_cultivation,
                                "comprehension_amount": self._scale_integer_amount(
                                    amount=fallback_cultivation,
                                    ratio=yield_config.insight_gain_ratio,
                                ),
                                "spirit_stone_amount": self._scale_integer_amount(
                                    amount=fallback_cultivation,
                                    ratio=yield_config.spirit_stone_gain_ratio,
                                ) * self._resolve_spirit_stone_multiplier(growth_snapshot.realm_id),
                            }
                            retreat_state.context_json = adjusted_context
                            self._state_repository.save_retreat_state(retreat_state)
        snapshot = self.get_retreat_status(character_id=character_id, now=current_time)
        assert snapshot is not None
        return snapshot

    def get_retreat_status(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> RetreatStatusSnapshot | None:
        """读取角色当前闭关状态。"""
        retreat_state = self._state_repository.get_retreat_state(character_id)
        if retreat_state is None:
            return None

        current_time = now or datetime.utcnow()
        metrics = self._build_progress_metrics(retreat_state=retreat_state, now=current_time)
        can_settle = self._can_finish_state(retreat_state=retreat_state)
        reward = self._calculate_reward(retreat_state=retreat_state, metrics=metrics) if can_settle else None
        pending_reward = reward if retreat_state.status == _RETREAT_STATUS_RUNNING and reward is not None else None
        return RetreatStatusSnapshot(
            character_id=character_id,
            status=retreat_state.status,
            realm_id=self._resolve_realm_id(retreat_state=retreat_state),
            started_at=retreat_state.started_at,
            scheduled_end_at=retreat_state.scheduled_end_at,
            settled_at=retreat_state.settled_at,
            can_settle=can_settle,
            reward_available=metrics.reward_available,
            elapsed_seconds=metrics.actual_elapsed_seconds,
            settlement_seconds=metrics.settlement_seconds,
            minimum_reward_seconds=metrics.minimum_reward_seconds,
            full_yield_seconds=metrics.full_yield_seconds,
            yield_progress=metrics.yield_progress,
            status_hint=self._build_status_hint(retreat_state=retreat_state, metrics=metrics),
            pending_cultivation=0 if pending_reward is None else pending_reward.cultivation_amount,
            pending_comprehension=0 if pending_reward is None else pending_reward.comprehension_amount,
            pending_spirit_stone=0 if pending_reward is None else pending_reward.spirit_stone_amount,
        )

    def can_settle(self, *, character_id: int, now: datetime | None = None) -> bool:
        """判断当前闭关是否可主动结束。"""
        del now
        retreat_state = self._state_repository.get_retreat_state(character_id)
        if retreat_state is None:
            return False
        return self._can_finish_state(retreat_state=retreat_state)

    def settle_retreat(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> RetreatSettlementResult:
        """结算当前闭关收益，并结束本次闭关。"""
        retreat_state = self._require_retreat_state(character_id)
        current_time = now or datetime.utcnow()
        if not self._can_finish_state(retreat_state=retreat_state):
            raise RetreatNotReadyError(f"闭关当前不可结束或已结算：{character_id}")

        metrics = self._build_progress_metrics(retreat_state=retreat_state, now=current_time)
        reward = self._calculate_reward(retreat_state=retreat_state, metrics=metrics)
        applied_cultivation = 0
        growth_snapshot = self._growth_service.get_snapshot(character_id=character_id)

        if reward.cultivation_amount > 0:
            cultivation_result = self._growth_service.add_cultivation(
                character_id=character_id,
                amount=reward.cultivation_amount,
            )
            applied_cultivation = cultivation_result.applied_amount
            growth_snapshot = cultivation_result.snapshot

        if reward.comprehension_amount > 0:
            growth_snapshot = self._growth_service.add_comprehension(
                character_id=character_id,
                amount=reward.comprehension_amount,
            )

        if reward.spirit_stone_amount > 0:
            balance = self._require_currency_balance(character_id)
            balance.spirit_stone += reward.spirit_stone_amount
            self._character_repository.save_currency_balance(balance)
            growth_snapshot = self._growth_service.get_snapshot(character_id=character_id)

        settled_at = current_time
        retreat_state.status = _RETREAT_STATUS_COMPLETED
        retreat_state.settled_at = settled_at
        self._state_repository.save_retreat_state(retreat_state)

        started_at = self._require_started_at(retreat_state)
        scheduled_end_at = self._require_scheduled_end_at(retreat_state)
        return RetreatSettlementResult(
            character_id=character_id,
            realm_id=reward.realm_id,
            started_at=started_at,
            scheduled_end_at=scheduled_end_at,
            settled_at=settled_at,
            reward=reward,
            applied_cultivation=applied_cultivation,
            growth_snapshot=growth_snapshot,
            reward_available=metrics.reward_available,
            minimum_reward_seconds=metrics.minimum_reward_seconds,
            full_yield_seconds=metrics.full_yield_seconds,
        )

    def _calculate_reward(
        self,
        *,
        retreat_state: RetreatState,
        metrics: _RetreatProgressMetrics,
    ) -> RetreatRewardBreakdown:
        realm_id = self._resolve_realm_id(retreat_state=retreat_state)
        if retreat_state.status != _RETREAT_STATUS_RUNNING:
            return RetreatRewardBreakdown(
                realm_id=realm_id,
                actual_elapsed_seconds=metrics.actual_elapsed_seconds,
                settlement_seconds=metrics.settlement_seconds,
                reward_seconds=metrics.reward_seconds,
                cultivation_amount=0,
                comprehension_amount=0,
                spirit_stone_amount=0,
            )

        if not metrics.reward_available:
            cultivation_amount = 0
            comprehension_amount = 0
            spirit_stone_amount = 0
        else:
            daily_entry = self._get_daily_entry(realm_id)
            closed_door_source = self._get_closed_door_source(realm_id)
            cultivation_amount = self._scale_full_yield_amount(
                daily_amount=daily_entry.daily_cultivation,
                ratio=closed_door_source.ratio,
                reward_seconds=metrics.reward_seconds,
                full_yield_seconds=metrics.full_yield_seconds,
            )
            if cultivation_amount <= 0:
                minimum_reward_bonus = self._read_minimum_reward_bonus(retreat_state=retreat_state)
                cultivation_amount = minimum_reward_bonus[0]
                comprehension_amount = minimum_reward_bonus[1]
                spirit_stone_amount = minimum_reward_bonus[2]
            else:
                comprehension_amount = self._scale_integer_amount(
                    amount=cultivation_amount,
                    ratio=self._static_config.cultivation_sources.closed_door_yield.insight_gain_ratio,
                )
                spirit_stone_amount = self._scale_integer_amount(
                    amount=cultivation_amount,
                    ratio=self._static_config.cultivation_sources.closed_door_yield.spirit_stone_gain_ratio,
                ) * self._resolve_spirit_stone_multiplier(realm_id)
        return RetreatRewardBreakdown(
            realm_id=realm_id,
            actual_elapsed_seconds=metrics.actual_elapsed_seconds,
            settlement_seconds=metrics.settlement_seconds,
            reward_seconds=metrics.reward_seconds,
            cultivation_amount=cultivation_amount,
            comprehension_amount=comprehension_amount,
            spirit_stone_amount=spirit_stone_amount,
        )

    def _build_progress_metrics(self, *, retreat_state: RetreatState, now: datetime) -> _RetreatProgressMetrics:
        minimum_reward_seconds = int(self._minimum_reward_duration().total_seconds())
        full_yield_seconds = int(self._full_yield_duration().total_seconds())
        actual_elapsed_seconds = self._resolve_actual_elapsed_seconds(retreat_state=retreat_state, now=now)
        settlement_seconds = min(actual_elapsed_seconds, self._resolve_planned_elapsed_seconds(retreat_state=retreat_state))
        reward_seconds = min(settlement_seconds, full_yield_seconds)
        reward_available = settlement_seconds >= minimum_reward_seconds
        return _RetreatProgressMetrics(
            actual_elapsed_seconds=actual_elapsed_seconds,
            settlement_seconds=settlement_seconds,
            reward_seconds=reward_seconds,
            minimum_reward_seconds=minimum_reward_seconds,
            full_yield_seconds=full_yield_seconds,
            reward_available=reward_available,
            yield_progress=self._build_yield_progress(
                reward_seconds=reward_seconds,
                full_yield_seconds=full_yield_seconds,
            ),
        )

    def _normalize_duration(self, duration: timedelta | None) -> timedelta:
        normalized_duration = _DEFAULT_RETREAT_DURATION if duration is None else duration
        if normalized_duration <= timedelta(0):
            raise InvalidRetreatDurationError("闭关持续时间必须大于 0")
        if normalized_duration > self._max_claim_duration():
            max_days = self._static_config.cultivation_sources.closed_door_yield.max_days_per_claim
            raise InvalidRetreatDurationError(f"闭关持续时间不能超过 {max_days} 天")
        return normalized_duration

    def _minimum_reward_duration(self) -> timedelta:
        minutes = self._static_config.cultivation_sources.closed_door_yield.minimum_reward_minutes
        return timedelta(minutes=minutes)

    def _full_yield_duration(self) -> timedelta:
        hours = self._static_config.cultivation_sources.closed_door_yield.full_yield_hours
        return timedelta(hours=hours)

    def _max_claim_duration(self) -> timedelta:
        max_days = self._static_config.cultivation_sources.closed_door_yield.max_days_per_claim
        return timedelta(days=max_days)

    @staticmethod
    def _scale_full_yield_amount(
        *,
        daily_amount: int,
        ratio: Decimal,
        reward_seconds: int,
        full_yield_seconds: int,
    ) -> int:
        if reward_seconds <= 0 or full_yield_seconds <= 0:
            return 0
        scaled = (Decimal(daily_amount) * ratio * Decimal(reward_seconds)) / Decimal(full_yield_seconds)
        return int(scaled.quantize(_DECIMAL_INTEGER, rounding=ROUND_DOWN))

    @staticmethod
    def _scale_integer_amount(*, amount: int, ratio: Decimal) -> int:
        scaled = Decimal(amount) * ratio
        return int(scaled.quantize(_DECIMAL_INTEGER, rounding=ROUND_DOWN))

    @staticmethod
    def _build_yield_progress(*, reward_seconds: int, full_yield_seconds: int) -> Decimal:
        if reward_seconds <= 0 or full_yield_seconds <= 0:
            return Decimal("0.0000")
        ratio = (Decimal(reward_seconds) / Decimal(full_yield_seconds)).quantize(
            _DECIMAL_RATIO_QUANTIZER,
            rounding=ROUND_DOWN,
        )
        return min(_DECIMAL_ONE, ratio)

    def _build_status_hint(self, *, retreat_state: RetreatState, metrics: _RetreatProgressMetrics) -> str:
        if retreat_state.status != _RETREAT_STATUS_RUNNING:
            return "上次闭关已结束，可重新开始新的闭关。"
        if not metrics.reward_available:
            remaining_seconds = max(0, metrics.minimum_reward_seconds - metrics.settlement_seconds)
            return (
                f"当前未达到 {self._format_duration(metrics.minimum_reward_seconds)} 起算门槛，"
                f"现在结束将无收益；还差 {self._format_duration(remaining_seconds)}。"
            )
        if metrics.reward_seconds >= metrics.full_yield_seconds:
            return "当前已达到 12 小时满收益上限，现在结束可拿满本次闭关收益。"
        remaining_seconds = max(0, metrics.full_yield_seconds - metrics.reward_seconds)
        return (
            f"当前收益进度 {self._format_percent(metrics.yield_progress)}，"
            f"距离满收益还差 {self._format_duration(remaining_seconds)}。"
        )

    def _resolve_planned_elapsed_seconds(self, *, retreat_state: RetreatState) -> int:
        started_at = self._require_started_at(retreat_state)
        scheduled_end_at = self._require_scheduled_end_at(retreat_state)
        if scheduled_end_at <= started_at:
            return 0
        return int((scheduled_end_at - started_at).total_seconds())

    def _resolve_actual_elapsed_seconds(self, *, retreat_state: RetreatState, now: datetime) -> int:
        started_at = self._require_started_at(retreat_state)
        if retreat_state.status == _RETREAT_STATUS_RUNNING:
            end_at = now
        elif retreat_state.settled_at is not None:
            end_at = retreat_state.settled_at
        else:
            end_at = self._require_scheduled_end_at(retreat_state)
        if end_at <= started_at:
            return 0
        return int((end_at - started_at).total_seconds())

    def _get_daily_entry(self, realm_id: str):
        for entry in self._static_config.daily_cultivation.entries:
            if entry.realm_id == realm_id:
                return entry
        raise RetreatStateError(f"缺少闭关标准日修为配置：{realm_id}")

    def _get_closed_door_source(self, realm_id: str):
        for source in self._static_config.cultivation_sources.sources:
            if source.realm_id == realm_id and source.source_category == _CLOSED_DOOR_SOURCE_CATEGORY:
                return source
        raise RetreatStateError(f"缺少闭关修为来源配置：{realm_id}")

    def _resolve_realm_id(self, *, retreat_state: RetreatState) -> str:
        realm_id = retreat_state.context_json.get("realm_id")
        if isinstance(realm_id, str) and realm_id:
            return realm_id
        return self._growth_service.get_snapshot(character_id=retreat_state.character_id).realm_id

    def _resolve_spirit_stone_multiplier(self, realm_id: str) -> int:
        return resolve_spirit_stone_economy_multiplier(
            static_config=self._static_config,
            realm_id=realm_id,
        )

    @staticmethod
    def _read_minimum_reward_bonus(*, retreat_state: RetreatState) -> tuple[int, int, int]:
        payload = retreat_state.context_json.get("minimum_reward_bonus")
        if not isinstance(payload, dict):
            return 0, 0, 0
        cultivation_amount = payload.get("cultivation_amount")
        comprehension_amount = payload.get("comprehension_amount")
        spirit_stone_amount = payload.get("spirit_stone_amount")
        return (
            cultivation_amount if isinstance(cultivation_amount, int) else 0,
            comprehension_amount if isinstance(comprehension_amount, int) else 0,
            spirit_stone_amount if isinstance(spirit_stone_amount, int) else 0,
        )

    @staticmethod
    def _can_finish_state(*, retreat_state: RetreatState) -> bool:
        if retreat_state.status != _RETREAT_STATUS_RUNNING:
            return False
        if retreat_state.settled_at is not None:
            return False
        if retreat_state.started_at is None or retreat_state.scheduled_end_at is None:
            return False
        return True

    @staticmethod
    def _can_settle_state(*, retreat_state: RetreatState, now: datetime) -> bool:
        if not RetreatService._can_finish_state(retreat_state=retreat_state):
            return False
        return now >= retreat_state.scheduled_end_at

    def _require_retreat_state(self, character_id: int) -> RetreatState:
        retreat_state = self._state_repository.get_retreat_state(character_id)
        if retreat_state is None:
            raise RetreatNotFoundError(f"角色不存在闭关记录：{character_id}")
        return retreat_state

    def _require_currency_balance(self, character_id: int) -> CurrencyBalance:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise CharacterNotFoundError(f"角色不存在：{character_id}")
        if aggregate.currency_balance is None:
            raise CharacterGrowthStateError(f"角色缺少货币余额：{character_id}")
        return aggregate.currency_balance

    def _has_running_healing(self, *, character_id: int) -> bool:
        healing_state = self._state_repository.get_healing_state(character_id)
        if healing_state is None:
            return False
        return healing_state.status == _RUNNING_HEALING_STATUS and healing_state.settled_at is None

    @staticmethod
    def _require_started_at(retreat_state: RetreatState) -> datetime:
        if retreat_state.started_at is None:
            raise RetreatStateError(f"闭关缺少开始时间：{retreat_state.character_id}")
        return retreat_state.started_at

    @staticmethod
    def _require_scheduled_end_at(retreat_state: RetreatState) -> datetime:
        if retreat_state.scheduled_end_at is None:
            raise RetreatStateError(f"闭关缺少结束时间：{retreat_state.character_id}")
        return retreat_state.scheduled_end_at

    @staticmethod
    def _format_percent(value: Decimal) -> str:
        return f"{(value * Decimal('100')).quantize(Decimal('0.1'), rounding=ROUND_DOWN)}%"

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        if total_seconds <= 0:
            return "0 分钟"
        minutes = total_seconds // 60
        hours, remaining_minutes = divmod(minutes, 60)
        parts: list[str] = []
        if hours > 0:
            parts.append(f"{hours} 小时")
        if remaining_minutes > 0 or not parts:
            parts.append(f"{remaining_minutes} 分钟")
        return "".join(parts)


__all__ = [
    "InvalidRetreatDurationError",
    "RetreatAlreadyRunningError",
    "RetreatNotFoundError",
    "RetreatNotReadyError",
    "RetreatRewardBreakdown",
    "RetreatService",
    "RetreatServiceError",
    "RetreatSettlementResult",
    "RetreatStateError",
    "RetreatStatusSnapshot",
]
