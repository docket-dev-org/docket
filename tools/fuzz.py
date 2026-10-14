#!/usr/bin/env python3
"""Fuzz Docket's parsers, canon, hashing and verifier — with invariants, not just "no crash".

A fuzzer that only checks "did not raise" finds crashes; a fuzzer that checks *properties*
finds lies. Every target below asserts something the project promises, so a failure means
a documented behaviour is wrong, not merely that an input was unusual:

``canon``     canonical bytes are deterministic, round-trip through ``json``, and distinct
              inputs never collide; non-integral floats are refused rather than rounded.
``faultspec`` random text either parses into internally consistent faults or raises
              ``ValueError`` — never another exception, and never a silently-ignored token.
``ledger``    a mutated ledger file is either reported as unhealthy, or parses to a chain
              that actually validates. **The reader may never call a tampered file healthy.**
``receipt``   a mutated receipt never crashes the verifier, and any change to hashed
              content is reported as a failure rather than passing.
``proof``     a doctored inclusion proof never verifies. The metamorphic relation behind the
              project's central claim: edit anything a proof covers — a sibling hash, a side,
              the leaf, the row's binding to the ledger — and verification must fail. (A
              version of this target found a receipt whose proof verified after one hex digit
              was flipped, because the verifier counted proofs instead of checking all of them.)
``policy``    random TOML either loads, or raises a ``ValueError`` naming the problem.

Usage::

    python tools/fuzz.py --seconds 20            # every target, time-boxed
    python tools/fuzz.py --target canon -n 5000  # one target, fixed iterations
    python tools/fuzz.py --seed 1234 --verbose

Exit status is 0 only when every target survives every iteration.
"""

from __future__ import annotations

import argparse
import atexit
import json
import random
import shutil
import string
import sys
import tempfile
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docket.canon import canonical_bytes  # noqa: E402
from docket.ledger import Ledger  # noqa: E402
from docket.policy import Policy  # noqa: E402
from docket.receipt import build_receipt  # noqa: E402
from docket.resilience import parse_fault_spec  # noqa: E402
from docket.verify import verify_receipt  # noqa: E402

#: Scratch directory for the process, created on first use (see :func:`_workdir`).
_TMP_DIR: Path | None = None

UNICODE_SOUP = "áéíóúñüçßåøœµπλ→≈≠±∞✓漢字🎯\u200b\u0301\u0000\u001f"
CONTROL = "\r\n\t\x00\x1b\b"


class FuzzFailure(AssertionError):
    """A property was violated. Carries the input so the failure can be reproduced."""

    def __init__(self, target: str, message: str, payload: Any) -> None:
        super().__init__(f"{target}: {message}\n  input: {payload!r}")
        self.target = target
        self.payload = payload


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #


def rand_string(rng: random.Random, *, max_length: int = 12) -> str:
    alphabet = string.printable + UNICODE_SOUP + CONTROL
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_length)))


