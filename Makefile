.PHONY: help test demo lint fmt audit verify-js vectors wheel clean check site site-check

help:
	@echo "make test       run the full test suite (offline, no API keys)"
	@echo "make demo       run the runaway-agent demo with the tamper demonstration"
	@echo "make verify-js  run the independent Node verifier against the vectors"
	@echo "make vectors    regenerate spec/vectors (only for a deliberate format change)"
	@echo "make wheel      build sdist + wheel into dist/"
	@echo "make lint fmt   ruff check / ruff format"
	@echo "make audit      bandit static analysis (suppressions documented in pyproject.toml)"
	@echo "make check      lint + audit + tests + verify-js"
	@echo "make site       build the docs site (needs mkdocs-material)"
	@echo "make site-check build the docs site to a temp dir, strict: catches broken links"

test:
	PYTHONPATH=src python3 -m pytest tests -q

demo:
	PYTHONPATH=src python3 -m docket demo --tamper

verify-js:
	node verify-js/test.mjs

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

audit:
	bandit -q -c pyproject.toml -r src tools

site:
	python3 tools/build_docs_site.py

site-check:
	python3 tools/build_docs_site.py --check

check: lint audit test verify-js

clean:
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
