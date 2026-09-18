from app.identity import config, matchers


def test_gate_boundaries_match_config_exactly():
    just_below_auto = config.AUTO_LINK_SCORE - 0.01
    at_auto = config.AUTO_LINK_SCORE

    assert matchers.gate(just_below_auto, margin=1.0, roster_size=10) != "auto_link"
    assert matchers.gate(at_auto, margin=1.0, roster_size=10) == "auto_link"


def test_gate_requires_min_roster_size_for_auto_link():
    result = matchers.gate(
        config.AUTO_LINK_SCORE, margin=1.0, roster_size=config.MIN_ROSTER_SIZE - 1
    )
    assert result != "auto_link"


def test_gate_ask_boundary():
    just_below_ask = config.ASK_SCORE - 0.01
    at_ask_with_margin = config.ASK_SCORE

    assert matchers.gate(just_below_ask, margin=1.0, roster_size=10) == "none"
    assert (
        matchers.gate(at_ask_with_margin, margin=config.MARGIN_MIN, roster_size=10)
        == "ask"
    )


def test_gate_ask_requires_margin():
    result = matchers.gate(
        config.ASK_SCORE, margin=config.MARGIN_MIN - 0.01, roster_size=10
    )
    assert result == "none"


def test_gate_is_exhaustive_and_exclusive():
    for score in (0.0, 0.3, 0.69, 0.70, 0.85, 0.94, 0.95, 1.0):
        for margin in (0.0, 0.1, 0.15, 0.29, 0.30, 0.5):
            outcome = matchers.gate(score, margin, roster_size=10)
            assert outcome in ("auto_link", "ask", "none")
