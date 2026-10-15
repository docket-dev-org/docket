"""The ``docket`` command line interface."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import time
from collections.abc import Sequence
from decimal import Decimal, DecimalException, InvalidOperation
from pathlib import Path
from typing import Any

from . import __version__
from .diagnostics import write_diagnostic_bundle
from .ledger import Ledger, quarantine_torn_tail
from .policy import DEFAULT_TOML, Caps, Policy
from .pricing import PriceTable
from .receipt import load_receipt
from .report import diff_receipts, render_diff, render_findings, render_markdown, render_receipt
from .resilience import parse_fault_spec
from .supervisor import Supervisor
from .util import NonStandardJSONError, http_url, human_bytes, loads_json
from .verify import verify_receipt

PROGRAM = "docket"

#: The largest USD cap the CLI will accept. Not a technical limit — it is a sanity bound so a
#: slipped keystroke (`1e300`) is refused where it was typed instead of becoming a number that
#: the cost arithmetic then has to survive.
MAX_CAP_USD = Decimal("1e12")


def _cap_amount(text: str) -> str:
    """An argparse ``type=`` for a USD cap: finite, non-negative, and not absurd.

    The parsing happens here rather than at the four call sites, so a bad value is refused by
    argparse — exit code ``2``, the same as any other bad flag, with the message naming the flag
    and the value. Before this existed, `--max-usd nan` was accepted and *silently disabled the
    cap* (NaN compares false against every threshold), while `--max-usd abc` printed
    ``[<class 'decimal.ConversionSyntax'>]`` — the decimal exception's args, not a sentence.
    """
    value = str(text).strip()
    try:
        amount = Decimal(value)
    except (DecimalException, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"must be a number, got {text!r}") from exc
    if not amount.is_finite():
        raise argparse.ArgumentTypeError(
            f'must be a finite amount, got {text!r}; for "no cap" omit --max-usd, '
            "which the receipt discloses as an unset cap rather than as a number"
        )
    if amount < 0:
        raise argparse.ArgumentTypeError(f"must not be negative, got {text!r}")
    if amount > MAX_CAP_USD:
        raise argparse.ArgumentTypeError(f"must not exceed {MAX_CAP_USD:,.0f} USD, got {text!r}")
    return value


def _whole_number(text: str) -> int:
    """An argparse ``type=`` for a count: a whole, non-negative number.

    ``int()`` after argparse accepted anything meant `--max-calls abc` reached the coercion with
    a bare ``ValueError`` (exit ``1``) or, for `--max-minutes -5`, produced a negative wall-clock
    cap that no run could ever satisfy — a cap that stops the run before it starts, spelled as a
    flag. Both are usage errors, so both are refused here, by argparse, with exit ``2``.
    """
    try:
        number = int(str(text).strip())
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"must be a whole number, got {text!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError(f"must not be negative, got {text!r}")
    return number


def _at_least_one(text: str) -> int:
    """A whole number that has to be at least 1 — a rate or a count where 0 means "nothing"."""
    number = _whole_number(text)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {text!r}")
    return number


def parse_upstream_overrides(pairs: list[str]) -> dict[str, str] | None:
    """Turn ``["openai=http://host:9000"]`` into ``{"openai": "http://host:9000"}``.

    The base URL of a provider is part of what a receipt *means* — a call to a mock, a gateway or a
    self-hosted model is not a call to the provider's own API — so an override is recorded in the
    run's evidence (name and scheme/host/path, without query, fragment or userinfo: a token pasted
    into a URL must not end up in a ledger).

    Refused with the reason rather than falling back to the default: silently ignoring a
    ``--upstream`` the operator typed would mean an agent talking to the real API while the command
    line says otherwise.
    """
    if not pairs:
        return None
    resolved: dict[str, str] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        name = name.strip().lower()
        if not sep or not name or not raw.strip():
            raise ValueError(f"--upstream needs NAME=URL, got {pair!r}")
        if not all(char.isalnum() or char in "-_" for char in name):
            raise ValueError(f"--upstream name must be letters, digits, '-' or '_', got {name!r}")
        if name in resolved:
            raise ValueError(f"--upstream {name} given twice; one base URL per provider")
        try:
            resolved[name] = http_url(raw.strip())
        except ValueError as exc:
            raise ValueError(f"--upstream {name}: {exc}") from exc
    return resolved


def _port_number(text: str) -> int:
    """A TCP port for the boundary: 0 (any free port) or 1024..65535.

    Ports below 1024 are refused rather than attempted. On a Linux box they need privileges the
    tool has no business asking for, and binding the *boundary* somewhere privileged is a
    deployment smell: the value of a fixed port is that an orchestrator can probe it, not that it
    is hard to reach.
    """
    value = _whole_number(text)
    if value == 0:
        return 0
    if not 1024 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 0 or between 1024 and 65535, not {text!r}")
    return value


def _positive_minutes(text: str) -> int:
    """Minutes to seconds, refusing zero and negatives: both mean "stop before the work"."""
    try:
        seconds = int(round(float(str(text).strip()) * 60))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"must be a number of minutes, got {text!r}") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"must be more than zero minutes, got {text!r}")
    return seconds


def _color_enabled(stream: Any) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("DOCKET_FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def _out(stream: Any, text: str = "") -> None:
    print(text, file=stream)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    from . import snapshot as snapshot_module

    try:
        policy = Policy.load(args.policy)
    except (OSError, ValueError) as exc:
        _out(sys.stderr, f"{PROGRAM}: cannot load policy: {exc}")
        return 43
    overrides: list[str] = []
    if args.max_usd is not None:
        policy.caps.set_max_usd(Decimal(args.max_usd))
        overrides.append(f"max_usd={args.max_usd}")
    if args.max_tokens is not None:
        policy.caps.max_tokens = args.max_tokens
        overrides.append(f"max_tokens={args.max_tokens}")
    if args.max_calls is not None:
        policy.caps.max_model_calls = args.max_calls
        overrides.append(f"max_model_calls={args.max_calls}")
    if args.max_minutes is not None:
        # Already seconds: `--max-minutes` is validated and converted by its argparse type, so a
        # bad value is a usage error before anything is constructed.
        policy.caps.max_wall_seconds = args.max_minutes
        overrides.append(f"max_wall_seconds={policy.caps.max_wall_seconds}")
    if args.on_breach:
        policy.caps.on_breach = args.on_breach
    if args.budget_mode:
        policy.budget_mode = args.budget_mode
    if args.max_calls_per_minute is not None:
        policy.resilience.max_calls_per_minute = args.max_calls_per_minute
        overrides.append(f"max_calls_per_minute={args.max_calls_per_minute}")
    if args.max_events is not None:
        policy.resilience.max_events = args.max_events
        overrides.append(f"max_events={args.max_events}")
    if args.fail_fast_evidence:
        policy.resilience.fail_fast_evidence = True
        overrides.append("fail_fast_evidence=true")
    faults = []
    if args.fault_inject:
        try:
            faults = parse_fault_spec(args.fault_inject)
        except ValueError as exc:
            _out(sys.stderr, f"{PROGRAM}: bad --fault-inject: {exc}")
            return 43
        if not faults:
            _out(sys.stderr, f"{PROGRAM}: --fault-inject did not describe any fault: {args.fault_inject!r}")
            return 43
    # The evidence directory is excluded from the watched tree by the Supervisor itself, from
    # resolved paths (`_exclude_own_evidence`) rather than from the flag as typed: a rule that
    # only works when the shell's directory happens to equal the child's is a rule that fails
    # quietly on the day it matters.

    seal_key = None
    if args.seal:
        from .signing import load_or_create_seal_key

        seal_key = load_or_create_seal_key(Path(args.docket_dir) / "keys" / "seal.key")

    if not args.command:
        _out(sys.stderr, f"{PROGRAM}: nothing to run. Usage: {PROGRAM} run -- <command> [args…]")
        return 43

    upstreams = None
    try:
        from_flag = parse_upstream_overrides(args.upstream or [])
    except ValueError as exc:
        _out(sys.stderr, f"{PROGRAM}: {exc}")
        return 43
    if from_flag:
        upstreams = from_flag

    upstream_override = os.environ.get("DOCKET_GAME_DAY_UPSTREAM")
    if upstream_override and upstreams is None:
        # Used by `docket game-day` to point a real `docket run` at a local scripted
        # provider. Documented, single-purpose, and never set by Docket itself in normal
        # operation: the boundary is still the real boundary. An explicit `--upstream` wins over
        # it, because a flag on the command line is what the operator just typed.
        try:
            upstreams = loads_json(upstream_override, what="DOCKET_GAME_DAY_UPSTREAM")
        except (json.JSONDecodeError, NonStandardJSONError) as exc:
            _out(sys.stderr, f"{PROGRAM}: bad DOCKET_GAME_DAY_UPSTREAM: {exc}")
            return 43
        if not isinstance(upstreams, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in upstreams.items()
        ):
            _out(sys.stderr, f"{PROGRAM}: DOCKET_GAME_DAY_UPSTREAM must be a JSON object of provider→URL")
            return 43

    from .supervisor import check_docket_dir

    problem = check_docket_dir(args.docket_dir)
    if problem:
        _out(sys.stderr, f"{PROGRAM}: {problem}")
        return 43
    if args.policy:
        policy_dir = Path(args.policy).resolve().parent
        problem = check_docket_dir(policy_dir) if not os.access(policy_dir, os.W_OK | os.X_OK) else None
        if problem:
            _out(
                sys.stderr,
                f"{PROGRAM}: cannot write next to the policy at {args.policy}: {problem} "
                "(the run keeps a copy of the policy it used beside the original)",
            )
            return 43

    supervisor = Supervisor(
        args.command,
        policy=policy,
        docket_dir=args.docket_dir,
        cwd=args.cwd,
        profile="minimal" if upstreams and args.profile == "full" else args.profile,
        http_proxy=args.http_proxy,
        record_bodies=args.record_bodies,
        seal_key=seal_key,
        sign_key=args.sign,
        label=args.label,
        price_table=PriceTable.load(args.prices) if args.prices else None,
        faults=faults,
        fault_seed=args.fault_seed,
        upstreams=upstreams,
        snapshot_files=args.snapshot or args.rollback_on_breach,
        snapshot_max_bytes=(
            args.snapshot_max_bytes if args.snapshot_max_bytes is not None else snapshot_module.DEFAULT_MAX_BYTES
        ),
        rollback_on_breach=args.rollback_on_breach,
        proxy_port=args.port,
    )
    if faults:
        _out(
            sys.stderr,
            f"{PROGRAM}: FAULT INJECTION ACTIVE ({'; '.join(f.describe() for f in faults)}"
            f", seed {args.fault_seed}) — this receipt records a test, not production behaviour",
        )
    try:
        result = supervisor.run()
    except snapshot_module.SnapshotTooLargeError as exc:
        # Refused before the agent started: the alternative is discovering mid-run that the
        # snapshot cannot cover what the agent is about to change.
        _out(sys.stderr, f"{PROGRAM}: {exc}")
        return 43
    color = _color_enabled(sys.stderr)
    receipt = result.receipt
    if args.report == "full":
        _out(sys.stderr, render_receipt(receipt, color=color))
    else:
        totals = receipt.get("totals", {})
        cap = receipt.get("policy", {}).get("document", {}).get("caps", {}).get("max_usd")
        status = f"stopped by policy ({result.breached})" if result.breached else "completed"
        _out(
            sys.stderr,
            f"{PROGRAM}: {result.docket_number} {status} · "
            f"${totals.get('cost_usd')}"
            + (f" of ${cap} cap" if cap else "")
            + f" · {totals.get('calls')} calls · receipt {result.receipt_path}",
        )
        if result.breached:
            _out(sys.stderr, f"{PROGRAM}: find out why with `{PROGRAM} explain {result.receipt_path}`")
        evidence = receipt.get("evidence") or {}
        if evidence.get("ledger_complete") is False:
            _out(
                sys.stderr,
                f"{PROGRAM}: WARNING the ledger is incomplete: {evidence.get('unwritten_events')} event(s) "
                f"never reached disk (first at seq {evidence.get('first_failing_seq')}). "
                "The receipt declares this and verification will report it.",
            )
        ops = receipt.get("ops") or {}
        snapshot = ops.get("snapshot") or {}
        rollback = ops.get("rollback")
        if snapshot.get("taken"):
            line = (
                f"{PROGRAM}: snapshot: {snapshot.get('files', 0)} file(s), "
                f"{human_bytes(snapshot.get('bytes_stored', 0))} stored"
            )
            if snapshot.get("not_covered"):
                line += f" · {len(snapshot['not_covered'])} file(s) it could not cover"
            _out(sys.stderr, line)
        if rollback and rollback.get("applied"):
            counts = rollback.get("counts") or {}
            summary = ", ".join(f"{value} {key}" for key, value in sorted(counts.items()) if value)
            _out(sys.stderr, f"{PROGRAM}: rolled back after breach: {summary or 'nothing to do'}")
            for item in rollback.get("refused") or []:
                _out(sys.stderr, f"{PROGRAM}: NOT rolled back: {item['path']} — {item['reason']}")
        elif rollback:
            _out(sys.stderr, f"{PROGRAM}: no rollback: {rollback.get('reason') or 'no snapshot taken'}")
            for item in rollback.get("refused") or []:
                _out(sys.stderr, f"{PROGRAM}: NOT rolled back: {item.get('path')} — {item.get('reason')}")
        breaker = ops.get("circuit_breaker") or {}
        opened = {name: state for name, state in breaker.items() if state.get("state") != "closed"}
        if opened:
            for name, state in opened.items():
                _out(
                    sys.stderr,
                    f"{PROGRAM}: circuit {state['state']} for {name!r} after {state['total_failures']} "
                    f"failure(s); the boundary stopped sending and said so in the ledger.",
                )
    return result.exit_code


def _load_receipt_arg(path: str | None) -> dict[str, Any] | None:
    if path:
        return load_receipt(path)
    latest = Path(".docket") / "latest.receipt.json"
    if latest.exists():
        return load_receipt(latest)
    return None


def _docket_dir_for(receipt_path: Path, override: str | None) -> Path:
    """Where a run kept its evidence, inferred from the receipt that points at it.

    A receipt lives at ``<docket-dir>/receipts/<number>.receipt.json`` and ``latest.receipt.json``
    is a copy at the top of the docket directory, so the *parent* of a receipt is not always the
    right answer — the parent of a ``receipts/`` directory is. Getting this wrong is the
    difference between "this run has no snapshot" and the snapshot sitting two directories away.
    """
    if override:
        return Path(override)
    parent = receipt_path.parent
    return parent.parent if parent.name == "receipts" else parent


def _store_candidates(receipt: dict[str, Any], receipt_path: Path, docket_dir: Path) -> list[Path]:
    """Every place the snapshot for this run could be, most specific first.

    The receipt records the manifest path the run used (relative to the run's working directory,
    which is itself recorded), so that is the first candidate; the conventional location inside
    the docket directory is the second. Both are derived rather than guessed at random.
    """
    session = receipt.get("session") or {}
    session_id = str(session.get("id") or "")
    manifest = ((receipt.get("ops") or {}).get("snapshot") or {}).get("manifest")
    candidates: list[Path] = []
    if isinstance(manifest, str) and manifest:
        recorded = Path(manifest)
        if recorded.is_absolute():
            candidates.append(recorded.parent)
        else:
            cwd = session.get("cwd")
            if isinstance(cwd, str) and cwd:
                candidates.append(Path(cwd) / recorded.parent)
            candidates.append(receipt_path.parent / recorded.parent)
            candidates.append(docket_dir / recorded.parent)
    if session_id:
        candidates.append(docket_dir / "snapshots" / session_id)
        candidates.append(receipt_path.parent / "snapshots" / session_id)
    seen: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.append(candidate)
    return seen


def _find_snapshot_store(
    receipt: dict[str, Any], receipt_path: Path, docket_dir: Path, override: str | None
) -> Path | None:
    if override:
        return Path(override)
    for candidate in _store_candidates(receipt, receipt_path, docket_dir):
        if (candidate / "manifest.json").exists():
            return candidate
    return None


def _ledger_candidates(store: Path, receipt_path: Path, docket_dir: Path) -> list[Path]:
    """Where the run's ledger could be: beside the snapshot store it was found in, then by convention."""
    session_id = store.name  # the store is <docket-dir>/snapshots/<session-id>
    places = [store.parent.parent, docket_dir, receipt_path.parent, Path.cwd()]
    seen: list[Path] = []
    for place in places:
        candidate = place / "runs" / f"{session_id}.jsonl"
        if candidate not in seen:
            seen.append(candidate)
    # A session id that is not a directory name (a hand-built store) still has a ledger by name.
    for place in places:
        for name in sorted(place.glob(f"{session_id}*.jsonl")) if session_id else []:
            if name not in seen:
                seen.append(name)
    return seen


