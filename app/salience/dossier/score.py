from dataclasses import dataclass

W_ATTENDEE_RARITY = 1.0
W_IS_EXTERNAL = 1.5
W_UNRESOLVED_THREADS = 0.5
W_DEADLINE_PROXIMITY = 1.5
W_IS_ONE_ON_ONE = 1.0
W_PREP_ABSENT = 1.0
W_RECURRENCE_FAMILIARITY = 1.0


@dataclass(frozen=True)
class DossierCandidateInputs:
    attendee_rarity: float          # 0-1, how rarely this owner meets these attendees
    is_external: bool
    unresolved_threads: int         # count of unresolved Slack/thread signals
    deadline_proximity: float       # 0-1, how close the nearest shared deadline is
    is_one_on_one: bool
    prep_absent: bool               # no dossier ever sent for this pairing before
    recurrence_familiarity: float   # 0-1, how routine/frequent this meeting series is


def score_dossier_candidate(inputs: DossierCandidateInputs) -> float:
    return (
        W_ATTENDEE_RARITY * inputs.attendee_rarity
        + W_IS_EXTERNAL * float(inputs.is_external)
        + W_UNRESOLVED_THREADS * inputs.unresolved_threads
        + W_DEADLINE_PROXIMITY * inputs.deadline_proximity
        + W_IS_ONE_ON_ONE * float(inputs.is_one_on_one)
        + W_PREP_ABSENT * float(inputs.prep_absent)
        - W_RECURRENCE_FAMILIARITY * inputs.recurrence_familiarity
    )
