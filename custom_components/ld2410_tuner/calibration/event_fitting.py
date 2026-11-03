"""Device fitting and fixed scoring through the shared hybrid facade."""

from math import inf

from .settings import DEFAULT_AUTOMATIC_WEIGHT


def assess(view, device_id, thresholds, timing=None, *, quality_context=None, start=-inf, end=inf):
    """One fixed pattern on the radar's labelled history, scored by the hybrid evaluator
    (never refitted); quality-corrected unless ``quality_context`` is False."""
    from ..engine.hybrid import facade

    return facade.assess(
        view, device_id, thresholds, timing, quality_context=quality_context, start=start, end=end
    )


def compare(view, device_id, context):
    """Saved and live patterns scored together on one hybrid fidelity plan (no learning)."""
    from ..engine.hybrid import facade

    return facade.compare(view, device_id, context)


def fit(
    view,
    device_id,
    keys,
    current,
    timing=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    constraints=None,
    start=-inf,
    end=inf,
):
    """Learn one radar's thresholds with the hybrid engine (``engine.hybrid.facade``)."""
    from ..engine.hybrid import facade

    return facade.fit(
        view,
        device_id,
        keys,
        current,
        timing,
        automatic_weight,
        constraints=constraints,
        start=start,
        end=end,
    )
