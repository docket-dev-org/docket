# Custom agent example: your own code, recorded

The boundary (`docket run`) sees HTTP traffic. It cannot see *what a call meant* — that the POST
was a retrieval step, that the file read was a config lookup, that the agent decided to skip a
tool because the answer was already cached. Only your agent knows that, so Docket gives the agent
a way to say so, and records the difference.

```console
$ PYTHONPATH=src python3 examples/custom-agent/agent_sdk.py
```

Or under the supervisor, where the session **attaches** to the run's ledger instead of opening
its own:

```console
$ PYTHONPATH=src python3 -m docket run --max-usd 0.10 -- python3 examples/custom-agent/agent_sdk.py
```

## What it demonstrates

| | |
| --- | --- |
| `with docket.Session() as session:` | opens a session, writes `session.start` and the policy into the ledger |
| `session.record_model_call(...)` | a model call the agent made **through the boundary** — the SDK adds the meaning, the boundary supplies the observed usage |
| `session.record_tool(...)` / `record_note(...)` | an assertion by the agent, labelled `reported_by: "agent"` in the ledger |
| `session.would_exceed(usd=…)` | a pre-flight check against the policy, so an agent can degrade gracefully instead of being killed |
| `session.docket_number` | what the run is called, and the identifier its events are filed under |
| `session.ledger_path` | where the evidence is — or `None` when a supervisor owns the ledger, which is the honest answer rather than a path this session does not write to |
| `session.attached` | whether this session attached to a supervised run or opened a ledger of its own |

## Testimony vs evidence

This is the part that matters, and it is why the two paths exist:

- **Boundary events** (`model.request`, `model.response`, `fs.change`, `net.allowed`, …) are
  observations: the supervisor wrote them because it saw the bytes and the syscalls.
- **Agent events** (`tool.call` from the SDK or the HTTP endpoints, `note`, and any
  `model.response` posted through `/docket/event`) are claims. They are recorded with
  `reported_by: "agent"` so a reader can weigh them accordingly — and in an attached session,
  they are labelled again on the receipt's usage rows (`asserted_by: "agent"`) if they carry
  cost.

An agent can lie to its own instrumentation. A receipt that hides that would be misleading;
this one does not, and the verification output says which is which.

## Production shape

Inside `docket run`, the session detects `DOCKET_PROXY_URL` and `DOCKET_SESSION` and writes
through the boundary's `/docket/event` endpoint — one writer per ledger, so the hash chain stays
valid. Standalone, it owns its ledger and writes its own receipt. Same API either way; check
`session.attached` if your code needs to care.

Two consequences of the attached case are worth stating, because code that ignores them crashes:

- `session.ledger_path` is `None`. The supervisor owns that file, so there is no path this session
  can honestly name. It used to be `session.ledger.path`, which raised `AttributeError` the first
  time anyone ran the example under `docket run` — the command this README has always advertised.
  `tests/test_examples.py` now drives both paths, so the crash cannot come back unnoticed.
- `session.receipt` is `None`. The supervisor writes the receipt for the whole run, and it commits
  to a ledger this session never read. Verify `.docket/latest.receipt.json` from the run, not one
  this session could have written.

```python
with docket.Session() as session:
    session.record_tool("sql.query", {"sql_digest": digest_of(sql)}, rows=len(rows))
    if session.would_exceed(usd="0.25"):
        session.record_note("stopping early: the next step would breach the cap")
        break
```

Verify what you produced, with either implementation:

```console
$ docket verify .docket/receipts/DKT-*.receipt.json
$ node verify-js/verify.mjs .docket/receipts/DKT-*.receipt.json
```
