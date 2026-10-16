#!/usr/bin/env python3
"""Compare the deploy checker's render of the Helm chart against a real Helm render.

`tools/deploy_check.py` renders `deploy/helm/docket` through a *declared subset* of the template
language, and refuses anything outside that subset by name. That makes it runnable anywhere — no
Helm, no cluster, no network — and it makes one claim necessary that the checker on its own cannot
support: **that the render is the render a cluster would get.**

This tool is how that claim is checked rather than asserted. It runs the real Helm client
(`helm template`), parses both renders into documents keyed by `kind` and `metadata.name`, and
compares them field for field, reporting the path of the first difference inside a document. It is
not part of `make check`: CI has no Helm, and a check that cannot run must not quietly pass. Instead
it exits **2** with a reason when Helm is missing — the same convention as `tools/deploy_check.py` —
and it is expected to be run by a maintainer who has Helm, and by anyone reviewing a change to the
chart.

    $ python3 tools/helm_fidelity.py
    5/5 documents identical field for field (helm v3.16.3)

The last recorded run: 2026-10-15, Helm `v3.16.3` (`GitCommit cfd0749`), same five documents,
identical field for field. It is also what found the integer defect: a number in `values.yaml`
reaches a template as a `float64` (Helm loads values through YAML → JSON → `map[string]any`), so a
large integer renders as `6.7108864e+07` unless the template pipes it through `int64` — a policy
Docket's own parser happens to accept, because it takes any finite whole number, and which a
stricter reader would not. The checker now reproduces Helm's formatting, and this tool is what says
the reproduction is faithful.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHART = ROOT / "deploy" / "helm" / "docket"


def _load_checker() -> Any:
    """Import `tools/deploy_check.py` by path so this file can reuse its renderer.

    The checker is a script, not a package module, and it is registered in `sys.modules` before
    execution for the reason its own tests document: a module executed without that registration has
    no entry for dataclasses and other runtime introspection to find by name.
    """
    path = ROOT / "tools" / "deploy_check.py"
    spec = importlib.util.spec_from_file_location("docket_deploy_check", path)
    if spec is None or spec.loader is None:  # pragma: no cover - the file is in the tree
        raise SystemExit("helm_fidelity: could not load tools/deploy_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["docket_deploy_check"] = module
    spec.loader.exec_module(module)
    return module


def documents_from_stream(text: str, where: str, yaml: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Split a rendered stream on `---` and key each document by `(kind, name)`.

    Keying by kind-and-name rather than by position is the point: two renders can order their
    documents differently and still agree about the objects a cluster would create, and two renders
    that agree on position but not on content are exactly the failure this tool exists to find.
    Documents that are not mappings, and empty splits between separators, are skipped — a rendered
    `NOTES.txt` and a trailing `---` are neither.
    """
    found: dict[tuple[str, str], dict[str, Any]] = {}
    # Split on a separator *line*, not on the characters "---" anywhere: splitting on "\n---"
    # would eat the newline that ends the previous document's last line, which for a block scalar
    # is part of the value (`policy.toml: |` ends with exactly one newline, by YAML's rules). That
    # one character was the first difference this tool reported, and it was the tool's bug.
    for index, chunk in enumerate(re.split(r"(?m)^---[ \t]*$", text)):
        try:
            parsed = yaml.safe_load(chunk)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            raise SystemExit(f"helm_fidelity: {where}: document {index + 1} is not YAML: {exc}") from exc
        if not isinstance(parsed, dict) or "kind" not in parsed:
            continue
        metadata = parsed.get("metadata") or {}
        key = (str(parsed["kind"]), str(metadata.get("name", "")))
        if key in found:
            raise SystemExit(f"helm_fidelity: {where}: two documents named {key[0]}/{key[1]}")
        found[key] = parsed
    return found


def first_difference(left: Any, right: Any, path: str = "") -> str | None:
    """The path of the first difference between two parsed documents, or `None` if they are equal."""
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                return f"{path}.{key} (only in the checker's render)"
            if key not in right:
                return f"{path}.{key} (only in the Helm render)"
            found = first_difference(left[key], right[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return f"{path} (length {len(left)} versus {len(right)})"
        for index, (one, other) in enumerate(zip(left, right, strict=True)):
            found = first_difference(one, other, f"{path}[{index}]")
            if found:
                return found
        return None
    if left != right:
        return f"{path} ({left!r} versus {right!r})"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chart", type=Path, default=DEFAULT_CHART, help=f"chart directory (default {DEFAULT_CHART})")
    parser.add_argument("--release", default="docket", help="release name to render with (default docket)")
    parser.add_argument("--helm", default="helm", help="the Helm client to run (default: helm on PATH)")
    args = parser.parse_args(argv)

    executable = shutil.which(args.helm)
    if executable is None:
        print(
            f"helm_fidelity: {args.helm!r} is not installed, so the checker's render cannot be compared "
            "against a real one. This is not a pass.\n"
            "Install Helm (https://helm.sh/docs/intro/install/) and run this again; it is deliberately not "
            "part of `make check`, because CI has no Helm and a check that cannot run must say so.",
            file=sys.stderr,
        )
        return 2

    checker = _load_checker()
    yaml = checker._yaml(where=None)
    try:
        rendered = subprocess.run(
            [executable, "template", args.release, str(args.chart)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Reachable without a broken environment: a client on PATH that the kernel cannot
        # execute raises here, and `tests/test_helm_fidelity.py` drives exactly that.
        print(f"helm_fidelity: could not run {executable}: {exc}", file=sys.stderr)
        return 2
    if rendered.returncode != 0:
        print(f"helm_fidelity: helm template failed ({rendered.returncode}):\n{rendered.stderr}", file=sys.stderr)
        return 2

    helm_documents = documents_from_stream(rendered.stdout, "helm template", yaml)
    by_template = checker.render_chart(args.chart)
    checker_documents: dict[tuple[str, str], dict[str, Any]] = {}
    skipped: list[str] = []
    for name, text in sorted(by_template.items()):
        if not name.endswith((".yaml", ".yml")):
            # `NOTES.txt` is not a manifest and not YAML: Helm prints it to the user on install, and
            # `helm template` does not emit it at all. Comparing it here would be comparing a file
            # Helm never renders in this mode; the checker still renders it, so a broken note is
            # caught by the checker's own tests.
            skipped.append(name)
            continue
        for key, document in documents_from_stream(text, f"the checker's render of {name}", yaml).items():
            if key in checker_documents:
                raise SystemExit(f"helm_fidelity: the checker renders {key[0]}/{key[1]} twice")
            checker_documents[key] = document

    problems: list[str] = []
    for key in sorted(set(helm_documents) | set(checker_documents)):
        if key not in helm_documents:
            problems.append(f"{key[0]}/{key[1]}: rendered by the checker, not by Helm")
            continue
        if key not in checker_documents:
            problems.append(f"{key[0]}/{key[1]}: rendered by Helm, not by the checker")
            continue
        difference = first_difference(helm_documents[key], checker_documents[key])
        if difference:
            problems.append(f"{key[0]}/{key[1]}: {difference}")

    version = subprocess.run([executable, "version", "--short"], capture_output=True, text=True, check=False)
    label = version.stdout.strip() or "unknown version"
    if problems:
        print(f"{len(problems)} difference(s) between the Helm render and the checker's ({label}):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    total = len(helm_documents)
    note = f" (checked every YAML template; {' and '.join(skipped)} is not one)" if skipped else ""
    print(f"{total}/{total} documents identical field for field ({label}){note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
