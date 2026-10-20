"""LD2410 public regression suite in the real HA container, offline."""

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

os.chdir("/workspace/project")
reports = Path("/config/lab-reports")
reports.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix="ha-qa-") as temporary:
    dependencies = Path(temporary) / "dependencies"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            "/lab/wheels",
            "--target",
            str(dependencies),
            "-r",
            "requirements-dev.txt",
        ],
        check=True,
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(dependencies),
        "COVERAGE_FILE": str(reports / ".coverage"),
        "RUFF_CACHE_DIR": str(reports / "ruff-cache"),
    }
    commands = [
        ["-m", "ruff", "check", "custom_components", "tools", "tests"],
        ["-m", "ruff", "format", "--check", "custom_components", "tools", "tests"],
        ["tools/check_complexity.py"],
        ["tools/validate_package.py"],
        [
            "-m",
            "pytest",
            "--basetemp",
            temporary + "/pytest",
            "-o",
            "cache_dir=/config/lab-reports/pytest-cache",
            "--cov",
            "--cov-fail-under=96.95",
            "--cov-report=term",
            "--cov-report=xml:/config/lab-reports/coverage.xml",
            *sys.argv[1:],
        ],
    ]
    for command in commands:
        print("QA:", command, flush=True)
        subprocess.run([sys.executable, *command], check=True, env=environment)
    coverage = ET.parse(reports / "coverage.xml").getroot()
    covered = int(coverage.attrib["lines-covered"])
    total = int(coverage.attrib["lines-valid"])
    if covered * 10000 < total * 9695:
        raise RuntimeError(f"Coverage {covered / total:.4%} is below the retained 96.95% floor")
