"""功法静态配置模型。"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    LAUNCH_REALM_IDS,
    OrderedConfigItem,
    PercentageDecimal,
    PositiveInt,
    ShortText,
    StableId,
    StaticConfigModel,
    VersionedSectionConfig,
)

LAUNCH_SKILL_AXIS_IDS: tuple[str, ...] = ("sword", "body", "spell")
LAUNCH_SKILL_PATH_IDS: tuple[str, ...] = (
    "wenxin_sword",
    "zhanqing_sword",
    "manhuang_body",
    "changsheng_body",
    "qingyun_spell",
    "wangchuan_spell",
)
LAUNCH_SKILL_TYPE_IDS: tuple[str, ...] = ("main", "auxiliary")
LAUNCH_SKILL_AUXILIARY_SLOT_IDS: tuple[str, ...] = ("guard", "movement", "spirit")
LAUNCH_SKILL_QUALITY_IDS: tuple[str, ...] = ("ordinary", "good", "superior", "rare", "perfect")
LAUNCH_SKILL_DROP_POOL_IDS: tuple[str, ...] = (
    "launch_main_pool",
    "launch_guard_pool",
    "launch_movement_pool",
    "launch_spirit_pool",
)


class SkillAxisDefinition(OrderedConfigItem):
    """功法主轴定义。"""

    axis_id: StableId
    combat_identity: ShortText
    focus_summary: ShortText


class SkillPathDefinition(OrderedConfigItem):
    """功法流派定义，同时绑定基础行为模板。"""

    path_id: StableId
    axis_id: StableId
    template_id: StableId
    combat_identity: ShortText
    preferred_scene: ShortText


class SkillPathConfig(VersionedSectionConfig):
    """功法主轴与流派绑定配置。"""

    axes: tuple[SkillAxisDefinition, ...]
    paths: tuple[SkillPathDefinition, ...]

    @property
    def ordered_axes(self) -> tuple[SkillAxisDefinition, ...]:
        """按顺序返回全部主轴。"""
        return tuple(sorted(self.axes, key=lambda item: item.order))

    @property
    def ordered_paths(self) -> tuple[SkillPathDefinition, ...]:
        """按顺序返回全部流派。"""
        return tuple(sorted(self.paths, key=lambda item: item.order))

    def get_axis(self, axis_id: str) -> SkillAxisDefinition | None:
        """读取指定主轴定义。"""
        for axis in self.axes:
            if axis.axis_id == axis_id:
                return axis
        return None

    def get_path(self, path_id: str) -> SkillPathDefinition | None:
        """读取指定流派定义。"""
        for path in self.paths:
            if path.path_id == path_id:
                return path
        return None

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构错误。"""
        ordered_axes = self.ordered_axes
        ordered_paths = self.ordered_paths
        axis_ids = tuple(axis.axis_id for axis in ordered_axes)
        path_ids = tuple(path.path_id for path in ordered_paths)
        axis_order_counter = Counter(axis.order for axis in self.axes)
        axis_id_counter = Counter(axis.axis_id for axis in self.axes)
        path_order_counter = Counter(path.order for path in self.paths)
        path_id_counter = Counter(path.path_id for path in self.paths)
        template_id_counter = Counter(path.template_id for path in self.paths)
        axis_to_paths: dict[str, list[str]] = defaultdict(list)

        if axis_ids != LAUNCH_SKILL_AXIS_IDS:
            collector.add(
                filename=filename,
                config_path="axes",
                identifier="axis_sequence",
                reason="主轴必须固定为 sword、body、spell 三条",
            )
        if path_ids != LAUNCH_SKILL_PATH_IDS:
            collector.add(
                filename=filename,
                config_path="paths",
                identifier="path_sequence",
                reason="流派必须固定为首发六条预设路径，且顺序不可变",
            )

        for axis in self.axes:
            if axis_order_counter[axis.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="axes[].order",
                    identifier=axis.axis_id,
                    reason=f"主轴顺序值 {axis.order} 重复",
                )
            if axis_id_counter[axis.axis_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="axes[].axis_id",
                    identifier=axis.axis_id,
                    reason="主轴标识重复",
                )

        known_axis_ids = set(LAUNCH_SKILL_AXIS_IDS)
        for path in self.paths:
            if path_order_counter[path.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="paths[].order",
                    identifier=path.path_id,
                    reason=f"流派顺序值 {path.order} 重复",
                )
            if path_id_counter[path.path_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="paths[].path_id",
                    identifier=path.path_id,
                    reason="流派标识重复",
                )
            if template_id_counter[path.template_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="paths[].template_id",
                    identifier=path.template_id,
                    reason="行为模板绑定标识重复",
                )
            if path.axis_id not in known_axis_ids:
                collector.add(
                    filename=filename,
                    config_path="paths[].axis_id",
                    identifier=path.path_id,
                    reason=f"流派引用了未定义主轴 {path.axis_id}",
                )
                continue
            axis_to_paths[path.axis_id].append(path.path_id)

        for axis_id in LAUNCH_SKILL_AXIS_IDS:
            if len(axis_to_paths[axis_id]) != 2:
                collector.add(
                    filename=filename,
                    config_path="paths",
                    identifier=axis_id,
                    reason="每条主轴必须且只能归属两个流派",
                )


