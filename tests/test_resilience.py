"""Resilience primitives: the breaker, the limiter, the injector, the evidence guard.

These tests are written against the *properties* the tools claims, not against the
implementation: a breaker that opens when it should and closes when it should, a limiter
that refuses rather than queues, an injector that is deterministic and declared, and a
guard that can stop a run rather than lose a record. Every state transition asserted here
is one the receipt can carry.
"""

from __future__ import annotations

import threading

import pytest

from docket.canon import canonical_bytes
from docket.resilience import (
    CircuitBreaker,
    EvidenceGaps,
    EvidenceGuard,
    Fault,
    FaultInjector,
    HealthState,
    LedgerWriteError,
    RateLimiter,
    parse_fault_spec,
)

# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


def test_breaker_is_closed_until_the_threshold_is_reached():
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=10)
    assert breaker.state_of("openai") == "closed"
    assert breaker.allow("openai") == (True, "")
    breaker.record_failure("openai")
    breaker.record_failure("openai")
    assert breaker.state_of("openai") == "closed", "two of three failures must not open it"
    assert breaker.allow("openai")[0]


def test_breaker_opens_at_the_threshold_and_refuses_with_an_increasing_cooldown():
    # The clock is injectable, so this asserts real timing behaviour without sleeping:
    # a test that sleeps for its own cooldown is a test that hides a regression in slow CI.
    now = {"t": 1000.0}
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=10, max_cooldown_seconds=25, clock=lambda: now["t"])
    breaker.record_failure("openai")
    breaker.record_failure("openai")
    assert breaker.state_of("openai") == "open"
    allowed, reason = breaker.allow("openai")
    assert not allowed
    assert "circuit open" in reason and "cooldown" in reason, "a refusal must explain itself"
    assert breaker.snapshot()["openai"]["openings"] == 1
    assert breaker.snapshot()["openai"]["cooldown_seconds"] == 10

    # After the cooldown a single probe is allowed (half-open); if it fails, the cooldown
    # doubles — and the doubling stops at the configured ceiling.
    now["t"] += 11
    assert breaker.allow("openai")[0], "half-open must let one probe through"
    assert breaker.state_of("openai") == "half-open"
    breaker.record_failure("openai")
    assert breaker.snapshot()["openai"]["cooldown_seconds"] == 20
    now["t"] += 21
    assert breaker.allow("openai")[0]
    breaker.record_failure("openai")
    assert breaker.snapshot()["openai"]["cooldown_seconds"] == 25, "cooldown must cap at the maximum"
    assert breaker.snapshot()["openai"]["openings"] == 3


def test_a_success_closes_a_half_open_breaker_and_resets_the_streak():
    now = {"t": 0.0}
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=1, clock=lambda: now["t"])
    breaker.record_failure("openai")
    breaker.record_failure("openai")
    now["t"] += 2
    assert breaker.allow("openai")[0]
    breaker.record_success("openai")
    assert breaker.state_of("openai") == "closed"
    assert breaker.snapshot()["openai"]["consecutive_failures"] == 0


def test_breaker_keeps_separate_state_per_upstream():
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    breaker.record_failure("openai")
    assert breaker.state_of("openai") == "open"
    assert breaker.state_of("anthropic") == "closed"
    assert breaker.allow("anthropic")


def test_breaker_snapshot_is_receipt_safe():
    """A breaker snapshot lands in the receipt, so it must contain no floats (§3.3)."""
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=1.5)
    breaker.record_failure("openai")
    canonical_bytes(breaker.snapshot())  # raises TypeError if a float slipped in


def test_breaker_is_thread_safe_under_contention():
    breaker = CircuitBreaker(threshold=50, cooldown_seconds=60)

    def hammer() -> None:
        for _ in range(200):
            breaker.record_failure("openai")
            breaker.allow("openai")

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    state = breaker.snapshot()["openai"]
    assert state["total_failures"] == 1600
    assert state["openings"] >= 1


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


def test_unconfigured_limiter_allows_everything_and_reports_that_it_is_not_configured():
    limiter = RateLimiter()
    assert not limiter.configured
    assert all(limiter.acquire() == (True, 0.0) for _ in range(50))
    assert limiter.refused == 0, "an unconfigured limiter must never manufacture a refusal"


def test_per_minute_limiter_refuses_with_a_retry_hint():
    limiter = RateLimiter(per_minute=3)
    assert all(ok for ok, _ in [limiter.acquire() for _ in range(3)])
    ok, retry_after = limiter.acquire()
    assert not ok
    assert retry_after > 0, "a refusal must say when to come back"
    assert limiter.refused == 1
    assert limiter.allowed == 3


