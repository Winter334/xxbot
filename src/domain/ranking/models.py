"""评分领域输入与输出模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


@dataclass(frozen=True, slots=True)
class ScoreStatInput:
    """单条可计分属性输入。"""

    stat_id: str
    value: int


@dataclass(frozen=True, slots=True)
class ScoreSpecialEffectInput:
    """评分链路使用的特殊效果输入。"""

    effect_id: str
    effect_type: str
    trigger_event: str
    public_score_key: str | None
    hidden_pvp_score_key: str | None
    payload: dict[str, str | int | bool | None]


@dataclass(frozen=True, slots=True)
class ScoreAffixInput:
    """单条可计分词条输入。"""

    affix_id: str
    tier_id: str
    value: int
    is_pve_specialized: bool
    is_pvp_specialized: bool
    affix_kind: str = "numeric"
    special_effect: ScoreSpecialEffectInput | None = None


@dataclass(frozen=True, slots=True)
class ScoreEquipmentItemInput:
    """单件装备或法宝的评分输入。"""

    item_id: int
    slot_id: str
    equipped_slot_id: str | None
    quality_id: str
    template_id: str
    is_artifact: bool
    enhancement_level: int
    artifact_nurture_level: int
    refinement_level: int
    resonance_name: str | None
    affixes: tuple[ScoreAffixInput, ...]
    resolved_stats: tuple[ScoreStatInput, ...]


@dataclass(frozen=True, slots=True)
class ScoreSkillItemInput:
    """单件功法实例评分输入。"""

    item_id: int
    lineage_id: str
    skill_name: str
    path_id: str
    path_name: str
    axis_id: str
    skill_type: str
    auxiliary_slot_id: str | None
    rank_id: str
    rank_name: str
    rank_order: int
    quality_id: str
    quality_name: str
    total_budget: int
    resolved_patch_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScoreSkillLoadoutInput:
    """功法装配评分输入。"""

    main_axis_id: str | None
    main_path_id: str | None
    main_path_name: str | None
    behavior_template_id: str | None
    main_skill: ScoreSkillItemInput | None
    guard_skill: ScoreSkillItemInput | None
    movement_skill: ScoreSkillItemInput | None
    spirit_skill: ScoreSkillItemInput | None


@dataclass(frozen=True, slots=True)
class ScoreGrowthInput:
    """角色成长评分输入。"""

    realm_id: str
    stage_id: str
    cultivation_value: int
    comprehension_value: int
    realm_total_cultivation: int


@dataclass(frozen=True, slots=True)
class CharacterScoringInput:
    """单角色评分计算输入。"""

    character_id: int
    growth: ScoreGrowthInput
    skill_loadout: ScoreSkillLoadoutInput | None
    equipped_items: tuple[ScoreEquipmentItemInput, ...]


class LeaderboardBoardType(str, Enum):
    """阶段 8 首发榜单类型。"""

    POWER = "power"
    PVP_CHALLENGE = "pvp_challenge"
    ENDLESS_DEPTH = "endless_depth"

    @classmethod
    def launch_board_types(cls) -> tuple["LeaderboardBoardType", ...]:
        """返回阶段 8 首发全部榜单类型。"""
        return (
            cls.POWER,
            cls.PVP_CHALLENGE,
            cls.ENDLESS_DEPTH,
        )


@dataclass(frozen=True, slots=True)
class CalculatedCharacterScore:
    """单角色评分计算结果。"""

    score_version: str
    total_power_score: int
    public_power_score: int
    hidden_pvp_score: int
    growth_score: int
    equipment_score: int
    skill_score: int
    artifact_score: int
    pvp_adjustment_score: int
    main_path_id: str
    main_path_name: str
    preferred_scene: str
    breakdown: dict[str, Any]


__all__ = [
    "CalculatedCharacterScore",
    "CharacterScoringInput",
    "LeaderboardBoardType",
    "ScoreAffixInput",
    "ScoreEquipmentItemInput",
    "ScoreGrowthInput",
    "ScoreSkillItemInput",
    "ScoreSkillLoadoutInput",
    "ScoreSpecialEffectInput",
    "ScoreStatInput",
]
