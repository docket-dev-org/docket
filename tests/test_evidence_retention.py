"""Retention of the provider responses the boundary saw.

The subject is a gap with a specific shape. A ``model.request`` event can carry the request body
verbatim; the matching ``model.response`` event could only ever carry ``response_bytes`` — a length.
The bytes were then dropped. That is invisible until the provider is one Docket cannot read, at which
point the token counts on the event are an *estimate* derived from those very bytes and the bytes are
gone: the call is recorded, the response is not, and the estimate can never be checked.

So these tests are in two halves. The first is about what is written and what is said about it: the
digest is unconditional, the body is opt-in, a truncated body says it is truncated, and a body that
was asked for and could not be kept says why. The second is about whether any of it is *checked*: a
retained body that nothing re-hashes is decoration, and ``docket verify`` has to be able to tell you
the file is not the bytes the boundary returned.

Nothing here parses a provider response. Capturing a response because it was seen, rather than
because it was understood, is the whole point.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from docket.evidence import (
    DEFAULT_MAX_RESPONSE_BYTES,
    ResponseCapture,
    capture_response,
    check_retained,
    digest,
    responses_root,
    stored_digest_is_consistent,
)
from docket.policy import Policy

# --- the digest, which is not optional ---------------------------------------------------------- #


def test_the_digest_is_recorded_when_nothing_is_retained(tmp_path: Path) -> None:
    """Retention off is the default, and the default still identifies the response.

    This is the property that makes the feature safe to leave off: an operator who does not want
    response bodies on disk still gets a 64-character handle on every one, which is what lets
    anyone who *does* obtain a body prove it is the body Docket saw.
    """
    capture = capture_response(b'{"ok":true}', directory=tmp_path)
    assert capture.retained is False
    assert capture.sha256 == digest(b'{"ok":true}')
    assert capture.stored_sha256 is None
    assert capture.path is None
    assert capture.note is None, "an unrequested omission is a configuration, not a failure"
    assert not (tmp_path / "responses").exists()


def test_the_digest_covers_the_whole_body_even_when_only_a_prefix_is_kept(tmp_path: Path) -> None:
    capture = capture_response(b"x" * 500, retain=True, max_bytes=100, directory=responses_root(tmp_path))
    assert capture.sha256 == digest(b"x" * 500), "the whole-body digest must not shrink with the cap"
    assert capture.stored_sha256 == digest(b"x" * 100)
    assert capture.sha256 != capture.stored_sha256
    assert capture.truncated is True
    assert capture.stored_bytes == 100
    assert capture.total_bytes == 500


def test_a_body_that_fits_is_not_marked_truncated(tmp_path: Path) -> None:
    capture = capture_response(b"small", retain=True, max_bytes=1024, directory=responses_root(tmp_path))
    assert capture.truncated is False
    assert capture.stored_bytes == capture.total_bytes
    assert capture.sha256 == capture.stored_sha256


# --- what the retention actually writes --------------------------------------------------------- #


@pytest.mark.parametrize(
    "rel,root,expected",
    [
        ("responses/ab.bin", Path("/run/.docket"), "responses/ab.bin"),
        ("responses/ab.bin", None, "responses/ab.bin"),
    ],
)
def test_a_path_is_recorded_relative_to_the_run_directory(rel: str, root: Path | None, expected: str) -> None:
    """Relative so a receipt travels; absolute only when there is no root to be relative to.

    The absolute form is kept rather than guessed at in the one case it can happen — a path that is
    not under the root — because a path that resolves to nothing is worse than a path that names a
    machine.
    """
    capture = ResponseCapture(4, "a" * 64, True, 4, "b" * 64, Path(rel), False)
    assert capture.to_dict(root=root)["response_path"] == expected


def test_a_capture_cannot_be_altered_after_it_is_built() -> None:
    """A capture is evidence about a write that already happened, so it is frozen.

    ``frozen=True`` is not decoration here: an in-memory object that describes bytes on disk must
    not be editable by whoever holds it, or the digest on the event and the digest in memory can
    drift apart with no way to tell which one was measured.
    """
    import dataclasses

    capture = capture_response(b"body", retain=True, directory=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        capture.sha256 = "0" * 64  # type: ignore[misc]


def test_truncated_defaults_to_false_when_a_capture_is_built_by_hand() -> None:
    """The default is part of the contract: a capture that says nothing is not a truncated one."""
    capture = ResponseCapture(4, "a" * 64, True, 4, "b" * 64, None)
    assert capture.truncated is False
    assert capture.to_dict()["response_truncated"] is False


def test_a_path_outside_the_run_directory_keeps_its_absolute_form() -> None:
    capture = ResponseCapture(4, "a" * 64, True, 4, "b" * 64, Path("/elsewhere/ab.bin"), False)
    assert capture.to_dict(root=Path("/run/.docket"))["response_path"] == "/elsewhere/ab.bin"


def test_a_deep_directory_that_does_not_exist_yet_is_created(tmp_path: Path) -> None:
    """`.docket/responses/` may not exist on the first call of a run, or `.docket` either."""
    deep = tmp_path / "a" / "b" / "c" / "responses"
    capture = capture_response(b"body", retain=True, directory=deep)
    assert capture.retained is True
    assert capture.path is not None and capture.path.parent == deep


def test_a_cap_of_zero_keeps_nothing_and_says_so(tmp_path: Path) -> None:
    """Reachable only by a direct caller: the policy refuses a cap under 1.

    Handled rather than argued with, because the event must describe what happened. An empty file
    under the whole-body digest, with `stored_bytes: 0` and `truncated: true`, is a true
    description; a silent refusal to retain anything would not be.
    """
    capture = capture_response(b"a real body", retain=True, max_bytes=0, directory=responses_root(tmp_path))
    assert capture.retained is True
    assert capture.stored_bytes == 0
    assert capture.total_bytes == 11
    assert capture.truncated is True
    assert capture.sha256 == digest(b"a real body"), "the digest still covers the whole body"
    assert capture.stored_sha256 == digest(b"")


def test_a_cap_of_one_keeps_exactly_one_byte(tmp_path: Path) -> None:
    """The smallest cap the policy allows, and the one a `>` / `>=` slip would silently break.

    At a cap of 1 a `>` written as `>=` keeps nothing and still reports success, which is the kind
    of off-by-one that only shows up as "retention is on and the file is empty".
    """
    capture = capture_response(b"a real body", retain=True, max_bytes=1, directory=responses_root(tmp_path))
    assert capture.retained is True
    assert capture.stored_bytes == 1
    assert capture.path is not None and capture.path.read_bytes() == b"a"
    assert capture.truncated is True
    assert capture.stored_sha256 == digest(b"a")


def test_a_negative_cap_keeps_nothing_rather_than_raising(tmp_path: Path) -> None:
    capture = capture_response(b"a real body", retain=True, max_bytes=-5, directory=responses_root(tmp_path))
    assert capture.retained is True
    assert capture.stored_bytes == 0
    assert capture.sha256 == digest(b"a real body")


@pytest.mark.parametrize(
    "free,should_retain",
    [
        (16 * 1024 * 1024, False),
        (16 * 1024 * 1024 + 3, False),
        # `free - need == reserve` is the boundary and it retains: the check is `<`, not `<=`.
        (16 * 1024 * 1024 + 4, True),
        (16 * 1024 * 1024 + 5, True),
        (None, True),
    ],
)
def test_the_free_space_boundary_is_off_by_nothing(tmp_path: Path, free: int | None, should_retain: bool) -> None:
    """The reserve is a floor, not a target: `free - need < reserve` refuses, and `==` retains.

    Four bytes of body and a 16 MiB reserve, so `free == reserve + 3` is the last value that
    refuses and `free == reserve + 4` — which would leave exactly the reserve — is the first that
    retains. That is the boundary, and it is pinned from both sides because an off-by-one here is
    the difference between "the disk was protected" and "the disk was protected most of the time". `free is None` is the sandbox's answer when the filesystem cannot be asked, and retention
    proceeds — refusing to keep evidence because a stat call failed would be the wrong trade.
    """
    capture = capture_response(
        b"y" * 4,
        retain=True,
        directory=responses_root(tmp_path),
        disk_free=(lambda: free) if free is not None else None,
        min_free_bytes=16 * 1024 * 1024,
    )
    assert capture.retained is should_retain
    if not should_retain:
        assert capture.note is not None and "16" in capture.note


def test_a_disk_probe_that_cannot_answer_does_not_block_retention(tmp_path: Path) -> None:
    """`disk_free` returning None means "could not ask", not "nothing free"."""
    capture = capture_response(
        b"body",
        retain=True,
        directory=responses_root(tmp_path),
        disk_free=lambda: None,
        min_free_bytes=16 * 1024 * 1024,
    )
    assert capture.retained is True
    assert capture.note is None


# --- what the retention actually writes --------------------------------------------------------- #


def test_a_retained_body_is_written_under_a_name_that_is_its_own_digest(tmp_path: Path) -> None:
    body = b'{"content":"an answer from a provider Docket cannot read"}'
    capture = capture_response(body, retain=True, directory=responses_root(tmp_path))
    assert capture.path is not None
    assert capture.path.name == f"{digest(body)}.bin"
    assert capture.path.read_bytes() == body
    assert check_retained(capture.path, capture.stored_sha256 or "") == (
        True,
        f"sha256 {digest(body)[:16]}… ({len(body)} bytes)",
    )


def test_the_same_body_twice_is_one_file(tmp_path: Path) -> None:
    """Named by digest, so a repeated answer costs one file rather than one per call."""
    root = responses_root(tmp_path)
    first = capture_response(b"same", retain=True, directory=root)
    second = capture_response(b"same", retain=True, directory=root)
    assert first.path == second.path
    assert len(list(root.iterdir())) == 1


def test_the_default_cap_is_big_enough_for_a_completion_and_small_enough_to_be_boring() -> None:
    assert DEFAULT_MAX_RESPONSE_BYTES == 64 * 1024


def test_retention_asked_for_with_no_place_to_write_says_so(tmp_path: Path) -> None:
    capture = capture_response(b"body", retain=True, directory=None)
    assert capture.retained is False
    assert capture.note is not None and "no run directory" in capture.note


def test_a_full_disk_stops_the_write_and_says_why(tmp_path: Path) -> None:
    """Writing the last free block would take the supervisor's own ledger with it."""
    root = responses_root(tmp_path)
    capture = capture_response(
        b"y" * 4096,
        retain=True,
        directory=root,
        # Exactly the reserved headroom free, so a 4 KiB write would eat into it.
        disk_free=lambda: 16 * 1024 * 1024,
        min_free_bytes=16 * 1024 * 1024,
    )
    assert capture.retained is False
    assert capture.note is not None
    assert "free" in capture.note and "reserves" in capture.note
    assert not root.exists() or not list(root.iterdir())


def test_a_write_that_fails_is_disclosed_not_swallowed(tmp_path: Path, monkeypatch) -> None:
    """The failure this project exists to prevent is evidence that quietly went missing."""
    root = responses_root(tmp_path)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("builtins.open", refuse)
    capture = capture_response(b"body", retain=True, directory=root)
    assert capture.retained is False
    assert capture.note is not None and "No space left on device" in capture.note


def test_an_oserror_with_no_strerror_still_produces_a_readable_note(tmp_path: Path, monkeypatch) -> None:
    """`exc.strerror or exc` exists because an `OSError` built without one has `strerror is None`.

    A note of "None" would be worse than no note, so the fallback is the exception itself. Reached
    by an `OSError` raised with only a message, which is what a caller-side wrapper produces.
    """
    from docket.evidence import responses_root

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError("permission denied, no errno supplied")

    monkeypatch.setattr("builtins.open", refuse)
    capture = capture_response(b"body", retain=True, directory=responses_root(tmp_path))
    assert capture.retained is False
    assert capture.note is not None
    assert "no errno supplied" in capture.note
    assert "None" not in capture.note


def test_check_retained_handles_a_directory_where_a_file_should_be(tmp_path: Path) -> None:
    """An unreadable target is a stated failure, not a raised exception.

    The verifier runs this over whatever the caller handed it, and "the checker crashed" is not a
    verification result.
    """
    directory = tmp_path / "ab.bin"
    directory.mkdir()
    ok, detail = check_retained(directory, "a" * 64)
    assert ok is False
    assert "could not be read" in detail


