from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.evidence.store import (
    EvidenceIntegrityError,
    InMemoryEvidenceStore,
    independent_signal_classes,
)
from app.schemas.evidence import (
    Evidence,
    EvidenceType,
    Provenance,
    SignalClass,
    content_fingerprint,
)
from app.schemas.normalized import Provider, Resource, Severity, TimeRange

INV = "inv-001"
T0 = datetime(2026, 9, 4, 10, 14, 33, 245000, tzinfo=timezone.utc)
WINDOW = TimeRange(start=T0 - timedelta(minutes=15), end=T0 + timedelta(minutes=15))

REDIS_ERROR = "RedisConnectionException: Unable to acquire connection from pool"


def provenance(provider: Provider, query: str) -> Provenance:
    return Provenance(
        provider=provider,
        integration_id=f"{provider.value}-1",
        query=query,
        search_window=WINDOW,
        executed_at=T0,
        retrieved_at=T0,
    )


def make(
    *,
    provider: Provider,
    content: str = REDIS_ERROR,
    signal_class: SignalClass = SignalClass.APPLICATION_LOG,
    evidence_type: EvidenceType = EvidenceType.LOG,
    timestamp: datetime = T0,
    service: str = "payment-service",
    query: str = 'index=prod trace_id="4bf92f"',
):
    def build(evidence_id: str) -> Evidence:
        return Evidence(
            evidence_id=evidence_id,
            investigation_id=INV,
            evidence_type=evidence_type,
            signal_class=signal_class,
            timestamp=timestamp,
            summary=content[:60],
            content=content,
            resource=Resource(service=service, environment="production"),
            level=Severity.ERROR,
            fingerprint=content_fingerprint(
                signal_class=signal_class,
                service=service,
                content=content,
                timestamp=timestamp,
            ),
            provenance=provenance(provider, query),
        )

    return build


def test_evidence_ids_are_sequential_and_stable():
    store = InMemoryEvidenceStore()
    first = store.add(investigation_id=INV, build=make(provider=Provider.SPLUNK))
    second = store.add(investigation_id=INV, build=make(provider=Provider.DATADOG, content="x"))
    assert (first.evidence_id, second.evidence_id) == ("E1", "E2")
    assert store.get(INV, "E1") is first


def test_ids_do_not_leak_between_investigations():
    store = InMemoryEvidenceStore()
    store.add(investigation_id="inv-a", build=_for("inv-a", Provider.SPLUNK))
    other = store.add(investigation_id="inv-b", build=_for("inv-b", Provider.SPLUNK))
    assert other.evidence_id == "E1"
    assert store.get("inv-a", "E2") is None


def _for(investigation_id: str, provider: Provider):
    def build(evidence_id: str) -> Evidence:
        return Evidence(
            evidence_id=evidence_id,
            investigation_id=investigation_id,
            evidence_type=EvidenceType.LOG,
            signal_class=SignalClass.APPLICATION_LOG,
            timestamp=T0,
            summary="s",
            content=REDIS_ERROR,
            fingerprint=content_fingerprint(
                signal_class=SignalClass.APPLICATION_LOG,
                service="payment-service",
                content=REDIS_ERROR,
                timestamp=T0,
            ),
            provenance=provenance(provider, "q"),
        )

    return build


def test_evidence_is_immutable_once_recorded():
    store = InMemoryEvidenceStore()
    evidence = store.add(investigation_id=INV, build=make(provider=Provider.SPLUNK))
    with pytest.raises(ValidationError):
        evidence.content = "rewritten"


def test_builder_cannot_forge_a_different_id():
    store = InMemoryEvidenceStore()
    store.add(investigation_id=INV, build=make(provider=Provider.SPLUNK))

    def bad(_allocated: str) -> Evidence:
        return make(provider=Provider.SPLUNK)("E1")

    with pytest.raises(EvidenceIntegrityError):
        store.add(investigation_id=INV, build=bad)