def test_the_window_slides_rather_than_resetting_on_a_boundary():
    now = {"t": 500.0}
    limiter = RateLimiter(per_minute=2, clock=lambda: now["t"])
    assert limiter.acquire()[0]
    assert limiter.acquire()[0]
    assert not limiter.acquire()[0]
    now["t"] += 59.0
    assert not limiter.acquire()[0], "the window is 60s, not 59"
    now["t"] += 2.0
    assert limiter.acquire()[0], "an admission older than 60s must stop counting"


def test_the_window_stops_counting_an_admission_at_exactly_sixty_seconds():
    """Kills `self._window[0] <= cutoff -> <` in `acquire`.

    The sliding window forgets admissions older than sixty seconds, and `<=` versus `<` decides
    whether a call made *exactly* sixty seconds ago still counts. Its neighbour steps 59 s and then
    2 s, which is the same answer under both readings; the mutant needs the exact instant, where it
    refuses a call the policy allows. Asserted from both sides: refused one tick before, allowed on
    the tick.
    """
    now = {"t": 500.0}
    limiter = RateLimiter(per_minute=2, clock=lambda: now["t"])
    assert limiter.acquire()[0] is True
    assert limiter.acquire()[0] is True
    now["t"] += 59.0
    assert limiter.acquire()[0] is False, "59 s is inside the window"
    now["t"] += 1.0  # exactly 60 s after the two admissions
    assert limiter.acquire()[0] is True, "an admission exactly 60 s old no longer counts"


def test_token_bucket_bursts_then_throttles_to_the_refill_rate():
    now = {"t": 0.0}
    limiter = RateLimiter(per_second=4, burst=4, clock=lambda: now["t"])
    assert [limiter.acquire()[0] for _ in range(4)] == [True] * 4
    ok, retry_after = limiter.acquire()
    assert not ok and retry_after > 0
    assert retry_after == pytest.approx(0.25, abs=0.01), "one token at 4/s is a quarter second away"
    now["t"] += 0.3
    assert limiter.acquire()[0], "tokens must refill"


def test_limiter_snapshot_is_receipt_safe_and_reports_tokens_as_decimal_strings():
    limiter = RateLimiter(per_second=2.5, per_minute=10)
    limiter.acquire()
    snapshot = limiter.snapshot()
    assert snapshot is not None
    canonical_bytes(snapshot)
    assert snapshot["per_second"] == "2.5"
    assert snapshot["allowed"] == 1


def test_a_decimal_rate_behaves_like_the_float_it_equals():
    """The regression test for a real 502.

    The policy loader hands this class Decimals; ``elapsed * self.per_second`` then raised
    ``TypeError: float * Decimal`` *inside a request*, which the boundary reported as an internal
    error and recorded in the ledger — a configuration value turning into a failed call. Both
    spellings must now behave identically, token for token.
    """
    from decimal import Decimal

    now = {"t": 0.0}
    decimal_limiter = RateLimiter(per_second=Decimal("2"), burst=Decimal("2"), clock=lambda: now["t"])
    float_limiter = RateLimiter(per_second=2.0, burst=2, clock=lambda: now["t"])
    for _ in range(6):
        now["t"] += 0.25
        assert decimal_limiter.acquire() == float_limiter.acquire()
    assert decimal_limiter.snapshot() == float_limiter.snapshot()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"per_second": float("nan")}, "per_second must be positive and finite"),
        ({"per_second": float("inf")}, "per_second must be positive and finite"),
        ({"per_second": 0}, "per_second must be positive and finite"),
        ({"per_second": -1.5}, "per_second must be positive and finite"),
        ({"per_second": True}, "per_second must be a number, not a bool"),
        ({"per_second": "fast"}, "per_second must be a number, not str"),
        ({"per_minute": 0}, "per_minute must be at least 1"),
        ({"per_minute": 2.5}, "per_minute must be a whole number"),
        ({"per_minute": True}, "per_minute must be a whole number, not a bool"),
        ({"burst": 0}, "burst must be at least 1"),
        ({"burst": 1.5}, "burst must be a whole number"),
        ({"burst": False}, "burst must be a whole number, not a bool"),
    ],
)
def test_a_rate_limiter_refuses_configuration_it_cannot_honour(kwargs, message):
    """Every one of these used to be accepted and then misbehave at request time.

    ``nan`` is the sharpest of them: every comparison against NaN is false, so a NaN rate compared
    false against ``tokens < 1.0`` and the limiter allowed *everything* — a guard that silently
    stops guarding, which is the exact failure this project exists to prevent.
    """
    with pytest.raises(ValueError, match=message):
        RateLimiter(**kwargs)


def test_a_limiter_that_cannot_be_serialised_cannot_be_recorded_so_it_cannot_be_built():
    """The snapshot lands in a ledger event, so it must be strict JSON — no NaN, no Infinity."""
    import json
    from decimal import Decimal

    limiter = RateLimiter(per_second=Decimal("0.5"), burst=1)
    json.dumps(limiter.snapshot(), allow_nan=False)


