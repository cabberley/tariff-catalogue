PYTHON ?= python
DRY_RUN ?= 0
export DRY_RUN

.PHONY: lint typecheck test harvest-au-cdr publish archive-ls

lint:
	ruff check .

typecheck:
	mypy

test:
	PYTHONPATH=src pytest

harvest-au-cdr:
	PYTHONPATH=src $(PYTHON) -m tariff_catalogue.cli harvest au-cdr

publish:
	PYTHONPATH=src $(PYTHON) -m tariff_catalogue.cli publish

archive-ls:
	PYTHONPATH=src $(PYTHON) -m tariff_catalogue.cli archive-ls "$(PREFIX)"
