"""Validate user-supplied exports without modifying history or disclosing identities.

Usage: python tools/validate_hybrid.py --history DIRECTORY --output DIRECTORY
Input: v2 radar export JSON files, metadata.json with a history/ day-store directory,
or an HA .storage directory (ld2410_tuner.data and ld2410_tuner/history/).
Embedded day files use the integration's lossless wire format. An optional baseline
is a frozen report.json from this protocol; its thresholds are re-scored without fitting.
All derived caches live in a detached temporary directory. No production routing is used.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import importlib
import importlib.util
import json
import math
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
import tracemalloc
from array import array
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "ld2410_tuner"
PROTOCOL = "exp2-cv-v2"
STATES = ("present", "not_present")
TOLERANCES = {
    "absolute_metric": 1e-7,
    "cpu_seconds": 10.0,
    "traced_peak_bytes": 64 * 1024**2,
    "rss_bytes": 512 * 1024**2,
}
ADDITIVE = (
    "occupied_seconds",
    "empty_seconds",
    "occupied_episodes",
    "empty_periods",
    "mostly_missed_episodes",
    "false_active_seconds",
    "false_triggers",
    "empty_periods_with_false_trigger",
    "empty_periods_mostly_false_active",
    "empty_periods_failed",
    "uncovered_label_seconds",
    "covered_seconds",
)


def modules():
    """Load the same HA-free namespace as the isolated production worker."""
    name = "ld2410_validation"
    if name not in sys.modules:
        package = ModuleType(name)
        package.__path__ = [str(PACKAGE)]
        sys.modules[name] = package
    return SimpleNamespace(
        **{
            key: importlib.import_module(name + "." + path)
            for key, path in {
                "adapters": "engine.hybrid.adapters",
                "engine": "engine.hybrid.engine",
                "evaluate": "engine.hybrid.evaluate",
                "exact": "engine.hybrid.exact",
                "types": "engine.hybrid.types",
                "constants": "engine.hybrid.constants",
                "days": "worker.calculations",
                "labels": "history.label_lookup",
                "dayfile": "history.dayfile",
                "daystore": "history.daystore",
                "keys": "const",
                "fit_rows": "history.fit_rows",
            }.items()
        }
    )


def referee():
    """Use the independent test oracle, never the old production search."""
    name = "hybrid_validation_referee"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / "hybrid_referee.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def private_path(path):
    parts = [p.lower() for p in path.parts]
    return any(a == "tests" and b == "data" for a, b in zip(parts, parts[1:], strict=False))


def checked_paths(history, output, baseline):
    history, output = Path(history).resolve(), Path(output).resolve()
    if not history.is_dir():
        raise ValueError("A readable user history directory is required")
    if _in_repository(output) or output.is_relative_to(history) or private_path(output):
        raise ValueError("Output must be outside the repository and input root")
    if history.is_relative_to(output):
        raise ValueError("Output must not contain the input root")
    if baseline is not None:
        baseline = Path(baseline).resolve()
        if _in_repository(baseline) or baseline.is_relative_to(history) or private_path(baseline):
            raise ValueError("Baseline must be outside the repository and input root")
    return history, output, baseline


def _in_repository(path):
    return any((ancestor / ".git").exists() for ancestor in (path, *path.parents))


def digest(paths):
    value = hashlib.sha256()
    for path in sorted(paths):
        value.update(path.name.encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def hashes():
    return {
        "software": digest(
            [
                Path(__file__),
                ROOT / "tests" / "hybrid_referee.py",
                *(PACKAGE / "engine" / "hybrid").glob("*.py"),
            ]
        ),
        "constants": digest([PACKAGE / "engine" / "hybrid" / "constants.py"]),
        "protocol": hashlib.sha256(
            (PROTOCOL + json.dumps(TOLERANCES, sort_keys=True)).encode()
        ).hexdigest(),
    }


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _metadata(history):
    devices, groups, room_labels, learning = {}, {}, {}, {}
    paths = sorted(history.glob("*.json"))
    storage = history / "ld2410_tuner.data"
    if storage.is_symlink():
        raise ValueError("Linked input metadata is not supported")
    if storage.is_file():
        paths.append(storage)
    for path in paths:
        if path.is_symlink():
            raise ValueError("Linked input metadata is not supported")
        raw = _read_json(path)
        raw = raw.get("data", raw)
        devices.update(raw.get("devices", {}))
        groups.update(raw.get("room_groups", {}))
        groups.update(raw.get("groups", {}))
        learning.update(raw.get("room_learning", {}))
        room_labels.update(raw.get("room_labels", {}))
    if not devices:
        raise ValueError("No device metadata in supplied directory")
    return devices, _reconstruct_groups(devices, groups, learning), room_labels


def _reconstruct_groups(devices, groups, learning):
    for gid, entry in learning.items():
        if gid in groups:
            continue
        job = entry.get("job", {}).get("id")
        members = [
            rid
            for rid, dev in devices.items()
            if job and dev.get("learning_job", {}).get("id") == job
        ]
        groups[gid] = {"kind": "area", "area_id": gid.removeprefix("area:"), "device_ids": members}
    return groups


def _device(raw):
    result = copy.deepcopy(raw)
    result["history_labels"] = copy.deepcopy(
        raw.get("history_labels", raw.get("history", {}).get("labels", []))
    )
    result.pop("group_labels", None)
    return result


def _capture(path, rid, date, *, persistent=True):
    stat = path.stat()
    return {
        "device_id": rid,
        "date": date,
        "path": str(path),
        "identity": (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size),
        "late": [],
        "persistent": persistent and date != modules().daystore.date_of(time.time()).isoformat(),
    }


def _unsafe_day(path, history):
    return path.is_symlink() or not path.resolve().is_relative_to(history)


def _copy_days(history, detached, rid, raw):
    """Copy only expected day paths, never open/recover the user's mutable DayStore."""
    folder = detached / quote(rid, safe="").replace(".", "%2E")
    folder.mkdir(parents=True, exist_ok=True)
    encoded = raw.get("history", {}).get("day_files", {})
    paths, live = {}, set()
    for day, payload in encoded.items():
        date.fromisoformat(day)
        path = folder / (day + ".day")
        path.write_bytes(base64.b64decode(payload, validate=True))
        paths[day] = path
    for path in _day_inputs(history, folder.name):
        if path.suffix == ".log" and path.stem in paths:
            continue  # A finished snapshot wins over its stale prefix log.
        if _unsafe_day(path, history):
            raise ValueError("Linked day input is not supported")
        date.fromisoformat(path.stem)
        target = folder / (path.stem + ".day")
        if path.suffix == ".log":
            _log_snapshot(path, target)
            live.add(path.stem)
        else:
            shutil.copyfile(path, target)
        paths[path.stem] = target
    return [
        _capture(path, rid, day, persistent=day not in live) for day, path in sorted(paths.items())
    ]


