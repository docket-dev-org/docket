"""Receipts and verification, including the adversarial cases."""

from __future__ import annotations

import json
import pathlib
from decimal import Decimal

import pytest

from docket.ledger import Ledger
from docket.receipt import load_receipt, receipt_core, receipt_digest, write_receipt
from docket.report import diff_receipts, render_markdown
from docket.signing import load_or_create_seal_key
from docket.verify import verify_receipt

SPEC_VECTORS = pathlib.Path(__file__).resolve().parents[1] / "spec" / "vectors"


def test_receipt_is_complete(make_receipt):
    receipt, path, ledger_path = make_receipt()
    for key in (
        "format",
        "docket_number",
        "session",
        "policy",
        "totals",
        "model_calls",
        "files",
        "ledger",
        "price_table",
        "appendix",
        "findings",
        "limits_disclosure",
    ):
        assert key in receipt, key
    assert receipt["totals"]["cost_usd"] == "0.018000"
    assert receipt["totals"]["calls"] == 3
    assert len(receipt["appendix"]["usage"]) == 3
    assert receipt["appendix"]["proof_included"] is True


def test_receipt_alone_verifies_without_the_ledger(make_receipt):
    receipt, path, _ = make_receipt()
    result = verify_receipt(load_receipt(path))
    assert result.ok, result.failures
    statuses = {c.name: c.status for c in result.checks}
    assert statuses["ledger-chain"] == "skip"
    assert statuses["cost-recomputation"] == "pass"
    assert statuses["usage-inclusion-proofs"] == "pass"


def test_no_check_reports_two_verdicts(make_receipt):
    """One check name, one verdict.

    The verifier used to report the Merkle-path verdict and the ledger re-binding verdict under
    the same name, so a doctored ledger produced `FAIL usage-inclusion-proofs` and
    `PASS usage-inclusion-proofs` a few lines apart — and a reader skimming sees whichever one
    they expected. Two facts, two names; this test is what keeps them apart, including for
    receipts where one of the two is clean and the other is not.
    """
    receipt, path, ledger_path = make_receipt()
    clean = verify_receipt(load_receipt(path), ledger_path=ledger_path)
    assert clean.ok, clean.failures
    lines = [line for line in pathlib.Path(ledger_path).read_text(encoding="utf-8").splitlines() if line.strip()]
    # Forge a *response*: that is the event the appendix rows are bound to, so the two facts
    # come apart — the proofs still verify against the receipt's claimed root, and the rows no
    # longer match the ledger as it now reads.
    target = next(index for index, line in enumerate(lines) if json.loads(line).get("type") == "model.response")
    payload = json.loads(lines[target])
    payload["data"]["model"] = "claude-haiku-forged"
    lines[target] = json.dumps(payload)
    pathlib.Path(ledger_path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    edited = verify_receipt(load_receipt(path), ledger_path=ledger_path)
    assert not edited.ok
    for result in (clean, edited):
        by_name: dict[str, set[str]] = {}
        for check in result.checks:
            by_name.setdefault(check.name, set()).add(check.status)
        conflicting = {name: statuses for name, statuses in by_name.items() if len(statuses) > 1}
        assert not conflicting, f"{conflicting} reports more than one verdict"
    # And the two facts are separately readable: the appendix is untouched, the ledger is not.
    statuses = {check.name: check.status for check in edited.checks}
    assert statuses["usage-inclusion-proofs"] == "pass"
    assert statuses["usage-rows-rebound"] == "fail"
    detail = next(
        check.detail for check in edited.checks if check.name == "usage-rows-rebound" and check.status == "fail"
    )
    assert "does not match the recomputed ledger event" in detail


def test_receipt_with_ledger_verifies_fully(make_receipt):
    receipt, path, ledger_path = make_receipt()
    result = verify_receipt(load_receipt(path), ledger_path=ledger_path)
    assert result.ok, result.failures
    assert {c.status for c in result.checks} <= {"pass", "skip"}
    assert all(
        c.status != "skip" for c in result.checks if c.name in {"ledger-chain", "merkle-root", "ledger-file-digest"}
    )


def test_verification_fails_when_the_ledger_is_edited(make_receipt):
    receipt, path, ledger_path = make_receipt()
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[1])
    payload["data"]["calls"] = 999
    lines[1] = json.dumps(payload)
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = verify_receipt(load_receipt(path), ledger_path=ledger_path)
    assert not result.ok
    assert any("ledger-chain" in f for f in result.failures)


