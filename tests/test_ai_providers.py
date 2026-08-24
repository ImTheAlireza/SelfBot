"""Tests for the database-backed AIManager and the new AI commands."""

from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from conftest import FakeBot, FakeEvent
from selfbot.config import AIConfig
from selfbot.errors import FeatureDisabledError, ProviderError
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
    assert call["headers"]["Accept"] == "text/event-stream, application/json"
    assert call["json"]["model"] == "m1"
    assert call["json"]["stream"] is True
    assert call["json"]["temperature"] == 0.7
    assert call["json"]["max_tokens"] == 1000


async def test_chat_joins_openai_sse_deltas(db) -> None:
    await db.add_provider(
        "bai", "https://api.b.ai/v1", "sk-test", model="gpt-5.2", is_default=True
    )
    stream = (
        'data: {"choices":[{"delta":{"role":"assistant","content":"Hello"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"!"},"finish_reason":null}]}\n\n'
        "data: [DONE]\n\n"
    )
    http = _Http(default=_Resp(stream))
    manager = AIManager(_ManagerBot(db, _ai_config(), http))

    assert await manager.chat("hi", history=False) == "Hello world!"
    assert len(http.calls) == 1
    assert http.calls[0]["json"]["stream"] is True


async def test_chat_retries_as_json_when_provider_rejects_streaming(db) -> None:
    await db.add_provider(
        "legacy", "https://legacy.example/v1", "sk-test", model="m1", is_default=True
    )
    http = _Http(
        responses=[
            _Resp({"error": {"message": "stream is unsupported"}}, status=400),
            _Resp({"choices": [{"message": {"content": "fallback JSON"}}]}),
        ]
    )
    manager = AIManager(_ManagerBot(db, _ai_config(), http))

    assert await manager.chat("hi", history=False) == "fallback JSON"
    assert [call["json"]["stream"] for call in http.calls] == [True, False]


async def test_chat_does_not_retry_authentication_errors(db) -> None:
    await db.add_provider(
        "bad", "https://bad.example/v1", "sk-bad", model="m1", is_default=True
    )
    http = _Http(default=_Resp({"error": {"message": "bad key"}}, status=401))
    manager = AIManager(_ManagerBot(db, _ai_config(), http))

    with pytest.raises(ProviderError, match="rejected the API key"):
        await manager.chat("hi", history=False)
    assert len(http.calls) == 1


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


async def test_set_active_model_updates_default_provider(db) -> None:
    await db.add_provider(
        "a", "https://a/v1", "ka", model="default-model", is_default=True
    )
    http = _Http(default=_Resp({"choices": [{"message": {"content": "ok"}}]}))
    manager = AIManager(_ManagerBot(db, _ai_config(), http))

    model, provider = await manager.set_active_model("luna")
    assert model == "luna" and provider == "a"

    await manager.chat("q", history=False)
    assert http.calls[0]["json"]["model"] == "luna"

    current_model, current_provider = await manager.current_model()
    assert current_model == "luna"
    assert current_provider == "a"


async def test_set_active_model_targets_specific_provider(db) -> None:
    await db.add_provider(
        "a", "https://a/v1", "ka", model="ma", is_default=True
    )
    await db.add_provider("b", "https://b/v1", "kb", model="mb")
    manager = AIManager(_ManagerBot(db, _ai_config(), _Http()))

    _model, provider = await manager.set_active_model("b/luna")
    assert provider == "b"
    b = await db.get_provider("b")
    assert b is not None and b.model == "luna"
    # Default provider's model untouched.
    a = await db.get_provider("a")
    assert a is not None and a.model == "ma"


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
    assert any("ai add" in r or "ANYAPI_KEY" in r for r in event.replies)


async def test_ai_model_shows_active(bot: FakeBot) -> None:
    event = FakeEvent(raw_text="ai model")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert any(ANYAPI_DEFAULT_MODEL in r for r in event.replies)


async def test_ai_status_lists_config_provider(bot: FakeBot) -> None:
    event = FakeEvent(raw_text="ai")
    await bot.registry.dispatch(bot, event, "ai")
    assert any("rapidapi" in r.lower() for r in event.replies)


