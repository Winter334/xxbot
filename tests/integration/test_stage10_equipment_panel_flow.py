"""阶段 10 装备 / 法宝 / 功法面板测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from alembic import command
from alembic.config import Config
import discord
import pytest

from application.character.profile_panel_query_service import (
    ProfilePanelQueryService,
    SkillPanelSkillSlotSnapshot,
    SkillPanelSnapshot,
)
from application.character.skill_drop_service import SkillDropService
from application.character.skill_loadout_service import SkillLoadoutService
from application.equipment.backpack_query_service import (
    BackpackCardSnapshot,
    BackpackEntryKey,
    BackpackEntryKind,
    BackpackEntrySummarySnapshot,
    BackpackFilterId,
    BackpackPanelSnapshot,
    BackpackSelectedDetailSnapshot,
)
from application.equipment.equipment_service import (
    EquipmentAffixSnapshot,
    EquipmentAttributeSnapshot,
    EquipmentCollectionSnapshot,
    EquipmentItemSnapshot,
    EquipmentNamingSnapshot,
    EquipmentResolvedStatSnapshot,
    EquipmentService,
    EquipmentUnequipTargetNotFoundError,
)
from application.equipment.forge_query_service import (
    ForgeCardSnapshot,
    ForgeFilterId,
    ForgeOperationCostSnapshot,
    ForgeOperationId,
    ForgeOperationPreviewSnapshot,
    ForgePanelQueryService,
    ForgePanelSnapshot,
    ForgeResourceEntrySnapshot,
    ForgeResourceSnapshot,
    ForgeTargetKind,
    ForgeTargetSnapshot,
)
from application.equipment.panel_query_service import (
    EquipmentCardSnapshot,
    EquipmentDropSummary,
    EquipmentPanelQueryService,
    EquipmentPanelSnapshot,
    EquipmentSlotPanelSnapshot,
)
from application.naming import ItemNamingBatchService
from application.ranking.score_service import CharacterScoreService
from infrastructure.config.static import clear_static_config_cache, load_static_config
from infrastructure.db.models import (
    Character,
    CharacterProgress,
    CharacterSkillLoadout,
    CurrencyBalance,
    DropRecord,
    InventoryItem,
    Player,
)
from infrastructure.db.repositories import (
    SqlAlchemyBattleRecordRepository,
    SqlAlchemyCharacterRepository,
    SqlAlchemyCharacterScoreSnapshotRepository,
    SqlAlchemyEquipmentRepository,
    SqlAlchemyInventoryRepository,
    SqlAlchemyPlayerRepository,
    SqlAlchemySkillRepository,
    SqlAlchemyStateRepository,
)
from infrastructure.db.session import create_session_factory, session_scope
from infrastructure.discord.backpack_panel import BackpackPanelPresenter, BackpackPanelState
from infrastructure.discord.character_panel import PanelMessagePayload
from infrastructure.discord.endless_panel import EndlessPanelPresenter
from infrastructure.discord.equipment_panel import (
    EquipmentPanelController,
    EquipmentPanelDisplayMode,
    EquipmentPanelPresenter,
    EquipmentPanelView,
)
from infrastructure.discord.forge_panel import ForgePanelPresenter, ForgePanelState

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class _ServiceBundle:
    character_panel_query_service: object | None
    equipment_panel_query_service: EquipmentPanelQueryService
    equipment_service: EquipmentService
    skill_loadout_service: SkillLoadoutService


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


def _build_sqlite_url(database_path: Path) -> str:
    """构造测试用 SQLite 地址。"""
    return f"sqlite+pysqlite:///{database_path.as_posix()}"



def _upgrade_database(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """将测试数据库升级到最新迁移版本。"""
    monkeypatch.chdir(PROJECT_ROOT)
    monkeypatch.setenv("DATABASE_URL", database_url)
    command.upgrade(Config((PROJECT_ROOT / "alembic.ini").as_posix()), "head")



def _create_services(session, *, static_config):
    player_repository = SqlAlchemyPlayerRepository(session)
    character_repository = SqlAlchemyCharacterRepository(session)
    state_repository = SqlAlchemyStateRepository(session)
    skill_repository = SqlAlchemySkillRepository(session)
    equipment_repository = SqlAlchemyEquipmentRepository(session)
    inventory_repository = SqlAlchemyInventoryRepository(session)
    score_snapshot_repository = SqlAlchemyCharacterScoreSnapshotRepository(session)
    battle_record_repository = SqlAlchemyBattleRecordRepository(session)
    score_service = CharacterScoreService(
        character_repository=character_repository,
        score_snapshot_repository=score_snapshot_repository,
        static_config=static_config,
    )
    equipment_service = EquipmentService(
        character_repository=character_repository,
        equipment_repository=equipment_repository,
        inventory_repository=inventory_repository,
        static_config=static_config,
        score_service=score_service,
    )
    skill_loadout_service = SkillLoadoutService(
        character_repository=character_repository,
        skill_repository=skill_repository,
        score_service=score_service,
        static_config=static_config,
    )
    skill_drop_service = SkillDropService(
        character_repository=character_repository,
        skill_repository=skill_repository,
        static_config=static_config,
    )
    naming_batch_service = ItemNamingBatchService(
        state_repository=state_repository,
        equipment_service=equipment_service,
        skill_repository=skill_repository,
        skill_runtime_support=skill_drop_service._skill_runtime_support,
        static_config=static_config,
    )
    profile_panel_query_service = ProfilePanelQueryService(
        character_repository=character_repository,
        skill_loadout_service=skill_loadout_service,
        static_config=static_config,
    )
    equipment_panel_query_service = EquipmentPanelQueryService(
        equipment_service=equipment_service,
        profile_panel_query_service=profile_panel_query_service,
        battle_record_repository=battle_record_repository,
        skill_repository=skill_repository,
        static_config=static_config,
        naming_batch_service=naming_batch_service,
    )
    forge_panel_query_service = ForgePanelQueryService(
        equipment_service=equipment_service,
        inventory_repository=inventory_repository,
        profile_panel_query_service=profile_panel_query_service,
        static_config=static_config,
    )
    return SimpleNamespace(
        player_repository=player_repository,
        character_repository=character_repository,
        state_repository=state_repository,
        skill_repository=skill_repository,
        equipment_repository=equipment_repository,
        inventory_repository=inventory_repository,
        score_snapshot_repository=score_snapshot_repository,
        battle_record_repository=battle_record_repository,
        score_service=score_service,
        equipment_service=equipment_service,
        profile_panel_query_service=profile_panel_query_service,
        equipment_panel_query_service=equipment_panel_query_service,
        forge_panel_query_service=forge_panel_query_service,
        naming_batch_service=naming_batch_service,
        skill_loadout_service=skill_loadout_service,
    )



def _create_character_with_skill_context(services, *, discord_user_id: str, character_name: str) -> int:
    player = services.player_repository.add(
        Player(
            discord_user_id=discord_user_id,
            display_name=f"{character_name}道友",
        )
    )
    character = services.character_repository.add(
        Character(
            player_id=player.id,
            name=character_name,
            title="测试者",
            total_power_score=0,
            public_power_score=0,
            hidden_pvp_score=0,
        )
    )
    progress = CharacterProgress(
        character_id=character.id,
        realm_id="mortal",
        stage_id="middle",
        cultivation_value=18,
        comprehension_value=9,
        current_hp_ratio=Decimal("1.0000"),
        current_mp_ratio=Decimal("1.0000"),
    )
    services.character_repository.save_progress(progress)
    services.character_repository.save_currency_balance(
        CurrencyBalance(
            character_id=character.id,
            spirit_stone=30000,
            honor_coin=0,
        )
    )
    services.character_repository.save_skill_loadout(
        CharacterSkillLoadout(
            character_id=character.id,
            main_axis_id="sword",
            main_path_id="wenxin_sword",
            behavior_template_id="wenxin_sword",
            body_method_id=None,
            movement_skill_id=None,
            spirit_skill_id=None,
            config_version=services.equipment_service._static_config.skill_paths.config_version,
            loadout_notes_json={},
        )
    )
    services.score_service.refresh_character_score(character_id=character.id)
    return character.id



def _seed_materials(services, *, character_id: int) -> None:
    for item_id, quantity in {
        "enhancement_stone": 8,
        "wash_jade": 4,
        "seal_talisman": 4,
        "reforge_crystal": 4,
        "artifact_essence": 10,
    }.items():
        services.inventory_repository.upsert_item(
            InventoryItem(
                character_id=character_id,
                item_type="material",
                item_id=item_id,
                quantity=quantity,
                item_payload_json={},
            )
        )



def _build_test_snapshot(*, include_artifact: bool, weapon_equipped: bool, skill_main_path_id: str = "wenxin_sword") -> EquipmentPanelSnapshot:
    slot_definitions = load_static_config().equipment.ordered_slots
    slot_name_by_id = {slot.slot_id: slot.name for slot in slot_definitions}

    weapon_item = _build_item_snapshot(
        item_id=1001,
        slot_id="weapon",
        slot_name=slot_name_by_id["weapon"],
        display_name="星陨剑",
        quality_name="史诗",
        equipped_slot_id="weapon" if weapon_equipped else None,
    )
    weapon_candidate = _build_item_snapshot(
        item_id=1002,
        slot_id="weapon",
        slot_name=slot_name_by_id["weapon"],
        display_name="试锋剑",
        quality_name="稀有",
        equipped_slot_id=None,
    )
    artifact_item = _build_item_snapshot(
        item_id=2001,
        slot_id="artifact",
        slot_name=slot_name_by_id["artifact"],
        display_name="天火镜",
        quality_name="史诗",
        equipped_slot_id="artifact" if include_artifact else None,
        is_artifact=True,
        artifact_nurture_level=2,
    )

    slot_panels = []
    for slot in slot_definitions:
        equipped_item = None
        candidate_items: tuple[EquipmentItemSnapshot, ...] = ()
        if slot.slot_id == "weapon":
            equipped_item = weapon_item if weapon_equipped else None
            candidate_items = (weapon_candidate,) if not weapon_equipped else (weapon_item, weapon_candidate)
        elif slot.slot_id == "artifact":
            equipped_item = artifact_item if include_artifact else None
            candidate_items = (artifact_item,) if include_artifact else ()
        equipped_card = None if equipped_item is None else _build_equipment_card_snapshot(equipped_item)
        candidate_cards = tuple(_build_equipment_card_snapshot(item) for item in candidate_items)
        slot_panels.append(
            EquipmentSlotPanelSnapshot(
                slot_id=slot.slot_id,
                slot_name=slot.name,
                core_role=slot.core_role,
                equipped_item=equipped_item,
                candidate_items=candidate_items,
                equipped_card=equipped_card,
                candidate_cards=candidate_cards,
            )
        )

    collection = EquipmentCollectionSnapshot(
        character_id=77,
        spirit_stone=6000,
        active_items=tuple(item for item in (weapon_item, weapon_candidate, artifact_item) if item.item_state == "active"),
        equipped_items=tuple(
            item for item in (weapon_item if weapon_equipped else None, artifact_item if include_artifact else None) if item is not None
        ),
        dismantled_items=(),
    )
    lineage_payload_by_path_id = {
        "wenxin_sword": {
            "main_skill_name": "七杀剑诀",
            "main_lineage_id": "seven_kill_sword",
            "path_name": "问心剑道",
            "guard": ("golden_bell_guard", "金钟护元诀"),
            "movement": ("wind_shadow_step", "风影步"),
            "spirit": ("sword_heart_lock", "锁念剑心诀"),
            "preferred_scene": "问道争锋、首领攻坚、破境试锋。",
            "main_patches": ("main_burst_damage_up",),
            "guard_patches": ("guard_damage_reduction",),
            "movement_patches": ("movement_speed_bonus",),
            "spirit_patches": ("spirit_crit_bonus",),
        },
        "zhanqing_sword": {
            "main_skill_name": "斩情诀",
            "main_lineage_id": "cutting_emotion_manual",
            "path_name": "斩情剑道",
            "guard": ("blood_armor_guard", "血煞护体功"),
            "movement": ("chasing_light_step", "逐光身法"),
            "spirit": ("blade_intent_focus", "刃念凝神篇"),
            "preferred_scene": "渊境征伐、群邪扫荡。",
            "main_patches": ("main_combo_trigger_up",),
            "guard_patches": ("guard_damage_reduction",),
            "movement_patches": ("movement_speed_bonus",),
            "spirit_patches": ("spirit_crit_bonus",),
        },
    }
    lineage_payload = lineage_payload_by_path_id[skill_main_path_id]
    skill_snapshot = SkillPanelSnapshot(
        character_id=77,
        character_name="顾长明",
        realm_id="mortal",
        stage_id="middle",
        main_axis_id="sword",
        main_axis_name="剑诀系",
        axis_focus_summary="主打抢节奏与单体压制。",
        main_path_id=skill_main_path_id,
        main_path_name=lineage_payload["main_skill_name"],
        preferred_scene=lineage_payload["preferred_scene"],
        combat_identity="高速单体爆发、残血斩杀、先手压制。",
        behavior_template_id=skill_main_path_id,
        behavior_template_name=lineage_payload["path_name"],
        resource_policy="burst",
        template_tags=("爆发", "先手"),
        main_skill=SkillPanelSkillSlotSnapshot(
            slot_id="main",
            slot_name="主修",
            item_id=3001,
            lineage_id=lineage_payload["main_lineage_id"],
            skill_name=lineage_payload["main_skill_name"],
            path_id=skill_main_path_id,
            path_name=lineage_payload["path_name"],
            rank_id="mortal",
            rank_name="一阶",
            quality_id="ordinary",
            quality_name="凡品",
            skill_type="main",
            total_budget=8 if skill_main_path_id == "wenxin_sword" else 10,
            resolved_patch_ids=lineage_payload["main_patches"],
        ),
        auxiliary_skills=(
            SkillPanelSkillSlotSnapshot(
                slot_id="guard",
                slot_name="护体",
                item_id=3002,
                lineage_id=lineage_payload["guard"][0],
                skill_name=lineage_payload["guard"][1],
                path_id=skill_main_path_id,
                path_name=lineage_payload["path_name"],
                rank_id="mortal",
                rank_name="一阶",
                quality_id="ordinary",
                quality_name="凡品",
                skill_type="auxiliary",
                total_budget=3,
                resolved_patch_ids=lineage_payload["guard_patches"],
            ),
            SkillPanelSkillSlotSnapshot(
                slot_id="movement",
                slot_name="身法",
                item_id=3003,
                lineage_id=lineage_payload["movement"][0],
                skill_name=lineage_payload["movement"][1],
                path_id=skill_main_path_id,
                path_name=lineage_payload["path_name"],
                rank_id="mortal",
                rank_name="一阶",
                quality_id="ordinary",
                quality_name="凡品",
                skill_type="auxiliary",
                total_budget=3,
                resolved_patch_ids=lineage_payload["movement_patches"],
            ),
            SkillPanelSkillSlotSnapshot(
                slot_id="spirit",
                slot_name="神识",
                item_id=3004,
                lineage_id=lineage_payload["spirit"][0],
                skill_name=lineage_payload["spirit"][1],
                path_id=skill_main_path_id,
                path_name=lineage_payload["path_name"],
                rank_id="mortal",
                rank_name="一阶",
                quality_id="ordinary",
                quality_name="凡品",
                skill_type="auxiliary",
                total_budget=3,
                resolved_patch_ids=lineage_payload["spirit_patches"],
            ),
        ),
        config_version="1.0.0",
        owned_skills=(
            SkillPanelSkillSlotSnapshot(
                slot_id="main",
                slot_name="主修",
                item_id=3001,
                lineage_id=lineage_payload["main_lineage_id"],
                skill_name=lineage_payload["main_skill_name"],
                path_id=skill_main_path_id,
                path_name=lineage_payload["path_name"],
                rank_id="mortal",
                rank_name="一阶",
                quality_id="ordinary",
                quality_name="凡品",
                skill_type="main",
                total_budget=8 if skill_main_path_id == "wenxin_sword" else 10,
                resolved_patch_ids=lineage_payload["main_patches"],
                equipped_slot_id="main",
            ),
            SkillPanelSkillSlotSnapshot(
                slot_id="main",
                slot_name="主修",
                item_id=3011,
                lineage_id="formless_heart_sword" if skill_main_path_id == "wenxin_sword" else "seven_kill_sword",
                skill_name="无相心剑诀" if skill_main_path_id == "wenxin_sword" else "七杀剑诀",
                path_id="wenxin_sword",
                path_name="问心剑道",
                rank_id="mortal",
                rank_name="一阶",
                quality_id="good",
                quality_name="良品",
                skill_type="main",
                total_budget=9,
                resolved_patch_ids=lineage_payload["main_patches"],
                equipped_slot_id=None,
            ),
            SkillPanelSkillSlotSnapshot(
                slot_id="guard",
                slot_name="护体",
                item_id=3002,
                lineage_id=lineage_payload["guard"][0],
                skill_name=lineage_payload["guard"][1],
                path_id=skill_main_path_id,
                path_name=lineage_payload["path_name"],
                rank_id="mortal",
                rank_name="一阶",
                quality_id="ordinary",
                quality_name="凡品",
                skill_type="auxiliary",
                total_budget=3,
                resolved_patch_ids=lineage_payload["guard_patches"],
                equipped_slot_id="guard",
            ),
            SkillPanelSkillSlotSnapshot(
                slot_id="guard",
                slot_name="护体",
                item_id=3012,
                lineage_id="blood_armor_guard",
                skill_name="血煞护体功",
                path_id="zhanqing_sword",
                path_name="斩情剑道",
                rank_id="mortal",
                rank_name="一阶",
                quality_id="good",
                quality_name="良品",
                skill_type="auxiliary",
                total_budget=4,
                resolved_patch_ids=lineage_payload["guard_patches"],
                equipped_slot_id=None,
            ),
        ),
    )
    return EquipmentPanelSnapshot(
        character_id=77,
        spirit_stone=6000,
        collection=collection,
        slot_panels=tuple(slot_panels),
        skill_snapshot=skill_snapshot,
        latest_drop=None,
    )



def _build_item_snapshot(
    *,
    item_id: int,
    slot_id: str,
    slot_name: str,
    display_name: str,
    quality_name: str,
    equipped_slot_id: str | None,
    is_artifact: bool = False,
    artifact_nurture_level: int = 0,
    affixes: tuple[EquipmentAffixSnapshot, ...] | None = None,
) -> EquipmentItemSnapshot:
    resolved_affixes = affixes or (
        EquipmentAffixSnapshot(
            affix_id="attack_power",
            affix_name="攻力加成",
            stat_id="attack_power",
            category="base_stat",
            tier_id="earth",
            tier_name="地品",
            rolled_multiplier=Decimal("1.0"),
            value=12,
            is_pve_specialized=False,
            is_pvp_specialized=False,
            position=1,
        ),
    )
    return EquipmentItemSnapshot(
        item_id=item_id,
        character_id=77,
        slot_id=slot_id,
        slot_name=slot_name,
        equipped_slot_id=equipped_slot_id,
        quality_id="epic",
        quality_name=quality_name,
        template_id="skyfire_mirror" if is_artifact else "iron_sword",
        template_name="天火镜" if is_artifact else "玄铁剑",
        rank_id="mortal",
        rank_name="一阶",
        rank_order=1,
        mapped_realm_id="mortal",
        is_artifact=is_artifact,
        resonance_name="炎华" if is_artifact else None,
        item_state="active",
        display_name=display_name,
        enhancement_level=1,
        artifact_nurture_level=artifact_nurture_level,
        enhancement_success_count=1,
        enhancement_failure_count=0,
        base_attribute_multiplier=Decimal("1.0"),
        affix_base_value_multiplier=Decimal("1.0"),
        dismantle_reward_multiplier=Decimal("1.0"),
        enhancement_base_stat_bonus_ratio=Decimal("0"),
        enhancement_affix_bonus_ratio=Decimal("0"),
        nurture_base_stat_bonus_ratio=Decimal("0"),
        nurture_affix_bonus_ratio=Decimal("0"),
        base_stat_bonus_ratio=Decimal("0"),
        affix_bonus_ratio=Decimal("0"),
        base_attributes=(EquipmentAttributeSnapshot(stat_id="attack_power", value=88),),
        affixes=resolved_affixes,
        resolved_stats=(EquipmentResolvedStatSnapshot(stat_id="attack_power", value=100),),
        naming=EquipmentNamingSnapshot(
            resolved_name=display_name,
            naming_template_id="legendary_masterwork",
            naming_source="test",
            naming_metadata={},
        ),
        dismantled_at=None,
    )



def _build_equipment_card_snapshot(item: EquipmentItemSnapshot) -> EquipmentCardSnapshot:
    growth_line = f"强化 +{item.enhancement_level}"
    if item.is_artifact:
        growth_line += f"｜祭炼 {item.artifact_nurture_level}"
    keyword_lines = tuple(EquipmentPanelPresenter._format_affix_line(affix) for affix in item.affixes[:3])
    return EquipmentCardSnapshot(
        name=item.display_name,
        badge_line=f"{'法宝' if item.is_artifact else item.slot_name}｜{item.rank_name}｜{item.quality_name}",
        growth_line=growth_line,
        stat_lines=("攻力 100",),
        keyword_lines=keyword_lines,
    )



def _build_backpack_snapshot() -> BackpackPanelSnapshot:
    selected_affixes = (
        EquipmentAffixSnapshot(
            affix_id="attack_power",
            affix_name="攻力加成",
            stat_id="attack_power",
            category="base_stat",
            tier_id="earth",
            tier_name="地品",
            rolled_multiplier=Decimal("1.0"),
            value=12,
            is_pve_specialized=False,
            is_pvp_specialized=False,
            position=1,
        ),
        EquipmentAffixSnapshot(
            affix_id="artifact_counter_dot",
            affix_name="反噬灼息",
            stat_id="",
            category="special_pattern",
            tier_id="heaven",
            tier_name="天品",
            rolled_multiplier=Decimal("1.0"),
            value=0,
            is_pve_specialized=False,
            is_pvp_specialized=False,
            affix_kind="special_effect",
            special_effect=SimpleNamespace(
                effect_id="counter_dot",
                effect_name="反噬灼息",
                effect_type="counter_dot",
                trigger_event="damage_taken",
                payload={
                    "trigger_rate_permille": 200,
                    "dot_ratio_permille": 200,
                    "duration_rounds": 2,
                    "max_stacks": 3,
                    "cooldown_rounds": 2,
                    "max_triggers_per_round": 1,
                },
                public_score_key="se_counter_dot",
                hidden_pvp_score_key="pvp_se_counter_dot",
            ),
            position=2,
        ),
    )
    equipped_affixes = (
        EquipmentAffixSnapshot(
            affix_id="attack_power",
            affix_name="攻力加成",
            stat_id="attack_power",
            category="base_stat",
            tier_id="mystic",
            tier_name="玄品",
            rolled_multiplier=Decimal("1.0"),
            value=10,
            is_pve_specialized=False,
            is_pvp_specialized=False,
            position=1,
        ),
    )
    selected_equipment = _build_item_snapshot(
        item_id=5101,
        slot_id="weapon",
        slot_name="武器",
        display_name="照霜剑",
        quality_name="史诗",
        equipped_slot_id=None,
        affixes=selected_affixes,
    )
    equipped_equipment = _build_item_snapshot(
        item_id=5102,
        slot_id="weapon",
        slot_name="武器",
        display_name="破军刃",
        quality_name="稀有",
        equipped_slot_id="weapon",
        affixes=equipped_affixes,
    )
    page_entries = (
        BackpackEntrySummarySnapshot(
            entry_key=BackpackEntryKey(entry_kind=BackpackEntryKind.EQUIPMENT, item_id=5101),
            entry_kind=BackpackEntryKind.EQUIPMENT,
            item_id=5101,
            slot_id="weapon",
            slot_name="武器",
            display_name="照霜剑",
            quality_name="史诗",
            rank_name="一阶",
            equipped=False,
            is_artifact=False,
            summary_line="一阶｜史诗｜强化 +1｜2词条/1特效",
        ),
        BackpackEntrySummarySnapshot(
            entry_key=BackpackEntryKey(entry_kind=BackpackEntryKind.EQUIPMENT, item_id=5103),
            entry_kind=BackpackEntryKind.EQUIPMENT,
            item_id=5103,
            slot_id="weapon",
            slot_name="武器",
            display_name="试锋剑",
            quality_name="稀有",
            rank_name="一阶",
            equipped=False,
            is_artifact=False,
            summary_line="一阶｜稀有｜强化 +0｜1词条/0特效",
        ),
    )
    selected_detail = BackpackSelectedDetailSnapshot(
        entry_key=page_entries[0].entry_key,
        entry_kind=BackpackEntryKind.EQUIPMENT,
        equipment_item=selected_equipment,
        selected_card=_build_equipment_card_snapshot(selected_equipment),
        equip_action_enabled=True,
        equip_action_label="装配",
        same_type_equipped_entry_key=BackpackEntryKey(entry_kind=BackpackEntryKind.EQUIPMENT, item_id=5102),
        same_type_equipped_equipment_item=equipped_equipment,
        equipped_card=_build_equipment_card_snapshot(equipped_equipment),
        is_same_as_equipped=False,
    )
    return BackpackPanelSnapshot(
        character_id=77,
        character_name="顾长明",
        filter_id=BackpackFilterId.ALL,
        page=1,
        page_size=25,
        total_items=2,
        total_pages=1,
        page_entries=page_entries,
        selected_detail=selected_detail,
    )



def _build_forge_snapshot() -> ForgePanelSnapshot:
    item = _build_item_snapshot(
        item_id=6201,
        slot_id="artifact",
        slot_name="法宝",
        display_name="天火镜",
        quality_name="史诗",
        equipped_slot_id="artifact",
        is_artifact=True,
        artifact_nurture_level=2,
        affixes=(
            EquipmentAffixSnapshot(
                affix_id="artifact_damage_to_barrier",
                affix_name="伤转灵障",
                stat_id="",
                category="special_pattern",
                tier_id="heaven",
                tier_name="天品",
                rolled_multiplier=Decimal("1.0"),
                value=0,
                is_pve_specialized=False,
                is_pvp_specialized=False,
                affix_kind="special_effect",
                special_effect=SimpleNamespace(
                    effect_id="damage_to_barrier",
                    effect_name="伤转灵障",
                    effect_type="damage_to_barrier",
                    trigger_event="damage_resolved",
                    payload={
                        "trigger_rate_permille": 1000,
                        "damage_ratio_permille": 150,
                        "cooldown_rounds": 1,
                        "max_stacks": 1,
                        "max_triggers_per_round": 1,
                    },
                    public_score_key="se_damage_to_barrier",
                    hidden_pvp_score_key="pvp_se_damage_to_barrier",
                ),
                position=1,
            ),
        ),
    )
    target = ForgeTargetSnapshot(
        target_id="equipment:6201",
        target_kind=ForgeTargetKind.EQUIPMENT,
        slot_id="artifact",
        slot_name="法宝",
        core_role="法宝位",
        display_name="天火镜",
        summary_line="一阶｜史诗｜祭炼 2｜1词条/1特效",
        equipped=True,
        equipment_item=item,
        supported_operations=(ForgeOperationId.NURTURE, ForgeOperationId.WASH, ForgeOperationId.REFORGE),
    )
    secondary_target = ForgeTargetSnapshot(
        target_id="equipment:6202",
        target_kind=ForgeTargetKind.EQUIPMENT,
        slot_id="weapon",
        slot_name="武器",
        core_role="武器位",
        display_name="试锋剑",
        summary_line="一阶｜稀有｜强化 +0｜1词条/0特效",
        equipped=False,
        equipment_item=_build_item_snapshot(
            item_id=6202,
            slot_id="weapon",
            slot_name="武器",
            display_name="试锋剑",
            quality_name="稀有",
            equipped_slot_id=None,
        ),
        supported_operations=(ForgeOperationId.ENHANCE, ForgeOperationId.WASH),
    )
    return ForgePanelSnapshot(
        character_id=77,
        character_name="顾长明",
        resources=ForgeResourceSnapshot(
            spirit_stone=6000,
            enhancement_stone=0,
            enhancement_shard=0,
            wash_dust=4,
            spirit_sand=0,
            spirit_pattern_stone=3,
            soul_binding_jade=2,
            artifact_essence=10,
            entries=(
                ForgeResourceEntrySnapshot(resource_id="spirit_stone", resource_name="灵石", quantity=6000),
                ForgeResourceEntrySnapshot(resource_id="artifact_essence", resource_name="法宝精粹", quantity=10),
            ),
        ),
        filter_id=ForgeFilterId.ALL,
        page=1,
        page_size=25,
        total_items=2,
        total_pages=1,
        targets=(target, secondary_target),
        selected_target=target,
        selected_target_card=ForgeCardSnapshot(
            name="天火镜",
            badge_line="法宝｜一阶｜史诗",
            growth_line="强化 +1｜祭炼 2",
            stat_lines=("攻力 100", "气血 120"),
            keyword_lines=("伤转灵障：造成伤害后必定把本次伤害的15.0%转化为自身护盾，冷却1回合。",),
        ),
        current_operation_name="法宝培养",
        operation_costs=(
            ForgeOperationCostSnapshot(
                resource_id="spirit_stone",
                resource_name="灵石",
                required_quantity=1200,
                owned_quantity=6000,
            ),
            ForgeOperationCostSnapshot(
                resource_id="artifact_essence",
                resource_name="法宝精粹",
                required_quantity=3,
                owned_quantity=10,
            ),
        ),
        operation_preview=ForgeOperationPreviewSnapshot(
            title="法宝培养",
            lines=("祭炼 2 → 3", "基础属性 +6.0%", "词条成长 +4.0%"),
        ),
    )



def _build_controller_for_view(snapshot: EquipmentPanelSnapshot) -> EquipmentPanelController:
    controller = EquipmentPanelController(
        session_factory=_DummySessionFactory(),
        service_bundle_factory=lambda session: _ServiceBundle(
            character_panel_query_service=None,
            equipment_panel_query_service=SimpleNamespace(get_panel_snapshot=lambda *, character_id: snapshot),
            equipment_service=SimpleNamespace(),
            skill_loadout_service=SimpleNamespace(),
        ),
    )
    return controller



def _build_interaction(*, user_id: int = 50001) -> SimpleNamespace:
    response = SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=response,
    )



def test_backpack_embed_removes_verbose_page_list_and_keeps_compact_status() -> None:
    snapshot = _build_backpack_snapshot()
    embed = BackpackPanelPresenter.build_embed(snapshot=snapshot, state=BackpackPanelState(selected_entry_key=snapshot.page_entries[0].entry_key))

    field_names = [field.name for field in embed.fields]
    assert field_names[:3] == ["📌 浏览状态", "✨ 选中卡", "🛡 当前已装同槽卡"]
    assert "🎒 当前页" not in field_names
    status_field = next(field for field in embed.fields if field.name == "📌 浏览状态")
    selected_field = next(field for field in embed.fields if field.name == "✨ 选中卡")
    equipped_field = next(field for field in embed.fields if field.name == "🛡 当前已装同槽卡")
    assert "当前页可选：2 项，请使用下拉框查看。" in status_field.value
    assert "照霜剑" in status_field.value
    assert "```" in selected_field.value
    assert "词条：" in selected_field.value
    assert "攻力加成：攻力 +12" in selected_field.value
    assert "反噬灼息：受到伤害后有20.0%概率对伤害来源施加持续伤害，系数为20.0%，持续2回合，最多叠加3层，冷却2回合。" in selected_field.value
    assert "```" in equipped_field.value
    assert "攻力加成：攻力 +10" in equipped_field.value



def test_equipment_hub_embed_only_keeps_equipped_slot_list() -> None:
    snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True)
    embed = EquipmentPanelPresenter.build_embed(
        snapshot=snapshot,
        display_mode=EquipmentPanelDisplayMode.HUB,
        selected_slot_id=None,
        selected_candidate_item_id=None,
        action_note=None,
    )

    field_names = [field.name for field in embed.fields]
    assert field_names == ["🛡 已装备部位列表"]
    assert "```" in embed.fields[0].value
    assert "武器｜星陨剑 +1｜候选 1" in embed.fields[0].value
    assert "法宝｜天火镜 祭2｜候选 0" in embed.fields[0].value



def test_equipment_slot_detail_embed_uses_three_compact_blocks() -> None:
    snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True)
    embed = EquipmentPanelPresenter.build_embed(
        snapshot=snapshot,
        display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
        selected_slot_id="weapon",
        selected_candidate_item_id=1002,
        action_note=None,
    )

    field_names = [field.name for field in embed.fields]
    assert field_names[:3] == ["🛡 当前装备", "🎒 候选列表", "✨ 选中候选"]
    assert "部位说明" not in field_names
    current_field = next(field for field in embed.fields if field.name == "🛡 当前装备")
    candidate_list_field = next(field for field in embed.fields if field.name == "🎒 候选列表")
    selected_candidate_field = next(field for field in embed.fields if field.name == "✨ 选中候选")
    assert "```" in current_field.value
    assert ">02. 试锋剑" in candidate_list_field.value
    assert "```" in selected_candidate_field.value
    assert "词条：" in selected_candidate_field.value
    assert "攻力加成：攻力 +12" in selected_candidate_field.value



def test_forge_embed_removes_verbose_target_list_and_keeps_compact_status() -> None:
    snapshot = _build_forge_snapshot()
    embed = ForgePanelPresenter.build_embed(snapshot=snapshot, state=ForgePanelState(selected_target_id="equipment:6201"))

    field_names = [field.name for field in embed.fields]
    assert field_names[:4] == ["📌 目标状态", "✨ 目标卡", "💰 本次消耗", "🧪 结果预览"]
    assert "⚒ 当前目标列表" not in field_names
    status_field = next(field for field in embed.fields if field.name == "📌 目标状态")
    target_field = next(field for field in embed.fields if field.name == "✨ 目标卡")
    cost_field = next(field for field in embed.fields if field.name == "💰 本次消耗")
    preview_field = next(field for field in embed.fields if field.name == "🧪 结果预览")
    assert "当前页可选：2 项，请使用下拉框切换目标。" in status_field.value
    assert "当前目标：天火镜｜已装" in status_field.value
    assert "当前操作：法宝培养" in status_field.value
    assert "词条：" in target_field.value
    assert "伤转灵障：造成伤害后必定把本次伤害的15.0%转化为自身护盾，冷却1回合。" in target_field.value
    assert "灵石 1200 / 持有 6000" in cost_field.value
    assert "法宝精粹 3 / 持有 10" in cost_field.value
    assert "祭炼 2 → 3" in preview_field.value
    assert "基础属性 +6.0%" in preview_field.value



def test_unequip_item_moves_equipped_item_back_to_active_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """卸下装备后应清空部位穿戴状态，并回到候选列表。"""
    database_url = _build_sqlite_url(tmp_path / "stage10_unequip.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        services = _create_services(session, static_config=static_config)
        character_id = _create_character_with_skill_context(services, discord_user_id="91001", character_name="顾长明")
        _seed_materials(services, character_id=character_id)
        generated = services.equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="epic",
            template_id="iron_sword",
            affix_count=2,
            seed=7,
        )
        equip_result = services.equipment_service.equip_item(
            character_id=character_id,
            equipment_item_id=generated.item.item_id,
        )
        assert equip_result.equipped_slot_id == "weapon"

        result = services.equipment_service.unequip_item(
            character_id=character_id,
            equipped_slot_id="weapon",
        )

        assert result.unequipped_slot_id == "weapon"
        assert result.item.item_id == generated.item.item_id
        assert result.item.equipped_slot_id is None
        collection = services.equipment_service.list_equipment(character_id=character_id)
        assert collection.equipped_items == ()
        assert [item.item_id for item in collection.active_items] == [generated.item.item_id]
        with pytest.raises(EquipmentUnequipTargetNotFoundError):
            services.equipment_service.unequip_item(character_id=character_id, equipped_slot_id="weapon")



def test_equip_skill_instance_updates_loadout_and_score_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """按实例装配主修功法后应写回配置并刷新评分快照。"""
    database_url = _build_sqlite_url(tmp_path / "stage10_equip_skill_instance.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()

    with session_scope(session_factory) as session:
        services = _create_services(session, static_config=static_config)
        character_id = _create_character_with_skill_context(services, discord_user_id="91002", character_name="宁昭")
        aggregate = services.character_repository.get_aggregate(character_id)
        assert aggregate is not None
        assert aggregate.skill_loadout is not None
        starter_main_skill_id = aggregate.skill_loadout.main_skill_id
        assert starter_main_skill_id is not None

        generated_switch = services.skill_loadout_service.switch_main_path(
            character_id=character_id,
            main_path_id="zhanqing_sword",
        )
        aggregate = services.character_repository.get_aggregate(character_id)
        assert aggregate is not None
        assert aggregate.skill_loadout is not None
        generated_main_skill_id = aggregate.skill_loadout.main_skill_id
        assert generated_main_skill_id is not None
        assert generated_main_skill_id != starter_main_skill_id

        result = services.skill_loadout_service.equip_skill_instance(
            character_id=character_id,
            skill_item_id=starter_main_skill_id,
        )

        assert generated_switch.character_id == character_id
        assert result.character_id == character_id
        assert result.slot_id == "main"
        assert result.previous_skill_item_id == generated_main_skill_id
        assert result.equipped_skill_item_id == starter_main_skill_id
        assert result.main_path_id == "wenxin_sword"
        assert result.behavior_template_id == "wenxin_sword"

        aggregate = services.character_repository.get_aggregate(character_id)
        assert aggregate is not None
        assert aggregate.skill_loadout is not None
        assert aggregate.skill_loadout.main_skill_id == starter_main_skill_id
        assert aggregate.skill_loadout.main_path_id == "wenxin_sword"
        assert aggregate.skill_loadout.behavior_template_id == "wenxin_sword"
        persisted_snapshot = services.score_snapshot_repository.get_by_character_id(character_id)
        assert persisted_snapshot is not None
        assert persisted_snapshot.breakdown_json["source_summary"]["main_path_id"] == "wenxin_sword"
        assert persisted_snapshot.breakdown_json["source_summary"]["main_skill_name"] == "七杀剑诀"
        profile_snapshot = services.profile_panel_query_service.get_skill_snapshot(character_id=character_id)
        assert profile_snapshot.main_path_id == "wenxin_sword"
        assert profile_snapshot.behavior_template_id == "wenxin_sword"
        assert profile_snapshot.main_path_name == "七杀剑诀"
        assert profile_snapshot.main_skill.path_name == "问心剑道"
        assert any(skill.item_id == starter_main_skill_id and skill.equipped_slot_id == "main" for skill in profile_snapshot.owned_skills)
        assert any(skill.item_id == generated_main_skill_id and skill.equipped_slot_id is None for skill in profile_snapshot.owned_skills)



@pytest.mark.asyncio
async def test_equipment_slot_detail_disables_actions_when_current_slot_is_empty() -> None:
    """当前部位没有已装备物品时，装备动作应禁用。"""
    snapshot = _build_test_snapshot(include_artifact=False, weapon_equipped=False)
    view = EquipmentPanelView(
        controller=_build_controller_for_view(snapshot),
        owner_user_id=50001,
        character_id=snapshot.character_id,
        snapshot=snapshot,
        display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
        selected_slot_id="weapon",
        selected_candidate_item_id=1002,
    )

    assert view.try_equip.disabled is False
    assert view.enhance_equipment.disabled is True
    assert view.wash_equipment.disabled is True
    assert view.reforge_equipment.disabled is True
    assert view.dismantle_equipment.disabled is True
    assert view.unequip_equipment.disabled is True
    assert view.nurture_artifact not in view.children



@pytest.mark.asyncio
async def test_artifact_slot_detail_shows_nurture_action_for_equipped_artifact() -> None:
    """artifact 部位存在已装备法宝时应显示法宝培养入口。"""
    snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True)
    view = EquipmentPanelView(
        controller=_build_controller_for_view(snapshot),
        owner_user_id=50001,
        character_id=snapshot.character_id,
        snapshot=snapshot,
        display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
        selected_slot_id="artifact",
        selected_candidate_item_id=2001,
    )

    assert view.nurture_artifact in view.children
    assert view.nurture_artifact.disabled is False
    labels = {child.label for child in view.children if isinstance(child, discord.ui.Button)}
    assert "法宝培养" in labels
    embed = view.build_embed()
    current_equipped_field = next(field for field in embed.fields if field.name == "🛡 当前装备")
    assert "祭炼 2" in current_equipped_field.value



@pytest.mark.asyncio
async def test_skill_detail_only_keeps_skill_instance_entry_without_equipment_actions() -> None:
    """功法页只保留功法实例装配入口，不应混入装备动作。"""
    clear_static_config_cache()
    snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True)
    view = EquipmentPanelView(
        controller=_build_controller_for_view(snapshot),
        owner_user_id=50001,
        character_id=snapshot.character_id,
        snapshot=snapshot,
        display_mode=EquipmentPanelDisplayMode.SKILL_DETAIL,
        selected_slot_id=None,
        selected_candidate_item_id=None,
    )

    button_labels = {child.label for child in view.children if isinstance(child, discord.ui.Button)}
    assert "功法详情" in button_labels
    assert "总览" in button_labels
    assert "刷新" in button_labels
    assert "尝试装备" not in button_labels
    assert "强化" not in button_labels
    assert "洗炼" not in button_labels
    assert "重铸" not in button_labels
    assert "分解" not in button_labels
    assert "法宝培养" not in button_labels
    assert "卸下装备" not in button_labels
    select_placeholders = {child.placeholder for child in view.children if isinstance(child, discord.ui.Select)}
    assert "选择要装配的功法实例" in select_placeholders
    assert "选择装备部位查看详情" not in select_placeholders



def test_recent_drop_summary_and_embed_show_endless_instance_drops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最近掉落摘要与装备面板应展示无尽结算产出的装备/法宝实例。"""
    database_url = _build_sqlite_url(tmp_path / "stage10_recent_endless_drop.db")
    _upgrade_database(database_url, monkeypatch)
    session_factory = create_session_factory(database_url)
    static_config = load_static_config()
    drop_time = datetime(2026, 3, 26, 21, 0, 0)

    with session_scope(session_factory) as session:
        services = _create_services(session, static_config=static_config)
        character_id = _create_character_with_skill_context(
            services,
            discord_user_id="50011",
            character_name="顾长明",
        )
        weapon = services.equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="weapon",
            quality_id="legendary",
            rank_id="foundation",
            template_id="iron_sword",
            affix_count=2,
            seed=11,
        ).item
        artifact = services.equipment_service.generate_equipment(
            character_id=character_id,
            slot_id="artifact",
            quality_id="epic",
            rank_id="foundation",
            template_id="skyfire_mirror",
            affix_count=1,
            seed=5,
        ).item
        skill_item = services.skill_repository.get_skill_item(services.skill_loadout_service.list_owned_skills(character_id=character_id)[0].item_id)
        assert skill_item is not None
        services.equipment_service.apply_custom_name(
            character_id=character_id,
            equipment_item_id=weapon.item_id,
            resolved_name="AI·裂风玄刃",
            naming_source="ai_batch",
            naming_template_id="panel_test",
            naming_metadata={"batch_id": "1"},
        )
        services.equipment_service.apply_custom_name(
            character_id=character_id,
            equipment_item_id=artifact.item_id,
            resolved_name="AI·曜火镜",
            naming_source="ai_batch",
            naming_template_id="panel_test",
            naming_metadata={"batch_id": "1"},
        )
        services.skill_loadout_service._skill_runtime_support.apply_custom_name(
            character_id=character_id,
            skill_item_id=skill_item.id,
            resolved_name="AI·七杀剑诀",
            naming_source="ai_batch",
            naming_metadata={"batch_id": "1"},
        )
        drop_record = DropRecord(
            character_id=character_id,
            battle_report_id=None,
            source_type="endless",
            source_ref="endless:retreat:floor_10",
            items_json=[
                {
                    "entry_type": "stable_reward_bundle",
                    "settled": {"cultivation": 1200, "insight": 9, "refining_essence": 12},
                },
                {
                    "entry_type": "pending_reward_bundle",
                    "settled": {"equipment_score": 80, "artifact_score": 18, "dao_pattern_score": 16},
                },
                {
                    "entry_type": "equipment_drop",
                    "item_id": weapon.item_id,
                    "display_name": "旧武器名",
                    "slot_name": weapon.slot_name,
                    "quality_name": weapon.quality_name,
                    "rank_name": weapon.rank_name,
                    "is_artifact": False,
                },
                {
                    "entry_type": "artifact_drop",
                    "item_id": artifact.item_id,
                    "display_name": "旧法宝名",
                    "quality_name": artifact.quality_name,
                    "rank_name": artifact.rank_name,
                    "resonance_name": artifact.resonance_name,
                    "is_artifact": True,
                },
                {
                    "entry_type": "skill_drop",
                    "item_id": skill_item.id,
                    "skill_name": "旧功法名",
                    "rank_name": skill_item.rank_name,
                    "quality_name": skill_item.quality_name,
                    "skill_type": skill_item.skill_type,
                },
            ],
            currencies_json={"cultivation": 1200, "insight": 9, "refining_essence": 12},
            occurred_at=drop_time,
        )
        item_lines = services.equipment_panel_query_service._build_item_lines(record=drop_record)
        currency_lines = services.equipment_panel_query_service._build_currency_lines(record=drop_record)

    assert item_lines[:3] == (
        f"装备实例：AI·裂风玄刃｜{weapon.quality_name}｜{weapon.rank_name}｜{weapon.slot_name}",
        f"法宝实例：AI·曜火镜｜{artifact.quality_name}｜{artifact.rank_name}｜共鸣 {artifact.resonance_name}",
        f"功法实例：AI·七杀剑诀｜{skill_item.rank_name}｜{skill_item.quality_name}",
    )
    assert currency_lines == ("修为 +1200", "感悟 +9", "祭炼精华 +12")

    base_snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True)
    snapshot = EquipmentPanelSnapshot(
        character_id=base_snapshot.character_id,
        spirit_stone=base_snapshot.spirit_stone,
        collection=base_snapshot.collection,
        slot_panels=base_snapshot.slot_panels,
        skill_snapshot=base_snapshot.skill_snapshot,
        latest_drop=EquipmentDropSummary(
            source_type="endless",
            source_label="无涯渊境",
            source_ref="endless:retreat:floor_10",
            occurred_at=drop_time,
            item_lines=item_lines,
            currency_lines=currency_lines,
        ),
    )
    embed = EquipmentPanelPresenter.build_embed(
        snapshot=snapshot,
        display_mode=EquipmentPanelDisplayMode.HUB,
        selected_slot_id=None,
        selected_candidate_item_id=None,
        action_note=None,
    )
    assert [field.name for field in embed.fields] == ["🛡 已装备部位列表"]



