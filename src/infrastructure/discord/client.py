"""Discord 客户端定义。"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from sqlalchemy.orm import sessionmaker

from application.ranking import AsyncLeaderboardRefreshCoordinator
from infrastructure.db.health import DatabaseHealthService
from infrastructure.discord.breakthrough_panel import BreakthroughPanelController
from infrastructure.discord.character_panel import CharacterPanelController
from infrastructure.discord.cultivation_panel import CultivationPanelController
from infrastructure.discord.endless_panel import EndlessPanelController
from infrastructure.discord.equipment_panel import EquipmentPanelController
from infrastructure.discord.leaderboard_panel import LeaderboardPanelController
from infrastructure.discord.pvp_panel import PvpPanelController
from infrastructure.discord.recovery_panel import RecoveryPanelController

logger = logging.getLogger(__name__)


class XianBotClient(discord.Client):
    """阶段 10 首批 Discord 客户端。"""

    def __init__(
        self,
        *,
        application_id: int,
        session_factory: sessionmaker,
        database_health_service: DatabaseHealthService,
        character_panel_controller: CharacterPanelController,
        cultivation_panel_controller: CultivationPanelController,
        endless_panel_controller: EndlessPanelController,
        breakthrough_panel_controller: BreakthroughPanelController,
        equipment_panel_controller: EquipmentPanelController,
        recovery_panel_controller: RecoveryPanelController,
        pvp_panel_controller: PvpPanelController,
        leaderboard_panel_controller: LeaderboardPanelController,
        leaderboard_refresh_coordinator: AsyncLeaderboardRefreshCoordinator | None = None,
        guild_id: int | None = None,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents, application_id=application_id)
        self.tree = app_commands.CommandTree(self)
        self.session_factory = session_factory
        self.database_health_service = database_health_service
        self.character_panel_controller = character_panel_controller
        self.cultivation_panel_controller = cultivation_panel_controller
        self.endless_panel_controller = endless_panel_controller
        self.breakthrough_panel_controller = breakthrough_panel_controller
        self.equipment_panel_controller = equipment_panel_controller
        self.recovery_panel_controller = recovery_panel_controller
        self.pvp_panel_controller = pvp_panel_controller
        self.leaderboard_panel_controller = leaderboard_panel_controller
        self.leaderboard_refresh_coordinator = leaderboard_refresh_coordinator
        self.guild_id = guild_id
        self._commands_registered = False

    async def setup_hook(self) -> None:
        """在登录后、网关事件前初始化命令。"""
        self._register_commands()
        if self.leaderboard_refresh_coordinator is not None:
            self.leaderboard_refresh_coordinator.attach_loop(self.loop)
            self.leaderboard_refresh_coordinator.start()
            logger.info("已注册阶段 8 榜单后台刷新任务")
        if self.guild_id is not None:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("已同步开发 guild 命令", extra={"guild_id": self.guild_id})
        else:
            await self.tree.sync()
            logger.info("已同步全局命令")

    def _register_commands(self) -> None:
        """注册基础 slash command。"""
        if self._commands_registered:
            return

        @self.tree.command(name="ping", description="检查 BOT 与数据库状态")
        async def ping(interaction: discord.Interaction) -> None:
            self.database_health_service.probe()
            await interaction.response.send_message("pong | bot=ok | db=ok", ephemeral=True)

        xian_group = app_commands.Group(name="修仙", description="修仙主命令")

        @xian_group.command(name="面板", description="打开公开角色主面板")
        async def xian_panel(interaction: discord.Interaction) -> None:
            await self.character_panel_controller.open_public_home(interaction)

        @xian_group.command(name="创建", description="创建角色并进入公开面板")
        async def xian_create(interaction: discord.Interaction) -> None:
            await self.character_panel_controller.start_character_creation(interaction)

        @xian_group.command(name="修炼", description="打开修炼与闭关私有面板")
        async def xian_cultivation(interaction: discord.Interaction) -> None:
            await self.cultivation_panel_controller.open_panel_by_discord_user_id(interaction)

        @xian_group.command(name="无尽", description="打开无尽副本私有面板")
        async def xian_endless(interaction: discord.Interaction) -> None:
            await self.endless_panel_controller.open_panel_by_discord_user_id(interaction)

        @xian_group.command(name="突破", description="打开突破秘境私有面板")
        async def xian_breakthrough(interaction: discord.Interaction) -> None:
            await self.breakthrough_panel_controller.open_panel_by_discord_user_id(interaction)

        @xian_group.command(name="装备", description="打开装备 / 法宝 / 功法私有面板")
        async def xian_equipment(interaction: discord.Interaction) -> None:
            await self.equipment_panel_controller.open_panel_by_discord_user_id(interaction)

        @xian_group.command(name="斗法", description="打开 PVP 挑战私有面板")
        async def xian_pvp(interaction: discord.Interaction) -> None:
            await self.pvp_panel_controller.open_panel_by_discord_user_id(interaction)

        @xian_group.command(name="榜单", description="打开排行榜私有面板")
        async def xian_leaderboard(interaction: discord.Interaction) -> None:
            await self.leaderboard_panel_controller.open_panel_by_discord_user_id(interaction)

        @xian_group.command(name="恢复", description="打开恢复状态私有面板")
        async def xian_recovery(interaction: discord.Interaction) -> None:
            await self.recovery_panel_controller.open_panel_by_discord_user_id(interaction)

        self.tree.add_command(xian_group)
        self._commands_registered = True

    async def close(self) -> None:
        """关闭客户端前停止后台任务。"""
        if self.leaderboard_refresh_coordinator is not None:
            await self.leaderboard_refresh_coordinator.shutdown()
        await super().close()

    async def on_ready(self) -> None:
        """记录客户端上线状态。"""
        if self.user is None:
            logger.info("BOT 已连接，但当前用户信息未就绪")
            return
        logger.info("BOT 已上线：%s", self.user)
