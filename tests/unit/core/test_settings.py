from app.core.config import Settings


def test_settings_ignores_env_vars_it_does_not_declare(monkeypatch, tmp_path):
    # regression: pydantic-settings' default env_file behavior forbids any
    # .env key not declared as a Settings field — real .env files carry
    # unrelated config (GOOGLE_GENAI_USE_VERTEXAI, GOOGLE_CLOUD_PROJECT,
    # SLACK_BOT_TOKEN, ...) that Settings never needs to know about.
    #
    # Real environment variables outrank _env_file in pydantic-settings'
    # source precedence — app/cli.py's own load_dotenv() call (triggered
    # merely by importing app.cli, which tests/test_cli.py does) can have
    # already populated os.environ['DATABASE_URL'] for the rest of this
    # pytest process. Clear it so this test verifies Settings' own
    # env_file/extra="ignore" behavior, not whatever ran before it.
    monkeypatch.delenv("DATABASE_URL", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+psycopg://x:y@localhost:5432/z\n"
        "GOOGLE_GENAI_USE_VERTEXAI=true\n"
        "GOOGLE_CLOUD_PROJECT=some-project\n"
        "SOME_UNRELATED_VAR=whatever\n"
    )

    settings = Settings(_env_file=str(env_file))

    assert settings.database_url == "postgresql+psycopg://x:y@localhost:5432/z"
