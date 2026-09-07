"""Connector contract.

Every observability platform is reached through this interface and nothing else.
The AI orchestrator never imports a provider module, never sees provider JSON,
and never holds a credential. It asks the router for a capability and gets
normalized signals back.

Two rules make the platform survivable in production:

Capability negotiation. Providers do not offer the same operations. Loki has no
traces. Prometheus has no logs. A connector declares what it supports and the
router skips the rest instead of raising.

Partial failure is normal. Every operation returns a ProviderResult, not a bare
list. A Datadog 429 degrades the investigation, it does not fail it, and the
final report names the provider that was unavailable.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, TypeVar

from app.schemas.normalized import (
    Alert,
    Deployment,
    KubernetesEvent,
    LogRecord,
    MetricSeries,
    Provider,
    ServiceDependencyGraph,
    Span,
    TimeRange,
    Trace,
)

T = TypeVar("T")


class Capability(str, Enum):
    """Operations a connector may implement. Maps one to one onto agent tools."""

    SEARCH_LOGS = "search_logs"
    GET_TRACE = "get_trace"
    GET_SPANS = "get_spans"
    GET_SERVICE_METRICS = "get_service_metrics"
    GET_INFRASTRUCTURE_METRICS = "get_infrastructure_metrics"
    GET_ERRORS = "get_errors"
    GET_ALERTS = "get_alerts"
    GET_DEPLOYMENTS = "get_deployments"
    GET_KUBERNETES_EVENTS = "get_kubernetes_events"
    GET_SERVICE_DEPENDENCIES = "get_service_dependencies"
    GET_SERVICE_METADATA = "get_service_metadata"
    SEARCH_RELATED_INCIDENTS = "search_related_incidents"


class ProviderStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    CIRCUIT_OPEN = "circuit_open"
    ERROR = "error"

    @property
    def is_failure(self) -> bool:
        return self not in (ProviderStatus.OK, ProviderStatus.PARTIAL, ProviderStatus.UNSUPPORTED)


@dataclass(frozen=True)
class ExecutedQuery:
    """The literal query sent to the provider. Surfaced in the View Queries panel."""

    provider: Provider
    integration_id: str
    capability: Capability
    query: str
    query_language: str
    window: TimeRange
    executed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    """Outcome of one connector call. Never raises past the router."""

    provider: Provider
    integration_id: str
    capability: Capability
    status: ProviderStatus
    data: list[T] = field(default_factory=list)
    queries: tuple[ExecutedQuery, ...] = ()
    truncated: bool = False
    error_message: str | None = None
    latency_ms: float | None = None

    @property
    def usable(self) -> bool:
        return self.status in (ProviderStatus.OK, ProviderStatus.PARTIAL) and bool(self.data)

    @classmethod
    def unsupported(
        cls, provider: Provider, integration_id: str, capability: Capability
    ) -> "ProviderResult[T]":
        return cls(
            provider=provider,
            integration_id=integration_id,
            capability=capability,
            status=ProviderStatus.UNSUPPORTED,
            error_message=f"{provider.value} does not support {capability.value}",
        )


class ConnectorError(Exception):
    """Base class for connector failures. Caught and mapped to ProviderStatus."""


class AuthenticationError(ConnectorError):
    pass


class RateLimitError(ConnectorError):
    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class QueryRejectedError(ConnectorError):
    """Raised by the safety layer when a generated query is not read-only."""


@dataclass(frozen=True)
class HealthStatus:
    healthy: bool
    checked_at: datetime
    latency_ms: float | None = None
    detail: str | None = None


class ObservabilityConnector(abc.ABC):
    """Base class for every provider adapter.

    Subclasses declare `provider` and `capabilities`, then implement only the
    operations they declared. The default implementations return an
    `unsupported` result, so a connector that supports logs alone is a valid
    connector and the router will simply not ask it for traces.
    """

    provider: Provider
    capabilities: frozenset[Capability] = frozenset()
    query_language: str = "unknown"

    def __init__(self, integration_id: str, config: dict[str, Any]) -> None:
        self.integration_id = integration_id
        self._config = config

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @abc.abstractmethod
    async def test_connection(self) -> HealthStatus:
        """Validate credentials and reachability. Called from the config screen."""

    @abc.abstractmethod
    async def health_check(self) -> HealthStatus:
        """Cheap liveness probe used by the provider health panel and breaker."""

    async def close(self) -> None:
        """Release pooled clients. Safe to call more than once."""

    async def search_logs(
        self,
        *,
        query: str,
        window: TimeRange,
        limit: int = 1000,
        filters: dict[str, str] | None = None,
    ) -> ProviderResult[LogRecord]:
        return ProviderResult.unsupported(
            self.provider, self.integration_id, Capability.SEARCH_LOGS
        )

    async def get_trace(self, *, trace_id: str, window: TimeRange) -> ProviderResult[Trace]:
        return ProviderResult.unsupported(self.provider, self.integration_id, Capability.GET_TRACE)

    async def get_spans(
        self, *, trace_id: str, window: TimeRange
    ) -> ProviderResult[Span]:
        return ProviderResult.unsupported(self.provider, self.integration_id, Capability.GET_SPANS)

    async def get_service_metrics(
        self, *, service: str, metrics: list[str], window: TimeRange
    ) -> ProviderResult[MetricSeries]:
        return ProviderResult.unsupported(
            self.provider, self.integration_id, Capability.GET_SERVICE_METRICS
        )

    async def get_infrastructure_metrics(
        self, *, resource_filter: dict[str, str], metrics: list[str], window: TimeRange
    ) -> ProviderResult[MetricSeries]:
        return ProviderResult.unsupported(
            self.provider, self.integration_id, Capability.GET_INFRASTRUCTURE_METRICS
        )

    async def get_errors(
        self, *, service: str | None, window: TimeRange, limit: int = 500
    ) -> ProviderResult[LogRecord]:
        return ProviderResult.unsupported(self.provider, self.integration_id, Capability.GET_ERRORS)

    async def get_alerts(
        self, *, window: TimeRange, service: str | None = None
    ) -> ProviderResult[Alert]:
        return ProviderResult.unsupported(self.provider, self.integration_id, Capability.GET_ALERTS)

    async def get_deployments(
        self, *, window: TimeRange, service: str | None = None
    ) -> ProviderResult[Deployment]:
        return ProviderResult.unsupported(
            self.provider, self.integration_id, Capability.GET_DEPLOYMENTS
        )

    async def get_kubernetes_events(
        self, *, window: TimeRange, namespace: str | None = None
    ) -> ProviderResult[KubernetesEvent]:
        return ProviderResult.unsupported(
            self.provider, self.integration_id, Capability.GET_KUBERNETES_EVENTS
        )

    async def get_service_dependencies(
        self, *, service: str | None, window: TimeRange
    ) -> ProviderResult[ServiceDependencyGraph]:
        return ProviderResult.unsupported(
            self.provider, self.integration_id, Capability.GET_SERVICE_DEPENDENCIES
        )

    def source_url(self, **_: Any) -> str | None:
        """Deep link back into the provider UI. Optional per provider."""
        return None
