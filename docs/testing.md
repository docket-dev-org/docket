# Testing

What is tested, with what, and what is deliberately not tested. The last table is the important
one: a testing document that only lists successes is a marketing page.

Everything here runs **offline**, with no API key and no network. That is not a convenience — it
is a constraint the project keeps on purpose, because a test suite that needs a provider is a
test suite that stops running on the day the provider has an incident. Where a provider is
needed, `docket.fakeupstream` is a real HTTP server on loopback that speaks the same wire format
and can be told to fail on cue.

## The suite

```console
$ make test          # 530 tests, ~90 s, no network
$ make check         # + ruff, + bandit, + the independent Node verifier
```

| file | tests | what it pins |
| --- | --- | --- |
| `test_canon.py` | 28 | canonical JSON, Merkle trees and proofs, the no-floats rule |
| `test_vectors.py` | 9 | the committed conformance vectors, including that regenerating them changes nothing |
| `test_ledger.py` | 12 | append/read, chain resumes, ceilings, `unwritten` bookkeeping |
| `test_ledger_faults.py` | 22 | a ledger on a failing filesystem: ENOSPC, EIO, a closed handle, an OS error while opening |
| `test_ledger_concurrency.py` | 4 | one ledger, eight writer threads: order, chain, agreement, file digest |
| `test_receipt_verify.py` | 19 | the receipt and every verification check, plus one name to one verdict |
| `test_proxy.py` | 15 | pass-through, streaming, policy enforcement, what is never recorded |
| `test_proxy_concurrency.py` | 6 | parallel calls through the real boundary: chain, totals, limiter, breaker, cap |
| `test_resilience.py` | 24 | breaker, limiter, fault injection grammar, evidence guard |
| `test_policy.py` | 11 | accept and reject paths for every policy key |
| `test_pricing.py` | 19 | price table, cost arithmetic, the missing-model path, ceilings |
| `test_explain.py` | 13 | analysis rules and their attributed impact |
| `test_supervisor.py` | 11 | the run lifecycle, exit codes, the evidence directory exclusion |
| `test_snapshot_restore.py` | 25 | snapshot, plan, apply, the CLI, idempotence, the restore record |
| `test_snapshot_confinement.py` | 25 | hostile documents: paths, digests, modes, symlinks, keys |
| `test_fsdiff.py` | 5 | what the file diff sees, with a relative and an absolute cwd |
| `test_chaos.py` | 29 | the fault grammar, `audit_run`, the game-day phases |
| `test_security.py` | 40 | red team: forgery, injection, bypass, confinement, secret capture |
| `test_cli_input.py` | 36 | numeric flags: every cap, every way to spell it wrong |
| `test_cli_sdk_demo.py` | 17 | the commands end to end, the SDK, the demo |
| `test_i18n.py` | 16 | locale independence, Unicode round trips, non-ASCII paths |
| `test_url_guards.py` | 28 | the two URL guards, and a syntax-tree walk that every `urlopen` uses one |
| `test_diagnostics.py` | 8 | the self-test's probes against a working installation |
| `test_signing_and_attach.py` | 8 | HMAC sealing, Ed25519 signing, agent-asserted events |
| `test_render_golden.py` | 3 | the rendered receipt, report and diff, against golden files |
| `test_fuzz_regressions.py` | 98 | one test per defect a fuzzer found (see below) |

## By kind

