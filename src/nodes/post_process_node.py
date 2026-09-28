"""AgentCore Platform v1.0"""

# post_process — docs/02_design.md Node Configuration §post_process +
# §ユーザビリティ設計レビュー (優先度冒頭明示) + §(c) S-3 output gate design +
# §(d) 施設特定機微情報秘匿.
#
# Responsibilities:
#   1. Shape formatted_output per mode (advisory / ingest summary / feedback confirmation)
#   2. For /invoke: prepend an explicit priority heading (decoupled from prose
#      tone, since S-3 flattens assertive language into a uniform non-assertive
#      register)
#   3. S-3 extension (_extra_security_gate_output): assertive-language downgrade,
#      unsupported-claim stripping, fabricated-evidence-ID detection, mandatory
#      disclaimer + expert-consultation recommendation, facility-sensitive-info
#      redaction, generic client-echo sanitization.

from __future__ import annotations

import re
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

_PRIORITY_LABEL_JA = {
    "low": "軽微",
    "medium": "要注意",
    "high": "警戒",
    "critical": "重大（詳細調査・専門家確認を推奨）",
}

_PRIORITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}

# Assertive language -> non-assertive rewrite. re.IGNORECASE (combined with
# NFKC normalization of the subject text before matching) gives the
# "NFKC + casefold" bypass-proofing required by docs/02_design.md
# §セキュリティ設計方針 S-3 without permanently lowercasing the displayed
# narrative.
_ASSERTIVE_PATTERNS = [
    (re.compile(r"(確定です)", re.IGNORECASE), "の可能性が高いと考えられます"),
    (re.compile(r"(間違いありません)", re.IGNORECASE), "の可能性があります"),
    (re.compile(r"(区分\s*[IVⅠ-Ⅳivix]+)", re.IGNORECASE), "要専門家確認"),
    (re.compile(r"(です。$)", re.IGNORECASE), "と考えられます。"),
    (re.compile(r"\bwill\s+fail\b", re.IGNORECASE), "may be at risk of failing"),
    (re.compile(r"\bis\s+confirmed\b", re.IGNORECASE), "may be indicated"),
]

_DISCLAIMER_JA = (
    "\n\n【免責】本所見は非断定のドラフトであり、健全性の確定判定・署名は"
    "有資格技術者の責任です。重大な予兆が疑われる場合は、詳細調査・専門家確認を推奨します。"
)

# Agent-authored boilerplate that the per-sentence evidence-ID check must
# never strip. Matched by EXACT sentence equality (after NFKC + whitespace/
# delimiter trimming), never as a substring — a substring match would let an
# LLM smuggle an unsourced claim through by prefixing a protected phrase.
_PROTECTED_SENTENCES = frozenset(
    unicodedata.normalize("NFKC", s)
    for s in (
        "【免責】本所見は非断定のドラフトであり、健全性の確定判定・署名は"
        "有資格技術者の責任です",
        "重大な予兆が疑われる場合は、詳細調査・専門家確認を推奨します",
        "一般的な設備種別劣化知識に基づく注意喚起（詳細解釈は現在利用できません）:",
        "現時点で顕著な経年差分シグナルは検出されていません",
    )
)

_REDACTION_MARKER_INVALID_ID = "[要検証]"
_REDACTION_MARKER_NO_EVIDENCE = "[要検証：根拠となる経年差分/過去事例IDが確認できません]"
_REDACTION_MARKER_CONTROL = "[要検証：制御指示は本AGの対象外]"
_REDACTION_MARKER_FIELD_ID = "[検証不能な根拠ID表記のためマスク]"
_REDACTION_MARKER_CREDENTIAL = "[検証不能な認証情報様文字列のためマスク]"
_REDACTION_MARKER_SENSITIVE_LOCATION = "[施設特定機微情報のためマスク]"

# 施設特定機微情報（docs/02_design.md §(d)）: 正確な位置座標（緯度経度）・
# 詳細な住所番地。抽象化された資産種別・重要度・進行傾向のみを出力に残す。
_LAT_LON_PATTERN = re.compile(
    r"[-+]?\d{1,3}\.\d{3,}\s*[,、]\s*[-+]?\d{1,3}\.\d{3,}"
)

