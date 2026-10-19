"""Snapshots and rollback: undo what an agent did to the working tree.

Docket's default posture is *evidence*, not control: it records the digest of every file the
run touched and proves afterwards what changed. That is enough to investigate, and not enough
to recover — a digest cannot be restored. This module adds the deliberate, opt-in second half:

``docket run --snapshot``
    Before the agent starts, copy the *contents* of the files the policy watches into a
    content-addressed store under the run's evidence directory, with a manifest of path,
    size, digest, mode and mtime. Files above the size ceiling, or a tree over the byte
    budget, are refused up front rather than half-captured: a snapshot you cannot rely on is
    worse than no snapshot, because it invites restoring over a file it never captured.

``docket run --rollback-on-breach``
    If the run breaches a cap, restore the watched tree automatically from that snapshot and
    record what was restored in the ledger.

``docket restore <receipt-or-run>``
    The same restore, on demand, months later: dry-run by default, ``--apply`` to write,
    and it refuses to overwrite a file that changed *after* the run unless ``--force`` says
    so. Restoring files is a destructive act on somebody's working tree; it is never done
    silently.

**A restore obeys one rule: it only ever touches files inside the run's tree.** The list of
paths comes from documents — a ledger, a snapshot manifest — that describe what a run did but
do not prove who wrote about it. So the paths are checked rather than trusted: no absolute
paths, no ``..``, no component that is a symbolic link, no blob outside the store, no setuid
bit, and the tree itself is never taken purely from the document (see ``docket restore
--help``). A path that fails the check appears in the plan as ``refused`` with the reason, and
is not touched.

Two properties are load-bearing:

* **Content-addressed, so it is small.** Ten runs that rewrite one file have one copy of its
  bytes, not ten; identical files across directories share one blob.
* **Honest about what it cannot do.** Files created *after* the run are removed on restore;
  files the snapshot skipped (too large, unreadable, created during the run) are listed as
  ``not_covered`` and the report says so rather than implying a clean tree.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fsdiff import MAX_HASH_BYTES
from .util import atomic_write, loads_json, matches_any

#: Largest total size a snapshot may store, by default. An agent's working tree is usually
#: small; a repository is not, and copying one into ``.docket`` on every run would be a
#: surprise nobody asked for. Over the budget the run is refused with the numbers, and the
#: operator either raises the budget or narrows ``[files] watch``.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024


@dataclass
class SnapshotResult:
    """What a snapshot captured, what it refused, and where it lives."""

    root: str
    store: str
    manifest_path: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    not_covered: list[dict[str, str]] = field(default_factory=list)
    bytes_stored: int = 0
    bytes_watched: int = 0
    taken_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "store": self.store,
            "manifest": self.manifest_path,
            "files": len(self.entries),
            "not_covered": self.not_covered,
            "bytes_stored": self.bytes_stored,
            "bytes_watched": self.bytes_watched,
            "taken_at": self.taken_at,
        }


class SnapshotTooLargeError(RuntimeError):
    """The watched tree does not fit the snapshot budget."""

    def __init__(self, message: str, *, watched: int, budget: int) -> None:
        super().__init__(message)
        self.watched = watched
        self.budget = budget


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _blob_path(store: Path, digest: str) -> Path:
    """Content-addressed blob location: ``store/blobs/<aa>/<rest>`` (fan-out keeps directories small)."""
    return store / "blobs" / digest[:2] / digest[2:]


def _blob_for(store: Path, digest: Any) -> tuple[Path | None, str]:
    """The blob a digest names, or a reason why it names nothing usable.

    The store location is *derived from the digest*, never taken from the document: a manifest
    that says ``"blob": "../../../home/you/.ssh/id_rsa"`` names no blob, because a blob is only
    ever ``blobs/<aa>/<rest>`` under the store. A digest that is not 64 lowercase hex digits
    gets the same treatment — the digest is a content address, and a content address with a
    slash, a dot or a letter outside ``0-9a-f`` is not one.
    """
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None, f"the snapshot has no usable SHA-256 for this file (got {digest!r})"
    return _blob_path(store, digest), ""


def _resolve_inside(base: Path, relative: Any) -> tuple[Path | None, str]:
    """Join ``relative`` onto ``base``, refusing anything that could leave the tree.

    A restore writes and deletes files, and the list of paths comes from a document (a ledger,
    a snapshot manifest) that Docket did not authenticate. So every path is checked, not
    trusted: absolute paths are refused (``base / "/etc/passwd"`` is ``/etc/passwd``), ``..``
    components are refused, and no component may be a symbolic link, because restoring through
    one writes wherever it points. A plan that fails this is reported path by path rather than
    obeyed.

    This guards against a *hostile document*. It is not a defence against a local attacker
    racing Docket between the check and the write, which is a different threat model — and one
    where the operator's own tree is already lost.
    """
    if not isinstance(relative, str) or not relative:
        return None, f"not a usable path: {relative!r}"
    if "\x00" in relative:
        return None, "the path contains a NUL byte"
    if os.path.isabs(relative):
        return None, "an absolute path: a restore only ever writes inside the run's tree"
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None, "the path contains an empty, '.' or '..' component"
    path = base
    for part in parts:
        path = path / part
        if path.is_symlink():
            return None, f"the path goes through the symbolic link {path}"
    try:
        resolved_parent = path.parent.resolve()
    except OSError as exc:
        return None, f"the path cannot be resolved: {exc}"
    if resolved_parent != base and base not in resolved_parent.parents:
        return None, f"the path resolves outside the tree ({resolved_parent})"
    return path, ""


def take_snapshot(
    *,
    roots: list[str],
    ignore: list[str],
    root: str | os.PathLike[str],
    store: str | os.PathLike[str],
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_file_bytes: int = MAX_HASH_BYTES,
    taken_at: str = "",
) -> SnapshotResult:
    """Copy watched file contents into ``store`` and write the manifest.

    Reads every watched file twice (once to measure, once to copy) only when the budget is not
    already exceeded; the measuring pass is what makes the refusal possible *before* anything
    is written, so a refused snapshot leaves no half-copied tree behind.
    """
    base = Path(root).resolve()
    store_path = Path(store)
    result = SnapshotResult(root=str(base), store=str(store_path), manifest_path=str(store_path / "manifest.json"))
    result.taken_at = taken_at
    candidates: list[tuple[str, Path, os.stat_result]] = []
    for watched in roots:
        target = (base / watched) if not Path(watched).is_absolute() else Path(watched)
        if not target.exists():
            continue
        try:
            target.relative_to(base)
        except ValueError:
            # A watched root outside the working directory (an absolute path in [files] watch)
            # has no relative name, and a snapshot is a set of relative names: the restore
            # joins them onto the run's tree. Recording the absolute path instead would put a
            # path from a document in charge of where a restore writes, so it is disclosed as
            # uncovered rather than captured under a name that cannot be trusted.
            result.not_covered.append(
                {
                    "path": str(target),
                    "reason": "outside the run's working directory, so it has no name a restore could safely join onto it",
                }
            )
            continue
        paths = [target] if target.is_file() else sorted(target.rglob("*"))
        for path in paths:
            try:
                relative = str(path.relative_to(base))
            except ValueError:
                # Only reachable through a symlinked directory during the walk: same reasoning
                # as above — refuse the name rather than invent one.
                result.not_covered.append(
                    {"path": str(path), "reason": "the path left the run's working directory during the walk"}
                )
                continue
            if path.is_symlink():
                result.not_covered.append(
                    {
                        "path": relative,
                        # Following it would copy the target's bytes — possibly a key or a
                        # credential outside the tree — into the store and later into the tree
                        # as a regular file. The link itself is cheap to recreate; its content
                        # is not Docket's to capture.
                        "reason": "symbolic link: the snapshot stores file contents, and following it would "
                        "capture whatever it points at",
                    }
                )
                continue
            if path.is_dir():
                continue
            if matches_any(relative, ignore):
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                result.not_covered.append({"path": relative, "reason": f"cannot stat: {exc.strerror or exc}"})
                continue
            if stat.st_size > max_file_bytes:
                result.not_covered.append(
                    {
                        "path": relative,
                        "reason": f"{stat.st_size} bytes is over the per-file snapshot ceiling of {max_file_bytes}",
                    }
                )
                continue
            candidates.append((relative, path, stat))
            result.bytes_watched += stat.st_size
    if result.bytes_watched > max_bytes:
        raise SnapshotTooLargeError(
            f"the watched tree is {result.bytes_watched} bytes, over the snapshot budget of {max_bytes}. "
            "Raise --snapshot-max-bytes deliberately, or narrow [files] watch so the snapshot covers what "
            "an agent can actually damage.",
            watched=result.bytes_watched,
            budget=max_bytes,
        )

    (store_path / "blobs").mkdir(parents=True, exist_ok=True)
    copied: dict[str, str] = {}
    for relative, path, stat in candidates:
        try:
            digest = _sha256_file(path)
        except OSError as exc:
            result.not_covered.append({"path": relative, "reason": f"cannot read: {exc.strerror or exc}"})
            continue
        blob = _blob_path(store_path, digest)
        if digest not in copied:
            blob.parent.mkdir(parents=True, exist_ok=True)
            if not blob.exists():
                # Copy to a temporary file first: a blob that appears half-written under its
                # own digest would be a corrupted snapshot that looks complete.
                temporary = blob.with_suffix(".partial")
                shutil.copyfile(path, temporary)
                os.replace(temporary, blob)
                result.bytes_stored += stat.st_size
            copied[digest] = str(blob.relative_to(store_path))
        result.entries.append(
            {
                "path": relative,
                "blob": copied[digest],
                "sha256": digest,
                "size": stat.st_size,
                "mode": stat.st_mode & 0o777,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    manifest = {
        "format": "docket/snapshot/v1",
        "root": str(base),
        "taken_at": result.taken_at,
        "bytes_watched": result.bytes_watched,
        "bytes_stored": result.bytes_stored,
        "entries": result.entries,
        "not_covered": result.not_covered,
    }
    # UTF-8 rather than `\uXXXX` escapes, matching the ledger and the receipt: one serialisation
    # convention in the codebase, and a manifest a person can read when a path looks wrong.
    atomic_write(store_path / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False))
    return result


def read_manifest(store: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a snapshot manifest, refusing anything that is not one."""
    path = Path(store) / "manifest.json" if Path(store).is_dir() else Path(store)
    if not path.exists():
        raise FileNotFoundError(f"no snapshot manifest at {path}")
    document = loads_json(path.read_text(encoding="utf-8"), what=str(path))
    if not isinstance(document, dict) or document.get("format") != "docket/snapshot/v1":
        raise ValueError(f"{path} is not a docket snapshot manifest")
    return document


