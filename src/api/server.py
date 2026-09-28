"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for MFG-C2-091 AssetInspectionHistoryAdvisoryGraph.
# Entry points are adapters only — no business logic here.
#
# wire_services() constructs the real LLM client and asset store and injects
# them via config — MUST be called before the first /invoke or the graph
# falls back to deterministic/no-op behavior for every mode and the first
# invocation can fail with an HTTP 500 response.

import os
import secrets
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.secrets.base import MissingSecret
from shared.secrets import factory as secrets_factory
from src.graph.graph import AssetInspectionHistoryAdvisoryGraph

NAMESPACE = "mfg"
AGENT_NAME = "mfg-c2-091"


def wire_services() -> dict:
    """Construct real service clients for production use.

    Returns a config dict suitable for AssetInspectionHistoryAdvisoryGraph(config=...).
    Import of the Anthropic client is local so tests that never call
    wire_services() do not need the `[anthropic]` extra installed.
    """
    # TODO(Asset Store Backend): InMemoryAssetStore is the
    # NON-PERSISTENT local-dev default — it MUST be replaced with a persistent
    # BaseAssetStore subclass before any production/customer-facing deployment.
    from src.services.asset_store import InMemoryAssetStore

    # Injection seam: the concrete model identity is an operational /
    # deployment-time decision, not hardcoded here.
    #
    # 🔴 A missing `[anthropic]` extra is a SUPPORTED degradation (the graph
    # runs its deterministic fallback — docs/02_design.md §経年所見の3段パイプ
    # ライン Stage 3), but a wiring mistake is NOT. This block previously
    # imported `AnthropicLLMClient`, a name that does not exist in
    # `shared.services.llm.anthropic_client` (the class is `AnthropicClient`),
    # and constructed it with no arguments although `AnthropicClient.__init__`
    # requires `config["api_key"]`. Both failures were swallowed by a blanket
    # `except Exception`, so production silently ran with `llm=None` on every
    # request while `config/agent.yaml` declared `generation_mode: "llm"` —
    # and `wire_services()` is never exercised by CI, so nothing caught it.
    # The import failure is now handled on its own; anything else propagates.
    # Exactly two conditions degrade to llm=None, both documented in the
    # operation guide (the service must keep serving the
    # deterministic path, so neither may stop startup):
    #   (a) the optional `[anthropic]` extra is not installed
    #   (b) ANTHROPIC_API_KEY is not provisioned in this environment
    # Anything else — a renamed/absent public symbol, a wrong constructor
    # signature, a broken transitive import — is a WIRING BUG and propagates.
    # A blanket `except Exception` here is what hid the original defect.
    llm = None
    try:
        from shared.services.llm.anthropic_client import AnthropicClient
    except ModuleNotFoundError as exc:
        # (a) Only the optional dependency itself. A ModuleNotFoundError naming
        # anything else means our own import graph is broken — do not swallow.
        # Note `from X import Name` raises plain ImportError when the MODULE
        # exists but the NAME does not, so that case propagates here: it is
        # precisely the `AnthropicLLMClient` typo this block used to hide.
        if exc.name != "langchain_anthropic":
            raise
    else:
        provider = secrets_factory(namespace=NAMESPACE, agent_name=AGENT_NAME)
        try:
            # Narrow on purpose: ONLY the secret lookup may degrade. Wrapping
            # the constructor too would swallow a future wiring defect that
            # happens to raise MissingSecret from inside the client.
            api_key = provider.require("ANTHROPIC_API_KEY")
        except MissingSecret:
            # (b) Key not provisioned — documented degradation. Raising here
            # would abort module import and take /health down with it.
            api_key = None
        if api_key is not None:
            llm = AnthropicClient({"api_key": api_key})

    return {
        "llm": llm,
        "asset_store": InMemoryAssetStore(),  # TODO(Asset Store Backend): replace with a persistent store before production
        "max_retry": 3,
    }


app = FastAPI(title="AssetInspectionHistoryAdvisoryAgent")

agent = AssetInspectionHistoryAdvisoryGraph(config=wire_services())
agent.compile()
agent.provision_secrets(secrets_factory(namespace=NAMESPACE, agent_name=AGENT_NAME))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Optional caller authentication: when
    # INVOKE_AUTH_TOKEN is set on the server environment, callers that no
    # upstream middleware vouched for (still ANONYMOUS) must present it as a
    # Bearer token and run at VERIFIED_EXTERNAL. Middleware-established trust
    # is never demoted. This adapter is the entry-point auth boundary
    # (standalone equivalent of platform AuthMiddleware) — a deployment-level
    # caller credential, not an agent secret, so ctx.secrets does not apply
    # (no InvocationContext exists before authentication).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str
        # input (headers decode as latin-1), which would 500 instead of the
        # generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was
            # absent, malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL
    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx)


@app.get("/health")
def health():
    return {"status": "ok", "agent": AGENT_NAME}
