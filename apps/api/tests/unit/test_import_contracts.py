"""The module rules of docs/design.md §8 are import-linter contracts, and each one really fails.

Every violation test copies the three packages into a temporary folder, plants one wrong import
there and runs `lint-imports` against the copy, so the repository itself is never changed.
`PYTHONPATH` puts the copy in front of the installed workspace packages; a test that sees the
expected contract broken has therefore analysed the copy, not the original.
"""

import ast
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[4]
PYPROJECT = ROOT / "pyproject.toml"
DESIGN = (ROOT / "docs" / "design.md").read_text(encoding="utf-8")
LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"
PACKAGES = {
    "hub": ROOT / "apps" / "api" / "src" / "hub",
    "support_tools": ROOT / "apps" / "mcp_server" / "src" / "support_tools",
    "hub_evals": ROOT / "evals" / "src" / "hub_evals",
}

# (package folder relative to the copied src, import statement)
Plant = tuple[str, str]


def _config() -> dict[str, Any]:
    config: dict[str, Any] = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return config["tool"]["importlinter"]  # type: ignore[no-any-return]


def _contracts() -> dict[str, dict[str, Any]]:
    return {contract["id"]: contract for contract in _config()["contracts"]}


def _module_rules() -> str:
    start = DESIGN.index("Module rules (enforced by import-linter in CI):")
    return DESIGN[start : DESIGN.index("\n## ", start)]


