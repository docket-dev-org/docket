# Command-line reference

```
docket [--version] {run,verify,report,explain,diff,demo,init,prices,doctor} …
```

Exit codes are part of the interface, because scripts and CI depend on them:

| code | meaning |
| --- | --- |
| `0` | success (for `verify`: the receipt checks out) |
| `1` | not verified — `docket verify` only |
| `42` | the supervised run was stopped because a cap was crossed |
| `43` | usage or policy error (bad policy file, nothing to run, missing receipt) |
| `70` | internal error (a bug — please report it with the ledger) |
| `130` | interrupted by `SIGINT` (Ctrl-C is forwarded to the supervised process) |

---

## `docket run [flags] -- <command> [args…]`

Runs a command under a policy, records everything observable, and writes a receipt.

| flag | default | meaning |
| --- | --- | --- |
| `-p, --policy PATH` | none | policy TOML. With no policy: no caps, everything observable recorded |
| `--docket-dir DIR` | `.docket` | where `runs/`, `receipts/`, `latest` live |
| `--cwd DIR` | current directory | working directory for the child |
| `--profile {full,minimal}` | `full` | how many provider base-URL variables to inject (`minimal` sets only OpenAI/Anthropic) |
| `--http-proxy` | off | also export `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`, so *all* HTTP traffic can be recorded and subject to `[egress]` |
| `--record-bodies` | off | store request bodies in the ledger. Off by default because prompts are the most sensitive thing a user has |
| `--prices PATH` | bundled table | price table for this run |
| `--seal` | off | seal the receipt with a local HMAC key (`~/.docket/seal.key`, created on first use) |
| `--sign KEY_PATH` | off | sign the receipt with an Ed25519 private key (requires `docket[sign]`) |
| `--label TEXT` | none | human note stored in the receipt (e.g. the ticket number) |
| `--report {summary,full}` | `summary` | what to print to stderr when the run ends. The receipt is written either way |
| `--max-usd`, `--max-tokens`, `--max-calls`, `--max-minutes` | policy | per-run overrides |
| `--on-breach {kill,deny}` | policy | per-run override |
| `--budget-mode {usd,quota}` | policy | per-run override |

Environment injected into the child (in addition to provider base URLs):

```
DOCKET_PROXY_URL      http://127.0.0.1:<port>/        # for /docket/* recording endpoints
DOCKET_SESSION        the session id
DOCKET_DOCKET_DIR     where the ledger is being written
DOCKET_POLICY_DIGEST  digest of the policy in force
DOCKET_TOOL_VERSION   the version producing the record
```

Provider variables injected with `--profile full`: `OPENAI_BASE_URL`, `OPENAI_API_BASE`,
`ANTHROPIC_BASE_URL`, `GEMINI_API_BASE_URL`, `DEEPSEEK_BASE_URL`, `MISTRAL_BASE_URL`,
`OPENROUTER_BASE_URL`, `GROQ_BASE_URL`. An agent that honours none of these still works via
`--http-proxy`, or by calling `/docket/*` directly — and one that bypasses all of them is
recorded as *missing*, not as *nothing happened*: the receipt's `limits_disclosure` states the
boundary's scope, and the session event records which variables were set.

## `docket verify <receipt.json> [flags]`

Verifies offline. Exits `0` if it checks out, `1` if it does not, `2` on usage/IO errors.

| flag | meaning |
| --- | --- |
| `--ledger PATH` | also check the ledger file the receipt came from (chain, roots, file digest, re-binding of usage rows) |
| `--seal-key PATH` | HMAC key file, to check a sealed receipt |
| `--public-key HEX` | expected Ed25519 public key; without it, a signature is checked only against the key embedded in the receipt |
| `--json` | machine-readable result (same `checks[]` array, with `status` of `pass`/`fail`/`skip` and a `detail`) |

Output contract: every check is `PASS`, `FAIL` or `SKIP` **with a reason**. `SKIP` never counts
as a pass; a verifier that reports coverage it does not have is worse than no verifier.

## `docket report [receipt] [--markdown]`

Renders the newest receipt (or the one you pass) as a human summary. `--markdown` emits a
version suitable for a pull request, incident ticket or client email — the shape people
actually need when they have to explain an agent run to someone else.

## `docket explain [receipt] [--ledger PATH] [--json]`

Analyses a ledger and prints findings — where the money and the risk went — each with
severity, confidence (`deterministic` / `heuristic`), attributed USD impact, the event
sequence numbers it is based on, and a remedy. `--json` emits the same as data.

Findings currently implemented (thresholds live in `src/docket/explain.py` and are visible in
the output): runaway loops (repeated byte-identical requests), retry storms, repeated context
billed uncached, oversized context calls, oversized tool payloads, denied calls, deny-write
violations, premium-model leakage on small calls, and estimated-usage warnings.

## `docket diff <left.receipt.json> <right.receipt.json> [--json]`

Compares two receipts: spend, calls, tokens, models, tools, files, findings. The intended use
is *drift detection* — did last week's prompt change double the cost, did the model swap move
p95 calls to a pricier tier, did a new tool start returning huge payloads.

## `docket demo [--max-usd N] [--tamper] [--keep] [--json]`

Runs the scripted runaway agent against an in-process fake provider: **offline, no API key,
deterministic**. `--tamper` then edits one byte of the ledger and shows verification failing at
the exact event. `--keep` leaves the workspace (with its ledger and receipt) on disk and prints
the path. `--json` emits the run summary as a single JSON object.

## `docket init [--path docket.toml] [--name NAME] [--max-usd N] [--force]`

Writes a commented starter policy. Refuses to overwrite unless `--force`.

## `docket prices [--path PATH] [--json]`

Prints the price table with its `as_of` date, per-family source notes and the table digest —
the same digest that appears in every receipt produced with it. If your invoice disagrees with
this table, you want `--prices` on `docket run`.

## `docket doctor [--docket-dir DIR]`

Checks the local environment: Python version, Node (for the independent verifier), docket
directory writability, price table load, optional signing support. First line of support triage.

---

## Recipe: CI

```yaml
- run: pip install docket
- run: docket run -p .docket/ci.toml --label "${{ github.run_id }}" -- python agent.py
  continue-on-error: true      # let the report step run even when the cap fires (exit 42)
- run: docket verify .docket/latest.receipt.json --json > verification.json
- uses: actions/upload-artifact@v4
  with: { name: docket-evidence, path: ".docket/**" }
```

`docket verify` exits `1` on a receipt that does not check out, so a pipeline can gate on
evidence rather than on trust in the pipeline itself.

## Recipe: a client deliverable

```bash
docket run --max-usd 20 --label "acme-migration" --report full -- ./agent.sh
docket report --markdown .docket/latest.receipt.json > evidence.md
docket verify .docket/latest.receipt.json --ledger .docket/runs/xxxx.jsonl
# ship evidence.md + the receipt + the ledger; the client can verify with:
node verify-js/verify.mjs DKT-*.receipt.json --ledger xxxx.jsonl
```
