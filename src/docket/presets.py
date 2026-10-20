"""Framework presets for `docket init --from <preset>`.

`docket init` writes a policy, and a policy is only useful if it names the models the agent will
actually call. A generic `docket.toml` with an empty `models.allow` and a $5 cap is technically
correct and practically useless: the reader has to know which family their agent uses, what a
sensible cap is for a coding session, and — the part that actually breaks — which environment
variable their agent honours. Get that last one wrong and the agent talks to the provider directly,
Docket records nothing, and the receipt that comes out is a receipt about nothing.

So each preset carries three things:

1. a policy whose `models.allow` names real patterns from `src/docket/prices.json`;
2. the environment variable the framework actually reads, which is not the same for all of them;
3. the exact command that attaches the agent to the boundary.

The variable names are taken from each framework's own documentation, and the ones that differ
from what Docket injects by default are called out in each preset's notes. `gemini-cli` is the clearest
case: it reads `GOOGLE_GEMINI_BASE_URL`, which is not what a generic OpenAI-shaped client reads, so
the supervisor sets both.

Every preset here is exercised by `tests/test_presets.py`, which writes the policy, loads it back
through the real parser, and runs a supervised agent against the scripted provider. A preset that
produces a policy the parser refuses is a preset that cannot ship.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .policy import DEFAULT_EGRESS_ALLOW, Caps, Egress, Files, Models, Policy


@dataclass(frozen=True)
class Preset:
    """One framework's worth of starting configuration."""

    key: str
    title: str
    #: One line for the listing, short enough to align in a terminal.
    blurb: str
    summary: str
    #: Model patterns this framework's agents call, matched against `src/docket/prices.json`.
    model_patterns: tuple[str, ...]
    #: The command that runs the agent. `{policy}` is substituted with the policy path.
    command: str
    #: Environment variables the framework reads, as (name, what it should point at).
    attach: tuple[tuple[str, str], ...]
    #: What the reader has to know that is not in the command.
    notes: tuple[str, ...] = ()
    #: Cap overrides, when the framework's own defaults are not a sensible starting point.
    max_usd: str = "10.00"
    max_tokens: int | None = 4_000_000
    max_model_calls: int | None = 400
    #: Extra hosts the framework needs beyond the provider's own API.
    extra_egress: tuple[str, ...] = ()
    #: Paths a coding agent should not have its writes recorded against.
    ignore_paths: tuple[str, ...] = field(default_factory=tuple)


