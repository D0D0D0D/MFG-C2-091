# MFG-C2-091 — Unit Tests: AssetProfileService

from src.services.asset_profile_service import AssetProfileService


class TestAssetProfileService:
    def test_known_asset_type_returns_profile(self):
        service = AssetProfileService()
        profile = service.get_profile("bridge_appurtenance")
        assert "metrics" in profile
        assert "expansion_joint_gap_mm" in profile["metrics"]

    def test_unknown_asset_type_returns_empty_dict_not_raise(self):
        """🔴 docs/02_design.md §(a): unlike a tenant-boundary lookup, an
        unknown asset_type is NOT fail-closed — it degrades gracefully."""
        service = AssetProfileService()
        profile = service.get_profile("totally_unregistered_type_xyz")
        assert profile == {}

    def test_empty_or_non_string_asset_type_returns_empty_dict(self):
        service = AssetProfileService()
        assert service.get_profile("") == {}
        assert service.get_profile(None) == {}

    def test_path_traversal_attempt_does_not_escape_config_dir(self):
        service = AssetProfileService()
        profile = service.get_profile("../../../../etc/passwd")
        assert profile == {}

    def test_list_known_asset_types_includes_initial_samples(self):
        service = AssetProfileService()
        known = service.list_known_asset_types()
        assert "bridge_appurtenance" in known
        assert "rotating_machine" in known
        assert "water_pipe" in known

    def test_all_three_initial_profiles_declare_intervention_types(self):
        service = AssetProfileService()
        for asset_type in ("bridge_appurtenance", "rotating_machine", "water_pipe"):
            profile = service.get_profile(asset_type)
            assert profile.get("intervention_types"), f"{asset_type} missing intervention_types"
            assert profile.get("degradation_modes"), f"{asset_type} missing degradation_modes"
