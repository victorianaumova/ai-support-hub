"""`make data-check`: validate the files in `data/` (docs/operations.md §1).

The check fails safe: a file passes only when a validator for it exists. `data/seed/` holds the
four seed files, checked by the schemas and cross-file rules of `hub.domain.seed`, plus a scan
that no text in them looks like card data. `data/kb/` holds the articles (`*.md`) and
`error_codes.csv`, checked by `hub.domain.kb`; the seed's error codes must be in that CSV. Any
other file (except `.gitkeep` placeholders) fails with "no validator", so data can never pass CI
unchecked. `data/eval/` holds `tickets.dev.jsonl` and `attacks.jsonl`, checked line by line
against `hub_evals.schemas`, then across lines and files (P4-01); its `README.md` and
`CHANGELOG.md` are documentation.

This never reads `data/eval/tickets.test.jsonl` (AGENTS.md, never-do rule 7): there is no test
set (ADR-0018), and files without a validator are judged by name only.
"""

import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path

from pydantic import ValidationError

from hub.domain.enums import Intent
from hub.domain.kb import (
    ERROR_CODES_DOC_ID,
    ERROR_CODES_FILE,
    KbFormatError,
    Visibility,
    article_errors,
    error_code_errors,
    error_codes_in,
    parse_article,
    parse_error_codes,
)
from hub.domain.seed import (
    DEMO_CUSTOMERS,
    SeedData,
    parse_seed,
    validate_demo_customers,
    validate_seed,
)
from hub.guardrails.masking import mask, mask_text
from hub_evals.schemas import (
    ATTACKS_PER_CATEGORY,
    MIN_HARD_TICKETS,
    MIN_MUST_HANDOFF,
    TICKET_COUNT,
    Attack,
    AttackCategory,
    Ticket,
)

KNOWN_FOLDERS = frozenset({"kb", "seed", "eval"})
PLACEHOLDER = ".gitkeep"
# Written by macOS Finder and git-ignored; not data.
OS_NOISE = frozenset({".DS_Store"})
SEED_FILES = ("customers.json", "cards.json", "transactions.json", "operators.json")
# The chat's customer picker (P10-02): optional, checked against the seed when present.
DEMO_CUSTOMERS_FILE = "demo_customers.json"
# Stricter than masking, which only catches Luhn-valid numbers: the seed holds no long digit
# runs at all (cards keep only the last 4 digits).
_LONG_DIGIT_RUN = re.compile(r"\d{13,}")
TICKETS_FILE = "tickets.dev.jsonl"
# ADR-0018: there is no test set. Refused by name, never opened (AGENTS.md rule 7).
TEST_SET_FILE = "tickets.test.jsonl"
ATTACKS_FILE = "attacks.jsonl"
EVAL_DOCS = frozenset({"README.md", "CHANGELOG.md"})
# Public test card numbers of the card networks (never issued to a person). A ticket may hold a
# card number on purpose (hard case `card_number`), but only one of these (AGENTS.md rule 1).
PUBLIC_TEST_CARDS = frozenset(
    {
        "4111111111111111",
        "4242424242424242",
        "4000056655665556",
        "5555555555554444",
        "5105105105105100",
        "2223003122003222",
        "378282246310005",
        "371449635398431",
        "6011111111111117",
    }
)
COMPOSITION_SKIPPED = "counts and coverage not checked until every line is valid"
NOT_A_TEST_CARD = "holds a card number that is not a public test number"
_TEST_CARD_PATTERNS = tuple(
    re.compile(r"(?<!\d)" + r"[ -]?".join(number) + r"(?!\d)") for number in PUBLIC_TEST_CARDS
)


def check_layout(data_dir: Path) -> list[str]:
    """Return one error message per file that has no validator or is in an unknown place."""
    if not data_dir.is_dir():
        return [f"{data_dir}: not a directory"]
    errors: list[str] = []
    for path in sorted(data_dir.rglob("*")):
        relative = path.relative_to(data_dir)
        if path.name in OS_NOISE or (path.is_dir() and len(relative.parts) > 1):
            continue
        if len(relative.parts) == 1:
            if path.is_dir() and path.name in KNOWN_FOLDERS:
                continue
            errors.append(f"{path}: not a known data folder ({', '.join(sorted(KNOWN_FOLDERS))})")
        elif relative == Path("eval", TEST_SET_FILE):
            errors.append(f"{path}: there is no test set (ADR-0018); remove this file")
        elif path.name != PLACEHOLDER and not (
            _is_seed_file(relative) or _is_kb_file(relative) or _is_eval_file(relative)
        ):
            errors.append(f"{path}: no validator for this file yet")
    return errors


