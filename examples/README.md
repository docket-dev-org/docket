# Examples

Runnable, offline, small. Each one is a complete path from "an agent runs" to "a receipt someone
else can verify".

| example | shows |
| --- | --- |
| [`quickstart/`](quickstart/) | the smallest possible run: a policy, a six-line agent, a verified receipt. No network, no API key |
| [`ci/`](ci/) | gating a pipeline on a receipt, and uploading it as evidence |
| [`client-deliverable/`](client-deliverable/) | producing something you can hand a client: markdown summary + receipt + ledger, and how they verify it |
| [`custom-agent/`](custom-agent/) | recording tool calls and notes from your own agent, in Python and over HTTP |
| [`subscription/`](subscription/) | `--budget-mode quota` for plans that have no per-token bill |

Run any of them from the repository root:

```bash
PYTHONPATH=src python3 examples/quickstart/run_example.py
```

Every example prints the commands to verify what it just produced, including with the
independent Node verifier (`node verify-js/verify.mjs …`).
