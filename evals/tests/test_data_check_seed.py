"""`make data-check` on `data/seed/`: schemas, cross-file rules and the card-data scan.

The rules themselves are tested in apps/api/tests/unit/test_seed_schema.py; these tests show that
data-check runs them on the files and reports problems instead of passing.
"""

import json
import shutil
from pathlib import Path

import pytest

from hub_evals.data_check import (
    DEMO_CUSTOMERS_FILE,
    SEED_FILES,
    check,
    check_card_data,
    check_demo_customers,
    check_seed,
    main,
)

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "data" / "seed"


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """A copy of the repository's `data/` folder with the real seed and KB files."""
    target = tmp_path / "data"
    (target / "eval").mkdir(parents=True)
    shutil.copytree(SEED, target / "seed")
    shutil.copytree(ROOT / "data" / "kb", target / "kb")
    return target


def _edit(data: Path, name: str, index: int, **fields: object) -> None:
    path = data / "seed" / name
    items = json.loads(path.read_text(encoding="utf-8"))
    items[index].update(fields)
    path.write_text(json.dumps(items), encoding="utf-8")


def test_the_repository_seed_passes() -> None:
    assert check(ROOT / "data") == []


def test_main_reports_ok(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(data)]) == 0
    assert "data-check: OK" in capsys.readouterr().out


def test_an_empty_seed_folder_passes(tmp_path: Path) -> None:
    assert check_seed(tmp_path) == []


def test_a_missing_seed_file_fails(data: Path) -> None:
    (data / "seed" / "cards.json").unlink()
    assert check_seed(data / "seed") == [
        f"{data / 'seed' / 'cards.json'}: missing (the seed has four files)"
    ]


def test_a_schema_error_is_reported_with_its_location(data: Path) -> None:
    _edit(data, "customers.json", 0, tier="platinum")
    errors = check_seed(data / "seed")
    assert len(errors) == 1
    assert "0.tier" in errors[0]


def test_invalid_json_is_reported(data: Path) -> None:
    (data / "seed" / "operators.json").write_text("[{", encoding="utf-8")
    errors = check_seed(data / "seed")
    assert any("Invalid JSON" in e or "json_invalid" in e or "EOF" in e for e in errors)


def test_a_seed_code_missing_from_the_csv_fails(data: Path) -> None:
    csv = data / "kb" / "error_codes.csv"
    lines = csv.read_text(encoding="utf-8").splitlines(keepends=True)
    csv.write_text(
        "".join(line for line in lines if not line.startswith("ERR-5010")), encoding="utf-8"
    )
    errors = check_seed(data / "seed")
    assert errors
    assert all("ERR-5010 is not in data/kb/error_codes.csv" in e for e in errors)


def test_without_the_csv_the_seed_codes_cannot_be_checked(data: Path) -> None:
    (data / "kb" / "error_codes.csv").unlink()
    assert check_seed(data / "seed") == [
        "seed: error codes cannot be checked: error_codes.csv missing or invalid"
    ]


def test_without_the_csv_other_seed_rules_still_run(data: Path) -> None:
    (data / "kb" / "error_codes.csv").unlink()
    _edit(data, "cards.json", 0, customer_ref="CUS-9999")
    assert any("unknown customer CUS-9999" in e for e in check_seed(data / "seed"))


def test_a_cross_file_error_is_reported(data: Path) -> None:
    _edit(data, "cards.json", 0, customer_ref="CUS-9999")
    assert any("unknown customer CUS-9999" in e for e in check_seed(data / "seed"))


def test_a_full_card_number_in_any_text_fails(
    data: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit(data, "transactions.json", 0, counterparty="Shop 4111 1111 1111 1111")
    assert main([str(data)]) == 1
    err = capsys.readouterr().err
    assert "seed/transactions.json: value looks like card data" in err
    assert "4111" not in err  # the message never repeats the value


@pytest.mark.parametrize(
    "value",
    [
        "4111111111111111",  # Luhn-valid test card
        "1234567890123",  # long digit run, not Luhn-valid
        "my CVV is 123",
        4111111111111111,  # numbers are scanned too
        {"4111 1111 1111 1111": "key"},  # and keys
    ],
)
def test_check_card_data_finds(value: object) -> None:
    raw = json.dumps([{"field": value}]).encode()
    assert check_card_data("x.json", raw) == [
        "seed/x.json: value looks like card data (masking would change it)"
    ]


@pytest.mark.parametrize(
    "value", ["-3d 10:15", "1250.00", "CUS-0001", "ERR-3001", "4242", 12.5, True, None]
)
def test_check_card_data_passes_ordinary_values(value: object) -> None:
    assert check_card_data("x.json", json.dumps([{"field": value}]).encode()) == []


def test_check_card_data_leaves_invalid_json_to_the_schema() -> None:
    assert check_card_data("x.json", b"[{") == []


def test_the_seed_files_constant_matches_the_data_model() -> None:
    assert {*SEED_FILES, DEMO_CUSTOMERS_FILE} == {p.name for p in SEED.glob("*.json")}


def test_the_demo_customers_file_names_seeded_customers_only(tmp_path: Path) -> None:
    from hub.domain.seed import parse_seed

    seed = parse_seed({name: (SEED / name).read_bytes() for name in SEED_FILES})
    path = tmp_path / DEMO_CUSTOMERS_FILE
    card = {"hint": "h", "suggestions": ["Where is my money?"]}
    path.write_text(
        json.dumps(
            [
                {"customer_ref": "CUS-0001", **card},
                {"customer_ref": "CUS-0001", **card},
                {"customer_ref": "CUS-0999", **card},
                {"customer_ref": "CUS-0021", **card},
            ]
        )
    )
    assert check_demo_customers(path, seed) == [
        "seed/demo_customers.json: CUS-0001: listed more than once",
        "seed/demo_customers.json: CUS-0999: not in customers.json",
        "seed/demo_customers.json: CUS-0021: holds attack-suite canaries; never a demo card",
    ]
    path.write_text(json.dumps([{"customer_ref": "CUS-0001", "hint": "h", "suggestions": []}]))
    assert check_demo_customers(path, seed)
    path.write_text(json.dumps([{"customer_ref": "CUS-0001", **card, "x": 1}]))
    assert check_demo_customers(path, seed)
    assert check_demo_customers(tmp_path / "missing.json", seed) == []
