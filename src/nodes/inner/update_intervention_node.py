"""AgentCore Platform v1.0"""

# /feedback — update_intervention (docs/02_design.md §/ingest §/feedback
# の内部ノード構成). outcome_narrative / post_condition / action / status
# を確定 UPDATE または新規 INSERT する。構造化入力を前提とするが、自由記述
# メモがある場合は /ingest と同じ structure_with_llm 相当ステップを再利用
# する（LLM 使用は自由記述構造化のみに限定 — 経年解釈は含まない）。

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.llm_text import complete_text

from src.services.asset_store import BaseAssetStore


class UpdateInterventionNode(FunctionNode):
    """intervention の確定フィールドを UPDATE/INSERT する。"""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, asset_store: BaseAssetStore, llm: Any | None = None) -> None:
        super().__init__()
        self._store = asset_store
        self._llm = llm

    def execute(self, state: dict) -> dict:
        org_id = state.get("org_id", "")
        asset_id = state.get("asset_id", "")
        intervention_id = state.get("resolved_intervention_id", "")
        payload = state.get("payload", {}) or {}

        outcome_narrative = payload.get("outcome_narrative", "")
        free_note = payload.get("free_note", "")
        if not outcome_narrative and free_note and self._llm is None:
            # LLM 不在時は無加工の free_note をそのまま採用する（縮退 — 構造化
            # なしでも記録そのものは欠落させない）。
            outcome_narrative = free_note
        elif not outcome_narrative and free_note and self._llm is not None:
            try:
                outcome_narrative = self._structure_free_note(free_note)
            except Exception as exc:  # noqa: BLE001 — LLM failure degrades, does not crash
                emit_trace_event("feedback_structure_llm_failed", {"error": str(exc)}, state)
                outcome_narrative = free_note

        fields: dict[str, Any] = {
            "asset_id": asset_id,
            "action": payload.get("action"),
            "post_condition": payload.get("post_condition"),
            "outcome_narrative": outcome_narrative,
            "triggering_metrics": payload.get("triggering_metrics", []),
            "confirmed_by": payload.get("confirmed_by", ""),
        }
        fields = {k: v for k, v in fields.items() if v not in (None, "", [])}

        self._store.update_intervention(org_id, intervention_id, fields)

        emit_trace_event(
            "feedback_intervention_updated",
            {"org_id": org_id, "intervention_id": intervention_id},
            state,
        )

        return {
            "updated_intervention_fields": fields,
            "status": AgentStatus.SUCCESS.value,
        }

    def _structure_free_note(self, free_note: str) -> str:
        prompt = (
            "Structure the following free-text intervention outcome note into a concise "
            "factual narrative. Do NOT add aging-trend interpretation or a priority — "
            "that is out of scope for this step.\n\n"
            f"Note: {free_note}"
        )
        return complete_text(self._llm, prompt)

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
