"""角色突破前置条件与正式破境执行服务。"""

from __future__ import annotations

from dataclasses import dataclass

from application.character.growth_service import CharacterGrowthStateError, CharacterNotFoundError
from application.ranking.score_service import CharacterScoreService
from domain.character import (
    CharacterGrowthProgression,
    GrowthRuleNotFoundError,
    RealmGrowthRule,
    resolve_breakthrough_comprehension_threshold,
)
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, InventoryRepository

_GAP_TYPE_OPEN_LIMIT = "open_limit"
_GAP_TYPE_CULTIVATION_INSUFFICIENT = "cultivation_insufficient"
_GAP_TYPE_COMPREHENSION_INSUFFICIENT = "comprehension_insufficient"
_GAP_TYPE_QUALIFICATION_MISSING = "qualification_missing"
_GAP_TYPE_MATERIAL_INSUFFICIENT = "material_insufficient"


@dataclass(frozen=True, slots=True)
class BreakthroughPrecheckGap:
    """单个突破缺口。"""

    gap_type: str
    current_value: int | None = None
    required_value: int | None = None
    missing_value: int | None = None
    item_type: str | None = None
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class BreakthroughPrecheckResult:
    """下一次大境界突破的只读预检结果。"""

    character_id: int
    current_realm_id: str
    current_realm_name: str
    target_realm_id: str | None
    target_realm_name: str | None
    mapping_id: str | None
    passed: bool
    current_cultivation_value: int
    required_cultivation_value: int | None
    current_comprehension_value: int
    required_comprehension_value: int | None
    qualification_obtained: bool
    gaps: tuple[BreakthroughPrecheckGap, ...]


@dataclass(frozen=True, slots=True)
class BreakthroughConsumedItem:
    """正式突破时消耗的单条材料记录。"""

    item_type: str
    item_id: str
    quantity: int
    before_quantity: int
    after_quantity: int


@dataclass(frozen=True, slots=True)
class BreakthroughExecutionResult:
    """正式突破执行后的稳定返回结构。"""

    character_id: int
    mapping_id: str
    from_realm_id: str
    from_realm_name: str
    to_realm_id: str
    to_realm_name: str
    new_stage_id: str
    new_stage_name: str
    previous_cultivation_value: int
    new_cultivation_value: int
    previous_comprehension_value: int
    consumed_comprehension_value: int
    remaining_comprehension_value: int
    qualification_consumed: bool
    consumed_items: tuple[BreakthroughConsumedItem, ...]


class CharacterProgressionServiceError(RuntimeError):
    """角色突破服务基础异常。"""


class BreakthroughConfigError(CharacterProgressionServiceError):
    """突破静态配置损坏。"""


class BreakthroughExecutionBlockedError(CharacterProgressionServiceError):
    """正式突破执行被前置条件阻断。"""


