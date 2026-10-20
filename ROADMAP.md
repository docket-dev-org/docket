# Roadmap

Themes, not promises: dates are targets and slip when reality intervenes.

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

## Shipped since 0.1 — what the "production engineering" list turned out to be

A concept-by-concept answer to the usual checklist (rate limiting, breakers, Kubernetes, Helm,
backpressure, exactly-once, p99, on-call, …) lives in
[`docs/production-coverage.md`](docs/production-coverage.md): what is implemented and tested, what is
recorded but not enforced, what is a validated artefact that has never been executed here, and what
is deliberately refused with a reason. The short version, as of this commit:

- [x] per-call latency recorded, summarised under a stated rule, and **recomputed by both verifiers**
- [x] `--port` (a fixed loopback port an orchestrator can probe), `--upstream NAME=URL` (a mock,
      gateway or self-hosted model, recorded as evidence without its query string), and the
      boundary's `/docket/healthz`, `/docket/readyz`, `/docket/metrics` documented
- [x] `DOCKET_AGENT_MODE=backoff`: full jitter, capped, honouring `Retry-After` — the client half of
      the rate-limit contract
- [x] a container recipe, a compose smoke test, a pod topology and a Helm chart, each checked
      statically by `tools/deploy_check.py` (16 checks) whose own tests break every invariant on
      purpose — and none of them executed, because no container runtime exists here
- [x] an invariant suite: one writer per ledger, 200 events from 8 threads, a replayed call recorded
      twice, a killed agent, a clock that lies
- [x] **load and endurance measured, not estimated**: `make load` (2,000 calls over 8 threads →
      175.93 calls/s, client p50 44 ms) and `make soak` (12,002 calls paced over ten minutes, 0 drops,
      receipt still verifying), with the boundary's memory measured at ≈1.1 KiB per recorded event —
      and a control run at half the duration showing the same per-event cost, so the growth is the
      event list rather than a leak. Reports in
      [`docs/measurements/2026-10-15-2core/`](docs/measurements/2026-10-15-2core/README.md); the
      harness refuses to call a run evidence when the client's count and the ledger disagree
- [x] `policy.denial_code`: one reader for "the boundary refused this", shared by the reference agent
      and the load driver, which fixed a real defect (a provider's own `error.code` was being read as
      a Docket refusal) and closed a 502 in `RateLimiter` (a `Decimal` rate used to fail inside the
      request; `nan` silently disabled the limiter)

## 0.2 — make it easy to adopt — *target: 6 weeks*

- [x] `docket init --from <framework>` presets for Claude Code, Codex CLI, Gemini CLI, aider,
      OpenCode, and a generic HTTP profile. Each writes a policy that names the models that
      framework calls and prints the command that attaches it. `tests/test_presets.py` loads
      every policy back through the real parser and runs five of them end to end against the
      scripted provider.
- [x] GitHub Action: run an agent step under a policy, upload the receipt as an artifact,
      fail the job on a policy violation or an unverifiable receipt. The composite action lives
      at `action.yml`, with `receipt`, `exit-code`, `spend`, `calls`, `verified` and
      `cap-exceeded` as outputs. `tests/test_action.py` executes its own `run:` blocks against
      a real supervised run — including the tampered-receipt and cap-fired paths — and
      `tests/test_action_smoke.py` covers the scripted provider and smoke agent it depends on.
      `.github/workflows/action-smoke.yml` runs it in Actions on every push.
- [ ] `docket watch` — tail a live run with a spend meter, safe to leave on a second terminal
- [x] receipt `--redact` profiles for circulating evidence externally (paths, argv, secrets).
      `docket report --redact paths,argv,hosts,secrets` writes a redacted receipt *and* ledger,
      re-chains the pair so it still verifies, records what it replaced under a `redaction`
      block, and never touches the originals. `tests/test_redact.py` asserts every profile on
      its own leaves a verifying pair, and that the strings a client should not see are absent
      from both files.
- [ ] honest coverage: record provider responses the boundary saw *without* parsing them
      (unknown providers get recorded, not skipped)
- [ ] packaging: `pipx`-friendly docs, `uv tool install` snippet, Homebrew formula 💰

**How this page is kept honest.** Every `[x]` above is a claim that something exists, and a claim
about code that nothing checks is the same failure mode as a stale test count: it was true when
written and nobody noticed when it stopped being true. `tests/test_roadmap.py` walks every checked
box on this page and requires it to name a file, a command, or a measurement that exists in the
tree. An unchecked box is a plan; a checked box is a statement of fact, and it is held to one.

## 0.3 — verification anyone can run — *target: Q1 2027*

- [x] Go verifier (single static binary, no dependencies). Shipped in 0.1.5: `verify-go/` builds
      with `go build`, passes `go vet`, `gofmt` and `go test`, and is one of the three
      implementations held to the 132-vector corpus. It was listed here as unshipped while all of
      that was true, which is the kind of stale row this page now has a guard against.
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
