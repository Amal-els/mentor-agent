"""Owner-scoped graph inspection and human review endpoints."""
import datetime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import GraphEdge, GraphNode
from app.core.scope import OwnerScope
from app.graph.resolver import backfill_owner_graph, review_edge
from app.triggers.agenda.webhook_auth import _acting_user_secret_is_valid, _token_is_valid

router = APIRouter()

def _open_session():
    return get_session_factory(get_engine(get_settings().database_url))()

@router.get("/webhooks/graph")
async def graph_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})
    owner_user_id = request.query_params.get("owner_user_id")
    if owner_user_id is None:
        return JSONResponse(status_code=400, content={"status": "owner_user_id required"})
    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, owner_user_id, request):
            return JSONResponse(status_code=401, content={"status": "invalid acting user secret"})
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        has_any_node = (
            session.query(GraphNode.id).filter_by(owner_user_id=owner_user_id).first()
            is not None
        )
        force_refresh = (
            request.query_params.get("refresh") == "true"
            or request.query_params.get("rebuild") == "true"
        )
        needs_backfill = force_refresh or not has_any_node
        if not needs_backfill:
            from app.core.models import GoogleCredential as _GCred, User as _User
            from app.identity.models import Identity as _Identity
            unlinked_node = (
                session.query(GraphNode.id)
                .filter(
                    GraphNode.owner_user_id == owner_user_id,
                    GraphNode.node_type.in_(["event", "message", "work_item"]),
                    ~session.query(GraphEdge.id)
                    .filter(
                        (GraphEdge.from_node_id == GraphNode.id)
                        | (GraphEdge.to_node_id == GraphNode.id)
                    )
                    .exists(),
                )
                .first()
            )
            if unlinked_node is not None:
                needs_backfill = True
            if not needs_backfill:
                owner_user = session.get(_User, owner_user_id)
                if owner_user is not None:
                    creds_to_check: list[str] = []
                    if owner_user.slack_user_id:
                        creds_to_check.append(f"slack:{owner_user.slack_user_id}")
                    if owner_user.notion_owner_email:
                        creds_to_check.append(f"notion:{owner_user.notion_owner_email}")
                    google_cred = session.get(_GCred, owner_user_id)
                    if google_cred is not None and google_cred.google_email:
                        creds_to_check.append(f"google:{google_cred.google_email}")
                    for cred_key in creds_to_check:
                        existing = (
                            session.query(GraphNode.id)
                            .filter_by(
                                owner_user_id=owner_user_id,
                                node_type="identity",
                                canonical_key=cred_key,
                            )
                            .one_or_none()
                        )
                        if existing is None:
                            needs_backfill = True
                            break

        if needs_backfill:
            backfill_owner_graph(scope, SystemClock())
        nodes = (
            session.query(GraphNode)
            .filter_by(owner_user_id=owner_user_id)
            .order_by(GraphNode.id)
            .all()
        )
        edges = (
            session.query(GraphEdge)
            .filter_by(owner_user_id=owner_user_id)
            .order_by(GraphEdge.id)
            .all()
        )
        node_payload = [
            {
                "id": node.id,
                "type": node.node_type,
                "key": node.canonical_key,
                "label": node.label,
                "properties": node.properties,
            }
            for node in nodes
        ]
        edge_payload = [
            {
                "id": edge.id,
                "from": edge.from_node_id,
                "to": edge.to_node_id,
                "type": edge.edge_type,
                "confidence": edge.confidence,
                "status": edge.status,
                "method": edge.method,
                "evidence": edge.evidence,
            }
            for edge in edges
        ]
        if request.query_params.get("format") == "dot":
            lines = ["digraph mentor_graph {"]
            for node in node_payload:
                label = node["label"].replace('"', "'")
                lines.append(f'  "{node["id"]}" [label="{label}"];')
            for edge in edge_payload:
                label = f'{edge["type"]} ({edge["status"]}, {edge["confidence"]:.2f})'
                lines.append(
                    f'  "{edge["from"]}" -> "{edge["to"]}" [label="{label}"];'
                )
            lines.append("}")
            return JSONResponse(content={"format": "dot", "graph": "\n".join(lines)})
        return JSONResponse(content={"nodes": node_payload, "edges": edge_payload})
    finally:
        session.close()


@router.post("/webhooks/graph/review")
async def graph_review(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})
    body = await request.json()
    owner_user_id = body.get("owner_user_id")
    if not owner_user_id:
        return JSONResponse(status_code=400, content={"status": "owner_user_id required"})
    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, owner_user_id, request):
            return JSONResponse(status_code=401, content={"status": "invalid acting user secret"})
        edge = review_edge(
            OwnerScope(owner_user_id=owner_user_id, session=session),
            body.get("edge_id", ""),
            body.get("reviewer_id", owner_user_id),
            body.get("decision", ""),
            SystemClock(),
        )
        return JSONResponse(
            content={
                "id": edge.id,
                "status": edge.status,
                "reviewed_by": edge.reviewed_by,
                "reviewed_at": edge.reviewed_at.isoformat()
                if isinstance(edge.reviewed_at, datetime.datetime)
                else None,
            }
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"status": "rejected", "reason": str(exc)})
    finally:
        session.close()
