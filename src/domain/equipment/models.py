"""装备领域模型。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from types import MappingProxyType
from typing import Mapping, Protocol

_DECIMAL_ZERO = Decimal("0")
_DECIMAL_ONE = Decimal("1")
_QUALITY_SPECIAL_EFFECT_STRENGTH_MULTIPLIER_BY_ID = MappingProxyType(
    {
        "common": Decimal("1.00"),
        "rare": Decimal("1.03"),
        "epic": Decimal("1.06"),
        "earthly": Decimal("1.10"),
        "legendary": Decimal("1.15"),
        "immortal": Decimal("1.20"),
    }
)


def _scale_value(value: int, bonus_ratio: Decimal) -> int:
    """按当前增幅比例计算最终整数值。"""
    return int((Decimal(value) * (_DECIMAL_ONE + bonus_ratio)).to_integral_value(rounding=ROUND_HALF_UP))


def _scale_integer_value(value: int, multiplier: Decimal) -> int:
    """按倍率缩放特殊词条中的数值字段。"""
    return int((Decimal(value) * multiplier).to_integral_value(rounding=ROUND_HALF_UP))


def special_effect_strength_multiplier_for_quality(*, quality_id: str) -> Decimal:
    """读取装备品质对应的特殊词条强度倍率。"""
    normalized_quality_id = quality_id.strip()
    return _QUALITY_SPECIAL_EFFECT_STRENGTH_MULTIPLIER_BY_ID.get(normalized_quality_id, _DECIMAL_ONE)


def scale_special_effect_payload(
    *,
    quality_id: str,
    payload: Mapping[str, str | int | bool | None],
) -> Mapping[str, str | int | bool | None]:
    """按装备品质放大特殊词条的强度字段。"""
    multiplier = special_effect_strength_multiplier_for_quality(quality_id=quality_id)
    normalized_payload = dict(payload)
    if multiplier == _DECIMAL_ONE:
        return MappingProxyType(normalized_payload)
    scaled_payload = {
        key: _scale_special_effect_payload_value(key=key, value=value, multiplier=multiplier)
        for key, value in normalized_payload.items()
    }
    return MappingProxyType(scaled_payload)


def _scale_special_effect_payload_value(
    *,
    key: str,
    value: str | int | bool | None,
    multiplier: Decimal,
) -> str | int | bool | None:
    """仅放大明确属于强度的数值键。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    normalized_key = key.strip()
    if normalized_key == "suppression_permille":
        return _scale_integer_value(value, multiplier)
    if normalized_key == "trigger_rate_permille" or normalized_key.endswith("_threshold_permille"):
        return value
    if normalized_key.endswith("_ratio_permille"):
        return _scale_integer_value(value, multiplier)
    return value


class EquipmentRandomSource(Protocol):
    """装备领域使用的随机源抽象。"""

    def randrange(self, stop: int) -> int:
        """返回小于给定上界的随机整数。"""

    def random(self) -> float:
        """返回 [0, 1) 区间的随机浮点值。"""


@dataclass(frozen=True, slots=True)
class EquipmentResourceCost:
    """一次装备行为涉及的资源数量。"""

    resource_id: str
    quantity: int


@dataclass(frozen=True, slots=True)
class EquipmentAttributeValue:
    """装备基础属性条目。"""

    stat_id: str
    value: int

    def resolved_value(self, bonus_ratio: Decimal) -> int:
        """读取应用成长倍率后的最终值。"""
        return _scale_value(self.value, bonus_ratio)


