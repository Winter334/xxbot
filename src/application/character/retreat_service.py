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
_DECIMAL_DAY_SECONDS = Decimal("86400")
_DECIMAL_INTEGER = Decimal("1")
_RETREAT_STATUS_COMPLETED = "completed"
_RETREAT_STATUS_RUNNING = "running"
_DEFAULT_RETREAT_DURATION = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class RetreatRewardBreakdown:
    """闭关收益拆分结果。"""

    realm_id: str
    elapsed_seconds: int
    cultivation_amount: int
    comprehension_amount: int
    spirit_stone_amount: int


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
        growth_snapshot = self._growth_service.get_snapshot(character_id=character_id)
        retreat_state = self._state_repository.get_retreat_state(character_id)

        if retreat_state is not None and retreat_state.status == _RETREAT_STATUS_RUNNING and retreat_state.settled_at is None:
            raise RetreatAlreadyRunningError(f"角色已在闭关中：{character_id}")

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
        can_settle = self._can_settle_state(retreat_state=retreat_state, now=current_time)
        reward = self._calculate_reward(retreat_state=retreat_state, now=current_time) if can_settle else None

        return RetreatStatusSnapshot(
            character_id=character_id,
            status=retreat_state.status,
            realm_id=self._resolve_realm_id(retreat_state=retreat_state),
            started_at=retreat_state.started_at,
            scheduled_end_at=retreat_state.scheduled_end_at,
            settled_at=retreat_state.settled_at,
            can_settle=can_settle,
            pending_cultivation=0 if reward is None else reward.cultivation_amount,
            pending_comprehension=0 if reward is None else reward.comprehension_amount,
            pending_spirit_stone=0 if reward is None else reward.spirit_stone_amount,
        )

    def can_settle(self, *, character_id: int, now: datetime | None = None) -> bool:
        """判断当前闭关是否已达到可结算时间。"""
        retreat_state = self._state_repository.get_retreat_state(character_id)
        if retreat_state is None:
            return False
        return self._can_settle_state(retreat_state=retreat_state, now=now or datetime.utcnow())

    def settle_retreat(
        self,
        *,
        character_id: int,
        now: datetime | None = None,
    ) -> RetreatSettlementResult:
        """结算已完成的闭关收益，并结束本次闭关。"""
        retreat_state = self._require_retreat_state(character_id)
        current_time = now or datetime.utcnow()
        if not self._can_settle_state(retreat_state=retreat_state, now=current_time):
            raise RetreatNotReadyError(f"闭关尚未完成或已结算：{character_id}")

        reward = self._calculate_reward(retreat_state=retreat_state, now=current_time)
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

        claim_end_at = min(current_time, self._require_scheduled_end_at(retreat_state))
        retreat_state.status = _RETREAT_STATUS_COMPLETED
        retreat_state.settled_at = claim_end_at
        self._state_repository.save_retreat_state(retreat_state)

        started_at = self._require_started_at(retreat_state)
        scheduled_end_at = self._require_scheduled_end_at(retreat_state)
        return RetreatSettlementResult(
            character_id=character_id,
            realm_id=reward.realm_id,
            started_at=started_at,
            scheduled_end_at=scheduled_end_at,
            settled_at=claim_end_at,
            reward=reward,
            applied_cultivation=applied_cultivation,
            growth_snapshot=growth_snapshot,
        )

    def _calculate_reward(self, *, retreat_state: RetreatState, now: datetime) -> RetreatRewardBreakdown:
        started_at = self._require_started_at(retreat_state)
        scheduled_end_at = self._require_scheduled_end_at(retreat_state)
        if retreat_state.status != _RETREAT_STATUS_RUNNING:
            return RetreatRewardBreakdown(
                realm_id=self._resolve_realm_id(retreat_state=retreat_state),
                elapsed_seconds=0,
                cultivation_amount=0,
                comprehension_amount=0,
                spirit_stone_amount=0,
            )

        elapsed_until = min(now, scheduled_end_at)
        if elapsed_until <= started_at:
            elapsed_seconds = 0
        else:
            max_elapsed = self._max_claim_duration()
            elapsed_duration = min(elapsed_until - started_at, max_elapsed)
            elapsed_seconds = int(elapsed_duration.total_seconds())

        realm_id = self._resolve_realm_id(retreat_state=retreat_state)
        daily_entry = self._get_daily_entry(realm_id)
        closed_door_source = self._get_closed_door_source(realm_id)
        cultivation_amount = self._scale_daily_amount(
            daily_amount=daily_entry.daily_cultivation,
            ratio=closed_door_source.ratio,
            elapsed_seconds=elapsed_seconds,
        )
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
            elapsed_seconds=elapsed_seconds,
            cultivation_amount=cultivation_amount,
            comprehension_amount=comprehension_amount,
            spirit_stone_amount=spirit_stone_amount,
        )

    def _normalize_duration(self, duration: timedelta | None) -> timedelta:
        normalized_duration = _DEFAULT_RETREAT_DURATION if duration is None else duration
        if normalized_duration <= timedelta(0):
            raise InvalidRetreatDurationError("闭关持续时间必须大于 0")
        if normalized_duration > self._max_claim_duration():
            max_days = self._static_config.cultivation_sources.closed_door_yield.max_days_per_claim
            raise InvalidRetreatDurationError(f"闭关持续时间不能超过 {max_days} 天")
        return normalized_duration

    def _max_claim_duration(self) -> timedelta:
        max_days = self._static_config.cultivation_sources.closed_door_yield.max_days_per_claim
        return timedelta(days=max_days)

    @staticmethod
    def _scale_daily_amount(*, daily_amount: int, ratio: Decimal, elapsed_seconds: int) -> int:
        scaled = (Decimal(daily_amount) * ratio * Decimal(elapsed_seconds)) / _DECIMAL_DAY_SECONDS
        return int(scaled.quantize(_DECIMAL_INTEGER, rounding=ROUND_DOWN))

    @staticmethod
    def _scale_integer_amount(*, amount: int, ratio: Decimal) -> int:
        scaled = Decimal(amount) * ratio
        return int(scaled.quantize(_DECIMAL_INTEGER, rounding=ROUND_DOWN))

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
    def _can_settle_state(*, retreat_state: RetreatState, now: datetime) -> bool:
        if retreat_state.status != _RETREAT_STATUS_RUNNING:
            return False
        if retreat_state.settled_at is not None:
            return False
        if retreat_state.started_at is None or retreat_state.scheduled_end_at is None:
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
