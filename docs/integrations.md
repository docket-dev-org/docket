# Integrations

Docket wraps a process, so it works with agents it has never heard of. Everything below is a
variation on one of three mechanisms:

1. **Provider base-URL variables** — most SDKs and CLIs honour them (`--profile full` sets the
   common set).
2. **`HTTP_PROXY`** — `--http-proxy` routes all HTTP(S) traffic through the boundary, so even
   agents that hard-code URLs are covered (subject to `[egress]`).
3. **The `/docket/*` endpoints** — for agents that want to record *business* events (tools,
   notes) that the boundary cannot observe by itself.

If an agent does none of these, Docket records the process lifecycle and filesystem changes and
says plainly in `limits_disclosure` that model traffic was not observed. That is a worse
receipt than a complete one — and much better than a receipt that pretends.

---

## Claude Code / Claude Agent SDK

```bash
docket run --max-usd 5 --label "issue-412" -- claude -p "fix the failing parser test"
```

`ANTHROPIC_BASE_URL` is injected, so the session's API calls cross the boundary. Model names in
the ledger are what the API reports. On subscription (Pro/Max) plans, use
`--budget-mode quota` and cap on tokens/calls/wall-clock instead of dollars:

```bash
docket run --budget-mode quota --max-tokens 2_000_000 --max-minutes 45 -- claude
```

## Codex CLI / OpenAI SDK agents

```bash
docket run --max-usd 5 -- codex exec "add a /version endpoint with a test"
```

`OPENAI_BASE_URL` and `OPENAI_API_BASE` are both set, which covers the CLI and the Python/Node
SDKs. Chat-completions and responses-style endpoints are handled; streaming responses are
tallied when usage arrives in the stream (or estimated from the response body when the provider
does not report it, with `usage_source = "estimated"` and a receipt that says so).

## Gemini CLI

```bash
docket run --max-usd 5 -- gemini -p "summarise the changes in this repo"
```

`GEMINI_API_BASE_URL` is injected, and the model is parsed from the request *path*
(`/v1beta/models/<model>:generateContent`), because Gemini does not put it in the body.

## aider, llm, and other OpenAI-compatible tools

```bash
docket run --max-usd 3 -- aider --message "make the logging structured" --no-auto-commits
docket run --max-usd 1 -- llm -m gpt-6.1 "explain this traceback"
```

Anything that lets you point at a base URL will work. For local models (Ollama, llama.cpp,
vLLM) Docket still records calls and tokens; the bundled table prices self-hosted families at
zero marginal cost, with a note that this is not the whole cost of running them.

## Agents that ignore base URLs: `--http-proxy`

```bash
docket run --max-usd 10 --http-proxy -- ./scripts/agent.sh
```

`HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY` are exported; `NO_PROXY` keeps loopback traffic
local. Model hosts are always permitted (recording them is the point); everything else is
subject to `[egress]`. TLS traffic passes through as a tunnel: Docket records the *host and
decision*, not the payload, and says so in the receipt.

## Your own agent — Python SDK

```python
import docket

with docket.Session(max_usd=2.00) as s:          # runs under `docket run`, or standalone
    s.record_model_call(model="claude-sonnet-5", input_tokens=1200, output_tokens=340)
    s.record_tool("search", {"q": "invoice totals"}, result_bytes=41_000)
    s.record_note("phase 2: writing files")
    if s.would_exceed(usd="0.50"):
        s.record_note("stopping: next step would exceed the remaining budget")
        break
```

Inside a supervised run (`docket run -- … python agent.py`) the session detects the boundary
through the environment and **attaches**: every event goes over HTTP to `/docket/event`, so the
supervisor remains the only writer of the ledger file and the chain stays valid. Outside a
supervised run it opens its own ledger and writes its own receipt — useful when the agent *is*
the process you control, and the case where its claims are the whole record.