def test_verification_fails_when_a_receipt_field_is_faked(make_receipt):
    receipt, path, _ = make_receipt()
    receipt["totals"]["cost_usd"] = "0.00"
    receipt["appendix"]["usage"] = [{**row, "cost_usd": "0.00"} for row in receipt["appendix"]["usage"]]
    result = verify_receipt(receipt)
    assert not result.ok
    assert any("totals" in f for f in result.failures)


def test_verification_fails_when_a_usage_row_is_faked(make_receipt):
    receipt, path, _ = make_receipt()
    receipt["appendix"]["usage"][0]["output_tokens"] = 4242
    result = verify_receipt(receipt)
    assert not result.ok
    assert any("inclusion" in f or "recomputation" in f for f in result.failures)


def test_verification_fails_without_an_embedded_price_table(make_receipt):
    receipt, path, _ = make_receipt()
    del receipt["price_table"]
    result = verify_receipt(receipt)
    assert not result.ok
    assert any("price table" in f for f in result.failures)


def test_missing_disclosure_is_a_failure(make_receipt):
    receipt, path, _ = make_receipt()
    receipt["limits_disclosure"] = {}
    result = verify_receipt(receipt)
    assert not result.ok
    assert any("disclosure" in f for f in result.failures)


def test_sealed_receipt_verifies_only_with_the_key(tmp_path, make_receipt):
    key = load_or_create_seal_key(tmp_path / "key")
    receipt, _, _ = make_receipt()
    from docket.receipt import build_receipt  # noqa: F401  (documentation of import path)
    from docket.signing import seal

    receipt["seal"] = seal(receipt_core(receipt), key)
    assert verify_receipt(receipt, seal_key=key).ok
    wrong = verify_receipt(receipt, seal_key="00" * 32)
    assert not wrong.ok
    assert any("seal" in f for f in wrong.failures)


def test_seal_detects_edit_after_sealing(tmp_path, make_receipt):
    key = load_or_create_seal_key(tmp_path / "key")
    from docket.signing import seal

    receipt, _, _ = make_receipt()
    receipt["seal"] = seal(receipt_core(receipt), key)
    receipt["session"]["exit_code"] = 7
    assert not verify_receipt(receipt, seal_key=key).ok


def test_seal_key_file_is_created_once(tmp_path):
    path = tmp_path / "seal.key"
    first = load_or_create_seal_key(path)
    second = load_or_create_seal_key(path)
    assert first == second
    assert len(first) == 64


def test_receipt_digest_ignores_its_own_seal(tmp_path, make_receipt):
    key = load_or_create_seal_key(tmp_path / "key")
    from docket.signing import seal

    receipt, _, _ = make_receipt()
    before = receipt_digest(receipt)
    receipt["seal"] = seal(receipt_core(receipt), key)
    assert receipt_digest(receipt) == before


def test_receipt_round_trips_through_disk(make_receipt, tmp_path):
    receipt, path, _ = make_receipt()
    again = load_receipt(path)
    assert again == receipt
    target = tmp_path / "copy.receipt.json"
    write_receipt(again, target)
    assert load_receipt(target) == receipt


def test_markdown_report_mentions_the_essentials(make_receipt):
    receipt, _, _ = make_receipt()
    markdown = render_markdown(receipt)
    assert receipt["docket_number"] in markdown
    assert "estimated cost" in markdown
    assert "docket verify" in markdown


def test_verify_renders_a_verdict_line(make_receipt):
    receipt, path, _ = make_receipt()
    rendered = verify_receipt(load_receipt(path)).render(color=False)
    assert rendered.splitlines()[0].startswith("VERIFIED")


def test_diff_between_receipts_flags_behaviour_change(make_receipt):
    a, _, _ = make_receipt()
    b = json.loads(json.dumps(a))
    b["docket_number"] = "DKT-20260101-FFFFFF"
    b["totals"]["calls"] = 9
    b["totals"]["by_model"][0]["model"] = "gpt-6-luna"
    difference = diff_receipts(a, b)
    assert difference["changes"]["calls"] == (3, 9)
    assert "gpt-6-luna" in difference["changes"]["models_added"][1]
    assert difference["verdict"] == "behaviour changed"


def test_report_render_includes_proof_and_limits(make_receipt):
    from docket.report import render_receipt

    receipt, _, _ = make_receipt()
    text = render_receipt(receipt, color=False)
    assert "WHAT THIS RECEIPT PROVES" in text
    assert "does not observe" in text
    assert "VERIFY IT YOURSELF" in text


