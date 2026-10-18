# Command-line reference

```
docket [--version] {run,restore,verify,report,explain,diff,demo,init,prices,
                    selftest,chaos,game-day,doctor} …
```

Exit codes are part of the interface, because scripts and CI depend on them:

| code | meaning |
| --- | --- |
| `0` | success (for `verify`: the receipt checks out) |
| `2` | a flag could not be used as written (`--max-usd nan`, `--max-calls -3`): argparse's own usage error, raised before any run, no workspace touched |
| `1` | not verified (`verify`), or a restore that refused or failed on a path (`restore`) |
| `42` | the supervised run was stopped because a cap was crossed |
| `43` | usage or policy error (bad policy file, nothing to run, missing receipt, a tree that does not fit the snapshot budget) |
| `70` | internal error (a bug — please report it with the ledger) |
| `130` | interrupted by `SIGINT` (Ctrl-C is forwarded to the supervised process) |

---

## `docket run [flags] -- <command> [args…]`

Runs a command under a policy, records everything observable, and writes a receipt.

| flag | default | meaning |
| --- | --- | --- |
| `-p, --policy PATH` | none | policy TOML. With no policy: no caps, everything observable recorded |
| `--max-usd AMOUNT` | off | override the policy's USD cap. Must be a finite, non-negative amount no larger than 1e12; for "no cap" omit the flag (the receipt discloses an unset cap as unset rather than as a number) |
| `--max-tokens N` / `--max-calls N` / `--max-events N` | off | override the token, model-call and ledger-event caps. Whole numbers, not negative |
| `--max-minutes M` | off | wall-clock cap, in minutes, more than zero |
| `--max-calls-per-minute N` | off | rate limit at the boundary (recorded as HTTP 429); at least 1 |
| `--docket-dir DIR` | `.docket` | where `runs/`, `receipts/`, `restores/` and `latest` live. If this directory is inside the watched tree it is excluded from the file diff *structurally* — Docket's own ledger, receipt and snapshot store are never reported as changes the agent made, and a rollback never plans to delete the record it came from. The exclusion is per-run and comes from the resolved paths, so it cannot be forgotten in a policy file; it is recorded as a `note` event and appears in the effective policy the receipt embeds. `--docket-dir .` puts the evidence in the watched tree itself: nothing is excluded, and the run says so on stderr|
| `--cwd DIR` | current directory | working directory for the child |
| `--profile {full,minimal}` | `full` | how many provider base-URL variables to inject (`minimal` sets only OpenAI/Anthropic) |
| `--http-proxy` | off | also export `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`, so *all* HTTP traffic can be recorded and subject to `[egress]` |
| `--record-bodies` | off | store request bodies in the ledger. Off by default because prompts are the most sensitive thing a user has |
| `--prices PATH` | bundled table | price table for this run |
| `--seal` | off | seal the receipt with a local HMAC key (`~/.docket/seal.key`, created on first use) |
| `--sign KEY_PATH` | off | sign the receipt with an Ed25519 private key (requires `docket[sign]`) |
| `--label TEXT` | none | human note stored in the receipt (e.g. the ticket number) |
| `--report {summary,full}` | `summary` | what to print to stderr when the run ends. The receipt is written either way |
| `--max-usd`, `--max-tokens`, `--max-calls`, `--max-minutes` | policy | per-run overrides |
| `--on-breach {kill,deny}` | policy | per-run override |
| `--budget-mode {usd,quota}` | policy | per-run override |
| `--max-calls-per-minute N` | policy | refuse calls beyond N per minute (recorded, answered `429`) |
| `--max-events N` | policy | ceiling on this run's own ledger events; the run stops at the ceiling rather than losing the record |
| `--fail-fast-evidence` | off | stop the run when an event cannot be written, instead of continuing with a disclosed gap |
| `--cache` | off | answer an identical request (same upstream path, same canonical body) from the boundary's response cache instead of the provider. Every hit is a `model.cache_hit` event, the provider sees nothing, and the saving is reported beside the spend, never inside it — [docs/caching.md](caching.md) |
| `--cache-ttl SECONDS` | `300` | how long an entry stays usable; `0` means entries never expire on time (capacity still bounds them). Turns the cache on |
| `--cache-max-entries N` | `256` | least-recently-used entries are evicted above this. At least 1. Turns the cache on |
| `--cache-max-hits N` | `1000` | ceiling on calls the cache may answer in this run; `0` means no ceiling. A cached loop spends nothing, so no dollar cap can end it — **this** is the brake that makes it start paying again, and the moment it engages is written to the ledger as a note. Turns the cache on |
| `--fault-inject SPEC` | off | make the boundary misbehave on purpose (see `docket chaos`) |
| `--fault-seed N` | random | seed for `--fault-inject`; the same seed reproduces the same faults |
| `--snapshot` | off | copy the *contents* of watched files before the agent starts, so the run can be undone |
| `--snapshot-max-bytes N` | `67108864` (64 MiB) | budget for that copy. Over it the run is refused before the agent starts (exit `43`) |
| `--rollback-on-breach` | off | implies `--snapshot`: if the run breaches a cap, put the watched tree back from the snapshot and record what was restored |
| `--upstream NAME=URL` | provider defaults | point a provider prefix at another base URL (repeatable). This is what a container, a mock provider or an OpenAI-compatible gateway needs. The override is **evidence**: the run records name and destination (scheme, host, port, path) in `session.start`, with query, fragment and userinfo stripped, because a token pasted into a URL must never reach a ledger. A malformed override is refused rather than ignored — silently falling back to the real API while the command line says otherwise is the failure mode this flag exists to prevent |
| `--port N` | `0` (any free port) | bind the boundary to a fixed loopback port. A readiness probe, a metrics scrape or a sidecar that finds the boundary by address needs the port to be knowable; the run records the port it asked for (`session.start.boundary.requested_port`) and the port it actually bound (a `note`). `0`–`1023` are refused: needing root to listen is a deployment smell, and `--port` is about being findable, not about being hard to reach |