| kind | how it is done here |
| --- | --- |
| unit | per-module files above; the pure functions (`canon`, `pricing`, `policy`) are tested directly |
| integration | `test_proxy.py` / `test_supervisor.py`: real loopback HTTP, real subprocess, real files |
| system | `docket demo`, `docket game-day` and the clean-venv install in CI: the whole thing, as shipped |
| acceptance / UAT | the transcripts in the README and `docs/cli.md` are captured from real runs, not written by hand; `tools/transcript_run.py` reproduces them |
| functional | every documented feature has a test named after the claim it makes |
| non-functional | concurrency, locale, fault tolerance and cost; see the other tables |
| smoke | CI installs the wheel into a clean venv and runs `docket demo`, asserting `VERIFIED` appears |
| sanity | `docket doctor` and `docket selftest` (16 probes) before anything else, when something looks wrong |
| regression | `test_fuzz_regressions.py`: 98 named tests, each reproducing one defect that was found and fixed |
| end-to-end | `tools/transcript_run.py`: supervisor → proxy → child agent → ledger → receipt → verifier, offline |
| performance / load / stress | the game-day `rate-limit` and `ledger-pressure` phases, plus the concurrency files; no throughput target is claimed (see *Not covered*) |
| endurance / soak | **not covered** — see below |
| spike | `test_proxy_concurrency.py`: a burst of parallel calls against a one-call cap |
| volume | the game day's ledger-pressure phase grows a ledger to its ceiling and asserts the run stops rather than dropping events |
| scalability | one process, threads rather than workers; the ledger is the only shared resource and it is serialised deliberately |
| security | `test_security.py` (red team) + `bandit` in CI + the URL-guard walk |
| penetration | the red team's forgery cases: a doctored receipt, a doctored ledger, a doctored manifest, a path traversal, a setuid mode, a swapped blob |
| usability | the transcripts are the usability test; a change that makes the output harder to read shows up in the golden files |
| accessibility | the terminal output is plain UTF-8 box drawing with a `NO_COLOR` path, and the machine-readable form (`--json`, the markdown report) carries the same content — so nothing is *only* conveyed by colour |
| compatibility | CI matrix: Python 3.11, 3.12, 3.13; Node 20 for the independent verifier |
| reliability | `chaos.py` + the eight game-day phases; a broken ledger tails off, a full one is declared |
| maintainability | ruff (lint **and** format), bandit, and the seam rule in CONTRIBUTING (each claim has one test file) |
| localisation / i18n | `test_i18n.py`: identical digests under `C`, `de_DE`, `tr_TR`, `ja_JP`; NFC ≠ NFD; non-ASCII paths round-trip |
| black / white / grey box | black: the CLI and the JS verifier; white: internals such as `_open_ledger_handle`; grey: the red team, which reads the code and then attacks the interface |
| manual | the game-day post-mortem is read by a person; the record it writes is for that person |
| automation | `make check`; CI runs the same on every push |
| exploratory | the defects in "Found by testing" below were found by trying things, not by a plan |
| ad-hoc | `docket chaos --fault …` and `tools/fuzz.py --target …` are the ad-hoc entry points |
| monkey / gorilla | `docket game-day` kills the supervisor with SIGKILL mid-run and asserts what survives |
| boundary-value | every cap: 0, 1, exactly at the limit, one over; every ceiling in `pricing`/`policy` |
| equivalence partitioning | the fault grammar, the policy validators, the URL guards (loopback vs not, scheme by scheme) |
| mutation | **partial** — see below |
| static | `ruff check`, `ruff format --check`, `bandit -q -c pyproject.toml -r src tools` |
| dynamic | `tools/fuzz.py`: six property-based targets, time-boxed, seeded and reproducible |
| alpha / beta | **not applicable yet** — no release channel beyond the source tree |
| A / B | **not applicable** — nothing here is a user-facing choice to optimise |
| recovery | the game-day `rollback` phase, `test_snapshot_restore.py`, and the torn-tail recovery path |
| configuration | `test_policy.py` (accept/reject), plus the game day running with mutated policies |
| installation | CI: `pip install --no-index --find-links dist docket` into a fresh venv, then `docket --version` and `docket demo` |
| visual | `test_render_golden.py`: the receipt, the markdown report and the diff, byte-for-byte |
| API | the boundary's HTTP surface (`test_proxy.py`) and the SDK's event API (`test_cli_sdk_demo.py`) |
| database | **not applicable** — there is no database; the ledger is a JSONL file by design (ADR 0001) |

## Reliability and chaos