def test_canonical_head_is_recorded_in_the_receipt(make_receipt):
    receipt, _, ledger_path = make_receipt()
    events = Ledger.read(ledger_path)
    assert receipt["ledger"]["head"] == events[-1].hash
    assert receipt["ledger"]["events"] == len(events)


@pytest.mark.parametrize("junk", [[], ["docket/receipt/v1"], "a receipt", 42, None, True])
def test_a_document_that_is_not_a_receipt_is_a_verdict_not_a_crash(junk):
    """`verify_receipt` is total: the caller chooses the file to hand over.

    The contract has two halves, and only the crash-free half was tested. The other half is that
    the *verdict* is `ok=False` — a verifier that returns "not a receipt" in a check list but
    `ok=True` in the field a caller branches on is a verifier that says yes to everything it
    cannot read. (Found by `tools/mutate.py`: replacing `ok=False` with `ok=True` on this path
    changed no test's outcome.)
    """
    result = verify_receipt(junk)
    assert result.ok is False, f"a {type(junk).__name__} is not a receipt and must not verify"
    assert result.docket_number is None
    assert [check.name for check in result.checks] == ["receipt-format"]
    assert result.checks[0].status == "fail"
    assert "not a receipt" in result.checks[0].detail
    assert type(junk).__name__ in result.checks[0].detail
    assert result.failures, "a refusal has to be printable, not an empty list"


@pytest.mark.parametrize(
    ("ops", "expected"),
    [
        ({"chaos_run": True, "fault_injection": {"injections": 3}}, "3 injected fault(s)"),
        ({"chaos_run": True, "fault_injection": {}}, "0 injected fault(s)"),
        ({"chaos_run": True, "fault_injection": "many"}, "0 injected fault(s)"),
        ({"chaos_run": True}, "0 injected fault(s)"),
    ],
)
def test_a_chaos_receipt_reports_the_injection_count_it_actually_states(make_receipt, ops, expected):
    """`ops.chaos_run` with no countable numbers under it must not inflate the skip message.

    Receipts come from other machines, so the block is a document rather than an invariant: it may
    be missing, or be a string, or name no count. Each of those is a value Docket cannot count,
    and the honest rendering is zero. The check itself is a `skip` — this number is the only thing
    it says, and it is what tells a reader whether the receipt records one injected fault or
    twenty. Found by `tools/mutate.py`, which showed `0` → `1` on both defaults changing no test.
    """
    receipt, _path, ledger_path = make_receipt()
    receipt["ops"] = ops
    check = next(item for item in verify_receipt(receipt, ledger_path=ledger_path).checks if item.name == "chaos-run")
    assert check.status == "skip"
    assert expected in check.detail, check.detail
    # The skip says what the document *is*, not that it is fine: nothing here claims the run was
    # production behaviour, which is the entire reason the check exists.
    assert "test, not production" in check.detail


