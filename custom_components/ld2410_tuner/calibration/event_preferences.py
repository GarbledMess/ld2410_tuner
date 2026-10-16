"""Gate preferences from elapsed energy mass, independent of event delivery density."""


def _quantile(histogram, fraction):
    target = sum(histogram) * fraction
    cumulative = 0.0
    for value, duration in enumerate(histogram):
        cumulative += duration
        if cumulative > target:
            return value
    return max(i for i, duration in enumerate(histogram) if duration)


def _histograms(cells, key, quality, scale=None):
    histograms = {
        (source, state): [0.0] * 101
        for source in ("human", "automatic")
        for state in ("present", "not_present")
    }
    for start, end, values, state, source, _confidence in cells:
        mass = end - start if scale is None else (end - start) * scale(start)
        if scale is not None and mass == 0:
            continue
        value = values.get(key)
        if value is None or (
            source == "automatic"
            and state == "present"
            and quality is not None
            and not quality.supports(key, value)
        ):
            continue
        histograms[source, state][value] += mass
    return histograms


def _preference(histograms, fallback, reference):
    humans = {state: histograms["human", state] for state in ("present", "not_present")}
    selected = {
        state: values if sum(values) or state in reference else histograms["automatic", state]
        for state, values in humans.items()
    }
    noise, presence = selected["not_present"], selected["present"]
    ceiling = max((i for i, duration in enumerate(noise) if duration), default=None)
    occupied = _quantile(presence, 0.25) if sum(presence) else None
    separated = ceiling is not None and occupied is not None and occupied > ceiling
    supported = separated and all(sum(values) > 0 for values in humans.values())
    preferred = int(fallback)
    if supported:
        preferred = (ceiling + occupied) // 2
    elif sum(noise):
        preferred = min(100, _quantile(noise, 0.99) + 2)
    return {
        "preferred_threshold": preferred,
        "noise_ceiling": ceiling,
        "presence_reference": occupied,
        "separated": separated,
        "human_supported": supported,
    }


def prepare(cells, keys, *, quality=None, scale=None):
    """Histogram evidence for a fixed set of cells and a fixed quality model.

    Reference policy changes select among these masses without changing them.
    Keep this bundle local to the objective that owns those cells, model and scale.
    """
    return {
        key: {
            source: tuple(mass) for source, mass in _histograms(cells, key, quality, scale).items()
        }
        for key in keys
    }


def preferences(cells, current, *, quality=None, reference=(), prepared=None, scale=None):
    """Cells are ``(start,end,values,state,source,confidence)`` with admitted labels.

    Confidence is deliberately absent from the quantile mass, matching existing
    gate preferences; elapsed physical time replaces the old observation count.
    Optional scale weights represented time; callers split cells at scale boundaries.
    """
    return {
        key: _preference(
            _histograms(cells, key, quality, scale) if prepared is None else prepared[key],
            value,
            reference,
        )
        for key, value in current.items()
    }
