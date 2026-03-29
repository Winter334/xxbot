"""阶段 10 角色主面板展示测试。"""

from __future__ import annotations

from application.character.panel_query_service import (
    CharacterPanelBattleProjection,
    CharacterPanelEquipmentDisplay,
    CharacterPanelEquipmentSlotDisplay,
    CharacterPanelOverview,
    CharacterPanelSkillDisplay,
)
from infrastructure.discord.character_panel import CharacterPanelPresenter


def _build_overview() -> CharacterPanelOverview:
    return CharacterPanelOverview(
        discord_user_id="30001",
        player_display_name="流云",
        character_id=1001,
        character_name="墨尘",
        character_title="问道者",
        badge_name="天榜新秀",
        realm_id="foundation",
        realm_name="筑基",
        stage_id="late",
        stage_name="后期",
        main_path_name="太虚剑经",
        main_skill=CharacterPanelSkillDisplay(
            item_id=3101,
            skill_name="太虚剑经",
            path_id="taixu_sword",
            path_name="太虚剑道",
            rank_name="三阶",
            quality_name="上品",
            slot_id="main",
            skill_type="main",
        ),
        auxiliary_skills=(
            CharacterPanelSkillDisplay(
                item_id=3102,
                skill_name="玄甲护心诀",
                path_id="taixu_sword",
                path_name="太虚剑道",
                rank_name="三阶",
                quality_name="上品",
                slot_id="guard",
                skill_type="auxiliary",
            ),
            CharacterPanelSkillDisplay(
                item_id=3103,
                skill_name="流云踏月步",
                path_id="taixu_sword",
                path_name="太虚剑道",
                rank_name="三阶",
                quality_name="上品",
                slot_id="movement",
                skill_type="auxiliary",
            ),
            CharacterPanelSkillDisplay(
                item_id=3104,
                skill_name="剑心照神诀",
                path_id="taixu_sword",
                path_name="太虚剑道",
                rank_name="三阶",
                quality_name="上品",
                slot_id="spirit",
                skill_type="auxiliary",
            ),
        ),
        public_power_score=18540,
        battle_projection=CharacterPanelBattleProjection(
            behavior_template_id="taixu_sword",
            max_hp=58590,
            current_hp=58590,
            max_resource=56313,
            current_resource=56313,
            attack_power=14058,
            guard_power=13046,
            speed=2814,
            crit_rate_permille=65,
            crit_damage_bonus_permille=3000,
            hit_rate_permille=14889,
            dodge_rate_permille=14964,
            control_bonus_permille=45,
            control_resist_permille=146,
            healing_power_permille=0,
            shield_power_permille=0,
            damage_bonus_permille=378,
            damage_reduction_permille=1946,
            counter_rate_permille=71,
        ),
        spirit_stone=6422,
        current_cultivation_value=3743,
        required_cultivation_value=5000,
        current_comprehension_value=260,
        required_comprehension_value=400,
        target_realm_name="金丹",
        equipment_slots=(
            CharacterPanelEquipmentSlotDisplay(
                slot_id="weapon",
                slot_name="武器",
                item=CharacterPanelEquipmentDisplay(
                    slot_id="weapon",
                    slot_name="武器",
                    display_name="后天·寒星断岳剑",
                    quality_name="一品",
                    rank_name="四阶",
                    enhancement_level=15,
                    artifact_nurture_level=0,
                    is_artifact=False,
                    resonance_name=None,
                    primary_stats=("攻力 14058", "暴伤 300.0%"),
                    affix_summary=("破军(天阶) 12.0%",),
                ),
            ),
            CharacterPanelEquipmentSlotDisplay(
                slot_id="armor",
                slot_name="护甲",
                item=CharacterPanelEquipmentDisplay(
                    slot_id="armor",
                    slot_name="护甲",
                    display_name="危境回春云纹袍",
                    quality_name="传说",
                    rank_name="三阶",
                    enhancement_level=0,
                    artifact_nurture_level=0,
                    is_artifact=False,
                    resonance_name=None,
                    primary_stats=("护体 13046", "气血 58590"),
                    affix_summary=("镇岳(天阶) 19.4%",),
                ),
            ),
            CharacterPanelEquipmentSlotDisplay(
                slot_id="accessory",
                slot_name="饰品",
                item=CharacterPanelEquipmentDisplay(
                    slot_id="accessory",
                    slot_name="饰品",
                    display_name="后天·踏霄灵佩",
                    quality_name="一品",
                    rank_name="四阶",
                    enhancement_level=11,
                    artifact_nurture_level=0,
                    is_artifact=False,
                    resonance_name=None,
                    primary_stats=("迅捷 2814", "命中 1488.9%"),
                    affix_summary=("追影(地阶) 6.5%",),
                ),
            ),
        ),
        artifact_item=CharacterPanelEquipmentDisplay(
            slot_id="artifact",
            slot_name="法宝",
            display_name="雷契返雷令",
            quality_name="至宝",
            rank_name="三阶",
            enhancement_level=0,
            artifact_nurture_level=0,
            is_artifact=True,
            resonance_name="雷契",
            primary_stats=("气血 12000", "护盾 45.0%"),
            affix_summary=("星辉(天阶) 22.0%", "御空(地阶) 8.0%"),
        ),
    )


