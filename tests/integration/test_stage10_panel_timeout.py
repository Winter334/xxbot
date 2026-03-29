"""阶段 10 Discord 面板超时管理测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from infrastructure.discord.character_panel import (
    CharacterCreationGuideView,
    CharacterHomePanelView,
    CharacterOpenPublicHomeView,
    DiscordInteractionVisibilityResponder,
    PanelMessagePayload,
    PanelVisibility,
)


def _build_message(message_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        embeds=[discord.Embed(title="角色面板")],
        delete=AsyncMock(),
        edit=AsyncMock(),
        id=message_id,
    )


@pytest.mark.asyncio
async def test_private_send_message_caps_timeout_and_deletes_message_on_expiry() -> None:
    responder = DiscordInteractionVisibilityResponder()
    view = CharacterCreationGuideView(controller=SimpleNamespace(), timeout=20 * 60)
    message = _build_message(message_id=42001)
    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock()),
        original_response=AsyncMock(return_value=message),
    )
    payload = PanelMessagePayload(embed=discord.Embed(title="角色创建"), view=view)

    await responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PRIVATE)

    assert view.timeout == 14 * 60
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True

    await view.on_timeout()

    message.delete.assert_awaited_once()
    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_edit_message_caps_rebuilt_view_and_rebinds_current_message() -> None:
    responder = DiscordInteractionVisibilityResponder()
    view = CharacterOpenPublicHomeView(controller=SimpleNamespace(), timeout=20 * 60)
    message = _build_message(message_id=42002)
    interaction = SimpleNamespace(
        response=SimpleNamespace(edit_message=AsyncMock()),
        message=message,
    )
    payload = PanelMessagePayload(embed=discord.Embed(title="角色创建完成"), view=view)

    await responder.edit_message(interaction, payload=payload)

    assert view.timeout == 14 * 60
    interaction.response.edit_message.assert_awaited_once()
    assert interaction.response.edit_message.await_args.kwargs["view"] is view

    await view.on_timeout()

    message.delete.assert_awaited_once()
    message.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_timeout_falls_back_to_removing_view_when_delete_fails() -> None:
    responder = DiscordInteractionVisibilityResponder()
    view = CharacterCreationGuideView(controller=SimpleNamespace(), timeout=20 * 60)
    message = _build_message(message_id=42009)
    message.delete = AsyncMock(side_effect=discord.HTTPException(response=SimpleNamespace(status=500, reason="err"), message="boom"))
    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock()),
        original_response=AsyncMock(return_value=message),
    )
    payload = PanelMessagePayload(embed=discord.Embed(title="角色创建"), view=view)

    await responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PRIVATE)
    await view.on_timeout()

    message.delete.assert_awaited_once()
    message.edit.assert_awaited_once_with(view=None)


@pytest.mark.asyncio
async def test_public_send_message_binds_message_for_timeout_cleanup() -> None:
    responder = DiscordInteractionVisibilityResponder()
    view = CharacterHomePanelView(
        controller=SimpleNamespace(),
        owner_user_id=30001,
        character_id=1001,
        timeout=20 * 60,
    )
    message = _build_message(message_id=42003)
    interaction = SimpleNamespace(
        response=SimpleNamespace(send_message=AsyncMock()),
        original_response=AsyncMock(return_value=message),
    )
    payload = PanelMessagePayload(embed=discord.Embed(title="公开角色面板"), view=view)

    await responder.send_message(interaction, payload=payload, visibility=PanelVisibility.PUBLIC)
    await view.on_timeout()

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is False
    message.delete.assert_awaited_once()
    message.edit.assert_not_awaited()
