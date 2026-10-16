# Docket DKT-20260102-VECT01

**Command** `agent --task vector`  
**Exit** 0 · **Duration** 2.5s

| metric | value |
| --- | --- |
| estimated cost | $0.01 (mixed: reported and estimated usage) |
| model calls | 4 (0 denied) |
| tokens | 9,900 (in 9,000, cached 1,000, out 900) |
| files changed | 0 (+0 ~0 -0) |
| deny-write violations | 0 |
| ledger | 12 events · root `1539f98b9a9187be…` |
| latency | p50 101ms, p95 103ms, p99 103ms, max 103ms over 4 measured call(s), nearest-rank |

## Model mix

| model | calls | input | cached | output | estimated cost |
| --- | --- | --- | --- | --- | --- |
| claude-sonnet-5 | 2 | 2,000 | 1,000 | 400 | $0.01 |
| unlisted-model-1 | 1 | 2,000 | 0 | 100 | $0.0035 |
| gpt-6-luna | 1 | 5,000 | 0 | 400 | $0.0007 |

## Verification

```sh
docket verify DKT-20260102-VECT01.receipt.json
```

The receipt embeds the price table it used, so the cost total above can be recomputed from the
provider-reported token counts without trusting Docket.
