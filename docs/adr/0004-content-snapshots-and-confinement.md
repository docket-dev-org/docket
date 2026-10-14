# ADR 0004 — Content snapshots, and why a restore will not take the tree from a document

- **Status:** accepted
- **Date:** 2026-10-14
- **Deciders:** founding maintainer
- **Applies to:** `src/docket/snapshot.py`, `src/docket/supervisor.py`, `src/docket/cli.py`,
  `src/docket/receipt.py`, `tests/test_snapshot_restore.py`, `tests/test_snapshot_confinement.py`

## Context

Docket's default filesystem discipline is *proof, not prevention*: `fs.snapshot` records a
digest of every watched file before the run, a second pass after it, and the difference lands in
the ledger as `fs.change` events. That answers "what did this run touch?" and it cannot answer
"put it back" — a digest is not a copy.

Automated rollback (the resilience mandate) needs actual bytes. Adding them raised four
questions that the code could not answer for us:

1. **Where do the bytes come from on restore, and who decides what to write?** The list of
   paths to restore is derived from the ledger and the snapshot manifest. Neither document is
   authenticated: a ledger is verified to be *internally consistent*, not to have been written
   by Docket. So a restore that trusts its inputs is a filesystem-write primitive aimed by
   whoever wrote the ledger.
2. **What happens to a file that somebody edited after the run?** The obvious implementation
   ("put the old bytes back") silently destroys post-run work — including the fix for whatever
   the agent broke.
3. **How do we avoid a partial snapshot that looks complete?** A snapshot over budget that
   fills the store until it hits the ceiling leaves files that cannot be restored but which the
   receipt's "snapshot taken" flag implies can be.
4. **How do we keep the store from being a second copy of the repository?** Runs of a
   hundred-file tree, a few times an hour, or ten revisions of the same file per run.

## Decision

1. **Opt-in, and refused up front.** Contents are copied only with `run --snapshot` (or
   `--rollback-on-breach`, which implies it). The tree is measured against the byte budget
   *before* anything is written; over budget the run is refused with both numbers and exits
   `43`. Files the snapshot declines (over the per-file ceiling, symlinks, roots outside the
   working directory) are recorded in `ops.snapshot.not_covered`, so the receipt states what
   *cannot* be undone rather than implying a clean tree.
2. **Content-addressed store** (`snapshots/<session>/blobs/<aa>/<rest>`, `manifest.json`,
   format `docket/snapshot/v1`). Identical bytes are stored once, so repeated revisions of a
   file and identical files in different directories cost one copy. Blobs are written to
   `.partial` and `os.replace`d under their own digest: a blob that appears under a digest is
   the bytes that digest names, or it does not exist.
3. **The plan is derived from the record, never from a live re-walk.** `plan_restore(manifest,
   fs.change events)` yields one action per changed path: `restore`, `recreate`, `remove`, or
   `not_covered` (named, with its reason). It can be produced, printed and audited on a machine
   that has never seen the tree.
4. **Confinement: a restore only touches what is inside the tree it was pointed at.** Every
   path is checked, not trusted — no absolute paths, no `..`, no empty/`.` components, no
   component that is a symbolic link, and the blob location is *derived from the digest*
   (`blobs/<aa>/<rest>` under the store), so a manifest cannot name a file to copy from.
   Digests must be 64 lowercase hex digits; modes are masked to permission bits, so a doctored
   manifest cannot restore a file setuid. A path that fails is reported as `refused` with the
   reason and not touched.
5. **The tree itself is never taken from a document alone.** The receipt, the ledger and the
   manifest all record the run's working directory; `docket restore` refuses to choose when
   they disagree and asks for `--root`, printing the disagreement. `--root` is the operator
   saying out loud which tree they mean.
6. **A file that changed after the run is refused unless `--force`.** Each plan item carries the
   digest the run recorded for that file; the restore measures the file again and refuses on a
   mismatch, naming both digests. The automatic rollback inside a run passes `force=True`
   because it happens milliseconds after the child exits, when the tree is still the run's.
7. **Restore is reported, never asserted.** Per-path statuses (`restored`, `recreated`,
   `removed`, `refused`, `failed`, `absent`, `skipped`) are returned as records; the CLI prints
   each one, exits `1` if any path was refused or failed, and the automatic rollback writes an
   `x-docket.rollback-applied` event with the counts and the paths.

## Consequences

- A breached run can put the tree back, and says what it could not put back. The receipt
  carries `ops.snapshot` (taken, files, bytes stored, not covered) and `ops.rollback` (applied,
  trigger, counts, paths, refusals).
- Restoring is safe to run against a doctored receipt: the worst outcome in the confinement
  tests is a refusal with a reason.
- The store costs, in the worst case, one copy of the watched tree per session — bounded by the
  budget and by content addressing. It is not a version-control system and does not pretend to
  be one: there is no history beyond the sessions that took snapshots.
- A hostile *local* actor who can write into the tree between the check and the write is not
  addressed here. That is a different threat model (the operator has already handed the tree to
  an autonomous process) and is stated as a limit in `docs/threat-model.md` rather than
  implied to be covered.
- `--snapshot` is off by default and stays off by default. A tool that silently copies your
  working tree into `.docket` on every run is not a tool you leave installed.

## Alternatives considered

- **Re-walk the tree and diff it against the manifest at restore time.** Rejected: it makes the
  restore depend on the *current* state of the tree rather than the record, which is both
  unauditable and circular — the thing being restored is the input to the plan.
- **Store full copies per run instead of content-addressed blobs.** Rejected on arithmetic: ten
  revisions of a 2 MiB file is 20 MiB of store rather than 2 MiB, and the budget above which we
  refuse becomes ten times smaller in practice.
- **Take the root from the receipt (`session.cwd`) and restore there.** Rejected: it hands the
  destination of a write primitive to a document anyone can edit. Hence the three-way agreement
  and the explicit `--root`.
- **Follow symlinks during the snapshot.** Rejected: it copies the target's bytes into the
  evidence directory — frequently a key, a `.env`, or a credential in `~/.aws` — and restores
  them later as a regular file. Disclosed in `not_covered` instead.
- **Auto-restore on every breach, without opting in.** Rejected: silently reverting an
  operator's tree is a bigger surprise than an overspend, and the recorded diff already gives
  them the choice afterwards via `docket restore`.
