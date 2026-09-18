from pathlib import Path

import yaml

from app.agenda.payload import build_a2ui_payload

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "agenda"


def _load(name: str) -> dict:
    return yaml.safe_load((FIXTURES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def test_simple_summary_includes_every_item_as_an_editable_field():
    fixture = _load("simple_summary")

    payload = build_a2ui_payload(fixture["items"], fixture["pending_consent_items"])

    item_ids = {
        node["item_id"]
        for node in payload["components"]
        if node["type"] == "editable_text"
    }
    assert item_ids == {"item-1", "item-2"}


def test_simple_summary_has_no_consent_cards():
    fixture = _load("simple_summary")

    payload = build_a2ui_payload(fixture["items"], fixture["pending_consent_items"])

    consent_cards = [
        node for node in payload["components"] if node["type"] == "consent_card"
    ]
    assert consent_cards == []


def test_pending_consent_items_get_one_keep_drop_card_each():
    fixture = _load("with_pending_consent")

    payload = build_a2ui_payload(fixture["items"], fixture["pending_consent_items"])

    consent_cards = [
        node for node in payload["components"] if node["type"] == "consent_card"
    ]
    assert len(consent_cards) == 1
    assert consent_cards[0]["item_id"] == "item-2"
    assert set(consent_cards[0]["actions"]) == {"keep", "drop"}


def test_add_item_control_is_always_present():
    fixture = _load("simple_summary")

    payload = build_a2ui_payload(fixture["items"], fixture["pending_consent_items"])

    add_controls = [
        node for node in payload["components"] if node["type"] == "add_item"
    ]
    assert len(add_controls) == 1


def test_every_open_item_has_a_visibility_toggle():
    fixture = _load("simple_summary")

    payload = build_a2ui_payload(fixture["items"], fixture["pending_consent_items"])

    toggles = {
        node["item_id"]
        for node in payload["components"]
        if node["type"] == "visibility_toggle"
    }
    assert toggles == {"item-1", "item-2"}
