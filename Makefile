ROOT := $(shell pwd)
PROTO_DIR := $(ROOT)/proto
PROTO_FILES := $(wildcard $(PROTO_DIR)/harmonograf/v1/*.proto)

SERVER_PB := $(ROOT)/server/harmonograf_server/pb
CLIENT_PB := $(ROOT)/client/harmonograf_client/pb
FRONTEND_PB := $(ROOT)/frontend/src/pb

.PHONY: help proto proto-python proto-ts \
        server-install client-install frontend-install install \
        server-run frontend-dev \
        test server-test client-test frontend-test e2e \
        lint format clean stats

help:
	@echo "Harmonograf Makefile targets:"
	@echo "  proto            Regenerate all proto code (Python for now)"
	@echo "  proto-python     Regenerate Python stubs under server/ and client/"
	@echo "  proto-ts         Regenerate TypeScript stubs under frontend/"
	@echo "  install          Install all components"
	@echo "  server-run       Run the server in dev mode"
	@echo "  frontend-dev     Run the Vite dev server"
	@echo "  test             Run server + client + frontend tests"
	@echo "  e2e              Run the end-to-end test against the ADK submodule"
	@echo "  stats            Query the running server's GetStats RPC"
	@echo "  clean            Remove generated code and build artifacts"

# ---------------------------------------------------------------------------
# Proto codegen
# ---------------------------------------------------------------------------

proto: proto-python proto-ts

# Python: uses grpcio-tools. The --pyi_out flag writes type stubs so mypy
# and IDEs see the generated symbols. An __init__.py is created in the
# output package so it imports cleanly.
proto-python:
	@mkdir -p $(SERVER_PB)/harmonograf/v1 $(CLIENT_PB)/harmonograf/v1
	@uv run --with grpcio-tools --with grpcio --with mypy-protobuf python -m grpc_tools.protoc \
		--proto_path=$(PROTO_DIR) \
		--python_out=$(SERVER_PB) \
		--grpc_python_out=$(SERVER_PB) \
		--pyi_out=$(SERVER_PB) \
		$(PROTO_FILES)
	@uv run --with grpcio-tools --with grpcio --with mypy-protobuf python -m grpc_tools.protoc \
		--proto_path=$(PROTO_DIR) \
		--python_out=$(CLIENT_PB) \
		--grpc_python_out=$(CLIENT_PB) \
		--pyi_out=$(CLIENT_PB) \
		$(PROTO_FILES)
	@touch $(SERVER_PB)/harmonograf/__init__.py $(SERVER_PB)/harmonograf/v1/__init__.py
	@touch $(CLIENT_PB)/harmonograf/__init__.py $(CLIENT_PB)/harmonograf/v1/__init__.py
	@echo "Python proto stubs regenerated."

# TypeScript: deferred. The frontend engineer picks the toolchain
# (@bufbuild/protoc-gen-es + @connectrpc/protoc-gen-connect-es is the
# current recommendation) as part of task #12. This target stays as a
# placeholder so `make proto` does not silently no-op.
proto-ts:
	@if [ -f $(ROOT)/frontend/buf.gen.yaml ]; then \
		cd $(ROOT)/frontend && pnpm exec buf generate; \
		echo "TypeScript proto stubs regenerated under frontend/src/pb/."; \
	else \
		echo "proto-ts: frontend/buf.gen.yaml not present yet — skipping."; \
	fi

# ---------------------------------------------------------------------------
# Installs
# ---------------------------------------------------------------------------

install: server-install client-install frontend-install
	@git submodule update --init --recursive

server-install:
	@cd $(ROOT)/server && uv sync

client-install:
	@cd $(ROOT)/client && uv sync

frontend-install:
	@cd $(ROOT)/frontend && pnpm install --frozen-lockfile

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

server-run:
	@cd $(ROOT)/server && uv run python -m harmonograf_server --store sqlite --data-dir $(ROOT)/data

frontend-dev:
	@cd $(ROOT)/frontend && pnpm dev

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test: server-test client-test frontend-test

server-test:
	@cd $(ROOT)/server && uv run --with pytest --with pytest-asyncio python -m pytest -q

client-test:
	@cd $(ROOT)/client && uv run --with pytest --with pytest-asyncio python -m pytest -q

frontend-test:
	@cd $(ROOT)/frontend && pnpm build && pnpm lint

e2e:
	@cd $(ROOT) && uv run --with pytest --with pytest-asyncio python -m pytest tests/e2e -q

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

lint:
	@cd $(ROOT)/server && uv run --with ruff python -m ruff check . || true
	@cd $(ROOT)/client && uv run --with ruff python -m ruff check . || true
	@cd $(ROOT)/frontend && pnpm lint || true

format:
	@cd $(ROOT)/server && uv run --with ruff python -m ruff format .
	@cd $(ROOT)/client && uv run --with ruff python -m ruff format .
	@cd $(ROOT)/frontend && pnpm exec prettier --write src/ 2>/dev/null || true

stats:
	@cd $(ROOT)/server && uv run python -c "import asyncio; from harmonograf_server.cli_stats import main; asyncio.run(main())"

clean:
	@rm -rf $(SERVER_PB) $(CLIENT_PB) $(FRONTEND_PB)
	@find $(ROOT) -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@find $(ROOT) -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf $(ROOT)/frontend/dist
	@echo "Clean done. Run 'make proto' to regenerate stubs."
