"""配置与数据库基础链路测试。"""

from __future__ import annotations

from infrastructure.config.constants import DEFAULT_DATABASE_URL
from infrastructure.config.settings import Settings, get_settings
from infrastructure.db.health import DatabaseHealthService
from infrastructure.db.session import create_engine_from_url


def test_settings_use_default_sqlite_database(monkeypatch) -> None:
    """未提供数据库地址时，应回退到默认 SQLite。"""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "123456789012345678")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AI_NAMING_API_KEY", raising=False)
    monkeypatch.delenv("AI_NAMING_API_URL", raising=False)
    monkeypatch.delenv("AI_NAMING_MODEL", raising=False)
    get_settings.cache_clear()

    settings = Settings()

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.ai_naming_enabled is False
    assert settings.ai_naming_api_key is None
    assert settings.ai_naming_api_url is None
    assert settings.ai_naming_model is None


def test_settings_enable_ai_naming_when_all_env_present(monkeypatch) -> None:
    """AI 命名三项环境变量同时存在时应判定为启用。"""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "123456789012345678")
    monkeypatch.setenv("AI_NAMING_API_KEY", "test-key")
    monkeypatch.setenv("AI_NAMING_API_URL", "https://example.com/naming")
    monkeypatch.setenv("AI_NAMING_MODEL", "gpt-test")
    get_settings.cache_clear()

    settings = Settings()

    assert settings.ai_naming_enabled is True
    assert settings.ai_naming_api_key == "test-key"
    assert settings.ai_naming_api_url == "https://example.com/naming"
    assert settings.ai_naming_model == "gpt-test"



def test_database_health_probe_with_sqlite(tmp_path) -> None:
    """SQLite 引擎应能通过基础连通性检查。"""
    database_path = tmp_path / "test.db"
    engine = create_engine_from_url(f"sqlite+pysqlite:///{database_path.as_posix()}")

    DatabaseHealthService(engine).probe()
