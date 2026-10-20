"""`docket report --redact <profiles>`: a receipt and ledger you can hand over.

The example that motivated this said "Redact before shipping if you must", which is a manual step
dressed up as advice — and following it by hand breaks the hash chain, so the honest outcome of the
old advice was an artifact that no longer verified. Redaction is a command now, and the property
that matters is that the pair it produces still verifies. Every test here asserts that, because a
redaction that silently breaks the format is worse than the manual edit it replaced.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from docket.ledger import Ledger
from docket.redact import (
    PROFILES,
    RedactionError,
    RedactionReport,
    ledger_bytes_are_canonical,
    parse_profiles,
    rechain,
    redact_event,
    redact_receipt,
)
from docket.verify import verify_receipt

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def a_receipt(tmp_path: Path):
    """A real supervised run, with a path and an argv that need redacting."""
    from docket.fakeupstream import FakeUpstream, Step
    from docket.policy import Policy
    from docket.supervisor import Supervisor

    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, urllib.request\n"
        "base = os.environ['OPENAI_BASE_URL']\n"
        "body = json.dumps({'model': 'gpt-6-luna', 'max_tokens': 16,"
        " 'messages': [{'role': 'user', 'content': 'hi'}]}).encode()\n"
        "r = urllib.request.Request(base + '/chat/completions', data=body,"
        " headers={'content-type': 'application/json'}, method='POST')\n"
        "urllib.request.urlopen(r, timeout=30).read()\n"
        "open('notes.txt', 'w').write('/home/alice/projects/acme/src/main.py')\n",
        encoding="utf-8",
    )
    upstream = FakeUpstream(steps=[Step(model="gpt-6-luna", input_tokens=600, output_tokens=120, text="ok")])
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, str(agent)],
            policy=Policy(name="redact-test"),
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()
    finally:
        upstream.stop()
    return result


# --- the profile parser ------------------------------------------------------------------------ #


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("paths", ["paths"]),
        ("paths,argv", ["paths", "argv"]),
        ("argv, paths", ["argv", "paths"]),
        ("all", ["paths", "argv", "hosts", "secrets"]),
        ("all,paths", ["paths", "argv", "hosts", "secrets"]),
        ("paths,paths", ["paths"]),
    ],
)
def test_profiles_parse(spec: str, expected: list[str]) -> None:
    assert parse_profiles(spec) == expected


def test_an_unknown_profile_is_refused_and_names_the_choices() -> None:
    """A typo must not silently produce an unredacted file."""
    with pytest.raises(RedactionError, match="unknown redaction profile 'pathz'"):
        parse_profiles("pathz")
    with pytest.raises(RedactionError, match="choose from"):
        parse_profiles("pathz")


def test_an_empty_profile_list_is_refused() -> None:
    with pytest.raises(RedactionError, match="at least one profile"):
        parse_profiles("")
    with pytest.raises(RedactionError, match="at least one profile"):
        parse_profiles(",,")


# --- the redaction itself ---------------------------------------------------------------------- #


def test_paths_are_replaced_with_a_shape_preserving_placeholder() -> None:
    """The depth survives, so a reader can still tell a deep tree from a shallow one."""
    from docket.redact import redact_value

    report = RedactionReport(profiles=["paths"])
    out = redact_value("/home/alice/projects/acme/src/main.py", "path", ["paths"], report)
    assert out == "<redacted-path depth=6>", out
    assert "alice" not in out and "acme" not in out


def test_argv_is_replaced_by_a_placeholder_that_keeps_the_list_a_list() -> None:
    from docket.redact import redact_value

    report = RedactionReport(profiles=["argv"])
    out = redact_value(["/usr/bin/python3", "agent.py", "--secret-flag"], "argv", ["argv"], report)
    assert out == "<redacted-argv>", out


def test_a_secret_is_replaced_wherever_it_appears() -> None:
    """The profile runs over every string, because a secret in a place nobody expected is the
    failure mode."""
    from docket.redact import redact_value

    report = RedactionReport(profiles=["secrets"])
    for secret in (
        "sk-live-abcdef123456",
        "ghp_ABCDEFGHIJKLMNOPQRST",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1234567890-abcdef",
    ):
        out = redact_value(f"authorization: {secret}", "note", ["secrets"], report)
        assert out == "<redacted-secret>", f"{secret} survived redaction: {out}"


def test_a_host_and_a_port_are_both_replaced() -> None:
    from docket.redact import redact_value

    report = RedactionReport(profiles=["hosts"])
    assert redact_value("api.anthropic.com", "host", ["hosts"], report) == "<redacted-host>"
    assert redact_value(443, "port", ["hosts"], report) == 0


def test_a_profile_that_matches_nothing_replaces_nothing() -> None:
    """A profile applied to a document with no such surface must report zero, not guess."""
    from docket.redact import redact_value

    report = RedactionReport(profiles=["argv"])
    assert redact_value({"a": 1}, "", ["argv"], report) == {"a": 1}
    assert report.replaced == {}


# --- the pair still verifies ------------------------------------------------------------------- #


def test_the_redacted_pair_verifies(a_receipt, tmp_path: Path) -> None:
    """The property the whole feature exists for."""
    events = Ledger.read(a_receipt.ledger_path)
    report = RedactionReport(profiles=["paths", "argv", "hosts", "secrets"])
    redacted, chained = redact_receipt(
        json.loads(json.dumps(a_receipt.receipt)), events, ["paths", "argv", "hosts", "secrets"], report
    )

    from docket.redact import write_pair

    new_receipt, new_ledger = write_pair(
        redacted, chained, a_receipt.receipt_path, a_receipt.ledger_path, tmp_path / "out"
    )
    verification = verify_receipt(json.loads(new_receipt.read_text(encoding="utf-8")), ledger_path=new_ledger)
    assert verification.ok, "\n".join(f"{c.status:5} {c.name}: {c.detail}" for c in verification.checks)


def test_the_original_is_untouched_by_a_redaction(a_receipt, tmp_path: Path) -> None:
    """Raw evidence is not something a reporting command gets to overwrite."""
    before_receipt = a_receipt.receipt_path.read_text(encoding="utf-8")
    before_ledger = a_receipt.ledger_path.read_text(encoding="utf-8")

    events = Ledger.read(a_receipt.ledger_path)
    report = RedactionReport(profiles=["all"])
    redacted, chained = redact_receipt(json.loads(json.dumps(a_receipt.receipt)), events, ["all"], report)

    from docket.redact import write_pair

    write_pair(redacted, chained, a_receipt.receipt_path, a_receipt.ledger_path, tmp_path / "out")
    assert a_receipt.receipt_path.read_text(encoding="utf-8") == before_receipt
    assert a_receipt.ledger_path.read_text(encoding="utf-8") == before_ledger


def test_writing_the_pair_into_the_receipts_directory_leaves_the_originals_alone(a_receipt, tmp_path: Path) -> None:
    """The default `--out` is beside the receipt, so this is the path every user takes."""
    before_receipt = a_receipt.receipt_path.read_bytes()
    before_ledger = a_receipt.ledger_path.read_bytes()

    events = Ledger.read(a_receipt.ledger_path)
    report = RedactionReport(profiles=["all"])
    redacted, chained = redact_receipt(json.loads(json.dumps(a_receipt.receipt)), events, ["all"], report)

    from docket.redact import write_pair

    new_receipt, new_ledger = write_pair(
        redacted, chained, a_receipt.receipt_path, a_receipt.ledger_path, a_receipt.receipt_path.parent
    )
    assert new_receipt != a_receipt.receipt_path and new_ledger != a_receipt.ledger_path
    assert a_receipt.receipt_path.read_bytes() == before_receipt
    assert a_receipt.ledger_path.read_bytes() == before_ledger


def test_the_redaction_is_recorded_in_the_receipt(a_receipt, tmp_path: Path) -> None:
    """A receipt that says nothing about being redacted is lying about what it is."""
    events = Ledger.read(a_receipt.ledger_path)
    report = RedactionReport(profiles=["paths"])
    redacted, _chained = redact_receipt(json.loads(json.dumps(a_receipt.receipt)), events, ["paths"], report)
    block = redacted["redaction"]
    assert block["profiles"] == ["paths"]
    assert block["replaced"].get("paths", 0) > 0
    assert "re-chained" in block["note"]


def test_every_profile_leaves_a_verifiable_pair(a_receipt, tmp_path: Path) -> None:
    """Not just `all`: each profile on its own, because a profile can break the chain by itself."""
    events = Ledger.read(a_receipt.ledger_path)
    for profile in PROFILES:
        if profile == "all":
            continue
        report = RedactionReport(profiles=[profile])
        redacted, chained = redact_receipt(json.loads(json.dumps(a_receipt.receipt)), events, [profile], report)

        from docket.redact import write_pair

        new_receipt, new_ledger = write_pair(
            redacted, chained, a_receipt.receipt_path, a_receipt.ledger_path, tmp_path / profile
        )
        verification = verify_receipt(json.loads(new_receipt.read_text(encoding="utf-8")), ledger_path=new_ledger)
        assert verification.ok, f"profile {profile!r} broke the format:\n" + "\n".join(
            f"{c.status:5} {c.name}: {c.detail}" for c in verification.checks
        )


def test_the_redacted_receipt_no_longer_carries_the_original_paths(a_receipt, tmp_path: Path) -> None:
    """The actual point: the string the client should not see is not in the file."""
    original_cwd = a_receipt.receipt["session"]["cwd"]
    assert original_cwd, "the fixture produced no cwd to redact"

    events = Ledger.read(a_receipt.ledger_path)
    report = RedactionReport(profiles=["paths"])
    redacted, chained = redact_receipt(json.loads(json.dumps(a_receipt.receipt)), events, ["paths"], report)

    from docket.redact import write_pair

    new_receipt, new_ledger = write_pair(
        redacted, chained, a_receipt.receipt_path, a_receipt.ledger_path, tmp_path / "out"
    )
    text = new_receipt.read_text(encoding="utf-8") + new_ledger.read_text(encoding="utf-8")
    assert original_cwd not in text, "the original cwd survived redaction"
    assert str(a_receipt.ledger_path) not in text


# --- the chain helpers ------------------------------------------------------------------------- #


def test_rechain_uses_the_ledgers_genesis_constant() -> None:
    """The first version used zeroes, which made every hash differ from the verifier's."""
    from docket.ledger import GENESIS

    events = [Ledger.read.__self__ if False else None]  # placeholder removed below
    del events
    led = Ledger.open(Path("/tmp") / "redact-chain-probe.jsonl")
    try:
        led.append("note", {"message": "one"})
        led.append("note", {"message": "two"})
        read_back = list(led.events)
    finally:
        led.close()
    re_chained = rechain(read_back)
    assert re_chained[0].prev == GENESIS, "the chain does not start at the ledger's genesis"
    assert ledger_bytes_are_canonical(re_chained)


