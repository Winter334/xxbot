"""阶段 10 PVP / 突破私有面板展示测试。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from application.breakthrough.panel_service import (
    BreakthroughGapCard,
    BreakthroughGoalCard,
    BreakthroughPanelSnapshot,
    BreakthroughRecentResultCard,
    BreakthroughRecentSettlementSnapshot,
    BreakthroughStatusCard,
    BreakthroughTrialCard,
)
from application.breakthrough.reward_service import BreakthroughRewardApplicationResult
from application.breakthrough.trial_service import (
    BreakthroughTrialEntrySnapshot,
    BreakthroughTrialGroupSnapshot,
    BreakthroughTrialHubSnapshot,
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
from infrastructure.discord.breakthrough_panel import BreakthroughActionNote, BreakthroughPanelPresenter
from infrastructure.discord.pvp_panel import PvpPanelPresenter

_NOW = datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
_CYCLE_ANCHOR_DATE = date(2026, 3, 30)


@dataclass(frozen=True, slots=True)
class _DummyBattleDigest:
    battle_report_id: int = 1
    result: str = "ally_victory"
    completed_rounds: int = 6
    focus_unit_name: str = "青玄"
    final_hp_ratio: str = "0.7000"
    final_mp_ratio: str = "0.3500"
    ally_damage_dealt: int = 3000
    ally_damage_taken: int = 1800
    ally_healing_done: int = 0
    successful_hits: int = 8
    critical_hits: int = 2
    control_skips: int = 1


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
        battle_report_digest=_DummyBattleDigest(),
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
    settlement = BreakthroughRewardApplicationResult(
        settlement_type="first_clear",
        victory=True,
        qualification_granted=True,
        progress_status="cleared",
        attempt_count=1,
        cleared_count=1,
        reward_payload={"items": [{"reward_kind": "qualification"}]},
        settlement_payload={},
        soft_limit_snapshot=None,
        currency_changes={"spirit_stone": 1200},
        item_changes=({"item_id": "artifact_essence", "quantity": 1},),
        battle_report_id=None,
        drop_record_id=None,
        source_ref="breakthrough_trial:foundation_to_core",
    )
    goal_card = BreakthroughGoalCard(
        current_realm_name="凡体",
        current_stage_name="圆满",
        target_realm_name="筑基",
        qualification_obtained=False,
        can_breakthrough=False,
    )
    trial_card = BreakthroughTrialCard(
        mapping_id="foundation_to_core",
        trial_name="凝元试锋",
        group_name="根基试炼",
        target_realm_name="筑基",
        environment_rule="剑势压制",
        can_challenge=True,
        is_cleared=False,
        attempt_count=0,
        cleared_count=0,
    )
    status_card = BreakthroughStatusCard(
        current_realm_name="凡体",
        current_stage_name="圆满",
        current_hp_ratio="0.8125",
        current_mp_ratio="0.4375",
        qualification_obtained=False,
        current_cultivation_value=960,
        required_cultivation_value=1000,
        current_comprehension_value=80,
        required_comprehension_value=100,
    )
    gap_card = BreakthroughGapCard(
        passed=False,
        lines=("修为还差 40", "感悟还差 20", "缺少突破资格"),
    )
    recent_result_card = BreakthroughRecentResultCard(
        trial_name="凝元试锋",
        result_label="首次通关",
        qualification_changed=True,
        reward_summary="突破资格｜灵石 +1200｜法宝精粹 +1",
        occurred_at=_NOW,
    )
    recent_settlement = BreakthroughRecentSettlementSnapshot(
        mapping_id="foundation_to_core",
        trial_name="凝元试锋",
        group_id="foundation",
        group_name="根基试炼",
        occurred_at=_NOW,
        settlement=settlement,
        goal_card=goal_card,
        trial_card=trial_card,
        status_card=status_card,
        gap_card=gap_card,
        recent_result_card=recent_result_card,
        battle_report_digest=_DummyBattleDigest(),
    )
    return BreakthroughPanelSnapshot(
        overview=_build_overview(),
        precheck=BreakthroughPrecheckResult(
            character_id=1001,
            current_realm_id="mortal",
            current_realm_name="凡体",
            target_realm_id="foundation",
            target_realm_name="筑基",
            mapping_id="foundation_to_core",
            passed=False,
            current_cultivation_value=960,
            required_cultivation_value=1000,
            current_comprehension_value=80,
            required_comprehension_value=100,
            qualification_obtained=False,
            gaps=(
                BreakthroughPrecheckGap(gap_type="cultivation_insufficient", missing_value=40),
                BreakthroughPrecheckGap(gap_type="comprehension_insufficient", missing_value=20),
                BreakthroughPrecheckGap(gap_type="qualification_missing"),
            ),
        ),
        hub=BreakthroughTrialHubSnapshot(
            character_id=1001,
            current_realm_id="mortal",
            current_stage_id="perfect",
            qualification_obtained=False,
            current_hp_ratio="0.8125",
            current_mp_ratio="0.4375",
            current_trial_mapping_id="foundation_to_core",
            current_trial=None,
            repeatable_trials=(),
            cleared_mapping_ids=(),
            groups=(),
        ),
        goal_card=goal_card,
        current_trial_card=trial_card,
        status_card=status_card,
        gap_card=gap_card,
        recent_result_card=recent_result_card,
        recent_settlement=recent_settlement,
    )


def _build_trial_entry(
    *,
    mapping_id: str,
    trial_name: str,
    group_id: str,
    to_realm_id: str,
    environment_rule: str,
    can_challenge: bool,
    is_cleared: bool,
    is_current_trial: bool,
    attempt_count: int,
    cleared_count: int,
) -> BreakthroughTrialEntrySnapshot:
    return BreakthroughTrialEntrySnapshot(
        mapping_id=mapping_id,
        trial_name=trial_name,
        group_id=group_id,
        from_realm_id="mortal",
        to_realm_id=to_realm_id,
        environment_rule=environment_rule,
        environment_rule_id=f"{mapping_id}_env",
        repeat_reward_direction="spirit_stone",
        boss_template_id=f"boss_{mapping_id}",
        boss_stage_id="middle",
        boss_scale_permille=1000,
        first_clear_grants_qualification=False,
        can_challenge=can_challenge,
        is_cleared=is_cleared,
        is_current_trial=is_current_trial,
        attempt_count=attempt_count,
        cleared_count=cleared_count,
        best_clear_at=None,
        first_cleared_at=None,
        last_cleared_at=None,
        qualification_granted_at=None,
        last_reward_direction=None,
    )



def _build_breakthrough_snapshot_with_trial_choices() -> BreakthroughPanelSnapshot:
    snapshot = _build_breakthrough_snapshot()
    current_trial = _build_trial_entry(
        mapping_id="foundation_to_core",
        trial_name="凝元试锋",
        group_id="foundation",
        to_realm_id="foundation",
        environment_rule="剑势压制",
        can_challenge=True,
        is_cleared=False,
        is_current_trial=True,
        attempt_count=0,
        cleared_count=0,
    )
    selected_trial = _build_trial_entry(
        mapping_id="legacy_trial",
        trial_name="回风旧关",
        group_id="legacy_trials",
        to_realm_id="legacy_target",
        environment_rule="回风阵",
        can_challenge=True,
        is_cleared=True,
        is_current_trial=False,
        attempt_count=3,
        cleared_count=1,
    )
    hub = BreakthroughTrialHubSnapshot(
        character_id=snapshot.hub.character_id,
        current_realm_id=snapshot.hub.current_realm_id,
        current_stage_id=snapshot.hub.current_stage_id,
        qualification_obtained=snapshot.hub.qualification_obtained,
        current_hp_ratio=snapshot.hub.current_hp_ratio,
        current_mp_ratio=snapshot.hub.current_mp_ratio,
        current_trial_mapping_id=current_trial.mapping_id,
        current_trial=current_trial,
        repeatable_trials=(selected_trial,),
        cleared_mapping_ids=(selected_trial.mapping_id,),
        groups=(
            BreakthroughTrialGroupSnapshot(
                group_id="foundation",
                group_name="根基试炼",
                theme_summary="当前主线突破",
                reward_focus_summary="资格突破",
                trials=(current_trial,),
            ),
            BreakthroughTrialGroupSnapshot(
                group_id="legacy_trials",
                group_name="旧关复盘",
                theme_summary="复盘已通关试炼",
                reward_focus_summary="低密度查看",
                trials=(selected_trial,),
            ),
        ),
    )
    return replace(snapshot, hub=hub)



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


def test_breakthrough_hub_embed_focuses_goal_trial_status_gap_and_recent_result() -> None:
    snapshot = _build_breakthrough_snapshot()

    embed = BreakthroughPanelPresenter.build_hub_embed(snapshot=snapshot, selected_mapping_id="foundation_to_core")
    text = _flatten_embed(embed)
    field_names = [field.name for field in embed.fields]

    assert field_names == ["🌠 目标突破", "⚔ 当前试炼", "🧍 我方状态", "📌 缺口", "🏁 最近结果"]
    assert "分组概览" not in field_names
    assert "突破资格与前置" not in field_names
    assert "当前突破状态" not in field_names
    assert "奖励方向" not in text
    assert "主题：" not in text
    assert "软限制" not in text
    assert "```text" in text


def test_breakthrough_settlement_embed_keeps_same_five_cards_and_short_gap_message() -> None:
    snapshot = _build_breakthrough_snapshot()

    embed = BreakthroughPanelPresenter.build_settlement_embed(snapshot=snapshot, selected_mapping_id="foundation_to_core")
    text = _flatten_embed(embed)
    field_names = [field.name for field in embed.fields]

    assert field_names == ["🌠 目标突破", "⚔ 当前试炼", "🧍 我方状态", "📌 缺口", "🏁 最近结果"]
    assert "结算概览" not in field_names
    assert "资格与前置检查" not in field_names
    assert "奖励与资源变化" not in field_names
    assert "当前状态变化" not in field_names
    assert "关键战报摘要" not in field_names
    assert "分组概览" not in text
    assert "奖励方向" not in text



def test_breakthrough_hub_embed_shows_precheck_feedback_in_description() -> None:
    snapshot = _build_breakthrough_snapshot()

    embed = BreakthroughPanelPresenter.build_hub_embed(
        snapshot=snapshot,
        selected_mapping_id="foundation_to_core",
        action_note=BreakthroughActionNote(
            title="突破资格与前置",
            lines=(
                "前置判定：仍有缺口",
                "突破资格：尚未具备",
                "缺口：修为还差 40；感悟还差 20；缺少突破资格",
            ),
        ),
    )

    assert embed.description is not None
    assert "【突破资格与前置】" in embed.description
    assert "前置判定：仍有缺口" in embed.description
    assert "缺口：修为还差 40；感悟还差 20；缺少突破资格" in embed.description
    assert [field.name for field in embed.fields] == ["🌠 目标突破", "⚔ 当前试炼", "🧍 我方状态", "📌 缺口", "🏁 最近结果"]



def test_breakthrough_hub_embed_reflects_selected_trial_in_body_and_footer() -> None:
    snapshot = _build_breakthrough_snapshot_with_trial_choices()

    embed = BreakthroughPanelPresenter.build_hub_embed(snapshot=snapshot, selected_mapping_id="legacy_trial")
    trial_block = next(field.value for field in embed.fields if field.name == "⚔ 当前试炼")

    assert "回风旧关｜旧关复盘" in trial_block
    assert "试炼景象：回风阵" in trial_block
    assert embed.footer.text is not None
    assert "当前查看：回风旧关｜旧关复盘" in embed.footer.text
    assert "已通关" in embed.footer.text
    assert "可挑战" in embed.footer.text



def test_breakthrough_hub_embed_shows_execution_feedback_without_breaking_layout() -> None:
    snapshot = _build_breakthrough_snapshot()

    embed = BreakthroughPanelPresenter.build_hub_embed(
        snapshot=snapshot,
        selected_mapping_id="foundation_to_core",
        action_note=BreakthroughActionNote(
            title="突破完成",
            lines=(
                "境界：凡体 → 筑基",
                "当前结论：已踏入 筑基·初期",
                "资格状态：已消耗",
            ),
        ),
    )

    assert embed.description is not None
    assert "【突破完成】" in embed.description
    assert "当前结论：已踏入 筑基·初期" in embed.description
    assert "资格状态：已消耗" in embed.description
    assert [field.name for field in embed.fields] == ["🌠 目标突破", "⚔ 当前试炼", "🧍 我方状态", "📌 缺口", "🏁 最近结果"]
