"""Regenerate tests/fixtures/characterisation/*.json from current behaviour.

Usage: python tools/update_characterisation.py
Review the diff before keeping it: these files pin behaviour that later changes must preserve.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "characterisation"


def generate(directory=FIXTURES):
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        import test_characterisation as suite
    finally:
        sys.path.remove(str(ROOT / "tests"))
    directory.mkdir(parents=True, exist_ok=True)
    for name, (builder, drop) in suite.CASES.items():
        text = suite.canonical(builder(), drop)
        (directory / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")
    return sorted(suite.CASES)


def main():
    for name in generate():
        print(f"wrote {name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
