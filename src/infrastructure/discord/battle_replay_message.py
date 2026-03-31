"""Discord 战斗回放独立消息支持。"""

from __future__ import annotations

import asyncio

import discord

from application.battle import BattleReplayFrame, BattleReplayPresentation
from infrastructure.discord.character_panel import DiscordInteractionVisibilityResponder, PanelMessagePayload

_MAX_DESCRIPTION_LENGTH = 3900


class BattleReplayMessagePresenter:
    """负责把战斗回放帧投影为独立消息 Embed。"""

    @classmethod
    def build_embed(
        cls,
        *,
        presentation: BattleReplayPresentation,
        frame: BattleReplayFrame,
        frame_index: int,
        total_frames: int,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=frame.title,
            description=cls._build_description(frame=frame),
            color=cls._resolve_color(result=presentation.result),
        )
        progress_text = f"演出 {frame_index + 1}/{max(1, total_frames)}"
        footer_text = progress_text if not frame.footer else f"{frame.footer}｜{progress_text}"
        embed.set_footer(text=footer_text[:2048])
        return embed

    @staticmethod
    def _build_description(*, frame: BattleReplayFrame) -> str:
        lines = tuple(
            line.strip()
            for line in frame.lines
            if isinstance(line, str) and line.strip()
        )
        description = "\n".join(lines) if lines else "战局已定。"
        return description[:_MAX_DESCRIPTION_LENGTH]

    @staticmethod
    def _resolve_color(*, result: str) -> discord.Color:
        if result == "ally_victory":
            return discord.Color.gold()
        if result == "enemy_victory":
            return discord.Color.red()
        return discord.Color.blurple()


class BattleReplayMessagePlayer:
    """负责发送并逐步编辑独立战斗回放消息。"""

    def __init__(
        self,
        *,
        responder: DiscordInteractionVisibilityResponder,
    ) -> None:
        self._responder = responder

    async def play(
        self,
        interaction: discord.Interaction,
        *,
        presentation: BattleReplayPresentation,
    ) -> None:
        """发送独立私有消息并按少量帧进行累计编辑。"""
        if not presentation.frames:
            return
        first_frame = presentation.frames[0]
        message = await self._responder.send_private_followup_message(
            interaction,
            payload=PanelMessagePayload(
                embed=BattleReplayMessagePresenter.build_embed(
                    presentation=presentation,
                    frame=first_frame,
                    frame_index=0,
                    total_frames=len(presentation.frames),
                )
            ),
        )
        if message is None:
            return
        for frame_index, previous_frame in enumerate(presentation.frames[:-1]):
            if previous_frame.pause_seconds > 0:
                await asyncio.sleep(previous_frame.pause_seconds)
            next_frame = presentation.frames[frame_index + 1]
            await self._responder.edit_private_followup_message(
                message,
                payload=PanelMessagePayload(
                    embed=BattleReplayMessagePresenter.build_embed(
                        presentation=presentation,
                        frame=next_frame,
                        frame_index=frame_index + 1,
                        total_frames=len(presentation.frames),
                    )
                ),
            )


__all__ = [
    "BattleReplayMessagePlayer",
    "BattleReplayMessagePresenter",
]
