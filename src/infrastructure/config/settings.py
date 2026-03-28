"""应用配置定义。"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from infrastructure.config.constants import DEFAULT_DATABASE_URL, DEFAULT_LOG_LEVEL


class Settings(BaseSettings):
    """应用运行配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        env_ignore_empty=True,
    )

    discord_bot_token: str = Field(alias="DISCORD_BOT_TOKEN")
    discord_application_id: int = Field(alias="DISCORD_APPLICATION_ID")
    database_url: str = Field(
        default=DEFAULT_DATABASE_URL,
        alias="DATABASE_URL",
    )
    discord_guild_id: int | None = Field(default=None, alias="DISCORD_GUILD_ID")
    log_level: str = Field(default=DEFAULT_LOG_LEVEL, alias="LOG_LEVEL")
    ai_naming_api_key: str | None = Field(default=None, alias="AI_NAMING_API_KEY")
    ai_naming_api_url: str | None = Field(default=None, alias="AI_NAMING_API_URL")
    ai_naming_model: str | None = Field(default=None, alias="AI_NAMING_MODEL")

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        """统一日志级别大小写。"""
        return value.upper()

    @field_validator("ai_naming_api_key", "ai_naming_api_url", "ai_naming_model")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        """统一可选文本配置的空白值。"""
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @property
    def ai_naming_enabled(self) -> bool:
        """返回当前是否具备完整 AI 命名提供方配置。"""
        return all((self.ai_naming_api_key, self.ai_naming_api_url, self.ai_naming_model))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回缓存后的应用配置。"""
    return Settings()
