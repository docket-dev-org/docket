"""CLI surface, in-process SDK, and the offline demo."""

from __future__ import annotations

import io
import json
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


def test_demo_json_output_is_machine_readable(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["demo", "--max-usd", "2.00", "--json"]) == 0
    captured = capsys.readouterr().out
    start = captured.index("{")
    payload = json.loads(captured[start:])
    assert payload["verify_ok"] is True
    assert payload["stopped_as_expected"] is True