def test_the_usage_row_counts_in_the_failure_message_are_exact(make_receipt):
    """The numbers *are* the message: they say what to fix and how much of it there is.

    A verifier that fails with "0 of 10 usage rows are not proven" is worse than useless — it
    tells a reader the opposite of the truth about the very rows that made it fail. Each counter
    in this block was checked individually by `tools/mutate.py`, which found that no test noticed
    any of them changing; this test fixes the arithmetic, the report cap and the summary line
    together, because they are one sentence.

    The document below contains one of each defect a receipt can carry in its usage appendix:
    a proven row, rows with a malformed proof, rows with no proof, a row whose proof is
    well-formed but belongs to a different event, a row citing a sequence number the ledger does
    not have, and a row that is not even an object.
    """
    receipt, _path, ledger_path = make_receipt()
    rows = receipt["appendix"]["usage"]
    assert len(rows) == 3, "the fixture is the baseline; this test tampers with a copy"

    tampered = json.loads(json.dumps(receipt))
    poisoned: list[object] = [dict(rows[0])]
    poisoned += [{**rows[0], "merkle_path": [{"side": "up", "hash": "zz"}]} for _ in range(4)]
    poisoned += [{key: value for key, value in rows[0].items() if key != "merkle_path"} for _ in range(2)]
    poisoned.append({**rows[0], "event_hash": "0" * 64})  # a proof for a different leaf
    poisoned.append({**rows[0], "seq": 9999})  # a sequence number the ledger never had
    poisoned.append("nope")  # not an object
    tampered["appendix"]["usage"] = poisoned

    result = verify_receipt(tampered, ledger_path=ledger_path)
    assert result.ok is False
    proofs = [check for check in result.checks if check.name == "usage-inclusion-proofs"]
    rebound = [check for check in result.checks if check.name == "usage-rows-rebound"]

    # The summary line: 10 rows, 2 proven, 2 without proofs, 6 failing theirs.
    summary = next(check for check in proofs if "of 10 usage rows" in check.detail)
    assert summary.detail == (
        "8 of 10 usage rows are not proven (2 proven, 2 without proofs, 6 failing their proof)"
    ), summary.detail
    # ... and the re-binding line, which counts the rows the *ledger* disagrees with.
    assert rebound[-1].detail == (
        "2 of 10 usage rows are not the events the recomputed ledger has at those sequence numbers " "(7 re-bound)"
    ), rebound[-1].detail

    # The per-row reports are capped per check, so one kind of defect cannot spend the whole
    # budget: three named rows, then one line for the rest — never ten copies of one sentence.
    malformed = [check for check in proofs if "malformed inclusion proof" in check.detail]
    assert len(malformed) == 3, [check.detail for check in malformed]
    assert any("further usage rows fail usage-inclusion-proofs the same way" in check.detail for check in proofs)
    # The two named re-binding failures are named, with the sequence numbers that failed.
    assert [check.detail for check in rebound[:-1]] == [
        "usage row 7 (seq 3) does not match the recomputed ledger event",
        "usage row 8 cites seq 9999, outside the ledger",
    ]
    # A non-object row is reported as such rather than crashing the count.
    assert not any("usage row 9" in check.detail for check in proofs), "a non-object row is counted, not described"


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (True, "True"),
        (False, "False"),
        (None, "None"),
        (1.5, "1.5"),
        (float("nan"), "nan"),
        ("", "''"),
        ("abc", "'abc'"),
        ([], "list"),
        ({}, "dict"),
    ],
)
def test_an_amount_that_is_not_an_amount_is_refused_not_coerced(value, label):
    """`Decimal(True)` is 1 and `Decimal('nan')` is a trap — the guard exists for that reason.

    It reads `isinstance(value, bool) or value is None`, and `tools/mutate.py` showed that
    flipping `or` to `and` changed no test outcome: nothing in the suite handed an amount parser
    a boolean. The failure mode that would then be live is exactly the one the docstring names —
    a `True` in the totals block silently verifying as one dollar.
    """
    from docket.verify import _amount

    with pytest.raises(ValueError):
        _amount(value)


@pytest.mark.parametrize("value", [0, 1, -3, "0", "1.50", " 12.5 ", Decimal("0.25"), Decimal("0")])
def test_the_amount_parser_accepts_the_shapes_a_receipt_actually_uses(value):
    from docket.verify import _amount

    parsed = _amount(value)
    assert isinstance(parsed, Decimal) and parsed.is_finite()


@pytest.mark.parametrize("value", ["nan", "Infinity", "-Infinity", Decimal("NaN"), Decimal("Infinity")])
def test_a_non_finite_amount_is_refused_whatever_type_carries_it(value):
    """A `NaN` total compares false against everything, so it would be reported as a *mismatch*
    — "the receipt says NaN, recomputation says 0.005" — rather than as the junk it is. Refusing
    it by name keeps the failure legible, and `Infinity` has no JSON spelling at all (spec §3.2).
    """
    from docket.verify import _amount

    with pytest.raises(ValueError, match="finite"):
        _amount(value)


def test_a_hostile_totals_string_produces_a_verdict_naming_what_it_could_not_read(make_receipt):
    """The one call site of `_amount` is inside the verifier, whose contract is totality.

    Before this was tightened, an unparsable string left `decimal.InvalidOperation` to escape
    `_amount`; the call site caught it, so no user-visible bug existed — but the *report* is what
    matters here, and it has to name the value it refused rather than the type of exception it
    happened to raise.
    """
    receipt, _path, ledger_path = make_receipt()
    for hostile in ["abc", "1e", "0x10", "1_000", "not money"]:
        receipt["totals"]["cost_usd"] = hostile
        result = verify_receipt(receipt, ledger_path=ledger_path)
        assert result.ok is False
        totals = [check for check in result.checks if check.name == "totals"]
        assert any(hostile in check.detail for check in totals), [check.detail for check in totals]