@pytest.mark.asyncio
async def test_unequip_action_edits_private_panel_with_result_note() -> None:
    """卸下动作成功后应走私有面板刷新链路，并写回动作说明。"""
    snapshot = _build_test_snapshot(include_artifact=False, weapon_equipped=False)
    controller = _build_controller_for_view(snapshot)
    controller._unequip_item = lambda **kwargs: SimpleNamespace(
        unequipped_slot_id="weapon",
        item=SimpleNamespace(display_name="星陨剑"),
    )
    controller._load_snapshot = lambda **kwargs: snapshot
    controller.responder.edit_message = AsyncMock()
    interaction = _build_interaction()

    await controller.unequip_equipped_item(
        interaction,
        character_id=77,
        owner_user_id=50001,
        slot_id="weapon",
        selected_candidate_item_id=None,
    )

    controller.responder.edit_message.assert_awaited_once()
    _, kwargs = controller.responder.edit_message.await_args
    payload = kwargs["payload"]
    assert isinstance(payload, PanelMessagePayload)
    result_field = next(field for field in payload.embed.fields if field.name == "卸下结果")
    assert "已卸下部位：weapon" in result_field.value
    assert "物品已回到当前部位候选列表。" in result_field.value



@pytest.mark.asyncio
async def test_equip_skill_instance_edits_private_panel_with_action_note() -> None:
    """功法实例装配成功后应刷新功法页并写回结果说明。"""
    snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True, skill_main_path_id="zhanqing_sword")
    controller = _build_controller_for_view(snapshot)
    controller._equip_skill_instance = lambda **kwargs: SimpleNamespace(
        slot_id="guard",
        previous_skill_item_id=3002,
        equipped_skill_item_id=3012,
        config_version="1.0.0",
    )
    controller._load_snapshot = lambda **kwargs: snapshot
    controller.responder.edit_message = AsyncMock()
    interaction = _build_interaction()

    await controller.equip_skill_instance(
        interaction,
        character_id=77,
        owner_user_id=50001,
        skill_item_id=3012,
    )

    controller.responder.edit_message.assert_awaited_once()
    _, kwargs = controller.responder.edit_message.await_args
    payload = kwargs["payload"]
    assert isinstance(payload, PanelMessagePayload)
    result_field = next(field for field in payload.embed.fields if field.name == "功法装配结果")
    assert "装配槽位：护体" in result_field.value
    assert "当前功法：血煞护体功｜一阶｜良品" in result_field.value
    assert "此前该槽位已有已装配功法" in result_field.value
    assert "所属流派：斩情剑道" in result_field.value
    assert "战斗流派：斩情剑道" in result_field.value
    assert "配置版本" not in result_field.value



