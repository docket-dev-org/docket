"""`docket watch`: tail a live run, with a spend meter.

`ROADMAP.md` §0.2 promised "`docket watch` — tail a live run with a spend meter, safe to leave on a
second terminal", and there was no such command: the only "watch" anywhere in `--help` was a word in
the `demo` blurb. A run you cannot see while it happens is a run you find out about afterwards, which
is the wrong way round for a tool whose whole subject is a cap.

The command follows a ledger file as a supervisor appends to it, prints each event as it lands, and
keeps a running total costed against the same price table the boundary used. It stops when the run
does — a `session.end` event — or on Ctrl-C, and it prints a summary either way.

    docket run --max-usd 5 -- ./agent.sh &     # one terminal
    docket watch                                # another

Nothing here talks to the boundary or to a provider. It reads a file, which is what makes it safe to
leave running and what makes it work against a run started by somebody else.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from . import events as ev
from .pricing import CostMeter, PriceTable

#: How long to wait for another line before deciding the run has gone quiet.
POLL_INTERVAL = 0.25

#: How long a ledger may stay unchanged before `watch` decides the run is over. A supervisor that
#: has exited leaves the file alone, so silence is the end-of-run signal; the timeout is generous
#: enough that a slow model call does not look like the end.
QUIET_SECONDS = 5.0

#: The events worth printing. Everything else is ledger plumbing — the policy digest, the snapshot
#: before and after — and a tail that prints plumbing is a tail nobody leaves running.
SHOWN: frozenset[str] = frozenset(
    {
        ev.MODEL_REQUEST,
        ev.MODEL_RESPONSE,
        ev.MODEL_CACHE_HIT,
        ev.TOOL_CALL,
        ev.TOOL_RESULT,
        ev.NET_DENIED,
        ev.POLICY_VIOLATION,
        ev.CAP_WARNING,
        ev.CAP_EXCEEDED,
        ev.FS_CHANGE,
        ev.PROC_SPAWN,
        ev.PROC_EXIT,
        ev.SESSION_START,
        ev.SESSION_END,
    }
)


def _count(value: object) -> str:
    """A token count with thousands separators, or `?` when it is not a number."""
    try:
        return f"{int(value):,}"  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return "?"


class WatchError(RuntimeError):
    """No ledger to follow, or one that cannot be read."""


@dataclass
class Watcher:
    """Follows one ledger file and keeps the running totals."""

    meter: CostMeter
    price_table: PriceTable
    interval: float = POLL_INTERVAL
    quiet_seconds: float = QUIET_SECONDS
    seen: int = 0
    events_shown: int = 0
    finished: bool = False
    interrupted: bool = False
    docket_number: str = ""
    last_line_at: float = field(default_factory=time.monotonic)

    # -- reading ----------------------------------------------------------- #

    def follow(self, path: Path, out: TextIO, *, once: bool = False) -> int:
        """Print events as they arrive, and return a process exit code."""
        if not path.exists():
            raise WatchError(f"no ledger at {path}")
        with path.open("r", encoding="utf-8") as handle:
            while True:
                line = handle.readline()
                if line:
                    self.last_line_at = time.monotonic()
                    self._handle_line(line, out)
                    if self.finished:
                        break
                    continue
                if once:
                    break
                if self.finished:
                    break
                if time.monotonic() - self.last_line_at > self.quiet_seconds:
                    # The run has gone quiet. That is the end-of-run signal for a supervisor that
                    # exited, and it is reported rather than assumed silently.
                    out.write("watch: no new events; assuming the run has finished\n")
                    out.flush()
                    break
                time.sleep(self.interval)
        self._summary(out)
        return 0

    def _handle_line(self, line: str, out: TextIO) -> None:
        self._remember_number(line)
        line = line.strip()
        if not line:
            return
        self.seen += 1
        try:
            import json

            event = json.loads(line)
        except ValueError:
            # A torn tail is what a supervisor killed mid-write leaves behind. It is not an event,
            # and refusing to print the rest of the file over it would be worse.
            return
        if not isinstance(event, dict):
            return
        kind = str(event.get("type") or "")
        data = event.get("data") or {}
        self._meter(kind, data)
        if kind == ev.SESSION_END:
            self.finished = True
        if kind in SHOWN:
            self.events_shown += 1
            out.write(self._render(kind, data))
            out.flush()

    def _remember_number(self, line: str) -> None:
        """Keep the docket number from `session.start`, so the end line can name the run."""
        import json

        try:
            event = json.loads(line)
        except ValueError:
            return
        if isinstance(event, dict) and event.get("type") == ev.SESSION_START:
            self.docket_number = str((event.get("data") or {}).get("docket_number") or "")

    # -- the meter --------------------------------------------------------- #

    def _meter(self, kind: str, data: dict[str, Any]) -> None:
        """Cost a metered response the same way the boundary did."""
        if kind != ev.MODEL_RESPONSE:
            return
        model = str(data.get("model") or "")
        usage = data.get("usage") or {}
        try:
            self.meter.record(
                model=model,
                input_tokens=int(usage.get("input_tokens") or 0),
                cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                usage_source=str(data.get("usage_source") or "reported"),
            )
        except (TypeError, ValueError):
            # An event that cannot be costed is shown but not counted, and the summary says so
            # rather than reporting a total that quietly omitted a call.
            self.uncosted = getattr(self, "uncosted", 0) + 1

    # -- rendering --------------------------------------------------------- #

    def _render(self, kind: str, data: dict[str, Any]) -> str:
        detail = self._detail(kind, data)
        return f"{self._stamp(kind)} {kind:<18} {detail}\n"

    def _stamp(self, kind: str) -> str:
        if kind in (ev.SESSION_START, ev.SESSION_END):
            return "    "
        return f"${self.meter.cost:>8.6f}"

    def _detail(self, kind: str, data: dict[str, Any]) -> str:
        if kind == ev.MODEL_RESPONSE:
            usage = data.get("usage") or {}
            # The usage fields are formatted, not assumed: an event whose counts are not numbers is
            # still shown, because a tail that stops printing is worse than one that prints "?".
            return (
                f"{data.get('model', '?')}  in={_count(usage.get('input_tokens'))} "
                f"out={_count(usage.get('output_tokens'))}  ${data.get('cost_usd', '0')}"
            )
        if kind == ev.MODEL_REQUEST:
            return f"{data.get('model', '?')}  {data.get('path', '')}"
        if kind == ev.MODEL_CACHE_HIT:
            return f"cache hit  saved ${data.get('saved_usd', data.get('savings_usd', '0'))}"
        if kind == ev.TOOL_CALL:
            return f"{data.get('name', '?')}  {data.get('args_digest', '')[:16]}"
        if kind == ev.TOOL_RESULT:
            ok = "ok" if data.get("ok") else "failed"
            return f"{data.get('name', '?')}  {ok}  {data.get('result_bytes', 0):,} bytes"
        if kind == ev.NET_DENIED:
            return f"denied  {data.get('host', data.get('path', '?'))}  ({data.get('reason', '?')})"
        if kind == ev.POLICY_VIOLATION:
            return f"{data.get('rule', '?')}  {data.get('path', '')}"
        if kind == ev.CAP_WARNING:
            return f"{data.get('cap', '?')} at {data.get('fraction', '?')} of the limit"
        if kind == ev.CAP_EXCEEDED:
            return f"{data.get('cap', '?')} exceeded — the run is being stopped"
        if kind == ev.FS_CHANGE:
            return f"{data.get('change', '?')}  {data.get('path', '?')}"
        if kind == ev.PROC_SPAWN:
            argv = data.get("argv") or []
            return f"spawned {' '.join(str(a) for a in argv[:3])}"
        if kind == ev.PROC_EXIT:
            return f"exit {data.get('exit_code', '?')}"
        if kind == ev.SESSION_START:
            return f"session {data.get('docket_number', '?')}  {data.get('cwd', '')}"
        if kind == ev.SESSION_END:
            breached = f"  breached={data.get('breached')}" if data.get("breached") else ""
            return (
                f"{self.docket_number or '?'} finished in {data.get('duration_seconds', '?')}s  "
                f"exit {data.get('exit_code', '?')}{breached}"
            )
        return str(data)[:120]

    def _summary(self, out: TextIO) -> None:
        uncosted = getattr(self, "uncosted", 0)
        out.write("\n")
        out.write(
            f"watch: {self.events_shown} event(s) shown of {self.seen} in the ledger  "
            f"spend ${self.meter.cost}  {self.meter.calls} call(s)\n"
        )
        if self.meter.assumed_models:
            out.write(f"watch: priced by assumption for {', '.join(sorted(self.meter.assumed_models))}\n")
        if uncosted:
            out.write(f"watch: {uncosted} metered response(s) could not be costed and are not in the total\n")
        out.flush()


def newest_ledger(docket_dir: Path) -> Path | None:
    """The most recently modified ledger in a Docket directory, or `None`."""
    runs = docket_dir / "runs"
    if not runs.is_dir():
        return None
    ledgers = sorted(runs.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    return ledgers[-1] if ledgers else None


def resolve_ledger(named: str | None, cwd: Path) -> Path:
    """The ledger to follow: the one named, or the newest one beside the caller."""
    if named:
        path = Path(named)
        if not path.exists():
            raise WatchError(f"no ledger at {path}")
        return path
    # Only the directory the caller named. Falling back to the process's own working directory
    # would make `watch` follow a run that has nothing to do with the one asked about, which is
    # worse than saying there is nothing to follow.
    found = newest_ledger(cwd / ".docket")
    if found is not None:
        return found
    raise WatchError("no ledger found. Start a run in another terminal, or pass --ledger <file> pointing at one.")


def watch(
    named: str | None,
    *,
    out: TextIO | None = None,
    once: bool = False,
    interval: float = POLL_INTERVAL,
    cwd: Path | None = None,
) -> int:
    """Follow a ledger and print it. Returns a process exit code."""
    stream = out or sys.stdout
    ledger = resolve_ledger(named, cwd or Path.cwd())
    meter = CostMeter(price_table=PriceTable.load())
    watcher = Watcher(meter=meter, price_table=PriceTable.load(), interval=interval)
    stream.write(f"watch: following {ledger}\n")
    stream.flush()
    try:
        return watcher.follow(ledger, stream, once=once)
    except KeyboardInterrupt:
        watcher.interrupted = True
        stream.write("\n")
        watcher._summary(stream)  # noqa: SLF001 - the summary is the same for an interrupted run
        return 0


def render_once(events: Iterable[dict[str, Any]], price_table: PriceTable | None = None) -> str:
    """Render a fixed list of events, for `--once` and for the tests.

    Reading a finished ledger and rendering it is the same code path as following a live one, which
    is what makes `--once` honest rather than a second implementation.
    """
    import io
    import json

    meter = CostMeter(price_table=price_table or PriceTable.load())
    watcher = Watcher(meter=meter, price_table=price_table or PriceTable.load())
    buffer = io.StringIO()
    for event in events:
        watcher._handle_line(json.dumps(event), buffer)  # noqa: SLF001
    summary = io.StringIO()
    watcher._summary(summary)  # noqa: SLF001
    return buffer.getvalue() + summary.getvalue()
