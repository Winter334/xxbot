"""功法背包与装配应用服务。"""

from __future__ import annotations

from dataclasses import dataclass

from application.character.skill_runtime_support import (
    CharacterSkillLoadoutSnapshot,
    SkillInventoryItemSnapshot,
    SkillRuntimeSupport,
)
from application.ranking.score_service import CharacterScoreService, CharacterScoreServiceError
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.repositories import CharacterRepository, SkillRepository, SqlAlchemySkillRepository

_MAIN_SLOT_ID = "main"
_SLOT_FIELD_BY_SLOT_ID: dict[str, str] = {
    _MAIN_SLOT_ID: "main_skill_id",
    "guard": "guard_skill_id",
    "movement": "movement_skill_id",
    "spirit": "spirit_skill_id",
}


@dataclass(frozen=True, slots=True)
class SkillPathSwitchApplicationResult:
    """旧路线切换入口的兼容返回结果。"""

    character_id: int
    previous_main_path_id: str | None
    previous_behavior_template_id: str | None
    main_axis_id: str
    main_path_id: str
    behavior_template_id: str
    config_version: str | None


@dataclass(frozen=True, slots=True)
class SkillSlotEquipApplicationResult:
    """单个槽位装配完成后的返回结果。"""

    character_id: int
    slot_id: str
    previous_skill_item_id: int | None
    equipped_skill_item_id: int
    main_path_id: str
    behavior_template_id: str
    config_version: str | None
    loadout: CharacterSkillLoadoutSnapshot


class SkillLoadoutServiceError(RuntimeError):
    """功法装配服务基础异常。"""


class SkillLoadoutCharacterNotFoundError(SkillLoadoutServiceError):
    """角色不存在。"""


class SkillLoadoutStateError(SkillLoadoutServiceError):
    """角色功法配置状态不完整。"""


class SkillPathNotFoundError(SkillLoadoutServiceError):
    """功法流派未配置。"""


class SkillLoadoutItemNotFoundError(SkillLoadoutServiceError):
    """目标功法实例不存在。"""


class SkillLoadoutOwnershipError(SkillLoadoutServiceError):
    """功法实例不属于当前角色。"""


class SkillLoadoutSlotValidationError(SkillLoadoutServiceError):
    """功法实例与目标槽位不匹配。"""