def test_skill_detail_embed_hides_internal_fields_and_uses_localized_names() -> None:
    """功法详情页不应暴露内部字段，并应展示中文化文案。"""
    snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True)

    embed = EquipmentPanelPresenter.build_embed(
        snapshot=snapshot,
        display_mode=EquipmentPanelDisplayMode.SKILL_DETAIL,
        selected_slot_id=None,
        selected_candidate_item_id=None,
        action_note=None,
    )

    field_names = {field.name for field in embed.fields}
    assert "补充信息" not in field_names
    current_field = next(field for field in embed.fields if field.name == "当前装配")
    detail_field = next(field for field in embed.fields if field.name == "主修详情")
    auxiliary_field = next(field for field in embed.fields if field.name == "辅助装配")
    full_text = "\n".join(field.value for field in embed.fields)

    assert "凡人·中期" in current_field.value
    assert "mortal" not in current_field.value
    assert "预算" not in detail_field.value
    assert "预算" not in auxiliary_field.value
    assert "配置版本" not in full_text
    assert "标签" not in full_text
    assert "main_burst_damage_up" not in full_text
    assert "guard_damage_reduction" not in full_text
    assert "movement_speed_bonus" not in full_text
    assert "spirit_crit_bonus" not in full_text
    assert "流派加成：爆发增伤" in detail_field.value
    assert "流派加成 护盾增幅" not in auxiliary_field.value
    assert "流派加成 减伤增幅" in auxiliary_field.value
    assert "流派加成 速度增幅" in auxiliary_field.value
    assert "流派加成 神识暴伤" in auxiliary_field.value
    assert "神识：锁念剑心诀｜问心剑道｜一阶｜凡品｜流派加成 神识暴伤" in auxiliary_field.value