@pytest.mark.parametrize("member", ["ledger", "totals", "price_table"])
@pytest.mark.parametrize("shape", [None, [], "", 0, True, "an object"])
def test_a_required_member_of_the_wrong_shape_is_a_named_failure_not_a_crash(make_receipt, member, shape):
    """Every member of a receipt is written by nobody the verifier trusts.

    The three required members are read as *objects*: `section()` exists so that a receipt whose
    `price_table` is a string produces "the receipt's price_table is not an object: str" instead
    of an `AttributeError` out of the first `.get()`. `tools/mutate.py` found that the fallback
    that makes that true could be replaced with `return None` without failing a test — so the
    guard was written but never exercised, which is the same as not having it.
    """
    receipt, _path, ledger_path = make_receipt()
    if shape is None:
        receipt.pop(member, None)
    else:
        receipt[member] = shape
    result = verify_receipt(receipt, ledger_path=ledger_path)

    assert result.ok is False, f"{member}={shape!r} must not verify"
    shapes = [check for check in result.checks if check.name == "receipt-shape"]
    assert shapes, f"no receipt-shape failure for {member}={shape!r}: {[c.name for c in result.checks]}"
    if shape is None:
        assert any(f"no {member!r} member" in check.detail for check in shapes), [c.detail for c in shapes]
    else:
        assert any(f"{member} is not an object" in check.detail for check in shapes), [c.detail for c in shapes]
    # And the failure is a *verdict*: the checks that depend on the member still ran and said
    # something, rather than the whole verification ending at the first surprise.
    assert len(result.checks) >= 5


def test_a_result_serialises_to_the_shape_the_cli_and_ci_promises(make_receipt):
    """`to_dict` is what `docket verify --json` emits and what a CI job branches on.

    `tools/mutate.py` replaced its body with `return None` and nothing noticed, so no test
    asserted the machine-readable form of a verification.
    """
    receipt, _path, ledger_path = make_receipt()
    good = verify_receipt(receipt, ledger_path=ledger_path).to_dict()
    assert set(good) >= {"ok", "docket_number", "checks", "failures"}
    assert good["ok"] is True and good["failures"] == []
    assert all({"name", "status", "detail"} <= set(check) for check in good["checks"])

    receipt["totals"]["cost_usd"] = "9.99"
    bad = verify_receipt(receipt, ledger_path=ledger_path).to_dict()
    assert bad["ok"] is False
    assert bad["failures"] and all(isinstance(item, str) for item in bad["failures"])
    # The whole document survives a round trip through JSON: that is what makes it useful to a job.
    assert json.loads(json.dumps(bad)) == bad


def test_the_rendered_verdict_marks_colour_by_default_and_can_be_turned_off(make_receipt):
    """`render()` defaults to colour because it is written for a terminal.

    A mutant that flipped the default produced plain text and no test disagreed, which would be
    invisible until someone noticed the CLI had gone monochrome. Both halves are pinned: colour
    on by default, and a `NO_COLOR`-style call that emits no escape sequences at all.
    """
    receipt, _path, ledger_path = make_receipt()
    result = verify_receipt(receipt, ledger_path=ledger_path)
    coloured = result.render()
    assert "\033[" in coloured and "VERIFIED" in coloured
    plain = result.render(color=False)
    assert "\033[" not in plain and "VERIFIED" in plain


def test_the_rendered_verdict_names_the_docket_number(make_receipt):
    """The verdict line is `VERIFIED  DKT-…` — the number is how a human finds the file again."""
    receipt, _path, ledger_path = make_receipt()
    rendered = verify_receipt(receipt, ledger_path=ledger_path).render(color=False)
    assert rendered.splitlines()[0].startswith("VERIFIED")
    assert "DKT-20260101-ABC123" in rendered.splitlines()[0]


def test_an_empty_usage_appendix_says_there_were_no_calls(make_receipt):
    """A session that made no metered calls is a *pass* with a reason, not a silence.

    `tools/mutate.py` flipped the branch that decides between "nothing to recompute" and "there
    were no calls" and nothing noticed, because no test had a receipt with an empty appendix.
    """
    receipt, _path, ledger_path = make_receipt()
    receipt["appendix"]["usage"] = []
    result = verify_receipt(receipt, ledger_path=ledger_path)
    checks = {check.name: check for check in result.checks}
    assert checks["usage-inclusion-proofs"].status == "pass"
    assert "no metered calls" in checks["usage-inclusion-proofs"].detail
    # With nothing to re-bind, nothing is claimed about re-binding.
    assert "usage-rows-rebound" not in checks


