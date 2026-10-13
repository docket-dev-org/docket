# Client deliverable example: evidence you can hand over

The scenario: you ran an agent against a client's repository. They want to know what it did and
what it cost, and they would rather not take your word for it.

```bash
# 1. Run the work with a receipt-friendly label and full report
docket run --max-usd 20.00 --label "acme-migration" --report full -- ./agent.sh

# 2. Turn the receipt into something a human reads
docket report --markdown .docket/latest.receipt.json > evidence.md

# 3. Hand over three files and one command
#    - evidence.md                       (what happened, in prose)
#    - DKT-*.receipt.json                (the claim, machine-readable)
#    - <session>.jsonl                   (the ledger the claim commits to)
```

What the client does, with nothing installed but Node (or Python, or their own implementation):

```bash
node verify-js/verify.mjs DKT-*.receipt.json --ledger <session>.jsonl
```

They will see each check, including the honest ones: which items were checked, which were
skipped and *why*, and the `limits_disclosure` block stating what was not observed. Nothing in
the exchange requires them to trust you, your laptop, or this project.

## What makes this different from a screenshot of a dashboard

| a dashboard screenshot | a receipt |
| --- | --- |
| taken by the person with something to hide | checked by the person with something to check |
| totals as rendered by a vendor | totals recomputed from token counts and an embedded price table |
| "trust me, the log says so" | any edit fails at the first modified event |
| scope unstated | scope stated in the artifact and enforced by the verifier |
| a PNG | a file their tooling can parse, diff and archive |

## Practical notes

- **Redact before shipping if you must.** The ledger contains paths, argv, model names and byte
  counts — metadata about the client's repository. Agree on what they get; a receipt with fewer
  events is still a verifiable receipt (verification is per-check, and skips are labelled).
- **Sign when the relationship warrants it.** `docket run --sign key.pem` (Ed25519) makes the
  receipt attestable by the key holder; the public key travels in the receipt, and the client
  can pin the key they expect with `--public-key`.
- **Put the verification command in the deliverable.** A receipt nobody knows how to check is
  a PDF with extra steps.
- **Quote the `docket_number`.** `DKT-YYYYMMDD-XXXXXX` is the handle for emails, tickets and
  invoices — the reason the format has a human-quotable identifier at all.
