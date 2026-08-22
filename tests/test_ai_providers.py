"""Tests for the database-backed AIManager and the new AI commands."""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from conftest import FakeBot, FakeEvent
from selfbot.config import AIConfig
from selfbot.errors import FeatureDisabledError
from selfbot.registry import CommandRegistry
from selfbot.services.ai import (
    ANYAPI_DEFAULT_MODEL,
    AIManager,
    extract_answer,
)


@dataclass
class _Resp:
    payload: Any
    status: int = 200

    async def json(self, *, content_type: Any = None) -> Any:
        return self.payload

    async def text(self) -> str:
        return str(self.payload)

    async def read(self) -> bytes:
        if isinstance(self.payload, (dict, list)):
            import json

            return json.dumps(self.payload).encode()
        return str(self.payload).encode()


@dataclass
class _Http:
    responses: list[_Resp] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    default: _Resp | None = None

    async def request(self, method: str, url: str, **kwargs: Any) -> _Resp:
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        assert self.default is not None
        return self.default


class _ManagerBot:
    """Minimal bot shape for direct AIManager tests."""

    def __init__(self, db: Any, ai_config: AIConfig, http: _Http) -> None:
        self.db = db
        self.config = types.SimpleNamespace(ai=ai_config)
        self.http = http


def _ai_config(**overrides: Any) -> AIConfig:
    base: dict[str, Any] = {
        "rapidapi_key": "",
        "anyapi_key": "",
        "anyapi_base_url": "https://api.anyapi.ai/v1",
        "anyapi_model": ANYAPI_DEFAULT_MODEL,
        "bluesminds_key": "",
        "bluesminds_base_url": "https://api.bluesminds.com/v1",
        "bluesminds_model": ANYAPI_DEFAULT_MODEL,
        "memory_turns": 10,
        "memory_budget": 24000,
        "cooldown_max": 900,
    }
    base.update(overrides)
    return AIConfig(**base)


async def test_chat_uses_default_openai_provider(db) -> None:
    await db.add_provider(
        "anyapi", "https://api.anyapi.ai/v1", "sk-test", model="m1", is_default=True
    )
    http = _Http(default=_Resp({"choices": [{"message": {"content": "hello!"}}]}))
    manager = AIManager(_ManagerBot(db, _ai_config(), http))

    answer = await manager.chat("hi", history=False)

    assert answer == "hello!"
    call = http.calls[0]
    assert call["url"] == "https://api.anyapi.ai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["json"]["model"] == "m1"


async def test_chat_falls_back_to_next_provider_on_429(db) -> None:
    await db.add_provider(
        "a", "https://a/v1", "ka", model="ma", is_default=True
    )
    await db.add_provider("b", "https://b/v1", "kb", model="mb")
    http = _Http(
        responses=[
            _Resp({"error": {"message": "rate limited"}}, status=429),
            _Resp({"choices": [{"message": {"content": "from b"}}]}),
        ]
    )
    manager = AIManager(_ManagerBot(db, _ai_config(), http))

    assert await manager.chat("q", history=False) == "from b"

    a = await db.get_provider("a")
    assert a is not None and a.failure_count >= 1
    assert a.cooldown_until is not None
    b = await db.get_provider("b")
    assert b is not None and b.success_count >= 1


async def test_chat_skips_cooling_provider(db) -> None:
    from datetime import timedelta

    from selfbot.db import utcnow

    await db.add_provider(
        "a", "https://a/v1", "ka", model="ma", is_default=True
    )
    await db.add_provider("b", "https://b/v1", "kb", model="mb")
    await db.set_provider_cooldown(
        "a", utcnow() + timedelta(minutes=5), error="quota"
    )
    http = _Http(default=_Resp({"choices": [{"message": {"content": "from b"}}]}))
    manager = AIManager(_ManagerBot(db, _ai_config(), http))

    assert await manager.chat("q", history=False) == "from b"
    # Only b was called — a was skipped due to cooldown.
    assert all(call["url"].startswith("https://b") for call in http.calls)


async def test_chat_raises_when_no_providers(db) -> None:
    manager = AIManager(_ManagerBot(db, _ai_config(), _Http()))
    with pytest.raises(FeatureDisabledError):
        await manager.chat("q", history=False)


async def test_config_fallback_when_db_empty(db) -> None:
    config = _ai_config(anyapi_key="sk-env", anyapi_model="env-model")
    http = _Http(default=_Resp({"choices": [{"message": {"content": "ok"}}]}))
    manager = AIManager(_ManagerBot(db, config, http))

    assert await manager.chat("q", history=False) == "ok"
    assert http.calls[0]["json"]["model"] == "env-model"


