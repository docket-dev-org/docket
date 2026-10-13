.PHONY: help test demo lint fmt verify-js vectors wheel clean check

help:
	@echo "make test       run the full test suite (offline, no API keys)"
	@echo "make demo       run the runaway-agent demo with the tamper demonstration"
	@echo "make verify-js  run the independent Node verifier against the vectors"
	@echo "make vectors    regenerate spec/vectors (only for a deliberate format change)"
	@echo "make wheel      build sdist + wheel into dist/"
	@echo "make lint fmt   ruff check / ruff format"
	@echo "make check      lint + tests + verify-js"

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
	ruff check src tests spec
	ruff format --check src tests spec

fmt:
	ruff check --fix src tests spec
	ruff format src tests spec

check: lint test verify-js

clean:
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
