# The response cache: paying once for the same question

An agent that asks the same thing twice is the most common way to spend money for nothing, and the
boundary is the only place that sees both the request that was sent twice and the response that came
back twice. So the cache lives at the boundary, and it obeys one rule above all the others:

> **A response served from the cache is never silent.** Every hit is a `model.cache_hit` event in
> the ledger, and the money it saved is recomputed by `docket verify` from the call it replayed and
> the price table embedded in the receipt.

That is the whole idea. Everything below is the consequences of it.

## What this is, and what it is not

Three different things get called "caching" around LLM agents, and confusing them costs money:

| | what it caches | who controls it | what Docket does |
| --- | --- | --- | --- |
| **provider prompt caching** | the provider's own KV/prefill state for a stable prompt prefix | the provider; billed at a discounted `cached_input` rate | **measures** it: `usage.cached_input_tokens` is recorded per call, and the [`cache-miss` finding](concepts.md) estimates what uncached repeats cost |
| **a gateway or CDN cache** | HTTP responses, keyed by URL and headers | the gateway; usually invisible in your ledger | **out of scope**: Docket is not in that path |
| **this cache** | a provider response the boundary itself received, keyed by the exact request it forwarded to | the operator's policy file | **implements it**, and records every hit |

The first and the third are complementary: prompt caching discounts the *tokens* a call costs, while
this cache removes the *call*. A run can benefit from both, and the two numbers appear in different
places in the receipt (`usage.cached_input_tokens` in the rows, `cache.saved_usd` beside the totals)
so neither is mistaken for the other.

## Turning it on

Off by default. A recorder that changes what your agent receives without being asked is not a
recorder you can trust, so nothing here is automatic.

```toml
# docket.toml
[cache]
enabled = true
ttl_seconds = 300      # 0 = entries never expire on time (capacity still bounds them)
max_entries = 256      # least-recently-used entries are evicted above this
max_hits = 1000        # 0 = no ceiling; see "the hazard" below before you set that
```

```bash
docket run --cache --max-usd 5 -- your-agent          # on, with the defaults above
docket run --cache --cache-ttl 60 --cache-max-hits 50 -- your-agent
docket demo --cache                                    # the scripted loop, cached and braked
```

Naming any one of those keys turns the cache on, so `ttl_seconds` alone is not a silently inert
setting; `enabled = false` beside them is how you configure a cache and leave it off.

The configuration is copied into the receipt (`cache.policy`) and into the policy digest, so a reader
can see the exact numbers a saving was claimed under. A policy that says nothing about caching keeps
the digest it had before this feature existed — that property is asserted by a test
(`tests/test_cache.py::test_a_policy_that_says_nothing_about_caching_keeps_the_digest_it_had`),
because a digest that moves when a feature is added turns every earlier receipt into a document
nobody can re-derive.

## The rules

**What is stored.** Only a response the provider actually sent: a 2xx, received whole, to a
non-streaming *or* streaming request, and **not** rewritten or shortened by fault injection. A
truncated stream, an error response, a connection reset, or a response the chaos injector touched is
never stored. The reason travels on the response event itself:

```json
{ "type": "model.response", "data": {
    "cache": { "key": "9f2c…", "stored": false,
               "reason": "not cached: the response was rewritten by fault injection (corrupt body: 8 bytes)" } } }
```

That is not bookkeeping. Caching a corrupted body would make one injected fault sticky for the rest
of the run, and — worse — the receipt would then show a saving earned by replaying a fault.

**What a hit returns.** The origin's bytes, with the origin's headers, to a request of the same kind:
the key is the digest of the request the boundary forwarded, so a streaming request can never be
answered with a buffered body, or the other way round. The bytes are identical to what the provider
sent — a hit is a replay, not a re-generation.

**What a hit is not.** It is not a model call. The provider is not contacted, no
`model.request`/`model.response` pair is written, and the hit contributes **nothing** to
`totals.cost_usd`, `model_calls.count` or any cap. Folding a saving into the spend would make the
evidence describe a run that never took place; the saving is reported beside the spend, never inside
it.

**The key is exact.** Same upstream path, same canonical body. `{"messages": [...]}` and
`{"messages": [...], "temperature": 0}` with the same content are *different* requests and get
different keys. There is no fuzzy matching, no embedding similarity, and no normalisation beyond the
canonical encoding the ledger uses everywhere. If your agent varies a timestamp or a request id per
call, this cache will not help you — and it will report a miss, not a guess.

## In the ledger

One miss and its two hits:

```
seq 4  model.request    { ..., "request_sha256": "9f2c…", "body_bytes": 118 }
seq 5  model.response   { "usage": {...}, "cost_usd": "0.006000",
                          "cache": { "key": "9f2c…", "stored": true } }
seq 6  model.cache_hit  { "key": "9f2c…", "origin": { "seq": 5, "hash": "a11f…" },
                          "model": "claude-sonnet-5", "usage": { "input_tokens": 1000, ... },
                          "avoided_cost_usd": "0.006000", "response_sha256": "77bd…",
                          "age_seconds": 1.204, "circuit_state": "closed" }
```