The boundary always listens on **loopback**. `--port` chooses *which* loopback port, never whether
the socket is reachable from a network: peers cannot post events into a run, and `/docket/metrics`
is read from inside the host or pod (see [`docs/deployment.md`](deployment.md)).

Environment injected into the child (in addition to provider base URLs):

```
DOCKET_PROXY_URL      http://127.0.0.1:<port>/        # for /docket/* recording endpoints
DOCKET_SESSION        the session id
DOCKET_DOCKET_DIR     where the ledger is being written
DOCKET_POLICY_DIGEST  digest of the policy in force
DOCKET_TOOL_VERSION   the version producing the record
```

The `/docket/*` endpoints on the boundary accept agent-asserted events from any language:
`/docket/tool/call`, `/docket/tool/result`, `/docket/note`, and `/docket/event` for any type in
the vocabulary (`{"type": "model.response", "data": {...}}`). They append to the *current*
session's ledger and label everything they record with `reported_by: "agent"`, so a reader can
tell a claim from a boundary observation. The supervisor stays the only process that appends to
the ledger file — which is what keeps the hash chain valid.

Provider variables injected with `--profile full`: `OPENAI_BASE_URL`, `OPENAI_API_BASE`,
`ANTHROPIC_BASE_URL`, `GEMINI_API_BASE_URL`, `DEEPSEEK_BASE_URL`, `MISTRAL_BASE_URL`,
`OPENROUTER_BASE_URL`, `GROQ_BASE_URL`. An agent that honours none of these still works via
`--http-proxy`, or by calling `/docket/*` directly — and one that bypasses all of them is
recorded as *missing*, not as *nothing happened*: the receipt's `limits_disclosure` states the
boundary's scope, and the session event records which variables were set.

### The boundary's operations endpoints

While a run is in progress the boundary answers three endpoints of its own, on the loopback port it
bound (the port is printed by `--report full`, recorded in the ledger, and fixed with `--port`):

