# MFG-C2-091 — Unit Tests: AssetWorkflowGraphNode (main slot, GraphNode contract)

import json

from framework.schemas.agent_status import AgentStatus

from src.nodes.main_node import AssetWorkflowGraphNode


class TestAssetWorkflowGraphNode:
    def test_get_subgraph_is_cached_not_rebuilt(self):
        node = AssetWorkflowGraphNode(config={"llm": None})
        first = node.get_subgraph()
        second = node.get_subgraph()
        assert first is second

    def test_extract_input_prefers_validated_input(self):
        node = AssetWorkflowGraphNode(config={"llm": None})
        state = {"validated_input": json.dumps({"mode": "invoke"})}
        assert node.extract_input(state) == state["validated_input"]

    def test_extract_input_falls_back_to_composed_envelope(self):
        node = AssetWorkflowGraphNode(config={"llm": None})
        state = {"mode": "ingest", "org_id": "org1", "asset_type": "bridge_appurtenance", "payload": {}}
        raw = node.extract_input(state)
        parsed = json.loads(raw)
        assert parsed["mode"] == "ingest"
        assert parsed["org_id"] == "org1"

    def test_merge_output_invoke_mode_omits_ingest_and_feedback_keys(self):
        node = AssetWorkflowGraphNode(config={"llm": None})
        sub_result = {
            "status": AgentStatus.SUCCESS.value,
            "delta_findings": [],
            "candidate_cases": [],
            "advisory_output": {},
        }
        delta = node.merge_output({}, sub_result)
        assert "delta_findings" in delta
        assert "ingest_summary" not in delta
        assert "feedback_result" not in delta

    def test_merge_output_propagates_degradation_reason(self):
        node = AssetWorkflowGraphNode(config={"llm": None})
        sub_result = {
            "status": AgentStatus.SUCCESS.value,
            "ingest_summary": {},
            "degradation_reason": "llm_unavailable",
        }
        delta = node.merge_output({}, sub_result)
        assert delta["degradation_reason"] == "llm_unavailable"

    def test_required_trust_level_is_valid_enum(self):
        from framework.schemas.trust_level import TrustLevel

        assert AssetWorkflowGraphNode.required_trust_level in (
            TrustLevel.ANONYMOUS,
            TrustLevel.VERIFIED_EXTERNAL,
            TrustLevel.INTERNAL,
        )

    def test_error_strategy_is_propagate(self):
        assert AssetWorkflowGraphNode.error_strategy == "propagate"

    def test_propagate_hitl_is_false(self):
        assert AssetWorkflowGraphNode.propagate_hitl is False
