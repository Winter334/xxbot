"""角色成长阶段规则。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from infrastructure.config.static.models.common import StaticGameConfig
from infrastructure.config.static.models.cultivation import DailyCultivationEntry
from infrastructure.config.static.models.progression import RealmDefinition, RealmStageDefinition

_DECIMAL_ZERO = Decimal("0")
_DECIMAL_INTEGER = Decimal("1")
_ENDLESS_ACTIVE_PEAK_REGION_DIVISOR = Decimal("5")
_BREAKTHROUGH_COMPREHENSION_MINIMUM = 10
_BREAKTHROUGH_COMPREHENSION_CURVE_BASE = 10.0
_BREAKTHROUGH_COMPREHENSION_CURVE_SCALE = 20.0
_BREAKTHROUGH_COMPREHENSION_CURVE_EXPONENT = 2.22
_SPIRIT_STONE_CURVE_EXPONENT = 0.28


@dataclass(frozen=True, slots=True)
class StageThreshold:
    """单个小阶段的入段门槛。"""

    stage_id: str
    stage_name: str
    order: int
    entry_cultivation: int


@dataclass(frozen=True, slots=True)
class RealmGrowthRule:
    """单个大境界的成长规则快照。"""

    realm_id: str
    realm_name: str
    total_cultivation: int
    stage_thresholds: tuple[StageThreshold, ...]


class GrowthRuleNotFoundError(LookupError):
    """成长规则不存在。"""


class CharacterGrowthProgression:
    """基于静态配置解析角色成长规则。"""

    def __init__(self, static_config: StaticGameConfig) -> None:
        ordered_realms = tuple(sorted(static_config.realm_progression.realms, key=lambda item: item.order))
        if not ordered_realms:
            raise GrowthRuleNotFoundError("未找到任何已配置的大境界")
        self._launch_realm_id = ordered_realms[0].realm_id
        self._realm_rules = self._build_realm_rules(
            static_config=static_config,
            ordered_realms=ordered_realms,
        )

    @property
    def launch_realm_id(self) -> str:
        """返回首发起始大境界标识。"""
        return self._launch_realm_id

    def get_launch_rule(self) -> RealmGrowthRule:
        """返回首发起始大境界成长规则。"""
        return self.get_realm_rule(self._launch_realm_id)

    def get_realm_rule(self, realm_id: str) -> RealmGrowthRule:
        """读取指定大境界成长规则。"""
        try:
            return self._realm_rules[realm_id]
        except KeyError as exc:
            raise GrowthRuleNotFoundError(f"未找到大境界成长规则：{realm_id}") from exc

    def resolve_stage(self, realm_id: str, cultivation_value: int) -> StageThreshold:
        """根据当前修为解析所在小阶段。"""
        rule = self.get_realm_rule(realm_id)
        normalized_value = max(0, cultivation_value)
        current_stage = rule.stage_thresholds[0]
        for threshold in rule.stage_thresholds:
            if normalized_value < threshold.entry_cultivation:
                break
            current_stage = threshold
        return current_stage

    @staticmethod
    def _build_realm_rules(
        *,
        static_config: StaticGameConfig,
        ordered_realms: tuple[RealmDefinition, ...],
    ) -> dict[str, RealmGrowthRule]:
        stage_definition_map = {stage.stage_id: stage for stage in static_config.realm_progression.stages}
        daily_entry_map = {entry.realm_id: entry for entry in static_config.daily_cultivation.entries}
        realm_rules: dict[str, RealmGrowthRule] = {}

        for realm in ordered_realms:
            daily_entry = CharacterGrowthProgression._require_daily_entry(
                realm_id=realm.realm_id,
                daily_entry_map=daily_entry_map,
            )
            ordered_stages = CharacterGrowthProgression._resolve_realm_stages(
                realm=realm,
                stage_definition_map=stage_definition_map,
            )
            realm_rules[realm.realm_id] = RealmGrowthRule(
                realm_id=realm.realm_id,
                realm_name=realm.name,
                total_cultivation=daily_entry.total_cultivation,
                stage_thresholds=CharacterGrowthProgression._build_stage_thresholds(
                    total_cultivation=daily_entry.total_cultivation,
                    ordered_stages=ordered_stages,
                ),
            )
        return realm_rules

    @staticmethod
    def _require_daily_entry(
        *,
        realm_id: str,
        daily_entry_map: dict[str, DailyCultivationEntry],
    ) -> DailyCultivationEntry:
        try:
            return daily_entry_map[realm_id]
        except KeyError as exc:
            raise GrowthRuleNotFoundError(f"缺少大境界标准日修为配置：{realm_id}") from exc

    @staticmethod
    def _resolve_realm_stages(
        *,
        realm: RealmDefinition,
        stage_definition_map: dict[str, RealmStageDefinition],
    ) -> tuple[RealmStageDefinition, ...]:
        ordered_stages: list[RealmStageDefinition] = []
        for stage_id in realm.stage_ids:
            try:
                ordered_stages.append(stage_definition_map[stage_id])
            except KeyError as exc:
                raise GrowthRuleNotFoundError(
                    f"大境界 {realm.realm_id} 引用了未定义的小阶段：{stage_id}"
                ) from exc
        return tuple(sorted(ordered_stages, key=lambda item: item.order))

    @staticmethod
    def _build_stage_thresholds(
        *,
        total_cultivation: int,
        ordered_stages: tuple[RealmStageDefinition, ...],
    ) -> tuple[StageThreshold, ...]:
        total_weight = sum((stage.multiplier for stage in ordered_stages), start=_DECIMAL_ZERO)
        if total_weight <= _DECIMAL_ZERO:
            raise GrowthRuleNotFoundError("小阶段倍率总和必须大于 0")

        accumulated_weight = _DECIMAL_ZERO
        stage_thresholds: list[StageThreshold] = []

        for index, stage in enumerate(ordered_stages):
            if index == 0:
                entry_cultivation = 0
            else:
                ratio = accumulated_weight / total_weight
                entry_cultivation = CharacterGrowthProgression._round_up_threshold(
                    Decimal(total_cultivation) * ratio
                )
                previous_entry = stage_thresholds[-1].entry_cultivation
                entry_cultivation = min(total_cultivation, max(previous_entry, entry_cultivation))

            stage_thresholds.append(
                StageThreshold(
                    stage_id=stage.stage_id,
                    stage_name=stage.name,
                    order=stage.order,
                    entry_cultivation=entry_cultivation,
                )
            )
            accumulated_weight += stage.multiplier

        return tuple(stage_thresholds)

    @staticmethod
    def _round_up_threshold(value: Decimal) -> int:
        """按向上取整计算整数门槛。"""
        return int(value.to_integral_value(rounding=ROUND_CEILING))


def resolve_realm_coefficient(*, static_config: StaticGameConfig, realm_id: str) -> int:
    """读取指定大境界的基准系数。"""
    for entry in static_config.base_coefficients.realm_curve.entries:
        if entry.realm_id == realm_id:
            return entry.coefficient
    raise GrowthRuleNotFoundError(f"缺少大境界基准系数配置：{realm_id}")



def resolve_breakthrough_comprehension_threshold(*, static_config: StaticGameConfig, realm_id: str) -> int:
    """按大境界基准系数推导当前突破感悟门槛。"""
    realm_coefficient = resolve_realm_coefficient(static_config=static_config, realm_id=realm_id)
    curve_input = math.log10(float(realm_coefficient))
    raw_threshold = _BREAKTHROUGH_COMPREHENSION_CURVE_BASE + _BREAKTHROUGH_COMPREHENSION_CURVE_SCALE * math.pow(
        curve_input,
        _BREAKTHROUGH_COMPREHENSION_CURVE_EXPONENT,
    )
    return max(_BREAKTHROUGH_COMPREHENSION_MINIMUM, _round_half_up_to_int(Decimal(str(raw_threshold))))



def resolve_endless_region_total_cultivation(*, static_config: StaticGameConfig, realm_id: str) -> int:
    """按主动高效段目标推导完整区域稳定修为。"""
    daily_entry = _require_daily_entry_from_config(static_config=static_config, realm_id=realm_id)
    active_peak_ratio = _require_source_ratio(
        static_config=static_config,
        realm_id=realm_id,
        source_category="active_peak",
    )
    total_value = Decimal(daily_entry.daily_cultivation) * active_peak_ratio / _ENDLESS_ACTIVE_PEAK_REGION_DIVISOR
    return max(1, _round_half_up_to_int(total_value))



def resolve_endless_region_total_insight(*, static_config: StaticGameConfig, realm_id: str) -> int:
    """按突破感悟门槛推导完整区域稳定感悟上限。"""
    threshold = resolve_breakthrough_comprehension_threshold(static_config=static_config, realm_id=realm_id)
    return max(1, threshold // 10)



def resolve_spirit_stone_economy_multiplier(*, static_config: StaticGameConfig, realm_id: str) -> int:
    """按大境界基准系数推导灵石主曲线倍率。"""
    realm_coefficient = resolve_realm_coefficient(static_config=static_config, realm_id=realm_id)
    return max(1, round(math.pow(float(realm_coefficient), _SPIRIT_STONE_CURVE_EXPONENT)))



def _require_daily_entry_from_config(*, static_config: StaticGameConfig, realm_id: str) -> DailyCultivationEntry:
    for entry in static_config.daily_cultivation.entries:
        if entry.realm_id == realm_id:
            return entry
    raise GrowthRuleNotFoundError(f"缺少大境界标准日修为配置：{realm_id}")



def _require_source_ratio(*, static_config: StaticGameConfig, realm_id: str, source_category: str) -> Decimal:
    for source in static_config.cultivation_sources.sources:
        if source.realm_id == realm_id and source.source_category == source_category:
            return source.ratio
    raise GrowthRuleNotFoundError(f"缺少大境界修炼来源配置：{realm_id}:{source_category}")



def _round_half_up_to_int(value: Decimal) -> int:
    return int(value.quantize(_DECIMAL_INTEGER, rounding=ROUND_HALF_UP))



__all__ = [
    "CharacterGrowthProgression",
    "GrowthRuleNotFoundError",
    "RealmGrowthRule",
    "StageThreshold",
    "resolve_breakthrough_comprehension_threshold",
    "resolve_endless_region_total_cultivation",
    "resolve_endless_region_total_insight",
    "resolve_realm_coefficient",
    "resolve_spirit_stone_economy_multiplier",
]