def _log_snapshot(path, target):
    """Use the existing log reader, bounded to its last confirmed observation."""
    m = modules()
    start, end = m.daystore.bounds(date.fromisoformat(path.stem))
    records = m.dayfile.decode_log(path.read_bytes(), start)
    builder = m.dayfile.DayBuilder(start, end)
    for record in records:
        builder.add(record)
    until = min(end, max((r.time for r in records), default=start))
    target.write_bytes(m.dayfile.encode_day(builder.view(until)))


def _day_inputs(history, folder):
    sources = (history / "history" / folder, history / "ld2410_tuner" / "history" / folder)
    for source in sources:
        resolved = source.resolve()
        if not resolved.is_relative_to(history):
            raise ValueError("Day source must remain inside supplied history")
        yield from sorted(source.glob("*.day"))
        yield from sorted(source.glob("*.log"))


def load_history(history: Path, detached: Path):
    """Build an identity-checked read-only worker view from exports/storage metadata."""
    history, detached = Path(history).resolve(), Path(detached).resolve()
    if private_path(detached) or detached.is_relative_to(history) or detached.is_relative_to(ROOT):
        raise ValueError("Detached storage must be outside input and repository")
    raw, groups, room_labels = _metadata(history)
    entries = [
        entry
        for rid, dev in sorted(raw.items())
        for entry in _copy_days(history, detached, rid, dev)
    ]
    if not entries:
        raise ValueError("No day files supplied")
    budgets = {
        "max_rows": None,
        "max_guesses": None,
        "max_raw_bytes": 128 * 1024**2,
        "max_file_bytes": None,
    }
    devices = {rid: _device(dev) for rid, dev in raw.items()}
    days = modules().days.ReadOnlyDays({"days": entries, "budgets": budgets})
    bounds = [modules().daystore.bounds(date.fromisoformat(e["date"])) for e in entries]
    view = SimpleNamespace(
        data={"devices": devices, "groups": groups, "room_labels": room_labels},
        _days=days,
        _history_until=max(b for _, b in bounds),
        _attribution_until=max(b for _, b in bounds),
        _attribution_guesses={},
    )
    return SimpleNamespace(
        view=view,
        devices=devices,
        start=min(a for a, _ in bounds),
        end=max(b for _, b in bounds),
        detached=detached,
        groups=groups,
        room_labels=room_labels,
    )


def _mask_labels(labels, heldout):
    result = []
    for label in labels:
        parts = referee().subtract([(label["start"], label["end"])], sorted(heldout))
        result.extend({**label, "start": a, "end": b} for a, b in parts)
    return result


def _human_device(device, heldout):
    result = dict(device)
    labels = [
        s for s in device.get("history_labels", []) if s.get("source") in (None, "human", "manual")
    ]
    result["history_labels"] = _mask_labels(labels, heldout)
    # Resolve a live human label before masking; no saved result/automatic label reaches quality.
    start = device.get("training_label_start")
    if start is not None:
        live = {
            "start": start,
            "end": device.get("training_expires_at") or float("inf"),
            "state": device.get("training_state", "unknown"),
        }
        result["history_labels"].extend(_mask_labels([live], heldout))
    result["training_label_start"] = None
    result["training_state"] = "unknown"
    result.pop("learning_results", None)
    return result


class PrimaryDays:
    """Physical input is transductive; guesses never reach fold quality learning."""

    def __init__(self, days):
        self.source = days

    def __getattr__(self, name):
        return getattr(self.source, name)

    def guess_runs(self, *args, **kwargs):
        return []


