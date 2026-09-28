"""AgentCore Platform v1.0"""

# Stage 3 — 統合解釈 (docs/02_design.md §経年所見の3段パイプライン).
# THE ONLY LLM STEP in the /invoke pipeline. Synthesizes Stage 1
# delta_findings + Stage 2 candidate_cases into a non-assertive advisory
# with priority.
#
# Injection seam: the LLM is injected
# via constructor (config["llm"]); no model ID is hardcoded here. When llm is
# None, falls back to a deterministic advisory built from Stage1/2 data only
# — status stays "success", degradation_reason="llm_unavailable"
# (docs/02_design.md §🔴 status の有効値 — "degraded" must never be assigned
# to status).
#
# priority is derived deterministically from delta_findings/candidate_cases —
# never left to LLM judgment (docs/02_design.md §ユーザビリティ設計レビュー).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.llm_text import complete_text

_PRIORITY_ORDER = ["low", "medium", "high", "critical"]


class AdvisorySynthesisNode(FunctionNode):
    """Stage 3: LLM-based synthesis of the non-assertive aging advisory.

    `llm` follows the BaseLLM interface (shared/services/llm/) — duck-typed
    here (constructor injection only, no import) so this node stays
    decoupled from the concrete LLM client wiring. See src/api/server.py
    wire_services() for how the real client is constructed and injected;
    `llm` MUST honour the BaseLLM wire contract
    (`complete(messages: list) -> dict`); all calls go through
    `src/services/llm_text.complete_text()`, which enforces it.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any | None = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        delta_findings: list[dict[str, Any]] = state.get("delta_findings", []) or []
        candidate_cases: list[dict[str, Any]] = state.get("candidate_cases", []) or []

        priority = self._derive_priority(delta_findings, candidate_cases)
        valid_ids = self._collect_valid_ids(delta_findings, candidate_cases)

        # データ薄で Stage1/2 の対象が空でも、資産一般の注意喚起を返す
        # （degradation_reason は既存のものを保全 — §(b) の縮退契約）。
        existing_degradation = state.get("degradation_reason")

        if self._llm is None:
            advisory = self._deterministic_fallback(delta_findings, candidate_cases, priority)
            emit_trace_event(
                "stage3_llm_unavailable_fallback",
                {"finding_count": len(delta_findings), "candidate_count": len(candidate_cases)},
                state,
            )
            result: dict[str, Any] = {
                "advisory_output": advisory,
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": self._combine_degradation_reasons(existing_degradation, "llm_unavailable"),
            }
            return result

        if not valid_ids:
            # 🔴 An empty evidence set makes the Stage-3 prompt unsatisfiable:
            # it would order the model to cite every sentence from `[]`. The
            # model then either fabricates an ID or writes uncitable prose, and
            # the S-3 provenance gate redacts the whole narrative either way.
            # Take the deterministic branch — it already emits the protected
            # "顕著な経年差分シグナルは検出されていません" sentence for this case.
            #
            # This is NOT an llm_unavailable degradation: the LLM is healthy,
            # there is simply nothing for it to ground a claim on. Reporting it
            # as llm_unavailable would misattribute the cause to the provider
            # and mask a genuine provider outage on the same asset.
            advisory = self._deterministic_fallback(delta_findings, candidate_cases, priority)
            emit_trace_event(
                "stage3_no_valid_evidence_deterministic",
                {"finding_count": len(delta_findings), "candidate_count": len(candidate_cases)},
                state,
            )
            no_evidence_result: dict[str, Any] = {
                "advisory_output": advisory,
                "status": AgentStatus.SUCCESS.value,
            }
            if existing_degradation:
                no_evidence_result["degradation_reason"] = existing_degradation
            return no_evidence_result

        prompt = self._build_prompt(delta_findings, candidate_cases, priority, valid_ids)
        try:
            raw_text = complete_text(self._llm, prompt)
        except Exception as exc:  # noqa: BLE001 — LLM failure degrades, does not crash the pipeline
            emit_trace_event("stage3_llm_call_failed", {"error": str(exc)}, state)
            advisory = self._deterministic_fallback(delta_findings, candidate_cases, priority)
            return {
                "advisory_output": advisory,
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": self._combine_degradation_reasons(existing_degradation, "llm_unavailable"),
            }

        advisory = {
            "priority": priority,
            "narrative": raw_text,
            "cited_finding_ids": [f.get("finding_id") for f in delta_findings],
            "cited_intervention_ids": [
                c.get("matched_intervention_id") for c in candidate_cases if c.get("matched_intervention_id")
            ],
            "valid_evidence_ids": sorted(valid_ids),
        }

        emit_trace_event(
            "stage3_advisory_synthesized",
            {"priority": priority, "finding_count": len(delta_findings)},
            state,
        )

        result = {"advisory_output": advisory, "status": AgentStatus.SUCCESS.value}
        if existing_degradation:
            result["degradation_reason"] = existing_degradation
        return result

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _derive_priority(
        delta_findings: list[dict[str, Any]], candidate_cases: list[dict[str, Any]]
    ) -> str:
        """Deterministic priority derivation — never delegated to LLM
        judgment (docs/02_design.md §(c): 重要度×進行速度の緊急度シグナル
        は Stage1 が抽出済み。ここでは severity の最大値を優先度とする)。
        """
        levels = []
        for f in delta_findings:
            sev = f.get("severity")
            if sev in _PRIORITY_ORDER:
                levels.append(sev)
        for c in candidate_cases:
            sev = c.get("severity_baseline")
            if sev in _PRIORITY_ORDER:
                levels.append(sev)
        if not levels:
            return "low"
        return max(levels, key=lambda s: _PRIORITY_ORDER.index(s))

    @staticmethod
    def _collect_valid_ids(
        delta_findings: list[dict[str, Any]], candidate_cases: list[dict[str, Any]]
    ) -> set[str]:
        ids = {f.get("finding_id") for f in delta_findings if f.get("finding_id")}
        ids |= {c.get("matched_intervention_id") for c in candidate_cases if c.get("matched_intervention_id")}
        ids |= {c.get("matched_finding_id") for c in candidate_cases if c.get("matched_finding_id")}
        return ids

    @staticmethod
    def _combine_degradation_reasons(existing: str | None, new_reason: str) -> str:
        """Combine a pre-existing degradation_reason (possibly itself already
        "+"-joined by an earlier stage) with a new one from THIS stage,
        without silently dropping either. Regression: a naive
        `existing or new_reason` erases `llm_unavailable` whenever an
        upstream stage (e.g. Stage1 insufficient_inspection_history) already
        set a reason — losing the fact that Stage3 ALSO degraded.
        """
        if not existing:
            return new_reason
        reasons = existing.split("+")
        if new_reason not in reasons:
            reasons.append(new_reason)
        return "+".join(reasons)

    @staticmethod
    def _build_prompt(
        delta_findings: list[dict[str, Any]],
        candidate_cases: list[dict[str, Any]],
        priority: str,
        valid_ids: set[str],
    ) -> str:
        return (
            "You are an infrastructure/equipment asset aging-trend advisory writer.\n"
            "Rules:\n"
            "1. You may ONLY reference risks present in the provided finding/candidate data below.\n"
            # 🔴 The S-3 provenance gate in post_process_node.py enforces this
            # PER SENTENCE (_strip_unsupported_claims splits on 。！？\n and
            # redacts any sentence carrying no valid evidence ID). Saying only
            # "every claim" made a real model cite once per paragraph, so the
            # gate redacted most of an otherwise-correct advisory into
            # "[要検証：...]" markers (live LLM check, 2026-08-20). The prompt
            # must state the granularity the gate actually applies.
            "2. Every SENTENCE MUST carry a cited evidence ID in square brackets, "
            "taken verbatim from this exact set: "
            f"{sorted(valid_ids)}\n"
            "   Place the bracket INSIDE the sentence, immediately BEFORE the closing "
            "'。' — write '～の可能性があります[ID]。', NOT '～の可能性があります。[ID]'. "
            "An ID placed after the '。' belongs to the next sentence, leaving the "
            "sentence it was meant to support uncited.\n"
            "   A sentence with no ID inside it is removed before the reader sees it, "
            "so do not write linking or summarising sentences without one. "
            "Repeat the relevant ID in each sentence rather than citing once per "
            "paragraph. Never invent an ID outside the set above.\n"
            "3. Use non-assertive language (e.g. '~の可能性があります'), never assert a definitive "
            "health classification (e.g. '区分II') as certain.\n"
            "4. Do not issue automated control actions or a final health determination — that is "
            "the responsibility of a qualified engineer.\n\n"
            f"Derived priority (do not override): {priority}\n"
            f"Stage 1 findings: {delta_findings}\n"
            f"Stage 2 candidate cases: {candidate_cases}\n"
        )

    @staticmethod
    def _deterministic_fallback(
        delta_findings: list[dict[str, Any]], candidate_cases: list[dict[str, Any]], priority: str
    ) -> dict[str, Any]:
        lines = ["一般的な設備種別劣化知識に基づく注意喚起（詳細解釈は現在利用できません）:"]
        for f in delta_findings:
            lines.append(f"- {f.get('metric')} が {f.get('severity')} 水準の経年差分を示しています（根拠: {f.get('finding_id')}）")
        for c in candidate_cases:
            evidence = c.get("matched_intervention_id") or c.get("matched_finding_id")
            lines.append(f"- {c.get('category')} の可能性があります（根拠: {evidence}）")
        if len(lines) == 1:
            lines.append("現時点で顕著な経年差分シグナルは検出されていません。")
        return {
            "priority": priority,
            "narrative": "\n".join(lines),
            "cited_finding_ids": [f.get("finding_id") for f in delta_findings],
            "cited_intervention_ids": [
                c.get("matched_intervention_id") for c in candidate_cases if c.get("matched_intervention_id")
            ],
            "valid_evidence_ids": sorted(
                AdvisorySynthesisNode._collect_valid_ids(delta_findings, candidate_cases)
            ),
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
