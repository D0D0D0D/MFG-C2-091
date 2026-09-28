# MFG-C2-091 — Unit Tests: PreProcessNode

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.pre_process_node import PreProcessNode
from src.services.asset_store import InMemoryAssetStore


def _state(payload_obj: dict) -> dict:
    return {
        "user_input": json.dumps(payload_obj),
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "x",
    }


class TestPreProcessNode:
    def test_missing_org_id_is_fail_closed(self):
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state({"mode": "invoke", "asset_type": "bridge_appurtenance", "asset_id": "a1", "payload": {}})
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("fail-closed" in msg for msg in result["error_log"])

    def test_empty_org_id_is_fail_closed(self):
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state(
            {"mode": "invoke", "org_id": "  ", "asset_type": "bridge_appurtenance", "asset_id": "a1", "payload": {}}
        )
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_unknown_mode_is_rejected(self):
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state(
            {"mode": "delete_everything", "org_id": "org1", "asset_type": "bridge_appurtenance", "payload": {}}
        )
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_missing_asset_type_is_rejected(self):
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state({"mode": "ingest", "org_id": "org1", "payload": {}})
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_unknown_asset_type_degrades_not_fail_closed(self):
        """🔴 §(a): an unregistered asset_type is NOT a tenant-boundary
        failure — it must succeed with a degradation marker, unlike org_id."""
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state(
            {
                "mode": "ingest",
                "org_id": "org1",
                "asset_type": "totally_unknown_type",
                "payload": {"records": []},
            }
        )
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["degradation_reason"] == "asset_profile_not_found"
        assert result["asset_profile"] == {}

    def test_known_asset_type_resolves_profile(self):
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state(
            {"mode": "ingest", "org_id": "org1", "asset_type": "bridge_appurtenance", "payload": {"records": []}}
        )
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "metrics" in result["asset_profile"]
        assert "degradation_reason" not in result

    def test_invoke_requires_asset_id(self):
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state({"mode": "invoke", "org_id": "org1", "asset_type": "bridge_appurtenance", "payload": {}})
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_feedback_requires_asset_id(self):
        """Regression: /feedback writes to interventions.asset_id
        and reconcile_prediction looks up the prior advisory BY asset_id — an
        absent asset_id must be rejected here, not silently proceed with an
        empty string (which would corrupt the store and always report
        no_prior_advisory)."""
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state(
            {"mode": "feedback", "org_id": "org1", "asset_type": "bridge_appurtenance", "payload": {"action": "repair"}}
        )
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("asset_id" in msg for msg in result["error_log"])

    def test_ingest_does_not_require_asset_id(self):
        """/ingest can carry records for multiple assets; asset_id at the
        payload top level is optional (each record supplies its own)."""
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state(
            {"mode": "ingest", "org_id": "org1", "asset_type": "bridge_appurtenance", "payload": {"records": []}}
        )
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value

    def test_invoke_loads_asset_history(self):
        store = InMemoryAssetStore()
        store.write_inspections("org1", [{"asset_id": "a1", "inspected_at": "2025-01-01", "measurements": []}])
        node = PreProcessNode(asset_store=store)
        state = _state(
            {
                "mode": "invoke",
                "org_id": "org1",
                "asset_type": "bridge_appurtenance",
                "asset_id": "a1",
                "payload": {"current_inspection": {}},
            }
        )
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(result["asset_history"]["inspections"]) == 1

    def test_malformed_json_is_rejected(self):
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = {
            "user_input": "{not valid json",
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "correlation_id": "x",
        }
        result = node(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_deeply_nested_payload_does_not_crash(self):
        """A deeply nested JSON array in the raw request body must not crash
        json.loads (RecursionError) inside PreProcessNode — build the nested
        structure as a raw STRING (not via json.dumps, which would itself
        recurse the same way while serializing a Python list this deep)."""
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        nested = "[" * 2000 + '"x"' + "]" * 2000
        user_input = '{"mode":"invoke","org_id":"org1","asset_type":"x","asset_id":"a1","payload":' + nested + "}"
        state = {
            "user_input": user_input,
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "correlation_id": "x",
        }
        result = node(state)  # must not raise RecursionError
        assert result["status"] in (AgentStatus.ERROR.value, AgentStatus.SUCCESS.value)

    def test_s2_masks_inspector_name_pii_in_free_note(self):
        node = PreProcessNode(asset_store=InMemoryAssetStore())
        state = _state(
            {
                "mode": "ingest",
                "org_id": "org1",
                "asset_type": "bridge_appurtenance",
                "payload": {"records": [], "free_note": "田中太郎様が確認しました"},
            }
        )
        gated = node._extra_security_gate_input(state)
        envelope = json.loads(gated["user_input"])
        assert "[MASKED]" in envelope["payload"]["free_note"]
        assert "田中太郎様" not in envelope["payload"]["free_note"]
