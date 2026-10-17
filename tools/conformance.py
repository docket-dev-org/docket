#!/usr/bin/env python3
"""Run any receipt verifier over the conformance corpus, and say exactly where it disagrees.

The corpus in `spec/vectors/` is the format's contract: seven positive vectors that a conforming
implementation must accept, and `spec/vectors/invalid/` — twenty-six documents that it must refuse,
each with a machine-readable expectation naming the check that has to catch it.

This tool exists so that a third party can point it at *their* verifier:

    python3 tools/conformance.py --cmd "my-verifier {file} --json --ledger {ledger}"
    python3 tools/conformance.py --python --node        # the two that ship here
    python3 tools/conformance.py --list                 # the registry

Every command is a template. `{file}` is replaced with the vector, `{ledger}` with the ledger the
expectation names (and the vector is run without a ledger at all if the command does not mention it).
The verifier must print the JSON shape the format's verifiers share — `{ok, checks: [{name, status,
detail}]}` — because a corpus cannot judge a verifier whose verdicts it cannot read.

What a result means:

* `pass`      — a positive vector was accepted, or a negative one was refused with the stated check;
* `wrong-ok`  — the verifier **accepted a document it must refuse**. This is the failure the corpus exists
                to find, and no amount of other passes compensates for it;
* `wrong-check` — it refused, but by a different rule than the vector names. Usually a real finding:
                either the vector's expectation is wrong, or the verifier reports the symptom instead
                of the cause;
* `not-judged` — the verifier reports `skip` for the check the vector is about. A declared gap, never a
                pass: an implementation that cannot check something has not checked it.

The exit code is 0 only when every vector was judged and judged correctly. `--allow-gaps` reports
coverage and exits 0 when nothing was *wrong*, for the honest intermediate state of an implementation
that declares which checks it does not yet perform.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
SPEC = REPO / "spec"
VECTORS = SPEC / "vectors"
INVALID = VECTORS / "invalid"
REGISTRY = SPEC / "registry.json"

#: The two shipped verifiers, so `--python` and `--node` are conveniences rather than a second config.
SHIPPED = {
    "python": '"{python}" -m docket verify "{file}" --json{ledger_flag}',
    "node": 'node verify-js/verify.mjs "{file}" --json{ledger_flag}',
    "go": 'verify-go/docket-verify "{file}" --json{ledger_flag}',
}


def registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def positive_vectors() -> list[pathlib.Path]:
    """The vectors a conforming implementation must accept: every JSON document that is a receipt."""
    out: list[pathlib.Path] = []
    for path in sorted(VECTORS.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(document, dict) and document.get("format") == "docket/receipt/v1":
            out.append(path)
    return out


def negative_vectors() -> list[tuple[pathlib.Path, dict]]:
    out: list[tuple[pathlib.Path, dict]] = []
    for expectation in sorted(INVALID.glob("*.expected.json")):
        vector = expectation.with_name(expectation.name.replace(".expected.json", ".json"))
        out.append((vector, json.loads(expectation.read_text(encoding="utf-8"))))
    return out


def command_for(template: str, vector: pathlib.Path, ledger: pathlib.Path | None) -> list[str] | None:
    """The argv for one run, or None if this vector needs a ledger the template cannot be given.

    Two placeholders are understood: `{ledger_flag}` becomes ` --ledger "<path>"` or the empty string,
    and `{ledger}` is the path alone. A template that mentions neither cannot be run against a vector
    that needs a ledger, and the caller records that as `not-judged` rather than running it against a
    receipt whose chain checks will simply skip.
    """
    if ledger is not None and "{ledger" not in template:
        return None
    text = template.replace("{python}", sys.executable)
    text = text.replace("{file}", str(vector))
    ledger_flag = "" if ledger is None else f' --ledger "{ledger}"'
    # `--ledger "{ledger}"` written by hand is equivalent to the `{ledger_flag}` shorthand.
    text = text.replace('--ledger "{ledger}"', "{ledger_flag}")
    text = text.replace("{ledger}", str(ledger) if ledger else "")
    text = text.replace("{ledger_flag}", ledger_flag)
    return shlex.split(text)


def run_one(argv: list[str], timeout: int = 180) -> dict:
    # The reference verifier is imported from `src/`, not installed, when this runs in a checkout: the
    # same environment the test suite uses. A third party's command inherits it too, which is harmless
    # (their interpreter ignores a path that has no module they import) and keeps the tool usable from a
    # fresh clone with nothing installed.
    env = dict(os.environ)
    src = str(REPO / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # `tempfile.gettempdir()` rather than a literal "/tmp": the verifier writes its scratch files
    # under TMPDIR, and a hardcoded path is both a portability bug and the shape of a symlink attack
    # (bandit B108, which flagged this line when it was written).
    env.setdefault("TMPDIR", tempfile.gettempdir())
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=str(REPO), env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"kind": "error", "detail": str(exc)[:200], "returncode": None}
    payload = None
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = None
    return {
        "kind": "ran",
        "returncode": completed.returncode,
        "payload": payload,
        "stdout": completed.stdout[:400],
        "stderr": completed.stderr[:400],
    }


def classify(result: dict, expectation: dict) -> tuple[str, str]:
    """(verdict, detail) for one vector against one verifier."""
    if result["kind"] == "error":
        return "error", result["detail"]
    payload = result.get("payload")
    reject = expectation.get("reject")
    if reject is not None:
        # A parse-level rejection: the verifier exits non-zero without a JSON verdict, and says why.
        if result["returncode"] != 0 and payload is None:
            needle = reject.get("reason_contains", "")
            if needle.lower() in (result.get("stderr") or "").lower():
                return "pass", f"rejected: {needle}"
            return (
                "wrong-reason",
                f"rejected without mentioning {needle!r}: {(result.get('stderr') or '').strip()[:120]}",
            )
        return "wrong-ok", "the document was read and judged; it must be rejected before any check runs"
    if payload is None:
        return (
            "error",
            f"exit {result['returncode']} with no JSON verdict: {(result.get('stderr') or '').strip()[:120]}",
        )
    checks = payload.get("checks") or []
    failures = [c for c in checks if c.get("status") == "fail"]
    expected = expectation.get("check")
    if expected is None:
        return (
            ("pass", "accepted")
            if payload.get("ok")
            else ("wrong-check", f"refused: {failures[0].get('name') if failures else 'no failing check'}")
        )
    if failures:
        first = failures[0].get("name")
        if first == expected:
            return "pass", f"refused by {expected}"
        return "wrong-check", f"refused by {first!r}, the vector names {expected!r}"
    skips = {c.get("name") for c in checks if c.get("status") == "skip"}
    if expected in skips:
        return "not-judged", f"the verifier skips {expected!r}"
    return "wrong-ok", "the invalid document was accepted"


def evaluate(template: str, name: str, jobs: int) -> list[dict]:
    """Every vector against one verifier."""
    tasks: list[tuple[str, pathlib.Path, pathlib.Path | None, dict]] = []
    for vector in positive_vectors():
        tasks.append(("positive", vector, None, {"check": None}))
    for vector, expectation in negative_vectors():
        ledger = expectation.get("ledger")
        ledger_path = None if not ledger else INVALID / ledger
        tasks.append(("negative", vector, ledger_path, expectation))

    def work(task: tuple) -> dict:
        kind, vector, ledger_path, expectation = task
        argv = command_for(template, vector, ledger_path)
        if argv is None:
            return {
                "verifier": name,
                "vector": vector.name,
                "kind": kind,
                "verdict": "not-judged",
                "detail": "the command template cannot be given a ledger, which this vector needs",
                "expected": expectation.get("check"),
            }
        verdict, detail = classify(run_one(argv), expectation)
        return {
            "verifier": name,
            "vector": vector.name,
            "kind": kind,
            "verdict": verdict,
            "detail": detail,
            "expected": expectation.get("check"),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return list(pool.map(work, tasks))


def observed_capabilities(template: str, name: str, jobs: int) -> set[str]:
    """Every check id a verifier emits across the whole corpus.

    The registry's `capabilities` table says which ids each implementation emits. Maintaining that by
    hand failed exactly as hand-maintained tables do: the Node verifier was recorded as emitting
    twelve ids when it emitted eighteen, because the person writing the table read the source rather
    than the output. This derives the set from the verdicts instead — a check that never appears in
    any run is not emitted, whatever the source says — and `--capabilities` compares the result with
    the registry and fails when they differ.
    """
    seen: set[str] = set()
    tasks = [(vector, None) for vector in positive_vectors()]
    tasks += [
        (vector, None if not expectation.get("ledger") else INVALID / expectation["ledger"])
        for vector, expectation in negative_vectors()
    ]

    def work(task: tuple[pathlib.Path, pathlib.Path | None]) -> list[str]:
        vector, ledger_path = task
        argv = command_for(template, vector, ledger_path)
        if argv is None:
            return []
        payload = run_one(argv).get("payload") or {}
        return [c.get("name", "") for c in payload.get("checks") or [] if c.get("name")]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for names in pool.map(work, tasks):
            seen.update(names)
    return seen


def capability_report(templates: list[tuple[str, str]], jobs: int) -> tuple[dict[str, list[str]], list[str]]:
    """{verifier: sorted ids} and the disagreements with `spec/registry.json`."""
    declared = registry()["capabilities"]
    found = {name: sorted(observed_capabilities(template, name, jobs)) for name, template in templates}
    problems: list[str] = []
    for name, ids in found.items():
        if name not in declared:
            problems.append(f"{name}: emits {len(ids)} ids but has no row in the registry")
            continue
        # Ids the corpus cannot reach are exempt from the completeness half of the comparison — but
        # only because the registry says why, in `conditional`. An exemption with a stated reason is
        # a documented gap; an exemption without one would be the table quietly covering for code.
        conditional = declared.get(f"{name}_conditional") or {}
        missing = sorted(set(ids) - set(declared[name]))
        extra = sorted(set(declared[name]) - set(ids) - set(conditional))
        if missing:
            problems.append(f"{name}: emits ids the registry does not list: {', '.join(missing)}")
        if extra:
            problems.append(f"{name}: the registry lists ids it never emits: {', '.join(extra)}")
        for identifier, why in conditional.items():
            if not str(why).strip():
                problems.append(f"{name}: {identifier} is conditional with no reason stated")
    valid = {entry["id"] for entry in registry()["ids"]}
    for name, ids in found.items():
        for identifier in ids:
            if identifier not in valid:
                problems.append(f"{name}: emits {identifier!r}, which is not in the registry")
    return found, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--cmd",
        action="append",
        default=[],
        metavar="TEMPLATE",
        help="a verifier command; {file} and {ledger} are substituted",
    )
    parser.add_argument("--python", action="store_true", help="the reference verifier in this repository")
    parser.add_argument("--node", action="store_true", help="the Node verifier in this repository")
    parser.add_argument("--go", action="store_true", help="verify-go/docket-verify, if it has been built")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--list", action="store_true", help="print the check-id registry and exit")
    parser.add_argument(
        "--capabilities", action="store_true", help="print the ids each verifier emits, and hold the registry to it"
    )
    parser.add_argument(
        "--allow-gaps", action="store_true", help="exit 0 when nothing is wrong, even if some vectors were not judged"
    )
    args = parser.parse_args()

    if args.list:
        data = registry()
        print(f"{'id':26s} emitted  assertion")
        for entry in data["ids"]:
            print(f"{entry['id']:26s} {str(entry['emitted']):7s}  {entry['assertion']}")
        print("\ncapabilities:")
        for verifier, ids in data["capabilities"].items():
            if verifier == "note":
                continue
            print(f"  {verifier:8s} {len(ids):2d} ids")
        return 0

    templates: list[tuple[str, str]] = []
    for flag, key in (("python", "python"), ("node", "node"), ("go", "go")):
        if getattr(args, flag):
            templates.append((key, SHIPPED[key]))
    for index, template in enumerate(args.cmd, 1):
        templates.append((f"cmd{index}", template))
    if not templates:
        parser.error("give at least one verifier: --python, --node, --go or --cmd")

    if args.capabilities:
        found, problems = capability_report(templates, args.jobs)
        for name, ids in found.items():
            print(f"{name}: {len(ids)} ids")
            print("  " + " ".join(ids))
        for problem in problems:
            print(f"DISAGREES {problem}")
        return 1 if problems else 0

    report: list[dict] = []
    for name, template in templates:
        report.extend(evaluate(template, name, args.jobs))

    bad = [r for r in report if r["verdict"] in {"wrong-ok", "wrong-check", "wrong-reason", "error"}]
    gaps = [r for r in report if r["verdict"] == "not-judged"]
    judged = [r for r in report if r["verdict"] == "pass"]

    if args.json:
        print(
            json.dumps(
                {
                    "results": report,
                    "summary": {
                        "correct": len(judged),
                        "wrong": len(bad),
                        "not_judged": len(gaps),
                        "positive": len(positive_vectors()),
                        "negative": len(negative_vectors()),
                    },
                },
                indent=1,
            )
        )
    else:
        for row in bad:
            print(f"  {row['verdict'].upper():12s} {row['verifier']:6s} {row['vector']:34s} {row['detail']}")
        for row in gaps:
            print(f"  {'NOT-JUDGED':12s} {row['verifier']:6s} {row['vector']:34s} {row['detail']}")
        print(
            f"\n{len(judged)} correct, {len(bad)} wrong, {len(gaps)} not judged "
            f"({len(positive_vectors())} positive, {len(negative_vectors())} negative vectors)"
        )
    if bad:
        return 1
    if gaps and not args.allow_gaps:
        print("some vectors were not judged; pass --allow-gaps to accept a declared gap", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
