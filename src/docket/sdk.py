"""In-process instrumentation for agents you write yourself.

The boundary (``docket run``) records everything that crosses it. Tool calls and
notes happen *inside* your process, so they need a way in. Two ways are provided:

* this module, for Python agents: ``with Session() as s: s.record_tool(...)``;
* the boundary's instrumentation endpoint, for everything else::

      POST $DOCKET_PROXY_URL/docket/tool/call   {"name": "sql", "args_digest": "…"}

Both write into the same hash-chained ledger, and both mark their events with
``reported_by`` so a reader can tell boundary-observed facts from agent-reported
claims. That distinction is deliberate: an agent can lie to its own instrumentation,
and a receipt that hides this would be misleading.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import events as ev
from .canon import digest_of, usd
from .ledger import Ledger
from .policy import Policy
from .pricing import CostMeter, PriceTable
from .receipt import build_receipt, write_receipt
from .util import atomic_write, docket_number

_state = threading.local()


def _require_session() -> Session:
    session = getattr(_state, "session", None)
    if session is None:
        raise RuntimeError("no active Docket session: use `with docket.Session() as s:` first")
    return session


class _AppendResult:
    """The little bit of an event the SDK callers use (``seq`` and ``hash``)."""

    __slots__ = ("seq", "hash")

    def __init__(self, seq: int | None = None, hash_: str | None = None) -> None:
        self.seq = seq
        self.hash = hash_

    def __getitem__(self, key: str) -> Any:  # tolerate dict-style access in user code
        return {"seq": self.seq, "hash": self.hash}[key]


class _BoundaryRecorder:
    """Writes events through the supervised run's boundary, not into its ledger file.

    Two writers on one file would produce two competing hash chains, so an attached
    session must go through the boundary: the supervisor stays the only process that
    appends to the ledger, which is what keeps the chain verifiable. (Reading the
    file from a child and rewriting it would be worse: it would look like tampering.)
    """

    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.errors: list[str] = []

    def append(self, event_type: str, data: dict[str, Any]) -> _AppendResult:
        payload = json.dumps({"type": event_type, "data": data}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/docket/event",
            data=payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read() or b"{}")
            return _AppendResult(body.get("seq"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            # Never break the agent because instrumentation failed: record the failure
            # locally and keep going. The receipt will simply not contain the claim.
            self.errors.append(f"{event_type}: {exc}")
            return _AppendResult(None)


class _LedgerRecorder:
    """Writes to a ledger this process owns (the standalone case)."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def append(self, event_type: str, data: dict[str, Any]) -> Any:
        return self.ledger.append(event_type, data)


def record_model_call(**kwargs: Any) -> Any:
    return _require_session().record_model_call(**kwargs)


def record_tool(name: str, **kwargs: Any) -> Any:
    return _require_session().record_tool(name, **kwargs)


def record_note(message: str, **data: Any) -> Any:
    return _require_session().record_note(message, **data)


