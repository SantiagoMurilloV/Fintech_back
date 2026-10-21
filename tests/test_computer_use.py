"""Computer use and the Claude chat client, with a fake Claude and a fake screen."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.models import RateSnapshot
from app.services import llm
from app.services.computer_use import agent, tradingview


class FakeBrowser:
    def __init__(self):
        self.actions, self.pages = [], []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def goto(self, url, settle_ms=0):
        self.pages.append(url)

    def screenshot(self):
        return "iVBORw0KGgo="

    def act(self, action):
        self.actions.append(action)
        return self.screenshot()


def fake_claude(replies):
    """A client whose messages.create returns the given replies in order."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return replies.pop(0)

    return NS(messages=NS(create=create)), calls


def tool(name, **inputs):
    return NS(type="tool_use", name=name, id=f"tu_{name}", input=inputs)


def text(value):
    return NS(type="text", text=value)


def test_the_loop_acts_until_claude_answers(monkeypatch):
    client, calls = fake_claude([
        NS(content=[tool("left_click", coordinate=[600, 400])]),
        NS(content=[text('Leído: {"price": 0.99905, "change_pct": 0.01, "currency": "USD"}')]),
    ])
    monkeypatch.setattr(agent, "_anthropic", lambda: client)
    browser = FakeBrowser()
    result = agent.run(browser, "read the price")
    assert browser.actions == [{"action": "left_click", "coordinate": [600, 400]}]
    assert result["json"]["price"] == 0.99905 and result["steps"] == 1
    assert calls[0]["tools"][0]["type"] == "computer_toolset_20260801"
    # The screenshot after the click goes back as the tool result.
    assert calls[1]["messages"][-1]["content"][0]["type"] == "tool_result"


def test_the_loop_stops_at_the_step_budget(monkeypatch):
    client, _ = fake_claude([NS(content=[tool("scroll", scroll_direction="down",
                                               scroll_amount=3)]) for _ in range(10)])
    monkeypatch.setattr(agent, "_anthropic", lambda: client)
    result = agent.run(FakeBrowser(), "read", max_steps=2)
    assert result["json"] is None and "2 acciones" in result["error"]


def test_watchlist_symbols_and_pairs():
    assert tradingview.parse_watchlist(" coinbase:usdtusd, BAD, BINANCE:BTCUSDT,") == [
        "COINBASE:USDTUSD", "BINANCE:BTCUSDT"]
    assert tradingview.pair_of("COINBASE:USDTUSD") == "USDT/USD"
    assert tradingview.pair_of("BINANCE:BTCUSDT") == "BTC/USDT"


def test_tradingview_stores_valid_readings_only(db, monkeypatch):
    readings = iter([{"price": 0.99905}, {"price": None, "reason": "pop-up"}])
    monkeypatch.setattr(tradingview, "Browser", FakeBrowser)
    monkeypatch.setattr(tradingview.agent, "is_available", lambda: True)
    monkeypatch.setattr(tradingview.agent, "run",
                        lambda *_a, **_k: {"json": next(readings), "steps": 1, "error": None})
    report = tradingview.run(db, ["COINBASE:USDTUSD", "COINBASE:ETHUSD"])
    assert [s["ok"] for s in report["symbols"]] == [True, False]
    rows = db.query(RateSnapshot).all()
    assert [(r.source, r.pair, r.price) for r in rows] == [("tradingview", "USDT/USD", 0.99905)]


def test_tradingview_without_a_key_says_so(db, monkeypatch):
    monkeypatch.setattr(tradingview.agent, "is_available", lambda: False)
    assert "ANTHROPIC_API_KEY" in tradingview.run(db, ["COINBASE:USDTUSD"])["error"]


def test_claude_chat_returns_text_and_clean_json(monkeypatch):
    replies = [NS(content=[text("Hola.")]),
               NS(content=[text('Claro: {"tool": "kpis", "arguments": {}}')])]
    client, calls = fake_claude(replies)
    monkeypatch.setattr(llm, "_client", client)
    monkeypatch.setattr(llm, "CHAT_PROVIDER", "anthropic")
    assert llm.complete("sys", "hola") == "Hola."
    assert llm.chat("sys", [{"role": "assistant", "content": "x"},
                            {"role": "user", "content": "a"},
                            {"role": "user", "content": "b"}],
                    json_object=True) == '{"tool": "kpis", "arguments": {}}'
    # Turns start with the user and alternate; the JSON rule is in the system.
    assert calls[1]["messages"] == [{"role": "user", "content": "a\n\nb"}]
    assert "JSON" in calls[1]["system"]


def test_a_failing_model_never_breaks_the_turn(monkeypatch):
    def boom(**_):
        raise RuntimeError("overloaded")
    monkeypatch.setattr(llm, "_client", NS(messages=NS(create=boom)))
    monkeypatch.setattr(llm, "CHAT_PROVIDER", "anthropic")
    assert llm.complete("sys", "hola") is None
