"""静态配置加载与跨文件校验入口。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib
from importlib.resources import files as resource_files
from importlib.resources.abc import Traversable
from pydantic import ValidationError

from infrastructure.config.static import files as static_files_package
from infrastructure.config.static.errors import StaticConfigIssueCollector, StaticConfigValidationError
from infrastructure.config.static.models import (
    BaseCoefficientConfig,
    BattleTemplateConfig,
    BreakthroughTrialConfig,
    CultivationSourceConfig,
    DailyCultivationConfig,
    EndlessDungeonConfig,
    EnemyConfig,
    EquipmentConfig,
    PvpConfig,
    RealmProgressionConfig,
    SkillDropConfig,
    SkillGenerationConfig,
    SkillLineageConfig,
    SkillPathConfig,
    StaticGameConfig,
)
from infrastructure.config.static.models.cultivation import EXPECTED_SOURCE_CATEGORIES
from infrastructure.config.static.models.skill import (
    LAUNCH_SKILL_AUXILIARY_SLOT_IDS,
    SkillDropPoolDefinition,
    SkillLineageDefinition,
)

ResourcePayload = Mapping[str, Any] | str | bytes
ResourceProvider = Callable[[str], ResourcePayload]
_FORBIDDEN_BREAKTHROUGH_RESOURCE_PREFIXES: tuple[str, ...] = (
    "dao_pattern_",
    "inheritance_",
)


@dataclass(frozen=True, slots=True)
class _SectionSpec:
    """描述单个静态配置节的文件与模型映射。"""

    filename: str
    section_name: str
    model_type: type


_SECTION_SPECS: tuple[_SectionSpec, ...] = (
    _SectionSpec("realm_progression.toml", "realm_progression", RealmProgressionConfig),
    _SectionSpec("daily_cultivation.toml", "daily_cultivation", DailyCultivationConfig),
    _SectionSpec("base_coefficients.toml", "base_coefficients", BaseCoefficientConfig),
    _SectionSpec("cultivation_sources.toml", "cultivation_sources", CultivationSourceConfig),
    _SectionSpec("skill_paths.toml", "skill_paths", SkillPathConfig),
    _SectionSpec("skill_lineages.toml", "skill_lineages", SkillLineageConfig),
    _SectionSpec("skill_generation.toml", "skill_generation", SkillGenerationConfig),
    _SectionSpec("skill_drops.toml", "skill_drops", SkillDropConfig),
    _SectionSpec("battle_templates.toml", "battle_templates", BattleTemplateConfig),
    _SectionSpec("equipment.toml", "equipment", EquipmentConfig),
    _SectionSpec("enemies.toml", "enemies", EnemyConfig),
    _SectionSpec("breakthrough_trials.toml", "breakthrough_trials", BreakthroughTrialConfig),
    _SectionSpec("endless_dungeon.toml", "endless_dungeon", EndlessDungeonConfig),
    _SectionSpec("pvp.toml", "pvp", PvpConfig),
)


def load_static_config(
    *,
    resource_dir: str | Path | Traversable | None = None,
    resource_provider: ResourceProvider | None = None,
) -> StaticGameConfig:
    """加载完整静态配置，并执行单文件与跨文件校验。"""
    collector = StaticConfigIssueCollector()
    loaded_sections: dict[str, object] = {}

    for spec in _SECTION_SPECS:
        raw_data = _load_raw_section(
            spec=spec,
            collector=collector,
            resource_dir=resource_dir,
            resource_provider=resource_provider,
        )
        if raw_data is None:
            continue

        section = _validate_section(spec=spec, raw_data=raw_data, collector=collector)
        if section is None:
            continue

        loaded_sections[spec.section_name] = section
        section.collect_issues(filename=spec.filename, collector=collector)

    if len(loaded_sections) == len(_SECTION_SPECS):
        config = StaticGameConfig(**loaded_sections)
        _collect_cross_file_issues(config=config, collector=collector)
    else:
        config = None

    collector.raise_if_any()
    if config is None:
        raise StaticConfigValidationError(collector.issues)
    return config


def _load_raw_section(
    *,
    spec: _SectionSpec,
    collector: StaticConfigIssueCollector,
    resource_dir: str | Path | Traversable | None,
    resource_provider: ResourceProvider | None,
) -> dict[str, Any] | None:
    """按来源读取单个 TOML 配置。"""
    try:
        payload = _read_payload(
            filename=spec.filename,
            resource_dir=resource_dir,
            resource_provider=resource_provider,
        )
    except FileNotFoundError:
        collector.add(
            filename=spec.filename,
            config_path="<file>",
            identifier=spec.section_name,
            reason="静态配置文件不存在",
        )
        return None
    except tomllib.TOMLDecodeError as exc:
        collector.add(
            filename=spec.filename,
            config_path="<root>",
            identifier=spec.section_name,
            reason=f"TOML 解析失败：{exc}",
        )
        return None

    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, bytes):
        return tomllib.loads(payload.decode("utf-8"))
    if isinstance(payload, str):
        return tomllib.loads(payload)

    collector.add(
        filename=spec.filename,
        config_path="<provider>",
        identifier=spec.section_name,
        reason="资源提供者返回了不支持的载荷类型",
    )
    return None


def _read_payload(
    *,
    filename: str,
    resource_dir: str | Path | Traversable | None,
    resource_provider: ResourceProvider | None,
) -> ResourcePayload:
    """读取原始资源载荷。"""
    if resource_provider is not None:
        return resource_provider(filename)

    if isinstance(resource_dir, Traversable):
        resource = resource_dir.joinpath(filename)
        if not resource.is_file():
            raise FileNotFoundError(filename)
        with resource.open("rb") as handle:
            return handle.read()

    if resource_dir is not None:
        file_path = Path(resource_dir) / filename
        return file_path.read_bytes()

    resource = resource_files(static_files_package).joinpath(filename)
    if not resource.is_file():
        raise FileNotFoundError(filename)
    with resource.open("rb") as handle:
        return handle.read()


def _validate_section(
    *,
    spec: _SectionSpec,
    raw_data: dict[str, Any],
    collector: StaticConfigIssueCollector,
) -> object | None:
    """把原始字典校验为强类型模型。"""
    try:
        return spec.model_type.model_validate(raw_data)
    except ValidationError as exc:
        for error in exc.errors():
            location = tuple(str(part) for part in error.get("loc", ()))
            config_path = ".".join(location) if location else "<root>"
            identifier = location[-1] if location else spec.section_name
            collector.add(
                filename=spec.filename,
                config_path=config_path,
                identifier=identifier,
                reason=error.get("msg", "字段校验失败"),
            )
        return None


def _collect_cross_file_issues(
    *,
    config: StaticGameConfig,
    collector: StaticConfigIssueCollector,
) -> None:
    """执行跨文件引用与边界校验。"""
    progression_realm_ids = tuple(
        realm.realm_id for realm in sorted(config.realm_progression.realms, key=lambda item: item.order)
    )
    progression_stage_ids = {stage.stage_id for stage in config.realm_progression.stages}
    daily_realm_ids = tuple(entry.realm_id for entry in sorted(config.daily_cultivation.entries, key=lambda item: item.order))
    coefficient_realm_ids = tuple(
        entry.realm_id for entry in sorted(config.base_coefficients.realm_curve.entries, key=lambda item: item.order)
    )

    if daily_realm_ids != progression_realm_ids:
        collector.add(
            filename="daily_cultivation.toml",
            config_path="entries",
            identifier="realm_alignment",
            reason="标准日修为映射必须与境界配置的大境界顺序完全一致",
        )
    if coefficient_realm_ids != progression_realm_ids:
        collector.add(
            filename="base_coefficients.toml",
            config_path="realm_curve.entries",
            identifier="realm_alignment",
            reason="基础系数境界曲线必须与境界配置的大境界顺序完全一致",
        )

    _collect_cultivation_source_cross_issues(
        config=config,
        collector=collector,
        progression_realm_ids=progression_realm_ids,
    )
    _collect_skill_cross_issues(config=config, collector=collector)
    _collect_breakthrough_cross_issues(
        config=config,
        collector=collector,
        progression_realm_ids=progression_realm_ids,
        progression_stage_ids=progression_stage_ids,
    )
    _collect_equipment_cross_issues(
        config=config,
        collector=collector,
        progression_realm_ids=progression_realm_ids,
    )
    _collect_endless_dungeon_cross_issues(config=config, collector=collector)


def _collect_equipment_cross_issues(
    *,
    config: StaticGameConfig,
    collector: StaticConfigIssueCollector,
    progression_realm_ids: tuple[str, ...],
) -> None:
    """校验装备阶数与大境界配置的对应关系。"""
    equipment_rank_realm_ids = tuple(rank.mapped_realm_id for rank in config.equipment.ordered_equipment_ranks)
    if equipment_rank_realm_ids != progression_realm_ids:
        collector.add(
            filename="equipment.toml",
            config_path="equipment_ranks",
            identifier="realm_alignment",
            reason="装备阶数映射必须与境界配置的大境界顺序完全一致",
        )


def _collect_skill_cross_issues(*, config: StaticGameConfig, collector: StaticConfigIssueCollector) -> None:
    """校验功法相关静态配置之间的引用关系。"""
    path_by_id = {path.path_id: path for path in config.skill_paths.paths}
    template_by_path_id = {template.path_id: template for template in config.battle_templates.templates}
    attribute_pool_ids = {pool.pool_id for pool in config.skill_generation.attribute_pools}
    patch_pool_ids = {pool.pool_id for pool in config.skill_generation.patch_pools}
    lineage_by_id = {lineage.lineage_id: lineage for lineage in config.skill_lineages.lineages}

    for path in config.skill_paths.paths:
        template = template_by_path_id.get(path.path_id)
        if template is None:
            collector.add(
                filename="battle_templates.toml",
                config_path="templates",
                identifier=path.path_id,
                reason="每个功法流派都必须绑定一个基础行为模板",
            )
            continue
        if template.template_id != path.template_id:
            collector.add(
                filename="skill_paths.toml",
                config_path="paths[].template_id",
                identifier=path.path_id,
                reason="流派绑定的行为模板标识必须与 battle_templates.toml 中的模板一致",
            )

    for lineage in config.skill_lineages.lineages:
        path = path_by_id.get(lineage.path_id)
        if path is None:
            continue
        if lineage.attribute_pool_id not in attribute_pool_ids:
            collector.add(
                filename="skill_lineages.toml",
                config_path="lineages[].attribute_pool_id",
                identifier=lineage.lineage_id,
                reason=f"功法谱系引用了未定义属性池 {lineage.attribute_pool_id}",
            )
        for patch_pool_id in lineage.patch_pool_ids:
            if patch_pool_id not in patch_pool_ids:
                collector.add(
                    filename="skill_lineages.toml",
                    config_path="lineages[].patch_pool_ids",
                    identifier=lineage.lineage_id,
                    reason=f"功法谱系引用了未定义补丁池 {patch_pool_id}",
                )
        if lineage.skill_type == "main" and lineage.auxiliary_slot_id is not None:
            collector.add(
                filename="skill_lineages.toml",
                config_path="lineages[].auxiliary_slot_id",
                identifier=lineage.lineage_id,
                reason="主修功法谱系不能声明辅助槽位",
            )
        if lineage.skill_type == "auxiliary" and lineage.auxiliary_slot_id not in LAUNCH_SKILL_AUXILIARY_SLOT_IDS:
            collector.add(
                filename="skill_lineages.toml",
                config_path="lineages[].auxiliary_slot_id",
                identifier=lineage.lineage_id,
                reason="辅助功法谱系必须绑定有效辅助槽位",
            )
        if path.axis_id not in {axis.axis_id for axis in config.skill_paths.axes}:
            collector.add(
                filename="skill_paths.toml",
                config_path="paths[].axis_id",
                identifier=path.path_id,
                reason=f"流派引用了未定义主轴 {path.axis_id}",
            )

    for pool in config.skill_drops.pools:
        _collect_skill_drop_pool_cross_issues(
            pool=pool,
            collector=collector,
            lineage_by_id=lineage_by_id,
        )


def _collect_skill_drop_pool_cross_issues(
    *,
    pool: SkillDropPoolDefinition,
    collector: StaticConfigIssueCollector,
    lineage_by_id: dict[str, SkillLineageDefinition],
) -> None:
    """校验单个功法掉落池与谱系配置的对应关系。"""
    for entry in pool.entries:
        lineage = lineage_by_id.get(entry.lineage_id)
        if lineage is None:
            collector.add(
                filename="skill_drops.toml",
                config_path="pools[].entries[].lineage_id",
                identifier=pool.pool_id,
                reason=f"掉落池引用了未定义功法谱系 {entry.lineage_id}",
            )
            continue
        if lineage.skill_type != pool.skill_type:
            collector.add(
                filename="skill_drops.toml",
                config_path="pools[].entries[].lineage_id",
                identifier=pool.pool_id,
                reason=f"掉落池中的谱系 {entry.lineage_id} 与掉落池类型不匹配",
            )
        if pool.skill_type == "auxiliary" and lineage.auxiliary_slot_id != pool.auxiliary_slot_id:
            collector.add(
                filename="skill_drops.toml",
                config_path="pools[].entries[].lineage_id",
                identifier=pool.pool_id,
                reason=f"辅助掉落池中的谱系 {entry.lineage_id} 与槽位类型不匹配",
            )


def _collect_cultivation_source_cross_issues(
    *,
    config: StaticGameConfig,
    collector: StaticConfigIssueCollector,
    progression_realm_ids: tuple[str, ...],
) -> None:
    """校验修为来源占比与境界配置的对应关系。"""
    realm_to_sources: dict[str, list[Any]] = defaultdict(list)
    for source in config.cultivation_sources.sources:
        realm_to_sources[source.realm_id].append(source)

    for realm_id in progression_realm_ids:
        ordered_sources = tuple(sorted(realm_to_sources.get(realm_id, ()), key=lambda item: item.order))
        categories = tuple(source.source_category for source in ordered_sources)
        if categories != EXPECTED_SOURCE_CATEGORIES:
            collector.add(
                filename="cultivation_sources.toml",
                config_path="sources",
                identifier=realm_id,
                reason="每个大境界都必须完整声明闭关、高效、常规、低效尾段四类修为来源",
            )


def _collect_breakthrough_cross_issues(
    *,
    config: StaticGameConfig,
    collector: StaticConfigIssueCollector,
    progression_realm_ids: tuple[str, ...],
    progression_stage_ids: set[str],
) -> None:
    """校验突破配置与境界、敌人和资源边界的一致性。"""
    progression_transition_set = set(zip(progression_realm_ids[:-1], progression_realm_ids[1:], strict=False))
    enemy_template_ids = {template.template_id for template in config.enemies.templates}
    known_group_ids = {group.group_id for group in config.breakthrough_trials.trial_groups}
    known_environment_rule_ids = {rule.rule_id for rule in config.breakthrough_trials.environment_rules}
    known_reward_pool_ids = {pool.pool_id for pool in config.breakthrough_trials.repeat_reward_pools}

    for trial in config.breakthrough_trials.trials:
        if trial.group_id not in known_group_ids:
            collector.add(
                filename="breakthrough_trials.toml",
                config_path="trials[].group_id",
                identifier=trial.mapping_id,
                reason=f"引用了未定义秘境组 {trial.group_id}",
            )
        if trial.environment_rule_id not in known_environment_rule_ids:
            collector.add(
                filename="breakthrough_trials.toml",
                config_path="trials[].environment_rule_id",
                identifier=trial.mapping_id,
                reason=f"引用了未定义环境规则 {trial.environment_rule_id}",
            )
        if trial.repeat_reward_pool_id not in known_reward_pool_ids:
            collector.add(
                filename="breakthrough_trials.toml",
                config_path="trials[].repeat_reward_pool_id",
                identifier=trial.mapping_id,
                reason=f"引用了未定义重复奖励池 {trial.repeat_reward_pool_id}",
            )
        if trial.boss_template_id not in enemy_template_ids:
            collector.add(
                filename="breakthrough_trials.toml",
                config_path="trials[].boss_template_id",
                identifier=trial.mapping_id,
                reason=f"突破映射引用了未定义敌人模板 {trial.boss_template_id}",
            )
        if trial.boss_stage_id not in progression_stage_ids:
            collector.add(
                filename="breakthrough_trials.toml",
                config_path="trials[].boss_stage_id",
                identifier=trial.mapping_id,
                reason=f"突破映射引用的首领阶段 {trial.boss_stage_id} 未在 realm_progression.toml 中声明",
            )
        if (trial.from_realm_id, trial.to_realm_id) not in progression_transition_set:
            collector.add(
                filename="breakthrough_trials.toml",
                config_path="trials[].to_realm_id",
                identifier=trial.mapping_id,
                reason="突破映射必须严格对应相邻大境界提升",
            )
        for requirement in trial.required_items:
            if requirement.item_id.startswith(_FORBIDDEN_BREAKTHROUGH_RESOURCE_PREFIXES):
                collector.add(
                    filename="breakthrough_trials.toml",
                    config_path="trials[].required_items[].item_id",
                    identifier=trial.mapping_id,
                    reason="首发突破材料不能直接消耗道纹或传承类资源",
                )


def _collect_endless_dungeon_cross_issues(
    *,
    config: StaticGameConfig,
    collector: StaticConfigIssueCollector,
) -> None:
    """校验无尽副本与敌人配置的引用关系。"""
    region_bias_ids = {bias.region_bias_id for bias in config.enemies.region_biases}
    for region in config.endless_dungeon.regions:
        if region.region_bias_id not in region_bias_ids:
            collector.add(
                filename="endless_dungeon.toml",
                config_path="regions[].region_bias_id",
                identifier=region.region_id,
                reason=f"无尽副本区域引用了未定义敌人区域偏置 {region.region_bias_id}",
            )
