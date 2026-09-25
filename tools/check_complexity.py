"""Enforce a cognitive-complexity ceiling on every shipped Python function."""

import ast
from pathlib import Path

from cognitive_complexity.api import get_cognitive_complexity

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 10


def measure(root=ROOT):
    """Return relative paths and scores, including nested functions and methods."""
    scores = []
    paths = sorted((root / "custom_components").rglob("*.py"))
    paths += sorted((root / "tools").glob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scores.append(
                    (
                        str(path.relative_to(root)),
                        node.lineno,
                        node.name,
                        get_cognitive_complexity(node),
                    )
                )
    return scores


def main():
    scores = measure()
    failures = [row for row in scores if row[3] > LIMIT]
    for path, line, name, score in failures:
        print(f"{path}:{line}: {name} complexity {score} exceeds {LIMIT}")
    print(
        f"Checked {len(scores)} Python functions; maximum complexity "
        f"{max((row[3] for row in scores), default=0)} (limit {LIMIT})."
    )
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
