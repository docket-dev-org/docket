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
    which is the same thing with a smaller blast radius), so that `pip install docket-ai` keeps
    working with neither extra installed.
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


def test_no_definition_in_the_package_is_dead_code() -> None:
    """Every public definition under `src/docket/` is called from somewhere a reader can reach.

    `generate_keypair` sat in `src/docket/signing.py` from the first release and nothing called
    it: no command, no test, no documentation. `docket run --sign` was a flag with no way to
    produce a key, and the function that could have produced one was invisible. Seven more
    definitions had the same shape — `short_hash`, `mb`, `money_or_zero`, `stable_seed`,
    `is_known`, `default_price_table`, `price_document_digest`, `_gzip_bytes` — all one- or
    two-line helpers with no caller anywhere in the tree.

    Dead code is not harmless here. This is a project whose pitch is that a receipt says what the
    tool did; a package that carries functions nothing runs is making the same kind of claim
    about itself. The rule is deliberately narrow: a *top-level* function or class in `src/` that
    appears nowhere else in `src/`, `tests/`, `tools/`, `examples/` or `verify-js/` is dead. A
    dunder is exempt, and so is a name that appears in `__all__`, because that is a deliberate
    re-export.
    """
    import ast
    import re

    root = Path(__file__).resolve().parents[1]
    search_roots = [root / "src", root / "tests", root / "tools", root / "examples", root / "verify-js"]
    corpus: list[str] = []
    for directory in search_roots:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".mjs", ".js"}:
                corpus.append(path.read_text(encoding="utf-8", errors="replace"))
    everything = "\n".join(corpus)
    assert len(corpus) > 50, "the search roots look wrong; too few files were read"

    dead: list[str] = []
    for module in sorted((root / "src" / "docket").rglob("*.py")):
        relative = module.relative_to(root).as_posix()
        text = module.read_text(encoding="utf-8")
        exported = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        try:
                            exported = set(ast.literal_eval(node.value))
                        except (ValueError, TypeError):
                            exported = set()
        for node in ast.parse(text).body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if name.startswith("__") or name in exported:
                continue
            if len(re.findall(rf"\b{re.escape(name)}\b", everything)) <= 1:
                dead.append(f"{relative}:{node.lineno} {name}")

    assert not dead, (
        "definitions in src/docket that nothing calls: " + repr(dead) + ". Either wire them up, "
        "or delete them: a package that carries functions nothing runs is claiming more than it does."
    )


def test_the_typing_classifier_is_backed_by_a_marker_and_a_clean_check() -> None:
    """`Typing :: Typed` and `py.typed` are a claim, and the claim has three parts.

    The classifier said the package was typed and the wheel shipped no `py.typed`, so no
    consumer's type checker would read the annotations at all. That is invisible from the
    repository: `pip install` succeeds, the tests pass, and nothing compares the claim to the
    artefact. Two of the three parts are checked here, offline:

    * `src/docket/py.typed` exists, so the annotations ship;
    * `[project]` declares the classifier and `requires-python` together, because a marker
      without the classifier is the same claim made only half-way.

    The third part — that the annotations are *correct* — is mypy's, and it runs in CI. That is
    the part that mattered: the package was annotated all along, and 65 of those annotations
    were wrong, which nobody could see because nothing asked. Running mypy here as well would
    need mypy in the test dependencies, which the package deliberately does not have.
    """
    import tomllib

    root = Path(__file__).resolve().parents[1]
    marker = root / "src" / "docket" / "py.typed"
    assert marker.is_file(), (
        "src/docket/py.typed is missing: the Typing :: Typed classifier is a claim no type "
        "checker can act on without it"
    )

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert "Typing :: Typed" in project.get("classifiers", []), (
        "the Typing :: Typed classifier is gone while src/docket/py.typed ships: the marker would be an unclaimed one"
    )
    assert project.get("requires-python", "").startswith(">=3.11"), "the typing claim needs 3.11+"


