"""Tunable constants for L5 salience (docs/plans/morning-pulse.md M3). No
literal threshold values may appear anywhere else in app/salience/."""

# Base scores per item type — before urgency/relevance bonuses or weight.
BASE_SCORE_EVENT = 5.0
BASE_SCORE_WORK_ITEM = 4.0
BASE_SCORE_MESSAGE = 4.0
BASE_SCORE_COMMITMENT = 4.5

# Urgency bonuses (decision 2: "explicit deadline/time pressure within window").
URGENCY_BONUS_DUE_TODAY = 2.0
URGENCY_BONUS_OVERDUE = 3.0

# Relevance bonus (decision 2: "someone is blocked waiting on the user, OR it
# moves a tracked goal" — goal-linkage isn't modeled on Event/WorkItem yet,
# so only the person-waiting half is implemented).
RELEVANCE_BONUS_PERSON_WAITING = 1.5

# Priority-ranking signals (docs/whiteboards/
# Data_Flow_Diagram_For_Command_Center_Brief.excalidraw's priority table).
# "Blocking others" — high weight, same order of magnitude as an overdue
# deadline: a ticket with a downstream dependent is effectively blocking
# someone else's work, not just the owner's.
BLOCKING_OTHERS_BONUS = 3.0

# "Staleness" — medium, scales with age: a per-day bonus once a work item
# or message thread has gone quiet for STALENESS_THRESHOLD_DAYS+, capped so
# an ancient item doesn't dominate every other signal.
STALENESS_THRESHOLD_DAYS = 2
STALENESS_BONUS_PER_DAY = 0.3
STALENESS_BONUS_MAX = 3.0

# "Meeting proximity" — medium: does today's calendar include a meeting
# involving the same person this item is tied to.
MEETING_PROXIMITY_BONUS = 1.5

# "Action requested" (email-specific, from the Gmail triageInbox tool's own
# heuristic classification) — someone explicitly needs something from you,
# same order of magnitude as blocking others: a message you can ignore vs.
# one that's actually asking for something are not the same priority.
ACTION_REQUESTED_BONUS = 3.0

# "Source recency" is deliberately NOT a bonus here — the whiteboard table
# marks it "filters noise, not urgency itself". It's carried as an
# informational score_term (is_new_since_last_pulse) only.

# There is deliberately no pre-ranker candidate cap here anymore (removed —
# see docs/plans/morning-pulse.md's note on the shortlist-cap removal):
# ranker_agent/critic_agent see every scored candidate, not a pre-cut top N,
# so a genuinely high-priority item can never be excluded before the LLM
# gets a chance to weigh it against the priority signals. Commitments never
# enter this competition either way — pulse.md keeps "Owed" a separate,
# unranked card section, oldest-first.

# Push (cron) budget — applied by the suppression+budget gate, after
# suppression removals, over the full (uncapped) scored candidate pool.
# Pull always bypasses this.
BUDGET_PUSH_MAX_ITEMS = 5

# Weight read-path: lazy decay toward the neutral multiplier (1.0) over this
# many days since the weight was last written, then clamp.
WEIGHT_NEUTRAL = 1.0
WEIGHT_DECAY_WINDOW_DAYS = 30
WEIGHT_CLAMP_MIN = 0.2
WEIGHT_CLAMP_MAX = 3.0

# How far a weight must have drifted from WEIGHT_NEUTRAL (as a fraction,
# e.g. 0.05 = 5%) before app/delivery/cards.py's personalization note
# mentions it on the pulse card — below this, the drift is noise from a
# handful of clicks, not a settled preference worth surfacing.
PERSONALIZATION_NOTE_THRESHOLD = 0.05
