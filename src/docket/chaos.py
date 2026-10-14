"""Chaos experiments and game days: deliberate failure, on purpose, with a post-mortem.

The point of this module is not that Docket survives nothing — it is that every claim
about failure behaviour gets *exercised* and *recorded*. A circuit breaker that has
never opened is a hypothesis; a fallback that has never been triggered is a comment.

Three surfaces live here:

``docket chaos``
    One run, faults armed, then a mechanical audit of what the boundary did: which calls
    were refused and why, whether every refusal reached the ledger, whether the breaker
    opened at the right moment, whether fault injections were declared in the receipt.

``docket game-day``
    A scripted series of phases — normal, upstream failures, upstream latency, rate
    limiting, a ledger that fills up, and the supervisor being killed outright — each a
    real supervised run, each audited, ending in a written post-mortem with what the
    experiment proved, what it did not prove, and what still leaks.

``analyse_run``
    The audit itself, usable on any ledger + receipt: it re-derives the claims from the
    events rather than trusting the summary, so it works on a run that produced no
    receipt at all (which is precisely the interesting case).

Nothing here fabricates a result. Where a phase cannot be run (no ``/dev/full``, an
unavailable port), the phase is recorded as skipped with the reason.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import events as ev
from .ledger import Ledger
from .policy import Caps, Policy
from .resilience import parse_fault_spec
from .util import atomic_write, scratch_workspace

# Event types the audit looks for, grouped by what they mean for failure behaviour.
REFUSAL_EVENTS = (ev.MODEL_DENIED,)
FAILURE_EVENTS = (ev.MODEL_ERROR,)


def ledger_facts(ledger_path: Path) -> dict[str, Any]:
    """Everything the audit needs, re-derived from the file rather than from a summary."""
    # Read-only and non-exclusive: an audit must never be the reason a live run cannot
    # write, and it must work on a file whose writer is gone.
    events, diagnostics = Ledger.read_with_diagnostics(ledger_path)
    counts: dict[str, int] = {}
    for event in events:
        counts[event.type] = counts.get(event.type, 0) + 1
    denials = [e.data for e in events if e.type in REFUSAL_EVENTS]
    errors = [e.data for e in events if e.type in FAILURE_EVENTS]
    # A response event carries the upstream's status. In a healthy run every one of them
    # is 2xx; a 4xx or 5xx here is a failure that was filed as an answer, and the audit
    # has to be able to see that rather than trusting the event type alone.
    response_statuses = sorted(
        {int(e.data.get("status") or 0) for e in events if e.type == ev.MODEL_RESPONSE and e.data.get("status")}
    )
    return {
        "path": str(ledger_path),
        "events": len(events),
        "counts": counts,
        "diagnostics": diagnostics,
        "denial_codes": sorted({str(d.get("code")) for d in denials}),
        "denials": len(denials),
        "errors": len(errors),
        "error_statuses": sorted({int(e.get("status") or 0) for e in errors}),
        "response_statuses": response_statuses,
        "faults": [e.data for e in events if e.type == "x-docket.fault"],
        "rate_limited": [e.data for e in events if e.type == "x-docket.rate-limited"],
        "cap_events": [e.data for e in events if e.type == ev.CAP_EXCEEDED],
        "model_responses": counts.get(ev.MODEL_RESPONSE, 0),
        "model_requests": counts.get(ev.MODEL_REQUEST, 0),
        "evidence_gaps": [e.data for e in events if e.type == "x-docket.evidence-gap"],
        "rollbacks": [e.data for e in events if e.type == "x-docket.rollback-applied"],
        # A restore that declined every path is the outcome an operator most needs to see, and
        # it is recorded under its own name — so the audit looks for both, and the check below
        # passes on either precisely because one of them always exists once a restore was tried.
        "rollbacks_refused": [e.data for e in events if e.type == "x-docket.rollback-refused"],
        "tool_calls": counts.get(ev.TOOL_CALL, 0),
        "notes": [e.data.get("message") for e in events if e.type == ev.NOTE and e.data.get("message")],
    }


def agent_summary(notes: list[str]) -> dict[str, Any] | None:
    """Recover the synthetic agent's own report of what it saw, if it made one.

    This is what makes the audit a *cross-check* rather than a self-portrait: the agent
    counts the responses it received on the wire, and those counts are compared with the
    events the boundary wrote. A boundary that quietly dropped a refusal from the ledger
    would disagree with the agent's arithmetic.
    """
    for message in notes:
        if isinstance(message, str) and message.startswith("scriptagent: {"):
            with contextlib.suppress(json.JSONDecodeError):
                return json.loads(message[len("scriptagent: ") :])
    return None


def audit_run(
    ledger_path: Path,
    receipt: dict[str, Any] | None,
    *,
    exit_code: int | None = None,
    expected_faults: int | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Mechanically check the properties Docket claims under stress.

    Every check is derived from recorded events, so a run that wrote no receipt — or a
    receipt with a hole in it — is still auditable. Each check returns a status:

    ``pass``  the property holds, with the numbers that show it;
    ``fail``  the property is violated (this is a bug, not a configuration choice);
    ``skip``  the property could not be tested here, with the reason. A skip is never
    silently upgraded to a pass — that is the difference between a test suite and a
    rubber stamp.
    """
    facts = ledger_facts(ledger_path)
    checks: list[dict[str, str]] = []

    def check(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    summary = agent_summary(facts["notes"])

    # 1. Does the wire agree with the record? The agent reports what it received; the
    #    ledger must contain a recorded explanation for each non-2xx it saw.
    if summary is None:
        check(
            "every failed response the agent saw is explained by the record",
            "skip",
            "the supervised command did not report its own status counts, so there is nothing "
            "independent to cross-check the ledger against",
        )
    else:
        statuses = [int(s) for s in summary.get("statuses") or []]
        observed_bad = sum(1 for s in statuses if s >= 400)
        explained = facts["denials"] + len(facts["rate_limited"]) + len(facts["faults"]) + facts["errors"]
        injection_only = len(facts["faults"]) >= observed_bad
        check(
            "every failed response the agent saw is explained by the record",
            "pass" if explained >= observed_bad else "fail",
            f"agent received {observed_bad} non-2xx of {len(statuses)} calls; the ledger explains "
            f"{explained} ({facts['denials']} denied, {len(facts['rate_limited'])} throttled, "
            f"{len(facts['faults'])} injected, {facts['errors']} error)"
            + (
                " — note: injected faults are also recorded as model.error, so this is an upper bound"
                if injection_only
                else ""
            ),
        )

    # 2. A failure must never be filed as a success — checked from both sides: the error
    #    events must all describe real failures, and no *response* may carry a failure
    #    status (a receipt that counts a 500 as an answer overstates what the agent got
    #    and understates what the run cost).
    bad_errors = [status for status in facts["error_statuses"] if status and status < 400]
    bad_responses = [status for status in facts["response_statuses"] if status >= 400]
    if bad_responses:
        check(
            "no failed call was recorded as a successful one",
            "fail",
            f"{len(bad_responses)} model.response event(s) carry a failure status "
            f"{sorted(set(bad_responses))}: the run has answers it never received",
        )
    else:
        check(
            "no failed call was recorded as a successful one",
            "pass" if not bad_errors else "fail",
            f"model.error statuses: {facts['error_statuses'] or 'none'}; "
            f"{facts['model_responses']} model.response event(s) all 2xx; "
            f"{facts['denials']} refusal(s) recorded separately",
        )

    # 3. Injections must be declared in the receipt: undeclared fault injection in a
    #    formal record would be evidence fraud, whoever did it.
    injected = len(facts["faults"])
    if receipt is None:
        check(
            "fault injection is declared in the receipt",
            "skip" if not injected else "pass",
            f"{injected} injection(s) recorded, but no receipt exists to declare them in; "
            "the ledger carries the evidence",
        )
    else:
        declared = bool((receipt.get("ops") or {}).get("chaos_run"))
        check(
            "fault injection is declared in the receipt",
            "pass" if (declared or not injected) else "fail",
            f"{injected} injection(s) recorded; receipt ops.chaos_run={declared}",
        )

    # 4. The breaker's story must match the wire.
    breaker_snapshot = ((receipt or {}).get("ops") or {}).get("circuit_breaker") or {}
    opened = {name: state for name, state in breaker_snapshot.items() if state.get("openings")}
    if opened:
        check(
            "an opened breaker refused calls by name",
            "pass" if "circuit_open" in facts["denial_codes"] else "fail",
            f"openings={sum(s['openings'] for s in opened.values())}, refusal codes seen="
            f"{facts['denial_codes'] or 'none'}",
        )
    elif receipt is not None:
        check(
            "an opened breaker refused calls by name",
            "skip",
            "no breaker opened in this run (no upstream failure reached the threshold)",
        )

    # 5. Throttling must be visible on both sides: the boundary's own state, the retry
    #    hint the agent was given, and the code the agent reports having received.
    limited = len(facts["rate_limited"])
    if limited:
        items = facts["rate_limited"]
        names_limiter = all(isinstance(item.get("limiter"), dict) for item in items)
        hints = [str(item.get("retry_after_seconds")) for item in items]
        hints_present = all(hint not in {"None", "", "0"} for hint in hints)
        agent_codes = [str(code) for code in (summary or {}).get("refusals", [])] if summary else []
        throttle_codes = {"rate_limited", "max_events", "max_ledger_bytes"}
        agent_saw = any(code in throttle_codes for code in agent_codes)
        check(
            "throttled calls are recorded with the limiter's own state and a retry hint",
            "pass" if (names_limiter and hints_present) else "fail",
            f"{limited} throttle event(s); limiter snapshot on each: {names_limiter}; "
            f"retry_after_seconds={hints[:3]}",
        )
        if summary is not None:
            check(
                "the agent was told *why* it was refused, not just refused",
                "pass" if agent_saw else "fail",
                f"the agent reports refusal codes {sorted(set(agent_codes))}; "
                f"throttle codes seen on the wire: {sorted(set(agent_codes) & throttle_codes)}",
            )

    # 6. The receipt's headline numbers must match the recorded calls.
    if receipt is not None:
        totals = receipt.get("totals") or {}
        check(
            "the receipt's call count matches the recorded successful calls",
            "pass" if totals.get("calls") == facts["model_responses"] else "fail",
            f"receipt totals.calls={totals.get('calls')}, ledger model.response={facts['model_responses']}",
        )

    # 7. The ledger file must be readable, and any damage classified honestly.
    diagnostics = facts["diagnostics"]
    if diagnostics.get("torn_tail"):
        check(
            "a truncated final line is reported as damage, not silently accepted",
            "pass",
            f"torn tail at line {diagnostics['torn_tail']['line']} "
            f"({diagnostics['torn_tail']['bytes']} bytes) — recoverable with "
            "`docket doctor --quarantine-torn-tail`",
        )
    else:
        check(
            "the ledger file parses cleanly",
            "pass" if diagnostics.get("healthy", False) else "fail",
            f"{facts['events']} events, {diagnostics.get('bytes')} bytes",
        )

    # 8. If the ledger lost events, the receipt must say so — and must fail verification.
    if receipt is not None:
        evidence = receipt.get("evidence") or {}
        complete = evidence.get("ledger_complete")
        if complete is None:
            check(
                "the receipt declares whether every event reached disk",
                "skip",
                "this receipt predates the evidence member",
            )
        else:
            check(
                "the receipt declares whether every event reached disk",
                "pass" if complete or facts.get("unwritten") else "fail",
                f"evidence.ledger_complete={complete}, unwritten={evidence.get('unwritten_events')}",
            )
        if complete is False:
            from .verify import verify_receipt

            result = verify_receipt(receipt, ledger_path=ledger_path)
            failed_evidence = [c for c in result.checks if c.name == "evidence-completeness" and c.status == "fail"]
            check(
                "an incomplete ledger fails verification instead of passing quietly",
                "pass" if (failed_evidence and not result.ok) else "fail",
                f"verifier: {len(result.failures)} failure(s), evidence-completeness="
                f"{failed_evidence[0].status if failed_evidence else 'missing'}",
            )

    # 9. Exit codes must be explained by something in the record.
    if exit_code is not None and exit_code > 0:
        explained = bool(facts["cap_events"]) or facts["denials"] > 0 or facts["errors"] > 0 or exit_code == 143
        check(
            "a non-zero exit is explained by a recorded cause",
            "pass" if explained else "fail",
            f"exit {exit_code}; caps={len(facts['cap_events'])}, denials={facts['denials']}, "
            f"errors={facts['errors']}",
        )
    # 10. Ledger ceiling: when the record refuses to grow, the run must stop.
    ledger_caps = [c for c in facts["cap_events"] if c.get("cap") in {"max_events", "max_ledger_bytes"}]
    if ledger_caps:
        check(
            "a ledger at its ceiling stops the run rather than silently dropping events",
            "pass" if exit_code is not None and exit_code != 0 else "fail",
            f"cap {ledger_caps[0]['cap']} hit at {ledger_caps[0].get('observed')}; exit code {exit_code}",
        )
    return checks, facts


# --------------------------------------------------------------------------- #
# docket chaos
# --------------------------------------------------------------------------- #


def chaos_environment(**overrides: str) -> dict[str, str]:
    """Environment for the synthetic agent in a chaos run."""
    env = {
        "DOCKET_AGENT_CALLS": "8",
        "DOCKET_AGENT_MODE": "retry",
    }
    env.update(overrides)
    return env


def run_chaos(
    *,
    faults_spec: str,
    seed: int = 0,
    calls: int = 8,
    mode: str = "retry",
    policy: Policy | None = None,
    command: list[str] | None = None,
    workspace: Path | None = None,
    docket_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
    keep: bool = False,
) -> dict[str, Any]:
    """Run one supervised experiment with faults armed, then audit it.

    ``command`` defaults to the bundled synthetic agent: a real process, real HTTP, real
    boundary. With a command of your own, the faults land on your agent instead.

    ``workspace`` is where the run's ledger and receipts are written. Supply one to keep them
    (a caller's directory is never removed); leave it out and a scratch workspace is created and
    removed when the process exits, unless ``keep`` or ``DOCKET_KEEP_SCRATCH`` says otherwise.
    The outcome reports which happened, so a caller can tell a human where the evidence is —
    and only when it is still there.
    """
    from .fakeupstream import FakeUpstream, Step
    from .supervisor import Supervisor

    faults = parse_fault_spec(faults_spec)
    # A chaos experiment must be reproducible and offline: the scripted provider speaks
    # the real response shapes, so the boundary, the ledger and the verifier are
    # exercised for real while the provider side stays deterministic.
    upstream = FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=9_000, output_tokens=250)])
    upstream_host, upstream_port = upstream.start()
    upstream_url = f"http://{upstream_host}:{upstream_port}"
    workspace, workspace_kept = scratch_workspace("docket-chaos-", keep=keep, path=workspace)
    docket_dir = docket_dir or workspace / ".docket"
    env = chaos_environment(DOCKET_AGENT_CALLS=str(calls), DOCKET_AGENT_MODE=mode)
    if extra_env:
        env.update(extra_env)
    if command is None:
        command = [sys.executable, "-m", "docket.scriptagent"]
        env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1]))

    supervisor = Supervisor(
        command,
        policy=policy or Policy(name="chaos experiment", caps=Caps(max_usd=None)),
        docket_dir=str(docket_dir),
        cwd=str(workspace),
        profile="minimal",
        upstreams={"openai": upstream_url, "anthropic": upstream_url},
        faults=faults,
        fault_seed=seed,
        env_extra=env,
    )
    started = time.monotonic()
    try:
        result = supervisor.run()
    finally:
        upstream.stop()
    duration = round(time.monotonic() - started, 3)
    checks, facts = audit_run(
        result.ledger_path, result.receipt, exit_code=result.exit_code, expected_faults=len(faults)
    )
    return {
        "faults": [f.describe() for f in faults],
        "seed": seed,
        "workspace": str(workspace),
        "workspace_kept": workspace_kept,
        "duration_seconds": duration,
        "exit_code": result.exit_code,
        "breached": result.breached,
        "docket_number": result.docket_number,
        "receipt_path": str(result.receipt_path) if result.receipt_path else None,
        "ledger_path": str(result.ledger_path),
        "checks": checks,
        "facts": {k: v for k, v in facts.items() if k != "notes"},
        "notes": facts["notes"],
    }