def test_check_retained_reads_an_oserror_with_no_strerror(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "ab.bin"
    target.write_bytes(b"body")

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError("an ioctl failed somewhere")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    ok, detail = check_retained(target, "a" * 64)
    assert ok is False
    assert "ioctl failed somewhere" in detail
    assert "None" not in detail


def test_a_capture_whose_own_numbers_disagree_is_detectable() -> None:
    """A hand-written receipt can claim anything; this is how a claim is caught.

    ``stored_digest_is_consistent`` is what separates a number that was measured from one that was
    typed: a retained capture that reports no digest, or a truncation flag that contradicts the
    byte counts, is a capture that describes a write that did not happen.
    """
    assert stored_digest_is_consistent(ResponseCapture(0, "a" * 64, False)) is True
    # The disjunction matters: a capture that claims nothing was retained but *carries* a digest is
    # describing a write that did not happen, and `and -> or` makes the check accept it.
    assert stored_digest_is_consistent(ResponseCapture(10, "a" * 64, False, 4, "b" * 64, None, False)) is False
    # And one of the two alone is enough to be inconsistent: a digest with no length, or a length
    # with no digest, describes a write nobody can check. This is the case `and -> or` accepts.
    assert stored_digest_is_consistent(ResponseCapture(10, "a" * 64, False, None, "b" * 64, None, False)) is False
    assert stored_digest_is_consistent(ResponseCapture(10, "a" * 64, False, 4, None, None, False)) is False
    assert stored_digest_is_consistent(ResponseCapture(10, "a" * 64, True)) is False
    assert stored_digest_is_consistent(ResponseCapture(10, "a" * 64, True, 10, "b" * 64, None, False)) is True
    assert stored_digest_is_consistent(ResponseCapture(10, "a" * 64, True, 4, "b" * 64, None, False)) is False, (
        "a 4-byte store of a 10-byte body is a truncation, and must say so"
    )
    assert stored_digest_is_consistent(ResponseCapture(10, "a" * 64, True, 4, "b" * 64, None, True)) is True


# --- check_retained ----------------------------------------------------------------------------- #


def test_a_missing_retained_body_fails_with_a_stated_reason(tmp_path: Path) -> None:
    ok, detail = check_retained(tmp_path / "absent.bin", "a" * 64)
    assert ok is False
    assert detail == f"the retained response {tmp_path / 'absent.bin'} is missing"


def test_a_retained_body_that_is_not_the_bytes_recorded_fails(tmp_path: Path) -> None:
    target = tmp_path / "body.bin"
    target.write_bytes(b"what is on disk")
    ok, detail = check_retained(target, digest(b"what the boundary returned"))
    assert ok is False
    assert "is not the bytes the boundary returned" in detail


# --- the policy section ------------------------------------------------------------------------- #


def test_a_policy_that_says_nothing_about_retention_keeps_its_digest() -> None:
    """A digest that moves when a feature is added invalidates every document written before it.

    Receipts and signatures name the policy digest. An ``[evidence]`` section that appears in the
    serialised form by default would turn every receipt written by an earlier version into one
    nobody can re-derive, so the section is only present when it is on.
    """
    assert "evidence" not in Policy().to_dict()
    assert Policy().evidence.record_responses is False


def test_the_section_appears_when_retention_is_on() -> None:
    policy = Policy.from_dict({"evidence": {"record_responses": True}})
    assert policy.to_dict()["evidence"]["record_responses"] is True


def test_a_size_on_its_own_means_retention_is_on() -> None:
    """A cap with retention off configures a retention that never happens, which nobody means."""
    assert Policy.from_dict({"evidence": {"record_response_max_bytes": 2048}}).evidence.record_responses is True


def test_an_explicit_off_wins_beside_a_size() -> None:
    policy = Policy.from_dict({"evidence": {"record_responses": False, "record_response_max_bytes": 2048}})
    assert policy.evidence.record_responses is False
    assert "evidence" not in policy.to_dict()


@pytest.mark.parametrize("cap", [0, -1, "big", 1.5, True])
def test_a_cap_that_cannot_mean_anything_is_refused_at_load_time(cap: Any) -> None:
    """The same rule as every other policy key: a bad value must never become a run that kept nothing."""
    with pytest.raises(ValueError, match="evidence.record_response_max_bytes"):
        Policy.from_dict({"evidence": {"record_response_max_bytes": cap}})


# --- the boundary, against a provider it cannot read -------------------------------------------- #


def _unreadable_upstream(tmp_path: Path):
    """A fake upstream whose usage block is in none of the three shapes Docket reads.

    Anthropic sends ``input_tokens``, OpenAI sends ``prompt_tokens``, Gemini sends
    ``promptTokenCount``. ``tokens_in``/``tokens_out`` is a fourth provider, and before retention
    existed the response that carried it was estimated and discarded.
    """
    from docket.fakeupstream import FakeUpstream, Step

    return FakeUpstream(
        steps=[
            Step(
                model="mystery-model-9",
                body_override={
                    "model": "mystery-model-9",
                    "content": "an answer in a shape nobody taught Docket",
                    "usage": {"tokens_in": 4242, "tokens_out": 137},
                },
            )
        ]
    )


def _run_with_retention(tmp_path: Path, *, retain: bool) -> Any:
    from docket.policy import Policy
    from docket.supervisor import Supervisor

    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, urllib.request\n"
        "base = os.environ['OPENAI_BASE_URL']\n"
        "body = json.dumps({'model': 'mystery-model-9', 'max_tokens': 16,"
        " 'messages': [{'role': 'user', 'content': 'hi'}]}).encode()\n"
        "r = urllib.request.Request(base + '/chat/completions', data=body,"
        " headers={'content-type': 'application/json'}, method='POST')\n"
        "urllib.request.urlopen(r, timeout=30).read()\n",
        encoding="utf-8",
    )
    document: dict[str, Any] = {"evidence": {}}
    if retain:
        document["evidence"]["record_responses"] = True
    upstream = _unreadable_upstream(tmp_path)
    upstream.start()
    try:
        policy = Policy.from_dict(document)
        supervisor = Supervisor(
            [sys.executable, str(agent)],
            policy=policy,
            docket_dir=tmp_path / ".docket",
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        return supervisor.run()
    finally:
        upstream.stop()


def _events(result: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(result.ledger_path).read_text().splitlines() if line.strip()]


def _response_event(result: Any) -> dict[str, Any]:
    responses = [e for e in _events(result) if e["type"] == "model.response"]
    assert len(responses) == 1, f"expected exactly one model.response event, got {len(responses)}"
    return responses[0]


def test_an_unreadable_provider_is_still_recorded_and_now_also_retained(tmp_path: Path) -> None:
    """The gap itself, closed: the estimate and the bytes it came from are both in the evidence.

    ``tokens_in``/``tokens_out`` is a shape Docket cannot read, so the token counts on this event
    are an estimate. What changed is that the body those tokens were estimated from is now on disk
    and digest-pinned, so the estimate can be checked by a reader with a parser Docket does not have.
    """
    result = _run_with_retention(tmp_path, retain=True)
    event = _response_event(result)
    assert event["data"]["usage_source"] == "estimated", "the provider really is unreadable"
    assert event["data"]["response_retained"] is True
    assert event["data"]["response_sha256"]
    assert event["data"]["response_stored_sha256"]
    assert event["data"]["response_truncated"] is False
    # Relative to the run directory, so the receipt travels and `--redact paths` can replace it.
    assert not Path(event["data"]["response_path"]).is_absolute()
    retained = (tmp_path / ".docket") / event["data"]["response_path"]
    assert retained.read_bytes().decode() == json.dumps(
        {
            "model": "mystery-model-9",
            "content": "an answer in a shape nobody taught Docket",
            "usage": {"tokens_in": 4242, "tokens_out": 137},
        },
        ensure_ascii=False,
    )


def test_the_same_run_without_retention_records_the_digest_and_keeps_nothing(tmp_path: Path) -> None:
    result = _run_with_retention(tmp_path, retain=False)
    event = _response_event(result)
    assert event["data"]["response_retained"] is False
    assert event["data"]["response_sha256"], "the digest is the part that is never optional"
    assert "response_path" not in event["data"]
    assert "response_stored_sha256" not in event["data"]
    assert not (tmp_path / ".docket" / "responses").exists()


# --- does anyone actually check it? -------------------------------------------------------------- #


def test_a_verifier_that_never_looks_at_a_retained_body_is_caught(tmp_path: Path) -> None:
    """The mutation this whole feature rests on.

    Delete the ``retained-responses`` check from ``verify.py`` and the retained body becomes
    decoration: a file on disk, named by the event, that nothing re-hashes. A body swapped for a
    friendlier one after the run would then verify clean.
    """
    import inspect

    from docket import verify

    source = inspect.getsource(verify)
    assert "retained-responses" in source, (
        "verify.py must re-hash every retained response body; without that check the retention "
        "is evidence nothing checks"
    )
    assert "check_retained" in source


def test_a_tampered_retained_body_fails_verification(tmp_path: Path) -> None:
    from docket.verify import verify_receipt

    result = _run_with_retention(tmp_path, retain=True)
    retained = (tmp_path / ".docket") / _response_event(result)["data"]["response_path"]
    receipt = json.loads(Path(result.receipt_path).read_text())
    clean = verify_receipt(
        receipt, ledger_events=list(_read_ledger(result.ledger_path)), ledger_path=result.ledger_path
    )
    assert clean.ok is True
    assert any(c.name == "retained-responses" and c.status == "pass" for c in clean.checks)

    retained.write_text('{"tampered": true}')
    tampered = verify_receipt(
        receipt, ledger_events=list(_read_ledger(result.ledger_path)), ledger_path=result.ledger_path
    )
    assert tampered.ok is False
    failure = [c for c in tampered.checks if c.name == "retained-responses"]
    assert failure and failure[0].status == "fail"
    assert "is not the bytes the boundary returned" in failure[0].detail


def test_a_deleted_retained_body_fails_verification(tmp_path: Path) -> None:
    from docket.verify import verify_receipt

    result = _run_with_retention(tmp_path, retain=True)
    event = _response_event(result)
    ((tmp_path / ".docket") / event["data"]["response_path"]).unlink()
    receipt = json.loads(Path(result.receipt_path).read_text())
    result_verify = verify_receipt(
        receipt, ledger_events=list(_read_ledger(result.ledger_path)), ledger_path=result.ledger_path
    )
    assert result_verify.ok is False
    failure = [c for c in result_verify.checks if c.name == "retained-responses"]
    assert failure and failure[0].status == "fail"
    assert "is missing" in failure[0].detail


def _read_ledger(path: Path) -> list[Any]:
    from docket.ledger import Ledger

    return list(Ledger.read(Path(path)))


# --- redaction ---------------------------------------------------------------------------------- #


def test_a_retained_path_is_redacted_by_name(tmp_path: Path) -> None:
    """The recorded path is relative to the run directory, so the path *shape* does not match it.

    It is still a fact about the repository the run happened in, so it is redacted by key name
    rather than by shape. Without that, ``--redact paths`` would leave a filesystem layout behind
    in a document meant for circulation.
    """
    from docket.redact import redact_value

    report: Any = None
    from docket.redact import RedactionReport

    report = RedactionReport()
    out = redact_value({"response_path": "responses/ab12.bin", "response_retained": True}, "", ["paths"], report)
    assert out["response_path"] != "responses/ab12.bin"
    assert report.replaced.get("paths", 0) >= 1


def test_a_redacted_receipt_still_verifies(tmp_path: Path) -> None:
    """The pair must re-chain, which is the promise ``--redact`` already makes.

    Adding a field to the response event must not break it: the redaction rewrites data and the
    chain is recomputed, so a redacted copy of a retained run is still a document that verifies.
    """
    from docket.redact import RedactionReport, redact_receipt, write_pair
    from docket.verify import verify_receipt

    result = _run_with_retention(tmp_path, retain=True)
    receipt = json.loads(Path(result.receipt_path).read_text())
    events = _read_ledger(result.ledger_path)
    report = RedactionReport()
    redacted, chained = redact_receipt(receipt, events, ["paths"], report)
    out_dir = tmp_path / "out"
    new_receipt, new_ledger = write_pair(
        redacted, chained, Path(result.receipt_path), Path(result.ledger_path), out_dir
    )
    verdict = verify_receipt(json.loads(new_receipt.read_text()), ledger_path=new_ledger)
    assert verdict.ok is True, [c.detail for c in verdict.checks if c.status == "fail"]
    body = new_receipt.read_text()
    assert "responses/" not in body


def test_a_retention_the_boundary_could_not_perform_is_disclosed_on_the_event(tmp_path: Path) -> None:
    """A run whose evidence quietly went missing is the failure this recorder exists to prevent.

    Retention is asked for, the write cannot happen, and the event has to say so. The digest is
    still there — it is computed before anything can fail — so the response stays identifiable even
    though it was not kept.

    The failure is reached by planting a regular *file* where the responses directory belongs. That
    is deterministic, needs no privileges, and is not hypothetical: a half-finished cleanup, a
    stale checkout, or a user who typed the wrong path produces exactly this. It is also the one
    case where ``mkdir(exist_ok=True)`` genuinely raises, because ``exist_ok`` only forgives an
    existing *directory*.
    """
    from docket.policy import Policy
    from docket.supervisor import Supervisor

    docket_dir = tmp_path / ".docket"
    docket_dir.mkdir()
    (docket_dir / "responses").write_text("this is where the directory should be\n", encoding="utf-8")

    agent = tmp_path / "agent.py"
    agent.write_text(
        "import json, os, urllib.request\n"
        "base = os.environ['OPENAI_BASE_URL']\n"
        "body = json.dumps({'model': 'mystery-model-9', 'max_tokens': 16,"
        " 'messages': [{'role': 'user', 'content': 'hi'}]}).encode()\n"
        "r = urllib.request.Request(base + '/chat/completions', data=body,"
        " headers={'content-type': 'application/json'}, method='POST')\n"
        "urllib.request.urlopen(r, timeout=30).read()\n",
        encoding="utf-8",
    )
    upstream = _unreadable_upstream(tmp_path)
    upstream.start()
    try:
        supervisor = Supervisor(
            [sys.executable, str(agent)],
            policy=Policy.from_dict({"evidence": {"record_responses": True}}),
            docket_dir=docket_dir,
            cwd=tmp_path,
            upstreams={"openai": upstream.url},
        )
        result = supervisor.run()
    finally:
        upstream.stop()

    event = _response_event(result)
    assert event["data"]["usage_source"] == "estimated", "the provider is still the unreadable one"
    assert event["data"]["response_retained"] is False
    assert event["data"]["response_sha256"], "the digest is computed before anything can fail"
    assert event["data"]["response_note"], "a retention that failed must say why on the event"
    assert "response_path" not in event["data"], "nothing was written, so nothing is named"
    # The planted file is untouched: evidence is never repaired by rewriting it.
    assert (docket_dir / "responses").read_text() == "this is where the directory should be\n"


def test_a_receipt_without_a_ledger_does_not_blame_the_verifier_for_a_missing_path(tmp_path: Path) -> None:
    from docket.verify import verify_receipt

    """No ledger means no run directory, and a path resolved against the wrong place is a path to nothing.

    Reporting that as a failure would blame the caller's own input, so the check is skipped with a
    reason instead.
    """
    result = _run_with_retention(tmp_path, retain=True)
    receipt = json.loads(Path(result.receipt_path).read_text())
    verdict = verify_receipt(receipt, ledger_events=_read_ledger(result.ledger_path), ledger_path=None)
    assert verdict.ok is True
    assert not any(c.name == "retained-responses" for c in verdict.checks)


if __name__ == "__main__":  # pragma: no cover - `python3 tests/test_evidence_retention.py`
    sys.exit(pytest.main([__file__, "-q"]))
