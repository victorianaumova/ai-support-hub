"""All settings of the Hub, read from the environment and `.env` (docs/operations.md §2).

No other module reads environment variables. One module sets some: `hub/llm/gateway.py` sets
LiteLLM's switches (`LITELLM_SWITCHES`) before it imports LiteLLM, so LiteLLM loads no `.env`
and fetches nothing from the network; no Hub setting is read from them.

An unknown name in `.env` (a typo such as `DATBASE_URL`) stops the app at start, so a setting
is never silently left at its default.
Process environment variables have no common prefix, so pydantic-settings cannot tell an
unknown one from `PATH` and ignores it; only `.env` is checked for unknown names.
"""

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        # `KEY=` in .env means "not set": the default applies (e.g. CORE_ANCHOR_DATE= → None).
        env_ignore_empty=True,
        frozen=True,
    )

    app_env: Literal["local", "test", "demo"] = "local"
    log_level: LogLevel = "INFO"

    # Secret: the URLs carry the database password.
    # The default is the local app role (hub_api, member of hub_app), never the schema owner:
    # a missing DATABASE_URL must not silently give the app UPDATE and DELETE on audit_log
    # (security §4). Migrations then fail loudly until DATABASE_URL_DIRECT names the owner.
    database_url: SecretStr = SecretStr("postgresql+psycopg://hub_api:hub_api@localhost:5432/hub")
    database_url_direct: SecretStr = SecretStr("")
    eval_database_url: SecretStr = SecretStr("")
    # Migrator of the evaluation database (`make migrate`); empty = DATABASE_URL_DIRECT with the
    # database name of EVAL_DATABASE_URL (the local stack has one server for both).
    eval_database_url_direct: SecretStr = SecretStr("")

    core_anchor_date: date | None = None
    jobs_token: SecretStr = SecretStr("")
    # Storage limitation (security §6, NFR-13; P8-05): the `retention` job deletes tickets with
    # their messages, drafts and runs after this many days without activity, and the checkpoints
    # of a closed ticket this many days after the close.
    retention_ticket_days: int = Field(default=730, ge=1)
    retention_checkpoint_days: int = Field(default=7, ge=0)

    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr = SecretStr("")
    # Folder of the local embedding model files (`make models` downloads them; ADR-0018). The
    # app only reads it: a missing model is an error, never a download.
    embedding_cache_dir: Path = Path(".cache/models")

    anthropic_api_key: SecretStr = SecretStr("")
    llm_mode: Literal["real", "fake"] = "fake"
    models_config: Path = Path("config/models.yaml")
    # Prompt files `prompts/<node>/v<N>.md`; the active versions are named in models.yaml.
    prompts_dir: Path = Path("prompts")
    # Routing policy and the rule version of confidence (ai-pipeline §6, P6-02).
    policy_config: Path = Path("config/policy.yaml")
    confidence_config: Path = Path("config/confidence.json")

    mcp_server_url: str = "http://localhost:8001/mcp"
    mcp_server_token: SecretStr = SecretStr("")

    # Jira and Slack are mocks inside the MCP server; there is no real mode (ADR-0019, P7-02).
    jira_mode: Literal["mock"] = "mock"
    slack_mode: Literal["mock"] = "mock"

    session_secret: SecretStr = SecretStr("")
    demo_access_code: SecretStr = SecretStr("")
    seed_operator_password: SecretStr = SecretStr("")
    # The chat's customer picker: which demo customers, a hint and first messages (P10-02).
    demo_customers_file: Path = Path("data/seed/demo_customers.json")

    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = ""

    daily_budget_usd: float = Field(default=0.5, ge=0)
    eval_max_cost_usd: float = Field(default=1.0, ge=0)
    # Committed evaluation runs (`metrics.json`, `config.json`); read by `/console/evals/latest`.
    eval_runs_dir: Path = Path("evals/runs")
    eval_trace_sample: float = Field(default=0.2, ge=0, le=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def database_url_for_migrations(self) -> SecretStr:
        """Direct (not pooled) connection; falls back to the app connection when not set."""
        return (
            self.database_url_direct
            if self.database_url_direct.get_secret_value()
            else self.database_url
        )

    @property
    def eval_database_url_for_migrations(self) -> SecretStr:
        """The migrator connection of the evaluation database: EVAL_DATABASE_URL_DIRECT, else
        the `hub` migrator (`database_url_for_migrations`) pointed at the database of
        EVAL_DATABASE_URL. Empty while EVAL_DATABASE_URL is empty: there is nothing to
        migrate."""
        if self.eval_database_url_direct.get_secret_value():
            return self.eval_database_url_direct
        eval_url = self.eval_database_url.get_secret_value()
        if not eval_url:
            return SecretStr("")
        database = make_url(eval_url).database
        migrator = make_url(self.database_url_for_migrations.get_secret_value())
        return SecretStr(migrator.set(database=database).render_as_string(hide_password=False))


class SettingsError(Exception):
    """Settings could not be loaded. Names the variables, never their values."""


def load_settings() -> Settings:
    """Read the settings at start. A wrong value may be a secret under a misspelled name,
    so the error names each variable and the problem, and drops pydantic's `input_value`."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise SettingsError("invalid settings: " + "; ".join(problems)) from None
