# FAQ

The awkward questions, answered before you have to ask them in an issue.

**Is this just logging with extra steps?**
No, for three specific reasons. Logging happens after the fact; Docket refuses calls at the
boundary and can terminate the process. Logs you trust; a receipt you check, in another
language, offline. And logs rarely state their own scope; every receipt carries
`limits_disclosure` and verification fails without it.

**Why not just use LiteLLM / a gateway / my cloud's AI gateway?**
Those are good and often the right tool. Docket differs where it matters to evidence: it works
with subscription-authenticated agents where there is no key to route; it does not require your
traffic to pass through a service you must trust to be honest about its logs; and it produces an
artifact a third party can verify with no account. Use both if your situation calls for it.

**Is a receipt "tamper-proof"?**
No, and anyone who says that about any local tool is overselling. A receipt is *tamper-evident*:
edits, reordering, truncation and extension are detectable, and verification names the first
affected event. An agent with write access can delete the ledger; it cannot make a modified one
verify. Archive receipts (or their digests) somewhere the agent cannot reach, and sign them if
the recipient needs to distinguish "honest operator" from "correct operator".

**Can an agent escape the cap?**
Yes, in ways the docs state: by bypassing the boundary (direct provider URL without
`--http-proxy`), by having a process outside the supervised tree spend money, or by the one call
that crosses the ceiling completing before the meter notices. What it cannot do is make the
receipt claim otherwise. Choose caps below your pain threshold and use a sandbox for real
containment — Docket is the evidence layer, not the wall.

**Does Docket store my prompts?**
No. Digests, token counts, byte sizes, model names, timestamps and paths only. `--record-bodies`
stores request bodies, and it is off by default with a warning in the docs because prompts are
the most sensitive thing a user has. Ledgers are still metadata about your work: treat `.docket/`
with the same care as a log file.

**How accurate is the cost?**
As accurate as (a) the provider's usage fields and (b) the price table you use. Reported usage
gives exact arithmetic on the provider's own counts; estimated usage (when a provider returns no
usage) is marked `estimated` and should not be presented as metered. Models priced by the `*`
fallback are listed in `totals.assumed_price_models` and flagged `assumed` — override with
`--prices` if your rates differ from list price.

**Subscription plans — my agent does not have per-token billing.**
Use `--budget-mode quota`. Docket then caps tokens, calls and wall-clock and refuses to invent a
dollar figure, because a made-up number with six decimal places is worse than no number.

**Why Python? Shouldn't this be Go/Rust?**
The tool is a supervisor, an HTTP proxy and a file watcher — the standard library covers all
three, and a `pipx`-installable tool with zero dependencies is the shortest path to adoption on
machines where agents already run. The *format* is language-neutral and the verifier exists in
two languages already; verifiers in Go, Rust and TypeScript are the first funded roadmap items
(`ROADMAP.md`, `SPONSORS.md`) because verification is the part that should not depend on Python.

**What if the agent writes to the ledger?**
It can. Then verification fails at the first modified event — that is the design. Keep
`.docket/` outside the agent's write scope where you can, sign receipts when the recipient
needs more than integrity, and archive a digest somewhere the agent cannot reach.

**Is the price table trustworthy?**
It has an `as_of` date, per-family source notes, and a digest that appears in every receipt
produced with it. It is a best-effort list-price table for budgeting, not a billing system.
Compare digests when the money matters.

**Why "Docket"?**
A docket is the official record of a proceeding — a number, a file, a record of what happened.
Every run gets one. The word is plain English, which matters for a tool used by auditors,
lawyers and finance people who are not going to learn a new vocabulary.

**Does it work with [my agent]?**
If it honours `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`/`GEMINI_API_BASE_URL`, yes. If it does not,
`--http-proxy`. If it is a service on another machine, no — and the receipt will say model
traffic was not observed rather than presenting a suspiciously cheap run as a quiet one.
See [`integrations.md`](integrations.md).

**Can I get receipts for runs that already happened?**
No. A receipt attests to what crossed the boundary during a supervised run; there is no
retroactive mode, and inventing one would create exactly the unverifiable claims this format
exists to avoid.

**What is the business model? Why is it free?**
The cap and the format are free and Apache-2.0 forever. Sponsorship funds the standard-shaped
work — independent verifiers, format stewardship, an LTS window, docs, and the public incident
corpus — with the independence rules written into [`GOVERNANCE.md`](../GOVERNANCE.md). There is
no hosted product on the roadmap, and there will not be one.

**I found a way to make a receipt verify when it should not.**
That is a security advisory, and it is exactly the class of report this project most wants.
[`SECURITY.md`](../SECURITY.md) — private reporting, 72-hour acknowledgement target. Please do
not open a public issue first.

**Where do I ask a question that is not here?**
[Discussions](https://github.com/docket-dev/docket/discussions). If your question turns out to
be a common one, it gets added to this file — that is the only reason it exists.
