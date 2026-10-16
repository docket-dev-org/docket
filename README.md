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

┌─ DOCKET DKT-20261015-746EF4 ───────────────────────────────────────────────┐
│ /usr/local/bin/python3 -m docket.demo --child                              │
└────────────────────────────────────────────────────────────────────────────┘
  exit       42  (stopped: max_usd)
  duration   160ms
  spend      $2.00 of $2.00 cap
  calls      15   denied: 1
  latency    p50 12ms · p95 23ms · p99 23ms · max 23ms  (15 call(s), nearest-rank)
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
  ✓ 62 ledger events in order, hash-chained and Merkle-rooted
  ✓ $2.00 recomputable from provider token counts and the embedded price table
  ✓ 3 filesystem change(s) with content digests
  ✗ does not observe: traffic that does not pass through the Docket boundary (e.g. an agent configured with a direct provider URL, or HTTPS tunnels when egress.mode = tunnel)
  ✗ does not observe: prompts, completions or file contents unless --record-bodies is enabled

VERIFY IT YOURSELF (offline, no account)
  docket verify DKT-20261015-746EF4.receipt.json
  node verify-js/verify.mjs DKT-20261015-746EF4.receipt.json

VERIFIED  DKT-20261015-746EF4
  PASS  format: docket/receipt/v1
  PASS  ledger-chain: 62 events, every hash and link recomputed
  PASS  head: head matches receipt (39ebf3716d8dee52…)
  PASS  event-count: 62 events
  PASS  merkle-root: 6103dbfd35035efc…
  PASS  usage-inclusion-proofs: 15 usage rows proven against the receipt's claimed Merkle root and re-bound to the recomputed ledger
  PASS  usage-rows-rebound: 15 usage rows re-bound to the recomputed ledger
  PASS  cost-recomputation: 15 calls recomputed from token counts
  PASS  totals: total 2.000500 agrees with recomputation
  PASS  token-totals: 647,420 tokens
  PASS  latency-summary: p50 12 ms · p99 23 ms over 15 measured call(s), nearest-rank
  SKIP  seal: receipt is not sealed
  SKIP  signature: receipt is not signed
  PASS  limits-disclosure: receipt states what Docket could and could not observe
```

Then break it on purpose — one byte, in the middle of the ledger:

```console
$ docket demo --tamper
…
  Now tampering with one byte of the ledger and verifying again:
NOT VERIFIED  DKT-20261015-746EF4
  PASS  format: docket/receipt/v1
  FAIL  ledger-chain: broken at event #6: content hash mismatch (event was modified after it was written)
  SKIP  head: not checked: the chain is broken at event #6
  PASS  event-count: 62 events
  FAIL  merkle-root: recomputed 8792629ac9da7829… != receipt 6103dbfd35035efc…
  FAIL  usage-rows-rebound: usage row 0 (seq 6) does not match the recomputed ledger event
  PASS  usage-inclusion-proofs: 15 usage rows proven against the receipt's claimed Merkle root
  FAIL  usage-rows-rebound: 1 of 15 usage rows are not the events the recomputed ledger has at those
        sequence numbers (14 re-bound)
  PASS  cost-recomputation: 15 calls recomputed from token counts
  PASS  totals: total 2.000500 agrees with recomputation
  PASS  token-totals: 647,420 tokens
  PASS  latency-summary: p50 12 ms · p99 23 ms over 15 measured call(s), nearest-rank
  SKIP  seal: receipt is not sealed
  SKIP  signature: receipt is not signed
  PASS  limits-disclosure: receipt states what Docket could and could not observe
  PASS  evidence-completeness: every event reached the ledger file

  Try it on your own agent:
    docket run --max-usd 5 -- my-agent --flag
