"""阶段 10 角色创建入口测试。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.character import CharacterAlreadyExistsError, DiscordCharacterBindingNotFoundError
from infrastructure.discord.character_panel import (
    CharacterOpenPublicHomeView,
    CharacterPanelController,
    CharacterPanelPresenter,
    CharacterCreationGuideView,
    PanelVisibility,
)


@dataclass(slots=True)
class _CreatedCharacterSnapshot:
    character_name: str
    character_title: str | None


class _GrowthServiceStub:
    def __init__(self, *, create_result: _CreatedCharacterSnapshot | None = None, create_exception: Exception | None = None) -> None:
        self.create_result = create_result or _CreatedCharacterSnapshot(character_name="青玄", character_title="问道者")
        self.create_exception = create_exception
        self.create_calls: list[dict[str, str | None]] = []

    def create_character(
        self,
        *,
        discord_user_id: str,
        player_display_name: str,
        character_name: str,
        title: str | None,
    ) -> _CreatedCharacterSnapshot:
        self.create_calls.append(
            {
                "discord_user_id": discord_user_id,
                "player_display_name": player_display_name,
                "character_name": character_name,
                "title": title,
            }
        )
        if self.create_exception is not None:
            raise self.create_exception
        return self.create_result


class _CharacterQueryServiceStub:
    def __init__(self, *, overview: object | None = None, lookup_exception: Exception | None = None) -> None:
        self.overview = overview or object()
        self.lookup_exception = lookup_exception
        self.lookup_calls: list[str] = []

    def get_overview_by_discord_user_id(self, *, discord_user_id: str):
        self.lookup_calls.append(discord_user_id)
        if self.lookup_exception is not None:
            raise self.lookup_exception
        return self.overview


@dataclass(slots=True)
class _ServiceBundle:
    character_panel_query_service: _CharacterQueryServiceStub
    character_growth_service: _GrowthServiceStub


class _DummySession:
    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


class _DummySessionFactory:
    def __call__(self) -> _DummySession:
        return _DummySession()


def _build_controller(
    *,
    growth_service: _GrowthServiceStub | None = None,
    query_service: _CharacterQueryServiceStub | None = None,
) -> CharacterPanelController:
    service_bundle = _ServiceBundle(
        character_panel_query_service=query_service or _CharacterQueryServiceStub(),
        character_growth_service=growth_service or _GrowthServiceStub(),
    )
    return CharacterPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: service_bundle,
    )


def _build_interaction(*, user_id: int = 30001, display_name: str = "流云") -> SimpleNamespace:
    user = SimpleNamespace(
        id=user_id,
        display_name=display_name,
        name=display_name,
        display_avatar=SimpleNamespace(url="https://example.com/avatar.png"),
    )
    response = SimpleNamespace(send_modal=AsyncMock())
    interaction = SimpleNamespace(user=user, response=response)
    return interaction


@pytest.mark.asyncio
async def test_open_public_home_without_character_sends_private_creation_guide() -> None:
    """公开入口在无角色时应返回私有创建引导，而不是自动建号。"""
    growth_service = _GrowthServiceStub()
    query_service = _CharacterQueryServiceStub(lookup_exception=DiscordCharacterBindingNotFoundError("未创建角色"))
    controller = _build_controller(growth_service=growth_service, query_service=query_service)
    interaction = _build_interaction()
    controller.responder.send_message = AsyncMock()
    controller.responder.send_private_error = AsyncMock()

    await controller.open_public_home(interaction)

    assert growth_service.create_calls == []
    controller.responder.send_private_error.assert_not_awaited()
    controller.responder.send_message.assert_awaited_once()
    _, kwargs = controller.responder.send_message.await_args
    payload = kwargs["payload"]
    assert kwargs["visibility"] is PanelVisibility.PRIVATE
    assert payload.embed.title == CharacterPanelPresenter.build_creation_guide_embed().title
    assert payload.embed.description == CharacterPanelPresenter.build_creation_guide_embed().description
    assert isinstance(payload.view, CharacterCreationGuideView)


@pytest.mark.asyncio
async def test_submit_character_creation_calls_growth_service_create_character() -> None:
    """显式提交流程应调用角色成长服务完成建号。"""
    growth_service = _GrowthServiceStub(
        create_result=_CreatedCharacterSnapshot(character_name="青玄", character_title="问道者")
    )
    controller = _build_controller(growth_service=growth_service)
    interaction = _build_interaction(user_id=30002, display_name="星河")
    controller.responder.send_message = AsyncMock()
    controller.responder.send_private_error = AsyncMock()

    await controller.submit_character_creation(
        interaction,
        character_name="  青玄  ",
        title="  问道者  ",
    )

    assert growth_service.create_calls == [
        {
            "discord_user_id": "30002",
            "player_display_name": "星河",
            "character_name": "青玄",
            "title": "问道者",
        }
    ]
    controller.responder.send_private_error.assert_not_awaited()
    controller.responder.send_message.assert_awaited_once()
    _, kwargs = controller.responder.send_message.await_args
    payload = kwargs["payload"]
    assert kwargs["visibility"] is PanelVisibility.PRIVATE
    assert payload.embed.title == "角色创建完成"
    assert isinstance(payload.view, CharacterOpenPublicHomeView)


@pytest.mark.asyncio
async def test_submit_character_creation_when_character_exists_returns_private_prompt_with_public_home_entry() -> None:
    """重复创建命中已有角色时应返回私有提示，并保留进入公开面板入口。"""
    growth_service = _GrowthServiceStub(create_exception=CharacterAlreadyExistsError("角色已存在"))
    controller = _build_controller(growth_service=growth_service)
    interaction = _build_interaction(user_id=30003, display_name="归舟")
    controller.responder.send_message = AsyncMock()
    controller.responder.send_private_error = AsyncMock()

    await controller.submit_character_creation(
        interaction,
        character_name="归舟",
        title="",
    )

    assert growth_service.create_calls == [
        {
            "discord_user_id": "30003",
            "player_display_name": "归舟",
            "character_name": "归舟",
            "title": None,
        }
    ]
    controller.responder.send_private_error.assert_not_awaited()
    controller.responder.send_message.assert_awaited_once()
    _, kwargs = controller.responder.send_message.await_args
    payload = kwargs["payload"]
    assert kwargs["visibility"] is PanelVisibility.PRIVATE
    assert payload.embed.title == CharacterPanelPresenter.build_existing_character_embed().title
    assert payload.embed.description == CharacterPanelPresenter.build_existing_character_embed().description
    assert isinstance(payload.view, CharacterOpenPublicHomeView)
