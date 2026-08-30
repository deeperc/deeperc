# DeepERC — public runbook targets
#
# The private repo's Makefile has 12 targets, every one of which reaches a
# private-tier path or tool (the bad-corpus generator, the recall harness, the
# board corpus, saved baselines, logs/). None of them have a public equivalent,
# so this is a fresh file rather than a filtered copy of that one.
#
# Python env: the plain `venv/` layout the README's install section documents
# (`python3 -m venv venv && pip install -r requirements.txt`).
#
# Run `make` or `make help` for the target list.

SHELL := /bin/bash
PY    := venv/bin/python3

.DEFAULT_GOAL := help

.PHONY: help test

help:  ## Show this target list
	@echo "DeepERC runbook targets  (python env: $(PY))"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) \
	  | sed -E 's/^([a-zA-Z0-9_-]+):.*## /  make \1\t/' \
	  | expand -t34
	@echo ""

test:  ## Test gate — pytest minus the external-dependency tests
	@test -x $(PY) || { \
	  echo "ERROR: $(PY) not found."; \
	  echo "  Create the venv first, per the README's Install section:"; \
	  echo "    python3 -m venv venv && pip install -r requirements.txt"; \
	  exit 1; }
	@$(PY) -c "import pytest" 2>/dev/null || { \
	  echo "Installing test dependencies (requirements-dev.txt)..."; \
	  $(PY) -m pip install -q -r requirements-dev.txt || { \
	    echo "ERROR: could not install test dependencies. Install them manually:"; \
	    echo "    $(PY) -m pip install -r requirements-dev.txt"; \
	    exit 1; }; }
	cd schematic_checker_poc && set -o pipefail && \
	  ../$(PY) -m pytest -q -m "not integration and not gemma_smoke"
