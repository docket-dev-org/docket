# Docket receipt format, version 1

**Status:** stable (v1, October 2026) · **Format tag:** `docket/receipt/v1` · **Ledger tag:** `docket/ledger/v1`
**Reference implementation:** `docket` (Python, zero dependencies) · **Independent implementation:** `verify-js/verify.mjs` (Node, zero dependencies) · **Conformance vectors:** `spec/vectors/`
**Licence of this specification text:** CC BY 4.0 (copy it, implement it, quote it; attribution appreciated). The reference implementation and tooling are Apache-2.0.

---

## 1. Purpose and scope

This document specifies a file format for **evidence about what an AI agent did**, and a
procedure for checking that evidence offline, in any language, without trusting the tool
that produced it or the person who ran it.

The design goal that shapes every decision below: a receipt must be checkable by someone
who has *nothing but the receipt*. That means the receipt must carry the rules it was
checked against (the price table), the roots that bind it to the full record (Merkle
root), and a plain statement of what it does **not** cover (§9).

Non-goals: this format does not attempt to prove that no other action occurred outside the
recorded boundary, does not provide trusted timestamps, and does not replace a sandbox or
an operating-system audit facility. §9 is normative about these limits and receipts
carry a copy of it.

## 2. Conformance

An implementation conforms to this specification if:

1. it reproduces every value in `spec/vectors/*.json` byte for byte; and
2. for any receipt it accepts, it reports exactly one of *verified*, *not verified* or
   *not checked* for each item in §8 — never "passed" for something it did not check.

The keywords MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are used as in RFC 2119.

## 3. Canonical form

Every digest in this format is computed over the **canonical form** of a value.

### 3.1 Encoding

1. Objects: keys sorted by Unicode code point; no insignificant whitespace; `{`/`}`/`,`/`:`
   only.
2. Strings: JSON-escaped, UTF-8 encoded, non-ASCII characters emitted literally (never
   `\uXXXX` escapes for characters in the Basic Multilingual Plane).
3. Arrays: order preserved.
4. Booleans and null are literals `true`, `false`, `null`.

Equivalently: `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
in Python, or `JSON.stringify` over a key-sorted re-construction in JavaScript.

### 3.2 Numbers — the interoperability rule

**A hashed structure MUST NOT contain a floating-point number.**

- Integers are written as JSON numbers (`1200`, `-3`, `0`).
- A float with an integral value MUST be normalised to an integer (`5.0` → `5`).
- Any other fractional quantity MUST be encoded as a decimal **string**.

This rule exists because shortest-representation rules differ between runtimes: Python
serialises `5.0` as `5.0` and `1e-07` as `1e-07`, while JavaScript serialises them as `5`
and `1e-7`. A format that permits floats produces receipts that verify in one language and
fail in another — the single most common way a "tamper-evident" record quietly becomes
unverifiable. Implementations MUST reject a hashed structure containing a non-integral
float rather than guessing at a representation.

**A document MUST NOT contain `NaN`, `Infinity` or `-Infinity`.** Those three tokens are not
JSON: RFC 8259 has no way to spell them, and `JSON.parse` rejects them, while Python's
`json.loads` accepts them as an extension — so a document carrying one is a document that one
reference implementation reads and the other refuses. An implementation that *reads* this
format MUST reject them (rather than rewriting them), because a value with no JSON form cannot
be canonicalised, and a digest computed over a value only one runtime can express is exactly
the failure this section exists to prevent. `null` is the only JSON spelling of "no value".

### 3.3 Decimal strings

Amounts of money and other fractional values use a decimal string with at most six
fractional digits:

- `"0.00"`, `"12.50"` — exactly two decimals when the value is a whole number of cents;
- `"0.018000"`, `"1234.567890"` — exactly six decimals otherwise.

Verifiers MUST compare amounts as exact decimals (integer micro-units are sufficient:
multiply by 10^6), never as binary floating point.

### 3.4 Digests

`sha256(X)` denotes the SHA-256 of the UTF-8 encoding of the canonical form of `X`,
lowercase hexadecimal. `sha256(bytes)` denotes the SHA-256 of those bytes.

## 4. The ledger

A ledger is an append-only file of newline-delimited JSON objects ("JSONL"). Each line is
one event.

### 4.1 Event

```
{ "seq": <integer, 0-based>,
  "ts":  <RFC 3339 UTC timestamp, e.g. "2026-10-13T09:14:22Z">,
  "type": <string>,
  "data": <object>,
  "prev": <hex digest of the previous event, or the genesis value>,
  "hash": sha256({seq, ts, type, data, prev}) }
