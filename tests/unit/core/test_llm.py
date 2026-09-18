from app.core.llm import creds_available


def test_no_creds_configured(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    assert creds_available() is False


def test_gemini_api_key_is_sufficient(monkeypatch):
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    assert creds_available() is True


def test_google_api_key_is_sufficient(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key")

    assert creds_available() is True


def test_vertex_ai_requires_both_flag_and_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    assert creds_available() is False  # flag alone isn't enough

    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-project")
    assert creds_available() is False  # project alone isn't enough


def test_vertex_ai_enabled_with_flag_and_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "civentra")

    assert creds_available() is True


def test_vertex_ai_flag_is_case_insensitive(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "True")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "civentra")

    assert creds_available() is True
