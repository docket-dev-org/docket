# ADR 0008 — A third implementation, written from the specification alone

- **Status:** accepted
- **Date:** 2026-10-17
- **Context:** `spec/docket-receipt-1.md`, `spec/vectors/`, `verify-go/`

## The problem

The format's central claim is that a stranger can check a receipt *without trusting the tool*.
Two implementations by the same author test that claim only weakly: they agree partly because they
were written by the same hand, on the same days, with the same reading of the same ambiguities.
The corpus narrows the space, but a corpus is written by the same author too.

## The decision

Write a third implementation, in Go, **from `spec/` and `spec/vectors/` only**, without reading
`src/docket/verify.py` or `verify-js/verify.mjs`. Standard library only. Declare the clean-room
constraint before writing any code (`verify-go/CLEANROOM.md`), and record every question the
specification failed to answer as an ambiguity row with the decision taken and whether the spec
should be amended.

Three consequences make this worth the cost:

1. **It tests the document, not the code.** Every ambiguity it hit is a question a real third
   party would hit, in the order they would hit it. Twelve surfaced; five are worth amending.
2. **It is measurable.** `python3 tools/conformance.py --python --node --go` reports
   `132 correct, 0 wrong, 0 not judged` over 44 vectors — agreement on behaviour, not on prose.
3. **It is honest about what it cannot check.** A seal needs a key; the Go verifier reports
   `SKIP`, which is the third verdict the format defines and the one a dishonest implementation
   would rather not emit.

## What was rejected

- **A second Python implementation.** Same language, same idioms, same author: it would agree for
  reasons that have nothing to do with the spec.
- **Transliterating the Node verifier into Go.** Fast, and worthless — the ambiguities are exactly
  what would be lost.
- **Only publishing the ambiguity list without the implementation.** The list is only credible if
  something was built against the answers.

## What this does not establish

That the specification is unambiguous. It establishes the opposite: twelve questions, five
amendments. That is the useful result, and it is more persuasive than a claim of completeness —
a format that has been implemented three times and still has a list of open questions is a format
whose limits are known.
