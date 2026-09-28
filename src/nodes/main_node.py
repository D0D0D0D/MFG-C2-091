"""AgentCore Platform v1.0"""

# main slot — AssetWorkflowGraphNode (docs/02_design.md §main（GraphNode）契約).
# Wraps the inner AssetWorkflowGraph (src/graph/domain_workflow_graph.py).
#
# get_subgraph()  — cached in __init__ (avoid rebuilding the inner StateGraph
#                    on every execute() — Cat2 HITL subgraph-cache lesson).
# extract_input() — JSON-serialized envelope (default=str) of the fields the
#                    inner graph's dispatch node needs.
# merge_output()  — mode-dependent: only returns the keys the mode actually
#                    produced (no None-padding — docs/02_design.md §main 契約).

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class AssetWorkflowGraphNode(GraphNode):
    """Outer `main` slot — delegates to the inner AssetWorkflowGraph."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any] | None = None, asset_store=None) -> None:
        super().__init__()
        self._config = config or {}
        self._asset_store = asset_store
        self._subgraph = None  # built lazily on first get_subgraph() call

    def get_subgraph(self):
        if self._subgraph is None:
            from src.graph.domain_workflow_graph import AssetWorkflowGraph

            subgraph = AssetWorkflowGraph(config=self._config, asset_store=self._asset_store)
            subgraph.compile()
            self._subgraph = subgraph
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        return state.get(
            "validated_input",
            json.dumps(
                {
                    "mode": state.get("mode"),
                    "org_id": state.get("org_id"),
                    "asset_type": state.get("asset_type"),
                    "asset_id": state.get("asset_id"),
                    "payload": state.get("payload", {}),
                    "org_context": state.get("org_context", {}),
                    "asset_profile": state.get("asset_profile", {}),
                    "asset_history": state.get("asset_history", {}),
                },
                default=str,
            ),
        )

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        """Only return the fields the mode that actually ran produced
        (docs/02_design.md §main 契約 — no None-padding for absent modes).
        """
        delta: dict[str, Any] = {"status": sub_result.get("status", AgentStatus.SUCCESS.value)}
        if sub_result.get("degradation_reason"):
            delta["degradation_reason"] = sub_result["degradation_reason"]

        if "delta_findings" in sub_result:
            delta["delta_findings"] = sub_result["delta_findings"]
            # Truncation disclosure must cross the inner/outer boundary too —
            # an omitted key is silently dropped by LangGraph's state_schema
            # projection, hiding from the operator that delta_findings was
            # capped at all (mirrors AquacultureEnvironmentalEarlyWarningAgent's signal_list_total_count
            # precedent).
            delta["delta_findings_total_count"] = sub_result.get(
                "delta_findings_total_count", len(sub_result["delta_findings"])
            )
            if sub_result.get("delta_findings_truncated"):
                delta["delta_findings_truncated"] = True
        if "candidate_cases" in sub_result:
            delta["candidate_cases"] = sub_result["candidate_cases"]
            delta["candidate_cases_total_count"] = sub_result.get(
                "candidate_cases_total_count", len(sub_result["candidate_cases"])
            )
            if sub_result.get("candidate_cases_truncated"):
                delta["candidate_cases_truncated"] = True
        if "advisory_output" in sub_result:
            delta["advisory_output"] = sub_result["advisory_output"]
        if "ingest_summary" in sub_result:
            delta["ingest_summary"] = sub_result["ingest_summary"]
        if "feedback_result" in sub_result:
            delta["feedback_result"] = sub_result["feedback_result"]

        return delta

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        return state

    def _extra_security_gate_output(self, result: dict) -> dict:
        return result
