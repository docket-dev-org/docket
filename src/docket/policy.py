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
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
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


@dataclass
class Caps:
    max_usd: Decimal | None = None
    max_tokens: int | None = None
    max_model_calls: int | None = None
    max_wall_seconds: int | None = None
    warn_at: float = 0.8
    on_breach: str = "kill"  # kill | deny
    grace_seconds: float = 5.0

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
    source_path: str | None = None

    # -- loading ----------------------------------------------------------- #
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Policy":
        if path is None:
            return cls()
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw, source_path=str(path))

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source_path: str | None = None) -> "Policy":
        caps_raw = raw.get("caps", {}) or {}
        models_raw = raw.get("models", {}) or {}
        egress_raw = raw.get("egress", {}) or {}
        files_raw = raw.get("files", {}) or {}
        budget_raw = raw.get("budget", {}) or {}
        policy_raw = raw.get("policy", {}) or {}

        caps = Caps(
            max_usd=Decimal(str(caps_raw["max_usd"])) if caps_raw.get("max_usd") is not None else None,
            max_tokens=int(caps_raw["max_tokens"]) if caps_raw.get("max_tokens") is not None else None,
            max_model_calls=int(caps_raw["max_model_calls"]) if caps_raw.get("max_model_calls") is not None else None,
            max_wall_seconds=int(caps_raw["max_wall_seconds"]) if caps_raw.get("max_wall_seconds") is not None else None,
            # tolerate an 80 value meaning 80% as well as 0.8 meaning 0.8, and the
            # decimal strings the serialised form uses
            warn_at=_ratio(caps_raw.get("warn_at", 0.8)),
            on_breach=str(caps_raw.get("on_breach", "kill")),
            grace_seconds=float(caps_raw.get("grace_seconds", 5.0)),
        )
        if caps.on_breach not in {"kill", "deny"}:
            raise ValueError("caps.on_breach must be 'kill' or 'deny'")
        if caps.max_usd is not None and caps.max_usd < 0:
            raise ValueError("caps.max_usd must not be negative")

        egress = Egress(
            mode=str(egress_raw.get("mode", "deny")),
            allow=[str(h) for h in egress_raw.get("allow", DEFAULT_EGRESS_ALLOW)],
        )
        if egress.mode not in {"deny", "tunnel"}:
            raise ValueError("egress.mode must be 'deny' or 'tunnel'")

        policy = cls(
            name=str(policy_raw.get("name", "default")),
            caps=caps,
            models=Models(
                allow=[str(p) for p in models_raw.get("allow", [])],
                deny=[str(p) for p in models_raw.get("deny", [])],
            ),
            egress=egress,
            files=Files(
                watch=[str(p) for p in files_raw.get("watch", ["."])],
                ignore=[str(p) for p in files_raw.get("ignore", [])],
                deny_write=[str(p) for p in files_raw.get("deny_write", [])],
            ),
            budget_mode=str(budget_raw.get("mode", "usd")),
            source_path=source_path,
        )
        if policy.budget_mode not in {"usd", "quota"}:
            raise ValueError("budget.mode must be 'usd' or 'quota'")
        return policy

    # -- introspection ------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "caps": self.caps.to_dict(),
            "models": self.models.to_dict(),
            "egress": self.egress.to_dict(),
            "files": self.files.to_dict(),
            "budget": {"mode": self.budget_mode},
        }

    def digest(self) -> str:
        return digest_of(self.to_dict())

    def to_toml(self) -> str:
        """Render the policy as a commented TOML document (used by ``docket init``)."""
        lines = [
            f'# Docket policy — {self.name}',
            "# https://github.com/docket-dev/docket/blob/main/docs/policy-reference.md",
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
            f'warn_at = {self.caps.warn_at}',
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
            ],
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
            "trust_assumptions": [
                "the operator ran Docket honestly; the chain proves what was written, not that nothing else happened",
                "reported token usage is the provider's own count; estimated usage is marked as estimated",
            ],
        }


def DEFAULT_TOML(name: str = "default", max_usd: str = "5.00") -> str:
    policy = Policy(name=name, caps=Caps(max_usd=Decimal(max_usd)))
    return policy.to_toml()
