from app.identity import config


def test_gate_ordering_is_sane():
    assert 0 < config.MARGIN_MIN < 1
    assert 0 < config.ASK_SCORE < config.AUTO_LINK_SCORE <= 1
    assert config.SINGLE_CANDIDATE_MARGIN == config.MARGIN_MIN * 2
    assert config.MIN_ROSTER_SIZE >= 2
    assert config.FRIDAY_BATCH_CAP == 7
    assert config.MAX_ASKS_PER_CANDIDATE == 1
    assert config.KEY_VERSION >= 1


def test_pending_confirmation_ttl_is_positive():
    from app.identity import config

    assert config.PENDING_CONFIRMATION_TTL_DAYS > 0