def _is_seed_file(relative: Path) -> bool:
    return (
        relative.parts[0] == "seed"
        and len(relative.parts) == 2
        and relative.name in {*SEED_FILES, DEMO_CUSTOMERS_FILE}
    )


def _is_kb_file(relative: Path) -> bool:
    return (
        relative.parts[0] == "kb"
        and len(relative.parts) == 2
        and (relative.suffix == ".md" or relative.name == ERROR_CODES_FILE)
    )


def _is_eval_file(relative: Path) -> bool:
    return (
        relative.parts[0] == "eval"
        and len(relative.parts) == 2
        and relative.name in {TICKETS_FILE, ATTACKS_FILE, *EVAL_DOCS}
    )


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield str(value)


def check_card_data(name: str, raw: bytes) -> list[str]:
    """No text or number in a seed file may look like a card number, CVV, PIN or 2FA code."""
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        return []  # the schema check reports invalid JSON
    errors: list[str] = []
    for text in _strings(document):
        if _looks_like_card_data(text):
            errors.append(f"seed/{name}: value looks like card data (masking would change it)")
    return errors


def _looks_like_card_data(text: str) -> bool:
    return bool(_LONG_DIGIT_RUN.search(text)) or mask_text(text) != text


def check_seed(seed_dir: Path) -> list[str]:
    """Validate the seed files. A missing `data/seed/` folder or empty folder is valid.

    Error codes are checked against `error_codes.csv` in the `kb` folder next to `seed_dir`.
    """
    present = [name for name in SEED_FILES if (seed_dir / name).is_file()]
    if not present:
        return []
    missing = [name for name in SEED_FILES if name not in present]
    if missing:
        return [f"{seed_dir / name}: missing (the seed has four files)" for name in missing]
    files = {name: (seed_dir / name).read_bytes() for name in SEED_FILES}
    errors: list[str] = []
    for name, raw in files.items():
        errors.extend(check_card_data(name, raw))
    try:
        seed = parse_seed(files)
    except ValidationError as error:
        return [*errors, *_format_validation(error)]
    codes = _read_error_codes(seed_dir.parent / "kb" / ERROR_CODES_FILE)
    if codes is None:
        errors.append(f"seed: error codes cannot be checked: {ERROR_CODES_FILE} missing or invalid")
        codes = _codes_used(seed)  # report the other seed rules anyway
    errors.extend(f"seed: {message}" for message in validate_seed(seed, codes))
    return [*errors, *check_demo_customers(seed_dir / DEMO_CUSTOMERS_FILE, seed)]


def check_demo_customers(path: Path, seed: SeedData) -> list[str]:
    """Schema, known customers, no canary customer, no card data in hints or suggestions."""
    if not path.is_file():
        return []
    raw = path.read_bytes()
    errors = check_card_data(path.name, raw)
    try:
        entries = DEMO_CUSTOMERS.validate_json(raw)
    except ValidationError as error:
        return [*errors, *(f"seed/{path.name}: {m}" for m in _format_validation(error))]
    return [*errors, *(f"seed/{path.name}: {m}" for m in validate_demo_customers(entries, seed))]


def _read_error_codes(path: Path) -> frozenset[str] | None:
    try:
        return frozenset(r.code for r in parse_error_codes(path.read_text(encoding="utf-8")))
    except (OSError, KbFormatError):
        return None


def _codes_used(seed: SeedData) -> frozenset[str]:
    codes = {t.error_code for t in seed.transactions}
    codes |= {c.upgrade.error_code for c in seed.customers if c.upgrade is not None}
    return frozenset(code for code in codes if code is not None)


def check_kb(kb_dir: Path) -> list[str]:
    """Validate the articles and `error_codes.csv`. A missing or empty `data/kb/` is valid.

    Once there is any KB file, the CSV must exist and every intent needs an external article.
    """
    articles = sorted(kb_dir.glob("*.md")) if kb_dir.is_dir() else []
    csv_path = kb_dir / ERROR_CODES_FILE
    if not articles and not csv_path.is_file():
        return []
    errors: list[str] = []
    codes: frozenset[str] = frozenset()
    if not csv_path.is_file():
        errors.append(f"{csv_path}: missing")
    else:
        csv_errors, codes = _check_error_codes(csv_path)
        errors.extend(csv_errors)
    covered: set[Intent] = set()
    for path in articles:
        text = path.read_text(encoding="utf-8")
        if _looks_like_card_data(text):
            errors.append(f"kb/{path.name}: text looks like card data (masking would change it)")
        try:
            article = parse_article(text)
        except KbFormatError as error:
            errors.append(f"kb/{path.name}: {error}")
            continue
        errors.extend(f"kb/{path.name}: {e}" for e in article_errors(article, path.stem))
        errors.extend(
            f"kb/{path.name}: {code} is not in {ERROR_CODES_FILE}"
            for code in sorted(error_codes_in(article.body) - codes)
            if codes
        )
        if article.front_matter.visibility is Visibility.EXTERNAL:
            covered.update(article.front_matter.intents)
    if articles:
        errors.extend(
            f"kb: no external article for intent {intent}"
            for intent in Intent
            if intent not in covered
        )
    return errors


