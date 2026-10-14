# ADR 0003 — Verification compares recomputed hashes, never stored ones

- **Status:** accepted
- **Date:** 2026-10-13
- **Deciders:** founding maintainer
- **Applies to:** `src/docket/verify.py`, `verify-js/verify.mjs`, §8 of the spec

## Context

A ledger line stores both an event's contents and a `hash` field computed over them. A receipt
stores the ledger's head hash and Merkle root. The naive verification implementation compares
stored values with claimed values: "does the last line's `hash` equal the receipt's `head`?" and
"does the Merkle root rebuilt from the *stored* hashes equal the receipt's root?".

That implementation is wrong in a specific and instructive way. Demonstrated while building the
tamper demo: edit the *contents* of event #5 but leave its stored `hash` untouched. Then:

- the **chain check** correctly fails at #5 (the recomputation no longer matches);
- but the **head check still passes**, because the last event's own bytes were never modified —
  its stored hash still equals its stored hash;
- and the **Merkle root still passes**, because the tree was rebuilt from the stored hashes,
  which are the values that were hashed at write time.

The verdict was still "not verified", so nothing was unsafe. But the output was *misleading*: it
told a reader that the root was intact when the record it commits to had been altered, and a
hurried reader could conclude that only one line had a cosmetic problem.

## Decision

1. When a ledger is supplied, **every value compared against the receipt is recomputed from
   event contents** (`Event.compute_hash()`), never read from the stored `hash` field. An
   intact ledger is unaffected; a tampered one fails every dependent check.
2. **Usage rows are re-bound to the ledger**: besides verifying each row's Merkle path, the
   verifier checks that the row's `event_hash` equals the recomputed hash of the event at its
   `seq`. A row that proves membership in the *claimed* root but not in the *recomputed* record
   is reported as a failure with the offending row and sequence number — under its own check
   name, `usage-rows-rebound`, so that the two facts stay separable in the output (amended
   2026-10-14; asking one check name to carry both verdicts produced a report containing
   `FAIL usage-inclusion-proofs` and `PASS usage-inclusion-proofs` at once).
3. When the chain is broken, checks that depend on it report **`SKIP` with the reason**
   ("not checked: the chain is broken at event #5") rather than passing. A check whose
   precondition has failed is not a pass.
4. When no ledger is supplied, the verifier says plainly that proofs are against *the receipt's
   claimed root* and invites the ledger, instead of implying independent confirmation.

## Consequences

- The tamper demo now reports `FAIL ledger-chain`, `SKIP head`, `FAIL merkle-root`,
  `FAIL usage-rows-rebound` — accurate, specific, and impossible to misread.
- Cost-recomputation checks still pass on a tampered ledger when the *appendix* was untouched.
  That is intentional: it tells a reader precisely where the modification was, which is more
  useful in an argument than "everything failed".
- Verification is marginally more expensive (hashes are computed twice: once for the chain, once
  for the tree). For a 100k-event ledger this is a rounding error next to the I/O, and the
  alternative is a verifier whose output needs a footnote.
- Any future check added to `verify.py` must state which hashes it compares and why; the
  invariant is asserted by `test_verification_fails_when_the_ledger_is_edited`.

## Alternatives considered

- **Compare stored values (the naive implementation).** Rejected: reported above — it produces
  true statements that mislead, which is the worst property a verifier can have.
- **Fail every check when the chain is broken.** Rejected: it destroys the diagnostic value of
  knowing *which* part was touched, and it makes a partial ledger (a legitimate case — someone
  kept only the tail) indistinguishable from a tampered one.
- **Rebuild the Merkle tree from stored hashes but also assert stored==recomputed somewhere
  else.** Rejected as redundant: once the chain check exists, comparisons must simply use the
  right inputs rather than relying on another check to cover for them.