| endpoint | answers | what it is for |
| --- | --- | --- |
| `GET /docket/healthz` | `{"status": "ok", "session", "uptime_seconds", "events", "breached"}` | liveness: the process is up and its counters are moving |
| `GET /docket/readyz` | `{"ready": true, ...}` or `503` with the reason | **readiness, which here is an evidence question**: is the ledger still writable, is the volume above `min_free_bytes`, is the breaker shut? A `503` means the run is still going and Docket can no longer promise to record it |
| `GET /docket/metrics` | Prometheus text (`docket_boundary_requests_total`, `docket_model_calls_total`, `docket_model_denied_total`, `docket_cost_usd`, `docket_events_total`, `docket_rate_limited_total`, `docket_breaker_state`, `docket_ledger_degraded`, …) | the numbers a dashboard or an alert wants, in the format monitoring systems already read |

They are on **loopback only**, like every other endpoint the boundary serves: a peer that can reach
the boundary can post agent-asserted events into a live run, and nothing outside the host or pod
should be able to do that. A readiness probe, a metrics scrape or a sidecar therefore runs *inside*
the same container or pod — [`deploy/kubernetes/`](../deploy/kubernetes/deployment.yaml) and
[`deploy/compose.yaml`](../deploy/compose.yaml) show both, and
[`docs/deployment.md`](deployment.md#what-the-boundary-does-not-expose) explains why there is no
Service to scrape.

### Rolling back a run

`--snapshot` and `--rollback-on-breach` are the only Docket features that **write to your
tree**. Everything else observes. Two consequences, both deliberate:

* A snapshot that cannot cover the tree is refused *before the agent starts* — a partial
  snapshot is worse than none, because it invites restoring over a file it never captured.
  Files it declines (over the per-file ceiling, symlinks, roots outside the working
  directory) are listed in `ops.snapshot.not_covered` in the receipt: the receipt says what
  *cannot* be undone, not just what can.
* The automatic rollback runs *after* the child exits, never during: killing an agent and then
  fighting it for the same files leaves a tree that belongs to neither party.

See `docket restore` below for the on-demand form, and
[ADR 0004](adr/0004-content-snapshots-and-confinement.md) for why the store is
content-addressed and why a restore will not take the tree from a document.

## `docket restore <receipt.json> [flags]`

Undoes the filesystem changes a recorded run made, from that run's own snapshot. Dry run by
default: it prints what it would do and writes nothing until you pass `--apply`.

| flag | meaning |
| --- | --- |
| `--apply` | actually restore (default: show the plan and exit) |
| `--root DIR` | the tree to restore into. Defaults to the working directory the run recorded, and is required when the receipt, the ledger and the snapshot manifest disagree about it |
| `--force` | restore even where a file changed after the run |
| `--ledger PATH` | the run's ledger (default: beside the receipt) |
| `--snapshot-store DIR` | the snapshot directory (default: beside the receipt) |
| `--docket-dir DIR` | where the run kept its evidence |
| `--json` | the plan, the applied records and the counts as data |

```console
$ docket restore .docket/latest.receipt.json      # dry run: prints the plan, writes nothing
  docket restore — run 017aa8b8e4ac into /home/you/project

  would restore src/agent.py -> 4efcb38c3e18

  dry run: nothing was written. Add --apply to restore these paths.

  the snapshot could not cover 1 file(s):
    models/big.bin — 12000000 bytes is over the per-file snapshot ceiling of 8388608
```

The verbs are one per path: `would restore` (the file existed before the run and was modified),
`would recreate` (it was deleted), `would remove` (the agent created it), and `skip (not covered)`
for a change the snapshot has no copy of. `--apply` reports what *happened* instead — `restored`,
`recreated`, `removed`, `already restored`, `REFUSED`, `FAILED`, `skipped (not covered)`,
`already gone` — and an applied restore is recorded beside the evidence:

```console
$ docket restore .docket/latest.receipt.json --apply
  REFUSED     src/agent.py -> 4efcb38c3e18 — the file changed after the run (expected
              4efcb38c3e18…, found 5ad82ad71d5c…); re-run with --force to overwrite it

  applied: {'refused': 1}

  recorded: .docket/restores/20261014T203012Z-DKT-20261014-58CBC2.json
$ echo $?
1
```

Six rules, all of them learned from what a rollback tool must not do:

1. **The plan comes from the record, not from re-walking the tree.** It is derived from the
   snapshot manifest and the run's own `fs.change` events, so it can be audited (and
   re-derived) by anyone holding the same two files.
