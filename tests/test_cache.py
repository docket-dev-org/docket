"""The response cache: what it may answer, what it must never answer, and what it proves.

Every test here is written against one of the rules in ``docket/cache.py``'s
docstring, because those rules are the whole argument for shipping a cache inside a
recording tool: an entry only ever holds a response the provider actually sent; a hit
serves the origin's bytes to a request of the same kind; every hit is an event; the
saving is recomputed by ``docket verify`` from the origin's row and the price table;
invalidation is visible (TTL, capacity, or a refusal to store); and a cached loop is
stopped by a ceiling on the cache's own answers, because no dollar cap can stop a loop
that spends nothing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from docket import cache as response_cache
from docket import events as ev
from docket.cache import ResponseCache, build_entry, storable, summary  # noqa: F401 - summary is used
from docket.fakeupstream import FakeUpstream, Step
from docket.ledger import Ledger
from docket.policy import Cache, Caps, Policy
from docket.pricing import CostMeter, PriceTable
from docket.proxy import RecordingProxy
from docket.receipt import build_receipt
from docket.resilience import CircuitBreaker
from docket.verify import verify_receipt

BODY = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "the same question"}]}


class FakeClock:
    """A clock the test moves by hand, so no test here sleeps to prove a TTL."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _entry(
    key: str = "k1", *, status: int = 200, body: bytes = b"{}", now: float = 1000.0, origin: object | None = None
) -> response_cache.Entry:
    if origin is None:

        class Origin:
            seq = 7
            hash = "a" * 64

        origin = Origin()

    return build_entry(
        key=key,
        origin=origin,
        status=status,
        model="claude-sonnet-5",
        usage=(1000, 0, 200),
        usage_source="reported",
        cost_usd="0.006000",
        body=body,
        headers=[("Content-Type", "application/json")],
        streaming=False,
        now=now,
    )


# --------------------------------------------------------------------------- #
# The cache object on its own
# --------------------------------------------------------------------------- #


def test_the_cache_is_off_until_a_policy_turns_it_on():
    cache = ResponseCache()
    assert cache.enabled is False
    assert cache.store(_entry()) is False
    assert cache.lookup("k1") is None
    assert len(cache) == 0
    # Nothing was counted either: a disabled cache has not missed anything, it is
    # simply not part of the run, and a dashboard reading "0 hits" from it would be
    # reading a measurement that was never taken.
    assert cache.snapshot()["misses"] == 0


def test_a_hit_returns_the_entry_and_a_miss_is_counted():
    cache = ResponseCache(enabled=True, clock=FakeClock())
    cache.store(_entry())
    hit = cache.lookup("k1")
    assert hit is not None and hit.key == "k1"
    assert cache.lookup("nope") is None
    view = cache.snapshot()
    assert (view["hits"], view["misses"], view["stores"], view["entries"]) == (1, 1, 1, 1)


def test_an_entry_expires_at_its_ttl_and_is_dropped():
    clock = FakeClock()
    cache = ResponseCache(enabled=True, ttl_seconds=30.0, clock=clock)
    cache.store(_entry(now=clock()))
    clock.advance(29.0)
    assert cache.lookup("k1") is not None
    clock.advance(2.0)
    assert cache.lookup("k1") is None
    view = cache.snapshot()
    assert view["expirations"] == 1
    # Dropped, not merely filtered: a stale entry that stays resident is memory held
    # by traffic rather than by configuration.
    assert view["entries"] == 0


def test_a_ttl_of_zero_means_entries_never_expire_on_time():
    clock = FakeClock()
    cache = ResponseCache(enabled=True, ttl_seconds=0.0, clock=clock)
    cache.store(_entry(now=clock()))
    clock.advance(86_400.0)
    assert cache.lookup("k1") is not None
    assert cache.snapshot()["expirations"] == 0


def test_a_ttl_of_one_second_still_expires():
    """Kills `self.ttl_seconds > 0 -> > 1`, the one TTL a whole-second reading can hide.

    A guard written `> 1` treats a one-second TTL as "no TTL at all", so the entry lives until it is
    evicted: the cache would answer with a response the policy said was stale one second after it was
    stored. Thirty seconds, the TTL the other expiry test uses, cannot tell the two readings apart.
    """
    clock = FakeClock()
    cache = ResponseCache(enabled=True, ttl_seconds=1.0, clock=clock)
    cache.store(_entry(now=clock()))
    clock.advance(1.5)
    assert cache.lookup("k1") is None
    assert cache.snapshot()["expirations"] == 1


def test_the_least_recently_used_entry_is_evicted_first():
    cache = ResponseCache(enabled=True, max_entries=2, clock=FakeClock())
    cache.store(_entry("a"))
    cache.store(_entry("b"))
    assert cache.lookup("a") is not None  # a is now the most recently used
    cache.store(_entry("c"))
    assert cache.lookup("b") is None
    assert cache.lookup("a") is not None
    assert cache.lookup("c") is not None
    assert cache.snapshot()["evictions"] == 1


def test_a_hit_ceiling_stops_the_cache_answering_and_says_why():
    cache = ResponseCache(enabled=True, max_hits=2, clock=FakeClock())
    cache.store(_entry())
    assert cache.lookup("k1") is not None
    assert cache.lookup("k1") is not None
    assert cache.exhausted is True
    assert cache.lookup("k1") is None
    assert cache.snapshot()["refusals_at_ceiling"] == 1
    # The entry is still there: the ceiling stops the cache *answering*, it does not
    # throw away what it has, so a run that lifts the ceiling is still warm.
    assert len(cache) == 1


def test_max_hits_zero_means_there_is_no_ceiling():
    cache = ResponseCache(enabled=True, max_hits=0, clock=FakeClock())
    cache.store(_entry())
    for _ in range(50):
        assert cache.lookup("k1") is not None
    assert cache.exhausted is False


def test_one_entry_can_be_invalidated_and_the_whole_cache_cleared():
    cache = ResponseCache(enabled=True, clock=FakeClock())
    cache.store(_entry("a"))
    cache.store(_entry("b"))
    assert cache.invalidate("a") is True
    assert cache.invalidate("a") is False
    assert len(cache) == 1
    assert cache.clear() == 1
    assert len(cache) == 0


def test_a_configuration_that_cannot_be_honoured_is_refused_by_name():
    with pytest.raises(ValueError, match="ttl_seconds"):
        ResponseCache(enabled=True, ttl_seconds=-1.0)
    with pytest.raises(ValueError, match="ttl_seconds"):
        ResponseCache(enabled=True, ttl_seconds=float("nan"))
    with pytest.raises(ValueError, match="max_entries"):
        ResponseCache(enabled=True, max_entries=0)
    with pytest.raises(ValueError, match="max_hits"):
        ResponseCache(enabled=True, max_hits=-1)
    with pytest.raises(ValueError, match="enabled"):
        ResponseCache(enabled=1)  # type: ignore[arg-type]


def test_only_a_response_the_provider_sent_may_be_stored():
    cache = ResponseCache(enabled=True, clock=FakeClock())
    assert cache.store(_entry(status=500)) is False
    assert cache.store(_entry(key="")) is False
    assert len(cache) == 0


def test_every_refusal_to_cache_names_its_reason():
    ok, reason = storable(status=200, truncated=False, injected=None)
    assert ok is True and reason == ""
    ok, reason = storable(status=429, truncated=False, injected=None)
    assert ok is False and "successful response" in reason
    ok, reason = storable(status=None, truncated=False, injected=None)
    assert ok is False and "successful response" in reason
    ok, reason = storable(status=200, truncated=True, injected=None)
    assert ok is False and "prefix" in reason
    ok, reason = storable(status=200, truncated=False, injected="corrupt body: 8 bytes")
    assert ok is False and "fault injection" in reason


def test_an_entry_needs_the_response_event_it_copies():
    class Nameless:
        seq = None
        hash = None

    with pytest.raises(ValueError, match="sequence number"):
        build_entry(
            key="k",
            origin=Nameless(),
            status=200,
            model="m",
            usage=(1, 0, 1),
            usage_source="reported",
            cost_usd="0.00",
            body=b"{}",
            headers=[],
            streaming=False,
            now=0.0,
        )


# --------------------------------------------------------------------------- #
# The receipt's view of the cache
# --------------------------------------------------------------------------- #


def _hit(data: dict, seq: int = 10, event_type: str = ev.MODEL_CACHE_HIT) -> object:
    payload = {"key": "k", "model": "claude-sonnet-5", "avoided_cost_usd": "0.01", "usage": {"input_tokens": 5}}
    payload.update(data)

    class _Event:
        type = event_type
        hash = f"{seq:064d}"

        def __init__(self) -> None:
            self.seq = seq
            self.data = payload

    return _Event()


