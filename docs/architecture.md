# Code ownership and invariants

The Home Assistant adapter owns lifecycle and external calls. Pure numerical code
has no Home Assistant dependency. Runtime operations receive one explicit
`TunerRuntime` argument; its class exposes a stable API by binding named functions
from the feature modules. There is no mixin hierarchy or import-time registration.

```text
custom_components/ld2410_tuner/
├── __init__.py              # Home Assistant lifecycle
├── config_flow.py           # Required Home Assistant entrypoint
├── const.py                 # Integration-wide constants
├── runtime/                 # Coordinator, discovery, websocket API
├── presence/                # Automatic labels and pure inference
├── history/                 # Recording, cleanup, human labels/timeouts
├── calibration/             # Explicit Learn/Apply, fitting, search, metrics
├── presentation/            # History queries, dashboard snapshots/exports
├── static/
│   ├── ld2410-tuner-panel.js # Public panel entrypoint
│   └── panel/               # Card, controls, learning summaries, chart,
│                            # SVG drawing, selection, shared constants/styles
├── brand/
└── translations/
```

| Package | Modules and responsibility |
| --- | --- |
| `runtime/` | `coordinator.py` owns state/tasks; `discovery.py` follows registry changes; `websocket.py` exposes administrator commands |
| `presence/` | `autolabelling.py` records guesses/feedback; `inference.py` implements the pure temporal estimator |
| `history/` | `recording.py` owns buffers/executor snapshots; `cleanup.py` normalizes stored blocks; `labels.py` owns human labels/timeouts |
| `calibration/` | `service.py` owns explicit Learn/Apply and hardware guards; `fitting.py`, `search.py`, `metrics.py`, `constants.py` own the numerical learner |
| `presentation/` | `charts.py` aggregates history; `snapshots.py` builds dashboard/export data |
| `static/panel/` | `view.js` owns the shell/draw lifecycle; `card.js`, `controls.js`, `learning.js` own card sections and interactions; `chart.js`, `visualization.js`, `selection.js` own history requests, SVG drawing, and range interaction; `constants.js` owns shared values |

Package initializers only document ownership; imports name concrete modules. The
release allowlist includes each initializer and nested asset explicitly. Tests and
replay tools use the same grouped module paths as the installed integration.

## Contracts preserved by the refactor

- Human labels and explicit UNKNOWN exclusions override automatic guesses. Guesses
  never enter the authoritative human reference histograms.
- Learn and Apply are separate user commands. The estimator only records training
  evidence; the physical radar still operates with static gate thresholds.
- The fitting search retains its iteration order and strict tie-breaking. The
  refactor does not change acceptance targets, automatic weights, or Apply guards.
- Missing energy is not zero. Old observations without confidence do not acquire
  invented automatic labels. Cleanup preserves valid timestamps and label priority.
- Executor work uses detached views. Revision guards prevent stale work from
  overwriting newer sampling or human corrections.
- History windows share a fixed ending timestamp until Refresh. Obsolete requests
  cannot overwrite the current chart; failed refreshes retain the last plot.
- Static modules ship together in the explicit release allowlist. The versioned
  panel entrypoint and uncached static responses require a full panel reload after
  an update.

## Quality checks

`tools/check_complexity.py` checks every function in the integration and Python
tools, including nested functions, against a ceiling of 10. Ruff enforces a common
format and catches unused code, import errors, and unsafe Python patterns. Pytest
coverage includes all integration and tool files and fails below 85%. Playwright
loads the real ES modules against a simulated Home Assistant websocket boundary.

Exact before/after comparisons covered 6,000 estimator observations, 24 varied
threshold-fitting fixtures, history cleanup across ten stored devices at two
retention windows, and all 15 replay reports. Private recordings and saved baseline
code are not part of the repository. Synthetic regression tests remain in `tests/`.
These checks do not substitute for a live Home Assistant and hardware trial.

## Verified local result

| Measure | Before | After |
| --- | ---: | ---: |
| Integration entrypoint lines | 1,436 | 135 |
| Maximum production Python cognitive complexity | 143 | 10 |
| Regression tests | 83 | 104 |
| Full integration and Python tools line coverage | 64% | 94.65% |

All 201 production Python functions meet the complexity ceiling. Ruff lint and
format checks, Prettier, and the Chromium regression harness pass. The deterministic
1.9.3 archive contains all 36 runtime assets and excludes development dependencies,
tests and recordings. CI is configured to repeat the checks before version-triggered
releases; a hosted CI run and a live hardware trial have not been performed locally.

## Sonar cleanup

Cancellation now propagates after timeout cleanup, so cancelled tasks remain
observably cancelled. Device iteration deliberately uses a shallow snapshot across
executor awaits; registry discovery cannot invalidate its iterator. Synchronous
state updates use Home Assistant's callback decorator, and redundant setup hooks
and unused internal arguments have been removed.

The card renderer is split into focused renderers and event wiring; SVG drawing is
separate from chart fetching and refresh state. Nested conditional/template
expressions were replaced with named intermediate values or helpers. The browser
regressions exercise the actual nested ES imports and retained DOM controls.

The local SonarQube for IDE extension (5.10.0) analyzed all 41 Python/JavaScript
source and test files with its installed Python 5.31.0 and JavaScript 13.9.0
analyzers. The default active rules reported 61 findings before cleanup and zero
issues or security hotspots afterwards. No `NOSONAR` markers, disabled rules, or
source exclusions were added. This is local extension evidence, not a hosted
SonarQube server quality-gate result.
