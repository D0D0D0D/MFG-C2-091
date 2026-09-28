"""AgentCore Platform v1.0"""

# /feedback — reconcile_prediction (docs/02_design.md §/ingest §/feedback
# の内部ノード構成). 直近の /invoke advisory_output（あれば）と実際の補修
# 結果を突合し、進行捕捉の的中/空振りを feedback_result に記録する。
# 決定論の突合ロジック — LLM 不使用。次回 Stage 2 照合の精度向上シグナル
# として扱う。

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.asset_store import BaseAssetStore


class ReconcilePredictionNode(FunctionNode):
    """直近 advisory_output と実際の補修結果を突合する。"""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, asset_store: BaseAssetStore) -> None:
        super().__init__()
        self._store = asset_store

    def execute(self, state: dict) -> dict:
        org_id = state.get("org_id", "")
        asset_id = state.get("asset_id", "")
        intervention_id = state.get("resolved_intervention_id", "")
        updated_fields = state.get("updated_intervention_fields", {}) or {}

        recent_advisory = self._store.get_recent_advisory(org_id, asset_id)

        reconciliation: dict[str, Any]
        if recent_advisory is None:
            reconciliation = {"prediction_available": False, "outcome": "no_prior_advisory"}
        else:
            predicted_priority = recent_advisory.get("priority", "")
            actual_action = updated_fields.get("action", "")
            # 単純な的中判定: 優先度が high/critical の予測で実際に修繕/交換
            # (repair/replace) が行われていれば的中、observe のみで完了して
            # いれば空振り（過剰検知の可能性）とみなす。
            if predicted_priority in ("high", "critical") and actual_action in ("repair", "replace"):
                outcome = "hit"
            elif predicted_priority in ("high", "critical") and actual_action == "observe":
                outcome = "false_positive_candidate"
            else:
                outcome = "inconclusive"
            reconciliation = {
                "prediction_available": True,
                "predicted_priority": predicted_priority,
                "actual_action": actual_action,
                "outcome": outcome,
            }

        feedback_result = {
            "intervention_id": intervention_id,
            "asset_id": asset_id,
            "reconciliation": reconciliation,
        }

        emit_trace_event(
            "feedback_reconciled",
            {"org_id": org_id, "intervention_id": intervention_id, "outcome": reconciliation.get("outcome")},
            state,
        )

        return {
            "feedback_result": feedback_result,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
