"""Deterministic A2UI JSON assembly (design spec §4.4c). The LLM never
emits A2UI JSON directly — this function is the only thing that does,
and it is pure/side-effect-free so it's testable against fixtures rather
than live LLM output, the same discipline app/fixtures/pulse/ established
for this codebase's other composition-layer testing."""


def build_a2ui_payload(items: list[dict], pending_consent_items: list[dict]) -> dict:
    components = []

    for item in items:
        components.append(
            {
                "type": "editable_text",
                "item_id": item["id"],
                "text": item["text"],
                "source": item["source"],
            }
        )
        components.append(
            {
                "type": "visibility_toggle",
                "item_id": item["id"],
                "current": item["visibility"],
                "options": ["shared", "manager_only", "report_only"],
            }
        )

    for item in pending_consent_items:
        components.append(
            {
                "type": "consent_card",
                "item_id": item["id"],
                "text": item["text"],
                "actions": ["keep", "drop"],
            }
        )

    components.append({"type": "add_item"})

    return {"components": components}
