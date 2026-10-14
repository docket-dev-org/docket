"""Drive a real `docket run` against a scripted provider — for producing transcripts.

Not part of the test suite and not shipped behaviour: it is the same trick `docket game-day`
uses (`DOCKET_GAME_DAY_UPSTREAM`), with the fake provider started from this process so the
transcript is a genuine end-to-end run with no API key.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docket.fakeupstream import FakeUpstream, Step  # noqa: E402

DOCKET = str(Path(__file__).resolve().parents[1] / "src")


def main() -> int:
    workspace = Path(sys.argv[1])
    extra = sys.argv[2:]
    upstream = FakeUpstream(
        steps=[
            Step(model="claude-sonnet-5", input_tokens=380_000, output_tokens=95_000),
        ]
    )
    host, port = upstream.start()
    env = dict(os.environ)
    env["PYTHONPATH"] = DOCKET
    env["DOCKET_GAME_DAY_UPSTREAM"] = json.dumps({"openai": f"http://{host}:{port}"})
    env.setdefault("DOCKET_AGENT_CALLS", "6")
    env.setdefault("DOCKET_AGENT_WRITE", "notes.txt")
    env.setdefault("DOCKET_AGENT_MODEL", "claude-sonnet-5")
    command = [
        sys.executable,
        "-m",
        "docket",
        "run",
        "--policy",
        str(workspace / "docket.toml"),
        "--cwd",
        str(workspace),
        *extra,
        "--",
        sys.executable,
        "-m",
        "docket.scriptagent",
    ]
    try:
        completed = subprocess.run(command, env=env, cwd=str(workspace))
    finally:
        upstream.stop()
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
