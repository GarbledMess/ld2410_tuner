"""Validate this integration's local package; no network or catalogue submission."""

import json
import sys
from pathlib import Path

DOMAIN = "ld2410_tuner"
RUNTIME_FILES = (
    "__init__.py",
    "brand/icon.png",
    "calibration/__init__.py",
    "calibration/constants.py",
    "calibration/fitting.py",
    "calibration/metrics.py",
    "calibration/search.py",
    "calibration/service.py",
    "config_flow.py",
    "const.py",
    "history/__init__.py",
    "history/cleanup.py",
    "history/labels.py",
    "history/recording.py",
    "manifest.json",
    "presence/__init__.py",
    "presence/autolabelling.py",
    "presence/inference.py",
    "presentation/__init__.py",
    "presentation/charts.py",
    "presentation/snapshots.py",
    "runtime/__init__.py",
    "runtime/coordinator.py",
    "runtime/discovery.py",
    "runtime/websocket.py",
    "services.yaml",
    "static/ld2410-tuner-panel.js",
    "static/panel/card.js",
    "static/panel/chart.js",
    "static/panel/constants.js",
    "static/panel/controls.js",
    "static/panel/learning.js",
    "static/panel/selection.js",
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