def test_a_receipt_that_omits_the_token_total_cannot_pass_by_default(make_receipt):
    """The default for a missing `totals.total_tokens` is `-1`, and it is load-bearing.

    With `0` — the obvious "harmless" default — a receipt that never stated a token total would
    compare equal to a recomputation of zero calls and *pass*, which is the worst possible
    outcome: the field is absent and the verifier says the totals agree. `-1` is impossible as a
    token count, so the comparison fails and the failure is legible.
    """
    receipt, _path, ledger_path = make_receipt()
    receipt["totals"].pop("total_tokens", None)
    receipt["appendix"]["usage"] = []
    result = verify_receipt(receipt, ledger_path=ledger_path)
    token_check = next(check for check in result.checks if check.name == "token-totals")
    assert token_check.status == "fail", token_check.detail
    assert result.ok is False


def test_an_incomplete_ledger_is_reported_with_the_counts_it_states(make_receipt):
    """`evidence.ledger_complete: false` is the receipt saying "not every event reached disk".

    Two shapes are checked: a receipt that states how many events were lost, and one that only
    says the ledger is incomplete. The second must not invent a count — `0 event(s) never reached
    disk, first at seq None` is the honest rendering of a document that does not say.
    """
    receipt, _path, ledger_path = make_receipt()
    receipt["evidence"] = {"ledger_complete": False}
    bare = next(
        check
        for check in verify_receipt(receipt, ledger_path=ledger_path).checks
        if check.name == "evidence-completeness"
    )
    assert bare.status == "fail"
    assert bare.detail == "the ledger is incomplete: 0 event(s) never reached disk, first at seq None"

    receipt["evidence"] = {"ledger_complete": False, "unwritten_events": 4, "first_failing_seq": 9}
    stated = next(
        check
        for check in verify_receipt(receipt, ledger_path=ledger_path).checks
        if check.name == "evidence-completeness"
    )
    assert stated.status == "fail"
    assert stated.detail == "the ledger is incomplete: 4 event(s) never reached disk, first at seq 9"


@pytest.mark.parametrize("evidence", ["yes", [], 0, True])
def test_a_ledger_evidence_member_of_the_wrong_shape_is_a_named_failure(make_receipt, evidence):
    receipt, _path, ledger_path = make_receipt()
    receipt["evidence"] = evidence
    check = next(
        check
        for check in verify_receipt(receipt, ledger_path=ledger_path).checks
        if check.name == "evidence-completeness"
    )
    assert check.status == "fail"
    assert "is not an object" in check.detail


@pytest.mark.parametrize(
    ("signature", "status", "fragment"),
    [
        (None, "skip", "not signed"),
        ("abc", "fail", "signature is not an object: str"),
        ([], "fail", "signature is not an object: list"),
        (0, "fail", "signature is not an object: int"),
        (False, "fail", "signature is not an object: bool"),
        ({}, "fail", "empty signature object"),
    ],
)
def test_a_signature_member_is_read_by_presence_not_by_truthiness(make_receipt, signature, status, fragment):
    """`"signature": 0` is a malformed member, not an unsigned receipt.

    The difference matters in the direction of leniency: reporting "not signed" for a receipt
    that carries a junk signature member would let a doctored document pass as merely unsigned.
    The verifier says so in its own comment and, until this test, nothing checked it —
    `tools/mutate.py` flipped the shape branches and the suite stayed green.
    """
    receipt, _path, ledger_path = make_receipt()
    receipt["signature"] = signature
    check = next(
        check for check in verify_receipt(receipt, ledger_path=ledger_path).checks if check.name == "signature"
    )
    assert check.status == status, check.detail
    assert fragment in check.detail, check.detail


def test_without_a_ledger_nothing_claims_the_rows_were_re_bound(make_receipt):
    """Re-binding needs the ledger. Without one, the verifier may not mention it at all.

    A mutated `if events is not None and rows:` turned this into a claim that zero rows were
    re-bound — a pass with nothing behind it. The honest output says what was checked and what
    was not, so the header note about supplying a ledger is the only mention of the chain.
    """
    receipt, _path, _ledger_path = make_receipt()
    result = verify_receipt(receipt)
    names = [check.name for check in result.checks]
    assert "usage-rows-rebound" not in names, names
    proofs = next(check for check in result.checks if check.name == "usage-inclusion-proofs")
    assert "re-bound" not in proofs.detail, proofs.detail
    assert result.ok is True, result.failures  # a receipt alone can still verify


