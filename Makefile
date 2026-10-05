.PHONY: install all test lint check

install:
	pip install -e ".[dev]"

all:            ## reproduce every result, figure and doc table
	python -m tlr all

test:
	python -m pytest -q

lint:
	ruff check src tests

check: lint test all
	python -m tlr check-docs