`origin.seq` and `origin.hash` are what make a hit checkable rather than merely plausible: they name
the response being replayed, and that response is in the same ledger, hashed into the same chain and
Merkle tree. `circuit_state` is recorded because a hit can happen while the provider is down — a
reader should see that a run kept working through an outage on what it already had, rather than
inferring a healthy provider.

**A hit needs no upstream, so an open circuit breaker does not stop it.** The breaker protects the
provider from being hammered; a request that is not going to the provider has nothing to do with it.
A call the cache *cannot* answer is still refused while the breaker is open (`tests/test_cache.py`
drives both halves of that sentence).

## In the receipt

```json
"cache": {
  "enabled": true,
  "policy": { "enabled": true, "ttl_seconds": 300, "max_entries": 256, "max_hits": 1000 },
  "hits": 5, "saved_usd": "0.622500", "avoided_tokens": 201500,
  "hits_ceiling_reached": true,
  "by_origin": [ { "key": "9f2c…", "origin_seq": 5, "origin_hash": "a11f…",
                   "model": "claude-sonnet-5", "usage": { ... },
                   "avoided_usd_per_hit": "0.124500", "avoided_usd_total": "0.622500",
                   "hits": 5, "first_seq": 6, "last_seq": 40,
                   "event_hash": "…", "merkle_path": [ ... ] } ]
}
```

The member is **absent** when the run had no cache. That is a different statement from a cache that
was configured and answered nothing, and a verifier reports the first as *skip, with the reason*.

`by_origin` groups hits that replay the same origin with the same key, model, usage and per-hit
amount. Hits that disagree stay in separate groups rather than being summed: a total that
reconciles to a number no single hit ever claimed is worse than two rows that each say what
happened.

## Verifying a saving offline

`docket verify` recomputes rather than believes. Three checks, reported by name:

```
PASS  cache-savings: 5 hit(s) recomputed at the origin's price: 0.622500 not spent
PASS  cache-hit-origins: every hit resolves to the stored response it replays
PASS  cache-hit-proofs: 1 cache group(s) proven against the receipt's claimed Merkle root
```

* **`cache-savings`** — for every group, the per-hit amount is recomputed from the *origin's* token
  counts and the embedded price table (the same arithmetic §5 of the verification procedure applies
  to a metered call), multiplied by the group's hit count, and compared with `saved_usd` and
  `avoided_tokens`. A receipt that overstates the saving fails here. So does one whose groups do not
  add up to its own total.
* **`cache-hit-origins`** — every group's origin must be a metered call in the same receipt, recorded
  as stored, under the same key, with the same model and the same usage. When the ledger is supplied,
  this gets stricter: every `model.cache_hit` event must cite a `seq` that exists, is a response, was
  stored under the same key, and whose hash matches what the hit claims; and each group's hit count
  must equal the number of events in the ledger that match it.
* **`cache-hit-proofs`** — each group carries an inclusion proof for its first hit against
  `ledger.merkle_root`, the same mechanism the usage appendix uses. Omitted together for a large
  session, and then reported as *skip* rather than assumed.

The **hit count is a measurement, not arithmetic**: it is the number of events the boundary wrote,
which is why it is bound to the ledger by proof and by comparison instead of being derived. The
money is arithmetic, which is why it is recomputed from first principles.

Turn a dial and watch it fail — this is the receipt from `docket demo --cache`, edited:

```bash
python3 - <<'EOF'
import json; p = "DKT-…​.receipt.json"; r = json.load(open(p))
r["cache"]["saved_usd"] = "9.99"
json.dump(r, open(p, "w"))
EOF
docket verify DKT-…​.receipt.json
#   FAIL  cache-savings: cache.saved_usd is 9.99, but the groups recompute to 0.622500
```

## Going away, and the brake

Three ways an entry stops being used, all of them visible:

1. **TTL.** Checked on lookup; an expired entry is *dropped*, not merely filtered, so memory is bound
   by configuration rather than by traffic. Counted as `expirations`.
2. **Capacity.** Least recently used first. Counted as `evictions`.
3. **Refusal to store.** A response the provider did not send is never kept (see above).

And one ceiling that exists for a reason no dollar cap can cover:

### The hazard, named

> A cache can answer a loop that never ends and never spends. `caps.max_usd` and
> `caps.max_model_calls` stop nothing, because there is nothing left to spend and no call being
> made.

`cache.max_hits` is a ceiling on the cache's *own* answers. When it is reached the boundary stops
answering from the cache and forwards again, so the run resumes paying and every cap it was already
subject to bites as before. The first time that happens is written to the ledger as a `note`:

```json
{ "type": "note", "data": { "event": "cache.hits_ceiling_reached", "max_hits": 5,
    "message": "the response cache has answered its configured maximum of 5 calls; further identical
                requests are forwarded upstream again, so spend resumes and the usual caps apply" } }
```

`docket demo --cache` shows the whole thing in one screen: the scripted loop's repeated call is
answered free five times, the brake engages, the loop pays again, and `max_usd` fires — with
`cache.saved_usd = 0.622500` reported beside a spend that ended where it always did.

