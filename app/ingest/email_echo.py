"""Ingest-time extraction of structured references from notification emails
(Gmail echoes of events originating from GitHub, Jira, Slack, Notion).

AGENT.md Privacy Rule: Raw email subjects and bodies are stripped during
ingestion and never persisted. This module runs BEFORE stripping, extracting
only a minimal structured reference (e.g. source="github", external_id="org/repo#42")
and storing that derived pointer on the Message row (source_echo_of).

Downstream salience scoring uses source_echo_of to suppress redundant Gmail
echoes when the origin event is already ingested from its direct source.
"""

import dataclasses
import re
from collections.abc import Callable
from typing import Any

# Shared reference parsing patterns
URL_RE = re.compile(r"https?://[^\s)>]+")
GITHUB_ISSUE_KEY_RE = re.compile(r"(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)")
GITHUB_URL_RE = re.compile(
    r"https?://github\.com/(?P<repo>[^/]+/[^/]+)/(issues|pull)/(?P<number>\d+)"
)
GITHUB_SUBJECT_RE = re.compile(r"\[(?P<repo>[\w.-]+/[\w.-]+)\].*?#(?P<number>\d+)")

JIRA_KEY_RE = re.compile(r"\b(?P<key>[A-Z][A-Z0-9_]+-\d+)\b")
JIRA_URL_RE = re.compile(
    r"https?://[a-zA-Z0-9.-]+\.atlassian\.net/browse/(?P<key>[A-Z][A-Z0-9_]+-\d+)"
)

SLACK_PERMALINK_RE = re.compile(
    r"https?://[a-zA-Z0-9.-]+\.slack\.com/archives/(?P<channel>[A-Z0-9]+)/p(?P<ts_compact>\d{10,16})"
)

NOTION_PAGE_URL_RE = re.compile(
    r"https?://(?:www\.)?notion\.so/(?:[a-zA-Z0-9_-]+/)?(?:[a-zA-Z0-9_-]+-)?(?P<page_id>[a-f0-9]{32}|[a-f0-9-]{36})",
    re.IGNORECASE,
)

NOTION_CONTENT_ACTIVITY_KEYWORDS = (
    "mentioned you in",
    "mentioned you on",
    "commented in",
    "commented on",
    "replied to",
    "assigned you",
    "updated",
    "shared the page",
    "shared a page",
    "invited you to edit",
    "added a comment",
    "new comment",
)

NOTION_WORKSPACE_MEMBERSHIP_KEYWORDS = (
    "invited you to the workspace",
    "invited you to join",
    "joined the workspace",
    "workspace role",
    "removed from the workspace",
    "removed from workspace",
    "workspace settings",
    "workspace member",
    "welcome to notion",
    "reset your password",
)


@dataclasses.dataclass(frozen=True)
class SourceEchoRef:
    """Minimal structured identifier representing the origin event this email echoes."""

    source: str  # "github" | "jira" | "slack" | "notion"
    external_id: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "external_id": self.external_id}

    @classmethod
    def from_dict(cls, data: dict | None) -> "SourceEchoRef | None":
        if not data or not isinstance(data, dict):
            return None
        source = data.get("source")
        external_id = data.get("external_id")
        if source and external_id:
            return cls(source=str(source), external_id=str(external_id))
        return None


def _format_slack_timestamp(ts_compact: str) -> str:
    """Converts Slack permalink compact timestamp 'p1787864910052589' (16 digits)
    to canonical Slack message ts '1787864910.052589'."""
    if len(ts_compact) > 6:
        return f"{ts_compact[:-6]}.{ts_compact[-6:]}"
    return ts_compact


def _normalize_notion_uuid(raw_id: str) -> str:
    """Converts 32-char hex string to canonical 36-char hyphenated UUID."""
    clean = raw_id.replace("-", "").lower()
    if len(clean) == 32:
        return f"{clean[:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:]}"
    return raw_id.lower()


def _extract_all_text(raw_email: dict) -> str:
    """Combines all raw text fields (subject, snippet, bodyExcerpt, text) for matcher regexes."""
    parts = [
        raw_email.get("subject") or "",
        raw_email.get("snippet") or "",
        raw_email.get("bodyExcerpt") or "",
        raw_email.get("text") or "",
        raw_email.get("body") or "",
    ]
    return " ".join(p for p in parts if p)


# --- Provider Matchers --------------------------------------------------------


def match_github_echo(raw_email: dict) -> SourceEchoRef | None:
    from_header = (raw_email.get("from") or "").lower()
    domain = (raw_email.get("domain") or "").lower()
    subject = raw_email.get("subject") or ""
    all_text = _extract_all_text(raw_email)

    is_github_sender = (
        "notifications@github.com" in from_header
        or "github.com" in from_header
        or domain == "github.com"
    )
    if not is_github_sender:
        return None

    # 1. URL in text: https://github.com/org/repo/(issues|pull)/42
    url_match = GITHUB_URL_RE.search(all_text)
    if url_match:
        return SourceEchoRef(
            source="github",
            external_id=f"{url_match.group('repo')}#{url_match.group('number')}",
        )

    # 2. Subject pattern: [org/repo] Title (#42) or [org/repo] Pull request #42
    subj_match = GITHUB_SUBJECT_RE.search(subject)
    if subj_match:
        return SourceEchoRef(
            source="github",
            external_id=f"{subj_match.group('repo')}#{subj_match.group('number')}",
        )

    # 3. Direct org/repo#42 anywhere in subject or body
    key_match = GITHUB_ISSUE_KEY_RE.search(all_text)
    if key_match:
        return SourceEchoRef(
            source="github",
            external_id=f"{key_match.group('repo')}#{key_match.group('number')}",
        )

    return None


