"""Deterministic analysis of a session: where the money and the risk actually went.

Every finding is arithmetic over ledger events, not a model's opinion. Each one
carries the events it was derived from (``evidence``), states its rule, and is
marked ``deterministic`` or ``heuristic`` so a reader knows how much to trust it.
That distinction is the whole point: an audit trail that blends hard facts with
guesses is worse than no audit trail, because it launders the guesses.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Iterable

from . import events as ev
from .policy import Policy
from .pricing import PriceTable
from .util import human_int, human_usd, mb

FAT_CONTEXT_TOKENS = 100_000
FAT_PAYLOAD_BYTES = 32 * 1024
LOOP_THRESHOLD = 6
RETRY_STORM_THRESHOLD = 3
PREMIUM_INPUT_PRICE = Decimal("5.00")


def _finding(
    rule: str,
    severity: str,
    confidence: str,
    title: str,
    detail: str,
    *,
    usd_impact: Decimal | None = None,
    evidence: Iterable[int] = (),
    remedy: str = "",
) -> dict[str, Any]:
    return {
        "rule": rule,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "detail": detail,
        "usd_impact": human_usd(usd_impact) if usd_impact is not None else None,
        "evidence": list(evidence)[:40],
        "remedy": remedy,
    }


def analyse(
    events: Iterable[Any],
    *,
    price_table: PriceTable,
    policy: Policy | None = None,
    max_findings: int = 12,
) -> list[dict[str, Any]]:
    """Return findings ordered by estimated dollar impact, then severity."""
    events = list(events)
    responses = [e for e in events if e.type == ev.MODEL_RESPONSE]
    requests = [e for e in events if e.type == ev.MODEL_REQUEST]
    findings: list[dict[str, Any]] = []

    def cost_of(event: Any) -> Decimal:
        try:
            return Decimal(str(event.data.get("cost_usd", "0")))
        except Exception:  # pragma: no cover - defensive
            return Decimal("0")

    total_cost = sum((cost_of(e) for e in responses), Decimal("0"))

    # -- 1. Runaway loop: identical requests repeated --------------------- #
    by_fingerprint: dict[str, list[Any]] = defaultdict(list)
    for req in requests:
        by_fingerprint[str(req.data.get("fingerprint", "?"))].append(req)
    for fingerprint, group in sorted(by_fingerprint.items(), key=lambda kv: -len(kv[1])):
        if len(group) < LOOP_THRESHOLD:
            continue
        seqs = {g.seq for g in group}
        related = [r for r in responses if r.seq - 1 in seqs or r.seq - 2 in seqs]
        burned = sum((cost_of(r) for r in related), Decimal("0"))
        first, last = group[0], group[-1]
        detail = (
            f"{len(group)} byte-identical requests (same prompt, same tools) were sent between "
            f"{first.ts} and {last.ts}. Identical requests are the signature of a loop that will not "
            f"terminate on its own; the model sees no new information, so nothing changes."
        )
        if related:
            detail += f" Estimated cost of the repeated group: {human_usd(burned)}."
        findings.append(
            _finding(
                "runaway-loop",
                "critical",
                "deterministic",
                f"Runaway loop: {len(group)} identical model calls",
                detail,
                usd_impact=burned if burned else None,
                evidence=[g.seq for g in group],
                remedy=(
                    "A cap would have stopped this: see the 'counterfactual' finding below, then set "
                    "caps.max_usd / caps.max_model_calls in your policy."
                ),
            )
        )
        break

    # -- 2. Counterfactual: what a cap would have saved ------------------- #
    if policy is not None and policy.caps.max_usd is not None:
        cap = policy.caps.max_usd
        running = Decimal("0")
        crossed_at: Any | None = None
        spent_after = Decimal("0")
        for response in responses:
            running += cost_of(response)
            if crossed_at is None and running >= cap:
                crossed_at = response
            elif crossed_at is not None:
                spent_after += cost_of(response)
        if crossed_at is not None and spent_after > 0:
            findings.append(
                _finding(
                    "counterfactual",
                    "high",
                    "deterministic",
                    f"A {human_usd(cap)} cap would have stopped this run at call #{crossed_at.seq}",
                    (
                        f"Cumulative estimated cost crossed {human_usd(cap)} at {crossed_at.ts}; "
                        f"{human_usd(spent_after)} was spent after that point. With this policy active "
                        f"as a hard cap, the run would have been terminated there."
                    ),
                    usd_impact=spent_after,
                    evidence=[crossed_at.seq],
                    remedy="Run with `docket run --max-usd` to enforce the cap instead of reporting it.",
                )
            )

    # -- 3. Retry storm ---------------------------------------------------- #
    window: list[Any] = []
    for event in events:
        if event.type == ev.MODEL_ERROR:
            window.append(event)
            continue
        if event.type == ev.MODEL_RESPONSE and window:
            if len(window) >= RETRY_STORM_THRESHOLD:
                findings.append(
                    _finding(
                        "retry-storm",
                        "medium",
                        "deterministic",
                        f"{len(window)} failed calls before a successful one",
                        (
                            "Each retry re-sends the accumulated context, so cost grows faster than "
                            "progress. Failures that repeat are a signal that a human or a different "
                            "strategy is needed."
                        ),
                        evidence=[w.seq for w in window],
                        remedy="Bound retries in the agent, or set caps.max_model_calls as a backstop.",
                    )
                )
            window = []

    # -- 4. Context amplification / missing cache ------------------------- #
    repeats = {fp: len(g) for fp, g in by_fingerprint.items() if len(g) > 1}
    if repeats:
        repeated_responses = [
            r
            for r in responses
            if str(r.data.get("usage", {}).get("cached_input_tokens") or 0) == "0"
        ]
        repeated_input_tokens = sum(int(r.data.get("usage", {}).get("input_tokens") or 0) for r in repeated_responses)
        if repeated_input_tokens > 0 and len(repeated_responses) >= 3:
            price, assumed = price_table.match(str(repeated_responses[0].data.get("model", "")))
            delta_per_million = max(price.input - price.cached_input, Decimal("0"))
            potential = (Decimal(repeated_input_tokens) * delta_per_million / Decimal(1_000_000)).quantize(Decimal("0.000001"))
            findings.append(
                _finding(
                    "cache-miss",
                    "medium",
                    "heuristic",
                    "Repeated context appears to be uncached",
                    (
                        f"{len(repeated_responses)} calls reported 0 cached input tokens across "
                        f"{human_int(repeated_input_tokens)} input tokens, while request fingerprints repeat. "
                        f"If prompt caching were in effect at this model's list price, the same traffic could "
                        f"cost about {human_usd(potential)} less. Providers cache only on prefixes they "
                        f"consider stable, so this is an estimate, not a defect."
                    ),
                    usd_impact=potential,
                    evidence=[r.seq for r in repeated_responses[:20]],
                    remedy=(
                        "Keep the stable part of the prompt at the front, avoid interleaving timestamps or "
                        "ids, and check the provider's cache-write/read counters."
                    )
                    + ("" if not assumed else " (Price assumed: model not in table.)"),
                )
            )

    # -- 5. Fat context ---------------------------------------------------- #
    fat = [r for r in responses if int(r.data.get("usage", {}).get("input_tokens") or 0) >= FAT_CONTEXT_TOKENS]
    if fat:
        worst = max(fat, key=cost_of)
        burned = sum((cost_of(r) for r in fat), Decimal("0"))
        findings.append(
            _finding(
                "fat-context",
                "medium",
                "deterministic",
                f"{len(fat)} call{'' if len(fat) == 1 else 's'} carried {human_int(FAT_CONTEXT_TOKENS)}+ input tokens",
                (
                    f"Input tokens are billed on every call, so a large context is a recurring charge. "
                    f"The largest single call carried "
                    f"{human_int(int(worst.data.get('usage', {}).get('input_tokens') or 0))} tokens "
                    f"for {human_usd(cost_of(worst))}. Calls of this size totalled {human_usd(burned)}."
                ),
                usd_impact=burned,
                evidence=[r.seq for r in fat[:20]],
                remedy="Summarise or truncate tool output before it re-enters the prompt; compact deliberately.",
            )
        )

    # -- 6. Premium model on trivial calls --------------------------------- #
    premium = []
    for response in responses:
        model = str(response.data.get("model", ""))
        price, assumed = price_table.match(model)
        usage = response.data.get("usage", {})
        if (
            not assumed
            and price.input >= PREMIUM_INPUT_PRICE
            and int(usage.get("input_tokens") or 0) < 2000
            and int(usage.get("output_tokens") or 0) < 500
        ):
            premium.append(response)
    if premium:
        burned = sum((cost_of(r) for r in premium), Decimal("0"))
        findings.append(
            _finding(
                "premium-for-small-calls",
                "low",
                "heuristic",
                f"{len(premium)} small call{'' if len(premium) == 1 else 's'} went to a premium model",
                (
                    f"Calls with less than 2,000 input tokens on a model priced at or above "
                    f"$5 per million input tokens cost {human_usd(burned)} here. Routing only these "
                    f"calls to a cheaper tier typically costs a fraction of that, but whether quality "
                    f"survives the switch is a judgement this tool does not make for you."
                ),
                usd_impact=burned,
                evidence=[r.seq for r in premium[:20]],
                remedy="Set models.allow in the policy to make expensive models opt-in per task.",
            )
        )

    # -- 7. Policy denials and violations ---------------------------------- #
    denied = [e for e in events if e.type == ev.MODEL_DENIED]
    if denied:
        findings.append(
            _finding(
                "policy-denials",
                "medium",
                "deterministic",
                f"{len(denied)} model call{'' if len(denied) == 1 else 's'} blocked by policy",
                "Blocked calls are free, which is the entire point of enforcing at the boundary.",
                evidence=[e.seq for e in denied[:20]],
                remedy="Check whether the agent needs a different model or a wider allow-list.",
            )
        )
    violations = [e for e in events if e.type == ev.POLICY_VIOLATION]
    if violations:
        paths = ", ".join(str(v.data.get("path")) for v in violations[:5])
        findings.append(
            _finding(
                "write-scope",
                "high",
                "deterministic",
                f"{len(violations)} file(s) inside a deny_write scope were changed",
                (
                    f"Changed: {paths}. Docket proves this happened; it cannot veto another process's "
                    f"write syscalls from user space, so this is evidence rather than prevention."
                ),
                evidence=[v.seq for v in violations[:20]],
                remedy="Give the agent a narrower working directory, or run it in a sandbox/container.",
            )
        )

    # -- 8. Concentration -------------------------------------------------- #
    if len(responses) >= 5 and total_cost > 0:
        top = sorted(responses, key=cost_of, reverse=True)[:3]
        share = sum((cost_of(r) for r in top), Decimal("0")) / total_cost * 100
        if share >= Decimal("50"):
            findings.append(
                _finding(
                    "concentration",
                    "low",
                    "deterministic",
                    f"3 of {len(responses)} calls were {share:.0f}% of the cost",
                    (
                        "Spend concentrated in a handful of calls is easy to fix and easy to miss: "
                        f"the top call was {human_usd(cost_of(top[0]))} at {top[0].ts}."
                    ),
                    usd_impact=sum((cost_of(r) for r in top), Decimal("0")),
                    evidence=[r.seq for r in top],
                    remedy="Look at the largest calls first; they dominate the bill.",
                )
            )

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(
        key=lambda f: (
            order.get(str(f["severity"]), 9),
            -float(str(f["usd_impact"]).replace("$", "").replace(",", "") or 0)
            if f["usd_impact"]
            else 0,
        )
    )
    return findings[:max_findings]


def total_estimated_savings(findings: list[dict[str, Any]]) -> str:
    """Sum of the dollar impacts Docket attributes to concrete findings."""
    total = Decimal("0")
    for finding in findings:
        value = finding.get("usd_impact")
        if isinstance(value, str) and value.startswith("$"):
            try:
                total += Decimal(value.replace("$", "").replace(",", ""))
            except Exception:  # pragma: no cover - defensive
                continue
    return human_usd(total)


def oversized_payloads(events: Iterable[Any], threshold_bytes: int = FAT_PAYLOAD_BYTES) -> list[tuple[int, int]]:
    """``(seq, bytes)`` for model requests whose body exceeded ``threshold_bytes``.

    Kept separate from :func:`analyse` because it is used by the markdown report
    rather than by the findings list.
    """
    out = []
    for event in events:
        if event.type == ev.MODEL_REQUEST and int(event.data.get("body_bytes") or 0) >= threshold_bytes:
            out.append((event.seq, int(event.data.get("body_bytes") or 0)))
    return out
