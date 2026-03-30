"""锻造面板只读查询服务。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from application.character.profile_panel_query_service import (
    ProfilePanelQueryService,
    ProfilePanelQueryServiceError,
    SkillPanelSkillSlotSnapshot,
)
from application.equipment.equipment_service import (
    EquipmentItemSnapshot,
    EquipmentService,
    EquipmentServiceError,
)
from application.equipment.panel_query_service import format_equipment_affix_display_line, format_equipment_affix_display_lines
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.repositories import InventoryRepository

_PAGE_SIZE = 25
_MATERIAL_ITEM_TYPE = "material"
_RESOURCE_NAME_BY_ID = {
    "spirit_stone": "灵石",
    "enhancement_stone": "强化石",
    "enhancement_shard": "强化碎晶",
    "wash_jade": "洗炼玉",
    "seal_talisman": "封缄符",
    "reforge_crystal": "重铸晶",
    "wash_dust": "洗炼尘",
    "spirit_sand": "灵砂",
    "spirit_pattern_stone": "灵纹石",
    "soul_binding_jade": "缚魂玉",
    "artifact_essence": "法宝精粹",
}
_EQUIPMENT_CATEGORY_ORDER = {
    "weapon": 0,
    "armor": 1,
    "accessory": 2,
    "artifact": 3,
}
_SKILL_SLOT_ORDER = {
    "main": 0,
    "guard": 1,
    "movement": 2,
    "spirit": 3,
}
_SKILL_CORE_ROLE_BY_SLOT_ID = {
    "main": "主修功法",
    "guard": "护体功法",
    "movement": "身法功法",
    "spirit": "神识功法",
}
_OPERATION_NAME_BY_ID = {
    "enhance": "强化",
    "wash": "洗炼",
    "reforge": "重铸",
    "nurture": "法宝培养",
    "dismantle": "分解",
    "unequip": "卸下装备",
}
_STAT_NAME_BY_ID = {
    "max_hp": "气血",
    "attack_power": "攻力",
    "guard_power": "护体",
    "speed": "迅捷",
    "crit_rate_permille": "暴击",
    "crit_damage_bonus_permille": "暴伤",
    "hit_rate_permille": "命中",
    "dodge_rate_permille": "闪避",
    "damage_bonus_permille": "增伤",
    "damage_reduction_permille": "减伤",
    "counter_rate_permille": "反击",
    "control_bonus_permille": "控势",
    "control_resist_permille": "定心",
    "healing_power_permille": "疗愈",
    "shield_power_permille": "护盾",
    "penetration_permille": "穿透",
}


class ForgeFilterId(StrEnum):
    """锻造面板筛选标识。"""

    ALL = "all"
    WEAPON = "weapon"
    ARMOR = "armor"
    ACCESSORY = "accessory"
    ARTIFACT = "artifact"
    SKILL = "skill"


class ForgeTargetKind(StrEnum):
    """锻造目标类别。"""

    EQUIPMENT = "equipment"
    SKILL = "skill"


class ForgeOperationId(StrEnum):
    """锻造面板支持的操作标识。"""

    ENHANCE = "enhance"
    WASH = "wash"
    REFORGE = "reforge"
    NURTURE = "nurture"
    DISMANTLE = "dismantle"
    UNEQUIP = "unequip"


class ForgePanelQueryServiceError(RuntimeError):
    """锻造查询基础异常。"""


class ForgePanelStateError(ForgePanelQueryServiceError):
    """锻造查询状态异常。"""


@dataclass(frozen=True, slots=True)
class ForgeResourceEntrySnapshot:
    """单个锻造资源摘要。"""

    resource_id: str
    resource_name: str
    quantity: int


@dataclass(frozen=True, slots=True)
class ForgeResourceSnapshot:
    """锻造资源栏快照。"""

    spirit_stone: int
    enhancement_stone: int
    enhancement_shard: int
    wash_dust: int
    spirit_sand: int
    spirit_pattern_stone: int
    soul_binding_jade: int
    artifact_essence: int
    entries: tuple[ForgeResourceEntrySnapshot, ...]


@dataclass(frozen=True, slots=True)
class ForgeCardSnapshot:
    """可直接渲染的锻造目标卡片。"""

    name: str
    badge_line: str
    growth_line: str | None
    stat_lines: tuple[str, ...]
    keyword_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ForgeOperationCostSnapshot:
    """当前操作消耗快照。"""

    resource_id: str
    resource_name: str
    required_quantity: int
    owned_quantity: int


@dataclass(frozen=True, slots=True)
class ForgeOperationPreviewSnapshot:
    """当前操作结果预览。"""

    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ForgeTargetSnapshot:
    """锻造当前可选目标。"""

    target_id: str
    target_kind: ForgeTargetKind
    slot_id: str
    slot_name: str
    core_role: str
    display_name: str
    summary_line: str
    equipped: bool = False
    equipment_item: EquipmentItemSnapshot | None = None
    equipped_skill: SkillPanelSkillSlotSnapshot | None = None
    supported_operations: tuple[ForgeOperationId, ...] = ()


@dataclass(frozen=True, slots=True)
class ForgePanelSnapshot:
    """锻造面板只读快照。"""

    character_id: int
    character_name: str
    resources: ForgeResourceSnapshot
    filter_id: ForgeFilterId
    page: int
    page_size: int
    total_items: int
    total_pages: int
    targets: tuple[ForgeTargetSnapshot, ...]
    selected_target: ForgeTargetSnapshot | None
    selected_target_card: ForgeCardSnapshot | None
    current_operation_name: str | None
    operation_costs: tuple[ForgeOperationCostSnapshot, ...]
    operation_preview: ForgeOperationPreviewSnapshot | None


class ForgePanelQueryService:
    """聚合锻造资源栏、筛选分页与培养目标的查询服务。"""

    def __init__(
        self,
        *,
        equipment_service: EquipmentService,
        inventory_repository: InventoryRepository,
        profile_panel_query_service: ProfilePanelQueryService,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._equipment_service = equipment_service
        self._inventory_repository = inventory_repository
        self._profile_panel_query_service = profile_panel_query_service
        self._static_config = static_config or get_static_config()
        self._slot_definitions = tuple(self._static_config.equipment.ordered_slots)
        self._slot_by_id = {slot.slot_id: slot for slot in self._slot_definitions}
        self._equipment_quality_order_by_id = {
            quality.quality_id: quality.order for quality in self._static_config.equipment.qualities
        }
        self._skill_quality_order_by_id = {
            quality.quality_id: quality.order for quality in self._static_config.skill_generation.qualities
        }

    def get_panel_snapshot(
        self,
        *,
        character_id: int,
        filter_id: ForgeFilterId = ForgeFilterId.ALL,
        page: int = 1,
        selected_target_id: str | None = None,
        pending_action: str | None = None,
        locked_affix_positions: tuple[int, ...] = (),
    ) -> ForgePanelSnapshot:
        """读取锻造面板快照。"""
        normalized_filter = self._normalize_filter_id(filter_id)
        normalized_page = max(1, int(page))
        try:
            collection = self._equipment_service.list_equipment(character_id=character_id)
            skill_snapshot = self._profile_panel_query_service.get_skill_snapshot(character_id=character_id)
            material_items = self._inventory_repository.list_by_character_id_and_type(character_id, _MATERIAL_ITEM_TYPE)
        except (EquipmentServiceError, ProfilePanelQueryServiceError) as exc:
            raise ForgePanelStateError(str(exc)) from exc

        active_equipment_items = tuple(item for item in collection.active_items if item.item_state == "active")
        skill_items = tuple(skill_item for skill_item in (skill_snapshot.main_skill, *skill_snapshot.auxiliary_skills))
        equipment_targets = tuple(self._build_equipment_target(item=item) for item in active_equipment_items)
        skill_targets = tuple(self._build_skill_target(skill_item=skill_item) for skill_item in skill_items)
        filtered_targets = self._build_filtered_targets(
            filter_id=normalized_filter,
            equipment_targets=equipment_targets,
            skill_targets=skill_targets,
        )
        total_items = len(filtered_targets)
        total_pages = max(1, (total_items + _PAGE_SIZE - 1) // _PAGE_SIZE)
        current_page = min(normalized_page, total_pages)
        page_targets = filtered_targets[(current_page - 1) * _PAGE_SIZE : current_page * _PAGE_SIZE]
        selected_target = self._resolve_selected_target(
            selected_target_id=selected_target_id,
            page_targets=page_targets,
        )
        material_quantity_by_id = self._build_material_quantity_map(material_items=material_items)
        resources = self._build_resource_snapshot(
            spirit_stone=collection.spirit_stone,
            material_items=material_items,
        )
        operation_id = self._resolve_operation_id(target=selected_target, pending_action=pending_action)
        return ForgePanelSnapshot(
            character_id=character_id,
            character_name=skill_snapshot.character_name,
            resources=resources,
            filter_id=normalized_filter,
            page=current_page,
            page_size=_PAGE_SIZE,
            total_items=total_items,
            total_pages=total_pages,
            targets=page_targets,
            selected_target=selected_target,
            selected_target_card=self._build_target_card(target=selected_target),
            current_operation_name=None if operation_id is None else _OPERATION_NAME_BY_ID.get(operation_id.value),
            operation_costs=self._build_operation_costs(
                target=selected_target,
                operation_id=operation_id,
                spirit_stone=collection.spirit_stone,
                material_quantity_by_id=material_quantity_by_id,
                locked_affix_positions=locked_affix_positions,
            ),
            operation_preview=self._build_operation_preview(
                target=selected_target,
                operation_id=operation_id,
                locked_affix_positions=locked_affix_positions,
            ),
        )

    @staticmethod
    def _normalize_filter_id(filter_id: ForgeFilterId | str) -> ForgeFilterId:
        if isinstance(filter_id, ForgeFilterId):
            return filter_id
        try:
            return ForgeFilterId(str(filter_id).strip())
        except ValueError as exc:
            raise ForgePanelStateError(f"未支持的锻造筛选：{filter_id}") from exc

    def _build_resource_snapshot(self, *, spirit_stone: int, material_items) -> ForgeResourceSnapshot:
        material_quantity_by_id = self._build_material_quantity_map(material_items=material_items)
        enhancement_stone = material_quantity_by_id.get("enhancement_stone", 0)
        enhancement_shard = material_quantity_by_id.get("enhancement_shard", 0)
        wash_dust = material_quantity_by_id.get("wash_dust", 0) + material_quantity_by_id.get("wash_jade", 0)
        spirit_sand = material_quantity_by_id.get("spirit_sand", 0)
        spirit_pattern_stone = material_quantity_by_id.get("spirit_pattern_stone", 0) + material_quantity_by_id.get("seal_talisman", 0)
        soul_binding_jade = material_quantity_by_id.get("soul_binding_jade", 0) + material_quantity_by_id.get("reforge_crystal", 0)
        artifact_essence = material_quantity_by_id.get("artifact_essence", 0)
        entries = tuple(
            ForgeResourceEntrySnapshot(
                resource_id=resource_id,
                resource_name=resource_name,
                quantity=quantity,
            )
            for resource_id, resource_name, quantity in (
                ("spirit_stone", _RESOURCE_NAME_BY_ID["spirit_stone"], max(0, int(spirit_stone))),
                ("enhancement_stone", _RESOURCE_NAME_BY_ID["enhancement_stone"], enhancement_stone),
                ("enhancement_shard", _RESOURCE_NAME_BY_ID["enhancement_shard"], enhancement_shard),
                ("wash_dust", _RESOURCE_NAME_BY_ID["wash_dust"], wash_dust),
                ("spirit_sand", _RESOURCE_NAME_BY_ID["spirit_sand"], spirit_sand),
                ("spirit_pattern_stone", _RESOURCE_NAME_BY_ID["spirit_pattern_stone"], spirit_pattern_stone),
                ("soul_binding_jade", _RESOURCE_NAME_BY_ID["soul_binding_jade"], soul_binding_jade),
                ("artifact_essence", _RESOURCE_NAME_BY_ID["artifact_essence"], artifact_essence),
            )
        )
        return ForgeResourceSnapshot(
            spirit_stone=max(0, int(spirit_stone)),
            enhancement_stone=enhancement_stone,
            enhancement_shard=enhancement_shard,
            wash_dust=wash_dust,
            spirit_sand=spirit_sand,
            spirit_pattern_stone=spirit_pattern_stone,
            soul_binding_jade=soul_binding_jade,
            artifact_essence=artifact_essence,
            entries=entries,
        )

    @staticmethod
    def _build_material_quantity_map(*, material_items) -> dict[str, int]:
        return {str(item.item_id): max(0, int(item.quantity)) for item in material_items}

    def _build_target_card(self, *, target: ForgeTargetSnapshot | None) -> ForgeCardSnapshot | None:
        if target is None:
            return None
        if target.target_kind is ForgeTargetKind.SKILL:
            if target.equipped_skill is None:
                return None
            return self._build_skill_card(skill_item=target.equipped_skill)
        if target.equipment_item is None:
            return None
        return self._build_equipment_card(item=target.equipment_item)

    def _build_equipment_card(self, *, item: EquipmentItemSnapshot) -> ForgeCardSnapshot:
        stats = item.resolved_stats if item.resolved_stats else item.base_attributes
        stat_lines = tuple(
            f"{self._format_stat_name(stat.stat_id)} {self._format_stat_value(stat.stat_id, stat.value)}"
            for stat in stats[:4]
        )
        keyword_lines = format_equipment_affix_display_lines(item.affixes, static_config=self._static_config, limit=3)
        growth_parts = [f"强化 +{item.enhancement_level}"]
        if item.is_artifact:
            growth_parts.append(f"祭炼 {item.artifact_nurture_level}")
        return ForgeCardSnapshot(
            name=item.display_name,
            badge_line=f"{'法宝' if item.is_artifact else item.slot_name}｜{item.rank_name}｜{item.quality_name}",
            growth_line="｜".join(growth_parts),
            stat_lines=stat_lines or ("暂无关键属性",),
            keyword_lines=keyword_lines,
        )

    def _build_skill_card(self, *, skill_item: SkillPanelSkillSlotSnapshot) -> ForgeCardSnapshot:
        return ForgeCardSnapshot(
            name=skill_item.skill_name,
            badge_line=f"{skill_item.slot_name}｜{skill_item.rank_name}｜{skill_item.quality_name}",
            growth_line=f"流派 {skill_item.path_name}",
            stat_lines=(
                f"类型 {'主修' if skill_item.skill_type == 'main' else '辅修'}",
                f"预算 {skill_item.total_budget}",
                f"加成 {len(skill_item.resolved_patch_ids)}",
            ),
            keyword_lines=tuple(self._format_patch_name(patch_id) for patch_id in skill_item.resolved_patch_ids[:3]),
        )

    def _resolve_operation_id(
        self,
        *,
        target: ForgeTargetSnapshot | None,
        pending_action: str | None,
    ) -> ForgeOperationId | None:
        if target is None or target.target_kind is ForgeTargetKind.SKILL or target.equipment_item is None:
            return None
        normalized_pending_action = str(pending_action or "").strip()
        if normalized_pending_action:
            try:
                pending_operation = ForgeOperationId(normalized_pending_action)
            except ValueError:
                pending_operation = None
            if pending_operation is not None and pending_operation in target.supported_operations:
                return pending_operation
        item = target.equipment_item
        if item.is_artifact and ForgeOperationId.NURTURE in target.supported_operations:
            return ForgeOperationId.NURTURE
        if ForgeOperationId.ENHANCE in target.supported_operations:
            return ForgeOperationId.ENHANCE
        if ForgeOperationId.WASH in target.supported_operations:
            return ForgeOperationId.WASH
        if ForgeOperationId.REFORGE in target.supported_operations:
            return ForgeOperationId.REFORGE
        if ForgeOperationId.DISMANTLE in target.supported_operations:
            return ForgeOperationId.DISMANTLE
        return None

    def _build_operation_costs(
        self,
        *,
        target: ForgeTargetSnapshot | None,
        operation_id: ForgeOperationId | None,
        spirit_stone: int,
        material_quantity_by_id: dict[str, int],
        locked_affix_positions: tuple[int, ...],
    ) -> tuple[ForgeOperationCostSnapshot, ...]:
        if target is None or operation_id is None or target.equipment_item is None:
            return ()
        cost_mapping = self._resolve_operation_cost_mapping(
            item=target.equipment_item,
            operation_id=operation_id,
            locked_affix_positions=locked_affix_positions,
        )
        return tuple(
            ForgeOperationCostSnapshot(
                resource_id=resource_id,
                resource_name=_RESOURCE_NAME_BY_ID.get(resource_id, resource_id),
                required_quantity=quantity,
                owned_quantity=(max(0, int(spirit_stone)) if resource_id == "spirit_stone" else material_quantity_by_id.get(resource_id, 0)),
            )
            for resource_id, quantity in cost_mapping.items()
            if quantity > 0
        )

    def _build_operation_preview(
        self,
        *,
        target: ForgeTargetSnapshot | None,
        operation_id: ForgeOperationId | None,
        locked_affix_positions: tuple[int, ...],
    ) -> ForgeOperationPreviewSnapshot | None:
        if target is None:
            return None
        if target.target_kind is ForgeTargetKind.SKILL:
            return ForgeOperationPreviewSnapshot(title="功法", lines=("当前功法暂无锻造操作。",))
        item = target.equipment_item
        if item is None or operation_id is None:
            return ForgeOperationPreviewSnapshot(title="锻造", lines=("当前目标暂无可执行操作。",))
        if operation_id is ForgeOperationId.ENHANCE:
            target_level = item.enhancement_level + 1
            level_rule = self._static_config.equipment.get_enhancement_level(target_level)
            if level_rule is None:
                return ForgeOperationPreviewSnapshot(title="强化", lines=("强化已到上限。",))
            return ForgeOperationPreviewSnapshot(
                title="强化",
                lines=(
                    f"强化 +{item.enhancement_level} → +{target_level}",
                    f"成功率 {float(level_rule.success_rate) * 100:.1f}%",
                    f"词条 {len(item.affixes)} → {len(item.affixes) + level_rule.bonus_affix_unlock_count}",
                ),
            )
        if operation_id is ForgeOperationId.NURTURE:
            target_level = item.artifact_nurture_level + 1
            level_rule = self._static_config.equipment.get_artifact_nurture_level(target_level)
            if level_rule is None:
                return ForgeOperationPreviewSnapshot(title="法宝培养", lines=("祭炼已到上限。",))
            return ForgeOperationPreviewSnapshot(
                title="法宝培养",
                lines=(
                    f"祭炼 {item.artifact_nurture_level} → {target_level}",
                    f"基础属性 +{float(level_rule.base_stat_bonus_ratio) * 100:.1f}%",
                    f"词条成长 +{float(level_rule.affix_bonus_ratio) * 100:.1f}%",
                ),
            )
        if operation_id is ForgeOperationId.WASH:
            locked_summary = self._build_affix_transition_lines(item=item, locked_affix_positions=locked_affix_positions)
            return ForgeOperationPreviewSnapshot(title="洗炼", lines=locked_summary or ("当前没有可洗炼词条。",))
        if operation_id is ForgeOperationId.REFORGE:
            return ForgeOperationPreviewSnapshot(
                title="重铸",
                lines=(
                    f"底材 {item.template_name} → 随机同品质新底材",
                    f"词条 {len(item.affixes)} 条 → 全部重铸",
                    f"强化 +{item.enhancement_level} → +{item.enhancement_level}",
                    (
                        f"祭炼 {item.artifact_nurture_level} → {item.artifact_nurture_level}"
                        if item.is_artifact
                        else f"部位 {item.slot_name} 保持不变"
                    ),
                ),
            )
        if operation_id is ForgeOperationId.DISMANTLE:
            returns = self._build_dismantle_return_lines(item=item)
            return ForgeOperationPreviewSnapshot(title="分解", lines=returns or ("本次不会获得资源回收。",))
        return ForgeOperationPreviewSnapshot(title="锻造", lines=("当前操作暂无预览。",))

    def _resolve_operation_cost_mapping(
        self,
        *,
        item: EquipmentItemSnapshot,
        operation_id: ForgeOperationId,
        locked_affix_positions: tuple[int, ...],
    ) -> dict[str, int]:
        if operation_id is ForgeOperationId.ENHANCE:
            target_level = item.enhancement_level + 1
            level_rule = self._static_config.equipment.get_enhancement_level(target_level)
            if level_rule is None:
                return {}
            return {cost.resource_id: int(cost.quantity) for cost in level_rule.costs}
        if operation_id is ForgeOperationId.NURTURE:
            target_level = item.artifact_nurture_level + 1
            level_rule = self._static_config.equipment.get_artifact_nurture_level(target_level)
            if level_rule is None:
                return {}
            return {cost.resource_id: int(cost.quantity) for cost in level_rule.costs}
        if operation_id is ForgeOperationId.WASH:
            lock_count = len(set(locked_affix_positions))
            cost_mapping: dict[str, int] = defaultdict(int)
            for cost in self._static_config.equipment.wash.base_costs:
                cost_mapping[cost.resource_id] += int(cost.quantity)
            for cost in self._static_config.equipment.wash.lock_extra_costs:
                cost_mapping[cost.resource_id] += int(cost.quantity) * lock_count
            return dict(cost_mapping)
        if operation_id is ForgeOperationId.REFORGE:
            return {cost.resource_id: int(cost.quantity) for cost in self._static_config.equipment.reforge.costs}
        return {}

    def _build_affix_transition_lines(
        self,
        *,
        item: EquipmentItemSnapshot,
        locked_affix_positions: tuple[int, ...],
    ) -> tuple[str, ...]:
        locked_positions = set(locked_affix_positions)
        lines: list[str] = [f"锁定 {len(locked_positions)} 条"]
        for index, affix in enumerate(item.affixes[:4], start=1):
            transition = "保留" if index in locked_positions else "重洗"
            lines.append(f"{index}. {self._format_affix_keyword(affix)} → {transition}")
        return tuple(lines)

    def _build_dismantle_return_lines(self, *, item: EquipmentItemSnapshot) -> tuple[str, ...]:
        rule = self._static_config.equipment.get_dismantle_rule(item.quality_id)
        if rule is None:
            return ()
        returns: dict[str, int] = defaultdict(int)
        for resource in rule.base_returns:
            returns[resource.resource_id] += int(resource.quantity)
        for resource in rule.enhancement_returns_per_level:
            returns[resource.resource_id] += int(resource.quantity) * item.enhancement_level
        for resource in rule.affix_returns_per_count:
            returns[resource.resource_id] += int(resource.quantity) * len(item.affixes)
        if item.is_artifact:
            for resource in rule.artifact_bonus_returns:
                returns[resource.resource_id] += int(resource.quantity)
            for resource in rule.artifact_nurture_returns_per_level:
                returns[resource.resource_id] += int(resource.quantity) * item.artifact_nurture_level
        return tuple(
            f"{_RESOURCE_NAME_BY_ID.get(resource_id, resource_id)} +{quantity}"
            for resource_id, quantity in returns.items()
            if quantity > 0
        )

    @staticmethod
    def _format_stat_name(stat_id: str) -> str:
        return _STAT_NAME_BY_ID.get(stat_id, stat_id)

    @staticmethod
    def _format_stat_value(stat_id: str, value: int) -> str:
        if stat_id.endswith("_permille"):
            return f"{value / 10:.1f}%"
        return str(value)

    def _format_affix_keyword(self, affix) -> str:
        return format_equipment_affix_display_line(affix, static_config=self._static_config)

    def _format_patch_name(self, patch_id: str) -> str:
        normalized_patch_id = patch_id.strip()
        if not normalized_patch_id:
            return "未命名流派修正"
        patch = self._static_config.skill_generation.get_patch(normalized_patch_id)
        if patch is not None:
            return str(patch.name)
        if _looks_like_internal_identifier(normalized_patch_id):
            return "未命名流派修正"
        return normalized_patch_id

    def _build_filtered_targets(
        self,
        *,
        filter_id: ForgeFilterId,
        equipment_targets: tuple[ForgeTargetSnapshot, ...],
        skill_targets: tuple[ForgeTargetSnapshot, ...],
    ) -> tuple[ForgeTargetSnapshot, ...]:
        if filter_id is ForgeFilterId.ALL:
            merged_targets = [*equipment_targets, *skill_targets]
            merged_targets.sort(key=self._build_all_target_sort_key)
            return tuple(merged_targets)

        if filter_id is ForgeFilterId.SKILL:
            sorted_skills = sorted(skill_targets, key=self._build_skill_target_sort_key)
            return tuple(sorted_skills)

        filtered_equipment_targets = [
            target
            for target in equipment_targets
            if target.equipment_item is not None
            and self._match_equipment_filter(item=target.equipment_item, filter_id=filter_id)
        ]
        filtered_equipment_targets.sort(key=self._build_equipment_target_sort_key)
        return tuple(filtered_equipment_targets)

    def _build_all_target_sort_key(self, target: ForgeTargetSnapshot) -> tuple[int, int, int, int, int, int]:
        if target.target_kind is ForgeTargetKind.EQUIPMENT:
            item = target.equipment_item
            if item is None:
                return (len(_EQUIPMENT_CATEGORY_ORDER), 1, 0, 0, 0, 0)
            category_order = _EQUIPMENT_CATEGORY_ORDER.get(self._resolve_equipment_category(item), len(_EQUIPMENT_CATEGORY_ORDER))
            return (category_order, *self._build_equipment_target_sort_key(target))
        return (len(_EQUIPMENT_CATEGORY_ORDER), *self._build_skill_target_sort_key(target))

    def _build_equipment_target_sort_key(self, target: ForgeTargetSnapshot) -> tuple[int, int, int, int, int]:
        item = target.equipment_item
        if item is None:
            return (1, 0, 0, 0, 0)
        growth_level = item.artifact_nurture_level if item.is_artifact else item.enhancement_level
        return (
            0 if target.equipped else 1,
            -self._equipment_quality_order_by_id.get(item.quality_id, 0),
            -item.rank_order,
            -growth_level,
            -item.item_id,
        )

    def _build_skill_target_sort_key(self, target: ForgeTargetSnapshot) -> tuple[int, int, int, int, int]:
        skill_item = target.equipped_skill
        if skill_item is None:
            return (1, len(_SKILL_SLOT_ORDER), 0, 0, 0)
        return (
            0 if target.equipped else 1,
            _SKILL_SLOT_ORDER.get(skill_item.slot_id, len(_SKILL_SLOT_ORDER)),
            -self._skill_quality_order_by_id.get(skill_item.quality_id, 0),
            -skill_item.item_id,
            0,
        )

    @staticmethod
    def _resolve_equipment_category(item: EquipmentItemSnapshot) -> str:
        if item.is_artifact or item.slot_id == ForgeFilterId.ARTIFACT.value:
            return ForgeFilterId.ARTIFACT.value
        return item.slot_id

    @staticmethod
    def _match_equipment_filter(*, item: EquipmentItemSnapshot, filter_id: ForgeFilterId) -> bool:
        if filter_id is ForgeFilterId.WEAPON:
            return item.slot_id == ForgeFilterId.WEAPON.value and not item.is_artifact
        if filter_id is ForgeFilterId.ARMOR:
            return item.slot_id == ForgeFilterId.ARMOR.value and not item.is_artifact
        if filter_id is ForgeFilterId.ACCESSORY:
            return item.slot_id == ForgeFilterId.ACCESSORY.value and not item.is_artifact
        if filter_id is ForgeFilterId.ARTIFACT:
            return item.slot_id == ForgeFilterId.ARTIFACT.value or item.is_artifact
        return False

    def _build_equipment_target(self, *, item: EquipmentItemSnapshot) -> ForgeTargetSnapshot:
        slot_definition = self._slot_by_id.get(item.slot_id)
        affix_count = len(item.affixes)
        special_affix_count = sum(
            1
            for affix in item.affixes
            if affix.affix_kind == "special_effect" or affix.special_effect is not None
        )
        equipped = item.equipped_slot_id == item.slot_id
        growth_line = f"祭炼 {item.artifact_nurture_level}" if item.is_artifact else f"强化 +{item.enhancement_level}"
        return ForgeTargetSnapshot(
            target_id=self._serialize_equipment_target_id(item_id=item.item_id),
            target_kind=ForgeTargetKind.EQUIPMENT,
            slot_id=item.slot_id,
            slot_name=item.slot_name,
            core_role=(slot_definition.core_role if slot_definition is not None else f"{item.slot_name}目标"),
            display_name=item.display_name,
            summary_line=f"{item.quality_name}｜{item.rank_name}｜{growth_line}｜{affix_count}词条/{special_affix_count}特效",
            equipped=equipped,
            equipment_item=item,
            supported_operations=self._build_supported_operations(item=item),
        )

    def _build_skill_target(self, *, skill_item: SkillPanelSkillSlotSnapshot) -> ForgeTargetSnapshot:
        patch_count = len(skill_item.resolved_patch_ids)
        return ForgeTargetSnapshot(
            target_id=self._serialize_skill_target_id(item_id=skill_item.item_id),
            target_kind=ForgeTargetKind.SKILL,
            slot_id=skill_item.slot_id,
            slot_name=f"{skill_item.slot_name}功法",
            core_role=_SKILL_CORE_ROLE_BY_SLOT_ID.get(skill_item.slot_id, "功法槽位，当前纳入统一锻造目标选择。"),
            display_name=skill_item.skill_name,
            summary_line=f"{skill_item.rank_name}｜{skill_item.quality_name}｜流派加成 {patch_count}",
            equipped=skill_item.equipped_slot_id == skill_item.slot_id,
            equipped_skill=skill_item,
            supported_operations=(),
        )

    @staticmethod
    def _build_supported_operations(*, item: EquipmentItemSnapshot) -> tuple[ForgeOperationId, ...]:
        operations: list[ForgeOperationId] = [
            ForgeOperationId.ENHANCE,
            ForgeOperationId.WASH,
            ForgeOperationId.REFORGE,
        ]
        if item.is_artifact or item.slot_id == "artifact":
            operations.append(ForgeOperationId.NURTURE)
        operations.append(ForgeOperationId.DISMANTLE)
        if item.equipped_slot_id == item.slot_id:
            operations.append(ForgeOperationId.UNEQUIP)
        return tuple(operations)

    @staticmethod
    def _resolve_selected_target(
        *,
        selected_target_id: str | None,
        page_targets: tuple[ForgeTargetSnapshot, ...],
    ) -> ForgeTargetSnapshot | None:
        if not page_targets:
            return None
        if selected_target_id is not None:
            for target in page_targets:
                if target.target_id == selected_target_id:
                    return target
        return page_targets[0]

    @staticmethod
    def _serialize_equipment_target_id(*, item_id: int) -> str:
        return f"equipment:{item_id}"

    @staticmethod
    def _serialize_skill_target_id(*, item_id: int) -> str:
        return f"skill:{item_id}"


def _looks_like_internal_identifier(value: str) -> bool:
    return all(character.islower() or character.isdigit() or character == "_" for character in value)


__all__ = [
    "ForgeCardSnapshot",
    "ForgeFilterId",
    "ForgeOperationCostSnapshot",
    "ForgeOperationId",
    "ForgeOperationPreviewSnapshot",
    "ForgePanelQueryService",
    "ForgePanelQueryServiceError",
    "ForgePanelSnapshot",
    "ForgePanelStateError",
    "ForgeResourceEntrySnapshot",
    "ForgeResourceSnapshot",
    "ForgeTargetKind",
    "ForgeTargetSnapshot",
]
