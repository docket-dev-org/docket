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
   the public key in the receipt) verify, when present.

Anything that cannot be checked is reported as *unchecked* with the reason, never
as passed. A verification tool that overstates its own coverage is worse than none.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .canon import merkle_root, sha256_hex, verify_merkle_proof
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
        """An integer from untrusted input, or ``None`` after stating why it is not one."""
        if isinstance(value, bool) or value is None:
            add("receipt-shape", "fail", f"{where} is not a whole number: {value!r}")
            return None
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
        chain_ok = chain_problem is None
        if chain_ok:
            add("ledger-chain", "pass", f"{len(events)} events, every hash and link recomputed")
        else:
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
            add("head", "skip", f"not checked: the chain is broken at event #{chain_problem[0]}")
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
        add("every-usage-row-recomputed", "pass", "no metered calls in this session")
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
                            f"usage row {index} does not prove against the receipt's Merkle root "
                            f"{root_value[:16]}…",
                        )
                except ValueError as exc:
                    proof_failures += 1
                    report_row(
                        "usage-inclusion-proofs", index, f"usage row {index} carries a malformed inclusion proof: {exc}"
                    )
            if events is not None:
                seq = row.get("seq")
                if isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq < len(events):
                    if row.get("event_hash") == recomputed_hashes[seq]:
                        rebound += 1
                    else:
                        rebind_failures += 1
                        report_row(
                            "usage-rows-rebound",
                            index,
                            f"usage row {index} (seq {seq}) does not match the recomputed ledger event",
                        )
                else:
                    rebind_failures += 1
                    report_row("usage-rows-rebound", index, f"usage row {index} cites seq {seq!r}, outside the ledger")
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
            declared_raw = totals.get("cost_usd", "0.00")
            try:
                declared = _amount(declared_raw)
            except (InvalidOperation, ValueError, TypeError):
                add(
                    "totals",
                    "fail",
                    f"receipt total is not a decimal amount: {declared_raw!r} ({type(declared_raw).__name__})",
                )
            else:
                if declared == Decimal(recomputed["cost_usd"]):
                    add("totals", "pass", f"total {declared_raw} agrees with recomputation")
                else:
                    add("totals", "fail", f"receipt total {declared_raw} != recomputed {recomputed['cost_usd']}")

            tokens_raw = totals.get("total_tokens", -1)
            tokens_declared = whole(tokens_raw, "the receipt's totals.total_tokens")
            if tokens_declared is None:
                add("token-totals", "fail", f"receipt token total is not a whole number: {tokens_raw!r}")
            else:
                if tokens_declared == int(recomputed["total_tokens"]):
                    add("token-totals", "pass", f"{recomputed['total_tokens']:,} tokens")
                else:
                    add("token-totals", "fail", "receipt token totals do not match the usage appendix")

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
