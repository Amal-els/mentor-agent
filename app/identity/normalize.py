"""Conservative-only key construction (design spec §4.4). The key normalizes,
the matchers guess: nothing here may conflate two different people. Digit-suffix
stripping, alias-domain folding, and fuzzy comparison belong in matchers.py."""

import hashlib
import unicodedata

KNOWN_SOURCES = frozenset({"calendar", "slack", "linear", "jira"})


def normalize_handle(raw: str) -> str:
    return unicodedata.normalize("NFKC", raw).strip().casefold()


def normalize_email(raw: str) -> str:
    email = unicodedata.normalize("NFKC", raw).strip().casefold()
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    return f"{local}@{domain}"


def build_reference_key(
    source: str,
    external_id: str | None,
    handle: str | None,
    display_name: str | None = None,
) -> str:
    if source not in KNOWN_SOURCES:
        raise ValueError(f"unknown source: {source!r}")
    if external_id:
        return f"{source}:{external_id}"
    if handle:
        return f"{source}:handle:{normalize_handle(handle)}"
    if display_name:
        return f"{source}:handle:{normalize_handle(display_name)}"
    raise ValueError(
        "build_reference_key requires external_id, handle, or display_name"
    )


def safe_log_key(reference_key: str) -> str:
    """The logging rule from design spec §7: an external_id-keyed reference_key
    is an opaque provider ID and safe to log as-is. A handle-keyed reference_key
    has its handle portion hashed before it may reach any log line."""
    prefix, _, rest = reference_key.partition(":")
    if rest.startswith("handle:"):
        handle_part = rest[len("handle:") :]
        digest = hashlib.sha256(handle_part.encode("utf-8")).hexdigest()[:8]
        return f"{prefix}:handle:{digest}"
    return reference_key
