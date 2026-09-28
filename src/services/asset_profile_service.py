"""AgentCore Platform v1.0"""

# Loads config/asset_types/{asset_type}.yaml — the asset-type-agnostic
# dictionary that Stage 1/2 use instead of hardcoded enums
# (docs/02_design.md §データモデル概要, §(a)). Pure domain service: no
# agenticstar imports, no side effects beyond reading local config files.
#
# 🔴 docs/02_design.md §(a) 未知 asset_type の挙動: unlike a tenant-boundary
# lookup (e.g. species_code in AquacultureEnvironmentalEarlyWarningAgent), an unknown asset_type here is
# NOT a fail-closed condition. get_profile() returns an EMPTY dict rather
# than raising, so callers (pre_process) degrade gracefully (共通スキーマ上
# での蓄積・経年差分検知は継続、プロファイル依存判定のみスキップ) instead of
# rejecting the whole request.

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import yaml

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "config", "asset_types")


@lru_cache(maxsize=64)
def _load_yaml(asset_type: str) -> dict[str, Any]:
    safe_type = os.path.basename(asset_type)  # defend against path traversal
    path = os.path.join(_CONFIG_DIR, f"{safe_type}.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


class AssetProfileService:
    """Domain service: resolve an asset_type to its full profile dict.

    Returns {} (never raises) for an unregistered asset_type — the
    contract that lets pre_process/Stage1 degrade gracefully rather than
    fail-closed (docs/02_design.md §(a): "対象は保守点検記録が存在する設備
    全般に拡張されうる前提"; a new asset_type is supported the moment its
    profile is dropped in, and is NOT an error before then).
    """

    def get_profile(self, asset_type: str) -> dict[str, Any]:
        if not asset_type or not isinstance(asset_type, str):
            return {}
        return dict(_load_yaml(asset_type))

    def list_known_asset_types(self) -> list[str]:
        if not os.path.isdir(_CONFIG_DIR):
            return []
        return sorted(
            fname[: -len(".yaml")] for fname in os.listdir(_CONFIG_DIR) if fname.endswith(".yaml")
        )
