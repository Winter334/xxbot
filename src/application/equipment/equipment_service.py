"""阶段 6 装备应用服务。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import random
from typing import TypeVar

from domain.character import resolve_spirit_stone_economy_multiplier
from domain.equipment import (
    ArtifactNurtureRule,
    EquipmentAffixOperationRule,
    EquipmentAffixValue,
    EquipmentAttributeValue,
    EquipmentDismantleRule,
    EquipmentEnhancementRule,
    EquipmentGenerationRequest,
    EquipmentGenerationRule,
    EquipmentItem as DomainEquipmentItem,
    EquipmentNamingRecord,
    EquipmentNamingService,
    EquipmentResourceCost,
    EquipmentRuleError,
    EquipmentSpecialEffectValue,
    TemplateEquipmentNamingService,
)
from application.ranking.score_service import CharacterScoreService
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import (
    ArtifactNurtureState,
    ArtifactProfile,
    CurrencyBalance,
    EquipmentAffix,
    EquipmentDismantleRecord,
    EquipmentEnhancement,
    EquipmentItem as EquipmentItemModel,
    EquipmentNamingState,
    InventoryItem,
)
from infrastructure.db.repositories import (
    CharacterAggregate,
    CharacterRepository,
    EquipmentRepository,
    InventoryRepository,
)

_ITEM_STATE_ACTIVE = "active"
_ITEM_STATE_DISMANTLED = "dismantled"
_RESOURCE_KIND_CURRENCY = "currency"
_RESOURCE_KIND_MATERIAL = "material"
_RESOURCE_CHANGE_CONSUME = "consume"
_RESOURCE_CHANGE_GRANT = "grant"
_SPIRIT_STONE_RESOURCE_ID = "spirit_stone"
_DISMANTLE_AUDIT_SOURCE = "equipment_dismantle"
_DEFAULT_EQUIPMENT_RANK_ID = "mortal"

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class EquipmentAttributeSnapshot:
    """装备基础属性快照。"""

    stat_id: str
    value: int


@dataclass(frozen=True, slots=True)
class EquipmentSpecialEffectSnapshot:
    """装备特殊效果快照。"""

    effect_id: str
    effect_name: str
    effect_type: str
    trigger_event: str
    payload: dict[str, str | int | bool | None]
    public_score_key: str | None
    hidden_pvp_score_key: str | None


@dataclass(frozen=True, slots=True)
class EquipmentAffixSnapshot:
    """装备词条快照。"""

    affix_id: str
    affix_name: str
    stat_id: str
    category: str
    tier_id: str
    tier_name: str
    rolled_multiplier: Decimal
    value: int
    is_pve_specialized: bool
    is_pvp_specialized: bool
    affix_kind: str = "numeric"
    special_effect: EquipmentSpecialEffectSnapshot | None = None
    position: int | None = None


@dataclass(frozen=True, slots=True)
class EquipmentResolvedStatSnapshot:
    """装备最终属性快照。"""

    stat_id: str
    value: int


@dataclass(frozen=True, slots=True)
class EquipmentNamingSnapshot:
    """装备命名快照。"""

    resolved_name: str
    naming_template_id: str
    naming_source: str
    naming_metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class EquipmentResourceLedgerEntry:
    """一次应用服务操作的资源变化记录。"""

    resource_id: str
    resource_kind: str
    change_type: str
    quantity: int
    before_quantity: int
    after_quantity: int


@dataclass(frozen=True, slots=True)
class EquipmentItemSnapshot:
    """单件装备或法宝的应用层快照。"""

    item_id: int
    character_id: int
    slot_id: str
    slot_name: str
    equipped_slot_id: str | None
    quality_id: str
    quality_name: str
    template_id: str
    template_name: str
    rank_id: str
    rank_name: str
    rank_order: int
    mapped_realm_id: str
    is_artifact: bool
    resonance_name: str | None
    item_state: str
    display_name: str
    enhancement_level: int
    artifact_nurture_level: int
    enhancement_success_count: int
    enhancement_failure_count: int
    base_attribute_multiplier: Decimal
    affix_base_value_multiplier: Decimal
    dismantle_reward_multiplier: Decimal
    enhancement_base_stat_bonus_ratio: Decimal
    enhancement_affix_bonus_ratio: Decimal
    nurture_base_stat_bonus_ratio: Decimal
    nurture_affix_bonus_ratio: Decimal
    base_stat_bonus_ratio: Decimal
    affix_bonus_ratio: Decimal
    base_attributes: tuple[EquipmentAttributeSnapshot, ...]
    affixes: tuple[EquipmentAffixSnapshot, ...]
    resolved_stats: tuple[EquipmentResolvedStatSnapshot, ...]
    naming: EquipmentNamingSnapshot | None
    dismantled_at: datetime | None


@dataclass(frozen=True, slots=True)
class EquipmentCollectionSnapshot:
    """角色装备查询结果。"""

    character_id: int
    spirit_stone: int
    active_items: tuple[EquipmentItemSnapshot, ...]
    equipped_items: tuple[EquipmentItemSnapshot, ...]
    dismantled_items: tuple[EquipmentItemSnapshot, ...]


@dataclass(frozen=True, slots=True)
class EquipmentGenerationApplicationResult:
    """装备生成后的应用层结果。"""

    item: EquipmentItemSnapshot


@dataclass(frozen=True, slots=True)
class EquipmentEquipApplicationResult:
    """装备穿戴后的应用层结果。"""

    item: EquipmentItemSnapshot
    previous_item: EquipmentItemSnapshot | None
    equipped_slot_id: str


@dataclass(frozen=True, slots=True)
class EquipmentUnequipApplicationResult:
    """装备卸下后的应用层结果。"""

    item: EquipmentItemSnapshot
    unequipped_slot_id: str


@dataclass(frozen=True, slots=True)
class EquipmentEnhancementApplicationResult:
    """装备强化后的应用层结果。"""

    item: EquipmentItemSnapshot
    success: bool
    previous_level: int
    target_level: int
    success_rate: Decimal
    added_affixes: tuple[EquipmentAffixSnapshot, ...]
    resource_changes: tuple[EquipmentResourceLedgerEntry, ...]


@dataclass(frozen=True, slots=True)
class EquipmentWashApplicationResult:
    """装备洗炼后的应用层结果。"""

    item: EquipmentItemSnapshot
    locked_affix_indices: tuple[int, ...]
    rerolled_affixes: tuple[EquipmentAffixSnapshot, ...]
    resource_changes: tuple[EquipmentResourceLedgerEntry, ...]


@dataclass(frozen=True, slots=True)
class EquipmentReforgeApplicationResult:
    """装备重铸后的应用层结果。"""

    item: EquipmentItemSnapshot
    previous_template_id: str
    previous_affixes: tuple[EquipmentAffixSnapshot, ...]
    resource_changes: tuple[EquipmentResourceLedgerEntry, ...]


@dataclass(frozen=True, slots=True)
class ArtifactNurtureApplicationResult:
    """法宝培养后的应用层结果。"""

    item: EquipmentItemSnapshot
    previous_level: int
    target_level: int
    resource_changes: tuple[EquipmentResourceLedgerEntry, ...]


@dataclass(frozen=True, slots=True)
class EquipmentDismantleApplicationResult:
    """装备分解后的应用层结果。"""

    item: EquipmentItemSnapshot
    resource_changes: tuple[EquipmentResourceLedgerEntry, ...]
    settled_at: datetime


class EquipmentServiceError(RuntimeError):
    """装备应用服务基础异常。"""


class EquipmentCharacterNotFoundError(EquipmentServiceError):
    """角色不存在。"""


class EquipmentCharacterStateError(EquipmentServiceError):
    """角色装备上下文不完整。"""


class EquipmentNotFoundError(EquipmentServiceError):
    """装备不存在。"""


class EquipmentOwnershipError(EquipmentServiceError):
    """装备不属于当前角色。"""


class EquipmentOperationStateError(EquipmentServiceError):
    """装备当前状态不允许执行目标操作。"""


class EquipmentSlotNotFoundError(EquipmentServiceError):
    """装备部位未配置。"""


class EquipmentUnequipTargetNotFoundError(EquipmentServiceError):
    """指定部位当前没有已装备物品。"""


class EquipmentResourceInsufficientError(EquipmentServiceError):
    """执行装备操作所需资源不足。"""


class EquipmentRuleViolationError(EquipmentServiceError):
    """领域层装备规则校验失败。"""


class EquipmentService:
    """负责编排阶段 6 装备、强化、洗炼、重铸、法宝培养、分解与查询。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        equipment_repository: EquipmentRepository,
        inventory_repository: InventoryRepository,
        static_config: StaticGameConfig | None = None,
        naming_service: EquipmentNamingService | None = None,
        score_service: CharacterScoreService | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._equipment_repository = equipment_repository
        self._inventory_repository = inventory_repository
        self._static_config = static_config or get_static_config()
        self._generation_rule = EquipmentGenerationRule(self._static_config)
        self._enhancement_rule = EquipmentEnhancementRule(self._static_config, self._generation_rule)
        self._affix_operation_rule = EquipmentAffixOperationRule(self._static_config, self._generation_rule)
        self._artifact_nurture_rule = ArtifactNurtureRule(self._static_config)
        self._dismantle_rule = EquipmentDismantleRule(self._static_config)
        self._naming_service = naming_service or TemplateEquipmentNamingService(self._static_config)
        self._score_service = score_service
        self._slot_by_id = {slot.slot_id: slot for slot in self._static_config.equipment.ordered_slots}
        self._ordered_ranks = tuple(self._static_config.equipment.ordered_equipment_ranks)
        self._ordered_qualities = tuple(self._static_config.equipment.ordered_qualities)
        self._non_artifact_slot_ids = tuple(slot.slot_id for slot in self._static_config.equipment.ordered_slots if slot.slot_id != "artifact")
        self._default_rank = self._static_config.equipment.get_equipment_rank(_DEFAULT_EQUIPMENT_RANK_ID)
        if self._default_rank is None:
            raise ValueError(f"缺少默认装备阶数定义：{_DEFAULT_EQUIPMENT_RANK_ID}")
        if not self._ordered_ranks:
            raise ValueError("缺少装备阶数定义，无法生成无尽结算掉落")
        if not self._ordered_qualities:
            raise ValueError("缺少装备品质定义，无法生成无尽结算掉落")
        if not self._non_artifact_slot_ids:
            raise ValueError("缺少普通装备部位定义，无法生成无尽结算掉落")

    def generate_equipment(
        self,
        *,
        character_id: int,
        slot_id: str,
        quality_id: str,
        rank_id: str = _DEFAULT_EQUIPMENT_RANK_ID,
        template_id: str | None = None,
        affix_count: int | None = None,
        seed: int | None = None,
    ) -> EquipmentGenerationApplicationResult:
        """生成并落库一件新装备或法宝。"""
        self._require_aggregate(character_id)
        generated_item = self._execute_rule(
            lambda: self._generation_rule.generate_equipment(
                request=EquipmentGenerationRequest(
                    slot_id=slot_id,
                    quality_id=quality_id,
                    rank_id=rank_id,
                    template_id=template_id,
                    affix_count=affix_count,
                ),
                random_source=self._build_random_source(seed),
            )
        )
        named_item = self._ensure_named_item(generated_item, force_refresh=True)
        equipment_model = self._new_equipment_model(character_id=character_id, domain_item=named_item)
        persisted_model = self._equipment_repository.add(equipment_model)
        return EquipmentGenerationApplicationResult(item=self._build_item_snapshot(persisted_model))

    def generate_endless_settlement_item(
        self,
        *,
        character_id: int,
        score: int,
        floor: int,
        is_artifact: bool,
        seed: int | None = None,
    ) -> EquipmentGenerationApplicationResult | None:
        """按无尽结算保留分数生成一件同步命名掉落实例。"""
        if score <= 0:
            return None
        slot_id = "artifact" if is_artifact else self._resolve_endless_reward_slot_id(seed=seed)
        quality_id = self._resolve_endless_reward_quality_id(score=score)
        rank_id = self._resolve_endless_reward_rank_id(floor=floor)
        return self.generate_equipment(
            character_id=character_id,
            slot_id=slot_id,
            quality_id=quality_id,
            rank_id=rank_id,
            seed=seed,
        )

    def equip_item(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
    ) -> EquipmentEquipApplicationResult:
        """尝试将一件候选装备或法宝穿戴到其对应部位。"""
        self._require_aggregate(character_id)
        equipment_model = self._require_active_equipment(character_id=character_id, equipment_item_id=equipment_item_id)
        target_slot_id = equipment_model.slot_id
        previous_model = self._equipment_repository.get_equipped_in_slot(character_id, target_slot_id)
        previous_snapshot = None if previous_model is None else self._build_item_snapshot(previous_model)
        if previous_model is not None and previous_model.id != equipment_model.id:
            previous_model.equipped_slot_id = None
            self._equipment_repository.save(previous_model)
        equipment_model.equipped_slot_id = target_slot_id
        persisted_model = self._equipment_repository.save(equipment_model)
        self._refresh_score_if_configured(character_id)
        return EquipmentEquipApplicationResult(
            item=self._build_item_snapshot(persisted_model),
            previous_item=None if previous_model is not None and previous_model.id == persisted_model.id else previous_snapshot,
            equipped_slot_id=target_slot_id,
        )

    def unequip_item(
        self,
        *,
        character_id: int,
        equipped_slot_id: str,
    ) -> EquipmentUnequipApplicationResult:
        """按装备位卸下当前已穿戴的装备或法宝。"""
        self._require_aggregate(character_id)
        normalized_slot_id = equipped_slot_id.strip()
        if normalized_slot_id not in self._slot_by_id:
            raise EquipmentSlotNotFoundError(f"未配置的装备部位：{equipped_slot_id}")
        equipment_model = self._equipment_repository.get_equipped_in_slot(character_id, normalized_slot_id)
        if equipment_model is None:
            raise EquipmentUnequipTargetNotFoundError(f"部位 {normalized_slot_id} 当前没有已装备物品")
        equipment_model.equipped_slot_id = None
        persisted_model = self._equipment_repository.save(equipment_model)
        self._refresh_score_if_configured(character_id)
        return EquipmentUnequipApplicationResult(
            item=self._build_item_snapshot(persisted_model),
            unequipped_slot_id=normalized_slot_id,
        )

    def enhance_equipment(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
        seed: int | None = None,
    ) -> EquipmentEnhancementApplicationResult:
        """执行一次装备强化，显式处理资源扣减与结果落库。"""
        aggregate = self._require_aggregate(character_id)
        equipment_model = self._require_active_equipment(character_id=character_id, equipment_item_id=equipment_item_id)
        domain_item = self._to_domain_item(equipment_model)
        enhancement_result = self._execute_rule(
            lambda: self._enhancement_rule.enhance(
                item=domain_item,
                random_source=self._build_random_source(seed),
            )
        )
        resource_changes = self._consume_resources(
            character_id=character_id,
            currency_balance=aggregate.currency_balance,
            resource_costs=enhancement_result.costs,
        )
        updated_item = enhancement_result.item
        if enhancement_result.success:
            updated_item = self._ensure_named_item(updated_item, force_refresh=True)
        self._sync_equipment_model(
            equipment_model=equipment_model,
            domain_item=updated_item,
            success_count_delta=1 if enhancement_result.success else 0,
            failure_count_delta=0 if enhancement_result.success else 1,
        )
        persisted_model = self._equipment_repository.save(equipment_model)
        self._refresh_score_if_equipped(character_id=character_id, equipment_model=persisted_model)
        return EquipmentEnhancementApplicationResult(
            item=self._build_item_snapshot(persisted_model),
            success=enhancement_result.success,
            previous_level=enhancement_result.previous_level,
            target_level=enhancement_result.target_level,
            success_rate=enhancement_result.success_rate,
            added_affixes=self._build_affix_value_snapshots(enhancement_result.added_affixes),
            resource_changes=resource_changes,
        )

    def wash_equipment(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
        locked_affix_indices: tuple[int, ...] = (),
        seed: int | None = None,
    ) -> EquipmentWashApplicationResult:
        """执行一次装备洗炼，并显式处理资源扣减。"""
        aggregate = self._require_aggregate(character_id)
        equipment_model = self._require_active_equipment(character_id=character_id, equipment_item_id=equipment_item_id)
        domain_item = self._to_domain_item(equipment_model)
        wash_result = self._execute_rule(
            lambda: self._affix_operation_rule.wash(
                item=domain_item,
                locked_affix_indices=locked_affix_indices,
                random_source=self._build_random_source(seed),
            )
        )
        resource_changes = self._consume_resources(
            character_id=character_id,
            currency_balance=aggregate.currency_balance,
            resource_costs=wash_result.costs,
        )
        updated_item = self._ensure_named_item(wash_result.item, force_refresh=True)
        self._sync_equipment_model(equipment_model=equipment_model, domain_item=updated_item)
        persisted_model = self._equipment_repository.save(equipment_model)
        self._refresh_score_if_equipped(character_id=character_id, equipment_model=persisted_model)
        return EquipmentWashApplicationResult(
            item=self._build_item_snapshot(persisted_model),
            locked_affix_indices=wash_result.locked_affix_indices,
            rerolled_affixes=self._build_affix_value_snapshots(wash_result.rerolled_affixes),
            resource_changes=resource_changes,
        )

    def reforge_equipment(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
        seed: int | None = None,
    ) -> EquipmentReforgeApplicationResult:
        """执行一次装备重铸，并显式处理资源扣减。"""
        aggregate = self._require_aggregate(character_id)
        equipment_model = self._require_active_equipment(character_id=character_id, equipment_item_id=equipment_item_id)
        domain_item = self._to_domain_item(equipment_model)
        reforge_result = self._execute_rule(
            lambda: self._affix_operation_rule.reforge(
                item=domain_item,
                random_source=self._build_random_source(seed),
            )
        )
        resource_changes = self._consume_resources(
            character_id=character_id,
            currency_balance=aggregate.currency_balance,
            resource_costs=reforge_result.costs,
        )
        updated_item = self._ensure_named_item(reforge_result.item, force_refresh=True)
        self._sync_equipment_model(equipment_model=equipment_model, domain_item=updated_item)
        persisted_model = self._equipment_repository.save(equipment_model)
        self._refresh_score_if_equipped(character_id=character_id, equipment_model=persisted_model)
        return EquipmentReforgeApplicationResult(
            item=self._build_item_snapshot(persisted_model),
            previous_template_id=reforge_result.previous_template_id,
            previous_affixes=self._build_affix_value_snapshots(reforge_result.previous_affixes),
            resource_changes=resource_changes,
        )

    def nurture_artifact(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
    ) -> ArtifactNurtureApplicationResult:
        """执行一次法宝培养，并显式处理资源扣减。"""
        aggregate = self._require_aggregate(character_id)
        equipment_model = self._require_active_equipment(character_id=character_id, equipment_item_id=equipment_item_id)
        domain_item = self._to_domain_item(equipment_model)
        nurture_result = self._execute_rule(lambda: self._artifact_nurture_rule.nurture(item=domain_item))
        resource_changes = self._consume_resources(
            character_id=character_id,
            currency_balance=aggregate.currency_balance,
            resource_costs=nurture_result.costs,
        )
        updated_item = self._ensure_named_item(nurture_result.item, force_refresh=False)
        self._sync_equipment_model(equipment_model=equipment_model, domain_item=updated_item)
        persisted_model = self._equipment_repository.save(equipment_model)
        self._refresh_score_if_equipped(character_id=character_id, equipment_model=persisted_model)
        return ArtifactNurtureApplicationResult(
            item=self._build_item_snapshot(persisted_model),
            previous_level=nurture_result.previous_level,
            target_level=nurture_result.target_level,
            resource_changes=resource_changes,
        )

    def dismantle_equipment(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
        occurred_at: datetime | None = None,
        reason: str | None = None,
        operator: str | None = None,
    ) -> EquipmentDismantleApplicationResult:
        """分解一件装备或法宝，并将回收结果入账。"""
        aggregate = self._require_aggregate(character_id)
        equipment_model = self._require_active_equipment(character_id=character_id, equipment_item_id=equipment_item_id)
        domain_item = self._to_domain_item(equipment_model)
        dismantle_result = self._execute_rule(lambda: self._dismantle_rule.dismantle(item=domain_item))
        resource_changes = self._grant_resources(
            character_id=character_id,
            currency_balance=aggregate.currency_balance,
            resource_costs=dismantle_result.returns,
        )
        settled_at = occurred_at or datetime.utcnow()
        equipment_model.item_state = _ITEM_STATE_DISMANTLED
        equipment_model.equipped_slot_id = None
        equipment_model.dismantled_at = settled_at
        self._sync_dismantle_record(
            equipment_model=equipment_model,
            character_id=character_id,
            returns=dismantle_result.returns,
            settled_at=settled_at,
            reason=reason,
            operator=operator,
        )
        persisted_model = self._equipment_repository.save(equipment_model)
        self._refresh_score_if_configured(character_id)
        return EquipmentDismantleApplicationResult(
            item=self._build_item_snapshot(persisted_model),
            resource_changes=resource_changes,
            settled_at=settled_at,
        )

    def get_equipment_detail(self, *, character_id: int, equipment_item_id: int) -> EquipmentItemSnapshot:
        """读取角色单件装备明细。"""
        equipment_model = self._require_owned_equipment(character_id=character_id, equipment_item_id=equipment_item_id)
        return self._build_item_snapshot(equipment_model)

    def list_equipment(self, *, character_id: int) -> EquipmentCollectionSnapshot:
        """读取角色当前装备集合视图。"""
        aggregate = self._require_aggregate(character_id)
        return EquipmentCollectionSnapshot(
            character_id=character_id,
            spirit_stone=aggregate.currency_balance.spirit_stone,
            active_items=tuple(
                self._build_item_snapshot(item)
                for item in self._equipment_repository.list_active_by_character_id(character_id)
            ),
            equipped_items=tuple(
                self._build_item_snapshot(item)
                for item in self._equipment_repository.list_equipped_by_character_id(character_id)
            ),
            dismantled_items=tuple(
                self._build_item_snapshot(item)
                for item in self._equipment_repository.list_dismantled_by_character_id(character_id)
            ),
        )

    def apply_custom_name(
        self,
        *,
        character_id: int,
        equipment_item_id: int,
        resolved_name: str,
        naming_template_id: str = "custom_name",
        naming_source: str = "custom_override",
        naming_metadata: Mapping[str, str] | None = None,
    ) -> EquipmentItemSnapshot:
        """为已有装备或法宝实例写入自定义名称与命名来源。"""
        equipment_model = self._require_active_equipment(character_id=character_id, equipment_item_id=equipment_item_id)
        normalized_name = resolved_name.strip()
        if not normalized_name:
            raise EquipmentRuleViolationError("装备命名不能为空")
        normalized_template_id = naming_template_id.strip() or "custom_name"
        normalized_source = naming_source.strip() or "custom_override"
        domain_item = self._to_domain_item(equipment_model).with_name(
            EquipmentNamingRecord(
                resolved_name=normalized_name,
                naming_template_id=normalized_template_id,
                naming_source=normalized_source,
                naming_metadata=dict(naming_metadata or {}),
            )
        )
        self._sync_equipment_model(equipment_model=equipment_model, domain_item=domain_item)
        persisted_model = self._equipment_repository.save(equipment_model)
        return self._build_item_snapshot(persisted_model)

    def _require_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise EquipmentCharacterNotFoundError(f"角色不存在：{character_id}")
        if aggregate.currency_balance is None:
            raise EquipmentCharacterStateError(f"角色缺少货币余额：{character_id}")
        return aggregate

    def _require_owned_equipment(self, *, character_id: int, equipment_item_id: int) -> EquipmentItemModel:
        equipment_model = self._equipment_repository.get(equipment_item_id)
        if equipment_model is None:
            raise EquipmentNotFoundError(f"装备不存在：{equipment_item_id}")
        if equipment_model.character_id != character_id:
            raise EquipmentOwnershipError(f"装备 {equipment_item_id} 不属于角色 {character_id}")
        return equipment_model

    def _require_active_equipment(self, *, character_id: int, equipment_item_id: int) -> EquipmentItemModel:
        equipment_model = self._require_owned_equipment(
            character_id=character_id,
            equipment_item_id=equipment_item_id,
        )
        if equipment_model.item_state != _ITEM_STATE_ACTIVE:
            raise EquipmentOperationStateError(
                f"装备 {equipment_item_id} 当前状态为 {equipment_model.item_state}，无法执行该操作"
            )
        return equipment_model

    @staticmethod
    def _build_random_source(seed: int | None) -> random.Random:
        return random.Random(seed)

    def _resolve_endless_reward_slot_id(self, *, seed: int | None) -> str:
        random_source = self._build_random_source(seed)
        return self._non_artifact_slot_ids[random_source.randrange(len(self._non_artifact_slot_ids))]

    def _resolve_endless_reward_quality_id(self, *, score: int) -> str:
        normalized_score = max(1, score)
        quality_index = min(len(self._ordered_qualities) - 1, (normalized_score - 1) // 16)
        return self._ordered_qualities[quality_index].quality_id

    def _resolve_endless_reward_rank_id(self, *, floor: int) -> str:
        normalized_floor = max(1, floor)
        rank_order = min(len(self._ordered_ranks), ((normalized_floor - 1) // 10) + 1)
        return self._ordered_ranks[rank_order - 1].rank_id

    def _execute_rule(self, executor: Callable[[], _T]) -> _T:
        try:
            return executor()
        except EquipmentRuleError as exc:
            raise EquipmentRuleViolationError(str(exc)) from exc

    def _refresh_score_if_equipped(self, *, character_id: int, equipment_model: EquipmentItemModel) -> None:
        if equipment_model.equipped_slot_id is None:
            return
        self._refresh_score_if_configured(character_id)

    def _refresh_score_if_configured(self, character_id: int) -> None:
        if self._score_service is None:
            return
        self._score_service.refresh_character_score(character_id=character_id)

    def _ensure_named_item(self, domain_item: DomainEquipmentItem, *, force_refresh: bool) -> DomainEquipmentItem:
        if domain_item.naming is not None and not force_refresh:
            return domain_item
        naming = self._execute_rule(lambda: self._naming_service.assign_name(item=domain_item))
        return domain_item.with_name(naming)

    def _new_equipment_model(self, *, character_id: int, domain_item: DomainEquipmentItem) -> EquipmentItemModel:
        equipment_model = EquipmentItemModel(
            character_id=character_id,
            slot_id=domain_item.slot_id,
            slot_name=domain_item.slot_name,
            equipped_slot_id=None,
            quality_id=domain_item.quality_id,
            quality_name=domain_item.quality_name,
            template_id=domain_item.template_id,
            template_name=domain_item.template_name,
            rank_id=domain_item.rank_id,
            rank_name=domain_item.rank_name,
            rank_order=domain_item.rank_order,
            mapped_realm_id=domain_item.mapped_realm_id,
            is_artifact=domain_item.is_artifact,
            resonance_name=domain_item.resonance_name,
            item_state=_ITEM_STATE_ACTIVE,
            item_name=domain_item.display_name,
            base_snapshot_json={"base_attributes": []},
        )
        self._sync_equipment_model(equipment_model=equipment_model, domain_item=domain_item)
        return equipment_model

    def _sync_equipment_model(
        self,
        *,
        equipment_model: EquipmentItemModel,
        domain_item: DomainEquipmentItem,
        success_count_delta: int = 0,
        failure_count_delta: int = 0,
    ) -> None:
        equipment_model.slot_id = domain_item.slot_id
        equipment_model.slot_name = domain_item.slot_name
        equipment_model.quality_id = domain_item.quality_id
        equipment_model.quality_name = domain_item.quality_name
        equipment_model.template_id = domain_item.template_id
        equipment_model.template_name = domain_item.template_name
        equipment_model.rank_id = domain_item.rank_id
        equipment_model.rank_name = domain_item.rank_name
        equipment_model.rank_order = domain_item.rank_order
        equipment_model.mapped_realm_id = domain_item.mapped_realm_id
        equipment_model.is_artifact = domain_item.is_artifact
        equipment_model.resonance_name = domain_item.resonance_name
        equipment_model.item_name = domain_item.display_name
        equipment_model.base_snapshot_json = {
            "base_attributes": [
                {"stat_id": attribute.stat_id, "value": attribute.value}
                for attribute in domain_item.base_attributes
            ]
        }

        if equipment_model.enhancement is None:
            equipment_model.enhancement = EquipmentEnhancement(
                enhancement_level=0,
                success_count=0,
                failure_count=0,
                base_stat_bonus_ratio=Decimal("0"),
                affix_bonus_ratio=Decimal("0"),
            )
        equipment_model.enhancement.enhancement_level = domain_item.enhancement_level
        equipment_model.enhancement.base_stat_bonus_ratio = domain_item.enhancement_base_stat_bonus_ratio
        equipment_model.enhancement.affix_bonus_ratio = domain_item.enhancement_affix_bonus_ratio
        equipment_model.enhancement.success_count = max(
            0,
            equipment_model.enhancement.success_count + success_count_delta,
        )
        equipment_model.enhancement.failure_count = max(
            0,
            equipment_model.enhancement.failure_count + failure_count_delta,
        )

        existing_affixes = list(equipment_model.affixes)
        target_affixes = list(domain_item.affixes)
        for index, affix in enumerate(target_affixes, start=1):
            if index <= len(existing_affixes):
                affix_model = existing_affixes[index - 1]
            else:
                affix_model = EquipmentAffix(position=index)
                equipment_model.affixes.append(affix_model)
            affix_model.position = index
            affix_model.affix_id = affix.affix_id
            affix_model.affix_name = affix.affix_name
            affix_model.stat_id = affix.stat_id
            affix_model.category = affix.category
            affix_model.tier_id = affix.tier_id
            affix_model.tier_name = affix.tier_name
            affix_model.roll_value = affix.rolled_multiplier
            affix_model.value = affix.value
            affix_model.affix_kind = affix.affix_kind
            affix_model.is_pve_specialized = affix.is_pve_specialized
            affix_model.is_pvp_specialized = affix.is_pvp_specialized
            if affix.special_effect is None:
                affix_model.special_effect_id = None
                affix_model.special_effect_name = None
                affix_model.special_effect_type = None
                affix_model.trigger_event = None
                affix_model.special_effect_payload_json = {}
                affix_model.public_score_key = None
                affix_model.hidden_pvp_score_key = None
            else:
                affix_model.special_effect_id = affix.special_effect.effect_id
                affix_model.special_effect_name = affix.special_effect.effect_name
                affix_model.special_effect_type = affix.special_effect.effect_type
                affix_model.trigger_event = affix.special_effect.trigger_event
                affix_model.special_effect_payload_json = dict(affix.special_effect.payload)
                affix_model.public_score_key = affix.special_effect.public_score_key
                affix_model.hidden_pvp_score_key = affix.special_effect.hidden_pvp_score_key
        while len(equipment_model.affixes) > len(target_affixes):
            equipment_model.affixes.pop()

        if domain_item.is_artifact:
            if equipment_model.artifact_profile is None:
                equipment_model.artifact_profile = ArtifactProfile(
                    artifact_template_id=domain_item.template_id,
                    refinement_level=0,
                    core_effect_snapshot_json={},
                )
            else:
                equipment_model.artifact_profile.artifact_template_id = domain_item.template_id
            if equipment_model.artifact_nurture_state is None:
                equipment_model.artifact_nurture_state = ArtifactNurtureState(
                    nurture_level=0,
                    base_stat_bonus_ratio=Decimal("0"),
                    affix_bonus_ratio=Decimal("0"),
                )
            equipment_model.artifact_nurture_state.nurture_level = domain_item.artifact_nurture_level
            equipment_model.artifact_nurture_state.base_stat_bonus_ratio = domain_item.nurture_base_stat_bonus_ratio
            equipment_model.artifact_nurture_state.affix_bonus_ratio = domain_item.nurture_affix_bonus_ratio
        else:
            equipment_model.artifact_profile = None
            equipment_model.artifact_nurture_state = None

        if domain_item.naming is None:
            equipment_model.naming_state = None
        else:
            if equipment_model.naming_state is None:
                equipment_model.naming_state = EquipmentNamingState(
                    resolved_name=domain_item.naming.resolved_name,
                    naming_template_id=domain_item.naming.naming_template_id,
                    naming_source=domain_item.naming.naming_source,
                    naming_metadata_json=dict(domain_item.naming.naming_metadata),
                )
            else:
                equipment_model.naming_state.resolved_name = domain_item.naming.resolved_name
                equipment_model.naming_state.naming_template_id = domain_item.naming.naming_template_id
                equipment_model.naming_state.naming_source = domain_item.naming.naming_source
                equipment_model.naming_state.naming_metadata_json = dict(domain_item.naming.naming_metadata)

    def _sync_dismantle_record(
        self,
        *,
        equipment_model: EquipmentItemModel,
        character_id: int,
        returns: tuple[EquipmentResourceCost, ...],
        settled_at: datetime,
        reason: str | None,
        operator: str | None,
    ) -> None:
        audit_metadata: dict[str, object] = {"source": _DISMANTLE_AUDIT_SOURCE}
        if reason is not None:
            audit_metadata["reason"] = reason
        if operator is not None:
            audit_metadata["operator"] = operator
        returns_json = [
            {"resource_id": resource.resource_id, "quantity": resource.quantity}
            for resource in returns
        ]
        if equipment_model.dismantle_record is None:
            equipment_model.dismantle_record = EquipmentDismantleRecord(
                character_id=character_id,
                status="completed",
                returns_json=returns_json,
                audit_metadata_json=audit_metadata,
                settled_at=settled_at,
            )
            return
        equipment_model.dismantle_record.character_id = character_id
        equipment_model.dismantle_record.status = "completed"
        equipment_model.dismantle_record.returns_json = returns_json
        equipment_model.dismantle_record.audit_metadata_json = audit_metadata
        equipment_model.dismantle_record.settled_at = settled_at

    def _consume_resources(
        self,
        *,
        character_id: int,
        currency_balance: CurrencyBalance,
        resource_costs: tuple[EquipmentResourceCost, ...],
    ) -> tuple[EquipmentResourceLedgerEntry, ...]:
        aggregated_costs = self._aggregate_resources(
            resource_costs,
            spirit_stone_multiplier=self._resolve_spirit_stone_multiplier(character_id=character_id),
        )
        if not aggregated_costs:
            return ()
        self._validate_resource_availability(
            character_id=character_id,
            currency_balance=currency_balance,
            aggregated_resources=aggregated_costs,
        )
        resource_changes: list[EquipmentResourceLedgerEntry] = []
        for resource_id, quantity in sorted(aggregated_costs.items(), key=lambda item: item[0]):
            if resource_id == _SPIRIT_STONE_RESOURCE_ID:
                before_quantity = currency_balance.spirit_stone
                currency_balance.spirit_stone = before_quantity - quantity
                self._character_repository.save_currency_balance(currency_balance)
                resource_changes.append(
                    EquipmentResourceLedgerEntry(
                        resource_id=resource_id,
                        resource_kind=_RESOURCE_KIND_CURRENCY,
                        change_type=_RESOURCE_CHANGE_CONSUME,
                        quantity=quantity,
                        before_quantity=before_quantity,
                        after_quantity=currency_balance.spirit_stone,
                    )
                )
                continue
            inventory_item = self._require_material_item(character_id=character_id, resource_id=resource_id)
            before_quantity = inventory_item.quantity
            inventory_item.quantity = before_quantity - quantity
            self._inventory_repository.upsert_item(inventory_item)
            resource_changes.append(
                EquipmentResourceLedgerEntry(
                    resource_id=resource_id,
                    resource_kind=_RESOURCE_KIND_MATERIAL,
                    change_type=_RESOURCE_CHANGE_CONSUME,
                    quantity=quantity,
                    before_quantity=before_quantity,
                    after_quantity=inventory_item.quantity,
                )
            )
        return tuple(resource_changes)

    def _grant_resources(
        self,
        *,
        character_id: int,
        currency_balance: CurrencyBalance,
        resource_costs: tuple[EquipmentResourceCost, ...],
    ) -> tuple[EquipmentResourceLedgerEntry, ...]:
        aggregated_returns = self._aggregate_resources(resource_costs)
        if not aggregated_returns:
            return ()
        resource_changes: list[EquipmentResourceLedgerEntry] = []
        for resource_id, quantity in sorted(aggregated_returns.items(), key=lambda item: item[0]):
            if resource_id == _SPIRIT_STONE_RESOURCE_ID:
                before_quantity = currency_balance.spirit_stone
                currency_balance.spirit_stone = before_quantity + quantity
                self._character_repository.save_currency_balance(currency_balance)
                resource_changes.append(
                    EquipmentResourceLedgerEntry(
                        resource_id=resource_id,
                        resource_kind=_RESOURCE_KIND_CURRENCY,
                        change_type=_RESOURCE_CHANGE_GRANT,
                        quantity=quantity,
                        before_quantity=before_quantity,
                        after_quantity=currency_balance.spirit_stone,
                    )
                )
                continue
            inventory_item = self._inventory_repository.get_item(
                character_id,
                _RESOURCE_KIND_MATERIAL,
                resource_id,
            )
            if inventory_item is None:
                inventory_item = InventoryItem(
                    character_id=character_id,
                    item_type=_RESOURCE_KIND_MATERIAL,
                    item_id=resource_id,
                    quantity=0,
                    item_payload_json={},
                )
            before_quantity = inventory_item.quantity
            inventory_item.quantity = before_quantity + quantity
            self._inventory_repository.upsert_item(inventory_item)
            resource_changes.append(
                EquipmentResourceLedgerEntry(
                    resource_id=resource_id,
                    resource_kind=_RESOURCE_KIND_MATERIAL,
                    change_type=_RESOURCE_CHANGE_GRANT,
                    quantity=quantity,
                    before_quantity=before_quantity,
                    after_quantity=inventory_item.quantity,
                )
            )
        return tuple(resource_changes)

    def _validate_resource_availability(
        self,
        *,
        character_id: int,
        currency_balance: CurrencyBalance,
        aggregated_resources: dict[str, int],
    ) -> None:
        for resource_id, quantity in sorted(aggregated_resources.items(), key=lambda item: item[0]):
            if quantity <= 0:
                continue
            if resource_id == _SPIRIT_STONE_RESOURCE_ID:
                available_quantity = max(0, currency_balance.spirit_stone)
            else:
                inventory_item = self._inventory_repository.get_item(
                    character_id,
                    _RESOURCE_KIND_MATERIAL,
                    resource_id,
                )
                available_quantity = 0 if inventory_item is None else max(0, inventory_item.quantity)
            if available_quantity < quantity:
                raise EquipmentResourceInsufficientError(
                    f"资源不足：{resource_id}，需要 {quantity}，当前只有 {available_quantity}"
                )

    def _require_material_item(self, *, character_id: int, resource_id: str) -> InventoryItem:
        inventory_item = self._inventory_repository.get_item(
            character_id,
            _RESOURCE_KIND_MATERIAL,
            resource_id,
        )
        if inventory_item is None:
            raise EquipmentResourceInsufficientError(f"资源不足：{resource_id}，当前没有任何库存")
        return inventory_item

    def _resolve_spirit_stone_multiplier(self, *, character_id: int) -> int:
        aggregate = self._require_aggregate(character_id)
        if aggregate.progress is None:
            raise EquipmentCharacterStateError(f"角色缺少成长状态：{character_id}")
        return resolve_spirit_stone_economy_multiplier(
            static_config=self._static_config,
            realm_id=aggregate.progress.realm_id,
        )

    @staticmethod
    def _aggregate_resources(
        resource_costs: tuple[EquipmentResourceCost, ...],
        *,
        spirit_stone_multiplier: int = 1,
    ) -> dict[str, int]:
        aggregated_resources: dict[str, int] = defaultdict(int)
        for resource in resource_costs:
            quantity = resource.quantity
            if resource.resource_id == _SPIRIT_STONE_RESOURCE_ID:
                quantity *= spirit_stone_multiplier
            aggregated_resources[resource.resource_id] += quantity
        return dict(aggregated_resources)

    def _build_item_snapshot(self, equipment_model: EquipmentItemModel) -> EquipmentItemSnapshot:
        domain_item = self._to_domain_item(equipment_model)
        enhancement = equipment_model.enhancement
        naming_snapshot = self._build_naming_snapshot(domain_item.naming)
        return EquipmentItemSnapshot(
            item_id=equipment_model.id,
            character_id=equipment_model.character_id,
            slot_id=domain_item.slot_id,
            slot_name=domain_item.slot_name,
            equipped_slot_id=equipment_model.equipped_slot_id,
            quality_id=domain_item.quality_id,
            quality_name=domain_item.quality_name,
            template_id=domain_item.template_id,
            template_name=domain_item.template_name,
            rank_id=domain_item.rank_id,
            rank_name=domain_item.rank_name,
            rank_order=domain_item.rank_order,
            mapped_realm_id=domain_item.mapped_realm_id,
            is_artifact=domain_item.is_artifact,
            resonance_name=domain_item.resonance_name,
            item_state=equipment_model.item_state,
            display_name=domain_item.display_name,
            enhancement_level=domain_item.enhancement_level,
            artifact_nurture_level=domain_item.artifact_nurture_level,
            enhancement_success_count=0 if enhancement is None else enhancement.success_count,
            enhancement_failure_count=0 if enhancement is None else enhancement.failure_count,
            base_attribute_multiplier=domain_item.base_attribute_multiplier,
            affix_base_value_multiplier=domain_item.affix_base_value_multiplier,
            dismantle_reward_multiplier=domain_item.dismantle_reward_multiplier,
            enhancement_base_stat_bonus_ratio=domain_item.enhancement_base_stat_bonus_ratio,
            enhancement_affix_bonus_ratio=domain_item.enhancement_affix_bonus_ratio,
            nurture_base_stat_bonus_ratio=domain_item.nurture_base_stat_bonus_ratio,
            nurture_affix_bonus_ratio=domain_item.nurture_affix_bonus_ratio,
            base_stat_bonus_ratio=domain_item.base_stat_bonus_ratio,
            affix_bonus_ratio=domain_item.affix_bonus_ratio,
            base_attributes=tuple(self._build_attribute_snapshot(attribute) for attribute in domain_item.base_attributes),
            affixes=tuple(
                self._build_affix_snapshot(affix, position=index)
                for index, affix in enumerate(domain_item.affixes, start=1)
            ),
            resolved_stats=tuple(
                EquipmentResolvedStatSnapshot(stat_id=stat.stat_id, value=stat.value)
                for stat in domain_item.resolved_stat_lines()
            ),
            naming=naming_snapshot,
            dismantled_at=equipment_model.dismantled_at,
        )

    @staticmethod
    def _build_attribute_snapshot(attribute: EquipmentAttributeValue) -> EquipmentAttributeSnapshot:
        return EquipmentAttributeSnapshot(stat_id=attribute.stat_id, value=attribute.value)

    @staticmethod
    def _build_special_effect_snapshot(
        special_effect: EquipmentSpecialEffectValue | None,
    ) -> EquipmentSpecialEffectSnapshot | None:
        if special_effect is None:
            return None
        return EquipmentSpecialEffectSnapshot(
            effect_id=special_effect.effect_id,
            effect_name=special_effect.effect_name,
            effect_type=special_effect.effect_type,
            trigger_event=special_effect.trigger_event,
            payload=dict(special_effect.payload),
            public_score_key=special_effect.public_score_key,
            hidden_pvp_score_key=special_effect.hidden_pvp_score_key,
        )

    @staticmethod
    def _build_affix_snapshot(affix: EquipmentAffixValue, *, position: int | None = None) -> EquipmentAffixSnapshot:
        return EquipmentAffixSnapshot(
            affix_id=affix.affix_id,
            affix_name=affix.affix_name,
            stat_id=affix.stat_id,
            category=affix.category,
            tier_id=affix.tier_id,
            tier_name=affix.tier_name,
            rolled_multiplier=affix.rolled_multiplier,
            value=affix.value,
            is_pve_specialized=affix.is_pve_specialized,
            is_pvp_specialized=affix.is_pvp_specialized,
            affix_kind=affix.affix_kind,
            special_effect=EquipmentService._build_special_effect_snapshot(affix.special_effect),
            position=position,
        )

    def _build_affix_value_snapshots(self, affixes: tuple[EquipmentAffixValue, ...]) -> tuple[EquipmentAffixSnapshot, ...]:
        return tuple(self._build_affix_snapshot(affix) for affix in affixes)

    @staticmethod
    def _build_naming_snapshot(naming: EquipmentNamingRecord | None) -> EquipmentNamingSnapshot | None:
        if naming is None:
            return None
        return EquipmentNamingSnapshot(
            resolved_name=naming.resolved_name,
            naming_template_id=naming.naming_template_id,
            naming_source=naming.naming_source,
            naming_metadata=dict(naming.naming_metadata),
        )

    def _resolve_rank_payload(self, equipment_model: EquipmentItemModel) -> tuple[str, str, int, str, Decimal, Decimal, Decimal]:
        rank_id = equipment_model.rank_id or _DEFAULT_EQUIPMENT_RANK_ID
        rank = self._static_config.equipment.get_equipment_rank(rank_id)
        if rank is None:
            rank = self._default_rank
        rank_name = equipment_model.rank_name or rank.name
        rank_order = equipment_model.rank_order or rank.order
        mapped_realm_id = equipment_model.mapped_realm_id or rank.mapped_realm_id
        return (
            rank_id,
            rank_name,
            rank_order,
            mapped_realm_id,
            rank.base_attribute_multiplier,
            rank.affix_base_value_multiplier,
            rank.dismantle_reward_multiplier,
        )

    def _to_domain_item(self, equipment_model: EquipmentItemModel) -> DomainEquipmentItem:
        enhancement = equipment_model.enhancement
        nurture_state = equipment_model.artifact_nurture_state
        naming_state = equipment_model.naming_state
        (
            rank_id,
            rank_name,
            rank_order,
            mapped_realm_id,
            base_attribute_multiplier,
            affix_base_value_multiplier,
            dismantle_reward_multiplier,
        ) = self._resolve_rank_payload(equipment_model)
        base_attributes = tuple(
            EquipmentAttributeValue(
                stat_id=str(attribute_payload["stat_id"]),
                value=int(attribute_payload["value"]),
            )
            for attribute_payload in equipment_model.base_snapshot_json.get("base_attributes", [])
        )
        affixes = tuple(
            EquipmentAffixValue(
                affix_id=affix.affix_id,
                affix_name=affix.affix_name,
                stat_id=affix.stat_id,
                category=affix.category,
                tier_id=affix.tier_id,
                tier_name=affix.tier_name,
                rolled_multiplier=affix.roll_value,
                value=affix.value,
                is_pve_specialized=affix.is_pve_specialized,
                is_pvp_specialized=affix.is_pvp_specialized,
                affix_kind=affix.affix_kind,
                special_effect=None
                if affix.special_effect_id is None
                else EquipmentSpecialEffectValue(
                    effect_id=affix.special_effect_id,
                    effect_name=affix.special_effect_name or affix.special_effect_id,
                    effect_type=affix.special_effect_type or "unknown",
                    trigger_event=affix.trigger_event or "unknown",
                    payload=dict(affix.special_effect_payload_json),
                    public_score_key=affix.public_score_key,
                    hidden_pvp_score_key=affix.hidden_pvp_score_key,
                ),
            )
            for affix in equipment_model.affixes
        )
        naming = None
        if naming_state is not None:
            naming = EquipmentNamingRecord(
                resolved_name=naming_state.resolved_name,
                naming_template_id=naming_state.naming_template_id,
                naming_source=naming_state.naming_source,
                naming_metadata=dict(naming_state.naming_metadata_json),
            )
        return DomainEquipmentItem(
            slot_id=equipment_model.slot_id,
            slot_name=equipment_model.slot_name,
            quality_id=equipment_model.quality_id,
            quality_name=equipment_model.quality_name,
            template_id=equipment_model.template_id,
            template_name=equipment_model.template_name,
            rank_id=rank_id,
            rank_name=rank_name,
            rank_order=rank_order,
            mapped_realm_id=mapped_realm_id,
            is_artifact=equipment_model.is_artifact,
            resonance_name=equipment_model.resonance_name,
            enhancement_level=0 if enhancement is None else enhancement.enhancement_level,
            artifact_nurture_level=0 if nurture_state is None else nurture_state.nurture_level,
            base_attributes=base_attributes,
            affixes=affixes,
            base_attribute_multiplier=base_attribute_multiplier,
            affix_base_value_multiplier=affix_base_value_multiplier,
            dismantle_reward_multiplier=dismantle_reward_multiplier,
            enhancement_base_stat_bonus_ratio=Decimal("0") if enhancement is None else enhancement.base_stat_bonus_ratio,
            enhancement_affix_bonus_ratio=Decimal("0") if enhancement is None else enhancement.affix_bonus_ratio,
            nurture_base_stat_bonus_ratio=Decimal("0") if nurture_state is None else nurture_state.base_stat_bonus_ratio,
            nurture_affix_bonus_ratio=Decimal("0") if nurture_state is None else nurture_state.affix_bonus_ratio,
            naming=naming,
        )


__all__ = [
    "ArtifactNurtureApplicationResult",
    "EquipmentAffixSnapshot",
    "EquipmentSpecialEffectSnapshot",
    "EquipmentAttributeSnapshot",
    "EquipmentCharacterNotFoundError",
    "EquipmentCharacterStateError",
    "EquipmentCollectionSnapshot",
    "EquipmentDismantleApplicationResult",
    "EquipmentEnhancementApplicationResult",
    "EquipmentEquipApplicationResult",
    "EquipmentGenerationApplicationResult",
    "EquipmentItemSnapshot",
    "EquipmentNamingSnapshot",
    "EquipmentNotFoundError",
    "EquipmentOperationStateError",
    "EquipmentOwnershipError",
    "EquipmentReforgeApplicationResult",
    "EquipmentResolvedStatSnapshot",
    "EquipmentResourceInsufficientError",
    "EquipmentResourceLedgerEntry",
    "EquipmentRuleViolationError",
    "EquipmentService",
    "EquipmentServiceError",
    "EquipmentWashApplicationResult",
]
