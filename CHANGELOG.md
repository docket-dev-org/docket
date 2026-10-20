## [Unreleased]
- **The tamper demo test no longer leaves its workspace behind.** `test_the_tamper_demo_is_reproducible_by_the_command_it_prints`
  calls `run_demo(..., keep=True, ...)` so it can assert on the receipt and ledger, and never removed
  them, so every run left a directory under `/tmp`. It now reads the path the demo prints for a kept
  workspace and registers a finalizer. Measured: one directory per run before, zero after.

  **A correction to something this round got wrong.** The same leftover was first read as a leak in
  shipped code, in `run_demo` itself. It is not: `run_demo` removes the workspace unless `keep` is
  set, and prints where it kept one when it does. The leak was in the test that opts in and forgets
  to clean up. Checking the source before concluding would have found that; concluding from a
  directory left in `/tmp` did not.

- **The packaging tests no longer fill the disk they run on.** `tests/test_packaging.py` created
  four throwaway environments per run with `mkdtemp` and never removed them. Measured: **149
  directories** named `docket-uv-*`, `docket-pipx-*`, `docket-venv-*` and `docket-journey-*` under
  `/tmp`, **636 MB**, with a 1 GB tmpfs at **100%** — which turned a full gate run into
  `OSError: [Errno 28] No space left on device` at *setup*, inside `test_mutate.py`'s fixture, a long
  way from the tests that caused it. A suite that fills the disk it runs on reports the failure
  somewhere unrelated, and nothing connected the two because nothing counted the directories.

  All four sites now register a finalizer. Two guards hold it closed: one reads the source with `ast`
  and fails any test that creates a throwaway environment without removing it, the other creates one
  and checks it is gone. Five mutations are caught — each of the four removals dropped, and a removal
  that stops actually deleting. The behavioural guard earned its place during the mutation run: the
  mutant that replaced `rmtree` with `pass` left a real directory behind, and the next run failed on
  it.

  The structural guard's first version counted `mkdtemp` against `addfinalizer` across the whole file
  and reported 7 against 6 — the seventh being its own test's inline `rmtree`, which is a removal. A
  guard that fires on correct code gets switched off, so it reads with `ast` and asks per function.

Suite 1487 -> 1489.


## [0.2.6] - 2026-10-20

- **Two defects in the shipped mutation harnesses, found by running them rather than reading them.**

  `tools/mutation/mutate_release_version.py` applied its mutation and returned: it wrote the mutated
  `.github/workflows/release.yml` and left it there, so running the three harnesses in sequence left
  the repository's own release workflow in a mutated state. A shipped tool that damages the repository
  it ships with is not a tool. It now applies, runs the guard, restores, and refuses to report success
  unless the file it reads back is byte-identical to what it read.

  `tools/mutation/mutate_evidence.py` counted a stale anchor as an uncaught mutation. One anchor
  carried eight spaces of indentation where the source line carried four, after a refactor moved an
  `if` out of a nested block, so the mutation was never applied and the harness reported "1 of 15
  mutations were not caught" — which says something is wrong without saying what. A mutation that was
  never applied has not been caught and has not escaped; it has not been asked. The anchor is
  re-pointed, all 15 are caught again, and a stale anchor is now reported as `STALE`, named, and still
  fails the run, because an unexercised guard is not a guarded one.

  The three harnesses in `tools/mutation/` were tracked in the repository and therefore public, and
  nothing in the documentation named them or said how to run them. `docs/testing.md` now carries their
  commands, their mutation counts and scores, and both repairs above.

No change to the shipped code: `git diff 0.2.5..0.2.6 -- src/` is empty.

## [0.2.5] - 2026-10-20

### Fixed

- **The packaging tests survive PyPI's propagation window instead of guessing at it.** Measured
  on the v0.2.4 release gate: the guard found 0.2.4 on the index, un-skipped the clean-venv install,
  and CI's `pip` then resolved only up to 0.2.3, because the probe and the installer had hit
  different edge caches minutes apart. The gate failed on a propagation delay with a package and
  artifacts that were both correct.

  Three changes, one root cause. The guard reads the **simple index**, which is the cache `pip`,
  `pipx` and `uv` resolve against, rather than the JSON API, which is a different cache with a
  different lifetime — the defect the 0.2.1 gate needed fixed, recorded as applied and verified,
  and never actually committed. The guard **waits**, polling until the index serves the version,
  and skips only a version that never appears. And the install **retries** on a propagation-shaped
  error, recognising all three wordings for "the index has no such version", while a real breakage
  returns on the first attempt rather than being retried into a slower version of the same failure.

  `tests/test_propagation.py` (20 tests) exercises this rather than asserting it in prose, including
  two that ask the real PyPI simple index for a version that exists and for one that never did.
  Seven mutations caught: the guard reading the JSON API again, the guard deciding once, an empty
  phrase list, an install helper that never retries, one that retries real breakage too, one that
  discards the real failure output, and a call site that bypasses the helper.

  Two of those tests found defects in the tests themselves, which is the reason to write them:
  `time` was a function-local import so it could not be patched, and the call-site guard asserted
  that nothing calls `_run` at all, which is false — running the binary an install just produced is
  not an install and has nothing to retry. Both corrected.

- **The propagation wait is scoped rather than paid by every run.** As first written it was a single
  180 s constant evaluated at import, so a tree declaring a version nobody had uploaded spent
  3m0.6s discovering that. The release workflow now sets `DOCKET_PYPI_PROPAGATION_SECONDS=300`,
  because that is the one job that runs against a version uploaded minutes earlier; every other run
  uses a 15 s default.

- **The "no PyPI credential" guard matches credentials rather than the letters P-Y-P-I.** It asserted
  that no environment key contains "PYPI", which is a proxy for "no PyPI credential" and tripped on
  `DOCKET_PYPI_PROPAGATION_SECONDS`, a timeout bound holding no credential — and the way to make the
  suite green would have been to rename the variable so the guard stopped looking at it. It now
  matches what a credential looks like: a `TWINE_*` variable, anything carrying a password or token,
  or any value reading a secret other than the run-scoped `github.token`.

  Four guards hold both halves in place, and six mutations of them are caught: a `twine upload` step,
  a PyPI token read from a repository secret, the release token pointed at a long-lived secret, a
  literal `TWINE_PASSWORD`, the propagation bound dropped from the gate, and the bound shortened
  below a release-gate value.

Suite 1465 -> 1487.

No change to the shipped code: `git diff 0.2.4..0.2.5 -- src/` is empty.

## [0.2.4] - 2026-10-20

### Fixed

- **The release workflow recovers from a re-run and can be dispatched by hand.** The version
  came from `${GITHUB_REF_NAME#v}`, so a `workflow_dispatch` run on a branch resolved
  `main`, looked for `## [main]` in the changelog and failed whatever the maintainer did;
  the trigger the workflow declares could not succeed. The version is read out of
  `pyproject.toml` and a tag that disagrees with it fails the job. Publication ran
  `gh release create`, which exits non-zero when the release exists, so a re-run after a
  partial failure stopped there with deleting the release as the only way past it; it now
  uploads onto an existing release. Three guards, five mutations, all caught.

- **The README link check no longer reports an unreachable host as a broken link.** On the
  v0.2.3 gate, `img.shields.io` dropped a connection on one badge and the check reported
  `RemoteDisconnected` under *URLs in README.md that do not answer 200*, failing a tag
  whose package and artifacts were both correct. `RemoteDisconnected` is an `OSError`, and
  connection-level errors appended to the same list as status codes, so a host that never
  answered was reported as a link that answered wrongly. A 404 stays conclusive and still
  fails; a dropped connection is retried three times with backoff and then printed as not
  verified rather than asserted to be broken. Five tests against two real loopback servers;
  six mutations caught.

No change to the shipped code: `git diff 0.2.3..0.2.4 -- src/` is empty.

  `RemoteDisconnected` is an `OSError`, and the handler for connection-level errors appended to
  the same list as the handler for status codes, so a host that never answered was reported as a
  link that answered wrongly. A 404 is an answer and a retry returns the same status, so it is
  still conclusive and still fails. A dropped connection is retried three times with backoff; if
  the host still never answers, the result is printed as *not verified* rather than asserted to
  be broken, because the test did not observe these links to be broken.

  `tests/test_link_check.py` (5 tests) pins the distinction against two real loopback servers,
  one answering 404 and one accepting the connection and closing it without answering. Six
  mutations of the check are caught: status failures recorded in the unreachable list, the
  separate list removed, the attempt count reduced to one, the backoff removed, the unreachable
  result not reported, and a retry loop that never retries. Suite 1460 -> 1465.

- **The release workflow recovers from a re-run and can be dispatched by hand.** Two defects in
  `.github/workflows/release.yml`, both measured on the v0.2.3 run:

  1. The version came from `${GITHUB_REF_NAME#v}`, so a `workflow_dispatch` run on a branch
     resolved `main`, looked for `## [main]` in the changelog, and failed with
     *CHANGELOG.md has no section for main* whatever the maintainer did. The declared
     `workflow_dispatch` trigger could not succeed. The version is now read out of
     `pyproject.toml`, and a tag that disagrees with it fails the job instead of publishing a
     release whose name and contents disagree.
  2. Publication ran `gh release create`, which exits non-zero when the release already exists.
     The v0.2.3 tag gate passed, the artifacts were good, and the run stopped at publication
     because the release had been cut by hand first — with deleting the release as the only way
     past it. Publication now uploads onto an existing release.

  `tests/test_release_workflow.py` gains three guards for this, and all five mutations of them
  are caught (version from the ref name; disagreement reported but not fatal; no existence check;
  upload without `--clobber`; the create path alone). Suite 1457 -> 1460.

## [0.2.3] - 2026-10-20

### Fixed

- **The `uv` install path asks the index rather than its own cache.** `uv` keeps an index
  cache with a different lifetime from PyPI's, and the packaging guard reads PyPI's. Measured
  on 0.2.2: the simple index served the file immediately while `uv tool install
  docket-ai==0.2.2` resolved *no version of docket-ai==0.2.2* until `--refresh` was passed.
  The test now passes it. This fix is not provable by mutation here: removing both flags
  leaves the test green, because the cache has since populated and the failure window has
  closed. The evidence is the direct measurement taken in the window.

No change to the shipped code: `git diff 0.2.2..0.2.3 -- src/` is empty.

## [0.2.2] - 2026-10-20

### Fixed

- **The packaging tests ask the index the installer asks.** They decided whether to skip by
  querying the JSON API, while `pip`, `pipx` and `uv` resolve against the simple index. Those
  are different caches with different lifetimes, and the guard contradicted itself on 0.2.1:
  the JSON API answered 200 for a freshly uploaded version while `pipx install` still resolved
  *No matching distribution found*, so the guard un-skipped the test and the test failed on a
  propagation delay that had nothing to do with the package. Both now read the simple index.
  `PUBLISHING.md` records the ordering this implies: upload to PyPI before pushing the tag, so
  the gate measures a published release rather than skipping three tests.

No change to the shipped code: `git diff 0.2.1..0.2.2 -- src/` is empty.

## [0.2.1] - 2026-10-20

### Fixed

- **The release gate installs the package under test.** `.github/workflows/release.yml` ran
  the whole suite against a tree whose package was never installed, so `tests/test_action.py`
  and `tests/test_packaging.py` failed with `docket: command not found` — exit 127, which is
  not a verdict about the tag and reads like one. 0.2.0 was published with a release check
  that had never passed. This is the same omission the CI `tests` job had, fixed there first
  and missed here.

- **The packaging tests measure the version the tree declares.** `tests/test_packaging.py`
  pinned the version as a literal, and its pipx test resolved the binary with
  `shutil.which("docket")` after a successful install — which finds whichever `docket` is
  already on `PATH`, not the one pipx just made. It passed on every clean machine and failed
  on the release candidate. The install is now directed into a throwaway `PIPX_HOME` and
  `PIPX_BIN_DIR`, and the binary is taken from that directory.

### Added

- **`tests/test_release_workflow.py`**, which holds the release gate to the same standard as
  the push gate: the package is installed and installed before the suite runs, the release
  job's test dependencies are the CI `tests` job's, the gate runs the `tests` directory
  rather than a subset, it prints its counts, a tag with no changelog section fails the job,
  the workflow uploads nothing to PyPI and carries no PyPI credential, and the GitHub
  release uses the run-scoped token. Eight mutations, all caught.

No change to the shipped code: `git diff 0.2.0..0.2.1 -- src/` is empty, which is why 0.2.1
exists — so that the newest tag, the newest release and a green gate name the same commit.

## [0.2.0] - 2026-10-20

- **Retention of the provider responses the boundary saw.** Every `model.response` event now
  carries `response_sha256` over the exact bytes returned to the agent, and
  `[evidence] record_responses` additionally keeps a bounded copy of the body in a `responses`
  sub-directory of the run directory, named by its own digest. `docket verify` re-hashes every
  retained body under the new `retained-responses` check, so one edited or deleted after the run
  fails. The reason this exists is a provider Docket cannot read: its usage block is in a fourth
  shape, the token counts are estimated from the response text, and that text used to be
  discarded, so the estimate could never be checked. A truncated body carries both digests and
  says which is which; the verifier checks the bytes that were written and never claims to have
  checked bytes it was not given. `docket report --redact` drops the retention fields rather
  than leaving a pointer to a body the copy does not carry, and records how many under
  `retained_responses_not_carried`. Retention is off by default: a response body can contain
  the prompt, a tool result, or a credential, so the digest is the default and the body is the
  opt-in. See `docs/evidence-retention.md`.

- **Published to PyPI as `docket-ai` 0.1.0.** `pip install docket-ai` installs the same
  package, and the import, the console script and the command are all still `docket`. The
  distribution name differs because PyPI administratively prohibits the bare name `docket`:
  the JSON API answers 404 for it, and `twine upload` still refuses it with *"The name
  'docket' isn't allowed"*. PyPI keeps that list to prevent dependency-confusion attacks, and
  the only way off it is a PEP 541 request reviewed by the PyPI administrators. `PUBLISHING.md`
  records the reason, the one place the name is written, and the request process.

- **`PUBLISHING.md`, the maintainer runbook for shipping a release.** It records what the
  release gate does, why the PyPI upload stays a manual `twine upload` rather than a step in
  the workflow (a PyPI token can publish under the project's name forever and cannot be
  scoped to a branch, so a repository secret hands it to everyone who ever gains admin here,
  while `github.token` is scoped to one run and expires with it), the prohibited-name
  evidence, and how to cut and re-cut a tag.

### Changed

- **The README leads with the install path that works.** It told a reader that `docket` was
  not on PyPI, that the name had been unregistered on 2026-10-17, and that
  `pip install docket` would start working after a future 0.1.0 publication. The release
  exists, so the apology was the only thing left. The install section now opens with
  `python3 -m pip install docket-ai`, and `pipx install docket-ai` replaces the wheel URL.
  The clone path stays, because it is what a contributor wants rather than what a user wants.

