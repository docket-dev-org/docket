"""The ``docket`` command line interface."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .ledger import Ledger
from .policy import DEFAULT_TOML, Caps, Policy
from .pricing import PriceTable
from .receipt import load_receipt
from .report import diff_receipts, render_diff, render_markdown, render_receipt
from .supervisor import EXIT_CAP_EXCEEDED, EXIT_OK, Supervisor
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
    if args.docket_dir:
        policy.files.ignore = policy.files.ignore + [f"{args.docket_dir}/**"]

    seal_key = None
    if args.seal:
        from .signing import load_or_create_seal_key

        seal_key = load_or_create_seal_key(Path(args.docket_dir) / "keys" / "seal.key")

    if not args.command:
        _out(sys.stderr, f"{PROGRAM}: nothing to run. Usage: {PROGRAM} run -- <command> [args…]")
        return 43

    supervisor = Supervisor(
        args.command,
        policy=policy,
        docket_dir=args.docket_dir,
        cwd=args.cwd,
        profile=args.profile,
        http_proxy=args.http_proxy,
        record_bodies=args.record_bodies,
        seal_key=seal_key,
        sign_key=args.sign,
        label=args.label,
        price_table=PriceTable.load(args.prices) if args.prices else None,
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
    from .explain import analyse, total_estimated_savings

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
    for finding in findings:
        impact = f"  [{finding['usd_impact']}]" if finding.get("usd_impact") else ""
        _out(sys.stdout, f"  {str(finding['severity']).upper():9} {finding['title']}{impact}")
        _out(sys.stdout, f"            {finding['detail']}")
        if finding.get("remedy"):
            _out(sys.stdout, f"            → {finding['remedy']}")
        _out(sys.stdout, f"            evidence: events {finding['evidence'][:8]}")
        _out(sys.stdout)
    _out(sys.stdout, f"  attributed impact: {total_estimated_savings(findings)}")
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
    _out(sys.stdout, "Docket estimates only; it never bills anyone. Override for one run with `docket run --prices <file>`.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    table = PriceTable.load()
    checks = [
        ("python", sys.version.split()[0], sys.version_info >= (3, 11)),
        ("node (for verify-js)", shutil.which("node") or "not found", bool(shutil.which("node"))),
        ("docket dir writable", str(Path(args.docket_dir).resolve()), os.access(Path.cwd(), os.W_OK)),
        ("price table", f"{table.as_of} · {len(table.prices)} patterns", True),
    ]
    try:
        from .signing import ed25519_available

        checks.append(("cryptography (signing)", "installed" if ed25519_available() else "optional, not installed", True))
    except Exception as exc:  # pragma: no cover
        checks.append(("cryptography (signing)", str(exc), True))
    _out(sys.stdout, f"{PROGRAM} {__version__} doctor")
    for name, detail, ok in checks:
        _out(sys.stdout, f"  {'ok ' if ok else 'warn'}  {name:24} {detail}")
    _out(sys.stdout)
    _out(sys.stdout, "Everything Docket needs to record and verify is in the standard library.")
    return 0


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
    run.add_argument("--profile", choices=["full", "minimal"], default="full", help="how many provider base URLs to set")
    run.add_argument("--http-proxy", action="store_true", help="also export HTTP_PROXY/HTTPS_PROXY for full egress control")
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

    doctor = sub.add_parser("doctor", help="check the local environment")
    doctor.add_argument("--docket-dir", default=".docket")
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