| phase | what it does | expectation |
| --- | --- | --- |
| `baseline` | a well-behaved agent, nothing broken | every call recorded, receipt verifies |
| `upstream-failure` | the provider fails some calls and serves others | failures are `model.error`, never `model.response`; denials are refused before forwarding. The audit's breaker check *skips* here (the fault rate never reaches the threshold) and says why — its condition is exercised by the `circuit-breaker` phase, and `tests/test_chaos.py` pins that a conditional check never reports a vacuous pass |
| `circuit-breaker` | the upstream fails repeatedly | the breaker opens, refuses by name, records the opening |
| `upstream-latency` | slow responses | latency is recorded, the run still finishes or stops legibly |
| `rate-limit` | a fast agent against `max_calls_per_second` | exactly the budget is admitted, each refusal is recorded, HTTP 429 |
| `ledger-pressure` | a ledger driven to its event ceiling | the run stops with exit `42` rather than dropping events |
| `rollback` | seeded files, a damaging agent, a cap breach | the files are byte-identical afterwards, the rollback is in the receipt and the ledger, the run's own evidence survives |
| `supervisor-killed` | SIGKILL mid-run | the ledger up to the kill is readable and classified, no receipt is claimed, recovery is stated |

`docket game-day` writes a post-mortem beside the evidence, including expected-vs-actual per
phase, and exits non-zero if a phase misses its own stated expectation. The last recorded full
run: **8/8 phases**, ~15 s, offline.

## Found by testing

Each of these was a real defect in a shipped feature, found by an activity in the table above,
with a named regression test in `test_fuzz_regressions.py` or the relevant file:

| found by | defect |
| --- | --- |
| fuzzing (6 targets, 13 findings) | canonical-form and verifier edge cases: float acceptance, digest casing, proof path handling, huge inputs |
| red teaming | a CONNECT tunnel that skipped egress checks; a secret reaching the ledger through a header; a receipt that verified after a field edit; a cap bypassed by a request shape |
| game day | evidence-directory self-recording; an absolute-path diff under a relative cwd; a rollback reported as applied when every path was refused |
| concurrency tests | a ledger written out of sequence order by parallel calls (the tool accusing its own run of tampering) |
| input testing | `--max-usd nan` silently disabling the cap; `--max-usd abc` printing a decimal exception's `args` |
| repeating an action | `docket restore` failing on the second run and blaming the operator for Docket's own edit |
| the conformance check | `spec/vectors` going stale after a policy change (the digest bound into every event) |
| re-reading the spec against the implementation | `NaN`/`Infinity` are read by Python and rejected by `JSON.parse`: a request body carrying one became `502 docket internal error` in the run's own evidence, a ledger line carrying one was reported as "cannot be canonicalised" instead of "not JSON", and a hand-written `Infinity` on the last line claimed the torn-tail crash-recovery path |

## Not covered, and why

- **A full mutation score.** `tools/fuzz.py` mutates *inputs*; there is no mutation run that
  mutates the *source* and measures how many mutants the suite kills. It is a real gap: the
  suite is property-heavy by design, which usually means a good score, but "usually" is not a
  measurement. A bounded, seeded mutation run over `canon.py` and `verify.py` is the next
  piece of this, and until it exists no number is claimed here.
- **Endurance and soak.** The longest run under test is seconds, not hours. Nothing in the
  design grows with wall-clock time — no caches, no unbounded buffers, the ledger is
  append-only and its ceiling is enforced — but that is an argument, not a measurement, and the
  argument rests on a suite that has never run for an hour.
- **Throughput.** No requests-per-second or tokens-per-second target exists, and none is
  claimed: Docket sits in the request path of a *bounded* agent, where the interesting number is
  cost, not speed.
- **Live providers.** CI never calls Anthropic, OpenAI or Google. The wire format is exercised
  against a scripted upstream; a provider-side change to a router, a header or a usage field
  would be caught by a user, not by CI. `--online` exists for that, and is deliberately not
  automated.
- **A local attacker racing Docket.** The confinement tests cover a hostile *document*. A
  process that can race the restore between its check and its write, on a tree the operator has
  already handed to an autonomous agent, is out of scope and named as such in the threat model.
- **Accessibility beyond the terminal.** There is no GUI to test. The `NO_COLOR` path, the
  monochrome golden files and the markdown report are what "nothing is only conveyed by colour"
  means here.
- **Windows.** The lock, the process-group kill and the file modes assume POSIX. `docket doctor`
  says so rather than pretending otherwise.
