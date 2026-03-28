"""阶段 2 仓储接口与 SQLAlchemy 实现。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from domain.breakthrough import (
    BreakthroughProgressSnapshot,
    BreakthroughRewardCycleType,
    BreakthroughRewardDirection,
    BreakthroughTrialProgressStatus,
)
from infrastructure.db.models import (
    BattleReport,
    BreakthroughRewardLedger,
    BreakthroughTrialProgress,
    Character,
    CharacterProgress,
    CharacterScoreSnapshot,
    CharacterSkillItem,
    CharacterSkillLoadout,
    CurrencyBalance,
    DropRecord,
    EndlessRunState,
    EquipmentItem,
    HealingState,
    HonorCoinLedger,
    InventoryItem,
    ItemNamingBatch,
    LeaderboardEntrySnapshot,
    LeaderboardSnapshot,
    Player,
    PvpChallengeRecord,
    PvpDailyActivityLedger,
    PvpDefenseSnapshot,
    RetreatState,
)


@dataclass(frozen=True, slots=True)
class CharacterAggregate:
    """角色基础聚合读取结果。"""

    player: Player
    character: Character
    progress: CharacterProgress | None
    skill_loadout: CharacterSkillLoadout | None
    currency_balance: CurrencyBalance | None
    score_snapshot: CharacterScoreSnapshot | None
    equipment_items: Sequence[EquipmentItem]
    inventory_items: Sequence[InventoryItem]


def build_breakthrough_progress_snapshot(progress: BreakthroughTrialProgress) -> BreakthroughProgressSnapshot:
    """把突破秘境进度模型转换为只读快照。"""
    status = BreakthroughTrialProgressStatus(progress.status) if progress.status else None
    return BreakthroughProgressSnapshot(
        mapping_id=progress.mapping_id,
        status=status,
        attempt_count=max(0, progress.attempt_count),
        cleared_count=max(0, progress.cleared_count),
        best_clear_at=None if progress.best_clear_at is None else progress.best_clear_at.isoformat(),
        first_cleared_at=None if progress.first_cleared_at is None else progress.first_cleared_at.isoformat(),
        last_cleared_at=None if progress.last_cleared_at is None else progress.last_cleared_at.isoformat(),
        qualification_granted_at=(
            None if progress.qualification_granted_at is None else progress.qualification_granted_at.isoformat()
        ),
        last_reward_direction=progress.last_reward_direction,
    )



def _equipment_detail_load_options() -> tuple:
    """返回装备明细查询所需的关联加载配置。"""
    return (
        selectinload(EquipmentItem.enhancement),
        selectinload(EquipmentItem.affixes),
        selectinload(EquipmentItem.artifact_profile),
        selectinload(EquipmentItem.artifact_nurture_state),
        selectinload(EquipmentItem.naming_state),
        selectinload(EquipmentItem.dismantle_record),
    )


class PlayerRepository(ABC):
    """玩家仓储接口。"""

    @abstractmethod
    def add(self, player: Player) -> Player:
        """新增玩家。"""

    @abstractmethod
    def get(self, player_id: int) -> Player | None:
        """按主键读取玩家。"""

    @abstractmethod
    def get_by_discord_user_id(self, discord_user_id: str) -> Player | None:
        """按 Discord 用户标识读取玩家。"""


class CharacterRepository(ABC):
    """角色聚合仓储接口。"""

    @abstractmethod
    def add(self, character: Character) -> Character:
        """新增角色聚合。"""

    @abstractmethod
    def save(self, character: Character) -> Character:
        """保存角色聚合。"""

    @abstractmethod
    def get(self, character_id: int) -> Character | None:
        """按主键读取角色。"""

    @abstractmethod
    def get_by_player_id(self, player_id: int) -> Character | None:
        """按玩家主键读取角色。"""

    @abstractmethod
    def get_aggregate(self, character_id: int) -> CharacterAggregate | None:
        """读取角色基础聚合。"""

    @abstractmethod
    def list_aggregates_for_ranking(self) -> Sequence[CharacterAggregate]:
        """读取榜单刷新所需的角色聚合。"""

    @abstractmethod
    def save_progress(self, progress: CharacterProgress) -> CharacterProgress:
        """保存角色成长状态。"""

    @abstractmethod
    def save_skill_loadout(self, skill_loadout: CharacterSkillLoadout) -> CharacterSkillLoadout:
        """保存角色功法配置。"""

    @abstractmethod
    def save_currency_balance(self, currency_balance: CurrencyBalance) -> CurrencyBalance:
        """保存角色货币余额。"""

    @abstractmethod
    def save_score_cache(
        self,
        *,
        character: Character,
        total_power_score: int,
        public_power_score: int,
        hidden_pvp_score: int,
    ) -> Character:
        """统一保存角色三项评分缓存。"""


class CharacterScoreSnapshotRepository(ABC):
    """角色评分明细快照仓储接口。"""

    @abstractmethod
    def get_by_character_id(self, character_id: int) -> CharacterScoreSnapshot | None:
        """按角色主键读取评分明细快照。"""

    @abstractmethod
    def upsert_snapshot(self, snapshot: CharacterScoreSnapshot) -> CharacterScoreSnapshot:
        """新增或更新角色评分明细快照。"""


class SkillRepository(ABC):
    """功法实例与装配仓储接口。"""

    @abstractmethod
    def add_skill_item(self, skill_item: CharacterSkillItem) -> CharacterSkillItem:
        """新增功法实例。"""

    @abstractmethod
    def save_skill_item(self, skill_item: CharacterSkillItem) -> CharacterSkillItem:
        """保存功法实例。"""

    @abstractmethod
    def get_skill_item(self, skill_item_id: int) -> CharacterSkillItem | None:
        """按主键读取功法实例。"""

    @abstractmethod
    def get_skill_item_by_character_and_id(self, character_id: int, skill_item_id: int) -> CharacterSkillItem | None:
        """按角色与主键读取功法实例。"""

    @abstractmethod
    def list_skill_items_by_character_id(self, character_id: int) -> Sequence[CharacterSkillItem]:
        """读取角色全部功法实例。"""

    @abstractmethod
    def save_skill_loadout(self, skill_loadout: CharacterSkillLoadout) -> CharacterSkillLoadout:
        """保存角色当前功法装配。"""

    @abstractmethod
    def get_skill_loadout(self, character_id: int) -> CharacterSkillLoadout | None:
        """读取角色当前功法装配。"""


class EquipmentRepository(ABC):
    """装备与法宝仓储接口。"""

    @abstractmethod
    def add(self, equipment_item: EquipmentItem) -> EquipmentItem:
        """新增装备实例。"""

    @abstractmethod
    def save(self, equipment_item: EquipmentItem) -> EquipmentItem:
        """保存装备实例与其关联明细。"""

    @abstractmethod
    def get(self, equipment_item_id: int) -> EquipmentItem | None:
        """按主键读取装备实例。"""

    @abstractmethod
    def get_by_character_and_id(self, character_id: int, equipment_item_id: int) -> EquipmentItem | None:
        """按角色与装备主键读取装备实例。"""

    @abstractmethod
    def get_equipped_in_slot(self, character_id: int, equipped_slot_id: str) -> EquipmentItem | None:
        """读取角色指定装备位的当前装备。"""

    @abstractmethod
    def list_by_character_id(self, character_id: int) -> Sequence[EquipmentItem]:
        """读取角色全部装备。"""

    @abstractmethod
    def list_active_by_character_id(self, character_id: int) -> Sequence[EquipmentItem]:
        """读取角色全部未分解装备。"""

    @abstractmethod
    def list_dismantled_by_character_id(self, character_id: int) -> Sequence[EquipmentItem]:
        """读取角色全部已分解装备。"""

    @abstractmethod
    def list_equipped_by_character_id(self, character_id: int) -> Sequence[EquipmentItem]:
        """读取角色当前已装备实例。"""


class InventoryRepository(ABC):
    """库存仓储接口。"""

    @abstractmethod
    def upsert_item(self, inventory_item: InventoryItem) -> InventoryItem:
        """新增或更新库存条目。"""

    @abstractmethod
    def get_item(self, character_id: int, item_type: str, item_id: str) -> InventoryItem | None:
        """读取单个库存条目。"""

    @abstractmethod
    def list_by_character_id(self, character_id: int) -> Sequence[InventoryItem]:
        """读取角色全部库存。"""

    @abstractmethod
    def list_by_character_id_and_type(self, character_id: int, item_type: str) -> Sequence[InventoryItem]:
        """读取角色指定类型库存。"""


class StateRepository(ABC):
    """角色当前状态仓储接口。"""

    @abstractmethod
    def save_retreat_state(self, retreat_state: RetreatState) -> RetreatState:
        """保存闭关状态。"""

    @abstractmethod
    def get_retreat_state(self, character_id: int) -> RetreatState | None:
        """读取闭关状态。"""

    @abstractmethod
    def save_healing_state(self, healing_state: HealingState) -> HealingState:
        """保存疗伤状态。"""

    @abstractmethod
    def get_healing_state(self, character_id: int) -> HealingState | None:
        """读取疗伤状态。"""

    @abstractmethod
    def save_endless_run_state(self, endless_run_state: EndlessRunState) -> EndlessRunState:
        """保存无尽副本运行状态。"""

    @abstractmethod
    def get_endless_run_state(self, character_id: int) -> EndlessRunState | None:
        """读取无尽副本运行状态。"""

    @abstractmethod
    def save_item_naming_batch(self, item_naming_batch: ItemNamingBatch) -> ItemNamingBatch:
        """保存实例命名批次。"""

    @abstractmethod
    def get_item_naming_batch(self, batch_id: int) -> ItemNamingBatch | None:
        """按主键读取实例命名批次。"""

    @abstractmethod
    def get_item_naming_batch_by_source(
        self,
        *,
        character_id: int,
        source_type: str,
        source_ref: str,
    ) -> ItemNamingBatch | None:
        """按角色与来源读取实例命名批次。"""

    @abstractmethod
    def list_item_naming_batches_by_status(self, status: str, *, limit: int = 20) -> Sequence[ItemNamingBatch]:
        """按状态读取实例命名批次。"""

    @abstractmethod
    def has_running_endless_run(self, character_id: int) -> bool:
        """判断角色是否存在进行中的无尽副本运行。"""


class BreakthroughRepository(ABC):
    """突破秘境进度仓储接口。"""

    @abstractmethod
    def save_progress(self, progress: BreakthroughTrialProgress) -> BreakthroughTrialProgress:
        """保存突破秘境进度。"""

    @abstractmethod
    def get_progress(self, character_id: int, mapping_id: str) -> BreakthroughTrialProgress | None:
        """读取单条突破秘境进度。"""

    @abstractmethod
    def get_or_create_progress(
        self,
        character_id: int,
        mapping_id: str,
        *,
        group_id: str,
        default_status: BreakthroughTrialProgressStatus = BreakthroughTrialProgressStatus.FAILED,
    ) -> BreakthroughTrialProgress:
        """读取或创建单条突破秘境进度。"""

    @abstractmethod
    def list_by_character_id(self, character_id: int) -> Sequence[BreakthroughTrialProgress]:
        """读取角色全部突破秘境进度。"""

    @abstractmethod
    def list_cleared_by_character_id(self, character_id: int) -> Sequence[BreakthroughTrialProgress]:
        """读取角色已首通的突破秘境进度。"""


class BreakthroughRewardLedgerRepository(ABC):
    """突破秘境方向级软限制账本仓储接口。"""

    @abstractmethod
    def save_ledger(self, ledger: BreakthroughRewardLedger) -> BreakthroughRewardLedger:
        """保存方向级软限制账本。"""

    @abstractmethod
    def get_ledger(
        self,
        character_id: int,
        reward_direction: BreakthroughRewardDirection,
        cycle_type: BreakthroughRewardCycleType,
        cycle_anchor_date: date,
    ) -> BreakthroughRewardLedger | None:
        """读取单个方向级软限制账本。"""

    @abstractmethod
    def get_or_create_ledger(
        self,
        character_id: int,
        reward_direction: BreakthroughRewardDirection,
        cycle_type: BreakthroughRewardCycleType,
        cycle_anchor_date: date,
    ) -> BreakthroughRewardLedger:
        """读取或创建单个方向级软限制账本。"""


class SnapshotRepository(ABC):
    """快照与榜单仓储接口。"""

    @abstractmethod
    def add_pvp_defense_snapshot(self, snapshot: PvpDefenseSnapshot) -> PvpDefenseSnapshot:
        """新增 PVP 防守快照。"""

    @abstractmethod
    def get_latest_pvp_defense_snapshot(self, character_id: int) -> PvpDefenseSnapshot | None:
        """读取角色最新 PVP 防守快照。"""

    @abstractmethod
    def get_active_pvp_defense_snapshot(self, character_id: int, now: datetime) -> PvpDefenseSnapshot | None:
        """读取角色当前仍在锁定周期内的 PVP 防守快照。"""

    @abstractmethod
    def get_pvp_defense_snapshot(self, snapshot_id: int) -> PvpDefenseSnapshot | None:
        """按主键读取单个 PVP 防守快照。"""

    @abstractmethod
    def add_leaderboard_snapshot(self, snapshot: LeaderboardSnapshot) -> LeaderboardSnapshot:
        """新增榜单快照。"""

    @abstractmethod
    def replace_leaderboard_snapshot(self, snapshot: LeaderboardSnapshot) -> LeaderboardSnapshot:
        """写入新榜单快照并替换同榜单旧快照。"""

    @abstractmethod
    def get_latest_leaderboard(self, board_type: str) -> LeaderboardSnapshot | None:
        """读取某种榜单的最新快照。"""

    @abstractmethod
    def list_latest_leaderboard_entries(self, board_type: str) -> Sequence[LeaderboardEntrySnapshot]:
        """读取某种榜单最新快照的全部条目。"""

    @abstractmethod
    def get_latest_leaderboard_entry(self, board_type: str, character_id: int) -> LeaderboardEntrySnapshot | None:
        """读取某角色在最新榜单快照中的条目。"""

    @abstractmethod
    def list_leaderboard_entries(
        self,
        leaderboard_snapshot_id: int,
        *,
        limit: int,
        offset: int = 0,
    ) -> Sequence[LeaderboardEntrySnapshot]:
        """按分页条件读取榜单条目。"""


class PvpChallengeRepository(ABC):
    """PVP 挑战与日账本仓储接口。"""

    @abstractmethod
    def add_challenge_record(self, record: PvpChallengeRecord) -> PvpChallengeRecord:
        """新增 PVP 挑战结算记录。"""

    @abstractmethod
    def list_challenge_records_by_attacker(
        self,
        attacker_character_id: int,
        cycle_anchor_date: date,
    ) -> Sequence[PvpChallengeRecord]:
        """读取攻击方在某个自然日内的挑战记录。"""

    @abstractmethod
    def get_latest_challenge_record_by_attacker(self, attacker_character_id: int) -> PvpChallengeRecord | None:
        """读取攻击方最近一次挑战结算记录。"""

    @abstractmethod
    def count_effective_challenges_against_target(
        self,
        attacker_character_id: int,
        defender_character_id: int,
        cycle_anchor_date: date,
    ) -> int:
        """统计攻击方在某个自然日内对同一目标的有效结算次数。"""

    @abstractmethod
    def get_daily_activity(self, character_id: int, cycle_anchor_date: date) -> PvpDailyActivityLedger | None:
        """读取角色在某个自然日内的 PVP 活动账本。"""

    @abstractmethod
    def get_or_create_daily_activity(self, character_id: int, cycle_anchor_date: date) -> PvpDailyActivityLedger:
        """读取或创建角色在某个自然日内的 PVP 活动账本。"""

    @abstractmethod
    def save_daily_activity(self, ledger: PvpDailyActivityLedger) -> PvpDailyActivityLedger:
        """保存角色在某个自然日内的 PVP 活动账本。"""


class HonorCoinLedgerRepository(ABC):
    """荣誉币流水仓储接口。"""

    @abstractmethod
    def add_ledger(self, ledger: HonorCoinLedger) -> HonorCoinLedger:
        """新增荣誉币流水。"""

    @abstractmethod
    def list_by_character_id(self, character_id: int, *, limit: int) -> Sequence[HonorCoinLedger]:
        """按角色读取荣誉币流水，按时间倒序返回。"""


class BattleRecordRepository(ABC):
    """战报与掉落仓储接口。"""

    @abstractmethod
    def add_battle_report(self, battle_report: BattleReport) -> BattleReport:
        """新增战报。"""

    @abstractmethod
    def list_battle_reports(self, character_id: int) -> Sequence[BattleReport]:
        """读取角色战报。"""

    @abstractmethod
    def add_drop_record(self, drop_record: DropRecord) -> DropRecord:
        """新增掉落记录。"""

    @abstractmethod
    def list_drop_records(self, character_id: int) -> Sequence[DropRecord]:
        """读取角色掉落记录。"""


class SqlAlchemyPlayerRepository(PlayerRepository):
    """玩家仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, player: Player) -> Player:
        self._session.add(player)
        self._session.flush()
        return player

    def get(self, player_id: int) -> Player | None:
        return self._session.get(Player, player_id)

    def get_by_discord_user_id(self, discord_user_id: str) -> Player | None:
        statement = select(Player).where(Player.discord_user_id == discord_user_id)
        return self._session.scalar(statement)


