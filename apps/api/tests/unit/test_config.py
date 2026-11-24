"""Settings: one class, every variable of .env.example, unknown names in .env stop the start."""

import re
from datetime import date
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine import make_url

from hub.config import Settings, SettingsError
from hub.main import create_app

ROOT = Path(__file__).resolve().parents[4]
ENV_EXAMPLE = ROOT / ".env.example"


def _env_example_names() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))


def _write_env(tmp_path: Path, text: str) -> Path:
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    return env


def test_env_example_and_settings_name_the_same_variables() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    assert _env_example_names() == fields


def test_env_example_loads_as_settings() -> None:
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert settings.app_env == "local"
    assert settings.core_anchor_date is None  # `CORE_ANCHOR_DATE=` means "not set"


def test_every_secret_loses_surrounding_whitespace(tmp_path: Path) -> None:
    # Secrets pasted from `openssl rand ... | pbcopy` end in a newline (first deploy, P10-07).
    names = [name for name, field in Settings.model_fields.items() if field.annotation is SecretStr]
    assert {"mcp_server_token", "session_secret", "jobs_token", "demo_access_code"} <= set(names)
    values = {name: f"  value-of-{name}\n" for name in names}
    settings = Settings(_env_file=None, **values)  # type: ignore[arg-type]
    for name in names:
        assert getattr(settings, name).get_secret_value() == f"value-of-{name}"


def test_a_secret_from_the_environment_loses_its_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_SERVER_TOKEN", "abc123\n")
    assert Settings(_env_file=None).mcp_server_token.get_secret_value() == "abc123"


def test_other_settings_keep_their_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    assert Settings(_env_file=None).qdrant_url == "http://qdrant:6333"


def test_the_budget_and_the_auto_reply_lock_default_to_the_safe_side() -> None:
    """Owner decision (P10-02): $0.20 a day (operations §6) and auto-reply locked off
    (ADR-0019) unless the deployment sets otherwise."""
    settings = Settings(_env_file=None)
    assert settings.daily_budget_usd == 0.20
    assert settings.auto_reply_allowed is False
    assert Settings(_env_file=ENV_EXAMPLE).auto_reply_allowed is False


def test_unknown_name_in_env_file_fails(tmp_path: Path) -> None:
    env = _write_env(tmp_path, "DATBASE_URL=postgresql+psycopg://x@y/z\n")
    with pytest.raises(ValidationError, match="datbase_url"):
        Settings(_env_file=env)


def test_unknown_name_in_env_file_stops_app_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path, "LOG_LEVL=INFO\nANTHROPIC_KEY=sk-not-a-real-key\nAPP_ENV=prod\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SettingsError) as raised:
        create_app()
    message = str(raised.value)
    assert "LOG_LEVL: Extra inputs are not permitted" in message
    assert "ANTHROPIC_KEY: Extra inputs are not permitted" in message
    assert "APP_ENV: " in message
    # The value under a misspelled name may be a secret: it must not be in the error.
    assert "sk-not-a-real-key" not in message
    assert "prod" not in message
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_app_starts_with_a_valid_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The app loads the model config and the prompts at start (P3-05): point at the repo's.
    _write_env(
        tmp_path,
        "APP_ENV=test\nCORE_ANCHOR_DATE=2026-11-12\n"
        f"MODELS_CONFIG={ROOT / 'config' / 'models.yaml'}\nPROMPTS_DIR={ROOT / 'prompts'}\n"
        f"POLICY_CONFIG={ROOT / 'config' / 'policy.yaml'}\n"
        f"CONFIDENCE_CONFIG={ROOT / 'config' / 'confidence.json'}\n",
    )
    monkeypatch.chdir(tmp_path)
    app = create_app()
    assert app.state.container.settings.core_anchor_date == date(2026, 11, 12)
    assert app.state.container.pipeline is not None
    assert app.state.container.pipeline.write_prompt.version == "write/v5"
    assert app.state.container.pipeline.policy.injection.high_score == 0.5
    handoff_prompt = app.state.container.pipeline.handoff_prompt
    assert handoff_prompt is not None
    assert handoff_prompt.version == "handoff_summary/v1"
    # The run searches the knowledge base through the container's retriever (P5-03).
    assert app.state.container.pipeline.retriever is app.state.container.kb.retriever


