.PHONY: help test lint fmt typecheck check demo clean install

VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
export PYTHONPATH := src:tests

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install the package with dev tooling
	python3 -m venv $(VENV)
	$(PIP) install -e ".[dev]"

test:  ## Run the test-suite
	$(PY) -m unittest discover -s tests -v

lint:  ## Lint with ruff
	$(VENV)/bin/ruff check .

fmt:  ## Format with ruff
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

typecheck:  ## Static types with mypy
	$(VENV)/bin/mypy src/studybuddy

check: lint typecheck test  ## Everything CI runs
	$(VENV)/bin/ruff format --check .

demo:  ## Run the offline end-to-end demo (no API key)
	$(PY) examples/offline_demo.py

clean:  ## Remove caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .ruff_cache .mypy_cache dist build *.egg-info
