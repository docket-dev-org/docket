"""Receipts: the compact artifact you can hand to someone else.

A receipt has to answer three questions without the ledger, without the tool and
without the vendor:

1. *What happened?* — totals, model mix, files touched, denials, cap events.
2. *Is this record intact?* — every usage row carries the hash of the ledger event
   it came from plus a Merkle inclusion proof against the receipt's root, so the
   receipt is cryptographically bound to the full ledger without shipping it.
3. *Is the arithmetic right?* — the price table used is embedded, so the cost
   totals can be recomputed from raw token counts by anyone.

The result is a file of a few kilobytes that a reviewer, an auditor or a customer
can check offline. See ``docs/verification.md``.
"""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import events as ev
from .canon import digest_of, merkle_proof, usd
from .explain import analyse
from .latency import is_latency
from .latency import summary as latency_summary
from .ledger import FORMAT as LEDGER_FORMAT
from .ledger import GENESIS, Event, Ledger
from .policy import Policy
from .pricing import CostMeter, PriceTable
from .signing import seal as make_seal
from .signing import sign as make_signature
from .util import atomic_write, loads_json

FORMAT = "docket/receipt/v1"
TOOL_VERSION = "0.1.0"
MAX_PROVEN_USAGE_ROWS = 4000


def _usage_rows(events: list[Event], proofs: dict[int, list[dict[str, str]]] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.type != ev.MODEL_RESPONSE:
            continue
        data = event.data
        usage = data.get("usage") or {}
        row: dict[str, Any] = {
            "seq": event.seq,
            "model": data.get("model"),
            "provider": data.get("provider"),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "usage_source": data.get("usage_source", "reported"),
            "cost_usd": str(data.get("cost_usd", "0.00")),
            "event_hash": event.hash,
        }
        # Provenance of the numbers themselves: a row asserted by the agent under
        # supervision is a claim, not a boundary observation. A reader must be able to
        # tell the two apart, so the label travels with the row.
        # Latency is carried when the call was *measured* (the boundary and the SDK time their own
        # round trips; an agent-asserted event may not have one). `is_latency` refuses floats,
        # bools and negatives: a measurement that is not a whole, non-negative millisecond count
        # is not a measurement, and the row simply does not claim one.
        if is_latency(data.get("latency_ms")):
            row["latency_ms"] = int(data["latency_ms"])
        if data.get("reported_by") == "agent":
            row["asserted_by"] = "agent"
        if proofs is not None and event.seq in proofs:
            row["merkle_path"] = proofs[event.seq]
        rows.append(row)
    return rows


def _totals_from_rows(rows: list[dict[str, Any]], meter: CostMeter) -> dict[str, Any]:
    """Totals computed from the usage rows that verification will recompute.

    Deriving totals from the same data the verifier checks means the receipt cannot
    disagree with its own appendix — the failure mode being a receipt that reports $0.00
    while the appendix recomputes to $0.0042, which verification (correctly) refuses.
    Rows asserted by the agent are counted and labelled separately, because a claim is
    not an observation.
    """
    input_tokens = sum(int(row.get("input_tokens") or 0) for row in rows)
    cached_tokens = sum(int(row.get("cached_input_tokens") or 0) for row in rows)
    output_tokens = sum(int(row.get("output_tokens") or 0) for row in rows)
    cost = sum((Decimal(str(row.get("cost_usd") or "0")) for row in rows), Decimal("0"))
    reported = sum(1 for row in rows if str(row.get("usage_source")) == "reported")
    estimated = sum(1 for row in rows if str(row.get("usage_source")) == "estimated")
    asserted = sum(1 for row in rows if row.get("asserted_by") == "agent")

    by_model: dict[str, dict[str, Any]] = {}
    for row in rows:
        model = str(row.get("model") or "unknown")
        entry = by_model.setdefault(
            model,
            {
                "model": model,
                "calls": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": Decimal("0"),
            },
        )
        entry["calls"] += 1
        entry["input_tokens"] += int(row.get("input_tokens") or 0)
        entry["cached_input_tokens"] += int(row.get("cached_input_tokens") or 0)
        entry["output_tokens"] += int(row.get("output_tokens") or 0)
        entry["cost_usd"] += Decimal(str(row.get("cost_usd") or "0"))

    if meter.mode == "quota":
        basis = "quota mode: subscription usage is counted in tokens, not dollars"
    elif estimated and reported:
        basis = "mixed: reported and estimated usage"
    elif estimated:
        basis = "estimated: provider reported no usage"
    else:
        basis = "reported: provider usage fields"
    if asserted:
        basis += f"; {asserted} call(s) asserted by the agent, not observed at the boundary"

    ordered = sorted(by_model.values(), key=lambda item: -item["cost_usd"])
    for entry in ordered:
        entry["cost_usd"] = usd(entry["cost_usd"])
    return {
        "calls": len(rows),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": usd(cost),
        "cost_basis": basis,
        "usage_reported_calls": reported,
        "usage_estimated_calls": estimated,
        "asserted_calls": asserted,
        "assumed_price_models": sorted(meter.assumed_models),
        "by_model": ordered,
    }


def build_receipt(
    *,
    events: list[Event],
    ledger: Ledger,
    meter: CostMeter,
    policy: Policy,
    session_id: str,
    docket_number: str,
    exit_code: int,
    duration_seconds: float,
    price_table: PriceTable,
    proxy_stats: Any | None = None,
    snapshot: dict[str, Any] | None = None,
    rollback: dict[str, Any] | None = None,
    seal_key: str | None = None,
    sign_key: str | Path | None = None,
    label: str | None = None,
    created_at: str | None = None,
    breaker: Any | None = None,
    limiter: Any | None = None,
    injector: Any | None = None,
    health: Any | None = None,
) -> dict[str, Any]:
    """Assemble a receipt from a finished session.

    ``created_at`` is injectable so that conformance vectors can be byte-stable.
    """
    hashes = [e.hash for e in events]
    proof_map: dict[int, list[dict[str, str]]] = {}
    if len(events) <= MAX_PROVEN_USAGE_ROWS:
        for index, event in enumerate(events):
            if event.type == ev.MODEL_RESPONSE:
                proof_map[event.seq] = merkle_proof(hashes, index)

    usage_rows = _usage_rows(events, proof_map)
    counts = ledger.types()
    findings = analyse(events, price_table=price_table, policy=policy)

    files = [e.data for e in events if e.type == ev.FS_CHANGE]
    file_summary = {
        "changed": len(files),
        "added": sum(1 for f in files if f.get("change") == "added"),
        "modified": sum(1 for f in files if f.get("change") == "modified"),
        "removed": sum(1 for f in files if f.get("change") == "removed"),
        "violations": sum(
            1 for e in events if e.type == ev.POLICY_VIOLATION and e.data.get("rule") == "files.deny_write"
        ),
        "top_paths": [f.get("path") for f in files[:25]],
    }

    by_provider: dict[str, int] = {}
    for event in events:
        if event.type == ev.MODEL_RESPONSE:
            provider = str(event.data.get("provider", "unknown"))
            by_provider[provider] = by_provider.get(provider, 0) + 1

    tools = [e.data for e in events if e.type == ev.TOOL_CALL]
    tool_names = sorted({str(t.get("name", "unknown")) for t in tools})
    tool_failures = sum(1 for e in events if e.type == ev.TOOL_RESULT and e.data.get("ok") is False)

    session_start = next((e.data for e in events if e.type == ev.SESSION_START), {})
    session_end = next((e.data for e in events if e.type == ev.SESSION_END), {})

    # -- evidence integrity: did the record survive the run? ---------------- #
    # A receipt is a claim about a run. If the ledger could not be written while the run
    # happened, the claim has a hole in it, and the hole belongs in the claim.
    evidence: dict[str, Any] = {
        "ledger_complete": not getattr(ledger, "unwritten", []),
        "unwritten_events": len(getattr(ledger, "unwritten", [])),
        "first_failing_seq": (ledger.unwritten[0]["seq"] if getattr(ledger, "unwritten", []) else None),
        "write_failures": len(getattr(ledger, "unwritten", [])),
        "fail_fast": bool(getattr(getattr(ledger, "guard", None), "fail_fast", False)),
        "policy_limits": {
            "max_events": getattr(ledger, "max_events", None),
            "max_ledger_bytes": getattr(ledger, "max_bytes", None),
        },
        "note": (
            "every event reached the ledger file"
            if not getattr(ledger, "unwritten", [])
            else "the ledger is an incomplete record; the chain will not verify past the first gap"
        ),
    }

    # -- failure behaviour: what the boundary did under stress -------------- #
    ops: dict[str, Any] = {
        "circuit_breaker": breaker.snapshot() if breaker is not None else None,
        "rate_limiter": limiter.snapshot() if limiter is not None and limiter.configured else None,
        "health": health.to_dict() if health is not None else None,
        "proxy": {
            "model_calls": getattr(proxy_stats, "model_calls", None),
            "model_errors": getattr(proxy_stats, "model_errors", None),
            "denied": getattr(proxy_stats, "denied", None),
        }
        if proxy_stats is not None
        else None,
        "fault_injection": injector.snapshot() if injector is not None and injector.enabled else None,
        "chaos_run": bool(injector is not None and injector.enabled),
        # Rollback is a claim about the working tree, so the receipt states it in full: what was
        # captured, what was restored, and — most importantly — which changed paths the snapshot
        # could not cover. A receipt that said only "rolled back: true" would be exactly the kind
        # of unqualified assurance this format exists to avoid.
        "snapshot": {
            "taken": True,
            "files": snapshot.get("files"),
            "bytes_stored": snapshot.get("bytes_stored"),
            "not_covered": snapshot.get("not_covered"),
            "manifest": snapshot.get("manifest"),
        }
        if snapshot
        else None,
        "rollback": {
            "applied": rollback.get("applied"),
            "trigger": rollback.get("trigger"),
            "counts": rollback.get("counts"),
            # Present when `applied` is false and a restore was attempted: why it declined.
            "reason": rollback.get("reason"),
            "paths": [
                item["path"]
                for item in rollback.get("applied_paths", [])
                if item.get("status") in {"restored", "recreated", "removed"}
            ],
            "refused": [
                {"path": item["path"], "reason": item.get("reason")}
                for item in rollback.get("applied_paths", [])
                if item.get("status") in {"refused", "failed", "skipped"}
            ],
        }
        if rollback
        else None,
    }

    receipt: dict[str, Any] = {
        "format": FORMAT,
        "docket_number": docket_number,
        "label": label,
        "created_at": created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": {"name": "docket", "version": TOOL_VERSION, "format": FORMAT},
        "session": {
            "id": session_id,
            "argv": session_start.get("argv", []),
            "cwd": session_start.get("cwd"),
            "exit_code": exit_code,
            # decimal string, not a float: hashed structures contain no floats (§3.3)
            "duration_seconds": f"{duration_seconds:.3f}",
            "breached": session_end.get("breached"),
            "interrupted": session_end.get("interrupted", False),
            "host": session_start.get("host", {}),
        },
        "policy": {
            "name": policy.name,
            "digest": policy.digest(),
            "document": policy.to_dict(),
            "source": policy.source_path,
        },
        "totals": _totals_from_rows(usage_rows, meter),
        "model_calls": {
            "count": len(usage_rows),
            "by_provider": by_provider,
            "denied": counts.get(ev.MODEL_DENIED, 0),
            "errors": counts.get(ev.MODEL_ERROR, 0),
        },
        "tools": {"calls": len(tools), "names": tool_names, "failures": tool_failures},
        "files": file_summary,
        "network": {
            "tunnels_allowed": counts.get(ev.NET_ALLOWED, 0),
            "tunnels_denied": counts.get(ev.NET_DENIED, 0),
        },
        "caps": {"exceeded": counts.get(ev.CAP_EXCEEDED, 0), "warnings": counts.get(ev.CAP_WARNING, 0)},
        "event_counts": counts,
        "ledger": {
            "format": LEDGER_FORMAT,
            "events": len(events),
            "head": ledger.head,
            "genesis": GENESIS,
            "merkle_root": ledger.merkle_root(),
            "file_sha256": ledger.file_sha256(),
            "chain": "sha256(prev + canonical_json(event))",
        },
        "price_table": {
            "schema": price_table.document.get("_meta", {}).get("schema", "docket/prices/v1"),
            "as_of": price_table.as_of,
            "digest": price_table.digest(),
            "document": price_table.document,
        },
        "appendix": {
            "usage": usage_rows,
            "proof_included": bool(proof_map) and len(usage_rows) > 0,
            "note": (
                "Each usage row carries the hash of its ledger event and a Merkle inclusion "
                "proof against ledger.merkle_root. Recompute cost from these rows using the "
                "embedded price_table to check this receipt without trusting docket."
            ),
        },
        "evidence": evidence,
        "ops": ops,
        "findings": findings,
        "limits_disclosure": policy.limits_disclosure(),
    }
    # The latency summary is *derived*, and placed last so it cannot be read as a measurement made
    # by the receipt writer: it is the summary of the `latency_ms` values in `appendix.usage`, and
    # `docket verify` recomputes it from those rows. Absent when nothing was measured, which is
    # the honest state for a session recorded through an SDK that did not time its calls.
    measured = [row["latency_ms"] for row in usage_rows if "latency_ms" in row]
    if measured:
        receipt["latency"] = latency_summary(measured, total_rows=len(usage_rows))
    if proxy_stats is not None:
        receipt["proxy"] = {
            "bytes_from_upstream": proxy_stats.bytes_from_upstream,
            "denials": proxy_stats.denials,
        }

    # Order matters, and getting it wrong produces a receipt whose signature cannot be
    # verified: the signature covers everything except itself, *including* the seal.
    # So both fields must exist (sealed value or None) before the signature is computed.
    core = receipt
    if seal_key:
        receipt["seal"] = make_seal(core, seal_key)
    receipt.setdefault("seal", None)
    if sign_key:
        receipt["signature"] = make_signature(receipt_signable(receipt), sign_key)
    else:
        receipt["signature"] = None
    return receipt


def receipt_core(receipt: dict[str, Any]) -> dict[str, Any]:
    """Everything except ``seal`` and ``signature`` (what a seal covers)."""
    return {k: v for k, v in receipt.items() if k not in {"seal", "signature"}}


def receipt_signable(receipt: dict[str, Any]) -> dict[str, Any]:
    """Everything except ``signature`` (what a signature covers, including the seal)."""
    return {k: v for k, v in receipt.items() if k != "signature"}


def write_receipt(receipt: dict[str, Any], path: str | Path) -> Path:
    import json

    atomic_write(path, json.dumps(receipt, indent=2, ensure_ascii=False) + "\n")
    return Path(path)


def load_receipt(path: str | Path) -> dict[str, Any]:
    return loads_json(Path(path).read_text(encoding="utf-8"), what=str(path))


def receipt_digest(receipt: dict[str, Any]) -> str:
    """Content digest of a receipt, ignoring its own seal and signature fields."""
    return digest_of(receipt_core(receipt))
