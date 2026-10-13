<div align="center">

<img src="assets/logo.svg" alt="Docket" width="88" height="88">

# Docket

**Every AI agent run gets a docket number: a hard spending cap, and a tamper-evident receipt anyone can verify offline.**

[![CI](https://github.com/docket-dev/docket/actions/workflows/ci.yml/badge.svg)](https://github.com/docket-dev/docket/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Runtime dependencies: 0](https://img.shields.io/badge/runtime%20deps-0-brightgreen.svg)](pyproject.toml)
[![Format: docket/receipt/v1](https://img.shields.io/badge/format-receipt%2Fv1-blueviolet.svg)](spec/docket-receipt-1.md)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE)

`docket run --max-usd 5 -- your-agent` &nbsp;·&nbsp; no account &nbsp;·&nbsp; no server &nbsp;·&nbsp; no telemetry &nbsp;·&nbsp; agents in any language

</div>

---

## The 20-second version

```console
$ docket demo
  Scenario: an agent is asked to fix one failing test.
  It plans on an expensive model, reads a huge file, then gets stuck re-sending
  the same request. Nothing inside the agent will ever stop it.

  Policy: max_usd = $2.00, deny_write = [.env], watch = [.]

  running (real supervisor, real boundary, scripted provider)…
    agent: the boundary refused this call (403, max_usd)
    agent: stopping here. It would have kept going.

┌─ DOCKET DKT-20261013-746EF4 ───────────────────────────────────────────────┐
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
  HIGH     1 file(s) inside a deny_write scope were changed
            Changed: .env. Docket proves this happened; it cannot veto
            another process's write syscalls from user space, so this is
            evidence rather than prevention.
  MEDIUM   Repeated context appears to be uncached  [$2.89]
  MEDIUM   1 call carried 100,000+ input tokens  [$0.37]
  MEDIUM   1 model call blocked by policy
  LOW      1 small call went to a premium model  [$0.01]

  attributed impact: $4.89

WHAT THIS RECEIPT PROVES
  ✓ 61 ledger events in order, hash-chained and Merkle-rooted
  ✓ $2.00 recomputable from provider token counts and the embedded price table
  ✓ 3 filesystem change(s) with content digests
  ✗ does not observe: traffic that does not pass through the Docket boundary
  ✗ does not observe: prompts, completions or file contents unless --record-bodies

VERIFIED  DKT-20261013-746EF4
  PASS  ledger-chain: 61 events, every hash and link recomputed
  PASS  merkle-root: 007fe16728a98fee…
  PASS  usage-inclusion-proofs: 15 usage rows proven against the Merkle root and
        re-bound to the recomputed ledger
  PASS  cost-recomputation: 15 calls recomputed from token counts
  PASS  totals: total 2.000500 agrees with recomputation
  SKIP  signature: receipt is not signed
```

Then `docket demo --tamper` edits a single byte of the ledger and verifies again:

```console
NOT VERIFIED  DKT-20261013-746EF4
  FAIL  ledger-chain: broken at event #5: content hash mismatch (event was modified after it was written)
  SKIP  head: not checked: the chain is broken at event #5
  FAIL  merkle-root: recomputed cb646a39ed3fe6f3… != receipt a7c03d9ae872c2cb…
  FAIL  usage-inclusion-proofs: usage row 0 (seq 5) does not match the recomputed ledger event
```

*Captured from a real run. Timestamps and hashes differ between runs; event counts and
totals are deterministic. `docket demo` needs no API key and no network.*

## Why this exists

Agents are now a large share of AI spend, and they fail in a specific way: **quietly,
expensively, and without evidence**.

| documented incident | what happened | source |
| --- | --- | --- |
| Mandiant, *AI Risk and Resilience Report*, reported 16 Sept 2026 | An **accounting agent** entered a runaway execution loop: **more than 15,000 high-cost API calls in less than an hour**, **~$50,000** in cloud charges, and it disrupted active business transactions. No attacker was involved. | [Help Net Security](https://www.helpnetsecurity.com/2026/09/16/google-mandiant-enterprise-ai-security-risks-report/) |
| Widely reported multi-agent incident | Two agents ping-ponged for **11 days**: **$47,000** in charges, $127 → $891 → $6,240 → $18,400 weekly, nobody noticed | [coverage](https://techstartups.com/2025/11/14/ai-agents-horror-stories-how-a-47000-failure-exposed-the-hype-and-hidden-risks-of-multi-agent-systems/) |
| Hacker News, June 2026 (1,278 points) | An agent re-created CloudFormation stacks on every error: **$6,531** AWS bill | [summary](https://www.nexgismo.com/blog/ai-agent-budget-guards-stop-runaway-api-costs) |
| Hacker News, April 2026 | **$37,901.73** Claude Opus bill where prompt caching had silently stopped applying through four layers of tooling | [analysis](https://ysecurity.io/blog/too-fancy-agent-50000-an-hour/) |

OWASP named the failure mode in its *Top 10 for Agentic Applications (2026)*: risk #2 is
**Denial of Wallet**. Dashboards tell you afterwards; soft alerts tell you while it is still
spending. What is usually missing is a **bound** and a **receipt**.

Docket is small and local on purpose:

- **Bounded** — hard caps on dollars, tokens, model calls and wall-clock time, enforced at
  the boundary where calls actually leave the machine. Cross a cap and the process group is
  terminated (exit code `42`), or further calls are refused — your choice per policy.
- **Legible** — an append-only, hash-chained ledger of model calls, tool calls, filesystem
  changes, egress decisions and process lifecycle; plus deterministic analysis that says
  where the money went, what a cap would have saved, and which events it is basing that on.
- **Provable** — a receipt whose integrity is checkable **offline, in another language, by
  someone who trusts neither you nor this tool**, whose cost totals recompute from raw token
  counts using the price table embedded in the receipt, and which states in writing what it
  does *not* cover.

### Who this is for

| you are… | Docket gives you |
| --- | --- |
| an engineer running coding agents on a repo | a daily cap you cannot accidentally exceed, and a log of every file the agent touched |
| a platform/ML team running agents in production | per-run receipts you can attach to incident reviews and client work, without shipping prompts anywhere |
| a consultancy or agency | evidence to hand a client: what the agent did, what it cost, verifiable without trusting your dashboard |
| in a regulated industry (finance, health, public sector) | a format an auditor or reviewer can check with their own tooling, plus a written statement of its limits |
| building agent frameworks | a boundary and evidence layer you can adopt wholesale instead of reimplementing badly |

## Install

```bash
pip install docket                  # Python 3.11+, zero runtime dependencies
pipx run docket demo                # try it with no install
PYTHONPATH=src python -m docket demo  # from a checkout
```

Optional: `pip install 'docket[sign]'` adds Ed25519 receipt signing through `cryptography`.
Nothing else in Docket ever needs a dependency, a network connection or an account.

## Quickest route to value

```bash
docket init                                   # write a docket.toml to edit
docket demo                                   # see the whole loop in ~2 seconds, offline
docket run --max-usd 5 -- claude -p "fix the failing test"   # bound a real agent
docket explain                                # where the money went, with evidence
docket verify .docket/receipts/DKT-*.receipt.json             # check a receipt offline
```

Any agent, any language, no SDK required — Docket supervises the process and stands at the
network boundary:

```bash
docket run --max-usd 5 -- claude -p "…"        # provider base URLs are injected
docket run --max-usd 5 -- codex exec "…"
docket run --max-usd 5 -- aider --message "…"
docket run --max-usd 5 --http-proxy -- ./your-agent   # everything through the boundary
```

For agents you write yourself, ten lines of SDK:

```python
import docket

with docket.Session(max_usd=2.00) as s:
    s.record_model_call(model="claude-sonnet-5", input_tokens=1200, output_tokens=340)
    s.record_tool("read_file", {"path": "src/app.py"}, result_bytes=41_000)   # args stored as a digest
    s.record_note("about to refactor")
```

…or plain HTTP from any language, because the boundary is just a loopback proxy:

```bash
curl -X POST "$DOCKET_PROXY_URL/docket/tool/call" \
     -d '{"name":"sql.query","args_digest":"9f86d0…","result_bytes":2048}'
```

## Declare the rules once

```toml
# docket init  →  docket.toml
[policy]
name = "team-default"

[budget]
mode = "usd"              # "usd" for metered APIs, "quota" for subscriptions
                          # (quota mode counts tokens and calls; dollars are not guessed)

[caps]
max_usd = 5.00            # hard stop at the boundary
max_tokens = 2_000_000
max_model_calls = 200
max_wall_seconds = 1800
warn_at = 0.8             # emit cap.warning at 80%
on_breach = "kill"        # or "deny": refuse further calls, leave the process alive
grace_seconds = 5.0       # SIGTERM, then SIGKILL after this long

[models]
deny = ["*opus*"]         # premium models become an explicit, per-task decision
allow = []                # if non-empty, only these patterns are permitted

[files]
watch = ["."]
ignore = [".git/**", ".venv/**", "node_modules/**"]
deny_write = [".env", "**/secrets/**"]     # detected, hashed, reported as a violation

[egress]
mode = "deny"             # "deny": refuse non-allowlisted hosts
                          # "tunnel": record and forward them (default in `docket init`)
allow = ["api.anthropic.com", "api.openai.com", "pypi.org"]
```

Override the price table for a single run (enterprise discounts, self-hosted models,
a currency of your own): `docket run --prices my-prices.json -- your-agent`.

Full reference, including every field and its defaults: [`docs/policy-reference.md`](docs/policy-reference.md).

## Verify a receipt yourself

Offline, no account, no network, nothing installed but Python:

```console
$ docket verify .docket/receipts/DKT-20261013-746EF4.receipt.json --ledger .docket/runs/e6af3d172073.jsonl
VERIFIED  DKT-20261013-746EF4
  PASS  format: docket/receipt/v1
  PASS  ledger-chain: 61 events, every hash and link recomputed
  PASS  head: head matches receipt (58c6d03ede6734d7…)
  PASS  event-count: 61 events
  PASS  merkle-root: da8d8f511ce9db64…
  PASS  ledger-file-digest: sha256 dd75a53afad8fad4…
  PASS  usage-inclusion-proofs: 15 usage rows proven against the Merkle root and re-bound to the recomputed ledger
  PASS  cost-recomputation: 15 calls recomputed from token counts
  PASS  totals: total 2.000500 agrees with recomputation
  PASS  token-totals: 647,420 tokens
  SKIP  seal: receipt is not sealed
  SKIP  signature: receipt is not signed
  PASS  limits-disclosure: receipt states what Docket could and could not observe
```

The same receipt, checked by an **independent implementation in a different language**
(Node standard library only, no dependencies, no network):

```console
$ node verify-js/verify.mjs .docket/receipts/DKT-20261013-746EF4.receipt.json \
      --ledger .docket/runs/e6af3d172073.jsonl
VERIFIED  DKT-20261013-746EF4
  PASS  ledger-chain: 61 events verified
  PASS  merkle-root: da8d8f511ce9db64…
  PASS  usage-inclusion-proofs: 15 usage rows proven against the Merkle root
  PASS  cost-recomputation: 15 calls recomputed from token counts
  PASS  totals: total 2.000500 agrees with recomputation
  PASS  ledger-file-digest: sha256 dd75a53afad8fad4…
  SKIP  signature: receipt is not signed
```

Sealed or signed receipts verify too — the seal needs the operator's key, the signature only the
public key in the receipt (pin the one you expect with `--public-key`):

```console
$ docket verify DKT-*.receipt.json --seal-key .docket/keys/seal.key --public-key 3c3adc3f…
VERIFIED  DKT-…-85A1F6
  PASS  seal: HMAC-SHA256 valid (key 95a17907576b3027)
  PASS  signature: Ed25519 signature valid
```

Exit codes: `0` verified, `1` not verified, `2` usage/IO error — so this drops straight into
CI. If any check cannot be performed, it is reported as `SKIP` with a reason, never as a
pass; a verifier that overstates its coverage is worse than no verifier.

## How it compares

Everything below is good software. Docket is deliberately narrow: a *bound*, a *record* and
a *proof*, none of which require your data to leave the machine.

| | enforceable hard caps | works with subscription/CLI agents | evidence is third-party verifiable | local, no account | framework-agnostic |
| --- | --- | --- | --- | --- | --- |
| **Docket** | yes, at the boundary | yes (base URL or `HTTP_PROXY`) | **yes — offline, any language, cost recomputed** | yes | yes (proxy + SDK + HTTP) |
| AI gateways (LiteLLM, Portkey, Bifrost, Cloudflare/Vercel AI Gateway) | yes — but your keys and traffic route through the gateway | partially (subscription/OAuth flows often cannot be routed) | no — you trust the gateway's own logs | no (SaaS, or self-hosted server) | yes, at the HTTP layer |
| Observability platforms (Langfuse, Helicone, Phoenix, OpenLLMetry) | no — they record, they do not stop | partially | no | no (SaaS-first) | yes |
| Record/replay cassettes (openvcr, agentrec, cs-vcr, vcrpy, many more) | no | yes | no — they make tests fast, they do not make claims checkable | yes | varies |
| Usage monitors for a single agent | alerts only | yes | no | yes | single-vendor |
| OS audit tooling (auditd, eBPF agents) | partially (filesystem/exec) | yes | not for model traffic or cost | yes | yes |

A receipt answers a question none of the others answer: *"prove what your agent did to my
repository — offline, without giving me your logs, and let me recompute the cost myself."*
Longer analysis, including what each alternative is better at: [`docs/comparison.md`](docs/comparison.md).

## What Docket does not do

Docket is not a sandbox and does not pretend to be one. Say this out loud before trusting it:

- it **cannot veto another process's filesystem writes** — changes under `deny_write` are
  detected, hashed and reported as violations, not prevented;
- it **does not see traffic that bypasses its boundary** — an agent pointed directly at a
  provider, or HTTPS tunnels while `egress.mode = "tunnel"`;
- it **records no prompt or completion content by default** — digests and token counts only,
  unless you pass `--record-bodies`;
- a receipt **proves the record is intact, not that the record is complete**: it carries no
  trusted timestamp, and it says nothing about anything outside the supervised process tree;
- provider-reported token counts are taken as given — a provider that lies produces a
  consistent but false record.

Every receipt embeds a `limits_disclosure` block stating exactly these limits, and
`docket verify` fails a receipt that omits it. Full treatment:
[`docs/threat-model.md`](docs/threat-model.md).

## How it works

```
   your agent (any language)                docket run
 ┌────────────────────────┐        ┌──────────────────────────────┐
 │  claude / codex / sdk  │  HTTP  │  boundary proxy (loopback)   │──▶ provider API
 │                        │───────▶│  · cap + model policy checks │
 └──────────┬─────────────┘        │  · usage → cost accrual      │
            │ writes files         └──────────────┬───────────────┘
            ▼                                     │ hash-chained events
     watched filesystem ◀── snapshot diff ────────┤
                                                  ▼
                                        .docket/runs/<id>.jsonl
                                        .docket/receipts/DKT-*.receipt.json
                                                  │
                                    ┌─────────────┴──────────────┐
                                    │ verify (Python, JS, yours) │
                                    └────────────────────────────┘
```

- Reference implementation: ~4,900 lines of Python, **zero runtime dependencies**.
- Format specification: [`spec/docket-receipt-1.md`](spec/docket-receipt-1.md) — canonical
  JSON, hash chain, RFC 6962 Merkle tree, signing, verification procedure, honest limits.
- Independent verifier: [`verify-js/verify.mjs`](verify-js/verify.mjs) — Node standard library only.
- Conformance vectors: [`spec/vectors/`](spec/vectors/) — committed bytes both
  implementations must reproduce, regenerated only on a deliberate format change. That is
  what makes "portable format" mean something rather than sounding good.

One format decision worth calling out: **hashed structures contain no floating-point
numbers** — integers only, and fractional values as decimal strings. Python serialises `5.0`
as `5.0`; JavaScript serialises it as `5`. A receipt that verifies in one runtime and fails
in the other is worse than no receipt, so the canonical form forbids the entire class of bug
and implementations must *reject* it rather than guess. Writing this rule down caught three
real bugs in this repository, and it is checked by tests and by the committed vectors.

## Documentation

| doc | what it covers |
| --- | --- |
| [`docs/quickstart.md`](docs/quickstart.md) | five minutes, from install to a verified receipt |
| [`docs/concepts.md`](docs/concepts.md) | ledger, receipt, boundary, caps, findings — the mental model |
| [`docs/policy-reference.md`](docs/policy-reference.md) | every policy field, default and failure mode |
| [`docs/cli.md`](docs/cli.md) | every command and flag, with exit codes |
| [`docs/integrations.md`](docs/integrations.md) | Claude Code, Codex, Gemini CLI, aider, custom SDK agents, CI |
| [`docs/verification.md`](docs/verification.md) | how to verify, what each check means, and how to write your own verifier |
| [`docs/threat-model.md`](docs/threat-model.md) | what Docket can and cannot establish, attacker by attacker |
| [`docs/comparison.md`](docs/comparison.md) | honest positioning against gateways, observability and cassettes |
| [`docs/architecture.md`](docs/architecture.md) | module map and the invariants each one owns |
| [`docs/faq.md`](docs/faq.md) | the awkward questions, answered up front |
| [`spec/docket-receipt-1.md`](spec/docket-receipt-1.md) | the normative format specification |

## Development

```bash
git clone https://github.com/docket-dev/docket && cd docket
make test        # 158 tests, offline, no API keys, ~30s
make demo        # the demo above, with the tamper demonstration
make verify-js   # independent verifier conformance (needs Node; no npm install)
make lint        # ruff
```

Contributions are welcome: [`CONTRIBUTING.md`](CONTRIBUTING.md), [`ROADMAP.md`](ROADMAP.md),
[`GOVERNANCE.md`](GOVERNANCE.md). Security reports: [`SECURITY.md`](SECURITY.md). Good first
issues: [`good first issue`](https://github.com/docket-dev/docket/labels/good%20first%20issue).

## Sponsorship

Docket exists to make agent runs *bounded and provable* by default — including for people
who will never pay for a dashboard. Sponsorship funds the work no single vendor would fund
alone:

- a **frozen, versioned format**: the spec, the conformance suite, the test vectors;
- **independent verifiers** in Go, Rust and TypeScript, so a reviewer can check a receipt
  with whatever is already installed on their machine;
- **1.0 and long-term support**: security fixes and format stability for a stated window;
- **the public record**: a maintained corpus of runaway-agent incidents with sources, and
  Failure-of-the-Month write-ups of real receipts.

Tiers and what each one funds: [`SPONSORS.md`](SPONSORS.md) ·
[`docs/sponsorship.md`](docs/sponsorship.md).
→ [**Sponsor Docket**](https://github.com/sponsors/docket-dev)

## Licence

Apache-2.0 — see [`LICENSE`](LICENSE). The format specification
(`spec/docket-receipt-1.md`) is additionally offered under CC BY 4.0, so that anyone may
implement it, including in a competing product. The format is the point; the tool is
replaceable.
