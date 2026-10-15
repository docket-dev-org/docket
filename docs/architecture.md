# Architecture

Small on purpose. The whole runtime is a supervisor, a boundary, a ledger and a verifier —
plus rendering and analysis, which are separable. There are no daemons, no database, no
background workers and no network service beyond a loopback proxy that lives for one run.

```
docket run -- <agent>
│
├── supervisor.py      owns the run: spawn, caps, snapshots, receipt, exit code
│   ├── proxy.py       the boundary: policy checks, usage→cost, event recording
│   ├── fsdiff.py      before/after filesystem digest snapshots and diffs
│   ├── snapshot.py    content snapshots and confined restore (opt-in rollback)
│   └── receipt.py     assembles the receipt from the ledger
│
├── ledger.py          append-only hash-chained event log (the source of truth)
│   ├── canon.py       canonical JSON, digests, RFC 6962 Merkle, USD decimals
│   ├── events.py      event vocabulary + request fingerprinting (loop detection)
│   └── pricing.py     price table, cost meter, recomputation for verification
│
├── policy.py          policy document, caps, allow/deny, digests, limits disclosure
├── explain.py         deterministic findings over a ledger
├── verify.py          offline verification of receipts and ledgers
├── report.py          terminal + markdown rendering, receipt diffing
├── sdk.py             in-process recording for Python agents
└── cli.py             argument parsing, exit codes, wiring
```

## Invariants, and which module owns each

| invariant | owned by | enforced by |
| --- | --- | --- |
| A hashed structure contains no floats (integers, or decimal strings) | `canon.py` | `test_canon.py` + both implementations in `verify-js/` |
| Canonical bytes are language-independent (sorted keys, no whitespace, literal UTF-8) | `canon.py` | `spec/vectors/canonical.json` |
| Ledger lines are canonical JSON + `\n`, so `file_sha256` is reproducible elsewhere | `ledger.py` | `test_ledger.py`, `spec/vectors/ledger.json` |
| A modified, reordered, truncated or extended ledger fails at the first affected event | `ledger.py` chain check + `verify.py` | `test_ledger.py`, `test_receipt_verify.py` |
| Downstream comparisons use **recomputed** hashes, never stored ones | `verify.py` | `test_verification_fails_when_the_ledger_is_edited` |
| A check that cannot run is `SKIP` with a reason, never a pass | `verify.py` | `test_receipt_verify.py` |
| Cost is recomputable from raw token counts and the embedded price table | `pricing.py` | `test_pricing.py`, `test_vectors.py` |
| Price families match in file order (first match wins) | `pricing.py` | `test_pricing.py` (specificity ordering) |
| Verify fails if `limits_disclosure` is absent | `verify.py` | `test_receipt_verify.py` |
| The effective policy (after flags) is what the receipt embeds | `supervisor.py`, `receipt.py` | `test_supervisor.py` |
| Usage marked `estimated` is never presented as metered | `pricing.py`, `receipt.py`, `report.py` | `test_pricing.py`, `test_receipt_verify.py` |
| A restore writes only inside the tree it was pointed at, and only where the record says | `snapshot.py` | `test_snapshot_confinement.py` |
| A file changed after the run is refused, not overwritten | `snapshot.py` | `test_snapshot_restore.py` |
| A snapshot that cannot cover the tree is refused before the agent starts | `supervisor.py`, `snapshot.py` | `test_snapshot_restore.py` |

Each of those has a test because each of them is a sentence in the README that would otherwise
be marketing.

## The run, step by step

1. `supervisor.run()` creates the docket directory, opens a ledger, writes `session.start`
   (with argv, cwd, host details and the docket number) and `policy.applied` (with the policy
   document and its digest).
2. It starts the boundary proxy on an ephemeral loopback port and computes the environment for
   the child: provider base-URL variables, `DOCKET_PROXY_URL`, session metadata.
3. It snapshots the watched filesystem — digests always, *contents* too when `--snapshot` is
   on, refusing the run up front if the tree does not fit the budget — writes `proc.spawn`, and
   launches the child in its own process group (`start_new_session=True`) so a breach can
   terminate the whole subtree.
4. While the child runs, the supervisor polls for exit, watches the wall-clock cap, and checks
   a breach signal set by the proxy. On breach: ask the boundary to refuse further calls,
   `SIGTERM` the group, wait `grace_seconds`, `SIGKILL`.