```python
with docket.Session() as s:
    print(s.attached)     # True under `docket run`, False standalone
    s.record_note("…")    # either way, labelled as an agent assertion
```

Records made through the SDK are marked as *asserted by the agent* (`reported_by: "agent"`)
rather than observed at the boundary. That distinction survives into the receipt: a reader can
see which parts of the record the agent claimed and which parts the boundary witnessed.

## Any language — the HTTP recording endpoints

```bash
# a tool call
curl -X POST "$DOCKET_PROXY_URL/docket/tool/call" \
     -H 'content-type: application/json' \
     -d '{"name":"sql.query","args_digest":"9f86d081884c7d659a2feaa0c55ad015","result_bytes":2048}'

curl -X POST "$DOCKET_PROXY_URL/docket/tool/result" \
     -d '{"name":"sql.query","ok":true,"result_bytes":2048}'

curl -X POST "$DOCKET_PROXY_URL/docket/note" \
     -d '{"message":"batch 3 of 12 finished"}'

# any event in the vocabulary (docs/concepts.md), including model calls your agent
# made outside the boundary — recorded as an assertion, with provenance preserved
curl -X POST "$DOCKET_PROXY_URL/docket/event" \
     -d '{"type":"model.response","data":{"model":"gpt-6.1","provider":"openai",
          "usage":{"input_tokens":1200,"output_tokens":340},
          "usage_source":"reported","cost_usd":"0.005800"}}'
```

Arguments and payloads are recorded as **digests and byte counts**, not contents: the record
proves the same call happened twice without keeping a second copy of your data.

## CI

```yaml
name: agent
on: [workflow_dispatch]
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      # Before the 0.1.0 release: pip install git+https://github.com/docket-dev/docket.git
      - run: pip install docket
      - name: Run the agent under a cap
        run: docket run -p .docket/ci.toml --label "${{ github.run_id }}" -- python agent.py
        continue-on-error: true
      - name: Verify the receipt and keep the evidence
        run: |
          docket verify .docket/latest.receipt.json --ledger .docket/runs/*.jsonl --json > verification.json
          cat verification.json
      - uses: actions/upload-artifact@v4
        with:
          name: docket-evidence-${{ github.run_id }}
          path: |
            .docket/**
            verification.json
      - name: Fail the job on a policy violation
        run: |
          test "$(python -c 'import json;print(json.load(open("verification.json"))["ok"])')" = "True"
```

A policy that suits CI: `on_breach = "kill"`, a modest `max_usd`, `deny_write` on `.env` and
lockfiles you did not mean to touch, and `[egress] mode = "deny"` with an allow-list of the
registries and APIs the job actually needs.

## Kubernetes / containers

Run the agent and Docket in the same container (or the same pod with a shared volume for
`.docket`): Docket needs the same filesystem view as the agent to snapshot it, and loopback.
The receipt is a file — collect it with whatever you already use for artifacts. There is no
sidecar protocol to speak and no service to run.

## Framework hooks

- **LangChain / LlamaIndex**: callbacks can forward usage to `record_model_call`; without that,
  the boundary still records the traffic if the framework honours base URLs.
- **Claude Agent SDK / OpenAI Agents SDK**: same — the boundary sees the HTTP calls.
- **Custom orchestrators**: put `docket run -- …` around the whole orchestrator and use the
  HTTP endpoints for step-level events.

## What does not work (and why)

- Agents that bypass the boundary: a directly configured provider URL, or an embedded client
  with a hard-coded host, in a run *without* `--http-proxy`. The receipt states the boundary's
  scope; a missing record is not the same as a quiet run.
- Model calls made by a *different* machine (your agent delegating to a hosted service that
  calls the model). Nothing local can see those; the receipt says so rather than inventing a
  total.
- Subscription plans in `usd` mode: there is no per-token bill, so `max_usd` is inert. Use
  `--budget-mode quota` with token/call/time caps, and read the receipt's `cost_basis`.