def test_same_log_from_three_platforms_counts_once():
    """The central anti-overconfidence rule.

    Splunk indexes the application log, Datadog ingests the same line, New Relic
    forwards it too. That is one observation, not three.
    """
    store = InMemoryEvidenceStore()
    splunk = store.add(investigation_id=INV, build=make(provider=Provider.SPLUNK))
    datadog = store.add(
        investigation_id=INV,
        build=make(
            provider=Provider.DATADOG,
            # different shipper, different whitespace and case, same event
            content="  redisconnectionexception: Unable to acquire connection from POOL ",
            timestamp=T0 + timedelta(milliseconds=180),
        ),
    )
    newrelic = store.add(investigation_id=INV, build=make(provider=Provider.NEWRELIC))

    assert splunk.duplicate_of is None
    assert datadog.duplicate_of == "E1"
    assert newrelic.duplicate_of == "E1"
    assert len(store.all(INV)) == 3, "duplicates are retained for audit"
    assert sum(e.counts_toward_confidence for e in store.all(INV)) == 1


def test_genuinely_independent_signals_are_not_deduped():
    store = InMemoryEvidenceStore()
    store.add(investigation_id=INV, build=make(provider=Provider.SPLUNK))
    store.add(
        investigation_id=INV,
        build=make(
            provider=Provider.DATADOG,
            content="span payment-service -> redis timed out after 3200ms",
            signal_class=SignalClass.DISTRIBUTED_TRACE,
            evidence_type=EvidenceType.SPAN,
        ),
    )
    store.add(
        investigation_id=INV,
        build=make(
            provider=Provider.NEWRELIC,
            content="redis.connection.pool.active=100 of max=100",
            signal_class=SignalClass.INFRASTRUCTURE_METRIC,
            evidence_type=EvidenceType.METRIC,
        ),
    )
    classes = independent_signal_classes(store.all(INV))
    assert classes == {
        SignalClass.APPLICATION_LOG,
        SignalClass.DISTRIBUTED_TRACE,
        SignalClass.INFRASTRUCTURE_METRIC,
    }


def test_timeline_is_chronological_and_deterministic():
    store = InMemoryEvidenceStore()
    store.add(
        investigation_id=INV,
        build=make(provider=Provider.SPLUNK, content="third", timestamp=T0 + timedelta(seconds=2)),
    )
    store.add(
        investigation_id=INV,
        build=make(provider=Provider.SPLUNK, content="first", timestamp=T0),
    )
    store.add(
        investigation_id=INV,
        build=make(provider=Provider.SPLUNK, content="second", timestamp=T0 + timedelta(seconds=1)),
    )
    assert [e.content for e in store.timeline(INV)] == ["first", "second", "third"]


def test_hallucinated_citations_are_detectable():
    store = InMemoryEvidenceStore()
    store.add(investigation_id=INV, build=make(provider=Provider.SPLUNK))
    assert store.unknown_ids(INV, {"E1", "E99"}) == {"E99"}


def test_provenance_is_required_and_frozen():
    store = InMemoryEvidenceStore()
    evidence = store.add(investigation_id=INV, build=make(provider=Provider.SPLUNK))
    assert evidence.provenance.query == 'index=prod trace_id="4bf92f"'
    with pytest.raises(ValidationError):
        evidence.provenance.query = "index=* | delete"


def test_telemetry_outranks_documentation():
    store = InMemoryEvidenceStore()
    log = store.add(investigation_id=INV, build=make(provider=Provider.SPLUNK))
    runbook = store.add(
        investigation_id=INV,
        build=make(
            provider=Provider.DEMO,
            content="Redis pool exhaustion runbook",
            evidence_type=EvidenceType.RUNBOOK,
            signal_class=SignalClass.DOCUMENTATION,
        ),
    )
    assert log.is_production_telemetry and not log.is_documentation
    assert runbook.is_documentation and not runbook.is_production_telemetry


def test_malformed_evidence_id_is_rejected():
    with pytest.raises(ValidationError):
        Evidence(
            evidence_id="evidence-1",
            investigation_id=INV,
            evidence_type=EvidenceType.LOG,
            signal_class=SignalClass.APPLICATION_LOG,
            timestamp=T0,
            summary="s",
            content="c",
            fingerprint="f",
            provenance=provenance(Provider.SPLUNK, "q"),
        )
