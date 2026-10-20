# Keeping the responses the boundary saw

A `model.request` event can carry the request body verbatim. The matching `model.response` event
could only carry `response_bytes`, which is a *length*, and the bytes themselves were then dropped.

That asymmetry is invisible right up to the moment it matters. When a provider's usage block is one
of the three shapes Docket knows how to read, the token counts on the event are measured and nothing
is lost. When it is not, the counts are an *estimate* derived from the response text, and the text was
the thing being discarded. The call is recorded; the response is not; and the estimate can never be
checked, re-derived, or second-guessed by anyone holding a parser Docket does not have.

This page is about closing that, and about the two different things that come out of closing it.

## The two numbers, and why they are different

Every `model.response` event now carries a digest. Retention of the body is a separate, opt-in
thing. They are not the same claim, and the event keeps them apart on purpose:

| field | always present | what it is | what it proves |
| --- | --- | --- | --- |
| `response_sha256` | yes | SHA-256 of the exact bytes returned to the agent | that a body someone hands you *is* the body Docket saw |
| `response_stored_sha256` | only when retained | SHA-256 of the bytes written to disk | that the file on disk is still what was written, re-checked by `docket verify` |
| `response_retained` | yes | whether the body was kept | whether there is anything to check |
| `response_path` | only when retained | where, relative to the run directory | where to look |
| `response_stored_bytes`, `response_total_bytes`, `response_truncated` | only when retained | what was kept, of what | whether you are looking at a prefix |
| `response_note` | only on failure | why retention was asked for and did not happen | that the omission was not silent |

The digest is 64 characters and it is not the response. That is what makes it safe to have on by
default: an operator who wants no response bodies on disk still gets a handle on every one, which is
what lets anyone who *is* given a body prove it is the one the boundary returned. A body that hashes
to something else is not.

## Turning retention on

Off by default, and deliberately so. A response body can contain the prompt, a tool result, a
person's name, or a credential. The digest is the default; the body is the opt-in.

```toml
# docket.toml
[evidence]
record_responses = true
record_response_max_bytes = 65536   # 1 or more; 64 KiB by default
```

Bodies are written under `.docket/responses/`, named by their own whole-body digest. Two
consequences worth knowing:

- An identical response costs one file, not one per call.
- A file's name *is* its claim about its contents, which is what makes the verifier's check possible.

A `[evidence]` section that names only a size turns retention on, because a cap on its own configures
a retention that never happens and nobody means that. `record_responses = false` written beside it is
the one way to say "configured, but off", and it wins.

The policy digest is unchanged when the section is off, so receipts and signatures written before
this feature existed still re-derive. A digest that moves when a feature is added would turn every
one of them into a document nobody can check.

## What truncation does and does not claim

A body longer than the cap is stored as a prefix, and the event says so:

```json
"response_sha256": "43012d1c3761654a…",
"response_stored_sha256": "9f2ab8c1e0d447…",
"response_total_bytes": 184320,
"response_stored_bytes": 65536,
"response_truncated": true
```

The two digests differ, and they are *meant* to. `docket verify` re-hashes the file and compares it
with `response_stored_sha256`, so what is checked is the bytes that were written. The whole-body
digest remains a claim about the complete response, and it is only checkable by someone who has the
complete response.

Docket pins what it can prove and labels what it cannot. A verifier that recomputed a truncated
prefix into a whole-body digest would be checking a claim it had no way to make.

## What `docket verify` does about it

A retained body that nothing re-hashes is decoration: a file on disk, named by an event, that could
be swapped for a friendlier one after the run and nobody would know. So the verifier walks every
`model.response` event that names a retained body and re-hashes it.

| outcome | check | detail |
| --- | --- | --- |
| every retained body matches | `retained-responses` PASS | `3 retained bodies re-hashed and matched` |
| some are prefixes | `retained-responses` PASS | `…2 of them a stored prefix (the digest covers the bytes written, not the whole response)` |
| a file was edited | `retained-responses` FAIL | `the retained response .docket/responses/4301….bin hashes to 38dfe37a… but the event recorded 43012d1c…` |
| a file was deleted | `retained-responses` FAIL | `the retained response … is missing` |
| no ledger was supplied | the check does not run | the paths are relative to the run directory, and a path resolved against the wrong place is a path to nothing |

The last row is a skip rather than a failure, because a receipt handed over without its ledger is a
normal thing to do and blaming the caller's own input is not a verification result.

A retention that was *asked for* and could not be performed is disclosed on the event in
`response_note`, never swallowed. The cases that produce one are a missing run directory, a
filesystem below the reserved headroom, and a write that failed. Refusing to write the last free block
on the host is deliberate: it would take the supervisor's own ledger with it.

## What redaction does about it

A retained body is a file on the producer's disk, not part of the document. `docket report --redact`
produces a copy meant to circulate, so it cannot carry a pointer to a body that did not travel with
it, and a copy that kept the pointer would fail the `retained-responses` check above.

So redaction removes the retention fields and records how many, in the receipt's own `redaction`
block:

```json
"retained_responses_not_carried": 2
```

The digest stays. It is not the body, it is 64 characters, and it is exactly what a recipient who is
*given* the body needs in order to prove it is the one the boundary saw. The originals are never
touched.

## Who this is for

| | without retention | with retention |
| --- | --- | --- |
| a provider Docket can read | nothing missing | a byte-for-byte copy of what came back, re-checkable |
| a provider Docket cannot read | tokens estimated from text that is then discarded | the same estimate, plus the text it came from, digest-pinned |
| an incident review | the call, the cost, the policy decision | the above, plus the response body as the agent received it |
| a dispute about what was spent | token counts and a price table | the above, plus a body whose identity anyone can check |

Nothing here parses a provider response. Capturing a response because it was *seen*, rather than
because it was *understood*, is the whole point.
