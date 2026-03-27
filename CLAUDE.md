# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git Workflow

This repo is a fork of `HKUDS/nanobot`. NEVER push to or open PRs against `HKUDS/nanobot` (upstream). ALWAYS push and open PRs against `taobojlen/nanobot` (origin).

## Development Commands

```bash
# Install for development
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test file
pytest tests/test_message_tool.py

# Run a single test by name
pytest tests/test_message_tool.py::test_function_name -v

# Lint
ruff check nanobot/

# Format
ruff format nanobot/

# Run the CLI
nanobot agent -m "Hello"
nanobot gateway
nanobot status
```

Linting: ruff with `line-length = 100`, selects E/F/I/N/W, ignores E501 (long lines). Tests use `pytest-asyncio` with `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).

## Architecture

nanobot is an ultra-lightweight agent framework. The core data flow is:

```
Chat Channel → MessageBus (inbound queue) → AgentRunner → LLM + Tools → MessageBus (outbound queue) → Chat Channel
```

### Key Components

**`nanobot/bus/`** — Async `MessageBus` with two `asyncio.Queue`s (inbound/outbound). Completely decouples channels from the agent. `InboundMessage` and `OutboundMessage` are the wire types.

**`nanobot/agent/loop.py`** — Core LLM tool-call loop with `AgentHook` lifecycle (see `hook.py`). Hooks provide `before_iteration`, `on_stream`, `on_stream_end`, `before_execute_tools`, `after_iteration`.

**`nanobot/agent/runner.py`** — `AgentRunner`: orchestrates the full request lifecycle. Consumes `InboundMessage`s, builds context, runs the loop, saves sessions, publishes `OutboundMessage`s. Handles slash commands and memory consolidation.

**`nanobot/agent/context.py`** — `ContextBuilder` assembles the full LLM prompt: identity + bootstrap files + memory + skills summary. A runtime context block is injected as a separate user message before each user turn.

**`nanobot/agent/memory.py`** — `MemoryStore`: two-layer persistent memory. `MEMORY.md` (long-term facts) + `HISTORY.md` (grep-searchable log).

**`nanobot/agent/skills.py`** — `SkillsLoader`: discovers skills from `workspace/skills/` (higher priority) and bundled `nanobot/skills/`. Skills with `always: true` are auto-loaded; others appear as summaries.

**`nanobot/agent/tools/`** — Built-in tools: `filesystem`, `shell`, `web` (search via Brave, fetch), `message`, `spawn` (background subagent), `cron`, `mcp`. All implement `BaseTool` with `get_definition()` and `execute()`.

**`nanobot/channels/`** — Auto-discovered via `registry.py` (pkgutil scan + entry_points). Each channel defines its own config class (Pydantic model) inside its module. `ChannelsConfig` uses `extra="allow"` — no per-channel fields in `schema.py`. `ChannelManager` validates, instantiates, and routes messages.

**`nanobot/providers/`** — Direct provider implementations (`anthropic_provider.py`, `openai_compat_provider.py`, `azure_openai_provider.py`). `ProviderRegistry` handles auto-detection. Prompt caching (Anthropic) is handled natively in the provider.

**`nanobot/config/schema.py`** — Pydantic models with `alias_generator=to_camel`. `ChannelsConfig` uses `extra="allow"` for dynamic channel configs. Root `Config` uses `pydantic-settings` with `env_prefix="NANOBOT_"`.

**`nanobot/cron/`** — Scheduled tasks with one-time (`at`), interval (`every`), and cron-expression (`cron`) schedules. Workspace-scoped state, timezone support, run history tracking.

### Adding a New Channel

1. Create `nanobot/channels/mychannel.py` with a class extending `BaseChannel`.
2. Define a config class (extending `Base`) inside the module.
3. Add a `default_config()` classmethod and validate dict→config in `__init__`.
4. The channel is auto-discovered by the registry — no changes to `manager.py` or `schema.py` needed.
