"""Validate this integration's local package; no network or catalogue submission."""

from pathlib import Path
import json
import sys


DOMAIN = "ld2410_tuner"


def read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def validate(root: Path) -> None:
    components = root / "custom_components"
    domains = sorted(
        path.name for path in components.iterdir()
        if path.is_dir() and path.name != "__pycache__"
    )
    if domains != [DOMAIN]:
        raise ValueError(f"Expected only custom_components/{DOMAIN}; found {domains}")

    hacs = read_object(root / "hacs.json")
    if not isinstance(hacs.get("name"), str) or not hacs["name"].strip():
        raise ValueError("hacs.json requires a non-empty name")
    if hacs.get("content_in_root", False) is not False:
        raise ValueError("content_in_root must be false for this package layout")
    if not isinstance(hacs.get("render_readme", False), bool):
        raise ValueError("render_readme must be a boolean")

    package = components / DOMAIN
    for name in (
        "__init__.py", "config_flow.py", "inference.py", "learning.py",
        "manifest.json", "translations/en.json", "brand/icon.png",
        "static/ld2410-tuner-panel.js",
    ):
        if not (package / name).is_file():
            raise ValueError(f"Missing integration file: {name}")

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
