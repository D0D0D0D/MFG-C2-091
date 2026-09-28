"""AGENTIC STAR Marketplace entry adapter for MFG-C2-091."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # pragma: no cover - AgentCore 1.0.0 has no Marketplace extra
    if exc.name != "shared.bootstrap":
        raise

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("The installed AgentCore wheel does not include Marketplace support")


from shared.services.llm.base_llm import BaseLLM

from src.graph.graph import AssetInspectionHistoryAdvisoryGraph

_NAMESPACE = "mfg"
_AGENT_NAME = "MFG-C2-091"
_PROVIDER_AGENT_NAME = "mfg_c2_091"

_MARKETPLACE_MODE = "invoke"
_MARKETPLACE_REJECTION_MODE = "marketplace_mode_not_available"

_SCOPE_ENV_BY_KEY = {
    "org_id": "STG_DEFAULT_ORG_ID",
    "asset_type": "STG_DEFAULT_ASSET_TYPE",
    "asset_id": "STG_DEFAULT_ASSET_ID",
}
_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_REQUIRED_SECRET_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, *_SCOPE_ENV_BY_KEY.values())
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)


class _AllowlistedEnvironmentProvider(SecretProvider):
    """Expose only deployment values declared by this single-agent adapter."""

    def __init__(self) -> None:
        self._namespace = _NAMESPACE
        self._agent_name = _PROVIDER_AGENT_NAME

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = os.environ.get(key)
        return value if value else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve a deployment value without silently trimming or repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Resolve the tenant and asset scope, failing closed on every bad value."""

    return {state_key: _require_clean(provider, env_key) for state_key, env_key in _SCOPE_ENV_BY_KEY.items()}


class _UnavailableAzureOpenAIClient:
    """AgentCore 1.0.0 compatibility stub for the absent Azure integration."""

    def __init__(self, config: dict[str, Any]) -> None:
        del config
        raise RuntimeError("The installed AgentCore wheel does not include AzureOpenAIClient")


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Build an Azure client per operation and retain no connection values."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _new_client(self) -> BaseLLM:
        # Keep the optional Azure integration lazy. AgentCore 1.0.0 does not
        # ship this module, while Marketplace images may provide it.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised by the 1.0.0 image at runtime
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _UnavailableAzureOpenAIClient  # type: ignore[misc,assignment]

        provider = current_secrets()
        client: BaseLLM = AzureOpenAIClient(
            {
                "api_key": _require_clean(provider, "AZURE_OPENAI_API_KEY"),
                "azure_endpoint": _require_azure_endpoint(provider),
                "azure_deployment": _require_clean(provider, "AZURE_OPENAI_DEPLOYMENT"),
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


def _rejection_packet() -> str:
    return json.dumps({"mode": _MARKETPLACE_REJECTION_MODE})


def _marketplace_user_input(user_input: str, scope: dict[str, str]) -> str:
    """Create a read-only invoke envelope from adapter-owned scope values."""

    try:
        packet = json.loads(user_input)
    except (json.JSONDecodeError, TypeError, RecursionError):
        return _rejection_packet()
    if not isinstance(packet, dict):
        return _rejection_packet()

    requested_mode = packet.get("mode", _MARKETPLACE_MODE)
    if requested_mode != _MARKETPLACE_MODE:
        return _rejection_packet()

    # Accept either the normal graph envelope or a payload-only Marketplace
    # message. Caller-supplied mode/scope never survives at the envelope level.
    payload = packet.get("payload", packet)
    if not isinstance(payload, dict):
        return _rejection_packet()

    envelope: dict[str, Any] = {
        "mode": _MARKETPLACE_MODE,
        **scope,
        "payload": payload,
    }
    return json.dumps(envelope, ensure_ascii=False)


class MarketplaceGraph(AssetInspectionHistoryAdvisoryGraph):
    """Marketplace-only read boundary and Azure dependency wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        marketplace_config = dict(config or {})
        # The runner compiles before provision_secrets(). Nodes must capture a
        # credential-free wrapper now; credentials are resolved only per call.
        marketplace_config["llm"] = _AzureMarketplaceLLM()
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # conversation_history is untrusted free text outside the S-2 scan.
        # Rebuild both the envelope and context exclusively from owned fields.
        del input_context
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        marketplace_ctx = replace(ctx, hitl_allowed=False) if ctx is not None else None
        return cast(
            dict[str, Any],
            super().invoke(
                _marketplace_user_input(user_input, scope),
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context={},
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore broader/dotenv-capable providers: this Pod may see only its
        # declared Azure credentials and deployment scope.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _scope_from_environment(environment_provider)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
