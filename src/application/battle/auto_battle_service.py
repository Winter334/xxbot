"""自动战斗最小应用服务编排。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from domain.battle import (
    AuxiliarySkillParameterPatch,
    BattleSnapshot,
    BattleTemplateParser,
    BattleTurnEngine,
    CompiledBehaviorTemplate,
    SeededBattleRandomSource,
)
from domain.battle.reporting import BattleReportArtifacts, BattleReportBuilder
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import BattleReport, CharacterProgress
from infrastructure.db.repositories import (
    BattleRecordRepository,
    CharacterAggregate,
    CharacterRepository,
)


@dataclass(frozen=True, slots=True)
class AutoBattleRequest:
    """单次自动战斗执行请求。"""

    character_id: int
    battle_type: str
    snapshot: BattleSnapshot
    opponent_ref: str | None = None
    focus_unit_id: str | None = None
    environment_snapshot: Mapping[str, str | int | bool | None] | None = None
    template_patches_by_template_id: Mapping[str, tuple[AuxiliarySkillParameterPatch, ...]] | None = None
    template_path_id_by_template_id: Mapping[str, str] | None = None
    persist_progress_writeback: bool = True

    def __post_init__(self) -> None:
        if self.character_id <= 0:
            raise ValueError("character_id 必须为正整数")
        if not self.battle_type or not self.battle_type.strip():
            raise ValueError("battle_type 不能为空")
        if self.focus_unit_id is not None and not self.focus_unit_id.strip():
            raise ValueError("focus_unit_id 不能为空白字符串")
        if self.opponent_ref is not None and not self.opponent_ref.strip():
            raise ValueError("opponent_ref 不能为空白字符串")


@dataclass(frozen=True, slots=True)
class AutoBattleReportRecordPayload:
    """战报持久化映射载荷。"""

    character_id: int
    battle_type: str
    result: str
    opponent_ref: str | None
    summary_json: dict[str, object]
    detail_log_json: dict[str, object]

    def to_model(self) -> BattleReport:
        """转换为战报 ORM 对象。"""
        return BattleReport(
            character_id=self.character_id,
            battle_type=self.battle_type,
            result=self.result,
            opponent_ref=self.opponent_ref,
            summary_json=self.summary_json,
            detail_log_json=self.detail_log_json,
        )


@dataclass(frozen=True, slots=True)
class AutoBattleProgressWriteback:
    """角色当前血蓝比回写载荷。"""

    character_id: int
    current_hp_ratio: Decimal
    current_mp_ratio: Decimal
    injury_level: str
    can_continue: bool
    loss_tags: tuple[str, ...]

    def apply_to(self, progress: CharacterProgress) -> CharacterProgress:
        """把战损结果回写到角色成长状态。"""
        progress.current_hp_ratio = self.current_hp_ratio
        progress.current_mp_ratio = self.current_mp_ratio
        return progress

    def to_payload(self) -> dict[str, object]:
        """导出回写说明载荷。"""
        return {
            "character_id": self.character_id,
            "current_hp_ratio": format(self.current_hp_ratio, ".4f"),
            "current_mp_ratio": format(self.current_mp_ratio, ".4f"),
            "injury_level": self.injury_level,
            "can_continue": self.can_continue,
            "loss_tags": list(self.loss_tags),
        }


@dataclass(frozen=True, slots=True)
class AutoBattlePersistenceMapping:
    """自动战斗结果到持久化边界的映射。"""

    battle_report_payload: AutoBattleReportRecordPayload
    progress_writeback: AutoBattleProgressWriteback
    drop_record_payloads: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class AutoBattleExecutionResult:
    """自动战斗应用服务执行结果。"""

    request: AutoBattleRequest
    focus_unit_id: str
    compiled_templates: dict[str, CompiledBehaviorTemplate]
    domain_result: object
    report_artifacts: BattleReportArtifacts
    persistence_mapping: AutoBattlePersistenceMapping
    persisted_battle_report_id: int | None = None


class AutoBattleServiceError(RuntimeError):
    """自动战斗应用服务基础异常。"""


class AutoBattleCharacterStateError(AutoBattleServiceError):
    """角色聚合状态不满足自动战斗执行条件。"""


class AutoBattleService:
    """编排模板解析、回合执行、战报构建与战损回写。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        battle_record_repository: BattleRecordRepository,
        static_config: StaticGameConfig | None = None,
        template_parser: BattleTemplateParser | None = None,
        turn_engine: BattleTurnEngine | None = None,
        report_builder: BattleReportBuilder | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._battle_record_repository = battle_record_repository
        self._static_config = static_config or get_static_config()
        self._template_parser = template_parser or BattleTemplateParser(
            template_config=self._static_config.battle_templates,
            skill_path_config=self._static_config.skill_paths,
        )
        self._turn_engine = turn_engine or BattleTurnEngine()
        self._report_builder = report_builder or BattleReportBuilder()

    def execute(
        self,
        *,
        request: AutoBattleRequest,
        persist: bool = True,
    ) -> AutoBattleExecutionResult:
        """执行单场自动战斗，并按需落库。"""
        aggregate = self._require_character_aggregate(request.character_id)
        focus_unit_id = self._resolve_focus_unit_id(request=request)
        compiled_templates = self._compile_behavior_templates(request=request)
        random_source = SeededBattleRandomSource(seed=request.snapshot.seed)
        domain_result = self._turn_engine.execute(
            snapshot=request.snapshot,
            behavior_templates=compiled_templates,
            random_source=random_source,
        )
        report_artifacts = self._report_builder.build(
            snapshot=request.snapshot,
            result=domain_result,
            behavior_templates=compiled_templates,
            template_config_version=self._static_config.battle_templates.config_version,
            focus_unit_id=focus_unit_id,
            environment_snapshot=request.environment_snapshot,
        )
        persistence_mapping = self._build_persistence_mapping(
            request=request,
            report_artifacts=report_artifacts,
        )

        persisted_battle_report_id: int | None = None
        if persist:
            persisted_report = self._persist_result(
                request=request,
                aggregate=aggregate,
                mapping=persistence_mapping,
            )
            persisted_battle_report_id = persisted_report.id

        return AutoBattleExecutionResult(
            request=request,
            focus_unit_id=focus_unit_id,
            compiled_templates=compiled_templates,
            domain_result=domain_result,
            report_artifacts=report_artifacts,
            persistence_mapping=persistence_mapping,
            persisted_battle_report_id=persisted_battle_report_id,
        )

    def _persist_result(
        self,
        *,
        request: AutoBattleRequest,
        aggregate: CharacterAggregate,
        mapping: AutoBattlePersistenceMapping,
    ) -> BattleReport:
        """把战报与战损回写到现有持久化边界。"""
        progress = aggregate.progress
        if progress is None:
            raise AutoBattleCharacterStateError(f"角色缺少成长状态：{aggregate.character.id}")
        if request.persist_progress_writeback:
            self._character_repository.save_progress(mapping.progress_writeback.apply_to(progress))
        return self._battle_record_repository.add_battle_report(mapping.battle_report_payload.to_model())

    def _build_persistence_mapping(
        self,
        *,
        request: AutoBattleRequest,
        report_artifacts: BattleReportArtifacts,
    ) -> AutoBattlePersistenceMapping:
        """构建对现有仓储与数据库字段的映射。"""
        loss_result = report_artifacts.loss
        return AutoBattlePersistenceMapping(
            battle_report_payload=AutoBattleReportRecordPayload(
                character_id=request.character_id,
                battle_type=request.battle_type,
                result=report_artifacts.summary.result,
                opponent_ref=request.opponent_ref,
                summary_json=report_artifacts.summary.to_payload(),
                detail_log_json=report_artifacts.detail.to_payload(),
            ),
            progress_writeback=AutoBattleProgressWriteback(
                character_id=request.character_id,
                current_hp_ratio=Decimal(loss_result.final_hp_ratio),
                current_mp_ratio=Decimal(loss_result.final_mp_ratio),
                injury_level=loss_result.injury_level,
                can_continue=loss_result.can_continue,
                loss_tags=loss_result.loss_tags,
            ),
            drop_record_payloads=(),
        )

    def _compile_behavior_templates(self, *, request: AutoBattleRequest) -> dict[str, CompiledBehaviorTemplate]:
        """按输入快照中声明的模板标识编译运行期模板。"""
        template_patches = request.template_patches_by_template_id or {}
        template_path_id_map = request.template_path_id_by_template_id or {}
        compiled_templates: dict[str, CompiledBehaviorTemplate] = {}
        template_ids = sorted(
            {
                unit.behavior_template_id
                for unit in (*request.snapshot.allies, *request.snapshot.enemies)
            }
        )
        for template_id in template_ids:
            source_path_id = template_path_id_map.get(template_id, template_id)
            compiled_templates[template_id] = self._template_parser.parse_template(
                path_id=source_path_id,
                patches=template_patches.get(template_id, ()),
            )
        return compiled_templates

    def _resolve_focus_unit_id(self, *, request: AutoBattleRequest) -> str:
        """确定需要映射回角色进度的焦点单位。"""
        ally_unit_ids = tuple(unit.unit_id for unit in request.snapshot.allies)
        if request.focus_unit_id is None:
            if len(ally_unit_ids) != 1:
                raise AutoBattleServiceError("多友方快照场景必须显式提供 focus_unit_id")
            return ally_unit_ids[0]
        if request.focus_unit_id not in ally_unit_ids:
            raise AutoBattleServiceError(f"focus_unit_id 不属于友方快照：{request.focus_unit_id}")
        return request.focus_unit_id

    def _require_character_aggregate(self, character_id: int) -> CharacterAggregate:
        """读取并校验角色聚合状态。"""
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise AutoBattleCharacterStateError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise AutoBattleCharacterStateError(f"角色缺少成长状态：{character_id}")
        return aggregate


__all__ = [
    "AutoBattleCharacterStateError",
    "AutoBattleExecutionResult",
    "AutoBattlePersistenceMapping",
    "AutoBattleProgressWriteback",
    "AutoBattleReportRecordPayload",
    "AutoBattleRequest",
    "AutoBattleService",
    "AutoBattleServiceError",
]