class SqlAlchemyCharacterRepository(CharacterRepository):
    """角色聚合仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, character: Character) -> Character:
        self._session.add(character)
        self._session.flush()
        return character

    def save(self, character: Character) -> Character:
        self._session.add(character)
        self._session.flush()
        return character

    def get(self, character_id: int) -> Character | None:
        return self._session.get(Character, character_id)

    def get_by_player_id(self, player_id: int) -> Character | None:
        statement = select(Character).where(Character.player_id == player_id)
        return self._session.scalar(statement)

    def get_aggregate(self, character_id: int) -> CharacterAggregate | None:
        statement = (
            select(Character)
            .execution_options(populate_existing=True)
            .options(
                joinedload(Character.player),
                selectinload(Character.progress),
                selectinload(Character.skill_loadout),
                selectinload(Character.currency_balance),
                selectinload(Character.score_snapshot),
                selectinload(Character.inventory_items),
                selectinload(Character.equipment_items).options(*_equipment_detail_load_options()),
            )
            .where(Character.id == character_id)
        )
        character = self._session.scalar(statement)
        if character is None:
            return None
        return CharacterAggregate(
            player=character.player,
            character=character,
            progress=character.progress,
            skill_loadout=character.skill_loadout,
            currency_balance=character.currency_balance,
            score_snapshot=character.score_snapshot,
            equipment_items=character.equipment_items,
            inventory_items=character.inventory_items,
        )

    def list_aggregates_for_ranking(self) -> Sequence[CharacterAggregate]:
        statement = (
            select(Character)
            .execution_options(populate_existing=True)
            .options(
                joinedload(Character.player),
                selectinload(Character.progress),
                selectinload(Character.skill_loadout),
                selectinload(Character.currency_balance),
                selectinload(Character.score_snapshot),
                selectinload(Character.inventory_items),
                selectinload(Character.equipment_items).options(*_equipment_detail_load_options()),
            )
            .order_by(Character.id.asc())
        )
        characters = self._session.scalars(statement).all()
        return [
            CharacterAggregate(
                player=character.player,
                character=character,
                progress=character.progress,
                skill_loadout=character.skill_loadout,
                currency_balance=character.currency_balance,
                score_snapshot=character.score_snapshot,
                equipment_items=character.equipment_items,
                inventory_items=character.inventory_items,
            )
            for character in characters
        ]

    def save_progress(self, progress: CharacterProgress) -> CharacterProgress:
        self._session.add(progress)
        self._session.flush()
        return progress

    def save_skill_loadout(self, skill_loadout: CharacterSkillLoadout) -> CharacterSkillLoadout:
        self._session.add(skill_loadout)
        self._session.flush()
        return skill_loadout

    def save_currency_balance(self, currency_balance: CurrencyBalance) -> CurrencyBalance:
        self._session.add(currency_balance)
        self._session.flush()
        return currency_balance

    def save_score_cache(
        self,
        *,
        character: Character,
        total_power_score: int,
        public_power_score: int,
        hidden_pvp_score: int,
    ) -> Character:
        character.total_power_score = total_power_score
        character.public_power_score = public_power_score
        character.hidden_pvp_score = hidden_pvp_score
        self._session.add(character)
        self._session.flush()
        return character


class SqlAlchemyCharacterScoreSnapshotRepository(CharacterScoreSnapshotRepository):
    """角色评分明细快照仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_character_id(self, character_id: int) -> CharacterScoreSnapshot | None:
        statement = select(CharacterScoreSnapshot).where(CharacterScoreSnapshot.character_id == character_id)
        return self._session.scalar(statement)

    def upsert_snapshot(self, snapshot: CharacterScoreSnapshot) -> CharacterScoreSnapshot:
        existing = self.get_by_character_id(snapshot.character_id)
        if existing is None:
            self._session.add(snapshot)
            self._session.flush()
            return snapshot

        existing.score_version = snapshot.score_version
        existing.total_power_score = snapshot.total_power_score
        existing.public_power_score = snapshot.public_power_score
        existing.hidden_pvp_score = snapshot.hidden_pvp_score
        existing.growth_score = snapshot.growth_score
        existing.equipment_score = snapshot.equipment_score
        existing.skill_score = snapshot.skill_score
        existing.artifact_score = snapshot.artifact_score
        existing.pvp_adjustment_score = snapshot.pvp_adjustment_score
        existing.breakdown_json = snapshot.breakdown_json
        existing.source_digest = snapshot.source_digest
        existing.computed_at = snapshot.computed_at
        self._session.add(existing)
        self._session.flush()
        return existing


