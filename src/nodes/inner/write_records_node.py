"""AgentCore Platform v1.0"""

# /ingest — write_records (docs/02_design.md §/ingest §データモデル概要).
# assets / inspections / interventions へ INSERT。書込件数・エラー行を
# ingest_summary へ集約。

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.asset_store import BaseAssetStore


class WriteRecordsNode(FunctionNode):
    """valid_records を assets/inspections/interventions へ書き込む。"""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, asset_store: BaseAssetStore) -> None:
        super().__init__()
        self._store = asset_store

    def execute(self, state: dict) -> dict:
        org_id = state.get("org_id", "")
        asset_type = state.get("asset_type", "")
        valid_records: list[dict[str, Any]] = state.get("valid_records", []) or []
        validation_errors: list[dict[str, Any]] = state.get("validation_errors", []) or []

        # assets 系列は resource-level（点検記録に紐づく資産マスタ）。ここでは
        # 未登録資産をこの点検記録の asset_type で仮登録する（マスタ更新は
        # 運用側の別プロセスが正とし、本エージェントは「初出資産は書き込む」のみ担う）。
        seen_asset_ids = {rec.get("asset_id") for rec in valid_records if rec.get("asset_id")}
        asset_written = 0
        if seen_asset_ids:
            asset_written = self._store.write_assets(
                org_id, [{"asset_id": aid, "org_id": org_id, "asset_type": asset_type} for aid in seen_asset_ids]
            )

        inspection_records = []
        for rec in valid_records:
            inspection_records.append(
                {
                    "asset_id": rec.get("asset_id"),
                    "org_id": org_id,
                    "inspected_at": rec.get("inspected_at"),
                    "inspector": rec.get("inspector"),
                    "method": rec.get("method"),
                    "findings": rec.get("findings"),
                    "measurements": rec.get("measurements", []),
                    "free_note": rec.get("free_note"),
                }
            )
        inspection_written = self._store.write_inspections(org_id, inspection_records) if inspection_records else 0

        # payload 側で明示的に interventions レコードが渡されている場合のみ
        # 補修履歴として書き込む（点検記録そのものは補修行為ではない）。
        payload = state.get("payload", {}) or {}
        intervention_records = payload.get("interventions", []) or []
        intervention_ids: list[str] = []
        if intervention_records:
            intervention_ids = self._store.write_interventions(org_id, intervention_records)

        ingest_summary = {
            "assets_written": asset_written,
            "inspections_written": inspection_written,
            "interventions_written": len(intervention_ids),
            "intervention_ids": intervention_ids,
            "error_row_count": len(validation_errors),
            "error_rows": validation_errors,
        }

        emit_trace_event(
            "ingest_write_completed",
            {
                "org_id": org_id,
                "inspections_written": inspection_written,
                "interventions_written": len(intervention_ids),
                "error_row_count": len(validation_errors),
            },
            state,
        )

        return {
            "ingest_summary": ingest_summary,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
