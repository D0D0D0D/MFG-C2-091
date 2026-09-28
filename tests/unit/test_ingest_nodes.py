# MFG-C2-091 — Unit Tests: /ingest inner nodes
# (normalize_input, structure_with_llm, validate_records, write_records)

from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.normalize_input_node import NormalizeInputNode
from src.nodes.inner.structure_with_llm_node import StructureWithLlmNode
from src.nodes.inner.validate_records_node import ValidateRecordsNode
from src.nodes.inner.write_records_node import WriteRecordsNode
from src.services.asset_store import InMemoryAssetStore


class _FakeLlm:
    """BaseLLM 契約に従う double: complete(messages: list) -> canonical dict。

    🔴 `.complete(prompt: str) -> str` を返す double にすると、本番が注入する
    `AnthropicClient`（`complete(messages) -> dict`）と形が違うまま全テストが
    緑になり、契約違反が実 LLM 1 回転でしか露見しない。
    """

    def complete(self, messages: list) -> dict:
        assert all(
            isinstance(m, dict) and "content" in m for m in messages
        ), "BaseLLM.complete() は messages: list[dict] を受け取る契約"
        return {
            "content": "構造化された所見: ひび割れ、程度中",
            "tool_calls": [],
            "model": "fake",
            "usage": {},
        }


class TestNormalizeInputNode:
    def test_absorbs_known_field_aliases(self):
        node = NormalizeInputNode()
        state = {
            "payload": {
                "records": [
                    {"資産ID": "a1", "点検日": "2025-01-01", "所見": "ひび割れ"},
                ]
            }
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        rec = result["normalized_records"][0]
        assert rec["asset_id"] == "a1"
        assert rec["inspected_at"] == "2025-01-01"
        assert rec["findings"] == "ひび割れ"

    def test_falls_back_to_payload_asset_id_when_row_lacks_one(self):
        node = NormalizeInputNode()
        state = {"payload": {"asset_id": "a1", "records": [{"inspected_at": "2025-01-01", "findings": "ok"}]}}
        result = node.execute(state)
        assert result["normalized_records"][0]["asset_id"] == "a1"

    def test_non_dict_records_are_skipped_not_crashed(self):
        node = NormalizeInputNode()
        state = {"payload": {"records": ["not a dict", 123, None]}}
        result = node.execute(state)
        assert result["normalized_records"] == []


class TestStructureWithLlmNode:
    def test_llm_none_passes_through_unchanged(self):
        node = StructureWithLlmNode(llm=None)
        records = [{"asset_id": "a1", "free_note": "some note"}]
        result = node.execute({"normalized_records": records})
        assert result["normalized_records"] == records

    def test_structures_free_note_when_findings_absent(self):
        node = StructureWithLlmNode(llm=_FakeLlm())
        records = [{"asset_id": "a1", "free_note": "ひび割れがある"}]
        result = node.execute({"normalized_records": records})
        assert result["normalized_records"][0]["findings"]["extracted_text"] == "構造化された所見: ひび割れ、程度中"

    def test_skips_records_that_already_have_findings(self):
        node = StructureWithLlmNode(llm=_FakeLlm())
        records = [{"asset_id": "a1", "findings": "already structured", "free_note": "ignored"}]
        result = node.execute({"normalized_records": records})
        assert result["normalized_records"][0]["findings"] == "already structured"

    def test_llm_failure_degrades_without_crashing(self):
        class Failing:
            def complete(self, messages: list) -> dict:
                raise RuntimeError("down")

        node = StructureWithLlmNode(llm=Failing())
        records = [{"asset_id": "a1", "free_note": "note"}]
        result = node.execute({"normalized_records": records})
        assert result["status"] == AgentStatus.SUCCESS.value


class TestValidateRecordsNode:
    def test_valid_record_passes(self):
        node = ValidateRecordsNode()
        records = [{"asset_id": "a1", "inspected_at": "2025-01-01", "findings": "ok"}]
        result = node.execute({"normalized_records": records})
        assert len(result["valid_records"]) == 1
        assert result["validation_errors"] == []

    def test_missing_required_field_is_rejected(self):
        node = ValidateRecordsNode()
        records = [{"asset_id": "a1", "findings": "ok"}]  # missing inspected_at
        result = node.execute({"normalized_records": records})
        assert result["valid_records"] == []
        assert result["validation_errors"][0]["reasons"] == ["missing_inspected_at"]

    def test_missing_findings_and_free_note_is_rejected(self):
        node = ValidateRecordsNode()
        records = [{"asset_id": "a1", "inspected_at": "2025-01-01"}]
        result = node.execute({"normalized_records": records})
        assert result["valid_records"] == []
        assert "missing_findings_or_free_note" in result["validation_errors"][0]["reasons"]


class TestWriteRecordsNode:
    def test_writes_assets_inspections_and_interventions(self):
        store = InMemoryAssetStore()
        node = WriteRecordsNode(asset_store=store)
        state = {
            "org_id": "org1",
            "asset_type": "bridge_appurtenance",
            "valid_records": [{"asset_id": "a1", "inspected_at": "2025-01-01", "findings": "ok", "measurements": []}],
            "validation_errors": [],
            "payload": {"interventions": [{"asset_id": "a1", "action": "repair"}]},
        }
        result = node.execute(state)
        summary = result["ingest_summary"]
        assert summary["assets_written"] == 1
        assert summary["inspections_written"] == 1
        assert summary["interventions_written"] == 1
        assert store.query_inspection_history("org1", "a1")

    def test_error_rows_are_reflected_in_summary(self):
        store = InMemoryAssetStore()
        node = WriteRecordsNode(asset_store=store)
        state = {
            "org_id": "org1",
            "asset_type": "x",
            "valid_records": [],
            "validation_errors": [{"row_index": 0, "reasons": ["missing_inspected_at"]}],
            "payload": {},
        }
        result = node.execute(state)
        assert result["ingest_summary"]["error_row_count"] == 1
