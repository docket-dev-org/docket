"""The reusable GitHub Action in `action.yml`, tested by running it.

An action that has never been executed is a feature advertised as integrated when it is only
written down. So the steps here take the action's own `run:` blocks, substitute the
`${{ inputs.* }}` expressions for real values, and execute them against a real supervised run —
the same way GitHub would.

`tests/test_action_smoke.py` covers the workflow that drives the action in CI. This file covers the
action's own logic, so it can run without a runner.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to read action.yml")

REPO = Path(__file__).resolve().parents[1]
ACTION = REPO / "action.yml"


@pytest.fixture(scope="module")
def action() -> dict:
    """The action document, parsed."""
    text = ACTION.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    assert document["runs"]["using"] == "composite", "the action must be a composite action"
    return document


def _steps(action: dict) -> list[dict]:
    return [step for step in action["runs"]["steps"] if "run" in step]


def _substitute(script: str, values: dict[str, str]) -> str:
    """Replace `${{ inputs.name }}` and `${{ steps.x.outputs.y }}` with real values.

    The step's `if:` condition is not part of the script: which step runs is the runner's decision,
    and each test picks the step it means to exercise.
    """
    for name, value in values.items():
        script = script.replace(f"${{{{ inputs.{name} }}}}", value)
        script = script.replace(f"${{{{ inputs.{name} || {name} }}}}", value)
        script = script.replace(f"${{{{ steps.run.outputs.{name} }}}}", value)
    # Anything left unsubstituted is a bug in the test, not in the action.
    assert "${{" not in script, f"unsubstituted expression left in the script: {script}"
    return script


# --- the document ------------------------------------------------------------------------------ #


def test_the_action_declares_its_interface(action: dict) -> None:
    """Every input is documented, and every output is fed by a step that produces it."""
    for name, spec in action["inputs"].items():
        assert spec.get("description", "").strip(), f"input {name!r} has no description"
    for name, spec in action["outputs"].items():
        assert "value" in spec, f"output {name!r} has no value"
        step = re.search(r"steps\.([a-z]+)\.outputs\.", spec["value"])
        assert step, f"output {name!r} does not read a step output"
        assert any(step.group(1) == s.get("id") for s in action["runs"]["steps"]), (
            f"output {name!r} reads steps.{step.group(1)}, which is not a step id"
        )


def test_the_action_has_a_default_for_every_optional_input(action: dict) -> None:
    for name, spec in action["inputs"].items():
        if spec.get("required"):
            continue
        assert "default" in spec, f"optional input {name!r} has no default, so a runner must guess"


def test_the_required_input_is_the_command(action: dict) -> None:
    required = [name for name, spec in action["inputs"].items() if spec.get("required")]
    assert required == ["command"], required


def test_the_action_sets_branding(action: dict) -> None:
    """Marketplace-facing metadata, checked here so it cannot be dropped silently."""
    assert action.get("branding", {}).get("icon"), "the action has no icon"
    assert action.get("description", "").strip()


def _evaluate(condition: str, outputs: dict[str, str], inputs: dict[str, str]) -> bool:
    """Evaluate a GitHub `if:` expression the way the runner would.

    The expressions this action uses are small: `always()`, a comparison against a step output, and
    a comparison against an input. They are evaluated here rather than trusted, because a step whose
    condition can never be true is a step that never runs — and the tests that execute a step's
    `run:` block directly would not notice.
    """
    expression = condition.strip()
    expression = expression.replace("always()", "True")
    for name, value in inputs.items():
        expression = expression.replace(f"inputs.{name}", repr(value))
    # The outputs are keyed as `<step id>.outputs.<name>`, so a reference is a direct lookup.
    for name, value in outputs.items():
        expression = expression.replace(f"steps.{name}", repr(value))
    expression = expression.replace("&&", " and ").replace("||", " or ")
    # A mutation that writes `false` where `always()` was must fail with the assertion about the
    # condition, not with a NameError from evaluating a bare word.
    expression = re.sub(r"\bfalse\b", "False", expression)
    expression = re.sub(r"\btrue\b", "True", expression)
    assert "${{" not in expression and "steps." not in expression and "inputs." not in expression, (
        f"could not evaluate {condition!r} against the given outputs and inputs: {expression!r}"
    )
    return bool(eval(expression, {"__builtins__": {}}, {}))  # noqa: S307 - the expression is built above


def test_every_guarded_step_can_actually_run(action: dict) -> None:
    """A step whose condition is unsatisfiable is a step that never runs.

    The tests below execute each step's `run:` block directly, which is the right way to test the
    shell and the wrong way to notice a disabled step. This evaluates the conditions instead, with
    the outputs a real run produces.
    """
    inputs = {name: spec.get("default", "") for name, spec in action["inputs"].items() if not spec.get("required")}
    # The outputs a completed run leaves behind.
    outputs = {"run.outputs.exit-code": "0", "run.outputs.receipt": ".docket/latest.receipt.json"}
    for step in action["runs"]["steps"]:
        condition = step.get("if")
        if not condition:
            continue
        assert _evaluate(condition, outputs, inputs), (
            f"step {step.get('name')!r} has condition {condition!r}, which is false for a completed run"
        )


def test_a_step_guarded_on_a_missing_receipt_is_skipped(action: dict) -> None:
    """The other half: with no receipt, the steps that need one must not run."""
    inputs = {name: spec.get("default", "") for name, spec in action["inputs"].items() if not spec.get("required")}
    outputs = {"run.outputs.exit-code": "70", "run.outputs.receipt": ""}
    for step in action["runs"]["steps"]:
        condition = step.get("if")
        if not condition or "steps.run.outputs.receipt" not in condition:
            continue
        assert not _evaluate(condition, outputs, inputs), (
            f"step {step.get('name')!r} runs even when no receipt was produced"
        )


def test_a_step_guarded_on_an_input_honours_its_default(action: dict) -> None:
    """`verify: false` and `fail-on-violation: false` must really turn those steps off."""
    inputs = {name: spec.get("default", "") for name, spec in action["inputs"].items() if not spec.get("required")}
    outputs = {"run.outputs.exit-code": "0", "run.outputs.receipt": ".docket/latest.receipt.json"}
    for step in action["runs"]["steps"]:
        condition = step.get("if") or ""
        for name in ("verify", "fail-on-violation", "upload-evidence"):
            if f"inputs.{name} == 'true'" not in condition:
                continue
            on = dict(inputs, **{name: "true"})
            off = dict(inputs, **{name: "false"})
            assert _evaluate(condition, outputs, on), f"{name}=true does not enable {step.get('name')!r}"
            assert not _evaluate(condition, outputs, off), f"{name}=false does not disable {step.get('name')!r}"


def test_every_step_declares_a_shell(action: dict) -> None:
    """A composite step without `shell` is a step that does not run."""
    for index, step in enumerate(action["runs"]["steps"]):
        if "uses" in step:
            continue
        assert step.get("shell"), f"step {index} ({step.get('name')}) has no shell"


# --- the steps, executed ----------------------------------------------------------------------- #


@pytest.fixture
def a_run(tmp_path: Path):
    """A workspace the action's own steps can run in, with a scripted provider still listening.

    The fixture yields rather than returns, because the action's `run` step starts its *own*
    `docket run` — and that run needs somewhere to forward to. Stopping the fake provider at the
    end of setup would make the action's run fail with a connection error, which is the failure mode
    this fixture exists to avoid: the action has to be tested with a live upstream, not a memory of
    one.
    """
    from docket.fakeupstream import FakeUpstream, Step
    from docket.policy import Policy
    from docket.supervisor import Supervisor

    policy_path = tmp_path / "docket.toml"
    policy_path.write_text(Policy(name="action-test").to_toml(), encoding="utf-8")
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, urllib.request\n"
        "base = os.environ['OPENAI_BASE_URL']\n"
        "body = json.dumps({'model': 'gpt-6-luna', 'max_tokens': 32,"
        " 'messages': [{'role': 'user', 'content': 'hi'}]}).encode()\n"
        "r = urllib.request.Request(base + '/chat/completions', data=body,"
        " headers={'content-type': 'application/json'}, method='POST')\n"
        "with urllib.request.urlopen(r, timeout=30) as resp: print('ok')\n",
        encoding="utf-8",
    )

    upstream = FakeUpstream(steps=[Step(model="gpt-6-luna", input_tokens=800, output_tokens=120, text="ok")] * 4)
    upstream.start()
    try:
        # One supervised run first, so the evidence directory, the ledger and a receipt exist —
        # which is the state the action's later steps expect to find.
        supervisor = Supervisor(
            [sys.executable, str(agent)],
            policy=Policy.load(policy_path),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()
        yield {
            "command": f"{sys.executable} {agent}",
            "policy": str(policy_path),
            "version": "0.1.8",
            "label": "unit-test",
            "evidence-path": ".docket",
            "artifact-name": "docket-evidence",
            "tmp": str(tmp_path),
            "receipt": str(result.receipt_path),
            # What the action's `upstreams` input has to carry so its own `docket run` forwards
            # here rather than to the provider's API.
            "upstreams": f"openai={upstream.url}",
        }
    finally:
        upstream.stop()


def _run_capped_step(action: dict, values: dict[str, str], tmp_path: Path, upstream) -> dict[str, str]:
    """The action's run and report steps, against a policy whose cap is below the first call.

    Both steps run, because `cap-exceeded` is written by the report step rather than the run step:
    asserting it after the run step alone would be asserting on an output nothing has produced yet.
    """
    outputs = tmp_path / "github_output"
    outputs.write_text("", encoding="utf-8")
    env = dict(
        os.environ,
        GITHUB_OUTPUT=str(outputs),
        POLICY=values["policy"],
        LABEL="capped",
        UPSTREAMS=f"openai={upstream.url}",
    )
    receipt = ""
    stdout = ""
    for step_id in ("run", "report"):
        step = next(s for s in _steps(action) if s.get("id") == step_id)
        script = _substitute(step["run"], {**values, "receipt": receipt})
        completed = _bash(script, cwd=tmp_path, env=env)
        assert completed.returncode == 0, f"the {step_id} step failed, which would fail the whole job"
        stdout += completed.stdout
        written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines() if "=" in line)
        receipt = written.get("receipt", "")
    written["_stdout"] = stdout
    return written


def _bash(script: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(cwd),
        env=dict(os.environ, **(env or {})),
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_the_run_step_produces_the_outputs_the_action_promises(
    action: dict, a_run: dict[str, str], tmp_path: Path
) -> None:
    """The `run:` block of the action's run step, executed for real.

    This is the step whose shell logic is most likely to be wrong: it captures an exit code that
    `continue-on-error` would otherwise swallow, and it resolves the receipt path two different
    ways. Both are exercised here.
    """
    step = next(s for s in _steps(action) if s.get("id") == "run")
    outputs = tmp_path / "github_output"
    outputs.write_text("", encoding="utf-8")
    env = dict(
        os.environ,
        GITHUB_OUTPUT=str(outputs),
        POLICY=a_run["policy"],
        LABEL=a_run["label"],
        UPSTREAMS=a_run["upstreams"],
    )
    completed = _bash(_substitute(step["run"], a_run), cwd=Path(a_run["tmp"]), env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr

    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert "exit-code" in written, "the step wrote no exit code"
    assert written["exit-code"] == "0", written
    assert written["receipt"], "the step wrote no receipt path"
    # The step's receipt path is relative to *its* working directory, not to this process's. The
    # first version of this assertion resolved it against the repository root, which happened to
    # carry a `.docket/` from an earlier run — so it passed locally and failed in CI on a repository
    # that was fine. That is the failure mode a guard that passes for the wrong reason has.
    assert (Path(a_run["tmp"]) / written["receipt"]).is_file(), (
        f"the receipt {written['receipt']} does not exist in the run's own directory"
    )


def test_the_report_step_reads_a_real_receipt(action: dict, a_run: dict[str, str], tmp_path: Path) -> None:
    step = next(s for s in _steps(action) if s.get("id") == "report")
    outputs = tmp_path / "github_output"
    outputs.write_text("", encoding="utf-8")
    env = dict(os.environ, GITHUB_OUTPUT=str(outputs))
    completed = _bash(_substitute(step["run"], a_run), cwd=Path(a_run["tmp"]), env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert written.get("calls", "0") != "0", "the report step counted no calls"
    assert written.get("spend"), "the report step read no spend"
    assert written.get("cap-exceeded") == "0", (
        f"the report step read cap-exceeded={written.get('cap-exceeded')!r} from a run that exceeded no cap"
    )
    assert "::notice::" in completed.stdout, "the report step printed no annotation"


def test_the_verify_step_passes_a_real_receipt(action: dict, a_run: dict[str, str], tmp_path: Path) -> None:
    step = next(s for s in _steps(action) if s.get("id") == "verify")
    outputs = tmp_path / "github_output"
    outputs.write_text("", encoding="utf-8")
    env = dict(os.environ, GITHUB_OUTPUT=str(outputs))
    completed = _bash(_substitute(step["run"], a_run), cwd=Path(a_run["tmp"]), env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    written = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines() if "=" in line)
    assert written.get("verified") == "true", written
    assert (Path(a_run["tmp"]) / "verification.json").is_file(), "no verification report was written"


def test_the_verify_step_fails_a_tampered_receipt(action: dict, a_run: dict[str, str], tmp_path: Path) -> None:
    """A green action that cannot go red is not a gate.

    The receipt is tampered with — one byte of its spend — and the same step must now fail.
    """
    import json

    receipt_path = Path(a_run["receipt"])
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document["totals"]["cost_usd"] = "999.000000"
    receipt_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    step = next(s for s in _steps(action) if s.get("id") == "verify")
    outputs = tmp_path / "github_output"
    outputs.write_text("", encoding="utf-8")
    env = dict(os.environ, GITHUB_OUTPUT=str(outputs))
    completed = _bash(_substitute(step["run"], a_run), cwd=Path(a_run["tmp"]), env=env)
    assert completed.returncode != 0, "the verify step passed a tampered receipt"
    assert "::error::" in completed.stdout, "the step failed without naming what failed"


def test_the_violation_step_passes_a_clean_run(action: dict, a_run: dict[str, str], tmp_path: Path) -> None:
    step = next(s for s in _steps(action) if "policy violation" in s.get("name", "").lower())
    completed = _bash(_substitute(step["run"], a_run), cwd=Path(a_run["tmp"]))
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_violation_step_fails_a_receipt_with_a_finding(action: dict, a_run: dict[str, str], tmp_path: Path) -> None:
    """The other half: a receipt that carries a violation must turn the job red."""
    import json

    receipt_path = Path(a_run["receipt"])
    document = json.loads(receipt_path.read_text(encoding="utf-8"))
    document.setdefault("findings", []).append(
        {"rule": "write-scope", "title": "Wrote outside the watched scope", "detail": "agent wrote /etc/passwd"}
    )
    receipt_path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    step = next(s for s in _steps(action) if "policy violation" in s.get("name", "").lower())
    completed = _bash(_substitute(step["run"], a_run), cwd=Path(a_run["tmp"]))
    assert completed.returncode != 0, "the violation step passed a receipt with a write-scope finding"
    assert "::error::" in completed.stdout


def test_the_run_step_reports_the_cap_as_a_result(action: dict, tmp_path: Path) -> None:
    """Exit 42 is the boundary working, and the action must say so rather than failing.

    A policy with a cap below the first call's cost is driven for real, and the step's exit code
    must be 42 while the step itself still succeeds.
    """
    from docket.fakeupstream import FakeUpstream, Step
    from docket.policy import Caps, Policy
    from docket.supervisor import Supervisor

    policy = Policy(name="capped", caps=Caps(max_usd="0.000001"))
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, urllib.request\n"
        "base = os.environ['OPENAI_BASE_URL']\n"
        "body = json.dumps({'model': 'gpt-6-luna', 'max_tokens': 32,"
        " 'messages': [{'role': 'user', 'content': 'hi'}]}).encode()\n"
        "r = urllib.request.Request(base + '/chat/completions', data=body,"
        " headers={'content-type': 'application/json'}, method='POST')\n"
        "with urllib.request.urlopen(r, timeout=30) as resp: print('ok')\n",
        encoding="utf-8",
    )
    # The policy file is written before anything runs, because the action's own `docket run` reads
    # it from disk — the in-memory policy the first supervisor used is not what the action sees.
    (tmp_path / "docket.toml").write_text(policy.to_toml(), encoding="utf-8")
    values = {
        "command": f"{sys.executable} {agent}",
        "policy": str(tmp_path / "docket.toml"),
        "version": "0.1.8",
        "label": "capped",
        "evidence-path": ".docket",
        "artifact-name": "docket-evidence",
        "tmp": str(tmp_path),
    }

    upstream = FakeUpstream(steps=[Step(model="gpt-6-luna", input_tokens=800, output_tokens=120, text="ok")])
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, str(agent)],
            policy=policy,
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        supervisor.run()
        written = _run_capped_step(action, values, tmp_path, upstream)
    finally:
        upstream.stop()

    assert written["exit-code"] == "42", f"expected exit 42 from a fired cap, got {written['exit-code']}"
    assert "::notice::" in written["_stdout"], "the cap was not reported as a notice"
    assert written.get("cap-exceeded") not in (None, "", "0"), (
        f"the receipt recorded no exceeded cap: {written.get('cap-exceeded')!r}"
    )


# --- the smoke workflow that drives it --------------------------------------------------------- #


def test_the_smoke_workflow_uses_the_action() -> None:
    """The workflow that proves the action runs must actually use it."""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "action-smoke.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["smoke"]["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]
    assert "./" in uses, f"the smoke workflow never uses the action: {uses}"
    # And it must check the outputs, or it is only running the action for the artifact.
    assert any("outputs" in (step.get("run") or "") for step in steps if "run" in step), (
        "the smoke workflow runs the action but checks none of its outputs"
    )


def test_the_example_policy_the_action_defaults_to_exists() -> None:
    """`policy: docket.toml` is the default, and `examples/ci/docket.toml` is the documented one."""
    assert (REPO / "examples" / "ci" / "docket.toml").is_file()
    assert (REPO / "examples" / "ci" / "capped.toml").is_file()
    for name in ("docket.toml", "capped.toml"):
        from docket.policy import Policy

        Policy.load(REPO / "examples" / "ci" / name)
