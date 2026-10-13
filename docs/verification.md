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
| `usage-inclusion-proofs` | each usage row's Merkle path verifies **and** the row re-binds to the recomputed event at its sequence number | the cost you are looking at came from that specific call in this specific record | that the tokens counted were well spent |
| `cost-recomputation` | every row's cost equals `f(input, cached_input, output)` under the embedded price table | the arithmetic is transparent and reproducible | that the *prices* are right — compare `price_table.digest` against a copy you trust |
| `totals` | the receipt's totals equal the sum of recomputed rows | internal consistency | — |
| `token-totals` | token sums match the appendix | nothing was quietly excluded from the sums | that the provider's counts are honest |
| `seal` | HMAC-SHA256 verifies with the supplied key | integrity to whoever holds the key | third-party verifiability (a seal is not a signature, and receipts say so) |
| `signature` | Ed25519 verifies against the embedded (or supplied-out-of-band) public key | the holder of the signing key attested to this record | *when* they attested it: there is no trusted timestamp |
| `limits-disclosure` | the receipt states its own scope and trust assumptions | the reader cannot be misled by omission | — |

`SKIP` is a first-class outcome with a printed reason ("no ledger supplied", "receipt is not
sealed", "chain broken, head not checked"). A verifier that reports coverage it does not have
is worse than no verifier, so skipped is never silently turned into passed.

## Worked example: one byte

`docket demo --tamper` performs exactly this, reproducibly:

```console
$ docket demo --tamper
VERIFIED  DKT-20261013-746EF4
  PASS  ledger-chain: 61 events, every hash and link recomputed
  ...

  Now tampering with one byte of the ledger and verifying again:
NOT VERIFIED  DKT-20261013-746EF4
  PASS  format: docket/receipt/v1
  FAIL  ledger-chain: broken at event #5: content hash mismatch (event was modified after it was written)
  SKIP  head: not checked: the chain is broken at event #5
  FAIL  merkle-root: recomputed cb646a39ed3fe6f3… != receipt a7c03d9ae872c2cb…
  FAIL  usage-inclusion-proofs: usage row 0 (seq 5) does not match the recomputed ledger event
```

Note what failed and what did not: the *cost* checks still pass, because the receipt's appendix
was not touched — and that is correct and informative. The chain, the root and the binding fail,
and the run is not verified. A verifier that made everything fail indiscriminately would be
easier to write and less useful in an argument.

## Writing your own verifier

You need SHA-256, JSON with sorted keys, and no network. The order of work:

1. **Read §3** of the spec and implement the canonical form exactly. The two traps are
   non-ASCII escaping and numbers: hashed structures contain integers only, and fractions are
   decimal strings. If your language prints `5.0` where another prints `5`, you will produce
   receipts that verify in one place and not the other.
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
