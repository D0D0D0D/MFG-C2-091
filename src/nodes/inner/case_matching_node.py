"""AgentCore Platform v1.0"""

# Stage 2 — 類似事例照合 (docs/02_design.md §経年所見の3段パイプライン).
# 検索（LLM 推論を介さない）: 自組織 interventions KB の類似条件検索
# (劣化モード・環境条件類似度による近傍探索) + 設備プロファイルの劣化モード
# 知識引き当て。
#
# 🔴 §(b) 最小データ要件: Stage1 が insufficient_history を検知した場合、
# 自組織 KB 対象から除外し空リストで縮退する（degradation_reason は
# Stage1/main で既に設定済みのためここでは上書きしない）。
#
# Runs inside the inner AssetWorkflowGraph (mode == "invoke" only).

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.asset_store import BaseAssetStore
from src.services.severity_cap import apply_severity_cap

_MAX_CANDIDATE_CASES = 200


class CaseMatchingNode(FunctionNode):
    """Stage 2: search-based (non-LLM) similar-case matching."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, asset_store: BaseAssetStore) -> None:
        super().__init__()
        self._store = asset_store

    def execute(self, state: dict) -> dict:
        org_id = state.get("org_id", "")
        asset_type = state.get("asset_type", "")
        asset_profile = state.get("asset_profile", {}) or {}
        delta_findings = state.get("delta_findings", []) or []

        candidates: list[dict[str, Any]] = []

        # §(b): データ薄の場合は自組織 KB 検索そのものをスキップ（degradation
        # は Stage1 が既に立てている — ここでは上書きしない）。
        # 🔴 Stage1 は複数の縮退理由を "+" 結合で返しうる
        # （例: "insufficient_inspection_history+asset_profile_not_found"）。
        # 完全一致比較だと組み合わせ時にこのスキップが働かなくなる回帰があった
        # ため、"+" 区切りの部分一致で判定する。
        existing_reasons = (state.get("degradation_reason") or "").split("+")
        insufficient_history = "insufficient_inspection_history" in existing_reasons
        if not insufficient_history and delta_findings:
            try:
                matched = self._store.query_similar_interventions(org_id, asset_type, delta_findings)
            except Exception as exc:  # noqa: BLE001 — store failure degrades, does not crash
                emit_trace_event("case_matching_store_failed", {"error": str(exc)}, state)
                matched = []
            for rec in matched:
                candidates.append(
                    {
                        "matched_intervention_id": rec.get("intervention_id"),
                        "matched_finding_id": None,
                        "action": rec.get("action"),
                        "category": rec.get("action"),
                        "severity_baseline": self._severity_from_record(rec),
                        "source": "org_intervention_history",
                    }
                )

        # 設備プロファイルの劣化モード知識引き当て（trigger_metrics が
        # delta_findings のいずれかの metric と一致するもの）。
        finding_metrics = {f.get("metric") for f in delta_findings}
        for mode_entry in asset_profile.get("degradation_modes", []) or []:
            trigger_metrics = set(mode_entry.get("trigger_metrics", []) or [])
            hit_findings = [f for f in delta_findings if f.get("metric") in trigger_metrics]
            if not (finding_metrics & trigger_metrics) or not hit_findings:
                continue
            # 一致した最初の finding を根拠として引用（複数一致時は最高severityを優先）。
            hit_findings.sort(key=lambda f: _SEVERITY_RANK.get(f.get("severity"), -1), reverse=True)
            candidates.append(
                {
                    "matched_intervention_id": None,
                    "matched_finding_id": hit_findings[0].get("finding_id"),
                    "action": None,
                    "category": mode_entry.get("mode_id"),
                    "severity_baseline": hit_findings[0].get("severity"),
                    "source": "asset_profile_degradation_mode",
                }
            )

        total_count = len(candidates)
        capped = apply_severity_cap(candidates, _MAX_CANDIDATE_CASES, severity_key="severity_baseline")

        emit_trace_event(
            "stage2_case_matching",
            {"org_id": org_id, "candidate_count": len(capped), "candidate_count_total": total_count},
            state,
        )

        result: dict[str, Any] = {
            "candidate_cases": capped,
            "candidate_cases_total_count": total_count,
            "status": AgentStatus.SUCCESS.value,
        }
        if total_count > len(capped):
            result["candidate_cases_truncated"] = True
        return result

    @staticmethod
    def _severity_from_record(rec: dict[str, Any]) -> str | None:
        # 補修履歴 record は severity を直接持たないことがあるため、action の
        # 種別から保守的にベースラインを推定する簡易ヒューリスティック。
        # 実プロファイルの整備に応じて精緻化できる。
        action = rec.get("action")
        if action == "replace":
            return "high"
        if action in ("repair", "detailed_investigation"):
            return "medium"
        return "low"

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state


_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}