def test_a_redacted_events_hash_is_the_one_verify_recomputes() -> None:
    """Stated as a property rather than only through the CLI, so a failure names the event."""
    led = Ledger.open(Path("/tmp") / "redact-canon-probe.jsonl")
    try:
        led.append("note", {"message": "hello", "path": "/home/alice/x"})
        events = list(led.events)
    finally:
        led.close()
    report = RedactionReport(profiles=["paths"])
    redacted = [redact_event(event, ["paths"], report) for event in events]
    assert ledger_bytes_are_canonical(rechain(redacted))


# --- the CLI ----------------------------------------------------------------------------------- #


def test_the_cli_writes_the_pair_and_says_so(a_receipt, tmp_path: Path) -> None:
    out = tmp_path / "handover"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "docket",
            "report",
            "--redact",
            "paths,argv",
            str(a_receipt.receipt_path),
            "--out",
            str(out),
        ],
        cwd=str(REPO),
        env={"PATH": "/tmp/vf/bin:/usr/bin:/bin", "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "redacted" in completed.stdout
    receipts = list(out.glob("*.redacted.receipt.json"))
    ledgers = list(out.glob("*.redacted.jsonl"))
    assert receipts and ledgers, f"the CLI wrote no pair: {sorted(p.name for p in out.iterdir())}"
    # And what it wrote verifies, through the CLI rather than through the library.
    verified = subprocess.run(
        [sys.executable, "-m", "docket", "verify", str(receipts[0]), "--ledger", str(ledgers[0])],
        cwd=str(REPO),
        env={"PATH": "/tmp/vf/bin:/usr/bin:/bin", "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_the_cli_refuses_an_unknown_profile_without_writing(a_receipt, tmp_path: Path) -> None:
    out = tmp_path / "handover"
    completed = subprocess.run(
        [sys.executable, "-m", "docket", "report", "--redact", "pathz", str(a_receipt.receipt_path), "--out", str(out)],
        cwd=str(REPO),
        env={"PATH": "/tmp/vf/bin:/usr/bin:/bin", "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "pathz" in completed.stderr
    assert not out.exists(), "a refused redaction still wrote a pair"


def test_the_cli_says_so_when_no_ledger_can_be_found(a_receipt, tmp_path: Path) -> None:
    """A receipt redacted on its own is a receipt that no longer verifies, so the command refuses."""
    orphan = tmp_path / "orphan.receipt.json"
    orphan.write_text(json.dumps(a_receipt.receipt), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "docket",
            "report",
            "--redact",
            "paths",
            str(orphan),
            "--ledger",
            str(tmp_path / "absent.jsonl"),
        ],
        cwd=str(tmp_path),
        env={"PATH": "/tmp/vf/bin:/usr/bin:/bin", "PYTHONPATH": str(REPO / "src")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "no ledger found" in completed.stderr