def test_equipment_slot_detail_card_uses_affix_name_and_effect_description() -> None:
    snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True)
    embed = EquipmentPanelPresenter.build_embed(
        snapshot=snapshot,
        display_mode=EquipmentPanelDisplayMode.SLOT_DETAIL,
        selected_slot_id="weapon",
        selected_candidate_item_id=1002,
        action_note=None,
    )

    current_field = next(field for field in embed.fields if field.name == "🛡 当前装备")
    selected_field = next(field for field in embed.fields if field.name == "✨ 选中候选")
    assert "攻力加成：攻力 +12" in current_field.value
    assert "攻力加成：攻力 +12" in selected_field.value
    assert "attack_power" not in selected_field.value
    assert "效果说明缺失" not in selected_field.value



def test_endless_skill_lines_localize_auxiliary_slot_names() -> None:
    """最近掉落与无尽摘要中的功法辅位应展示中文名。"""
    entry = {
        "skill_name": "锁念剑心诀",
        "rank_name": "一阶",
        "quality_name": "凡品",
        "skill_type": "auxiliary",
        "auxiliary_slot_id": "spirit",
    }

    query_line = EquipmentPanelQueryService._build_endless_skill_line(entry=entry)
    private_line = EndlessPanelPresenter._format_skill_drop_entry(entry=entry, public_mode=False)

    assert query_line == "功法实例：锁念剑心诀｜一阶｜凡品｜辅位 神识"
    assert private_line == "功法实例：锁念剑心诀｜一阶｜凡品｜辅位 神识"



