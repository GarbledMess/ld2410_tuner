# Development

## Repository layout

The installable package is `custom_components/ld2410_tuner`; only that folder is
installed by HACS. Its entrypoint owns Home Assistant setup and unload. Supporting
modules are grouped into `runtime/`, `rooms/`, `presence/`, `history/`, `calibration/`,
`engine/`, `worker/` and `presentation/`, and the panel entrypoint `static/ld2410-tuner-panel.js` loads
focused modules from `static/panel/`. See [architecture and invariants](architecture.md)
for the full ownership map.

Tests, developer tools, replay tools and CI live at the repository root. The layout
follows the [HACS integration requirements](https://www.hacs.dev/docs/publish/integration/).

## Checks

In a virtual environment:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m ruff check custom_components tools tests
python3 -m ruff format --check custom_components tools tests
python3 tools/check_complexity.py
python3 -m pytest --cov --cov-report=xml --cov-report=term-missing
python3 -m bandit -r custom_components tools
python3 tools/validate_package.py
npm ci
npx playwright install chromium   # on Linux you may need --with-deps
npm run format:check
npm test
```

- Coverage must be at least 85% across the integration and Python tools.
- Every production Python function, including nested ones, must have cognitive
  complexity of at most 10.
- Bandit fails on any finding; an intentional exception needs a `# nosec <test id>`
  comment with its reason on the line above.
- Python tests use synthetic data and stubbed Home Assistant boundaries; no private
  recordings are needed.
- The browser harness drives the panel against simulated websocket responses and writes
  `desktop.png` and `mobile.png` into a private `ld2410-panel-*` temporary directory. Set
  `PANEL_SOURCE` to run it against another panel file. It does not exercise a real Home
  Assistant or radar.
- `tools/validate_package.py` checks the package layout, runtime assets, JSON files,
  domain and basic HACS settings; Hassfest in CI covers the broader integration checks.

Caches and coverage output go to the ignored `.local-cache/`. Pytest temporary fixtures
use the system temporary directory, which the hybrid validator's out-of-repository
checks require.

`pre-commit install` adds a hook running ruff, Bandit, prettier, gitleaks and basic file
checks (including LF line endings); `pre-commit run --all-files` runs them on demand.
Hook versions match `requirements-dev.txt` and `package.json`.

[Home Assistant lab](ha-lab.md) describes the checks run in a resource-limited Home
Assistant lab (`tools/ha_lab*.json`).

## CI

GitHub Actions runs, on pushes and pull requests: the package check, Hassfest, Python
tests with coverage, style, complexity, the Chromium regressions, Bandit, a zizmor audit
of the workflows (every action is pinned to a commit) and a redacted gitleaks scan of the
full history. These also gate releases. CodeQL, pip-audit and npm audit run on pull
requests, `main` and weekly, and report without gating. Dependabot proposes weekly pip,
npm and action updates. SonarQube Cloud analysis runs only when a `SONAR_TOKEN` secret
exists.

## Releases

1. Bump `version` in `custom_components/ld2410_tuner/manifest.json` (the panel URL uses
   it to bust the browser cache) and add a [CHANGELOG](../CHANGELOG.md) entry.
2. Push to `main`. When validation passes and the version is higher than the previous
   commit's, the release job tags `v<version>` at that commit and attaches
   `ld2410_tuner.zip`. Commits with an unchanged version publish nothing.

The archive contains only the runtime files listed in `tools/validate_package.py`, with
`manifest.json` at its root. Uploads finish while the release is a draft; reruns verify
existing tags and asset hashes rather than replacing them, and reusing a version for a
different commit fails. Release jobs queue instead of cancelling each other. Local
builds publish nothing:

```sh
python3 tools/release.py
python3 -m unittest discover -s tests -p 'test_release.py'
```

HACS picks up published release tags and downloads the ZIP asset. For unattended
updates, drive the HACS
[update entity](https://www.hacs.dev/docs/use/entities/update/) with `update.install`
and your own restart policy. No workflow submits the repository to the HACS default
catalogue. The license is undecided.

When score calculation, evidence selection, filtering or timing-replay semantics change,
increment `SCORER_VERSION` in `calibration/comparison.py` so stored comparison scores
are recalculated. A frontend-only release does not need it.

## Offline hybrid validation

Run the learner against your own exported history, without Home Assistant:

```sh
python tools/validate_hybrid.py --history /path/to/history-export --output /path/to/validation-report
```

The input can be v2 radar export JSON files, `metadata.json` with a `history/` day-store
directory, or a Home Assistant `.storage` directory containing `ld2410_tuner.data` and
`ld2410_tuner/history/`. The output directory must be outside the repository and
separate from the input. `--jobs N` sets the worker count (default half the logical CPUs,
1 to 8); `--jobs 1` runs in-process.

It fits training folds, measures held-out episodes with an independent replay referee,
records CPU and memory, and writes `report.json`. `--baseline previous/report.json`
re-scores frozen baseline thresholds under the same protocol. Exit codes: 0 pass, 1
numerical or resource failure (a resource failure fails even when accuracy passes), 2
invalid input.

History is read without modification and caches use a detached temporary directory.
Reports replace radar and room identities with anonymous aliases and contain aggregate
metrics, threshold patterns and time intervals, without raw readings or label text.
Nothing is uploaded, but timestamps and aggregates can still reveal usage patterns, so
review reports before sharing them.

`python tests/lab/real_shape_timing.py --output timing.json` reports what the heaviest
jobs cost on a real-shaped synthetic home, each in the production child under its
resource limits on Linux; it fails if a job on a radar with two days of history is
refused. A constrained x86 lab is not a Home Assistant Green performance guarantee.
