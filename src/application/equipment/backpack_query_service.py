"""背包面板只读查询服务。"""

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
    EquipmentCollectionSnapshot,
    EquipmentItemSnapshot,
    EquipmentService,
    EquipmentServiceError,
)
from infrastructure.config.static import StaticGameConfig, get_static_config

_PAGE_SIZE = 25
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


class BackpackEntryKind(StrEnum):
    """背包统一实例类别。"""

    EQUIPMENT = "equipment"
    SKILL = "skill"


class BackpackFilterId(StrEnum):
    """背包筛选标识。"""

    ALL = "all"
    WEAPON = "weapon"
    ARMOR = "armor"
    ACCESSORY = "accessory"
    ARTIFACT = "artifact"
    SKILL = "skill"


class BackpackPanelQueryServiceError(RuntimeError):
    """背包查询基础异常。"""


class BackpackPanelStateError(BackpackPanelQueryServiceError):
    """背包状态异常。"""


@dataclass(frozen=True, slots=True)
class BackpackEntryKey:
    """背包实例唯一键。"""

    entry_kind: BackpackEntryKind
    item_id: int

    def serialize(self) -> str:
        """序列化为 Discord Select 可用的值。"""
        return f"{self.entry_kind.value}:{self.item_id}"

    @classmethod
    def parse(cls, raw_value: str) -> "BackpackEntryKey":
        """从 Discord Select 值解析实例键。"""
        normalized_value = raw_value.strip()
        kind_value, separator, item_id_value = normalized_value.partition(":")
        if not separator:
            raise BackpackPanelStateError(f"非法的背包实例键：{raw_value}")
        try:
            entry_kind = BackpackEntryKind(kind_value.strip())
            item_id = int(item_id_value.strip())
        except (ValueError, TypeError) as exc:
            raise BackpackPanelStateError(f"非法的背包实例键：{raw_value}") from exc
        if item_id <= 0:
            raise BackpackPanelStateError(f"非法的背包实例标识：{raw_value}")
        return cls(entry_kind=entry_kind, item_id=item_id)


@dataclass(frozen=True, slots=True)
class BackpackEntrySummarySnapshot:
    """背包当前列表中的实例摘要。"""

    entry_key: BackpackEntryKey
    entry_kind: BackpackEntryKind
    item_id: int
    slot_id: str
    slot_name: str
    display_name: str
    quality_name: str
    rank_name: str
    equipped: bool
    is_artifact: bool
    summary_line: str


@dataclass(frozen=True, slots=True)
class BackpackSelectedDetailSnapshot:
    """当前选中实例详情与同类对比对象。"""

    entry_key: BackpackEntryKey
    entry_kind: BackpackEntryKind
    equipment_item: EquipmentItemSnapshot | None = None
    skill_item: SkillPanelSkillSlotSnapshot | None = None
    equip_action_enabled: bool = False
    equip_action_label: str = "装配"
    same_type_equipped_entry_key: BackpackEntryKey | None = None
    same_type_equipped_equipment_item: EquipmentItemSnapshot | None = None
    same_type_equipped_skill_item: SkillPanelSkillSlotSnapshot | None = None
    is_same_as_equipped: bool = False


@dataclass(frozen=True, slots=True)
class BackpackPanelSnapshot:
    """背包面板只读快照。"""

    character_id: int
    character_name: str
    filter_id: BackpackFilterId
    page: int
    page_size: int
    total_items: int
    total_pages: int
    page_entries: tuple[BackpackEntrySummarySnapshot, ...]
    selected_detail: BackpackSelectedDetailSnapshot | None


