ROOT := $(shell pwd)
PROTO_DIR := $(ROOT)/proto
PROTO_FILES := $(wildcard $(PROTO_DIR)/harmonograf/v1/*.proto)

SERVER_PB := $(ROOT)/server/harmonograf_server/pb
CLIENT_PB := $(ROOT)/client/harmonograf_client/pb
FRONTEND_PB := $(ROOT)/frontend/src/pb

.PHONY: help proto proto-python proto-ts \
        server-install client-install frontend-install install \
        server-run frontend-dev demo demo-presentation .demo-agents-stage \
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

# Run the presentation_agent sample under the canonical `adk web` CLI
# with a harmonograf server at $(HARMONOGRAF_SERVER). Instrumentation
# lives inside presentation_agent/agent.py (the exported `app` attaches
# a harmonograf plugin), so simply running the agent under adk web is
# enough to emit spans.
#
# adk web expects an AGENTS_DIR where each subdirectory is one agent.
# We stage a dedicated .demo-agents/presentation_agent/ as a real
# passthrough package: a symlink would be rejected by ADK's
# _resolve_agent_dir which calls Path.resolve() and refuses any
# resolved path that escapes the agents_root sandbox. The passthrough
# loads the real presentation_agent.agent module by absolute file path
# (not by package import) to avoid colliding with the .demo-agents
# entry that ADK puts on sys.path[0].
#
# Override HARMONOGRAF_SERVER / ADK_WEB_PORT as needed:
#   make demo-presentation ADK_WEB_PORT=8080 HARMONOGRAF_SERVER=127.0.0.1:7531
HARMONOGRAF_SERVER ?= 127.0.0.1:7531
ADK_WEB_PORT ?= 8080
SERVER_PORT ?= 7531
FRONTEND_PORT ?= 5173

# Stage a real (non-symlink) passthrough agent dir at .demo-agents/presentation_agent.
# Regenerated fresh on every invocation so edits to the real module are picked up.
.demo-agents-stage:
	@rm -rf $(ROOT)/.demo-agents
	@mkdir -p $(ROOT)/.demo-agents/presentation_agent
	@printf '' > $(ROOT)/.demo-agents/presentation_agent/__init__.py
	@printf '%s\n' \
		'"""Passthrough so adk web can run presentation_agent under .demo-agents/.' \
		'' \
		'ADK puts .demo-agents/ on sys.path[0], which would otherwise shadow' \
		'the real presentation_agent package at the repo root. We load the' \
		'real module by absolute file path under a private name so ADK still' \
		'resolves agent_dir inside .demo-agents (its safety check rejects' \
		'symlinks that escape the sandbox) while the actual code lives in' \
		'the canonical presentation_agent/ package.' \
		'"""' \
		'from __future__ import annotations' \
		'' \
		'import importlib.util' \
		'from pathlib import Path' \
		'' \
		'_real = Path(__file__).resolve().parents[2] / "presentation_agent" / "agent.py"' \
		'_spec = importlib.util.spec_from_file_location("_real_presentation_agent", _real)' \
		'_mod = importlib.util.module_from_spec(_spec)' \
		'assert _spec.loader is not None' \
		'_spec.loader.exec_module(_mod)' \
		'' \
		'root_agent = _mod.root_agent' \
		'app = _mod.app' \
		> $(ROOT)/.demo-agents/presentation_agent/agent.py

demo-presentation: .demo-agents-stage
	@cd $(ROOT) && HARMONOGRAF_SERVER=$(HARMONOGRAF_SERVER) \
		uv run --extra e2e adk web --host 127.0.0.1 --port $(ADK_WEB_PORT) .demo-agents

# Boot the full Harmonograf demo stack: backend server + Vite frontend +
# adk web presentation_agent. Prints both URLs on startup and kills all
# three children when you Ctrl-C out of the foreground target.
demo: .demo-agents-stage
	@bash -eu -c '\
		cleanup() { \
			echo; \
			echo "[demo] shutting down..."; \
			kill $$SERVER_PID $$FRONTEND_PID $$ADK_PID 2>/dev/null || true; \
			wait 2>/dev/null || true; \
			exit 0; \
		}; \
		trap cleanup INT TERM EXIT; \
		echo "[demo] starting harmonograf-server on :$(SERVER_PORT) ..."; \
		( cd $(ROOT)/server && uv run python -m harmonograf_server \
			--store sqlite --data-dir $(ROOT)/data \
			--port $(SERVER_PORT) ) & SERVER_PID=$$!; \
		echo "[demo] starting frontend Vite dev server on :$(FRONTEND_PORT) ..."; \
		( cd $(ROOT)/frontend && pnpm dev --port $(FRONTEND_PORT) --strictPort ) & FRONTEND_PID=$$!; \
		echo "[demo] starting adk web presentation_agent on :$(ADK_WEB_PORT) ..."; \
		( cd $(ROOT) && HARMONOGRAF_SERVER=127.0.0.1:$(SERVER_PORT) \
			uv run --extra e2e adk web --host 127.0.0.1 --port $(ADK_WEB_PORT) .demo-agents ) & ADK_PID=$$!; \
		sleep 2; \
		echo ""; \
		echo "=================================================================="; \
		echo "  Harmonograf UI     : http://127.0.0.1:$(FRONTEND_PORT)"; \
		echo "  ADK web (agent UI) : http://127.0.0.1:$(ADK_WEB_PORT)"; \
		echo "  harmonograf-server : 127.0.0.1:$(SERVER_PORT) (gRPC)"; \
		echo "=================================================================="; \
		echo "  Press Ctrl-C to stop all three."; \
		echo ""; \
		wait \
	'

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
	@cd $(ROOT) && uv run --extra e2e pytest tests/e2e -q

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