class SqlAlchemySkillRepository(SkillRepository):
    """功法实例与装配仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_skill_item(self, skill_item: CharacterSkillItem) -> CharacterSkillItem:
        self._session.add(skill_item)
        self._session.flush()
        return skill_item

    def save_skill_item(self, skill_item: CharacterSkillItem) -> CharacterSkillItem:
        self._session.add(skill_item)
        self._session.flush()
        return skill_item

    def get_skill_item(self, skill_item_id: int) -> CharacterSkillItem | None:
        return self._session.get(CharacterSkillItem, skill_item_id)

    def get_skill_item_by_character_and_id(self, character_id: int, skill_item_id: int) -> CharacterSkillItem | None:
        statement = select(CharacterSkillItem).where(
            CharacterSkillItem.character_id == character_id,
            CharacterSkillItem.id == skill_item_id,
        )
        return self._session.scalar(statement)

    def list_skill_items_by_character_id(self, character_id: int) -> Sequence[CharacterSkillItem]:
        statement = (
            select(CharacterSkillItem)
            .where(CharacterSkillItem.character_id == character_id)
            .order_by(CharacterSkillItem.id.asc())
        )
        return self._session.scalars(statement).all()

    def save_skill_loadout(self, skill_loadout: CharacterSkillLoadout) -> CharacterSkillLoadout:
        self._session.add(skill_loadout)
        self._session.flush()
        return skill_loadout

    def get_skill_loadout(self, character_id: int) -> CharacterSkillLoadout | None:
        statement = select(CharacterSkillLoadout).where(CharacterSkillLoadout.character_id == character_id)
        return self._session.scalar(statement)


class SqlAlchemyEquipmentRepository(EquipmentRepository):
    """装备与法宝仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, equipment_item: EquipmentItem) -> EquipmentItem:
        self._session.add(equipment_item)
        self._session.flush()
        return equipment_item

    def save(self, equipment_item: EquipmentItem) -> EquipmentItem:
        self._session.add(equipment_item)
        self._session.flush()
        return equipment_item

    def get(self, equipment_item_id: int) -> EquipmentItem | None:
        statement = (
            select(EquipmentItem)
            .options(*_equipment_detail_load_options())
            .where(EquipmentItem.id == equipment_item_id)
        )
        return self._session.scalar(statement)

    def get_by_character_and_id(self, character_id: int, equipment_item_id: int) -> EquipmentItem | None:
        statement = (
            select(EquipmentItem)
            .options(*_equipment_detail_load_options())
            .where(EquipmentItem.character_id == character_id, EquipmentItem.id == equipment_item_id)
        )
        return self._session.scalar(statement)

    def get_equipped_in_slot(self, character_id: int, equipped_slot_id: str) -> EquipmentItem | None:
        statement = (
            select(EquipmentItem)
            .options(*_equipment_detail_load_options())
            .where(
                EquipmentItem.character_id == character_id,
                EquipmentItem.equipped_slot_id == equipped_slot_id,
                EquipmentItem.item_state == "active",
            )
        )
        return self._session.scalar(statement)

    def list_by_character_id(self, character_id: int) -> Sequence[EquipmentItem]:
        statement = (
            select(EquipmentItem)
            .options(*_equipment_detail_load_options())
            .where(EquipmentItem.character_id == character_id)
            .order_by(EquipmentItem.id)
        )
        return self._session.scalars(statement).all()

    def list_active_by_character_id(self, character_id: int) -> Sequence[EquipmentItem]:
        statement = (
            select(EquipmentItem)
            .options(*_equipment_detail_load_options())
            .where(EquipmentItem.character_id == character_id, EquipmentItem.item_state == "active")
            .order_by(EquipmentItem.id)
        )
        return self._session.scalars(statement).all()

    def list_dismantled_by_character_id(self, character_id: int) -> Sequence[EquipmentItem]:
        statement = (
            select(EquipmentItem)
            .options(*_equipment_detail_load_options())
            .where(EquipmentItem.character_id == character_id, EquipmentItem.item_state == "dismantled")
            .order_by(EquipmentItem.dismantled_at.desc(), EquipmentItem.id.desc())
        )
        return self._session.scalars(statement).all()

    def list_equipped_by_character_id(self, character_id: int) -> Sequence[EquipmentItem]:
        statement = (
            select(EquipmentItem)
            .options(*_equipment_detail_load_options())
            .where(
                EquipmentItem.character_id == character_id,
                EquipmentItem.equipped_slot_id.is_not(None),
                EquipmentItem.item_state == "active",
            )
            .order_by(EquipmentItem.equipped_slot_id.asc(), EquipmentItem.id.asc())
        )
        return self._session.scalars(statement).all()


