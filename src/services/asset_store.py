"""AgentCore Platform v1.0"""

# Domain data store abstraction for assets / inspections / interventions
# (docs/02_design.md §データモデル概要). This template owns the schema; the
# concrete backend (Postgres, SQLite, in-memory) is an injection seam so
# tests never touch a real DB (docs/02_design.md — "実接続は STG").
#
# IMPORTANT (docs/02_design.md §State Constraints, §(d)): intervention detail
# records (outcome_narrative / post_condition /施設特定機微情報を含む) must
# never be placed directly into graph State — only reference IDs
# (intervention_id / finding_id) flow through State/checkpoints. Resolution
# of full detail happens in the API adapter layer (src/api/server.py) only.

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# Store-side defense-in-depth cap on query_similar_interventions (Stage 2).
# Mirrors AquacultureEnvironmentalEarlyWarningAgent's incident_store cap rationale: a broad similarity query
# against a real DB backend should not have to rely on the node remembering
# to cap afterward. A store MAY return fewer than `limit` rows.
_DEFAULT_SIMILAR_INTERVENTIONS_LIMIT = 200


class BaseAssetStore(ABC):
    """Injection seam for the 3-series schema (assets/inspections/interventions)."""

    @abstractmethod
    def query_inspection_history(self, org_id: str, asset_id: str) -> list[dict[str, Any]]:
        """Return past inspections rows for asset_id, ordered oldest->newest (Stage 1)."""

    @abstractmethod
    def query_intervention_history(self, org_id: str, asset_id: str) -> list[dict[str, Any]]:
        """Return past interventions rows for asset_id (Stage 1 min-data check, Stage 2)."""

    @abstractmethod
    def query_similar_interventions(
        self,
        org_id: str,
        asset_type: str,
        delta_findings: list[dict[str, Any]],
        limit: int = _DEFAULT_SIMILAR_INTERVENTIONS_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return interventions whose triggering context is similar to delta_findings (Stage 2).

        Scoped to `org_id` only (tenant fail-closed — docs/02_design.md §(d):
        「他組織のデータへの参照は設計上不可能」). `limit` bounds the number
        of rows returned, applied BEFORE the node's own severity-priority
        cap. Implementations MAY return fewer rows than `limit`.
        """

    @abstractmethod
    def write_assets(self, org_id: str, records: list[dict[str, Any]]) -> int:
        """Upsert assets rows. Returns count written."""

    @abstractmethod
    def write_inspections(self, org_id: str, records: list[dict[str, Any]]) -> int:
        """Bulk-insert inspections rows. Returns count written."""

    @abstractmethod
    def write_interventions(self, org_id: str, records: list[dict[str, Any]]) -> list[str]:
        """Bulk-insert interventions rows. Returns list of assigned intervention_id."""

    @abstractmethod
    def resolve_intervention_id(self, org_id: str, intervention_id: str | None) -> str:
        """Return an existing intervention_id, or mint a new deterministic one."""

    @abstractmethod
    def update_intervention(self, org_id: str, intervention_id: str, fields: dict[str, Any]) -> None:
        """Update (or insert if absent) an interventions row identified by intervention_id."""

    @abstractmethod
    def get_recent_advisory(self, org_id: str, asset_id: str) -> dict[str, Any] | None:
        """Return the most recent /invoke advisory_output for this asset, if any
        (used by reconcile_prediction in /feedback)."""

    @abstractmethod
    def record_advisory(self, org_id: str, asset_id: str, advisory: dict[str, Any]) -> None:
        """Persist the S-3-sanitized advisory produced by a successful
        (non-degraded) /invoke, so a later /feedback can reconcile the
        prediction against the actual intervention outcome
        (docs/02_design.md §/feedback reconcile_prediction). Called by
        PostProcessNode after the S-3 output gate has been applied — the
        stored advisory is always the sanitized, caller-visible form."""


class InMemoryAssetStore(BaseAssetStore):
    """In-memory fake store — used by tests and as a safe local-dev default.

    Not for production use (no persistence across process restarts).
    """

    def __init__(self) -> None:
        self._assets: dict[str, dict[str, dict[str, Any]]] = {}
        self._inspections: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self._interventions: dict[str, dict[str, dict[str, Any]]] = {}
        self._last_advisory: dict[str, dict[str, dict[str, Any]]] = {}
        self._intervention_seq = 0

    def query_inspection_history(self, org_id: str, asset_id: str) -> list[dict[str, Any]]:
        rows = self._inspections.get(org_id, {}).get(asset_id, [])
        return sorted(rows, key=lambda r: r.get("inspected_at", ""))

    def query_intervention_history(self, org_id: str, asset_id: str) -> list[dict[str, Any]]:
        interventions = self._interventions.get(org_id, {})
        return [rec for rec in interventions.values() if rec.get("asset_id") == asset_id]

    def query_similar_interventions(
        self,
        org_id: str,
        asset_type: str,
        delta_findings: list[dict[str, Any]],
        limit: int = _DEFAULT_SIMILAR_INTERVENTIONS_LIMIT,
    ) -> list[dict[str, Any]]:
        interventions = self._interventions.get(org_id, {})
        finding_metrics = {f.get("metric") for f in delta_findings}
        matched = []
        for rec in interventions.values():
            triggering_metrics = rec.get("triggering_metrics") or []
            if finding_metrics & set(triggering_metrics):
                matched.append(rec)
                if len(matched) >= limit:
                    break
        return matched

    def write_assets(self, org_id: str, records: list[dict[str, Any]]) -> int:
        bucket = self._assets.setdefault(org_id, {})
        for rec in records:
            asset_id = rec.get("asset_id")
            if asset_id:
                bucket[asset_id] = {**bucket.get(asset_id, {}), **rec}
        return len(records)

    def write_inspections(self, org_id: str, records: list[dict[str, Any]]) -> int:
        bucket = self._inspections.setdefault(org_id, {})
        for rec in records:
            asset_id = rec.get("asset_id", "")
            bucket.setdefault(asset_id, []).append(rec)
        return len(records)

    def write_interventions(self, org_id: str, records: list[dict[str, Any]]) -> list[str]:
        ids = []
        bucket = self._interventions.setdefault(org_id, {})
        for rec in records:
            intervention_id = rec.get("intervention_id") or self.resolve_intervention_id(org_id, None)
            rec = {**rec, "intervention_id": intervention_id}
            bucket[intervention_id] = rec
            ids.append(intervention_id)
        return ids

    def resolve_intervention_id(self, org_id: str, intervention_id: str | None) -> str:
        if intervention_id and intervention_id in self._interventions.get(org_id, {}):
            return intervention_id
        self._intervention_seq += 1
        return f"inv-{org_id}-{self._intervention_seq:06d}"

    def update_intervention(self, org_id: str, intervention_id: str, fields: dict[str, Any]) -> None:
        bucket = self._interventions.setdefault(org_id, {})
        existing = bucket.get(intervention_id, {"intervention_id": intervention_id})
        existing.update(fields)
        bucket[intervention_id] = existing

    def get_recent_advisory(self, org_id: str, asset_id: str) -> dict[str, Any] | None:
        return self._last_advisory.get(org_id, {}).get(asset_id)

    def record_advisory(self, org_id: str, asset_id: str, advisory: dict[str, Any]) -> None:
        self._last_advisory.setdefault(org_id, {})[asset_id] = advisory