def match_jira_echo(raw_email: dict) -> SourceEchoRef | None:
    from_header = (raw_email.get("from") or "").lower()
    domain = (raw_email.get("domain") or "").lower()
    subject = raw_email.get("subject") or ""
    all_text = _extract_all_text(raw_email)

    is_jira_sender = (
        "atlassian.net" in from_header
        or "atlassian.com" in from_header
        or "jira@" in from_header
        or domain.endswith("atlassian.net")
        or "[jira]" in subject.lower()
    )
    if not is_jira_sender:
        return None

    # 1. Jira URL: https://company.atlassian.net/browse/PROJ-123
    url_match = JIRA_URL_RE.search(all_text)
    if url_match:
        return SourceEchoRef(
            source="jira",
            external_id=url_match.group("key").upper(),
        )

    # 2. Issue key in subject (e.g. "[JIRA] (PROJ-123) Summary" or "PROJ-123 Summary")
    subj_key_match = JIRA_KEY_RE.search(subject)
    if subj_key_match:
        return SourceEchoRef(
            source="jira",
            external_id=subj_key_match.group("key").upper(),
        )

    # 3. Issue key in text
    text_key_match = JIRA_KEY_RE.search(all_text)
    if text_key_match:
        return SourceEchoRef(
            source="jira",
            external_id=text_key_match.group("key").upper(),
        )

    return None


def match_slack_echo(raw_email: dict) -> SourceEchoRef | None:
    from_header = (raw_email.get("from") or "").lower()
    domain = (raw_email.get("domain") or "").lower()
    all_text = _extract_all_text(raw_email)

    is_slack_sender = (
        "slack.com" in from_header
        or domain == "slack.com"
        or "notification@slack.com" in from_header
        or "notifications@slack.com" in from_header
    )
    if not is_slack_sender:
        return None

    # Slack message permalink: https://<workspace>.slack.com/archives/<channel>/p<ts_compact>
    permalink_match = SLACK_PERMALINK_RE.search(all_text)
    if permalink_match:
        ts_formatted = _format_slack_timestamp(permalink_match.group("ts_compact"))
        return SourceEchoRef(
            source="slack",
            external_id=ts_formatted,
        )

    return None


def match_notion_echo(raw_email: dict) -> SourceEchoRef | None:
    from_header = (raw_email.get("from") or "").lower()
    domain = (raw_email.get("domain") or "").lower()
    subject = (raw_email.get("subject") or "").lower()
    all_text = _extract_all_text(raw_email)
    all_text_lower = all_text.lower()

    is_notion_sender = (
        "notion.so" in from_header
        or domain == "notion.so"
        or "notify@notion.so" in from_header
        or "notifications@notion.so" in from_header
    )
    if not is_notion_sender:
        return None

    # POSITIVE CONSTRAINT: Notion in this app is also the HRIS / org-data source.
    # Workspace / membership emails (invite, role change, workspace removal) are NOT
    # echoes of anything else and MUST pass through untouched.
    if any(k in subject or k in all_text_lower for k in NOTION_WORKSPACE_MEMBERSHIP_KEYWORDS):
        return None

    # Must positively be a content-activity notification
    is_content_activity = any(
        k in subject or k in all_text_lower for k in NOTION_CONTENT_ACTIVITY_KEYWORDS
    )
    if not is_content_activity:
        return None

    # Extract page ID from Notion URL in text
    page_match = NOTION_PAGE_URL_RE.search(all_text)
    if page_match:
        page_id = _normalize_notion_uuid(page_match.group("page_id"))
        return SourceEchoRef(
            source="notion",
            external_id=page_id,
        )

    return None


# --- Registry Pattern ---------------------------------------------------------


class EchoExtractorRegistry:
    """Registry of source-specific email echo matchers."""

    def __init__(self) -> None:
        self._matchers: list[tuple[str, Callable[[dict], SourceEchoRef | None]]] = []

    def register(
        self, source: str, matcher_fn: Callable[[dict], SourceEchoRef | None]
    ) -> None:
        self._matchers.append((source, matcher_fn))

    def extract(self, raw_email: dict) -> SourceEchoRef | None:
        """Runs registered matchers in order. Returns the first extracted reference, or None."""
        for _source_name, matcher in self._matchers:
            ref = matcher(raw_email)
            if ref is not None:
                return ref
        return None


# Default global registry pre-populated with GitHub, Jira, Slack, and Notion matchers
default_registry = EchoExtractorRegistry()
default_registry.register("github", match_github_echo)
default_registry.register("jira", match_jira_echo)
default_registry.register("slack", match_slack_echo)
default_registry.register("notion", match_notion_echo)


def extract_source_echo(
    raw_email: dict, registry: EchoExtractorRegistry | None = None
) -> SourceEchoRef | None:
    """Public helper: extracts structured origin reference from raw email dictionary."""
    reg = registry or default_registry
    return reg.extract(raw_email)
