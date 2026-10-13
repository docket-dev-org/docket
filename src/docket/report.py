"""Human-facing rendering: the terminal report and the markdown report."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .explain import total_estimated_savings
from .util import human_duration, human_int, human_usd

WIDTH = 78
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
CYAN = "\033[36m"
RESET = "\033[0m"

SEVERITY_STYLE = {
    "critical": (RED, "CRITICAL"),
    "high": (YELLOW, "HIGH"),
    "medium": (CYAN, "MEDIUM"),
    "low": (DIM, "LOW"),
}


def _paint(text: str, code: str, color: bool) -> str:
    return f"{code}{text}{RESET}" if color else text


def _row(label: str, value: str, color: bool) -> str:
    return f"  {_paint(label.ljust(10), DIM, color)} {value}"


def render_receipt(receipt: dict[str, Any], *, color: bool = True, show_findings: bool = True) -> str:
    """Render a receipt as a compact terminal report."""
    session = receipt.get("session", {})
    totals = receipt.get("totals", {})
    caps = receipt.get("policy", {}).get("document", {}).get("caps", {})
    lines: list[str] = []
    number = receipt.get("docket_number", "DKT-????")
    title = f" DOCKET {number} "
    lines.append(_paint("┌─" + title + "─" * max(0, WIDTH - len(title) - 3) + "┐", BOLD, color))

    argv = " ".join(str(a) for a in (session.get("argv") or [])[:6])
    if len(argv) > WIDTH - 5:
        argv = argv[: WIDTH - 6] + "…"
    lines.append("│ " + argv.ljust(WIDTH - 4) + " │")
    lines.append("└" + "─" * (WIDTH - 2) + "┘")

    breach = session.get("breached")
    exit_text = str(session.get("exit_code"))
    if breach:
        exit_text += _paint(f"  (stopped: {breach})", RED, color)
    lines.append(_row("exit", exit_text, color))
    lines.append(_row("duration", human_duration(float(session.get("duration_seconds") or 0)), color))

    cost = totals.get("cost_usd", "0.00")
    if caps.get("max_usd") and receipt.get("policy", {}).get("document", {}).get("budget", {}).get("mode") == "usd":
        lines.append(_row("spend", f"{human_usd(cost)} of {human_usd(caps['max_usd'])} cap", color))
    else:
        lines.append(_row("spend", f"{human_usd(cost)} (estimated at list prices)", color))
    lines.append(
        _row(
            "calls",
            f"{human_int(int(totals.get('calls') or 0))}"
            + (f" / {caps.get('max_model_calls')} max" if caps.get("max_model_calls") else "")
            + f"   denied: {receipt.get('model_calls', {}).get('denied', 0)}",
            color,
        )
    )
    lines.append(
        _row(
            "tokens",
            f"{human_int(int(totals.get('total_tokens') or 0))} "
            f"(in {human_int(int(totals.get('input_tokens') or 0))}"
            f" · cached {human_int(int(totals.get('cached_input_tokens') or 0))}"
            f" · out {human_int(int(totals.get('output_tokens') or 0))})",
            color,
        )
    )
    if totals.get("assumed_price_models"):
        lines.append(
            _row(
                "pricing",
                _paint(
                    "assumed for: " + ", ".join(totals["assumed_price_models"]) + " (not in price table)",
                    YELLOW,
                    color,
                ),
                color,
            )
        )
    files = receipt.get("files", {})
    lines.append(
        _row(
            "files",
            f"{files.get('changed', 0)} changed "
            f"(+{files.get('added', 0)} ~{files.get('modified', 0)} -{files.get('removed', 0)})"
            + (f"  {_paint(str(files.get('violations')) + ' deny-write violation(s)', RED, color)}" if files.get("violations") else ""),
            color,
        )
    )
    if receipt.get("tools", {}).get("calls"):
        lines.append(
            _row("tools", f"{receipt['tools']['calls']} ({', '.join(receipt['tools'].get('names', [])[:5])})", color)
        )

    findings = receipt.get("findings") or []
    if show_findings and findings:
        lines.append("")
        lines.append(_paint("FINDINGS", BOLD, color) + _paint("  (deterministic unless marked heuristic)", DIM, color))
        for finding in findings:
            code, label = SEVERITY_STYLE.get(str(finding.get("severity")), (DIM, "INFO"))
            impact = finding.get("usd_impact") or ""
            head = f"  {_paint(label.ljust(8), code, color)} {finding.get('title', '')}"
            if impact:
                head += _paint(f"  [{impact}]", DIM, color)
            lines.append(head)
            detail = str(finding.get("detail", ""))
            for chunk in _wrap(detail, WIDTH - 14):
                lines.append(" " * 12 + _paint(chunk, DIM, color))
            if finding.get("confidence") == "heuristic":
                lines.append(" " * 12 + _paint("(heuristic estimate)", YELLOW, color))
        savings = total_estimated_savings(findings)
        if savings != "$0.00":
            lines.append("")
            lines.append(_paint(f"  attributed impact: {savings}", BOLD, color))

    lines.append("")
    lines.append(_paint("WHAT THIS RECEIPT PROVES", BOLD, color))
    events = receipt.get("ledger", {}).get("events", 0)
    lines.append(f"  {_paint('✓', GREEN, color)} {events} ledger events in order, hash-chained and Merkle-rooted")
    lines.append(
        f"  {_paint('✓', GREEN, color)} {human_usd(cost)} recomputable from provider token counts and the embedded price table"
    )
    lines.append(
        f"  {_paint('✓', GREEN, color)} {receipt.get('files', {}).get('changed', 0)} filesystem change(s) with content digests"
    )
    limits = receipt.get("limits_disclosure", {})
    for item in (limits.get("not_observed") or [])[:2]:
        lines.append(f"  {_paint('✗', RED, color)} does not observe: {item}")
    lines.append("")
    lines.append(_paint("VERIFY IT YOURSELF (offline, no account)", BOLD, color))
    lines.append(f"  docket verify {number}.receipt.json")
    lines.append(f"  node verify-js/verify.mjs {number}.receipt.json")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def render_markdown(receipt: dict[str, Any]) -> str:
    """Render a receipt as markdown (for PR comments, tickets and audits)."""
    session = receipt.get("session", {})
    totals = receipt.get("totals", {})
    files = receipt.get("files", {})
    lines = [
        f"# Docket {receipt.get('docket_number', '')}",
        "",
        f"**Command** `{' '.join(str(a) for a in session.get('argv', []))}`  ",
        f"**Exit** {session.get('exit_code')}"
        + (f" (stopped by `{session.get('breached')}`)" if session.get("breached") else "")
        + f" · **Duration** {human_duration(float(session.get('duration_seconds') or 0))}",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| estimated cost | {human_usd(totals.get('cost_usd', '0.00'))} ({totals.get('cost_basis', '')}) |",
        f"| model calls | {totals.get('calls', 0)} ({receipt.get('model_calls', {}).get('denied', 0)} denied) |",
        f"| tokens | {human_int(int(totals.get('total_tokens') or 0))} "
        f"(in {human_int(int(totals.get('input_tokens') or 0))}, cached {human_int(int(totals.get('cached_input_tokens') or 0))}, "
        f"out {human_int(int(totals.get('output_tokens') or 0))}) |",
        f"| files changed | {files.get('changed', 0)} (+{files.get('added', 0)} ~{files.get('modified', 0)} -{files.get('removed', 0)}) |",
        f"| deny-write violations | {files.get('violations', 0)} |",
        f"| ledger | {receipt.get('ledger', {}).get('events', 0)} events · root `{str(receipt.get('ledger', {}).get('merkle_root'))[:16]}…` |",
        "",
    ]
    findings = receipt.get("findings") or []
    if findings:
        lines += ["## Findings", "", "| severity | finding | impact | confidence |", "| --- | --- | --- | --- |"]
        for finding in findings:
            lines.append(
                f"| {finding.get('severity')} | {finding.get('title')} | {finding.get('usd_impact') or '—'} | "
                f"{finding.get('confidence')} |"
            )
        lines += [
            "",
            f"Attributed impact: **{total_estimated_savings(findings)}**.",
            "",
        ]
        for finding in findings:
            if finding.get("detail"):
                lines.append(f"- **{finding.get('title')}** — {finding.get('detail')}")
                if finding.get("remedy"):
                    lines.append(f"  - remedy: {finding.get('remedy')}")
        lines.append("")
    by_model = totals.get("by_model") or []
    if by_model:
        lines += ["## Model mix", "", "| model | calls | input | cached | output | estimated cost |", "| --- | --- | --- | --- | --- | --- |"]
        for row in by_model:
            lines.append(
                f"| {row['model']} | {row['calls']} | {human_int(row['input_tokens'])} | "
                f"{human_int(row['cached_input_tokens'])} | {human_int(row['output_tokens'])} | {human_usd(row['cost_usd'])} |"
            )
        lines.append("")
    lines += [
        "## Verification",
        "",
        "```sh",
        f"docket verify {receipt.get('docket_number')}.receipt.json",
        "```",
        "",
        "The receipt embeds the price table it used, so the cost total above can be recomputed from the",
        "provider-reported token counts without trusting Docket.",
        "",
    ]
    return "\n".join(lines)


def diff_receipts(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Compare two receipts: behaviour drift, cost drift, cap drift."""
    def num(d: dict[str, Any], *keys: str) -> Decimal:
        node: Any = d
        for key in keys:
            node = (node or {}).get(key) if isinstance(node, dict) else None
        try:
            return Decimal(str(node or 0))
        except Exception:  # pragma: no cover - defensive
            return Decimal("0")

    a_totals, b_totals = a.get("totals", {}), b.get("totals", {})
    a_models = {m["model"]: m for m in a_totals.get("by_model", [])}
    b_models = {m["model"]: m for m in b_totals.get("by_model", [])}
    changes = {
        "cost_usd": (str(a_totals.get("cost_usd", "0.00")), str(b_totals.get("cost_usd", "0.00"))),
        "calls": (int(a_totals.get("calls") or 0), int(b_totals.get("calls") or 0)),
        "total_tokens": (int(a_totals.get("total_tokens") or 0), int(b_totals.get("total_tokens") or 0)),
        "files_changed": (int(a.get("files", {}).get("changed") or 0), int(b.get("files", {}).get("changed") or 0)),
        "exit_code": (a.get("session", {}).get("exit_code"), b.get("session", {}).get("exit_code")),
        "denied_calls": (int(a.get("model_calls", {}).get("denied") or 0), int(b.get("model_calls", {}).get("denied") or 0)),
        "models_added": (None, sorted(set(b_models) - set(a_models))),
        "models_removed": (None, sorted(set(a_models) - set(b_models))),
    }
    cost_delta = num(b, "totals", "cost_usd") - num(a, "totals", "cost_usd")
    return {
        "from": a.get("docket_number"),
        "to": b.get("docket_number"),
        "changes": changes,
        "cost_delta_usd": human_usd(cost_delta),
        "verdict": "identical shape" if changes["calls"][0] == changes["calls"][1] else "behaviour changed",
    }


def render_diff(diff: dict[str, Any], *, color: bool = True) -> str:
    lines = [
        _paint(f"DIFF {diff.get('from')} → {diff.get('to')}", BOLD, color),
        f"  cost delta: {diff.get('cost_delta_usd')}   {diff.get('verdict')}",
        "",
    ]
    for key, value in diff.get("changes", {}).items():
        old, new = value if isinstance(value, tuple) else (None, value)
        if old is None and not new:
            continue
        marker = " " if old == new else ("+" if key.startswith("models_added") else "~")
        lines.append(f"  {marker} {key.ljust(16)} {old}  →  {new}")
    return "\n".join(lines)