def test_limiter_counts_refusals_across_threads_without_losing_any():
    limiter = RateLimiter(per_minute=1)

    def hammer() -> None:
        for _ in range(50):
            limiter.acquire()

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert limiter.allowed + limiter.refused == 300


# --------------------------------------------------------------------------- #
# Fault injection
# --------------------------------------------------------------------------- #


def test_fault_spec_parses_every_documented_kind():
    faults = parse_fault_spec("delay=250ms:0.5,status=503:1.0,drop:0.25,reset:0.1,truncate=0.5,corrupt:0.05,slow=2s")
    kinds = [f.kind for f in faults]
    assert kinds == ["delay", "status", "drop", "reset", "truncate", "corrupt", "slow"]
    assert faults[0].seconds == pytest.approx(0.25)
    assert faults[0].probability == pytest.approx(0.5)
    assert faults[1].status == 503
    assert faults[6].seconds == pytest.approx(2.0)


def test_an_empty_spec_means_no_faults_and_a_bad_spec_is_refused_loudly():
    assert parse_fault_spec("") == []
    with pytest.raises(ValueError, match="unknown fault 'banana'"):
        parse_fault_spec("banana=1")
    with pytest.raises(ValueError):
        parse_fault_spec("delay=not-a-duration")
    with pytest.raises(ValueError):
        parse_fault_spec("status=999")
    with pytest.raises(ValueError):
        parse_fault_spec("status=500:2.0")


def test_injection_is_deterministic_for_a_seed_and_varies_across_seeds():
    spec = "status=500:0.5,drop:0.5"

    def run(seed: int) -> list[str | None]:
        injector = FaultInjector(parse_fault_spec(spec), seed=seed)
        return [fault.kind if (fault := injector.before_forward()) else None for _ in range(60)]

    same_seed_first, same_seed_second, other_seed = run(11), run(11), run(12)
    assert same_seed_first == same_seed_second, "the same seed must give the same experiment"
    assert same_seed_first != other_seed, "a different seed must actually change the experiment"
    assert any(item is not None for item in same_seed_first), "at 50% over 60 calls, something must fire"


def test_every_injection_is_counted_by_kind_so_the_receipt_can_declare_it():
    """Delays are consumed before forwarding, drops instead of forwarding — and the
    snapshot counts both, because the receipt has to declare every injected fault."""
    injector = FaultInjector(parse_fault_spec("delay=250ms:1.0,drop:1.0"), seed=3)
    for _ in range(5):
        seconds, delays = injector.next_delay()
        assert seconds == pytest.approx(0.25)
        assert len(delays) == 1
        assert injector.before_forward() is not None
    snapshot = injector.snapshot()
    assert snapshot["enabled"] is True
    assert snapshot["injections"] == 10
    assert snapshot["by_kind"] == {"delay": 5, "drop": 5}
    assert snapshot["requests_seen"] == 5
    canonical_bytes(snapshot)


def test_faults_that_take_no_value_refuse_a_meaningless_value():
    with pytest.raises(ValueError, match="takes none"):
        parse_fault_spec("drop=0.25")
    # …and the colon form works for every kind, so the grammar is uniform.
    assert parse_fault_spec("drop:0.25")[0].probability == pytest.approx(0.25)
    assert parse_fault_spec("truncate=0.5:0.2")[0].probability == pytest.approx(0.2)


def test_a_disabled_injector_reports_itself_as_disabled():
    injector = FaultInjector([], seed=0)
    assert injector.enabled is False
    assert injector.snapshot()["enabled"] is False
    assert injector.before_forward() is None


def test_the_injector_defaults_to_the_seed_it_records():
    """Kills `FaultInjector.__init__`'s `seed: int = 0 -> 1`.

    A chaos run is reproducible because the seed is both the generator's and the run's record: the
    snapshot carries it into the receipt, and an operator reproduces the run by passing it back. A
    default nobody chose would put a seed in the record that no flag produced — and the seed is
    exactly the kind of value a reader trusts without checking. The determinism half is asserted
    too, because a seed that is recorded but not used would satisfy the first assertion alone.
    """
    injector = FaultInjector(parse_fault_spec("status=503:0.5,slow=1s:0.5"))
    assert injector.snapshot()["seed"] == 0, "the default seed is the one the docs name"
    same = FaultInjector(parse_fault_spec("status=503:0.5,slow=1s:0.5"))
    assert [injector.after_response(200) for _ in range(20)] == [same.after_response(200) for _ in range(20)]


def test_describe_is_human_readable_and_names_the_effect():
    assert parse_fault_spec("delay=0.01")[0].describe() == "delay 0.01s"
    assert parse_fault_spec("status=503")[0].describe() == "forced status 503"
    assert "drop" in parse_fault_spec("drop")[0].describe()


