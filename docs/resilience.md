# Resilience: what happens when the world misbehaves

This is the document `docket init` points at, and it exists because every key in the `[resilience]`
section of a policy changes how a run behaves under failure — and a recorder that changes behaviour
without saying so is a recorder you cannot trust. Each mechanism below is opt-in, off by default
unless noted, and recorded in the ledger or the receipt when it acts.

The vocabulary is the ordinary one from production engineering; the difference here is that every
claim is a *decision with evidence*, not a metric you have to trust. Two rules run through the whole
page:

1. **Nothing acts silently.** A breaker opening, a request throttled, a fault injected, a write
   failure, a cap breached: each becomes an event with its reason, and the receipt reports it.
2. **Nothing is "fixed" by hiding it.** A lost event is disclosed as a gap, never papered over. A
   refusal says which rule refused. A torn ledger is classified before it is repaired.

## The failure surface

A supervised run has four things that can fail, and they need different answers:

| what fails | what you get | where it is documented |
| --- | --- | --- |
| the **upstream** (provider down, slow, returning 5xx) | the breaker, the timeout, recorded `model.error` events | this page, *Upstream* |
| the **agent** (loops, hammers, ignores refusals) | the rate limiter, the caps, `503`/`429` refusals in the agent's own words | this page, *Agent* |
| the **evidence path** (disk full, read-only volume, EIO) | the evidence guard, `fail_fast_evidence`, the readiness floor, the `unwritten` list | this page, *Evidence* |
| the **run itself** (killed, interrupted, clock wrong) | fsync-before-return, the torn-tail classifier, the monotonic clock, the receipt written only after the child is reaped | this page, *The run* |

## Upstream

**Circuit breaker** — `[resilience] breaker_threshold` (default 5), `breaker_cooldown_seconds` (30),
`breaker_max_cooldown_seconds` (300). After N consecutive upstream failures the boundary stops
sending and refuses with a named reason; a probe after the cooldown decides whether to close, and
the cooldown doubles up to the ceiling while it keeps failing. States and refusals are recorded
(`x-docket.breaker-open`), exported as `docket_breaker_state` and `docket_breaker_refusals_total`,
and the `circuit-breaker` game-day phase asserts the whole cycle from the ledger.

**Timeouts** — the upstream read has its own timeout (300 s default, `upstream_timeout` on the
boundary). A timeout is recorded as a failed call, not a silence: `model.error` with the reason, and
the breaker counts it.

**Provider failover: not implemented, deliberately.** There is one base URL per provider. Switching
providers automatically would change what the receipt means — a different account, a different
price, a different tokeniser — without changing anything a reader can see. If you need failover,
put it in your own router, above Docket, and the receipt will show which endpoint answered.

**Network partitions** are therefore a *sequence of failures*: each attempt is an error event, the
breaker opens, and the run either continues against a healthy provider or stops legibly. A partition
between the agent and the boundary is a connection failure on the agent's side; the boundary cannot
record what never arrived, and `docs/production-coverage.md` says so rather than implying
completeness.

## Agent

**Rate limiting** — `max_calls_per_second` (token bucket, burst = one second of steady traffic) and
`max_calls_per_minute` (sliding window). A refusal is an HTTP `429` carrying `Retry-After`, recorded
as `x-docket.rate-limited` with the limiter's own state, and counted in `docket_rate_limited_total`.
The reference agent's `DOCKET_AGENT_MODE=backoff` is the client half of that contract: full jitter
under a cap, doubling per refusal, honouring `Retry-After`, with the wait and the hint it was given
written into the ledger. The jitter taxonomy it implements — and the measurement that made full
jitter the default in the AWS SDKs — is Marc Brooker's [*Exponential Backoff And Jitter*][backoff]
(AWS Architecture Blog, March 2015); the boundary itself does not retry, so this mode exists to
*reproduce* a retry storm for a game day rather than to hide one.

[backoff]: https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/

**Caps** — `max_usd`, `max_tokens`, `max_model_calls`, `max_wall_seconds`. Crossing one is a
*policy outcome*, not an accident: the boundary refuses further model calls (`on_breach = "deny"`) or
the supervisor stops the run (`"kill"`, exit `42`), and the receipt states which cap was breached and
when. `--budget-mode quota` exists for subscriptions, where there is no per-token bill to measure.

**Loop and retry patterns** — `docket explain` names them (`runaway-loop`, `retry-storm`,
`cache-miss`, `fat-context`, `premium-for-small-calls`, `concentration`, `policy-denials`,
`write-scope`) with the evidence each finding rests on and the impact attributed per finding.

**What is deliberately absent**: Docket does not retry for the agent, does not deduplicate a repeated
call, and does not cache a response. All three would make the record describe a run that did not
happen — see `docs/production-coverage.md` §Idempotency for the reasoning.

## Evidence

**The evidence guard** — every event is `fsync`ed before the request proceeds. If a write fails
(`ENOSPC`, `EIO`, a closed descriptor), the event goes on the ledger's `unwritten` list, the run
carries on if it can, and the receipt discloses the gap: `ops.evidence` names how many events never
reached disk and where the first one was. `fail_fast_evidence = true` inverts that: stop the run
instead, for CI where half a record is worse than none.

