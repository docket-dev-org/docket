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
    EvidenceGuard,
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