PRESETS: tuple[Preset, ...] = (
    Preset(
        key="claude-code",
        blurb="Anthropic's coding agent",
        title="Claude Code",
        summary=(
            "Anthropic's coding agent. It reads `ANTHROPIC_BASE_URL` at process start, which is the "
            "variable Docket injects, so `docket run -- claude` attaches with nothing else to set."
        ),
        model_patterns=("*opus*", "*sonnet*", "*haiku*"),
        command="claude",
        attach=(("ANTHROPIC_BASE_URL", "the boundary's `/anthropic` path"),),
        notes=(
            "Claude Code reads the variable once, when the process starts. A session that was "
            "already running keeps the endpoint it was launched with.",
            "Pointing `ANTHROPIC_BASE_URL` at a host that is not Anthropic's own marks the session "
            "as non-first-party, which disables MCP tool search unless `ENABLE_TOOL_SEARCH=true`.",
        ),
        max_usd="15.00",
        max_model_calls=600,
    ),
    Preset(
        key="codex",
        blurb="OpenAI's coding agent",
        title="OpenAI Codex CLI",
        summary=(
            "OpenAI's coding agent. It reads `OPENAI_BASE_URL`, and it also has a top-level "
            "`openai_base_url` key in `~/.codex/config.toml` that takes a different route to the "
            "same place."
        ),
        model_patterns=("gpt-5*", "gpt-6*", "o3*", "o4*"),
        command="codex",
        attach=(("OPENAI_BASE_URL", "the boundary's `/openai/v1` path"),),
        notes=(
            "A project-local `.codex/config.toml` cannot set `openai_base_url`: Codex ignores "
            "provider keys there and warns at startup. Put it in the user-level config.",
            "If you would rather declare a provider than change the built-in one, use "
            "`[model_providers.<id>]` with `base_url` and `env_key`.",
        ),
        max_usd="15.00",
        max_model_calls=600,
    ),
    Preset(
        key="gemini-cli",
        blurb="Google's coding agent",
        title="Gemini CLI",
        summary=(
            "Google's coding agent. It reads `GOOGLE_GEMINI_BASE_URL`, which is not the variable a "
            "generic OpenAI-shaped client reads, so Docket sets both."
        ),
        model_patterns=("gemini-*-pro*", "gemini-*-flash*"),
        command="gemini",
        attach=(("GOOGLE_GEMINI_BASE_URL", "the boundary's `/gemini` path"),),
        notes=(
            "Gemini CLI enforces HTTPS on a non-local base URL, so the boundary's own loopback "
            "address is what it accepts.",
            "A session signed in with a Google account rather than an API key does not use a custom base URL at all.",
        ),
        max_usd="10.00",
        max_model_calls=500,
    ),
    Preset(
        key="aider",
        blurb="The pair programmer, multi-provider",
        title="aider",
        summary=(
            "The pair programmer. It reads `OPENAI_API_BASE`, and `--openai-api-base` sets the same "
            "thing for one invocation. Aider is multi-provider, so the model name carries the route."
        ),
        model_patterns=("gpt-5*", "gpt-6*", "*sonnet*", "deepseek-*", "*opus*"),
        command="aider --model openai/<model-name>",
        attach=(("OPENAI_API_BASE", "the boundary's `/openai/v1` path"),),
        notes=(
            "Aider prefixes a model with `openai/` to route it through its OpenAI-compatible "
            "client, and strips the prefix before sending it upstream.",
            "The base URL stops at `/v1`. One path segment further and the request 404s.",
        ),
        max_usd="10.00",
        max_model_calls=500,
    ),
    Preset(
        key="opencode",
        blurb="The terminal agent; needs a config block",
        title="opencode",
        summary=(
            "The terminal agent. It does not read `OPENAI_BASE_URL` at all: a provider has to be "
            "declared in `opencode.json` with its own `baseURL`, so this preset prints the block "
            "rather than writing it over a config file that is yours."
        ),
        model_patterns=("*sonnet*", "*opus*", "gpt-5*", "gpt-6*", "gemini-*-pro*"),
        command="opencode",
        attach=(),
        notes=(
            'Add a provider block to `opencode.json` with `"baseURL"` pointing at the boundary '
            'and `"npm"` set to `@ai-sdk/openai-compatible` for a Chat Completions endpoint.',
            "Match the adapter to the protocol: `@ai-sdk/openai` for `/v1/responses`.",
        ),
        max_usd="10.00",
        max_model_calls=500,
    ),
    Preset(
        key="http-proxy",
        blurb="Your own agent, any language",
        title="Any HTTP client",
        summary=(
            "Your own agent, in any language, talking to the boundary directly. Point the client's "
            "base URL at the boundary and post the events the SDK documents."
        ),
        model_patterns=("*",),
        command="your-agent",
        attach=(("DOCKET_PROXY_URL", "the boundary's root"),),
        notes=(
            "`DOCKET_PROXY_URL` is injected by `docket run`, and the four `/docket/*` recording "
            "routes are documented in `docs/integrations.md`.",
            'An event the agent asserts itself is recorded with `reported_by: "agent"`, which is '
            "what keeps a boundary observation distinguishable from a claim about one.",
        ),
        max_usd="5.00",
        max_tokens=1_000_000,
        max_model_calls=200,
    ),
    Preset(
        key="generic",
        blurb="No framework in particular",
        title="No framework",
        summary="The same policy `docket init` has always written, for an agent Docket does not know about.",
        model_patterns=(),
        command="your-agent",
        attach=(("DOCKET_PROXY_URL", "the boundary's root"),),
        max_usd="5.00",
        max_tokens=2_000_000,
        max_model_calls=200,
    ),
)

PRESET_KEYS: tuple[str, ...] = tuple(preset.key for preset in PRESETS)


def preset(key: str) -> Preset | None:
    """The preset named `key`, or `None` when there is no such preset."""
    for candidate in PRESETS:
        if candidate.key == key:
            return candidate
    return None


def policy_for(chosen: Preset, name: str) -> Policy:
    """The policy a preset writes, as a `Policy` rather than as text.

    Built through the dataclasses rather than by string concatenation, so a preset cannot produce
    TOML the parser would refuse: `to_toml` is the same code path that reads it back.
    """
    return Policy(
        name=name,
        caps=Caps(
            max_usd=Decimal(chosen.max_usd),
            max_tokens=chosen.max_tokens,
            max_model_calls=chosen.max_model_calls,
        ),
        models=Models(allow=list(chosen.model_patterns), deny=[]),
        # `deny` with the default allow-list is the strict posture: a host the policy does
        # not name is refused. `extra_egress` adds to that list rather than replacing it, so a
        # preset can never be looser than the default.
        egress=Egress(mode="deny", allow=[*DEFAULT_EGRESS_ALLOW, *chosen.extra_egress]),
        files=Files(watch=["."], ignore=list(chosen.ignore_paths), deny_write=[]),
    )


def describe_presets() -> str:
    """The listing `docket init --from` prints when it is given no value."""
    key_width = max(len(candidate.key) for candidate in PRESETS)
    return "\n".join(f"  {candidate.key:<{key_width}}  {candidate.blurb}" for candidate in PRESETS)


def next_steps(chosen: Preset, policy_path: str) -> list[str]:
    """The commands a reader runs after `docket init --from <preset>`, in order."""
    steps = [f"docket run --policy {policy_path} -- {chosen.command}"]
    if chosen.key == "opencode":
        steps.append("# then add a provider block to opencode.json — see the notes above")
    return steps
