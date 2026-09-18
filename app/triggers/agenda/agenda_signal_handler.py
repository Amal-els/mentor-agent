"""AG-UI webhook receiver (design spec §4.6) — inbound, not part of the
ADK agent tree; the orchestrator does not poll for this. Every signal
carries the acting user's identity; resolve_pair_scope is called before
any store function, and a None result is rejected outright, the same way
a former manager's signal is rejected — this handler has no separate
authorization logic of its own.

The one exception is the visibility check below: a keep/drop/field_edit
signal targeting a report_only/manager_only item from the non-visible
party is "ignored, not an error" (design spec §4.3.1/§7), same rejection
shape as the former-manager case, not a 500/exception. It reuses
app.agenda.store.is_visible_to — the exact same rule get_agenda filters
reads through — rather than reimplementing a second copy here."""

from sqlalchemy.orm import Session

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import (
    get_agenda_item,
    is_visible_to,
    mark_resolved,
    update_agenda_item,
)
from app.core.clock import SystemClock

# field_edit is the one branch that forwards an external, caller-supplied
# dict straight into update_agenda_item's changes -> setattr(row, key,
# value) loop (Task 10). Without an allowlist here, a webhook signal could
# set fields no client should ever touch directly — report_user_id/id/
# pair_id (identity/ownership), created_by_user_id/created_by_role
# (provenance), status/surfaced_count (owned by the keep/drop branches and
# the consent gate), resolved_at. Only the fields a human is actually
# editing when they "edit" an agenda item are allowed through; anything
# else in signal["changes"] is silently dropped rather than applied.
FIELD_EDIT_ALLOWED_FIELDS = {"text", "visibility"}


def handle_agenda_signal(session: Session, signal: dict) -> dict:
    scope = resolve_pair_scope(
        session, signal["report_user_id"], signal["acting_user_id"]
    )
    if scope is None:
        return {"status": "rejected", "reason": "no active relationship to this report"}

    clock = SystemClock()
    signal_type = signal["signal_type"]

    if signal_type in ("field_edit", "keep", "drop"):
        # Fetch-then-check rather than threading the check into
        # update_agenda_item/mark_resolved: those two are shared by every
        # store.py caller (e.g. SynthesizeOutcomeAgent re-surfacing an
        # item), most of which have nothing to do with an AG-UI signal
        # and shouldn't have to pass an acting-party check they don't
        # need. A missing item_id (item is None) is left to the existing
        # scalar_one() in update_agenda_item/mark_resolved to raise on,
        # unchanged from before this fix.
        target_item = get_agenda_item(scope, signal["item_id"])
        if target_item is not None and not is_visible_to(target_item, scope):
            return {
                "status": "rejected",
                "reason": "item not visible to acting user",
            }

    if signal_type == "field_edit":
        changes = {
            key: value
            for key, value in (signal["changes"] or {}).items()
            if key in FIELD_EDIT_ALLOWED_FIELDS
        }
        update_agenda_item(
            scope, signal["item_id"], changes, signal["acting_user_id"], clock
        )
    elif signal_type == "keep":
        update_agenda_item(
            scope,
            signal["item_id"],
            {"status": "open", "surfaced_count": 0},
            signal["acting_user_id"],
            clock,
        )
    elif signal_type == "drop":
        mark_resolved(scope, signal["item_id"], signal["acting_user_id"], clock)
    else:
        return {"status": "rejected", "reason": f"unknown signal_type: {signal_type!r}"}

    return {"status": "ok"}
