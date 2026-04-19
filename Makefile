ROOT := $(shell pwd)
PROTO_DIR := $(ROOT)/proto
PROTO_FILES := $(wildcard $(PROTO_DIR)/harmonograf/v1/*.proto)

# Goldfive proto tree. Resolved via the installed goldfive package so the
# path follows the editable dep without hardcoding a clone location; the
# generated stubs in goldfive/pb/ are shared by the harmonograf pbs at
# runtime via goldfive's namespace-package trick (goldfive/pb/__init__.py).
GOLDFIVE_PROTO_DIR := $(shell uv run --no-project python -c "import goldfive, pathlib; print(pathlib.Path(goldfive.__file__).resolve().parent.parent / 'proto')" 2>/dev/null)

SERVER_PB := $(ROOT)/server/harmonograf_server/pb
CLIENT_PB := $(ROOT)/client/harmonograf_client/pb
FRONTEND_PB := $(ROOT)/frontend/src/pb

.PHONY: help proto proto-python proto-ts \
        server-install client-install frontend-install install \
        server-run frontend-dev demo demo-presentation demo-standalone .demo-agents-stage \
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
	@echo "  demo             Boot server + frontend + goldfive-orchestrated ADK agent"
	@echo "  demo-standalone  Boot server + frontend and emit spans from the standalone example"
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
	@if [ -z "$(GOLDFIVE_PROTO_DIR)" ]; then \
		echo "ERROR: could not resolve goldfive proto directory. Run 'uv sync' first."; \
		exit 1; \
	fi
	@mkdir -p $(SERVER_PB)/harmonograf/v1 $(CLIENT_PB)/harmonograf/v1
	@uv run --with grpcio-tools --with grpcio --with mypy-protobuf python -m grpc_tools.protoc \
		--proto_path=$(PROTO_DIR) \
		--proto_path=$(GOLDFIVE_PROTO_DIR) \
		--python_out=$(SERVER_PB) \
		--grpc_python_out=$(SERVER_PB) \
		--pyi_out=$(SERVER_PB) \
		$(PROTO_FILES)
	@uv run --with grpcio-tools --with grpcio --with mypy-protobuf python -m grpc_tools.protoc \
		--proto_path=$(PROTO_DIR) \
		--proto_path=$(GOLDFIVE_PROTO_DIR) \
		--python_out=$(CLIENT_PB) \
		--grpc_python_out=$(CLIENT_PB) \
		--pyi_out=$(CLIENT_PB) \
		$(PROTO_FILES)
	@touch $(SERVER_PB)/harmonograf/__init__.py $(SERVER_PB)/harmonograf/v1/__init__.py
	@touch $(CLIENT_PB)/harmonograf/__init__.py $(CLIENT_PB)/harmonograf/v1/__init__.py
	@echo "Python proto stubs regenerated."

# TypeScript: buf v2 + @bufbuild/protoc-gen-es. Resolves the goldfive
# proto tree the same way proto-python does (via the installed goldfive
# package) so telemetry.proto / frontend.proto can `import "goldfive/v1/
# events.proto"`. The goldfive tree is exposed to buf as a symlinked
# vendor dir under frontend/; the symlink is gitignored and refreshed
# every run, so the checkout tracks the editable dep without committing
# a hardcoded path.
FRONTEND_VENDOR_PROTO := $(ROOT)/frontend/vendor-proto

proto-ts:
	@if [ ! -f $(ROOT)/frontend/buf.gen.yaml ]; then \
		echo "proto-ts: frontend/buf.gen.yaml not present yet — skipping."; \
		exit 0; \
	fi
	@if [ -z "$(GOLDFIVE_PROTO_DIR)" ]; then \
		echo "ERROR: could not resolve goldfive proto directory. Run 'uv sync' first."; \
		exit 1; \
	fi
	@rm -rf $(FRONTEND_VENDOR_PROTO)
	@mkdir -p $(FRONTEND_VENDOR_PROTO)
	@ln -sfn $(GOLDFIVE_PROTO_DIR)/goldfive $(FRONTEND_VENDOR_PROTO)/goldfive
	@cd $(ROOT)/frontend && pnpm exec buf generate
	@echo "TypeScript proto stubs regenerated under frontend/src/pb/."

# ---------------------------------------------------------------------------
# Installs
# ---------------------------------------------------------------------------

install: server-install client-install frontend-install

server-install:
	@cd $(ROOT) && uv sync

client-install:
	@cd $(ROOT) && uv sync

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
# lives inside tests/reference_agents/presentation_agent/agent.py (the exported `app` attaches
# a harmonograf plugin), so simply running the agent under adk web is
# enough to emit spans.
#
# adk web expects an AGENTS_DIR where each subdirectory is one agent.
# We stage a dedicated .demo-agents/presentation_agent/ as a real
# passthrough package: a symlink would be rejected by ADK's
# _resolve_agent_dir which calls Path.resolve() and refuses any
# resolved path that escapes the agents_root sandbox. The passthrough
# loads the real presentation_agent.agent module by absolute file path
# from tests/reference_agents/presentation_agent/agent.py (not by package
# import) to avoid colliding with the .demo-agents entry that ADK puts
# on sys.path[0].
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
		'the real presentation_agent package under tests/reference_agents/.' \
		'We load the real module by absolute file path under a private name' \
		'so ADK still resolves agent_dir inside .demo-agents (its safety' \
		'check rejects symlinks that escape the sandbox) while the actual' \
		'code lives in tests/reference_agents/presentation_agent/.' \
		'"""' \
		'from __future__ import annotations' \
		'' \
		'import importlib.util' \
		'from pathlib import Path' \
		'' \
		'_real = Path(__file__).resolve().parents[2] / "tests" / "reference_agents" / "presentation_agent" / "agent.py"' \
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
		OPENAI_API_KEY="$${OPENAI_API_KEY:-dummy}" \
		uv run --extra demo adk web --host 127.0.0.1 --port $(ADK_WEB_PORT) .demo-agents

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
			OPENAI_API_KEY="$${OPENAI_API_KEY:-dummy}" \
			uv run --extra demo adk web --host 127.0.0.1 --port $(ADK_WEB_PORT) .demo-agents ) & ADK_PID=$$!; \
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

# Standalone observability demo: boots the harmonograf server + Vite
# frontend and runs examples/standalone_observability/spans_only.py to
# push synthetic spans at them. No goldfive orchestration is involved —
# this is the "use harmonograf without goldfive" demo.
demo-standalone:
	@bash -eu -c '\
		cleanup() { \
			echo; \
			echo "[demo-standalone] shutting down..."; \
			kill $$SERVER_PID $$FRONTEND_PID 2>/dev/null || true; \
			wait 2>/dev/null || true; \
			exit 0; \
		}; \
		trap cleanup INT TERM EXIT; \
		echo "[demo-standalone] starting harmonograf-server on :$(SERVER_PORT) ..."; \
		( cd $(ROOT)/server && uv run python -m harmonograf_server \
			--store sqlite --data-dir $(ROOT)/data \
			--port $(SERVER_PORT) ) & SERVER_PID=$$!; \
		echo "[demo-standalone] starting frontend Vite dev server on :$(FRONTEND_PORT) ..."; \
		( cd $(ROOT)/frontend && pnpm dev --port $(FRONTEND_PORT) --strictPort ) & FRONTEND_PID=$$!; \
		sleep 3; \
		echo ""; \
		echo "=================================================================="; \
		echo "  Harmonograf UI     : http://127.0.0.1:$(FRONTEND_PORT)"; \
		echo "  harmonograf-server : 127.0.0.1:$(SERVER_PORT) (gRPC)"; \
		echo "=================================================================="; \
		echo "  Emitting spans via examples/standalone_observability/spans_only.py ..."; \
		echo ""; \
		( cd $(ROOT) && HARMONOGRAF_SERVER=127.0.0.1:$(SERVER_PORT) \
			uv run python examples/standalone_observability/spans_only.py ); \
		echo ""; \
		echo "[demo-standalone] spans emitted. Press Ctrl-C to stop server + frontend."; \
		wait \
	'

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

test: server-test client-test frontend-test

server-test:
	@cd $(ROOT) && uv run --extra e2e --with pytest --with pytest-asyncio python -m pytest -q server/tests/

client-test:
	@cd $(ROOT) && uv run --extra e2e --with pytest --with pytest-asyncio python -m pytest -q client/tests/

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
