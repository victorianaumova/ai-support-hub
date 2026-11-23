"""`make seed`: load the demo customers, operators and default settings (docs/operations.md §1).

Idempotent, in one transaction:
- customers are upserted by `external_ref` (the Hub mirrors the core mock), except anonymised
  rows: an erasure must survive a later seed;
- operators and settings are inserted only when missing, so a re-run never resets an operator's
  password or a value a lead changed in the console. The one exception is asked for by hand:
  `--reset-operator-passwords` sets every demo operator whose password is not
  `SEED_OPERATOR_PASSWORD` back to it, ends their sessions and lifts a lockout (P10-07: the
  first deploy seeded a password that ended in a newline).

Cards and transactions are not loaded: the core mock reads them from `data/seed/` (P7).
Every row written gets an audit record; a run that changes nothing writes none.
Output names only counts, never customer data.
"""

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hub.audit import record
from hub.auth.passwords import hash_password, verify_password
from hub.config import Settings, SettingsError, load_settings
from hub.db.models import Customer, Operator, Setting
from hub.db.repositories import CustomerRepository, OperatorRepository
from hub.db.session import connect
from hub.domain.enums import ActorType, SettingKey
from hub.domain.kb import ERROR_CODES_FILE, parse_error_codes
from hub.domain.seed import SeedData, parse_seed, validate_seed
from hub.guardrails.policy import Policy, PolicyError, load_policy

ACTOR = "make-seed"
SEED_DIR = Path("data/seed")
SEED_FILES = ("customers.json", "cards.json", "transactions.json", "operators.json")

# Until a confidence model is trained (never in budget mode, ADR-0018) auto-reply starts off and
# the threshold is 0.95 (docs/ai-pipeline.md §6).
DEFAULT_CONFIDENCE_THRESHOLD = 0.95


def default_settings(daily_budget_usd: float, policy: Policy) -> dict[SettingKey, Any]:
    """The first value of every setting. The intent allow-list is the `auto_reply_allowlist` of
    `config/policy.yaml`, so the two cannot drift (P6-02)."""
    return {
        SettingKey.AUTO_REPLY_ENABLED: False,
        SettingKey.CONFIDENCE_THRESHOLD: DEFAULT_CONFIDENCE_THRESHOLD,
        SettingKey.DAILY_BUDGET_USD: daily_budget_usd,
        SettingKey.INTENT_ALLOWLIST: list(policy.auto_reply_allowlist.intents),
    }


@dataclass
class SeedReport:
    customers_added: int = 0
    customers_updated: int = 0
    customers_unchanged: int = 0
    operators_added: int = 0
    operators_reset: int = 0
    settings_added: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.customers_added
            or self.customers_updated
            or self.operators_added
            or self.operators_reset
            or self.settings_added
        )


class SeedError(Exception):
    """The seed cannot run: invalid files or a missing setting. Nothing was written."""


def read_seed(seed_dir: Path, error_codes: Path | None = None) -> SeedData:
    """Read, parse and cross-check the seed files. Raises SeedError on any problem.

    Error codes in the seed are checked against `error_codes.csv`, by default the one in the
    `kb` folder next to `seed_dir` (`data/kb/`).
    """
    codes = read_error_codes(error_codes or seed_dir.parent / "kb" / ERROR_CODES_FILE)
    try:
        files = {name: (seed_dir / name).read_bytes() for name in SEED_FILES}
    except OSError as error:
        raise SeedError(f"cannot read the seed files in {seed_dir}: {error.strerror}") from error
    try:
        seed = parse_seed(files)
    except ValueError as error:  # pydantic.ValidationError is a ValueError
        raise SeedError("the seed files are invalid; run `make data-check`") from error
    if validate_seed(seed, codes):
        raise SeedError("the seed files are inconsistent; run `make data-check`")
    return seed


def read_error_codes(path: Path) -> frozenset[str]:
    """The codes of `error_codes.csv`. Raises SeedError if the file is missing or invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SeedError(f"cannot read the error codes in {path}: {error.strerror}") from error
    try:
        return frozenset(row.code for row in parse_error_codes(text))
    except ValueError as error:
        raise SeedError(f"{path} is invalid; run `make data-check`") from error


async def load_seed(
    session: AsyncSession,
    seed: SeedData,
    *,
    operator_password: str,
    daily_budget_usd: float,
    policy: Policy,
    reset_operator_passwords: bool = False,
) -> SeedReport:
    """Write the seed inside the caller's transaction (the caller commits)."""
    if not operator_password:
        raise SeedError(
            "SEED_OPERATOR_PASSWORD is empty. Set it in .env (the password of the demo "
            "operators), then run `make seed` (or `make dev`) again. Nothing was written."
        )
    report = SeedReport()
    await _load_customers(session, seed, report)
    await _load_operators(session, seed, operator_password, report)
    if reset_operator_passwords:
        await _reset_operator_passwords(session, seed, operator_password, report)
    await _load_settings(session, default_settings(daily_budget_usd, policy), report)
    return report