# --------------------------------------------------------------------------- #
# Evidence guard
# --------------------------------------------------------------------------- #


def test_tolerant_guard_records_gaps_instead_of_stopping_the_run():
    warnings: list[str] = []
    guard = EvidenceGuard(fail_fast=False, warn=warnings.append)
    guard.record_failure(seq=4, error=OSError("disk full"))
    gaps = guard.gaps
    assert guard.fail_fast is False
    assert gaps.degraded is True
    assert gaps.write_failures == 1
    assert gaps.first_failing_seq == 4
    assert "disk full" in gaps.last_error
    assert warnings and "disk full" in warnings[0], "the first gap must be loud, not silent"
    # Only the first failure warns: a full ledger would otherwise produce a line per event.
    guard.record_failure(seq=5, error=OSError("disk full"))
    guard.record_failure(seq=6, error=OSError("disk full"))
    assert len(warnings) == 1
    assert gaps.write_failures == 3


def test_fail_fast_guard_stops_the_run_with_the_reason_attached():
    guard = EvidenceGuard(fail_fast=True, warn=lambda message: None)
    with pytest.raises(LedgerWriteError) as exc:
        guard.record_failure(seq=9, error=OSError("read-only file system"))
    message = str(exc.value)
    assert "read-only file system" in message
    assert "seq 9" in message
    assert "--fail-fast-evidence" in message, "the error must name the flag that caused it"
    assert guard.gaps.fail_fast is True


def test_gaps_are_serialisable_for_the_receipt():
    guard = EvidenceGuard(fail_fast=False, warn=lambda message: None)
    guard.record_failure(seq=1, error=OSError("nope"))
    guard.record_failure(seq=2, error=OSError("nope again"))
    guard.record_note("elided", {"events": 12, "reason": "ceiling reached"})
    payload = guard.gaps.to_dict()
    canonical_bytes(payload)
    assert payload["write_failures"] == 2
    assert payload["first_failing_seq"] == 1, "the first gap is the one verification stops at"
    assert payload["extra"][0]["kind"] == "elided"


def test_readiness_reports_each_reason_a_boundary_should_not_serve():
    from docket.resilience import readiness

    ready = readiness(ledger_ok=True, ledger_error=None, free_bytes=10**9, min_free_bytes=1024)
    assert ready["ready"] is True and ready["problems"] == []

    unwritable = readiness(ledger_ok=False, ledger_error="OSError: read-only", free_bytes=10**9, min_free_bytes=1024)
    assert unwritable["ready"] is False
    assert any("read-only" in problem for problem in unwritable["problems"])
    canonical_bytes(unwritable)

    full_disk = readiness(ledger_ok=True, ledger_error=None, free_bytes=10, min_free_bytes=1024)
    assert full_disk["ready"] is False
    assert any("bytes free" in problem for problem in full_disk["problems"])


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def test_readiness_is_ready_with_exactly_the_free_space_the_policy_names():
    """Kills `free_bytes < min_free_bytes -> <=` in `readiness`.

    The minimum is the amount of space the operator said a run needs, so a volume holding exactly
    that much is ready and one byte less is not. Its neighbour tests 10 bytes against 1024, which is
    the same answer under both readings; the mutant turns the documented boundary into an off-by-one
    that marks a healthy volume not-ready — and the failure mode of a readiness check is flapping,
    not silence.
    """
    from docket.resilience import readiness

    exact = readiness(ledger_ok=True, ledger_error=None, free_bytes=4096, min_free_bytes=4096)
    assert exact["ready"] is True, "exactly the minimum is enough"
    assert exact["problems"] == []
    one_less = readiness(ledger_ok=True, ledger_error=None, free_bytes=4095, min_free_bytes=4096)
    assert one_less["ready"] is False and "minimum 4096" in one_less["problems"][0]


def test_health_counters_are_receipt_safe_and_monotonic():
    health = HealthState()
    for _ in range(3):
        health.counter("requests")
    health.counter("denied", 2)
    snapshot = health.to_dict()
    assert snapshot["requests"] == 3
    assert snapshot["denied"] == 2
    canonical_bytes(snapshot)
    # Health lands in the receipt, and the receipt is hashed: §3.3 forbids floats there,
    # so an integral duration is an int and a fractional one is a decimal string.
    assert not isinstance(snapshot["uptime_seconds"], float)
    assert health.uptime_seconds() >= 0.0


# --------------------------------------------------------------------------- #
# Mutation paydown: one test per survivor the sweep could not kill
# --------------------------------------------------------------------------- #
#
# `tools/mutate.py` seeded single-site mutations of `src/docket/resilience.py` and ran the mapped
# tests against each one; fourteen survived. Each is a behaviour this file did not pin, and the
# tests below name the mutant they were written to kill so the next sweep can be read against them.


