"""Policy: what an agent is allowed to spend, reach and touch.

A policy is a TOML document. Every field is optional; the defaults are safe for a
developer workstation and are printed by ``docket init``.

    [caps]
    max_usd = 5.00            # hard stop, enforced at the model boundary
    max_tokens = 2000000
    max_model_calls = 200
    max_wall_seconds = 1800
    on_breach = "kill"        # "kill" the process, or "deny" further model calls

    [models]
    allow = ["claude-*", "gpt-5*"]
    deny = ["*opus*"]

    [egress]
    mode = "deny"             # for non-model hosts in proxy mode: "deny" or "tunnel"
    allow = ["api.anthropic.com", "api.openai.com", "registry.npmjs.org"]

    [files]
    watch = ["."]
    ignore = [".git/**", "node_modules/**", "**/*.log"]
    deny_write = [".env", "**/secrets/**"]

Enforcement is deliberately bounded and Docket documents the boundary in every
receipt: caps and model allow/deny lists are enforced *before* a call leaves the
machine; egress rules are enforced for traffic that passes through Docket's
boundary; writes to ``deny_write`` paths are *detected and proven*, not prevented
(no portable mechanism exists to veto another process's syscalls from user space).
See ``docs/threat-model.md``.
"""

from __future__ import annotations

import fnmatch
import json
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .canon import digest_of, num, usd

_number = num


def _ratio(value: Any) -> float:
    """Read a warning threshold written as ``0.8``, ``"0.8"`` or ``80`` (percent)."""
    ratio = float(value)
    if ratio > 1.0:
        ratio = ratio / 100.0
    if not 0.0 < ratio <= 1.0:
        raise ValueError("caps.warn_at must be a fraction (0.8) or a percentage (80)")
    return ratio


DEFAULT_EGRESS_ALLOW = [
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
    "api.deepseek.com",
    "api.mistral.ai",
    "openrouter.ai",
    "api.groq.com",
    "localhost",
    "127.0.0.1",
]


#: The ``error.type`` the boundary stamps on every refusal it makes itself. A client telling a
#: refusal apart from a *provider's* error has to use this: the boundary relays upstream error
#: bodies verbatim — that is the honest thing to do — and providers put their own codes in
#: ``error.code`` too, so the type, not the status, is what identifies Docket as the author.
DENIAL_TYPE = "docket_policy_denied"


def denial_code(body: bytes | bytearray | str | object) -> str:
    """The code of a Docket *refusal*, or ``""`` when this payload is not one.

    One reader, two clients: the reference agent and the load driver both have to answer "was the
    boundary enforcing a limit, or did something break?", and if they answered it differently the
    same run would be described two ways — one of them wrong. Returns the code the boundary put in
    the envelope (``max_usd``, ``max_model_calls``, ``max_tokens``, ``model_policy``,
    ``rate_limited``, …), or an empty string for anything else: a relayed provider error, a
    malformed body, a payload that is not JSON at all.
    """
    if isinstance(body, bytes | bytearray):
        body = bytes(body).decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if not isinstance(error, dict) or error.get("type") != DENIAL_TYPE:
        return ""
    code = error.get("code")
    return code if isinstance(code, str) else ""