def _run_lint_imports(cwd: Path, pythonpath: Path | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return subprocess.run(
        [str(LINT_IMPORTS), "--config", str(PYPROJECT), "--no-cache", "--no-logo"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _broken(result: subprocess.CompletedProcess[str]) -> set[str]:
    by_name = {contract["name"]: contract_id for contract_id, contract in _contracts().items()}
    broken = {by_name[name] for name in by_name if f"{name} BROKEN" in result.stdout}
    kept = {by_name[name] for name in by_name if f"{name} KEPT" in result.stdout}
    # Every contract must report one way or the other; otherwise the output format changed.
    assert broken | kept == set(by_name.values()), result.stdout + result.stderr
    return broken


def _lint_copy(tmp_path: Path, plants: list[Plant]) -> tuple[int, set[str]]:
    src = tmp_path / "src"
    for name, folder in PACKAGES.items():
        shutil.copytree(folder, src / name, ignore=shutil.ignore_patterns("__pycache__"))
    for number, (package, statement) in enumerate(plants):
        (src / package / f"_planted_{number}.py").write_text(statement + "\n", encoding="utf-8")
    result = _run_lint_imports(tmp_path, src)
    return result.returncode, _broken(result)


# --- the configuration matches the document ---------------------------------------------------


def test_root_packages_are_the_three_workspace_packages() -> None:
    assert set(_config()["root_packages"]) == set(PACKAGES)


def test_domain_rule_has_its_contract() -> None:
    assert "- `domain` imports nothing from the project." in _module_rules()
    contract = _contracts()["domain-imports-nothing"]
    assert contract["type"] == "forbidden"
    assert contract["source_modules"] == ["hub.domain"]
    assert set(contract["forbidden_modules"]) == {"hub.*", "support_tools", "hub_evals"}


def test_services_rule_has_its_contract() -> None:
    match = re.search(r"^- (.+) do not import (.+)\.$", _module_rules(), flags=re.MULTILINE)
    assert match is not None, "the services rule is missing from design §8"
    sources = {f"hub.{name}" for name in re.findall(r"`(\w+)`", match.group(1))}
    forbidden = {f"hub.{name}" for name in re.findall(r"`(\w+)`", match.group(2))}
    contract = _contracts()["services-not-api-graph"]
    assert contract["type"] == "forbidden"
    assert set(contract["source_modules"]) == sources
    assert set(contract["forbidden_modules"]) == forbidden


def test_api_and_mcp_server_rule_has_its_contract() -> None:
    assert "`apps/mcp_server` and `apps/api` do not import each other." in _module_rules()
    contract = _contracts()["api-mcp-independent"]
    assert contract["type"] == "independence"
    assert set(contract["modules"]) == {"hub", "support_tools"}


def test_evals_and_mcp_server_rule_has_its_contract() -> None:
    assert "`evals` and `apps/mcp_server` do not import each other." in _module_rules()
    contract = _contracts()["evals-mcp-independent"]
    assert contract["type"] == "independence"
    assert set(contract["modules"]) == {"hub_evals", "support_tools"}


def test_litellm_rule_has_its_contract() -> None:
    assert "- Only `llm/gateway.py` imports `litellm`" in _module_rules()
    assert _config()["include_external_packages"] is True
    contract = _contracts()["only-gateway-imports-litellm"]
    # `protected` checks every module of the root packages, new ones included, and needs no
    # ignored or indirect imports.
    assert contract["type"] == "protected"
    assert contract["protected_modules"] == ["litellm"]
    assert contract["allowed_importers"] == ["hub.llm.gateway"]


def test_langfuse_rule_has_its_contract() -> None:
    assert "- Only `observability/tracing.py` imports `langfuse`" in _module_rules()
    contract = _contracts()["only-tracing-imports-langfuse"]
    assert contract["type"] == "protected"
    assert contract["protected_modules"] == ["langfuse"]
    assert contract["allowed_importers"] == ["hub.observability.tracing"]


def test_every_contract_comes_from_a_design_rule() -> None:
    # A contract without a rule in design §8 is a rule nobody agreed to.
    assert set(_contracts()) == {
        "domain-imports-nothing",
        "services-not-api-graph",
        "api-mcp-independent",
        "evals-mcp-independent",
        "only-gateway-imports-litellm",
        "only-tracing-imports-langfuse",
    }


def test_no_contract_is_weakened() -> None:
    # Ignored imports or allowed indirect imports would quietly switch a rule off (never-do 6).
    for contract_id, contract in _contracts().items():
        for option in ("ignore_imports", "allow_indirect_imports", "as_packages"):
            assert option not in contract, f"{contract_id} sets {option}"


# --- the contracts pass on the repository and fail on wrong imports ----------------------------


def test_the_repository_keeps_every_contract() -> None:
    result = _run_lint_imports(ROOT, None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _broken(result) == set()


SERVICES = ["guardrails", "kb", "llm", "tools", "memory", "audit"]

VIOLATIONS: list[tuple[str, list[Plant], set[str]]] = [
    ("domain->graph", [("hub/domain", "import hub.graph")], {"domain-imports-nothing"}),
    ("domain->db", [("hub/domain", "from hub.db import x")], {"domain-imports-nothing"}),
    ("domain->hub_evals", [("hub/domain", "import hub_evals")], {"domain-imports-nothing"}),
    (
        "domain->support_tools",
        [("hub/domain", "import support_tools")],
        {"domain-imports-nothing", "api-mcp-independent"},
    ),
    *[
        (
            f"{service}->{target}",
            [(f"hub/{service}", f"import hub.{target}")],
            {"services-not-api-graph"},
        )
        for service in SERVICES
        for target in ("api", "graph")
    ],
    (
        "kb->db->api (indirect)",
        # _planted_0 is the first plant: hub/db/_planted_0.py.
        [("hub/db", "import hub.api"), ("hub/kb", "import hub.db._planted_0")],
        {"services-not-api-graph"},
    ),
    ("api->support_tools", [("hub/api", "import support_tools")], {"api-mcp-independent"}),
    (
        "mcp adapters->hub",
        [("support_tools/adapters", "from hub import graph")],
        {"api-mcp-independent"},
    ),
    (
        "hub_evals->support_tools",
        [("hub_evals", "import support_tools")],
        {"evals-mcp-independent"},
    ),
    (
        "support_tools->hub_evals",
        [("support_tools", "import hub_evals")],
        {"evals-mcp-independent"},
    ),
    *[
        (
            f"{package}->litellm",
            [(package, statement)],
            {"only-gateway-imports-litellm"},
        )
        for package, statement in [
            ("hub/llm", "import litellm"),  # another module of the same package
            ("hub/graph", "from litellm import acompletion"),
            ("hub/kb", "import litellm.main"),
            ("hub", "import litellm"),
            ("hub_evals", "import litellm"),
            ("support_tools", "import litellm"),
        ]
    ],
    *[
        (
            f"{package}->langfuse",
            [(package, statement)],
            {"only-tracing-imports-langfuse"},
        )
        for package, statement in [
            ("hub/observability", "import langfuse"),  # another module of the same package
            ("hub/llm", "from langfuse import Langfuse"),
            ("hub/graph", "import langfuse"),
            ("hub_evals", "import langfuse"),
        ]
    ],
]


@pytest.mark.parametrize(
    ("plants", "expected"),
    [pytest.param(plants, expected, id=case) for case, plants, expected in VIOLATIONS],
)
def test_a_wrong_import_breaks_its_contract(
    tmp_path: Path, plants: list[Plant], expected: set[str]
) -> None:
    returncode, broken = _lint_copy(tmp_path, plants)
    assert returncode != 0
    assert broken == expected


def test_every_contract_has_a_violation_case() -> None:
    covered = set().union(*(expected for _, _, expected in VIOLATIONS))
    assert covered == set(_contracts())


def test_imports_the_design_allows_keep_every_contract(tmp_path: Path) -> None:
    allowed: list[Plant] = [
        ("hub_evals", "import hub.graph"),  # evals runs the production graph
        ("hub/graph", "import hub.kb, hub.llm, hub.tools, hub.memory, hub.audit"),
        ("hub/api", "import hub.graph, hub.db"),
        ("hub/kb", "import hub.llm, hub.domain"),
        ("hub/guardrails", "import hub.domain"),
        # domain is split into submodules (enums, models); `hub.*` in its contract must not
        # forbid that. _planted_6.py is the plant on the next line.
        ("hub/domain", "from hub.domain import _planted_6"),
        ("hub/domain", "X = 1"),
        # Reaching litellm through the gateway is the point of the gateway.
        ("hub/graph", "from hub.llm import gateway"),
    ]
    returncode, broken = _lint_copy(tmp_path, allowed)
    assert broken == set()
    assert returncode == 0


# --- litellm through the gateway's own names --------------------------------------------------
# import-linter sees module imports, not names: `from hub.llm.gateway import litellm` or
# `gateway.litellm.acompletion(...)` would call litellm outside the gateway and still keep the
# contract. This syntax check closes that; dynamic imports by string are refused too.

GATEWAY = "hub.llm.gateway"


def _is_gateway(node: ast.expr) -> bool:
    """`gateway` (as imported from hub.llm) or the dotted `hub.llm.gateway`."""
    if isinstance(node, ast.Name):
        return node.id == "gateway"
    return isinstance(node, ast.Attribute) and ast.unparse(node) == GATEWAY


def litellm_reach(source: str) -> list[str]:
    """Every way the source reaches litellm without an `import litellm` statement."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == GATEWAY:
            found += [f"import {a.name}" for a in node.names if a.name.startswith("litellm")]
        elif isinstance(node, ast.Attribute) and node.attr.startswith("litellm"):
            if _is_gateway(node.value):
                found.append(f"attribute {ast.unparse(node)}")
        elif isinstance(node, ast.Call) and ast.unparse(node.func).endswith(
            ("import_module", "__import__")
        ):
            first = node.args[0] if node.args else None
            name = first.value if isinstance(first, ast.Constant) else None
            if isinstance(name, str) and name.startswith("litellm"):
                found.append(f"dynamic import {name}")
    return found


@pytest.mark.parametrize(
    "source",
    [
        "from hub.llm.gateway import litellm",
        "from hub.llm.gateway import litellm_errors as errors",
        "from hub.llm import gateway\ngateway.litellm.acompletion()",
        "import hub.llm.gateway\nhub.llm.gateway.litellm_errors.Timeout",
        "import importlib\nimportlib.import_module('litellm')",
        "__import__('litellm.main')",
    ],
)
def test_reaching_litellm_through_the_gateway_is_found(source: str) -> None:
    assert litellm_reach(source)


@pytest.mark.parametrize(
    "source",
    [
        "from hub.llm.gateway import Gateway, build_gateway",
        "from hub.llm import gateway\ngateway.build_gateway",
        "import importlib\nimportlib.import_module('hub.llm.gateway')",
        "other.litellm_notes = 1",
    ],
)
def test_normal_use_of_the_gateway_is_not_found(source: str) -> None:
    assert litellm_reach(source) == []


def test_litellm_is_not_reached_through_the_gateway_module() -> None:
    gateway_file = PACKAGES["hub"] / "llm" / "gateway.py"
    offenders = {
        str(path.relative_to(ROOT)): found
        for folder in PACKAGES.values()
        for path in sorted(folder.rglob("*.py"))
        if path != gateway_file and (found := litellm_reach(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}
