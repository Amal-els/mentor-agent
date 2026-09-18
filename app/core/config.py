from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = (
        "postgresql+psycopg://mentor:mentor_dev_only@localhost:5432/mentor"
    )
    slack_signing_secret: str = ""
    slack_app_token: str = ""

    # Webhook receivers (app/triggers/webhook_router.py) — this app is
    # single-tenant in practice today (every live connector already reads
    # one set of credentials from process-wide env vars, not per-user), so
    # every verified webhook event is attributed to this one owner rather
    # than needing a per-repo/per-team owner-resolution step.
    webhook_owner_user_id: str = ""
    github_webhook_secret: str = ""
    linear_webhook_secret: str = ""
    jira_webhook_token: str = ""

    # Agenda feature's own HTTP entry points
    # (app/triggers/agenda_router.py) — internal/first-party callers (the
    # AG-UI frontend, whatever schedules trigger delivery), so a shared
    # token (verify_shared_token) is enough, the same scheme jira_webhook_token
    # already uses above rather than full HMAC.
    agenda_webhook_token: str = ""

    # Auto-provisioning/auto-revoking User rows from a live Notion
    # Objectives Owner/Manager directory scan (app/ingest/
    # notion_user_provision.py, wired into app/triggers/agenda_scheduler.py)
    # instead of requiring an admin to run `mentor link-notion` for every
    # new person. Off by default — same opt-in posture as the existing
    # AGENDA_NOTION_PAIR_SYNC flag, since this is a real trust-boundary
    # shift (anyone added to Objectives' Owner/Manager gets an account and
    # a real email automatically).
    agenda_auto_provision_notion_users: bool = False
    # Needed to build a working setup-link URL from a background scheduler
    # tick, which has no incoming HTTP request to read a base_url from
    # (unlike POST /webhooks/request-setup-link, which uses request.
    # base_url). Left blank means "don't send the onboarding email" (see
    # issue_setup_link's callers) rather than emailing a broken link.
    agenda_public_base_url: str = ""
    # Schedule defaults for an auto-provisioned User — genuinely not
    # derivable from Notion (Objectives has no concept of a person's
    # timezone or pulse-fire time), so every freshly auto-created row gets
    # these org-wide defaults rather than a per-person guess.
    agenda_default_tz: str = "UTC"
    agenda_default_pulse_time_local: str = "08:30"
    agenda_default_late_cutoff_local: str = "21:00"

    # .env carries config Settings doesn't own (GOOGLE_*, SLACK_BOT_TOKEN,
    # ...) — pydantic-settings' default forbids any .env key not declared
    # as a field here, so every unrelated var in a real .env file would
    # break Settings() entirely. extra="ignore" opts out of that.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
