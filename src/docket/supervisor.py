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

import io
import os
import platform
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import events as ev
from .canon import digest_of, num
from .fsdiff import deny_write_violations, diff, snapshot, tree_digest
from .ledger import Ledger
from .policy import Policy
from .pricing import CostMeter, PriceTable
from .proxy import RecordingProxy
from .receipt import build_receipt, write_receipt
from .util import atomic_write, docket_number

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

    # -- public API -------------------------------------------------------- #
    def run(self) -> RunResult:
        started_mono = self.monotonic()
        runs_dir = self.docket_dir / "runs"
        receipts_dir = self.docket_dir / "receipts"
        ledger_path = runs_dir / f"{self.session_id}.jsonl"
        ledger = Ledger.open(ledger_path, clock=self.clock)
        meter = CostMeter(price_table=self.price_table, mode=self.policy.budget_mode)
        number = docket_number(rng=self.rng)
        breach_state: dict[str, str | None] = {"cap": None}

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

        proxy = RecordingProxy(
            ledger=ledger,
            policy=self.policy,
            meter=meter,
            session_id=self.session_id,
            upstreams=self.upstreams,
            record_bodies=self.record_bodies,
        )
        host, port = proxy.start()

        def on_breach(cap: str) -> None:
            breach_state["cap"] = cap

        proxy.on_breach = on_breach

        before = {}
        if self.policy.files.watch:
            before = snapshot(self.policy.files.watch, self.policy.files.ignore, cwd=self.cwd)
            ledger.append(
                ev.FS_SNAPSHOT,
                {"phase": "before", "files": len(before), "tree_sha256": tree_digest(before), "roots": self.policy.files.watch},
            )

        env = self._child_env(host, port)
        proc: subprocess.Popen[bytes] | None = None
        exit_code = EXIT_INTERNAL
        interrupted = False
        previous_handlers: dict[int, Any] = {}

        try:
            ledger.append(ev.PROC_SPAWN, {"argv": self.argv, "cwd": str(self.cwd)})
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
                    try:
                        proc.send_signal(signum)
                    except OSError:
                        pass

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    previous_handlers[sig] = signal.signal(sig, _forward)
                except (ValueError, OSError):  # pragma: no cover - non-main thread
                    pass

            exit_code = self._supervise(proc, breach_state, started_mono, ledger)
        finally:
            for sig, handler in previous_handlers.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):  # pragma: no cover
                    pass

        # -- filesystem evidence -------------------------------------------- #
        after: dict[str, Any] = {}
        changes: list[dict[str, Any]] = []
        if self.policy.files.watch:
            after = snapshot(self.policy.files.watch, self.policy.files.ignore, cwd=self.cwd)
            changes = diff(before, after)
            for change in changes[:MAX_RECORDED_FILE_CHANGES]:
                ledger.append(ev.FS_CHANGE, change)
            if len(changes) > MAX_RECORDED_FILE_CHANGES:
                ledger.append(
                    ev.NOTE,
                    {"message": f"{len(changes) - MAX_RECORDED_FILE_CHANGES} further file changes elided"},
                )
            ledger.append(
                ev.FS_SNAPSHOT,
                {"phase": "after", "files": len(after), "tree_sha256": tree_digest(after), "changes": len(changes)},
            )
            for violation in deny_write_violations(changes, self.policy.files.deny_write):
                ledger.append(
                    ev.POLICY_VIOLATION,
                    {
                        "rule": "files.deny_write",
                        "path": violation["path"],
                        "detail": "file inside a deny_write scope was changed during the run",
                    },
                )

        duration = self.monotonic() - started_mono
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
            seal_key=self.seal_key,
            sign_key=self.sign_key,
            label=self.label,
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
                "DOCKET_TOOL_VERSION": "0.1.0",
            }
        )
        env.update(self.env_extra)
        return env

    def _supervise(
        self,
        proc: subprocess.Popen[bytes],
        breach_state: dict[str, str | None],
        started_mono: float,
        ledger: Ledger,
    ) -> int:
        wall_limit = self.policy.caps.max_wall_seconds
        while True:
            code = proc.poll()
            if code is not None:
                ledger.append(ev.PROC_EXIT, {"exit_code": code})
                return code
            elapsed = self.monotonic() - started_mono
            if wall_limit is not None and elapsed >= wall_limit and breach_state["cap"] is None:
                breach_state["cap"] = "max_wall_seconds"
                ledger.append(
                    ev.CAP_EXCEEDED,
                    {"cap": "max_wall_seconds", "elapsed_seconds": num(round(elapsed, 3)), "action": "kill"},
                )
            if breach_state["cap"] and self.policy.caps.on_breach == "kill":
                self._terminate(proc, ledger, breach_state["cap"])
                code = proc.wait(timeout=30)
                ledger.append(ev.PROC_EXIT, {"exit_code": code, "terminated_by": "docket"})
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
        ledger.append(ev.NOTE, {"message": f"sent SIGTERM to process group after {cap} cap"})
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
            try:
                proc.kill()
            except OSError:
                pass
        ledger.append(ev.NOTE, {"message": f"sent SIGKILL after {grace}s grace period"})


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