class Session:
    """A ledger for a run you control in-process.

    ``Session`` does not intercept anything; it is the honest, explicit path for
    code you own, and it pairs with the boundary for code you do not.
    """

    def __init__(
        self,
        docket_dir: str | os.PathLike[str] = ".docket",
        *,
        session_id: str | None = None,
        label: str | None = None,
        policy: Policy | None = None,
        price_table: PriceTable | None = None,
        auto_receipt: bool = True,
        seal_key: str | None = None,
    ) -> None:
        self.docket_dir = Path(os.environ.get("DOCKET_DOCKET_DIR", docket_dir))
        self.session_id = session_id or os.environ.get("DOCKET_SESSION") or f"py{int(time.time())}"
        self.label = label
        self.policy = policy or Policy()
        self.price_table = price_table or PriceTable.load()
        self.auto_receipt = auto_receipt
        self.seal_key = seal_key
        self.ledger: Ledger | None = None
        self.recorder: _BoundaryRecorder | _LedgerRecorder | None = None
        self.attached = False
        self.meter = CostMeter(price_table=self.price_table, mode=self.policy.budget_mode)
        self.started = time.monotonic()
        self.receipt: dict[str, Any] | None = None
        self.receipt_path: Path | None = None
        self._docket_number = docket_number()

    # -- context manager --------------------------------------------------- #
    def __enter__(self) -> Session:
        # Attached to a supervised run (`docket run -- …`): write through the boundary.
        # That keeps one writer per ledger, which is what keeps the chain valid.
        proxy_url = os.environ.get("DOCKET_PROXY_URL")
        if proxy_url and os.environ.get("DOCKET_SESSION"):
            self.attached = True
            self.recorder = _BoundaryRecorder(proxy_url)
        else:
            runs = self.docket_dir / "runs"
            self.ledger = Ledger.open(runs / f"{self.session_id}.jsonl")
            self.recorder = _LedgerRecorder(self.ledger)
        self.recorder.append(
            ev.SESSION_START,
            {
                "session_id": self.session_id,
                "docket_number": self._docket_number,
                "label": self.label,
                "argv": ["<in-process>"],
                "cwd": os.getcwd(),
                "host": {"python": True},
                "instrumentation": "sdk",
                "attached_to_run": self.attached,
            },
        )
        self.recorder.append(
            ev.POLICY_APPLIED,
            {"name": self.policy.name, "digest": self.policy.digest(), "document": self.policy.to_dict()},
        )
        _state.session = self
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if not (self.recorder is not None):
                raise RuntimeError(
                    "internal invariant broken: self.recorder is not None — this is a bug in docket; "
                    "please report it with the command that reproduced it"
                )
            exit_code = 0 if exc_type is None else 1
            self.recorder.append(
                ev.SESSION_END,
                {
                    "exit_code": exit_code,
                    "duration_seconds": f"{time.monotonic() - self.started:.3f}",
                    "breached": None,
                    "interrupted": exc_type is not None,
                    "cost_usd": str(self.meter.cost),
                    "exception": f"{exc_type.__name__}: {exc}" if exc_type else None,
                },
            )
            if self.attached:
                # The supervisor owns the ledger and the receipt; this session only added
                # labelled, agent-asserted events to its chain. Building a second receipt
                # from a ledger we do not own would be wrong (and impossible: we never
                # read the file).
                return False
            if self.auto_receipt and self.ledger is not None:
                receipt = build_receipt(
                    events=list(self.ledger),
                    ledger=self.ledger,
                    meter=self.meter,
                    policy=self.policy,
                    session_id=self.session_id,
                    docket_number=self._docket_number,
                    exit_code=exit_code,
                    duration_seconds=time.monotonic() - self.started,
                    price_table=self.price_table,
                    seal_key=self.seal_key,
                    label=self.label,
                )
                path = self.docket_dir / "receipts" / f"{self._docket_number}.receipt.json"
                write_receipt(receipt, path)
                self.receipt = receipt
                self.receipt_path = path
                atomic_write(self.docket_dir / "latest", self._docket_number + "\n")
        finally:
            if self.ledger is not None:
                self.ledger.close()
            _state.session = None
        return False

    # -- recording --------------------------------------------------------- #
    def record_model_call(
        self,
        *,
        model: str,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        provider: str = "unknown",
        usage_source: str = "reported",
        latency_ms: int | None = None,
        request_digest: str | None = None,
        fingerprint: str | None = None,
        **extra: Any,
    ) -> Any:
        """Record one model call made outside the boundary (e.g. inside a notebook)."""
        if not (self.recorder is not None):
            raise RuntimeError(
                "internal invariant broken: self.recorder is not None — this is a bug in docket; "
                "please report it with the command that reproduced it"
            )
        digest = request_digest or fingerprint or digest_of({"model": model, "in": input_tokens, "out": output_tokens})
        self.recorder.append(
            ev.MODEL_REQUEST,
            {
                "provider": provider,
                "model": model,
                "fingerprint": (fingerprint or digest)[:16],
                "request_sha256": digest,
                "reported_by": "agent",
                **extra,
            },
        )
        cost = self.meter.record(model, input_tokens, cached_input_tokens, output_tokens, usage_source)
        event = self.recorder.append(
            ev.MODEL_RESPONSE,
            {
                "provider": provider,
                "model": model,
                "status": 200,
                "latency_ms": latency_ms,
                "usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_input_tokens,
                    "output_tokens": output_tokens,
                },
                "usage_source": usage_source,
                "cost_usd": usd(cost),
                "cumulative_cost_usd": usd(self.meter.cost),
                "reported_by": "agent",
            },
        )
        if self.policy.caps.max_usd is not None and self.meter.cost > self.policy.caps.max_usd:
            self.recorder.append(
                ev.CAP_EXCEEDED,
                {
                    "cap": "max_usd",
                    "cost_usd": usd(self.meter.cost),
                    "tokens": self.meter.total_tokens,
                    "calls": self.meter.calls,
                    "action": "reported-only",
                },
            )
        return event

    def record_tool(
        self,
        name: str,
        args: Any = None,
        *,
        ok: bool = True,
        result_bytes: int = 0,
        error: str | None = None,
        digest_only: bool = True,
        **extra: Any,
    ) -> Any:
        """Record a tool invocation and its outcome.

        ``digest_only`` (the default) stores a hash of the arguments rather than the
        arguments themselves, so a receipt can prove a tool was called with specific
        inputs without leaking them into a file you might share.
        """
        if not (self.recorder is not None):
            raise RuntimeError(
                "internal invariant broken: self.recorder is not None — this is a bug in docket; "
                "please report it with the command that reproduced it"
            )
        args_digest = digest_of(args) if args is not None else None
        payload: dict[str, Any] = {
            "name": name,
            "ok": ok,
            "result_bytes": result_bytes,
            "reported_by": "agent",
            **extra,
        }
        if args_digest:
            payload["args_digest"] = args_digest
            if not digest_only:
                payload["args"] = args
        if error:
            payload["error"] = error
        return self.recorder.append(ev.TOOL_CALL if ok else ev.TOOL_RESULT, payload)

    def record_note(self, message: str, **data: Any) -> Any:
        if not (self.recorder is not None):
            raise RuntimeError(
                "internal invariant broken: self.recorder is not None — this is a bug in docket; "
                "please report it with the command that reproduced it"
            )
        return self.recorder.append(ev.NOTE, {"message": message, "reported_by": "agent", **data})

    # -- introspection ----------------------------------------------------- #
    @property
    def cost(self) -> Decimal:
        return self.meter.cost

    @property
    def calls(self) -> int:
        return self.meter.calls

    @property
    def tokens(self) -> int:
        return self.meter.total_tokens

    def would_exceed(self, *, usd: Decimal | str | None = None, tokens: int | None = None) -> bool:
        """Check a prospective spend against the policy caps before spending it."""
        over_usd = (
            usd is not None
            and self.policy.caps.max_usd is not None
            and self.meter.cost + Decimal(str(usd)) > self.policy.caps.max_usd
        )
        over_tokens = (
            tokens is not None
            and self.policy.caps.max_tokens is not None
            and self.meter.total_tokens + tokens > self.policy.caps.max_tokens
        )
        return bool(over_usd or over_tokens)
