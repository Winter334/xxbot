"""静态配置中心失败校验测试。"""

from __future__ import annotations

from importlib.resources import files as resource_files

import infrastructure.config.static.files as static_files_package
import pytest

from infrastructure.config.static import StaticConfigValidationError, load_static_config


def _read_static_file(filename: str) -> str:
    """读取默认静态配置文本，供测试替换。"""
    return resource_files(static_files_package).joinpath(filename).read_text(encoding="utf-8")


def _replace_once(source: str, old: str, new: str) -> str:
    """执行一次文本替换，避免测试数据静默失效。"""
    if old not in source:
        raise AssertionError(f"未找到待替换片段: {old}")
    return source.replace(old, new, 1)


def _build_resource_provider(*, overrides: dict[str, str] | None = None, missing_files: set[str] | None = None):
    """构造可替换单个静态文件的资源提供者。"""
    override_map = overrides or {}
    missing = missing_files or set()

    def provider(filename: str) -> str:
        if filename in missing:
            raise FileNotFoundError(filename)
        if filename in override_map:
            return override_map[filename]
        return _read_static_file(filename)

    return provider


def _find_issue(error: StaticConfigValidationError, *, filename: str, config_path_fragment: str):
    """按文件名和路径片段查找结构化错误。"""
    for issue in error.issues:
        if issue.filename == filename and config_path_fragment in issue.config_path:
            return issue
    raise AssertionError(f"未找到匹配错误: {filename} / {config_path_fragment}")


def test_load_static_config_fails_when_toml_file_is_missing() -> None:
    """缺失静态配置文件时应直接报错。"""
    provider = _build_resource_provider(missing_files={"enemies.toml"})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="enemies.toml", config_path_fragment="<file>")

    assert issue.identifier == "enemies"
    assert issue.reason == "静态配置文件不存在"


def test_load_static_config_fails_when_required_field_is_missing() -> None:
    """缺失必填字段时应返回字段级错误。"""
    broken_breakthrough = _replace_once(
        _read_static_file("breakthrough_trials.toml"),
        'boss_template_id = "guardian"\n',
        "",
    )
    provider = _build_resource_provider(overrides={"breakthrough_trials.toml": broken_breakthrough})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="breakthrough_trials.toml", config_path_fragment="boss_template_id")

    assert issue.identifier == "boss_template_id"
    assert "Field required" in issue.reason


def test_load_static_config_fails_when_numeric_value_is_out_of_range() -> None:
    """上限类数值越界时应命中边界校验。"""
    broken_coefficients = _replace_once(
        _read_static_file("base_coefficients.toml"),
        'crit_rate_cap = "0.75"',
        'crit_rate_cap = "1.20"',
    )
    provider = _build_resource_provider(overrides={"base_coefficients.toml": broken_coefficients})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="base_coefficients.toml", config_path_fragment="crit_rate_cap")

    assert issue.identifier == "crit_rate_cap"
    assert issue.reason == "上限类参数不能大于 1"


def test_load_static_config_fails_when_cross_reference_is_invalid() -> None:
    """跨文件引用失效时应命中交叉校验。"""
    broken_breakthrough = _replace_once(
        _read_static_file("breakthrough_trials.toml"),
        'boss_template_id = "guardian"',
        'boss_template_id = "missing_template"',
    )
    provider = _build_resource_provider(overrides={"breakthrough_trials.toml": broken_breakthrough})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="breakthrough_trials.toml", config_path_fragment="boss_template_id")

    assert issue.identifier == "mortal_to_qi_refining"
    assert "missing_template" in issue.reason


def test_load_static_config_fails_when_breakthrough_material_is_duplicated() -> None:
    """同一突破映射重复声明相同材料时应命中结构校验。"""
    duplicated_material = _replace_once(
        _read_static_file("breakthrough_trials.toml"),
        'required_items = [\n  { item_type = "material", item_id = "qi_condensation_grass", quantity = 2 },\n]\n',
        'required_items = [\n  { item_type = "material", item_id = "qi_condensation_grass", quantity = 2 },\n  { item_type = "material", item_id = "qi_condensation_grass", quantity = 1 },\n]\n',
    )
    provider = _build_resource_provider(overrides={"breakthrough_trials.toml": duplicated_material})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="breakthrough_trials.toml", config_path_fragment="required_items")

    assert issue.identifier == "mortal_to_qi_refining"
    assert issue.reason == "突破材料 material:qi_condensation_grass 重复声明"


def test_load_static_config_fails_when_launch_boundary_is_exceeded() -> None:
    """出现渡劫以上开放内容时应在启动前中止。"""
    extra_realm = """

[[realms]]
realm_id = "true_immortal"
name = "真仙"
order = 11
world_segment = "immortal_segment"
stage_ids = ["early", "middle", "late", "perfect"]
"""
    broken_progression = _read_static_file("realm_progression.toml") + extra_realm
    provider = _build_resource_provider(overrides={"realm_progression.toml": broken_progression})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="realm_progression.toml", config_path_fragment="realms")

    assert issue.identifier == "realm_sequence"
    assert issue.reason == "首发开放境界必须严格覆盖凡人到渡劫，且顺序不可变"


def test_load_static_config_fails_when_endless_region_bias_cross_reference_is_invalid() -> None:
    """无尽副本区域偏置引用失效时应命中跨文件校验。"""
    broken_endless_dungeon = _replace_once(
        _read_static_file("endless_dungeon.toml"),
        'region_bias_id = "flame"',
        'region_bias_id = "missing_bias"',
    )
    provider = _build_resource_provider(overrides={"endless_dungeon.toml": broken_endless_dungeon})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="endless_dungeon.toml", config_path_fragment="region_bias_id")

    assert issue.identifier == "flame"
    assert issue.reason == "无尽副本区域引用了未定义敌人区域偏置 missing_bias"


def test_load_static_config_fails_when_endless_structure_breaks_stage5_boundary() -> None:
    """无尽副本层数结构越界时应在加载阶段中止。"""
    broken_endless_dungeon = _replace_once(
        _read_static_file("endless_dungeon.toml"),
        'floors_per_region = 20',
        'floors_per_region = 18',
    )
    provider = _build_resource_provider(overrides={"endless_dungeon.toml": broken_endless_dungeon})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="endless_dungeon.toml", config_path_fragment="floors_per_region")

    assert issue.identifier == "floors_per_region"
    assert issue.reason == "无尽副本区域必须固定为每 20 层一个区域"
