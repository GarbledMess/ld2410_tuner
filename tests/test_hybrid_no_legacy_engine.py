"""Shipping fits cannot reach or package the superseded row search engine."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "custom_components" / "ld2410_tuner"
OBSOLETE = {
    "rooms/objective.py",
    "rooms/window_rates.py",
    "rooms/preferences.py",
    "rooms/replay.py",
    "calibration/evidence_plan.py",
    "calibration/evidence_budget.py",
    "calibration/event_preferences.py",
}
ENTRY_POINTS = {
    "calibration.event_fitting",
    "calibration.comparison",
    "calibration.comparison_jobs",
    "calibration.adaptive",
    "rooms.fitting",
    "rooms.assessment",
    "rooms.adaptive",
    "rooms.application",
    "worker.calculations",
}
FORBIDDEN = {
    "Objective",
    "LinkedObjective",
    "Radar",
    "_DeviceObjective",
    "_Gate",
    "threshold_masks",
    "WindowRates",
}


def _module(path):
    parts = list(path.relative_to(PACKAGE).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(module, tree):
    """Resolve relative imports, including imports inside public wrapper functions."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            base = module.split(".")[: -node.level] if node.level else []
            base += (node.module or "").split(".") if node.module else []
            prefix = ".".join(base)
            yield prefix
            yield from (f"{prefix}.{alias.name}" for alias in node.names)


def _name(node):
    return node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")


def test_shipped_fitting_import_graph_has_no_old_search():
    trees = {_module(p): ast.parse(p.read_text(encoding="utf-8")) for p in PACKAGE.rglob("*.py")}
    reached, pending = set(), set(ENTRY_POINTS)
    while pending:
        module = pending.pop()
        if module in reached or module not in trees:
            continue
        reached.add(module)
        pending.update(set(_imports(module, trees[module])) - reached)
    assert "engine.hybrid.facade" in reached
    assert not {p.removesuffix(".py").replace("/", ".") for p in OBSOLETE} & reached
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                assert node.name not in FORBIDDEN, (module, node.name)
            if isinstance(node, ast.Call):
                assert _name(node.func) not in FORBIDDEN, (module, node.lineno)
                if module in ENTRY_POINTS or module.startswith("engine.hybrid."):
                    assert _name(node.func) != "DurationGroup", (module, node.lineno)
        if module in {"rooms.fitting", "calibration.event_fitting"}:
            assert not {"search", "_search", "_objective"} & {
                n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
            }


@pytest.fixture
def replay_consumers():
    """Explicit inventory: shared timing validation and physical diagnostic geometry.

    Row-mask room replay has no live consumer; its independent interval oracle lives
    in tests/legacy_replay.py. These physical timing helpers remain shared by hybrid.
    """
    return {
        "calibration.event_timing",
        "rooms.evidence",
        "engine.hybrid.replay",
        "engine.hybrid.domains",
        "engine.hybrid.search_timing",
        "engine.hybrid.fixed",
        "engine.hybrid.timing",
    }


def test_remaining_physical_replay_consumers_are_inventoried(replay_consumers):
    actual = set()
    for path in PACKAGE.rglob("*.py"):
        module = _module(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if "calibration.event_replay" in set(_imports(module, tree)):
            actual.add(module)
    assert actual == replay_consumers


def test_release_contains_all_hybrid_modules_and_no_old_objective():
    tree = ast.parse((ROOT / "tools/validate_package.py").read_text(encoding="utf-8"))
    manifest = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "RUNTIME_FILES" for t in n.targets)
    )
    hybrid = {p.relative_to(PACKAGE).as_posix() for p in (PACKAGE / "engine/hybrid").glob("*.py")}
    assert hybrid <= set(manifest)
    assert not OBSOLETE.intersection(manifest)
    assert all(not (PACKAGE / p).exists() for p in OBSOLETE)


def test_duration_diagnostics_expose_no_search_ranks():
    tree = ast.parse((PACKAGE / "calibration/duration.py").read_text(encoding="utf-8"))
    replay = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DurationReplay"
    )
    assert not {"rank", "automatic_rank"} & {
        n.name for n in replay.body if isinstance(n, ast.FunctionDef)
    }


def test_duration_diagnostics_expose_no_search_weight_caches():
    tree = ast.parse((PACKAGE / "calibration/duration.py").read_text(encoding="utf-8"))
    group = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DurationGroup")
    assert not {"window_mass", "weighted_seconds", "_weighted", "_weighted_part"} & {
        n.name for n in group.body if isinstance(n, ast.FunctionDef)
    }
