"""无尽副本统一掉落编排。"""

from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Any

from application.character.skill_drop_service import SkillDropGenerationResult, SkillDropService
from application.equipment.equipment_service import EquipmentGenerationApplicationResult, EquipmentItemSnapshot, EquipmentService
from infrastructure.config.static import StaticGameConfig, get_static_config

_DROP_PROGRESS_PER_REWARD = 10
_DROP_KIND_WEIGHT_PAIRS: tuple[tuple[str, int], ...] = (
    ("equipment", 60),
    ("artifact", 25),
    ("skill", 15),
)
_DROP_RANK_OFFSET_WEIGHT_PAIRS: tuple[tuple[int, int], ...] = (
    (0, 70),
    (-1, 20),
    (1, 10),
)
_DROP_QUALITY_ORDER_WEIGHT_PAIRS: tuple[tuple[int, int], ...] = (
    (1, 55),
    (2, 25),
    (3, 12),
    (4, 6),
    (5, 2),
)
_ENTRY_TYPE_EQUIPMENT = "equipment_drop"
_ENTRY_TYPE_ARTIFACT = "artifact_drop"


@dataclass(frozen=True, slots=True)
class EndlessResolvedDropSpec:
    """单次统一掉落决策结果。"""

    drop_kind: str
    rank_order: int
    quality_order: int
    rank_id: str
    quality_id: str


class EndlessSettlementDropOrchestratorError(RuntimeError):
    """无尽副本统一掉落编排异常。"""


