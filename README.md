# Observability AI Agent

An AI incident investigation platform that queries Splunk, Datadog, New Relic,
Grafana, Loki, Elastic, CloudWatch and others in parallel, correlates what it
finds into a single evidence graph, and produces a root cause analysis where
every technical statement resolves to a citable piece of telemetry.

The point is not to summarise logs. The point is that an engineer at 2am should
not have to open six tabs and correlate them in their head.

## Product rule

No Evidence, No Claim.

Citation enforcement lives in backend code, not in the prompt. A model response
that cites `[E47]` when `E47` does not exist in the evidence store is rejected
before it reaches the user.

## Status: Phase 1 complete

Built and tested:

- Normalized OpenTelemetry aligned data model for logs, spans, traces, metrics,
  alerts, deployments, Kubernetes events, service dependency graphs
- `ObservabilityConnector` interface with capability negotiation and partial
  failure handling
- Connector registry, so adding a provider is one class plus one decorator
- Evidence object with full provenance, immutability, and cross-provider
  deduplication by signal class
- Append only evidence store with stable citation IDs
- 34 tests covering schema invariants, capability degradation, dedup, and
  citation validation

Not built yet, by design: everything from Phase 2 onward. See
`docs/ARCHITECTURE.md` for the schedule. There is no placeholder or TODO code in
the tree.

## Layout

```
backend/
  app/
    core/          settings, no credentials inline
    schemas/       normalized signals, evidence, claims, hypotheses
    connectors/    connector interface + registry
    evidence/      evidence store
    correlation/   phase 9
    agents/        phase 11
    security/      phase 14
    api/ auth/ database/ models/ prompts/ services/
  tests/
docs/
frontend/          phase 13
infra/             phase 16
```

## Run the tests

```bash
cd backend
pip install -r requirements.txt
pytest -q
```

## Adding a provider

```python
@register
class SplunkConnector(ObservabilityConnector):
    provider = Provider.SPLUNK
    capabilities = frozenset({Capability.SEARCH_LOGS, Capability.GET_ALERTS})
    query_language = "spl"
```

Implement only the capabilities you declare. Everything else returns
`UNSUPPORTED` and the router routes around it. No orchestrator, agent, or
frontend code changes.
