from app.dossier_payload import build_dossier_a2ui_payload


def test_builds_one_component_per_delivery():
    deliveries = [
        {"id": "d1", "event_external_id": "evt-1", "sent_at": "2026-08-19T09:00:00+00:00",
         "who": "Sam External", "why_now": "Renewal next week."},
    ]
    payload = build_dossier_a2ui_payload(deliveries)
    assert payload == {
        "components": [
            {"type": "dossier_card", "item_id": "d1", "event_external_id": "evt-1",
             "sent_at": "2026-08-19T09:00:00+00:00", "who": "Sam External",
             "why_now": "Renewal next week."},
        ]
    }
