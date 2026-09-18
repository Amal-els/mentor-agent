import datetime
import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.db import Base, get_engine, get_session_factory
from app.core.models import Event, GraphEdge, GraphNode, Message, User, WorkItem
from app.core.scope import OwnerScope
from app.graph.resolver import (
    CONFIRMATION_THRESHOLD,
    extract_reference_candidates,
    project_source_record,
)


@pytest.fixture
def pg_session() -> Session:
    engine = get_engine(get_settings().database_url)
    Base.metadata.create_all(engine)
    session: Session = get_session_factory(engine)()
    yield session
    session.close()


@pytest.fixture
def scope(pg_session) -> OwnerScope:
    uid = str(uuid.uuid4())
    pg_session.add(User(id=uid, created_at=datetime.datetime.now(datetime.UTC)))
    pg_session.commit()
    return OwnerScope(owner_user_id=uid, session=pg_session)


def test_explicit_github_url_is_confirmed():
    candidates = extract_reference_candidates(
        "Please review https://github.com/acme/payments/pull/42"
    )

    assert len(candidates) == 1
    assert candidates[0].canonical_key == "github:acme/payments#42"
    assert candidates[0].confidence >= CONFIRMATION_THRESHOLD
    assert candidates[0].status == "confirmed"


def test_issue_key_is_deduplicated_with_url():
    candidates = extract_reference_candidates(
        "acme/payments#42 https://github.com/acme/payments/issues/42"
    )

    assert len(candidates) == 1
    assert candidates[0].method == "explicit_github_url"


def test_missing_reference_does_not_guess():
    assert extract_reference_candidates("The deployment is blocked") == []


def test_unresolved_attendee_is_still_projected_as_identity_node(scope):
    """An attendee with no internal Person match (the same case
    app.sub_agents.dossier.sub_agents.gather.agent labels "external" for
    the dossier) must still show up in the graph — as an identity node
    linked by a review-candidate edge — rather than being silently
    dropped just because identity resolution couldn't attribute them."""
    clock = FrozenClock(at=datetime.datetime(2026, 8, 26, tzinfo=datetime.UTC))
    event = Event(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        actor_reference_key="calendar:evt-1",
        source="calendar",
        external_id="evt-1",
        title="Vendor sync",
        attendees=[
            {
                "source": "calendar",
                "email": "someone@othercompany.com",
                "display_name": "Someone Else",
            }
        ],
    )
    scope.add(event)
    scope.commit()

    project_source_record(scope, event, "event", clock)

    identity_nodes = (
        scope.session.query(GraphNode)
        .filter_by(owner_user_id=scope.owner_user_id, node_type="identity")
        .all()
    )
    assert len(identity_nodes) == 1
    assert identity_nodes[0].label == "Someone Else"

    edges = (
        scope.session.query(GraphEdge)
        .filter_by(owner_user_id=scope.owner_user_id, edge_type="attended")
        .all()
    )
    assert len(edges) == 1
    assert edges[0].from_node_id == identity_nodes[0].id
    assert edges[0].status == "candidate"


def _make_message(scope, **overrides) -> Message:
    defaults = dict(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        actor_reference_key="slack:U12345",
        source="slack",
        external_id="1699999999.0001",
    )
    defaults.update(overrides)
    message = Message(**defaults)
    scope.add(message)
    scope.commit()
    return message


def test_message_node_label_uses_sender_and_channel_when_both_resolved(scope):
    """REAL CHANGE (requested: "resolve user ids and channel ids in
    slack to the username and channel name") — a message node's label
    used to always be its raw canonical_key (e.g. a bare Slack
    timestamp), meaningless at a glance."""
    clock = FrozenClock(at=datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC))
    message = _make_message(
        scope, channel_name="general", sender_display_name="Jane Doe"
    )

    node = project_source_record(scope, message, "message", clock)

    assert node.label == "Jane Doe in #general"
    assert node.properties["channel_name"] == "general"
    assert node.properties["sender_display_name"] == "Jane Doe"


def test_message_node_label_falls_back_to_sender_only(scope):
    clock = FrozenClock(at=datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC))
    message = _make_message(scope, sender_display_name="Jane Doe")

    node = project_source_record(scope, message, "message", clock)

    assert node.label == "Message from Jane Doe"


