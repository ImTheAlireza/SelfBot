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


def test_username_tag_is_empty_without_username():
    """Plain [username] does not fall back — that is the combined tag's job."""
    user = FakeUser(id=777, first_name="Sara", username=None)
    assert render_welcome("yo [username]", user) == "yo "


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
# Approved join requests
# ---------------------------------------------------------------------------


def make_join_request_update(chat: Any, user_id: int, msg_id: int = 42) -> Any:
    from telethon import types

    message = types.MessageService(
        id=msg_id,
        peer_id=chat,
        from_id=types.PeerUser(user_id),
        action=types.MessageActionChatJoinedByRequest(),
    )
    return types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)


@pytest.mark.asyncio
async def test_welcomes_approved_join_request(config, db, registry):
    from telethon import types, utils

    bot = make_selfbot(config, db, registry)
    chat = types.PeerChannel(123)
    chat_id = utils.get_peer_id(chat)  # -100 prefixed

    await db.set_welcome_message(chat_id, "سلام [name] خوش اومدی")
    await db.set_welcome_enabled(chat_id, True)
    bot.client.entities[111] = FakeUser(id=111, first_name="علی")

    await bot._maybe_welcome_join_request(make_join_request_update(chat, 111))

    assert bot.client.sent_messages == [(chat_id, "سلام علی خوش اومدی")]


@pytest.mark.asyncio
async def test_approved_user_resolved_from_update_entities(config, db, registry):
    """The joining user is usually not in the session cache yet — the raw
    update's bundled entities must be used before get_entity."""
    from telethon import types, utils

    bot = make_selfbot(config, db, registry)
    chat = types.PeerChannel(123)
    chat_id = utils.get_peer_id(chat)

    await db.set_welcome_message(chat_id, "hi [name]")
    await db.set_welcome_enabled(chat_id, True)
    # get_entity would raise (user 111 NOT in bot.client.entities); the
    # update carries the user object instead, like real Telethon updates do.
    update = make_join_request_update(chat, 111)
    update._entities = {111: types.User(id=111, first_name="Ali")}

    await bot._maybe_welcome_join_request(update)

    assert bot.client.sent_messages == [(chat_id, "hi Ali")]


@pytest.mark.asyncio
async def test_approved_request_in_disabled_chat_is_ignored(config, db, registry):
    from telethon import types, utils

    bot = make_selfbot(config, db, registry)
    chat = types.PeerChannel(123)
    chat_id = utils.get_peer_id(chat)

    await db.set_welcome_message(chat_id, "hi [name]")  # saved but off
    bot.client.entities[111] = FakeUser(id=111)

    await bot._maybe_welcome_join_request(make_join_request_update(chat, 111))

    assert bot.client.sent_messages == []


@pytest.mark.asyncio
async def test_non_join_service_messages_are_ignored(config, db, registry):
    from telethon import types, utils

    bot = make_selfbot(config, db, registry)
    chat = types.PeerChannel(123)
    chat_id = utils.get_peer_id(chat)
    await db.set_welcome_message(chat_id, "hi [name]")
    await db.set_welcome_enabled(chat_id, True)

    message = types.MessageService(
        id=1,
        peer_id=chat,
        from_id=types.PeerUser(111),
        action=types.MessageActionPinMessage(),
    )
    update = types.UpdateNewChannelMessage(message=message, pts=1, pts_count=1)
    await bot._maybe_welcome_join_request(update)

    assert bot.client.sent_messages == []