def _start(device, keys, at):
    applies = {}
    for item in (device.get("learning_results") or {}).values():
        if item.get("applied_at") is not None and isinstance(item.get("configuration"), dict):
            before = item["configuration"]
            after = {**before, **{k: p["threshold"] for k, p in item.get("proposals", {}).items()}}
            applies[item.get("id") or item["applied_at"]] = (
                float(item["applied_at"]),
                before,
                after,
            )
    ordered = sorted(applies.values())
    for i, (when, _, after) in enumerate(ordered):
        end, agrees = _next_apply(ordered, i, device, after)
        if agrees and when <= at < end:
            return {key: after.get(key, 50) for key in keys}, "recovered"
    return dict.fromkeys(keys, 50), "default50"


def _next_apply(ordered, index, device, after):
    if index + 1 < len(ordered):
        when, before, _ = ordered[index + 1]
        return when, before == after
    last = device.get("last_applied") or {}
    return float("inf"), bool(last) and all(after.get(k) == v for k, v in last.items())


def _member(context, rid, at):
    dev = context.devices[rid]
    saved = (dev.get("learning_results") or {}).get("current", {})
    configured = (
        dev.get("current_thresholds") or dev.get("last_applied") or saved.get("configuration")
    )
    keys = [k for k in modules().keys.HISTORY_KEYS if k in (configured or {})]
    if not keys:
        keys = list(modules().keys.HISTORY_KEYS)  # exp2 fallback: every channel at 50
    current, provenance = _start(dev, keys, at)
    return {
        "view": context.view,
        "thresholds": current,
        "timing": dev.get("timing_configuration") or saved.get("timing_configuration") or {},
        "constraints": dev.get("threshold_constraints") or saved.get("threshold_constraints") or {},
    }, provenance


def _reader(labels):
    return modules().labels._history_label_reader({"history_labels": labels})


def _manual_room(context, gid, heldout):
    return _reader(
        _mask_labels(
            [s for s in context.room_labels.get(gid, []) if s.get("source") == "manual"], heldout
        )
    )


def _room_evidence(context, rid, heldout):
    readers = []
    for gid, group in context.groups.items():
        if rid not in group.get("device_ids", []):
            continue
        readers.append((_manual_room(context, gid, heldout), False))
        if group.get("kind") == "zone" and group.get("area_id"):
            readers.append((_manual_room(context, "area:" + group["area_id"], heldout), True))
    return readers


def _room_answer(readers, at):
    answers = {r(at) for r, parent in readers if not parent or r(at) == "not_present"}
    answers.discard(None)
    return next(iter(answers)) if len(answers) == 1 else "unknown" if answers else None


def _member_answer(readers, at):
    states = [r(at) for r in readers]
    if "present" in states:
        return "present"
    return "not_present" if states and all(s == "not_present" for s in states) else None


def _primary_spans(context, rid, heldout):
    own = {
        key: modules().labels._history_label_reader(_human_device(dev, heldout))
        for key, dev in context.devices.items()
    }
    if rid in context.devices:
        rooms = _room_evidence(context, rid, heldout)
        readers = [own[rid], *(r for r, _ in rooms)]

        def answer(at):
            state = own[rid](at)
            return state if state is not None else _room_answer(rooms, at)
    else:
        manual = _manual_room(context, rid, heldout)
        members = [own[k] for k in context.groups[rid].get("device_ids", []) if k in own]
        readers = [manual, *members]

        def answer(at):
            state = manual(at)
            return state if state is not None else _member_answer(members, at)

    cuts = sorted(
        {
            context.start,
            context.end,
            *(x for r in readers for x in r.edges if context.start < x < context.end),
        }
    )
    truth = [
        (a, b, state, "human", 1.0)
        for a, b in zip(cuts, cuts[1:], strict=False)
        if (state := answer(a)) in STATES
    ]
    return _truth_spans(truth)


def _truth_spans(truth):
    episodes = referee()._episodes(
        truth, (min((s[0] for s in truth), default=0), max((s[1] for s in truth), default=0))
    )
    return tuple(
        modules().types.TruthSpan(a, b, state, source, confidence, str(i), ())
        for i, (a, b, state, source, confidence) in enumerate(episodes)
    )


def training_problem(context, rid, heldout):
    """exp2 human truth, masked before fitting; physical input is transductive.

    The offline protocol needs no production quality/attribution learning: room
    human evidence is mapped directly and automatic evidence never enters a fit.
    """
    m = modules()
    members = [rid] if rid in context.devices else context.groups[rid].get("device_ids", [])
    at = min((a for a, _ in heldout), default=context.start)
    specs = {}
    for key in members:
        item, _ = _member(context, key, at)
        specs[key] = m.adapters._radar(
            key, list(item["thresholds"]), item["thresholds"], item["timing"], item["constraints"]
        )
    view = copy.copy(context.view)
    view._days = PrimaryDays(context.view._days)
    views = dict.fromkeys(members, view)
    rows = {
        key: m.fit_rows.CapturedRowSource(view, key, spec.channels) for key, spec in specs.items()
    }
    zone = m.types.ZoneSpec(rid, tuple(members), _primary_spans(context, rid, heldout))
    return m.types.FitProblem(
        specs,
        (zone,),
        context.start,
        context.end,
        m.adapters._Days(views, context.start, context.end),
        rows,
        m.types.EvidencePolicy(0, True, {}),
    )


