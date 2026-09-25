"""Data-derived preferred gate settings; actual recall constraints remain primary."""


def gate_preference(key, groups, automatic, fallback=50):
    human_noise = [row[1][key] for row in groups["not_present"] if key in row[1]]
    human_presence = [row[1][key] for row in groups["present"] if key in row[1]]
    noise = sorted(
        human_noise or [row[1][key] for row in automatic["not_present"] if key in row[1]]
    )
    presence = sorted(
        human_presence or [row[1][key] for row in automatic["present"] if key in row[1]]
    )
    ceiling = max(noise) if noise else None
    reference = presence[len(presence) // 4] if presence else None
    separated = ceiling is not None and reference is not None and reference > ceiling
    human_supported = separated and bool(human_noise) and bool(human_presence)
    preferred = int(fallback)
    if human_supported:
        # Centre the observed gap, rather than anchoring just above background.
        preferred = (ceiling + reference) // 2
    elif noise:
        preferred = min(100, noise[int((len(noise) - 1) * 0.99)] + 2)
    return {
        "preferred_threshold": preferred,
        "noise_ceiling": ceiling,
        "presence_reference": reference,
        "separated": separated,
        "human_supported": human_supported,
    }
