# Threat model

Written to be read *before* you rely on Docket for anything that matters. If a claim in the
README is missing here, that is a documentation bug — the two are meant to agree.

## What Docket is

A supervisor and evidence layer that runs beside an agent in user space, enforces caps at a
loopback HTTP boundary, records what crosses that boundary, and produces a receipt that a third
party can verify offline.

## Assets

1. **Money** — the spend an agent can incur.
2. **Time** — how long an agent can occupy a machine.
3. **Blast radius** — files touched, hosts contacted, processes started.
4. **Evidence** — the truthfulness of the record you hand to someone else.
5. **Secrets** — prompts, credentials, file contents that must not end up in a ledger.

## Adversaries and outcomes

### 1. A runaway agent (no attacker)

The common case: a loop, a retry storm, a cache that stopped working, an oversized context fed
back into itself. **Enforced:** caps on dollars, tokens, calls; wall-clock; process-group
termination or refusal. **Detected:** loops by request fingerprint, uncached repeats, oversized
payloads, premium-model leakage.

*Residual risk:* the crossing call completes before the meter notices; a cap is a ceiling on
crossing, not a wall before it. Choose a cap below your pain threshold, not at it. Also,
`max_usd` in `quota` mode is inert by design — subscription usage has no per-token price, and
inventing one would be a lie with decimal places.

### 2. An agent that is careless with data

Writes to `.env`, pushes a token, sends a prompt to a third-party host. **Enforced:**
`[models]` allow/deny; `[egress]` allow-list with `mode = "deny"`; boundary refusal before the
request leaves. **Detected:** `deny_write` violations with before/after digests; egress denials.

*Residual risk:* no prompt/completion content is stored by default, but **token counts, model
names, file paths, byte sizes and request digests are** — a ledger is still metadata about your
work. Gate access to `.docket/` as you would to a log. `--record-bodies` is off by default for
this reason; enabling it puts prompts on disk.

### 3. An agent that tries to tamper with its own record

An agent that can write files can write to `.docket/`. **Detected on verification:** edits,
reordering, truncation, extension — any modification fails `ledger-chain` at the first affected
event.

*Residual risk:* an agent with write access can delete the ledger and receipt entirely, or
truncate before the interesting part. It cannot make an altered ledger verify — but *absence*
of evidence is not detectable from inside the receipt. Mitigations: run with `--docket-dir`
outside the agent's write scope; seal or sign receipts (`--seal`/`--sign`); archive the receipt
somewhere the agent cannot reach (the digest in a CI log is enough to prove later that a
presented ledger was substituted). This is the honest limit of an unprivileged in-process
recorder, and the reason the format is designed to be *verifiable* rather than merely *logged*.

### 4. An operator who lies

Someone runs an agent, then presents a doctored receipt. **Detected:** verification fails;
`limits_disclosure` is part of the signed/ sealed payload, so a rewritten scope statement
breaks the seal/signature, not just the prose.

