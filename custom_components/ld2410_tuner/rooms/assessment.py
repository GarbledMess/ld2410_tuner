"""Room assessment on shared recordings; no learning, threshold writes or live presence entity.

Scored by the hybrid fixed-pattern evaluator (``engine.hybrid.facade.measure``): each zone's
own truth, the common time-and-episode cost and Seen-by attribution, never a refit."""

from ..calibration.constants import ROOM_SCORER_VERSION

SCORER_VERSION = ROOM_SCORER_VERSION


def calculate(members, start, end, room=None, *, quality=None):
    """Assess ``members``; ``room`` is a ``truth.view`` snapshot (None: no room labels)."""
    from ..engine.hybrid import facade

    thresholds = {key: dict(item["thresholds"]) for key, item in members.items()}
    return facade.measure(members, thresholds, start, end, room, quality=quality)