def test_a_fresh_breaker_has_recorded_nothing_and_needs_exactly_its_threshold():
    """Kills `_BreakerState.consecutive_failures = 0 -> 1` and `threshold < 1 -> < 0`.

    A breaker whose streak starts at one opens a failure early — for a threshold of five it would
    refuse on the fifth failure rather than the fifth *consecutive* one, and on a cold key it would
    behave as though something had already gone wrong. The counter is asserted at zero after the
    first allowed call, and at one after the first failure, so the start and the increment are both
    pinned; the threshold of zero is refused because a breaker that opens on nothing is off.
    """
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=10)
    assert breaker.allow("openai") == (True, "")
    assert breaker.snapshot()["openai"]["consecutive_failures"] == 0
    assert breaker.record_failure("openai") is True
    assert breaker.snapshot()["openai"]["consecutive_failures"] == 1
    with pytest.raises(ValueError, match="at least 1"):
        CircuitBreaker(threshold=0)


def test_a_half_open_circuit_admits_exactly_one_probe():
    """Kills `probes_in_flight >= 1 -> > 1` in `allow` and the `return` on the next line.

    "Half-open" means one request is allowed through to find out whether the upstream is back. Two
    would be two failures to record for one decision, and on a key with a slow upstream it would be
    the herd the breaker exists to prevent. The second refusal also has to be *legible*: the mutant
    that returns None instead of a reason would reach the caller as a `TypeError` in a request.
    """
    now = {"t": 1000.0}
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=10, clock=lambda: now["t"])
    breaker.record_failure("openai")
    breaker.record_failure("openai")
    now["t"] += 11
    assert breaker.allow("openai") == (True, ""), "the cooldown is up: one probe may go out"
    second = breaker.allow("openai")
    assert second[0] is False and "probe is already in flight" in second[1], second


def test_a_successful_probe_releases_the_half_open_slot_for_the_next_outage():
    """Pins recovery across two outages — and records why the counters are redundant.

    The first sweep claimed this test killed `state.probes_in_flight = 0 -> 1` in `record_success`.
    A later, wider run showed both that mutant and the one in `record_failure` surviving, and the
    reason is in `allow`: moving a key from `open` to `half-open` sets `probes_in_flight = 0`, so
    the value the success and failure paths leave behind is overwritten before it is ever read.
    Those two are equivalent mutants, not gaps; the site that matters is the reset inside `allow`,
    which this test kills, and what it pins is the behaviour a reader cares about — a recovered
    upstream is admitted again on the *next* outage, not only the first.
    """
    now = {"t": 0.0}
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=5, clock=lambda: now["t"])
    breaker.record_failure("openai")
    now["t"] += 6
    assert breaker.allow("openai")[0]
    breaker.record_success("openai")
    assert breaker.state_of("openai") == "closed"

    breaker.record_failure("openai")
    now["t"] += 6
    assert breaker.allow("openai")[0], "the second outage must admit a probe of its own"


def test_record_failure_says_whether_it_opened_the_circuit():
    """Kills `return True -> return None` on the *closed* path of `record_failure`.

    The caller turns this bool into an `x-docket.breaker-opened` event, which is how a receipt
    explains a run that stopped reaching its provider. A `None` here is falsy for the non-opening
    failures — the obvious assertion passes — and silently loses the opening event, so the answer is
    asserted against `True` itself as well as `False`.

    This test claimed *both* `return True` mutants when it was written. A wider run showed the
    half-open one surviving: reaching it needs a probe that fails, which no test did. The mutant
    list is an index into the source, so a claim about "both" is a claim about a line number — see
    `test_a_failed_probe_reports_that_it_reopened_the_circuit` for the other half.
    """
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=10)
    assert breaker.record_failure("openai") is False, "the first failure does not open it"
    assert breaker.record_failure("openai") is True, "the second one does"


def test_opened_reports_an_open_circuit_as_an_exact_boolean():
    """Kills `opened()`'s `return any(...) -> return None`.

    `opened()` is read by the health endpoints and the readiness reason list, and it reads as
    "not open" when it returns None. Asserted with `is`, because `assert breaker.opened()` would
    pass for `None` in the closed case and this test is about the answer, not its truthiness.
    """
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=10)
    assert breaker.opened() is False
    breaker.record_failure("openai")
    assert breaker.opened() is True
    breaker.record_success("openai")
    assert breaker.opened() is False


