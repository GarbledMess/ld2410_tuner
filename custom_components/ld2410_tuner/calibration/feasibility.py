"""Which selected gate each labelled presence observation depends on."""


def exclusive_presence(positives, thresholds):
    """Count labelled presence observations that depend on exactly one selected gate."""
    values = {key: [] for key in thresholds}
    for row in positives:
        hits = [key for key, threshold in thresholds.items() if row[1].get(key, -1) > threshold]
        if len(hits) == 1:
            key = hits[0]
            values[key].append(row[1][key])
    return {
        key: {
            "exclusive_presence_samples": len(energies),
            "weakest_exclusive_presence_energy": min(energies) if energies else None,
        }
        for key, energies in values.items()
    }
