"""The company brain: read over MCP with a read-only token, never required."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from app.agent.registry import get as get_tool
from app.agent.tools import knowledge
from app.services import brain


def sse(message: dict) -> NS:
    return NS(text=f"event: message\ndata: {json.dumps(message)}\n\n",
              headers={"content-type": "text/event-stream"}, raise_for_status=lambda: None)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(brain, "BRAIN_URL", "http://brain.internal:8080")
    monkeypatch.setattr(brain, "BRAIN_READ_TOKEN", "read-token")
    brain.reset()
    yield
    brain.reset()


def test_off_without_configuration_and_the_tool_is_not_offered():
    assert not brain.enabled()
    assert brain.call("query", {"query": "x"}) is None
    assert get_tool("search_knowledge") is None        # agent unchanged without a brain


def test_a_query_goes_to_mcp_with_the_read_token(configured, monkeypatch):
    sent = {}

    def post(url, headers, json, timeout):
        sent.update(url=url, headers=headers, body=json, timeout=timeout)
        return sse({"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": "wiki/concepts/net-revenue: fórmula…"}]}})

    monkeypatch.setattr(brain.httpx, "post", post)
    assert brain.call("query", {"query": "Net Revenue"}).startswith("wiki/concepts/net-revenue")
    assert sent["url"] == "http://brain.internal:8080/mcp"
    assert sent["headers"]["Authorization"] == "Bearer read-token"
    assert sent["body"]["method"] == "tools/call"
    assert sent["body"]["params"] == {"name": "query", "arguments": {"query": "Net Revenue"}}


def test_a_failure_returns_none_and_skips_the_brain_for_a_while(configured, monkeypatch):
    calls = []

    def down(*_, **__):
        calls.append(1)
        raise TimeoutError("timed out")

    monkeypatch.setattr(brain.httpx, "post", down)
    assert brain.call("query", {"query": "x"}) is None
    assert brain.call("query", {"query": "x"}) is None
    assert len(calls) == 1 and not brain.ready()


def test_a_refused_tool_counts_as_no_answer(configured, monkeypatch):
    monkeypatch.setattr(brain.httpx, "post", lambda *a, **k: sse(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "insufficient_scope"}}))
    assert brain.call("put_page", {"slug": "x"}) is None


def test_the_tool_gives_text_to_the_model_and_no_figures_or_blocks(configured, monkeypatch):
    monkeypatch.setattr(brain, "call", lambda tool, args: "Umbral de alerta: 5 % de variación")
    result = knowledge.search_knowledge(None, question="alertas")
    assert result.blocks == [] and "5 %" in result.summary
    assert result.data == {"source": "brain", "found": True}   # nothing for `present` to accept


def test_the_tool_reads_a_whole_page_when_asked(configured, monkeypatch):
    asked = {}
    monkeypatch.setattr(brain, "call", lambda tool, args: asked.update(tool=tool, args=args) or "…")
    knowledge.search_knowledge(None, question="Mandioca", page="companies/mandioca")
    assert asked == {"tool": "get_page", "args": {"slug": "companies/mandioca"}}


def test_without_an_answer_the_model_is_told_to_carry_on(configured, monkeypatch):
    monkeypatch.setattr(brain, "call", lambda tool, args: None)
    result = knowledge.search_knowledge(None, question="x")
    assert result.data["found"] is False and "demás herramientas" in result.summary