def test_a_run_without_a_cache_carries_no_cache_summary():
    assert summary(None, []) is None
    assert summary(ResponseCache(), []) is None


def test_a_hit_that_replays_a_response_with_no_input_tokens_says_zero():
    """Kills `int(usage.get("input_tokens") or 0) -> or 1` in `_groups`' usage triple.

    A response the provider reported as using no input tokens — a cached tool result, an empty
    completion — replayed `n` times saved exactly that many output tokens, and the receipt's
    `avoided_tokens` is arithmetic over the origin's own usage row. Reading a missing-or-zero field
    as `1` would tell the ledger's own story with a number the ledger does not support, one token
    per hit too high, which is the kind of drift verification exists to forbid.
    """
    events = [
        _hit(
            {
                "origin": {"seq": 0, "hash": "b" * 64},
                "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 40},
            },
            seq=10,
        )
    ]
    block = summary(ResponseCache(enabled=True), events)
    assert block is not None
    group = block["by_origin"][0]
    assert group["usage"] == {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 40}
    assert block["avoided_tokens"] == 40


def test_an_entry_is_served_at_exactly_its_ttl_and_dropped_after_it():
    """Kills `entry.age_seconds(...) > self.ttl_seconds -> >=`, and says what "outlived" means.

    `lookup`'s docstring promises that an entry which has *outlived* its TTL is dropped — a
    comparison in the other direction would drop an entry on the instant it is exactly as old as
    the TTL allows, which is a boundary the suite otherwise leaves to whichever way the operator
    was written. Stated as a test, a reader can disagree with the promise; unstated, it is a coin
    flip in a receipt.
    """
    clock = FakeClock()
    cache = ResponseCache(enabled=True, ttl_seconds=30.0, clock=clock)
    cache.store(_entry(now=clock()))
    clock.advance(30.0)
    assert cache.lookup("k1") is not None
    clock.advance(0.001)
    assert cache.lookup("k1") is None
    assert cache.snapshot()["expirations"] == 1


def test_the_summary_counts_hits_per_origin_and_sums_the_saving():
    events = [
        _hit({"origin": {"seq": 3, "hash": "a" * 64}}, seq=10),
        _hit({"origin": {"seq": 3, "hash": "a" * 64}}, seq=11),
        _hit({"origin": {"seq": 3, "hash": "a" * 64}}, seq=12),
    ]
    block = summary(ResponseCache(enabled=True), events)
    assert block is not None
    assert block["hits"] == 3
    assert len(block["by_origin"]) == 1
    group = block["by_origin"][0]
    assert group["origin_seq"] == 3
    assert group["hits"] == 3
    assert group["first_seq"] == 10 and group["last_seq"] == 12
    assert group["avoided_usd_per_hit"] == "0.01"
    assert group["avoided_usd_total"] == "0.03"
    assert block["saved_usd"] == "0.03"  # usd() writes whole cents with two decimals


def test_two_hits_that_disagree_are_never_merged_into_one_number():
    """A sum that reconciles to nothing any single hit claimed is worse than two rows.

    Two hits replaying the same origin but reporting different usage cannot both be
    right. Grouping them would produce a total that no hit in the ledger supports, so
    they stay separate — and verification then finds one of them disagreeing with the
    origin it cites.
    """
    events = [
        _hit({"origin": {"seq": 3, "hash": "a" * 64}, "usage": {"input_tokens": 5}}, seq=10),
        _hit({"origin": {"seq": 3, "hash": "a" * 64}, "usage": {"input_tokens": 900}}, seq=11),
    ]
    block = summary(ResponseCache(enabled=True), events)
    assert block is not None
    assert len(block["by_origin"]) == 2
    assert sorted(g["hits"] for g in block["by_origin"]) == [1, 1]


def test_two_hits_that_replayed_different_requests_stay_two_rows():
    """Kills `str(data.get("key") or "") -> ... and ""` in the group identity.

    A group is (origin, key, usage, amount) — the identity the verifier recomputes from the
    ledger when it checks that every hit a receipt claims has an event behind it. Drop the key
    from that identity and two hits that copied the same origin for *different* requests are
    merged: the receipt then reports one row, `hits: 2`, and one inclusion proof, and the second
    request's own evidence has nowhere to appear. The mutant reads `str(data.get("key") and "")`,
    which is the empty string for exactly the events that carry a key, so the surviving row's key
    is the empty string — asserted here as well as the count, because a row that names nothing is
    how the merge shows up in a reader's hands.
    """
    events = [
        _hit({"origin": {"seq": 3, "hash": "a" * 64}, "key": "sha256:first"}, seq=10),
        _hit({"origin": {"seq": 3, "hash": "a" * 64}, "key": "sha256:second"}, seq=11),
    ]
    block = summary(ResponseCache(enabled=True), events)
    assert block is not None
    assert len(block["by_origin"]) == 2, "one origin, two requests: two rows"
    assert sorted(g["key"] for g in block["by_origin"]) == ["sha256:first", "sha256:second"]
    assert sorted(g["hits"] for g in block["by_origin"]) == [1, 1]


def test_two_hits_for_different_models_stay_two_rows():
    """Kills the same mutant one field along: `str(data.get("model") or "") -> and ""`.

    The model is in the identity for the reason the key is: `docket verify` recomputes the group
    from the ledger, and a row that does not say which model was answered cannot be checked against
    the origin it cites — the price table is per model, so the saving depends on it. Merging the two
    also hands the reader the *first* event's model with `hits: 2`, a row that is wrong about one of
    the two hits it claims.
    """
    events = [
        _hit({"origin": {"seq": 3, "hash": "a" * 64}, "model": "claude-sonnet-5"}, seq=10),
        _hit({"origin": {"seq": 3, "hash": "a" * 64}, "model": "claude-haiku-4-5"}, seq=11),
    ]
    block = summary(ResponseCache(enabled=True), events)
    assert block is not None
    assert len(block["by_origin"]) == 2
    assert sorted(g["model"] for g in block["by_origin"]) == ["claude-haiku-4-5", "claude-sonnet-5"]
    assert sorted(g["hits"] for g in block["by_origin"]) == [1, 1]


def test_two_hits_that_saved_different_amounts_are_summed_not_multiplied():
    """Kills `str(data.get("avoided_cost_usd") or "") -> and ""`, and prices the merge.

    This is the mutant with a number attached. Two hits against one origin, at $0.01 and $0.02, are
    merged by the mutation into a single row whose per-hit amount is the *first* event's — so
    `saved_usd` reports $0.02 for $0.03 of saving, and every subsequent arithmetic step in the
    receipt (the verifier's recomputation from `appendix.usage`, the markdown report's total) is
    consistent with that wrong row. The row count and the sum are both asserted, because either one
    alone can be satisfied by a later coincidence.
    """
    events = [
        _hit({"origin": {"seq": 3, "hash": "a" * 64}, "avoided_cost_usd": "0.01"}, seq=10),
        _hit({"origin": {"seq": 3, "hash": "a" * 64}, "avoided_cost_usd": "0.02"}, seq=11),
    ]
    block = summary(ResponseCache(enabled=True), events)
    assert block is not None
    assert len(block["by_origin"]) == 2
    assert sorted(g["avoided_usd_per_hit"] for g in block["by_origin"]) == ["0.01", "0.02"]
    assert block["saved_usd"] == "0.03", "0.01 + 0.02 is 0.03, not 0.01 twice"


def test_the_ceiling_is_reported_only_when_the_ledger_records_it():
    class _Note:
        type = ev.NOTE
        seq = 4
        hash = "b" * 64
        data = response_cache.ceiling_note(10)

    without = summary(ResponseCache(enabled=True, max_hits=10), [_hit({"origin": {"seq": 3, "hash": "a" * 64}})])
    with_note = summary(
        ResponseCache(enabled=True, max_hits=10), [_hit({"origin": {"seq": 3, "hash": "a" * 64}}), _Note()]
    )
    assert without is not None and without["hits_ceiling_reached"] is False
    assert with_note is not None and with_note["hits_ceiling_reached"] is True


# --------------------------------------------------------------------------- #
# The policy surface
# --------------------------------------------------------------------------- #


def test_a_policy_that_says_nothing_about_caching_keeps_the_digest_it_had():
    """The compatibility property, stated as a test: silence must not move a digest.

    Receipts and signatures name the digest of the policy they ran under. A feature
    added to Docket must not change the digest of a policy that does not use it, or
    every receipt written before the feature existed becomes unverifiable.
    """
    default = Policy()
    assert "cache" not in default.to_dict()
    explicit_off = Policy.from_dict({"cache": {"enabled": False}})
    assert explicit_off.to_dict() == default.to_dict()
    assert explicit_off.digest() == default.digest()
    on = Policy.from_dict({"cache": {"enabled": True}})
    assert "cache" in on.to_dict()
    assert on.digest() != default.digest()


