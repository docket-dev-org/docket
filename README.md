# Docket

**Every AI agent run gets a docket number: a hard stop before it does something you cannot
undo, and a tamper-evident receipt anyone can verify offline.**

[![CI](https://github.com/docket-dev/docket/actions/workflows/ci.yml/badge.svg)](https://github.com/docket-dev/docket/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)](pyproject.toml)
[![format](https://img.shields.io/badge/receipt%20format-docket%2Freceipt%2Fv1-informational)](spec/docket-receipt-1.md)
[![licence](https://img.shields.io/badge/licence-Apache--2.0-blue)](LICENSE)

Docket runs your agent under a budget and a boundary it cannot talk its way out of, records
what crossed that boundary into a hash-chained ledger, and emits a receipt that a third party
can check **without the ledger, without the tool, without network access, and without trusting
the machine that produced it**. If the record is edited by one byte, verification says which
event changed.

Local-only. No account, no server, no telemetry, no API keys of its own. It wraps the agent
you already run.

---

## 30 seconds, no API key

```console
$ pip install docket        # or: pipx install docket; or: uvx docket
$ docket demo
```

The demo is not a mock-up: it starts a real supervisor, a real recording boundary, and a
scripted provider, then kills an agent that loops. This is actual output from one run
(trimmed in the middle, marked below). Totals and findings are deterministic; the head hash and
Merkle root change between runs because timestamps live inside the hashed events — which is also
why a receipt is bound to a *specific* run:

```text

  Scenario: an agent is asked to fix one failing test.
  It plans on an expensive model, reads a huge file, then gets stuck re-sending
  the same request. Nothing inside the agent will ever stop it.

  Policy: max_usd = $2.00, deny_write = [.env], watch = [.]

  running (real supervisor, real boundary, scripted provider)…

┌─ DOCKET DKT-20261014-746EF4 ───────────────────────────────────────────────┐
│ /usr/local/bin/python3 -m docket.demo --child                              │
└────────────────────────────────────────────────────────────────────────────┘
  exit       42  (stopped: max_usd)
  duration   160ms
  spend      $2.00 of $2.00 cap
  calls      15   denied: 1
  tokens     647,420 (in 642,900 · cached 0 · out 4,520)
  files      3 changed (+2 ~1 -0)  1 deny-write violation(s)
  tools      1 (read_file)

FINDINGS  (deterministic unless marked heuristic)
  CRITICAL Runaway loop: 13 identical model calls  [$1.62]
            13 byte-identical requests (same prompt, same tools) were sent
            between 17:46:44 and 17:47:00. Identical requests are the
            signature of a loop that will not terminate on its own; the
            model sees no new information, so nothing changes. Estimated
            cost of the repeated group: $1.62.
            rule: runaway-loop
            -> A cap would have stopped this: see the 'counterfactual' finding below, then set caps.max_usd / caps.max_model_calls in your policy.
  HIGH     1 file(s) inside a deny_write scope were changed
            Changed: .env. Docket proves this happened; it cannot veto
            another process's write syscalls from user space, so this is
            evidence rather than prevention.
            rule: write-scope
            -> Give the agent a narrower working directory, or run it in a sandbox/container.

  … (three more findings elided here: uncached repeat context $2.89, one call
     carrying 100,000+ input tokens $0.37, one blocked call. `docket demo` prints them all.)

  attributed impact: $4.89

WHAT THIS RECEIPT PROVES
  ✓ 61 ledger events in order, hash-chained and Merkle-rooted
  ✓ $2.00 recomputable from provider token counts and the embedded price table
  ✓ 3 filesystem change(s) with content digests
  ✗ does not observe: traffic that does not pass through the Docket boundary (e.g. an agent configured with a direct provider URL, or HTTPS tunnels when egress.mode = tunnel)
  ✗ does not observe: prompts, completions or file contents unless --record-bodies is enabled

VERIFY IT YOURSELF (offline, no account)
  docket verify DKT-20261014-746EF4.receipt.json
  node verify-js/verify.mjs DKT-20261014-746EF4.receipt.json

VERIFIED  DKT-20261014-746EF4
  PASS  format: docket/receipt/v1
  PASS  ledger-chain: 61 events, every hash and link recomputed
  PASS  head: head matches receipt (d30a13b1f0bc894c…)
  PASS  event-count: 61 events
  PASS  merkle-root: 451bdfccfea74097…
  PASS  usage-inclusion-proofs: 15 usage rows proven against the Merkle root and re-bound to the recomputed ledger
  PASS  cost-recomputation: 15 calls recomputed from token counts
  PASS  totals: total 2.000500 agrees with recomputation
  PASS  token-totals: 647,420 tokens
  SKIP  seal: receipt is not sealed
  SKIP  signature: receipt is not signed
  PASS  limits-disclosure: receipt states what Docket could and could not observe
```

Then break it on purpose — one byte, in the middle of the ledger:

```console
$ docket demo --tamper
…
NOT VERIFIED  DKT-20261014-746EF4
  PASS  format: docket/receipt/v1
  FAIL  ledger-chain: broken at event #5: content hash mismatch (event was modified after it was written)
  SKIP  head: not checked: the chain is broken at event #5
  PASS  event-count: 62 events
  FAIL  merkle-root: recomputed 12a3b2bc9358f08e… != receipt a0da61f8ee504000…
  FAIL  usage-inclusion-proofs: usage row 0 (seq 5) does not match the recomputed ledger event
  PASS  cost-recomputation: 15 calls recomputed from token counts
  PASS  totals: total 2.000500 agrees with recomputation
  PASS  token-totals: 647,420 tokens
  SKIP  seal: receipt is not sealed
  SKIP  signature: receipt is not signed
  PASS  limits-disclosure: receipt states what Docket could and could not observe

  Try it on your own agent:
    docket run --max-usd 5 -- my-agent --flag
```

## Use it on your own agent

```console
$ docket init                       # writes docket.toml you can edit
$ docket run --policy docket.toml -- claude "fix the failing test"
```

`docket run` starts your command as a child process, points its model traffic at a local
recording boundary, watches the working tree, enforces the caps, and writes
`.docket/receipts/<number>.receipt.json` when the run ends. Anything works — Python, Node,
Go, a shell script, an IDE agent, a cron job:

| Your agent | Command |
| --- | --- |
| Claude Code | `docket run --max-usd 5 -- claude` |
| OpenAI Codex CLI | `docket run --max-usd 5 -- codex` |
| Gemini CLI | `docket run --max-usd 5 -- gemini` |
| opencode / aider / any CLI | `docket run --max-usd 5 -- <command> …` |
| A script using the OpenAI or Anthropic SDK | `docket run --max-usd 5 -- python agent.py` |
| Something that ignores `*_BASE_URL` | `docket run --http-proxy --max-usd 5 -- <command>` |
| Your own in-process agent | `import docket` — record tool calls and notes directly |

Exit codes: `0` normal, `42` a cap was hit (the run was killed), `43` policy refused the run
before it started, `70` internal error, `130` interrupted. `42` means the boundary did its job.

## What it actually does

1. **Caps that bind outside the agent.** USD, tokens, model calls, wall-clock. The boundary
   refuses the next request once a cap is crossed and the supervisor terminates the process
   group — the agent does not need to cooperate, and cannot be prompted out of it.
2. **A hash-chained ledger** of model calls, tool calls, filesystem changes, network
   decisions, process lifecycle and policy events, written as canonical JSON.
3. **A receipt** carrying the totals, the policy, the environment, an RFC 6962 Merkle root
   over every event, and the price table used — so cost is *recomputable* by a stranger from
   raw token counts.
4. **Offline verification** with two independent implementations (Python and Node, both
   stdlib-only) and committed conformance vectors, so "verified" means something portable.
5. **Findings** that explain the bill: runaway loops, repeat context, oversized tool output,
   retry storms, premium models on trivial calls, blocked egress — each with the rule that
   produced it, its evidence, and `deterministic` vs `heuristic` stated plainly.

## What it proves, and what it does not

A receipt proves **what crossed the Docket boundary**, and nothing else. Specifically:

- **Proves:** the recorded events are in order and unaltered since they were written
  (Merkle root + hash chain); the totals agree with the token counts recorded; files under
  `fs.watch` changed in the ways listed; which model calls were refused and why.
- **Does not prove:** that the ledger contains *everything* the machine did. Traffic that
  never passed through the boundary is invisible — an agent configured with a provider URL
  that points elsewhere, or a tunnel allowed by `egress.mode = tunnel`. Docket says so in the
  receipt itself, in `limits_disclosure`, rather than hoping you assume otherwise.
- **Does not prevent:** filesystem writes outside the boundary (`deny_write` is detection and
  evidence, not a veto — user space cannot intercept another process's syscalls without
  `ptrace` or a container). Use a sandbox for prevention; use Docket for the record.
- **Does not sign by default:** sealing is an HMAC with your own key (proves integrity to
  yourself), signing is Ed25519 (proves integrity to someone who has your public key).
  Without either, a receipt is still tamper-evident against the ledger, but anyone who can
  rewrite both can produce a consistent forgery. The verification output says `SKIP` — never
  a silent pass.

That last paragraph is the product's whole personality: it is designed to be *unable* to
overstate what it knows.

## Receipts you can hand to someone

```console
$ docket run --seal --sign ~/.docket/agent.pem --report full -- my-agent
$ docket verify .docket/latest.receipt.json --seal-key .docket/keys/seal.key \
      --public-key <hex>
VERIFIED  DKT-20261013-85A1F6
  PASS  seal: HMAC-SHA256 valid (key 95a17907576b3027)
  PASS  signature: Ed25519 signature valid
```

`--report full` also writes Markdown, which is the artifact you attach to a change request, a
client deliverable, or an incident ticket. `docket explain <receipt>` re-derives the findings
from the ledger; `docket diff <a> <b>` shows what changed between two runs (cost, calls,
models, tools, files) — useful as a regression check for agents, the way a golden test is for
code.

## Install

```console
pip install docket          # Python 3.11+, zero runtime dependencies
pipx install docket         # or keep it out of your project venvs
uvx docket demo             # or run it without installing anything
```

From source: `git clone … && cd docket && make check` (tests are fully offline — no API keys,
no network, ~30 s).

## Documentation

| | |
| --- | --- |
| [Quickstart](docs/quickstart.md) | install → first capped run → verify, in five minutes |
| [Concepts](docs/concepts.md) | boundary, ledger, receipt, findings, caps |
| [CLI reference](docs/cli.md) | every command and flag, exit codes, environment |
| [Policy reference](docs/policy-reference.md) | the whole `docket.toml` schema |
| [Verification](docs/verification.md) | what each check means and how to run it in CI |
| [Integrations](docs/integrations.md) | Claude Code, Codex, Gemini CLI, opencode, aider, SDKs, HTTP |
| [Threat model and limits](docs/threat-model.md) | what Docket does not defend against |
| [Evidence](docs/evidence.md) | every problem claim with its source and its strength |
| [Comparison](docs/comparison.md) | Docket vs gateways, observability, sandboxes, LLM cassette tools |
| [Format specification](spec/docket-receipt-1.md) | the normative `docket/receipt/v1` contract |
| [Roadmap](ROADMAP.md) · [Governance](GOVERNANCE.md) · [Contributing](CONTRIBUTING.md) | how this project is run |

## The problem this exists for

Agent spend and agent authority both grow faster than the ability to audit either.

- A **Mandiant** case study in its *AI Risk and Resilience* report describes an accounting
  agent that made **more than 15,000 model calls in under an hour** and produced roughly
  **$50,000** in charges before anyone noticed, with the resulting database locking disrupting
  live transactions ([Help Net Security's report of the Mandiant findings][hns], September
  2026). This is the strongest single incident in this file: a named primary source, retrieved
  and read.
- **OWASP's GenAI Security Project** carries the category: *Unbounded Consumption* — explicitly
  including cost exhaustion or "denial of wallet" — is a top-10 LLM risk
  ([OWASP][owasp-llm]), and the peer-reviewed [Top 10 for Agentic Applications][owasp-agentic]
  (December 2025) names resource and cost abuse as its own risk class.
- Practitioners report the tail: an **eleven-day two-agent Analyzer/Verifier loop costing
  ~$47,000**, escalating $127 → $891 → $6,240 → $18,400 week over week ([Tech
  Startups][ts47k]); a Claude/Droid/Bedrock pipeline that billed **$37,901.73** when prompt
  caching silently stopped applying ([yssecurity][ys]); a **$6,531.30** bill from an agent
  re-creating cloud resources ([HN, via summary][nexgismo]).
- Those are the tail. The common case is quieter: a run that costs ten times what it should
  and leaves changes nobody can fully account for.

Every figure above carries a source-strength grade in [docs/evidence.md](docs/evidence.md) —
including which of them are primary sources and which are self-reported accounts this project
did not verify. Three of the four are grade **C**. The repository is built so that nothing
load-bearing rests on them.

None of this is fixed by watching a dashboard after the fact, which is what the existing
tooling mostly is. The gap Docket fills is *before* (caps that bind outside the agent) plus
*after* (an artifact that survives contact with a sceptic).

[Full evidence log with source-strength grades →](docs/evidence.md)

## Status

Early. `0.1.0`, published 2026-10-13. The receipt format is versioned and frozen by a written
spec with test vectors; the CLI may still move. One maintainer, no employer, no funding — see
[SPONSORS.md](SPONSORS.md) for what funding would and would not buy.

Nothing here is a security boundary you should rely on for isolation: Docket is an accounting
and evidence layer that happens to enforce budgets. Use containers or a sandbox when the
threat is a hostile agent; use Docket to know what happened, put a number on it, and prove the
number.

## Contributing

Bug reports, "this estimate is wrong" reports, and format critiques are all welcome. The two
rules that matter: **the test suite must stay offline**, and **nothing that hashes a structure
may change without a spec change** (a format break invalidates every receipt ever issued).
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Sponsorship

Docket is funded directly, not by a hosted tier — there is nothing to buy access to. Sponsors
fund maintenance, the second and third verifier implementations, the conformance suite, and
the incident corpus that keeps the estimates honest. See [SPONSORS.md](SPONSORS.md).

## Licence

Apache-2.0 for code; the specification text is CC BY 4.0 so that other implementations can
copy it freely.

[hns]: https://www.helpnetsecurity.com/2026/09/16/google-mandiant-enterprise-ai-security-risks-report/
[owasp-llm]: https://genai.owasp.org/llm-top-10/
[owasp-agentic]: https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/
[ts47k]: https://techstartups.com/2025/11/14/ai-agents-horror-stories-how-a-47000-failure-exposed-the-hype-and-hidden-risks-of-multi-agent-systems/
[nexgismo]: https://www.nexgismo.com/blog/ai-agent-budget-guards-stop-runaway-api-costs
[ys]: https://yssecurity.io/blog/too-fancy-agent-50000-an-hour/
