"""Schemas and checks for the demo data in `data/seed/` (docs/data-model.md §3).

Four files: `customers.json`, `cards.json`, `transactions.json`, `operators.json`. Dates are
offsets from an anchor date (`"-3d 10:15"` = three days before the anchor, 10:15 UTC), so the
demo always has "a withdrawal from three days ago" and a fixed `CORE_ANCHOR_DATE` gives
repeatable dates.

`support_scenarios` on a customer is test-data metadata: it says which intents the customer's
data fits, so `make data-check` can prove that every intent has customers. It must never reach a
model — no tool result and no prompt may contain it, because it would leak the expected intent
into evaluation (data model §3).

Pure code: no I/O, no imports from the rest of the project.
"""

import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from hub.domain.enums import CustomerTier, Intent, OperatorRole

MIN_CUSTOMERS_PER_INTENT = 3
CUSTOMER_COUNT = 40
MIN_TRANSACTIONS_PER_CUSTOMER = 5
MAX_TRANSACTIONS_PER_CUSTOMER = 40

_OFFSET = re.compile(r"^(?P<days>[+-][1-9]\d{0,3}|0)d (?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")


def parse_offset(offset: str) -> tuple[int, time]:
    """Split `"-3d 10:15"` into (-3, 10:15). Raises ValueError on any other shape."""
    match = _OFFSET.fullmatch(offset)
    if match is None:
        raise ValueError(f"offset {offset!r} is not like '-3d 10:15', '0d 09:00' or '+2d 15:00'")
    return int(match["days"]), time(int(match["hour"]), int(match["minute"]))


def resolve_offset(offset: str, anchor: date) -> datetime:
    """The UTC datetime of an offset: `anchor + days` at the given time of day."""
    days, at = parse_offset(offset)
    return datetime.combine(anchor + timedelta(days=days), at, tzinfo=UTC)


# Every date in the seed is an event that has happened: none lies on the anchor day or later,
# because with an empty `CORE_ANCHOR_DATE` (= today) a "0d 14:20" event is in the future
# during the morning (owner decision, P2-05). Expected dates (`expected_at`) are computed by the
# core mock (P7), not stored, and are the only dates that may be in the future.
LATEST_EVENT_DAY = -1


# The anchor day starts at 00:00 CET, which is 22:00 UTC the day before in summer (CEST). A
# "-1d 22:30" event would be on the anchor day in CET, so the last day ends earlier.
LAST_DAY_ENDS_UTC = time(22, 0)


def _check_past_offset(value: str) -> str:
    days, at = parse_offset(value)
    if days > LATEST_EVENT_DAY or (days == LATEST_EVENT_DAY and at >= LAST_DAY_ENDS_UTC):
        raise ValueError(
            f"offset {value!r} is not in the past: "
            f"seed events are before {LATEST_EVENT_DAY}d 22:00 (UTC), the start of the "
            "anchor day in CET"
        )
    return value


# A date in a seed file: the shape of `parse_offset`, and in the past.
Offset = Annotated[str, AfterValidator(_check_past_offset)]
CustomerRef = Annotated[str, Field(pattern=r"^CUS-\d{4}$")]
CardRef = Annotated[str, Field(pattern=r"^CRD-\d{4}$")]
TransactionRef = Annotated[str, Field(pattern=r"^TX-\d{6}$")]
ErrorCode = Annotated[str, Field(pattern=r"^ERR-[1-5]\d{3}$")]
Amount = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Balance = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
Text = Annotated[str, Field(min_length=1, max_length=200)]

HELD_FOR_REVIEW_CODE = "ERR-1012"
RESTRICTED_CODE = "ERR-5010"

