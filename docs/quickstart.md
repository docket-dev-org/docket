# Quickstart

Five minutes from nothing to a verified receipt. No API key, no account, no network — the demo
provider is scripted and local, so this works on a plane.

## 1. Install

Until the 0.1.0 release lands on PyPI, install from the repository — a clone, or the wheel
attached to the release:

```bash
git clone https://github.com/shivanshjain456/docket && cd docket
python3 -m pip install .          # or: pipx install .  (keeps it out of a project venv)
docket --version
```

No install at all also works, from inside the clone:

```bash
PYTHONPATH=src python3 -m docket demo
```

`pip install docket` starts working once 0.1.0 is published. Nothing above needs an account,
an API key, or the network after the clone — the demo provider is scripted and local.

## 2. Watch a runaway agent get stopped (20 seconds)

```bash
docket demo
```

You will see an agent loop on an expensive model, cross a `$2.00` cap, get refused at the
boundary, and be terminated (exit `42`). Docket then prints findings — the loop, the deny-write
violation, the uncached context — and verifies the receipt it just wrote.

Then:

```bash
docket demo --tamper      # edits one byte of the ledger; verification fails at that event
docket demo --keep        # keep the workspace and print the path so you can poke at the files
```

## 3. Bound a real agent

```bash
cd ~/your-project
docket init                                   # writes docket.toml (commented, edit freely)
docket run --max-usd 5 -- claude -p "fix the failing test in src/parser.py"
```

Or with any command, in any language:

```bash
docket run --max-usd 5 -- ./your-agent --flag value
docket run --max-usd 5 --http-proxy -- ./build.sh     # everything through the boundary
```

What you get back is one line per run (numbers from your run, not this page):

```console
docket: DKT-20261013-A03507 completed · $0.004510 of $5.00 cap · 3 calls · receipt .docket/receipts/DKT-20261013-A03507.receipt.json
```

A run that crosses a cap ends the same way, with a different verb:

```console
docket: DKT-20261013-746EF4 stopped by policy (max_usd) · $2.00 of $2.00 cap · 15 calls · receipt .docket/receipts/DKT-20261013-746EF4.receipt.json
```

(Exit code `42` in that case — see [cli.md](cli.md).)

## 4. Read what happened

```bash
docket report                 # human summary of the newest run
docket report --markdown      # the same, as markdown for a ticket or a client
docket explain                # findings: where the money went, with evidence
docket explain --json         # the same as data
```

Each finding names the ledger events it rests on, and says whether it is
`deterministic` (arithmetic over events) or `heuristic` (a model of what could have happened).

## 5. Verify the receipt offline

```bash
docket verify .docket/receipts/DKT-*.receipt.json --ledger .docket/runs/*.jsonl
```

Exits `0` if it checks out, `1` if it does not — usable in CI directly.

To prove the point that the format is not this tool, verify with the independent Node
implementation instead (standard library only, no dependencies). It ships inside the wheel,
so this works from an installation and not only from a clone:

```bash
docket verify --impl node .docket/receipts/DKT-*.receipt.json
```

Both implementations are bound to the same committed conformance vectors, which also ship
inside the wheel. If you are writing a verifier in another language, start at
[`../spec/docket-receipt-1.md`](../spec/docket-receipt-1.md) §8 and use those vectors as your
test suite.

## 6. Record your own agent's work (optional)

If you write the agent, you can add business-level events that Docket cannot observe on its own
— which tool did what, how big the result was, when a phase began:

```python
import docket

with docket.Session(max_usd=2.00) as s:
    s.record_model_call(model="claude-sonnet-5", input_tokens=1200, output_tokens=340)
    s.record_tool("search", {"q": "invoice totals"}, result_bytes=41_000)
    s.record_note("phase 2: writing files")
```

Any language, over HTTP:

```bash
curl -X POST "$DOCKET_PROXY_URL/docket/tool/call" \
     -d '{"name":"search","args_digest":"9f86d081884c7d659a2feaa0c55ad015","result_bytes":41000}'
```

Tool arguments are stored as digests by default — evidence without a second copy of your data.

## A worked example with files

[`examples/`](../examples/README.md) contains a runnable end-to-end scenario: a tiny agent, a policy, a
CI recipe, and the expected receipt, including how to run it with no network at all.

## Where to go next

- [`concepts.md`](concepts.md) — the mental model, if the words *ledger*, *receipt*, *boundary*
  and *cap* are still blurry.
- [`policy-reference.md`](policy-reference.md) — every field you can configure, and its failure
  mode.
- [`verification.md`](verification.md) — what each verification check actually proves.
- [`threat-model.md`](threat-model.md) — read before you rely on this for anything that matters.
- [`integrations.md`](integrations.md) — the agent you already use.