5. The proxy, for every model call: parses the request, decides policy (model allow/deny),
   checks the caps, records `model.request`, forwards, streams the response back, extracts
   usage (reported or estimated), accrues cost, records `model.response` with latency and
   bytes, and emits warnings at `warn_at`.
6. After exit: snapshot the filesystem again, diff, emit `fs.change` events and any
   `policy.violation` for `deny_write` hits; write `proc.exit` and `session.end`.
7. `receipt.py` builds the receipt: totals from the meter, per-call usage rows with Merkle
   inclusion paths, the price table (embedded), findings from `explain.py`, `limits_disclosure`
   from the policy, and the ledger roots. Optionally seal (HMAC) and sign (Ed25519).
8. The receipt is written atomically to `.docket/receipts/<number>.receipt.json`; `.docket/latest`
   and `.docket/latest.receipt.json` point at the newest run. The CLI prints a summary and
   exits `0`, or `42` if a cap fired.
9. If the run breached a cap and `--rollback-on-breach` was given, the watched tree is restored
   from the content snapshot *after* the child has exited, and the outcome — counts, paths,
   refusals — is written to the ledger as `x-docket.rollback-applied` (or
   `x-docket.rollback-refused` when every path was declined, because those are two outcomes and
   a report that runs them together is a false claim) and folded into `ops.rollback` in the
   receipt. `docket restore` performs the same restore on demand, months later, from the receipt
   and ledger alone.
10. A separate `docket restore --apply` — the same restore, months later, by somebody who may
   never have run the agent — writes its own record to
   `.docket/restores/<utc-stamp>-<docket-number>.json` (`docket/restore/v1`): what it planned,
   what it did, what it refused, and whether `--force` was used. It never edits the run's ledger
   or receipt: those are historical observations, and appending to them afterwards would break
   the receipt's own agreement with the ledger it cites.

## Why the ledger is the source of truth

Everything downstream — totals, findings, the receipt, the report — is derived from ledger
events rather than accumulated in parallel. That is why `docket explain --ledger x` can
reproduce the numbers in a receipt months later, and why the receipt's appendix can be
re-derived and checked rather than believed.

It also means a new analysis rule is a pure function of an existing record: no migration, no
re-instrumentation, no "we only started tracking that last quarter".

## Threading and failure behaviour

- The proxy is a `ThreadingHTTPServer` on loopback, one thread per request. Requests from a
  single agent are usually sequential; retries and parallel tool calls are not assumed away.
- **One ledger, one writer at a time.** `Ledger.append` takes an instance lock for the whole
  step — sequence number, `prev` link, digest, write — so the file is always a contiguous chain
  in sequence order no matter how many request threads append at once. Without it, a parallel
  burst produced a file a verifier reports as broken: the tool accusing an honest run. The lock
  is held for the append, not for the upstream call, so a slow provider does not serialise the
  writers.
- The caps are enforced **at admission**: with *n* requests already in flight when a cap is
  breached, at most one paid call per in-flight request is recorded before the refusal takes
  effect. The receipt says what happened; it does not claim the overshoot was prevented.
  ADR 0005 has the full list of concurrency invariants (contiguous chain, conserved cost,
  a limiter that admits exactly its budget, a breaker that counts each failure once).
- Ledger writes `flush()` and `fsync()` per line. A crash mid-run leaves a valid prefix of the
  chain, and `docket verify` on a truncated ledger reports the count mismatch rather than
  pretending completeness.
- If the child ignores `SIGTERM`, the group is `SIGKILL`ed after `grace_seconds`; the receipt
  records `interrupted` and the cap that caused it. Exit code `42` is returned either way.
- Nothing in the run path can block on the network: the proxy connects to the provider; if the
  provider is unreachable the agent sees the ordinary connection error and Docket records it as
  `model.error`.
- Docket never makes an outbound connection of its own. There is no telemetry, no update check,
  no remote price fetch — a test asserts the only client the core can construct targets
  loopback.

## Extending

- **A new provider**: add a prefix→upstream mapping and a usage extractor in `proxy.py`; the
  receipt format does not change.
- **A new analysis rule**: add it to `explain.py` with `severity`, `confidence`, `evidence`,
  `usd_impact`, `remedy`. Deterministic rules must be derivable from events alone.
- **A new verifier language**: implement §8 of the spec against `spec/vectors/`; no need to read
  this codebase.
- **A new event type**: add it to `events.py`. Unknown types are preserved by verifiers, so
  extensions do not break older readers — but anything that changes a *hash input* is a format
  change (see [`../CONTRIBUTING.md`](../CONTRIBUTING.md)).
