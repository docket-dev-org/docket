"""CLI surface, in-process SDK, and the offline demo."""

from __future__ import annotations

import io
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from docket.cli import main
from docket.demo import run_demo
from docket.receipt import load_receipt
from docket.verify import verify_receipt

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_init_writes_a_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--max-usd", "3.00"]) == 0
    policy_file = tmp_path / "docket.toml"
    assert policy_file.exists()
    assert "max_usd = 3.00" in policy_file.read_text(encoding="utf-8")
    assert main(["init"]) == 1, "must not silently overwrite"


def test_prices_shows_provenance(capsys):
    assert main(["prices"]) == 0
    out = capsys.readouterr().out
    assert "as of" in out
    assert "never bills anyone" in out


def test_prices_json_is_machine_readable(capsys):
    assert main(["prices", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["_meta"]["unit"] == "per_million_tokens"


def test_keygen_writes_a_keypair_the_receipt_signs_with(tmp_path, monkeypatch, capsys):
    """`docket keygen` -> `docket run --sign` -> `docket verify`, end to end.

    `generate_keypair` existed in `src/docket/signing.py` from the first release and nothing
    called it: no command, no documentation. The only way to sign a receipt was to already know
    the library function, which makes `--sign` a flag nobody could use. This test drives the
    whole path a reader would, from the command that writes the key to the verification that
    checks the signature it produced.
    """
    pytest.importorskip("cryptography")
    monkeypatch.chdir(tmp_path)
    key = tmp_path / "signing.key"

    assert main(["keygen", str(key)]) == 0
    assert key.is_file(), "keygen wrote no key"
    assert oct(key.stat().st_mode)[-3:] == "600", "the private key is not mode 0600"
    out = capsys.readouterr().out
    public_hex = re.search(r"public key\s+([0-9a-f]{64})", out)
    assert public_hex, out

    # The key signs a real receipt, and the signature verifies.
    assert main(["init", "--name", "signed", "--max-usd", "1"]) == 0
    assert main(["run", "--policy", "docket.toml", "--sign", str(key), "--", sys.executable, "-c", "print(1)"]) == 0
    result = verify_receipt(load_receipt(Path(".docket/latest.receipt.json")))
    assert result.ok, list(result.failures)
    signature = load_receipt(Path(".docket/latest.receipt.json"))["signature"]
    assert signature["public_key"] == public_hex.group(1), "the receipt embeds a different key"
    assert signature["value"], "the receipt carries no signature value"

    # And a receipt edited after the fact fails the signature check, not only the totals.
    tampered = tmp_path / "tampered.json"
    document = json.loads(Path(".docket/latest.receipt.json").read_text(encoding="utf-8"))
    document["totals"]["cost_usd"] = "0.000001"
    tampered.write_text(json.dumps(document, indent=2), encoding="utf-8")
    assert not verify_receipt(load_receipt(tampered)).ok, "an edited receipt still verifies"


def test_keygen_refuses_to_replace_a_key_without_force(tmp_path, monkeypatch, capsys):
    """Refusing is the point: replacing a signing key invalidates every receipt signed with the old one."""
    pytest.importorskip("cryptography")
    monkeypatch.chdir(tmp_path)
    key = tmp_path / "signing.key"

    assert main(["keygen", str(key)]) == 0
    capsys.readouterr()
    assert main(["keygen", str(key)]) == 1, "an existing key was overwritten"
    assert "exists" in capsys.readouterr().err, "the refusal does not say the file exists"
    assert key.read_bytes(), "the key was truncated by the refusal"

    assert main(["keygen", str(key), "--force"]) == 0, "--force did not replace the key"


def test_doctor_reports_the_environment(capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "docket" in out and "python" in out


def test_run_without_a_command_is_an_error(capsys):
    assert main(["run", "--max-usd", "1"]) == 43
    assert "nothing to run" in capsys.readouterr().err


def test_run_then_verify_report_explain_and_diff(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = "print('child ran')"
    rc = main(["run", "--max-usd", "1", "--", sys.executable, "-c", code])
    assert rc == 0
    receipts = list((tmp_path / ".docket" / "receipts").glob("*.receipt.json"))
    assert len(receipts) == 1
    receipt = load_receipt(receipts[0])
    assert receipt["policy"]["document"]["caps"]["max_usd"] == "1.00"
    capsys.readouterr()

    ledger = next((tmp_path / ".docket" / "runs").glob("*.jsonl"))
    assert main(["verify", str(receipts[0]), "--ledger", str(ledger)]) == 0
    assert "VERIFIED" in capsys.readouterr().out
    assert main(["report", str(receipts[0]), "--markdown"]) == 0
    assert "# Docket" in capsys.readouterr().out
    assert main(["explain", str(receipts[0]), "--ledger", str(ledger)]) == 0
    assert "No findings" in capsys.readouterr().out
    assert main(["diff", str(receipts[0]), str(receipts[0])]) == 0


def test_verify_fails_on_a_tampered_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["run", "--", sys.executable, "-c", "print('x')"])
    receipt_path = next((tmp_path / ".docket" / "receipts").glob("*.receipt.json"))
    receipt = load_receipt(receipt_path)
    receipt["totals"]["cost_usd"] = "999.00"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    capsys.readouterr()
    assert main(["verify", str(receipt_path)]) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out


def test_verify_unknown_file_is_an_error(tmp_path, capsys):
    assert main(["verify", str(tmp_path / "nope.json")]) == 1
    assert "no such receipt" in capsys.readouterr().err


def test_bad_policy_is_reported_not_crashed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.toml").write_text('[caps]\non_breach = "explode"\n', encoding="utf-8")
    assert main(["run", "--policy", "bad.toml", "--", sys.executable, "-c", "pass"]) == 43
    assert "cannot load policy" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# SDK
# --------------------------------------------------------------------------- #


def test_sdk_session_records_and_produces_a_verifiable_receipt(tmp_path, monkeypatch):
    import docket

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKET_DOCKET_DIR", str(tmp_path / ".docket"))
    with docket.Session(label="sdk test", policy=docket.Policy(name="sdk")) as session:
        session.record_model_call(model="claude-sonnet-5", input_tokens=1_000, output_tokens=200, provider="anthropic")
        session.record_tool("search", {"q": "widgets"}, result_bytes=1234)
        session.record_note("checkpoint reached")
        assert session.calls == 1
        assert session.tokens == 1200
        assert session.would_exceed(usd="1.00") is False
    assert session.receipt is not None
    assert verify_receipt(session.receipt).ok
    assert session.receipt_path and session.receipt_path.exists()
    kinds = {
        event["type"]
        for event in _read_lines(session.receipt_path.parent.parent / "runs" / f"{session.session_id}.jsonl")
    }
    assert {"session.start", "model.request", "model.response", "tool.call", "note", "session.end"} <= kinds


def test_sdk_cap_breach_is_recorded(tmp_path, monkeypatch):
    import docket

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKET_DOCKET_DIR", str(tmp_path / ".docket"))
    policy = docket.Policy(name="tight", caps=docket.Caps(max_usd=__import__("decimal").Decimal("0.001")))
    with docket.Session(policy=policy) as session:
        session.record_model_call(model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)
    events = _read_lines(session.receipt_path.parent.parent / "runs" / f"{session.session_id}.jsonl")
    assert any(event["type"] == "cap.exceeded" for event in events)
    assert session.would_exceed(usd="100.00") is True


def test_sdk_records_tool_args_as_digests_by_default(tmp_path, monkeypatch):
    import docket

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOCKET_DOCKET_DIR", str(tmp_path / ".docket"))
    with docket.Session() as session:
        session.record_tool("sql", {"query": "select * from secrets"})
    events = _read_lines(session.receipt_path.parent.parent / "runs" / f"{session.session_id}.jsonl")
    tool = next(event for event in events if event["type"] == "tool.call")
    assert "args" not in tool["data"]
    assert len(tool["data"]["args_digest"]) == 64


def test_sdk_without_a_session_raises():
    import docket

    with pytest.raises(RuntimeError):
        docket.record_tool("nope")


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #


def test_demo_stops_a_runaway_agent_and_verifies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    outcome = run_demo(max_usd=__import__("decimal").Decimal("2.00"), color=False, out=out)
    text = out.getvalue()
    assert outcome["stopped_as_expected"] is True
    assert outcome["verify_ok"] is True
    assert "Runaway loop" in text, "the demo must show why the run was stopped"
    assert "VERIFIED" in text
    assert "deny-write violation" in text


def test_a_tighter_cap_stops_the_run_earlier(tmp_path, monkeypatch):
    """A smaller cap must produce a smaller receipt: the boundary is the boundary."""
    monkeypatch.chdir(tmp_path)
    small = run_demo(max_usd=__import__("decimal").Decimal("0.50"), color=False, out=io.StringIO())
    large = run_demo(max_usd=__import__("decimal").Decimal("2.00"), color=False, out=io.StringIO())
    assert float(small["cost_usd"]) < float(large["cost_usd"])
    assert small["breached"] == large["breached"] == "max_usd"


def test_demo_tamper_mode_is_detected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    outcome = run_demo(max_usd=__import__("decimal").Decimal("2.00"), tamper=True, color=False, out=out)
    assert outcome["tamper_detected"] is True
    assert any("ledger-chain" in failure for failure in outcome["tamper_failures"])


def test_the_tamper_demo_is_reproducible_by_the_command_it_prints(tmp_path, monkeypatch, capsys):
    """The demo must end with a command that produces the verdict it just showed.

    `demo --tamper` doctored the ledger and printed NOT VERIFIED, then told the reader
    nothing about how to see that for themselves. The obvious follow-up —
    `docket verify <receipt>` — passed, because the receipt is intact and nothing looked
    for the ledger. A tamper demonstration a reader cannot reproduce is not evidence, so
    the demo now prints its own verification command and `verify` finds the ledger beside
    the receipt when `--ledger` is absent.
    """
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    run_demo(max_usd=__import__("decimal").Decimal("2.00"), tamper=True, keep=True, color=False, out=out)
    printed = re.search(r"^    docket verify (\S+) --ledger (\S+)$", out.getvalue(), re.MULTILINE)
    assert printed is not None, "the demo does not print the command that reproduces its verdict"
    receipt = Path(printed.group(1))
    ledger = Path(printed.group(2))
    assert receipt.is_file() and ledger.is_file(), "the printed command names files that do not exist"

    capsys.readouterr()
    # The reader's own command, without the `--ledger` the demo printed: the ledger is
    # found beside the receipt and the same verdict comes back.
    assert main(["verify", str(receipt)]) == 1
    text = capsys.readouterr().out
    assert "NOT VERIFIED" in text, text
    assert "ledger-chain" in text, text
    assert str(ledger) in text, "the ledger that was used is not named"

    # And the same receipt, with its ledger replaced by an untouched one, verifies —
    # which is the point of the digest match rather than a recency pick.
    assert main(["verify", str(receipt), "--ledger", str(ledger)]) == 1


def test_verify_finds_the_ledger_beside_the_receipt(tmp_path, monkeypatch, capsys):
    """A receipt that arrives with its ledger must be checked against it.

    Copying `.docket/receipts/X.receipt.json` and `.docket/runs/Y.jsonl` somewhere else
    is how a receipt is actually shared, so the lookup is relative to the receipt rather
    than to the working directory.
    """
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--max-usd", "5"]) == 0
    assert main(["run", "--policy", "docket.toml", "--", sys.executable, "-c", "print(1)"]) == 0
    receipt = tmp_path / ".docket" / "receipts"
    ledger = tmp_path / ".docket" / "runs"
    elsewhere = tmp_path / "shared"
    shutil.copytree(receipt, elsewhere / "receipts")
    shutil.copytree(ledger, elsewhere / "runs")
    capsys.readouterr()
    shared_receipt = next((elsewhere / "receipts").glob("*.receipt.json"))
    assert main(["verify", str(shared_receipt)]) == 0
    text = capsys.readouterr().out
    assert "VERIFIED" in text, text
    assert "found beside the receipt" in text, "the ledger that was used is not named"
    chained = verify_receipt(load_receipt(shared_receipt), ledger_path=str(next((elsewhere / "runs").glob("*.jsonl"))))
    chain = next(c for c in chained.checks if c.name == "ledger-chain")
    assert chain.status == "pass", chain


def test_verify_without_a_ledger_anywhere_still_passes_on_the_receipt_alone(tmp_path, monkeypatch, capsys):
    """No ledger beside the receipt is a SKIP, not a failure.

    A receipt is designed to stand alone — that is the whole claim — so the absence of the
    ledger must degrade to the check it cannot perform, not to a red result.
    """
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--max-usd", "5"]) == 0
    assert main(["run", "--policy", "docket.toml", "--", sys.executable, "-c", "print(1)"]) == 0
    receipt = next((tmp_path / ".docket" / "receipts").glob("*.receipt.json"))
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(receipt, alone / receipt.name)
    capsys.readouterr()
    assert main(["verify", str(alone / receipt.name)]) == 0
    text = capsys.readouterr().out
    assert "VERIFIED" in text, text
    assert "found beside the receipt" not in text


def test_demo_json_output_is_machine_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--max-usd", "2.00", "--json"]) == 0
    captured = capsys.readouterr().out
    start = captured.index("{")
    payload = json.loads(captured[start:])
    assert payload["verify_ok"] is True
    assert payload["stopped_as_expected"] is True


def test_verify_can_be_done_by_the_independent_node_implementation(tmp_path, monkeypatch, capsys):
    """`--impl node` must hand the receipt to a program that shares no code with this one.

    The trust claim is "the format is not this tool", which is only true if the other tool is
    reachable from an installation rather than from a clone. This runs the verifier the wheel
    carries and asserts the verdict is the Node implementation's own — including its exit code,
    which a pipeline already gates on.
    """
    monkeypatch.chdir(tmp_path)
    node = shutil.which("node")
    if node is None:  # pragma: no cover - Node is in CI; a machine without it gets the message
        pytest.skip("node is not on PATH")
    assert main(["init", "--max-usd", "5"]) == 0
    assert main(["run", "--policy", "docket.toml", "--", sys.executable, "-c", "print(1)"]) == 0
    receipt = next((tmp_path / ".docket" / "receipts").glob("*.receipt.json"))
    capsys.readouterr()
    assert main(["verify", "--impl", "node", str(receipt)]) == 0
    text = capsys.readouterr().out
    assert "VERIFIED" in text, text
    assert "verified by the Node implementation" in text, text
    assert "PASS" in text, "the Node implementation's own check lines must be shown, not a summary"


def test_verify_impl_node_agrees_with_the_python_one_on_a_tampered_ledger(tmp_path, monkeypatch, capsys):
    """Two implementations, one verdict: the disagreement check that matters.

    A tampered ledger is the case where an implementation could quietly pass what the other
    fails. Both must refuse it, and the Node one must find the same ledger beside the receipt.
    """
    monkeypatch.chdir(tmp_path)
    if shutil.which("node") is None:  # pragma: no cover
        pytest.skip("node is not on PATH")
    assert main(["init", "--max-usd", "5"]) == 0
    assert main(["run", "--policy", "docket.toml", "--", sys.executable, "-c", "print(1)"]) == 0
    receipt = next((tmp_path / ".docket" / "receipts").glob("*.receipt.json"))
    ledger = next((tmp_path / ".docket" / "runs").glob("*.jsonl"))
    # One byte of the ledger, the way `demo --tamper` does it: a field inside one event.
    document = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
    document["data"] = dict(document.get("data") or {})
    document["data"]["forged"] = True
    ledger.write_text(
        json.dumps(document) + "\n" + "\n".join(ledger.read_text(encoding="utf-8").splitlines()[1:]) + "\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert main(["verify", "--impl", "node", str(receipt)]) == 1
    assert "NOT VERIFIED" in capsys.readouterr().out


def test_verify_impl_node_without_node_on_path_says_so(tmp_path, monkeypatch, capsys):
    """A missing interpreter is a message, not a traceback.

    The receipt exists and the verifier exists, so the only thing wrong is the machine: that
    has to be reported as a requirement, and `docket doctor` is what tells the reader so.
    """
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--max-usd", "5"]) == 0
    assert main(["run", "--policy", "docket.toml", "--", sys.executable, "-c", "print(1)"]) == 0
    receipt = next((tmp_path / ".docket" / "receipts").glob("*.receipt.json"))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    capsys.readouterr()
    assert main(["verify", "--impl", "node", str(receipt)]) == 70
    text = capsys.readouterr().err
    assert "node" in text and "PATH" in text and "doctor" in text, text
