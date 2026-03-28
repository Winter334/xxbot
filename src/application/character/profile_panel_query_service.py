"""角色资料与功法展示只读查询。"""

from __future__ import annotations

from dataclasses import dataclass

from application.character.skill_loadout_service import SkillLoadoutService
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository


@dataclass(frozen=True, slots=True)
class SkillPanelSkillSlotSnapshot:
    """单个功法槽位或背包实例的展示快照。"""

    slot_id: str
    slot_name: str
    item_id: int
    lineage_id: str
    skill_name: str
    path_id: str
    path_name: str
    rank_id: str
    rank_name: str
    quality_id: str
    quality_name: str
    skill_type: str
    total_budget: int
    resolved_patch_ids: tuple[str, ...]
    equipped_slot_id: str | None = None


@dataclass(frozen=True, slots=True)
class SkillPanelSnapshot:
    """功法展示页所需的只读快照。"""

    character_id: int
    character_name: str
    realm_id: str
    stage_id: str
    main_axis_id: str
    main_axis_name: str
    axis_focus_summary: str
    main_path_id: str
    main_path_name: str
    preferred_scene: str
    combat_identity: str
    behavior_template_id: str
    behavior_template_name: str
    resource_policy: str
    template_tags: tuple[str, ...]
    main_skill: SkillPanelSkillSlotSnapshot
    auxiliary_skills: tuple[SkillPanelSkillSlotSnapshot, ...]
    config_version: str | None
    owned_skills: tuple[SkillPanelSkillSlotSnapshot, ...] = ()


class ProfilePanelQueryServiceError(RuntimeError):
    """角色资料查询基础异常。"""


class ProfilePanelCharacterNotFoundError(ProfilePanelQueryServiceError):
    """角色不存在。"""


class ProfilePanelStateError(ProfilePanelQueryServiceError):
    """角色资料状态不完整。"""


class ProfilePanelQueryService:
    """聚合角色资料与功法展示所需的只读数据。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        skill_loadout_service: SkillLoadoutService,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._skill_loadout_service = skill_loadout_service
        self._static_config = static_config or get_static_config()
        self._axis_by_id = {axis.axis_id: axis for axis in self._static_config.skill_paths.axes}
        self._path_by_id = {path.path_id: path for path in self._static_config.skill_paths.paths}
        self._template_by_id = {
            template.template_id: template for template in self._static_config.battle_templates.templates
        }
        self._slot_name_by_id = {
            "main": "主修",
            "guard": "护体",
            "movement": "身法",
            "spirit": "灵技",
        }

    def get_skill_snapshot(self, *, character_id: int) -> SkillPanelSnapshot:
        """读取角色当前功法展示快照。"""
        aggregate = self._require_aggregate(character_id)
        loadout = self._skill_loadout_service.get_current_loadout(character_id=character_id)
        owned_skills = self._skill_loadout_service.list_owned_skills(character_id=character_id)
        path = self._path_by_id.get(loadout.main_path_id)
        if path is None:
            raise ProfilePanelStateError(f"未配置的功法流派：{loadout.main_path_id}")

        axis = self._axis_by_id.get(loadout.main_axis_id)
        if axis is None:
            raise ProfilePanelStateError(f"未配置的功法主轴：{loadout.main_axis_id}")

        template = self._template_by_id.get(loadout.behavior_template_id)
        if template is None:
            raise ProfilePanelStateError(f"未配置的行为模板：{loadout.behavior_template_id}")

        progress = aggregate.progress
        assert progress is not None
        return SkillPanelSnapshot(
            character_id=aggregate.character.id,
            character_name=aggregate.character.name,
            realm_id=progress.realm_id,
            stage_id=progress.stage_id,
            main_axis_id=axis.axis_id,
            main_axis_name=axis.name,
            axis_focus_summary=axis.focus_summary,
            main_path_id=path.path_id,
            main_path_name=loadout.main_skill.skill_name,
            preferred_scene=path.preferred_scene,
            combat_identity=path.combat_identity,
            behavior_template_id=template.template_id,
            behavior_template_name=template.name,
            resource_policy=template.resource_policy,
            template_tags=template.template_tags,
            main_skill=self._build_skill_slot_snapshot(slot_id="main", skill_item=loadout.main_skill),
            auxiliary_skills=(
                self._build_skill_slot_snapshot(slot_id="guard", skill_item=loadout.guard_skill),
                self._build_skill_slot_snapshot(slot_id="movement", skill_item=loadout.movement_skill),
                self._build_skill_slot_snapshot(slot_id="spirit", skill_item=loadout.spirit_skill),
            ),
            config_version=loadout.config_version,
            owned_skills=tuple(
                self._build_skill_slot_snapshot(
                    slot_id=self._resolve_skill_slot_id(skill_item=skill_item),
                    skill_item=skill_item,
                )
                for skill_item in owned_skills
            ),
        )

    def _build_skill_slot_snapshot(self, *, slot_id: str, skill_item) -> SkillPanelSkillSlotSnapshot:
        path_name = self._path_by_id.get(skill_item.path_id)
        return SkillPanelSkillSlotSnapshot(
            slot_id=slot_id,
            slot_name=self._slot_name_by_id.get(slot_id, slot_id),
            item_id=skill_item.item_id,
            lineage_id=skill_item.lineage_id,
            skill_name=skill_item.skill_name,
            path_id=skill_item.path_id,
            path_name=skill_item.path_id if path_name is None else path_name.name,
            rank_id=skill_item.rank_id,
            rank_name=skill_item.rank_name,
            quality_id=skill_item.quality_id,
            quality_name=skill_item.quality_name,
            skill_type=skill_item.skill_type,
            total_budget=skill_item.total_budget,
            resolved_patch_ids=skill_item.resolved_patch_ids,
            equipped_slot_id=skill_item.equipped_slot_id,
        )

    def _resolve_skill_slot_id(self, *, skill_item) -> str:
        if skill_item.skill_type == "main":
            return "main"
        auxiliary_slot_id = str(skill_item.auxiliary_slot_id or "").strip()
        if auxiliary_slot_id:
            return auxiliary_slot_id
        raise ProfilePanelStateError(f"辅助功法缺少槽位配置：{skill_item.item_id}")

    def _require_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise ProfilePanelCharacterNotFoundError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise ProfilePanelStateError(f"角色缺少成长状态：{character_id}")
        return aggregate


__all__ = [
    "ProfilePanelCharacterNotFoundError",
    "ProfilePanelQueryService",
    "ProfilePanelQueryServiceError",
    "ProfilePanelStateError",
    "SkillPanelSkillSlotSnapshot",
    "SkillPanelSnapshot",
]