def plan_restore(
    manifest: dict[str, Any],
    changes: list[dict[str, Any]],
    *,
    include_added: bool = True,
) -> list[dict[str, Any]]:
    """Work out, per changed path, what restoring it means — without touching the tree.

    The plan is derived from the *snapshot manifest* and the run's ``fs.change`` events, so it
    can be audited before it is applied and re-derived from the ledger by anyone:

    * ``restore``  — the file existed before the run and was modified; put the old bytes back.
    * ``recreate`` — the file existed before and was removed; put it back.
    * ``remove``   — the file did not exist before the run and does; delete what the agent made.
    * ``not_covered`` — the file changed but the snapshot has no copy of its original bytes
      (skipped as too large, created during the run, or the run had no snapshot for it). Named
      explicitly, never silently ignored.
    """
    store_by_path: dict[str, dict[str, Any]] = {}
    for entry in manifest.get("entries", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        # First entry wins: a manifest that names one path twice is not a manifest Docket wrote,
        # and silently letting the *last* copy decide which bytes a path gets is how a doctored
        # store replaces the original with something else.
        store_by_path.setdefault(entry["path"], entry)
    plan: list[dict[str, Any]] = []
    for change in changes:
        path = change.get("path")
        kind = change.get("change")
        if not isinstance(path, str) or kind not in {"added", "modified", "removed"}:
            # A record that cannot be read is reported, not dropped: a restore that quietly
            # ignored an unreadable change would report "nothing to restore" over a file the
            # run demonstrably touched.
            plan.append(
                {
                    "path": str(path),
                    "action": "refused",
                    "reason": f"malformed change record: path={path!r}, change={kind!r}",
                    "change": kind,
                }
            )
            continue
        if _resolve_inside(Path("/"), path)[0] is None:
            reason = _resolve_inside(Path("/"), path)[1]
            plan.append(
                {
                    "path": path,
                    "action": "refused",
                    "reason": f"the record names a path a restore will not touch: {reason}",
                    "change": kind,
                }
            )
            continue
        entry = store_by_path.get(path)
        if kind == "modified" or kind == "removed":
            if entry is None:
                plan.append(
                    {
                        "path": path,
                        "action": "not_covered",
                        "reason": "the run changed this file but the snapshot has no copy of it",
                        "change": kind,
                    }
                )
            else:
                plan.append(
                    {
                        "path": path,
                        "action": "restore" if kind == "modified" else "recreate",
                        "blob": entry.get("blob"),
                        "sha256": entry.get("sha256"),
                        # What the document asked for, kept as asked: a manifest that requests
                        # setuid is a fact about the manifest, and `apply_restore` decides what
                        # to honour and says so. Clamping here would erase the evidence.
                        "mode": _requested_mode(entry, 0o644),
                        "mode_honoured": _mode_of(entry, 0o644),
                        "change": kind,
                    }
                )
        elif kind == "added" and include_added:
            plan.append({"path": path, "action": "remove", "change": kind})
    return plan


def apply_restore(
    plan: list[dict[str, Any]],
    *,
    root: str | os.PathLike[str],
    store: str | os.PathLike[str],
    force: bool = False,
) -> list[dict[str, Any]]:
    """Apply a restore plan. Returns one record per path, including refusals.

    Each plan item may carry a ``post_sha256``: the digest the file had at the *end* of the run.
    The file's digest is measured again here, and when the two disagree somebody edited it after
    the run — a colleague fixing the bug, a formatter, the operator themselves. Restoring over
    that would silently discard their work, so the path is refused unless ``force=True``. Refusals
    are returned as records rather than raised, so a partial restore is reported path by path
    instead of stopping at the first surprise.

    The exception is the file that *already holds the snapshot's bytes*: that is a path
    somebody — including an earlier run of this same command — has already put back. Refusing it
    as "changed after the run" would be a false accusation and would make restoring twice fail,
    which is exactly what an operator repeating a command (or a script doing it for them) would
    hit. It is reported as ``already``: no write, no refusal, no claim that anything happened.
    """
    base = Path(root).resolve()
    store_path = Path(store)
    applied: list[dict[str, Any]] = []
    for item in plan:
        action = item.get("action")
        if action not in {"restore", "recreate", "remove", "not_covered", "refused"}:
            applied.append({**item, "status": "refused", "reason": f"unknown action {action!r}"})
            continue
        if action == "not_covered":
            applied.append({**item, "status": "skipped", "reason": item.get("reason")})
            continue
        if action == "refused":
            applied.append({**item, "status": "refused", "reason": item.get("reason")})
            continue
        # Re-derived here, not taken from the plan: `apply_restore` is a public function, and a
        # caller that hands it a hand-built plan gets the same confinement as one that read the
        # plan out of a ledger.
        path, refusal = _resolve_inside(base, item.get("path"))
        if path is None:
            applied.append({**item, "status": "refused", "reason": f"not restored: {refusal}"})
            continue
        expected = (item.get("sha256") or "") if action in {"restore", "recreate"} else None
        post = item.get("post_sha256")
        needs_comparison = bool(expected) or (bool(post) and not force and action in {"remove", "restore", "recreate"})
        observed: str | None = None
        if needs_comparison:
            try:
                observed = _sha256_file(path) if path.is_file() and not path.is_symlink() else None
            except OSError:
                observed = None
        # The reason to refuse this path, if anybody edited it after the run finished. Computed
        # once and consulted per action: a removal has nothing to compare against, and a
        # restore/recreate that finds the snapshot's bytes already in place has nothing to
        # overwrite — which is what makes restoring twice a success rather than an accusation
        # (see the `already` branch below).
        changed_since_the_run: str | None = None
        if (
            not force
            and post
            and action in {"remove", "restore", "recreate"}
            and observed is not None
            and observed != post
        ):
            changed_since_the_run = (
                "the file changed after the run "
                f"(expected {post[:12]}…, found {observed[:12]}…); "
                "re-run with --force to overwrite it"
            )
        try:
            if action == "remove":
                if changed_since_the_run:
                    applied.append({**item, "status": "refused", "reason": changed_since_the_run})
                    continue
                if path.is_symlink() or path.is_file():
                    path.unlink()
                    applied.append({**item, "status": "removed"})
                elif path.exists():
                    applied.append({**item, "status": "refused", "reason": "not a regular file"})
                else:
                    applied.append({**item, "status": "absent"})
                continue
            if not expected:
                applied.append(
                    {
                        **item,
                        "status": "refused",
                        "reason": "the plan carries no digest, so the bytes cannot be checked",
                    }
                )
                continue
            blob, why = _blob_for(store_path, expected)
            if blob is None:
                applied.append({**item, "status": "refused", "reason": f"not restored: {why}"})
                continue
            recorded = item.get("blob")
            if recorded is not None and str(recorded) != str(blob.relative_to(store_path)):
                applied.append(
                    {
                        **item,
                        "status": "refused",
                        "reason": (
                            f"the plan points at {recorded!r} but its digest names "
                            f"{blob.relative_to(store_path)}; a blob is only ever its own digest"
                        ),
                    }
                )
                continue
            honoured = item.get("mode_honoured")
            mode = _mode_of(item, honoured if isinstance(honoured, int) else 0)
            if observed == expected and _current_mode(path) == mode:
                # The file already holds the bytes the snapshot wants, with the mode the plan
                # would apply: an earlier run of this same command, or the operator putting it
                # back by hand. Nothing to do, and — the reason this branch exists — nothing to
                # accuse anybody of: without it, a second restore reported the file as "changed
                # after the run" when the change was Docket's own, so repeating the command
                # failed. It sits after the plan has been checked against the store, so a
                # doctored plan is still refused on its own merits rather than reported as an
                # idempotent no-op.
                applied.append({**item, "status": "already", "reason": "already the bytes the snapshot holds"})
                continue
            if changed_since_the_run:
                applied.append({**item, "status": "refused", "reason": changed_since_the_run})
                continue
            try:
                descriptor = os.open(blob, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError as exc:
                applied.append(
                    {
                        **item,
                        "status": "failed",
                        "reason": f"snapshot blob {blob.relative_to(store_path)} is unreadable: {exc}",
                    }
                )
                continue
            try:
                with os.fdopen(descriptor, "rb") as handle:
                    data = handle.read()
            except OSError as exc:
                applied.append({**item, "status": "failed", "reason": f"snapshot blob could not be read: {exc}"})
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest != expected:
                applied.append(
                    {**item, "status": "failed", "reason": "snapshot blob does not match its manifest digest"}
                )
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path, refusal = _resolve_inside(base, item.get("path"))
            if path is None:
                applied.append({**item, "status": "refused", "reason": f"not restored: {refusal}"})
                continue
            # Atomic replace: a crash mid-restore leaves the old bytes or the new ones, never a
            # half-written file that a later restore would then treat as the original.
            atomic_write(path, data)
            record = {**item, "status": "restored" if action == "restore" else "recreated"}
            if item.get("mode") not in (None, mode):
                record["mode_note"] = (
                    f"applied {mode:#o}, not the {item['mode']:#o} the snapshot asked for: only the "
                    "permission bits are honoured, so the file does not come back setuid or setgid"
                )
            applied.append(record)
            if mode:
                with contextlib.suppress(OSError):
                    os.chmod(path, mode)
        except OSError as exc:
            applied.append({**item, "status": "failed", "reason": str(exc)})
    return applied


def _current_mode(path: Path) -> int:
    """The permission bits a file has right now, or 0 if they cannot be read.

    A file whose bytes are already right but whose mode is not is *not* an idempotent no-op:
    the plan asked for a mode and the tree does not have it, so the restore still has work to do.
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return 0


def _requested_mode(item: dict[str, Any], default: int = 0o644) -> int:
    """The mode a document asks for, verbatim — including bits a restore will not honour."""
    requested = item.get("mode", default)
    if isinstance(requested, bool) or not isinstance(requested, int):
        return default
    return requested


def _mode_of(item: dict[str, Any], default: int = 0) -> int:
    """The permission bits a document asks for, ignoring anything that is not one.

    Modes arrive from the same untrusted places as paths do — a manifest written by another
    Docket, a hand-edited store — and the bits that matter are the three rwx triples. Setuid,
    setgid and the sticky bit are dropped: a restore of somebody's file is not an occasion to
    create a program that runs as its owner.
    """
    try:
        requested = int(item.get("mode") or default)
    except (TypeError, ValueError):
        return default & 0o777
    return max(0, requested) & 0o777


def summarise(applied: list[dict[str, Any]]) -> dict[str, int]:
    """Count a restore by outcome, so a report can lead with what actually happened."""
    counts: dict[str, int] = {}
    for item in applied:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts
