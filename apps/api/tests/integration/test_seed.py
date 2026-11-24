"""`make seed` against Postgres as the app role: idempotent, audited, keeps changed values.

Uses a database of its own (the seed commits, and `verify_chain` needs an audit log that holds
only chained rows; the shared `hub` database has hand-made rows from test_db_roles.py).
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from hub.audit import verify_chain
from hub.auth.passwords import verify_password
from hub.db.models import AuditLog, Customer, Operator, Setting
from hub.domain.enums import SettingKey
from hub.domain.seed import SeedData
from hub.guardrails.policy import load_policy
from hub.observability.readiness import libpq_url
from hub.seed import SeedError, SeedReport, default_settings, load_seed, read_seed

from ._compose import ROOT
from ._db import app_url, as_user, migrate

POLICY = load_policy(ROOT / "config" / "policy.yaml")

DATABASE = "hub_seed_test"
PASSWORD = "demo-password-for-tests"  # noqa: S105 - test value, not a real credential
BUDGET = 3.0


@pytest.fixture(scope="module")
def seed_db(postgres_url: str) -> Iterator[tuple[str, str]]:
    """(migrator URL, app URL) of a migrated database of its own."""
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE)))
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    migrator = as_user(postgres_url, "hub", "hub", database=DATABASE)
    migrate(migrator)
    yield migrator, app_url(postgres_url, database=DATABASE)
    with psycopg.connect(libpq_url(postgres_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(DATABASE))
        )


@pytest.fixture(autouse=True)
def empty_tables(seed_db: tuple[str, str]) -> None:
    with psycopg.connect(libpq_url(seed_db[0]), autocommit=True) as connection:
        connection.execute(
            "TRUNCATE audit_log, settings, operators, customers RESTART IDENTITY CASCADE"
        )


@pytest.fixture
async def engine(seed_db: tuple[str, str]) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(seed_db[1])
    yield engine
    await engine.dispose()


@pytest.fixture(scope="module")
def seed() -> SeedData:
    return read_seed(ROOT / "data" / "seed")


async def _load(
    engine: AsyncEngine, seed: SeedData, password: str = PASSWORD, *, reset: bool = False
) -> SeedReport:
    async with AsyncSession(engine, expire_on_commit=False) as session, session.begin():
        return await load_seed(
            session,
            seed,
            operator_password=password,
            daily_budget_usd=BUDGET,
            policy=POLICY,
            reset_operator_passwords=reset,
        )


async def _count(engine: AsyncEngine, model: type[object]) -> int:
    async with AsyncSession(engine) as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def test_first_load_writes_customers_operators_settings_and_audit(
    engine: AsyncEngine, seed: SeedData
) -> None:
    report = await _load(engine, seed)
    assert report.customers_added == len(seed.customers) == 40
    assert report.operators_added == len(seed.operators)
    assert sorted(report.settings_added) == sorted(str(k) for k in SettingKey)

    async with AsyncSession(engine) as session:
        settings = {s.key: s.value for s in (await session.scalars(select(Setting))).all()}
        operators = (await session.scalars(select(Operator))).all()
        actions = (await session.scalars(select(AuditLog.action))).all()
    assert settings == default_settings(BUDGET, POLICY)
    assert settings[SettingKey.AUTO_REPLY_ENABLED] is False
    assert all(verify_password(o.password_hash, PASSWORD) for o in operators)
    assert all(PASSWORD not in o.password_hash for o in operators)
    assert actions.count("seed.customer_added") == 40
    assert actions.count("seed.operator_added") == len(seed.operators)
    assert actions.count("settings.changed") == len(SettingKey)

    async with AsyncSession(engine) as session:
        assert (await verify_chain(session)).ok


async def test_a_second_load_changes_nothing(engine: AsyncEngine, seed: SeedData) -> None:
    await _load(engine, seed)
    audit_rows = await _count(engine, AuditLog)
    async with AsyncSession(engine) as session:
        before = [
            (c.id, c.external_ref, c.display_name, c.email, c.tier, c.country)
            for c in (await session.scalars(select(Customer).order_by(Customer.external_ref)))
        ]
        hashes = (
            await session.scalars(select(Operator.password_hash).order_by(Operator.email))
        ).all()

    report = await _load(engine, seed)

    assert report == SeedReport(customers_unchanged=40)
    assert not report.changed
    assert await _count(engine, AuditLog) == audit_rows
    async with AsyncSession(engine) as session:
        after = [
            (c.id, c.external_ref, c.display_name, c.email, c.tier, c.country)
            for c in (await session.scalars(select(Customer).order_by(Customer.external_ref)))
        ]
        assert (
            await session.scalars(select(Operator.password_hash).order_by(Operator.email))
        ).all() == hashes
    assert after == before


async def test_a_changed_customer_is_updated_and_audited_without_personal_data(
    engine: AsyncEngine, seed: SeedData
) -> None:
    await _load(engine, seed)
    first = seed.customers[0]
    changed = seed.model_copy(
        update={
            "customers": [
                first.model_copy(update={"country": "PT", "display_name": "New Name"}),
                *seed.customers[1:],
            ]
        }
    )

    report = await _load(engine, changed)

    assert report.customers_updated == 1
    async with AsyncSession(engine) as session:
        row = await session.scalar(
            select(Customer).where(Customer.external_ref == first.external_ref)
        )
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "seed.customer_updated")
        )
        assert (await verify_chain(session)).ok
    assert row is not None
    assert (row.country, row.display_name) == ("PT", "New Name")
    assert audit is not None
    assert audit.entity_id == str(row.id)
    assert audit.details == {"fields": ["display_name", "country"]}
    assert "New Name" not in str(audit.details)


async def test_an_anonymised_customer_is_not_restored(engine: AsyncEngine, seed: SeedData) -> None:
    await _load(engine, seed)
    ref = seed.customers[0].external_ref
    async with AsyncSession(engine) as session, session.begin():
        row = await session.scalar(select(Customer).where(Customer.external_ref == ref))
        assert row is not None
        row.display_name, row.email = "anonymised", "anonymised@invalid"
        row.anonymised_at = datetime.now(UTC)

    report = await _load(engine, seed)

    assert report.customers_updated == 0
    async with AsyncSession(engine) as session:
        row = await session.scalar(select(Customer).where(Customer.external_ref == ref))
    assert row is not None
    assert row.display_name == "anonymised"


async def test_existing_settings_and_passwords_are_kept(
    engine: AsyncEngine, seed: SeedData
) -> None:
    await _load(engine, seed)
    async with AsyncSession(engine) as session, session.begin():
        setting = await session.get(Setting, SettingKey.CONFIDENCE_THRESHOLD)
        assert setting is not None
        setting.value = 0.8

    report = await _load(engine, seed, password="another-password")  # noqa: S106 - test value

    assert report.settings_added == []
    assert report.operators_added == 0
    async with AsyncSession(engine) as session:
        setting = await session.get(Setting, SettingKey.CONFIDENCE_THRESHOLD)
        operator = await session.scalar(select(Operator).limit(1))
    assert setting is not None
    assert setting.value == 0.8
    assert operator is not None
    assert verify_password(operator.password_hash, PASSWORD)


async def test_a_reset_sets_the_demo_passwords_ends_sessions_and_lifts_the_lockout(
    engine: AsyncEngine, seed: SeedData
) -> None:
    # The first deploy seeded the password with a trailing newline (P10-07).
    await _load(engine, seed, password=PASSWORD + "\n")
    async with AsyncSession(engine) as session, session.begin():
        locked = await session.scalar(select(Operator).limit(1))
        assert locked is not None
        locked.failed_logins = 5
        locked.locked_until = datetime.now(UTC)
    audit_before = await _count(engine, AuditLog)

    report = await _load(engine, seed, reset=True)

    assert report.operators_reset == len(seed.operators)
    async with AsyncSession(engine) as session:
        operators = list(await session.scalars(select(Operator)))
        actions = list(
            await session.scalars(
                select(AuditLog.action).where(AuditLog.action == "seed.operator_password_reset")
            )
        )
    assert all(verify_password(row.password_hash, PASSWORD) for row in operators)
    assert all(row.session_version == 2 for row in operators)
    assert all(row.failed_logins == 0 and row.locked_until is None for row in operators)
    assert len(actions) == len(seed.operators)
    assert await _count(engine, AuditLog) == audit_before + len(seed.operators)
    async with AsyncSession(engine) as session:
        assert (await verify_chain(session)).ok

    again = await _load(engine, seed, reset=True)
    assert again.operators_reset == 0
    assert not again.changed


async def test_an_empty_password_is_refused_and_nothing_is_written(
    engine: AsyncEngine, seed: SeedData
) -> None:
    with pytest.raises(SeedError, match="SEED_OPERATOR_PASSWORD"):
        await _load(engine, seed, password="")
    assert await _count(engine, Customer) == 0
    assert await _count(engine, AuditLog) == 0
