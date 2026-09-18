"""Adapts GitHub webhook event payloads into the same raw-dict shape
app/ingest/live_source.py's pull connectors produce for
app/ingest/normalize.py's normalize_work_item — pure functions, no I/O, so
they're testable without a running server or a real webhook delivery.

STATUS: built from GitHub's public webhook payload documentation
(pull_request/issues events), not yet live-proven against a real delivery
— every other adapter in this codebase was confirmed against real traffic
before being trusted (see app/ingest/live_source.py's module docstring);
this one should be re-checked against the first real webhook payload that
arrives once the receiver is registered, same discipline."""

def _repo_full_name(payload: dict) -> str:
    return payload.get("repository", {}).get("full_name", "")


def _actor_login(user_or_assignee: dict | None) -> str | None:
    return user_or_assignee.get("login") if user_or_assignee else None


def adapt_pull_request_event(payload: dict) -> dict | None:
    """Returns None for actions that don't represent a state worth
    tracking (e.g. "labeled", "synchronize" on an already-tracked PR still
    upserts fine via the same external_id, so no action-based filtering is
    needed beyond requiring the "pull_request" key to be present)."""
    pr = payload.get("pull_request")
    if pr is None:
        return None

    number = pr.get("number")
    repo = _repo_full_name(payload)
    external_id = f"{repo}#{number}" if repo and number is not None else None
    if external_id is None:
        return None

    assignee_login = _actor_login(pr.get("assignee")) or _actor_login(pr.get("user"))
    if pr.get("merged"):
        status = "merged"
    elif pr.get("draft"):
        status = "draft"
    else:
        status = pr.get("state", "open")

    return {
        "source": "github",
        "external_id": external_id,
        "actor_reference_key": f"github:{assignee_login}" if assignee_login else "github:unknown",
        "title": pr.get("title", ""),
        "status": status,
        "url": pr.get("html_url"),
        "due_at": None,
        "updated_at": pr.get("updated_at"),
        # A PR with reviewers explicitly requested is blocking someone
        # else's ability to merge/move forward on it — the same
        # "downstream dependent" shape blocks_others already models for
        # Jira's outward "Blocks" link.
        "blocks_others": bool(pr.get("requested_reviewers")),
    }


def adapt_issue_event(payload: dict) -> dict | None:
    """GitHub's webhook fires "issues" events for both plain issues and
    pull requests (a PR is an issue under the hood) — payloads for an
    actual PR carry a "pull_request" key on the issue object; skip those
    here so a PR isn't double-counted between this and
    adapt_pull_request_event."""
    issue = payload.get("issue")
    if issue is None or "pull_request" in issue:
        return None

    number = issue.get("number")
    repo = _repo_full_name(payload)
    external_id = f"{repo}#{number}" if repo and number is not None else None
    if external_id is None:
        return None

    assignee_login = _actor_login(issue.get("assignee")) or _actor_login(
        issue.get("user")
    )

    return {
        "source": "github",
        "external_id": external_id,
        "actor_reference_key": f"github:{assignee_login}" if assignee_login else "github:unknown",
        "title": issue.get("title", ""),
        "status": issue.get("state", "open"),
        "url": issue.get("html_url"),
        "due_at": None,
        "updated_at": issue.get("updated_at"),
        "blocks_others": False,
    }
