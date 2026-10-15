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
$ make test          # 700 tests, ~105 s, no network
$ make check         # + ruff, + bandit, + the independent Node verifier
```

| file | tests | what it pins |
| --- | --- | --- |
| `test_canon.py` | 28 | canonical JSON, Merkle trees and proofs, the no-floats rule |
| `test_vectors.py` | 9 | the committed conformance vectors, including that regenerating them changes nothing |
| `test_ledger.py` | 12 | append/read, chain resumes, ceilings, `unwritten` bookkeeping |
| `test_ledger_faults.py` | 22 | a ledger on a failing filesystem: ENOSPC, EIO, a closed handle, an OS error while opening |
| `test_ledger_concurrency.py` | 4 | one ledger, eight writer threads: order, chain, agreement, file digest |
| `test_latency.py` | 21 | the percentile rule, hostile row values, the verifier's recomputation, the report's tail line |
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
| `test_invariants.py` | 14 | executed properties: one writer per ledger, 200 events from 8 threads, no deduplication of a replayed call, socket and thread release, a SIGKILLed agent, a clock that steps forwards or backwards |
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
| mutation | `tools/mutate.py`: seeded single-site mutation of `canon.py`, `verify.py`, `ledger.py` and `latency.py`, with every survivor classified — see *Mutation testing* |
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

## Mutation testing

`tools/mutate.py` changes **one** site per run and asks whether the suite notices. A site is a
comparison operator, a boolean operator, a `not`, a boolean or small-integer constant, a
`min`/`max` call, or any `return`, rewritten through `ast` and spliced back by character span. A
mutant that the suite still passes is *survived*; a mutant the suite catches is *killed*. Timeouts
are counted and reported separately, never as kills.

```console
$ python3 tools/mutate.py --file src/docket/verify.py --seed 42 --max-mutants 200 \
      --json /tmp/verify.json --markdown /tmp/verify.md
