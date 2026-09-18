"""The tier ladder (design spec §4.3, §6). Pure functions only — no DB access,
no owner concept (roster is already owner-filtered by roster.py). Everything
here is fixture-testable against tests/identity/golden.yaml without a
database. Only match_idp through match_alias_email (tiers 0-2) are
categorical; match_exact_name through match_fuzzy_name (tiers 3-5) always
produce a scored MatchCandidate that passes through gate().

Gate thresholds, context-prior deltas, per-tier base scores, and tier-4/5
inclusion floors all live only in app.identity.config (spec constraint) —
nothing here hardcodes a threshold or a base score.
"""

import difflib
import re
from typing import Literal

from app.identity import config
from app.identity.normalize import normalize_email, normalize_handle
from app.identity.roster import RosterSnapshot
from app.identity.types import MatchCandidate, RawReference

# Suffixes/decorations that don't carry identity signal and are stripped
# before comparing a handle to a person's name: contractor/dev tags and
# trailing digit disambiguators (e.g. "jsmith2"). Stripping these is matcher
# logic, not key logic (spec §4.4) — it never touches build_reference_key.
_HANDLE_SUFFIX_RE = re.compile(r"(-dev|-ext|_ext|\d+)+$")

# Known secondary email domains an org uses for the same mailbox, mapped to
# the canonical domain (design spec §12 notes this table's exact format is
# open; a config-level mapping is the simplest DB-free representation).
_ALIAS_DOMAINS = {"acme.io": "acme.com"}


def match_idp(ref: RawReference, roster: RosterSnapshot) -> MatchCandidate | None:
    # No IdP/SSO connector exists yet (spec §9) — always returns None until
    # one does. resolve.py can call this uniformly from day one.
    return None


def match_primary_email(
    ref: RawReference, roster: RosterSnapshot
) -> MatchCandidate | None:
    if not ref.email:
        return None
    target = normalize_email(ref.email)
    for person in roster.people:
        if person.primary_email and normalize_email(person.primary_email) == target:
            return MatchCandidate(
                person_id=person.id, tier=1, score=config.TIER1_PRIMARY_EMAIL_SCORE
            )
    return None


def _alias_local_part(primary_email: str) -> str | None:
    """Derives the "first-initial.surname" alias-domain local part from a
    verified primary_email (e.g. "sarah.benyoussef@acme.com" ->
    "s.benyoussef"). Ties the alias guess to the already-verified primary
    email rather than free-parsing canonical_name, so two people who happen
    to share a display name never collide here purely by name shape."""
    local, _, _domain = primary_email.partition("@")
    first, sep, rest = local.partition(".")
    if not sep or not first or not rest:
        return None
    return f"{first[0]}.{rest}"


def match_alias_email(
    ref: RawReference, roster: RosterSnapshot
) -> MatchCandidate | None:
    if not ref.email:
        return None
    local, _, domain = normalize_email(ref.email).partition("@")
    canonical_domain = _ALIAS_DOMAINS.get(domain)
    if canonical_domain is None:
        return None
    matches = [
        person
        for person in roster.people
        if person.primary_email
        and _alias_local_part(normalize_email(person.primary_email)) == local
    ]
    if len(matches) != 1:
        # Ambiguous (or no) derived alias local-part — same hazard class as
        # match_exact_name's duplicate-name guard (spec: two people whose
        # derived alias collides must never be silently, deterministically
        # picked at a categorical/"verified" tier). Mirror that guard exactly.
        return None
    return MatchCandidate(
        person_id=matches[0].id, tier=2, score=config.TIER2_ALIAS_EMAIL_SCORE
    )


def match_exact_name(
    ref: RawReference, roster: RosterSnapshot
) -> MatchCandidate | None:
    if not ref.display_name:
        return None
    target = normalize_handle(ref.display_name)
    matches = [
        person
        for person in roster.people
        if person.is_active and normalize_handle(person.canonical_name) == target
    ]
    if len(matches) != 1:
        return None
    return MatchCandidate(
        person_id=matches[0].id, tier=3, score=config.TIER3_EXACT_NAME_SCORE
    )


