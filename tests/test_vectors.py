"""Conformance: the Python implementation must reproduce the committed vectors.

The same files are consumed by ``verify-js/test.mjs``, which is what makes the
format portable rather than merely documented.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from docket.canon import canonical_json, merkle_root, sha256_hex, verify_merkle_proof
from docket.ledger import GENESIS
from docket.receipt import receipt_core
from docket.verify import verify_receipt

VECTORS = Path(__file__).resolve().parents[1] / "spec" / "vectors"


def _load(name: str) -> dict:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))


def test_canonical_vectors():
    data = _load("canonical.json")
    assert data["schema"] == "docket/vectors/canonical/v1"
    for case in data["cases"]:
        assert canonical_json(case["input"]) == case["canonical"], case["input"]
        assert sha256_hex(case["canonical"]) == case["sha256"]


def test_merkle_vectors():
    data = _load("merkle.json")
    assert merkle_root(data["leaves"]) == data["root"]
    assert merkle_root([]) == data["empty_tree_root"]
    for proof in data["proofs"]:
        assert verify_merkle_proof(data["leaves"][proof["index"]], proof["path"], data["root"])


def test_ledger_vectors_reproduce_the_chain():
    data = _load("ledger.json")
    assert data["genesis"] == GENESIS
    raw = data["events"]
    assert raw[0]["prev"] == GENESIS
    prev = GENESIS
    for expected_seq, entry in enumerate(raw):
        assert entry["seq"] == expected_seq
        assert entry["prev"] == prev
        assert sha256_hex(canonical_json({k: entry[k] for k in ("seq", "ts", "type", "data", "prev")})) == entry["hash"]
        prev = entry["hash"]
    assert raw[-1]["hash"] == data["head"]
    assert merkle_root([entry["hash"] for entry in raw]) == data["merkle_root"]
    assert data["counts"]["model.response"] == 4


def test_receipt_vector_verifies_and_recomputes_cost():
    receipt = _load("receipt.json")
    result = verify_receipt(receipt)
    assert result.ok, result.failures
    assert receipt["totals"]["calls"] == 4
    # Arithmetic, spelled out so the vector cannot drift silently:
    #   sonnet:        1000 in * $3/1M  = 0.003      + 200 out * $15/1M  = 0.003    -> 0.006
    #   sonnet cached: 1000 cached * $0.30/1M = 0.0003 + 200 out * $15/1M = 0.003   -> 0.0033
    #   gpt-6-luna:    5000 in * $0.10/1M = 0.0005   + 400 out * $0.50/1M = 0.0002  -> 0.0007
    #   unlisted:      2000 in * $1.25/1M (fallback, flagged) = 0.0025
    #                  + 100 out * $10/1M = 0.001                                 -> 0.0035
    #   total                                                                       -> 0.0135
    assert receipt["totals"]["cost_usd"] == "0.013500"
    assert receipt["totals"]["assumed_price_models"] == ["unlisted-model-1"]
    assert receipt["totals"]["usage_reported_calls"] == 3
    assert receipt["totals"]["usage_estimated_calls"] == 1


def test_receipt_vector_usage_rows_are_bound_to_ledger_hashes():
    receipt = _load("receipt.json")
    ledger = _load("ledger.json")
    hashes = {entry["hash"] for entry in ledger["events"]}
    for row in receipt["appendix"]["usage"]:
        assert row["event_hash"] in hashes
        assert row["merkle_path"]


def test_receipt_vector_core_digest_is_stable():
    receipt = _load("receipt.json")
    expected = _load("receipt-digest.json")["receipt_core_sha256"]
    assert sha256_hex(canonical_json(receipt_core(receipt))) == expected


def test_receipt_vector_binds_to_the_ledger_vector():
    receipt = _load("receipt.json")
    ledger = _load("ledger.json")
    assert receipt["ledger"]["merkle_root"] == ledger["merkle_root"]
    assert receipt["ledger"]["head"] == ledger["head"]
    assert receipt["ledger"]["events"] == len(ledger["events"])


def test_vectors_are_the_only_source_of_truth_for_the_format():
    for name in ("canonical.json", "merkle.json", "ledger.json", "receipt.json"):
        assert (VECTORS / name).exists(), f"{name} must be committed"


def test_committed_vectors_match_the_generator(tmp_path, monkeypatch):
    """Regeneration must be a no-op, so "the vectors are stale" is a failing test rather than
    something a reader has to notice by eye. This is the check that would have caught the
    whole load-bearing set (the policy digest in every event, the receipt core digest, the
    Merkle root) drifting away from the implementation that is supposed to produce it.

    The generator is run in a copy of the vectors directory; nothing in the repository is
    written by running the tests.
    """
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    staged = tmp_path / "vectors"
    staged.mkdir()
    for path in VECTORS.glob("*.json"):
        (staged / path.name).write_bytes(path.read_bytes())
    script = tmp_path / "generate.py"
    script.write_text((VECTORS / "generate.py").read_text(encoding="utf-8"), encoding="utf-8")

    env = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONHASHSEED="0")
    completed = subprocess.run(
        [sys.executable, str(script)], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=300
    )
    assert completed.returncode == 0, completed.stderr

    stale = [
        name
        for name in sorted(p.name for p in VECTORS.glob("*.json") if p.name != "generate.py")
        if (staged / name).read_bytes() != (VECTORS / name).read_bytes()
    ]
    assert not stale, f"spec/vectors is stale relative to the implementation: {stale}. Regenerate deliberately."
