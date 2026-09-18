"""Live Notion SourceClients (app/ingest/base.py) — OKRs/career goals/1:1
notes and org-data (manager/report Pair) sync — backed by the official
@notionhq/notion-mcp-server. Split out of the former app/ingest/
live_source.py — see app/ingest/live_source/__init__.py's own docstring
for the split's full reasoning; every class/adapter here is re-exported
from there so `from app.ingest.live_source import LiveNotionGoalsClient`
(etc.) still works unchanged.

Kept as one file (unlike every other connector, split one-per-file):
LiveNotionGoalsClient, LiveNotionNotesClient, and LiveNotionPairClient all
walk the same Objectives -> Key Results -> Owner/Manager relation chain
independently (each its own live calls, per seed.py's "one client, one
normalize_fn" contract), and all three share the _notion_* property
adapters and resolve_notion_data_source*/​_notion_query_all helpers below
— splitting further would mean either duplicating those or reintroducing
the cross-file coupling this whole reorg is meant to avoid."""

import json
import os

from app.ingest.base import Healthy, Unauthorized, Window
from app.tools.mcp_config import McpSession, notion_mcp_spec


def _notion_plain_text(prop: dict | None) -> str | None:
    if not prop:
        return None
    parts = prop.get("title") or prop.get("rich_text") or []
    return parts[0]["plain_text"] if parts else None


def _notion_select(prop: dict | None) -> str | None:
    if not prop or not prop.get("select"):
        return None
    return prop["select"].get("name")


def _notion_number(prop: dict | None) -> float | None:
    return prop.get("number") if prop else None


def _notion_formula_number(prop: dict | None) -> float | None:
    if not prop or not prop.get("formula"):
        return None
    return prop["formula"].get("number")


def _notion_rollup_progress(prop: dict | None) -> float | None:
    """Confirmed live: this MCP server's rollup values come back as the
    underlying array of per-related-row formula numbers (function
    "show_original"), not a server-computed aggregate — averaged here
    rather than trusting a "function" field, since a live query returned
    "show_original" even for a rollup property configured "average" in
    the db's own schema."""
    if not prop or not prop.get("rollup"):
        return None
    array = prop["rollup"].get("array") or []
    values = [
        item["formula"]["number"]
        for item in array
        if item.get("type") == "formula"
        and item.get("formula", {}).get("number") is not None
    ]
    return sum(values) / len(values) if values else None


def _notion_relation_ids(prop: dict | None) -> list[str]:
    if not prop or not prop.get("relation"):
        return []
    return [r["id"] for r in prop["relation"]]


def _notion_people_emails(prop: dict | None) -> list[str]:
    if not prop or not prop.get("people"):
        return []
    return [
        p["person"]["email"] for p in prop["people"] if p.get("person", {}).get("email")
    ]


def _notion_people_entries(prop: dict | None) -> list[dict]:
    """Like _notion_people_emails, but keeps each entry's Notion person id
    and display name too (needed for auto-provisioning User rows —
    app/ingest/notion_user_provision.py — which needs a stable identity
    anchor, not just an email). Confirmed live: a people-property entry
    has the identical {id, name, person: {email}} shape API-get-users
    returns for the same person."""
    if not prop or not prop.get("people"):
        return []
    entries = []
    for p in prop["people"]:
        email = p.get("person", {}).get("email")
        if not email:
            continue
        entries.append({"id": p.get("id"), "email": email, "name": p.get("name")})
    return entries


def resolve_notion_data_sources(session: McpSession, title: str) -> list[dict]:
    """ALL real API-post-search result items (object=="data_source")
    whose title matches exactly — plural, because a title is not
    actually unique. Confirmed live: users can each create their own
    private db with an identical name (e.g. two separate "Career Goals"
    dbs, one per person, no shared Owner field to disambiguate by) — a
    single-match resolver silently picks whichever one search happens to
    return first, which is wrong whenever that isn't the target user's
    own db (see resolve_notion_data_source's docstring for the
    single-match case, still correct for Objectives/Key Results/1:1
    Notes, which are confirmed live to be singular/shared across users).
    Each item includes both `id` (the data_source_id, what
    API-query-data-source wants) and `parent.database_id` (the STABLE
    database id API-post-page's parent wants instead — confirmed live
    these are genuinely different values). Confirmed live this session:
    data_source_id is not stable across a database's own schema edits,
    and API-post-search is the only discovery tool this MCP server
    exposes (no "list databases" tool) — see notion_mcp_spec's
    docstring."""
    result = session.call("API-post-search", {"query": title})
    items = result.get("results", result) if isinstance(result, dict) else result
    matches = []
    for item in items or []:
        if item.get("object") != "data_source":
            continue
        item_title = item.get("title") or []
        if item_title and item_title[0].get("plain_text") == title:
            matches.append(item)
    return matches