def _find_ledger(
    receipt: dict[str, Any], store: Path, receipt_path: Path, docket_dir: Path, override: str | None
) -> Path | None:
    if override:
        return Path(override)
    for candidate in _ledger_candidates(store, receipt_path, docket_dir):
        if candidate.exists():
            return candidate
    return None


def cmd_restore(args: argparse.Namespace) -> int:
    """Undo the filesystem changes a recorded run made, from its own snapshot.

    Four questions are answered before anything is written: *can* this run be restored (is
    there a snapshot?), *which tree* it is about (three different documents record it, and
    they have to agree), *what* would change (derived from the ledger's own ``fs.change``
    events, so it can be audited first), and *has anyone edited those files since* (in which
    case the path is refused unless ``--force``). Restoring files is destructive; it is never
    done quietly, and it is never claimed to be complete — paths the snapshot could not cover
    are printed as exactly that.

    The tree is the one thing a restore must not get from a document alone. A receipt is
    evidence about a run, not proof of who wrote it: anyone who can hand you a receipt can
    name a different tree in it. So the receipt, the ledger and the snapshot manifest all
    record the run's working directory, and if they disagree Docket stops and shows you the
    disagreement rather than picking one — ``--root`` is how an operator decides, out loud.
    """
    from . import snapshot as snapshot_module

    receipt_path = Path(args.receipt)
    if not receipt_path.exists():
        _out(sys.stderr, f"{PROGRAM}: no such receipt: {receipt_path}")
        return 1
    receipt = load_receipt(receipt_path)
    session_id = str((receipt.get("session") or {}).get("id") or "")
    docket_dir = _docket_dir_for(receipt_path, args.docket_dir)
    store = _find_snapshot_store(receipt, receipt_path, docket_dir, args.snapshot_store)
    if store is None:
        _out(
            sys.stderr,
            f"{PROGRAM}: this run has no snapshot, so nothing can be restored from it. "
            "Snapshots are opt-in: run with --snapshot (or --rollback-on-breach) next time, "
            "and `--snapshot-store DIR` names a store this command should look in.",
        )
        return 1
    ledger_path = _find_ledger(receipt, store, receipt_path, docket_dir, args.ledger)
    if ledger_path is None:
        _out(
            sys.stderr,
            f"{PROGRAM}: the ledger for this run is missing (looked in "
            f"{', '.join(str(item) for item in _ledger_candidates(store, receipt_path, docket_dir))}), so which "
            "files changed cannot be re-derived. Restore needs both the snapshot and the record; "
            "`--ledger PATH` names it if it moved.",
        )
        return 1
    manifest = snapshot_module.read_manifest(store)
    events = Ledger.read(ledger_path)
    changes: list[dict[str, Any]] = []
    started_cwd = None
    for event in events:
        if event.type == "fs.change":
            changes.append(dict(event.data))
        elif event.type == "session.start" and started_cwd is None:
            started_cwd = event.data.get("cwd")
    recorded = {
        "the receipt": (receipt.get("session") or {}).get("cwd"),
        "the ledger": started_cwd,
        "the snapshot manifest": manifest.get("root"),
    }

    def _resolved(value: object) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return str(Path(value).resolve())
        except OSError:  # a path that cannot be resolved is compared as written, and will differ
            return value

    named = {label: _resolved(value) for label, value in recorded.items()}
    if args.root:
        root = Path(args.root).resolve()
        if not root.is_dir():
            _out(sys.stderr, f"{PROGRAM}: --root {root} is not a directory")
            return 1
        others = {label: value for label, value in named.items() if value and value != str(root)}
        header = f"  {PROGRAM} restore — run {session_id or 'unknown'} into {root}" + (
            f"  (not the tree {', '.join(sorted(others))} recorded: {', '.join(sorted(set(others.values())))})"
            if others
            else ""
        )
    else:
        distinct = {value for value in named.values() if value}
        if not distinct:
            _out(
                sys.stderr,
                f"{PROGRAM}: none of the receipt, the ledger or the snapshot records which tree this run "
                "used, so there is nowhere to restore. Pass --root to name it.",
            )
            return 1
        if len(distinct) > 1:
            detail = "\n".join(f"    {label}: {value}" for label, value in recorded.items() if value)
            _out(
                sys.stderr,
                f"{PROGRAM}: the records disagree about which tree this run used, so Docket will not "
                f"choose for you:\n{detail}\n"
                f"  Re-run with --root naming the tree you checked — the tree you want back.",
            )
            return 1
        root = Path(next(iter(distinct)))
        if not root.is_dir():
            _out(
                sys.stderr,
                f"{PROGRAM}: the tree this run recorded ({root}) is not a directory here. "
                "Pass --root to name where it is now, or where its copy is.",
            )
            return 1
        header = f"  {PROGRAM} restore — run {session_id or 'unknown'} into {root}"

    plan = snapshot_module.plan_restore(manifest, changes)
    for item in plan:
        change = next((c for c in changes if c.get("path") == item["path"]), {})
        after = change.get("after")
        item["post_sha256"] = after.get("sha256") if isinstance(after, dict) else None
    if not plan:
        _out(
            sys.stdout,
            f"  nothing to restore: the run changed none of the {len(manifest.get('entries', []))} snapshotted file(s)",
        )
        return 0
    applied = snapshot_module.apply_restore(plan, root=root, store=store, force=args.force) if args.apply else None
    verbs = {
        "restore": "restore",
        "recreate": "recreate",
        "remove": "remove",
        "not_covered": "skip (not covered)",
        "refused": "REFUSE",
    }
    # After applying, each line reports what *happened* to the path rather than what was
    # planned for it: a plan line reading "restore" next to a file that was refused is exactly
    # the kind of half-truth a restore must not print.
    done = {
        "restored": "restored",
        "recreated": "recreated",
        "removed": "removed",
        "refused": "REFUSED",
        "failed": "FAILED",
        "skipped": "skipped (not covered)",
        "absent": "already gone",
        "already": "already restored",
    }
    lines = [header, ""]
    rows = applied if applied is not None else plan
    for item in rows:
        after = f" -> {item['post_sha256'][:12]}" if item.get("post_sha256") else ""
        if applied is not None:
            verb = done.get(str(item.get("status")), str(item.get("status")))
        else:
            verb = "would " + verbs.get(str(item["action"]), str(item["action"]))
        reason = ""
        if item.get("reason") and (
            item.get("status") in {"refused", "failed", "skipped"} or item.get("action") in {"refused", "not_covered"}
        ):
            reason = f" — {item['reason']}"
        note = f" ({item['mode_note']})" if item.get("mode_note") else ""
        lines.append(f"  {verb:<11} {item['path']}{after}{reason}{note}")
    if applied is not None:
        counts = snapshot_module.summarise(applied)
        lines.append("")
        lines.append(f"  applied: {counts}")
    else:
        lines.append("")
        lines.append("  dry run: nothing was written. Add --apply to restore these paths.")
    if manifest.get("not_covered"):
        lines.append("")
        lines.append(f"  the snapshot could not cover {len(manifest['not_covered'])} file(s):")
        for item in manifest["not_covered"][:5]:
            lines.append(f"    {item['path']} — {item['reason']}")
    blocked = [item for item in (applied or []) if item["status"] in {"refused", "failed"}]
    payload: dict[str, Any] = {
        "root": str(root),
        "recorded": recorded,
        "plan": plan,
        "applied": applied,
        "counts": snapshot_module.summarise(applied) if applied else None,
        "not_covered": manifest.get("not_covered", []),
    }
    if applied is not None:
        # A restore is a destructive act performed on somebody's tree, months after the run,
        # possibly by somebody who did not run the agent. It leaves a record of its own — a new
        # file beside the evidence, never an edit to the run's ledger or receipt: those are
        # historical observations, and an implementation that appended to them after the fact
        # would break the receipt's own agreement with the ledger it cites. Dry runs write
        # nothing, because they change nothing.
        written = _write_restore_record(
            docket_dir=docket_dir,
            receipt_path=receipt_path,
            receipt=receipt,
            root=root,
            store=store,
            plan=plan,
            applied=applied,
            counts=snapshot_module.summarise(applied),
            not_covered=manifest.get("not_covered") or [],
            forced=bool(args.force),
            exit_code=1 if blocked else 0,
        )
        payload["record"] = str(written)
        if not args.json:
            lines.append("")
            lines.append(f"  recorded: {written}")
    if args.json:
        _out(sys.stdout, json.dumps(payload, indent=2))
    else:
        _out(sys.stdout, "\n".join(lines))
    return 1 if blocked else 0