def test_cache_policy_keys_are_parsed_and_bad_ones_are_refused():
    parsed = Policy.from_dict({"cache": {"enabled": True, "ttl_seconds": 12.5, "max_entries": 4, "max_hits": 7}})
    assert (parsed.cache.enabled, parsed.cache.ttl_seconds, parsed.cache.max_entries, parsed.cache.max_hits) == (
        True,
        12.5,
        4,
        7,
    )
    assert parsed.to_dict()["cache"]["ttl_seconds"] == "12.5"
    with pytest.raises(ValueError, match="cache.ttl_seconds"):
        Policy.from_dict({"cache": {"enabled": True, "ttl_seconds": -1}})
    with pytest.raises(ValueError, match="cache.max_entries"):
        Policy.from_dict({"cache": {"enabled": True, "max_entries": 0}})
    with pytest.raises(ValueError, match="cache.enabled"):
        Policy.from_dict({"cache": {"enabled": "yes"}})
    with pytest.raises(ValueError, match=r"\[cache\] must be a table"):
        Policy.from_dict({"cache": True})


def test_naming_a_cache_key_turns_the_cache_on_unless_enabled_says_otherwise():
    """The same rule as the command-line flags, because it is the same intention.

    `[cache]` with only `ttl_seconds` is someone asking for a cache; leaving it off would
    give them a configuration they never meant and no error to notice it by. `enabled =
    false` beside other keys is the one explicit way to say "tuned, but off".
    """
    tuned = Policy.from_dict({"cache": {"ttl_seconds": 30}})
    assert tuned.cache.enabled is True and tuned.cache.ttl_seconds == 30
    assert Policy.from_dict({"cache": {"max_hits": 5}}).cache.enabled is True
    off = Policy.from_dict({"cache": {"enabled": False, "ttl_seconds": 30}})
    assert off.cache.enabled is False and off.cache.ttl_seconds == 30
    # And an empty section is not a statement about anything: the cache stays off.
    assert Policy.from_dict({"cache": {}}).cache.enabled is False
    assert Policy.from_dict({"cache": {}}).digest() == Policy().digest()


def test_the_disclosure_says_whether_a_cache_is_in_play():
    off = Policy().limits_disclosure()
    on = Policy.from_dict({"cache": {"enabled": True}}).limits_disclosure()
    assert off["cache_behaviour"] == ["no response cache is configured: every request is forwarded"]
    assert any("model.cache_hit" in line for line in on["cache_behaviour"])


# --------------------------------------------------------------------------- #
# At the boundary
# --------------------------------------------------------------------------- #


