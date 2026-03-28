"""突破秘境奖励结算应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from domain.battle import BattleOutcome
from domain.character import resolve_spirit_stone_economy_multiplier
from domain.breakthrough import (
    BreakthroughProgressSnapshot,
    BreakthroughRewardCycleType,
    BreakthroughRewardDirection,
    BreakthroughRewardKind,
    BreakthroughRuleError,
    BreakthroughRuleService,
    BreakthroughSettlementResult,
    BreakthroughTrialProgressStatus,
)
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.config.static.models.breakthrough import BreakthroughTrialDefinition
from infrastructure.db.models import (
    BreakthroughRewardLedger,
    BreakthroughTrialProgress,
    CurrencyBalance,
    DropRecord,
    InventoryItem,
)
from infrastructure.db.repositories import (
    BattleRecordRepository,
    BreakthroughRepository,
    BreakthroughRewardLedgerRepository,
    CharacterAggregate,
    CharacterRepository,
    InventoryRepository,
    build_breakthrough_progress_snapshot,
)

_BREAKTHROUGH_SOURCE_TYPE = "breakthrough_trial"
_REPEAT_SETTLEMENT_MODE = "repeat"
_FIRST_CLEAR_SETTLEMENT_MODE = "first_clear"
_ALLOWED_CURRENCY_IDS = frozenset({"spirit_stone"})
_ALLOWED_ITEM_TYPE = "material"


@dataclass(frozen=True, slots=True)
class BreakthroughRewardApplicationResult:
    """单次突破秘境奖励结算后的稳定返回结构。"""

    settlement_type: str
    victory: bool
    qualification_granted: bool
    progress_status: str | None
    attempt_count: int
    cleared_count: int
    reward_payload: dict[str, object]
    settlement_payload: dict[str, object]
    soft_limit_snapshot: dict[str, object] | None
    currency_changes: dict[str, int]
    item_changes: tuple[dict[str, object], ...]
    battle_report_id: int | None
    drop_record_id: int | None
    source_ref: str


class BreakthroughRewardServiceError(RuntimeError):
    """突破秘境奖励服务基础异常。"""


class BreakthroughRewardStateError(BreakthroughRewardServiceError):
    """突破秘境奖励结算依赖的角色状态不完整。"""


class BreakthroughRewardBoundaryError(BreakthroughRewardServiceError):
    """突破秘境奖励越过阶段 7 应用层边界。"""


class BreakthroughRewardService:
    """负责首通资格、重复奖励与阶段 7 资源边界。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        breakthrough_repository: BreakthroughRepository,
        reward_ledger_repository: BreakthroughRewardLedgerRepository,
        inventory_repository: InventoryRepository,
        battle_record_repository: BattleRecordRepository,
        static_config: StaticGameConfig | None = None,
        rule_service: BreakthroughRuleService | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._breakthrough_repository = breakthrough_repository
        self._reward_ledger_repository = reward_ledger_repository
        self._inventory_repository = inventory_repository
        self._battle_record_repository = battle_record_repository
        self._static_config = static_config or get_static_config()
        self._rule_service = rule_service or BreakthroughRuleService(self._static_config)
        self._rule_service.validate_trial_configuration()

    def apply_battle_result(
        self,
        *,
        aggregate: CharacterAggregate,
        trial: BreakthroughTrialDefinition,
        trial_progress: BreakthroughTrialProgress,
        battle_outcome: BattleOutcome,
        battle_report_id: int | None,
        occurred_at: datetime,
    ) -> BreakthroughRewardApplicationResult:
        """按阶段 7 规则结算战斗结果，并写入进度与资源。"""
        progress = aggregate.progress
        if progress is None:
            raise BreakthroughRewardStateError(f"角色缺少成长状态：{aggregate.character.id}")

        previous_snapshot = build_breakthrough_progress_snapshot(trial_progress)
        settlement_mode = self._resolve_settlement_mode(previous_status=previous_snapshot.status)
        settlement_result, updated_ledger = self._resolve_settlement_result(
            character_id=aggregate.character.id,
            current_realm_id=progress.realm_id,
            trial=trial,
            previous_snapshot=previous_snapshot,
            battle_outcome=battle_outcome,
            occurred_at=occurred_at,
        )
        self._enforce_application_boundary(settlement_result=settlement_result)

        next_snapshot = self._rule_service.build_next_progress_snapshot(
            previous=previous_snapshot,
            settlement_result=settlement_result,
            occurred_at=occurred_at.isoformat(),
        )
        settlement_payload = settlement_result.to_progress_payload(
            battle_report_id=battle_report_id,
            occurred_at=occurred_at.isoformat(),
        )
        self._apply_progress_snapshot(
            progress_model=trial_progress,
            progress_snapshot=next_snapshot,
            settlement_payload=settlement_payload,
        )

        currency_changes: dict[str, int] = {}
        item_changes: list[dict[str, object]] = []
        if settlement_result.victory:
            currency_changes, item_changes = self._apply_reward_package(
                aggregate=aggregate,
                settlement_result=settlement_result,
            )
        if settlement_result.qualification_granted:
            progress.breakthrough_qualification_obtained = True

        source_ref = self._build_source_ref(
            mapping_id=trial.mapping_id,
            settlement_mode=settlement_mode,
            victory=settlement_result.victory,
        )
        drop_record = self._battle_record_repository.add_drop_record(
            self._build_drop_record(
                character_id=aggregate.character.id,
                battle_report_id=battle_report_id,
                source_ref=source_ref,
                currency_changes=currency_changes,
                item_changes=tuple(item_changes),
            )
        )
        trial_progress.last_result_json = {
            **settlement_payload,
            "currency_changes": currency_changes,
            "item_changes": item_changes,
            "drop_record_id": drop_record.id,
            "source_ref": source_ref,
        }

        self._character_repository.save_progress(progress)
        self._breakthrough_repository.save_progress(trial_progress)
        if updated_ledger is not None:
            self._reward_ledger_repository.save_ledger(updated_ledger)
        return BreakthroughRewardApplicationResult(
            settlement_type=settlement_result.settlement_type.value,
            victory=settlement_result.victory,
            qualification_granted=settlement_result.qualification_granted,
            progress_status=None if next_snapshot.status is None else next_snapshot.status.value,
            attempt_count=next_snapshot.attempt_count,
            cleared_count=next_snapshot.cleared_count,
            reward_payload=settlement_result.reward_package.to_payload(),
            settlement_payload=settlement_payload,
            soft_limit_snapshot=(
                None
                if settlement_result.reward_package.soft_limit is None
                else settlement_result.reward_package.soft_limit.to_payload()
            ),
            currency_changes=currency_changes,
            item_changes=tuple(item_changes),
            battle_report_id=battle_report_id,
            drop_record_id=drop_record.id,
            source_ref=source_ref,
        )

    def _resolve_settlement_result(
        self,
        *,
        character_id: int,
        current_realm_id: str,
        trial: BreakthroughTrialDefinition,
        previous_snapshot: BreakthroughProgressSnapshot,
        battle_outcome: BattleOutcome,
        occurred_at: datetime,
    ) -> tuple[BreakthroughSettlementResult, BreakthroughRewardLedger | None]:
        """根据胜负与历史状态解析本次结算语义。"""
        if battle_outcome is not BattleOutcome.ALLY_VICTORY:
            try:
                return (
                    self._rule_service.resolve_defeat(
                        mapping_id=trial.mapping_id,
                        previous_status=previous_snapshot.status,
                    ),
                    None,
                )
            except BreakthroughRuleError as exc:
                raise BreakthroughRewardBoundaryError(str(exc)) from exc

        if previous_snapshot.status is BreakthroughTrialProgressStatus.CLEARED:
            return self._resolve_repeat_clear(
                character_id=character_id,
                trial=trial,
                progress_snapshot=previous_snapshot,
                occurred_at=occurred_at,
            )
        try:
            return (
                self._rule_service.resolve_first_clear(
                    current_realm_id=current_realm_id,
                    progress=previous_snapshot,
                    occurred_at=occurred_at.isoformat(),
                ),
                None,
            )
        except BreakthroughRuleError as exc:
            raise BreakthroughRewardBoundaryError(str(exc)) from exc

    def _resolve_repeat_clear(
        self,
        *,
        character_id: int,
        trial: BreakthroughTrialDefinition,
        progress_snapshot: BreakthroughProgressSnapshot,
        occurred_at: datetime,
    ) -> tuple[BreakthroughSettlementResult, BreakthroughRewardLedger]:
        """按方向级软限制账本结算重复挑战奖励。"""
        pool = self._static_config.breakthrough_trials.get_repeat_reward_pool(trial.repeat_reward_pool_id)
        if pool is None:
            raise BreakthroughRewardBoundaryError(f"突破关卡缺少重复奖励池：{trial.mapping_id}")
        try:
            reward_direction = BreakthroughRewardDirection(pool.reward_direction)
            cycle_type = BreakthroughRewardCycleType(pool.cycle_type)
        except ValueError as exc:
            raise BreakthroughRewardBoundaryError(f"突破奖励池声明非法：{pool.pool_id}") from exc
        cycle_anchor = self._resolve_cycle_anchor(cycle_type=cycle_type, occurred_at=occurred_at)
        ledger = self._reward_ledger_repository.get_or_create_ledger(
            character_id,
            reward_direction,
            cycle_type,
            cycle_anchor,
        )
        consumed_count_before = max(0, ledger.high_yield_settlement_count)
        try:
            settlement_result = self._rule_service.resolve_repeat_clear(
                mapping_id=trial.mapping_id,
                progress=progress_snapshot,
                cycle_anchor=cycle_anchor,
                consumed_count_before=consumed_count_before,
            )
        except BreakthroughRuleError as exc:
            raise BreakthroughRewardBoundaryError(str(exc)) from exc
        soft_limit = settlement_result.reward_package.soft_limit
        if soft_limit is None:
            raise BreakthroughRewardBoundaryError("重复挑战结算缺少软限制快照")
        ledger.high_yield_settlement_count = soft_limit.consumed_count_after
        ledger.last_settled_at = occurred_at
        return settlement_result, ledger

    def _apply_reward_package(
        self,
        *,
        aggregate: CharacterAggregate,
        settlement_result: BreakthroughSettlementResult,
    ) -> tuple[dict[str, int], list[dict[str, object]]]:
        """把奖励包写入角色货币与库存。"""
        currency_changes: dict[str, int] = {}
        item_changes: list[dict[str, object]] = []
        for reward in settlement_result.reward_package.items:
            if reward.reward_kind is BreakthroughRewardKind.QUALIFICATION:
                continue
            if reward.reward_kind is BreakthroughRewardKind.CURRENCY:
                assert reward.resource_id is not None
                assert reward.quantity is not None
                actual_quantity = self._apply_currency_reward(
                    aggregate=aggregate,
                    resource_id=reward.resource_id,
                    quantity=reward.quantity,
                )
                currency_changes[reward.resource_id] = currency_changes.get(reward.resource_id, 0) + actual_quantity
                continue
            if reward.reward_kind is BreakthroughRewardKind.MATERIAL:
                assert reward.resource_id is not None
                assert reward.quantity is not None
                item_changes.append(
                    self._apply_material_reward(
                        character_id=aggregate.character.id,
                        resource_id=reward.resource_id,
                        quantity=reward.quantity,
                        bound=reward.bound,
                    )
                )
                continue
            raise BreakthroughRewardBoundaryError(f"突破秘境出现未支持奖励类型：{reward.reward_kind.value}")
        return currency_changes, item_changes

    def _apply_currency_reward(
        self,
        *,
        aggregate: CharacterAggregate,
        resource_id: str,
        quantity: int,
    ) -> int:
        """阶段 7 只允许发放白名单内的绑定主货币。"""
        if resource_id not in _ALLOWED_CURRENCY_IDS:
            raise BreakthroughRewardBoundaryError(f"突破秘境货币奖励越界：{resource_id}")
        balance = self._require_currency_balance(aggregate)
        current_value = getattr(balance, resource_id, None)
        if not isinstance(current_value, int):
            raise BreakthroughRewardBoundaryError(f"角色货币字段不存在：{resource_id}")
        actual_quantity = quantity
        if resource_id == "spirit_stone":
            progress = aggregate.progress
            if progress is None:
                raise BreakthroughRewardStateError(f"角色缺少成长状态：{aggregate.character.id}")
            actual_quantity *= resolve_spirit_stone_economy_multiplier(
                static_config=self._static_config,
                realm_id=progress.realm_id,
            )
        setattr(balance, resource_id, max(0, current_value) + actual_quantity)
        self._character_repository.save_currency_balance(balance)
        return actual_quantity

    def _apply_material_reward(
        self,
        *,
        character_id: int,
        resource_id: str,
        quantity: int,
        bound: bool,
    ) -> dict[str, object]:
        """阶段 7 的材料奖励统一写入绑定库存。"""
        if not bound:
            raise BreakthroughRewardBoundaryError("突破秘境材料奖励必须为绑定物品")
        existing = self._inventory_repository.get_item(character_id, _ALLOWED_ITEM_TYPE, resource_id)
        next_quantity = quantity if existing is None else max(0, existing.quantity) + quantity
        payload: dict[str, object] = {"bound": True}
        if existing is not None and isinstance(existing.item_payload_json, dict):
            payload = dict(existing.item_payload_json)
            payload["bound"] = True
        saved_item = self._inventory_repository.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type=_ALLOWED_ITEM_TYPE,
                item_id=resource_id,
                quantity=next_quantity,
                item_payload_json=payload,
            )
        )
        return {
            "reward_kind": BreakthroughRewardKind.MATERIAL.value,
            "item_type": saved_item.item_type,
            "item_id": saved_item.item_id,
            "quantity": quantity,
            "total_quantity": saved_item.quantity,
            "bound": True,
        }

    def _apply_progress_snapshot(
        self,
        *,
        progress_model: BreakthroughTrialProgress,
        progress_snapshot: BreakthroughProgressSnapshot,
        settlement_payload: dict[str, object],
    ) -> None:
        """把领域快照回写到突破进度持久化模型。"""
        progress_model.status = (
            BreakthroughTrialProgressStatus.FAILED.value
            if progress_snapshot.status is None
            else progress_snapshot.status.value
        )
        progress_model.attempt_count = progress_snapshot.attempt_count
        progress_model.cleared_count = progress_snapshot.cleared_count
        progress_model.best_clear_at = _parse_datetime(progress_snapshot.best_clear_at)
        progress_model.first_cleared_at = _parse_datetime(progress_snapshot.first_cleared_at)
        progress_model.last_cleared_at = _parse_datetime(progress_snapshot.last_cleared_at)
        progress_model.qualification_granted_at = _parse_datetime(progress_snapshot.qualification_granted_at)
        progress_model.last_reward_direction = progress_snapshot.last_reward_direction
        progress_model.last_result_json = settlement_payload

    def _build_drop_record(
        self,
        *,
        character_id: int,
        battle_report_id: int | None,
        source_ref: str,
        currency_changes: dict[str, int],
        item_changes: tuple[dict[str, object], ...],
    ) -> DropRecord:
        """突破秘境审计只记录基础资源与战报引用。"""
        items_json = [
            {
                "reward_kind": item["reward_kind"],
                "item_type": item["item_type"],
                "item_id": item["item_id"],
                "quantity": item["quantity"],
                "bound": item["bound"],
            }
            for item in item_changes
        ]
        return DropRecord(
            character_id=character_id,
            battle_report_id=battle_report_id,
            source_type=_BREAKTHROUGH_SOURCE_TYPE,
            source_ref=source_ref,
            items_json=items_json,
            currencies_json=currency_changes,
        )

    def _enforce_application_boundary(self, *, settlement_result: BreakthroughSettlementResult) -> None:
        """应用层再次阻止装备与终局掉落路径混入阶段 7。"""
        try:
            self._rule_service.enforce_reward_boundary(settlement_result.reward_package)
        except BreakthroughRuleError as exc:
            raise BreakthroughRewardBoundaryError(str(exc)) from exc
        for reward in settlement_result.reward_package.items:
            if reward.reward_kind is BreakthroughRewardKind.QUALIFICATION:
                continue
            if reward.reward_kind is BreakthroughRewardKind.CURRENCY:
                if reward.resource_id not in _ALLOWED_CURRENCY_IDS:
                    raise BreakthroughRewardBoundaryError(f"突破秘境货币奖励越界：{reward.resource_id}")
                continue
            if reward.reward_kind is BreakthroughRewardKind.MATERIAL:
                if not reward.bound:
                    raise BreakthroughRewardBoundaryError("突破秘境材料奖励必须固定为绑定")
                continue
            raise BreakthroughRewardBoundaryError(f"突破秘境奖励类型非法：{reward.reward_kind.value}")

    @staticmethod
    def _resolve_cycle_anchor(*, cycle_type: BreakthroughRewardCycleType, occurred_at: datetime) -> date:
        """首发软限制按自然日结算。"""
        if cycle_type is BreakthroughRewardCycleType.DAILY:
            return occurred_at.date()
        raise BreakthroughRewardBoundaryError(f"未支持的突破奖励周期类型：{cycle_type.value}")

    @staticmethod
    def _resolve_settlement_mode(*, previous_status: BreakthroughTrialProgressStatus | None) -> str:
        """源审计需要区分首通分支和重复挑战分支。"""
        if previous_status is BreakthroughTrialProgressStatus.CLEARED:
            return _REPEAT_SETTLEMENT_MODE
        return _FIRST_CLEAR_SETTLEMENT_MODE

    @staticmethod
    def _build_source_ref(*, mapping_id: str, settlement_mode: str, victory: bool) -> str:
        """构造突破秘境统一审计引用。"""
        return f"breakthrough:{mapping_id}:{settlement_mode}:{'victory' if victory else 'defeat'}"

    @staticmethod
    def _require_currency_balance(aggregate: CharacterAggregate) -> CurrencyBalance:
        """写入货币前要求角色余额模型存在。"""
        if aggregate.currency_balance is None:
            raise BreakthroughRewardStateError(f"角色缺少货币余额：{aggregate.character.id}")
        return aggregate.currency_balance


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


__all__ = [
    "BreakthroughRewardApplicationResult",
    "BreakthroughRewardBoundaryError",
    "BreakthroughRewardService",
    "BreakthroughRewardServiceError",
    "BreakthroughRewardStateError",
]
