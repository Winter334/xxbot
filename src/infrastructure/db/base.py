"""SQLAlchemy 基础对象。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""