```

Properties of the harness itself, because a mutation score is only as good as its tool:

- **It falsifies itself first.** Splicing every site back verbatim must be a byte-for-byte
  no-op (`splice_only`), and the unmutated sandbox must pass the whole subset before any mutant
  runs. If either check fails, no score is printed.
- **It mutates a copy**, one mutation at a time, restoring the file between runs.
- **The subset is declared, not implied.** `TARGET_TESTS` names the test files that make claims
  about each module. A survivor from a subset that does not cover the module's branches is an
  artefact of the subset, not a gap: verify.py's first wide run reported 23 survivors, four of
  them in the signing branch, and after `test_signing_and_attach.py` was added to the subset and
  *no source was changed* it reported 2.
- **Duplicate keys are not identities.** Two mutants at one line with the same operator (two
  `False -> True` on a single line) are separate mutants: report and compare them as such.
- **A survivor in a branch nothing can reach is not a gap.** Two of this round's survivors were
  mutants of dead defensive code — a second `torn_tail is None` check under a guard that had
  already returned, and an `events is None` check the reader cannot produce. They were deleted,
  not tested: branch coverage by assertion is theatre. The same reasoning removed a `required`
  parameter from `verify.section()` (all three call sites passed it) and an existence fallback
  around `p.stat()` (the handle above it always creates the file).

### The runs

Seed, candidate count and subset are recorded per run; scores are not comparable across seeds
because the sample differs, and the candidate count changes with the source.

| module | candidates | run | killed | survived | timed out | score | wall clock | seed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `src/docket/verify.py` | 95 | 95 | 94 | 1 | 0 | **98.9%** | 66 s | 42 |
| `src/docket/canon.py` | 63 | 63 | 58 | 1 | 4 | **98.3%** ([^timeouts]) | 209 s | 42 |
| `src/docket/ledger.py` | 171 | 171 | 143 | 28 | 0 | **83.6%** | 276 s | 11 |
| `src/docket/latency.py` | 21 | 21 | 21 | 0 | 0 | **100%** | 12 s | 0 |

`latency.py` was written *after* the harness existed, and the harness earned its keep immediately:
its first run left one survivor — the `return problems` inside the "the receipt states a latency
summary but no usage row carries `latency_ms`" branch, replaced by `return None`. Nothing noticed,
because `verify.py` reads a falsy return as "no disagreement", so a receipt could state **any**
p99 as long as its rows carried no measurement: a false *pass* in the feature whose entire point is
that a stated measurement must be recomputable. The fix was a test for the branch, not a change to
the branch. The same module's first candidate list also contained an unkillable `min(max(rank, 1),
len(ordered))` clamp — unreachable given `percentile_ms`'s own guards — which was deleted rather
than explained, and the subset it needs is a reminder of the same lesson in the other direction:
run with only `test_receipt_verify.py` and the score reads **4.2%**, because that file does not
touch the module at all.

Started from 67.5% (canon + verify, 80-mutant sample, seed 3) and 50.0% (ledger, 70 of 153
candidates, seed 11). The ledger number moved because the *tests* moved: the first run wrote no
test for the lock file's contents, the byte accounting, the ceiling fields, `degraded()`, the
repair path's sidecar naming, or the chain verdict's own silence.

### Every survivor, classified

A survivor is either a gap or an equivalent mutant. Each one is listed here with which it is and
why; an equivalent mutant is not a defect and no test pretends otherwise.

| site | mutant | verdict |
| --- | --- | --- |
| `verify.py:178` | `isinstance(value, bool) or value is None` → `and` | **equivalent.** `whole(True)` fails as a bool on one path and as the unparsable string `"True"` on the other, and `whole(None)` likewise; both routes record the same named failure with the same text. |
| `canon.py:51` | `or` → `and` in the early return for bool/`None`/str | **equivalent.** The fallback at the end of `_normalise` returns those three types unchanged as well; the early return is a fast path, not a behaviour. |
| `canon.py:139`, `canon.py:160` | `len(level) > 1` → `>=` and `1` → `0` in the `while` loop of `merkle_root` and of `merkle_proof` | **non-terminating, all four.** Each turns a one-node level into a level that is promoted to itself, so the tree never finishes. They exceed a 45 s per-mutant timeout (the same sites exceeded 120 s in an earlier run): reported as timeouts, never as kills, because a hang is not the suite noticing a wrong answer. |
| `ledger.py:153,160,161,168` | `compare=False` → `True` on the clock, the two handles and the append lock | **equivalent.** Nothing compares two `Ledger` objects — the field option is declared so that a repr and a comparison cannot reach into a descriptor, and the *repr* half of the same declaration is killed by `test_the_repr_of_a_ledger_is_safe_to_put_in_a_log`. |
| `ledger.py:168` | `init=False` → `True` on the append lock | **equivalent.** A lock passed to the constructor changes nothing; nothing constructs a ledger that way. |
| `ledger.py:215` | `if size > 0` → `>=` | **equivalent.** For a zero-byte file the guarded block reads the file and extends the event list with nothing. |
| `ledger.py:277` | `getattr(self._handle, "closed", False)` → default `True` | **equivalent.** The attribute exists on every file object a ledger can hold; the default is reachable only for an object that is not a file. |
| `ledger.py:301` | `self.max_bytes or 0` → `or 1` | **equivalent (unreachable).** This expression is inside `LedgerFullError`, raised only when a ceiling is set, and `_room_for` never refuses when both ceilings are `None`. |
| `ledger.py:365` | `__exit__` returning `False` → `None` | **equivalent.** The `with` statement tests truthiness; both let an exception through. |
| `ledger.py:406` | the diagnostics skeleton's `trailing_newline: True` → `False` | **equivalent.** Every path that reads bytes overwrites it; it is readable only for a file that does not exist, where neither value is a claim about bytes. |
| `ledger.py:628-646` | six mutants: the `and`/`not` of the stale-lock test, `unlink(missing_ok=True)` in both the stale branch and the release path, and the two `return`s of that branch | **unreachable here.** Those branches are the `else` of `fcntl is not None`, so on any platform with `flock` they cannot run at all; the POSIX equivalents are killed. `missing_ok=False` is additionally covered by the surrounding `contextlib.suppress(OSError)`. |
| `ledger.py:664-672` | ten mutants in the Windows process-liveness probe (both `if not …` guards, both `return`s, the exit-code comparison, the `OpenProcess` arguments) | **unreachable here.** Exercised on Windows only, where the process table is asked directly — see *Windows* below. |
| `ledger.py:680` | `except (ValueError, OverflowError)` → the two mutants of its `return False` | **unreachable on this kernel.** `os.kill` reports a pid no kernel would hand out as `ESRCH`, which is an `OSError` and is handled (and killed) by the branch above; this guard is for the platforms that raise `OverflowError` instead. |

[^timeouts]: All four mutants in this family failed to terminate within the 45 s per-mutant
    timeout. The score is over the 59 mutants that produced a verdict (63 candidates minus the
    four timeouts); a timeout is reported beside the score, never folded into it.

### What the round changed

Writing the tests that kill a mutant is where the defects were:

| found by | defect |
| --- | --- |
| a test for a surviving `return` | `quarantine_torn_tail` reported `performed: true` having moved **zero bytes** when the interrupted write had landed its partial line *with* a newline: the split took everything after the last newline, which is empty, and the ledger was left exactly as damaged as it was found — an operator's repair that repairs nothing, and says it worked. It now splits by the reader's own measurement of the torn tail. |
| reading a survivor in a Windows branch | `_pid_alive` probed with `os.kill(pid, 0)`. On Windows Python's `os.kill` *is* `TerminateProcess`, so the liveness check for a stale lock would have **killed the process holding the lock** — the opposite of the check, with a blast radius that reaches another run. Windows now asks the process table (`OpenProcess`/`GetExitCodeProcess`) and answers "alive" when it cannot tell. |
| a test for a stated-but-unmeasurable latency | `check_summary`'s "no usage row carries `latency_ms`" branch returned a list nobody had ever asserted on; mutating it to `return None` changed no test, and the verifier reads a falsy return as agreement, so a fabricated summary verified. (Found by mutating the new module, not by reading it.) |
| a test for the diagnostics' shape | `read_with_diagnostics` promised "the same keys on every path" in a comment while the missing-file path returned nine of eleven: `healthy` and `chain_error` raised `KeyError` for a caller that had only ever seen a healthy ledger. |
| a test for the ceiling's error | `LedgerFullError`'s byte ceiling collapsed to `0` under a mutated `or`, and the reported usage shifted under a mutated index, and nothing read either field. |
| reading a ledger's repr | the repr carried a descriptor, a lock handle and a memory address into CI logs. |

### Not claimed

A mutation score is a statement about the mutants a tool generated, over one subset, at one
commit. It says nothing about the defects no mutant represents: a missing feature, a wrong
decision, an interface nobody needed. 83.6% on `ledger.py` with every survivor explained is not
"the ledger is 83.6% correct"; it is "of 171 single-site changes to this file that this harness
produced, 143 were caught by these tests, 28 are provably equivalent or unreachable here, and
none is unaccounted for".

## Not covered, and why

- **A full mutation score over the whole source tree.** `tools/mutate.py` runs over three
  modules — the two whose promises are cryptographic (`canon.py`, `verify.py`), the one that
  holds the evidence (`ledger.py`), and the measurement added on top of them (`latency.py`). The
  rest of `src/docket` is not mutated: a score for `proxy.py` or `cli.py` would be a much larger
  run, and a number nobody has run is not a number. The harness's per-module test subsets now name
  the files each module's promises are made in, including the invariant suite for the lifecycle
  modules, so a future run of `supervisor.py` or `scriptagent.py` starts from a subset that can
  actually fail. What *has* been run, and what every survivor turned out to be, is in *Mutation
  testing* below.
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
- **Windows.** There is no Windows in this environment or in CI, so nothing Windows-specific is
  claimed: the `O_EXCL` lock fallback, the `OpenProcess` liveness probe and the process-group kill
  are written, are the reason those code paths are marked `# pragma: no cover`, and have never
  been executed here. The lock fallback's liveness probe killed a live process until this round
  (see *Mutation testing*); a Windows contributor's first run is the one that will exercise it.
