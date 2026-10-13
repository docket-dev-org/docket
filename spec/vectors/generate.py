#!/usr/bin/env python3
"""Generate the conformance vectors in this directory.

The vectors are the contract between implementations: ``docket`` (Python) and
``verify-js/verify.mjs`` (JavaScript, no dependencies) must both reproduce every
value here, byte for byte. They are generated once and committed; the test suites
check the committed files rather than regenerating them, so a change to an
implementation that breaks the format fails loudly.

Regenerate deliberately, in a format-changing commit:

    python spec/vectors/generate.py
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docket.canon import canonical_json, merkle_proof, merkle_root, sha256_hex  # noqa: E402
from docket.ledger import GENESIS, Ledger  # noqa: E402
from docket.policy import Caps, Policy  # noqa: E402
from docket.pricing import CostMeter, PriceTable  # noqa: E402
from docket.receipt import build_receipt, receipt_core  # noqa: E402

FIXED_TIME = "2026-01-02T03:04:05Z"


def _clock():
    counter = {"n": 0}

    def tick() -> str:
        counter["n"] += 1
        return FIXED_TIME

    return tick


def write(name: str, payload: object) -> None:
    (HERE / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {name}")


def main() -> None:
    # 1. canonical JSON ---------------------------------------------------- #
    canonical_cases = [
        {"input": {"b": 1, "a": 2}, "canonical": '{"a":2,"b":1}'},
        {"input": {"z": [3, 2, 1], "a": {"y": True, "x": None}}, "canonical": '{"a":{"x":null,"y":true},"z":[3,2,1]}'},
        {"input": {"s": "café — ☕", "n": 0}, "canonical": '{"n":0,"s":"café — ☕"}'},
        {"input": {"price": "0.018000", "tokens": 1200}, "canonical": '{"price":"0.018000","tokens":1200}'},
    ]
    for case in canonical_cases:
        case["sha256"] = sha256_hex(canonical_json(case["input"]))
    write("canonical.json", {"schema": "docket/vectors/canonical/v1", "cases": canonical_cases})

    # 2. Merkle tree ------------------------------------------------------- #
    leaves = [sha256_hex(f"leaf-{i}") for i in range(7)]
    proofs = [{"index": i, "path": merkle_proof(leaves, i)} for i in range(len(leaves))]
    write(
        "merkle.json",
        {
            "schema": "docket/vectors/merkle/v1",
            "leaf_prefix": "0x00",
            "node_prefix": "0x01",
            "rule": "RFC 6962: odd node promoted unchanged; empty tree = sha256('')",
            "leaves": leaves,
            "root": merkle_root(leaves),
            "proofs": proofs,
            "empty_tree_root": merkle_root([]),
        },
    )

    # 3. A small ledger and the receipt built from it ---------------------- #
    price_table = PriceTable.load()
    ledger = Ledger.in_memory(clock=_clock())
    ledger.append(
        "session.start",
        {
            "session_id": "vectorsession",
            "docket_number": "DKT-20260102-VECT01",
            "argv": ["agent", "--task", "vector"],
            "cwd": "/srv/vectors",
            "host": {"platform": "vector", "python": "3.13.0", "machine": "x86_64"},
        },
    )
    policy = Policy(name="vector", caps=Caps(max_usd=Decimal("5.00")))
    ledger.append("policy.applied", {"name": policy.name, "digest": policy.digest(), "document": policy.to_dict()})
    meter = CostMeter(price_table=price_table)
    calls = [
        ("claude-sonnet-5", 1_000, 0, 200, "reported"),
        ("claude-sonnet-5", 1_000, 1_000, 200, "reported"),
        ("gpt-6-luna", 5_000, 0, 400, "reported"),
        ("unlisted-model-1", 2_000, 0, 100, "estimated"),
    ]
    for index, (model, input_tokens, cached, output, source) in enumerate(calls):
        ledger.append(
            "model.request",
            {
                "provider": "vector",
                "model": model,
                "fingerprint": f"fp{index:02d}",
                "request_sha256": sha256_hex(f"request-{index}"),
                "streaming": False,
                "body_bytes": 512,
            },
        )
        cost = meter.record(model, input_tokens, cached, output, source)
        ledger.append(
            "model.response",
            {
                "provider": "vector",
                "model": model,
                "status": 200,
                "latency_ms": 100 + index,
                "usage": {"input_tokens": input_tokens, "cached_input_tokens": cached, "output_tokens": output},
                "usage_source": source,
                "cost_usd": str(cost),
                "cumulative_cost_usd": str(meter.cost),
            },
        )
    ledger.append("tool.call", {"name": "read_file", "args_digest": sha256_hex("args"), "result_bytes": 4096})
    ledger.append("session.end", {"exit_code": 0, "duration_seconds": "2.500", "breached": None, "interrupted": False})

    events = list(ledger)
    write(
        "ledger.json",
        {
            "schema": "docket/vectors/ledger/v1",
            "genesis": GENESIS,
            "note": "regenerate together with the receipt vector: the receipt binds these hashes",
            "events": [event.to_dict() for event in events],
            "head": ledger.head,
            "merkle_root": ledger.merkle_root(),
            "file_sha256": ledger.file_sha256(),
            "counts": ledger.types(),
        },
    )

    receipt = build_receipt(
        events=events,
        ledger=ledger,
        meter=meter,
        policy=policy,
        session_id="vectorsession",
        docket_number="DKT-20260102-VECT01",
        exit_code=0,
        duration_seconds=2.5,
        price_table=price_table,
        created_at=FIXED_TIME,
    )
    write("receipt.json", receipt)
    write(
        "receipt-digest.json",
        {
            "schema": "docket/vectors/receipt-digest/v1",
            "note": "sha256 of the canonical JSON of the receipt with seal and signature removed",
            "receipt_core_sha256": sha256_hex(canonical_json(receipt_core(receipt))),
        },
    )
    print(f"receipt total: {receipt['totals']['cost_usd']} over {receipt['totals']['calls']} calls")


if __name__ == "__main__":
    main()
