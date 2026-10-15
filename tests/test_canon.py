"""Canonical serialisation, digests and Merkle proofs.

The Merkle expectations in this file are computed by an independent implementation
written inline (not by calling ``docket.canon``), so a change in tree construction
cannot silently agree with itself.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from docket.canon import (
    canonical_bytes,
    canonical_json,
    merkle_proof,
    merkle_root,
    parse_usd,
    sha256_hex,
    usd,
    verify_merkle_proof,
)


def _independent_merkle(hexdigests: list[str]) -> str:
    """RFC 6962 tree hash, implemented here on purpose, independently of the library."""
    if not hexdigests:
        return hashlib.sha256(b"").hexdigest()
    level = [hashlib.sha256(b"\x00" + bytes.fromhex(h)).digest() for h in hexdigests]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(hashlib.sha256(b"\x01" + level[i] + level[i + 1]).digest())
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0].hex()


def test_canonical_json_sorts_keys_and_strips_whitespace():
    assert canonical_json({"b": 1, "a": [2, {"d": 3, "c": 4}]}) == '{"a":[2,{"c":4,"d":3}],"b":1}'


def test_canonical_json_preserves_non_ascii_literally():
    assert canonical_json({"k": "café — ☕"}) == '{"k":"café — ☕"}'


def test_canonical_json_is_stable_under_key_insertion_order():
    a = {"x": 1, "y": {"p": 2, "q": 3}}
    b = {"y": {"q": 3, "p": 2}, "x": 1}
    assert canonical_json(a) == canonical_json(b)
    assert sha256_hex(canonical_json(a)) == sha256_hex(canonical_json(b))


def test_canonical_json_rejects_unserialisable_objects():
    with pytest.raises(TypeError):
        canonical_json({"bad": object()})


def test_integral_floats_are_normalised_to_integers():
    """Python prints 5.0 as "5.0" and JavaScript as "5". Only one may win."""
    assert canonical_json({"seconds": 5.0}) == '{"seconds":5}'
    assert canonical_json({"nested": [1.0, 2.0]}) == '{"nested":[1,2]}'


def test_non_integer_floats_are_rejected_loudly():
    """The bug class that this rule exists to prevent: silent cross-language drift."""
    with pytest.raises(TypeError) as excinfo:
        canonical_json({"ratio": 0.8})
    assert "decimal string" in str(excinfo.value)
    with pytest.raises(TypeError):
        canonical_json({"nested": {"deep": [1, 2, 2.5]}})


def test_num_helper_encodes_fractions_as_strings():
    from docket.canon import num

    assert num(5.0) == 5
    assert num(800) == 800
    assert num(0.8) == "0.8"
    assert canonical_json({"warn_at": num(0.8)}) == '{"warn_at":"0.8"}'


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("2.0"), 2),  # an integral Decimal is an integer, never "2.0"
        (Decimal("0"), 0),
        # One and a half is not an integer, so it stays a string — normalised, so "1.50" and
        # "1.5" cannot both exist in a hashed structure. Money is a different question, and
        # `usd()` is what fixes a money string's shape (two decimals, or six).
        (Decimal("1.50"), "1.5"),
        (Decimal("0.25"), "0.25"),
        (Decimal("0.018000"), "0.018"),
        ("1.5", "1.5"),  # a string is already encoded; `num` must not re-parse it
        ("1200", "1200"),
        (-3, -3),
        (-0.5, "-0.5"),
    ],
)
def test_num_helper_handles_every_input_shape_it_documents(value, expected):
    """Every branch of `num` is a branch of *recording*, and `num` returning `None` would be a
    fact turning into null inside a hashed event.

    Found by `tools/mutate.py`: the string and `Decimal` branches could each be replaced by
    `return None` without a test noticing, because the existing cases were floats and ints.
    """
    from docket.canon import num

    encoded = num(value)
    assert encoded == expected, f"num({value!r}) == {encoded!r}, expected {expected!r}"
    assert type(encoded) is type(expected), f"num({value!r}) changed type to {type(encoded).__name__}"
    # Whatever it returns has to be canonically encodable: that is `num`'s whole job.
    assert json.loads(canonical_json({"v": encoded}))["v"] == expected


def test_digest_of_is_the_digest_of_the_canonical_form():
    """`digest_of` is the one-liner every other digest in the project sits on, and it had no test
    of its own (found by `tools/mutate.py`: replacing its body with `return None` changed no
    outcome in the suite). It is asserted against the composition it claims to be, in both
    directions: the value, and the property that key order cannot change it."""
    from docket.canon import digest_of

    value = {"b": [1, 2, {"c": "x"}], "a": None}
    assert digest_of(value) == sha256_hex(canonical_bytes(value))
    assert digest_of({"a": None, "b": [1, 2, {"c": "x"}]}) == digest_of(value)
    assert digest_of({"a": None, "b": [1, 2, {"c": "y"}]}) != digest_of(value)
    assert digest_of({}) == sha256_hex(b"{}")
    # A digest is lowercase hex of a 32-byte hash, never a repr or a prefixed string.
    assert len(digest_of(value)) == 64 and digest_of(value) == digest_of(value).lower()


@pytest.mark.parametrize("root", [None, 123, ["a" * 64], True, Decimal("1")])
def test_a_merkle_root_that_is_not_a_hex_string_is_refused(root):
    """The guard is `not isinstance(root, str) or not root`, and the two halves cover two
    different lies: a root that is not a string at all, and a root that is an empty one. Only the
    empty case was tested, so `or` -> `and` left a non-string root to fail later with an
    `AttributeError` out of `.lower()` — a crash where the contract says `ValueError`.
    """
    from docket.canon import verify_merkle_proof

    leaf = sha256_hex(b"leaf")
    path = [{"side": "left", "hash": "b" * 64}]
    with pytest.raises(ValueError):
        verify_merkle_proof(leaf, path, root)


def test_an_empty_merkle_root_is_refused_and_a_short_one_simply_does_not_prove():
    """Two shapes of bad root, two different answers — and the difference is the contract.

    An empty root is not a root, so it is a `ValueError` (the caller passed something that cannot
    be one). A short-but-real hex string is a root that cannot match, so the answer is `False`:
    "this does not prove" rather than "you called me wrong". Collapsing the two would make a
    verifier that raises on a doctored receipt instead of reporting it.
    """
    from docket.canon import verify_merkle_proof

    leaf = sha256_hex(b"leaf")
    path = [{"side": "left", "hash": "b" * 64}]
    with pytest.raises(ValueError, match="merkle root"):
        verify_merkle_proof(leaf, path, "")
    assert verify_merkle_proof(leaf, path, "0") is False
    assert verify_merkle_proof(leaf, path, "0" * 64) is False
    assert verify_merkle_proof(leaf, path, sha256_hex(b"not this leaf")) is False


@pytest.mark.parametrize(
    "digests",
    [
        [],
        ["a" * 64],
        ["a" * 64, "b" * 64],
        ["a" * 64, "b" * 64, "c" * 64],
        [f"{i:064x}" for i in range(1, 12)],
    ],
)
def test_merkle_root_matches_independent_implementation(digests):
    assert merkle_root(digests) == _independent_merkle(digests)


def test_merkle_root_of_empty_tree_is_sha256_of_empty_string():
    assert merkle_root([]) == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize("count", [1, 2, 3, 5, 8, 13])
def test_merkle_proofs_verify_for_every_leaf(count):
    digests = [sha256_hex(f"leaf-{i}") for i in range(count)]
    root = merkle_root(digests)
    for index, digest in enumerate(digests):
        proof = merkle_proof(digests, index)
        assert verify_merkle_proof(digest, proof, root), index


def test_merkle_proof_rejects_a_forged_leaf():
    digests = [sha256_hex(f"leaf-{i}") for i in range(5)]
    root = merkle_root(digests)
    proof = merkle_proof(digests, 2)
    assert not verify_merkle_proof(sha256_hex("forged"), proof, root)


def test_merkle_proof_index_bounds():
    with pytest.raises(IndexError):
        merkle_proof([sha256_hex("a")], 3)


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0.00"), ("1.5", "1.50"), ("1.234567", "1.234567"), ("0.000001", "0.000001")],
)
def test_usd_formatting_is_canonical(value, expected):
    assert usd(value) == expected


def test_usd_round_trip():
    for value in ("0.00", "12.50", "0.000001", "1234.567890"):
        assert usd(parse_usd(value)) == value


def test_usd_has_only_two_canonical_shapes():
    """Whole cents use 2 decimals; anything finer uses exactly 6. Never anything else."""
    for value in ("1.5", "2", "1234.56789", "0.0000010", "10.10"):
        rendered = usd(value)
        decimals = len(rendered.split(".")[1])
        assert decimals in {2, 6}
        assert usd(parse_usd(rendered)) == rendered


def test_parse_usd_rejects_floats_and_junk():
    for bad in [1.0, "one", "1.2.3", "-", ""]:
        with pytest.raises(ValueError):
            parse_usd(bad)  # type: ignore[arg-type]
