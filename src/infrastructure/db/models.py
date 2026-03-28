"""阶段 2 数据库模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infrastructure.db.base import Base

_DECIMAL_ZERO = Decimal("0.0000")
_DECIMAL_ONE = Decimal("1.0000")


class TimestampMixin:
    """提供通用创建时间与更新时间字段。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class SystemMarker(Base):
    """链路验证用系统标记表。"""

    __tablename__ = "system_markers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )


class Player(TimestampMixin, Base):
    """Discord 玩家主体。"""

    __tablename__ = "players"
    __table_args__ = (UniqueConstraint("discord_user_id", name="uq_players_discord_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)

    character: Mapped[Character | None] = relationship(back_populates="player", uselist=False)


class Character(TimestampMixin, Base):
    """角色基础档案。"""

    __tablename__ = "characters"
    __table_args__ = (
        UniqueConstraint("player_id", name="uq_characters_player_id"),
        Index("ix_characters_total_power_score", "total_power_score"),
        Index("ix_characters_hidden_pvp_score", "hidden_pvp_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_power_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    public_power_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hidden_pvp_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    player: Mapped[Player] = relationship(back_populates="character")
    score_snapshot: Mapped[CharacterScoreSnapshot | None] = relationship(
        back_populates="character",
        uselist=False,
        cascade="all, delete-orphan",
    )
    progress: Mapped[CharacterProgress | None] = relationship(
        back_populates="character",
        uselist=False,
        cascade="all, delete-orphan",
    )
    skill_loadout: Mapped[CharacterSkillLoadout | None] = relationship(
        back_populates="character",
        uselist=False,
        cascade="all, delete-orphan",
    )
    skill_items: Mapped[list[CharacterSkillItem]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="CharacterSkillItem.id",
    )
    currency_balance: Mapped[CurrencyBalance | None] = relationship(
        back_populates="character",
        uselist=False,
        cascade="all, delete-orphan",
    )
    equipment_items: Mapped[list[EquipmentItem]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="EquipmentItem.id",
    )
    inventory_items: Mapped[list[InventoryItem]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="InventoryItem.id",
    )
    retreat_state: Mapped[RetreatState | None] = relationship(
        back_populates="character",
        uselist=False,
        cascade="all, delete-orphan",
    )
    healing_state: Mapped[HealingState | None] = relationship(
        back_populates="character",
        uselist=False,
        cascade="all, delete-orphan",
    )
    endless_run_state: Mapped[EndlessRunState | None] = relationship(
        back_populates="character",
        uselist=False,
        cascade="all, delete-orphan",
    )
    breakthrough_progress_entries: Mapped[list[BreakthroughTrialProgress]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="BreakthroughTrialProgress.id",
    )
    breakthrough_reward_ledgers: Mapped[list[BreakthroughRewardLedger]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="BreakthroughRewardLedger.id",
    )
    pvp_defense_snapshots: Mapped[list[PvpDefenseSnapshot]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="PvpDefenseSnapshot.snapshot_version",
    )
    battle_reports: Mapped[list[BattleReport]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="BattleReport.occurred_at",
    )
    drop_records: Mapped[list[DropRecord]] = relationship(
        back_populates="character",
        cascade="all, delete-orphan",
        order_by="DropRecord.occurred_at",
    )


class CharacterScoreSnapshot(Base):
    """角色评分明细快照。"""

    __tablename__ = "character_score_snapshots"
    __table_args__ = (
        UniqueConstraint("character_id", name="uq_character_score_snapshots_character_id"),
        Index("ix_character_score_snapshots_score_version", "score_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    score_version: Mapped[str] = mapped_column(String(32), nullable=False)
    total_power_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    public_power_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hidden_pvp_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    growth_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    equipment_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skill_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    artifact_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pvp_adjustment_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakdown_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="score_snapshot")


class CharacterProgress(Base):
    """角色成长进度状态。"""

    __tablename__ = "character_progress"
    __table_args__ = (
        UniqueConstraint("character_id", name="uq_character_progress_character_id"),
        Index("ix_character_progress_highest_endless_floor", "highest_endless_floor"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    realm_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cultivation_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comprehension_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakthrough_qualification_obtained: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    highest_endless_floor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_hp_ratio: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=_DECIMAL_ONE)
    current_mp_ratio: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False, default=_DECIMAL_ONE)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="progress")


class CharacterSkillItem(TimestampMixin, Base):
    """角色持有的功法实例。"""

    __tablename__ = "character_skill_items"
    __table_args__ = (
        Index("ix_character_skill_items_character_id", "character_id"),
        Index("ix_character_skill_items_character_id_skill_type", "character_id", "skill_type"),
        Index("ix_character_skill_items_character_id_auxiliary_slot_id", "character_id", "auxiliary_slot_id"),
        Index("ix_character_skill_items_lineage_id", "lineage_id"),
        Index("ix_character_skill_items_item_state", "item_state"),
        Index("ix_character_skill_items_naming_source", "naming_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    lineage_id: Mapped[str] = mapped_column(String(64), nullable=False)
    path_id: Mapped[str] = mapped_column(String(64), nullable=False)
    axis_id: Mapped[str] = mapped_column(String(64), nullable=False)
    skill_type: Mapped[str] = mapped_column(String(32), nullable=False)
    auxiliary_slot_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    skill_name: Mapped[str] = mapped_column(String(128), nullable=False)
    naming_source: Mapped[str] = mapped_column(String(64), nullable=False, default="lineage_static")
    naming_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rank_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rank_name: Mapped[str] = mapped_column(String(64), nullable=False)
    rank_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    quality_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_name: Mapped[str] = mapped_column(String(64), nullable=False)
    total_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    budget_distribution_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    resolved_attributes_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    resolved_patches_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_record_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    item_state: Mapped[str] = mapped_column(String(32), nullable=False, default="inventory")
    equipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    unequipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    character: Mapped[Character] = relationship(back_populates="skill_items")


class CharacterSkillLoadout(Base):
    """角色当前功法装配快照。"""

    __tablename__ = "character_skill_loadouts"
    __table_args__ = (
        UniqueConstraint("character_id", name="uq_character_skill_loadouts_character_id"),
        Index("ix_character_skill_loadouts_main_skill_id", "main_skill_id"),
        Index("ix_character_skill_loadouts_main_path_id", "main_path_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    main_skill_id: Mapped[int | None] = mapped_column(ForeignKey("character_skill_items.id"), nullable=True)
    guard_skill_id: Mapped[int | None] = mapped_column(ForeignKey("character_skill_items.id"), nullable=True)
    movement_skill_id: Mapped[int | None] = mapped_column(ForeignKey("character_skill_items.id"), nullable=True)
    spirit_skill_id: Mapped[int | None] = mapped_column(ForeignKey("character_skill_items.id"), nullable=True)
    main_axis_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    main_path_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    behavior_template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    loadout_notes_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="skill_loadout")
    main_skill: Mapped[CharacterSkillItem | None] = relationship(foreign_keys=[main_skill_id])
    guard_skill: Mapped[CharacterSkillItem | None] = relationship(foreign_keys=[guard_skill_id])
    movement_skill: Mapped[CharacterSkillItem | None] = relationship(foreign_keys=[movement_skill_id])
    spirit_skill: Mapped[CharacterSkillItem | None] = relationship(foreign_keys=[spirit_skill_id])

    @property
    def body_method_id(self) -> int | None:
        """兼容旧语义字段，返回护体槽位的功法实例标识。"""
        return self.guard_skill_id

    @body_method_id.setter
    def body_method_id(self, value: int | None) -> None:
        """兼容旧语义字段，写入护体槽位的功法实例标识。"""
        self.guard_skill_id = value


class ItemNamingBatch(TimestampMixin, Base):
    """按单次结算聚合的实例命名批次。"""

    __tablename__ = "item_naming_batches"
    __table_args__ = (
        UniqueConstraint("character_id", "source_type", "source_ref", name="uq_item_naming_batches_character_source"),
        Index("ix_item_naming_batches_character_id_status", "character_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_payload_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    result_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(String(255), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class CurrencyBalance(Base):
    """角色货币余额。"""

    __tablename__ = "currency_balances"
    __table_args__ = (UniqueConstraint("character_id", name="uq_currency_balances_character_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    spirit_stone: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    honor_coin: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="currency_balance")


class EquipmentItem(TimestampMixin, Base):
    """角色装备实例。"""

    __tablename__ = "equipment_items"
    __table_args__ = (
        UniqueConstraint("character_id", "equipped_slot_id", name="uq_equipment_items_character_equipped_slot_id"),
        Index("ix_equipment_items_character_id", "character_id"),
        Index("ix_equipment_items_character_id_item_state", "character_id", "item_state"),
        Index("ix_equipment_items_quality_id", "quality_id"),
        Index("ix_equipment_items_template_id", "template_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    slot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slot_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    equipped_slot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quality_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, default="legacy_template")
    template_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    rank_id: Mapped[str] = mapped_column(String(64), nullable=False, default="mortal")
    rank_name: Mapped[str] = mapped_column(String(64), nullable=False, default="一阶")
    rank_order: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    mapped_realm_id: Mapped[str] = mapped_column(String(64), nullable=False, default="mortal")
    is_artifact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resonance_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    item_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dismantled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    character: Mapped[Character] = relationship(back_populates="equipment_items")
    enhancement: Mapped[EquipmentEnhancement | None] = relationship(
        back_populates="equipment_item",
        uselist=False,
        cascade="all, delete-orphan",
    )
    affixes: Mapped[list[EquipmentAffix]] = relationship(
        back_populates="equipment_item",
        cascade="all, delete-orphan",
        order_by="EquipmentAffix.position",
    )
    artifact_profile: Mapped[ArtifactProfile | None] = relationship(
        back_populates="equipment_item",
        uselist=False,
        cascade="all, delete-orphan",
    )
    artifact_nurture_state: Mapped[ArtifactNurtureState | None] = relationship(
        back_populates="equipment_item",
        uselist=False,
        cascade="all, delete-orphan",
    )
    naming_state: Mapped[EquipmentNamingState | None] = relationship(
        back_populates="equipment_item",
        uselist=False,
        cascade="all, delete-orphan",
    )
    dismantle_record: Mapped[EquipmentDismantleRecord | None] = relationship(
        back_populates="equipment_item",
        uselist=False,
        cascade="all, delete-orphan",
    )


class EquipmentEnhancement(Base):
    """装备当前强化状态。"""

    __tablename__ = "equipment_enhancements"
    __table_args__ = (UniqueConstraint("equipment_item_id", name="uq_equipment_enhancements_equipment_item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_item_id: Mapped[int] = mapped_column(ForeignKey("equipment_items.id"), nullable=False)
    enhancement_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_stat_bonus_ratio: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=_DECIMAL_ZERO)
    affix_bonus_ratio: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=_DECIMAL_ZERO)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    equipment_item: Mapped[EquipmentItem] = relationship(back_populates="enhancement")


class EquipmentAffix(Base):
    """装备词条实例。"""

    __tablename__ = "equipment_affixes"
    __table_args__ = (
        UniqueConstraint("equipment_item_id", "position", name="uq_equipment_affixes_equipment_item_position"),
        Index("ix_equipment_affixes_equipment_item_id", "equipment_item_id"),
        Index("ix_equipment_affixes_affix_id", "affix_id"),
        Index("ix_equipment_affixes_tier_id", "tier_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_item_id: Mapped[int] = mapped_column(ForeignKey("equipment_items.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    affix_id: Mapped[str] = mapped_column(String(64), nullable=False)
    affix_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    stat_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    tier_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tier_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    roll_value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    affix_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="numeric")
    special_effect_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    special_effect_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    special_effect_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trigger_event: Mapped[str | None] = mapped_column(String(64), nullable=True)
    special_effect_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    public_score_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hidden_pvp_score_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_pve_specialized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_pvp_specialized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    equipment_item: Mapped[EquipmentItem] = relationship(back_populates="affixes")


class ArtifactProfile(Base):
    """法宝专属附加信息。"""

    __tablename__ = "artifact_profiles"
    __table_args__ = (UniqueConstraint("equipment_item_id", name="uq_artifact_profiles_equipment_item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_item_id: Mapped[int] = mapped_column(ForeignKey("equipment_items.id"), nullable=False)
    artifact_template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    refinement_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    core_effect_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    equipment_item: Mapped[EquipmentItem] = relationship(back_populates="artifact_profile")


class ArtifactNurtureState(Base):
    """法宝培养持久化状态。"""

    __tablename__ = "artifact_nurture_states"
    __table_args__ = (UniqueConstraint("equipment_item_id", name="uq_artifact_nurture_states_equipment_item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_item_id: Mapped[int] = mapped_column(ForeignKey("equipment_items.id"), nullable=False)
    nurture_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_stat_bonus_ratio: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=_DECIMAL_ZERO)
    affix_bonus_ratio: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=_DECIMAL_ZERO)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    equipment_item: Mapped[EquipmentItem] = relationship(back_populates="artifact_nurture_state")


class EquipmentNamingState(Base):
    """装备命名结果持久化快照。"""

    __tablename__ = "equipment_naming_states"
    __table_args__ = (
        UniqueConstraint("equipment_item_id", name="uq_equipment_naming_states_equipment_item_id"),
        Index("ix_equipment_naming_states_naming_source", "naming_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_item_id: Mapped[int] = mapped_column(ForeignKey("equipment_items.id"), nullable=False)
    resolved_name: Mapped[str] = mapped_column(String(128), nullable=False)
    naming_template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    naming_source: Mapped[str] = mapped_column(String(64), nullable=False)
    naming_metadata_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    equipment_item: Mapped[EquipmentItem] = relationship(back_populates="naming_state")


class EquipmentDismantleRecord(TimestampMixin, Base):
    """装备分解审计记录。"""

    __tablename__ = "equipment_dismantle_records"
    __table_args__ = (
        UniqueConstraint("equipment_item_id", name="uq_equipment_dismantle_records_equipment_item_id"),
        Index("ix_equipment_dismantle_records_character_id_status", "character_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    equipment_item_id: Mapped[int] = mapped_column(ForeignKey("equipment_items.id"), nullable=False)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    returns_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    audit_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    equipment_item: Mapped[EquipmentItem] = relationship(back_populates="dismantle_record")
    character: Mapped[Character] = relationship()


class InventoryItem(Base):
    """角色库存条目。"""

    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("character_id", "item_type", "item_id", name="uq_inventory_items_character_type_item_id"),
        Index("ix_inventory_items_character_id_item_type", "character_id", "item_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="inventory_items")


class RetreatState(Base):
    """角色闭关状态。"""

    __tablename__ = "retreat_states"
    __table_args__ = (
        UniqueConstraint("character_id", name="uq_retreat_states_character_id"),
        Index("ix_retreat_states_scheduled_end_at", "scheduled_end_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    scheduled_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="retreat_state")


class HealingState(Base):
    """角色疗伤状态。"""

    __tablename__ = "healing_states"
    __table_args__ = (
        UniqueConstraint("character_id", name="uq_healing_states_character_id"),
        Index("ix_healing_states_scheduled_end_at", "scheduled_end_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    injury_level: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    scheduled_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="healing_state")


class EndlessRunState(Base):
    """无尽副本当前运行状态。"""

    __tablename__ = "endless_run_states"
    __table_args__ = (
        UniqueConstraint("character_id", name="uq_endless_run_states_character_id"),
        Index("ix_endless_run_states_highest_floor_reached", "highest_floor_reached"),
        Index("ix_endless_run_states_status", "status"),
        Index("ix_endless_run_states_current_node_type", "current_node_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_start_floor: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_floor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    highest_floor_reached: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_node_type: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    last_region_bias_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_enemy_template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_seed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    pending_rewards_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    run_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="endless_run_state")


class BreakthroughTrialProgress(Base):
    """突破秘境映射维度的进度记录。"""

    __tablename__ = "breakthrough_trial_progress"
    __table_args__ = (
        UniqueConstraint("character_id", "mapping_id", name="uq_breakthrough_trial_progress_character_mapping_id"),
        Index("ix_breakthrough_trial_progress_group_id", "group_id"),
        Index("ix_breakthrough_trial_progress_character_id_status", "character_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    mapping_id: Mapped[str] = mapped_column(String(64), nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cleared_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    best_clear_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    first_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    last_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    qualification_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    last_reward_direction: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="breakthrough_progress_entries")


class BreakthroughRewardLedger(Base):
    """突破秘境重复挑战方向级软限制账本。"""

    __tablename__ = "breakthrough_reward_ledgers"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "reward_direction",
            "cycle_type",
            "cycle_anchor_date",
            name="uq_breakthrough_reward_ledgers_character_direction_cycle",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    reward_direction: Mapped[str] = mapped_column(String(64), nullable=False)
    cycle_type: Mapped[str] = mapped_column(String(32), nullable=False)
    cycle_anchor_date: Mapped[date] = mapped_column(Date, nullable=False)
    high_yield_settlement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)

    character: Mapped[Character] = relationship(back_populates="breakthrough_reward_ledgers")


class PvpDefenseSnapshot(Base):
    """PVP 防守快照。"""

    __tablename__ = "pvp_defense_snapshots"
    __table_args__ = (
        UniqueConstraint("character_id", "snapshot_version", name="uq_pvp_defense_snapshots_character_version"),
        Index("ix_pvp_defense_snapshots_power_score", "power_score"),
        Index("ix_pvp_defense_snapshots_character_id", "character_id"),
        Index("ix_pvp_defense_snapshots_character_lock_expires_at", "character_id", "lock_expires_at"),
        Index("ix_pvp_defense_snapshots_build_fingerprint", "build_fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    power_score: Mapped[int] = mapped_column(Integer, nullable=False)
    public_power_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hidden_pvp_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score_version: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    build_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    rank_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    formation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    lock_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    lock_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="pvp_defense_snapshots")


class PvpDailyActivityLedger(Base):
    """PVP 每日挑战活动账本。"""

    __tablename__ = "pvp_daily_activity_ledgers"
    __table_args__ = (
        UniqueConstraint("character_id", "cycle_anchor_date", name="uq_pvp_daily_activity_ledgers_character_cycle"),
        Index("ix_pvp_daily_activity_ledgers_cycle_anchor_date", "cycle_anchor_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    cycle_anchor_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_challenge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_challenge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    defense_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_challenge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    character: Mapped[Character] = relationship()


class LeaderboardSnapshot(Base):
    """单次榜单快照头记录。"""

    __tablename__ = "leaderboard_snapshots"
    __table_args__ = (Index("ix_leaderboard_snapshots_board_type_generated_at", "board_type", "generated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    board_type: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    entries: Mapped[list[LeaderboardEntrySnapshot]] = relationship(
        back_populates="leaderboard_snapshot",
        cascade="all, delete-orphan",
        order_by="LeaderboardEntrySnapshot.rank_position",
    )


class LeaderboardEntrySnapshot(Base):
    """榜单单行快照。"""

    __tablename__ = "leaderboard_entry_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "leaderboard_snapshot_id",
            "rank_position",
            name="uq_leaderboard_entry_snapshots_snapshot_rank_position",
        ),
        UniqueConstraint(
            "leaderboard_snapshot_id",
            "character_id",
            name="uq_leaderboard_entry_snapshots_snapshot_character_id",
        ),
        Index("ix_leaderboard_entry_snapshots_score", "score"),
        Index("ix_leaderboard_entry_snapshots_character_id", "character_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    leaderboard_snapshot_id: Mapped[int] = mapped_column(ForeignKey("leaderboard_snapshots.id"), nullable=False)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    rank_position: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    leaderboard_snapshot: Mapped[LeaderboardSnapshot] = relationship(back_populates="entries")
    character: Mapped[Character] = relationship()


class BattleReport(Base):
    """战报记录。"""

    __tablename__ = "battle_reports"
    __table_args__ = (
        Index("ix_battle_reports_character_id_occurred_at", "character_id", "occurred_at"),
        Index("ix_battle_reports_battle_type", "battle_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    battle_type: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    opponent_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    detail_log_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="battle_reports")
    drop_records: Mapped[list[DropRecord]] = relationship(
        back_populates="battle_report",
        order_by="DropRecord.occurred_at",
    )


class PvpChallengeRecord(Base):
    """PVP 挑战结算记录。"""

    __tablename__ = "pvp_challenge_records"
    __table_args__ = (
        UniqueConstraint("battle_report_id", name="uq_pvp_challenge_records_battle_report_id"),
        Index(
            "ix_pvp_challenge_records_attacker_cycle_created_at",
            "attacker_character_id",
            "cycle_anchor_date",
            "created_at",
        ),
        Index(
            "ix_pvp_challenge_records_defender_cycle_created_at",
            "defender_character_id",
            "cycle_anchor_date",
            "created_at",
        ),
        Index(
            "ix_pvp_challenge_records_attacker_defender_cycle",
            "attacker_character_id",
            "defender_character_id",
            "cycle_anchor_date",
        ),
        Index("ix_pvp_challenge_records_leaderboard_snapshot_id", "leaderboard_snapshot_id"),
        Index("ix_pvp_challenge_records_defender_snapshot_id", "defender_snapshot_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    attacker_character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    defender_character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    defender_snapshot_id: Mapped[int] = mapped_column(ForeignKey("pvp_defense_snapshots.id"), nullable=False)
    leaderboard_snapshot_id: Mapped[int] = mapped_column(ForeignKey("leaderboard_snapshots.id"), nullable=False)
    battle_report_id: Mapped[int] = mapped_column(ForeignKey("battle_reports.id"), nullable=False)
    cycle_anchor_date: Mapped[date] = mapped_column(Date, nullable=False)
    battle_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    rank_before_attacker: Mapped[int] = mapped_column(Integer, nullable=False)
    rank_before_defender: Mapped[int] = mapped_column(Integer, nullable=False)
    rank_after_attacker: Mapped[int] = mapped_column(Integer, nullable=False)
    rank_after_defender: Mapped[int] = mapped_column(Integer, nullable=False)
    honor_coin_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rank_effect_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    settlement_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )

    attacker_character: Mapped[Character] = relationship(foreign_keys=[attacker_character_id])
    defender_character: Mapped[Character] = relationship(foreign_keys=[defender_character_id])
    defender_snapshot: Mapped[PvpDefenseSnapshot] = relationship()
    leaderboard_snapshot: Mapped[LeaderboardSnapshot] = relationship()
    battle_report: Mapped[BattleReport] = relationship()


class HonorCoinLedger(Base):
    """荣誉币流水。"""

    __tablename__ = "honor_coin_ledgers"
    __table_args__ = (
        Index("ix_honor_coin_ledgers_character_id_created_at", "character_id", "created_at"),
        Index("ix_honor_coin_ledgers_source_type", "source_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )

    character: Mapped[Character] = relationship()


class DropRecord(Base):
    """掉落记录。"""

    __tablename__ = "drop_records"
    __table_args__ = (
        Index("ix_drop_records_character_id_occurred_at", "character_id", "occurred_at"),
        Index("ix_drop_records_source_type", "source_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"), nullable=False)
    battle_report_id: Mapped[int | None] = mapped_column(ForeignKey("battle_reports.id"), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    items_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    currencies_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        nullable=False,
        server_default=func.now(),
    )

    character: Mapped[Character] = relationship(back_populates="drop_records")
    battle_report: Mapped[BattleReport | None] = relationship(back_populates="drop_records")


__all__ = [
    "ArtifactNurtureState",
    "ArtifactProfile",
    "BattleReport",
    "BreakthroughRewardLedger",
    "BreakthroughTrialProgress",
    "Character",
    "CharacterProgress",
    "CharacterScoreSnapshot",
    "CharacterSkillItem",
    "CharacterSkillLoadout",
    "CurrencyBalance",
    "DropRecord",
    "EndlessRunState",
    "EquipmentAffix",
    "EquipmentDismantleRecord",
    "EquipmentEnhancement",
    "EquipmentItem",
    "EquipmentNamingState",
    "HealingState",
    "HonorCoinLedger",
    "InventoryItem",
    "LeaderboardEntrySnapshot",
    "LeaderboardSnapshot",
    "Player",
    "PvpChallengeRecord",
    "PvpDailyActivityLedger",
    "PvpDefenseSnapshot",
    "RetreatState",
    "SystemMarker",
]
