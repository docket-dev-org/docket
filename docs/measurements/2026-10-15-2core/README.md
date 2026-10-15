# Measurements: one host, 2026-10-15

Three runs of [`tools/loadtest.py`](../../../tools/loadtest.py), kept as raw reports so that every
number quoted in [`docs/testing.md`](../../testing.md) and
[`docs/production-coverage.md`](../../production-coverage.md) can be checked against the document
that produced it instead of being taken on trust.

**Read the host line before the numbers.** These are measurements of *Docket on this machine*, not
benchmarks of Docket, of a model provider, or of your machine:

| | |
| --- | --- |
| host | 2 vCPUs, ~2 GB RAM, Python 3.13.16, Linux 6.1.158 x86_64 |
| provider | `python -m docket.fakeupstream` on loopback, one scripted step, no network |
| boundary | `docket run` (supervisor + boundary in one process), one usage row per call, fsync per event |
| clock | this container's, with other jobs on the same 2 cores between runs |

The runs answer three different questions, and the reports are deliberately separate artefacts:

| report | question | shape |
| --- | --- | --- |
| `load-report.json` | how fast can the recorder go, and what does a call cost in round trips? | 2,000 calls, 8 threads, as fast as the boundary answers |
| `soak-report.json` | does anything drift or grow over ten minutes of steady traffic? | 12,002 calls, 2 threads, paced to 20 calls/s for 600 s |
| `control-report.json` | is that growth a leak, or the bounded cost of the events recorded? | 6,002 calls, the same rate for half the time |

Every run ends the same way: the receipt is read back, the ledger is located **by the digest the
receipt claims**, the client's count of answered calls is compared with the ledger's rows, and
`docket verify` re-checks the receipt offline. A run whose two sides disagreed, whose rows lacked a
latency, or whose receipt failed to verify would have been reported as *not usable as evidence* and
would not be in this directory.

## The load point

The command is

```bash
make load   # python3 tools/loadtest.py --calls 2000 --concurrency 8 --report load-report.json
```

| measurement | value | source |
| --- | --- | --- |
| calls answered | 2,000 | client, and 2,000 model.response rows in the ledger |
| calls refused | 0 | client, from the boundary's `code` |
| calls errored | 0 | client |
| concurrency | 8 threads, one process | the driver's own argv |
| wall clock | 11.368 s | the orchestrator's clock |
| throughput | 175.93 calls/s | calls answered ÷ wall clock |
| client latency p50 / p95 / p99 | 44 / 48 / 53 ms (max 58 ms) | nearest-rank over 2,000 samples |
| supervisor RSS start / end / peak | 27,220 / 31,408 / 31,408 KiB (11 samples) | `/proc/<pid>/status` once a second, plus `VmHWM` |
| receipt verifies afterwards | yes (7 pass / 0 fail / 4 skip) | `docket verify --json` |

Host: Python 3.13.16 on Linux-6.1.158+-x86_64-with-glibc2.41, 2 cpus. one machine, one supervisor process; not a benchmark of the provider or of any other host

## The ten-minute soak

The command is

```bash
make soak   # python3 tools/loadtest.py --calls 200000 --concurrency 2 --seconds 600 --rate 20
```

| measurement | value | source |
| --- | --- | --- |
| calls answered | 12,002 | client, and 12,002 model.response rows in the ledger |
| calls refused | 0 | client, from the boundary's `code` |
| calls errored | 0 | client |
| concurrency | 2 threads, one process | the driver's own argv |
| wall clock | 600.166 s | the orchestrator's clock |
| throughput | 20.00 calls/s | calls answered ÷ wall clock |
| client latency p50 / p95 / p99 | 4 / 5 / 7 ms (max 35 ms) | nearest-rank over 12,002 samples |
| supervisor RSS start / end / peak | 27,148 / 54,328 / 54,328 KiB (600 samples) | `/proc/<pid>/status` once a second, plus `VmHWM` |
| receipt verifies afterwards | yes (7 pass / 0 fail / 4 skip) | `docket verify --json` |

Host: Python 3.13.16 on Linux-6.1.158+-x86_64-with-glibc2.41, 2 cpus. one machine, one supervisor process; not a benchmark of the provider or of any other host

## The control run: is that growth a leak?

The ten-minute soak ends **+27,180 KiB** above where it started, which is exactly the kind of
number that gets read as a leak and is worth testing rather than arguing about. So the same run was
done again at the same rate for half the time:

| run | window | calls | ledger events | supervisor RSS start → end | growth | per event |
| --- | --- | --- | --- | --- | --- | --- |
| soak (`soak-report.json`) | 600.166 s | 12,002 | 24,014 | 27,148 → 54,328 KiB | +27,180 KiB | 1.132 KiB |
| control (`control-report.json`) | 300.083 s | 6,002 | 12,014 | 27,132 → 40,852 KiB | +13,720 KiB | 1.142 KiB |
| load point (`load-report.json`) | 11.368 s | 2,000 | 4,010 | 27,220 → 31,408 KiB | +4,188 KiB | 1.044 KiB |

Half the duration halves the growth, and the per-event cost lands between **1.04 and 1.14 KiB across
runs spanning a 6× range in length**, with the two paced runs — which differ only in how long they
lasted — agreeing to within 1%. That is the opposite of a leak's signature: what is growing is the
**in-memory list of events** the boundary keeps so it can derive a Merkle root and write a receipt,
bounded by the run's own `max_events` and `max_ledger_bytes` ceilings, and gone when the process
exits. That is a real design consequence, not a reassurance: a run that recorded ten million events
would hold roughly 11 GB in memory before the ceilings stopped it, which is why the ceilings exist
and why [`docs/production-coverage.md`](../../production-coverage.md) lists reading a ledger at that
scale as unmeasured. The measured claim here is narrower and checkable: **memory tracks events
recorded, not time elapsed, at ~1.1 KiB per event, on this host, up to 24,014 events.**

## What these numbers cannot support

* **Not a provider benchmark.** The upstream is scripted and local; a real provider's latency would
  dominate everything here and would say nothing about Docket.
* **Not a load curve.** One machine, one process, one concurrency point (8 threads) and one paced
  rate (20 calls/s). There is no scaling curve, no saturation knee, no queueing analysis, and no
  p99-vs-concurrency plot — claiming any of those from these three runs would be inventing them.
* **Not a leak-free claim.** Ten minutes at 20 calls/s is a bounded test that would catch a gross
  leak; it cannot speak to hours. What it does support is the observation above — memory tracks the
  number of recorded events, bounded by the run's own ceilings — with a control run rather than an
  argument.
* **Not portable.** Another host, another filesystem (the evidence write is `fsync`ed per event),
  and another Python build will give different numbers. The command is in every report; re-running
  it is the only way to get a number about your machine.

## Reproducing

```bash
make load   # 2,000 calls, 8 threads
make soak   # 12,000 calls paced over ten minutes
```

Both write their ledger and receipt next to the report (`--work-dir`), so the evidence for a number
you want to argue about is on disk. `make check` does *not* run either one: a shared CI runner's
throughput would be a fact about the runner. CI runs the harness's own tests instead
(`tests/test_loadtest.py`), including the ones that assert a dropped call, a missing latency, a
receipt that does not verify, or two sides that disagree all make a run *unusable*.

**One version note.** The two paced runs were taken with the harness as it stood when this directory
was written; the load point was re-taken later. The change in between touched only how the memory
sampler waits for the workers to exit — it could add up to a second to a run's wall clock, and it
never touched a count, a latency or a sample. Re-running any of the three against the current tree is
the way to see that for yourself.
