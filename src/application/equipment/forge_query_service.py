"""锻造面板只读查询服务。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from application.character.profile_panel_query_service import (
    ProfilePanelQueryService,
    ProfilePanelQueryServiceError,
    SkillPanelSkillSlotSnapshot,
    SkillPanelSnapshot,
)
from application.equipment.equipment_service import (
    EquipmentItemSnapshot,
    EquipmentService,
    EquipmentServiceError,
)
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.repositories import InventoryRepository

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
_SKILL_CORE_ROLE_BY_SLOT_ID = {
    "main": "主修功法槽位，当前纳入统一锻造目标选择。",
    "guard": "护体功法槽位，当前纳入统一锻造目标选择。",
    "movement": "身法功法槽位，当前纳入统一锻造目标选择。",
    "spirit": "神识功法槽位，当前纳入统一锻造目标选择。",
}
_SKILL_ACTION_STATUS_TEXT = "当前功法培养写操作尚未开放；此处仅支持统一目标选择与详情查看。"


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
    equipped_item: EquipmentItemSnapshot | None = None
    equipped_skill: SkillPanelSkillSlotSnapshot | None = None
    supported_operations: tuple[ForgeOperationId, ...] = ()
    action_status_text: str = ""


@dataclass(frozen=True, slots=True)
class ForgePanelSnapshot:
    """锻造面板只读快照。"""

    character_id: int
    character_name: str
    resources: ForgeResourceSnapshot
    targets: tuple[ForgeTargetSnapshot, ...]


class ForgePanelQueryService:
    """聚合锻造资源栏与已装备目标的查询服务。"""

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

    def get_panel_snapshot(self, *, character_id: int) -> ForgePanelSnapshot:
        """读取锻造面板快照。"""
        try:
            collection = self._equipment_service.list_equipment(character_id=character_id)
            skill_snapshot = self._profile_panel_query_service.get_skill_snapshot(character_id=character_id)
            material_items = self._inventory_repository.list_by_character_id_and_type(character_id, _MATERIAL_ITEM_TYPE)
        except (EquipmentServiceError, ProfilePanelQueryServiceError) as exc:
            raise ForgePanelStateError(str(exc)) from exc

        equipped_by_slot = {
            item.slot_id: item
            for item in collection.equipped_items
            if item.item_state == "active" and item.equipped_slot_id == item.slot_id
        }
        resources = self._build_resource_snapshot(
            spirit_stone=collection.spirit_stone,
            material_items=material_items,
        )
        targets = self._build_targets(
            equipped_by_slot=equipped_by_slot,
            skill_snapshot=skill_snapshot,
        )
        return ForgePanelSnapshot(
            character_id=character_id,
            character_name=skill_snapshot.character_name,
            resources=resources,
            targets=targets,
        )

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

    def _build_targets(
        self,
        *,
        equipped_by_slot: dict[str, EquipmentItemSnapshot],
        skill_snapshot: SkillPanelSnapshot,
    ) -> tuple[ForgeTargetSnapshot, ...]:
        equipment_targets = tuple(
            ForgeTargetSnapshot(
                target_id=slot.slot_id,
                target_kind=ForgeTargetKind.EQUIPMENT,
                slot_id=slot.slot_id,
                slot_name=slot.name,
                core_role=slot.core_role,
                equipped_item=equipped_by_slot.get(slot.slot_id),
                supported_operations=self._build_supported_operations(slot_id=slot.slot_id),
                action_status_text=self._build_equipment_status_text(
                    slot_name=slot.name,
                    slot_id=slot.slot_id,
                    equipped_item=equipped_by_slot.get(slot.slot_id),
                ),
            )
            for slot in self._slot_definitions
        )
        skill_targets = tuple(
            self._build_skill_target(skill_item=skill_item)
            for skill_item in (skill_snapshot.main_skill, *skill_snapshot.auxiliary_skills)
        )
        return equipment_targets + skill_targets

    @staticmethod
    def _build_supported_operations(*, slot_id: str) -> tuple[ForgeOperationId, ...]:
        base_operations = (
            ForgeOperationId.ENHANCE,
            ForgeOperationId.WASH,
            ForgeOperationId.REFORGE,
            ForgeOperationId.DISMANTLE,
            ForgeOperationId.UNEQUIP,
        )
        if slot_id == "artifact":
            return base_operations + (ForgeOperationId.NURTURE,)
        return base_operations

    def _build_equipment_status_text(
        self,
        *,
        slot_name: str,
        slot_id: str,
        equipped_item: EquipmentItemSnapshot | None,
    ) -> str:
        if equipped_item is None:
            return f"当前{slot_name}槽位暂无已装备目标，需先从背包装配后才能执行锻造操作。"
        supported_operations = self._build_supported_operations(slot_id=slot_id)
        operation_names = "｜".join(self._format_operation_name(operation_id) for operation_id in supported_operations)
        return f"当前已锁定已装备目标，可执行：{operation_names}。"

    def _build_skill_target(self, *, skill_item: SkillPanelSkillSlotSnapshot) -> ForgeTargetSnapshot:
        return ForgeTargetSnapshot(
            target_id=skill_item.slot_id,
            target_kind=ForgeTargetKind.SKILL,
            slot_id=skill_item.slot_id,
            slot_name=f"{skill_item.slot_name}功法",
            core_role=_SKILL_CORE_ROLE_BY_SLOT_ID.get(skill_item.slot_id, "功法槽位，当前纳入统一锻造目标选择。"),
            equipped_skill=skill_item,
            supported_operations=(),
            action_status_text=_SKILL_ACTION_STATUS_TEXT,
        )

    @staticmethod
    def _format_operation_name(operation_id: ForgeOperationId) -> str:
        return {
            ForgeOperationId.ENHANCE: "强化",
            ForgeOperationId.WASH: "洗炼",
            ForgeOperationId.REFORGE: "重铸",
            ForgeOperationId.NURTURE: "法宝培养",
            ForgeOperationId.DISMANTLE: "分解",
            ForgeOperationId.UNEQUIP: "卸下装备",
        }[operation_id]


__all__ = [
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
