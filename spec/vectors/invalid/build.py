#!/usr/bin/env python3
"""Build the negative conformance corpus, and prove every vector fails for the reason it claims.

A conformance corpus that only contains valid documents tests that an implementation accepts what it
should. It says nothing about the thing a verifier is actually for: refusing what it must. This script
derives the negative corpus from the two valid vectors that ship beside it, mutates each copy in exactly
one stated way, and then **runs the reference verifier on the result** and refuses to write the file
unless the verifier's first failure is the check the vector claims.

That double duty is the point. The mutation is a claim about the spec ("`totals.cost_usd` that does not
sum is a `totals` failure"); the verifier is an opinion about the claim. If the two disagree, one of
them is wrong, and the build fails instead of shipping a corpus that documents a defect as a feature.

    python3 spec/vectors/invalid/build.py            # write the corpus
    python3 spec/vectors/invalid/build.py --check    # rebuild in memory and compare bytes

Every vector is a complete receipt, because a stranger has to be able to point their own verifier at
the file without running any of this code. Expectation files are JSON so a third-party runner can read
them without a parser for this script's format.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SPEC = HERE.parent.parent
REPO = SPEC.parent
BASE_RECEIPT = SPEC / "vectors" / "receipt.json"
BASE_LEDGER = SPEC / "vectors" / "ledger.json"
CACHE_RECEIPT = SPEC / "vectors" / "cache.json"

#: The ledger that most vectors are verified against. It is one file, not one per vector: the receipt
#: files are complete documents, but the ledger they were issued over is shared evidence, and copying it
#: twenty times would make the corpus harder to read rather than easier to trust.
LEDGER_NAME = "base.ledger.jsonl"

#: Where each vector's ledger lives, if it has one.
BASE = "base"
OWN = "own"
NONE = None

#: A vector that must be **accepted**: the document is unusual but the format requires reading it.
#: Refusals are the corpus's main business, but a corpus that only says what to refuse lets two
#: implementations disagree about what to *read* — and that is the first ambiguity a second
#: implementer meets. Each such vector cites the normative sentence that requires acceptance.
ACCEPT = "accept"

#: A vector whose **ledger** must be refused: the receipt beside it is valid, and the damage is in the
#: file the verifier is asked to read it against. Reason tokens follow the same rule as REJECT_WORDS —
#: only what appears in the document, or nothing at all.
LEDGER_REJECT = "ledger-reject"
LEDGER_REJECT_WORDS = {
    "37-ledger-duplicate-name": ["seq"],
    "38-ledger-truncated-line": [],
}

#: What the refusal on stderr must mention, per vector — **any one of**, and only tokens that appear
#: in the document itself.
#:
#: This started as a single required word per vector and immediately over-fitted: the BOM vector
#: demanded "BOM", which is Python's wording, and Node's parser said "Unexpected token '\ufeff'"
#: and was scored `wrong-reason` for refusing correctly. A corpus is a contract with third parties,
#: so it may not require *my* prose. What it may require is that the reason names the construct the
#: document actually contains (`NaN`, `Infinity`, `ledger`) — and, for byte-level damage with no
#: nameable token, nothing at all: an empty list means "refused, with some reason stated".
REJECT_WORDS = {
    "03-nan-in-totals": ["NaN"],
    "04-infinity-in-totals": ["Infinity"],
    "05-duplicate-member": ["docket_number"],
    "31-utf8-bom": [],
    "32-trailing-bytes": [],
    "33-invalid-utf8": [],
}


def ledger_text(events: list[dict]) -> str:
    """The JSONL a `docket run` writes: one canonical-compact event per line, trailing newline."""
    return "\n".join(json.dumps(e, sort_keys=True, separators=(",", ":")) for e in events) + "\n"


def base_pair() -> tuple[dict, str]:
    """The valid receipt and the bytes of the ledger it was issued over, made consistent with each other.

    The shipped vector's `ledger.file_sha256` was computed elsewhere; materialising the ledger here is
    what lets a vector ship a ledger at all, so the digest is recomputed over exactly the bytes written.
    """
    receipt = json.loads(BASE_RECEIPT.read_text(encoding="utf-8"))
    events = json.loads(BASE_LEDGER.read_text(encoding="utf-8"))["events"]
    text = ledger_text(events)
    receipt["ledger"]["file_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return receipt, text


def cache_pair() -> tuple[dict, None]:
    receipt = json.loads(CACHE_RECEIPT.read_text(encoding="utf-8"))
    return receipt, None


# ---------------------------------------------------------------------------------------------
# Mutations. Each takes (receipt, events, text) and returns (receipt, events, text, raw_override).
# `raw_override` is a string written instead of the JSON serialisation, for vectors that are about
# the bytes rather than about a value (duplicate names, NaN).
# ---------------------------------------------------------------------------------------------


def _noop(r, e, t):
    return r, e, t, None


def _format_unknown(r, e, t):
    r["format"] = "docket/receipt/v2"
    return r, e, t, None


def _totals_not_object(r, e, t):
    r["totals"] = "not an object"
    return r, e, t, None


def _nan(r, e, t):
    return r, e, t, json.dumps(r).replace('"calls": 4', '"calls": NaN', 1)


def _infinity(r, e, t):
    return r, e, t, json.dumps(r).replace('"calls": 4', '"calls": Infinity', 1)


def _duplicate_key(r, e, t):
    """A valid JSON text with one name appearing twice — legal to write, ambiguous to read."""
    text = json.dumps(r)
    marker = '"docket_number": "'
    index = text.index(marker) + len(marker)
    return r, e, t, text[:index] + 'DKT-DUPLICATE-0000", "docket_number": "' + text[index:]


def _top_level_list(r, e, t):
    return [r], e, t, None


def _chain_edited(r, e, t):
    e = copy.deepcopy(e)
    e[3]["data"]["edited_after_the_fact"] = True
    return r, e, ledger_text(e), None


def _head(r, e, t):
    r["ledger"]["head"] = "0" * 64
    return r, e, t, None


def _event_count(r, e, t):
    r["ledger"]["events"] = r["ledger"]["events"] - 1
    return r, e, t, None


def _merkle_root(r, e, t):
    r["ledger"]["merkle_root"] = "0" * 64
    return r, e, t, None


def _file_digest(r, e, t):
    return r, e, t + "\n", None


def _event_float(r, e, t):
    e = copy.deepcopy(e)
    e[3]["data"]["float_probe"] = 5.0
    return r, e, ledger_text(e), None


def _proof_tampered(r, e, t):
    path = r["appendix"]["usage"][0]["merkle_path"]
    path[0]["hash"] = "0" * 64
    return r, e, t, None


def _row_without_proof(r, e, t):
    r["appendix"]["usage"][0].pop("merkle_path")
    return r, e, t, None


def _row_rebound(r, e, t):
    rows = r["appendix"]["usage"]
    first, second = rows[0], rows[1]
    first["event_hash"], second["event_hash"] = second["event_hash"], first["event_hash"]
    first["merkle_path"], second["merkle_path"] = second["merkle_path"], first["merkle_path"]
    return r, e, t, None


def _row_seq_outside(r, e, t):
    r["appendix"]["usage"][0]["seq"] = 9999
    return r, e, t, None


def _cost_row(r, e, t):
    r["appendix"]["usage"][0]["cost_usd"] = "9.990000"
    return r, e, t, None


def _totals_cost(r, e, t):
    r["totals"]["cost_usd"] = "9.990000"
    return r, e, t, None


def _token_totals(r, e, t):
    r["totals"]["total_tokens"] = r["totals"]["total_tokens"] + 1
    return r, e, t, None


def _latency(r, e, t):
    r["latency"]["p50_ms"] = r["latency"]["p50_ms"] + 1
    return r, e, t, None


def _usage_source(r, e, t):
    r["appendix"]["usage"][0]["usage_source"] = "guessed"
    return r, e, t, None


def _limits_missing(r, e, t):
    r.pop("limits_disclosure")
    return r, e, t, None


def _cache_saved(r, e, t):
    r["cache"]["saved_usd"] = "9.990000"
    return r, e, t, None


def _cache_origin(r, e, t):
    r["cache"]["by_origin"][0]["origin_seq"] = 0
    return r, e, t, None


def _cache_proof(r, e, t):
    r["cache"]["by_origin"][0]["merkle_path"][0]["hash"] = "0" * 64
    return r, e, t, None


def _totals_calls(r, e, t):
    r["totals"]["calls"] = 0
    return r, e, t, None


def _totals_input_tokens(r, e, t):
    r["totals"]["input_tokens"] = 1
    return r, e, t, None


def _totals_token_split(r, e, t):
    """The sum stays right while the halves move: the shape a fabricated usage record has."""
    moved = 100
    r["totals"]["input_tokens"] -= moved
    r["totals"]["output_tokens"] += moved
    return r, e, t, None


def _bom(r, e, t):
    return r, e, t, "\ufeff" + json.dumps(r)


def _trailing_bytes(r, e, t):
    return r, e, t, json.dumps(r) + "\nnull\n"


def _invalid_utf8(r, e, t):
    """A byte that cannot appear in UTF-8, inside the document's own text.

    The first attempt at this vector was wrong in a way worth keeping written down: it built a
    *string* containing U+00FF and encoded it, which is valid UTF-8 (C3 BF) and verified cleanly.
    An invalid-byte vector has to be bytes, so the payload path accepts `bytes` and this returns
    one with a lone 0xFF where a name should be.
    """
    forged = json.dumps(r).replace('"docket_number"', '"docket_number\u00ff"', 1).encode("utf-8")
    return r, e, t, forged.replace(b'"docket_number\xc3\xbf"', b'"docket_number\xff"', 1)


def _integral_float(r, e, t):
    """`calls` written as `4e0`: a float with an integral value (spec §3.2, second bullet)."""
    return r, e, t, json.dumps(r).replace('"calls": 4', '"calls": 4e0', 1)


def _negative_zero(r, e, t):
    """`-0` is a JSON integer and equals zero; a reader that refuses it is refusing arithmetic."""
    return r, e, t, json.dumps(r).replace('"asserted_calls": 0', '"asserted_calls": -0', 1)


def _non_nfc(r, e, t):
    """A decomposed é where the composed form is the one a writer would emit.

    Spec §3.1 emits non-ASCII literally and sorts by code point; it never requires normalisation, so
    a reader MUST accept either form and MUST hash the bytes it was given (not a re-composed copy).
    The vectors that follow a receipt's digest would drift if an implementation normalised silently.
    """
    text = json.dumps(r).replace('"docket_number": "DKT-20260102-VECT01"', '"docket_number": "DKT-2026-\u00e9"', 1)
    return r, e, t, text.replace("\u00e9", "e\u0301")


def _quota_mode_basis_that_cannot_be_checked(r, e, t):
    """A subscription run: spend is counted in tokens, so the basis is a claim nothing can test.

    Refusing it would refuse every run on a metered-nothing plan; accepting it as *checked* would
    be believing an assertion. The format's answer is the third state, and this vector is what
    holds both implementations to it.
    """
    r["totals"]["cost_basis"] = "quota mode: subscription usage is counted in tokens, not dollars"
    return r, e, t, None


def _by_model_invents_a_model(r, e, t):
    """A per-model row for a model that was never called, priced to keep the total correct.

    This is the shape of a padded invoice: the total still reconciles, so every check that only
    sums the rows stays green, and the reader who wants to know *what* the money was spent on is
    the one who is misled.
    """
    r["totals"]["by_model"][0]["model"] = "model-that-was-never-called"
    return r, e, t, None


def _usage_counts_call_estimated_rows_reported(r, e, t):
    """Every row is estimated, and the receipt calls all four of them reported."""
    for row in r["appendix"]["usage"]:
        row["usage_source"] = "estimated"
    r["totals"]["usage_reported_calls"] = len(r["appendix"]["usage"])
    r["totals"]["usage_estimated_calls"] = 0
    return r, e, t, None


def _cost_basis_contradicts_its_rows(r, e, t):
    """`mixed` with one row, `reported` with three of four estimated — here, the second."""
    for row in r["appendix"]["usage"]:
        row["usage_source"] = "estimated"
    r["totals"]["usage_reported_calls"] = 0
    r["totals"]["usage_estimated_calls"] = len(r["appendix"]["usage"])
    r["totals"]["cost_basis"] = "reported: provider usage fields"
    return r, e, t, None


def _ledger_duplicate_name(r, e, t):
    """The *ledger* is the malformed file: one event names a key twice.

    `raw` carries the ledger's bytes and `text` is unused, because the vector's whole point is a
    ledger a verifier must refuse to read. Which value was hashed is unknowable — the hash is over
    the canonical form of an object that has two of them — so reconstructing one is guessing.
    """
    events = [json.loads(line) for line in t.strip().splitlines()]
    first = t.splitlines()[0]
    forged = first[:-1] + ', "seq": 0}'
    return r, events, t, forged


def _ledger_truncated_line(r, e, t):
    """The last line is half-written: an interrupted append, which is what a crash leaves behind."""
    lines = t.strip().splitlines()
    return r, None, t, t + lines[-1][: len(lines[-1]) // 2]


def _assumed_undisclosed(r, e, t):
    r["totals"].pop("assumed_price_models", None)
    return r, e, t, None


def _cache_hits(r, e, t):
    r["cache"]["hits"] = 99
    return r, e, t, None


#: name, base, mutation, expected first failing check, ledger, the rule the vector enforces, what changed.
VECTORS: list[tuple[str, str, object, str | None, str | None, str, str]] = [
    (
        "01-format-unknown",
        BASE,
        _format_unknown,
        "format",
        BASE,
        "spec §6: format is docket/receipt/v1",
        "the format tag names a version that does not exist",
    ),
    (
        "02-totals-not-object",
        BASE,
        _totals_not_object,
        "receipt-shape",
        BASE,
        "spec §6: every listed member is an object",
        "totals is a string",
    ),
    (
        "03-nan-in-totals",
        BASE,
        _nan,
        None,
        BASE,
        "spec §3.2: NaN is not JSON and MUST be rejected",
        "a NaN token appears in the document",
    ),
    (
        "04-infinity-in-totals",
        BASE,
        _infinity,
        None,
        BASE,
        "spec §3.2: Infinity is not JSON and MUST be rejected",
        "an Infinity token appears in the document",
    ),
    (
        "05-duplicate-member",
        BASE,
        _duplicate_key,
        None,
        BASE,
        "RFC 8259 §4 + spec §3.1: object names are unique",
        "docket_number appears twice in one object",
    ),
    (
        "06-top-level-list",
        BASE,
        _top_level_list,
        "receipt-format",
        BASE,
        "spec §6: a receipt is a JSON object",
        "the document is a JSON array",
    ),
    (
        "07-chain-edited-event",
        BASE,
        _chain_edited,
        "ledger-chain",
        OWN,
        "spec §4.3: every event hash recomputes",
        "event #3 gained a field after its hash was computed",
    ),
    ("08-head-mismatch", BASE, _head, "head", BASE, "spec §8 item 3", "ledger.head is 64 zeros"),
    (
        "09-event-count-mismatch",
        BASE,
        _event_count,
        "event-count",
        BASE,
        "spec §8 item 3",
        "ledger.events is one short of the ledger",
    ),
    (
        "10-merkle-root-mismatch",
        BASE,
        _merkle_root,
        "merkle-root",
        BASE,
        "spec §8 item 3",
        "ledger.merkle_root is 64 zeros",
    ),
    (
        "11-ledger-trailing-byte",
        BASE,
        _file_digest,
        "ledger-file-digest",
        OWN,
        "spec §8 item 7",
        "the ledger file gained a trailing newline",
    ),
    (
        "12-event-with-float",
        BASE,
        _event_float,
        "ledger-chain",
        OWN,
        "spec §3.2: a hashed structure MUST NOT contain a float",
        "event #3 gained a float in its data",
    ),
    (
        "13-usage-proof-tampered",
        BASE,
        _proof_tampered,
        "usage-inclusion-proofs",
        BASE,
        "spec §8 item 4",
        "one hex digit of one inclusion path is zeroed",
    ),
    (
        "14-usage-row-without-proof",
        BASE,
        _row_without_proof,
        "usage-inclusion-proofs",
        BASE,
        "spec §8 item 4 + §6.1",
        "one usage row was stripped of its merkle_path",
    ),
    (
        "15-usage-row-rebound",
        BASE,
        _row_rebound,
        "usage-rows-rebound",
        BASE,
        "spec §8 item 4: a row must be the event the ledger has at its seq",
        "two rows exchanged their event hash and proof",
    ),
    (
        "16-usage-seq-outside",
        BASE,
        _row_seq_outside,
        "usage-rows-rebound",
        BASE,
        "spec §6.1: seq names an event in the ledger",
        "one row cites seq 9999",
    ),
    (
        "17-cost-row-mismatch",
        BASE,
        _cost_row,
        "cost-recomputation",
        BASE,
        "spec §8 item 5",
        "one row's cost_usd is 9.99",
    ),
    (
        "18-totals-mismatch",
        BASE,
        _totals_cost,
        "totals",
        BASE,
        "spec §8 item 5",
        "totals.cost_usd does not equal the sum of the rows",
    ),
    (
        "19-token-totals-mismatch",
        BASE,
        _token_totals,
        "token-totals",
        BASE,
        "spec §8 item 5",
        "totals.total_tokens is one higher than the rows",
    ),
    (
        "20-latency-summary-mismatch",
        BASE,
        _latency,
        "latency-summary",
        BASE,
        "spec §6.2 + §8 item 6",
        "latency.p50_ms was changed by one millisecond",
    ),
    (
        "21-usage-source-unknown",
        BASE,
        _usage_source,
        "receipt-shape",
        BASE,
        "spec §6.1: usage_source is 'reported' | 'estimated'",
        "one row's usage_source is 'guessed'",
    ),
    (
        "22-limits-disclosure-missing",
        BASE,
        _limits_missing,
        "limits-disclosure",
        BASE,
        "spec §8 item 9",
        "the limits_disclosure member was removed",
    ),
    (
        "23-cache-saved-mismatch",
        "cache",
        _cache_saved,
        "cache-savings",
        NONE,
        "spec §8 item 10",
        "cache.saved_usd is 9.99",
    ),
    (
        "24-cache-origin-wrong",
        "cache",
        _cache_origin,
        "cache-hit-origins",
        NONE,
        "spec §8 item 10",
        "a cache group cites seq 0, which is not a metered call",
    ),
    (
        "25-cache-proof-tampered",
        "cache",
        _cache_proof,
        "cache-hit-proofs",
        NONE,
        "spec §8 item 10",
        "one hex digit of one cache proof is zeroed",
    ),
    (
        "26-cache-hits-mismatch",
        "cache",
        _cache_hits,
        "cache-savings",
        NONE,
        "spec §8 item 10",
        "cache.hits is 99 while the groups add up to 2",
    ),
    (
        "27-assumed-price-undisclosed",
        BASE,
        _assumed_undisclosed,
        "assumed-prices",
        BASE,
        "spec §9.3: a fallback price must be visible wherever the cost is",
        "totals.assumed_price_models was removed from a receipt that priced an unlisted model",
    ),
    (
        "28-totals-call-count",
        BASE,
        _totals_calls,
        "totals",
        BASE,
        "spec §6: totals.calls is the number of metered calls the appendix holds",
        "totals.calls is 0 while four usage rows are present",
    ),
    (
        "29-totals-input-tokens",
        BASE,
        _totals_input_tokens,
        "token-totals",
        BASE,
        "spec §6: totals.input_tokens is the sum over the usage rows",
        "totals.input_tokens is 1 while the rows sum to 9,000",
    ),
    (
        "30-totals-token-split",
        BASE,
        _totals_token_split,
        "token-totals",
        BASE,
        "spec §6: each token total is the sum of its own column",
        "100 tokens moved from input to output, so the sum is unchanged and the halves are wrong",
    ),
    # -- the parsing family: refusals that happen before any check runs ----------------------
    (
        "31-utf8-bom",
        BASE,
        _bom,
        None,
        BASE,
        "spec §3.1: a document is UTF-8 JSON, and a BOM is not part of it",
        "the file begins with a UTF-8 BOM",
    ),
    (
        "32-trailing-bytes",
        BASE,
        _trailing_bytes,
        None,
        BASE,
        "RFC 8259 §2: a JSON text is one value and nothing follows it",
        "a second JSON value follows the receipt",
    ),
    (
        "33-invalid-utf8",
        BASE,
        _invalid_utf8,
        None,
        BASE,
        "spec §3.1: the document is UTF-8",
        "a byte that is not valid UTF-8 appears inside a string",
    ),
    # -- the parsing family: documents that MUST be accepted ---------------------------------
    # These carry `accept` rather than `check`. They are here because a corpus that only says what
    # to refuse lets two implementations disagree about what to *read*, which is the ambiguity a
    # second implementer hits first. Spec §3.2 requires an integral float to be normalised, so
    # `4e0` is 4; `-0` is the integer zero; §3.1 never requires Unicode normalisation, so a
    # decomposed form is the same string written differently and MUST NOT be re-encoded.
    (
        "34-integral-float-accepted",
        BASE,
        _integral_float,
        ACCEPT,
        BASE,
        "spec §3.2: a float with an integral value MUST be normalised to an integer",
        "totals.calls is written `4e0`; the value is 4 and the receipt must still verify",
    ),
    (
        "35-negative-zero-accepted",
        BASE,
        _negative_zero,
        ACCEPT,
        BASE,
        "RFC 8259 §6: `-0` is a JSON number and equals zero",
        "totals.asserted_calls is written `-0`",
    ),
    (
        "36-non-nfc-accepted",
        BASE,
        _non_nfc,
        ACCEPT,
        BASE,
        "spec §3.1: non-ASCII is emitted literally; normalisation is never required",
        "a string carries a decomposed form; the bytes must not be rewritten",
    ),
    # -- the ledger itself is malformed ------------------------------------------------------
    # A receipt-level mutation cannot express this: the document under test is the *ledger*, and
    # what is being asserted is that a verifier refuses to read it rather than repairing it.
    (
        "37-ledger-duplicate-name",
        BASE,
        _ledger_duplicate_name,
        "ledger-reject",
        OWN,
        "spec §3.1: object names are unique; RFC 8259 §4 makes a repeat undefined",
        "the ledger's first event names `seq` twice, so which value was hashed is unknowable",
    ),
    # -- members the receipt states but does not prove --------------------------------------
    # Each of these leaves every arithmetic check that only sums the rows correct: the total
    # still reconciles with the appendix, so a verifier that stops at the sum believes the
    # receipt. What is wrong is the *breakdown* — where the money went, and whether the numbers
    # were reported or guessed — which is the part a human reads.
    (
        "42-quota-mode-basis-is-accepted",
        BASE,
        _quota_mode_basis_that_cannot_be_checked,
        ACCEPT,
        BASE,
        "spec §8 item 5b: quota mode is reported as not checked, never as a contradiction",
        "a subscription run states a quota-mode basis, which the receipt states nowhere else",
    ),
    (
        "39-by-model-invents-a-model",
        BASE,
        _by_model_invents_a_model,
        "by-model-totals",
        BASE,
        "spec §8 item 5a: by_model partitions the usage rows, no model appears that no row used",
        "totals.by_model names a model that no usage row used, with the total unchanged",
    ),
    (
        "40-usage-counts-say-reported",
        BASE,
        _usage_counts_call_estimated_rows_reported,
        "usage-source-counts",
        BASE,
        "spec §6.1: estimated usage must not be presented as metered; §8 item 5a counts the rows",
        "every row is estimated and the receipt says every call was reported",
    ),
    (
        "41-cost-basis-is-a-contradiction",
        BASE,
        _cost_basis_contradicts_its_rows,
        "cost-basis",
        BASE,
        "spec §8 item 5b: cost_basis may not contradict the usage-source counts it summarises",
        "cost_basis says reported while every row is estimated, and the counts beside it say so",
    ),
    (
        "38-ledger-truncated-line",
        BASE,
        _ledger_truncated_line,
        "ledger-reject",
        OWN,
        "spec §4: a ledger file is JSONL, one complete event per line",
        "the last line is half-written, as an interrupted append leaves it",
    ),
]


def build() -> dict[pathlib.Path, bytes]:
    """Every file the corpus consists of, as bytes. Nothing is written here."""
    base_receipt, base_ledger_text = base_pair()
    cache_receipt, _ = cache_pair()
    files: dict[pathlib.Path, bytes] = {
        HERE / LEDGER_NAME: base_ledger_text.encode("utf-8"),
    }
    for name, base, mutate, check, ledger, rule, mutation in VECTORS:
        receipt, events, text, raw = (
            (copy.deepcopy(base_receipt), None, None, None)
            if base == BASE
            else (copy.deepcopy(cache_receipt), None, None, None)
        )
        if base == BASE:
            events = json.loads(BASE_LEDGER.read_text(encoding="utf-8"))["events"]
            receipt, events, text, raw = mutate(receipt, events, base_ledger_text)
        else:
            receipt, events, text, raw = mutate(receipt, None, None)
        if check == LEDGER_REJECT:
            # The receipt is valid; the ledger is the file under test. `raw` holds the ledger's damaged
            # bytes, so the expectation can point at a file that exists on disk and a third party can
            # run it without this script.
            files[HERE / "ledgers" / f"{name}.jsonl"] = raw if isinstance(raw, bytes) else raw.encode("utf-8")
            payload = json.dumps(receipt, indent=1, ensure_ascii=False) + "\n"
        else:
            payload = raw if raw is not None else json.dumps(receipt, indent=1, ensure_ascii=False) + "\n"
        files[HERE / f"{name}.json"] = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        if ledger == OWN and check != LEDGER_REJECT:
            files[HERE / "ledgers" / f"{name}.jsonl"] = text.encode("utf-8")
        if check == LEDGER_REJECT:
            expectation = {
                "schema": "docket/conformance-expectation/v1",
                "vector": f"{name}.json",
                "ledger": f"ledgers/{name}.jsonl",
                "check": None,
                "reject": None,
                "accept": None,
                "ledger_reject": {"reason_any_of": LEDGER_REJECT_WORDS[name]},
                "rule": rule,
                "mutation": mutation,
            }
        elif check == ACCEPT:
            expectation = {
                "schema": "docket/conformance-expectation/v1",
                "vector": f"{name}.json",
                "ledger": None if ledger is None else (LEDGER_NAME if ledger == BASE else f"ledgers/{name}.jsonl"),
                "check": None,
                "reject": None,
                "accept": True,
                "rule": rule,
                "mutation": mutation,
            }
        elif check is None:
            expectation = {
                "schema": "docket/conformance-expectation/v1",
                "vector": f"{name}.json",
                "ledger": None if ledger is None else (LEDGER_NAME if ledger == BASE else f"ledgers/{name}.jsonl"),
                "check": None,
                "reject": {"kind": "parse", "reason_any_of": REJECT_WORDS[name]},
                "accept": None,
                "rule": rule,
                "mutation": mutation,
            }
        else:
            expectation = {
                "schema": "docket/conformance-expectation/v1",
                "vector": f"{name}.json",
                "ledger": None if ledger is None else (LEDGER_NAME if ledger == BASE else f"ledgers/{name}.jsonl"),
                "check": check,
                "reject": None,
                "accept": None,
                "rule": rule,
                "mutation": mutation,
            }
        files[HERE / f"{name}.expected.json"] = (json.dumps(expectation, indent=1) + "\n").encode("utf-8")
    return files


def _first_failure(receipt_path: pathlib.Path, ledger_path: pathlib.Path | None) -> tuple[str, str]:
    """(kind, detail): 'pass' | 'fail:<check>' | 'reject:<stderr>' | 'error:<why>'."""
    command = [sys.executable, "-m", "docket", "verify", str(receipt_path), "--json"]
    if ledger_path is not None:
        command += ["--ledger", str(ledger_path)]
    env = {"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}
    completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=180, cwd=str(REPO))
    if completed.returncode == 0 and completed.stdout.strip():
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return "error", f"exit 0 but not JSON: {completed.stdout[:120]!r}"
        failures = [c["name"] for c in result["checks"] if c["status"] == "fail"]
        return ("pass", "") if not failures else (f"fail:{failures[0]}", "")
    if completed.returncode != 0 and not completed.stdout.strip():
        return f"reject:{completed.stderr}", ""
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "error", f"exit {completed.returncode}, unparsable: {completed.stdout[:120]!r}"
    failures = [c["name"] for c in result["checks"] if c["status"] == "fail"]
    return (f"fail:{failures[0]}" if failures else "error", completed.stderr[:200])


def verify(files: dict[pathlib.Path, bytes]) -> list[str]:
    """Write the corpus to a temporary directory, run the reference verifier on it, and return problems."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="docket-vectors-") as tmp:
        root = pathlib.Path(tmp)
        for path, data in files.items():
            target = root / path.relative_to(HERE)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name, _base, _mutate, check, ledger, _rule, _mutation in VECTORS:
            receipt = root / f"{name}.json"
            ledger_path = (
                None if ledger is None else root / (LEDGER_NAME if ledger == BASE else f"ledgers/{name}.jsonl")
            )
            kind, detail = _first_failure(receipt, ledger_path)
            if check == LEDGER_REJECT:
                if not kind.startswith("reject:"):
                    problems.append(f"{name}: the malformed ledger was read; got {kind}")
                    continue
                reason = kind[len("reject:") :].strip()
                words = LEDGER_REJECT_WORDS[name]
                if not reason:
                    problems.append(f"{name}: the ledger was refused with no reason on stderr")
                elif words and not any(word.lower() in reason.lower() for word in words):
                    problems.append(f"{name}: refused, but the reason names none of {words}: {reason[:140]}")
                continue
            if check == ACCEPT:
                if kind != "pass":
                    problems.append(f"{name}: expected acceptance, got {kind} {detail}")
                continue
            if check is None:
                if not kind.startswith("reject:"):
                    problems.append(f"{name}: expected a parse rejection, got {kind}")
                    continue
                reason = kind[len("reject:") :].strip()
                words = REJECT_WORDS[name]
                if not reason:
                    problems.append(f"{name}: refused with no reason on stderr")
                elif words and not any(word.lower() in reason.lower() for word in words):
                    problems.append(f"{name}: rejected, but the reason names none of {words}: {reason[:140]}")
            elif kind != f"fail:{check}":
                problems.append(f"{name}: expected first failure {check!r}, got {kind} {detail}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="compare the working tree with a fresh build")
    args = parser.parse_args()
    files = build()
    problems = verify(files)
    if problems:
        print("the corpus does not say what it claims:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    if args.check:
        changed = [
            str(path.relative_to(REPO))
            for path, data in files.items()
            if not path.exists() or path.read_bytes() != data
        ]
        if changed:
            print("the shipped corpus differs from a fresh build:\n  " + "\n  ".join(changed), file=sys.stderr)
            return 1
        print(f"the corpus is current: {len(VECTORS)} vectors, every one failing for the stated reason")
        return 0
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    print(
        f"wrote {len(VECTORS)} vectors and {len(files) - len(VECTORS)} supporting files under {HERE.relative_to(REPO)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
