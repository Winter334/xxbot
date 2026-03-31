"""BOT 启动编排。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from importlib.resources.abc import Traversable

from application.battle import AutoBattleService
from application.breakthrough import BreakthroughPanelService, BreakthroughRewardService, BreakthroughTrialService
from application.character import CharacterGrowthService, CharacterProgressionService, RetreatService, SkillLoadoutService
from application.character.cultivation_panel_service import CultivationPanelService
from application.character.current_attribute_service import CurrentAttributeService
from application.character.skill_drop_service import SkillDropService
from application.character.panel_query_service import CharacterPanelQueryService
from application.character.profile_panel_query_service import ProfilePanelQueryService
from application.dungeon import EndlessDungeonService
from application.dungeon.endless_panel_service import EndlessPanelQueryService
from application.equipment.backpack_query_service import BackpackPanelQueryService
from application.equipment.equipment_service import EquipmentService
from application.equipment.forge_query_service import ForgePanelQueryService
from application.equipment.panel_query_service import EquipmentPanelQueryService
from application.naming import HttpAiItemNamingProvider, ItemNamingBatchService
from application.healing import HealingPanelService
from application.pvp import HonorCoinService, PvpDefenseSnapshotService, PvpService
from application.pvp.panel_service import PvpPanelService
from application.ranking import (
    AsyncLeaderboardRefreshCoordinator,
    LeaderboardPanelService,
    LeaderboardQueryService,
    LeaderboardRefreshService,
)
from application.ranking.score_service import CharacterScoreService
from infrastructure.config.settings import get_settings
from infrastructure.config.static import get_static_config, load_static_config
from infrastructure.config.static.loader import ResourceProvider
from infrastructure.db.health import DatabaseHealthService
from infrastructure.db.repositories import (
    SqlAlchemyBattleRecordRepository,
    SqlAlchemyBreakthroughRepository,
    SqlAlchemyBreakthroughRewardLedgerRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyCharacterScoreSnapshotRepository,
    SqlAlchemyEquipmentRepository,
    SqlAlchemyHonorCoinLedgerRepository,
    SqlAlchemyInventoryRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemyPvpChallengeRepository,
    SqlAlchemySkillRepository,
    SqlAlchemySnapshotRepository,
    SqlAlchemyStateRepository,
)
from infrastructure.db.session import create_engine_from_url, create_session_factory, session_scope
from infrastructure.discord.backpack_panel import BackpackPanelController
from infrastructure.discord.breakthrough_panel import BreakthroughPanelController
from infrastructure.discord.character_panel import CharacterPanelController
from infrastructure.discord.client import XianBotClient
from infrastructure.discord.cultivation_panel import CultivationPanelController
from infrastructure.discord.endless_panel import EndlessPanelController
from infrastructure.discord.equipment_panel import EquipmentPanelController
from infrastructure.discord.forge_panel import ForgePanelController
from infrastructure.discord.leaderboard_panel import LeaderboardPanelController
from infrastructure.discord.pvp_panel import PvpPanelController
from infrastructure.discord.recovery_panel import RecoveryPanelController
from infrastructure.logging.setup import configure_logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplicationServiceBundle:
    """启动期构造的一组应用服务实例。"""

    auto_battle_service: AutoBattleService
    honor_coin_service: HonorCoinService
    pvp_defense_snapshot_service: PvpDefenseSnapshotService
    pvp_service: PvpService
    pvp_panel_service: PvpPanelService
    leaderboard_panel_service: LeaderboardPanelService
    leaderboard_refresh_service: LeaderboardRefreshService
    character_growth_service: CharacterGrowthService
    character_progression_service: CharacterProgressionService
    character_panel_query_service: CharacterPanelQueryService
    endless_dungeon_service: EndlessDungeonService
    endless_panel_query_service: EndlessPanelQueryService
    breakthrough_trial_service: BreakthroughTrialService
    breakthrough_panel_service: BreakthroughPanelService
    cultivation_panel_service: CultivationPanelService
    retreat_service: RetreatService
    profile_panel_query_service: ProfilePanelQueryService
    skill_loadout_service: SkillLoadoutService
    equipment_service: EquipmentService
    equipment_panel_query_service: EquipmentPanelQueryService
    backpack_panel_query_service: BackpackPanelQueryService
    forge_panel_query_service: ForgePanelQueryService
    healing_panel_service: HealingPanelService



def build_client(
    *,
    static_config_resource_dir: str | Path | Traversable | None = None,
    static_config_resource_provider: ResourceProvider | None = None,
) -> XianBotClient:
    """构建 BOT 客户端实例。"""
    settings = get_settings()
    configure_logging(settings.log_level)
    _load_startup_static_config(
        resource_dir=static_config_resource_dir,
        resource_provider=static_config_resource_provider,
    )
    static_config = get_static_config()

    engine = create_engine_from_url(settings.database_url)
    session_factory = create_session_factory(settings.database_url)
    database_health_service = DatabaseHealthService(engine)
    leaderboard_refresh_coordinator = AsyncLeaderboardRefreshCoordinator(
        session_factory=session_factory,
        static_config=static_config,
    )
    cultivation_panel_controller = CultivationPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
    )
    recovery_panel_controller = RecoveryPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
    )
    equipment_panel_controller = EquipmentPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
    )
    backpack_panel_controller = BackpackPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
    )
    forge_panel_controller = ForgePanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
    )
    endless_panel_controller = EndlessPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
    )
    breakthrough_panel_controller = BreakthroughPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
    )
    pvp_panel_controller = PvpPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
    )
    leaderboard_panel_controller = LeaderboardPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
            refresh_coordinator=leaderboard_refresh_coordinator,
        ),
    )
    character_panel_controller = CharacterPanelController(
        session_factory=session_factory,
        service_bundle_factory=lambda session: build_application_service_bundle(
            session=session,
            static_config=static_config,
        ),
        cultivation_panel_controller=cultivation_panel_controller,
        endless_panel_controller=endless_panel_controller,
        breakthrough_panel_controller=breakthrough_panel_controller,
        backpack_panel_controller=backpack_panel_controller,
        forge_panel_controller=forge_panel_controller,
        recovery_panel_controller=recovery_panel_controller,
        pvp_panel_controller=pvp_panel_controller,
        leaderboard_panel_controller=leaderboard_panel_controller,
    )
    _seed_pvp_leaderboard_if_needed(
        session_factory=session_factory,
        static_config=static_config,
    )

    logger.info("BOT 基础组件初始化完成")
    return XianBotClient(
        application_id=settings.discord_application_id,
        session_factory=session_factory,
        database_health_service=database_health_service,
        leaderboard_refresh_coordinator=leaderboard_refresh_coordinator,
        character_panel_controller=character_panel_controller,
        cultivation_panel_controller=cultivation_panel_controller,
        endless_panel_controller=endless_panel_controller,
        breakthrough_panel_controller=breakthrough_panel_controller,
        backpack_panel_controller=backpack_panel_controller,
        forge_panel_controller=forge_panel_controller,
        equipment_panel_controller=equipment_panel_controller,
        recovery_panel_controller=recovery_panel_controller,
        pvp_panel_controller=pvp_panel_controller,
        leaderboard_panel_controller=leaderboard_panel_controller,
        guild_id=settings.discord_guild_id,
    )



def build_application_service_bundle(
    *,
    session,
    static_config,
    refresh_coordinator: AsyncLeaderboardRefreshCoordinator | None = None,
) -> ApplicationServiceBundle:
    """基于当前会话构造阶段 10 首批角色主面板所需服务。"""
    player_repository = SqlAlchemyPlayerRepository(session)
    character_repository = SqlAlchemyCharacterRepository(session)
    inventory_repository = SqlAlchemyInventoryRepository(session)
    equipment_repository = SqlAlchemyEquipmentRepository(session)
    skill_repository = SqlAlchemySkillRepository(session)
    score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
    snapshot_repository = SqlAlchemySnapshotRepository(session)
    battle_record_repository = SqlAlchemyBattleRecordRepository(session)
    pvp_challenge_repository = SqlAlchemyPvpChallengeRepository(session)
    honor_coin_ledger_repository = SqlAlchemyHonorCoinLedgerRepository(session)
    state_repository = SqlAlchemyStateRepository(session)
    breakthrough_repository = SqlAlchemyBreakthroughRepository(session)
    breakthrough_reward_ledger_repository = SqlAlchemyBreakthroughRewardLedgerRepository(session)

    score_service = CharacterScoreService(
        character_repository=character_repository,
        score_snapshot_repository=score_snapshot_repository,
        static_config=static_config,
    )
    growth_service = CharacterGrowthService(
        player_repository=player_repository,
        character_repository=character_repository,
        static_config=static_config,
        score_service=score_service,
    )
    progression_service = CharacterProgressionService(
        character_repository=character_repository,
        inventory_repository=inventory_repository,
        static_config=static_config,
        score_service=score_service,
    )
    skill_loadout_service = SkillLoadoutService(
        character_repository=character_repository,
        skill_repository=skill_repository,
        score_service=score_service,
        static_config=static_config,
    )
    current_attribute_service = CurrentAttributeService(
        character_repository=character_repository,
        skill_repository=skill_repository,
        static_config=static_config,
    )
    skill_drop_service = SkillDropService(
        character_repository=character_repository,
        skill_repository=skill_repository,
        static_config=static_config,
    )
    auto_battle_service = AutoBattleService(
        character_repository=character_repository,
        battle_record_repository=battle_record_repository,
        static_config=static_config,
    )
    honor_coin_service = HonorCoinService(
        character_repository=character_repository,
        honor_coin_ledger_repository=honor_coin_ledger_repository,
        static_config=static_config,
    )
    pvp_defense_snapshot_service = PvpDefenseSnapshotService(
        character_repository=character_repository,
        snapshot_repository=snapshot_repository,
        current_attribute_service=current_attribute_service,
        static_config=static_config,
    )
    healing_panel_service = HealingPanelService(
        character_repository=character_repository,
        state_repository=state_repository,
        static_config=static_config,
    )
    pvp_service = PvpService(
        character_repository=character_repository,
        snapshot_repository=snapshot_repository,
        pvp_challenge_repository=pvp_challenge_repository,
        auto_battle_service=auto_battle_service,
        defense_snapshot_service=pvp_defense_snapshot_service,
        honor_coin_service=honor_coin_service,
        healing_panel_service=healing_panel_service,
        static_config=static_config,
    )
    leaderboard_refresh_service = LeaderboardRefreshService(
        character_repository=character_repository,
        snapshot_repository=snapshot_repository,
        static_config=static_config,
    )
    leaderboard_query_service = LeaderboardQueryService(
        snapshot_repository=snapshot_repository,
        refresh_coordinator=refresh_coordinator,
    )
    retreat_service = RetreatService(
        state_repository=state_repository,
        character_repository=character_repository,
        growth_service=growth_service,
        static_config=static_config,
    )
    equipment_service = EquipmentService(
        character_repository=character_repository,
        equipment_repository=equipment_repository,
        inventory_repository=inventory_repository,
        static_config=static_config,
        score_service=score_service,
    )
    character_panel_query_service = CharacterPanelQueryService(
        player_repository=player_repository,
        character_repository=character_repository,
        snapshot_repository=snapshot_repository,
        growth_service=growth_service,
        progression_service=progression_service,
        score_service=score_service,
        current_attribute_service=current_attribute_service,
        equipment_service=equipment_service,
        static_config=static_config,
    )
    settings = get_settings()
    naming_batch_service = ItemNamingBatchService(
        state_repository=state_repository,
        equipment_service=equipment_service,
        skill_repository=skill_repository,
        skill_runtime_support=skill_loadout_service._skill_runtime_support,
        provider=HttpAiItemNamingProvider.from_settings(settings),
        static_config=static_config,
    )
    endless_dungeon_service = EndlessDungeonService(
        state_repository=state_repository,
        character_repository=character_repository,
        static_config=static_config,
        auto_battle_service=auto_battle_service,
        battle_record_repository=battle_record_repository,
        current_attribute_service=current_attribute_service,
        skill_drop_service=skill_drop_service,
        equipment_service=equipment_service,
        naming_batch_service=naming_batch_service,
        healing_panel_service=healing_panel_service,
    )
    endless_panel_query_service = EndlessPanelQueryService(
        character_panel_query_service=character_panel_query_service,
        endless_dungeon_service=endless_dungeon_service,
        state_repository=state_repository,
        battle_record_repository=battle_record_repository,
        naming_batch_service=naming_batch_service,
        static_config=static_config,
    )
    breakthrough_reward_service = BreakthroughRewardService(
        character_repository=character_repository,
        breakthrough_repository=breakthrough_repository,
        reward_ledger_repository=breakthrough_reward_ledger_repository,
        inventory_repository=inventory_repository,
        battle_record_repository=battle_record_repository,
        static_config=static_config,
    )
    breakthrough_trial_service = BreakthroughTrialService(
        state_repository=state_repository,
        character_repository=character_repository,
        breakthrough_repository=breakthrough_repository,
        auto_battle_service=auto_battle_service,
        reward_service=breakthrough_reward_service,
        current_attribute_service=current_attribute_service,
        static_config=static_config,
    )
    breakthrough_panel_service = BreakthroughPanelService(
        character_panel_query_service=character_panel_query_service,
        progression_service=progression_service,
        trial_service=breakthrough_trial_service,
        breakthrough_repository=breakthrough_repository,
        battle_record_repository=battle_record_repository,
        static_config=static_config,
    )
    cultivation_panel_service = CultivationPanelService(
        growth_service=growth_service,
        progression_service=progression_service,
        retreat_service=retreat_service,
        healing_panel_service=healing_panel_service,
        static_config=static_config,
    )
    profile_panel_query_service = ProfilePanelQueryService(
        character_repository=character_repository,
        skill_loadout_service=skill_loadout_service,
        static_config=static_config,
    )
    equipment_panel_query_service = EquipmentPanelQueryService(
        equipment_service=equipment_service,
        profile_panel_query_service=profile_panel_query_service,
        battle_record_repository=battle_record_repository,
        skill_repository=skill_repository,
        static_config=static_config,
        naming_batch_service=naming_batch_service,
    )
    backpack_panel_query_service = BackpackPanelQueryService(
        equipment_service=equipment_service,
        profile_panel_query_service=profile_panel_query_service,
        static_config=static_config,
    )
    forge_panel_query_service = ForgePanelQueryService(
        equipment_service=equipment_service,
        inventory_repository=inventory_repository,
        profile_panel_query_service=profile_panel_query_service,
        static_config=static_config,
    )
    pvp_panel_service = PvpPanelService(
        character_panel_query_service=character_panel_query_service,
        pvp_service=pvp_service,
        snapshot_repository=snapshot_repository,
        pvp_challenge_repository=pvp_challenge_repository,
        battle_record_repository=battle_record_repository,
        static_config=static_config,
    )
    leaderboard_panel_service = LeaderboardPanelService(
        character_panel_query_service=character_panel_query_service,
        leaderboard_query_service=leaderboard_query_service,
        snapshot_repository=snapshot_repository,
        static_config=static_config,
    )
    return ApplicationServiceBundle(
        auto_battle_service=auto_battle_service,
        honor_coin_service=honor_coin_service,
        pvp_defense_snapshot_service=pvp_defense_snapshot_service,
        pvp_service=pvp_service,
        pvp_panel_service=pvp_panel_service,
        leaderboard_panel_service=leaderboard_panel_service,
        leaderboard_refresh_service=leaderboard_refresh_service,
        character_growth_service=growth_service,
        character_progression_service=progression_service,
        character_panel_query_service=character_panel_query_service,
        endless_dungeon_service=endless_dungeon_service,
        endless_panel_query_service=endless_panel_query_service,
        breakthrough_trial_service=breakthrough_trial_service,
        breakthrough_panel_service=breakthrough_panel_service,
        cultivation_panel_service=cultivation_panel_service,
        retreat_service=retreat_service,
        profile_panel_query_service=profile_panel_query_service,
        skill_loadout_service=skill_loadout_service,
        equipment_service=equipment_service,
        equipment_panel_query_service=equipment_panel_query_service,
        backpack_panel_query_service=backpack_panel_query_service,
        forge_panel_query_service=forge_panel_query_service,
        healing_panel_service=healing_panel_service,
    )



def _seed_pvp_leaderboard_if_needed(*, session_factory, static_config) -> None:
    """启动时补齐 PVP 种子榜。"""
    with session_scope(session_factory) as session:
        bootstrap_services = build_application_service_bundle(
            session=session,
            static_config=static_config,
        )
        bootstrap_services.leaderboard_refresh_service.seed_pvp_board_if_missing()



def _load_startup_static_config(
    *,
    resource_dir: str | Path | Traversable | None,
    resource_provider: ResourceProvider | None,
) -> None:
    """在启动早期加载静态配置，必要时允许测试替换资源来源。"""
    if resource_dir is None and resource_provider is None:
        get_static_config()
        return

    load_static_config(resource_dir=resource_dir, resource_provider=resource_provider)



def run() -> None:
    """启动 BOT。"""
    settings = get_settings()
    client = build_client()
    client.run(settings.discord_bot_token, log_handler=None)