2. **A file edited after the run is refused, not overwritten.** The restore compares each file
   against the digest the run recorded for it; a mismatch means a colleague, a formatter or
   you has touched it since. `--force` is how you say "yes, over that".
3. **Nothing is written outside the run's tree.** Paths come from documents, so they are
   checked rather than trusted: no absolute paths, no `..`, no component that is a symlink, no
   blob outside the store, and no setuid bit. A path that fails appears as `REFUSE` with the
   reason. See [ADR 0004](adr/0004-content-snapshots-and-confinement.md).
4. **It never claims completeness.** Paths the snapshot could not cover are printed as exactly
   that, and the exit code is `1` whenever any path was refused or failed — a partial restore
   is not reported as success.
5. **Running it twice succeeds.** A file that already holds the snapshot's bytes, with the mode
   the plan would apply, is reported `already restored`: no write, no refusal. Before that
   branch existed, the second run refused the file as "changed after the run" — an accusation
   against the operator for an edit Docket itself had made, and a command that fails when a
   script or a nervous hand repeats it.
6. **It leaves a record of its own.** An applied restore writes
   `.docket/restores/<utc-stamp>-<docket-number>.json` (`docket/restore/v1`): the receipt and
   store it came from, the resolved root, whether `--force` was used, every per-path outcome,
   the counts, the uncovered paths and the exit code. Several restores of the same run are all
   kept, never overwritten. The run's ledger and receipt are not touched — they are historical
   observations, and appending to them afterwards would break the receipt's own agreement with
   the ledger it cites. Dry runs write nothing, because they change nothing.

## `docket verify <receipt.json> [flags]`

Verifies offline. Exits `0` if it checks out, `1` if it does not, `2` on usage/IO errors.

| flag | meaning |
| --- | --- |
| `--impl python\|node` | which implementation verifies: this one (default), or the independent Node one that ships inside the wheel |
| `--ledger PATH` | also check the ledger file the receipt came from (chain, roots, file digest, re-binding of usage rows) |
| `--seal-key PATH\|HEX` | the HMAC key file written by `docket run --seal` (or the hex key itself), to check a sealed receipt |
| `--public-key HEX` | expected Ed25519 public key; without it, a signature is checked only against the key embedded in the receipt |
| `--json` | machine-readable result (same `checks[]` array, with `status` of `pass`/`fail`/`skip` and a `detail`) |

Output contract: every check is `PASS`, `FAIL` or `SKIP` **with a reason**. `SKIP` never counts
as a pass; a verifier that reports coverage it does not have is worse than no verifier.

**The ledger is found, not requested.** Without `--ledger`, `docket verify` looks for the
ledger beside the receipt — the same `.docket/runs/` directory, chosen by the
`ledger.file_sha256` the receipt carries, so the file used is provably the one the receipt was
built from — and prints the line naming the file it used. `--impl node` uses the same
discovery, which is what makes `docket demo --tamper` repeatable by hand.

**`--impl node` is a second opinion, not a translation.** It runs the Node verifier that ships
inside the wheel (`docket/_vendor/verify.mjs`, byte-identical to `verify-js/verify.mjs` in the
repository) against the same receipt and the same ledger, and prints that program's own
verdict and its own exit codes: `0` verified, `1` not verified, `2` usage/IO. A pipeline that
gates on `docket verify` needs no change when it switches `--impl`. It needs Node.js 20 or
newer on `PATH`; `docket doctor` reports whether this installation has both.

