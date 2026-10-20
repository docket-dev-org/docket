# Contributing to Docket

Thanks for considering it. Docket is deliberately small: a supervision boundary, a ledger, a
receipt format and the documentation around them. Contributions that make those four things
clearer, more correct or more portable are the ones that matter most.

## The two rules that matter

1. **Nothing in this project may require a network call, an account or a paid dependency.**
   Docket is used in places where agents burn money and where evidence has to circulate
   offline. A feature that only works with connectivity is a different project.
2. **Anything that goes into a hash is a format change.** Canonical JSON, event shape, digest
   inputs, the receipt layout — changing any of them breaks every receipt ever produced. See
   [Changing the format](#changing-the-format).

## Getting started

```bash
git clone https://github.com/docket-dev-org/docket
cd docket
python3 -m venv .venv && . .venv/bin/activate      # or any 3.11+ interpreter
pip install -e '.[dev,sign]'   # dev: pytest, ruff (pinned), mkdocs, pyyaml — sign: cryptography
make test          # 1399 tests, fully offline, ~2-4 min on 2 vCPU
make demo          # the runaway-agent demo
make verify-js     # independent Node verifier against the committed vectors
```

There is no service to start and no fixture to download. If a test needs a provider, it uses
`docket.fakeupstream`, a scripted OpenAI/Anthropic-shaped server that runs in-process.

## Where to make a change

| you want to… | start here |
| --- | --- |
| add an analysis rule ("findings") | `src/docket/explain.py` + `tests/test_explain.py`; each rule needs a `rule`, `severity`, `confidence`, `usd_impact`, `evidence` and `remedy` |
| support another provider/framework | `src/docket/proxy.py` (routing, usage extraction) + `docs/integrations.md` |
| add a policy field | `src/docket/policy.py` (`DEFAULT_TOML` is the reference policy), `docs/policy-reference.md`, and a test for both accept and reject paths |
| write a verifier in a new language | `spec/docket-receipt-1.md` §8 is the checklist; `verify-js/` is the worked example; add the language to the table in `docs/verification.md` |
| improve output | `src/docket/report.py`; keep the terminal rendering readable at 80 columns and `--json` strictly machine-readable |
| fix the price table | `src/docket/prices.json` with a source URL and `as_of` date — never an unsourced number |

## Changing the format

Format changes are the highest-cost change in this repository, so they have a fixed process.
A pull request that touches canonical form, event shape, digest inputs, the Merkle
construction or the receipt layout must:

1. update `spec/docket-receipt-1.md` (the normative text) — including a rationale;
2. regenerate `spec/vectors/` with `make vectors` and commit the new bytes;
3. update every implementation in the repository (Python and `verify-js/`) so both reproduce
   the vectors;
4. add a `CHANGELOG.md` entry under **Format** and bump the format tag if the change is
   incompatible;
5. state the migration story for receipts already in circulation. "There isn't one" is an
   acceptable answer only if it is written down.

A change that leaves the vectors untouched but alters a shared function is a bug, not a
feature — CI runs both implementations against the vectors on every pull request.

## What CI runs, and what it blocks

`.github/workflows/ci.yml` runs nine jobs on every push to `main` and every pull request:
`lint (ruff)`, `tests (python 3.11)`, `tests (python 3.12)`, `tests (python 3.13)`,
`package (clean install, then run it)`, `conformance (independent verifier, node)`,
`verify-go (a third implementation, written from the spec alone)`,
`deploy artefacts (static: no container runtime is used, and none is pretended)`, and
`docs (mkdocs build --strict: no broken links, no stranded pages)`.

`main` requires all nine, and requires the branch to be up to date before it merges. A red job
blocks the merge rather than decorating it. The `docs site` and `Security` workflows run on
their own triggers and are not part of that set, because a path-scoped or scheduled workflow
that does not run on a given push cannot honestly be required on it.

## Tests

- Offline, deterministic, no API keys, no sleeps longer than necessary. Timing-sensitive
  tests use injectable clocks (`Ledger.in_memory(clock=…)`).
- A bug fix comes with a regression test that fails before the fix. The float rule in
  canonical JSON exists because three bugs got through without one.
- Verifier changes need a *negative* test: hand it a modified ledger and assert it notices.
- **Concurrency changes need a test that runs the thing concurrently.** The boundary is a
  `ThreadingHTTPServer`, so "two calls at once" is the normal case, not an edge case:
  `tests/test_ledger_concurrency.py` and `tests/test_proxy_concurrency.py` fire 6–8 threads at
  the real code. Assert a *bound* where a race decides the outcome (at most one paid call per
  in-flight request, never an exact admitted count), and assert **every request has an outcome**
  — a driver that only catches `HTTPError` lets a thread die silently and turns a lost request
  into a passing test. See `docs/adr/0005`.
- Run `make check` (lint + tests + Node conformance) before pushing.
- What is covered, by kind, and what is deliberately *not*: [`docs/testing.md`](docs/testing.md).

## Style

- `ruff check` and `ruff format` (config in `pyproject.toml`, 120 columns); imports sorted.
  The `dev` extra pins ruff to the version CI installs, because its formatter changes between
  releases — see the comment beside the pin. `make lint` checks formatting without writing it.
- Type annotations on public functions; `from __future__ import annotations` at the top.
- Comments explain *why*, especially where a decision looks strange. The honest-limits
  wording, the float rule and the "skip is not a pass" behaviour all have reasons worth
  preserving in prose.
- No new runtime dependencies. If a feature truly needs one, it goes behind an optional
  extra and the core must keep working without it.

## Commit messages and sign-off

Conventional-ish prefixes (`feat:`, `fix:`, `docs:`, `spec:`, `test:`, `chore:`) with a short
imperative subject. Meaningful commits, not one per keystroke; squash freely.

Contributions are accepted under the [Developer Certificate of Origin](https://developercertificate.org/)
— add `Signed-off-by: Your Name <you@example.com>` (`git commit -s`). No CLA, no copyright
assignment: your contribution stays yours, licensed under Apache-2.0 like the rest.

## Recognition

Contributors are listed in `CONTRIBUTORS.md` (generated at release time) and in release notes.
If your contribution is used in a downstream product, say so in
[Discussions](https://github.com/docket-dev-org/docket/discussions) — the "who uses this" list is
the most useful thing a young project can have.

## Review expectations

Maintainers aim to triage within a week and to give a real answer rather than silence. If a
pull request is going in a direction the project will not take, you will get an explanation
with the reasoning, not a closed tab.

## Security

Do not open a public issue for a vulnerability. See [`SECURITY.md`](SECURITY.md).
