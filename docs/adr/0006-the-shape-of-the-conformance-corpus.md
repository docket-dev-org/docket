# ADR 0006 — How a conformance corpus is shaped, and what it may demand

- **Status:** accepted
- **Date:** 2026-10-17
- **Deciders:** release engineer (acting for the founding maintainer)
- **Supersedes:** the Phase-1 sketch in `NEXT-AGENT-PROMPT.md` §9, which assumed a different layout
  and a different vocabulary. The sketch was written before anything was measured; each change below
  is recorded with the observation that forced it.

## Context

`spec/vectors/` proved that a conforming implementation accepts what it should. Nothing proved that
it **refuses what it must**, which is the whole point of a verifier: an implementation that accepts a
document whose totals do not sum has not implemented this format, it has implemented a JSON reader.

Building the negative corpus forced five decisions that a future implementer will meet, and that a
future maintainer could easily undo by accident. This ADR records them, with the rejected alternative
in each case, because a decision without its rejected alternative is a preference.

## Decisions

### 1. A vector is a document plus an expectation, not a scenario directory

**Decided:** `spec/vectors/invalid/NN-name.json` + `NN-name.expected.json`, where the expectation
names its ledger (`"base.ledger.jsonl"`, `"ledgers/NN-name.jsonl"`, or `null`).

**Rejected:** one directory per vector holding every input it needs.

**Evidence:** of the first 27 vectors, 22 needed only a receipt and the ledger-dependent ones needed
*one* ledger each. A directory tree would have added 27 directories to express what a one-line field
expresses, and the corpus's value is that a stranger can point their own verifier at a single file.

**Cost:** a vector that needs a key (`seal`, `signature`) is still not representable. Those are
exercised by `tests/test_receipt_verify.py`, which generates its material at run time. If key vectors
are added, they need `seal_key.txt`/`public_key.txt` beside the vector and a `"needs_key": true`
field — and `command_for()` in `tools/conformance.py` has to pass the flag. A skip is not coverage.

### 2. A parse-level refusal is a class, not a check id

**Decided:** `parse` appears in `spec/registry.json` with `"emitted": false`. A document that fails
before any check runs is refused with a **non-zero exit, no JSON on stdout, and a reason on stderr**;
the corpus scores the reason, not a verdict.

**Rejected:** emitting a synthetic `check` row for a document the loader could not read.

**Evidence:** `docket verify --json` on a receipt containing `NaN` exits 1 with *empty stdout*. That
is not a bug — the document never reaches the verifier — but a consumer that reads only stdout cannot
tell it from a crash, and a consumer that reports it as "no failing checks" has inverted the meaning
of the whole tool. Making it a class rather than an id keeps the verdict vocabulary honest: an id
means "this assertion failed", and no assertion ran.

### 3. The capability table is derived from output, never written by hand

**Decided:** `spec/registry.json.capabilities` is compared by `tools/conformance.py --capabilities`
against the ids each implementation actually printed across the whole corpus, and a test fails when
they differ.

**Rejected:** a hand-maintained list.

**Evidence:** the first hand-written table said the Node verifier emitted 12 ids. It emitted 18, and
one of them (`cached-totals`) existed in no registry at all. The table had been written by reading the
source, which is exactly how such a table goes wrong — it records what the author believed, not what
the program does.

### 4. A gap is declared, not dressed as a skip

**Decided:** `capabilities.node_conditional` names the ids a verifier does not emit and why. A
conformance run reports them `not-judged`, and `--allow-gaps` is the only way to exit 0 with them.

**Rejected:** having the Node verifier emit `skip` for `chaos-run` and `evidence-completeness`.

**Evidence:** a `skip` asserts "I looked and could not judge". The Node verifier does not look: it has
no implementation of either assertion. Calling that a skip is a false statement about coverage, and
the difference between "none of my checks failed" and "I ran the checks" is the difference this
project exists to make.

### 5. The corpus may demand a *reason*, never an implementation's prose

**Decided:** an expectation names `reason_any_of` — a list of tokens that must appear in the refusal,
and every token must appear in the document itself (`NaN`, `Infinity`, `docket_number`). For
byte-level damage with no nameable token (a BOM, trailing bytes, invalid UTF-8) the list is empty and
the requirement is only that a reason is stated.

**Rejected:** one required word per vector.

**Evidence:** the first version demanded the word `BOM`. Python says "Unexpected UTF-8 BOM"; Node said
"Unexpected token '\ufeff'" and refused **correctly**, and was scored `wrong-reason`. That is the
corpus inventing a dialect and calling a conforming implementation non-conforming — and it would have
been repeated against the Go implementation, which does not exist yet. A corpus written before the
third implementation is a corpus that must not encode the first two's vocabulary.

## Consequences

- `spec/vectors/invalid/` is 36 documents: 30 refusals (24 by a named check, 6 at parse time) and 3
  documents that must be **accepted** — `4e0` where an integer is expected, `-0`, and a decomposed
  Unicode form. The accepted ones exist because a corpus that only says what to refuse lets two
  implementations disagree about what to *read*, which is the first ambiguity a second implementer
  meets.
- Writing the corpus found seven defects in the two verifiers, each now held by a test:
  `usage_source` outside its vocabulary (both), a duplicated JSON name (Node), a re-binding folded
  into a proof check (Node), an id emitted only when it could not apply (Python), `assumed-prices`
  with no failure mode (both), `totals.calls` never compared with the appendix (both), and invalid
  UTF-8 silently rewritten to U+FFFD rather than refused (Node).
- `spec/conformance.md` is normative and this ADR is the reasoning behind it. Neither is a substitute
  for the other: the page says what a conforming implementation must do, this says why it must.
