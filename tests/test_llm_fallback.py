"""Claude answers first; DeepSeek takes over whenever Claude fails, and a
circuit breaker keeps a Claude that will not recover soon out of the way."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.agent import loop
from app.services import llm


class Claude:
    def __init__(self, error: Exception | None = None):
        self.error, self.calls = error, 0
        self.messages = NS(create=self.create)

    def create(self, **_):
        self.calls += 1
        if self.error:
            raise self.error
        return NS(content=[NS(type="text", text="desde Claude")])


class DeepSeek:
    def __init__(self):
        self.calls = 0
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        self.model = kwargs["model"]
        return NS(choices=[NS(message=NS(content="desde DeepSeek"))])


@pytest.fixture
def providers(monkeypatch):
    def install(claude, deepseek):
        monkeypatch.setattr(llm, "_claude", claude)
        monkeypatch.setattr(llm, "_deepseek", deepseek)
        llm.reset()
        return claude, deepseek
    yield install
    llm.reset()


def test_claude_answers_when_it_is_up(providers):
    claude, deepseek = providers(Claude(), DeepSeek())
    assert llm.complete("s", "hola") == "desde Claude"
    assert deepseek.calls == 0 and llm.provider() == "anthropic"


def test_deepseek_answers_the_same_question_when_claude_fails(providers):
    claude, deepseek = providers(Claude(RuntimeError("overloaded")), DeepSeek())
    assert llm.complete("s", "hola") == "desde DeepSeek"
    assert claude.calls == 1 and deepseek.calls == 1


def test_no_credit_sets_claude_aside_and_skips_it(providers):
    claude, deepseek = providers(
        Claude(RuntimeError("Your credit balance is too low to access the Anthropic API")),
        DeepSeek())
    for _ in range(3):
        assert llm.complete("s", "hola") == "desde DeepSeek"
    assert claude.calls == 1                      # tried once, then set aside
    assert llm.provider() == "deepseek" and not loop.available()
    assert "credit" in llm.status()["claude_down_reason"]


def test_transient_failures_open_the_breaker_only_after_three(providers):
    claude, _ = providers(Claude(RuntimeError("503 overloaded")), DeepSeek())
    for _ in range(3):
        llm.complete("s", "hola")
    assert claude.calls == 3 and not llm.claude_ready()


def test_claude_comes_back_when_the_pause_ends(providers, monkeypatch):
    claude, _ = providers(Claude(RuntimeError("invalid x-api-key")), DeepSeek())
    llm.complete("s", "hola")
    assert not llm.claude_ready()
    real = llm.time.monotonic
    monkeypatch.setattr(llm.time, "monotonic", lambda: real() + llm.HARD_DOWN_SECONDS + 1)
    claude.error = None
    assert llm.complete("s", "hola") == "desde Claude"


def test_only_claude_and_it_fails_degrades_without_raising(providers):
    providers(Claude(RuntimeError("down")), None)
    assert llm.complete("s", "hola") is None


def test_only_deepseek_still_answers(providers):
    _, deepseek = providers(None, DeepSeek())
    assert llm.complete("s", "hola") == "desde DeepSeek"
    assert llm.provider() == "deepseek" and not loop.available()


def test_deepseek_uses_its_own_model(providers):
    _, deepseek = providers(Claude(RuntimeError("down")), DeepSeek())
    llm.complete("s", "hola")
    assert deepseek.model == llm.DEEPSEEK_MODEL and deepseek.model.startswith("deepseek")


def test_a_chat_model_left_at_deepseek_never_reaches_claude(monkeypatch):
    import importlib

    from app import config
    monkeypatch.setenv("CHAT_MODEL", "deepseek-chat")
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.CLAUDE_MODEL.startswith("claude")
        assert reloaded.DEEPSEEK_MODEL == "deepseek-chat"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
