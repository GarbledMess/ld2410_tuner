"""Rewrite the recorded Learn baseline from the current code; review the diff before committing."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import test_tuner  # noqa: E402, F401
from test_hybrid_learn_work import BASELINE, CASES, comparable, learn, recorded  # noqa: E402


def _report(root, case):
    report = comparable(learn(recorded(root, 6, irregular=True), case))
    return json.loads(json.dumps(report, default=repr))


def main():
    with tempfile.TemporaryDirectory() as folder:
        found = {name: _report(Path(folder) / name, case) for name, case in CASES.items()}
    text = json.dumps(found, sort_keys=True, separators=(",", ":"))
    BASELINE.write_text(text + "\n", encoding="utf8", newline="\n")


if __name__ == "__main__":
    main()
