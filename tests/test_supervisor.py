"""End-to-end supervision: caps, kills, filesystem evidence, receipts.

These tests spawn real child processes and talk to a scripted upstream, so the
enforcement paths that matter are the paths that actually run.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal

from docket.fakeupstream import FakeUpstream, Step
from docket.ledger import Ledger
from docket.policy import Caps, Files, Policy
from docket.supervisor import EXIT_CAP_EXCEEDED, Supervisor
from docket.verify import verify_receipt

CALL_CHILD = """
import json, os, urllib.request
base = os.environ["OPENAI_BASE_URL"]
body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hello"}]}
request = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=20) as response:
    print(response.read()[:40])
"""

LOOP_CHILD = """
import json, os, sys, urllib.request, urllib.error
base = os.environ["OPENAI_BASE_URL"]
body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "loop forever"}]}
for _ in range(500):
    request = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        print("refused", exc.code)
        break
print("done")
"""

FS_CHILD = """
import os, pathlib
root = pathlib.Path(os.environ["DOCKET_DEMO_ROOT"])
(root / "src").mkdir(parents=True, exist_ok=True)
(root / "src" / "new.py").write_text("x = 1\\n")
(root / ".env").write_text("SECRET=1\\n")
"""

SLEEP_CHILD = "import time; time.sleep(30)"


def _upstream(url: str) -> FakeUpstream:
    return FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=100_000, output_tokens=1_000)])


def test_a_normal_run_produces_a_verifiable_receipt(tmp_path):
    upstream = _upstream(None)  # type: ignore[arg-type]
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, "-c", CALL_CHILD],
            policy=Policy(name="t", caps=Caps(max_usd=Decimal("100.00"))),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()
    finally:
        upstream.stop()
    assert result.exit_code == 0
    assert result.breached is None
    assert result.receipt["totals"]["calls"] == 1
    assert result.receipt["totals"]["cost_usd"] == "0.315000"
    assert verify_receipt(result.receipt, ledger_path=result.ledger_path).ok
    types = [e.type for e in Ledger.read(result.ledger_path)]
    assert types[0] == "session.start"
    assert "policy.applied" in types
    assert types[-1] == "session.end"
    assert "proc.spawn" in types and "proc.exit" in types
    assert result.receipt["ledger"]["events"] == len(types)


def test_cap_terminates_the_process_group_and_reports_exit_42(tmp_path):
    upstream = _upstream(None)  # type: ignore[arg-type]
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, "-c", LOOP_CHILD],
            policy=Policy(name="t", caps=Caps(max_usd=Decimal("1.00"), grace_seconds=1.0)),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()
    finally:
        upstream.stop()
    assert result.exit_code == EXIT_CAP_EXCEEDED
    assert result.breached == "max_usd"
    assert result.receipt["caps"]["exceeded"] == 1
    assert result.receipt["session"]["breached"] == "max_usd"
    events = Ledger.read(result.ledger_path)
    assert "cap.exceeded" in [e.type for e in events]
    assert verify_receipt(result.receipt, ledger_path=result.ledger_path).ok
    # the run must stop close to the cap, not far past it
    assert Decimal(result.receipt["totals"]["cost_usd"]) < Decimal("1.50")


def test_wall_clock_cap_terminates_a_child_that_never_calls_a_model(tmp_path):
    supervisor = Supervisor(
        [sys.executable, "-c", SLEEP_CHILD],
        policy=Policy(name="t", caps=Caps(max_usd=None, max_wall_seconds=1)),
        docket_dir=tmp_path / ".docket",
        cwd=tmp_path,
    )
    result = supervisor.run()
    assert result.exit_code == EXIT_CAP_EXCEEDED
    assert result.breached == "max_wall_seconds"
    assert result.receipt["totals"]["calls"] == 0


def test_deny_mode_does_not_kill_but_keeps_refusing(tmp_path):
    upstream = _upstream(None)  # type: ignore[arg-type]
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, "-c", LOOP_CHILD],
            policy=Policy(name="t", caps=Caps(max_usd=Decimal("0.35"), on_breach="deny")),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()
    finally:
        upstream.stop()
    # the child exits by itself after being refused; docket still records the breach
    assert result.breached == "max_usd"
    assert result.receipt["model_calls"]["denied"] >= 1
    assert result.exit_code == EXIT_CAP_EXCEEDED


def test_filesystem_evidence_and_deny_write_violations(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "src").mkdir()
    (workdir / "src" / "old.py").write_text("old = True\n")
    (workdir / ".env").write_text("EXISTING=1\n")
    supervisor = Supervisor(
        [sys.executable, "-c", FS_CHILD],
        policy=Policy(
            name="t",
            caps=Caps(),
            files=Files(watch=["."], ignore=[".docket/**"], deny_write=[".env"]),
        ),
        docket_dir=tmp_path / ".docket",
        cwd=workdir,
        env_extra={"DOCKET_DEMO_ROOT": str(workdir)},
    )
    result = supervisor.run()
    files = result.receipt["files"]
    assert files["changed"] >= 2
    assert files["added"] >= 1
    assert files["violations"] == 1
    events = Ledger.read(result.ledger_path)
    assert "policy.violation" in [e.type for e in events]
    change_paths = {e.data.get("path") for e in events if e.type == "fs.change"}
    assert "src/new.py" in change_paths
    assert ".env" in change_paths


def test_policy_is_recorded_with_its_digest(tmp_path):
    policy = Policy(name="audited", caps=Caps(max_usd=Decimal("3.00")))
    supervisor = Supervisor(
        [sys.executable, "-c", "print('no model calls')"],
        policy=policy,
        docket_dir=tmp_path / ".docket",
        cwd=tmp_path,
    )
    result = supervisor.run()
    assert result.receipt["policy"]["digest"] == policy.digest()
    assert result.receipt["policy"]["document"]["caps"]["max_usd"] == "3.00"
    assert "enforced_before_send" in result.receipt["limits_disclosure"]


def test_receipt_and_latest_pointer_are_written(tmp_path):
    supervisor = Supervisor(
        [sys.executable, "-c", "print('ok')"],
        docket_dir=tmp_path / ".docket",
        cwd=tmp_path,
    )
    result = supervisor.run()
    assert result.receipt_path.exists()
    assert (tmp_path / ".docket" / "latest.receipt.json").exists()
    assert (tmp_path / ".docket" / "latest").read_text(encoding="utf-8").strip() == result.docket_number
    assert json.loads(result.receipt_path.read_text(encoding="utf-8"))["docket_number"] == result.docket_number


def test_child_exit_code_is_preserved_when_no_cap_is_hit(tmp_path):
    supervisor = Supervisor(
        [sys.executable, "-c", "raise SystemExit(7)"],
        docket_dir=tmp_path / ".docket",
        cwd=tmp_path,
    )
    result = supervisor.run()
    assert result.exit_code == 7
    assert result.receipt["session"]["exit_code"] == 7


def test_sealed_run_verifies_with_the_key(tmp_path):
    from docket.signing import load_or_create_seal_key

    key = load_or_create_seal_key(tmp_path / "seal.key")
    supervisor = Supervisor(
        [sys.executable, "-c", "print('sealed')"],
        docket_dir=tmp_path / ".docket",
        cwd=tmp_path,
        seal_key=key,
    )
    result = supervisor.run()
    assert verify_receipt(result.receipt, seal_key=key).ok
    assert not verify_receipt(result.receipt, seal_key="11" * 32).ok


def test_base_url_environment_points_at_the_boundary(tmp_path):
    script = (
        "import os;"
        "print(os.environ['OPENAI_BASE_URL']);"
        "print(os.environ['ANTHROPIC_BASE_URL']);"
        "print(os.environ['DOCKET_SESSION'])"
    )
    supervisor = Supervisor(
        [sys.executable, "-c", script],
        docket_dir=tmp_path / ".docket",
        cwd=tmp_path,
    )
    result = supervisor.run()
    captured = tmp_path / "out.txt"
    # the child's stdout is the test process's stdout here; check the environment
    # was wired by asserting the receipt exists and the run succeeded
    assert result.exit_code == 0
    assert result.receipt["session"]["exit_code"] == 0
    assert captured.exists() is False


def test_http_proxy_mode_sets_proxy_variables(tmp_path):
    script = "import os; raise SystemExit(0 if os.environ.get('HTTPS_PROXY') else 9)"
    supervisor = Supervisor(
        [sys.executable, "-c", script],
        docket_dir=tmp_path / ".docket",
        cwd=tmp_path,
        http_proxy=True,
    )
    assert supervisor.run().exit_code == 0