```

`hash` covers exactly those five members: an implementation MUST recompute it from the
others and MUST NOT include `hash` in its own preimage.

### 4.2 Genesis

The first event's `prev` is
`sha256("docket/genesis/v1")` =
`adf41ffdf0fd9128f8f4fe88fa09fba359e27453b02be87886b621a2c2b0f7dc`
(the value is repeated in `spec/vectors/ledger.json` and MUST be checked against the
recomputation, not copied).

### 4.3 Chain validity

A chain is valid when, for every event in order: `seq` equals its index, `prev` equals the
previous event's `hash`, and the recomputed `hash` matches. Any mutation, insertion,
reordering, truncation or (for a ledger compared against a receipt) extension breaks
validity at or after the first affected event. A verifier MUST report the sequence number
of the first failure.

### 4.4 Event vocabulary (v1)

`session.start`, `session.end`, `policy.applied`, `policy.violation`, `cap.warning`,
`cap.exceeded`, `model.request`, `model.response`, `model.denied`, `model.error`,
`model.cache_hit`, `tool.call`, `tool.result`, `fs.snapshot`, `fs.change`, `proc.spawn`,
`proc.exit`, `net.allowed`, `net.denied`, `note`.

Unknown types MUST be preserved and MUST NOT cause verification to fail; extensions SHOULD
namespace them (`x-vendor.something`). Events carry a `reported_by` member when they were
asserted by the agent under supervision (`"agent"`) rather than observed at the boundary;
this distinction is normative for interpretation and MUST NOT be dropped.

### 4.4.1 Response-cache hits

A recorder MAY answer a repeated request from a response cache instead of contacting the
provider (the reference implementation does so only when the operator's policy enables it).
The provider then sees nothing, so there is no `model.request`/`model.response` pair to write:
the hit is recorded as its own event in place of both.

```
{ "type": "model.cache_hit",
  "data": { "key": <the request_sha256 of the request event that was served>,
            "origin": { "seq": <seq of the model.response being replayed>,
                        "hash": <hash of that event> },
            "model": ..., "usage": {...}, "usage_source": ...,
            "avoided_cost_usd": "0.006000", ... } }
