"""AgentCore Platform v1.0"""

# Stage 1 — 経年差分検知 (docs/02_design.md §経年所見の3段パイプライン).
# DETERMINISTIC / NON-LLM: 同一資産の直近点検 vs 過去点検の構造化所見・計測値
# 差分検出 + 設備プロファイルのアラーム閾値照合 (OPC UA LimitAlarmType 準拠,
# §(a)-1) + 重要度×進行速度の緊急度シグナル抽出. Same input -> same output,
# unit-testable.
#
# Runs inside the inner AssetWorkflowGraph (mode == "invoke" only).

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.asset_store import BaseAssetStore
from src.services.severity_cap import apply_severity_cap

# 🔴 docs/02_design.md §(b) 最小データ要件: 同一資産につき点検記録2回以上 ×
# 補修履歴(interventions) 1件以上。満たさない場合は Stage2 自組織 KB 対象から
# 除外し Stage3 は縮退する。
_MIN_INSPECTIONS = 2
_MIN_INTERVENTIONS = 1

# Evidence-ID grammar. Must stay in sync with the ASCII-anchored extractor in
# post_process_node._EVIDENCE_ID_PATTERN.
_ID_SAFE_PATTERN = re.compile(r"[^0-9A-Za-z_]")
_ID_COMPONENT_MAX_CHARS = 16

_MAX_FIELD_CHARS = 200
_MAX_REJECTED_RECORDED = 50

# Bound on delta_findings carried past Stage 1 (mirrors AquacultureEnvironmentalEarlyWarningAgent's
# _MAX_SIGNALS rationale — bound state/response size while never dropping
# the highest-severity finding from view; see apply_severity_cap).
_MAX_FINDINGS = 500


def _truncate(raw: Any, limit: int = _MAX_FIELD_CHARS) -> Any:
    if not isinstance(raw, str) or len(raw) <= limit:
        return raw
    return raw[:limit] + f"...[truncated, {len(raw)} chars]"


def _safe_field_repr(raw: Any) -> Any:
    if raw is None or isinstance(raw, (str, int, float, bool)):
        return _truncate(raw)
    return f"<{type(raw).__name__}, rejected: not a scalar>"


