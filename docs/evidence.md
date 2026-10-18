# Evidence

Every substantive external claim in this repository, with the source it rests on and an honest
statement of how strong that source is. The rule this file exists to enforce: **if a claim is
load-bearing, a reader must be able to check it, and must know how much checking it received.**

Compiled 2026-10-13. Sources were gathered by searching and fetching public pages, plus direct
calls to public APIs (GitHub REST/search, PyPI, npm). No paywalled analyst reports were read;
several cited articles are themselves summaries of other documents; nothing is quoted from a
page that was not retrieved.

**Grades.** **A** primary (the organisation's own page or API), **B** documented secondary
(reporting on a named primary source, retrievable), **C** community report (single-post,
anecdotal, or summary-of-summary), **D** vendor-commissioned or directional, **E** consensus
with no measured basis.

---

## 1. Claims about the problem

| Claim as used | Source | Grade |
| --- | --- | --- |
| An accounting agent made >15,000 high-cost model calls in under an hour, producing roughly $50,000 in charges and disrupting live transactions; "weak controls can have consequences even without an attacker" | Help Net Security's report of Google Mandiant's *AI Risk and Resilience Report* — [page](https://www.helpnetsecurity.com/2026/09/16/google-mandiant-enterprise-ai-security-risks-report/), 2026-09-16, fetched and read 2026-10-13 | **B** — named primary (Mandiant), retrievable secondary; the primary PDF was not read |
| Unbounded consumption is a first-class, named risk for LLM applications, including cost exhaustion ("denial of wallet") | OWASP GenAI Security Project, [LLM10:2025 Unbounded Consumption](https://genai.owasp.org/llm-top-10/), page fetched 2026-10-13 | **A** for the vulnerability's existence and framing. The 2026 edition's move of this risk up the ranking (#6, referencing a corpus of ~7.7k incidents) comes from secondary summaries — **B–C**, use the rank only with that hedge |
| The operational risk classes a runaway agent spend belongs to are named in OWASP's agentic framework — **ASI08 *Cascading Failures*** and **ASI10 *Rogue Agents*** — while *cost* exhaustion itself is a distinct entry in the **LLM** list (LLM10:2025 *Unbounded Consumption*), not a class in the agentic one | OWASP [Top 10 for Agentic Applications for 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/), published 2025-12-09; the landing page (fetched 2026-10-16) states the date and that the framework is globally peer-reviewed with 100+ contributors, and does *not* enumerate the entries | **A** for the framework, its date and its peer review — the primary page was read, twice, on 2026-10-13 and 2026-10-16. **B** for the entry names (ASI01–ASI10 and the fact that cost is not among them): five independent write-ups of the list agree entry for entry, and the primary page does not list them. An earlier version of this row read "Cost/resource abuse of agents is treated as a distinct agentic risk category" and is wrong: corrected 2026-10-16 against the list's own ten entries |
| A two-agent Analyzer/Verifier loop ran 11 days and produced a ~$47,000 bill, escalating $127 → $891 → $6,240 → $18,400 week over week, with no step limits, ceilings, monitoring or alerts | [Tech Startups](https://techstartups.com/2025/11/14/ai-agents-horror-stories-how-a-47000-failure-exposed-the-hype-and-hidden-risks-of-multi-agent-systems/), 2025-11-14, reporting an engineer's (Teja Kusireddy) account of his own incident | **C** — a self-reported incident retold by a secondary outlet; corroborated in substance by several independent write-ups, not by logs |
| A Claude/Droid/Bedrock pipeline with prompt caching silently disabled billed **$37,901.73** in about an hour; the traces that would explain it were unavailable after the fact | [yssecurity.io](https://yssecurity.io/blog/too-fancy-agent-50000-an-hour/), April 2026 | **C** — single-source technical post-mortem, plausible and detailed, unverified independently |
| A misbehaving agent re-creating cloud resources produced a $6,531.30 AWS bill; the discussion circulated widely | Hacker News thread (June 2026), summarised in [Nexgismo](https://www.nexgismo.com/blog/ai-agent-budget-guards-stop-runaway-api-costs) | **C** for the figure (secondary summary of a primary thread that this repository did not fetch) |
| A coding agent deleted 1,206 production records and then fabricated replacement data and test results (Replit, July 2025) | Widely reported at the time across technology press; not re-fetched for this repository | **C**, flagged as not re-verified |

**Deliberately excluded.** Several circulating "AI horror story" listicles, and an
OpenAI-conference anecdote about an agent deleting a production database and its backups in
nine seconds. Both are vendor-adjacent or unverifiable within the scope of this work. The
in-repo corpus ([incident-corpus.md](incident-corpus.md)) grades each entry rather than
blending them into a scarier number.

**What this means for the product claim.** The load-bearing incident is the Mandiant case
study (grade B, primary named and retrievable). Everything else is corroboration. If you are
deciding whether the problem is real, check the Mandiant case study — not this repository's
summary of it.

## 2. Claims about the landscape and about Docket's position

| Claim as used | Source | Grade |
| --- | --- | --- |
| Star counts for comparable projects (e.g. litellm 61,159; langfuse 35,771; browser-use 117,862; ollama 183,029; gemini-cli 107,314; n8n 207,252) | GitHub REST API, fetched 2026-10-13 | **A** |
| Repositories above thresholds: >100k = 132, >150k = 62, >200k = 30 | GitHub Search API, 2026-10-13 | **A** |
| AI gateways (LiteLLM, Portkey, Bifrost, Orq, TrueFoundry, Vercel/Cloudflare) enforce budgets; several require routing traffic through the gateway with virtual keys, and the strictest blocking modes sit in paid tiers | Vendor documentation via search results, October 2026 | **B** for existence and general capability; **not** independently benchmarked. Point-in-time: vendor tiers change |
| Record/replay cassettes for LLM traffic exist in quantity and mostly remain small (openvcr, agentrec, cs-vcr, culpa, llmtape, promptecho, agent-vcr …) | GitHub searches, 2026-10-13 | **A** for existence and counts |
| At least one agent-governance project already advertises a cryptographic audit trail (Justin0504/Aegis) | GitHub search result and repository description | **A** for the claim's existence; **no evaluation** of its implementation was performed. This is why the README does not claim to be "the first" such tool |
| Several session-level observability tools exist for coding agents (e.g. a Claude Code usage monitor at ~8.7k stars) | GitHub Search API, 2026-10-13 | **A** |

## 3. Design decisions and why they are in the repository

| Decision | Why | Where it is testable |
| --- | --- | --- |
| No floating-point numbers in any hashed structure | Python prints `5.0`, JavaScript prints `5`; that divergence broke cross-language verification during this build | `spec/docket-receipt-1.md` §3, `tests/test_canon.py`, `spec/vectors/canonical.json` |
| RFC 6962 Merkle tree (0x00/0x01 prefixes, unpaired nodes promoted) rather than last-node duplication | matches the transparency-log construction used by Sigstore/Rekor, so implementers' intuitions transfer | `spec/vectors/merkle.json`, both test suites |
| Verification recomputes hashes and re-binds usage rows to the recomputed ledger | a mid-file edit can leave a stored tail hash intact; comparing stored values reports a misleading pass | `tests/test_receipt_verify.py`, `verify-js/test.mjs`, [ADR 0003](adr/0003-verification-uses-recomputed-hashes.md) |
| `SKIP` with a reason instead of a silent pass | a verifier that overstates coverage is worse than no verifier | `docs/verification.md` |
| Price table as versioned data with `as_of`, per-family notes, and a digest inside every receipt; unknown models flagged `assumed` | makes cost recomputation auditable instead of authoritative-looking | `src/docket/prices.json`, `tests/test_pricing.py` |
| `budget_mode = "quota"` for subscription plans | much 2026 coding-agent usage is not metered per token, so inventing a dollar figure would be a lie dressed as a number | `docs/policy-reference.md` |
| Every receipt carries `limits_disclosure`, and verification fails without it | the honest-limits block is the part of this project that survives adversarial review | `tests/test_receipt_verify.py` |

## 4. Claims deliberately not made

| Tempting claim | Why it is not made |
| --- | --- |
| "The first / only cryptographic audit trail for agents" | Other projects advertise cryptographic audit trails (§2). Docket's differentiator is offline, serverless, cross-language verification with recomputable cost — not the mere existence of hashing |
| "Tamper-proof" | Nothing living in user space next to an agent is tamper-proof. The claim is tamper-*evident*, with residual risks written down in [threat-model.md](threat-model.md) |
| "Prevents agents from writing files" | `deny_write` is a tripwire and an evidence source; prevention needs a sandbox, and [threat-model.md](threat-model.md) says so |
| "Prevents runaway spend absolutely" | It prevents the model calls that cross the boundary it controls, and kills the process group. An agent holding a direct provider credential and a URL that bypasses the boundary is outside its reach — which is exactly what `limits_disclosure` states on every receipt |
| "Compliance with the EU AI Act" | No certification exists for this, and the relevant obligations moved to December 2027 (Reg. (EU) 2026/1744). Nothing here pitches regulatory urgency |

## 5. Reproducing this

- Star and fork counts for the comparable projects named in §2 came from the GitHub REST API,
  unauthenticated, on 2026-10-13. They are a snapshot: re-query before quoting them, because a
  figure that was true on the day it was fetched is not a figure that stays true.
- Anything graded **C–E** should be re-checked before it appears in a public claim. No
  load-bearing claim in the README depends on a grade below **B**, and the one that carries the
  most weight (Mandiant) is graded **B** with its source page verified by fetch.
