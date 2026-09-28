"""AgentCore Platform v1.0"""

# MFG-C2-091 AssetInspectionHistoryAdvisoryGraph — Cat 2 outer graph.
# Fixed backbone: initialize -> pre_process -> main -> {route} -> post_process -> finalize.
# Domain complexity (3 modes, 3-stage /invoke pipeline) lives in the inner
# AssetWorkflowGraph, wrapped by AssetWorkflowGraphNode in the `main` slot
# (docs/02_design.md §main（GraphNode）契約).
#
# add_edges()/route() are NOT overridden — backbone wiring belongs to the framework.

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import AssetWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class AssetInspectionHistoryAdvisoryGraph(AgentBaseGraph):
    """MFG-C2-091 — AssetInspectionHistoryAdvisoryAgent.

    config keys consumed:
      llm          — BaseLLM instance (the config["llm"] injection seam,
                      ) or None -> deterministic fallback
                      (docs/02_design.md §経年所見の3段パイプライン Stage 3).
      asset_store  — BaseAssetStore instance; defaults to InMemoryAssetStore
                      when absent (real backend wired by src/api/server.py
                      wire_services()).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config=config)

    @property
    def name(self) -> str:
        return "asset_inspection_history_advisory"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode

        # Resolve ONE shared store instance for all three slots. Without this,
        # an absent config["asset_store"] would leave pre_process reading from
        # no store while the inner graph falls back to its own private
        # InMemoryAssetStore — /ingest writes would then be invisible to the
        # next /invoke's history load, and post_process could not persist
        # advisories to the store /feedback reads (record_advisory).
        asset_store = self.config.get("asset_store")
        if asset_store is None:
            from src.services.asset_store import InMemoryAssetStore

            asset_store = InMemoryAssetStore()

        self._nodes["pre_process"] = PreProcessNode(asset_store=asset_store)
        self._nodes["main"] = AssetWorkflowGraphNode(
            config={"llm": self.config.get("llm")}, asset_store=asset_store
        )
        # asset_store injected so post_process can persist the S-3-sanitized
        # advisory for later /feedback reconciliation through record_advisory().
        self._nodes["post_process"] = PostProcessNode(asset_store=asset_store)

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.
