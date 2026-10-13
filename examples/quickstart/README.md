# Quickstart example: one run, one receipt

The smallest complete loop, fully offline:

```
policy  ->  docket run  ->  an agent that calls a model  ->  receipt  ->  verification
```

The model endpoint is Docket's scripted fake provider (no API key, no network, no cost). The
rest is the real thing: the real supervisor, boundary proxy, ledger and verifier.

```console
$ PYTHONPATH=src python3 examples/quickstart/run_example.py
workspace   /tmp/docket-quickstart-f5ujb_mp
policy      max_usd = $0.0005, on_breach = deny
model calls 4 scripted (third one is expensive)

  agent: call 1 ok — 900 in, 220 out
  agent: call 2 ok — 700 in, 180 out
  agent: call 3 ok — 40,000 in, 300 out
  agent: call 4 refused at the boundary (403, max_usd)
  agent: stopping cleanly. Without the boundary I would have kept looping.

exit code   42  (42 = terminated by a cap; 0 = completed)
breached    max_usd
spend       $0.004510 of $0.0005 cap
calls       3 forwarded, 1 refused
tokens      42,300 (reported: provider usage fields)

VERIFIED  DKT-20261013-37DDAF
  PASS  format: docket/receipt/v1
  PASS  ledger-chain: 16 events, every hash and link recomputed
  PASS  head: head matches receipt (930c9878f37d8cf4…)
  PASS  event-count: 16 events
  PASS  merkle-root: 18098d9478c900df…
  PASS  ledger-file-digest: sha256 65f0e3f7836ca0df…
  PASS  usage-inclusion-proofs: 3 usage rows proven against the Merkle root and re-bound to the recomputed ledger
  PASS  cost-recomputation: 3 calls recomputed from token counts
  PASS  totals: total 0.004510 agrees with recomputation
  PASS  token-totals: 42,300 tokens
  SKIP  seal: receipt is not sealed
  SKIP  signature: receipt is not signed
  PASS  limits-disclosure: receipt states what Docket could and could not observe
```

What to notice:

- **`agent.py` has never heard of Docket.** It reads `OPENAI_BASE_URL` like any ordinary
  program; being supervised is a base-URL change, not an SDK migration.
- **Call 3 crossed the cap and completed anyway** — a cap is a ceiling on crossing, not a wall
  in front of it. Call 4 was refused *before* it was forwarded, so the run cost `$0.004510`
  against a `$0.0005` target instead of continuing indefinitely.
- **Exit `42` with `on_breach = "deny"`** is correct: the run *was* stopped by policy (further
  calls refused), but the process was allowed to finish cleanly. `on_breach = "kill"` instead
  terminates the process group at the crossing moment — the right choice when the agent cannot
  be trusted to shut down.
- **The arithmetic is checkable.** gpt-6-luna is $0.10 in / $0.50 out per million tokens, so
  `(900+700+40000)×0.10 + (220+180+300)×0.50` micro-dollars = `$0.004510`. The verifier
  performs exactly this computation from the receipt's own appendix and compares it against
  the claim.

## Verify it as a third party would

```bash
# Docket's Python verifier
docket verify /tmp/docket-quickstart-*/.docket/receipts/*.receipt.json \
              --ledger /tmp/docket-quickstart-*/.docket/runs/*.jsonl

# or nothing but Node, from a clean checkout of this repository
node verify-js/verify.mjs /tmp/docket-quickstart-*/.docket/receipts/*.receipt.json \
                          --ledger /tmp/docket-quickstart-*/.docket/runs/*.jsonl
```

Both should print `VERIFIED` and exit `0`. Now edit a single byte of the ledger with any
tool, and run either command again: verification fails, naming the first broken event and
reporting the dependent checks as failures or as unchecked — never as passes.
