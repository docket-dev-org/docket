# ADR 0002 — No floating-point numbers in hashed structures

- **Status:** accepted
- **Date:** 2026-10-13
- **Deciders:** founding maintainer
- **Applies to:** `docket/ledger/v1`, `docket/receipt/v1` (canonical form, §3.2 of the spec)

## Context

The receipt format is only as useful as it is *reproducible by strangers*. Verification happens
in whatever language the verifier has: Python, Node, Go, a spreadsheet for all we know. The
first independent implementation (`verify-js/verify.mjs`) found a real divergence immediately:
a value that Python serialised as `5.0` (a float with an integral value, written by the policy's
`grace_seconds` default) was serialised by JavaScript as `5`. Every digest downstream of that
field differed, so a receipt produced by Python failed to verify in Node — with no bug in
either program, only in the format's permissiveness.

Shortest-representation rules for floats differ between runtimes and are not part of any
JSON standard:

| value | Python `json.dumps` | JavaScript `JSON.stringify` |
| --- | --- | --- |
| `5.0` | `5.0` | `5` |
| `1e-07` | `1e-07` | `1e-7` |
| `0.1 + 0.2` | `0.30000000000000004` | `0.30000000000000004` (agrees here, but by luck of IEEE-754) |

A "tamper-evident" format that verifies in one language and fails in another is worse than no
format, because it manufactures false tamper alarms and teaches people to distrust the check.

## Decision

**Hashed structures contain no floating-point numbers.**

1. Integers are written as JSON numbers.
2. A float with an integral value is normalised to an integer (`5.0` → `5`) before hashing.
3. Any other fractional quantity must be encoded as a **decimal string** with at most six
   fractional digits (`"0.018000"`, `"0.8"`), compared as exact decimals by verifiers.
4. Implementations must **reject** a hashed structure containing a non-integral float rather
   than guessing a representation. In this repository that is `canon._normalise()` raising
   `TypeError`, and `canonical()` throwing in `verify-js/verify.mjs`.

Money is therefore `"2.000500"`, `warn_at` is `"0.8"`, and `duration_seconds` is `"2.500"` —
strings where a reader might have expected numbers, in exchange for bytes that mean the same
thing everywhere.

## Consequences

- Receipts verify identically across implementations; `spec/vectors/canonical.json` fixes the
  rule in committed bytes, and both test suites consume it.
- Writing this rule down immediately caught three further violations in this repository
  (a `warn_at` fraction, a percentage, and a duration), each of which would have produced
  language-dependent receipts. The rule is enforced by construction: the serializer refuses
  rather than coerces.
- Consumers must parse decimal strings for arithmetic. That is a real cost — and the alternative
  is binary floating point in an evidence format, which is how `$0.018` becomes
  `$0.017999999999999999` in a document someone signs.
- A future "clean numbers" version of the format would be an incompatible change with a
  migration story, not a patch.

## Alternatives considered

- **Allow floats and specify a shortest-representation algorithm.** Rejected: it means
  implementing a float formatter (Ryu/Grisu-style) in every verifier, and the failure mode of
  getting it subtly wrong is exactly the false-alarm problem above.
- **Quantise everything to integers (micro-dollars) everywhere.** Rejected for this format: it
  makes human-readable receipts unreadable (`2000500` instead of `"2.000500"`) and would put a
  decimal-point convention in every consumer's head. The verifier does the micro-dollar
  arithmetic internally.
- **Store amounts as floats and only hash integers.** Rejected: money is the field most likely
  to be tampered with; it must be inside the hash.
