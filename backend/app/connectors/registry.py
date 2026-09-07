"""Connector registry.

Adding a provider means writing one adapter class and decorating it. No routing
code, no agent code, and no frontend code changes. The capability matrix the UI
renders is derived from the registry at runtime.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Type

from app.connectors.base import Capability, ObservabilityConnector
from app.schemas.normalized import Provider

_REGISTRY: dict[Provider, Type[ObservabilityConnector]] = {}


def register(
    cls: Type[ObservabilityConnector] | None = None,
) -> Any:
    """Class decorator that adds a connector to the registry."""

    def _wrap(target: Type[ObservabilityConnector]) -> Type[ObservabilityConnector]:
        provider = getattr(target, "provider", None)
        if not isinstance(provider, Provider):
            raise TypeError(f"{target.__name__} must set a Provider on `provider`")
        if not target.capabilities:
            raise TypeError(f"{target.__name__} must declare at least one capability")
        existing = _REGISTRY.get(provider)
        if existing is not None and existing is not target:
            raise ValueError(f"{provider.value} already registered by {existing.__name__}")
        _REGISTRY[provider] = target
        return target

    return _wrap(cls) if cls is not None else _wrap


def build(provider: Provider, integration_id: str, config: dict[str, Any]) -> ObservabilityConnector:
    """Instantiate a connector for a configured integration."""
    try:
        cls = _REGISTRY[provider]
    except KeyError:
        raise LookupError(f"no connector registered for provider {provider.value}") from None
    return cls(integration_id=integration_id, config=config)


def registered_providers() -> list[Provider]:
    return sorted(_REGISTRY, key=lambda p: p.value)


def capabilities_for(provider: Provider) -> frozenset[Capability]:
    cls = _REGISTRY.get(provider)
    return cls.capabilities if cls else frozenset()


def capability_matrix() -> dict[str, dict[str, bool]]:
    """What the integrations page renders, and what the tool router plans against."""
    return {
        provider.value: {
            capability.value: capability in capabilities_for(provider)
            for capability in Capability
        }
        for provider in registered_providers()
    }


def providers_supporting(capability: Capability) -> list[Provider]:
    return [p for p in registered_providers() if capability in capabilities_for(p)]


def select_for(
    capability: Capability, candidates: Iterable[Provider]
) -> list[Provider]:
    """Filter user-selected providers down to those that can serve a capability."""
    return [p for p in candidates if capability in capabilities_for(p)]


def _reset_for_tests() -> None:
    _REGISTRY.clear()
