"""AgentCore Platform v1.0"""

# pre_process — docs/02_design.md Node Configuration §pre_process:
#   - input validation, org_id resolution (TENANT FAIL-CLOSED)
#   - asset_type -> asset_profile resolution (未知 asset_type は空 dict で縮退,
#     決して fail-closed にしない — docs/02_design.md §(a))
#   - /invoke only: 対象 asset_id の過去 inspections/interventions 履歴ロード
#   - S-2 input gate extension: org_id format / payload size / PII (点検者氏名) strip
#
# Node contract (sdk/concepts/node-contract.md): execute(state) -> dict,
# return only changed keys, status is an AgentStatus string value.

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.asset_profile_service import AssetProfileService
from src.services.asset_store import BaseAssetStore

_VALID_MODES = {"ingest", "invoke", "feedback"}

# docs/02_design.md §データモデル概要: "PII 該当は点検者氏名程度で S-2 で除去する".
# A lightweight honorific-suffix pattern (氏名 + 様/さん/君/氏) covers the
# realistic PII surface in free-text inspection fields, mirroring the
# AquacultureEnvironmentalEarlyWarningAgent precedent (a lightweight PII gate for a Cat 2 template —
# a full Japanese NER pipeline is out of scope here).
_INSPECTOR_NAME_PII_PATTERN = re.compile(r"[぀-ヿ一-鿿]{2,4}(?:様|さん|君|氏)")
_FREE_TEXT_PAYLOAD_FIELDS = ("free_note", "outcome_narrative")

# S-2 payload size guard (docs/02_design.md §セキュリティ設計方針: "payload
# サイズ上限"). Bounds the raw user_input string before it is even parsed.
_MAX_PAYLOAD_CHARS = 2_000_000