class SkillLoadoutService:
    """负责角色功法背包读取与实例装配。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        skill_repository: SkillRepository | None = None,
        score_service: CharacterScoreService | None = None,
        static_config: StaticGameConfig | None = None,
        skill_runtime_support: SkillRuntimeSupport | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._skill_repository = skill_repository or self._build_fallback_skill_repository(character_repository)
        self._score_service = score_service
        self._static_config = static_config or get_static_config()
        self._skill_runtime_support = skill_runtime_support or SkillRuntimeSupport(
            character_repository=character_repository,
            skill_repository=self._skill_repository,
            static_config=self._static_config,
        )
        self._path_by_id = {
            path.path_id: path
            for path in self._static_config.skill_paths.paths
        }
        self._main_lineages_by_path_id: dict[str, list[str]] = {}
        for lineage in self._static_config.skill_lineages.lineages:
            if lineage.skill_type != "main":
                continue
            self._main_lineages_by_path_id.setdefault(lineage.path_id, []).append(lineage.lineage_id)

    def list_owned_skills(self, *, character_id: int) -> tuple[SkillInventoryItemSnapshot, ...]:
        """返回角色当前拥有的全部功法实例。"""
        self._require_character(character_id)
        return self._skill_runtime_support.list_skill_item_snapshots(character_id=character_id)

    def get_current_loadout(self, *, character_id: int) -> CharacterSkillLoadoutSnapshot:
        """返回角色当前完整装配。"""
        self._require_character(character_id)
        return self._skill_runtime_support.get_loadout_snapshot(character_id=character_id)

    def equip_skill_instance(
        self,
        *,
        character_id: int,
        skill_item_id: int,
    ) -> SkillSlotEquipApplicationResult:
        """按功法实例直接完成对应槽位装配。"""
        self._require_character(character_id)
        item = self._require_owned_item(character_id=character_id, skill_item_id=skill_item_id)
        return self.equip_skill_item(
            character_id=character_id,
            slot_id=self._resolve_target_slot_id_for_item(item),
            skill_item_id=item.id,
        )

    def equip_skill_item(
        self,
        *,
        character_id: int,
        slot_id: str,
        skill_item_id: int,
    ) -> SkillSlotEquipApplicationResult:
        """把指定功法实例装入目标槽位。"""
        self._require_character(character_id)
        normalized_slot_id = slot_id.strip()
        loadout_field = _SLOT_FIELD_BY_SLOT_ID.get(normalized_slot_id)
        if loadout_field is None:
            raise SkillLoadoutSlotValidationError(f"未支持的功法槽位：{slot_id}")

        item = self._require_owned_item(character_id=character_id, skill_item_id=skill_item_id)
        self._validate_slot_match(slot_id=normalized_slot_id, item=item)

        state = self._skill_runtime_support.ensure_skill_state(character_id=character_id)
        loadout = state.loadout_model
        previous_skill_item_id = getattr(loadout, loadout_field)
        if previous_skill_item_id == item.id:
            refreshed_state = self._skill_runtime_support.ensure_skill_state(character_id=character_id)
            return SkillSlotEquipApplicationResult(
                character_id=character_id,
                slot_id=normalized_slot_id,
                previous_skill_item_id=previous_skill_item_id,
                equipped_skill_item_id=item.id,
                main_path_id=refreshed_state.loadout_snapshot.main_path_id,
                behavior_template_id=refreshed_state.loadout_snapshot.behavior_template_id,
                config_version=refreshed_state.loadout_snapshot.config_version,
                loadout=refreshed_state.loadout_snapshot,
            )

        setattr(loadout, loadout_field, item.id)
        if normalized_slot_id == _MAIN_SLOT_ID:
            path = self._require_path(item.path_id)
            loadout.main_axis_id = path.axis_id
            loadout.main_path_id = path.path_id
            loadout.behavior_template_id = path.template_id
        loadout.config_version = self._static_config.skill_generation.config_version
        self._skill_repository.save_skill_loadout(loadout)
        refreshed_state = self._skill_runtime_support.ensure_skill_state(character_id=character_id)
        self._refresh_score_if_configured(character_id)
        return SkillSlotEquipApplicationResult(
            character_id=character_id,
            slot_id=normalized_slot_id,
            previous_skill_item_id=previous_skill_item_id,
            equipped_skill_item_id=item.id,
            main_path_id=refreshed_state.loadout_snapshot.main_path_id,
            behavior_template_id=refreshed_state.loadout_snapshot.behavior_template_id,
            config_version=refreshed_state.loadout_snapshot.config_version,
            loadout=refreshed_state.loadout_snapshot,
        )

    def switch_main_path(self, *, character_id: int, main_path_id: str) -> SkillPathSwitchApplicationResult:
        """兼容旧入口：选择或补发指定流派的主修功法实例后完成装配。"""
        self._require_character(character_id)
        normalized_main_path_id = main_path_id.strip()
        self._require_path(normalized_main_path_id)
        state = self._skill_runtime_support.ensure_skill_state(character_id=character_id)
        previous_main_path_id = state.loadout_snapshot.main_path_id
        previous_behavior_template_id = state.loadout_snapshot.behavior_template_id
        target_item = self._find_best_owned_main_item(
            character_id=character_id,
            main_path_id=normalized_main_path_id,
        )
        if target_item is None:
            target_item = self._grant_legacy_main_item(
                character_id=character_id,
                main_path_id=normalized_main_path_id,
            )
        equip_result = self.equip_skill_item(
            character_id=character_id,
            slot_id=_MAIN_SLOT_ID,
            skill_item_id=target_item.id,
        )
        return SkillPathSwitchApplicationResult(
            character_id=character_id,
            previous_main_path_id=previous_main_path_id,
            previous_behavior_template_id=previous_behavior_template_id,
            main_axis_id=equip_result.loadout.main_axis_id,
            main_path_id=equip_result.loadout.main_path_id,
            behavior_template_id=equip_result.loadout.behavior_template_id,
            config_version=equip_result.config_version,
        )

    def _find_best_owned_main_item(self, *, character_id: int, main_path_id: str):
        candidates = [
            item
            for item in self._skill_repository.list_skill_items_by_character_id(character_id)
            if item.skill_type == "main" and item.path_id == main_path_id
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item.rank_order, -self._resolve_quality_order(item.quality_id), item.id))
        return candidates[0]

    def _grant_legacy_main_item(self, *, character_id: int, main_path_id: str):
        lineage_ids = self._main_lineages_by_path_id.get(main_path_id, [])
        if not lineage_ids:
            raise SkillPathNotFoundError(f"指定流派缺少主修功法谱系：{main_path_id}")
        return self._skill_runtime_support.generate_skill_item(
            character_id=character_id,
            lineage_id=lineage_ids[0],
            rank_id="mortal",
            quality_id="ordinary",
            source_type="legacy_path_switch",
            source_record_id=f"path:{main_path_id}",
            seed=0,
        )

    @staticmethod
    def _build_fallback_skill_repository(character_repository: CharacterRepository) -> SkillRepository:
        session = getattr(character_repository, "_session", None)
        if session is None:
            raise ValueError("SkillLoadoutService 缺少 skill_repository，且无法从 character_repository 推导会话")
        return SqlAlchemySkillRepository(session)

    def _validate_slot_match(self, *, slot_id: str, item) -> None:
        if slot_id == _MAIN_SLOT_ID:
            if item.skill_type != "main":
                raise SkillLoadoutSlotValidationError(f"主修槽位不能装配非主修功法：{item.id}")
            return
        if item.skill_type != "auxiliary":
            raise SkillLoadoutSlotValidationError(f"辅助槽位不能装配主修功法：{item.id}")
        if item.auxiliary_slot_id != slot_id:
            raise SkillLoadoutSlotValidationError(
                f"功法实例 {item.id} 属于 {item.auxiliary_slot_id} 槽位，不能装入 {slot_id}"
            )

    def _require_character(self, character_id: int) -> None:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise SkillLoadoutCharacterNotFoundError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise SkillLoadoutStateError(f"角色缺少成长状态：{character_id}")

    def _require_path(self, path_id: str):
        path = self._path_by_id.get(path_id)
        if path is None:
            raise SkillPathNotFoundError(f"未配置的功法流派：{path_id}")
        return path

    def _require_owned_item(self, *, character_id: int, skill_item_id: int):
        item = self._skill_repository.get_skill_item(skill_item_id)
        if item is None:
            raise SkillLoadoutItemNotFoundError(f"功法实例不存在：{skill_item_id}")
        if item.character_id != character_id:
            raise SkillLoadoutOwnershipError(f"功法实例不属于当前角色：{skill_item_id}")
        return item

    @staticmethod
    def _resolve_target_slot_id_for_item(item) -> str:
        if item.skill_type == "main":
            return _MAIN_SLOT_ID
        auxiliary_slot_id = str(item.auxiliary_slot_id or "").strip()
        if not auxiliary_slot_id:
            raise SkillLoadoutSlotValidationError(f"辅助功法缺少槽位配置：{item.id}")
        return auxiliary_slot_id

    def _resolve_quality_order(self, quality_id: str) -> int:
        quality = self._static_config.skill_generation.get_quality(quality_id)
        return 0 if quality is None else quality.order

    def _refresh_score_if_configured(self, character_id: int) -> None:
        if self._score_service is None:
            return
        try:
            self._score_service.refresh_character_score(character_id=character_id)
        except CharacterScoreServiceError as exc:
            raise SkillLoadoutStateError(str(exc)) from exc


__all__ = [
    "CharacterSkillLoadoutSnapshot",
    "SkillInventoryItemSnapshot",
    "SkillLoadoutCharacterNotFoundError",
    "SkillLoadoutItemNotFoundError",
    "SkillLoadoutOwnershipError",
    "SkillLoadoutService",
    "SkillLoadoutServiceError",
    "SkillLoadoutSlotValidationError",
    "SkillLoadoutStateError",
    "SkillPathNotFoundError",
    "SkillPathSwitchApplicationResult",
    "SkillSlotEquipApplicationResult",
]
