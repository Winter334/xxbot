"""角色成长服务。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from application.ranking.score_service import CharacterScoreService
from domain.character import CharacterGrowthProgression, RealmGrowthRule, StageThreshold
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import Character, CharacterProgress, CurrencyBalance, Player
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, PlayerRepository

_DECIMAL_FULL_RATIO = Decimal("1.0000")


@dataclass(frozen=True, slots=True)
class CharacterGrowthSnapshot:
    """角色成长快照。"""

    player_id: int
    discord_user_id: str
    player_display_name: str
    character_id: int
    character_name: str
    character_title: str | None
    realm_id: str
    stage_id: str
    cultivation_value: int
    comprehension_value: int
    spirit_stone: int
    honor_coin: int
    realm_total_cultivation: int
    current_stage_entry_cultivation: int
    next_stage_id: str | None
    next_stage_entry_cultivation: int | None
    stage_thresholds: tuple[StageThreshold, ...]


@dataclass(frozen=True, slots=True)
class CultivationUpdateResult:
    """修为增加后的结果。"""

    snapshot: CharacterGrowthSnapshot
    requested_amount: int
    applied_amount: int
    previous_stage_id: str
    stage_changed: bool


class CharacterGrowthServiceError(RuntimeError):
    """角色成长服务基础异常。"""


class CharacterAlreadyExistsError(CharacterGrowthServiceError):
    """玩家已存在角色。"""


class CharacterNotFoundError(CharacterGrowthServiceError):
    """角色不存在。"""


class CharacterGrowthStateError(CharacterGrowthServiceError):
    """角色成长状态不完整。"""


class InvalidGrowthAmountError(CharacterGrowthServiceError):
    """成长数值非法。"""


class CharacterGrowthService:
    """负责编排角色创建、修为累计、感悟累计与小阶段状态更新。"""

    def __init__(
        self,
        *,
        player_repository: PlayerRepository,
        character_repository: CharacterRepository,
        static_config: StaticGameConfig | None = None,
        score_service: CharacterScoreService | None = None,
    ) -> None:
        self._player_repository = player_repository
        self._character_repository = character_repository
        self._static_config = static_config or get_static_config()
        self._progression = CharacterGrowthProgression(self._static_config)
        self._score_service = score_service

    def create_character(
        self,
        *,
        discord_user_id: str,
        player_display_name: str,
        character_name: str,
        title: str | None = None,
    ) -> CharacterGrowthSnapshot:
        """创建玩家角色，并初始化成长状态与货币余额。"""
        player = self._player_repository.get_by_discord_user_id(discord_user_id)
        if player is None:
            player = self._player_repository.add(
                Player(
                    discord_user_id=discord_user_id,
                    display_name=player_display_name,
                )
            )
        else:
            player.display_name = player_display_name

        existing_character = self._character_repository.get_by_player_id(player.id)
        if existing_character is not None:
            raise CharacterAlreadyExistsError(f"Discord 用户 {discord_user_id} 已创建角色")

        character = self._character_repository.add(
            Character(
                player_id=player.id,
                name=character_name,
                title=title,
                total_power_score=0,
                public_power_score=0,
                hidden_pvp_score=0,
            )
        )

        launch_rule = self._progression.get_launch_rule()
        self._character_repository.save_progress(
            CharacterProgress(
                character_id=character.id,
                realm_id=launch_rule.realm_id,
                stage_id=launch_rule.stage_thresholds[0].stage_id,
                cultivation_value=0,
                comprehension_value=0,
                breakthrough_qualification_obtained=False,
                highest_endless_floor=0,
                current_hp_ratio=_DECIMAL_FULL_RATIO,
                current_mp_ratio=_DECIMAL_FULL_RATIO,
            )
        )
        self._character_repository.save_currency_balance(
            CurrencyBalance(
                character_id=character.id,
                spirit_stone=0,
                honor_coin=0,
            )
        )

        self._refresh_score_if_configured(character.id)
        aggregate = self._require_aggregate(character.id)
        return self._build_snapshot(aggregate)

    def get_snapshot(self, *, character_id: int) -> CharacterGrowthSnapshot:
        """读取角色当前成长快照。"""
        aggregate = self._require_aggregate(character_id)
        return self._build_snapshot(aggregate)

    def add_cultivation(self, *, character_id: int, amount: int) -> CultivationUpdateResult:
        """增加修为，并在当前大境界内更新小阶段状态。"""
        normalized_amount = self._validate_positive_amount(amount=amount, label="修为")
        aggregate = self._require_aggregate(character_id)
        progress = aggregate.progress
        assert progress is not None

        rule = self._progression.get_realm_rule(progress.realm_id)
        previous_stage_id = progress.stage_id
        capped_value = min(rule.total_cultivation, progress.cultivation_value + normalized_amount)
        applied_amount = capped_value - progress.cultivation_value

        progress.cultivation_value = capped_value
        progress.stage_id = self._progression.resolve_stage(progress.realm_id, progress.cultivation_value).stage_id
        self._character_repository.save_progress(progress)
        self._refresh_score_if_configured(character_id)

        return CultivationUpdateResult(
            snapshot=self._build_snapshot(aggregate),
            requested_amount=normalized_amount,
            applied_amount=applied_amount,
            previous_stage_id=previous_stage_id,
            stage_changed=previous_stage_id != progress.stage_id,
        )

    def add_comprehension(self, *, character_id: int, amount: int) -> CharacterGrowthSnapshot:
        """增加感悟，不触发大境界突破逻辑。"""
        normalized_amount = self._validate_positive_amount(amount=amount, label="感悟")
        aggregate = self._require_aggregate(character_id)
        progress = aggregate.progress
        assert progress is not None

        progress.comprehension_value += normalized_amount
        self._character_repository.save_progress(progress)
        self._refresh_score_if_configured(character_id)
        return self._build_snapshot(aggregate)

    def _require_aggregate(self, character_id: int) -> CharacterAggregate:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise CharacterNotFoundError(f"角色不存在：{character_id}")
        if aggregate.progress is None:
            raise CharacterGrowthStateError(f"角色缺少成长状态：{character_id}")
        if aggregate.currency_balance is None:
            raise CharacterGrowthStateError(f"角色缺少货币余额：{character_id}")
        return aggregate

    def _build_snapshot(self, aggregate: CharacterAggregate) -> CharacterGrowthSnapshot:
        progress = aggregate.progress
        balance = aggregate.currency_balance
        if progress is None or balance is None:
            raise CharacterGrowthStateError(f"角色聚合状态不完整：{aggregate.character.id}")

        rule = self._progression.get_realm_rule(progress.realm_id)
        current_stage = self._find_stage_threshold(rule=rule, stage_id=progress.stage_id)
        next_stage = self._find_next_stage(rule=rule, current_stage_order=current_stage.order)

        return CharacterGrowthSnapshot(
            player_id=aggregate.player.id,
            discord_user_id=aggregate.player.discord_user_id,
            player_display_name=aggregate.player.display_name,
            character_id=aggregate.character.id,
            character_name=aggregate.character.name,
            character_title=aggregate.character.title,
            realm_id=progress.realm_id,
            stage_id=progress.stage_id,
            cultivation_value=progress.cultivation_value,
            comprehension_value=progress.comprehension_value,
            spirit_stone=balance.spirit_stone,
            honor_coin=balance.honor_coin,
            realm_total_cultivation=rule.total_cultivation,
            current_stage_entry_cultivation=current_stage.entry_cultivation,
            next_stage_id=None if next_stage is None else next_stage.stage_id,
            next_stage_entry_cultivation=None if next_stage is None else next_stage.entry_cultivation,
            stage_thresholds=rule.stage_thresholds,
        )

    @staticmethod
    def _validate_positive_amount(*, amount: int, label: str) -> int:
        if amount <= 0:
            raise InvalidGrowthAmountError(f"{label}增量必须大于 0")
        return amount

    @staticmethod
    def _find_stage_threshold(*, rule: RealmGrowthRule, stage_id: str) -> StageThreshold:
        for threshold in rule.stage_thresholds:
            if threshold.stage_id == stage_id:
                return threshold
        raise CharacterGrowthStateError(f"未找到小阶段门槛：{rule.realm_id}:{stage_id}")

    @staticmethod
    def _find_next_stage(*, rule: RealmGrowthRule, current_stage_order: int) -> StageThreshold | None:
        for threshold in rule.stage_thresholds:
            if threshold.order > current_stage_order:
                return threshold
        return None

    def _refresh_score_if_configured(self, character_id: int) -> None:
        if self._score_service is None:
            return
        self._score_service.refresh_character_score(character_id=character_id)


__all__ = [
    "CharacterAlreadyExistsError",
    "CharacterGrowthService",
    "CharacterGrowthServiceError",
    "CharacterGrowthSnapshot",
    "CharacterGrowthStateError",
    "CharacterNotFoundError",
    "CultivationUpdateResult",
    "InvalidGrowthAmountError",
]