# Limits per tier in EUR (world file §3).
DAILY_WITHDRAW_LIMIT_EUR = {
    CustomerTier.STARTER: Decimal(500),
    CustomerTier.VERIFIED: Decimal(10_000),
    CustomerTier.BUSINESS: Decimal(50_000),
}
# Cards per tier (physical, virtual), counted without cancelled cards.
CARD_ALLOWANCE = {
    CustomerTier.STARTER: (0, 0),
    CustomerTier.VERIFIED: (1, 3),
    CustomerTier.BUSINESS: (5, 20),
}
DISPUTE_WINDOW_DAYS = 120
# Fixed fees, set in EUR (world file §4). On a USD or GBP transaction they are still EUR amounts
# (charged from that balance at the mid-market rate, without a conversion fee), so the seed
# records them with `fee_currency: "EUR"`.
FIXED_FEES_EUR = {
    "withdrawal_sepa": Decimal("1.50"),
    "withdrawal_swift": Decimal("15.00"),
    "atm_withdrawal": Decimal("2.50"),
}
# A share of the daily withdrawal limit that counts as "nearly used".
NEARLY_USED_SHARE = Decimal("0.8")


class Currency(StrEnum):
    EUR = "EUR"
    USD = "USD"
    GBP = "GBP"


class _Seed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UpgradeStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"


class Upgrade(_Seed):
    """A verification for a higher tier (Starter → Verified, Verified → Business).

    Every seed customer has finished the checks of its current tier: an account without them
    cannot hold or move money (world file §3), and every customer has transactions.
    """

    target_tier: CustomerTier
    status: UpgradeStatus
    submitted_at: Offset
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def _rejection_has_code(self) -> "Upgrade":
        if (self.status is UpgradeStatus.REJECTED) != (self.error_code is not None):
            raise ValueError("a rejected upgrade needs an error_code; a pending one has none")
        if self.error_code is not None and not self.error_code.startswith("ERR-4"):
            raise ValueError("an upgrade error_code must be a verification code (ERR-4xxx)")
        return self


class Customer(_Seed):
    external_ref: CustomerRef
    display_name: Text
    email: Annotated[str, Field(pattern=r"^[a-z0-9.+-]+@example\.com$")]
    tier: CustomerTier
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    opened_at: Offset
    balances: dict[Currency, Balance]
    restricted: bool = False
    # Internal only: the core mock never returns it, and nobody tells the customer (tipping-off
    # rule, world file §6).
    restriction_reason: Text | None = None
    upgrade: Upgrade | None = None
    # Test-data metadata; never sent to a model (module docstring).
    support_scenarios: Annotated[list[Intent], Field(min_length=1)]

    @model_validator(mode="after")
    def _consistent(self) -> "Customer":
        if self.restricted != (self.restriction_reason is not None):
            raise ValueError("restriction_reason is required exactly when restricted is true")
        if self.restricted != (Intent.ACCOUNT_RESTRICTED in self.support_scenarios):
            raise ValueError("account_restricted scenario must match the restricted flag")
        if len(set(self.support_scenarios)) != len(self.support_scenarios):
            raise ValueError("support_scenarios has duplicates")
        if self.upgrade is not None and _tier_rank(self.upgrade.target_tier) <= _tier_rank(
            self.tier
        ):
            raise ValueError("upgrade.target_tier must be higher than the current tier")
        return self


def _tier_rank(tier: CustomerTier) -> int:
    return list(CustomerTier).index(tier)


class CardKind(StrEnum):
    VIRTUAL = "virtual"
    PHYSICAL = "physical"


class CardStatus(StrEnum):
    ACTIVE = "active"
    FROZEN = "frozen"
    PIN_BLOCKED = "pin_blocked"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    ORDERED = "ordered"
    SHIPPED = "shipped"
    DELIVERED = "delivered"


class Card(_Seed):
    """Only the last 4 digits: no tool returns more, so the seed holds no more (PCI scope)."""

    card_ref: CardRef
    customer_ref: CustomerRef
    kind: CardKind
    last4: Annotated[str, Field(pattern=r"^\d{4}$")]
    status: CardStatus
    issued_at: Offset
    delivery_status: DeliveryStatus | None = None
    delivery_method: Literal["standard", "express"] | None = None

    @model_validator(mode="after")
    def _delivery_only_for_physical(self) -> "Card":
        physical = self.kind is CardKind.PHYSICAL
        if physical != (self.delivery_status is not None and self.delivery_method is not None):
            raise ValueError("delivery_status and delivery_method are set exactly for physical")
        return self