```

One real capture. The docket number reproduces (the demo seeds its own RNG); the Merkle roots do
not, because they cover the session's own fields including a per-run temporary path.

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
6. **Rollback, when you ask for it** (`--snapshot`, `--rollback-on-breach`): a content snapshot
   of the watched tree taken before the agent starts, restored automatically when a cap fires —
   and restorable later by hand with `docket restore`, which refuses to overwrite anything that
   changed after the run. It is off by default, it prints what it cannot cover, and it will not
   write outside the tree it was pointed at.
7. **A response cache that can be audited** (`--cache`): a repeated request is answered at the
   boundary without contacting the provider, every hit is a `model.cache_hit` event, and the money
   it saved is *recomputed by the verifier* from the call it replayed — never folded into the
   spend, which keeps describing money that was actually paid. A cached loop spends nothing, so no
   dollar cap can end it; `cache.max_hits` is the brake that makes it start paying again, and the
   moment it engages is in the ledger. See [the response cache](docs/caching.md).

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
- **Does not undo what it never copied:** a rollback restores the files the snapshot covered.
  Anything created during the run is removed; anything the snapshot skipped is listed as not
  covered, because "the tree is exactly as it was" is a claim a snapshot cannot always support.
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

## Undoing what a run did

Every other feature here observes. `--snapshot` and `--rollback-on-breach` are the two that
write to your tree, and they are opt-in for that reason: a tool that silently copies your
working directory into `.docket/` on every run is not a tool you leave installed.

Cap the agent below what it will spend and it gets stopped; ask for the tree back and it comes
back — this is one real run, captured whole:

```console
$ docket run --snapshot --rollback-on-breach --max-usd 0.01 -- python -m docket.scriptagent
…
  files      1 changed (+0 ~1 -0)
  undo       snapshot: 3 file(s), 1.5 KiB stored  1 not covered
  rollback   applied (cap breached): 1 restored
docket: the run breached max_usd and 1 file(s) changed: the watched tree has been put back
        from the snapshot (1 restored). `docket restore --help` shows the same restore by hand.
```

The same restore, by hand, months later — dry run first, then applied. Note the third command:
somebody edited the file after the run, so the restore refuses and exits `1`:

```console
$ docket restore .docket/latest.receipt.json
  docket restore — run 017aa8b8e4ac into /home/you/project

  would restore src/agent.py -> 4efcb38c3e18

  dry run: nothing was written. Add --apply to restore these paths.

  the snapshot could not cover 1 file(s):
    models/big.bin — 12000000 bytes is over the per-file snapshot ceiling of 8388608

$ docket restore .docket/latest.receipt.json --apply
  REFUSED     src/agent.py -> 4efcb38c3e18 — the file changed after the run (expected
              4efcb38c3e18…, found 5ad82ad71d5c…); re-run with --force to overwrite it

  applied: {'refused': 1}
$ echo $?
1

$ docket restore .docket/latest.receipt.json --apply --force
  restored    src/agent.py -> 4efcb38c3e18

  applied: {'restored': 1}

  recorded: .docket/restores/20261014T203014Z-DKT-20261014-58CBC2.json

$ docket restore .docket/latest.receipt.json --apply     # and again, for good measure
  already restored src/agent.py -> 4efcb38c3e18

  applied: {'already': 1}
