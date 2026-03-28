"""角色主面板查询适配服务。"""

from __future__ import annotations

from dataclasses import dataclass

from application.character.current_attribute_service import CurrentAttributeService
from application.character.growth_service import CharacterGrowthService
from application.character.progression_service import CharacterProgressionService
from application.ranking.score_service import CharacterScoreService
from domain.pvp import PvpRewardDisplayType, PvpRuleService
from domain.ranking import LeaderboardBoardType
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, PlayerRepository, SnapshotRepository


@dataclass(frozen=True, slots=True)
class CharacterPanelSkillDisplay:
    """角色主面板展示用功法摘要。"""

    item_id: int
    skill_name: str
    path_id: str
    path_name: str
    rank_name: str
    quality_name: str
    slot_id: str
    skill_type: str


@dataclass(frozen=True, slots=True)
class CharacterPanelBattleProjection:
    """角色属性在主面板中的稳定投影。"""

    behavior_template_id: str
    max_hp: int
    current_hp: int
    max_resource: int
    current_resource: int
    attack_power: int
    guard_power: int
    speed: int
    crit_rate_permille: int
    crit_damage_bonus_permille: int
    hit_rate_permille: int
    dodge_rate_permille: int
    control_bonus_permille: int
    control_resist_permille: int
    healing_power_permille: int
    shield_power_permille: int
    damage_bonus_permille: int
    damage_reduction_permille: int
    counter_rate_permille: int


@dataclass(frozen=True, slots=True)
class CharacterPanelOverview:
    """角色主面板的查询结果。"""

    discord_user_id: str
    player_display_name: str
    character_id: int
    character_name: str
    character_title: str | None
    badge_name: str | None
    realm_id: str
    realm_name: str
    stage_id: str
    stage_name: str
    main_path_name: str | None
    main_skill: CharacterPanelSkillDisplay
    auxiliary_skills: tuple[CharacterPanelSkillDisplay, ...]
    public_power_score: int
    battle_projection: CharacterPanelBattleProjection


class CharacterPanelQueryServiceError(RuntimeError):
    """角色主面板查询服务基础异常。"""


class DiscordCharacterBindingNotFoundError(CharacterPanelQueryServiceError):
    """Discord 账号尚未绑定角色。"""


class CharacterPanelStateError(CharacterPanelQueryServiceError):
    """角色主面板依赖的状态不完整。"""


