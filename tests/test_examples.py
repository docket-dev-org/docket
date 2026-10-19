"""The five examples, executed — because "runnable" is a claim about the tool, not about the prose.

`examples/` is advertised in `README.md` and in `docs/testing.md` as five runnable
examples, each with its own README, and until this file existed **nothing ran them**: a renamed flag,
a moved module or a policy key the loader stopped accepting would have left five documents that lie
and a suite that stayed green. Each test below drives an example's own artefact:

* the quickstart and the SDK example are run as programs, offline, in a temporary directory, and the
  receipt each one prints is then verified with the command the example itself prints;
* the two policy files are handed to the same loader a user's policy goes through, under a real
  supervised run of a command that does nothing;
* every README is checked for links and for the repository paths its commands name.

Two things about the plumbing. `docket`'s one-line run summary goes to **stderr** (stdout stays clean
for a command's own output), so the assertions read the two streams joined. And an example's exit
status reports *its own* result, not the supervised run's: `run_example.py` exits `0` when the
verification it printed passed and `1` when it did not — the `42` on its `exit code` line is the
supervised command's status, which is what the receipt records.

What is *not* run here: `examples/client-deliverable`'s walkthrough, which is a sequence a person
types against their own repository (its README is checked like the others, and the CLI commands it
names are covered end to end by `test_cli_sdk_demo.py`), and the CI wiring of `examples/ci`, which is
a workflow fragment rather than a program.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

#: Directories a README may name as *this repository's* files. Anything else — `.docket/`, `agent.py`,
#: `DKT-*.receipt.json`, `evidence.md` — belongs to the reader's run or to the reader's own program,
#: which is the point of those examples and cannot exist in this tree.
REPO_PREFIXES = ("examples/", "src/", "tools/", "verify-js/", "deploy/", "spec/", "docs/")

PATH_TOKEN = re.compile(rf"(?<![\w/.])((?:{'|'.join(REPO_PREFIXES)})[\w./-]*)")

POLICIES = sorted(EXAMPLES.glob("*/docket.toml"))


def _run(args: list[str], tmp_path: Path, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    """Run a command the way a reader would: offline, in a temporary home, with `src` importable."""
    return subprocess.run(
        args,
        cwd=ROOT,
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _out(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _capture(output: str, pattern: str, group: int = 1) -> str:
    """The `group`-th capture of `pattern` in `output`, or a failure that shows what was there."""
    match = re.search(pattern, output, re.MULTILINE)
    assert match is not None, f"no match for {pattern!r} in the output:\n{output}"
    return match.group(group)


def _verify(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "docket", "verify", *args], tmp_path)


def test_the_quickstart_runs_offline_and_stops_at_the_cap(tmp_path):
    """The example's own claim, executed: four scripted calls, a breach, exit 42, a verified receipt.

    The README states the exit code, the breached cap, the call tally, the token total and the exact
    arithmetic ($0.004510, unchanged between runs) as the output to expect; this asserts the live run
    says the same things, then takes the two paths the run printed and runs the verification command
    the run printed *for* them. A quickstart that prints a receipt nobody can verify is a screenshot.
    """
    result = _run([sys.executable, "examples/quickstart/run_example.py"], tmp_path)
    assert result.returncode == 0, "0 = the verification below passed\n" + _out(result)
    output = _out(result)
    for expected in (
        "policy      max_usd = $0.0005, on_breach = deny",
        "model calls 4 scripted (third one is expensive)",
        "agent: call 3 ok — 40,000 in, 300 out",
        "agent: call 4 refused at the boundary (403, max_usd)",
        "exit code   42  (42 = terminated by a cap; 0 = completed)",
        "breached    max_usd",
        "spend       $0.004510 of $0.0005 cap",
        "calls       3 forwarded, 1 refused",
        "tokens      42,300 (reported: provider usage fields)",
        "PASS  ledger-chain: 18 events, every hash and link recomputed",
        "VERIFIED",
    ):
        assert expected in output, f"{expected!r} is not in the quickstart's output:\n{output}"

    receipt = Path(_capture(output, r"^receipt\s+(\S+)$"))
    printed = re.search(r"^  docket verify (\S+) --ledger (\S+)$", output, re.MULTILINE)
    assert printed is not None, f"the run does not print its own verification command:\n{output}"
    assert Path(printed.group(1)) == receipt, "the command it prints names the receipt it wrote"
    ledger = Path(printed.group(2))
    assert receipt.is_file() and ledger.is_file(), "the printed paths are the run's artefacts"

    checked = _verify(tmp_path, str(receipt), "--ledger", str(ledger))
    assert checked.returncode == 0, _out(checked)
    assert "VERIFIED" in _out(checked)

    # The README also prints the Node command, so the two implementations are given the same inputs.
    assert "node verify-js/verify.mjs " + str(receipt) in output, "the Node route is printed too"


def test_the_quickstart_receipt_stops_verifying_when_its_ledger_is_edited(tmp_path):
    """The example's last line is an instruction: "edit one byte of the ledger and run it again".

    A verifier that only ever says yes is the failure this project exists to make visible, so the
    instruction is executed: one digit of one token count changes, and the same command must fail and
    name what it found instead of passing. The digit is taken from the ledger itself rather than
    assumed, so this test breaks if the ledger ever stops carrying the rows the receipt recomputes.
    """
    result = _run([sys.executable, "examples/quickstart/run_example.py"], tmp_path)
    assert result.returncode == 0, _out(result)
    output = _out(result)
    receipt = Path(_capture(output, r"^receipt\s+(\S+)$"))
    ledger = Path(_capture(output, r"^  docket verify \S+ --ledger (\S+)$"))

    original = ledger.read_text(encoding="utf-8")
    edited_text, changes = re.subn(
        r'"input_tokens":(\d+)',
        lambda match: f'"input_tokens":{int(match.group(1)) + 1}',
        original,
        count=1,
    )
    assert changes == 1, "the ledger no longer carries a token count to edit"
    assert edited_text != original
    edited = tmp_path / "edited.jsonl"
    edited.write_text(edited_text, encoding="utf-8")

    checked = _verify(tmp_path, str(receipt), "--ledger", str(edited))
    assert checked.returncode == 1, "an edited ledger is not a pass\n" + _out(checked)
    assert "FAIL" in _out(checked) and "ledger-chain" in _out(checked), _out(checked)


def test_the_sdk_example_records_the_agents_own_claims_and_verifies(tmp_path):
    """The distinction the SDK example is about: what the boundary observed and what the agent said.

    The example prints the event kinds and at least one JSON line showing `reported_by: "agent"`, and
    ends with the command that verifies the receipt it wrote. Both are asserted, because the example's
    whole argument is that a reader can tell testimony from observation.
    """
    result = _run([sys.executable, "examples/custom-agent/agent_sdk.py"], tmp_path)
    assert result.returncode == 0, _out(result)
    output = result.stdout
    assert '"reported_by": "agent"' in output, output
    for kind in ("model.request", "model.response", "tool.call", "tool.result", "session.start"):
        assert kind in output, f"{kind} is not among the ledger's event kinds:\n{output}"

    receipt = Path(_capture(output, r"docket verify (\S+)$"))
    assert receipt.is_file(), output
    checked = _verify(tmp_path, str(receipt))
    assert checked.returncode == 0, _out(checked)
    assert "VERIFIED" in _out(checked)


def test_the_sdk_example_also_runs_under_the_supervisor(tmp_path):
    """The supervised path of the SDK example, which nothing ran until now.

    The example's own docstring and `examples/custom-agent/README.md` both advertise

        python3 -m docket run --max-usd 0.10 -- python3 examples/custom-agent/agent_sdk.py

    and the example used to **crash** there: it printed `session.ledger.path` unconditionally, and
    an attached session has `ledger = None` by design, because the supervisor is the single writer
    to that file. Only the standalone path was tested, so the crash shipped and the README kept
    advertising the command that caused it.

    The test now drives the supervised path and asserts the two things that make it correct rather
    than merely non-crashing: the agent's own events reached the run's ledger, labelled as claims
    (`reported_by: "agent"`), and the receipt the run wrote verifies with the chain intact. An
    attached session that silently dropped its events would pass a "does not crash" test.
    """
    workdir = tmp_path / "work"
    workdir.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "docket",
            "run",
            "--max-usd",
            "0.10",
            "--",
            sys.executable,
            str(EXAMPLES / "custom-agent" / "agent_sdk.py"),
        ],
        cwd=str(workdir),
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    # cwd is passed twice above; the later keyword wins, and the example is addressed from ROOT.
    assert result.returncode == 0, _out(result)
    output = _out(result)
    assert "attached" in output, "the example did not report that it attached to the run:\n" + output

    receipt = workdir / ".docket" / "latest.receipt.json"
    assert receipt.is_file(), "the supervised run wrote no receipt:\n" + output
    checked = _verify(tmp_path, str(receipt))
    assert checked.returncode == 0, _out(checked)
    assert "VERIFIED" in _out(checked), _out(checked)

    # The agent's own claim is in the run's chain, and it is labelled as a claim.
    ledgers = sorted((workdir / ".docket" / "runs").glob("*.jsonl"))
    assert ledgers, "no ledger beside the receipt"
    events = [json.loads(line) for line in ledgers[-1].read_text(encoding="utf-8").splitlines()]
    claimed = [event for event in events if event["data"].get("reported_by") == "agent"]
    assert claimed, "the agent's own events never reached the run's ledger:\n" + _out(checked)
    assert any(event["type"] == "tool.call" for event in claimed), (
        "the agent's tool.call is not among the events it asserted"
    )


def test_the_examples_directory_holds_the_policies_this_file_expects():
    """A guard for the parametrisation below: a renamed policy would otherwise run zero cases."""
    assert [path.parent.name for path in POLICIES] == ["ci", "subscription"]


@pytest.mark.parametrize("policy", POLICIES, ids=[path.parent.name for path in POLICIES])
def test_every_example_policy_is_accepted_by_a_real_run(policy: Path, tmp_path):
    """`examples/ci` and `examples/subscription` are *policies*: the claim is that they load.

    Not that a parser can read them — that a real supervised run of a trivial command accepts them and
    completes, which is the step where a missing key, a bad value or a `mode` the loader refuses would
    fail. The command is `python3 -c pass`, so no model call is made and the egress rules the policies
    carry are exercised only as far as parsing them.
    """
    result = _run(
        [
            sys.executable,
            "-m",
            "docket",
            "run",
            "-p",
            str(policy),
            "--docket-dir",
            str(tmp_path / "docket"),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        tmp_path,
    )
    assert result.returncode == 0, f"{policy.name} was refused\n" + _out(result)
    assert "completed" in _out(result), _out(result)
    assert list((tmp_path / "docket" / "receipts").glob("*.receipt.json")), "a receipt is still written"


def test_every_example_has_a_readme_that_lists_it_and_links_something_that_exists():
    """The directory listing in `examples/README.md` and every relative link inside every example."""
    index = (EXAMPLES / "README.md").read_text(encoding="utf-8")
    directories = sorted(path.name for path in EXAMPLES.iterdir() if path.is_dir())
    assert directories, "examples/ has no directories"
    for name in directories:
        readme = EXAMPLES / name / "README.md"
        assert readme.is_file(), f"{name} has no README"
        assert f"]({name}/README.md)" in index, f"examples/README.md does not link {name}"
        text = readme.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#\s]+)\)", text):
            if "://" in target:
                continue
            resolved = (readme.parent / target).resolve()
            assert resolved.exists(), f"{name}/README.md links {target}, which does not exist"


def test_every_repository_path_an_example_readme_names_exists():
    """Commands rot fastest: a flag can survive a rename that a documented path does not.

    Only paths into *this* repository are checked (`examples/`, `src/`, `verify-js/`, …); an example
    that tells a reader to run `./agent.sh` or read `.docket/latest.receipt.json` is naming the
    reader's own file, and those are exactly the paths that must not exist here.
    """
    readmes = sorted(EXAMPLES.glob("*/README.md")) + [EXAMPLES / "README.md"]
    assert len(readmes) >= 6, "the example READMEs are the subject of this test"
    for readme in readmes:
        text = readme.read_text(encoding="utf-8")
        for token in sorted(set(PATH_TOKEN.findall(text))):
            target = token.rstrip('.,;:)"')
            assert (ROOT / target).exists(), f"{readme.relative_to(ROOT)} names {target}, which is not in the tree"


def test_every_python_file_under_examples_compiles():
    """The files nobody runs still have to be Python: a syntax error is a broken example."""
    python = sorted(EXAMPLES.rglob("*.py"))
    assert len(python) >= 3, "there are three runnable example programs"
    for path in python:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