class _Tunnel:
    """The proxy's upstream URL holder, so tests read the address the proxy is on."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.proxy: RecordingProxy | None = None
        self.meter: CostMeter | None = None
        self.base = ""

    def start(self, upstream_url: str, policy: Policy, **kwargs) -> _Tunnel:
        self.meter = CostMeter(price_table=PriceTable.load())
        self.proxy = RecordingProxy(
            ledger=self.ledger,
            policy=policy,
            meter=self.meter,
            session_id="s1",
            upstreams={"openai": upstream_url},
            **kwargs,
        )
        host, port = self.proxy.start()
        self.base = f"http://{host}:{port}"
        return self

    def post(self, payload: dict, path: str = "/openai/v1/chat/completions") -> tuple[int, str]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")

    def stop(self) -> None:
        if self.proxy is not None:
            self.proxy.stop()


@pytest.fixture()
def tunnelled(ledger: Ledger):
    made: list[_Tunnel] = []

    def _make(upstream_url: str, policy: Policy, **kwargs) -> _Tunnel:
        tunnel = _Tunnel(ledger).start(upstream_url, policy, **kwargs)
        made.append(tunnel)
        return tunnel

    yield _make
    for tunnel in made:
        tunnel.stop()


def _cache_policy(**kwargs) -> Policy:
    return Policy(name="cached", caps=Caps(max_usd=None), cache=Cache(enabled=True, **kwargs))


def test_a_second_identical_call_is_answered_from_the_cache(tunnelled, ledger, fake_upstream):
    tunnel = tunnelled(fake_upstream.url, _cache_policy())
    first = tunnel.post(BODY)
    spend_after_first = tunnel.meter.cost
    second = tunnel.post(BODY)

    assert first == second
    assert len(fake_upstream.requests) == 1  # the provider saw one call, not two
    assert [e.type for e in ledger] == [ev.MODEL_REQUEST, ev.MODEL_RESPONSE, ev.MODEL_CACHE_HIT]
    response, hit = ledger.events[1], ledger.events[2]
    request = ledger.events[0]
    assert response.data["cache"] == {"key": request.data["request_sha256"], "stored": True}
    assert hit.data["origin"] == {"seq": response.seq, "hash": response.hash}
    assert hit.data["key"] == request.data["request_sha256"]
    assert hit.data["usage"] == response.data["usage"]
    assert hit.data["avoided_cost_usd"] == response.data["cost_usd"]
    # The hit moves nothing that describes money or calls: it is a call that did not
    # happen, and every total in the receipt has to keep meaning what it always meant.
    assert tunnel.meter.calls == 1
    assert tunnel.meter.cost == spend_after_first
    assert tunnel.proxy.stats.model_calls == 1


def test_a_different_request_is_a_miss_even_when_the_cache_is_on(tunnelled, fake_upstream):
    tunnel = tunnelled(fake_upstream.url, _cache_policy())
    tunnel.post(BODY)
    tunnel.post({**BODY, "messages": [{"role": "user", "content": "a different question"}]})
    assert len(fake_upstream.requests) == 2
    assert tunnel.proxy.cache.snapshot()["hits"] == 0
    assert tunnel.proxy.cache.snapshot()["misses"] == 2


def test_without_a_policy_key_nothing_is_cached_or_served(tunnelled, ledger, fake_upstream):
    tunnel = tunnelled(fake_upstream.url, Policy(caps=Caps(max_usd=None)))
    tunnel.post(BODY)
    tunnel.post(BODY)
    assert len(fake_upstream.requests) == 2
    assert [e.type for e in ledger] == [ev.MODEL_REQUEST, ev.MODEL_RESPONSE, ev.MODEL_REQUEST, ev.MODEL_RESPONSE]
    # No cache member anywhere, which is what keeps every receipt written before this
    # feature byte-identical to the receipt the same run produces today.
    for event in ledger:
        assert "cache" not in event.data


def test_a_streaming_hit_returns_the_bytes_the_origin_streamed(tunnelled, ledger, fake_upstream):
    tunnel = tunnelled(fake_upstream.url, _cache_policy())
    body = {**BODY, "stream": True}
    first = tunnel.post(body)
    second = tunnel.post(body)
    assert first == second
    assert "data:" in first[1]
    assert len(fake_upstream.requests) == 1
    assert [e.type for e in ledger][-1] == ev.MODEL_CACHE_HIT
    assert ledger.events[-1].data["streaming"] is True


def test_a_stream_and_a_non_stream_request_never_share_an_entry(tunnelled, ledger, fake_upstream):
    """The key covers the request as sent, so a body cannot be swapped for a stream.

    Serving a buffered body to a caller that asked for a stream would break the
    caller's parser in the dark; serving a stream to a caller that asked for JSON
    would hand it text where it expects an object. Two keys, and neither can answer
    for the other.
    """
    tunnel = tunnelled(fake_upstream.url, _cache_policy())
    tunnel.post(BODY)
    status, text = tunnel.post({**BODY, "stream": True})
    assert status == 200 and "data:" in text
    assert len(fake_upstream.requests) == 2
    assert tunnel.proxy.cache.snapshot()["hits"] == 0
    assert len(tunnel.proxy.cache) == 2


def test_a_failed_call_is_never_stored_and_never_served(tunnelled, ledger, fake_upstream):
    upstream = FakeUpstream(steps=[Step(status=500)])
    upstream.start()
    try:
        tunnel = tunnelled(upstream.url, _cache_policy())
        first = tunnel.post(BODY)
        second = tunnel.post(BODY)
        assert first[0] == 500 and second[0] == 500
        assert len(upstream.requests) == 2, "a failed response must not be cached"
        assert len(tunnel.proxy.cache) == 0
        errors = [e for e in ledger if e.type == ev.MODEL_ERROR]
        assert len(errors) == 2
        assert "cache" not in errors[0].data
    finally:
        upstream.stop()


def test_a_response_the_injector_touched_is_never_stored(tunnelled, ledger, fake_upstream):
    """One injected fault must not become the answer to every later call.

    Keying a cache on a corrupted body would make the corruption sticky for the rest
    of the run, and — worse — the receipt would then show a saving earned by replaying
    a fault. The response is still forwarded and still recorded; it is simply not kept.
    """

    class Fault:
        kind = "corrupt"

        @staticmethod
        def describe() -> str:
            return "corrupt body: 8 bytes"

    class Injector:
        enabled = True
        injected: list[object] = []

        @staticmethod
        def next_delay():
            return 0.0, []

        @staticmethod
        def before_forward():
            return None

        @staticmethod
        def after_response(status):
            return Fault()

    tunnel = tunnelled(fake_upstream.url, _cache_policy(), injector=Injector())
    tunnel.post(BODY)
    tunnel.post(BODY)
    assert len(fake_upstream.requests) == 2
    assert len(tunnel.proxy.cache) == 0
    stored = [e.data.get("cache") for e in ledger if e.type == ev.MODEL_RESPONSE]
    assert stored and stored[0]["stored"] is False
    assert "fault injection" in stored[0]["reason"]


def test_an_open_breaker_does_not_stop_a_call_the_cache_can_answer(tunnelled, ledger, fake_upstream):
    """A hit needs no upstream, so a provider outage is not its business.

    The run keeps working on what it already has — and the contract stays honest in
    both directions: the hit records the breaker's state, and a call the cache cannot
    answer is still refused while the breaker is open.
    """
    upstream = FakeUpstream(
        steps=[
            Step(model="claude-sonnet-5", input_tokens=10, output_tokens=2),
            Step(status=503),
        ]
    )
    upstream.start()
    try:
        tunnel = tunnelled(
            upstream.url,
            _cache_policy(),
            breaker=CircuitBreaker(threshold=1, cooldown_seconds=30.0, max_cooldown_seconds=60.0),
        )
        assert tunnel.post(BODY)[0] == 200  # stored
        other = {**BODY, "messages": [{"role": "user", "content": "another question"}]}
        assert tunnel.post(other)[0] == 503  # one failure trips the breaker
        status, _ = tunnel.post(BODY)
        assert status == 200, "the cached call should be served through the outage"
        assert tunnel.post(other)[0] == 503
        assert len(upstream.requests) == 2
        hit = [e for e in ledger if e.type == ev.MODEL_CACHE_HIT][-1]
        assert hit.data["circuit_state"] == "open"
    finally:
        upstream.stop()


def test_the_hit_ceiling_is_written_to_the_ledger_once_and_spend_resumes(tunnelled, ledger, fake_upstream):
    tunnel = tunnelled(fake_upstream.url, _cache_policy(max_hits=1))
    tunnel.post(BODY)  # miss, stored
    tunnel.post(BODY)  # hit
    spend_after_hit = tunnel.meter.cost
    tunnel.post(BODY)  # ceiling reached: forwarded again
    notes = [e for e in ledger if e.type == ev.NOTE and e.data.get("event") == "cache.hits_ceiling_reached"]
    assert len(notes) == 1
    assert notes[0].data["max_hits"] == 1
    assert len(fake_upstream.requests) == 2
    assert tunnel.meter.cost > spend_after_hit, "a capped cache must let the run spend again"
    assert tunnel.proxy.cache.snapshot()["refusals_at_ceiling"] >= 1


def test_the_metrics_expose_the_cache_only_when_the_policy_turns_it_on(tunnelled, fake_upstream):
    off = tunnelled(fake_upstream.url, Policy(caps=Caps(max_usd=None)))
    off.post(BODY)
    assert "docket_cache_hits_total" not in off.proxy.metrics_text()

    on = tunnelled(fake_upstream.url, _cache_policy())
    on.post(BODY)
    on.post(BODY)
    text = on.proxy.metrics_text()
    assert "docket_cache_hits_total 1" in text
    assert "docket_cache_misses_total 1" in text
    assert "docket_cache_enabled 1" in text
    assert "docket_cache_saved_usd 0.006000" in text


def test_concurrent_identical_calls_never_lose_a_hit_or_an_event(tunnelled, ledger, fake_upstream):
    """Two threads can both miss the same key; nothing may be lost when they do.

    Both forward, both record, both store — the second store replaces the first, and
    the hits that follow name whichever origin they were served from. What may not
    happen is a hit that no event supports or a response the cache cannot account for,
    so the arithmetic is asserted exactly: hits + upstream calls == client calls, and
    the ledger chains.
    """
    tunnel = tunnelled(fake_upstream.url, _cache_policy(), record_bodies=False)
    rounds = 6

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [pool.submit(tunnel.post, BODY) for _ in range(rounds)]
        statuses = [future.result()[0] for future in results]

    assert statuses == [200] * rounds
    hits = [e for e in ledger if e.type == ev.MODEL_CACHE_HIT]
    assert len(hits) + len(fake_upstream.requests) == rounds
    assert tunnel.proxy.cache.snapshot()["hits"] == len(hits)
    assert ledger.chain_error() is None
    for hit in hits:
        origin = ledger.events[hit.data["origin"]["seq"]]
        assert origin.type == ev.MODEL_RESPONSE
        assert origin.hash == hit.data["origin"]["hash"]
        assert origin.data["cache"]["stored"] is True


# --------------------------------------------------------------------------- #
# Receipts and verification
# --------------------------------------------------------------------------- #


def test_a_receipt_proves_the_saving_and_verification_recomputes_it(tmp_path, price_table):
    """The whole point, end to end: a hit, a receipt, and arithmetic a stranger can redo."""
    ledger = Ledger.open(tmp_path / "runs" / "cached.jsonl")
    try:
        upstream = FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=1000, output_tokens=200)])
        upstream.start()
        policy = _cache_policy()
        tunnel = _Tunnel(ledger).start(upstream.url, policy)
        try:
            ledger.append(ev.SESSION_START, {"argv": ["pytest"], "cwd": str(tmp_path), "host": {}})
            ledger.append(
                ev.POLICY_APPLIED, {"name": policy.name, "digest": policy.digest(), "document": policy.to_dict()}
            )
            tunnel.post(BODY)
            tunnel.post(BODY)
            tunnel.post(BODY)
            ledger.append(ev.SESSION_END, {"breached": None, "exit_code": 0})
            receipt = build_receipt(
                events=ledger.events,
                ledger=ledger,
                meter=tunnel.meter,
                policy=policy,
                session_id="s1",
                docket_number="DKT-20260101-ABCDEF",
                exit_code=0,
                duration_seconds=1.0,
                price_table=PriceTable.load(),
                cache=tunnel.proxy.cache,
            )
            block = receipt["cache"]
            assert block["hits"] == 2
            assert block["saved_usd"] == "0.012000"
            assert block["avoided_tokens"] == 2400
            assert block["policy"]["ttl_seconds"] == 300
            assert len(block["by_origin"]) == 1
            assert block["by_origin"][0]["hits"] == 2
            assert block["hits_ceiling_reached"] is False
            # The spend is untouched by the hits: $0.006 for the one call the provider saw.
            assert receipt["totals"]["cost_usd"] == "0.006000"
            assert receipt["model_calls"]["count"] == 1

            with_ledger = verify_receipt(receipt, ledger_events=ledger.events)
            assert with_ledger.ok, [c.detail for c in with_ledger.checks if c.status == "fail"]
            by_name = {c.name: c for c in with_ledger.checks if c.name.startswith("cache")}
            assert by_name["cache-savings"].status == "pass"
            assert by_name["cache-hit-origins"].status == "pass"
            assert by_name["cache-hit-proofs"].status == "pass"

            # And without the ledger, which is how most readers will get it: the saving
            # is still recomputed, from the origin row in the appendix and the price table.
            alone = verify_receipt(receipt)
            assert alone.ok, [c.detail for c in alone.checks if c.status == "fail"]
            assert {c.name: c.status for c in alone.checks}["cache-savings"] == "pass"
        finally:
            tunnel.stop()
            upstream.stop()
    finally:
        ledger.close()


def _cached_receipt(
    tmp_path: Path, hits: int = 2, ceiling: bool = False, extra_latency: int | None = None
) -> tuple[dict, Ledger, Policy]:
    """A minimal but complete cached session, built without a boundary.

    Verification is about documents, so the ledger is written directly: one metered
    response that was stored, and `hits` hits that replay it — the same shape the boundary
    produces, without starting three servers to get there. `hits=1` exists for the checks
    that are about *one* hit: a group claiming a single hit is the one shape where several
    of the verifier's counters and lookups can be swapped for their neighbours without the
    verdict changing, so it has tests of its own. `ceiling` writes the hit-ceiling note the
    boundary writes — the one ledger fact a receipt's `hits_ceiling_reached` must agree with.
    """
    from docket.canon import digest_of

    ledger = Ledger.open(tmp_path / "runs" / "c.jsonl")
    policy = _cache_policy()
    ledger.append(ev.SESSION_START, {"argv": ["pytest"], "cwd": str(tmp_path), "host": {}})
    ledger.append(ev.POLICY_APPLIED, {"name": policy.name, "digest": policy.digest(), "document": policy.to_dict()})
    key = digest_of({"body": BODY, "path": "/openai/v1/chat/completions"})
    ledger.append(
        ev.MODEL_REQUEST,
        {
            "provider": "openai",
            "model": "claude-sonnet-5",
            "method": "POST",
            "path": "/v1/chat/completions",
            "streaming": False,
            "fingerprint": "f" * 16,
            "request_sha256": key,
            "body_bytes": len(json.dumps(BODY)),
        },
    )
    response = ledger.append(
        ev.MODEL_RESPONSE,
        {
            "provider": "openai",
            "model": "claude-sonnet-5",
            "status": 200,
            "latency_ms": 12,
            "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
            "usage_source": "reported",
            "cost_usd": "0.006000",
            "cumulative_cost_usd": "0.006000",
            "streaming": False,
            "response_bytes": 42,
            "cache": {"key": key, "stored": True},
        },
    )
    if extra_latency is not None:
        # A second metered call with a *different* latency: with one call, p50 and p99 are the same
        # number, and a verifier that looked up the wrong percentile would still print the right
        # sentence. Two calls is the smallest session where those two numbers differ.
        ledger.append(
            ev.MODEL_RESPONSE,
            {
                "provider": "openai",
                "model": "claude-sonnet-5",
                "status": 200,
                "latency_ms": extra_latency,
                "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
                "usage_source": "reported",
                "cost_usd": "0.006000",
                "cumulative_cost_usd": "0.012000",
                "streaming": False,
                "response_bytes": 42,
                "cache": {"key": key, "stored": False},
            },
        )
    if ceiling:
        ledger.append(ev.NOTE, response_cache.ceiling_note(2))
    for _ in range(hits):
        ledger.append(
            ev.MODEL_CACHE_HIT,
            {
                "provider": "openai",
                "model": "claude-sonnet-5",
                "key": key,
                "origin": {"seq": response.seq, "hash": response.hash},
                "status": 200,
                "streaming": False,
                "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
                "usage_source": "reported",
                "avoided_cost_usd": "0.006000",
                "response_sha256": "c" * 64,
                "response_bytes": 42,
                "age_seconds": "1.5",
            },
        )
    ledger.append(ev.SESSION_END, {"breached": None, "exit_code": 0})
    meter = CostMeter(price_table=PriceTable.load())
    meter.record("claude-sonnet-5", 1000, 0, 200, "reported")
    if extra_latency is not None:
        meter.record("claude-sonnet-5", 1000, 0, 200, "reported")
    cache = ResponseCache(enabled=True)
    return (
        build_receipt(
            events=ledger.events,
            ledger=ledger,
            meter=meter,
            policy=policy,
            session_id="s1",
            docket_number="DKT-20260101-ABCDEF",
            exit_code=0,
            duration_seconds=1.0,
            price_table=PriceTable.load(),
            cache=cache,
        ),
        ledger,
        policy,
    )


def test_a_receipt_that_overstates_the_saving_fails_verification(tmp_path):
    receipt, ledger, _ = _cached_receipt(tmp_path)
    try:
        assert verify_receipt(receipt, ledger_events=ledger.events).ok
        receipt["cache"]["saved_usd"] = "9.99"
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert any("cache.saved_usd is 9.99" in f for f in result.failures)
    finally:
        ledger.close()


def test_a_hit_that_cites_an_unmetered_origin_fails_verification(tmp_path):
    receipt, ledger, _ = _cached_receipt(tmp_path)
    try:
        receipt["cache"]["by_origin"][0]["origin_seq"] = 99
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert any("not a metered call" in f for f in result.failures)
    finally:
        ledger.close()


def test_a_hit_that_replays_a_response_that_was_not_stored_fails_verification(tmp_path):
    receipt, ledger, _ = _cached_receipt(tmp_path)
    try:
        # The ledger says the response was stored under a different key, so the hit has
        # no origin: it claims to replay something that was never kept.
        receipt["cache"]["by_origin"][0]["key"] = "d" * 64
        receipt["cache"]["by_origin"][0]["event_hash"] = "e" * 64
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert any("cache-savings" in f or "cache-hit-origins" in f for f in result.failures)
    finally:
        ledger.close()


def test_a_receipt_that_claims_more_hits_than_the_ledger_has_fails_verification(tmp_path):
    receipt, ledger, _ = _cached_receipt(tmp_path)
    try:
        receipt["cache"]["hits"] = 5
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert any("cache.hits is 5" in f for f in result.failures)
    finally:
        ledger.close()


def test_a_group_whose_amount_does_not_match_the_price_table_fails_verification(tmp_path):
    receipt, ledger, _ = _cached_receipt(tmp_path)
    try:
        receipt["cache"]["by_origin"][0]["avoided_usd_per_hit"] = "0.005000"
        receipt["cache"]["saved_usd"] = "0.010000"
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert any("the price table says" in f for f in result.failures)
    finally:
        ledger.close()


def test_a_flipped_digit_in_a_group_proof_fails_verification(tmp_path):
    receipt, ledger, _ = _cached_receipt(tmp_path)
    try:
        path = receipt["cache"]["by_origin"][0]["merkle_path"]
        assert path, "the group should carry an inclusion proof in a small session"
        path[0]["hash"] = "0" * 64 if path[0]["hash"] != "0" * 64 else "1" * 64
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert any("cache-hit-proofs" in f for f in result.failures)
    finally:
        ledger.close()


def test_a_receipt_and_a_ledger_that_disagree_about_the_ceiling_fail_verification(tmp_path):
    receipt, ledger, _ = _cached_receipt(tmp_path)
    try:
        receipt["cache"]["hits_ceiling_reached"] = True
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert any("ceiling" in f for f in result.failures)
    finally:
        ledger.close()


def test_a_run_with_no_cache_skips_the_cache_checks_with_a_reason(tmp_path, make_receipt):
    receipt, _, _ = make_receipt()
    assert "cache" not in receipt
    result = verify_receipt(receipt)
    assert result.ok
    checks = {c.name: c for c in result.checks if c.name.startswith("cache")}
    assert checks["cache-savings"].status == "skip"
    assert "no response cache" in checks["cache-savings"].detail


def test_a_cache_member_that_is_not_an_object_fails_verification(tmp_path):
    receipt, ledger, _ = _cached_receipt(tmp_path)
    try:
        receipt["cache"] = "yes"
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert any("cache member is not an object" in f for f in result.failures)
    finally:
        ledger.close()


# --------------------------------------------------------------------------- #
# Mutation paydown: one test per survivor the sweep could not kill
# --------------------------------------------------------------------------- #
#
# `tools/mutate.py` seeded single-site mutations of `src/docket/cache.py` and ran the mapped tests
# against each one. Eleven survived — a change no test noticed — which is a statement about the
# tests, not about the mutants: every survivor below is either a behaviour this file did not pin or
# an equivalence to be argued. These tests close the ones that were gaps, each naming the mutant it
# was written to kill so that the next sweep can be read against this list.


def test_an_entry_is_a_frozen_value_without_an_instance_dictionary():
    """Kills `Entry`'s `frozen=True -> False` and `slots=True -> False`.

    An entry is handed out under the lock and used after it, and the cache holds hundreds of
    them; the docstring calls it "immutable, safe to hand out without the lock", which is a claim
    a mutation passing unnoticed would leave unsupported.
    """
    import dataclasses

    entry = _entry()
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.key = "something else"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.status = 500  # type: ignore[misc]
    assert not hasattr(entry, "__dict__"), "an entry is a value, not an object to decorate"


def test_a_configuration_is_refused_when_a_boolean_is_wearing_a_number_s_costume():
    """Kills the `or -> and` mutants in the two type guards of `ResponseCache.__init__`.

    `True` is an `int` in Python, so `isinstance(True, int) or not isinstance(...)` is the only
    reason a `ttl_seconds = true` from a hand-written TOML is refused rather than honoured as 1
    second. The same guard covers the two ceilings.
    """
    for kwargs in (
        {"ttl_seconds": True},
        {"ttl_seconds": "300"},
        {"ttl_seconds": float("inf")},
        {"ttl_seconds": float("-inf")},
        {"max_entries": True},
        {"max_entries": 1.5},
        {"max_hits": True},
        {"max_hits": 1.5},
    ):
        with pytest.raises(ValueError):
            ResponseCache(enabled=True, **kwargs)  # type: ignore[arg-type]


def test_an_entry_needs_an_origin_sequence_number_and_a_digest_that_is_a_digest():
    """Kills the `or -> and` mutant in `build_entry`'s digest guard, and its neighbours.

    A digest of `5` is not a digest and an empty string is not one either; a bool is not a
    sequence number, and neither is a negative one. Both are the ledger coordinates a hit will be
    checked against later, so a permissive check here becomes a receipt that cannot be verified.
    (The token counts are *not* validated here: they come from `proxy._token_field`, which is
    where a provider's negative or fractional count is turned into a disclosed problem — the
    sanitising is the boundary's job, and `tests/test_security.py` attacks it there.)
    """

    class Origin:
        def __init__(self, seq=7, digest="a" * 64):
            self.seq = seq
            self.hash = digest

    def build(origin):
        return build_entry(
            key="k",
            origin=origin,
            status=200,
            model="m",
            usage=(1, 0, 1),
            usage_source="reported",
            cost_usd="0.00",
            body=b"{}",
            headers=[],
            streaming=False,
            now=0.0,
        )

    for origin in (Origin(seq=True), Origin(seq=-1), Origin(seq="7"), Origin(seq=None)):
        with pytest.raises(ValueError, match="sequence number"):
            build(origin)
    for digest in (5, "", None, b"a" * 64, True):
        with pytest.raises(ValueError, match="response event it copies"):
            build(Origin(digest=digest))
    entry = build(Origin())
    assert (entry.origin_seq, entry.origin_hash) == (7, "a" * 64)
    assert (entry.input_tokens, entry.cached_input_tokens, entry.output_tokens) == (1, 0, 1)


def test_the_first_event_in_a_ledger_is_a_valid_origin_for_a_hit(tmp_path):
    """Kills `seq < 0 -> seq <= 0` and `seq < 0 -> seq < 1` in `build_entry`'s origin guard.

    A cache entry has to name the event it copies: that sequence number and hash are what make the
    entry checkable later. The ledger numbers its events from zero (`seq = len(events)`), so event 0
    is the first provider call of a run — the response a cache is most likely to be asked for twice.
    A guard demanding a positive sequence would refuse to cache it, and the saved amount that rests
    on it, so both the writer's guard and the reader's grouping are pinned here against the same
    real event.
    """
    ledger = Ledger.open(tmp_path / "runs" / "first.jsonl")
    try:
        first = ledger.append(ev.MODEL_RESPONSE, {"model": "claude-sonnet-5", "usage": {"input_tokens": 1000}})
        assert first.seq == 0, "the ledger's first event is 0, and this test is about that event"
        cache = ResponseCache(enabled=True, clock=FakeClock())
        assert cache.store(_entry(origin=first)) is True
        hit = cache.lookup("k1")
        assert hit is not None
        assert (hit.origin_seq, hit.origin_hash) == (0, first.hash)

        # The same event read back through the summary the receipt is built from.
        ledger.append(
            ev.MODEL_CACHE_HIT,
            {
                "key": "k1",
                "model": "claude-sonnet-5",
                "origin": {"seq": first.seq, "hash": first.hash},
                "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
                "usage_source": "reported",
                "avoided_cost_usd": "0.006000",
            },
        )
        block = summary(cache, ledger.events)
        assert block is not None
        assert block["hits"] == 1
        assert block["by_origin"][0]["origin_seq"] == 0
        assert block["by_origin"][0]["event_hash"] == ledger.events[-1].hash
        assert block["avoided_tokens"] == 1200
    finally:
        ledger.close()


def test_a_stored_entry_is_reported_as_stored():
    """Kills `return True -> return None` at the end of `ResponseCache.store`.

    Every caller in the tree treats this as a bool, and `assert cache.store(entry)` — the obvious
    way to write this test — passes for `None` too. So it is asserted against `True` itself, once
    for a store and once for the two refusals that must not report success.
    """
    cache = ResponseCache(enabled=True, clock=FakeClock())
    assert cache.store(_entry()) is True
    assert cache.store(_entry(status=500)) is False
    assert ResponseCache().store(_entry()) is False


def test_a_fresh_cache_has_counted_nothing():
    """Kills `self.avoided_tokens = 0 -> 1` in `__init__`.

    A counter that does not start at zero is a counter every later assertion can absorb: the
    metrics endpoint would report one token avoided by a run that never cached anything. All of
    them are asserted here as a set, so the next counter added to the class starts under the same
    claim.
    """
    cache = ResponseCache(enabled=True)
    snapshot = cache.snapshot()
    assert {name: snapshot[name] for name in ("hits", "misses", "stores", "evictions", "expirations")} == {
        "hits": 0,
        "misses": 0,
        "stores": 0,
        "evictions": 0,
        "expirations": 0,
    }
    assert snapshot["refusals_at_ceiling"] == 0
    assert snapshot["avoided_tokens"] == 0
    assert Decimal(str(snapshot["saved_usd"])) == 0
    assert snapshot["entries"] == 0


def test_an_expired_lookup_is_a_miss_as_well_as_an_expiry():
    """Kills `self.misses += 1 -> += 0` on the TTL-expiry path.

    The entry is dropped either way, so the lookup's answer does not change — but `misses` is what
    tells an operator whether a TTL is doing useful work or throwing away money, and a miss that
    expires an entry is still a miss the cache did not answer.
    """
    clock = FakeClock()
    cache = ResponseCache(enabled=True, ttl_seconds=10.0, clock=clock)
    assert cache.store(_entry(now=clock.value)) is True
    clock.advance(10.5)
    assert cache.lookup("k1") is None
    snapshot = cache.snapshot()
    assert snapshot["expirations"] == 1
    assert snapshot["misses"] == 1
    assert snapshot["hits"] == 0
    assert snapshot["entries"] == 0


def test_a_disabled_cache_summarises_to_nothing_unless_the_ledger_shows_a_hit():
    """Kills the `== -> !=` mutant in `summary`'s gate; pins the case the gate is there for.

    `summary` returns `None` for a run with no cache at all, and for a run whose policy configured
    a cache and switched it off — the distinction the docstring draws. Flipping the comparison
    makes *any* ordinary event count as a cache hit, so a run that merely mentioned a cache and
    disabled it would carry a `cache` member with no hits and no groups. The second assertion is
    the other half of the rule: when the ledger itself records `model.cache_hit`, a disabled cache
    object in the caller's hand must not be able to hide it — the events are the evidence, and the
    object is not.
    """
    enabled = ResponseCache(enabled=True)
    disabled = ResponseCache(enabled=False)
    ordinary = [_hit({"key": "k"}, event_type=ev.MODEL_RESPONSE)]
    assert summary(None, ordinary) is None
    assert summary(disabled, ordinary) is None, "a disabled cache must not claim a receipt member"
    assert summary(enabled, ordinary)["hits"] == 0, "an ordinary event is not a hit"

    hits = [_hit({"key": "k"})]
    assert summary(disabled, hits) is not None, "hits in the ledger outrank the caller's object"


def test_two_hits_citing_the_same_origin_sequence_with_different_digests_stay_apart():
    """Kills the `or -> and` mutant on the origin digest in `_groups`' key.

    The key exists so that two hits which disagree are never merged into one number. Two events can
    disagree about the digest of the sequence they cite — a ledger can be wrong, and a receipt built
    from it must show both claims rather than a sum that reconciles to nothing. Dropping the digest
    from the key merges them, which is exactly the quiet merge the docstring promises never happens.
    """
    enabled = ResponseCache(enabled=True)
    first = _hit({"origin": {"seq": 3, "hash": "a" * 64}, "usage": {"input_tokens": 5, "output_tokens": 7}})
    second = _hit({"origin": {"seq": 3, "hash": "b" * 64}, "usage": {"input_tokens": 5, "output_tokens": 7}})
    block = summary(enabled, [first, second])
    assert block is not None
    assert len(block["by_origin"]) == 2, block["by_origin"]
    assert {group["origin_hash"] for group in block["by_origin"]} == {"a" * 64, "b" * 64}
    assert all(group["hits"] == 1 for group in block["by_origin"])


def test_a_hit_whose_usage_omits_a_field_reports_zero_tokens_not_one():
    """Kills the `int(... or 0) -> int(... or 1)` mutant in `_groups`' usage triple.

    A hit event whose `usage` carries only `input_tokens` is a hit that reported nothing about
    output — and 0 is the only honest reading of a field that is not there. The number is quoted in
    the receipt and used by `docket verify` to recompute the saving, so an invented 1 would be an
    invented token, not a rounding detail.
    """
    enabled = ResponseCache(enabled=True)
    block = summary(enabled, [_hit({"usage": {"input_tokens": 5}})])
    assert block is not None
    usage = block["by_origin"][0]["usage"]
    assert usage == {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 0}
    assert block["by_origin"][0]["avoided_usd_per_hit"] == "0.01"


def test_an_empty_cache_is_still_a_cache_and_a_supplied_one_is_used(tunnelled, ledger, fake_upstream):
    """The bug `docket selftest` found: a caller's fresh cache was silently replaced.

    `ResponseCache` defines `__len__` so that `len(cache)` is the entry count — and that made an
    *empty* cache falsy. `RecordingProxy` decided with `cache or ResponseCache(...)`, so a caller
    who supplied a cache of their own got a different, policy-built one instead: disabled by
    default, and therefore doing nothing at all. Unit tests passed a cache through the *policy*,
    which is why none of them noticed; the self-test's probe passes one as an argument, as a host
    application would.

    Both halves are pinned here: the object is truthy whatever it contains, and the boundary uses
    the cache it was handed rather than deciding by truthiness.
    """
    fresh = ResponseCache(enabled=True, ttl_seconds=300)
    assert len(fresh) == 0
    assert bool(fresh) is True, "an empty cache must not read as 'no cache'"

    tunnel = tunnelled(fake_upstream.url, _cache_policy(), cache=ResponseCache(enabled=True, ttl_seconds=300))
    assert tunnel.proxy is not None
    assert tunnel.proxy.cache.enabled is True, "the supplied cache is the one in use"
    assert isinstance(tunnel.proxy.cache, ResponseCache)

    first = tunnel.post(BODY)
    second = tunnel.post(BODY)
    assert first == second
    hits = [e for e in ledger if e.type == ev.MODEL_CACHE_HIT]
    assert len(hits) == 1, f"the second identical call should have been a hit, got {len(hits)}"
    assert len(fake_upstream.requests) == 1, "the provider must have been asked once"


# --------------------------------------------------------------------------- #
# Mutation paydown: the verifier's own survivors
# --------------------------------------------------------------------------- #
#
# The sweep that scored `src/docket/cache.py` was run over `src/docket/verify.py` too, whose
# `cache-savings`, `cache-hit-origins` and `cache-hit-proofs` checks recompute what a receipt claims
# to have saved. Twenty-nine of its 177 mutants survived a wider test subset; the tests below were
# written one survivor at a time, each naming the mutant it kills so the next sweep can be read
# against the list. The re-sweep of 2026-10-16 left five, and four of those were assertions that
# stopped one token short of the mutant they were written for: one ended before the type suffix
# (554), one sat on a fixture whose p50 and p99 are the same number (568), one built a problem list
# with the wrong length for the mutant to be visible in (761), and one never asked for the sentence
# the mutant suppresses (867). Those four are fixed in place below and marked where the fix is not
# obvious. One survivor remains — `verify.py:186` — and it is argued equivalent in
# `docs/testing.md`, not pinned here, because no input can tell the two spellings apart. Every input
# in this section is a *receipt* — the document a third party is handed — with one field changed or
# one event written differently, because that is the only thing a verifier is ever given.


def _naming(result, fragment: str) -> list[str]:
    """The failure sentences that contain `fragment` — empty means the sentence never appeared."""
    return [failure for failure in result.failures if fragment in failure]


def _detail_of(result, name: str) -> tuple[str, str]:
    """`(status, detail)` of the one check called `name`; a guard against a renamed check passing."""
    matches = [(check.status, check.detail) for check in result.checks if check.name == name]
    assert len(matches) == 1, f"{name} appears {len(matches)} times: {matches}"
    return matches[0]


def test_a_one_hit_receipt_verifies_and_the_verifier_names_its_own_counts(tmp_path):
    """Kills six mutants: `proven = 0 -> 1`, `proven += 1 -> += 0`, `hits < 1 -> hits <= 1`, the
    `measured and …` that becomes `or`, and both `REPORTED[-1] -> REPORTED[0]` in the latency line.

    A receipt whose cache answered **one** call is the shape where those numbers are interchangeable
    with their neighbours: one proof of one group, one measured call. So the four sentences a reader
    actually sees are asserted word for word — they are the only place several of those numbers
    appear at all, and a verifier that names `p50` twice, counts two proofs for one group, or refuses
    a group that claims its single hit is telling the reader something that is not so.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1, extra_latency=40)
    try:
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert result.ok, result.failures
        assert _detail_of(result, "latency-summary") == (
            "pass",
            "p50 12 ms · p99 40 ms over 2 measured call(s), nearest-rank",
        )
        assert _detail_of(result, "cache-savings") == (
            "pass",
            "1 hit(s) recomputed at the origin's price: 0.006000 not spent",
        )
        assert _detail_of(result, "cache-hit-origins") == (
            "pass",
            "every hit resolves to the stored response it replays",
        )
        assert _detail_of(result, "cache-hit-proofs") == (
            "pass",
            "1 cache group(s) proven against the receipt's claimed Merkle root",
        )
    finally:
        ledger.close()


