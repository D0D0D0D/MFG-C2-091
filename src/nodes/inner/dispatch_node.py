"""AgentCore Platform v1.0"""

# Inner graph entry node — BaseGraph.invoke(user_input, ...) only seeds
# state["user_input"] (a JSON envelope string built by the outer
# AssetWorkflowGraphNode.extract_input()); it does NOT let the caller inject
# arbitrary custom state fields. This node parses that envelope and
# populates state["mode"] / org_id / asset_type / asset_id / org_context /
# asset_profile / asset_history / payload so that the downstream
# mode-dispatch conditional edge (which reads state["mode"] per
# docs/02_design.md §🔴 inner graph の state 型注釈方針) has something to
# route on.

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class DispatchNode(FunctionNode):
    """Parse the inner graph's user_input envelope into typed state fields."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        user_input = state.get("user_input", "")
        try:
            envelope = json.loads(user_input) if user_input else {}
        except (json.JSONDecodeError, TypeError):
            emit_trace_event("dispatch_invalid_envelope", {}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DispatchNode: inner user_input is not valid JSON envelope"],
            }
        except RecursionError:
            # A client-controlled request body containing a deeply nested
            # array/object exhausts the interpreter's recursion limit INSIDE
            # the stdlib decoder itself — not a json.JSONDecodeError, so not
            # caught above. Left uncaught it would crash the whole invocation
            # (remote DoS). Treat exactly like a malformed envelope.
            emit_trace_event("dispatch_invalid_envelope", {"reason": "excessive_nesting_depth"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DispatchNode: inner user_input is nested too deeply to parse"],
            }

        mode = envelope.get("mode")
        if mode not in ("invoke", "ingest", "feedback"):
            emit_trace_event("dispatch_unknown_mode", {"mode": mode}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"DispatchNode: unknown or missing mode {mode!r}"],
            }

        emit_trace_event("dispatch_resolved", {"mode": mode}, state)
        return {
            "mode": mode,
            "org_id": envelope.get("org_id", ""),
            "asset_type": envelope.get("asset_type", ""),
            "asset_id": envelope.get("asset_id", ""),
            "payload": envelope.get("payload", {}),
            "org_context": envelope.get("org_context", {}),
            "asset_profile": envelope.get("asset_profile", {}),
            "asset_history": envelope.get("asset_history", {}),
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