## Measured

The load harness runs a cached point alongside the uncached one, because "the cache works" and "the
cache works under eight threads" are different claims
([`docs/measurements/2026-10-15-2core/`](measurements/2026-10-15-2core/README.md), raw report in
`cache-report.json`). 2,000 identical calls, 8 threads, cache capped at 5,000 answers:

| measurement | cached run | uncached load point |
| --- | --- | --- |
| calls answered | 2,000 | 2,000 |
| reached the provider | **8** | 2,000 |
| answered from cache | **1,992** | — |
| not spent (receipt, recomputed) | **$11.952000** | — |
| wall clock | 11.018 s | 11.368 s |
| throughput | 181.53 calls/s | 175.93 calls/s |
| client latency p50 / p95 / p99 | 44 / 48 / 48 ms (max 52) | 44 / 48 / 53 ms (max 58) |
| supervisor RSS | 27,284 → 29,624 KiB | 27,220 → 31,408 KiB |
| receipt verifies | yes, 11 pass / 0 fail / 3 skip | yes |

Three honest readings of that table, and one that is not allowed:

* **The saving is the result.** 1,992 of 2,000 calls never reached the provider, and the receipt
  states $11.952000 not spent, recomputed from the eight calls that did.
* **The throughput difference is not a claim.** 181.53 vs 175.93 calls/s is a 3% difference between
  two different runs on a shared 2-vCPU host — the same order as the run-to-run spread already
  documented for the uncached points (99.69 → 166.56 → 175.93 calls/s as the host settled). This
  cache is not a throughput feature and nothing here says it is one.
* **Concurrency costs a few duplicate calls.** Eight threads that miss the same key at the same
  instant will each forward: there is **no request coalescing** (no "single-flight"), so 2,000
  identical calls produced eight metered responses, not one — visible in the receipt as six groups
  (two of the eight origins were replaced before anything replayed them). The cost of that is real
  and small ($0.048 instead of $0.006 in this run); the alternative — holding a thread on someone
  else's in-flight request — adds a wait and a failure mode to a recorder that is supposed to be
  invisible. It is a limitation, and it is named rather than hidden.
* What the table does **not** show: any saving on *your* workload. The driver sends one identical
  body, which is the best case for any cache. Real agents repeat themselves less; run
  `docket run --cache` against your own traffic and read `cache.hits`.

## Known limits

* **In memory, for one run.** Entries are not written to disk and do not survive the process. A
  cross-run cache would need an eviction policy, a size budget on disk and a story about staleness
  between runs; none of that is here, and a half-built version of it would be worse than none.
* **No coalescing** (above).
* **Exact match only** (above).
* **Responses can be large.** An entry holds the response bytes, so a run caching multi-megabyte
  responses is bounded by `max_entries` × response size. `MAX_RESPONSE_BUFFER` already caps what the
  boundary will buffer from an upstream; the cache does not raise it.
* **A hit is fast, and that changes what p99 means.** The client's latency summary covers every round
  trip it made; the receipt's `latency` summary covers the *metered* calls, because a hit is not a
  call. The load report prints both, and `tools/loadtest.py` refuses a run where the two counts
  disagree.

## What is tested

`tests/test_cache.py` (74 tests) is written against the rules above, one test per rule: the disabled
cache stores nothing and counts nothing; a TTL expires an entry and drops it; the LRU entry is the
one evicted; the ceiling stops answers and counts the refusals; a failed, truncated or
fault-injected response is never stored, and the reason is on the event; a streaming request is never
answered with a buffered body; a different request is a miss; an open breaker does not block a hit
but does block a call the cache cannot answer; concurrent identical calls lose nothing (hits plus
upstream calls equals client calls, and the chain verifies); and every receipt-level attack —
an overstated saving, a group citing an unmetered origin, a flipped digit in a proof, a hit count the
ledger does not support, a group whose amount contradicts the price table — makes verification fail
with the reason named. The same rules are exercised end to end by the game-day phase
`cache-brake` (`docket game-day --phases cache-brake`), which drives a repeated loop past the hit
ceiling and checks the brake against the ledger and the disk rather than against the intention.

The module's mutation score and survivors are in [`docs/testing.md`](testing.md#mutation-testing),
beside the other five modules, with every survivor classified as an equivalent mutant or a named gap.

## What it does not do

It does not cache tool results, file reads or anything that is not a model response through the
boundary, and it cannot answer anything that arrives inside a **CONNECT tunnel**: in
`--http-proxy` mode with `egress.mode = "tunnel"` the boundary is a byte pipe, so the request is
never parsed, nothing about it is metered, and nothing about it is cached — the tunnel is
recorded as an egress decision and the rest is opaque by construction. It does not decide what is "the same question" by any rule other than byte-identical
canonical form. It does not invalidate on a timer or a signal, and it does not talk to a provider's
cache-control headers — `Cache-Control: no-store` from an upstream is not consulted, because the
boundary is the operator's recorder and the operator's policy is the one that decides. It does not
save money you would not have spent: a call the cache answered is a call your agent did not need.
