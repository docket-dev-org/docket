"""A response cache at the boundary, where every hit is an event.

An agent that repeats an identical request is the most common way to spend money
for nothing, and the boundary is the only place that sees both the request that
was sent twice and the response that came back twice. So the cache lives here,
next to the meter it saves, and it obeys one rule above all: **a response served
from the cache is never silent.** Every hit is a ``model.cache_hit`` event that
names the response it replays, which is what lets ``docket verify`` recompute the
saving instead of taking the boundary's word for it.

What is stored
    Only a response the provider actually sent: a 2xx, received whole, and not
    rewritten or shortened by fault injection. A truncated stream, an error, a
    connection reset, or a response the chaos injector touched is *not* the
    provider's answer and is never stored — keying a cache on corruption would
    make one injected fault sticky for the rest of the run, and no test would
    ever show it as a bug.

What a hit looks like
    The client receives exactly the bytes the origin response carried, with the
    same headers, to a request of the same kind: the key covers the request the
    boundary forwarded, so a streaming request can never be answered with a
    non-streaming body or the other way round. The provider is not contacted, so
    no ``model.request``/``model.response`` pair is written; the hit event stands
    in its place, and the run's spend, call count and token totals are unchanged
    by it. That is the point — and it is why the saving is a separate number
    reported beside the spend, never folded into it.

Invalidation
    Three ways, all of them visible: an entry expires when it is older than
    ``ttl_seconds`` (checked on lookup, counted, dropped); an entry is evicted
    when the cache is full and a new response arrives (least-recently-used
    first); and a response that is not the provider's own is refused at the
    door. :meth:`ResponseCache.invalidate` and :meth:`ResponseCache.clear` exist
    for callers that want more — ``RecordingProxy.stop`` uses ``clear()`` — but
    nothing in the shipped product invalidates on a timer or a signal, and this
    docstring says so rather than implying otherwise.

The one hazard, named
    A cache can answer a loop that never ends and never spends: the dollar and
    call ceilings stop nothing, because there is nothing left to spend and no
    call being made. ``max_hits`` is therefore a ceiling on the *cache's* own
    answers. When it is reached the boundary stops answering from the cache and
    forwards again, so a runaway agent resumes paying and every cap it was
    already subject to bites as before. The first time that happens is written
    to the ledger as a note, because a fall in hit rate that nobody can explain
    is exactly the kind of number this project exists to keep visible.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import events as ev
from .canon import num, sha256_hex, usd

DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_HITS = 1000

#: Event member names a hit event carries. Kept in one place so the verifier and
#: the writer cannot drift apart silently.
HIT_ORIGIN = "origin"


@dataclass(frozen=True, slots=True)
class Entry:
    """One cached response, immutable, safe to hand out without the lock.

    ``stored_at`` is a monotonic reading, not a wall-clock time: the cache ages
    entries against the same clock the caps and durations use, so a system clock
    step cannot make an entry immortal or instantly stale (see
    ``docs/resilience.md`` on clock skew).
    """

    key: str
    origin_seq: int
    origin_hash: str
    status: int
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    usage_source: str
    cost_usd: str
    body: bytes
    headers: tuple[tuple[str, str], ...]
    streaming: bool
    stored_at: float
    body_sha256: str

    def age_seconds(self, now: float) -> float:
        return max(0.0, now - self.stored_at)

    def usage(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
        }

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def build_entry(
    *,
    key: str,
    origin: Any,
    status: int,
    model: str,
    usage: tuple[int, int, int],
    usage_source: str,
    cost_usd: str,
    body: bytes,
    headers: Iterable[tuple[str, str]],
    streaming: bool,
    now: float,
) -> Entry:
    """Build a cache entry from a response the boundary just received.

    ``origin`` is the ledger event for that response; its sequence number and hash
    are what make the entry checkable later, so an event without them is refused
    here rather than producing an entry that cannot be audited.
    """
    seq = getattr(origin, "seq", None)
    digest = getattr(origin, "hash", None)
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError(f"a cache entry needs the sequence number of the response it copies, got {seq!r}")
    if not isinstance(digest, str) or not digest:
        raise ValueError("a cache entry needs the hash of the response event it copies")
    inp, cached, out = (int(part) for part in usage)
    return Entry(
        key=key,
        origin_seq=seq,
        origin_hash=digest,
        status=int(status),
        model=str(model),
        input_tokens=inp,
        cached_input_tokens=cached,
        output_tokens=out,
        usage_source=str(usage_source),
        cost_usd=str(cost_usd),
        body=bytes(body),
        headers=tuple((str(k), str(v)) for k, v in headers),
        streaming=bool(streaming),
        stored_at=float(now),
        body_sha256=sha256_hex(body),
    )


class ResponseCache:
    """A bounded, thread-safe, in-memory cache of provider responses.

    The cache is *off* until a policy turns it on. With ``enabled=False`` every
    lookup misses, nothing is stored, and no cache member appears on any event —
    which is why a run that does not mention caching produces exactly the ledger
    and receipt it produced before this module existed.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_hits: int = DEFAULT_MAX_HITS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(enabled, bool):
            raise ValueError(f"cache.enabled must be true or false, got {enabled!r}")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int | float):
            raise ValueError(f"cache.ttl_seconds must be a number of seconds, got {ttl_seconds!r}")
        ttl = float(ttl_seconds)
        if ttl != ttl or ttl in (float("inf"), float("-inf")) or ttl < 0:
            raise ValueError(f"cache.ttl_seconds must be a finite, non-negative number, got {ttl_seconds!r}")
        for name, value, floor in (("max_entries", max_entries, 1), ("max_hits", max_hits, 0)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"cache.{name} must be a whole number, got {value!r}")
            if value < floor:
                raise ValueError(f"cache.{name} must be at least {floor}, got {value}")
        self.enabled = enabled
        self.ttl_seconds = ttl
        self.max_entries = int(max_entries)
        self.max_hits = int(max_hits)
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, Entry] = OrderedDict()
        # Telemetry, not evidence: these counters feed `/docket/metrics` and the
        # debug line, while every number in a receipt is rebuilt from the ledger.
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.evictions = 0
        self.expirations = 0
        self.refusals_at_ceiling = 0
        self.saved_usd = Decimal("0")
        self.avoided_tokens = 0

    # -- reading ------------------------------------------------------------ #
    @property
    def exhausted(self) -> bool:
        """True once the hit ceiling has been reached (``max_hits = 0``: never)."""
        if self.max_hits == 0:
            return False
        with self._lock:
            return self.hits >= self.max_hits

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __bool__(self) -> bool:
        """A cache is always a cache, even when it is empty.

        ``__len__`` makes an empty cache falsy, and that is a trap for a caller who writes
        ``cache or ResponseCache(...)``: the object they passed, configured and enabled, is
        quietly replaced by a different one. The boundary had exactly that bug — found by
        ``docket selftest``, not by the unit tests, because those pass their cache through the
        policy. Truthiness derived from a collection is only safe when "empty" and "absent" mean
        the same thing, and here they do not: ask ``cache.enabled``.
        """
        return True

    def now(self) -> float:
        """The cache's clock reading, so callers age entries against the same source."""
        return self._clock()

    def lookup(self, key: str) -> Entry | None:
        """Return the entry for ``key``, or ``None`` — and say why in the counters.

        An entry that has outlived its TTL is dropped here rather than left to
        occupy space: a stale entry that is only filtered at read time is a cache
        whose memory is bounded by traffic, not by configuration.
        """
        if not self.enabled:
            return None
        with self._lock:
            if self.exhausted_under_lock():
                self.refusals_at_ceiling += 1
                return None
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if self.ttl_seconds > 0 and entry.age_seconds(self._clock()) > self.ttl_seconds:
                del self._entries[key]
                self.expirations += 1
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry

    def store(self, entry: Entry) -> bool:
        """Insert ``entry``, evicting the least recently used if the cache is full.

        Refuses (returns ``False``) when caching is off, or when the entry is not
        a response the provider actually sent. Every refusal is the caller's to
        explain on the response event: ``cache.reason`` names it, so a run whose
        hit rate is lower than expected has the reason in its own record.
        """
        if not self.enabled:
            return False
        if not (200 <= entry.status < 300):
            return False
        if not entry.key:
            return False
        with self._lock:
            while len(self._entries) >= self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1
            existing = self._entries.get(entry.key)
            self._entries[entry.key] = entry
            self._entries.move_to_end(entry.key)
            if existing is None:
                self.stores += 1
            return True

    # -- writing ------------------------------------------------------------ #
    def note_served(self, entry: Entry, avoided_usd: Decimal) -> None:
        """Record that ``entry`` was served and what it would have cost.

        The avoided amount is quoted by the caller against the run's price table,
        because the cache deliberately knows nothing about pricing: one place
        prices calls (``pricing.CostMeter``), and the cache is not a second one.
        """
        with self._lock:
            self.saved_usd += avoided_usd
            self.avoided_tokens += entry.total_tokens()

    def invalidate(self, key: str) -> bool:
        """Drop one entry. Returns whether there was one.

        Nothing in the shipped product calls this: entries expire by TTL, are
        evicted by capacity, and responses that are not the provider's own are
        never stored. It is public because a host application that knows
        something the cache does not — a deploy, a policy change it decided to
        apply mid-run — should be able to act on it, and a method that exists and
        is tested is honest about being available.
        """
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> int:
        """Drop every entry, returning how many there were."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    # -- introspection ------------------------------------------------------ #
    def exhausted_under_lock(self) -> bool:
        return self.max_hits > 0 and self.hits >= self.max_hits

    def policy_dict(self) -> dict[str, Any]:
        """The cache's configuration, in the shape the policy file uses.

        Floats never reach a hashed structure (§3.3 of the spec), so the TTL is
        encoded through :func:`docket.canon.num`.
        """
        return {
            "enabled": True,
            "ttl_seconds": num(self.ttl_seconds),
            "max_entries": self.max_entries,
            "max_hits": self.max_hits,
        }

    def snapshot(self) -> dict[str, Any]:
        """Counters and configuration for ``/docket/metrics`` and the debug line."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "ttl_seconds": self.ttl_seconds,
                "max_entries": self.max_entries,
                "max_hits": self.max_hits,
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "stores": self.stores,
                "evictions": self.evictions,
                "expirations": self.expirations,
                "refusals_at_ceiling": self.refusals_at_ceiling,
                "saved_usd": usd(self.saved_usd),
                "avoided_tokens": self.avoided_tokens,
            }


