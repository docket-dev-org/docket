# verify-go — a third implementation, written from the specification alone

`docket-verify` reads a Docket receipt, recomputes everything it can from the bytes in front of
it, and reports each item under the id that `spec/registry.json` fixes. It shares no code with
the Python reference or the Node implementation, uses nothing outside Go's standard library, and
was written from `spec/docket-receipt-1.md` and `spec/vectors/` only — see `CLEANROOM.md` for
what that claim is worth and how it could be falsified.

It exists to test the claim the whole format rests on: **the specification alone is enough to
verify a receipt.** Two implementations by the same author can agree because they share habits.
A third, in a different language, built to the document, is the closest thing to evidence.

```console
$ cd verify-go && go build -o docket-verify .
$ ./docket-verify ../spec/vectors/receipt.json
  PASS  receipt-format: the document is a JSON object
  PASS  format: docket/receipt/v1
  ...
  PASS  limits-disclosure: receipt states what Docket could and could not observe

VERIFIED  DKT-20260102-VECT01

$ ./docket-verify receipt.json --ledger run.jsonl --json | jq '.ok, .failures'
```

## What it checks

Every item of §8 it can reach offline: the format tag, the shape of the members it reads, the
ledger chain (hash, link and sequence), the head, event count, Merkle root and file digest, the
re-binding of usage rows to the recomputed ledger, their inclusion proofs, the per-call cost and
every total derived from it — including `by_model[]`, the usage-source counts and `cost_basis` —
the latency summary, the Ed25519 signature, the response cache's savings, origins and proofs, the
disclosure of assumed prices, and the statement of limits. A seal is reported as **not checked**:
an HMAC needs a key, and a verifier that pretends otherwise is lying about what it did.

`totals.calls`, the token columns, the breakdown and the counts are one verdict each, in the
order §8 lists them, so the *first* failing check is a stable answer and the conformance corpus
can name it.

## What it does not do

- It does not check a seal (no key is passed on the command line) and reports `SKIP`.
- It does not check `evidence` completeness: §8 does not require it, and a check nobody is
  required to make is a check that cannot be compared across implementations.
- It does not verify against a *pinned* price table or public key out of band; the receipt's own
  embedded table and key are used, and using a hostile table produces a consistent-but-false
  result — the same limit §9 states for every implementation.
- It is a verifier, not a recorder: nothing here writes or signs a receipt.

## Agreement, measured

```console
$ python3 tools/conformance.py --python --node --go
132 correct, 0 wrong, 0 not judged (2 positive, 42 negative vectors)
```

`--go` runs this binary; `make conformance` includes it when the binary exists. The registry
records the ids each implementation actually emits (25 here), derived from observed output rather
than from reading source.

## Building and testing

```console
$ cd verify-go
$ go vet ./...                 # clean
$ gofmt -l .                   # empty
$ go test ./...                # 16 test functions, table-driven
$ go build -o docket-verify .  # the binary the conformance runner looks for
```

`GOPROXY=off` is enough: there is nothing to download. The toolchain used, its digest and where
it came from are in `TOOLCHAIN.md`.
