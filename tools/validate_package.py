"""Validate this integration's local package; no network or catalogue submission."""

import json
import sys
from pathlib import Path

DOMAIN = "ld2410_tuner"
RUNTIME_FILES = (
    "__init__.py",
    "brand/icon.png",
    "engine/__init__.py",
    "engine/columns.py",
    "worker/__init__.py",
    "worker/supervisor.py",
    "worker/limits.py",
    "worker/history.py",
    "worker/protocol.py",
    "history/commit.py",
    "worker/transactions.py",
    "worker/storage_values.py",
    "worker/startup.py",
    "worker/maintenance.py",
    "worker/flushing.py",
    "worker/bridge.py",
    "worker/capture.py",
    "worker/codec.py",
    "worker/calculations.py",
    "worker/entrypoint.py",
    "worker/recount.py",
    "worker/display.py",
    "calibration/__init__.py",
    "calibration/adaptive.py",
    "calibration/automatic.py",
    "calibration/constants.py",
    "calibration/comparison.py",
    "calibration/comparison_jobs.py",
    "calibration/comparison_cache.py",
    "calibration/episodes.py",
    "calibration/evidence_budget.py",
    "calibration/evidence_plan.py",
    "calibration/quality.py",
    "calibration/quality_evidence.py",
    "calibration/selection.py",
    "calibration/ownership.py",
    "calibration/event_replay.py",
    "calibration/event_timing.py",
    "calibration/event_quality.py",
    "calibration/summary_quality.py",
    "calibration/event_fitting.py",
    "calibration/event_preferences.py",
    "calibration/device_io.py",
    "calibration/duration.py",
    "calibration/intervals.py",
    "calibration/recovery.py",
    "calibration/feasibility.py",
    "calibration/diagnostics.py",
    "calibration/influence.py",
    "calibration/jobs.py",
    "calibration/metrics.py",
    "calibration/timing.py",
    "calibration/timing_config.py",
    "calibration/timing_metrics.py",
    "calibration/reliability.py",
    "calibration/scoring.py",
    "calibration/separation.py",
    "calibration/service.py",
    "calibration/settings.py",
    "calibration/thresholds.py",
    "calibration/results.py",
    "config_flow.py",
    "const.py",
    "entity_index.py",
    "history/__init__.py",
    "history/cleanup.py",
    "history/guesses.py",
    "history/trust.py",
    "history/policy.py",
    "history/budget.py",
    "history/storage.py",
    "history/source_labels.py",
    "history/label_lookup.py",
    "history/labels.py",
    "history/label_counts.py",
    "history/recording.py",
    "history/dayfile.py",
    "history/day_results.py",
    "history/pace.py",
    "history/daystore.py",
    "history/worker_snapshot.py",
    "history/ingest.py",
    "history/convert.py",
    "history/live.py",
    "history/spans.py",
    "history/summaries.py",
    "history/summary_cache.py",
    "manifest.json",
    "presence/__init__.py",
    "presence/autolabelling.py",
    "presence/inference.py",
    "presence/sources.py",
    "presence/source_buffer.py",
    "presence/references.py",
    "presence/rebuilds.py",
    "presence/reference_training.py",
    "presentation/__init__.py",
    "presentation/charts.py",
    "presentation/snapshots.py",
    "runtime/__init__.py",
    "runtime/coordinator.py",
    "runtime/schedule.py",
    "runtime/tasks.py",
    "runtime/discovery.py",
    "runtime/engineering.py",
    "runtime/websocket.py",
    "rooms/__init__.py",
    "rooms/adaptive.py",
    "rooms/assessment.py",
    "rooms/attribution.py",
    "rooms/application.py",
    "rooms/fitting.py",
    "rooms/learning.py",
    "rooms/objective.py",
    "rooms/evidence.py",
    "rooms/event_attribution.py",
    "rooms/groups.py",
    "rooms/intervals.py",
    "rooms/jobs.py",
    "rooms/layout.py",
    "rooms/membership.py",
    "rooms/replay.py",
    "rooms/room_labels.py",
    "rooms/roles.py",
    "rooms/room_sources.py",
    "rooms/seen_by.py",
    "rooms/sets.py",
    "rooms/truth.py",
    "services.yaml",
    "static/ld2410-tuner-panel.js",
    "static/panel/activity.js",
    "static/panel/calendar.js",
    "static/panel/card.js",
    "static/panel/rendering.js",
    "static/panel/layout.js",
    "static/panel/room_report.js",
    "static/panel/room_learning.js",
    "static/panel/room_timeline.js",
    "static/panel/comparison.js",
    "static/panel/chart.js",
    "static/panel/constants.js",
    "static/panel/controls.js",
    "static/panel/forms.js",
    "static/panel/history.js",
    "static/panel/history_graph.js",
    "static/panel/saved_results.js",
    "static/panel/learning.js",
    "static/panel/jobs.js",
    "static/panel/storage.js",
    "static/panel/timing_settings.js",
    "static/panel/sources.js",
    "static/panel/source_picker.js",
    "static/panel/selection.js",
    "static/panel/timeline.js",
    "static/panel/view.js",
    "static/panel/visualization.js",
    "translations/en.json",
)


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def validate(root: Path) -> None:
    components = root / "custom_components"
    domains = sorted(
        path.name for path in components.iterdir() if path.is_dir() and path.name != "__pycache__"
    )
    if domains != [DOMAIN]:
        raise ValueError(f"Expected only custom_components/{DOMAIN}; found {domains}")

    _validate_hacs(root)
    package = components / DOMAIN
    for name in RUNTIME_FILES:
        if not (package / name).is_file():
            raise ValueError(f"Missing integration file: {name}")

    _validate_manifest(package)


def _validate_hacs(root):
    hacs = read_object(root / "hacs.json")
    if not isinstance(hacs.get("name"), str) or not hacs["name"].strip():
        raise ValueError("hacs.json requires a non-empty name")
    if hacs.get("content_in_root", False) is not False:
        raise ValueError("content_in_root must be false for this package layout")
    if not isinstance(hacs.get("render_readme", False), bool):
        raise ValueError("render_readme must be a boolean")

    if hacs.get("zip_release") is not True or hacs.get("filename") != f"{DOMAIN}.zip":
        raise ValueError("HACS must use the versioned integration ZIP asset")


def _validate_manifest(package):
    manifest = read_object(package / "manifest.json")
    if manifest.get("domain") != DOMAIN:
        raise ValueError("Manifest domain must match the integration directory")
    for key in ("name", "version"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"Manifest requires a non-empty {key}")
    for path in package.rglob("*.json"):
        read_object(path)


if __name__ == "__main__":
    try:
        validate(Path(__file__).resolve().parents[1])
    except (OSError, ValueError) as error:
        sys.exit(f"Package validation failed: {error}")
    print("Integration package layout and JSON checks passed")
