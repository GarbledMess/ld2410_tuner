"""Local git hooks run the same tool versions as CI, and CI actions stay pinned."""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def requirement(name):
    for line in (ROOT / "requirements-dev.txt").read_text().splitlines():
        package, _, version = line.partition("==")
        if package.strip().lower() == name:
            return version.strip()
    raise AssertionError(f"{name} is not pinned in requirements-dev.txt")


def hook_repos():
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    return {repo["repo"]: repo for repo in config["repos"]}


def hook(repo, hook_id):
    return next(h for h in hook_repos()[repo]["hooks"] if h["id"] == hook_id)


def workflow_text():
    return "\n".join(path.read_text() for path in sorted(WORKFLOWS.glob("*.yml")))


def test_python_hooks_match_requirements():
    repos = hook_repos()
    assert repos["https://github.com/astral-sh/ruff-pre-commit"]["rev"] == (
        f"v{requirement('ruff')}"
    )
    assert repos["https://github.com/PyCQA/bandit"]["rev"] == requirement("bandit")


def test_prettier_hook_matches_package_json():
    pinned = json.loads((ROOT / "package.json").read_text())["devDependencies"]["prettier"]
    assert hook("local", "prettier")["additional_dependencies"] == [f"prettier@{pinned}"]


def test_gitleaks_hook_matches_ci():
    rev = hook_repos()["https://github.com/gitleaks/gitleaks"]["rev"]
    assert f"GITLEAKS_VERSION: {rev.removeprefix('v')}" in workflow_text()


def test_bandit_reports_every_severity_locally_and_in_ci():
    assert "args" not in hook("https://github.com/PyCQA/bandit", "bandit")
    assert re.search(r"bandit -r custom_components tools\n", workflow_text())


def test_esphome_validation_still_rejects_mismatches_under_optimisation():
    # python -O strips assert statements; validation must not silently pass then.
    script = (
        "import sys; sys.path.insert(0, 'tools'); import validate_esphome as v\n"
        "empty = {'ld2410': [], 'uart': [], 'script': [], 'globals': []}\n"
        "try:\n"
        "    v._check(empty, 1, {})\n"
        "except v.ValidationError:\n"
        "    pass\n"
        "else:\n"
        "    sys.exit('mismatch accepted')\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", script], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_every_action_is_pinned_to_a_commit():
    uses = re.findall(r"uses:\s*(\S+)", workflow_text())
    assert uses
    unpinned = [ref for ref in uses if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref)]
    assert unpinned == []
