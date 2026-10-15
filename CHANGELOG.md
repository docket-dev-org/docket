# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the tool follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The receipt format is versioned
separately by its tag (`docket/receipt/v1`) and changes only under the format process in
[`CONTRIBUTING.md`](CONTRIBUTING.md#changing-the-format).

## [Unreleased]

### Added

- **Snapshots and rollback** (`--snapshot`, `--rollback-on-breach`, `docket restore`): a
  content-addressed copy of the watched tree's *contents*, taken before the agent starts, with
  a manifest of path, digest, size, mode and mtime (`docket/snapshot/v1`). A breached run
  restores the tree automatically and records what it restored as `x-docket.rollback-applied`;
  `docket restore <receipt>` performs the same restore on demand, dry-run by default. Paths the
  snapshot could not cover are named in `ops.snapshot.not_covered`; a tree that does not fit the
  budget is refused *before* the agent starts (exit `43`); a file edited after the run is
  refused unless `--force`. See [ADR 0004](docs/adr/0004-content-snapshots-and-confinement.md).
- **Restore confinement**: a restore only ever touches files inside the tree it was pointed at.
  Paths from a ledger or manifest are checked, not trusted — no absolute paths, no `..`, no
  symlinked component, blobs located by digest inside the store, digests required to be 64 hex
  digits, modes masked to permission bits — and the receipt, ledger and manifest must agree on
  the tree, with `--root` when they do not.
- **The evidence directory is excluded from the watched tree** by the Supervisor itself, from
  resolved paths, and the exclusion is recorded (a `note` event and the effective policy in the
  receipt). Previously a programmatic run whose docket directory sat inside the working
  directory recorded its own ledger as a file *the agent* changed, and a rollback plan derived
  from that diff contained an instruction to delete the record it came from.
- **`docket game-day` gains a `rollback` phase** (`--phases rollback`): seeded files, an agent
  that damages them, a cap breach, then automated restore — audited against the disk (files
  byte for byte), the receipt and the ledger, including that the run's own evidence survived.
- **Two URL guards, one per kind of caller**, applied at every `urlopen` in the package:
  `util.http_url` (the scheme must be http or https, and a host must be present) for code that
  stands in for an agent and may be pointed at any provider, and `util.local_http_url` (that,
  plus the host must be this machine) for Docket's own clients — the boundary recorder and the
  self-test probes — whose address comes from the environment. A `DOCKET_PROXY_URL` that is not
  loopback is now refused when the recorder is constructed rather than at the first event, and
  `0.0.0.0` is *not* accepted as a destination. This is the answer to the static-analysis
  finding that flags an unchecked `urlopen` as a possible SSRF or local-file read.
- **An applied restore writes its own record**: `.docket/restores/<utc-stamp>-<docket-number>.json`
  (`docket/restore/v1`) with the receipt and store it came from, the resolved root, whether
  `--force` was used, every per-path outcome, the counts, the uncovered paths and the exit code.
  Several restores of one run are all kept. The run's ledger and receipt are never edited: they
  are historical observations, and appending to them after the fact would break the receipt's
  agreement with the ledger it cites. Dry runs write nothing.
- **`tools/mutate.py`, and `make mutation`**: single-site source mutation — comparison and boolean
  operators, `not`, boolean and small-integer constants, `min`/`max`, and any `return`, rewritten
  through `ast` and spliced back by character span. One mutant per run against a declared test
  subset, in a copy of the repository, with the harness falsifying itself first (splicing back
  verbatim must be a byte-for-byte no-op, and the unmutated sandbox must pass before any mutant
  runs). Current scores and every classified survivor: [`docs/testing.md`](docs/testing.md#mutation-testing).
- **`make audit`**: `bandit` over `src/` and `tools/`, also a CI step. Every remaining class of
  finding is either fixed or has one written-down reason in `[tool.bandit]` (`"PASS"` as an
  output word, seeded `random`, list-argv `subprocess`); nothing is suppressed without a reason.
- **Per-call latency, and a verifier that recomputes it.** Every usage row may now carry
  `latency_ms` — the round trip the recorder measured, in whole milliseconds — and the receipt
  derives `latency` from those rows: `{"method": "nearest-rank", "measured_calls", "total_rows",
  "total_ms", "max_ms", "p50_ms", "p95_ms", "p99_ms"}`. The rule is stated because a percentile
  without its rule is not a fact: the samples are sorted ascending and the rank is
  `ceil(p/100 · n)`, clamped into the sample, so p99 of 15 calls is the 15th value and p99 of 200
  is the 198th. Both verifiers recompute the summary from the same rows and **fail** on a member
  that disagrees, a `method` this verifier does not implement, a member the format does not
  define, a row value that is not a whole non-negative count, an unreadable appendix, or a
  summary stated over rows that carry no measurement at all — the last one being a false-pass
  hole that mutation testing found in this feature rather than a reviewer
  ([`docs/verification.md`](docs/verification.md), spec §6.2). A receipt with no measurement says
  nothing rather than zero, and coverage is explicit: `measured_calls` of `total_rows`.
- **`tests/test_invariants.py`: the properties no single-feature test can state.** Fourteen
  executed invariants over the places an operations reader worries about — two writers on one
  ledger are *refused* rather than interleaved (a file holding two chains is a file that accuses
  an honest run of tampering); eight threads appending 200 events through one handle produce one
  event per sequence number, in order, with no partial line; a replayed request is recorded as
  **two** calls and never deduplicated, because collapsing a duplicate would report a run that
  did not happen; a stopped boundary releases its socket and its thread; a supervised agent killed
  with `SIGKILL` leaves a readable prefix and no receipt that claims a clean exit; a wall-clock
  cap is measured on the monotonic clock, so a clock that jumps forwards cannot extend a run; a
  clock that jumps *backwards* cannot turn a bad clock into a false breach.
- **`DOCKET_AGENT_MODE=backoff`** in the reference adversarial agent, plus `Retry-After` parsing
  (`DOCKET_AGENT_MAX_SLEEP`, `DOCKET_AGENT_SEED`): full jitter under a ceiling, doubling per
  refusal, with the wait and the hint the boundary asked for written into the ledger as `note`
  events. This is the client's half of the rate-limit contract, and without it the boundary's
  `Retry-After` was a promise nothing in the repository ever exercised.
- **A fixed boundary port, and evidence of which port it was.** `--port N` (`docket run`) binds the
  boundary to a loopback port an orchestrator can probe; `0` (the default) still means any free
  port. The requested port is recorded in `session.start.boundary.requested_port` and the port that
  was *actually* bound in a note written once the socket exists, so the evidence never states a
  guess as a fact. Privileged ports are refused at the flag with the range named.
- **`--upstream NAME=URL`**, repeatable, for pointing a provider prefix at a container, a mock, an
  OpenAI-compatible gateway or a self-hosted model. The override is recorded as evidence — name and
  destination only, with query, fragment and userinfo stripped by `util.sanitize_url`, because a
  token pasted into a base URL must never reach a hash-chained file (`tests/test_proxy.py` asserts
  the secret is nowhere in the ledger's bytes). `docket init`'s emitted policy and this flag are the
  two ways to configure a container; both are checked by `tools/deploy_check.py`.
- **`python -m docket.fakeupstream <flags>`**: the scripted provider as a *service*, with `--host`,
  `--port`, `--steps`, `--model`, `--input-tokens`, `--output-tokens`, `--delay-ms`, a JSON
  handshake on stdout, a real `GET /healthz`, exit `2` when the port is taken, and a warning when
  asked to bind beyond loopback. This is what makes the compose file and the Kubernetes manifest
  produce a real receipt offline.
- **The boundary's operations endpoints are documented** (`docs/cli.md`): `/docket/healthz`,
  `/docket/readyz` (readiness is an *evidence* question — ledger writable, volume above its floor,
  breaker shut) and `/docket/metrics` (Prometheus text). They are loopback-only by design, and
  `docs/deployment.md` says what that means for probes and scrapers.
- **`deploy/`**: a container recipe (`Dockerfile`: two stages, non-root, read-only-root-friendly, no
  credentials, `STOPSIGNAL SIGTERM`), a compose smoke test whose verifier waits for the run and
  mounts evidence read-only, a hardened pod topology (two containers sharing loopback, `Recreate`
  strategy, probes on declared ports, default-deny NetworkPolicy), and a Helm chart in a **declared
  template subset** so a deterministic renderer can check what it produces.
- **`tools/deploy_check.py` and `make deploy-check`**: 16 static checks over those artefacts — no
  `:latest`, non-root, no privilege escalation, all capabilities dropped, read-only root filesystem,
  resource requests and limits, probe ports that are actually declared, one writer per evidence
  volume, a ConfigMap policy parsed by Docket's *own* loader, default-deny networking, a compose
  verifier that waits, and CI workflows that parse (a workflow with a YAML error runs nothing and
  looks green — this check found exactly that in this repository's own CI). It exits `2` rather than
  passing when its YAML reader is missing. `tests/test_deploy_check.py` breaks each invariant on
  purpose, one fixture per check, so the checker cannot be decoration.
- **`docs/resilience.md`** — what happens when the world misbehaves (breaker, throttle, timeouts,
  caps, evidence guard, ceilings, readiness, clocks, interruption), each mechanism with how it is
  proved, and the three things deliberately refused. `docket init` has pointed at this file since
  0.1.0; it now exists.
- **`docs/production-coverage.md`** — a map of ~110 production-engineering concepts, each cell
  naming the code and tests behind it, or the document that argues the boundary, or the reason it is
  out of scope, in a controlled vocabulary (*built*, *measured*, *modelled*, *reasoned*, *out of
  scope*). It states the three kinds of absence separately instead of blurring them.

### Fixed

- **`docket init` emitted a policy pointing at a document that did not exist** (`docs/resilience.md`),
  and `deploy/` would have shipped referencing two more. `references.resolve` now fails the deploy
  check on any referenced path that is missing, and the link is real.
- **`util.sanitize_url` mangled IPv6 literals**: `http://[::1]:9000/v1` became `http://::1:9000/v1`,
  which is not a URL. Found by the test that pins the port-and-path behaviour, not by reading it.
- **The supervisor printed a negative duration when the clock stepped backwards.** `elapsed` and
  the receipt's `duration_seconds` were computed as `monotonic() - started`, and a monotonic
  source that steps back — a VM restored from a snapshot, a container sharing a stale clock, a
  bug in a caller's injected clock — made both negative. A negative duration is a claim no clock
  can support: a reader comparing it to the cap has to invent a rule for what a negative budget
  means. Both are clamped at zero now, and `test_invariants.py` drives the case. The direction is
  deliberate: a clock that steps backwards can never *raise* the elapsed reading, so it cannot
  manufacture a breach, and the child's lifetime still bounds the run.
- **A receipt that *stated* a latency summary over untimed rows could pass.** `check_summary`
  returned "this claim cannot be recomputed" in that one branch, the verifier read a falsy return
  as "no disagreement", and a receipt could therefore claim any p99 at all as long as its usage
  rows carried no `latency_ms`. Found by mutating the new module (`return problems` → `return
  None` survived) rather than by reading it; the branch now has the test that proves it is
  reachable, and `latency.py` scores 21/21 killed.
- **`latency.py` shipped an unkillable clamp.** `rank = min(max(rank, 1), len(ordered))` could
  never bind: `percentile_ms` raises on an empty sample and on a percentage outside 1..100, and
  `1 <= ceil(p·n/100) <= n` follows from those two guards. The clamp was unreachable code whose
  mutants nothing could kill, so it is gone, with the proof in a comment and two tests that pin
  the extremes of the interval (`p1` and `p100` of every sample size from 1 to 11).

- **`quarantine_torn_tail` could report a repair it had not performed.** When the interrupted
  write had landed its partial line *together with* the newline that closed it — a flush that
  half-succeeds, not only a `kill -9` — the repair split the file at the last newline, which is
  empty in that shape: it reported `performed: true`, moved **zero bytes** and left the ledger
  exactly as damaged as it found it. `docket doctor --quarantine-torn-tail` now splits by the
  reader's own measurement of the torn tail (`torn_tail.bytes`, plus the newline when the file
  has one), and `test_ledger_contracts.py` asserts the moved bytes, the sidecar's contents and
  that the repaired file is healthy afterwards.
- **The Windows lock's liveness probe killed live processes.** `_pid_alive` probed with
  `os.kill(pid, 0)`, which is the POSIX idiom and, on Windows, is `TerminateProcess`: the check
  for "is the stale lock's holder still running?" would have terminated that holder with exit
  code 0. Windows now asks the process table through `OpenProcess`/`GetExitCodeProcess` and
  answers "alive" whenever it cannot tell, because a lock is never stolen on a guess. No Windows
  is available in this environment, so the branch is written, marked `# pragma: no cover`, and
  named as untested in [`docs/testing.md`](docs/testing.md#not-covered-and-why).
- **`read_with_diagnostics` returned eleven keys from a healthy file and nine from a missing
  one.** `healthy` and `chain_error` were documented as present on every path and were absent
  from the "file does not exist" path, so a caller that had only ever seen a healthy ledger got
  a `KeyError` from a ledger that was not there. The key set is now built once, pessimistically,
  and every path fills in what it learned; `test_every_diagnostics_path_returns_the_same_keys`
  pins it across six states (healthy, empty, missing, torn, corrupt, not-UTF-8).
- **A ledger's `repr` carried descriptors and a memory address.** `_handle`, `_lock_handle` and
  the clock were dataclass fields with default reprs, so a ledger printed into a CI log or a
  pasted traceback showed file descriptors, a lock handle and `<SteppedClock object at 0x7f...>`.
  They are `repr=False` now; the repr shows the state that matters (path, events, ceilings, bytes
  written, unwritable events).

- **A document with `NaN` or `Infinity` in it was read by Docket and refused by every JavaScript
  verifier.** Those two tokens (and `-Infinity`) are a Python extension: RFC 8259 has no spelling
  for them, `json.loads` accepts them, and `JSON.parse` rejects them. A request body carrying one
  reached the recorder, could not be canonicalised, and the boundary answered
  `502 docket_internal_error` — a value the client chose made the run's own evidence read
  "internal error". A ledger, manifest or receipt containing one was *accepted* by the reader and
  then failed the chain check with "the event's data cannot be canonicalised", a confusing verdict
  for what is really "this file is not JSON"; and a hostile `Infinity` on the last line of a
  ledger was filed as a *torn tail*, i.e. it claimed the ordinary crash-recovery path. Now every
  parse of a document Docket did not write goes through `util.loads_json`, which refuses the
  non-extensions by name (`Infinity is not a JSON number (RFC 8259 allows neither NaN nor
  Infinity)`), a request body carrying one is refused at the boundary with `400
  docket_bad_request`/`non_finite_number`, recorded as `net.denied`, and never forwarded — a body
  Docket cannot record is a body it must not forward — and a non-finite line is classified as
  damage (`bad_lines`), never as a torn write. `spec` §3.2 now states the rule normatively, since
  it is an interoperability rule and not an implementation detail.
- **Parallel model calls could write the ledger out of order.** The boundary is a
  `ThreadingHTTPServer`, so two calls in flight are two `append` calls: the sequence number,
  the `prev` link, the digest and the write were computed as separate steps, and a burst of
  eight threads produced a file whose events were not in sequence order — a chain that
  `docket verify` then reports as broken, i.e. Docket accusing an honest run of tampering.
  `Ledger.append` now runs under an instance lock, so the file is always one contiguous chain
  in sequence order. `tests/test_ledger_concurrency.py` (8 threads × 25 events, statuses and
  chain checked from disk) and `tests/test_proxy_concurrency.py` (parallel calls through the
  real boundary: sound chain, meter total exactly the sum of the rows, a limiter that admits
  exactly its budget, a breaker that counts each failure once, a burst that cannot walk past a
  cap, and a receipt built from the result that still verifies) cover it.
- **A bad `--max-usd` is a usage error, and `--max-usd nan` no longer disables the cap.**
  `Decimal("NaN")` compares false against every threshold, so `docket run --max-usd nan`
  completed with the cap silently switched off and filed an ordinary-looking receipt reading
  `$0.00 of $NaN cap`; `--max-usd abc` printed `[<class 'decimal.ConversionSyntax'>]` and exited
  `1`. Caps are now validated wherever they are set — an argparse `type=` for `--max-usd`,
  `--max-tokens`, `--max-calls`, `--max-events`, `--max-calls-per-minute` and `--max-minutes`
  (exit `2`, flag named, value quoted, nothing created) and `Caps.set_max_usd()` for code, which
  is the one way to change the field after construction. `inf` is refused as a cap too: an unset
  cap is spelled by omitting the flag, and the receipt discloses it as unset rather than as a
  number.
- **An unexpected exception is reported as a sentence.** The CLI's top-level handler printed the
  exception's `args` raw, which for a decimal error is its class object. The type is always named
  now, and the detail only when there is one.
- **Restoring twice no longer blames the operator.** After a successful restore the file holds
  the snapshot's bytes rather than the digest the run ended with, so a second `docket restore`
  refused it as "changed after the run" — a false accusation, and a command that fails when a
  script, a cron job or a nervous hand repeats it. A path that already holds the snapshot's bytes
  *and* the mode the plan would apply is now reported `already restored`: no write, no refusal.
  The check runs after the plan has been validated against the snapshot store, so a doctored
  manifest is still refused on its own merits rather than reported as an idempotent no-op.
- **A relative working directory made every recorded path absolute.** `fsdiff.snapshot`
  relativised paths against an *unresolved* base, and a relative base can never be a prefix of
  the absolute paths a walk produces — so with `cwd="."` (what `docket game-day` and
  `docket run --root .` pass) every `fs.change` event carried an absolute path. Nothing
  surfaced: no ignore pattern matched, so Docket's own ledger and snapshot store were recorded
  as the agent's changes, and the confined restore then refused its own change list as
  out-of-tree. The base is resolved now, and `tests/test_fsdiff.py` asserts the relative and
  absolute forms agree.
- **A rollback that restored nothing is no longer recorded as applied.** `ops.rollback.applied`
  was set for having *attempted* a restore, so a plan whose every item was refused — see the
  defect above — produced `applied: true`, an `x-docket.rollback-applied` event and a console
  line reading "the watched tree has been put back", over a tree that had not been touched.
  `applied` now means files went back; a declined restore is recorded as
  `x-docket.rollback-refused` with its reason, named in `ops.rollback.reason` and
  `ops.rollback.refused`, and printed by both `docket run` and `docket report`.
- **`spec/vectors` had gone stale.** The committed conformance vectors still carried a policy
  document from before the `[resilience]` defaults existed, so their policy digest — which is
  bound into the first ledger event and therefore into the head, the Merkle root and the receipt
  core digest — no longer agreed with the implementation that is supposed to produce it. They
  are regenerated, and `test_committed_vectors_match_the_generator` now runs the generator into
  a copy and fails if anything differs, which is the check that would have caught it.
- **`explain.total_estimated_savings` names an unreadable impact** instead of skipping it: those
  strings are written by Docket itself, so an unreadable one is a bug, and a silently smaller
  total in a document people make decisions from is worse than a visible caveat.
- **Verification no longer reports two verdicts under one check name.** The Merkle-path verdict
  and the ledger re-binding verdict were both reported as `usage-inclusion-proofs`, so a
  doctored ledger produced `FAIL usage-inclusion-proofs` and `PASS usage-inclusion-proofs`
  a few lines apart. Re-binding is now `usage-rows-rebound`; a test
  (`test_no_check_reports_two_verdicts`) keeps one name to one verdict. The Node verifier's
  detail line now distinguishes the two causes as well.
- **`docket demo --tamper` no longer crashes** when the event it picks is not a
  `model.response`: it edited whichever line happened to be fifth and, for any other event,
  assigned `data["model"] = data` — a self-referencing dict that made `json.dumps` raise. It
  now tampers with the first `model.response` and reports nothing to tamper with if there is
  none.

## [0.1.0] — 2026-10-13

First release: a bounded, legible, provable agent run.

### Added

- **Boundary** (`docket run -- <command>`): a loopback recording proxy that injects provider
  base URLs (or `HTTP_PROXY` with `--http-proxy`), enforces policy before forwarding a call,
  accrues cost from provider-reported usage, and terminates the supervised process group
  (`SIGTERM`, then `SIGKILL` after a grace period) when a cap is exceeded. Exit codes:
  `0` clean, `42` cap exceeded, `43` policy error, `70` internal, `130` interrupted.
- **Ledger**: append-only JSONL, each event carrying `seq`, `ts`, `type`, `data`, `prev` and
  `hash`; the first event binds to `sha256("docket/genesis/v1")`. Canonical JSON with integers
  only (fractional values are decimal strings) so that the same bytes verify in any runtime.
- **Receipts**: `docket/receipt/v1` with an RFC 6962 Merkle root over event hashes, per-call
  usage rows carrying inclusion proofs and re-bindable event hashes, the exact price table
  digest, deterministic findings with evidence sequence numbers, and a `limits_disclosure`
  block that verification fails if absent.
- **Verification**: offline `docket verify` (Python, zero dependencies) and an independent
  verifier in `verify-js/verify.mjs` (Node standard library only), both bound to the committed
  conformance vectors in `spec/vectors/`. Checks report `SKIP` with a reason rather than
  passing when they cannot be performed, and the two implementations agree on the *shape* of a
  failure as well as the verdict: both recompute every event hash, both decline to check the
  head when the chain is broken, and both re-bind each usage row to the recomputed event at its
  sequence number. The Node verifier additionally checks the receipt-core digest against a
  committed vector.
- **Findings**: runaway loops (byte-identical repeated requests), repeated context billed
  uncached, oversized context calls, policy-blocked calls, deny-write violations, premium-model
  leakage and retry storms — each with severity, confidence (`deterministic` / `heuristic`),
  attributed impact and a cited evidence trail.
- **Recording surfaces**: SDK (`docket.Session`, `record_model_call`, `record_tool`,
  `record_note`) for Python agents, and `/docket/tool/call` + `/docket/note` HTTP endpoints so
  agents in any language can write to the ledger.
- **Policy**: TOML policy with caps, model allow/deny globs with first-match ordering, watched
  and ignore globs, `deny_write` scopes, egress modes (`tunnel`, `allow`, `deny`, `allowlist`),
  price-table override, and `on_breach = "kill" | "deny"`.
- **Demo**: `docket demo` runs a scripted runaway agent against an in-process fake provider —
  offline, no API key, deterministic clock — prints findings, verifies the receipt, and with
  `--tamper` edits one byte of the ledger to show verification failing at the exact event.
- **Specification**: `spec/docket-receipt-1.md`, including the canonical-form rules, the
  verification procedure, and §9 *what a valid receipt does and does not establish*.
- **Conformance vectors**: `spec/vectors/{canonical,merkle,ledger,receipt,receipt-digest}.json`
  plus a generator, consumed as the cross-implementation contract by both test suites.

### Notes

- **Attached sessions.** An SDK `Session` started inside `docket run` writes through the
  boundary's `/docket/event` endpoint instead of opening the ledger file itself: one writer per
  chain, and every agent-asserted event carries `reported_by: "agent"` so a reader can tell
  claims from boundary observations. Outside a supervised run it keeps its own ledger, as before.
- **Totals are derived from the usage appendix**, so a receipt can never disagree with the rows
  a verifier recomputes (the failure mode being `$0.00` claimed against `$0.0042` recomputed).
  Rows asserted by the agent are counted and labelled separately (`asserted_calls`).
- `docket verify --seal-key` accepts the key file written by `docket run --seal` or the hex key.
- Zero runtime dependencies. `docket[sign]` is the only extra that installs anything
  (`cryptography`, for Ed25519 signatures).
- Prices in `src/docket/prices.json` are as of 2026-10-13 with sources recorded in the file;
  they are estimates for budgeting, and every receipt states which table it used and flags
  any model priced by fallback as `assumed`.

[Unreleased]: https://github.com/docket-dev/docket/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/docket-dev/docket/releases/tag/v0.1.0
