import re
import uuid
from dataclasses import dataclass
from typing import Any
from app.core.clock import Clock
from app.core.models import Event, GoogleCredential, GraphEdge, GraphNode, Message, User, WorkItem
from app.core.scope import OwnerScope
from app.identity.models import Identity, Person
from app.identity.resolve import resolve
from app.identity.types import RawReference

CONFIRMATION_THRESHOLD = 0.90
_URL_RE = re.compile(r"https?://[^\s)>]+")
_ISSUE_KEY_RE = re.compile(r"(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)")

@dataclass(frozen=True)
class ResolutionCandidate:
    canonical_key: str
    node_type: str
    confidence: float
    method: str
    evidence: dict

    @property
    def status(self) -> str:
        return "confirmed" if self.confidence >= CONFIRMATION_THRESHOLD else "candidate"

def extract_reference_candidates(text: str | None) -> list[ResolutionCandidate]:
    if not text:
        return []
    candidates: list[ResolutionCandidate] = []
    for url in _URL_RE.findall(text):
        url = url.rstrip(".,")
        github = re.fullmatch(
            r"https?://github\.com/(?P<repo>[^/]+/[^/]+)/(issues|pull)/(?P<number>\d+)",
            url,
        )
        if github:
            candidates.append(
                ResolutionCandidate(
                    canonical_key=f"github:{github['repo']}#{github['number']}",
                    node_type="work_item",
                    confidence=1.0,
                    method="explicit_github_url",
                    evidence={"url": url},
                )
            )
    for match in _ISSUE_KEY_RE.finditer(text):
        candidates.append(
            ResolutionCandidate(
                canonical_key=f"github:{match['repo']}#{match['number']}",
                node_type="work_item",
                confidence=0.95,
                method="explicit_issue_key",
                evidence={"text": match.group(0)},
            )
        )
    unique: dict[str, ResolutionCandidate] = {}
    for candidate in candidates:
        previous = unique.get(candidate.canonical_key)
        if previous is None or candidate.confidence > previous.confidence:
            unique[candidate.canonical_key] = candidate
    return list(unique.values())

def project_node(
    scope: OwnerScope,
    node_type: str,
    canonical_key: str,
    label: str,
    clock: Clock,
    properties: dict | None = None,
) -> GraphNode:
    node = scope.session.query(GraphNode).filter_by(
        owner_user_id=scope.owner_user_id,
        node_type=node_type,
        canonical_key=canonical_key,
    ).one_or_none()
    now = clock.now()
    if node is None:
        node = GraphNode(
            id=str(uuid.uuid4()),
            owner_user_id=scope.owner_user_id,
            node_type=node_type,
            canonical_key=canonical_key,
            label=label,
            properties=properties or {},
            created_at=now,
            updated_at=now,
        )
        scope.add(node)
    else:
        node.label = label
        node.properties = properties or node.properties
        node.updated_at = now
    scope.commit()
    return node

def link_nodes(
    scope: OwnerScope,
    from_node: GraphNode,
    to_node: GraphNode,
    edge_type: str,
    candidate: ResolutionCandidate,
    clock: Clock,
) -> GraphEdge:
    edge = scope.session.query(GraphEdge).filter_by(
        owner_user_id=scope.owner_user_id,
        from_node_id=from_node.id,
        to_node_id=to_node.id,
        edge_type=edge_type,
    ).one_or_none()
    if edge is None:
        edge = GraphEdge(
            id=str(uuid.uuid4()),
            owner_user_id=scope.owner_user_id,
            from_node_id=from_node.id,
            to_node_id=to_node.id,
            edge_type=edge_type,
            confidence=candidate.confidence,
            status=candidate.status,
            method=candidate.method,
            evidence=candidate.evidence,
            created_at=clock.now(),
        )
        scope.add(edge)
    elif candidate.confidence > edge.confidence:
        edge.confidence = candidate.confidence
        edge.status = candidate.status
        edge.method = candidate.method
        edge.evidence = candidate.evidence
    scope.commit()
    return edge

def _record_label(record: Any, canonical_key: str) -> str:
    title = getattr(record, "title", None)
    if title:
        return title
    channel_name = getattr(record, "channel_name", None)
    sender_display_name = getattr(record, "sender_display_name", None)
    if sender_display_name and channel_name:
        return f"{sender_display_name} in #{channel_name}"
    if sender_display_name:
        return f"Message from {sender_display_name}"
    if channel_name:
        return f"Message in #{channel_name}"
    return canonical_key

