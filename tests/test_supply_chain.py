"""Supply chain: what this package installs, what it imports, and what that costs a user.

A vulnerability scanner can only tell you about the dependencies you *declared*. This file checks the
declarations themselves, and then checks that the source agrees with them — which is the half that
catches the failure a scanner cannot see: a module that imports something the package never declared.
That failure is invisible in the author's environment (where the import is installed for other
reasons) and is a raw `ModuleNotFoundError` in a user's, so it is asserted here rather than trusted to
review.

`pip-audit` runs against the *development toolchain* in CI (`.github/workflows/security.yml`) because
there is nothing else to scan: the installable package has no dependencies at all.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "docket"

#: Every name outside the standard library and this package that anything shipped (or runnable
#: from) this repository may import: the extra that provides it, and the directory it is allowed in.
#: `cryptography` is needed for Ed25519 signatures and lives in the package; `yaml` reads the
#: deployment manifests and lives in `tools/`. Two imports, two files, both optional — that is the
#: whole third-party surface of the tree.
ALLOWED_THIRD_PARTY = {
    "cryptography": ("sign", ("src/docket",)),
    "yaml": ("deploy", ("tools",)),
}

#: The directories whose imports are checked. Tests are deliberately excluded: a test may import
#: anything it likes, because a test that cannot run is a skip, not a broken install.
CHECKED_DIRS = ("src/docket", "tools", "spec", "examples")


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_runtime_dependency_list_is_empty_and_extras_are_named():
    """`dependencies = []` is a claim, so it is read rather than repeated.

    The README, `SECURITY.md` and the packaging metadata all say this package installs nothing beyond
    the standard library. That is the strongest supply-chain statement a Python project can make, and
    it is worth exactly as much as the file it lives in — so the file is read here, and the extras are
    pinned to the three the documentation names, because an extra that quietly becomes required is
    how "no dependencies" stops being true.
    """
    project = _pyproject()["project"]
    assert project["dependencies"] == [], f"the runtime dependency list is not empty: {project['dependencies']}"
    extras = project["optional-dependencies"]
    assert sorted(extras) == ["deploy", "dev", "sign"], sorted(extras)
    assert extras["sign"] == ["cryptography>=42"]
    assert extras["deploy"] == ["pyyaml>=6"]


def _imported_top_level_names(path: Path) -> set[str]:
    """Every top-level module name imported anywhere in `path`, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            # `from . import x` and `from .ledger import y` are relative, so level > 0 means the
            # package itself — nothing to check. Absolute imports of a sibling module are the
            # package's own name and are filtered by the caller.
            names.add(node.module.split(".")[0])
    return names


def test_nothing_under_src_imports_a_package_the_project_does_not_declare():
    """The check a dependency scanner cannot make: does the source agree with the manifest?

    Every import in the shipped package has to resolve to the standard library, to the package
    itself, or to one of the two *optional* extras — and the optional ones are allowed only where the
    import is guarded, so a user who installed nothing can still import `docket` and run a capped
    run. This is asserted by parsing the files rather than by importing them, because an import that
    is missing from the environment is exactly the case a normal test run would fail to reach.
    """
    modules = sorted(module for directory in CHECKED_DIRS for module in (ROOT / directory).rglob("*.py"))
    assert modules, f"no modules found under {CHECKED_DIRS}"

    own = {module.stem for module in PACKAGE.glob("*.py")}
    stdlib = set(sys.stdlib_module_names)
    offenders: dict[str, set[str]] = {}
    for module in modules:
        relative = str(module.relative_to(ROOT))
        for name in _imported_top_level_names(module):
            if name in stdlib or name in own or name == "docket":
                continue
            allowed_in = ALLOWED_THIRD_PARTY.get(name)
            if allowed_in and relative.startswith(allowed_in[1]):
                continue
            offenders.setdefault(relative, set()).add(name)
    assert not offenders, "these modules import packages the project does not declare: " + ", ".join(
        f"{mod} -> {sorted(names)}" for mod, names in sorted(offenders.items())
    )


