# Clean-room declaration

This directory holds a **third** implementation of `docket/receipt/v1`, written to test whether the
specification is sufficient on its own. It is the only artifact in this repository whose value
depends on what its author did *not* read.

## The rule

- Written from `spec/docket-receipt-1.md`, `spec/conformance.md`, `spec/registry.json` and
  `spec/vectors/` — nothing else.
- The author did not read `src/docket/verify.py` or `verify-js/verify.mjs` while writing it.
- Consulting the **output** of the other verifiers is allowed, and is how agreement is measured
  (`tools/conformance.py --go`, `make conformance`). Consulting their **source** is not.
- Standard library only. No modules are downloaded, nothing is vendored, `go.mod` names one module.

## What would falsify this

A defect in the Go implementation that is only explicable by having copied another
implementation's *code* — the same variable names in the same order with the same private
functions, a comment quoting a line that is in neither the spec nor the vectors. The declaration is
worth what the diff shows; `git log` for these files is a single author's work against the spec.

## Ambiguities the spec left (and what was decided)

Each row: the question, where the spec failed to answer it, the decision, and whether the spec
should be amended. A decision recorded here is not a licence to differ from the other two
implementations — the corpus is what settles behaviour, and the Go verifier must agree with it.

| # | question | spec gap | decision | amend the spec? |
| --- | --- | --- | --- | --- |
| 1 | Which order is a Merkle audit path consumed in: leaf→root or root→leaf? | §5 defines the path "from leaf to root" but not how a pair is combined | consume leaf→root, folding each step with the current hash; proved by `merkle.json` | no — the vector settles it |
| 2 | Is a ledger line that is not valid JSON a refusal or a failed check? | §4 defines the chain but not the failure mode of a file that cannot be parsed; `conformance.md` §3 says a document that cannot be read is refused | refuse the whole invocation (exit 1, empty stdout, reason on stderr), before any check runs; vectors `37`, `38` | no — `conformance.md` §3 covers it, but §4 could say so |
| 3 | Is a trailing newline at the end of a ledger file an event? | §4.1 says one event per line and does not say what a blank line is | blank lines are skipped; the file digest still covers the extra byte, so vector `11` fails under `ledger-file-digest` | yes — one sentence in §4.1 |
| 4 | What shape is `limits_disclosure`? | §6 calls it "a statement, copied verbatim"; the reference writes an object | accept a non-empty string, object or list and require only that it is not empty | yes — §6 should name the shape |
| 5 | Does `totals.by_model[]` have a required order? | §8 item 5a requires it to partition the rows and says nothing about order | order is not checked: a verifier that demanded the builder's sort would be enforcing a rendering choice | no — silence already means "not required" |
| 6 | How is the sixth decimal of a recomputed cost rounded? | §3.3 fixes six decimal places but not the rounding of the last one | half-to-even, which is Python's `Decimal` default and therefore what the reference does | yes — §8.5 should name a rounding rule |
| 7 | How is a model matched against the price table? | §8 item 5 says "the embedded price table" and never defines matching; the table's own `_meta.matched_by` says fnmatch-style, first match in file order | fnmatch, case-insensitive, first match wins, `pattern` split on `/` into alternatives | yes — matching belongs in the specification, not in a note inside the table |
| 8 | Is `usage_source` outside the vocabulary a shape failure or a per-row one? | §6.1 fixes the enum; §8 does not say which id reports it | `receipt-shape`, as both other implementations do; vector `21` | no |
| 9 | Which ledger event says the cache hit ceiling was reached? | §6.4 says "the corresponding `note` event" without naming its contents | a `note` event whose `data` names `cache_hits_ceiling` in `note` or `code` | yes — §6.4 should name the marker |
| 10 | Does a receipt with no `cache` member get a cache verdict? | §6.4 says absence means the run had none | all three cache ids report `SKIP` with that reason | no |
| 11 | May a verifier omit a §8 item it cannot check? | §8 says items that cannot be checked are reported as such, which presumes every item is at least reported | every item is reported; `seal` without a key is `SKIP`, never a pass | no |
| 12 | Is `evidence` completeness part of §8? | §8 does not list it, though the reference emits `evidence-completeness` | not reported here; a check no implementation is required to make cannot be compared | yes — either add it to §8 or say it is optional |

## What the exercise found

Twelve questions the specification does not answer, and five of them are worth an amendment
(items 3, 4, 6, 7 and 9). None of them changed *behaviour*: the corpus settled every case before
the code was written, which is the point of having one. The two that would have mattered most in
practice are the rounding rule (item 6) and price-table matching (item 7) — a second author
reading only §8 could reasonably have chosen either, and produced a verifier that disagrees with
this one on receipts both accept.

That is the finding worth publishing: not "our spec is good", but "here is exactly where it is
under-specified, and here is what we decided".
