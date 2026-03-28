"""日志初始化。"""

from __future__ import annotations

import logging
from logging.config import dictConfig


def configure_logging(log_level: str) -> None:
    """配置应用日志。"""
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                }
            },
            "root": {
                "level": log_level,
                "handlers": ["console"],
            },
        }
    )
    logging.getLogger(__name__).debug("日志配置完成")
