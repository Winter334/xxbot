"""锻造面板只读查询服务。"""

from __future__ import annotations

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
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.repositories import InventoryRepository

_PAGE_SIZE = 25
_MATERIAL_ITEM_TYPE = "material"
_RESOURCE_NAME_BY_ID = {
    "spirit_stone": "灵石",
    "enhancement_stone": "强化石",
    "enhancement_shard": "强化碎晶",
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
    "main": "主修功法槽位，当前纳入统一锻造目标选择。",
    "guard": "护体功法槽位，当前纳入统一锻造目标选择。",
    "movement": "身法功法槽位，当前纳入统一锻造目标选择。",
    "spirit": "神识功法槽位，当前纳入统一锻造目标选择。",
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
        resources = self._build_resource_snapshot(
            spirit_stone=collection.spirit_stone,
            material_items=material_items,
        )
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
        material_quantity_by_id = {str(item.item_id): max(0, int(item.quantity)) for item in material_items}
        enhancement_stone = material_quantity_by_id.get("enhancement_stone", 0)
        enhancement_shard = material_quantity_by_id.get("enhancement_shard", 0)
        wash_dust = material_quantity_by_id.get("wash_dust", 0)
        spirit_sand = material_quantity_by_id.get("spirit_sand", 0)
        spirit_pattern_stone = material_quantity_by_id.get("spirit_pattern_stone", 0)
        soul_binding_jade = material_quantity_by_id.get("soul_binding_jade", 0)
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


__all__ = [
    "ForgeFilterId",
    "ForgeOperationId",
    "ForgePanelQueryService",
    "ForgePanelQueryServiceError",
    "ForgePanelSnapshot",
    "ForgePanelStateError",
    "ForgeResourceEntrySnapshot",
    "ForgeResourceSnapshot",
    "ForgeTargetKind",
    "ForgeTargetSnapshot",
]
