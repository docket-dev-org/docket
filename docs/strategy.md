# Adoption and sponsorship model

Written for someone deciding whether to bet attention or money on this project, and for the
project itself on the days when enthusiasm is not evidence. Every number has a source, and the
weak ones are labelled.

## 1. What the target actually is

The project's stated ambition is **100,000 GitHub stars and 1,000 distinct sponsors within 12
months of launch**. These are targets, not forecasts, and it is worth being precise about how
rare the star figure is:

| quantity | value | source |
| --- | --- | --- |
| repositories with more than **100,000** stars | **132** | GitHub search API, counted 2026-10-13 |
| repositories with more than 150,000 stars | 62 | same |
| repositories with more than 200,000 stars | 30 | same |

Against a platform holding hundreds of millions of public repositories, 100k stars is a
top-0.0001% property, and reaching it *in twelve months* is rarer still. For calibration, from a
sweep of comparable projects (stars and age collected via the GitHub API on 2026-10-13; the full
sweep is `research/gh_sweep.json` in the project's research log):

| project | stars | age at measurement | stars/month (lifetime average) |
| --- | --- | --- | --- |
| gemini-cli | 107,314 | 17.8 months | ~6,000 |
| browser-use | 117,862 | 23 months | ~5,000 |
| ollama | 183,029 | 39 months | ~4,600 |
| AutoGPT | 187,560 | 43 months | ~4,400 |
| dify | 158,375 | 42 months | ~3,800 |
| langchain | 147,769 | 48 months | ~3,100 |
| OpenHands | 90,808 | 31 months | ~2,900 |
| litellm | 61,159 | 39 months | ~1,600 |
| langfuse | 35,771 | 41 months | ~875 |
| Claude-Code-Usage-Monitor | 8,742 | 16 months | ~554 |

**100,000 stars in 12 months requires roughly 8,300 stars/month**, which is higher than *every*
lifetime average in the verified sample above — including projects with a foundation-model lab
behind them (gemini-cli) and the fastest-growing agent tool of 2025–26 (browser-use). The
sample is deliberately conservative: viral outliers that circulated in secondary summaries were
excluded because their numbers could not be confirmed against the API at the time of writing.

That is the honest framing of the ambition: **possible, at the extreme end, and conditional on
repeated public virality that this document cannot guarantee.** What can be engineered is the
*shape* of the attempt: a product that earns its first thousand users on utility, an artifact
(a receipt) that people need to show each other, and launch assets that make each attempt
count.

## 2. The addressable audience, and why they might care

| segment | size signal | why Docket is relevant to them |
| --- | --- | --- |
| developers running coding agents on their own machines | the single-agent usage monitor `Claude-Code-Usage-Monitor` has **8,742 stars** — i.e. thousands of developers already install tooling just to watch their agent's usage | caps and a receipt; the local, no-account promise fits exactly this habit |
| teams running agents in CI or production | LiteLLM (61,159) and Langfuse (35,771) show the size of the governance/observability audience | receipts attach to incidents, clients and audits; the boundary works where gateways cannot |
| regulated/enterprise adopters | every AI-governance buyer in 2026 is being asked for evidence, while the EU AI Act's obligations moved to Dec 2027 (Reg. (EU) 2026/1744) | a format an auditor can verify offline is a cheaper answer than a platform |
| the "cost shock" audience | OWASP names cost exhaustion in its **LLM** top 10 (*Unbounded Consumption*, LLM10:2025) and carries the neighbouring agentic classes (ASI08 *Cascading Failures*, ASI10 *Rogue Agents*) in its peer-reviewed agentic list; documented incidents reach $6.5k–$50k per event (`docs/incident-corpus.md`) | this is the audience that arrives through stories, which is what a launch needs |

Evidence on what actually moves stars (from the literature, not from hope):
[HN launches are worth on the order of **+121 stars at 24 h, +189 at 48 h, +289 at seven days**
on average](https://arxiv.org/abs/2511.04453) — with medians far lower, meaning the mean is driven
by a minority of hits; ~**75% of developers report consulting stars** when evaluating a project;
and **70–73% of "viral" projects credit deliberate social promotion** rather than pure
serendipity. Translation: promotion is not vanity for a project like this, it is the mechanism.

## 3. Staged plan, with what has to be true at each stage

Each stage has an entry condition (what earns the next stage), an asset, and a falsifiable
signal. The project stops being sensible at any stage where the entry condition is not met —
the plan is not "do all of these regardless".

| stage | target | entry condition from the previous stage | what we do | signal that it is working |
| --- | --- | --- | --- | --- |
| 0 | launch (0 → 500) | — | publish the repo, the spec, the vectors, the demo, the incident corpus; a README whose first screen is a real receipt | first 10 external issues; first verifier implementation in another language |
| 1 | utility (500 → 5,000) | someone other than the author has a receipt in production | integration profiles (Claude Code, Codex, Gemini, aider), CI action, `docket explain` output that people paste into issues | receipts attached to real incident reports; "we use this" mentions in other repos |
| 2 | category (5,000 → 25,000) | at least one non-author verifier maintained; a company citing receipts in public | verifiers in Go/Rust/TS, format 1.0 freeze, case studies under LTS, Failure-of-the-Month | the format appearing in other products' docs; sponsors from the "product needs the ecosystem" category |
| 3 | default (25,000 → 100,000) | 1.0 with an LTS window, and an external adopter who depends on it commercially | standards work (interop with transparency-log tooling), procurement-grade evidence, workshops | a receipt used as evidence in an external dispute; organisational sponsors at Charter tier |

The single most important engineering decision for stages 2–3 is already made: **the vectors and
the second implementation exist at 0.1**, because a format without conformance tests is a blog
post. Everything else in the plan can slip; that cannot.

## 4. Sponsorship: the arithmetic, its assumptions, and its gaps

**The target** is 1,000 distinct sponsors. On the observed shape of OSS funding — organisations
are ~40% of funding dollars at ~15× the average individual contribution, and sponsor counts
skew far more individual than dollars do — 1,000 sponsors plausibly look like roughly 850–900
individuals and 100–150 organisations. Docket's tier structure is built for that mix: a $5
individual tier that costs nothing to administer, and organisational tiers where the value is a
deliverable (an LTS window, a verifier, a workshop) rather than a feeling.

| scenario | individuals | orgs | monthly | annual |
| --- | --- | --- | --- | --- |
| conservative | 300 × $5 | 20 × $250 | ~$6.5k | ~$78k |
| base | 700 × $5 + 100 × $25 | 60 × $250 + 6 × $2,000 | ~$37k | ~$450k |
| target (1,000 sponsors) | 800 × $5 + 100 × $25 | 90 × $250 + 10 × $2,000 | ~$49k | ~$590k |

These are *plausible configurations*, not forecasts; the per-sponsor averages come from public
OSS funding discussions (individual averages in the single-digit dollars per month; organisational
contributions at a substantially higher multiple), and the org:individual ratio is the least
certain input. Two things about the model are worth stating bluntly:

- **It is back-loaded and gated by adoption.** Organisational sponsorship follows visible use by
  roughly a year; a project with 500 stars and no dependents raises individual money and little
  else. The 1,000-sponsor target is therefore mostly a *stage-2 and stage-3* target, and the plan
  treats month 1–6 sponsorship as a signal, not as revenue.
- **The sum above is larger than the published programme needs (~$35k–40k/yr).** That is
  intentional: the deliverables list is what the project can honestly promise today, and if
  income exceeds it, the order of application is published
  ([`SPONSORS.md`](../SPONSORS.md)) rather than absorbed.

## 5. What could falsify this

A plan that cannot fail is not a plan. The specific things that would show it is not working:

1. **No receipts in the wild by month 6.** If people install it and never verify, the artifact
   is not valuable enough, and the format story is decoration. (Measuring "verifications in the
   wild" is hard — the closest proxies are issues that attach receipts and third-party
   implementations.)
2. **A gateway absorbs the feature.** If a large platform ships offline-verifiable receipts
   with the same published vectors and no account, Docket's remaining justification is
   independence, and the project should say so and pick a narrower hill (verifier plurality and
   the corpus).
3. **Coding-agent vendors make caps native and provable.** Then the tool's job is integration
   and evidence, not enforcement, which changes the roadmap but not the format.
4. **Sponsorship never crosses individual tiers.** If organisational interest does not appear
   after visible adoption, the LTS commitments come off the roadmap rather than being promised
   and missed.

## 6. What is being asked of a sponsor, precisely

Not a bet on 100k stars. The ask is funding for public goods that exist regardless of how the
ambition lands: the frozen format with the change process, the verifiers in other languages, the
LTS window, the incident corpus, the docs. Those deliverables survive a project that only reaches
10,000 stars, which is the honest floor and a good outcome by any measure other than the one in
the first paragraph.
