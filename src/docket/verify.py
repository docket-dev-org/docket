"""Offline verification of receipts and ledgers.

``docket verify`` is the second half of the promise: the first half is that the
tool records; the second is that *someone else* can check the record without
trusting the tool, the operator or the vendor. Verification runs offline, against a
file, using nothing but the standard library.

Checks performed
----------------
1. format/version recognisable;
2. ledger chain: every event hash recomputed, every link checked, sequence dense;
3. head, event count and Merkle root agree with the receipt;
4. every usage row in the receipt's appendix is proven present in that Merkle tree
   by its inclusion path;
5. cost totals recomputed from raw token counts using the price table embedded in
   the receipt, compared call by call;
6. the ledger file's SHA-256 matches, when the ledger is supplied;
7. the seal (HMAC, needs the operator's key) and/or signature (Ed25519, needs only
   the public key in the receipt) verify, when present;
8. the response cache's saving: recomputed from the origin calls the hits replay and
   the embedded price table, with every hit group proven present in the ledger's
   Merkle tree, and — when the ledger is supplied — every hit resolving to the stored
   response it claims to replay.

Anything that cannot be checked is reported as *unchecked* with the reason, never
as passed. A verification tool that overstates its own coverage is worse than none.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .canon import merkle_root, sha256_hex, usd, verify_merkle_proof
from .latency import REPORTED, check_summary, is_latency
from .ledger import Event, Ledger
from .pricing import recompute_totals
from .receipt import receipt_core, receipt_signable
from .signing import verify_seal, verify_signature

#: Distinguishes an absent receipt member from one that is present and null.
_ABSENT = object()


@dataclass
class Check:
    name: str
    status: str  # pass | fail | skip
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class VerifyResult:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    docket_number: str | None = None
    failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "docket_number": self.docket_number,
            "checks": [c.to_dict() for c in self.checks],
            "failures": self.failures,
        }

    def render(self, color: bool = True) -> str:
        lines: list[str] = []
        for check in self.checks:
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[check.status]
            if color:
                mark = {
                    "pass": f"\033[32m{mark}\033[0m",
                    "fail": f"\033[31m{mark}\033[0m",
                    "skip": f"\033[33m{mark}\033[0m",
                }[check.status]
            lines.append(f"  {mark}  {check.name}: {check.detail}")
        verdict = "VERIFIED" if self.ok else "NOT VERIFIED"
        if color:
            verdict = f"\033[1;{'32' if self.ok else '31'}m{verdict}\033[0m"
        head = f"{verdict}  {self.docket_number or ''}".rstrip()
        return "\n".join([head] + lines)


def _amount(value: Any) -> Decimal:
    """Parse an amount out of a receipt, refusing the shapes that are not amounts.

    ``Decimal(True)`` is 1 and ``Decimal(float('nan'))`` is a trap, so booleans, floats
    and non-numeric strings are rejected here rather than silently coerced.

    Every refusal is a ``ValueError``. An unparsable string used to leave
    ``decimal.InvalidOperation`` to escape — which the one call site happens to catch, so it
    was invisible, but a helper whose contract is "raise ValueError for a non-amount" should
    not leak a second exception family depending on which *kind* of non-amount arrived. A
    ``Decimal`` is accepted because it is already exact and is the type this returns; a
    non-finite one is refused, because a NaN total compares false against everything and
    would be reported as a mismatch rather than as the junk it is.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError(f"not an amount: {value!r}")
    if isinstance(value, float):
        raise ValueError(f"amounts must not be floats: {value!r}")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"not a finite amount: {value!r}")
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("empty amount")
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"not an amount: {value!r}") from exc
        if not parsed.is_finite():
            raise ValueError(f"not a finite amount: {value!r}")
        return parsed
    raise ValueError(f"not an amount: {value!r} ({type(value).__name__})")