## `docket report [receipt] [--markdown]`

Renders the newest receipt (or the one you pass) as a human summary. `--markdown` emits a
version suitable for a pull request, incident ticket or client email — the shape people
actually need when they have to explain an agent run to someone else.

When the run's calls were timed, the summary carries `latency  p50 … · p95 … · p99 … · max …`
with the percentile method named (`nearest-rank`, spec §6.2) and the number of calls it covers;
`docket verify` recomputes those percentiles from the receipt's own usage rows, so the line is a
claim a third party can check rather than a number the tool asks to be believed.

## `docket explain [receipt] [--ledger PATH] [--json]`

Analyses a ledger and prints findings — where the money and the risk went — each with
severity, confidence (`deterministic` / `heuristic`), attributed USD impact, the event
sequence numbers it is based on, and a remedy. `--json` emits the same as data.

Findings currently implemented (thresholds live in `src/docket/explain.py` and are visible in
the output): runaway loops (repeated byte-identical requests), retry storms, repeated context
billed uncached, oversized context calls, oversized tool payloads, denied calls, deny-write
violations, premium-model leakage on small calls, and estimated-usage warnings.

## `docket diff <left.receipt.json> <right.receipt.json> [--json]`

Compares two receipts: spend, calls, tokens, models, tools, files, findings. The intended use
is *drift detection* — did last week's prompt change double the cost, did the model swap move
p95 calls to a pricier tier, did a new tool start returning huge payloads.

## `docket demo [--max-usd N] [--tamper] [--cache] [--keep] [--json]`

Runs the scripted runaway agent against an in-process fake provider: **offline, no API key,
deterministic**. `--tamper` then edits one byte of the ledger and shows verification failing at
the exact event. `--keep` leaves the workspace (with its ledger and receipt) on disk and prints
the path. `--json` emits the run summary as a single JSON object.

## `docket init [--path docket.toml] [--name NAME] [--max-usd N] [--force]`

Writes a commented starter policy. Refuses to overwrite unless `--force`.

## `docket prices [--path PATH] [--json]`

Prints the price table with its `as_of` date, per-family source notes and the table digest —
the same digest that appears in every receipt produced with it. If your invoice disagrees with
this table, you want `--prices` on `docket run`.

## `docket selftest [--json] [--keep]`

Exercises the boundary's own guarantees in a scratch workspace — request forwarding,
per-run caps, the circuit breaker, fault injection, a ledger that cannot be written, and a
child crash — and prints one line per probe: `PASS`, `FAIL` or `SKIP` with the reason. It is
the first thing to run when you suspect the tool rather than the agent, and the fastest way to
check a new machine. `--keep` leaves the scratch workspace in place for inspection; `--json`
emits the probes as data. Exit code is `0` only when nothing failed.

## `docket chaos [flags] -- <command> [args…]`

Runs a command with the boundary deliberately misbehaving, then audits what happened: it
cross-checks the ledger against the child's own count of the calls it saw on the wire, so the
receipt cannot be a self-portrait. With no command it drives the bundled synthetic agent.

```
--faults delay=250ms:0.5,status=503:0.5,drop:0.1,reset:0.05,truncate=0.5:0.2,corrupt:0.2,slow=3s:0.1
```

Each fault takes an optional probability (`:0.5`) and, where it makes sense, a magnitude
(`delay=250ms`, `slow=3s`, `truncate=0.5`, `corrupt=0.2`). `--seed` makes a run reproducible;
`--calls` and `--mode {bounded,retry}` shape the synthetic agent; `--max-usd` caps the
experiment. The result includes `ops.chaos_run` in the receipt and exit code `42` if the cap
fired.

## `docket game-day [--out DIR] [--phases …] [--online] [--max-usd N] [--json]`

Runs the scripted failure phases end to end — baseline, upstream failure, circuit breaker,
upstream latency, rate limit, ledger pressure, rollback, supervisor killed mid-run, and a response
cache driven to its own ceiling — and writes
a post-mortem next to the evidence: what was injected, what the record shows, which phase failed
its own audit, and the expected-vs-actual for each.

