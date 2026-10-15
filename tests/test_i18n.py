"""Localisation: the format is the same in every locale, and text is never "helpfully" fixed.

Docket's hashed structures must be byte-identical wherever they are produced and verified, and
that is a stronger requirement than usual for internationalisation: a receipt issued in Tokyo
has to verify in Berlin, and a Turkish locale must not change a case-folded model name into a
different allow-list decision halfway through. So the properties under test are:

* no locale-dependent formatting anywhere in the record (dates and numbers are spelled out in
  fixed, ASCII forms — `LC_ALL=tr_TR.UTF-8` must not change a single byte of a digest);
* Unicode is transported, never normalised: NFC and NFD are *different strings* with different
  digests, and an implementation that folded them would invalidate receipts rather than
  "clean them up";
* non-ASCII paths round-trip through a snapshot and a restore byte for byte.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unicodedata import normalize

import pytest

from docket.canon import canonical_json, sha256_hex
from docket.fsdiff import snapshot
from docket.snapshot import apply_restore, plan_restore, take_snapshot

REPO = Path(__file__).resolve().parents[1]


def test_canonical_json_does_not_normalise_unicode():
    """NFC and NFD are different strings. Both are legal; they are not equal.

    This is the Unicode version of "no floating point in hashed structures": a normalisation
    step would be an implementation quietly rewriting the data it is supposed to be freezing, and
    two implementations that normalise differently can never agree on a digest.
    """
    composed = "café"  # e + acute as one code point
    decomposed = normalize("NFD", composed)  # e + combining acute
    assert composed != decomposed
    assert canonical_json({"s": composed}) != canonical_json({"s": decomposed})
    assert sha256_hex(canonical_json({"s": composed})) != sha256_hex(canonical_json({"s": decomposed}))
    # Both survive a round trip through the canonical form unchanged.
    for text in (composed, decomposed):
        assert json.loads(canonical_json({"s": text}))["s"] == text


@pytest.mark.parametrize(
    "text",
    [
        "日本語のプロンプト",
        "🙂 emoji and ✓ marks",
        "עברית with العربية mixed in",
        "combining: e\u0301 vs é",
        "zero width\u200bspace and a null-ish \u0000 escape",
        'line\nbreak\ttab"quote\\backslash',
    ],
)
def test_text_round_trips_through_the_canonical_form(text: str):
    encoded = canonical_json({"prompt": text})
    assert json.loads(encoded)["prompt"] == text
    assert encoded == canonical_json({"prompt": text}), "canonicalisation is deterministic"


def test_the_canonical_form_is_utf8_not_escaped_ascii():
    """`ensure_ascii=True` would be valid JSON and a different digest. The spec fixes UTF-8."""
    encoded = canonical_json({"s": "日本語"})
    assert "日本語" in encoded
    assert "\\u" not in encoded


def _digest_under(locale: str, text: str) -> str:
    """The digest of a fixed structure, computed in a subprocess with a different locale."""
    program = (
        "import json, sys\n"
        "from docket.canon import canonical_json, sha256_hex\n"
        f"text = {text!r}\n"
        "print(json.dumps({'c': canonical_json({'s': text}), 'd': sha256_hex(canonical_json({'s': text}))}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), LC_ALL=locale, LANG=locale, PYTHONHASHSEED="0")
    completed = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=120, cwd=str(REPO)
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


@pytest.mark.parametrize("locale", ["C", "C.UTF-8", "de_DE.UTF-8", "tr_TR.UTF-8", "ja_JP.UTF-8"])
def test_a_digest_does_not_depend_on_the_locale(locale: str):
    """`tr_TR` is in the list on purpose: it is where case-folding rules surprise people — "I"
    lowercases to "ı", not "i" — and a model allow-list match that used a locale-aware fold
    would decide differently in Istanbul than in Berlin."""
    baseline = _digest_under("C", "model:claude-sonnet-5")
    assert _digest_under(locale, "model:claude-sonnet-5") == baseline


def test_the_whole_demo_verifies_under_a_non_c_locale(tmp_path, monkeypatch):
    """End to end: a run and its verification, in a locale with a different number format."""
    monkeypatch.chdir(tmp_path)
    program = (
        "import io, json, pathlib\n"
        "from docket.demo import run_demo\n"
        "outcome = run_demo(color=False, out=io.StringIO())\n"
        "assert outcome['verify_ok'] is True, outcome\n"
        "print(json.dumps({'totals': str(outcome['cost_usd']),"
        " 'number': pathlib.Path(outcome['receipt_path']).name.split('.')[0]}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), LC_ALL="de_DE.UTF-8", LANG="de_DE.UTF-8")
    completed = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=300, cwd=str(tmp_path)
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.splitlines()[-1])
    # `3,00` would be the German rendering of the same number; the record is always the machine
    # form (`3.00`), because the receipt is data before it is prose.
    assert "," not in payload["totals"], payload["totals"]
    assert Decimal(payload["totals"]) > 0
    assert payload["number"].startswith("DKT-"), payload["number"]


def test_non_ascii_paths_survive_a_snapshot_and_a_restore(tmp_path: Path):
    """A filename is evidence too. The manifest is UTF-8 JSON, the plan is derived from it, and
    the restore has to put the bytes back at the same name — not at a mojibake version of it."""
    tree = tmp_path / "tree"
    tree.mkdir()
    names = {
        "naïve.txt": "accent\n",
        "日本語.txt": "japanese\n",
        "emoji-🙂.txt": "emoji\n",
        "עברית.txt": "rtl\n",
    }
    for name, content in names.items():
        (tree / name).write_text(content, encoding="utf-8")
    store = tmp_path / "store"
    taken = take_snapshot(roots=["."], ignore=[], root=tree, store=store)

    # The manifest is valid UTF-8 JSON with the names spelled exactly as they are on disk.
    raw = (store / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    assert {entry["path"] for entry in manifest["entries"]} == set(names)
    assert "\\u" not in raw, "names are written as UTF-8, not as escapes"

    for name in names:
        (tree / name).write_text("damaged\n", encoding="utf-8")
    changes = [{"path": name, "change": "modified", "after": {"sha256": "0" * 64, "size": 8}} for name in names]
    plan = plan_restore(manifest, changes)
    applied = apply_restore(plan, root=tree, store=store, force=True)
    assert [item["status"] for item in applied] == ["restored"] * len(names)

    for name, content in names.items():
        assert (tree / name).read_text(encoding="utf-8") == content
    assert sorted(path.name for path in tree.iterdir()) == sorted(names), "no mojibake twins"
    assert len(taken.entries) == len(names)


def test_the_filesystem_diff_reports_and_matches_non_ascii_paths(tmp_path: Path):
    """Ignore patterns are globs over names; a name the matcher mangles would silently ignore
    the wrong file — the same class of failure as the absolute-path defect, one alphabet over."""
    tree = tmp_path / "tree"
    (tree / "keep").mkdir(parents=True)
    (tree / "keep" / "日本語.txt").write_text("one\n", encoding="utf-8")
    (tree / "ignore").mkdir()
    (tree / "ignore" / "日本語.txt").write_text("two\n", encoding="utf-8")
    state = snapshot(["."], ignore=["ignore/**"], cwd=str(tree))
    assert sorted(state) == ["keep/日本語.txt"], sorted(state)

    from docket.fsdiff import diff

    (tree / "keep" / "日本語.txt").write_text("changed\n", encoding="utf-8")
    after = snapshot(["."], ignore=["ignore/**"], cwd=str(tree))
    changes = diff(state, after)
    assert [change["path"] for change in changes] == ["keep/日本語.txt"]
