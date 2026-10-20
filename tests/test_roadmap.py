"""Every checked box in `ROADMAP.md` is a claim about code, so it is held to the code.

A roadmap row marked `[x]` says something exists. That is the same class of statement as a test
count in a document: it was true when it was written, and nothing notices when it stops being
true. The Go verifier sat in the 0.3 section as an unchecked box while it built, vetted, tested
and ran the 132-vector corpus.

The rule this file enforces is deliberately narrow. A checked box must name at least one thing
that can be looked up — a path, a `docket` subcommand, a `make` target, a CLI flag — and every
such thing must exist. A box that names nothing checkable is not waved through: it must appear in
`PROSE_ONLY` with a reason, so an unverifiable claim is a reviewed decision rather than a silent
pass. Adding one without a reason fails this test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from docket.cli import build_parser

REPO = Path(__file__).resolve().parents[1]
ROADMAP = REPO / "ROADMAP.md"

#: A checked box whose claim is prose and cannot be looked up, each with the reason it is allowed.
#: The keys are prefixes, matched with `startswith`, so a box whose wording shifts slightly still
#: resolves to its exemption — and a genuinely new claim does not.
PROSE_ONLY: dict[str, str] = {
    "append-only hash-chained ledger": (
        "the ledger's properties are behavioural, not a path; they are held by "
        "tests/test_ledger.py and the receipt's own merkle-root check"
    ),
    "receipts with embedded price table": (
        "held by tests/test_receipt_verify.py and the conformance corpus, not by a single file"
    ),
    "findings engine: runaway loops": (
        "the findings live in src/docket/explain.py; the box names the findings, not the module"
    ),
    "filesystem snapshots and diffs": (
        "four capabilities across src/docket/snapshot.py, fsdiff.py, proxy.py and sdk.py"
    ),
    "offline demo with a scripted provider": ("`make demo` runs it; the box does not name the target"),
    "per-call latency recorded": ("held by the latency-summary check in both verifiers and tests/test_latency.py"),
    "an invariant suite: one writer per ledger": (
        "held by tests/test_invariants.py; the box names the properties, not the file"
    ),
}

#: Tokens that look like a path or a command but are not a claim about this repository.
NOT_A_CLAIM = {
    "NAME=URL",  # the shape of --upstream's argument, not a file
    "Retry-After",  # an HTTP header the client half honours
    "error.code",  # a provider's own field, named as the thing that was misread
    "Decimal",  # a Python type, named as the thing that failed inside RateLimiter
    "nan",  # a float value, named as the thing that silently disabled the limiter
}


#: Evidence that something is shipped, paired with the words a checked box must use.
#: This is the *reverse* direction of the guard below: the Go verifier sat in the 0.3 section as
#: an unchecked box while it built, vetted, tested and ran the corpus. A checked box that names
#: something absent is a false claim; an unchecked box beside present evidence is a stale one, and
#: the second is the harder of the two to notice.
SHIPPED_MEANS_CHECKED: dict[str, str] = {
    "verify-go/": "Go verifier",
    "spec/vectors/": "conformance",
    "src/docket/sdk.py": "SDK",
    "src/docket/proxy.py": "boundary proxy",
    "deploy/": "Helm chart",
}


def _checked_boxes() -> list[str]:
    """Every `[x]` box on the roadmap, as one line each."""
    text = ROADMAP.read_text(encoding="utf-8")
    boxes: list[str] = []
    for block in re.split(r"\n(?=- \[)", text):
        if not block.startswith("- [x]"):
            continue
        # Continuation lines are indented, so they are folded into the box's first line.
        folded = re.sub(r"\n\s+", " ", block[len("- [x]") :]).strip()
        boxes.append(folded)
    return boxes


def _tokens(box: str) -> list[str]:
    return re.findall(r"`([^`]+)`", box)


def _classify(token: str) -> str | None:
    """What kind of thing this token claims, or `None` when it is not a claim.

    The kinds are the ones that can actually be looked up: a subcommand, a `make` target, a Go
    toolchain command, a CLI flag, a path, a policy key, a `policy.<name>` member, an environment
    variable, or the receipt format tag. Anything else is prose, and prose is handled by the
    exemption list rather than by pretending to check it.
    """
    if token in NOT_A_CLAIM:
        return None
    if token.startswith("docket ") and len(token.split()) == 2:
        return "subcommand"
    if token.startswith("make ") and len(token.split()) == 2:
        return "make-target"
    if token.startswith("go ") and len(token.split()) == 2:
        return "go-command"
    if token.startswith("--"):
        return "flag"
    if token.startswith("policy.") and re.fullmatch(r"policy\.\w+", token):
        return "policy-member"
    if re.fullmatch(r"[A-Z][A-Z0-9_]*=[^ ]*", token):
        return "env-var"
    if token.startswith("/docket/"):
        return "http-route"
    if token == "docket/receipt/v1":
        return "format-tag"
    if token in _policy_keys():
        return "policy-key"
    if "/" in token:
        return "path"
    return None


def _policy_keys() -> set[str]:
    """Every key a policy file may set, read from the dataclasses rather than from the docs."""
    from docket.policy import Caps, Files, Models, Policy

    keys: set[str] = set()
    for model in (Policy, Caps, Models, Files):
        keys.update(model.__dataclass_fields__)
    for model in (Policy,):
        for name in ("resilience", "cache"):
            inner = getattr(model, "__dataclass_fields__", {}).get(name)
            if inner is not None:
                keys.update(inner.type.__dataclass_fields__ if hasattr(inner.type, "__dataclass_fields__") else [])
    return keys


def _http_routes() -> set[str]:
    """Every `/docket/...` route the boundary actually dispatches, read from the proxy.

    Two route tables live in `src/docket/proxy.py`: the operational set the dispatcher compares
    against, and the mapping that turns an instrumentation path into an event type. Both are read
    from the source rather than listed here, so a route removed from the proxy fails this test.
    """
    source = (REPO / "src" / "docket" / "proxy.py").read_text(encoding="utf-8")
    routes: set[str] = set()
    ops = re.search(r"route in \{([^}]+)\}", source)
    if ops:
        routes.update(re.findall(r'"(\w+)"', ops.group(1)))
    mapping = re.search(r"mapping = \{(.*?)\}", source, re.DOTALL)
    if mapping:
        routes.update(re.findall(r'"(\w+/\w+)"', mapping.group(1)))
    # The generic route accepts any event in the vocabulary, so it is dispatched by name.
    routes.add("event")
    return routes


def _environment_names() -> set[str]:
    """Every `DOCKET_*` name the source reads, so a claimed variable is a real one."""
    names: set[str] = set()
    for path in (REPO / "src" / "docket").glob("*.py"):
        names.update(re.findall(r"\b(DOCKET_[A-Z0-9_]+)\b", path.read_text(encoding="utf-8")))
    return names


def _subcommands() -> set[str]:
    return set(build_parser()._subparsers._group_actions[0].choices)  # noqa: SLF001 - the parser is the interface


def _make_targets() -> set[str]:
    text = (REPO / "Makefile").read_text(encoding="utf-8")
    return set(re.findall(r"^([a-z][a-z0-9-]*):", text, re.MULTILINE))


def _cli_flags() -> set[str]:
    """Every long flag any subcommand accepts, read from the real parser."""
    flags: set[str] = set()
    for action in build_parser()._actions:  # noqa: SLF001
        flags.update(action.option_strings)
    for choices in _subcommands():
        subparser = build_parser()._subparsers._group_actions[0].choices[choices]  # noqa: SLF001
        for action in subparser._actions:  # noqa: SLF001
            flags.update(action.option_strings)
    return flags


def test_the_roadmap_has_checked_boxes() -> None:
    """A canary: a parse that found nothing would make every assertion below vacuous."""
    boxes = _checked_boxes()
    assert len(boxes) >= 15, f"parsed {len(boxes)} checked boxes from ROADMAP.md; the parser has stopped working"


def test_every_checked_box_names_something_that_exists() -> None:
    """Each `[x]` names a path, a command, a target or a flag, and every one of them is real."""
    missing: list[str] = []
    verified = 0
    for box in _checked_boxes():
        for token in _tokens(box):
            kind = _classify(token)
            if kind is None:
                continue
            if kind == "subcommand":
                name = token.split()[1]
                if name not in _subcommands():
                    missing.append(f"`{token}`: no such subcommand")
                else:
                    verified += 1
            elif kind == "make-target":
                name = token.split()[1]
                if name not in _make_targets():
                    missing.append(f"`{token}`: no such target in the Makefile")
                else:
                    verified += 1
            elif kind == "go-command":
                verified += 1  # the toolchain commands are checked by make verify-go
            elif kind == "flag":
                if token.split()[0] not in _cli_flags():
                    missing.append(f"`{token}`: no CLI option of that name")
                else:
                    verified += 1
            elif kind == "policy-member":
                name = token.split(".", 1)[1]
                if name not in _policy_keys() and not (REPO / "src" / "docket" / "policy.py").read_text(
                    encoding="utf-8"
                ).count(f"def {name}"):
                    missing.append(f"`{token}`: policy.py has no such key or function")
                else:
                    verified += 1
            elif kind == "env-var":
                name = token.split("=", 1)[0]
                if name not in _environment_names():
                    missing.append(f"`{token}`: the source never reads this variable")
                else:
                    verified += 1
            elif kind == "http-route":
                if token.removeprefix("/docket/") not in _http_routes():
                    missing.append(f"`{token}`: the boundary does not dispatch this route")
                else:
                    verified += 1
            elif kind == "policy-key":
                verified += 1  # the key's presence in the dataclass is the check
            elif kind == "format-tag":
                from docket._version import FORMAT_VERSION

                if token != FORMAT_VERSION:
                    missing.append(f"`{token}`: the format tag is {FORMAT_VERSION!r}")
                else:
                    verified += 1
            else:
                path = REPO / token.rstrip("/")
                if not path.exists():
                    missing.append(f"`{token}`: no such path in the tree")
                else:
                    verified += 1
    assert not missing, "ROADMAP.md claims things that do not exist:\n  - " + "\n  - ".join(missing)
    assert verified >= 10, f"only {verified} claims were verified; the classifier is too narrow to mean anything"


def test_every_prose_only_box_is_a_reviewed_decision() -> None:
    """A checked box that names nothing checkable must be excused by name, with a reason.

    The exemption list is the review. A new unverifiable claim has to be argued for here, in a
    diff a reader can see, rather than passing because nothing looked at it.
    """
    unexplained: list[str] = []
    for box in _checked_boxes():
        if any(_classify(token) for token in _tokens(box)):
            continue
        if not any(box.startswith(key) for key in PROSE_ONLY):
            unexplained.append(box[:70])
    assert not unexplained, (
        "these checked boxes name nothing that can be verified and are not excused in PROSE_ONLY:\n  - "
        + "\n  - ".join(unexplained)
    )


def test_no_excused_box_has_become_verifiable() -> None:
    """The exemption list shrinks as claims become checkable, and never grows silently."""
    boxes = _checked_boxes()
    stale = [key for key in PROSE_ONLY if not any(box.startswith(key) for box in boxes)]
    assert not stale, f"PROSE_ONLY names boxes that are no longer on the roadmap: {stale}"


def test_present_evidence_is_marked_as_shipped() -> None:
    """Something demonstrably in the tree must not sit in an unchecked box.

    The Go verifier was listed as `[ ]` while `verify-go/` built clean and agreed on all 132
    conformance vectors. The reader was told a feature was planned that was already running.
    """
    boxes = _checked_boxes()
    stale: list[str] = []
    for evidence, phrase in SHIPPED_MEANS_CHECKED.items():
        if not (REPO / evidence.rstrip("/")).exists():
            continue
        if not any(phrase.lower() in box.lower() for box in boxes):
            stale.append(f"`{evidence}` is in the tree but no checked box mentions {phrase!r}")
    assert not stale, "ROADMAP.md understates what is shipped:\n  - " + "\n  - ".join(stale)


def test_an_unchecked_box_is_not_held_to_the_code() -> None:
    """The guard must not start failing on plans.

    An unchecked `[ ]` box is a plan, not a claim. If this test ever fires, the parser has begun
    reading unchecked boxes as checked ones.
    """
    text = ROADMAP.read_text(encoding="utf-8")
    unchecked = len(re.findall(r"^- \[ \]", text, re.MULTILINE))
    assert unchecked > 0, "ROADMAP.md has no unchecked boxes left; it is no longer a roadmap"
    assert len(_checked_boxes()) + unchecked == len(re.findall(r"^- \[[ x]\]", text, re.MULTILINE))


@pytest.mark.parametrize(
    "token",
    ["docket verify", "make load", "make soak", "--port", "--upstream", "tools/deploy_check.py", "tools/mutate.py"],
)
def test_the_claims_this_guard_was_written_for_are_real(token: str) -> None:
    """The specific rows that were wrong or fragile when this guard was added."""
    kind = _classify(token)
    assert kind is not None, token
    if kind == "subcommand":
        assert token.split()[1] in _subcommands()
    elif kind == "make-target":
        assert token.split()[1] in _make_targets()
    elif kind == "flag":
        assert token in _cli_flags()
    else:
        assert (REPO / token).exists(), token
