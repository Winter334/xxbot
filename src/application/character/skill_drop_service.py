"""功法掉落生成服务。"""

from __future__ import annotations

from dataclasses import dataclass
import random

from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.config.static.models.skill import SkillDropConfig, SkillDropPoolDefinition
from infrastructure.db.repositories import CharacterRepository, SkillRepository, SqlAlchemySkillRepository

from application.character.skill_runtime_support import SkillRuntimeSupport

_RANK_OFFSET_WEIGHT_PAIRS: tuple[tuple[int, int], ...] = (
    (0, 70),
    (-1, 20),
    (1, 10),
)
_QUALITY_WEIGHT_BY_ID: tuple[tuple[str, int], ...] = (
    ("ordinary", 55),
    ("good", 25),
    ("superior", 12),
    ("rare", 6),
    ("perfect", 2),
)
@dataclass(frozen=True, slots=True)
class SkillDropGenerationResult:
    """单次功法掉落生成结果。"""

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
    source_type: str
    source_record_id: str | None

    def to_drop_summary(self) -> dict[str, object]:
        """转换为结算层可直接展示的摘要。"""
        return {
            "entry_type": "skill_drop",
            "item_id": self.item_id,
            "character_id": self.character_id,
            "lineage_id": self.lineage_id,
            "skill_name": self.skill_name,
            "path_id": self.path_id,
            "axis_id": self.axis_id,
            "skill_type": self.skill_type,
            "auxiliary_slot_id": self.auxiliary_slot_id,
            "rank_id": self.rank_id,
            "rank_name": self.rank_name,
            "rank_order": self.rank_order,
            "quality_id": self.quality_id,
            "quality_name": self.quality_name,
            "source_type": self.source_type,
            "source_record_id": self.source_record_id,
        }