def project_source_record(
    scope: OwnerScope, record: Any, node_type: str, clock: Clock
) -> GraphNode:
    source = record.source or "unknown"
    canonical_key = f"{source}:{record.external_id or record.id}"
    node = project_node(
        scope,
        node_type,
        canonical_key,
        _record_label(record, canonical_key),
        clock,
        {
            "source": source,
            "external_id": record.external_id,
            "url": record.url,
            "channel_name": getattr(record, "channel_name", None),
            "sender_display_name": getattr(record, "sender_display_name", None),
        },
    )
    actor_key = getattr(record, "actor_reference_key", None)
    target_person_id = getattr(record, "resolved_person_id", None)
    if not target_person_id and actor_key:
        source_prefix, _, rest = actor_key.partition(":")
        owner_user = scope.session.get(User, scope.owner_user_id)
        if owner_user is not None:
            google_cred = scope.session.get(GoogleCredential, scope.owner_user_id)
            owner_keys = {
                owner_user.id,
                owner_user.slack_user_id,
                owner_user.notion_owner_email,
                owner_user.notion_person_id,
                google_cred.google_email if google_cred else None,
            }
            owner_keys.discard(None)
            if rest in owner_keys or actor_key in owner_keys:
                target_person_id = scope.owner_user_id

        if not target_person_id and rest:
            person_by_email = scope.session.execute(
                scope.query(Person).where(
                    (Person.primary_email == rest) | (Person.id == rest)
                )
            ).scalars().first()
            if person_by_email is not None:
                target_person_id = person_by_email.id

        if not target_person_id:
            from app.identity.models import Identity as _Identity
            identity_row = scope.session.execute(
                scope.query(_Identity).where(_Identity.reference_key == actor_key)
            ).scalar_one_or_none()
            if identity_row is not None:
                target_person_id = identity_row.person_id
    if target_person_id:
        person_row = scope.session.get(Person, target_person_id)
        user_row = scope.session.get(User, target_person_id)
        # REAL BUG FOUND AND FIXED (confirmed live): when person_row is
        # None but a User row exists, this fell straight to
        # user_row.notion_display_name with no further fallback — for a
        # real, roster-linked user with no display name set (both
        # notion_display_name and notion_owner_email None, exactly
        # backfill_owner_graph's own "owner_label" fallback chain has to
        # handle), that's None, which violates graph_nodes.label's
        # NOT NULL constraint. Same fallback order as
        # backfill_owner_graph's owner_label, plus the final
        # str(target_person_id) this branch already had for the
        # neither-row-exists case.
        person_label = (
            person_row.canonical_name
            if person_row is not None
            else user_row.notion_display_name or user_row.notion_owner_email
            if user_row is not None
            else None
        ) or str(target_person_id)
        person_node = project_node(
            scope,
            "person",
            f"person:{target_person_id}",
            person_label,
            clock,
            {
                "person_id": target_person_id,
                "notion_person_id": user_row.notion_person_id if user_row else None,
                "notion_email": user_row.notion_owner_email if user_row else (person_row.primary_email if person_row else None),
                "roster_source": person_row.roster_source if person_row else "user",
            },
        )
        link_nodes(
            scope,
            person_node,
            node,
            "authored_or_organizes",
            ResolutionCandidate(
                canonical_key=person_node.canonical_key,
                node_type="person",
                confidence=1.0,
                method="identity_graph",
                evidence={"person_id": target_person_id},
            ),
            clock,
        )
        if actor_key:
            source_prefix, _, rest = actor_key.partition(":")
            id_label = f"{source_prefix.capitalize()} ({rest})" if rest else actor_key
            identity_node = project_node(
                scope,
                "identity",
                f"identity:{actor_key}",
                id_label,
                clock,
                {"source": source_prefix, "actor_reference_key": actor_key},
            )
            link_nodes(
                scope,
                person_node,
                identity_node,
                "has_identity",
                ResolutionCandidate(
                    canonical_key=actor_key,
                    node_type="identity",
                    confidence=0.95,
                    method="actor_reference",
                    evidence={"actor_reference_key": actor_key},
                ),
                clock,
            )
    elif actor_key:
        source_prefix, _, rest = actor_key.partition(":")
        id_label = f"{source_prefix.capitalize()} ({rest})" if rest else actor_key
        identity_node = project_node(
            scope,
            "identity",
            f"identity:{actor_key}",
            id_label,
            clock,
            {"source": source_prefix, "actor_reference_key": actor_key},
        )
        link_nodes(
            scope,
            identity_node,
            node,
            "authored_or_organizes",
            ResolutionCandidate(
                canonical_key=identity_node.canonical_key,
                node_type="identity",
                confidence=0.0,
                method="unresolved_actor",
                evidence={"actor_reference_key": actor_key},
            ),
            clock,
        )
    if node_type == "event":
        for attendee in getattr(record, "attendees", []) or []:
            reference = RawReference(
                source=attendee.get("source", "calendar"),
                external_id=attendee.get("external_id"),
                handle=attendee.get("handle"),
                email=attendee.get("email"),
                display_name=attendee.get("display_name"),
            )
            resolution = resolve(scope, reference, clock)
            attendee_id = getattr(resolution, "person_id", None)
            if attendee_id is not None:
                attendee_record = scope.session.get(Person, attendee_id)
                attendee_node = project_node(
                    scope,
                    "person",
                    f"person:{attendee_id}",
                    attendee_record.canonical_name if attendee_record else str(attendee_id),
                    clock,
                )
                confidence = (
                    1.0 if getattr(resolution, "confidence", "") == "verified" else 0.95
                )
                method = "identity_resolution"
            else:
                reference_key = getattr(resolution, "reference_key", None)
                if reference_key is None:
                    continue
                attendee_node = project_node(
                    scope,
                    "identity",
                    f"identity:{reference_key}",
                    attendee.get("display_name")
                    or attendee.get("email")
                    or attendee.get("handle")
                    or reference_key,
                    clock,
                    {
                        "source": attendee.get("source", "calendar"),
                        "email": attendee.get("email"),
                        "handle": attendee.get("handle"),
                    },
                )
                confidence = 0.0
                method = "unresolved_attendee"
            link_nodes(
                scope,
                attendee_node,
                node,
                "attended",
                ResolutionCandidate(
                    canonical_key=attendee_node.canonical_key,
                    node_type=attendee_node.node_type,
                    confidence=confidence,
                    method=method,
                    evidence={"reference": attendee},
                ),
                clock,
            )

    text = " ".join(
        value for value in (getattr(record, "title", None), getattr(record, "url", None)) if value
    )
    for candidate in extract_reference_candidates(text):
        if candidate.canonical_key == canonical_key:
            continue
        target = project_node(
            scope, candidate.node_type, candidate.canonical_key,
            candidate.canonical_key, clock,
        )
        link_nodes(scope, node, target, "references", candidate, clock)
    return node


