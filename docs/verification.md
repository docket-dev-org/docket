# Verification

Verification is the half of Docket that matters most. Anyone can write logs; the claim here is
that **a stranger can check the record without trusting the tool, the operator or the vendor** —
offline, with whatever language they already have installed.

## The three ways to verify

```bash
# 1. Python reference implementation (zero dependencies)
docket verify receipt.json --ledger runs/xxxx.jsonl

# 2. Independent Node implementation (standard library only)
node verify-js/verify.mjs receipt.json --ledger runs/xxxx.jsonl

# 3. Your own, from the spec
spec/docket-receipt-1.md §8 + spec/vectors/*.json as the test suite
```

Exit codes: `0` verified, `1` not verified, `2` usage/IO error. `--json` gives the same result
as data:

```json
{"ok": false, "receipt": "DKT-20261013-746EF4",
 "checks": [{"name": "ledger-chain", "status": "fail", "detail": "broken at event #5: content hash mismatch (event was modified after it was written)"}]}
```

## Every check, and what it actually proves

| check | passes when | what it proves | what it does **not** prove |
| --- | --- | --- | --- |
| `parse` | the file is JSON this format reads at all — one object, no duplicate names, no `NaN`/`Infinity` | the verdict below is about a document that means one thing | — (it is not a verdict: a refusal here exits `1` with an empty stdout and the reason on stderr) |
| `format` | the receipt declares a known version | the reader knows the rules it is being held to | that the producer implemented them |
| `receipt-format` | the document is a JSON object | there is a receipt to hold to the rules at all | — |
| `receipt-shape` | every member a check reads has the type the format gives it, and `usage_source` is `reported` or `estimated` | a malformed member is named once, instead of surfacing as a wrong answer three checks later | that the *values* are true |
| `ledger-chain` | every event's hash recomputes and every link matches, in order | the record existed in this order and has not been altered since | that the record is *complete* |
| `head` | the recomputed final hash matches the receipt | the receipt refers to this exact ledger state | — (reported `SKIP` when the chain is broken, because a mid-file edit can leave the tail hash intact) |
| `event-count` | the count matches | no events were dropped or appended | —  |
| `merkle-root` | the root recomputed over event hashes matches | the receipt's commitment covers exactly these events | — |
| `ledger-file-digest` | the file's SHA-256 matches | the bytes you have are the bytes that were receipted | — |
| `usage-rows-rebound` | each usage row's `event_hash` equals the **recomputed** hash of the event at its `seq` (only when a ledger is supplied) | the rows belong to *this* ledger, not merely to some ledger with the same claimed root | that the recomputed ledger is the whole story (see `limits-disclosure`) |
| `usage-inclusion-proofs` | each usage row's Merkle path verifies against the receipt's claimed root — or the session states plainly that a large run shipped a summary without proofs | the cost you are looking at came from a call that is in the record the receipt commits to | that the record is the one you were handed — see the next row |
| `cost-recomputation` | every row's cost equals `f(input, cached_input, output)` under the embedded price table | the arithmetic is transparent and reproducible | that the *prices* are right — compare `price_table.digest` against a copy you trust |
| `totals` | the receipt's totals equal the sum of recomputed rows | internal consistency | — |
| `token-totals` | token sums match the appendix | nothing was quietly excluded from the sums | that the provider's counts are honest |
| `cached-totals` | `totals.cached_input_tokens` matches the appendix, when the receipt states one | the cached subtotal — the number a saving is computed from — was not restated | that a cache hit was worth making |
| `assumed-prices` | a model priced by the price table's fallback entry is named in `totals.assumed_price_models`; passes when nothing was assumed | a guessed tariff is disclosed wherever its cost appears | that the fallback price is the right one for that model |
| `evidence-completeness` | the receipt's `evidence` member accounts for every metered call and every event written to disk | no event that mattered was lost between the ledger and the receipt | that nothing happened outside the run |
| `latency-summary` | the stated `latency` (§6.2) is recomputed from the `latency_ms` values of the rows it summarises, by the method it names | the percentiles in the receipt describe *these* calls, not a memory of them | that the clock is accurate, that the recorder was the only thing in the request path, or that the provider's own timing is visible — latency is a wall-clock observation and the member says so |
| `seal` | HMAC-SHA256 verifies with the supplied key | integrity to whoever holds the key | third-party verifiability (a seal is not a signature, and receipts say so) |
| `signature` | Ed25519 verifies against the embedded (or supplied-out-of-band) public key | the holder of the signing key attested to this record | *when* they attested it: there is no trusted timestamp |
| `cache-savings` | each cache group's per-hit amount is recomputed from the **origin call's** token counts and the embedded price table, times that group's hit count; the sums must equal `cache.saved_usd` and `cache.avoided_tokens`; with a ledger supplied, the hit total must equal the number of `model.cache_hit` events and the receipt and ledger must agree on whether the hit ceiling was reached | the saving in the receipt is arithmetic you can redo, not a number the producer chose — and the run's `totals` still describe money that was actually spent | that the calls were worth making: a hit is a saving only if the request was one the agent needed |
| `cache-hit-origins` | every group's origin is a metered call in the same receipt, recorded as stored under the same key, with the same model and usage — and, when the ledger is supplied, every `model.cache_hit` event cites an event that exists, is a `model.response` recorded as stored under that key, and whose hash and usage match what the hit claims | a hit is tied to the response it replays rather than to a plausible-looking story about it | that the response was still the right answer when it was replayed: there is no freshness check beyond the TTL the policy set |
| `cache-hit-proofs` | each group's `merkle_path` proves its first hit against `ledger.merkle_root` | the hit count is bound to the recorded ledger, not merely asserted | — (reported `SKIP` when a large session ships the summary without proofs, and the detail line says so) |
| `limits-disclosure` | the receipt states its own scope and trust assumptions | the reader cannot be misled by omission | — |
| `chaos-run` | *advisory, never pass/fail*: a game-day receipt says fault injection was active | a test run cannot be read as production behaviour | anything about the faults themselves |

