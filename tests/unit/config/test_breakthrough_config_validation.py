"""阶段 7 突破秘境配置校验测试。"""

from __future__ import annotations

from importlib.resources import files as resource_files

import infrastructure.config.static.files as static_files_package
import pytest

from infrastructure.config.static import StaticConfigValidationError, load_static_config

_EXPECTED_GROUP_IDS = ("entry_trials", "mind_palace", "void_gate")
_EXPECTED_MAPPING_IDS = (
    "mortal_to_qi_refining",
    "qi_refining_to_foundation",
    "foundation_to_core",
    "core_to_nascent_soul",
    "nascent_soul_to_deity_transformation",
    "deity_transformation_to_void_refinement",
    "void_refinement_to_body_integration",
    "body_integration_to_great_vehicle",
    "great_vehicle_to_tribulation",
)


def _read_static_file(filename: str) -> str:
    """读取默认静态配置文本，供测试替换。"""
    return resource_files(static_files_package).joinpath(filename).read_text(encoding="utf-8")



def _replace_once(source: str, old: str, new: str) -> str:
    """执行一次文本替换，避免测试片段失效。"""
    if old not in source:
        raise AssertionError(f"未找到待替换片段: {old}")
    return source.replace(old, new, 1)



def _build_resource_provider(*, overrides: dict[str, str] | None = None):
    """构造可替换单个静态文件的资源提供者。"""
    override_map = overrides or {}

    def provider(filename: str) -> str:
        if filename in override_map:
            return override_map[filename]
        return _read_static_file(filename)

    return provider



def _find_issue(error: StaticConfigValidationError, *, filename: str, reason_fragment: str):
    """按文件名和错误原因片段查找结构化校验结果。"""
    for issue in error.issues:
        if issue.filename == filename and reason_fragment in issue.reason:
            return issue
    raise AssertionError(f"未找到匹配错误: {filename} / {reason_fragment}")



def test_load_static_config_keeps_stage7_launch_group_and_mapping_boundary() -> None:
    """默认配置应固定为三组秘境与九次突破映射。"""
    static_config = load_static_config()

    assert tuple(group.group_id for group in static_config.breakthrough_trials.ordered_trial_groups) == _EXPECTED_GROUP_IDS
    assert tuple(trial.mapping_id for trial in static_config.breakthrough_trials.ordered_trials) == _EXPECTED_MAPPING_IDS



def test_load_static_config_fails_when_repeat_reward_pool_contains_duplicate_resource_id() -> None:
    """重复奖励池内的资源标识不得重复，避免同池重复发放。"""
    broken_breakthrough = _replace_once(
        _read_static_file("breakthrough_trials.toml"),
        '{ resource_kind = "material", resource_id = "enhancement_shard", quantity = 4, bound = true }',
        '{ resource_kind = "material", resource_id = "enhancement_stone", quantity = 4, bound = true }',
    )
    provider = _build_resource_provider(overrides={"breakthrough_trials.toml": broken_breakthrough})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="breakthrough_trials.toml", reason_fragment="重复奖励池存在重复资源标识")

    assert "repeat_reward_pools" in issue.config_path



def test_load_static_config_fails_when_repeat_reward_pool_resource_leaves_whitelist() -> None:
    """重复奖励池的实际资源必须全部落在奖励白名单内。"""
    broken_breakthrough = _replace_once(
        _read_static_file("breakthrough_trials.toml"),
        'resource_whitelist = ["artifact_essence"]',
        'resource_whitelist = ["spirit_stone"]',
    )
    provider = _build_resource_provider(overrides={"breakthrough_trials.toml": broken_breakthrough})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(exc_info.value, filename="breakthrough_trials.toml", reason_fragment="重复奖励池资源必须全部落在奖励白名单内")

    assert "repeat_reward_pools" in issue.config_path



def test_load_static_config_fails_when_breakthrough_repeat_reward_declares_endgame_drop() -> None:
    """突破秘境重复奖励不得混入装备或其他终局实体。"""
    broken_breakthrough = _replace_once(
        _read_static_file("breakthrough_trials.toml"),
        'resource_whitelist = ["artifact_essence"]',
        'resource_whitelist = ["iron_sword"]',
    )
    broken_breakthrough = _replace_once(
        broken_breakthrough,
        'resource_id = "artifact_essence"',
        'resource_id = "iron_sword"',
    )
    provider = _build_resource_provider(overrides={"breakthrough_trials.toml": broken_breakthrough})

    with pytest.raises(StaticConfigValidationError) as exc_info:
        load_static_config(resource_provider=provider)

    issue = _find_issue(
        exc_info.value,
        filename="breakthrough_trials.toml",
        reason_fragment="突破秘境重复奖励不得声明装备、法宝、道纹或其他终局掉落实体",
    )

    assert issue.config_path == "repeat_reward_pools[].resources[].resource_id"
    assert issue.identifier == "artifact_material_pool"
