"""AgentCore Platform v1.0"""

# Inner graph for MFG-C2-091 (docs/02_design.md §経年所見の3段パイプライン
# §/ingest /feedback の内部ノード構成). Fully custom topology (BaseGraph, not
# AgentBaseGraph) — 3 modes share this graph but execute disjoint node sets
# selected by state["mode"].
#
# 🔴 docs/02_design.md §🔴 inner graph の state 型注釈方針: the mode-routing
# function and get_output() are annotated with
# AssetWorkflowState (this module's own schema), NOT AgentState. Annotating
# with the base AgentState causes state projection to drop the custom `mode`
# field, producing mis-routing.
#
# Directory rule: src/graph/domain_workflow_graph.py is the only accepted path.

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.advisory_synthesis_node import AdvisorySynthesisNode
from src.nodes.inner.case_matching_node import CaseMatchingNode
from src.nodes.inner.dispatch_node import DispatchNode
from src.nodes.inner.normalize_input_node import NormalizeInputNode
from src.nodes.inner.reconcile_prediction_node import ReconcilePredictionNode
from src.nodes.inner.resolve_intervention_node import ResolveInterventionNode
from src.nodes.inner.signal_detection_node import SignalDetectionNode
from src.nodes.inner.structure_with_llm_node import StructureWithLlmNode
from src.nodes.inner.update_intervention_node import UpdateInterventionNode
from src.nodes.inner.validate_records_node import ValidateRecordsNode
from src.nodes.inner.write_records_node import WriteRecordsNode
from src.schemas.state import State


class AssetWorkflowState(State):
    """Inner graph state schema — see docs/02_design.md §🔴 inner graph の
    state 型注釈方針. Identical field contract to the outer State, declared
    as its own type so route()/get_output() type annotations resolve
    against the real schema rather than the framework base AgentState.
    """


