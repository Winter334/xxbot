"""境界推进配置模型。"""

from __future__ import annotations

from collections import Counter

from infrastructure.config.static.errors import StaticConfigIssueCollector
from infrastructure.config.static.models.common import (
    LAUNCH_REALM_IDS,
    LAUNCH_STAGE_IDS,
    OrderedConfigItem,
    PositiveDecimal,
    StableId,
    VersionedSectionConfig,
)


class RealmStageDefinition(OrderedConfigItem):
    """统一小阶段定义。"""

    stage_id: StableId
    multiplier: PositiveDecimal


class RealmDefinition(OrderedConfigItem):
    """单个大境界定义。"""

    realm_id: StableId
    world_segment: StableId
    stage_ids: tuple[StableId, ...]


class RealmProgressionConfig(VersionedSectionConfig):
    """大境界与小阶段配置。"""

    stages: tuple[RealmStageDefinition, ...]
    realms: tuple[RealmDefinition, ...]

    def collect_issues(self, *, filename: str, collector: StaticConfigIssueCollector) -> None:
        """收集当前配置节的结构与边界错误。"""
        self._collect_stage_issues(filename=filename, collector=collector)
        self._collect_realm_issues(filename=filename, collector=collector)

    def _collect_stage_issues(
        self,
        *,
        filename: str,
        collector: StaticConfigIssueCollector,
    ) -> None:
        stage_ids = tuple(stage.stage_id for stage in sorted(self.stages, key=lambda item: item.order))
        order_counter = Counter(stage.order for stage in self.stages)
        id_counter = Counter(stage.stage_id for stage in self.stages)

        if len(self.stages) != 4:
            collector.add(
                filename=filename,
                config_path="stages",
                identifier="stage_count",
                reason="首发小阶段必须固定为四个",
            )

        if stage_ids != LAUNCH_STAGE_IDS:
            collector.add(
                filename=filename,
                config_path="stages",
                identifier="stage_sequence",
                reason="小阶段顺序必须固定为 early、middle、late、perfect",
            )

        for stage in self.stages:
            if order_counter[stage.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="stages[].order",
                    identifier=stage.stage_id,
                    reason=f"小阶段顺序值 {stage.order} 重复",
                )
            if id_counter[stage.stage_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="stages[].stage_id",
                    identifier=stage.stage_id,
                    reason="小阶段标识重复",
                )

    def _collect_realm_issues(
        self,
        *,
        filename: str,
        collector: StaticConfigIssueCollector,
    ) -> None:
        ordered_realms = tuple(sorted(self.realms, key=lambda item: item.order))
        realm_ids = tuple(realm.realm_id for realm in ordered_realms)
        order_counter = Counter(realm.order for realm in self.realms)
        id_counter = Counter(realm.realm_id for realm in self.realms)
        known_stage_ids = {stage.stage_id for stage in self.stages}

        if realm_ids != LAUNCH_REALM_IDS:
            collector.add(
                filename=filename,
                config_path="realms",
                identifier="realm_sequence",
                reason="首发开放境界必须严格覆盖凡人到渡劫，且顺序不可变",
            )

        for realm in self.realms:
            if order_counter[realm.order] > 1:
                collector.add(
                    filename=filename,
                    config_path="realms[].order",
                    identifier=realm.realm_id,
                    reason=f"大境界顺序值 {realm.order} 重复",
                )
            if id_counter[realm.realm_id] > 1:
                collector.add(
                    filename=filename,
                    config_path="realms[].realm_id",
                    identifier=realm.realm_id,
                    reason="大境界标识重复",
                )
            if len(realm.stage_ids) != 4:
                collector.add(
                    filename=filename,
                    config_path="realms[].stage_ids",
                    identifier=realm.realm_id,
                    reason="每个大境界必须固定声明四个小阶段",
                )
                continue
            if tuple(realm.stage_ids) != LAUNCH_STAGE_IDS:
                collector.add(
                    filename=filename,
                    config_path="realms[].stage_ids",
                    identifier=realm.realm_id,
                    reason="大境界的小阶段顺序必须固定为 early、middle、late、perfect",
                )
            for stage_id in realm.stage_ids:
                if stage_id not in known_stage_ids:
                    collector.add(
                        filename=filename,
                        config_path="realms[].stage_ids",
                        identifier=realm.realm_id,
                        reason=f"引用了未定义的小阶段 {stage_id}",
                    )