@dataclass
class Caps:
    max_usd: Decimal | None = None
    max_tokens: int | None = None
    max_model_calls: int | None = None
    max_wall_seconds: int | None = None
    warn_at: float = 0.8
    on_breach: str = "kill"  # kill | deny
    grace_seconds: float = 5.0

    def set_max_usd(self, amount: Decimal | int | str | None) -> None:
        """Validate an amount and assign it — the one sanctioned way to change this cap.

        ``max_usd`` is the guard the whole tool exists for, and a guard that can be *silently*
        switched off is worse than no guard: ``Decimal("NaN")`` compares false against every
        threshold, so a run with a NaN cap sails past every check and files a receipt that looks
        ordinary. ``__post_init__`` catches it at construction, but a caller can assign the
        field afterwards — the CLI did, from its own flag — and skip that entirely. So the
        validation lives here, and the dataclass hook below calls the same code.
        """
        self.max_usd = self._validated_usd(amount)

    @staticmethod
    def _validated_usd(amount: Decimal | int | str | None) -> Decimal | None:
        """A cap that is a real, non-negative, finite amount, or a ``ValueError`` naming it."""
        if amount is None:
            return None
        value = amount
        if not isinstance(value, Decimal):
            try:
                value = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"caps.max_usd is not a valid amount: {amount!r}") from exc
        if not value.is_finite():
            # Covers both NaN and ±Infinity: neither is a budget. `inf` reads like "no limit",
            # which is what `max_usd = None` is for, and it must be spelled that way because
            # None is disclosed in the receipt as "no cap set" while `inf` would be a number.
            raise ValueError(f"caps.max_usd must be a finite amount, not {value}")
        if value < 0:
            raise ValueError(f"caps.max_usd must be a non-negative amount, not {value}")
        return value

    def __post_init__(self) -> None:
        """Accept ``"0.05"`` as well as ``Decimal("0.05")``, and reject nonsense early.

        The dataclass is a public surface (tests, examples and the SDK construct it), and
        a string that reaches comparison with a Decimal raises deep inside a request
        thread — a crash in the boundary rather than an error at the call site. Coercing
        here makes the failure mode "clear exception where you wrote the mistake".
        """
        self.max_usd = self._validated_usd(self.max_usd)
        if self.max_tokens is not None and int(self.max_tokens) < 0:
            raise ValueError("caps.max_tokens must not be negative")
        if self.max_model_calls is not None and int(self.max_model_calls) < 0:
            raise ValueError("caps.max_model_calls must not be negative")
        if not 0.0 < float(self.warn_at) <= 1.0:
            raise ValueError(f"caps.warn_at must be in (0, 1], not {self.warn_at}")
        if float(self.grace_seconds) < 0:
            raise ValueError("caps.grace_seconds must not be negative")

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the ledger and the receipt.

        Note the number discipline demanded by the canonical form: money and any
        other fractional quantity is a decimal string, never a float, so the same
        bytes verify in Python and JavaScript alike.
        """
        return {
            "max_usd": usd(self.max_usd) if self.max_usd is not None else None,
            "max_tokens": self.max_tokens,
            "max_model_calls": self.max_model_calls,
            "max_wall_seconds": self.max_wall_seconds,
            "warn_at": _number(self.warn_at),
            "on_breach": self.on_breach,
            "grace_seconds": _number(self.grace_seconds),
        }


@dataclass
class Models:
    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)

    def decide(self, model: str) -> tuple[bool, str]:
        name = (model or "").lower()
        for pattern in self.deny:
            if fnmatch.fnmatch(name, pattern.lower()):
                return False, f"model matches deny rule {pattern!r}"
        if self.allow:
            for pattern in self.allow:
                if fnmatch.fnmatch(name, pattern.lower()):
                    return True, f"model matches allow rule {pattern!r}"
            return False, "model does not match any allow rule"
        return True, "no model allow-list configured"

    def to_dict(self) -> dict[str, Any]:
        return {"allow": self.allow, "deny": self.deny}


@dataclass
class Resilience:
    """Failure behaviour: what the boundary does when the world misbehaves.

    Every knob is opt-in, because a recorder that changes behaviour on its own is a
    recorder you cannot trust. Defaults are chosen so that the *evidence* stays honest
    and the run keeps working:

    * ``breaker_threshold`` / ``breaker_cooldown_seconds`` — stop hammering an upstream
      that is failing, and record the decision.
    * ``max_calls_per_minute`` / ``max_calls_per_second`` — a token bucket in front of
      the upstream, so a looping agent is throttled rather than merely billed.
    * ``max_events`` / ``max_ledger_bytes`` — a ceiling on the run's own record, so a
      runaway agent cannot fill a disk with its own press coverage.
    * ``fail_fast_evidence`` — ``false`` tolerates a write failure and declares the gap
      in the receipt; ``true`` stops the run instead, for CI where half a record is
      worse than none.
    * ``min_free_bytes`` — readiness reports the volume as not-ready below this floor.
    """

    breaker_threshold: int = 5
    breaker_cooldown_seconds: int = 30
    breaker_max_cooldown_seconds: int = 300
    max_calls_per_minute: int | None = None
    max_calls_per_second: Decimal | None = None
    max_events: int | None = None
    max_ledger_bytes: int | None = 256 * 1024 * 1024
    fail_fast_evidence: bool = False
    min_free_bytes: int = 16 * 1024 * 1024

    def to_dict(self) -> dict[str, Any]:
        return {
            "breaker_threshold": self.breaker_threshold,
            "breaker_cooldown_seconds": self.breaker_cooldown_seconds,
            "breaker_max_cooldown_seconds": self.breaker_max_cooldown_seconds,
            "max_calls_per_minute": self.max_calls_per_minute,
            "max_calls_per_second": _number(self.max_calls_per_second)
            if self.max_calls_per_second is not None
            else None,
            "max_events": self.max_events,
            "max_ledger_bytes": self.max_ledger_bytes,
            "fail_fast_evidence": self.fail_fast_evidence,
            "min_free_bytes": self.min_free_bytes,
        }


@dataclass
class Cache:
    """The boundary's response cache: what may be answered without the provider.

    Off by default, like every other intervention this recorder can make. The
    numbers here are policy, not tuning: they are copied into the receipt, so a
    reader can see the exact configuration under which a saving was claimed.
    ``max_hits`` is a ceiling on the cache's *own* answers rather than on spend —
    a cached loop spends nothing, so no dollar cap can stop it, and this is the
    knob that makes such a loop start paying again (``docs/caching.md``).
    """

    enabled: bool = False
    ttl_seconds: float = 300.0
    max_entries: int = 256
    max_hits: int = 1000

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ttl_seconds": num(self.ttl_seconds),
            "max_entries": self.max_entries,
            "max_hits": self.max_hits,
        }


@dataclass
class Egress:
    mode: str = "deny"  # deny | tunnel
    allow: list[str] = field(default_factory=lambda: list(DEFAULT_EGRESS_ALLOW))

    def decide(self, host: str) -> tuple[bool, str]:
        hostname = (host or "").split(":")[0].lower()
        for pattern in self.allow:
            pat = pattern.lower()
            if hostname == pat or fnmatch.fnmatch(hostname, pat):
                return True, f"host matches allow rule {pattern!r}"
        if self.mode == "tunnel":
            return True, "egress.mode = tunnel: non-model hosts are tunnelled and recorded"
        return False, "host is not in the egress allow-list"

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "allow": self.allow}


@dataclass
class Files:
    watch: list[str] = field(default_factory=lambda: ["."])
    ignore: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"watch": self.watch, "ignore": self.ignore, "deny_write": self.deny_write}


@dataclass
class Policy:
    name: str = "default"
    caps: Caps = field(default_factory=Caps)
    models: Models = field(default_factory=Models)
    egress: Egress = field(default_factory=Egress)
    files: Files = field(default_factory=Files)
    budget_mode: str = "usd"  # usd | quota
    resilience: Resilience = field(default_factory=Resilience)
    cache: Cache = field(default_factory=Cache)
    source_path: str | None = None

    # -- loading ----------------------------------------------------------- #
    @classmethod
    def load(cls, path: str | Path | None = None) -> Policy:
        if path is None:
            return cls()
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw, source_path=str(path))

    @staticmethod
    def _string_list(section: str, key: str, value: Any, default: list[str] | None = None) -> list[str]:
        """Coerce a list-typed policy key, refusing anything that is not a list of strings.

        A policy file is written by a human under time pressure. `allow = false` must say
        "egress.allow must be a list of host patterns, got bool", not raise TypeError from
        somewhere deep inside a run — and it must never quietly become an empty list,
        because an empty allow-list is a *stricter* policy than the author intended.
        """
        if value is None:
            return list(default or [])
        if isinstance(value, str):
            raise ValueError(
                f"{section}.{key} must be a list of strings, got the string {value!r} " f'(write it as ["{value}"])'
            )
        if isinstance(value, bool | int | float | dict):
            raise ValueError(f"{section}.{key} must be a list of strings, got {type(value).__name__}")
        if not isinstance(value, list | tuple):
            raise ValueError(f"{section}.{key} must be a list of strings, got {type(value).__name__}")
        out: list[str] = []
        for index, item in enumerate(value):
            if isinstance(item, dict | list | tuple) or item is None:
                raise ValueError(f"{section}.{key}[{index}] must be a string, got {type(item).__name__}")
            out.append(str(item))
        return out

    @staticmethod
    def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
        """Return a policy section, refusing a section written as a scalar.

        ``caps = 5`` at the top level of a TOML document is a typo with teeth: the old code
        took it as a dict and raised ``AttributeError`` from inside the loader. Every section is
        untrusted input, so every section gets a type check that names itself.
        """
        value = raw.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"[{name}] must be a table, got {type(value).__name__} ({value!r})")
        return value

    @staticmethod
    def _name(policy_raw: dict[str, Any], raw: dict[str, Any]) -> str:
        """Read the policy name from either shape, refusing anything that is not a string.

        There are two shapes in the wild and they must agree on identity, because
        ``digest()`` is computed over ``to_dict()`` and that digest is what a receipt
        records:

        * the TOML document people write — ``[policy] name = "prod"`` (``Policy.load``);
        * the serialised form — a top-level ``"name"`` (``to_dict()``), which is what the
          SDK writes into the ledger, so a policy can be reconstructed from its own record.

        Previously ``from_dict`` only looked inside ``[policy]``, so reconstructing a policy
        from ``to_dict()`` silently renamed it to ``"default"`` and changed its digest — the
        reconstructed policy no longer matched the digest recorded beside it. A name given
        twice *differently* is refused rather than resolved by precedence: an identity field
        is the worst place to guess.
        """
        nested = policy_raw.get("name")
        top = raw.get("name")
        for where, value in (("policy.name", nested), ("name", top)):
            if value is None or isinstance(value, str):
                continue
            raise ValueError(f"{where} must be a string, got {value!r} ({type(value).__name__})")
        if nested and top and str(nested) != str(top):
            raise ValueError(f"the policy name is given twice and differently: policy.name={nested!r}, name={top!r}")
        # Presence, not truthiness: an explicitly empty name is a mistake to report, not a
        # silent fall back to the default (which would name the policy something nobody chose).
        chosen = nested if nested is not None else top
        if chosen is None:
            return "default"
        name = chosen.strip()
        if not name:
            raise ValueError("policy.name must not be empty")
        return name

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source_path: str | None = None) -> Policy:
        if not isinstance(raw, dict):
            raise ValueError(f"a policy document must be a table, got {type(raw).__name__}")
        caps_raw = cls._section(raw, "caps")
        models_raw = cls._section(raw, "models")
        egress_raw = cls._section(raw, "egress")
        files_raw = cls._section(raw, "files")
        budget_raw = cls._section(raw, "budget")
        resilience_raw = cls._section(raw, "resilience")
        policy_raw = cls._section(raw, "policy")

        def _amount(key: str) -> Decimal | None:
            # ``_amount_key`` refuses booleans, infinities (``1e999`` in TOML parses to ``inf``,
            # which a naive coercion accepts as a cap that can never be breached) and negatives
            # that cannot be amounts, naming ``caps.<key>`` and the offending value.
            return _amount_key("caps", key, caps_raw.get(key), None)

        def _whole(key: str) -> int | None:
            return _whole_key("caps", key, caps_raw.get(key), None)

        try:
            warn_at = _ratio(caps_raw.get("warn_at", 0.8))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"caps.warn_at must be a fraction or percentage: {caps_raw.get('warn_at')!r}") from exc

        try:
            grace_seconds = _finite_number("caps", "grace_seconds", caps_raw.get("grace_seconds", 5.0))
        except ValueError as exc:
            raise ValueError(
                f"caps.grace_seconds is not a number of seconds: {caps_raw.get('grace_seconds')!r}"
            ) from exc

        caps = Caps(
            max_usd=_amount("max_usd"),
            max_tokens=_whole("max_tokens"),
            max_model_calls=_whole("max_model_calls"),
            max_wall_seconds=_whole("max_wall_seconds"),
            # tolerate an 80 value meaning 80% as well as 0.8 meaning 0.8, and the
            # decimal strings the serialised form uses
            warn_at=warn_at,
            on_breach=str(caps_raw.get("on_breach", "kill")),
            grace_seconds=grace_seconds,
        )
        if caps.on_breach not in {"kill", "deny"}:
            raise ValueError("caps.on_breach must be 'kill' or 'deny'")
        if caps.max_usd is not None and caps.max_usd < 0:
            raise ValueError("caps.max_usd must not be negative")

        egress = Egress(
            mode=str(egress_raw.get("mode", "deny")),
            allow=cls._string_list("egress", "allow", egress_raw.get("allow"), DEFAULT_EGRESS_ALLOW),
        )
        if egress.mode not in {"deny", "tunnel"}:
            raise ValueError("egress.mode must be 'deny' or 'tunnel'")

        resilience = _resilience_from(resilience_raw)

        policy = cls(
            name=cls._name(policy_raw, raw),
            caps=caps,
            resilience=resilience,
            models=Models(
                allow=cls._string_list("models", "allow", models_raw.get("allow")),
                deny=cls._string_list("models", "deny", models_raw.get("deny")),
            ),
            egress=egress,
            files=Files(
                watch=cls._string_list("files", "watch", files_raw.get("watch"), ["."]),
                ignore=cls._string_list("files", "ignore", files_raw.get("ignore")),
                deny_write=cls._string_list("files", "deny_write", files_raw.get("deny_write")),
            ),
            budget_mode=str(budget_raw.get("mode", "usd")),
            cache=_cache_from(cls._section(raw, "cache")),
            source_path=source_path,
        )
        if policy.budget_mode not in {"usd", "quota"}:
            raise ValueError("budget.mode must be 'usd' or 'quota'")
        return policy

    # -- introspection ------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "name": self.name,
            "caps": self.caps.to_dict(),
            "models": self.models.to_dict(),
            "egress": self.egress.to_dict(),
            "files": self.files.to_dict(),
            "budget": {"mode": self.budget_mode},
            "resilience": self.resilience.to_dict(),
        }
        # The cache appears only when it is on. A policy that says nothing about
        # caching *is* the policy it always was, and it must keep the same digest:
        # receipts and signatures written by an earlier version name that digest,
        # and a digest that moves when a feature is added turns every one of them
        # into a document nobody can re-derive.
        if self.cache.enabled:
            document["cache"] = self.cache.to_dict()
        return document

    def digest(self) -> str:
        return digest_of(self.to_dict())

    def to_toml(self) -> str:
        """Render the policy as a commented TOML document (used by ``docket init``)."""
        lines = [
            f"# Docket policy — {self.name}",
            "# https://github.com/shivanshjain456/docket/blob/main/docs/policy-reference.md",
            "",
            "[policy]",
            f'name = "{self.name}"',
            "",
            "[budget]",
            f'mode = "{self.budget_mode}"   # "usd" for metered APIs, "quota" for subscriptions',
            "",
            "[caps]",
        ]
        cap_lines = []
        if self.caps.max_usd is not None:
            cap_lines.append(f"max_usd = {self.caps.max_usd}")
        if self.caps.max_tokens is not None:
            cap_lines.append(f"max_tokens = {self.caps.max_tokens}")
        if self.caps.max_model_calls is not None:
            cap_lines.append(f"max_model_calls = {self.caps.max_model_calls}")
        if self.caps.max_wall_seconds is not None:
            cap_lines.append(f"max_wall_seconds = {self.caps.max_wall_seconds}")
        lines += cap_lines or ["# max_usd = 5.00", "# max_tokens = 2000000", "# max_model_calls = 200"]
        lines += [
            f"warn_at = {self.caps.warn_at}",
            f'on_breach = "{self.caps.on_breach}"',
            f"grace_seconds = {self.caps.grace_seconds}",
            "",
            "[models]",
            f"allow = {self.models.allow!r}",
            f"deny = {self.models.deny!r}",
            "",
            "[egress]",
            f'mode = "{self.egress.mode}"',
            f"allow = {self.egress.allow!r}",
            "",
            "[files]",
            f"watch = {self.files.watch!r}",
            f"ignore = {self.files.ignore!r}",
            f"deny_write = {self.files.deny_write!r}",
            "",
            "# Failure behaviour. Every key below is off/quiet by default: a recorder that",
            "# changes how your agent behaves without being asked is a recorder you cannot",
            "# trust. See docs/resilience.md before turning any of them on.",
            "[resilience]",
            f"breaker_threshold = {self.resilience.breaker_threshold}          # consecutive upstream failures before refusing to send",
            f"breaker_cooldown_seconds = {self.resilience.breaker_cooldown_seconds}     # first cooldown; doubles on a failed probe",
            f"breaker_max_cooldown_seconds = {self.resilience.breaker_max_cooldown_seconds}  # ceiling for the doubling",
            "# max_calls_per_minute = 120      # sliding-window throttle in front of the upstream",
            "# max_calls_per_second = 5        # token bucket; burst = one second of steady traffic",
            "# max_events = 200000             # ceiling on this run's own record",
            f"max_ledger_bytes = {self.resilience.max_ledger_bytes or 0}  # ceiling on the ledger file in bytes; 0 = no ceiling",
            f"fail_fast_evidence = {str(self.resilience.fail_fast_evidence).lower()}      # true: stop the run if evidence cannot be written",
            f"min_free_bytes = {self.resilience.min_free_bytes}    # readiness reports not-ready below this floor",
            "",
            "# Response cache. Off by default: a recorder that changes what your agent",
            "# receives without being asked is not a recorder you can trust. When it is on,",
            "# every call it answers is a `model.cache_hit` event in the ledger and the saving",
            "# is recomputed by `docket verify` — see docs/caching.md.",
            "[cache]",
            f"enabled = {str(self.cache.enabled).lower()}            # answer an identical request from the cache instead of the provider",
            f"ttl_seconds = {self.cache.ttl_seconds}       # 0 = entries never expire on time (capacity still bounds them)",
            f"max_entries = {self.cache.max_entries}         # least-recently-used entries are evicted above this",
            f"max_hits = {self.cache.max_hits}          # 0 = no ceiling; a cached loop stops paying, so it needs its own brake",
            "",
        ]
        return "\n".join(lines)

    def limits_disclosure(self) -> dict[str, Any]:
        """What this configuration can and cannot observe or prevent.

        Copied verbatim into every receipt so that a reader never has to guess the
        scope of the evidence they are looking at.
        """
        return {
            "enforced_before_send": [
                "per-session USD ceiling (where usage is reported or estimated)",
                "per-session token ceiling",
                "per-session model-call ceiling",
                "model allow/deny rules",
                "wall-clock ceiling (by terminating the supervised process)",
                "call-rate ceiling, when resilience.max_calls_per_minute or max_calls_per_second is set",
                "requests to an upstream whose circuit breaker is open",
                "any further model call once the ledger has reached its configured limit",
            ],
            "cache_behaviour": (
                [
                    "when cache.enabled is set, an identical request (same upstream path and same",
                    "canonical body) is answered from the response cache instead of the provider:",
                    "the provider never sees it, and it is recorded as model.cache_hit",
                    "the saving is reported beside the spend, never inside it",
                ]
                if self.cache.enabled
                else ["no response cache is configured: every request is forwarded"]
            ),
            "recorded_not_enforced": [
                "filesystem changes under watched paths (detected by snapshot diffing)",
                "writes to deny_write paths (proven after the fact, flagged as violations)",
                "process spawn/exit of the supervised child only, not its descendants' syscalls",
            ],
            "not_observed": [
                "traffic that does not pass through the Docket boundary (e.g. an agent "
                "configured with a direct provider URL, or HTTPS tunnels when egress.mode = tunnel)",
                "prompts, completions or file contents unless --record-bodies is enabled",
                "actions taken by processes outside the supervised process tree",
            ],
            "failure_behaviour": [
                "upstream failures are counted per provider; a circuit breaker stops sending "
                "after the configured threshold and records every state change",
                "when the ledger cannot be written, the run continues by default and the "
                "receipt declares the gap (resilience.fail_fast_evidence = true stops it instead)",
                "a torn final line in a ledger is reported, never silently repaired; "
                "`docket doctor --quarantine-torn-tail` moves the bytes aside and keeps them",
            ],
            "trust_assumptions": [
                "the operator ran Docket honestly; the chain proves what was written, not that nothing else happened",
                "reported token usage is the provider's own count; estimated usage is marked as estimated",
            ],
        }


def DEFAULT_TOML(name: str = "default", max_usd: str = "5.00") -> str:
    policy = Policy(name=name, caps=Caps(max_usd=Decimal(max_usd)))
    return policy.to_toml()


def _finite_number(section: str, key: str, value: Any) -> float:
    """A finite number from a policy document, or a ``ValueError`` naming it.

    Two failure modes are worth naming rather than defaulting. TOML parses ``1e999`` to
    ``inf`` (and ``nan`` is reachable through arithmetic on it), which ``int()`` then refuses
    with an unhelpful ``OverflowError``; and ``true`` is an ``int`` in Python, so a boolean
    would otherwise sail through a numeric field. Both are refused by name.
    """
    where = f"{section}.{key}"
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{where} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} must be a number, got {value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{where} must be a finite number, got {value!r}")
    return number


def _whole_key(section: str, key: str, value: Any, default: int | None) -> int | None:
    """A whole number from a policy document, or the default when the key is absent."""
    if value is None:
        return default
    number = _finite_number(section, key, value)
    if number != int(number):
        raise ValueError(f"{section}.{key} must be a whole number, got {value!r}")
    return int(number)


def _amount_key(section: str, key: str, value: Any, default: Decimal | None) -> Decimal | None:
    if value is None:
        return default
    number = _finite_number(section, key, value)
    if number < 0:
        raise ValueError(f"{section}.{key} must not be negative, got {value!r}")
    return Decimal(str(value).strip())


def _bool_key(section: str, key: str, value: Any, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{section}.{key} must be true or false, got {value!r}")
    return value


def _cache_from(raw: dict[str, Any]) -> Cache:
    """Parse ``[cache]``, refusing anything that would make the cache dishonest.

    A negative TTL, a fractional entry ceiling or a hit cap that is not a number is
    a configuration the boundary cannot honour, and it is refused at load time with
    the key named — the same rule as every other policy key: a bad value must never
    become a 502 in the middle of a run.
    """
    # A `[cache]` section that names any tuning key is an operator asking for a cache: on
    # its own, `ttl_seconds` configures a cache that never answers, which is a policy
    # nobody means. `enabled = false` written beside other keys is the one way to say
    # "configured, but off" — and it wins, because it is the explicit statement.
    other_keys = sorted(set(raw) - {"enabled"})
    enabled = _bool_key("cache", "enabled", raw.get("enabled"), bool(other_keys))
    raw_ttl = raw.get("ttl_seconds")
    ttl = 300.0 if raw_ttl is None else _finite_number("cache", "ttl_seconds", raw_ttl)
    if ttl < 0:
        raise ValueError(f"cache.ttl_seconds must be zero (never expire) or positive, got {raw_ttl!r}")
    max_entries = _whole_key("cache", "max_entries", raw.get("max_entries"), 256)
    max_hits = _whole_key("cache", "max_hits", raw.get("max_hits"), 1000)
    if max_entries is None or max_entries < 1:
        raise ValueError(f"cache.max_entries must be at least 1, got {raw.get('max_entries')!r}")
    if max_hits is None or max_hits < 0:
        raise ValueError(f"cache.max_hits must be zero (no ceiling) or positive, got {raw.get('max_hits')!r}")
    return Cache(enabled=enabled, ttl_seconds=ttl, max_entries=int(max_entries), max_hits=int(max_hits))


def _resilience_from(raw: dict[str, Any]) -> Resilience:
    """Build the resilience section, rejecting values that cannot mean anything.

    Silence is the failure mode being designed against here: a policy knob that is
    ignored because of a typo is worse than one that fails loudly, so unknown keys and
    impossible numbers raise rather than defaulting.
    """
    known = set(Resilience.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(
            f"unknown key(s) in [resilience]: {', '.join(unknown)}. Known keys: {', '.join(sorted(known))}"
        )
    defaults = Resilience()
    resilience = Resilience(
        breaker_threshold=_whole_key(
            "resilience", "breaker_threshold", raw.get("breaker_threshold"), defaults.breaker_threshold
        ),
        breaker_cooldown_seconds=_whole_key(
            "resilience",
            "breaker_cooldown_seconds",
            raw.get("breaker_cooldown_seconds"),
            defaults.breaker_cooldown_seconds,
        ),
        breaker_max_cooldown_seconds=_whole_key(
            "resilience",
            "breaker_max_cooldown_seconds",
            raw.get("breaker_max_cooldown_seconds"),
            defaults.breaker_max_cooldown_seconds,
        ),
        max_calls_per_minute=_whole_key("resilience", "max_calls_per_minute", raw.get("max_calls_per_minute"), None),
        max_calls_per_second=_amount_key("resilience", "max_calls_per_second", raw.get("max_calls_per_second"), None),
        max_events=_whole_key("resilience", "max_events", raw.get("max_events"), None),
        # Absent means the documented default, like every other key in this section.
        # It used to mean `None` — no byte ceiling at all — which is a *weaker* policy
        # than the one `docs/resilience.md` and the deployment manifests describe, and a
        # hand-written policy file got it by saying nothing. `0` is how an operator asks
        # for no ceiling on purpose, and it is stored as `None` because that is what
        # `Ledger.open` understands.
        max_ledger_bytes=_whole_key(
            "resilience", "max_ledger_bytes", raw.get("max_ledger_bytes"), defaults.max_ledger_bytes
        ),
        fail_fast_evidence=_bool_key(
            "resilience", "fail_fast_evidence", raw.get("fail_fast_evidence"), defaults.fail_fast_evidence
        ),
        min_free_bytes=_whole_key("resilience", "min_free_bytes", raw.get("min_free_bytes"), defaults.min_free_bytes),
    )
    if resilience.breaker_threshold < 1:
        raise ValueError("resilience.breaker_threshold must be at least 1")
    if resilience.breaker_cooldown_seconds < 0:
        raise ValueError("resilience.breaker_cooldown_seconds must not be negative")
    if resilience.max_calls_per_minute is not None and resilience.max_calls_per_minute < 1:
        raise ValueError("resilience.max_calls_per_minute must be at least 1")
    if resilience.max_calls_per_second is not None and resilience.max_calls_per_second <= 0:
        raise ValueError("resilience.max_calls_per_second must be positive")
    if resilience.max_events is not None and resilience.max_events < 1:
        raise ValueError("resilience.max_events must be at least 1")
    if resilience.max_ledger_bytes == 0:
        # Deliberate and explicit: no byte ceiling. The disk is then the only bound, and
        # readiness still reports on free space (resilience.min_free_bytes).
        resilience.max_ledger_bytes = None
    if resilience.max_ledger_bytes is not None and resilience.max_ledger_bytes < 4096:
        raise ValueError(
            "resilience.max_ledger_bytes must be 0 (no ceiling) or at least 4096 "
            "(below that the ledger cannot hold a run)"
        )
    if resilience.min_free_bytes < 0:
        raise ValueError("resilience.min_free_bytes must not be negative")
    return resilience
