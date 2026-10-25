"""Readings stated as rows ``(time, {gate: energy}, label)``, as the recorded cells, evidence
rows and attributions the integration builds from recorded history."""

import test_tuner  # noqa: F401  (registers tuner_under_test)
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.rooms import attribution, event_attribution

STEP = 6  # Each stated reading holds this long (one sample of the sampled recorder).


def _gates(rows):
    """Each named gate's position in a reading: the standard gates where named so, others in
    the order they first appear (tests may name gates ``a`` and ``b``)."""
    names = dict.fromkeys(key for _, gates, *_ in rows for key in gates)
    free = iter(index for index, key in enumerate(HISTORY_KEYS) if key not in names)
    return {key: HISTORY_KEYS.index(key) if key in HISTORY_KEYS else next(free) for key in names}


def _ends(times, step):
    """Each reading holds until the next one, at most ``step`` seconds: readings up to
    ``step`` apart are one run, further apart leave a gap."""
    ends = [*times[1:], float("inf")] if times else []
    return [min(after, now + step) for now, after in zip(times, ends, strict=True)]


def cells_of(rows, step=STEP):
    """The rows as recorded cells ``(start, end, readings)`` (``_ends``)."""
    gates = _gates(rows)
    ends = _ends([row[0] for row in rows], step)
    cells = []
    for (timestamp, values, *_), end in zip(rows, ends, strict=True):
        vector = [None] * len(HISTORY_KEYS)
        for key, value in values.items():
            vector[gates[key]] = value
        cells.append((timestamp, end, tuple(vector)))
    return cells


def baseline_of(rows, quiet_at):
    """The radar's empty baseline from the readings of ``rows`` where ``quiet_at``."""
    cells = cells_of(rows)
    return event_attribution._baseline(cells, [quiet_at(cell[0]) for cell in cells])


def attribute(rows, episode, base, radar_label_at):
    """Whether the radar saw ``episode`` (its first and last reading times): its human
    label, then its baseline."""
    inside = [cell for cell in cells_of(rows) if episode[0] <= cell[0] <= episode[1]]
    result = event_attribution._decide(inside, radar_label_at, base)
    return {"state": result["state"], "confidence": result["confidence"]}


def episodes_of(cells, read):
    """The runs of contiguous ``cells`` that ``read`` answers Present at."""
    labels = [read(cell[0]) for cell in cells]
    return [cells[first:stop] for first, stop in event_attribution._episodes(cells, labels)]


def found_of(radar, rows, radar_label_at, group_truths):
    """``radar``'s attributions over each group's episodes, as ``radar_attribution`` finds
    them (``weighted_member_target``'s ``found``)."""
    cells = cells_of(rows)
    quiet_at = attribution.quiet_reader(radar_label_at, group_truths)
    base = event_attribution._baseline(cells, [quiet_at(cell[0]) for cell in cells])
    return {
        group: {
            radar: [
                (
                    (episode[0][0], event_attribution._closed(episode[-1][1])),
                    event_attribution._decide(episode, radar_label_at, base),
                )
                for episode in episodes_of(cells, read)
            ]
        }
        for group, read in group_truths.items()
    }


def rows_of(rows):
    """The rows ``(time, {gate: energy}, label)`` as ``evidence.Rows`` (gates in every row),
    each reading held until the next (``_ends``)."""
    from array import array

    from tuner_under_test.rooms import evidence

    keys = list(rows[0][1]) if rows else []
    return evidence.Rows(
        array("d", (row[0] for row in rows)),
        [bytes(row[1][key] for row in rows) for key in keys],
        {key: position for position, key in enumerate(keys)},
        ends=array("d", _ends([row[0] for row in rows], STEP)),
    )