class TransactionType(StrEnum):
    INCOMING_SEPA = "incoming_sepa"
    INCOMING_ACH = "incoming_ach"
    INCOMING_FPS = "incoming_fps"
    PAYMENT_REQUEST_CARD = "payment_request_card"
    PAYMENT_REQUEST_BANK = "payment_request_bank"
    WITHDRAWAL_SEPA = "withdrawal_sepa"
    WITHDRAWAL_SWIFT = "withdrawal_swift"
    CARD_PAYMENT = "card_payment"
    ATM_WITHDRAWAL = "atm_withdrawal"
    CONVERSION = "conversion"
    FEE = "fee"
    MASS_PAYOUT = "mass_payout"


INCOMING = frozenset(
    {
        TransactionType.INCOMING_SEPA,
        TransactionType.INCOMING_ACH,
        TransactionType.INCOMING_FPS,
        TransactionType.PAYMENT_REQUEST_CARD,
        TransactionType.PAYMENT_REQUEST_BANK,
    }
)
WITHDRAWALS = frozenset({TransactionType.WITHDRAWAL_SEPA, TransactionType.WITHDRAWAL_SWIFT})
CARD_USE = frozenset({TransactionType.CARD_PAYMENT, TransactionType.ATM_WITHDRAWAL})
# The currency a rail carries (world file §2); other types may use any currency.
RAIL_CURRENCY = {
    TransactionType.INCOMING_SEPA: Currency.EUR,
    TransactionType.INCOMING_ACH: Currency.USD,
    TransactionType.INCOMING_FPS: Currency.GBP,
    TransactionType.WITHDRAWAL_SEPA: Currency.EUR,
}
# Which error-code ranges fit which transaction types (world file §7).
ERROR_RANGES: dict[frozenset[TransactionType], tuple[str, ...]] = {
    INCOMING: ("ERR-1",),
    WITHDRAWALS | {TransactionType.MASS_PAYOUT}: ("ERR-2", RESTRICTED_CODE),
    CARD_USE: ("ERR-3", RESTRICTED_CODE),
}


class TransactionStatus(StrEnum):
    PENDING = "pending"
    HELD_FOR_REVIEW = "held_for_review"
    COMPLETED = "completed"
    FAILED = "failed"
    # A failed withdrawal whose money came back to the balance.
    RETURNED = "returned"


class StatusChange(_Seed):
    status: TransactionStatus
    at: Offset


class Transaction(_Seed):
    reference: TransactionRef
    customer_ref: CustomerRef
    type: TransactionType
    amount: Amount
    currency: Currency
    fee: Balance = Decimal(0)
    # The currency of `fee`; empty means the transaction's currency. Fixed fees are EUR.
    fee_currency: Currency | None = None
    status: TransactionStatus
    error_code: ErrorCode | None = None
    requested_at: Offset
    status_history: Annotated[list[StatusChange], Field(min_length=1)]
    counterparty: Text
    card_ref: CardRef | None = None
    merchant_category: Text | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "Transaction":
        errors = transaction_errors(self)
        if errors:
            raise ValueError("; ".join(errors))
        return self


def transaction_errors(tx: Transaction) -> list[str]:
    """Rules inside one transaction (rules across files are in `validate_seed`)."""
    errors: list[str] = []
    history = tx.status_history
    if history[0].at != tx.requested_at:
        errors.append("status_history must start at requested_at")
    if history[-1].status is not tx.status:
        errors.append("the last status_history entry must equal status")
    instants = [_sort_key(change.at) for change in history]
    if instants != sorted(instants):
        errors.append("status_history must be in time order")
    errors.extend(_history_errors(tx))
    needs_code = tx.status in {
        TransactionStatus.FAILED,
        TransactionStatus.RETURNED,
        TransactionStatus.HELD_FOR_REVIEW,
    }
    if needs_code != (tx.error_code is not None):
        errors.append("error_code is required exactly for failed, returned and held_for_review")
    if tx.status is TransactionStatus.HELD_FOR_REVIEW and tx.error_code != HELD_FOR_REVIEW_CODE:
        errors.append(f"held_for_review uses {HELD_FOR_REVIEW_CODE}")
    if tx.status is TransactionStatus.RETURNED and tx.type not in WITHDRAWALS:
        errors.append("only withdrawals can be returned")
    if tx.error_code is not None:
        allowed = next((r for types, r in ERROR_RANGES.items() if tx.type in types), ())
        if not tx.error_code.startswith(allowed):
            errors.append(f"{tx.error_code} does not fit a {tx.type} transaction")
    if (tx.type in CARD_USE) != (tx.card_ref is not None):
        errors.append("card_ref is set exactly for card payments and ATM withdrawals")
    if (tx.type is TransactionType.CARD_PAYMENT) != (tx.merchant_category is not None):
        errors.append("merchant_category is set exactly for card payments")
    if tx.type is TransactionType.WITHDRAWAL_SEPA and _in_cutoff_gap(tx.requested_at):
        errors.append(
            "a SEPA withdrawal requested 13:00-13:59 UTC is before 15:00 CET in winter but after "
            "15:00 CEST in summer; pick a time that is on one side of the cut-off all year"
        )
    errors.extend(_fee_errors(tx))
    rail = RAIL_CURRENCY.get(tx.type)
    if rail is not None and tx.currency is not rail:
        errors.append(f"{tx.type} carries {rail}, not {tx.currency}")
    return errors


