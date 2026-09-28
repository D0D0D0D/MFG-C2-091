# MFG-C2-091 — Unit Tests: PostProcessNode (S-3 output gate)

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode


def _invoke_state(advisory, delta_findings=None):
    return {
        "mode": "invoke",
        "advisory_output": advisory,
        "delta_findings": delta_findings or [],
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "correlation_id": "x",
    }


class TestPostProcessNode:
    def test_priority_heading_is_prepended_structurally(self):
        node = PostProcessNode()
        advisory = {
            "priority": "critical",
            "narrative": "経年劣化の可能性があります（根拠: fnd-1）。",
            "cited_finding_ids": ["fnd-1"],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory, [{"finding_id": "fnd-1", "severity": "critical"}]))
        formatted = result["formatted_output"]
        assert formatted["priority"] == "critical"
        assert formatted["priority_label_ja"]

    def test_fabricated_evidence_id_is_redacted(self):
        node = PostProcessNode()
        advisory = {
            "priority": "high",
            "narrative": "重大な損傷が疑われます（根拠: fnd-FAKE-999）。",
            "cited_finding_ids": ["fnd-1"],  # fnd-FAKE-999 is NOT in the valid set
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory))
        narrative = result["formatted_output"]["narrative"]
        assert "fnd-FAKE-999" not in narrative
        assert "要検証" in narrative

    def test_sentence_without_any_evidence_id_is_redacted(self):
        node = PostProcessNode()
        advisory = {
            "priority": "high",
            "narrative": "この設備は来月中に確実に崩壊します。",
            "cited_finding_ids": [],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory))
        narrative = result["formatted_output"]["narrative"]
        assert "確実に崩壊します" not in narrative
        assert "要検証" in narrative

    def test_valid_citation_survives(self):
        node = PostProcessNode()
        advisory = {
            "priority": "medium",
            "narrative": "経年劣化の可能性があります（根拠: fnd-1）。",
            "cited_finding_ids": ["fnd-1"],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory))
        narrative = result["formatted_output"]["narrative"]
        assert "fnd-1" in narrative
        assert "要検証" not in narrative

    def test_assertive_health_classification_is_downgraded(self):
        node = PostProcessNode()
        advisory = {
            "priority": "high",
            "narrative": "区分IIに該当します（根拠: fnd-1）。",
            "cited_finding_ids": ["fnd-1"],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory))
        narrative = result["formatted_output"]["narrative"]
        assert "区分II" not in narrative or "要専門家確認" in narrative

    def test_control_action_directive_is_suppressed(self):
        node = PostProcessNode()
        advisory = {
            "priority": "critical",
            "narrative": "直ちに交通を停止してください（根拠: fnd-1）。",
            "cited_finding_ids": ["fnd-1"],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory))
        narrative = result["formatted_output"]["narrative"]
        assert "直ちに交通を停止してください" not in narrative

    def test_disclaimer_always_present(self):
        node = PostProcessNode()
        advisory = {
            "priority": "low",
            "narrative": "現時点で顕著な経年差分シグナルは検出されていません。",
            "cited_finding_ids": [],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory))
        assert "免責" in result["formatted_output"]["narrative"]

    def test_facility_location_coordinates_in_narrative_are_masked(self):
        """Regression: narrative is exempt from the generic
        _sanitize_client_echoes pass (own per-sentence provenance pipeline),
        so lat/lon masking must be applied to it explicitly — a properly
        cited sentence could otherwise leak facility GPS coordinates
        unmasked even though it correctly cites a valid evidence ID."""
        node = PostProcessNode()
        advisory = {
            "priority": "low",
            "narrative": "座標 35.681236, 139.767125 付近で経年劣化の可能性があります（根拠: fnd-1）。",
            "cited_finding_ids": ["fnd-1"],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory))
        narrative = result["formatted_output"]["narrative"]
        assert "35.681236" not in narrative
        assert "139.767125" not in narrative
        assert "施設特定機微情報" in narrative

    def test_credential_shaped_token_in_narrative_is_masked(self):
        """Regression: same exemption gap as above, for
        credential-shaped tokens."""
        node = PostProcessNode()
        advisory = {
            "priority": "low",
            "narrative": "この所見は fnd-1 に基づきます sk-ant-api03-SECRETVALUE1234567890XYZ12345678。",
            "cited_finding_ids": ["fnd-1"],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory))
        narrative = result["formatted_output"]["narrative"]
        assert "SECRETVALUE1234567890XYZ12345678" not in narrative

    def test_facility_location_coordinates_are_masked(self):
        node = PostProcessNode()
        advisory = {
            "priority": "low",
            "narrative": "現時点で顕著な経年差分シグナルは検出されていません。",
            "cited_finding_ids": [],
            "cited_intervention_ids": [],
        }
        state = _invoke_state(
            advisory,
            [{"finding_id": "fnd-1", "metric": "35.681236, 139.767125", "severity": "low"}],
        )
        result = node(state)
        finding_index = result["formatted_output"].get("finding_index", [])
        if finding_index:
            assert "35.681236" not in str(finding_index[0].get("metric", ""))

    def test_credential_shaped_client_echo_is_masked(self):
        node = PostProcessNode()
        advisory = {
            "priority": "low",
            "narrative": "現時点で顕著な経年差分シグナルは検出されていません。",
            "cited_finding_ids": [],
            "cited_intervention_ids": [],
        }
        state = _invoke_state(
            advisory, [{"finding_id": "fnd-1", "metric": "sk-ant-api03-SECRETVALUE1234567890", "severity": "low"}]
        )
        result = node(state)
        rendered = str(result["formatted_output"])
        assert "SECRETVALUE1234567890" not in rendered

    def test_finding_index_selects_highest_severity_when_capped(self):
        from src.nodes.post_process_node import _MAX_FINDING_INDEX_RECORDED

        node = PostProcessNode()
        findings = [
            {"finding_id": f"fnd-{i}", "metric": "m", "severity": "low", "progression_rate": 0.1}
            for i in range(_MAX_FINDING_INDEX_RECORDED + 10)
        ]
        findings.append({"finding_id": "fnd-critical", "metric": "m", "severity": "critical", "progression_rate": 99.0})
        advisory = {
            "priority": "critical",
            "narrative": "所見です。",
            "cited_finding_ids": [],
            "cited_intervention_ids": [],
        }
        result = node(_invoke_state(advisory, findings))
        finding_index = result["formatted_output"]["finding_index"]
        ids = {f["finding_id"] for f in finding_index}
        assert "fnd-critical" in ids

    def test_ingest_mode_passthrough(self):
        node = PostProcessNode()
        state = {
            "mode": "ingest",
            "ingest_summary": {"assets_written": 1},
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "correlation_id": "x",
        }
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]["ingest_summary"]["assets_written"] == 1

    def test_feedback_mode_passthrough(self):
        node = PostProcessNode()
        state = {
            "mode": "feedback",
            "feedback_result": {"intervention_id": "inv-1"},
            "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
            "correlation_id": "x",
        }
        result = node(state)
        assert result["formatted_output"]["feedback_result"]["intervention_id"] == "inv-1"

    def test_sanitized_advisory_is_persisted_on_normal_invoke_success(self):
        """Regression: record_advisory() was never called, so
        /feedback reconciliation always returned no_prior_advisory. A normal
        (non-degraded) /invoke success must persist the S-3-sanitized
        advisory scoped to org_id+asset_id."""
        from src.services.asset_store import InMemoryAssetStore

        store = InMemoryAssetStore()
        node = PostProcessNode(asset_store=store)
        advisory = {
            "priority": "critical",
            "narrative": "経年劣化の可能性があります（根拠: fnd-1）。",
            "cited_finding_ids": ["fnd-1"],
            "cited_intervention_ids": [],
        }
        state = _invoke_state(advisory, [{"finding_id": "fnd-1", "severity": "critical"}])
        state["org_id"] = "org1"
        state["asset_id"] = "asset1"
        result = node(state)
        assert result["status"] == AgentStatus.SUCCESS.value

        recorded = store.get_recent_advisory("org1", "asset1")
        assert recorded is not None
        assert recorded["priority"] == "critical"
        # The persisted advisory must be the SANITIZED form (S-3通過後) —
        # the disclaimer the gate appends is present in the stored narrative.
        assert "免責" in recorded["narrative"]

    def test_degraded_invoke_does_not_persist_advisory(self):
        """縮退成功（llm_unavailable 等）は照合対象の予測として記録しない."""
        from src.services.asset_store import InMemoryAssetStore

        store = InMemoryAssetStore()
        node = PostProcessNode(asset_store=store)
        advisory = {
            "priority": "low",
            "narrative": "現時点で顕著な経年差分シグナルは検出されていません。",
            "cited_finding_ids": [],
            "cited_intervention_ids": [],
        }
        state = _invoke_state(advisory)
        state["org_id"] = "org1"
        state["asset_id"] = "asset1"
        state["degradation_reason"] = "llm_unavailable"
        node(state)
        assert store.get_recent_advisory("org1", "asset1") is None

    def test_store_failure_during_persistence_does_not_crash_the_advisory(self):
        from src.services.asset_store import InMemoryAssetStore

        class BrokenStore(InMemoryAssetStore):
            def record_advisory(self, *a, **kw):
                raise RuntimeError("db down")

        node = PostProcessNode(asset_store=BrokenStore())
        advisory = {
            "priority": "high",
            "narrative": "経年劣化の可能性があります（根拠: fnd-1）。",
            "cited_finding_ids": ["fnd-1"],
            "cited_intervention_ids": [],
        }
        state = _invoke_state(advisory, [{"finding_id": "fnd-1", "severity": "high"}])
        state["org_id"] = "org1"
        state["asset_id"] = "asset1"
        result = node(state)  # must not raise
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"]["narrative"]

    def test_degradation_reason_surfaces_in_output_and_is_never_sanitized_away(self):
        """Regression: AgentBaseGraph.get_output() only reads
        formatted_output/result — degradation_reason must be folded into
        formatted_output or it silently vanishes from the caller response."""
        node = PostProcessNode()
        state = _invoke_state(
            {
                "priority": "low",
                "narrative": "現時点で顕著な経年差分シグナルは検出されていません。",
                "cited_finding_ids": [],
                "cited_intervention_ids": [],
            }
        )
        state["degradation_reason"] = "insufficient_inspection_history"
        result = node(state)
        assert result["formatted_output"]["degradation_reason"] == "insufficient_inspection_history"
        assert result["degradation_reason"] == "insufficient_inspection_history"
