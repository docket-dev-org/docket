"""The supervisor: one process, one docket number, one bounded session.

``docket run -- policy.toml -- my-agent`` does this:

1. opens a session ledger and records the policy that will be enforced;
2. starts the loopback boundary proxy;
3. snapshots the watched filesystem scope;
4. starts the agent with base-URL (and optionally ``HTTP_PROXY``) environment
   variables pointing at the boundary;
5. watches wall-clock and the cap state while the agent runs, terminating the
   process group when a cap is crossed;
6. snapshots again, diffs, and records what changed;
7. closes the session, builds a receipt and prints the docket number.

The supervisor is deliberately boring about the child process: it does not inject
into the agent's runtime, does not patch its HTTP client, and does not require the
agent to be written in Python. That is what makes it work with an agent you did not
write.
"""

from __future__ import annotations

import contextlib
import io
import os
import platform
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import events as ev
from ._version import __version__
from .canon import num
from .fsdiff import deny_write_violations, diff, snapshot, tree_digest
from .ledger import Ledger, LedgerFullError, _now_rfc3339
from .policy import Policy
from .pricing import CostMeter, PriceTable
from .proxy import RecordingProxy
from .receipt import build_receipt, write_receipt
from .resilience import CircuitBreaker, EvidenceGuard, FaultInjector, HealthState, RateLimiter
from .snapshot import (
    DEFAULT_MAX_BYTES,
    SnapshotTooLargeError,
    apply_restore,
    plan_restore,
    read_manifest,
    summarise,
    take_snapshot,
)
from .util import atomic_write, docket_number, sanitize_url

EXIT_OK = 0
EXIT_CAP_EXCEEDED = 42
EXIT_POLICY_ERROR = 43
EXIT_INTERNAL = 70

MAX_RECORDED_FILE_CHANGES = 500


@dataclass
class RunResult:
    session_id: str
    docket_number: str
    exit_code: int
    breached: str | None
    ledger_path: Path
    receipt_path: Path
    receipt: dict[str, Any] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)


