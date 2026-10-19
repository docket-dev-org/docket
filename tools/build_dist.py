#!/usr/bin/env python3
"""Build the distributions from a git ref, and record where they came from.

The failure this tool exists to prevent happened once already, in 0.1.4. The wheel and sdist
were built with ``python -m build`` from a working tree, and then two more commits landed
before the upload. The published 0.1.4 long description therefore lacked the README logo
that the repository had, and PyPI reserves a filename once it is published, so the only
remedy was a new release. The 0.1.5 sdist repeated the same mistake at a smaller scale: it
was built one commit before the ``.gitignore`` that tags it.

The fix is to stop treating the working tree as the source of truth. ``git archive`` exports
exactly the content a ref commits, and nothing else: untracked files do not leak in, and a
dirty tracked file is exported at the committed version, not the edited one. Building from
that export makes "the artifact matches the tag" true by construction rather than by
discipline.

``--check`` is the half that keeps it true. It reads ``dist/PROVENANCE.json`` and refuses a
mismatch, so an upload cannot happen against artifacts whose origin is not the ref being
released.

Run it from the repository root. It needs ``git`` and ``build``; it does not need network if
the build backend is already installed, which is why ``--no-isolation`` is accepted and
passed through.

    # Requires: Python 3.11+, git, and the `build` package.
    python3 tools/build_dist.py --ref v0.1.6            # export, build, record
    python3 tools/build_dist.py --check --ref v0.1.6    # prove dist/ came from that ref
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DIST = REPO / "dist"
PROVENANCE = DIST / "PROVENANCE.json"

#: Files the build itself creates, which a source digest must not count.
IGNORED_IN_DIGEST = {"PROVENANCE.json"}


class BuildError(RuntimeError):
    """Raised when the build cannot proceed, with the reason the caller needs."""


def run_git(*arguments: str, cwd: Path = REPO) -> str:
    """Run a git command and return its stdout, or raise with its stderr."""
    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell, no user input
        ["git", *arguments],  # noqa: S607 - git is resolved from PATH by design
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise BuildError(
            "git " + " ".join(arguments) + f" failed with exit code {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def resolve_ref(reference: str, repo: Path = REPO) -> str:
    """The commit a ref names, as a full sha. A ref that names nothing is an error, not a guess."""
    try:
        return run_git("rev-parse", "--verify", f"{reference}^{{commit}}", cwd=repo)
    except BuildError as error:
        raise BuildError(f"{reference!r} is not a ref in this repository: {error}") from error


def export_tree(sha: str, destination: Path, repo: Path = REPO) -> None:
    """Write the content of a commit into a directory, with no other file in it."""
    archive = subprocess.run(  # noqa: S603 - a fixed argv, no shell, no user input
        ["git", "archive", "--format=tar", sha],  # noqa: S607 - resolved from PATH by design
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        raise BuildError(f"git archive {sha} failed: {archive.stderr.decode('utf-8', 'replace').strip()}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        # A member outside the destination would be a tar-slip. git archive does not emit one,
        # and refusing is cheaper than trusting it.
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise BuildError(f"refusing to extract {member.name!r}: it escapes the destination")
        bundle.extractall(destination, filter="data")


def source_digest(root: Path) -> str:
    """A digest over the file names and their bytes, with no timestamp in it.

    Two exports of the same commit must agree, or the digest cannot answer the only question
    it is asked. Directory order is normalised by sorting, and empty directories contribute
    nothing because a source distribution cannot carry one.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in IGNORED_IN_DIGEST or "/dist/" in f"/{relative}/":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((relative, digest))
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_distributions(source: Path, outdir: Path, no_isolation: bool) -> list[Path]:
    """Run ``python -m build`` over an exported tree and return the artifacts it produced."""
    command = [sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(outdir)]
    if no_isolation:
        command.append("--no-isolation")
    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell, no user input
        command,
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise BuildError(
            f"python -m build failed with exit code {completed.returncode}:\n{completed.stderr.strip()[-2000:]}"
        )
    produced = sorted(path for path in outdir.iterdir() if path.is_file())
    if not produced:
        raise BuildError("the build reported success and produced no artifact")
    return produced


