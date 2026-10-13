"""Shared fixtures. Everything here runs offline: no API keys, no network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from docket.fakeupstream import FakeUpstream, Step  # noqa: E402
from docket.ledger import Ledger  # noqa: E402
from docket.policy import Caps, Policy  # noqa: E402
from docket.pricing import CostMeter, PriceTable  # noqa: E402
from docket.receipt import build_receipt, write_receipt  # noqa: E402


class SteppedClock:
    """A deterministic clock: one fixed timestamp per call."""

    def __init__(self, start: float = 1_792_000_000.0, step: float = 0.5) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> str:
        import time

        self.value += self.step
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.value))


@pytest.fixture()
def clock() -> SteppedClock:
    return SteppedClock()


@pytest.fixture()
def ledger(tmp_path: Path, clock: SteppedClock) -> Ledger:
    led = Ledger.open(tmp_path / "ledger.jsonl", clock=clock)
    yield led
    led.close()


@pytest.fixture()
def price_table() -> PriceTable:
    return PriceTable.load()


@pytest.fixture()
def fake_upstream() -> FakeUpstream:
    upstream = FakeUpstream(steps=[Step(model="claude-sonnet-5", input_tokens=1000, output_tokens=200)])
    upstream.start()
    yield upstream
    upstream.stop()


@pytest.fixture()
def policy() -> Policy:
    return Policy(name="test", caps=Caps(max_usd=None))


@pytest.fixture()
def make_receipt(tmp_path: Path, clock: SteppedClock, price_table: PriceTable):
    """Build a small but complete receipt from a synthetic ledger."""

    def _make(
        *,
        max_usd: str | None = None,
        sign_key: Path | None = None,
        seal_key: str | None = None,
    ) -> tuple[dict, Path, Path]:
        led = Ledger.open(tmp_path / "runs" / "s.jsonl", clock=clock)
        led.append("session.start", {"argv": ["pytest"], "cwd": str(tmp_path), "host": {}})
        pol = Policy(name="t", caps=Caps(max_usd=None if max_usd is None else __import__("decimal").Decimal(max_usd)))
        led.append("policy.applied", {"name": pol.name, "digest": pol.digest(), "document": pol.to_dict()})
        meter = CostMeter(price_table=price_table)
        for index in range(3):
            body = {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]}
            led.append(
                "model.request",
                {"provider": "anthropic", "model": "claude-sonnet-5", "fingerprint": f"fp{index}", "body": body},
            )
            cost = meter.record("claude-sonnet-5", 1000, 0, 200, "reported")
            led.append(
                "model.response",
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-5",
                    "status": 200,
                    "usage": {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 200},
                    "usage_source": "reported",
                    "cost_usd": str(cost),
                },
            )
        led.append("session.end", {"exit_code": 0, "breached": None, "interrupted": False})
        receipt = build_receipt(
            events=list(led),
            ledger=led,
            meter=meter,
            policy=pol,
            session_id="s",
            docket_number="DKT-20260101-ABC123",
            exit_code=0,
            duration_seconds=1.5,
            price_table=price_table,
            seal_key=seal_key,
            sign_key=sign_key,
        )
        path = tmp_path / "receipts" / "DKT-20260101-ABC123.receipt.json"
        write_receipt(receipt, path)
        led.close()
        return receipt, path, tmp_path / "runs" / "s.jsonl"

    return _make


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
