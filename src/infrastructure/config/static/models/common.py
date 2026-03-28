"""静态配置模型通用基础。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Mapping

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

if TYPE_CHECKING:
    from infrastructure.config.static.models.battle import BattleTemplateConfig
    from infrastructure.config.static.models.breakthrough import BreakthroughTrialConfig
    from infrastructure.config.static.models.cultivation import (
        BaseCoefficientConfig,
        CultivationSourceConfig,
        DailyCultivationConfig,
    )
    from infrastructure.config.static.models.endless_dungeon import EndlessDungeonConfig
    from infrastructure.config.static.models.enemy import EnemyConfig
    from infrastructure.config.static.models.equipment import EquipmentConfig
    from infrastructure.config.static.models.progression import RealmProgressionConfig
    from infrastructure.config.static.models.pvp import PvpConfig
    from infrastructure.config.static.models.skill import (
        SkillDropConfig,
        SkillGenerationConfig,
        SkillLineageConfig,
        SkillPathConfig,
    )

LAUNCH_REALM_IDS: tuple[str, ...] = (
    "mortal",
    "qi_refining",
    "foundation",
    "core",
    "nascent_soul",
    "deity_transformation",
    "void_refinement",
    "body_integration",
    "great_vehicle",
    "tribulation",
)
LAUNCH_STAGE_IDS: tuple[str, ...] = ("early", "middle", "late", "perfect")
LAUNCH_REALM_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("mortal", "qi_refining"),
    ("qi_refining", "foundation"),
    ("foundation", "core"),
    ("core", "nascent_soul"),
    ("nascent_soul", "deity_transformation"),
    ("deity_transformation", "void_refinement"),
    ("void_refinement", "body_integration"),
    ("body_integration", "great_vehicle"),
    ("great_vehicle", "tribulation"),
)

ConfigVersion = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=5,
        max_length=32,
        pattern=r"^\d+\.\d+\.\d+$",
    ),
]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9_]+$",
    ),
]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
]
PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
PercentageDecimal = Annotated[Decimal, Field(ge=0, le=1)]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
SortOrder = Annotated[int, Field(ge=1)]


class StaticConfigModel(BaseModel):
    """所有静态配置模型的共同基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionedSectionConfig(StaticConfigModel):
    """所有静态配置节的共同版本字段。"""

    config_version: ConfigVersion


class OrderedConfigItem(StaticConfigModel):
    """带顺序与展示名的基础条目。"""

    name: DisplayName
    order: SortOrder


class RealmScopedConfigItem(OrderedConfigItem):
    """带境界标识的基础条目。"""

    realm_id: StableId


@dataclass(frozen=True, slots=True)
class StaticGameConfig:
    """静态配置聚合根，提供按 section 的只读访问。"""

    realm_progression: "RealmProgressionConfig"
    daily_cultivation: "DailyCultivationConfig"
    base_coefficients: "BaseCoefficientConfig"
    cultivation_sources: "CultivationSourceConfig"
    skill_paths: "SkillPathConfig"
    skill_lineages: "SkillLineageConfig"
    skill_generation: "SkillGenerationConfig"
    skill_drops: "SkillDropConfig"
    battle_templates: "BattleTemplateConfig"
    equipment: "EquipmentConfig"
    enemies: "EnemyConfig"
    breakthrough_trials: "BreakthroughTrialConfig"
    endless_dungeon: "EndlessDungeonConfig"
    pvp: "PvpConfig"
    _sections: Mapping[str, object] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        sections = MappingProxyType(
            {
                "realm_progression": self.realm_progression,
                "daily_cultivation": self.daily_cultivation,
                "base_coefficients": self.base_coefficients,
                "cultivation_sources": self.cultivation_sources,
                "skill_paths": self.skill_paths,
                "skill_lineages": self.skill_lineages,
                "skill_generation": self.skill_generation,
                "skill_drops": self.skill_drops,
                "battle_templates": self.battle_templates,
                "equipment": self.equipment,
                "enemies": self.enemies,
                "breakthrough_trials": self.breakthrough_trials,
                "endless_dungeon": self.endless_dungeon,
                "pvp": self.pvp,
            }
        )
        object.__setattr__(self, "_sections", sections)

    @property
    def sections(self) -> Mapping[str, object]:
        """返回按 section 名称索引的只读视图。"""
        return self._sections

    def get_section(self, section_name: str) -> object:
        """按 section 名称读取配置切片。"""
        try:
            return self._sections[section_name]
        except KeyError as exc:
            raise KeyError(f"未知静态配置 section: {section_name}") from exc
