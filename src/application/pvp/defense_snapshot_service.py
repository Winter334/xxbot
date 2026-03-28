"""PVP 防守快照应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from typing import Any

from application.character.current_attribute_service import CurrentAttributeService
from application.character.growth_service import CharacterGrowthStateError, CharacterNotFoundError
from domain.battle import BattleSide, BattleUnitSnapshot
from domain.pvp import (
    PvpDefenseSnapshotState,
    PvpDefenseSnapshotUsageDecision,
    PvpLeaderboardEntry,
    PvpRuleService,
)
from domain.ranking import LeaderboardBoardType
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import CharacterProgress, CharacterScoreSnapshot, PvpDefenseSnapshot
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, SnapshotRepository

_ACTIVE_ITEM_STATE = "active"
_DECIMAL_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class PvpDefenseSnapshotBundle:
    """单次防守快照解析后的只读结果。"""

    snapshot_state: PvpDefenseSnapshotState
    usage_decision: PvpDefenseSnapshotUsageDecision
    battle_unit_snapshot: BattleUnitSnapshot
    display_summary: dict[str, object]


class PvpDefenseSnapshotServiceError(RuntimeError):
    """PVP 防守快照服务基础异常。"""


class PvpDefenseSnapshotStateError(PvpDefenseSnapshotServiceError):
    """PVP 防守快照依赖的角色状态不完整。"""


class PvpDefenseSnapshotService:
    """负责抓取、复用与投影 PVP 防守快照。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        snapshot_repository: SnapshotRepository,
        current_attribute_service: CurrentAttributeService | None = None,
        static_config: StaticGameConfig | None = None,
        rule_service: PvpRuleService | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._snapshot_repository = snapshot_repository
        self._current_attribute_service = current_attribute_service or CurrentAttributeService(
            character_repository=character_repository,
        )
        self._static_config = static_config or get_static_config()
        self._rule_service = rule_service or PvpRuleService(self._static_config)
        self._realm_name_by_id = {
            realm.realm_id: realm.name
            for realm in self._static_config.realm_progression.realms
        }
        self._stage_name_by_id = {
            stage.stage_id: stage.name
            for stage in self._static_config.realm_progression.stages
        }

    def ensure_snapshot(
        self,
        *,
        character_id: int,
        now: datetime,
        requested_reason: str,
        leaderboard_entry: PvpLeaderboardEntry | None = None,
    ) -> PvpDefenseSnapshotBundle:
        """确保角色存在可用的防守快照，并返回战斗可用投影。"""
        aggregate = self._require_character_aggregate(character_id)
        progress = self._require_progress(aggregate)
        score_snapshot = self._require_score_snapshot(aggregate)
        resolved_leaderboard_entry = leaderboard_entry or self._get_leaderboard_entry(character_id)
        latest_snapshot_state = self.get_latest_snapshot_state(character_id)
        build_fingerprint = self._build_fingerprint(
            aggregate=aggregate,
            score_snapshot=score_snapshot,
        )
        usage_decision = self._rule_service.decide_defense_snapshot_usage(
            current_snapshot=latest_snapshot_state,
            now=now,
            build_fingerprint=build_fingerprint,
            requested_reason=requested_reason,
            score_version=score_snapshot.score_version,
        )
        if usage_decision.reuse_existing:
            active_snapshot_state = self.get_active_snapshot_state(character_id=character_id, now=now)
            if active_snapshot_state is None:
                raise PvpDefenseSnapshotStateError(f"角色缺少可复用的有效防守快照：{character_id}")
            return self._build_bundle(
                snapshot_state=active_snapshot_state,
                usage_decision=usage_decision,
                fallback_unit_name=aggregate.character.name,
            )

        current_attributes = self._current_attribute_service.get_pvp_view(character_id=character_id)
        snapshot_model = self._build_snapshot_model(
            aggregate=aggregate,
            progress=progress,
            score_snapshot=score_snapshot,
            leaderboard_entry=resolved_leaderboard_entry,
            build_fingerprint=build_fingerprint,
            usage_decision=usage_decision,
            current_attributes=current_attributes,
        )
        persisted_snapshot = self._snapshot_repository.add_pvp_defense_snapshot(snapshot_model)
        snapshot_state = self._to_snapshot_state(persisted_snapshot)
        return self._build_bundle(
            snapshot_state=snapshot_state,
            usage_decision=usage_decision,
            fallback_unit_name=aggregate.character.name,
        )

    def get_active_snapshot_state(self, *, character_id: int, now: datetime) -> PvpDefenseSnapshotState | None:
        """读取角色当前仍在锁定期内的快照状态。"""
        snapshot_model = self._snapshot_repository.get_active_pvp_defense_snapshot(character_id, now)
        return None if snapshot_model is None else self._to_snapshot_state(snapshot_model)

    def get_latest_snapshot_state(self, character_id: int) -> PvpDefenseSnapshotState | None:
        """读取角色最新一版防守快照状态。"""
        snapshot_model = self._snapshot_repository.get_latest_pvp_defense_snapshot(character_id)
        return None if snapshot_model is None else self._to_snapshot_state(snapshot_model)

    def build_enemy_battle_unit(
        self,
        *,
        snapshot_state: PvpDefenseSnapshotState,
        fallback_unit_name: str | None = None,
    ) -> BattleUnitSnapshot:
        """把防守快照投影为 PVP 敌方战斗单位。"""
        stats_payload = snapshot_state.stats if isinstance(snapshot_state.stats, dict) else {}
        summary_payload = snapshot_state.summary if isinstance(snapshot_state.summary, dict) else {}
        snapshot_version = snapshot_state.snapshot_version or 0
        unit_name = str(summary_payload.get("character_name") or fallback_unit_name or f"守阵者{snapshot_state.character_id}")
        template_id = str(stats_payload.get("behavior_template_id") or "zhanqing_sword")
        special_effect_payloads = stats_payload.get("special_effect_payloads")
        normalized_special_effect_payloads = tuple(
            dict(payload)
            for payload in special_effect_payloads
            if isinstance(payload, dict)
        ) if isinstance(special_effect_payloads, list) else ()
        return BattleUnitSnapshot(
            unit_id=f"pvp:defender:{snapshot_state.character_id}:v{snapshot_version}",
            unit_name=unit_name,
            side=BattleSide.ENEMY,
            behavior_template_id=template_id or str(stats_payload.get("behavior_template_id") or "zhanqing_sword"),
            realm_id=str(stats_payload.get("realm_id") or summary_payload.get("realm_id") or "mortal"),
            stage_id=str(stats_payload.get("stage_id") or summary_payload.get("stage_id") or "early"),
            max_hp=_read_int(stats_payload.get("max_hp"), default=1),
            current_hp=_read_int(stats_payload.get("current_hp"), default=_read_int(stats_payload.get("max_hp"), default=1)),
            current_shield=_read_int(stats_payload.get("current_shield"), default=0),
            max_resource=_read_int(stats_payload.get("max_resource"), default=100),
            current_resource=_read_int(stats_payload.get("current_resource"), default=_read_int(stats_payload.get("max_resource"), default=100)),
            attack_power=_read_int(stats_payload.get("attack_power"), default=1),
            guard_power=_read_int(stats_payload.get("guard_power"), default=0),
            speed=_read_int(stats_payload.get("speed"), default=1),
            crit_rate_permille=_read_int(stats_payload.get("crit_rate_permille"), default=0),
            crit_damage_bonus_permille=_read_int(stats_payload.get("crit_damage_bonus_permille"), default=0),
            hit_rate_permille=_read_int(stats_payload.get("hit_rate_permille"), default=1000),
            dodge_rate_permille=_read_int(stats_payload.get("dodge_rate_permille"), default=0),
            control_bonus_permille=_read_int(stats_payload.get("control_bonus_permille"), default=0),
            control_resist_permille=_read_int(stats_payload.get("control_resist_permille"), default=0),
            healing_power_permille=_read_int(stats_payload.get("healing_power_permille"), default=0),
            shield_power_permille=_read_int(stats_payload.get("shield_power_permille"), default=0),
            damage_bonus_permille=_read_int(stats_payload.get("damage_bonus_permille"), default=0),
            damage_reduction_permille=_read_int(stats_payload.get("damage_reduction_permille"), default=0),
            counter_rate_permille=_read_int(stats_payload.get("counter_rate_permille"), default=0),
            special_effect_payloads=normalized_special_effect_payloads,
        )

    def build_display_summary(self, *, snapshot_state: PvpDefenseSnapshotState) -> dict[str, object]:
        """构造展示层可直接消费的快照摘要。"""
        summary_payload = dict(snapshot_state.summary) if isinstance(snapshot_state.summary, dict) else {}
        summary_payload.update(
            {
                "snapshot_id": snapshot_state.snapshot_id,
                "snapshot_version": snapshot_state.snapshot_version,
                "snapshot_reason": snapshot_state.snapshot_reason,
                "rank_position": snapshot_state.rank_position,
                "public_power_score": snapshot_state.public_power_score,
                "hidden_pvp_score": snapshot_state.hidden_pvp_score,
                "score_version": snapshot_state.score_version,
                "lock_started_at": None if snapshot_state.lock_started_at is None else snapshot_state.lock_started_at.isoformat(),
                "lock_expires_at": None if snapshot_state.lock_expires_at is None else snapshot_state.lock_expires_at.isoformat(),
            }
        )
        return summary_payload

    def _build_bundle(
        self,
        *,
        snapshot_state: PvpDefenseSnapshotState,
        usage_decision: PvpDefenseSnapshotUsageDecision,
        fallback_unit_name: str,
    ) -> PvpDefenseSnapshotBundle:
        battle_unit_snapshot = self.build_enemy_battle_unit(
            snapshot_state=snapshot_state,
            fallback_unit_name=fallback_unit_name,
        )
        return PvpDefenseSnapshotBundle(
            snapshot_state=snapshot_state,
            usage_decision=usage_decision,
            battle_unit_snapshot=battle_unit_snapshot,
            display_summary=self.build_display_summary(snapshot_state=snapshot_state),
        )

    def _build_snapshot_model(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
        score_snapshot: CharacterScoreSnapshot,
        leaderboard_entry: PvpLeaderboardEntry | None,
        build_fingerprint: str,
        usage_decision: PvpDefenseSnapshotUsageDecision,
        current_attributes,
    ) -> PvpDefenseSnapshot:
        formation_payload = self._build_formation_payload(
            aggregate=aggregate,
            progress=progress,
            score_snapshot=score_snapshot,
            current_attributes=current_attributes,
        )
        stats_payload = self._build_stats_payload(current_attributes=current_attributes)
        summary_payload = self._build_summary_payload(
            aggregate=aggregate,
            progress=progress,
            score_snapshot=score_snapshot,
            formation_payload=formation_payload,
            usage_decision=usage_decision,
            leaderboard_entry=leaderboard_entry,
            build_fingerprint=build_fingerprint,
        )
        return PvpDefenseSnapshot(
            character_id=aggregate.character.id,
            snapshot_version=usage_decision.target_snapshot_version,
            power_score=aggregate.character.total_power_score,
            public_power_score=aggregate.character.public_power_score,
            hidden_pvp_score=aggregate.character.hidden_pvp_score,
            score_version=score_snapshot.score_version,
            snapshot_reason=usage_decision.resolved_reason,
            build_fingerprint=build_fingerprint,
            rank_position=None if leaderboard_entry is None else leaderboard_entry.rank_position,
            formation_json=formation_payload,
            stats_json=stats_payload,
            summary_json=summary_payload,
            source_updated_at=score_snapshot.computed_at,
            lock_started_at=usage_decision.lock_started_at,
            lock_expires_at=usage_decision.lock_expires_at,
        )

    def _build_formation_payload(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
        score_snapshot: CharacterScoreSnapshot,
        current_attributes,
    ) -> dict[str, Any]:
        equipped_items = self._list_equipped_items(aggregate)
        source_summary = self._resolve_source_summary(score_snapshot)
        equipped_payload = [self._serialize_equipped_item(item) for item in equipped_items]
        artifact_payload = [item for item in equipped_payload if bool(item.get("is_artifact"))]
        loadout = current_attributes.skill_loadout
        return {
            "character_id": aggregate.character.id,
            "character_name": aggregate.character.name,
            "realm_id": progress.realm_id,
            "stage_id": progress.stage_id,
            "behavior_template_id": current_attributes.behavior_template_id,
            "main_axis_id": loadout.main_axis_id,
            "main_path_id": loadout.main_path_id,
            "main_path_name": loadout.main_skill.skill_name,
            "guard_skill_id": loadout.guard_skill.item_id,
            "movement_skill_id": loadout.movement_skill.item_id,
            "spirit_skill_id": loadout.spirit_skill.item_id,
            "skill_loadout_version": loadout.config_version,
            "equipped_items": equipped_payload,
            "artifact_items": artifact_payload,
            "equipped_item_count": len(equipped_payload),
            "artifact_count": len(artifact_payload),
            "score_source_summary": source_summary,
        }

    def _build_stats_payload(
        self,
        *,
        current_attributes,
    ) -> dict[str, Any]:
        return current_attributes.to_stats_payload(
            current_hp_ratio=Decimal("1.0000"),
            current_mp_ratio=Decimal("1.0000"),
            current_shield=0,
        )

    def _build_summary_payload(
        self,
        *,
        aggregate: CharacterAggregate,
        progress: CharacterProgress,
        score_snapshot: CharacterScoreSnapshot,
        formation_payload: dict[str, Any],
        usage_decision: PvpDefenseSnapshotUsageDecision,
        leaderboard_entry: PvpLeaderboardEntry | None,
        build_fingerprint: str,
    ) -> dict[str, Any]:
        main_path_name = formation_payload.get("main_path_name")
        if not isinstance(main_path_name, str) or not main_path_name:
            main_path_name = str(formation_payload.get("main_path_id") or "未定功法")
        realm_name = self._realm_name_by_id.get(progress.realm_id, progress.realm_id)
        stage_name = self._stage_name_by_id.get(progress.stage_id, progress.stage_id)
        display_summary = (
            f"{realm_name}{stage_name}｜{main_path_name}｜战力 {aggregate.character.public_power_score}｜"
            f"装备 {formation_payload.get('equipped_item_count', 0)} 件"
        )
        source_summary = self._resolve_source_summary(score_snapshot)
        return {
            "character_name": aggregate.character.name,
            "character_title": aggregate.character.title,
            "realm_id": progress.realm_id,
            "realm_name": realm_name,
            "stage_id": progress.stage_id,
            "stage_name": stage_name,
            "main_path_id": formation_payload.get("main_path_id"),
            "main_path_name": main_path_name,
            "equipped_item_count": formation_payload.get("equipped_item_count", 0),
            "artifact_count": formation_payload.get("artifact_count", 0),
            "public_power_score": aggregate.character.public_power_score,
            "hidden_pvp_score": aggregate.character.hidden_pvp_score,
            "score_version": score_snapshot.score_version,
            "score_source_digest": score_snapshot.source_digest,
            "skill_loadout_version": formation_payload.get("skill_loadout_version"),
            "display_summary": display_summary,
            "snapshot_reason": usage_decision.resolved_reason,
            "snapshot_reason_code": usage_decision.reason_code,
            "build_fingerprint": build_fingerprint,
            "leaderboard_rank_position": None if leaderboard_entry is None else leaderboard_entry.rank_position,
            "source_summary": source_summary,
        }

    def _get_leaderboard_entry(self, character_id: int) -> PvpLeaderboardEntry | None:
        entry_model = self._snapshot_repository.get_latest_leaderboard_entry(
            LeaderboardBoardType.PVP_CHALLENGE.value,
            character_id,
        )
        if entry_model is None:
            return None
        summary = dict(entry_model.summary_json) if isinstance(entry_model.summary_json, dict) else {}
        return PvpLeaderboardEntry(
            character_id=entry_model.character_id,
            rank_position=entry_model.rank_position,
            public_power_score=_read_int(summary.get("public_power_score"), default=0),
            hidden_pvp_score=entry_model.score,
            realm_id=str(summary.get("realm_id") or "mortal"),
            best_rank=_read_optional_int(summary.get("best_rank")),
            protected_until=_read_optional_datetime(summary.get("protected_until")),
            latest_defense_snapshot_version=_read_optional_int(summary.get("latest_defense_snapshot_version")),
            challenge_tier=_read_optional_str(summary.get("challenge_tier")),
            reward_preview_tier=_read_optional_str(summary.get("reward_preview_tier")),
            summary=summary,
        )

    def _build_fingerprint(
        self,
        *,
        aggregate: CharacterAggregate,
        score_snapshot: CharacterScoreSnapshot,
    ) -> str:
        current_attributes = self._current_attribute_service.get_pvp_view(character_id=aggregate.character.id)
        equipped_items = self._list_equipped_items(aggregate)
        payload = {
            "character_id": aggregate.character.id,
            "realm_id": current_attributes.realm_id,
            "stage_id": current_attributes.stage_id,
            "score_version": score_snapshot.score_version,
            "score_source_digest": score_snapshot.source_digest,
            "public_power_score": score_snapshot.public_power_score,
            "hidden_pvp_score": score_snapshot.hidden_pvp_score,
            "main_path_id": current_attributes.main_path_id,
            "behavior_template_id": current_attributes.behavior_template_id,
            "skill_loadout_version": current_attributes.skill_loadout.config_version,
            "skill_item_ids": {
                "main": current_attributes.skill_loadout.main_skill.item_id,
                "guard": current_attributes.skill_loadout.guard_skill.item_id,
                "movement": current_attributes.skill_loadout.movement_skill.item_id,
                "spirit": current_attributes.skill_loadout.spirit_skill.item_id,
            },
            "template_patch_payloads": [dict(payload) for payload in current_attributes.template_patch_payloads],
            "stats": current_attributes.to_stats_payload(
                current_hp_ratio=Decimal("1.0000"),
                current_mp_ratio=Decimal("1.0000"),
                current_shield=0,
            ),
            "equipped_item_signatures": [
                {
                    "item_id": item.id,
                    "slot_id": item.slot_id,
                    "equipped_slot_id": item.equipped_slot_id,
                    "template_id": item.template_id,
                    "quality_id": item.quality_id,
                    "enhancement_level": 0 if item.enhancement is None else item.enhancement.enhancement_level,
                    "artifact_refinement_level": 0 if item.artifact_profile is None else item.artifact_profile.refinement_level,
                    "artifact_nurture_level": 0 if item.artifact_nurture_state is None else item.artifact_nurture_state.nurture_level,
                    "affix_signature": [
                        {
                            "affix_id": affix.affix_id,
                            "tier_id": affix.tier_id,
                            "value": affix.value,
                            "is_pvp_specialized": affix.is_pvp_specialized,
                        }
                        for affix in sorted(item.affixes, key=lambda current: current.position)
                    ],
                }
                for item in equipped_items
            ],
        }
        serialized_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _list_equipped_items(aggregate: CharacterAggregate) -> tuple[EquipmentItemModel, ...]:
        equipped_items = [
            item
            for item in aggregate.equipment_items
            if item.item_state == _ACTIVE_ITEM_STATE and item.equipped_slot_id is not None
        ]
        equipped_items.sort(key=lambda current: (str(current.equipped_slot_id), current.id))
        return tuple(equipped_items)

    @staticmethod
    def _serialize_equipped_item(item: EquipmentItemModel) -> dict[str, object]:
        return {
            "item_id": item.id,
            "slot_id": item.slot_id,
            "slot_name": item.slot_name,
            "equipped_slot_id": item.equipped_slot_id,
            "quality_id": item.quality_id,
            "quality_name": item.quality_name,
            "template_id": item.template_id,
            "template_name": item.template_name,
            "item_name": item.item_name,
            "is_artifact": item.is_artifact,
            "resonance_name": item.resonance_name,
            "enhancement_level": 0 if item.enhancement is None else item.enhancement.enhancement_level,
            "artifact_refinement_level": 0 if item.artifact_profile is None else item.artifact_profile.refinement_level,
            "artifact_nurture_level": 0 if item.artifact_nurture_state is None else item.artifact_nurture_state.nurture_level,
            "affixes": [
                {
                    "affix_id": affix.affix_id,
                    "affix_name": affix.affix_name,
                    "tier_id": affix.tier_id,
                    "tier_name": affix.tier_name,
                    "value": affix.value,
                    "is_pvp_specialized": affix.is_pvp_specialized,
                }
                for affix in sorted(item.affixes, key=lambda current: current.position)
            ],
        }

    @staticmethod
    def _resolve_source_summary(score_snapshot: CharacterScoreSnapshot) -> dict[str, Any]:
        breakdown = score_snapshot.breakdown_json if isinstance(score_snapshot.breakdown_json, dict) else {}
        source_summary = breakdown.get("source_summary")
        return dict(source_summary) if isinstance(source_summary, dict) else {}

    @staticmethod
    def _resolve_skill_breakdown(score_snapshot: CharacterScoreSnapshot) -> dict[str, Any]:
        breakdown = score_snapshot.breakdown_json if isinstance(score_snapshot.breakdown_json, dict) else {}
        skill_breakdown = breakdown.get("skill")
        return dict(skill_breakdown) if isinstance(skill_breakdown, dict) else {}

    @staticmethod
    def _resolve_behavior_template_id(skill_loadout: CharacterSkillLoadout | None) -> str:
        if skill_loadout is None:
            return _DEFAULT_HERO_TEMPLATE_ID
        template_id = skill_loadout.behavior_template_id.strip()
        if not template_id:
            return _DEFAULT_HERO_TEMPLATE_ID
        if template_id not in _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID:
            return _DEFAULT_HERO_TEMPLATE_ID
        return template_id

    @staticmethod
    def _resolve_template_profile(template_id: str) -> dict[str, Decimal | int]:
        try:
            return _PATH_COMBAT_PROFILE_BY_TEMPLATE_ID[template_id]
        except KeyError as exc:
            raise PvpDefenseSnapshotStateError(f"未支持的行为模板画像：{template_id}") from exc

    def _calculate_base_hp(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_hp,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("12"),
            factor=factor,
            minimum=1,
        )

    def _calculate_base_attack(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_attack,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("2"),
            factor=factor,
            minimum=1,
        )

    def _calculate_base_guard(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        return self._calculate_scaled_stat(
            base_value=self._static_config.base_coefficients.scalar.base_defense,
            realm_id=realm_id,
            stage_id=stage_id,
            divisor=Decimal("4"),
            factor=factor,
            minimum=0,
        )

    def _calculate_base_speed(self, *, realm_id: str, stage_id: str, factor: Decimal) -> int:
        base_speed = Decimal(self._static_config.base_coefficients.scalar.base_speed)
        realm_coefficient = self._resolve_realm_coefficient(realm_id)
        stage_multiplier = self._resolve_stage_multiplier(stage_id)
        scaled_value = (base_speed + realm_coefficient * Decimal("2")) * stage_multiplier * factor
        return max(1, _round_decimal_to_int(scaled_value))

    def _calculate_scaled_stat(
        self,
        *,
        base_value: int,
        realm_id: str,
        stage_id: str,
        divisor: Decimal,
        factor: Decimal,
        minimum: int,
    ) -> int:
        realm_coefficient = self._resolve_realm_coefficient(realm_id)
        stage_multiplier = self._resolve_stage_multiplier(stage_id)
        scaled_value = Decimal(base_value) * realm_coefficient * stage_multiplier * factor / divisor
        return max(minimum, _round_decimal_to_int(scaled_value))

    def _resolve_realm_coefficient(self, realm_id: str) -> Decimal:
        try:
            return self._realm_coefficient_by_realm_id[realm_id]
        except KeyError as exc:
            raise PvpDefenseSnapshotStateError(f"未找到大境界基准系数：{realm_id}") from exc

    def _resolve_stage_multiplier(self, stage_id: str) -> Decimal:
        try:
            return self._stage_multiplier_by_stage_id[stage_id]
        except KeyError as exc:
            raise PvpDefenseSnapshotStateError(f"未找到小阶段倍率：{stage_id}") from exc

    @staticmethod
    def _to_snapshot_state(snapshot_model: PvpDefenseSnapshot) -> PvpDefenseSnapshotState:
        return PvpDefenseSnapshotState(
            character_id=snapshot_model.character_id,
            snapshot_id=snapshot_model.id,
            snapshot_version=snapshot_model.snapshot_version,
            build_fingerprint=snapshot_model.build_fingerprint,
            snapshot_reason=snapshot_model.snapshot_reason,
            score_version=snapshot_model.score_version,
            rank_position=snapshot_model.rank_position,
            public_power_score=snapshot_model.public_power_score,
            hidden_pvp_score=snapshot_model.hidden_pvp_score,
            lock_started_at=snapshot_model.lock_started_at,
            lock_expires_at=snapshot_model.lock_expires_at,
            formation=dict(snapshot_model.formation_json) if isinstance(snapshot_model.formation_json, dict) else {},
            stats=dict(snapshot_model.stats_json) if isinstance(snapshot_model.stats_json, dict) else {},
            summary=dict(snapshot_model.summary_json) if isinstance(snapshot_model.summary_json, dict) else {},
        )

    def _require_character_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise CharacterNotFoundError(f"角色不存在：{character_id}")
        return aggregate

    @staticmethod
    def _require_progress(aggregate: CharacterAggregate) -> CharacterProgress:
        if aggregate.progress is None:
            raise CharacterGrowthStateError(f"角色缺少成长状态：{aggregate.character.id}")
        return aggregate.progress

    @staticmethod
    def _require_score_snapshot(aggregate: CharacterAggregate) -> CharacterScoreSnapshot:
        if aggregate.score_snapshot is None:
            raise PvpDefenseSnapshotStateError(f"角色缺少评分明细快照：{aggregate.character.id}")
        return aggregate.score_snapshot



def _read_decimal(value: object, *, default: Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception:
            return default
    return default



def _read_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default



def _read_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None



def _read_optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value
    return None



def _read_optional_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None



def _round_decimal_to_int(value: Decimal) -> int:
    return int(value.quantize(_DECIMAL_ONE, rounding=ROUND_HALF_UP))


__all__ = [
    "PvpDefenseSnapshotBundle",
    "PvpDefenseSnapshotService",
    "PvpDefenseSnapshotServiceError",
    "PvpDefenseSnapshotStateError",
]
