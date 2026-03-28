"""PVP 荣誉币应用服务。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from application.character.growth_service import CharacterGrowthStateError, CharacterNotFoundError
from domain.pvp import (
    PvpBattleOutcome,
    PvpHonorCoinSettlement,
    PvpLeaderboardEntry,
    PvpRewardDisplayItem,
    PvpRewardPreview,
    PvpRewardState,
    PvpRuleService,
)
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import CurrencyBalance, HonorCoinLedger
from infrastructure.db.repositories import CharacterAggregate, CharacterRepository, HonorCoinLedgerRepository

_HONOR_COIN_LEDGER_SCHEMA_VERSION = "stage9.honor_coin.v1"


@dataclass(frozen=True, slots=True)
class HonorCoinBalanceSnapshot:
    """荣誉币余额只读快照。"""

    character_id: int
    honor_coin: int


@dataclass(frozen=True, slots=True)
class HonorCoinApplicationResult:
    """单次荣誉币入账后的应用层结果。"""

    character_id: int
    ledger_id: int
    ledger_created_at: datetime
    settlement: PvpHonorCoinSettlement
    detail: dict[str, object]


class HonorCoinServiceError(RuntimeError):
    """荣誉币服务基础异常。"""


class HonorCoinStateError(HonorCoinServiceError):
    """荣誉币上下文状态不完整。"""


class HonorCoinService:
    """负责荣誉币余额查询、奖励预览与单次结算入账。"""

    def __init__(
        self,
        *,
        character_repository: CharacterRepository,
        honor_coin_ledger_repository: HonorCoinLedgerRepository,
        static_config: StaticGameConfig | None = None,
        rule_service: PvpRuleService | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._honor_coin_ledger_repository = honor_coin_ledger_repository
        self._static_config = static_config or get_static_config()
        self._rule_service = rule_service or PvpRuleService(self._static_config)

    def get_balance(self, *, character_id: int) -> HonorCoinBalanceSnapshot:
        """读取角色当前荣誉币余额。"""
        balance = self._require_currency_balance(character_id)
        return HonorCoinBalanceSnapshot(
            character_id=character_id,
            honor_coin=max(0, int(balance.honor_coin)),
        )

    def preview_rank_rewards(
        self,
        *,
        rank_position: int,
        honor_coin_on_win: int = 0,
        honor_coin_on_loss: int = 0,
    ) -> PvpRewardPreview:
        """按指定名次生成展示奖励预览。"""
        return self._rule_service.build_reward_preview(
            rank_position=rank_position,
            honor_coin_on_win=max(0, honor_coin_on_win),
            honor_coin_on_loss=max(0, honor_coin_on_loss),
            reward_state=PvpRewardState.PREVIEW,
        )

    def preview_challenge_rewards(
        self,
        *,
        attacker: PvpLeaderboardEntry,
        defender: PvpLeaderboardEntry,
        attacker_current_win_streak: int,
        rank_position_on_win: int | None = None,
    ) -> PvpRewardPreview:
        """按单个目标预估挑战奖励预览。"""
        on_win = self._rule_service.calculate_honor_coin_settlement(
            attacker=attacker,
            defender=defender,
            battle_outcome=PvpBattleOutcome.ALLY_VICTORY,
            attacker_current_win_streak=attacker_current_win_streak,
            balance_before=None,
        ).delta
        on_loss = self._rule_service.calculate_honor_coin_settlement(
            attacker=attacker,
            defender=defender,
            battle_outcome=PvpBattleOutcome.ENEMY_VICTORY,
            attacker_current_win_streak=attacker_current_win_streak,
            balance_before=None,
        ).delta
        return self.preview_rank_rewards(
            rank_position=rank_position_on_win or attacker.rank_position,
            honor_coin_on_win=on_win,
            honor_coin_on_loss=on_loss,
        )

    def apply_settlement(
        self,
        *,
        character_id: int,
        source_type: str,
        source_ref: str | None,
        settlement: PvpHonorCoinSettlement,
        occurred_at: datetime | None = None,
        detail_extension: dict[str, object] | None = None,
    ) -> HonorCoinApplicationResult:
        """把单次荣誉币结算写入余额与流水。"""
        if not source_type or not source_type.strip():
            raise ValueError("source_type 不能为空")
        current_time = occurred_at or datetime.utcnow()
        balance_model = self._require_currency_balance(character_id)
        balance_before = max(0, int(balance_model.honor_coin))
        balance_after = balance_before + settlement.delta
        persisted_settlement = replace(
            settlement,
            balance_before=balance_before,
            balance_after=balance_after,
        )
        balance_model.honor_coin = balance_after
        self._character_repository.save_currency_balance(balance_model)
        detail_payload = self._build_detail_payload(
            settlement=persisted_settlement,
            source_type=source_type,
            source_ref=source_ref,
            detail_extension=detail_extension,
        )
        ledger = self._honor_coin_ledger_repository.add_ledger(
            HonorCoinLedger(
                character_id=character_id,
                source_type=source_type,
                source_ref=source_ref,
                delta=persisted_settlement.delta,
                balance_after=balance_after,
                detail_json=detail_payload,
                created_at=current_time,
            )
        )
        return HonorCoinApplicationResult(
            character_id=character_id,
            ledger_id=ledger.id,
            ledger_created_at=ledger.created_at,
            settlement=persisted_settlement,
            detail=detail_payload,
        )

    def _build_detail_payload(
        self,
        *,
        settlement: PvpHonorCoinSettlement,
        source_type: str,
        source_ref: str | None,
        detail_extension: dict[str, object] | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": _HONOR_COIN_LEDGER_SCHEMA_VERSION,
            "source_type": source_type,
            "source_ref": source_ref,
            "battle_outcome": settlement.battle_outcome.value,
            "rank_gap": settlement.rank_gap,
            "delta": settlement.delta,
            "balance_before": settlement.balance_before,
            "balance_after": settlement.balance_after,
            "components": [self._serialize_component(component) for component in settlement.components],
            "reward_preview": None
            if settlement.reward_preview is None
            else self._serialize_reward_preview(settlement.reward_preview),
        }
        if detail_extension:
            payload.update(detail_extension)
        return payload

    @staticmethod
    def _serialize_component(component) -> dict[str, object]:
        return {
            "component_id": component.component_id,
            "configured_delta": component.configured_delta,
            "applied_delta": component.applied_delta,
            "summary": component.summary,
            "triggered": component.triggered,
        }

    @staticmethod
    def _serialize_reward_preview(preview: PvpRewardPreview) -> dict[str, object]:
        return {
            "reward_tier_id": preview.reward_tier_id,
            "rank_range": {
                "rank_start": preview.rank_range.rank_start,
                "rank_end": preview.rank_range.rank_end,
            },
            "honor_coin_on_win": preview.honor_coin_on_win,
            "honor_coin_on_loss": preview.honor_coin_on_loss,
            "summary": preview.summary,
            "display_items": [HonorCoinService._serialize_reward_item(item) for item in preview.display_items],
        }

    @staticmethod
    def _serialize_reward_item(item: PvpRewardDisplayItem) -> dict[str, object]:
        return {
            "reward_id": item.reward_id,
            "reward_type": item.reward_type.value,
            "name": item.name,
            "rarity": item.rarity,
            "state": item.state.value,
            "source": item.source.value,
            "meta": dict(item.meta),
        }

    def _require_currency_balance(self, character_id: int) -> CurrencyBalance:
        aggregate = self._character_repository.get_aggregate(character_id)
        if aggregate is None:
            raise CharacterNotFoundError(f"角色不存在：{character_id}")
        if aggregate.currency_balance is None:
            raise CharacterGrowthStateError(f"角色缺少货币余额：{character_id}")
        return aggregate.currency_balance


__all__ = [
    "HonorCoinApplicationResult",
    "HonorCoinBalanceSnapshot",
    "HonorCoinService",
    "HonorCoinServiceError",
    "HonorCoinStateError",
]