def test_a_group_that_claims_a_hit_the_ledger_does_not_record_is_refused(tmp_path):
    """Kills four mutants in the tally loop: `tally.get(identity, 0) -> or 1` (both occurrences), the
    same `0 -> 1` inside the sentence, and `not isinstance(group, dict) -> isinstance(...)`, which
    would skip every real group.

    The ledger's hit events are grouped by identity — origin sequence and hash, key, model, the three
    token counts and the price — and each receipt group has to match one of those tallies. Change the
    key a group claims and its identity is one the ledger never recorded: the receipt must fail, and
    the sentence must say the ledger records *none*, not that it recorded one.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        receipt["cache"]["by_origin"][0]["key"] = "a-key-the-ledger-never-recorded"
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert _naming(result, "the ledger records 0"), result.failures
    finally:
        ledger.close()


def test_a_group_that_claims_no_hits_is_refused(tmp_path):
    """Kills `hits is None or hits < 1 -> and`, `hits < 1 -> hits < 0`, and the tally sentence's
    `int(group.get("hits") or 0) -> or 1`.

    A group with `hits: 0` is not a saving; it is a field nobody filled in, and it must be refused by
    name. Both of the guard mutants step past it instead — one by asking whether `None` is less than
    one, the other by asking whether zero is negative — so the group is counted as a saving of
    nothing and the sentence a reader needs never appears.

    The third mutant is in the *tally* loop, which is a separate pass over the same groups and does
    not skip a group the guard refused: the group's identity still matches the ledger's one recorded
    hit, so the sentence "the ledger records 1" is the second disagreement about the same group. With
    `or 1` the falsy `hits` becomes the ledger's own count, the comparison agrees, and that sentence
    is never written — one refusal where there were two.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        group = receipt["cache"]["by_origin"][0]
        group["hits"] = 0
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert _naming(result, "does not say how many hits it counts: 0"), result.failures
        assert _naming(result, f"claims 0 hit(s) replaying seq {group['origin_seq']}; the ledger records 1"), (
            result.failures
        )
    finally:
        ledger.close()


