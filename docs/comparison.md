# How Docket compares

Written to be useful to someone choosing tools rather than to flatter this one. Every category
below contains software that is better than Docket at something specific; where that is true it
is said in the row.

## The one-line positioning

Gateways **route and control**, observability platforms **record and visualise**, cassettes
**replay for tests**, OS audit tools **trace at the kernel**, usage monitors **alert**. Docket
**bounds a run and produces a receipt a third party can check offline, with the cost
recomputable from raw token counts.**

## Where the alternatives genuinely win

| you need | use that instead, because |
| --- | --- |
| central per-team/per-key budgets across dozens of services | an AI gateway (LiteLLM, Portkey, Bifrost, cloud AI gateways): one place to set quotas, routing, retries, fallbacks across providers |
| dashboards, prompt/trace inspection, evals, human review of conversations | observability platforms (Langfuse, Helicone, Phoenix, OpenLLMetry and friends) — a whole UI and query language for traces, which Docket deliberately does not build |
| deterministic tests that do not re-call the model | cassette/replay tools (openvcr, agentrec, cs-vcr, vcrpy, and a dozen more) — that is their entire job, and they are good at it |
| kernel-level enforcement of file/exec/network policy | seccomp, Landlock, gVisor, containers, auditd, eBPF — real sandboxes and real audit facilities. Use them *and* Docket if you need both prevention and portable evidence |
| notifications when a Claude Code session burns its window | usage monitors (e.g. `Claude-Code-Usage-Monitor`, ~8.7k stars as of Oct 2026) — pleasant, single-vendor, and exactly the right shape for personal use |
| a full agent platform | OpenHands, Dify, n8n, LangChain — Docket is a boundary and a receipt format, not a runtime |

## Feature-level comparison

| | **Docket** | AI gateways | Observability | Cassettes | OS audit |
| --- | --- | --- | --- | --- | --- |
| enforceable caps on spend | yes, at the boundary | yes | no | no | no |
| works with subscription/OAuth agents (no API key to route) | yes (base URL or `HTTP_PROXY`) | often no — routing needs keys | partially | yes | yes |
| record of model calls | yes, with usage and cost | yes | yes | yes | no (opaque TLS) |
| record of filesystem changes | yes (snapshot diff, digests) | no | no | no | yes (syscall-level, needs privileges) |
| egress decisions recorded | yes (`[egress]`) | partially | no | no | yes |
| **integrity provable by a third party** | **yes — hash chain + Merkle root + optional signature, offline** | no — you trust the vendor's logs | no | no | no |
| **cost recomputable from raw token counts** | **yes, using the price table embedded in the receipt** | totals shown by the gateway | totals shown by the platform | n/a | n/a |
| works with **zero** network/account | yes | no (self-hosted server, or SaaS) | no | yes | yes |
| runtime dependencies to adopt | none | a server or an account | an SDK + a platform | one package | root, sometimes kernel features |
| honest scope statement shipped *inside the artifact* | yes (`limits_disclosure`) | rarely | rarely | no | n/a |

## The "cryptographic audit trail" claim, carefully

Some agent-governance projects already advertise cryptographic audit trails — for example
Justin0504/Aegis, which describes itself as runtime policy enforcement for AI agents with a
cryptographic audit trail (≈500+ stars when this was written; check the project before
repeating that number). The existence of hashing in an audit log is not the interesting part
and Docket does not claim novelty there.

The distinctions that matter, and that are falsifiable by reading both READMEs and both
receipt formats:

1. **Who can check it.** Docket's receipts are verifiable **offline, by a stranger, in another
   language**, with the vectors in this repository as the acceptance test. Systems that verify
   inside their own server or dashboard require trusting that server.
2. **What is being proven.** Docket binds *cost* to *raw token counts* under a price table that
   is embedded and digested, so a reader can recompute the bill rather than accept a total.
3. **How scope is communicated.** Every Docket receipt carries a machine-readable statement of
   what it does *not* cover, and verification fails if it is missing.

If a competitor does all three, that is excellent news for everyone using agents, and we will
say so here — corrections to this page are welcome as issues.

## Why not just build this into a gateway?

Gateways sit in the request path of *your* API keys. A very large share of agent usage in 2026
runs on subscriptions (Claude Pro/Max, ChatGPT plans, enterprise seats, IDE-integrated agents),
where there is no key to route and no per-token bill to intercept. Docket's boundary is chosen
so that the tool works in the world as it is: point the agent's base URL at localhost, or wrap
the process with `HTTP_PROXY`, and record what actually happened. Caps are expressed in dollars
*because that is the unit people defend against*, and in tokens when dollars do not exist.

## Why not just use git?

`git diff` tells you the end state of the working tree. Docket tells you the end state *and* the
sequence of events that produced it, *and* what the model calls cost, *and* which events the
analysis rests on — in a form whose integrity someone else can check without your repository.
When a client asks "what did your agent do to our codebase, and what did it cost?", the git
diff is half an answer.

## What would make Docket the wrong choice

Be honest with yourself before adopting:

- you need enforcement at the kernel level → you need a sandbox, and Docket is at most the
  evidence layer beside it;
- you want conversation traces and evaluation tooling → use an observability platform;
- you have one agent, one provider, and a corporate gateway already enforcing budgets, and
  nobody will ever ask you to *prove* anything → you may not need this yet;
- you want a managed service with an SLA → nothing here is that, on purpose.

If you recognise your situation in one of those lines, the useful thing Docket can still do for
you is the receipt format — implement it, or ask your vendor to.