*Residual risk:* the operator can always run the agent *without* Docket, or point it outside
the boundary and present a truthful receipt of a run that says nothing happened. A receipt
proves what it covers; it cannot prove what it never saw. If your threat model includes a
dishonest counterparty, require receipts **and** independent corroboration (provider-side
billing exports, the other side's logs).

### 5. A provider that lies about usage

Token counts come from the provider's usage fields. A provider that under-reports produces a
consistent, verifiable, wrong record. **Mitigated:** `usage_source` distinguishes `reported`
from `estimated`; figures that are estimated are labelled in the receipt and by the verifier.
Compare against invoices where it matters.

### 6. A third party modifying a receipt in transit

Email, chat, ticket systems — the receipt travels. **Detected on verification:** any edit breaks
the inclusion proofs or the seal/signature. Signing is what makes this a defence rather than an
inconvenience, because a signature travels with the receipt while a seal needs the key.

### 7. A hostile receipt, ledger or snapshot manifest handed to `docket restore`

A restore is the one Docket operation that writes and deletes, and its input is a document.
Ledgers are verified to be internally consistent, not to have been written by Docket, and a
manifest is just a JSON file. **Enforced:** the plan is derived from the record and printed
before anything is written; paths are checked rather than trusted (no absolute paths, no `..`,
no symlinked component), blobs are located by digest inside the store, digests must be 64
lowercase hex digits, and modes are masked to permission bits, so a doctored manifest cannot
make Docket restore a setuid binary or copy a file from outside the store. **Detected:** the
three documents that record the run's tree must agree; if the receipt claims a different tree
than the ledger or manifest, the restore stops and asks for `--root` instead of choosing.

*Residual risk:* the destination is a directory on your machine, and a restore writes real
files there. A convincing forged ledger can make a restore put an *old* file back over a newer
one — which is why a path whose current digest does not match the run's record is refused
without `--force`, and why the plan is printed first. Also: `--force` and `--root` are explicit
operator statements, and both can be aimed at the wrong tree by a human in a hurry.

### 8. A malicious actor who can run code as you

Root, or an agent with your privileges, can kill Docket, edit the ledger, or read your keys.
Docket's answer is not prevention (it cannot be) but *detectability by others*: signed receipts
archived outside the machine, and a chain that cannot be edited undetectably. If your threat
model includes a local root attacker, you also need an append-only sink elsewhere — file a
discussion, this is exactly what the anchoring work in the roadmap is for.

## Trust assumptions, stated plainly

1. The operator ran the recorder honestly (chain proves integrity, not intent).
2. The machine clock was not manipulated (timestamps are claims, not proofs — there is no
   trusted time source).
3. The provider reported token counts truthfully.
4. SHA-256 is collision-resistant for this purpose.
5. The price table you trust is the one whose digest matches the receipt's — check it when the
   number matters.

## What is explicitly out of scope

- **Prevention of filesystem writes** (`deny_write` is a tripwire).
- **Restoring files that no snapshot covered.** `not_covered` says which ones; a rollback is
  never a promise that the tree is exactly as it was.
- **Defending the tree against a local attacker racing a restore** between the check and the
  write. See ADR 0004 — the confinement rules are aimed at hostile *documents*, not at a
  concurrent attacker with write access to the same directory.
- **Observing anything that bypasses the boundary** (direct provider URLs, tunnels in
  `mode = "tunnel"`, other processes).
- **Sandboxing** (containers, gVisor, seccomp, VMs — different tools, complementary).
- **Prompt-injection consequences inside the agent** (Docket records; it does not judge).
- **Deciding whether an action was correct.**

## Attack surface of Docket itself

| surface | exposure | notes |
| --- | --- | --- |
| loopback proxy | the supervised process and anything on the machine that can reach 127.0.0.1 during the run | no authentication by design: it is a local recording boundary, not a public service. It binds loopback only |
| `/docket/*` recording endpoints | same | they append events to the *current* session only; they cannot read other sessions or write to another ledger |
| ledger/receipt files | your filesystem | mode `0600`-friendly; no content by default; treat as sensitive metadata |
| price table | a JSON file | a malicious table can skew `max_usd` enforcement downward (fails safe) or upward with a large cap (visible: it is embedded in the receipt) |
| any document Docket reads | your filesystem, or the agent | parsed strictly: `NaN`/`Infinity`, duplicate-key ambiguity and non-UTF-8 bytes are refused rather than interpreted, and depth is bounded before parsing. A body carrying a non-finite number is refused at the boundary, not forwarded and not filed as an internal error |
| seal key | `~/.docket/seal.key` | an HMAC key: whoever has it can produce a seal. It is not a signature; do not present it as one |
| snapshot store | `.docket/snapshots/` | a copy of watched file *contents* — the most sensitive thing Docket writes. It inherits the watched tree's sensitivity: ignore secrets in `[files] ignore`, keep `.docket/` out of the agent's write scope, and treat the store like the source it mirrors |
| `docket restore` | your working tree, opt-in via `--apply` | dry run by default; refuses paths that changed after the run unless `--force`; refuses anything outside the run's tree. The blast radius is the watched tree, which is the point |

Reporting: [`../SECURITY.md`](../SECURITY.md). Claims that can be broken are listed there as
*in scope*, so there is no ambiguity about what a valid report looks like.