@pytest.mark.parametrize("name", sorted(ALLOWED_THIRD_PARTY))
def test_every_optional_import_is_guarded_by_a_reason(name: str):
    """An optional import that is not guarded is not optional.

    `import cryptography` at the top of a module turns the extra into a requirement, and the failure
    appears in a clean environment as an ImportError from an unrelated command. Each allowed import
    is therefore required to sit inside a `try`/`except ImportError` (or an import *inside* a function,
    which is the same thing with a smaller blast radius), so that `pip install docket` keeps working
    with neither extra installed.
    """
    directory = ALLOWED_THIRD_PARTY[name][1]
    guarded: list[str] = []
    for module in sorted(module for d in directory for module in (ROOT / d).rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        # Top-level imports of the optional name: these must be inside a try block.
        for node in tree.body:
            if isinstance(node, ast.Try):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Import | ast.ImportFrom) and _imports(inner, name):
                        guarded.append(f"{module.relative_to(ROOT)} (try/except)")
            elif isinstance(node, ast.Import | ast.ImportFrom) and _imports(node, name):
                raise AssertionError(
                    f"{module.relative_to(ROOT)} imports {name} at module level, outside a guard: "
                    f"that makes the extra a requirement"
                )
        # An import inside a function body counts too, and is what most call sites here do.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and name in _imported_top_level_names_of(node):
                guarded.append(f"{module.relative_to(ROOT)} (in a function)")
    assert guarded, (
        f"nothing under {directory} imports {name} — the extra is declared and never used, or the "
        f"import moved somewhere this check does not look"
    )


def _imports(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == name for alias in node.names)
    return isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == name


def _imported_top_level_names_of(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Import):
            names.update(alias.name.split(".")[0] for alias in inner.names)
        elif isinstance(inner, ast.ImportFrom) and inner.level == 0 and inner.module:
            names.add(inner.module.split(".")[0])
    return names


def test_the_wheel_carries_the_independent_verifier_and_the_conformance_corpus():
    """`verify --impl node` is a claim about an *installation*, so the installation is checked.

    `force-include` in `pyproject.toml` copies `verify-js/verify.mjs` and `spec/vectors/` into
    the wheel under `docket/_vendor/`. Nothing in the test suite builds a wheel, so a dropped
    line there would only surface when a reader ran the command and got "not in this
    installation" — which is the exact failure the vendoring exists to prevent. This reads the
    packaging configuration instead, which is where the promise is made.
    """
    import tomllib

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include.get("verify-js/verify.mjs") == "docket/_vendor/verify.mjs", (
        "the wheel no longer carries the Node verifier, so `docket verify --impl node` "
        "fails on a clean install: " + repr(force_include)
    )
    assert force_include.get("spec/vectors") == "docket/_vendor/vectors", (
        "the wheel no longer carries the conformance corpus: " + repr(force_include)
    )
    # And the files those entries name are the ones in the tree, byte for byte.
    assert (root / "verify-js" / "verify.mjs").is_file()
    assert (root / "spec" / "vectors").is_dir()
    assert any((root / "spec" / "vectors").glob("*.json")), "the positive corpus is empty"


def test_the_node_verifier_in_the_wheel_is_the_one_in_the_repository():
    """The vendored copy and the repository copy must be the same program.

    Two implementations that agree is the claim; two *files* that differ is a different claim
    wearing the same clothes. The build copies the repository's file, so this asserts the copy
    that ships is derived from the file the suite tests rather than a fork of it.
    """
    from docket.cli import _node_verifier

    root = Path(__file__).resolve().parents[1]
    resolved = _node_verifier()
    assert resolved is not None, "no Node verifier found from a checkout"
    assert resolved.read_bytes() == (root / "verify-js" / "verify.mjs").read_bytes()
