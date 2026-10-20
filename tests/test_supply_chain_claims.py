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

1. No document may call a release artifact or a commit signed unless the pipeline contains a step that
   signs it.
2. The commands the supply-chain section tells a reader to run have to exist on the real CLI surface,
   with the real flags, so a rename or a removal fails the build rather than leaving prose describing
   a command that no longer runs.

Three gaps were found by mutating this guard rather than by reading it, and each was a real gap:

- It skipped any line containing "unsigned", so a line that claimed signing *and* disclaimed it in one
  sentence walked through. A word boundary already separates the two, because `\\bsigned\\b` does not
  match inside "unsigned" where the `n` is a word character, so the skip is gone.
- It matched only "signed commits", so the same claim written "commits are signed" was missed.
- Quotations were stripped line by line, so a quotation that wraps -- the normal case in a file with a
  100-column convention -- left half of itself behind and the guard flagged the documentation written
  to explain the defect.
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

# The two word orders of the signed-commits claim. A guard for one phrasing is a guard for one
# phrasing.
COMMIT_PATTERNS = (
    re.compile(r"\bsigned\s+commits?\b", re.IGNORECASE),
    re.compile(r"\bcommits?\s+(?:are|is|will be)\s+signed\b", re.IGNORECASE),
)


def pipeline_text() -> str:
    parts = [p.read_text(encoding="utf-8") for p in WORKFLOWS]
    parts += [
        (REPO / "Makefile").read_text(encoding="utf-8"),
        (REPO / "pyproject.toml").read_text(encoding="utf-8"),
    ]
    for extra in (REPO / "tools").glob("*.py"):
        parts.append(extra.read_text(encoding="utf-8"))
    return "\n".join(parts).lower()


def prose_lines(text: str) -> list[str]:
    """The document's lines, with every double-quoted span blanked out.

    `docs/testing.md` and the changelog both quote the claims this guard condemns, in order to record
    what was wrong. An assertion and a quotation of an assertion are different things, and the
    difference has to survive the check or the guard flags its own documentation.

    The masking runs over the whole document rather than line by line, because a quotation wraps: the
    changelog entry for this fix opens `"with a signed `SHA256SUMS`"` on one line and closes it on the
    next, and a line-based stripper leaves `a signed` behind on the second line with an unbalanced
    quote. Newlines are preserved so the result still lines up with the source, and the quote marks
    themselves are blanked so half a quotation cannot survive either.

    This only ever removes text, so a claim written without quotes is still tested.
    """
    out: list[str] = []
    inside = False
    for char in text:
        if char == '"':
            inside = not inside
            out.append(" ")
        elif inside:
            out.append("\n" if char == "\n" else " ")
        else:
            out.append(char)
    return "".join(out).splitlines()


def test_no_document_claims_a_release_artifact_is_signed() -> None:
    """A signature claim needs a signing step; there is none, so no document may make it."""
    assert not any(step in pipeline_text() for step in SIGNING_STEPS), (
        "the pipeline now signs artifacts, so this guard's premise is stale: the documents may say so"
        " again, and this check should be inverted rather than kept"
    )
    offenders: list[str] = []
    for document in DOCUMENTS:
        for number, line in enumerate(prose_lines(document.read_text(encoding="utf-8")), 1):
            low = line.lower()
            if "sha256sums" not in low:
                continue
            if re.search(r"\bsigned\b", low):
                offenders.append(f"{document.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, (
        "a document claims the release checksums are signed, and nothing signs them:\n  " + "\n  ".join(offenders)
    )


def test_no_document_claims_commits_are_signed() -> None:
    """Commits are not signed, so no document may promise that they are, in either word order."""
    offenders: list[str] = []
    for document in DOCUMENTS:
        for number, line in enumerate(prose_lines(document.read_text(encoding="utf-8")), 1):
            if "not signed" in line.lower():
                continue
            if any(pattern.search(line) for pattern in COMMIT_PATTERNS):
                offenders.append(f"{document.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, "a document claims commits are signed; no commit in the repository is:\n  " + "\n  ".join(
        offenders
    )


def test_the_signing_commands_the_documents_name_exist() -> None:
    """`SECURITY.md` tells a reader to run three commands. They have to be real commands."""
    from docket.cli import build_parser

    parser = build_parser()
    subcommands = {action.dest for action in parser._actions if hasattr(action, "choices")}
    assert subcommands, "the parser exposes no subcommands, which cannot be right"

    choices = {action.dest: list(action.choices) for action in parser._actions if getattr(action, "choices", None)}
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
def test_documents_do_not_promise_a_signing_key_the_pipeline_cannot_use(document: Path) -> None:
    """A promise to sign in CI would need a secret this repository has no way to hold."""
    text = document.read_text(encoding="utf-8").lower()
    assert "sign the release" not in text, f"{document.name} promises to sign the release; no signing step exists"
