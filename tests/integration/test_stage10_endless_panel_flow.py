"""阶段 10 无尽副本面板适配测试。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from application.character.panel_query_service import (
    CharacterPanelBattleProjection,
    CharacterPanelOverview,
    CharacterPanelSkillDisplay,
)
from application.dungeon import (
    EndlessRunAnchorSnapshot,
    EndlessRunRewardLedgerSnapshot,
    EndlessRunSettlementResult,
    EndlessRunStatusSnapshot,
    EndlessSettlementRewardSection,
)
from application.dungeon.endless_panel_service import (
    EndlessAdvancePresentation,
    EndlessBattleReportDigest,
    EndlessEnemyUnitDigest,
    EndlessFloorPanelSnapshot,
    EndlessPanelQueryService,
    EndlessPanelSnapshot,
    EndlessRecentSettlementSnapshot,
    EndlessRunPresentationSnapshot,
)
from domain.dungeon import EndlessRegionSnapshot
from infrastructure.config.static import load_static_config
from infrastructure.db.session import create_session_factory, session_scope
from infrastructure.discord.endless_panel import (
    EndlessDisplayMode,
    EndlessPanelController,
    EndlessPanelPresenter,
    EndlessPanelView,
    EndlessPublicSettlementPresenter,
)
from tests.integration.test_endless_service import (
    _build_services,
    _build_sqlite_url,
    _set_character_progress,
    _upgrade_database,
)

_NOW = datetime(2026, 3, 29, 23, 0, tzinfo=UTC)


@dataclass(slots=True)
class _OverviewStub:
    overview: CharacterPanelOverview

    def get_overview(self, *, character_id: int) -> CharacterPanelOverview:
        assert character_id == self.overview.character_id
        return self.overview


def _build_overview(*, character_id: int = 1001, character_name: str = "青玄") -> CharacterPanelOverview:
    return CharacterPanelOverview(
        discord_user_id="30001",
        player_display_name="流云",
        character_id=character_id,
        character_name=character_name,
        character_title="问道者",
        badge_name=None,
        realm_id="great_vehicle",
        realm_name="大乘",
        stage_id="perfect",
        stage_name="圆满",
        main_path_name="问心剑道",
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
        auxiliary_skills=(),
        public_power_score=4321,
        battle_projection=CharacterPanelBattleProjection(
            behavior_template_id="wenxin_sword",
            max_hp=1200,
            current_hp=960,
            max_resource=800,
            current_resource=520,
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
        target_realm_name="渡劫",
        equipment_slots=(),
        artifact_item=None,
    )


def _build_battle_digest() -> EndlessBattleReportDigest:
    return EndlessBattleReportDigest(
        battle_report_id=9101,
        result="ally_victory",
        completed_rounds=4,
        focus_unit_name="青玄",
        final_hp_ratio="0.7600",
        final_mp_ratio="0.4100",
        ally_damage_dealt=3200,
        ally_damage_taken=1180,
        ally_healing_done=140,
        successful_hits=9,
        critical_hits=2,
        control_skips=1,
        unit_defeated=2,
        action_highlights=("七杀剑诀×2", "风影步×1"),
        round_highlights=(
            "第 1 回合：出手 青玄·七杀剑诀｜暴击 1｜状态变化 1",
            "第 2 回合：出手 青玄·风影步｜击破 妖兽1号",
        ),
    )


def _build_floor_snapshot(
    *,
    floor: int,
    battle_outcome_label: str = "胜利",
    cumulative_progress: int = 12,
    claimable_drop_count: int = 1,
    battle_digest: EndlessBattleReportDigest | None = None,
) -> EndlessFloorPanelSnapshot:
    return EndlessFloorPanelSnapshot(
        floor=floor,
        node_type="elite" if floor % 5 == 0 else "normal",
        node_label="精英层" if floor % 5 == 0 else "常规层",
        region_id="wind",
        region_name="风域",
        region_theme="速度系敌人更多，主打先手压制。",
        race_id="spirit",
        race_name="灵体",
        race_profile="高闪避、偏术法。",
        template_id="swift",
        template_name="灵巧型",
        template_profile="高速度、偏闪避、低承伤。",
        enemy_count=2,
        realm_name="大乘",
        stage_name="圆满",
        style_tags=("精英层", "灵体", "灵巧型"),
        enemy_units=(
            EndlessEnemyUnitDigest(
                unit_name="灵体1号",
                realm_id="great_vehicle",
                stage_id="perfect",
                max_hp=980,
                attack_power=166,
                guard_power=120,
                speed=121,
                behavior_template_id="zhanqing_sword",
            ),
            EndlessEnemyUnitDigest(
                unit_name="灵体2号",
                realm_id="great_vehicle",
                stage_id="perfect",
                max_hp=1020,
                attack_power=158,
                guard_power=128,
                speed=118,
                behavior_template_id="zhanqing_sword",
            ),
        ),
        battle_outcome="ally_victory" if battle_outcome_label == "胜利" else "enemy_victory",
        battle_outcome_label=battle_outcome_label,
        reward_granted=battle_outcome_label == "胜利",
        battle_report_id=None if battle_digest is None else battle_digest.battle_report_id,
        stable_reward_summary={"cultivation": 1200, "insight": 2, "refining_essence": 1},
        pending_reward_summary={"drop_progress": 4},
        drop_progress_gained=4,
        cumulative_drop_progress=cumulative_progress,
        claimable_drop_count=claimable_drop_count,
        current_hp_ratio="0.7600",
        current_mp_ratio="0.4100",
        battle_report_digest=battle_digest,
    )


def _build_panel_snapshot(*, phase: str) -> EndlessPanelSnapshot:
    overview = _build_overview()
    battle_digest = _build_battle_digest()
    latest_floor = _build_floor_snapshot(floor=5, cumulative_progress=12, claimable_drop_count=1, battle_digest=battle_digest)
    preview_floor = _build_floor_snapshot(floor=6, cumulative_progress=12, claimable_drop_count=1, battle_digest=None)
    region = EndlessRegionSnapshot(
        region_index=1,
        region_id="wind",
        region_name="风域",
        region_bias_id="wind",
        start_floor=1,
        end_floor=20,
        theme_summary="速度系敌人更多，主打先手压制。",
    )
    if phase == "decision":
        status = "running"
        presentation = EndlessRunPresentationSnapshot(
            phase="decision",
            phase_label="第 5 层决策点",
            stopped_floor=5,
            decision_floor=5,
            next_floor=6,
            can_continue=True,
            can_settle_retreat=True,
            can_settle_defeat=False,
            battle_count=5,
            advanced_floor_count=5,
            pending_drop_progress=12,
            claimable_drop_count=1,
            latest_floor_result=latest_floor,
            recent_floor_results=(latest_floor,),
            upcoming_floor_preview=preview_floor,
        )
        run_status = EndlessRunStatusSnapshot(
            character_id=overview.character_id,
            has_active_run=True,
            status=status,
            selected_start_floor=1,
            current_floor=6,
            highest_floor_reached=6,
            current_node_type=None,
            current_region=region,
            anchor_status=EndlessRunAnchorSnapshot(
                highest_unlocked_anchor_floor=0,
                available_start_floors=(1,),
                selected_start_floor=1,
                selected_start_floor_unlocked=True,
                current_anchor_floor=0,
                next_anchor_floor=10,
            ),
            run_seed=20260329,
            reward_ledger=EndlessRunRewardLedgerSnapshot(
                stable_cultivation=6300,
                stable_insight=13,
                stable_refining_essence=14,
                pending_drop_progress=12,
                drop_count=1,
                last_reward_floor=5,
                drop_display=(),
                latest_node_result={"floor": 5, "reward_granted": True},
                advanced_floor_count=5,
                latest_anchor_unlock=None,
                encounter_history=(),
            ),
            encounter_history=(),
            started_at=_NOW.replace(tzinfo=None),
        )
    elif phase == "pending_defeat":
        failed_floor = _build_floor_snapshot(
            floor=9,
            battle_outcome_label="战败",
            cumulative_progress=8,
            claimable_drop_count=0,
            battle_digest=battle_digest,
        )
        status = "pending_defeat_settlement"
        presentation = EndlessRunPresentationSnapshot(
            phase="pending_defeat_settlement",
            phase_label="第 9 层战败待结算",
            stopped_floor=9,
            decision_floor=None,
            next_floor=None,
            can_continue=False,
            can_settle_retreat=False,
            can_settle_defeat=True,
            battle_count=4,
            advanced_floor_count=4,
            pending_drop_progress=8,
            claimable_drop_count=0,
            latest_floor_result=failed_floor,
            recent_floor_results=(failed_floor,),
            upcoming_floor_preview=None,
        )
        run_status = EndlessRunStatusSnapshot(
            character_id=overview.character_id,
            has_active_run=True,
            status=status,
            selected_start_floor=1,
            current_floor=9,
            highest_floor_reached=9,
            current_node_type=None,
            current_region=region,
            anchor_status=EndlessRunAnchorSnapshot(
                highest_unlocked_anchor_floor=0,
                available_start_floors=(1,),
                selected_start_floor=1,
                selected_start_floor_unlocked=True,
                current_anchor_floor=0,
                next_anchor_floor=10,
            ),
            run_seed=20260329,
            reward_ledger=EndlessRunRewardLedgerSnapshot(
                stable_cultivation=4800,
                stable_insight=10,
                stable_refining_essence=10,
                pending_drop_progress=8,
                drop_count=0,
                last_reward_floor=8,
                drop_display=(),
                latest_node_result={"floor": 9, "reward_granted": False},
                advanced_floor_count=4,
                latest_anchor_unlock=None,
                encounter_history=(),
            ),
            encounter_history=(),
            started_at=_NOW.replace(tzinfo=None),
        )
    else:
        non_decision_floor = _build_floor_snapshot(floor=2, cumulative_progress=4, claimable_drop_count=0, battle_digest=battle_digest)
        next_floor = _build_floor_snapshot(floor=3, cumulative_progress=4, claimable_drop_count=0, battle_digest=None)
        status = "running"
        presentation = EndlessRunPresentationSnapshot(
            phase="running",
            phase_label="待继续挑战",
            stopped_floor=2,
            decision_floor=None,
            next_floor=3,
            can_continue=True,
            can_settle_retreat=False,
            can_settle_defeat=False,
            battle_count=2,
            advanced_floor_count=2,
            pending_drop_progress=4,
            claimable_drop_count=0,
            latest_floor_result=non_decision_floor,
            recent_floor_results=(non_decision_floor,),
            upcoming_floor_preview=next_floor,
        )
        run_status = EndlessRunStatusSnapshot(
            character_id=overview.character_id,
            has_active_run=True,
            status=status,
            selected_start_floor=1,
            current_floor=3,
            highest_floor_reached=3,
            current_node_type=None,
            current_region=region,
            anchor_status=EndlessRunAnchorSnapshot(
                highest_unlocked_anchor_floor=0,
                available_start_floors=(1,),
                selected_start_floor=1,
                selected_start_floor_unlocked=True,
                current_anchor_floor=0,
                next_anchor_floor=10,
            ),
            run_seed=20260329,
            reward_ledger=EndlessRunRewardLedgerSnapshot(
                stable_cultivation=2400,
                stable_insight=4,
                stable_refining_essence=2,
                pending_drop_progress=4,
                drop_count=0,
                last_reward_floor=2,
                drop_display=(),
                latest_node_result={"floor": 2, "reward_granted": True},
                advanced_floor_count=2,
                latest_anchor_unlock=None,
                encounter_history=(),
            ),
            encounter_history=(),
            started_at=_NOW.replace(tzinfo=None),
        )
    return EndlessPanelSnapshot(
        overview=overview,
        run_status=run_status,
        run_presentation=presentation,
        recent_settlement=None,
    )


def _flatten_embed(embed) -> str:
    parts = [embed.title or "", embed.description or "", embed.footer.text or ""]
    for field in embed.fields:
        parts.append(field.name)
        parts.append(field.value)
    return "\n".join(parts)


def test_endless_panel_query_service_maps_auto_advance_results_and_enemy_battle_digests(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """查询层应映射统一掉落进度、多层推进结果、敌人信息与战斗摘要。"""
    database_url = _build_sqlite_url(tmp_path / "endless_panel_query.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        (
            growth_service,
            endless_service,
            _,
            state_repository,
            battle_record_repository,
            naming_batch_service,
            _,
            _,
        ) = _build_services(session, static_config)
        created = growth_service.create_character(
            discord_user_id="32031",
            player_display_name="照川",
            character_name="临渊",
        )
        _set_character_progress(
            character_repository=endless_service._character_repository,  # type: ignore[attr-defined]
            character_id=created.character_id,
            realm_id="great_vehicle",
            stage_id="perfect",
        )
        overview = _build_overview(character_id=created.character_id, character_name="临渊")
        panel_query_service = EndlessPanelQueryService(
            character_panel_query_service=_OverviewStub(overview=overview),
            endless_dungeon_service=endless_service,
            state_repository=state_repository,
            battle_record_repository=battle_record_repository,
            naming_batch_service=naming_batch_service,
            static_config=static_config,
        )

        endless_service.start_run(
            character_id=created.character_id,
            selected_start_floor=1,
            seed=20260329,
            now=_NOW.replace(tzinfo=None),
        )
        result = endless_service.advance_next_floor(character_id=created.character_id)
        advance_presentation = panel_query_service.build_advance_presentation(
            character_id=created.character_id,
            result=result,
            overview=overview,
        )
        snapshot = panel_query_service.get_panel_snapshot(character_id=created.character_id)

        assert snapshot.run_presentation.decision_floor == 5
        assert snapshot.run_presentation.can_settle_retreat is True
        assert snapshot.run_presentation.pending_drop_progress == result.run_status.reward_ledger.pending_drop_progress
        assert len(snapshot.run_presentation.recent_floor_results) == 5
        latest_floor = snapshot.run_presentation.latest_floor_result
        assert latest_floor is not None
        assert latest_floor.template_name != ""
        assert latest_floor.race_name != ""
        assert len(latest_floor.enemy_units) >= 1
        assert latest_floor.battle_report_digest is not None
        assert latest_floor.battle_report_digest.action_highlights or latest_floor.battle_report_digest.round_highlights
        preview = snapshot.run_presentation.upcoming_floor_preview
        assert preview is not None
        assert preview.floor == 6
        assert preview.battle_outcome is None

        assert [item.floor for item in advance_presentation.floor_results] == [1, 2, 3, 4, 5]
        assert any(item.battle_report_digest is not None for item in advance_presentation.floor_results)
        assert advance_presentation.pending_drop_progress == snapshot.run_presentation.pending_drop_progress
        assert advance_presentation.claimable_drop_count == snapshot.run_presentation.claimable_drop_count


@pytest.mark.asyncio
async def test_endless_panel_view_hides_retreat_outside_decision_point() -> None:
    """非 5/10 层决策点不应展示结算撤离。"""
    snapshot = _build_panel_snapshot(phase="running")
    view = EndlessPanelView(
        controller=SimpleNamespace(),
        owner_user_id=30001,
        character_id=snapshot.overview.character_id,
        snapshot=snapshot,
        selected_start_floor=1,
        display_mode=EndlessDisplayMode.HUB,
    )

    child_labels = [item.label for item in view.children if hasattr(item, "label") and item.label is not None]

    assert "继续挑战" in child_labels
    assert "结算撤离" not in child_labels
    assert "主动撤离" not in child_labels
    assert view.advance_next_floor.disabled is False
    assert view.start_run.disabled is True


@pytest.mark.asyncio
async def test_endless_panel_view_shows_continue_and_retreat_at_decision_point() -> None:
    """第 5/10 层决策点应同时展示继续挑战与结算撤离。"""
    snapshot = _build_panel_snapshot(phase="decision")
    view = EndlessPanelView(
        controller=SimpleNamespace(),
        owner_user_id=30001,
        character_id=snapshot.overview.character_id,
        snapshot=snapshot,
        selected_start_floor=1,
        display_mode=EndlessDisplayMode.HUB,
    )

    child_labels = [item.label for item in view.children if hasattr(item, "label") and item.label is not None]

    assert "继续挑战" in child_labels
    assert "结算撤离" in child_labels
    assert "主动撤离" not in child_labels
    assert view.advance_next_floor.disabled is False
    assert view.settle_retreat.disabled is False


@pytest.mark.asyncio
async def test_endless_panel_view_shows_defeat_settlement_only_after_failure() -> None:
    """战败后只应展示战败结算，不应允许继续挑战或结算撤离。"""
    snapshot = _build_panel_snapshot(phase="pending_defeat")
    view = EndlessPanelView(
        controller=SimpleNamespace(),
        owner_user_id=30001,
        character_id=snapshot.overview.character_id,
        snapshot=snapshot,
        selected_start_floor=1,
        display_mode=EndlessDisplayMode.HUB,
    )

    child_labels = [item.label for item in view.children if hasattr(item, "label") and item.label is not None]

    assert "结算撤离" not in child_labels
    assert "战败结算" in child_labels
    assert view.advance_next_floor.disabled is True
    assert view.settle_defeat.disabled is False


def test_endless_hub_embed_uses_unified_drop_progress_and_no_legacy_score_terms() -> None:
    """主面板文案应使用统一掉落进度语义，并去除旧分数与锚点主文案。"""
    snapshot = _build_panel_snapshot(phase="decision")

    embed = EndlessPanelPresenter.build_hub_embed(snapshot=snapshot, selected_start_floor=1)
    text = _flatten_embed(embed)

    assert "统一掉落进度" in text
    assert "可结算掉落" in text
    assert "下一战敌阵" in text
    assert "灵巧型" in text
    assert "装备分" not in text
    assert "法宝分" not in text
    assert "功法分" not in text
    assert "推进一层" not in text
    assert "主动撤离" not in text
    assert "锚点" not in text


def test_endless_public_settlement_embed_uses_source_progress_for_highlight_drops() -> None:
    """公开高光播报应依赖统一掉落进度，而不是旧分数字段。"""
    base_snapshot = _build_panel_snapshot(phase="decision")
    settlement = EndlessRunSettlementResult(
        character_id=base_snapshot.overview.character_id,
        settlement_type="retreat",
        terminated_floor=25,
        current_region=base_snapshot.run_status.current_region,
        stable_rewards=EndlessSettlementRewardSection(
            original={"cultivation": 6300, "insight": 13, "refining_essence": 14},
            deducted={"cultivation": 0, "insight": 0, "refining_essence": 0},
            settled={"cultivation": 6300, "insight": 13, "refining_essence": 14},
        ),
        pending_rewards=EndlessSettlementRewardSection(
            original={"drop_progress": 20},
            deducted={"drop_progress": 0},
            settled={"drop_progress": 20},
        ),
        final_drop_list=(
            {
                "entry_type": "equipment_drop",
                "display_name": "太虚战铠",
                "quality_id": "epic",
                "quality_name": "史诗",
                "is_artifact": False,
                "source_progress": 20,
                "source_score": 0,
            },
            {
                "entry_type": "artifact_drop",
                "display_name": "星辉镜",
                "quality_id": "rare",
                "quality_name": "稀有",
                "is_artifact": True,
                "source_progress": 20,
                "source_score": 0,
            },
            {
                "entry_type": "equipment_drop",
                "display_name": "旧分残留剑",
                "quality_id": "legendary",
                "quality_name": "传说",
                "is_artifact": False,
                "source_progress": 0,
                "source_score": 999,
            },
            {
                "entry_type": "equipment_drop",
                "display_name": "凡铁短刃",
                "quality_id": "common",
                "quality_name": "普通",
                "is_artifact": False,
                "source_progress": 20,
                "source_score": 999,
            },
        ),
        accounting_completed=True,
        can_repeat_read=True,
        settled_at=_NOW,
    )
    snapshot = replace(
        base_snapshot,
        recent_settlement=EndlessRecentSettlementSnapshot(
            settlement_result=settlement,
            selected_start_floor=1,
            advanced_floor_count=5,
            record_floor_before_run=20,
            last_floor_result=base_snapshot.run_presentation.latest_floor_result,
        ),
    )

    embed = EndlessPublicSettlementPresenter.build_embed(snapshot=snapshot)

    assert embed is not None
    text = _flatten_embed(embed)
    assert "高价值掉落" in text
    assert "太虚战铠｜史诗" in text
    assert "星辉镜｜稀有" in text
    assert "旧分残留剑" not in text
    assert "凡铁短刃" not in text


def test_endless_advance_lines_show_multi_floor_results_and_battle_process() -> None:
    """自动推进回执应体现多层结果、敌人信息与战斗过程信号。"""
    battle_digest = _build_battle_digest()
    advance_presentation = EndlessAdvancePresentation(
        stopped_reason="decision",
        stopped_reason_label="抵达决策点",
        stopped_floor=5,
        decision_floor=5,
        next_floor=6,
        can_settle_retreat=True,
        pending_drop_progress=12,
        claimable_drop_count=1,
        floor_results=(
            _build_floor_snapshot(floor=4, cumulative_progress=8, claimable_drop_count=0, battle_digest=battle_digest),
            _build_floor_snapshot(floor=5, cumulative_progress=12, claimable_drop_count=1, battle_digest=battle_digest),
        ),
    )

    lines = EndlessPanelController._build_advance_lines(advance_presentation=advance_presentation)
    text = "\n".join(lines)

    assert "自动推进：第 4-5 层" in text
    assert "统一掉落进度" in text
    assert "灵体·灵巧型×2" in text
    assert "关键技能：" in text or "第 1 回合：" in text
    assert "可继续挑战或结算撤离" in text
