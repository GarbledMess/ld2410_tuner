"""Room and linked fitting through the shared hybrid facade."""

from ..calibration.constants import ROOM_METHOD
from ..calibration.settings import DEFAULT_AUTOMATIC_WEIGHT

METHOD = ROOM_METHOD


def measure(members, thresholds, start, end, room=None, *, quality=None):
    """One fixed pattern assessed on ``room`` truth by the hybrid evaluator (no refit)."""
    from ..engine.hybrid import facade

    return facade.measure(members, thresholds, start, end, room, quality=quality)


def fit(
    members,
    start,
    end,
    room=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    quality_contexts=None,
):
    """One zone's joint fit: exactly ``fit_set`` of that zone, without ``groups``."""
    from ..engine.hybrid import facade

    return facade.room_fit(
        members, start, end, room, automatic_weight, quality_contexts=quality_contexts
    )


def fit_set(
    groups,
    start,
    end,
    rooms=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    quality_contexts=None,
):
    """Fit a learning set together with the hybrid engine.

    ``groups`` is ``{group_id: members}``; ``rooms`` is ``{group_id: truth.view snapshot}``
    (missing: no room labels). Each zone is scored on its own room truth; zones without
    observed labelled time in both classes are skipped (``groups[gid] == {"skipped":
    reason}``) and the set is fitted on the rest. A radar serving only skipped zones keeps
    its current threshold and is not searched. Raises only when no zone can be scored.

    Returns ``fit``'s keys plus ``groups``. ``cost_before``/``cost_after`` are sums of the
    scored zones' common time-and-episode costs. A one-zone set is identical to ``fit``.
    For several zones the top-level ``before``/``after`` have ``room`` None and per-radar
    measures only: callers must use ``groups`` for per-zone results."""
    from ..engine.hybrid import facade

    return facade.fit_set(
        groups, start, end, rooms, automatic_weight, quality_contexts=quality_contexts
    )