def resolve_notion_data_source(session: McpSession, title: str) -> dict | None:
    """First match only — correct for Objectives/Key Results/1:1 Notes
    (confirmed live: exactly one db per title, shared across users, no
    per-user duplication). NOT correct for anything that can be
    per-user-private with a reused title (Career Goals — see
    resolve_notion_data_sources, the plural version, used there
    instead)."""
    matches = resolve_notion_data_sources(session, title)
    return matches[0] if matches else None


def resolve_notion_data_source_id(session: McpSession, title: str) -> str | None:
    item = resolve_notion_data_source(session, title)
    return item.get("id") if item else None


def resolve_notion_database_id(session: McpSession, title: str) -> str | None:
    item = resolve_notion_data_source(session, title)
    return item.get("parent", {}).get("database_id") if item else None


def _notion_query_all(session: McpSession, data_source_id: str) -> list[dict]:
    """UNVERIFIED pagination path — start_cursor/has_more never actually
    triggered live (the test workspace's dbs never had enough rows).
    Written defensively against Notion's documented cursor shape."""
    pages: list[dict] = []
    cursor: str | None = None
    while True:
        arguments = {"data_source_id": data_source_id}
        if cursor:
            arguments["start_cursor"] = cursor
        result = session.call("API-query-data-source", arguments)
        payload = result if isinstance(result, dict) else {}
        pages.extend(payload.get("results", []))
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")
        if not cursor:
            break
    return pages


def _adapt_notion_objective(page: dict) -> dict:
    props = page.get("properties", {})
    progress = _notion_rollup_progress(
        props.get("Overall Progress ")
    ) or _notion_rollup_progress(props.get("Overall Progress"))
    return {
        "source": "notion",
        "external_id": page["id"],
        "goal_type": "objective",
        "title": _notion_plain_text(props.get("Objective Name")) or "",
        "status": _notion_select(props.get("Status")),
        "quarter": _notion_select(props.get("Quarter")),
        "progress": progress,
        "parent_external_id": None,
    }


def _adapt_notion_key_result(page: dict) -> dict:
    props = page.get("properties", {})
    objective_ids = _notion_relation_ids(props.get("Objective"))
    return {
        "source": "notion",
        "external_id": page["id"],
        "goal_type": "key_result",
        "title": _notion_plain_text(props.get("Key Result Name")) or "",
        "status": None,
        "quarter": None,
        "progress": _notion_formula_number(props.get("Progress")),
        "current_value": _notion_number(props.get("Current Value")),
        "target_value": _notion_number(props.get("Target Value")),
        "parent_external_id": objective_ids[0] if objective_ids else None,
    }


def _adapt_notion_career_goal(page: dict) -> dict:
    """Career Goals can link to more than one Objective (a real relation
    list) but Goal.parent_external_id is a single column — only the first
    linked Objective id is kept there; ownership filtering (LiveNotion
    GoalsClient.fetch()) checks the FULL relation list, not just this
    truncated first id."""
    props = page.get("properties", {})
    objective_ids = _notion_relation_ids(props.get("Objectives"))
    return {
        "source": "notion",
        "external_id": page["id"],
        "goal_type": "career_goal",
        "title": _notion_plain_text(props.get("Career Goal")) or "",
        "status": None,
        "quarter": None,
        "parent_external_id": objective_ids[0] if objective_ids else None,
    }


def _adapt_notion_one_on_one_note(page: dict) -> dict:
    props = page.get("properties", {})
    kr_ids = _notion_relation_ids(props.get("Key Results"))
    return {
        "source": "notion",
        "external_id": page["id"],
        "title": _notion_plain_text(props.get("Meeting Note")),
        "meeting_id": None,
        "linked_key_result_external_ids": json.dumps(kr_ids) if kr_ids else None,
    }


