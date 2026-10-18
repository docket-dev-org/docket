# Supporting Docket

Docket is free, Apache-2.0, and works offline with no account and no API key. Nothing in the
tool is gated, and nothing in it phones home. If it is useful to you, that is the whole deal.

## What support pays for

The code is done and tested; what costs time is keeping it trustworthy:

- **Format stewardship.** `docket/receipt/v1` is frozen, with a written change process. Every
  incompatible change ships a migration note, because a format break invalidates every receipt
  ever issued.
- **Independent verifiers.** A format is only worth what a third party can reproduce. The
  repository ships Python, Node and Go implementations that share no code and agree on 44
  committed conformance vectors; a fourth in another language is the natural next one.
- **Long-term support.** A stated window for security fixes and format stability, so that
  "how long will this work" has a number instead of an shrug.
- **The incident corpus.** Every documented runaway-agent cost incident, sourced and dated —
  the evidence base the whole problem rests on, maintained rather than assembled once.

## What support does not buy

No roadmap veto, no format change, no "verified" stamp on a receipt's output, no influence on
which findings are reported or omitted, no favourable treatment in a security advisory, and no
say over the licence or the governance principles. Those are stated in
[`GOVERNANCE.md`](GOVERNANCE.md) so that this sentence is enforceable rather than polite.

A sponsorship does not buy support either, unless a separate written agreement says so.

## If you want to contribute

[GitHub Sponsors](https://github.com/sponsors/docket-dev-org) is the mechanism, and one-time support
at any amount is as welcome as a monthly tier. The most useful contribution is not money:
a receipt from a run that surprised you, a defect report with the ledger attached, or a
conformance vector from a language this repository does not cover yet.

Current supporters are listed in [`CONTRIBUTORS.md`](CONTRIBUTORS.md).
