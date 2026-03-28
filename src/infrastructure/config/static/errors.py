"""静态配置统一异常与错误收集器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class StaticConfigIssue:
    """描述单个静态配置错误。"""

    filename: str
    config_path: str
    identifier: str
    reason: str

    def format_message(self) -> str:
        """格式化单条错误信息。"""
        return (
            f"文件={self.filename} | 路径={self.config_path} | "
            f"标识={self.identifier} | 原因={self.reason}"
        )


class StaticConfigValidationError(Exception):
    """静态配置加载或校验失败。"""

    def __init__(self, issues: Iterable[StaticConfigIssue]) -> None:
        collected = tuple(issues)
        if not collected:
            raise ValueError("静态配置异常至少需要一条错误信息")

        self.issues = collected
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        details = [
            f"{index}. {issue.format_message()}"
            for index, issue in enumerate(self.issues, start=1)
        ]
        return "静态配置校验失败：\n" + "\n".join(details)


class StaticConfigIssueCollector:
    """收集静态配置错误，便于一次性汇总抛出。"""

    def __init__(self) -> None:
        self._issues: list[StaticConfigIssue] = []

    @property
    def issues(self) -> tuple[StaticConfigIssue, ...]:
        """返回当前已收集的错误列表。"""
        return tuple(self._issues)

    @property
    def has_issues(self) -> bool:
        """标记当前是否已经存在错误。"""
        return bool(self._issues)

    def add(
        self,
        *,
        filename: str,
        config_path: str,
        identifier: str,
        reason: str,
    ) -> None:
        """追加一条结构化错误。"""
        self._issues.append(
            StaticConfigIssue(
                filename=filename,
                config_path=config_path,
                identifier=identifier,
                reason=reason,
            )
        )

    def extend(self, issues: Iterable[StaticConfigIssue]) -> None:
        """批量追加错误。"""
        self._issues.extend(issues)

    def raise_if_any(self) -> None:
        """如果已收集错误，则统一抛出。"""
        if self._issues:
            raise StaticConfigValidationError(self._issues)
