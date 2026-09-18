# tests/unit/test_dossier_score.py
from app.salience.dossier.score import DossierCandidateInputs, score_dossier_candidate


def test_score_rewards_external_unresolved_deadline_pressure():
    high = DossierCandidateInputs(
        attendee_rarity=0.9,
        is_external=True,
        unresolved_threads=3,
        deadline_proximity=0.8,
        is_one_on_one=False,
        prep_absent=True,
        recurrence_familiarity=0.0,
    )
    low = DossierCandidateInputs(
        attendee_rarity=0.1,
        is_external=False,
        unresolved_threads=0,
        deadline_proximity=0.0,
        is_one_on_one=False,
        prep_absent=False,
        recurrence_familiarity=1.0,
    )
    assert score_dossier_candidate(high) > score_dossier_candidate(low)


def test_recurrence_familiarity_is_subtracted():
    familiar = DossierCandidateInputs(
        attendee_rarity=0.5,
        is_external=False,
        unresolved_threads=0,
        deadline_proximity=0.0,
        is_one_on_one=True,
        prep_absent=False,
        recurrence_familiarity=1.0,
    )
    unfamiliar = DossierCandidateInputs(
        attendee_rarity=0.5,
        is_external=False,
        unresolved_threads=0,
        deadline_proximity=0.0,
        is_one_on_one=True,
        prep_absent=False,
        recurrence_familiarity=0.0,
    )
    assert score_dossier_candidate(familiar) < score_dossier_candidate(unfamiliar)
