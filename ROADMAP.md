# Roadmap

Themes, not promises: dates are targets and slip when reality intervenes. Anything marked
💰 is a sponsored deliverable (see [`SPONSORS.md`](SPONSORS.md)); the list is public so that
funding has consequences that can be checked.

## 0.1 — the bounded run (shipped 2026-10-13)

- [x] boundary proxy with cap enforcement (`max_usd`, `max_tokens`, `max_model_calls`, `max_wall_seconds`)
- [x] append-only hash-chained ledger, canonical JSON, RFC 6962 Merkle root
- [x] receipts with embedded price table, usage inclusion proofs, limits disclosure
- [x] offline `docket verify` (Python) and an independent Node verifier, both bound to the
      committed conformance vectors
- [x] findings engine: runaway loops, uncached repeats, oversized context, denied calls,
      deny-write violations, premium-model leakage
- [x] filesystem snapshots and diffs, egress decisions, SDK + raw-HTTP recording
- [x] offline demo with a scripted provider, and a tamper demonstration
- [x] zero runtime dependencies; format specification `docket/receipt/v1`
- [x] mutation testing of the cryptographic modules and the ledger (`tools/mutate.py`), with
      every survivor classified as a gap or an equivalent mutant — scores in
      [`docs/testing.md`](docs/testing.md#mutation-testing)

## 0.2 — make it easy to adopt — *target: 6 weeks*

- [ ] `docket init --from <framework>` presets for Claude Code, Codex CLI, Gemini CLI, aider,
      OpenCode, and a generic HTTP profile
- [ ] GitHub Action: run an agent step under a policy, upload the receipt as an artifact,
      fail the job on a policy violation or an unverifiable receipt
- [ ] `docket watch` — tail a live run with a spend meter, safe to leave on a second terminal
- [ ] receipt `--redact` profiles for circulating evidence externally (paths, argv, secrets)
- [ ] honest coverage: record provider responses the boundary saw *without* parsing them
      (unknown providers get recorded, not skipped)
- [ ] packaging: `pipx`-friendly docs, `uv tool install` snippet, Homebrew formula 💰

## 0.3 — verification anyone can run — *target: Q1 2027*

- [ ] Go verifier (single static binary, no dependencies) 💰
- [ ] Rust verifier (crate + CLI) 💰
- [ ] TypeScript/browser verifier (verify a receipt by dropping it into a static page, fully
      client-side) 💰
- [ ] `docket verify --explain` — for each failing check, the smallest thing you can do next
- [ ] verifier conformance kit: vectors + a runner any implementation can point at, published
      with a compatibility table
- [ ] a public "verify this receipt" static demo page (no upload; the file never leaves the
      browser)

## 0.4 — evidence that survives scrutiny — *target: Q2 2027*

- [ ] signed receipts by default in CI contexts (`Ed25519`, key from env or OS keychain)
- [ ] transparency-log anchoring: optional append of a receipt digest to a public log
      (RFC 6962-style, generic — the goal is a *trusted timestamp*, never a blockchain) 💰
- [ ] witness co-signing: a second party countersigns a receipt without seeing contents
- [ ] multi-run attestations: "these 200 receipts all verify against this key" for procurement
- [ ] `docket diff` semantics for policy changes: what a stricter cap would have done to last
      week's traffic (retrospective enforcement) 💰

## 1.0 — long-term support — *target: H2 2027*

- [ ] format frozen with a compatibility promise and a published deprecation policy 💰
- [ ] security-fix window for the previous major, stated in months, not vibes 💰
- [ ] documented disaster story: what to do when a receipt fails to verify in production
- [ ] case studies from regulated-industry adopters (published with their consent)

## Explicitly not on the roadmap

- **A hosted service.** There will be no Docket Cloud; the promise is that evidence need not
  pass through anyone's account.
- **A dashboard.** Rendering is someone else's product; Docket emits receipts, JSON and
  Markdown. Dashboards subscribe to the ledger format.
- **A sandbox.** Containers, gVisor, seccomp and hypervisors are better at enforcement than a
  user-space tool; Docket records and bounds, and says so.
- **Prompt/content recording by default.** Evidence that requires storing what people typed
  will not be the default, whatever it would cost to make it a feature.
- **Blockchain anything.** A transparency log needs witnesses and hashing, not tokens.
