from scripts.validate_pulse_fixtures import FIXTURES_DIR, validate_all


def test_all_pulse_fixtures_are_valid():
    validated = validate_all(FIXTURES_DIR)
    assert "normal_day" in validated
    assert "clear_day" in validated
    assert "series_suppression" in validated
    assert "critic/clean_pass" in validated


def test_at_least_one_critic_scenario_is_a_clean_pass():
    # guards against a critic fixture set where every scenario expects
    # revise — that would let an always-revise critic pass validation
    import yaml

    critic_dir = FIXTURES_DIR / "critic"
    verdicts = {
        yaml.safe_load((d / "expected_revision.yaml").read_text(encoding="utf-8"))[
            "verdict"
        ]
        for d in critic_dir.iterdir()
        if d.is_dir()
    }
    assert "pass" in verdicts
    assert "revise" in verdicts