# --------------------------------------------------------------------------- #
# The receipt's view of a cached session
# --------------------------------------------------------------------------- #


def _groups(events: Iterable[Any]) -> OrderedDict[tuple[Any, ...], dict[str, Any]]:
    """Group hit events by what they replay, never merging two different claims.

    Two hits belong to the same group only when they replay the same origin *and*
    report the same model, usage and avoided amount. Any disagreement produces a
    second group rather than a quiet merge: the verifier then finds one of the two
    groups disagreeing with the origin it cites, instead of a sum that reconciles
    to a number no single hit ever claimed.
    """
    grouped: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
    for event in events:
        if event.type != ev.MODEL_CACHE_HIT:
            continue
        data = event.data or {}
        origin = data.get(HIT_ORIGIN) or {}
        usage = data.get("usage") or {}
        triple = (
            int(usage.get("input_tokens") or 0),
            int(usage.get("cached_input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
        )
        key = (
            origin.get("seq"),
            str(origin.get("hash") or ""),
            str(data.get("key") or ""),
            str(data.get("model") or ""),
            triple,
            str(data.get("avoided_cost_usd") or ""),
        )
        group = grouped.get(key)
        if group is None:
            group = {
                "key": str(data.get("key") or ""),
                "origin_seq": origin.get("seq"),
                "origin_hash": str(origin.get("hash") or ""),
                "model": str(data.get("model") or ""),
                "usage": {
                    "input_tokens": triple[0],
                    "cached_input_tokens": triple[1],
                    "output_tokens": triple[2],
                },
                "avoided_usd_per_hit": str(data.get("avoided_cost_usd") or "0.00"),
                "usage_source": str(data.get("usage_source") or ""),
                "hits": 0,
                "first_seq": event.seq,
                "last_seq": event.seq,
                "event_hash": event.hash,
            }
            grouped[key] = group
        group["hits"] += 1
        group["last_seq"] = event.seq
    return grouped


def summary(
    cache: ResponseCache | None, events: list[Any], proofs: dict[int, list[dict[str, str]]] | None = None
) -> dict[str, Any] | None:
    """Build the receipt's ``cache`` member from the ledger, or ``None``.

    ``None`` means "this run had no cache", which is a different statement from
    ``{"enabled": false}`` — the second says the policy mentioned a cache and
    turned it off. Members:

    ``hits``
        The number of calls the cache answered. A boundary measurement, in the
        same class as a latency sample: bound to the ledger by the inclusion
        proof on each group, checked against the ledger's events when the ledger
        is supplied, and never derivable from arithmetic.
    ``saved_usd`` / ``avoided_tokens``
        Recomputed by the verifier from the origin rows in ``appendix.usage`` and
        the embedded price table. If these ever disagree with that arithmetic,
        verification fails.
    ``by_origin``
        One row per (origin, key, usage, amount): what was replayed, how many
        times, and one inclusion proof pinning the group to the ledger's Merkle
        root.
    """
    if cache is None or (not cache.enabled and not any(e.type == ev.MODEL_CACHE_HIT for e in events)):
        return None
    groups = list(_groups(events).values())
    hits_total = sum(int(g["hits"]) for g in groups)
    saved = Decimal("0")
    avoided_tokens = 0
    for group in groups:
        if proofs is not None and group["first_seq"] in proofs:
            group["merkle_path"] = proofs[group["first_seq"]]
        group["avoided_usd_total"] = usd(Decimal(str(group["avoided_usd_per_hit"])) * Decimal(int(group["hits"])))
        saved += Decimal(str(group["avoided_usd_per_hit"])) * Decimal(int(group["hits"]))
        usage = group["usage"]
        avoided_tokens += (int(usage["input_tokens"]) + int(usage["output_tokens"])) * int(group["hits"])
    block: dict[str, Any] = {
        "enabled": True,
        "policy": cache.policy_dict() if cache is not None else {},
        "hits": hits_total,
        "saved_usd": usd(saved),
        "avoided_tokens": avoided_tokens,
        "by_origin": groups,
        "hits_ceiling_reached": any(
            e.type == ev.NOTE and isinstance(e.data, dict) and e.data.get("event") == "cache.hits_ceiling_reached"
            for e in events
        ),
        "note": (
            "Every hit is a model.cache_hit event in the ledger; the saved amount is recomputed by "
            "docket verify from appendix.usage and the embedded price table, and each group below "
            "pins one of its hits to ledger.merkle_root with an inclusion proof. The saving is "
            "reported beside the spend, never inside it: totals.cost_usd remains money that was "
            "actually spent."
        ),
    }
    return block


def ceiling_note(max_hits: int) -> dict[str, Any]:
    """The ledger note written the first time the hit ceiling is reached."""
    return {
        "event": "cache.hits_ceiling_reached",
        "max_hits": int(max_hits),
        "message": (
            f"the response cache has answered its configured maximum of {int(max_hits)} calls; "
            "further identical requests are forwarded upstream again, so spend resumes and the "
            "usual caps apply"
        ),
    }


def storable(*, status: int | None, truncated: bool, injected: str | None) -> tuple[bool, str]:
    """Whether a response may be cached, and if not, why not — in words.

    The decision is made *before* the response event is written, so the event and
    the cache can never disagree about what happened, and the reason travels with
    the event instead of living only in a counter. The three refusals are the
    three ways a response stops being the provider's answer:

    * it is not a success (a 401, a provider 429, a 5xx, or no response at all);
    * it was cut short in flight, so what Docket holds is a prefix;
    * the chaos injector rewrote it, so caching it would make one injected fault
      sticky for the whole run.
    """
    if status is None or not (200 <= status < 300):
        return False, "not cached: the provider did not return a successful response"
    if truncated:
        return False, "not cached: the response was cut short in flight, so what arrived is a prefix"
    if injected:
        return False, f"not cached: the response was rewritten by fault injection ({injected})"
    return True, ""


def response_cache_metadata(key: str, decision: tuple[bool, str]) -> dict[str, Any]:
    """The ``cache`` member placed on a ``model.response`` event.

    ``key`` is the same digest the request event recorded as ``request_sha256``,
    which is what makes an entry findable: a verifier can walk from a hit back to
    the response it replays without re-hashing any body.
    """
    stored, reason = decision
    meta: dict[str, Any] = {"key": key, "stored": bool(stored)}
    if reason:
        meta["reason"] = reason
    return meta
