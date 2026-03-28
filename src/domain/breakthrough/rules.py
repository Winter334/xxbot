"""突破秘境领域规则。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

from infrastructure.config.static.models.breakthrough import BreakthroughTrialConfig, BreakthroughTrialDefinition
from infrastructure.config.static.models.common import StaticGameConfig

from domain.breakthrough.models import (
    BreakthroughProgressSnapshot,
    BreakthroughRewardCycleType,
    BreakthroughRewardDirection,
    BreakthroughRewardItem,
    BreakthroughRewardKind,
    BreakthroughRewardPackage,
    BreakthroughSettlementResult,
    BreakthroughSettlementType,
    BreakthroughSoftLimitSnapshot,
    BreakthroughTrialProgressStatus,
    scale_reward_quantity,
)

_ALLOWED_REWARD_KINDS = frozenset(
    {
        BreakthroughRewardKind.QUALIFICATION,
        BreakthroughRewardKind.CURRENCY,
        BreakthroughRewardKind.MATERIAL,
    }
)
_FORBIDDEN_REPEAT_RESOURCE_IDS = frozenset(
    {
        "weapon",
        "armor",
        "accessory",
        "artifact",
        "common",
        "rare",
        "epic",
        "legendary",
    }
)


class BreakthroughRuleError(ValueError):
    """突破秘境规则输入不合法。"""


class BreakthroughRuleService:
    """封装阶段 7 所需的纯领域规则判定。"""

    def __init__(self, static_config: StaticGameConfig) -> None:
        self._static_config = static_config
        self._config: BreakthroughTrialConfig = static_config.breakthrough_trials
        self._known_stage_ids = {stage.stage_id for stage in static_config.realm_progression.stages}
        self._equipment_slot_ids = {slot.slot_id for slot in static_config.equipment.slots}
        self._equipment_quality_ids = {quality.quality_id for quality in static_config.equipment.qualities}
        self._artifact_template_ids = {
            template.template_id for template in static_config.equipment.artifact_templates
        }
        self._normal_equipment_template_ids = {
            template.template_id for template in static_config.equipment.base_templates
        }

    def get_current_trial(self, *, current_realm_id: str) -> BreakthroughTrialDefinition | None:
        """读取当前大境界对应的下一次突破映射。"""
        return self._config.get_trial_by_from_realm_id(current_realm_id)

    def can_challenge_trial(
        self,
        *,
        current_realm_id: str,
        target_mapping_id: str,
        cleared_mapping_ids: set[str] | frozenset[str],
    ) -> bool:
        """判定目标关卡当前是否允许挑战。"""
        trial = self._require_trial(target_mapping_id)
        if target_mapping_id in cleared_mapping_ids:
            return True
        current_trial = self.get_current_trial(current_realm_id=current_realm_id)
        if current_trial is None:
            return False
        return trial.mapping_id == current_trial.mapping_id

    def resolve_first_clear(
        self,
        *,
        current_realm_id: str,
        progress: BreakthroughProgressSnapshot,
        occurred_at: str,
    ) -> BreakthroughSettlementResult:
        """按首通规则结算资格与进度语义。"""
        trial = self._require_trial(progress.mapping_id)
        if progress.status is BreakthroughTrialProgressStatus.CLEARED:
            raise BreakthroughRuleError("已首通的关卡不能再次按首通规则结算")

        qualification_granted = trial.from_realm_id == current_realm_id
        reward_items = tuple(self._build_reward_items(trial, settlement_type=BreakthroughSettlementType.FIRST_CLEAR))
        reward_package = BreakthroughRewardPackage(direction=None, items=reward_items)
        return BreakthroughSettlementResult(
            settlement_type=BreakthroughSettlementType.FIRST_CLEAR,
            victory=True,
            mapping_id=trial.mapping_id,
            reward_package=reward_package,
            qualification_granted=qualification_granted,
            resulting_progress_status=BreakthroughTrialProgressStatus.CLEARED,
        )

    def resolve_repeat_clear(
        self,
        *,
        mapping_id: str,
        progress: BreakthroughProgressSnapshot,
        cycle_anchor: date,
        consumed_count_before: int,
    ) -> BreakthroughSettlementResult:
        """按重复挑战与软限制规则结算资源奖励。"""
        trial = self._require_trial(mapping_id)
        if progress.status is not BreakthroughTrialProgressStatus.CLEARED:
            raise BreakthroughRuleError("未首通的关卡不能结算重复挑战奖励")
        pool = self._require_reward_pool(trial.repeat_reward_pool_id)
        direction = self._parse_direction(pool.reward_direction)
        ratio = pool.high_yield_ratio if consumed_count_before < pool.high_yield_limit else pool.reduced_yield_ratio
        soft_limit = BreakthroughSoftLimitSnapshot(
            reward_direction=direction,
            cycle_type=BreakthroughRewardCycleType(pool.cycle_type),
            cycle_anchor=cycle_anchor.isoformat(),
            high_yield_limit=pool.high_yield_limit,
            consumed_count_before=consumed_count_before,
            consumed_count_after=consumed_count_before + 1,
            applied_ratio=ratio,
            entered_reduced_yield=consumed_count_before >= pool.high_yield_limit,
        )
        reward_items = tuple(
            self._build_repeat_reward_items(
                trial=trial,
                ratio=ratio,
            )
        )
        reward_package = BreakthroughRewardPackage(
            direction=direction,
            items=reward_items,
            soft_limit=soft_limit,
        )
        return BreakthroughSettlementResult(
            settlement_type=BreakthroughSettlementType.REPEAT_CLEAR,
            victory=True,
            mapping_id=trial.mapping_id,
            reward_package=reward_package,
            qualification_granted=False,
            resulting_progress_status=BreakthroughTrialProgressStatus.CLEARED,
        )

    def resolve_defeat(self, *, mapping_id: str, previous_status: BreakthroughTrialProgressStatus | None) -> BreakthroughSettlementResult:
        """按失败规则输出空奖励结算。"""
        trial = self._require_trial(mapping_id)
        return BreakthroughSettlementResult(
            settlement_type=BreakthroughSettlementType.DEFEAT,
            victory=False,
            mapping_id=trial.mapping_id,
            reward_package=BreakthroughRewardPackage(direction=None, items=()),
            qualification_granted=False,
            resulting_progress_status=previous_status,
        )

    def build_next_progress_snapshot(
        self,
        *,
        previous: BreakthroughProgressSnapshot,
        settlement_result: BreakthroughSettlementResult,
        occurred_at: str,
    ) -> BreakthroughProgressSnapshot:
        """根据结算结果推导下一份进度快照。"""
        attempt_count = previous.attempt_count + 1
        last_reward_direction = (
            None if settlement_result.reward_package.direction is None else settlement_result.reward_package.direction.value
        )
        if settlement_result.settlement_type is BreakthroughSettlementType.DEFEAT:
            return replace(
                previous,
                status=previous.status or BreakthroughTrialProgressStatus.FAILED,
                attempt_count=attempt_count,
                last_reward_direction=last_reward_direction,
            )

        cleared_count = previous.cleared_count + 1
        next_snapshot = replace(
            previous,
            status=BreakthroughTrialProgressStatus.CLEARED,
            attempt_count=attempt_count,
            cleared_count=cleared_count,
            best_clear_at=occurred_at,
            last_cleared_at=occurred_at,
            last_reward_direction=last_reward_direction,
        )
        if settlement_result.settlement_type is BreakthroughSettlementType.FIRST_CLEAR:
            next_snapshot = replace(
                next_snapshot,
                first_cleared_at=previous.first_cleared_at or occurred_at,
                qualification_granted_at=occurred_at if settlement_result.qualification_granted else previous.qualification_granted_at,
            )
        return next_snapshot

    def enforce_reward_boundary(self, package: BreakthroughRewardPackage) -> None:
        """在领域层再次阻止核心终局掉落进入突破奖励。"""
        for item in package.items:
            if item.reward_kind not in _ALLOWED_REWARD_KINDS:
                raise BreakthroughRuleError("突破秘境奖励类型越界")
            if item.reward_kind is BreakthroughRewardKind.QUALIFICATION:
                continue
            assert item.resource_id is not None
            if item.resource_id in _FORBIDDEN_REPEAT_RESOURCE_IDS:
                raise BreakthroughRuleError("突破秘境奖励不能直接使用终局实体标识")
            if item.resource_id in self._equipment_slot_ids:
                raise BreakthroughRuleError("突破秘境奖励不能掉落装备槽位实体")
            if item.resource_id in self._equipment_quality_ids:
                raise BreakthroughRuleError("突破秘境奖励不能掉落装备品质实体")
            if item.resource_id in self._artifact_template_ids:
                raise BreakthroughRuleError("突破秘境奖励不能掉落法宝模板")
            if item.resource_id in self._normal_equipment_template_ids:
                raise BreakthroughRuleError("突破秘境奖励不能掉落装备模板")
            if item.reward_kind is BreakthroughRewardKind.MATERIAL and not item.bound:
                raise BreakthroughRuleError("突破秘境材料奖励必须为绑定物品")

    def validate_trial_configuration(self) -> None:
        """在领域层复核关键配置，避免应用层误用危险奖励。"""
        for trial in self._config.trials:
            if trial.boss_stage_id not in self._known_stage_ids:
                raise BreakthroughRuleError(f"突破关卡引用了未定义阶段：{trial.boss_stage_id}")
            pool = self._require_reward_pool(trial.repeat_reward_pool_id)
            if pool.reward_direction != trial.repeat_reward_direction:
                raise BreakthroughRuleError("突破关卡奖励方向与绑定奖励池不一致")
            repeat_items = tuple(self._build_repeat_reward_items(trial=trial, ratio=Decimal("1.0")))
            self.enforce_reward_boundary(
                BreakthroughRewardPackage(
                    direction=self._parse_direction(pool.reward_direction),
                    items=repeat_items,
                    soft_limit=BreakthroughSoftLimitSnapshot(
                        reward_direction=self._parse_direction(pool.reward_direction),
                        cycle_type=BreakthroughRewardCycleType(pool.cycle_type),
                        cycle_anchor="1970-01-01",
                        high_yield_limit=pool.high_yield_limit,
                        consumed_count_before=0,
                        consumed_count_after=1,
                        applied_ratio=Decimal("1.0"),
                        entered_reduced_yield=False,
                    ),
                )
            )

    def _build_reward_items(
        self,
        trial: BreakthroughTrialDefinition,
        *,
        settlement_type: BreakthroughSettlementType,
    ) -> list[BreakthroughRewardItem]:
        items: list[BreakthroughRewardItem] = []
        for reward in trial.first_clear_rewards:
            reward_kind = BreakthroughRewardKind(reward.reward_kind)
            if reward_kind is BreakthroughRewardKind.QUALIFICATION:
                items.append(BreakthroughRewardItem(reward_kind=reward_kind))
                continue
            items.append(
                BreakthroughRewardItem(
                    reward_kind=reward_kind,
                    resource_id=reward.resource_id,
                    quantity=reward.quantity,
                    bound=reward.bound,
                )
            )
        package = BreakthroughRewardPackage(direction=None, items=tuple(items))
        self.enforce_reward_boundary(package)
        return items

    def _build_repeat_reward_items(
        self,
        *,
        trial: BreakthroughTrialDefinition,
        ratio: Decimal,
    ) -> list[BreakthroughRewardItem]:
        pool = self._require_reward_pool(trial.repeat_reward_pool_id)
        items = [
            BreakthroughRewardItem(
                reward_kind=BreakthroughRewardKind(resource.resource_kind),
                resource_id=resource.resource_id,
                quantity=scale_reward_quantity(resource.quantity, ratio),
                bound=resource.bound,
            )
            for resource in pool.resources
        ]
        self.enforce_reward_boundary(
            BreakthroughRewardPackage(
                direction=self._parse_direction(pool.reward_direction),
                items=tuple(items),
                soft_limit=BreakthroughSoftLimitSnapshot(
                    reward_direction=self._parse_direction(pool.reward_direction),
                    cycle_type=BreakthroughRewardCycleType(pool.cycle_type),
                    cycle_anchor="1970-01-01",
                    high_yield_limit=pool.high_yield_limit,
                    consumed_count_before=0,
                    consumed_count_after=1,
                    applied_ratio=ratio,
                    entered_reduced_yield=False,
                ),
            )
        )
        return items

    def _require_trial(self, mapping_id: str) -> BreakthroughTrialDefinition:
        trial = self._config.get_trial(mapping_id)
        if trial is None:
            raise BreakthroughRuleError(f"未定义的突破映射：{mapping_id}")
        return trial

    def _require_reward_pool(self, pool_id: str):
        pool = self._config.get_repeat_reward_pool(pool_id)
        if pool is None:
            raise BreakthroughRuleError(f"未定义的重复奖励池：{pool_id}")
        return pool

    @staticmethod
    def _parse_direction(value: str) -> BreakthroughRewardDirection:
        try:
            return BreakthroughRewardDirection(value)
        except ValueError as exc:
            raise BreakthroughRuleError(f"未知奖励方向：{value}") from exc


__all__ = [
    "BreakthroughRuleError",
    "BreakthroughRuleService",
]