class EndlessSettlementDropOrchestrator:
    """把统一掉落进度转为装备、法宝、功法实例。"""

    def __init__(
        self,
        *,
        equipment_service: EquipmentService,
        skill_drop_service: SkillDropService,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._equipment_service = equipment_service
        self._skill_drop_service = skill_drop_service
        self._static_config = static_config or get_static_config()
        ordered_realms = tuple(sorted(self._static_config.realm_progression.realms, key=lambda item: item.order))
        ordered_equipment_ranks = tuple(self._static_config.equipment.ordered_equipment_ranks)
        ordered_skill_ranks = tuple(self._static_config.skill_generation.ordered_ranks)
        ordered_equipment_qualities = tuple(self._static_config.equipment.ordered_qualities)
        ordered_skill_qualities = tuple(self._static_config.skill_generation.ordered_qualities)
        if not ordered_realms:
            raise EndlessSettlementDropOrchestratorError("境界配置为空，无法解析无尽副本掉落阶数")
        if not ordered_equipment_ranks or not ordered_skill_ranks:
            raise EndlessSettlementDropOrchestratorError("阶数配置为空，无法解析无尽副本掉落阶数")
        if not ordered_equipment_qualities or not ordered_skill_qualities:
            raise EndlessSettlementDropOrchestratorError("品质配置为空，无法解析无尽副本掉落品质")
        self._realm_order_by_id = {realm.realm_id: realm.order for realm in ordered_realms}
        self._equipment_rank_id_by_order = {rank.order: rank.rank_id for rank in ordered_equipment_ranks}
        self._skill_rank_id_by_order = {rank.order: rank.rank_id for rank in ordered_skill_ranks}
        self._equipment_quality_id_by_order = {quality.order: quality.quality_id for quality in ordered_equipment_qualities}
        self._skill_quality_id_by_order = {quality.order: quality.quality_id for quality in ordered_skill_qualities}
        self._max_rank_order = min(max(self._equipment_rank_id_by_order), max(self._skill_rank_id_by_order))
        self._max_equipment_quality_order = max(self._equipment_quality_id_by_order)
        self._max_skill_quality_order = max(self._skill_quality_id_by_order)

    def generate_settlement_drops(
        self,
        *,
        character_id: int,
        realm_id: str,
        pending_drop_progress: int,
        run_seed: int,
        terminated_floor: int,
        source_ref: str,
    ) -> tuple[dict[str, Any], ...]:
        """根据统一掉落进度生成全部终结掉落。"""
        drop_count = max(0, pending_drop_progress) // _DROP_PROGRESS_PER_REWARD
        if drop_count <= 0:
            return ()
        drop_entries: list[dict[str, Any]] = []
        for drop_index in range(1, drop_count + 1):
            decision_seed = self._compose_drop_seed(
                run_seed=run_seed,
                terminated_floor=terminated_floor,
                drop_index=drop_index,
                salt=17,
            )
            random_source = random.Random(decision_seed)
            spec = self._resolve_drop_spec(realm_id=realm_id, random_source=random_source)
            instance_seed = self._compose_drop_seed(
                run_seed=run_seed,
                terminated_floor=terminated_floor,
                drop_index=drop_index,
                salt=97,
            )
            if spec.drop_kind == "skill":
                generated_skill = self._skill_drop_service.generate_endless_settlement_drop(
                    character_id=character_id,
                    rank_id=spec.rank_id,
                    quality_id=spec.quality_id,
                    seed=instance_seed,
                    source_ref=source_ref,
                )
                drop_entries.append(
                    self._build_skill_drop_entry(
                        generated_skill=generated_skill,
                        source_floor=terminated_floor,
                        source_progress=pending_drop_progress,
                        drop_index=drop_index,
                    )
                )
                continue
            generated_item = self._equipment_service.generate_endless_settlement_item(
                character_id=character_id,
                rank_id=spec.rank_id,
                quality_id=spec.quality_id,
                is_artifact=spec.drop_kind == "artifact",
                seed=instance_seed,
            )
            drop_entries.append(
                self._build_equipment_drop_entry(
                    item=generated_item.item,
                    source_floor=terminated_floor,
                    source_progress=pending_drop_progress,
                    drop_index=drop_index,
                )
            )
        return tuple(drop_entries)

    def _resolve_drop_spec(self, *, realm_id: str, random_source: random.Random) -> EndlessResolvedDropSpec:
        drop_kind = self._weighted_pick(choices=_DROP_KIND_WEIGHT_PAIRS, random_source=random_source)
        max_rank_order = self._resolve_max_rank_order(realm_id=realm_id)
        current_rank_order = self._require_realm_order(realm_id=realm_id)
        offset = self._weighted_pick(choices=_DROP_RANK_OFFSET_WEIGHT_PAIRS, random_source=random_source)
        rank_order = max(1, min(max_rank_order, current_rank_order + int(offset)))
        quality_order_roll = self._weighted_pick(choices=_DROP_QUALITY_ORDER_WEIGHT_PAIRS, random_source=random_source)
        if drop_kind == "skill":
            quality_order = max(1, min(self._max_skill_quality_order, int(quality_order_roll)))
            return EndlessResolvedDropSpec(
                drop_kind=drop_kind,
                rank_order=rank_order,
                quality_order=quality_order,
                rank_id=self._require_skill_rank_id(rank_order),
                quality_id=self._require_skill_quality_id(quality_order),
            )
        quality_order = max(1, min(self._max_equipment_quality_order, int(quality_order_roll)))
        return EndlessResolvedDropSpec(
            drop_kind=drop_kind,
            rank_order=rank_order,
            quality_order=quality_order,
            rank_id=self._require_equipment_rank_id(rank_order),
            quality_id=self._require_equipment_quality_id(quality_order),
        )

    def _resolve_max_rank_order(self, *, realm_id: str) -> int:
        current_realm_order = self._require_realm_order(realm_id=realm_id)
        return min(self._max_rank_order, current_realm_order + 1)

    def _require_realm_order(self, *, realm_id: str) -> int:
        try:
            return self._realm_order_by_id[realm_id]
        except KeyError as exc:
            raise EndlessSettlementDropOrchestratorError(f"未配置的角色境界：{realm_id}") from exc

    def _require_equipment_rank_id(self, rank_order: int) -> str:
        try:
            return self._equipment_rank_id_by_order[rank_order]
        except KeyError as exc:
            raise EndlessSettlementDropOrchestratorError(f"装备阶数顺序未配置：{rank_order}") from exc

    def _require_skill_rank_id(self, rank_order: int) -> str:
        try:
            return self._skill_rank_id_by_order[rank_order]
        except KeyError as exc:
            raise EndlessSettlementDropOrchestratorError(f"功法阶数顺序未配置：{rank_order}") from exc

    def _require_equipment_quality_id(self, quality_order: int) -> str:
        try:
            return self._equipment_quality_id_by_order[quality_order]
        except KeyError as exc:
            raise EndlessSettlementDropOrchestratorError(f"装备品质顺序未配置：{quality_order}") from exc

    def _require_skill_quality_id(self, quality_order: int) -> str:
        try:
            return self._skill_quality_id_by_order[quality_order]
        except KeyError as exc:
            raise EndlessSettlementDropOrchestratorError(f"功法品质顺序未配置：{quality_order}") from exc

    @staticmethod
    def _compose_drop_seed(*, run_seed: int, terminated_floor: int, drop_index: int, salt: int) -> int:
        return run_seed * 4099 + terminated_floor * 131 + drop_index * 17 + salt

    @staticmethod
    def _weighted_pick(*, choices: tuple[tuple[Any, int], ...], random_source: random.Random) -> Any:
        total_weight = sum(max(0, weight) for _, weight in choices)
        if total_weight <= 0:
            raise EndlessSettlementDropOrchestratorError("权重总和必须大于 0")
        cursor = random_source.randrange(total_weight)
        running = 0
        for value, weight in choices:
            running += max(0, weight)
            if cursor < running:
                return value
        return choices[-1][0]

    @staticmethod
    def _build_equipment_drop_entry(
        *,
        item: EquipmentItemSnapshot,
        source_floor: int,
        source_progress: int,
        drop_index: int,
    ) -> dict[str, Any]:
        return {
            "entry_type": _ENTRY_TYPE_ARTIFACT if item.is_artifact else _ENTRY_TYPE_EQUIPMENT,
            "item_id": item.item_id,
            "quantity": 1,
            "display_name": item.display_name,
            "slot_id": item.slot_id,
            "slot_name": item.slot_name,
            "quality_id": item.quality_id,
            "quality_name": item.quality_name,
            "rank_id": item.rank_id,
            "rank_name": item.rank_name,
            "mapped_realm_id": item.mapped_realm_id,
            "template_id": item.template_id,
            "template_name": item.template_name,
            "is_artifact": item.is_artifact,
            "resonance_name": item.resonance_name,
            "enhancement_level": item.enhancement_level,
            "artifact_nurture_level": item.artifact_nurture_level,
            "source_floor": max(1, source_floor),
            "source_progress": max(0, source_progress),
            "drop_index": max(1, drop_index),
        }

    @staticmethod
    def _build_skill_drop_entry(
        *,
        generated_skill: SkillDropGenerationResult,
        source_floor: int,
        source_progress: int,
        drop_index: int,
    ) -> dict[str, Any]:
        payload = generated_skill.to_drop_summary()
        payload["source_floor"] = max(1, source_floor)
        payload["source_progress"] = max(0, source_progress)
        payload["drop_index"] = max(1, drop_index)
        return payload


__all__ = [
    "EndlessResolvedDropSpec",
    "EndlessSettlementDropOrchestrator",
    "EndlessSettlementDropOrchestratorError",
]