def _write_restore_record(
    *,
    docket_dir: Path,
    receipt_path: Path,
    receipt: dict[str, Any],
    root: Path,
    store: Path,
    plan: list[dict[str, Any]],
    applied: list[dict[str, Any]],
    counts: dict[str, int],
    not_covered: list[dict[str, Any]],
    forced: bool,
    exit_code: int,
) -> Path:
    """Write what this restore did, beside the evidence, without touching the run's records.

    The record is self-describing (``docket/restore/v1``), names the receipt and the store it
    came from, and carries the per-path outcomes verbatim — including the refusals, which are
    the part an operator investigating "who put this file back?" most needs. Names are
    timestamped so several restores of the same run are all kept; nothing is ever overwritten.
    """
    from .util import atomic_write

    directory = docket_dir / "restores"
    # The filename carries a compact, sortable UTC stamp; the document carries RFC 3339, the
    # same form the ledger and the receipt use for every other instant they record.
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    restored_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    number = str(receipt.get("docket_number") or "unknown")
    base = f"{stamp}-{number}"
    candidate = directory / f"{base}.json"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{base}-{suffix}.json"
        suffix += 1
    document = {
        "format": "docket/restore/v1",
        "tool": {"name": "docket", "version": __version__},
        "restored_at": restored_at,
        "docket_number": number,
        "session_id": (receipt.get("session") or {}).get("id"),
        "receipt": str(receipt_path),
        "snapshot_store": str(store),
        "root": str(root),
        "forced": bool(forced),
        "counts": counts,
        "planned": len(plan),
        "applied": applied,
        "not_covered": not_covered,
        "exit_code": exit_code,
    }
    atomic_write(candidate, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    return candidate


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.receipt)
    if not path.exists():
        _out(sys.stderr, f"{PROGRAM}: no such receipt: {path}")
        return 1
    receipt = load_receipt(path)
    # `--seal-key` accepts either the key file written by `docket run --seal` or the
    # hex key itself; the file is the common case (and the one that gets pasted wrong).
    seal_key = args.seal_key
    if seal_key:
        candidate = Path(seal_key)
        if candidate.is_file():
            seal_key = candidate.read_text(encoding="utf-8").strip()
    result = verify_receipt(
        receipt,
        ledger_path=args.ledger,
        seal_key=seal_key,
        expect_public_key=args.public_key,
    )
    if args.json:
        _out(sys.stdout, json.dumps(result.to_dict(), indent=2))
    else:
        _out(sys.stdout, result.render(color=_color_enabled(sys.stdout)))
    return 0 if result.ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    receipt = _load_receipt_arg(args.receipt)
    if receipt is None:
        _out(sys.stderr, f"{PROGRAM}: no receipt found. Pass one, or run something first.")
        return 1
    if args.markdown:
        _out(sys.stdout, render_markdown(receipt))
    else:
        _out(sys.stdout, render_receipt(receipt, color=_color_enabled(sys.stdout)))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from .explain import analyse

    receipt = _load_receipt_arg(args.receipt)
    if receipt is None:
        _out(sys.stderr, f"{PROGRAM}: no receipt found. Pass one, or run something first.")
        return 1
    if args.ledger:
        ledger_path = Path(args.ledger)
    else:
        candidates = list((Path(".docket") / "runs").glob("*.jsonl"))
        ledger_path = max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
    if ledger_path is None or not ledger_path.exists():
        _out(sys.stderr, f"{PROGRAM}: ledger not found; pass --ledger <file>")
        return 1
    events = Ledger.read(ledger_path)
    findings = analyse(
        events,
        price_table=PriceTable.load(),
        policy=Policy.from_dict(receipt.get("policy", {}).get("document", {}) or {}),
    )
    if args.json:
        _out(sys.stdout, json.dumps(findings, indent=2))
        return 0
    if not findings:
        _out(sys.stdout, "No findings: nothing in this run looks like waste or risk.")
        return 0
    color = _color_enabled(sys.stdout)
    _out(sys.stdout, f"{receipt.get('docket_number')} — {len(findings)} finding(s)")
    _out(sys.stdout)
    _out(sys.stdout, render_findings(findings, color=color))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    a = load_receipt(args.left)
    b = load_receipt(args.right)
    difference = diff_receipts(a, b)
    if args.json:
        _out(sys.stdout, json.dumps(difference, indent=2))
    else:
        _out(sys.stdout, render_diff(difference, color=_color_enabled(sys.stdout)))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    outcome = run_demo(
        max_usd=Decimal(args.max_usd),
        keep=args.keep,
        tamper=args.tamper,
        color=_color_enabled(sys.stdout) and not args.json,
        # --json means "machine-readable, nothing else": the narrative goes to a
        # buffer so that stdout is a single JSON object and nothing pipes a
        # pretty-printed story into `jq`.
        out=io.StringIO() if args.json else None,
    )
    if args.json:
        _out(sys.stdout, json.dumps(outcome, indent=2))
    return 0 if (outcome["verify_ok"] and (not args.tamper or outcome.get("tamper_detected"))) else 1


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if path.exists() and not args.force:
        _out(sys.stderr, f"{PROGRAM}: {path} exists (use --force to overwrite)")
        return 1
    from .util import atomic_write

    atomic_write(path, DEFAULT_TOML(name=args.name, max_usd=args.max_usd))
    _out(sys.stdout, f"wrote {path}. Start a bounded run with:")
    _out(sys.stdout, f"  {PROGRAM} run --policy {path} -- your-agent")
    return 0


