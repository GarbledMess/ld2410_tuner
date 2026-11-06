"""Hand-written recorded histories for tests of what the worker does with one radar's days."""

import test_tuner
from test_rooms import member


def view(stretches, labels, runs=()):
    """A radar labelled ``labels`` whose recorded history is ``stretches`` and guess ``runs``."""
    from tuner_under_test.history import dayfile

    result = member("any-name", [], labels)["view"]
    for events, end in stretches:
        test_tuner.recorded(result, "any-name", events, end)
    for start, end, state, level, basis in runs:
        result._days.append("any-name", dayfile.Guess(start, state, level, basis))
        result._days.append("any-name", dayfile.Guess(end, None, 0, 0))
    result.hass = None
    result.data["room_snapshot"] = None
    return result


def block(start, end, value):
    return [(start, (value,) + (None,) * 17)], end
