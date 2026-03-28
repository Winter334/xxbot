"""功法运行时共享支持。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import random
from typing import Any

from domain.battle import (
    ActionNumericBonusPatch,
    ActionNumericField,
    ActionPatchSelector,
    ActionThresholdField,
    ActionThresholdShiftPatch,
    ActionTriggerCapAdjustment,
    AuxiliarySkillParameterPatch,
)
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.config.static.models.skill import (
    SkillLineageDefinition,
    SkillPatchDefinition,
    SkillPathDefinition,
    SkillQualityDefinition,
    SkillRankDefinition,
)
from infrastructure.db.models import CharacterSkillItem, CharacterSkillLoadout
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, SkillRepository

_ITEM_STATE_EQUIPPED = "equipped"
_ITEM_STATE_INVENTORY = "inventory"
_MAIN_SLOT_ID = "main"
_STARTER_LINEAGE_BY_SLOT_ID: dict[str, str] = {
    _MAIN_SLOT_ID: "seven_kill_sword",
    "guard": "golden_bell_guard",
    "movement": "wind_shadow_step",
    "spirit": "sword_heart_lock",
}
_FLAT_ATTRIBUTE_VALUE_BY_STAT_ID: dict[str, int] = {
    "max_hp": 18,
    "attack_power": 2,
    "guard_power": 2,
    "speed": 1,
    "crit_rate_permille": 12,
    "crit_damage_bonus_permille": 28,
    "dodge_rate_permille": 12,
    "damage_bonus_permille": 10,
    "damage_reduction_permille": 8,
    "counter_rate_permille": 12,
    "control_hit_permille": 12,
    "control_resist_permille": 12,
    "heal_power": 14,
}


@dataclass(frozen=True, slots=True)
class SkillInventoryItemSnapshot:
    """单件功法实例的稳定快照。"""

    item_id: int
    character_id: int
    lineage_id: str
    skill_name: str
    path_id: str
    axis_id: str
    skill_type: str
    auxiliary_slot_id: str | None
    rank_id: str
    rank_name: str
    rank_order: int
    quality_id: str
    quality_name: str
    naming_source: str
    naming_metadata: dict[str, Any]
    total_budget: int
    resolved_attributes: dict[str, int]
    resolved_patch_ids: tuple[str, ...]
    item_state: str
    equipped_slot_id: str | None


@dataclass(frozen=True, slots=True)
class CharacterSkillLoadoutSnapshot:
    """角色当前功法装配快照。"""

    character_id: int
    main_axis_id: str
    main_path_id: str
    behavior_template_id: str
    main_skill: SkillInventoryItemSnapshot
    guard_skill: SkillInventoryItemSnapshot
    movement_skill: SkillInventoryItemSnapshot
    spirit_skill: SkillInventoryItemSnapshot
    config_version: str | None


@dataclass(frozen=True, slots=True)
class ResolvedCharacterSkillState:
    """角色功法运行时状态。"""

    aggregate: CharacterAggregate
    loadout_model: CharacterSkillLoadout
    loadout_snapshot: CharacterSkillLoadoutSnapshot
    items_by_id: dict[int, CharacterSkillItem]


class SkillRuntimeSupport:
    """封装功法实例生成、默认装配与运行时解析。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        skill_repository: SkillRepository,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._skill_repository = skill_repository
        self._static_config = static_config or get_static_config()
        self._path_by_id = {
            path.path_id: path
            for path in self._static_config.skill_paths.paths
        }
        self._lineage_by_id = {
            lineage.lineage_id: lineage
            for lineage in self._static_config.skill_lineages.lineages
        }
        self._rank_by_id = {
            rank.rank_id: rank
            for rank in self._static_config.skill_generation.ranks
        }
        self._quality_by_id = {
            quality.quality_id: quality
            for quality in self._static_config.skill_generation.qualities
        }
        self._attribute_pool_by_id = {
            pool.pool_id: pool
            for pool in self._static_config.skill_generation.attribute_pools
        }
        self._patch_by_id = {
            patch.patch_id: patch
            for patch in self._static_config.skill_generation.patches
        }
        self._patch_pool_by_id = {
            pool.pool_id: pool
            for pool in self._static_config.skill_generation.patch_pools
        }
        self._quality_order_by_id = {
            quality.quality_id: quality.order
            for quality in self._static_config.skill_generation.qualities
        }

    def ensure_skill_state(
        self,
        *,
        character_id: int,
        occurred_at: datetime | None = None,
    ) -> ResolvedCharacterSkillState:
        """确保角色存在可用的完整功法装配。"""
        aggregate = self._require_aggregate(character_id)
        current_time = occurred_at or datetime.utcnow()
        items_by_id = {
            item.id: item
            for item in self._skill_repository.list_skill_items_by_character_id(character_id)
        }
        loadout = self._skill_repository.get_skill_loadout(character_id)
        changed = False

        if not items_by_id:
            for slot_id in (_MAIN_SLOT_ID, "guard", "movement", "spirit"):
                item = self._grant_starter_item(
                    character_id=character_id,
                    slot_id=slot_id,
                    occurred_at=current_time,
                )
                items_by_id[item.id] = item
            changed = True

        if loadout is None:
            loadout = CharacterSkillLoadout(
                character_id=character_id,
                main_skill_id=None,
                guard_skill_id=None,
                movement_skill_id=None,
                spirit_skill_id=None,
                main_axis_id=None,
                main_path_id=None,
                behavior_template_id=None,
                config_version=self._static_config.skill_generation.config_version,
                loadout_notes_json={},
            )
            aggregate.character.skill_loadout = loadout
            changed = True

        main_item, main_created = self._resolve_or_create_slot_item(
            character_id=character_id,
            slot_id=_MAIN_SLOT_ID,
            preferred_item_id=loadout.main_skill_id,
            items_by_id=items_by_id,
            occurred_at=current_time,
        )
        guard_item, guard_created = self._resolve_or_create_slot_item(
            character_id=character_id,
            slot_id="guard",
            preferred_item_id=loadout.guard_skill_id,
            items_by_id=items_by_id,
            occurred_at=current_time,
        )
        movement_item, movement_created = self._resolve_or_create_slot_item(
            character_id=character_id,
            slot_id="movement",
            preferred_item_id=loadout.movement_skill_id,
            items_by_id=items_by_id,
            occurred_at=current_time,
        )
        spirit_item, spirit_created = self._resolve_or_create_slot_item(
            character_id=character_id,
            slot_id="spirit",
            preferred_item_id=loadout.spirit_skill_id,
            items_by_id=items_by_id,
            occurred_at=current_time,
        )
        changed = changed or any((main_created, guard_created, movement_created, spirit_created))
        changed = self._assign_loadout(
            loadout=loadout,
            main_item=main_item,
            guard_item=guard_item,
            movement_item=movement_item,
            spirit_item=spirit_item,
        ) or changed
        changed = self._synchronize_item_states(
            items_by_id=items_by_id,
            loadout=loadout,
            occurred_at=current_time,
        ) or changed

        if changed:
            self._skill_repository.save_skill_loadout(loadout)

        loadout_snapshot = self._build_loadout_snapshot(
            loadout=loadout,
            items_by_id=items_by_id,
        )
        return ResolvedCharacterSkillState(
            aggregate=aggregate,
            loadout_model=loadout,
            loadout_snapshot=loadout_snapshot,
            items_by_id=items_by_id,
        )

    def list_skill_item_snapshots(self, *, character_id: int) -> tuple[SkillInventoryItemSnapshot, ...]:
        """返回角色已拥有的全部功法快照。"""
        state = self.ensure_skill_state(character_id=character_id)
        loadout = state.loadout_model
        return tuple(
            self.build_item_snapshot(item=item, loadout=loadout)
            for item in sorted(state.items_by_id.values(), key=self._sort_item_key)
        )

    def get_loadout_snapshot(self, *, character_id: int) -> CharacterSkillLoadoutSnapshot:
        """返回角色当前装配快照。"""
        state = self.ensure_skill_state(character_id=character_id)
        return state.loadout_snapshot

    def generate_skill_item(
        self,
        *,
        character_id: int,
        lineage_id: str,
        rank_id: str,
        quality_id: str,
        source_type: str,
        source_record_id: str | None = None,
        seed: int | None = None,
    ) -> CharacterSkillItem:
        """按给定谱系、阶数与品质生成一件随机功法实例。"""
        lineage = self.require_lineage(lineage_id)
        rank = self.require_rank(rank_id)
        quality = self.require_quality(quality_id)
        random_source = random.Random(seed)
        total_budget = self._roll_total_budget(
            lineage=lineage,
            rank=rank,
            quality=quality,
            random_source=random_source,
        )
        distribution = self._build_random_distribution(
            lineage=lineage,
            total_budget=total_budget,
            random_source=random_source,
        )
        resolved_attributes = self._resolve_attributes(distribution)
        resolved_patches = self._resolve_patch_payloads(lineage)
        fallback_metadata = self._build_fallback_naming_metadata(
            lineage_id=lineage.lineage_id,
            rank_id=rank.rank_id,
            quality_id=quality.quality_id,
            source_type=source_type,
            source_record_id=source_record_id,
        )
        skill_item = CharacterSkillItem(
            character_id=character_id,
            lineage_id=lineage.lineage_id,
            path_id=lineage.path_id,
            axis_id=self.require_path(lineage.path_id).axis_id,
            skill_type=lineage.skill_type,
            auxiliary_slot_id=lineage.auxiliary_slot_id,
            skill_name=lineage.name,
            naming_source="lineage_static",
            naming_metadata_json=fallback_metadata,
            rank_id=rank.rank_id,
            rank_name=rank.name,
            rank_order=rank.order,
            quality_id=quality.quality_id,
            quality_name=quality.name,
            total_budget=total_budget,
            budget_distribution_json=distribution,
            resolved_attributes_json=resolved_attributes,
            resolved_patches_json=resolved_patches,
            source_type=source_type,
            source_record_id=source_record_id,
            is_locked=False,
            item_state=_ITEM_STATE_INVENTORY,
            equipped_at=None,
            unequipped_at=None,
        )
        return self._skill_repository.add_skill_item(skill_item)

    def build_item_snapshot(
        self,
        *,
        item: CharacterSkillItem,
        loadout: CharacterSkillLoadout | None,
    ) -> SkillInventoryItemSnapshot:
        """把 ORM 功法实例转换为稳定快照。"""
        return SkillInventoryItemSnapshot(
            item_id=item.id,
            character_id=item.character_id,
            lineage_id=item.lineage_id,
            skill_name=item.skill_name,
            path_id=item.path_id,
            axis_id=item.axis_id,
            skill_type=item.skill_type,
            auxiliary_slot_id=item.auxiliary_slot_id,
            rank_id=item.rank_id,
            rank_name=item.rank_name,
            rank_order=item.rank_order,
            quality_id=item.quality_id,
            quality_name=item.quality_name,
            naming_source=str(item.naming_source or "lineage_static"),
            naming_metadata=dict(item.naming_metadata_json) if isinstance(item.naming_metadata_json, dict) else {},
            total_budget=item.total_budget,
            resolved_attributes=dict(item.resolved_attributes_json) if isinstance(item.resolved_attributes_json, dict) else {},
            resolved_patch_ids=tuple(self._extract_patch_ids(item)),
            item_state=item.item_state,
            equipped_slot_id=self.resolve_equipped_slot_id(loadout=loadout, item_id=item.id),
        )

    def collect_resolved_attribute_values(self, *, item: CharacterSkillItem) -> dict[str, int]:
        """汇总单件功法实例提供的全部属性修正。"""
        resolved_values = dict(item.resolved_attributes_json) if isinstance(item.resolved_attributes_json, dict) else {}
        for payload in self._normalize_patch_payloads(item):
            if payload.get("patch_kind") != "attribute_bonus":
                continue
            if payload.get("operation") != "add_flat":
                continue
            stat_id = str(payload.get("target_key") or "").strip()
            if not stat_id:
                continue
            resolved_values[stat_id] = resolved_values.get(stat_id, 0) + _read_int(payload.get("value"))
        return resolved_values

    def build_template_patches(self, *, item: CharacterSkillItem) -> tuple[AuxiliarySkillParameterPatch, ...]:
        """把功法实例中的模板补丁载荷转换为战斗领域对象。"""
        patches: list[AuxiliarySkillParameterPatch] = []
        for payload in self._normalize_patch_payloads(item):
            patch = self._build_template_patch_from_payload(payload)
            if patch is None:
                continue
            patches.append(patch)
        return tuple(patches)

    def require_lineage(self, lineage_id: str) -> SkillLineageDefinition:
        """读取指定功法谱系定义。"""
        try:
            return self._lineage_by_id[lineage_id]
        except KeyError as exc:
            raise LookupError(f"未配置的功法谱系：{lineage_id}") from exc

    def require_rank(self, rank_id: str) -> SkillRankDefinition:
        """读取指定功法阶数定义。"""
        try:
            return self._rank_by_id[rank_id]
        except KeyError as exc:
            raise LookupError(f"未配置的功法阶数：{rank_id}") from exc

    def require_quality(self, quality_id: str) -> SkillQualityDefinition:
        """读取指定功法品质定义。"""
        try:
            return self._quality_by_id[quality_id]
        except KeyError as exc:
            raise LookupError(f"未配置的功法品质：{quality_id}") from exc

    def require_path(self, path_id: str) -> SkillPathDefinition:
        """读取指定功法流派定义。"""
        try:
            return self._path_by_id[path_id]
        except KeyError as exc:
            raise LookupError(f"未配置的功法流派：{path_id}") from exc

    @staticmethod
    def resolve_equipped_slot_id(
        *,
        loadout: CharacterSkillLoadout | None,
        item_id: int,
    ) -> str | None:
        """解析单件功法实例当前位于哪个装配槽位。"""
        if loadout is None:
            return None
        if loadout.main_skill_id == item_id:
            return _MAIN_SLOT_ID
        if loadout.guard_skill_id == item_id:
            return "guard"
        if loadout.movement_skill_id == item_id:
            return "movement"
        if loadout.spirit_skill_id == item_id:
            return "spirit"
        return None

    def _require_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise LookupError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise RuntimeError(f"角色缺少成长状态：{character_id}")
        return aggregate

    def _resolve_or_create_slot_item(
        self,
        *,
        character_id: int,
        slot_id: str,
        preferred_item_id: int | None,
        items_by_id: dict[int, CharacterSkillItem],
        occurred_at: datetime,
    ) -> tuple[CharacterSkillItem, bool]:
        item = self._pick_item_for_slot(
            items=items_by_id.values(),
            slot_id=slot_id,
            preferred_item_id=preferred_item_id,
        )
        if item is not None:
            return item, False
        created_item = self._grant_starter_item(
            character_id=character_id,
            slot_id=slot_id,
            occurred_at=occurred_at,
        )
        items_by_id[created_item.id] = created_item
        return created_item, True

    def _pick_item_for_slot(
        self,
        *,
        items,
        slot_id: str,
        preferred_item_id: int | None,
    ) -> CharacterSkillItem | None:
        if preferred_item_id is not None:
            for item in items:
                if item.id != preferred_item_id:
                    continue
                if self._slot_matches_item(slot_id=slot_id, item=item):
                    return item
                break
        candidates = [item for item in items if self._slot_matches_item(slot_id=slot_id, item=item)]
        if not candidates:
            return None
        candidates.sort(key=self._sort_item_key)
        return candidates[0]

    def _slot_matches_item(self, *, slot_id: str, item: CharacterSkillItem) -> bool:
        if slot_id == _MAIN_SLOT_ID:
            return item.skill_type == "main"
        return item.skill_type == "auxiliary" and item.auxiliary_slot_id == slot_id

    def _assign_loadout(
        self,
        *,
        loadout: CharacterSkillLoadout,
        main_item: CharacterSkillItem,
        guard_item: CharacterSkillItem,
        movement_item: CharacterSkillItem,
        spirit_item: CharacterSkillItem,
    ) -> bool:
        path = self.require_path(main_item.path_id)
        changed = False
        field_values = {
            "main_skill_id": main_item.id,
            "guard_skill_id": guard_item.id,
            "movement_skill_id": movement_item.id,
            "spirit_skill_id": spirit_item.id,
            "main_axis_id": path.axis_id,
            "main_path_id": path.path_id,
            "behavior_template_id": path.template_id,
            "config_version": self._static_config.skill_generation.config_version,
        }
        for field_name, value in field_values.items():
            if getattr(loadout, field_name) == value:
                continue
            setattr(loadout, field_name, value)
            changed = True
        if not isinstance(loadout.loadout_notes_json, dict):
            loadout.loadout_notes_json = {}
            changed = True
        return changed

    def _synchronize_item_states(
        self,
        *,
        items_by_id: dict[int, CharacterSkillItem],
        loadout: CharacterSkillLoadout,
        occurred_at: datetime,
    ) -> bool:
        equipped_item_ids = {
            skill_item_id
            for skill_item_id in (
                loadout.main_skill_id,
                loadout.guard_skill_id,
                loadout.movement_skill_id,
                loadout.spirit_skill_id,
            )
            if skill_item_id is not None
        }
        changed = False
        for item in items_by_id.values():
            target_state = _ITEM_STATE_EQUIPPED if item.id in equipped_item_ids else _ITEM_STATE_INVENTORY
            if item.item_state != target_state:
                item.item_state = target_state
                if target_state == _ITEM_STATE_EQUIPPED:
                    item.equipped_at = occurred_at
                else:
                    item.unequipped_at = occurred_at
                self._skill_repository.save_skill_item(item)
                changed = True
                continue
            if target_state == _ITEM_STATE_EQUIPPED and item.equipped_at is None:
                item.equipped_at = occurred_at
                self._skill_repository.save_skill_item(item)
                changed = True
        return changed

    def _build_loadout_snapshot(
        self,
        *,
        loadout: CharacterSkillLoadout,
        items_by_id: dict[int, CharacterSkillItem],
    ) -> CharacterSkillLoadoutSnapshot:
        main_item = self._require_loadout_item(items_by_id, loadout.main_skill_id, slot_id=_MAIN_SLOT_ID)
        guard_item = self._require_loadout_item(items_by_id, loadout.guard_skill_id, slot_id="guard")
        movement_item = self._require_loadout_item(items_by_id, loadout.movement_skill_id, slot_id="movement")
        spirit_item = self._require_loadout_item(items_by_id, loadout.spirit_skill_id, slot_id="spirit")
        return CharacterSkillLoadoutSnapshot(
            character_id=loadout.character_id,
            main_axis_id=str(loadout.main_axis_id or main_item.axis_id),
            main_path_id=str(loadout.main_path_id or main_item.path_id),
            behavior_template_id=str(
                loadout.behavior_template_id or self.require_path(main_item.path_id).template_id
            ),
            main_skill=self.build_item_snapshot(item=main_item, loadout=loadout),
            guard_skill=self.build_item_snapshot(item=guard_item, loadout=loadout),
            movement_skill=self.build_item_snapshot(item=movement_item, loadout=loadout),
            spirit_skill=self.build_item_snapshot(item=spirit_item, loadout=loadout),
            config_version=loadout.config_version,
        )

    @staticmethod
    def _require_loadout_item(
        items_by_id: dict[int, CharacterSkillItem],
        skill_item_id: int | None,
        *,
        slot_id: str,
    ) -> CharacterSkillItem:
        if skill_item_id is None:
            raise RuntimeError(f"功法装配缺少槽位：{slot_id}")
        try:
            return items_by_id[skill_item_id]
        except KeyError as exc:
            raise RuntimeError(f"功法装配引用了不存在的实例：{slot_id}:{skill_item_id}") from exc

    def _grant_starter_item(
        self,
        *,
        character_id: int,
        slot_id: str,
        occurred_at: datetime,
    ) -> CharacterSkillItem:
        lineage = self.require_lineage(_STARTER_LINEAGE_BY_SLOT_ID[slot_id])
        rank = self.require_rank("mortal")
        quality = self.require_quality("ordinary")
        total_budget = self._minimum_budget_for(lineage=lineage, rank=rank, quality=quality)
        distribution = self._build_round_robin_distribution(lineage=lineage, total_budget=total_budget)
        resolved_attributes = self._resolve_attributes(distribution)
        resolved_patches = self._resolve_patch_payloads(lineage)
        starter_source_record_id = f"starter:{slot_id}:{occurred_at.isoformat()}"
        starter_item = CharacterSkillItem(
            character_id=character_id,
            lineage_id=lineage.lineage_id,
            path_id=lineage.path_id,
            axis_id=self.require_path(lineage.path_id).axis_id,
            skill_type=lineage.skill_type,
            auxiliary_slot_id=lineage.auxiliary_slot_id,
            skill_name=lineage.name,
            naming_source="lineage_static",
            naming_metadata_json=self._build_fallback_naming_metadata(
                lineage_id=lineage.lineage_id,
                rank_id=rank.rank_id,
                quality_id=quality.quality_id,
                source_type="starter_grant",
                source_record_id=starter_source_record_id,
            ),
            rank_id=rank.rank_id,
            rank_name=rank.name,
            rank_order=rank.order,
            quality_id=quality.quality_id,
            quality_name=quality.name,
            total_budget=total_budget,
            budget_distribution_json=distribution,
            resolved_attributes_json=resolved_attributes,
            resolved_patches_json=resolved_patches,
            source_type="starter_grant",
            source_record_id=starter_source_record_id,
            is_locked=False,
            item_state=_ITEM_STATE_INVENTORY,
            equipped_at=None,
            unequipped_at=None,
        )
        return self._skill_repository.add_skill_item(starter_item)

    def apply_custom_name(
        self,
        *,
        character_id: int,
        skill_item_id: int,
        resolved_name: str,
        naming_source: str = "custom_override",
        naming_metadata: dict[str, str] | None = None,
    ) -> SkillInventoryItemSnapshot:
        """为已有功法实例写入异步或人工名称。"""
        skill_item = self._skill_repository.get_skill_item_by_character_and_id(character_id, skill_item_id)
        if skill_item is None:
            raise LookupError(f"功法实例不存在：{skill_item_id}")
        normalized_name = resolved_name.strip()
        if not normalized_name:
            raise ValueError("功法命名不能为空")
        skill_item.skill_name = normalized_name
        skill_item.naming_source = naming_source.strip() or "custom_override"
        skill_item.naming_metadata_json = dict(naming_metadata or {})
        persisted_item = self._skill_repository.save_skill_item(skill_item)
        loadout = self._skill_repository.get_skill_loadout(character_id)
        return self.build_item_snapshot(item=persisted_item, loadout=loadout)

    @staticmethod
    def _build_fallback_naming_metadata(
        *,
        lineage_id: str,
        rank_id: str,
        quality_id: str,
        source_type: str,
        source_record_id: str | None,
    ) -> dict[str, str]:
        metadata = {
            "lineage_id": lineage_id,
            "rank_id": rank_id,
            "quality_id": quality_id,
            "source_type": source_type,
        }
        if source_record_id is not None:
            metadata["source_record_id"] = source_record_id
        return metadata

    def _minimum_budget_for(
        self,
        *,
        lineage: SkillLineageDefinition,
        rank: SkillRankDefinition,
        quality: SkillQualityDefinition,
    ) -> int:
        if lineage.skill_type == "main":
            return rank.main_budget_min + quality.budget_bonus
        return rank.auxiliary_budget_min + quality.budget_bonus

    def _roll_total_budget(
        self,
        *,
        lineage: SkillLineageDefinition,
        rank: SkillRankDefinition,
        quality: SkillQualityDefinition,
        random_source: random.Random,
    ) -> int:
        if lineage.skill_type == "main":
            base_budget = random_source.randint(rank.main_budget_min, rank.main_budget_max)
        else:
            base_budget = random_source.randint(rank.auxiliary_budget_min, rank.auxiliary_budget_max)
        return base_budget + quality.budget_bonus

    def _build_round_robin_distribution(
        self,
        *,
        lineage: SkillLineageDefinition,
        total_budget: int,
    ) -> dict[str, int]:
        stat_ids = self._require_attribute_pool_stat_ids(lineage.attribute_pool_id)
        distribution = {stat_id: 0 for stat_id in stat_ids}
        if not stat_ids:
            return distribution
        for offset in range(total_budget):
            stat_id = stat_ids[offset % len(stat_ids)]
            distribution[stat_id] += 1
        return {stat_id: value for stat_id, value in distribution.items() if value > 0}

    def _build_random_distribution(
        self,
        *,
        lineage: SkillLineageDefinition,
        total_budget: int,
        random_source: random.Random,
    ) -> dict[str, int]:
        stat_ids = self._require_attribute_pool_stat_ids(lineage.attribute_pool_id)
        distribution = {stat_id: 0 for stat_id in stat_ids}
        if not stat_ids:
            return distribution
        for _ in range(total_budget):
            stat_id = stat_ids[random_source.randrange(len(stat_ids))]
            distribution[stat_id] += 1
        return {stat_id: value for stat_id, value in distribution.items() if value > 0}

    def _require_attribute_pool_stat_ids(self, pool_id: str) -> tuple[str, ...]:
        pool = self._attribute_pool_by_id.get(pool_id)
        if pool is None:
            raise LookupError(f"未配置的功法属性池：{pool_id}")
        return tuple(pool.stat_ids)

    def _resolve_attributes(self, distribution: dict[str, int]) -> dict[str, int]:
        resolved: dict[str, int] = {}
        for stat_id, point_count in distribution.items():
            if point_count <= 0:
                continue
            resolved[stat_id] = point_count * _FLAT_ATTRIBUTE_VALUE_BY_STAT_ID.get(stat_id, 1)
        return resolved

    def _resolve_patch_payloads(self, lineage: SkillLineageDefinition) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        seen_patch_ids: set[str] = set()
        for patch_pool_id in lineage.patch_pool_ids:
            patch_pool = self._patch_pool_by_id.get(patch_pool_id)
            if patch_pool is None:
                continue
            for patch_id in patch_pool.patch_ids:
                if patch_id in seen_patch_ids:
                    continue
                patch_definition = self._patch_by_id.get(patch_id)
                if patch_definition is None:
                    continue
                payloads.append(self._build_patch_payload(patch_definition))
                seen_patch_ids.add(patch_id)
        return payloads

    @staticmethod
    def _build_patch_payload(patch_definition: SkillPatchDefinition) -> dict[str, Any]:
        return {
            "patch_id": patch_definition.patch_id,
            "patch_name": patch_definition.name,
            "patch_kind": patch_definition.patch_kind,
            "target_key": patch_definition.target_key,
            "operation": patch_definition.operation,
            "value": patch_definition.value,
            "summary": patch_definition.summary,
        }

    def _build_template_patch_from_payload(
        self,
        payload: dict[str, Any],
    ) -> AuxiliarySkillParameterPatch | None:
        patch_kind = str(payload.get("patch_kind") or "").strip()
        target_key = str(payload.get("target_key") or "").strip()
        operation = str(payload.get("operation") or "").strip()
        patch_id = str(payload.get("patch_id") or "").strip()
        patch_name = str(payload.get("patch_name") or patch_id).strip() or patch_id
        value = _read_int(payload.get("value"))
        if not patch_id or not target_key:
            return None

        if patch_kind == "template_scalar" and operation == "add_permille":
            selectors = self._resolve_damage_selectors(target_key)
            if not selectors:
                return None
            return AuxiliarySkillParameterPatch(
                patch_id=patch_id,
                patch_name=patch_name,
                numeric_bonuses=tuple(
                    ActionNumericBonusPatch(
                        field=ActionNumericField.DAMAGE_SCALE_PERMILLE,
                        delta=value,
                        selector=selector,
                    )
                    for selector in selectors
                ),
            )

        if patch_kind == "template_threshold" and operation == "raise_permille":
            selector = self._resolve_threshold_selector(target_key)
            if selector is None:
                return None
            return AuxiliarySkillParameterPatch(
                patch_id=patch_id,
                patch_name=patch_name,
                threshold_shifts=(
                    ActionThresholdShiftPatch(
                        field=ActionThresholdField.TARGET_HP_BELOW_PERMILLE,
                        delta=value,
                        selector=selector,
                    ),
                ),
            )

        if patch_kind == "template_trigger_cap" and operation == "add_count":
            selector = self._resolve_trigger_selector(target_key)
            if selector is None:
                return None
            return AuxiliarySkillParameterPatch(
                patch_id=patch_id,
                patch_name=patch_name,
                trigger_cap_adjustments=(
                    ActionTriggerCapAdjustment(
                        delta=value,
                        selector=selector,
                    ),
                ),
            )

        return None

    @staticmethod
    def _resolve_damage_selectors(target_key: str) -> tuple[ActionPatchSelector, ...]:
        if target_key == "burst_damage":
            return (ActionPatchSelector(required_labels=("burst",)),)
        if target_key == "spell_damage":
            return tuple(
                ActionPatchSelector(required_labels=(label,))
                for label in ("spell_focus", "aoe", "control", "debuff")
            )
        return ()

    @staticmethod
    def _resolve_threshold_selector(target_key: str) -> ActionPatchSelector | None:
        if target_key == "execute_threshold":
            return ActionPatchSelector(required_labels=("execute",))
        return None

    @staticmethod
    def _resolve_trigger_selector(target_key: str) -> ActionPatchSelector | None:
        if target_key == "combo_attack":
            return ActionPatchSelector(required_labels=("combo",))
        return None

    @staticmethod
    def _normalize_patch_payloads(item: CharacterSkillItem) -> list[dict[str, Any]]:
        raw_payload = item.resolved_patches_json if isinstance(item.resolved_patches_json, list) else []
        return [dict(entry) for entry in raw_payload if isinstance(entry, dict)]

    @staticmethod
    def _extract_patch_ids(item: CharacterSkillItem) -> tuple[str, ...]:
        patch_ids: list[str] = []
        for payload in SkillRuntimeSupport._normalize_patch_payloads(item):
            patch_id = str(payload.get("patch_id") or "").strip()
            if patch_id:
                patch_ids.append(patch_id)
        return tuple(patch_ids)

    def _sort_item_key(self, item: CharacterSkillItem) -> tuple[int, int, int]:
        quality_order = self._quality_order_by_id.get(item.quality_id, 0)
        return (-item.rank_order, -quality_order, item.id)



def _read_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


__all__ = [
    "CharacterSkillLoadoutSnapshot",
    "ResolvedCharacterSkillState",
    "SkillInventoryItemSnapshot",
    "SkillRuntimeSupport",
]
