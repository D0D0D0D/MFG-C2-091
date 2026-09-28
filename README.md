# MFG-C2-091 — AssetInspectionHistoryAdvisoryAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: MFG

## Overview

This agent accumulates inspection and repair history for infrastructure and equipment assets —
bridge appurtenances, rotating machinery, water and sewer pipework — and, on each new inspection,
compares the latest reading against that asset's own past record. It returns a non-conclusive
ageing observation and a maintenance priority, with the finding ids the observation rests on.

It suits maintenance work where the useful question is not "what does this reading say" but "how
has this asset changed since last time, and what should be looked at next": a periodic inspection
programme, a maintenance planner triaging which assets to visit, a team whose inspection records
arrive in whatever format the field crew used.

Three modes share one endpoint, selected by the `mode` field of the request payload:

| Mode | Purpose |
|---|---|
| `ingest` | Load existing inspection records into the agent's own schema |
| `invoke` | Compare a new inspection against history and return the advisory |
| `feedback` | Record what was actually repaired, improving later comparisons |

The division of labour between the language model and the deterministic layer is the point of
the design. The model writes the narrative; it does not decide the finding:

- **Deltas, severities and priority are computed, not generated.** The finding index — metric,
  delta, progression rate, severity — comes from the deterministic comparison against history.
  The model's job is to explain it.
- **The narrative cites finding ids.** Each observation carries `cited_finding_ids` and
  `cited_intervention_ids` back to the records it was derived from.
- **No model, no failure.** With no language-model client wired the run still completes with a
  deterministic advisory; the response then carries `degradation_reason` and a human-readable
  notice, so a degraded answer is visible as degraded rather than silently thinner.
- **Facility-identifying detail is withheld.** Precise locations and detailed defect particulars
  are removed from the output; a client-supplied string echoed back is sanitised.
- **Asset types are configuration.** `config/asset_types/{asset_type}.yaml` defines a type;
  adding one needs no code change. Three ship as samples: `bridge_appurtenance`,
  `rotating_machine`, `water_pipe`.

Every observation is an explicitly non-conclusive draft. It does not perform image-based
diagnosis, does not fill in statutory inspection forms, does not issue a soundness verdict, and
does not emit control actions.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime and a secret provider;
- optionally, a language model. The standalone adapter builds an `AnthropicClient` when the
  `anthropic` extra is installed **and** `ANTHROPIC_API_KEY` is provisioned; either being absent
  is a supported degradation. Any other wiring failure — a renamed symbol, a broken import,
  a constructor mismatch — propagates rather than silently disabling the model;
- **a persistent asset store.** The shipped `InMemoryAssetStore` is a local-development default
  and must be replaced with a persistent `BaseAssetStore` implementation before any production
  deployment;
- caller authentication in front of the entry adapter.

Runtime parameters in `config/config.yaml`:

| Key | Meaning |
|---|---|
| `max_retry` (`3`) | Retry budget for transient pre-processing failures |
| `timeout_seconds` (`30`) | Execution timeout |
| `memory_enabled` (`false`), `hitl.enabled` (`false`) | Off by design — the workflow is a single stateless call per mode |

`config/agent.yaml` is the static manifest and carries `required_trust_level`
(`VERIFIED_EXTERNAL`) and the declared secret; runtime tuning belongs in `config/config.yaml`.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform, which supplies the entry adapter, the authentication middleware and the secret provider. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. The `anthropic` extra is needed for the model-backed path. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and the agent degrades deliberately rather than
refusing to start. The standalone server in `src/api/server.py` does start without the platform:
it wires an in-memory asset store, and it builds a language-model client only when both the
`anthropic` extra and `ANTHROPIC_API_KEY` are available. When either is missing, it serves the
deterministic advisory path and says so through `degradation_reason` — a missing key must not
take `/health` down. Every other wiring error is treated as a bug and propagates. Nothing
persists across a restart with the in-memory store. The test suite runs without a platform
connection.

## Input

`POST /invoke` takes `input` — a **JSON string** carrying the envelope — plus a `session_id`.
The envelope is `mode`, `org_id`, `asset_type`, `asset_id` and `payload`.