class LiveNotionGoalsClient:
    """Backed by the official @notionhq/notion-mcp-server. Real schema
    confirmed live this session for all 3 dbs: Objectives (Objective
    Name/title, Quarter/select, Status/select, Owner/people, "Overall
    Progress "/rollup, Key Results/relation), Key Results (Key Result
    Name/title, Current/Target/Start Value/number, Progress/formula,
    Objective/relation), Career Goals (Career Goal/title,
    Objectives/relation).

    Ownership: Objectives is the only one of the 3 dbs with an Owner
    (people) field — Career Goals and Key Results have none (confirmed
    live). fetch() filters Objectives to rows whose Owner people include
    notion_owner_email (set via `mentor link-notion`), then keeps only
    Key Results/Career Goals reachable from those Objectives via their
    relation fields, rather than filtering those two independently.
    Returns [] (not an error) when notion_owner_email hasn't been linked
    yet — same "nothing to attribute" shape as an unset credential.

    Career Goals specifically: confirmed live that this can be a SEPARATE
    private database per user, all sharing the identical title "Career
    Goals" (unlike Objectives/Key Results/1:1 Notes, confirmed singular).
    fetch() queries every "Career Goals"-titled db found (resolve_notion_
    data_sources, plural — see its own docstring), not just the first
    match a naive title search would pick; the existing Objectives-
    relation ownership filter (same mechanism as Key Results above) then
    naturally keeps only rows that actually belong to this user, so
    querying an extra person's db just contributes zero rows rather than
    leaking their data.

    Data source ids are resolved by title via API-post-search on every
    fetch(), never hardcoded — see notion_mcp_spec's docstring for the
    live-confirmed reason (data_source_id churns on schema edits)."""

    source = "notion"

    def __init__(
        self,
        notion_token: str | None = None,
        notion_owner_email: str | None = None,
    ):
        self.notion_token = notion_token or os.environ.get("NOTION_TOKEN")
        self.notion_owner_email = notion_owner_email

    def _spec(self):
        return notion_mcp_spec(self.notion_token)

    def health(self):
        return Healthy() if self.notion_token else Unauthorized()

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        if not self.notion_owner_email:
            return []
        try:
            with McpSession(self._spec()) as session:
                objective_ds = resolve_notion_data_source_id(session, "Objectives")
                kr_ds = resolve_notion_data_source_id(session, "Key Results")
                # Plural, not resolve_notion_data_source_id — Career Goals
                # can be a separate private db per user with an identical
                # title (confirmed live), unlike Objectives/Key Results/
                # 1:1 Notes. Query every match; the Objectives-relation
                # filter below (same mechanism already used for Key
                # Results) is what actually decides ownership per row, so
                # querying an extra db that isn't this user's just
                # contributes zero matching rows, not wrong data.
                career_ds_list = [
                    item["id"]
                    for item in resolve_notion_data_sources(session, "Career Goals")
                ]

                rows: list[dict] = []
                owned_objective_ids: set[str] = set()

                if objective_ds:
                    for page in _notion_query_all(session, objective_ds):
                        emails = _notion_people_emails(
                            page.get("properties", {}).get("Owner")
                        )
                        if self.notion_owner_email not in emails:
                            continue
                        owned_objective_ids.add(page["id"])
                        rows.append(_adapt_notion_objective(page))

                if kr_ds and owned_objective_ids:
                    for page in _notion_query_all(session, kr_ds):
                        relation_ids = _notion_relation_ids(
                            page.get("properties", {}).get("Objective")
                        )
                        if owned_objective_ids.intersection(relation_ids):
                            rows.append(_adapt_notion_key_result(page))

                if career_ds_list and owned_objective_ids:
                    for career_ds in career_ds_list:
                        for page in _notion_query_all(session, career_ds):
                            relation_ids = _notion_relation_ids(
                                page.get("properties", {}).get("Objectives")
                            )
                            if owned_objective_ids.intersection(relation_ids):
                                rows.append(_adapt_notion_career_goal(page))
        except Exception:
            return []
        return rows


