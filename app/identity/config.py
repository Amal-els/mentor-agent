"""Tunable constants for identity resolution (spec §4.3, §4.5, §4.8, §6). No
literal threshold values may appear anywhere else in app/identity/."""

AUTO_LINK_SCORE = 0.95
ASK_SCORE = 0.70
MARGIN_MIN = 0.15
SINGLE_CANDIDATE_MARGIN = MARGIN_MIN * 2  # margin required vs. the runner-up
# for the near-certain auto-link gate
MIN_ROSTER_SIZE = 2  # below this, auto-link never fires (cold-start guard)

CO_MEETING_PRIOR = 0.10
SHARED_PROJECT_PRIOR = 0.10
ACTIVE_CANDIDATES_PENALTY = -0.15

# Tier base scores (spec §4.3, §6) — the fixed confidence each categorical/
# near-categorical tier reports. Deliberately ordered so a lower tier can
# never outrank a higher one on score alone, and TIER3_EXACT_NAME_SCORE is
# deliberately below AUTO_LINK_SCORE so tier 3 can structurally never
# auto-link (spec: "exact name is not categorical and never auto-links").
TIER1_PRIMARY_EMAIL_SCORE = 0.98
TIER2_ALIAS_EMAIL_SCORE = 0.92
TIER3_EXACT_NAME_SCORE = 0.85

# Tier 4/5 inclusion floors — the minimum similarity score for a fuzzy
# candidate to be produced at all (below these, a person is not even a weak
# candidate). Genuine decision thresholds, not just confidence outputs.
TIER4_HANDLE_HEURISTIC_FLOOR = 0.6
TIER5_FUZZY_NAME_FLOOR = 0.4

FRIDAY_BATCH_CAP = 7
MAX_ASKS_PER_CANDIDATE = 1  # an expired or rejected ask is never re-surfaced
PENDING_CONFIRMATION_TTL_DAYS = 7
KEY_VERSION = 1