@dataclass(frozen=True, slots=True)
class EquipmentSpecialEffectValue:
    """装备特殊效果词条的通用描述。"""

    effect_id: str
    effect_name: str
    effect_type: str
    trigger_event: str
    payload: Mapping[str, str | int | bool | None]
    public_score_key: str | None = None
    hidden_pvp_score_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class EquipmentAffixValue:
    """装备词条结果。"""

    affix_id: str
    affix_name: str
    stat_id: str
    category: str
    tier_id: str
    tier_name: str
    rolled_multiplier: Decimal
    value: int
    is_pve_specialized: bool
    is_pvp_specialized: bool
    affix_kind: str = "numeric"
    special_effect: EquipmentSpecialEffectValue | None = None

    def __post_init__(self) -> None:
        if self.affix_kind == "special_effect" and self.special_effect is None:
            raise ValueError("特殊词条必须声明特殊效果定义")
        if self.affix_kind != "special_effect" and self.special_effect is not None:
            raise ValueError("数值词条不能额外挂载特殊效果定义")

    @property
    def is_special(self) -> bool:
        """返回当前词条是否为特殊效果词条。"""
        return self.affix_kind == "special_effect"

    @property
    def has_numeric_payload(self) -> bool:
        """返回当前词条是否携带可聚合的数值部分。"""
        return bool(self.stat_id.strip()) and self.value != 0

    @property
    def special_effect_id(self) -> str | None:
        """返回特殊效果标识。"""
        if self.special_effect is None:
            return None
        return self.special_effect.effect_id

    def resolved_value(self, bonus_ratio: Decimal) -> int:
        """读取应用成长倍率后的最终值。"""
        if not self.has_numeric_payload:
            return 0
        return _scale_value(self.value, bonus_ratio)


@dataclass(frozen=True, slots=True)
class EquipmentResolvedStat:
    """聚合后的装备最终属性。"""

    stat_id: str
    value: int


@dataclass(frozen=True, slots=True)
class EquipmentNamingRecord:
    """装备命名结果与来源元数据。"""

    resolved_name: str
    naming_template_id: str
    naming_source: str
    naming_metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "naming_metadata", MappingProxyType(dict(self.naming_metadata)))


@dataclass(frozen=True, slots=True)
class EquipmentRank:
    """装备与法宝统一阶数信息。"""

    rank_id: str
    rank_name: str
    rank_order: int
    mapped_realm_id: str
    base_attribute_multiplier: Decimal
    affix_base_value_multiplier: Decimal
    dismantle_reward_multiplier: Decimal


@dataclass(frozen=True, slots=True)
class EquipmentItem:
    """单件装备或法宝的纯领域表达。"""

    slot_id: str
    slot_name: str
    quality_id: str
    quality_name: str
    template_id: str
    template_name: str
    rank_id: str
    rank_name: str
    rank_order: int
    mapped_realm_id: str
    is_artifact: bool
    resonance_name: str | None
    enhancement_level: int
    artifact_nurture_level: int
    base_attributes: tuple[EquipmentAttributeValue, ...]
    affixes: tuple[EquipmentAffixValue, ...]
    base_attribute_multiplier: Decimal = _DECIMAL_ONE
    affix_base_value_multiplier: Decimal = _DECIMAL_ONE
    dismantle_reward_multiplier: Decimal = _DECIMAL_ONE
    enhancement_base_stat_bonus_ratio: Decimal = _DECIMAL_ZERO
    enhancement_affix_bonus_ratio: Decimal = _DECIMAL_ZERO
    nurture_base_stat_bonus_ratio: Decimal = _DECIMAL_ZERO
    nurture_affix_bonus_ratio: Decimal = _DECIMAL_ZERO
    naming: EquipmentNamingRecord | None = None

    def __post_init__(self) -> None:
        if not self.is_artifact:
            if self.resonance_name is not None:
                raise ValueError("普通装备不能声明法宝共鸣名")
            if self.artifact_nurture_level != 0:
                raise ValueError("普通装备不能声明法宝培养等级")
            if self.nurture_base_stat_bonus_ratio != _DECIMAL_ZERO:
                raise ValueError("普通装备不能声明法宝基础属性培养倍率")
            if self.nurture_affix_bonus_ratio != _DECIMAL_ZERO:
                raise ValueError("普通装备不能声明法宝词条培养倍率")

    @property
    def display_name(self) -> str:
        """返回装备当前展示名称。"""
        if self.naming is not None:
            return self.naming.resolved_name
        return f"{self.quality_name}{self.template_name}"

    @property
    def base_stat_bonus_ratio(self) -> Decimal:
        """返回总基础属性成长倍率。"""
        return self.enhancement_base_stat_bonus_ratio + self.nurture_base_stat_bonus_ratio

    @property
    def affix_bonus_ratio(self) -> Decimal:
        """返回总词条成长倍率。"""
        return self.enhancement_affix_bonus_ratio + self.nurture_affix_bonus_ratio

    @property
    def numeric_affixes(self) -> tuple[EquipmentAffixValue, ...]:
        """返回全部数值词条。"""
        return tuple(affix for affix in self.affixes if not affix.is_special)

    @property
    def special_affixes(self) -> tuple[EquipmentAffixValue, ...]:
        """返回全部特殊效果词条。"""
        return tuple(affix for affix in self.affixes if affix.is_special)

    def resolved_stat_lines(self) -> tuple[EquipmentResolvedStat, ...]:
        """返回聚合后的最终属性。"""
        totals: dict[str, int] = defaultdict(int)
        for attribute in self.base_attributes:
            totals[attribute.stat_id] += attribute.resolved_value(self.base_stat_bonus_ratio)
        for affix in self.affixes:
            if not affix.has_numeric_payload:
                continue
            totals[affix.stat_id] += affix.resolved_value(self.affix_bonus_ratio)
        return tuple(
            EquipmentResolvedStat(stat_id=stat_id, value=value)
            for stat_id, value in sorted(totals.items(), key=lambda item: item[0])
        )

    def resolved_stat_map(self) -> Mapping[str, int]:
        """返回按属性标识索引的最终属性只读视图。"""
        return MappingProxyType({line.stat_id: line.value for line in self.resolved_stat_lines()})

    def with_name(self, naming: EquipmentNamingRecord) -> "EquipmentItem":
        """返回带命名结果的新装备实例。"""
        return replace(self, naming=naming)