class CharacterPanelQueryService:
    """聚合角色档案、评分与稳定属性投影，供 Discord 主面板读取。"""

    def __init__(
        self,
        *,
        player_repository: PlayerRepository,
        character_repository: CharacterRepository,
        snapshot_repository: SnapshotRepository,
        growth_service: CharacterGrowthService,
        progression_service: CharacterProgressionService,
        score_service: CharacterScoreService,
        current_attribute_service: CurrentAttributeService,
        static_config: StaticGameConfig | None = None,
        pvp_rule_service: PvpRuleService | None = None,
    ) -> None:
        self._player_repository = player_repository
        self._character_repository = character_repository
        self._snapshot_repository = snapshot_repository
        self._growth_service = growth_service
        self._progression_service = progression_service
        self._score_service = score_service
        self._current_attribute_service = current_attribute_service
        self._static_config = static_config or get_static_config()
        self._pvp_rule_service = pvp_rule_service or PvpRuleService(self._static_config)
        self._stage_name_by_id = {
            stage.stage_id: stage.name for stage in self._static_config.realm_progression.stages
        }
        self._path_name_by_id = {
            path.path_id: path.name for path in self._static_config.skill_paths.paths
        }

    def get_overview_by_discord_user_id(self, *, discord_user_id: str) -> CharacterPanelOverview:
        """按 Discord 用户标识读取角色主面板。"""
        player = self._player_repository.get_by_discord_user_id(discord_user_id)
        if player is None:
            raise DiscordCharacterBindingNotFoundError(f"Discord 账号尚未绑定角色：{discord_user_id}")
        character = self._character_repository.get_by_player_id(player.id)
        if character is None:
            raise DiscordCharacterBindingNotFoundError(f"Discord 账号尚未创建角色：{discord_user_id}")
        return self.get_overview(character_id=character.id)

    def get_overview(self, *, character_id: int) -> CharacterPanelOverview:
        """按角色标识读取角色主面板。"""
        growth_snapshot = self._growth_service.get_snapshot(character_id=character_id)
        breakthrough_precheck = self._progression_service.get_breakthrough_precheck(character_id=character_id)
        aggregate = self._require_aggregate(character_id)
        current_attributes = self._current_attribute_service.get_neutral_view(character_id=character_id)
        projection = self._build_battle_projection(current_attributes=current_attributes)
        skill_loadout = current_attributes.skill_loadout

        return CharacterPanelOverview(
            discord_user_id=growth_snapshot.discord_user_id,
            player_display_name=growth_snapshot.player_display_name,
            character_id=growth_snapshot.character_id,
            character_name=growth_snapshot.character_name,
            character_title=growth_snapshot.character_title,
            badge_name=self._resolve_badge_name(character_id=character_id),
            realm_id=growth_snapshot.realm_id,
            realm_name=breakthrough_precheck.current_realm_name,
            stage_id=growth_snapshot.stage_id,
            stage_name=self._stage_name_by_id.get(growth_snapshot.stage_id, growth_snapshot.stage_id),
            main_path_name=skill_loadout.main_skill.skill_name,
            main_skill=self._build_skill_display(
                slot_id="main",
                path_name=self._path_name_by_id.get(skill_loadout.main_skill.path_id, skill_loadout.main_skill.path_id),
                skill_item=skill_loadout.main_skill,
            ),
            auxiliary_skills=(
                self._build_skill_display(
                    slot_id="guard",
                    path_name=self._path_name_by_id.get(skill_loadout.guard_skill.path_id, skill_loadout.guard_skill.path_id),
                    skill_item=skill_loadout.guard_skill,
                ),
                self._build_skill_display(
                    slot_id="movement",
                    path_name=self._path_name_by_id.get(skill_loadout.movement_skill.path_id, skill_loadout.movement_skill.path_id),
                    skill_item=skill_loadout.movement_skill,
                ),
                self._build_skill_display(
                    slot_id="spirit",
                    path_name=self._path_name_by_id.get(skill_loadout.spirit_skill.path_id, skill_loadout.spirit_skill.path_id),
                    skill_item=skill_loadout.spirit_skill,
                ),
            ),
            public_power_score=aggregate.character.public_power_score,
            battle_projection=projection,
        )

    def _require_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise DiscordCharacterBindingNotFoundError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise CharacterPanelStateError(f"角色缺少成长状态：{character_id}")
        return aggregate

    @staticmethod
    def _build_battle_projection(*, current_attributes) -> CharacterPanelBattleProjection:
        stats_payload = current_attributes.to_stats_payload()
        return CharacterPanelBattleProjection(
            behavior_template_id=current_attributes.behavior_template_id,
            max_hp=current_attributes.max_hp,
            current_hp=int(stats_payload["current_hp"]),
            max_resource=int(stats_payload["max_resource"]),
            current_resource=int(stats_payload["current_resource"]),
            attack_power=current_attributes.attack_power,
            guard_power=current_attributes.guard_power,
            speed=current_attributes.speed,
            crit_rate_permille=current_attributes.crit_rate_permille,
            crit_damage_bonus_permille=current_attributes.crit_damage_bonus_permille,
            hit_rate_permille=current_attributes.hit_rate_permille,
            dodge_rate_permille=current_attributes.dodge_rate_permille,
            control_bonus_permille=current_attributes.control_bonus_permille,
            control_resist_permille=current_attributes.control_resist_permille,
            healing_power_permille=current_attributes.healing_power_permille,
            shield_power_permille=current_attributes.shield_power_permille,
            damage_bonus_permille=current_attributes.damage_bonus_permille,
            damage_reduction_permille=current_attributes.damage_reduction_permille,
            counter_rate_permille=current_attributes.counter_rate_permille,
        )

    @staticmethod
    def _build_skill_display(*, slot_id: str, path_name: str, skill_item) -> CharacterPanelSkillDisplay:
        return CharacterPanelSkillDisplay(
            item_id=skill_item.item_id,
            skill_name=skill_item.skill_name,
            path_id=skill_item.path_id,
            path_name=path_name,
            rank_name=skill_item.rank_name,
            quality_name=skill_item.quality_name,
            slot_id=slot_id,
            skill_type=skill_item.skill_type,
        )

    def _resolve_badge_name(self, *, character_id: int) -> str | None:
        entry = self._snapshot_repository.get_latest_leaderboard_entry(
            LeaderboardBoardType.PVP_CHALLENGE.value,
            character_id,
        )
        if entry is None:
            return None
        reward_preview = self._pvp_rule_service.build_reward_preview(
            rank_position=entry.rank_position,
            honor_coin_on_win=0,
            honor_coin_on_loss=0,
        )
        for reward_item in reward_preview.display_items:
            if reward_item.reward_type is PvpRewardDisplayType.BADGE:
                return reward_item.name
        return None


__all__ = [
    "CharacterPanelBattleProjection",
    "CharacterPanelOverview",
    "CharacterPanelQueryService",
    "CharacterPanelQueryServiceError",
    "CharacterPanelSkillDisplay",
    "CharacterPanelStateError",
    "DiscordCharacterBindingNotFoundError",
]
