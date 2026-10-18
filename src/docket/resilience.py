"""Resilience primitives: failure detection, throttling, fault injection, evidence guards.

Everything here exists because of a specific way the world misbehaves, and every
component is *off by default* — a tool that silently changes behaviour under stress is
worse than one that fails predictably. What each one is for:

``CircuitBreaker``
    An upstream that is failing will stay failing for a while. Continuing to send it
    requests burns quota, adds latency to every call, and produces a wall of errors that
    hides the first real one. The breaker counts consecutive failures per provider,
    stops sending when the count crosses a threshold, and probes once after a cooldown
    that backs off exponentially. This is the standard closed → open → half-open state
    machine; the only unusual part is that the state transitions are *written to the
    ledger*, so an operator reading a receipt can see that the boundary stopped
    hammering an upstream and why.

``RateLimiter``
    A misbehaving agent can issue model calls far faster than any human reviews them.
    A token bucket (steady rate with a burst allowance) plus an optional sliding-window
    cap gives the boundary a way to say "not yet" — which is different from "no": the
    request is refused with a truthful ``429`` and a ``Retry-After``, and the refusal is
    recorded.

``FaultInjector``
    Testing a recorder of reality requires a hostile reality. The injector makes the
    boundary deliver latency, dropped requests, truncated bodies, corrupted bytes,
    forced status codes and connection resets on a deterministic, seeded schedule.
    Injections are recorded as events (``x-docket.fault``), because a receipt produced
    by a chaos run must be distinguishable from one produced by a normal run — otherwise
    the tool would be laundering synthetic failures into apparent evidence.

``EvidenceGuard``
    Two failure modes pull in opposite directions. If the ledger cannot be written
    (disk full, read-only mount, revoked permissions), the default must be **fail-soft**:
    the agent keeps working and the *evidence* declares its own hole. In CI, where an
    incomplete record is worse than no record, ``fail_fast`` stops the run instead.
    Neither path is silent: the guard counts failures, keeps the first error, marks the
    receipt, and prints one loud line to stderr.

``HealthState``
    Liveness, readiness and the numbers a metrics endpoint needs. Readiness is a real
    question for this tool: can the boundary still *write evidence* and *reach the
    upstream*? A boundary that is running but cannot record is not ready, and saying so
    is more useful than returning 200 out of politeness.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half-open"


class LedgerWriteError(RuntimeError):
    """Raised when evidence could not be recorded and ``fail_fast`` is in force."""


@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    opened_at: float | None = None
    cooldown: float = 0.0
    probes_in_flight: int = 0
    state: str = CLOSED
    total_failures: int = 0
    total_successes: int = 0
    openings: int = 0


class CircuitBreaker:
    """Consecutive-failure breaker with exponential cooldown, per upstream key.

    ``record_success`` closes the circuit; ``record_failure`` opens it once the
    threshold is crossed. While open, :meth:`allow` refuses every request with a
    reason a human can act on, and after ``cooldown_seconds`` the next caller becomes a
    single half-open probe: if it succeeds the circuit closes, if it fails the cooldown
    doubles (capped at ``max_cooldown_seconds``).
    """

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown_seconds: float = 30.0,
        max_cooldown_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError("breaker threshold must be at least 1")
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds
        self.clock = clock
        self._states: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def _state(self, key: str) -> _BreakerState:
        state = self._states.get(key)
        if state is None:
            state = _BreakerState(cooldown=self.cooldown_seconds)
            self._states[key] = state
        return state

    def allow(self, key: str) -> tuple[bool, str]:
        """Return ``(allowed, reason)``. ``reason`` is empty when allowed."""
        with self._lock:
            state = self._state(key)
            now = self.clock()
            if state.state == OPEN:
                if not (state.opened_at is not None):
                    raise RuntimeError(
                        "internal invariant broken: state.opened_at is not None — this is a bug in docket; "
                        "please report it with the command that reproduced it"
                    )
                elapsed = now - state.opened_at
                if elapsed < state.cooldown:
                    wait = state.cooldown - elapsed
                    return False, (
                        f"circuit open for {key!r} after {state.consecutive_failures} consecutive "
                        f"failures; {wait:.0f}s of cooldown remaining"
                    )
                state.state = HALF_OPEN
                state.probes_in_flight = 0
            if state.state == HALF_OPEN:
                if state.probes_in_flight >= 1:
                    return False, f"circuit half-open for {key!r}: a probe is already in flight"
                state.probes_in_flight += 1
                return True, ""
            return True, ""

    def record_success(self, key: str) -> None:
        with self._lock:
            state = self._state(key)
            state.total_successes += 1
            state.state = CLOSED
            state.consecutive_failures = 0
            state.opened_at = None
            state.probes_in_flight = 0
            state.cooldown = self.cooldown_seconds

    def record_failure(self, key: str) -> bool:
        """Count a failure; returns ``True`` when this failure opened the circuit."""
        with self._lock:
            state = self._state(key)
            state.total_failures += 1
            state.consecutive_failures += 1
            if state.state == HALF_OPEN:
                state.probes_in_flight = 0
                state.state = OPEN
                state.opened_at = self.clock()
                state.cooldown = min(state.cooldown * 2, self.max_cooldown_seconds)
                state.openings += 1
                return True
            if state.state == CLOSED and state.consecutive_failures >= self.threshold:
                state.state = OPEN
                state.opened_at = self.clock()
                state.openings += 1
                return True
            return False

    def state_of(self, key: str) -> str:
        with self._lock:
            return self._state(key).state

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe view, for the receipt and ``/metrics``."""
        with self._lock:
            return {
                key: {
                    "state": state.state,
                    "consecutive_failures": state.consecutive_failures,
                    "total_failures": state.total_failures,
                    "total_successes": state.total_successes,
                    "openings": state.openings,
                    "cooldown_seconds": _seconds(state.cooldown),
                }
                for key, state in sorted(self._states.items())
            }

    def opened(self) -> bool:
        with self._lock:
            return any(state.state == OPEN for state in self._states.values())


