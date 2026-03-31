"""Discord 角色主面板交互。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import logging
from typing import Protocol
import unicodedata
from weakref import WeakKeyDictionary

import discord
from sqlalchemy.orm import Session, sessionmaker

from application.character import CharacterAlreadyExistsError, CharacterGrowthService
from application.character.panel_query_service import (
    CharacterPanelOverview,
    CharacterPanelQueryService,
    CharacterPanelQueryServiceError,
    DiscordCharacterBindingNotFoundError,
)
from infrastructure.db.session import session_scope

logger = logging.getLogger(__name__)

_PUBLIC_PANEL_TIMEOUT_SECONDS = 5 * 60
_PRIVATE_PANEL_TIMEOUT_SECONDS = 14 * 60
_PUBLIC_BROADCAST_DELETE_AFTER_SECONDS = 60
_EXPIRED_PANEL_FOOTER_TEXT = "交互已过期，请重新打开最新面板。"
_VIEW_MESSAGE_REGISTRY: WeakKeyDictionary[discord.ui.View, discord.Message] = WeakKeyDictionary()


class PanelVisibility(StrEnum):
    """Discord 面板响应可见性。"""

    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class PanelMessagePayload:
    """单次 Discord 响应载荷。"""

    embed: discord.Embed
    view: discord.ui.View | None = None


class CharacterPanelServiceBundle(Protocol):
    """角色主面板所需的最小服务集合。"""

    character_panel_query_service: CharacterPanelQueryService
    character_growth_service: CharacterGrowthService


class PrivatePanelController(Protocol):
    """私有子面板控制器协议。"""

    async def open_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """打开指定角色的私有面板。"""


def _cap_private_view_timeout(view: discord.ui.View | None) -> None:
    if view is None:
        return
    current_timeout = view.timeout
    if current_timeout is None or current_timeout > _PRIVATE_PANEL_TIMEOUT_SECONDS:
        view.timeout = _PRIVATE_PANEL_TIMEOUT_SECONDS



def _cap_public_view_timeout(view: discord.ui.View | None) -> None:
    if view is None:
        return
    current_timeout = view.timeout
    if current_timeout is None or current_timeout > _PUBLIC_PANEL_TIMEOUT_SECONDS:
        view.timeout = _PUBLIC_PANEL_TIMEOUT_SECONDS


def _bind_view_message(view: discord.ui.View | None, message: discord.Message) -> None:
    if view is None:
        return
    _VIEW_MESSAGE_REGISTRY[view] = message
    bind_message = getattr(view, "bind_message", None)
    if callable(bind_message):
        bind_message(message)


def _build_timeout_embed(message: discord.Message) -> discord.Embed | None:
    if not message.embeds:
        return None
    embed = discord.Embed.from_dict(message.embeds[0].to_dict())
    footer_text = getattr(embed.footer, "text", None)
    footer_icon_url = getattr(embed.footer, "icon_url", None)
    if footer_text and _EXPIRED_PANEL_FOOTER_TEXT in footer_text:
        return embed
    next_footer_text = _EXPIRED_PANEL_FOOTER_TEXT if not footer_text else f"{footer_text}｜{_EXPIRED_PANEL_FOOTER_TEXT}"
    if footer_icon_url:
        embed.set_footer(text=next_footer_text, icon_url=footer_icon_url)
        return embed
    embed.set_footer(text=next_footer_text)
    return embed


async def _managed_view_on_timeout(self: discord.ui.View) -> None:
    message = _VIEW_MESSAGE_REGISTRY.get(self)
    if message is None:
        return
    try:
        await message.delete()
        return
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        await message.edit(view=None)
    except (discord.Forbidden, discord.HTTPException):
        logger.warning("私有面板超时回收失败", extra={"message_id": message.id, "view": type(self).__name__})


discord.ui.View.on_timeout = _managed_view_on_timeout  # type: ignore[method-assign]


class DiscordInteractionVisibilityResponder:
    """统一处理公开与私有响应。"""

    async def send_message(
        self,
        interaction: discord.Interaction,
        *,
        payload: PanelMessagePayload,
        visibility: PanelVisibility,
    ) -> None:
        if visibility is PanelVisibility.PRIVATE:
            _cap_private_view_timeout(payload.view)
        else:
            _cap_public_view_timeout(payload.view)
        await interaction.response.send_message(
            embed=payload.embed,
            view=payload.view,
            ephemeral=visibility is PanelVisibility.PRIVATE,
        )
        try:
            message = await interaction.original_response()
        except (discord.NotFound, discord.HTTPException):
            return
        _bind_view_message(payload.view, message)

    async def send_private_followup_message(
        self,
        interaction: discord.Interaction,
        *,
        payload: PanelMessagePayload,
    ) -> discord.Message | None:
        _cap_private_view_timeout(payload.view)
        message = await interaction.followup.send(
            embed=payload.embed,
            view=payload.view,
            ephemeral=True,
            wait=True,
        )
        _bind_view_message(payload.view, message)
        return message

    async def edit_private_followup_message(
        self,
        message: discord.Message,
        *,
        payload: PanelMessagePayload,
    ) -> discord.Message:
        _cap_private_view_timeout(payload.view)
        await message.edit(embed=payload.embed, view=payload.view)
        _bind_view_message(payload.view, message)
        return message

    async def edit_public_message(
        self,
        interaction: discord.Interaction,
        *,
        payload: PanelMessagePayload,
    ) -> None:
        _cap_public_view_timeout(payload.view)
        await interaction.response.edit_message(embed=payload.embed, view=payload.view)
        if interaction.message is not None:
            _bind_view_message(payload.view, interaction.message)

    async def edit_message(
        self,
        interaction: discord.Interaction,
        *,
        payload: PanelMessagePayload,
    ) -> None:
        _cap_private_view_timeout(payload.view)
        await interaction.response.edit_message(embed=payload.embed, view=payload.view)
        if interaction.message is not None:
            _bind_view_message(payload.view, interaction.message)

    async def send_public_broadcast(self, channel: discord.abc.Messageable, *, embed: discord.Embed) -> None:
        await channel.send(embed=embed, delete_after=_PUBLIC_BROADCAST_DELETE_AFTER_SECONDS)

    async def send_private_error(self, interaction: discord.Interaction, *, message: str) -> None:
        embed = discord.Embed(title="角色主面板", description=message, color=discord.Color.red())
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)


class CharacterPanelPresenter:
    """负责把角色总览投影为 Discord Embed。"""

    @classmethod
    def build_public_home_embed(
        cls,
        *,
        overview: CharacterPanelOverview,
        discord_display_name: str,
        avatar_url: str | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"{overview.character_name}｜修仙面板",
            color=discord.Color.blurple(),
        )
        del discord_display_name
        embed.add_field(name="🌟 修行概览", value=cls._build_cultivation_summary_block(overview), inline=True)
        embed.add_field(name="🫀 核心状态", value=cls._build_core_status_block(overview), inline=True)
        embed.add_field(name="📜 功法", value=cls._build_skill_block(overview), inline=False)
        embed.add_field(name="⚔️ 装备 / 法宝", value=cls._build_equipment_artifact_block(overview), inline=False)
        embed.add_field(name="📊 属性总览", value=cls._build_combined_stats_block(overview), inline=False)
        embed.add_field(name="📈 修行进度", value=cls._build_progress_block(overview), inline=False)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        return embed

    @classmethod
    def build_creation_guide_embed(cls) -> discord.Embed:
        embed = discord.Embed(
            title="角色创建",
            description="当前还没有角色档案。点击下方按钮填写角色名与可选称号，创建完成后即可进入公开主面板。",
            color=discord.Color.gold(),
        )
        embed.add_field(
            name="说明",
            value="公开入口会继续保留；只有显式提交创建后，才会生成角色。",
            inline=False,
        )
        embed.set_footer(text="本消息仅自己可见")
        return embed

    @classmethod
    def build_existing_character_embed(cls) -> discord.Embed:
        embed = discord.Embed(
            title="角色创建",
            description="你已经创建过角色，无需重复创建。",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="下一步",
            value="点击下方按钮即可进入公开主面板。",
            inline=False,
        )
        embed.set_footer(text="本消息仅自己可见")
        return embed

    @classmethod
    def build_creation_success_embed(
        cls,
        *,
        character_name: str,
        character_title: str | None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="角色创建完成",
            description=f"角色 **{character_name}** 已创建成功。",
            color=discord.Color.green(),
        )
        embed.add_field(name="称号", value=character_title or "无", inline=True)
        embed.add_field(
            name="下一步",
            value="点击下方按钮即可进入公开主面板。",
            inline=False,
        )
        embed.set_footer(text="本消息仅自己可见")
        return embed

    @staticmethod
    def _build_identity_block(*, overview: CharacterPanelOverview, discord_display_name: str) -> str:
        return (
            f"角色：{overview.character_name}\n"
            f"Discord：{discord_display_name or overview.player_display_name}\n"
            f"绑定名：{overview.player_display_name}"
        )

    @staticmethod
    def _build_main_skill_block(overview: CharacterPanelOverview) -> str:
        main_skill = overview.main_skill
        return (
            f"{main_skill.skill_name}\n"
            f"{main_skill.rank_name}｜{main_skill.quality_name}\n"
            f"{main_skill.path_name}"
        )

    @classmethod
    def _build_cultivation_summary_block(cls, overview: CharacterPanelOverview) -> str:
        return (
            f"🏷 称号：{overview.character_title or '无'}\n"
            f"🎖 徽记：{overview.badge_name or '无'}\n"
            f"🧭 境界：{overview.realm_name}·{overview.stage_name}\n"
            f"🔥 战力：{overview.public_power_score}\n"
            f"💰 灵石：{overview.spirit_stone}"
        )

    @classmethod
    def _build_skill_block(cls, overview: CharacterPanelOverview) -> str:
        lines = [
            (
                f"{cls._skill_slot_label(overview.main_skill.slot_id)}：{overview.main_skill.skill_name}｜"
                f"{overview.main_skill.rank_name}｜{overview.main_skill.quality_name}｜{overview.main_skill.path_name}"
            )
        ]
        for skill in overview.auxiliary_skills:
            lines.append(
                f"{cls._skill_slot_label(skill.slot_id)}：{skill.skill_name}｜{skill.rank_name}｜"
                f"{skill.quality_name}｜{skill.path_name}"
            )
        return "\n".join(lines)

    @staticmethod
    def _build_core_status_block(overview: CharacterPanelOverview) -> str:
        projection = overview.battle_projection
        return (
            f"❤️ 气血：{projection.current_hp}/{projection.max_hp}\n"
            f"🔷 灵力：{projection.current_resource}/{projection.max_resource}"
        )

    @classmethod
    def _build_equipment_artifact_block(cls, overview: CharacterPanelOverview) -> str:
        lines: list[str] = []
        for slot in overview.equipment_slots:
            icon = cls._slot_icon(slot.slot_id)
            if slot.item is None:
                lines.append(f"{icon} {slot.slot_name}：未装备")
                continue
            lines.append(f"{icon} {slot.slot_name}：{cls._format_equipment_item_head(slot.item)}")
        if overview.artifact_item is None:
            lines.append("💠 本命法宝：未装备")
        else:
            lines.append(f"💠 本命法宝：{cls._format_equipment_item_head(overview.artifact_item)}")
        return "\n".join(lines)

    @classmethod
    def _format_equipment_item_head(cls, item) -> str:
        return f"[{item.quality_name}·{item.rank_name}] {item.display_name}｜强化 +{item.enhancement_level}"

    @classmethod
    def _build_combined_stats_block(cls, overview: CharacterPanelOverview) -> str:
        projection = overview.battle_projection
        return cls._build_two_column_stat_code_block(
            (
                (f"⚔ 攻力：{projection.attack_power}", f"🩸 穿透：{cls._format_permille(projection.damage_bonus_permille)}"),
                (f"🛡 护体：{projection.guard_power}", f"🧱 减伤：{cls._format_permille(projection.damage_reduction_permille)}"),
                (f"💨 迅捷：{projection.speed}", f"↩ 反击：{cls._format_permille(projection.counter_rate_permille)}"),
                (f"🎯 命中：{cls._format_permille(projection.hit_rate_permille)}", f"🪄 控势：{cls._format_permille(projection.control_bonus_permille)}"),
                (f"🍃 闪避：{cls._format_permille(projection.dodge_rate_permille)}", f"🧘 定心：{cls._format_permille(projection.control_resist_permille)}"),
                (f"💥 暴击：{cls._format_permille(projection.crit_rate_permille)}", f"💚 疗愈：{cls._format_permille(projection.healing_power_permille)}"),
                (f"🔥 暴伤：{cls._format_permille(projection.crit_damage_bonus_permille)}", f"🫧 护盾：{cls._format_permille(projection.shield_power_permille)}"),
            )
        )

    @classmethod
    def _build_two_column_stat_code_block(cls, rows: tuple[tuple[str, str | None], ...]) -> str:
        left_width = max(cls._display_width(left) for left, _ in rows)
        formatted_rows: list[str] = []
        for left, right in rows:
            if not right:
                formatted_rows.append(left)
                continue
            formatted_rows.append(f"{cls._pad_display_text(left, width=left_width + 5)}{right}")
        return cls._build_stat_code_block(tuple(formatted_rows))

    @classmethod
    def _pad_display_text(cls, value: str, *, width: int) -> str:
        padding = max(0, width - cls._display_width(value))
        return value + (" " * padding)

    @staticmethod
    def _display_width(value: str) -> int:
        width = 0
        for char in value:
            width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        return width

    @classmethod
    def _build_progress_block(cls, overview: CharacterPanelOverview) -> str:
        target_realm_name = overview.target_realm_name or "当前已到开放上限"
        return "\n".join(
            (
                f"目标境界：{target_realm_name}",
                cls._build_progress_line(
                    label="修为",
                    current=overview.current_cultivation_value,
                    required=overview.required_cultivation_value,
                ),
                cls._build_progress_line(
                    label="感悟",
                    current=overview.current_comprehension_value,
                    required=overview.required_comprehension_value,
                ),
            )
        )

    @classmethod
    def _build_progress_line(cls, *, label: str, current: int, required: int | None) -> str:
        if required is None or required <= 0:
            return f"{label}：{cls._build_progress_bar(ratio=1.0)} 已达上限"
        ratio = cls._normalize_progress_ratio(current=current, required=required)
        return (
            f"{label}：{cls._build_progress_bar(ratio=ratio)} {ratio * 100:.1f}%\n"
            f"{max(0, min(current, required))}/{required}"
        )

    @staticmethod
    def _normalize_progress_ratio(*, current: int, required: int) -> float:
        if required <= 0:
            return 1.0
        normalized = max(0.0, min(float(current) / float(required), 1.0))
        return normalized

    @staticmethod
    def _build_progress_bar(*, ratio: float, width: int = 12) -> str:
        clamped_ratio = max(0.0, min(ratio, 1.0))
        filled = int(round(clamped_ratio * width))
        filled = max(0, min(filled, width))
        return "▰" * filled + "▱" * (width - filled)

    @staticmethod
    def _build_stat_code_block(lines: tuple[str, ...]) -> str:
        return "```text\n" + "\n".join(lines) + "\n```"

    @staticmethod
    def _slot_icon(slot_id: str) -> str:
        return {
            "weapon": "⚔",
            "armor": "🛡",
            "accessory": "🧿",
            "artifact": "💠",
        }.get(slot_id, "•")

    @staticmethod
    def _skill_slot_label(slot_id: str) -> str:
        return {
            "main": "🗡 主修",
            "guard": "🛡 护体",
            "movement": "👣 身法",
            "spirit": "✨ 灵技",
        }.get(slot_id, f"📘 {slot_id}")

    @staticmethod
    def _format_permille(value: int) -> str:
        return f"{value / 10:.1f}%"


def _slot_name_to_chinese(slot_id: str) -> str:
    return {
        "main": "主修",
        "guard": "护体",
        "movement": "身法",
        "spirit": "灵技",
    }.get(slot_id, slot_id)


class CharacterHomePanelView(discord.ui.View):
    """公开角色主面板交互视图。"""

    def __init__(
        self,
        *,
        controller: CharacterPanelController,
        owner_user_id: int,
        character_id: int,
        timeout: float = _PUBLIC_PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller
        self.owner_user_id = owner_user_id
        self.character_id = character_id
        self.message: discord.Message | None = None

    def bind_message(self, message: discord.Message) -> None:
        """记录当前公开消息，用于超时回收。"""
        self.message = message

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        await self._controller.responder.send_private_error(
            interaction,
            message="该公开主面板仅允许发起者操作。",
        )
        return False

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        try:
            await self.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            try:
                embed = _build_timeout_embed(self.message)
                if embed is None:
                    await self.message.edit(view=None)
                else:
                    await self.message.edit(embed=embed, view=None)
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("公开角色主面板回收失败", extra={"message_id": self.message.id})

    @discord.ui.button(label="刷新主面板", style=discord.ButtonStyle.primary)
    async def refresh_home(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.refresh_public_home(
            interaction,
            character_id=self.character_id,
            owner_user_id=self.owner_user_id,
        )


    @discord.ui.button(label="修炼", style=discord.ButtonStyle.secondary, row=1)
    async def open_cultivation(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_cultivation_panel(
            interaction,
            character_id=self.character_id,
        )

    @discord.ui.button(label="无涯渊境", style=discord.ButtonStyle.secondary, row=1)
    async def open_endless(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_endless_panel(
            interaction,
            character_id=self.character_id,
        )

    @discord.ui.button(label="突破秘境", style=discord.ButtonStyle.secondary, row=1)
    async def open_breakthrough(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_breakthrough_panel(
            interaction,
            character_id=self.character_id,
        )

    @discord.ui.button(label="背包", style=discord.ButtonStyle.secondary, row=1)
    async def open_backpack(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_backpack_panel(
            interaction,
            character_id=self.character_id,
        )

    @discord.ui.button(label="锻造", style=discord.ButtonStyle.secondary, row=1)
    async def open_forge(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_forge_panel(
            interaction,
            character_id=self.character_id,
        )

    @discord.ui.button(label="仙榜论道", style=discord.ButtonStyle.secondary, row=2)
    async def open_pvp(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_pvp_panel(
            interaction,
            character_id=self.character_id,
        )

    @discord.ui.button(label="天榜", style=discord.ButtonStyle.secondary, row=2)
    async def open_leaderboard(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_leaderboard_panel(
            interaction,
            character_id=self.character_id,
        )

    @discord.ui.button(label="恢复状态", style=discord.ButtonStyle.secondary, row=2)
    async def open_recovery(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_recovery_panel(
            interaction,
            character_id=self.character_id,
        )


class CharacterCreationGuideView(discord.ui.View):
    """角色创建引导视图。"""

    def __init__(
        self,
        *,
        controller: CharacterPanelController,
        timeout: float = _PUBLIC_PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller

    @discord.ui.button(label="创建角色", style=discord.ButtonStyle.success)
    async def start_creation(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.start_character_creation(interaction)


class CharacterOpenPublicHomeView(discord.ui.View):
    """公开主面板入口视图。"""

    def __init__(
        self,
        *,
        controller: CharacterPanelController,
        timeout: float = _PUBLIC_PANEL_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._controller = controller

    @discord.ui.button(label="进入公开主面板", style=discord.ButtonStyle.primary)
    async def open_public_home(  # type: ignore[override]
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        del button
        await self._controller.open_public_home(interaction)


class CharacterCreationModal(discord.ui.Modal, title="创建角色"):
    """角色创建输入框。"""

    def __init__(self, *, controller: CharacterPanelController) -> None:
        super().__init__()
        self._controller = controller
        self.character_name_input = discord.ui.TextInput(
            label="角色名",
            placeholder="请输入角色名",
            min_length=1,
            max_length=64,
            required=True,
        )
        self.character_title_input = discord.ui.TextInput(
            label="称号（可选）",
            placeholder="可留空",
            max_length=64,
            required=False,
        )
        self.add_item(self.character_name_input)
        self.add_item(self.character_title_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._controller.submit_character_creation(
            interaction,
            character_name=self.character_name_input.value,
            title=self.character_title_input.value,
        )


class CharacterPanelController:
    """组织公开主面板与私有详情的 Discord 交互。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        service_bundle_factory: Callable[[Session], CharacterPanelServiceBundle],
        cultivation_panel_controller: PrivatePanelController | None = None,
        endless_panel_controller: PrivatePanelController | None = None,
        breakthrough_panel_controller: PrivatePanelController | None = None,
        backpack_panel_controller: PrivatePanelController | None = None,
        forge_panel_controller: PrivatePanelController | None = None,
        recovery_panel_controller: PrivatePanelController | None = None,
        pvp_panel_controller: PrivatePanelController | None = None,
        leaderboard_panel_controller: PrivatePanelController | None = None,
        responder: DiscordInteractionVisibilityResponder | None = None,
        public_panel_timeout: float = _PUBLIC_PANEL_TIMEOUT_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._service_bundle_factory = service_bundle_factory
        self._cultivation_panel_controller = cultivation_panel_controller
        self._endless_panel_controller = endless_panel_controller
        self._breakthrough_panel_controller = breakthrough_panel_controller
        self._backpack_panel_controller = backpack_panel_controller
        self._forge_panel_controller = forge_panel_controller
        self._recovery_panel_controller = recovery_panel_controller
        self._pvp_panel_controller = pvp_panel_controller
        self._leaderboard_panel_controller = leaderboard_panel_controller
        self.responder = responder or DiscordInteractionVisibilityResponder()
        self._public_panel_timeout = public_panel_timeout

    async def open_public_home(self, interaction: discord.Interaction) -> None:
        """打开公开角色主面板。"""
        try:
            overview = self._load_overview_by_discord_user_id(discord_user_id=str(interaction.user.id))
        except DiscordCharacterBindingNotFoundError:
            await self._send_creation_guide(interaction)
            return
        except CharacterPanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        view = self._build_home_view(owner_user_id=interaction.user.id, character_id=overview.character_id)
        payload = PanelMessagePayload(
            embed=CharacterPanelPresenter.build_public_home_embed(
                overview=overview,
                discord_display_name=_resolve_display_name(interaction.user),
                avatar_url=_resolve_avatar_url(interaction.user),
            ),
            view=view,
        )
        await self.responder.send_message(
            interaction,
            payload=payload,
            visibility=PanelVisibility.PUBLIC,
        )

    async def start_character_creation(self, interaction: discord.Interaction) -> None:
        """进入显式角色创建链路。"""
        try:
            self._load_overview_by_discord_user_id(discord_user_id=str(interaction.user.id))
        except DiscordCharacterBindingNotFoundError:
            await self.present_creation_modal(interaction)
            return
        except CharacterPanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return
        await self._send_existing_character_prompt(interaction)

    async def present_creation_modal(self, interaction: discord.Interaction) -> None:
        """弹出角色创建输入框。"""
        await interaction.response.send_modal(CharacterCreationModal(controller=self))

    async def submit_character_creation(
        self,
        interaction: discord.Interaction,
        *,
        character_name: str,
        title: str,
    ) -> None:
        """提交角色创建。"""
        normalized_character_name = character_name.strip()
        normalized_title = title.strip() or None
        if not normalized_character_name:
            await self.responder.send_private_error(interaction, message="角色名不能为空。")
            return

        try:
            snapshot = self._create_character(
                discord_user_id=str(interaction.user.id),
                player_display_name=_resolve_display_name(interaction.user),
                character_name=normalized_character_name,
                title=normalized_title,
            )
        except CharacterAlreadyExistsError:
            await self._send_existing_character_prompt(interaction)
            return
        except Exception:
            logger.exception("角色创建失败", extra={"discord_user_id": str(interaction.user.id)})
            await self.responder.send_private_error(interaction, message="角色创建失败，请稍后重试。")
            return

        await self._send_creation_success(
            interaction,
            character_name=snapshot.character_name,
            character_title=snapshot.character_title,
        )

    async def refresh_public_home(
        self,
        interaction: discord.Interaction,
        *,
        character_id: int,
        owner_user_id: int,
    ) -> None:
        """刷新公开角色主面板。"""
        try:
            overview = self._load_overview(character_id=character_id)
        except CharacterPanelQueryServiceError as exc:
            await self.responder.send_private_error(interaction, message=str(exc))
            return

        view = self._build_home_view(owner_user_id=owner_user_id, character_id=character_id)
        payload = PanelMessagePayload(
            embed=CharacterPanelPresenter.build_public_home_embed(
                overview=overview,
                discord_display_name=_resolve_display_name(interaction.user),
                avatar_url=_resolve_avatar_url(interaction.user),
            ),
            view=view,
        )
        await self.responder.edit_public_message(interaction, payload=payload)

    async def open_cultivation_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """从公开主面板打开修炼私有面板。"""
        if self._cultivation_panel_controller is None:
            await self.responder.send_private_error(interaction, message="修炼面板尚未接入。")
            return
        await self._cultivation_panel_controller.open_panel(interaction, character_id=character_id)

    async def open_endless_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """从公开主面板打开无涯渊境私有面板。"""
        if self._endless_panel_controller is None:
            await self.responder.send_private_error(interaction, message="无涯渊境面板尚未接入。")
            return
        await self._endless_panel_controller.open_panel(interaction, character_id=character_id)

    async def open_breakthrough_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """从公开主面板打开突破秘境私有面板。"""
        if self._breakthrough_panel_controller is None:
            await self.responder.send_private_error(interaction, message="突破秘境面板尚未接入。")
            return
        await self._breakthrough_panel_controller.open_panel(interaction, character_id=character_id)

    async def open_backpack_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """从公开主面板打开背包私有面板。"""
        if self._backpack_panel_controller is None:
            await self.responder.send_private_error(interaction, message="背包面板尚未接入。")
            return
        await self._backpack_panel_controller.open_panel(interaction, character_id=character_id)

    async def open_forge_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """从公开主面板打开锻造私有面板。"""
        if self._forge_panel_controller is None:
            await self.responder.send_private_error(interaction, message="锻造面板尚未接入。")
            return
        await self._forge_panel_controller.open_panel(interaction, character_id=character_id)

    async def open_equipment_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """兼容旧入口：转发到背包私有面板。"""
        await self.open_backpack_panel(interaction, character_id=character_id)

    async def open_pvp_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """从公开主面板打开仙榜论道私有面板。"""
        if self._pvp_panel_controller is None:
            await self.responder.send_private_error(interaction, message="仙榜论道面板尚未接入。")
            return
        await self._pvp_panel_controller.open_panel(interaction, character_id=character_id)

    async def open_leaderboard_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """从公开主面板打开榜单私有面板。"""
        if self._leaderboard_panel_controller is None:
            await self.responder.send_private_error(interaction, message="榜单面板尚未接入。")
            return
        await self._leaderboard_panel_controller.open_panel(interaction, character_id=character_id)

    async def open_recovery_panel(self, interaction: discord.Interaction, *, character_id: int) -> None:
        """从公开主面板打开恢复私有面板。"""
        if self._recovery_panel_controller is None:
            await self.responder.send_private_error(interaction, message="恢复面板尚未接入。")
            return
        await self._recovery_panel_controller.open_panel(interaction, character_id=character_id)

    async def _send_creation_guide(self, interaction: discord.Interaction) -> None:
        payload = PanelMessagePayload(
            embed=CharacterPanelPresenter.build_creation_guide_embed(),
            view=CharacterCreationGuideView(
                controller=self,
                timeout=self._public_panel_timeout,
            ),
        )
        await self.responder.send_message(
            interaction,
            payload=payload,
            visibility=PanelVisibility.PRIVATE,
        )

    async def _send_existing_character_prompt(self, interaction: discord.Interaction) -> None:
        payload = PanelMessagePayload(
            embed=CharacterPanelPresenter.build_existing_character_embed(),
            view=CharacterOpenPublicHomeView(
                controller=self,
                timeout=self._public_panel_timeout,
            ),
        )
        await self.responder.send_message(
            interaction,
            payload=payload,
            visibility=PanelVisibility.PRIVATE,
        )

    async def _send_creation_success(
        self,
        interaction: discord.Interaction,
        *,
        character_name: str,
        character_title: str | None,
    ) -> None:
        payload = PanelMessagePayload(
            embed=CharacterPanelPresenter.build_creation_success_embed(
                character_name=character_name,
                character_title=character_title,
            ),
            view=CharacterOpenPublicHomeView(
                controller=self,
                timeout=self._public_panel_timeout,
            ),
        )
        await self.responder.send_message(
            interaction,
            payload=payload,
            visibility=PanelVisibility.PRIVATE,
        )

    def _create_character(
        self,
        *,
        discord_user_id: str,
        player_display_name: str,
        character_name: str,
        title: str | None,
    ):
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.character_growth_service.create_character(
                discord_user_id=discord_user_id,
                player_display_name=player_display_name,
                character_name=character_name,
                title=title,
            )

    def _load_overview_by_discord_user_id(self, *, discord_user_id: str) -> CharacterPanelOverview:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.character_panel_query_service.get_overview_by_discord_user_id(
                discord_user_id=discord_user_id,
            )

    def _load_overview(self, *, character_id: int) -> CharacterPanelOverview:
        with session_scope(self._session_factory) as session:
            services = self._service_bundle_factory(session)
            return services.character_panel_query_service.get_overview(character_id=character_id)

    def _build_home_view(self, *, owner_user_id: int, character_id: int) -> CharacterHomePanelView:
        return CharacterHomePanelView(
            controller=self,
            owner_user_id=owner_user_id,
            character_id=character_id,
            timeout=self._public_panel_timeout,
        )


def _resolve_display_name(user: discord.abc.User) -> str:
    display_name = getattr(user, "display_name", None)
    if isinstance(display_name, str) and display_name:
        return display_name
    return user.name



def _resolve_avatar_url(user: discord.abc.User) -> str | None:
    display_avatar = getattr(user, "display_avatar", None)
    if display_avatar is None:
        return None
    return display_avatar.url


__all__ = [
    "CharacterPanelController",
    "CharacterPanelPresenter",
    "DiscordInteractionVisibilityResponder",
    "PanelMessagePayload",
    "PanelVisibility",
]