@pytest.mark.asyncio
async def test_same_join_is_not_welcomed_twice(config, db, registry):
    """If both the ChatAction and the raw path fire, greet only once."""
    from telethon import types, utils

    bot = make_selfbot(config, db, registry)
    chat = types.PeerChannel(123)
    chat_id = utils.get_peer_id(chat)

    await db.set_welcome_message(chat_id, "hi [name]")
    await db.set_welcome_enabled(chat_id, True)
    bot.client.entities[111] = FakeUser(id=111, first_name="Ali")

    update = make_join_request_update(chat, 111)
    await bot._maybe_welcome_join_request(update)
    await bot._maybe_welcome_join_request(update)

    event = FakeActionEvent(chat_id=chat_id, users=[FakeUser(id=111, first_name="Ali")])
    await bot._maybe_welcome(event)

    assert bot.client.sent_messages == [(chat_id, "hi Ali")]
    assert event.replies == []


@pytest.mark.asyncio
async def test_paused_bot_ignores_join_requests(config, db, registry):
    from telethon import types, utils

    bot = make_selfbot(config, db, registry)
    chat = types.PeerChannel(123)
    chat_id = utils.get_peer_id(chat)
    await db.set_welcome_message(chat_id, "hi [name]")
    await db.set_welcome_enabled(chat_id, True)
    bot.client.entities[111] = FakeUser(id=111)
    bot.active = False

    await bot._maybe_welcome_join_request(make_join_request_update(chat, 111))

    assert bot.client.sent_messages == []


# ---------------------------------------------------------------------------
# Admin log polling (joins whose service message was deleted)
# ---------------------------------------------------------------------------


class AdminLogClient(FakeClient):
    """FakeClient that also answers GetAdminLogRequest calls."""

    def __init__(self) -> None:
        super().__init__()
        self.admin_log_results: list[Any] = []
        self.admin_log_requests: list[Any] = []
        self.admin_log_error: Exception | None = None

    async def __call__(self, request: Any) -> Any:
        self.admin_log_requests.append(request)
        if self.admin_log_error is not None:
            raise self.admin_log_error
        if self.admin_log_results:
            return self.admin_log_results.pop(0)
        from telethon import types

        return types.channels.AdminLogResults(events=[], chats=[], users=[])


def make_admin_log_result(entries: list[tuple[int, int, Any]], users: list[Any]) -> Any:
    """entries: (event_id, user_id, action) triples."""
    from telethon import types

    events_ = [
        types.ChannelAdminLogEvent(id=eid, date=None, user_id=uid, action=action)
        for eid, uid, action in entries
    ]
    return types.channels.AdminLogResults(events=events_, chats=[], users=users)


def make_selfbot_with_admin_log(config, db, registry) -> SelfBot:
    bot = SelfBot(config, registry=registry, client=AdminLogClient(), db=db)
    bot.me = FakeMe()
    return bot


@pytest.mark.asyncio
async def test_admin_log_join_is_welcomed(config, db, registry):
    """A join whose service message was deleted still gets a welcome."""
    from telethon import types, utils

    bot = make_selfbot_with_admin_log(config, db, registry)
    chat_id = utils.get_peer_id(types.PeerChannel(123))
    await db.set_welcome_message(chat_id, "hi [name]")
    await db.set_welcome_enabled(chat_id, True)

    # First pass: establishes the log position, greets nobody.
    bot.client.admin_log_results.append(
        make_admin_log_result(
            [(10, 999, types.ChannelAdminLogEventActionParticipantJoin())],
            [types.User(id=999, first_name="Old")],
        )
    )
    await bot._poll_welcome_joins()
    assert bot.client.sent_messages == []

    # Second pass: a new join appeared after the recorded position.
    bot.client.admin_log_results.append(
        make_admin_log_result(
            [(11, 111, types.ChannelAdminLogEventActionParticipantJoin())],
            [types.User(id=111, first_name="Ali")],
        )
    )
    await bot._poll_welcome_joins()
    assert bot.client.sent_messages == [(chat_id, "hi Ali")]