def test_public_home_embed_uses_simpler_sections_and_progress_bar() -> None:
    embed = CharacterPanelPresenter.build_public_home_embed(
        overview=_build_overview(),
        discord_display_name="流云",
        avatar_url=None,
    )

    field_names = [field.name for field in embed.fields]
    assert "身份" not in field_names
    assert "功法" in field_names
    assert "装备 / 法宝" in field_names
    assert "修行进度" in field_names

    skill_field = next(field for field in embed.fields if field.name == "功法")
    equipment_field = next(field for field in embed.fields if field.name == "装备 / 法宝")
    core_field = next(field for field in embed.fields if field.name == "核心状态")
    stats_field = next(field for field in embed.fields if field.name == "基础属性")
    progress_field = next(field for field in embed.fields if field.name == "修行进度")

    assert "主修：太虚剑经" in skill_field.value
    assert "护体：玄甲护心诀" in skill_field.value
    assert "身法：流云踏月步" in skill_field.value
    assert "灵技：剑心照神诀" in skill_field.value

    assert "危境回春云纹袍｜强化 +0" in equipment_field.value
    assert "雷契返雷令｜强化 +0" in equipment_field.value
    assert "词条" not in equipment_field.value
    assert "属性" not in equipment_field.value

    assert "气血：58590/58590" in core_field.value
    assert "灵力：56313/56313" in core_field.value
    assert "攻力" not in core_field.value

    assert "气血" not in stats_field.value
    assert "灵力" not in stats_field.value
    assert "攻力：14058" in stats_field.value
    assert stats_field.value.startswith("```text\n")

    assert "目标境界：金丹" in progress_field.value
    assert "修为：" in progress_field.value
    assert "感悟：" in progress_field.value
    assert "█" in progress_field.value


def test_private_detail_embed_keeps_extended_stats_and_progress_bar() -> None:
    embed = CharacterPanelPresenter.build_private_detail_embed(
        overview=_build_overview(),
        discord_display_name="流云",
        avatar_url=None,
    )

    field_names = [field.name for field in embed.fields]
    assert "身份" not in field_names
    assert "扩展属性" in field_names
    assert "修行进度" in field_names

    extended_field = next(field for field in embed.fields if field.name == "扩展属性")
    progress_field = next(field for field in embed.fields if field.name == "修行进度")
    assert "穿透：37.8%" in extended_field.value
    assert "减伤：194.6%" in extended_field.value
    assert "控势：4.5%" in extended_field.value
    assert "3743/5000" in progress_field.value
    assert "260/400" in progress_field.value