def test_a_usage_row_bound_to_the_first_ledger_event_re_binds(make_receipt):
    """The window is `0 <= seq < len(events)`, and the lower bound is a real ledger position.

    `seq: 0` is the first event of the session — not a sentinel, not "missing". A mutated bound
    of `1 <= seq` reported a correctly-bound first-event row as "cites seq 0, outside the
    ledger", which is the verifier inventing a failure about an honest record.
    """
    from docket.ledger import Ledger

    receipt, _path, ledger_path = make_receipt()
    events = Ledger.read(ledger_path)
    receipt["appendix"]["usage"].append(
        {
            "seq": 0,
            "event_hash": events[0].hash,
            "model": "claude-sonnet-5",
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_usd": "0.000001",
        }
    )
    check = next(
        check for check in verify_receipt(receipt, ledger_path=ledger_path).checks if check.name == "usage-rows-rebound"
    )
    assert check.status == "pass", check.detail
    assert check.detail == "4 usage rows re-bound to the recomputed ledger", check.detail


def test_a_proven_and_re_bound_receipt_says_both_in_the_pass_message(make_receipt):
    """The pass message is a claim, and it is made of two clauses: proven, and re-bound.

    `tools/mutate.py` showed that the second clause could be dropped (or inverted, so it appeared
    only when re-binding had *failed*) without a test noticing.
    """
    receipt, _path, ledger_path = make_receipt()
    detail = next(
        check
        for check in verify_receipt(receipt, ledger_path=ledger_path).checks
        if check.name == "usage-inclusion-proofs"
    ).detail
    assert (
        detail == "3 usage rows proven against the receipt's claimed Merkle root and re-bound to the recomputed ledger"
    ), detail


def test_a_broken_chain_is_a_skip_that_names_the_first_bad_event(make_receipt):
    """When the chain cannot be walked, the head comparison is *not checked* — with the reason.

    The skip names the sequence number where the walk stopped, because "not checked: the chain is
    broken at event #<something>" is the difference between a reader who can find the damage and
    one who cannot. `tools/mutate.py` changed which element of the diagnostic was interpolated
    and no test noticed.
    """
    receipt, _path, ledger_path = make_receipt()
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[1])
    edited["data"]["argv"] = ["tampered"]
    lines[1] = json.dumps(edited)
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = verify_receipt(receipt, ledger_path=ledger_path)
    head = next(check for check in result.checks if check.name == "head")
    assert head.status == "skip", head.detail
    assert "not checked" in head.detail
    assert "at event #1" in head.detail, head.detail
    assert result.ok is False


@pytest.mark.parametrize(
    ("appendix", "fragment"),
    [
        ("not an object", "appendix is not an object: str"),
        ([], "appendix is not an object: list"),
        ({"usage": {"seq": 1}}, "appendix.usage is not a list: dict"),
        ({"usage": "rows"}, "appendix.usage is not a list: str"),
    ],
)
def test_an_unreadable_usage_appendix_is_reported_instead_of_recomputed(make_receipt, appendix, fragment):
    """An appendix nobody can read is not an appendix with no calls in it.

    `tools/mutate.py` flipped the `appendix_ok = False` markers in both branches without a test
    noticing; with them flipped, a receipt whose `appendix.usage` is a string would be
    recomputed as if it had no rows — the failure would still happen, but the *reason* would be
    buried under a misleading "0 calls recomputed".
    """
    receipt, _path, ledger_path = make_receipt()
    receipt["appendix"] = appendix
    result = verify_receipt(receipt, ledger_path=ledger_path)
    proofs = [check for check in result.checks if check.name == "usage-inclusion-proofs"]
    assert any(fragment in check.detail for check in proofs), [check.detail for check in proofs]
    recomputation = next(check for check in result.checks if check.name == "cost-recomputation")
    assert recomputation.status == "fail"
    assert "not readable" in recomputation.detail, recomputation.detail
    # And nothing claims there were no calls: an unreadable appendix is not an empty one. The
    # failure above is the only thing said about those rows.
    assert not [
        check for check in result.checks if check.name == "every-usage-row-recomputed"
    ], "an unreadable appendix must not be reported as a session with no metered calls"


def test_one_kind_of_bad_usage_row_cannot_flood_the_report(make_receipt):
    """Six rows failing the same way produce three named rows and **one** summary line.

    The cap is per check, so a single defect repeated fifty times cannot bury every other
    finding. `seen == 3` is what makes the summary appear exactly once; `!=` would emit a
    "further rows" line per row, which is the flood the cap exists to prevent.
    """
    receipt, _path, ledger_path = make_receipt()
    template = receipt["appendix"]["usage"][0]
    receipt["appendix"]["usage"] = [{**template, "merkle_path": [{"side": "up", "hash": "zz"}]} for _ in range(6)]
    proofs = [
        check
        for check in verify_receipt(receipt, ledger_path=ledger_path).checks
        if check.name == "usage-inclusion-proofs"
    ]
    named = [check for check in proofs if "malformed inclusion proof" in check.detail]
    further = [check for check in proofs if "further usage rows" in check.detail]
    assert len(named) == 3, [check.detail for check in named]
    assert len(further) == 1, [check.detail for check in further]
    assert "6 of 6 usage rows are not proven" in proofs[-1].detail, proofs[-1].detail


