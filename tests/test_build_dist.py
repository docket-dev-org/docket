"""Guards for `tools/build_dist.py`, which builds the distributions from a git ref.

The tool exists because of a specific, measured failure: the 0.1.4 wheel and sdist were built
from a working tree, two commits landed, and the upload carried the earlier content. PyPI
reserves a filename once it is published, so the published long description could not be
corrected and a new release was the only remedy. The 0.1.5 sdist repeated it at a smaller
scale, one commit behind the `.gitignore` that tags it.

These tests run offline against a scratch git repository, so they need neither `build` nor
network, and they fail on the property that matters rather than on the tool's plumbing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Literal

import pytest

TOOL = Path(__file__).resolve().parent.parent / "tools" / "build_dist.py"


def _load_tool() -> types.ModuleType:
    """Import `tools/build_dist.py` by path, the way the other tool tests do.

    Registered in `sys.modules` before execution for the same reason `test_deploy_check.py`
    registers its checker: loading a module without registering it is the documented way to
    break anything that looks its own module up.
    """
    name = "build_dist_under_test"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bd = _load_tool()


def _git(repository: Path, *arguments: str) -> str:
    """Run git in a scratch repository and return its stdout."""
    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell, no user input
        ["git", *arguments],  # noqa: S607 - resolved from PATH by design
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def scratch_repo(tmp_path: Path) -> Path:
    """A git repository with one commit, whose identity the tests do not depend on."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Docket tests")
    (repository / "README.md").write_text("one\n", encoding="utf-8")
    (repository / "nested").mkdir()
    (repository / "nested" / "data.txt").write_text("two\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "--quiet", "-m", "first")
    return repository


def test_the_tool_exists_and_declares_its_contract() -> None:
    assert TOOL.is_file(), f"{TOOL} is missing"
    text = TOOL.read_text(encoding="utf-8")
    assert "git archive" in text, "the tool must build from a ref, not from the working tree"
    assert "--check" in text, "the tool must offer a way to prove dist/ matches a ref"


def test_a_ref_that_names_nothing_is_refused_rather_than_guessed(scratch_repo: Path) -> None:
    """A typo in a release ref must stop the release, not build whatever HEAD happens to be."""
    with pytest.raises(bd.BuildError, match="is not a ref"):
        bd.resolve_ref("v9.9.9-does-not-exist", scratch_repo)


def test_a_ref_resolves_to_the_commit_it_names(scratch_repo: Path) -> None:
    head = _git(scratch_repo, "rev-parse", "HEAD")
    assert bd.resolve_ref("HEAD", scratch_repo) == head
    assert bd.resolve_ref(head[:8], scratch_repo) == head


def test_an_export_carries_only_what_the_commit_carries(scratch_repo: Path) -> None:
    """The property the whole tool rests on: an untracked file does not leak into the build."""
    destination = scratch_repo.parent / "exported"
    bd.export_tree(bd.resolve_ref("HEAD", scratch_repo), destination, scratch_repo)

    assert (destination / "README.md").read_text(encoding="utf-8") == "one\n"
    assert (destination / "nested" / "data.txt").read_text(encoding="utf-8") == "two\n"

    # Written after the commit, so it is untracked. An export that contains it is building
    # from the working tree, which is the defect this tool was written to end.
    (scratch_repo / "untracked-secret.txt").write_text("must not ship\n", encoding="utf-8")
    destination_two = scratch_repo.parent / "exported-two"
    bd.export_tree(bd.resolve_ref("HEAD", scratch_repo), destination_two, scratch_repo)
    assert not (destination_two / "untracked-secret.txt").exists()


def test_an_edited_tracked_file_exports_at_the_committed_version(scratch_repo: Path) -> None:
    """A dirty tracked file must not reach the artifact either."""
    (scratch_repo / "README.md").write_text("EDITED AND NOT COMMITTED\n", encoding="utf-8")
    destination = scratch_repo.parent / "exported-dirty"
    bd.export_tree(bd.resolve_ref("HEAD", scratch_repo), destination, scratch_repo)
    assert (destination / "README.md").read_text(encoding="utf-8") == "one\n"


def test_the_source_digest_is_stable_across_two_exports_of_one_commit(scratch_repo: Path) -> None:
    """A digest that moved between two exports of the same commit could not answer its question."""
    first = scratch_repo.parent / "digest-one"
    second = scratch_repo.parent / "digest-two"
    for destination in (first, second):
        bd.export_tree(bd.resolve_ref("HEAD", scratch_repo), destination, scratch_repo)
    assert bd.source_digest(first) == bd.source_digest(second)


def test_the_source_digest_moves_when_the_commit_does(scratch_repo: Path) -> None:
    before = scratch_repo.parent / "digest-before"
    bd.export_tree(bd.resolve_ref("HEAD", scratch_repo), before, scratch_repo)
    original = bd.source_digest(before)

    (scratch_repo / "README.md").write_text("two\n", encoding="utf-8")
    _git(scratch_repo, "add", "-A")
    _git(scratch_repo, "commit", "--quiet", "-m", "second")

    after = scratch_repo.parent / "digest-after"
    bd.export_tree(bd.resolve_ref("HEAD", scratch_repo), after, scratch_repo)
    assert bd.source_digest(after) != original


def test_a_member_escaping_the_destination_is_refused(scratch_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tar-slip guard. Injected rather than constructed, because git archive never emits one."""

    class _Escaping:
        name = "../escaped.txt"

    class _Bundle:
        def __enter__(self) -> _Bundle:
            return self

        def __exit__(self, *_args: object) -> Literal[False]:
            return False

        def getmembers(self) -> list[_Escaping]:
            return [_Escaping()]

        def extractall(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover
            raise AssertionError("extractall must not be reached once a member is refused")

    monkeypatch.setattr(bd.tarfile, "open", lambda *_args, **_kwargs: _Bundle())
    with pytest.raises(bd.BuildError, match="escapes the destination"):
        bd.export_tree(bd.resolve_ref("HEAD", scratch_repo), scratch_repo.parent / "never", scratch_repo)


def test_a_provenance_record_for_the_wrong_commit_is_refused(
    scratch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect itself: artifacts built from an earlier commit must not pass as this release."""
    monkeypatch.setattr(bd, "REPO", scratch_repo)
    monkeypatch.setattr(bd, "DIST", tmp_path / "dist")
    monkeypatch.setattr(bd, "PROVENANCE", tmp_path / "dist" / "PROVENANCE.json")

    head = bd.resolve_ref("HEAD", scratch_repo)
    bd.PROVENANCE.parent.mkdir(parents=True, exist_ok=True)
    bd.PROVENANCE.write_text(json.dumps({"ref": "HEAD", "commit": "0" * 40, "artifacts": []}), encoding="utf-8")
    assert bd.check("HEAD") == 1

    # Recording the right commit, with the artifacts unchanged, passes.
    bd.PROVENANCE.write_text(json.dumps({"ref": "HEAD", "commit": head, "artifacts": []}), encoding="utf-8")
    assert bd.check("HEAD") == 0


def test_an_artifact_that_changed_after_the_build_is_refused(
    scratch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The check resolves the ref before it looks at the artifacts, so the repository has to be
    # one where HEAD exists. Pointing it at a directory that is not a repository would make the
    # test pass for the wrong reason: an unresolvable ref rather than a changed artifact.
    monkeypatch.setattr(bd, "REPO", scratch_repo)
    monkeypatch.setattr(bd, "DIST", tmp_path / "dist")
    monkeypatch.setattr(bd, "PROVENANCE", tmp_path / "dist" / "PROVENANCE.json")
    bd.DIST.mkdir(parents=True)

    artifact = bd.DIST / "docket_ai-0.0.0-py3-none-any.whl"
    artifact.write_bytes(b"as built\n")
    import hashlib

    recorded = hashlib.sha256(b"as built\n").hexdigest()
    # The commit recorded here is the one HEAD names, so a failure can only be the artifact
    # digest. Recording an unresolvable sha instead would make the test pass on the commit
    # mismatch and never reach the comparison it claims to be testing.
    bd.PROVENANCE.write_text(
        json.dumps(
            {
                "ref": "HEAD",
                "commit": bd.resolve_ref("HEAD", scratch_repo),
                "artifacts": [{"filename": artifact.name, "sha256": recorded, "size_bytes": 9}],
            }
        ),
        encoding="utf-8",
    )

    # Sanity: the record is accepted before the artifact is touched, so the refusal below is
    # about the bytes and not about the provenance.
    assert bd.check("HEAD") == 0

    artifact.write_bytes(b"as edited afterwards\n")
    assert bd.check("HEAD") == 1


def test_a_missing_provenance_record_is_an_error_not_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A check that passed with nothing to check would be worse than no check."""
    monkeypatch.setattr(bd, "PROVENANCE", tmp_path / "dist" / "PROVENANCE.json")
    with pytest.raises(bd.BuildError, match="does not exist"):
        bd.read_provenance()


def test_a_dirty_working_tree_cannot_reach_the_artifact(
    scratch_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 0.1.4 defect, reproduced and refused.

    The repository is edited after its commit, the build is run against the ref, and the
    artifact is asserted to carry the committed content. `python -m build` is replaced by a
    stand-in that packages whatever the exported source says, because the property under test
    is where the bytes come from, not what the backend does with them.
    """
    (scratch_repo / "README.md").write_text("EDITED AND NOT COMMITTED\n", encoding="utf-8")

    def stand_in_for_build(source: Path, outdir: Path, no_isolation: bool) -> list[Path]:
        assert not no_isolation
        artifact = outdir / "fake-0.0.0.tar.gz"
        artifact.write_text((source / "README.md").read_text(encoding="utf-8"), encoding="utf-8")
        return [artifact]

    monkeypatch.setattr(bd, "build_distributions", stand_in_for_build)
    monkeypatch.setattr(bd, "REPO", scratch_repo)
    monkeypatch.setattr(bd, "DIST", tmp_path / "dist")
    monkeypatch.setattr(bd, "PROVENANCE", tmp_path / "dist" / "PROVENANCE.json")

    assert bd.build("HEAD", no_isolation=False) == 0

    shipped = bd.DIST / "fake-0.0.0.tar.gz"
    assert shipped.is_file(), "the build produced nothing"
    assert shipped.read_text(encoding="utf-8") == "one\n", (
        "the artifact carries the working tree rather than the commit, which is the defect this tool exists to end"
    )

    record = json.loads(bd.PROVENANCE.read_text(encoding="utf-8"))
    assert record["ref"] == "HEAD"
    assert record["commit"] == bd.resolve_ref("HEAD", scratch_repo)
    assert [entry["filename"] for entry in record["artifacts"]] == ["fake-0.0.0.tar.gz"]
    assert record["artifacts"][0]["sha256"] == hashlib.sha256(b"one\n").hexdigest()
    assert bd.check("HEAD") == 0


def _fences(markdown: str) -> list[tuple[str, str]]:
    """Every fenced block in a document, as (language, body), in order.

    The language matters to one caller and not to the others. The parse test needs only the
    blocks that declare `bash`, because the guide also shows twine's own 400 output inside a
    plain fence and an error message with an unbalanced quote is not a shell script. The twine
    guards need all of them, because that same plain fence holds a `twine upload` command a
    bash-only reader would miss.
    """
    fences: list[tuple[str, str]] = []
    current: list[str] = []
    language = ""
    inside = False
    for line in markdown.splitlines():
        if line.strip().startswith("```") and not inside:
            inside, language, current = True, line.strip().removeprefix("```").strip(), []
            continue
        if inside and line.strip() == "```":
            fences.append((language, "\n".join(current)))
            inside = False
            continue
        if inside:
            current.append(line)
    return fences


def test_every_bash_block_in_the_publishing_guide_parses() -> None:
    """A command block that does not parse is a command block nobody can run."""
    guide = (Path(__file__).resolve().parent.parent / "PUBLISHING.md").read_text(encoding="utf-8")
    fences = [body for language, body in _fences(guide) if language == "bash"]
    assert fences, "no ```bash blocks found in PUBLISHING.md; the matcher has stopped working"
    for index, fence in enumerate(fences, start=1):
        completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell, no user input
            ["bash", "-n"],  # noqa: S607 - resolved from PATH by design
            input=fence,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"block {index} of PUBLISHING.md does not parse: {completed.stderr.strip()}"


def test_the_guide_never_feeds_the_whole_of_dist_to_twine() -> None:
    """The regression the provenance file introduced.

    `dist/` now holds `PROVENANCE.json` beside the two distributions, and `twine check dist/*`
    refuses a file it does not recognise with *"Unknown distribution format"*. The guide told
    the reader to run exactly that, so the command it shipped was already broken. Both twine
    commands must name the two extensions.
    """
    guide = (Path(__file__).resolve().parent.parent / "PUBLISHING.md").read_text(encoding="utf-8")
    twine_lines = [
        line.strip().removeprefix("$ ")
        for _language, fence in _fences(guide)
        for line in fence.splitlines()
        if line.strip().startswith(("twine check", "twine upload", "$ twine"))
    ]
    assert twine_lines, "no twine command found in PUBLISHING.md; the matcher has stopped working"
    for command in twine_lines:
        # Every path the command hands to twine must be a distribution. The bare wildcard
        # `dist/*` would hand it PROVENANCE.json as well, and twine refuses a file it does
        # not recognise as one.
        for path in re.findall(r"dist/\S*", command):
            assert path.endswith((".whl", ".tar.gz")), (
                f"{command!r} passes {path!r} to twine, which is not a distribution"
            )
        assert "dist/*.whl" in command and "dist/*.tar.gz" in command, f"{command!r} does not name both distributions"


def test_the_guide_sends_the_token_as_the_username_token() -> None:
    """PyPI withdrew username and password authentication from the upload endpoint.

    A token sent under the account name is refused with a 403 that names authentication rather
    than the username, so the guide has to say which username to use. This failed on the 0.1.7
    upload: the token was valid, the account was the right one, and `twine upload` still
    returned `403 Username/Password authentication is no longer supported`.
    """
    guide = (Path(__file__).resolve().parent.parent / "PUBLISHING.md").read_text(encoding="utf-8")
    block = next(fence for _language, fence in _fences(guide) if "twine upload" in fence)
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    assert 'export TWINE_USERNAME="__token__"' in lines, (
        "PUBLISHING.md does not set TWINE_USERNAME to the literal string __token__; "
        "PyPI refuses a token sent under any other username"
    )
    assert not any(line.startswith("export TWINE_USERNAME=") and "__token__" not in line for line in lines), block


def test_the_guide_builds_from_a_ref_before_it_uploads() -> None:
    """The order is the point: build from the ref, check it, then upload."""
    guide = (Path(__file__).resolve().parent.parent / "PUBLISHING.md").read_text(encoding="utf-8")
    block = next(fence for _language, fence in _fences(guide) if "twine upload" in fence)
    lines = [line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert any(line.startswith("python3 tools/build_dist.py --ref") for line in lines), block
    assert any(line == "python3 tools/build_dist.py --check --ref vX.Y.Z" for line in lines), block
    check = lines.index(next(line for line in lines if line == "python3 tools/build_dist.py --check --ref vX.Y.Z"))
    for command in lines[:check]:
        assert not command.startswith("twine"), f"{command!r} runs before the provenance check"


def test_the_command_line_reports_a_bad_ref_with_a_nonzero_status() -> None:
    completed = subprocess.run(  # noqa: S603 - a fixed argv, no shell, no user input
        [sys.executable, str(TOOL), "--check", "--ref", "v0.0.0-not-a-ref"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr
    assert "is not a ref" in completed.stderr
