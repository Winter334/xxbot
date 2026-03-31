"""修炼面板查询与单次修炼适配服务。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from application.character.growth_service import CharacterGrowthSnapshot, CharacterGrowthService
from application.character.progression_service import BreakthroughPrecheckResult, CharacterProgressionService
from application.character.retreat_service import RetreatService, RetreatStatusSnapshot
from application.healing import HealingPanelService, RecoveryActionBlockedError
from infrastructure.config.static import StaticGameConfig, get_static_config

_ACTIVE_PRACTICE_SOURCE_CATEGORY = "active_peak"
_DECIMAL_INTEGER = Decimal("1")
_RUNNING_RETREAT_STATUS = "running"


@dataclass(frozen=True, slots=True)
class CultivationPanelSnapshot:
    """修炼与闭关面板所需的聚合快照。"""

    growth_snapshot: CharacterGrowthSnapshot
    breakthrough_precheck: BreakthroughPrecheckResult
    retreat_status: RetreatStatusSnapshot | None
    practice_cultivation_amount: int


@dataclass(frozen=True, slots=True)
class PracticeOnceResult:
    """单次修炼动作结果。"""

    snapshot: CultivationPanelSnapshot
    requested_amount: int
    applied_amount: int
    previous_stage_id: str
    stage_changed: bool


class CultivationPanelServiceError(RuntimeError):
    """修炼面板服务基础异常。"""


class CultivationPracticeBlockedError(CultivationPanelServiceError):
    """当前状态不允许执行单次修炼。"""


class CultivationPanelConfigError(CultivationPanelServiceError):
    """修炼面板依赖的静态配置缺失。"""


class CultivationPanelService:
    """聚合修炼面板查询，并提供单次修炼动作。"""

    def __init__(
        self,
        *,
        growth_service: CharacterGrowthService,
        progression_service: CharacterProgressionService,
        retreat_service: RetreatService,
        healing_panel_service: HealingPanelService,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._growth_service = growth_service
        self._progression_service = progression_service
        self._retreat_service = retreat_service
        self._healing_panel_service = healing_panel_service
        self._static_config = static_config or get_static_config()

    def get_panel_snapshot(self, *, character_id: int) -> CultivationPanelSnapshot:
        """读取修炼与闭关面板快照。"""
        growth_snapshot = self._growth_service.get_snapshot(character_id=character_id)
        breakthrough_precheck = self._progression_service.get_breakthrough_precheck(character_id=character_id)
        retreat_status = self._retreat_service.get_retreat_status(character_id=character_id)
        practice_cultivation_amount = self._resolve_active_practice_amount(realm_id=growth_snapshot.realm_id)
        return CultivationPanelSnapshot(
            growth_snapshot=growth_snapshot,
            breakthrough_precheck=breakthrough_precheck,
            retreat_status=retreat_status,
            practice_cultivation_amount=practice_cultivation_amount,
        )

    def practice_once(self, *, character_id: int) -> PracticeOnceResult:
        """执行一次主动修炼，按主动高效段口径增加修为。"""
        current_snapshot = self.get_panel_snapshot(character_id=character_id)
        if self._is_retreat_running(current_snapshot.retreat_status):
            raise CultivationPracticeBlockedError(f"角色闭关中，无法执行单次修炼：{character_id}")
        try:
            self._healing_panel_service.ensure_action_not_blocked_by_recovery(
                character_id=character_id,
                action_label="单次修炼",
            )
        except RecoveryActionBlockedError as exc:
            raise CultivationPracticeBlockedError(str(exc)) from exc

        cultivation_result = self._growth_service.add_cultivation(
            character_id=character_id,
            amount=current_snapshot.practice_cultivation_amount,
        )
        updated_snapshot = self.get_panel_snapshot(character_id=character_id)
        return PracticeOnceResult(
            snapshot=updated_snapshot,
            requested_amount=current_snapshot.practice_cultivation_amount,
            applied_amount=cultivation_result.applied_amount,
            previous_stage_id=cultivation_result.previous_stage_id,
            stage_changed=cultivation_result.stage_changed,
        )

    def _resolve_active_practice_amount(self, *, realm_id: str) -> int:
        daily_amount = self._get_daily_cultivation_amount(realm_id=realm_id)
        ratio = self._get_active_practice_ratio(realm_id=realm_id)
        scaled_amount = int((Decimal(daily_amount) * ratio).quantize(_DECIMAL_INTEGER, rounding=ROUND_DOWN))
        return max(1, scaled_amount)

    def _get_daily_cultivation_amount(self, *, realm_id: str) -> int:
        for entry in self._static_config.daily_cultivation.entries:
            if entry.realm_id == realm_id:
                return entry.daily_cultivation
        raise CultivationPanelConfigError(f"缺少标准日修为配置：{realm_id}")

    def _get_active_practice_ratio(self, *, realm_id: str) -> Decimal:
        for source in self._static_config.cultivation_sources.sources:
            if source.realm_id == realm_id and source.source_category == _ACTIVE_PRACTICE_SOURCE_CATEGORY:
                return source.ratio
        raise CultivationPanelConfigError(f"缺少主动修炼来源配置：{realm_id}")

    @staticmethod
    def _is_retreat_running(retreat_status: RetreatStatusSnapshot | None) -> bool:
        if retreat_status is None:
            return False
        return retreat_status.status == _RUNNING_RETREAT_STATUS and retreat_status.settled_at is None


__all__ = [
    "CultivationPanelConfigError",
    "CultivationPanelService",
    "CultivationPanelServiceError",
    "CultivationPanelSnapshot",
    "CultivationPracticeBlockedError",
    "PracticeOnceResult",
]
