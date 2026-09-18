"""Unit tests for Gmail echo suppression in assemble_and_score.

Tests:
  - Gmail echo of an ingested GitHub WorkItem is suppressed
  - Gmail echo of a missing origin item is NOT suppressed (passes through)
  - Gmail echo of an ingested Slack Message is suppressed (with current_message_id exclusion)
  - Gmail echo of an ingested Jira WorkItem is suppressed
  - Non-echo Gmail message passes through normally
  - Work-item-vs-work-item is never suppressed (regression guard)
"""
import datetime
import uuid

import pytest

from app.core.clock import FrozenClock
from app.core.models import Message, WorkItem
from app.core.scope import OwnerScope
from app.salience.pulse.assemble import assemble_and_score, find_origin_item


# ─────────────────────────── helpers ──────────────────────────────────────────

CLOCK = FrozenClock(at=datetime.datetime(2026, 8, 28, 8, 0, tzinfo=datetime.UTC))


def _make_message(scope: OwnerScope, **kwargs) -> Message:
    """Insert a Message row via the scope session and return it."""
    msg_id = kwargs.get("external_id", str(uuid.uuid4()))
    defaults = dict(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        actor_reference_key=f"gmail:test+{msg_id}@example.com",
        source="gmail",
        external_id=msg_id,
        channel="inbox",
        sent_at=datetime.datetime(2026, 8, 28, 7, 0, tzinfo=datetime.UTC),
        body_ref="gmail://test",
        is_dm=False,
        action_requested=False,
        source_echo_of=None,
    )
    defaults.update(kwargs)
    row = Message(**defaults)
    scope.session.add(row)
    scope.session.commit()
    return row


def _make_work_item(scope: OwnerScope, source: str, external_id: str) -> WorkItem:
    row = WorkItem(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        actor_reference_key=f"{source}:{external_id}",
        source=source,
        external_id=external_id,
        title="Test work item",
        status="open",
        updated_at=datetime.datetime(2026, 8, 28, 6, 0, tzinfo=datetime.UTC),
    )
    scope.session.add(row)
    scope.session.commit()
    return row


# ─────────────────────────── find_origin_item ──────────────────────────────────


def test_find_origin_item_github_found(make_scope):
    scope = make_scope()
    _make_work_item(scope, "github", "org/repo#42")
    result = find_origin_item(scope, {"source": "github", "external_id": "org/repo#42"})
    assert result is not None
    assert result.external_id == "org/repo#42"


def test_find_origin_item_github_missing(make_scope):
    scope = make_scope()
    result = find_origin_item(scope, {"source": "github", "external_id": "org/repo#9999"})
    assert result is None


def test_find_origin_item_jira_found(make_scope):
    scope = make_scope()
    _make_work_item(scope, "jira", "PROJ-123")
    result = find_origin_item(scope, {"source": "jira", "external_id": "PROJ-123"})
    assert result is not None


def test_find_origin_item_jira_missing(make_scope):
    scope = make_scope()
    result = find_origin_item(scope, {"source": "jira", "external_id": "PROJ-999"})
    assert result is None


def test_find_origin_item_slack_found(make_scope):
    scope = make_scope()
    slack_msg = _make_message(
        scope,
        source="slack",
        external_id="1787864910.052589",
    )
    result = find_origin_item(scope, {"source": "slack", "external_id": "1787864910.052589"})
    assert result is not None
    assert result.external_id == "1787864910.052589"


def test_find_origin_item_slack_excludes_self(make_scope):
    """Ensures a Gmail echo row is not matched against itself (id exclusion)."""
    scope = make_scope()
    gmail_msg = _make_message(
        scope,
        source="slack",  # hypothetical: same external_id as a slack message that hasn't been ingested yet
        external_id="1111111111.000000",
    )
    result = find_origin_item(
        scope,
        {"source": "slack", "external_id": "1111111111.000000"},
        current_message_id=gmail_msg.id,
    )
    assert result is None


def test_find_origin_item_empty_echo_dict(make_scope):
    scope = make_scope()
    assert find_origin_item(scope, {}) is None
    assert find_origin_item(scope, {"source": "github"}) is None
    assert find_origin_item(scope, {"external_id": "PROJ-1"}) is None


# ─────────────────────────── assemble suppression ──────────────────────────────


