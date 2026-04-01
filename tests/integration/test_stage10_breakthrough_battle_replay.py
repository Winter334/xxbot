"""突破三问叩关行记链路测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from infrastructure.discord.breakthrough_panel import BreakthroughPanelController


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
async def test_start_trial_updates_qualification_page_then_replay_then_result_message() -> None:
    """开始突破试炼后，应先刷新问玄关页，再播叩关行记，最后补发文学化结果。"""
    interaction = SimpleNamespace()
    presentation = SimpleNamespace(battle_report_id=9101)
    snapshot = SimpleNamespace(
        overview=SimpleNamespace(character_id=1001),
        recent_trial=SimpleNamespace(
            battle_report_id=9101,
            battle_replay_presentation=presentation,
        ),
    )
    result = SimpleNamespace(mapping_id="mortal_to_qi_refining")
    controller = BreakthroughPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: None,
        replay_message_player=SimpleNamespace(play=AsyncMock()),
    )
    controller.responder.edit_message = AsyncMock()
    controller.responder.send_private_error = AsyncMock()
    controller._build_qualification_payload = Mock(return_value=SimpleNamespace())
    controller._challenge_trial = Mock(return_value=(result, snapshot))
    call_order: list[str] = []

    async def _record_replay(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("replay")

    async def _record_result(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("result")

    async def _record_edit(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("edit")

    controller._play_trial_journey_if_available = AsyncMock(side_effect=_record_replay)
    controller._send_trial_result_message = AsyncMock(side_effect=_record_result)
    controller.responder.edit_message = AsyncMock(side_effect=_record_edit)

    await controller.start_trial(
        interaction,
        character_id=1001,
        owner_user_id=30001,
        mapping_id="mortal_to_qi_refining",
    )

    controller._challenge_trial.assert_called_once_with(
        character_id=1001,
        mapping_id="mortal_to_qi_refining",
    )
    controller._play_trial_journey_if_available.assert_awaited_once_with(interaction, snapshot=snapshot)
    controller._send_trial_result_message.assert_awaited_once_with(
        interaction,
        snapshot=snapshot,
        result=result,
    )
    controller.responder.send_private_error.assert_not_awaited()
    assert call_order == ["edit", "replay", "result"]


@pytest.mark.asyncio
async def test_start_material_trial_updates_material_page_then_replay_then_result_message() -> None:
    """开始材料秘境后，应先刷新采材页，再播采材行记，最后补发采材结果。"""
    interaction = SimpleNamespace()
    result = SimpleNamespace(
        replay_presentation=SimpleNamespace(battle_report_id=9201),
        character_id=1001,
        battle_report_id=9201,
        mapping_id="mortal_to_qi_refining",
    )
    snapshot = SimpleNamespace()
    controller = BreakthroughPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: None,
        replay_message_player=SimpleNamespace(play=AsyncMock()),
    )
    controller.responder.edit_message = AsyncMock()
    controller.responder.send_private_error = AsyncMock()
    controller._build_material_payload = Mock(return_value=SimpleNamespace())
    controller._challenge_material_trial = Mock(return_value=(result, snapshot))
    call_order: list[str] = []

    async def _record_replay(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("replay")

    async def _record_result(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("result")

    async def _record_edit(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("edit")

    controller._play_material_journey_if_available = AsyncMock(side_effect=_record_replay)
    controller._send_material_result_message = AsyncMock(side_effect=_record_result)
    controller.responder.edit_message = AsyncMock(side_effect=_record_edit)

    await controller.start_material_trial(
        interaction,
        character_id=1001,
        owner_user_id=30001,
        mapping_id="mortal_to_qi_refining",
    )

    controller._challenge_material_trial.assert_called_once_with(
        character_id=1001,
        mapping_id="mortal_to_qi_refining",
    )
    controller._play_material_journey_if_available.assert_awaited_once_with(interaction, result=result)
    controller._send_material_result_message.assert_awaited_once_with(
        interaction,
        snapshot=snapshot,
        result=result,
    )
    controller.responder.send_private_error.assert_not_awaited()
    assert call_order == ["edit", "replay", "result"]


@pytest.mark.asyncio
async def test_start_trial_keeps_main_flow_when_replay_send_fails() -> None:
    """叩关行记发送失败时，应静默降级且仍发送结果消息。"""
    interaction = SimpleNamespace()
    presentation = SimpleNamespace(battle_report_id=9101)
    controller = BreakthroughPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: None,
        replay_message_player=SimpleNamespace(
            play=AsyncMock(
                side_effect=discord.HTTPException(
                    response=SimpleNamespace(status=500, reason="err"),
                    message="boom",
                )
            )
        ),
    )
    snapshot = SimpleNamespace(
        overview=SimpleNamespace(character_id=1001),
        recent_trial=SimpleNamespace(
            battle_report_id=9101,
            battle_replay_presentation=presentation,
        ),
    )

    await controller._play_trial_journey_if_available(interaction, snapshot=snapshot)

    controller._battle_replay_message_player.play.assert_awaited_once_with(
        interaction,
        presentation=presentation,
    )


@pytest.mark.asyncio
async def test_start_material_trial_keeps_main_flow_when_replay_send_fails() -> None:
    """采材行记发送失败时，应静默降级且仍保留主流程。"""
    interaction = SimpleNamespace()
    controller = BreakthroughPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: None,
        replay_message_player=SimpleNamespace(
            play=AsyncMock(
                side_effect=discord.HTTPException(
                    response=SimpleNamespace(status=500, reason="err"),
                    message="boom",
                )
            )
        ),
    )
    result = SimpleNamespace(
        replay_presentation=SimpleNamespace(battle_report_id=9201),
        character_id=1001,
        battle_report_id=9201,
        mapping_id="mortal_to_qi_refining",
    )

    await controller._play_material_journey_if_available(interaction, result=result)

    controller._battle_replay_message_player.play.assert_awaited_once_with(
        interaction,
        presentation=result.replay_presentation,
    )


@pytest.mark.asyncio
async def test_execute_breakthrough_refreshes_root_then_sends_result_message() -> None:
    """正式叩天门后，应先回写根页，再发送独立结果消息。"""
    interaction = SimpleNamespace()
    action_result = SimpleNamespace(snapshot=SimpleNamespace(), execution_result=SimpleNamespace())
    controller = BreakthroughPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: None,
    )
    controller._execute_breakthrough = Mock(return_value=action_result)
    controller._build_root_payload = Mock(return_value=SimpleNamespace())
    controller.responder.edit_message = AsyncMock()
    controller.responder.send_private_error = AsyncMock()
    call_order: list[str] = []

    async def _record_edit(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("edit")

    async def _record_result(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("result")

    controller.responder.edit_message = AsyncMock(side_effect=_record_edit)
    controller._send_execution_result_message = AsyncMock(side_effect=_record_result)

    await controller.execute_breakthrough(
        interaction,
        character_id=1001,
        owner_user_id=30001,
    )

    controller._execute_breakthrough.assert_called_once_with(character_id=1001)
    controller._send_execution_result_message.assert_awaited_once_with(
        interaction,
        snapshot=action_result.snapshot,
        action_result=action_result,
    )
    controller.responder.send_private_error.assert_not_awaited()
    assert call_order == ["edit", "result"]
