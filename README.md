# docket

[![CI](https://github.com/docket-dev-org/docket/actions/workflows/ci.yml/badge.svg)](https://github.com/docket-dev-org/docket/actions/workflows/ci.yml)
[![version](https://img.shields.io/badge/version-0.1.8-blue)](https://docket-dev-org.github.io/docket/CHANGELOG/)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/docket-dev-org/docket/blob/main/LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)

A local budget-and-evidence layer for AI agents: it caps what a run can spend, records every model call, tool call, and file change in a hash-chained ledger, and emits a receipt that a third party verifies offline, without the ledger, the tool, or network access.

Docket wraps the agent you already run. It needs no account, no server, no telemetry, and no API keys of its own.

> [!NOTE]
> Docket 0.1.8 is on PyPI and installs with `pip install docket-ai`. The `docket/receipt/v1` format is versioned and frozen: the specification in `spec/` and its 44 committed conformance vectors define it, and three implementations that share no code are held to that contract, so a receipt written by 0.1.0 verifies under any later release. The command line follows semantic versioning. Exit codes, flags, and the JSON shape of every command are part of the interface and stay stable across 0.1.x; the format tag changes only under the process in [CONTRIBUTING.md](https://docket-dev-org.github.io/docket/CONTRIBUTING/#changing-the-format), and never silently.

---

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://docket-dev-org.github.io/docket/assets/logo-horizontal-dark.svg">
  <img src="https://docket-dev-org.github.io/docket/assets/logo-horizontal.svg" alt="Docket: a local budget-and-evidence layer for AI agents" width="250" height="80">
</picture>

## Why Docket?

Agent runs spend money and change files, and neither the spend nor the change arrives with a record you can hand to someone else.

- **The spend has no ceiling inside the agent.** A retry loop, a prompt that stopped being cached, or an agent re-creating cloud resources bills at machine speed. The cap has to bind *outside* the process, because the process is the thing that is wrong.
- **The change has no account.** Files are edited, secrets are read, and egress goes wherever the SDK points. A crash log tells you the process died; it does not tell you what it did first.
- **The evidence has no owner.** Dashboard screenshots, log lines, and provider invoices are three different systems that disagree, and none of them can be checked by the person you send them to.

Existing tooling covers parts of this problem. None of it covers the seam between spending, acting, and proving:

| what you might reach for | what it does well | where it stops |
| --- | --- | --- |
| An LLM gateway or proxy | Routes traffic, adds keys, meters usage per tenant | Records only traffic it sees, keeps the record in its own database, and the invoice it produces is the vendor's word |
| Observability or APM | Traces, spans, dashboards for everything | After the fact; sampling loses the events you want; nothing is tamper-evident |
| A sandbox or container | Prevents writes and egress it was told to prevent | Prevention is not accounting: "it could not write there" is not "it wrote this, here is the digest" |
| LLM record/replay tools | Replays previous calls, makes tests deterministic | Built for development, not custody; no caps, no file evidence, no third-party verification |

Docket is designed around five commitments that follow from those gaps:

1. **Account, do not pretend to sandbox.** Docket proves what happened. For prevention, use a container, and read [the threat model](https://docket-dev-org.github.io/docket/docs/threat-model/) for the line between the two.
2. **Caps bind outside the agent.** The boundary refuses the request that would cross the cap, and the supervisor terminates the process group. The agent cannot be prompted out of it.
3. **Verification must not require trust.** A receipt is checked by recomputing hashes and costs from the file itself, by three independent implementations, under a written [format specification](https://docket-dev-org.github.io/docket/spec/docket-receipt-1/).
4. **The record states its own limits.** Every receipt carries `limits_disclosure`: what Docket could see in that run and what it could not. Silence is never the default.
5. **The money must be recomputable.** Costs are recomputed by the verifier from raw token counts and the embedded price table, so a stranger does not have to believe your arithmetic.

## Architecture and how it works

```text
   your agent ──────► Docket supervisor ──────► recording boundary ──────► provider API
   (any process)            │                          │
                            │                          ├─ model calls   (+ tokens, cost)
                            │                          ├─ refusals      (caps, egress, breaker)
                            │                          └─ cache hits    (when --cache)
                            ├─ filesystem watcher ────► file changes   (+ content digests)
                            └─ lifecycle ─────────────► start, exit, tool calls, policy events
                                                       │
                                                       ▼
                              .docket/ledger.jsonl   canonical JSON, hash-chained, Merkle-rooted
                                                       │
                                                       ▼
                              .docket/receipts/DKT-<date>-<id>.receipt.json
                                                       │
                        ┌──────────────────────────────┼──────────────────────────────┐
                        ▼                              ▼                              ▼
                 docket verify                verify-js/verify.mjs           verify-go/docket-verify
                 (Python reference)           (Node, zero dependencies)      (Go, clean-room)
```

1. **`docket run` starts your command as a child process** and sets the provider base URLs it finds in the environment to a local boundary (`--http-proxy` additionally sets `HTTP_PROXY` and `HTTPS_PROXY` for agents that honour them).
2. **The boundary forwards each model request** to the upstream provider, counts tokens, prices the call, and appends an event. When a cap would be crossed it refuses with HTTP 403 (`docket_cap_exceeded`) and the supervisor stops the process group with exit code `42`.
3. **The ledger appends canonical JSON** with no floats, one event per line, each event carrying the hash of the previous one. A filesystem watcher adds events for changed files with their content digests.
4. **At exit the supervisor writes a receipt**: totals, policy, environment, per-model breakdown, an RFC 6962 Merkle root over every event, and the price table used.
5. **The verifier recomputes everything it can** from the receipt alone: chain links, Merkle paths, per-call cost, totals, and latency percentiles. It reports per check `PASS`, `FAIL`, or `SKIP` with a reason.

### What a receipt proves, and what it does not

- **Proves:** the recorded events are in order and unaltered since they were written; the totals agree with the recorded token counts and the embedded price table; the listed files changed in the listed ways; which calls were refused and why.
- **Does not prove:** that the ledger contains everything the machine did. Traffic that never crossed the boundary is invisible, whether an agent pointed at a provider URL directly or a tunnel allowed by `egress.mode = "tunnel"`.
- **Does not prevent:** writes outside the boundary, or reads the process was permitted to make. `deny_write` is detection and evidence, not a veto: user space cannot intercept another process's syscalls without `ptrace` or a container.
- **Does not sign by default:** sealing is an HMAC with your key; signing is Ed25519 with a key someone else can verify. Without either, a receipt is tamper-evident against the ledger but not against someone who can rewrite both files.

> [!WARNING]
> Docket is an accounting and evidence layer that enforces budgets, not a sandbox. When the threat is a hostile agent, isolate it with a container and use Docket to record and price what it did.

### Three implementations, one format

The format is only worth what an independent implementation can reproduce, so the repository holds three verifiers that share no code: the Python reference, a zero-dependency Node one, and a clean-room Go one written from the specification alone. All three run against the same 44 committed conformance vectors, which are 2 valid documents plus a negative corpus of 42, of which 38 must be refused (6 at parse time, 32 by a named check) and 4 must be accepted. That is 132 verdicts that have to agree.

```console
$ make conformance
88 correct, 0 wrong, 0 not judged (2 positive, 42 negative vectors)
```

### Where the rest is written down

| page | what it answers |
| --- | --- |
| [Quickstart](https://docket-dev-org.github.io/docket/docs/quickstart/) | install, first capped run, and verification in five minutes |
| [Concepts](https://docket-dev-org.github.io/docket/docs/concepts/) | boundary, ledger, receipt, findings, and caps |
| [CLI reference](https://docket-dev-org.github.io/docket/docs/cli/) | every command and flag, with exit codes and environment variables |
| [Policy reference](https://docket-dev-org.github.io/docket/docs/policy-reference/) | the complete `docket.toml` schema |
| [Verification](https://docket-dev-org.github.io/docket/docs/verification/) | what each check means and how to run it in CI |
| [Integrations](https://docket-dev-org.github.io/docket/docs/integrations/) | Claude Code, Codex, Gemini CLI, opencode, aider, the SDKs, and HTTP proxying |
| [Threat model and limits](https://docket-dev-org.github.io/docket/docs/threat-model/) | what Docket does not defend against |
| [Comparison](https://docket-dev-org.github.io/docket/docs/comparison/) | Docket against gateways, observability, sandboxes, and record/replay tools |
| [Response cache](https://docket-dev-org.github.io/docket/docs/caching/) | what a cache hit is, the brake it needs, and the measured cost |
| [Testing](https://docket-dev-org.github.io/docket/docs/testing/) | what is tested, by kind, and what is deliberately not |
| [Format specification](https://docket-dev-org.github.io/docket/spec/docket-receipt-1/) and [Conformance](https://docket-dev-org.github.io/docket/spec/conformance/) | the normative `docket/receipt/v1` contract and the corpus that holds implementations to it |
| [Production coverage](https://docket-dev-org.github.io/docket/docs/production-coverage/) | 77 production-engineering concepts, each with what exists here and where |
| [Resilience practice](https://docket-dev-org.github.io/docket/docs/sre-coverage/) | 22 resilience practices, covering stress, fuzzing, chaos, game days, FMEA, and post-mortems, and which ones are practised |
| [Evidence](https://docket-dev-org.github.io/docket/docs/evidence/) | every problem claim with its source and its strength grade |

## Prerequisites

| requirement | version | needed for |
| --- | --- | --- |
| Python | 3.11 or newer | `docket` itself; the reference verifier; the test suite |
| `pip` and `venv` | bundled with Python 3.11+ | installing from the wheel or from a clone |
| Node.js | 20 or newer | optional: the second verifier (`verify-js/verify.mjs`) |
| Go | 1.27 or newer | optional: the third verifier (`verify-go/`), built from source |
| Disk | a few MB per run | ledgers and receipts under `.docket/` |
| Network | none for `demo`, `verify`, `selftest`, or the test suite | a real capped run needs the provider your agent already calls |

Docket runs on Linux and macOS. The Windows code paths exist, a separate file lock and a process-liveness probe, and CI exercises only Ubuntu, so treat Windows as supported but not covered by CI.

## Quick start

### 1. Install

To install Docket into your current Python environment, run:

```bash
# Requires: Python 3.11+, pip, and network access to pypi.org.
python3 -m pip install docket-ai
docket --version
```

To keep the package out of your project virtualenv, install it into its own isolated environment instead:

```bash
# Requires: Python 3.11+, pipx, and network access to pypi.org.
pipx install docket-ai
```

To install straight from the repository rather than the release, which tracks `main` instead of a pinned version, run:

```bash
# Requires: Python 3.11+, pip, git, and network access to github.com.
python3 -m pip install git+https://github.com/docket-dev-org/docket.git
```

> [!NOTE]
> The distribution is named `docket-ai` because PyPI administratively prohibits the bare name `docket`. PyPI keeps that list to prevent dependency-confusion attacks. Everything else is `docket`: the import is `import docket`, the console script is `docket`, and the command you type is `docket`. Only the name `pip install` takes differs.

### 2. Cap a run of your own agent

To create a starter policy and run any command under it, run:

```bash
# Requires: docket installed, and a command that exits.
docket init --name my-agent --max-usd 1
docket run --policy docket.toml -- python3 -c "print('hello from the agent')"
docket verify .docket/latest.receipt.json
```

```text
hello from the agent
docket: DKT-20261018-5FB7B5 completed · $0.00 of $1.00 cap · 0 calls · receipt .docket/receipts/DKT-20261018-5FB7B5.receipt.json

VERIFIED  DKT-20261018-5FB7B5
  PASS  format: docket/receipt/v1
  PASS  ledger-chain: 9 events, every hash and link recomputed
  PASS  head: head matches receipt
  PASS  event-count: 9 events
  PASS  merkle-root: recomputed root matches the receipt
  PASS  totals: 0 call(s), total 0.00 agrees with recomputation
  PASS  limits-disclosure: receipt states what Docket could and could not observe
  ledger     .docket/runs/30bb5ff6360b.jsonl (found beside the receipt)
```

One run, trimmed to the lines that matter; the docket number, the event count and the ledger name are yours. The chain, the head and the Merkle root are checked because `docket verify` finds the ledger beside the receipt, with no flag needed, and the last line names the file it used.

`.docket/latest.receipt.json` is a path every run rewrites, so the command works the second time and the hundredth time. Verify one specific receipt by naming it, as `docket verify .docket/receipts/DKT-20261018-5FB7B5.receipt.json`.

Point the command at your real agent and the same receipt carries its calls. Exit code `0` means the run ended normally; `42` means a cap stopped it.

### 3. See the failure modes

`docket demo` starts a real supervisor, a real recording boundary, and a scripted provider, then refuses the call that would cross a $2.00 cap:

```bash
# Requires: docket installed. No API key and no network are used.
docket demo
```

```text
  Scenario: an agent is asked to fix one failing test.
  It plans on an expensive model, reads a huge file, then gets stuck re-sending
  the same request. Nothing inside the agent will ever stop it.

  Policy: max_usd = $2.00, deny_write = [.env], watch = [.]

  running (real supervisor, real boundary, scripted provider)…
    agent: the boundary refused this call (403, max_usd)
    agent: stopping here. It would have kept going.

  exit       42  (stopped: max_usd)
  spend      $2.00 of $2.00 cap
  calls      15   denied: 1
  tokens     647,420 (in 642,900 · cached 0 · out 4,520)
  files      3 changed (+2 ~1 -0)  1 deny-write violation(s)

FINDINGS  (deterministic unless marked heuristic)
  CRITICAL Runaway loop: 13 identical model calls  [$1.62]
  HIGH     1 file(s) inside a deny_write scope were changed
  MEDIUM   Repeated context appears to be uncached  [$2.89]
```

The transcript is trimmed to the lines that matter; every line in it comes from one real run, and the full output names the rule behind each finding and what to change.

To see tamper detection, run the same demo with one byte of the ledger flipped:

```bash
# Requires: docket installed. Offline.
docket demo --tamper
```

The demo ends by printing the exact command that reproduces the verdict below, receipt and ledger included. One run of it, trimmed:

```text
NOT VERIFIED  DKT-20261018-746EF4
  FAIL  ledger-chain: broken at event #7: content hash mismatch (event was modified after it was written)
  SKIP  head: not checked: the chain is broken at event #7
  FAIL  merkle-root: recomputed ded1ee862e4a1df7… != receipt 4b3ccfcfb84a1449…
```

The event number, both hashes, and the date inside the docket number differ on every run; the verdict and the three failing checks do not. The receipt itself is intact and the ledger beside it is the doctored file, so `docket verify <receipt>` finds that ledger and fails the same way. The receipt does not become "unreadable". It becomes specific about exactly where the record stops being trustworthy.

## Core workflows and CLI reference

| command | what it does | what you get |
| --- | --- | --- |
| `docket demo` | Runs a scripted runaway agent under a real boundary | A receipt, its findings, and a verification report; `--tamper` adds the doctored run |
| `docket init` | Writes a commented starter policy | `docket.toml` with caps, watch scope, egress allow list, and resilience defaults |
| `docket run -- <command>` | Runs any command under the policy | A ledger and a receipt per run; exit `42` on a cap, `43` if the policy refuses to start |
| `docket verify <receipt>` | Verifies a receipt offline | One line per check; exit `1` if any `FAIL`; the ledger beside the receipt is used unless `--ledger` names another; `--impl node` hands it to the independent Node verifier the wheel carries |
| `docket report <receipt>` | Renders the receipt | Terminal summary or Markdown for a change request or incident ticket |
| `docket explain <receipt>` | Re-derives findings from the ledger | Where the money and the risk went, with the rule and evidence behind each finding |
| `docket diff <a> <b>` | Compares two runs | Cost, calls, models, tools, and file deltas, which is a regression check for agents |
| `docket restore <receipt>` | Rolls back a `--snapshot` run | A dry run by default; `--apply` restores, refusing files edited after the run |
| `docket prices` | Shows the bundled price table | Per-model prices with their provenance and revision date |
| `docket doctor` | Checks the local environment | Ledger health, damaged or unreadable files, disk floor, and configuration problems |
| `docket selftest` | Exercises the boundary's own guarantees | Forwarding, caps, breaker, fault injection, and ledger-failure handling, offline |
| `docket chaos -- <command>` | Runs a command under injected faults | A receipt that records what was injected and what the agent did about it |
| `docket game-day` | Runs scripted failure phases | A post-mortem document and a receipt per phase |

Exit codes are part of the interface:

| code | meaning |
| --- | --- |
| `0` | the command exited normally |
| `1` | `verify`, `report`, `explain` or `diff` found a problem, or the receipt could not be read; `restore` also uses `1` when there is no snapshot to restore |
| `42` | a cap was crossed and the run was stopped |
| `43` | the policy could not be loaded: missing file, invalid TOML, or a value out of range |
| `70` | internal error |
| `130` | interrupted (Ctrl-C) |

`docket run` hands back the exit code of the command it wrapped, so a pipeline sees what the agent saw: `docket run --policy docket.toml -- ./build.sh` exits `0` when the build passed and `1` when it failed. `42` and `43` are Docket's own codes and cannot collide with a child's, because the supervisor writes them over the child's code only when it stopped the run itself.

Four workflows cover most of the work:

1. **Cap a coding agent.** Run `docket run --max-usd 5 -- claude` and read the receipt when the task ends. The table of agents and their flags is in [Integrations](https://docket-dev-org.github.io/docket/docs/integrations/).
2. **Gate a pipeline on evidence.** Run `docket verify .docket/latest.receipt.json --json` in CI; a non-zero exit fails the job, and the JSON names the check that failed. The ledger beside the receipt is checked too, so a chain break fails the gate. See [Verification](https://docket-dev-org.github.io/docket/docs/verification/).
3. **Check for drift after a prompt change.** Run `docket diff old.receipt.json new.receipt.json` and compare cost, call count, and models before you ship the change.
4. **Answer "why should I believe you?" without a clone.** Run `docket verify --impl node receipt.json` and get a verdict from a program that shares no code with this one.

## Configuration

`docket init` writes `docket.toml`. Every key below is real, and the comments state the default the file ships with:

```toml
[policy]
name = "my-agent"

[budget]
mode = "usd"                # "usd" for metered APIs; "quota" for subscription plans (tokens, not dollars)

[caps]
max_usd = 5                 # the boundary refuses the call that would cross this
warn_at = 0.8               # emit a cap.warning event at 80% of the cap
on_breach = "kill"          # "kill" stops the process group; "deny" refuses calls and leaves it alive
grace_seconds = 5.0         # SIGTERM, then SIGKILL after this many seconds

[files]
watch = ["."]               # tree recorded in the receipt, changes digested
deny_write = [".env"]       # violations are recorded and reported, not prevented

[egress]
mode = "deny"               # "deny" refuses unlisted hosts; "tunnel" forwards and records them
allow = ["api.anthropic.com", "api.openai.com"]

[cache]
enabled = false             # off by default: a recorder must not change what your agent receives
max_hits = 1000             # a cached loop spends nothing, so it needs its own brake
```

Precedence runs command-line flag, then policy file, then built-in default. Three settings decide where the record goes and what the child knows about it:

- `--docket-dir` (default `.docket`) sets where ledgers and receipts live; `DOCKET_DOCKET_DIR` overrides it.
- Docket exports `DOCKET_PROXY_URL`, `DOCKET_SESSION`, `DOCKET_POLICY_DIGEST`, and `DOCKET_TOOL_VERSION` to the child process, so an instrumented agent can cite its own run.
- The full schema, including the resilience and cache sections, is in the [policy reference](https://docket-dev-org.github.io/docket/docs/policy-reference/).

## AI agent integration

Docket ships no MCP server and no `AGENTS.md`, and the omission is deliberate: Docket wraps a process, so an agent needs no protocol, no client library, and no knowledge that Docket exists. Three surfaces connect the two, in the order most teams need them.

1. **Wrap the process.** The agent keeps its own CLI and its own provider credentials; Docket sits above it.

   | your agent | command |
   | --- | --- |
   | Claude Code | `docket run --max-usd 5 -- claude` |
   | OpenAI Codex CLI | `docket run --max-usd 5 -- codex` |
   | Gemini CLI | `docket run --max-usd 5 -- gemini` |
   | aider, opencode, or any CLI | `docket run --max-usd 5 -- <command> …` |
   | a script using the OpenAI or Anthropic SDK | `docket run --max-usd 5 -- python agent.py` |
   | something that ignores `*_BASE_URL` | `docket run --http-proxy --max-usd 5 -- <command>` |

2. **Read the JSON.** `docket verify --json`, `docket demo --json`, and `docket prices --json` emit machine-readable results, so a supervising agent can decide what to do next from the check names rather than by parsing prose.

3. **Record in-process.** Code you control can open a session and record its own calls and tool use, which produces the same ledger and receipt as a wrapped run:

   ```python
   from docket import Session

   with Session(label="nightly-backfill") as session:
       session.record_tool("read_file", path="src/agent.py")
       session.record_model_call(model="claude-sonnet-5", input_tokens=1200, output_tokens=300)

   print(session.receipt_path)
   ```

If you need a protocol bridge, whether an MCP server, an internal agent framework, or an A2A gateway, the boundary already answers the provider's own HTTP API, so a bridge in front of it changes nothing about what is recorded. [Open an issue](https://github.com/docket-dev-org/docket/issues) with the interface you need and the repository will grow one rather than claim one today.

## Contributing

To run the suite exactly as CI runs it, run:

```bash
# Requires: Python 3.11+, and the repository checked out.
git clone https://github.com/docket-dev-org/docket.git
cd docket
PYTHONPATH=src python3 -m pytest tests -ra
```

That is 1252 tests in about three minutes, fully offline: no API keys, no network, no provider account. The whole gate is `make check` (1252 tests plus lint, the security audit, the Node verifier, the conformance corpus, and the deployment checks), which additionally needs Node.js 20+, `ruff`, and `pyyaml` from the `dev` and `deploy` extras.

Two rules matter more than style:

- **The suite stays offline.** A test that needs the network is a test that will fail in someone else's CI.
- **Nothing that hashes a structure changes without a spec change.** A format break invalidates every receipt ever issued.

Read [CONTRIBUTING.md](https://docket-dev-org.github.io/docket/CONTRIBUTING/) for the workflow and the review bar, [SUPPORT.md](https://docket-dev-org.github.io/docket/SUPPORT/) for where to ask questions and what to include in a report, [SECURITY.md](https://docket-dev-org.github.io/docket/SECURITY/) for vulnerability reports, and [GOVERNANCE.md](https://docket-dev-org.github.io/docket/GOVERNANCE/) for how decisions are made.

## License

Docket is licensed under the **Apache License 2.0** (SPDX: `Apache-2.0`); the full text is in [LICENSE](https://github.com/docket-dev-org/docket/blob/main/LICENSE). The specification text in `spec/` is licensed **CC BY 4.0** so that other implementations can copy it freely.

Docket is free and Apache-2.0, with no hosted tier; [SPONSORS.md](https://docket-dev-org.github.io/docket/SPONSORS/) says what support pays for and what it does not buy.

Receipts, ledgers, reports, snapshots, and restores that Docket writes are **yours**. The license covers Docket's source code; it makes no claim on the data you record with it, and the format is published so that a record outlives this tool.
