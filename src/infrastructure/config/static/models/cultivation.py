"""修为与基础系数配置模型。"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    LAUNCH_REALM_IDS,
    NonNegativeDecimal,
    PositiveDecimal,
    PositiveInt,
    RealmScopedConfigItem,
    ShortText,
    StableId,
    VersionedSectionConfig,
)

_DECIMAL_ONE = Decimal("1")
EXPECTED_SOURCE_CATEGORIES: tuple[str, ...] = (
    "closed_door",
    "active_peak",
    "active_regular",
    "active_tail",
)


class DailyCultivationEntry(RealmScopedConfigItem):
    """单个大境界的标准日修为映射。"""

    standard_days: PositiveDecimal
    daily_cultivation: PositiveInt
    total_cultivation: PositiveInt


class DailyCultivationConfig(VersionedSectionConfig):
    """标准日修为映射配置。"""

    entries: tuple[DailyCultivationEntry, ...]

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构与数值错误。"""
        ordered_entries = tuple(sorted(self.entries, key=lambda item: item.order))
        realm_ids = tuple(entry.realm_id for entry in ordered_entries)
        order_counter = Counter(entry.order for entry in self.entries)
        id_counter = Counter(entry.realm_id for entry in self.entries)

        if realm_ids != LAUNCH_REALM_IDS:
            collector.add(
                filename=filename,
                config_path="entries",
                identifier="realm_sequence",
                reason="标准日修为映射必须完整覆盖凡人到渡劫，且顺序不可变",
            )

        for entry in self.entries:
            if order_counter[entry.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="entries[].order",
                    identifier=entry.realm_id,
                    reason=f"标准日修为顺序值 {entry.order} 重复",
                )
            if id_counter[entry.realm_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="entries[].realm_id",
                    identifier=entry.realm_id,
                    reason="标准日修为映射的大境界标识重复",
                )
            expected_total = int(entry.standard_days * Decimal(entry.daily_cultivation))
            if entry.total_cultivation != expected_total:
                collector.add(
                    filename=filename,
                    config_path="entries[].total_cultivation",
                    identifier=entry.realm_id,
                    reason=(
                        "标准日数、单日修为、总修为不自洽，"
                        f"期望值为 {expected_total}"
                    ),
                )


class BaseScalarCoefficient(VersionedSectionConfig):
    """基础标量系数配置。"""

    base_hp: PositiveInt
    base_attack: PositiveInt
    base_defense: PositiveInt
    base_speed: PositiveInt
    crit_rate_cap: PositiveDecimal
    dodge_rate_cap: PositiveDecimal
    control_rate_cap: PositiveDecimal
    damage_reduction_cap: PositiveDecimal
    penetration_cap: PositiveDecimal
    lifesteal_cap: PositiveDecimal
    extra_action_cap_per_round: PositiveInt
    counterattack_cap_per_round: PositiveInt

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集基础数值系数的边界错误。"""
        self._check_cap(
            collector=collector,
            filename=filename,
            config_path="crit_rate_cap",
            identifier="crit_rate_cap",
            value=self.crit_rate_cap,
        )
        self._check_cap(
            collector=collector,
            filename=filename,
            config_path="dodge_rate_cap",
            identifier="dodge_rate_cap",
            value=self.dodge_rate_cap,
        )
        self._check_cap(
            collector=collector,
            filename=filename,
            config_path="control_rate_cap",
            identifier="control_rate_cap",
            value=self.control_rate_cap,
        )
        self._check_cap(
            collector=collector,
            filename=filename,
            config_path="damage_reduction_cap",
            identifier="damage_reduction_cap",
            value=self.damage_reduction_cap,
        )
        self._check_cap(
            collector=collector,
            filename=filename,
            config_path="penetration_cap",
            identifier="penetration_cap",
            value=self.penetration_cap,
        )
        self._check_cap(
            collector=collector,
            filename=filename,
            config_path="lifesteal_cap",
            identifier="lifesteal_cap",
            value=self.lifesteal_cap,
        )

    def _check_cap(
        self,
        *,
        collector: StaticConfigIssueCollector,
        filename: str,
        config_path: str,
        identifier: str,
        value: Decimal,
    ) -> None:
        if value > _DECIMAL_ONE:
            collector.add(
                filename=filename,
                config_path=config_path,
                identifier=identifier,
                reason="上限类参数不能大于 1",
            )


class RealmCoefficientEntry(RealmScopedConfigItem):
    """按大境界配置的基准系数。"""

    coefficient: PositiveInt


class RealmCoefficientConfig(VersionedSectionConfig):
    """大境界基准系数表。"""

    entries: tuple[RealmCoefficientEntry, ...]

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前系数表的结构错误。"""
        ordered_entries = tuple(sorted(self.entries, key=lambda item: item.order))
        realm_ids = tuple(entry.realm_id for entry in ordered_entries)
        order_counter = Counter(entry.order for entry in self.entries)
        id_counter = Counter(entry.realm_id for entry in self.entries)

        if realm_ids != LAUNCH_REALM_IDS:
            collector.add(
                filename=filename,
                config_path="entries",
                identifier="realm_sequence",
                reason="基准系数表必须完整覆盖凡人到渡劫，且顺序不可变",
            )

        for entry in self.entries:
            if order_counter[entry.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="entries[].order",
                    identifier=entry.realm_id,
                    reason=f"基准系数顺序值 {entry.order} 重复",
                )
            if id_counter[entry.realm_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="entries[].realm_id",
                    identifier=entry.realm_id,
                    reason="基准系数的大境界标识重复",
                )