class SqlAlchemyInventoryRepository(InventoryRepository):
    """库存仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_item(self, inventory_item: InventoryItem) -> InventoryItem:
        existing = self.get_item(
            inventory_item.character_id,
            inventory_item.item_type,
            inventory_item.item_id,
        )
        if existing is None:
            self._session.add(inventory_item)
            self._session.flush()
            return inventory_item
        existing.quantity = inventory_item.quantity
        existing.item_payload_json = inventory_item.item_payload_json
        self._session.flush()
        return existing

    def get_item(self, character_id: int, item_type: str, item_id: str) -> InventoryItem | None:
        statement = select(InventoryItem).where(
            InventoryItem.character_id == character_id,
            InventoryItem.item_type == item_type,
            InventoryItem.item_id == item_id,
        )
        return self._session.scalar(statement)

    def list_by_character_id(self, character_id: int) -> Sequence[InventoryItem]:
        statement = (
            select(InventoryItem)
            .where(InventoryItem.character_id == character_id)
            .order_by(InventoryItem.item_type.asc(), InventoryItem.item_id.asc())
        )
        return self._session.scalars(statement).all()

    def list_by_character_id_and_type(self, character_id: int, item_type: str) -> Sequence[InventoryItem]:
        statement = (
            select(InventoryItem)
            .where(InventoryItem.character_id == character_id, InventoryItem.item_type == item_type)
            .order_by(InventoryItem.item_id.asc())
        )
        return self._session.scalars(statement).all()


class SqlAlchemyStateRepository(StateRepository):
    """角色当前状态仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_retreat_state(self, retreat_state: RetreatState) -> RetreatState:
        self._session.add(retreat_state)
        self._session.flush()
        return retreat_state

    def get_retreat_state(self, character_id: int) -> RetreatState | None:
        statement = select(RetreatState).where(RetreatState.character_id == character_id)
        return self._session.scalar(statement)

    def save_healing_state(self, healing_state: HealingState) -> HealingState:
        self._session.add(healing_state)
        self._session.flush()
        return healing_state

    def get_healing_state(self, character_id: int) -> HealingState | None:
        statement = select(HealingState).where(HealingState.character_id == character_id)
        return self._session.scalar(statement)

    def save_endless_run_state(self, endless_run_state: EndlessRunState) -> EndlessRunState:
        self._session.add(endless_run_state)
        self._session.flush()
        return endless_run_state

    def get_endless_run_state(self, character_id: int) -> EndlessRunState | None:
        statement = select(EndlessRunState).where(EndlessRunState.character_id == character_id)
        return self._session.scalar(statement)

    def save_item_naming_batch(self, item_naming_batch: ItemNamingBatch) -> ItemNamingBatch:
        self._session.add(item_naming_batch)
        self._session.flush()
        return item_naming_batch

    def get_item_naming_batch(self, batch_id: int) -> ItemNamingBatch | None:
        return self._session.get(ItemNamingBatch, batch_id)

    def get_item_naming_batch_by_source(
        self,
        *,
        character_id: int,
        source_type: str,
        source_ref: str,
    ) -> ItemNamingBatch | None:
        statement = select(ItemNamingBatch).where(
            ItemNamingBatch.character_id == character_id,
            ItemNamingBatch.source_type == source_type,
            ItemNamingBatch.source_ref == source_ref,
        )
        return self._session.scalar(statement)

    def list_item_naming_batches_by_status(self, status: str, *, limit: int = 20) -> Sequence[ItemNamingBatch]:
        statement = (
            select(ItemNamingBatch)
            .where(ItemNamingBatch.status == status)
            .order_by(ItemNamingBatch.created_at.asc(), ItemNamingBatch.id.asc())
            .limit(max(1, limit))
        )
        return self._session.scalars(statement).all()

    def has_running_endless_run(self, character_id: int) -> bool:
        endless_run_state = self.get_endless_run_state(character_id)
        if endless_run_state is None:
            return False
        return endless_run_state.status == "running"


