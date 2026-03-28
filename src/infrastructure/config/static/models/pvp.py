"""PVP 静态配置模型。"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    DisplayName,
    NonNegativeInt,
    OrderedConfigItem,
    PercentageDecimal,
    PositiveInt,
    ShortText,
    StableId,
    StaticConfigModel,
    VersionedSectionConfig,
)

_ALLOWED_HONOR_COIN_SOURCE_IDS: tuple[str, ...] = ("base", "rank_gap_bonus", "upset_bonus", "streak_bonus", "loss_floor")
_ALLOWED_REWARD_TIERS: tuple[str, ...] = ("top3", "top10", "top50")
_ALLOWED_BOARD_WINDOW_EXPAND_ORDER: tuple[str, ...] = (
    "public_power_tolerance_ratio",
    "hidden_score_tolerance_ratio",
    "rank_window_expand_step",
)
_ALLOWED_SNAPSHOT_REASON_IDS: tuple[str, ...] = (
    "enter_ladder",
    "challenge_start",
    "expired_refresh",
    "build_changed",
    "defense_on_demand",
)


class PvpDailyLimitConfig(StaticConfigModel):
    """PVP 每日次数限制配置。"""

    effective_challenge_limit: PositiveInt
    repeat_target_limit: PositiveInt


class PvpProtectionConfig(StaticConfigModel):
    """PVP 榜单保护配置。"""

    new_entry_protection_hours: PositiveInt
    defense_snapshot_lock_hours: PositiveInt


class PvpDefenseFailureCapEntry(OrderedConfigItem):
    """高名次防守失败上限条目。"""

    rank_start: PositiveInt
    rank_end: PositiveInt
    daily_failure_cap: PositiveInt


class PvpAntiAbuseConfig(StaticConfigModel):
    """PVP 防刷相关配置。"""

    cycle_timezone: ShortText
    defense_failure_caps: tuple[PvpDefenseFailureCapEntry, ...]
    allowed_snapshot_reasons: tuple[StableId, ...]


class PvpTargetPoolConfig(StaticConfigModel):
    """PVP 目标池收敛配置。"""

    rank_window_up: PositiveInt
    rank_window_down: PositiveInt
    max_realm_gap: PositiveInt
    public_power_tolerance_ratio: PercentageDecimal
    hidden_score_tolerance_ratio: PercentageDecimal
    fallback_min_candidate_count: PositiveInt
    fallback_public_power_tolerance_ratio: PercentageDecimal
    fallback_hidden_score_tolerance_ratio: PercentageDecimal
    rank_window_expand_step: PositiveInt
    expansion_order: tuple[StableId, ...]


class PvpHonorCoinComponent(OrderedConfigItem):
    """荣誉币计算组件配置。"""

    component_id: StableId
    delta: int
    summary: ShortText


class PvpHonorCoinConfig(StaticConfigModel):
    """荣誉币基础参数配置。"""

    win_base: PositiveInt
    loss_base: NonNegativeInt
    win_floor: PositiveInt
    loss_floor: NonNegativeInt
    rank_gap_bonus_step: PositiveInt
    rank_gap_bonus_per_step: PositiveInt
    upset_bonus_threshold: PositiveInt
    upset_bonus: PositiveInt
    streak_bonus_trigger: PositiveInt
    streak_bonus: PositiveInt
    components: tuple[PvpHonorCoinComponent, ...]


class PvpRewardTierDefinition(OrderedConfigItem):
    """PVP 展示奖励档位定义。"""

    reward_tier_id: StableId
    rank_start: PositiveInt
    rank_end: PositiveInt
    summary: ShortText


class PvpRewardPreviewConfig(StaticConfigModel):
    """PVP 奖励预览配置。"""

    default_tier_id: StableId
    reward_tiers: tuple[PvpRewardTierDefinition, ...]


class PvpConfig(VersionedSectionConfig):
    """PVP 静态配置。"""

    daily_limit: PvpDailyLimitConfig
    protection: PvpProtectionConfig
    anti_abuse: PvpAntiAbuseConfig
    target_pool: PvpTargetPoolConfig
    honor_coin: PvpHonorCoinConfig
    reward_preview: PvpRewardPreviewConfig

    @property
    def ordered_defense_failure_caps(self) -> tuple[PvpDefenseFailureCapEntry, ...]:
        """按顺序返回高名次防守失败上限条目。"""
        return tuple(sorted(self.anti_abuse.defense_failure_caps, key=lambda item: item.order))

    @property
    def ordered_reward_tiers(self) -> tuple[PvpRewardTierDefinition, ...]:
        """按顺序返回奖励档位条目。"""
        return tuple(sorted(self.reward_preview.reward_tiers, key=lambda item: item.order))

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集 PVP 配置中的结构与边界错误。"""
        self._collect_daily_limit_issues(filename=filename, collector=collector)
        self._collect_anti_abuse_issues(filename=filename, collector=collector)
        self._collect_target_pool_issues(filename=filename, collector=collector)
        self._collect_honor_coin_issues(filename=filename, collector=collector)
        self._collect_reward_preview_issues(filename=filename, collector=collector)

    def _collect_daily_limit_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        if self.daily_limit.effective_challenge_limit != 6:
            collector.add(
                filename=filename,
                config_path="daily_limit.effective_challenge_limit",
                identifier="effective_challenge_limit",
                reason="阶段 11 联调后每日有效挑战次数必须固定为 6 次",
            )
        if self.daily_limit.repeat_target_limit != 3:
            collector.add(
                filename=filename,
                config_path="daily_limit.repeat_target_limit",
                identifier="repeat_target_limit",
                reason="阶段 11 联调后同目标重复挑战上限必须固定为 3 次",
            )
        if self.protection.new_entry_protection_hours != 6:
            collector.add(
                filename=filename,
                config_path="protection.new_entry_protection_hours",
                identifier="new_entry_protection_hours",
                reason="首发阶段 9 新入榜保护时间必须固定为 6 小时",
            )
        if self.protection.defense_snapshot_lock_hours != 24:
            collector.add(
                filename=filename,
                config_path="protection.defense_snapshot_lock_hours",
                identifier="defense_snapshot_lock_hours",
                reason="首发阶段 9 防守快照锁定周期必须固定为 24 小时",
            )

    def _collect_anti_abuse_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        anti_abuse = self.anti_abuse
        ordered_caps = self.ordered_defense_failure_caps
        if anti_abuse.cycle_timezone != "Asia/Shanghai":
            collector.add(
                filename=filename,
                config_path="anti_abuse.cycle_timezone",
                identifier="cycle_timezone",
                reason="PVP 自然日锚点时区必须固定为 Asia/Shanghai",
            )

        cap_orders = Counter(item.order for item in anti_abuse.defense_failure_caps)
        rank_starts = Counter(item.rank_start for item in anti_abuse.defense_failure_caps)
        last_rank_end = 0
        for entry in ordered_caps:
            if cap_orders[entry.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="anti_abuse.defense_failure_caps[].order",
                    identifier=entry.name,
                    reason=f"高名次防守失败上限顺序值 {entry.order} 重复",
                )
            if rank_starts[entry.rank_start] > 1:
                collector.add(
                    filename=filename,
                    config_path="anti_abuse.defense_failure_caps[].rank_start",
                    identifier=entry.name,
                    reason="高名次防守失败上限起始名次重复",
                )
            if entry.rank_end < entry.rank_start:
                collector.add(
                    filename=filename,
                    config_path="anti_abuse.defense_failure_caps[].rank_end",
                    identifier=entry.name,
                    reason="高名次防守失败上限的结束名次不能小于起始名次",
                )
            if entry.rank_start != last_rank_end + 1:
                collector.add(
                    filename=filename,
                    config_path="anti_abuse.defense_failure_caps",
                    identifier=entry.name,
                    reason="高名次防守失败上限必须按连续名次区间声明",
                )
            last_rank_end = entry.rank_end

        if tuple(reason for reason in anti_abuse.allowed_snapshot_reasons) != _ALLOWED_SNAPSHOT_REASON_IDS:
            collector.add(
                filename=filename,
                config_path="anti_abuse.allowed_snapshot_reasons",
                identifier="allowed_snapshot_reasons",
                reason="首发阶段 9 快照抓取原因集合必须与设计约束保持一致",
            )

    def _collect_target_pool_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        target_pool = self.target_pool
        if target_pool.rank_window_up != 5:
            collector.add(
                filename=filename,
                config_path="target_pool.rank_window_up",
                identifier="rank_window_up",
                reason="首发阶段 9 目标池向前榜位窗口必须固定为 5",
            )
        if target_pool.rank_window_down != 3:
            collector.add(
                filename=filename,
                config_path="target_pool.rank_window_down",
                identifier="rank_window_down",
                reason="首发阶段 9 目标池向后榜位窗口必须固定为 3",
            )
        if target_pool.max_realm_gap != 1:
            collector.add(
                filename=filename,
                config_path="target_pool.max_realm_gap",
                identifier="max_realm_gap",
                reason="首发阶段 9 目标池大境界差距上限必须固定为 1",
            )
        if target_pool.public_power_tolerance_ratio != Decimal("0.25"):
            collector.add(
                filename=filename,
                config_path="target_pool.public_power_tolerance_ratio",
                identifier="public_power_tolerance_ratio",
                reason="首发阶段 9 目标池公开评分差值上限必须固定为 25%",
            )
        if target_pool.hidden_score_tolerance_ratio != Decimal("0.18"):
            collector.add(
                filename=filename,
                config_path="target_pool.hidden_score_tolerance_ratio",
                identifier="hidden_score_tolerance_ratio",
                reason="首发阶段 9 目标池隐藏评分差值上限必须固定为 18%",
            )
        if target_pool.fallback_public_power_tolerance_ratio != Decimal("0.35"):
            collector.add(
                filename=filename,
                config_path="target_pool.fallback_public_power_tolerance_ratio",
                identifier="fallback_public_power_tolerance_ratio",
                reason="首发阶段 9 目标池回退公开评分差值上限必须固定为 35%",
            )
        if target_pool.fallback_hidden_score_tolerance_ratio != Decimal("0.25"):
            collector.add(
                filename=filename,
                config_path="target_pool.fallback_hidden_score_tolerance_ratio",
                identifier="fallback_hidden_score_tolerance_ratio",
                reason="首发阶段 9 目标池回退隐藏评分差值上限必须固定为 25%",
            )
        if target_pool.rank_window_expand_step != 2:
            collector.add(
                filename=filename,
                config_path="target_pool.rank_window_expand_step",
                identifier="rank_window_expand_step",
                reason="首发阶段 9 目标池榜位扩窗步长必须固定为 2",
            )
        if target_pool.fallback_min_candidate_count < 3:
            collector.add(
                filename=filename,
                config_path="target_pool.fallback_min_candidate_count",
                identifier="fallback_min_candidate_count",
                reason="目标池回退前的最小候选数量不能小于 3",
            )
        if tuple(item for item in target_pool.expansion_order) != _ALLOWED_BOARD_WINDOW_EXPAND_ORDER:
            collector.add(
                filename=filename,
                config_path="target_pool.expansion_order",
                identifier="expansion_order",
                reason="目标池回退顺序必须与设计约束保持一致",
            )

    def _collect_honor_coin_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        components = self.honor_coin.components
        component_order_counter = Counter(item.order for item in components)
        component_id_counter = Counter(item.component_id for item in components)
        component_ids = tuple(item.component_id for item in sorted(components, key=lambda item: item.order))
        if component_ids != _ALLOWED_HONOR_COIN_SOURCE_IDS:
            collector.add(
                filename=filename,
                config_path="honor_coin.components",
                identifier="component_sequence",
                reason="荣誉币计算组件顺序必须与首发设计约束保持一致",
            )

        for component in components:
            if component_order_counter[component.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="honor_coin.components[].order",
                    identifier=component.component_id,
                    reason=f"荣誉币组件顺序值 {component.order} 重复",
                )
            if component_id_counter[component.component_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="honor_coin.components[].component_id",
                    identifier=component.component_id,
                    reason="荣誉币组件标识重复",
                )

        if self.honor_coin.win_floor > self.honor_coin.win_base:
            collector.add(
                filename=filename,
                config_path="honor_coin.win_floor",
                identifier="win_floor",
                reason="胜利保底值不能高于胜利基础值",
            )
        if self.honor_coin.loss_floor > self.honor_coin.loss_base:
            collector.add(
                filename=filename,
                config_path="honor_coin.loss_floor",
                identifier="loss_floor",
                reason="失败保底值不能高于失败基础值",
            )

    def _collect_reward_preview_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        reward_preview = self.reward_preview
        ordered_tiers = self.ordered_reward_tiers
        tier_orders = Counter(item.order for item in reward_preview.reward_tiers)
        tier_ids = Counter(item.reward_tier_id for item in reward_preview.reward_tiers)
        declared_tier_ids = tuple(item.reward_tier_id for item in ordered_tiers)
        if declared_tier_ids != _ALLOWED_REWARD_TIERS:
            collector.add(
                filename=filename,
                config_path="reward_preview.reward_tiers",
                identifier="reward_tier_sequence",
                reason="奖励档位顺序必须固定为 top3、top10、top50",
            )
        if reward_preview.default_tier_id not in declared_tier_ids:
            collector.add(
                filename=filename,
                config_path="reward_preview.default_tier_id",
                identifier="default_tier_id",
                reason="默认奖励档位必须引用已声明的档位标识",
            )

        last_rank_end = 0
        for tier in ordered_tiers:
            if tier_orders[tier.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="reward_preview.reward_tiers[].order",
                    identifier=tier.reward_tier_id,
                    reason=f"奖励档位顺序值 {tier.order} 重复",
                )
            if tier_ids[tier.reward_tier_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="reward_preview.reward_tiers[].reward_tier_id",
                    identifier=tier.reward_tier_id,
                    reason="奖励档位标识重复",
                )
            if tier.rank_end < tier.rank_start:
                collector.add(
                    filename=filename,
                    config_path="reward_preview.reward_tiers[].rank_end",
                    identifier=tier.reward_tier_id,
                    reason="奖励档位的结束名次不能小于起始名次",
                )
            if tier.rank_start != last_rank_end + 1:
                collector.add(
                    filename=filename,
                    config_path="reward_preview.reward_tiers",
                    identifier=tier.reward_tier_id,
                    reason="奖励档位必须按连续名次区间声明",
                )
            last_rank_end = tier.rank_end


__all__ = [
    "PvpAntiAbuseConfig",
    "PvpConfig",
    "PvpDailyLimitConfig",
    "PvpDefenseFailureCapEntry",
    "PvpHonorCoinComponent",
    "PvpHonorCoinConfig",
    "PvpProtectionConfig",
    "PvpRewardPreviewConfig",
    "PvpRewardTierDefinition",
    "PvpTargetPoolConfig",
]
