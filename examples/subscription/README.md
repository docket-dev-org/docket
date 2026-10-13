# Subscription example: caps without pretending to know the price

Most coding-agent usage in 2026 is not billed per token: it is a Pro/Max/Team subscription or an
enterprise seat. There is no per-call price to multiply, and a tool that invents one produces a
number with six decimal places that means nothing.

`budget mode = "quota"` is the honest answer. Docket caps **tokens, model calls and wall-clock
time**, refuses further calls at the boundary, and records `cost_basis` saying that no dollar
figure is being claimed.

```toml
# examples/subscription/docket.toml
[policy]
name = "subscription-workday"

[budget]
mode = "quota"              # dollars are not metered on this plan; do not guess

[caps]
max_tokens = 3_000_000      # about a heavy afternoon of coding-agent use
max_model_calls = 250
max_wall_seconds = 10_800   # three hours
warn_at = 0.8
on_breach = "kill"
```

```console
$ docket run -p examples/subscription/docket.toml -- claude -p "…"
docket: DKT-20261013-… stopped by policy (max_tokens) · 250 calls · receipt .docket/receipts/DKT-…json
```

Within the receipt:

```json
{
  "totals": {
    "cost_usd": "0.000000",
    "cost_basis": "quota mode: subscription usage is counted in tokens, not dollars",
    "total_tokens": 3000431
  }
}
```

Two things to notice:

- **`max_usd` is inert in quota mode** — it is not silently treated as zero, it is not applied
  at all. If you set both, the caps that apply are the ones with meaning.
- **The token counts are still real evidence.** They come from the provider's own usage fields;
  a receipt in quota mode proves how much of your window a run consumed, which is the number
  subscription users actually care about. It simply refuses to translate that into money.

## Why not approximate a price?

Because the estimate would be wrong in a way nobody could check. A subscription's marginal cost
per token is zero until you hit the limit, at which point it is "you cannot work". A tool that
reports `$12.40` for that traffic is not being helpful; it is fabricating a number that will end
up in a report. Quota mode exists so the honest answer is available.

If your plan *does* have a metered overage rate, use `--prices` with your own table and
`mode = "usd"`: your numbers, your provenance, embedded in the receipt.