class SqlAlchemyBreakthroughRepository(BreakthroughRepository):
    """突破秘境进度仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_progress(self, progress: BreakthroughTrialProgress) -> BreakthroughTrialProgress:
        self._session.add(progress)
        self._session.flush()
        return progress

    def get_progress(self, character_id: int, mapping_id: str) -> BreakthroughTrialProgress | None:
        statement = select(BreakthroughTrialProgress).where(
            BreakthroughTrialProgress.character_id == character_id,
            BreakthroughTrialProgress.mapping_id == mapping_id,
        )
        return self._session.scalar(statement)

    def get_or_create_progress(
        self,
        character_id: int,
        mapping_id: str,
        *,
        group_id: str,
        default_status: BreakthroughTrialProgressStatus = BreakthroughTrialProgressStatus.FAILED,
    ) -> BreakthroughTrialProgress:
        progress = self.get_progress(character_id, mapping_id)
        if progress is not None:
            return progress
        progress = BreakthroughTrialProgress(
            character_id=character_id,
            mapping_id=mapping_id,
            group_id=group_id,
            status=default_status.value,
            attempt_count=0,
            cleared_count=0,
            last_result_json={},
        )
        self._session.add(progress)
        self._session.flush()
        return progress

    def list_by_character_id(self, character_id: int) -> Sequence[BreakthroughTrialProgress]:
        statement = (
            select(BreakthroughTrialProgress)
            .where(BreakthroughTrialProgress.character_id == character_id)
            .order_by(BreakthroughTrialProgress.mapping_id.asc())
        )
        return self._session.scalars(statement).all()

    def list_cleared_by_character_id(self, character_id: int) -> Sequence[BreakthroughTrialProgress]:
        statement = (
            select(BreakthroughTrialProgress)
            .where(
                BreakthroughTrialProgress.character_id == character_id,
                BreakthroughTrialProgress.status == BreakthroughTrialProgressStatus.CLEARED.value,
            )
            .order_by(BreakthroughTrialProgress.mapping_id.asc())
        )
        return self._session.scalars(statement).all()


class SqlAlchemyBreakthroughRewardLedgerRepository(BreakthroughRewardLedgerRepository):
    """突破秘境方向级软限制账本仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_ledger(self, ledger: BreakthroughRewardLedger) -> BreakthroughRewardLedger:
        self._session.add(ledger)
        self._session.flush()
        return ledger

    def get_ledger(
        self,
        character_id: int,
        reward_direction: BreakthroughRewardDirection,
        cycle_type: BreakthroughRewardCycleType,
        cycle_anchor_date: date,
    ) -> BreakthroughRewardLedger | None:
        statement = select(BreakthroughRewardLedger).where(
            BreakthroughRewardLedger.character_id == character_id,
            BreakthroughRewardLedger.reward_direction == reward_direction.value,
            BreakthroughRewardLedger.cycle_type == cycle_type.value,
            BreakthroughRewardLedger.cycle_anchor_date == cycle_anchor_date,
        )
        return self._session.scalar(statement)

    def get_or_create_ledger(
        self,
        character_id: int,
        reward_direction: BreakthroughRewardDirection,
        cycle_type: BreakthroughRewardCycleType,
        cycle_anchor_date: date,
    ) -> BreakthroughRewardLedger:
        ledger = self.get_ledger(character_id, reward_direction, cycle_type, cycle_anchor_date)
        if ledger is not None:
            return ledger
        ledger = BreakthroughRewardLedger(
            character_id=character_id,
            reward_direction=reward_direction.value,
            cycle_type=cycle_type.value,
            cycle_anchor_date=cycle_anchor_date,
            high_yield_settlement_count=0,
        )
        self._session.add(ledger)
        self._session.flush()
        return ledger


