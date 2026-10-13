# ADR 0001 — Record format decisions as ADRs in `docs/adr/`

- **Status:** accepted
- **Date:** 2026-10-13
- **Deciders:** founding maintainer

## Context

This project's most valuable asset is not its code; it is a *format* that other people will
implement. Format decisions are therefore the expensive kind: they are observable by strangers,
they are hard to reverse once receipts exist, and the reasoning behind them is exactly what a
future implementer needs in order to make compatible choices (and to know which of today's
choices are load-bearing versus accidental).

[`GOVERNANCE.md`](../../GOVERNANCE.md) requires that disagreements which cannot be resolved be
written down — including the rejected option and why. Without a home, that requirement decays
into "somebody said so in a pull request once".

## Decision

Format and architecture decisions are recorded as short ADRs in `docs/adr/`, numbered, in the
conventional form: context, decision, consequences (including the options rejected).

An ADR is required for:

- anything that changes a hashed structure, digest input, canonical form, Merkle construction or
  the receipt layout (which also triggers the format process in
  [`CONTRIBUTING.md`](../../CONTRIBUTING.md#changing-the-format));
- invariants that tests enforce but whose *reason* is not obvious from the code — for example
  the no-floats rule (ADR 0002) and the recomputed-hashes rule (ADR 0003);
- decisions that close a door: what Docket will never do, and the reasoning that survives the
  people who made it.

An ADR is not required for ordinary engineering choices that a reader can infer from the code
and its tests.

## Consequences

- The spec remains justified rather than merely asserted; a new implementer can read *why* the
  canonical form forbids floats instead of filing a bug about it.
- Reversing a decision requires writing the reversal, which is a feature: the cost of changing
  the format should be visible before it is paid.
- ADRs are immutable except for a status change (`superseded by ADR NNNN`); the record of
  having been wrong is kept, because a project whose reasoning is always right in hindsight is
  a project nobody should believe.

## Alternatives considered

- **Comment threads and commit messages.** Rejected: unfindable by someone implementing the
  format from the spec in another language, who is the primary audience for this reasoning.
- **A wiki.** Rejected: not versioned with the code, not reviewable in a pull request, and not
  part of the artifact a verifier receives.
- **A `DESIGN.md` that grows forever.** Rejected: it becomes a document nobody reads and nobody
  can safely edit; ADRs make the cost of a decision proportional and keep the history.
