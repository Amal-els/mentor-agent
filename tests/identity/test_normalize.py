import pytest

from app.identity.normalize import (
    build_reference_key,
    normalize_email,
    normalize_handle,
)


def test_normalize_handle_casefolds_and_trims():
    assert normalize_handle("  Sbenali  ") == "sbenali"


def test_normalize_handle_does_not_strip_digit_suffix():
    # sbenali and sbenali2 are frequently different people — the key must never
    # silently conflate them. Digit-suffix stripping belongs in match_handle_heuristic.
    assert normalize_handle("sbenali2") == "sbenali2"
    assert normalize_handle("sbenali2") != normalize_handle("sbenali")


def test_normalize_handle_unicode_nfkc_folds():
    assert normalize_handle("Şenol") == normalize_handle("şenol")


def test_normalize_email_lowercases_and_strips_plus_tag():
    assert normalize_email("Sarah.BenYoussef+linear@ACME.com") == (
        "sarah.benyoussef@acme.com"
    )


def test_normalize_email_does_not_fold_alias_domains():
    # acme.io -> acme.com folding is matcher evidence (tier 2), not a key transform
    assert normalize_email("s.benyoussef@acme.io") == "s.benyoussef@acme.io"


def test_build_reference_key_prefers_external_id():
    key = build_reference_key(source="slack", external_id="U123", handle="sbenali")
    assert key == "slack:U123"


def test_build_reference_key_falls_back_to_handle():
    key = build_reference_key(source="slack", external_id=None, handle="Sbenali")
    assert key == "slack:handle:sbenali"


def test_build_reference_key_falls_back_to_display_name():
    # A calendar attendee identified only by a display name (no external_id,
    # no handle) — the design spec's own worked example (a transcript speaker
    # with no ID, just a name). Reuses the "handle:" prefix and normalize_handle,
    # since a display name is free-text same as a handle.
    key = build_reference_key(
        source="calendar",
        external_id=None,
        handle=None,
        display_name="Sarah Ben Youssef",
    )
    assert key == "calendar:handle:sarah ben youssef"


def test_build_reference_key_requires_something():
    with pytest.raises(ValueError):
        build_reference_key(source="slack", external_id=None, handle=None)


def test_build_reference_key_rejects_unknown_source():
    with pytest.raises(ValueError):
        build_reference_key(source="carrier_pigeon", external_id="1", handle=None)


def test_safe_log_key_passes_through_external_id_keys():
    from app.identity.normalize import safe_log_key

    assert safe_log_key("slack:U123") == "slack:U123"


def test_safe_log_key_hashes_handle_portion():
    from app.identity.normalize import safe_log_key

    logged = safe_log_key("slack:handle:sbenali")
    assert logged.startswith("slack:handle:")
    assert "sbenali" not in logged
    assert len(logged.split(":")[-1]) == 8
