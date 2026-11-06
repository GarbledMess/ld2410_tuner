"""Rewrite the recorded Learn baseline from the current code; review the diff before committing."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import test_tuner  # noqa: E402, F401
from test_hybrid_learn_work import BASELINE, CASES, comparable, learn, recorded  # noqa: E402
from test_hybrid_repeat_learn import BASELINE as REAL_BASELINE  # noqa: E402
from test_hybrid_repeat_learn import recorded_reports  # noqa: E402


def _report(root, case):
    report = comparable(learn(recorded(root, 6, irregular=True), case))
    return json.loads(json.dumps(report, default=repr))


def _write(path, found):
    text = json.dumps(found, sort_keys=True, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf8", newline="\n")


def main():
    with tempfile.TemporaryDirectory() as folder:
        _write(BASELINE, {name: _report(Path(folder) / name, case) for name, case in CASES.items()})
    with tempfile.TemporaryDirectory() as folder:
        found = {name: recorded_reports(Path(folder) / name, case) for name, case in CASES.items()}
    _write(REAL_BASELINE, found)


if __name__ == "__main__":
    main()
