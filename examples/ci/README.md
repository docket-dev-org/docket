# CI example

A pipeline that runs an agent, keeps the evidence, and gates on the receipt being verifiable.

There are two ways to do this, and the second is the one most people want.

## The reusable action

The repository ships a composite action at [`action.yml`](../../action.yml). One `uses:` line
installs Docket, runs the agent under a policy, verifies the receipt offline, fails the job on a
policy violation, and uploads the evidence — including from failed jobs.

```yaml
name: agent
on: [workflow_dispatch, pull_request]

jobs:
  agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: docket-dev-org/docket@v0.1.8
        with:
          command: python3 agent.py
          policy: examples/ci/docket.toml
          label: ${{ github.run_id }}
```

The action reports, as step outputs you can assert on or pass to a later job:

| Output | What it holds |
| --- | --- |
| `receipt` | Path to the receipt, or empty when none was written |
| `exit-code` | The agent's own exit code. `42` means the cap fired |
| `spend` | Recorded spend in USD, as the receipt states it |
| `calls` | Model calls the boundary forwarded |
| `verified` | `true`, `false`, or `skipped` |
| `cap-exceeded` | How many caps the run exceeded, as the receipt counts them |

Inputs worth knowing:

| Input | Default | Why you would change it |
| --- | --- | --- |
| `policy` | `docket.toml` | Point at a policy somewhere else |
| `version` | `0.1.8` | Pin a different release. `latest` is not reproducible |
| `upstreams` | *(none)* | `NAME=URL` per line, to point the boundary at a gateway or a scripted provider |
| `fail-on-violation` | `true` | Turn off when a violation is a warning in your pipeline |
| `verify` | `true` | Turn off only when another step verifies independently |
| `upload-evidence` | `true` | Turn off when your own artifact step handles it |

A run whose cap fires exits `42`, and the action reports that as a `::notice::` rather than failing
the job. Exit `42` is the boundary working: failing the pipeline there would teach people to raise
caps instead of reading receipts.

The action is exercised on every push by
[`.github/workflows/action-smoke.yml`](../../.github/workflows/action-smoke.yml), against the
scripted provider below — a real supervised run, a real verification, and the cap path on demand.

## Without the action

If you would rather write the steps yourself, this is the same pipeline spelled out. It is also the
clearest statement of what the action does.

```yaml
name: agent
on: [workflow_dispatch, pull_request]

jobs:
  agent:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }

      # docket is on PyPI as docket-ai; the command it installs is `docket`:
      - run: pip install docket-ai

      - name: Run the agent under a policy
        run: |
          docket run -p examples/ci/docket.toml --label "${GITHUB_RUN_ID}" \
            -- python agent.py
        continue-on-error: true          # exit 42 (cap fired) is a result, not a crash

      - name: Verify the receipt and the ledger, offline
        run: |
          docket verify .docket/latest.receipt.json --ledger .docket/runs/*.jsonl --json \
            | tee verification.json
          python - <<'PY'
          import json, sys
          report = json.load(open("verification.json"))
          skipped = [c for c in report["checks"] if c["status"] == "skip"]
          print(json.dumps({"ok": report["ok"], "skipped": [c["name"] for c in skipped]}, indent=2))
          sys.exit(0 if report["ok"] else 1)
          PY

      - name: Fail the job on a policy violation
        run: |
          python - <<'PY'
          import json, sys
          receipt = json.load(open(".docket/latest.receipt.json"))
          violations = [f for f in receipt["findings"] if f["rule"] in {"write-scope", "policy-denials"}]
          for finding in violations:
              print(f"::error::{finding['title']} — {finding['detail']}")
          sys.exit(1 if violations else 0)
          PY

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: docket-evidence-${{ github.run_id }}
          path: |
            .docket/**
            verification.json
```

Why this shape:

- **`continue-on-error: true` on the run step.** Exit `42` means the *cap worked*. Failing the
  pipeline there would teach people to raise caps instead of reading receipts. The following
  steps decide what is actually a failure: an unverifiable receipt, or a policy violation that
  matters to you.
- **Verification as its own step with `--json`.** The `checks` array includes `skip` entries
  with reasons; a pipeline that only looks at `ok` cannot tell "signature not checked" from
  "signature valid". The snippet prints what was skipped, which is where surprises hide.
- **Evidence uploaded with `if: always()`.** The run you need to explain is the one that went
  wrong; artifacts are most valuable from failed jobs.
- **`deny_write` on lockfiles and `.env`.** The most common unattended-agent damage is not
  spend, it is a mutated lockfile or a committed secret. Those become findings; the step above
  turns them into a red X.
- **`on_breach = "kill"`.** In CI nobody is watching the meter, so the safe default is to stop.

The policy in [`docket.toml`](docket.toml) is a usable starting point: `$2.00`, 60 model calls,
15 minutes, premium models denied, egress allow-listed.

## Trying it with no API key

Two files here make the pipeline runnable without a credential:

- [`fake_provider.py`](fake_provider.py) serves one fixed OpenAI-shaped response and prints its URL.
- [`agent.py`](agent.py) makes exactly one call through whatever base URL the environment names.

```bash
# Requires: Python 3.11 or newer, and nothing else.
python3 examples/ci/fake_provider.py &     # prints a URL, then serves
docket run -p examples/ci/docket.toml --upstream openai=http://127.0.0.1:<port> \
  -- python3 examples/ci/agent.py
```

[`capped.toml`](capped.toml) sets a cap below the first call's cost, so the boundary refuses it and
the run exits `42`. That is the path the smoke workflow exercises on demand.

## Verifying without installing Docket

If you want the verification step to be independent of the tool under test (a reasonable
instinct), check out this repository at a pinned version and run the Node verifier:

```bash
node verify-js/verify.mjs .docket/latest.receipt.json --ledger .docket/runs/*.jsonl
```

That is the point of the format being specified and vector-tested: the producer and the
verifier are allowed to be different programs by different people.
