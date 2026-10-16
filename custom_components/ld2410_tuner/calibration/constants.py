"""Fixed learning limits; autolabelling does not change these criteria."""

MIN_CLASS_SAMPLES = 50
MIN_RECALL = 0.999
MISSED_TIME_COST = (
    5.0  # Finite cost per missed occupied-time fraction versus false-active empty time.
)
MAX_FPR = 0.005
MAX_CLASS_SAMPLES = 5000  # Samples kept per class, human and automatic, newest first.
# Automatic evidence reads a stretch of one guess in snippets of this many consecutive samples
# (the timing model needs consecutive ones: samples more than two apart start a new session),
# spaced so that a whole history yields about twice MAX_CLASS_SAMPLES samples; a history that
# small is read in full. Each snippet sample stands for the samples between.
AUTO_SNIPPET_ROWS = 10
METHOD = "human_priority_v11"
AUTO_CLASS_CAP = 0.25  # At most 20% of combined class evidence when manual data exists.
MIN_AUTO_CONFIDENCE = 0.55
# Which automatic guesses count as evidence (history/trust.py), fitted on the recorded guesses of
# four radars against their human labels, held out by time block and by radar
# (eval/p6b-report.md). A radar's recorded confidence is the age of its newest reference, not a
# probability. A not_present guess was right on every labelled sample (45 000+ over about 60
# episodes), whatever its confidence: 0.95 is the rule-of-three bound for that many episodes.
TRUST_NOT_PRESENT = 0.95
# A present guess by its recorded confidence, from this bound up: with no usable reference (0.65)
# or a reference under about six hours old (0.975 and over) it was right; in between, as the
# reference ages, it was right about one time in six.
TRUST_PRESENT = ((0.0, 0.95), (0.70, 0.15), (0.975, 0.95))
# A present guess does not count where every gate reads within QUIET_QUANTILE of the radar's
# Empty-labelled readings plus QUIET_MARGIN energy units: the radar was "present" at the level
# of an empty room.
QUIET_QUANTILE = 0.999
QUIET_MARGIN = 3
# A room's present estimate needs this many of its members to say present (one in a group of one).
AGREEING_MEMBERS = 2
# A run's confidence is stored in steps of this much.
GUESS_CONFIDENCE_STEP = 0.05
# A radar's recorded guess starts a new run when its state or basis changes, after a gap, or
# when its raw confidence is this far from the run's: a confidence hovering on a step edge makes
# one run, and a run's confidence is off by less than this.
GUESS_CONFIDENCE_DELTA = 0.10
SAMPLE_SECONDS = 6.0
# Attribution (spec 4.3): a radar saw an occupied episode when a gate channel stays above
# its empty-baseline tail quantile plus the margin for the sustained duration. Chosen by
# replaying recorded office data: every labelled occupied run held a streak of >= 260 s,
# while 30 s (5 samples) instead of 18 s cut false attribution on held-out empty runs
# from ~27% to ~10%.
ATTRIBUTION_TAIL_QUANTILE = 0.99
ATTRIBUTION_MARGIN = 3  # Energy units above the baseline quantile.
ATTRIBUTION_SUSTAIN_SECONDS = 30
ATTRIBUTION_FULL_CONFIDENCE_SAMPLES = 100
