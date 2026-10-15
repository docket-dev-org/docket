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
| `format` | the receipt declares a known version | the reader knows the rules it is being held to | that the producer implemented them |
| `ledger-chain` | every event's hash recomputes and every link matches, in order | the record existed in this order and has not been altered since | that the record is *complete* |
| `head` | the recomputed final hash matches the receipt | the receipt refers to this exact ledger state | — (reported `SKIP` when the chain is broken, because a mid-file edit can leave the tail hash intact) |
| `event-count` | the count matches | no events were dropped or appended | —  |
| `merkle-root` | the root recomputed over event hashes matches | the receipt's commitment covers exactly these events | — |
| `ledger-file-digest` | the file's SHA-256 matches | the bytes you have are the bytes that were receipted | — |
| `usage-rows-rebound` | each usage row's `event_hash` equals the **recomputed** hash of the event at its `seq` (only when a ledger is supplied) | the rows belong to *this* ledger, not merely to some ledger with the same claimed root | that the recomputed ledger is the whole story (see `limits-disclosure`) |
| `usage-inclusion-proofs` | each usage row's Merkle path verifies against the receipt's claimed root | the cost you are looking at came from a call that is in the record the receipt commits to | that the record is the one you were handed — see the next row |
| `cost-recomputation` | every row's cost equals `f(input, cached_input, output)` under the embedded price table | the arithmetic is transparent and reproducible | that the *prices* are right — compare `price_table.digest` against a copy you trust |
| `totals` | the receipt's totals equal the sum of recomputed rows | internal consistency | — |
| `token-totals` | token sums match the appendix | nothing was quietly excluded from the sums | that the provider's counts are honest |
| `latency-summary` | the stated `latency` (§6.2) is recomputed from the `latency_ms` values of the rows it summarises, by the method it names | the percentiles in the receipt describe *these* calls, not a memory of them | that the clock is accurate, that the recorder was the only thing in the request path, or that the provider's own timing is visible — latency is a wall-clock observation and the member says so |
| `seal` | HMAC-SHA256 verifies with the supplied key | integrity to whoever holds the key | third-party verifiability (a seal is not a signature, and receipts say so) |
| `signature` | Ed25519 verifies against the embedded (or supplied-out-of-band) public key | the holder of the signing key attested to this record | *when* they attested it: there is no trusted timestamp |
| `limits-disclosure` | the receipt states its own scope and trust assumptions | the reader cannot be misled by omission | — |

`SKIP` is a first-class outcome with a printed reason ("no ledger supplied", "receipt is not
sealed", "chain broken, head not checked"). A verifier that reports coverage it does not have
is worse than no verifier, so skipped is never silently turned into passed.

**One name, one verdict.** `docket verify` never emits the same check name with two different
statuses: an earlier version reported the Merkle-path verdict and the re-binding verdict under
one name, so a doctored ledger produced `FAIL usage-inclusion-proofs` and
`PASS usage-inclusion-proofs` a few lines apart. A reader skimming that sees whichever verdict
they expected, which is precisely the failure mode a verifier exists to prevent — hence two
names, and a test (`test_no_check_reports_two_verdicts`) that keeps it that way.

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
