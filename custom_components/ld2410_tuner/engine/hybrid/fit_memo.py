"""A device fit over unchanged evidence is searched once ever.

A fit depends only on its problem: the readings and labels its quality fingerprint hashes,
the resolved truth, the radar's settings and policy, and this code. Those form the key, so a
changed day, label or setting is simply a different fit. The memory holds only what a report
takes from a fit, beside the finished days under their result budget.

A whole room job goes further. A worker view is its data, its cutoffs and its days and nothing
else (``worker.calculations.restore``), so those with the job's parameters are every input of a
room report: the report is kept under them, and an unchanged job reads no row at all. The job's
open days are private copies, so they are told apart by their bytes.
"""

import json
import os
import tempfile
import zlib
from functools import lru_cache
from hashlib import blake2b
from pathlib import Path

from ...history import summary_cache
from ...history.fit_days import _cache_root
from ...worker import codec
from ...worker.capture import ATTRIBUTES
from . import engine
from .constants import WARMUP_SECONDS
from .types import FitResult

_SUFFIX = ".fit"
_SOURCES = ("calibration", "engine", "history", "rooms", "worker")
_POLICY_OMITS = ("models",)
_MAX_BYTES = 1024 * 1024


@lru_cache(maxsize=1)
def _sources():
    root = Path(__file__).resolve().parents[2]
    digest = blake2b(digest_size=16)
    for folder in _SOURCES:
        for path in sorted((root / folder).rglob("*.py")):
            digest.update(path.read_bytes())
    digest.update((root / "const.py").read_bytes())
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
    # What ``engine.fit`` searches: nothing past the last label's region reaches it.
    searched = engine.prepared(problem)
    settings = (
        problem.start,
        searched.start,
        searched.end,
        sorted(problem.radars.items()),
        searched.zones,
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


def _recall(path, decode):
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        result = decode(path.read_bytes())
        os.utime(path)  # The result budget removes the oldest first.
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError, zlib.error):
        return None


def _remember(path, root, encode):
    temporary = None
    try:
        raw = encode()
        if len(raw) > _MAX_BYTES:
            return
        with summary_cache._lock:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".fittmp", mode="wb", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            os.replace(temporary, path)
            summary_cache.prune_after(root, path)
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if temporary is not None:
            summary_cache._remove(temporary)


def _fitter(place):
    """``engine.fit``, remembered at ``place``; without one, a plain search."""

    def fit(problem, **options):
        if place is None:
            return engine.fit(problem, **options)
        summary_cache.worker_only()
        folder, root = place
        path = folder / (_key(problem) + _SUFFIX)
        found = _recall(path, lambda raw: _decoded(raw, problem))
        if found is not None:
            return found
        result = engine.fit(problem, **options)
        _remember(path, root, lambda: _encoded(result))
        return result

    return fit


def fitter(view, device_id):
    """``engine.fit`` for this radar, remembering results beside its finished days."""
    return _fitter(_place(view, device_id))


def _first_place(views):
    """Where the first member with a finished day remembers; None when none has one."""
    found = (_place(views[key], key) for key in sorted(views))
    return next(filter(None, found), None)


def room_fitter(groups):
    """``engine.fit`` for a linked set, remembering results beside a member's finished days."""
    members = {key: item["view"] for found in groups.values() for key, item in found.items()}
    return _fitter(_first_place(members))


def _day_identity(days, device_id, date):
    entry = days.entries[device_id, date]
    found = days.result_identity(device_id, date)
    if found is None and entry["path"] is not None:
        found = blake2b(Path(entry["path"]).read_bytes(), digest_size=20).hexdigest()
    return repr((device_id, date, found, entry["late"])).encode()


def _job_key(views, parameters):
    """Every input of a room job: its parameters, and each view's data, cutoffs and days."""
    digest = blake2b(_producer().encode(), digest_size=20)
    digest.update(repr(parameters).encode())
    for name in sorted(views):
        view, days = views[name], views[name]._days
        attributes = {key: getattr(view, key, None) for key in ATTRIBUTES}
        digest.update(repr((name, view.data, attributes, days.budgets)).encode())
        digest.update(repr(sorted(days.live_dates.items())).encode())
        for device_id, date in sorted(days.entries):
            digest.update(_day_identity(days, device_id, date))
    return digest.hexdigest()


def _encoded_report(report):
    return zlib.compress(codec.pack_result(report), 6)


def _decoded_report(raw):
    stream = zlib.decompressobj()
    text = stream.decompress(raw, _MAX_BYTES + 1)  # A report is one bounded worker response.
    if not stream.eof:
        raise ValueError("Incomplete or oversized room memory")
    return codec.unpack_result(text)


def remembered_room(views, parameters, compute):
    """``compute()``'s room report, once for each distinct job."""
    place = _first_place(views)
    if place is None:
        return compute()
    summary_cache.worker_only()
    try:
        key = _job_key(views, parameters)
    except OSError:  # The job itself will say what is wrong with its days.
        return compute()
    folder, root = place
    path = folder / (key + _SUFFIX)
    found = _recall(path, _decoded_report)
    if found is not None:
        return found
    report = compute()
    _remember(path, root, lambda: _encoded_report(report))
    return report
