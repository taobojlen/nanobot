"""Tests for cache_control injection in LiteLLMProvider."""

from __future__ import annotations

from typing import Any

import pytest

from nanobot.providers.litellm_provider import LiteLLMProvider


def _provider() -> LiteLLMProvider:
    return LiteLLMProvider(api_key="test", default_model="claude-sonnet-4-6")


def _runtime_ctx() -> dict[str, Any]:
    return {"role": "user", "content": "[Runtime Context\nCurrent Time: 2026-01-01 12:00 (Thursday)"}


def _has_cache(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    if isinstance(content, list):
        return any(block.get("cache_control") == {"type": "ephemeral"} for block in content)
    return False


def _apply(messages, tools=None):
    return LiteLLMProvider.__new__(LiteLLMProvider)._apply_cache_control.__func__(
        _provider(), messages, tools
    )


# ---------------------------------------------------------------------------
# Helper that calls _apply_cache_control directly
# ---------------------------------------------------------------------------

def apply_cache(messages, tools=None):
    p = _provider()
    return p._apply_cache_control(messages, tools)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_system_message_is_always_cached():
    messages = [
        {"role": "system", "content": "You are helpful."},
        _runtime_ctx(),
        {"role": "user", "content": "Hello"},
    ]
    result, _ = apply_cache(messages)
    assert _has_cache(result[0])


def test_no_history_only_system_cached():
    """With no prior history, only the system message gets a cache breakpoint."""
    messages = [
        {"role": "system", "content": "You are helpful."},
        _runtime_ctx(),
        {"role": "user", "content": "Hello"},
    ]
    result, _ = apply_cache(messages)
    assert _has_cache(result[0])         # system
    assert not _has_cache(result[1])     # runtime ctx — not cached
    assert not _has_cache(result[2])     # current user message — not cached


def test_last_history_message_is_cached():
    """The last history message should get a second cache breakpoint."""
    history = [
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4."},
    ]
    messages = [
        {"role": "system", "content": "You are helpful."},
        *history,
        _runtime_ctx(),
        {"role": "user", "content": "And 3+3?"},
    ]
    result, _ = apply_cache(messages)

    assert _has_cache(result[0])     # system
    assert not _has_cache(result[1]) # first history user — not the last
    assert _has_cache(result[2])     # last history message (assistant "4.")
    assert not _has_cache(result[3]) # runtime ctx — not cached
    assert not _has_cache(result[4]) # current user message — not cached


def test_runtime_ctx_and_current_user_never_cached():
    """Volatile messages must never get cache_control."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prior turn"},
        {"role": "assistant", "content": "prior reply"},
        _runtime_ctx(),
        {"role": "user", "content": "new message"},
    ]
    result, _ = apply_cache(messages)
    assert not _has_cache(result[3])  # runtime ctx
    assert not _has_cache(result[4])  # current user


def test_tool_messages_after_runtime_ctx_not_cached():
    """Tool call/result messages appended during the loop should not be cached."""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "prior turn"},
        {"role": "assistant", "content": "prior reply"},
        _runtime_ctx(),
        {"role": "user", "content": "run a tool"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "name": "shell", "content": "done"},
    ]
    result, _ = apply_cache(messages)
    assert not _has_cache(result[5])  # assistant tool call
    assert not _has_cache(result[6])  # tool result


def test_tools_last_item_cached():
    tools = [
        {"name": "tool_a", "description": "a"},
        {"name": "tool_b", "description": "b"},
    ]
    messages = [{"role": "system", "content": "sys"}, _runtime_ctx(), {"role": "user", "content": "hi"}]
    _, result_tools = apply_cache(messages, tools)
    assert result_tools[-1].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in result_tools[0]


def test_list_content_history_message_cached():
    """History messages with list content (e.g. multimodal) should be handled."""
    history_msg = {
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            {"type": "text", "text": "What's in this image?"},
        ],
    }
    messages = [
        {"role": "system", "content": "sys"},
        history_msg,
        {"role": "assistant", "content": "A cat."},
        _runtime_ctx(),
        {"role": "user", "content": "describe more"},
    ]
    result, _ = apply_cache(messages)
    assert _has_cache(result[2])  # last history message (assistant)
