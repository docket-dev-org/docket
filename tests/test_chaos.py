"""Chaos experiments and the audit that judges them.

Two things are being tested, and the second matters more than the first:

1. ``docket chaos`` runs a real supervised agent through injected faults and produces a
   receipt that declares them.
2. The audit can *fail*. An auditor that only ever prints PASS is decoration, so these
   tests hand it deliberately bad records — a 500 filed as a success, injections with no
   declaration, a receipt whose totals disagree with its own ledger — and require it to
   object, by name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docket import events as ev
from docket.chaos import (
    GAME_DAY_PHASES,
    agent_summary,
    audit_run,
    ledger_facts,
    render_chaos,
    render_post_mortem,
    run_chaos,
    run_game_day,
    run_phase,
)
from docket.ledger import Ledger
from docket.policy import Caps, Policy
from docket.receipt import build_receipt
from docket.verify import verify_receipt


def _status(checks: list[dict[str, str]], name: str) -> dict[str, str]:
    for check in checks:
        if check["name"] == name:
            return check
    raise AssertionError(f"no check named {name!r} in {[c['name'] for c in checks]}")


def _receipt_for(ledger: Ledger, **overrides) -> dict:
    kwargs = {
        "events": list(ledger),
        "ledger": ledger,
        "meter": None,
        "policy": Policy(name="audit", caps=Caps(max_usd=None)),
        "session_id": "audit",
        "docket_number": "DKT-20260101-AUDIT0",
        "exit_code": 0,
        "duration_seconds": 1.0,
        "price_table": None,
        "created_at": "2026-01-01T00:00:00Z",
    }
    kwargs.update(overrides)
    from docket.pricing import CostMeter, PriceTable

    kwargs["meter"] = kwargs["meter"] or CostMeter(price_table=PriceTable.load())
    kwargs["price_table"] = kwargs["price_table"] or PriceTable.load()
    return build_receipt(**kwargs)


# --------------------------------------------------------------------------- #
# The auditor must be able to fail
# --------------------------------------------------------------------------- #


def test_a_failed_call_filed_as_a_success_is_caught(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "bad.jsonl", max_bytes=None)
    ledger.append(ev.MODEL_REQUEST, {"model": "claude-sonnet-5"})
    ledger.append(ev.MODEL_RESPONSE, {"model": "claude-sonnet-5", "status": 500, "usage": {}})
    ledger.append(ev.SESSION_END, {"exit_code": 0})
    ledger.close()
    checks, _ = audit_run(tmp_path / "bad.jsonl", None)
    verdict = _status(checks, "no failed call was recorded as a successful one")
    assert verdict["status"] == "fail"
    assert "500" in verdict["detail"]


def test_undeclared_fault_injection_is_caught(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "chaos.jsonl", max_bytes=None)
    ledger.append("x-docket.fault", {"kind": "status", "detail": "forced status 503"})
    ledger.append(ev.MODEL_ERROR, {"status": 503})
    ledger.append(ev.SESSION_END, {"exit_code": 0})
    ledger.close()
    receipt = _receipt_for(ledger)
    receipt["ops"]["chaos_run"] = False  # as if a run lied about being an experiment
    checks, _ = audit_run(tmp_path / "chaos.jsonl", receipt)
    verdict = _status(checks, "fault injection is declared in the receipt")
    assert verdict["status"] == "fail"
    assert verdict["detail"] == "1 injection(s) recorded; receipt ops.chaos_run=False"


def test_a_receipt_whose_totals_disagree_with_its_ledger_is_caught(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "totals.jsonl", max_bytes=None)
    ledger.append(ev.MODEL_RESPONSE, {"model": "claude-sonnet-5", "usage": {"input_tokens": 10}})
    ledger.close()
    receipt = _receipt_for(ledger)
    receipt["totals"]["calls"] = 7
    checks, _ = audit_run(tmp_path / "totals.jsonl", receipt)
    verdict = _status(checks, "the receipt's call count matches the recorded successful calls")
    assert verdict["status"] == "fail"
    assert "receipt totals.calls=7" in verdict["detail"]


def test_a_broken_chain_is_reported_by_the_audit(tmp_path: Path):
    """An edited ledger must not be reported as a clean record."""
    ledger = Ledger.open(tmp_path / "edited.jsonl", max_bytes=None)
    ledger.append(ev.MODEL_RESPONSE, {"model": "claude-sonnet-5", "usage": {"input_tokens": 10}})
    ledger.append(ev.SESSION_END, {"exit_code": 0})
    ledger.close()
    lines = (tmp_path / "edited.jsonl").read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["data"]["model"] = "gpt-6-luna"
    lines[0] = json.dumps(tampered)
    (tmp_path / "edited.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    checks, facts = audit_run(tmp_path / "edited.jsonl", None)
    assert facts["diagnostics"]["chain_error"] is not None
    assert facts["diagnostics"]["healthy"] is False
    verdict = _status(checks, "the ledger file parses cleanly")
    assert verdict["status"] == "fail"


def test_an_agent_that_did_not_report_its_own_counts_is_a_skip_not_a_pass(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "quiet.jsonl", max_bytes=None)
    ledger.append(ev.MODEL_RESPONSE, {"model": "claude-sonnet-5", "usage": {}})
    ledger.close()
    checks, _ = audit_run(tmp_path / "quiet.jsonl", None)
    verdict = _status(checks, "every failed response the agent saw is explained by the record")
    assert verdict["status"] == "skip"
    assert "nothing independent to cross-check" in verdict["detail"]


def test_agent_summary_only_trusts_the_agents_own_machine_readable_report():
    notes = [
        "step 1 done",
        'scriptagent: {"calls": 3, "statuses": [200, 500, 429], "refusals": ["rate_limited"], "mode": "retry"}',
        "epilogue",
    ]
    summary = agent_summary(notes)
    assert summary == {"calls": 3, "statuses": [200, 500, 429], "refusals": ["rate_limited"], "mode": "retry"}
    assert agent_summary(["scriptagent: not json"]) is None
    assert agent_summary([]) is None


def test_the_cross_check_uses_the_agents_counts_to_explain_the_ledger(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "cross.jsonl", max_bytes=None)
    ledger.append(ev.MODEL_ERROR, {"status": 500, "provider": "openai"})
    ledger.append(ev.NOTE, {"message": 'scriptagent: {"statuses": [500, 200], "calls": 2, "refusals": []}'})
    ledger.close()
    checks, _ = audit_run(tmp_path / "cross.jsonl", None)
    verdict = _status(checks, "every failed response the agent saw is explained by the record")
    assert verdict["status"] == "pass"
    assert "agent received 1 non-2xx of 2 calls" in verdict["detail"]


# --------------------------------------------------------------------------- #
# A real chaos run
# --------------------------------------------------------------------------- #


def test_run_chaos_injects_faults_and_records_the_experiment(tmp_path: Path):
    outcome = run_chaos(faults_spec="status=503:1.0", seed=4, calls=3, workspace=tmp_path / "chaos")
    assert outcome["exit_code"] == 0
    facts = outcome["facts"]
    assert len(facts["faults"]) == 3, "every injected fault is in the ledger"
    assert facts["errors"] == 3 and facts["model_responses"] == 0
    assert outcome["checks"] and not [c for c in outcome["checks"] if c["status"] == "fail"]

    receipt = json.loads(Path(outcome["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["ops"]["chaos_run"] is True
    assert receipt["ops"]["fault_injection"]["injections"] == 3
    assert receipt["evidence"]["ledger_complete"] is True
    result = verify_receipt(receipt, ledger_path=outcome["ledger_path"])
    assert result.ok, result.failures
    chaos_check = [c for c in result.checks if c.name == "chaos-run"]
    assert chaos_check and chaos_check[0].status == "skip", "a chaos receipt must say so at verification time"


def test_run_chaos_against_a_local_provider_never_reaches_the_network(tmp_path: Path):
    """The experiment is deterministic and offline: no key, no spend, same result."""
    first = run_chaos(faults_spec="drop:1.0", seed=9, calls=2, workspace=tmp_path / "a")
    second = run_chaos(faults_spec="drop:1.0", seed=9, calls=2, workspace=tmp_path / "b")
    assert first["facts"]["denial_codes"] or first["facts"]["errors"]
    assert len(first["facts"]["faults"]) == len(second["facts"]["faults"]) == 2
    assert first["facts"]["counts"].get("model.denied") == second["facts"]["counts"].get("model.denied")


def test_chaos_reports_reads_as_a_verdict_not_a_log(tmp_path: Path):
    outcome = run_chaos(faults_spec="status=500:1.0", seed=2, calls=2, workspace=tmp_path / "chaos")
    text = render_chaos(outcome)
    assert "PASS  no failed call was recorded as a successful one" in text
    assert "receipt:" in text
    assert "passed," in text and "failed," in text and "skipped" in text


def test_a_scratch_workspace_is_removed_unless_it_is_kept(tmp_path: Path, monkeypatch):
    """A tool that runs a hundred times a day must not leave a hundred directories behind.

    The rule is stated in one place (``docket.util.scratch_workspace``) and both entry points
    obey it: a directory the caller supplied is kept, a scratch one is removed at exit — and the
    report says which, so nothing prints a path that will not be there.
    """
    from docket.util import scratch_workspace

    monkeypatch.delenv("DOCKET_KEEP_SCRATCH", raising=False)
    supplied = tmp_path / "mine"
    path, kept = scratch_workspace("docket-test-", path=supplied)
    assert (path, kept) == (supplied, True)

    auto, kept = scratch_workspace("docket-test-")
    assert kept is False and Path(auto).is_dir()
    scratch, kept = scratch_workspace("docket-test-", keep=True)
    assert kept is True and Path(scratch).is_dir()
    Path(scratch).rmdir()

    monkeypatch.setenv("DOCKET_KEEP_SCRATCH", "1")
    env_kept, kept = scratch_workspace("docket-test-")
    assert kept is True
    Path(env_kept).rmdir()


def test_a_chaos_run_says_whether_its_evidence_survives(tmp_path: Path):
    outcome = run_chaos(faults_spec="status=503:1.0", seed=1, calls=1, workspace=tmp_path / "kept")
    assert outcome["workspace_kept"] is True
    text = render_chaos(outcome)
    assert "workspace kept at" in text and str(tmp_path / "kept") in text


def test_a_game_day_says_whether_its_evidence_survives(tmp_path: Path):
    report = run_game_day(root=tmp_path / "day", phases=[p for p in GAME_DAY_PHASES if p.name == "baseline"])
    assert report["root_kept"] is True


# --------------------------------------------------------------------------- #
# Game day phases
# --------------------------------------------------------------------------- #


def test_the_baseline_phase_runs_offline_and_verifies(tmp_path: Path):
    phase = next(p for p in GAME_DAY_PHASES if p.name == "baseline")
    record = run_phase(phase, root=tmp_path)
    assert record["phase_ok"] is True
    assert record["verification"]["ok"] is True
    assert record["facts"]["errors"] == 0
    assert record["facts"]["model_responses"] == 4
    assert (tmp_path / "baseline" / ".docket" / "receipts").exists()


def test_the_circuit_breaker_phase_proves_the_breaker_opens_by_name(tmp_path: Path):
    phase = next(p for p in GAME_DAY_PHASES if p.name == "circuit-breaker")
    record = run_phase(phase, root=tmp_path)
    assert record["phase_ok"] is True, [c for c in record["checks"] if c["status"] == "fail"]
    facts = record["facts"]
    assert facts["error_statuses"] == [500]
    assert facts["model_responses"] == 0, "a run where every call failed records no successful call"
    assert "circuit_open" in facts["denial_codes"]
    receipt = json.loads(Path(record["receipt_path"]).read_text(encoding="utf-8"))
    breaker = receipt["ops"]["circuit_breaker"]["openai"]
    assert breaker["openings"] == 1 and breaker["state"] == "open"
    assert breaker["total_failures"] >= 5


def test_the_rate_limit_phase_records_the_limiter_state_and_a_retry_hint(tmp_path: Path):
    phase = next(p for p in GAME_DAY_PHASES if p.name == "rate-limit")
    record = run_phase(phase, root=tmp_path)
    assert record["phase_ok"] is True, [c for c in record["checks"] if c["status"] == "fail"]
    throttles = record["facts"]["rate_limited"]
    assert throttles, "the ceiling must actually have been hit"
    # Whole numbers stay integers in a hashed structure (no floats, §3.3), so 2/s is
    # recorded as 2 — a decimal string only when the value is genuinely fractional.
    assert all(item["limiter"]["per_second"] == 2 for item in throttles)
    assert all(float(item["retry_after_seconds"]) > 0 for item in throttles)


def test_the_ledger_pressure_phase_stops_the_run_instead_of_dropping_events(tmp_path: Path):
    phase = next(p for p in GAME_DAY_PHASES if p.name == "ledger-pressure")
    record = run_phase(phase, root=tmp_path)
    assert record["phase_ok"] is True, [c for c in record["checks"] if c["status"] == "fail"]
    assert record["exit_code"] == 42
    assert record["breached"] in {"max_events", "max_ledger_bytes"}
    caps = [c["cap"] for c in record["facts"]["cap_events"]]
    assert "max_events" in caps
    assert record["facts"]["diagnostics"]["healthy"] is True, "nothing was lost; the run refused instead"


def test_a_subset_of_phases_runs_and_the_post_mortem_lists_expectations(tmp_path: Path):
    report = run_game_day(root=tmp_path, phases=[p for p in GAME_DAY_PHASES if p.name == "baseline"])
    assert report["phases_total"] == 1
    assert report["phases_ok"] == 1
    text = render_post_mortem(report)
    assert "# Game day post-mortem" in text
    assert "## What this proves" in text
    assert "## What this does not prove" in text, "a post-mortem that oversells is a marketing document"
    assert "## Known limits" in text
    assert "Expected:" in text and "Observed:" in text


def test_ledger_facts_re_derives_counts_from_the_file(tmp_path: Path):
    ledger = Ledger.open(tmp_path / "facts.jsonl", max_bytes=None)
    ledger.append(ev.MODEL_REQUEST, {"model": "m"})
    ledger.append(ev.MODEL_RESPONSE, {"model": "m", "usage": {"input_tokens": 5}})
    ledger.append(ev.MODEL_DENIED, {"code": "model_policy"})
    ledger.append(ev.MODEL_ERROR, {"status": 503})
    ledger.append("x-docket.rate-limited", {"retry_after_seconds": "1.5"})
    ledger.append("x-docket.fault", {"kind": "drop"})
    ledger.close()
    facts = ledger_facts(tmp_path / "facts.jsonl")
    assert facts["model_requests"] == 1 and facts["model_responses"] == 1
    assert facts["denials"] == 1 and facts["denial_codes"] == ["model_policy"]
    assert facts["error_statuses"] == [503]
    assert len(facts["rate_limited"]) == 1 and len(facts["faults"]) == 1


def test_the_rollback_phase_really_puts_the_seeded_files_back(tmp_path):
    """The disaster-recovery drill, end to end: damage, breach, restore, and the evidence intact.

    The unit tests cover the restore function; this covers the *drill* — a real phase in the
    standing game day, run against a real synthetic agent, audited from the disk.
    """
    phase = next(p for p in GAME_DAY_PHASES if p.name == "rollback")
    record = run_phase(phase, root=tmp_path / "day")
    workspace = tmp_path / "day" / "rollback"

    # Each assertion carries the record when it fails. This test is a whole subprocess run, and
    # a bare "assert False" from a phase that only misbehaves under load is a dead end: the
    # record is what says *which* expectation moved (`duration_seconds`, the exit code, the
    # receipt's ops block, the audit's own checks).
    assert (record["exit_code"], record["breached"]) == (42, "max_usd"), {
        k: record.get(k) for k in ("exit_code", "breached", "duration_seconds", "env", "checks")
    }
    for relative, content in phase.seed_files.items():
        assert (workspace / relative).read_text(encoding="utf-8") == content, f"{relative} was not restored"
    assert record["receipt_ops"]["snapshot"]["taken"] is True, record["receipt_ops"]
    assert record["receipt_ops"]["rollback"]["applied"] is True, record["receipt_ops"]
    assert record["facts"]["rollbacks"], f"the rollback is a ledger event: {record['facts']}"
    assert record["verification"]["ok"], record["verification"]["failures"]
    failures = [check for check in record["checks"] if check["status"] == "fail"]
    assert not failures, failures
    # The evidence the rollback was derived from is still there to be re-checked.
    ledger_path = Path(record["ledger_path"])
    assert ledger_path.exists() and Path(record["receipt_path"]).exists()


@pytest.mark.parametrize("phase_name", [p.name for p in GAME_DAY_PHASES])
def test_every_phase_declares_what_it_expects_before_it_runs(phase_name: str):
    """A phase with no expectations is a phase that cannot fail, which is not a test."""
    phase = next(p for p in GAME_DAY_PHASES if p.name == phase_name)
    assert phase.description.strip()
    assert len(phase.expect) >= 2
    assert all(isinstance(item, str) and item for item in phase.expect)
