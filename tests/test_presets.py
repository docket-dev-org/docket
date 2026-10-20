"""`docket init --from <preset>`, exercised end to end.

A preset is only useful if the policy it writes is one the parser accepts and one a real run can
use. So each preset is written to disk, loaded back through the real `Policy.load`, and — for the
ones that name a model the price table knows — driven through a supervised run against the
scripted provider, which then has to verify.

The first version of `policy_for` wrote `egress.mode = "allow"`, which the parser refuses: the only
values are `deny` and `tunnel`. A preset that writes an invalid policy is worse than no preset, so
`test_every_preset_writes_a_policy_the_parser_accepts` is the load-bearing one here.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from docket.policy import DEFAULT_EGRESS_ALLOW, Policy
from docket.presets import PRESET_KEYS, PRESETS, describe_presets, next_steps, policy_for, preset
from docket.pricing import PriceTable

# --- the registry ------------------------------------------------------------------------------ #


def test_the_registry_has_the_frameworks_the_roadmap_promises() -> None:
    """`ROADMAP.md` §0.2 names five frameworks plus a generic profile; the registry must have them."""
    for key in ("claude-code", "codex", "gemini-cli", "aider", "opencode", "http-proxy", "generic"):
        assert key in PRESET_KEYS, f"no preset for {key}"


def test_keys_are_unique_and_shell_safe() -> None:
    assert len(PRESET_KEYS) == len(set(PRESET_KEYS)), "duplicate preset keys"
    for key in PRESET_KEYS:
        assert key.islower() and " " not in key and "/" not in key, key


def test_a_lookup_returns_the_preset_or_nothing() -> None:
    assert preset("claude-code") is not None
    assert preset("no-such-thing") is None


def test_the_listing_names_every_preset() -> None:
    listing = describe_presets()
    for key in PRESET_KEYS:
        assert key in listing, f"{key} is missing from `docket init --from`"
    assert ".." not in listing, "the listing has a doubled full stop"


# --- the policy each preset writes -------------------------------------------------------------- #


@pytest.mark.parametrize("key", PRESET_KEYS)
def test_every_preset_writes_a_policy_the_parser_accepts(key: str, tmp_path: Path) -> None:
    """The load-bearing test: a policy the parser refuses is a preset that cannot ship."""
    chosen = preset(key)
    assert chosen is not None
    path = tmp_path / "docket.toml"
    path.write_text(policy_for(chosen, name="probe").to_toml(), encoding="utf-8")
    loaded = Policy.load(path)
    assert loaded.name == "probe"
    assert loaded.caps.max_usd == Decimal(chosen.max_usd)
    assert loaded.models.allow == list(chosen.model_patterns)


@pytest.mark.parametrize("key", PRESET_KEYS)
def test_a_preset_is_never_looser_than_the_default(key: str) -> None:
    """`egress.mode = allow` was the first version of this, and it would have allowed every host.

    A preset's allow-list must be the default's plus its own additions, and its mode must be
    `deny`, so a host the policy does not name is still refused.
    """
    chosen = preset(key)
    assert chosen is not None
    policy = policy_for(chosen, name="probe")
    assert policy.egress.mode == "deny", f"{key} sets egress.mode to {policy.egress.mode!r}"
    for host in DEFAULT_EGRESS_ALLOW:
        assert policy.egress.decide(host)[0], f"{key} dropped the default allow entry {host!r}"
    assert not policy.egress.decide("not-a-provider.example.com")[0], f"{key} allows an unnamed host"


@pytest.mark.parametrize("key", PRESET_KEYS)
def test_a_preset_names_a_positive_cap(key: str) -> None:
    chosen = preset(key)
    assert chosen is not None
    assert Decimal(chosen.max_usd) > 0, f"{key} has a cap of {chosen.max_usd}"
    assert chosen.max_tokens is None or chosen.max_tokens > 0
    assert chosen.max_model_calls is None or chosen.max_model_calls > 0


def test_the_model_patterns_are_patterns_the_price_table_can_match() -> None:
    """A preset that names a model no price entry matches silently prices at the catch-all.

    The catch-all is $1.25/$10.00, which is a real price and therefore a silent one: the run is
    recorded and costed, just wrongly. So every pattern a preset names is matched here.
    """
    table = PriceTable.load()
    for chosen in PRESETS:
        for pattern in chosen.model_patterns:
            matched = [price for price in table.prices if price.pattern == pattern]
            assert matched, f"{chosen.key} names model pattern {pattern!r}, which the price table does not have"


# --- the command each preset prints ------------------------------------------------------------- #


@pytest.mark.parametrize("key", PRESET_KEYS)
def test_next_steps_prints_a_runnable_command(key: str, tmp_path: Path) -> None:
    """The printed command has to be a command, with the policy path substituted in."""
    chosen = preset(key)
    assert chosen is not None
    policy_path = str(tmp_path / "docket.toml")
    steps = next_steps(chosen, policy_path)
    assert steps, f"{key} prints no next step"
    assert policy_path in steps[0]
    assert "{policy}" not in steps[0], "the placeholder was not substituted"
    assert chosen.command in steps[0]


def test_the_command_is_not_a_placeholder() -> None:
    """A preset whose command is `your-agent` is honest for the generic profile and a lie elsewhere."""
    for chosen in PRESETS:
        if chosen.key in ("http-proxy", "generic"):
            continue
        assert "your-agent" not in chosen.command, f"{chosen.key} still prints a placeholder command"


def test_every_preset_that_needs_a_variable_names_one() -> None:
    """A framework Docket cannot attach to has to say which variable to set.

    `opencode` is the exception, and it says so in its notes instead.
    """
    for chosen in PRESETS:
        if chosen.key == "opencode":
            assert any("opencode.json" in note for note in chosen.notes), "opencode must say where to configure it"
            continue
        assert chosen.attach, f"{chosen.key} names no environment variable"


def test_the_variables_a_preset_names_are_ones_the_supervisor_injects() -> None:
    """A preset that tells the reader to set a variable Docket does not inject is a contradiction.

    The reader would set it by hand, which works, but the preset's promise — "`docket run` injects
    these" — would be false. Every variable named here must be in `_base_url_env`.
    """
    from docket.supervisor import _base_url_env

    injected = _base_url_env("127.0.0.1", 0, "full")
    for chosen in PRESETS:
        for name, _what in chosen.attach:
            assert name in injected, f"{chosen.key} names {name}, which the supervisor does not inject"


def test_gemini_cli_gets_the_variable_gemini_cli_reads() -> None:
    """The gap this preset was written to close.

    The supervisor injected `GEMINI_API_BASE_URL`, which no framework reads. Gemini CLI reads
    `GOOGLE_GEMINI_BASE_URL`, so without it the one framework that needed the boundary talked to
    Google directly and Docket recorded nothing.
    """
    from docket.supervisor import _base_url_env

    injected = _base_url_env("127.0.0.1", 8080, "full")
    assert "GOOGLE_GEMINI_BASE_URL" in injected, "Gemini CLI's variable is not injected"
    assert injected["GOOGLE_GEMINI_BASE_URL"].endswith("/gemini")


# --- the presets actually work ------------------------------------------------------------------ #


@pytest.mark.parametrize("key", ["claude-code", "codex", "gemini-cli", "aider", "http-proxy"])
def test_a_preset_policy_drives_a_run_that_verifies(key: str, tmp_path: Path) -> None:
    """The whole point: write the policy, run an agent under it, verify the receipt.

    The agent is a scripted one and the provider is the scripted fake, so this is a real supervised
    run through the real boundary with the real parser, not a unit test of `to_toml`.
    """
    from docket.fakeupstream import FakeUpstream, Step
    from docket.supervisor import Supervisor
    from docket.verify import verify_receipt

    chosen = preset(key)
    assert chosen is not None
    policy = policy_for(chosen, name=key)
    probe_model = _PROBE_MODELS[chosen.model_patterns[0]]

    upstream = FakeUpstream(steps=[Step(model=probe_model, input_tokens=900, output_tokens=220, text="ok")])
    upstream.start()
    workspace = tmp_path / "work"
    workspace.mkdir()
    agent = tmp_path / "agent.py"
    agent.write_text(_SCRIPTED_AGENT.replace("claude-sonnet-5", probe_model), encoding="utf-8")
    try:
        supervisor = Supervisor(
            [__import__("sys").executable, str(agent)],
            policy=policy,
            docket_dir=workspace / ".docket",
            cwd=workspace,
            upstreams={"openai": upstream.url, "anthropic": upstream.url, "gemini": upstream.url},
        )
        result = supervisor.run()
    finally:
        upstream.stop()

    # The agent must have reached the boundary, not the provider: a run whose agent bypassed the
    # boundary records nothing, which is the failure the presets exist to prevent.
    assert result.receipt["totals"]["calls"] >= 1, f"{key}: the agent made no recorded call"
    verification = verify_receipt(result.receipt, ledger_path=result.ledger_path)
    assert verification.ok, f"{key}: the receipt did not verify:\n{verification.render(color=False)}"


def test_the_cli_writes_the_preset_and_says_what_to_run(tmp_path: Path) -> None:
    """`docket init --from claude-code` through the real CLI, in a real directory."""
    import os
    import subprocess
    import sys

    from docket.cli import main

    policy_path = tmp_path / "docket.toml"
    completed = subprocess.run(
        [sys.executable, "-m", "docket", "init", "--from", "claude-code", "--path", str(policy_path)],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert policy_path.is_file(), "the CLI wrote no policy"
    assert "claude" in completed.stdout, "the CLI did not name the command to run"
    # And what it wrote must load.
    assert Policy.load(policy_path).caps.max_usd == Decimal("15.00")
    assert main is not None


def test_an_unknown_preset_is_refused_with_the_list(tmp_path: Path) -> None:
    """A typo must not silently write the generic policy."""
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-m", "docket", "init", "--from", "claude-cod", "--path", str(tmp_path / "d.toml")],
        cwd=str(root),
        env=dict(os.environ, PYTHONPATH=str(root / "src")),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "no such preset" in completed.stderr
    assert "claude-code" in completed.stderr, "the refusal must name the presets so the typo is fixable"
    assert not (tmp_path / "d.toml").exists(), "a refused preset still wrote a policy"


#: A concrete model name for each pattern a preset allows. The run's agent has to call a model the
#: preset's own `models.allow` admits, or the boundary correctly refuses it with a 403 and the test
#: is measuring the model policy rather than the preset.
_PROBE_MODELS = {
    "*opus*": "claude-opus-5",
    "*sonnet*": "claude-sonnet-5",
    "*haiku*": "claude-haiku-4",
    "gpt-5*": "gpt-5.6-codex",
    "gpt-6*": "gpt-6.1",
    "o3*": "o3",
    "o4*": "o4",
    "gemini-*-pro*": "gemini-2.5-pro",
    "gemini-*-flash*": "gemini-2.5-flash",
    "deepseek-*": "deepseek-chat",
    "*": "gpt-6-luna",
}


#: An agent that makes one model call through the boundary, reading the base URL from the
#: environment the way a real agent does. The path is the OpenAI-shaped one, which every preset's
#: framework understands.
_SCRIPTED_AGENT = """
import json
import os
import urllib.request

base = os.environ.get("OPENAI_BASE_URL") or os.environ.get("DOCKET_PROXY_URL", "")
body = json.dumps({
    "model": "claude-sonnet-5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "hello"}],
}).encode()
request = urllib.request.Request(
    base + "/chat/completions", data=body,
    headers={"content-type": "application/json"}, method="POST",
)
with urllib.request.urlopen(request, timeout=60) as response:
    payload = json.loads(response.read())
print("agent saw model:", payload.get("model"))
"""
