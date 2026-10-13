"""Filesystem snapshots and diffs — the evidence for what an agent touched.

Snapshotting is intentionally boring: walk the watched paths, skip ignored globs
and files above a size ceiling, hash what is left. Two snapshots and a diff give a
verifiable list of added, modified and removed files, each with a content digest,
which is what an auditor or a reviewer actually wants ("what changed during this
run?") rather than a stream of syscalls nobody reads.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import matches_any

MAX_HASH_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FileState:
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"size": self.size, "sha256": self.sha256}


def _hash_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def snapshot(
    roots: Iterable[str],
    ignore: Iterable[str] = (),
    cwd: str | os.PathLike[str] | None = None,
    max_files: int = 20000,
) -> dict[str, FileState]:
    """Hash the watched tree. Returns a mapping of relative path to state."""
    base = Path(cwd or os.getcwd())
    result: dict[str, FileState] = {}
    count = 0
    for root in roots:
        target = (base / root).resolve() if not Path(root).is_absolute() else Path(root)
        if not target.exists():
            continue
        candidates = [target] if target.is_file() else sorted(target.rglob("*"))
        for path in candidates:
            if path.is_dir() or path.is_symlink():
                continue
            try:
                rel = str(path.relative_to(base))
            except ValueError:
                rel = str(path)
            if matches_any(rel, ignore):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > MAX_HASH_BYTES:
                continue
            digest = _hash_file(path)
            if digest is None:
                continue
            result[rel] = FileState(size=stat.st_size, sha256=digest)
            count += 1
            if count >= max_files:
                return result
    return result


def diff(
    before: dict[str, FileState],
    after: dict[str, FileState],
) -> list[dict[str, Any]]:
    """Structural diff of two snapshots."""
    changes: list[dict[str, Any]] = []
    for path in sorted(set(after) - set(before)):
        changes.append({"path": path, "change": "added", **after[path].to_dict()})
    for path in sorted(set(before) & set(after)):
        if before[path].sha256 != after[path].sha256:
            changes.append(
                {
                    "path": path,
                    "change": "modified",
                    "before": before[path].to_dict(),
                    "after": after[path].to_dict(),
                }
            )
    for path in sorted(set(before) - set(after)):
        changes.append({"path": path, "change": "removed", **before[path].to_dict()})
    return changes


def tree_digest(state: dict[str, FileState]) -> str:
    """A single digest summarising a whole tree (order-independent)."""
    h = hashlib.sha256()
    for path in sorted(state):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(state[path].sha256.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def deny_write_violations(changes: list[dict[str, Any]], deny_write: Iterable[str]) -> list[dict[str, Any]]:
    """Changes that fall inside a ``files.deny_write`` scope."""
    patterns = list(deny_write)
    if not patterns:
        return []
    violations = []
    for change in changes:
        if change["change"] == "removed":
            continue
        if matches_any(change["path"], patterns):
            violations.append(change)
    return violations