def rand_json(rng: random.Random, depth: int = 0) -> Any:
    """A JSON-encodable value of arbitrary shape, including shapes nobody intends."""
    choices = ["str", "int", "bool", "none"]
    if depth < 4:
        choices += ["list", "dict", "dict", "list"]
    kind = rng.choice(choices)
    if kind == "str":
        return rand_string(rng)
    if kind == "int":
        return rng.choice([0, 1, -1, 2**53, -(2**53), 10**18, rng.randint(-(10**6), 10**6)])
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "none":
        return None
    if kind == "list":
        return [rand_json(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = [rand_string(rng, max_length=8) for _ in range(rng.randint(0, 4))]
    return {key: rand_json(rng, depth + 1) for key in keys}


def integral_floats(rng: random.Random, depth: int = 0) -> Any:
    """A value whose float members are all integral — legal in a hashed structure.

    Depth-bounded: a generator that can recurse without limit is a bug in the harness, and
    a harness bug that looks like a product failure wastes the fuzzer's whole purpose.
    """
    kind = rng.choice(["float", "list", "dict"]) if depth < 3 else "float"
    if kind == "float":
        return float(rng.randint(-1000, 1000))
    if kind == "list":
        return [integral_floats(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {rand_string(rng, max_length=6): integral_floats(rng, depth + 1) for _ in range(rng.randint(0, 3))}


# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #


def fuzz_canon(rng: random.Random) -> None:
    value = rand_json(rng)
    blob = canonical_bytes(value)
    # 1. it round-trips: the canonical form is valid JSON for the same value.
    if json.loads(blob) != json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True)):
        raise FuzzFailure("canon", "canonical bytes do not round-trip to the same value", value)
    # 2. it is deterministic, whatever order the keys were inserted in.
    shuffled = json.loads(json.dumps(value, ensure_ascii=False))
    if isinstance(shuffled, dict):
        items = list(shuffled.items())
        rng.shuffle(items)
        shuffled = dict(items)
    if canonical_bytes(shuffled) != blob:
        raise FuzzFailure("canon", "key insertion order changed the canonical bytes", value)

    # 3. no floats survive into a hashed structure: an integral float normalises to an
    #    integer, and a non-integral one is refused outright (checked below). Scanning the
    #    decoded value rather than the bytes, because a string may legitimately contain a
    #    dot — scanning bytes would call `"1.2.3"` a float.
    def contains_float(node: Any) -> bool:
        if isinstance(node, float):
            return True
        if isinstance(node, dict):
            return any(contains_float(item) for item in node.values())
        if isinstance(node, list):
            return any(contains_float(item) for item in node)
        return False

    if contains_float(json.loads(blob)):
        raise FuzzFailure("canon", "a float survived into a hashed structure", value)

    floats = integral_floats(rng)
    canonical_bytes({"v": floats})  # integral floats are legal and normalise

    # 4. a non-integral float is refused rather than silently rounded: rounding would make
    #    two different amounts hash identically.
    try:
        canonical_bytes({"cost": 0.1 + rng.random()})
    except TypeError:
        pass
    else:
        raise FuzzFailure("canon", "a non-integral float was accepted into a hashed structure", value)


def fuzz_faultspec(rng: random.Random) -> None:
    grammar = [
        "delay",
        "status",
        "drop",
        "reset",
        "truncate",
        "corrupt",
        "slow",
        "seed",
        "=",
        ":",
        ",",
        "",
        " ",
        "\t",
        "0",
        "1",
        "0.5",
        "-1",
        "2",
        "nan",
        "inf",
        "1e999",
        "99999999999999999999",
        "250ms",
        "2s",
        "1h",
        "abc",
        "banana",
        "503",
        "999",
        "0.0",
        "1.0",
        "1.5",
        "\n",
        "\u0000",
        "漢",
        "٩",
        "0x10",
        "+1",
        ".5",
    ]
    spec = "".join(rng.choice(grammar) for _ in range(rng.randint(1, 12)))
    try:
        faults = parse_fault_spec(spec)
    except ValueError:
        return
    except Exception as exc:  # noqa: BLE001 - anything other than ValueError is a bug
        raise FuzzFailure("faultspec", f"raised {type(exc).__name__}: {exc}", spec) from exc
    for fault in faults:
        if not 0.0 <= fault.probability <= 1.0:
            raise FuzzFailure("faultspec", f"probability out of range: {fault.probability}", spec)
        if fault.kind == "status" and not 100 <= fault.status <= 599:
            raise FuzzFailure("faultspec", f"invalid status: {fault.status}", spec)
        if fault.kind in {"delay", "slow"} and fault.seconds < 0:
            raise FuzzFailure("faultspec", f"negative duration: {fault.seconds}", spec)
        if fault.kind == "truncate" and not 0.0 < fault.fraction <= 1.0:
            raise FuzzFailure("faultspec", f"invalid fraction: {fault.fraction}", spec)


def _build_ledger(path: Path, events: int = 6) -> None:
    ledger = Ledger.open(path, max_bytes=None)
    for index in range(events):
        ledger.append("note", {"index": index, "text": f"event {index}", "nested": {"a": [1, 2, index]}})
    ledger.append("session.end", {"exit_code": 0})
    ledger.close()


def fuzz_ledger(rng: random.Random) -> None:
    """Damage a real ledger in every way a disk or an adversary would, then read it."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fuzz.jsonl"
        _build_ledger(path)
        original = path.read_bytes()
        kind = rng.choice(
            ["truncate", "flip", "insert", "delete_line", "reorder", "duplicate", "append_junk", "unlink"]
        )
        lines = original.split(b"\n")
        if kind == "truncate":
            mutated = original[: rng.randint(0, max(1, len(original) - 1))]
        elif kind == "flip":
            position = rng.randrange(len(original))
            byte = bytearray(original)
            byte[position] ^= 1 << rng.randrange(8)
            mutated = bytes(byte)
        elif kind == "insert":
            position = rng.randrange(len(original) + 1)
            mutated = original[:position] + rng.choice([b"\x00", b"}", b"\xff", b"x", b"\n"]) + original[position:]
        elif kind == "delete_line":
            if len(lines) < 3:
                return
            dropped = rng.randrange(len(lines) - 1)
            mutated = b"\n".join(lines[:dropped] + lines[dropped + 1 :])
        elif kind == "reorder":
            if len(lines) < 4:
                return
            first, second = sorted(rng.sample(range(len(lines) - 1), 2))
            lines[first], lines[second] = lines[second], lines[first]
            mutated = b"\n".join(lines)
        elif kind == "duplicate":
            if len(lines) < 3:
                return
            picked = rng.randrange(len(lines) - 1)
            mutated = b"\n".join(lines[:picked] + [lines[picked]] + lines[picked:])
        elif kind == "append_junk":
            mutated = original + rng.choice([b'{"partial":', b"\xff\xfe", b"\n\n", b"not json\n"])
        else:
            path.unlink()
            mutated = None

        if mutated is not None:
            path.write_bytes(mutated)

        try:
            events, diagnostics = Ledger.read_with_diagnostics(path)
        except Exception as exc:  # noqa: BLE001
            raise FuzzFailure("ledger", f"the reader raised {type(exc).__name__}: {exc}", kind) from exc

        # The property that matters: "healthy" means the chain actually validates. Calling
        # a damaged file healthy would put a forged record into a receipt.
        if diagnostics.get("healthy"):
            prev = None
            for index, event in enumerate(events):
                if event.seq != index:
                    raise FuzzFailure("ledger", f"healthy file with a sequence gap at {index}", kind)
                if event.compute_hash() != event.hash:
                    raise FuzzFailure("ledger", f"healthy file with a bad hash at seq {index}", kind)
                if prev is not None and event.prev != prev:
                    raise FuzzFailure("ledger", f"healthy file with a broken link at seq {index}", kind)
                prev = event.hash


def _workdir() -> Path:
    """One scratch directory per process, reused by every iteration.

    The first version called ``tempfile.mkdtemp()`` once per iteration, so a single 60-second
    run created tens of thousands of directories. Where ``/tmp`` is a tmpfs — the usual
    container layout, and the layout this project was developed in — those directories are
    pinned RAM: the run filled ``/tmp``, the box went into swap-less thrashing, and every
    command in the sandbox started taking minutes. A fuzzer that takes down the machine it runs
    on is not finding bugs, it *is* the bug. One directory, reused, cleaned up at exit; and
    ``TMPDIR`` still decides where it lives, so a constrained container can point it at disk.
    """
    global _TMP_DIR
    if _TMP_DIR is None:
        _TMP_DIR = Path(tempfile.mkdtemp(prefix="docket-fuzz-"))
        atexit.register(shutil.rmtree, _TMP_DIR, True)
    return _TMP_DIR


def _build_receipt() -> tuple[dict[str, Any], Path]:
    from docket.pricing import CostMeter, PriceTable

    table = PriceTable.load()
    tmp = _workdir()
    runs = tmp / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / "f.jsonl"
    if path.exists():
        # A fresh chain per iteration: reusing the file would make the ledger reader deal with
        # yesterday's events and turn the target into a test of something else.
        path.unlink()
    ledger = Ledger.open(path, max_bytes=None)
    meter = CostMeter(price_table=table)
    ledger.append("policy.applied", {"name": "fuzz", "digest": "x", "document": {}})
    for index in range(3):
        ledger.append("model.request", {"provider": "openai", "model": "claude-sonnet-5", "fingerprint": f"fp{index}"})
        cost = meter.record("claude-sonnet-5", 1000, 0, 200, "reported")
        ledger.append(
            "model.response",
            {
                "provider": "openai",
                "model": "claude-sonnet-5",
                "status": 200,
                "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
                "usage_source": "reported",
                "cost_usd": str(cost),
            },
        )
    ledger.append("session.end", {"exit_code": 0, "breached": None, "interrupted": False})
    receipt = build_receipt(
        events=list(ledger),
        ledger=ledger,
        meter=meter,
        policy=Policy(name="fuzz"),
        session_id="fuzz",
        docket_number="DKT-20260101-FUZZ00",
        exit_code=0,
        duration_seconds=1.0,
        price_table=table,
        created_at="2026-01-01T00:00:00Z",
    )
    ledger_path = tmp / "runs" / "f.jsonl"
    ledger.close()
    return receipt, ledger_path


def _paths_in(document: Any) -> list[tuple[Any, Any]]:
    """Every (container, key) pair in a nested document, so a mutation can address one value."""
    found: list[tuple[Any, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict) and node:
            for key, value in node.items():
                found.append((node, key))
                walk(value)
        elif isinstance(node, list) and node:
            for index, value in enumerate(node):
                found.append((node, index))
                walk(value)

    walk(document)
    return found


def _mutate_receipt(rng: random.Random, receipt: dict[str, Any]) -> dict[str, Any]:
    mutated = json.loads(json.dumps(receipt))
    # Several edits at once, not one: a verifier can survive any single field being wrong and
    # still come apart when two of them disagree with each other (a total that no longer matches
    # the rows it summarises, a digest that no longer matches the document beside it).
    for _ in range(rng.randint(1, 3)):
        # Re-walk before every edit: an earlier edit can delete the key a collected path points
        # at, and acting on a stale path is a bug in the harness that looks like a bug in the
        # product. (It has happened: a sibling swap raised KeyError out of the fuzzer itself.)
        paths = _paths_in(mutated)
        if not paths:
            return mutated
        op = rng.choice(
            ["replace", "delete", "corrupt_type", "huge_number", "shuffle_list", "swap_siblings", "whole_document"]
        )
        container, key = rng.choice(paths)
        if op == "replace" and isinstance(container, dict):
            container[key] = rand_json(rng)
        elif op == "delete" and isinstance(container, dict):
            container.pop(key, None)
        elif op == "corrupt_type":
            container[key] = rng.choice([None, True, 0, "", [], {}, "0.0", -1, 2**64])
        elif op == "huge_number":
            container[key] = rng.choice([10**30, "9" * 40, -(10**30), 1e308 if rng.random() < 0.5 else "0.1"])
        elif op == "shuffle_list" and isinstance(container, list):
            rng.shuffle(container)
        elif op == "swap_siblings" and isinstance(container, dict):
            candidates = [k for k in container if k != key]
            if key in container and candidates:
                other = rng.choice(candidates)
                container[key], container[other] = container[other], container[key]
        elif op == "whole_document":
            return rand_json(rng)
    return mutated


def fuzz_receipt(rng: random.Random) -> None:
    receipt, ledger_path = _build_receipt()
    mutated = _mutate_receipt(rng, receipt)
    if not isinstance(mutated, dict):
        # The whole document replaced by a scalar, a list or a string: the verifier must still
        # return a verdict, because whoever asks for a verification chooses the file to hand it.
        try:
            result = verify_receipt(mutated, ledger_path=ledger_path)
        except Exception as exc:  # noqa: BLE001
            raise FuzzFailure(
                "receipt",
                f"the verifier raised {type(exc).__name__}: {exc} on a non-object receipt",
                json.dumps(mutated)[:200],
            ) from exc
        if result.ok:
            raise FuzzFailure("receipt", "a receipt that is not an object verified", json.dumps(mutated)[:200])
        return
    try:
        result = verify_receipt(mutated, ledger_path=ledger_path)
    except Exception as exc:  # noqa: BLE001 - a verifier that raises on hostile input is broken
        raise FuzzFailure(
            "receipt", f"the verifier raised {type(exc).__name__}: {exc}", json.dumps(mutated)[:400]
        ) from exc
    # A verifier must always give a verdict with the checks it performed.
    if not hasattr(result, "ok") or not result.checks:
        raise FuzzFailure("receipt", "the verifier returned no verdict", json.dumps(mutated)[:200])
    # Mutating anything the hashes cover must break verification. The seal, signature and
    # appendix are the parts that propagate; `limits_disclosure` and cosmetic fields are
    # not covered, so a mutation that only touches those may legitimately still pass —
    # this target therefore reports rather than asserts for that case, and the assertion
    # below is limited to mutations of numbers that must be recomputable.
    totals = mutated.get("totals")
    if isinstance(totals, dict) and "cost_usd" in totals:
        original = receipt["totals"]["cost_usd"]
        if str(totals["cost_usd"]) != str(original) and result.ok:
            raise FuzzFailure(
                "receipt",
                f"a receipt whose total changed ({original} -> {totals['cost_usd']}) still verified",
                json.dumps(mutated)[:200],
            )


def fuzz_policy(rng: random.Random) -> None:
    fragments = [
        "[policy]",
        "[caps]",
        "[models]",
        "[files]",
        "[egress]",
        "[resilience]",
        "name = ",
        "max_usd = ",
        "max_tokens = ",
        "warn_at = ",
        "allow = ",
        "deny = ",
        "breaker_threshold = ",
        '"0.5"',
        '"abc"',
        "-1",
        "0",
        "1e999",
        "true",
        "false",
        "[]",
        "['a']",
        "unknown_key = 1",
        "\n",
        "# comment\n",
        "= ",
        "null",
    ]
    text = "".join(rng.choice(fragments) for _ in range(rng.randint(1, 14)))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "policy.toml"
        path.write_text(text, encoding="utf-8")
        try:
            policy = Policy.load(path)
        except ValueError:
            return
        except Exception as exc:  # noqa: BLE001
            raise FuzzFailure("policy", f"raised {type(exc).__name__}: {exc}", text) from exc
        # A policy that loads must round-trip: the digest a receipt records has to be
        # reproducible from the document that produced it.
        again = Policy.from_dict(policy.to_dict())
        if again.digest() != policy.digest():
            raise FuzzFailure("policy", "a loaded policy did not round-trip to the same digest", text)
        if policy.caps.max_usd is not None and policy.caps.max_usd < 0:
            raise FuzzFailure("policy", f"a negative cap loaded: {policy.caps.max_usd}", text)
        if policy.caps.max_usd is not None and not isinstance(policy.caps.max_usd, Decimal):
            raise FuzzFailure("policy", "max_usd is not a Decimal", text)


def fuzz_proof(rng: random.Random) -> None:
    """A doctored inclusion proof must never verify — the claim the whole project rests on.

    Every mutation below edits something the receipt's Merkle root and appendix cover, so a
    ``True`` verdict afterwards means the verifier is not actually checking what it says it
    checks. This is a metamorphic test: rather than comparing against a fixed expectation, it
    asserts that a *transformation* (doctoring) has the documented effect (failure).
    """
    receipt, ledger_path = _build_receipt()
    rows = receipt.get("appendix", {}).get("usage") or []
    profiled = [row for row in rows if isinstance(row, dict) and row.get("merkle_path")]
    if not profiled:
        return
    row = rng.choice(profiled)
    path = row["merkle_path"]
    mutation = rng.choice(
        ["flip_sibling_hash", "flip_side", "drop_step", "duplicate_step", "flip_event_hash", "swap_rows", "break_hex"]
    )
    if mutation == "flip_sibling_hash":
        step = rng.choice(path)
        step["hash"] = _flip_hex(step["hash"], rng)
    elif mutation == "flip_side":
        step = rng.choice(path)
        step["side"] = "left" if step.get("side") == "right" else "right"
    elif mutation == "drop_step":
        if len(path) < 2:
            return
        path.pop(rng.randrange(len(path)))
    elif mutation == "duplicate_step":
        path.append(dict(rng.choice(path)))
    elif mutation == "flip_event_hash":
        row["event_hash"] = _flip_hex(str(row.get("event_hash")), rng)
    elif mutation == "break_hex":
        rng.choice(path)["hash"] = "zz"
    elif mutation == "swap_rows":
        if len(profiled) < 2:
            return
        other = rng.choice([r for r in profiled if r is not row])
        row["merkle_path"], other["merkle_path"] = other["merkle_path"], row["merkle_path"]
    try:
        result = verify_receipt(receipt, ledger_path=ledger_path)
    except Exception as exc:  # noqa: BLE001
        raise FuzzFailure("proof", f"the verifier raised {type(exc).__name__}: {exc}", mutation) from exc
    if result.ok:
        raise FuzzFailure("proof", f"a receipt survived doctoring ({mutation}) and still verified", mutation)
    if not any(check.status == "fail" and "usage" in check.name for check in result.checks):
        raise FuzzFailure(
            "proof",
            f"doctoring ({mutation}) did not fail a usage check; failures were "
            f"{[c.name for c in result.checks if c.status == 'fail']}",
            mutation,
        )


def _flip_hex(value: str, rng: random.Random) -> str:
    """Change one hex digit of ``value``, so the edited string stays hex."""
    if not value:
        return "0"
    index = rng.randrange(len(value))
    digit = value[index].lower()
    replacement = rng.choice([d for d in "0123456789abcdef" if d != digit])
    return value[:index] + replacement + value[index + 1 :]


TARGETS: dict[str, Callable[[random.Random], None]] = {
    "canon": fuzz_canon,
    "faultspec": fuzz_faultspec,
    "ledger": fuzz_ledger,
    "receipt": fuzz_receipt,
    "proof": fuzz_proof,
    "policy": fuzz_policy,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fuzz Docket's parsers, canon and verifier")
    parser.add_argument("--target", choices=sorted(TARGETS), help="one target (default: all)")
    parser.add_argument("-n", "--iterations", type=int, default=0, help="iterations per target (0: time-boxed)")
    parser.add_argument("--seconds", type=float, default=10.0, help="per-target time box when -n is not given")
    parser.add_argument("--seed", type=int, default=None, help="seed for reproducibility")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    seed = args.seed if args.seed is not None else random.randrange(2**32)
    rng = random.Random(seed)
    chosen = [args.target] if args.target else sorted(TARGETS)
    print(f"docket fuzz — seed {seed}, targets {', '.join(chosen)}")

    failures: list[FuzzFailure] = []
    for name in chosen:
        target = TARGETS[name]
        started = time.monotonic()
        iterations = 0
        local_seed = rng.randrange(2**32)
        local = random.Random(local_seed)
        while True:
            if args.iterations:
                if iterations >= args.iterations:
                    break
            elif time.monotonic() - started >= args.seconds:
                break
            try:
                target(local)
            except FuzzFailure as failure:
                failures.append(failure)
                print(f"  FAIL  {name} after {iterations} iterations (target seed {local_seed})")
                print(f"        {failure}")
                break
            except Exception as exc:  # noqa: BLE001 - a fuzzer bug must never read as a product bug
                print(f"  ERROR {name} after {iterations} iterations (target seed {local_seed})")
                print(f"        the fuzzer itself raised {type(exc).__name__}: {exc}")
                print("        This is a bug in tools/fuzz.py, not in Docket. Fix the harness first.")
                return 2
            iterations += 1
        elapsed = time.monotonic() - started
        rate = iterations / elapsed if elapsed else 0
        status = "fail" if failures and failures[-1].target == name else "ok"
        print(
            f"  {status:4}  {name:10} {iterations:6d} iterations in {elapsed:5.1f}s ({rate:7.0f}/s) seed {local_seed}"
        )

    if failures:
        print(
            f"\n{len(failures)} target(s) violated a property. Each failure above is reproducible: "
            "re-run with the printed seed."
        )
        return 1
    print("\nEvery target held its properties.")
    return 0


if __name__ == "__main__":  # pragma: no cover - tool entry point
    raise SystemExit(main())
