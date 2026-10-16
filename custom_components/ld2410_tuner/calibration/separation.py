"""Data-derived preferred gate settings; actual recall constraints remain primary."""


def _readings(rows, key):
    """``(energy, weight)`` of the rows that read gate ``key``, sorted: a row weighs the time it
    held in six-second units (its fifth field), a sampled row one sample."""
    return sorted((row[1][key], row[4] if len(row) > 4 else 1) for row in rows if key in row[1])


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
