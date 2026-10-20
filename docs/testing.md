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
$ make test          # 1487 tests, no network (2-4 min on a 2-vCPU box)
$ make check         # + ruff, + bandit, + the independent Node verifier
```

| file | tests | what it pins |
| --- | --- | --- |
| `test_canon.py` | 44 | canonical JSON, Merkle trees and proofs, the no-floats rule |
| `test_vectors.py` | 11 | the committed conformance vectors, including that regenerating them changes nothing — and that the *generator actually wrote where the test is looking*, which it did not for as long as that check existed |
| `test_conformance.py` | 57 | the negative corpus is a claim about the format, and this holds the claim: it rebuilds every vector, runs it, and fails if any is accepted or refused by a rule the vector does not name — plus the verifier defects writing the corpus found (an enum read as free text, a duplicate JSON name, a re-binding folded into a proof, a ledger that was repaired instead of refused, and a per-model breakdown that could name a model nobody called) — plus two canaries proving the runner can fail: a rewritten expectation must produce `wrong-check`, and a vector with no expectation must be reported rather than silently skipped |
| `test_policy.py` | 14 | accept and reject paths for every policy key |
| `test_pricing.py` | 19 | price table, cost arithmetic, the missing-model path, ceilings |
| `test_ledger.py` | 18 | append/read, chain resumes, ceilings, `unwritten` bookkeeping |
| `test_ledger_contracts.py` | 35 | the ledger's caller-facing contracts: event dicts, byte accounting, ceilings, a file that grew behind the writer, the lock file, the repair sidecar |
| `test_ledger_faults.py` | 29 | a ledger on a failing filesystem: ENOSPC, EIO, a closed handle, an OS error while opening |
| `test_ledger_concurrency.py` | 4 | one ledger, eight writer threads: order, chain, agreement, file digest |
| `test_latency.py` | 21 | the percentile rule, hostile row values, the verifier's recomputation, the report's tail line |
| `test_receipt_verify.py` | 121 | the receipt and every verification check, plus one name to one verdict |
| `test_proxy.py` | 18 | the boundary: pass-through, streaming, policy enforcement, what is never recorded, and `quiesce` — the drain that stops the receipt racing the boundary's last recording call, proven to refuse success while a request is in hand and to report a timeout rather than hang |
| `test_cache.py` | 74 | the response cache: what may be stored and what may never be, hit/miss/TTL/LRU/ceiling behaviour, the key covering the request as sent, an open breaker not blocking a hit, concurrency, every receipt-level attack on a saving, and twenty-five tests written to kill survivors of a mutation sweep — nine for the cache itself and sixteen for the verifier that recomputes a stated saving |
| `test_proxy_concurrency.py` | 6 | parallel calls through the real boundary: chain, totals, limiter, breaker, cap |
| `test_resilience.py` | 60 | breaker, limiter, fault injection grammar, evidence guard |
| `test_supply_chain.py` | 10 | what the package installs and what it imports: `dependencies = []` read from the manifest, the three extras by name, and an AST walk asserting every import outside the standard library resolves to a declared, guarded extra |
| `test_explain.py` | 13 | analysis rules and their attributed impact |
| `test_supervisor.py` | 11 | the run lifecycle, exit codes, the evidence directory exclusion |
| `test_snapshot_restore.py` | 26 | snapshot, plan, apply, the CLI, idempotence, the restore record |
| `test_snapshot_confinement.py` | 25 | hostile documents: paths, digests, modes, symlinks, keys |
| `test_fsdiff.py` | 5 | what the file diff sees, with a relative and an absolute cwd |
| `test_invariants.py` | 16 | executed properties: one writer per ledger, 200 events from 8 threads, no deduplication of a replayed call, socket and thread release, a SIGKILLed agent, a clock that steps forwards or backwards |
| `test_chaos.py` | 34 | the fault grammar, `audit_run`, the game-day phases (including the cache brake), the reference agent repeating itself on request, and the independence of the phases' policies from one another |
| `test_security.py` | 40 | red team: forgery, injection, bypass, confinement, secret capture |
| `test_cli_input.py` | 51 | numeric flags: every cap, every way to spell it wrong |
| `test_cli_sdk_demo.py` | 26 | the commands end to end, the SDK, the demo, and the race the 0.1.6 release gate hit: a recording handler held for 600 ms must still leave a receipt that verifies against the ledger, because the receipt used to be built before the boundary's last append landed |
| `test_examples.py` | 10 | the five examples executed rather than described: the quickstart and the SDK program run offline and the receipt each one prints is then verified with the command each one prints, one digit of the quickstart's ledger is edited and the same verification is required to fail, both policy files are accepted by a real supervised run, and every README's links, repository paths and Python files are checked |
| `test_i18n.py` | 16 | locale independence, Unicode round trips, non-ASCII paths |
| `test_url_guards.py` | 43 | the two URL guards, and a syntax-tree walk that every `urlopen` uses one |
| `test_diagnostics.py` | 8 | the self-test's probes against a working installation |
| `test_signing_and_attach.py` | 8 | HMAC sealing, Ed25519 signing, agent-asserted events |
| `test_render_golden.py` | 3 | the rendered receipt, report and diff, against golden files |
| `test_loadtest.py` | 37 | the load harness: refusal classification, the pacing schedule, the RSS sentinel, and the orchestrator refusing to call a self-inconsistent run evidence |
| `test_deploy_check.py` | 44 | the static deployment checker, one broken fixture per invariant it claims to enforce, the number formatting Helm actually produces, and the ConfigMap value that arrived as a float — a checker that cannot fail is decoration |
| `test_mutate.py` | 40 | the mutation harness itself: the splice is a byte-for-byte no-op, every mutant changes exactly one line and compiles, a site on a line with non-ASCII text splices by character not by byte (the regression that hid 82 of `verify.py`'s 177 sites), a site at byte zero is a candidate rather than a drop, every operator is exercised and labelled, the docstring rule answers `is True`/`is False` when it is asked directly, the verdict maps pytest's exit code to survived/killed/killed-import/timeout, timeouts stay out of the score, `--only-line` runs every mutant on a line and refuses a line with none, a dropped site is counted rather than lost, a sandbox that cannot be deleted does not replace the report or any of the three refusals beside it, and the CLI writes both reports, re-runs into a directory it already made, and decides its exit code against `--min-score` inclusively |
| `test_helm_fidelity.py` | 17 | the comparison between the checker's chart render and a real Helm render: pairing by kind and name, the block scalar's final newline, the path of the first difference, exit 2 when Helm is absent, and — where Helm is installed — the end-to-end comparison |
| `test_documented_endpoints.py` | 11 | the four `/docket/*` routes the integrations page tells a reader to `curl`, plus the three operational endpoints and a pinned `--port`: each is driven over a real socket against the real boundary, and each must land in the ledger labelled as an agent assertion — a documented endpoint that does not answer is a feature advertised as integrated when it is only written down | the endpoints were documented, and the documentation was the only thing that had ever exercised them |
| `test_packaging.py` | 5 | the installation paths the README promises, executed rather than asserted in prose: `uv tool install` builds an isolated environment and installs a working `docket` command, a clean venv install runs the whole `init` → `run` → `verify` journey, and every install line the README shows is one these tests cover | a wheel that imports cleanly in the repository and fails in a venv is a wheel whose `install_requires` is lying, and `uv` writes its progress to stderr, so asserting on stdout alone reports a successful install as a failure |
| `test_release_workflow.py` | 14 | `.github/workflows/release.yml`, by parsing what it does: the package under test is installed and installed *before* the suite runs, the release job's test dependencies are the CI `tests` job's, the gate runs the `tests` directory rather than a subset, it prints its counts, a tag with no changelog section fails the job, the workflow uploads nothing to PyPI and carries no PyPI credential, the GitHub release uses the run-scoped token, and the workflow fires on a tag rather than on every push, the release version is read out of `pyproject.toml` rather than out of the ref name, a tag that disagrees with the declared version fails the job, and publication uploads onto a release that already exists rather than requiring it to be deleted, the release gate asks for the long propagation bound so it waits for the index to serve the version it uploaded, and the default is short so ordinary runs do not pay for a wait they have no reason to make | the release gate installed test dependencies but never the package, so `tests/test_action.py` and `tests/test_packaging.py` failed with `docket: command not found` — exit 127, which is not a verdict about the tag and reads like one. The gate was red for every tag from the moment the action tests landed, which is the same omission the CI `tests` job had. The release version came from. The gate ran with a 15 s default, so it skipped three install paths on a slow index and shipped a tag with those paths unverified and reported as skipped `${GITHUB_REF_NAME#v}`, so a `workflow_dispatch` run on a branch resolved `main`, looked for `## [main]` in the changelog and failed whatever the maintainer did, and the publication step ran `gh release create`, which exits non-zero when the release exists — measured on v0.2.3, where the tag gate passed, the artifacts were good and only the publication step was stuck
| `test_freshness_workflow.py` | 9 | `.github/workflows/freshness.yml`, by running what it runs: the drift check is the same one CI runs, a clean tree reports exit code 0, a tree with one quoted number wrong reports a non-zero code the workflow can branch on, the report step writes a file `gh issue create --body-file` can read, and no step that opens an issue is unguarded | a scheduled workflow that has never been exercised is the easiest thing in a repository to get wrong, and an issue-opening step that is not guarded on the drift check would fire on a clean tree |
| `test_watch.py` | 14 | `docket watch`, driven against a real supervised run in another thread: the live tail prints each event, the running total grows by exactly each call's cost, a completed run ends on `session.end` without waiting for the quiet timeout, a ledger with no end is reported as quiet rather than polled forever, a torn tail does not stop the events after it, a metered response that cannot be costed is reported rather than omitted, and `resolve_ledger` follows only the directory it was given | a fallback to the process's own working directory would have made `watch` follow a run unrelated to the one asked about, which is worse than saying there is nothing to follow |
| `test_evidence_retention.py` | 48 | retention of the provider responses the boundary saw: the digest is unconditional and the body is not, a truncated body carries both digests and says which is which, a cap of zero or one behaves as written from both sides, the reserved-headroom boundary is pinned at the byte, a write that fails is disclosed on the event rather than swallowed, `docket verify` re-hashes every retained body and catches one that was edited or deleted, and `--redact paths` replaces a recorded path that is relative to the run directory | the retained path is relative to the run directory, so the path *shape* redaction matches on does not catch it and a document meant for circulation kept a filesystem layout; and a redacted copy carried a pointer to a body that never travelled with it, so the pair failed its own `retained-responses` check |
| `test_redact.py` | 24 | `docket report --redact <profiles>`, the redaction path: profiles parse and refuse a typo, paths keep their depth, argv collapses to one placeholder, a secret is replaced wherever it appears, a host and a port both go, every profile on its own leaves a pair that verifies, the originals are byte-identical afterwards, and the CLI refuses when no ledger can be found | the first version restarted the chain at zeroes instead of the ledger's genesis constant, which made every hash in the redacted ledger differ from the one the verifier recomputes; the usage appendix's `merkle_path` was left pointing at the old tree; and the receipt's `file_sha256` was dropped rather than recomputed, so the digest check failed |
| `test_action.py` | 17 | the reusable GitHub Action at `action.yml`, tested by running it: the action's own `run:` blocks are substituted with real values and executed against a real supervised run, a tampered receipt must turn the verify step red, a receipt with a write-scope finding must turn the violation step red, a cap below the first call must produce exit 42 while the step itself still succeeds, and every step's `if:` condition is evaluated so a step that can never run is caught | three real bugs were found this way: annotations were written into `$GITHUB_OUTPUT` where nothing reads them, `set -e` aborted the verify step on a failing `docket verify` so the job went red with nothing naming the failed check, and the `breached` output read a key the receipt does not have so it was always empty |
| `test_action_smoke.py` | 11 | what `.github/workflows/action-smoke.yml` depends on, without a runner: the scripted provider answers the route the boundary forwards to and names a model the price table knows, the smoke agent makes one call and exits 0 and exits 42 when refused and says so when detached, and the workflow really uses the action, passes it a scripted upstream, and asserts the cap path | the smoke agent exits 2 with no base URL, because an agent that silently does nothing when detached is a green job that tested nothing |
| `test_presets.py` | 44 | `docket init --from <preset>`, the seven framework presets: every policy is loaded back through the real `Policy.load`, no preset is looser than the default egress posture, every model pattern a preset names is one the price table can match, the printed command is a real command with the path substituted, and five of the seven are driven end to end through a supervised run against the scripted provider and then verified | the first version of `policy_for` wrote `egress.mode = "allow"`, which the parser refuses, so a preset would have shipped an invalid policy; and Gemini CLI reads `GOOGLE_GEMINI_BASE_URL`, which the supervisor was not injecting, so the one framework that needed the boundary would have talked to Google directly and recorded nothing |
| `test_roadmap.py` | 13 | every checked box in `ROADMAP.md` is a claim about code, so it is held to the code: a path, subcommand, `make` target, CLI flag, policy key, environment variable or dispatched route must exist, a box that names nothing checkable must be excused by name with a reason, and present evidence must not sit in an unchecked box | the Go verifier was listed as `[ ]` while `verify-go/` built clean and agreed on all 132 conformance vectors |
| `test_sync_counts.py` | 14 | `tools/sync_counts.py`, the tool that rewrites every number the documentation quotes about the suite and the version, so a contributor runs `make sync` instead of editing five files: the collect parser reads the shape `pytest --collect-only -q` actually prints, a result that parses to nothing is an error rather than an empty dict, `--check` reports drift without writing, and a rewrite substitutes only the captured group — the first version replaced the whole match and turned `# 1237 tests, fully offline` into `1238` | a repair tool that silently corrupts a sentence is worse than no tool, and the guard that refuses a stale number is only half the story: something has to be able to fix one |
| `test_doc_links.py` | 25 | every `docs/…md` reference in the tree resolves, every page is in the nav, the scanner itself can fail, a staged site copy is not read as a second copy of the documentation, this table's own counts are reconciled against the suite, the counts written beside a test filename in the other pages are reconciled too, the size the README quotes for the whole suite is reconciled, every pytest command the Makefile and the CI jobs ship prints its counts and its skips, no document states a position inside a source list that does not publish one, every row of a graded table carries its grade, every `make` target the workflows and the help text name exists, every flag `docs/cli.md` documents is an option of the command it is filed under, and no document outside the raw measurements carries a path from the machine that wrote it, every source line cited in prose (`verify.py:106`) exists and carries a site the mutation harness would mutate, and the coverage maps have a row for every concept and practice they claim |
| `test_link_check.py` | 5 | the README link check keeps a broken link and an unreachable host apart, against two real loopback servers: one answers 404, the other accepts the connection and closes it without answering, which is what a CDN under load does. A 404 must classify as a status failure, a dropped connection must not classify as a status failure, the two must be recorded in different lists, the connection failure must be retried with backoff rather than settled on the first drop, and an unreachable result must reach the log rather than be discarded | on the v0.2.3 release gate, `img.shields.io` dropped a connection on one badge and the check reported `RemoteDisconnected` under "URLs in README.md that do not answer 200". `RemoteDisconnected` is an `OSError`, and the handler for connection-level errors appended to the same list as the handler for status codes, so a host that never answered was reported as a link that answered wrongly. The tag failed on that, with a package and artifacts that were both correct |
| `test_propagation.py` | 20 | the propagation handling in the packaging tests, exercised rather than reasoned about: the guard reads the simple index rather than the JSON API, the guard waits rather than deciding once, all three wordings for "the index has no such version" are recognised as propagation and a real breakage is not, the install helper retries a propagation failure and then reports it with the real output, the helper does not retry a real breakage or a success, every installer invocation goes through the helper, a version that never appears becomes a skip rather than an endless wait, a version that arrives mid-wait is found, and the real PyPI simple index is asked for a version that exists and for one that never did | the 0.2.4 release gate. The guard found 0.2.4 on the index, un-skipped the clean-venv install, and CI's `pip` then resolved only up to 0.2.3: the probe and the installer had hit different edge caches minutes apart. No pre-flight probe can close that gap, because the guard and the installer are not reading the same cache at the same moment, so the install closes it itself |
| `test_a11y_check.py` | 71 | the site checker against pages built to break each rule it applies, and against pages built to be clean: every rule proves it can fail, every way a link or a control can be named proves it does not cry wolf, and eighteen of them were written to kill survivors of the checker's own mutation sweep (98.5%, `mut-a11y-release.json`) — the ones that assert what the verdict cannot show: a name, its source, a line number, the tree the parser built |
| `test_build_dist.py` | 19 | the release build comes from a git ref and not from the working tree: an untracked file does not leak into an export, a dirty tracked file exports at the committed version, the source digest is stable across two exports of one commit and moves when the commit does, a member escaping the destination is refused, and `--check` refuses artifacts built from another commit or changed since they were built — plus three that hold `PUBLISHING.md` to what it tells the reader to run: every ```bash block parses, no twine command is handed the whole of `dist/` (the guide shipped `twine check dist/*` after the tool began writing `PROVENANCE.json` into `dist/`, and twine refuses a file it does not recognise), and nothing reaches twine before the provenance check | the release build comes from a git ref and not from the working tree: an untracked file does not leak into an export, a dirty tracked file exports at the committed version, the source digest is stable across two exports of one commit and moves when the commit does, a member escaping the destination is refused, and `--check` refuses artifacts built from another commit or changed since they were built, and the guide names `__token__` as the username, a distribution in `dist/` that the record does not name is refused (the previous release's wheel survives a rebuild and the documented upload command then hands PyPI a filename it already holds), and a rebuild clears the distributions it supersedes | the release build comes from a git ref and not from the working tree: an untracked file does not leak into an export, a dirty tracked file exports at the committed version, the source digest is stable across two exports of one commit and moves when the commit does, a member escaping the destination is refused, and `--check` refuses artifacts built from another commit or changed since they were built |
| `test_brand_assets.py` | 11 | the brand kit is wired to the surfaces that render it: every asset the site names is checked in, the theme override still extends `base.html` (it did not, and the home page rendered as a 256-byte fragment), the brand stylesheet is the last one Material emits, no retired colour survives in the tree, every hex in it is from the approved palette, no brand SVG sets its wordmark as live text, the rasters are the shapes their host demands (512 square opaque for the avatar, 1280x640 opaque for the social card, 180 square opaque for the touch icon, and 16/32/48 at 32bpp inside the `.ico`), the site stager still carries `.ico` and `.css` and still skips the in-repository brand note, every asset link in every built page resolves on that page, and `make-assets.py` still renders the PNGs that are checked in byte for byte |
| `test_fuzz_regressions.py` | 98 | one test per defect a fuzzer found (see below) |

Counts are collected (`pytest --collect-only`), not remembered — and "they are checked" is now a
property rather than a claim: `tests/test_doc_links.py::test_the_counted_test_table_in_the_docs_matches_the_suite`
runs the collection and fails on any row that disagrees with it, or on a test file the table has
forgotten. (The sentence here used to say the CI run checked them. Nothing did. A number in a table
is exactly the kind of statement that goes stale silently while every test stays green, which this
repository has already demonstrated once with a conformance vector.)

Two options are not required to use Docket — the core has **zero runtime dependencies** — and the
suite says so by *skipping with a reason* rather than failing, because a bare clone that goes red
looks like a broken project:

The counts in that table come from `pytest --collect-only -q` (how many tests exist) and from a run
with `-ra` (how many passed, and why the rest skipped) — never from a log a script had to guess at.
The release checklist quotes counts collected at a commit rather than a number copied out of a log for
the same reason, and the verbosity trap that makes the difference is recorded under the table.

| installed | result | the skips |
| --- | --- | --- |
| `pytest` alone (`make test`) | 1,106 passed, 47 skipped | `tools/deploy_check.py`'s checks (need PyYAML); the chart comparison reads the chart's `values.yaml` — PyYAML is what these cells lack. The Helm client itself is installed on the machine that ran this matrix (see below); the Ed25519 sealing and signing tests (need `cryptography`) |
| `pip install pytest cryptography` | 1,111 passed, 42 skipped | `tools/deploy_check.py`'s checks (need PyYAML); the chart comparison reads the chart's `values.yaml` — PyYAML is what these cells lack. The Helm client itself is installed on the machine that ran this matrix (see below) |
| `pip install pytest pyyaml` | 1,148 passed, 5 skipped | the Ed25519 sealing and signing tests (need `cryptography`) |
| `pip install -e '.[dev,sign]'` (**the documented dev setup**) | 1,153 passed, nothing skipped | none |

Each cell is a fresh virtual environment with `pip install pytest` plus the extras its row names, then
`pytest tests -ra` from the repository root. All four cells **collect the same 1,153 tests**: what
differs between them is whether a test runs or skips, which is what the last column is for. `-ra` is
what makes that column checkable — every skip is a sentence with its reason, and a cell that skipped
something without saying why would be indistinguishable from a cell that never had the test.

Two measurement defects are recorded here because both produced a *plausible* number rather than an
error. The 2026-10-15 version of this table recorded `0 skipped` in every cell, while the four runs it
summarised had reported their skips as 47, 42, 6 and 1 — the counts had been summed with `paste | bc`,
and `bc` is not installed everywhere; the sum is `awk` now, and each cell's counts are checked against
that cell's own summary line before they are quoted. The second is verbosity, and this page stated it
wrongly until 2026-10-16: it said the counts come from a run with `-ra` and "never a `-q` run's last
line", as if `-q` were the problem. The flag that matters is how many times it is passed.
`pyproject.toml`'s `addopts` already carry one `-q`, so a command line that adds another asks for
`-qq` — at which level pytest 9 prints **no summary line at all**, and a script that greps for one
records zero passes for a suite that passed a thousand. A single `-q` prints it, and that is measured
rather than recalled — `16 passed in 10.26s` beside the progress line of this page's own test file,
`-ra` on top of the `addopts` flag. The extra flag was in the mutation harness's own pytest line, in
the release check's, and in the five commands this repository ships for other people to run — `make
test` and the four CI jobs — where it is quieter and was therefore invisible longer: a green job with
no line in its log saying how many tests passed. All five run `pytest tests -ra` now, and
`tests/test_doc_links.py` reads `addopts` and fails on any shipped command line that adds a second
`-q` or drops `-ra`, so the sentence above is a test rather than a convention.

The skips are not a way of hiding a defect: the checker these 30 cover **exits 2 with a reason**
when PyYAML is absent — "could not run", a different code from the 1 that means "found a problem" —
and `tests/test_deploy_check.py` includes the test that pins exactly that, on a machine that *has*
PyYAML, by shadowing the import rather than uninstalling anything.

**The round tabulated above ran at 2026-10-16T06:45:30Z, and it is the fourteenth measured on this tree.** The
rounds before it are filed rather than overwritten — in the `mutation/` directory of the workspace that
produced these documents, which is not part of this repository — because a matrix someone might check
is evidence and a matrix edited in place is not. Until 2026-10-16T17:15Z the filing was a manual step
taken between rounds, and one round was lost to it: the `16:49:21Z` cells were overwritten by the next
round's own first command, which is why the table below carries their numbers with nothing behind them
but the run's output. The script files the previous round before it clears the directory now, and the
table names every round by the stamp *that round's own summary wrote*, which is also a correction: the
earlier prose on this page labelled three rounds by the time their directory was filed rather than by
the time they ran. The newest, `06:45:30Z`, measured **1,106 / 1,111 / 1,148 / 1,153** with 47 / 42 / 5 / 0 skipped. Every round, and what its machine had:

| round (start, UTC) | tests collected | passed per cell | skipped per cell | the environment, and what the round was |
| --- | --- | --- | --- | --- |
| `08:05:28Z` | 1,024 | 977 / 982 / 1,017 / 1,023 | 47 / 42 / 6 / 1 | the first round kept here; the `+ PyYAML` cell exited 1 on the wall-clock flake in `tests/test_snapshot_restore.py` |
| `08:21:47Z` | 1,025 | 978 / 983 / 1,019 / 1,024 | 47 / 42 / 6 / 1 | the same tree an hour later, the flake not repeating |
| `10:39:50Z` | 1,027 | 980 / 985 / 1,022 / 1,027 | 46 / 41 / 5 / 0 | cells red: the comparison test had one gate and two prerequisites |
| `10:54:56Z` | 1,027 | 980 / 985 / 1,022 / 1,027 | 47 / 42 / 5 / 0 | the gate and the prerequisite both in place |
| `14:06:58Z` | 1,028 | 981 / 986 / 1,022 / 1,027 | 47 / 42 / 6 / 1 | taken with the Helm client **off** the machine |
| `14:45:28Z` | 1,029 | 982 / 987 / 1,024 / 1,029 | 47 / 42 / 5 / 0 | the Helm client back on the machine |
| `15:04:24Z` | 1,031 | 984 / 989 / 1,026 / 1,031 | 47 / 42 / 5 / 0 | the two document guards of that hour added |
| `15:21:56Z` | 1,033 | 986 / 991 / 1,028 / 1,033 | 47 / 42 / 5 / 0 | the release record re-based onto them |
| `16:49:21Z` | 1,077 | 1,030 / 1,035 / 1,072 / 1,077 | 47 / 42 / 5 / 0 | the round whose cells were overwritten: the next round cleared `matrix/` before filing it, so its numbers survive here and in the run's own output and nowhere else. `tools_matrix.sh` files the previous round now |
| `17:15:10Z` | 1,076 | 1,029 / 1,034 / 1,071 / 1,076 | 47 / 42 / 5 / 0 | one test fewer than the round above, because the release-record wiring assertion was moved out of the repository (see below) |
| `17:39:26Z` | 1,079 | 1,032 / 1,037 / 1,074 / 1,079 | 47 / 42 / 5 / 0 | one round older than the round the documents describe |
| `19:09:03Z` | 1,105 | 1,058 / 1,063 / 1,100 / 1,105 | 47 / 42 / 5 / 0 | one round older than the round the documents describe |
| `20:19:09Z` | 1,116 | 1,069 / 1,074 / 1,111 / 1,116 | 47 / 42 / 5 / 0 | one round older than the round the documents describe |
| `06:45:30Z` | 1,153 | 1,106 / 1,111 / 1,148 / 1,153 | 47 / 42 / 5 / 0 | **this round** — the tree these documents describe |

A skip count is only evidence when the environment that produced it is stated, which is why each round
is named and what its machine had is written beside it. All of them describe this machine except the
`14:06:58Z` round, which was taken with the Helm client off it. Every round is four fresh virtual
environments, `pip install pytest` plus the extras the row above the table names, `PYTHONPATH=src`,
`pytest tests -ra` from the repository root, and a separate `--collect-only -q` for the collected
count.

Three of the earlier rounds were quoted with a *derived* number before they ran, and all three times
the derivation was wrong — by one, by two, and by two again: a re-base by hand is a prediction, and the
cells are the measurement. That is the same lesson the table itself records about `paste | bc` — the sum is a
command now, or it is a guess.

The matrix is also where a defect was found, in the same family as the one this page already records
for `tools/deploy_check.py`: the `10:39:50Z` round is the one in which the comparison test showed that it
had **one gate and two prerequisites**. It skipped when the Helm *client* was missing, and with the
client reachable and PyYAML absent — rows 1 and 2 above — it ran and *failed* on the tool's `PyYAML is
not installed` exit 2, a red suite for a setup the README describes. It carries a second gate now, in
the same idiom as `tests/test_deploy_check.py`'s, and the round in which those two cells were red is
kept beside the green one.

## By kind

| kind | how it is done here |
| --- | --- |
| unit | per-module files above; the pure functions (`canon`, `pricing`, `policy`) are tested directly |
| integration | `test_proxy.py` / `test_supervisor.py`: real loopback HTTP, real subprocess, real files |
| system | `docket demo`, `docket selftest` (19 probes against a real boundary in one process) and `docket game-day` (every failure phase, with a post-mortem) — all three run in CI on every push — plus the clean-venv install of the wheel: the whole thing, as shipped |
| acceptance / UAT | the transcripts in the README and `docs/cli.md` are captured from real runs, not written by hand; `tools/transcript_run.py` reproduces them; and the example READMEs are checked against live runs by `tests/test_examples.py`, which asserts the quickstart's own numbers, edits one digit of its ledger, and requires the verification it prints to fail |
| functional | every documented feature has a test named after the claim it makes |
| non-functional | concurrency, locale, fault tolerance and cost; see the other tables |
| smoke | CI installs the wheel into a clean venv and runs `docket demo`, asserting `VERIFIED` appears; the suite also runs the quickstart and the SDK example end to end (`tests/test_examples.py`) |
| sanity | `docket doctor` and `docket selftest` (19 probes) before anything else, when something looks wrong |
| regression | `test_fuzz_regressions.py`: 98 named tests, each reproducing one defect that was found and fixed |
| end-to-end (E2E) | `tools/transcript_run.py`: supervisor → proxy → child agent → ledger → receipt → verifier, offline |
| performance / load / stress | `tools/loadtest.py`, which drives a real supervised run under load (2,000 calls over 8 threads, and a paced soak), plus the game-day `rate-limit` and `ledger-pressure` phases and the concurrency files — numbers below |
| endurance / soak | `tools/loadtest.py --seconds`: a paced run against a real boundary, RSS sampled once a second — ten minutes is the longest run in the suite (`make soak`). Bound in the section below |
| spike | `test_proxy_concurrency.py`: a burst of parallel calls against a one-call cap |
| volume | the game day's ledger-pressure phase grows a ledger to its ceiling and asserts the run stops rather than dropping events |
| scalability | one process, threads rather than workers; the ledger is the only shared resource, serialised deliberately by one append lock — and the load point is where that serialisation becomes a number |
| security | `test_security.py` (red team) + `bandit` in CI + the URL-guard walk + `tests/test_supply_chain.py` (the manifest and the imports agreeing) + `pip-audit` over the development toolchain on a schedule (`.github/workflows/security.yml`): *no known vulnerabilities* as of 2026-10-15 |
| penetration | the red team's forgery cases: a doctored receipt, a doctored ledger, a doctored manifest, a path traversal, a setuid mode, a swapped blob |
| usability | the transcripts are the usability test; a change that makes the output harder to read shows up in the golden files |
| accessibility | two artefacts. The terminal output is plain UTF-8 box drawing with a `NO_COLOR` path, and the machine-readable form (`--json`, the markdown report) carries the same content, so nothing is *only* conveyed by colour. And the **built documentation site** — the only HTML this project publishes — is checked by `tools/a11y_check.py` against ten mechanical rules (language, title, one `h1`, no skipped heading level, a single `main` landmark, `alt` present on every image, an accessible name on every link and every rendered form control, a header cell or caption on every table, no duplicate `id`): `tests/test_a11y_check.py` makes every rule prove it can fail and every path by which a link or a control can be named prove it does not cry wolf. The name precedence is the accessible-name computation of AccName 1.2 §4.3.2 — `aria-labelledby`, then `aria-label`, then a control's `<label>`, then the element's content, then the `title` attribute, which the specification marks as a last resort — and each run prints where the names it accepted came from, with the split in the JSON report (`names_by_source`), so the number a text-only rule would have got wrong is a line in the record rather than a claim beside it |
| compatibility | CI matrix: Python 3.11, 3.12, 3.13; Node 20 for the independent verifier |
| reliability | `chaos.py` + the nine game-day phases; a broken ledger tails off, a full one is declared |
| the practices rather than the kinds | [`docs/sre-coverage.md`](sre-coverage.md): 22 resilience practices — stress, fuzzing, chaos monkey, game days, red teaming, synthetic monitoring, FMEA, rejuvenation, post-mortems (postmortems) — with the status of each |
| maintainability | ruff (lint **and** format, **pinned** to one version in both the `dev` extra and CI: the formatter rewrites different files in 0.17 than in 0.6.9), bandit, and the seam rule in CONTRIBUTING (each claim has one test file) |
| localisation / localization / i18n | `test_i18n.py`: identical digests under `C`, `de_DE`, `tr_TR`, `ja_JP`; NFC ≠ NFD; non-ASCII paths round-trip |
| black-box / white-box / grey-box | black: the CLI and the JS verifier; white: internals such as `_open_ledger_handle`; grey: the red team, which reads the code and then attacks the interface |
| manual | the game-day post-mortem is read by a person; the record it writes is for that person |
| automation | `make check`; CI runs the same on every push |
| exploratory | the defects in "Found by testing" below were found by trying things, not by a plan |
| ad-hoc | `docket chaos --fault …` and `tools/fuzz.py --target …` are the ad-hoc entry points |
| monkey / gorilla | `docket game-day` kills the supervisor with SIGKILL mid-run and asserts what survives |
| boundary-value | every cap: 0, 1, exactly at the limit, one over; every ceiling in `pricing`/`policy` |
| equivalence partitioning | the fault grammar, the policy validators, the URL guards (loopback vs not, scheme by scheme), and the cache key (same body vs differing field, stream vs non-stream) |
| mutation | `tools/mutate.py`: seeded single-site mutation of `canon.py`, `verify.py`, `ledger.py`, `latency.py`, `loaddriver.py`, `cache.py`, `evidence.py` and `resilience.py`, plus the Helm-fidelity tool and the harness's own tests (it scores itself with the file you are reading), with every survivor classified — see *Mutation testing* |
| static | `ruff check`, `ruff format --check`, `bandit -q -c pyproject.toml -r src tools` |
| dynamic | `tools/fuzz.py`: six property-based targets, time-boxed, seeded and reproducible |
| alpha / beta | **not applicable yet** — no release channel beyond the source tree |
| A/B | **not applicable** — nothing here is a user-facing choice to optimise |
| recovery | the game-day `rollback` phase, `test_snapshot_restore.py`, and the torn-tail recovery path |
| cache invalidation | three ways, all counted and all tested: `ttl_seconds` expiry (checked on lookup, entry dropped), LRU eviction at `max_entries`, and the refusal to store a response the provider did not send (error, truncated stream, injected fault). The one that is *policy* rather than mechanism is `max_hits`, which stops the cache answering so the run starts paying again |
| configuration | `test_policy.py` (accept/reject), plus the game day running with mutated policies |
| installation | CI: `pip install --no-index --find-links dist docket` into a fresh venv, then `docket --version` and `docket demo` |
| visual | `test_render_golden.py`: the receipt, the markdown report and the diff, byte-for-byte |
| API | the boundary's HTTP surface (`test_proxy.py`) and the SDK's event API (`test_cli_sdk_demo.py`) |
| database | **not applicable** — there is no database; the ledger is a JSONL file by design (ADR 0001) |

## Measured: throughput, latency and memory

`tools/loadtest.py` generates load the way the boundary is actually used — a supervisor process, a
boundary inside it, a scripted provider behind it, and an agent child in front — and then reads the
evidence back. Its full reports, including the host line, are in
[`docs/measurements/2026-10-15-2core/`](measurements/2026-10-15-2core/README.md); the table here is a
summary of the same files, and every number in it was produced by the command beside it.

| run | shape | calls | wall clock | throughput | client p50 / p95 / p99 | boundary RSS | receipt |
| --- | --- | --- | --- | --- | --- | --- | --- |
| load point | `make load` — 2,000 calls, 8 threads, unpaced | 2,000 | 11.368 s | **175.93 calls/s** | 44 / 48 / 53 ms | +4,188 KiB | verifies |
| soak | `make soak` — 12,000 calls at 20/s for ten minutes | 12,002 | 600.166 s | 20.00 calls/s (paced) | 4 / 5 / 7 ms | +27,180 KiB | verifies |
| soak control | the same rate, half the time | 6,002 | 300.083 s | 20.00 calls/s (paced) | 4 / 5 / 8 ms | +13,720 KiB | verifies |

Three things this table says, and two it does not:

* **The recorder's cost is the write, not the CPU.** Unpaced, 8 threads push
  175.93 calls/s through the boundary on 2 vCPUs, with a client p50 of
  44 ms — an order of magnitude more than the paced run's 4 ms, because under
  concurrency every event waits behind the append lock and its `fsync`. That is the design
  decision in ADR 0005 showing up as the only throughput number the tool has.
* **Nothing drifts for ten minutes.** 12,002 calls, 0 refusals, 0 errors, 0 dropped events, and the
  receipt from the loaded run verifies afterwards with the same checks a quiet run passes.
* **Memory tracks events, not time.** Ten minutes added
  27,180 KiB and half the duration added
  13,720 KiB: the same ~1.1 KiB per recorded event in both, which is
  the in-memory event list the Merkle root and the receipt are derived from — bounded by the run's
  own `max_events` and `max_ledger_bytes` ceilings rather than by a timer.
* **It is not a provider benchmark.** The upstream is scripted and local. Nothing here says anything
  about a real model API's latency, and the reports say so themselves.
* **It is not a load curve.** One concurrency point, one paced rate, one machine. No saturation
  knee, no scaling plot, no claim about another host.

The harness is tested the same way everything else is: `tests/test_loadtest.py` asserts that a
dropped call, a missing latency, a disagreement between the client's count and the ledger's rows, a
receipt that no longer verifies, or a ledger that does not hash to the receipt's own claim all make
a run **not usable as evidence**. A load tool whose output cannot be wrong would be a marketing
statement, not a measurement.

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
| `cache-brake` | a repeated loop (`DOCKET_AGENT_MODE=loop`) against a response cache with `max_hits = 2` | the hits are in the ledger as events, the ceiling is recorded *and* named in the receipt, and the calls after it reached the provider again — the one phase whose subject is a number rather than a failure, so verification recomputes the saving and the spend still describes money actually spent |
| `supervisor-killed` | SIGKILL mid-run | the ledger up to the kill is readable and classified, no receipt is claimed, recovery is stated |

`docket game-day` writes a post-mortem beside the evidence, including expected-vs-actual per
phase, and exits non-zero if a phase misses its own stated expectation. The last recorded full
run: **9/9 phases**, ~20 s, offline.

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
| writing an end-to-end probe for a feature already tested | a response cache handed to the boundary was silently discarded while it was empty — `ResponseCache` defines `__len__`, so `cache or ResponseCache(...)` replaced the caller's enabled cache with the policy-built one, and the boundary did no caching at all. Forty-one unit tests could not see it: every one of them configured the cache through the *policy*, and the defect only appears while the cache is empty. Fixed at both ends and pinned by a regression test that passes a cache as an argument, the way a host application does |
| reading the site navigation | `mkdocs.yml` listed two ADRs twice (0002 and 0003 again after 0005): the pages resolve, so no strict build fails, and the menu simply shows the same entry twice. The link checker now asserts every document is in the navigation |
| the conformance check | `spec/vectors` going stale after a policy change (the digest bound into every event) |
| re-reading the spec against the implementation | `NaN`/`Infinity` are read by Python and rejected by `JSON.parse`: a request body carrying one became `502 docket internal error` in the run's own evidence, a ledger line carrying one was reported as "cannot be canonicalised" instead of "not JSON", and a hand-written `Infinity` on the last line claimed the torn-tail crash-recovery path |
| reading the artefacts the way CI would | `mkdocs build --strict` aborted on this repository — the documentation links to `tools/deploy_check.py` and `tools/loadtest.py`, and the staging step that feeds mkdocs did not copy `.py` files, so the docs job would have failed for anyone who enabled it. The staging step carries them now, and a test stages the tree and resolves every relative link in the docs against it, without needing `mkdocs-material` installed |
| installing the documented dev setup | `pip install -e '.[dev,sign]'` pulled the newest formatted, while CI pins `0.6.9` — and the two disagree about seventeen files of this tree, so a contributor's `make lint` could fail on files CI calls formatted. The extra pins the version CI installs |
| writing a probe for a feature end to end | `docket selftest`'s new cache probe drives two identical calls through a real boundary and found that **a cache supplied to `RecordingProxy` was silently ignored while it was empty**: `ResponseCache` defines `__len__`, so the boundary's `cache or ResponseCache(...)` replaced the caller's configured, enabled object with a policy-built one. Every unit test passed a cache through the *policy*, so none of them could see it, and the failure appeared or vanished with the cache's contents. Fixed at both ends (`is not None`, and `__bool__` so an empty cache is still a cache), with a regression test that supplies one as an argument |
| checking the manifests the way a cluster would | the chart rendered by **Helm itself** (v3.16.3) produced `max_ledger_bytes = 6.7108864e+07` where the static checker produced `67108864`: Helm hands every template a `float64` for each number in `values.yaml`, and Go prints a large one in scientific notation, which TOML reads as a float. Docket's loader accepted it, so nothing in the tree noticed — the divergence was invisible because the checker rendered numbers the way *Python* writes them. The chart's integers now go through `\| int64`, the checker renders values the way Helm does (pinned against a table produced by running Helm), it refuses an integer key that arrives as a float by name, and the two renderers now agree document for document (`deploy/README.md`) |
| installing the package the way a user would | `tools/deploy_check.py` needs PyYAML, and the suite around it *failed* — 28 tests, not skipped — when it was absent, so a bare clone was red while the README promised a green suite (`pip install -e '.[dev]'` did not pull it in either). The same tool also exited `1` when it could not run, which is the code it uses for "found a problem"; its own docstring promised `2`. Both are fixed, and the exit code now has a test that blocks the import rather than uninstalling anything |
| checking the release the way a publisher gets it | the FMEA names `docs/measurements/2026-10-15-2core/load-report.json` as what catches tail latency, that directory's README and `docs/production-coverage.md` name the other reports, and `.gitignore` carried a bare `load-report.json` line — which ignores that name *anywhere* in the tree. The two reports were in the authoring checkout and in no clone: the suite was green where it was written and red the first time the release checks ran against a clone of the bundle instead of the working directory, which is what that check is for. The rules are anchored now, the reports are tracked, and two tests hold the property — one scans every file the documentation names, the other feeds the scanner the rule that shipped and requires it to fail |
| running the dependency matrix with the Helm client installed | `tests/test_helm_fidelity.py`'s end-to-end comparison had one gate and two prerequisites: it skipped when the Helm *client* was missing (`shutil.which("helm")`), and the moment the client became reachable — this round put it on the path the mutation harness's sandbox uses — it ran in the two cells without PyYAML and **failed**, on the tool's own `PyYAML is not installed` exit 2, where every other test in the file asks for a `yaml` fixture and skips with its reason. A user following the README with Helm installed and no deploy extra got a red suite for an environment problem. The same defect `tools/deploy_check.py` had, in the same file family: the test now declares both prerequisites |
| re-reading what the commands the repository ships actually print | `make test` and four CI jobs ran the suite with `-q` on a command line whose `addopts` already carry one — so pytest was asked for `-qq`, where it prints the progress line and no counts line, and the jobs went green with nothing in the log saying how many tests passed. Found while correcting the same defect in the mutation harness's control run and in the release check, where it was load-bearing: a control whose counts are missing cannot be read. Both came from the same sentence on this page, which states the rule that nothing enforced — and the five shipped command lines now run plain `-ra`, with the rule pinned by `tests/test_doc_links.py` |
| running the dependency matrix | `tests/test_snapshot_restore.py` looked the two `docket restore` record documents up by *filename*, expecting the second to carry the `-2` collision suffix. In the `pytest` + PyYAML cell the two commands stamped their records one second apart (`…T081327Z` and `…T081328Z`), so there was no collision, no suffix, and a `StopIteration` on a machine merely slower than the one the test was written on — the matrix's only failure in that cell, and the reason the test now identifies its records by what they say. |

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
  runs. If either check fails, no score is printed. The report also carries that control run's own
  counts — passed, failed, skipped — because *passing* and *running* are different facts: a skipped
  test cannot fail, so a mutant in the region a skip covers is scored as a survivor. The
  2026-10-16 verify sweep is why the counts are there: run under an interpreter without the optional
  `cryptography` extra it skipped the signature tests, and the five mutants in `verify.py`'s
  signature block survived a sweep with nothing left to catch them — 171/177 on the same tree that
  scores 176/177 with the extra installed. The harness cannot tell a deliberate `skipif` from a
  missing dependency; it can refuse to let a reader guess which environment a score was taken in.
- **It mutates a copy**, one mutation at a time, restoring the file between runs.
- **The subset is declared, not implied.** `DEFAULT_TESTS` names the test files that make claims
  about each module. A survivor from a subset that does not cover the module's branches is an
  artefact of the subset, not a gap: verify.py's first wide run reported 23 survivors, four of
  them in the signing branch, and after `test_signing_and_attach.py` was added to the subset and
  *no source was changed* it reported 2.
- **Duplicate keys are not identities.** Two mutants at one line with the same operator (two
  `False -> True` on a single line) are separate mutants: report and compare them as such.
- **The mutant must actually be the code that runs.** A mutation whose replacement has the same
  file size as the original — `0` → `1`, `max` → `min` — and lands within the same clock second can
  be masked by a stale `__pycache__` entry, because CPython's cache key is (mtime seconds, size).
  The harness already runs its subset with `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`
  and copies the sandbox without `__pycache__`, so a *reported* survivor is a real survivor; anyone
  reproducing a survivor by hand has to clear `src/docket/__pycache__` between edits or they will be
  measuring the previous program.
- **The harness has its own tests, and one of them found a bug that had been inflating the
  documentation's numbers.** `tests/test_mutate.py` (40) exercises the tool the way the tool exercises
  the source: a fixture with one of every site kind, the splice checked for being a byte-for-byte
  no-op, every mutant checked for changing exactly one line and still compiling, the six verdicts
  `_run_tests` can return pinned against real pytest runs, and the sampling checked for being a
  function of the seed. The first version of that file asserted a mutant on a line containing an em
  dash and got **none**. The cause was `_span`: `ast` reports `col_offset` as a **UTF-8 byte** offset
  while the splice indexes *characters*, so on any line with a non-ASCII character before the token
  the splice landed inside a character, produced text that did not compile, and the site was dropped
  by a bare `continue`. `splice_only` could not catch it — splicing the original text back is a no-op
  at any offset — and nothing counted the drops, so the effect was invisible: `verify.py` offered 95
  candidate mutants instead of 177 (`ledger.py` 171 instead of 172; every other mutated file was
  unaffected, because their sites sit on ASCII lines). The fix converts both offsets through
  `_char_offset`, and the regression is pinned by a fixture line with an em dash in it. Every score
  in the table below was re-measured afterwards; the reports written before the fix are kept beside
  the later ones (in the `mutation/` directory of the workspace that produced them, which is not part
  of this repository — a report is an observation of the tree it ran against, so it is filed rather
  than overwritten).
- **A site that produces no mutant is counted, not dropped silently.** `mutants_for_source` now
  records one `{"file", "line", "operator", "reason"}` per dropped site, the sweep prints the count,
  and the JSON report and the markdown header carry it. The four reasons are named — the span does not
  lie inside the file, the operator could not be applied, the replacement does not compile, the
  replacement is the original text — and two of them are driven by tests rather than waiting for the
  corpus to produce them. A report that says `0 candidates` without saying why is indistinguishable
  from a clean sweep of a file with nothing to mutate.
- **A single survivor can be re-checked with the harness's own machinery, in seconds.**
  `--only-line 384` runs *every* mutant on that line, sampling nothing, in the harness's own sandbox
  with its own interpreter and environment; the report records the selection so a filtered run is never
  read as a whole-file score, and a line with no mutant is refused rather than reported as a clean
  file. This exists because the honest next step after reading a survivor list is to reproduce one
  verdict, and reproducing it by hand is how a reader measures a stale `__pycache__` — or, as the
  em-dash bug shows, how a reader measures something the harness never mutated at all.
- **A paydown note is a claim, and it can be stale in three different ways.** The tests written to
  close a survivor name the mutant they were written for in their docstring, which is what makes a
  later sweep readable against them — and a named mutant is a line-number index into a file that
  moves, so the claim expires the moment either file does. Re-measuring `resilience.py` after the
  module grew showed two names were false: one test claimed both `return True` mutants of
  `record_failure` while only ever reaching the first, and another believed it killed the two
  `probes_in_flight = 0` assignments, which are unreachable behind the reset in `allow`. The
  2026-10-16 sweep of `verify.py` then showed a third shape, and it is the one worth looking for
  first: the test *did* kill a mutant on its line and stopped one token short of the other. Four
  sites in `verify.py` were that shape — an assertion that ended before the type suffix, a
  fixture where two percentiles were the same number, a boundary whose two spellings part
  company only at exactly three problems, and a sentence nobody had asked for — and each one is
  written out in *The paydown* below, with the receipt it reads. All four are repaired in
  `tests/test_cache.py`, and the residue is one diagnosed equivalent (`:186`). Names are corrected
  here rather than deleted, because a docstring that says "kills X" is how the next reader decides
  whether a site is covered — and the sweep is what keeps the claim honest.
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
| `src/docket/verify.py` | 95 | 95 | 94 | 1 | 0 | **98.9%** ([^prefile]) | 66 s | 42 |
| `src/docket/verify.py` (after the tests below) | 177 | 177 | 176 | 1 | 0 | **99.4%** ([^prefile]) | 2,125 s | 0 |
| `src/docket/verify.py` (after the §8 derived-member checks) | 237 | 237 | 229 | 8 | 0 | **96.6%** | 2,988 s | 0 |
| `src/docket/verify.py` (after the five tests the sweep above prompted) | 237 | 237 | 234 | 3 | 0 | **98.7%** | 2,864 s | 0 |
| `src/docket/canon.py` | 63 | 63 | 58 | 1 | 4 | **98.3%** ([^timeouts]) | 209 s | 42 |
| `src/docket/ledger.py` | 171 | 171 | 143 | 28 | 0 | **83.6%** ([^prefile]) | 276 s | 11 |
| `src/docket/ledger.py` (after the tests below) | 172 | 172 | 144 | 28 | 0 | **83.7%** ([^prefile]) | 788 s | 0 |
| `src/docket/latency.py` | 21 | 21 | 21 | 0 | 0 | **100%** | 12 s | 0 |
| `src/docket/loaddriver.py` (first) | 63 | 60 | 45 | 12 | 3 | **78.9%** | 822 s | 0 |
| `src/docket/loaddriver.py` (after the tests below) | 61 | 61 | 56 | 2 | 3 | **96.5%** | 817 s | 0 |
| `src/docket/cache.py` (first) | 126 | 60 | 49 | 11 | 0 | **81.7%** | 378 s | 0 |
| `src/docket/cache.py` (wide re-run) | 128 | 128 | 120 | 8 | 0 | **93.8%** | 618 s | 0 |
| `src/docket/cache.py` (after the tests below) | 128 | 128 | 128 | 0 | 0 | **100%** | 432 s | 0 |
| `src/docket/resilience.py` (first) | 166 | 60 | 46 | 14 | 0 | **76.7%** | 454 s | 0 |
| `src/docket/resilience.py` (wide re-run) | 166 | 166 | 141 | 25 | 0 | **84.9%** | 877 s | 0 |
| `src/docket/resilience.py` (after the tests below) | 166 | 166 | 163 | 3 | 0 | **98.2%** | 228 s | 0 |
| `tools/helm_fidelity.py` (first) | 47 | 47 | 13 | 34 | 0 | **27.7%** | 28 s | 0 |
| `tools/helm_fidelity.py` (after the tests below) | 47 | 47 | 45 | 2 | 0 | **95.7%** | 41 s | 0 |
| `tools/mutate.py` (after the tests below) | 144 | 144 | 144 | 0 | 0 | **100%** | 342 s | 0 |

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

### The release round

One pass, one commit, one harness: the nine mutated modules of the release round, re-measured
together after the subsets and the source stopped moving, so that the numbers below are
comparable with each other and every one of them names the tree it was taken on. The tenth row
was added the same day, when `tools/a11y_check.py` — the release record's own accessibility
check — was given a subset and swept too; the section *The tenth report* below is that sweep. The parameters are the ones the
earlier rounds used where they are stated (`--min-score 0`, `--seed 0`), and the `control`
column is new: it is the unmutated sandbox's own summary line, before any mutant ran, in
pytest's words. An outcome that did not happen is not written (pytest names only what
happened), so `121 passed` is 121 passed with nothing failed and nothing skipped — and the
report says so independently in its control block's `clean` flag. A subset that skips is a
subset that cannot kill, so a reader can now check the
environment a score was taken in instead of trusting the author's note about it. Every control
in this round skipped nothing, which is itself the evidence that the optional `cryptography`
and `PyYAML` extras were installed: the signature tests and the Helm-fidelity tests are
`skipif`-gated on exactly those. Every report names its own interpreter — here
`3.13.16` — and its own subset, so
the environment a score was taken in is a field rather than a paragraph.

| module | candidates | run | killed | survived | timed out | score | wall clock | seed | control (as pytest wrote it) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `src/docket/verify.py` | 177 | 177 | 176 | 1 | 0 | **99.4%** | 2,127 s | 0 | 189 passed |
| `src/docket/ledger.py` | 172 | 172 | 144 | 28 | 0 | **83.7%** | 802 s | 0 | 121 passed |
| `src/docket/loaddriver.py` | 61 | 61 | 56 | 2 | 3 | **96.5%** | 815 s | 0 | 37 passed |
| `src/docket/canon.py` | 63 | 63 | 58 | 1 | 4 | **98.3%** | 515 s | 0 | 55 passed |
| `src/docket/cache.py` | 128 | 128 | 128 | 0 | 0 | **100%** | 437 s | 0 | 197 passed |
| `tools/mutate.py` | 144 | 144 | 144 | 0 | 0 | **100%** | 384 s | 0 | 40 passed |
| `src/docket/resilience.py` | 166 | 166 | 163 | 3 | 0 | **98.2%** | 250 s | 0 | 110 passed |
| `tools/helm_fidelity.py` | 47 | 47 | 45 | 2 | 0 | **95.7%** | 49 s | 0 | 17 passed |
| `src/docket/latency.py` | 21 | 21 | 21 | 0 | 0 | **100%** | 15 s | 0 | 117 passed |
| `tools/a11y_check.py` | 136 | 136 | 134 | 2 | 0 | **98.5%** | 89 s | 0 | 71 passed |

Every report carries its module's mutable sites, its subset, the control run's counts and the
verdict for every mutant it ran; `provenance.py` re-lists each file's sites and compares them
with the sites the report says it ran, so a report that describes a different file than the one
on disk is caught rather than quoted. The survivors this round leaves are classified under
*Every survivor, classified*.

The round reproduced every score the earlier rounds had reported, and — checked mutant by
mutant, as `(line, operator, from, to)` — every survivor and every timeout as well. That is the
useful part: the numbers quoted above were measured across three days, two environments and a
test suite that grew underneath them, and this round says which of them the current tree still
supports. It also says what the earlier rounds could not, because the evidence is in the report
rather than in a note about it: each subset ran, in full, against the interpreter named in its
own control block.

**The control column found something on its first round.** `tools/helm_fidelity.py` scored
45 of 47 with two survivors and a control run of `16 passed, 1 skipped`, and the skip was the
test that runs the *real* Helm client — the comparison the module exists to make. The reason
was mechanical: the harness gives every test process a fixed `PATH`
(`/usr/bin:/bin:/usr/local/bin`) so that a score cannot depend on the operator's shell, and the
client was on a path outside it. With the client reachable the control run is `17 passed,
0 skipped` and the score is the same 45 of 47 — an unchanged number is not the same fact as an
unchecked one, and a subtitle that says *checked every YAML template* has to be able to say
which client did the checking.
### The paydown

The verifier's corpus is 177 candidates — the number the byte-offset fix revealed — and the first
full round scored 126 of them, because the subset named three test files where the claims live in
four. Naming the fourth moved the score without touching one line of the verifier: **148 of 177
(83.6%)**. The tests written for the survivors that round left moved it again, to **172 of 177
(97.2%)** in a 2,109-second sweep at seed 0. Five survivors remained, which is few enough to read one
at a time — and reading them was the point, because four of the five were tests that had killed one
mutant on their line and stopped one token short of the other. Each stop was a different kind of near
miss.

| site | what the test asserted | what the second mutant did behind it |
| --- | --- | --- |
| `verify.py:934` | `… count: 'seven'` — the value, and not the type suffix beside it | `type(malformed[1]).__name__`: with two malformed rows of the *same* type the sentence reads identically either way, so the assertion could not see whose type was quoted |
| `verify.py:947` | `p50 12 ms · p99 12 ms over 1 measured call(s)` | the fixture measured **one** call, where p50 and p99 are the same number: looking up the wrong percentile printed the same sentence, character for character |
| `verify.py:1140` | `not [failure for failure in result.failures if "further origin problems" in failure]` | the stop condition counts a list, and its two spellings (`> 3` and `>= 3`) print the *same* sentence for every length except one: at exactly three the mutant adds "further origin problems: 0 more" where the original says nothing. The test holds exactly three now — a group naming the wrong model, a token count that does not match the call it cites, and the appendix key that both takes the call out of the cached-response record and stops the tally containing it — and asserts the four sentences that follow, plus the absence of a fifth |
| `verify.py:1008` | that a group whose `hits` is not a positive count is refused | the guard refuses it, and the *tally* loop is a second pass over the same groups that does not skip them: with `int(group.get("hits") or 0)` mutated to `or 1`, the falsy count becomes the ledger's own count, the comparison agrees, and the second sentence about the same group is never written |

All four repairs are in `tests/test_cache.py` and marked in place: the assertion extended to include
`(str)`; a second metered call with a different latency (`extra_latency=40`) so that the two
percentiles differ before one of them can be looked up wrongly; a third origin problem, so the list
holds exactly three — the one length at which the two spellings of the stop condition part company;
and an assertion for the tally sentence, which no test had ever asked for. None of the four needed a
change to `src/docket/verify.py`.

The sweep was then re-run over the file as it stands after those tests **and** after the derived-member
checks (§8 items 5a/5b), which took it from 189 mutable sites to **237**: the first run of that
sweep left `229 killed, 8 survived, 0 timed out — 96.6%`, 2,988 s at seed 0, control run 264 tests.
Eight survivors is a worse number than the last run's one, and the reason is not that the tests got
worse: 189 of those sites did not exist before, and a new check is written before the test that pins
every branch of it.

Each of the eight was then classified, because a survivor count is not a defect list. Five were gaps
in the new code and were closed with tests that name the behaviour — `by_model` members as integral
floats (§3.2 normalises them), the three usage-source counts omitted on a session with no metered
calls (absent is not zero), and a `mixed` basis over rows that are all one kind (a claim that must be
falsifiable). The fifth was the worst of them: `not (reported and estimated)` weakened to `or` makes
"mixed" unfalsifiable for exactly the case it exists for. Three are equivalents, demonstrated rather
than argued: the two bool/`None` fast paths produce the same status *and the same sentence* for
`true` and `null` (`verify.py:106` in `whole`, `verify.py:723` in the `by_model` normaliser), and the
`breakdown_judged` flag at `verify.py:763` sits behind `if recomputed is not None` — a row whose cost
is not an amount fails `cost-recomputation` and the breakdown block is never entered, which is shown
by probing both shapes and observing no `by-model-totals` verdict at all.

The re-run, with those five tests in place, is the number this page quotes above:
**`234 killed, 3 survived, 0 timed out — 98.7%`**, 2,864 s at seed 0, control run 271 tests, and the
three survivors are the three equivalents. A score is a statement about a corpus of changes, not about
correctness — 98.7% means "of 237 single-site changes to this file, 234 were noticed by something".



The general lesson is narrower than "write more tests" and worth stating: a docstring that names a
mutant is a claim about *that* mutant, a line can carry two of them, and an assertion that is right
about the sentence it checks can still be blind to the half of the sentence it stopped before.

### Every survivor, classified

A survivor is either a gap or an equivalent mutant. Each one is listed here with which it is and
why; an equivalent mutant is not a defect and no test pretends otherwise. The three `verify.py`
rows below are that file's entire survivor list in the run this page quotes (98.7%), and the five
survivors the same sweep found before those tests existed are listed in the next section with the
tests that closed them. Every other file's rows come from the run that produced them.

| site | mutant | verdict |
| --- | --- | --- |
| `verify.py:106` | `isinstance(value, bool) or value is None` → `and` | **equivalent.** `whole(True)` fails as a bool on one path and as the unparsable string `"True"` on the other, and `whole(None)` likewise; both routes record the same named failure with the same text. |
| `verify.py:723` | the same expression in `quiet_whole`, the `by_model` normaliser | **equivalent.** Probed with `by_model[0].calls` set to `true` and to `null`: the mutant and the original both report `by_model['claude-sonnet-5'].calls is not a whole number`, character for character. |
| `verify.py:763` | `breakdown_judged = True` → `False` in the arithmetic-failure branch | **unreachable.** The branch that sets it is inside `if recomputed is not None`; every input that could raise there (a non-numeric token count, a cost that is not an amount) fails `cost-recomputation` first and returns `recomputed = None`, so the derived-member block never runs — probed with both shapes, and `by-model-totals` is absent from the report in each. |
| `canon.py:51` | `or` → `and` in the early return for bool/`None`/str | **equivalent.** The fallback at the end of `_normalise` returns those three types unchanged as well; the early return is a fast path, not a behaviour. |
| `canon.py:139`, `canon.py:160` | `len(level) > 1` → `>=` and `1` → `0` in the `while` loop of `merkle_root` and of `merkle_proof` | **non-terminating, all four.** Each turns a one-node level into a level that is promoted to itself, so the tree never finishes. They exceed a 45 s per-mutant timeout (the same sites exceeded 120 s in an earlier run): reported as timeouts, never as kills, because a hang is not the suite noticing a wrong answer. |
| `ledger.py:153,160,161,168` | `compare=False` → `True` on the clock, the two handles and the append lock | **equivalent.** Nothing compares two `Ledger` objects — the field option is declared so that a repr and a comparison cannot reach into a descriptor, and the *repr* half of the same declaration is killed by `test_the_repr_of_a_ledger_is_safe_to_put_in_a_log`. |
| `ledger.py:168` | `init=False` → `True` on the append lock | **equivalent.** A lock passed to the constructor changes nothing; nothing constructs a ledger that way. |
| `ledger.py:215` | `if size > 0` → `>=` | **equivalent.** For a zero-byte file the guarded block reads the file and extends the event list with nothing. |
| `ledger.py:277` | `getattr(self._handle, "closed", False)` → default `True` | **equivalent.** The attribute exists on every file object a ledger can hold; the default is reachable only for an object that is not a file. |
| `ledger.py:301` | `self.max_bytes or 0` → `or 1` | **equivalent (unreachable).** This expression is inside `LedgerFullError`, raised only when a ceiling is set, and `_room_for` never refuses when both ceilings are `None`. |
| `ledger.py:369` | `__exit__` returning `False` → `None` | **equivalent.** The `with` statement tests truthiness; both let an exception through. |
| `ledger.py:410` | the diagnostics skeleton's `trailing_newline: True` → `False` | **equivalent.** Every path that reads bytes overwrites it; it is readable only for a file that does not exist, where neither value is a claim about bytes. |
| `ledger.py:632-646` | six mutants: the `and`/`not` of the stale-lock test (`:628`), `unlink(missing_ok=True)` in the stale branch (`:629`) and in the release path (`:646`), and the two `return`s of the stale branch (`:630`, `:633`) | **five unreachable here, one equivalent.** `:628`–`:633` sit inside `_acquire_lock`'s `try: # pragma: no cover - Windows` block, past the `fcntl` early return, so on any platform with `flock` they cannot run at all — the POSIX equivalents are killed. `:646` does run here, and its `missing_ok=False` is equivalent because a lock file that is already gone raises `FileNotFoundError`, an `OSError` the line above suppresses. |
| `loaddriver.py:216` | `if next_call > now` → `>=` | **equivalent.** At `next_call == now` the mutant calls `time.sleep(0.0)` where the original skips the call; a zero-length sleep does not move the schedule. |
| `loaddriver.py:265` | `while time.monotonic() < due` → `<=` | **equivalent in effect, and stated precisely.** The outer loop samples RSS on a fixed cadence; this inner loop only waits until the next sample is due, sleeping in 20 ms steps. The mutant takes one extra step when the clock reads *exactly* `due` — a nanosecond-level coincidence — and the only consequence is that the next sample can be up to 20 ms later than it would have been. No sample is added or lost, and the sampling quantum is 20 ms, so the series a soak reports is the same series. |
| `loaddriver.py:130,132,133` | `return False` → `True` on the call budget and on the deadline, and `issued += 1` → `+= 0` | **non-terminating, all three.** Remove the guard that stops a run and the run does not stop, so the test that drives it never returns within the 45 s per-mutant timeout. Reported as timeouts, never as kills — same family as the `merkle_root` mutants above. |
| `ledger.py:676-672` | ten mutants in the Windows process-liveness probe `_pid_alive`: the two `WinDLL`/`OpenProcess` arguments (`:664`, `:665`), the two `if not …` guards (`:666`, `:670`), what each guard returns in both spellings (`:667`, `:671` — the value swapped, the `return` replaced by `None`), `return code.value == still_active` replaced by `None` (`:672`), and that comparison's `==`→`!=` (`:672`) | **unreachable here.** The whole block is under `if os.name == "nt"`, which asks the process table directly instead of calling `os.kill` — on Windows that *is* `TerminateProcess`, so the liveness probe would kill the process holding the lock. The *Windows* bullet under *Not covered, and why* says what that costs. |
| `resilience.py:76,151,161` | `probes_in_flight = 0` → `1` in the dataclass default, in `record_success` and in `record_failure` | **equivalent, and the file's own test says why.** `allow` sets `probes_in_flight = 0` on the transition from `open` to `half-open` — that is the site the test kills — so the value these three sites leave behind is overwritten before anything reads it. What the test pins instead is the behaviour a reader cares about: an upstream that recovered is admitted again on the *next* outage, not only the first. |
| `helm_fidelity.py:53` | `if spec is None or spec.loader is None` → `and` | **unreachable, and marked as such.** The guard carries `# pragma: no cover`; `spec` is `None` only when `spec_from_file_location` fails, and for a file that exists the loader it returns is never `None`. Reaching the branch needs an interpreter whose import machinery returns a loaderless spec. |
| `helm_fidelity.py:105` | `zip(left, right, strict=True)` → `strict=False` | **equivalent.** The line above returns a length mismatch before the `zip` is built, so the two lists are always the same length by then and `strict` can never fire. A belt kept on after the braces. |
| `ledger.py:690` | `except (ValueError, OverflowError)` → the two mutants of its `return False` | **unreachable on this kernel.** `os.kill` reports a pid no kernel would hand out as `ESRCH`, which is an `OSError` and is handled (and killed) by the branch above; this guard is for the platforms that raise `OverflowError` instead. |
| `a11y_check.py:289` | `return []` → `return None`, the fallback of `_native_text_alternative` | **equivalent.** Its only caller is `if native:` — `None` and `[]` are both falsy and the caller never iterates the result, so no page can distinguish them. Deleting the fallback would not help: an early `return None` on every path is the same mutant wearing a different line number |
| `a11y_check.py:132` | the parser's sentinel node `Node("root", {}, 1)` → line `0` | **equivalent.** The sentinel is not an element any rule reports — every rule walks `root.descendants()`, which starts at the children — so its own line number is read by nothing. It is a marker for the stack's floor, and `Node.line` is a field the sentinel has to fill |

[^prefile]: Measured before the `_span` byte-offset fix in `tools/mutate.py` (see *Mutation
    testing*): the candidate count for `verify.py` and `ledger.py` here is lower than the number
    of mutable sites those files actually hold (95 of 177, and 171 of 172), so these scores
    describe a smaller corpus than the rows below them. The numbers are kept as they ran.

[^timeouts]: All four mutants in this family failed to terminate within the 45 s per-mutant
    timeout. The score is over the 59 mutants that produced a verdict (63 candidates minus the
    four timeouts); a timeout is reported beside the score, never folded into it.

### The tenth report: the checker's own sweep

`tools/a11y_check.py` reads the published site and is the release record's sixteenth check, so it was
swept on 2026-10-16 like the machine it runs in: **136 candidates, 134 killed, 98.5%**, no timeouts, a
subset of its own tests, a control run of `71 passed`, filed as `mut-a11y-release.json` — and every
number in this section can be read from that file. It was the *third* sweep on this file: the first
scored **67.1%** and the second **91.9%**, and neither of those two reports exists any more, because
each sweep writes to the same path and the next one replaced it. The 47 survivors they left are what the
repairs below were read from; the paragraph is their record now, and the score that survives with its
evidence beside it is the 98.5%.

| the sweep found | what was wrong |
| --- | --- |
| no fixture used a self-closing tag | `<img … />` goes through a different parser handler than `<img …>`, and both handlers' mutants lived: a self-closing image kept its attributes (or lost them) with nothing to notice |
| nothing asserted a *line* on a page with no landmarks | `mains[0].line if mains else 1` and its two siblings could return the second element's line, or `0`, and every test still passed |
| the name precedence was read from the verdict | a link with text and a tooltip passes whether it is named by the text or by the tooltip, so three mutants in the tally and the precedence lived for a whole round |
| one fixture had one control per name source | the counter's *lookup* key could become the second character of the source name and the tally would still be right on every page in the file |
| the parser's end-tag search floor | dropping the floor from `range(len(self._open) - 1, 0, -1)` left an outermost element open and nested everything after it inside — the rules still ran, on the wrong tree |
| `--build` was tested with a site that did not exist | a mutant comparing the build's exit code to `1` exits 2 for the *missing site's* reason, so the test passed for the wrong cause; and nothing ran a *successful* build to kill the inverted comparison |

Three findings were defects in the tool rather than holes in its tests, and all three were deleted-wrong
code rather than missing checks. The document title was captured by a `_capturing_title` flag whose two
branches the sweep proved unreachable — `handle_data` keeps only the first captured text, and a page's
`<title>` always precedes its other text — so the title is read from the finished tree now. A form control
was named by its own content, which WAI-ARIA forbids (a control's name sources are the author's): a
wrapped `<textarea>` was being named by the text inside it. And a wrapping `<label>` fell back to the whole
wrapper's text when it had none of its own, which names a control after its own value.

The harness found the fourth defect itself, in a test written minutes earlier: the new
`test_the_default_site_is_the_one_this_repository_builds` asserted `ROOT.name == "docket"` — true of this
workspace, false of every clone made into a differently-named directory. The sweep refused to run:
*sandbox does not pass the suite before any mutation (1 failed, 64 passed)*. A harness that scores mutants
against a baseline that does not pass is measuring nothing, so the refusal is the feature, and the test now
asserts where it is in the tree instead of what the tree is called.

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
| a test for a surviving `max` → `min` | the existing rate test **passed with the mutant applied**: it measured total wall clock through a *cold* boundary, where the first call's own cost cleared the 0.4 s threshold, so the soak's central claim ("the load is paced") was pinned by nothing. It now warms up first, asserts the schedule's floor, and compares against an unpaced run. |
| a surviving sentinel comparison | `note_rss` compared a bare `-1` against a bare `0` in the same expression, and both mutants survived. The answer is now a named constant (`UNMEASURABLE`) with a test that feeds the sampler the sentinel and a real reading. |
| mutating the harness and asking it a question its own call site cannot | `_is_docstring`'s four guards and its two `return`s — six mutants — survived because the one place that *calls* the function has already established what they check: it is reached only from the `--include-strings` branch, where the node is a non-empty string constant. Raising the guard's lower bound from byte zero to byte one was worse than unkillable-and-harmless: a site at the first byte of a file was silently *dropped*, and a drop is reported as "a site that produced no mutant", never as a survivor. Both are now asked directly. |
| treating `rmtree(ignore_errors=True)` as strict | four mutants, all in the sweep's cleanup. The cleanup runs in `finally:`, so a `PermissionError` there would replace whatever the run was about to report — the report, a broken baseline's refusal, a refused splice, or a refused line filter — with a traceback about a temporary directory. A test under mutation can leave exactly that shape behind (a read-only directory is a normal thing for a test to create), so a fixture tree now contains one, and four tests assert that each exit still reaches the reader. |
| a survivor in a *liveness probe* | `os.kill(pid, 0)` → `os.kill(pid, 1)`: the probe answers the same question and sends `SIGHUP` while asking it — the same defect class as the Windows `os.kill`, which *is* `TerminateProcess` there. The existing test could not see it, and the reason is a property of the measuring environment: every sweep runs under `nohup`, which ignores `SIGHUP` and passes that disposition to its children, so the signal is a no-op where the score is measured. The new test makes *delivery* observable instead of fatal — the child installs a handler and creates a marker file inside it. |
| reading 51 survivors of `verify.py` | they were not gaps in the verifier and not equivalent mutants: `DEFAULT_TESTS` gave the file three test paths, and the block that recomputes a *stated* cache saving (`cache-savings`, `cache-hit-origins`, `cache-hit-proofs`) is exercised from `tests/test_cache.py`, which the list did not name. A subset is part of the measurement: the same 177 candidates scored 71.2% against three files, 83.6% once the fourth was named, and 97.2% after the tests those survivors asked for — and the five left after *that* are the round recorded in *The paydown* above, four of them tests that stopped one token short. The list now names `tests/test_cache.py`, first, with the reason beside it. |

Five of the eight survivors the 237-site sweep first reported were gaps in code that had been
written hours earlier, and each is closed by a test that names the behaviour: an integral float in
`by_model` (§3.2 normalises it — `verify.py:723`), the three usage-source counts omitted on a
session with no metered calls, where `-1` is the only thing separating "absent" from "stated as
zero" (`verify.py:835`), and a `mixed` basis over rows that are all one kind, weakened to
`or` so that the claim is unfalsifiable in exactly the case the sentence exists for
(`verify.py:877`). The worst of them is the last: it is not a crash and not a wrong sentence, but a
**pass** where the receipt's own classification is contradicted by its rows. They were found by
mutation after the tests were green, which is the only reason they were found at all.

### Not claimed

A mutation score is a statement about the mutants a tool generated, over one subset, at one
commit. It says nothing about the defects no mutant represents: a missing feature, a wrong
decision, an interface nobody needed. 83.7% on `ledger.py` with every survivor explained is not
"the ledger is 83.7% correct"; it is "of 172 single-site changes to this file that this harness
produced, 144 were caught by these tests, 28 are provably equivalent or unreachable here, and
none is unaccounted for". The same sentence applies to the verifier with its own numbers:
98.7% is not "the verifier is 98.7% correct"; it is "of 237 single-site changes to
`verify.py` that this harness produced, 234 were caught by these tests and 3 are
argued equivalent, with the argument written out in the table above".

## Not covered, and why

- **A full mutation score over the whole source tree.** `tools/mutate.py` has been run over nine
  files — the two whose promises are cryptographic (`canon.py`, `verify.py`), the one that holds the
  evidence (`ledger.py`), the measurement added on top of them (`latency.py`), the load driver
  (`loaddriver.py`), the response cache, the resilience primitives, and two of the tools themselves
  (`helm_fidelity.py` and `mutate.py`, the harness scoring its own tests). `deploy_check.py`,
  `loadtest.py` and `fuzz.py` are mapped in `DEFAULT_TESTS` — each with the test file that would kill
  its mutants — but not yet measured, and a mapping is not a score. The rest of `src/docket` is not
  mutated: a score for `proxy.py` or `cli.py` would be a much larger run, and a number nobody has run
  is not a number. The harness's
  per-module test subsets name the files each module's promises are made in, including the invariant
  suite for the lifecycle modules, so a future run of `supervisor.py` or `scriptagent.py` starts from
  a subset that can actually fail. What *has* been run, and what every survivor turned out to be, is
  in *Mutation testing* below. A tool is in that list only when its own tests exist: a harness with
  no tests is a harness whose numbers nobody can check.
- **Endurance beyond ten minutes, and at scale.** `make soak` runs a real boundary for ten
  minutes at a paced 20 calls/s and shows memory tracking events rather than time (≈1.1 KiB per
  event, both at 6,002 and at 12,002 calls). That is a bounded soak on one 2-vCPU host: it cannot
  speak to hours, to a slower filesystem, or to the ten-million-event ledger whose in-memory cost
  the same arithmetic predicts (~11 GB, against `max_ledger_bytes` as the ceiling that stops it).
- **Throughput as a curve.** There is one measured point, not a curve: 8 threads unpaced on 2
  vCPUs, where the append lock and its `fsync` are the bottleneck. No requests-per-second target
  is claimed for a production agent, no tokens/s number exists, and no concurrency sweep has been
  run — a single point is what the reports contain and all they are used for.
- **Live providers.** CI never calls Anthropic, OpenAI or Google. The wire format is exercised
  against a scripted upstream; a provider-side change to a router, a header or a usage field
  would be caught by a user, not by CI. `--online` exists for that, and is deliberately not
  automated.
- **A local attacker racing Docket.** The confinement tests cover a hostile *document*. A
  process that can race the restore between its check and its write, on a tree the operator has
  already handed to an autonomous agent, is out of scope and named as such in the threat model.
- **Accessibility beyond the terminal.** There is no GUI to test. The `NO_COLOR` path, the
  monochrome golden files and the markdown report are what "nothing is only conveyed by colour"
  means here. The documentation site is the one HTML artefact and `tools/a11y_check.py` decides ten
  mechanical questions about it; it is a static check, not a substitute for a person using a screen
  reader on a real page, and WCAG 2.2 conformity is not claimed here. Its name computation implements
  the precedence of AccName 1.2 §4.3.2 and not the whole algorithm: no CSS generated content, no shadow
  roots or slots, no `aria-hidden` exclusion inside a name, no full flat-string normalisation, and no
  role-based prohibition of naming (safe here because only links and form controls are asked, and both
  allow naming). It also has no equivalent of a contrast check, a focus-order check or a
  keyboard-traversal check — the four things a person with a screen reader would notice first if they
  were wrong.
- **A suite that has been proved to hold under heavy CPU contention.** Under artificial load —
  two busy loops on a two-core box, a third process's worth of oversubscription — `test_chaos.py`'s
  rollback drill failed once: it drives a whole game-day phase as a subprocess with a wall-clock
  budget, and it prints the phase's record when it fails, which is how the next occurrence would
  name the field that moved. It did not reproduce in three attempts under the same load, and the
  unloaded suite is green on every run recorded here, so this is written down as an observed
  sensitivity rather than a diagnosis. The pacing test was rewritten for the same reason after a
  run at load showed its margin was 5%: it now asserts the schedule's floor, which contention can
  only move in the safe direction.
- **Windows.** There is no Windows in this environment or in CI, so nothing Windows-specific is
  claimed: the `O_EXCL` lock fallback, the `OpenProcess` liveness probe and the process-group kill
  are written, are the reason those code paths are marked `# pragma: no cover`, and have never
  been executed here. The lock fallback's liveness probe killed a live process until this round
  (see *Mutation testing*); a Windows contributor's first run is the one that will exercise it.