```

A `model.response` event whose response was kept carries
`"cache": {"key": <request_sha256>, "stored": true}`; when the response was **not** kept the
member is present with `"stored": false` and a `"reason"` saying why (an error, a truncated
stream, or a response rewritten by fault injection). That member is what a verifier uses to
check a hit: the hit names an origin, and the origin must be a `model.response` recorded as
stored under the same key.

A hit MUST NOT contribute to `totals`, to `model_calls.count` or to any cap: it is a call that
did not happen, and folding it into the spend would make the evidence describe a run that never
took place. What it *saved* is reported separately (§6.4) and recomputed by verification.

### 4.5 File digest

The ledger's `file_sha256` is the SHA-256 of the file bytes as written. Implementations
SHOULD write lines in canonical form so that this digest is reproducible by other
implementations.

## 5. Merkle tree

The receipt commits to the whole ledger with an RFC 6962 Merkle tree over the ordered
event hashes:

- leaf hash: `sha256(0x00 || event_hash_bytes)`
- node hash: `sha256(0x01 || left || right)`
- an unpaired node is promoted unchanged to the next level
- the empty tree hashes to `sha256("")`

Inclusion proofs are the audit path as `[{"side": "left"|"right", "hash": "..."}]`, from
leaf to root. The odd-node rule and the prefixes prevent second-preimage ambiguity between
leaves and interior nodes; implementations MUST NOT substitute the "duplicate the last
node" rule used by other systems, because it changes every root.

## 6. The receipt

A receipt is a JSON object with these members:

| member | meaning |
| --- | --- |
| `format` | `"docket/receipt/v1"` |
| `docket_number` | human-quotable identifier, `DKT-YYYYMMDD-XXXXXX` |
| `created_at` | RFC 3339 UTC |
| `tool` | `{name, version, format}` |
| `session` | `{id, argv, cwd, exit_code, duration_seconds, breached, interrupted, host}` |
| `policy` | `{name, digest, document, source}` — `digest` = §3.4 of `document` |
| `totals` | token and cost totals, `cost_basis`, usage-source counts, `by_model[]` — all derived from `appendix.usage` and all checked against it (§8 items 5–5b) |
| `model_calls` | `{count, by_provider, denied, errors}` |
| `tools` | `{calls, names, failures}` |
| `files` | `{changed, added, modified, removed, violations, top_paths}` |
| `network` | `{tunnels_allowed, tunnels_denied}` |
| `caps` | `{exceeded, warnings}` |
| `event_counts` | event type → count |
| `ledger` | `{format, events, head, genesis, merkle_root, file_sha256, chain}` |
| `price_table` | `{schema, as_of, digest, document}` — the exact table used |
| `appendix` | `{usage[], proof_included, note}` |
| `latency` | §6.2 summary of the per-call latencies in `appendix.usage`, or absent when none was measured |
| `findings` | analytic results, each with `rule`, `severity`, `confidence`, `usd_impact`, `evidence`, `remedy` |
| `limits_disclosure` | §9 statement, copied verbatim from the policy in force |
| `seal` | `null` or `{alg: "hmac-sha256", key_id, value}` |
| `signature` | `null` or `{alg: "ed25519", public_key, value}` |

### 6.1 Appendix usage rows

Each row binds one metered call to the ledger:

```
{ "seq": <event seq>, "model": "...", "provider": "...",
  "input_tokens": <int>, "cached_input_tokens": <int>, "output_tokens": <int>,
  "usage_source": "reported" | "estimated",
  "cost_usd": "0.018000",
  "latency_ms": <int milliseconds, optional — §6.2>,
  "event_hash": <hash of the model.response event>,
  "merkle_path": [ ... ] }
```

`latency_ms` is optional and appears only when the call was timed. A row MUST NOT carry `0` to
mean "not measured": the member is absent, and `latency.measured_calls` (§6.2) states how many
rows had one.

`usage_source` is normative: `"estimated"` means the provider returned no usage and the
numbers were derived by heuristics. A consumer MUST NOT present estimated usage as metered.
Rows are limited to a documented maximum (`MAX_PROVEN_USAGE_ROWS` = 4000 in the reference
implementation); above that, proofs are omitted and `proof_included` is `false`, so a
verifier knows to demand the ledger instead of assuming coverage.

### 6.2 Latency

`latency` is **optional and derived**: it is present when at least one usage row carries
`latency_ms`, and it MUST be the summary of exactly those values. It is the only member of a
receipt that summarises a *measurement* rather than an arithmetic result, so both the method and
the coverage are part of it:

```
{ "method": "nearest-rank",
  "measured_calls": <int>, "total_rows": <int>,
  "total_ms": <int>, "max_ms": <int>,
  "p50_ms": <int>, "p95_ms": <int>, "p99_ms": <int> }