```

(Two lines wrapped for print width — the tool emits one line per path. The tree was a scratch
directory; the run is one real run, `DKT-20261014-58CBC2`.)

That middle refusal is the point. Somebody edited `src/agent.py` after the run — often to fix
what the agent broke — and a restore that silently discards it is a worse bug than the overspend.
Six rules follow from the same reasoning:

| rule | why |
| --- | --- |
| The plan comes from the receipt, the ledger and the manifest — never from re-walking the tree | so anyone holding the same two files can audit (and re-derive) what a restore will do before it runs |
| A file edited after the run is refused unless `--force` | the fix for an agent's mess is usually a human edit; overwriting it is data loss dressed up as recovery |
| Only paths inside the run's tree are ever touched | the path list comes from documents, so it is checked, not trusted: no absolute paths, no `..`, no symlinked components, blobs located by digest, modes masked to permission bits ([ADR 0004](docs/adr/0004-content-snapshots-and-confinement.md)) |
| It never claims completeness | files the snapshot could not cover (too large, symlinked, outside the tree) are named in `ops.snapshot.not_covered`, and `restore` exits `1` if any path was refused |
| Running it twice is success, not a scare | a file that already holds the snapshot's bytes is reported `already restored` — without that branch, the second run refused it as "changed after the run", blaming the operator (or Docket itself) for an edit nobody made |
| It leaves a record of its own | an applied restore writes `.docket/restores/<utc>-<docket-number>.json` (`docket/restore/v1`): what it planned, what it did, what it refused, and whether `--force` was used. The run's ledger and receipt are never edited: they are historical observations, and appending to them afterwards would break the receipt's agreement with the ledger it cites |

A tree that does not fit the snapshot budget is refused **before the agent starts** — a partial
snapshot invites restoring over a file it never captured, which is the one failure mode a
rollback tool cannot recover from.

## Install

```console
pip install docket          # Python 3.11+, zero runtime dependencies
pipx install docket         # or keep it out of your project venvs
uvx docket demo             # or run it without installing anything
```

From source: `git clone … && cd docket && make check` (1027 tests, fully offline — no API keys,
no network, ~90 s).

## Documentation

| | |
| --- | --- |
| [Quickstart](docs/quickstart.md) | install → first capped run → verify, in five minutes |
| [Concepts](docs/concepts.md) | boundary, ledger, receipt, findings, caps |
| [CLI reference](docs/cli.md) | every command and flag, exit codes, environment |
| [Policy reference](docs/policy-reference.md) | the whole `docket.toml` schema |
| [Response cache](docs/caching.md) | what a hit is, what it is not, the brake it needs, and the measured cost |
| [Verification](docs/verification.md) | what each check means and how to run it in CI |
| [Integrations](docs/integrations.md) | Claude Code, Codex, Gemini CLI, opencode, aider, SDKs, HTTP |
| [Threat model and limits](docs/threat-model.md) | what Docket does not defend against |
| [Testing](docs/testing.md) | what is tested, by kind, and what is deliberately not |
| [Measurements](docs/measurements/2026-10-15-2core/README.md) | throughput, client latency and memory from four real runs, with the host and the limits stated |
| [Resilience](docs/resilience.md) | breakers, throttles, timeouts, caps, the evidence guard — and what each one records |
| [Deployment](docs/deployment.md) | containers, pods, the Helm chart, and what the deploy check does and does not prove |
| [Production coverage](docs/production-coverage.md) | 77 production-engineering concepts, each with what exists here and where |
| [Standards](docs/standards.md) | the norm or paper behind each mechanism — RFCs, CAP, Sagas, the tail at scale, Chaos Engineering — and where Docket departs |
| [Resilience practice coverage](docs/sre-coverage.md) | 22 resilience practices — stress, fuzzing, chaos, game days, red teaming, FMEA, post-mortems — and which are actually practised |
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

Early. `0.1.0`, dated 2026-10-13 (see [CHANGELOG.md](CHANGELOG.md)). The receipt format is
versioned and frozen by a written spec with test vectors; the CLI may still move. One maintainer, no employer, no funding — see
[SPONSORS.md](SPONSORS.md) for what funding would and would not buy.

Nothing here is a security boundary you should rely on for isolation: Docket is an accounting
and evidence layer that happens to enforce budgets. Use containers or a sandbox when the
threat is a hostile agent; use Docket to know what happened, put a number on it, and prove the
number.

## Contributing

Bug reports, "this estimate is wrong" reports, and format critiques are all welcome. The two
rules that matter: **the test suite must stay offline**, and **nothing that hashes a structure
may change without a spec change** (a format break invalidates every receipt ever issued).
See [CONTRIBUTING.md](CONTRIBUTING.md); for where to ask questions, what to include in a report, and
what this project does not support, see [SUPPORT.md](SUPPORT.md).

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