# The status a transaction can start with and the steps it can take (data model §3). A
# transaction is created `pending`, or already `completed` (an instant transfer, a card payment
# that settled at once) or `failed` (refused on request). `completed` and `returned` are final;
# only a failed withdrawal comes back (`returned`, checked above).
FIRST_STATUSES = frozenset(
    {TransactionStatus.PENDING, TransactionStatus.COMPLETED, TransactionStatus.FAILED}
)
STATUS_STEPS: dict[TransactionStatus, frozenset[TransactionStatus]] = {
    TransactionStatus.PENDING: frozenset(
        {
            TransactionStatus.HELD_FOR_REVIEW,
            TransactionStatus.COMPLETED,
            TransactionStatus.FAILED,
        }
    ),
    TransactionStatus.HELD_FOR_REVIEW: frozenset(
        {TransactionStatus.COMPLETED, TransactionStatus.FAILED}
    ),
    TransactionStatus.FAILED: frozenset({TransactionStatus.RETURNED}),
    TransactionStatus.COMPLETED: frozenset(),
    TransactionStatus.RETURNED: frozenset(),
}


def _history_errors(tx: Transaction) -> list[str]:
    """Every step of `status_history`, not only its ends (P8-01, deferred from P2-05)."""
    statuses = [change.status for change in tx.status_history]
    errors: list[str] = []
    if statuses[0] not in FIRST_STATUSES:
        errors.append(f"status_history cannot start with {statuses[0]}")
    errors.extend(
        f"status_history step {before} -> {after} is not allowed"
        for before, after in pairwise(statuses)
        if after not in STATUS_STEPS[before]
    )
    return errors


def _fee_errors(tx: Transaction) -> list[str]:
    """Fee rules of world file §4 that a single transaction can break."""
    errors: list[str] = []
    fee_currency = tx.fee_currency or tx.currency
    if tx.fee_currency is not None and tx.fee == 0:
        errors.append("fee_currency is set only when there is a fee")
    fixed = FIXED_FEES_EUR.get(tx.type)
    if fixed is not None and tx.fee > 0 and (tx.fee != fixed or fee_currency is not Currency.EUR):
        errors.append(f"the {tx.type} fee is EUR {fixed}, set in EUR (fee_currency EUR)")
    if tx.type is TransactionType.FEE and tx.currency is not Currency.EUR:
        errors.append("a fee transaction is a fixed fee, set in EUR")
    if (
        tx.fee > 0
        and tx.type in WITHDRAWALS
        and tx.status
        in {
            TransactionStatus.FAILED,
            TransactionStatus.RETURNED,
        }
    ):
        errors.append("a failed or returned withdrawal has no fee")
    if tx.fee > 0 and tx.type is TransactionType.MASS_PAYOUT:
        errors.append("mass payouts have no fee (Business benefit)")
    return errors


# SEPA withdrawals requested before 15:00 CET arrive the next business day (world file §5).
# 15:00 CET is 14:00 UTC in winter and 13:00 UTC in summer (CEST), so a UTC time in between
# lands on different sides of the cut-off depending on the anchor date.
SEPA_CUTOFF_GAP_UTC = (time(13, 0), time(14, 0))


