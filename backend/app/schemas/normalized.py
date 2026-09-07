"""Normalized observability data model.

Every connector converts provider-specific payloads into these types before any
other layer sees them. Nothing downstream (correlation, evidence, LLM) is
allowed to know what Splunk or Datadog JSON looks like.

The model is aligned to OpenTelemetry concepts: Resource, Log, Span, Metric,
plus operational signals (Alert, Deployment, KubernetesEvent) that OTel does not
standardize but incident investigation needs.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Provider(str, Enum):
    """Observability platforms the system can talk to."""

    SPLUNK = "splunk"
    DATADOG = "datadog"
    NEWRELIC = "newrelic"
    GRAFANA = "grafana"
    LOKI = "loki"
    PROMETHEUS = "prometheus"
    ELASTIC = "elastic"
    OPENSEARCH = "opensearch"
    DYNATRACE = "dynatrace"
    CLOUDWATCH = "cloudwatch"
    AZURE_MONITOR = "azure_monitor"
    GCP_LOGGING = "gcp_logging"
    DEMO = "demo"


class Severity(str, Enum):
    """OTel severity buckets, collapsed to the levels operators actually use."""

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, raw: str | None) -> "Severity":
        """Map arbitrary provider level strings onto the canonical set."""
        if not raw:
            return cls.UNKNOWN
        token = raw.strip().upper()
        aliases = {
            "WARNING": cls.WARN,
            "ERR": cls.ERROR,
            "SEVERE": cls.ERROR,
            "CRITICAL": cls.FATAL,
            "CRIT": cls.FATAL,
            "EMERG": cls.FATAL,
            "ALERT": cls.FATAL,
            "NOTICE": cls.INFO,
            "VERBOSE": cls.DEBUG,
            "FINE": cls.DEBUG,
        }
        if token in aliases:
            return aliases[token]
        try:
            return cls(token)
        except ValueError:
            return cls.UNKNOWN


class SpanStatus(str, Enum):
    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


class SearchKeyType(str, Enum):
    """Identifier types an engineer can investigate with."""

    TRACE_ID = "trace_id"
    CORRELATION_ID = "correlation_id"
    REQUEST_ID = "request_id"
    TRANSACTION_ID = "transaction_id"
    SESSION_ID = "session_id"
    CUSTOM = "custom"
    NATURAL_LANGUAGE = "natural_language"


class TimeRange(BaseModel):
    """Half-open interval [start, end). All timestamps are timezone aware UTC."""

    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _require_tzinfo(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone aware")
        return value

    def model_post_init(self, _context: Any) -> None:
        if self.end <= self.start:
            raise ValueError("end must be after start")

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def expanded(self, seconds: float) -> "TimeRange":
        """Widen the window symmetrically for neighbouring-event lookups."""
        from datetime import timedelta

        delta = timedelta(seconds=seconds)
        return TimeRange(start=self.start - delta, end=self.end + delta)


class Resource(BaseModel):
    """Where a signal came from. OTel resource attributes, flattened."""

    service: str | None = None
    service_version: str | None = None
    environment: str | None = None
    host: str | None = None
    container: str | None = None
    pod: str | None = None
    namespace: str | None = None
    cluster: str | None = None
    region: str | None = None
    cloud_account: str | None = None

    def correlation_keys(self) -> dict[str, str]:
        """Non-empty fields usable as correlation join keys."""
        return {k: v for k, v in self.model_dump().items() if v}


class CorrelationKeys(BaseModel):
    """Identifiers that stitch signals across providers into one incident."""

    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    transaction_id: str | None = None
    session_id: str | None = None

    def non_empty(self) -> dict[str, str]:
        return {k: v for k, v in self.model_dump().items() if v}

    def intersects(self, other: "CorrelationKeys") -> bool:
        mine, theirs = self.non_empty(), other.non_empty()
        return any(mine[k] == theirs.get(k) for k in mine if k in theirs)


class SignalBase(BaseModel):
    """Common shape for every normalized signal."""

    provider: Provider
    integration_id: str
    timestamp: datetime
    resource: Resource = Field(default_factory=Resource)
    keys: CorrelationKeys = Field(default_factory=CorrelationKeys)
    attributes: dict[str, Any] = Field(default_factory=dict)
    raw_id: str | None = Field(
        default=None, description="Provider-native event identifier, for provenance"
    )
    source_url: str | None = Field(
        default=None, description="Deep link back into the provider UI"
    )

    @field_validator("timestamp")
    @classmethod
    def _require_tzinfo(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone aware")
        return value


class LogRecord(SignalBase):
    signal: Literal["log"] = "log"
    level: Severity = Severity.UNKNOWN
    message: str
    logger: str | None = None
    exception_type: str | None = None
    stack_trace: str | None = None


class Span(SignalBase):
    signal: Literal["span"] = "span"
    name: str
    duration_ms: float
    status: SpanStatus = SpanStatus.UNSET
    status_message: str | None = None
    kind: str | None = None
    peer_service: str | None = None
    http_status_code: int | None = None

    @property
    def end_time(self) -> datetime:
        from datetime import timedelta

        return self.timestamp + timedelta(milliseconds=self.duration_ms)


class Trace(BaseModel):
    """A full trace assembled from spans, possibly across providers."""

    trace_id: str
    spans: list[Span] = Field(default_factory=list)

    @property
    def root(self) -> Span | None:
        by_id = {s.keys.span_id: s for s in self.spans if s.keys.span_id}
        for span in sorted(self.spans, key=lambda s: s.timestamp):
            parent = span.keys.parent_span_id
            if not parent or parent not in by_id:
                return span
        return None

    @property
    def services(self) -> list[str]:
        seen: list[str] = []
        for span in sorted(self.spans, key=lambda s: s.timestamp):
            name = span.resource.service
            if name and name not in seen:
                seen.append(name)
        return seen

    def error_spans(self) -> list[Span]:
        return [s for s in self.spans if s.status is SpanStatus.ERROR]


class MetricPoint(SignalBase):
    signal: Literal["metric"] = "metric"
    metric: str
    value: float
    unit: str | None = None


class MetricSeries(BaseModel):
    metric: str
    unit: str | None = None
    points: list[MetricPoint] = Field(default_factory=list)

    def peak(self) -> MetricPoint | None:
        return max(self.points, key=lambda p: p.value) if self.points else None


class Alert(SignalBase):
    signal: Literal["alert"] = "alert"
    name: str
    state: str
    severity: Severity = Severity.UNKNOWN
    description: str | None = None


class Deployment(SignalBase):
    signal: Literal["deployment"] = "deployment"
    service: str
    version: str
    revision: str | None = None
    deployed_by: str | None = None


class KubernetesEvent(SignalBase):
    signal: Literal["k8s_event"] = "k8s_event"
    reason: str
    message: str
    object_kind: str | None = None
    object_name: str | None = None
    count: int = 1


class ServiceEdge(BaseModel):
    """One directed dependency between two services."""

    caller: str
    callee: str
    call_count: int = 0
    error_count: int = 0

    @property
    def error_rate(self) -> float:
        return self.error_count / self.call_count if self.call_count else 0.0


class ServiceDependencyGraph(BaseModel):
    edges: list[ServiceEdge] = Field(default_factory=list)

    def services(self) -> list[str]:
        seen: list[str] = []
        for edge in self.edges:
            for name in (edge.caller, edge.callee):
                if name not in seen:
                    seen.append(name)
        return seen

    def downstream_of(self, service: str) -> list[str]:
        return [e.callee for e in self.edges if e.caller == service]

    def upstream_of(self, service: str) -> list[str]:
        return [e.caller for e in self.edges if e.callee == service]


Signal = (
    LogRecord | Span | MetricPoint | Alert | Deployment | KubernetesEvent
)
"""Any normalized signal. Discriminated on the `signal` literal field."""