def review_edge(
    scope: OwnerScope,
    edge_id: str,
    reviewer_id: str,
    decision: str,
    clock: Clock,
) -> GraphEdge:
    if decision not in {"confirmed", "rejected"}:
        raise ValueError("decision must be 'confirmed' or 'rejected'")
    edge = scope.session.query(GraphEdge).filter_by(
        id=edge_id, owner_user_id=scope.owner_user_id
    ).one_or_none()
    if edge is None:
        raise ValueError("graph edge not found")
    edge.status = decision
    edge.reviewed_by = reviewer_id
    edge.reviewed_at = clock.now()
    scope.commit()
    return edge


def backfill_owner_graph(scope: OwnerScope, clock: Clock) -> int:
    projected = 0
    from sqlalchemy import delete, select
    scope.session.execute(
        delete(GraphEdge).where(
            GraphEdge.owner_user_id == scope.owner_user_id,
            GraphEdge.reviewed_by.is_(None),  # preserve user manual review decisions
        )
    )
    # Any edge surviving the delete above (a reviewed one) still points at
    # its endpoint nodes — wiping every node unconditionally would violate
    # that edge's FK. REAL BUG FOUND AND FIXED (confirmed live): a single
    # reviewed edge made the whole backfill_owner_graph 500 on every poll,
    # which made /webhooks/graph itself 500 and the UI silently fall back
    # to its baked-in sample graph — looking exactly like "stale data" with
    # no visible error. Excluding referenced nodes is safe, not just a
    # workaround: project_node upserts by (owner_user_id, node_type,
    # canonical_key), not by id, so a kept node is transparently refreshed
    # in place if its source record still projects this pass.
    referenced_node_ids = scope.session.execute(
        select(GraphEdge.from_node_id).where(GraphEdge.owner_user_id == scope.owner_user_id)
        .union(
            select(GraphEdge.to_node_id).where(GraphEdge.owner_user_id == scope.owner_user_id)
        )
    ).scalars().all()
    scope.session.execute(
        delete(GraphNode).where(
            GraphNode.owner_user_id == scope.owner_user_id,
            GraphNode.id.notin_(referenced_node_ids) if referenced_node_ids else True,
        )
    )
    scope.commit()
    owner_user = scope.session.get(User, scope.owner_user_id)
    if owner_user is not None:
        owner_label = (
            owner_user.notion_display_name
            or owner_user.notion_owner_email
            or scope.owner_user_id
        )
        owner_person_node = project_node(
            scope,
            "person",
            f"person:{scope.owner_user_id}",
            owner_label,
            clock,
            {
                "person_id": scope.owner_user_id,
                "notion_email": owner_user.notion_owner_email,
                "notion_person_id": owner_user.notion_person_id,
                "slack_user_id": owner_user.slack_user_id,
                "is_owner": True,
            },
        )
        owner_identities: list[tuple[str, str, str, str, dict]] = []
        if owner_user.slack_user_id:
            ref_key = f"slack:{owner_user.slack_user_id}"
            owner_identities.append((
                f"identity:{ref_key}",
                ref_key,
                f"Slack ({owner_user.slack_user_id})",
                "slack",
                {"slack_user_id": owner_user.slack_user_id},
            ))
        if owner_user.notion_owner_email:
            ref_key = f"notion:{owner_user.notion_owner_email}"
            owner_identities.append((
                f"identity:{ref_key}",
                ref_key,
                f"Notion ({owner_user.notion_owner_email})",
                "notion",
                {"email": owner_user.notion_owner_email,
                 "notion_person_id": owner_user.notion_person_id},
            ))
        if owner_user.notion_person_id and not owner_user.notion_owner_email:
            ref_key = f"notion:{owner_user.notion_person_id}"
            owner_identities.append((
                f"identity:{ref_key}",
                ref_key,
                f"Notion ({owner_user.notion_person_id})",
                "notion",
                {"notion_person_id": owner_user.notion_person_id},
            ))
        google_cred = scope.session.get(GoogleCredential, scope.owner_user_id)
        if google_cred is not None and google_cred.google_email:
            ref_key = f"google:{google_cred.google_email}"
            owner_identities.append((
                f"identity:{ref_key}",
                ref_key,
                f"Google ({google_cred.google_email})",
                "google",
                {"email": google_cred.google_email},
            ))
        for canonical_key, ref_key, label, source, props in owner_identities:
            id_node = project_node(
                scope,
                "identity",
                canonical_key,
                label,
                clock,
                {"source": source, **props},
            )
            link_nodes(
                scope,
                owner_person_node,
                id_node,
                "has_identity",
                ResolutionCandidate(
                    canonical_key=ref_key,
                    node_type="identity",
                    confidence=1.0,
                    method="user_row",
                    evidence=props,
                ),
                clock,
            )
    for person in scope.session.execute(scope.query(Person)).scalars().all():
        project_node(
            scope,
            "person",
            f"person:{person.id}",
            person.canonical_name,
            clock,
            {
                "person_id": person.id,
                "notion_email": person.primary_email,
                "roster_source": person.roster_source,
            },
        )
    ref_key_to_person: dict[str, str] = {}
    for identity in scope.session.execute(scope.query(Identity)).scalars().all():
        ref_key_to_person[identity.reference_key] = identity.person_id
        person_row = scope.session.get(Person, identity.person_id)
        person_label = (
            person_row.canonical_name if person_row is not None else f"person:{identity.person_id}"
        )
        person_node = project_node(
            scope,
            "person",
            f"person:{identity.person_id}",
            person_label,
            clock,
            {
                "person_id": identity.person_id,
                "notion_email": person_row.primary_email if person_row else None,
                "roster_source": person_row.roster_source if person_row else None,
            },
        )
        ref_key = identity.reference_key
        canonical_key = f"identity:{ref_key}" if not ref_key.startswith("identity:") else ref_key
        identity_node = project_node(
            scope,
            "identity",
            canonical_key,
            identity.display_name or identity.handle or identity.email or ref_key,
            clock,
            {"source": identity.source, "external_id": identity.external_id, "email": identity.email},
        )
        link_nodes(
            scope,
            person_node,
            identity_node,
            "has_identity",
            ResolutionCandidate(
                canonical_key=ref_key,
                node_type="identity",
                confidence=1.0 if identity.confidence == "verified" else 0.95,
                method=identity.verified_by,
                evidence=identity.provenance or {},
            ),
            clock,
        )

    for model, node_type in (
        (Event, "event"),
        (Message, "message"),
        (WorkItem, "work_item"),
    ):
        rows = scope.session.execute(scope.query(model)).scalars().all()
        for row in rows:
            if row.resolved_person_id is None:
                actor_key = getattr(row, "actor_reference_key", None)
                if actor_key and actor_key in ref_key_to_person:
                    row.resolved_person_id = ref_key_to_person[actor_key]
                    scope.commit()
            project_source_record(scope, row, node_type, clock)
            projected += 1
    return projected