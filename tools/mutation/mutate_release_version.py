"""Mutation: the tag/version disagreement is reported but not fatal.

Anchor-anchored so a workflow reformat cannot silently turn this into a no-op SKIP: the anchor
is counted before it is used, and a miss aborts. Raises rather than asserting, because
`tools/` is bandit-scanned and `assert` is compiled out under `-O`.

Run from the repository root.
"""

from pathlib import Path

WORKFLOW = Path(".github/workflows/release.yml")

OLD = (
    '            echo "tag ${GITHUB_REF_NAME} does not name the version pyproject.toml '
    'declares (${VERSION}); refusing to publish" >&2\n'
    "            exit 1\n"
)

NEW = '            echo "tag ${GITHUB_REF_NAME} disagrees with pyproject.toml"\n'


def main() -> int:
    text = WORKFLOW.read_text(encoding="utf-8")
    found = text.count(OLD)
    if found != 1:
        raise SystemExit(f"anchor appears {found} times, expected 1; refusing to mutate")
    WORKFLOW.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print("mutation applied: disagreement reported, not fatal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