```

* A usage row MAY carry `latency_ms`: a whole, non-negative number of milliseconds measured
  around that call. It MUST NOT be a float, a boolean or a negative number; a row without a
  measurement MUST omit the member rather than substitute `0`.
* `method` names the percentile rule so that two receipts can be compared. The only value defined
  by this version is `"nearest-rank"`: sort the samples ascending, take
  `rank = ceil(p / 100 × n)` clamped into `[1, n]`, and read the `rank`-th smallest (1-indexed).
  This rule always returns a value that was actually measured. A verifier MUST refuse to compare
  a summary whose `method` it does not implement.
* `measured_calls` is the number of rows carrying `latency_ms`; `total_rows` is the number of rows
  in `appendix.usage`. They differ when only some calls were timed, and a consumer MUST NOT read a
  percentile as covering the whole session when they do.
* `total_ms` and `max_ms` are the sum and the maximum of the measured values.
* A verifier that recomputes the cost of the appendix SHOULD also recompute this summary from the
  same rows and fail on any disagreement. A receipt whose stated percentiles contradict the rows
  beside them is a fabricated measurement, which is worse than a missing one.

Latency is wall-clock time observed by the recorder, not by the provider: it includes the
boundary, the network and the provider's queue. It is not a claim about the provider's own
timing, and no member of this format reports a *trusted* timestamp.

### 6.3 Findings

Findings are derived arithmetic, not testimony. Each one MUST carry `confidence` of
`"deterministic"` (recomputed from events alone) or `"heuristic"` (a model of what could
have happened), and MUST cite the event sequence numbers it rests on. `usd_impact` is an
attributed figure, not a guaranteed saving.

### 6.4 Response cache (optional)

Present only when the run had a response cache enabled. Absent means the run had none, which is
a different statement from a cache that was configured and answered nothing.

```
{ "enabled": true,
  "policy": { "enabled": true, "ttl_seconds": 300, "max_entries": 256, "max_hits": 1000 },
  "hits": <int>, "saved_usd": "0.012000", "avoided_tokens": <int>,
  "hits_ceiling_reached": <bool>,
  "by_origin": [ { "key": ..., "origin_seq": <int>, "origin_hash": ...,
                   "model": ..., "usage": {...},
                   "avoided_usd_per_hit": "0.006000",
                   "avoided_usd_total": "0.012000",
                   "hits": <int>, "first_seq": <int>, "last_seq": <int>,
                   "event_hash": <hash of the first hit in this group>,
                   "merkle_path": [ ... ] } ] }
