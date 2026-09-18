def test_pending_consent_threshold_is_positive():
    from app.agenda import config

    assert config.PENDING_CONSENT_THRESHOLD > 0