async def test_ai_set_model_via_ai_model(bot: FakeBot) -> None:
    # The test config has a rapidapi (non-openai) provider, so add an openai one.
    await bot.db.add_provider(
        "bluesminds", "https://api.bluesminds.com/v1", "sk-bm",
        model="old-model", is_default=True, kind="openai",
    )
    bot.ai = None  # force manager to reload cache
    event = FakeEvent(raw_text="ai model luna")
    await bot.registry.dispatch(bot, event, "ai model luna")
    assert any("luna" in r for r in event.replies)
    p = await bot.db.get_provider("bluesminds")
    assert p is not None and p.model == "luna"


class ContextStub:
    """Minimal stand-in used only to construct a manager against a bot."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot




async def test_ai_add_validates_url(bot: FakeBot) -> None:
    event = FakeEvent(raw_text="ai add bad example.com kxxx model")
    await bot.registry.dispatch(bot, event, event.raw_text)
    assert event.deleted
    assert any("http://" in r or "https://" in r for r in event.replies)


async def test_ai_add_bai_normalizes_endpoint_and_deletes_key_message(bot: FakeBot) -> None:
    event = FakeEvent(
        raw_text=(
            "ai add bai https://api.b.ai/v1/chat/completions "
            "sk-example-secret-key gpt-5.2"
        )
    )

    await bot.registry.dispatch(bot, event, event.raw_text)

    assert event.deleted
    provider = await bot.db.get_provider("bai")
    assert provider is not None
    assert provider.base_url == "https://api.b.ai/v1"
    assert provider.model == "gpt-5.2"
    assert provider.api_key == "sk-example-secret-key"


async def test_gpt_reply_footer_shows_provider_and_model(bot: FakeBot) -> None:
    from selfbot.plugins.ai import get_manager

    await bot.db.add_provider(
        "bluesminds", "https://api.bluesminds.com/v1", "sk-bm",
        model="gpt-luna", is_default=True, kind="openai",
    )
    bot.ai = None
    manager = get_manager(ContextStub(bot))
    bot.ai = manager

    class _H:
        async def request(self, *a, **k):
            return _Resp({"choices": [{"message": {"content": "answer"}}]})

    bot.http = _H()
    event = FakeEvent(raw_text="gpt hi")
    await bot.registry.dispatch(bot, event, "gpt hi")
    assert any("via bluesminds" in r and "gpt-luna" in r for r in event.replies)


# --------------------------------------------------------------------------
# Flexible ai add parsing (order-agnostic; name derived from URL)
# --------------------------------------------------------------------------


def test_parse_add_args_url_key_name_order() -> None:
    from selfbot.plugins.ai import _parse_add_args

    r = _parse_add_args(
        ["https://agentrouter.org", "sk-iWDv3ks4D9prn1ZzUfG6Rwv26GqXS6D9pXdk", "agentrouter"]
    )
    assert r["base_url"] == "https://agentrouter.org"
    assert r["name"] == "agentrouter"
    assert r["model"] == ""


def test_parse_add_args_url_key_model_no_name() -> None:
    from selfbot.plugins.ai import _name_from_url, _parse_add_args

    r = _parse_add_args(
        ["https://agentrouter.org/v1", "sk-iWDv3ks4D9prn1ZzUfG6Rwv26GqXS6D9", "luna"]
    )
    assert r["name"] is None
    assert r["model"] == "luna"
    assert _name_from_url(r["base_url"]) == "agentrouter"


def test_parse_add_args_name_url_key_model() -> None:
    from selfbot.plugins.ai import _parse_add_args

    r = _parse_add_args(
        ["openai", "https://api.openai.com/v1", "sk-xxx", "gpt-4o"]
    )
    assert r == {
        "name": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-xxx",
        "model": "gpt-4o",
    }


def test_parse_add_args_minimal_url_key() -> None:
    from selfbot.plugins.ai import _parse_add_args

    r = _parse_add_args(["https://api.openai.com/v1", "sk-xxx"])
    assert r["name"] is None and r["model"] == ""


def test_parse_add_args_accepts_authorization_bearer_format() -> None:
    from selfbot.plugins.ai import _parse_add_args

    separate = _parse_add_args(
        ["bai", "https://api.b.ai/v1", "Bearer", "sk-secret", "gpt-5.2"]
    )
    quoted = _parse_add_args(
        ["bai", "https://api.b.ai/v1", "Bearer sk-secret", "gpt-5.2"]
    )
    assert separate["api_key"] == quoted["api_key"] == "sk-secret"
    assert separate["model"] == quoted["model"] == "gpt-5.2"


def test_parse_add_args_flags() -> None:
    from selfbot.plugins.ai import _parse_add_args

    r = _parse_add_args(["-model", "luna", "https://x/v1", "sk-xxx"])
    assert r["model"] == "luna" and r["name"] is None


def test_parse_add_args_missing_url() -> None:
    import pytest

    from selfbot.errors import UsageError
    from selfbot.plugins.ai import _parse_add_args

    with pytest.raises(UsageError):
        _parse_add_args(["sk-xxx"])


def test_name_from_url() -> None:
    from selfbot.plugins.ai import _name_from_url

    assert _name_from_url("https://api.agentrouter.org/v1") == "agentrouter"
    assert _name_from_url("https://api.openai.com/v1") == "openai"
    assert _name_from_url("https://www.example.com/") == "example"


# --------------------------------------------------------------------------
# Base URL normalization and smart provider test
# --------------------------------------------------------------------------


def test_normalize_base_url_appends_v1_to_bare_host() -> None:
    from selfbot.plugins.ai import _normalize_base_url

    assert _normalize_base_url("https://agentrouter.org") == "https://agentrouter.org/v1"
    assert _normalize_base_url("https://agentrouter.org/") == "https://agentrouter.org/v1"
    assert _normalize_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert _normalize_base_url("https://host/custom") == "https://host/custom"


def test_normalize_base_url_accepts_full_endpoint_from_api_examples() -> None:
    from selfbot.plugins.ai import _normalize_base_url

    assert (
        _normalize_base_url("https://api.b.ai/v1/chat/completions")
        == "https://api.b.ai/v1"
    )
    assert _normalize_base_url("https://api.b.ai/v1/models") == "https://api.b.ai/v1"
    assert (
        _normalize_base_url("https://host/openai/v1/chat/completions?debug=1")
        == "https://host/openai/v1"
    )


async def test_test_provider_tries_v1_fallback_and_corrects(db) -> None:
    from selfbot.services.ai import AIManager

    await db.add_provider(
        "agentrouter", "https://agentrouter.org", "sk-test", is_default=True
    )

    calls: list[str] = []

    class _Resp:
        def __init__(self, status, payload):
            self.status = status
            self._payload = payload

        async def text(self):
            import json

            return json.dumps(self._payload)

        async def json(self, content_type=None):
            return self._payload

    class _Http:
        async def request(self, method, url, **kwargs):
            calls.append(url)
            # First call to /models 404s; /v1/models works.
            if url.endswith("/v1/models"):
                return _Resp(200, {"data": [{"id": "claude-opus-4-6"}]})
            return _Resp(404, {"error": "not found"})

    class _Bot:
        def __init__(self):
            self.db = db
            self.http = _Http()
            self.config = types.SimpleNamespace(ai=_ai_config())
            self.metrics = None

    manager = AIManager(_Bot())
    ok, detail = await manager.test_provider("agentrouter")
    assert ok, detail
    assert any(u.endswith("/v1/models") for u in calls)
    assert "Auto-corrected" in detail
    fixed = await db.get_provider("agentrouter")
    assert fixed is not None and fixed.base_url == "https://agentrouter.org/v1"


async def test_test_provider_timeout_hints_at_v1(db) -> None:
    from selfbot.services.ai import AIManager

    await db.add_provider("x", "https://unreachable.example", "sk-test")

    class _Timeout(Exception):
        pass

    class _Http:
        async def request(self, *a, **k):
            raise _Timeout("Request to unreachable.example timed out after 30s")

    class _Bot:
        def __init__(self):
            self.db = db
            self.http = _Http()
            self.config = types.SimpleNamespace(ai=_ai_config())
            self.metrics = None

    manager = AIManager(_Bot())
    ok, detail = await manager.test_provider("x")
    assert not ok
    assert "/v1" in detail or "timed out" in detail.lower()
