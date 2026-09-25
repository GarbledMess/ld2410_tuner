"""Integration configuration and storage constants."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

DOMAIN = "ld2410_tuner"
INTEGRATION_VERSION = json.loads(Path(__file__).with_name("manifest.json").read_text())["version"]
STORAGE_VERSION = 2
STORAGE_KEY = f"{DOMAIN}.data"
GATE_RE = re.compile(
    r"^(?P<prefix>.+)_g(?P<gate>[0-8])_(?P<kind>move|still)_(?P<metric>energy|threshold)$"
)
TRAINING_STATES = {"present", "not_present", "unknown"}
MIN_SAMPLES = 20
MAX_HISTOGRAM_COUNT = 5000
HISTOGRAM_BINS = 101
AUTO_SAMPLE_INTERVAL = 2.0
AUTO_ABSENT_SCORE = 0.75
STORE_DELAY = 5
HISTORY_SAMPLE_INTERVAL = 5.0
HISTORY_BLOCK_SAMPLES = 60
HISTORY_RETENTION_DAYS = 30
HISTORY_RETENTION_SECONDS = HISTORY_RETENTION_DAYS * 86400
HISTORY_RETENTION_CHECK_INTERVAL = timedelta(hours=1)
HISTORY_LABELS_MAX = 1000
FEEDBACK_BIAS_STEP_UP = 0.4
FEEDBACK_BIAS_DECAY = 0.05
PRESENT_BIAS_MIN = -1.0
PRESENT_BIAS_MAX = 4.0
ABSENT_BIAS_MIN = 0.0
ABSENT_BIAS_MAX = AUTO_ABSENT_SCORE * 0.9
FEEDBACK_LOG_MAX = 200
HISTORY_KEYS = [f"g{gate}_{kind}" for gate in range(9) for kind in ("move", "still")]
