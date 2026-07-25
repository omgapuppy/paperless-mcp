.PHONY: install format lint typecheck test check run-mcp run-cli build docker-build

install:
	uv sync --dev

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest

check: lint typecheck test build

run-mcp:
	uv run paperless-mcp mcp

run-cli:
	uv run paperless-mcp

build:
	uv build

docker-build:
	docker build --tag paperless-mcp:local .
