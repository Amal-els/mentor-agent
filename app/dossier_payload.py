"""Deterministic A2UI JSON assembly for the dossier panel, same discipline
as app/agenda/payload.py: pure, no LLM, no side effects, fixture-testable."""


def build_dossier_a2ui_payload(deliveries: list[dict]) -> dict:
    components = [
        {
            "type": "dossier_card",
            "item_id": d["id"],
            "event_external_id": d["event_external_id"],
            "sent_at": d["sent_at"],
            "who": d["who"],
            "why_now": d["why_now"],
        }
        for d in deliveries
    ]
    return {"components": components}
