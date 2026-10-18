# Support

Docket is a small project with no support staff and **no service-level agreement**. This page says
what help exists, where to ask for it, and what to include — so that a good report can be answered
once instead of over six messages. It is deliberately short.

## Before you ask

Most reports are answered by one of these, and each one prints what it observed rather than a verdict:

| symptom | run this first |
| --- | --- |
| anything unexplained | `docket doctor` — the health of this machine *and* of the ledgers already on it (`--json` to paste into an issue) |
| "is this install working at all?" | `docket selftest` — 19 probes that use the product the way a deployment does: forward a call, refuse a denied model, cross a cap, trip a breaker, inject a fault, hit the cache and miss it, verify a receipt and catch a tampered one |
| a receipt that will not verify | `docket verify <receipt> --json` — every check with its verdict and the sentence behind it |
| a run that spent more than expected | `docket explain <receipt>` — the findings, with the events each one rests on |
| "the docs say X and the tool does Y" | `docket --version`, then the document's path. That mismatch is a bug in one of the two, and worth reporting |

## Where to ask

| what you have | where it goes | notes |
| --- | --- | --- |
| something that should work and does not | [bug report](https://github.com/shivanshjain456/docket/issues/new?template=bug.yml) | the fastest path; the template asks for the four things below |
| a feature request, or an objection to a design decision | [feature request](https://github.com/shivanshjain456/docket/issues/new?template=feature.yml) | "this estimate is wrong" reports are welcome here too |
| a question, or a "how would I…" | [Discussions](https://github.com/shivanshjain456/docket/discussions), or [an issue](https://github.com/shivanshjain456/docket/issues/new) if that tab is not there yet | `CONTRIBUTING.md` and `docs/faq.md` both route questions here, and a question asked as an issue is answered, not closed |
| a security problem | [SECURITY.md](SECURITY.md), privately | **not** an issue — please do not open one for a vulnerability |
| supporting the project, procurement, invoices | [SPONSORS.md](SPONSORS.md) | what support pays for and what it does not buy |

## What to include in a bug report

1. **What you ran**, verbatim, and what you expected instead.
2. **`docket --version`**, your Python version, and your operating system.
3. **The smallest input that reproduces it** — a policy file, a receipt, a ledger line, or an
   `examples/` directory you edited. Receipts and ledgers record prompts, responses and paths, so
   redact anything you would not put in a public issue; a receipt can also be *shrunk* by hand,
   because verification reads a JSON document rather than a live run.
4. **`docket doctor --json`** from the machine, and whether `make check` is green there.

A report that carries those four is usually diagnosable without a round trip. A report that says
"it doesn't work" cannot be answered except by asking for all four.

## What this project cannot help with

- **Your provider account**: quotas, rate limits, billing disputes, key rotation. Those belong to
  OpenAI, Anthropic, Google or whoever you bought from. Docket records what your agent spent; it
  does not mediate the bill.
- **Your agent framework's internals** — but if the boundary failed to record a call the framework
  made, that *is* a Docket bug and worth an issue (see
  [docs/production-coverage.md](docs/production-coverage.md) for the honest state of HTTP/2, gRPC
  and streaming coverage).
- **Kubernetes support.** The repository ships manifests, a chart and a static checker for them, and
  `helm template` has been run against the chart; **no cluster has ever been used here**, and nothing
  in the verification claims otherwise. See [docs/deployment.md](docs/deployment.md).
- **A promise about a date.** There is no roadmap commitment attached to a request unless it is in
  [ROADMAP.md](ROADMAP.md).

## Response times, honestly

There is no SLA, no paid support tier, and no on-call rota. Issues are read by the maintainers when
they can be, and the ones that come with a reproduction go first because they are the ones that can be
answered. **A sponsorship does not buy support** unless a separate written agreement says so, and
[SPONSORS.md](SPONSORS.md) states it in the same words. This page exists because that sentence has to
be findable by the person about to be disappointed: a
funding page that quietly implies support hours would be selling something the project does not have.