async def test_global_model_override(db) -> None:
    await db.add_provider(
        "a", "https://a/v1", "ka", model="default-model", is_default=True
    )
    await db.set_setting("ai.default_model", "override-model")
    http = _Http(default=_Resp({"choices": [{"message": {"content": "ok"}}]}))
    manager = AIManager(_ManagerBot(db, _ai_config(), http))

    await manager.chat("q", history=False)
    assert http.calls[0]["json"]["model"] == "override-model"


async def test_memory_is_persisted_and_pruned(db) -> None:
    await db.add_provider(
        "a", "https://a/v1", "ka", model="m", is_default=True
    )
    http = _Http(default=_Resp({"choices": [{"message": {"content": "answer"}}]}))
    config = _ai_config(memory_turns=2)
    manager = AIManager(_ManagerBot(db, config, http))

    await manager.chat("first", chat_id=-100, history=True)
    await manager.chat("second", chat_id=-100, history=True)

    stored = await db.recent_ai_messages(-100, 20)
    roles = [m.role for m in stored]
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2
    # The second call must include the first turn in its request.
    second_request = http.calls[1]["json"]["messages"]
    assert any(m["content"] == "first" for m in second_request)


async def test_budget_drops_oldest_turns(db) -> None:
    await db.add_provider(
        "a", "https://a/v1", "ka", model="m", is_default=True
    )
    for _ in range(5):
        await db.add_ai_message(-100, "user", "x" * 5000)
        await db.add_ai_message(-100, "assistant", "y" * 5000)

    http = _Http(default=_Resp({"choices": [{"message": {"content": "ok"}}]}))
    config = _ai_config(memory_budget=8000)
    manager = AIManager(_ManagerBot(db, config, http))
    await manager.chat("new", chat_id=-100, history=True)

    messages = http.calls[0]["json"]["messages"]
    total = sum(len(m["content"]) for m in messages)
    assert total <= 8000 + 6000  # current prompt may exceed on its own
    assert messages[0]["role"] == "system"


async def test_status_reports_availability(db) -> None:
    from datetime import timedelta

    from selfbot.db import utcnow

    await db.add_provider(
        "enabled", "https://e/v1", "key", is_default=True
    )
    await db.add_provider("disabled", "https://d/v1", "key")
    await db.update_provider("disabled", enabled=False)
    await db.add_provider("cooling", "https://c/v1", "key")
    await db.set_provider_cooldown("cooling", utcnow() + timedelta(seconds=30))

    manager = AIManager(_ManagerBot(db, _ai_config(), _Http()))
    by_name = {s.provider.name: s for s in await manager.status()}

    assert by_name["enabled"].available is True
    assert by_name["disabled"].available is False
    assert by_name["cooling"].available is False
    assert 0 < by_name["cooling"].cooldown_remaining <= 30


async def test_set_provider_model_clears_cache(db) -> None:
    await db.add_provider("a", "https://a/v1", "ka", model="old", is_default=True)
    manager = AIManager(_ManagerBot(db, _ai_config(), _Http()))
    assert (await manager.providers())[0].model == "old"
    await manager.set_provider_model("a", "new")
    assert (await manager.providers())[0].model == "new"


def test_extract_answer_supports_text_parts() -> None:
    payload = {
        "choices": [
            {"message": {"content": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]}}
        ]
    }
    assert extract_answer(payload) == "A\nB"


# --------------------------------------------------------------------------
# Command-level wiring
# --------------------------------------------------------------------------


async def test_gpt_no_providers_reports_setup(bot: FakeBot) -> None:
    import dataclasses

    from selfbot.plugins.ai import get_manager

    bot.config = dataclasses.replace(bot.config, ai=_ai_config())
    manager = get_manager(ContextStub(bot))
    bot.ai = manager
    event = FakeEvent(raw_text="gpt hello")
    registry: CommandRegistry = bot.registry
    handled = await registry.dispatch(bot, event, event.raw_text)
    assert handled is True
    assert any("provider add" in r or "ANYAPI_KEY" in r for r in event.replies)


async def test_gptmodel_current_uses_config_when_empty(bot: FakeBot) -> None:
    event = FakeEvent(raw_text="gptmodel current")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any(ANYAPI_DEFAULT_MODEL in r for r in event.replies)


async def test_aistatus_lists_config_provider(bot: FakeBot) -> None:
    event = FakeEvent(raw_text="aistatus")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("rapidapi" in r.lower() for r in event.replies)


class ContextStub:
    """Minimal stand-in used only to construct a manager against a bot."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot


async def test_provider_requires_args(bot: FakeBot) -> None:
    event = FakeEvent(raw_text="provider")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert event.replies, "expected help output"


async def test_provider_add_validates_url(bot: FakeBot) -> None:
    event = FakeEvent(raw_text="provider add bad example.com kxxx model")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any("http://" in r or "https://" in r for r in event.replies)
