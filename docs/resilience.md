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
written into the ledger.

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

## Proving it, rather than asserting it

| mechanism | how it is exercised |
| --- | --- |
| breaker, limiter, faults, evidence guard | `docket selftest` (16 checks), `tests/test_resilience.py` (24), `tests/test_proxy_concurrency.py` |
| caps, timeouts, kill path | `tests/test_supervisor.py`, `tests/test_invariants.py` (16 invariants, including a real wall-clock cap and a clock that lies) |
| the whole thing under fault injection | `docket chaos` (fault grammar, seeded and recorded) and `docket game-day` (8 phases) |
| lost evidence | `tests/test_ledger_faults.py` (ENOSPC, EIO, closed handle), `tests/test_ledger_contracts.py` |
| killing a run mid-flight | game-day `supervisor-killed` phase; `tests/test_invariants.py` |
| recovery | `docket doctor`, `--quarantine-torn-tail`, `--snapshot` / `--rollback-on-breach` / `docket restore` |

The long form of what is *not* covered — endurance, throughput, live providers, Windows, a real
cluster — is `docs/testing.md#not-covered-and-why`. It is worth reading before this page.
