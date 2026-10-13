#!/usr/bin/env python3
"""Quickstart example: one supervised agent run, one verified receipt — fully offline.

    policy  ->  docket run  ->  an agent that calls a model  ->  receipt  ->  verification

The model endpoint is Docket's scripted fake provider: no API key, no network, no cost. Every
other part is the real thing — the real supervisor, the real boundary proxy, the real ledger
and the real verifier.

    PYTHONPATH=src python3 examples/quickstart/run_example.py
"""

from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "src"))

from docket.fakeupstream import FakeUpstream, Step  # noqa: E402
from docket.policy import Policy  # noqa: E402
from docket.supervisor import Supervisor  # noqa: E402
from docket.verify import verify_receipt  # noqa: E402


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="docket-quickstart-"))
    # Scripted answers: the third call is deliberately huge (40k input tokens on a model billed
    # per input token), which is enough to cross a $0.0005 cap.
    upstream = FakeUpstream(
        steps=[
            Step(model="gpt-6-luna", input_tokens=900, output_tokens=220, text="here is the failing test"),
            Step(model="gpt-6-luna", input_tokens=700, output_tokens=180, text="applying the fix"),
            Step(model="gpt-6-luna", input_tokens=40_000, output_tokens=300, text="re-reading everything"),
        ]
    )
    upstream.start()

    try:
        policy = Policy.from_dict(
            {
                "policy": {"name": "quickstart"},
                "caps": {"max_usd": "0.0005", "on_breach": "deny"},
                "egress": {"mode": "tunnel"},
                "files": {"watch": ["agent.py"], "ignore": []},
            }
        )
        supervisor = Supervisor(
            [sys.executable, str(HERE / "agent.py")],
            policy=policy,
            docket_dir=workspace / ".docket",
            cwd=workspace,
            upstreams={"openai": upstream.url},
        )
        print(f"workspace   {workspace}", flush=True)
        print(f"policy      max_usd = ${policy.caps.max_usd}, on_breach = {policy.caps.on_breach}", flush=True)
        print("model calls 4 scripted (third one is expensive)\n", flush=True)

        result = supervisor.run()
        receipt = result.receipt
        totals = receipt["totals"]

        print(f"\nexit code   {result.exit_code}  (42 = terminated by a cap; 0 = completed)")
        print(f"breached    {result.breached or 'no cap crossed'}")
        print(f"spend       ${totals['cost_usd']} of ${Decimal(policy.caps.max_usd)} cap")
        print(f"calls       {totals['calls']} forwarded, {receipt['model_calls']['denied']} refused")
        print(f"tokens      {totals['total_tokens']:,} ({totals['cost_basis']})")
        print(f"receipt     {result.receipt_path}")

        verification = verify_receipt(receipt, ledger_path=result.ledger_path)
        print("\n" + verification.render(color=False).strip())
        print("\nCheck it yourself, with either implementation:")
        print(f"  docket verify {result.receipt_path} --ledger {result.ledger_path}")
        print(f"  node verify-js/verify.mjs {result.receipt_path} --ledger {result.ledger_path}")
        print("\nThen edit one byte of the ledger and run the same command again.")
        return 0 if verification.ok else 1
    finally:
        upstream.stop()


if __name__ == "__main__":
    raise SystemExit(main())
