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
from decimal import Decimal, InvalidOperation
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
from .verify import verify_receipt

PROGRAM = "docket"


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
    try:
        policy = Policy.load(args.policy)
    except (OSError, ValueError) as exc:
        _out(sys.stderr, f"{PROGRAM}: cannot load policy: {exc}")
        return 43
    overrides: list[str] = []
    if args.max_usd is not None:
        policy.caps.max_usd = Decimal(args.max_usd)
        overrides.append(f"max_usd={args.max_usd}")
    if args.max_tokens is not None:
        policy.caps.max_tokens = int(args.max_tokens)
        overrides.append(f"max_tokens={args.max_tokens}")
    if args.max_calls is not None:
        policy.caps.max_model_calls = int(args.max_calls)
        overrides.append(f"max_model_calls={args.max_calls}")
    if args.max_minutes is not None:
        policy.caps.max_wall_seconds = int(float(args.max_minutes) * 60)
        overrides.append(f"max_wall_seconds={policy.caps.max_wall_seconds}")
    if args.on_breach:
        policy.caps.on_breach = args.on_breach
    if args.budget_mode:
        policy.budget_mode = args.budget_mode
    if args.max_calls_per_minute is not None:
        policy.resilience.max_calls_per_minute = int(args.max_calls_per_minute)
        overrides.append(f"max_calls_per_minute={args.max_calls_per_minute}")
    if args.max_events is not None:
        policy.resilience.max_events = int(args.max_events)
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
    if args.docket_dir:
        policy.files.ignore = policy.files.ignore + [f"{args.docket_dir}/**"]

    seal_key = None
    if args.seal:
        from .signing import load_or_create_seal_key

        seal_key = load_or_create_seal_key(Path(args.docket_dir) / "keys" / "seal.key")

    if not args.command:
        _out(sys.stderr, f"{PROGRAM}: nothing to run. Usage: {PROGRAM} run -- <command> [args…]")
        return 43

    upstream_override = os.environ.get("DOCKET_GAME_DAY_UPSTREAM")
    upstreams = None
    if upstream_override:
        # Used by `docket game-day` to point a real `docket run` at a local scripted
        # provider. Documented, single-purpose, and never set by Docket itself in normal
        # operation: the boundary is still the real boundary.
        try:
            upstreams = json.loads(upstream_override)
        except json.JSONDecodeError as exc:
            _out(sys.stderr, f"{PROGRAM}: bad DOCKET_GAME_DAY_UPSTREAM: {exc}")
            return 43
        if not isinstance(upstreams, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in upstreams.items()
        ):
            _out(sys.stderr, f"{PROGRAM}: DOCKET_GAME_DAY_UPSTREAM must be a JSON object of provider→URL")
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
    )
    if faults:
        _out(
            sys.stderr,
            f"{PROGRAM}: FAULT INJECTION ACTIVE ({'; '.join(f.describe() for f in faults)}"
            f", seed {args.fault_seed}) — this receipt records a test, not production behaviour",
        )
    result = supervisor.run()
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
                    "workspace": str(workspace),
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
        policy.caps.max_usd = Decimal(args.max_usd)
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
    )
    if args.json:
        _out(sys.stdout, json.dumps(outcome, indent=2))
    else:
        _out(sys.stdout, render_chaos(outcome))
        if args.keep:
            _out(sys.stdout)
            _out(sys.stdout, f"  workspace kept at {outcome['ledger_path'].rsplit('/', 2)[0]}")
    return 1 if [c for c in outcome["checks"] if c["status"] == "fail"] else 0


def cmd_game_day(args: argparse.Namespace) -> int:
    from .chaos import render_post_mortem, run_game_day

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out_dir = Path(args.out or f".docket/game-day-{stamp}")
    policy = Policy(name="game day", caps=Caps(max_usd=None))
    if args.max_usd:
        policy.caps.max_usd = Decimal(args.max_usd)
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
            marks = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}
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
        _out(sys.stdout, "  contents: environment, ledger diagnostics, receipts summary, doctor output (no ledger bodies)")
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
    run.add_argument("--max-usd", help="override the USD cap")
    run.add_argument("--max-tokens", help="override the token cap")
    run.add_argument("--max-calls", help="override the model-call cap")
    run.add_argument("--max-minutes", help="override the wall-clock cap (minutes)")
    run.add_argument("--on-breach", choices=["kill", "deny"], help="override breach behaviour")
    run.add_argument("--budget-mode", choices=["usd", "quota"], help="usd for metered APIs, quota for subscriptions")
    run.add_argument("--max-calls-per-minute", help="throttle: refuse calls beyond this rate (recorded, HTTP 429)")
    run.add_argument("--max-events", help="ceiling on this run's own ledger events")
    run.add_argument("--fail-fast-evidence", action="store_true", help="stop the run if evidence cannot be written")
    run.add_argument(
        "--fault-inject",
        metavar="SPEC",
        help="chaos testing: make the boundary misbehave (delay=250ms, status=503:0.5, drop, reset, truncate, corrupt, slow)",
    )
    run.add_argument("--fault-seed", type=int, default=0, help="seed for --fault-inject (same seed, same faults)")
    run.add_argument("command", nargs=argparse.REMAINDER, help="command to run (after --)")
    run.set_defaults(func=cmd_run)

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
    demo.add_argument("--max-usd", default="2.00")
    demo.add_argument("--tamper", action="store_true", help="also show tamper detection")
    demo.add_argument("--keep", action="store_true", help="keep the demo workspace")
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(func=cmd_demo)

    init = sub.add_parser("init", help="write a starter policy")
    init.add_argument("--path", default="docket.toml")
    init.add_argument("--name", default="default")
    init.add_argument("--max-usd", default="5.00")
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
    chaos.add_argument("--max-usd", help="USD cap for the experiment")
    chaos.add_argument("--json", action="store_true")
    chaos.add_argument("--keep", action="store_true")
    chaos.add_argument("command", nargs=argparse.REMAINDER, help="command to run (after --)")
    chaos.set_defaults(func=cmd_chaos)

    game_day = sub.add_parser(
        "game-day", help="run the scripted failure phases against an agent and write a post-mortem"
    )
    game_day.add_argument("--out", help="where to write the record (default .docket/game-day-<stamp>)")
    game_day.add_argument("--phases", help="comma-separated subset of phases to run")
    game_day.add_argument("--online", action="store_true", help="use your configured provider instead of a scripted one")
    game_day.add_argument("--max-usd", help="USD cap per phase (required with --online)")
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
    except (OSError, ValueError, InvalidOperation) as exc:
        print(f"{PROGRAM}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