def test_a_group_that_carries_no_cache_key_is_refused(tmp_path):
    """Kills `not isinstance(key, str) or not key -> and`, which would accept a key of `""` (or `5`).

    The key is what a later hit is matched on, so a group without one is a claim with nothing behind
    it. The two halves of that guard cover two different ways of having no key — the wrong type and
    the empty string — and the mutant drops both at once.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        receipt["cache"]["by_origin"][0]["key"] = ""
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert _naming(result, "carries no cache key"), result.failures
    finally:
        ledger.close()


def test_a_group_whose_usage_omits_a_token_count_contributes_zero_not_one(tmp_path):
    """Kills both `int(usage.get(field) or 0) -> or 1` in the avoided-token sum.

    `or 0` is the difference between "this group reports zero tokens" and "this group reports one
    token": with the mutant, a usage object that omits a field inflates the recomputed total by one
    per hit, and a receipt that is *right* about its own arithmetic is accused of disagreeing with
    it. Both fields are driven here, because the two mutants are one field each.
    """
    for field, expected in (("input_tokens", 200), ("output_tokens", 1000)):
        case = tmp_path / field  # a directory per case: `_cached_receipt` appends to one ledger path
        case.mkdir()
        receipt, ledger, _ = _cached_receipt(case, hits=1)
        try:
            receipt["cache"]["by_origin"][0]["usage"][field] = 0
            result = verify_receipt(receipt, ledger_events=ledger.events)
            assert not result.ok
            assert _naming(result, f"the groups add up to {expected}"), result.failures
        finally:
            ledger.close()


def test_a_group_without_an_inclusion_proof_makes_the_proof_check_skip(tmp_path):
    """Kills `without_proof += 1 -> += 0` in the proof loop.

    A session large enough to omit proofs is *summarised* rather than accused: the check skips with a
    reason, which is the difference between "not checked" and "checked and fine". With the mutant the
    count stays zero, so the branch that assumes every group carries a proof runs instead and reports
    a pass over nothing.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        del receipt["cache"]["by_origin"][0]["merkle_path"]
        result = verify_receipt(receipt, ledger_events=ledger.events)
        status, detail = _detail_of(result, "cache-hit-proofs")
        assert status == "skip", (status, detail)
        assert "without inclusion proofs" in detail, detail
    finally:
        ledger.close()