The `rollback` phase is the disaster-recovery drill: the workspace is seeded with files, the
agent modifies them and breaches its cap, and the phase then checks the disk — the seeded files
byte for byte, the rollback declaration in the receipt, the `x-docket.rollback-applied` event in
the ledger, and that the run's own ledger and receipt survived the restore that they paid for. A
restore that restores nothing is recorded as `x-docket.rollback-refused` with its reason, and the
phase treats `applied: true` over zero restored files as a failure — the check reads the disk, not
the claim. Offline by default against a scripted
provider (no API key, deterministic). `--phases` runs a subset (e.g. `--phases
ledger-pressure`); `--online` uses your configured provider and requires `--max-usd`.

## `docket doctor [--docket-dir DIR]`

Checks the local environment: Python version, Node (for the independent verifier), docket
directory writability, price table load, optional signing support. First line of support triage.

---

## Other entry points

There are three ways to run Docket's own components without `docket run` driving them, and they are
documented here because the CLI is not the whole tool. All three are modules in the shipped package,
so they are present wherever `docket` is installed.

### `python -m docket.scriptagent` — the reference agent

The bounded agent the demo, the game day and `make demo` use. It reads its boundary from
`DOCKET_PROXY_URL` (set by the supervisor), so it refuses to run unsupervised with exit `2`.

| variable | meaning |
| --- | --- |
| `DOCKET_AGENT_CALLS` | how many model calls to make (default 6) |
| `DOCKET_AGENT_MODE` | `bounded`, `runaway` (never stops) or `backoff` (honours `Retry-After` with full jitter) |
| `DOCKET_AGENT_MAX_SLEEP` | ceiling for a backoff sleep, in seconds (default 5) |
| `DOCKET_AGENT_SEED` | seed for the jitter, so a backoff sequence is reproducible |
| `DOCKET_AGENT_MODEL` | model name to ask for |

### `python -m docket.fakeupstream` — a scripted provider, as a service

Serves provider-shaped responses on loopback for tests, containers and game days: `--steps N`
scripts responses (the last repeats forever), `--input-tokens` / `--output-tokens` set the usage
reported, `--delay-ms` adds latency, `--model` sets the model name. It prints
`{"listening": "http://127.0.0.1:9100", …}` on stdout so a script can read the port, exits `2` on a
taken port, and warns on stderr if asked to bind anything but loopback. `GET /healthz` answers
`{"status": "ok", …}` for a container healthcheck.

```
docket run --upstream openai=http://127.0.0.1:9100 --max-usd 1 -- python -m docket.scriptagent
```

### `python -m docket.loaddriver` — the load generator

Drives N calls across C threads *through the boundary* — it reads `DOCKET_PROXY_URL` and exits `2`
without it — so the load it generates is metered, capped and recorded like any other agent's, and
it separates refusals (with the code the boundary sent) from errors. Flags: `--calls`,
`--concurrency`, `--seconds` (endurance mode), `--rate` (paced, in calls per second across all
workers), `--route`, `--timeout`, `--report PATH`. It exits `3` if any call failed, so a load test
that dropped calls cannot leave a receipt that reads like a clean run.

It is normally driven by `tools/loadtest.py`, which starts the scripted provider, supervises the
run, samples the supervisor's memory, then reads the receipt and the ledger back and refuses to
call the result usable evidence when the two sides disagree:

```bash
make load   # 2,000 calls, 8 threads — throughput and client latency
make soak   # 12,000 calls paced over ten minutes — memory and drift
```

Both write a report (`--report` / `--markdown`) next to the run's ledger and receipt. The measured
numbers, the host they came from and the limits of the method are in
[`docs/measurements/2026-10-15-2core/`](measurements/2026-10-15-2core/README.md).

## Development tools (not installed with the package)