class SkillDropService:
    """按首发默认规则生成功法掉落并落库。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        skill_repository: SkillRepository | None = None,
        static_config: StaticGameConfig | None = None,
        skill_runtime_support: SkillRuntimeSupport | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._skill_repository = skill_repository or self._build_fallback_skill_repository(character_repository)
        self._static_config = static_config or get_static_config()
        self._skill_runtime_support = skill_runtime_support or SkillRuntimeSupport(
            character_repository=character_repository,
            skill_repository=self._skill_repository,
            static_config=self._static_config,
        )
        self._ordered_ranks = tuple(self._static_config.skill_generation.ordered_ranks)
        self._drop_config = self._static_config.skill_drops
        self._drop_pool_by_id = {
            pool.pool_id: pool
            for pool in self._drop_config.pools
        }
        self._pool_id_by_skill_type = self._build_pool_id_by_skill_type(self._drop_config)
        self._slot_rate_by_slot_id = self._build_slot_rate_by_slot_id(self._drop_config)
        self._main_drop_weight = self._normalize_probability_weight(
            self._drop_config.default_probabilities.main_lineage_drop_rate,
        )
        self._auxiliary_drop_weight = self._normalize_probability_weight(
            self._drop_config.default_probabilities.auxiliary_lineage_drop_rate,
        )
        self._duplicate_drop_allowed = self._drop_config.default_probabilities.duplicate_drop_allowed

    def generate_endless_settlement_drop(
        self,
        *,
        character_id: int,
        floor: int,
        seed: int | None = None,
        source_ref: str,
    ) -> SkillDropGenerationResult:
        """为无尽副本最终结算生成一件功法掉落。"""
        self._require_character(character_id)
        random_source = random.Random(seed)
        skill_type = self._weighted_pick(
            choices=(("main", self._main_drop_weight), ("auxiliary", self._auxiliary_drop_weight)),
            random_source=random_source,
        )
        auxiliary_slot_id = None
        if skill_type == "auxiliary":
            auxiliary_slot_id = self._weighted_pick(
                choices=tuple(self._slot_rate_by_slot_id.items()),
                random_source=random_source,
            )
        pool_id = self._resolve_pool_id(skill_type=skill_type, auxiliary_slot_id=auxiliary_slot_id)
        lineage_id = self._pick_lineage_id(
            character_id=character_id,
            pool_id=pool_id,
            random_source=random_source,
        )
        rank_id = self._resolve_rank_id(floor=floor, random_source=random_source)
        quality_id = self._weighted_pick(
            choices=_QUALITY_WEIGHT_BY_ID,
            random_source=random_source,
        )
        generated_item = self._skill_runtime_support.generate_skill_item(
            character_id=character_id,
            lineage_id=lineage_id,
            rank_id=rank_id,
            quality_id=quality_id,
            source_type="endless_skill_drop",
            source_record_id=source_ref,
            seed=random_source.randint(1, 1_000_000_000),
        )
        return SkillDropGenerationResult(
            item_id=generated_item.id,
            character_id=generated_item.character_id,
            lineage_id=generated_item.lineage_id,
            skill_name=generated_item.skill_name,
            path_id=generated_item.path_id,
            axis_id=generated_item.axis_id,
            skill_type=generated_item.skill_type,
            auxiliary_slot_id=generated_item.auxiliary_slot_id,
            rank_id=generated_item.rank_id,
            rank_name=generated_item.rank_name,
            rank_order=generated_item.rank_order,
            quality_id=generated_item.quality_id,
            quality_name=generated_item.quality_name,
            source_type=generated_item.source_type,
            source_record_id=generated_item.source_record_id,
        )

    def _pick_lineage_id(self, *, character_id: int, pool_id: str, random_source: random.Random) -> str:
        pool = self._require_pool(pool_id=pool_id)
        candidate_entries = list(pool.entries)
        if not self._duplicate_drop_allowed:
            owned_lineage_ids = {
                item.lineage_id
                for item in self._skill_repository.list_skill_items_by_character_id(character_id)
                if item.item_state != "dismantled"
            }
            filtered_entries = [entry for entry in candidate_entries if entry.lineage_id not in owned_lineage_ids]
            if filtered_entries:
                candidate_entries = filtered_entries
        return self._weighted_pick(
            choices=tuple((entry.lineage_id, entry.weight) for entry in candidate_entries),
            random_source=random_source,
        )

    def _resolve_rank_id(self, *, floor: int, random_source: random.Random) -> str:
        base_order = min(len(self._ordered_ranks), max(1, ((max(1, floor) - 1) // 10) + 1))
        offset = self._weighted_pick(choices=_RANK_OFFSET_WEIGHT_PAIRS, random_source=random_source)
        resolved_order = max(1, min(len(self._ordered_ranks), base_order + int(offset)))
        return self._ordered_ranks[resolved_order - 1].rank_id

    @staticmethod
    def _weighted_pick(*, choices, random_source: random.Random):
        total_weight = sum(max(0, weight) for _, weight in choices)
        if total_weight <= 0:
            raise RuntimeError("权重总和必须大于 0")
        cursor = random_source.randrange(total_weight)
        running = 0
        for value, weight in choices:
            running += max(0, weight)
            if cursor < running:
                return value
        return choices[-1][0]

    def _require_character(self, character_id: int) -> None:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise RuntimeError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise RuntimeError(f"角色缺少成长状态：{character_id}")

    def _resolve_pool_id(self, *, skill_type: str, auxiliary_slot_id: str | None) -> str:
        if skill_type == "main":
            pool_id = self._pool_id_by_skill_type.get("main")
            if pool_id is None:
                raise RuntimeError("未配置主修功法掉落池")
            return pool_id
        if auxiliary_slot_id is None:
            raise RuntimeError("辅助功法掉落缺少槽位类型")
        pool_id = self._pool_id_by_skill_type.get(("auxiliary", auxiliary_slot_id))
        if pool_id is None:
            raise RuntimeError(f"未配置辅助功法掉落池：{auxiliary_slot_id}")
        return pool_id

    def _require_pool(self, *, pool_id: str) -> SkillDropPoolDefinition:
        pool = self._drop_pool_by_id.get(pool_id)
        if pool is None or not pool.entries:
            raise RuntimeError(f"未配置可用功法掉落池：{pool_id}")
        return pool

    @staticmethod
    def _build_pool_id_by_skill_type(drop_config: SkillDropConfig) -> dict[object, str]:
        mapping: dict[object, str] = {}
        for pool in drop_config.pools:
            if pool.skill_type == "main":
                mapping["main"] = pool.pool_id
                continue
            mapping[(pool.skill_type, pool.auxiliary_slot_id)] = pool.pool_id
        return mapping

    @staticmethod
    def _build_slot_rate_by_slot_id(drop_config: SkillDropConfig) -> dict[str, int]:
        probability = drop_config.default_probabilities
        return {
            "guard": SkillDropService._normalize_probability_weight(probability.guard_slot_rate),
            "movement": SkillDropService._normalize_probability_weight(probability.movement_slot_rate),
            "spirit": SkillDropService._normalize_probability_weight(probability.spirit_slot_rate),
        }

    @staticmethod
    def _normalize_probability_weight(value) -> int:
        return int(value * 10_000)

    @staticmethod
    def _build_fallback_skill_repository(character_repository: CharacterRepository) -> SkillRepository:
        session = getattr(character_repository, "_session", None)
        if session is None:
            raise ValueError("SkillDropService 缺少 skill_repository，且无法从 character_repository 推导会话")
        return SqlAlchemySkillRepository(session)


__all__ = [
    "SkillDropGenerationResult",
    "SkillDropService",
]
