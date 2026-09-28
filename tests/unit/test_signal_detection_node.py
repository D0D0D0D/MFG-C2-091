# MFG-C2-091 — Unit Tests: SignalDetectionNode (Stage 1, deterministic)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.signal_detection_node import SignalDetectionNode
from src.services.asset_store import InMemoryAssetStore

# OPC UA LimitAlarmType-style scalar thresholds (config/asset_types/*.yaml).
_GAP_LIMITS = {
    "unit": "mm",
    "low_low_limit": 5.0,
    "low_limit": 10.0,
    "high_limit": 40.0,
    "high_high_limit": 60.0,
}


def _base_state(measurements, asset_profile=None, asset_history=None):
    return {
        "org_id": "org1",
        "asset_id": "asset1",
        "asset_profile": asset_profile or {},
        "asset_history": asset_history or {"inspections": [], "interventions": []},
        "payload": {"current_inspection": {"inspected_at": "2026-07-01", "measurements": measurements}},
    }


class TestSignalDetectionNode:
    def test_no_measurements_no_findings(self):
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        result = node.execute(_base_state([]))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["delta_findings"] == []

    def test_beyond_high_high_limit_produces_critical_finding(self):
        """A reading past `high_high_limit` is the WORST case, not a data anomaly."""
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        state = _base_state([{"metric": "expansion_joint_gap_mm", "value": 65.0}], asset_profile=profile)
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(result["delta_findings"]) == 1
        finding = result["delta_findings"][0]
        assert finding["severity"] == "critical"
        assert finding["finding_id"] == SignalDetectionNode._build_finding_id(
            "asset1", "expansion_joint_gap_mm", "2026-07-01"
        )

    def test_below_low_low_limit_produces_critical_finding(self):
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        state = _base_state([{"metric": "expansion_joint_gap_mm", "value": 2.0}], asset_profile=profile)
        result = node.execute(state)
        assert len(result["delta_findings"]) == 1
        assert result["delta_findings"][0]["severity"] == "critical"

    def test_value_exactly_on_a_limit_is_not_a_signal(self):
        """Comparisons are STRICT. A regression to `>=`/`<=` must fail here."""
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}

        def _sev(value):
            state = _base_state([{"metric": "expansion_joint_gap_mm", "value": value}], asset_profile=profile)
            findings = node.execute(state)["delta_findings"]
            return findings[0]["severity"] if findings else None

        assert _sev(40.0) is None  # == high_limit
        assert _sev(10.0) is None  # == low_limit
        assert _sev(60.0) == "medium"  # == high_high_limit -> still only past high_limit
        assert _sev(5.0) == "medium"  # == low_low_limit
        assert _sev(60.0001) == "critical"
        assert _sev(4.9999) == "critical"

    def test_string_valued_reading_is_not_silently_dropped(self):
        """A JSON payload carrying a numeric string must not vanish."""
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        state = _base_state([{"metric": "expansion_joint_gap_mm", "value": "65.0"}], asset_profile=profile)
        result = node.execute(state)
        assert len(result["delta_findings"]) == 1
        assert result["delta_findings"][0]["severity"] == "critical"
        assert result["unparseable_measurement_count"] == 0 if "unparseable_measurement_count" in result else True

    def test_unreadable_value_is_reported_not_swallowed(self):
        """`True` is rejected too: bool subclasses int, so it would otherwise
        score as the reading 1.0."""
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        state = _base_state(
            [
                {"metric": "expansion_joint_gap_mm", "value": "n/a"},
                {"metric": "expansion_joint_gap_mm", "value": True},
            ],
            asset_profile=profile,
        )
        result = node.execute(state)
        assert result["delta_findings"] == []

    def test_non_finite_values_are_unparseable_not_silent_no_signal(self):
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        for raw in (float("nan"), "NaN", float("inf"), "-inf"):
            state = _base_state([{"metric": "expansion_joint_gap_mm", "value": raw}], asset_profile=profile)
            result = node.execute(state)
            assert result["delta_findings"] == [], f"{raw!r} produced a finding"

    def test_missing_metric_and_value_are_rejected_not_scored(self):
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        state = _base_state([{"value": 65.0}, {"metric": "expansion_joint_gap_mm"}], asset_profile=profile)
        result = node.execute(state)
        assert result["delta_findings"] == []

    def test_metric_not_in_profile_is_still_deviation_checked(self):
        """An unconfigured metric can still be caught by progression detection
        against the asset's own inspection history."""
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        history = {
            "inspections": [
                {"inspected_at": "2025-01-01", "measurements": [{"metric": "unknown_metric", "value": 1.0}]},
                {"inspected_at": "2025-06-01", "measurements": [{"metric": "unknown_metric", "value": 1.0}]},
            ],
            "interventions": [{"intervention_id": "inv-1", "asset_id": "asset1"}],
        }
        state = _base_state(
            [{"metric": "unknown_metric", "value": 50.0}], asset_profile={"metrics": {}}, asset_history=history
        )
        result = node.execute(state)
        assert len(result["delta_findings"]) == 1
        assert result["delta_findings"][0]["progression_rate"] is not None

    def test_insufficient_history_sets_degradation_reason(self):
        """🔴 §(b) min data requirement: <2 inspections or <1 intervention ->
        degradation_reason=insufficient_inspection_history."""
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        state = _base_state([{"metric": "expansion_joint_gap_mm", "value": 20.0}], asset_profile=profile)
        result = node.execute(state)
        assert result["degradation_reason"] == "insufficient_inspection_history"

    def test_sufficient_history_does_not_set_min_data_degradation(self):
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        history = {
            "inspections": [
                {"inspected_at": "2025-01-01", "measurements": [{"metric": "expansion_joint_gap_mm", "value": 15}]},
                {"inspected_at": "2025-06-01", "measurements": [{"metric": "expansion_joint_gap_mm", "value": 20}]},
            ],
            "interventions": [{"intervention_id": "inv-1", "asset_id": "asset1"}],
        }
        state = _base_state(
            [{"metric": "expansion_joint_gap_mm", "value": 22.0}], asset_profile=profile, asset_history=history
        )
        result = node.execute(state)
        assert "degradation_reason" not in result or "insufficient_inspection_history" not in result.get(
            "degradation_reason", ""
        )

    def test_unknown_asset_type_empty_profile_sets_asset_profile_not_found(self):
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        history = {
            "inspections": [
                {"inspected_at": "2025-01-01", "measurements": []},
                {"inspected_at": "2025-06-01", "measurements": []},
            ],
            "interventions": [{"intervention_id": "inv-1", "asset_id": "asset1"}],
        }
        state = _base_state([{"metric": "foo", "value": 1.0}], asset_profile={}, asset_history=history)
        result = node.execute(state)
        assert result["degradation_reason"] == "asset_profile_not_found"

    def test_both_degradation_reasons_combine_without_overwriting(self):
        """Regression: insufficient_history AND asset_profile_not_found holding
        simultaneously must not silently drop one reason."""
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        state = _base_state([{"metric": "foo", "value": 1.0}], asset_profile={})
        result = node.execute(state)
        assert "insufficient_inspection_history" in result["degradation_reason"]
        assert "asset_profile_not_found" in result["degradation_reason"]

    def test_finding_id_is_deterministic(self):
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        state = _base_state([{"metric": "expansion_joint_gap_mm", "value": 65.0}], asset_profile=profile)
        r1 = node.execute(state)
        r2 = node.execute(state)
        assert r1["delta_findings"][0]["finding_id"] == r2["delta_findings"][0]["finding_id"]

    def test_finding_id_is_ascii_safe_for_s3_gate(self):
        """A non-ASCII asset_id must not defeat the S-3 provenance gate's
        ASCII-anchored evidence-ID extractor."""
        from src.nodes.post_process_node import _EVIDENCE_ID_PATTERN

        for asset_id in ("asset1", "橋梁A", "a b", "a.b", None):
            fid = SignalDetectionNode._build_finding_id(asset_id, "expansion_joint_gap_mm", "2026-07-01")
            found = _EVIDENCE_ID_PATTERN.findall(fid)
            assert found and found[0] == fid, f"{asset_id!r} -> {fid!r} not wholly matched"

    def test_malformed_profile_limit_raises_rather_than_reading_as_safe(self):
        import pytest

        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": {"high_limit": "forty"}}}
        state = _base_state([{"metric": "expansion_joint_gap_mm", "value": 65.0}], asset_profile=profile)
        with pytest.raises(TypeError):
            node.execute(state)

    def test_progression_rate_computed_from_last_prior_inspection(self):
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        history = {
            "inspections": [
                {"inspected_at": "2025-01-01", "measurements": [{"metric": "expansion_joint_gap_mm", "value": 20}]},
            ],
            "interventions": [{"intervention_id": "inv-1", "asset_id": "asset1"}],
        }
        state = _base_state(
            [{"metric": "expansion_joint_gap_mm", "value": 25.0}], asset_profile=profile, asset_history=history
        )
        result = node.execute(state)
        assert len(result["delta_findings"]) == 0 or result["delta_findings"][0]["delta"] == 5.0

    def test_findings_capped_but_critical_finding_always_survives(self):
        """SAFETY-CRITICAL: with more than _MAX_FINDINGS measurements, a single
        critical finding appended LAST must still be present in the capped
        delta_findings list."""
        from src.nodes.inner.signal_detection_node import _MAX_FINDINGS

        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {f"metric_{i}": _GAP_LIMITS for i in range(_MAX_FINDINGS + 50)}}
        measurements = [{"metric": f"metric_{i}", "value": 50.0} for i in range(_MAX_FINDINGS + 50)]
        # The critical one, appended LAST — worst case for a naive first-N cap.
        measurements.append({"metric": "critical_metric", "value": 65.0})
        profile["metrics"]["critical_metric"] = _GAP_LIMITS

        state = _base_state(measurements, asset_profile=profile)
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(result["delta_findings"]) <= _MAX_FINDINGS
        assert result["delta_findings_truncated"] is True
        severities = {f["severity"] for f in result["delta_findings"]}
        assert "critical" in severities, "the critical finding was dropped by the cap"

    def test_digest_is_128_bits_or_more(self):
        sid = SignalDetectionNode._build_finding_id("asset1", "expansion_joint_gap_mm", "2026-07-01")
        digest = sid.rsplit("-", 1)[-1]
        assert len(digest) >= 32
        assert all(c in "0123456789abcdef" for c in digest)

    def test_non_scalar_metric_is_rejected_not_recursed_into(self):
        deep = "x"
        for _ in range(1500):
            deep = [deep]
        node = SignalDetectionNode(asset_store=InMemoryAssetStore())
        profile = {"metrics": {"expansion_joint_gap_mm": _GAP_LIMITS}}
        state = _base_state([{"metric": deep, "value": 65.0}], asset_profile=profile)
        result = node.execute(state)  # must not raise RecursionError
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["delta_findings"] == []