@dataclass(frozen=True, slots=True)
class EquipmentGenerationRequest:
    """装备生成请求。"""

    slot_id: str
    quality_id: str
    rank_id: str
    template_id: str | None = None
    affix_count: int | None = None


@dataclass(frozen=True, slots=True)
class EquipmentEnhancementResult:
    """强化尝试结果。"""

    item: EquipmentItem
    success: bool
    previous_level: int
    target_level: int
    success_rate: Decimal
    costs: tuple[EquipmentResourceCost, ...]
    added_affixes: tuple[EquipmentAffixValue, ...] = ()


@dataclass(frozen=True, slots=True)
class EquipmentWashResult:
    """洗炼结果。"""

    item: EquipmentItem
    locked_affix_indices: tuple[int, ...]
    costs: tuple[EquipmentResourceCost, ...]
    rerolled_affixes: tuple[EquipmentAffixValue, ...]


@dataclass(frozen=True, slots=True)
class EquipmentReforgeResult:
    """重铸结果。"""

    item: EquipmentItem
    previous_template_id: str
    previous_affixes: tuple[EquipmentAffixValue, ...]
    costs: tuple[EquipmentResourceCost, ...]


@dataclass(frozen=True, slots=True)
class ArtifactNurtureResult:
    """法宝培养结果。"""

    item: EquipmentItem
    previous_level: int
    target_level: int
    costs: tuple[EquipmentResourceCost, ...]


@dataclass(frozen=True, slots=True)
class EquipmentDismantleResult:
    """装备分解结果。"""

    item: EquipmentItem
    returns: tuple[EquipmentResourceCost, ...]


__all__ = [
    "ArtifactNurtureResult",
    "EquipmentAffixValue",
    "EquipmentAttributeValue",
    "EquipmentDismantleResult",
    "EquipmentEnhancementResult",
    "EquipmentGenerationRequest",
    "EquipmentItem",
    "EquipmentNamingRecord",
    "EquipmentRandomSource",
    "EquipmentRank",
    "EquipmentReforgeResult",
    "EquipmentResolvedStat",
    "EquipmentResourceCost",
    "EquipmentSpecialEffectValue",
    "EquipmentWashResult",
    "scale_special_effect_payload",
    "special_effect_strength_multiplier_for_quality",
]
