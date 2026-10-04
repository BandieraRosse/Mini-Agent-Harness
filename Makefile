.DEFAULT_GOAL := run

PYTHON ?= python3
VENV ?= .venv
ARGS ?=

.PHONY: run install

# Keep stdin/stdout attached to the terminal for the enhanced interface.
run: $(VENV)/.installed
	@$(VENV)/bin/python -m miniagent $(ARGS)

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python scripts/install_terminal.py
	@touch $(VENV)/.installed

$(VENV)/.installed: pyproject.toml Makefile scripts/install_terminal.py scripts/build_release.py
	$(MAKE) install
