"""The URL guards: what Docket's own clients are allowed to fetch.

Every ``urlopen`` in the package passes its URL through one of two guards, and the guards are
the answer to the static-analysis finding that flags an unchecked URL as a possible SSRF or a
local-file read. So the guards get tests of their own — including the hostname shapes that are
designed to look local and are not.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

from docket.util import http_url, local_http_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/openai/v1/chat/completions",
        "http://127.0.0.5:9/",
        "http://localhost:41234/docket/event",
        "http://[::1]:8080/x",
        "https://127.0.0.1/x",
    ],
)
def test_local_addresses_are_allowed(url: str):
    assert local_http_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://127.0.0.1/x",
        "gopher://127.0.0.1/x",
        "data:text/plain,hello",
        "http:///no-host",
        "http://0.0.0.0:8080/x",  # a bind address, not a destination
        "http://10.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",  # the cloud metadata address
        "http://[fe80::1]/x",
        "http://localhost.evil.example/x",  # a name that *starts* with localhost
        "http://localhost@evil.example/x",  # userinfo, so the host is evil.example
        "http://127.0.0.1.evil.example/x",
        "http://2130706433/x",  # 127.0.0.1 as a decimal integer
        "http://0x7f000001/x",
    ],
)
def test_anything_that_is_not_this_machine_is_refused(url: str):
    with pytest.raises(ValueError):
        local_http_url(url)


@pytest.mark.parametrize("url", ["http://api.openai.com/v1", "https://api.anthropic.com/v1"])
def test_the_weaker_guard_allows_remote_hosts_but_still_insists_on_http(url: str):
    """Agent-side code talks to whatever provider it is pointed at — but never to a file."""
    assert http_url(url) == url


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x/y", "http:///nohost", ""])
def test_the_weaker_guard_refuses_everything_else(url: str):
    with pytest.raises(ValueError):
        http_url(url)


def test_the_sdk_refuses_a_boundary_url_that_is_not_local(monkeypatch):
    """A non-local boundary would divert the agent's own event stream — refuse at setup, not
    silently at the first event. The SDK's contract is "attach to the supervised run", and
    that run's boundary is loopback."""
    from docket.sdk import _BoundaryRecorder

    monkeypatch.setenv("DOCKET_PROXY_URL", "http://collector.example:9000")
    with pytest.raises(ValueError) as caught:
        _BoundaryRecorder("http://collector.example:9000")
    assert "local addresses" in str(caught.value)

    recorder = _BoundaryRecorder("http://127.0.0.1:9/")  # constructing is the check; no request
    assert recorder.base_url == "http://127.0.0.1:9"


# --- the invariant, kept by static analysis -------------------------------------------
#
# The guards are only worth something if every call site uses one, and a new `urlopen` added
# six months from now is exactly how that stops being true. This walks the package's own syntax
# tree and resolves each `urlopen` argument back to its source: a guard call, or a `Request`
# whose URL came from one. It is the test that makes the SECURITY.md sentence "every urlopen in
# the repository goes through a guard" a checked fact rather than a promise.


def _urlopen_targets(path: Path) -> Iterator[tuple[int, ast.AST, dict[str, ast.AST]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignments: dict[str, ast.AST] = {}
    calls: list[tuple[int, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and node.value is not None:
                    previous = assignments.get(target.id)
                    if previous is None or getattr(previous, "lineno", 0) < node.lineno:
                        assignments[target.id] = node.value
        if isinstance(node, ast.Call) and _dotted(node.func) == "urllib.request.urlopen" and node.args:
            calls.append((node.lineno, node.args[0]))
    for lineno, argument in calls:
        yield lineno, argument, assignments


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _resolves_to_a_guard(node: ast.AST, assignments: dict[str, ast.AST], depth: int = 0) -> bool:
    if depth > 4:  # pragma: no cover - defensive against a pathological source file
        return False
    if isinstance(node, ast.Name):
        source = assignments.get(node.id)
        return source is not None and _resolves_to_a_guard(source, assignments, depth + 1)
    if isinstance(node, ast.Call):
        name = _dotted(node.func)
        if name in {"http_url", "local_http_url"}:
            return True
        if name.endswith(".Request") and node.args:
            return _resolves_to_a_guard(node.args[0], assignments, depth + 1)
    return False


def test_every_urlopen_in_the_package_resolves_to_a_guard():
    package = Path(__file__).resolve().parents[1] / "src" / "docket"
    unguarded: list[str] = []
    sites = 0
    files = 0
    for path in sorted(package.rglob("*.py")):
        parsed = list(_urlopen_targets(path))
        if not parsed:
            continue
        files += 1
        for lineno, target, assignments in parsed:
            sites += 1
            if not _resolves_to_a_guard(target, assignments):
                unguarded.append(f"{path.name}:{lineno} {ast.unparse(target)[:80]}")
    assert sites >= 5, "the walk found no call sites; it would pass vacuously"
    assert not unguarded, "urlopen without a guard: " + "; ".join(unguarded)
    # A sanity check on the walk itself: the files that make the calls are the files examined.
    assert files >= 4, f"only {files} file(s) with a urlopen were found"


def test_the_walk_actually_flags_an_unguarded_call(tmp_path: Path):
    """A test that cannot fail is not a test. This runs the same walk over a module that *does*
    have the defect, and over one that has it fixed, so the check above is known to discriminate.
    """
    unguarded = tmp_path / "bad.py"
    unguarded.write_text(
        "import urllib.request\n\n"
        "def fetch(user_supplied):\n"
        "    request = urllib.request.Request(user_supplied)\n"
        "    return urllib.request.urlopen(request, timeout=5)\n",
        encoding="utf-8",
    )
    sites = list(_urlopen_targets(unguarded))
    assert len(sites) == 1
    lineno, target, assignments = sites[0]
    assert lineno == 5
    assert not _resolves_to_a_guard(target, assignments), "an unguarded call must not pass"

    guarded = tmp_path / "good.py"
    guarded.write_text(
        "import urllib.request\n\n"
        "from docket.util import http_url\n\n"
        "def fetch(user_supplied):\n"
        "    request = urllib.request.Request(http_url(user_supplied))\n"
        "    return urllib.request.urlopen(request, timeout=5)\n",
        encoding="utf-8",
    )
    _, target, assignments = next(iter(_urlopen_targets(guarded)))
    assert _resolves_to_a_guard(target, assignments)
