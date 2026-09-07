"""Evidence store.

Responsibilities in this phase:

* allocate stable evidence IDs, E1 upward, in retrieval order per investigation
* detect cross-provider duplicates by fingerprint and mark them, never drop them
* refuse mutation of an already recorded object
* answer the two lookups the rest of the system needs: by ID for citation
  rendering, and by predicate for correlation and ranking

The in-memory implementation is the reference and the one tests run against. The
PostgreSQL implementation lands with the persistence phase and must satisfy the
same protocol and the same test suite.
"""

from __future__ import annotations

import itertools
import threading
from typing import Callable, Iterable, Protocol

from app.schemas.evidence import Evidence, SignalClass


class EvidenceIntegrityError(Exception):
    """Raised when a write would alter recorded evidence."""


class EvidenceStore(Protocol):
    def add(self, *, investigation_id: str, build: Callable[[str], Evidence]) -> Evidence: ...

    def get(self, investigation_id: str, evidence_id: str) -> Evidence | None: ...

    def all(self, investigation_id: str) -> list[Evidence]: ...

    def exists(self, investigation_id: str, evidence_id: str) -> bool: ...


class InMemoryEvidenceStore:
    """Reference implementation. Thread safe, append only."""

    def __init__(self) -> None:
        self._by_investigation: dict[str, dict[str, Evidence]] = {}
        self._fingerprints: dict[str, dict[str, str]] = {}
        self._counters: dict[str, itertools.count] = {}
        self._lock = threading.Lock()

    def add(self, *, investigation_id: str, build: Callable[[str], Evidence]) -> Evidence:
        """Allocate the next ID and record the object the builder returns.

        The builder receives the allocated ID so the caller can construct a
        frozen Evidence in one shot. Duplicate detection runs after
        construction: if an equivalent observation from another provider is
        already recorded, the new object is stored with `duplicate_of` set so
        the audit trail keeps both, while confidence scoring counts one.
        """
        with self._lock:
            counter = self._counters.setdefault(investigation_id, itertools.count(1))
            evidence_id = f"E{next(counter)}"
            evidence = build(evidence_id)

            if evidence.evidence_id != evidence_id:
                raise EvidenceIntegrityError(
                    f"builder returned {evidence.evidence_id}, expected {evidence_id}"
                )
            if evidence.investigation_id != investigation_id:
                raise EvidenceIntegrityError("evidence investigation_id does not match")

            bucket = self._by_investigation.setdefault(investigation_id, {})
            if evidence_id in bucket:
                raise EvidenceIntegrityError(f"{evidence_id} already recorded")

            seen = self._fingerprints.setdefault(investigation_id, {})
            original = seen.get(evidence.fingerprint)
            if original is not None and evidence.duplicate_of is None:
                evidence = evidence.model_copy(update={"duplicate_of": original})
            elif original is None:
                seen[evidence.fingerprint] = evidence_id

            bucket[evidence_id] = evidence
            return evidence

    def get(self, investigation_id: str, evidence_id: str) -> Evidence | None:
        return self._by_investigation.get(investigation_id, {}).get(evidence_id)

    def exists(self, investigation_id: str, evidence_id: str) -> bool:
        return self.get(investigation_id, evidence_id) is not None

    def all(self, investigation_id: str) -> list[Evidence]:
        bucket = self._by_investigation.get(investigation_id, {})
        return sorted(bucket.values(), key=lambda e: int(e.evidence_id[1:]))

    def find(
        self, investigation_id: str, predicate: Callable[[Evidence], bool]
    ) -> list[Evidence]:
        return [e for e in self.all(investigation_id) if predicate(e)]

    def timeline(self, investigation_id: str) -> list[Evidence]:
        """Chronological order. Built deterministically, before the LLM sees anything."""
        return sorted(
            self.all(investigation_id), key=lambda e: (e.timestamp, int(e.evidence_id[1:]))
        )

    def unknown_ids(self, investigation_id: str, cited: Iterable[str]) -> set[str]:
        """IDs cited by a model response that do not exist. Used to reject answers."""
        return {cid for cid in cited if not self.exists(investigation_id, cid)}


def independent_signal_classes(evidence: Iterable[Evidence]) -> set[SignalClass]:
    """Distinct signal classes among non-duplicate evidence.

    Confidence is a function of how many independent classes agree, not how many
    rows were returned. Twelve copies of one log line is one signal.
    """
    return {e.signal_class for e in evidence if e.counts_toward_confidence}
