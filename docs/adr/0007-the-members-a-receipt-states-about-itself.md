# ADR 0007 — The members a receipt states about its own rows

- **Status:** accepted
- **Date:** 2026-10-17
- **Context:** `docket/receipt/v1`, `spec/registry.json`, both verifiers

## The problem

A receipt carries four groups of members that are *derived* from `appendix.usage` rather than
observed: `totals.by_model[]`, the three usage-source counts, and `totals.cost_basis`. The
specification's verification procedure (§8) named only the total, the token totals and the
per-row costs. So these were checked by nothing.

That is not a cosmetic gap. A sweep that rewrote each of them in turn — a model nobody called
added to the breakdown with its cost moved out of a model that was called, every row's
`usage_source` flipped to `reported` beside a matching count, `cost_basis` changed from `mixed`
to `reported` — left `ok: true` every time. The total reconciled because the arithmetic that
checked it never looked at the breakdown; the breakdown is the part a human reads. A receipt
that names a model that was never called, or that presents guessed usage as metered, is a
fabricated record that happens to sum correctly.

## The decision

1. **`by-model-totals`** — `totals.by_model[]` must *partition* the usage rows: every model a
   row used appears exactly once, no entry names a model no row used, and each entry's `calls`,
   `input_tokens`, `cached_input_tokens`, `output_tokens` and `cost_usd` are the sums over that
   model's rows. Order is not checked: the builder sorts by cost, and a verifier that required
   the order would be enforcing a rendering choice.
2. **`usage-source-counts`** — `usage_reported_calls`, `usage_estimated_calls` and
   `asserted_calls` are recounted from the rows. §6.1 already says a consumer MUST NOT present
   estimated usage as metered; a count that says otherwise is the receipt doing exactly that.
3. **`cost-basis`** — a *contradiction* check, not a prose match. The leading word names the
   class (`reported`, `estimated`, `mixed`, `quota mode`) and must agree with the counts. The
   rest of the sentence is free text and is never compared, because a format may not demand one
   implementation's wording. `quota mode` is reported `SKIP`: the meter's mode is a fact about
   the run that the receipt records nowhere else, so a verifier that accepted it would be
   believing an assertion, and one that rejected it would reject every subscription run. The
   third verdict is the honest one, and it is visible.
4. **The corpus says so.** Three negative vectors (`39`, `40`, `41`) and one must-accept (`42`,
   the quota-mode basis) hold both implementations to it. A rule that lives only in a paragraph
   is a rule one implementation can miss.

## Alternatives rejected

- **Check nothing and document that these members are informational.** Tempting, because it is
  true that the format's §8 did not require them. Rejected: the members exist to be read, the
  project's own standard is that no record make a claim it cannot back, and the fix is a sum
  over rows that are already parsed. "Unverified but documented" is what the whole product
  exists to replace.
- **Compare `cost_basis` as an exact string.** Rejected outright: it makes one implementation's
  sentence normative, so a conforming verifier written elsewhere fails on wording — the same
  mistake the corpus was built to avoid when it refused to encode the reference tool's prose.
- **Report the quota class as a pass.** That is what "checking" it would amount to, since
  nothing in the receipt can contradict the claim. A pass that means "I believed it" is worse
  than a skip that means "I could not".
- **Require the meter's mode to be a new receipt member.** Correct in the long run, and out of
  scope for `v1`: it changes the document shape for a case (`quota` mode) that is rare, and a
  version bump is the right vehicle. Recorded here so the next version starts from it rather
  than rediscovering it.
