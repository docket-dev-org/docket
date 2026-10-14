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
| `--fault-inject SPEC` | off | make the boundary misbehave on purpose (see `docket chaos`) |
| `--fault-seed N` | random | seed for `--fault-inject`; the same seed reproduces the same faults |
| `--snapshot` | off | copy the *contents* of watched files before the agent starts, so the run can be undone |
| `--snapshot-max-bytes N` | `67108864` (64 MiB) | budget for that copy. Over it the run is refused before the agent starts (exit `43`) |
| `--rollback-on-breach` | off | implies `--snapshot`: if the run breaches a cap, put the watched tree back from the snapshot and record what was restored |

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
| `--ledger PATH` | also check the ledger file the receipt came from (chain, roots, file digest, re-binding of usage rows) |
| `--seal-key PATH\|HEX` | the HMAC key file written by `docket run --seal` (or the hex key itself), to check a sealed receipt |
| `--public-key HEX` | expected Ed25519 public key; without it, a signature is checked only against the key embedded in the receipt |
| `--json` | machine-readable result (same `checks[]` array, with `status` of `pass`/`fail`/`skip` and a `detail`) |

Output contract: every check is `PASS`, `FAIL` or `SKIP` **with a reason**. `SKIP` never counts
as a pass; a verifier that reports coverage it does not have is worse than no verifier.

## `docket report [receipt] [--markdown]`

Renders the newest receipt (or the one you pass) as a human summary. `--markdown` emits a
version suitable for a pull request, incident ticket or client email — the shape people
actually need when they have to explain an agent run to someone else.

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

## `docket demo [--max-usd N] [--tamper] [--keep] [--json]`

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
upstream latency, rate limit, ledger pressure, rollback, supervisor killed mid-run — and writes
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

## Recipe: CI

```yaml
- run: pip install docket
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