def test_the_version_is_written_in_exactly_one_place() -> None:
    """Every claim about which version is running must come from one derivation.

    The version was written three times — `__init__.py`'s `__version__`, `receipt.py`'s
    `TOOL_VERSION`, and `supervisor.py`'s `DOCKET_TOOL_VERSION` — and each had to be edited by
    hand on every release. Two of the three were still `0.1.0` when the distribution installed
    0.1.2, so `docket --version` said one thing, `pip show` said another, and every receipt
    written since 0.1.0 named a tool version that was not the one running. A receipt that
    misreports the tool that wrote it makes a false claim about provenance, which is the one
    thing a receipt exists to make checkable.

    All three now import `__version__` from `src/docket/_version.py`, which derives it from the
    installed distribution, falls back to the nearest `pyproject.toml`, and then to `unknown`.
    This test pins the property that made the bug possible: **no module under `src/` may carry a
    version literal**. Scanning only `__init__.py` is what let the other two literals survive
    the first fix.

    It also asserts the derivation answers with `pyproject.toml`'s version when run from a source
    tree, because that is the case a contributor hits and the one where a wrong answer is least
    visible.
    """
    import os
    import re
    import subprocess

    root = Path(__file__).resolve().parents[1]
    declared = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]

    # No module under src/ may carry a version literal. The pattern matches a name containing
    # "version" bound to a quoted semver-looking string, by either `=` or `:`, so it catches the
    # assignment form (`TOOL_VERSION = "0.1.0"`) and the dict-entry form the supervisor used
    # (`"DOCKET_TOOL_VERSION": "0.1.0"`). Scanning only `__init__.py`, or only assignments, is
    # what let the other two literals survive the first fix.
    # The name may be quoted (a dict key) or bare (an assignment), so the closing quote is
    # optional. Both forms were in use: `TOOL_VERSION = "0.1.0"` and `"DOCKET_TOOL_VERSION":
    # "0.1.0"`.
    version_binding = re.compile(
        r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)"?\s*[:=]\s*"(?P<value>[0-9]+\.[0-9]+\.[0-9]+[^"]*)"'
    )
    literals: list[str] = []
    for module in sorted((root / "src" / "docket").rglob("*.py")):
        for number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), start=1):
            match = version_binding.search(line)
            if match and "version" in match.group("name").lower():
                literals.append(f"{module.relative_to(root).as_posix()}:{number}: {line.strip()}")
    assert not literals, "a version literal is written in the source again; it must come from _version.py: " + repr(
        literals
    )

    # In a source tree the version comes from pyproject.toml, so it must equal the declared one.
    # The probe runs in a subprocess so that this test process's own imports cannot answer.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    probe = subprocess.run(
        [sys.executable, "-c", "import docket; print(docket.__version__)"],
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        check=True,
    )
    reported = probe.stdout.strip()
    assert reported == declared, f"source tree reports {reported!r}, pyproject.toml says {declared!r}"


def test_every_ci_step_that_installs_the_package_names_the_distribution() -> None:
    """A CI step that installs by the wrong name fails the job it was written to prove.

    The `package` job builds the wheel and then installs it into a clean venv to check that a
    user can. It installed `docket` by name, which stopped resolving the moment the
    distribution became `docket-ai`, and the job went red on a build that was fine. Nothing in
    the suite noticed, because nothing builds a wheel.

    The distribution name is read from `pyproject.toml` rather than repeated, so this asserts
    the same thing the job does. PyPI prohibits the bare name `docket`, so the distribution and
    the command genuinely differ, and a test that only checked "the word docket appears" would
    pass while the job was broken.
    """
    import tomllib

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    distribution = config["project"]["name"]

    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    assert workflows, "no workflows found"
    offenders: list[str] = []
    for workflow in workflows:
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), start=1):
            if "pip install" not in line or "--find-links" not in line:
                continue
            if "$DIST" in line or f"install {distribution}" in line:
                continue
            if "find-links" in line and distribution not in line:
                offenders.append(f"{workflow.relative_to(root)}:{number}: {line.strip()}")
    assert not offenders, (
        "a CI step installs the package by a name that is not the distribution in "
        f"pyproject.toml ({distribution!r}): " + "; ".join(offenders)
    )
