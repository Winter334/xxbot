"""启动阶段静态配置校验测试。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files as resource_files

import infrastructure.config.static.files as static_files_package
import pytest

from bot.bootstrap import build_client
from infrastructure.config.settings import get_settings
from infrastructure.config.static import StaticConfigValidationError, clear_static_config_cache


@dataclass(frozen=True, slots=True)
class _CallCounter:
    """记录依赖创建函数是否被调用。"""

    count: int = 0

    def increment(self) -> "_CallCounter":
        return _CallCounter(count=self.count + 1)


def _read_static_file(filename: str) -> str:
    """读取默认静态配置文本。"""
    return resource_files(static_files_package).joinpath(filename).read_text(encoding="utf-8")


def _replace_once(source: str, old: str, new: str) -> str:
    """执行一次文本替换。"""
    if old not in source:
        raise AssertionError(f"未找到待替换片段: {old}")
    return source.replace(old, new, 1)


def test_build_client_stops_before_database_setup_when_static_config_is_invalid(monkeypatch) -> None:
    """启动入口应在静态配置失败时提前中止。"""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "123456789012345678")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    get_settings.cache_clear()
    clear_static_config_cache()

    broken_lineages = _replace_once(
        _read_static_file("skill_lineages.toml"),
        'lineage_id = "seven_kill_sword"',
        'lineage_id = "missing_template_lineage"',
    )

    def resource_provider(filename: str) -> str:
        if filename == "skill_lineages.toml":
            return broken_lineages
        return _read_static_file(filename)

    engine_calls = {"count": 0}
    session_calls = {"count": 0}
    health_calls = {"count": 0}
    client_calls = {"count": 0}

    def fail_on_engine(database_url: str):
        engine_calls["count"] += 1
        raise AssertionError(f"不应进入数据库引擎创建: {database_url}")

    def fail_on_session_factory(database_url: str):
        session_calls["count"] += 1
        raise AssertionError(f"不应进入会话工厂创建: {database_url}")

    def fail_on_health_service(engine):
        health_calls["count"] += 1
        raise AssertionError(f"不应进入数据库健康检查服务创建: {engine}")

    def fail_on_client(*args, **kwargs):
        client_calls["count"] += 1
        raise AssertionError("不应进入 Discord 客户端创建")

    monkeypatch.setattr("bot.bootstrap.create_engine_from_url", fail_on_engine)
    monkeypatch.setattr("bot.bootstrap.create_session_factory", fail_on_session_factory)
    monkeypatch.setattr("bot.bootstrap.DatabaseHealthService", fail_on_health_service)
    monkeypatch.setattr("bot.bootstrap.XianBotClient", fail_on_client)

    with pytest.raises(StaticConfigValidationError) as exc_info:
        build_client(static_config_resource_provider=resource_provider)

    assert any(issue.filename == "skill_drops.toml" for issue in exc_info.value.issues)
    assert engine_calls["count"] == 0
    assert session_calls["count"] == 0
    assert health_calls["count"] == 0
    assert client_calls["count"] == 0
