"""Every third-party module a package imports is declared in its pyproject.

The uv workspace installs all packages into one environment, so a missing
dependency only shows in the Docker image of that package (a coordinator that
cannot start). This catches it in the unit tests.
"""

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "shared": "pos_shared",
    "coordinator": "pos_coordinator",
    "worker": "pos_worker",
    "agent": "pos_agent",
}
# import name -> distribution name, where they differ
DIST = {
    "argon2": "argon2-cffi",
    "pydantic_settings": "pydantic-settings",
    "yaml": "pyyaml",
}
# provided by a declared distribution (e.g. starlette comes with fastapi)
PROVIDED_BY = {"starlette": "fastapi", "httpcore": "httpx", "anyio": "httpx"}
FIRST_PARTY = set(PACKAGES.values())


def _imports(package_dir: Path) -> set[str]:
    names = set()
    for path in package_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _declared(project: str) -> set[str]:
    deps = tomllib.loads((ROOT / project / "pyproject.toml").read_text())["project"]["dependencies"]
    return {re.split(r"[\[<>=!~ ;]", d, maxsplit=1)[0].lower() for d in deps}


@pytest.mark.parametrize("project", sorted(PACKAGES))
def test_imports_are_declared(project):
    declared = _declared(project)
    if project != "shared":
        declared |= _declared("shared")  # pos-shared's dependencies come along
    third_party = _imports(ROOT / project / PACKAGES[project]) - FIRST_PARTY
    third_party -= set(sys.stdlib_module_names)
    missing = []
    for name in sorted(third_party):
        dist = DIST.get(name, name).replace("_", "-").lower()
        if dist in declared or PROVIDED_BY.get(name) in declared:
            continue
        missing.append(name)
    # direct imports of transitive packages are fine only where listed in PROVIDED_BY;
    # anything else must be in the package's own pyproject
    assert missing == [], f"{project} imports undeclared packages: {missing}"
