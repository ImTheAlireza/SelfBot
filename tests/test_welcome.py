"""The `selfwlc` per-chat welcome feature."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from conftest import FakeClient, FakeEvent, FakeMessage
from selfbot.bot import SelfBot
from selfbot.plugins.welcome import render_welcome

# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


@dataclass
class FakeUser:
    id: int = 111
    first_name: str = "Ali"
    last_name: str | None = None
    username: str | None = None
    bot: bool = False


def test_name_tag():
    user = FakeUser(first_name="Ali", last_name="Reza")
    assert render_welcome("hello [name]", user) == "hello Ali Reza"


def test_nametag_is_a_mention():
    user = FakeUser(id=555, first_name="Ali")
    assert render_welcome("hi [nametag]", user) == "hi [Ali](tg://user?id=555)"


def test_username_tag():
    user = FakeUser(username="alireza")
    assert render_welcome("yo [username]", user) == "yo @alireza"


def test_username_tag_falls_back_to_mention():
    """A user with no @username still gets greeted, via a mention."""
    user = FakeUser(id=777, first_name="Sara", username=None)
    assert render_welcome("yo [username]", user) == "yo [Sara](tg://user?id=777)"


def test_combined_tag_prefers_username():
    user = FakeUser(id=1, first_name="Ali", username="alireza")
    assert render_welcome("hey [[username]/[nametag]]", user) == "hey @alireza"


def test_combined_tag_falls_back_to_nametag():
    user = FakeUser(id=9, first_name="Ali", username=None)
    assert render_welcome("hey [[username]/[nametag]]", user) == "hey [Ali](tg://user?id=9)"


def test_combined_tag_not_shredded_by_component_tags():
    """[[username]/[nametag]] must be replaced atomically, not piecewise."""
    user = FakeUser(id=2, first_name="Ali", username="ali")
    out = render_welcome("[[username]/[nametag]] and [username] and [nametag]", user)
    assert out == "@ali and @ali and [Ali](tg://user?id=2)"


def test_persian_text_and_tags():
    user = FakeUser(id=3, first_name="علی", last_name="رضا", username=None)
    assert render_welcome("سلام [name] خوش آمدی", user) == "سلام علی رضا خوش آمدی"


def test_multiple_occurrences_all_replaced():
    user = FakeUser(first_name="Ali")
    assert render_welcome("[name] [name]!", user) == "Ali Ali!"


def test_empty_profile_name_falls_back():
    user = FakeUser(first_name="", last_name=None)
    assert render_welcome("hi [name]", user) == "hi User"


# ---------------------------------------------------------------------------
# The selfwlc command
# ---------------------------------------------------------------------------


def make_reply_event(text: str, **kwargs: Any) -> FakeEvent:
    return FakeEvent(
        is_reply=True,
        reply_message=FakeMessage(text=text),
        **kwargs,
    )


@pytest.fixture
def wlc_event():
    def _make(raw_text: str, *, reply_text: str | None = None, chat_id: int = -100):
        if reply_text is None:
            return FakeEvent(raw_text=raw_text, chat_id=chat_id)
        message = FakeMessage(text=reply_text)
        message.raw_text = reply_text  # type: ignore[attr-defined]
        return FakeEvent(
            raw_text=raw_text,
            chat_id=chat_id,
            is_reply=True,
            reply_message=message,
        )

    return _make


@pytest.mark.asyncio
async def test_on_without_saved_message_errors(bot, registry, wlc_event):
    event = wlc_event("selfwlc on")
    await registry.dispatch(bot, event, "selfwlc on")
    output = " ".join(event.replies)
    assert "No welcome message is saved" in output


@pytest.mark.asyncio
async def test_set_requires_reply(bot, registry, wlc_event):
    event = wlc_event("selfwlc set")
    await registry.dispatch(bot, event, "selfwlc set")
    assert any("Reply to a message" in r for r in event.replies)


@pytest.mark.asyncio
async def test_set_then_on_then_off(bot, registry, wlc_event, db):
    event = wlc_event("selfwlc set", reply_text="hello [name]")
    await registry.dispatch(bot, event, "selfwlc set")
    assert any("saved" in r for r in event.replies)

    saved = await db.get_welcome(-100)
    assert saved is not None
    assert saved.message == "hello [name]"
    assert saved.enabled is False  # set alone does not activate

    event = wlc_event("selfwlc on")
    await registry.dispatch(bot, event, "selfwlc on")
    assert (await db.get_welcome(-100)).enabled is True

    event = wlc_event("selfwlc off")
    await registry.dispatch(bot, event, "selfwlc off")
    welcome = await db.get_welcome(-100)
    assert welcome.enabled is False
    assert welcome.message == "hello [name]"  # off keeps the template


@pytest.mark.asyncio
async def test_set_persian_message(bot, registry, wlc_event, db):
    event = wlc_event("selfwlc set", reply_text="سلام [name] به گروه خوش آمدی")
    await registry.dispatch(bot, event, "selfwlc set")
    saved = await db.get_welcome(-100)
    assert saved.message == "سلام [name] به گروه خوش آمدی"


@pytest.mark.asyncio
async def test_updating_message_keeps_enabled_state(bot, registry, wlc_event, db):
    event = wlc_event("selfwlc set", reply_text="v1")
    await registry.dispatch(bot, event, "selfwlc set")
    await registry.dispatch(bot, wlc_event("selfwlc on"), "selfwlc on")

    event = wlc_event("selfwlc set", reply_text="v2")
    await registry.dispatch(bot, event, "selfwlc set")

    welcome = await db.get_welcome(-100)
    assert welcome.message == "v2"
    assert welcome.enabled is True


@pytest.mark.asyncio
async def test_off_all_disables_every_chat(bot, registry, wlc_event, db):
    for chat_id in (-1, -2):
        event = wlc_event("selfwlc set", reply_text="hi", chat_id=chat_id)
        await registry.dispatch(bot, event, "selfwlc set")
        event = wlc_event("selfwlc on", chat_id=chat_id)
        await registry.dispatch(bot, event, "selfwlc on")

    event = wlc_event("selfwlc off -all", chat_id=-1)
    await registry.dispatch(bot, event, "selfwlc off -all")

    assert (await db.get_welcome(-1)).enabled is False
    assert (await db.get_welcome(-2)).enabled is False
    # Templates survive `off -all`.
    assert (await db.get_welcome(-2)).message == "hi"


@pytest.mark.asyncio
async def test_list_shows_all_chats(bot, registry, wlc_event):
    for chat_id, text in ((-1, "one"), (-2, "two")):
        event = wlc_event("selfwlc set", reply_text=text, chat_id=chat_id)
        await registry.dispatch(bot, event, "selfwlc set")
    event = wlc_event("selfwlc on", chat_id=-2)
    await registry.dispatch(bot, event, "selfwlc on")

    event = wlc_event("selfwlc list", chat_id=-1)
    await registry.dispatch(bot, event, "selfwlc list")
    output = " ".join(event.replies)
    assert "-1" in output and "-2" in output
    assert "one" in output and "two" in output
    assert "on" in output and "off" in output


@pytest.mark.asyncio
async def test_list_empty(bot, registry, wlc_event):
    event = wlc_event("selfwlc list")
    await registry.dispatch(bot, event, "selfwlc list")
    assert any("No welcome messages" in r for r in event.replies)


@pytest.mark.asyncio
async def test_clear_removes_current_chat_only(bot, registry, wlc_event, db):
    for chat_id in (-1, -2):
        event = wlc_event("selfwlc set", reply_text="hi", chat_id=chat_id)
        await registry.dispatch(bot, event, "selfwlc set")

    event = wlc_event("selfwlc clear", chat_id=-1)
    await registry.dispatch(bot, event, "selfwlc clear")

    assert await db.get_welcome(-1) is None
    assert await db.get_welcome(-2) is not None


@pytest.mark.asyncio
async def test_clear_all_removes_everything(bot, registry, wlc_event, db):
    for chat_id in (-1, -2):
        event = wlc_event("selfwlc set", reply_text="hi", chat_id=chat_id)
        await registry.dispatch(bot, event, "selfwlc set")

    event = wlc_event("selfwlc clear -all", chat_id=-1)
    await registry.dispatch(bot, event, "selfwlc clear -all")

    assert await db.list_welcomes() == []


@pytest.mark.asyncio
async def test_on_after_clear_errors_again(bot, registry, wlc_event, db):
    event = wlc_event("selfwlc set", reply_text="hi")
    await registry.dispatch(bot, event, "selfwlc set")
    await registry.dispatch(bot, wlc_event("selfwlc clear"), "selfwlc clear")

    event = wlc_event("selfwlc on")
    await registry.dispatch(bot, event, "selfwlc on")
    assert any("No welcome message is saved" in r for r in event.replies)


@pytest.mark.asyncio
async def test_unknown_action_shows_usage(bot, registry, wlc_event):
    event = wlc_event("selfwlc bogus")
    await registry.dispatch(bot, event, "selfwlc bogus")
    assert any("Usage" in r for r in event.replies)


@pytest.mark.asyncio
async def test_selfwlc_is_owner_only(bot, registry, wlc_event):
    event = FakeEvent(raw_text="selfwlc list", sender_id=999, out=False)
    await registry.dispatch(bot, event, "selfwlc list")
    assert any("owner" in r for r in event.replies)


# ---------------------------------------------------------------------------
# The join handler
# ---------------------------------------------------------------------------


@dataclass
class FakeActionEvent:
    """Stands in for ``events.ChatAction.Event``."""

    chat_id: int = -100
    user_joined: bool = True
    user_added: bool = False
    users: list[Any] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    reply_fails: bool = False

    async def get_users(self) -> list[Any]:
        return self.users

    async def reply(self, text: str, **_kwargs: Any) -> FakeMessage:
        if self.reply_fails:
            raise RuntimeError("cannot reply to service message")
        self.replies.append(text)
        return FakeMessage(text=text)


class FakeMe:
    id = 4242
    first_name = "Owner"
    last_name = None
    username = "owner"


def make_selfbot(config, db, registry) -> SelfBot:
    bot = SelfBot(config, registry=registry, client=FakeClient(), db=db)
    bot.me = FakeMe()
    return bot


@pytest.mark.asyncio
async def test_welcomes_joining_user(config, db, registry):
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "سلام [name] خوش اومدی")
    await db.set_welcome_enabled(-100, True)

    event = FakeActionEvent(users=[FakeUser(first_name="علی")])
    await bot._maybe_welcome(event)

    assert event.replies == ["سلام علی خوش اومدی"]


@pytest.mark.asyncio
async def test_welcomes_added_user_too(config, db, registry):
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "hi [name]")
    await db.set_welcome_enabled(-100, True)

    event = FakeActionEvent(user_joined=False, user_added=True, users=[FakeUser()])
    await bot._maybe_welcome(event)

    assert event.replies == ["hi Ali"]


@pytest.mark.asyncio
async def test_only_fires_in_enabled_chat(config, db, registry):
    """The welcome must stay scoped to the chat it was turned on in."""
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "hi [name]")
    await db.set_welcome_enabled(-100, True)

    event = FakeActionEvent(chat_id=-200, users=[FakeUser()])
    await bot._maybe_welcome(event)

    assert event.replies == []
    assert bot.client.sent_messages == []


@pytest.mark.asyncio
async def test_saved_but_disabled_does_not_fire(config, db, registry):
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "hi [name]")  # never switched on

    event = FakeActionEvent(users=[FakeUser()])
    await bot._maybe_welcome(event)

    assert event.replies == []


@pytest.mark.asyncio
async def test_falls_back_to_plain_message_when_reply_fails(config, db, registry):
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "hi [name]")
    await db.set_welcome_enabled(-100, True)

    event = FakeActionEvent(users=[FakeUser()], reply_fails=True)
    await bot._maybe_welcome(event)

    assert event.replies == []
    assert bot.client.sent_messages == [(-100, "hi Ali")]


@pytest.mark.asyncio
async def test_welcomes_every_user_in_a_batch(config, db, registry):
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "hi [name]")
    await db.set_welcome_enabled(-100, True)

    event = FakeActionEvent(
        users=[FakeUser(id=1, first_name="A"), FakeUser(id=2, first_name="B")]
    )
    await bot._maybe_welcome(event)

    assert event.replies == ["hi A", "hi B"]


@pytest.mark.asyncio
async def test_skips_bots_and_self(config, db, registry):
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "hi [name]")
    await db.set_welcome_enabled(-100, True)

    event = FakeActionEvent(
        users=[
            FakeUser(id=50, first_name="Robo", bot=True),
            FakeUser(id=FakeMe.id, first_name="Owner"),
        ]
    )
    await bot._maybe_welcome(event)

    assert event.replies == []


@pytest.mark.asyncio
async def test_paused_bot_does_not_welcome(config, db, registry):
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "hi [name]")
    await db.set_welcome_enabled(-100, True)
    bot.active = False

    event = FakeActionEvent(users=[FakeUser()])
    await bot._maybe_welcome(event)

    assert event.replies == []


@pytest.mark.asyncio
async def test_non_join_actions_are_ignored(config, db, registry):
    bot = make_selfbot(config, db, registry)
    await db.set_welcome_message(-100, "hi [name]")
    await db.set_welcome_enabled(-100, True)

    event = FakeActionEvent(user_joined=False, user_added=False, users=[FakeUser()])
    await bot._maybe_welcome(event)

    assert event.replies == []


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_welcome_crud(db):
    assert await db.get_welcome(-1) is None

    await db.set_welcome_message(-1, "hello [name]")
    welcome = await db.get_welcome(-1)
    assert welcome.message == "hello [name]"
    assert welcome.enabled is False

    assert await db.set_welcome_enabled(-1, True) == 1
    assert (await db.get_welcome(-1)).enabled is True

    assert await db.delete_welcome(-1) == 1
    assert await db.get_welcome(-1) is None


@pytest.mark.asyncio
async def test_enable_without_row_affects_nothing(db):
    assert await db.set_welcome_enabled(-99, True) == 0


@pytest.mark.asyncio
async def test_disable_all_welcomes(db):
    await db.set_welcome_message(-1, "a")
    await db.set_welcome_message(-2, "b")
    await db.set_welcome_enabled(-1, True)
    await db.set_welcome_enabled(-2, True)

    assert await db.disable_all_welcomes() == 2
    assert all(not w.enabled for w in await db.list_welcomes())
    # Second run is a no-op.
    assert await db.disable_all_welcomes() == 0


@pytest.mark.asyncio
async def test_delete_all_welcomes(db):
    await db.set_welcome_message(-1, "a")
    await db.set_welcome_message(-2, "b")
    assert await db.delete_all_welcomes() == 2
    assert await db.list_welcomes() == []
