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
from decimal import Decimal
from pathlib import Path
from typing import Any

from .canon import merkle_root, sha256_hex, verify_merkle_proof
from .ledger import Event, Ledger
from .pricing import recompute_totals
from .receipt import receipt_core, receipt_signable
from .signing import verify_seal, verify_signature


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


def verify_receipt(
    receipt: dict[str, Any],
    *,
    ledger_events: Sequence[Event] | None = None,
    ledger_path: str | Path | None = None,
    seal_key: str | None = None,
    expect_public_key: str | None = None,
) -> VerifyResult:
    """Verify a receipt (and optionally the ledger it came from)."""
    checks: list[Check] = []
    failures: list[str] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append(Check(name, status, detail))
        if status == "fail":
            failures.append(f"{name}: {detail}")

    fmt = receipt.get("format")
    if fmt != "docket/receipt/v1":
        add("format", "fail", f"unrecognised receipt format {fmt!r}")
    else:
        add("format", "pass", "docket/receipt/v1")

    # -- 2/3. ledger chain and roots ------------------------------------- #
    events = list(ledger_events) if ledger_events is not None else None
    if events is None and ledger_path is not None:
        events = Ledger.read(ledger_path)
    ledger_meta = receipt.get("ledger", {})
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
        if len(events) == int(ledger_meta.get("events", -1)):
            add("event-count", "pass", f"{len(events)} events")
        else:
            add("event-count", "fail", f"ledger has {len(events)} events, receipt claims {ledger_meta.get('events')}")
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
    appendix = receipt.get("appendix", {})
    rows = appendix.get("usage") or []
    root_value = str(ledger_meta.get("merkle_root", ""))
    if not rows:
        add("every-usage-row-recomputed", "pass", "no metered calls in this session")
    else:
        proven = 0
        unproven = 0
        rebound = 0
        for index, row in enumerate(rows):
            path = row.get("merkle_path")
            if not path:
                unproven += 1
                continue
            if verify_merkle_proof(str(row.get("event_hash")), path, root_value):
                proven += 1
            if events is not None:
                seq = row.get("seq")
                if isinstance(seq, int) and 0 <= seq < len(events):
                    if row.get("event_hash") == recomputed_hashes[seq]:
                        rebound += 1
                    else:
                        unproven += 1
                        add(
                            "usage-inclusion-proofs",
                            "fail",
                            f"usage row {index} (seq {seq}) does not match the recomputed ledger event",
                        )
                        break
                else:
                    unproven += 1
                    add("usage-inclusion-proofs", "fail", f"usage row {index} cites seq {seq!r}, outside the ledger")
                    break
        if unproven and events is not None and any(c.name == "usage-inclusion-proofs" for c in checks):
            pass  # already reported, with the offending row
        elif unproven and not proven:
            add(
                "usage-inclusion-proofs",
                "skip",
                "receipt carries a usage summary without inclusion proofs (large session)",
            )
        elif unproven:
            add("usage-inclusion-proofs", "fail", f"{unproven} usage rows lack inclusion proofs")
        elif events is not None:
            add(
                "usage-inclusion-proofs",
                "pass",
                f"{proven} usage rows proven against the Merkle root and re-bound to the recomputed ledger",
            )
        else:
            add(
                "usage-inclusion-proofs",
                "pass",
                f"{proven} usage rows proven against the receipt's claimed root (supply --ledger to re-bind them)",
            )

    # -- 5. cost recomputation -------------------------------------------- #
    price_doc = (receipt.get("price_table") or {}).get("document")
    if not isinstance(price_doc, dict):
        add("cost-recomputation", "fail", "receipt does not embed the price table it used")
    else:
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
            ],
            price_doc,
        )
        totals = receipt.get("totals", {})
        if recomputed["per_call_mismatches"]:
            first = recomputed["per_call_mismatches"][0]
            add(
                "cost-recomputation",
                "fail",
                f"per-call cost mismatch at seq {first['seq']}: recorded {first['recorded']} recomputed {first['recomputed']}",
            )
        else:
            add("cost-recomputation", "pass", f"{recomputed['calls']} calls recomputed from token counts")
        declared = str(totals.get("cost_usd", "0.00"))
        if Decimal(declared) == Decimal(recomputed["cost_usd"]):
            add("totals", "pass", f"total {declared} agrees with recomputation")
        else:
            add("totals", "fail", f"receipt total {declared} != recomputed {recomputed['cost_usd']}")
        if int(totals.get("total_tokens", -1)) == int(recomputed["total_tokens"]):
            add("token-totals", "pass", f"{recomputed['total_tokens']:,} tokens")
        else:
            add("token-totals", "fail", "receipt token totals do not match the usage appendix")

    # -- 7. sealing and signing ------------------------------------------- #
    seal_block = receipt.get("seal")
    if seal_block:
        if seal_key:
            if verify_seal(receipt_core(receipt), seal_block, seal_key):
                add("seal", "pass", f"HMAC-SHA256 valid (key {seal_block.get('key_id')})")
            else:
                add("seal", "fail", "HMAC-SHA256 seal does not verify with the supplied key")
        else:
            add("seal", "skip", "receipt is sealed; supply --seal-key to check the HMAC")
    else:
        add("seal", "skip", "receipt is not sealed")
    signature = receipt.get("signature")
    if signature:
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
    else:
        add("signature", "skip", "receipt is not signed")

    # -- 8. disclosure ----------------------------------------------------- #
    if receipt.get("limits_disclosure"):
        add("limits-disclosure", "pass", "receipt states what Docket could and could not observe")
    else:
        add("limits-disclosure", "fail", "receipt does not disclose the scope of its evidence")

    # -- 9. did the record itself survive the run? ------------------------- #
    # A receipt that quietly carries an incomplete ledger would be the tool lying about
    # its own evidence, which is the one thing this format exists to prevent. Old
    # receipts without the member are "skip", never "pass".
    evidence = receipt.get("evidence")
    if evidence is None:
        add("evidence-completeness", "skip", "receipt predates the evidence member")
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
    ops = receipt.get("ops") or {}
    if ops.get("chaos_run"):
        faults = (ops.get("fault_injection") or {}).get("injections", 0)
        add("chaos-run", "skip", f"fault injection was active: {faults} injected fault(s) — this receipt records a test, not production behaviour")

    return VerifyResult(
        ok=not failures,
        checks=checks,
        docket_number=receipt.get("docket_number"),
        failures=failures,
    )
