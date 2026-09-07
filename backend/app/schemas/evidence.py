"""Evidence model.

The evidence object is the unit of truth in this system. Nothing reaches the LLM
that is not an Evidence instance, and no claim leaves the system without
referencing evidence IDs that exist in the store.

Three properties matter and are enforced structurally rather than by prompting:

1. Provenance. Every object records the provider, the exact query, the search
   window, when it was retrieved, and what was redacted.
2. Immutability. Once written, an evidence object is frozen. Re-running a query
   later produces new objects, it never edits old ones.
3. Independence. Objects carry a content fingerprint so the same underlying
   application event ingested by three platforms is recognised as one signal and
   does not triple-count toward confidence.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.normalized import Provider, Resource, CorrelationKeys, Severity, TimeRange


class EvidenceType(str, Enum):
    LOG = "log"
    TRACE = "trace"
    SPAN = "span"
    METRIC = "metric"
    ALERT = "alert"
    DEPLOYMENT = "deployment"
    K8S_EVENT = "k8s_event"
    INFRA_METRIC = "infra_metric"
    EXCEPTION = "exception"
    SERVICE_HEALTH = "service_health"
    RUNBOOK = "runbook"
    ARCHITECTURE_DOC = "architecture_doc"
    HISTORICAL_INCIDENT = "historical_incident"
    POSTMORTEM = "postmortem"


PRODUCTION_TELEMETRY: frozenset[EvidenceType] = frozenset(
    {
        EvidenceType.LOG,
        EvidenceType.TRACE,
        EvidenceType.SPAN,
        EvidenceType.METRIC,
        EvidenceType.ALERT,
        EvidenceType.DEPLOYMENT,
        EvidenceType.K8S_EVENT,
        EvidenceType.INFRA_METRIC,
        EvidenceType.EXCEPTION,
        EvidenceType.SERVICE_HEALTH,
    }
)
"""Telemetry outranks documentation when deciding what actually happened."""

DOCUMENTATION_CONTEXT: frozenset[EvidenceType] = frozenset(
    {
        EvidenceType.RUNBOOK,
        EvidenceType.ARCHITECTURE_DOC,
        EvidenceType.HISTORICAL_INCIDENT,
        EvidenceType.POSTMORTEM,
    }
)


class SignalClass(str, Enum):
    """What kind of independent observation this is.

    Two evidence objects only corroborate each other if their signal classes
    differ. An application log shipped to Splunk and the same log forwarded to
    Datadog are both APPLICATION_LOG and count once.
    """

    APPLICATION_LOG = "application_log"
    DISTRIBUTED_TRACE = "distributed_trace"
    APPLICATION_METRIC = "application_metric"
    INFRASTRUCTURE_METRIC = "infrastructure_metric"
    ALERT_RULE = "alert_rule"
    CONTROL_PLANE_EVENT = "control_plane_event"
    DOCUMENTATION = "documentation"


class Provenance(BaseModel):
    """Where an evidence object came from and what was done to it."""

    model_config = ConfigDict(frozen=True)

    provider: Provider
    integration_id: str
    source_account: str | None = None
    query: str
    query_language: str | None = None
    search_window: TimeRange
    executed_at: datetime
    retrieved_at: datetime
    original_event_id: str | None = None
    transformations: tuple[str, ...] = ()
    redactions_applied: tuple[str, ...] = ()
    source_url: str | None = None


class Evidence(BaseModel):
    """One immutable, citable fact retrieved from an observability platform."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(pattern=r"^E\d+$")
    investigation_id: str
    evidence_type: EvidenceType
    signal_class: SignalClass
    timestamp: datetime
    summary: str = Field(description="One line, safe to render in a citation chip")
    content: str = Field(description="Redacted message, span detail, or metric value")
    resource: Resource = Field(default_factory=Resource)
    keys: CorrelationKeys = Field(default_factory=CorrelationKeys)
    level: Severity = Severity.UNKNOWN
    fingerprint: str = Field(description="Content hash used for cross-provider dedup")
    provenance: Provenance
    attributes: dict[str, Any] = Field(default_factory=dict)
    duplicate_of: str | None = Field(
        default=None,
        description="Set when this restates an earlier evidence object from another provider",
    )

    @property
    def is_production_telemetry(self) -> bool:
        return self.evidence_type in PRODUCTION_TELEMETRY

    @property
    def is_documentation(self) -> bool:
        return self.evidence_type in DOCUMENTATION_CONTEXT

    @property
    def counts_toward_confidence(self) -> bool:
        return self.duplicate_of is None


def content_fingerprint(
    *,
    signal_class: SignalClass,
    service: str | None,
    content: str,
    timestamp: datetime,
    bucket_ms: int = 1000,
) -> str:
    """Fingerprint an observation independently of which platform shipped it.

    The timestamp is bucketed because the same event carries slightly different
    ingest times per provider. Message text is normalised for whitespace and
    case so formatting differences between shippers do not defeat dedup.
    """
    bucket = int(timestamp.timestamp() * 1000) // bucket_ms
    normalized = " ".join(content.split()).lower()
    material = f"{signal_class.value}|{service or ''}|{normalized}|{bucket}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class ClaimStatus(str, Enum):
    """Verdict from the evidence validator. Nothing renders as fact below SUPPORTED."""

    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    INFERRED = "INFERRED"
    CONTRADICTED = "CONTRADICTED"
    UNSUPPORTED = "UNSUPPORTED"


class Assertion(str, Enum):
    """How a statement must be labelled in the UI. Never blend these."""

    OBSERVED = "observed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"
    RECOMMENDED = "recommended"


class Claim(BaseModel):
    """One technical statement plus the evidence it stands on."""

    claim_id: str
    text: str
    assertion: Assertion
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    status: ClaimStatus = ClaimStatus.UNSUPPORTED
    reasoning: str | None = None

    def cited_ids(self) -> set[str]:
        return set(self.supporting) | set(self.contradicting)


class Hypothesis(BaseModel):
    """A candidate root cause the agent actively tries to disprove."""

    hypothesis_id: str
    statement: str
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    missing: list[str] = Field(
        default_factory=list, description="Telemetry that would settle this, but is unavailable"
    )
    status: ClaimStatus = ClaimStatus.UNSUPPORTED
    confidence: float = 0.0
    confidence_rationale: str | None = None
