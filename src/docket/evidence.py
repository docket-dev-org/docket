"""Bounded capture of the provider responses the boundary saw.

A ``model.request`` event can carry the request body verbatim (``--record-bodies``), but the
matching ``model.response`` event could only ever carry ``response_bytes`` — a *length*. The bytes
the boundary handed back to the agent were then dropped. That asymmetry has one consequence that
matters: when a provider's usage block is not one of the three shapes Docket knows how to read, the
token counts on the event are an *estimate* derived from those very bytes, and the bytes are gone.
The call is recorded; the response is not, and no reader can re-parse it with a parser Docket does
not have. An unknown provider is therefore recorded as a guess about itself.

This module closes that gap without pretending to close it. It records two numbers, and they are
different in kind:

* ``response_sha256`` — the digest of the exact bytes returned to the agent. Always present, and
  cheap, and not a privacy exposure: a digest is 64 characters. This makes a response
  *identifiable*: anyone who obtains the body can prove it is the body Docket saw, and a body that
  hashes to something else is not.
* ``response_stored_sha256`` — the digest of the bytes actually written to disk. Present only when
  retention is on. This is what makes a retained response *checkable*, and it is the one
  ``docket verify`` recomputes over the file.

The two are **not** the same number when the body is longer than the retention cap, and the event
says so rather than letting the pair look like a match: ``response_truncated``, plus
``response_stored_bytes`` and ``response_total_bytes``. Docket pins what it can prove and labels
what it cannot.

Retention is off by default and stays off unless a policy asks for it. A response body can contain
the prompt, a tool result, a person's name, or a secret; the digest is the default and the body is
the opt-in. Nothing here parses the body — that is the point. A response is captured because it was
seen, not because Docket understood it.

Files are named by their own digest, so an identical response is stored once and a file's name is
its claim about its contents.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Default cap on the bytes retained for one response body. 64 KiB covers a completion and its
#: usage block, and is small enough that a thousand calls cannot fill a disk the ledger has
#: promised to keep an eye on.
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024

#: Sub-directory of the run directory that holds retained bodies, named by digest.
SUBDIRECTORY = "responses"

#: Suffix for a retained body. ``.bin`` because the body is stored exactly as it arrived and Docket
#: does not claim it is text.
SUFFIX = ".bin"


@dataclass(frozen=True)
class ResponseCapture:
    """What the boundary can say about one provider response body.

    Total by construction: every field is either a measurement or ``None``, and
    ``stored_sha256 is None`` is precisely the statement "these bytes were not kept". A capture is
    never fabricated — if the digest was computed, it is the digest of those bytes.
    """

    #: Length of the exact bytes the boundary returned to the agent.
    total_bytes: int
    #: SHA-256 of those exact bytes. Always computed.
    sha256: str
    #: Whether the body was kept on disk.
    retained: bool
    #: Bytes written to disk, when retained. Less than ``total_bytes`` when truncated.
    stored_bytes: int | None = None
    #: SHA-256 of the bytes written to disk, when retained. Not the same as ``sha256`` when the
    #: body was truncated, and the event carries both so the difference is visible.
    stored_sha256: str | None = None
    #: Where the body was written, when retained.
    path: Path | None = None
    #: Whether what was kept is a prefix of the body rather than all of it.
    truncated: bool = False
    #: Why retention did not happen when it was asked for. Never set when retention was not asked
    #: for: an unrequested omission is not a failure and must not read as one.
    note: str | None = None

    def to_dict(self, *, root: Path | None = None) -> dict[str, Any]:
        """The fields to merge into a ``model.response`` event.

        The digest is always present. The retention fields are present when retention was
        requested, so an event that says nothing about retention asked for nothing. ``root`` makes
        the recorded path relative to the run directory, which is what keeps a receipt portable and
        what lets ``--redact paths`` replace it with one placeholder instead of leaving a
        filesystem layout behind.
        """
        fields: dict[str, Any] = {
            "response_sha256": self.sha256,
            "response_retained": self.retained,
        }
        if self.stored_bytes is not None:
            fields["response_stored_bytes"] = self.stored_bytes
            fields["response_total_bytes"] = self.total_bytes
            fields["response_truncated"] = self.truncated
        if self.stored_sha256 is not None:
            fields["response_stored_sha256"] = self.stored_sha256
        if self.path is not None:
            fields["response_path"] = _relative(self.path, root)
        if self.note is not None:
            fields["response_note"] = self.note
        return fields


def _relative(path: Path, root: Path | None) -> str:
    """``path`` as a string, relative to ``root`` when it is inside it.

    A receipt is meant to travel. An absolute path is a fact about the machine that made the
    receipt, and ``--redact paths`` replaces it with a placeholder, which is right for a public
    copy and useless for a reader who has the run. A path relative to the run directory is both.
    """
    if root is None:
        return str(path)
    try:
        return str(Path(path).relative_to(Path(root)))
    except ValueError:
        # Not under the root: keep the absolute path and let `--redact paths` handle it. Guessing
        # at a relative form here would produce a path that resolves to nothing.
        return str(path)


def digest(body: bytes) -> str:
    """SHA-256 of the exact bytes, as lowercase hex. Never raises for any input."""
    return hashlib.sha256(body).hexdigest()


def capture_response(
    body: bytes,
    *,
    retain: bool = False,
    max_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    directory: Path | None = None,
    disk_free: Any = None,
    min_free_bytes: int | None = None,
) -> ResponseCapture:
    """Digest one response body, and retain a bounded prefix of it when asked to.

    Total by contract: this never raises and never loses the body silently.

    * The digest is over the whole body, always, whether or not anything is retained. It is
      computed first and it is the number that identifies the response.
    * When ``retain`` is false, nothing is written and ``retained`` is false. That is the default
      and it is a configuration, not an error, so ``note`` stays ``None``.
    * When ``retain`` is true and there is nowhere to write, ``retained`` is false and ``note``
      says why. A response the boundary was asked to keep and could not keep is disclosed, because
      a run whose evidence quietly went missing is the failure this project exists to prevent.
    * When ``retain`` is true and the disk is below ``min_free_bytes``, the body is not written and
      ``note`` says so. Writing the last free block on the host would take the supervisor's own
      ledger with it.
    """
    total = digest(body)
    if not retain:
        return ResponseCapture(total_bytes=len(body), sha256=total, retained=False)

    if directory is None:
        return ResponseCapture(
            total_bytes=len(body),
            sha256=total,
            retained=False,
            note="retention was enabled but no run directory was given",
        )

    # A cap of zero or below keeps nothing. The policy refuses a cap under 1, so this is reachable
    # only by a caller that builds a capture directly, and it is handled rather than argued with:
    # an empty body is written, and the event says `stored_bytes: 0` and `truncated: true`, which
    # is a true description of what happened instead of a claim that nothing was asked for.
    kept = body[:max_bytes] if max_bytes > 0 else b""
    need = len(kept)
    free = disk_free() if callable(disk_free) else None
    if min_free_bytes is not None and free is not None and free - need < min_free_bytes:
        return ResponseCapture(
            total_bytes=len(body),
            sha256=total,
            retained=False,
            note=(
                f"the filesystem holding {directory} has {free} bytes free and writing "
                f"{need} would leave less than the {min_free_bytes} bytes the policy reserves"
            ),
        )

    target = Path(directory) / f"{total}{SUFFIX}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            # Written non-atomically on purpose: the file's name is its digest, so a torn write
            # can only ever produce a file whose contents do not match its name, and `docket
            # verify` catches exactly that. An atomic temp-and-rename would need a second, stable
            # name, which is the thing being avoided.
            with open(target, "wb") as handle:
                handle.write(kept)
    except OSError as exc:
        return ResponseCapture(
            total_bytes=len(body),
            sha256=total,
            retained=False,
            note=f"could not write the retained response to {target}: {exc.strerror or exc}",
        )

    return ResponseCapture(
        total_bytes=len(body),
        sha256=total,
        retained=True,
        stored_bytes=len(kept),
        stored_sha256=digest(kept),
        path=target,
        truncated=len(kept) < len(body),
    )


def check_retained(path: Path, expected: str) -> tuple[bool, str]:
    """Does the file at ``path`` hash to ``expected``?

    This is the check ``docket verify`` runs over a retained body. It is deliberately only about
    the *stored* bytes: a truncated body cannot be re-hashed into the whole-body digest, and a
    verifier that pretended otherwise would be checking a claim it had no way to make.

    Returns ``(ok, detail)``. ``ok`` is false with a stated reason when the file is absent,
    unreadable, or a different size — never by raising.
    """
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return False, f"the retained response {path} is missing"
    except OSError as exc:
        return False, f"the retained response {path} could not be read: {exc.strerror or exc}"
    actual = digest(data)
    if actual == expected:
        return True, f"sha256 {actual[:16]}… ({len(data)} bytes)"
    return False, (
        f"the retained response {path} hashes to {actual[:16]}… but the event recorded "
        f"{expected[:16]}…: the file is not the bytes the boundary returned"
    )


def responses_root(run_directory: Path) -> Path:
    """Where retained bodies live for a run directory."""
    return Path(run_directory) / SUBDIRECTORY


def stored_digest_is_consistent(capture: ResponseCapture) -> bool:
    """Whether a capture's own numbers agree with each other.

    Used by tests and by ``docket verify`` to reject a hand-written receipt that claims a retained
    body of one length whose digest belongs to another. Cheap, and it is the difference between a
    number that was measured and a number that was typed.
    """
    if not capture.retained:
        return capture.stored_sha256 is None and capture.stored_bytes is None
    return (
        capture.stored_sha256 is not None
        and capture.stored_bytes is not None
        and capture.stored_bytes <= capture.total_bytes
        and (capture.truncated or capture.stored_bytes == capture.total_bytes)
    )