def test_message_node_label_falls_back_to_channel_only(scope):
    clock = FrozenClock(at=datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC))
    message = _make_message(scope, channel_name="general")

    node = project_source_record(scope, message, "message", clock)

    assert node.label == "Message in #general"


def test_message_node_label_falls_back_to_raw_key_when_nothing_resolved(scope):
    """The Gmail case (and any Slack message where name resolution
    genuinely failed): no regression from the prior always-raw-key
    behavior."""
    clock = FrozenClock(at=datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC))
    message = _make_message(
        scope, source="gmail", external_id="18abc", actor_reference_key="gmail:x"
    )

    node = project_source_record(scope, message, "message", clock)

    assert node.label == "gmail:18abc"


def test_a_work_item_referencing_its_own_github_key_does_not_self_loop(scope):
    """REAL BUG FOUND AND FIXED (requested: "work items aren't linked")
    — a WorkItem whose own title contains its own GitHub reference (a PR
    description linking back to itself) used to produce a stored,
    meaningless self-loop edge — the ONLY edge that WorkItem had,
    making it look linked when it effectively wasn't."""
    clock = FrozenClock(at=datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC))
    work_item = WorkItem(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        actor_reference_key="github:someone",
        source="github",
        external_id="acme/repo#1",
        title="Fix acme/repo#1 again",
    )
    scope.add(work_item)
    scope.commit()

    project_source_record(scope, work_item, "work_item", clock)

    self_edges = (
        scope.session.query(GraphEdge)
        .filter_by(owner_user_id=scope.owner_user_id, edge_type="references")
        .all()
    )
    assert self_edges == []


def test_work_item_with_no_resolved_actor_still_gets_an_identity_node(scope):
    """REAL BUG FOUND AND FIXED: with no internal Person match,
    resolved_person_id stays None and a WorkItem got ZERO edges at all —
    unlike an event's own unresolved attendee, which still becomes a
    visible "identity" node. Now mirrors that same fallback."""
    clock = FrozenClock(at=datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC))
    work_item = WorkItem(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        actor_reference_key="github:external-contributor",
        source="github",
        external_id="acme/repo#2",
        title="Unrelated title",
        resolved_person_id=None,
    )
    scope.add(work_item)
    scope.commit()

    project_source_record(scope, work_item, "work_item", clock)

    identity_nodes = (
        scope.session.query(GraphNode)
        .filter_by(
            owner_user_id=scope.owner_user_id,
            node_type="identity",
            canonical_key="identity:github:external-contributor",
        )
        .all()
    )
    assert len(identity_nodes) == 1
    assert identity_nodes[0].label == "github:external-contributor"

    edges = (
        scope.session.query(GraphEdge)
        .filter_by(owner_user_id=scope.owner_user_id, edge_type="authored_or_organizes")
        .all()
    )
    assert len(edges) == 1
    assert edges[0].from_node_id == identity_nodes[0].id
    assert edges[0].status == "candidate"


def test_work_item_with_a_resolved_actor_gets_a_person_node_not_identity(scope):
    """The fallback above must only fire when resolution genuinely
    failed — a resolved actor should still get the real "person" node
    (existing behavior), not a redundant unresolved-identity node too."""
    clock = FrozenClock(at=datetime.datetime(2026, 8, 27, tzinfo=datetime.UTC))
    person_id = str(uuid.uuid4())
    scope.add(User(id=person_id, created_at=datetime.datetime.now(datetime.UTC)))
    scope.commit()
    work_item = WorkItem(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        actor_reference_key="github:resolved-person",
        source="github",
        external_id="acme/repo#3",
        title="Unrelated title",
        resolved_person_id=person_id,
    )
    scope.add(work_item)
    scope.commit()

    project_source_record(scope, work_item, "work_item", clock)

    identity_nodes = (
        scope.session.query(GraphNode)
        .filter_by(owner_user_id=scope.owner_user_id, node_type="identity")
        .all()
    )
    assert identity_nodes == []
    person_nodes = (
        scope.session.query(GraphNode)
        .filter_by(owner_user_id=scope.owner_user_id, node_type="person")
        .all()
    )
    assert len(person_nodes) == 1