class BackpackPanelQueryService:
    """聚合装备实例、法宝实例与功法实例的背包查询服务。"""

    def __init__(
        self,
        *,
        equipment_service: EquipmentService,
        profile_panel_query_service: ProfilePanelQueryService,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._equipment_service = equipment_service
        self._profile_panel_query_service = profile_panel_query_service
        self._static_config = static_config or get_static_config()
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
        filter_id: BackpackFilterId = BackpackFilterId.ALL,
        page: int = 1,
        selected_entry_key: BackpackEntryKey | None = None,
    ) -> BackpackPanelSnapshot:
        """读取背包面板快照。"""
        normalized_filter = self._normalize_filter_id(filter_id)
        normalized_page = max(1, int(page))
        try:
            collection = self._equipment_service.list_equipment(character_id=character_id)
            skill_snapshot = self._profile_panel_query_service.get_skill_snapshot(character_id=character_id)
        except (EquipmentServiceError, ProfilePanelQueryServiceError) as exc:
            raise BackpackPanelStateError(str(exc)) from exc

        equipment_items = tuple(item for item in collection.active_items if item.item_state == "active")
        skill_items = tuple(skill_snapshot.owned_skills)
        equipment_by_id = {item.item_id: item for item in equipment_items}
        skill_by_id = {item.item_id: item for item in skill_items}
        filtered_entries = self._build_filtered_entries(
            filter_id=normalized_filter,
            equipment_items=equipment_items,
            skill_items=skill_items,
            equipment_by_id=equipment_by_id,
            skill_by_id=skill_by_id,
        )
        total_items = len(filtered_entries)
        total_pages = max(1, (total_items + _PAGE_SIZE - 1) // _PAGE_SIZE)
        current_page = min(normalized_page, total_pages)
        page_entries = filtered_entries[(current_page - 1) * _PAGE_SIZE : current_page * _PAGE_SIZE]
        selected_detail = self._build_selected_detail(
            selected_entry_key=selected_entry_key,
            page_entries=page_entries,
            equipment_by_id=equipment_by_id,
            skill_by_id=skill_by_id,
            collection=collection,
            skill_snapshot=skill_snapshot,
        )
        return BackpackPanelSnapshot(
            character_id=character_id,
            character_name=skill_snapshot.character_name,
            filter_id=normalized_filter,
            page=current_page,
            page_size=_PAGE_SIZE,
            total_items=total_items,
            total_pages=total_pages,
            page_entries=page_entries,
            selected_detail=selected_detail,
        )

    @staticmethod
    def _normalize_filter_id(filter_id: BackpackFilterId | str) -> BackpackFilterId:
        if isinstance(filter_id, BackpackFilterId):
            return filter_id
        try:
            return BackpackFilterId(str(filter_id).strip())
        except ValueError as exc:
            raise BackpackPanelStateError(f"未支持的背包筛选：{filter_id}") from exc

    def _build_filtered_entries(
        self,
        *,
        filter_id: BackpackFilterId,
        equipment_items: tuple[EquipmentItemSnapshot, ...],
        skill_items: tuple[SkillPanelSkillSlotSnapshot, ...],
        equipment_by_id: dict[int, EquipmentItemSnapshot],
        skill_by_id: dict[int, SkillPanelSkillSlotSnapshot],
    ) -> tuple[BackpackEntrySummarySnapshot, ...]:
        equipment_entries = tuple(self._build_equipment_entry_summary(item=item) for item in equipment_items)
        skill_entries = tuple(self._build_skill_entry_summary(skill_item=skill_item) for skill_item in skill_items)

        if filter_id is BackpackFilterId.ALL:
            merged_entries = [*equipment_entries, *skill_entries]
            merged_entries.sort(
                key=lambda entry: self._build_all_entry_sort_key(
                    entry=entry,
                    equipment_by_id=equipment_by_id,
                    skill_by_id=skill_by_id,
                )
            )
            return tuple(merged_entries)

        if filter_id is BackpackFilterId.SKILL:
            filtered_skill_entries = [entry for entry in skill_entries]
            filtered_skill_entries.sort(key=lambda entry: self._build_skill_entry_sort_key(skill_item=skill_by_id[entry.item_id]))
            return tuple(filtered_skill_entries)

        filtered_equipment_entries = [
            entry
            for entry in equipment_entries
            if self._match_equipment_filter(item=equipment_by_id[entry.item_id], filter_id=filter_id)
        ]
        filtered_equipment_entries.sort(
            key=lambda entry: self._build_equipment_entry_sort_key(item=equipment_by_id[entry.item_id])
        )
        return tuple(filtered_equipment_entries)

    def _build_equipment_entry_summary(self, *, item: EquipmentItemSnapshot) -> BackpackEntrySummarySnapshot:
        is_equipped = item.equipped_slot_id == item.slot_id
        if item.is_artifact or item.slot_id == BackpackFilterId.ARTIFACT.value:
            summary_line = f"{item.quality_name}｜{item.rank_name}｜祭炼 {item.artifact_nurture_level}"
        else:
            summary_line = f"{item.quality_name}｜{item.rank_name}｜强化 +{item.enhancement_level}"
        return BackpackEntrySummarySnapshot(
            entry_key=BackpackEntryKey(entry_kind=BackpackEntryKind.EQUIPMENT, item_id=item.item_id),
            entry_kind=BackpackEntryKind.EQUIPMENT,
            item_id=item.item_id,
            slot_id=item.slot_id,
            slot_name=item.slot_name,
            display_name=item.display_name,
            quality_name=item.quality_name,
            rank_name=item.rank_name,
            equipped=is_equipped,
            is_artifact=item.is_artifact,
            summary_line=summary_line,
        )

    @staticmethod
    def _build_skill_entry_summary(*, skill_item: SkillPanelSkillSlotSnapshot) -> BackpackEntrySummarySnapshot:
        is_equipped = skill_item.equipped_slot_id == skill_item.slot_id
        return BackpackEntrySummarySnapshot(
            entry_key=BackpackEntryKey(entry_kind=BackpackEntryKind.SKILL, item_id=skill_item.item_id),
            entry_kind=BackpackEntryKind.SKILL,
            item_id=skill_item.item_id,
            slot_id=skill_item.slot_id,
            slot_name=skill_item.slot_name,
            display_name=skill_item.skill_name,
            quality_name=skill_item.quality_name,
            rank_name=skill_item.rank_name,
            equipped=is_equipped,
            is_artifact=False,
            summary_line=f"{skill_item.slot_name}｜{skill_item.rank_name}｜{skill_item.quality_name}",
        )

    def _build_all_entry_sort_key(
        self,
        *,
        entry: BackpackEntrySummarySnapshot,
        equipment_by_id: dict[int, EquipmentItemSnapshot],
        skill_by_id: dict[int, SkillPanelSkillSlotSnapshot],
    ) -> tuple[int, int, int, int, int, int]:
        if entry.entry_kind is BackpackEntryKind.EQUIPMENT:
            item = equipment_by_id[entry.item_id]
            category_order = _EQUIPMENT_CATEGORY_ORDER.get(self._resolve_equipment_category(item), len(_EQUIPMENT_CATEGORY_ORDER))
            equipment_key = self._build_equipment_entry_sort_key(item=item)
            return (category_order, *equipment_key)
        skill_item = skill_by_id[entry.item_id]
        skill_key = self._build_skill_entry_sort_key(skill_item=skill_item)
        return (len(_EQUIPMENT_CATEGORY_ORDER), *skill_key)

    def _build_equipment_entry_sort_key(self, *, item: EquipmentItemSnapshot) -> tuple[int, int, int, int]:
        return (
            0 if item.equipped_slot_id == item.slot_id else 1,
            -self._equipment_quality_order_by_id.get(item.quality_id, 0),
            -item.enhancement_level,
            -item.item_id,
        )

    def _build_skill_entry_sort_key(self, *, skill_item: SkillPanelSkillSlotSnapshot) -> tuple[int, int, int, int]:
        return (
            0 if skill_item.equipped_slot_id == skill_item.slot_id else 1,
            _SKILL_SLOT_ORDER.get(skill_item.slot_id, len(_SKILL_SLOT_ORDER)),
            -self._skill_quality_order_by_id.get(skill_item.quality_id, 0),
            -skill_item.item_id,
        )

    @staticmethod
    def _resolve_equipment_category(item: EquipmentItemSnapshot) -> str:
        if item.is_artifact or item.slot_id == BackpackFilterId.ARTIFACT.value:
            return BackpackFilterId.ARTIFACT.value
        return item.slot_id

    @staticmethod
    def _match_equipment_filter(*, item: EquipmentItemSnapshot, filter_id: BackpackFilterId) -> bool:
        if filter_id is BackpackFilterId.WEAPON:
            return item.slot_id == BackpackFilterId.WEAPON.value and not item.is_artifact
        if filter_id is BackpackFilterId.ARMOR:
            return item.slot_id == BackpackFilterId.ARMOR.value and not item.is_artifact
        if filter_id is BackpackFilterId.ACCESSORY:
            return item.slot_id == BackpackFilterId.ACCESSORY.value and not item.is_artifact
        if filter_id is BackpackFilterId.ARTIFACT:
            return item.slot_id == BackpackFilterId.ARTIFACT.value or item.is_artifact
        return False

    def _build_selected_detail(
        self,
        *,
        selected_entry_key: BackpackEntryKey | None,
        page_entries: tuple[BackpackEntrySummarySnapshot, ...],
        equipment_by_id: dict[int, EquipmentItemSnapshot],
        skill_by_id: dict[int, SkillPanelSkillSlotSnapshot],
        collection: EquipmentCollectionSnapshot,
        skill_snapshot: SkillPanelSnapshot,
    ) -> BackpackSelectedDetailSnapshot | None:
        if selected_entry_key is None:
            return None
        selected_entry = next((entry for entry in page_entries if entry.entry_key == selected_entry_key), None)
        if selected_entry is None:
            return None
        if selected_entry.entry_kind is BackpackEntryKind.EQUIPMENT:
            equipment_item = equipment_by_id.get(selected_entry.item_id)
            if equipment_item is None:
                return None
            same_type_equipped_item = self._resolve_same_slot_equipped_equipment(
                collection=collection,
                slot_id=equipment_item.slot_id,
            )
            is_same_as_equipped = (
                same_type_equipped_item is not None and same_type_equipped_item.item_id == equipment_item.item_id
            )
            return BackpackSelectedDetailSnapshot(
                entry_key=selected_entry.entry_key,
                entry_kind=selected_entry.entry_kind,
                equipment_item=equipment_item,
                equip_action_enabled=not is_same_as_equipped,
                equip_action_label="已装备" if is_same_as_equipped else "装配",
                same_type_equipped_entry_key=(
                    None
                    if same_type_equipped_item is None
                    else BackpackEntryKey(
                        entry_kind=BackpackEntryKind.EQUIPMENT,
                        item_id=same_type_equipped_item.item_id,
                    )
                ),
                same_type_equipped_equipment_item=same_type_equipped_item,
                is_same_as_equipped=is_same_as_equipped,
            )

        skill_item = skill_by_id.get(selected_entry.item_id)
        if skill_item is None:
            return None
        same_type_equipped_skill = self._resolve_same_slot_equipped_skill(
            skill_snapshot=skill_snapshot,
            slot_id=skill_item.slot_id,
        )
        is_same_as_equipped = same_type_equipped_skill is not None and same_type_equipped_skill.item_id == skill_item.item_id
        return BackpackSelectedDetailSnapshot(
            entry_key=selected_entry.entry_key,
            entry_kind=selected_entry.entry_kind,
            skill_item=skill_item,
            equip_action_enabled=not is_same_as_equipped,
            equip_action_label="已装配" if is_same_as_equipped else "装配",
            same_type_equipped_entry_key=(
                None
                if same_type_equipped_skill is None
                else BackpackEntryKey(
                    entry_kind=BackpackEntryKind.SKILL,
                    item_id=same_type_equipped_skill.item_id,
                )
            ),
            same_type_equipped_skill_item=same_type_equipped_skill,
            is_same_as_equipped=is_same_as_equipped,
        )

    @staticmethod
    def _resolve_same_slot_equipped_equipment(
        *,
        collection: EquipmentCollectionSnapshot,
        slot_id: str,
    ) -> EquipmentItemSnapshot | None:
        for item in collection.equipped_items:
            if item.item_state == "active" and item.slot_id == slot_id and item.equipped_slot_id == slot_id:
                return item
        return None

    @staticmethod
    def _resolve_same_slot_equipped_skill(
        *,
        skill_snapshot: SkillPanelSnapshot,
        slot_id: str,
    ) -> SkillPanelSkillSlotSnapshot | None:
        if slot_id == "main":
            return skill_snapshot.main_skill
        for skill_item in skill_snapshot.auxiliary_skills:
            if skill_item.slot_id == slot_id:
                return skill_item
        return None


__all__ = [
    "BackpackEntryKey",
    "BackpackEntryKind",
    "BackpackEntrySummarySnapshot",
    "BackpackFilterId",
    "BackpackPanelQueryService",
    "BackpackPanelQueryServiceError",
    "BackpackPanelSnapshot",
    "BackpackPanelStateError",
    "BackpackSelectedDetailSnapshot",
]