class SqlAlchemySnapshotRepository(SnapshotRepository):
    """快照与榜单仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_pvp_defense_snapshot(self, snapshot: PvpDefenseSnapshot) -> PvpDefenseSnapshot:
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def get_latest_pvp_defense_snapshot(self, character_id: int) -> PvpDefenseSnapshot | None:
        statement = (
            select(PvpDefenseSnapshot)
            .where(PvpDefenseSnapshot.character_id == character_id)
            .order_by(PvpDefenseSnapshot.snapshot_version.desc(), PvpDefenseSnapshot.id.desc())
        )
        return self._session.scalars(statement).first()

    def get_active_pvp_defense_snapshot(self, character_id: int, now: datetime) -> PvpDefenseSnapshot | None:
        statement = (
            select(PvpDefenseSnapshot)
            .where(
                PvpDefenseSnapshot.character_id == character_id,
                PvpDefenseSnapshot.lock_expires_at >= now,
            )
            .order_by(PvpDefenseSnapshot.snapshot_version.desc(), PvpDefenseSnapshot.id.desc())
        )
        return self._session.scalars(statement).first()

    def get_pvp_defense_snapshot(self, snapshot_id: int) -> PvpDefenseSnapshot | None:
        return self._session.get(PvpDefenseSnapshot, snapshot_id)

    def add_leaderboard_snapshot(self, snapshot: LeaderboardSnapshot) -> LeaderboardSnapshot:
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def replace_leaderboard_snapshot(self, snapshot: LeaderboardSnapshot) -> LeaderboardSnapshot:
        persisted_snapshot = self.add_leaderboard_snapshot(snapshot)
        statement = select(LeaderboardSnapshot).where(
            LeaderboardSnapshot.board_type == persisted_snapshot.board_type,
            LeaderboardSnapshot.id != persisted_snapshot.id,
        )
        stale_snapshots = self._session.scalars(statement).all()
        for stale_snapshot in stale_snapshots:
            self._session.delete(stale_snapshot)
        self._session.flush()
        return persisted_snapshot

    def get_latest_leaderboard(self, board_type: str) -> LeaderboardSnapshot | None:
        entry_options = selectinload(LeaderboardSnapshot.entries).selectinload(LeaderboardEntrySnapshot.character)
        statement = (
            select(LeaderboardSnapshot)
            .options(entry_options)
            .where(LeaderboardSnapshot.board_type == board_type)
            .order_by(LeaderboardSnapshot.generated_at.desc(), LeaderboardSnapshot.id.desc())
        )
        return self._session.scalars(statement).first()

    def list_latest_leaderboard_entries(self, board_type: str) -> Sequence[LeaderboardEntrySnapshot]:
        latest_snapshot = self.get_latest_leaderboard(board_type)
        if latest_snapshot is None:
            return ()
        return tuple(latest_snapshot.entries)

    def get_latest_leaderboard_entry(self, board_type: str, character_id: int) -> LeaderboardEntrySnapshot | None:
        latest_snapshot = self.get_latest_leaderboard(board_type)
        if latest_snapshot is None:
            return None
        for entry in latest_snapshot.entries:
            if entry.character_id == character_id:
                return entry
        return None

    def list_leaderboard_entries(
        self,
        leaderboard_snapshot_id: int,
        *,
        limit: int,
        offset: int = 0,
    ) -> Sequence[LeaderboardEntrySnapshot]:
        statement = (
            select(LeaderboardEntrySnapshot)
            .options(selectinload(LeaderboardEntrySnapshot.character))
            .where(LeaderboardEntrySnapshot.leaderboard_snapshot_id == leaderboard_snapshot_id)
            .order_by(LeaderboardEntrySnapshot.rank_position.asc())
            .offset(offset)
            .limit(limit)
        )
        return self._session.scalars(statement).all()


class SqlAlchemyPvpChallengeRepository(PvpChallengeRepository):
    """PVP 挑战与日账本仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_challenge_record(self, record: PvpChallengeRecord) -> PvpChallengeRecord:
        self._session.add(record)
        self._session.flush()
        return record

    def list_challenge_records_by_attacker(
        self,
        attacker_character_id: int,
        cycle_anchor_date: date,
    ) -> Sequence[PvpChallengeRecord]:
        statement = (
            select(PvpChallengeRecord)
            .where(
                PvpChallengeRecord.attacker_character_id == attacker_character_id,
                PvpChallengeRecord.cycle_anchor_date == cycle_anchor_date,
            )
            .order_by(PvpChallengeRecord.created_at.desc(), PvpChallengeRecord.id.desc())
        )
        return self._session.scalars(statement).all()

    def get_latest_challenge_record_by_attacker(self, attacker_character_id: int) -> PvpChallengeRecord | None:
        statement = (
            select(PvpChallengeRecord)
            .where(PvpChallengeRecord.attacker_character_id == attacker_character_id)
            .order_by(PvpChallengeRecord.created_at.desc(), PvpChallengeRecord.id.desc())
        )
        return self._session.scalars(statement).first()

    def count_effective_challenges_against_target(
        self,
        attacker_character_id: int,
        defender_character_id: int,
        cycle_anchor_date: date,
    ) -> int:
        statement = select(func.count(PvpChallengeRecord.id)).where(
            PvpChallengeRecord.attacker_character_id == attacker_character_id,
            PvpChallengeRecord.defender_character_id == defender_character_id,
            PvpChallengeRecord.cycle_anchor_date == cycle_anchor_date,
        )
        count_value = self._session.scalar(statement)
        return int(count_value or 0)

    def get_daily_activity(self, character_id: int, cycle_anchor_date: date) -> PvpDailyActivityLedger | None:
        statement = select(PvpDailyActivityLedger).where(
            PvpDailyActivityLedger.character_id == character_id,
            PvpDailyActivityLedger.cycle_anchor_date == cycle_anchor_date,
        )
        return self._session.scalar(statement)

    def get_or_create_daily_activity(self, character_id: int, cycle_anchor_date: date) -> PvpDailyActivityLedger:
        ledger = self.get_daily_activity(character_id, cycle_anchor_date)
        if ledger is not None:
            return ledger
        ledger = PvpDailyActivityLedger(
            character_id=character_id,
            cycle_anchor_date=cycle_anchor_date,
            effective_challenge_count=0,
            successful_challenge_count=0,
            defense_failure_count=0,
        )
        self._session.add(ledger)
        self._session.flush()
        return ledger

    def save_daily_activity(self, ledger: PvpDailyActivityLedger) -> PvpDailyActivityLedger:
        self._session.add(ledger)
        self._session.flush()
        return ledger