def _aliases(context):
    return {rid: "R" + str(i) for i, rid in enumerate(context.devices, 1)}


def _secondary(context, rid, primary):
    dev = context.devices.get(rid, {})
    human = [{"start": a, "end": b, "state": state} for a, b, state, *_ in primary]
    unknown = [s for s in dev.get("history_labels", []) if s["state"] == "unknown"]
    reader = modules().labels._history_label_reader(
        {"history_labels": [*dev.get("heuristic_labels", []), *human, *unknown]}
    )
    cuts = sorted(
        {context.start, context.end, *(x for x in reader.edges if context.start < x < context.end)}
    )
    return [
        (a, b, reader(a), "human", 1.0)
        for a, b in zip(cuts, cuts[1:], strict=False)
        if reader(a) in STATES
    ]


def _truth(problem):
    return [(s.start, s.end, s.state, s.source, s.confidence) for s in problem.zones[0].truth]


class DiskVector:
    """Referee-compatible sequence with at most one 4096-element block resident."""

    def __init__(self, path, code):
        self.path, self.code = path, code
        self.width = 8 if code == "d" else 1
        self.count, self.offset, self.cache = 0, -1, ()

    def extend(self, values):
        with self.path.open("ab") as stream:
            stream.write(values.tobytes() if self.code == "d" else values)
        self.count += len(values)

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        offset = index // 4096 * 4096
        if self.offset != offset:
            with self.path.open("rb") as stream:
                stream.seek(offset * self.width)
                raw = stream.read(4096 * self.width)
            self.cache = array(self.code)
            self.cache.frombytes(raw)
            self.offset = offset
        return self.cache[index - offset]


def _oracle_radars(problem, directory):
    """Spill the independent oracle's columns, rather than retaining a history array."""
    result = {}
    directory.mkdir(exist_ok=True)
    for index, (rid, spec) in enumerate(problem.radars.items()):
        prefix = str(index) + "-"
        times = DiskVector(directory / (prefix + "times"), "d")
        ends = DiskVector(directory / (prefix + "ends"), "d")
        columns = [DiskVector(directory / (prefix + str(c)), "B") for c in range(18)]
        for block in problem.rows[rid].blocks(problem.start, problem.end):
            times.extend(block.times)
            ends.extend(block.ends)
            for c in spec.channels:
                columns[c].extend(block.columns[c])
        result[rid] = SimpleNamespace(times=times, ends=ends, columns=columns, timing=spec.timing)
    return result


def _coverage(radars, problem):
    if not radars:
        return []
    cov = [(problem.start, problem.end)]
    for rid, radar in radars.items():
        cov = referee().intersect(cov, list(_covered(radar, problem.radars[rid].channels)))
    return cov


def _covered(radar, channels):
    current = None
    for i, (a, b) in enumerate(zip(radar.times, radar.ends, strict=True)):
        usable = b > a and all(radar.columns[c][i] != 255 for c in channels)
        if _coverage_break(current, a, usable):
            yield current
            current = None
        if usable:
            current = (current[0], b) if current is not None else (a, b)
    if current is not None:
        yield current


def _coverage_break(current, start, usable):
    return current is not None and (not usable or start > current[1])


