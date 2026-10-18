# Policy reference

A policy is a TOML file. `docket init` writes a commented starter; `docket run -p policy.toml
-- <command>` uses it. Everything is optional — a policy with no file at all means "no caps,
record everything observable".

The policy **document** (not the file bytes) is digested with SHA-256 and embedded in every
receipt, so a reader can see exactly which rules were in force. Two policies that differ only
in whitespace or comment text produce the same digest; two that differ in any value produce
different digests.

```toml
[policy]
name = "team-default"       # a label, embedded in the receipt
```

---

## `[budget]`

| key | values | default | meaning |
| --- | --- | --- | --- |
| `mode` | `"usd"`, `"quota"` | `"usd"` | `usd` meters spend; `quota` counts tokens and calls and **does not guess dollars** — the right choice for subscription plans (Claude Pro/Max, ChatGPT plans, Gemini subscriptions) where there is no per-token bill |

In `quota` mode the meter still counts tokens and calls, caps still apply, and receipts record
`cost_basis = "quota mode: subscription usage is counted in tokens, not dollars"`. This matters:
inventing a dollar figure for an unmetered subscription would be laundered precision.

## `[caps]`

Checked **before** every model call is forwarded and again after each response.

| key | type | default | meaning |
| --- | --- | --- | --- |
| `max_usd` | decimal | none | cumulative spend ceiling. Only meaningful in `usd` mode |
| `max_tokens` | int | none | cumulative `input + output` tokens (cached input counted separately) |
| `max_model_calls` | int | none | number of forwarded model calls |
| `max_wall_seconds` | int | none | wall-clock ceiling for the supervised process |
| `warn_at` | fraction (`0.8`) or percent (`80`) | `0.8` | emit a `cap.warning` event at this fraction of `max_usd` |
| `on_breach` | `"kill"`, `"deny"` | `"kill"` | `kill`: terminate the process group (exit `42`). `deny`: refuse further model calls with HTTP 403 and a `docket_cap_exceeded` body, leave the process alive |
| `grace_seconds` | float | `5.0` | time between `SIGTERM` and `SIGKILL` on breach |

Behaviour worth knowing exactly:

- The cap is a **ceiling on crossing, not a promise never to cross it**: the call that crosses
  the line is *allowed to complete* (it is already in flight), the meter notices immediately
  after, and every subsequent call is refused. That is why a run can end at `$2.00` with a
  `$2.00` cap and a recorded spend of `$2.0005` — the overage is the call that crossed.
- With `on_breach = "kill"` the supervisor first asks the boundary to stop accepting calls,
  then terminates the process group (`SIGTERM`, then `SIGKILL` after `grace_seconds`). The
  exit code is `42`, and the receipt's `session.breached` names the cap that fired.
- Caps are enforced where the call leaves the machine. If an agent bypasses the boundary, no
  cap applies to that traffic — the receipt says so, in `limits_disclosure`.

## `[models]`

| key | type | default | meaning |
| --- | --- | --- | --- |
| `deny` | list of globs | `[]` | if the model matches any deny pattern, the call is refused (`403`, `docket_policy_denied`) |
| `allow` | list of globs | `[]` | if non-empty, only matching models are permitted; deny still wins |

Matching is case-insensitive `fnmatch` on the model string, evaluated over the **whole list**
in order for `deny` (first match refuses). A blocked call is recorded as a `model.denied`
event, costs nothing, and is reported as a finding.

Refusing before forwarding is the cheapest possible enforcement: a denied call is free.

## `[files]`

| key | type | default | meaning |
| --- | --- | --- | --- |
| `watch` | list of globs | `["."]` | directories/files snapshotted (digests always; contents too when `--snapshot` is on). Roots outside the working directory are disclosed as not covered — see [ADR 0004](adr/0004-content-snapshots-and-confinement.md) |
| `ignore` | list of globs | `[]` | paths excluded from snapshots, digest and content alike (use for `.git/**`, `.venv/**`, `node_modules/**`). An ignored path is also one a rollback cannot restore |
| `deny_write` | list of globs | `[]` | paths that must not change. Changes are **detected, hashed and reported as `fs.change` + `policy.violation` events** |

A cap is validated where it is set, not where it is used: `caps.max_usd` must be a finite,
non-negative amount (a `NaN` cap compares false against every threshold, so it is a cap that can
never fire — the one failure mode this tool must not have), and the same is true of the flags
that override it (`docket run --max-usd`). In code, `Caps.set_max_usd(...)` is the sanctioned
way to change it after construction, because assigning the field directly skips the check that
`__post_init__` does for constructors.

Read this twice: `deny_write` is a **tripwire, not a lock**. Docket runs in user space next to
the agent; it cannot veto another process's write syscalls. The receipt proves the file
changed, with content digests before and after — which is exactly what you need if it happens,
and not the same thing as prevention. If you need prevention, use a sandbox (and keep Docket
for the evidence).

