"""The conformance corpus, and the three defects writing it found.

A negative vector is a claim about the format: *this* document must be refused, by *this* check. The
first half of this file holds the corpus to that claim mechanically — it rebuilds it, runs the
reference verifier over every vector, and fails if any vector is accepted or refused by a different
rule. That is what stops the corpus from drifting into documentation of whatever the code happens to
do.

The second half is the reason the corpus was built rather than described. Writing
`21-usage-source-unknown` and running it turned up a receipt that verified while carrying a
`usage_source` outside the vocabulary spec §6.1 defines; running the same corpus against the Node
verifier turned up two more (a duplicate JSON name accepted silently, and a usage row's re-binding
folded into the inclusion-proof check). Each one is now a test, so the fix cannot quietly regress.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from docket.verify import verify_receipt

REPO = pathlib.Path(__file__).resolve().parents[1]
INVALID = REPO / "spec" / "vectors" / "invalid"
BASE = json.loads((REPO / "spec" / "vectors" / "receipt.json").read_text(encoding="utf-8"))


def _run_cli(path: pathlib.Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "docket", "verify", str(path), "--json", *arguments],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO),
    )


def test_the_corpus_still_says_what_it_claims():
    """Rebuild in memory and re-derive every expectation.

    This is not a byte comparison (that is `build.py --check`); it is the semantic one: after every
    change to the verifier, each vector must still fail with the check its expectation names.
    """
    sys.path.insert(0, str(INVALID))
    try:
        builder = __import__("build")
    finally:
        sys.path.pop(0)
    problems = builder.verify(builder.build())
    assert problems == [], "\n".join(problems)


def test_both_shipped_verifiers_agree_with_the_corpus(tmp_path):
    """`tools/conformance.py` over both implementations: nothing wrong, nothing unjudged.

    `--allow-gaps` is deliberately *not* passed. An implementation that skips the check a vector is
    about has not passed the vector, and this test is here to hold the two verifiers to the same
    vocabulary rather than to a count of agreements.
    """
    completed = subprocess.run(
        [sys.executable, "tools/conformance.py", "--python", "--node"],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "0 wrong" in completed.stdout
    assert "0 not judged" in completed.stdout


@pytest.mark.parametrize("vector", sorted(path.name for path in INVALID.glob("*.expected.json")))
def test_every_vector_carries_a_rule_and_a_mutation(vector):
    """An expectation without a stated rule is a fixture; with one, it is an argument.

    Three kinds exist and each must be complete:
      * a check id — the vector must be refused *by that check*;
      * `reject` — a parse-level refusal, with the words its reason may name (possibly none: the
        corpus cannot require one implementation's prose);
      * `accept` — a document the format requires reading (an integral float, `-0`, a decomposed
        form), which is where two implementations most often disagree without either being broken.
    """
    expectation = json.loads((INVALID / vector).read_text(encoding="utf-8"))
    assert expectation["rule"].startswith(("spec §", "RFC ")), expectation["rule"]
    assert expectation["mutation"].strip()
    kinds = [expectation["check"] is not None, expectation["reject"] is not None, expectation.get("accept") is True]
    assert sum(kinds) == 1, f"{vector} must name exactly one outcome, found {kinds}"
    if expectation["reject"] is not None:
        assert expectation["reject"]["kind"] == "parse"
        assert isinstance(expectation["reject"]["reason_any_of"], list)


def test_usage_source_outside_the_vocabulary_is_a_shape_failure():
    """The defect the corpus found: an enum read as free text.

    `usage_source` is `"reported"` or `"estimated"` (spec §6.1), and a consumer is required not to
    present estimated usage as metered. A row saying `"guessed"` used to verify: its arithmetic was
    still recomputed, so every check that looked at numbers passed, and the one word a consumer
    would branch on was never examined.
    """
    receipt = json.loads(json.dumps(BASE))
    receipt["appendix"]["usage"][0]["usage_source"] = "guessed"
    result = verify_receipt(receipt)
    assert result.ok is False
    shape = [check for check in result.checks if check.name == "receipt-shape"]
    assert shape and shape[0].status == "fail"
    assert "usage_source" in shape[0].detail and "guessed" in shape[0].detail


@pytest.mark.parametrize(
    ("label", "contents", "expected_word"),
    [
        ("NaN", "NaN", "NaN"),
        ("Infinity", "Infinity", "Infinity"),
        ("a -Infinity literal", "-Infinity", "Infinity"),
    ],
)
def test_a_non_json_constant_is_refused_before_any_check_runs(tmp_path, label, contents, expected_word):
    """A parse failure is a verdict a machine can read: exit 1, empty stdout, a stated reason.

    The corpus made this concrete. `docket verify --json` on a receipt containing `NaN` exits 1 with
    *no JSON at all*, because the document never reaches the verifier — the loader refuses it. A
    consumer that only reads stdout sees "nothing", which is indistinguishable from a crash. The
    contract this test holds is the one that makes the conformance tool able to classify it: the
    reason is on stderr, in a sentence that names the offending constant.
    """
    path = tmp_path / "receipt.json"
    text = (REPO / "spec" / "vectors" / "receipt.json").read_text(encoding="utf-8")
    marker = '"calls": 4'
    assert marker in text
    path.write_text(text.replace(marker, f'"calls": {contents}', 1), encoding="utf-8")
    completed = _run_cli(path)
    assert completed.returncode != 0
    assert completed.stdout.strip() == "", "a refused document must not also produce a verdict"
    assert expected_word in completed.stderr
    assert label  # the parametrisation is the documentation


def test_a_duplicate_member_is_refused_rather_than_resolved(tmp_path):
    """Two names for one field make a file mean two things to two readers.

    RFC 8259 §4 leaves the behaviour undefined and spec §3.1 requires unique names. The loader
    refuses, on stderr, naming the repeated member — and says why, because "duplicate key" without
    the consequence invites a caller to work around it.
    """
    text = (REPO / "spec" / "vectors" / "receipt.json").read_text(encoding="utf-8")
    marker = '"docket_number": "'
    index = text.index(marker) + len(marker)
    forged = text[:index] + 'DKT-DUPLICATE-0000", "docket_number": "' + text[index:]
    path = tmp_path / "receipt.json"
    path.write_text(forged, encoding="utf-8")
    completed = _run_cli(path)
    assert completed.returncode != 0
    assert completed.stdout.strip() == ""
    assert "docket_number" in completed.stderr
    assert "twice" in completed.stderr


def test_the_corpus_runner_needs_no_network_and_no_credential():
    """The corpus is published so a stranger can run it offline, with nothing installed and no key.

    The vectors themselves carry URLs — a receipt embeds the price-table sources it used, and that is
    data about where a price came from, not a fetch. What must not reach for the network is the code
    that *runs* the corpus: if `tools/conformance.py` or the corpus builder imported an HTTP client,
    the claim "clone it and run it" would be false in a sandbox with no egress. This is a source-level
    guard, stated as such: it rules out an import, not a future `subprocess` call to `curl`.
    """
    for path in (REPO / "tools" / "conformance.py", INVALID / "build.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("import socket", "import urllib", "import requests", "from urllib", "http.client"):
            assert forbidden not in text, f"{path.name} imports {forbidden}"
    for expectation in sorted(INVALID.glob("*.expected.json")):
        ledger = json.loads(expectation.read_text(encoding="utf-8"))["ledger"]
        assert ledger is None or "://" not in ledger, expectation.name


def _corpus_copy(tmp_path: pathlib.Path) -> pathlib.Path:
    """A writable copy of the corpus, so a canary can lie to the runner without touching the repo."""
    import shutil

    target = tmp_path / "invalid"
    shutil.copytree(INVALID, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def test_the_runner_can_fail(tmp_path):
    """A test that only ever sees a passing run is a test that has never seen the tool work.

    Every assertion in this file up to here says "the corpus and the verifiers agree". None of them
    says the runner *notices when they do not*, and a runner with an inverted comparison would pass
    all of them. So this one makes the corpus lie: one expectation is rewritten to name a check the
    verifier will not emit, and the runner must exit non-zero and say `wrong-check`. The vector is
    copied first — a canary that edits the repository is a canary that breaks the next test run.
    """
    corpus = _corpus_copy(tmp_path)
    expectation = corpus / "18-totals-mismatch.expected.json"
    payload = json.loads(expectation.read_text(encoding="utf-8"))
    payload["check"] = "signature"  # the receipt is not signed, so this check is a skip, never a failure
    expectation.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "tools/conformance.py", "--python", "--corpus", str(corpus)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO),
    )
    assert completed.returncode != 0, "the runner accepted a vector whose expectation is wrong"
    assert "WRONG-CHECK" in completed.stdout or "wrong-check" in completed.stdout.lower(), completed.stdout


def test_a_vector_without_an_expectation_is_not_silently_ignored(tmp_path):
    """An orphan vector is a document nobody checks, and silence is how it stays that way.

    The runner iterates expectations, so a `.json` with no `*.expected.json` beside it is invisible.
    That is the right default for a directory that also holds ledgers — but it must be *counted and
    reported*, not merely absent, or a vector can be added and forgotten in the same commit.
    """
    corpus = _corpus_copy(tmp_path)
    (corpus / "99-orphan.json").write_text('{"format": "docket/receipt/v1"}\n', encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "tools/conformance.py", "--python", "--corpus", str(corpus)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(REPO),
    )
    assert completed.returncode != 0, "an orphan vector was accepted silently"
    assert "99-orphan.json" in completed.stdout + completed.stderr


def test_a_call_count_that_disagrees_with_the_appendix_is_a_totals_failure():
    """Probing found `totals.calls` was never compared with the appendix.

    A receipt could say `"calls": 0` beside four usage rows and verify: the total cost was right,
    the token sum was right, and the number a cap counts and a reader quotes was never read. It is
    the same claim as the cost — "this is what the rows add up to" — so it is the same id, reported
    once (two rows under one id is the failure `test_no_check_reports_two_verdicts` exists for).
    """
    receipt = json.loads(json.dumps(BASE))
    receipt["totals"]["calls"] = 0
    result = verify_receipt(receipt)
    assert result.ok is False
    rows = [check for check in result.checks if check.name == "totals"]
    assert len(rows) == 1, "one id, one verdict"
    assert rows[0].status == "fail"
    assert "0 call(s)" in rows[0].detail and "4" in rows[0].detail


def test_tokens_moved_between_the_halves_are_a_token_totals_failure():
    """The sum can be right while both halves are wrong — the shape of a fabricated usage record."""
    receipt = json.loads(json.dumps(BASE))
    receipt["totals"]["input_tokens"] -= 100
    receipt["totals"]["output_tokens"] += 100
    assert receipt["totals"]["total_tokens"] == receipt["totals"]["input_tokens"] + receipt["totals"]["output_tokens"]
    result = verify_receipt(receipt)
    rows = [check for check in result.checks if check.name == "token-totals"]
    assert len(rows) == 1
    assert rows[0].status == "fail"
    assert "input_tokens" in rows[0].detail and "output_tokens" in rows[0].detail


def test_an_integral_float_is_read_and_a_fractional_one_is_refused():
    """The corpus settled a disagreement rather than documenting one.

    Spec §3.2 says a float with an integral value MUST be normalised to an integer and implementations
    MUST reject a *non-integral* float. The Python verifier refused both, the Node one accepted both,
    and neither had ever been asked. The ambiguity is now decided by the sentence that exists, in
    both implementations, and `34-integral-float-accepted` holds them to it.
    """
    integral = json.loads(json.dumps(BASE))
    integral["totals"]["calls"] = 4.0  # what `4e0` and `4.0` both parse to
    assert verify_receipt(integral).ok is True

    fractional = json.loads(json.dumps(BASE))
    fractional["totals"]["calls"] = 4.5
    result = verify_receipt(fractional)
    assert result.ok is False
    shape = [check for check in result.checks if check.name == "receipt-shape"]
    assert shape and "fractional" in shape[0].detail


def test_the_corpus_demands_no_implementations_prose():
    """A refusal must state a reason; it must not be required to use my words.

    The first version of the BOM vector demanded the word "BOM", which is Python's wording. Node
    refused correctly with "Unexpected token '\ufeff'" and was scored `wrong-reason`. A corpus is a
    contract with third parties — including the Go implementation that does not exist yet — so it may
    require that the reason names a construct *in the document* (`NaN`, `Infinity`, a member name),
    and for byte-level damage with no nameable token, nothing at all.
    """
    for path in sorted(INVALID.glob("*.expected.json")):
        expectation = json.loads(path.read_text(encoding="utf-8"))
        if expectation["reject"] is None:
            continue
        words = expectation["reject"]["reason_any_of"]
        assert isinstance(words, list)
        document = (INVALID / path.name.replace(".expected.json", ".json")).read_bytes().decode("utf-8", "replace")
        for word in words:
            assert word in document, f"{path.name} requires {word!r}, which is not in the document"