_ANTHROPIC_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")

_EVIDENCE_ID_PATTERN = re.compile(
    r"(?<![0-9A-Za-z-])(fnd-[0-9A-Za-z_-]+|inv-[0-9A-Za-z_-]+)(?![0-9A-Za-z-])"
)

_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[。！？\n])")

_NON_CLAIM_FRAGMENT_PATTERN = re.compile(
    r"""^\s*
    (?:
        [#＃]+\s*.*
      | [-*・]\s*[^。！？]{0,20}$
      | 【[^】]*】\s*$
      | [^。！？\n]{0,20}[:：]\s*$
    )
    \s*$""",
    re.VERBOSE,
)

_CONTROL_ACTION_PATTERNS = [
    (re.compile(r"直ちに[^。！？\n]*?(?:してください|すること)", re.IGNORECASE), _REDACTION_MARKER_CONTROL),
    (re.compile(r"[^。！？\n]*?を(?:稼働|停止|操作)してください", re.IGNORECASE), _REDACTION_MARKER_CONTROL),
]

_MAX_FIELD_CHARS = 300


def _truncate(raw: Any, limit: int = _MAX_FIELD_CHARS) -> Any:
    if not isinstance(raw, str) or len(raw) <= limit:
        return raw
    return raw[:limit] + f"...[truncated, {len(raw)} chars]"


# ── allowlist-based generic sanitizer over the whole formatted_output tree ──
_SANITIZE_EXEMPT_KEYS = frozenset(
    {
        "narrative",
        "priority",
        "priority_label_ja",
        "finding_id",
        "cited_finding_ids",
        "cited_intervention_ids",
        "intervention_ids",
        "intervention_id",
        "reason",
        "degradation_reason",
        "degradation_notice_ja",
    }
)

_SANITIZE_MAX_DEPTH = 8
_DEPTH_TRUNCATED_MARKER = "[構造が深すぎるため打ち切り]"


def _sanitize_scalar_string(value: str) -> str:
    sanitized = _EVIDENCE_ID_PATTERN.sub(_REDACTION_MARKER_FIELD_ID, value)
    sanitized = _LAT_LON_PATTERN.sub(_REDACTION_MARKER_SENSITIVE_LOCATION, sanitized)
    for pattern, replacement in _CONTROL_ACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    for finding in detect_credentials(sanitized):
        sanitized = sanitized.replace(finding["value"], _REDACTION_MARKER_CREDENTIAL)
    sanitized = _ANTHROPIC_KEY_PATTERN.sub(_REDACTION_MARKER_CREDENTIAL, sanitized)
    return _truncate(sanitized)


def _sanitize_client_echoes(node: Any, *, exempt: bool = False, _depth: int = 0) -> Any:
    """Recursively sanitize every string in `node` not reached via an exempt
    key. MUST NEVER raise RecursionError: `_depth` bounds the walk.
    """
    if _depth >= _SANITIZE_MAX_DEPTH and isinstance(node, (dict, list)):
        return _DEPTH_TRUNCATED_MARKER
    if isinstance(node, dict):
        return {
            key: _sanitize_client_echoes(
                val, exempt=exempt or key in _SANITIZE_EXEMPT_KEYS, _depth=_depth + 1
            )
            for key, val in node.items()
        }
    if isinstance(node, list):
        return [_sanitize_client_echoes(item, exempt=exempt, _depth=_depth + 1) for item in node]
    if isinstance(node, str):
        return node if exempt else _sanitize_scalar_string(node)
    return node


def _select_highest_priority(findings: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """SAFETY-CRITICAL ordering: highest severity/progression_rate first,
    never a naive first-N slice (mirrors AquacultureEnvironmentalEarlyWarningAgent precedent — a critical
    finding must never silently drop from the operator-visible index).
    """
    if len(findings) <= limit:
        return findings

    def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, float]:
        _index, f = item
        rank = _PRIORITY_RANK.get(f.get("severity"), -1)
        progression = f.get("progression_rate")
        magnitude = abs(progression) if isinstance(progression, (int, float)) else 0.0
        return (rank, magnitude)

    indexed = list(enumerate(findings))
    indexed.sort(key=_sort_key, reverse=True)
    kept_indices = {index for index, _f in indexed[:limit]}
    return [f for index, f in enumerate(findings) if index in kept_indices]