def fold_plan(truth, coverage):
    r = referee()
    labelled = [
        s for s in truth if s[2] in STATES and r.duration(r.intersect([s[:2]], coverage)) > 0
    ]
    if not all(any(s[2] == state for s in labelled) for state in STATES):
        return {"kind": "none", "heldout": []}
    window = min(s[0] for s in labelled), max(s[1] for s in labelled)
    episodes = r._episodes(labelled, window)
    present = [
        s for s in episodes if s[2] == "present" and r.duration(r.intersect([s[:2]], coverage)) > 0
    ]
    if len(present) == 1:
        return _half_split(episodes, coverage)
    if len(present) < 2:
        return {"kind": "none", "heldout": []}
    count = min(4, len(present))
    sizes = [len(present) // count + int(i < len(present) % count) for i in range(count)]
    cuts, used = [window[0]], 0
    for size in sizes[:-1]:
        used += size
        cuts.append((present[used - 1][1] + present[used][0]) / 2)
    cuts.append(window[1])
    return {
        "kind": "balanced4" if count == 4 else "leave_one_occupied_episode_out",
        "heldout": [[(a, b)] for a, b in zip(cuts, cuts[1:], strict=False)],
        "occupied_episodes_per_block": sizes,
    }


def _half_split(episodes, coverage):
    halves = [[], []]
    for a, b, *_ in episodes:
        seen = referee().intersect([(a, b)], coverage)
        if seen:
            mid = (seen[0][0] + seen[-1][1]) / 2
            halves[0].append((a, mid))
            halves[1].append((mid, b))
    has_empty = any(s[2] == "not_present" for s in episodes)
    return {
        "kind": "half_split_single_occupied_episode",
        "heldout": halves if has_empty else [],
        "optimistic": True,
        "occupied_episodes_per_block": [1, 1],
    }


def _clip(truth, intervals):
    return [
        (max(a, x), min(b, y), *rest)
        for a, b, *rest in truth
        for x, y in intervals
        if min(b, y) > max(a, x)
    ]


def _counts(truth, coverage, intervals):
    result = {}
    for state, prefix in (("present", "occupied"), ("not_present", "empty")):
        spans = [s for s in _clip(truth, intervals) if s[2] == state]
        result[prefix + "_seconds"] = sum(
            referee().duration(referee().intersect([s[:2]], coverage)) for s in spans
        )
        result["occupied_episodes" if state == "present" else "empty_periods"] = sum(
            referee().duration(referee().intersect([s[:2]], coverage)) > 0 for s in spans
        )
    return result


def _oracle_replay(radars, pattern):
    return {
        rid: referee().replay(
            radar, pattern[rid], (radar.times[0], radar.ends[-1]) if len(radar.times) else (0, 0)
        )
        for rid, radar in radars.items()
    }


def _oracle_score(radars, pattern, truth, intervals, replayed=None):
    replayed = replayed if replayed is not None else _oracle_replay(radars, pattern)
    scores = [
        referee().referee_score(
            radars, pattern, truth, (a, b), include_margins=False, replayed=replayed
        )
        for a, b in intervals
    ]
    return aggregate(scores)


def aggregate(scores):
    """Pool sufficient statistics; never average normalized fold costs."""
    result = {key: sum(s.get(key, 0) for s in scores) for key in ADDITIVE}
    stats = _sum_statistics(scores)
    result.update(_pooled_cost(stats))
    occupied = result["occupied_seconds"]
    detected = sum((s.get("occupied_recall") or 0) * s["occupied_seconds"] for s in scores)
    result["occupied_recall"] = detected / occupied if occupied else None
    result["longest_miss_seconds"] = max((s["longest_miss_seconds"] for s in scores), default=0)
    result["sufficient_statistics"] = stats
    return result


def _sum_statistics(scores):
    stats = {}
    for score in scores:
        for key, values in score["sufficient_statistics"].items():
            normalized = "/".join(key) if isinstance(key, tuple) else key
            old = stats.setdefault(normalized, [0.0] * 4)
            for i, value in enumerate(values):
                old[i] += value
    return stats


def _pooled_cost(stats):
    time_term = episode_term = 0.0
    complete = True
    for state, weight in (("present", 5), ("not_present", 1)):
        values = stats.get(state + "/human", [0, 0, 0, 0])
        if not values[0]:
            complete = False
        else:
            time_term += weight * values[1] / values[0]
            episode_term += weight * values[3] / values[2] if values[2] else 0
    return {
        "cost": 100 * (time_term + episode_term) if complete else None,
        "time_term": time_term,
        "episode_term": episode_term,
    }


def _score(problem, pattern, truth, intervals, mode):
    m = modules()
    kind = m.exact.Evaluator if mode == "exact" else m.evaluate.CellEvaluator
    # Distinct zones preserve each held-out interval's episode counts, while a
    # single replay/record scan shares the full timing prefix across all of them.
    zones = tuple(
        replace(problem.zones[0], group_id=str(i), truth=_truth_spans(_clip(truth, [interval])))
        for i, interval in enumerate(intervals)
    )
    with kind(problem, ((problem.start, max(b for _, b in intervals)),)) as evaluator:
        score = evaluator.scores([pattern], zones=zones)[0]
    return aggregate(list(score["zones"].values()))


def _rss():
    if sys.platform == "win32":
        return _windows_rss()
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _windows_rss():
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            *[
                (key, ctypes.c_size_t)
                for key in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ],
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError("RSS measurement unavailable")
    return counters.PeakWorkingSetSize


def _fit_once(problem, traced=False):
    if traced:
        tracemalloc.start()
    start = time.process_time()
    try:
        result = modules().engine.fit(problem)
        performance = {
            "cpu_seconds": time.process_time() - start,
            "rss_peak_bytes": _rss(),
            "rss_scope": "process lifetime high-water mark",
            "traced_peak_bytes": tracemalloc.get_traced_memory()[1] if traced else None,
        }
        return result, performance
    finally:
        if traced:
            tracemalloc.stop()


def _clear_cells(context):
    # Only our detached files are eligible for removal.
    for path in context.detached.glob("*/*.cell"):
        path.unlink()


def _measure(context, problem):
    _clear_cells(context)
    cold, cold_perf = _fit_once(problem)
    warm, warm_perf = _fit_once(problem)
    _clear_cells(context)
    traced_cold, cold_mem = _fit_once(problem, True)
    traced_warm, warm_mem = _fit_once(problem, True)
    cold_perf["traced_peak_bytes"] = cold_mem["traced_peak_bytes"]
    warm_perf["traced_peak_bytes"] = warm_mem["traced_peak_bytes"]
    deterministic = all(r.thresholds == cold.thresholds for r in (warm, traced_cold, traced_warm))
    return cold, {"cold": cold_perf, "warm": warm_perf}, deterministic


def _legal(problem, pattern):
    if set(pattern) != set(problem.radars):
        return False
    return all(
        set(pattern[rid]) == set(r.channels)
        and all(type(v) is int and v in r.domains[c] for c, v in pattern[rid].items())
        for rid, r in problem.radars.items()
    )


def _same(left, right):
    keys = (
        *(key for key in ADDITIVE if key != "covered_seconds"),
        "cost",
        "occupied_recall",
        "longest_miss_seconds",
        "time_term",
        "episode_term",
    )
    return all(_close(left.get(k), right.get(k)) for k in keys)


def _close(left, right):
    if left is None or right is None:
        return left == right
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=TOLERANCES["absolute_metric"])