def test_a_malformed_proof_is_counted_as_a_group_that_does_not_prove(tmp_path):
    """Kills `proof_failures += 1 -> += 0` in the `except ValueError` arm.

    A proof that cannot be read is a proof that did not prove: the count is what the reader sees, and
    with the mutant the receipt reports a pass over zero groups instead of naming the group whose
    path was unreadable.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        receipt["cache"]["by_origin"][0]["merkle_path"] = "not a path at all"
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert _naming(result, "do not prove against the receipt's Merkle root"), result.failures
    finally:
        ledger.close()


def test_three_cache_savings_problems_do_not_report_a_fourth_line(tmp_path):
    """Kills `len(savings_problems) > 3 -> >=` in the "further problems" summary.

    Each family reports its first three problems and then one line counting what is left. At exactly
    three that line must not appear — "further cache savings problems: 0 more" contradicts itself —
    so the declared hits, amount and token total are all moved off the groups at once.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        receipt["cache"].update({"hits": 9, "saved_usd": "9.99", "avoided_tokens": 99999})
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert not [failure for failure in result.failures if "further cache savings" in failure]
    finally:
        ledger.close()


def test_three_origin_problems_do_not_report_a_fourth_line(tmp_path):
    """Kills `len(origin_problems) > 3 -> >=` in the same summary, for the origins family.

    The stop condition is a boundary, and the two spellings of it differ at exactly one length: three
    disagreements in the list print the same three sentences either way, and a fourth — "further
    origin problems: 0 more" — appears only under the mutant. So the receipt has to produce **three**
    entries in that list, and this one produces them from three separate fields: a group whose model
    is not the one the call returned, a group whose token count does not match the call it cites, and
    the appendix key that both takes the call out of the cached-response record and, because the key
    is part of the group's identity, stops the tally below from containing the group. That tally loop
    is a second pass over the same groups and adds its own sentence directly, without going through
    the list this boundary counts — which is why the assertion is four sentences, not three.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        group = receipt["cache"]["by_origin"][0]
        group["model"] = "a-model-that-never-answered"
        group["usage"]["input_tokens"] = 1
        # The third problem has to come from this loop: the tally loop below adds its own sentence
        # directly, without going through the list the summary counts.
        receipt["appendix"]["usage"][0]["cache_key"] = "another-key"
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert len(_naming(result, "cache-hit-origins:")) == 4, result.failures
        assert not [failure for failure in result.failures if "further origin problems" in failure]
    finally:
        ledger.close()


def test_three_event_problems_do_not_report_a_fourth_line(tmp_path):
    """Kills `len(event_problems) > 3 -> >=` in the event half of the same summary.

    The event half compares each hit in the *ledger* with the response it replays. Three hits whose
    cited origin hash is wrong are three problems, so the line that counts what is left of them has
    nothing to add.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=3)
    try:
        hit_events = [event for event in ledger.events if event.type == ev.MODEL_CACHE_HIT]
        assert len(hit_events) == 3, "the fixture has to produce exactly three hits to test this"
        for event in hit_events:
            event.data["origin"]["hash"] = "0" * 64
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert not [failure for failure in result.failures if "further origin problems" in failure]
    finally:
        ledger.close()


