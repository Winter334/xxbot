"""数据库引擎与会话管理。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def _is_sqlite(database_url: str) -> bool:
    """判断当前数据库是否为 SQLite。"""
    return database_url.startswith("sqlite")


def ensure_database_path(database_url: str) -> None:
    """确保文件型数据库的目录存在。"""
    if not _is_sqlite(database_url):
        return

    prefix = "sqlite+pysqlite:///"
    if not database_url.startswith(prefix):
        return

    relative_path = database_url.removeprefix(prefix)
    if relative_path.startswith(":memory:"):
        return

    db_path = Path(relative_path)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)


def create_engine_from_url(database_url: str) -> Engine:
    """根据数据库地址创建引擎。"""
    ensure_database_path(database_url)
    connect_args = {"check_same_thread": False} if _is_sqlite(database_url) else {}
    return create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)


def create_session_factory(database_url: str) -> sessionmaker[Session]:
    """创建会话工厂。"""
    engine = create_engine_from_url(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """提供事务型会话上下文。"""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database_connection(engine: Engine) -> None:
    """执行数据库连通性检查。"""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