def _in_cutoff_gap(offset: str) -> bool:
    start, end = SEPA_CUTOFF_GAP_UTC
    return start <= parse_offset(offset)[1] < end


def _sort_key(offset: str) -> tuple[int, time]:
    return parse_offset(offset)


class Operator(_Seed):
    """A demo operator. The password comes from `SEED_OPERATOR_PASSWORD`, never from the file."""

    email: Annotated[str, Field(pattern=r"^[a-z0-9.+-]+@tallybridge\.example$")]
    name: Text
    role: OperatorRole


class SeedData(_Seed):
    customers: list[Customer]
    cards: list[Card]
    transactions: list[Transaction]
    operators: list[Operator]


CUSTOMERS = TypeAdapter(list[Customer])
CARDS = TypeAdapter(list[Card])
TRANSACTIONS = TypeAdapter(list[Transaction])
OPERATORS = TypeAdapter(list[Operator])


def parse_seed(files: dict[str, bytes]) -> SeedData:
    """Parse the four files (name → raw JSON). Raises `pydantic.ValidationError`."""
    return SeedData(
        customers=CUSTOMERS.validate_json(files["customers.json"]),
        cards=CARDS.validate_json(files["cards.json"]),
        transactions=TRANSACTIONS.validate_json(files["transactions.json"]),
        operators=OPERATORS.validate_json(files["operators.json"]),
    )


