"""Job definitions and clip state transitions (SPEC §3). Jobs arrive from M1 onward."""

from __future__ import annotations

# Clip states in pipeline order.
PIPELINE_STATES = (
    "found",
    "fetched",
    "filtered",
    "transcribed",
    "written",
    "rendered",
    "qa_passed",
    "scheduled",
    "posted",
    "submitted",
)
# States a clip can end in off the main line.
SIDE_STATES = ("rejected", "qa_failed", "dead")
ALL_STATES = PIPELINE_STATES + SIDE_STATES

# A failed step retries after these delays; the third failure marks the clip dead.
RETRY_DELAYS_MIN = (1, 5, 30)
MAX_FAILURES = 3
