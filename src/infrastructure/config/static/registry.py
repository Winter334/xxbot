"""静态配置缓存入口。"""

from __future__ import annotations

from pathlib import Path

from importlib.resources.abc import Traversable

from infrastructure.config.static.loader import ResourceProvider, load_static_config
from infrastructure.config.static.models import BattleTemplateConfig, StaticGameConfig

_STATIC_CONFIG_CACHE: StaticGameConfig | None = None


def get_static_config(
    *,
    resource_dir: str | Path | Traversable | None = None,
    resource_provider: ResourceProvider | None = None,
) -> StaticGameConfig:
    """返回缓存后的静态配置。"""
    global _STATIC_CONFIG_CACHE
    if _STATIC_CONFIG_CACHE is None:
        _STATIC_CONFIG_CACHE = load_static_config(
            resource_dir=resource_dir,
            resource_provider=resource_provider,
        )
    return _STATIC_CONFIG_CACHE


def get_battle_template_config(
    *,
    resource_dir: str | Path | Traversable | None = None,
    resource_provider: ResourceProvider | None = None,
) -> BattleTemplateConfig:
    """返回缓存后的战斗行为模板配置。"""
    return get_static_config(
        resource_dir=resource_dir,
        resource_provider=resource_provider,
    ).battle_templates


def clear_static_config_cache() -> None:
    """清理进程内静态配置缓存。"""
    global _STATIC_CONFIG_CACHE
    _STATIC_CONFIG_CACHE = None


__all__ = [
    "clear_static_config_cache",
    "get_battle_template_config",
    "get_static_config",
]