class LiveNotionNotesClient:
    """Backed by the same official Notion MCP server as
    LiveNotionGoalsClient — the "1:1 Notes" db (Meeting Note/title, Key
    Results/relation to the Key Results db). No Owner field on this db
    either (confirmed live) — ownership resolved transitively through the
    same Key-Result -> Objective -> Owner chain LiveNotionGoalsClient
    already walks. Re-derives that chain independently (a second,
    self-contained set of live calls) rather than sharing state with
    LiveNotionGoalsClient, matching seed.py's "one client, one
    normalize_fn" contract, which gives each client its own fetch()."""

    source = "notion"

    def __init__(
        self,
        notion_token: str | None = None,
        notion_owner_email: str | None = None,
    ):
        self.notion_token = notion_token or os.environ.get("NOTION_TOKEN")
        self.notion_owner_email = notion_owner_email

    def _spec(self):
        return notion_mcp_spec(self.notion_token)

    def health(self):
        return Healthy() if self.notion_token else Unauthorized()

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        if not self.notion_owner_email:
            return []
        try:
            with McpSession(self._spec()) as session:
                objective_ds = resolve_notion_data_source_id(session, "Objectives")
                kr_ds = resolve_notion_data_source_id(session, "Key Results")
                notes_ds = resolve_notion_data_source_id(session, "1:1 Notes")

                owned_objective_ids: set[str] = set()
                if objective_ds:
                    for page in _notion_query_all(session, objective_ds):
                        emails = _notion_people_emails(
                            page.get("properties", {}).get("Owner")
                        )
                        if self.notion_owner_email in emails:
                            owned_objective_ids.add(page["id"])

                owned_kr_ids: set[str] = set()
                if kr_ds and owned_objective_ids:
                    for page in _notion_query_all(session, kr_ds):
                        relation_ids = _notion_relation_ids(
                            page.get("properties", {}).get("Objective")
                        )
                        if owned_objective_ids.intersection(relation_ids):
                            owned_kr_ids.add(page["id"])

                rows: list[dict] = []
                if notes_ds and owned_kr_ids:
                    for page in _notion_query_all(session, notes_ds):
                        relation_ids = _notion_relation_ids(
                            page.get("properties", {}).get("Key Results")
                        )
                        if owned_kr_ids.intersection(relation_ids):
                            rows.append(_adapt_notion_one_on_one_note(page))
        except Exception:
            return []
        return rows


