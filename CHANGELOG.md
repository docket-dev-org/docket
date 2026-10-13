# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the tool follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The receipt format is versioned
separately by its tag (`docket/receipt/v1`) and changes only under the format process in
[`CONTRIBUTING.md`](CONTRIBUTING.md#changing-the-format).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-10-13

First release: a bounded, legible, provable agent run.

### Added

- **Boundary** (`docket run -- <command>`): a loopback recording proxy that injects provider
  base URLs (or `HTTP_PROXY` with `--http-proxy`), enforces policy before forwarding a call,
  accrues cost from provider-reported usage, and terminates the supervised process group
  (`SIGTERM`, then `SIGKILL` after a grace period) when a cap is exceeded. Exit codes:
  `0` clean, `42` cap exceeded, `43` policy error, `70` internal, `130` interrupted.
- **Ledger**: append-only JSONL, each event carrying `seq`, `ts`, `type`, `data`, `prev` and
  `hash`; the first event binds to `sha256("docket/genesis/v1")`. Canonical JSON with integers
  only (fractional values are decimal strings) so that the same bytes verify in any runtime.
- **Receipts**: `docket/receipt/v1` with an RFC 6962 Merkle root over event hashes, per-call
  usage rows carrying inclusion proofs and re-bindable event hashes, the exact price table
  digest, deterministic findings with evidence sequence numbers, and a `limits_disclosure`
  block that verification fails if absent.
- **Verification**: offline `docket verify` (Python, zero dependencies) and an independent
  verifier in `verify-js/verify.mjs` (Node standard library only), both bound to the committed
  conformance vectors in `spec/vectors/`. Checks report `SKIP` with a reason rather than
  passing when they cannot be performed.
- **Findings**: runaway loops (byte-identical repeated requests), repeated context billed
  uncached, oversized context calls, policy-blocked calls, deny-write violations, premium-model
  leakage and retry storms — each with severity, confidence (`deterministic` / `heuristic`),
  attributed impact and a cited evidence trail.
- **Recording surfaces**: SDK (`docket.Session`, `record_model_call`, `record_tool`,
  `record_note`) for Python agents, and `/docket/tool/call` + `/docket/note` HTTP endpoints so
  agents in any language can write to the ledger.
- **Policy**: TOML policy with caps, model allow/deny globs with first-match ordering, watched
  and ignore globs, `deny_write` scopes, egress modes (`tunnel`, `allow`, `deny`, `allowlist`),
  price-table override, and `on_breach = "kill" | "deny"`.
- **Demo**: `docket demo` runs a scripted runaway agent against an in-process fake provider —
  offline, no API key, deterministic clock — prints findings, verifies the receipt, and with
  `--tamper` edits one byte of the ledger to show verification failing at the exact event.
- **Specification**: `spec/docket-receipt-1.md`, including the canonical-form rules, the
  verification procedure, and §9 *what a valid receipt does and does not establish*.
- **Conformance vectors**: `spec/vectors/{canonical,merkle,ledger,receipt,receipt-digest}.json`
  plus a generator, consumed as the cross-implementation contract by both test suites.

### Notes

- Zero runtime dependencies. `docket[sign]` is the only extra that installs anything
  (`cryptography`, for Ed25519 signatures).
- Prices in `src/docket/prices.json` are as of 2026-10-13 with sources recorded in the file;
  they are estimates for budgeting, and every receipt states which table it used and flags
  any model priced by fallback as `assumed`.

[Unreleased]: https://github.com/docket-dev/docket/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/docket-dev/docket/releases/tag/v0.1.0
