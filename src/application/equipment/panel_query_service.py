"""装备面板只读查询适配。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from application.character.profile_panel_query_service import (
    ProfilePanelQueryService,
    ProfilePanelQueryServiceError,
    SkillPanelSnapshot,
)
from application.equipment.equipment_service import (
    EquipmentCollectionSnapshot,
    EquipmentItemSnapshot,
    EquipmentService,
    EquipmentServiceError,
)
from application.naming import ItemNamingBatchService
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import DropRecord
from infrastructure.db.repositories import BattleRecordRepository, SkillRepository

_ENDLESS_SOURCE_TYPE = "endless"
_BREAKTHROUGH_SOURCE_TYPE = "breakthrough_trial"
_ENDLESS_EQUIPMENT_ENTRY_TYPE = "equipment_drop"
_ENDLESS_ARTIFACT_ENTRY_TYPE = "artifact_drop"
_ENDLESS_SKILL_ENTRY_TYPE = "skill_drop"
_RESOURCE_NAME_BY_ID = {
    "spirit_stone": "灵石",
    "enhancement_stone": "强化石",
    "enhancement_shard": "强化碎晶",
    "wash_dust": "洗炼尘",
    "spirit_sand": "灵砂",
    "spirit_pattern_stone": "灵纹石",
    "soul_binding_jade": "缚魂玉",
    "artifact_essence": "法宝精粹",
    "foundation_pill": "筑基丹",
    "nascent_soul_flower": "元婴花",
    "cultivation": "修为",
    "insight": "感悟",
    "refining_essence": "祭炼精华",
}
_AUXILIARY_SLOT_NAME_BY_ID = {
    "guard": "护体",
    "movement": "身法",
    "spirit": "神识",
}
_DROP_SOURCE_LABEL_BY_TYPE = {
    _ENDLESS_SOURCE_TYPE: "无涯渊境",
    _BREAKTHROUGH_SOURCE_TYPE: "突破秘境",
}
_ENDLESS_REWARD_NAME_BY_KEY = {
    "cultivation": "修为",
    "insight": "感悟",
    "refining_essence": "祭炼精华",
    "equipment_score": "装备分",
    "artifact_score": "法宝分",
    "dao_pattern_score": "道纹分",
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


@dataclass(frozen=True, slots=True)
class EquipmentCardSnapshot:
    """可直接渲染的装备卡片快照。"""

    name: str
    badge_line: str
    growth_line: str | None
    stat_lines: tuple[str, ...]
    keyword_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EquipmentSlotPanelSnapshot:
    """单个装备部位展示快照。"""

    slot_id: str
    slot_name: str
    core_role: str
    equipped_item: EquipmentItemSnapshot | None
    candidate_items: tuple[EquipmentItemSnapshot, ...]
    equipped_card: EquipmentCardSnapshot | None = None
    candidate_cards: tuple[EquipmentCardSnapshot, ...] = ()


@dataclass(frozen=True, slots=True)
class EquipmentDropSummary:
    """最近一次装备相关获取摘要。"""

    source_type: str
    source_label: str
    source_ref: str | None
    occurred_at: datetime
    item_lines: tuple[str, ...]
    currency_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EquipmentPanelSnapshot:
    """装备总览面板所需快照。"""

    character_id: int
    spirit_stone: int
    collection: EquipmentCollectionSnapshot
    slot_panels: tuple[EquipmentSlotPanelSnapshot, ...]
    skill_snapshot: SkillPanelSnapshot
    latest_drop: EquipmentDropSummary | None


class EquipmentPanelQueryServiceError(RuntimeError):
    """装备面板查询基础异常。"""


class EquipmentPanelStateError(EquipmentPanelQueryServiceError):
    """装备面板查询状态异常。"""


class EquipmentPanelQueryService:
    """聚合装备、法宝、功法与最近获取摘要。"""

    def __init__(
        self,
        *,
        equipment_service: EquipmentService,
        profile_panel_query_service: ProfilePanelQueryService,
        battle_record_repository: BattleRecordRepository,
        skill_repository: SkillRepository | None = None,
        static_config: StaticGameConfig | None = None,
        naming_batch_service: ItemNamingBatchService | None = None,
    ) -> None:
        self._equipment_service = equipment_service
        self._profile_panel_query_service = profile_panel_query_service
        self._battle_record_repository = battle_record_repository
        self._static_config = static_config or get_static_config()
        self._skill_repository = skill_repository or self._build_fallback_skill_repository(profile_panel_query_service)
        self._naming_batch_service = naming_batch_service
        self._slot_definitions = tuple(self._static_config.equipment.ordered_slots)
        self._quality_order_by_id = {
            quality.quality_id: quality.order for quality in self._static_config.equipment.qualities
        }

    def get_panel_snapshot(self, *, character_id: int) -> EquipmentPanelSnapshot:
        """读取装备面板总览快照。"""
        try:
            collection = self._equipment_service.list_equipment(character_id=character_id)
            skill_snapshot = self._profile_panel_query_service.get_skill_snapshot(character_id=character_id)
        except (EquipmentServiceError, ProfilePanelQueryServiceError) as exc:
            raise EquipmentPanelStateError(str(exc)) from exc

        return EquipmentPanelSnapshot(
            character_id=character_id,
            spirit_stone=collection.spirit_stone,
            collection=collection,
            slot_panels=self._build_slot_panels(collection=collection),
            skill_snapshot=skill_snapshot,
            latest_drop=self._load_latest_drop_summary(character_id=character_id),
        )

    def has_owned_skill_item(self, *, character_id: int, skill_item_id: int) -> bool:
        """判断角色是否仍持有指定功法实例。"""
        if skill_item_id <= 0:
            return False
        return self._skill_repository.get_skill_item_by_character_and_id(character_id, skill_item_id) is not None

    def _build_slot_panels(self, *, collection: EquipmentCollectionSnapshot) -> tuple[EquipmentSlotPanelSnapshot, ...]:
        equipped_by_slot = {
            item.slot_id: item for item in collection.equipped_items if item.item_state == "active"
        }
        active_by_slot: dict[str, list[EquipmentItemSnapshot]] = defaultdict(list)
        for item in collection.active_items:
            if item.item_state != "active":
                continue
            active_by_slot[item.slot_id].append(item)

        for items in active_by_slot.values():
            items.sort(key=self._candidate_sort_key)

        return tuple(
            EquipmentSlotPanelSnapshot(
                slot_id=slot.slot_id,
                slot_name=slot.name,
                core_role=slot.core_role,
                equipped_item=equipped_by_slot.get(slot.slot_id),
                candidate_items=tuple(active_by_slot.get(slot.slot_id, [])),
                equipped_card=(
                    None
                    if equipped_by_slot.get(slot.slot_id) is None
                    else self._build_item_card(equipped_by_slot[slot.slot_id])
                ),
                candidate_cards=tuple(self._build_item_card(item) for item in active_by_slot.get(slot.slot_id, [])),
            )
            for slot in self._slot_definitions
        )

    def _build_item_card(self, item: EquipmentItemSnapshot) -> EquipmentCardSnapshot:
        stats = item.resolved_stats if item.resolved_stats else item.base_attributes
        stat_lines = tuple(
            f"{self._format_stat_name(stat.stat_id)} {self._format_stat_value(stat.stat_id, stat.value)}"
            for stat in stats[:4]
        )
        keyword_lines = tuple(self._format_affix_keyword(affix) for affix in item.affixes[:3])
        growth_parts = [f"强化 +{item.enhancement_level}"]
        if item.is_artifact:
            growth_parts.append(f"祭炼 {item.artifact_nurture_level}")
        return EquipmentCardSnapshot(
            name=item.display_name,
            badge_line=f"{'法宝' if item.is_artifact else item.slot_name}｜{item.rank_name}｜{item.quality_name}",
            growth_line="｜".join(growth_parts),
            stat_lines=stat_lines or ("暂无关键属性",),
            keyword_lines=keyword_lines,
        )

    @staticmethod
    def _format_affix_keyword(affix) -> str:
        if affix.affix_kind == "special_effect" or affix.special_effect is not None or not affix.stat_id.strip():
            return affix.affix_name
        return f"{affix.affix_name} {EquipmentPanelQueryService._format_stat_value(affix.stat_id, affix.value)}"

    @staticmethod
    def _format_stat_name(stat_id: str) -> str:
        return _STAT_NAME_BY_ID.get(stat_id, stat_id)

    @staticmethod
    def _format_stat_value(stat_id: str, value: int) -> str:
        if stat_id.endswith("_permille"):
            return f"{value / 10:.1f}%"
        return str(value)

    def _load_latest_drop_summary(self, *, character_id: int) -> EquipmentDropSummary | None:
        records = self._battle_record_repository.list_drop_records(character_id)
        if not records:
            return None
        record = records[0]
        return EquipmentDropSummary(
            source_type=record.source_type,
            source_label=_DROP_SOURCE_LABEL_BY_TYPE.get(record.source_type, record.source_type or "未知来源"),
            source_ref=record.source_ref,
            occurred_at=record.occurred_at,
            item_lines=self._build_item_lines(record=record),
            currency_lines=self._build_currency_lines(record=record),
        )

    def _build_item_lines(self, *, record: DropRecord) -> tuple[str, ...]:
        if record.source_type == _ENDLESS_SOURCE_TYPE:
            return self._build_endless_drop_lines(record=record)
        if record.source_type == _BREAKTHROUGH_SOURCE_TYPE:
            return self._build_breakthrough_drop_lines(record=record)
        lines: list[str] = []
        for item in record.items_json:
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("item_id") or item.get("entry_type") or "未知条目")
            quantity = int(item.get("quantity") or 0)
            if quantity > 0:
                lines.append(f"{item_id} ×{quantity}")
            else:
                lines.append(item_id)
        return tuple(lines)

    def _build_endless_drop_lines(self, *, record: DropRecord) -> tuple[str, ...]:
        instance_lines: list[str] = []
        bundle_lines: list[str] = []
        other_lines: list[str] = []
        entries = self._refresh_endless_entries(character_id=record.character_id, entries=record.items_json)
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            entry_type = str(entry.get("entry_type") or "")
            if entry_type == "stable_reward_bundle":
                parts = self._format_reward_mapping(_normalize_int_mapping(entry.get("settled")))
                if parts:
                    bundle_lines.append("稳定资源：" + "｜".join(parts))
                continue
            if entry_type == "pending_reward_bundle":
                parts = self._format_reward_mapping(_normalize_int_mapping(entry.get("settled")))
                if parts:
                    bundle_lines.append("未稳掉落：" + "｜".join(parts))
                continue
            if entry_type in {_ENDLESS_EQUIPMENT_ENTRY_TYPE, _ENDLESS_ARTIFACT_ENTRY_TYPE}:
                line = self._build_endless_instance_line(entry=entry)
                if line is not None:
                    instance_lines.append(line)
                continue
            if entry_type == _ENDLESS_SKILL_ENTRY_TYPE:
                line = self._build_endless_skill_line(entry=entry)
                if line is not None:
                    other_lines.append(line)
                continue
            settled = _normalize_int_mapping(entry.get("settled"))
            parts = self._format_reward_mapping(settled)
            if parts:
                other_lines.append("掉落摘要：" + "｜".join(parts))
        return tuple(instance_lines + other_lines + bundle_lines)

    @staticmethod
    def _build_endless_instance_line(*, entry: Mapping[str, object]) -> str | None:
        entry_type = str(entry.get("entry_type") or "")
        is_artifact = bool(entry.get("is_artifact")) or entry_type == _ENDLESS_ARTIFACT_ENTRY_TYPE
        display_name = str(entry.get("display_name") or entry.get("template_name") or "").strip()
        if not display_name:
            return None
        parts = [display_name]
        quality_name = str(entry.get("quality_name") or "").strip()
        rank_name = str(entry.get("rank_name") or "").strip()
        slot_name = str(entry.get("slot_name") or "").strip()
        resonance_name = str(entry.get("resonance_name") or "").strip()
        if quality_name:
            parts.append(quality_name)
        if rank_name:
            parts.append(rank_name)
        if slot_name and not is_artifact:
            parts.append(slot_name)
        if resonance_name:
            parts.append(f"共鸣 {resonance_name}")
        prefix = "法宝实例" if is_artifact else "装备实例"
        return prefix + "：" + "｜".join(parts)

    @staticmethod
    def _build_endless_skill_line(*, entry: Mapping[str, object]) -> str | None:
        skill_name = str(entry.get("skill_name") or "").strip()
        if not skill_name:
            return None
        parts = [skill_name]
        rank_name = str(entry.get("rank_name") or "").strip()
        quality_name = str(entry.get("quality_name") or "").strip()
        skill_type = str(entry.get("skill_type") or "").strip()
        auxiliary_slot_id = str(entry.get("auxiliary_slot_id") or "").strip()
        if rank_name:
            parts.append(rank_name)
        if quality_name:
            parts.append(quality_name)
        if skill_type == "auxiliary" and auxiliary_slot_id:
            parts.append(f"辅位 {_AUXILIARY_SLOT_NAME_BY_ID.get(auxiliary_slot_id, '未知辅位')}")
        return "功法实例：" + "｜".join(parts)

    @staticmethod
    def _build_breakthrough_drop_lines(*, record: DropRecord) -> tuple[str, ...]:
        lines: list[str] = []
        for item in record.items_json:
            if not isinstance(item, Mapping):
                continue
            item_id = str(item.get("item_id") or "未知材料")
            quantity = int(item.get("quantity") or 0)
            bound = bool(item.get("bound"))
            prefix = "绑定" if bound else "未绑定"
            item_name = _RESOURCE_NAME_BY_ID.get(item_id, item_id)
            lines.append(f"{prefix} {item_name} ×{quantity}")
        return tuple(lines)

    @staticmethod
    def _build_currency_lines(*, record: DropRecord) -> tuple[str, ...]:
        mapping = _normalize_int_mapping(record.currencies_json)
        return tuple(
            f"{_RESOURCE_NAME_BY_ID.get(resource_id, resource_id)} +{quantity}"
            for resource_id, quantity in mapping.items()
            if quantity > 0
        )

    def _refresh_endless_entries(
        self,
        *,
        character_id: int,
        entries: Sequence[object],
    ) -> tuple[Mapping[str, object], ...]:
        normalized_entries = tuple(entry for entry in entries if isinstance(entry, Mapping))
        if self._naming_batch_service is None:
            return normalized_entries
        return self._naming_batch_service.refresh_drop_entries(
            character_id=character_id,
            entries=normalized_entries,
        )

    def _format_reward_mapping(self, reward_mapping: Mapping[str, int]) -> list[str]:
        parts: list[str] = []
        for key, quantity in reward_mapping.items():
            if quantity <= 0:
                continue
            parts.append(f"{_ENDLESS_REWARD_NAME_BY_KEY.get(key, key)} +{quantity}")
        return parts

    def _candidate_sort_key(self, item: EquipmentItemSnapshot) -> tuple[int, int, int, int]:
        quality_order = self._quality_order_by_id.get(item.quality_id, 0)
        equipped_bonus = 1 if item.equipped_slot_id is not None else 0
        return (-quality_order, -item.enhancement_level, -equipped_bonus, -item.item_id)

    @staticmethod
    def _build_fallback_skill_repository(profile_panel_query_service: ProfilePanelQueryService) -> SkillRepository:
        skill_loadout_service = getattr(profile_panel_query_service, "_skill_loadout_service", None)
        skill_repository = None if skill_loadout_service is None else getattr(skill_loadout_service, "_skill_repository", None)
        if skill_repository is None:
            raise ValueError("EquipmentPanelQueryService 缺少 skill_repository，且无法从 profile_panel_query_service 推导仓储")
        return skill_repository


def _normalize_int_mapping(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, int] = {}
    for key, raw in value.items():
        try:
            quantity = int(raw)
        except (TypeError, ValueError):
            continue
        normalized[str(key)] = quantity
    return normalized


__all__ = [
    "EquipmentCardSnapshot",
    "EquipmentDropSummary",
    "EquipmentPanelQueryService",
    "EquipmentPanelQueryServiceError",
    "EquipmentPanelSnapshot",
    "EquipmentPanelStateError",
    "EquipmentSlotPanelSnapshot",
]