class SignalDetectionNode(FunctionNode):
    """Stage 1: produce a deterministic list of aging-delta findings."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, asset_store: BaseAssetStore) -> None:
        super().__init__()
        self._store = asset_store

    def execute(self, state: dict) -> dict:
        org_id = state.get("org_id", "")
        asset_id = state.get("asset_id", "")
        asset_profile = state.get("asset_profile", {}) or {}
        payload = state.get("payload", {}) or {}
        current_inspection: dict[str, Any] = payload.get("current_inspection", {}) or {}
        measurements: list[dict[str, Any]] = current_inspection.get("measurements", []) or []

        asset_history = state.get("asset_history", {}) or {}
        past_inspections = asset_history.get("inspections", [])
        past_interventions = asset_history.get("interventions", [])

        # 🔴 §(b) 最小データ要件チェック。満たさない場合は縮退マーカーを立てる
        # （Stage2/3 が参照して自組織照合から除外・一般知識ベースへ縮退）。
        insufficient_history = (
            len(past_inspections) < _MIN_INSPECTIONS or len(past_interventions) < _MIN_INTERVENTIONS
        )

        findings: list[dict[str, Any]] = []
        unparseable: list[dict[str, Any]] = []
        rejected_total = 0

        def _reject(m: dict[str, Any], reason: str) -> None:
            nonlocal rejected_total
            rejected_total += 1
            if len(unparseable) >= _MAX_REJECTED_RECORDED:
                return
            unparseable.append(
                {
                    "metric": _safe_field_repr(m.get("metric")),
                    "value": _truncate(repr(m.get("value"))),
                    "reason": reason,
                }
            )

        metric_profiles = (asset_profile.get("metrics") or {})

        for m in measurements:
            metric = m.get("metric")
            raw_value = m.get("value")

            if not metric:
                _reject(m, "missing_metric")
                continue
            if raw_value is None:
                _reject(m, "missing_value")
                continue
            if not isinstance(metric, str):
                _reject(m, "malformed_field_type")
                continue

            value = self._coerce_value(raw_value)
            if value is None:
                _reject(m, "unreadable_value")
                continue

            metric_profile = metric_profiles.get(metric)
            if not metric_profile:
                _reject(m, "metric_not_in_asset_profile")

            range_severity = self._range_severity(value, metric_profile)

            prior_values = self._prior_metric_values(past_inspections, metric)
            delta, progression_rate, deviation_severity = self._progression(value, prior_values)

            severity = self._max_severity(range_severity, deviation_severity)
            if severity is None:
                continue

            inspected_at = current_inspection.get("inspected_at", "")
            finding_id = self._build_finding_id(asset_id, metric, inspected_at)
            findings.append(
                {
                    "finding_id": finding_id,
                    "metric": metric,
                    "value": value,
                    "delta": delta,
                    "progression_rate": progression_rate,
                    "severity": severity,
                    "source": "range_and_history",
                }
            )

        total_finding_count = len(findings)
        capped_findings = self._apply_cap(findings)

        emit_trace_event(
            "stage1_signal_detection",
            {
                "org_id": org_id,
                "asset_id": asset_id,
                "finding_count": len(capped_findings),
                "finding_count_total": total_finding_count,
                "unparseable_measurement_count": rejected_total,
                "insufficient_history": insufficient_history,
            },
            state,
        )

        result: dict[str, Any] = {
            "delta_findings": capped_findings,
            "delta_findings_total_count": total_finding_count,
            "status": AgentStatus.SUCCESS.value,
        }
        if total_finding_count > len(capped_findings):
            result["delta_findings_truncated"] = True
        # Both conditions can hold simultaneously (unknown asset_type AND a
        # thin history). Neither reason may silently overwrite the other —
        # combine them so post_process/the operator sees both causes rather
        # than losing one to whichever check ran last.
        reasons = []
        if insufficient_history:
            reasons.append("insufficient_inspection_history")
        if not metric_profiles and not asset_profile:
            reasons.append("asset_profile_not_found")
        if reasons:
            result["degradation_reason"] = "+".join(reasons)
        return result

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _id_component(raw: Any) -> str:
        text = "" if raw is None else str(raw)
        return _ID_SAFE_PATTERN.sub("_", text)[:_ID_COMPONENT_MAX_CHARS]

    @staticmethod
    def _injective_digest(*raw_parts: Any) -> str:
        buf = bytearray()
        for part in raw_parts:
            if part is None:
                buf += b"N"
            else:
                text = str(part).encode("utf-8")
                buf += b"S" + str(len(text)).encode("ascii") + b"\x00" + text
        return hashlib.sha256(bytes(buf)).hexdigest()

    @classmethod
    def _build_finding_id(cls, asset_id: Any, metric: Any, inspected_at: Any) -> str:
        """Deterministic, ASCII-safe, INJECTIVE evidence ID:
        `fnd-{asset_id}-{metric}-{inspected_at}` (docs/02_design.md §Stage 1),
        sanitized per §(a)-1's `finding_id` grammar constraint.
        """
        readable = [cls._id_component(asset_id), cls._id_component(metric), cls._id_component(inspected_at)]
        digest = cls._injective_digest(asset_id, metric, inspected_at)
        return "fnd-" + "-".join(readable) + "-" + digest

    @staticmethod
    def _coerce_value(raw: Any) -> float | None:
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            value = float(raw)
        elif isinstance(raw, str):
            try:
                value = float(raw.strip())
            except ValueError:
                return None
        else:
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _range_severity(value: float, metric_profile: dict[str, Any] | None) -> str | None:
        """Score against §(a)-1's OPC UA LimitAlarmType scalar thresholds
        (low_low_limit <= low_limit <= high_limit <= high_high_limit, all
        optional). Comparisons are STRICT: a reading exactly equal to a
        limit is not a signal.
        """
        if not metric_profile:
            return None
        low_low = metric_profile.get("low_low_limit")
        low = metric_profile.get("low_limit")
        high = metric_profile.get("high_limit")
        high_high = metric_profile.get("high_high_limit")

        if low_low is not None and value < low_low:
            return "critical"
        if high_high is not None and value > high_high:
            return "critical"
        if low is not None and value < low:
            return "medium"
        if high is not None and value > high:
            return "medium"
        return None

    @staticmethod
    def _prior_metric_values(past_inspections: list[dict[str, Any]], metric: str) -> list[tuple[str, float]]:
        """Extract (inspected_at, value) pairs for `metric` from past
        inspections' `measurements`, ordered as received (asset_store
        contract: query_inspection_history returns oldest->newest).
        """
        out: list[tuple[str, float]] = []
        for insp in past_inspections:
            for m in insp.get("measurements", []) or []:
                if m.get("metric") != metric:
                    continue
                raw = m.get("value")
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                if not math.isfinite(raw):
                    continue
                out.append((insp.get("inspected_at", ""), float(raw)))
        return out

    @staticmethod
    def _progression(
        value: float, prior_values: list[tuple[str, float]]
    ) -> tuple[float | None, float | None, str | None]:
        """Progression速度 = 直近点検との差分 / 経過点検回数. Requires at
        least one prior reading; returns (None, None, None) otherwise
        (handled by the §(b) min-data-requirement gate at the caller level,
        not silently scored as "no signal").
        """
        if not prior_values:
            return None, None, None
        _, last_value = prior_values[-1]
        delta = value - last_value
        progression_rate = delta / len(prior_values)
        magnitude = abs(progression_rate)
        if magnitude >= 5.0:
            return round(delta, 4), round(progression_rate, 4), "high"
        if magnitude >= 1.0:
            return round(delta, 4), round(progression_rate, 4), "medium"
        return round(delta, 4), round(progression_rate, 4), None

    @staticmethod
    def _max_severity(a: str | None, b: str | None) -> str | None:
        order = {"medium": 1, "high": 2, "critical": 3}
        candidates = [s for s in (a, b) if s]
        if not candidates:
            return None
        return max(candidates, key=lambda s: order.get(s, 0))

    @staticmethod
    def _apply_cap(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        def _magnitude(f: dict[str, Any]) -> float:
            rate = f.get("progression_rate")
            return abs(rate) if isinstance(rate, (int, float)) else 0.0

        return apply_severity_cap(findings, _MAX_FINDINGS, severity_key="severity", magnitude_fn=_magnitude)

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
