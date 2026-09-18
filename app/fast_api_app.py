import contextlib
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
import google.auth
from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.cloud import logging as google_cloud_logging
from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.typing import Feedback
from app.triggers.agenda.agenda_router import router as agenda_router
from app.triggers.agenda.auth_router import router as auth_router
from app.triggers.checklist_router import router as checklist_router
from app.triggers.dossier.dossier_router import router as dossier_router
from app.triggers.agenda.goals_router import router as goals_router
from app.triggers.google_oauth_router import router as google_oauth_router
from app.triggers.graph_router import router as graph_router
from app.triggers.agenda.ledgers_router import router as ledgers_router
from app.triggers.live_notifications_router import router as live_notifications_router
from app.triggers.mentor_advisor_router import router as mentor_advisor_router
from app.triggers.agenda.preferences_router import router as preferences_router
from app.triggers.agenda.setup_router import router as setup_router
from app.triggers.slack.slack_router import router as slack_router
from app.triggers.webhooks.webhook_router import router as webhook_router

logging.basicConfig(level=logging.INFO)
load_dotenv()
_, project_id = google.auth.default()
logging_client = google_cloud_logging.Client()
logger = logging_client.logger(__name__)
allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent
    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    yield

app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=False,
    lifespan=lifespan,
)
app.title = "mentor-agent"
app.description = "API for interacting with the Agent mentor-agent"
app.include_router(slack_router)
app.include_router(webhook_router)
app.include_router(setup_router)
app.include_router(auth_router)
app.include_router(agenda_router)
app.include_router(preferences_router)
app.include_router(goals_router)
app.include_router(ledgers_router)
app.include_router(dossier_router)
app.include_router(graph_router)
app.include_router(live_notifications_router)
app.include_router(google_oauth_router)
app.include_router(checklist_router)
app.include_router(mentor_advisor_router)
STATIC_DIR = Path(__file__).parent / "static"
_mentor_ui_enabled = os.environ.get("MENTOR_UI_ENABLED")
if _mentor_ui_enabled is None and os.environ.get("AGENDA_UI_ENABLED") is not None:
    logging.getLogger(__name__).warning(
        "AGENDA_UI_ENABLED is deprecated, rename it to MENTOR_UI_ENABLED "
        "(still honored for now, but this fallback may be removed later)"
    )
    _mentor_ui_enabled = os.environ.get("AGENDA_UI_ENABLED")
class _NoCacheStaticFiles(StaticFiles):
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


if STATIC_DIR.exists() and _mentor_ui_enabled == "true":
    app.mount("/static", _NoCacheStaticFiles(directory=STATIC_DIR), name="static")
    @app.get("/ui")
    def serve_agenda_ui():
        import json

        from app.core.config import get_settings

        token = get_settings().agenda_webhook_token
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        injected = f"<script>window.__AGENDA_TOKEN__ = {json.dumps(token)};</script>"
        app_js_version = int((STATIC_DIR / "app.js").stat().st_mtime)
        html = html.replace(
            '<script src="/static/app.js">',
            injected + f'\n  <script src="/static/app.js?v={app_js_version}">',
        )
        return HTMLResponse(content=html)

@app.post("/feedback")
def collect_feedback(feedback: Feedback) -> dict[str, str]:
    logger.log_struct(feedback.model_dump(), severity="INFO")
    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