def _empty_clients():
    """No live clients — assemble_and_score won't attempt to fetch live data."""
    return []


def test_echo_suppressed_when_github_origin_present(make_scope):
    """A Gmail echo of an already-ingested GitHub WorkItem must be filtered out of the shortlist."""
    scope = make_scope()
    # The origin WorkItem
    _make_work_item(scope, "github", "myorg/api#7")
    # A Gmail echo pointing to it
    echo_msg = _make_message(
        scope,
        source="gmail",
        external_id="msg-github-echo-1",
        source_echo_of={"source": "github", "external_id": "myorg/api#7"},
    )

    ctx = assemble_and_score(scope, CLOCK, _empty_clients())
    message_ids = {item.item_id for item in ctx.shortlist if item.item_type == "message"}
    # Echo must be absent from the shortlist (identified by its DB row .id)
    assert echo_msg.id not in message_ids


def test_echo_suppressed_when_jira_origin_present(make_scope):
    scope = make_scope()
    _make_work_item(scope, "jira", "MENT-214")
    echo_msg = _make_message(
        scope,
        source="gmail",
        external_id="msg-jira-echo-1",
        source_echo_of={"source": "jira", "external_id": "MENT-214"},
    )

    ctx = assemble_and_score(scope, CLOCK, _empty_clients())
    message_ids = {item.item_id for item in ctx.shortlist if item.item_type == "message"}
    assert echo_msg.id not in message_ids


def test_echo_not_suppressed_when_origin_missing(make_scope):
    """If the origin item isn't in DB, the Gmail echo should survive."""
    scope = make_scope()
    orphan_msg = _make_message(
        scope,
        source="gmail",
        external_id="msg-github-echo-noorigin",
        source_echo_of={"source": "github", "external_id": "missing/repo#404"},
    )

    ctx = assemble_and_score(scope, CLOCK, _empty_clients())
    message_ids = {item.item_id for item in ctx.shortlist if item.item_type == "message"}
    # Origin absent → echo survives (checked by row .id)
    assert orphan_msg.id in message_ids


def test_non_echo_gmail_message_passes_through(make_scope):
    """A Gmail message with no source_echo_of must always appear in the shortlist."""
    scope = make_scope()
    plain_msg = _make_message(
        scope,
        source="gmail",
        external_id="msg-plain-gmail",
        source_echo_of=None,
    )

    ctx = assemble_and_score(scope, CLOCK, _empty_clients())
    message_ids = {item.item_id for item in ctx.shortlist if item.item_type == "message"}
    # No source_echo_of → always in shortlist (checked by row .id)
    assert plain_msg.id in message_ids


def test_work_item_vs_work_item_not_suppressed(make_scope):
    """Work items from different sources covering the same task must not suppress each other.
    This is the regression guard: we never suppress WorkItems, only Gmail echo Messages."""
    scope = make_scope()
    _make_work_item(scope, "github", "myorg/api#7")
    _make_work_item(scope, "jira", "PROJ-7")

    ctx = assemble_and_score(scope, CLOCK, _empty_clients())
    work_item_ids = {item.item_id for item in ctx.shortlist if item.item_type == "work_item"}
    # Both work items must be present — no cross-source work item suppression
    assert len([i for i in ctx.shortlist if i.item_type == "work_item"]) == 2


def test_multiple_echoes_independently_checked(make_scope):
    """Two different Gmail echoes: one origin present (suppressed), one absent (kept)."""
    scope = make_scope()
    _make_work_item(scope, "github", "myorg/api#7")
    # Echo 1 — origin present → should be suppressed
    echo_present = _make_message(
        scope,
        source="gmail",
        external_id="echo-present",
        source_echo_of={"source": "github", "external_id": "myorg/api#7"},
    )
    # Echo 2 — origin absent → should pass through
    echo_absent = _make_message(
        scope,
        source="gmail",
        external_id="echo-absent",
        source_echo_of={"source": "github", "external_id": "myorg/api#999"},
    )

    ctx = assemble_and_score(scope, CLOCK, _empty_clients())
    message_ids = {item.item_id for item in ctx.shortlist if item.item_type == "message"}
    # Row .id is the item_id in the scored shortlist
    assert echo_present.id not in message_ids   # suppressed — origin is present
    assert echo_absent.id in message_ids         # passes through — origin is absent
