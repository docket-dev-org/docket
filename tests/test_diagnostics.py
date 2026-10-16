"""Diagnostics: the self-test (synthetic monitoring) and the support bundle.

``docket selftest`` is the surface a CI job or a cron runs against a deployment, so the
properties it asserts must be the properties the project actually promises — forwarding,
recording, refusal, capping, throttling, breaking, injecting, caching, surviving a failing
disk, building a receipt, and catching a tampered one. If a probe ever stops being able to
fail, these tests fail, which is the only way a self-test stays worth running. The cache
probes earned their place on the first run: they found a supplied cache the boundary was
silently ignoring (see `CHANGELOG.md`, Unreleased → Fixed).
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from docket.diagnostics import (
    Probe,
    collect_diagnostics,
    environment_checks,
    render_probes,
    self_test,
    write_diagnostic_bundle,
)
from docket.ledger import Ledger


@pytest.fixture(scope="module")
def probes() -> list[Probe]:
    """The full probe suite, run once: each probe binds real sockets and makes real calls."""
    result, _workspace = self_test()
    return result


def test_every_probe_passes_or_says_why_it_could_not_run(probes):
    failures = [p for p in probes if p.status == "fail"]
    assert not failures, [(p.name, p.detail) for p in failures]
    assert len(probes) >= 15, "the probe suite must cover the guarantees, not a sample of them"


def test_the_probe_suite_covers_each_advertised_guarantee(probes):
    names = " | ".join(p.name for p in probes)
    for guarantee in (
        "forwards and records",  # the boundary actually proxies
        "provider-reported usage",  # usage is the provider's numbers, not an estimate
        "healthz",
        "readiness",
        "metrics for the guarantees",
        "denied model",  # policy refusal
        "crosses a cap",  # budget enforcement
        "throttles",  # rate limiting
        "circuit",  # circuit breaking
        "not recorded as a successful",  # failure classification
        "injects faults",  # chaos
        "repeated request from the cache",  # the response cache answers without the provider
        "a hit costs nothing",  # ...and the saving is not a spend
        "miss, never a wrong answer",  # ...and a different request is still a call
        "descriptor was closed",  # recovery
        "failing write",  # evidence degradation under a real kernel limit
        "closing a ledger on a failing filesystem",  # cleanup that must not raise
        "verifies offline",  # receipts
        "one-field edit",  # verification actually rejects
    ):
        assert guarantee in names, f"no probe covers {guarantee!r}"


def test_probe_details_carry_evidence_not_just_a_verdict(probes):
    """A probe that prints "ok" is a probe nobody can argue with, which is worthless."""
    for probe in probes:
        if probe.status == "skipped":
            assert probe.detail, "a skip must say why it could not run"
            continue
        detail = probe.detail
        assert detail and detail.strip().lower() not in {"ok", "passed", "true", "yes", "done"}
        assert any(char.isdigit() for char in detail), f"probe {probe.name!r} reports no numbers: {detail!r}"
        assert len(detail.split()) >= 3, f"probe {probe.name!r} reports a fragment: {detail!r}"
        assert detail.strip().lower() != probe.name.strip().lower(), "the detail must add information"


def test_render_probes_marks_the_three_states_and_totals_them():
    probes = [
        Probe(name="a", status="pass", detail="did the thing (3 calls)"),
        Probe(name="b", status="fail", detail="did not (0 calls)"),
        Probe(name="c", status="skipped", detail="no /dev/full here"),
    ]
    text = render_probes(probes)
    assert "PASS  a" in text and "FAIL  b" in text and "SKIP  c" in text
    assert "1 passed, 1 failed, 1 skipped" in text


# --------------------------------------------------------------------------- #
# The bundle
# --------------------------------------------------------------------------- #


@pytest.fixture()
def populated_docket_dir(tmp_path: Path) -> Path:
    docket_dir = tmp_path / ".docket"
    runs = docket_dir / "runs"
    runs.mkdir(parents=True)
    ledger = Ledger.open(runs / "run-1.jsonl", max_bytes=None)
    ledger.append("model.request", {"model": "claude-sonnet-5", "body": {"secret": "PROMPT-CANARY-TEXT"}})
    ledger.append("model.response", {"model": "claude-sonnet-5", "usage": {"input_tokens": 41}, "cost_usd": "0.0001"})
    ledger.append("session.end", {"exit_code": 0, "breached": None})
    ledger.close()
    damaged = runs / "run-2.jsonl"
    ledger = Ledger.open(damaged, max_bytes=None)
    ledger.append("note", {"message": "before the crash"})
    ledger.close()
    with damaged.open("a", encoding="utf-8") as handle:
        handle.write('{"torn": ')  # what a killed run leaves behind
    receipts = docket_dir / "receipts"
    receipts.mkdir()
    (receipts / "DKT-20260101-AAAAAA.receipt.json").write_text(
        json.dumps(
            {
                "format": "docket/receipt/v1",
                "docket_number": "DKT-20260101-AAAAAA",
                "created_at": "2026-01-01T00:00:00Z",
                "tool": {"version": "0.1.0"},
                "session": {"exit_code": 0},
                "totals": {"calls": 1, "cost_usd": "0.0001", "total_tokens": 41},
                "ledger": {"events": 3, "head": "a" * 64, "merkle_root": "b" * 64, "file_sha256": "c" * 64},
                "evidence": {"ledger_complete": True, "unwritten_events": 0},
                "ops": {"chaos_run": False},
                "findings": [{"rule": "fat-context"}],
            }
        ),
        encoding="utf-8",
    )
    return docket_dir


def test_collect_diagnostics_reports_health_per_ledger(populated_docket_dir: Path):
    report = collect_diagnostics(populated_docket_dir)
    by_name = {entry["name"]: entry for entry in report["ledgers"]}
    assert by_name["run-1.jsonl"]["healthy"] is True
    assert by_name["run-1.jsonl"]["lines"] == 3
    assert by_name["run-2.jsonl"]["healthy"] is False
    assert by_name["run-2.jsonl"]["torn_tail"] is True
    assert report["receipts"][0]["docket_number"] == "DKT-20260101-AAAAAA"
    assert report["receipts"][0]["finding_rules"] == ["fat-context"]
    assert report["checks"] and all({"name", "ok", "detail"} <= set(c) for c in report["checks"])


def test_the_bundle_carries_no_prompts_receipts_or_ledger_bodies(populated_docket_dir: Path, tmp_path: Path):
    """A diagnostic bundle that leaks a customer's prompts is worse than no bundle."""
    target = tmp_path / "bundle.tar.gz"
    write_diagnostic_bundle(populated_docket_dir, target)
    blob = target.read_bytes()
    assert b"PROMPT-CANARY-TEXT" not in blob, "ledger bodies must never leave the machine in a bundle"
    with tarfile.open(target, "r:gz") as archive:
        names = sorted(archive.getnames())
        assert names == ["README.txt", "diagnostics.json"]
        payload = json.loads(archive.extractfile("diagnostics.json").read().decode("utf-8"))
        readme = archive.extractfile("README.txt").read().decode("utf-8")
    assert payload["docket"]["version"]
    assert payload["environment"]["python"]
    assert payload["ledgers"] and payload["receipts"]
    assert "does NOT contain" in readme, "the bundle states what it excludes"
    assert "PROMPT-CANARY-TEXT" not in json.dumps(payload)


def test_environment_checks_are_data_not_prose():
    checks = environment_checks(Path.cwd())
    assert any(check["name"].startswith("python") for check in checks)
    assert all(isinstance(check["ok"], bool) for check in checks)
    assert all(isinstance(check["detail"], str) and check["detail"] for check in checks)


def test_collecting_diagnostics_on_an_empty_directory_still_answers(tmp_path: Path):
    """Support is most often asked for a machine where nothing has run yet."""
    report = collect_diagnostics(tmp_path / "nothing-here")
    assert report["ledgers"] == [] and report["receipts"] == []
    assert report["checks"], "environment checks still run when there are no ledgers"
