"""Fixed learning limits; autolabelling does not change these criteria."""

MIN_CLASS_SAMPLES = 50
MIN_RECALL = 0.999
MAX_FPR = 0.005
MAX_CLASS_SAMPLES = 5000
METHOD = "human_priority_v4"
AUTO_WEIGHT = 0.20
AUTO_CLASS_CAP = 0.25  # At most 20% of combined class evidence when manual data exists.
MIN_AUTO_CONFIDENCE = 0.55
MAX_MISSED_RUN = 1
MAX_FALSE_BURSTS_PER_HOUR = 1.0
SAMPLE_SECONDS = 6.0