Snapshots are taken of the paths in `watch`, with large/binary files hashed by digest rather
than stored. Changes appear as `fs.change` events with `added`/`modified`/`removed` and the
digests involved.

One exclusion is not yours to write: **the run's own evidence directory is excluded from the
watched tree by the Supervisor**, from resolved paths, whenever `--docket-dir` lands inside it.
Docket writes its ledger *during* the run, so watching it would record Docket's own writes as
the agent's changes — and a rollback derived from that diff would contain an instruction to
delete the record it was derived from. Adding `.docket/**` to `ignore` yourself is harmless but
unnecessary; the exclusion is recorded as a `note` event and in the effective policy the receipt
embeds, so it is visible in the evidence rather than implied by the code. It is *not* applied
when `--docket-dir` is the working directory itself (there would be nothing left to watch): that
run proceeds with no exclusion and says so on stderr.

## `[cache]`

The boundary's response cache. Off unless `enabled` says otherwise, and every hit it makes is a
`model.cache_hit` event in the ledger — see [the response cache](caching.md) for the full account,
including the hazard this section exists to bound.

| key | type | default | meaning |
| --- | --- | --- | --- |
| `enabled` | bool | `false` | answer an identical request (same upstream path, same canonical body) from the cache instead of the provider |
| `ttl_seconds` | number ≥ 0 | `300` | how long an entry stays usable; `0` = never expire on time, with capacity still bounding memory |
| `max_entries` | int ≥ 1 | `256` | least-recently-used entries are evicted above this |
| `max_hits` | int ≥ 0 | `1000` | ceiling on calls the cache may answer in this run; `0` = no ceiling. **A cached loop spends nothing, so no dollar or call cap can stop it** — this ceiling makes the run start paying again, and the moment it engages is recorded as a `note` event |

`[cache]` appears in the receipt's policy document — and therefore in its digest — **only when it is
enabled**. A policy that says nothing about caching is the policy it always was, with the digest it
always had: receipts and signatures name that digest, and a feature that moved it would turn every
earlier receipt into a document nobody could re-derive. `tests/test_cache.py` asserts exactly that.

Naming any tuning key turns the cache on, in the policy file and on the command line alike:
`ttl_seconds` alone in `[cache]`, or `--cache-max-entries 4`, would otherwise configure a cache that
never answers — a policy nobody means. `enabled = false` written beside other keys is the one
explicit way to say "tuned, but off", and it wins because it is the explicit statement. An empty
`[cache]` section says nothing and leaves the cache off.

## `[egress]`

Applies to `--http-proxy` mode (and to CONNECT tunnels generally).

| key | type | default | meaning |
| --- | --- | --- | --- |
| `mode` | `"deny"`, `"tunnel"` | `"deny"` | `deny`: a host that is not in `allow` is refused with a `net.denied` event. `tunnel`: non-model hosts are forwarded and recorded as `net.allowed` — useful for package installs, honest about its limits |
| `allow` | list of host patterns | provider hosts + `localhost`, `127.0.0.1` | exact hostnames or globs |

Model provider hosts are always permitted: the boundary's job is to *record* them, not block
them. Blocking a model host wholesale would defeat the purpose of the boundary.

## Where the price table fits

Prices are not part of the policy; they are a separate, versioned artifact
(`src/docket/prices.json`, tag `docket/prices/v1`) with an `as_of` date and a source note per
family. Every receipt embeds the exact table it used, plus `price_table.digest`, so a reader
can compare it against a copy they trust.

- `docket prices` shows the table and its digest.
- `docket run --prices my.json -- …` uses your own table for one run (enterprise discounts,
  private endpoints, a model that is newer than your installed version).
- A model that matches only the `*` fallback is flagged in the receipt
  (`totals.assumed_price_models`) and marked `assumed`. If your spend matters, override the
  table rather than accepting the fallback.

## Precedence and overrides

Command-line flags override the file for that run: `--max-usd`, `--max-tokens`, `--max-calls`,
`--max-minutes`, `--on-breach`, `--budget-mode`, `--prices`. The effective policy — not the
file — is what the receipt embeds, so overrides are visible to anyone verifying later.

## What a policy cannot express

- "Ask a human before exceeding" — there is no approval workflow, by design; Docket's answer to
  ambiguity is a cap, not a chat.
- Per-tool or per-path budgets ("$2 for file reads, $5 for the shell"). Everything is
  session-scoped today. If you need this, open a discussion with the workflow you have in
  mind — [#2](https://github.com/shivanshjain456/docket/issues) style, with receipts.
- Anything about *semantics*: Docket does not decide whether a call is a good idea, only
  whether it is permitted and what it cost.
