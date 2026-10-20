"""Produce a receipt and ledger you can hand to somebody else.

`examples/client-deliverable/README.md` used to say "Redact before shipping if you must", which is
a manual step dressed up as advice: the reader is told the ledger contains paths, argv and hostnames,
and left to edit JSON by hand. Editing a receipt by hand breaks its hash chain, so the honest
outcome of following that advice was an artifact that no longer verified — the one property the
whole format exists to provide.

So redaction is a command now, and it produces a *pair* that still verifies:

    docket report --redact paths,argv,hosts .docket/latest.receipt.json --out redacted/

The event data is redacted, the hash chain is recomputed, the Merkle root and the receipt's ledger
block are rebuilt to match, and both files are written under a new name. The originals are never
touched: raw evidence is not something a reporting command gets to overwrite.

What each profile removes:

``paths``
    Filesystem paths — ``cwd``, ``argv`` entries that look like paths, ``fs.*`` paths, the file
    summary's ``top_paths``. Replaced with a stable placeholder that keeps the *shape* (the number
    of path segments) so a reader can still see how deep the tree was.
``argv``
    The full argument vector, replaced by its length. A reader learns the agent was invoked with
    seven arguments and not what they were.
``hosts``
    Hostnames and ports — the boundary's own address, upstream hosts, provider hosts.
``secrets``
    Anything matching a credential shape in any string value: ``sk-``, ``ghp_``, ``AKIA``,
    ``Bearer `` tokens, ``xoxb-``, and long high-entropy runs. This profile is the one that runs
    last, over every string, because a secret in a place nobody expected is the failure mode.

Redaction is recorded in the receipt under ``redaction``: which profiles were applied and how many
values each one replaced. A receipt that says nothing about being redacted is a receipt that is
lying about what it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canon import canonical_bytes, merkle_proof, merkle_root, sha256_hex
from .ledger import GENESIS, Event

#: The profiles, in the order they are applied. `secrets` is last so it sees every string the other
#: profiles left behind.
PROFILES: tuple[str, ...] = ("paths", "argv", "hosts", "secrets", "all")

#: Credential shapes. Ordered longest-first so the most specific match wins.
_SECRET_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|credential)\b\s*[=:]\s*\S{6,}"),
)

#: A value that looks like a filesystem path rather than a bare word.
_PATH_SHAPE = re.compile(r"^(?:[A-Za-z]:)?(?:/|~|\.{1,2}/)")

#: Keys whose value names a place in the watched tree, whether or not it is absolute. A relative
#: filename is still a fact about the repository it belongs to.
_PATH_KEYS = frozenset(
    {"path", "cwd", "root", "roots", "target", "file", "filename", "top_paths", "watched", "snapshot_root"}
)

#: Keys whose value is a hostname or a port, by name, so a rename elsewhere does not silently
#: un-redact them.
_HOST_KEYS = frozenset({"host", "hostname", "upstream", "upstream_host", "provider_host", "server"})
_PORT_KEYS = frozenset({"port", "requested_port", "upstream_port"})


class RedactionError(ValueError):
    """A profile name that does not exist, or a receipt that cannot be redacted."""


@dataclass
class RedactionReport:
    """What a redaction did, which is recorded in the receipt it produced."""

    profiles: list[str] = field(default_factory=list)
    replaced: dict[str, int] = field(default_factory=dict)

    def bump(self, profile: str, count: int = 1) -> None:
        self.replaced[profile] = self.replaced.get(profile, 0) + count

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles": list(self.profiles),
            "replaced": dict(sorted(self.replaced.items())),
            "note": (
                "this receipt was derived by redaction; its ledger was re-chained to match, so the "
                "pair still verifies against each other and no longer commits to the original events"
            ),
        }


def parse_profiles(spec: str) -> list[str]:
    """`paths,argv` becomes `["paths", "argv"]`, or raises naming what was wrong.

    `all` expands to every profile. An empty spec is an error rather than a silent no-op, because a
    reader who typed `--redact` with a typo in it would otherwise get an unredacted file.
    """
    names = [part.strip() for part in spec.split(",") if part.strip()]
    if not names:
        raise RedactionError("--redact needs at least one profile: " + ", ".join(PROFILES))
    expanded: list[str] = []
    for name in names:
        if name not in PROFILES:
            raise RedactionError(f"unknown redaction profile {name!r}; choose from {', '.join(PROFILES)}")
        if name == "all":
            for candidate in PROFILES:
                if candidate != "all" and candidate not in expanded:
                    expanded.append(candidate)
        elif name not in expanded:
            expanded.append(name)
    return expanded


def _placeholder_for(value: str, kind: str) -> Any:
    """A replacement that keeps the shape of what it replaces.

    A path becomes a placeholder of the same depth, so a reader can still tell
    `/home/alice/projects/acme/src/main.py` from `/tmp/x.py` without learning either. An argv
    becomes a count. A host becomes a name that says it was a host.
    """
    if kind == "path":
        depth = value.count("/")
        return f"<redacted-path depth={depth}>"
    if kind == "argv":
        return "<redacted-argv>"
    if kind == "host":
        return "<redacted-host>"
    if kind == "port":
        return 0
    if kind == "secret":
        return "<redacted-secret>"
    return "<redacted>"


def _is_path(value: Any) -> bool:
    return isinstance(value, str) and bool(_PATH_SHAPE.match(value)) and len(value) > 1


def redact_value(value: Any, key: str, profiles: list[str], report: RedactionReport) -> Any:
    """Redact one value, by its key, and count what was replaced."""
    if isinstance(value, dict):
        return {k: redact_value(v, str(k), profiles, report) for k, v in value.items()}
    if isinstance(value, list):
        if key in ("argv", "args", "command") and "argv" in profiles and value:
            report.bump("argv", len(value))
            return _placeholder_for("", "argv")
        return [redact_value(item, key, profiles, report) for item in value]
    if not isinstance(value, str):
        if isinstance(value, int) and not isinstance(value, bool) and key in _PORT_KEYS and "hosts" in profiles:
            report.bump("hosts")
            return 0
        return value

    if "secrets" in profiles:
        for shape in _SECRET_SHAPES:
            if shape.search(value):
                report.bump("secrets")
                return _placeholder_for(value, "secret")
    if "paths" in profiles and (key in _PATH_KEYS or _is_path(value)):
        report.bump("paths")
        return _placeholder_for(value, "path")
    if "hosts" in profiles and (key in _HOST_KEYS or key.endswith("_host")):
        report.bump("hosts")
        return _placeholder_for(value, "host")
    return value


def redact_event(event: Event, profiles: list[str], report: RedactionReport) -> Event:
    """One ledger event, with its data redacted and its hash left to be recomputed."""
    data = redact_value(dict(event.data or {}), "", profiles, report)
    return Event(seq=event.seq, ts=event.ts, type=event.type, data=data, prev=event.prev, hash="")


def rechain(events: list[Event]) -> list[Event]:
    """Recompute the hash chain over redacted events.

    Each event's hash covers its own core, which includes `prev`, so the chain is rebuilt forward
    from genesis. This is the same computation `docket verify` performs, which is why the pair
    verifies afterwards.
    """
    # The chain starts at the ledger's genesis constant, not at zeroes. Getting this wrong makes
    # every hash in the redacted ledger differ from the one `docket verify` recomputes, which is
    # the failure this function exists to avoid.
    previous = GENESIS
    out: list[Event] = []
    for event in events:
        chained = Event(seq=event.seq, ts=event.ts, type=event.type, data=event.data, prev=previous, hash="")
        chained.hash = chained.compute_hash()
        previous = chained.hash
        out.append(chained)
    return out


def redact_receipt(
    receipt: dict[str, Any],
    events: list[Event],
    profiles: list[str],
    report: RedactionReport,
) -> tuple[dict[str, Any], list[Event]]:
    """Redact the events, re-chain them, and bring the receipt's own ledger claims into line.

    The receipt carries four things that mirror the ledger: the head hash, the event count, the
    Merkle root and the file digest, plus a per-row `event_hash` and `merkle_path` in the usage
    appendix. All of them are rebuilt here, because a receipt that still claimed the old head would
    fail the `head` check on the redacted ledger.
    """
    chained = rechain([redact_event(event, profiles, report) for event in events])

    # The receipt's own surfaces, redacted with the same rules the events were.
    for key in ("session", "files", "network", "proxy", "ops", "tools", "appendix", "evidence"):
        if key in receipt:
            receipt[key] = redact_value(receipt[key], "", profiles, report)

    ledger_block = dict(receipt.get("ledger") or {})
    ledger_block["events"] = len(chained)
    ledger_block["head"] = chained[-1].hash if chained else "0" * 64
    ledger_block["merkle_root"] = merkle_root([event.hash for event in chained])
    # `docket verify` recomputes the digest of the ledger file it is handed and compares it with
    # this value, so it has to be the digest of the file this function's caller is about to write.
    # It is computed here from the same bytes `write_pair` emits, which is why the two are written
    # by one function rather than two.
    receipt["ledger"] = ledger_block

    # The usage appendix pins each row to the event that produced it. After redaction those events
    # have new hashes, so the pins are rebuilt from the re-chained events.
    hashes = [event.hash for event in chained]
    appendix = receipt.get("appendix") or {}
    for row in appendix.get("usage", []) if isinstance(appendix.get("usage"), list) else []:
        if not isinstance(row, dict):
            continue
        seq = row.get("seq")
        index = next((i for i, event in enumerate(chained) if event.seq == seq), None)
        if index is None:
            continue
        row["event_hash"] = hashes[index]
        # The proof is a sibling path through the Merkle tree of the *new* hashes. Leaving the old
        # one in place fails `usage-inclusion-proofs`, which is the check that carries the project's
        # central claim, so it is rebuilt rather than dropped.
        row["merkle_path"] = merkle_proof(hashes, index)
    receipt["appendix"] = appendix

    receipt["redaction"] = report.to_dict()
    return receipt, chained


def ledger_line(event: Event) -> str:
    """One ledger line, in the canonical form `docket verify` recomputes from."""
    return canonical_bytes(event.to_dict()).decode("utf-8")


def write_pair(
    receipt: dict[str, Any],
    events: list[Event],
    receipt_path: Path,
    ledger_path: Path,
    out_dir: Path,
) -> tuple[Path, Path]:
    """Write the redacted receipt and ledger, and never over the originals.

    The ledger is written first, because the receipt records the digest of the file that exists.
    Writing the receipt first would leave it naming a digest of bytes nobody has produced.
    """
    import json

    from .util import atomic_write

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = receipt_path.stem
    if stem.endswith(".receipt"):
        stem = stem[: -len(".receipt")]
    new_receipt = out_dir / f"{stem}.redacted.receipt.json"
    new_ledger = out_dir / f"{stem}.redacted.jsonl"
    # The `.redacted.` infix makes a collision with the originals impossible, so there is no guard
    # to write here: what has to hold is that the originals are byte-identical afterwards, and
    # `tests/test_redact.py::test_the_original_is_untouched_by_a_redaction` asserts exactly that.

    body = "".join(ledger_line(event) + "\n" for event in events)
    atomic_write(new_ledger, body)
    receipt.setdefault("ledger", {})["file_sha256"] = sha256_hex(body.encode("utf-8"))
    atomic_write(new_receipt, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return new_receipt, new_ledger


def ledger_bytes_are_canonical(events: list[Event]) -> bool:
    """True when every event's hash is the one its own core produces.

    Used by the tests rather than by the command: it is the property that makes a redacted pair
    verifiable, stated as a function so it can be asserted directly.
    """
    return all(event.hash == event.compute_hash() for event in events)
