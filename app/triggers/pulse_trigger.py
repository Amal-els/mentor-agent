"""L1 triggers (docs/plans/morning-pulse.md M5). Time-based schedule or
explicit user invocation -> TriggerEvent. Zero LLM calls, zero DB access —
that's app/pipeline/pulse.py's job once a TriggerEvent exists."""

import datetime
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TriggerEvent:
    kind: str
    trigger: str  # "cron" | "pull"
    owner_user_id: str
    requested_at: datetime.datetime
    params: dict = field(default_factory=dict)


class CrossUserPulseRequestError(Exception):
    """Decision 4 (docs/plans/morning-pulse.md): cross-user pulses are
    disabled entirely. This is not a placeholder for a future delegation
    grant — there is no grant table, and build_pull_trigger has no
    parameter that bypasses this check."""


def build_cron_trigger(
    owner_user_id: str, requested_at: datetime.datetime
) -> TriggerEvent:
    return TriggerEvent(
        kind="pulse",
        trigger="cron",
        owner_user_id=owner_user_id,
        requested_at=requested_at,
    )


def build_pull_trigger(
    requesting_user_id: str,
    target_owner_user_id: str,
    requested_at: datetime.datetime,
    date: datetime.date | None = None,
) -> TriggerEvent:
    """`/mentor pulse [date]` builds this. Refuses unconditionally when the
    requester isn't the target owner — pulse.md's "requested for someone
    else" edge case."""
    if requesting_user_id != target_owner_user_id:
        raise CrossUserPulseRequestError(
            f"user {requesting_user_id!r} may not request a pulse for "
            f"{target_owner_user_id!r} — no delegation mechanism exists"
        )
    params = {"date": date.isoformat()} if date is not None else {}
    return TriggerEvent(
        kind="pulse",
        trigger="pull",
        owner_user_id=target_owner_user_id,
        requested_at=requested_at,
        params=params,
    )