def test_a_breaker_counts_both_outcomes_for_the_receipt_it_fills_in():
    """Kills `total_successes: int = 0 -> 1` and `state.total_successes += 1 -> += 0`.

    `snapshot()` is not a debugging view: it is what the receipt and `/metrics` carry, and
    `total_successes` beside `total_failures` is how a reader tells an upstream that was mostly
    healthy from one that was mostly not. Two mutants make it lie in opposite directions — a fresh
    key reporting a success it never had, and successes never being counted at all — and both read
    as a plausible number, which is why the assertion is on the value rather than on the field's
    presence.
    """
    now = {"t": 1000.0}
    breaker = CircuitBreaker(threshold=5, cooldown_seconds=30.0, clock=lambda: now["t"])
    assert breaker.record_failure("openai") is False
    failures_only = breaker.snapshot()["openai"]
    assert (failures_only["total_failures"], failures_only["total_successes"]) == (1, 0)
    assert failures_only["state"] == "closed" and failures_only["consecutive_failures"] == 1

    breaker.record_success("openai")
    breaker.record_success("openai")
    counted = breaker.snapshot()["openai"]
    assert counted["total_successes"] == 2, "two successes are two successes in the receipt"
    assert counted["consecutive_failures"] == 0, "a success ends the streak"

    for _ in range(5):
        breaker.record_failure("openai")
    opened = breaker.snapshot()["openai"]
    assert (opened["state"], opened["openings"], opened["total_failures"]) == ("open", 1, 6)
    assert opened["total_successes"] == 2, "opening the circuit does not un-count the successes"


def test_the_cooldown_admits_its_probe_on_the_instant_it_ends():
    """Kills `elapsed < state.cooldown -> <=` in `allow`.

    The existing sweep test advances the clock *past* the cooldown, so both readings agree there;
    the mutation is visible only exactly at the boundary, where `<=` refuses a probe the policy said
    was due and the breaker stays open for a whole further cooldown — the difference between
    recovering on time and recovering late, which is what a caller waiting on an upstream feels.
    Both sides of the instant are asserted, because a test that checked only the boundary would
    pass for a breaker with no cooldown at all.
    """
    now = {"t": 0.0}
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=10.0, clock=lambda: now["t"])
    assert breaker.record_failure("openai") is True
    now["t"] = 9.999
    still_open = breaker.allow("openai")
    assert still_open[0] is False and "cooldown remaining" in still_open[1], still_open
    now["t"] = 10.0
    assert breaker.allow("openai") == (True, ""), "the probe is due the instant the cooldown ends"


def test_a_fault_with_zero_probability_never_matches_even_at_the_bottom_of_the_range():
    """Kills `Fault.match`'s `rng.random() < probability -> <=`.

    `probability: 0` is how a declared fault is switched off in an experiment without deleting it
    from the spec. A uniform generator returns 0.0 with probability 2^-53, so `<=` fires a
    "disabled" fault roughly never — which is exactly the class of defect that survives a
    statistical test and shows up in an audit. The generator is stubbed to its lower bound here, and
    the upper bound is checked too: `probability: 1` matches at 1.0 is *not* reachable for a
    uniform draw in [0, 1), and the code says `<`.
    """

    class Fixed:
        def __init__(self, value):
            self.value = value

        def random(self):
            return self.value

    assert Fault(kind="drop", probability=0.0).match(Fixed(0.0)) is False
    assert Fault(kind="drop", probability=0.0).match(Fixed(0.5)) is False
    assert Fault(kind="drop", probability=1.0).match(Fixed(0.0)) is True


def test_a_fault_argument_is_the_value_that_was_written():
    """Kills `parse_fault_spec`'s `float(argument or "0.5") -> argument and "0.5"`.

    `and` returns the *last* truthy operand, so every fraction would silently have become 0.5 —
    `truncate:0.8` would have truncated at half the declared point, and the receipt would have
    recorded the declared value. The default belongs to the argument being absent, which is a
    condition about `argument`, not a fallback computed from it.
    """
    assert [fault.fraction for fault in parse_fault_spec("truncate=0.8")] == [0.8]
    assert [fault.fraction for fault in parse_fault_spec("truncate")] == [0.5], (
        "the documented default, and only when the argument is absent"
    )
    assert [fault.seconds for fault in parse_fault_spec("slow=2s")] == [2.0]
    assert [fault.seconds for fault in parse_fault_spec("delay")] == [1.0]
    assert [fault.probability for fault in parse_fault_spec("drop:0.25")] == [0.25]


def test_a_zero_length_delay_is_a_delay_that_does_nothing_not_a_syntax_error():
    """Kills the `seconds < 0 -> <= 0` mutant in the duration parser.

    `delay:0ms` is a legitimate way to say "this experiment has no delay" — it is also what a
    generated spec produces when a knob is turned to zero. Refusing it as negative is a refusal of
    the operator's arithmetic rather than of the input; a negative duration is still refused.
    """
    assert [fault.seconds for fault in parse_fault_spec("delay=0ms")] == [0.0]
    assert [fault.seconds for fault in parse_fault_spec("slow=0s")] == [0.0]
    with pytest.raises(ValueError, match="negative"):
        parse_fault_spec("delay=-1s")