@pytest.mark.asyncio
async def test_admin_log_join_by_request_is_welcomed(config, db, registry):
    from telethon import types, utils

    bot = make_selfbot_with_admin_log(config, db, registry)
    chat_id = utils.get_peer_id(types.PeerChannel(123))
    await db.set_welcome_message(chat_id, "سلام [name]")
    await db.set_welcome_enabled(chat_id, True)

    await bot._poll_welcome_joins()  # records position (empty log)

    invite = types.ChatInviteExported(link="https://t.me/+x", admin_id=1, date=None)
    action = types.ChannelAdminLogEventActionParticipantJoinByRequest(
        invite=invite, approved_by=1
    )
    bot.client.admin_log_results.append(
        make_admin_log_result([(5, 111, action)], [types.User(id=111, first_name="علی")])
    )
    await bot._poll_welcome_joins()
    assert bot.client.sent_messages == [(chat_id, "سلام علی")]


@pytest.mark.asyncio
async def test_admin_log_user_already_greeted_is_skipped(config, db, registry):
    """The realtime path greeted the user; the poller must not repeat it."""
    from telethon import types, utils

    bot = make_selfbot_with_admin_log(config, db, registry)
    chat_id = utils.get_peer_id(types.PeerChannel(123))
    await db.set_welcome_message(chat_id, "hi [name]")
    await db.set_welcome_enabled(chat_id, True)

    await bot._poll_welcome_joins()  # establish position
    assert not bot._already_welcomed(chat_id, 111)  # realtime path marks the user

    bot.client.admin_log_results.append(
        make_admin_log_result(
            [(7, 111, types.ChannelAdminLogEventActionParticipantJoin())],
            [types.User(id=111, first_name="Ali")],
        )
    )
    await bot._poll_welcome_joins()
    assert bot.client.sent_messages == []


@pytest.mark.asyncio
async def test_admin_log_non_join_events_ignored(config, db, registry):
    from telethon import types, utils

    bot = make_selfbot_with_admin_log(config, db, registry)
    chat_id = utils.get_peer_id(types.PeerChannel(123))
    await db.set_welcome_message(chat_id, "hi [name]")
    await db.set_welcome_enabled(chat_id, True)

    await bot._poll_welcome_joins()

    bot.client.admin_log_results.append(
        make_admin_log_result(
            [(8, 111, types.ChannelAdminLogEventActionParticipantLeave())],
            [types.User(id=111, first_name="Ali")],
        )
    )
    await bot._poll_welcome_joins()
    assert bot.client.sent_messages == []


@pytest.mark.asyncio
async def test_admin_log_error_warns_once(config, db, registry, caplog):
    """No admin rights → warn once, don't spam the log every 30s."""
    import logging as _logging

    from telethon import types, utils

    bot = make_selfbot_with_admin_log(config, db, registry)
    chat_id = utils.get_peer_id(types.PeerChannel(123))
    await db.set_welcome_message(chat_id, "hi")
    await db.set_welcome_enabled(chat_id, True)
    bot.client.admin_log_error = RuntimeError("CHAT_ADMIN_REQUIRED")

    with caplog.at_level(_logging.WARNING, logger="selfbot.bot"):
        await bot._poll_welcome_joins()
        await bot._poll_welcome_joins()

    warnings = [r for r in caplog.records if "admin log" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_disabled_welcome_is_not_polled(config, db, registry):
    from telethon import types, utils

    bot = make_selfbot_with_admin_log(config, db, registry)
    chat_id = utils.get_peer_id(types.PeerChannel(123))
    await db.set_welcome_message(chat_id, "hi")  # saved but off

    await bot._poll_welcome_joins()
    assert bot.client.admin_log_requests == []


@pytest.mark.asyncio
async def test_legacy_small_groups_are_not_polled(config, db, registry):
    """Basic groups have no admin log; the poller must skip them quietly."""
    bot = make_selfbot_with_admin_log(config, db, registry)
    await db.set_welcome_message(-1234, "hi")  # PeerChat-style id
    await db.set_welcome_enabled(-1234, True)

    await bot._poll_welcome_joins()
    assert bot.client.admin_log_requests == []


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