`SKIP` is a first-class outcome with a printed reason ("no ledger supplied", "receipt is not
sealed", "chain broken, head not checked"). A verifier that reports coverage it does not have
is worse than no verifier, so skipped is never silently turned into passed.

**One name, one verdict.** `docket verify` never emits the same check name with two different
statuses: an earlier version reported the Merkle-path verdict and the re-binding verdict under
one name, so a doctored ledger produced `FAIL usage-inclusion-proofs` and
`PASS usage-inclusion-proofs` a few lines apart. A reader skimming that sees whichever verdict
they expected, which is precisely the failure mode a verifier exists to prevent — hence two
names, and a test (`test_no_check_reports_two_verdicts`) that keeps it that way.

## Holding an implementation to the vocabulary

The table above is a promise; `spec/registry.json` is the same promise in a form a machine can read,
and `tools/conformance.py` is how you check whether an implementation keeps it — yours included:

```bash
make conformance                            # both verifiers shipped here, over the whole corpus
python3 tools/conformance.py --cmd "my-verify {file} --json{ledger_flag}"   # yours
python3 tools/conformance.py --list         # every id, and what it asserts
python3 tools/conformance.py --capabilities --python --node   # which ids each one actually emits
```

The corpus has two halves. `spec/vectors/` holds valid documents a conforming verifier must accept;
`spec/vectors/invalid/` holds **30** documents it must refuse and **3** it must accept, each with an `expected.json` naming
the check that has to catch it and the rule it enforces. `python3 spec/vectors/invalid/build.py
--check` rebuilds the corpus and re-derives every expectation: each vector is run through the
reference verifier, and the build fails unless the first failure is the check the vector names. A
vector that is refused for the *wrong* reason is a vector that documents a defect as a feature.

Three outcomes are kept apart, because conflating them is how a verifier looks better than it is:

* **wrong-ok** — a document that must be refused was accepted. Nothing compensates for this.
* **not-judged** — the implementation reports `skip`, or does not emit the id at all. A declared gap.
  `--allow-gaps` reports it and still exits 0; without it, an unjudged vector fails the run.
* **wrong-check** — refused, but by a different rule than the vector names. Usually a real finding:
  the verifier is reporting the symptom rather than the cause.

`--capabilities` is the part worth stealing if you build this for another format. It derives the set
of ids a verifier emits from its *output* over the whole corpus and compares that with the registry.
It exists because the hand-written version of that table said the Node verifier emitted twelve ids
when it emitted eighteen, and because an id can be emitted only on a receipt shape nobody tests —
`every-usage-row-recomputed` was emitted only when a receipt had no usage rows, which is the one
case where the question cannot arise. That check no longer exists; its assertion is part of
`usage-inclusion-proofs`.

## Worked example: one byte

`docket demo --tamper` performs exactly this, reproducibly:

```console
$ docket demo --tamper
VERIFIED  DKT-20261015-746EF4
  PASS  ledger-chain: 62 events, every hash and link recomputed
  ...

  Now tampering with one byte of the ledger and verifying again:
NOT VERIFIED  DKT-20261015-746EF4
  PASS  format: docket/receipt/v1
  FAIL  ledger-chain: broken at event #6: content hash mismatch (event was modified after it was written)
  SKIP  head: not checked: the chain is broken at event #6
  PASS  event-count: 62 events
  FAIL  merkle-root: recomputed 8792629ac9da7829… != receipt 6103dbfd35035efc…
  FAIL  usage-rows-rebound: usage row 0 (seq 6) does not match the recomputed ledger event
  PASS  usage-inclusion-proofs: 15 usage rows proven against the receipt's claimed Merkle root
  FAIL  usage-rows-rebound: 1 of 15 usage rows are not the events the recomputed ledger has at
        those sequence numbers (14 re-bound)
  PASS  cost-recomputation: 15 calls recomputed from token counts
  PASS  latency-summary: p50 12 ms · p99 23 ms over 15 measured call(s), nearest-rank
  SKIP  seal: receipt is not sealed
  SKIP  signature: receipt is not signed
```

`latency-summary` is the one check above that a flipped byte in the *events* cannot turn red: the
measured times sit in the usage rows, the summary is recomputed from those same rows, and so it
still agrees with itself. That is not a weakness, it is the boundary of the claim — the line says
"these percentiles describe these rows", not "the ledger is intact". Keeping the two claims apart
is what `ledger-chain` and `usage-rows-rebound` are for, and it is why the check is recomputed
rather than trusted: a receipt whose percentiles contradict its own appendix does fail.

(One real capture, lightly trimmed. The docket number is reproducible — `docket demo` seeds its
own random source — but the Merkle roots are not: they cover the session's own fields, including
the temporary workspace path, which differs per run. Treat the shape as the contract and the
digests as an example. Two lines above are wrapped for print width; the tool emits one per check.)

Note what failed and what did not: the *cost* checks still pass, because the receipt's appendix
was not touched — and that is correct and informative. The chain, the root and the binding fail,
and the run is not verified. A verifier that made everything fail indiscriminately would be
easier to write and less useful in an argument.

## Writing your own verifier

You need SHA-256, JSON with sorted keys, and no network. The order of work:

1. **Read §3** of the spec and implement the canonical form exactly. The three traps are
   non-ASCII escaping, numbers, and the JSON non-extensions. Non-ASCII characters are emitted
   literally (never as `\uXXXX`); hashed structures contain integers only, with fractions as
   decimal strings, because if your language prints `5.0` where another prints `5` you will
   produce receipts that verify in one place and not the other; and a document containing
   `NaN`, `Infinity` or `-Infinity` is **not JSON** — RFC 8259 has no spelling for them,
   `JSON.parse` rejects them and Python's `json.loads` accepts them, so a verifier that reads
   one has accepted a document the other reference implementation cannot even parse. Docket
   refuses them at every read (`util.loads_json`) rather than rewriting them, and so should a
   verifier: **reject the file, do not repair it.**
2. **Read §4–§5** and implement the chain and the RFC 6962 tree (leaf prefix `0x00`, node prefix
   `0x01`, promote unpaired nodes — do *not* duplicate the last node, which is a different tree
   with different roots).
3. **Run `spec/vectors/` first.** `merkle.json` fixes your tree, `ledger.json` fixes your chain,
   `receipt.json` gives you a full receipt with a known digest, `canonical.json` fixes your
   serializer. Reproducing those bytes is the acceptance test; if you pass them, you interoperate.
4. **Implement §8's check list**, including `SKIP` semantics. A partial verifier that says
   "verified" for the parts it happens to implement is a liability, not an implementation.
5. Tell us — we will list your implementation in the README and run it in CI on a canary
   receipt, which is the cheap version of a standards body.

Things you do **not** need: the Docket package, an account, network access, or agreement with
the tool. That is the entire point of committing the vectors.

## Verifying without the ledger

A receipt alone can be checked for: format, inclusion proofs against the claimed root, cost
recomputation, totals, seal/signature and limits disclosure. Chain, head and file digest need
the ledger file — which is why the honest phrasing is "the receipt is internally consistent and
its rows are provably in *a* tree with this root", not "this is the whole truth".

If you need the strongest available statement and the ledger is unavailable, ask for the
ledger. The README's rule is the one to carry: **a receipt proves the record is intact, not
that the record is complete** — completeness requires a boundary, and the receipt's
`limits_disclosure` tells you exactly where that boundary was.
