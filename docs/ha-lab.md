# LD2410 checks in the shared HA lab

The lab implementation, caches, VM disks and setup artifacts live outside this
repository. Use the user-level `shared-ha-lab` skill in Codex or Claude, or read
the shared lab installation's `README.md`.

From this checkout, with `HA_LAB_HOME` set to the external shared lab installation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:HA_LAB_HOME/ha-lab.ps1" --config tools/ha_lab.json --project .
```

Use `tools/ha_lab_browser.json` for browser checks. Project-owned QA recipes live
in `tests/lab`; the shared tool provides the generic VM/container environment.
Default state is external: `<user-temp>/shared-ha-lab/machine`. All projects reuse
this VM disk and its Linux/Docker/HA cache. The global
`<user-temp>/shared-ha-lab/run.lock` admits one run across all projects and state
overrides. Every run gets fresh HA configuration/container/source selection and
separate diagnostics under `machine/runs/<project-id>/<run-id>`.
No private `tests/data` inputs are exported.

The final production-worker run passed **2,155 tests with 97.02% coverage**
against the retained 96.95% floor. Ruff lint/format, complexity (maximum 10),
package validation, integration setup and API smoke checks passed. The runtime
was HA 2026.10.2 / Python 3.14.6, with one CPU, 1536 MiB RAM, 256 PIDs and
no swap devices, inside the shared 2-vCPU/2-GiB Linux VM.

Four genuine production learning children each processed 6,000 synthetic rows.
Four additional storage children flushed journals, trimmed expired history and
recovered startup state, preserving all 6,000 rows. The live HA server answered
257 HTTP requests: p95 14.135 ms, maximum 31.154 ms. All workers were reaped.
Startup, transaction, supervisor, capture, protocol and limit modules have 100%
statement coverage in the complete suite. The production-route regression tests also
verify numerical parity, stale-result rejection and actual child priority,
OOM adjustment and resource limits. These are synthetic constrained x86 checks,
not a timing guarantee for every installation or an actual ARM hardware test.

The separate browser run passed formatting and the complete panel regression
recipe, including server-backed jobs, reconnects, progress, duplicate actions,
export errors/timeouts, mobile/touch interactions and stale saves, with no browser
errors. A temporary HACS-format archive built successfully; the released archive
was unchanged. The lab QA script explicitly checks coverage XML so a runner
cannot silently accept a below-floor percentage.

Diagnostic runs under the external shared state:

- Full QA and production HTTP probe:
  `machine/runs/ld2410_tuner-1340dbebdf00/20261020-103455-eb326814d582bd19`.
- Browser:
  `machine/runs/ld2410_tuner-1340dbebdf00/20261020-103235-f5b4efbbf3fbde7c`.
- Independent-project disk/source isolation verification:
  `machine/runs/cross-project-probe-453da40d344d/20261019-152850-96f12c17ac9e3360`.

See [the worker implementation and safety boundary](hacs-worker-investigation.md).
No publication or production installation was made during this verification.
The iteration was subsequently accepted for local closeout; see
[the closeout](iteration-closeout.md).

## 1.25.1 production regression verification

The final regression run passed **2,193 tests with 97.04% coverage** in the same
constrained HA container. This includes real CPU-budget exhaustion, legacy
float-day decoding, bounded cache retention, a real learning-through-storage
save, saturated background admission and a full populated snapshot with
filesystem calls forbidden. Lint, formatting, complexity and package checks passed.
The complete panel browser recipe and browser formatting passed with no browser errors.

The production HTTP probe preserved 6,000 rows across four learning and four
storage children; HA answered 288 requests with p95 11.640 ms and maximum
37.489 ms. A separate protected worker learned from 86,400 rows and 18 legacy
float gates in 5.641 CPU seconds / 5.781 wall seconds, with 100,028 KiB peak RSS.
The mandatory 512-MiB address-space and 120-second hard CPU ceilings remain.
These representative synthetic checks do not determine the exact cause of a
previous production SIGKILL or guarantee that every workload fits its budgets.

Diagnostics: `machine/runs/ld2410_tuner-1340dbebdf00/20261020-145949-2a77437a2d8eb440`.