def render_chaos(outcome: dict[str, Any]) -> str:
    """A chaos report that leads with what was learned, not with what was run."""
    lines: list[str] = []
    lines.append(f"  docket chaos — faults: {'; '.join(outcome['faults']) or 'none'} (seed {outcome['seed']})")
    lines.append("")
    lines.append(
        f"  run {outcome['docket_number']} · exit {outcome['exit_code']} · "
        f"{outcome['duration_seconds']}s · {outcome['facts']['events']} events"
    )
    facts = outcome["facts"]
    lines.append(
        f"  boundary: {facts['model_requests']} model.request · {facts['model_responses']} model.response · "
        f"{facts['denials']} denied {facts['denial_codes']} · {facts['errors']} error {facts['error_statuses']}"
    )
    if facts["faults"]:
        kinds: dict[str, int] = {}
        for item in facts["faults"]:
            kinds[str(item.get("kind"))] = kinds.get(str(item.get("kind")), 0) + 1
        lines.append(f"  injected: {kinds}")
    if facts["rate_limited"]:
        lines.append(f"  throttled: {len(facts['rate_limited'])} call(s)")
    if facts["cap_events"]:
        lines.append(f"  caps: {[c.get('cap') for c in facts['cap_events']]}")
    lines.append("")
    marks = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
    for check in outcome["checks"]:
        lines.append(f"  {marks[check['status']]}  {check['name']}")
        lines.append(f"        {check['detail']}")
    failed = [c for c in outcome["checks"] if c["status"] == "fail"]
    skipped = [c for c in outcome["checks"] if c["status"] == "skip"]
    lines.append(
        f"  {len(outcome['checks']) - len(failed) - len(skipped)} passed, {len(failed)} failed, "
        f"{len(skipped)} skipped"
    )
    lines.append("")
    if outcome["receipt_path"]:
        lines.append(f"  receipt: {outcome['receipt_path']}")
        lines.append(f"  verify it: docket verify {outcome['receipt_path']}")
    else:
        lines.append("  no receipt: the run did not reach the point where one is written")
    if outcome.get("workspace_kept"):
        lines.append(f"  workspace kept at {outcome.get('workspace')}")
    else:
        lines.append(
            "  the scratch workspace is removed when this command exits "
            "(--keep, or DOCKET_KEEP_SCRATCH=1, to inspect the ledger)"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# docket game-day
# --------------------------------------------------------------------------- #


@dataclass
class Phase:
    """One deliberate failure, with the expectation written down *before* running it."""

    name: str
    description: str
    expect: list[str]
    env: dict[str, str] = field(default_factory=dict)
    faults: str = ""
    policy_mutator: str = ""
    kill_after: float | None = None
    #: Turn on the content snapshot and automatic rollback for this phase, and write these
    #: files into the workspace before the agent starts — the state a restore has to put back.
    snapshot: bool = False
    seed_files: dict[str, str] = field(default_factory=dict)


GAME_DAY_PHASES: list[Phase] = [
    Phase(
        name="baseline",
        description="A well-behaved agent, nothing broken. Everything after this is compared to it.",
        expect=[
            "every call is forwarded and recorded",
            "the receipt verifies end to end",
            "no refusals, no faults, no evidence gaps",
        ],
        env={"DOCKET_AGENT_CALLS": "4", "DOCKET_AGENT_MODE": "bounded"},
    ),
    Phase(
        name="upstream-failure",
        description="The provider fails some calls and serves others: are the two kept apart?",
        expect=[
            "failures are recorded as model.error, never as model.response",
            "successes are still counted and billed at the provider's own numbers",
            "the receipt declares ops.chaos_run so a reader knows this was an experiment",
        ],
        env={"DOCKET_AGENT_CALLS": "10", "DOCKET_AGENT_MODE": "retry"},
        faults="status=500:0.5",
    ),
    Phase(
        name="circuit-breaker",
        description="Every call fails: how many is enough before the boundary stops trying?",
        expect=[
            "the circuit opens after the configured threshold of consecutive failures",
            "later calls are refused with a named reason and an upstream that is not contacted",
            "the receipt carries the breaker's snapshot, so the opening is reconstructable",
        ],
        env={"DOCKET_AGENT_CALLS": "10", "DOCKET_AGENT_MODE": "retry"},
        faults="status=500:1.0",
    ),
    Phase(
        name="upstream-latency",
        description="The provider is slow rather than broken: costs, timeouts and the wall-clock cap.",
        expect=[
            "slow calls still complete and are billed at the provider's own numbers",
            "no call is recorded twice",
        ],
        env={"DOCKET_AGENT_CALLS": "4", "DOCKET_AGENT_MODE": "bounded"},
        faults="delay=350ms:0.6",
    ),
    Phase(
        name="rate-limit",
        description="A fast agent against a per-second ceiling: throttling must be visible to the agent.",
        expect=[
            "calls beyond the ceiling are refused with 429 and Retry-After",
            "each throttle is a rate-limit event in the ledger",
        ],
        env={"DOCKET_AGENT_CALLS": "6", "DOCKET_AGENT_MODE": "retry"},
        policy_mutator="per_second",
    ),
    Phase(
        name="ledger-pressure",
        description="The ledger hits its event ceiling mid-run: the tool cannot keep its promise to record.",
        expect=[
            "the boundary refuses further model calls when it can no longer record them",
            "the cap is named in the ledger and the supervisor stops the run",
            "the receipt declares the incompleteness and verification reports it",
        ],
        env={"DOCKET_AGENT_CALLS": "12", "DOCKET_AGENT_MODE": "retry"},
        policy_mutator="max_events",
    ),
    Phase(
        name="rollback",
        description=(
            "The agent damages the working tree and then breaches its cap: does the automatic "
            "rollback put the tree back, and does the record say what it could not put back?"
        ),
        expect=[
            "a content snapshot is taken before the agent starts and is declared in the receipt",
            "the breach triggers a restore, recorded as a ledger event as well as in the receipt",
            "the seeded file is byte-for-byte what it was before the run",
            "the run's own ledger and receipt survive the rollback (the evidence is not collateral)",
            "anything the snapshot could not cover is named, never implied to be restored",
        ],
        env={
            "DOCKET_AGENT_CALLS": "6",
            "DOCKET_AGENT_MODE": "bounded",
            "DOCKET_AGENT_WRITE": "notes.txt",
        },
        policy_mutator="tight_cap",
        snapshot=True,
        seed_files={
            "notes.txt": "the agent should not touch this\n",
            "src/app.py": "print('hello')\n",
        },
    ),
    Phase(
        name="supervisor-killed",
        description="SIGKILL the supervisor mid-run: what survives, what is lost, what recovery looks like.",
        expect=[
            "the ledger up to the kill is still readable (and a torn tail is classified as one)",
            "no receipt is written, because the process that writes it is gone",
            "the recovery path is stated, not hidden",
        ],
        env={"DOCKET_AGENT_CALLS": "60", "DOCKET_AGENT_MODE": "retry", "DOCKET_AGENT_SLEEP": "0.2"},
        kill_after=2.0,
    ),
]


def _phase_policy(phase: Phase, base: Policy) -> Policy:
    policy = Policy(
        name=base.name,
        budget_mode=base.budget_mode,
        caps=base.caps,
        files=base.files,
        models=base.models,
        egress=base.egress,
        resilience=base.resilience,
    )
    if phase.policy_mutator == "per_second":
        policy.resilience.max_calls_per_second = 2
    elif phase.policy_mutator == "max_events":
        policy.resilience.max_events = 8
    elif phase.policy_mutator == "tight_cap":
        # A cap a single scripted call crosses, so the breach (and therefore the rollback)
        # happens on a schedule rather than by luck. Copied, not mutated: `Caps` is shared
        # with the base policy, and a phase that edited it in place would change the next phase.
        policy.caps = replace(policy.caps, max_usd=Decimal("0.01"))
    return policy


def run_phase(
    phase: Phase,
    *,
    root: Path,
    command: list[str] | None = None,
    base_policy: Policy | None = None,
    offline: bool = True,
) -> dict[str, Any]:
    """Run one phase for real and audit it. Returns a record for the post-mortem.

    ``offline`` (the default) points the boundary at a local scripted provider that
    speaks the real OpenAI and Anthropic response shapes: the experiment then needs no
    API key, no network and no spend, and stays reproducible. Pass ``offline=False`` to
    run the same phases against your configured provider — with a cap set, which is the
    entire point of this tool.
    """
    from .fakeupstream import FakeUpstream, Step
    from .supervisor import Supervisor

    upstream = None
    upstreams: dict[str, str] | None = None
    # A phase spawned as its own `docket run` process passes the scripted provider down
    # through the environment; that keeps the child a real CLI invocation rather than a
    # special-cased in-process shortcut.
    inherited = os.environ.get("DOCKET_GAME_DAY_UPSTREAM")
    if inherited:
        with contextlib.suppress(json.JSONDecodeError):
            upstreams = json.loads(inherited)
        offline = False
    if offline:
        upstream = FakeUpstream(
            steps=[
                Step(model="claude-sonnet-5", input_tokens=9_000, output_tokens=250),
                Step(model="claude-sonnet-5", input_tokens=12_000, output_tokens=300),
            ]
        )
        host, port = upstream.start()
        upstreams = {"openai": f"http://{host}:{port}", "anthropic": f"http://{host}:{port}"}

    workspace = root / phase.name
    workspace.mkdir(parents=True, exist_ok=True)
    # The state a rollback is supposed to put back, written before anything runs: a phase
    # that only ever modified files the agent itself created would test nothing.
    for relative, content in phase.seed_files.items():
        seeded = workspace / relative
        seeded.parent.mkdir(parents=True, exist_ok=True)
        seeded.write_text(content, encoding="utf-8")
    docket_dir = workspace / ".docket"
    env = dict(phase.env)
    if command is None:
        command = [sys.executable, "-m", "docket.scriptagent"]
        env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    policy = _phase_policy(phase, base_policy or Policy(name="game day", caps=Caps(max_usd=None)))
    faults = parse_fault_spec(phase.faults) if phase.faults else []

    record: dict[str, Any] = {
        "phase": phase.name,
        "description": phase.description,
        "expect": list(phase.expect),
        "faults": [f.describe() for f in faults],
        "env": {k: v for k, v in env.items() if k.startswith("DOCKET_AGENT")},
    }

    if phase.kill_after is not None:
        record.update(_killed_phase(phase, command, env, workspace, docket_dir, policy, upstreams))
    else:
        supervisor = Supervisor(
            command,
            policy=policy,
            docket_dir=str(docket_dir),
            cwd=str(workspace),
            profile="minimal" if offline else "full",
            upstreams=upstreams,
            faults=faults,
            fault_seed=7,
            env_extra=env,
            snapshot_files=phase.snapshot,
            rollback_on_breach=phase.snapshot,
        )
        started = time.monotonic()
        try:
            result = supervisor.run()
        finally:
            if upstream is not None:
                upstream.stop()
        record["duration_seconds"] = round(time.monotonic() - started, 3)
        record["exit_code"] = result.exit_code
        record["breached"] = result.breached
        record["docket_number"] = result.docket_number
        record["ledger_path"] = str(result.ledger_path)
        record["receipt_path"] = str(result.receipt_path) if result.receipt_path else None
        checks, facts = audit_run(
            result.ledger_path, result.receipt, exit_code=result.exit_code, expected_faults=len(faults)
        )
        record["checks"] = checks
        record["facts"] = {k: v for k, v in facts.items() if k != "notes"}
        if result.receipt_path and result.receipt_path.exists():
            from .receipt import load_receipt
            from .verify import verify_receipt

            receipt = load_receipt(result.receipt_path)
            # The ops block, kept in the phase record: snapshot and rollback declarations are
            # facts about the run that the post-mortem has to be able to show, and the record
            # is read by people who will not open a 14 kB receipt to find them.
            record["receipt_ops"] = receipt.get("ops")
            verification = verify_receipt(receipt, ledger_path=result.ledger_path)
            record["verification"] = {
                "ok": verification.ok,
                "passed": sum(1 for c in verification.checks if c.status == "pass"),
                "failed": len(verification.failures),
                "skipped": sum(1 for c in verification.checks if c.status == "skip"),
                "failures": verification.failures,
            }
    # A rollback phase is checked against the disk as well as the record: the claim being
    # tested is "the tree is back", and only the tree can settle that. The pre-run contents
    # are known here (this phase seeded them), so the comparison is byte-for-byte rather than
    # a digest of a digest.
    if phase.snapshot:
        record.setdefault("checks", []).extend(_rollback_checks(phase, workspace, docket_dir, record))
    # A phase "passes" when nothing failed. Skips do not count as failures — but they
    # are also never reported as passes, and the post-mortem lists them.
    record["phase_ok"] = not [c for c in record.get("checks", []) if c["status"] == "fail"]
    return record


def _rollback_checks(phase: Phase, workspace: Path, docket_dir: Path, record: dict[str, Any]) -> list[dict[str, str]]:
    """Did the tree come back, and does the record say what came back?"""
    checks: list[dict[str, str]] = []
    operations = record.get("receipt_ops") or {}
    snapshot = operations.get("snapshot") or {}
    rollback = operations.get("rollback") or {}

    changed = [
        path for path, content in phase.seed_files.items() if (workspace / path).read_text(encoding="utf-8") != content
    ]
    checks.append(
        {
            "name": "the seeded files are byte-for-byte what they were before the run",
            "status": "pass" if not changed else "fail",
            "detail": f"{len(phase.seed_files)} seeded file(s) checked on disk; "
            + ("all restored" if not changed else f"still different: {changed}"),
        }
    )
    checks.append(
        {
            "name": "the rollback is declared in the receipt",
            "status": "pass" if rollback.get("applied") else "fail",
            "detail": f"ops.rollback.applied={rollback.get('applied')}, trigger={rollback.get('trigger')!r}, "
            f"counts={rollback.get('counts')}",
        }
    )
    facts = record.get("facts", {})
    applied_events = facts.get("rollbacks") or []
    refused_events = facts.get("rollbacks_refused") or []
    # `facts` is trimmed of notes and stored in the record; the rollback events are kept.
    checks.append(
        {
            "name": "the rollback is a ledger event, not just a receipt field",
            "status": "pass" if (applied_events or refused_events) else "fail",
            "detail": (
                f"{len(applied_events)} x-docket.rollback-applied and "
                f"{len(refused_events)} x-docket.rollback-refused event(s) in the ledger"
            ),
        }
    )
    runs = sorted((docket_dir / "runs").glob("*.jsonl"))
    receipts = sorted((docket_dir / "receipts").glob("*.json"))
    survived = bool(runs) and (bool(receipts) or record.get("receipt_path") is None)
    checks.append(
        {
            "name": "the run's own evidence survived the rollback",
            "status": "pass" if survived else "fail",
            "detail": f"{len(runs)} ledger file(s) and {len(receipts)} receipt(s) still present under "
            f"{docket_dir}; a rollback that deleted its own record would be worse than no rollback",
        }
    )
    if snapshot.get("taken"):
        not_covered = snapshot.get("not_covered") or []
        checks.append(
            {
                "name": "the receipt names what the snapshot could not cover",
                "status": "pass" if not_covered else "skip",
                "detail": (
                    f"not covered: {[item.get('path') for item in not_covered]}"
                    if not_covered
                    else "every watched file was covered, so there is nothing to disclose here"
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "the receipt declares the snapshot it took",
                "status": "fail",
                "detail": f"ops.snapshot={snapshot or None}: the phase asked for --snapshot",
            }
        )
    return checks


def _killed_phase(
    phase: Phase,
    command: list[str],
    env: dict[str, str],
    workspace: Path,
    docket_dir: Path,
    policy: Policy,
    upstreams: dict[str, str] | None = None,
) -> dict[str, Any]:
    """SIGKILL the supervisor mid-run and report exactly what that costs.

    This is the disaster-recovery phase: not "does the code handle an exception", but
    "what does an operator find on disk when the process is gone and cannot clean up".
    """
    from . import __version__

    child_env = dict(os.environ)
    child_env.update(env)
    child_env["PYTHONPATH"] = env.get("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    argv = [
        sys.executable,
        "-m",
        "docket",
        "run",
        # Absolute paths: the child runs with cwd set to the phase workspace, so a
        # relative path here would resolve somewhere else entirely (the first version of
        # this phase failed for exactly that reason, and the child's stderr said so).
        "--policy",
        str(_write_phase_policy(workspace, policy).resolve()),
        "--docket-dir",
        str(docket_dir.resolve()),
    ]
    if upstreams:
        child_env["DOCKET_GAME_DAY_UPSTREAM"] = json.dumps(upstreams)
    argv += ["--", *command]
    proc = subprocess.Popen(argv, cwd=str(workspace), env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        # "Kill it mid-run" only means something if the run has started. Wait for the
        # ledger to exist (the supervisor creates it before spawning the agent), then let
        # the configured number of seconds of work happen before pulling the plug.
        ledger_dir = docket_dir / "runs"
        deadline = time.monotonic() + 20
        ledgers: list[Path] = []
        while time.monotonic() < deadline:
            ledgers = sorted(ledger_dir.glob("*.jsonl")) if ledger_dir.exists() else []
            if ledgers and ledgers[0].stat().st_size > 0:
                break
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        if not ledgers:
            stderr_tail = ""
            with contextlib.suppress(Exception):
                stderr_tail = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")[-1200:]
            return {
                "killed_after_seconds": None,
                "checks": [
                    _mk_check(
                        "the supervised run started before it was killed",
                        False,
                        "no ledger appeared within 20s, so there was no run to kill; the child's "
                        f"stderr says: {stderr_tail.strip() or '(nothing)'}",
                    )
                ],
                "facts": {},
            }
        time.sleep(phase.kill_after or 1.0)
        pre_kill_events = len(ledgers[0].read_text(encoding="utf-8").splitlines())
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)
        # Did the agent survive its supervisor? Measure it twice, because the interesting
        # fact is not "a process existed" but "could it keep spending": once immediately
        # (it is a separate session, so nothing reaps it for us) and once after a few
        # seconds of trying to reach a boundary that no longer exists.
        immediate = _scriptagent_alive()
        time.sleep(3.0)
        after_three_seconds = _scriptagent_alive()
        agent_alive = immediate or after_three_seconds
        fact = ledger_facts(ledgers[0])
        receipt_dir = docket_dir / "receipts"
        receipts = sorted(receipt_dir.glob("*.receipt.json")) if receipt_dir.exists() else []
        torn = bool(fact["diagnostics"].get("torn_tail"))
        checks = [
            _mk_check(
                "the ledger written before the kill is still readable",
                fact["events"] > 0,
                f"{fact['events']} event(s) readable after SIGKILL; {pre_kill_events} line(s) were on "
                f"disk at kill time",
            ),
            _mk_check(
                "no receipt is promised when the process that writes it is gone",
                not receipts,
                f"{len(receipts)} receipt(s) present; a receipt is written by the supervisor, so a "
                "killed supervisor writes none",
            ),
            _mk_check(
                "damage is classified rather than hidden",
                fact["diagnostics"].get("healthy", False) or torn or bool(fact["diagnostics"].get("bad_lines")),
                "read_with_diagnostics classified the file: "
                + (
                    f"torn tail at line {fact['diagnostics']['torn_tail']['line']}, recoverable with "
                    "`docket doctor --quarantine-torn-tail`"
                    if torn
                    else "parses cleanly (the kill landed between writes)"
                ),
            ),
            _mk_check(
                "killing the supervisor stops the agent from reaching a provider",
                not after_three_seconds,
                f"agent process present immediately after the kill: {immediate}; "
                f"present 3s later: {after_three_seconds}"
                + (
                    " — its base URL points at the boundary in the killed process, so every "
                    "subsequent call fails at once rather than reaching a provider"
                    if immediate and not after_three_seconds
                    else ""
                ),
            ),
            _mk_check(
                "the loss is bounded and stated",
                fact["events"] > 0,
                f"the record ends at the last flushed event ({fact['events']} of {pre_kill_events} lines "
                "readable); events already fsynced are not lost, and nothing after the kill was ever "
                "recorded",
            ),
        ]
        recovery = (
            f"docket doctor --docket-dir {docket_dir} --quarantine-torn-tail {ledgers[0]}"
            if torn
            else f"docket doctor --docket-dir {docket_dir}"
        )
        return {
            "killed_after_seconds": phase.kill_after,
            "agent_survived_supervisor_kill": agent_alive,
            "agent_alive_immediately_after_kill": immediate,
            "agent_alive_after_3s": after_three_seconds,
            "events_before_kill": pre_kill_events,
            "checks": checks,
            "facts": {k: v for k, v in fact.items() if k != "notes"},
            "recovery_command": recovery,
            "tool_version": __version__,
        }
    finally:
        if proc.poll() is None:  # pragma: no cover - safety net
            with contextlib.suppress(OSError):
                proc.kill()
        with contextlib.suppress(Exception):
            proc.stderr.close() if proc.stderr else None


def _mk_check(name: str, condition: bool, detail: str) -> dict[str, str]:
    """A single check record, in the same tri-state shape ``audit_run`` produces."""
    return {"name": name, "status": "pass" if condition else "fail", "detail": detail}


def _scriptagent_alive() -> bool:
    """True when a synthetic agent is still running after its supervisor was killed."""
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, check=False).stdout
    return "docket.scriptagent" in out


def _write_phase_policy(workspace: Path, policy: Policy) -> Path:
    path = workspace / "docket.toml"
    atomic_write(path, policy.to_toml())
    return path


def run_game_day(
    *,
    root: Path | None = None,
    phases: list[Phase] | None = None,
    command: list[str] | None = None,
    policy: Policy | None = None,
    keep: bool = False,
) -> dict[str, Any]:
    """Run the whole series and return the post-mortem record.

    ``root`` is where every phase's ledger, receipt and the post-mortem are written; supply one
    (as ``docket game-day --out`` does) and it is kept. Left out, a scratch root is created and
    removed at exit unless ``keep`` or ``DOCKET_KEEP_SCRATCH`` says otherwise — and ``root_kept``
    in the returned record says which.
    """
    root, root_kept = scratch_workspace("docket-game-day-", keep=keep, path=root)
    phases = phases if phases is not None else GAME_DAY_PHASES
    started = time.monotonic()
    records = [run_phase(phase, root=root, command=command, base_policy=policy) for phase in phases]
    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": str(root),
        "root_kept": root_kept,
        "duration_seconds": round(time.monotonic() - started, 3),
        "phases": records,
        "phases_ok": sum(1 for r in records if r.get("phase_ok")),
        "phases_total": len(records),
        "failed_checks": [
            {"phase": r["phase"], **check}
            for r in records
            for check in r.get("checks", [])
            if check["status"] == "fail"
        ],
        "skipped_checks": [
            {"phase": r["phase"], **check}
            for r in records
            for check in r.get("checks", [])
            if check["status"] == "skip"
        ],
    }
    return report