Callers must be authenticated. Trust established by upstream middleware is used as-is and never
demoted. When `INVOKE_AUTH_TOKEN` is set on the server environment, an otherwise-anonymous caller
must present it as `Authorization: Bearer` or receive **401** with a deliberately generic body.
When the token is not set and no middleware vouched for the caller, the request reaches the graph
as `ANONYMOUS` and is refused there — every node requires `VERIFIED_EXTERNAL`, so the S-1 trust
gate stops it before any node body runs.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "{\"mode\":\"invoke\",\"org_id\":\"org1\",\"asset_type\":\"bridge_appurtenance\",\"asset_id\":\"asset1\",\"payload\":{\"current_inspection\":{\"inspected_at\":\"2026-07-01\",\"measurements\":[{\"metric\":\"expansion_joint_gap_mm\",\"value\":45}]}}}",
    "session_id": "demo-001"
  }'
```

Input fails closed: an oversized payload, a body that is not a valid JSON envelope, an
unresolvable `org_id`, an unknown `mode`, a missing `asset_type`, or a missing `asset_id` for
`invoke` / `feedback` all end the run with `status=error` and a reason in `error_log`.

## Output

`output` carries the formatted result for the requested mode.

For `invoke`: `priority`, `priority_label_ja`, `narrative`, `cited_finding_ids`,
`cited_intervention_ids`, and — when there are deltas — `finding_index` with
`finding_index_total_count` (plus `finding_index_truncated` when the index was capped).
`degradation_reason` and `degradation_notice_ja` are added whenever the run degraded.

```json
{
  "priority": "medium",
  "priority_label_ja": "要注意",
  "narrative": "伸縮装置の遊間が拡大傾向にある可能性があります（根拠: fnd-...）。\n\n【免責】本所見は非断定のドラフトであり、健全性の確定判定・署名は有資格技術者の責任です。",
  "cited_finding_ids": ["fnd-..."],
  "finding_index": [
    {"finding_id": "fnd-...", "metric": "expansion_joint_gap_mm", "delta": 5,
     "progression_rate": 0.4, "severity": "medium"}
  ],
  "finding_index_total_count": 1
}
```

`ingest` returns `ingest_summary`; `feedback` returns `feedback_result`.

Values above are illustrative; real output depends on the asset's accumulated history.

## Security and Limitations

Every node requires verified-external trust, so an anonymous caller is stopped before any node
body runs. Free-text inspection fields are scanned on the way in for personal names written with
a Japanese honorific suffix. On the way out the output gate runs before the advisory is
persisted, so what is stored is the sanitised form, and client-supplied strings echoed back
through the response are sanitised rather than reflected verbatim.

Known limits, recorded rather than hidden:

- **The provenance check confirms a finding id exists in the same fragment, not that it supports
  the claim.** Sentence splitting also recognises Japanese terminators only, so a fragment
  punctuated solely with ASCII full stops is treated as one sentence.
- **Inbound model output on the `ingest` / `feedback` paths can be persisted before the output
  gate sees it.** The framework gate scans top-level strings, while structured model text is
  nested and the database write commits first. A permanent fix — recursive inspection before
  persistence, or separating structuring from the write — is a design change and has not been
  made.
- **Client-derived strings reach the prompt.** Fields such as `metric` and `action` are included
  in the prompt, which leaves room for instruction injection.
- **Thin history means generic advice.** An asset with fewer than two inspections and no repair
  history gets asset-type-general guidance; accuracy improves as history accumulates.
- **The bundled asset-type profiles are samples.** Review them against your own inspection
  standards before operational use.

Output is machine-generated, non-conclusive and assistive. A soundness determination and its
sign-off remain the responsibility of a qualified engineer.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2) with an inner workflow subgraph
- Generation mode: `llm`, with a deterministic fallback when no model is available
- HITL: not enabled
- Persistence: an injected asset store; the bundled in-memory one is for development
- External API clients: `AnthropicClient` from the framework's shared services, built by the
  standalone adapter when the extra and the key are both present

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection and without a language model. Running the agent against a
bare `uvicorn src.api.server:app` starts and serves the deterministic path, storing nothing
across restarts.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the three-stage observation pipeline
and the security design. `docs/03_test_spec.md` records the test specification and the
verification results, including a live-model run and the open items above.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
