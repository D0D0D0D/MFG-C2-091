"""AgentCore Platform v1.0"""

# /ingest — structure_with_llm (docs/02_design.md §/ingest §/feedback の
# 内部ノード構成). 自由記述の点検所見メモ（free_note）のみを対象に、構造化
# フィールド（発見日・部位・欠陥種別・程度等）を抽出する。経年解釈は行わない
# （それは /invoke Stage3 の責務）。LLM 不在時は無加工で透過（構造化強化な
# しの縮退 — status は success のまま）。

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.llm_text import complete_text


class StructureWithLlmNode(FunctionNode):
    """自由記述 free_note のみを対象に構造化フィールドを抽出する（LLM使用）。"""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any | None = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        records: list[dict[str, Any]] = state.get("normalized_records", []) or []
        needs_structuring = [r for r in records if r.get("free_note") and not r.get("findings")]

        if self._llm is None or not needs_structuring:
            emit_trace_event(
                "ingest_structure_skipped",
                {"llm_available": self._llm is not None, "candidate_count": len(needs_structuring)},
                state,
            )
            return {
                "normalized_records": records,
                "status": AgentStatus.SUCCESS.value,
            }

        structured_records: list[dict[str, Any]] = []
        for rec in records:
            if rec in needs_structuring:
                try:
                    extracted = self._extract_structured_findings(rec.get("free_note", ""))
                    rec = {**rec, "findings": extracted}
                except Exception as exc:  # noqa: BLE001 — LLM failure degrades, does not crash
                    emit_trace_event("ingest_structure_llm_failed", {"error": str(exc)}, state)
            structured_records.append(rec)

        emit_trace_event(
            "ingest_structure_completed",
            {"structured_count": len(needs_structuring)},
            state,
        )

        return {
            "normalized_records": structured_records,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extract_structured_findings(self, free_note: str) -> dict[str, Any]:
        """free_note から構造化所見を抽出する（LLM呼び出し）。抽出フォーマット
        は本テンプレートの内部契約であり、経年解釈（優先度判定等）は含まない
        — それは /invoke Stage3 のみの責務（docs/02_design.md）。
        """
        prompt = (
            "Extract structured inspection findings from the following free-text note. "
            "Return only factual descriptive fields (defect type, location, severity as "
            "described by the inspector) — do NOT interpret aging trends or assign a "
            "priority; that is out of scope for this extraction step.\n\n"
            f"Note: {free_note}"
        )
        raw_text = complete_text(self._llm, prompt)
        return {"extracted_text": raw_text}

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
