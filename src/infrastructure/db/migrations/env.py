"""Alembic 迁移环境。"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from infrastructure.config.constants import DEFAULT_DATABASE_URL
from infrastructure.db.base import Base
from infrastructure.db import models  # noqa: F401
from infrastructure.db.session import ensure_database_path

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


target_metadata = Base.metadata


def get_database_url() -> str:
    """优先从环境变量读取数据库地址。"""
    cli_value = context.get_x_argument(as_dictionary=True).get("database_url")
    env_value = os.getenv("DATABASE_URL")
    return cli_value or env_value or DEFAULT_DATABASE_URL


def run_migrations_offline() -> None:
    """离线模式执行迁移。"""
    url = get_database_url()
    ensure_database_path(url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式执行迁移。"""
    configuration = config.get_section(config.config_ini_section, {})
    database_url = get_database_url()
    ensure_database_path(database_url)
    configuration["sqlalchemy.url"] = database_url

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
