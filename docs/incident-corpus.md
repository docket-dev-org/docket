# The incident corpus

Docket's whole reason to exist is a failure mode with a name: **Denial of Wallet**. This page
is the maintained, sourced record of it. It is also a funded deliverable
([`SPONSORS.md`](../SPONSORS.md), item 4) because a standard needs an evidence base, not a
vibe.

**How to read the confidence column**

| grade | meaning |
| --- | --- |
| **A — primary** | the organisation that operated the system published it, or a named reporter with direct access |
| **B — documented secondary** | trade press or a vendor report summarising a named incident, with enough specifics to be checkable |
| **C — community report** | an operator's own public account (Hacker News, blog) — real, but unaudited |
| **D — claim or survey** | a vendor-commissioned survey or a conference claim; directionally useful, not evidence of a specific event |

Corrections are welcome and will be recorded in the entry as a correction rather than silently
edited: open an issue with a primary source.

---

## Cost and runaway incidents

| date | what happened | grade | source |
| --- | --- | --- | --- |
| Sept 2026 | **An accounting agent entered a runaway execution loop:** **more than 15,000 high-cost API calls in less than an hour**, approximately **$50,000** in cloud charges, disrupting active business transactions. The report's framing: weak controls have consequences even without an attacker. | B | Mandiant *AI Risk and Resilience Report* (Sept 2026) case study, as reported by [Help Net Security](https://www.helpnetsecurity.com/2026/09/16/google-mandiant-enterprise-ai-security-risks-report/) — article verified 2026-10-13; the "corrupted null value" detail appeared in secondary summaries of the report and is not in this article, so it is deliberately not stated here |
| 2026 | **An 11-day multi-agent loop:** an "Analyzer" agent and a "Verifier" agent ping-ponged for eleven days, escalating weekly spend from $127 to $891 to $6,240 to $18,400 — a reported **$47,000** with no useful output. | C | operator account as reported by [TechStartups](https://techstartups.com/2025/11/14/ai-agents-horror-stories-how-a-47000-failure-exposed-the-hype-and-hidden-risks-of-multi-agent-systems/) |
| 12 Jun 2026 | **$6,531.30 in AWS charges** (1,278 points on Hacker News) after an agent re-created CloudFormation stacks on every error, compounding its own mess. | C | Hacker News thread, as summarised by [Nexgismo](https://www.nexgismo.com/blog/ai-agent-budget-guards-stop-runaway-api-costs) |
| 28 Apr 2026 | **$37,901.73** in Claude Opus usage in a single session: prompt caching had silently stopped applying through four layers of tooling (Droid → LiteLLM → Bedrock), so every call was billed at uncached rates. | C | Hacker News discussion + [analysis](https://ysecurity.io/blog/too-fancy-agent-50000-an-hour/) |
| Jul 2025 | **An agent deleted 1,206 production records** and then fabricated replacement data to hide the deletion, then told the user to roll back manually. | B | widely reported (Replit incident); see press coverage of the SaaStr account |
| May 2026 | **A production database and its backups deleted in nine seconds**, described by ServiceNow's chief executive in a keynote as the reason agents need a kill switch. | D | keynote claim, reported in technology press |
| n/a | **GPU hijacking:** an agent loop that consumed cloud GPU capacity, with reported costs around **$1.2M**. | B | Alibaba *ROME* research report coverage |

## Standards and recognised risk

- **OWASP, *Top 10 for Agentic Applications* (2026)** — published 2025-12-09, ASI01–ASI10, and
  **cost is not one of its ten classes**: its second entry (ASI02) is *Tool Misuse and
  Exploitation*. The classes a runaway spend belongs to are **ASI08 *Cascading Failures*** and
  **ASI10 *Rogue Agents***. An earlier version of this line cited "risk #2: Denial of Wallet",
  which the list does not say; corrected 2026-10-16. What *is* a named class, in the LLM list, is
  the next bullet — and that one is graded above.
- **OWASP LLM10:2025 — "Unbounded Consumption"** — the LLM-application variant: cost per token,
  retries, long contexts, no ceiling.

## The environment that makes it possible

- **IDC survey (Dec 2025): 96% of enterprises reported exceeding their AI cost estimates.**
  Grade D (vendor-commissioned survey), but consistent with the engineering reality: cost is
  discovered, not predicted.
- **Agent spend is now a material line item:** the same survey literature and vendor reporting
  consistently describe agent/coding-assistant usage as the fastest-growing slice of AI spend —
  with the awkward property that the spender is a program, not a person with a credit-card
  instinct for "this feels expensive".

## Why this matters for a *receipt*

Every incident above has the same shape: **the record of what happened was produced by the same
system that failed, and nobody was in a position to check it independently.** The bill arrives
as a surprise; the timeline is reconstructed from partial logs; the question "did the agent do
anything else?" cannot be answered at all.

A receipt changes the *aftermath*: the boundary refuses the calls that would continue the loop,
the ledger records the sequence, and the cost is recomputable by the person asking — including
a client, an auditor, or an insurer who does not have access to your systems and should not need
it.

## Contributing an incident

Open an issue with the **Incident report** template and include:

1. what the agent was doing and which model(s) were involved;
2. the outcome in numbers — calls, tokens, dollars, wall-clock, files changed;
3. **a source** (thread, blog, press article, or your own write-up with evidence);
4. whether you are willing to share a receipt (even redacted) — a real receipt is worth more
   than a paragraph.

Entries that cannot be sourced are not published, and an entry that is challenged with a better
source is corrected in public rather than removed quietly. If your organisation cannot be named,
say so — the entry can be published as "a named organisation, anonymised at its request" with
the number and the failure shape intact, which is the part the corpus is actually for.
