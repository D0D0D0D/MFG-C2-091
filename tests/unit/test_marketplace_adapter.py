"""AGENTIC STAR Marketplace entry-adapter boundary tests."""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.graph.graph import AssetInspectionHistoryAdvisoryGraph
from src.services.asset_store import InMemoryAssetStore


def _set_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {
        "AZURE_OPENAI_API_KEY": f"{suffix}-key",
        "AZURE_OPENAI_ENDPOINT": f"https://{suffix}.azure.example",
        "AZURE_OPENAI_DEPLOYMENT": f"{suffix}-deployment",
        "STG_DEFAULT_ORG_ID": f"{suffix}-org",
        "STG_DEFAULT_ASSET_TYPE": "bridge_appurtenance",
        "STG_DEFAULT_ASSET_ID": f"{suffix}-asset",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def _payload() -> str:
    return json.dumps(
        {
            "current_inspection": {
                "inspected_at": "2026-08-31",
                "measurements": [{"metric": "expansion_joint_gap_mm", "value": 65.0}],
            }
        }
    )


def _contains(value, needle: str) -> bool:
    if isinstance(value, dict):
        return any(_contains(key, needle) or _contains(item, needle) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains(item, needle) for item in value)
    return needle in value if isinstance(value, str) else False


def test_runner_context_is_rebuilt_and_conversation_history_never_enters_state(monkeypatch):
    _set_environment(monkeypatch)
    sentinel = "UNSCANNED-MARKETPLACE-HISTORY-091"
    agent = cli.MarketplaceGraph()
    agent.compile()
    original_invoke = agent._compiled.invoke
    captured = {}

    def capture_state(initial_state, config=None):
        captured["initial"] = initial_state
        final_state = original_invoke(initial_state, config=config)
        captured["final"] = final_state
        return final_state

    agent._compiled.invoke = capture_state
    result = agent.invoke(
        _payload(),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
        input_context={
            "conversation_history": [{"role": "user", "content": sentinel}],
            "future_runner_key": sentinel,
        },
    )

    assert captured["initial"]["input_context"] == {}
    assert not _contains(captured["initial"], sentinel)
    assert not _contains(captured["final"], sentinel)
    assert not _contains(result, sentinel)
    assert result["status"] == "success"


def test_invoke_rebuilds_envelope_from_deployment_scope(monkeypatch):
    values = _set_environment(monkeypatch, "deployment")
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(user_input=user_input, input_context=input_context, ctx=ctx)
        return {"status": "success"}

    monkeypatch.setattr(AssetInspectionHistoryAdvisoryGraph, "invoke", parent_invoke)
    original_ctx = InvocationContext(
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=True,
    )
    result = cli.MarketplaceGraph().invoke(
        json.dumps(
            {
                "mode": "invoke",
                "org_id": "caller-org",
                "asset_type": "caller-type",
                "asset_id": "caller-asset",
                "payload": {"current_inspection": {"measurements": []}},
            }
        ),
        ctx=original_ctx,
        input_context={"conversation_history": ["untrusted"]},
    )

    adapted = json.loads(captured["user_input"])
    assert result == {"status": "success"}
    assert adapted["mode"] == "invoke"
    assert adapted["org_id"] == values["STG_DEFAULT_ORG_ID"]
    assert adapted["asset_type"] == values["STG_DEFAULT_ASSET_TYPE"]
    assert adapted["asset_id"] == values["STG_DEFAULT_ASSET_ID"]
    assert captured["input_context"] == {}
    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].hitl_allowed is False


@pytest.mark.parametrize("mode", ["ingest", "feedback"])
def test_marketplace_rejects_write_modes(mode):
    adapted = json.loads(
        cli._marketplace_user_input(
            json.dumps({"mode": mode, "payload": {"records": []}}),
            {"org_id": "o", "asset_type": "t", "asset_id": "a"},
        )
    )

    assert adapted == {"mode": cli._MARKETPLACE_REJECTION_MODE}


def test_marketplace_write_packet_cannot_mutate_store(monkeypatch):
    _set_environment(monkeypatch)
    store = InMemoryAssetStore()
    agent = cli.MarketplaceGraph(config={"asset_store": store})
    agent.compile()

    result = agent.invoke(
        json.dumps(
            {
                "mode": "ingest",
                "payload": {
                    "records": [
                        {
                            "asset_id": "forbidden-write",
                            "inspected_at": "2026-08-31",
                            "findings": {"defect": "crack"},
                        }
                    ]
                },
            }
        ),
        ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
    )

    assert result["status"] == "error"
    assert store.query_inspection_history("one-org", "forbidden-write") == []


@pytest.mark.parametrize("missing_key", cli._REQUIRED_SECRET_KEYS)
def test_missing_required_value_fails_during_startup(monkeypatch, missing_key):
    _set_environment(monkeypatch)
    monkeypatch.delenv(missing_key)

    with pytest.raises(MissingSecret, match=missing_key):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize("invalid", [" ", "bad value", "bad\nvalue"])
def test_blank_or_whitespace_scope_fails_closed(monkeypatch, invalid):
    _set_environment(monkeypatch)
    monkeypatch.setenv("STG_DEFAULT_ORG_ID", invalid)

    with pytest.raises(ValueError, match="STG_DEFAULT_ORG_ID"):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


def test_allowlisted_provider_hides_unrelated_environment(monkeypatch):
    _set_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    agent = cli.MarketplaceGraph()

    agent.provision_secrets(NullProvider())

    assert agent._secrets_provider.get("UNRELATED_PLATFORM_SECRET") is None


def test_azure_llm_resolves_fresh_credentials_for_every_call(monkeypatch):
    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "messages": messages}

        def stream(self, messages):
            del messages
            yield "ok"

        def bind_tools(self, tools):
            del tools
            return self

    azure_module = ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = FakeAzureOpenAIClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)

    provider = cli._AllowlistedEnvironmentProvider()
    llm = cli._AzureMarketplaceLLM()
    assert vars(llm) == {}

    with bound_secrets(provider):
        first = _set_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        second = _set_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])

    assert vars(llm) == {}
    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_main_passes_config_to_marketplace_runner(monkeypatch):
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "MFG-C2-091"
    assert called["namespace"] == "mfg"
    assert called["config"]["max_retry"] == 3