class AssetWorkflowGraph(BaseGraph):
    """Inner domain workflow — mode-dispatch across /ingest /invoke /feedback.

    Topology (mode-dependent, wired via conditional edges from `dispatch` to
    each mode's entry node and plain edges to END from each mode's terminal
    node):

        /invoke:   START -> dispatch -> signal_detection -> case_matching -> advisory_synthesis -> END
        /ingest:   START -> dispatch -> normalize_input -> structure_with_llm -> validate_records -> write_records -> END
        /feedback: START -> dispatch -> resolve_intervention -> update_intervention -> reconcile_prediction -> END

    `dispatch` is required because BaseGraph.invoke(user_input, ...) only
    seeds state["user_input"] — it cannot inject arbitrary custom state
    fields at invocation time. DispatchNode parses the JSON envelope (built
    by the outer AssetWorkflowGraphNode.extract_input()) into state["mode"] /
    org_id / asset_type / asset_id / payload / ... so the conditional edge
    immediately after it has state["mode"] to route on.
    """

    def __init__(self, config: dict[str, Any] | None = None, asset_store=None) -> None:
        # asset_store is a constructor-injection seam (fake in tests, real
        # store wired by server.py's wire_services()). Falls back to an
        # in-memory store so the graph never crashes for lack of wiring in
        # non-write-critical paths (e.g. ad-hoc local smoke tests).
        from src.services.asset_store import InMemoryAssetStore

        self._asset_store = asset_store or InMemoryAssetStore()
        super().__init__(config=config or {})

    @property
    def name(self) -> str:
        return "asset_workflow"

    @property
    def state_schema(self) -> type:
        return AssetWorkflowState

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        llm = self.config.get("llm")

        self._nodes["dispatch"] = DispatchNode()

        # /invoke
        self._nodes["signal_detection"] = SignalDetectionNode(asset_store=self._asset_store)
        self._nodes["case_matching"] = CaseMatchingNode(asset_store=self._asset_store)
        self._nodes["advisory_synthesis"] = AdvisorySynthesisNode(llm=llm)

        # /ingest
        self._nodes["normalize_input"] = NormalizeInputNode()
        self._nodes["structure_with_llm"] = StructureWithLlmNode(llm=llm)
        self._nodes["validate_records"] = ValidateRecordsNode()
        self._nodes["write_records"] = WriteRecordsNode(asset_store=self._asset_store)

        # /feedback
        self._nodes["resolve_intervention"] = ResolveInterventionNode(asset_store=self._asset_store)
        self._nodes["update_intervention"] = UpdateInterventionNode(asset_store=self._asset_store, llm=llm)
        self._nodes["reconcile_prediction"] = ReconcilePredictionNode(asset_store=self._asset_store)

    def add_edges(self) -> None:
        self._sg.add_edge(START, "dispatch")
        self._sg.add_conditional_edges(
            "dispatch",
            self._route_entry,
            {
                "invoke": "signal_detection",
                "ingest": "normalize_input",
                "feedback": "resolve_intervention",
                "error": END,
            },
        )

        # /invoke pipeline
        self._sg.add_edge("signal_detection", "case_matching")
        self._sg.add_edge("case_matching", "advisory_synthesis")
        self._sg.add_edge("advisory_synthesis", END)

        # /ingest pipeline
        self._sg.add_edge("normalize_input", "structure_with_llm")
        self._sg.add_edge("structure_with_llm", "validate_records")
        self._sg.add_edge("validate_records", "write_records")
        self._sg.add_edge("write_records", END)

        # /feedback pipeline
        self._sg.add_edge("resolve_intervention", "update_intervention")
        self._sg.add_edge("update_intervention", "reconcile_prediction")
        self._sg.add_edge("reconcile_prediction", END)

    def _route_entry(self, state: AssetWorkflowState) -> str:
        """Mode dispatch — MUST be annotated with AssetWorkflowState, not
        AgentState (docs/02_design.md §🔴 inner graph の state 型注釈方針:
        annotating with the base AgentState causes state projection to drop
        the custom `mode` field, producing mis-routing).

        Runs after `dispatch`, so state["mode"] is guaranteed to be set (or
        DispatchNode already routed status=error, handled below).
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return "error"
        mode = state.get("mode")
        if mode not in ("invoke", "ingest", "feedback"):
            return "error"
        return mode

    def route(self, state: AssetWorkflowState) -> str:
        """Required by BaseGraph ABC. Not used for conditional routing beyond
        the entry dispatch (add_conditional_edges references _route_entry,
        not this method) — each mode's internal pipeline is linear.
        """
        return END

    def get_output(self, state: AssetWorkflowState) -> dict:
        """Shape sub_result for the outer AssetWorkflowGraphNode.merge_output().

        MUST be annotated with AssetWorkflowState — see module docstring.
        Returns every mode-specific field; merge_output() on the outer side
        picks only the ones relevant to the mode that actually ran.
        """
        output: dict[str, Any] = {
            "status": state.get("status", AgentStatus.SUCCESS.value),
        }
        if state.get("degradation_reason"):
            output["degradation_reason"] = state["degradation_reason"]

        mode = state.get("mode")
        if mode == "invoke":
            output["delta_findings"] = state.get("delta_findings", [])
            output["delta_findings_total_count"] = state.get(
                "delta_findings_total_count", len(output["delta_findings"])
            )
            if state.get("delta_findings_truncated"):
                output["delta_findings_truncated"] = True
            output["candidate_cases"] = state.get("candidate_cases", [])
            output["candidate_cases_total_count"] = state.get(
                "candidate_cases_total_count", len(output["candidate_cases"])
            )
            if state.get("candidate_cases_truncated"):
                output["candidate_cases_truncated"] = True
            output["advisory_output"] = state.get("advisory_output", {})
        elif mode == "ingest":
            output["ingest_summary"] = state.get("ingest_summary", {})
        elif mode == "feedback":
            output["feedback_result"] = state.get("feedback_result", {})

        return output
