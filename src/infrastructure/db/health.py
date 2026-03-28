"""数据库健康检查入口。"""

from sqlalchemy.engine import Engine

from infrastructure.db.session import check_database_connection


class DatabaseHealthService:
    """数据库健康检查服务。"""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def probe(self) -> None:
        """检查数据库连通性。"""
        check_database_connection(self._engine)
