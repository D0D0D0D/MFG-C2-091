"""AgentCore Platform v1.0"""

# /ingest — validate_records (docs/02_design.md §(b) /ingest が前提とする
# 最小データ要件). 必須フィールド（asset_id, inspected_at, 少なくとも1つの
# findings または free_note）を検証する。決定論・LLM不使用。不足行は書き
#込まずエラーリストへ回す（データ品質検証）。

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_REQUIRED_FIELDS = ("asset_id", "inspected_at")


class ValidateRecordsNode(FunctionNode):
    """必須フィールド充足チェック。不足行はエラーリストへ回し、書込対象から除外する。"""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        records: list[dict[str, Any]] = state.get("normalized_records", []) or []

        valid_records: list[dict[str, Any]] = []
        validation_errors: list[dict[str, Any]] = []

        for idx, rec in enumerate(records):
            errors = []
            for field in _REQUIRED_FIELDS:
                if not rec.get(field):
                    errors.append(f"missing_{field}")
            if not rec.get("findings") and not rec.get("free_note"):
                errors.append("missing_findings_or_free_note")

            if errors:
                validation_errors.append({"row_index": idx, "reasons": errors})
            else:
                valid_records.append(rec)

        emit_trace_event(
            "ingest_validation_completed",
            {"valid_count": len(valid_records), "error_count": len(validation_errors)},
            state,
        )

        return {
            "valid_records": valid_records,
            "validation_errors": validation_errors,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