**Ceilings** — `max_ledger_bytes` (256 MiB default) and `max_events` bound the run's own record, so
a runaway agent cannot fill a disk with its own press coverage. A full ledger is a policy outcome:
the boundary answers `503` with `code: max_events` and the supervisor stops the run. Control events
(session start/end, spawn/exit) are exempt from the event ceiling, never from the byte ceiling, so
the record of *why* it stopped always fits.

**Readiness** — `/docket/readyz` reports the ledger as writable and the volume above
`min_free_bytes` (16 MiB default), and turns red *before* evidence is lost rather than after. It
also reports the breaker's state, so a pod that cannot talk to its provider is distinguishable from
one that cannot write.

## The run

**A killed run leaves a readable prefix, not a corrupt file.** Lines are complete or missing;
a half-written tail is *classified* by `docket doctor` (with the byte offset) rather than guessed at,
and repaired only by an explicit command that moves the bytes to a sidecar. `--quarantine-torn-tail`
never deletes, and never overwrites the original.

**Clocks.** Timestamps are RFC 3339 UTC for a reader to compare against; *ordering* never depends on
them (the chain is the order). Caps and durations are measured on the monotonic clock, so a VM whose
wall clock steps cannot extend a run. A clock that steps *backwards* is clamped: no duration may be
negative, and it cannot manufacture a breach. `Retry-After` expressed as an HTTP date is
deliberately ignored — a local clock behind the provider's would make the wait negative, and a
negative sleep is a busy loop.

**Interruption.** `SIGTERM`/`SIGINT` are forwarded to the agent's process group, the child is reaped,
and only then is the receipt written — a receipt is a claim that the run ended, so it is written
after it did. The container `STOPSIGNAL` is `SIGTERM` for the same reason.

## Failure modes, ranked by what they would cost (an FMEA)

A list of mechanisms is not an analysis, and "resilient" is not a property you can assert. This is
the analysis: the ways a supervised run actually fails, what the failure costs, how often it happens
to a well-behaved deployment, how it is *noticed*, and the artefact in this repository that notices
it. Every row names a detector, and the detector column is checkable — a test reads this table and
asserts that every path it names exists in the tree.

**How the ratings work, and what they are not.** Severity, occurrence and detection are each rated
`1`–`5`. Severity is what the operator loses (5 = money, or evidence that cannot be reconstructed).
Occurrence is how often it happens in ordinary operation. Detection is the chance it is caught
*automatically* — a `5` would mean nobody learns until a person reads the ledger, and nothing here is
rated 5, because every failure below leaves a record somewhere. The risk score is the product.
**These ratings are judgements, not measurements**: they come from reading this tree, the incident
corpus and the game-day records, and a reasonable reviewer would move some of them by one. The
*catcher* column is the part that is evidence, and the only part the test asserts. Rows are ordered
by risk score, highest first — the order in which a failure would actually hurt.

