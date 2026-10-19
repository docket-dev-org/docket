"""Event vocabulary for the Docket ledger.

The vocabulary is deliberately small and closed: every event type is part of the
public format (``docket/receipt/v1``), and unknown types are preserved verbatim by
verifiers rather than rejected. Implementations that add events should namespace
them (``x-vendor.something``).
"""

from __future__ import annotations

from typing import Any

# Session lifecycle
SESSION_START = "session.start"
SESSION_END = "session.end"

# Policy
POLICY_APPLIED = "policy.applied"
POLICY_VIOLATION = "policy.violation"
CAP_WARNING = "cap.warning"
CAP_EXCEEDED = "cap.exceeded"

# Model traffic (recorded by the local boundary proxy)
MODEL_REQUEST = "model.request"
MODEL_RESPONSE = "model.response"
MODEL_DENIED = "model.denied"
MODEL_ERROR = "model.error"
# A call the boundary answered from its own response cache: the provider was not
# contacted, so there is no request/response pair to record, and this event stands
# in its place. It never contributes to spend, call counts or token totals; the
# money it saved is reported beside them (see `docs/caching.md`).
MODEL_CACHE_HIT = "model.cache_hit"

# Tools and side effects
TOOL_CALL = "tool.call"
TOOL_RESULT = "tool.result"
FS_SNAPSHOT = "fs.snapshot"
FS_CHANGE = "fs.change"
PROC_SPAWN = "proc.spawn"
PROC_EXIT = "proc.exit"
NET_ALLOWED = "net.allowed"
NET_DENIED = "net.denied"

# Free-form
NOTE = "note"

KNOWN_TYPES = frozenset(
    {
        SESSION_START,
        SESSION_END,
        POLICY_APPLIED,
        POLICY_VIOLATION,
        CAP_WARNING,
        CAP_EXCEEDED,
        MODEL_REQUEST,
        MODEL_RESPONSE,
        MODEL_DENIED,
        MODEL_ERROR,
        MODEL_CACHE_HIT,
        TOOL_CALL,
        TOOL_RESULT,
        FS_SNAPSHOT,
        FS_CHANGE,
        PROC_SPAWN,
        PROC_EXIT,
        NET_ALLOWED,
        NET_DENIED,
        NOTE,
    }
)

#: Human-facing descriptions used by ``docket explain`` and the report renderer.
DESCRIPTIONS = {
    SESSION_START: "session opened",
    SESSION_END: "session closed",
    POLICY_APPLIED: "policy applied",
    POLICY_VIOLATION: "policy violation",
    CAP_WARNING: "budget threshold crossed",
    CAP_EXCEEDED: "budget cap exceeded",
    MODEL_REQUEST: "model call sent",
    MODEL_RESPONSE: "model call returned",
    MODEL_DENIED: "model call blocked by policy",
    MODEL_ERROR: "model call failed",
    MODEL_CACHE_HIT: "model call answered from cache",
    TOOL_CALL: "tool invoked",
    TOOL_RESULT: "tool returned",
    FS_SNAPSHOT: "filesystem snapshot",
    FS_CHANGE: "file changed",
    PROC_SPAWN: "process started",
    PROC_EXIT: "process exited",
    NET_ALLOWED: "network tunnel allowed",
    NET_DENIED: "network tunnel blocked",
    NOTE: "note",
}


def describe(event_type: str) -> str:
    return DESCRIPTIONS.get(event_type, event_type)


def request_fingerprint(data: dict[str, Any]) -> str:
    """Stable identity of a model request, ignoring volatile fields.

    Used by ``docket explain`` to detect loops and repeated context. Volatile
    fields (ids, timestamps, per-call nonces) are excluded so that a retry storm
    and a genuine distinct call are not confused.
    """
    import hashlib
    import json

    body = data.get("body")
    # `cleaned` is `Any` because a request body is whatever JSON the client sent: a
    # non-dict passes through unchanged rather than being reshaped into one.
    if isinstance(body, dict):
        cleaned: Any = {
            k: v
            for k, v in body.items()
            if k
            not in {
                "metadata",
                "request_id",
                "idempotency_key",
                "stream",
                "stream_options",
                "user",
            }
        }
    else:
        cleaned = body
    blob = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
