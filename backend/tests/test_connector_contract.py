from datetime import datetime, timedelta, timezone

import pytest

from app.connectors import registry
from app.connectors.base import (
    Capability,
    HealthStatus,
    ObservabilityConnector,
    ProviderResult,
    ProviderStatus,
)
from app.schemas.normalized import LogRecord, Provider, TimeRange

T0 = datetime(2026, 9, 4, 10, 14, 30, tzinfo=timezone.utc)
WINDOW = TimeRange(start=T0, end=T0 + timedelta(minutes=30))


class LogsOnlyConnector(ObservabilityConnector):
    """A provider that ships logs and nothing else. Loki, roughly."""

    provider = Provider.LOKI
    capabilities = frozenset({Capability.SEARCH_LOGS})
    query_language = "logql"

    async def test_connection(self) -> HealthStatus:
        return HealthStatus(healthy=True, checked_at=T0)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, checked_at=T0)

    async def search_logs(self, *, query, window, limit=1000, filters=None):
        record = LogRecord(
            provider=self.provider,
            integration_id=self.integration_id,
            timestamp=T0,
            message="RedisConnectionException: Unable to acquire connection from pool",
        )
        return ProviderResult(
            provider=self.provider,
            integration_id=self.integration_id,
            capability=Capability.SEARCH_LOGS,
            status=ProviderStatus.OK,
            data=[record],
        )


@pytest.fixture(autouse=True)
def clean_registry():
    registry._reset_for_tests()
    yield
    registry._reset_for_tests()


@pytest.mark.asyncio
async def test_unsupported_capability_degrades_instead_of_raising():
    connector = LogsOnlyConnector("i1", {})
    result = await connector.get_trace(trace_id="4bf92f", window=WINDOW)
    assert result.status is ProviderStatus.UNSUPPORTED
    assert result.status.is_failure is False
    assert result.data == []
    assert "does not support" in result.error_message


@pytest.mark.asyncio
async def test_declared_capability_returns_normalized_records():
    connector = LogsOnlyConnector("i1", {})
    result = await connector.search_logs(query='{app="payment"}', window=WINDOW)
    assert result.usable
    assert isinstance(result.data[0], LogRecord)
    assert result.data[0].provider is Provider.LOKI


def test_registry_rejects_connector_without_capabilities():
    class Empty(ObservabilityConnector):
        provider = Provider.ELASTIC

        async def test_connection(self):
            ...

        async def health_check(self):
            ...

    with pytest.raises(TypeError):
        registry.register(Empty)


def test_registry_rejects_duplicate_provider():
    registry.register(LogsOnlyConnector)

    class Other(LogsOnlyConnector):
        pass

    with pytest.raises(ValueError):
        registry.register(Other)


def test_router_selects_only_capable_providers():
    registry.register(LogsOnlyConnector)
    selected = registry.select_for(
        Capability.GET_TRACE, [Provider.LOKI, Provider.DATADOG]
    )
    assert selected == []
    assert registry.select_for(Capability.SEARCH_LOGS, [Provider.LOKI]) == [Provider.LOKI]


def test_capability_matrix_is_derived_not_hardcoded():
    registry.register(LogsOnlyConnector)
    matrix = registry.capability_matrix()
    assert matrix["loki"]["search_logs"] is True
    assert matrix["loki"]["get_trace"] is False
    assert set(matrix["loki"]) == {c.value for c in Capability}


def test_building_unregistered_provider_is_explicit():
    with pytest.raises(LookupError):
        registry.build(Provider.SPLUNK, "i9", {})


def test_failure_statuses_are_distinguished_from_unsupported():
    assert ProviderStatus.RATE_LIMITED.is_failure
    assert ProviderStatus.TIMEOUT.is_failure
    assert not ProviderStatus.PARTIAL.is_failure
    assert not ProviderStatus.UNSUPPORTED.is_failure