def _rate(value: object, *, what: str) -> float:
    """A positive, finite rate as a ``float``, or a ``ValueError`` naming what was given.

    Coercion is deliberate: ``Decimal("0.5")`` is a reasonable thing to configure and the policy
    loader hands Decimals to this class, so refusing the type would be pedantry. What is *not*
    acceptable is where the failure used to happen — ``elapsed * self.per_second`` raised
    ``TypeError: float * Decimal`` inside a request, which surfaced as an HTTP 502 in the middle of
    a run. A configuration mistake must be a ``ValueError`` at construction, before the first call.
    ``nan`` and ``inf`` are refused for the sharper reason that launched this check: every
    comparison against ``nan`` is false, so a ``nan`` rate silently *disables* the limiter — the
    single failure class this project exists to make impossible.
    """
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a number, not a bool: {value!r}")
    try:
        number = float(value)  # type: ignore[arg-type] - anything with __float__, checked next
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a number, not {type(value).__name__}: {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{what} must be positive and finite, not {value!r}")
    return number


def _count(value: object, *, what: str, minimum: int = 1) -> int:
    """A whole number of at least ``minimum``, or a ``ValueError`` naming what was given."""
    if isinstance(value, bool):
        raise ValueError(f"{what} must be a whole number, not a bool: {value!r}")
    try:
        number = int(value)  # type: ignore[arg-type]
        exact = float(value) == number  # type: ignore[arg-type] - rejects 2.5, accepts "2"
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a whole number, not {type(value).__name__}: {value!r}") from exc
    if not exact:
        raise ValueError(f"{what} must be a whole number, not {value!r}")
    if number < minimum:
        raise ValueError(f"{what} must be at least {minimum}, not {value!r}")
    return number


def _seconds(value: float) -> int | str:
    """Seconds for a hashed structure: integers stay integers (no floats, §3.3)."""
    from .canon import num

    return num(round(value, 3))


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


class RateLimiter:
    """Token bucket plus an optional sliding-window cap.

    ``per_second`` refills the bucket (default capacity ``burst``, i.e. one second of
    steady traffic); ``per_minute`` caps how many calls may *start* inside any 60-second
    window. Both are optional; with neither set, :meth:`acquire` always allows and the
    object costs one comparison.
    """

    def __init__(
        self,
        *,
        per_second: float | None = None,
        per_minute: int | None = None,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_second = None if per_second is None else _rate(per_second, what="per_second")
        self.per_minute = None if per_minute is None else _count(per_minute, what="per_minute")
        if burst is None:
            # Default capacity is one second of steady traffic — the shape the docs promise.
            self.capacity = self.per_second if self.per_second is not None else 1.0
        else:
            # A burst of zero would refuse every call forever, which is a way of switching a
            # limiter on in order to switch a service off; leave the limiter unset instead.
            self.capacity = float(_count(burst, what="burst"))
        self.clock = clock
        self._tokens = self.capacity
        self._last = clock()
        self._window: deque[float] = deque()
        self._lock = threading.Lock()
        self.allowed = 0
        self.refused = 0

    @property
    def configured(self) -> bool:
        return self.per_second is not None or self.per_minute is not None

    def acquire(self) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``. ``retry_after`` is 0 when allowed."""
        if not self.configured:
            return True, 0.0
        with self._lock:
            now = self.clock()
            if self.per_second is not None:
                elapsed = max(now - self._last, 0.0)
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.per_second)
            if self.per_minute is not None:
                cutoff = now - 60.0
                while self._window and self._window[0] <= cutoff:
                    self._window.popleft()
                if len(self._window) >= self.per_minute:
                    retry = max(self._window[0] + 60.0 - now, 0.0)
                    self.refused += 1
                    return False, round(retry, 3)
            if self.per_second is not None and self._tokens < 1.0:
                retry = (1.0 - self._tokens) / self.per_second if self.per_second else 1.0
                self.refused += 1
                return False, round(retry, 3)
            if self.per_second is not None:
                self._tokens -= 1.0
            if self.per_minute is not None:
                self._window.append(now)
            self.allowed += 1
            return True, 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "per_second": _seconds(self.per_second) if self.per_second is not None else None,
                "per_minute": self.per_minute,
                "burst": int(self.capacity),
                "allowed": self.allowed,
                "refused": self.refused,
                "tokens_available": _seconds(round(self._tokens, 3)),
            }


# --------------------------------------------------------------------------- #
# Fault injection (chaos engineering)
# --------------------------------------------------------------------------- #

FAULT_KINDS = {"delay", "drop", "truncate", "corrupt", "status", "reset", "slow"}


@dataclass
class Fault:
    """One configured fault: a kind, a probability, and kind-specific arguments."""

    kind: str
    probability: float = 1.0
    seconds: float = 0.0
    status: int = 500
    fraction: float = 0.5
    reason: str = ""

    def match(self, rng: random.Random) -> bool:
        return rng.random() < self.probability

    def describe(self) -> str:
        if self.kind == "delay":
            return f"delay {self.seconds:g}s"
        if self.kind == "slow":
            return f"slow upstream ({self.seconds:g}s then reset)"
        if self.kind == "status":
            return f"forced status {self.status}"
        if self.kind == "truncate":
            return f"truncated body ({self.fraction:.0%} kept)"
        if self.kind == "corrupt":
            return "corrupted body bytes"
        if self.kind == "drop":
            return "request dropped before the upstream"
        return f"connection reset before response ({self.kind})"


def parse_fault_spec(spec: str) -> list[Fault]:
    """Parse ``--fault-inject`` into faults.

    Grammar: comma-separated ``kind[=value[:probability]]`` terms. Examples::

        delay=250ms          add 250 ms to every forwarded request
        delay=2s:0.3         add 2 s to 30% of requests
        status=503:0.5       answer half the requests with 503
        status=429           every request answered 429
        truncate=0.5         return only the first half of each body
        corrupt=0.2          flip bytes in 20% of bodies
        drop:0.1             refuse 10% of requests without calling upstream
        reset:0.05           close the connection with no response
        slow=3s:0.1          sleep 3 s then reset, 10% of the time

    A bare kind means "always". ``seed=N`` sets the deterministic random seed.
    Raises :class:`ValueError` with the offending token for anything unrecognised —
    a chaos flag that silently does nothing would be worse than no flag.
    """

    faults: list[Fault] = []
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if token.startswith("seed="):
            continue
        trailing_probability = ""
        if "=" not in token and ":" in token:
            # ``drop:0.25`` — a probability for a fault that takes no value. Accepted so
            # that every kind reads the same way, rather than making the one grammar
            # wart a thing users have to learn from an error message.
            head, _, trailing_probability = token.partition(":")
            kind, value = head, ""
        elif "=" not in token:
            kind, value = token, ""
        else:
            kind, value = token.split("=", 1)
        kind = kind.strip().lower()
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault {kind!r}; known kinds: {', '.join(sorted(FAULT_KINDS))}")
        argument, _, probability_text = value.partition(":")
        if trailing_probability:
            probability_text = trailing_probability
        try:
            probability = float(probability_text) if probability_text else 1.0
        except ValueError as exc:
            raise ValueError(f"bad probability in {token!r}: {probability_text!r}") from exc
        if not 0.0 <= probability <= 1.0:
            raise ValueError(f"probability in {token!r} must be between 0 and 1")

        fault = Fault(kind=kind, probability=probability)
        argument = argument.strip()
        if kind in {"delay", "slow"}:
            fault.seconds = _parse_duration(argument or "1s", token)
        elif kind == "status":
            try:
                fault.status = int(argument or "500")
            except ValueError as exc:
                raise ValueError(f"bad status in {token!r}: {argument!r}") from exc
            if not 100 <= fault.status <= 599:
                raise ValueError(f"status in {token!r} must be a valid HTTP status code")
        elif kind in {"drop", "reset", "corrupt"} and argument:
            # ``drop=0.25`` looks like "drop 25% of requests" and would otherwise be
            # parsed as "drop everything", silently ignoring the number. Refusing is the
            # only honest option: a chaos flag that quietly does something else is worse
            # than no flag at all.
            raise ValueError(
                f"{token!r} sets a value on {kind!r}, which takes none; write {kind}:{argument} for a probability"
            )
        elif kind == "truncate":
            try:
                fault.fraction = float(argument or "0.5")
            except ValueError as exc:
                raise ValueError(f"bad fraction in {token!r}: {argument!r}") from exc
            if not 0.0 < fault.fraction <= 1.0:
                raise ValueError(f"fraction in {token!r} must be in (0, 1]")
        faults.append(fault)
    return faults


def _parse_duration(text: str, token: str) -> float:
    value = text.strip().lower()
    multiplier = 1.0
    if value.endswith("ms"):
        multiplier, value = 0.001, value[:-2]
    elif value.endswith("s"):
        value = value[:-1]
    try:
        seconds = float(value) * multiplier
    except ValueError as exc:
        raise ValueError(f"bad duration in {token!r}: {text!r} (use 250ms, 2s, 1.5s)") from exc
    if seconds < 0:
        raise ValueError(f"duration in {token!r} must not be negative")
    return seconds


class FaultInjector:
    """Deterministic fault schedule for a boundary.

    ``random.Random(seed)`` plus a request counter means the same seed and the same
    request sequence produce the same faults, which is what makes a chaos run
    reproducible in a bug report. Injections are recorded by the caller (the proxy) as
    ledger events; this class only decides *what* happens.
    """

    def __init__(self, faults: list[Fault], *, seed: int = 0) -> None:
        self.faults = list(faults)
        self.seed = seed
        self.rng = random.Random(seed)
        self.requests = 0
        self.injected: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.faults)

    def next_delay(self) -> tuple[float, list[Fault]]:
        """Latency to apply before forwarding, and the faults that caused it."""
        with self._lock:
            self.requests += 1
            delays = [f for f in self.faults if f.kind == "delay" and f.match(self.rng)]
            seconds = sum(f.seconds for f in delays)
            for fault in delays:
                self._record(fault)
            return seconds, delays

    def before_forward(self) -> Fault | None:
        """A fault that prevents the upstream call entirely (drop / reset)."""
        with self._lock:
            for fault in self.faults:
                if fault.kind in {"drop", "reset"} and fault.match(self.rng):
                    self._record(fault)
                    return fault
            return None

    def after_response(self, status: int) -> Fault | None:
        """A fault that rewrites a response: forced status, truncation, corruption, slow."""
        with self._lock:
            for fault in self.faults:
                if fault.kind == "slow" and fault.match(self.rng):
                    self._record(fault)
                    return fault
                if fault.kind == "status" and fault.match(self.rng):
                    self._record(fault)
                    return fault
            for fault in self.faults:
                if fault.kind in {"truncate", "corrupt"} and fault.match(self.rng):
                    self._record(fault)
                    return fault
            return None

    def _record(self, fault: Fault) -> None:
        self.injected.append(
            {
                "kind": fault.kind,
                "detail": fault.describe(),
                "probability": _seconds(fault.probability),
                "request_index": self.requests,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "seed": self.seed,
                "spec": [f.describe() for f in self.faults],
                "requests_seen": self.requests,
                "injections": len(self.injected),
                "by_kind": _counts(fault["kind"] for fault in self.injected),
            }


def _counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return out


# --------------------------------------------------------------------------- #
# Evidence guard
# --------------------------------------------------------------------------- #


@dataclass
class EvidenceGaps:
    """What could not be recorded, and why. Travels into the receipt."""

    write_failures: int = 0
    first_failing_seq: int | None = None
    last_error: str = ""
    fail_fast: bool = False
    extra: list[dict[str, Any]] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        return self.write_failures > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "write_failures": self.write_failures,
            "first_failing_seq": self.first_failing_seq,
            "last_error": self.last_error,
            "fail_fast": self.fail_fast,
            "degraded": self.degraded,
            "extra": list(self.extra),
            "note": (
                "the ledger file is not a complete record of this run: these events exist in "
                "memory but not on disk, so the chain will not verify past the first gap. "
                "Treat the totals as an upper bound on what was observed, not as a complete account."
            )
            if self.write_failures
            else "no evidence gaps",
        }


class EvidenceGuard:
    """Records how often evidence could not be written, and optionally stops the run.

    The guard is deliberately not a silent ``except``: the first failure produces one
    loud line on stderr, every failure increments a counter that lands in the receipt,
    and ``fail_fast`` converts the situation into a :class:`LedgerWriteError` for
    callers that prefer a stopped run to an incomplete record.
    """

    def __init__(self, *, fail_fast: bool = False, warn: Callable[[str], None] | None = None) -> None:
        self.gaps = EvidenceGaps(fail_fast=fail_fast)
        self.fail_fast = fail_fast
        self._warn = warn or (lambda message: None)
        self._warned = False

    def record_failure(self, *, seq: int | None, error: BaseException) -> None:
        self.gaps.write_failures += 1
        if self.gaps.first_failing_seq is None:
            self.gaps.first_failing_seq = seq
        self.gaps.last_error = f"{type(error).__name__}: {error}"
        if not self._warned:
            self._warned = True
            self._warn(
                f"warning: the ledger could not be written ({self.gaps.last_error}). "
                "The run continues and the receipt will declare the gap."
            )
        if self.fail_fast:
            raise LedgerWriteError(
                f"evidence could not be recorded at seq {seq}: {self.gaps.last_error} "
                "(running with --fail-fast-evidence, which stops the run instead of producing "
                "an incomplete record)"
            ) from error

    def record_note(self, kind: str, detail: dict[str, Any]) -> None:
        self.gaps.extra.append({"kind": kind, **detail})


# --------------------------------------------------------------------------- #
# Health and metrics
# --------------------------------------------------------------------------- #


class HealthState:
    """Liveness, readiness and counters, shared by ``/healthz``, ``/readyz``, ``/metrics``."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.requests = 0
        self.denied = 0
        self.errors = 0
        self.rate_limited = 0
        self.breaker_refusals = 0
        self.in_flight = 0
        self._lock = threading.Lock()

    def counter(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name, 0) + amount)

    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                # Decimal string, not a float: health lands in the receipt, and receipts
                # are hashed (§3.3 of the spec forbids floats in hashed structures).
                "uptime_seconds": _seconds(self.uptime_seconds()),
                "requests": self.requests,
                "denied": self.denied,
                "errors": self.errors,
                "rate_limited": self.rate_limited,
                "breaker_refusals": self.breaker_refusals,
                "in_flight": self.in_flight,
            }


def readiness(
    *, ledger_ok: bool, ledger_error: str | None, free_bytes: int | None, min_free_bytes: int
) -> dict[str, Any]:
    """The readiness decision, in one place so tests can pin it.

    Not ready means one of: evidence cannot be written, or the volume holding the
    ledger has less than ``min_free_bytes`` free (a run that fills a disk turns a
    budget problem into an outage).
    """
    problems: list[str] = []
    if not ledger_ok:
        problems.append(f"ledger is not writable: {ledger_error or 'unknown error'}")
    if free_bytes is not None and free_bytes < min_free_bytes:
        problems.append(f"only {free_bytes} bytes free where the ledger is written (minimum {min_free_bytes})")
    return {
        "ready": not problems,
        "problems": problems,
        "ledger_writable": ledger_ok,
        "free_bytes": free_bytes,
        "min_free_bytes": min_free_bytes,
    }
