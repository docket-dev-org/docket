# Sponsor Docket

> **The pitch in one line:** every other tool in this space asks you to trust its dashboard.
> Docket produces a receipt that a stranger can check offline, in any language, for free —
> and sponsorship is what keeps that receipt a *public standard* instead of a product feature.

`docket run --max-usd 5 -- your-agent` costs nothing, needs no account, and will stay
Apache-2.0. Sponsorship funds the parts of this work that no single user pays for and no
single vendor benefits from exclusively.

---

## What money actually buys

Each line item is a deliverable with an acceptance condition. They are published here so
funding can be audited rather than believed.

| # | deliverable | acceptance condition (how you know it happened) | estimate |
| --- | --- | --- | --- |
| 1 | **Independent verifiers** in Go, Rust and TypeScript | each one reproduces `spec/vectors/*.json` exactly, is run in CI on every pull request, and needs no third-party package | ~120 h · **$9k–12k** |
| 2 | **Format stewardship**: a frozen `receipt/v1`, a change process with public rationale, and a migration note for every incompatible change | spec text, vectors and both implementations change in the same pull request, every time | ~4 h/month · **$3k/yr** |
| 3 | **Long-term support window**: security fixes and format stability for a stated period after 1.0, with response targets you can hold us to | `SECURITY.md` states a window in months; advisories ship inside it | ~6 h/month · **$5k/yr** |
| 4 | **The public incident corpus**: every documented runaway-agent cost incident, sourced, dated and summarised — the evidence base this whole problem rests on | a maintained file in this repository; sources linked, disputed entries labelled as disputed | ~20 h · **$2k** |
| 5 | **Documentation and workshops**: quickstart, policy reference, threat model, and a 90-minute workshop that gets a team from zero to a verified receipt | docs merged; workshop materials are in-repo and were actually delivered at least once | ~40 h · **$4k** |
| 6 | **Release engineering**: reproducible builds, signed sums, a clean-venv install test, and release notes a user can read | CI green on a clean install; every release has signed artefacts | ~2 h/month · **$1.5k/yr** |
| 7 | **Failure-of-the-Month**: a real (anonymised where needed) receipt analysed in public — what the agent did, what it cost, what the cap would have done | monthly, archived, with the receipt attached | ~3 h/month · **$2k/yr** |

**Total to do all of it for a year: roughly $35k–40k**, which is also the honest answer to
"how much does a standard need to exist rather than merely be claimed": one part-time
maintainer's attention, spread across deliverables that are individually checkable.

Nothing here pays for marketing, conferences, "growth" or a hosted service. Those are
explicitly unfunded (see [`GOVERNANCE.md`](GOVERNANCE.md) principle 3).

## Who should sponsor this

**Individuals** — because you run agents that can spend your money and touch your files, and
because you would rather hold a receipt than an apology. Individual sponsorship is what makes
the project legible as *used*: a wall of individual supporters is the strongest single signal a
young project can show a corporate sponsor.

**Organisations**, by the reason they would care:

| who | why this is their problem | what sponsorship gets them |
| --- | --- | --- |
| AI platforms and gateways | caps exist in their products, but *portable proof* does not; receipts make their governance claims auditable and interoperable | recognition as a founding supporter, early sight of format changes, a working group seat |
| model providers and cloud platforms | a shared, verifiable cost-and-behaviour record lowers the awkward negotiation about runaway bills | same, plus the format's acknowledgement section |
| agent frameworks and dev-tool vendors | "bounded and provable runs" becomes a box they can check without building a ledger themselves | integration profile maintained by the project, not by their overworked team |
| consultancies, agencies, systems integrators | client work needs evidence of what the agent did and what it cost, without handing over prompts | workshop delivery, priority integration questions, a receipt format they can standardise on |
| regulated enterprises (finance, health, public sector) | auditors want a record that can be checked by someone else's tooling, with limits stated in writing | LTS window, a format-stability promise, and an influence path that does not require becoming a maintainer |
| insurers, GRC and security vendors | a verifiable record of agent behaviour is upstream of underwriting and review processes | format input, and everything above |
| funds and foundations | agent safety and infrastructure work that is unglamorous, local-first and auditable | public reporting on every dollar, per deliverable |

If your product needs "agents that cannot quietly spend $50,000" to be normal rather than
heroic, this is the cheapest way to make that true.

## Tiers

GitHub Sponsors allows up to 10 monthly and 10 one-time tiers; this project lists five and
intends to earn the right to add more. Every benefit below is testable — if it does not
happen within the stated time, that is a bug in this file and you should open an issue.

| tier | monthly | for | benefits (all verifiable) |
| --- | --- | --- | --- |
| **Bounded** | $5 | individuals | name in `SPONSORS.md` within 7 days of your first payment; a monthly "what shipped, what broke" note; the warm feeling of funding a cap that will stop someone else's $50,000 loop |
| **Legible** | $25 | power users, small teams | everything above, plus 72-hour triage on issues you file, and your vote in the quarterly poll that orders the next verifier/feature (published results, non-binding on principle — see governance) |
| **Provable** | $250 | teams shipping agents | everything above, plus your organisation linked in `SPONSORS.md`, a quarterly line-item statement of where that quarter's money went, and a 60-minute office-hours call per quarter on integrating receipts |
| **Charter** | $2,000 | companies whose product depends on this being a standard | everything above, plus acknowledgement in the specification's acknowledgements section, a written priority review of one of your use cases, a stated 48-hour response target for integration questions, and input to the annual roadmap survey. **Capped at 5 sponsors** so the benefits cannot dilute |
| **Filing fee** | $100 one-time | anyone | your name in the one-time supporters list and in the release notes of the next release, in the form of a real ledger line: `{"type":"note","data":{"sponsor":"…"}}` |

One-time support at any amount is equally welcome and is listed without a tier name.
Tax treatment depends on your jurisdiction; GitHub Sponsors handles the mechanics and takes
no platform fee from individual sponsors today.

### What sponsorship never buys

No roadmap veto, no format change, no "verified" stamp on a receipt's output, no influence on
which findings are reported (or omitted), no favourable treatment in a security advisory, no
say over the licence or the governance principles. Those are stated in
[`GOVERNANCE.md`](GOVERNANCE.md) so that this sentence is enforceable rather than polite.

### What happens if funding exceeds the plan

In order: (1) pay the maintenance hours the deliverables above assume; (2) commission the
third verifier rather than waiting for a volunteer; (3) fund a second maintainer for
triage and releases; (4) publish annual accounts. If there is ever more than that, the
surplus goes to the incident corpus and workshops, published before it is spent.

## Recognising the reality of early sponsorship

New open-source projects almost never get corporate money before they have users; the
credibility gate for organisational sponsorship is roughly a thousand stars and a visible set
of dependent projects. Docket's plan for the first months is therefore unglamorous:
individual sponsors, a small number of organisational sponsors who need portable evidence
*before* it is fashionable, and a public record of exactly what was delivered each month. The
full model, including its assumptions and how the numbers were derived, is in
[`docs/strategy.md`](docs/strategy.md).

## Current sponsors

**None yet — this section is reserved for the first supporters.** If you are reading this
before anyone has sponsored, you are early enough to shape the deliverables: open a discussion
and say which of the seven line items above you would pay for, and it goes on the list with
your name on the reasoning.

---

<div align="center">

[**❤ Sponsor Docket on GitHub**](https://github.com/sponsors/docket-dev)

*One cap that stops one runaway loop pays for this project several times over. The cap is
free forever; the standard is what needs you.*

</div>