class PreProcessNode(FunctionNode):
    """Validate input, resolve tenant (org_id, fail-closed), select asset profile,
    and (for /invoke) load the target asset's inspection/intervention history.
    """

    # /ingest と /feedback は書込副作用を持つ。/invoke は読み取り主体だが
    # 組織固有の点検履歴という機微情報を扱うため一律 VERIFIED_EXTERNAL
    # （docs/02_design.md §セキュリティ設計方針 S-1）。
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        asset_profile_service: AssetProfileService | None = None,
        asset_store: BaseAssetStore | None = None,
    ) -> None:
        super().__init__()
        # Constructor injection seam — tests pass fakes, server.py wires the
        # real store via wire_services().
        self._profile_service = asset_profile_service or AssetProfileService()
        self._asset_store = asset_store

    def execute(self, state: dict) -> dict:
        user_input = state.get("user_input", "")
        if isinstance(user_input, str) and len(user_input) > _MAX_PAYLOAD_CHARS:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: payload exceeds size limit"],
            }

        payload = self._parse_user_input(user_input)
        if payload is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is not valid JSON envelope"],
            }

        mode = payload.get("mode")
        org_id = payload.get("org_id")
        asset_type = payload.get("asset_type")

        # ── TENANT FAIL-CLOSED (docs/02_design.md §(d)) ──
        # 空/欠落の org_id は決して下流へ通過させない — 越境参照を防ぐ唯一の境界。
        if not org_id or not isinstance(org_id, str) or not org_id.strip():
            emit_trace_event(
                "tenant_fail_closed",
                {"reason": "org_id missing or empty"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: org_id resolution failed (fail-closed)"],
            }

        if mode not in _VALID_MODES:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: unknown mode {mode!r}, expected one of {sorted(_VALID_MODES)}"],
            }

        # asset_type is required for the envelope, but an UNKNOWN asset_type is
        # NOT fail-closed (docs/02_design.md §(a)) — get_profile() returns {}
        # and downstream Stage1/2 skip profile-dependent checks, degrading to
        # asset-type-general guidance instead of erroring the whole request.
        if not asset_type or not isinstance(asset_type, str):
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: asset_type is required"],
            }

        asset_profile = self._profile_service.get_profile(asset_type)
        degradation_reason = None
        if not asset_profile:
            degradation_reason = "asset_profile_not_found"
            emit_trace_event(
                "asset_profile_not_found",
                {"asset_type": asset_type},
                state,
            )

        org_context = {
            "org_id": org_id,
            "region": payload.get("region", ""),
        }

        envelope: dict[str, Any] = {
            "mode": mode,
            "org_id": org_id,
            "asset_type": asset_type,
            "asset_id": payload.get("asset_id", ""),
            "payload": payload.get("payload", {}),
            "org_context": org_context,
            "asset_profile": asset_profile,
        }

        result: dict[str, Any] = {
            "mode": mode,
            "org_id": org_id,
            "asset_type": asset_type,
            "org_context": org_context,
            "asset_profile": asset_profile,
            "status": AgentStatus.SUCCESS.value,
        }
        if payload.get("asset_id"):
            result["asset_id"] = payload["asset_id"]
        if degradation_reason:
            result["degradation_reason"] = degradation_reason

        # 🔴 /invoke と /feedback はいずれも対象資産に紐づく操作
        # （/feedback は interventions.asset_id への UPDATE/INSERT・直近
        # advisory の resolve に asset_id を要する — データモデル概要参照）。
        # asset_id 欠落を /invoke だけで弾くと /feedback が空文字列のまま
        # 補修履歴を書き込み、進行捕捉の突合も機能しなくなる（reconcile_
        # prediction が asset_id で advisory を検索できない）。
        if mode in ("invoke", "feedback"):
            asset_id = payload.get("asset_id", "")
            if not asset_id:
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": [f"PreProcessNode: asset_id is required for /{mode}"],
                }

        if mode == "invoke":
            asset_id = payload.get("asset_id", "")
            asset_history = self._load_asset_history(org_id, asset_id, state)
            envelope["asset_history"] = asset_history
            result["asset_history"] = asset_history

        result["validated_input"] = json.dumps(envelope, default=str, ensure_ascii=False)

        emit_trace_event(
            "pre_process_validated",
            {"mode": mode, "org_id": org_id, "asset_type": asset_type},
            state,
        )
        return result

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_user_input(user_input: str) -> dict[str, Any] | None:
        if not user_input or not user_input.strip():
            return None
        try:
            data = json.loads(user_input)
        except (json.JSONDecodeError, TypeError):
            return None
        except RecursionError:
            # A client-controlled request body containing extremely deeply
            # nested arrays/objects exhausts the interpreter's recursion
            # limit INSIDE the stdlib json decoder itself (RecursionError is
            # a RuntimeError subclass, not a json.JSONDecodeError, so it is
            # not caught above). Treat as "not parseable" rather than crash
            # the whole invocation (remote DoS — mirrors AquacultureEnvironmentalEarlyWarningAgent precedent).
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _load_asset_history(self, org_id: str, asset_id: str, state: dict) -> dict[str, Any]:
        """Load inspections/interventions history for /invoke Stage1/2. Never
        raises — an empty history is a valid (if data-thin) state, handled by
        §(b) 最小データ要件 downstream, not a pre_process failure.
        """
        if self._asset_store is None:
            return {"inspections": [], "interventions": []}
        try:
            inspections = self._asset_store.query_inspection_history(org_id, asset_id)
            interventions = self._asset_store.query_intervention_history(org_id, asset_id)
        except Exception as exc:  # noqa: BLE001 — store failure degrades, does not crash
            emit_trace_event("asset_history_load_failed", {"error": str(exc)}, state)
            return {"inspections": [], "interventions": []}
        return {"inspections": inspections, "interventions": interventions}

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 extension (docs/02_design.md §セキュリティ設計方針): strip
        obvious inspector-name PII from free-text fields inside the JSON
        envelope before execute() ever sees it. Must not raise — a malformed
        envelope is left as-is and rejected later by execute()'s own
        JSON/schema validation (status=error).
        """
        user_input = state.get("user_input", "")
        if not isinstance(user_input, str) or not user_input:
            return state

        try:
            envelope = json.loads(user_input)
        except (json.JSONDecodeError, TypeError):
            return state  # malformed JSON — execute() will reject it with status=error
        except RecursionError:
            # S-2 gate runs BEFORE execute() — first place raw user_input is
            # parsed. See PreProcessNode._parse_user_input for rationale.
            return state

        if not isinstance(envelope, dict):
            return state

        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            return state

        redacted = False
        for field in _FREE_TEXT_PAYLOAD_FIELDS:
            value = payload.get(field)
            if isinstance(value, str) and _INSPECTOR_NAME_PII_PATTERN.search(value):
                payload[field] = _INSPECTOR_NAME_PII_PATTERN.sub("[MASKED]", value)
                redacted = True

        # /ingest の records[] も同様のフィールドを持ちうる。
        records = payload.get("records")
        if isinstance(records, list):
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                for field in _FREE_TEXT_PAYLOAD_FIELDS:
                    value = rec.get(field)
                    if isinstance(value, str) and _INSPECTOR_NAME_PII_PATTERN.search(value):
                        rec[field] = _INSPECTOR_NAME_PII_PATTERN.sub("[MASKED]", value)
                        redacted = True

        if redacted:
            state = dict(state)
            envelope["payload"] = payload
            state["user_input"] = json.dumps(envelope, default=str, ensure_ascii=False)

        return state
