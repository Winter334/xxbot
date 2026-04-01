"""突破三问面板与 PVP 面板展示测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

from application.battle import BattleReplayFrame, BattleReplayPresentation
from application.breakthrough.panel_service import (
    BreakthroughMaterialPageSnapshot,
    BreakthroughMaterialRequirementSnapshot,
    BreakthroughPanelSnapshot,
    BreakthroughQualificationPageSnapshot,
    BreakthroughRecentTrialSnapshot,
    BreakthroughRootStatus,
)
from application.character.panel_query_service import (
    CharacterPanelBattleProjection,
    CharacterPanelOverview,
    CharacterPanelSkillDisplay,
)
from application.character.progression_service import BreakthroughPrecheckGap, BreakthroughPrecheckResult
from application.pvp.panel_service import (
    PvpOpponentCard,
    PvpPanelSnapshot,
    PvpRecentResultCard,
    PvpRecentSettlementSnapshot,
    PvpRewardCard,
    PvpStatusCard,
)
from application.pvp.pvp_service import PvpHubSnapshot, PvpTargetListSnapshot
from infrastructure.discord.breakthrough_panel import BreakthroughActionResult, BreakthroughPanelPresenter
from infrastructure.discord.pvp_panel import PvpPanelPresenter

_NOW = datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
_CYCLE_ANCHOR_DATE = date(2026, 3, 30)


class _DummyExecutionResult:
    from_realm_name = "凡体"
    to_realm_name = "炼气"
    new_stage_name = "初期"
    consumed_items = ()



def _build_overview(*, character_name: str = "青玄") -> CharacterPanelOverview:
    return CharacterPanelOverview(
        discord_user_id="30001",
        player_display_name="流云",
        character_id=1001,
        character_name=character_name,
        character_title="问道者",
        badge_name=None,
        realm_id="mortal",
        realm_name="凡体",
        stage_id="middle",
        stage_name="中期",
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
        spirit_stone=5000,
        current_cultivation_value=320,
        required_cultivation_value=1000,
        current_comprehension_value=40,
        required_comprehension_value=100,
        target_realm_name="筑基",
        equipment_slots=(),
        artifact_item=None,
    )



def _flatten_embed(embed) -> str:
    parts = [embed.title or "", embed.description or "", embed.footer.text or ""]
    for field in embed.fields:
        parts.append(field.name)
        parts.append(field.value)
    return "\n".join(parts)



def _build_pvp_snapshot() -> PvpPanelSnapshot:
    reward_card = PvpRewardCard(
        tier_name="白银一阶",
        summary="白银一阶奖励",
        honor_coin_on_win=18,
        honor_coin_on_loss=6,
        visible_reward_lines=("徽记｜天榜新秀（本次）",),
    )
    opponent_card = PvpOpponentCard(
        character_id=2002,
        character_name="寒川",
        character_title="逐月客",
        rank_position=18,
        realm_name="凡体",
        stage_name="中期",
        main_path_name="寒泉诀",
        public_power_score=4280,
        hidden_pvp_score=98640,
        rank_gap=-2,
        display_summary="剑气内敛",
        reward_card=reward_card,
    )
    recent_result_card = PvpRecentResultCard(
        opponent_name="寒川",
        outcome="ally_victory",
        occurred_at=_NOW,
        rank_before=20,
        rank_after=18,
        rank_shift=2,
        honor_coin_delta=12,
    )
    recent_settlement = PvpRecentSettlementSnapshot(
        challenge_record_id=301,
        occurred_at=_NOW,
        cycle_anchor_date=_CYCLE_ANCHOR_DATE,
        attacker_character_id=1001,
        defender_character_id=2002,
        defender_snapshot_id=401,
        leaderboard_snapshot_id=501,
        battle_report_id=601,
        battle_outcome="ally_victory",
        rank_before_attacker=20,
        rank_after_attacker=18,
        rank_before_defender=18,
        rank_after_defender=20,
        rank_effect_applied=True,
        honor_coin_delta=12,
        honor_coin_balance_after=120,
        anti_abuse_flags=("repeat_target_limit_reached",),
        reward_preview={"summary": "白银一阶奖励"},
        display_rewards=({"reward_type": "badge", "name": "天榜新秀", "state": "unlocked_now"},),
        reward_card=reward_card,
        opponent_card=opponent_card,
        recent_result_card=recent_result_card,
        settlement_payload={"honor_coin": {"balance_after": 120}},
        battle_report_digest=None,
        defender_summary={"character_name": "寒川"},
    )
    return PvpPanelSnapshot(
        overview=_build_overview(),
        hub=PvpHubSnapshot(
            character_id=1001,
            cycle_anchor_date=_CYCLE_ANCHOR_DATE,
            current_rank_position=20,
            current_best_rank=18,
            protected_until=None,
            remaining_challenge_count=3,
            honor_coin_balance=120,
            reward_preview={"summary": "青铜二阶奖励", "honor_coin_on_win": 12, "honor_coin_on_loss": 4, "display_items": []},
            defense_snapshot_summary={"display_summary": "稳守问心剑势"},
            target_list=PvpTargetListSnapshot(
                character_id=1001,
                cycle_anchor_date=_CYCLE_ANCHOR_DATE,
                generated_at=_NOW,
                current_rank_position=20,
                current_best_rank=18,
                targets=(),
                rejected_targets=(),
                fallback_triggered=False,
                expansion_steps_applied=(),
            ),
        ),
        current_hidden_pvp_score=98765,
        current_public_power_score=4321,
        current_challenge_tier="silver_1",
        current_reward_tier_name="白银一阶",
        current_entry_summary={"public_power_score": 4321},
        daily_challenge_limit=5,
        repeat_target_limit=2,
        status_card=PvpStatusCard(
            rank_position=20,
            best_rank=18,
            remaining_challenge_count=3,
            daily_challenge_limit=5,
            used_challenge_count=2,
            honor_coin_balance=120,
            public_power_score=4321,
            hidden_pvp_score=98765,
            reward_tier_name="白银一阶",
            protected_until=None,
        ),
        target_cards=(opponent_card,),
        recent_result_card=recent_result_card,
        recent_settlement=recent_settlement,
    )



def _build_breakthrough_snapshot() -> BreakthroughPanelSnapshot:
    return BreakthroughPanelSnapshot(
        overview=_build_overview(),
        precheck=BreakthroughPrecheckResult(
            character_id=1001,
            current_realm_id="mortal",
            current_realm_name="凡体",
            target_realm_id="qi_refining",
            target_realm_name="炼气",
            mapping_id="mortal_to_qi_refining",
            passed=False,
            current_cultivation_value=40,
            required_cultivation_value=50,
            current_comprehension_value=8,
            required_comprehension_value=10,
            qualification_obtained=False,
            gaps=(
                BreakthroughPrecheckGap(gap_type="cultivation_insufficient", missing_value=10),
                BreakthroughPrecheckGap(gap_type="comprehension_insufficient", missing_value=2),
                BreakthroughPrecheckGap(gap_type="qualification_missing", missing_value=1),
            ),
        ),
        root_status=BreakthroughRootStatus(
            current_realm_display="凡体·中期",
            next_realm_name="炼气",
            qualification_obtained=False,
            material_ready=False,
            can_breakthrough=False,
        ),
        qualification_page=BreakthroughQualificationPageSnapshot(
            mapping_id="mortal_to_qi_refining",
            trial_name="凡人破炼气",
            environment_rule="固定一条护体压制环境规则。",
            atmosphere_text="门前灵压早已成势，只等你亲自上前叩这一关。",
            passed=False,
            material_gap_text="凝气草 ×1",
            start_trial_enabled=True,
        ),
        material_page=BreakthroughMaterialPageSnapshot(
            mapping_id="mortal_to_qi_refining",
            current_realm_name="凡体",
            target_realm_name="炼气",
            material_trial_name="凝气草泽",
            atmosphere_text="薄雾伏在乱草之间，初生灵气沿着湿土回转。",
            requirements=(
                BreakthroughMaterialRequirementSnapshot(
                    item_type="material",
                    item_id="qi_condensation_grass",
                    item_name="凝气草",
                    required_quantity=2,
                    owned_quantity=1,
                    missing_quantity=1,
                ),
            ),
            all_satisfied=False,
            gap_summary="凝气草 ×1",
            start_trial_enabled=True,
        ),
        recent_trial=BreakthroughRecentTrialSnapshot(
            mapping_id="mortal_to_qi_refining",
            trial_name="凡人破炼气",
            occurred_at=_NOW,
            battle_report_id=9101,
            battle_replay_presentation=BattleReplayPresentation(
                battle_report_id=9101,
                result="ally_victory",
                focus_unit_name="青玄",
                summary_line="胜势已定｜4 回合｜余留气血 70.0%",
                highlight_lines=("⚔️ 你一剑逼退守关者。",),
                frames=(
                    BattleReplayFrame(
                        title="叩关行记｜凡人破炼气",
                        lines=("山门微震。",),
                        footer="战后回放｜胜势已定",
                        pause_seconds=0.0,
                    ),
                ),
            ),
        ),
    )



def _build_breakthrough_snapshot_ready() -> BreakthroughPanelSnapshot:
    snapshot = _build_breakthrough_snapshot()
    return replace(
        snapshot,
        precheck=replace(snapshot.precheck, passed=True, qualification_obtained=True, gaps=()),
        root_status=replace(
            snapshot.root_status,
            qualification_obtained=True,
            material_ready=True,
            can_breakthrough=True,
        ),
        qualification_page=replace(
            snapshot.qualification_page,
            passed=True,
            material_gap_text="已无缺漏",
            start_trial_enabled=False,
        ),
        material_page=BreakthroughMaterialPageSnapshot(
            mapping_id="mortal_to_qi_refining",
            current_realm_name="凡体",
            target_realm_name="炼气",
            material_trial_name="凝气草泽",
            atmosphere_text="薄雾伏在乱草之间，初生灵气沿着湿土回转。",
            requirements=(
                BreakthroughMaterialRequirementSnapshot(
                    item_type="material",
                    item_id="qi_condensation_grass",
                    item_name="凝气草",
                    required_quantity=2,
                    owned_quantity=2,
                    missing_quantity=0,
                ),
            ),
            all_satisfied=True,
            gap_summary="已无缺漏",
            start_trial_enabled=False,
        ),
    )



def test_pvp_hub_embed_focuses_core_cards_and_removes_report_sections() -> None:
    snapshot = _build_pvp_snapshot()

    embed = PvpPanelPresenter.build_hub_embed(snapshot=snapshot, selected_target_character_id=2002)
    text = _flatten_embed(embed)
    field_names = [field.name for field in embed.fields]

    assert field_names == ["🏆 我方", "🎯 当前对手", "🎁 本场奖励", "🏁 最近结果"]
    assert "防守摘要" not in field_names
    assert "当前不可论道目标摘要" not in field_names
    assert "可论道目标摘要" not in field_names
    assert "次数与荣誉" not in field_names
    assert "当前展示奖励" not in field_names
    assert "不可论道" not in text
    assert "候选扩窗" not in text
    assert "防守快照" not in text
    assert "```text" in text



def test_pvp_settlement_embed_keeps_core_cards_without_battle_report_sections() -> None:
    snapshot = _build_pvp_snapshot()

    embed = PvpPanelPresenter.build_settlement_embed(snapshot=snapshot, selected_target_character_id=2002)
    text = _flatten_embed(embed)
    field_names = [field.name for field in embed.fields]

    assert field_names == ["🏆 我方", "🎯 当前对手", "🎁 本场奖励", "🏁 最近结果"]
    assert "关键战报摘要" not in field_names
    assert "分数与荣誉变化" not in field_names
    assert "可见奖励与结算标记" not in field_names
    assert "荣誉币构成" not in text
    assert "聚焦角色" not in text
    assert "repeat_target_limit_reached" not in text



def test_breakthrough_root_embed_only_keeps_four_lines_and_new_entry_note() -> None:
    snapshot = _build_breakthrough_snapshot()

    embed = BreakthroughPanelPresenter.build_root_embed(snapshot=snapshot)
    text = _flatten_embed(embed)

    assert embed.title == "青玄｜破境三问"
    assert embed.fields == []
    assert "当前境界：凡体·中期 → 炼气" in text
    assert "资格状态：未得资格" in text
    assert "材料状态：仍缺若干" in text
    assert "破境状态：条件未满" in text
    assert "玄关与采材已各分一路，入门后自有去处。" in text
    assert "采破境灵材" in text
    assert "检灵材" not in text



def test_breakthrough_qualification_embed_keeps_only_required_sections() -> None:
    snapshot = _build_breakthrough_snapshot()

    embed = BreakthroughPanelPresenter.build_qualification_embed(snapshot=snapshot)
    text = _flatten_embed(embed)

    assert embed.title == "青玄｜问玄关"
    assert embed.fields == []
    assert "今番所问：凡人破炼气" in text
    assert "当前是否通过：未通过" in text
    assert "材料缺口：凝气草 ×1" in text
    assert "关前气象" not in text
    assert "不写杂项" in text



def test_breakthrough_material_embed_only_shows_material_trial_structure_and_items() -> None:
    snapshot = _build_breakthrough_snapshot()

    embed = BreakthroughPanelPresenter.build_material_embed(snapshot=snapshot)
    text = _flatten_embed(embed)

    assert embed.title == "青玄｜凝气草泽"
    assert [field.name for field in embed.fields] == ["本境可采灵材"]
    assert "当前境界：凡体 → 炼气" in text
    assert "秘境名：凝气草泽" in text
    assert "薄雾伏在乱草之间，初生灵气沿着湿土回转。" in text
    assert "凝气草 ×2" in text
    assert "持有" not in text
    assert "所需灵材" not in text



def test_breakthrough_material_embed_ready_state_still_only_lists_drops() -> None:
    snapshot = _build_breakthrough_snapshot_ready()

    embed = BreakthroughPanelPresenter.build_material_embed(snapshot=snapshot)
    text = _flatten_embed(embed)

    assert "凝气草 ×2" in text
    assert "持有" not in text
    assert "已齐" not in text



def test_breakthrough_material_result_embed_focuses_on_gains_and_remaining_gap() -> None:
    snapshot = _build_breakthrough_snapshot()
    result = SimpleNamespace(
        victory=True,
        trial_name="凝气草泽",
        drop_items=(
            SimpleNamespace(item_name="凝气草", quantity=1),
        ),
        all_satisfied_after=False,
        remaining_gap_summary="凝气草 ×1",
    )

    embed = BreakthroughPanelPresenter.build_material_result_embed(snapshot=snapshot, result=result)
    text = _flatten_embed(embed)

    assert embed.title == "青玄｜采境回响"
    assert "此行带回：凝气草 ×1。" in text
    assert "余下仍缺：凝气草 ×1。" in text
    assert "资格" not in text



def test_breakthrough_material_result_embed_reports_all_ready_when_materials_complete() -> None:
    snapshot = _build_breakthrough_snapshot_ready()
    result = SimpleNamespace(
        victory=True,
        trial_name="凝气草泽",
        drop_items=(
            SimpleNamespace(item_name="凝气草", quantity=1),
        ),
        all_satisfied_after=True,
        remaining_gap_summary="已无缺漏",
    )

    embed = BreakthroughPanelPresenter.build_material_result_embed(snapshot=snapshot, result=result)
    text = _flatten_embed(embed)

    assert "此行所缺灵材已齐" in text
    assert "余下仍缺" not in text



def test_breakthrough_trial_result_embed_uses_literary_result_message() -> None:
    snapshot = _build_breakthrough_snapshot_ready()
    result = type("TrialResult", (), {})()
    result.trial_name = "凡人破炼气"
    result.settlement = type("Settlement", (), {"victory": True, "qualification_granted": True})()

    embed = BreakthroughPanelPresenter.build_trial_result_embed(snapshot=snapshot, result=result)
    text = _flatten_embed(embed)

    assert embed.title == "青玄｜叩关余响"
    assert "突破资格已落掌中" in text
    assert "此地只证玄关，不赐材料机缘。" in text
    assert "结算" not in text



def test_breakthrough_execution_result_embed_reports_blocked_without_table() -> None:
    snapshot = _build_breakthrough_snapshot()
    action_result = BreakthroughActionResult(snapshot=snapshot, blocked_message="缺少突破资格")

    embed = BreakthroughPanelPresenter.build_execution_result_embed(
        snapshot=snapshot,
        action_result=action_result,
    )
    text = _flatten_embed(embed)

    assert embed.title == "青玄｜叩天门"
    assert "天门未应。" in text
    assert "缺少突破资格" in text
    assert "表" not in text



def test_breakthrough_execution_result_embed_reports_success_in_literary_style() -> None:
    snapshot = _build_breakthrough_snapshot_ready()
    action_result = BreakthroughActionResult(
        snapshot=snapshot,
        execution_result=_DummyExecutionResult(),
    )

    embed = BreakthroughPanelPresenter.build_execution_result_embed(
        snapshot=snapshot,
        action_result=action_result,
    )
    text = _flatten_embed(embed)

    assert "凡体旧壁已裂，炼气的新气终于落入经脉。" in text
    assert "你已踏入 炼气·初期。" in text
    assert "此身境路，自此另开一重。" in text
    assert "结算" not in text