class CharacterProgressionService:
    """负责读取下一次大境界突破前置条件，并执行正式破境写回。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        inventory_repository: InventoryRepository,
        static_config: StaticGameConfig | None = None,
        score_service: CharacterScoreService | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._inventory_repository = inventory_repository
        self._static_config = static_config or get_static_config()
        self._progression = CharacterGrowthProgression(self._static_config)
        self._score_service = score_service
        ordered_realms = tuple(sorted(self._static_config.realm_progression.realms, key=lambda item: item.order))
        if not ordered_realms:
            raise BreakthroughConfigError("未找到任何已配置的大境界")
        self._max_realm_id = ordered_realms[-1].realm_id

    def get_breakthrough_precheck(self, *, character_id: int) -> BreakthroughPrecheckResult:
        """读取角色当前是否满足下一次大境界突破前置条件。"""
        aggregate = self._require_aggregate(character_id)
        progress = aggregate.progress
        assert progress is not None

        current_rule = self._require_realm_rule(progress.realm_id)
        trial = self._static_config.breakthrough_trials.get_trial_by_from_realm_id(progress.realm_id)

        if trial is None:
            return self._build_open_limit_result(
                character_id=character_id,
                current_rule=current_rule,
                current_cultivation_value=max(0, progress.cultivation_value),
                current_comprehension_value=max(0, progress.comprehension_value),
                qualification_obtained=progress.breakthrough_qualification_obtained,
                current_realm_id=progress.realm_id,
            )

        target_rule = self._require_realm_rule(trial.to_realm_id)
        required_comprehension_value = resolve_breakthrough_comprehension_threshold(
            static_config=self._static_config,
            realm_id=progress.realm_id,
        )
        current_cultivation_value = max(0, progress.cultivation_value)
        current_comprehension_value = max(0, progress.comprehension_value)
        gaps: list[BreakthroughPrecheckGap] = []

        cultivation_gap = current_rule.total_cultivation - current_cultivation_value
        if cultivation_gap > 0:
            gaps.append(
                BreakthroughPrecheckGap(
                    gap_type=_GAP_TYPE_CULTIVATION_INSUFFICIENT,
                    current_value=current_cultivation_value,
                    required_value=current_rule.total_cultivation,
                    missing_value=cultivation_gap,
                )
            )

        comprehension_gap = required_comprehension_value - current_comprehension_value
        if comprehension_gap > 0:
            gaps.append(
                BreakthroughPrecheckGap(
                    gap_type=_GAP_TYPE_COMPREHENSION_INSUFFICIENT,
                    current_value=current_comprehension_value,
                    required_value=required_comprehension_value,
                    missing_value=comprehension_gap,
                )
            )

        if not progress.breakthrough_qualification_obtained:
            gaps.append(
                BreakthroughPrecheckGap(
                    gap_type=_GAP_TYPE_QUALIFICATION_MISSING,
                    current_value=0,
                    required_value=1,
                    missing_value=1,
                )
            )

        for requirement in trial.required_items:
            owned_item = self._inventory_repository.get_item(
                character_id,
                requirement.item_type,
                requirement.item_id,
            )
            owned_quantity = 0 if owned_item is None else max(0, owned_item.quantity)
            missing_quantity = requirement.quantity - owned_quantity
            if missing_quantity > 0:
                gaps.append(
                    BreakthroughPrecheckGap(
                        gap_type=_GAP_TYPE_MATERIAL_INSUFFICIENT,
                        current_value=owned_quantity,
                        required_value=requirement.quantity,
                        missing_value=missing_quantity,
                        item_type=requirement.item_type,
                        item_id=requirement.item_id,
                    )
                )

        return BreakthroughPrecheckResult(
            character_id=character_id,
            current_realm_id=current_rule.realm_id,
            current_realm_name=current_rule.realm_name,
            target_realm_id=target_rule.realm_id,
            target_realm_name=target_rule.realm_name,
            mapping_id=trial.mapping_id,
            passed=not gaps,
            current_cultivation_value=current_cultivation_value,
            required_cultivation_value=current_rule.total_cultivation,
            current_comprehension_value=current_comprehension_value,
            required_comprehension_value=required_comprehension_value,
            qualification_obtained=progress.breakthrough_qualification_obtained,
            gaps=tuple(gaps),
        )

    def execute_breakthrough(self, *, character_id: int) -> BreakthroughExecutionResult:
        """执行正式破境，并写回大境界、小阶段、修为、感悟与材料消耗。"""
        aggregate = self._require_aggregate(character_id)
        progress = aggregate.progress
        assert progress is not None

        precheck = self.get_breakthrough_precheck(character_id=character_id)
        if not precheck.passed or precheck.target_realm_id is None or precheck.mapping_id is None:
            raise BreakthroughExecutionBlockedError(self._build_execution_blocked_message(precheck=precheck))

        trial = self._static_config.breakthrough_trials.get_trial(precheck.mapping_id)
        if trial is None:
            raise BreakthroughConfigError(f"未定义的突破映射：{precheck.mapping_id}")
        target_rule = self._require_realm_rule(trial.to_realm_id)
        required_comprehension_value = resolve_breakthrough_comprehension_threshold(
            static_config=self._static_config,
            realm_id=precheck.current_realm_id,
        )
        if not target_rule.stage_thresholds:
            raise BreakthroughConfigError(f"目标大境界缺少小阶段配置：{target_rule.realm_id}")
        target_stage = target_rule.stage_thresholds[0]

        previous_cultivation_value = max(0, progress.cultivation_value)
        previous_comprehension_value = max(0, progress.comprehension_value)
        consumed_items = self._consume_required_items(
            character_id=character_id,
            requirements=trial.required_items,
        )

        progress.realm_id = target_rule.realm_id
        progress.stage_id = target_stage.stage_id
        progress.cultivation_value = 0
        progress.comprehension_value = max(0, previous_comprehension_value - required_comprehension_value)
        progress.breakthrough_qualification_obtained = False
        self._character_repository.save_progress(progress)
        self._refresh_score_if_configured(character_id)

        return BreakthroughExecutionResult(
            character_id=character_id,
            mapping_id=trial.mapping_id,
            from_realm_id=precheck.current_realm_id,
            from_realm_name=precheck.current_realm_name,
            to_realm_id=target_rule.realm_id,
            to_realm_name=target_rule.realm_name,
            new_stage_id=target_stage.stage_id,
            new_stage_name=target_stage.stage_name,
            previous_cultivation_value=previous_cultivation_value,
            new_cultivation_value=progress.cultivation_value,
            previous_comprehension_value=previous_comprehension_value,
            consumed_comprehension_value=required_comprehension_value,
            remaining_comprehension_value=progress.comprehension_value,
            qualification_consumed=True,
            consumed_items=consumed_items,
        )

    def _build_open_limit_result(
        self,
        *,
        character_id: int,
        current_rule: RealmGrowthRule,
        current_cultivation_value: int,
        current_comprehension_value: int,
        qualification_obtained: bool,
        current_realm_id: str,
    ) -> BreakthroughPrecheckResult:
        if current_realm_id != self._max_realm_id:
            raise BreakthroughConfigError(f"当前大境界缺少突破映射：{current_realm_id}")

        return BreakthroughPrecheckResult(
            character_id=character_id,
            current_realm_id=current_rule.realm_id,
            current_realm_name=current_rule.realm_name,
            target_realm_id=None,
            target_realm_name=None,
            mapping_id=None,
            passed=False,
            current_cultivation_value=current_cultivation_value,
            required_cultivation_value=None,
            current_comprehension_value=current_comprehension_value,
            required_comprehension_value=None,
            qualification_obtained=qualification_obtained,
            gaps=(BreakthroughPrecheckGap(gap_type=_GAP_TYPE_OPEN_LIMIT),),
        )

    def _consume_required_items(self, *, character_id: int, requirements) -> tuple[BreakthroughConsumedItem, ...]:
        consumed_items: list[BreakthroughConsumedItem] = []
        for requirement in requirements:
            required_quantity = max(0, requirement.quantity)
            if required_quantity <= 0:
                continue
            inventory_item = self._inventory_repository.get_item(
                character_id,
                requirement.item_type,
                requirement.item_id,
            )
            owned_quantity = 0 if inventory_item is None else max(0, inventory_item.quantity)
            if owned_quantity < required_quantity:
                missing_quantity = required_quantity - owned_quantity
                raise BreakthroughExecutionBlockedError(
                    f"突破材料不足：{requirement.item_id} 还差 {missing_quantity}"
                )
            assert inventory_item is not None
            before_quantity = inventory_item.quantity
            inventory_item.quantity = before_quantity - required_quantity
            self._inventory_repository.upsert_item(inventory_item)
            consumed_items.append(
                BreakthroughConsumedItem(
                    item_type=requirement.item_type,
                    item_id=requirement.item_id,
                    quantity=required_quantity,
                    before_quantity=before_quantity,
                    after_quantity=inventory_item.quantity,
                )
            )
        return tuple(consumed_items)

    def _require_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise CharacterNotFoundError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise CharacterGrowthStateError(f"角色缺少成长状态：{character_id}")
        return aggregate

    def _require_realm_rule(self, realm_id: str) -> RealmGrowthRule:
        try:
            return self._progression.get_realm_rule(realm_id)
        except GrowthRuleNotFoundError as exc:
            raise BreakthroughConfigError(f"无法解析大境界规则：{realm_id}") from exc

    def _refresh_score_if_configured(self, character_id: int) -> None:
        if self._score_service is None:
            return
        self._score_service.refresh_character_score(character_id=character_id)

    @staticmethod
    def _build_execution_blocked_message(*, precheck: BreakthroughPrecheckResult) -> str:
        if precheck.target_realm_id is None:
            return "当前已到开放上限，无法继续突破"
        gap_parts = [CharacterProgressionService._describe_gap(gap=gap) for gap in precheck.gaps]
        normalized_parts = [part for part in gap_parts if part]
        if not normalized_parts:
            return "当前未满足突破前置"
        return "当前未满足突破前置：" + "；".join(normalized_parts)

    @staticmethod
    def _describe_gap(*, gap: BreakthroughPrecheckGap) -> str:
        if gap.gap_type == _GAP_TYPE_OPEN_LIMIT:
            return "当前已到开放上限"
        if gap.gap_type == _GAP_TYPE_CULTIVATION_INSUFFICIENT:
            return f"修为还差 {gap.missing_value}"
        if gap.gap_type == _GAP_TYPE_COMPREHENSION_INSUFFICIENT:
            return f"感悟还差 {gap.missing_value}"
        if gap.gap_type == _GAP_TYPE_QUALIFICATION_MISSING:
            return "缺少突破资格"
        if gap.gap_type == _GAP_TYPE_MATERIAL_INSUFFICIENT:
            item_label = gap.item_id or "突破材料"
            return f"{item_label} 还差 {gap.missing_value}"
        return ""


__all__ = [
    "BreakthroughConfigError",
    "BreakthroughConsumedItem",
    "BreakthroughExecutionBlockedError",
    "BreakthroughExecutionResult",
    "BreakthroughPrecheckGap",
    "BreakthroughPrecheckResult",
    "CharacterProgressionService",
    "CharacterProgressionServiceError",
]
