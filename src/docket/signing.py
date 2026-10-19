"""Sealing (HMAC) and signing (Ed25519) of receipts.

Two different promises, deliberately kept apart:

``seal``
    HMAC-SHA256 with a key held by the operator. Cheap, dependency-free and good
    for *integrity*: nobody can change the receipt without the key. It does **not**
    give third-party verifiability, because verifying requires the same secret
    that produces it.

``sign``
    Ed25519 over the receipt core. Anyone with the public key can verify, and the
    public key is embedded in the receipt, so a reviewer can check that the
    holder of a specific key attested to this exact record. Requires the optional
    ``cryptography`` package; Docket degrades gracefully and says so rather than
    inventing home-made cryptography.

Docket never rolls its own cryptographic primitives.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Any

from .canon import canonical_bytes
from .util import atomic_write

ALG_SEAL = "hmac-sha256"
ALG_SIGN = "ed25519"


def new_seal_key() -> str:
    return secrets.token_hex(32)


def key_id(key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(key_hex)).hexdigest()[:16]


def seal(receipt_core: dict[str, Any], key_hex: str) -> dict[str, Any]:
    value = hmac.new(bytes.fromhex(key_hex), canonical_bytes(receipt_core), hashlib.sha256).hexdigest()
    return {"alg": ALG_SEAL, "key_id": key_id(key_hex), "value": value}


def verify_seal(receipt_core: dict[str, Any], sealed: dict[str, Any], key_hex: str) -> bool:
    """Check a receipt's HMAC seal against ``key_hex``.

    Total by contract, for the same reason as :func:`verify_signature`: the sealed block comes
    from whoever supplied the receipt, so anything that is not a ``{"key_id", "value"}`` object
    is ``False`` rather than an ``AttributeError``. A *key* that is not hex is different — that
    is the verifier's own input, and it is reported as ``ValueError`` naming the problem.
    """
    if not isinstance(sealed, dict):
        return False
    try:
        expected = seal(receipt_core, key_hex)
    except ValueError as exc:
        raise ValueError(f"the seal key is not usable ({exc}); expected a hex string") from exc
    value = sealed.get("value")
    if not isinstance(value, str):
        return False
    return hmac.compare_digest(expected["value"], value) and expected["key_id"] == sealed.get("key_id")


def load_or_create_seal_key(key_path: str | os.PathLike[str]) -> str:
    """Load the local sealing key, creating it owner-only if it does not exist.

    A sealing key that other users can read is not a seal, so this both creates the key
    with mode 0600 and refuses to *use* one that is group- or world-readable, naming the
    file and the fix. Silently accepting a world-readable key would make every receipt
    produced with it look sealed while being forgeable by anyone on the machine.
    """
    path = Path(key_path)
    if path.exists():
        if path.is_symlink():
            raise ValueError(f"refusing to use {path}: a sealing key must not be a symbolic link")
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ValueError(
                f"refusing to use {path}: it is readable by other users (mode {mode:o}). "
                f"Fix it with `chmod 600 {path}` — a key anyone on the machine can read is not a seal."
            )
        return path.read_text(encoding="utf-8").strip()
    key = new_seal_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    if hasattr(os, "O_NOFOLLOW"):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
    else:  # pragma: no cover - platform dependent
        atomic_write(path, key + "\n")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return key


# --------------------------------------------------------------------------- #
# Ed25519 (optional dependency)
# --------------------------------------------------------------------------- #


def ed25519_available() -> bool:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def generate_keypair(private_path: str | os.PathLike[str]) -> str:
    """Create an Ed25519 keypair; returns the public key as hex."""
    if not ed25519_available():  # pragma: no cover - environment dependent
        raise RuntimeError("Ed25519 signing needs the optional 'cryptography' package: pip install 'docket[sign]'")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = Path(private_path)
    atomic_write(path, pem)
    with contextlib.suppress(OSError):  # pragma: no cover - platform dependent
        os.chmod(path, 0o600)
    public = key.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return public.hex()


def sign(receipt_core: dict[str, Any], private_path: str | os.PathLike[str]) -> dict[str, Any]:
    if not ed25519_available():  # pragma: no cover - environment dependent
        raise RuntimeError("Ed25519 signing needs the optional 'cryptography' package")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        # `load_pem_private_key` returns a union of every key type the library supports, and most
        # of them have no `.sign(data)` — RSA needs padding and an algorithm, DSA needs an
        # algorithm, ECDSA needs a signature algorithm. Refusing here names the key that was
        # supplied; letting it through would be an AttributeError from inside the library, which
        # tells a reader nothing about which file is wrong.
        raise ValueError(
            f"{private_path} holds a {type(key).__name__}, not an Ed25519 private key; "
            "generate one with `docket keygen` or OpenSSL's `genpkey -algorithm ed25519`"
        )
    signature = key.sign(canonical_bytes(receipt_core))
    public = key.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    return {"alg": ALG_SIGN, "public_key": public.hex(), "value": base64.b16encode(signature).decode().lower()}


def verify_signature(receipt_core: dict[str, Any], signature: dict[str, Any]) -> bool:
    """Check an Ed25519 signature over ``receipt_core``.

    Total by contract: the signature block arrives inside the receipt, so a malformed one (a
    number where the object belongs, a hex string of the wrong length, an algorithm this build
    does not know) is ``False`` — a verdict — and never an exception. Only the *environment* can
    raise, and that is a ``RuntimeError`` the caller turns into a ``skip``: a missing optional
    dependency is not the receipt's fault.
    """
    if not isinstance(signature, dict):
        return False
    if signature.get("alg") != ALG_SIGN:
        return False
    public_hex = signature.get("public_key")
    value_hex = signature.get("value")
    if not isinstance(public_hex, str) or not isinstance(value_hex, str):
        return False
    if not ed25519_available():  # pragma: no cover - environment dependent
        raise RuntimeError("verifying an Ed25519 signature needs the 'cryptography' package")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        public.verify(bytes.fromhex(value_hex), canonical_bytes(receipt_core))
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False
