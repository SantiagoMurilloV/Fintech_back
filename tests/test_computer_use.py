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
    return NS(type="tool_use", name=name, id=f"tu_{name}", input=inputs, toolset_name="computer")


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
    result_block = calls[1]["messages"][-1]["content"][0]
    assert result_block["type"] == "tool_result" and result_block["toolset_name"] == "computer"


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


class RecipeBrowser(FakeBrowser):
    """A page whose headline element shows `price`; `missing` breaks the recipe."""

    def __init__(self, price="0.99925", missing=False, learned='span[data-qa-id="new-last"]'):
        super().__init__()
        self.price, self.missing, self.learned = price, missing, learned

    @property
    def url(self):
        return self.pages[-1] if self.pages else ""

    def read_text(self, selector, timeout_ms=0):
        if self.missing:
            raise TimeoutError("element not found")
        return self.price

    def derive_selector(self, value):
        return self.learned


def _no_reference(_base):
    return None


def test_the_recipe_reads_without_a_model(db, monkeypatch):
    monkeypatch.setattr(tradingview, "Browser", lambda: RecipeBrowser())
    monkeypatch.setattr(tradingview.agent, "run", lambda *a, **k: pytest.fail("no model needed"))
    report = tradingview.run(db, ["COINBASE:USDTUSD"], reference_fetch=lambda _b: 0.9991)
    entry = report["symbols"][0]
    assert entry["ok"] and entry["mode"] == "receta" and entry["price"] == 0.99925
    assert report["summary"]["por_receta"] == 1
    snap = db.query(RateSnapshot).one()
    assert (snap.source, snap.pair, snap.price) == ("tradingview", "USDT/USD", 0.99925)
    recipe = tradingview.recipes(db)[0]
    assert recipe["learned_by"] == "default" and recipe["failures"] == 0


def test_a_broken_recipe_falls_back_and_is_relearned(db, monkeypatch):
    monkeypatch.setattr(tradingview, "Browser", lambda: RecipeBrowser(missing=True))
    monkeypatch.setattr(tradingview.agent, "is_available", lambda: True)
    monkeypatch.setattr(tradingview.agent, "run", lambda *a, **k: {
        "json": {"price": 0.99925}, "steps": 2, "error": None})
    entry = tradingview.run(db, ["COINBASE:USDTUSD"], reference_fetch=lambda _b: 0.999)[
        "symbols"][0]
    assert entry["ok"] and entry["mode"] == "computer_use" and entry["recipe_relearned"]
    recipe = tradingview.recipes(db)[0]
    assert recipe["selector"] == 'span[data-qa-id="new-last"]'
    assert recipe["learned_by"] == "computer_use" and recipe["failures"] == 0


def test_a_number_far_from_the_reference_is_not_stored(db, monkeypatch):
    # The recipe now points at some other figure; computer use is off.
    monkeypatch.setattr(tradingview, "Browser", lambda: RecipeBrowser(price="1.25"))
    monkeypatch.setattr(tradingview.agent, "is_available", lambda: False)
    entry = tradingview.run(db, ["COINBASE:USDTUSD"], reference_fetch=lambda _b: 0.9991)[
        "symbols"][0]
    assert not entry["ok"] and "referencia" in entry["error"]
    assert db.query(RateSnapshot).count() == 0
    assert tradingview.recipes(db)[0]["failures"] == 1


def test_a_fresh_exchange_reading_is_the_reference(db):
    db.add(RateSnapshot(source="kraken", pair="USDT/USD", price=0.9990,
                        taken_at=tradingview._now()))
    db.commit()
    price, source = tradingview.reference_price(db, "USDT/USD", fetch=_no_reference)
    assert (price, source) == (0.9990, "kraken")


def test_prices_parse_like_the_page_writes_them():
    assert tradingview.parse_price("67,950.12") == 67950.12
    assert tradingview.parse_price("\u22121.5") == -1.5
    assert tradingview.parse_price("—") is None


def test_the_selector_is_derived_from_a_real_page():
    from app.services.computer_use.browser import Browser, BrowserUnavailable
    try:
        browser = Browser().__enter__()
    except BrowserUnavailable:
        pytest.skip("Chromium not installed")
    try:
        browser.page.set_content("""
          <div><span class="price-AbC123x9 js-last" data-qa-id="symbol-last-value"
                style="font-size:40px">0.99925</span>
          <table><tr><td class="cell">0.99925</td></tr></table></div>""")
        assert browser.derive_selector(0.99925) == 'span[data-qa-id="symbol-last-value"]'
        # As on TradingView: the number in a bare <span> inside the marked one,
        # and a tick between the read and the search.
        browser.page.set_content("""
          <span class="last-KLji300y js-symbol-last" data-qa-id="symbol-last-value"
                style="font-size:40px"><span>2,513.4</span></span>""")
        assert browser.derive_selector(2514.1) == 'span[data-qa-id="symbol-last-value"]'
    finally:
        browser.__exit__(None, None, None)


def test_tradingview_without_a_browser_says_so(db, monkeypatch):
    from app.services.computer_use.browser import BrowserUnavailable

    def unavailable():
        raise BrowserUnavailable("El navegador para computer use no está instalado.")
    monkeypatch.setattr(tradingview, "Browser", unavailable)
    assert "navegador" in tradingview.run(db, ["COINBASE:USDTUSD"])["error"]


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