def test_a_price_assumption_is_named_or_the_receipt_fails():
    """`assumed-prices` is the id a reader would reach for to ask "was anything guessed?".

    It existed with nothing asserting it: a sweep that turned `if not assumed:` into `if assumed:`
    left the whole suite green, and that mutant says "every model matched a price entry" precisely
    when the receipt was priced by an assumption. Both directions are pinned here — the receipt
    that names its assumption passes and names it, and the one that drops the disclosure fails.
    """
    vector = json.loads((SPEC_VECTORS / "receipt.json").read_text(encoding="utf-8"))

    disclosed = verify_receipt(json.loads(json.dumps(vector)))
    check = next(c for c in disclosed.checks if c.name == "assumed-prices")
    assert check.status == "pass", check.detail
    assert "unlisted-model-1" in check.detail, "the pass must name the model that was assumed"

    silent = json.loads(json.dumps(vector))
    silent["totals"].pop("assumed_price_models")
    result = verify_receipt(silent)
    named = next(c for c in result.checks if c.name == "assumed-prices")
    assert named.status == "fail", named.detail
    assert "unlisted-model-1" in named.detail
    assert result.ok is False


def test_an_omitted_total_is_absent_not_zero():
    """A member that is not there is not a member that said zero.

    Each of the four totals used a `-1` sentinel, on the reasoning that a count is never negative.
    The suite proved it only on a receipt whose other totals were non-zero, so `-1` could become
    `0` in any *one* of the four and the suite stayed green: the other members failed anyway and
    each of these is a single check. A session with no metered calls separates them — every true
    value is zero, so only the sentinel can tell "absent" from "stated as zero".
    """
    vector = json.loads((SPEC_VECTORS / "receipt.json").read_text(encoding="utf-8"))
    receipt = json.loads(json.dumps(vector))
    receipt.pop("latency", None)
    receipt["appendix"]["usage"] = []
    receipt["totals"].update(
        {
            "cost_usd": "0.000000",
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "usage_reported_calls": 0,
            "usage_estimated_calls": 0,
            "by_model": [],
        }
    )
    baseline = verify_receipt(receipt)
    assert baseline.ok, baseline.failures

    for member, check_name in (
        ("calls", "totals"),
        ("input_tokens", "token-totals"),
        ("output_tokens", "token-totals"),
        ("total_tokens", "token-totals"),
    ):
        wounded = json.loads(json.dumps(receipt))
        wounded["totals"].pop(member)
        result = verify_receipt(wounded)
        assert result.ok is False, f"a receipt with no totals.{member} verified"
        states = {c.name: c.status for c in result.checks}
        assert states[check_name] == "fail", (member, states.get(check_name))


def test_the_empty_appendix_case_is_decided_by_the_appendix_being_readable():
    """`if not rows and not appendix_ok` looks like two conditions with one job. It has two.

    Turning either `not` off is invisible to the ordinary receipt: it has rows, so the first
    condition is false whichever way it is read. The two cases that separate them are a session
    with no metered calls and a receipt whose appendix cannot be read at all — and they pull in
    opposite directions. An unreadable appendix must not also be told "no metered calls" (a pass
    beside a fail under one id), and an empty one must still say every row was accounted for.
    """
    vector = json.loads((SPEC_VECTORS / "receipt.json").read_text(encoding="utf-8"))

    empty = json.loads(json.dumps(vector))
    empty.pop("latency", None)
    empty["appendix"]["usage"] = []
    empty["totals"].update(
        {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cached_input_tokens": 0}
    )
    verdicts = [c for c in verify_receipt(empty).checks if c.name == "usage-inclusion-proofs"]
    assert [c.status for c in verdicts] == ["pass"], verdicts
    assert "no metered calls" in verdicts[0].detail

    unreadable = json.loads(json.dumps(vector))
    unreadable["appendix"]["usage"] = "the rows were lost"
    statuses = [c.status for c in verify_receipt(unreadable).checks if c.name == "usage-inclusion-proofs"]
    assert statuses and all(status == "fail" for status in statuses), statuses
