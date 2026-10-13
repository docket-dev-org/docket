"""Receipts and verification, including the adversarial cases."""

from __future__ import annotations

import json

from docket.ledger import Ledger
from docket.receipt import load_receipt, receipt_core, receipt_digest, write_receipt
from docket.report import diff_receipts, render_markdown
from docket.signing import load_or_create_seal_key
from docket.verify import verify_receipt


def test_receipt_is_complete(make_receipt):
    receipt, path, ledger_path = make_receipt()
    for key in (
        "format",
        "docket_number",
        "session",
        "policy",
        "totals",
        "model_calls",
        "files",
        "ledger",
        "price_table",
        "appendix",
        "findings",
        "limits_disclosure",
    ):
        assert key in receipt, key
    assert receipt["totals"]["cost_usd"] == "0.018000"
    assert receipt["totals"]["calls"] == 3
    assert len(receipt["appendix"]["usage"]) == 3
    assert receipt["appendix"]["proof_included"] is True


def test_receipt_alone_verifies_without_the_ledger(make_receipt):
    receipt, path, _ = make_receipt()
    result = verify_receipt(load_receipt(path))
    assert result.ok, result.failures
    statuses = {c.name: c.status for c in result.checks}
    assert statuses["ledger-chain"] == "skip"
    assert statuses["cost-recomputation"] == "pass"
    assert statuses["usage-inclusion-proofs"] == "pass"


def test_receipt_with_ledger_verifies_fully(make_receipt):
    receipt, path, ledger_path = make_receipt()
    result = verify_receipt(load_receipt(path), ledger_path=ledger_path)
    assert result.ok, result.failures
    assert {c.status for c in result.checks} <= {"pass", "skip"}
    assert all(
        c.status != "skip" for c in result.checks if c.name in {"ledger-chain", "merkle-root", "ledger-file-digest"}
    )


def test_verification_fails_when_the_ledger_is_edited(make_receipt):
    receipt, path, ledger_path = make_receipt()
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[1])
    payload["data"]["calls"] = 999
    lines[1] = json.dumps(payload)
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_receipt(load_receipt(path), ledger_path=ledger_path)
    assert not result.ok
    assert any("ledger-chain" in f for f in result.failures)


def test_verification_fails_when_a_receipt_field_is_faked(make_receipt):
    receipt, path, _ = make_receipt()
    receipt["totals"]["cost_usd"] = "0.00"
    receipt["appendix"]["usage"] = [{**row, "cost_usd": "0.00"} for row in receipt["appendix"]["usage"]]
    result = verify_receipt(receipt)
    assert not result.ok
    assert any("totals" in f for f in result.failures)


def test_verification_fails_when_a_usage_row_is_faked(make_receipt):
    receipt, path, _ = make_receipt()
    receipt["appendix"]["usage"][0]["output_tokens"] = 4242
    result = verify_receipt(receipt)
    assert not result.ok
    assert any("inclusion" in f or "recomputation" in f for f in result.failures)


def test_verification_fails_without_an_embedded_price_table(make_receipt):
    receipt, path, _ = make_receipt()
    del receipt["price_table"]
    result = verify_receipt(receipt)
    assert not result.ok
    assert any("price table" in f for f in result.failures)


def test_missing_disclosure_is_a_failure(make_receipt):
    receipt, path, _ = make_receipt()
    receipt["limits_disclosure"] = {}
    result = verify_receipt(receipt)
    assert not result.ok
    assert any("disclosure" in f for f in result.failures)


def test_sealed_receipt_verifies_only_with_the_key(tmp_path, make_receipt):
    key = load_or_create_seal_key(tmp_path / "key")
    receipt, _, _ = make_receipt()
    from docket.receipt import build_receipt  # noqa: F401  (documentation of import path)
    from docket.signing import seal

    receipt["seal"] = seal(receipt_core(receipt), key)
    assert verify_receipt(receipt, seal_key=key).ok
    wrong = verify_receipt(receipt, seal_key="00" * 32)
    assert not wrong.ok
    assert any("seal" in f for f in wrong.failures)


def test_seal_detects_edit_after_sealing(tmp_path, make_receipt):
    key = load_or_create_seal_key(tmp_path / "key")
    from docket.signing import seal

    receipt, _, _ = make_receipt()
    receipt["seal"] = seal(receipt_core(receipt), key)
    receipt["session"]["exit_code"] = 7
    assert not verify_receipt(receipt, seal_key=key).ok


def test_seal_key_file_is_created_once(tmp_path):
    path = tmp_path / "seal.key"
    first = load_or_create_seal_key(path)
    second = load_or_create_seal_key(path)
    assert first == second
    assert len(first) == 64


def test_receipt_digest_ignores_its_own_seal(tmp_path, make_receipt):
    key = load_or_create_seal_key(tmp_path / "key")
    from docket.signing import seal

    receipt, _, _ = make_receipt()
    before = receipt_digest(receipt)
    receipt["seal"] = seal(receipt_core(receipt), key)
    assert receipt_digest(receipt) == before


def test_receipt_round_trips_through_disk(make_receipt, tmp_path):
    receipt, path, _ = make_receipt()
    again = load_receipt(path)
    assert again == receipt
    target = tmp_path / "copy.receipt.json"
    write_receipt(again, target)
    assert load_receipt(target) == receipt


def test_markdown_report_mentions_the_essentials(make_receipt):
    receipt, _, _ = make_receipt()
    markdown = render_markdown(receipt)
    assert receipt["docket_number"] in markdown
    assert "estimated cost" in markdown
    assert "docket verify" in markdown


def test_verify_renders_a_verdict_line(make_receipt):
    receipt, path, _ = make_receipt()
    rendered = verify_receipt(load_receipt(path)).render(color=False)
    assert rendered.splitlines()[0].startswith("VERIFIED")


def test_diff_between_receipts_flags_behaviour_change(make_receipt):
    a, _, _ = make_receipt()
    b = json.loads(json.dumps(a))
    b["docket_number"] = "DKT-20260101-FFFFFF"
    b["totals"]["calls"] = 9
    b["totals"]["by_model"][0]["model"] = "gpt-6-luna"
    difference = diff_receipts(a, b)
    assert difference["changes"]["calls"] == (3, 9)
    assert "gpt-6-luna" in difference["changes"]["models_added"][1]
    assert difference["verdict"] == "behaviour changed"


def test_report_render_includes_proof_and_limits(make_receipt):
    from docket.report import render_receipt

    receipt, _, _ = make_receipt()
    text = render_receipt(receipt, color=False)
    assert "WHAT THIS RECEIPT PROVES" in text
    assert "does not observe" in text
    assert "VERIFY IT YOURSELF" in text


def test_canonical_head_is_recorded_in_the_receipt(make_receipt):
    receipt, _, ledger_path = make_receipt()
    events = Ledger.read(ledger_path)
    assert receipt["ledger"]["head"] == events[-1].hash
    assert receipt["ledger"]["events"] == len(events)
