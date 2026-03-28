"""无尽副本敌人遭遇生成规则。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from domain.dungeon.models import EndlessEnemyEncounter, EndlessNodeType
from domain.dungeon.progression import EndlessDungeonProgression
from infrastructure.config.static.models.common import StaticGameConfig
from infrastructure.config.static.models.enemy import EnemyRaceDefinition, RegionBiasDefinition

_DECIMAL_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class _WeightedEntry:
    """带权候选项。"""

    key: str
    weight: Decimal


class EndlessEncounterGenerator:
    """按区域偏置与楼层规则生成确定性敌人遭遇。"""

    def __init__(self, static_config: StaticGameConfig) -> None:
        self._static_config = static_config
        self._progression = EndlessDungeonProgression(static_config)
        self._enemy_config = static_config.enemies
        self._encounter_config = static_config.endless_dungeon.encounter
        self._regions_by_bias = {
            region.region_bias_id: region
            for region in self._enemy_config.region_biases
        }
        self._races_by_id = {
            race.race_id: race
            for race in self._enemy_config.races
        }

    def generate(self, *, floor: int, seed: int) -> EndlessEnemyEncounter:
        """按楼层与随机种子生成确定性遭遇。"""
        floor_snapshot = self._progression.resolve_floor(floor)
        region_bias = self._require_region_bias(floor_snapshot.region.region_bias_id)
        rng = random.Random(self._compose_seed(seed=seed, floor=floor_snapshot.floor))
        race_id = self._select_race_id(region_bias=region_bias, rng=rng)
        race = self._require_race(race_id)
        template_id = self._select_template_id(region_bias=region_bias, race=race, rng=rng)
        return EndlessEnemyEncounter(
            floor=floor_snapshot.floor,
            region_id=floor_snapshot.region.region_id,
            region_bias_id=region_bias.region_bias_id,
            node_type=floor_snapshot.node_type,
            race_id=race_id,
            template_id=template_id,
            enemy_count=self._resolve_enemy_count(floor_snapshot.node_type),
            seed=seed,
        )

    @staticmethod
    def _compose_seed(*, seed: int, floor: int) -> int:
        """组合输入种子与楼层，保证相同输入得到相同结果。"""
        return seed * 100003 + floor * 97

    def _select_race_id(self, *, region_bias: RegionBiasDefinition, rng: random.Random) -> str:
        weighted_entries: list[_WeightedEntry] = []
        favored_races = set(region_bias.favored_race_ids)
        favored_bonus = self._encounter_config.favored_race_bonus
        for race in sorted(self._enemy_config.races, key=lambda item: item.order):
            weight = Decimal("1")
            if race.race_id in favored_races:
                weight += favored_bonus
            weighted_entries.append(_WeightedEntry(key=race.race_id, weight=weight))
        return self._pick_weighted_key(weighted_entries=tuple(weighted_entries), rng=rng)

    def _select_template_id(
        self,
        *,
        region_bias: RegionBiasDefinition,
        race: EnemyRaceDefinition,
        rng: random.Random,
    ) -> str:
        favored_templates = set(race.favored_template_ids)
        favored_bonus = self._encounter_config.favored_template_bonus
        weighted_entries: list[_WeightedEntry] = []
        for template_weight in sorted(region_bias.template_weights, key=lambda item: item.order):
            weight = template_weight.weight
            if template_weight.template_id in favored_templates:
                weight += favored_bonus
            weighted_entries.append(_WeightedEntry(key=template_weight.template_id, weight=weight))
        return self._pick_weighted_key(weighted_entries=tuple(weighted_entries), rng=rng)

    @staticmethod
    def _pick_weighted_key(*, weighted_entries: tuple[_WeightedEntry, ...], rng: random.Random) -> str:
        total_weight = sum((entry.weight for entry in weighted_entries), start=_DECIMAL_ZERO)
        if total_weight <= _DECIMAL_ZERO:
            raise ValueError("候选权重总和必须大于 0")
        threshold = Decimal(str(rng.random())) * total_weight
        accumulated = _DECIMAL_ZERO
        for entry in weighted_entries:
            accumulated += entry.weight
            if threshold < accumulated:
                return entry.key
        return weighted_entries[-1].key

    def _resolve_enemy_count(self, node_type: EndlessNodeType) -> int:
        if node_type is EndlessNodeType.NORMAL:
            return self._encounter_config.normal_enemy_count
        if node_type is EndlessNodeType.ELITE:
            return self._encounter_config.elite_enemy_count
        return self._encounter_config.boss_enemy_count

    def _require_region_bias(self, region_bias_id: str) -> RegionBiasDefinition:
        try:
            return self._regions_by_bias[region_bias_id]
        except KeyError as exc:
            raise ValueError(f"未找到敌人区域偏置：{region_bias_id}") from exc

    def _require_race(self, race_id: str) -> EnemyRaceDefinition:
        try:
            return self._races_by_id[race_id]
        except KeyError as exc:
            raise ValueError(f"未找到敌人族群：{race_id}") from exc


__all__ = ["EndlessEncounterGenerator"]