class ClosedDoorYieldConfig(VersionedSectionConfig):
    """闭关附加产出边界。"""

    insight_gain_ratio: NonNegativeDecimal
    spirit_stone_gain_ratio: NonNegativeDecimal
    max_days_per_claim: PositiveInt
    allow_breakthrough_resource_drop: bool

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集闭关产出边界错误。"""
        if self.allow_breakthrough_resource_drop:
            collector.add(
                filename=filename,
                config_path="allow_breakthrough_resource_drop",
                identifier="closed_door_reward_boundary",
                reason="闭关修炼不得产出关键突破资源",
            )


class CultivationSourceEntry(RealmScopedConfigItem):
    """单个大境界的修为来源占比。"""

    ratio: PositiveDecimal
    source_category: StableId
    description: ShortText


class CultivationSourceConfig(VersionedSectionConfig):
    """修为来源占比配置。"""

    sources: tuple[CultivationSourceEntry, ...]
    closed_door_yield: ClosedDoorYieldConfig

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集占比与边界错误。"""
        self.closed_door_yield.collect_issues(filename=filename, collector=collector)
        grouped_ratios: dict[str, Decimal] = {realm_id: Decimal("0") for realm_id in LAUNCH_REALM_IDS}
        seen_pairs: set[tuple[str, str]] = set()

        for source in self.sources:
            if source.realm_id not in grouped_ratios:
                collector.add(
                    filename=filename,
                    config_path="sources[].realm_id",
                    identifier=source.realm_id,
                    reason="修为来源引用了未开放的大境界",
                )
                continue
            pair = (source.realm_id, source.source_category)
            if pair in seen_pairs:
                collector.add(
                    filename=filename,
                    config_path="sources[].source_category",
                    identifier=f"{source.realm_id}:{source.source_category}",
                    reason="同一大境界下的修为来源分类重复",
                )
            seen_pairs.add(pair)
            grouped_ratios[source.realm_id] += source.ratio

        for realm_id, total_ratio in grouped_ratios.items():
            if total_ratio != _DECIMAL_ONE:
                collector.add(
                    filename=filename,
                    config_path="sources",
                    identifier=realm_id,
                    reason=f"修为来源占比之和必须等于 1，当前为 {total_ratio}",
                )


class BaseCoefficientConfig(VersionedSectionConfig):
    """基础系数聚合配置。"""

    scalar: BaseScalarCoefficient
    realm_curve: RealmCoefficientConfig

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集基础系数聚合配置错误。"""
        self.scalar.collect_issues(filename=filename, collector=collector)
        self.realm_curve.collect_issues(filename=filename, collector=collector)
