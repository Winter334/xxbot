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
                    display_name="后天·玄岳法衣",
                    quality_name="一品",
                    rank_name="四阶",
                    enhancement_level=12,
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
            display_name="后天·九霄星辰图",
            quality_name="一品",
            rank_name="四阶",
            enhancement_level=0,
            artifact_nurture_level=3,
            is_artifact=True,
            resonance_name="星海共鸣",
            primary_stats=("气血 12000", "护盾 45.0%"),
            affix_summary=("星辉(天阶) 22.0%", "御空(地阶) 8.0%"),
        ),
    )


def test_public_home_embed_removes_action_text_and_shows_equipment_summary() -> None:
    embed = CharacterPanelPresenter.build_public_home_embed(
        overview=_build_overview(),
        discord_display_name="流云",
        avatar_url=None,
    )

    field_names = [field.name for field in embed.fields]
    assert "操作" not in field_names
    assert "装备概览" in field_names
    assert "本命法宝" in field_names

    equipment_field = next(field for field in embed.fields if field.name == "装备概览")
    artifact_field = next(field for field in embed.fields if field.name == "本命法宝")
    stats_field = next(field for field in embed.fields if field.name == "基础属性")

    assert "⚔ 武器" in equipment_field.value
    assert "🛡 护甲" in equipment_field.value
    assert "🧿 饰品" in equipment_field.value
    assert "后天·寒星断岳剑" in equipment_field.value
    assert "祭炼 3" in artifact_field.value
    assert "星海共鸣" in artifact_field.value
    assert stats_field.value.startswith("```text\n")
    assert embed.footer.text == "公开展示｜实际操作入口请使用下方按钮"


def test_private_detail_embed_shows_extended_stats_and_artifact_summary() -> None:
    embed = CharacterPanelPresenter.build_private_detail_embed(
        overview=_build_overview(),
        discord_display_name="流云",
        avatar_url=None,
    )

    field_names = [field.name for field in embed.fields]
    assert "扩展属性" in field_names
    assert "装备概览" in field_names
    assert "本命法宝" in field_names

    extended_field = next(field for field in embed.fields if field.name == "扩展属性")
    assert "穿透 37.8%" in extended_field.value
    assert "减伤 194.6%" in extended_field.value
    assert "控势 4.5%" in extended_field.value