def _check_error_codes(path: Path) -> tuple[list[str], frozenset[str]]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    if _looks_like_card_data(text):
        errors.append(f"kb/{path.name}: text looks like card data (masking would change it)")
    try:
        rows = parse_error_codes(text)
    except KbFormatError as error:
        return [*errors, f"kb/{path.name}: {error}"], frozenset()
    for row in rows:
        errors.extend(f"kb/{path.name}: {e}" for e in error_code_errors(row))
    return errors, frozenset(row.code for row in rows)


def _format_validation(error: ValidationError) -> list[str]:
    return [
        f"seed ({error.title}): {'.'.join(str(p) for p in item['loc'])}: {item['msg']}"
        for item in error.errors(include_url=False, include_input=False)
    ]


class _References:
    """What the evaluation files may refer to: seed customers and external KB documents."""

    def __init__(self, data_dir: Path) -> None:
        self.customers = _customer_refs(data_dir / "seed" / "customers.json")
        self.articles = _article_visibility(data_dir / "kb")
        codes = _read_error_codes(data_dir / "kb" / ERROR_CODES_FILE) or frozenset()
        self.error_codes = frozenset(f"{ERROR_CODES_DOC_ID}#{code}" for code in codes)

    def customer_errors(self, ref: str) -> list[str]:
        return [] if ref in self.customers else [f"customer_ref {ref} is not a seed customer"]

    def gold_errors(self, doc_id: str) -> list[str]:
        if doc_id.startswith(f"{ERROR_CODES_DOC_ID}#"):
            known = doc_id in self.error_codes
            return [] if known else [f"gold doc {doc_id} is not a row of {ERROR_CODES_FILE}"]
        visibility = self.articles.get(doc_id)
        if visibility is None:
            return [f"gold doc {doc_id} is not a KB article"]
        if visibility is not Visibility.EXTERNAL:
            # Retrieval for the writer returns only external chunks, so it could never be hit.
            return [f"gold doc {doc_id} is internal; gold documents must be external"]
        return []


def _customer_refs(path: Path) -> frozenset[str]:
    """`external_ref` of every customer; empty when the file is missing or not JSON (the seed
    check reports that)."""
    try:
        document = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(document, list):
        return frozenset()
    return frozenset(
        ref
        for item in document
        if isinstance(item, dict) and isinstance(ref := item.get("external_ref"), str)
    )


def _article_visibility(kb_dir: Path) -> dict[str, Visibility]:
    """Article id → visibility for every article that parses (the KB check reports the rest)."""
    found: dict[str, Visibility] = {}
    for path in sorted(kb_dir.glob("*.md")) if kb_dir.is_dir() else []:
        try:
            front_matter = parse_article(path.read_text(encoding="utf-8")).front_matter
        except KbFormatError:
            continue
        found[front_matter.id] = front_matter.visibility
    return found


def card_number_errors(text: str) -> list[str]:
    """A card number in evaluation data must be a public test number.

    Give it decoded text: in raw JSON a number written as `\\u0034…` escapes would pass."""
    for pattern in _TEST_CARD_PATTERNS:
        text = pattern.sub("[test card]", text)
    result = mask(text)
    if result.failed or result.counts.get("PAN", 0) or _LONG_DIGIT_RUN.search(text):
        return [NOT_A_TEST_CARD]
    return []


