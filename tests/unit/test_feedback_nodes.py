# MFG-C2-091 — Unit Tests: /feedback inner nodes
# (resolve_intervention, update_intervention, reconcile_prediction)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.reconcile_prediction_node import ReconcilePredictionNode
from src.nodes.inner.resolve_intervention_node import ResolveInterventionNode
from src.nodes.inner.update_intervention_node import UpdateInterventionNode
from src.services.asset_store import InMemoryAssetStore


class TestResolveInterventionNode:
    def test_mints_new_id_when_absent(self):
        node = ResolveInterventionNode(asset_store=InMemoryAssetStore())
        result = node.execute({"org_id": "org1", "payload": {}})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["resolved_intervention_id"].startswith("inv-org1-")

    def test_reuses_existing_id(self):
        store = InMemoryAssetStore()
        store.write_interventions("org1", [{"intervention_id": "inv-org1-000005", "asset_id": "a1"}])
        node = ResolveInterventionNode(asset_store=store)
        result = node.execute({"org_id": "org1", "payload": {"intervention_id": "inv-org1-000005"}})
        assert result["resolved_intervention_id"] == "inv-org1-000005"


class TestUpdateInterventionNode:
    def test_updates_fields_from_structured_payload(self):
        store = InMemoryAssetStore()
        node = UpdateInterventionNode(asset_store=store, llm=None)
        state = {
            "org_id": "org1",
            "asset_id": "a1",
            "resolved_intervention_id": "inv-1",
            "payload": {"action": "repair", "post_condition": "good", "outcome_narrative": "fixed the gap"},
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["updated_intervention_fields"]["action"] == "repair"
        history = store.query_intervention_history("org1", "a1")
        assert history[0]["action"] == "repair"

    def test_free_note_structured_when_no_llm_passes_through(self):
        node = UpdateInterventionNode(asset_store=InMemoryAssetStore(), llm=None)
        state = {
            "org_id": "org1",
            "asset_id": "a1",
            "resolved_intervention_id": "inv-1",
            "payload": {"action": "repair", "free_note": "raw note text"},
        }
        result = node.execute(state)
        assert result["updated_intervention_fields"]["outcome_narrative"] == "raw note text"


class TestReconcilePredictionNode:
    def test_no_prior_advisory_is_inconclusive(self):
        node = ReconcilePredictionNode(asset_store=InMemoryAssetStore())
        state = {
            "org_id": "org1",
            "asset_id": "a1",
            "resolved_intervention_id": "inv-1",
            "updated_intervention_fields": {},
        }
        result = node.execute(state)
        assert result["feedback_result"]["reconciliation"]["outcome"] == "no_prior_advisory"

    def test_high_priority_prediction_with_repair_action_is_a_hit(self):
        store = InMemoryAssetStore()
        store.record_advisory("org1", "a1", {"priority": "critical"})
        node = ReconcilePredictionNode(asset_store=store)
        state = {
            "org_id": "org1",
            "asset_id": "a1",
            "resolved_intervention_id": "inv-1",
            "updated_intervention_fields": {"action": "repair"},
        }
        result = node.execute(state)
        assert result["feedback_result"]["reconciliation"]["outcome"] == "hit"

    def test_high_priority_prediction_with_observe_only_is_false_positive_candidate(self):
        store = InMemoryAssetStore()
        store.record_advisory("org1", "a1", {"priority": "high"})
        node = ReconcilePredictionNode(asset_store=store)
        state = {
            "org_id": "org1",
            "asset_id": "a1",
            "resolved_intervention_id": "inv-1",
            "updated_intervention_fields": {"action": "observe"},
        }
        result = node.execute(state)
        assert result["feedback_result"]["reconciliation"]["outcome"] == "false_positive_candidate"
