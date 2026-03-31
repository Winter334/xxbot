"""阶段 10 突破秘境战斗回放链路测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from infrastructure.discord.breakthrough_panel import BreakthroughDisplayMode, BreakthroughPanelController


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


@pytest.mark.asyncio
async def test_start_trial_keeps_main_flow_when_replay_data_missing() -> None:
    """最近结算缺少回放数据时，不应影响主结算页刷新与后续公开播报。"""
    replay_player = SimpleNamespace(play=AsyncMock())
    controller = BreakthroughPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: None,
        replay_message_player=replay_player,
    )
    controller.responder.send_private_error = AsyncMock()
    controller._build_challenge_lines = Mock(return_value=("试炼已毕",))
    result = SimpleNamespace(mapping_id="foundation_to_core")
    snapshot = SimpleNamespace(
        overview=SimpleNamespace(character_id=1001),
        recent_settlement=SimpleNamespace(battle_replay_presentation=None),
    )
    controller._challenge_trial = Mock(return_value=(result, snapshot))
    call_order: list[str] = []

    async def _record_edit(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("edit")

    async def _record_public(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("public")

    controller._edit_panel = AsyncMock(side_effect=_record_edit)
    controller._send_public_highlight_if_needed = AsyncMock(side_effect=_record_public)

    await controller.start_trial(
        SimpleNamespace(),
        character_id=1001,
        owner_user_id=30001,
        selected_mapping_id="foundation_to_core",
    )

    replay_player.play.assert_not_awaited()
    controller._edit_panel.assert_awaited_once()
    controller._send_public_highlight_if_needed.assert_awaited_once_with(SimpleNamespace(), snapshot=snapshot)
    controller.responder.send_private_error.assert_not_awaited()
    assert call_order == ["edit", "public"]
    assert controller._edit_panel.await_args.kwargs["display_mode"] is BreakthroughDisplayMode.SETTLEMENT


@pytest.mark.asyncio
async def test_start_trial_keeps_main_flow_when_replay_send_fails() -> None:
    """独立回放消息发送失败时，应静默降级且不阻断主链路。"""
    interaction = SimpleNamespace()
    presentation = SimpleNamespace(battle_report_id=9101)
    replay_player = SimpleNamespace(
        play=AsyncMock(
            side_effect=discord.HTTPException(
                response=SimpleNamespace(status=500, reason="err"),
                message="boom",
            )
        )
    )
    controller = BreakthroughPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: None,
        replay_message_player=replay_player,
    )
    controller.responder.send_private_error = AsyncMock()
    controller._build_challenge_lines = Mock(return_value=("试炼已毕",))
    result = SimpleNamespace(mapping_id="foundation_to_core")
    snapshot = SimpleNamespace(
        overview=SimpleNamespace(character_id=1001),
        recent_settlement=SimpleNamespace(battle_replay_presentation=presentation),
    )
    controller._challenge_trial = Mock(return_value=(result, snapshot))
    call_order: list[str] = []

    async def _record_edit(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("edit")

    async def _record_public(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("public")

    controller._edit_panel = AsyncMock(side_effect=_record_edit)
    controller._send_public_highlight_if_needed = AsyncMock(side_effect=_record_public)

    await controller.start_trial(
        interaction,
        character_id=1001,
        owner_user_id=30001,
        selected_mapping_id="foundation_to_core",
    )

    replay_player.play.assert_awaited_once_with(interaction, presentation=presentation)
    controller._edit_panel.assert_awaited_once()
    controller._send_public_highlight_if_needed.assert_awaited_once_with(interaction, snapshot=snapshot)
    controller.responder.send_private_error.assert_not_awaited()
    assert call_order == ["edit", "public"]
    assert controller._edit_panel.await_args.kwargs["display_mode"] is BreakthroughDisplayMode.SETTLEMENT