async def load_customers(session: AsyncSession, seed: SeedData) -> SeedReport:
    """Only the customers, inside the caller's transaction: the evaluation runner fills its own
    database with them (P4-03); it needs no operators and keeps the default settings."""
    report = SeedReport()
    await _load_customers(session, seed, report)
    return report


async def _load_customers(session: AsyncSession, seed: SeedData, report: SeedReport) -> None:
    customers = CustomerRepository(session)
    for item in seed.customers:
        fields = {
            "display_name": item.display_name,
            "email": item.email,
            "tier": item.tier,
            "country": item.country,
        }
        row = await customers.get_by_external_ref(item.external_ref)
        if row is None:
            row = await customers.add(Customer(external_ref=item.external_ref, **fields))
            await _audit(session, "seed.customer_added", "customer", row)
            report.customers_added += 1
            continue
        changed = [name for name, value in fields.items() if getattr(row, name) != value]
        if row.anonymised_at is not None or not changed:
            report.customers_unchanged += 1
            continue
        for name in changed:
            setattr(row, name, fields[name])
        await session.flush()
        # Field names only: the audit log holds no names or emails (data model, audit_log).
        await _audit(session, "seed.customer_updated", "customer", row, {"fields": changed})
        report.customers_updated += 1


async def _load_operators(
    session: AsyncSession, seed: SeedData, password: str, report: SeedReport
) -> None:
    operators = OperatorRepository(session)
    for item in seed.operators:
        if await operators.get_by_email(item.email) is not None:
            continue
        row = await operators.add(
            Operator(
                email=item.email,
                name=item.name,
                role=item.role,
                password_hash=hash_password(password),
            )
        )
        await _audit(session, "seed.operator_added", "operator", row, {"role": str(item.role)})
        report.operators_added += 1


async def _reset_operator_passwords(
    session: AsyncSession, seed: SeedData, password: str, report: SeedReport
) -> None:
    """Demo operators only (the seed file's emails); one whose password already matches is left
    alone, so a second reset writes nothing."""
    operators = OperatorRepository(session)
    for item in seed.operators:
        row = await operators.get_by_email(item.email, for_update=True)
        if row is None or verify_password(row.password_hash, password):
            continue
        await operators.change_password(row, hash_password(password))  # ends their sessions
        row.failed_logins = 0
        row.locked_until = None
        await session.flush()
        await _audit(session, "seed.operator_password_reset", "operator", row)
        report.operators_reset += 1


async def _load_settings(
    session: AsyncSession, defaults: dict[SettingKey, Any], report: SeedReport
) -> None:
    for key, value in defaults.items():
        if await session.get(Setting, key) is not None:
            continue
        session.add(Setting(key=key, value=value))
        await session.flush()
        await record(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR,
            action="settings.changed",
            entity_type="setting",
            entity_id=str(key),
            details={"key": str(key), "old": None, "new": value},
        )
        report.settings_added.append(str(key))


async def _audit(
    session: AsyncSession,
    action: str,
    entity_type: str,
    row: Customer | Operator,
    details: dict[str, Any] | None = None,
) -> None:
    await record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=ACTOR,
        action=action,
        entity_type=entity_type,
        entity_id=row.id,
        details=details,
    )


async def run(
    settings: Settings, seed_dir: Path, *, reset_operator_passwords: bool = False
) -> SeedReport:
    seed = read_seed(seed_dir)
    try:
        policy = load_policy(settings.policy_config)
    except PolicyError as error:
        raise SeedError(str(error)) from error
    database = connect(settings.database_url.get_secret_value())
    try:
        async with database.sessions() as session, session.begin():
            return await load_seed(
                session,
                seed,
                operator_password=settings.seed_operator_password.get_secret_value(),
                daily_budget_usd=settings.daily_budget_usd,
                policy=policy,
                reset_operator_passwords=reset_operator_passwords,
            )
    finally:
        await database.dispose()


RESET_FLAG = "--reset-operator-passwords"


def main(argv: list[str]) -> int:
    """`python -m hub.seed [SEED_DIR] [--reset-operator-passwords]`."""
    unknown = [arg for arg in argv if arg.startswith("--") and arg != RESET_FLAG]
    if unknown:
        print(f"seed: unknown option {unknown[0]} (known: {RESET_FLAG})", file=sys.stderr)
        return 2
    paths = [arg for arg in argv if not arg.startswith("--")]
    seed_dir = Path(paths[0]) if paths else SEED_DIR
    try:
        report = asyncio.run(
            run(load_settings(), seed_dir, reset_operator_passwords=RESET_FLAG in argv)
        )
    except (SeedError, SettingsError) as error:
        print(f"seed: {error}", file=sys.stderr)
        return 1
    print(
        f"seed: customers +{report.customers_added} ~{report.customers_updated} "
        f"={report.customers_unchanged}; operators +{report.operators_added} "
        f"reset {report.operators_reset}; "
        f"settings +{len(report.settings_added)}" + ("" if report.changed else " (nothing to do)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