def test_a_receipt_that_claims_the_ceiling_agrees_with_a_ledger_that_records_it(tmp_path):
    """Kills `type == "note" -> !=` and `data.get("event") == "cache.hits_ceiling_reached" -> !=`.

    The ceiling note is a ledger fact: a receipt may claim the ceiling was reached only when the note
    is there, and a receipt that does not claim it must not be summarised as a run that did. Both
    mutants invert one half of that recognition, so a receipt that is *right* about the ceiling is
    accused of disagreeing with its own ledger.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1, ceiling=True)
    try:
        assert receipt["cache"]["hits_ceiling_reached"] is True, "the fixture must carry the claim"
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert result.ok, result.failures
    finally:
        ledger.close()


def test_a_hit_citing_the_first_event_says_that_event_is_not_a_response(tmp_path):
    """Kills `not 0 <= seq < len(events) -> not 1 <= seq < len(events)` in the event check.

    The ledger numbers its events from zero, so event zero is a real position — the check's lower
    bound is the byte zero of the sequence space, the same off-by-one the harness's span guard had.
    With the mutant, a hit citing seq 0 is told the event does not exist, which is a different
    sentence about a different problem, and it hides what is actually wrong: the first event of a run
    is not a response.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        hit = next(event for event in ledger.events if event.type == ev.MODEL_CACHE_HIT)
        hit.data["origin"]["seq"] = 0
        hit.data["origin"]["hash"] = ledger.events[0].hash
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert _naming(result, "which is a session.start event, not a response"), result.failures
    finally:
        ledger.close()


def test_a_hit_whose_origin_sequence_is_a_boolean_is_refused(tmp_path):
    """Kills `not isinstance(seq, int) or isinstance(seq, bool) -> and` in the event check.

    `True` is an `int` in Python, and `events[True]` is `events[1]` — so the second half of that
    guard is the only thing keeping a boolean from indexing the ledger. The mutant reads a hit that
    cites `true` as one that cites seq 1, and reports a problem with a different event than the one
    the receipt named.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        hit = next(event for event in ledger.events if event.type == ev.MODEL_CACHE_HIT)
        hit.data["origin"]["seq"] = True
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert _naming(result, "cites origin seq True, which is not an event in this ledger"), result.failures
    finally:
        ledger.close()


def test_the_latency_check_names_the_first_malformed_row(tmp_path):
    """Kills `malformed[0] -> malformed[1]` in the sentence that reports a bad latency.

    The sentence quotes the row it found. With two bad rows the mutant quotes the second — the reader
    is pointed at the wrong line of a receipt they are about to argue about — and with one bad row it
    raises `IndexError`, so the malformed receipt produces a traceback instead of a finding.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=2)
    try:
        rows = receipt["appendix"]["usage"]
        first, second = deepcopy(rows[0]), deepcopy(rows[0])
        first["seq"], first["latency_ms"] = 3, "seven"
        # The second value is a different *type* on purpose: `type(malformed[0]).__name__` is the
        # other half of the sentence, and with two strings it would read the same either way.
        second["seq"], second["latency_ms"] = 4, None
        rows[:] = [first, second]
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        # The whole sentence, type suffix included: `malformed[0]` appears twice on that line, and
        # an assertion that stops before `(str)` leaves the second half unpinned — mutating the
        # *type* lookup then reports the second row's type while still naming the first row's value.
        assert _naming(result, "a latency that is not a whole, non-negative millisecond count: 'seven' (str)"), (
            result.failures
        )
    finally:
        ledger.close()


def test_a_usage_row_without_a_latency_is_not_counted_as_measured(tmp_path):
    """Kills `isinstance(row, dict) and is_latency(...) -> or` in the list of measured rows.

    "Measured" is the number the summary is compared against, so a row that carries no measurement
    may not be counted as one. With the mutant a row without `latency_ms` enters the list, and the
    comparison is then made against a value that is not a millisecond count at all.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        rows = receipt["appendix"]["usage"]
        extra = deepcopy(rows[0])
        extra["seq"] = 4
        del extra["latency_ms"]
        rows.append(extra)
        receipt["latency"]["total_rows"] = 2
        result = verify_receipt(receipt, ledger_events=ledger.events)
        status, detail = _detail_of(result, "latency-summary")
        assert status == "pass", (status, detail)
        assert detail == "p50 12 ms · p99 12 ms over 1 measured call(s), nearest-rank", detail
    finally:
        ledger.close()


def test_an_appendix_row_whose_sequence_is_a_boolean_is_not_an_origin(tmp_path):
    """Kills `isinstance(seq, int) and not isinstance(seq, bool) -> or` in the appendix index.

    The index exists so that a group can name the metered call it replays. `True == 1` in Python, so
    with the mutant an appendix row whose `seq` is `true` answers for seq 1: a group citing a call
    that was never metered is then checked against a row it does not belong to, and the sentence says
    the call is there when it is not.
    """
    receipt, ledger, _ = _cached_receipt(tmp_path, hits=1)
    try:
        receipt["appendix"]["usage"][0]["seq"] = True
        receipt["cache"]["by_origin"][0]["origin_seq"] = 1
        result = verify_receipt(receipt, ledger_events=ledger.events)
        assert not result.ok
        assert _naming(result, "cites origin seq 1, which is not a metered call"), result.failures
    finally:
        ledger.close()