class LiveNotionPairClient:
    """Backed by the same official Notion MCP server as
    LiveNotionGoalsClient. Real "Manager" (people) property confirmed
    live this session, added to the Objectives db alongside the existing
    "Owner" field — the manager/report relationship this app calls a
    Pair. Notion's Teamspace grouping feature (a real workspace-
    organization feature the user set up in parallel — one Teamspace per
    pair, e.g. "Dev Team") is NOT usable for this: confirmed live via
    session.list_tools() that this MCP server exposes no team/teamspace-
    membership tool at all (only a flat, ungrouped API-get-users) — the
    Manager field is the only actually-queryable signal.

    Not a SourceClient in the usual sense (no owner_user_id scoping — a
    Pair-sync edge is inherently cross-user, org-wide). fetch() returns
    one edge dict per Objective whose Owner and Manager people fields are
    BOTH set and DIFFERENT (Owner == Manager means "no manager tracked for
    this Objective," not a Pair). Only the first email in each people
    field is used — Notion's Owner/Manager properties are modeled here as
    single-person fields in practice, same assumption LiveNotionGoalsClient's
    Owner-based ownership filter already makes."""

    source = "notion_pairs"

    def __init__(self, notion_token: str | None = None):
        self.notion_token = notion_token or os.environ.get("NOTION_TOKEN")

    def _spec(self):
        return notion_mcp_spec(self.notion_token)

    def health(self):
        return Healthy() if self.notion_token else Unauthorized()

    def fetch(self, window: Window, owner_user_id: str) -> list[dict]:
        try:
            with McpSession(self._spec()) as session:
                objective_ds = resolve_notion_data_source_id(session, "Objectives")
                if not objective_ds:
                    return []

                edges: list[dict] = []
                seen: set[tuple[str, str]] = set()
                for page in _notion_query_all(session, objective_ds):
                    props = page.get("properties", {})
                    owner_emails = _notion_people_emails(props.get("Owner"))
                    manager_emails = _notion_people_emails(props.get("Manager"))
                    if not owner_emails or not manager_emails:
                        continue
                    report_email = owner_emails[0]
                    manager_email = manager_emails[0]
                    if report_email == manager_email:
                        continue
                    key = (report_email, manager_email)
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append(
                        {
                            "report_notion_email": report_email,
                            "manager_notion_email": manager_email,
                        }
                    )
        except Exception:
            return []
        return edges

    def fetch_directory(self) -> list[dict]:
        """One Objectives scan returning every DISTINCT person referenced
        in Owner or Manager (regardless of whether they're paired with
        anyone — unlike fetch(), which only yields owner!=manager edges),
        for auto-provisioning User rows (app/ingest/notion_user_
        provision.py). Notion's people-type property is a picker bound to
        the workspace's real member/guest list — you cannot type an
        arbitrary email into it — so anyone discovered here is, by
        construction, already a real workspace member; no separate
        membership check is needed at provisioning time (see revoke_
        departed_notion_users for the reverse: detecting someone who
        WAS a member and left). Deduplicated by Notion's own stable
        person id, not email."""
        try:
            with McpSession(self._spec()) as session:
                objective_ds = resolve_notion_data_source_id(session, "Objectives")
                if not objective_ds:
                    return []
                seen: dict[str, dict] = {}
                for page in _notion_query_all(session, objective_ds):
                    props = page.get("properties", {})
                    for prop_name in ("Owner", "Manager"):
                        for entry in _notion_people_entries(props.get(prop_name)):
                            person_id = entry.get("id")
                            if not person_id:
                                continue
                            seen[person_id] = {
                                "notion_person_id": person_id,
                                "email": entry["email"],
                                "display_name": entry.get("name"),
                            }
        except Exception:
            return []
        return list(seen.values())

    def fetch_active_member_ids(self) -> set[str] | None:
        """Full current-workspace-member id set (API-get-users, type ==
        "person" only — excludes bot integrations like this app's own
        Notion connection), for revoke_departed_notion_users to diff
        against previously-provisioned notion_person_ids. Deliberately a
        SEPARATE live call from fetch_directory(): someone can be removed
        from the workspace without ever appearing in an Objectives Owner/
        Manager field again to prove it, so revocation needs the full
        member list, not just the narrower Objectives-derived directory.
        Returns None (not an empty set) on any failure — a transient API
        error must never be read as "everyone left the workspace" and
        mass-revoke access; the caller treats None as "unknown, skip this
        cycle."

        REAL BUG FOUND AND FIXED (confirmed live this session): Notion's
        error response is itself a dict (e.g. {"object": "error",
        "status": 401, "message": "API token is invalid.", ...} — this
        exact shape was observed live earlier). The naive `result.get(
        "results", result)` fallback silently treated that error dict AS
        IF it were the results list when no "results" key was present —
        iterating over a dict yields its string KEYS, every one of which
        then failed the `isinstance(u, dict)` check, producing an EMPTY
        SET, not None. That is exactly the "everyone left the workspace"
        failure mode this function's own docstring promises can't
        happen. This actually fired in production once already, and
        revoke_departed_notion_users correctly (per its own logic, given
        the wrong input) revoked THREE real, currently-active linked
        users' agenda_client_secret as a result. Fixed by explicitly
        requiring a real list of results before treating the response as
        valid membership data; anything else (an error object, a
        missing "results" key, a non-list/non-dict response) now
        correctly returns None instead of silently degrading to an
        empty set."""
        try:
            with McpSession(self._spec()) as session:
                result = session.call("API-get-users", {})
                if isinstance(result, list):
                    users = result
                elif isinstance(result, dict):
                    if result.get("object") == "error":
                        return None
                    users = result.get("results")
                    if not isinstance(users, list):
                        return None
                else:
                    return None
                return {
                    u["id"]
                    for u in users
                    if isinstance(u, dict) and u.get("type") == "person" and u.get("id")
                }
        except Exception:
            return None
