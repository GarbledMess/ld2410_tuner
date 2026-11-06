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
ROBUST_WEIGHT = 1.0
QUIET_SECONDS = 14400.0
QUIET_SLACK = 0.10
QUIET_CAP = 100.0
# A candidate may not detect this much less training presence than the best allowed one.
PRESENCE_FLOOR = 0.005
# Lowering every chosen threshold together: the amounts tried, and the false-active seconds
# worth paying per second of human presence caught. Before trigger pricing, prices 50 and 100
# chose alike and 20 lost one fold (2026-11-05,
# tuned on the same folds, so optimistic); the cost-ranked search alone left this presence.
PRESENCE_SHIFTS = (1, 2, 3, 4, 6, 8, 12)
PRESENCE_ROUNDS = 12  # One gate step per round, so a fit's extra replay work stays bounded.
PRESENCE_PRICE = 50.0
FALSE_TRIGGER_SECONDS = 30.0  # A trigger costs more than its seconds: it can wake a room.
ROBUST_SHORTLIST = 6
ROBUST_SWEEPS = 1
ROBUST_SEARCH_MIN_WEIGHT = 0.05
CELL_CACHE_VERSION = 5
CELL_CACHE_MEMORY_BYTES = 2 * 1024**2
CELL_CACHE_DISK_BYTES = 256 * 1024**2
# Evidence horizons settled 2026-10-31 (exp2 hybrid_fast): automatic evidence counts
# only for the last 28 days before the fit end (held-out accuracy unchanged on every
# fold); human labels count newest first up to 400 labelled hours, keeping the label
# that crosses the cap whole. Real users rarely reach it; it bounds a label-heavy year.
AUTOMATIC_HORIZON_DAYS = 28
HUMAN_EVIDENCE_HOURS = 400

# Label anomaly audit (2026-10-30 dev loop, exp2 hybrid_robust; tuned on the same data,
# so optimistic). Sixty-second windows inside training labels; trimmed references;
# suspects are sub-stretches only and capped per class and per human time.
WINDOW_SECONDS = 60
EMPTY_LIKE_Q = 0.90
EMPTY_LIKE_MAX = 0.5
OCCUPIED_LIKE_Q = 0.99
OCCUPIED_LIKE_MIN = 0.75
ITERATIONS = 3
MIN_FLAGGED_SHARE = 0.75
EDGE_WINDOWS = 2
MIN_WINDOWS = {
    ("present", "human"): 2,
    ("present", "entity"): 1,
    ("not_present", "human"): 3,
    ("not_present", "entity"): 2,
}
CAP_CLASS = 0.8
CAP_HUMAN = 0.25
ANOMALY_ROWS = 50
# Replaces the prototype's 60 CPU-s refit guard: the masked re-score may spend at most
# this many row-pattern operations (rows x re-scored patterns x two passes), so a slow
# machine reaches the same decision as a fast one.
REFIT_OPERATIONS = 2 * EXACT_BUDGET
# Resident joined training cells per audit pass; wider rooms are audited in member groups.
AUDIT_BUFFER_BYTES = 16 * 1024**2