class SkillRankDefinition(OrderedConfigItem):
    """功法阶数与预算区间定义。"""

    rank_id: StableId
    main_budget_min: PositiveInt
    main_budget_max: PositiveInt
    auxiliary_budget_min: PositiveInt
    auxiliary_budget_max: PositiveInt


class SkillQualityDefinition(OrderedConfigItem):
    """功法品质与预算加算定义。"""

    quality_id: StableId
    budget_bonus: int


class SkillAuxiliarySlotDefinition(OrderedConfigItem):
    """辅助功法槽位定义。"""

    slot_id: StableId
    summary: ShortText


class SkillAttributePoolDefinition(OrderedConfigItem):
    """功法属性池定义。"""

    pool_id: StableId
    stat_ids: tuple[StableId, ...]
    summary: ShortText


class SkillPatchDefinition(OrderedConfigItem):
    """单条功法补丁定义。"""

    patch_id: StableId
    patch_kind: StableId
    target_key: StableId
    operation: StableId
    value: int
    summary: ShortText


class SkillPatchPoolDefinition(OrderedConfigItem):
    """功法补丁池定义。"""

    pool_id: StableId
    patch_ids: tuple[StableId, ...]
    summary: ShortText


class SkillGenerationConfig(VersionedSectionConfig):
    """功法生成所需的阶数、品质、属性池与补丁池配置。"""

    ranks: tuple[SkillRankDefinition, ...]
    qualities: tuple[SkillQualityDefinition, ...]
    auxiliary_slots: tuple[SkillAuxiliarySlotDefinition, ...]
    attribute_pools: tuple[SkillAttributePoolDefinition, ...]
    patches: tuple[SkillPatchDefinition, ...]
    patch_pools: tuple[SkillPatchPoolDefinition, ...]

    @property
    def ordered_ranks(self) -> tuple[SkillRankDefinition, ...]:
        """按顺序返回功法阶数定义。"""
        return tuple(sorted(self.ranks, key=lambda item: item.order))

    @property
    def ordered_qualities(self) -> tuple[SkillQualityDefinition, ...]:
        """按顺序返回功法品质定义。"""
        return tuple(sorted(self.qualities, key=lambda item: item.order))

    @property
    def ordered_auxiliary_slots(self) -> tuple[SkillAuxiliarySlotDefinition, ...]:
        """按顺序返回辅助槽位定义。"""
        return tuple(sorted(self.auxiliary_slots, key=lambda item: item.order))

    @property
    def ordered_attribute_pools(self) -> tuple[SkillAttributePoolDefinition, ...]:
        """按顺序返回属性池定义。"""
        return tuple(sorted(self.attribute_pools, key=lambda item: item.order))

    @property
    def ordered_patches(self) -> tuple[SkillPatchDefinition, ...]:
        """按顺序返回补丁定义。"""
        return tuple(sorted(self.patches, key=lambda item: item.order))

    @property
    def ordered_patch_pools(self) -> tuple[SkillPatchPoolDefinition, ...]:
        """按顺序返回补丁池定义。"""
        return tuple(sorted(self.patch_pools, key=lambda item: item.order))

    def get_rank(self, rank_id: str) -> SkillRankDefinition | None:
        """读取指定功法阶数。"""
        for rank in self.ranks:
            if rank.rank_id == rank_id:
                return rank
        return None

    def get_quality(self, quality_id: str) -> SkillQualityDefinition | None:
        """读取指定功法品质。"""
        for quality in self.qualities:
            if quality.quality_id == quality_id:
                return quality
        return None

    def get_auxiliary_slot(self, slot_id: str) -> SkillAuxiliarySlotDefinition | None:
        """读取指定辅助槽位。"""
        for slot in self.auxiliary_slots:
            if slot.slot_id == slot_id:
                return slot
        return None

    def get_attribute_pool(self, pool_id: str) -> SkillAttributePoolDefinition | None:
        """读取指定属性池。"""
        for pool in self.attribute_pools:
            if pool.pool_id == pool_id:
                return pool
        return None

    def get_patch(self, patch_id: str) -> SkillPatchDefinition | None:
        """读取指定补丁定义。"""
        for patch in self.patches:
            if patch.patch_id == patch_id:
                return patch
        return None

    def get_patch_pool(self, pool_id: str) -> SkillPatchPoolDefinition | None:
        """读取指定补丁池。"""
        for patch_pool in self.patch_pools:
            if patch_pool.pool_id == pool_id:
                return patch_pool
        return None

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构错误。"""
        self._collect_ranks(filename=filename, collector=collector)
        self._collect_qualities(filename=filename, collector=collector)
        self._collect_auxiliary_slots(filename=filename, collector=collector)
        self._collect_attribute_pools(filename=filename, collector=collector)
        self._collect_patches(filename=filename, collector=collector)
        self._collect_patch_pools(filename=filename, collector=collector)

    def _collect_ranks(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_ranks = self.ordered_ranks
        rank_ids = tuple(rank.rank_id for rank in ordered_ranks)
        rank_order_counter = Counter(rank.order for rank in self.ranks)
        rank_id_counter = Counter(rank.rank_id for rank in self.ranks)

        if rank_ids != LAUNCH_REALM_IDS:
            collector.add(
                filename=filename,
                config_path="ranks",
                identifier="rank_sequence",
                reason="功法阶数必须与首发十个大境界一一对应，且顺序不可变",
            )

        for rank in self.ranks:
            if rank_order_counter[rank.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="ranks[].order",
                    identifier=rank.rank_id,
                    reason=f"功法阶数顺序值 {rank.order} 重复",
                )
            if rank_id_counter[rank.rank_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="ranks[].rank_id",
                    identifier=rank.rank_id,
                    reason="功法阶数标识重复",
                )
            if rank.main_budget_min > rank.main_budget_max:
                collector.add(
                    filename=filename,
                    config_path="ranks[].main_budget_min",
                    identifier=rank.rank_id,
                    reason="主修预算下限不能大于上限",
                )
            if rank.auxiliary_budget_min > rank.auxiliary_budget_max:
                collector.add(
                    filename=filename,
                    config_path="ranks[].auxiliary_budget_min",
                    identifier=rank.rank_id,
                    reason="辅助预算下限不能大于上限",
                )

    def _collect_qualities(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_qualities = self.ordered_qualities
        quality_ids = tuple(quality.quality_id for quality in ordered_qualities)
        quality_order_counter = Counter(quality.order for quality in self.qualities)
        quality_id_counter = Counter(quality.quality_id for quality in self.qualities)

        if quality_ids != LAUNCH_SKILL_QUALITY_IDS:
            collector.add(
                filename=filename,
                config_path="qualities",
                identifier="quality_sequence",
                reason="功法品质必须固定为 ordinary、good、superior、rare、perfect 五档",
            )

        for quality in self.qualities:
            if quality_order_counter[quality.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="qualities[].order",
                    identifier=quality.quality_id,
                    reason=f"功法品质顺序值 {quality.order} 重复",
                )
            if quality_id_counter[quality.quality_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="qualities[].quality_id",
                    identifier=quality.quality_id,
                    reason="功法品质标识重复",
                )

    def _collect_auxiliary_slots(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        ordered_slots = self.ordered_auxiliary_slots
        slot_ids = tuple(slot.slot_id for slot in ordered_slots)
        slot_order_counter = Counter(slot.order for slot in self.auxiliary_slots)
        slot_id_counter = Counter(slot.slot_id for slot in self.auxiliary_slots)

        if slot_ids != LAUNCH_SKILL_AUXILIARY_SLOT_IDS:
            collector.add(
                filename=filename,
                config_path="auxiliary_slots",
                identifier="auxiliary_slot_sequence",
                reason="辅助槽位必须固定为 guard、movement、spirit 三类",
            )

        for slot in self.auxiliary_slots:
            if slot_order_counter[slot.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="auxiliary_slots[].order",
                    identifier=slot.slot_id,
                    reason=f"辅助槽位顺序值 {slot.order} 重复",
                )
            if slot_id_counter[slot.slot_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="auxiliary_slots[].slot_id",
                    identifier=slot.slot_id,
                    reason="辅助槽位标识重复",
                )

    def _collect_attribute_pools(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        pool_order_counter = Counter(pool.order for pool in self.attribute_pools)
        pool_id_counter = Counter(pool.pool_id for pool in self.attribute_pools)

        for pool in self.attribute_pools:
            if pool_order_counter[pool.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="attribute_pools[].order",
                    identifier=pool.pool_id,
                    reason=f"属性池顺序值 {pool.order} 重复",
                )
            if pool_id_counter[pool.pool_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="attribute_pools[].pool_id",
                    identifier=pool.pool_id,
                    reason="属性池标识重复",
                )
            if not pool.stat_ids:
                collector.add(
                    filename=filename,
                    config_path="attribute_pools[].stat_ids",
                    identifier=pool.pool_id,
                    reason="属性池至少需要声明一个属性标识",
                )
            if len(pool.stat_ids) != len(set(pool.stat_ids)):
                collector.add(
                    filename=filename,
                    config_path="attribute_pools[].stat_ids",
                    identifier=pool.pool_id,
                    reason="属性池中存在重复属性标识",
                )

    def _collect_patches(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        patch_order_counter = Counter(patch.order for patch in self.patches)
        patch_id_counter = Counter(patch.patch_id for patch in self.patches)

        for patch in self.patches:
            if patch_order_counter[patch.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="patches[].order",
                    identifier=patch.patch_id,
                    reason=f"补丁顺序值 {patch.order} 重复",
                )
            if patch_id_counter[patch.patch_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="patches[].patch_id",
                    identifier=patch.patch_id,
                    reason="补丁标识重复",
                )

    def _collect_patch_pools(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        patch_ids = {patch.patch_id for patch in self.patches}
        pool_order_counter = Counter(pool.order for pool in self.patch_pools)
        pool_id_counter = Counter(pool.pool_id for pool in self.patch_pools)

        for pool in self.patch_pools:
            if pool_order_counter[pool.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="patch_pools[].order",
                    identifier=pool.pool_id,
                    reason=f"补丁池顺序值 {pool.order} 重复",
                )
            if pool_id_counter[pool.pool_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="patch_pools[].pool_id",
                    identifier=pool.pool_id,
                    reason="补丁池标识重复",
                )
            if not pool.patch_ids:
                collector.add(
                    filename=filename,
                    config_path="patch_pools[].patch_ids",
                    identifier=pool.pool_id,
                    reason="补丁池至少需要声明一个补丁标识",
                )
            if len(pool.patch_ids) != len(set(pool.patch_ids)):
                collector.add(
                    filename=filename,
                    config_path="patch_pools[].patch_ids",
                    identifier=pool.pool_id,
                    reason="补丁池中存在重复补丁标识",
                )
            for patch_id in pool.patch_ids:
                if patch_id not in patch_ids:
                    collector.add(
                        filename=filename,
                        config_path="patch_pools[].patch_ids",
                        identifier=pool.pool_id,
                        reason=f"补丁池引用了未定义补丁 {patch_id}",
                    )


class SkillLineageDefinition(OrderedConfigItem):
    """功法谱系定义。"""

    lineage_id: StableId
    path_id: StableId
    skill_type: StableId
    auxiliary_slot_id: StableId | None = None
    attribute_pool_id: StableId
    patch_pool_ids: tuple[StableId, ...]
    summary: ShortText


class SkillLineageConfig(VersionedSectionConfig):
    """功法谱系配置。"""

    lineages: tuple[SkillLineageDefinition, ...]

    @property
    def ordered_lineages(self) -> tuple[SkillLineageDefinition, ...]:
        """按顺序返回功法谱系。"""
        return tuple(sorted(self.lineages, key=lambda item: item.order))

    def get_lineage(self, lineage_id: str) -> SkillLineageDefinition | None:
        """读取指定功法谱系。"""
        for lineage in self.lineages:
            if lineage.lineage_id == lineage_id:
                return lineage
        return None

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构错误。"""
        lineage_order_counter = Counter(lineage.order for lineage in self.lineages)
        lineage_id_counter = Counter(lineage.lineage_id for lineage in self.lineages)
        main_count = 0
        auxiliary_count = 0
        path_to_main_count: dict[str, int] = defaultdict(int)
        path_to_auxiliary_slots: dict[str, list[str]] = defaultdict(list)

        for lineage in self.lineages:
            if lineage_order_counter[lineage.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="lineages[].order",
                    identifier=lineage.lineage_id,
                    reason=f"功法谱系顺序值 {lineage.order} 重复",
                )
            if lineage_id_counter[lineage.lineage_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="lineages[].lineage_id",
                    identifier=lineage.lineage_id,
                    reason="功法谱系标识重复",
                )
            if lineage.path_id not in LAUNCH_SKILL_PATH_IDS:
                collector.add(
                    filename=filename,
                    config_path="lineages[].path_id",
                    identifier=lineage.lineage_id,
                    reason=f"功法谱系引用了未定义流派 {lineage.path_id}",
                )
            if lineage.skill_type not in LAUNCH_SKILL_TYPE_IDS:
                collector.add(
                    filename=filename,
                    config_path="lineages[].skill_type",
                    identifier=lineage.lineage_id,
                    reason=f"功法类型 {lineage.skill_type} 不在首发允许范围内",
                )
                continue

            if lineage.skill_type == "main":
                main_count += 1
                path_to_main_count[lineage.path_id] += 1
                if lineage.auxiliary_slot_id is not None:
                    collector.add(
                        filename=filename,
                        config_path="lineages[].auxiliary_slot_id",
                        identifier=lineage.lineage_id,
                        reason="主修功法不能声明辅助槽位类型",
                    )
                continue

            auxiliary_count += 1
            if lineage.auxiliary_slot_id not in LAUNCH_SKILL_AUXILIARY_SLOT_IDS:
                collector.add(
                    filename=filename,
                    config_path="lineages[].auxiliary_slot_id",
                    identifier=lineage.lineage_id,
                    reason="辅助功法必须绑定 guard、movement、spirit 之一",
                )
                continue
            path_to_auxiliary_slots[lineage.path_id].append(lineage.auxiliary_slot_id)

        if main_count != 12:
            collector.add(
                filename=filename,
                config_path="lineages",
                identifier="main_lineage_count",
                reason="首发主修功法谱系必须固定为 12 条",
            )
        if auxiliary_count != 18:
            collector.add(
                filename=filename,
                config_path="lineages",
                identifier="auxiliary_lineage_count",
                reason="首发辅助功法谱系必须固定为 18 条",
            )

        for path_id in LAUNCH_SKILL_PATH_IDS:
            if path_to_main_count[path_id] != 2:
                collector.add(
                    filename=filename,
                    config_path="lineages",
                    identifier=path_id,
                    reason="每个流派必须固定配置两条主修功法谱系",
                )
            auxiliary_slot_ids = tuple(sorted(path_to_auxiliary_slots[path_id]))
            if auxiliary_slot_ids != tuple(sorted(LAUNCH_SKILL_AUXILIARY_SLOT_IDS)):
                collector.add(
                    filename=filename,
                    config_path="lineages",
                    identifier=f"{path_id}_auxiliary_slots",
                    reason="每个流派必须固定配置护体、身法、神识三条辅助功法谱系",
                )


