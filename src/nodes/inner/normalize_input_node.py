"""AgentCore Platform v1.0"""

# /ingest — normalize_input (docs/02_design.md §/ingest §/feedback の内部
# ノード構成). 異種フォーマット（手書き報告書OCR結果・Excel由来JSON・点検
# 管理システムエクスポート）を共通スキーマ候補へ変換。決定論の列マッピング
# — LLM 不使用。
#
# 入力 payload.records は各要素が異種キー名を持ちうる（手元フォーマット差）。
# 既知のエイリアスを吸収し、共通フィールド名
# （asset_id/inspected_at/inspector/method/findings/measurements/free_note）
# へ正規化する。未知フィールドはそのまま透過（構造化強化は後段
# structure_with_llm・validate_records が担う）。

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# 列名エイリアス辞書（手書き報告書OCR・Excelエクスポート等でよく見る表記ゆれ）。
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "asset_id": ("asset_id", "資産ID", "設備ID", "asset_no"),
    "inspected_at": ("inspected_at", "点検日", "点検年月日", "date"),
    "inspector": ("inspector", "点検者", "担当者"),
    "method": ("method", "点検方法", "手法"),
    "findings": ("findings", "所見", "点検結果"),
    "measurements": ("measurements", "計測値", "数値データ"),
    "free_note": ("free_note", "備考", "自由記述", "所見メモ"),
}


class NormalizeInputNode(FunctionNode):
    """異種フォーマットの点検記録を共通スキーマ候補へ正規化する。"""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        payload = state.get("payload", {}) or {}
        raw_records: list[dict[str, Any]] = payload.get("records", []) or []

        normalized: list[dict[str, Any]] = []
        for raw in raw_records:
            if not isinstance(raw, dict):
                continue
            record: dict[str, Any] = {}
            for canonical, aliases in _FIELD_ALIASES.items():
                for alias in aliases:
                    if alias in raw:
                        record[canonical] = raw[alias]
                        break
            # asset_id は payload レベルの asset_id をフォールバックとして使う
            # （/ingest は資産単位で呼ばれることが多い運用を想定）。
            if "asset_id" not in record and payload.get("asset_id"):
                record["asset_id"] = payload["asset_id"]
            normalized.append(record)

        emit_trace_event(
            "ingest_normalize_completed",
            {"record_count": len(normalized)},
            state,
        )

        return {
            "normalized_records": normalized,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