def cmd_prices(args: argparse.Namespace) -> int:
    table = PriceTable.load(args.path)
    if args.json:
        _out(sys.stdout, json.dumps(table.document, indent=2))
        return 0
    meta = table.document.get("_meta", {})
    _out(sys.stdout, f"price table {meta.get('schema')} as of {table.as_of} ({meta.get('currency')})")
    _out(sys.stdout, f"digest: {table.digest()}")
    _out(sys.stdout)
    _out(sys.stdout, f"  {'pattern':32} {'input':>9} {'cached':>9} {'output':>9}")
    for price in table.prices:
        _out(
            sys.stdout,
            f"  {price.pattern:32} {price.input:>9} {price.cached_input:>9} {price.output:>9}   {price.source[:40]}",
        )
    _out(sys.stdout)
    _out(
        sys.stdout,
        "Docket estimates only; it never bills anyone. Override for one run with `docket run --prices <file>`.",
    )
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from .diagnostics import render_probes, self_test

    probes, workspace = self_test(keep_workspace=args.keep)
    failed = [p for p in probes if p.status == "fail"]
    if args.json:
        _out(
            sys.stdout,
            json.dumps(
                {
                    "probes": [
                        {"name": p.name, "status": p.status, "detail": p.detail, "data": p.data} for p in probes
                    ],
                    "passed": sum(1 for p in probes if p.status == "pass"),
                    "failed": len(failed),
                    "skipped": sum(1 for p in probes if p.status == "skipped"),
                    # The workspace is only reported as a place to look when it is still there:
                    # `--keep` is what decides that, and the JSON says which way it went.
                    "workspace": str(workspace),
                    "workspace_kept": bool(args.keep),
                },
                indent=2,
            ),
        )
        return 1 if failed else 0
    _out(sys.stdout, f"{PROGRAM} {__version__} selftest — the boundary, exercised in this process")
    _out(sys.stdout)
    _out(sys.stdout, render_probes(probes, color=_color_enabled(sys.stdout)))
    if args.keep:
        _out(sys.stdout)
        _out(sys.stdout, f"  workspace kept at {workspace}")
    return 1 if failed else 0


