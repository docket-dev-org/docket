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
- **`make audit`**: `bandit` over `src/` and `tools/`, also a CI step. Every remaining class of
  finding is either fixed or has one written-down reason in `[tool.bandit]` (`"PASS"` as an
  output word, seeded `random`, list-argv `subprocess`); nothing is suppressed without a reason.

### Fixed

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