def _resources(performance):
    return all(
        p["cpu_seconds"] <= TOLERANCES["cpu_seconds"]
        and p["traced_peak_bytes"] <= TOLERANCES["traced_peak_bytes"]
        and p["rss_peak_bytes"] <= TOLERANCES["rss_bytes"]
        for p in performance.values()
    )


def _failures(legal, deterministic, parity, performance):
    checks = {
        "illegal_thresholds": legal,
        "nondeterministic": deterministic,
        "exact_parity": parity,
    }
    for reason, field, limit in (
        ("cpu_budget", "cpu_seconds", "cpu_seconds"),
        ("traced_memory_budget", "traced_peak_bytes", "traced_peak_bytes"),
        ("rss_budget", "rss_peak_bytes", "rss_bytes"),
    ):
        checks[reason] = all(p[field] <= TOLERANCES[limit] for p in performance.values())
    return [reason for reason, passed in checks.items() if not passed]


def _pattern_json(pattern, aliases):
    return {
        aliases[rid]: {str(c): value for c, value in channels.items()}
        for rid, channels in pattern.items()
    }


def _margins(radars, pattern, truth, window, aliases):
    return {
        aliases[rid]: referee().margins(radar, pattern[rid], truth, window)
        for rid, radar in radars.items()
    }


def _fit_audit(result):
    fields = (
        "status",
        "scoring_mode",
        "base_cost",
        "quiet_cost",
        "selection_cost",
        "row_work",
        "operations",
        "spilled_bytes",
    )
    audit = {key: result.diagnostics.get(key) for key in fields}
    anomaly = result.diagnostics.get("anomaly", {})
    # Never pass arbitrary diagnostic rows/episode ids into an anonymous report.
    safe = (
        "status",
        "suspects",
        "gain",
        "margin",
        "operations",
        "operation_budget",
        "discarded_count",
        "discarded_seconds",
    )
    audit["anomaly"] = {key: anomaly[key] for key in safe if key in anomaly}
    return audit


def _fold(context, rid, alias, full, radars, coverage, heldout, index, baseline):
    truth = _truth(full)
    training = training_problem(context, rid, heldout)
    result, performance, deterministic = _measure(context, training)
    replayed = _oracle_replay(radars, result.thresholds)
    primary = _oracle_score(radars, result.thresholds, truth, heldout, replayed)
    exact = _score(full, result.thresholds, truth, heldout, "exact")
    cells = _score(full, result.thresholds, truth, heldout, "cells")
    legal = _legal(training, result.thresholds)
    aliases = _aliases(context)
    provenance = {
        aliases[key]: _member(context, key, min(a for a, _ in heldout))[1] for key in full.radars
    }
    parity = _same(primary, exact)
    resource_ok = _resources(performance)
    # A fold may own the only empty period. The harness still scores its legal
    # unchanged start; missing training support is an annotation, not a failure.
    sufficient = result.diagnostics["status"] != "insufficient"
    failures = _failures(legal, deterministic, parity, performance)
    start = {key: spec.current for key, spec in training.radars.items()}
    record = {
        "fold": index,
        "heldout": heldout,
        "status": "failed" if failures else "ok",
        "failures": failures,
        "thresholds": _pattern_json(result.thresholds, aliases),
        "legal": legal,
        "parity": parity,
        "resource_ok": resource_ok,
        "sufficient_training": sufficient,
        "fit": _fit_audit(result),
        "start": {
            "thresholds": _pattern_json(start, aliases),
            "primary": _oracle_score(radars, start, truth, heldout),
        },
        "primary": primary,
        "exact": exact,
        "cells": cells,
        "coverage_scope": {
            "primary": "held-out intervals",
            "exact": "replay regions including warm-up",
            "cells": "replay regions including warm-up",
        },
        "source": "human",
        "scoring_mode": "exact",
        "fit_scoring_mode": result.after.get("scoring_mode"),
        "start_provenance": provenance,
        "deterministic": deterministic,
        "training_regions": [(s.start, s.end) for s in training.zones[0].truth],
        "training_counts": _counts(_truth(training), coverage, [(context.start, context.end)]),
        "held_counts": _counts(truth, coverage, heldout),
        "performance": performance,
        "margins": _margins(
            radars, result.thresholds, _truth(training), (context.start, context.end), aliases
        ),
        "secondary": _oracle_score(
            radars, result.thresholds, _secondary(context, rid, truth), heldout, replayed
        ),
    }
    record["baseline"] = _fixed_baseline(
        baseline, alias, index, full, radars, heldout, aliases, primary
    )
    if record["baseline"].get("status") == "failed":
        record["status"] = "failed"
        record["failures"].append("baseline_mismatch")
    return record