def _read_lines[M: (Ticket, Attack)](
    path: Path, model: type[M], item_errors: Callable[[M], list[str]]
) -> tuple[list[str], list[M]]:
    """Validate a JSON Lines file. Returns one message per problem, each with its line number,
    and the items that passed the schema."""
    label = f"eval/{path.name}"
    errors: list[str] = []
    items: list[M] = []
    seen: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except UnicodeDecodeError as error:
        return [f"{label}: not UTF-8 text ({error.reason} at byte {error.start})"], []
    if lines and lines[-1] == "":
        lines.pop()  # the newline at the end of the file
    if not lines:
        return [f"{label}: empty file"], []
    for number, line in enumerate(lines, start=1):
        where = f"{label}:{number}"
        if not line.strip():
            errors.append(f"{where}: blank line")
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            errors.append(f"{where}: invalid JSON: {error.msg} (column {error.colno})")
            errors.extend(f"{where}: {e}" for e in card_number_errors(line))
            continue
        if isinstance(raw, dict) and isinstance(raw.get("id"), str):
            where = f"{where} ({raw['id']})"
        # Every decoded string (keys too), like the seed check: JSON escapes cannot hide a number.
        if any(card_number_errors(text) for text in _strings(raw)):
            errors.append(f"{where}: {NOT_A_TEST_CARD}")
        try:
            item = model.model_validate(raw)
        except ValidationError as error:
            errors.extend(f"{where}: {message}" for message in _validation_messages(error))
            continue
        item_id = item.id
        if item_id in seen:
            errors.append(f"{where}: id repeats line {seen[item_id]}")
        else:
            seen[item_id] = number
        errors.extend(f"{where}: {e}" for e in item_errors(item))
        items.append(item)
    return errors, items


def _validation_messages(error: ValidationError) -> list[str]:
    messages: list[str] = []
    for item in error.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        messages.append(f"{location}: {message}" if location else message)
    return messages


def check_tickets(path: Path, refs: _References) -> list[str]:
    """Each line against `Ticket` and the references; then the composition rules of eval plan
    §3. The composition rules run only when every line is valid, so one broken line does not
    also report a missing intent; a last message says they were skipped."""

    def item_errors(ticket: Ticket) -> list[str]:
        errors = refs.customer_errors(ticket.customer_ref)
        for doc_id in ticket.gold_doc_ids:
            errors.extend(refs.gold_errors(doc_id))
        return errors

    errors, tickets = _read_lines(path, Ticket, item_errors)
    if errors:
        return [*errors, f"eval/{path.name}: {COMPOSITION_SKIPPED}"]
    label = f"eval/{path.name}"
    if len(tickets) != TICKET_COUNT:
        errors.append(f"{label}: {len(tickets)} tickets, expected exactly {TICKET_COUNT}")
    intents = {t.intent for t in tickets}
    errors.extend(f"{label}: no ticket with intent {i}" for i in Intent if i not in intents)
    hard = sum(t.difficulty == "hard" for t in tickets)
    if hard < MIN_HARD_TICKETS:
        errors.append(f"{label}: {hard} hard tickets, expected at least {MIN_HARD_TICKETS}")
    handoffs = sum(t.must_handoff for t in tickets)
    if handoffs < MIN_MUST_HANDOFF:
        errors.append(
            f"{label}: {handoffs} must_handoff tickets, expected at least {MIN_MUST_HANDOFF}"
        )
    return errors


def check_attacks(path: Path, refs: _References) -> list[str]:
    """Each line against `Attack`; then exactly 2 attacks in each of the 8 categories."""
    errors, attacks = _read_lines(path, Attack, lambda a: refs.customer_errors(a.customer_ref))
    if errors:
        return [*errors, f"eval/{path.name}: {COMPOSITION_SKIPPED}"]
    counts = Counter(a.category for a in attacks)
    return [
        f"eval/{path.name}: {counts[category]} attacks in category {category}, "
        f"expected exactly {ATTACKS_PER_CATEGORY}"
        for category in AttackCategory
        if counts[category] != ATTACKS_PER_CATEGORY
    ]


def check_eval(data_dir: Path) -> list[str]:
    """Validate `tickets.dev.jsonl` and `attacks.jsonl`. A missing file is valid (each arrives
    with its own task: P4-02 and P8-01)."""
    eval_dir = data_dir / "eval"
    tickets = eval_dir / TICKETS_FILE
    attacks = eval_dir / ATTACKS_FILE
    if not tickets.is_file() and not attacks.is_file():
        return []
    refs = _References(data_dir)
    errors: list[str] = []
    if tickets.is_file():
        errors.extend(check_tickets(tickets, refs))
    if attacks.is_file():
        errors.extend(check_attacks(attacks, refs))
    return errors


def check(data_dir: Path) -> list[str]:
    errors = check_layout(data_dir)
    if data_dir.is_dir():
        errors.extend(check_kb(data_dir / "kb"))
        errors.extend(check_seed(data_dir / "seed"))
        errors.extend(check_eval(data_dir))
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m hub_evals.data_check <data dir>", file=sys.stderr)
        return 2
    data_dir = Path(argv[0])
    errors = check(data_dir)
    for error in errors:
        print(f"data-check: {error}", file=sys.stderr)
    if errors:
        print(f"data-check: FAILED, {len(errors)} problem(s)", file=sys.stderr)
        return 1
    print(f"data-check: OK ({data_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
