"""突破材料秘境与突破秘境共用的轻量动态难度服务。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from application.character.current_attribute_service import CurrentAttributeService, CurrentAttributeSnapshot
from application.dungeon.endless_service import _DEFAULT_HERO_TEMPLATE_ID, _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID
from infrastructure.config.static import StaticGameConfig, get_static_config

_DECIMAL_ONE = Decimal("1")
_DECIMAL_THOUSAND = Decimal("1000")
_CORRECTION_RATIO = Decimal("0.25")
_MIN_RATIO_PERMILLE = 850
_MAX_RATIO_PERMILLE = 1250
_MAX_ADJUSTMENT_PERMILLE = 80


@dataclass(frozen=True, slots=True)
class BreakthroughDynamicDifficultySnapshot:
    """轻量动态难度计算结果。"""

    base_scale_permille: int
    adjusted_scale_permille: int
    adjustment_permille: int
    player_power_ratio_permille: int


class BreakthroughDynamicDifficultyService:
    """以境界基准为主、玩家实时属性轻微修正的动态难度服务。"""

    def __init__(
        self,
        *,
        current_attribute_service: CurrentAttributeService,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._current_attribute_service = current_attribute_service
        self._static_config = static_config or get_static_config()
        self._realm_coefficient_by_realm_id = {
            entry.realm_id: Decimal(entry.coefficient)
            for entry in self._static_config.base_coefficients.realm_curve.entries
        }
        self._stage_multiplier_by_stage_id = {
            stage.stage_id: Decimal(stage.multiplier)
            for stage in self._static_config.realm_progression.stages
        }

    def resolve_enemy_scale(
        self,
        *,
        character_id: int,
        base_scale_permille: int,
    ) -> BreakthroughDynamicDifficultySnapshot:
        """按当前角色实时属性给基础敌人倍率附加轻量修正。"""
        attributes = self._current_attribute_service.get_pve_view(character_id=character_id)
        power_ratio_permille = self._estimate_player_power_ratio_permille(attributes=attributes)
        centered_ratio = Decimal(power_ratio_permille - 1000) * _CORRECTION_RATIO / _DECIMAL_ONE
        adjustment_permille = _clamp_int(
            _round_decimal_to_int(centered_ratio),
            lower=-_MAX_ADJUSTMENT_PERMILLE,
            upper=_MAX_ADJUSTMENT_PERMILLE,
        )
        adjusted_scale_permille = max(1, int(base_scale_permille) + adjustment_permille)
        return BreakthroughDynamicDifficultySnapshot(
            base_scale_permille=max(1, int(base_scale_permille)),
            adjusted_scale_permille=adjusted_scale_permille,
            adjustment_permille=adjustment_permille,
            player_power_ratio_permille=power_ratio_permille,
        )

    def _estimate_player_power_ratio_permille(self, *, attributes: CurrentAttributeSnapshot) -> int:
        template_id = attributes.behavior_template_id
        if template_id not in _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID:
            template_id = _DEFAULT_HERO_TEMPLATE_ID
        profile = _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID[template_id]
        hp_ratio = self._build_ratio_permille(
            current_value=attributes.max_hp,
            baseline_value=self._calculate_base_hp(
                realm_id=attributes.realm_id,
                stage_id=attributes.stage_id,
                factor=_read_decimal(profile.get("hp_factor"), default=Decimal("1.0")),
            ),
        )
        attack_ratio = self._build_ratio_permille(
            current_value=attributes.attack_power,
            baseline_value=self._calculate_base_attack(
                realm_id=attributes.realm_id,
                stage_id=attributes.stage_id,
                factor=_read_decimal(profile.get("attack_factor"), default=Decimal("1.0")),
            ),
        )
        guard_ratio = self._build_ratio_permille(
            current_value=attributes.guard_power,
            baseline_value=self._calculate_base_guard(
                realm_id=attributes.realm_id,
                stage_id=attributes.stage_id,
                factor=_read_decimal(profile.get("guard_factor"), default=Decimal("1.0")),
            ),
        )
        speed_ratio = self._build_ratio_permille(
            current_value=attributes.speed,
            baseline_value=self._calculate_base_speed(
                realm_id=attributes.realm_id,
                stage_id=attributes.stage_id,
                factor=_read_decimal(profile.get("speed_factor"), default=Decimal("1.0")),
            ),
        )
        weighted_ratio = (
            hp_ratio * 300
            + attack_ratio * 400
            + guard_ratio * 200
            + speed_ratio * 100
        ) // 1000
        return _clamp_int(weighted_ratio, lower=_MIN_RATIO_PERMILLE, upper=_MAX_RATIO_PERMILLE)

    @staticmethod
    def _build_ratio_permille(*, current_value: int, baseline_value: int) -> int:
        normalized_baseline = max(1, int(baseline_value))
        ratio = (Decimal(max(0, int(current_value))) * _DECIMAL_THOUSAND / Decimal(normalized_baseline)).quantize(
            _DECIMAL_ONE,
            rounding=ROUND_HALF_UP,
        )
        return _clamp_int(int(ratio), lower=_MIN_RATIO_PERMILLE, upper=_MAX_RATIO_PERMILLE)

    def _calculate_base_hp(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_hp,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("12"),
            factor=factor,
            minimum=1,
        )

    def _calculate_base_attack(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_attack,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("2"),
            factor=factor,
            minimum=1,
        )

    def _calculate_base_guard(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_defense,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("4"),
            factor=factor,
            minimum=0,
        )

    def _calculate_base_speed(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        base_speed = Decimal(self._static_config.base_coefficients.scalar.base_speed)
        realm_coefficient = self._resolve_realm_coefficient(realm_id)
        stage_multiplier = self._resolve_stage_multiplier(stage_id)
        scaled_value = (base_speed + realm_coefficient * Decimal("2")) * stage_multiplier * factor
        return max(1, _round_decimal_to_int(scaled_value))

    def _calculate_scaled_stat(
        self,
        *,
        base_value: int,
        realm_id: str,
        stage_id: str,
        divisor: Decimal,
        factor: Decimal,
        minimum: int,
    ) -> int:
        realm_coefficient = self._resolve_realm_coefficient(realm_id)
        stage_multiplier = self._resolve_stage_multiplier(stage_id)
        scaled_value = Decimal(base_value) * realm_coefficient * stage_multiplier * factor / divisor
        return max(minimum, _round_decimal_to_int(scaled_value))

    def _resolve_realm_coefficient(self, realm_id: str) -> Decimal:
        try:
            return self._realm_coefficient_by_realm_id[realm_id]
        except KeyError as exc:
            raise RuntimeError(f"未找到大境界基准系数：{realm_id}") from exc

    def _resolve_stage_multiplier(self, stage_id: str) -> Decimal:
        try:
            return self._stage_multiplier_by_stage_id[stage_id]
        except KeyError as exc:
            raise RuntimeError(f"未找到小阶段倍率：{stage_id}") from exc


def _read_decimal(value: object, *, default: Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception:
            return default
    return default


def _round_decimal_to_int(value: Decimal) -> int:
    return int(value.quantize(_DECIMAL_ONE, rounding=ROUND_HALF_UP))


def _clamp_int(value: int, *, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))


__all__ = [
    "BreakthroughDynamicDifficultyService",
    "BreakthroughDynamicDifficultySnapshot",
]
