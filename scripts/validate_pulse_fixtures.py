"""Validate app/fixtures/pulse/* against the contract in
app/fixtures/pulse/README.md. Run directly (`uv run python
scripts/validate_pulse_fixtures.py`) or import `validate_all` from tests.
"""

import sys
from pathlib import Path

import yaml

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "app" / "fixtures" / "pulse"

_REQUIRED_DAY_KEYS = {
    "name",
    "description",
    "owner",
    "now",
    "events",
    "work_items",
    "messages",
    "commitments",
}
_REQUIRED_OWNER_KEYS = {"id", "tz", "pulse_fire_time_local", "late_cutoff_local"}
_REQUIRED_EVENT_KEYS = {
    "external_id",
    "source",
    "title",
    "starts_at",
    "ends_at",
    "status",
    "actor_reference_key",
}
_REQUIRED_WORK_ITEM_KEYS = {
    "external_id",
    "source",
    "title",
    "status",
    "actor_reference_key",
}
_REQUIRED_MESSAGE_KEYS = {
    "external_id",
    "source",
    "channel",
    "sent_at",
    "is_dm",
    "body_ref",
    "actor_reference_key",
}
_REQUIRED_COMMITMENT_KEYS = {"description", "promised_at", "status"}


def _require_keys(d: dict, required: set[str], where: str) -> None:
    missing = required - d.keys()
    if missing:
        raise AssertionError(f"{where}: missing required keys {sorted(missing)}")


def validate_day_fixture(path: Path) -> str:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    where = path.name

    _require_keys(data, _REQUIRED_DAY_KEYS, where)
    if data["name"] != path.stem:
        raise AssertionError(
            f"{where}: name={data['name']!r} does not match filename stem {path.stem!r}"
        )

    _require_keys(data["owner"], _REQUIRED_OWNER_KEYS, f"{where}:owner")

    for event in data["events"]:
        _require_keys(
            event, _REQUIRED_EVENT_KEYS, f"{where}:events[{event.get('external_id')}]"
        )
    for item in data["work_items"]:
        _require_keys(
            item,
            _REQUIRED_WORK_ITEM_KEYS,
            f"{where}:work_items[{item.get('external_id')}]",
        )
    for msg in data["messages"]:
        _require_keys(
            msg, _REQUIRED_MESSAGE_KEYS, f"{where}:messages[{msg.get('external_id')}]"
        )
        if not str(msg["body_ref"]).startswith("fixture://"):
            raise AssertionError(
                f"{where}:messages[{msg.get('external_id')}]: body_ref must be a "
                "fixture:// pointer, never real message content"
            )
    for commitment in data["commitments"]:
        _require_keys(
            commitment,
            _REQUIRED_COMMITMENT_KEYS,
            f"{where}:commitments[{commitment.get('description')}]",
        )

    for suppression in data.get("suppressions", []):
        _require_keys(
            suppression,
            {"scope", "target_ref", "reason", "expires_at"},
            f"{where}:suppressions[{suppression.get('target_ref')}]",
        )
        if suppression["scope"] not in {"instance", "series", "temporal", "global"}:
            raise AssertionError(
                f"{where}:suppressions[{suppression.get('target_ref')}]: "
                f"invalid scope {suppression['scope']!r}"
            )

    for source, spec in data.get("unhealthy_sources", {}).items():
        if spec.get("status") not in {"unauthorized", "error"}:
            raise AssertionError(
                f"{where}:unhealthy_sources[{source}]: invalid status {spec.get('status')!r}"
            )
        if spec["status"] == "error" and "stale_as_of" not in spec:
            raise AssertionError(
                f"{where}:unhealthy_sources[{source}]: status=error requires stale_as_of"
            )

    return data["name"]