class SqlAlchemyHonorCoinLedgerRepository(HonorCoinLedgerRepository):
    """荣誉币流水仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_ledger(self, ledger: HonorCoinLedger) -> HonorCoinLedger:
        self._session.add(ledger)
        self._session.flush()
        return ledger

    def list_by_character_id(self, character_id: int, *, limit: int) -> Sequence[HonorCoinLedger]:
        statement = (
            select(HonorCoinLedger)
            .where(HonorCoinLedger.character_id == character_id)
            .order_by(HonorCoinLedger.created_at.desc(), HonorCoinLedger.id.desc())
            .limit(limit)
        )
        return self._session.scalars(statement).all()


class SqlAlchemyBattleRecordRepository(BattleRecordRepository):
    """战报与掉落仓储 SQLAlchemy 实现。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_battle_report(self, battle_report: BattleReport) -> BattleReport:
        self._session.add(battle_report)
        self._session.flush()
        return battle_report

    def list_battle_reports(self, character_id: int) -> Sequence[BattleReport]:
        statement = (
            select(BattleReport)
            .where(BattleReport.character_id == character_id)
            .order_by(BattleReport.occurred_at.desc(), BattleReport.id.desc())
        )
        return self._session.scalars(statement).all()

    def add_drop_record(self, drop_record: DropRecord) -> DropRecord:
        self._session.add(drop_record)
        self._session.flush()
        return drop_record

    def list_drop_records(self, character_id: int) -> Sequence[DropRecord]:
        statement = (
            select(DropRecord)
            .where(DropRecord.character_id == character_id)
            .order_by(DropRecord.occurred_at.desc(), DropRecord.id.desc())
        )
        return self._session.scalars(statement).all()


__all__ = [
    "BattleRecordRepository",
    "BreakthroughRepository",
    "BreakthroughRewardLedgerRepository",
    "CharacterAggregate",
    "CharacterRepository",
    "CharacterScoreSnapshotRepository",
    "EquipmentRepository",
    "HonorCoinLedgerRepository",
    "InventoryRepository",
    "PlayerRepository",
    "PvpChallengeRepository",
    "SkillRepository",
    "SnapshotRepository",
    "SqlAlchemyBattleRecordRepository",
    "SqlAlchemyBreakthroughRepository",
    "SqlAlchemyBreakthroughRewardLedgerRepository",
    "SqlAlchemyCharacterRepository",
    "SqlAlchemyCharacterScoreSnapshotRepository",
    "SqlAlchemyEquipmentRepository",
    "SqlAlchemyHonorCoinLedgerRepository",
    "SqlAlchemyInventoryRepository",
    "SqlAlchemyPlayerRepository",
    "SqlAlchemyPvpChallengeRepository",
    "SqlAlchemySkillRepository",
    "SqlAlchemySnapshotRepository",
    "SqlAlchemyStateRepository",
    "StateRepository",
    "build_breakthrough_progress_snapshot",
]