- **The version note states what is guaranteed instead of hedging.** It said the CLI "may
  change before 1.0", which read as an unfinished project. It now says the format is frozen by
  the specification and its 44 vectors across three implementations, and that exit codes,
  flags and the JSON shape of every command are part of the interface and stable across 0.1.x.

- **The exit-code table gains `1`, and documents exit-code propagation.** `docket run` hands
  back the exit code of the command it wrapped, so `docket run --policy docket.toml --
  ./build.sh` is safe in a pipeline. That was true and undocumented.

- **The quickstart verifies `.docket/latest.receipt.json` instead of a `DKT-*.receipt.json`
  glob.** The glob worked on the first run and exited `2` on the second, because `verify` takes
  one receipt and argparse rejects two. The path every run rewrites has no such edge.

- **`pyproject.toml` uses the SPDX license expression and `license-files`**, so PyPI renders
  the licence rather than a free-text string.

### Fixed

- **The doc-link guard now asserts the opposite of what it used to.** It used to fail any
  document that instructed a reader to install an unpublished package, and its own docstring
  said to relax it by removing the instruction once published. It now fails any document that
  still carries the apology, and reads the distribution name from `pyproject.toml` rather than
  repeating it, so a rename cannot leave the guard and the guide disagreeing.

- **`docket verify --impl node`: a second opinion from an installation.** The claim that the
  format is not this tool needed the other tool to be reachable without a clone, so the wheel now
  carries `verify-js/verify.mjs` and the conformance corpus under `docket/_vendor/`, and
  `docket verify --impl node` runs that program against the same receipt and the same ledger and
  prints its own verdict and its own exit codes (`0`/`1`/`2`). A pipeline that already gates on
  `docket verify` needs no change when it switches. `docket doctor` now reports whether this
  installation has both the verifier and a `node` on `PATH`, and two tests read the packaging
  configuration rather than a built artifact, so a dropped `force-include` line fails here
  instead of in a reader's terminal.

- **`docket verify` finds the ledger beside the receipt instead of checking less.** It used to
  need `--ledger` or it skipped the chain, the roots, the file digest and the re-binding of the
  usage rows. It now looks in the `.docket/runs/` directory beside the receipt it was handed and
  selects by the `ledger.file_sha256` the receipt carries, so the file used is provably the one
  the receipt was built from — including for a receipt that arrived in an archive with its
  ledger alongside. The line naming the file is printed, `--ledger` still overrides, and a
  receipt with no ledger anywhere still degrades to `SKIP` rather than to a red result.

- **A third implementation, written from the specification alone.** `verify-go/` is a
  dependency-free Go verifier built from `spec/` and the vectors without reading the Python or
  Node one, with a `CLEANROOM.md` that declares the constraint before any code existed. It agrees
  with the other two on the whole corpus — `132 correct, 0 wrong, 0 not judged` over 44 vectors —
  and it produced the artifact the claim needed: **twelve questions the specification does not
  answer**, five of which are worth amending (rounding of the sixth decimal, price-table matching,
  the shape of `limits_disclosure`, blank lines in a ledger, and the marker for a reached cache
  ceiling). `make verify-go` runs vet, gofmt and 16 table-driven test functions; CI has its own
  job; `make conformance` includes it whenever the binary exists.

- **Three more members of a receipt were stated and never checked, and now are.**
  `totals.by_model[]`, the usage-source counts and `totals.cost_basis` are derived from
  `appendix.usage`, so they can be recomputed — but §8 only ever named the total, the token
  totals and the per-row costs. A sweep that rewrote each of them left `ok: true` every time: a
  breakdown could name a model nobody called (with the total still reconciling), or present four
  estimated calls as reported. `by-model-totals` and `usage-source-counts` are new checks;
  `cost-basis` is a contradiction check that compares only the class word and reports `quota
  mode` as `SKIP`, because the meter's mode is stated nowhere else in the receipt and believing
  it is not checking it. Four new corpus vectors (`39`–`42`), eleven new tests in the Python
  suite, eleven in the Node one, and ADR 0007 records why the other designs were rejected.

- **`ledger_reject`: the corpus can now say "this ledger must be refused".** Every vector was a
  *receipt* that must fail; a malformed ledger — the file an interrupted append or a hand edit
  actually damages — had no way to be expressed, so the two implementations' refusal to repair one
  was asserted by their tests and by nothing a stranger could run. Two vectors (`37`, `38`: a
  duplicated key inside an event, a half-written last line) and a fourth expectation kind close it.
  The rule is the same as for receipts: a reason must be stated, and the vector may only require a
  token that appears in the ledger.

- **The corpus grew to 36 vectors and found four more defects — including one in the same file twice.**
  `spec/vectors/invalid/` now covers the parsing family (a UTF-8 BOM, trailing bytes, invalid UTF-8 —
  all refused before any check runs) and, for the first time, **documents that must be accepted**:
  `4e0` where an integer is expected, `-0`, and a decomposed Unicode form. Those three exist because a
  corpus that only says what to refuse lets two implementations disagree about what to *read*, and the
  first one settled a real disagreement: spec §3.2 says an integral float MUST be normalised, Python
  refused every float token and Node accepted every one. The spec's sentence decided it; both now
  normalise an integral float and refuse a fractional one. Probing the totals member by member also
  found that `totals.calls`, `totals.input_tokens` and `totals.output_tokens` were never compared with
  the appendix in either implementation — a receipt could say `"calls": 0` beside four usage rows and
  verify — and that Node's `readFileSync(path, "utf8")` silently replaced invalid bytes with U+FFFD, so
  it verified a *repaired* copy of a corrupted document. All four are fixed, with vectors and tests.
- **`reason_any_of`, because a corpus may not demand one implementation's prose.** The first version
  of the BOM vector required the word `BOM`; Node said `Unexpected token '\ufeff'`, refused correctly,
  and was scored `wrong-reason`. An expectation now names tokens that must appear **in the document**
  (`NaN`, `Infinity`, a member name), or nothing at all when the damage has no nameable token. Writing
  a corpus before the third implementation exists is an exercise in not encoding the first two's
  vocabulary.
- **`docs/adr/0006-the-shape-of-the-conformance-corpus.md`** records the five design decisions with
  the alternative rejected in each case: the document-plus-expectation layout, the `parse` class,
  deriving the capability table from output, declaring gaps instead of manufacturing skips, and the
  limits of what a corpus may demand.

- **A negative conformance corpus, and the six verifier defects it found.** The vectors could only
  show that a conforming implementation accepts what it should; nothing tested that it refuses what it
  must, which is what a verifier is for. `spec/vectors/invalid/` now holds 27 complete documents —
  a doctored ledger, a totals figure that does not sum, a Merkle path with one hex digit zeroed, a
  `NaN`, an `Infinity`, a duplicated `docket_number`, a top-level array, an undisclosed fallback
  price — each with an `expected.json` naming the check that has to catch it and the rule it enforces.
  `spec/vectors/invalid/build.py` derives each vector from the valid ones, mutates it in exactly one
  stated way, then **runs the reference verifier and refuses to write the corpus** unless the first
  failure is the check the vector claims — so a vector cannot quietly document a defect as a feature.
  Building it, and running it against the second implementation, found six real defects: the Python
  verifier accepted a `usage_source` outside the vocabulary spec §6.1 defines (the row's arithmetic
  still recomputed, so every numeric check passed and the one word a consumer branches on was never
  read); the Node verifier accepted a document that named `ledger` twice, silently keeping the last
  value, where RFC 8259 §4 makes the meaning undefined; it also accepted the same out-of-vocabulary
  `usage_source`; it folded "is this row the event the ledger holds at its seq" into the inclusion-proof
  check, so a shuffled row reported a broken proof rather than a re-binding failure; the Python verifier
  emitted `every-usage-row-recomputed` **only** for receipts with no usage rows, the one case where the
  question cannot arise; and its `assumed-prices` row did not exist at all while the Node one reported
  `pass` for every assumption it noticed. All six are fixed, with a test each.
- **`spec/registry.json` and `tools/conformance.py`: the check vocabulary, in a form a machine reads.**
  The registry names every id, what it asserts, and which ids each shipped verifier emits. The
  capability table is **derived, not written**: `--capabilities` collects the ids each implementation
  actually printed across the whole corpus and fails when the registry disagrees — which is how the
  hand-written version was caught saying the Node verifier emitted twelve ids when it emitted eighteen.
  Ids a verifier does not emit are recorded as declared gaps (`node_conditional`), with the reason
  stated, rather than as `skip` rows: a skip claims "this verifier looked and could not judge", which
  would be false. `tools/conformance.py` runs any verifier (`--cmd "my-verify {file} --json{ledger_flag}"`)
  over the corpus and keeps three outcomes apart — `wrong-ok` (a document that must be refused was
  accepted), `not-judged` (a declared gap) and `wrong-check` (refused, but by a different rule than the
  vector names). `make conformance` runs both shipped verifiers and the capability comparison; it is
  part of `make check`. Both implementations now report **58 correct, 0 wrong, 0 not-judged** over
  2 positive and 27 negative vectors.
- **A star-count comparison that was quoting two figures traceable to nothing, and no longer does.**
  An audit of this project's own claims on 2026-10-17 found two landscape numbers with no source: one
  had been read against `dify/dify`, a slug that answers 404 (the project is `langgenius/dify`), and
  one had come from a search listing nobody saved. Both were re-swept by canonical slug against the
  GitHub REST API, unauthenticated, and the query that produced them is recorded with its timestamp so
  the snapshot can be re-taken. What was learned and kept: every figure quoted onward carries the slug
  asked for, the API path called and the URL that answers it, plus three
  `total_count` search rows. `tests/test_strategy_numbers.py` reads the page and the sweep together
  and refuses a page whose star counts, ages, rates, thresholds or inline figures stopped matching
  the evidence beside it; it cannot re-ask GitHub (that is the sweep's job, and re-running it means
  re-basing the page), but it can refuse the drift that actually happened.
- **`tools/a11y_check.py`: the built documentation site is checked for the accessibility defects a
  program can decide.** The site is the only HTML this project publishes; `mkdocs --strict` proved it
  built, and nothing looked at whether a reader using a screen reader could use it. Ten rules —
  language, title, exactly one `h1` and no skipped heading level, a single `main` landmark, `alt` on
  every image, an accessible name on every link and every rendered form control, a header cell or
  caption on every table, no duplicate `id` — with the accessible name computed from `aria-label`,
  `aria-labelledby` resolved through the page's own ids, element text, a nested `<img alt>`, a nested
  `<svg><title>`, a `<label for=…>` elsewhere on the page, or a `<label>` that wraps the control.
  `tests/test_a11y_check.py` makes every rule prove it can fail and every one of those naming paths
  prove it does not cry wolf, and the check runs in the release record beside the docs build.
