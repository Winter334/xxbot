"""结算后实例批处理命名服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Protocol
from urllib import error, request as urllib_request

from application.character.skill_runtime_support import SkillRuntimeSupport
from application.equipment.equipment_service import EquipmentService, EquipmentServiceError
from infrastructure.config.settings import Settings
from infrastructure.config.static import StaticGameConfig, get_static_config
from infrastructure.db.models import ItemNamingBatch
from infrastructure.db.repositories import SkillRepository, StateRepository

_TARGET_EQUIPMENT = "equipment"
_TARGET_ARTIFACT = "artifact"
_TARGET_SKILL = "skill"
_ENDLESS_SOURCE_TYPE = "endless_settlement"
_STATUS_PENDING = "pending"
_STATUS_PROCESSING = "processing"
_STATUS_COMPLETED = "completed"
_STATUS_SKIPPED = "skipped"
_EQUIPMENT_ENTRY_TYPE = "equipment_drop"
_ARTIFACT_ENTRY_TYPE = "artifact_drop"
_SKILL_ENTRY_TYPE = "skill_drop"
_DEFAULT_SKIPPED_REASON = "provider_unavailable"
_AI_NAMING_SOURCE = "ai_batch"
_DEFAULT_HTTP_TIMEOUT_SECONDS = 20


@dataclass(frozen=True, slots=True)
class ItemNamingCandidate:
    """单个实例的批处理命名候选。"""

    target_type: str
    instance_id: int
    fallback_name: str
    prompt_context: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ItemNamingBatchRequest:
    """一次批处理命名请求。"""

    batch_id: int
    character_id: int
    source_type: str
    source_ref: str
    candidates: tuple[ItemNamingCandidate, ...]


@dataclass(frozen=True, slots=True)
class ItemNamingBatchResult:
    """单个实例的命名返回结果。"""

    target_type: str
    instance_id: int
    generated_name: str | None = None
    error_message: str | None = None


class ItemNamingProvider(Protocol):
    """批处理 AI 命名提供方。"""

    provider_name: str

    def generate_names(self, *, request: ItemNamingBatchRequest) -> tuple[ItemNamingBatchResult, ...]:
        """按单批次输入返回实例命名结果。"""


class ItemNamingBatchServiceError(RuntimeError):
    """批处理命名服务基础异常。"""


class ItemNamingBatchNotFoundError(ItemNamingBatchServiceError):
    """命名批次不存在。"""


class ItemNamingBatchService:
    """负责创建、处理并回填高价值实例的批处理命名。"""

    def __init__(
        self,
        *,
        state_repository: StateRepository,
        equipment_service: EquipmentService,
        skill_repository: SkillRepository,
        skill_runtime_support: SkillRuntimeSupport,
        provider: ItemNamingProvider | None = None,
        static_config: StaticGameConfig | None = None,
    ) -> None:
        self._state_repository = state_repository
        self._equipment_service = equipment_service
        self._skill_repository = skill_repository
        self._skill_runtime_support = skill_runtime_support
        self._provider = provider
        self._static_config = static_config or get_static_config()
        self._equipment_quality_order_by_id = {
            quality.quality_id: quality.order for quality in self._static_config.equipment.qualities
        }
        self._skill_quality_order_by_id = {
            quality.quality_id: quality.order for quality in self._static_config.skill_generation.qualities
        }
        self._minimum_epic_equipment_quality_order = self._require_equipment_quality_order("epic")
        self._minimum_rare_skill_quality_order = self._require_skill_quality_order("rare")

    def create_endless_settlement_batch(
        self,
        *,
        character_id: int,
        source_ref: str,
        final_drop_list: Sequence[Mapping[str, Any]],
    ) -> ItemNamingBatch | None:
        """为一次无尽最终结算中的高价值保留掉落创建命名批次。"""
        existing = self._state_repository.get_item_naming_batch_by_source(
            character_id=character_id,
            source_type=_ENDLESS_SOURCE_TYPE,
            source_ref=source_ref,
        )
        if existing is not None:
            return existing
        candidates = self._collect_endless_candidates(final_drop_list=final_drop_list)
        if not candidates:
            return None
        processed_at = None
        result_payload: dict[str, Any] = {}
        provider_name = None if self._provider is None else self._provider.provider_name
        status = _STATUS_PENDING
        if self._provider is None:
            status = _STATUS_SKIPPED
            processed_at = datetime.utcnow()
            result_payload = {
                "renamed": [],
                "failed": [],
                "skipped_reason": _DEFAULT_SKIPPED_REASON,
                "candidate_count": len(candidates),
            }
        batch = ItemNamingBatch(
            character_id=character_id,
            source_type=_ENDLESS_SOURCE_TYPE,
            source_ref=source_ref,
            status=status,
            provider_name=provider_name,
            request_payload_json=[self._candidate_to_payload(candidate) for candidate in candidates],
            result_payload_json=result_payload,
            error_message=None,
            processed_at=processed_at,
        )
        return self._state_repository.save_item_naming_batch(batch)

    def process_pending_batches(self, *, limit: int = 20) -> tuple[ItemNamingBatch, ...]:
        """按状态顺序处理待执行命名批次。"""
        batches = self._state_repository.list_item_naming_batches_by_status(_STATUS_PENDING, limit=limit)
        return tuple(self.process_batch(batch_id=batch.id) for batch in batches)

    def process_batch(self, *, batch_id: int) -> ItemNamingBatch:
        """处理单个命名批次，并将结果回填到真实实例。"""
        batch = self._state_repository.get_item_naming_batch(batch_id)
        if batch is None:
            raise ItemNamingBatchNotFoundError(f"命名批次不存在：{batch_id}")
        if batch.status in {_STATUS_COMPLETED, _STATUS_SKIPPED}:
            return batch
        if self._provider is None:
            return self._mark_batch_skipped(batch=batch, reason=_DEFAULT_SKIPPED_REASON)
        candidates = tuple(self._payload_to_candidate(payload) for payload in self._normalize_payload_list(batch.request_payload_json))
        if not candidates:
            return self._mark_batch_skipped(batch=batch, reason="empty_candidates")
        batch.status = _STATUS_PROCESSING
        batch.provider_name = self._provider.provider_name
        batch.error_message = None
        self._state_repository.save_item_naming_batch(batch)
        request_payload = ItemNamingBatchRequest(
            batch_id=batch.id,
            character_id=batch.character_id,
            source_type=batch.source_type,
            source_ref=batch.source_ref,
            candidates=candidates,
        )
        renamed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        provider_error: str | None = None
        try:
            results = self._provider.generate_names(request=request_payload)
        except Exception as exc:  # noqa: BLE001
            results = ()
            provider_error = str(exc)
        result_by_target = {
            (result.target_type, result.instance_id): result
            for result in results
            if result.instance_id > 0 and result.target_type in {_TARGET_EQUIPMENT, _TARGET_ARTIFACT, _TARGET_SKILL}
        }
        for candidate in candidates:
            result = result_by_target.get((candidate.target_type, candidate.instance_id))
            generated_name = "" if result is None else str(result.generated_name or "").strip()
            failure_reason = provider_error if provider_error is not None else None if result is None else result.error_message
            if not generated_name:
                failed.append(
                    {
                        "target_type": candidate.target_type,
                        "instance_id": candidate.instance_id,
                        "fallback_name": candidate.fallback_name,
                        "reason": failure_reason or "missing_generated_name",
                    }
                )
                continue
            try:
                self._apply_generated_name(
                    character_id=batch.character_id,
                    batch_id=batch.id,
                    source_ref=batch.source_ref,
                    candidate=candidate,
                    generated_name=generated_name,
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    {
                        "target_type": candidate.target_type,
                        "instance_id": candidate.instance_id,
                        "fallback_name": candidate.fallback_name,
                        "reason": str(exc),
                    }
                )
                continue
            renamed.append(
                {
                    "target_type": candidate.target_type,
                    "instance_id": candidate.instance_id,
                    "fallback_name": candidate.fallback_name,
                    "generated_name": generated_name,
                }
            )
        batch.status = _STATUS_COMPLETED
        batch.processed_at = datetime.utcnow()
        batch.error_message = provider_error
        batch.result_payload_json = {
            "renamed": renamed,
            "failed": failed,
            "provider_name": self._provider.provider_name,
        }
        return self._state_repository.save_item_naming_batch(batch)

    def refresh_drop_entries(
        self,
        *,
        character_id: int,
        entries: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], ...]:
        """按实例当前名称刷新掉落展示条目。"""
        refreshed_entries: list[dict[str, Any]] = []
        for entry in entries:
            refreshed = dict(entry)
            entry_type = str(refreshed.get("entry_type") or "")
            if entry_type in {_EQUIPMENT_ENTRY_TYPE, _ARTIFACT_ENTRY_TYPE}:
                item_id = _read_int(refreshed.get("item_id"))
                if item_id > 0:
                    try:
                        item = self._equipment_service.get_equipment_detail(
                            character_id=character_id,
                            equipment_item_id=item_id,
                        )
                    except EquipmentServiceError:
                        item = None
                    if item is not None:
                        refreshed["display_name"] = item.display_name
                        refreshed["quality_id"] = item.quality_id
                        refreshed["quality_name"] = item.quality_name
                        refreshed["rank_id"] = item.rank_id
                        refreshed["rank_name"] = item.rank_name
                        refreshed["slot_id"] = item.slot_id
                        refreshed["slot_name"] = item.slot_name
                        refreshed["resonance_name"] = item.resonance_name
            elif entry_type == _SKILL_ENTRY_TYPE:
                item_id = _read_int(refreshed.get("item_id"))
                skill_item = None if item_id <= 0 else self._skill_repository.get_skill_item_by_character_and_id(character_id, item_id)
                if skill_item is not None:
                    refreshed["skill_name"] = skill_item.skill_name
                    refreshed["quality_id"] = skill_item.quality_id
                    refreshed["quality_name"] = skill_item.quality_name
                    refreshed["rank_id"] = skill_item.rank_id
                    refreshed["rank_name"] = skill_item.rank_name
                    refreshed["lineage_id"] = skill_item.lineage_id
            refreshed_entries.append(refreshed)
        return tuple(refreshed_entries)

    def _mark_batch_skipped(self, *, batch: ItemNamingBatch, reason: str) -> ItemNamingBatch:
        batch.status = _STATUS_SKIPPED
        batch.processed_at = datetime.utcnow()
        batch.error_message = None
        batch.result_payload_json = {
            "renamed": [],
            "failed": [],
            "skipped_reason": reason,
        }
        return self._state_repository.save_item_naming_batch(batch)

    def _collect_endless_candidates(
        self,
        *,
        final_drop_list: Sequence[Mapping[str, Any]],
    ) -> tuple[ItemNamingCandidate, ...]:
        candidates: list[ItemNamingCandidate] = []
        for entry in final_drop_list:
            entry_type = str(entry.get("entry_type") or "")
            if entry_type in {_EQUIPMENT_ENTRY_TYPE, _ARTIFACT_ENTRY_TYPE}:
                target_type = _TARGET_ARTIFACT if entry_type == _ARTIFACT_ENTRY_TYPE or bool(entry.get("is_artifact")) else _TARGET_EQUIPMENT
                if target_type == _TARGET_EQUIPMENT and not self._is_high_value_equipment(entry=entry):
                    continue
                instance_id = _read_int(entry.get("item_id"))
                fallback_name = str(entry.get("display_name") or entry.get("template_name") or "").strip()
                if instance_id <= 0 or not fallback_name:
                    continue
                candidates.append(
                    ItemNamingCandidate(
                        target_type=target_type,
                        instance_id=instance_id,
                        fallback_name=fallback_name,
                        prompt_context={
                            "entry_type": entry_type,
                            "template_name": str(entry.get("template_name") or "").strip(),
                            "slot_name": str(entry.get("slot_name") or "").strip(),
                            "quality_name": str(entry.get("quality_name") or "").strip(),
                            "rank_name": str(entry.get("rank_name") or "").strip(),
                            "resonance_name": str(entry.get("resonance_name") or "").strip(),
                            "source_floor": _read_int(entry.get("source_floor")),
                            "source_score": _read_int(entry.get("source_score")),
                        },
                    )
                )
                continue
            if entry_type != _SKILL_ENTRY_TYPE or not self._is_high_value_skill(entry=entry):
                continue
            instance_id = _read_int(entry.get("item_id"))
            fallback_name = str(entry.get("skill_name") or "").strip()
            if instance_id <= 0 or not fallback_name:
                continue
            candidates.append(
                ItemNamingCandidate(
                    target_type=_TARGET_SKILL,
                    instance_id=instance_id,
                    fallback_name=fallback_name,
                    prompt_context={
                        "entry_type": entry_type,
                        "lineage_id": str(entry.get("lineage_id") or "").strip(),
                        "path_id": str(entry.get("path_id") or "").strip(),
                        "axis_id": str(entry.get("axis_id") or "").strip(),
                        "quality_name": str(entry.get("quality_name") or "").strip(),
                        "rank_name": str(entry.get("rank_name") or "").strip(),
                        "skill_type": str(entry.get("skill_type") or "").strip(),
                        "auxiliary_slot_id": str(entry.get("auxiliary_slot_id") or "").strip(),
                    },
                )
            )
        return tuple(candidates)

    def _apply_generated_name(
        self,
        *,
        character_id: int,
        batch_id: int,
        source_ref: str,
        candidate: ItemNamingCandidate,
        generated_name: str,
    ) -> None:
        metadata = {
            "batch_id": str(batch_id),
            "provider_name": "" if self._provider is None else self._provider.provider_name,
            "source_ref": source_ref,
            "fallback_name": candidate.fallback_name,
            "target_type": candidate.target_type,
        }
        for key, value in candidate.prompt_context.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            metadata[normalized_key] = "" if value is None else str(value)
        if candidate.target_type in {_TARGET_EQUIPMENT, _TARGET_ARTIFACT}:
            self._equipment_service.apply_custom_name(
                character_id=character_id,
                equipment_item_id=candidate.instance_id,
                resolved_name=generated_name,
                naming_template_id=self._provider.provider_name,
                naming_source=_AI_NAMING_SOURCE,
                naming_metadata=metadata,
            )
            return
        if candidate.target_type == _TARGET_SKILL:
            self._skill_runtime_support.apply_custom_name(
                character_id=character_id,
                skill_item_id=candidate.instance_id,
                resolved_name=generated_name,
                naming_source=_AI_NAMING_SOURCE,
                naming_metadata=metadata,
            )
            return
        raise ItemNamingBatchServiceError(f"不支持的命名目标类型：{candidate.target_type}")

    def _is_high_value_equipment(self, *, entry: Mapping[str, Any]) -> bool:
        quality_id = str(entry.get("quality_id") or "").strip()
        return self._equipment_quality_order_by_id.get(quality_id, 0) >= self._minimum_epic_equipment_quality_order

    def _is_high_value_skill(self, *, entry: Mapping[str, Any]) -> bool:
        quality_id = str(entry.get("quality_id") or "").strip()
        return self._skill_quality_order_by_id.get(quality_id, 0) >= self._minimum_rare_skill_quality_order

    def _require_equipment_quality_order(self, quality_id: str) -> int:
        if quality_id not in self._equipment_quality_order_by_id:
            raise ItemNamingBatchServiceError(f"装备品质未配置：{quality_id}")
        return self._equipment_quality_order_by_id[quality_id]

    def _require_skill_quality_order(self, quality_id: str) -> int:
        if quality_id not in self._skill_quality_order_by_id:
            raise ItemNamingBatchServiceError(f"功法品质未配置：{quality_id}")
        return self._skill_quality_order_by_id[quality_id]

    @staticmethod
    def _candidate_to_payload(candidate: ItemNamingCandidate) -> dict[str, Any]:
        return {
            "target_type": candidate.target_type,
            "instance_id": candidate.instance_id,
            "fallback_name": candidate.fallback_name,
            "prompt_context": dict(candidate.prompt_context),
        }

    @staticmethod
    def _payload_to_candidate(payload: Mapping[str, Any]) -> ItemNamingCandidate:
        prompt_context = payload.get("prompt_context")
        normalized_prompt_context = dict(prompt_context) if isinstance(prompt_context, Mapping) else {}
        return ItemNamingCandidate(
            target_type=str(payload.get("target_type") or "").strip(),
            instance_id=_read_int(payload.get("instance_id")),
            fallback_name=str(payload.get("fallback_name") or "").strip(),
            prompt_context=normalized_prompt_context,
        )

    @staticmethod
    def _normalize_payload_list(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, list):
            return []
        return [payload for payload in value if isinstance(payload, Mapping)]


class EchoItemNamingProvider:
    """用于测试或本地验证的简单批处理命名提供方。"""

    provider_name = "echo_provider"

    def generate_names(self, *, request: ItemNamingBatchRequest) -> tuple[ItemNamingBatchResult, ...]:
        results: list[ItemNamingBatchResult] = []
        for candidate in request.candidates:
            prefix = "法宝" if candidate.target_type == _TARGET_ARTIFACT else "功法" if candidate.target_type == _TARGET_SKILL else "装备"
            results.append(
                ItemNamingBatchResult(
                    target_type=candidate.target_type,
                    instance_id=candidate.instance_id,
                    generated_name=f"{prefix}·{candidate.fallback_name}",
                )
            )
        return tuple(results)


class HttpAiItemNamingProvider:
    """基于 HTTP JSON 接口的最小 AI 命名提供方。"""

    provider_name = "http_ai_provider"

    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        model: str,
        timeout_seconds: int = _DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = api_key.strip()
        self._api_url = api_url.strip()
        self._model = model.strip()
        self._timeout_seconds = max(1, timeout_seconds)
        if not self._api_key or not self._api_url or not self._model:
            raise ValueError("AI 命名提供方配置不完整")

    @classmethod
    def from_settings(cls, settings: Settings) -> "HttpAiItemNamingProvider | None":
        """根据环境配置构造 HTTP AI 命名提供方。"""
        if not settings.ai_naming_enabled:
            return None
        return cls(
            api_key=settings.ai_naming_api_key or "",
            api_url=settings.ai_naming_api_url or "",
            model=settings.ai_naming_model or "",
        )

    def generate_names(self, *, request: ItemNamingBatchRequest) -> tuple[ItemNamingBatchResult, ...]:
        payload = {
            "model": self._model,
            "batch_id": request.batch_id,
            "character_id": request.character_id,
            "source_type": request.source_type,
            "source_ref": request.source_ref,
            "prompt": self._build_prompt(request),
            "items": [
                {
                    "target_type": candidate.target_type,
                    "instance_id": candidate.instance_id,
                    "fallback_name": candidate.fallback_name,
                    "prompt_context": dict(candidate.prompt_context),
                }
                for candidate in request.candidates
            ],
        }
        request_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }
        http_request = urllib_request.Request(
            self._api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        try:
            with urllib_request.urlopen(http_request, timeout=self._timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise ItemNamingBatchServiceError(f"AI 命名 HTTP 错误：{exc.code} {detail}".strip()) from exc
        except error.URLError as exc:
            raise ItemNamingBatchServiceError(f"AI 命名请求失败：{exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise ItemNamingBatchServiceError("AI 命名返回了无效 JSON") from exc
        return self._parse_response_payload(response_payload)

    @staticmethod
    def _build_prompt(request_payload: ItemNamingBatchRequest) -> str:
        item_lines = []
        for candidate in request_payload.candidates:
            context_parts = [
                f"target_type={candidate.target_type}",
                f"instance_id={candidate.instance_id}",
                f"fallback_name={candidate.fallback_name}",
            ]
            for key, value in sorted(candidate.prompt_context.items(), key=lambda item: item[0]):
                context_parts.append(f"{key}={value}")
            item_lines.append("; ".join(context_parts))
        joined_items = "\n".join(f"- {line}" for line in item_lines)
        return (
            "你是修仙题材文字游戏的物品命名助手。"
            "请基于 fallback_name 与上下文，为每个实例生成更有风味但仍可读的中文名字。"
            "要求：1. 保持 target_type 与 instance_id 原样对应；"
            "2. 名字长度尽量控制在 2 到 12 个中文字符；"
            "3. 若把握不足，可返回空 generated_name 并填写 error_message；"
            "4. 不要返回多余解释；"
            "5. 响应必须是 JSON 对象，结构为 {'results': [...]}，其中每项包含 target_type、instance_id、generated_name、error_message。"
            f"\n本批次来源：{request_payload.source_type}/{request_payload.source_ref}。"
            f"\n待命名实例：\n{joined_items}"
        )

    def _parse_response_payload(self, payload: Any) -> tuple[ItemNamingBatchResult, ...]:
        if not isinstance(payload, Mapping):
            raise ItemNamingBatchServiceError("AI 命名返回结构错误：缺少顶层对象")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ItemNamingBatchServiceError("AI 命名返回结构错误：缺少 results 列表")
        results: list[ItemNamingBatchResult] = []
        for item in raw_results:
            if not isinstance(item, Mapping):
                continue
            results.append(
                ItemNamingBatchResult(
                    target_type=str(item.get("target_type") or "").strip(),
                    instance_id=_read_int(item.get("instance_id")),
                    generated_name=None if item.get("generated_name") is None else str(item.get("generated_name")),
                    error_message=None if item.get("error_message") is None else str(item.get("error_message")),
                )
            )
        return tuple(results)


def _read_int(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return default


__all__ = [
    "EchoItemNamingProvider",
    "HttpAiItemNamingProvider",
    "ItemNamingBatchNotFoundError",
    "ItemNamingBatchRequest",
    "ItemNamingBatchResult",
    "ItemNamingBatchService",
    "ItemNamingBatchServiceError",
    "ItemNamingCandidate",
    "ItemNamingProvider",
]
