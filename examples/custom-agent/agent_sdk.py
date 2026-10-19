#!/usr/bin/env python3
"""Recording an agent's *own* work: model calls, tool calls and notes.

The boundary sees HTTP traffic; only your agent knows what a call *meant*. This example records
both, and demonstrates the distinction the format cares about: events asserted by the agent
(`reported_by: "agent"`) are marked as claims, while boundary events are observations. A reader
of the receipt can tell them apart, which is the difference between evidence and testimony.

Run it standalone (it writes its own ledger and receipt):

    PYTHONPATH=src python3 examples/custom-agent/agent_sdk.py

or under the supervisor, where it attaches to the run's ledger instead of opening its own:

    PYTHONPATH=src python3 -m docket run --max-usd 0.10 -- python3 examples/custom-agent/agent_sdk.py

Both paths are the same code. The difference is who owns the ledger file: standalone, this
process opens one and writes the receipt; attached, the supervisor is the only writer to that
file, because two writers would produce two competing hash chains and the chain would not
verify. So an attached session reports `ledger_path is None` — there is no file of its own to
name — and lets the run that owns it carry the receipt.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

import docket  # noqa: E402
from docket.policy import Policy  # noqa: E402

POLICY = Policy.from_dict({"policy": {"name": "sdk-example"}, "caps": {"max_usd": "0.05"}})


def main() -> int:
    # A throwaway docket dir keeps the standalone run from leaving artifacts in your checkout.
    # Under the supervisor this is ignored: DOCKET_DOCKET_DIR points at the run's own evidence
    # directory, and the supervisor owns the ledger inside it.
    docket_dir = Path(tempfile.mkdtemp(prefix="docket-sdk-")) / ".docket"
    with docket.Session(policy=POLICY, docket_dir=docket_dir) as session:
        print(f"session   {session.docket_number}")

        if session.attached:
            # Attached: the supervisor owns the ledger and will write the receipt for the whole
            # run. There is no path here to name, and inventing one would be a lie.
            print("attached  yes — writing through the boundary; the run owns the ledger")
        else:
            print(f"ledger    {session.ledger_path}")

        # 1. A model call the agent made through the boundary: the SDK adds the *meaning*
        #    (which step of the plan this was for), while usage and cost come from the
        #    boundary's own observation if the run is supervised.
        session.record_model_call(
            model="claude-sonnet-5",
            input_tokens=1_200,
            cached_input_tokens=0,
            output_tokens=340,
            note="step 1: summarise the failing test",
        )

        # 2. A tool call: arguments are recorded as a digest and a size, not contents.
        session.record_tool(
            "search",
            {"q": "invoice totals", "limit": 20},
            result_bytes=41_000,
            note="grep over the docs tree",
        )

        # 3. A tool that failed — failures are first-class evidence, not noise.
        session.record_tool("sql.query", {"stmt_digest": "9f86d081884c7d65"}, ok=False, error="timeout after 30s")

        # 4. A note: the cheapest interesting event type in the vocabulary.
        session.record_note("about to apply the third patch; budget check follows")

        # 5. Budget awareness inside the agent: ask before spending, rather than after.
        if session.would_exceed(usd="0.02"):
            session.record_note("stopping before the expensive step: it would exceed the remaining budget")
            print("stopped early: next step would exceed the remaining budget")
        else:
            session.record_model_call(model="claude-haiku-4.5", input_tokens=800, output_tokens=120)

        print(f"recorded  {session.calls} model call(s), {session.tokens:,} tokens, ${session.cost} attributed")

    if session.attached:
        # The supervisor writes the receipt for the run, and it commits to the whole ledger —
        # including these events. Verify the run's receipt, not one this session could not have
        # written without reading a file it does not own.
        print("\nreceipt   written by the run that owns this ledger")
        print("verify    docket verify .docket/latest.receipt.json")
    else:
        receipt = session.receipt
        print(
            f"receipt   {receipt['docket_number']} · {receipt['totals']['calls']} calls · "
            f"${receipt['totals']['cost_usd']}"
        )
        print("\nevent types in this ledger:")
        for event_type, count in sorted(receipt["event_counts"].items()):
            print(f"  {count:>3}  {event_type}")
        print("\nverify it:")
        print(f"  docket verify {session.receipt_path}")

    print("\nnote how the receipt distinguishes what the agent claimed from what the boundary saw:")
    for record in session.ledger.events if session.ledger is not None else []:
        if record.type == "tool.call":
            print("  " + json.dumps({"type": record.type, "reported_by": record.data.get("reported_by")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