def describe(path: Path) -> dict[str, Any]:
    """The name, size and digest of an artifact, which is what a provenance record needs."""
    return {
        "filename": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def read_provenance() -> dict[str, Any]:
    """The provenance record in ``dist/``, or an error saying there is none."""
    if not PROVENANCE.is_file():
        raise BuildError(
            f"{PROVENANCE} does not exist: build with `python3 tools/build_dist.py --ref <ref>` before checking"
        )
    try:
        record = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildError(f"{PROVENANCE.name} is not readable JSON: {error}") from error
    if not isinstance(record, dict) or "ref" not in record or "commit" not in record:
        raise BuildError(f"{PROVENANCE.name} does not look like a provenance record")
    return record


def check(reference: str) -> int:
    """Refuse unless the artifacts in ``dist/`` came from the ref being released."""
    expected = resolve_ref(reference, REPO)
    record = read_provenance()
    problems: list[str] = []

    if record.get("commit") != expected:
        problems.append(f"the artifacts were built from {record.get('commit')!r}, but {reference!r} is {expected!r}")

    artifacts = record.get("artifacts") or []
    for artifact in artifacts:
        path = DIST / artifact.get("filename", "")
        if not path.is_file():
            problems.append(f"{artifact.get('filename')!r} is recorded but missing from dist/")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != artifact.get("sha256"):
            problems.append(
                f"{artifact['filename']} has changed since it was built: "
                f"{artifact.get('sha256')} recorded, {actual} on disk"
            )

    if problems:
        print("dist/ does not match " + reference + ":", file=sys.stderr)
        for problem in problems:
            print("  - " + problem, file=sys.stderr)
        print(
            "\nRebuild with: python3 tools/build_dist.py --ref " + reference,
            file=sys.stderr,
        )
        return 1

    print(f"dist/ matches {reference} ({expected[:12]}), {len(artifacts)} artifact(s) unchanged")
    return 0


def build(reference: str, no_isolation: bool) -> int:
    """Export a ref, build from the export, and record the result."""
    # REPO is read here rather than taken as a default argument, because a default is bound when
    # the function is defined and a caller that rebinds the module's REPO would be ignored.
    sha = resolve_ref(reference, REPO)
    DIST.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="docket-dist-") as scratch:
        source = Path(scratch) / "source"
        export_tree(sha, source, REPO)
        digest = source_digest(source)
        outdir = Path(scratch) / "out"
        outdir.mkdir()
        produced = build_distributions(source, outdir, no_isolation)

        for artifact in produced:
            shutil.copy2(artifact, DIST / artifact.name)
        copied = [DIST / artifact.name for artifact in produced]

    record: dict[str, Any] = {
        "ref": reference,
        "commit": sha,
        "source_sha256": digest,
        "artifacts": [describe(path) for path in sorted(copied)],
    }
    PROVENANCE.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"built from {reference} ({sha[:12]}), source digest {digest[:12]}")
    for entry in record["artifacts"]:
        size = entry["size_bytes"]
        print(f"  {entry['filename']}  {size:,} B  sha256 {entry['sha256'][:16]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the distributions from a git ref and record where they came from.",
    )
    parser.add_argument(
        "--ref",
        default="HEAD",
        help="the git ref to build from (default: HEAD)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify dist/ came from --ref instead of building",
    )
    parser.add_argument(
        "--no-isolation",
        action="store_true",
        help="pass --no-isolation to `python -m build` (needs the backend already installed)",
    )
    arguments = parser.parse_args(argv)

    try:
        if arguments.check:
            return check(arguments.ref)
        return build(arguments.ref, arguments.no_isolation)
    except BuildError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
