.PHONY: help game-day selftest helm-fidelity test demo lint fmt sync sync-check audit mutation verify-js vectors conformance verify-go wheel clean check site site-check a11y deploy-check load soak cache-load

help:
	@echo "make test       run the full test suite (offline, no API keys)"
	@echo "make demo       run the runaway-agent demo with the tamper demonstration"
	@echo "make verify-js  run the independent Node verifier against the vectors"
	@echo "make vectors    regenerate spec/vectors (only for a deliberate format change)"
	@echo "make conformance run the negative corpus against both verifiers, and hold the registry to what they emit"
	@echo "make verify-go  vet, gofmt, test and build the clean-room Go verifier (needs Go 1.27)"
	@echo "make wheel      build sdist + wheel into dist/"
	@echo "make lint fmt   ruff check / ruff format"
	@echo "make sync       rewrite every number the docs quote about the suite and the version"
	@echo "make sync-check refuse a tree whose quoted numbers have drifted (CI)"
	@echo "make audit      bandit static analysis (suppressions documented in pyproject.toml)"
	@echo "make mutation   mutate the source, run the tests, report every survivor (slow)"
	@echo "make check      lint + audit + tests + verify-js + conformance + deploy-check"
	@echo "make selftest   19 probes: the boundary, exercised in this process (6 s)"
	@echo "make game-day   every failure phase, end to end, with a post-mortem (~20 s)"
	@echo "make site       build the docs site (needs mkdocs-material)"
	@echo "make site-check build the docs site to a temp dir, strict: catches broken links"
	@echo "make a11y       build the site and check its HTML (language, headings, alt, names, ids)"
	@echo "make deploy-check check deploy/ statically (no container runtime needed; none is present)"
	@echo "make helm-fidelity compare the checker's chart render against real Helm (needs helm; exits 2 without it)"
	@echo "make load       measure throughput and latency through a real boundary (a number about this host)"
	@echo "make soak       measure memory and drift over 10 minutes (same caveat, longer)"
	@echo "make cache-load the same load point with the response cache on: what a hit saves, and what it returns"

test:
	# No `-q` here: `pyproject.toml`'s `addopts` already carry one, and a second asks pytest for
	# `-qq` — at which level it prints no counts line, so `make test` would end without saying how
	# many tests passed. The summary line is the point of the command; `-ra` adds the skip reasons
	# in the cells that have skips.
	PYTHONPATH=src python3 -m pytest tests -ra

demo:
	PYTHONPATH=src python3 -m docket demo --tamper

verify-js:
	node verify-js/test.mjs

# The corpus is the format's contract, and this target is the only place both halves are run against
# it. `--capabilities` is not decoration: it compares the registry's promise about which ids each
# verifier emits with the ids they actually emitted, so a check that quietly disappears (or one that
# only ever fires on an empty receipt) fails here rather than at a stranger's desk.
conformance:
	PYTHONPATH=src python3 tools/conformance.py --python --node
	@if [ -x verify-go/docket-verify ]; then PYTHONPATH=src python3 tools/conformance.py --go; fi
	PYTHONPATH=src python3 tools/conformance.py --capabilities --python --node
	@if [ -x verify-go/docket-verify ]; then PYTHONPATH=src python3 tools/conformance.py --capabilities --go; fi

# The third implementation is a check on the *specification*, not on the other two: it is
# written from spec/ only, and the corpus is what holds it to the same answers.
verify-go:
	cd verify-go && GOFLAGS=-mod=readonly GOPROXY=off go vet ./... && test -z "$$(gofmt -l .)" && GOPROXY=off go test ./...
	cd verify-go && GOFLAGS=-mod=readonly GOPROXY=off go build -o docket-verify .


vectors:
	PYTHONPATH=src python3 spec/vectors/generate.py

wheel:
	python3 -m build

lint:
	ruff check src tests spec examples tools assets
	ruff format --check src tests spec examples tools assets

fmt:
	ruff check --fix src tests spec examples tools assets
	ruff format src tests spec examples tools assets
	python3 tools/sync_counts.py

sync:
	python3 tools/sync_counts.py

sync-check:
	python3 tools/sync_counts.py --check

audit:
	bandit -q -c pyproject.toml -r src tools

site:
	python3 tools/build_docs_site.py

site-check:
	python3 tools/build_docs_site.py --check

a11y: # build the site, then check it for the accessibility defects a program can decide
	python3 tools/a11y_check.py --build

# The mutation sweep is slow on purpose: every mutant is a full pytest run over the module's
# declared subset, and the honest answer to "does the suite notice" is worth minutes, not seconds.
# It is not part of `make check`. Reports are written to the working directory, not committed.
mutation:
	python3 tools/mutate.py --json mutation-report.json --markdown mutation-report.md

# Static validation of the container/manifest/chart recipes. It deliberately passes --json so the
# result is machine-readable, and it exits 2 rather than 0 when PyYAML is missing: a check that could
# not run must never look like a check that passed.
helm-fidelity:
	python3 tools/helm_fidelity.py

deploy-check:
	PYTHONPATH=src python3 tools/deploy_check.py

# A load point and a soak are measurements, not assertions: the number depends on the host, and a
# shared CI runner's throughput would be a fact about the runner. So they are opt-in targets whose
# reports land in a file, and CI runs the *tests* of the harness instead.
load:
	python3 tools/loadtest.py --calls 2000 --concurrency 8 --report load-report.json --markdown load-report.md

soak:
	python3 tools/loadtest.py --calls 200000 --concurrency 2 --seconds 600 --rate 20 \
		--work-dir .docket-soak --report soak-report.json --markdown soak-report.md

# The same load with the response cache on. The ceiling is high enough that nothing in this run
# reaches the brake, so the number is "what the cache answers", not "what it stops answering".
cache-load:
	python3 tools/loadtest.py --calls 2000 --concurrency 8 --cache-hits 5000 \
		--report cache-report.json --markdown cache-report.md

game-day:
	PYTHONPATH=src python3 -m docket game-day --out .docket/game-day

selftest:
	PYTHONPATH=src python3 -m docket selftest

check: lint audit test verify-js conformance deploy-check

clean:
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