def _prefix_ratio(a: str, b: str) -> float:
    """Similarity based on shared leading characters only, not "share a
    substring anywhere." Handles are conventionally built from the front of
    a name (firstname, firstname+suffix, initial+surname, ...), so a genuine
    match shares a long common prefix. A coincidental substring shared in the
    middle of two otherwise-different strings (e.g. "sbenali" vs
    "sarahbenali", which merely share the "benali" tail) is not evidence of
    identity and must not score as if it were — that's exactly the
    adversarial case the golden set punishes.

    Known trade-off (documented, not a bug): this is a real recall
    regression vs. a full-string ratio for legitimate "first-initial+
    surname" handles — e.g. "jsmith" against canonical name "John Smith"
    now scores below the tier-4 inclusion floor (no common prefix: 'j' vs
    'j' matches, then 's' vs 'o' doesn't), where a full-string comparison
    would have caught it via the shared "smith" tail. That's a missed
    match, not a false merge, consistent with this module's precision-over-
    recall design principle — not something to fix by loosening the metric
    back toward substring matching."""
    if not a or not b:
        return 0.0
    common = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        common += 1
    return 2 * common / (len(a) + len(b))


def match_handle_heuristic(
    ref: RawReference, roster: RosterSnapshot
) -> list[MatchCandidate]:
    if not ref.handle:
        return []
    stem = _HANDLE_SUFFIX_RE.sub("", normalize_handle(ref.handle))
    if not stem:
        return []
    candidates = []
    for person in roster.people:
        name_stem = "".join(normalize_handle(person.canonical_name).split())
        ratio = _prefix_ratio(stem, name_stem)
        if ratio >= config.TIER4_HANDLE_HEURISTIC_FLOOR:
            candidates.append(
                MatchCandidate(person_id=person.id, tier=4, score=round(ratio, 4))
            )
    return candidates


def match_fuzzy_name(ref: RawReference, roster: RosterSnapshot) -> list[MatchCandidate]:
    if not ref.display_name:
        return []
    target = normalize_handle(ref.display_name)
    candidates = []
    for person in roster.people:
        name = normalize_handle(person.canonical_name)
        ratio = difflib.SequenceMatcher(None, target, name).ratio()
        if (
            config.TIER5_FUZZY_NAME_FLOOR <= ratio < 1.0
        ):  # exact ties are tier 3's job, not tier 5's
            candidates.append(
                MatchCandidate(person_id=person.id, tier=5, score=round(ratio, 4))
            )
    return candidates


def _relationship_with_self(person_id: str, roster: RosterSnapshot):
    self_person = next((p for p in roster.people if p.is_self), None)
    if self_person is None or self_person.id == person_id:
        return None
    pair = tuple(sorted((self_person.id, person_id)))
    return roster.relationships.get(pair)


def _person_is_active(person_id: str, roster: RosterSnapshot) -> bool:
    return any(p.id == person_id and p.is_active for p in roster.people)


def apply_context_priors(
    candidates: list[MatchCandidate], ref: RawReference, roster: RosterSnapshot
) -> list[MatchCandidate]:
    """Applies §4 context priors to tier 4/5 candidates only (tiers 0-3 are
    unaffected — they're either categorical or already gate-bound on their own
    fixed score). Priors are computed relative to the self person (the user
    this Mentor instance serves) via PersonRelationship, keyed (a, b) with
    a < b per spec §5."""
    active_count = sum(1 for c in candidates if _person_is_active(c.person_id, roster))
    adjusted = []
    for c in candidates:
        if c.tier not in (4, 5):
            adjusted.append(c)
            continue
        delta = 0.0
        rel = _relationship_with_self(c.person_id, roster)
        if rel is not None and rel.co_meeting_count > 0:
            delta += config.CO_MEETING_PRIOR
        if rel is not None and rel.shared_project_count > 0:
            delta += config.SHARED_PROJECT_PRIOR
        if active_count > 1:
            delta += config.ACTIVE_CANDIDATES_PENALTY
        adjusted.append(
            MatchCandidate(
                person_id=c.person_id,
                tier=c.tier,
                score=max(0.0, min(1.0, c.score + delta)),
            )
        )
    return adjusted


def gate(
    score: float, margin: float, roster_size: int
) -> Literal["auto_link", "ask", "none"]:
    if (
        score >= config.AUTO_LINK_SCORE
        and margin >= config.SINGLE_CANDIDATE_MARGIN
        and roster_size >= config.MIN_ROSTER_SIZE
    ):
        return "auto_link"
    if score >= config.ASK_SCORE and margin >= config.MARGIN_MIN:
        return "ask"
    return "none"