@pytest.mark.parametrize(
    ("line", "field"),
    [
        ("APP_ENV=prod", "app_env"),
        ("LLM_MODE=maybe", "llm_mode"),
        ("JIRA_MODE=real", "jira_mode"),
        ("SLACK_MODE=real", "slack_mode"),
        # The real-mode settings are gone (P7-02); an old .env line is refused, not ignored.
        ("JIRA_API_TOKEN=x", "jira_api_token"),
        ("SLACK_BOT_TOKEN=x", "slack_bot_token"),
        ("EVAL_TRACE_SAMPLE=1.5", "eval_trace_sample"),
        ("DAILY_BUDGET_USD=-1", "daily_budget_usd"),
        ("CORE_ANCHOR_DATE=yesterday", "core_anchor_date"),
        ("LOG_LEVEL=LOUD", "log_level"),
    ],
)
def test_invalid_values_fail(tmp_path: Path, line: str, field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=_write_env(tmp_path, line + "\n"))


def test_log_level_is_case_insensitive() -> None:
    assert Settings(_env_file=None, log_level="debug").log_level == "DEBUG"


def test_secrets_are_hidden_in_repr() -> None:
    settings = Settings(_env_file=None, session_secret="not-a-real-secret")  # noqa: S106 - placeholder
    assert "not-a-real-secret" not in repr(settings)
    assert settings.session_secret.get_secret_value() == "not-a-real-secret"


def test_migrations_use_the_direct_url_when_set() -> None:
    pooled = Settings(_env_file=None, database_url="postgresql+psycopg://a@pooled/db")
    assert pooled.database_url_for_migrations.get_secret_value() == (
        "postgresql+psycopg://a@pooled/db"
    )
    direct = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://a@pooled/db",
        database_url_direct="postgresql+psycopg://a@direct/db",
    )
    assert direct.database_url_for_migrations.get_secret_value() == (
        "postgresql+psycopg://a@direct/db"
    )


def test_the_eval_migrator_defaults_to_the_hub_migrator_on_the_eval_database() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://app:pw@host:5432/hub",
        database_url_direct="postgresql+psycopg://owner:secret@host:5432/hub",
        eval_database_url="postgresql+psycopg://app:pw@host:5432/hub_eval",
    )
    assert settings.eval_database_url_for_migrations.get_secret_value() == (
        "postgresql+psycopg://owner:secret@host:5432/hub_eval"
    )


def test_the_eval_migrator_url_wins_when_set() -> None:
    settings = Settings(
        _env_file=None,
        eval_database_url="postgresql+psycopg://app@host/hub_eval",
        eval_database_url_direct="postgresql+psycopg://owner@other/hub_eval",
    )
    assert settings.eval_database_url_for_migrations.get_secret_value() == (
        "postgresql+psycopg://owner@other/hub_eval"
    )


def test_no_eval_database_means_nothing_to_migrate() -> None:
    assert Settings(_env_file=None).eval_database_url_for_migrations.get_secret_value() == ""


def test_database_urls_are_hidden_in_repr() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://hub:pw-placeholder@db/hub",
        eval_database_url="postgresql+psycopg://hub:pw-placeholder@db/eval",
    )
    assert "pw-placeholder" not in repr(settings)


def test_settings_are_frozen() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        settings.app_env = "demo"  # type: ignore[misc]


def _user(url: str) -> str | None:
    return make_url(url).username


def test_default_app_url_is_the_app_role_not_the_migrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Least privilege (security §4): without DATABASE_URL the app must not run as the owner.
    for name in ("DATABASE_URL", "DATABASE_URL_DIRECT", "EVAL_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    migrator = _user(Settings(_env_file=ENV_EXAMPLE).database_url_direct.get_secret_value())
    assert _user(settings.database_url.get_secret_value()) == "hub_api"
    assert _user(settings.database_url.get_secret_value()) != migrator


def test_env_example_separates_app_and_migrator_users(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "DATABASE_URL_DIRECT", "EVAL_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=ENV_EXAMPLE)
    assert _user(settings.database_url.get_secret_value()) == "hub_api"
    assert _user(settings.eval_database_url.get_secret_value()) == "hub_api"
    assert _user(settings.database_url_direct.get_secret_value()) == "hub"


def test_an_empty_eval_database_url_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # deploy.yml passes `EVAL_DATABASE_URL=` to Cloud Run (P10-07 review): the demo has no
    # evaluation database, and the empty value must mean exactly that, not a broken URL.
    monkeypatch.delenv("EVAL_DATABASE_URL", raising=False)
    monkeypatch.delenv("EVAL_DATABASE_URL_DIRECT", raising=False)
    unset = Settings(_env_file=None)
    monkeypatch.setenv("EVAL_DATABASE_URL", "")
    empty = Settings(_env_file=None)
    assert empty.eval_database_url.get_secret_value() == ""
    assert empty.eval_database_url == unset.eval_database_url
    assert empty.eval_database_url_for_migrations.get_secret_value() == ""
