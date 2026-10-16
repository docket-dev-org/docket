# Security policy

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/docket-dev/docket/security/advisories/new)
on this repository. If you cannot use it, email `security@docket.dev`. Please include: version
(`docket --version`), platform, the smallest reproduction you have, and whether the issue lets
an agent escape a cap, forge a receipt, or read data Docket promised not to record.

**Response targets** (best effort, stated so you can hold us to them): acknowledgement within
72 hours, an assessment within 7 days, and a fix or a documented mitigation within 30 days for
issues that break a documented guarantee. Credit in the advisory unless you prefer otherwise.

## What counts as a security issue here

Docket makes narrow, checkable claims. Breaking one of them is a security issue:

| claim | class of bug that breaks it |
| --- | --- |
| a cap is enforced at the boundary | a call reaches a provider after the cap was crossed; a cap check that a model name, header or request shape can bypass |
| a receipt is tamper-evident | any edit, truncation, reordering or extension of a ledger that `docket verify` still reports as verified |
| cost is recomputable | a usage path where the recorded cost and the token counts disagree under the embedded price table and verification still passes |
| limits are disclosed truthfully | a receipt that omits, weakens or contradicts `limits_disclosure` |
| secrets are not recorded | an API key, authorization header, prompt or completion content appearing in a ledger or receipt when it was not explicitly requested |
| the boundary is not a backdoor | the local proxy exposing an endpoint that lets a non-supervised process write arbitrary events into another session's ledger, or read another session's data |
| a restore is confined | `docket restore` (or a rollback) writing or deleting anything outside the tree it was pointed at, choosing that tree from a forged receipt, following a symlink out of the tree, applying a setuid mode, or overwriting a file that changed after the run without `--force` |
| a snapshot does not sweep up secrets | a watched symlink whose target is copied into the snapshot store, or a watched root outside the working directory captured under a path a restore could be aimed with |
| Docket's own fetches stay local | a `urlopen` in the package reaching `file://`, a cloud metadata address, or any host that is not this machine when the address came from the environment (`DOCKET_PROXY_URL`, the self-test probes) |
| an agent-side URL is never a file read | an `urlopen` handed a scheme other than http/https in code that stands in for an agent (`demo.py`, `scriptagent.py`) |

Ordinary bugs (wrong output, a crash, an unhelpful message) are normal issues, not advisories.

## Not in scope

- **Escaping the boundary by design.** An agent pointed at a provider URL directly, or
  superuser, or traffic outside the supervised process tree, is outside what Docket claims —
  this is documented in [`docs/threat-model.md`](docs/threat-model.md) and stated in every
  receipt.
- **Filesystem writes that violate `deny_write`.** Detection and reporting is the documented
  behaviour; prevention is not claimed (a sandbox is the right tool, and the docs say so).
- **Resource exhaustion of the recording process itself** by a supervised agent with the same
  privileges — an agent that can kill Docket can also edit its files; Docket's answer is
  verifiability, not tamper-proofness against root.
- **A snapshot you asked for containing sensitive content.** `--snapshot` copies the *contents*
  of the watched tree into `.docket/snapshots/`, by design: that is what makes a rollback
  possible. What it excludes — symlinks, roots outside the working directory, anything matched
  by `[files] ignore` — is recorded in `not_covered`. Everything else in the watched tree lands
  in the store, so watch a directory that does not contain credentials you would not commit, and
  use `ignore` for `.env`, key material and caches.
- **An attacker racing a restore** between its confinement check and its write, on a directory
  the operator has already handed to an autonomous process. The confinement rules are aimed at
  hostile documents (see [ADR 0004](docs/adr/0004-content-snapshots-and-confinement.md)).
- **Prompt-injection consequences inside an agent.** Docket records what an agent did; it does
  not judge whether it was wise.
- Denial of service against a *consumer* of receipts (a 10 GB receipt is a bug report about
  limits, unless it also bypasses a documented limit check — then it is an advisory).

## Supply chain posture

- Zero runtime dependencies in the core (`dependencies = []`), so the installable surface is
  the standard library. `[sign]` adds `cryptography` and only for Ed25519 signatures.
- CI runs lint, the full offline test suite on Python 3.11–3.13, the independent Node
  verifier against the committed conformance vectors, and a clean-venv install + demo smoke
  test.
- Releases are built from a tag in CI; the sdist and wheel are attached to the GitHub release
  with a signed `SHA256SUMS`. The repository is expected to be protected with required
  reviews and signed commits.
- `docket` never phones home. There is no telemetry, no update check, no analytics, no account.
  The only addresses Docket itself dials are loopback (its own boundary, its probes) and the
  provider URLs the *user* configured — because a supervised agent asked it to.
- Every `urllib.request.urlopen` in the repository passes its URL through one of two guards
  before the call, and that is checked rather than promised: `tests/test_url_guards.py` walks
  the package's syntax tree and fails if any `urlopen` argument cannot be resolved back to a
  guard, so a call site added later without one breaks the build. The guards are
  `util.local_http_url` (scheme http/https, a host must be present, and the host must be this
  machine — `127.0.0.0/8`, `::1`, `localhost`; `0.0.0.0` is a bind address and is refused as a
  destination) for Docket's own clients, and `util.http_url` (scheme only) for code that stands
  in for an agent and may legitimately be pointed at a remote provider. The test covers both,
  including the hostnames that are designed to look local and are not: `localhost.evil.example`,
  `localhost@evil.example`, `127.0.0.1.evil.example`, `2130706433`, `0x7f000001`.
- Dependencies are scanned on a schedule (`.github/workflows/security.yml`), not only on a pull
  request: `pip-audit` audits the whole development and CI toolchain against the advisory databases,
  and a failure there means the *toolchain* has a known vulnerability, which should not appear as an
  unrelated red build on somebody's change. As of 2026-10-15 it reports no known vulnerabilities.
- The claim above about dependencies is *checked, not repeated*: `tests/test_supply_chain.py` reads
  `dependencies = []` out of `pyproject.toml`, pins the three extras by name, and then walks the
  syntax tree of every shipped module (`src/`, `tools/`, `spec/`, `examples/`) to assert that the
  only imports outside the standard library are `cryptography` and `yaml` — two imports, in two files,
  both optional, both behind a guard so that a clean install can still import `docket` and record a
  capped run. An import that a user's environment does not have is the failure a dependency scanner
  cannot see, and it is the one this test exists to catch.
- Static analysis runs in CI: `bandit -q -c pyproject.toml -r src tools`. Suppressions are
  class-by-class with a written reason in `[tool.bandit]`, never blanket; the URL rule (`B310`)
  is not suppressed at the call sites — it is answered by the guards above.

## Supported versions

Until 1.0, the latest minor release receives security fixes. After 1.0, the latest two minors
and the previous major, for a stated window announced at release (the long-term-support policy
is one of the funded deliverables in [`SPONSORS.md`](SPONSORS.md)).
