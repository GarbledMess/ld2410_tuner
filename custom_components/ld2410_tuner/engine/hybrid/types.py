"""Shared immutable fitting contracts; channel indexes follow HISTORY_KEYS."""

from __future__ import annotations

from array import array
from collections.abc import Iterator
from dataclasses import dataclass, field
from dataclasses import replace as _replace
from datetime import date
from typing import TYPE_CHECKING, Protocol

from .constants import AUTOMATIC_HORIZON_DAYS, HUMAN_EVIDENCE_HOURS, MISSED_WEIGHT

if TYPE_CHECKING:
    from .digest import DayDigest

Pattern = dict[str, dict[int, int]]


@dataclass(frozen=True, slots=True)
class RowBlock:
    times: array
    ends: array
    columns: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class TruthSpan:
    start: float
    end: float
    state: str
    source: str
    confidence: float
    episode_id: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RadarSpec:
    device_id: str
    channels: tuple[int, ...]
    domains: dict[int, tuple[int, ...]]
    current: dict[int, int]
    timing: dict


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    group_id: str
    members: tuple[str, ...]
    truth: tuple[TruthSpan, ...]


@dataclass(frozen=True, slots=True)
class EvidencePolicy:
    automatic_weight: float
    reference: bool
    quality: dict
    automatic_horizon_days: float | None = AUTOMATIC_HORIZON_DAYS
    human_evidence_hours: float | None = HUMAN_EVIDENCE_HOURS
    # The weight of a missed presence fraction against a false-active one for the zone this
    # policy scores; ``missed_weights`` holds each zone's own (its presence priority).
    missed_weight: float = MISSED_WEIGHT
    missed_weights: dict = field(default_factory=dict)

    def for_zone(self, group_id):
        """This policy with ``group_id``'s missed weight (a report variant uses its base zone)."""
        base = group_id.split("\x00", 1)[0] if isinstance(group_id, str) else group_id
        weight = self.missed_weights.get(base, self.missed_weight)
        return self if weight == self.missed_weight else _replace(self, missed_weight=weight)


@dataclass(frozen=True, slots=True)
class FitProblem:
    radars: dict[str, RadarSpec]
    zones: tuple[ZoneSpec, ...]
    start: float
    end: float
    days: object
    rows: dict[str, object]
    policy: EvidencePolicy


@dataclass(frozen=True, slots=True)
class ScorePlan:
    mode: str
    regions: tuple[tuple[float, float], ...]
    row_count: int
    finalist_limit: int


@dataclass(frozen=True, slots=True)
class FitResult:
    thresholds: Pattern
    before: dict
    after: dict
    diagnostics: dict


class RowSource(Protocol):
    def blocks(self, lo: float, hi: float) -> Iterator[RowBlock]: ...

    def rows_in(self, lo: float, hi: float) -> int: ...


class DaySource(Protocol):
    def dates(self, device_id: str) -> Iterator[date]: ...

    def digest(self, device_id: str, date: date, channels: tuple[int, ...]) -> DayDigest | None: ...