def _fixed_baseline(baseline, alias, index, problem, radars, heldout, aliases, primary):
    if baseline is None:
        return {"supplied": False}
    matches = [s for s in baseline["sets"] if s["alias"] == alias]
    if not matches or index >= len(matches[0]["folds"]):
        return {"supplied": True, "status": "failed", "reason": "missing frozen fold"}
    frozen = matches[0]["folds"][index]
    if frozen["heldout"] != [list(part) for part in heldout]:
        return {"supplied": True, "status": "failed", "reason": "fold geometry differs"}
    pattern = {
        rid: {int(c): v for c, v in frozen["thresholds"].get(aliases[rid], {}).items()}
        for rid in problem.radars
    }
    if not _legal(problem, pattern):
        return {"supplied": True, "status": "failed", "reason": "illegal frozen pattern"}
    score = _oracle_score(radars, pattern, _truth(problem), heldout)
    parity = _same(primary, score) and _same(score, frozen["primary"])
    return {
        "supplied": True,
        "status": "ok" if parity else "failed",
        "primary": score,
        "thresholds": _pattern_json(pattern, aliases),
    }


def _skipped(context, rid, alias, plan):
    return {
        "alias": alias,
        "kind": "device" if rid in context.devices else "room",
        "status": "skipped",
        "skip_reason": "missing_primary_class",
        "failures": [],
        "plan": plan,
        "folds": [],
        "aggregate": aggregate([]),
        "secondary_aggregate": aggregate([]),
    }


def _set(context, rid, alias, baseline):
    full = training_problem(context, rid, [])
    if not all(any(s.state == state for s in full.zones[0].truth) for state in STATES):
        return _skipped(context, rid, alias, {"kind": "none", "heldout": []})
    radars = _oracle_radars(full, context.detached / ("oracle-" + alias))
    coverage = _coverage(radars, full)
    plan = fold_plan(_truth(full), coverage)
    if not plan["heldout"]:
        return _skipped(context, rid, alias, plan)
    folds = [
        _fold(context, rid, alias, full, radars, coverage, heldout, index, baseline)
        for index, heldout in enumerate(plan["heldout"])
    ]
    return {
        "alias": alias,
        "failures": sorted({reason for f in folds for reason in f["failures"]}),
        "plan": plan,
        "folds": folds,
        "kind": "device" if rid in context.devices else "room",
        "status": "ok" if folds and all(f["status"] == "ok" for f in folds) else "failed",
        "aggregate": aggregate([f["primary"] for f in folds]),
        "secondary_aggregate": aggregate([f["secondary"] for f in folds]),
    }


def _baseline(path):
    if path is None:
        return None
    baseline = _read_json(path)
    if baseline.get("protocol") != PROTOCOL or baseline.get("tolerances") != TOLERANCES:
        raise ValueError("Baseline protocol or frozen tolerances differ")
    if baseline.get("hashes", {}).get("constants") != hashes()["constants"]:
        raise ValueError("Baseline numerical constants differ")
    _baseline_structure(baseline)
    return baseline


def _baseline_structure(baseline):
    _baseline_fields(baseline, {"sets": list})
    for item in baseline["sets"]:
        _baseline_fields(item, {"alias": str, "folds": list, "aggregate": dict, "status": str})
        for fold in item["folds"]:
            _baseline_fields(fold, {"heldout": list, "thresholds": dict, "primary": dict})


def _baseline_fields(value, fields):
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key), kind) for key, kind in fields.items()
    ):
        raise ValueError("Invalid baseline report structure")


def _baseline_check(report, baseline):
    if baseline is None:
        return {"supplied": False, "status": "not compared"}
    # A frozen validated-hybrid report is a parity oracle, not a tunable target.
    expected = {s["alias"]: s for s in baseline["sets"]}
    parity = set(expected) == {s["alias"] for s in report["sets"]}
    for item in report["sets"]:
        previous = expected.get(item["alias"], {})
        parity = parity and item["status"] == previous.get("status")
        parity = parity and _same(item["aggregate"], previous.get("aggregate", {}))
        parity = parity and all(f["baseline"].get("status") == "ok" for f in item["folds"])
    return {
        "supplied": True,
        "status": "ok" if parity else "failed",
        "sha256": hashlib.sha256(json.dumps(baseline, sort_keys=True).encode()).hexdigest(),
    }


