import datetime
from app.core.clock import Clock
from app.core.scope import OwnerScope
from app.salience.pulse.assemble import assemble_and_score
from app.salience.pulse.gate import apply_gate
from app.salience.pulse.types import PulseContext

def build_pulse_context(
    scope: OwnerScope,
    clock: Clock,
    source_clients: list,
    trigger: str,
    requested_at: datetime.datetime,
    shortlist_limit: int | None = None,
) -> PulseContext:
    pre_gate = assemble_and_score(scope, clock, source_clients, limit=shortlist_limit)
    survivors, removals = apply_gate(scope, clock, pre_gate, trigger)
    return PulseContext(
        owner_user_id=pre_gate.owner_user_id,
        owner_tz=pre_gate.owner_tz,
        window_date=pre_gate.window_date,
        window_reason=pre_gate.window_reason,
        trigger=trigger,
        requested_at=requested_at,
        shortlist=survivors,
        day_events=pre_gate.day_events,
        owed=pre_gate.owed,
        degraded_sources=pre_gate.degraded_sources,
        removals=removals,
        weights=pre_gate.weights,
    )