"""Shared immutable fitting contracts; channel indexes follow HISTORY_KEYS."""

from __future__ import annotations

from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Protocol

from .constants import AUTOMATIC_HORIZON_DAYS, HUMAN_EVIDENCE_HOURS

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
