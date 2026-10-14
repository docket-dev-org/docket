"""Signing round-trips, and the attached-session path (SDK inside `docket run`).

Two classes of bug this file exists to prevent, both found by running the software the
way the docs describe rather than the way the tests did:

1. **A signature that cannot be verified.** Signing happened before the ``seal`` field
   was defaulted onto the receipt, so the signed bytes and the verified bytes differed by
   exactly one field. The result was a receipt that claimed to be signed and never
   verified — worse than an unsigned one, because it looks stronger while being broken.
2. **Two writers, one ledger.** An SDK session inside a supervised run appended to the
   same ledger file the supervisor owns, with its own sequence numbers and link values.
   The file became a chain with a fork in it. Attached sessions now write through the
   boundary's ``/docket/event`` endpoint instead, so the supervisor remains the only
   process that appends.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from docket.ledger import Ledger
from docket.policy import Caps, Policy
from docket.receipt import build_receipt, receipt_signable
from docket.signing import ed25519_available
from docket.supervisor import Supervisor
from docket.verify import verify_receipt

requires_crypto = pytest.mark.skipif(not ed25519_available(), reason="needs the optional cryptography extra")

REPO = Path(__file__).resolve().parents[1]


def _keypair(tmp_path: Path) -> tuple[Path, str]:
    from docket.signing import generate_keypair

    private = tmp_path / "ed25519.pem"
    public_hex = generate_keypair(private)
    return private, public_hex


@requires_crypto
def test_a_signed_receipt_verifies_with_the_embedded_public_key(tmp_path, make_receipt):
    private, public_hex = _keypair(tmp_path)
    receipt, _, _ = make_receipt(sign_key=private)
    assert receipt["signature"]["alg"] == "ed25519"
    assert receipt["signature"]["public_key"] == public_hex
    result = verify_receipt(receipt)
    assert result.ok, result.failures
    assert any(c.name == "signature" and c.status == "pass" for c in result.checks)


@requires_crypto
def test_a_signed_receipt_verifies_with_a_pinned_public_key(tmp_path, make_receipt):
    private, public_hex = _keypair(tmp_path)
    receipt, _, _ = make_receipt(sign_key=private)
    assert verify_receipt(receipt, expect_public_key=public_hex).ok
    wrong = verify_receipt(receipt, expect_public_key="00" * 32)
    assert not wrong.ok
    assert any("signature" in failure for failure in wrong.failures)


@requires_crypto
def test_signing_after_sealing_still_verifies(tmp_path, make_receipt):
    """The seal is part of what the signature covers; both together must verify."""
    from docket.signing import load_or_create_seal_key

    private, _ = _keypair(tmp_path)
    seal_key = load_or_create_seal_key(tmp_path / "seal.key")
    receipt, _, _ = make_receipt(sign_key=private, seal_key=seal_key)
    assert receipt["seal"] is not None
    result = verify_receipt(receipt, seal_key=seal_key)
    assert result.ok, result.failures


@requires_crypto
def test_editing_a_signed_receipt_breaks_the_signature(tmp_path, make_receipt):
    private, _ = _keypair(tmp_path)
    receipt, _, _ = make_receipt(sign_key=private)
    receipt["totals"]["cost_usd"] = "999.000000"
    result = verify_receipt(receipt)
    assert not result.ok
    assert any(c.name == "signature" and c.status == "fail" for c in result.checks)


@requires_crypto
def test_signature_covers_the_seal_field_being_absent_or_present(tmp_path, make_receipt):
    """Regression: the signed payload must include `seal` even when it is null."""
    private, _ = _keypair(tmp_path)
    receipt, _, _ = make_receipt(sign_key=private)
    assert receipt["seal"] is None
    assert "seal" in receipt_signable(receipt)
    payload = json.dumps(receipt_signable(receipt), sort_keys=True)
    assert '"seal": null' in payload.replace(" ", " ") or '"seal":null' in payload


# --------------------------------------------------------------------------- #
# Attached sessions: the SDK inside a supervised run
# --------------------------------------------------------------------------- #

AGENT = """
import docket

with docket.Session() as session:
    assert session.attached, "expected to attach to the supervised run"
    session.record_note("child agent: starting the expensive part")
    session.record_tool("read_file", {"path": "src/app.py"}, result_bytes=1200)
    session.record_model_call(model="claude-sonnet-5", input_tokens=800, output_tokens=120)
    session.record_note("child agent: done")
