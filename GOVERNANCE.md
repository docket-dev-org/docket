# Governance

Docket is a small project with a large claim: that a receipt produced by this tool can be
checked by someone who trusts neither the tool nor its authors. That claim constrains how the
project is governed, because the format is worth more than the implementation.

## Principles

1. **The format is the artifact.** Implementations are replaceable; `spec/docket-receipt-1.md`
   and its conformance vectors are what make a receipt portable. Format stability outranks
   feature velocity.
2. **No telemetry, no accounts, no network requirement — ever.** This is a constitutional
   constraint, not a roadmap item.
3. **Sponsors buy deliverables, not decisions.** Money can pay for a benchmark, a verifier, a
   release window or a support commitment. It cannot buy a format change, a roadmap veto, a
   "sponsored by" position on a verification result, or silence about a defect. Any sponsored
   deliverable is public, and a sponsor that asks for anything on this list is told no in
   public issue rather than email.
4. **Claims stay narrow and checkable.** Every security-adjacent claim in the docs must name
   its limits in the same breath. "Honest limits" is a feature of this project, not fine
   print.

## Roles

| role | who | responsibilities |
| --- | --- | --- |
| Contributor | anyone | pull requests, issues, reviews, docs |
| Reviewer | contributors with a track record of merged, correct changes | may approve non-format changes |
| Maintainer | listed in `MAINTAINERS.md` | merges, releases, security response, format decisions |
| Format Steward | one maintainer, rotating annually | owns the spec text and the vectors; holds a veto on format changes until 1.0 |

## How decisions are made

- **Lazy consensus** on ordinary changes: a pull request with one reviewer approval and no
  reasoned objection within 72 hours may be merged by a maintainer.
- **Maintainer consensus** for behaviour changes, policy fields, new findings rules and
  anything touching the threat model: two maintainers must approve.
- **Format changes** follow the process in [`CONTRIBUTING.md`](CONTRIBUTING.md#changing-the-format):
  the Format Steward plus two maintainers, normative spec text, regenerated vectors, both
  implementations updated in the same pull request, migration story written down.
- **Constitutional matters** (principles 1–4 above, and the licence) require unanimous
  maintainer agreement, and any change to them must be announced in a pinned discussion for
  at least 14 days before it takes effect.

Disagreements that cannot be resolved are recorded as an ADR in `docs/adr/` — including the
option that was rejected and why. A project that documents its disagreements is easier to
trust than one that appears to have none.

## Becoming a maintainer

Sustained, high-quality participation over a few months: reviews that catch real problems,
fixes for issues you did not create, docs that reduced support load, verifiers or
integrations maintained rather than contributed once. Existing maintainers nominate; the
nomination is announced in Discussions for a week for comment; then a simple majority of
maintainers confirms. Inactivity for six months moves you to *Maintainer emeritus* — you keep
the title and can return.

## Releases and support

- Semantic versioning for the tool; the format tag is versioned separately (`docket/receipt/v1`)
  and changes only per the format process.
- Releases are cut from `main` by a maintainer, tagged, built in CI, and accompanied by a
  `CHANGELOG.md` entry and a signed `SHA256SUMS`.
- The support commitment for any given version is stated at release time. Sponsorship is what
  allows that commitment to be longer than "until the next maintainer gets busy" —
  see [`SPONSORS.md`](SPONSORS.md).

## Funding and independence

Sponsorship is accepted through GitHub Sponsors (and, if it ever exists, a fiscal host) under
the rules in principle 3. Funds are spent on the deliverables listed publicly in
[`SPONSORS.md`](SPONSORS.md); when the project has more than one maintainer, incoming funds
are split by a published formula rather than by negotiation in private. Until that point the
maintainer's commitment is stated plainly: sponsorship pays for maintenance time, verifiers,
documentation and release engineering — it does not change what the software does or how it
verifies.

## Changing this document

Open a pull request. Governance changes need maintainer consensus, a 14-day comment window and
an entry in `CHANGELOG.md`. Founders' special rights: none — that is the point.