def test_the_injector_hands_back_the_fault_it_injected():
    """Kills `FaultInjector.after_response`'s `return fault -> return None` on the rewriting path.

    The caller uses the returned fault to decide what to do to the bytes — truncate at
    `fraction`, corrupt them — and to mark the response as touched so the cache refuses to store it.
    A `None` there is not "no fault": it is a fault the response never received, with the ledger
    still counting the injection.
    """
    fault = Fault(kind="truncate", probability=1.0, fraction=0.5)
    injector = FaultInjector([fault], seed=0)
    assert injector.after_response(200) is fault
    assert injector.injected and injector.injected[0]["kind"] == "truncate"
    corrupt = Fault(kind="corrupt", probability=1.0)
    assert FaultInjector([corrupt], seed=0).after_response(200) is corrupt
    assert FaultInjector([], seed=0).after_response(200) is None


def test_an_evidence_guard_is_tolerant_unless_it_is_told_otherwise():
    """Kills `EvidenceGuard.__init__`'s `fail_fast: bool = False -> True`.

    The default is policy, not an accident: a write failure is recorded as a declared gap and the
    run continues, because a run that stops on the first ENOSPC produces no receipt at all. Turning
    the default into `True` would make every run stop where the documentation says it keeps going,
    and the tests that pass `fail_fast=True` explicitly would never notice.
    """
    guard = EvidenceGuard(warn=lambda message: None)
    guard.record_failure(seq=7, error=OSError("no space left on device"))
    assert guard.gaps.write_failures == 1
    assert guard.gaps.to_dict()["fail_fast"] is False
    assert guard.gaps.degraded is True

    strict = EvidenceGuard(fail_fast=True)
    with pytest.raises(LedgerWriteError):
        strict.record_failure(seq=7, error=OSError("no space left on device"))


def test_a_failed_probe_reports_that_it_reopened_the_circuit():
    """Kills both `return True` mutants on the half-open path of `record_failure`.

    The failed probe is the failure that re-opens the circuit, and it is the one the caller must
    hear about: it doubles the cooldown and is what an operator sees as "the upstream failed
    again". The existing test only ever crosses the threshold from `closed`, so this path — the one
    a healthy-looking half-open probe takes — had no assertion at all.
    """
    now = {"t": 1000.0}
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=10, clock=lambda: now["t"])
    breaker.record_failure("openai")
    assert breaker.record_failure("openai") is True
    now["t"] += 11
    assert breaker.allow("openai")[0], "the cooldown is up: a probe may go out"
    assert breaker.state_of("openai") == "half-open"
    assert breaker.record_failure("openai") is True, "the probe failed: this failure re-opened it"
    assert breaker.state_of("openai") == "open"
    assert breaker.snapshot()["openai"]["openings"] == 2


def test_the_rate_limiter_counts_the_calls_it_refused():
    """Kills `self.refused += 1 -> += 0` on both refusal paths.

    `refused` is the number a receipt and `/docket/metrics` report as the price of the limit, and
    the number a soak's "0 refused" line depends on. A limiter that refuses every call and reports
    that it refused none is a limit that looks unused.
    """
    limiter = RateLimiter(per_second=1.0, burst=1)
    allowed = [limiter.acquire()[0] for _ in range(4)]
    assert allowed[0] is True and allowed[1:] == [False, False, False]
    assert limiter.snapshot()["refused"] == 3

    per_minute = RateLimiter(per_minute=1)
    assert per_minute.acquire()[0] is True
    assert per_minute.acquire()[0] is False
    assert per_minute.snapshot()["refused"] == 1


def test_a_fault_is_injected_only_by_its_own_kind_and_only_at_its_probability():
    """Kills the per-kind guards in `FaultInjector.after_response`.

    The loops in `after_response` are two conditions each — the kind this loop handles *and* this
    fault's probability — and both halves are load-bearing. Reading the condition as `or` injects a
    fault the experiment switched off (`probability: 0`), or hands back a fault of the wrong kind:
    a `drop`, which belongs before the request is forwarded, returned as a response rewrite. The
    kind comparisons have to be exact for the same reason: a loop that accepts "anything not
    `slow`" returns a `drop` where the answer is "no fault".
    """
    # A switched-off fault is not injected...
    assert FaultInjector([Fault(kind="slow", probability=0.0, seconds=1.0)], seed=0).after_response(200) is None
    # ...and an armed one is, exactly once per response it fires on, and it is recorded.
    slow = Fault(kind="slow", probability=1.0, seconds=2.0)
    injector = FaultInjector([slow], seed=0)
    assert injector.after_response(200) is slow
    assert [fault["kind"] for fault in injector.injected] == ["slow"]

    # A fault that belongs to another loop is never returned from this one: a drop is not a
    # response rewrite, so an armed drop does not make `after_response` answer with anything.
    armed_drop = Fault(kind="drop", probability=1.0)
    disabled_truncate = Fault(kind="truncate", probability=0.0)
    assert FaultInjector([armed_drop, disabled_truncate], seed=0).after_response(200) is None

    # And a switched-off fault of one kind does not stand in for an armed fault of another.
    status = Fault(kind="status", probability=1.0, status=503)
    assert FaultInjector([Fault(kind="truncate", probability=0.0), status], seed=0).after_response(200) is status
    reset = Fault(kind="reset", probability=1.0)
    assert FaultInjector([reset, Fault(kind="status", probability=0.0)], seed=0).after_response(200) is None