```

* `hits` is the number of `model.cache_hit` events. It is a boundary measurement, not
  arithmetic: a verifier checks it against the ledger's events when the ledger is supplied, and
  against the sum of `by_origin[].hits` always.
* `saved_usd` and `avoided_tokens` MUST equal the sum over `by_origin` of the per-hit amount
  times that group's `hits`, and the per-hit amount MUST be recomputable from the origin's token
  counts and the embedded price table — the same arithmetic §8.5 applies to a metered call.
* `by_origin` groups hits that replay the same origin with the same key, model, usage and
  per-hit amount. Hits that disagree MUST NOT be merged into one group: a sum that reconciles to
  a number no hit ever claimed is worse than two rows.
* `merkle_path`, when present, proves the group's first hit against `ledger.merkle_root`, the
  same way an appendix row proves a metered call. Like the appendix, proofs for a large session
  may be omitted together, and a verifier MUST then say it could not check them rather than
  assume them.
* `hits_ceiling_reached` MUST be true exactly when the ledger carries the corresponding `note`
  event. A cache that answers a loop spends nothing, so a ceiling on what it may answer is
  policy, and whether it was reached changes how the run's economics are to be read.

## 7. Signing and sealing

- **Seal** — `HMAC-SHA256(key, canonical(§6 without seal and signature))`. Proves integrity
  to whoever holds the key; it does **not** give third-party verifiability, and receipts
  MUST NOT describe a seal as a signature.
- **Signature** — Ed25519 over `canonical(§6 without signature, i.e. including the seal)`.
  The public key is embedded; a verifier SHOULD compare it against an expected key supplied
  out of band.

Implementations MUST NOT invent cryptographic primitives. If a required primitive is
unavailable, verification reports *not checked* with a reason.

## 8. Verification procedure

For a receipt (and the ledger, when available), report each item as pass / fail / not
checked:

1. `format` is recognised;
2. every event hash recomputed, links and sequence checked → first failure reported;
3. `head`, `events` and `merkle_root` equal the values recomputed from the ledger;
4. each usage row's `event_hash` is proven present via its `merkle_path` against
   `ledger.merkle_root`;
5. the cost of every usage row is recomputed from `input_tokens`, `cached_input_tokens`,
   `output_tokens` and the embedded `price_table`, and compared with `cost_usd`; the sum is
   compared with `totals.cost_usd`; token totals likewise;
5a. `totals.by_model[]` partitions those same rows: every model named by a row appears exactly
   once, no model appears that no row used, and each entry's `calls`, `input_tokens`,
   `cached_input_tokens`, `output_tokens` and `cost_usd` are the sums over that model's rows.
   `totals.usage_reported_calls`, `totals.usage_estimated_calls` and `totals.asserted_calls` are
   the number of rows with that `usage_source`, and with `asserted_by == "agent"`;
5b. `totals.cost_basis` does not contradict those counts. Only the leading word is checked: it
   names the class — `reported`, `estimated`, `mixed` or `quota mode` — and the rest of the
   sentence is free text. `quota mode` is reported as *not checked*, because the meter's mode is
   a fact about the run that the receipt records nowhere else; a verifier that accepted it would
   be accepting an assertion, and one that rejected it would reject every subscription run;
6. `latency`, when present, is recomputed from the `latency_ms` values of the rows it summarises
   (§6.2) and must agree with them in every member; a row whose `latency_ms` is not a whole,
   non-negative number is a failure;
7. `ledger.file_sha256` matches the supplied file;
8. `seal` verifies with the supplied key; `signature` verifies with the embedded or
   supplied public key;
9. `limits_disclosure` is present;
10. `cache`, when present: `saved_usd` and `avoided_tokens` equal the sum over `by_origin` of
    the per-hit amount recomputed from each origin's usage and the embedded price table; every
    group's origin is a usage row recorded as stored under the same key with the same usage;
    every group's inclusion proof verifies against the Merkle root; `hits` equals the sum of the
    groups and the number of `model.cache_hit` events in the supplied ledger.

Failure of any item makes the receipt **not verified**. Items that cannot be checked are
reported as such; a verifier that reports success without checking is non-conforming.

The **names** these items are reported under are fixed by
[`registry.json`](registry.json), and what it means to conform — including what must be refused
before any item is reached — is defined by [`conformance.md`](conformance.md). An implementation
that reports item 4 under an id of its own choosing is not conforming, however correct its answer.

## 9. What a valid receipt does and does not establish

**Establishes.** That the recorded events existed in this order and have not been altered
since the roots were computed (within SHA-256); that the stated token counts imply the
stated cost under the embedded price table (assuming the price table is the one the reader
believes — compare `price_table.digest` against a known value where it matters); that the
filesystem changes listed are the differences between two snapshots taken at the stated
moments; that, if signed, the holder of the signing key attested to this record; and, where a
cache is reported, that each hit replays a call the same receipt also records, at that call's
own price — money that was *not* spent, kept separate from the totals, which continue to
describe money that was.

**Does not establish.** That nothing else happened: traffic that bypassed the boundary, work
by processes outside the supervised tree, prompts or completions withheld by default, and
actions taken before or after the session are all outside the record. It carries no trusted
timestamp (a signer can backdate their own claims). It does not prove the agent was
well-behaved, only what was observed. It says nothing about whether an action was *right*.

**Trust assumptions.** The operator ran the recording tool honestly; the provider's token
counts are truthful (a provider that lies about usage produces a consistent but false
record); the machine clock was not manipulated beyond the ability to produce a plausible
timestamp.

## 10. Versioning

The `format` tag changes only for incompatible changes. Compatible additions (new event
types, new receipt members) keep `v1`; consumers MUST ignore members they do not know and
MUST NOT treat an unknown member as corruption. Conformance vectors are the normative
examples: a change to a vector that changes the *shape* of the format is an incompatible change
and is made under a new `format` tag, deliberately and in one commit, while a vector whose
*example values* changed — a different policy document inside the example, say — leaves the tag
alone. Either way the vectors move with the implementation, in the same commit; a committed
vector the implementation no longer reproduces is a defect, not a version. Immutable tags are
recommended, and where a writer runs from a git checkout the commit in `tool.version` SHOULD
identify it.
