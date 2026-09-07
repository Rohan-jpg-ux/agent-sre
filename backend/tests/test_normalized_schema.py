from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.schemas.normalized import (
    CorrelationKeys,
    LogRecord,
    Provider,
    Resource,
    ServiceDependencyGraph,
    ServiceEdge,
    Severity,
    Span,
    SpanStatus,
    TimeRange,
    Trace,
)

T0 = datetime(2026, 9, 4, 10, 14, 30, tzinfo=timezone.utc)


def window(seconds: int = 600) -> TimeRange:
    return TimeRange(start=T0, end=T0 + timedelta(seconds=seconds))


def test_naive_timestamps_are_rejected():
    with pytest.raises(ValidationError):
        TimeRange(start=datetime(2026, 9, 4, 10, 0), end=datetime(2026, 9, 4, 10, 30))


def test_inverted_range_is_rejected():
    with pytest.raises(ValueError):
        TimeRange(start=T0, end=T0 - timedelta(minutes=1))


def test_window_expansion_is_symmetric():
    expanded = window(60).expanded(120)
    assert expanded.start == T0 - timedelta(seconds=120)
    assert expanded.duration_seconds == 60 + 240


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("warning", Severity.WARN),
        ("CRITICAL", Severity.FATAL),
        ("err", Severity.ERROR),
        ("Notice", Severity.INFO),
        ("banana", Severity.UNKNOWN),
        (None, Severity.UNKNOWN),
    ],
)
def test_severity_normalisation_across_providers(raw, expected):
    assert Severity.parse(raw) is expected


def test_log_record_requires_aware_timestamp():
    with pytest.raises(ValidationError):
        LogRecord(
            provider=Provider.SPLUNK,
            integration_id="i1",
            timestamp=datetime(2026, 9, 4, 10, 14, 33),
            message="boom",
        )


def _span(name, service, offset_ms, duration_ms, span_id, parent=None, status=SpanStatus.OK):
    return Span(
        provider=Provider.DATADOG,
        integration_id="i1",
        timestamp=T0 + timedelta(milliseconds=offset_ms),
        name=name,
        duration_ms=duration_ms,
        status=status,
        resource=Resource(service=service, environment="production"),
        keys=CorrelationKeys(trace_id="4bf92f", span_id=span_id, parent_span_id=parent),
    )


def test_trace_finds_root_and_orders_services():
    trace = Trace(
        trace_id="4bf92f",
        spans=[
            _span("redis.get", "redis", 2000, 3200, "s4", "s3", SpanStatus.ERROR),
            _span("GET /checkout", "api-gateway", 0, 5300, "s1"),
            _span("POST /pay", "payment-service", 1200, 3900, "s3", "s2"),
            _span("POST /orders", "order-service", 200, 5000, "s2", "s1"),
        ],
    )
    assert trace.root is not None
    assert trace.root.resource.service == "api-gateway"
    assert trace.services == ["api-gateway", "order-service", "payment-service", "redis"]
    assert [s.resource.service for s in trace.error_spans()] == ["redis"]


def test_span_end_time_derives_from_duration():
    span = _span("redis.get", "redis", 0, 3200, "s4")
    assert (span.end_time - span.timestamp).total_seconds() == pytest.approx(3.2)


def test_correlation_keys_intersect_only_on_shared_field():
    a = CorrelationKeys(trace_id="abc", span_id="s1")
    b = CorrelationKeys(trace_id="abc", request_id="r9")
    c = CorrelationKeys(request_id="r9")
    assert a.intersects(b)
    assert not a.intersects(c)


def test_dependency_graph_walks_both_directions():
    graph = ServiceDependencyGraph(
        edges=[
            ServiceEdge(caller="api-gateway", callee="order-service", call_count=100),
            ServiceEdge(caller="order-service", callee="payment-service", call_count=100, error_count=24),
            ServiceEdge(caller="payment-service", callee="redis", call_count=300, error_count=42),
        ]
    )
    assert graph.downstream_of("order-service") == ["payment-service"]
    assert graph.upstream_of("payment-service") == ["order-service"]
    assert graph.edges[1].error_rate == pytest.approx(0.24)


def test_resource_correlation_keys_drop_empty_fields():
    resource = Resource(service="payment-service", pod="payment-7df91")
    assert resource.correlation_keys() == {
        "service": "payment-service",
        "pod": "payment-7df91",
    }