def test_every_fault_kind_describes_itself_and_not_another_kind():
    """Kills the `describe` mutants, including the `kind == "drop"` comparison.

    `describe()` is what a ledger's `fault.injected` event and the game day's post-mortem quote, and
    the one thing it must never do is describe a different experiment: a corrupted body reported as
    "request dropped before the upstream" is a record that misleads the person reading it. The kinds
    are checked against each other, so a fall-through that answers for everything cannot pass.
    """
    described = {
        "delay": parse_fault_spec("delay=0.25")[0],
        "slow": parse_fault_spec("slow=3")[0],
        "status": parse_fault_spec("status=503")[0],
        "truncate": parse_fault_spec("truncate=0.25")[0],
        "corrupt": parse_fault_spec("corrupt")[0],
        "drop": parse_fault_spec("drop")[0],
        "reset": parse_fault_spec("reset")[0],
    }
    texts = {}
    for kind, fault in described.items():
        text = fault.describe()
        assert isinstance(text, str) and text.strip(), f"{kind} describes itself as {text!r}"
        texts[kind] = text
    assert len(set(texts.values())) == len(texts), f"two kinds share a description: {texts}"
    # The two kinds no clause of their own follows: each must say what it did, not merely which
    # kind it was. A loop that answers "request dropped" for a reset, or names the wrong effect in
    # the parentheses, reads as a different experiment in the ledger.
    assert "dropped before the upstream" in texts["drop"], texts["drop"]
    assert "connection reset before response" in texts["reset"], texts["reset"]
    # Each description names the parameter it was configured with, so it cannot be a generic line.
    assert "0.25" in texts["delay"] and "3" in texts["slow"]
    assert "503" in texts["status"] and "25%" in texts["truncate"]


def test_a_clean_evidence_gap_block_is_not_degraded_and_does_not_fail_fast():
    """Kills `EvidenceGaps.fail_fast = False -> True` and `degraded: write_failures > 0 -> >= 0`.

    `degraded` is the flag that says the receipt's totals are an upper bound rather than an account
    of what happened, and `fail_fast` says the run stopped at the first write failure. Both travel
    into the receipt, so a clean run asserting `degraded: true` — or a run that promises to stop and
    reports that it does not — is a record that misstates its own reliability.
    """
    clean = EvidenceGaps()
    assert clean.to_dict()["fail_fast"] is False
    assert clean.degraded is False
    assert clean.to_dict()["degraded"] is False
    one_gap = EvidenceGaps(write_failures=1, first_failing_seq=4, last_error="OSError: no space left on device")
    assert one_gap.degraded is True
    assert one_gap.to_dict()["write_failures"] == 1
    assert one_gap.to_dict()["fail_fast"] is False


def test_a_fresh_health_state_has_counted_nothing():
    """Kills `HealthState.errors = 0 -> 1`.

    These counters land in the receipt and on `/metrics`: a run whose first act is to report one
    error it did not have is a health surface nobody can use to tell a quiet run from a broken one.
    """
    health = HealthState()
    counters = health.to_dict()
    assert {
        name: counters[name]
        for name in ("requests", "denied", "errors", "rate_limited", "breaker_refusals", "in_flight")
    } == {
        "requests": 0,
        "denied": 0,
        "errors": 0,
        "rate_limited": 0,
        "breaker_refusals": 0,
        "in_flight": 0,
    }


def test_a_counter_that_was_never_declared_starts_at_zero():
    """Kills `getattr(self, name, 0) -> getattr(self, name, 1)` in `HealthState.counter`.

    Every counter the boundary uses is a declared attribute, asserted at zero by the test above — so
    the fallback is reached only for a name that was never declared, which is where a typo lands.
    Starting that counter at one adds a phantom event to the metrics surface a reader uses to tell a
    quiet run from a broken one; starting at zero makes the first call the first event. Asserted on
    an undeclared name *and* a declared one, since the two paths differ.
    """
    health = HealthState()
    health.counter("never_declared")
    assert health.never_declared == 1, "one call is one event, not two"
    health.counter("never_declared", 3)
    assert health.never_declared == 4
    health.counter("errors")
    assert health.errors == 1 and health.to_dict()["errors"] == 1
