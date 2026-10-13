# Sponsorship, in detail

[`SPONSORS.md`](../SPONSORS.md) is the offer. This page is the reasoning behind it: what the
money is for, why this shape, who tends to fund work like this, what sponsors get, and how the
project avoids the traps that make funded OSS go wrong.

## The proposition, in one paragraph

Agent runs are now large enough to be a line item and opaque enough to be unverifiable: the
record is produced by the system that failed, and nobody outside the operator's account can check
it. Docket's answer is a hard boundary (caps enforced where calls leave the machine) plus an
offline-verifiable receipt (hash chain, Merkle root, recomputable cost, honest scope statement).
Anyone can adopt the tool for free; what cannot be sustained on enthusiasm alone is the
*standard-shaped* work around it — a frozen format with a change process, independent verifiers
in other languages, a long-term-support window, and a public evidence base. That work is what
sponsorship buys, and it is the reason a company whose product depends on a predictable agent
economy would fund it.

## What the money buys, and why those items

The seven deliverables in [`SPONSORS.md`](../SPONSORS.md) are chosen so that each one is
(a) public, (b) individually checkable, and (c) something no single user would pay for:

| deliverable | why it needs funding rather than enthusiasm |
| --- | --- |
| independent verifiers (Go, Rust, TypeScript) | a verifier only has value if a *third party* can run it with what they already have; this is the difference between a format and a claim, and it is the kind of unglamorous work that a volunteer does once and abandons |
| format stewardship | format changes are the most expensive thing this project does; a public process with written rationale is what stops the format drifting with whoever is active that month |
| LTS window | procurement and regulated users ask "how long will this work", and the honest answer today is "as long as someone is around". A funded window converts that into a number |
| incident corpus | the evidence base for the entire problem. Vendors summarise incidents to sell products; a neutral, sourced corpus is a public good |
| docs and workshops | adoption cost is mostly explanation: what the boundary covers, what it does not, how to write a policy that will not surprise a teammate |
| release engineering | signed artifacts, reproducible builds, clean-install tests — the boring machinery that makes `pip install docket` trustworthy |
| Failure-of-the-Month | a real receipt analysed in public. Teaches by example, and holds the project to its own claimed honours |

The sum (~$35k–40k/year) is deliberately a part-time-maintainer number rather than a startup
number: a standard needs to be boring and present, not well-funded and fast.

## Who funds work like this

Three patterns recur in funded infrastructure OSS, and Docket is aimed at all three:

1. **Companies whose product needs the ecosystem to exist.** A gateway, framework or model
   provider benefits when "bounded, provable agent runs" is normal, because it removes the
   awkward conversation about surprise bills and makes their governance claims auditable by
   someone other than them. For them, the cheapest way to have this is to fund it as a
   standard — the case for the *Charter* tier.
2. **Organisations for whom a single incident dwarfs the cost of prevention.** A $50,000
   runaway loop pays for this project several times over. Banks, insurers, healthcare,
   consultancies and integrators under audit pressure have budget for evidence, and their
   procurement actually requires the LTS and format-stability commitments (deliverables 2–3).
   Their willingness to fund is the reason those deliverables come first.
3. ** Individuals who run agents that spend their own money and touch their own files.** Not
   much money each, and worth a great deal as a signal: a wall of individual sponsors is what
   makes a corporate sponsor's legal team comfortable that a project is real.

The honest sequencing note, which is also in [`SPONSORS.md`](../SPONSORS.md): organisational
sponsorship typically arrives *after* visible adoption (roughly a thousand stars and a set of
dependent projects is the observed gate), which is why the plan's early months lean on
individual support and on the small number of organisations that need portable evidence before
it is fashionable.

## What sponsors receive, stated so it can be checked

Recognition is the easiest benefit to give and the easiest to fake, so each tier names something
with a time limit and a place to look: a name in `SPONSORS.md` within seven days, a link, a
line-item statement of where the quarter's money went, a documented response target for
integration questions, acknowledgement in the specification itself. Benefits that scale are
deliberately absent — the top tier is capped at five sponsors specifically so that "input to the
roadmap survey" stays a real conversation rather than a mailing list.

## What is deliberately *not* for sale

No influence over verification results, findings, advisories, the licence, or the governance
principles; no format change in exchange for money; no suppression of a defect report. This is
stated in [`GOVERNANCE.md`](../GOVERNANCE.md) as a constitutional constraint precisely so that a
would-be sponsor can see the answer before asking, and so that a future maintainer with a
funding shortfall cannot quietly trade it away.

Two related commitments that keep the incentive honest:

- **The full incident corpus is published**, including incidents that make Docket's own
  necessity argument look opportunistic. If the corpus is not credible, the product is not
  either.
- **Every sponsored deliverable is public.** There are no "sponsor-only" features, no private
  forks, no paid tier of the tool. The sponsored items are the ones that make the *format*
  durable, and a format with private pieces is not a format.

## How funds are handled

- GitHub Sponsors is the primary channel (no platform fee from individual sponsors today).
  A fiscal host will be used if the project needs legal-entity services — decided in public
  when it happens.
- With a single maintainer, funding pays for maintenance hours on the deliverables above, and
  the quarterly line-item statement published under the *Provable* tier is the account of it.
- With more than one maintainer, a published formula splits income rather than private
  negotiation ([`GOVERNANCE.md`](../GOVERNANCE.md)).
- If funding exceeds the plan, the order of application is published in
  [`SPONSORS.md`](../SPONSORS.md): maintenance hours → third verifier → a second maintainer →
  annual accounts.

## What would make sponsorship unwise

Said plainly, because a sponsorship page that only argues for itself is marketing:

- if you need a managed product with an SLA on *availability*, Docket is not that and will not be
  (there is no service to be available; the receipt is a file);
- if your interest is a proprietary extension of the format, the licence (Apache-2.0, with the
  spec additionally CC BY 4.0) and the governance rules are explicitly against it;
- if you want your cap or your model tier to be the default in the price table — the table is
  versioned data with recorded sources and digests, and it is not a sponsorship slot.

If none of those apply, the practical question is: **what would a receipt be worth to you the
first time an agent does something expensive and you have to explain it?** That number, divided
by the cost of a year of this project, is the sponsorship case. For an organisation where the
answer is "several days of an incident review", it is not close.

→ [**Sponsor Docket**](https://github.com/sponsors/docket-dev)
