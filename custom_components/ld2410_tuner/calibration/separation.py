"""Data-derived preferred gate settings; actual recall constraints remain primary."""


def _readings(rows, key):
    """``(energy, weight)`` of the rows that read gate ``key``, sorted: a row weighs the time it
    held in six-second units (its fifth field), a sampled row one sample. Rows whose readings
    are kept as bytes in one layout (``evidence.Readings``) are read at the gate's position."""
    position = _position(rows, key)
    if position is None:
        return sorted((row[1][key], _weight(row)) for row in rows if key in row[1])
    if position is False:
        return []  # The layout has no such gate.
    return sorted(
        (row[1].values[position], _weight(row)) for row in rows if row[1].values[position] <= 100
    )


def _weight(row):
    return row[4] if len(row) > 4 else 1


def _position(rows, key):
    """The gate's position when every row's readings are bytes in one layout (False when
    that layout lacks it), else None."""
    from ..rooms.evidence import Readings

    if not rows or rows[0][1].__class__ is not Readings:
        return None
    layout = rows[0][1].layout
    if not all(row[1].__class__ is Readings and row[1].layout is layout for row in rows):
        return None
    position = layout.get(key)
    return False if position is None else position


def _at_rank(readings, rank):
    """The smallest energy whose cumulative weight exceeds ``rank``."""
    total = 0
    for energy, weight in readings:
        total += weight
        if total > rank:
            return energy
    return readings[-1][0]


def gate_preference(key, groups, automatic, fallback=50):
    human_noise = _readings(groups["not_present"], key)
    human_presence = _readings(groups["present"], key)
    noise = human_noise or _readings(automatic["not_present"], key)
    presence = human_presence or _readings(automatic["present"], key)
    noise_mass, presence_mass = sum(w for _, w in noise), sum(w for _, w in presence)
    ceiling = noise[-1][0] if noise else None
    reference = _at_rank(presence, int(presence_mass // 4)) if presence else None
    separated = ceiling is not None and reference is not None and reference > ceiling
    human_supported = separated and bool(human_noise) and bool(human_presence)
    preferred = int(fallback)
    if human_supported:
        # Centre the observed gap, rather than anchoring just above background.
        preferred = (ceiling + reference) // 2
    elif noise:
        preferred = min(100, _at_rank(noise, int((noise_mass - 1) * 0.99)) + 2)
    return {
        "preferred_threshold": preferred,
        "noise_ceiling": ceiling,
        "presence_reference": reference,
        "separated": separated,
        "human_supported": human_supported,
    }
