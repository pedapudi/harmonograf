# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project vision

Harmonograf is a console for understanding, interacting with, and coordinating multi-agent frameworks. The repository is currently a blank slate (only a README) — architectural decisions below are intent, not yet implemented.

## High-level architecture

Three components are planned, and changes usually span more than one of them:

1. **Visual frontend** — a Gantt-chart-style view. X-axis is time, Y-axis is one row per agent, and each block represents an agent activity (e.g., a tool call, a step, a span). Blocks are interactive (clickable) to drill into details. This is the human-facing surface for observing and coordinating agents.

2. **Client library** — embedded inside agent implementations to emit activity to the server. It must be compatible with ADK (Google's Agent Development Kit) as a first-class integration target, so its data model and hooks should map cleanly onto ADK concepts. Multiple agents (multiple processes) will use the client library concurrently.

3. **Server process** — hosts the visualization frontend and terminates connections from client libraries across all participating agents. It is the fan-in point: many clients, one server, one UI. It owns the canonical timeline and is the bridge that lets the frontend coordinate agents (not just observe them).

Key cross-cutting concerns to keep in mind when designing any piece:
- The data model (agent, activity/block, time range, metadata payload) is shared across all three components and should be defined once.
- The frontend is not read-only — interactions flow back through the server to clients, so the client library needs a bidirectional channel, not just telemetry egress.
- "Coordinating" implies the server may mediate control, not just display — design client APIs with that in mind rather than treating it purely as an observability tool.