def validate_critic_scenario(scenario_dir: Path) -> str:
    where = scenario_dir.name
    required_files = {"draft_card.yaml", "context.yaml", "expected_revision.yaml"}
    present = {p.name for p in scenario_dir.glob("*.yaml")}
    missing = required_files - present
    if missing:
        raise AssertionError(f"critic/{where}: missing files {sorted(missing)}")
    extra = present - required_files
    if extra:
        raise AssertionError(f"critic/{where}: unexpected files {sorted(extra)}")

    context = yaml.safe_load(
        (scenario_dir / "context.yaml").read_text(encoding="utf-8")
    )
    draft = yaml.safe_load(
        (scenario_dir / "draft_card.yaml").read_text(encoding="utf-8")
    )
    expected = yaml.safe_load(
        (scenario_dir / "expected_revision.yaml").read_text(encoding="utf-8")
    )

    _require_keys(context, {"shortlist", "degraded_sources"}, f"critic/{where}:context")
    for item in context["shortlist"]:
        _require_keys(
            item,
            {"item_id", "score", "score_terms", "candidate_focus"},
            f"critic/{where}:context.shortlist[{item.get('item_id')}]",
        )

    _require_keys(
        draft,
        {"prompt_id", "temperature", "degradation_line", "items"},
        f"critic/{where}:draft_card",
    )
    for item in draft["items"]:
        _require_keys(
            item,
            {"item_id", "title", "why_now", "action"},
            f"critic/{where}:draft_card.items",
        )

    _require_keys(expected, {"verdict", "reasons"}, f"critic/{where}:expected_revision")
    if expected["verdict"] not in {"pass", "revise"}:
        raise AssertionError(
            f"critic/{where}:expected_revision: invalid verdict {expected['verdict']!r}"
        )
    if expected["verdict"] == "revise" and not expected["reasons"]:
        raise AssertionError(
            f"critic/{where}:expected_revision: verdict=revise requires at least one reason"
        )
    if expected["verdict"] == "pass" and expected["reasons"]:
        raise AssertionError(
            f"critic/{where}:expected_revision: verdict=pass must have no reasons"
        )
    for reason in expected["reasons"]:
        _require_keys(
            reason, {"item_id", "reason"}, f"critic/{where}:expected_revision.reasons"
        )

    return where


def validate_ranker_scenario(path: Path) -> str:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    where = path.name

    _require_keys(data, {"name", "shortlist", "expected_ordered_ids"}, where)
    if data["name"] != path.stem:
        raise AssertionError(
            f"{where}: name={data['name']!r} does not match filename stem {path.stem!r}"
        )

    ids = set()
    for item in data["shortlist"]:
        _require_keys(
            item,
            {"item_id", "score", "score_terms"},
            f"{where}:shortlist[{item.get('item_id')}]",
        )
        ids.add(item["item_id"])

    if len(data["expected_ordered_ids"]) > 3:
        raise AssertionError(f"{where}: expected_ordered_ids has more than 3 entries")
    if len(data["expected_ordered_ids"]) != len(set(data["expected_ordered_ids"])):
        raise AssertionError(f"{where}: expected_ordered_ids has duplicates")
    unknown = set(data["expected_ordered_ids"]) - ids
    if unknown:
        raise AssertionError(
            f"{where}: expected_ordered_ids references ids not in shortlist: {sorted(unknown)}"
        )

    return data["name"]


def validate_all(fixtures_dir: Path = FIXTURES_DIR) -> list[str]:
    validated = []

    day_fixtures = sorted(fixtures_dir.glob("*.yaml"))
    if not day_fixtures:
        raise AssertionError(f"no day fixtures found under {fixtures_dir}")
    for path in day_fixtures:
        validated.append(validate_day_fixture(path))

    critic_dir = fixtures_dir / "critic"
    scenario_dirs = sorted(p for p in critic_dir.iterdir() if p.is_dir())
    if not scenario_dirs:
        raise AssertionError(f"no critic scenarios found under {critic_dir}")
    verdicts = set()
    for scenario_dir in scenario_dirs:
        validate_critic_scenario(scenario_dir)
        expected = yaml.safe_load(
            (scenario_dir / "expected_revision.yaml").read_text(encoding="utf-8")
        )
        verdicts.add(expected["verdict"])
        validated.append(f"critic/{scenario_dir.name}")
    if "pass" not in verdicts:
        raise AssertionError(
            "no critic scenario has verdict=pass — a critic that always revises would "
            "pass every other fixture"
        )

    ranker_dir = fixtures_dir / "ranker"
    ranker_fixtures = sorted(ranker_dir.glob("*.yaml"))
    if not ranker_fixtures:
        raise AssertionError(f"no ranker fixtures found under {ranker_dir}")
    for path in ranker_fixtures:
        validated.append(f"ranker/{validate_ranker_scenario(path)}")

    return validated


def main() -> int:
    try:
        validated = validate_all()
    except AssertionError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    for name in validated:
        print(f"ok: {name}")
    print(f"{len(validated)} fixtures valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