| failure mode | what it costs | S | O | D | risk | what catches it here |
| --- | --- | --- | --- | --- | --- | --- |
| the agent loops and keeps spending | money, and a receipt that has to say how much | 5 | 4 | 1 | 20 | the cap is enforced *at the boundary* — the call is refused with `error.type = docket_policy_denied` and never forwarded — with `cap.warning` at `warn_at` (0.8 of the cap) before the ceiling and `cap.exceeded` at it; `tests/test_supervisor.py` and `tests/test_proxy.py` assert both events in a real run, and `docket demo` replays the loop offline in CI |
| the price table goes stale and the cap fires late | a bill the cap was supposed to stop | 4 | 2 | 2 | 16 | every receipt carries the table's date, provenance and the per-call unit prices it used (`docket prices`, `src/docket/pricing.py`), so a late cap is reconstructible afterwards; `docket diff` shows a receipt that moved when nothing else did. Noticed at review, not mid-run |
| the receipt verifies but is not true | the artefact the whole product rests on | 5 | 1 | 3 | 15 | the format is a written specification (`spec/docket-receipt-1.md`) with committed vectors, checked by two independent implementations against the same bytes — `src/docket/verify.py` and `verify-js/verify.mjs` — plus 113 tests in `tests/test_receipt_verify.py`. Caught by running `docket verify`, not by a guard during the run |
| the provider fails some calls and serves others | spend you cannot attribute | 3 | 4 | 1 | 12 | successes and failures are different event types (`model.response` never covers a failed call), the breaker opens on a failure *rate*, and the game-day `upstream-failure` phase in `src/docket/chaos.py`, audited by `tests/test_chaos.py`, asserts the two are never merged |
| the run is killed with SIGKILL (a torn tail) | a half-written last line | 3 | 2 | 2 | 12 | `docket doctor` classifies the tail and reports its byte offset instead of guessing (`src/docket/diagnostics.py`); `--quarantine-torn-tail` moves the bytes to a sidecar and never deletes; the game-day `supervisor-killed` phase kills a real supervisor mid-run. Caught when `doctor` is run |
| the agent reaches a host nobody approved | unrecorded spend, or data leaving | 3 | 2 | 2 | 12 | `[egress]` in the policy, a `net.denied` event with the refusing code, the CONNECT path checked the same way as a direct request, and `tests/test_url_guards.py` walking the package's syntax tree so a call site added later cannot skip the guard |
| a rollback misses a file the run changed | drift that survives the undo | 3 | 2 | 2 | 12 | the snapshot is the list of paths the run touched, `docket restore` is a dry run by default (`tests/test_snapshot_restore.py`), and a restore writes a self-describing record (`docket/restore/v1`) beside the evidence instead of editing the run's own records |
| a signed-off ledger is edited after the fact | any claim the ledger supports | 5 | 1 | 2 | 10 | every event carries the previous event's hash, the receipt commits to the chain head, `tests/test_receipt_verify.py` edits one field and asserts a specific failure, and `docket doctor` re-derives the chain of an old ledger. Caught when someone verifies |
| a header carries a secret into the ledger | a credential in an audit trail | 5 | 1 | 2 | 10 | headers are redacted at the boundary, URLs are sanitised before they reach an event, `tests/test_security.py` plants a token and asserts it is absent from the bytes on disk, and `_exclude_own_evidence` keeps the evidence directory out of the snapshot |
| the provider goes slow (tail latency) | a run that overruns its wall-clock budget | 3 | 3 | 1 | 9 | a per-call `upstream_timeout` on the boundary's own connection, `--max-wall-clock` measured on the monotonic clock, per-call `duration_ms`, a p50/p95/p99 table in the load report (`docs/measurements/2026-10-15-2core/load-report.json`), and the game-day `upstream-latency` phase |
| the disk fills while the run is writing | evidence that stops without saying so | 4 | 2 | 1 | 8 | the evidence guard (`fail_fast_evidence`) turns a failing write into a stopped run, the receipt states `ledger_complete: false` with `unwritten_events`, `first_failing_seq` and `write_failures`, readiness reports the volume below `min_free_bytes`, `tests/test_ledger_faults.py` reproduces ENOSPC on a real filesystem, and the game-day `ledger-pressure` phase drives a ledger into its ceiling |
| the boundary's own memory grows with the run | an OOM kill that takes the evidence with it | 4 | 1 | 2 | 8 | the run's event list is bounded by `max_events` and `max_ledger_bytes`, the cache by its TTL, `max_entries` and a `max_hits` brake, and the soak measured growth at **1.04–1.14 KiB per event across a 6× range of run lengths** (`docs/measurements/2026-10-15-2core/soak-report.json`). The honest part: ten million events would still be ~11 GB, which is why the ceilings exist |
| the clock steps (an NTP jump, a paused VM) | a cap that never fires, or a negative sleep | 3 | 2 | 1 | 6 | caps and durations are measured on the monotonic clock, wall-clock timestamps are for readers only and ordering never depends on them, a step backwards is clamped, and `tests/test_invariants.py` runs a clock that lies |
| the cache answers a request it should never have | an agent acting on another request's answer | 5 | 1 | 1 | 5 | a hit requires an exact `request_sha256`, only a whole non-injected 2xx is ever stored, a TTL bounds the window, `policy/hits` in the receipt lets a reader check the saving, and `tests/test_cache.py` asserts each refusal to store |
| a rollback touches a file the run did not change | data loss the run did not cause | 5 | 1 | 1 | 5 | confinement is checked before any write — `/proc`, a symlink out of the tree, a path outside the snapshot — and `tests/test_snapshot_confinement.py` (25 tests) tries to escape the tree on purpose |
| two supervisors append to one ledger | a chain with two writers is not a chain | 4 | 1 | 1 | 4 | opening a ledger takes an exclusive lock and a second writer is refused by name (`src/docket/ledger.py`, ADR 0005), with `tests/test_ledger_concurrency.py` holding the lock from a real second process |

## Proving it, rather than asserting it

| mechanism | how it is exercised |
| --- | --- |
| breaker, limiter, faults, evidence guard | `docket selftest` (19 checks), `tests/test_resilience.py` (60), `tests/test_proxy_concurrency.py` |
| caps, timeouts, kill path | `tests/test_supervisor.py`, `tests/test_invariants.py` (16 invariants, including a real wall-clock cap and a clock that lies) |
| the whole thing under fault injection | `docket chaos` (fault grammar, seeded and recorded) and `docket game-day` (9 phases) |
| lost evidence | `tests/test_ledger_faults.py` (ENOSPC, EIO, closed handle), `tests/test_ledger_contracts.py` |
| killing a run mid-flight | game-day `supervisor-killed` phase; `tests/test_invariants.py` |
| recovery | `docket doctor`, `--quarantine-torn-tail`, `--snapshot` / `--rollback-on-breach` / `docket restore` |

The long form of what is *not* covered — endurance, throughput, live providers, Windows, a real
cluster — is `docs/testing.md#not-covered-and-why`. It is worth reading before this page.
