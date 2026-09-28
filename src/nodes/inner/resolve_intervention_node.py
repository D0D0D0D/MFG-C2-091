"""AgentCore Platform v1.0"""

# /feedback — resolve_intervention (docs/02_design.md §/ingest §/feedback
# の内部ノード構成). 対象 intervention_id（新規なら採番）を解決する。

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.asset_store import BaseAssetStore


class ResolveInterventionNode(FunctionNode):
    """対象 intervention_id を解決する（新規なら採番）。"""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, asset_store: BaseAssetStore) -> None:
        super().__init__()
        self._store = asset_store

    def execute(self, state: dict) -> dict:
        org_id = state.get("org_id", "")
        payload = state.get("payload", {}) or {}
        requested_id = payload.get("intervention_id")

        resolved_id = self._store.resolve_intervention_id(org_id, requested_id)

        emit_trace_event(
            "feedback_intervention_resolved",
            {"org_id": org_id, "intervention_id": resolved_id, "was_new": requested_id != resolved_id},
            state,
        )

        return {
            "resolved_intervention_id": resolved_id,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