def _duplicates(values: list[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def validate_seed(seed: SeedData, known_error_codes: frozenset[str]) -> list[str]:
    """Rules across the files. Returns one message per problem; empty means valid.

    `known_error_codes` is the full list from `data/kb/error_codes.csv` (world file §7); the
    caller reads it, so this module stays free of I/O.
    """
    errors: list[str] = []
    customers = {c.external_ref: c for c in seed.customers}
    cards = {c.card_ref: c for c in seed.cards}

    for name, refs in (
        ("customer external_ref", [c.external_ref for c in seed.customers]),
        ("customer email", [c.email for c in seed.customers]),
        ("card_ref", [c.card_ref for c in seed.cards]),
        ("transaction reference", [t.reference for t in seed.transactions]),
        ("operator email", [o.email for o in seed.operators]),
    ):
        errors.extend(f"duplicate {name}: {ref}" for ref in _duplicates(refs))

    if len(seed.customers) != CUSTOMER_COUNT:
        errors.append(f"expected {CUSTOMER_COUNT} customers, found {len(seed.customers)}")
    missing_roles = set(OperatorRole) - {o.role for o in seed.operators}
    if missing_roles:
        errors.append(f"no operator with role {', '.join(sorted(missing_roles))}")

    errors.extend(_error_code_errors(seed, known_error_codes))
    errors.extend(_card_errors(seed, customers))
    errors.extend(_transaction_errors(seed, customers, cards))
    errors.extend(_scenario_errors(seed, customers))
    return errors


def _error_code_errors(seed: SeedData, known: frozenset[str]) -> list[str]:
    used = [(t.reference, t.error_code) for t in seed.transactions] + [
        (c.external_ref, c.upgrade.error_code) for c in seed.customers if c.upgrade is not None
    ]
    return [
        f"{ref}: {code} is not in data/kb/error_codes.csv"
        for ref, code in used
        if code is not None and code not in known
    ]


def _card_errors(seed: SeedData, customers: dict[str, Customer]) -> list[str]:
    errors: list[str] = []
    owned: dict[str, Counter[CardKind]] = {}
    for card in seed.cards:
        customer = customers.get(card.customer_ref)
        if customer is None:
            errors.append(f"{card.card_ref}: unknown customer {card.customer_ref}")
            continue
        if card.status is not CardStatus.CANCELLED:
            owned.setdefault(card.customer_ref, Counter())[card.kind] += 1
        if card.delivery_method == "express" and not _in_eu(customer.country):
            errors.append(f"{card.card_ref}: express delivery is EU only")
    for ref, counts in owned.items():
        physical, virtual = CARD_ALLOWANCE[customers[ref].tier]
        if counts[CardKind.PHYSICAL] > physical or counts[CardKind.VIRTUAL] > virtual:
            errors.append(f"{ref}: more cards than the {customers[ref].tier} tier allows")
    return errors


# EU member states (ISO 3166-1 alpha-2), for the "express delivery is EU only" rule.
EU_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
        "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    }
)  # fmt: skip


def _in_eu(country: str) -> bool:
    return country in EU_COUNTRIES


def _transaction_errors(
    seed: SeedData, customers: dict[str, Customer], cards: dict[str, Card]
) -> list[str]:
    errors: list[str] = []
    per_customer: Counter[str] = Counter()
    for tx in seed.transactions:
        customer = customers.get(tx.customer_ref)
        if customer is None:
            errors.append(f"{tx.reference}: unknown customer {tx.customer_ref}")
            continue
        per_customer[tx.customer_ref] += 1
        if tx.card_ref is not None:
            card = cards.get(tx.card_ref)
            if card is None or card.customer_ref != tx.customer_ref:
                errors.append(f"{tx.reference}: card {tx.card_ref} is not the customer's card")
        if tx.error_code == RESTRICTED_CODE and not customer.restricted:
            errors.append(f"{tx.reference}: {RESTRICTED_CODE} on a customer who is not restricted")
        if tx.type is TransactionType.MASS_PAYOUT and customer.tier is not CustomerTier.BUSINESS:
            errors.append(f"{tx.reference}: mass payouts are Business only")
        if tx.type in CARD_USE and customer.tier is CustomerTier.STARTER:
            errors.append(f"{tx.reference}: Starter customers have no card")
        if _sort_key(tx.requested_at) < _sort_key(customer.opened_at):
            errors.append(f"{tx.reference}: requested before the account was opened")
    errors.extend(daily_limit_errors(seed, customers))
    for ref in customers:
        count = per_customer[ref]
        if not MIN_TRANSACTIONS_PER_CUSTOMER <= count <= MAX_TRANSACTIONS_PER_CUSTOMER:
            errors.append(
                f"{ref}: {count} transactions, expected "
                f"{MIN_TRANSACTIONS_PER_CUSTOMER}-{MAX_TRANSACTIONS_PER_CUSTOMER}"
            )
    return errors


def _scenario_errors(seed: SeedData, customers: dict[str, Customer]) -> list[str]:
    """Each declared scenario is backed by data; each intent has enough customers."""
    errors: list[str] = []
    txs: dict[str, list[Transaction]] = {}
    for tx in seed.transactions:
        txs.setdefault(tx.customer_ref, []).append(tx)
    cards: dict[str, list[Card]] = {}
    for card in seed.cards:
        cards.setdefault(card.customer_ref, []).append(card)

    counts: Counter[Intent] = Counter()
    for ref, customer in customers.items():
        for intent in customer.support_scenarios:
            counts[intent] += 1
            check = SCENARIO_CHECKS.get(intent)
            if check is not None and not check(customer, txs.get(ref, []), cards.get(ref, [])):
                errors.append(f"{ref}: data does not fit the {intent} scenario")
    for intent in Intent:
        if counts[intent] < MIN_CUSTOMERS_PER_INTENT:
            errors.append(
                f"intent {intent}: {counts[intent]} customers, "
                f"expected at least {MIN_CUSTOMERS_PER_INTENT}"
            )
    return errors


def _days(offset: str) -> int:
    return parse_offset(offset)[0]


def _has_pending_incoming(c: Customer, txs: list[Transaction], cards: list[Card]) -> bool:
    open_statuses = {TransactionStatus.PENDING, TransactionStatus.HELD_FOR_REVIEW}
    return any(t.type in INCOMING and t.status in open_statuses for t in txs)


def _has_withdrawal_problem(c: Customer, txs: list[Transaction], cards: list[Card]) -> bool:
    problems = {TransactionStatus.PENDING, TransactionStatus.FAILED, TransactionStatus.RETURNED}
    return any(t.type in WITHDRAWALS and t.status in problems for t in txs)


def _has_declined_card(c: Customer, txs: list[Transaction], cards: list[Card]) -> bool:
    return any(t.type in CARD_USE and t.status is TransactionStatus.FAILED for t in txs)


def _has_physical_card(c: Customer, txs: list[Transaction], cards: list[Card]) -> bool:
    return any(card.kind is CardKind.PHYSICAL for card in cards)


def _has_upgrade(c: Customer, txs: list[Transaction], cards: list[Card]) -> bool:
    return c.upgrade is not None


def _has_fees(c: Customer, txs: list[Transaction], cards: list[Card]) -> bool:
    fee_types = {TransactionType.CONVERSION, TransactionType.FEE}
    return any(t.fee > 0 or t.type in fee_types for t in txs)


def _limit_nearly_used(c: Customer, txs: list[Transaction], cards: list[Card]) -> bool:
    """A limit error, or EUR withdrawals on one day that used most of the daily limit.

    Seed events are never on the anchor day (LATEST_EVENT_DAY), so the daily limit is never
    nearly used *today*; the data shows a day on which it was, and limit errors.
    """
    limit_codes = {"ERR-1020", "ERR-2001", "ERR-3011"}
    if any(t.error_code in limit_codes for t in txs):
        return True
    per_day: dict[int, Decimal] = {}
    for t in txs:
        if (
            t.type in WITHDRAWALS
            and t.currency is Currency.EUR
            and t.status in {TransactionStatus.COMPLETED, TransactionStatus.PENDING}
        ):
            day = _days(t.requested_at)
            per_day[day] = per_day.get(day, Decimal(0)) + t.amount
    limit = DAILY_WITHDRAW_LIMIT_EUR[c.tier] * NEARLY_USED_SHARE
    return any(total >= limit for total in per_day.values())


def _has_disputable_payment(c: Customer, txs: list[Transaction], cards: list[Card]) -> bool:
    return any(
        t.type is TransactionType.CARD_PAYMENT
        and t.status is TransactionStatus.COMPLETED
        and -DISPUTE_WINDOW_DAYS < _days(t.requested_at) <= 0
        for t in txs
    )


type ScenarioCheck = Callable[[Customer, list[Transaction], list[Card]], bool]

# login_security, account_management and other need no particular data: any customer can ask.
# account_restricted is checked by `Customer` itself (it must match the restricted flag).
SCENARIO_CHECKS: dict[Intent, ScenarioCheck] = {
    Intent.PAYMENT_PENDING: _has_pending_incoming,
    Intent.WITHDRAWAL_ISSUE: _has_withdrawal_problem,
    Intent.CARD_DECLINED: _has_declined_card,
    Intent.CARD_LOST_STOLEN: _has_physical_card,
    Intent.KYC_VERIFICATION: _has_upgrade,
    Intent.FEES_FX: _has_fees,
    Intent.LIMITS: _limit_nearly_used,
    Intent.DISPUTE_CHARGEBACK: _has_disputable_payment,
}


# --- daily limits (P8-01, deferred from P2-05) ----------------------------------------------------

# Daily limits per tier in EUR (world file §3): withdrawals (bank withdrawals and mass payouts),
# card spend, ATM withdrawals. Starter has no card, so no card or ATM limit applies.
DAILY_CARD_SPEND_LIMIT_EUR = {
    CustomerTier.VERIFIED: Decimal(5_000),
    CustomerTier.BUSINESS: Decimal(15_000),
}
DAILY_ATM_LIMIT_EUR = {
    CustomerTier.VERIFIED: Decimal(1_000),
    CustomerTier.BUSINESS: Decimal(2_000),
}
# Limits are in EUR equivalent, but the world file gives no exchange rate. The check counts one
# USD or GBP as this many euros, more than either has been worth, so a day it accepts is within
# the limit at any realistic rate. A validator margin, not a fact of the world.
EUR_PER_UNIT_AT_MOST = {Currency.EUR: Decimal(1), Currency.USD: Decimal("1.25"),
                        Currency.GBP: Decimal("1.25")}  # fmt: skip
# Money that left or is leaving the account: a failed transaction never left, a returned one
# came back.
COUNTS_TOWARD_LIMIT = frozenset(
    {TransactionStatus.PENDING, TransactionStatus.HELD_FOR_REVIEW, TransactionStatus.COMPLETED}
)


class DailyLimit(StrEnum):
    WITHDRAWAL = "withdrawal"
    CARD_SPEND = "card spend"
    ATM = "ATM"


def _limit_kind(tx: Transaction) -> DailyLimit | None:
    if tx.type in WITHDRAWALS or tx.type is TransactionType.MASS_PAYOUT:
        return DailyLimit.WITHDRAWAL
    if tx.type is TransactionType.CARD_PAYMENT:
        return DailyLimit.CARD_SPEND
    if tx.type is TransactionType.ATM_WITHDRAWAL:
        return DailyLimit.ATM
    return None


def _daily_limit(kind: DailyLimit, tier: CustomerTier) -> Decimal | None:
    if kind is DailyLimit.WITHDRAWAL:
        return DAILY_WITHDRAW_LIMIT_EUR[tier]
    table = DAILY_CARD_SPEND_LIMIT_EUR if kind is DailyLimit.CARD_SPEND else DAILY_ATM_LIMIT_EUR
    return table.get(tier)


# Limits reset at 00:00 CET (world file §3). The seed stores UTC times and the anchor date can
# be in winter (CET = UTC+1) or summer (CEST = UTC+2), so 22:00-22:59 UTC is the same CET day in
# winter and the next one in summer; 23:00 UTC and later is always the next day.
CET_DAY_STARTS_UTC = (time(22, 0), time(23, 0))


def cet_days(offset: str) -> tuple[int, ...]:
    """The CET day (as an offset from the anchor day) an event falls on: one day, or two when
    it depends on summer time."""
    days, at = parse_offset(offset)
    summer_start, winter_start = CET_DAY_STARTS_UTC
    if at >= winter_start:
        return (days + 1,)
    if at >= summer_start:
        return (days, days + 1)
    return (days,)


def daily_limit_errors(seed: SeedData, customers: dict[str, Customer]) -> list[str]:
    """Totals per customer, CET day and limit (withdrawals incl. pending and mass payouts; card
    spend incl. pending payments; ATM) against the tier's daily limit. An event whose CET day
    depends on summer time counts on both days it can fall on."""
    totals: dict[tuple[str, DailyLimit, int], Decimal] = {}
    for tx in seed.transactions:
        kind = _limit_kind(tx)
        if kind is None or tx.status not in COUNTS_TOWARD_LIMIT or tx.customer_ref not in customers:
            continue
        amount = tx.amount * EUR_PER_UNIT_AT_MOST[tx.currency]
        for day in cet_days(tx.requested_at):
            key = (tx.customer_ref, kind, day)
            totals[key] = totals.get(key, Decimal(0)) + amount
    errors: list[str] = []
    for (ref, kind, day), total in sorted(totals.items()):
        limit = _daily_limit(kind, customers[ref].tier)
        if limit is not None and total > limit:
            errors.append(
                f"{ref}: {kind} on CET day {day:+d}d is up to EUR {total:.2f}, above the "
                f"daily limit of EUR {limit} of the {customers[ref].tier} tier"
            )
    return errors


# --- the chat's customer picker (P10-02) ----------------------------------------------------

# Customers whose seed data holds canary strings of the attack suite (P8-01): never on a card.
CANARY_CUSTOMERS = frozenset({"CUS-0021", "CUS-0023"})


class DemoCustomerEntry(BaseModel):
    """One card of `data/seed/demo_customers.json`: which customer, a hint, first messages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    customer_ref: Annotated[str, Field(pattern=r"^CUS-\d{4}$")]
    hint: Annotated[str, Field(min_length=1, max_length=40)]
    suggestions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=140)]], Field(min_length=1, max_length=4)
    ]


DEMO_CUSTOMERS = TypeAdapter(list[DemoCustomerEntry])


def validate_demo_customers(entries: list[DemoCustomerEntry], seed: SeedData) -> list[str]:
    """Every card names a seeded customer once, and never a canary customer."""
    known = {customer.external_ref for customer in seed.customers}
    errors = [
        f"{ref}: listed more than once"
        for ref, n in Counter(e.customer_ref for e in entries).items()
        if n > 1
    ]
    for entry in entries:
        if entry.customer_ref not in known:
            errors.append(f"{entry.customer_ref}: not in customers.json")
        if entry.customer_ref in CANARY_CUSTOMERS:
            errors.append(f"{entry.customer_ref}: holds attack-suite canaries; never a demo card")
    return errors
