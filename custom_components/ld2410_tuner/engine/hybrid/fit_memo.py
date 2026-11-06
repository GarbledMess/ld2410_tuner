"""A device fit over unchanged evidence is searched once ever.

A fit depends only on its problem: the readings and labels its quality fingerprint hashes,
the resolved truth, the radar's settings and policy, and this code. Those form the key, so a
changed day, label or setting is simply a different fit. The memory holds only what a device
report takes from a fit, beside the finished days under their result budget.
"""

import json
import os
import tempfile
from functools import lru_cache
from hashlib import blake2b
from pathlib import Path

from ...history import summary_cache
from ...history.fit_days import _cache_root
from . import engine
from .constants import WARMUP_SECONDS
from .types import FitResult

_SUFFIX = ".fit"
_SOURCES = ("calibration", "engine", "history", "rooms")
_POLICY_OMITS = ("models",)
_MAX_BYTES = 1024 * 1024


@lru_cache(maxsize=1)
def _sources():
    root = Path(__file__).resolve().parents[2]
    digest = blake2b(digest_size=16)
    for folder in _SOURCES:
        for path in sorted((root / folder).rglob("*.py")):
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _producer():
    return _sources()


def _place(view, device_id):
    """The first finished day's folder and cache root; live days hold no memory."""
    days = view._days
    found = getattr(days, "result_identity", None)
    if found is None:
        return None
    for date in days.days(device_id):
        persistent = found(device_id, date)
        if persistent is not None:
            path = Path(persistent[0])
            return path.parent, _cache_root(days, path)
    return None


def _key(problem):
    digest = blake2b(_producer().encode(), digest_size=20)
    policy = problem.policy
    quality = {k: v for k, v in policy.quality.items() if k not in _POLICY_OMITS}
    settings = (
        problem.start,
        problem.end,
        sorted(problem.radars.items()),
        problem.zones,
        policy.automatic_weight,
        policy.reference,
        policy.automatic_horizon_days,
        policy.human_evidence_hours,
        quality,
    )
    digest.update(repr(settings).encode())
    for rid in sorted(problem.radars):
        digest.update(policy.quality["models"][rid].context.fingerprint.encode())
        for block in problem.rows[rid].blocks(problem.start - WARMUP_SECONDS, problem.start):
            digest.update(block.times.tobytes() + block.ends.tobytes())
            digest.update(b"".join(block.columns))
    return digest.hexdigest()


def _encoded(result):
    keep = ("quiet_cost", "selection_cost", "anomaly")
    diagnostics = {k: result.diagnostics[k] for k in keep if k in result.diagnostics}
    return json.dumps({"thresholds": result.thresholds, "diagnostics": diagnostics}).encode()


def _decoded(raw, problem):
    value = json.loads(raw)
    diagnostics = value["diagnostics"]
    costs = diagnostics["quiet_cost"], diagnostics["selection_cost"]
    if not all(isinstance(cost, (int, float)) for cost in costs):
        raise ValueError("Invalid fit costs")
    thresholds = {
        rid: {int(channel): int(t) for channel, t in row.items()}
        for rid, row in value["thresholds"].items()
    }
    expected = {rid: set(radar.channels) for rid, radar in problem.radars.items()}
    if {rid: set(row) for rid, row in thresholds.items()} != expected:
        raise ValueError("Fit memory is for other gates")
    return FitResult(thresholds, None, None, diagnostics)


def _recall(path, problem):
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        result = _decoded(path.read_bytes(), problem)
        os.utime(path)  # The result budget removes the oldest first.
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _remember(path, root, result):
    temporary = None
    try:
        raw = _encoded(result)
        if len(raw) > _MAX_BYTES:
            return
        with summary_cache._lock:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".fittmp", mode="wb", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            os.replace(temporary, path)
            summary_cache.prune(root)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary is not None:
            summary_cache._remove(temporary)


def fitter(view, device_id):
    """``engine.fit`` for this radar, remembering results beside its finished days."""
    place = _place(view, device_id)

    def fit(problem):
        if place is None:
            return engine.fit(problem)
        summary_cache.worker_only()
        folder, root = place
        path = folder / (_key(problem) + _SUFFIX)
        found = _recall(path, problem)
        if found is not None:
            return found
        result = engine.fit(problem)
        _remember(path, root, result)
        return result

    return fit
