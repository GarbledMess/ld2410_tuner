"""Numerical contract tuned on 2026-10 evidence (exp2-cv-v2).

The accepted validation includes R1 fold 3 drift; these values are not
universal guarantees against held-out longest-miss regression.
"""

CELL_SECONDS = 6
CHANNELS = 18
MISSING = 255
MISSED_WEIGHT = 5.0
TOLERANCE = 0.5
EVIDENCE_POINTS = 20.0
SHORTLIST = 16
SWEEPS = 3
TOP_CROSSINGS = 4
EXACT_BUDGET = 30_000_000
MIN_EXACT = 2
WARMUP_SECONDS = 1800.0
CHUNK_SECONDS = 21600.0
ROW_BLOCK_ROWS = 4096
SPILL_BYTES = 8 * 1024**2
SHARD_CELLS = 4096
MASK_CACHE_BYTES = 1024**2
ATT_NUM = 3
ATT_DEN = 5
ROBUST_WEIGHT = 0.5
QUIET_SECONDS = 14400.0
QUIET_SLACK = 0.10
QUIET_CAP = 50.0
ROBUST_SHORTLIST = 6
ROBUST_SWEEPS = 1
ROBUST_SEARCH_MIN_WEIGHT = 0.05
CELL_CACHE_VERSION = 3
CELL_CACHE_MEMORY_BYTES = 2 * 1024**2
CELL_CACHE_DISK_BYTES = 256 * 1024**2
# Evidence horizons settled 2026-10-31 (exp2 hybrid_fast): automatic evidence counts
# only for the last 28 days before the fit end (held-out accuracy unchanged on every
# fold); human labels count newest first up to 400 labelled hours, keeping the label
# that crosses the cap whole. Real users rarely reach it; it bounds a label-heavy year.
AUTOMATIC_HORIZON_DAYS = 28
HUMAN_EVIDENCE_HOURS = 400