def _base_url_env(host: str, port: int, profile: str) -> dict[str, str]:
    root = f"http://{host}:{port}"
    env = {
        "OPENAI_BASE_URL": f"{root}/openai/v1",
        "OPENAI_API_BASE": f"{root}/openai/v1",
        "ANTHROPIC_BASE_URL": f"{root}/anthropic",
        "GEMINI_API_BASE_URL": f"{root}/gemini",
        "DEEPSEEK_BASE_URL": f"{root}/deepseek/v1",
        "MISTRAL_BASE_URL": f"{root}/mistral/v1",
        "OPENROUTER_BASE_URL": f"{root}/openrouter/v1",
        "GROQ_BASE_URL": f"{root}/groq/v1",
        "OLLAMA_HOST": f"{root}/ollama",
        "DOCKET_PROXY_URL": root,
    }
    if profile == "minimal":
        return {k: v for k, v in env.items() if k in {"OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "DOCKET_PROXY_URL"}}
    return env


def check_docket_dir(directory: str | os.PathLike[str]) -> str | None:
    """A reason the evidence directory cannot be used, or ``None``.

    Checked *before* the boundary starts, because every alternative is worse. Discovering
    mid-run that the ledger cannot be written turns a recorded run into an unrecorded one while
    the agent keeps spending, and a report that says "0 events" when it means "the directory was
    read-only" is the exact failure this project exists to prevent. So a run that cannot keep the
    record does not start, and the message names the fix.

    The symlink case is not hypothetical: ``.docket`` in a shared checkout can be a link into a
    directory the run's owner does not control, which would put another party's copy of the
    evidence somewhere the owner never chose. Same class of problem as the ledger's own
    ``O_NOFOLLOW`` rule.
    """
    path = Path(directory)
    if path.is_symlink():
        return (
            f"refusing to use {path} as the docket directory: it is a symbolic link, so the run's "
            "evidence would be written through it to a path you did not choose. Point --docket-dir "
            "at a directory you control."
        )
    if path.exists() and not path.is_dir():
        return f"refusing to use {path} as the docket directory: it exists and is not a directory."
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        return f"cannot create {path}: {probe} is not writable by this user, so evidence cannot be recorded."
    return None


class Supervisor:
    """Runs a child process under a policy and produces a receipt."""

    def __init__(
        self,
        argv: list[str],
        *,
        policy: Policy | None = None,
        docket_dir: str | os.PathLike[str] = ".docket",
        cwd: str | os.PathLike[str] | None = None,
        profile: str = "full",
        http_proxy: bool = False,
        record_bodies: bool = False,
        price_table: PriceTable | None = None,
        upstreams: dict[str, str] | None = None,
        snapshot_files: bool = False,
        snapshot_max_bytes: int = DEFAULT_MAX_BYTES,
        rollback_on_breach: bool = False,
        clock: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        seal_key: str | None = None,
        sign_key: str | os.PathLike[str] | None = None,
        env_extra: dict[str, str] | None = None,
        session_id: str | None = None,
        stdout: Any = None,
        stderr: Any = None,
        rng: Any = None,
        label: str | None = None,
        faults: list[Any] | None = None,
        fault_seed: int = 0,
        evidence_warn: Any = None,
        time_scale: float = 1.0,
        proxy_port: int = 0,
    ) -> None:
        self.argv = argv
        self.policy = policy or Policy()
        self.docket_dir = Path(docket_dir)
        self.cwd = Path(cwd or os.getcwd())
        self.profile = profile
        self.http_proxy = http_proxy
        self.record_bodies = record_bodies
        self.price_table = price_table or PriceTable.load()
        self.upstreams = upstreams or {}
        # Rollback is opt-in for a reason: a snapshot stores file *contents*, and copying a
        # working tree into .docket on every run is a surprise nobody asked for. --snapshot turns
        # the copy on; --rollback-on-breach additionally restores it when a cap is breached.
        self.snapshot_files = bool(snapshot_files) or bool(rollback_on_breach)
        self.snapshot_max_bytes = int(snapshot_max_bytes)
        self.rollback_on_breach = bool(rollback_on_breach)
        #: Filled in by :meth:`run` so callers (and the receipt) can report what was captured
        #: and whether anything was restored.
        self.snapshot: dict[str, Any] | None = None
        self.rollback: dict[str, Any] | None = None
        self.clock = clock
        self.monotonic = monotonic
        self.seal_key = seal_key
        self.sign_key = sign_key
        self.env_extra = env_extra or {}
        self.session_id = session_id or _session_id(self.clock, rng)
        self.rng = rng
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self.label = label
        #: Faults are an explicit, recorded act: a run with injections is marked in the
        #: ledger and cannot be mistaken for a normal run's evidence.
        self.faults = faults or []
        self.fault_seed = fault_seed
        self.evidence_warn = evidence_warn
        #: Wall-clock caps are measured in real seconds, but a supervised wait can be
        #: scaled for tests. 1.0 means "faithful"; never changed by the CLI.
        self.time_scale = time_scale
        #: The port the boundary listens on. 0 means "any free port"; a fixed port is what a
        #: health probe or a metrics scrape needs, and it is recorded in the run's evidence.
        self.proxy_port = int(proxy_port)

    def _record(
        self, ledger: Ledger, event_type: str, data: dict[str, Any], breach_state: dict[str, str | None]
    ) -> Any:
        """Append supervisor-written evidence, tolerating a ledger that is already full.

        The rule for the supervising tool is the opposite of the rule for the agent: a
        full ledger must never turn the *shutdown path* into a traceback. When the ceiling
        is hit, the run is marked as stopped by it and the receipt says so; nothing is
        silently dropped, and a control event naming the ceiling is always recordable.
        """
        try:
            return ledger.append(event_type, data)
        except LedgerFullError as exc:
            if breach_state.get("cap") is None:
                breach_state["cap"] = "max_events" if ledger.max_events is not None else "max_ledger_bytes"
            with contextlib.suppress(Exception):
                ledger.append(
                    ev.CAP_EXCEEDED,
                    {
                        "cap": breach_state["cap"],
                        "observed": exc.observed,
                        "limit": exc.limit,
                        "action": f"not recording {event_type} (ledger ceiling reached)",
                    },
                    control=True,
                )
            return None

    def _exclude_own_evidence(self) -> str | None:
        """Keep Docket's own evidence directory out of the tree it watches.

        The ledger is written *during* the run, so if it lives inside the watched tree it shows
        up in the diff as something the agent created. Two consequences, both bad: the record
        blames the agent for Docket's own writes, and — with `--snapshot` — a rollback plan
        derived from that diff contains an instruction to delete the ledger. A recorder that
        destroys its own evidence while reporting a tidy rollback is worse than no rollback.

        So the exclusion is structural, not a policy the caller has to remember: the evidence
        directory is stripped from the watched tree by construction, the effective policy in
        the receipt carries the exclusion, and the ledger says so in a `note`.
        """
        try:
            relative = self.docket_dir.resolve().relative_to(self.cwd.resolve())
        except (OSError, ValueError):
            return None  # outside the watched tree: nothing to exclude
        if relative == Path("."):
            # `--docket-dir .`: the evidence directory *is* the tree. Excluding "." would
            # exclude everything, so this is disclosed instead of silently watched.
            return "."
        pattern = f"{relative.as_posix().rstrip('/')}/**"
        if pattern not in self.policy.files.ignore:
            self.policy.files.ignore = [*self.policy.files.ignore, pattern]
        return pattern

    def _say(self, message: str) -> None:
        """One line to the operator, on stderr, at the moment it matters."""
        print(f"docket: {message}", file=self.stderr, flush=True)

    def _warn_evidence(self, message: str) -> None:
        """One loud line the first time evidence cannot be written. Never silent."""
        if self.evidence_warn is not None:
            self.evidence_warn(message)
            return
        self._say(message)

    # -- public API -------------------------------------------------------- #
    def run(self) -> RunResult:
        started_mono = self.monotonic()
        # Before the ledger exists, or the policy is recorded: excluding the evidence
        # directory changes the effective policy, and the receipt has to carry the policy that
        # was actually in force.
        evidence_exclusion = self._exclude_own_evidence()
        if evidence_exclusion == ".":
            self._say(
                "the evidence directory is the working directory, so this run's own ledger and "
                "receipts are part of the tree it watches — point --docket-dir at a directory "
                "you control if that is not what you meant"
            )
        runs_dir = self.docket_dir / "runs"
        receipts_dir = self.docket_dir / "receipts"
        ledger_path = runs_dir / f"{self.session_id}.jsonl"
        resilience = self.policy.resilience
        guard = EvidenceGuard(
            fail_fast=resilience.fail_fast_evidence,
            warn=self._warn_evidence,
        )
        ledger = Ledger.open(
            ledger_path,
            clock=self.clock,
            guard=guard,
            max_events=resilience.max_events,
            max_bytes=resilience.max_ledger_bytes,
        )
        meter = CostMeter(price_table=self.price_table, mode=self.policy.budget_mode)
        number = docket_number(rng=self.rng)
        breach_state: dict[str, str | None] = {"cap": None}
        breaker = CircuitBreaker(
            threshold=resilience.breaker_threshold,
            cooldown_seconds=float(resilience.breaker_cooldown_seconds),
            max_cooldown_seconds=float(resilience.breaker_max_cooldown_seconds),
        )
        limiter = RateLimiter(
            per_second=float(resilience.max_calls_per_second) if resilience.max_calls_per_second else None,
            per_minute=resilience.max_calls_per_minute,
        )
        injector = FaultInjector(self.faults, seed=self.fault_seed) if self.faults else None
        health = HealthState()

        ledger.append(
            ev.SESSION_START,
            {
                "session_id": self.session_id,
                "docket_number": number,
                "label": self.label,
                "argv": self.argv,
                "cwd": str(self.cwd),
                "profile": self.profile,
                "http_proxy": self.http_proxy,
                "record_bodies": self.record_bodies,
                "tool": "docket",
                # The *requested* port. 0 means "any free port", and the port that was
                # actually bound is recorded in a note once the socket exists, so the evidence
                # never states a guess as a fact.
                "boundary": {"host": "127.0.0.1", "requested_port": self.proxy_port},
                # Where the *model* traffic was sent. An override changes what a call means (a
                # mock, a gateway, a self-hosted model is not the provider's own API), so it is
                # evidence, not configuration. Query, fragment and userinfo are dropped: a token
                # pasted into a URL must never reach a ledger.
                "upstream_overrides": {name: sanitize_url(url) for name, url in sorted((self.upstreams or {}).items())},
                "host": {
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "machine": platform.machine(),
                },
            },
        )
        ledger.append(
            ev.POLICY_APPLIED,
            {
                "name": self.policy.name,
                "digest": self.policy.digest(),
                "document": self.policy.to_dict(),
                "source": self.policy.source_path,
            },
        )
        if evidence_exclusion and evidence_exclusion != ".":
            ledger.append(
                ev.NOTE,
                {
                    "message": "the evidence directory is inside the watched tree and is excluded from it",
                    "excluded": evidence_exclusion,
                    "why": (
                        "Docket writes its ledger during the run; watching it would record Docket's own "
                        "writes as the agent's changes, and a rollback derived from that diff would "
                        "delete the record it was derived from"
                    ),
                },
            )

        proxy = RecordingProxy(
            ledger=ledger,
            policy=self.policy,
            meter=meter,
            session_id=self.session_id,
            upstreams=self.upstreams,
            record_bodies=self.record_bodies,
            breaker=breaker,
            limiter=limiter,
            injector=injector,
            health=health,
            min_free_bytes=resilience.min_free_bytes,
        )
        host, port = proxy.start(self.proxy_port)
        ledger.append(
            ev.NOTE,
            {
                "message": "the boundary is listening on loopback",
                "host": host,
                "port": port,
                "requested_port": self.proxy_port,
            },
        )

        if injector is not None:
            ledger.append(
                "x-docket.fault-plan",
                {"seed": self.fault_seed, "spec": [f.describe() for f in self.faults]},
            )

        def on_breach(cap: str) -> None:
            breach_state["cap"] = cap

        proxy.on_breach = on_breach

        before = {}
        if self.policy.files.watch:
            before = snapshot(self.policy.files.watch, self.policy.files.ignore, cwd=self.cwd)
            ledger.append(
                ev.FS_SNAPSHOT,
                {
                    "phase": "before",
                    "files": len(before),
                    "tree_sha256": tree_digest(before),
                    "roots": self.policy.files.watch,
                },
            )
        if self.snapshot_files and self.policy.files.watch:
            # Contents, not digests: copied before the agent exists, so the only files in the
            # store are ones the agent has not touched yet. A tree over the budget is refused
            # here — before the agent runs — rather than discovered as unrestorable later.
            store = self.docket_dir / "snapshots" / self.session_id
            try:
                taken = take_snapshot(
                    roots=list(self.policy.files.watch),
                    ignore=list(self.policy.files.ignore),
                    root=self.cwd,
                    store=store,
                    max_bytes=self.snapshot_max_bytes,
                    taken_at=(self.clock or _now_rfc3339)(),
                )
            except SnapshotTooLargeError as exc:
                ledger.append(
                    ev.NOTE,
                    {"message": f"snapshot refused: {exc}", "watched_bytes": exc.watched, "budget": exc.budget},
                    control=True,
                )
                ledger.append(
                    ev.SESSION_END,
                    {
                        "exit_code": EXIT_INTERNAL,
                        "duration_seconds": "0.000",
                        "breached": None,
                        "interrupted": False,
                        "note": "the run did not start: its file snapshot did not fit the budget",
                    },
                )
                ledger.close()
                raise
            self.snapshot = taken.to_dict()
            ledger.append(
                ev.FS_SNAPSHOT,
                {
                    "phase": "contents",
                    "format": "docket/snapshot/v1",
                    "files": len(taken.entries),
                    "bytes_stored": taken.bytes_stored,
                    "bytes_watched": taken.bytes_watched,
                    "not_covered": len(taken.not_covered),
                    "store": str(store),
                },
            )

        env = self._child_env(host, port)
        proc: subprocess.Popen[bytes] | None = None
        exit_code = EXIT_INTERNAL
        interrupted = False
        # Keyed by signal *number*, and the values are the handlers `signal.signal` returns:
        # either a callable or one of the SIG_* constants, which is why the value type is `Any`.
        previous_handlers: dict[int, Any] = {}

        try:
            ledger.append(ev.PROC_SPAWN, {"argv": self.argv, "cwd": str(self.cwd)}, control=True)
            proc = subprocess.Popen(
                self.argv,
                cwd=str(self.cwd),
                env=env,
                stdout=_popen_stdio(self.stdout),
                stderr=_popen_stdio(self.stderr),
                stdin=None,
                start_new_session=True,
            )

            def _forward(signum: int, frame: Any) -> None:  # pragma: no cover - signal path
                nonlocal interrupted
                interrupted = True
                if proc and proc.poll() is None:
                    with contextlib.suppress(OSError):
                        proc.send_signal(signum)

            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(ValueError, OSError):  # pragma: no cover - non-main thread
                    # `signal.signal` is overloaded on the signal type; the return is the
                    # previous handler, which mypy types through the `Signals` overload.
                    previous_handlers[sig] = signal.signal(sig, _forward)  # type: ignore[assignment]

            exit_code = self._supervise(proc, breach_state, started_mono, ledger, proxy)
        finally:
            # `signal.signal` is overloaded on the signal type, so mypy resolves the loop
            # variable through the `Signals` overload and rejects the int key. The dict is keyed
            # by number because that is what `signal.signal` hands back.
            for sig, handler in previous_handlers.items():  # type: ignore[assignment]
                with contextlib.suppress(ValueError, OSError):  # pragma: no cover
                    # `handler` is `Any` because that is what `signal.signal` hands back.
                    signal.signal(sig, handler)

        # -- filesystem evidence -------------------------------------------- #
        after: dict[str, Any] = {}
        changes: list[dict[str, Any]] = []
        if self.policy.files.watch:
            after = snapshot(self.policy.files.watch, self.policy.files.ignore, cwd=self.cwd)
            changes = diff(before, after)
            for change in changes[:MAX_RECORDED_FILE_CHANGES]:
                self._record(ledger, ev.FS_CHANGE, change, breach_state)
            if len(changes) > MAX_RECORDED_FILE_CHANGES:
                ledger.append(
                    ev.NOTE,
                    {"message": f"{len(changes) - MAX_RECORDED_FILE_CHANGES} further file changes elided"},
                    control=True,
                )
            self._record(
                ledger,
                ev.FS_SNAPSHOT,
                {"phase": "after", "files": len(after), "tree_sha256": tree_digest(after), "changes": len(changes)},
                breach_state,
            )
            for violation in deny_write_violations(changes, self.policy.files.deny_write):
                self._record(
                    ledger,
                    ev.POLICY_VIOLATION,
                    {
                        "rule": "files.deny_write",
                        "path": violation["path"],
                        "detail": "file inside a deny_write scope was changed during the run",
                    },
                    breach_state,
                )

        if self.snapshot_files and self.rollback_on_breach and breach_state["cap"] and changes:
            self.rollback = self._rollback(ledger, changes)
            counts = self.rollback.get("counts") or {}
            if self.rollback.get("applied"):
                summary = (
                    ", ".join(f"{value} {key}" for key, value in sorted(counts.items()) if value) or "nothing to do"
                )
                self._say(
                    f"the run breached {breach_state['cap']} and {len(changes)} file(s) changed: "
                    f"the watched tree has been put back from the snapshot ({summary}). "
                    f"`docket restore --help` shows the same restore done by hand."
                )
            elif self.rollback.get("refused"):
                # Refused, not silent: an operator whose tree is still damaged has to hear it
                # from the run, in the same breath as the breach that caused it.
                self._say(
                    f"the run breached {breach_state['cap']} and {len(changes)} file(s) changed, and NOTHING "
                    f"was put back: the snapshot could not restore {len(self.rollback['refused'])} path(s). "
                    f"The receipt's ops.rollback.refused names each one and why, and this run's own record "
                    f"lists the same in `docket report`."
                )
            for item in self.rollback.get("refused") or []:
                self._say(f"NOT rolled back: {item['path']} — {item['reason']}")

        # Same clamp, same reason as the cap check: the receipt's `duration_seconds` is read by
        # people and compared against the wall-clock cap, and a negative duration is a claim no
        # clock can support.
        duration = max(0.0, self.monotonic() - started_mono)
        if breach_state["cap"]:
            exit_code = EXIT_CAP_EXCEEDED
        ledger.append(
            ev.SESSION_END,
            {
                "exit_code": exit_code,
                "duration_seconds": f"{duration:.3f}",
                "breached": breach_state["cap"],
                "interrupted": interrupted,
                "cost_usd": str(meter.cost),
            },
        )
        events = list(ledger)
        # -- evidence integrity and failure behaviour ---------------------- #
        # Three things must be visible in the receipt, not just in a log line: whether
        # every event reached the disk, whether the boundary stopped talking to an
        # upstream, and whether the run was a chaos run.
        if ledger.degraded:
            gap_event = {
                "unwritten": ledger.unwritten[:20],
                "count": len(ledger.unwritten),
                "first_failing_seq": ledger.unwritten[0]["seq"],
                "last_error": ledger.unwritten[-1]["error"],
                "note": "the file is not a complete record: the chain will not verify past the first gap",
            }
            # Best-effort: the same conditions that caused the gap may prevent this write.
            with contextlib.suppress(Exception):
                ledger.append("x-docket.evidence-gap", gap_event)
            for gap in guard.gaps.extra:
                ledger.append("note", {"message": "evidence note", **gap})

        receipt = build_receipt(
            events=events,
            ledger=ledger,
            meter=meter,
            policy=self.policy,
            session_id=self.session_id,
            docket_number=number,
            exit_code=exit_code,
            duration_seconds=duration,
            price_table=self.price_table,
            proxy_stats=proxy.stats,
            snapshot=self.snapshot,
            rollback=self.rollback,
            seal_key=self.seal_key,
            sign_key=Path(self.sign_key) if self.sign_key is not None else None,
            label=self.label,
            breaker=breaker,
            limiter=limiter,
            injector=injector,
            health=health,
            cache=proxy.cache,
        )
        ledger.close()
        proxy.stop()

        receipt_path = receipts_dir / f"{number}.receipt.json"
        write_receipt(receipt, receipt_path)
        atomic_write(self.docket_dir / "latest", number + "\n")
        atomic_write(
            self.docket_dir / "latest.receipt.json",
            (receipt_path.read_text(encoding="utf-8")),
        )

        return RunResult(
            session_id=self.session_id,
            docket_number=number,
            exit_code=exit_code,
            breached=breach_state["cap"],
            ledger_path=ledger_path,
            receipt_path=receipt_path,
            receipt=receipt,
            timings={"duration_seconds": duration},
        )

    # -- internals ---------------------------------------------------------- #
    def _child_env(self, host: str, port: int) -> dict[str, str]:
        env = dict(os.environ)
        env.update(_base_url_env(host, port, self.profile))
        if self.http_proxy:
            proxy_url = f"http://{host}:{port}"
            env["HTTP_PROXY"] = proxy_url
            env["HTTPS_PROXY"] = proxy_url
            env["http_proxy"] = proxy_url
            env["https_proxy"] = proxy_url
            env["ALL_PROXY"] = proxy_url
            env["NO_PROXY"] = "localhost,127.0.0.1,::1"
            env["no_proxy"] = env["NO_PROXY"]
        env.update(
            {
                "DOCKET_SESSION": self.session_id,
                "DOCKET_DOCKET_DIR": str(self.docket_dir),
                "DOCKET_POLICY_DIGEST": self.policy.digest(),
                "DOCKET_TOOL_VERSION": __version__,
            }
        )
        env.update(self.env_extra)
        return env

    def _rollback(self, ledger: Ledger, changes: list[dict[str, Any]]) -> dict[str, Any]:
        """Restore the watched tree from the run's snapshot after a breach.

        Deliberately after the run, not during it: killing the agent and then fighting it for
        the same files would leave a tree that is neither the agent's nor the operator's. The
        restore is recorded as evidence — what was planned, what was applied, and what could
        not be covered — because "we rolled it back" is a claim like any other.

        ``force`` is on here and off in `docket restore`: this happens milliseconds after the
        child exited, so the tree is still the run's, whereas an on-demand restore happens
        whenever the operator gets round to it and may well meet their own later edits.
        """
        store = self.docket_dir / "snapshots" / self.session_id
        if not (self.snapshot or {}).get("manifest"):
            # A breach that wanted a rollback and could not have one is still an outcome of the
            # run, and the record says so under the same event name as a declined restore —
            # with the reason, so the two cases are never confused.
            self._record_control(
                ledger,
                "x-docket.rollback-refused",
                {"trigger": "cap breached", "reason": "the run took no snapshot", "counts": {}, "paths": []},
            )
            return {"applied": False, "reason": "the run took no snapshot"}
        document = read_manifest(store)
        plan = plan_restore(document, changes)
        for item in plan:
            change = next((c for c in changes if c.get("path") == item["path"]), None)
            after = (change or {}).get("after")
            # What the file looks like now, according to the run's own record. `removed` needs
            # no protection: there is nothing left to overwrite.
            item["post_sha256"] = after.get("sha256") if isinstance(after, dict) else None
        applied = apply_restore(plan, root=self.cwd, store=store, force=True)
        counts = summarise(applied)
        undone = [item["path"] for item in applied if item["status"] in {"restored", "recreated", "removed"}]
        # The same three outcomes the receipt reports as "not put back": refused, failed, and
        # skipped (a change the snapshot has no copy of). `absent` means the file already is
        # what the restore wanted, so it is not a complaint.
        refused = [
            {"path": item["path"], "reason": item.get("reason") or item["status"]}
            for item in applied
            if item["status"] in {"refused", "failed", "skipped"}
        ]
        # "Was this rollback applied?" is one of the load-bearing claims a receipt makes, so it
        # is answered by the only evidence that counts — how many files actually went back. A
        # plan whose every item was refused is not a rollback that happened; it is a rollback
        # that was *attempted and declined*, and saying otherwise is exactly the kind of claim
        # the record exists to make checkable.
        did_something = bool(undone)
        # `applied_paths` carries every path, including the ones that refused: the receipt
        # reduces it to summary lists, and anything reading the record directly sees the detail.
        record: dict[str, Any] = {
            "applied": did_something,
            "trigger": "cap breached",
            "counts": counts,
            "applied_paths": applied,
            "refused": refused,
            "not_covered": document.get("not_covered") or [],
        }
        if not did_something:
            record["reason"] = (
                f"the restore was refused for every changed path ({len(refused)} of "
                f"{len(applied)}); the receipt's ops.rollback.refused names each one and why"
            )
        event: dict[str, Any] = {
            "trigger": record["trigger"],
            "restored": counts.get("restored", 0),
            "recreated": counts.get("recreated", 0),
            "removed": counts.get("removed", 0),
            "refused": counts.get("refused", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "paths": undone,
        }
        if not did_something:
            event["reason"] = record["reason"]
        self._record_control(
            ledger,
            # Namespaced, as spec §4.4 requires of an implementation's own extensions: the v1
            # vocabulary is closed, so a Docket-specific event has to be recognisable as such
            # by a verifier that has never heard of it. Two names, because "applied" and
            # "refused" are two outcomes and one name carrying both would be unreadable.
            "x-docket.rollback-applied" if did_something else "x-docket.rollback-refused",
            event,
        )
        return record

    def _record_control(self, ledger: Ledger, event_type: str, data: dict[str, Any]) -> None:
        """Append an event that describes the record itself, whatever the ledger's state.

        Control-plane facts (a rollback happened, evidence was lost) are the ones a reader most
        needs, so they go through the always-recordable path and never raise: a run that cannot
        write them says so in the receipt instead of dying at the last step.
        """
        try:
            ledger.append(event_type, data, control=True)
        except Exception as exc:  # noqa: BLE001 - the receipt carries the gap when this fails
            with contextlib.suppress(Exception):
                self._warn_evidence(f"{event_type} could not be recorded: {exc}")

    def _supervise(
        self,
        proc: subprocess.Popen[bytes],
        breach_state: dict[str, str | None],
        started_mono: float,
        ledger: Ledger,
        proxy: Any,
    ) -> int:
        wall_limit = self.policy.caps.max_wall_seconds
        while True:
            code = proc.poll()
            if code is not None:
                ledger.append(ev.PROC_EXIT, {"exit_code": code}, control=True)
                return code
            # A full ledger is a policy outcome, not an environment failure: the
            # boundary has already told the agent (in HTTP words) and recorded why. The
            # run stops here so that "the tool could not keep its promise to record"
            # cannot be mistaken for a normal completion.
            if (
                getattr(proxy, "ledger_full", None) is not None
                and proxy.ledger_full.is_set()
                and breach_state["cap"] is None
            ):
                breach_state["cap"] = (
                    "max_events" if getattr(ledger, "max_events", None) is not None else "max_ledger_bytes"
                )
            # `max(0.0, ...)` is not decoration. `monotonic` is an injectable source (tests and
            # embedders supply their own), and a source that steps *backwards* — a VM restored from
            # a snapshot, a container sharing a stale clock, a caller's own bug — would make
            # `elapsed` negative, which is a duration the receipt must never print: nothing can
            # spend a negative number of seconds, and a downstream reader comparing it to the cap
            # has to decide what a negative budget means. Clamping states the invariant instead:
            # elapsed time is a quantity, and quantities of time are not negative.
            elapsed = max(0.0, self.monotonic() - started_mono)
            if wall_limit is not None and elapsed >= wall_limit and breach_state["cap"] is None:
                breach_state["cap"] = "max_wall_seconds"
                ledger.append(
                    ev.CAP_EXCEEDED,
                    {"cap": "max_wall_seconds", "elapsed_seconds": num(round(elapsed, 3)), "action": "kill"},
                )
            if breach_state["cap"] and self.policy.caps.on_breach == "kill":
                self._terminate(proc, ledger, breach_state["cap"])
                code = proc.wait(timeout=30)
                ledger.append(ev.PROC_EXIT, {"exit_code": code, "terminated_by": "docket"}, control=True)
                return code
            time.sleep(0.05)

    def _terminate(self, proc: subprocess.Popen[bytes], ledger: Ledger, cap: str) -> None:
        if proc.poll() is not None:
            return
        grace = self.policy.caps.grace_seconds
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
            try:
                proc.terminate()
            except OSError:
                return
        ledger.append(ev.NOTE, {"message": f"sent SIGTERM to process group after {cap} cap"}, control=True)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
            with contextlib.suppress(OSError):
                proc.kill()
        ledger.append(ev.NOTE, {"message": f"sent SIGKILL after {grace}s grace period"}, control=True)


def _popen_stdio(stream: Any) -> Any:
    """Return a stream ``Popen`` can inherit, or ``None`` to inherit the parent's.

    Under test harnesses (and notebook kernels) ``sys.stdout`` is often an
    in-memory object whose ``fileno()`` raises, which ``Popen`` cannot accept. The
    honest fallback is inheritance rather than silently swallowing the child's
    output.
    """
    if stream is None:
        return None
    try:
        stream.fileno()
    except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
        return None
    return stream


def _session_id(clock: Callable[[], str] | None, rng: Any) -> str:
    import secrets

    source = rng or secrets.SystemRandom()
    return "".join(source.choice("0123456789abcdef") for _ in range(12))


def run_command(argv: Iterable[str], **kwargs: Any) -> RunResult:
    """Convenience wrapper: ``run_command(["my-agent", "--flag"], policy=...)``."""
    return Supervisor(list(argv), **kwargs).run()
