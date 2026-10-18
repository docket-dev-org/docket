# CI example

A pipeline that runs an agent, keeps the evidence, and gates on the receipt being verifiable.

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

      # Until 0.1.0 is on PyPI, install from the repository:
      #   pip install git+https://github.com/docket-dev-org/docket.git
      - run: pip install docket

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

## Verifying without installing Docket

If you want the verification step to be independent of the tool under test (a reasonable
instinct), check out this repository at a pinned version and run the Node verifier:

```bash
node verify-js/verify.mjs .docket/latest.receipt.json --ledger .docket/runs/*.jsonl
```

That is the point of the format being specified and vector-tested: the producer and the
verifier are allowed to be different programs by different people.
