"""Shared fixtures for live LLM smoke coverage.

The seed history and invoke payload mirror `tests/integration/test_graph.py`.
"""

from __future__ import annotations

import pytest

from src.services.asset_store import InMemoryAssetStore


@pytest.fixture
def asset_store() -> InMemoryAssetStore:
    """Seed the same asset history used by `_seed_store_with_history()`."""
    store = InMemoryAssetStore()
    store.write_inspections(
        "org1",
        [
            {
                "asset_id": "asset1",
                "inspected_at": "2024-01-01",
                "measurements": [{"metric": "expansion_joint_gap_mm", "value": 15}],
            },
            {
                "asset_id": "asset1",
                "inspected_at": "2025-01-01",
                "measurements": [{"metric": "expansion_joint_gap_mm", "value": 25}],
            },
        ],
    )
    store.write_interventions(
        "org1",
        [{"asset_id": "asset1", "action": "repair", "triggering_metrics": ["expansion_joint_gap_mm"]}],
    )
    return store


@pytest.fixture
def agent_config(asset_store) -> dict:
    """Graph config for `work/scripts/mrb_live_llm_check.py`.

    🔴 This fixture used to monkeypatch the live-check harness's BaseLLM client
    so it would accept a bare string prompt and hand back a bare string. That
    made the harness conform to a defect in this template (nodes calling
    `complete(prompt_str)` and using the result as text) instead of exposing
    it, so the live LLM turn could not detect the contract violation. The nodes
    now speak the real BaseLLM contract via `src/services/llm_text.py`, and the
    harness client is used unmodified.
    """
    return {"asset_store": asset_store}


@pytest.fixture
def case_payload() -> dict:
    """Integration invoke payload that produces a critical finding."""
    return {
        "mode": "invoke",
        "org_id": "org1",
        "asset_type": "bridge_appurtenance",
        "asset_id": "asset1",
        "payload": {
            "current_inspection": {
                "inspected_at": "2026-01-01",
                "measurements": [{"metric": "expansion_joint_gap_mm", "value": 65.0}],
            }
        },
    }


@pytest.fixture
def live_input_context() -> dict:
    """The agent payload carries mode/org/asset fields; no extra context is needed."""
    return {"mode": "invoke"}
