# Architecture

## Governing rule

No Evidence, No Claim.

The LLM is a reasoning layer over a structured evidence graph. It is not the
source of truth and it never talks to an observability platform. Everything the
model can say has to resolve to an evidence ID that exists in the store, and
that check runs in backend code, not in the prompt.

## Layers

```
Next.js web app
      |
FastAPI backend  ->  Auth / RBAC / tenant scoping
      |
AI Investigation Orchestrator
      |-- Query Planner
      |-- Tool Router          (capability negotiation, parallel fan out)
      |-- Correlation Engine
      |-- Evidence Validator
      |-- Response Generator
      |
Connector Layer     splunk | datadog | newrelic | grafana | loki | prometheus
                    elastic | opensearch | dynatrace | cloudwatch | azure | gcp
      |
Normalization Layer (OTel aligned: Resource, Log, Span, Metric)
      |
Evidence Engine     extract -> redact -> fingerprint -> dedup -> rank -> store
      |
PostgreSQL + pgvector + Redis
      |
LLM reasoning  ->  RCA report + chat
```

## Direction of trust

Three inputs reach the model and they are kept in separate channels that never
concatenate into one blob:

1. System instructions. Trusted.
2. User query. Semi trusted, never able to override system rules.
3. Observability data. Untrusted, always. A log line that reads "ignore your
   previous instructions" is a string inside an evidence object, and evidence
   objects are data the model is asked to reason about, never instructions it is
   asked to follow.

## Phase 1 contracts, built

### `app/schemas/normalized.py`

Provider payloads die at the connector boundary. Everything past it is
`LogRecord`, `Span`, `Trace`, `MetricSeries`, `Alert`, `Deployment`,
`KubernetesEvent`, carrying a `Resource` and a `CorrelationKeys` block. All
timestamps are timezone aware and validated as such, because half of all
cross platform correlation bugs are a naive datetime from one provider.

`Severity.parse` collapses the level vocabularies the providers actually emit
(WARNING, SEVERE, CRIT, FINE) onto one enum, so "count the ERROR logs" means the
same thing across Splunk and CloudWatch.

### `app/connectors/base.py`

`ObservabilityConnector` with two properties that matter operationally.

Capability negotiation. Providers are not interchangeable. Loki has no traces,
Prometheus has no logs, CloudWatch has no service dependency map. A connector
declares a `capabilities` set and implements only those. Everything else falls
through to a default that returns `ProviderStatus.UNSUPPORTED`. The router
filters the user's selected providers per capability instead of raising.

Partial failure as a first class outcome. Every call returns a `ProviderResult`,
never a bare list, and never raises past the router. `UNSUPPORTED` is explicitly
not a failure. `RATE_LIMITED`, `TIMEOUT`, `AUTH_FAILED`, `CIRCUIT_OPEN` are. If
Datadog returns 429 the investigation continues on Splunk and New Relic and the
report says which provider was missing and what that leaves unknown.

Every result also carries the `ExecutedQuery` objects that produced it, which is
what the View Queries panel and the audit log render.

### `app/schemas/evidence.py`

The evidence object is frozen at construction. `Provenance` records provider,
integration, the literal query, query language, search window, execution time,
retrieval time, original event ID, transformations, and redactions applied. If
the source data changes later, re-querying creates new evidence rather than
editing old evidence, so a six month old incident report still reproduces.

`SignalClass` is the anti-overconfidence mechanism. Two evidence objects only
corroborate each other if their signal classes differ. One application error
shipped to Splunk, ingested by Datadog, and forwarded by New Relic is three rows
and one observation. Genuinely independent confirmation looks like: application
log says the connection failed, distributed trace says the Redis span timed out,
infrastructure metric says the pool was at 100 percent. That is three classes and
it is what a 90 plus confidence score is allowed to rest on.

`Claim` and `Hypothesis` carry supporting, contradicting, and missing evidence.
The hypothesis structure exists so the agent has to try to disprove candidates
rather than committing to the first plausible stack trace it reads.

`Assertion` forces every rendered statement into exactly one of observed,
inferred, unknown, recommended. The UI is not allowed to blend them.

### `app/evidence/store.py`

Append only. IDs are allocated E1 upward per investigation and never reused, so
`[E24]` in a chat message written today still resolves next quarter. The store
detects duplicate observations by fingerprint and marks them with `duplicate_of`
rather than dropping them, which keeps the audit trail complete while confidence
counts each observation once. `unknown_ids` is the hook the citation enforcer
uses to reject a model response that cites evidence that does not exist.

Fingerprinting normalises whitespace and case and buckets the timestamp to one
second, because the same event carries a different ingest time and a different
capitalisation depending on which agent shipped it.

## Deliberate deferrals

These are scheduled, not forgotten, and none of them are stubbed in the tree:

| Phase | Component |
| --- | --- |
| 2 | SQLAlchemy models, Alembic migrations, tenant scoping at the query layer |
| 3 | OIDC auth, RBAC, org / project / environment hierarchy |
| 4 | Secret backends, integration CRUD, test connection flow |
| 5 | Demo connector and the three synthetic incident scenarios |
| 6 to 8 | Splunk, Datadog, New Relic connectors |
| 9 | Correlation engine and the incident evidence graph |
| 10 | Evidence ranking, clustering, token budgeting |
| 11 to 12 | Investigation agent, hypothesis loop, validator, chat |
| 13 | Next.js frontend |
| 14 | Redaction, prompt injection tests, query safety, audit log |
| 15 to 16 | Full test matrix, Docker, Helm |
