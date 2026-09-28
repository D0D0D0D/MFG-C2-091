# MFG-C2-091 — Unit Tests: CaseMatchingNode (Stage 2, search-only, non-LLM)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.case_matching_node import CaseMatchingNode
from src.services.asset_store import InMemoryAssetStore


class TestCaseMatchingNode:
    def test_no_findings_no_candidates(self):
        node = CaseMatchingNode(asset_store=InMemoryAssetStore())
        result = node.execute({"org_id": "org1", "asset_type": "bridge_appurtenance", "delta_findings": []})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["candidate_cases"] == []

    def test_matches_intervention_history_by_trigger_metric(self):
        store = InMemoryAssetStore()
        store.write_interventions(
            "org1",
            [{"asset_id": "asset1", "action": "repair", "triggering_metrics": ["expansion_joint_gap_mm"]}],
        )
        node = CaseMatchingNode(asset_store=store)
        findings = [{"finding_id": "fnd-1", "metric": "expansion_joint_gap_mm", "severity": "critical"}]
        result = node.execute({"org_id": "org1", "asset_type": "bridge_appurtenance", "delta_findings": findings})
        assert len(result["candidate_cases"]) == 1
        assert result["candidate_cases"][0]["matched_intervention_id"] is not None
        assert result["candidate_cases"][0]["source"] == "org_intervention_history"

    def test_skips_org_kb_search_when_insufficient_history(self):
        """§(b): thin history -> Stage2 excludes org KB search (degradation
        already set by Stage1, not overwritten here)."""
        store = InMemoryAssetStore()
        store.write_interventions(
            "org1",
            [{"asset_id": "asset1", "action": "repair", "triggering_metrics": ["expansion_joint_gap_mm"]}],
        )
        node = CaseMatchingNode(asset_store=store)
        findings = [{"finding_id": "fnd-1", "metric": "expansion_joint_gap_mm", "severity": "critical"}]
        result = node.execute(
            {
                "org_id": "org1",
                "asset_type": "bridge_appurtenance",
                "delta_findings": findings,
                "degradation_reason": "insufficient_inspection_history",
            }
        )
        assert result["candidate_cases"] == []

    def test_skips_org_kb_search_when_reason_is_combined_via_plus(self):
        """Regression: Stage1 can return a "+"-joined combined
        degradation_reason (e.g. "insufficient_inspection_history+asset_
        profile_not_found"). An exact-equality check against the single
        reason string would fail to detect this and let Stage2 search the
        org KB anyway, contradicting §(b)."""
        store = InMemoryAssetStore()
        store.write_interventions(
            "org1",
            [{"asset_id": "asset1", "action": "repair", "triggering_metrics": ["expansion_joint_gap_mm"]}],
        )
        node = CaseMatchingNode(asset_store=store)
        findings = [{"finding_id": "fnd-1", "metric": "expansion_joint_gap_mm", "severity": "critical"}]
        result = node.execute(
            {
                "org_id": "org1",
                "asset_type": "bridge_appurtenance",
                "delta_findings": findings,
                "degradation_reason": "insufficient_inspection_history+asset_profile_not_found",
            }
        )
        assert result["candidate_cases"] == []

    def test_matches_asset_profile_degradation_mode(self):
        node = CaseMatchingNode(asset_store=InMemoryAssetStore())
        profile = {
            "degradation_modes": [{"mode_id": "expansion_joint_wear", "trigger_metrics": ["expansion_joint_gap_mm"]}]
        }
        findings = [{"finding_id": "fnd-1", "metric": "expansion_joint_gap_mm", "severity": "high"}]
        result = node.execute(
            {
                "org_id": "org1",
                "asset_type": "bridge_appurtenance",
                "asset_profile": profile,
                "delta_findings": findings,
            }
        )
        assert len(result["candidate_cases"]) == 1
        assert result["candidate_cases"][0]["matched_finding_id"] == "fnd-1"
        assert result["candidate_cases"][0]["category"] == "expansion_joint_wear"

    def test_store_failure_degrades_to_empty_candidates(self):
        class BrokenStore(InMemoryAssetStore):
            def query_similar_interventions(self, *a, **kw):
                raise RuntimeError("db down")

        node = CaseMatchingNode(asset_store=BrokenStore())
        findings = [{"finding_id": "fnd-1", "metric": "m", "severity": "high"}]
        result = node.execute({"org_id": "org1", "asset_type": "x", "delta_findings": findings})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["candidate_cases"] == []