- **One exemption, and it expires.** material renders its drawer and search toggles as
  `<input class="md-toggle" type="checkbox">`, and the theme's stylesheet says `.md-toggle{
  display:none}`: a control that is not rendered is not announced, so no name is due. The checker
  reads the CSS of the site it is inspecting rather than trusting a class name, prints which class
  exempted how many controls, and stops exempting the moment that rule is gone — an exemption that
  cannot expire is how a checker rots into a rubber stamp.

- **Two more guards, both green on the tree they were written for.** `docs/cli.md` is 397 lines of
  flags and none of them had ever been checked against the parser: the new test reads the level-2
  sections whose heading is a `docket <command>` line, requires the command to exist, and requires
  every flag filed under it to be an option of it — 72 flags, all real, and the scan fails if it reads
  fewer than 60, so a heading rename cannot turn it green by finding nothing. The second refuses an
  absolute path from the machine that wrote the docs — the authoring sandbox's home directory, or a
  `/tmp` toolchain path — in any prose document, with the raw measurement reports
  exempt because their own README says their `command` and `ledger_path` fields are the paths the
  harness ran with, unedited.

- **Two more guards in the file whose subject is documents matching the repository.** One fails on a
  row of a *graded* table that carries no grade — `docs/evidence.md`'s claim ledger and the incident
  corpus's incident table, found by their own header rows — because a claim that loses its grade
  quietly becomes the same weight as a sourced one, which is the failure the grades exist to prevent.
  The other reads every `make` target named in `.github/workflows/*.yml` and in the Makefile's `help`
  text and fails on one that does not exist: a job calling a target nobody defined is a red build for
  whoever enables it, and a help line is a documented command. Both are the shape of the correction
  that preceded them: a number or a name that was true when written and had no guard.

- **A test that refuses a rank the source does not publish.** Three shipped documents said OWASP's
  agentic top ten puts "denial of wallet" at risk #2 — the list's second entry is *Tool Misuse and
  Exploitation*, and cost is not one of its ten classes at all. A rank is the most precise kind of
  citation and the cheapest to invent, so
  `tests/test_doc_links.py::test_no_document_asserts_a_rank_inside_a_source_that_does_not_publish_one`
  reads every document that discusses such a list, fails on a position claim against it, and fails if
  the discussion disappears instead of being corrected — because deleting the paragraph would
  otherwise be a way to pass. Quoting the old wording in order to correct it stays allowed, which is
  how the correction is recorded in `docs/incident-corpus.md`.

- **A guard against the verbosity trap, in the file whose subject is the documents matching what the
  repository does.** `tests/test_doc_links.py` reads `pyproject.toml`'s `addopts`, asserts exactly one
  `-q` there, and then checks every pytest command the repository ships — the `make test` target and
  the four CI jobs — for a second `-q` (which is `-qq`, where pytest prints no counts line) and for a
  missing `-ra` (which is what makes a skip say why it skipped). It is the fifth place the same defect
  turned up in one day, and the first one that cannot come back.

- **Every mutation score in `docs/testing.md` is re-measured in one round, in one pass, with the
  control run recorded in every report** (`docs/testing.md`, *The release round*). The nine modules
  were swept back to back on one commit with one harness, each report carrying what its unmutated
  sandbox *did* before any mutant ran — `189 passed`, `121 passed`, `17 passed`, in pytest's own
  words — and the round reproduced every score, every survivor and every timeout the earlier rounds
  had reported, mutant by mutant as `(line, operator, from, to)`. That mattered because those numbers
  had been measured across three days, two environments and a test suite that grew underneath them:
  they now sit in one table that names the interpreter and the subset behind each of them. **The
  control column earned its place on its first round**: the Helm module scored 45 of 47 with a control
  run of `16 passed, 1 skipped`, and the skip was the test that runs the *real* Helm client — the
  harness gives its test processes a fixed `PATH` so that a score cannot depend on the operator's
  shell, and the client was outside it. With the client reachable the control reads `17 passed,
  0 skipped` and the score is unchanged, which is a claim a reader can now check in the report
  instead of taking from the author.

- **Two tests that hold the documentation to what a clone can carry** (`tests/test_doc_links.py`,
  15 tests). One reads `.gitignore` — implementing the forms this repository uses and *refusing*
  `**` rather than guessing at its meaning — matches every rule the way git does (anchored or by name
  at any depth, last match wins, `!` re-includes), and fails on any file the documentation names that
  a clone would not have. The other feeds the reader the rule that shipped and the rule that replaced
  it: a checker that cannot fail is decoration, and this one is asked to fail.

- **The dependency matrix in `docs/testing.md` is re-measured, and a claim this page made about
  pytest is corrected.** The table had been three rounds stale (it still carried 840 / 845 / 868 /
  878), and the sentence above it said the counts come from "a run with `-ra`, never a `-q` run's last
  line" — as if `-q` were the problem. What matters is how many times the flag is passed:
  `pyproject.toml`'s `addopts` already carry one, so a command line that adds another asks for `-qq`,
  and at that level pytest 9 prints no summary line at all — a script that greps for one records zero
  passes for a suite that passed a thousand. A single `-q` prints it. The page now says which round is
  tabulated, what the earlier rounds measured, and where the two that are not tabulated are filed.

- **A support page that says what support is not** (`SUPPORT.md`, in the documentation's nav and
  linked from the README's Contributing section). The repository had a security policy, a contribution
  guide and a sponsorship pack, and nothing that answered "something is wrong — where do I go": the
  issue templates existed but no page named them, no page said what a report has to contain, and no
  page said the thing most projects leave implicit, that there is no service-level agreement. It routes
  bugs, feature requests, questions, security reports and sponsorship questions to the right place,
  tells a reporter to run `docket doctor --json` and `docket selftest` *before* opening an issue (both
  print what they observed rather than a verdict), lists what this project cannot help with — a
  provider's billing, a Kubernetes cluster nobody here has, a date nobody promised — and states in the
  same words as `SPONSORS.md` that **a sponsorship does not buy support**.

- **The five examples are executed by the suite instead of being described by it**
  (`tests/test_examples.py`, 9 tests). `examples/` was advertised in the README, in these docs and in
  the adoption plan as five runnable examples, and nothing ran them: a renamed flag, a moved module or
  a policy key the loader stopped accepting would have left five documents that lie and a suite that
  stayed green. The quickstart and the SDK example now run as programs — offline, in a temporary
  directory — and the receipt each one prints is fed to the verification command each one prints; the
  quickstart's own instruction ("edit one byte of the ledger and run it again") is executed, with one
  digit of one token count changed and the verification required to *fail*; both policy files go
  through a real supervised run; and every README is checked for links, for the repository paths its
  commands name and for the Python files it ships. Writing it found the quickstart's own capture
  stale in two ways — it said 17 events where the tree produces 18, and it omitted the
  `cache-savings` verdict line entirely — so the block is now a real capture of the current tree, and
  the note beside it says which parts move between machines.
- **Every survivor of the harness's own sweep is closed, and three of them were defects in the
  *tests'* reach rather than in the code** (`tests/test_mutate.py`, 33 tests; `tools/mutate.py`).
  The harness was re-run over itself, its twelve survivors were read one by one, and each is now
  killed by a test that says what the mutant would have done: raising the span guard's lower bound
  from byte zero to byte one (a site at the first byte of a file was silently *dropped* — and only a
  file that begins with a mutable node can tell the two bounds apart, so no existing fixture could);
  flipping `parents=True` or `exist_ok=True` on the two report paths (the CLI test wrote into a
  two-level directory once but never into the directory it had already made); treating the four
  `rmtree(..., ignore_errors=True)` calls as strict (the sweep's cleanup runs in `finally:`, where a
  failure would replace the report — or the refusal — with a `PermissionError` from the temporary
  directory, so a fixture tree now contains a directory with no write bit and four tests assert that
  each of the four exits still reaches the reader); and the five guards inside `_is_docstring`, which
  the sweep cannot reach at all because its only caller has already established what they check — a
  direct question is now asked, with `is True`/`is False` rather than truthiness, because `and` turned
  into `or` there says "yes" to a bare number and filters a whole file's worth of sites out.
- **The ledger's liveness probe has a test that observes signal *delivery*, not disposition — because
  the sweep's own environment hides the difference** (`tests/test_ledger_contracts.py`).
  `os.kill(pid, 0)` mutated to `os.kill(pid, 1)` answers the same question and sends `SIGHUP` while
  asking it: a liveness check with a blast radius, the same defect class as the Windows probe that
  would have terminated the process holding the lock. The existing test could not see it, and the
  reason is worth keeping: every sweep runs under `nohup`, which ignores `SIGHUP` and passes that
  disposition to its children, so the signal is a no-op in the environment the score is measured in.
  The new test has the child install its own handler and create a marker file *inside* it, so a
  delivered `SIGHUP` is observable where it would otherwise be swallowed. The same file gained a test
  for `chain_error`'s broken-link branch, the one of its four that a file-based reader was covering
  instead.
- **A mutation score was reporting 51 gaps in the verifier that were 51 missing test files**
  (`tools/mutate.py`, `DEFAULT_TESTS`). The byte-offset fix took `verify.py`'s candidate count from 95
  to 177; the round that followed scored 126 of them and listed the rest as survivors. Reading them
  was the point: nearly every one sits in the block that recomputes a *stated* cache saving
  (`cache-savings`, `cache-hit-origins`, `cache-hit-proofs`), and that block is exercised from
  `tests/test_cache.py` — a file the verifier's subset did not name. The subset was part of the
  measurement, the score was reading as the tests' fault, and the fix is one line in the mapping plus
  the explanation beside it. The round after it is recorded next, because the two are the same lesson
  read twice.
- **The harness had no tests, and the first one found a bug that had been hiding a sixth of
  `verify.py`'s mutants** (`tests/test_mutate.py`, 16 tests; `tools/mutate.py`). The tool
  that produces every mutation score in this repository had no tests: the numbers in
  `docs/testing.md` rested on a tool nothing checked, which is the same shape of gap as a conformance
  vector nothing ran. The new file asserts the properties the harness claims — the splice is a
  byte-for-byte no-op, every mutant changes exactly one line and still compiles, each operator kind is
  found and labelled the way a reader expects, the sample is a function of the seed, the six verdicts
  `_run_tests` can return are distinguished by real pytest runs, timeouts stay out of the score, and
  the sweep refuses to score anything if the unmutated copy is already red. The em-dash fixture in it
  produced **no mutant at all**, and the reason was a real defect: `ast` reports columns in UTF-8
  *bytes* while the splice indexes *characters*, so every site on a line with a non-ASCII character
  before it was spliced into the middle of a character, failed to compile, and was dropped by a silent
  `continue`. Measured on the corpus: `verify.py` offered **95 candidates instead of 177**,
  `ledger.py` 171 instead of 172, and the other seven mutated files were unaffected because their
  sites sit on ASCII lines. `splice_only` could not have caught it — splicing the original text back
  is a no-op at any offset — and nothing counted drops, so the effect was invisible from inside the
  tool. Both offsets now go through `_char_offset`, the regression is pinned by a fixture line with an
  em dash in it, and every score quoted in the documentation was re-measured afterwards.
- **`tools/mutate.py --only-line N`: re-check one survivor with the harness's own machinery, in
  seconds.** Reading a survivor list is only useful if the next step is cheap: reproducing a verdict
  by hand is how a reader measures a stale `__pycache__`, or measures a site the harness never
  mutated. `--only-line` runs *every* mutant on the named line (repeatable, sampling nothing) in the
  harness's own sandbox with its own interpreter and environment, records the selection in the JSON
  and markdown reports so a filtered run is never read as a whole-file score, and refuses a line that
  holds no mutant rather than reporting `0 candidates`. The ten resilience sites closed in this round
  were verified this way, and the same flag re-checks the three cache sites.
- **A site that produces no mutant is now counted rather than dropped in silence.** The four ways a
  site can yield nothing — the span does not lie inside the file, the operator cannot be applied, the
  replacement does not compile, the replacement is the original text — each produce a record that the
  sweep prints and carries into the report as `dropped`, and the markdown header states the count. Two
  of the four branches are driven by tests rather than waiting for the corpus to produce them. This is
  the second half of the em-dash fix: the bug was not that a site was dropped, it was that a dropped
  site left no trace anywhere a reader could see it.
- **Two more documentation guards, for the two ways a number can go stale without failing a
  build.** `tests/test_doc_links.py` now reconciles (1) every count written *beside a test filename*
  anywhere in the living documents — `` `tests/test_x.py` (12) ``, `(12 tests)`, and "12 tests in
  `` `tests/x.py` ``" — against `pytest --collect-only`, and (2) the four sentences that quote the size
  of the whole suite (README, CONTRIBUTING, `docs/testing.md`, `docs/sre-coverage.md`). Both had
  already rotted: `docs/resilience.md` and `docs/sre-coverage.md` said `test_resilience.py` had 49
  tests (it had 54), `docs/sre-coverage.md` said `test_helm_fidelity.py` had 5 (17), `docs/caching.md`
  said `test_cache.py` had 41 (55), `docs/production-coverage.md` said `test_chaos.py` had 32 (33) —
  each written when its number was true. The suite total had no reader at all. `CHANGELOG.md` and
  `docs/measurements/**` are exempt by name, and the reason is in the guard: they are records of
  *then*, and re-basing them would falsify evidence rather than fix a claim.
- **Twenty-five tests closing every survivor of the 2026-10-15 mutation rounds** — three in
  `tests/test_cache.py` and six in `tests/test_resilience.py` for the survivors that were gaps, plus
  the harness's own sixteen above. The cache's three were the group identity in `_groups` (`key`,
  `model`, `avoided_cost_usd`): with the operator mutated, two hits that replayed different requests
  are merged into one row whose stated amount is the *first* hit's — so `saved_usd` reports $0.02 for
  $0.03 of saving and the row count no longer matches the ledger. The resilience six close the
  counter defaults, the `total_successes += 1` increment, the cooldown boundary (`elapsed < cooldown`
  versus `<=`), the sixty-second window boundary, the injector's default seed, the
  `getattr(self, name, 0)` fallback in `HealthState.counter`, and the free-space boundary in
  `readiness` — five of the six are exact boundaries the previous tests stepped past by one tick,
  which is the difference between testing a boundary and testing a rounding. The three
  `probes_in_flight` mutants that remain are classified as equivalent rather than tested, with the
  reason recorded in `docs/testing.md`: `allow` resets the field one statement before its only reader
  on every path into `half-open`, so no test can observe the difference, and the reset itself is
  killed.
- **A standards page: the norm or paper behind each mechanism, and where this one departs**
  ([`docs/standards.md`](docs/standards.md)). Every mechanism a reader can name — the hash chain, the
  canonical form, inclusion proofs, `429`/`Retry-After`, token buckets, the breaker, backoff jitter,
  cache storing rules, percentiles, tail latency, CAP, compensation, locking, clock ordering, chaos
  principles, probes, versioning — now carries its source, the strength of that source, and one line
  saying whether Docket follows it exactly, implements a smaller subset, or deliberately departs. The
  departures are the useful part and are collected in one list: an HTTP-date `Retry-After` is ignored
  because comparing a remote clock to a local one turns a wait into a hot loop; the cache implements a
  much smaller subset of RFC 9111 than the RFC allows; floats are refused where RFC 8785 would
  canonicalise them; nothing is hedged, so tail latency is measured rather than mitigated; no fencing
  token is issued because there is no distributed resource to fence; and no chaos experiment ever runs
  against production traffic. It also states plainly how each source was read — full text, section,
  abstract or practitioner canon — because "cited" and "read" are different claims.
- **A resilience-practice coverage map** (`docs/sre-coverage.md`), the third of the three maps
  beside the production-coverage concepts and the testing taxonomy: twenty-two SRE practices — stress
  testing, fuzzing, chaos engineering, chaos monkey, game days, red teaming, dynamic analysis,
  vulnerability scanning, synthetic monitoring, proactive monitoring, observability, FMEA, circuit
  breaking, rate limiting, automated rollback, disaster recovery, software rejuvenation, degradation
  testing, blast-radius mitigation, IaC testing, post-mortems, infrastructure hardening — each with
  what this repository actually does, what the evidence is, and where it stops. It was written because
  two of the practices were being *asserted in prose* rather than done (there was writing about failure
  and no failure analysis; "post-mortem" named documents that were not post-mortems), and a coverage
  document is the easiest place for that to happen again: a test enumerates the twenty-two rows, so a
  practice cannot lose its row or acquire a status outside the legend without failing the build.
- **A failure-modes-and-effects analysis, in the document that already described the failure
  surface** ([`docs/resilience.md`](docs/resilience.md), "Failure modes, ranked by what they would
  cost"). Sixteen ways a supervised run fails, each rated severity / occurrence / detection and
  ordered by risk, each naming the artefact that catches it — the money loop stopped at the boundary,
  a stale price table, a receipt that verifies but is not true, a torn tail, a leaked secret, a
  rollback that misses a file, two writers on one ledger. The page states plainly that the ratings are
  judgements and why; the *detector* column is evidence, and a test asserts that every path it names
  exists, that each risk score is the product of its own three ratings, and that the rows are still
  ordered highest-risk-first. The column is the point: a failure mode with a detector named in prose is
  a mechanism someone built, and a failure mode without one is now visible as exactly that.
- **`docket selftest` and `docket game-day` run in CI on every push, not only when someone remembers
  them.** The self-test's nineteen probes and the game day's nine phases are the two things that
  exercise the product end to end offline (about twenty-six seconds together), and a self-test that
  nobody runs is a self-test nobody notices breaking — the same argument as `tests/README`, applied to
  the surface an operator is most likely to point at a deployment. `make selftest` and `make game-day`
  run them locally with no arguments.
- **`tools/helm_fidelity.py`: the chart render is now compared against a real Helm client, and the
  comparison is a command rather than a memory.** `tools/deploy_check.py` renders `deploy/helm/docket`
  through a declared subset of the template language so that it runs anywhere — no Helm, no cluster,
  no network — and that design costs one claim: the render has to be the render a cluster would get.
  The new tool runs `helm template`, keys both renders by `kind` and `metadata.name`, compares them
  field for field and names the path of the first difference inside a document. Re-run on 2026-10-15
  against Helm `v3.16.3`: **5/5 documents identical field for field**. It is deliberately not part of
  `make check` — CI has no Helm client — so it exits **2** with that reason, the same convention as
  the checker without PyYAML, and `tests/test_helm_fidelity.py` asserts that exit code, the pairing
  rule, the difference reporting and (where Helm exists) the comparison itself.

  Its first working run found a defect in the *tool*: it split the document stream on the characters
  `"\n---"`, which eats the newline ending the previous document's last line — and for the policy's
  `|` block scalar that newline is part of the value, so the tool reported a one-character difference
  Helm does not have. The separator is matched as a line now, and the test pins it. That is the second
  time in this release that the instrument, not the artefact, was what failed first.
- **Supply-chain checks that read the manifest instead of repeating it**
  (`tests/test_supply_chain.py`, `.github/workflows/security.yml`). `dependencies = []` is the project's
  strongest packaging claim and it is worth what the file it lives in is worth, so a test reads it,
  pins the three extras by name, and then walks the syntax tree of every shipped module to assert that
  the only imports outside the standard library are `cryptography` and `yaml` — two imports, in two
  files, both optional, both behind a guard, with a test that fails if either stops being guarded or
  stops being used. That is the failure a dependency scanner cannot see (the author's environment has
  the package for other reasons; the user's raises `ModuleNotFoundError`). `pip-audit` covers the other
  half on a *schedule* rather than on every pull request, because a CVE disclosed in a test dependency
  has nothing to do with tonight's commit and a job that reddens unrelated changes gets ignored; as of
  2026-10-15 it reports no known vulnerabilities across the development and CI toolchain.

- **A response cache at the boundary, with every hit in the ledger** (`docket run --cache`,
  `[cache]` in the policy, `src/docket/cache.py`). A repeated request — same upstream path, same
  canonical body — is answered from the response the boundary already received, and the provider
  sees nothing. The feature is built around one rule: **a hit is never silent.** Each one is a
  `model.cache_hit` event naming the response it replays (`origin.seq`, `origin.hash`), and the
  money it saved is recomputed by `docket verify` from that origin's token counts and the embedded
  price table, so a saving is arithmetic a stranger can redo rather than a number the producer
  chose. Three checks were added to both verifiers (`cache-savings`, `cache-hit-origins`,
  `cache-hit-proofs`), the receipt carries a `cache` member only when a cache was in play, and the
  format specification documents the event and the member (a compatible addition under `v1`).
  What is stored is only what the provider actually sent: an error, a stream cut short in flight,
  or a response rewritten by fault injection is never cached, and the reason travels on the
  response event. What a hit is *not* is a model call: it moves no cap, no total and no call count,
  so the run's spend keeps describing money that was actually paid, and the saving is reported
  beside it. Three ways an entry goes away, all counted and all tested: TTL expiry, LRU eviction,
  and refusing to store. The hazard is named and bounded rather than hidden — **a cache can answer
  a loop that spends nothing, and no dollar cap can end a loop that spends nothing**, so
  `cache.max_hits` is a ceiling on the cache's own answers; when it is reached the boundary stops
  answering, the run starts paying again, and the moment is written to the ledger as a note.
  `docket demo --cache` shows exactly that, and the load harness gained a cached point alongside
  the uncached one (`docs/measurements/2026-10-15-2core/cache-report.json`): 2,000 identical calls
  across 8 threads, 8 of them reaching the provider, $11.952000 not spent, the receipt still
  verifying. No request coalescing, exact-match keys only, in-memory for one run — each named as a
  limit in `docs/caching.md` rather than left for a user to discover.
- **A cache conformance vector** (`spec/vectors/cache.json`, `cache-digest.json`): a small cached
  session both implementations must reproduce, including the saving, the spend it does *not*
  change, and the case where a hand-edited saving must fail verification.

- **A ninth game-day phase for the cache's own failure mode, and an agent that repeats itself on
  request** (`docket game-day --phases cache-brake`, `DOCKET_AGENT_MODE=loop`). Every other phase
  asks what happens when something breaks; this one asks what happens when nothing does — the loop
  stops costing money, so nothing a dollar cap watches will ever stop it. The reference agent's
  usual modes append a step counter to each prompt, which makes every request unique and a cache
  pointless, so `loop` sends the same body every call: the traffic a cache is *for*. The phase runs
  eight calls against a `max_hits` of 2 and checks the ledger and the disk rather than the
  intention — two `model.cache_hit` events, the ceiling recorded as a note and named in the
  receipt, the calls after it arriving at the provider again, the saving recomputed by `docket
  verify`, and the spend still describing money actually spent. `audit_run` gained the two facts
  the check needs (`cache_hits`, `cache_ceiling_note`) so the audit stays a re-derivation from the
  file, and a phase that could not see them would fail rather than pass vacuously.
- **The test table in `docs/testing.md` can no longer go stale quietly.** Its counts are collected
  (`pytest --collect-only`) and now *checked*: `tests/test_doc_links.py` re-collects the suite and
  fails on any row that disagrees, or on a test file the table has forgotten. The paragraph above
  the table used to say the CI run verified this; nothing did, and the first run of the new test
  caught the table missing a file that had just been added to it. This is the same failure mode as
  the stale conformance vector — a number in a document that no test reads — so it gets the same
  treatment.
- **A load point and a soak, as real supervised runs** (`python -m docket.loaddriver`,
  `tools/loadtest.py`, `make load`, `make soak`). The driver is a supervised child like any other
  agent: it goes through `DOCKET_PROXY_URL`, so every call it makes is metered, capped, recorded
  and verifiable afterwards, and a load generator that talked to a provider directly would be
  measuring something else. It separates *refusals* (the boundary enforcing a limit, with the code
  it sent) from *errors*, paces itself to a target rate so an endurance run lasts the whole window,
  and summarises client latency with the receipt's own nearest-rank rule. The orchestrator starts a
  scripted provider as a service, runs `docket run`, samples the supervisor's RSS once a second
  (plus `VmHWM` for the peak a sample can miss), then reads the receipt and the ledger back and
  **refuses to call the run usable as evidence** if the client's count and the ledger's rows
  disagree, if any response row lacks a latency, or if the receipt no longer verifies. `make load`
  and `make soak` are opt-in targets: a shared CI runner's throughput would be a fact about the
  runner, so CI runs this harness's tests instead.
- **`docs/testing.md` states the measured numbers** — throughput, client-latency percentiles and
  memory over ten minutes, with the host, the exact commands and the two things the measurement
  cannot support (a provider's latency, and any claim about another machine).
- **Snapshots and rollback** (`--snapshot`, `--rollback-on-breach`, `docket restore`): a
  content-addressed copy of the watched tree's *contents*, taken before the agent starts, with
  a manifest of path, digest, size, mode and mtime (`docket/snapshot/v1`). A breached run
  restores the tree automatically and records what it restored as `x-docket.rollback-applied`;
  `docket restore <receipt>` performs the same restore on demand, dry-run by default. Paths the
  snapshot could not cover are named in `ops.snapshot.not_covered`; a tree that does not fit the
  budget is refused *before* the agent starts (exit `43`); a file edited after the run is
  refused unless `--force`. See [ADR 0004](docs/adr/0004-content-snapshots-and-confinement.md).
- **Restore confinement**: a restore only ever touches files inside the tree it was pointed at.
  Paths from a ledger or manifest are checked, not trusted — no absolute paths, no `..`, no
  symlinked component, blobs located by digest inside the store, digests required to be 64 hex
  digits, modes masked to permission bits — and the receipt, ledger and manifest must agree on
  the tree, with `--root` when they do not.
- **The evidence directory is excluded from the watched tree** by the Supervisor itself, from
  resolved paths, and the exclusion is recorded (a `note` event and the effective policy in the
  receipt). Previously a programmatic run whose docket directory sat inside the working
  directory recorded its own ledger as a file *the agent* changed, and a rollback plan derived
  from that diff contained an instruction to delete the record it came from.
- **`docket game-day` gains a `rollback` phase** (`--phases rollback`): seeded files, an agent
  that damages them, a cap breach, then automated restore — audited against the disk (files
  byte for byte), the receipt and the ledger, including that the run's own evidence survived.
- **Two URL guards, one per kind of caller**, applied at every `urlopen` in the package:
  `util.http_url` (the scheme must be http or https, and a host must be present) for code that
  stands in for an agent and may be pointed at any provider, and `util.local_http_url` (that,
  plus the host must be this machine) for Docket's own clients — the boundary recorder and the
  self-test probes — whose address comes from the environment. A `DOCKET_PROXY_URL` that is not
  loopback is now refused when the recorder is constructed rather than at the first event, and
  `0.0.0.0` is *not* accepted as a destination. This is the answer to the static-analysis
  finding that flags an unchecked `urlopen` as a possible SSRF or local-file read.
- **An applied restore writes its own record**: `.docket/restores/<utc-stamp>-<docket-number>.json`
  (`docket/restore/v1`) with the receipt and store it came from, the resolved root, whether
  `--force` was used, every per-path outcome, the counts, the uncovered paths and the exit code.
  Several restores of one run are all kept. The run's ledger and receipt are never edited: they
  are historical observations, and appending to them after the fact would break the receipt's
  agreement with the ledger it cites. Dry runs write nothing.
- **`tools/mutate.py`, and `make mutation`**: single-site source mutation — comparison and boolean
  operators, `not`, boolean and small-integer constants, `min`/`max`, and any `return`, rewritten
  through `ast` and spliced back by character span. One mutant per run against a declared test
  subset, in a copy of the repository, with the harness falsifying itself first (splicing back
  verbatim must be a byte-for-byte no-op, and the unmutated sandbox must pass before any mutant
  runs). Current scores and every classified survivor: [`docs/testing.md`](docs/testing.md#mutation-testing).
- **`make audit`**: `bandit` over `src/` and `tools/`, also a CI step. Every remaining class of
  finding is either fixed or has one written-down reason in `[tool.bandit]` (`"PASS"` as an
  output word, seeded `random`, list-argv `subprocess`); nothing is suppressed without a reason.
- **Per-call latency, and a verifier that recomputes it.** Every usage row may now carry
  `latency_ms` — the round trip the recorder measured, in whole milliseconds — and the receipt
  derives `latency` from those rows: `{"method": "nearest-rank", "measured_calls", "total_rows",
  "total_ms", "max_ms", "p50_ms", "p95_ms", "p99_ms"}`. The rule is stated because a percentile
  without its rule is not a fact: the samples are sorted ascending and the rank is
  `ceil(p/100 · n)`, clamped into the sample, so p99 of 15 calls is the 15th value and p99 of 200
  is the 198th. Both verifiers recompute the summary from the same rows and **fail** on a member
  that disagrees, a `method` this verifier does not implement, a member the format does not
  define, a row value that is not a whole non-negative count, an unreadable appendix, or a
  summary stated over rows that carry no measurement at all — the last one being a false-pass
  hole that mutation testing found in this feature rather than a reviewer
  ([`docs/verification.md`](docs/verification.md), spec §6.2). A receipt with no measurement says
  nothing rather than zero, and coverage is explicit: `measured_calls` of `total_rows`.
- **`tests/test_invariants.py`: the properties no single-feature test can state.** Fourteen
  executed invariants over the places an operations reader worries about — two writers on one
  ledger are *refused* rather than interleaved (a file holding two chains is a file that accuses
  an honest run of tampering); eight threads appending 200 events through one handle produce one
  event per sequence number, in order, with no partial line; a replayed request is recorded as
  **two** calls and never deduplicated, because collapsing a duplicate would report a run that
  did not happen; a stopped boundary releases its socket and its thread; a supervised agent killed
  with `SIGKILL` leaves a readable prefix and no receipt that claims a clean exit; a wall-clock
  cap is measured on the monotonic clock, so a clock that jumps forwards cannot extend a run; a
  clock that jumps *backwards* cannot turn a bad clock into a false breach.
- **`DOCKET_AGENT_MODE=backoff`** in the reference adversarial agent, plus `Retry-After` parsing
  (`DOCKET_AGENT_MAX_SLEEP`, `DOCKET_AGENT_SEED`): full jitter under a ceiling, doubling per
  refusal, with the wait and the hint the boundary asked for written into the ledger as `note`
  events. This is the client's half of the rate-limit contract, and without it the boundary's
  `Retry-After` was a promise nothing in the repository ever exercised.
- **A fixed boundary port, and evidence of which port it was.** `--port N` (`docket run`) binds the
  boundary to a loopback port an orchestrator can probe; `0` (the default) still means any free
  port. The requested port is recorded in `session.start.boundary.requested_port` and the port that
  was *actually* bound in a note written once the socket exists, so the evidence never states a
  guess as a fact. Privileged ports are refused at the flag with the range named.
- **`--upstream NAME=URL`**, repeatable, for pointing a provider prefix at a container, a mock, an
  OpenAI-compatible gateway or a self-hosted model. The override is recorded as evidence — name and
  destination only, with query, fragment and userinfo stripped by `util.sanitize_url`, because a
  token pasted into a base URL must never reach a hash-chained file (`tests/test_proxy.py` asserts
  the secret is nowhere in the ledger's bytes). `docket init`'s emitted policy and this flag are the
  two ways to configure a container; both are checked by `tools/deploy_check.py`.
- **`python -m docket.fakeupstream <flags>`**: the scripted provider as a *service*, with `--host`,
  `--port`, `--steps`, `--model`, `--input-tokens`, `--output-tokens`, `--delay-ms`, a JSON
  handshake on stdout, a real `GET /healthz`, exit `2` when the port is taken, and a warning when
  asked to bind beyond loopback. This is what makes the compose file and the Kubernetes manifest
  produce a real receipt offline.
- **The boundary's operations endpoints are documented** (`docs/cli.md`): `/docket/healthz`,
  `/docket/readyz` (readiness is an *evidence* question — ledger writable, volume above its floor,
  breaker shut) and `/docket/metrics` (Prometheus text). They are loopback-only by design, and
  `docs/deployment.md` says what that means for probes and scrapers.
- **`deploy/`**: a container recipe (`Dockerfile`: two stages, non-root, read-only-root-friendly, no
  credentials, `STOPSIGNAL SIGTERM`), a compose smoke test whose verifier waits for the run and
  mounts evidence read-only, a hardened pod topology (two containers sharing loopback, `Recreate`
  strategy, probes on declared ports, default-deny NetworkPolicy), and a Helm chart in a **declared
  template subset** so a deterministic renderer can check what it produces.
- **`tools/deploy_check.py` and `make deploy-check`**: 16 static checks over those artefacts — no
  `:latest`, non-root, no privilege escalation, all capabilities dropped, read-only root filesystem,
  resource requests and limits, probe ports that are actually declared, one writer per evidence
  volume, a ConfigMap policy parsed by Docket's *own* loader, default-deny networking, a compose
  verifier that waits, and CI workflows that parse (a workflow with a YAML error runs nothing and
  looks green — this check found exactly that in this repository's own CI). It exits `2` rather than
  passing when its YAML reader is missing. `tests/test_deploy_check.py` breaks each invariant on
  purpose, one fixture per check, so the checker cannot be decoration.
- **`docs/resilience.md`** — what happens when the world misbehaves (breaker, throttle, timeouts,
  caps, evidence guard, ceilings, readiness, clocks, interruption), each mechanism with how it is
  proved, and the three things deliberately refused. `docket init` has pointed at this file since
  0.1.0; it now exists.
- **`docs/production-coverage.md`** — a map of 77 production-engineering concepts, each cell
  naming the code and tests behind it, or the document that argues the boundary, or the reason it is
  out of scope, in a controlled vocabulary (*built*, *measured*, *modelled*, *reasoned*, *out of
  scope*). It states the three kinds of absence separately instead of blurring them.

- **The docs site builds, strictly, from a staging step that carries what the docs link to.**
  `mkdocs build --strict` aborted on this repository: `docs/deployment.md` and the measurements
  README link to `tools/deploy_check.py` and `tools/loadtest.py`, which are real files, referenced
  the way every other repository path is referenced — and which `tools/build_docs_site.py` did not
  copy. The staging step now carries them, and `tests/test_doc_links.py` stages the tree in a test
  and resolves every relative link in the docs against it, so this needs no `mkdocs-material` to
  check.
- **`deploy` extra, and the load driver's own guards.** `pip install -e '.[deploy]'` installs the
  checker's YAML reader on its own; `tests/test_loadtest.py` gained tests for the argument guards
  (`calls`/`concurrency` below one, a boundary URL the driver cannot parse), the zero-length
  summary, the report's sorted-key byte-stability, the driver's `--help` text, and the RSS
  sentinel (`UNMEASURABLE`, now a named constant rather than a literal `-1` compared twice).

- **Two cache tests that close the boundary cases the mutation sweep kept asking for.** A mutant of
  the origin guard (`seq < 0` → `seq <= 0`) survived because no test ever stored a hit that copies the
  **first event in a ledger**: the ledger numbers its events from zero, so event 0 is the first
  provider call of every run — and the response a cache is most likely to be asked for twice. The
  test builds a real ledger, takes event 0, stores a hit that names it, and reads the group back
  through the summary the receipt is built from. The second test is a one-second TTL: a guard written
  `ttl_seconds > 1` treats a one-second TTL as "no TTL at all", and the other expiry test's thirty
  seconds cannot tell the two readings apart, so the entry would be served stale until eviction.
- **A second mutation round on `resilience.py`, and the two paydown claims it falsified.** A wider
  re-measurement of the same module reported 25 survivors where the first round had classified
  fourteen, and two of those survivors were covered — or so their docstrings said — by tests that
  named them. The re-run showed what had actually happened: one mutant is *equivalent* (both
  `probes_in_flight = 0` writes in the breaker are overwritten by the reset inside `allow`, so the
  test that claimed to kill them kills the one that matters), and one was never reached (the
  half-open `return True`, which needs a probe that *fails*, a path no test walked). Both docstrings
  now say which mutant they kill and why. Five tests close the rest: a failed probe reporting that
  it re-opened the circuit, the limiter's refusal counter, the fault injector's per-kind
  probability guards (a `probability: 0` fault must not be injected, and a `drop` must never be
  returned as a response rewrite), every fault kind describing itself rather than another kind, and
  a clean `EvidenceGaps` that is neither degraded nor failing fast. The survivor list is a line
  number index into the source: a claim that one test kills "both" mutants is a claim about
  positions, and it goes stale the moment the file moves.
- **The Helm-fidelity tool's own contract is tested, not just its happy path.** The tool existed to
  be run by a person with Helm installed; its mutation sweep killed 13 of 47 candidates, which is
  the shape of a tool whose *reporter* is untested. The suite now drives `main()` through a
  stand-in client: renders that agree exit 0 and name the version and the file they skipped,
  a field that differs exits 1 with the object and the field path, a Helm that fails (or cannot be
  executed at all) exits 2 with the client's own message, a stream that names one object twice is
  refused by kind and name, and a client that cannot answer `version` is labelled unknown rather
  than crashing the comparison. That took the score from 27.7% to over 85% without touching what
  the tool does.
- **The standards map has a guard of its own.** `docs/testing.md`'s counts were pinned and the
  standards page was not, so `tests/test_doc_links.py` now asserts that every source cell names
  something a reader can look up — a year, a domain or a document identifier — that every strength
  comes from the small vocabulary the page defines, and that the size the companion maps quote is the
  size the page has. It caught one row on its first run: a book, named with neither a link nor a year.

### Removed

- **The adoption-and-sponsorship model, its detail page, and the star-count sweep that fed them.**
  The adoption model argued from GitHub star counts — how many repositories hold more than 100,000
  stars, how fast ten comparable projects reached theirs, and what that implied about this project's
  own twelve-month target — and its companion page costed seven deliverables against a funding goal.
  Both were written for a reader deciding whether to bet money on the project, which is a real reader
  and not the one the repository is for. Presenting the tool primarily through its growth targets is a
  claim about attention, not about software, and the two documents spent more words on the star
  arithmetic than on what the boundary does. The raw sweep JSON went with them, because nothing else
  in the tree quoted it.

  What is kept, and why: the landscape figures in [`docs/evidence.md`](docs/evidence.md) §2 — stars
  and forks for comparable projects, fetched 2026-10-13 and labelled as a snapshot — because they
  size the field a reader is choosing in. The governance principle that funding cannot buy a format
  change, a roadmap veto or silence about a defect, because that is a safeguard rather than a pitch.
  And one sentence in the README, in the licence section, pointing at
  [`SPONSORS.md`](SPONSORS.md), which is now a short page stating what support pays for, what it does
  not buy, and that a sponsorship does not buy support.

  `tests/test_strategy_numbers.py` went too: it existed to reconcile the deleted page's figures
  against the deleted sweep, and a test guarding a removed claim is decoration.

### Fixed

- **The tamper demo is now reproducible.** `docket demo --tamper` doctored the ledger and
  printed `NOT VERIFIED`, then told the reader nothing about how to see that for themselves —
  and the obvious follow-up, `docket verify <receipt>`, answered `VERIFIED`, because the receipt
  is intact and nothing had looked for the ledger. The demo now prints the command that
  reproduces its own verdict, receipt and ledger included, and `docket verify` finds that
  ledger. Four documents also told a reader to run `pip install docket` as the first step for a
  package PyPI answers 404 for; each now names the install-from-source command that works today
  and says when the published one starts working, with a test that refuses an unmarked one.
  The README's `--tamper` transcript is labelled as one run of a demo whose event number and
  hashes change each time, and exit code `43` is stated as what it is: the policy could not be
  loaded.

- **The growth model re-based onto the sweep it names.** Every figure in its comparison table, its
  three ecosystem-threshold rows and its inline audience sentences became the value in that sweep, with
  ages as months since creation and each rate as the stars over that age. The undated first sweep was
  kept as it was written: it fed the adjacent-audience rows the research log quotes, and an observation
  archive is not re-based.
- **The accessibility checker read a form control's own content as its name, and a wrapping `<label>`'s
  whole text as a label.** The checker was swept by the mutation harness (136 candidates, 134 killed,
  98.5% — the score and both survivors are in `docs/testing.md`), and reading the survivors found three
  defects in the tool. A form control is not named by its content — WAI-ARIA gives `textbox`, `combobox`
  and `checkbox` the *author* as their name source — so a `<label><textarea>draft</textarea></label>` was
  named `draft`, a value used as a name. A wrapping `<label>` with no text of its own fell back to the
  whole wrapper, which names a control after its own value; it now contributes only the text outside the
  control. And the document title was read twice by two handlers keeping a flag in step, with both
  branches unreachable — the title is read from the finished tree, because an unkillable branch is
  deleted rather than explained.

- **The accessibility checker read the `title` attribute before the element's own text.** No verdict
  changed — a tooltip is a name, so a link named by one passed either way — but the precedence was
  wrong, and the count it fed was wrong with it: the first tally called 442 links by their tooltip
  when a reader hears their text. The order is now the one *Accessible Name and Description
  Computation 1.2* §4.3.2 step 2 specifies — `aria-labelledby`, `aria-label`, a control's `<label>`,
  name from content, and only then the tooltip, which the specification marks as used *only if
  nothing else, including subtree content, has provided results*. Three tests assert the precedence
  from the report rather than from the verdict, because the verdict cannot tell the difference.

- **The code of conduct carried two `h1` headings, which the new site check found on its first run.**
  `CODE_OF_CONDUCT.md` opened with a title line above the Contributor Covenant's own heading, so the
  rendered page had two roots for the outline a screen reader announces. The Covenant's heading is
  the document's heading now.
- **`tools/a11y_check.py`'s "could not run" path exited 1, not the 2 its docstring promises.**
  `sys.exit("message")` exits 1 — the code this project reserves for "found a problem" — so a
  directory that was not a built site would have been recorded as a failing check. The tool's own
  test caught it: `test_exit_codes_are_0_1_and_2`.

- **A documented-flag class of defect that had never been checked, and a re-base that was typed rather
  than measured.** Three of the last four dependency-matrix rounds were quoted in this page with a
  number worked out by hand from the previous round's, and all three were wrong — by one, by two, by
  two — which is the same failure the matrix section already records for `paste | bc`, one level up.
  The sections now say so, and the plan's two moving numbers are written by
  `mutation/rebase_plan_numbers.py` (workspace, not shipped), which reads them from the measurement,
  refuses a missing label instead of reading it as zero, and re-reads the file before reporting.

- **`docs/incident-corpus.md` opened by giving the failure mode a name it does not have.** "Docket's
  whole reason to exist is a failure mode with a name: **Denial of Wallet**" — the name is
  *unbounded consumption* (OWASP LLM10), and "denial of wallet" is how practitioners describe it. The
  page now says so, in the same correction round as the fabricated rank above.

- **A fabricated citation rank in four documents, including the README.** "OWASP's *Top 10 for Agentic
  Applications* puts denial of wallet at risk #2" appeared in `README.md`, `docs/evidence.md` and
  `docs/incident-corpus.md`, and the phrase it stood for — that the agentic list
  "treats resource/cost abuse as its own class" — is not true either: its classes are ASI01–ASI10, and
  cost exhaustion is a class in the **LLM** list (LLM10:2025 *Unbounded Consumption*), which all four
  documents already cite correctly two lines away. The evidence ledger had even hedged that the rank
  "could not be pinned to the primary text, so this repository does not cite a rank for it", so the
  repository was contradicting itself in adjacent paragraphs of the same page. The primary page was
  fetched and read (2026-10-16) to settle it: it states the date and the 100+ reviewer peer review and
  does **not** enumerate the entries, so the entry names are grade B (five consistent secondary
  write-ups) while the framework's existence, date and review are grade A. All four sites now name the
  classes a runaway spend actually belongs to — **ASI08 *Cascading Failures*** and **ASI10 *Rogue
  Agents*** — and `docs/evidence.md` carries both grades with the earlier wording marked as wrong.

- **`make test` and four CI jobs ran the suite two `-q`s deep, so their logs carried no counts.**
  `pyproject.toml`'s `addopts` already carry one `-q`; a command line that adds another asks pytest for
  `-qq`, at which level it prints the progress line and nothing else. It exits 0 either way, which is
  how this survived review: green jobs, no line saying how many tests passed, and a CONTRIBUTING.md
  promising "1028 tests" with nothing in the output to confirm it. Measured before it was fixed — with
  `addopts = "-q --strict-markers"`, `pytest tests -q` prints `1 passed in 0.00s` on a one-test file,
  and the same command at `-qq` prints a dot and stops. All five lines now run plain
  `pytest tests -ra`: the counts line is back in the Makefile's output and in every CI log, with the
  skip reasons beside it.

- **Two survivor rows in `docs/testing.md` account for their mutants line by line now, and one of
  them gave the wrong reason.** `ledger.py:628-646` is six mutants under one verdict, and the verdict
  was written as if all six sat in dead code: "those branches are the `else` of `fcntl is not None`,
  so on any platform with `flock` they cannot run at all". Five of them do — `:628`'s `and`/`not` and
  the `unlink`, `return` and `True`→`False` beside it are all inside `_acquire_lock`'s
  `try: # pragma: no cover - Windows` block, past the `fcntl` early return. The sixth, `:646`'s
  `unlink(missing_ok=True)` in `_release_lock`, is **reached on this platform** and is a different
  kind of survivor: `missing_ok=False` raises `FileNotFoundError` for a lock file that is already
  gone, and the line above suppresses `OSError`, so the two spellings cannot be told apart here. The
  row now says which is which and cites every line. The Windows-probe row (`ledger.py:664-672`) does
  the same: it names all ten of its mutants — the two `WinDLL`/`OpenProcess` arguments, the two
  `if not …` guards, what each guard returns in both spellings, and the liveness comparison in both
  — instead of naming four families of them and leaving the count to the reader's arithmetic.

- **The Helm-fidelity comparison had one gate and two prerequisites.** With a Helm client on `PATH`
  and PyYAML absent, `test_the_checkers_render_is_the_render_helm_produces` ran and failed on
  `tools/helm_fidelity.py`'s `PyYAML is not installed` exit 2 — a red suite for a setup the README
  describes, where every other test in the file both skips and says why. The gate named the client
  only; it now names both, in the idiom `tests/test_deploy_check.py` already used
  (`importlib.util.find_spec("yaml") is None`). Found by re-running the dependency matrix with the Helm
  client installed, which is what turned a skip into a run: cells 1 and 2 were red with one failure
  each, and the round after the fix is green in all four — **980 / 985 / 1,022 / 1,027 passed** with
  47 / 42 / 5 / 0 skipped, and the documented dev setup now runs the whole suite with **nothing
  skipped**.

- **Two measured reports the documentation names were in the authoring checkout and in no clone.**
  `.gitignore` ignored `load-report.json` and `soak-report.json` as bare names, which git reads as
  "this name, at any depth" — so the curated reports under `docs/measurements/2026-10-15-2core/`,
  named by that directory's README, by the FMEA row for tail latency (`load-report.json`,
  `soak-report.json`) and by `docs/production-coverage.md` (`cache-report.json`), were tracked by
  nobody. Nothing in the test suite noticed, because the test that holds those names to the tree ran
  where the ignored files happened to be present. The rules are anchored to the repository root now
  (the copies `make load`, `make soak` and `make cache-load` write there are still outputs), the four
  reports are in the tree, and the new canary above is the reason this cannot come back quietly. It
  was found by running the release checks against a clone of the bundle rather than against the
  working directory — the same check that exists to notice a difference between the two.

- **Four tests in `tests/test_cache.py` killed one mutant on their line and stopped one token
  short of the other** — and what said so was the sweep, not a reading. The verifier's full corpus is
  177 candidates; after the paydown round it stood at 172 of 177 (97.2%) with five survivors, and each survivor was
  re-checked with `--only-line`, the harness's own machinery, before anything was written. Four of the
  five were near misses of four different kinds: an assertion that ended before the type suffix
  (`verify.py:554`, where `malformed[0]` appears twice on one line so the *value* and the *type* are
  two mutants), a fixture that measured one call, where p50 and p99 are the same number
  (`verify.py:568`), a problem list two entries long where the stop condition's two spellings part
  company only at exactly three (`verify.py:761`), and a refuted group whose second sentence — the
  tally loop's, a separate pass that does not skip the group the guard refused — was never asked for
  (`verify.py:867`). All four
  repairs are assertions about the same receipts and none of them touched `src/docket/verify.py`; the
  re-sweep reads 176 of 177 (99.4%), with the argued-equivalent `verify.py:186` as its only survivor. The cache's
  re-measured 128 of 128 (100.0%) is in
  the same table, because `tests/test_cache.py` is in that subset too.
- **Two maps named "33 kinds" where the kind table has 44, and the harness's own documentation named a
  symbol that does not exist.** `TARGET_TESTS` was never a name in `tools/mutate.py`; the map is
  `DEFAULT_TESTS`, and both the tool's docstring and `docs/testing.md` said the other one. The count
  in `docs/production-coverage.md` and `docs/sre-coverage.md` had rotted the same way — a number
  written when it was true, beside a table that grew. Both are the class of claim that a reader
  cannot check and a test cannot see, which is why the seven brief terms that were answered only in an
  unsearchable spelling (`E2E`, `localization`, `black-box`/`white-box`/`grey-box`, `A/B`,
  `postmortem`) now appear in the maps in both spellings.
- **The optional-dependency matrix described a suite 152 tests smaller than the one in the
  tree.** The four cells in `docs/testing.md` quoted 840/845/868/878 passed — a first row whose own
  two numbers sum to 873 tests where `pytest` now collects 1025 — from a round measured before
  the contract, cache, i18n and fuzz suites existed. They are re-measured — a fresh venv per cell,
  `pip install pytest` plus the extras its row names, `-ra` so every skip carries its reason — and the
  third column now names what each skip would have checked. The previous matrix also recorded
  `0 skipped` in every cell beside summary lines reading 47, 42, 6 and 1: the skip counts had been
  summed with `paste | bc`, and `bc` is not installed everywhere. The sum is `awk` now, the counts are
  checked against each run's own summary line before they are quoted, and the defect is recorded in
  `docs/testing.md` rather than quietly fixed.
- **The documentation's own consistency check read the built site as if it were the documentation.**
  `tools/build_docs_site.py` stages a copy of the whole tree into `.mkdocs-site/`, and
  `tests/test_doc_links.py` scanned every markdown file it could find — so after `make site`, the same
  page was read twice, and the copy's own illustrative example of a counted reference failed the guard
  as a test file that does not exist. A copy is not a second opinion. Both build directories are
  excluded from the two scanners, and a twelfth test in that file pins the exclusion by asking the
  predicate about paths — including one that merely contains the word `site` — rather than by building
  a site in the test.
- **This file had a bullet spliced into another one.** The verifier-subset bullet ended mid-sentence
  and a second bullet's text continued from it, so a reader got one paragraph that was two claims and
  a sentence that stopped without a verb. It is repaired above; the two `docs/testing.md` bullets that
  said the same thing about stale paydown notes ("can go stale" and "two of them were wrong") are one
  bullet now, with the third shape the 2026-10-16 sweep added: the test did kill a mutant on its line,
  and stopped one token short of the next.
- **A mutation score was taken under an interpreter that had lost an optional dependency, and
  5 mutants were recorded as survivors because the tests that would have caught them
  skipped.** The first re-measurement of `src/docket/verify.py` on 2026-10-16 read 171 of 177 (96.6%) with 6
  survivors; 5 of them are the mutants of the signature block (`verify.py:931`, `verify.py:933`, `verify.py:934`, `verify.py:937`), which is
  exactly the region `tests/test_signing_and_attach.py` and `tests/test_vectors.py` are in that
  subset to cover. They were not missed by the subset and they are not equivalent: `cryptography` —
  the optional extra that seals and signs — was absent from the interpreter the harness runs tests
  with, so every signature test *skipped*, and a skipped test cannot fail. With the extra installed
  the same tree reads 176 of 177 (99.4%) against an unchanged `src/docket/verify.py`. The harness now carries the
  control run's own counts (`passed`, `failed`, `skipped`) in its JSON and markdown reports, marks
  that control `clean` only when nothing skipped, and prints a warning beside the score when it did
  not. Writing the four tests that pin it in both polarities found a second way the same number could
  have gone missing: the harness passed `-q` to a `pyproject.toml` whose `addopts` already carry one,
  and at `-qq` pytest 9 prints no counts line at all — so the control's counts would have been read
  from a line that never printed. The flags are asserted by a real pytest run inside the suite now
  (`tests/test_mutate.py`, 40 tests) rather than by reading them.
- **The dependency matrix caught a test that depended on how fast the machine was.** In the
  `pytest` + PyYAML cell — the configuration `docs/production-coverage.md` describes for running
  without the signing extra — `tests/test_snapshot_restore.py` failed: 1017 passed,
  6 skipped, 1 failed, exit 1, where the other three cells
  were green (977 / 982 / 1,023 passed and 47 / 42 / 1 skipped, all exit 0). The test restores the same run twice and
  looked the two record documents up *by filename*, expecting the second to carry the `-2` collision
  suffix; the two commands stamped their records `20261016T081327Z` and `20261016T081328Z` — one second apart, so there
  was no collision, no suffix, and a `StopIteration` on a machine merely slower than the one the test
  was written on. The test now identifies its records by what they say — one did the work, the other
  found it already done, neither overwrote the other — and the collision is a test of its own with
  `docket/cli.py`'s clock pinned to one instant, so the suffix is exercised by construction instead of
  by luck.
- **The guard that checks test counts beside a filename was checking one mention per file, not
  every mention.** `tests/test_doc_links.py` collected its references into a dict keyed by the test
  file and used `setdefault`, so only the alphabetically first sentence about each file was ever
  compared with `pytest --collect-only`: `docs/sre-coverage.md`'s count of `test_chaos.py` was wrong
  and unread behind two correct mentions of the same file elsewhere. It now walks every occurrence —
  which failed immediately, named the stale sentence, and now passes. The same shape of defect as the
  one the guard was written for, one level up: a claim with no reader looks exactly like a claim that
  was checked.
- **Every game-day phase after `rate-limit` was quietly running under the previous phases' limits.**
  `_phase_policy` built each phase's policy by *sharing* the base policy's `Resilience` object, so the
  two mutators that write to it edited the base itself: `rate-limit`'s two-calls-per-second bucket and
  `ledger-pressure`'s eight-event ceiling were still in force for `ledger-pressure`, `rollback`,
  `cache-brake` and `supervisor-killed`. `cache-brake` is where it surfaced — it was stopped by a
  ledger ceiling it never declared, after a single provider call, with zero hits, and two of its own
  checks failed, so `docket game-day` (and therefore CI, where it runs on every push) exited 1 with
  8 of 9 phases met. `rollback` "passed" the same way, with a cap breach that was not the one it
  declares. The policy is now a `deepcopy`, and `tests/test_chaos.py` asserts the general property —
  building every phase against a base another phase has already been built against must give exactly
  the policy a fresh base gives, and the base must come back untouched — rather than the two values
  that happened to leak. All nine phases pass with the fix in place, and `cache-brake`'s first run as
  its own experiment is the first time it has reached its own hypothesis: 8 calls, 2 hits, the
  ceiling note in the ledger, and `saved_usd` recomputed by the verifier.
- **The mutation harness mutated the wrong characters on any line containing non-ASCII text, and
  dropped the result without a word.** `_span` added `ast`'s UTF-8 *byte* offsets to *character*
  offsets computed from the source's lines, so on a line with an em dash before the token the splice
  landed inside a character; the mutant text did not compile, and a bare `continue` discarded it.
  Every site on every such line was invisible to every score this repository has published:
  `verify.py` offered 95 candidate mutants where it has 177 (46% of its sites), `ledger.py` 171 where
  it has 172. The fix converts both offsets through `_char_offset`, the drop paths now emit records
  that the sweep prints and writes into the report, and `tests/test_mutate.py` pins the regression
  with a fixture whose mutable token sits after an em dash. `tools/helm_fidelity.py`'s second round
  (27.7% → 85.1% of 47 mutants) and the new tests above landed in the same pass.
- **A note that said two secondary summaries disagreed about whether an HTTP 429 may be cached now
  cites the norm that settles it.** RFC 6585 §4 was read in full for this: it ends with "Responses
  with the 429 status code MUST NOT be stored by a cache" — and says the same of 428, 431 and 511.
  Docket's behaviour was already compliant (its cache stores 2xx only, and a test pins that a 429 is
  refused with a reason); what changed is that the rule now has a normative reason in
  `docs/standards.md` and in the architecture invariant that states it, instead of resting on a
  judgement call.

- **The site navigation listed two ADRs twice.** `mkdocs.yml` carried `0002` and `0003` again after
  `0005`, which is the kind of defect a strict build does not catch — the pages resolve, so nothing
  fails; the menu simply has the same entry twice. Removed, and `docs/sre-coverage.md` added to the
  section it belongs in (the link checker asserts that every document is in the navigation, so a new
  document cannot be added without one).

- **A response cache supplied to the boundary was silently ignored while it was empty.** `RecordingProxy`
  chose its cache with `cache or ResponseCache(...)`, and `ResponseCache` defines `__len__` — so an
  empty cache was falsy and a caller who passed their own (enabled, configured) object got a
  different, policy-built one instead. Nothing failed: the boundary simply did no caching, and it
  started caching later if the cache they passed happened to be non-empty, which is a defect that
  appears and disappears with traffic. The unit tests passed a cache through the *policy*, so none of
  them could have caught it; `docket selftest`'s new cache probe passes one as an argument, the way a
  host application would, and caught it on its first run. Fixed in both places — the boundary compares
  `is not None`, and `ResponseCache.__bool__` says plainly that an empty cache is still a cache —
  with a regression test that supplies a fresh cache and asserts the second identical call is a hit.

- **The Helm chart rendered a policy value Docket read as a float, and the checker that was supposed
  to catch that rendered it as an integer.** Helm hands every template a `float64` for each number
  in `values.yaml` (YAML → JSON → `map[string]any`), and Go prints a `float64` in scientific
  notation from 1e6 up, so the ConfigMap mounted into a pod said
  `max_ledger_bytes = 6.7108864e+07` rather than `67108864`. TOML reads that as a float; Docket's
  loader accepts a whole float, so the run would have worked — with a policy whose type is not the
  one the key documents, and with a config in the cluster that differs from the one the repository
  reviews. It went unnoticed because `tools/deploy_check.py` rendered values the way Python writes
  them instead of the way Helm writes them, which made the checker's verdicts evidence about a
  document no cluster would ever see. Three changes: the chart writes its integers through
  `| int64`; the checker converts values to the types Helm's templates receive and formats numbers
  with Go's shortest representation (`_go_float`, pinned against a table produced by running `helm
  template`, Helm v3.16.3); and `configmap.policy-parses` now refuses an integer key that arrives as
  a float, naming the line and the fix. `helm lint` and `helm template` were also run against that
  release for the first time — the chart lints clean and its five documents are now identical,
  field for field, to the static renderer's (`deploy/README.md`). `helm install` still has not run:
  there is no cluster here.

- **A documented default that silence silently removed.** `resilience.max_ledger_bytes` is
  documented as 256 MiB, and every deployment manifest sets it explicitly — but a hand-written
  policy file that *omitted* the key got no byte ceiling at all (`None`), a weaker policy than the
  documentation promised, applied to anyone who used the key's absence as the default it reads as.
  Both shipped example policies were in that position. Absent now means the default, like every
  other key in the section; `0` is how an operator asks for no ceiling on purpose, and the
  behaviour change is stated here rather than slipped into a patch release.
- **A test that could not fail was hiding a stale vector.** `test_committed_vectors_match_the_generator`
  copied the committed vectors into a staging directory but wrote the generator *outside* it, so the
  generator overwrote the real files and the comparison was between each file and itself. It passed
  for as long as it existed, and `spec/vectors/receipt.json` had in fact drifted from the
  implementation: the `latency` member and the policy's cache disclosure line were both missing from
  it. The comparison now runs with the generator inside the staging directory, a canary file is
  overwritten first to prove it wrote there, the set of files produced is compared with the set
  committed, and the vector has been regenerated (two golden renders follow it — the diff is one
  added line in each). The Node suite had a second instance of the same failure mode: it asserted
  that `receipt.json` carries no latency, which was true only because of the drift, so it now builds
  a receipt without latency rather than reading one.
- **The deployment checker exited `1` when it could not run, which is the code it uses for "found a
  problem".** Its docstring, `docs/cli.md` and the operator's own reading all say `2` — "could not
  run" and "found a defect" are the two answers a check has to keep apart. `raise SystemExit("text")`
  exits `1`; the reason now goes to stderr and the code is `2`, and the test that pins it shadows the
  `yaml` import instead of uninstalling anything.
- **Twenty-eight tests failed — did not skip — when PyYAML was absent**, so a bare clone ran red
  while the documentation promised a green suite, and the CI `test` job (which installed only
  `pytest` and `cryptography`) could not have passed on any of its three interpreters. The manifest
  tests now skip with the reason the way the Ed25519 tests do; the renderer tests, which never read a
  manifest, keep running without it; and CI installs `pyyaml` so the job stays at zero skips.
- **The test that claimed to pin the load driver's pacer passed with the pacer mutated.** It
  measured total wall clock through a cold boundary, where the first call's own cost cleared the
  threshold — so `max` → `min` in the schedule survived a test written to catch it, and the soak's
  central claim ("the load is paced") was pinned by nothing. It warms up first now and asserts the
  schedule's floor *and* the ratio against an unpaced run.
- **`ruff` was floored, not pinned, in the `dev` extra, while CI pinned `0.6.9`.** The formatter's
  output is not stable between releases — 0.17 rewrites seventeen files in this tree that `0.6.9`
  leaves alone — so the documented setup could fail `make lint` on files CI calls formatted, and
  `make fmt` could rewrite seventeen unrelated files. The extra now pins the version CI installs.

- **`RateLimiter` accepted numbers it could not honour, and failed inside a request.**
  `RateLimiter(per_second=Decimal("0.5"))` — which is exactly what the policy loader produces —
  passed construction and then raised `TypeError: float * Decimal` on the first call, surfacing as
  an HTTP 502 in the middle of a run. Worse, `per_second=float("nan")` was accepted and *silently
  disabled the limiter*: every comparison against NaN is false, so `tokens < 1.0` was never true
  and every call was allowed. `per_second`, `per_minute` and `burst` are now coerced and validated
  at construction (positive, finite, whole where a count is meant) with a `ValueError` naming the
  value and the type; a `burst` of zero is refused rather than becoming a limiter that refuses
  everything forever.
- **A provider's error code was read as a Docket refusal.** The reference agent took any
  `error.code` from a non-2xx body as the boundary's refusal code, but the boundary relays upstream
  error bodies verbatim and providers use `error.code` for their own codes (`invalid_api_key`):
  a rejected credential would have been recorded as the boundary enforcing a limit, and the agent
  would have backed off politely instead of surfacing a broken key. There is now one reader
  (`policy.denial_code`) that identifies a refusal by the envelope's `type` and nothing else, used
  by both the reference agent and the load driver so the two cannot drift.
- **`docket init` emitted a policy pointing at a document that did not exist** (`docs/resilience.md`),
  and `deploy/` would have shipped referencing two more. `references.resolve` now fails the deploy
  check on any referenced path that is missing, and the link is real.
- **`util.sanitize_url` mangled IPv6 literals**: `http://[::1]:9000/v1` became `http://::1:9000/v1`,
  which is not a URL. Found by the test that pins the port-and-path behaviour, not by reading it.
- **The supervisor printed a negative duration when the clock stepped backwards.** `elapsed` and
  the receipt's `duration_seconds` were computed as `monotonic() - started`, and a monotonic
  source that steps back — a VM restored from a snapshot, a container sharing a stale clock, a
  bug in a caller's injected clock — made both negative. A negative duration is a claim no clock
  can support: a reader comparing it to the cap has to invent a rule for what a negative budget
  means. Both are clamped at zero now, and `test_invariants.py` drives the case. The direction is
  deliberate: a clock that steps backwards can never *raise* the elapsed reading, so it cannot
  manufacture a breach, and the child's lifetime still bounds the run.
- **A receipt that *stated* a latency summary over untimed rows could pass.** `check_summary`
  returned "this claim cannot be recomputed" in that one branch, the verifier read a falsy return
  as "no disagreement", and a receipt could therefore claim any p99 at all as long as its usage
  rows carried no `latency_ms`. Found by mutating the new module (`return problems` → `return
  None` survived) rather than by reading it; the branch now has the test that proves it is
  reachable, and `latency.py` scores 21/21 killed.
- **`latency.py` shipped an unkillable clamp.** `rank = min(max(rank, 1), len(ordered))` could
  never bind: `percentile_ms` raises on an empty sample and on a percentage outside 1..100, and
  `1 <= ceil(p·n/100) <= n` follows from those two guards. The clamp was unreachable code whose
  mutants nothing could kill, so it is gone, with the proof in a comment and two tests that pin
  the extremes of the interval (`p1` and `p100` of every sample size from 1 to 11).

- **`quarantine_torn_tail` could report a repair it had not performed.** When the interrupted
  write had landed its partial line *together with* the newline that closed it — a flush that
  half-succeeds, not only a `kill -9` — the repair split the file at the last newline, which is
  empty in that shape: it reported `performed: true`, moved **zero bytes** and left the ledger
  exactly as damaged as it found it. `docket doctor --quarantine-torn-tail` now splits by the
  reader's own measurement of the torn tail (`torn_tail.bytes`, plus the newline when the file
  has one), and `test_ledger_contracts.py` asserts the moved bytes, the sidecar's contents and
  that the repaired file is healthy afterwards.
- **The Windows lock's liveness probe killed live processes.** `_pid_alive` probed with
  `os.kill(pid, 0)`, which is the POSIX idiom and, on Windows, is `TerminateProcess`: the check
  for "is the stale lock's holder still running?" would have terminated that holder with exit
  code 0. Windows now asks the process table through `OpenProcess`/`GetExitCodeProcess` and
  answers "alive" whenever it cannot tell, because a lock is never stolen on a guess. No Windows
  is available in this environment, so the branch is written, marked `# pragma: no cover`, and
  named as untested in [`docs/testing.md`](docs/testing.md#not-covered-and-why).
- **`read_with_diagnostics` returned eleven keys from a healthy file and nine from a missing
  one.** `healthy` and `chain_error` were documented as present on every path and were absent
  from the "file does not exist" path, so a caller that had only ever seen a healthy ledger got
  a `KeyError` from a ledger that was not there. The key set is now built once, pessimistically,
  and every path fills in what it learned; `test_every_diagnostics_path_returns_the_same_keys`
  pins it across six states (healthy, empty, missing, torn, corrupt, not-UTF-8).
- **A ledger's `repr` carried descriptors and a memory address.** `_handle`, `_lock_handle` and
  the clock were dataclass fields with default reprs, so a ledger printed into a CI log or a
  pasted traceback showed file descriptors, a lock handle and `<SteppedClock object at 0x7f...>`.
  They are `repr=False` now; the repr shows the state that matters (path, events, ceilings, bytes
  written, unwritable events).

- **A document with `NaN` or `Infinity` in it was read by Docket and refused by every JavaScript
  verifier.** Those two tokens (and `-Infinity`) are a Python extension: RFC 8259 has no spelling
  for them, `json.loads` accepts them, and `JSON.parse` rejects them. A request body carrying one
  reached the recorder, could not be canonicalised, and the boundary answered
  `502 docket_internal_error` — a value the client chose made the run's own evidence read
  "internal error". A ledger, manifest or receipt containing one was *accepted* by the reader and
  then failed the chain check with "the event's data cannot be canonicalised", a confusing verdict
  for what is really "this file is not JSON"; and a hostile `Infinity` on the last line of a
  ledger was filed as a *torn tail*, i.e. it claimed the ordinary crash-recovery path. Now every
  parse of a document Docket did not write goes through `util.loads_json`, which refuses the
  non-extensions by name (`Infinity is not a JSON number (RFC 8259 allows neither NaN nor
  Infinity)`), a request body carrying one is refused at the boundary with `400
  docket_bad_request`/`non_finite_number`, recorded as `net.denied`, and never forwarded — a body
  Docket cannot record is a body it must not forward — and a non-finite line is classified as
  damage (`bad_lines`), never as a torn write. `spec` §3.2 now states the rule normatively, since
  it is an interoperability rule and not an implementation detail.
- **Parallel model calls could write the ledger out of order.** The boundary is a
  `ThreadingHTTPServer`, so two calls in flight are two `append` calls: the sequence number,
  the `prev` link, the digest and the write were computed as separate steps, and a burst of
  eight threads produced a file whose events were not in sequence order — a chain that
  `docket verify` then reports as broken, i.e. Docket accusing an honest run of tampering.
  `Ledger.append` now runs under an instance lock, so the file is always one contiguous chain
  in sequence order. `tests/test_ledger_concurrency.py` (8 threads × 25 events, statuses and
  chain checked from disk) and `tests/test_proxy_concurrency.py` (parallel calls through the
  real boundary: sound chain, meter total exactly the sum of the rows, a limiter that admits
  exactly its budget, a breaker that counts each failure once, a burst that cannot walk past a
  cap, and a receipt built from the result that still verifies) cover it.
- **A bad `--max-usd` is a usage error, and `--max-usd nan` no longer disables the cap.**
  `Decimal("NaN")` compares false against every threshold, so `docket run --max-usd nan`
  completed with the cap silently switched off and filed an ordinary-looking receipt reading
  `$0.00 of $NaN cap`; `--max-usd abc` printed `[<class 'decimal.ConversionSyntax'>]` and exited
  `1`. Caps are now validated wherever they are set — an argparse `type=` for `--max-usd`,
  `--max-tokens`, `--max-calls`, `--max-events`, `--max-calls-per-minute` and `--max-minutes`
  (exit `2`, flag named, value quoted, nothing created) and `Caps.set_max_usd()` for code, which
  is the one way to change the field after construction. `inf` is refused as a cap too: an unset
  cap is spelled by omitting the flag, and the receipt discloses it as unset rather than as a
  number.
- **An unexpected exception is reported as a sentence.** The CLI's top-level handler printed the
  exception's `args` raw, which for a decimal error is its class object. The type is always named
  now, and the detail only when there is one.
- **Restoring twice no longer blames the operator.** After a successful restore the file holds
  the snapshot's bytes rather than the digest the run ended with, so a second `docket restore`
  refused it as "changed after the run" — a false accusation, and a command that fails when a
  script, a cron job or a nervous hand repeats it. A path that already holds the snapshot's bytes
  *and* the mode the plan would apply is now reported `already restored`: no write, no refusal.
  The check runs after the plan has been validated against the snapshot store, so a doctored
  manifest is still refused on its own merits rather than reported as an idempotent no-op.
- **A relative working directory made every recorded path absolute.** `fsdiff.snapshot`
  relativised paths against an *unresolved* base, and a relative base can never be a prefix of
  the absolute paths a walk produces — so with `cwd="."` (what `docket game-day` and
  `docket run --root .` pass) every `fs.change` event carried an absolute path. Nothing
  surfaced: no ignore pattern matched, so Docket's own ledger and snapshot store were recorded
  as the agent's changes, and the confined restore then refused its own change list as
  out-of-tree. The base is resolved now, and `tests/test_fsdiff.py` asserts the relative and
  absolute forms agree.
- **A rollback that restored nothing is no longer recorded as applied.** `ops.rollback.applied`
  was set for having *attempted* a restore, so a plan whose every item was refused — see the
  defect above — produced `applied: true`, an `x-docket.rollback-applied` event and a console
  line reading "the watched tree has been put back", over a tree that had not been touched.
  `applied` now means files went back; a declined restore is recorded as
  `x-docket.rollback-refused` with its reason, named in `ops.rollback.reason` and
  `ops.rollback.refused`, and printed by both `docket run` and `docket report`.
- **`spec/vectors` had gone stale.** The committed conformance vectors still carried a policy
  document from before the `[resilience]` defaults existed, so their policy digest — which is
  bound into the first ledger event and therefore into the head, the Merkle root and the receipt
  core digest — no longer agreed with the implementation that is supposed to produce it. They
  are regenerated, and `test_committed_vectors_match_the_generator` now runs the generator into
  a copy and fails if anything differs, which is the check that would have caught it.
- **`explain.total_estimated_savings` names an unreadable impact** instead of skipping it: those
  strings are written by Docket itself, so an unreadable one is a bug, and a silently smaller
  total in a document people make decisions from is worse than a visible caveat.
- **Verification no longer reports two verdicts under one check name.** The Merkle-path verdict
  and the ledger re-binding verdict were both reported as `usage-inclusion-proofs`, so a
  doctored ledger produced `FAIL usage-inclusion-proofs` and `PASS usage-inclusion-proofs`
  a few lines apart. Re-binding is now `usage-rows-rebound`; a test
  (`test_no_check_reports_two_verdicts`) keeps one name to one verdict. The Node verifier's
  detail line now distinguishes the two causes as well.
- **`docket demo --tamper` no longer crashes** when the event it picks is not a
  `model.response`: it edited whichever line happened to be fifth and, for any other event,
  assigned `data["model"] = data` — a self-referencing dict that made `json.dumps` raise. It
  now tampers with the first `model.response` and reports nothing to tamper with if there is
  none.

## [0.1.8] - 2026-10-19

### Fixed

- **`PUBLISHING.md` told the reader to run a command PyPI now refuses.** The guide shipped
  `twine upload dist/*.whl dist/*.tar.gz` with no username, and PyPI has withdrawn username and
  password authentication from the upload endpoint entirely. Setting `TWINE_USERNAME` to the
  account name, which is the obvious thing to do, is refused with
  `403 Username/Password authentication is no longer supported` — an error that names
  authentication rather than the username, so a correct token and a correct account both look
  like the problem. The guide now exports `TWINE_USERNAME=__token__` beside the token and says
  why the literal string is required. This cost one failed upload while shipping 0.1.7.

- **The release build left the previous release's artifacts in `dist/`, so the documented upload
  command was one release away from breaking.** `tools/build_dist.py` copied the new wheel and
  sdist beside the old ones and stopped there, and the command `PUBLISHING.md` gives the reader
  names both extensions rather than `dist/*`. Shipping a second release therefore handed PyPI a
  filename it already holds, which fails *after* the current release's files have been accepted.
  A rebuild now clears the distributions it supersedes and prints each removal, and `--check`
  refuses a `dist/` holding a distribution the provenance record does not name. Only the two
  extensions the upload command reaches are touched.

- **The suite size quoted in four documents was two tests behind.** Adding the guards above moved
  the collected count from 1236 to 1238, and the README, `CONTRIBUTING.md`, `docs/testing.md` and
  `docs/sre-coverage.md` each promise the reader a total. The per-file row for
  `test_build_dist.py` moved from 16 to 19.

## [0.1.7] - 2026-10-19

### Fixed

- **The race the 0.1.6 release gate found, shipped under a number that carries the fix.** The
  0.1.6 wheel and sdist were uploaded before the race was closed, so the published 0.1.6 is the
  one whose receipt could claim 62 events over a ledger holding 64. PyPI reserves a filename once
  it is published, so the only way to ship the fix is a release built from the commit that holds
  it — which is what this is. The remote `v0.1.6` tag pointed at that same commit, its release
  workflow had failed, and no release existed for it; the tag was deleted rather than left
  naming a version whose artifacts did not match it.

  Nothing else changed. The tree is the one `d8992ca` describes: the drain that stops the receipt
  racing the boundary's last recording call, the build-from-ref tooling that keeps an artifact
  from predating its tag, and the publishing guide corrected to name the two distributions
  rather than the whole of `dist/`.

## [0.1.6] - 2026-10-19
### Fixed

- **The receipt no longer races the boundary's last recording call.** The 0.1.6 release gate
  failed on `test_demo_stops_a_runaway_agent_and_verifies` with the receipt claiming 62 events
  over a ledger holding 64, and `event-count` and `merkle-root` refusing a run whose evidence
  was otherwise intact. The cause is a race, not a logic error: the proxy records events from
  its own handler threads, an agent's last act before it exits is a recording call, and the
  supervisor terminated the agent on the cap breach without waiting for that call to land. The
  receipt was built from a ledger snapshot taken before the append, so it described a ledger
  that no longer existed.

  It could not be reproduced locally — 30 serial runs and 96 runs under eight-way contention on
  a two-core box, all clean — because the window is milliseconds wide and depends on a loaded
  runner. It was reproduced by holding the recording handler for 600 ms, which fails every
  time, and that reproduction is now the regression test.

  `RecordingProxy.quiesce` waits for the boundary to have no request in hand, and `run` calls it
  before it takes the filesystem snapshot and builds the receipt. The counter it waits on is
  `active_requests`, taken in `_guarded` around every request on every path and released in a
  `finally`, because the existing `in_flight` counter only moves on the model-call path and the
  recording endpoints are exactly the ones that were racing. If the drain times out the run
  records a control note saying the receipt may under-count, rather than producing a silently
  wrong receipt. Three guards pin it: the demo with a held recording handler still verifies,
  `quiesce` refuses to report success while a request is in hand, and `quiesce` reports a
  timeout rather than hanging. Each was proven to fire by disabling the drain, removing the
  wait, and dropping the counter's release.

- **The publishing guide no longer tells the reader to run a command that fails.** It shipped
  `twine check dist/*` and `twine upload dist/*`, and `dist/` now also holds `PROVENANCE.json`,
  which twine refuses with *"Unknown distribution format: 'PROVENANCE.json'"*. The commands were
  measured before the guide shipped them, which is how the break was found: the first run of
  `twine check dist/*` on a freshly built `dist/` failed. Both now name the two extensions, and
  three guards hold the guide to it — every `bash` block parses, no twine command is handed a
  path that is not a distribution, and nothing reaches twine before the provenance check. Each
  was proven to fire by injecting the defect it names.



- **The release build comes from a git ref, so an artifact can no longer predate its tag.** The
  0.1.4 wheel and sdist were built with `python -m build` from a working tree, two commits
  landed before the upload, and the published long description lacked the README logo the
  repository had. The 0.1.5 sdist repeated the same mistake at a smaller scale: it was built one
  commit before the `.gitignore` that tags it, so the tarball on PyPI carries a `.gitignore` the
  tag does not. Neither could be corrected in place, because PyPI reserves a filename once it is
  published.

  `tools/build_dist.py` now exports the ref with `git archive` and builds from that export, so
  an untracked file cannot leak in and a dirty tracked file exports at the committed version.
  `--check` then reads `dist/PROVENANCE.json` and refuses a mismatch, which makes "the artifact
  matches the tag" true by construction rather than by discipline. `PUBLISHING.md` ships the
  commands in the order that keeps it true.

  Thirteen guards cover it, and each was proven to fire by injecting the defect it names:
  building from the working tree, deleting the tar-slip guard, ignoring a mismatched commit,
  and ignoring an artifact that changed after the build. Two of those guards initially passed
  for the wrong reason — one recorded a commit that could not resolve and so failed on the
  commit mismatch rather than the digest it claimed to test, and nothing exercised the build
  path at all, so "build from the working tree" survived. Both are fixed.

## [0.1.5] - 2026-10-19

### Fixed

- **The published long description carries the mark.** The 0.1.4 wheel and sdist were built from
  the commit before the README logo was committed, so the package page rendered the version and
  the badges but no artwork. `README.md` is the long description, and the two distributions of
  0.1.4 froze it at that commit; re-uploading them under the same filenames is not possible,
  because PyPI reserves a filename once it is published. 0.1.5 is built from the commit that
  holds the logo, and the package page and the repository now agree.

### Added

- **The README carries the horizontal lockup, in both schemes.** A `<picture>` with a
  dark-mode `<source>` and a light `<img>`, 250x80, sitting above `## Why Docket?`. Both
  targets are absolute and point at the documentation site, which is where the rest of the
  README already points: `README.md` is the PyPI long description, and PyPI resolves a relative
  target against `https://pypi.org/project/docket-ai/`, so a repo-relative logo would 404 on
  the package page while resolving correctly on GitHub. That is the same defect the 29
  markdown links already carried, and it took three releases to notice.

- **The PyPI link guard now sees raw HTML.** `INLINE_LINK` matches `](target)` only, so a raw
  `src` or `srcset` was invisible to both halves of `tests/test_doc_links.py`; a logo pointed
  at a relative path would have 404'd on PyPI unreported. `HTML_ASSET` reads the `src`,
  `srcset` and `href` attributes of any `img`, `source` or `link` tag, and both guards use it.
  Both halves were proven to fire on the defect they now name by injecting a relative logo
  path and a dead absolute one into the README and watching the right guard fail.

  Measured on the published description rather than on the source: rendered through PyPI's own
  `readme_renderer` and nh3 sanitiser, the 0.1.5 description carries 52 `href` and `src`
  attributes, 36 of them absolute, 0 relative, and all 30 distinct URLs answer 200.

## [0.1.4] - 2026-10-19

### Changed

- **The visual identity is the bounded-ledger mark, and the retired one is gone.** The repository
  carried an indigo-and-teal receipt-with-a-tick logo whose palette names were still selected in
  `mkdocs.yml` as `primary: indigo` / `accent: teal`, which made the documentation site carry a
  second, retired identity. `assets/` now holds the approved kit: the color, dark, black and
  white lockups, the standalone symbol, the 16-24px micro cut, the wordmark, the 512px avatar
  tile, the 1280x640 social card and their vector sources, with the font notice and a generator
  that renders both rasters from the SVGs. `theme.logo` is the white micro symbol on the cobalt
  toolbar and `theme.favicon` is the adaptive micro favicon rather than a shrunken 512px tile.

  Three defects were found by rendering the result rather than by reading the diff, and all three
  are fixed:

  1. `overrides/main.html` was written without `{% extends "base.html" %}`. `main.html` *is*
     Material's page template, so the home page and the 404 page rendered as that one fragment —
     256 bytes where a 50 KB document belongs. The build stayed green and still reported 53 pages,
     because a page rendered from a fragment is still a page.
  2. The brand stylesheet used `:root`, and Material puts `data-md-color-primary` on `<body>`.
     Both selectors are specificity (0,1,0), so the file won on `<html>` and changed nothing
     anywhere that consumes the variables: Chromium measured the header background as
     `rgb(32, 148, 243)`, Material's blue, in light mode. The selectors now name `<body>` at
     (0,2,1).
  3. The favicon and touch-icon `<link>`s were written as literal strings, which mkdocs does not
     rewrite per page, so they resolved at the root and 404'd on all 47 pages below it. They now
     go through `| url`.

  The site also carried three `theme-color` metas, because Material's palette script appends its
  own and the effective value was `#2094f3`. Deleting the two in the override and fixing the
  cascade leaves one meta that carries the brand colour.

  `tests/test_brand_assets.py` guards all of it, and each of its guards was proven to fail on the
  defect it names by injecting that defect into a scratch copy of the tree.

- **The documentation site is themed in the brand palette.** `assets/brand.css` writes the approved
  cobalt and bright cobalt over the CSS custom properties Material's palette stylesheet sets.
  Material has no colour named for cobalt, so `primary: blue` remains the closest supported
  fallback and the file refines it; every hex in it is from the approved palette, and a guard
  refuses one that is not. `theme-color` follows the same two colours.

- **`assets/` is staged for the site, and the site is rebuilt when it changes.** The stager now
  carries `.ico` and `.css`, which no file in the tree had before, and skips `assets/BRAND.md` —
  the in-repository alias map for the artwork, which mkdocs would otherwise publish as a page the
  navigation does not list. `docs.yml` triggers on `assets/**` and `overrides/**`, so an
  asset-only change redeploys instead of leaving the previous mark on the live site.

## [0.1.3] - 2026-10-19

### Fixed

- **Every receipt since 0.1.0 named a tool version that was not running.** The version was written
  three times — `src/docket/__init__.py`'s `__version__`, `src/docket/receipt.py`'s
  `TOOL_VERSION`, and `src/docket/supervisor.py`'s `DOCKET_TOOL_VERSION` — and each had to be
  edited by hand on every release. Two of the three were still `0.1.0` when the distribution
  installed 0.1.2, so `docket --version` said one thing, `pip show` said another, and the
  `tool.version` field in every receipt named a release that was not the one that wrote it. A
  receipt that misreports its author makes a false claim about provenance, which is the one thing
  a receipt exists to make checkable. All three now import from a new `src/docket/_version.py`,
  which derives the version from the installed distribution, falls back to the nearest
  `pyproject.toml`, and then to `unknown`. `FORMAT_VERSION` moved there for the same reason. The
  guard that had checked only `__init__.py` now scans every module under `src/` for a version
  literal, in both the assignment and the dict-entry form, because scanning one file is what let
  the other two literals survive the first fix.

- **`examples/custom-agent/agent_sdk.py` crashed on the command its own README advertised.** Under
  `docket run`, the session attaches to the supervised run and `session.ledger` is `None` by
  design — the supervisor is the only writer to that file, because two writers would produce two
  competing hash chains. The example printed `session.ledger.path` unconditionally, so the
  supervised command raised `AttributeError` while the standalone one worked. Only the standalone
  path was tested. The example now branches on `session.attached` and says what is true in each
  case, and `tests/test_examples.py` drives the supervised path and asserts the agent's own events
  reached the run's ledger labelled `reported_by: "agent"` and that the receipt verifies.

- **The `Typing :: Typed` classifier was a claim with nothing behind it.** `pyproject.toml`
  declared the classifier and the wheel shipped no `py.typed`, so no consumer's type checker
  would read the package's annotations at all. The package was annotated all along — and 65 of
  those annotations were wrong, which nobody could see because nothing asked. `py.typed` now
  ships, mypy runs in the lint job, and the 65 errors are fixed: narrowing that did not survive
  a second method call, `__exit__` annotated `bool` where it always returns `False`, a signing
  key accepted and then used as any key type, a metrics dict annotated `dict[str, int]` that
  holds a decimal string, and the rest. A guard test fails if the marker disappears while the
  classifier stays.

- **`docket keygen` did not exist, so `docket run --sign` had no way to produce a key.**
  `src/docket/signing.py` could generate an Ed25519 keypair from the first release and nothing
  called it: no command, no documentation. The only way to sign a receipt was to already know
  the library function. `docket keygen [PATH] [--force]` writes the keypair at mode 0600, prints
  the public key, refuses to replace an existing key without `--force` (replacing a signing key
  invalidates every receipt signed with the old one), and exits 70 naming the install when the
  optional `cryptography` package is absent. Documented in the CLI reference, and tested end to
  end: keygen, sign, verify, and a tampered receipt that fails both the totals and the signature.

- **Two `assert`s were added to narrow a type, and `bandit` refused them.** An `assert` is
  removed under `python -O`, and a tool whose whole claim is that it does not skip checks cannot
  rest one on a statement the interpreter is entitled to delete. `verify.py` now tests
  `chain_problem` directly — which is the narrowing that was wanted anyway, and drops the
  `chain_ok` alias that made the tuple unreachable — and `policy.py` raises for the case that
  cannot happen rather than asserting it. The line citations in `docs/testing.md` moved with the
  code and were re-checked against the harness's site list again.

- **Eight definitions in the package were dead code.** `generate_keypair` was the visible one —
  `docket run --sign` was a flag with no way to produce a key, and the function that could have
  produced one was invisible. Seven more had the same shape: `short_hash`, `mb`,
  `money_or_zero`, `stable_seed`, `is_known`, `default_price_table`, `price_document_digest`
  and `_gzip_bytes`, each a one- or two-line helper with no caller anywhere in the tree. Dead
  code is not harmless in a project whose pitch is that a receipt says what the tool did: a
  package that carries functions nothing runs is making the same kind of claim about itself. All
  eight are deleted, and `tests/test_supply_chain.py` now fails when a top-level definition in
  `src/` appears nowhere else in `src/`, `tests/`, `tools/`, `examples/` or `verify-js/`.

- **A signing key of the wrong type produced an `AttributeError` from inside `cryptography`.**
  `load_pem_private_key` returns a union of every key type the library supports, and most of
  them have no `.sign(data)`. The call now refuses a non-Ed25519 key with a message naming the
  file and the key it holds.

- **Every link in the README was broken on the PyPI page.** `README.md` is the package's long
  description, and PyPI renders it with no repository around it, resolving a relative target
  against `https://pypi.org/project/docket-ai/`. All 29 relative links therefore 404ed there while
  resolving perfectly on GitHub — invisible from the repository, which is why it survived three
  releases. They are now absolute: a target the documentation site publishes points at the site,
  so a reader arriving from PyPI lands in the documentation rather than in raw markdown, and
  `LICENSE` points at the repository. Two guards were added: one fails on any relative target, one
  fetches every absolute URL and fails on a non-200.

### Changed

- `Documentation` in `[project.urls]` pointed at a GitHub directory listing. It now points at the
  live documentation site, which is what a reader arriving from PyPI is looking for.

- Two public properties replace a private attribute an example reached into:
  `Session.docket_number` and `Session.ledger_path`, the latter returning `None` when a
  supervisor owns the ledger rather than a path this session does not write to.

- The line citations in `docs/testing.md` were re-based onto the code as it now stands. Each one
  was re-checked against `tools/mutate.py`'s site list, so every number the page quotes is a
  line the mutation harness would actually mutate.

- The conformance vectors were regenerated, because they carry `tool.version` and the spec states
  that vectors move with the implementation in the same commit. Only the version string and the
  two `receipt_core_sha256` digests that cover it changed; all three implementations still agree
  (Python and Node: 88 correct, 0 wrong; Node's own suite: 100/100).

## [0.1.2] - 2026-10-18

### Fixed

- **`docket --version` reported a version the package was not.** `src/docket/__init__.py` carried a
  literal `__version__ = "0.1.0"` while `pyproject.toml` moved to 0.1.1, and nothing compared the
  two. Every receipt's `tool.version` field and the output of `docket --version`, `docket
  selftest` and `docket doctor` said 0.1.0 while `pip show`, the PyPI page and
  `importlib.metadata` said 0.1.1. A receipt that names the wrong tool makes a false claim about
  provenance, which is the one thing a receipt exists to make checkable.

  The version is now read from the installed distribution, falling back to the `pyproject.toml`
  of a source tree and then to `unknown`. The distribution name is not written into the module,
  so renaming it cannot desynchronise the two again. `tests/test_supply_chain.py` asserts the
  derivation and fails if the literal returns.

## [0.1.1] - 2026-10-18

### Fixed

- **The quickstart linked a wheel filename that does not exist.** It told a reader to install
  `docket-0.1.0-py3-none-any.whl`, which the rename to the `docket-ai` distribution had turned
  into `docket_ai-0.1.0-py3-none-any.whl`. The quickstart now installs from PyPI like the
  README does, keeps the clone path for a contributor, and links to `PUBLISHING.md` for why the
  distribution name differs from the command.

- **The PyPI long description quoted a stale test count.** The 0.1.0 wheel was built before the
  supply-chain guard was added, so its rendered page said 1196 tests where the repository said
  1197. The wheels are otherwise byte-identical.

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
  passing when they cannot be performed, and the two implementations agree on the *shape* of a
  failure as well as the verdict: both recompute every event hash, both decline to check the
  head when the chain is broken, and both re-bind each usage row to the recomputed event at its
  sequence number. The Node verifier additionally checks the receipt-core digest against a
  committed vector.
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

- **Attached sessions.** An SDK `Session` started inside `docket run` writes through the
  boundary's `/docket/event` endpoint instead of opening the ledger file itself: one writer per
  chain, and every agent-asserted event carries `reported_by: "agent"` so a reader can tell
  claims from boundary observations. Outside a supervised run it keeps its own ledger, as before.
- **Totals are derived from the usage appendix**, so a receipt can never disagree with the rows
  a verifier recomputes (the failure mode being `$0.00` claimed against `$0.0042` recomputed).
  Rows asserted by the agent are counted and labelled separately (`asserted_calls`).
- `docket verify --seal-key` accepts the key file written by `docket run --seal` or the hex key.
- Zero runtime dependencies. `docket[sign]` is the only extra that installs anything
  (`cryptography`, for Ed25519 signatures).
- Prices in `src/docket/prices.json` are as of 2026-10-13 with sources recorded in the file;
  they are estimates for budgeting, and every receipt states which table it used and flags
  any model priced by fallback as `assumed`.

[Unreleased]: https://github.com/docket-dev-org/docket/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/docket-dev-org/docket/releases/tag/v0.1.2
[0.1.1]: https://github.com/docket-dev-org/docket/releases/tag/v0.1.1
[0.1.0]: https://github.com/docket-dev-org/docket/releases/tag/v0.1.0
