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

__all__ = [
    "AutoBattleCharacterStateError",
    "AutoBattleExecutionResult",
    "AutoBattlePersistenceMapping",
    "AutoBattleProgressWriteback",
    "AutoBattleReportRecordPayload",
    "AutoBattleRequest",
    "AutoBattleService",
    "AutoBattleServiceError",
]