print("child: recorded 4 events through the boundary")
"""


def test_sdk_inside_a_supervised_run_writes_through_the_boundary(tmp_path, fake_upstream):
    """One writer per ledger: the child's events land in the supervisor's chain."""
    (tmp_path / "agent_child.py").write_text(AGENT, encoding="utf-8")
    policy = Policy(
        name="attach-test",
        caps=Caps(max_usd=Decimal("1.00")),
    )
    supervisor = Supervisor(
        [sys.executable, str(tmp_path / "agent_child.py")],
        policy=policy,
        docket_dir=tmp_path / ".docket",
        cwd=tmp_path,
        upstreams={"openai": fake_upstream.url},
        env_extra={"PYTHONPATH": str(REPO / "src")},
    )
    result = supervisor.run()

    events = Ledger.read(result.ledger_path)
    types = [event.type for event in events]
    assert "note" in types, "the child's notes did not reach the ledger"
    assert types.count("note") >= 2
    assert "tool.call" in types

    # The chain must be intact: a second writer would have broken it.
    assert Ledger(events=events).chain_error() is None
    verification = verify_receipt(result.receipt, ledger_path=result.ledger_path)
    assert verification.ok, verification.failures

    # Agent-asserted events are labelled as claims, not observations. The supervisor's own
    # notes about the run are *not* labelled: `reported_by: "agent"` means "the supervised
    # process said this", and a note Docket wrote about its own evidence directory is an
    # observation, not a claim. Both cases are asserted, so neither can drift silently.
    agent_asserted = [event for event in events if event.data.get("reported_by") == "agent"]
    assert [event.type for event in agent_asserted].count("note") == 2
    assert "tool.call" in [event.type for event in agent_asserted]
    assert "model.response" in [event.type for event in agent_asserted]
    supervisor_notes = [
        event for event in events if event.type == "note" and "reported_by" not in event.data
    ]
    assert supervisor_notes, "the run's own note about the evidence directory is in the record"
    assert all("excluded" in event.data for event in supervisor_notes)


def test_sdk_standalone_still_writes_its_own_ledger_and_receipt(tmp_path):
    """Outside a supervised run, the session owns the ledger (the notebook case)."""
    docket_dir = tmp_path / ".docket"
    script = tmp_path / "standalone.py"
    script.write_text(
        f"""
import sys
sys.path.insert(0, {str(REPO / 'src')!r})
import docket

with docket.Session(docket_dir={str(docket_dir)!r}) as session:
    assert not session.attached
    session.record_note("standalone")
    session.record_model_call(model="claude-haiku-4.5", input_tokens=100, output_tokens=20)
print("standalone ok")
""",
        encoding="utf-8",
    )
    import subprocess

    completed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr
    receipts = list((docket_dir / "receipts").glob("*.receipt.json"))
    assert receipts, "standalone session produced no receipt"
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    verification = verify_receipt(receipt, ledger_path=next((docket_dir / "runs").glob("*.jsonl")))
    assert verification.ok, verification.failures


def test_cli_verify_reads_the_seal_key_file(tmp_path, monkeypatch, capsys):
    """`docket verify --seal-key <file>` must read the file, not hash the path.

    Found the honest way: running the command from the documentation and getting
    "non-hexadecimal number found in fromhex() arg at position 0", because the CLI
    passed the path through to an API that wanted key material.
    """
    from docket.cli import main
    from docket.receipt import write_receipt
    from docket.signing import load_or_create_seal_key

    monkeypatch.chdir(tmp_path)
    key_path = tmp_path / ".docket" / "keys" / "seal.key"
    key_path.parent.mkdir(parents=True)
    key = load_or_create_seal_key(key_path)

    # A small sealed ledger + receipt, written the way `docket run --seal` would.
    ledger = Ledger.open(tmp_path / ".docket" / "runs" / "s.jsonl")
    ledger.append("session.start", {"argv": ["pytest"], "cwd": str(tmp_path), "host": {}})
    ledger.append("session.end", {"exit_code": 0, "breached": None, "interrupted": False})
    from docket.policy import Caps, Policy
    from docket.pricing import CostMeter, PriceTable

    table = PriceTable.load()
    receipt = build_receipt(
        events=list(ledger),
        ledger=ledger,
        meter=CostMeter(price_table=table),
        policy=Policy(name="seal-cli", caps=Caps()),
        session_id="s",
        docket_number="DKT-20260101-SEAL01",
        exit_code=0,
        duration_seconds=0.5,
        price_table=table,
        seal_key=key,
        created_at="2026-01-01T00:00:00Z",
    )
    receipt_path = tmp_path / ".docket" / "receipts" / "DKT-20260101-SEAL01.receipt.json"
    write_receipt(receipt, receipt_path)
    ledger.close()
    capsys.readouterr()

    assert main(["verify", str(receipt_path), "--seal-key", str(key_path)]) == 0
    out = capsys.readouterr().out
    assert "seal" in out and "HMAC-SHA256 valid" in out

    # A wrong key must be reported, not silently ignored.
    wrong = tmp_path / "wrong.key"
    wrong.write_text("ab" * 32, encoding="utf-8")
    assert main(["verify", str(receipt_path), "--seal-key", str(wrong)]) == 1
    assert "does not verify" in capsys.readouterr().out
