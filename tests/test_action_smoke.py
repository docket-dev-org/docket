"""The pieces `.github/workflows/action-smoke.yml` depends on, tested without a runner.

The workflow is the only thing that proves the action runs in GitHub Actions, and it cannot run
here. What *can* be checked here is everything it relies on: that the scripted provider answers the
route the boundary forwards to, that the smoke agent makes exactly one call and exits 0, that the
policies it names load, and that the workflow really uses the action and checks its outputs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to read the workflow")

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def provider_url() -> str:
    """The fake provider, started the way the workflow starts it, and stopped afterwards."""
    process = subprocess.Popen(
        [sys.executable, str(REPO / "examples" / "ci" / "fake_provider.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The workflow polls for the URL line; the same wait is used here so the test exercises the
        # same startup contract rather than a sleep.
        url = ""
        for _ in range(100):
            line = process.stdout.readline() if process.stdout else ""
            if line.strip():
                url = line.strip()
                break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        assert url.startswith("http://"), f"the provider printed no URL: {url!r}"
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


# --- the scripted provider --------------------------------------------------------------------- #


def test_the_provider_answers_the_route_the_boundary_forwards(provider_url: str) -> None:
    """`/v1/chat/completions` is what the boundary appends to the openai upstream."""
    status, payload = _post(f"{provider_url}/v1/chat/completions", {"model": "gpt-6-luna", "messages": []})
    assert status == 200, payload
    assert payload["usage"]["prompt_tokens"] > 0, "the response carries no usage, so nothing can be costed"
    assert payload["choices"][0]["message"]["content"]


def test_the_provider_names_a_model_the_price_table_knows(provider_url: str) -> None:
    """A model outside the price table is costed at the catch-all, which is wrong rather than zero."""
    from docket.pricing import PriceTable

    table = PriceTable.load()
    status, payload = _post(f"{provider_url}/v1/chat/completions", {"model": "gpt-6-luna"})
    assert status == 200
    model = payload["model"]
    assert any(price.pattern == "gpt-6-luna*" for price in table.prices), (
        f"the provider answers as {model!r}, which the price table does not name"
    )


def test_the_provider_refuses_a_route_it_does_not_serve(provider_url: str) -> None:
    """A 404 is how a mis-pointed boundary shows up, rather than an empty receipt."""
    status, _payload = _post(f"{provider_url}/v1/embeddings", {"input": "x"})
    assert status == 404


# --- the smoke agent ---------------------------------------------------------------------------- #


def test_the_smoke_agent_makes_one_call_and_exits_zero(provider_url: str, tmp_path: Path) -> None:
    """The workflow asserts `calls >= 1`; the agent is what makes that true."""
    completed = subprocess.run(
        [sys.executable, str(REPO / "examples" / "ci" / "agent.py")],
        cwd=str(tmp_path),
        env=dict(os.environ, OPENAI_BASE_URL=provider_url),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "gpt-6-luna" in completed.stdout


def test_the_smoke_agent_exits_42_when_the_boundary_refuses(provider_url: str, tmp_path: Path) -> None:
    """The capped path of the workflow depends on this exit code meaning "the cap fired"."""
    # Point the agent at a provider that refuses, by asking for a path it does not serve.
    completed = subprocess.run(
        [sys.executable, str(REPO / "examples" / "ci" / "agent.py")],
        cwd=str(tmp_path),
        env=dict(os.environ, OPENAI_BASE_URL=f"{provider_url}/v1"),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_smoke_agent_says_so_when_nothing_is_injected(tmp_path: Path) -> None:
    """An agent that silently does nothing when detached is a green job that tested nothing."""
    completed = subprocess.run(
        [sys.executable, str(REPO / "examples" / "ci" / "agent.py")],
        cwd=str(tmp_path),
        env={k: v for k, v in os.environ.items() if k not in ("OPENAI_BASE_URL", "DOCKET_PROXY_URL")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "DOCKET_PROXY_URL" in completed.stderr


# --- the workflow itself ------------------------------------------------------------------------ #


def test_the_workflow_uses_the_action_and_checks_its_outputs() -> None:
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "action-smoke.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["smoke"]["steps"]
    uses = [step.get("uses") for step in steps if "uses" in step]
    assert "./" in uses, f"the smoke workflow never uses the action: {uses}"
    assert any("outputs" in (step.get("run") or "") for step in steps if "run" in step), (
        "the smoke workflow runs the action but checks none of its outputs"
    )


def test_the_workflow_passes_the_scripted_provider_to_the_action() -> None:
    """Without `upstreams`, the action's own `docket run` would forward to the real API."""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "action-smoke.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["smoke"]["steps"]
    action_steps = [step for step in steps if step.get("uses") == "./"]
    assert action_steps, "the workflow never invokes the action"
    for step in action_steps:
        assert "upstreams" in step.get("with", {}), (
            f"the action step {step.get('id')!r} passes no upstreams, so it would call the real API"
        )


def test_the_workflow_checks_the_cap_path() -> None:
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "action-smoke.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["smoke"]["steps"]
    capped = [step for step in steps if step.get("id") == "capped"]
    assert capped, "the workflow never exercises the cap path"
    asserting = [step for step in steps if "run" in step and "steps.capped.outputs.exit-code" in step["run"]]
    assert asserting, "the workflow runs the capped path but never asserts its exit code"
    assert any("cap-exceeded" in (step.get("run") or "") for step in steps), (
        "the workflow never asserts the cap-exceeded count, so exit 42 alone would pass a run whose receipt recorded nothing"
    )


def test_the_policies_the_workflow_names_load() -> None:
    from docket.policy import Policy

    for name in ("docket.toml", "capped.toml"):
        Policy.load(REPO / "examples" / "ci" / name)


def test_the_capped_policy_really_does_fire() -> None:
    """A "capped" policy that does not cap would make the smoke test's exit-42 assertion vacuous."""
    from docket.policy import Policy

    policy = Policy.load(REPO / "examples" / "ci" / "capped.toml")
    assert policy.caps.max_usd is not None
    assert policy.caps.max_usd < Decimal("0.001"), f"the capped policy allows ${policy.caps.max_usd}"


from decimal import Decimal  # noqa: E402  - used by the assertion above