def cmd_chaos(args: argparse.Namespace) -> int:
    from .chaos import render_chaos, run_chaos

    policy = Policy(name="chaos experiment", caps=Caps(max_usd=None))
    if args.max_usd:
        policy.caps.set_max_usd(Decimal(args.max_usd))
    command = [c for c in (args.command or []) if c != "--"] or None
    if not command and not args.faults:
        _out(
            sys.stderr,
            f"{PROGRAM}: nothing to break. Give a fault spec (--faults status=500:1.0) or a command to run "
            "after `--`. Run `docket selftest` for the built-in checks.",
        )
        return 43
    outcome = run_chaos(
        faults_spec=args.faults or "",
        seed=args.seed,
        calls=args.calls,
        mode=args.mode,
        policy=policy,
        command=command,
        keep=args.keep,
    )
    if args.json:
        _out(sys.stdout, json.dumps(outcome, indent=2))
    else:
        _out(sys.stdout, render_chaos(outcome))
    return 1 if [c for c in outcome["checks"] if c["status"] == "fail"] else 0


def cmd_game_day(args: argparse.Namespace) -> int:
    from .chaos import render_post_mortem, run_game_day

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out_dir = Path(args.out or f".docket/game-day-{stamp}")
    policy = Policy(name="game day", caps=Caps(max_usd=None))
    if args.max_usd:
        policy.caps.set_max_usd(Decimal(args.max_usd))
    if args.online and not args.max_usd:
        # Refusing here is the whole point: a game day against a real provider without a
        # cap is an experiment with an unbounded bill.
        _out(
            sys.stderr,
            f"{PROGRAM}: --online spends real money; pass --max-usd to bound it (the phases run the "
            "synthetic agent, and a cap also proves the cap path on live traffic).",
        )
        return 43
    phases = None
    if args.phases:
        from .chaos import GAME_DAY_PHASES

        wanted = [name.strip() for name in args.phases.split(",") if name.strip()]
        unknown = [name for name in wanted if name not in {p.name for p in GAME_DAY_PHASES}]
        if unknown:
            _out(
                sys.stderr,
                f"{PROGRAM}: unknown phase(s) {unknown}; known: {[p.name for p in GAME_DAY_PHASES]}",
            )
            return 43
        phases = [p for p in GAME_DAY_PHASES if p.name in wanted]
    _out(sys.stdout, f"{PROGRAM} game day — {len(phases) if phases else 'all'} phase(s), writing to {out_dir}")
    report = run_game_day(root=out_dir, phases=phases, policy=policy)
    post_mortem = render_post_mortem(report)
    out_dir.mkdir(parents=True, exist_ok=True)
    from .util import atomic_write

    atomic_write(out_dir / "post-mortem.md", post_mortem)
    atomic_write(out_dir / "game-day.json", json.dumps(report, indent=2, sort_keys=True))
    if args.json:
        _out(
            sys.stdout,
            json.dumps(
                {
                    "phases": report["phases"],
                    "phases_ok": report["phases_ok"],
                    "phases_total": report["phases_total"],
                    "failed_checks": report["failed_checks"],
                    "skipped_checks": report["skipped_checks"],
                    "post_mortem": str(out_dir / "post-mortem.md"),
                    "record": str(out_dir / "game-day.json"),
                },
                indent=2,
            ),
        )
    else:
        for record in report["phases"]:
            counts: dict[str, int] = {}
            for check in record.get("checks", []):
                counts[check["status"]] = counts.get(check["status"], 0) + 1
            _out(
                sys.stdout,
                f"  {record['phase']:18} exit {str(record.get('exit_code')):>4}  "
                f"{counts.get('pass', 0)} passed, {counts.get('fail', 0)} failed, {counts.get('skip', 0)} skipped",
            )
        _out(sys.stdout)
        _out(
            sys.stdout,
            f"{PROGRAM}: {report['phases_ok']}/{report['phases_total']} phases met every stated expectation",
        )
        for failed in report["failed_checks"]:
            _out(sys.stdout, f"  FAIL  {failed['phase']}: {failed['name']} — {failed['detail']}")
        _out(sys.stdout, f"  post-mortem: {out_dir / 'post-mortem.md'}")
        _out(sys.stdout, f"  raw record:  {out_dir / 'game-day.json'}")
    return 1 if report["failed_checks"] else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    if args.quarantine_torn_tail:
        result = quarantine_torn_tail(args.quarantine_torn_tail)
        _out(sys.stdout, f"{PROGRAM} doctor --quarantine-torn-tail {args.quarantine_torn_tail}")
        if result.performed:
            _out(sys.stdout, f"  moved   {result.moved_bytes} byte(s) to {result.sidecar}")
            _out(sys.stdout, f"  kept    {result.kept_bytes} byte(s) in place")
            _out(sys.stdout, "  the ledger now parses cleanly; the moved bytes were preserved, not deleted.")
            return 0
        _out(sys.stdout, f"  nothing moved: {result.reason}")
        return 0 if "parses cleanly" in result.reason else 1

    if args.bundle:
        path = write_diagnostic_bundle(Path(args.docket_dir).resolve(), Path(args.bundle))
        _out(sys.stdout, f"{PROGRAM}: wrote diagnostic bundle {path}")
        _out(
            sys.stdout,
            "  contents: environment, ledger diagnostics, receipts summary, doctor output (no ledger bodies)",
        )
        return 0

    table = PriceTable.load()
    checks = [
        ("python", sys.version.split()[0], sys.version_info >= (3, 11)),
        ("node (for verify-js)", shutil.which("node") or "not found", bool(shutil.which("node"))),
        ("docket dir writable", str(Path(args.docket_dir).resolve()), os.access(Path.cwd(), os.W_OK)),
        ("price table", f"{table.as_of} · {len(table.prices)} patterns", True),
    ]
    try:
        from .signing import ed25519_available

        checks.append(
            ("cryptography (signing)", "installed" if ed25519_available() else "optional, not installed", True)
        )
    except Exception as exc:  # pragma: no cover
        checks.append(("cryptography (signing)", str(exc), True))
    ledger_dir = Path(args.docket_dir) / "runs"
    ledgers = sorted(ledger_dir.glob("*.jsonl")) if ledger_dir.exists() else []
    unhealthy = []
    for path in ledgers:
        _, diagnostics = Ledger.read_with_diagnostics(path)
        if not diagnostics.get("healthy", False):
            unhealthy.append((path, diagnostics))
    checks.append(
        (
            "ledgers",
            f"{len(ledgers)} found" + (f", {len(unhealthy)} damaged" if unhealthy else ", all parse"),
            not unhealthy,
        )
    )
    free = shutil.disk_usage(Path.cwd()).free
    checks.append(("free space", f"{free // (1024 * 1024)} MiB where the ledger lives", free > 16 * 1024 * 1024))

    _out(sys.stdout, f"{PROGRAM} {__version__} doctor")
    for name, detail, ok in checks:
        _out(sys.stdout, f"  {'ok ' if ok else 'warn'}  {name:24} {detail}")
    for path, diagnostics in unhealthy:
        _out(sys.stdout)
        _out(sys.stdout, f"  damaged ledger: {path}")
        if diagnostics.get("torn_tail"):
            tail = diagnostics["torn_tail"]
            _out(sys.stdout, f"    torn final line at line {tail['line']} ({tail['bytes']} bytes): {tail['error']}")
            _out(sys.stdout, f"    recover with: {PROGRAM} doctor --quarantine-torn-tail {path}")
            _out(sys.stdout, "    (the moved bytes are kept beside the ledger; nothing is deleted)")
        if diagnostics.get("bad_lines"):
            first = diagnostics["bad_lines"][0]
            _out(sys.stdout, f"    line {first['line']} is not valid JSON: {first['error']}")
            _out(sys.stdout, "    this is not a torn write: the file was edited or corrupted. Preserve it as evidence.")
        if not diagnostics.get("utf8", True):
            _out(sys.stdout, f"    {diagnostics.get('error')}")
    _out(sys.stdout)
    _out(sys.stdout, "Everything Docket needs to record and verify is in the standard library.")
    return 0 if not unhealthy else 1


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Every agent run gets a docket number: hard caps, and a tamper-evident receipt anyone can verify.",
        epilog="Docs: https://github.com/docket-dev/docket · Spec: spec/docket-receipt-1.md",
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {__version__}")
    sub = parser.add_subparsers(dest="subcommand")

    run = sub.add_parser("run", help="run a command under a policy, recording a receipt")
    run.add_argument("--policy", "-p", help="policy TOML file (see `docket init`)")
    run.add_argument("--prices", help="price table JSON (default: the one bundled with this version)")
    run.add_argument("--docket-dir", default=".docket", help="where ledgers and receipts live (default: .docket)")
    run.add_argument("--cwd", help="working directory for the child process")
    run.add_argument(
        "--profile", choices=["full", "minimal"], default="full", help="how many provider base URLs to set"
    )
    run.add_argument(
        "--http-proxy", action="store_true", help="also export HTTP_PROXY/HTTPS_PROXY for full egress control"
    )
    run.add_argument("--record-bodies", action="store_true", help="store request bodies in the ledger (off by default)")
    run.add_argument("--seal", action="store_true", help="seal the receipt with a local HMAC key")
    run.add_argument("--sign", metavar="KEY_PATH", help="sign the receipt with an Ed25519 key (needs docket[sign])")
    run.add_argument("--label", help="a human label for this run")
    run.add_argument("--report", choices=["summary", "full"], default="summary")
    run.add_argument(
        "--upstream",
        action="append",
        metavar="NAME=URL",
        help="override a provider's base URL (repeatable); what a container, a mock provider or an "
        "OpenAI-compatible gateway needs",
    )
    run.add_argument(
        "--port",
        type=_port_number,
        default=0,
        help="bind the boundary to this loopback port (default: any free port); a fixed port is "
        "what a readiness probe or a metrics scrape needs",
    )
    run.add_argument("--max-usd", type=_cap_amount, help="override the USD cap")
    run.add_argument("--max-tokens", type=_whole_number, help="override the token cap")
    run.add_argument("--max-calls", type=_whole_number, help="override the model-call cap")
    run.add_argument("--max-minutes", type=_positive_minutes, help="override the wall-clock cap (minutes)")
    run.add_argument("--on-breach", choices=["kill", "deny"], help="override breach behaviour")
    run.add_argument("--budget-mode", choices=["usd", "quota"], help="usd for metered APIs, quota for subscriptions")
    run.add_argument(
        "--max-calls-per-minute",
        type=_at_least_one,
        help="throttle: refuse calls beyond this rate (recorded, HTTP 429)",
    )
    run.add_argument("--max-events", type=_whole_number, help="ceiling on this run's own ledger events")
    run.add_argument("--fail-fast-evidence", action="store_true", help="stop the run if evidence cannot be written")
    run.add_argument(
        "--fault-inject",
        metavar="SPEC",
        help="chaos testing: make the boundary misbehave (delay=250ms, status=503:0.5, drop, reset, truncate, corrupt, slow)",
    )
    run.add_argument("--fault-seed", type=int, default=0, help="seed for --fault-inject (same seed, same faults)")
    run.add_argument(
        "--snapshot",
        action="store_true",
        help="copy watched file contents before the run, so it can be undone (needs [files] watch)",
    )
    run.add_argument(
        "--snapshot-max-bytes",
        type=int,
        default=None,
        help="budget for --snapshot (default 64 MiB; the run is refused if the tree does not fit)",
    )
    run.add_argument(
        "--rollback-on-breach",
        action="store_true",
        help="if a cap is breached, restore the watched tree from the snapshot and record it",
    )
    run.add_argument("command", nargs=argparse.REMAINDER, help="command to run (after --)")
    run.set_defaults(func=cmd_run)

    restore = sub.add_parser(
        "restore", help="undo the file changes a recorded run made, from its --snapshot (dry run by default)"
    )
    restore.add_argument("receipt", help="the receipt of the run to undo")
    restore.add_argument("--apply", action="store_true", help="actually restore (default: show the plan)")
    restore.add_argument(
        "--root",
        help="the tree to restore into; defaults to the working directory the run recorded, "
        "and is required when the receipt, ledger and manifest do not agree on one",
    )
    restore.add_argument("--force", action="store_true", help="restore even if a file changed after the run")
    restore.add_argument("--ledger", help="the run's ledger file (default: beside the receipt)")
    restore.add_argument("--snapshot-store", help="the snapshot directory (default: beside the receipt)")
    restore.add_argument("--docket-dir", help="where the run kept its evidence (default: the receipt's parent)")
    restore.add_argument("--json", action="store_true")
    restore.set_defaults(func=cmd_restore)

    verify = sub.add_parser("verify", help="verify a receipt offline; exit 1 if it does not check out")
    verify.add_argument("receipt")
    verify.add_argument("--ledger", help="also check the ledger file it came from")
    verify.add_argument("--seal-key", help="HMAC key file, to check a sealed receipt")
    verify.add_argument("--public-key", help="expected Ed25519 public key (hex) of the signer")
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(func=cmd_verify)

    report = sub.add_parser("report", help="render a receipt")
    report.add_argument("receipt", nargs="?")
    report.add_argument("--markdown", action="store_true", help="emit markdown (for PRs and tickets)")
    report.set_defaults(func=cmd_report)

    explain = sub.add_parser("explain", help="explain where the money and the risk went")
    explain.add_argument("receipt", nargs="?")
    explain.add_argument("--ledger", help="ledger to analyse (defaults to the newest run)")
    explain.add_argument("--json", action="store_true")
    explain.set_defaults(func=cmd_explain)

    diff = sub.add_parser("diff", help="compare two receipts (drift after a prompt or model change)")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.add_argument("--json", action="store_true")
    diff.set_defaults(func=cmd_diff)

    demo = sub.add_parser("demo", help="watch a runaway agent get stopped, offline, no API key")
    demo.add_argument("--max-usd", type=_cap_amount, default="2.00")
    demo.add_argument("--tamper", action="store_true", help="also show tamper detection")
    demo.add_argument("--keep", action="store_true", help="keep the demo workspace")
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(func=cmd_demo)

    init = sub.add_parser("init", help="write a starter policy")
    init.add_argument("--path", default="docket.toml")
    init.add_argument("--name", default="default")
    init.add_argument("--max-usd", type=_cap_amount, default="5.00")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    prices = sub.add_parser("prices", help="show the price table and its provenance")
    prices.add_argument("--path", help="use a different price file")
    prices.add_argument("--json", action="store_true")
    prices.set_defaults(func=cmd_prices)

    selftest = sub.add_parser(
        "selftest",
        help="exercise the boundary's own guarantees (forwarding, caps, breaker, faults, ledger failure)",
    )
    selftest.add_argument("--json", action="store_true")
    selftest.add_argument("--keep", action="store_true", help="keep the probe workspace")
    selftest.set_defaults(func=cmd_selftest)

    chaos = sub.add_parser("chaos", help="run a command under injected faults and audit what happened")
    chaos.add_argument(
        "--faults",
        default="",
        help="fault spec: delay=250ms:0.5, status=500:1.0, drop, reset, slow=2s, truncate, corrupt (seed=N)",
    )
    chaos.add_argument("--seed", type=int, default=0)
    chaos.add_argument("--calls", type=int, default=8, help="calls for the bundled synthetic agent")
    chaos.add_argument("--mode", choices=["bounded", "retry"], default="retry")
    chaos.add_argument("--max-usd", type=_cap_amount, help="USD cap for the experiment")
    chaos.add_argument("--json", action="store_true")
    chaos.add_argument("--keep", action="store_true")
    chaos.add_argument("command", nargs=argparse.REMAINDER, help="command to run (after --)")
    chaos.set_defaults(func=cmd_chaos)

    game_day = sub.add_parser(
        "game-day", help="run the scripted failure phases against an agent and write a post-mortem"
    )
    game_day.add_argument("--out", help="where to write the record (default .docket/game-day-<stamp>)")
    game_day.add_argument("--phases", help="comma-separated subset of phases to run")
    game_day.add_argument(
        "--online", action="store_true", help="use your configured provider instead of a scripted one"
    )
    game_day.add_argument("--max-usd", type=_cap_amount, help="USD cap per phase (required with --online)")
    game_day.add_argument("--json", action="store_true")
    game_day.set_defaults(func=cmd_game_day)

    doctor = sub.add_parser("doctor", help="check the local environment and the health of existing ledgers")
    doctor.add_argument("--docket-dir", default=".docket")
    doctor.add_argument(
        "--quarantine-torn-tail",
        metavar="LEDGER",
        help="move an incomplete final line aside, keeping every byte (refuses mid-file damage)",
    )
    doctor.add_argument(
        "--bundle",
        metavar="PATH",
        nargs="?",
        const="docket-diagnostics.tar.gz",
        help="write a diagnostic bundle for a bug report (environment, ledger health, no bodies)",
    )
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "subcommand", None):
        parser.print_help()
        return 0
    if args.subcommand == "run" and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive path
        return 130
    except (OSError, ValueError, InvalidOperation, DecimalException) as exc:
        # A decimal exception's `args` is often just its class (that is where
        # `[<class 'decimal.ConversionSyntax'>]` came from), so the message is built rather than
        # printed raw: the type is always named, and the detail only when there is one.
        detail = str(exc) or (exc.args[0] if exc.args and isinstance(exc.args[0], str) else "")
        print(
            f"{PROGRAM}: {type(exc).__name__}: {detail}" if detail else f"{PROGRAM}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
