# Concepts

Six nouns carry the whole design. If you read only one page before adopting Docket, read this
one — most disappointments in this category come from a mismatch on the *boundary*.

## Boundary

The place where Docket sits between your agent and the outside world: a loopback HTTP proxy
that the supervised process is pointed at (provider base-URL variables, or `HTTP_PROXY` in
`--http-proxy` mode), plus snapshot points on the filesystem before and after the run.

Everything Docket claims rests on this sentence: **it can only bound and record what crosses
the boundary.** An agent configured with a direct provider URL, a subprocess with its own
network access, or traffic that never touches the filesystem it watches, is outside the
receipt — and every receipt says so in `limits_disclosure`.

This is a limitation and a design choice. The alternative (kernel hooks, sandboxes, eBPF) buys
more coverage at the cost of privileges, portability, and the ability to run inside a container
you do not control. Docket complements those tools; it does not replace them.

## Cap

A ceiling checked at the boundary before a model call is forwarded: dollars, tokens, model
calls, or wall-clock time. Two behaviours:

- `on_breach = "kill"` — stop accepting calls, terminate the process group, exit `42`.
- `on_breach = "deny"` — refuse further model calls (HTTP 403), let the process live so it can
  finish writing its work or shut down cleanly.

A cap is a *ceiling on crossing, not a promise never to cross*. The call that crosses completes
(it is already in flight); the meter notices immediately after; everything subsequent is
refused. That is why a `$2.00` cap can end with `$2.0005` recorded — and why the receipt states
both numbers.

## Ledger

An append-only file of events, one JSON object per line. Each event carries its sequence
number, timestamp, type, data, the hash of the previous event, and its own hash computed over
all of that. The first event links to a fixed genesis value.

Consequences worth internalising:

- **Any edit is detectable.** Change one byte in the middle and every following link stops
  matching; verification names the first broken event.
- **Order is part of the record.** Reordering, inserting or truncating is detectable.
- **Bytes matter.** Lines are written in canonical form (sorted keys, no whitespace) and
  hashed structures contain no floating-point numbers — integers, or decimal strings. This is
  what lets a verifier written in another language reproduce the same digests. The rule caught
  three real bugs in this repository during development.

## Receipt

The portable artifact for one run: a JSON file with the session summary, the policy that was in
force (and its digest), totals, an appendix of per-call usage rows carrying Merkle inclusion
proofs, the exact price table used (and its digest), deterministic findings, and the
`limits_disclosure` block. Optionally a seal (HMAC) and/or an Ed25519 signature.

A receipt is designed to be *handed to someone else*: a client, an auditor, an incident
review, a counterparty. It is self-contained — the reader needs no access to your machine, your
account, or your vendor relationship.

## Merkle root and inclusion proofs

The receipt does not contain the whole ledger. Instead it commits to it: every event hash
becomes a leaf in an RFC 6962 Merkle tree, and the root goes in the receipt. Each usage row in
the appendix carries the short path of sibling hashes that proves "this event is in that tree"
— which is how "the cost you are looking at came from this exact call" is checkable without
shipping the entire record.

## Findings

Arithmetic over events, printed as human sentences. Each finding declares its `severity`, its
`confidence` — `deterministic` (recomputed from events alone) or `heuristic` (a model of what
could have happened) — the event sequence numbers it rests on, an attributed USD impact, and a
remedy.

The confidence distinction is not decoration. It is the difference between "13 byte-identical
requests were sent, costing $1.62" (deterministic) and "prompt caching could have saved about
$2.89" (heuristic — the provider may have had reasons not to cache). Blending the two would
launder guesses as facts, which is the failure mode of every "AI observability" panel that
shows a number nobody can reproduce.

## What "verifiable" buys you, concretely

| question | answer without Docket | answer with a receipt |
| --- | --- | --- |
| what did the agent spend? | screenshot a dashboard, if you have access and trust it | recompute it from token counts and the embedded price table |
| what did the agent change? | git diff of *something*, maybe | digests of every changed file, in order, in the record |
| did anything get edited after the fact? | nobody knows | any edit fails verification, named at the first changed event |
| can I hand this to an auditor? | export a CSV and hope | hand over a file they can check with their own tool |
| what was *not* observed? | usually unstated | `limits_disclosure`, copied verbatim into every receipt |

## Vocabulary that appears in receipts

| term | meaning |
| --- | --- |
| `docket_number` | `DKT-YYYYMMDD-XXXXXX`, the human handle for a run |
| `usage_source` | `reported` (provider's own counts) or `estimated` (heuristic; a consumer must not present it as metered) |
| `price_table.digest` | which price table was used — compare against a copy you trust |
| `assumed_price_models` | models priced only by the `*` fallback; treat their totals as approximate |
| `breached` | the cap that fired, e.g. `max_usd` |
| `event_counts` | counts per event type, so a reader can see what was recorded without opening the ledger |
| `findings[]` | analysis with evidence pointers and confidence labels |