_MAX_FINDING_INDEX_RECORDED = 50


class PostProcessNode(FunctionNode):
    """Format and finalize output; S-3 output gate is the primary safety net
    for the non-assertive / provenance-verified advisory contract.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, asset_store=None) -> None:
        # Constructor injection seam (fake in tests, real store wired by
        # graph.py <- server.py wire_services()). None => advisory
        # persistence is skipped gracefully (e.g. minimal PB scans that
        # instantiate the node with no collaborators).
        super().__init__()
        self._asset_store = asset_store

    def execute(self, state: dict) -> dict:
        mode = state.get("mode", "")
        formatted: dict[str, Any]

        if mode == "invoke":
            formatted = self._format_advisory(state)
        elif mode == "ingest":
            formatted = self._format_ingest_summary(state)
        elif mode == "feedback":
            formatted = self._format_feedback_result(state)
        else:
            formatted = {"summary": "unknown mode", "mode": mode}

        emit_trace_event("post_process_formatted", {"mode": mode}, state)

        # 🔴 AgentBaseGraph.get_output() (framework default) only surfaces
        # state["formatted_output"]/["result"] to the caller as `output` — it
        # does NOT read state["degradation_reason"]. A degradation_reason set
        # by an inner node would otherwise silently vanish from the
        # caller-visible response even though it is present in State/the
        # checkpoint. Fold it into formatted_output itself (with an explicit
        # Japanese framing) so the縮退した旨 always reaches the operator
        # (docs/02_design.md §(a)/(b) 縮退設計・§ユーザビリティ設計レビュー
        # エラー/フォールバック面).
        if state.get("degradation_reason"):
            formatted = {**formatted, "degradation_reason": state["degradation_reason"]}
            formatted["degradation_notice_ja"] = self._degradation_notice(state["degradation_reason"])

        # Apply the S-3 output gate explicitly HERE (not only in the @final
        # hook) so the sanitized advisory is available for persistence below.
        # The @final gate hook re-runs the same idempotent pipeline as
        # defense-in-depth — see _extra_security_gate_output. The persisted
        # advisory must be the
        # S-3-sanitized form (S-3通過後), which only exists after this gate;
        # persisting from the gate hook itself would be a side effect inside
        # a security gate AND the hook has no access to org_id/asset_id
        # (it only receives the execute() result dict); persisting from the
        # server adapter would violate "entry points are adapters only".
        formatted = self._apply_s3_output_gate(formatted)

        # Persist the sanitized advisory so /feedback's
        # reconcile_prediction can compare it against the actual intervention
        # outcome. Normal (non-degraded) /invoke success only — a degraded
        # advisory (LLM欠落/データ薄/プロファイル未整備) is not a prediction
        # worth reconciling against.
        if (
            mode == "invoke"
            and self._asset_store is not None
            and not state.get("degradation_reason")
            and state.get("org_id")
            and state.get("asset_id")
        ):
            try:
                self._asset_store.record_advisory(state["org_id"], state["asset_id"], formatted)
                emit_trace_event(
                    "advisory_recorded",
                    {"org_id": state["org_id"], "asset_id": state["asset_id"]},
                    state,
                )
            except Exception as exc:  # noqa: BLE001 — 記録失敗はadvisory提示自体を落とさない
                emit_trace_event("advisory_record_failed", {"error": str(exc)}, state)

        result: dict[str, Any] = {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }
        if state.get("degradation_reason"):
            result["degradation_reason"] = state["degradation_reason"]
        return result

    @staticmethod
    def _degradation_notice(degradation_reason: str) -> str:
        notices = []
        for reason in degradation_reason.split("+"):
            if reason == "llm_unavailable":
                notices.append("LLMが利用できないため決定論的な簡易所見に縮退しています。")
            elif reason == "insufficient_inspection_history":
                notices.append("点検・補修履歴が最小データ要件（点検2回以上×補修履歴1件以上）に達していないため、自組織照合を実施していません。")
            elif reason == "asset_profile_not_found":
                notices.append("この設備種別の設備プロファイルが未整備のため、種別固有のアラーム閾値・劣化モード照合をスキップしています。")
            else:
                notices.append(f"縮退動作: {reason}")
        return " ".join(notices)

    # ── mode-specific formatting ─────────────────────────────────────────

    def _format_advisory(self, state: dict) -> dict[str, Any]:
        advisory = state.get("advisory_output", {}) or {}
        delta_findings = state.get("delta_findings", []) or []
        priority = self._derive_display_priority(advisory, delta_findings)

        narrative = advisory.get("narrative", "")

        formatted: dict[str, Any] = {
            # 優先度を構造化見出しとして冒頭に明示（docs/02_design.md
            # §ユーザビリティ設計レビュー — "非断定" と "優先度が伝わらない" は別問題）。
            "priority": priority,
            "priority_label_ja": _PRIORITY_LABEL_JA.get(priority, priority),
            "narrative": narrative,
            "cited_finding_ids": advisory.get("cited_finding_ids", []),
            "cited_intervention_ids": advisory.get("cited_intervention_ids", []),
        }

        eligible = [f for f in delta_findings if f.get("finding_id")]
        total_finding_count = len(eligible)
        kept = _select_highest_priority(eligible, _MAX_FINDING_INDEX_RECORDED)
        finding_index = [
            {
                "finding_id": f["finding_id"],
                "metric": _truncate(f.get("metric")),
                "delta": f.get("delta"),
                "progression_rate": f.get("progression_rate"),
                "severity": f.get("severity"),
            }
            for f in kept
        ]
        if finding_index:
            formatted["finding_index"] = finding_index
            formatted["finding_index_total_count"] = total_finding_count
            if total_finding_count > len(finding_index):
                formatted["finding_index_truncated"] = True

        if "delta_findings_total_count" in state:
            formatted["delta_findings_total_count"] = state["delta_findings_total_count"]
            if state.get("delta_findings_truncated"):
                formatted["delta_findings_truncated"] = True
        if "candidate_cases_total_count" in state:
            formatted["candidate_cases_total_count"] = state["candidate_cases_total_count"]
            if state.get("candidate_cases_truncated"):
                formatted["candidate_cases_truncated"] = True

        return formatted

    @staticmethod
    def _format_ingest_summary(state: dict) -> dict[str, Any]:
        summary = state.get("ingest_summary", {}) or {}
        return {"ingest_summary": summary}

    @staticmethod
    def _format_feedback_result(state: dict) -> dict[str, Any]:
        result = state.get("feedback_result", {}) or {}
        return {"feedback_result": result}

    @staticmethod
    def _derive_display_priority(advisory: dict[str, Any], delta_findings: list[dict[str, Any]]) -> str:
        if advisory.get("priority"):
            return advisory["priority"]
        levels = [f.get("severity") for f in delta_findings if f.get("severity")]
        order = ["low", "medium", "high", "critical"]
        ranked = [lv for lv in levels if lv in order]
        if not ranked:
            return "low"
        return max(ranked, key=order.index)

    # ── S-3 output gate extension ────────────────────────────────────────

    def _extra_security_gate_output(self, result: dict) -> dict:
        """docs/02_design.md §(c) S-3 output gate — thin wrapper over
        `_apply_s3_output_gate`. The full pipeline also runs explicitly at
        the end of execute() (so the S-3-sanitized advisory is available for
        record_advisory persistence — see execute()); this hook re-runs the
        SAME idempotent pipeline as defense-in-depth, so the gate remains
        non-bypassable regardless of what execute() returned (TC-07 / PB-6
        contract unchanged).
        """
        formatted = result.get("formatted_output")
        if not isinstance(formatted, dict):
            return result
        result["formatted_output"] = self._apply_s3_output_gate(formatted)
        return result

    @classmethod
    def _apply_s3_output_gate(cls, formatted: dict) -> dict:
        """docs/02_design.md §(c) S-3 output gate. Ordered steps:
          1) assertive-language downgrade
          2) fabricated evidence-ID detection
          3) per-sentence evidence-ID check + control-action suppression
          4) mandatory disclaimer + expert consultation for critical priority
          5) formatted_output-wide client-echo sanitization (evidence-ID
             lookalike masking + control-instruction suppression + credential
             masking + facility-sensitive-location masking + length bound)

        IDEMPOTENT by construction (verified by the test suite running it
        twice — once from execute(), once from the @final gate hook):
        assertive rewrites do not re-match their own replacements, redaction
        markers are protected/pattern-disjoint, and the disclaimer is
        appended only when absent.
        """
        formatted = _sanitize_client_echoes(formatted)

        if "narrative" not in formatted:
            return formatted

        narrative = formatted.get("narrative", "") or ""
        valid_ids = set(formatted.get("cited_finding_ids", []) or []) | set(
            formatted.get("cited_intervention_ids", []) or []
        )
        valid_ids.discard(None)
        valid_ids_cf = {str(v).casefold() for v in valid_ids}

        rewritten = unicodedata.normalize("NFKC", narrative)
        for pattern, replacement in _ASSERTIVE_PATTERNS:
            rewritten = pattern.sub(replacement, rewritten)

        # 🔴 narrative is exempt from the generic _sanitize_client_echoes pass
        # (own per-sentence provenance pipeline below), but that means
        # location/credential masking must be applied to it explicitly here —
        # otherwise an LLM- or fallback-generated narrative could leak facility
        # GPS coordinates or a credential-shaped token even while correctly
        # citing a valid evidence ID (docs/02_design.md §(d) 施設特定機微情報秘匿,
        # §セキュリティ設計方針 S-3). Order-safe: lat/lon and credential patterns
        # are disjoint from the fnd-/inv- evidence-ID grammar, so masking them
        # first cannot corrupt the citation check that follows.
        rewritten = _LAT_LON_PATTERN.sub(_REDACTION_MARKER_SENSITIVE_LOCATION, rewritten)
        for finding in detect_credentials(rewritten):
            rewritten = rewritten.replace(finding["value"], _REDACTION_MARKER_CREDENTIAL)
        rewritten = _ANTHROPIC_KEY_PATTERN.sub(_REDACTION_MARKER_CREDENTIAL, rewritten)

        cited_in_text = set(_EVIDENCE_ID_PATTERN.findall(rewritten.casefold()))
        fabricated = cited_in_text - valid_ids_cf
        for fake_id in fabricated:
            rewritten = re.sub(re.escape(fake_id), _REDACTION_MARKER_INVALID_ID, rewritten, flags=re.IGNORECASE)

        rewritten = cls._strip_unsupported_claims(rewritten, valid_ids_cf)

        formatted["narrative"] = rewritten

        priority = formatted.get("priority")
        if priority == "critical" and _DISCLAIMER_JA.strip() not in rewritten:
            formatted["narrative"] = rewritten + _DISCLAIMER_JA
        elif _DISCLAIMER_JA.strip() not in formatted["narrative"]:
            # 免責は重大度にかかわらず必ず付与する（docs/02_design.md §(c) 免責付与）。
            formatted["narrative"] = formatted["narrative"] + _DISCLAIMER_JA

        return formatted

    @staticmethod
    def _strip_unsupported_claims(narrative: str, valid_ids_cf: set[str]) -> str:
        sentences = _SENTENCE_SPLIT_PATTERN.split(narrative)
        out: list[str] = []
        for sentence in sentences:
            if not sentence.strip():
                out.append(sentence)
                continue

            bare = sentence.strip().rstrip("。！？\n").strip()
            if bare in _PROTECTED_SENTENCES or bare in (
                _REDACTION_MARKER_NO_EVIDENCE,
                _REDACTION_MARKER_CONTROL,
            ):
                out.append(sentence)
                continue

            if _NON_CLAIM_FRAGMENT_PATTERN.match(sentence):
                out.append(sentence)
                continue

            control_suppressed = sentence
            for pattern, replacement in _CONTROL_ACTION_PATTERNS:
                control_suppressed = pattern.sub(replacement, control_suppressed)
            if control_suppressed != sentence:
                out.append(control_suppressed)
                continue

            cited = {cid.casefold() for cid in _EVIDENCE_ID_PATTERN.findall(sentence)}
            if cited & valid_ids_cf:
                out.append(sentence)
            else:
                delimiter = sentence[len(sentence.rstrip("。！？\n")):]
                out.append(_REDACTION_MARKER_NO_EVIDENCE + delimiter)
        return "".join(out)
