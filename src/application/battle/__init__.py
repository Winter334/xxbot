"""自动战斗应用服务。"""

from application.battle.auto_battle_service import (
    AutoBattleCharacterStateError,
    AutoBattleExecutionResult,
    AutoBattlePersistenceMapping,
    AutoBattleProgressWriteback,
    AutoBattleReportRecordPayload,
    AutoBattleRequest,
    AutoBattleService,
    AutoBattleServiceError,
)
from application.battle.battle_replay_service import (
    BattleReplayDisplayContext,
    BattleReplayFrame,
    BattleReplayPresentation,
    BattleReplayService,
)

__all__ = [
    "AutoBattleCharacterStateError",
    "AutoBattleExecutionResult",
    "AutoBattlePersistenceMapping",
    "AutoBattleProgressWriteback",
    "AutoBattleReportRecordPayload",
    "AutoBattleRequest",
    "AutoBattleService",
    "AutoBattleServiceError",
    "BattleReplayDisplayContext",
    "BattleReplayFrame",
    "BattleReplayPresentation",
    "BattleReplayService",
]