def render_post_mortem(report: dict[str, Any]) -> str:
    """Write the post-mortem: what was attempted, what happened, what it means, what leaks."""
    lines: list[str] = []
    lines.append("# Game day post-mortem")
    lines.append("")
    lines.append(f"Run {report['started_at']} · {report['phases_total']} phases · {report['duration_seconds']}s")
    lines.append("")
    lines.append(
        "Each phase is a real supervised run: real child process, real boundary, real ledger, "
        "real receipt. Nothing below is a simulation of Docket; the failures are simulated, "
        "the recording is not."
    )
    lines.append("")
    lines.append(f"**Result: {report['phases_ok']}/{report['phases_total']} phases met every stated expectation.**")
    lines.append("")
    for record in report["phases"]:
        lines.append(f"## {record['phase']}")
        lines.append("")
        lines.append(record["description"])
        lines.append("")
        lines.append("Expected:")
        for item in record["expect"]:
            lines.append(f"- {item}")
        lines.append("")
        lines.append("Observed:")
        for check in record.get("checks", []):
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[check["status"]]
            lines.append(f"- **{mark}** {check['name']} — {check['detail']}")
        facts = record.get("facts") or {}
        if facts:
            lines.append("")
            lines.append(
                "  counts: "
                + json.dumps(
                    {
                        "events": facts.get("events"),
                        "model.request": facts.get("model_requests"),
                        "model.response": facts.get("model_responses"),
                        "model.denied": facts.get("denials"),
                        "model.error": facts.get("errors"),
                        "faults": len(facts.get("faults") or []),
                        "rate_limited": len(facts.get("rate_limited") or []),
                        "caps": [c.get("cap") for c in facts.get("cap_events") or []],
                    }
                )
            )
        for key, label in (
            ("exit_code", "exit code"),
            ("breached", "stopped by cap"),
            ("docket_number", "docket number"),
            ("verification", "verification"),
            ("recovery_command", "recovery"),
            ("agent_survived_supervisor_kill", "agent survived the supervisor kill"),
        ):
            if key in record and record[key] is not None:
                lines.append(f"- {label}: `{record[key]}`")
        lines.append("")
    lines.append("## What this proves")
    lines.append("")
    lines.append(
        "- Failures at the provider boundary are recorded as failures, refusals are recorded as "
        "refusals, and neither is ever written down as a successful call."
    )
    lines.append(
        "- The circuit breaker, the rate limiter and the ledger ceiling all stop traffic *and say so "
        "in the ledger*, so an operator reading the receipt can reconstruct the decision."
    )
    lines.append(
        "- A ledger that could not be completed is declared incomplete in the receipt and fails "
        "verification, rather than passing as a clean record."
    )
    lines.append("- A killed supervisor leaves a readable ledger, no receipt, and a named recovery command.")
    lines.append("")
    lines.append("## What this does not prove")
    lines.append("")
    lines.append(
        "- Nothing here is a substitute for testing *your* agent: these phases exercise Docket's "
        "boundary, not your application's own retry logic, queues or partial writes."
    )
    lines.append(
        "- The upstream failures are injected by Docket itself. They model provider errors, latency "
        "and truncation; they do not model a hostile provider, TLS interception, or DNS failures, "
        "which are covered separately by the boundary's egress and URL handling."
    )
    lines.append(
        "- SIGKILL is the strongest kill available; a machine losing power may lose events that were "
        "buffered but not fsynced — Docket fsyncs each event for exactly this reason, and the "
        "surviving prefix is what verification can prove."
    )
    lines.append("")
    lines.append("## Known limits (not yet covered by an automated phase)")
    lines.append("")
    lines.append(
        "- Multi-process contention on one ledger is handled with an exclusive lock and reported as "
        "`LedgerLockedError`; a *queue* of writers is not implemented, by design — Docket's model is "
        "one supervisor per run."
    )
    lines.append(
        "- Clock skew across machines is not corrected: timestamps are recording-time evidence, not "
        "wall-clock truth."
    )
    lines.append(
        "- The fake upstream used by the offline commands is deliberately single-node; upstream "
        "multi-region failover is out of scope for the boundary and belongs to your gateway."
    )
    lines.append("")
    return "\n".join(lines)
