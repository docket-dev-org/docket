"""Prove tests/test_evidence_retention.py by mutation.

Each mutation is a deliberate defect in the shipped code. A mutation that no test catches is a
guard that does not guard, which is worse than no guard: it reads as coverage while letting the
defect through. Run with ``python3 tools/mutation/mutate_evidence.py`` from the repository root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "tests" / "test_evidence_retention.py"

MUTATIONS: list[tuple[str, str, str, str]] = [
    # (name, file, old, new)
    (
        "digest only covers the kept prefix",
        "src/docket/evidence.py",
        "    total = digest(body)",
        "    total = digest(body[:max(0, max_bytes)])",
    ),
    (
        "retention on by default",
        "src/docket/policy.py",
        "    record_responses: bool = False",
        "    record_responses: bool = True",
    ),
    (
        "a full disk is ignored and the body is written anyway",
        "src/docket/evidence.py",
        "    if min_free_bytes is not None and free is not None and free - need < min_free_bytes:",
        "        if False:",
    ),
    (
        "a write failure is swallowed",
        "src/docket/evidence.py",
        '            note=f"could not write the retained response to {target}: {exc.strerror or exc}",',
        "            note=None,",
    ),
    (
        "a truncated body claims it is whole",
        "src/docket/evidence.py",
        "        truncated=len(kept) < len(body),",
        "        truncated=False,",
    ),
    (
        "the recorded path becomes absolute",
        "src/docket/evidence.py",
        "        return str(Path(path).relative_to(Path(root)))",
        "        return str(path)",
    ),
    (
        "the verifier stops re-hashing retained bodies",
        "src/docket/verify.py",
        "        if ledger_path is not None:\n            _check_retained_responses(events, ledger_path, add)",
        "        if False:\n            _check_retained_responses(events, ledger_path, add)",
    ),
    (
        "the verifier ignores a mismatched digest",
        "src/docket/verify.py",
        "        ok, detail = check_retained(run_root / rel, digest)\n        if not ok:",
        '        ok, detail = (True, "not checked")\n        if not ok:',
    ),
    (
        "redaction leaves the pointer to a body the copy does not carry",
        "src/docket/redact.py",
        '    if data.get("response_retained"):',
        "    if False:",
    ),
    (
        "the policy section is serialised even when off",
        "src/docket/policy.py",
        '        if self.evidence.record_responses:\n            document["evidence"] = self.evidence.to_dict()',
        '        document["evidence"] = self.evidence.to_dict()',
    ),
    (
        "an explicit off loses to a size",
        "src/docket/policy.py",
        '    enabled = _bool_key("evidence", "record_responses", raw.get("record_responses"), bool(other_keys))',
        "    enabled = True",
    ),
    (
        "a nonsensical cap is accepted",
        "src/docket/policy.py",
        "    if cap is None or cap < 1:",
        "    if False:",
    ),
    (
        "a capture whose numbers disagree looks consistent",
        "src/docket/evidence.py",
        "        and (capture.truncated or capture.stored_bytes == capture.total_bytes)",
        "        and True",
    ),
    (
        "a body is filed under something other than its own digest",
        "src/docket/evidence.py",
        '    target = Path(directory) / f"{total}{SUFFIX}"',
        '    target = Path(directory) / f"{len(body)}{SUFFIX}"',
    ),
    (
        "a retention the boundary could not perform is not disclosed on the event",
        "src/docket/proxy.py",
        "            ).to_dict(root=run_root)",
        '            ).to_dict(root=run_root)\n        response_data.pop("response_note", None)',
    ),
]


def run_suite() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(TARGET), "-q", "--no-header", "-x"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]


def main() -> int:
    ok, output = run_suite()
    if not ok:
        print("the suite does not pass on an unmutated tree; fix that first")
        print(output)
        return 2

    escaped = 0
    stale = 0
    for name, relative, old, new in MUTATIONS:
        target = ROOT / relative
        original = target.read_text()
        if original.count(old) != 1:
            # A mutation that was never applied has not been caught and has not escaped; it has not
            # been asked. Counting it beside an escaped mutant reports a failure without saying
            # what, which is how this one survived a refactor that moved an `if` out of a nested
            # block and changed its indentation.
            print(f"STALE {name}: the anchor appears {original.count(old)} times, not once")
            stale += 1
            continue
        target.write_text(original.replace(old, new, 1))
        try:
            passed, output = run_suite()
        finally:
            target.write_text(original)
        if passed:
            print(f"ESCAPED  {name}")
            escaped += 1
        else:
            print(f"caught   {name}")

    print()
    if stale:
        print(
            f"{stale} of {len(MUTATIONS)} mutations were not applied: the anchor no longer matches "
            f"the source. Fix the anchor — an unexercised guard is not a guarded one."
        )
        return 1
    if escaped:
        print(f"{escaped} of {len(MUTATIONS)} mutations escaped")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
