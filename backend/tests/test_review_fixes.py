"""Targeted tests for the PR #3 review fixes.

These tests cover only the behaviors the reviewers flagged — they're not a
complete suite. The full app is not booted; we import the module and exercise
helpers and (where convenient) the FastAPI app via TestClient with the LLM
network calls patched out.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest import mock

# Make backend importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Provide bare-minimum env so module import doesn't blow up on Anthropic init
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("LLM_ENDPOINT_KEY", "test-llm-key")
os.environ.setdefault("DID_AGENT_ID", "agt_test")
os.environ.setdefault("DID_CLIENT_KEY", "ck_test")

import main  # noqa: E402


def test_strip_markdown_chunk_removes_blacklist_chars():
    """Per-chunk strip must work on tiny fragments where regex-based stripping
    fails because emphasis is split across chunk boundaries."""
    # Simulate the boundary-split case
    assert main.strip_markdown_chunk("**") == ""
    assert main.strip_markdown_chunk("Wort") == "Wort"
    assert main.strip_markdown_chunk("*bold*") == "bold"
    assert main.strip_markdown_chunk("# header") == " header"
    assert main.strip_markdown_chunk("`code`") == "code"
    assert main.strip_markdown_chunk("plain text") == "plain text"


def test_messages_for_anthropic_always_drops_claim_marker():
    """CLAIM must never reach Claude — even on later turns when drop_claim
    was conceptually False in the old code."""
    msgs = [
        {"role": "user", "content": "CLAIM:ctx_abc12345"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "Wie geht's?"},
    ]
    # inject_greeting=False (later turn): claim still gone
    out = main._messages_for_anthropic(msgs, inject_greeting=False)
    serialized = " | ".join(m["content"] for m in out)
    assert "CLAIM:" not in serialized
    # Should still keep the real user/assistant content
    assert any("Wie geht" in m["content"] for m in out)


def test_messages_for_anthropic_injects_greeting_only_on_first_turn():
    """When inject_greeting=True and only CLAIM was sent, a benign opener
    is appended so the model still produces a greeting."""
    msgs = [{"role": "user", "content": "CLAIM:ctx_abc12345"}]
    out = main._messages_for_anthropic(msgs, inject_greeting=True)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "begrüße" in out[0]["content"].lower()

    # And NOT on later turns
    out2 = main._messages_for_anthropic(msgs, inject_greeting=False)
    # CLAIM dropped, no greeting injected → empty list
    assert out2 == []


def test_messages_for_anthropic_does_not_double_inject():
    """If a real user message follows the CLAIM, we must NOT inject the
    greeting opener on top of it."""
    msgs = [
        {"role": "user", "content": "CLAIM:ctx_abc12345"},
        {"role": "user", "content": "Hallo!"},
    ]
    out = main._messages_for_anthropic(msgs, inject_greeting=True)
    user_msgs = [m for m in out if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "Hallo!"


def test_is_first_turn_only_true_when_claim_consumed(monkeypatch):
    """The race condition: D-ID sends full history every call. Once we've
    popped the claim from CLAIM_STORE, the second call (with same history)
    must return is_first_turn=False, not True."""
    import asyncio

    main.CLAIM_STORE.clear()
    main.DISTINCT_STORE.clear()
    main.CLAIM_STORE["ctx_test12345678"] = {
        "notion_id": None,
        "expires_at": main._now() + 60,
    }
    msgs = [{"role": "user", "content": "CLAIM:ctx_test12345678"}]

    # First call: claim is in store → consumed → first turn
    guest1, first1 = asyncio.run(main._resolve_guest_for_request(msgs, "distinct_1"))
    assert first1 is True

    # Second call with the SAME messages: claim already consumed (popped) →
    # must NOT be treated as first turn anymore.
    guest2, first2 = asyncio.run(main._resolve_guest_for_request(msgs, "distinct_1"))
    assert first2 is False


def test_is_first_turn_false_when_no_claim():
    import asyncio
    msgs = [{"role": "user", "content": "Hello"}]
    guest, first = asyncio.run(main._resolve_guest_for_request(msgs, None))
    assert first is False


def test_strip_markdown_full_text_still_intact():
    """The full-text strip used by /api/chat should still work end-to-end."""
    out = main.strip_markdown("**bold** and *italic* and `code`")
    # No markdown markers should remain after the regex pass.
    assert "**" not in out
    assert "`" not in out