| tool | what it does | how it can fail |
| --- | --- | --- |
| `tools/deploy_check.py` | statically validates `deploy/`: renders the Helm chart through a declared subset of the template language (refusing any construct it does not implement), then checks hardening, probes, image pinning, one-writer-per-volume, the compose wait-and-read-only contract, the policy inside every ConfigMap (parsed by Docket's own loader), and that workflows parse | exits `1` on any finding, `2` when its YAML reader is missing — it never passes without running. `tests/test_deploy_check.py` breaks each invariant on purpose |
| `tools/helm_fidelity.py` | renders the chart with the real Helm client and with `tools/deploy_check.py`'s renderer, pairs the documents by kind and name, and reports the path of the first field that differs — the check that found a `float64` reaching a config file as `6.7108864e+07` | exits `1` on any divergence, `2` when Helm is not installed, printing "This is not a pass" rather than reporting success; not part of `make check`, because CI has no Helm. `make helm-fidelity` |
| `tools/loadtest.py` | runs the load point and the soak described above | exits `1` when the run is not usable as evidence, `2` when the run cannot start |
| `tools/mutate.py` | seeded single-site mutation of a source file, with the tests that must kill each mutant; reports every survivor classified as a gap or an equivalent mutant, counts the sites that produced no mutant, and re-checks a single line with `--only-line N` (`make mutation`) | non-zero when the score is under `--min-score`; a survivor with a subset that never touches the file is the artefact the tool warns about, a `--only-line` run names its selection so it is never read as a whole-file score, and a line with no mutant is refused rather than reported as clean |
| `tools/fuzz.py` | time-boxed property tests over the parsers and the canonicaliser (`--target`, `--seconds`, `--seed`, `-n`) | non-zero on a counterexample, naming the target and the input text that produced it |
| `tools/transcript_run.py` | runs the exact command a transcript was captured from, against a scripted provider, and prints the real output (the README's transcripts were produced this way and pasted verbatim) | non-zero if the run fails; nothing here writes documentation, so a stale transcript stays visible in the diff instead of being silently refreshed |
| `tools/build_docs_site.py` | builds the documentation site strictly (`make site-check`) | non-zero on a broken link or a page missing from the nav |
| `tools/a11y_check.py` | reads the **built** site and decides ten mechanical accessibility questions — language, title, exactly one `h1` with no skipped level, a single `main`, `alt` on every image, an accessible name on every link and every rendered control, a header cell or caption on every table, no duplicate `id` — computing an accessible name in the order AccName 1.2 §4.3.2 prescribes — `aria-labelledby`, `aria-label`, a control's `<label>`, the element's own content (text, a nested `<img alt>` or `<svg><title>`), and the `title` attribute last — and reporting the split by source in its JSON (`make a11y`) | exits `1` with one line per finding (`page:line: rule: detail`), `2` when pointed at something that is not a built site; controls whose class the site's own CSS declares `display: none` are exempt as unrendered, and the exemption is printed with its CSS evidence so it cannot outlive the rule |

---

## Recipe: CI

```yaml
- run: pip install docket   # docket is on PyPI
- run: docket run -p .docket/ci.toml --label "${{ github.run_id }}" -- python agent.py
  continue-on-error: true      # let the report step run even when the cap fires (exit 42)
- run: docket verify .docket/latest.receipt.json --json > verification.json
- uses: actions/upload-artifact@v4
  with: { name: docket-evidence, path: ".docket/**" }
```

`docket verify` exits `1` on a receipt that does not check out, so a pipeline can gate on
evidence rather than on trust in the pipeline itself.

## Recipe: a client deliverable

```bash
docket run --max-usd 20 --label "acme-migration" --report full -- ./agent.sh
docket report --markdown .docket/latest.receipt.json > evidence.md
docket verify .docket/latest.receipt.json --ledger .docket/runs/xxxx.jsonl
# ship evidence.md + the receipt + the ledger; the client can verify with:
node verify-js/verify.mjs DKT-*.receipt.json --ledger xxxx.jsonl
```