def _default_jobs():
    return max(1, min(8, (os.cpu_count() or 1) // 2))


def _jobs(value):
    if type(value) is not int or value < 1:
        raise ValueError("jobs must be a positive integer")
    return value


def _cli_jobs(value):
    try:
        return _jobs(int(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("jobs must be a positive integer") from error


def _worker_snapshot(context):
    return {
        "devices": context.devices,
        "groups": context.groups,
        "room_labels": context.room_labels,
        "start": context.start,
        "end": context.end,
        "entries": list(context.view._days.entries.values()),
        "budgets": context.view._days.budgets,
    }


def _worker_init(snapshot, parent, baseline):
    global _WORKER_CONTEXT, _WORKER_BASELINE
    try:
        _WORKER_CONTEXT = _worker_context(snapshot, parent)
        _WORKER_BASELINE = baseline
    except Exception:
        # Executor logs initializer tracebacks; keep input identities out of stderr.
        raise RuntimeError("Validation worker setup failed") from None


def _worker_context(snapshot, parent):
    detached = Path(tempfile.mkdtemp(prefix="worker-", dir=parent))
    entries = []
    for entry in snapshot["entries"]:
        source = Path(entry["path"])
        target = detached / source.parent.name / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        entries.append(
            {
                **entry,
                **_capture(
                    target, entry["device_id"], entry["date"], persistent=entry["persistent"]
                ),
            }
        )
    data = {key: snapshot[key] for key in ("devices", "groups", "room_labels")}
    view = SimpleNamespace(
        data=data,
        _days=modules().days.ReadOnlyDays({"days": entries, "budgets": snapshot["budgets"]}),
        _history_until=snapshot["end"],
        _attribution_until=snapshot["end"],
        _attribution_guesses={},
    )
    return SimpleNamespace(
        **data, view=view, start=snapshot["start"], end=snapshot["end"], detached=detached
    )


def _worker_set(rid, alias):
    result = _set(_WORKER_CONTEXT, rid, alias, _WORKER_BASELINE)
    for fold in result["folds"]:
        for measurement in fold["performance"].values():
            measurement["rss_scope"] = "worker process lifetime high-water mark"
    return result


def _stop_workers(pool):
    # Python 3.12 has no public terminate_workers API. Stop running fits before
    # shutdown so failures cannot wait for unrelated sets or race temp cleanup.
    processes = list(pool._processes.values())
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        process.join()


def _parallel_sets(context, targets, baseline, jobs):
    # A stable import name is required even when callers load this file by path.
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        worker = importlib.import_module("validate_hybrid")
        return _pool_sets(worker, context, targets, baseline, jobs)
    finally:
        sys.path.pop(0)


def _pool_sets(worker, context, targets, baseline, jobs):
    pool = ProcessPoolExecutor(
        max_workers=jobs,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=worker._worker_init,
        initargs=(_worker_snapshot(context), context.detached, baseline),
    )
    try:
        futures = {pool.submit(worker._worker_set, *target): i for i, target in enumerate(targets)}
        results = [None] * len(targets)
        for future in as_completed(futures):
            results[futures[future]] = future.result()
        return results
    except BaseException as error:
        _stop_workers(pool)
        if isinstance(error, (ValueError, OSError, KeyError, TypeError)):
            raise
        if isinstance(error, Exception):
            raise RuntimeError("Validation worker failed") from error
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def validate(
    history: Path, output: Path, *, baseline: Path | None = None, jobs: int | None = None
) -> dict:
    """Return/write an anonymous report; ValueError/OSError indicate invalid input."""
    jobs = _default_jobs() if jobs is None else _jobs(jobs)
    history, output, baseline = checked_paths(history, output, baseline)
    frozen = _baseline(baseline)  # Freeze before producing any new results.
    with tempfile.TemporaryDirectory(prefix="hybrid-validation-") as temporary:
        context = load_history(history, Path(temporary))
        targets = [
            *((rid, alias) for rid, alias in _aliases(context).items()),
            *((gid, "G" + str(i)) for i, gid in enumerate(context.groups, 1)),
        ]
        sets = (
            [_set(context, rid, alias, frozen) for rid, alias in targets]
            if jobs == 1
            else _parallel_sets(context, targets, frozen, jobs)
        )
    report = {
        "protocol": PROTOCOL,
        "hashes": hashes(),
        "tolerances": dict(TOLERANCES),
        "sets": sets,
        "secondary_truth": {"circular": True, "used_for_verdict": False},
        "input": "user supplied",
        "primary_truth": "human only",
        "accepted_exception": {
            "set": "R1",
            "fold": 3,
            "excluded_from_aggregate": False,
            "waives_validation_failure": False,
            "reason": "Accepted held-out low-energy drift; longer miss remains visible",
        },
        "status": "ok" if all(s["status"] != "failed" for s in sets) else "failed",
    }
    report["baseline"] = _baseline_check(report, frozen)
    if report["baseline"]["status"] == "failed":
        report["status"] = "failed"
    output.mkdir(parents=True, exist_ok=True)
    # Round-trip makes the return contract exactly match the JSON (including tuples).
    encoded = json.dumps(report, indent=2, allow_nan=False)
    _write_report(output, encoded)
    return json.loads(encoded)


def _write_report(output, encoded):
    with tempfile.NamedTemporaryFile(
        dir=output, mode="w", encoding="utf-8", newline="\n", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(encoded + "\n")
            stream.close()
            os.replace(temporary, output / "report.json")
        finally:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--jobs", type=_cli_jobs, default=None, help="set workers (default: up to 8)"
    )
    args = parser.parse_args(argv)
    try:
        report = validate(args.history, args.output, baseline=args.baseline, jobs=args.jobs)
    except (ValueError, OSError, KeyError, TypeError):
        print("Invalid validation input; no private details emitted", file=sys.stderr)
        return 2
    except RuntimeError:
        print(
            "Validation worker failed; no report written or private details emitted",
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"protocol": PROTOCOL, "status": report["status"]}))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
