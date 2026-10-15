# ADR 0005 — One writer at a time, and what concurrency is allowed to mean

- **Status:** accepted
- **Date:** 2026-10-15
- **Deciders:** founding maintainer
- **Applies to:** `src/docket/ledger.py`, `src/docket/proxy.py`, `src/docket/resilience.py`,
  `src/docket/pricing.py`, `tests/test_ledger_concurrency.py`, `tests/test_proxy_concurrency.py`,
  `docs/architecture.md`, `docs/testing.md`

## Context

The boundary is a `ThreadingHTTPServer`: every request is handled on its own thread, so an agent
that issues parallel tool calls — or one process with several agents in it — produces genuinely
concurrent `Ledger.append` calls. Sequence number allocation, the `prev` link, the hashed body
and the write to disk used to be four separate steps. Under eight writer threads the file ended
up with events *not* in sequence order (index 16 held sequence 18, observed), which
`docket verify` reports as a broken chain. The failure mode is the worst kind for this tool: an
honest run that Docket accuses of tampering, with no attacker present.

The question the fix forced was bigger than one lock: what do Docket's concurrency guarantees
actually consist of, and which of them may a reviewer rely on?

## Decision

1. **`Ledger.append` is atomic per instance.** One `threading.RLock`, held across sequence
   allocation, body construction, hash, and write. Re-entrant because control paths
   (`_record_control`, the evidence guard) append from inside a write. The lock is a field with
   `init=False` and `compare=False`, so it does not leak into equality, `repr`, or a dataclass's
   constructor signature.

2. **The file is always one contiguous chain.** After any mix of parallel appends, the events on
   disk are in sequence order, link to their predecessor, and every digest matches its own
   preimage. `len(ledger.events)`, the number of lines, and the highest sequence number agree.
   This is the invariant a verifier depends on and therefore the one that is enforced, not
   approximated.

3. **Cost accounting is conserved, not merely consistent.** The meter's total after a burst
   equals the sum of the `model.response` rows in the ledger, and the ledger's own totals equal
   what the receipt claims. `verify` recomputes the total from the usage rows, so a lost
   `+=` in the meter would make an honest receipt fail its own check — which is why it is
   covered by a test rather than argued about.

4. **Counters that gate spending are atomic.** The rate limiter admits exactly its budget under
   contention and records every refusal (`x-docket.rate-limited`, HTTP 429); the breaker counts
   each failure once and opens once, no matter how many requests fail simultaneously.

5. **The cap is enforced at admission, and the honest bound is stated.** A burst of *n*
   requests in flight when a cap is breached can have at most one paid call per in-flight
   request recorded before the refusal takes effect; the code does not cancel a request that has
   already been forwarded, and it does not claim to. Tests assert the bound and that every
   request has exactly one accounted outcome (a status, or a recorded transport failure) —
   never an exact admitted count, which would be a lie about a race.

6. **Anything else shared is lock-free and specified as such.** `CostMeter` accumulates
   `Decimal`s and integers with read-modify-write on attributes; under CPython a thread switch
   cannot land inside an attribute increment, and 3,200 concurrent `record()` calls at a
   1 µs switch interval lose nothing (measured — kept as a test, so an implementation that
   starts parsing or formatting inside the critical section gets caught). The specification
   therefore permits a lock but does not require one; what it *requires* is that a concurrent
   implementation behave like the serial one.

## Consequences

- **Throughput is bounded by the ledger write.** Appends serialize; the boundary's ceiling is
  one append at a time (tens of microseconds of local file I/O on the machines measured here).
  Docket sits in front of a bounded agent, so this is not a claim about a target: no
  requests-per-second figure is published, and none should be inferred from these tests.
- **Parallelism is preserved everywhere it matters.** Upstream requests are not serialized by
  the ledger lock: the lock is held for the append, not for the call. A slow provider does not
  block the writers of other requests.
- **`append` is not safe across processes.** Two Docket processes appending to one path is
  still undefined, and `Ledger.open` refuses a second writer *in the same process* by account,
  not by file lock. Stated in `docs/architecture.md` and in the class docstring.
- **Crashes can leave a torn last line.** Atomicity here is thread-level ordering, not
  power-loss atomicity; a truncated final line is classified and surfaced (`Ledger.read` reports
  the tail), never silently dropped. This is a deliberate non-goal: `fsync` per append would
  make the honest cost of an audit trail visible in every agent run.
- **Test counts and the suite grew by 10**, all offline: `test_ledger_concurrency.py` (4) and
  `test_proxy_concurrency.py` (6).

## Alternatives considered

- **A single writer thread with a queue.** Rejected: it removes the ordering problem by making
  the ledger asynchronous, so a crash loses queued events, and "the event is on disk" is no
  longer true when `append` returns — a worse property for an audit trail than a lock.
- **One ledger per request, merged at the end.** Rejected: it breaks the single chain that the
  whole format is built on, and the merge itself needs the lock it was avoiding.
- **A global (module-level) lock.** Rejected: two ledgers in one process are independent files
  and should not contend.
- **Lock-free sequence allocation (`itertools.count`) plus an ordered write queue.** Rejected as
  a strictly larger mechanism for the same guarantee; the write is the serialization point and
  a file write cannot be lock-free anyway.
- **Documenting the race instead of fixing it.** Rejected: the artifact a user hands to a
  reviewer is the chain, and a chain that verifies only when the agent behaves serially is not
  a guarantee.
- **Making the cap exact by pre-reserving cost before forwarding.** Rejected for the reason in
  decision 5: the price of a call is known only from its response, so a reservation would have
  to be a guess, and a guess enforced as a hard limit is worse than an honest bound. The bound —
  one call per in-flight request — is what the tests assert and what the docs say.
