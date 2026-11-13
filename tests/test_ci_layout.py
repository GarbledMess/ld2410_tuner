"""The slow scaling tests leave the default run but never leave CI."""

import re
import shlex
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "validate.yml").read_text()


def _pytest_options():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]


def _job(name):
    found = re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  \S|\Z)", WORKFLOW)
    assert found, f"CI job {name} is missing"
    return found.group(1)


def test_the_scaling_mark_is_registered_and_left_out_of_the_default_run():
    options = _pytest_options()
    assert any(line.startswith("scaling:") for line in options["markers"])
    addopts = shlex.split(options["addopts"])
    assert addopts[addopts.index("-m") + 1] == "not scaling"
    assert "--strict-markers" in addopts


def test_ci_runs_the_scaling_tests_on_every_push_and_releases_only_after_them():
    assert "python3 -m pytest -m scaling" in _job("scaling")
    needs = re.search(r"needs: \[(.*?)\]", _job("release")).group(1)
    assert "scaling" in [item.strip() for item in needs.split(",")]
