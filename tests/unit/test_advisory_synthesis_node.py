# MFG-C2-091 — Unit Tests: AdvisorySynthesisNode (Stage 3, LLM + fallback)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.advisory_synthesis_node import AdvisorySynthesisNode


class _FakeLlm:
    """BaseLLM 契約に従う double: complete(messages: list) -> canonical dict。

    🔴 `.complete(prompt: str) -> str` を返す double にすると、本番が注入する
    `AnthropicClient`（`complete(messages) -> dict`）と形が違うまま全テストが
    緑になり、契約違反が実 LLM 1 回転でしか露見しない。
    """

    def __init__(self, response: str = "根拠に基づく所見です。"):
        self._response = response

    def complete(self, messages: list) -> dict:
        return {"content": self._response, "tool_calls": [], "model": "fake", "usage": {}}


class _FailingLlm:
    def complete(self, messages: list) -> dict:
        raise RuntimeError("upstream timeout")


class TestAdvisorySynthesisNode:
    def test_llm_none_falls_back_deterministically(self):
        node = AdvisorySynthesisNode(llm=None)
        findings = [{"finding_id": "fnd-1", "metric": "m", "severity": "critical"}]
        result = node.execute({"delta_findings": findings, "candidate_cases": []})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["degradation_reason"] == "llm_unavailable"
        assert "fnd-1" in result["advisory_output"]["narrative"]

    def test_priority_never_left_to_llm_judgment(self):
        node = AdvisorySynthesisNode(llm=_FakeLlm())
        findings = [{"finding_id": "fnd-1", "metric": "m", "severity": "critical"}]
        result = node.execute({"delta_findings": findings, "candidate_cases": []})
        assert result["advisory_output"]["priority"] == "critical"

    def test_empty_findings_and_cases_produce_low_priority(self):
        node = AdvisorySynthesisNode(llm=None)
        result = node.execute({"delta_findings": [], "candidate_cases": []})
        assert result["advisory_output"]["priority"] == "low"

    def test_llm_failure_degrades_to_deterministic_fallback(self):
        node = AdvisorySynthesisNode(llm=_FailingLlm())
        findings = [{"finding_id": "fnd-1", "metric": "m", "severity": "high"}]
        result = node.execute({"delta_findings": findings, "candidate_cases": []})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["degradation_reason"] == "llm_unavailable"

    def test_existing_degradation_reason_is_preserved_not_overwritten(self):
        """When Stage1 already flagged insufficient_inspection_history and the
        LLM IS available, that reason must survive (not be silently dropped)."""
        node = AdvisorySynthesisNode(llm=_FakeLlm())
        result = node.execute(
            {
                "delta_findings": [],
                "candidate_cases": [],
                "degradation_reason": "insufficient_inspection_history",
            }
        )
        assert result.get("degradation_reason") == "insufficient_inspection_history"

    def test_llm_unavailable_combines_with_existing_reason_not_overwritten(self):
        """Regression: a naive `existing or "llm_unavailable"`
        silently drops the fact that Stage3 ALSO degraded whenever an
        upstream stage already set a reason."""
        node = AdvisorySynthesisNode(llm=None)
        result = node.execute(
            {
                "delta_findings": [],
                "candidate_cases": [],
                "degradation_reason": "insufficient_inspection_history",
            }
        )
        assert "insufficient_inspection_history" in result["degradation_reason"]
        assert "llm_unavailable" in result["degradation_reason"]

    def test_llm_failure_combines_with_existing_reason_not_overwritten(self):
        # Findings are required for the LLM to be called at all: an empty
        # evidence set now short-circuits to the deterministic branch BEFORE
        # the provider is contacted (see
        # test_no_valid_evidence_skips_the_llm_without_claiming_it_is_down).
        node = AdvisorySynthesisNode(llm=_FailingLlm())
        result = node.execute(
            {
                "delta_findings": [{"finding_id": "fnd-1", "metric": "m", "severity": "high"}],
                "candidate_cases": [],
                "degradation_reason": "asset_profile_not_found",
            }
        )
        assert "asset_profile_not_found" in result["degradation_reason"]
        assert "llm_unavailable" in result["degradation_reason"]

    def test_no_valid_evidence_skips_the_llm_without_claiming_it_is_down(self):
        """An unsatisfiable prompt must not be sent, and must not be reported
        as a provider outage.

        With no findings and no candidate cases there is no evidence ID to
        cite, so the Stage-3 prompt would order the model to cite every
        sentence from `[]`. The model fabricates or writes uncitable prose and
        the S-3 gate redacts the whole narrative. Misreporting this as
        `llm_unavailable` would also mask a real provider outage on the same
        asset.
        """

        class _Recording:
            def __init__(self):
                self.calls = 0

            def complete(self, messages: list) -> dict:
                self.calls += 1
                return {"content": "x", "tool_calls": [], "model": "fake", "usage": {}}

        llm = _Recording()
        node = AdvisorySynthesisNode(llm=llm)
        result = node.execute({"delta_findings": [], "candidate_cases": []})
        assert llm.calls == 0, "証拠ゼロで LLM を呼んではならない（充足不能なプロンプト）"
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "llm_unavailable" not in (result.get("degradation_reason") or "")
        assert "顕著な経年差分シグナルは検出されていません" in result["advisory_output"]["narrative"]

    def test_cited_ids_collect_both_finding_and_intervention_sources(self):
        node = AdvisorySynthesisNode(llm=_FakeLlm())
        findings = [{"finding_id": "fnd-1", "metric": "m", "severity": "high"}]
        cases = [{"matched_intervention_id": "inv-1", "severity_baseline": "high"}]
        result = node.execute({"delta_findings": findings, "candidate_cases": cases})
        assert result["advisory_output"]["cited_finding_ids"] == ["fnd-1"]
        assert result["advisory_output"]["cited_intervention_ids"] == ["inv-1"]
        assert set(result["advisory_output"]["valid_evidence_ids"]) == {"fnd-1", "inv-1"}