class SkillDropPoolEntryDefinition(StaticConfigModel):
    """掉落池中的单个谱系条目。"""

    lineage_id: StableId
    weight: PositiveInt


class SkillDropDefaultProbabilityConfig(StaticConfigModel):
    """功法掉落默认概率配置。"""

    main_lineage_drop_rate: PercentageDecimal
    auxiliary_lineage_drop_rate: PercentageDecimal
    guard_slot_rate: PercentageDecimal
    movement_slot_rate: PercentageDecimal
    spirit_slot_rate: PercentageDecimal
    duplicate_drop_allowed: bool


class SkillDropPoolDefinition(OrderedConfigItem):
    """单个功法掉落池定义。"""

    pool_id: StableId
    skill_type: StableId
    auxiliary_slot_id: StableId | None = None
    summary: ShortText
    entries: tuple[SkillDropPoolEntryDefinition, ...]


class SkillDropConfig(VersionedSectionConfig):
    """功法掉落池配置。"""

    default_probabilities: SkillDropDefaultProbabilityConfig
    pools: tuple[SkillDropPoolDefinition, ...]

    @property
    def ordered_pools(self) -> tuple[SkillDropPoolDefinition, ...]:
        """按顺序返回功法掉落池。"""
        return tuple(sorted(self.pools, key=lambda item: item.order))

    def get_pool(self, pool_id: str) -> SkillDropPoolDefinition | None:
        """读取指定掉落池。"""
        for pool in self.pools:
            if pool.pool_id == pool_id:
                return pool
        return None

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构错误。"""
        probability = self.default_probabilities
        if probability.main_lineage_drop_rate + probability.auxiliary_lineage_drop_rate != Decimal("1"):
            collector.add(
                filename=filename,
                config_path="default_probabilities",
                identifier="drop_rate_sum",
                reason="主修与辅助功法的默认掉落概率之和必须等于 1",
            )
        if probability.guard_slot_rate + probability.movement_slot_rate + probability.spirit_slot_rate != Decimal("1"):
            collector.add(
                filename=filename,
                config_path="default_probabilities",
                identifier="auxiliary_slot_rate_sum",
                reason="三个辅助槽位的默认掉落概率之和必须等于 1",
            )

        ordered_pools = self.ordered_pools
        pool_ids = tuple(pool.pool_id for pool in ordered_pools)
        pool_order_counter = Counter(pool.order for pool in self.pools)
        pool_id_counter = Counter(pool.pool_id for pool in self.pools)

        if pool_ids != LAUNCH_SKILL_DROP_POOL_IDS:
            collector.add(
                filename=filename,
                config_path="pools",
                identifier="drop_pool_sequence",
                reason="首发掉落池必须固定为 launch_main_pool、launch_guard_pool、launch_movement_pool、launch_spirit_pool",
            )

        for pool in self.pools:
            if pool_order_counter[pool.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="pools[].order",
                    identifier=pool.pool_id,
                    reason=f"掉落池顺序值 {pool.order} 重复",
                )
            if pool_id_counter[pool.pool_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="pools[].pool_id",
                    identifier=pool.pool_id,
                    reason="掉落池标识重复",
                )
            if pool.skill_type not in LAUNCH_SKILL_TYPE_IDS:
                collector.add(
                    filename=filename,
                    config_path="pools[].skill_type",
                    identifier=pool.pool_id,
                    reason=f"掉落池引用了未定义功法类型 {pool.skill_type}",
                )
            if pool.skill_type == "main" and pool.auxiliary_slot_id is not None:
                collector.add(
                    filename=filename,
                    config_path="pools[].auxiliary_slot_id",
                    identifier=pool.pool_id,
                    reason="主修掉落池不能声明辅助槽位",
                )
            if pool.skill_type == "auxiliary" and pool.auxiliary_slot_id not in LAUNCH_SKILL_AUXILIARY_SLOT_IDS:
                collector.add(
                    filename=filename,
                    config_path="pools[].auxiliary_slot_id",
                    identifier=pool.pool_id,
                    reason="辅助掉落池必须绑定有效辅助槽位",
                )
            if not pool.entries:
                collector.add(
                    filename=filename,
                    config_path="pools[].entries",
                    identifier=pool.pool_id,
                    reason="掉落池至少需要一条谱系权重配置",
                )
                continue
            lineage_counter = Counter(entry.lineage_id for entry in pool.entries)
            for entry in pool.entries:
                if lineage_counter[entry.lineage_id] > 1:
                    collector.add(
                        filename=filename,
                        config_path="pools[].entries[].lineage_id",
                        identifier=pool.pool_id,
                        reason=f"掉落池内存在重复谱系标识 {entry.lineage_id}",
                    )


__all__ = [
    "LAUNCH_SKILL_AUXILIARY_SLOT_IDS",
    "LAUNCH_SKILL_AXIS_IDS",
    "LAUNCH_SKILL_DROP_POOL_IDS",
    "LAUNCH_SKILL_PATH_IDS",
    "LAUNCH_SKILL_QUALITY_IDS",
    "LAUNCH_SKILL_TYPE_IDS",
    "SkillAttributePoolDefinition",
    "SkillAuxiliarySlotDefinition",
    "SkillAxisDefinition",
    "SkillDropConfig",
    "SkillDropDefaultProbabilityConfig",
    "SkillDropPoolDefinition",
    "SkillDropPoolEntryDefinition",
    "SkillGenerationConfig",
    "SkillLineageConfig",
    "SkillLineageDefinition",
    "SkillPatchDefinition",
    "SkillPatchPoolDefinition",
    "SkillPathConfig",
    "SkillPathDefinition",
    "SkillQualityDefinition",
    "SkillRankDefinition",
]
