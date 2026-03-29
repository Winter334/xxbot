"""阶段 10 PVP 公开播报测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.character.panel_query_service import (
    CharacterPanelBattleProjection,
    CharacterPanelOverview,
    CharacterPanelSkillDisplay,
)
from application.pvp.panel_service import PvpBattleReportDigest, PvpPanelSnapshot, PvpRecentSettlementSnapshot
from application.pvp.pvp_service import PvpChallengeResult, PvpHubSnapshot, PvpTargetListSnapshot
import infrastructure.discord.pvp_panel as pvp_panel_module
from infrastructure.discord.pvp_panel import PvpDisplayMode, PvpPanelController, PvpPublicSettlementPresenter

_NOW = datetime(2026, 3, 27, 20, 0, tzinfo=UTC)
_CYCLE_ANCHOR_DATE = date(2026, 3, 27)


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


@dataclass(slots=True)
class _RewardTier:
    reward_tier_id: str
    name: str
    rank_start: int
    rank_end: int
    order: int


def _build_overview(*, character_id: int = 1001, character_name: str = "青玄") -> CharacterPanelOverview:
    return CharacterPanelOverview(
        discord_user_id="30001",
        player_display_name="流云",
        character_id=character_id,
        character_name=character_name,
        character_title="问道者",
        badge_name=None,
        realm_id="mortal",
        realm_name="凡体",
        stage_id="middle",
        stage_name="中期",
        main_path_name="七杀剑诀",
        main_skill=CharacterPanelSkillDisplay(
            item_id=3001,
            skill_name="七杀剑诀",
            path_id="wenxin_sword",
            path_name="问心剑道",
            rank_name="一阶",
            quality_name="凡品",
            slot_id="main",
            skill_type="main",
        ),
        auxiliary_skills=(
            CharacterPanelSkillDisplay(
                item_id=3002,
                skill_name="金钟护元诀",
                path_id="wenxin_sword",
                path_name="问心剑道",
                rank_name="一阶",
                quality_name="凡品",
                slot_id="guard",
                skill_type="auxiliary",
            ),
            CharacterPanelSkillDisplay(
                item_id=3003,
                skill_name="风影步",
                path_id="wenxin_sword",
                path_name="问心剑道",
                rank_name="一阶",
                quality_name="凡品",
                slot_id="movement",
                skill_type="auxiliary",
            ),
            CharacterPanelSkillDisplay(
                item_id=3004,
                skill_name="锁念剑心诀",
                path_id="wenxin_sword",
                path_name="问心剑道",
                rank_name="一阶",
                quality_name="凡品",
                slot_id="spirit",
                skill_type="auxiliary",
            ),
        ),
        public_power_score=4321,
        battle_projection=CharacterPanelBattleProjection(
            behavior_template_id="wenxin_sword",
            max_hp=1000,
            current_hp=1000,
            max_resource=800,
            current_resource=800,
            attack_power=180,
            guard_power=120,
            speed=95,
            crit_rate_permille=150,
            crit_damage_bonus_permille=500,
            hit_rate_permille=900,
            dodge_rate_permille=80,
            control_bonus_permille=60,
            control_resist_permille=70,
            healing_power_permille=0,
            shield_power_permille=0,
            damage_bonus_permille=100,
            damage_reduction_permille=90,
            counter_rate_permille=20,
        ),
        spirit_stone=6422,
        current_cultivation_value=320,
        required_cultivation_value=1000,
        current_comprehension_value=40,
        required_comprehension_value=100,
        target_realm_name="筑基",
        equipment_slots=(),
        artifact_item=None,
    )


def _build_hub(*, character_id: int = 1001) -> PvpHubSnapshot:
    return PvpHubSnapshot(
        character_id=character_id,
        cycle_anchor_date=_CYCLE_ANCHOR_DATE,
        current_rank_position=20,
        current_best_rank=18,
        protected_until=None,
        remaining_challenge_count=3,
        honor_coin_balance=120,
        reward_preview={
            "summary": "青铜二阶奖励",
            "honor_coin_on_win": 12,
            "honor_coin_on_loss": 4,
            "display_items": [],
        },
        defense_snapshot_summary={
            "snapshot_version": 2,
            "realm_name": "凡体",
            "stage_name": "中期",
            "main_path_name": "问心剑道",
            "main_skill_name": "七杀剑诀",
            "public_power_score": 4321,
            "display_summary": "稳守问心剑势",
        },
        target_list=PvpTargetListSnapshot(
            character_id=character_id,
            cycle_anchor_date=_CYCLE_ANCHOR_DATE,
            generated_at=_NOW,
            current_rank_position=20,
            current_best_rank=18,
            targets=(),
            rejected_targets=(),
            fallback_triggered=False,
            expansion_steps_applied=(),
        ),
    )


def _build_battle_report_digest(*, focus_unit_name: str = "玄影分身") -> PvpBattleReportDigest:
    return PvpBattleReportDigest(
        battle_report_id=9101,
        result="ally_victory",
        completed_rounds=7,
        focus_unit_name=focus_unit_name,
        final_hp_ratio="0.7310",
        final_mp_ratio="0.2840",
        ally_damage_dealt=3200,
        ally_damage_taken=1870,
        ally_healing_done=140,
        successful_hits=9,
        critical_hits=2,
        control_skips=1,
    )


def _build_recent_settlement(
    *,
    battle_outcome: str = "ally_victory",
    rank_before_attacker: int = 20,
    rank_after_attacker: int = 18,
    rank_before_defender: int = 18,
    rank_after_defender: int = 20,
    honor_coin_delta: int = 12,
    honor_coin_balance_after: int | None = 120,
    anti_abuse_flags: tuple[str, ...] = (),
    display_rewards: tuple[dict[str, object], ...] = (),
    settlement_payload: dict[str, object] | None = None,
    battle_report_digest: PvpBattleReportDigest | None = None,
    defender_summary: dict[str, object] | None = None,
) -> PvpRecentSettlementSnapshot:
    return PvpRecentSettlementSnapshot(
        challenge_record_id=301,
        occurred_at=_NOW,
        cycle_anchor_date=_CYCLE_ANCHOR_DATE,
        attacker_character_id=1001,
        defender_character_id=2002,
        defender_snapshot_id=401,
        leaderboard_snapshot_id=501,
        battle_report_id=9101,
        battle_outcome=battle_outcome,
        rank_before_attacker=rank_before_attacker,
        rank_after_attacker=rank_after_attacker,
        rank_before_defender=rank_before_defender,
        rank_after_defender=rank_after_defender,
        rank_effect_applied=rank_before_attacker != rank_after_attacker,
        honor_coin_delta=honor_coin_delta,
        honor_coin_balance_after=honor_coin_balance_after,
        anti_abuse_flags=anti_abuse_flags,
        reward_preview={"summary": "青铜二阶奖励"},
        display_rewards=display_rewards,
        settlement_payload=settlement_payload
        or {
            "honor_coin": {
                "balance_after": honor_coin_balance_after,
                "components": [
                    {
                        "component_id": "base_win",
                        "summary": "胜场基础",
                        "applied_delta": honor_coin_delta,
                        "triggered": True,
                    }
                ],
            },
            "anti_abuse_flags": list(anti_abuse_flags),
            "hidden_score_before": 98765,
            "hidden_score_after": 98640,
            "private_note": "仅私有结算可见",
        },
        battle_report_digest=battle_report_digest or _build_battle_report_digest(),
        defender_summary=defender_summary
        or {
            "character_name": "寒川",
            "character_title": "逐月客",
            "realm_name": "凡体",
            "stage_name": "中期",
            "main_path_name": "寒泉诀",
            "public_power_score": 4280,
            "display_summary": "剑气内敛",
            "snapshot_version": 3,
        },
    )


def _build_snapshot(
    *,
    recent_settlement: PvpRecentSettlementSnapshot | None,
    character_name: str = "青玄",
    current_hidden_pvp_score: int = 98765,
    current_reward_tier_name: str | None = "青铜二阶",
    current_challenge_tier: str | None = "bronze_2",
) -> PvpPanelSnapshot:
    return PvpPanelSnapshot(
        overview=_build_overview(character_name=character_name),
        hub=_build_hub(),
        current_hidden_pvp_score=current_hidden_pvp_score,
        current_public_power_score=4321,
        current_challenge_tier=current_challenge_tier,
        current_reward_tier_name=current_reward_tier_name,
        current_entry_summary={"public_power_score": 4321},
        daily_challenge_limit=5,
        repeat_target_limit=2,
        recent_settlement=recent_settlement,
    )


def _build_reward_tiers() -> tuple[_RewardTier, ...]:
    return (
        _RewardTier(
            reward_tier_id="bronze_2",
            name="青铜二阶",
            rank_start=11,
            rank_end=30,
            order=3,
        ),
        _RewardTier(
            reward_tier_id="silver_1",
            name="白银一阶",
            rank_start=1,
            rank_end=10,
            order=2,
        ),
    )


def _build_embed_text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts.append(field.name)
        parts.append(field.value)
    return "\n".join(parts)


def _find_field_value(embed, field_name: str) -> str | None:
    for field in embed.fields:
        if field.name == field_name:
            return field.value
    return None


def _build_challenge_result() -> PvpChallengeResult:
    return PvpChallengeResult(
        attacker_character_id=1001,
        defender_character_id=2002,
        cycle_anchor_date=_CYCLE_ANCHOR_DATE,
        battle_outcome="ally_victory",
        battle_report_id=9101,
        leaderboard_snapshot_id=501,
        defender_snapshot_id=401,
        challenge_record_id=301,
        rank_before_attacker=20,
        rank_after_attacker=18,
        rank_before_defender=18,
        rank_after_defender=20,
        rank_effect_applied=True,
        honor_coin_delta=12,
        honor_coin_balance_after=120,
        anti_abuse_flags=(),
        reward_preview={"summary": "青铜二阶奖励"},
        display_rewards=(),
        settlement={"honor_coin": {"balance_after": 120}},
        environment_snapshot={},
    )


def _build_controller(monkeypatch: pytest.MonkeyPatch) -> PvpPanelController:
    monkeypatch.setattr(
        pvp_panel_module,
        "get_static_config",
        lambda: SimpleNamespace(pvp=SimpleNamespace(ordered_reward_tiers=_build_reward_tiers())),
    )
    return PvpPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: None,
    )


def _build_interaction(*, user_id: int = 30001) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        channel=SimpleNamespace(send=AsyncMock()),
    )


def test_public_highlight_broadcast_keeps_highlight_path_and_filters_frame_rewards() -> None:
    """命中高光条件时应继续走高光播报，并过滤 frame 类奖励。"""
    snapshot = _build_snapshot(
        recent_settlement=_build_recent_settlement(
            rank_before_attacker=18,
            rank_after_attacker=12,
            rank_before_defender=12,
            rank_after_defender=18,
            display_rewards=(
                {"reward_type": "badge", "name": "天榜新秀", "state": "unlocked_now"},
                {"reward_type": "frame", "name": "流光边框", "state": "unlocked_now"},
            ),
        )
    )

    embed = PvpPublicSettlementPresenter.build_embed(
        snapshot=snapshot,
        reward_tiers=_build_reward_tiers(),
    )

    assert embed is not None
    assert embed.title == "青玄｜仙榜论道高光播报"
    highlight_value = _find_field_value(embed, "高光结果")
    assert highlight_value is not None
    assert "排名显著上升：第 18 名 → 第 12 名" in highlight_value
    reward_value = _find_field_value(embed, "新获得展示奖励")
    assert reward_value is not None
    assert "徽记：天榜新秀（本次获得）" in reward_value
    assert "流光边框" not in reward_value


def test_public_normal_broadcast_for_small_rank_up_without_highlight_condition() -> None:
    """普通胜利且仅小幅提升名次时应走普通播报。"""
    snapshot = _build_snapshot(
        recent_settlement=_build_recent_settlement(
            rank_before_attacker=20,
            rank_after_attacker=18,
            rank_before_defender=18,
            rank_after_defender=20,
            display_rewards=({"reward_type": "frame", "name": "流光边框", "state": "unlocked_now"},),
        )
    )

    embed = PvpPublicSettlementPresenter.build_embed(
        snapshot=snapshot,
        reward_tiers=_build_reward_tiers(),
    )

    assert embed is not None
    assert embed.title == "青玄｜仙榜论道结果播报"
    assert _find_field_value(embed, "高光结果") is None
    assert _find_field_value(embed, "新获得展示奖励") is None
    assert "流光边框" not in _build_embed_text(embed)


@pytest.mark.parametrize(
    ("battle_outcome", "rank_before_attacker", "rank_after_attacker"),
    (
        ("enemy_victory", 20, 20),
        ("draw", 20, 20),
        ("ally_victory", 20, 20),
    ),
)
def test_public_broadcast_skipped_when_settlement_does_not_meet_public_conditions(
    battle_outcome: str,
    rank_before_attacker: int,
    rank_after_attacker: int,
) -> None:
    """失败、平局或胜利但名次未变化时不应公开播报。"""
    snapshot = _build_snapshot(
        recent_settlement=_build_recent_settlement(
            battle_outcome=battle_outcome,
            rank_before_attacker=rank_before_attacker,
            rank_after_attacker=rank_after_attacker,
            rank_before_defender=18,
            rank_after_defender=18,
            display_rewards=(),
        )
    )

    embed = PvpPublicSettlementPresenter.build_embed(
        snapshot=snapshot,
        reward_tiers=_build_reward_tiers(),
    )

    assert embed is None


def test_public_broadcast_omits_private_settlement_details_and_hidden_fields() -> None:
    """公开播报不应泄露私有结算细节、隐藏分与反滥用标记。"""
    snapshot = _build_snapshot(
        recent_settlement=_build_recent_settlement(
            anti_abuse_flags=("repeat_target_limit_reached", "rank_unchanged"),
            battle_report_digest=_build_battle_report_digest(focus_unit_name="幽冥替身"),
        ),
        current_hidden_pvp_score=98765,
    )

    embed = PvpPublicSettlementPresenter.build_embed(
        snapshot=snapshot,
        reward_tiers=_build_reward_tiers(),
    )

    assert embed is not None
    public_text = _build_embed_text(embed)
    assert "98765" not in public_text
    assert "repeat_target_limit_reached" not in public_text
    assert "本次后同目标次数达到上限" not in public_text
    assert "仅私有结算可见" not in public_text
    assert "幽冥替身" not in public_text
    assert "战报标识" not in public_text
    assert "结算标记" not in public_text
    assert "当前分数" not in public_text


@pytest.mark.asyncio
async def test_challenge_target_updates_private_settlement_before_public_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制器应先更新私有结算页，再尝试公开播报。"""
    controller = _build_controller(monkeypatch)
    interaction = _build_interaction()
    result = _build_challenge_result()
    snapshot = _build_snapshot(recent_settlement=_build_recent_settlement())
    call_order: list[str] = []

    controller._challenge_target = Mock(return_value=(result, snapshot))

    async def _record_edit(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("edit")

    async def _record_public(*args, **kwargs) -> None:
        del args, kwargs
        call_order.append("public")

    controller._edit_panel = AsyncMock(side_effect=_record_edit)
    controller._send_public_highlight_if_needed = AsyncMock(side_effect=_record_public)
    controller.responder.send_private_error = AsyncMock()

    await controller.challenge_target(
        interaction,
        character_id=1001,
        owner_user_id=30001,
        selected_target_character_id=2002,
    )

    controller._challenge_target.assert_called_once_with(character_id=1001, target_character_id=2002)
    controller._edit_panel.assert_awaited_once()
    controller._send_public_highlight_if_needed.assert_awaited_once_with(interaction, snapshot=snapshot)
    controller.responder.send_private_error.assert_not_awaited()
    assert call_order == ["edit", "public"]
    assert controller._edit_panel.await_args.kwargs["display_mode"] is PvpDisplayMode.SETTLEMENT