def test_profile_panel_query_service_masks_unknown_internal_identifiers() -> None:
    """功法槽位快照兜底不应回显内部 path/slot 标识。"""
    service = object.__new__(ProfilePanelQueryService)
    service._slot_name_by_id = {"main": "主修", "guard": "护体"}
    service._path_by_id = {}

    snapshot = service._build_skill_slot_snapshot(
        slot_id="unknown_slot",
        skill_item=SimpleNamespace(
            item_id=9001,
            lineage_id="mystery_lineage",
            skill_name="无名功法",
            path_id="unknown_path",
            rank_id="mortal",
            rank_name="一阶",
            quality_id="ordinary",
            quality_name="凡品",
            skill_type="auxiliary",
            total_budget=3,
            resolved_patch_ids=(),
            equipped_slot_id=None,
        ),
    )

    assert snapshot.slot_name == "未知槽位"
    assert snapshot.path_name == "未知流派"



@pytest.mark.asyncio
async def test_switch_skill_main_path_edits_private_panel_without_internal_fields() -> None:
    """主修流派切换回执不应再包含配置版本等内部字段。"""
    snapshot = _build_test_snapshot(include_artifact=True, weapon_equipped=True, skill_main_path_id="zhanqing_sword")
    controller = _build_controller_for_view(snapshot)
    controller._switch_skill_main_path = lambda **kwargs: SimpleNamespace(
        previous_main_path_id="wenxin_sword",
        config_version="1.0.0",
    )
    controller._load_snapshot = lambda **kwargs: snapshot
    controller.responder.edit_message = AsyncMock()
    interaction = _build_interaction()

    await controller.switch_skill_main_path(
        interaction,
        character_id=77,
        owner_user_id=50001,
        main_path_id="zhanqing_sword",
    )

    controller.responder.edit_message.assert_awaited_once()
    _, kwargs = controller.responder.edit_message.await_args
    payload = kwargs["payload"]
    assert isinstance(payload, PanelMessagePayload)
    result_field = next(field for field in payload.embed.fields if field.name == "功法装配结果")
    assert "主修流派：问心剑道 → 斩情剑道" in result_field.value
    assert "主修功法：斩情诀｜一阶｜凡品" in result_field.value
    assert "战斗流派：斩情剑道" in result_field.value
    assert "配置版本" not in result_field.value