def verify_receipt(
    receipt: dict[str, Any],
    *,
    ledger_events: Sequence[Event] | None = None,
    ledger_path: str | Path | None = None,
    seal_key: str | None = None,
    expect_public_key: str | None = None,
) -> VerifyResult:
    """Verify a receipt (and optionally the ledger it came from).

    Total by contract: whoever asks for a verification chooses the file to hand over, so a
    document that is not a receipt at all is a *verdict* ("not a receipt: expected a JSON
    object, got list"), never a ``TypeError`` out of the first line that touches it.
    """
    checks: list[Check] = []
    failures: list[str] = []
    if not isinstance(receipt, dict):
        detail = f"not a receipt: expected a JSON object, got {type(receipt).__name__}"
        return VerifyResult(
            ok=False,
            checks=[Check("receipt-format", "fail", detail)],
            docket_number=None,
            failures=[f"receipt-format: {detail}"],
        )

    def add(name: str, status: str, detail: str) -> None:
        checks.append(Check(name, status, detail))
        if status == "fail":
            failures.append(f"{name}: {detail}")

    def section(name: str) -> dict[str, Any]:
        """A named member as an object, or ``{}`` plus a stated failure when it is not one.

        Every member of a receipt is written by nobody the verifier trusts, and the members the
        checks read are read as *objects*. Rather than repeating ``isinstance`` at twenty call
        sites (and forgetting one, which is how ``totals`` and ``evidence`` used to raise
        ``AttributeError`` out of a verifier), every read goes through here: a member that is the
        wrong shape becomes a named failure once, and the checks that depend on it report on an
        empty object rather than crashing.

        There is no "optional" variant: a member this verifier reads and a receipt does not carry
        is a *failure*, because the alternative — reporting nothing and returning ``{}`` — makes a
        stripped receipt look like a receipt that passed. Callers that can legitimately be absent
        (the seal, the signature, the appendix) read ``receipt.get(...)`` directly and say so in
        the check they record.
        """
        value = receipt.get(name)
        if value is None:
            add("receipt-shape", "fail", f"the receipt has no {name!r} member")
            return {}
        if isinstance(value, dict):
            return value
        add("receipt-shape", "fail", f"the receipt's {name} is not an object: {type(value).__name__}")
        return {}

    def whole(value: Any, where: str) -> int | None:
        """An integer from untrusted input, or ``None`` after stating why it is not one.

        An integral float is accepted and normalised, because spec §3.2 says exactly that: "A float
        with an integral value MUST be normalised to an integer (5.0 → 5)", and the sentence that
        follows it rejects only *non-integral* floats. This function used to refuse every float
        token, which made the Python verifier stricter than the format and put it at odds with the
        Node one on a document the spec says to read: the negative corpus's `34-integral-float-accepted`
        vector caught the disagreement, and the corpus refused to be written until it was settled.
        """
        if isinstance(value, bool) or value is None:
            add("receipt-shape", "fail", f"{where} is not a whole number: {value!r}")
            return None
        if isinstance(value, float):
            if not value.is_integer():
                add("receipt-shape", "fail", f"{where} is a fractional number: {value!r}")
                return None
            return int(value)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            add("receipt-shape", "fail", f"{where} is not a whole number: {value!r}")
            return None

    # A sweep of the members that must be objects, before any check reads one. Present-but-wrong
    # is a finding; absent is not (older receipts legitimately omit members, and their absence is
    # reported by the check that depends on them, as a "skip" with a reason). Doing this once, up
    # front, is what keeps a malformed member from surfacing as an AttributeError three checks
    # later, or — worse — as a check that silently reads an empty object and passes.
    for member in (
        "tool",
        "session",
        "policy",
        "ledger",
        "totals",
        "caps",
        "model_calls",
        "files",
        "network",
        "event_counts",
        "appendix",
        "price_table",
        "evidence",
        "ops",
        "limits_disclosure",
    ):
        if member in receipt and receipt[member] is not None and not isinstance(receipt[member], dict):
            add(
                "receipt-shape",
                "fail",
                f"the receipt's {member} is not an object: {type(receipt[member]).__name__}",
            )

    fmt = receipt.get("format")
    if fmt != "docket/receipt/v1":
        add("format", "fail", f"unrecognised receipt format {fmt!r}")
    else:
        add("format", "pass", "docket/receipt/v1")

    # -- 2/3. ledger chain and roots ------------------------------------- #
    events = list(ledger_events) if ledger_events is not None else None
    if events is None and ledger_path is not None:
        events = Ledger.read(ledger_path)
    ledger_meta = section("ledger")
    if events is None:
        add(
            "ledger-chain",
            "skip",
            "no ledger supplied: run `docket verify <receipt> --ledger <file>` to check the full chain",
        )
    else:
        chain_problem = Ledger(events=events).chain_error()
        # `chain_problem` is tested directly rather than through a `chain_ok` alias: the alias
        # was a bool, so the tuple was unreachable in the branch that unpacks it. An `assert`
        # would have narrowed it too, but an assert is removed under `python -O`, and a tool
        # whose whole claim is that it does not skip checks cannot rest one on that.
        if chain_problem is None:
            chain_ok = True
            add("ledger-chain", "pass", f"{len(events)} events, every hash and link recomputed")
        else:
            chain_ok = False
            seq, reason = chain_problem
            add("ledger-chain", "fail", f"broken at event #{seq}: {reason}")
        # Every downstream comparison uses *recomputed* hashes, never the hashes stored
        # in the file: a receipt issued over an intact ledger is unaffected, and a
        # ledger whose contents were edited after the fact fails here too instead of
        # passing because its stored hashes were left untouched.
        recomputed_hashes = [e.compute_hash() for e in events]
        head = recomputed_hashes[-1] if recomputed_hashes else None
        if not chain_ok:
            # A ledger edited mid-file can still end at a hash that matches the
            # receipt's, because a later event's own bytes were not rewritten.
            # Reporting "pass" here would be technically true and materially
            # misleading, so it is reported as unchecked.
            add("head", "skip", f"not checked: the chain is broken at event #{seq}")
        elif head == ledger_meta.get("head"):
            add("head", "pass", f"head matches receipt ({str(head)[:16]}…)")
        else:
            add(
                "head",
                "fail",
                f"recomputed head {str(head)[:16]}… does not match receipt {str(ledger_meta.get('head'))[:16]}…",
            )
        declared_events = whole(ledger_meta.get("events"), "the receipt's ledger.events")
        if declared_events is None:
            pass  # already reported as a shape failure
        elif len(events) == declared_events:
            add("event-count", "pass", f"{len(events)} events")
        else:
            add("event-count", "fail", f"ledger has {len(events)} events, receipt claims {declared_events}")
        root = merkle_root(recomputed_hashes)
        if root == ledger_meta.get("merkle_root"):
            add("merkle-root", "pass", f"{root[:16]}…")
        else:
            add(
                "merkle-root",
                "fail",
                f"recomputed {root[:16]}… != receipt {str(ledger_meta.get('merkle_root'))[:16]}…",
            )
        if ledger_path is not None:
            actual = sha256_hex(Path(ledger_path).read_bytes())
            if actual == ledger_meta.get("file_sha256"):
                add("ledger-file-digest", "pass", f"sha256 {actual[:16]}…")
            else:
                add("ledger-file-digest", "fail", "ledger file digest does not match the receipt")

    # -- 4. usage appendix inclusion proofs ------------------------------- #
    #
    # This is the check that carries the project's central claim: every metered call in the
    # receipt is *provably* one of the events the ledger committed to. It therefore has to be
    # strict in three directions at once.
    #
    # Two facts live here and they are reported under two names, because a check that means
    # two things cannot be read: ``usage-inclusion-proofs`` is the Merkle-path verdict (does the
    # row prove membership in the root the receipt claims?), and ``usage-rows-rebound`` is the
    # re-binding verdict (is that row the event the *recomputed* ledger has at that sequence?),
    # which only applies when a ledger is supplied. Reporting both under one name produced
    # output with "FAIL usage-inclusion-proofs" and "PASS usage-inclusion-proofs" four lines
    # apart — the worst kind of verifier output, since a reader skimming it sees whichever
    # verdict they expected.
    #
    # * A row whose proof does not verify is a failure even when no ledger is supplied. The
    #   earlier version incremented ``proven`` and never compared it with the number of rows, so
    #   a receipt with one proof edited — a single hex digit flipped — still reported
    #   "usage-inclusion-proofs: pass", with a detail line claiming every row was proven. A
    #   verifier that congratulates a doctored proof is worse than no verifier at all.
    # * A malformed proof (a path that is not a list of {side, hash} objects, a hash that is not
    #   hex) is a stated failure, not a traceback: the same "a verifier an attacker can switch
    #   off" rule as everywhere else.
    # * Everything about the appendix is untrusted, including its shape, so nothing below is
    #   reached without an ``isinstance`` check.
    appendix = receipt.get("appendix")
    rows: list[Any] = []
    appendix_ok = True
    if appendix is None:
        appendix = {}
    if not isinstance(appendix, dict):
        add(
            "usage-inclusion-proofs",
            "fail",
            f"the receipt's appendix is not an object: {type(appendix).__name__}",
        )
        appendix = {}
        appendix_ok = False
    raw_rows = appendix.get("usage")
    if raw_rows is None:
        raw_rows = []
    if not isinstance(raw_rows, list):
        add(
            "usage-inclusion-proofs",
            "fail",
            f"the receipt's appendix.usage is not a list: {type(raw_rows).__name__}",
        )
        raw_rows = []
        appendix_ok = False
    rows = list(raw_rows)
    root_value = str(ledger_meta.get("merkle_root", ""))
    if not rows and not appendix_ok:
        pass  # block 5 reports the unreadable appendix; there is nothing here to recompute
    elif not rows:
        # Nothing to account for. `every-usage-row-recomputed` used to be a check of its own here,
        # and *only* here: a receipt with rows never emitted it, so the id a consumer would reach for
        # to ask "was every row accounted for?" was absent in every case where the answer was not
        # already obvious. It is now part of `usage-inclusion-proofs`, whose assertion covers both
        # halves (a row is proven, or the session says plainly that proofs were omitted).
        add("usage-inclusion-proofs", "pass", "no metered calls in this session")
    else:
        proven = 0
        without_proof = 0
        proof_failures = 0
        rebound = 0
        rebind_failures = 0
        reported: dict[str, int] = {}

        def report_row(check: str, index: int, message: str) -> None:
            """Name the first few offending rows; summarise the rest without flooding output.

            The cap is per check, so a session with one bad proof and one bad re-binding shows
            the first three of each rather than spending the whole budget on one kind.
            """
            seen = reported.get(check, 0)
            reported[check] = seen + 1
            if seen < 3:
                add(check, "fail", message)
            elif seen == 3:
                add(check, "fail", f"further usage rows fail {check} the same way; see the counts below")

        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                proof_failures += 1
                report_row("usage-inclusion-proofs", index, f"usage row {index} is not an object: {type(row).__name__}")
                continue
            # `usage_source` is an enum, not free text (spec §6.1: "reported" | "estimated"), and a
            # consumer is required not to present estimated usage as metered. A value outside the
            # vocabulary used to pass every check: the row's arithmetic was still recomputed, so the
            # receipt verified while carrying a word no consumer could act on. The negative corpus
            # found it — `21-usage-source-unknown` was written as a vector and the verifier accepted
            # it, which is the point of writing the vector first.
            source = row.get("usage_source")
            if source not in ("reported", "estimated"):
                add(
                    "receipt-shape",
                    "fail",
                    f"usage row {index} has usage_source {source!r}; the vocabulary is 'reported' or 'estimated'",
                )
            path = row.get("merkle_path")
            if not path:
                without_proof += 1
            else:
                try:
                    if verify_merkle_proof(str(row.get("event_hash")), path, root_value):
                        proven += 1
                    else:
                        proof_failures += 1
                        report_row(
                            "usage-inclusion-proofs",
                            index,
                            f"usage row {index} does not prove against the receipt's Merkle root {root_value[:16]}…",
                        )
                except ValueError as exc:
                    proof_failures += 1
                    report_row(
                        "usage-inclusion-proofs", index, f"usage row {index} carries a malformed inclusion proof: {exc}"
                    )
            if events is not None:
                # A fresh name per row: `seq` is narrowed to `int` in the branch above, and mypy
                # would otherwise carry that narrowing into the next row's `Any | None`.
                row_seq = row.get("seq")
                if isinstance(row_seq, int) and not isinstance(row_seq, bool) and 0 <= row_seq < len(events):
                    if row.get("event_hash") == recomputed_hashes[row_seq]:
                        rebound += 1
                    else:
                        rebind_failures += 1
                        report_row(
                            "usage-rows-rebound",
                            index,
                            f"usage row {index} (seq {row_seq}) does not match the recomputed ledger event",
                        )
                else:
                    rebind_failures += 1
                    report_row(
                        "usage-rows-rebound", index, f"usage row {index} cites seq {row_seq!r}, outside the ledger"
                    )
        if proven == len(rows):
            add(
                "usage-inclusion-proofs",
                "pass",
                f"{proven} usage rows proven against the receipt's claimed Merkle root"
                + (" and re-bound to the recomputed ledger" if events is not None and not rebind_failures else ""),
            )
        elif without_proof == len(rows):
            # A large session ships a summary without per-row proofs by design (spec §6): say so,
            # rather than claiming an unproven row was proven.
            add(
                "usage-inclusion-proofs",
                "skip",
                "receipt carries a usage summary without inclusion proofs (large session)",
            )
        else:
            add(
                "usage-inclusion-proofs",
                "fail",
                f"{proof_failures + without_proof} of {len(rows)} usage rows are not proven "
                f"({proven} proven, {without_proof} without proofs, {proof_failures} failing their proof)",
            )
        if events is not None and rows:
            if rebind_failures:
                add(
                    "usage-rows-rebound",
                    "fail",
                    f"{rebind_failures} of {len(rows)} usage rows are not the events the recomputed ledger "
                    f"has at those sequence numbers ({rebound} re-bound)",
                )
            else:
                add(
                    "usage-rows-rebound",
                    "pass",
                    f"{rebound} usage rows re-bound to the recomputed ledger",
                )

    # -- 5. cost recomputation -------------------------------------------- #
    price_doc = section("price_table").get("document")
    if not appendix_ok:
        add(
            "cost-recomputation",
            "fail",
            "cannot recompute the cost: the receipt's usage appendix is not readable",
        )
    elif not isinstance(price_doc, dict):
        add("cost-recomputation", "fail", "receipt does not embed the price table it used")
    else:
        try:
            recomputed = recompute_totals(
                [
                    {
                        "seq": r.get("seq"),
                        "type": "model.response",
                        "data": {
                            "model": r.get("model"),
                            "usage": {
                                "input_tokens": r.get("input_tokens"),
                                "cached_input_tokens": r.get("cached_input_tokens"),
                                "output_tokens": r.get("output_tokens"),
                            },
                            "cost_usd": r.get("cost_usd"),
                        },
                    }
                    for r in rows
                    if isinstance(r, dict)
                ],
                price_doc,
            )
        except (ValueError, ArithmeticError, LookupError) as exc:
            # A receipt or a ledger arrives from whoever asked for the verification, so
            # every number in it is untrusted. A malformed one is a *finding*, stated
            # plainly; it must not raise, because a verifier that crashes on a hostile
            # receipt is a verifier an attacker can switch off. The price table itself is
            # untrusted too, so arithmetic escaping from it is caught here as well as being
            # refused inside pricing; this is the backstop, not the first line.
            add("cost-recomputation", "fail", f"cannot recompute the cost: {exc}")
            recomputed = None
        if recomputed is not None:
            if recomputed["per_call_mismatches"]:
                first = recomputed["per_call_mismatches"][0]
                add(
                    "cost-recomputation",
                    "fail",
                    f"per-call cost mismatch at seq {first['seq']}: recorded {first['recorded']} "
                    f"recomputed {first['recomputed']}",
                )
            else:
                add("cost-recomputation", "pass", f"{recomputed['calls']} calls recomputed from token counts")

            totals = section("totals")
            # One id, one verdict: the cost, the call count and the decimal amount are three
            # statements about the same claim — "this is what the rows add up to" — so they are
            # computed together and reported once. Emitting a second `totals` row with its own
            # status is what an earlier draft did, and `test_no_check_reports_two_verdicts` exists
            # because a report holding `FAIL totals` and `PASS totals` lets a reader see whichever
            # one they expected.
            totals_problems: list[str] = []
            declared_raw = totals.get("cost_usd", "0.00")
            declared_amount = None
            try:
                declared_amount = _amount(declared_raw)
            except (InvalidOperation, ValueError, TypeError):
                totals_problems.append(
                    f"receipt total is not a decimal amount: {declared_raw!r} ({type(declared_raw).__name__})"
                )
            calls_raw = totals.get("calls", -1)
            calls_declared = whole(calls_raw, "the receipt's totals.calls")
            if calls_declared is None:
                totals_problems.append(f"receipt call count is not a whole number: {calls_raw!r}")
            elif calls_declared != int(recomputed["calls"]):
                totals_problems.append(
                    f"receipt says {calls_declared} call(s); the usage appendix holds {recomputed['calls']}"
                )
            if declared_amount is not None and declared_amount != Decimal(recomputed["cost_usd"]):
                totals_problems.append(f"receipt total {declared_raw} != recomputed {recomputed['cost_usd']}")
            if totals_problems:
                add("totals", "fail", "; ".join(totals_problems))
            else:
                add(
                    "totals",
                    "pass",
                    f"{recomputed['calls']} call(s), total {declared_raw} agrees with recomputation",
                )

            # Three counts, one assertion: the halves and the sum. Comparing only the sum lets a
            # receipt move 1,000 tokens from input to output and still verify, which is a claim about
            # what the model was asked and what it said — exactly the shape of a fabricated usage
            # record. Found by probing the member field by field rather than by reading the check.
            declared_counts = {
                "input_tokens": totals.get("input_tokens", -1),
                "output_tokens": totals.get("output_tokens", -1),
                "total_tokens": totals.get("total_tokens", -1),
            }
            declared_whole: dict[str, int] = {}
            for name, raw in declared_counts.items():
                value = whole(raw, f"the receipt's totals.{name}")
                if value is not None:
                    declared_whole[name] = value
            if len(declared_whole) != len(declared_counts):
                add("token-totals", "fail", "a receipt token total is not a whole number")
            else:
                wrong = [name for name, value in declared_whole.items() if value != int(recomputed[name])]
                if not wrong:
                    add(
                        "token-totals",
                        "pass",
                        f"{recomputed['input_tokens']:,} in + {recomputed['output_tokens']:,} out "
                        f"= {recomputed['total_tokens']:,} tokens",
                    )
                else:
                    add(
                        "token-totals",
                        "fail",
                        "receipt token totals do not match the usage appendix: "
                        + ", ".join(f"{name} {declared_whole[name]:,} vs {int(recomputed[name]):,}" for name in wrong),
                    )

            # The Node verifier has always checked the cached half of the token total and the Python
            # one never did: found by asking each implementation which ids it emits across the whole
            # corpus, rather than by reading either one. A receipt can carry a correct total and a
            # wrong cached subtotal, and the subtotal is what a saving is computed from.
            if "cached_input_tokens" in totals:
                cached_raw = totals.get("cached_input_tokens")
                cached_declared = whole(cached_raw, "the receipt's totals.cached_input_tokens")
                if cached_declared is None:
                    add("cached-totals", "fail", f"receipt cached total is not a whole number: {cached_raw!r}")
                elif cached_declared == int(recomputed["cached_input_tokens"]):
                    add("cached-totals", "pass", f"{recomputed['cached_input_tokens']:,} cached tokens")
                else:
                    add("cached-totals", "fail", "receipt cached totals do not match the usage appendix")

            # `assumed-prices` is advisory, not a pass/fail on the arithmetic: a fallback price is a
            # guess, and the format's rule is not "never guess" (a session with an unlisted model
            # still has to be priced) but "never guess silently". The Node verifier emitted a `pass`
            # for every assumption it noticed, which reads as "the receipt is fine" when it is the
            # receipt's *disclosure* that is in question; the registry fixes the meaning, and this is
            # the Python side of the same rule.
            assumed = [str(m) for m in recomputed.get("assumed_models", [])]
            disclosed = totals.get("assumed_price_models")
            disclosed_names = {str(m) for m in disclosed} if isinstance(disclosed, list) else set()
            if not assumed:
                add("assumed-prices", "pass", "every model matched a price entry")
            elif set(assumed) <= disclosed_names:
                add("assumed-prices", "pass", f"priced by assumption and disclosed: {', '.join(assumed)}")
            else:
                undisclosed = [m for m in assumed if m not in disclosed_names]
                add(
                    "assumed-prices",
                    "fail",
                    f"priced by assumption but not disclosed in totals.assumed_price_models: {', '.join(undisclosed)}",
                )

            # `totals.by_model`, `totals.usage_reported_calls` / `usage_estimated_calls` /
            # `asserted_calls` and `totals.cost_basis` are *derived*: the receipt states them so a
            # reader can see where the money went without re-deriving it. None of them was checked,
            # so all of them could be rewritten freely on a receipt that still verified — a receipt
            # that names a model that was never called, or claims every row was reported when half
            # were estimated, which §6.1 says a consumer MUST NOT present as metered. They are
            # recomputable from the same rows item 5 already recomputes the total from.
            def quiet_whole(value: Any) -> int | None:
                """The same acceptance as `whole`, without emitting a second id.

                `whole` speaks for itself under `receipt-shape`. Here a value that is not a whole
                number is a problem with *this* breakdown, and saying so twice — once under a shape
                id and once under `by-model-totals` — is what "one id, one verdict" forbids.
                """
                if isinstance(value, bool) or value is None:
                    return None
                if isinstance(value, float):
                    return int(value) if value.is_integer() else None
                try:
                    return int(str(value).strip())
                except (TypeError, ValueError):
                    return None

            model_sums: dict[str, dict[str, Any]] = {}
            source_counts = {"reported": 0, "estimated": 0, "asserted": 0}
            try:
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("model") or "unknown")
                    slot = model_sums.setdefault(
                        name,
                        {
                            "calls": 0,
                            "input_tokens": 0,
                            "cached_input_tokens": 0,
                            "output_tokens": 0,
                            "cost": Decimal("0"),
                        },
                    )
                    slot["calls"] += 1
                    for member in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        slot[member] += int(row.get(member) or 0)
                    slot["cost"] += Decimal(str(row.get("cost_usd") or "0"))
                    if row.get("usage_source") == "reported":
                        source_counts["reported"] += 1
                    elif row.get("usage_source") == "estimated":
                        source_counts["estimated"] += 1
                    if row.get("asserted_by") == "agent":
                        source_counts["asserted"] += 1
            except (TypeError, ValueError, ArithmeticError) as exc:
                add("by-model-totals", "fail", f"cannot recompute the per-model breakdown: {exc}")
                add("usage-source-counts", "fail", f"cannot recount the usage sources: {exc}")
                model_sums = {}
                breakdown_judged = True
            else:
                breakdown_judged = False

            # Judged even when the appendix holds no rows: a session with no metered calls must
            # state an empty breakdown, and "nothing to compare" is not a pass. Emitting nothing
            # here is how a receipt with no rows could carry any `by_model` it liked — the Node
            # verifier compared it either way, which is how the two disagreed on an empty session.
            if not breakdown_judged:
                declared_models = totals.get("by_model")
                if not isinstance(declared_models, list):
                    add(
                        "by-model-totals",
                        "fail",
                        f"the receipt states no per-model breakdown: totals.by_model is {type(declared_models).__name__}",
                    )
                else:
                    stated: dict[str, dict[str, Any]] = {}
                    problems: list[str] = []
                    for index, entry in enumerate(declared_models):
                        if not isinstance(entry, dict):
                            problems.append(f"by_model[{index}] is not an object")
                            continue
                        name = str(entry.get("model") or "unknown")
                        if name in stated:
                            # Two rows for one model: a reader summing them gets a number no row
                            # claimed, and a reader taking the first gets a number the receipt does
                            # not mean. The same rule as the cache's `by_origin` groups.
                            problems.append(f"by_model names {name!r} twice")
                        stated[name] = entry
                    for name in sorted(set(stated) - set(model_sums)):
                        problems.append(f"by_model names {name!r}, which no usage row used")
                    for name in sorted(set(model_sums) - set(stated)):
                        problems.append(f"by_model omits {name!r}, which {model_sums[name]['calls']} usage row(s) used")
                    for name in sorted(set(stated) & set(model_sums)):
                        entry = stated[name]
                        expected = model_sums[name]
                        for member, want in (
                            ("calls", expected["calls"]),
                            ("input_tokens", expected["input_tokens"]),
                            ("cached_input_tokens", expected["cached_input_tokens"]),
                            ("output_tokens", expected["output_tokens"]),
                        ):
                            if member not in entry:
                                problems.append(f"by_model[{name!r}] omits {member}")
                                continue
                            got = quiet_whole(entry.get(member))
                            if got is None:
                                problems.append(f"by_model[{name!r}].{member} is not a whole number")
                            elif got != want:
                                problems.append(f"by_model[{name!r}].{member} says {got}, the rows sum to {want}")
                        if "cost_usd" not in entry:
                            problems.append(f"by_model[{name!r}] omits cost_usd")
                        else:
                            try:
                                if _amount(entry.get("cost_usd")) != expected["cost"]:
                                    problems.append(
                                        f"by_model[{name!r}].cost_usd says {entry.get('cost_usd')}, "
                                        f"the rows sum to {usd(expected['cost'])}"
                                    )
                            except (InvalidOperation, ValueError, TypeError):
                                problems.append(f"by_model[{name!r}].cost_usd is not a decimal amount")
                    if problems:
                        add("by-model-totals", "fail", "; ".join(problems[:4]))
                    else:
                        add(
                            "by-model-totals",
                            "pass",
                            f"{len(model_sums)} model(s), every count and amount summing to the usage rows",
                        )

            declared_counts = {
                "usage_reported_calls": (totals.get("usage_reported_calls", -1), source_counts["reported"]),
                "usage_estimated_calls": (totals.get("usage_estimated_calls", -1), source_counts["estimated"]),
                "asserted_calls": (totals.get("asserted_calls", -1), source_counts["asserted"]),
            }
            count_problems: list[str] = []
            for name, (raw, want) in declared_counts.items():
                got = quiet_whole(raw)
                if got is None:
                    count_problems.append(f"totals.{name} is not a whole number")
                elif got != want:
                    count_problems.append(f"totals.{name} says {got}; the rows hold {want}")
            if count_problems:
                add("usage-source-counts", "fail", "; ".join(count_problems))
            else:
                add(
                    "usage-source-counts",
                    "pass",
                    f"{source_counts['reported']} reported, {source_counts['estimated']} estimated, "
                    f"{source_counts['asserted']} asserted by the agent",
                )

            # `cost_basis` is a human summary of exactly those counts, so the only thing worth
            # checking is that it does not contradict them. The leading word names the class;
            # a receipt that says "reported" while rows are estimated is claiming metered usage
            # for numbers the provider never reported (§6.1), and that is a contradiction however
            # the rest of the sentence reads. `quota mode` cannot be checked at all: the meter's
            # mode is a fact about the run that the receipt does not state anywhere else, so a
            # verifier that accepted it would be accepting an assertion, and one that rejected it
            # would reject every subscription run. It is reported as not checked, which is what it
            # is. Not a prose match: any wording is accepted as long as the class agrees.
            basis_text = totals.get("cost_basis")
            if not isinstance(basis_text, str) or not basis_text.strip():
                add("cost-basis", "fail", f"totals.cost_basis is not a sentence: {basis_text!r}")
            else:
                head = basis_text.strip().split(":", 1)[0].strip().lower()
                reported_n, estimated_n = source_counts["reported"], source_counts["estimated"]
                if head == "quota mode":
                    add(
                        "cost-basis",
                        "skip",
                        "the run claims subscription (quota) pricing, which the receipt states nowhere else",
                    )
                elif head == "mixed" and not (reported_n and estimated_n):
                    add(
                        "cost-basis",
                        "fail",
                        f"cost_basis says mixed, but the rows hold {reported_n} reported and {estimated_n} estimated call(s)",
                    )
                elif head == "reported" and estimated_n:
                    add(
                        "cost-basis",
                        "fail",
                        f"cost_basis says reported, but {estimated_n} row(s) are estimated — usage the provider never reported",
                    )
                elif head == "estimated" and reported_n:
                    add(
                        "cost-basis",
                        "fail",
                        f"cost_basis says estimated, but {reported_n} row(s) were reported by the provider",
                    )
                elif head not in {"mixed", "reported", "estimated"}:
                    add(
                        "cost-basis",
                        "fail",
                        f"cost_basis names no usage class this format defines: {basis_text[:60]!r}",
                    )
                else:
                    add("cost-basis", "pass", f"{head} basis agrees with the usage rows")

    # -- 6. the latency summary ------------------------------------------- #
    # Every other number in a receipt is arithmetic that a verifier can redo from first
    # principles. Latency is the exception: it is a *measurement* taken around a call, so the
    # only honest thing a verifier can do with it is check that the summary is the summary of
    # the rows beside it — the same rows that are already bound to the Merkle tree. A summary
    # that disagrees with its own rows is a fabricated measurement inside a tamper-evident
    # document, which is worse than no measurement at all.
    # Absent and present-but-null are different statements: an absent member says "nothing was
    # timed", while `"latency": null` is a malformed claim in a document that is supposed to be
    # checkable. The sentinel keeps the distinction, so the second one fails instead of skipping.
    stated_latency = receipt.get("latency", _ABSENT)
    if stated_latency is _ABSENT:
        add("latency-summary", "skip", "the receipt carries no per-call latency")
    elif not appendix_ok:
        add(
            "latency-summary",
            "fail",
            "cannot recompute the latency summary: the receipt's usage appendix is not readable",
        )
    else:
        malformed = [
            row.get("latency_ms")
            for row in rows
            if isinstance(row, dict) and "latency_ms" in row and not is_latency(row.get("latency_ms"))
        ]
        if malformed:
            add(
                "latency-summary",
                "fail",
                f"a usage row carries a latency that is not a whole, non-negative millisecond count: "
                f"{malformed[0]!r} ({type(malformed[0]).__name__})",
            )
        else:
            measured = [
                row["latency_ms"] for row in rows if isinstance(row, dict) and is_latency(row.get("latency_ms"))
            ]
            problems = check_summary(stated_latency, measured, len(rows))
            if problems:
                add("latency-summary", "fail", "; ".join(problems[:3]))
            else:
                add(
                    "latency-summary",
                    "pass",
                    f"p50 {measured and stated_latency.get('p50_ms')} ms · "
                    f"p{REPORTED[-1]} {stated_latency.get(f'p{REPORTED[-1]}_ms')} ms over "
                    f"{len(measured)} measured call(s), nearest-rank",
                )

    # -- 6b. the response cache, when the run had one --------------------- #
    # A saving is the easiest number in a receipt to overstate, because nothing about
    # the spend contradicts it: a run that spent $4 and claims to have saved $400 looks
    # exactly like a run that spent $4. So the claim is checked the way every other
    # number here is — arithmetic, redone from raw counts and the embedded price table.
    #
    # What is recomputed: each group's per-hit amount against the origin row it names
    # (a metered call, so it is in `appendix.usage` and already proven against the root),
    # and the totals against the sum of the groups. What is *checked* rather than
    # recomputed: how many times each origin was replayed. A hit is an event, not
    # arithmetic, so the count is a measurement — bound to the ledger by the inclusion
    # proof carried on each group, and compared with the ledger's own events when the
    # ledger is supplied. Absent member: this run had no cache, and that is a skip with
    # the reason, never a pass.
    stated_cache = receipt.get("cache", _ABSENT)
    if stated_cache is _ABSENT:
        add("cache-savings", "skip", "this run had no response cache: every call was forwarded")
    elif not isinstance(stated_cache, dict):
        add(
            "cache-savings",
            "fail",
            f"the receipt's cache member is not an object: {type(stated_cache).__name__}",
        )
    else:
        groups_raw = stated_cache.get("by_origin")
        savings_problems: list[str] = []
        origin_problems: list[str] = []
        proof_failures = 0
        proven = 0
        without_proof = 0
        total_hits = 0
        saved_total = Decimal("0")
        avoided_tokens_total = 0
        if not isinstance(groups_raw, list):
            add(
                "cache-savings",
                "fail",
                f"the receipt's cache.by_origin is not a list: {type(groups_raw).__name__}",
            )
            groups_raw = []
        rows_by_seq: dict[Any, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_seq = row.get("seq")
            if isinstance(row_seq, int) and not isinstance(row_seq, bool):
                rows_by_seq[row_seq] = row
        for index, group in enumerate(groups_raw):
            if not isinstance(group, dict):
                savings_problems.append(f"cache group {index} is not an object: {type(group).__name__}")
                continue
            hits = whole(group.get("hits"), f"cache group {index} hits")
            usage = group.get("usage")
            key = group.get("key")
            model = group.get("model")
            origin_seq = whole(group.get("origin_seq"), f"cache group {index} origin_seq")
            if hits is None or hits < 1:
                savings_problems.append(
                    f"cache group {index} does not say how many hits it counts: {group.get('hits')!r}"
                )
                continue
            if not isinstance(key, str) or not key:
                savings_problems.append(f"cache group {index} carries no cache key")
            if not isinstance(usage, dict):
                savings_problems.append(f"cache group {index} carries no usage object")
                continue
            total_hits += hits
            avoided_tokens_total += (int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)) * hits
            # -- the origin: a metered call this receipt already proves ------ #
            if origin_seq is None:
                origin_problems.append(
                    f"cache group {index} does not cite the call it replays: origin_seq {group.get('origin_seq')!r}"
                )
            else:
                row = rows_by_seq.get(origin_seq)
                if row is None:
                    origin_problems.append(
                        f"cache group {index} cites origin seq {origin_seq}, which is not a metered call "
                        "in this receipt's appendix"
                    )
                else:
                    if row.get("cache_key") != key:
                        origin_problems.append(
                            f"cache group {index}: the call at seq {origin_seq} is not recorded as a "
                            "cached response with this key"
                        )
                    if row.get("model") != model:
                        origin_problems.append(
                            f"cache group {index}: replayed model {model!r} but seq {origin_seq} returned "
                            f"{row.get('model')!r}"
                        )
                    for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                        if int(usage.get(field) or 0) != int(row.get(field) or 0):
                            origin_problems.append(
                                f"cache group {index}: {field} {usage.get(field)!r} does not match the call "
                                f"at seq {origin_seq} ({row.get(field)!r})"
                            )
            # -- the money: the same arithmetic the totals use -------------- #
            if not isinstance(price_doc, dict):
                savings_problems.append(
                    f"cache group {index} cannot be priced: the receipt does not embed the price table it used"
                )
            else:
                try:
                    recomputed_group = recompute_totals(
                        [
                            {
                                "seq": origin_seq,
                                "type": "model.response",
                                "data": {
                                    "model": model,
                                    "usage": {
                                        "input_tokens": usage.get("input_tokens"),
                                        "cached_input_tokens": usage.get("cached_input_tokens"),
                                        "output_tokens": usage.get("output_tokens"),
                                    },
                                },
                            }
                        ],
                        price_doc,
                    )
                except (ValueError, ArithmeticError, LookupError) as exc:
                    savings_problems.append(f"cache group {index} cannot be priced: {exc}")
                else:
                    per_hit = recomputed_group["cost_usd"]
                    if str(group.get("avoided_usd_per_hit")) != per_hit:
                        savings_problems.append(
                            f"cache group {index} claims {group.get('avoided_usd_per_hit')!r} saved per hit; "
                            f"the price table says {per_hit}"
                        )
                    saved_total += Decimal(per_hit) * hits
            # -- the proof: one row per group, pinned to the tree ------------ #
            path_value = group.get("merkle_path")
            if not path_value:
                without_proof += 1
            else:
                try:
                    if verify_merkle_proof(str(group.get("event_hash")), path_value, root_value):
                        proven += 1
                    else:
                        proof_failures += 1
                except ValueError as exc:
                    proof_failures += 1
                    savings_problems.append(f"cache group {index} carries a malformed inclusion proof: {exc}")
        declared_hits = whole(stated_cache.get("hits"), "the receipt's cache.hits")
        if isinstance(groups_raw, list):
            if declared_hits is None:
                savings_problems.append(f"the receipt's cache.hits is not a whole number: {stated_cache.get('hits')!r}")
            elif declared_hits != total_hits:
                savings_problems.append(
                    f"cache.hits is {declared_hits}, but the groups below it add up to {total_hits}"
                )
            declared_saved = stated_cache.get("saved_usd")
            try:
                declared_amount = _amount(declared_saved)
            except (InvalidOperation, ValueError, TypeError):
                savings_problems.append(
                    f"the receipt's cache.saved_usd is not a decimal amount: {declared_saved!r} "
                    f"({type(declared_saved).__name__})"
                )
            else:
                if declared_amount != Decimal(usd(saved_total)):
                    savings_problems.append(
                        f"cache.saved_usd is {declared_saved}, but the groups recompute to {usd(saved_total)}"
                    )
            declared_tokens = whole(stated_cache.get("avoided_tokens"), "the receipt's cache.avoided_tokens")
            if declared_tokens is None:
                savings_problems.append(
                    f"the receipt's cache.avoided_tokens is not a whole number: {stated_cache.get('avoided_tokens')!r}"
                )
            elif declared_tokens != avoided_tokens_total:
                savings_problems.append(
                    f"cache.avoided_tokens is {declared_tokens}, but the groups add up to {avoided_tokens_total}"
                )
        if savings_problems:
            for problem in savings_problems[:3]:
                add("cache-savings", "fail", problem)
            if len(savings_problems) > 3:
                add("cache-savings", "fail", f"further cache savings problems: {len(savings_problems) - 3} more")
        else:
            add(
                "cache-savings",
                "pass",
                f"{total_hits} hit(s) recomputed at the origin's price: {usd(saved_total)} not spent",
            )
        if origin_problems:
            for problem in origin_problems[:3]:
                add("cache-hit-origins", "fail", problem)
            if len(origin_problems) > 3:
                add("cache-hit-origins", "fail", f"further origin problems: {len(origin_problems) - 3} more")
        elif total_hits:
            add("cache-hit-origins", "pass", "every hit resolves to the stored response it replays")
        if groups_raw and without_proof == len(groups_raw):
            add(
                "cache-hit-proofs",
                "skip",
                "receipt carries a cache summary without inclusion proofs (large session)",
            )
        elif groups_raw:
            if proof_failures:
                add(
                    "cache-hit-proofs",
                    "fail",
                    f"{proof_failures} cache group(s) do not prove against the receipt's Merkle root "
                    f"{root_value[:16]}…",
                )
            else:
                add(
                    "cache-hit-proofs",
                    "pass",
                    f"{proven} cache group(s) proven against the receipt's claimed Merkle root",
                )

        # -- and the events themselves, when the ledger is here ------------- #
        if events is not None:
            hit_events = [e for e in events if getattr(e, "type", None) == "model.cache_hit"]
            if len(hit_events) != total_hits:
                add(
                    "cache-savings",
                    "fail",
                    f"the receipt claims {total_hits} cache hit(s); the ledger records {len(hit_events)}",
                )
            tally: dict[tuple[Any, ...], int] = {}
            event_problems: list[str] = []
            for event in hit_events:
                data = event.data if isinstance(event.data, dict) else {}
                _raw = data.get("origin")
                origin = _raw if isinstance(_raw, dict) else {}
                _raw = data.get("usage")
                usage = _raw if isinstance(_raw, dict) else {}
                identity = (
                    origin.get("seq"),
                    origin.get("hash"),
                    data.get("key"),
                    data.get("model"),
                    usage.get("input_tokens"),
                    usage.get("cached_input_tokens"),
                    usage.get("output_tokens"),
                    str(data.get("avoided_cost_usd") or ""),
                )
                tally[identity] = tally.get(identity, 0) + 1
                # A fresh name: `seq` is already bound in this function by the chain-problem
                # unpacking above, where it is an `int`. This one comes out of a ledger record,
                # so it is `Any` until the isinstance below narrows it.
                hit_origin_seq = origin.get("seq")
                if (
                    not isinstance(hit_origin_seq, int)
                    or isinstance(hit_origin_seq, bool)
                    or not 0 <= hit_origin_seq < len(events)
                ):
                    event_problems.append(
                        f"the hit at seq {event.seq} cites origin seq {hit_origin_seq!r}, "
                        f"which is not an event in this ledger"
                    )
                    continue
                origin_event = events[hit_origin_seq]
                if getattr(origin_event, "type", "") != "model.response":
                    event_problems.append(
                        f"the hit at seq {event.seq} replays seq {hit_origin_seq}, which is a "
                        f"{getattr(origin_event, 'type', '?')} event, not a response"
                    )
                    continue
                origin_data = origin_event.data if isinstance(origin_event.data, dict) else {}
                _raw = origin_data.get("cache")
                meta = _raw if isinstance(_raw, dict) else {}
                if meta.get("stored") is not True:
                    event_problems.append(
                        f"the hit at seq {event.seq} replays seq {hit_origin_seq}, which was not "
                        f"recorded as a stored response"
                    )
                if meta.get("key") != data.get("key"):
                    event_problems.append(
                        f"the hit at seq {event.seq} carries key {str(data.get('key'))[:12]}…, but the response "
                        f"at seq {hit_origin_seq} was stored under {str(meta.get('key'))[:12]}…"
                    )
                if origin.get("hash") != getattr(origin_event, "hash", None):
                    event_problems.append(
                        f"the hit at seq {event.seq} cites origin hash {str(origin.get('hash'))[:12]}…, but the "
                        f"event at seq {hit_origin_seq} hashes to "
                        f"{str(getattr(origin_event, 'hash', ''))[:12]}…"
                    )
                _raw = origin_data.get("usage")
                origin_usage = _raw if isinstance(_raw, dict) else {}
                for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                    if int(usage.get(field) or 0) != int(origin_usage.get(field) or 0):
                        event_problems.append(
                            f"the hit at seq {event.seq} reports {field} {usage.get(field)!r}; the response it "
                            f"replays recorded {origin_usage.get(field)!r}"
                        )
            if event_problems:
                for problem in event_problems[:3]:
                    add("cache-hit-origins", "fail", problem)
                if len(event_problems) > 3:
                    add("cache-hit-origins", "fail", f"further origin problems: {len(event_problems) - 3} more")
            for index, group in enumerate(groups_raw):
                if not isinstance(group, dict):
                    continue
                _raw = group.get("usage")
                usage = _raw if isinstance(_raw, dict) else {}
                identity = (
                    group.get("origin_seq"),
                    group.get("origin_hash"),
                    group.get("key"),
                    group.get("model"),
                    usage.get("input_tokens"),
                    usage.get("cached_input_tokens"),
                    usage.get("output_tokens"),
                    str(group.get("avoided_usd_per_hit") or ""),
                )
                if tally.get(identity, 0) != int(group.get("hits") or 0):
                    add(
                        "cache-hit-origins",
                        "fail",
                        f"cache group {index} claims {group.get('hits')} hit(s) replaying seq "
                        f"{group.get('origin_seq')}; the ledger records {tally.get(identity, 0)}",
                    )
            # The ceiling note is a ledger fact, so a receipt that claims the ceiling
            # was reached must have the note to show for it — and a ledger that has the
            # note must not be summarised as a run that never hit the ceiling.
            note_present = any(
                getattr(e, "type", None) == "note"
                and isinstance(e.data, dict)
                and e.data.get("event") == "cache.hits_ceiling_reached"
                for e in events
            )
            if bool(stated_cache.get("hits_ceiling_reached")) != note_present:
                add(
                    "cache-savings",
                    "fail",
                    "the receipt and the ledger disagree about whether the cache reached its hit ceiling"
                    + (
                        f" (receipt says {bool(stated_cache.get('hits_ceiling_reached'))}, ledger records the note)"
                        if note_present
                        else " (receipt says it was reached; no such note is in the ledger)"
                    ),
                )

    # -- 7. sealing and signing ------------------------------------------- #
    # Presence, not truthiness: a receipt carrying ``"signature": 0`` or ``"seal": {}`` has a
    # malformed signature member, and reporting "not signed" for it would be a small lie in the
    # direction of leniency. A member that is not an object is a stated failure.
    seal_block = receipt.get("seal")
    if seal_block is None:
        add("seal", "skip", "receipt is not sealed")
    elif not isinstance(seal_block, dict):
        add("seal", "fail", f"the receipt's seal is not an object: {type(seal_block).__name__}")
    elif not seal_key:
        add("seal", "skip", "receipt is sealed; supply --seal-key to check the HMAC")
    elif not seal_block:
        add("seal", "fail", "the receipt carries an empty seal object")
    else:
        try:
            sealed_ok = verify_seal(receipt_core(receipt), seal_block, seal_key)
        except ValueError as exc:
            add("seal", "fail", f"cannot check the seal: {exc}")
        else:
            if sealed_ok:
                add("seal", "pass", f"HMAC-SHA256 valid (key {seal_block.get('key_id')})")
            else:
                add("seal", "fail", "HMAC-SHA256 seal does not verify with the supplied key")
    signature = receipt.get("signature")
    if signature is None:
        add("signature", "skip", "receipt is not signed")
    elif not isinstance(signature, dict):
        add("signature", "fail", f"the receipt's signature is not an object: {type(signature).__name__}")
    elif not signature:
        add("signature", "fail", "the receipt carries an empty signature object")
    else:
        try:
            ok = verify_signature(receipt_signable(receipt), signature)
        except RuntimeError as exc:  # cryptography not installed
            add("signature", "skip", str(exc))
            ok = None
        if ok is True:
            key_note = ""
            if expect_public_key and expect_public_key.lower() != str(signature.get("public_key", "")).lower():
                ok = False
                key_note = " (public key does not match the expected signer)"
            add("signature", "pass" if ok else "fail", "Ed25519 signature valid" + key_note)
        elif ok is False:
            add("signature", "fail", "Ed25519 signature does not verify")

    # -- 8. disclosure ----------------------------------------------------- #
    disclosure = receipt.get("limits_disclosure")
    if isinstance(disclosure, dict) and disclosure:
        add("limits-disclosure", "pass", "receipt states what Docket could and could not observe")
    elif disclosure is None:
        add("limits-disclosure", "fail", "receipt does not disclose the scope of its evidence")
    else:
        add(
            "limits-disclosure",
            "fail",
            f"the receipt's limits_disclosure is not an object: {type(disclosure).__name__}",
        )

    # -- 9. did the record itself survive the run? ------------------------- #
    # A receipt that quietly carries an incomplete ledger would be the tool lying about
    # its own evidence, which is the one thing this format exists to prevent. Old
    # receipts without the member are "skip", never "pass".
    evidence = receipt.get("evidence")
    if evidence is None:
        add("evidence-completeness", "skip", "receipt predates the evidence member")
    elif not isinstance(evidence, dict):
        add(
            "evidence-completeness",
            "fail",
            f"the receipt's evidence is not an object: {type(evidence).__name__}",
        )
    elif evidence.get("ledger_complete") is False:
        add(
            "evidence-completeness",
            "fail",
            f"the ledger is incomplete: {evidence.get('unwritten_events', 0)} event(s) never reached disk, "
            f"first at seq {evidence.get('first_failing_seq')}",
        )
    else:
        add("evidence-completeness", "pass", "every event reached the ledger file")

    # -- 10. was this a chaos run? ----------------------------------------- #
    ops = receipt.get("ops")
    ops = ops if isinstance(ops, dict) else {}
    if receipt.get("ops") is not None and not isinstance(receipt.get("ops"), dict):
        add("receipt-shape", "fail", f"the receipt's ops is not an object: {type(receipt.get('ops')).__name__}")
    if ops.get("chaos_run"):
        fault_block = ops.get("fault_injection")
        faults = fault_block.get("injections", 0) if isinstance(fault_block, dict) else 0
        add(
            "chaos-run",
            "skip",
            f"fault injection was active: {faults} injected fault(s) — this receipt records a test, not production behaviour",
        )

    number = receipt.get("docket_number")
    return VerifyResult(
        ok=not failures,
        checks=checks,
        docket_number=number if isinstance(number, str) else None,
        failures=failures,
    )
