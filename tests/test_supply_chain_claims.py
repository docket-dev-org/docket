"""A supply-chain claim in a document has to be backed by a step that performs it.

`SECURITY.md` said releases are attached "with a signed `SHA256SUMS`", that the repository is
"protected with required reviews and signed commits", and `MAINTAINERS.md` and `GOVERNANCE.md`
repeated the signing claim. Measured before any of it was changed:

- `.github/workflows/release.yml` builds the file with `cd dist && sha256sum * > SHA256SUMS`. A
  checksum list, not a signature. No key, no detached signature, no attestation.
- No `gpg`, `cosign`, `minisign`, `sigstore` or `gh attestation` invocation exists in `.github/`,
  `tools/`, `Makefile` or `pyproject.toml`.
- Zero of the first fifty commits verify as GPG-signed, `user.signingkey` is unset, and no workflow
  signs anything.
- `main`'s protection sets `required_approving_review_count` to `0`, so the review requirement
  enforces no review. The nine required status checks and the force-push block are real, and were
  observed to reject a force-push.

The text now says what is true and points at what Docket genuinely signs: receipts, with Ed25519,
through `docket keygen`, `docket run --sign` and `docket verify`. Those three were executed, and
`docket verify` reported `PASS signature: Ed25519 signature valid` on a receipt produced by a real
run against a real loopback upstream.

This guard exists so the claim cannot drift back the way it drifted in. It checks two directions:

1. No document may call a release artifact signed unless the pipeline contains a step that signs it.
2. The commands the supply-chain section tells a reader to run have to exist on the real CLI surface,
   with the real flags, so a rename or a removal fails the build rather than leaving prose describing
   a command that no longer runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

DOCUMENTS = sorted(REPO.glob("*.md")) + sorted((REPO / "docs").glob("*.md"))
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))

# Files a signature could come from. If none of these appears in the pipeline, a claim that an
# artifact is signed is a claim nothing performs.
SIGNING_STEPS = ("gpg", "cosign", "minisign", "sigstore", "gh attestation", "notary")


def pipeline_text() -> str:
    parts = [p.read_text(encoding="utf-8") for p in WORKFLOWS]
    parts += [
        (REPO / "Makefile").read_text(encoding="utf-8"),
        (REPO / "pyproject.toml").read_text(encoding="utf-8"),
    ]
    for extra in (REPO / "tools").glob("*.py"):
        parts.append(extra.read_text(encoding="utf-8"))
    return "\n".join(parts).lower()


def prose(line: str) -> str:
    """A line with its quoted spans removed.

    `docs/testing.md` quotes the claims this guard condemns -- `"with a signed `SHA256SUMS`"` -- in
    order to record what was wrong. An assertion and a quotation of an assertion are different things,
    and the difference has to survive the check or the guard flags its own documentation. Markdown's
    quoting convention in this repository is the double quote, so those spans are dropped.

    This only ever removes text, so a claim written without quotes is still tested.
    """
    return re.sub(r'"[^"]*"', " ", line)


def test_no_document_claims_a_release_artifact_is_signed() -> None:
    """A signature claim needs a signing step; there is none, so no document may make it.

    The claim is matched with `\\bsigned\\b`, not with a substring search, and that distinction is
    load-bearing. A word boundary does not occur inside "unsigned", because the `n` before it is a
    word character, so the pattern already ignores the disclaimer wording and no separate skip is
    needed. An earlier version skipped any line containing "unsigned", which let a line that claims
    signing *and* disclaims it in the same sentence through untouched -- a mutation proved it.
    """
    assert not any(step in pipeline_text() for step in SIGNING_STEPS), (
        "the pipeline now signs artifacts, so this guard's premise is stale: the documents may "
        "say so again, and this check should be inverted rather than kept"
    )
    offenders: list[str] = []
    for document in DOCUMENTS:
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            low = prose(line).lower()
            if "sha256sums" not in low:
                continue
            if re.search(r"\bsigned\b", low):
                offenders.append(f"{document.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, (
        "a document claims the release checksums are signed, and nothing signs them:\n  "
        + "\n  ".join(offenders)
    )


def test_no_document_claims_commits_are_signed() -> None:
    """Commits are not signed, so no document may promise that they are.

    Both word orders are matched. A mutation that wrote "commits are signed" instead of "signed
    commits" survived the first version of this check, and it is the same claim said the other way
    round -- a guard that only catches one phrasing is a guard for one phrasing.
    """
    offenders: list[str] = []
    patterns = (
        re.compile(r"\bsigned\s+commits?\b", re.IGNORECASE),
        re.compile(r"\bcommits?\s+(?:are|is|will be)\s+signed\b", re.IGNORECASE),
    )
    for document in DOCUMENTS:
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            text = prose(line)
            if "not signed" in text.lower():
                continue
            if any(pattern.search(text) for pattern in patterns):
                offenders.append(f"{document.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, (
        "a document claims commits are signed; no commit in the repository is:\n  "
        + "\n  ".join(offenders)
    )


def test_the_signing_commands_the_documents_name_exist() -> None:
    """`SECURITY.md` tells a reader to run three commands. They have to be real commands."""
    from docket.cli import build_parser

    parser = build_parser()
    subcommands = {action.dest for action in parser._actions if hasattr(action, "choices")}
    assert subcommands, "the parser exposes no subcommands, which cannot be right"

    choices = {
        action.dest: list(action.choices)
        for action in parser._actions
        if getattr(action, "choices", None)
    }
    available = {name for group in choices.values() for name in group}
    assert "keygen" in available, "SECURITY.md names `docket keygen`, which the CLI does not have"
    assert "verify" in available, "SECURITY.md names `docket verify`, which the CLI does not have"

    # The flags the section depends on: --sign on run, --public-key on verify.
    flags: dict[str, set[str]] = {}
    for action in parser._actions:
        if not getattr(action, "choices", None):
            continue
        for name, child in action.choices.items():
            flags[name] = {opt for sub in child._actions for opt in sub.option_strings}
    assert "--sign" in flags.get("run", set()), "SECURITY.md names `docket run --sign`, which has no --sign"
    assert "--public-key" in flags.get("verify", set()), (
        "SECURITY.md says verify checks the signature, but `docket verify` has no --public-key"
    )


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.name)
def test_documents_do_not_promise_a_signing_key_that_the_pipeline_cannot_use(document: Path) -> None:
    """A promise to sign in CI would need a secret this repository has no way to hold."""
    text = document.read_text(encoding="utf-8").lower()
    assert "sign the release" not in text, (
        f"{document.name} promises to sign the release; no signing step exists"
    )
