import asyncio
import dataclasses
import datetime
import sqlite3
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.helpers.poll_store import PollRecord, PollStore, open_poll_store
from cogs.polls import (
    POLL,
    POLL_DURATION,
    VOTE,
    CloseMenu,
    PollInputError,
    Polls,
    build_results_embed,
    count_votes,
    describe_outcome,
    format_results,
    parse_poll_input,
    setup_polls,
)

GUILD_ID = 1
AUTHOR_ID = 100
OTHER_ID = 200
POLLS_ID, VOTES_ID, MOD_ID, GENERAL_ID = 11, 12, 13, 14
NOW = datetime.datetime(2026, 9, 30, 12, tzinfo=datetime.timezone.utc)
FUTURE = datetime.datetime(2999, 1, 1, tzinfo=datetime.timezone.utc)
PAST = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


class FakeChannel:
    def __init__(self, channel_id, name):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.send = AsyncMock()
        self.fetch_message = AsyncMock()


class FakeBot:
    def __init__(self, guild, channels):
        self._guild = guild
        self._channels = {channel.id: channel for channel in channels}
        self.wait_until_ready = AsyncMock()

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_guild(self, guild_id):
        return self._guild if guild_id == GUILD_ID else None


class FakeAnswer:
    def __init__(self, answer_id, text, voters, vote_count=0, error=None):
        self.id = answer_id
        self.text = text
        self._voters = voters
        self.vote_count = vote_count
        self._error = error

    async def voters(self, *, limit=None):
        if self._error is not None:
            raise self._error
        for voter in self._voters[:limit]:
            yield voter


def make_http_error(cls=discord.HTTPException, status=500):
    response = SimpleNamespace(status=status, reason="Error")
    return cls(cast(Any, response), "error")


class Setup:
    # A guild with the poll, vote, mod and general channels, a store and the cog.
    def __init__(self, channels=("active_polls", "active_votes", "mod_chat")):
        self.polls = FakeChannel(POLLS_ID, "active_polls")
        self.votes = FakeChannel(VOTES_ID, "active_votes")
        self.mod = FakeChannel(MOD_ID, "mod_chat")
        self.general = FakeChannel(GENERAL_ID, "general")
        present = [c for c in (self.polls, self.votes, self.mod) if c.name in channels]
        self.guild = SimpleNamespace(
            id=GUILD_ID, text_channels=present + [self.general]
        )
        self.bot = FakeBot(self.guild, present + [self.general])
        self.store = PollStore(":memory:")
        self.cog = Polls(self.bot, self.store)

    def ctx(self, channel=None, author_id=AUTHOR_ID, guild=True):
        return SimpleNamespace(
            guild=self.guild if guild else None,
            channel=channel or self.general,
            author=SimpleNamespace(id=author_id, mention=f"<@{author_id}>"),
            message=SimpleNamespace(delete=AsyncMock()),
            reply=AsyncMock(return_value=SimpleNamespace(id=999)),
        )

    def add(self, message_id=500, kind="poll", author_id=AUTHOR_ID, **overrides):
        record = PollRecord(
            message_id=message_id,
            channel_id=POLLS_ID if kind == "poll" else VOTES_ID,
            guild_id=GUILD_ID,
            author_id=author_id,
            kind=kind,
            question=f"Question {message_id}?",
            created_at=NOW + datetime.timedelta(minutes=message_id),
            expires_at=FUTURE,
        )
        record = dataclasses.replace(record, **overrides)
        self.store.add(record)
        return record

    def poll_message(self, record, answers, expires_at=FUTURE):
        msg = SimpleNamespace(
            id=record.message_id,
            jump_url=f"https://discord.com/channels/1/{record.channel_id}/{record.message_id}",
            poll=SimpleNamespace(answers=answers, expires_at=expires_at),
        )
        msg.end_poll = AsyncMock(return_value=msg)
        channel = self.polls if record.kind == "poll" else self.votes
        channel.fetch_message = AsyncMock(return_value=msg)
        return msg


def sent_message(message_id=500, expires_at=None):
    return SimpleNamespace(
        id=message_id,
        jump_url=f"https://discord.com/channels/1/11/{message_id}",
        created_at=NOW,
        poll=SimpleNamespace(expires_at=expires_at or NOW + POLL_DURATION),
        delete=AsyncMock(),
    )


def run_command(cog, command, ctx, **kwargs):
    callback = cast(Any, command.callback)
    asyncio.run(callback(cog, ctx, **kwargs))


def reply_text(ctx):
    return ctx.reply.call_args.args[0]


def answers(*votes):
    return [
        FakeAnswer(index, f"Option {index}", [object()] * count)
        for index, count in enumerate(votes, start=1)
    ]


# parse_poll_input


def test_parse_poll_input_strips_parts_and_drops_empty_options():
    question, options = parse_poll_input("  Best language?  | Python ||  Rust | ")

    assert question == "Best language?"
    assert options == ["Python", "Rust"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "Start with the question"),
        ("| Python | Rust", "Start with the question"),
        ("Best language?", "at least 2 options"),
        ("Best language? | Python", "at least 2 options"),
        ("Q | " + " | ".join(f"o{i}" for i in range(11)), "at most 10 options"),
        ("Q" * 301 + " | a | b", "at most 300 characters"),
        ("Q | " + "a" * 56 + " | b", "at most 55 characters"),
        ("Q | Python | python", "only be listed once"),
    ],
)
def test_parse_poll_input_rejects_invalid_input(text, message):
    with pytest.raises(PollInputError, match=message):
        parse_poll_input(text)


def test_parse_poll_input_accepts_the_limits():
    question, options = parse_poll_input(
        "Q" * 300 + " | " + " | ".join("a" * 54 + str(i) for i in range(10))
    )

    assert len(question) == 300
    assert len(options) == 10


# PollStore


def test_store_adds_and_gets_records():
    setup = Setup()
    record = setup.add(500, kind="vote")

    assert setup.store.get(500) == record
    assert setup.store.get(501) is None


def test_store_lists_open_records_for_author_and_kind_newest_first():
    setup = Setup()
    older = setup.add(500)
    newer = setup.add(501)
    setup.add(502, kind="vote")
    setup.add(503, author_id=OTHER_ID)
    setup.add(504, guild_id=2)
    setup.add(505)
    setup.store.claim_close(505)

    assert setup.store.list_open(GUILD_ID, AUTHOR_ID, "poll") == [newer, older]


def test_store_claims_close_once_and_can_reopen():
    setup = Setup()
    setup.add(500)

    assert setup.store.claim_close(500)
    assert not setup.store.claim_close(500)
    assert not setup.store.claim_close(999)

    setup.store.reopen(500)

    assert setup.store.claim_close(500)


def test_store_lists_expired_open_records():
    setup = Setup()
    setup.add(500, expires_at=NOW - datetime.timedelta(seconds=1))
    setup.add(501, expires_at=NOW + datetime.timedelta(seconds=1))
    setup.add(502, expires_at=NOW - datetime.timedelta(days=1))
    setup.store.claim_close(502)

    assert [r.message_id for r in setup.store.list_expired(NOW)] == [500]


def test_store_persists_across_reopening(tmp_path):
    path = str(tmp_path / "nested" / "polls.db")
    store = PollStore(path)
    record = PollRecord(500, POLLS_ID, GUILD_ID, AUTHOR_ID, "vote", "Q?", NOW, FUTURE)
    store.add(record)
    store.close()

    assert PollStore(path).get(500) == record


def test_polls_are_not_loaded_without_a_database(tmp_path):
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("")

    store = open_poll_store(str(not_a_directory / "polls.db"))

    assert store is None
    with pytest.raises(RuntimeError):
        asyncio.run(setup_polls(SimpleNamespace(), [], store))


# Opening


def test_open_poll_posts_native_poll_and_saves_it():
    setup = Setup()
    setup.polls.send.return_value = sent_message()
    ctx = setup.ctx()

    run_command(setup.cog, setup.cog.open_poll, ctx, text="Best? | Python | Rust")

    setup.polls.send.assert_awaited_once()
    args, kwargs = setup.polls.send.call_args
    assert args[0] == f"📊 Poll by <@{AUTHOR_ID}>"
    poll = kwargs["poll"]
    assert poll.question == "Best?"
    assert [answer.text for answer in poll.answers] == ["Python", "Rust"]
    assert poll.duration == datetime.timedelta(hours=768)
    assert kwargs["allowed_mentions"].users is False
    assert setup.store.get(500) == PollRecord(
        500, POLLS_ID, GUILD_ID, AUTHOR_ID, "poll", "Best?", NOW, NOW + POLL_DURATION
    )
    assert reply_text(ctx) == (
        f"Posted your poll in <#{POLLS_ID}>: https://discord.com/channels/1/11/500"
    )


def test_open_vote_posts_in_active_votes():
    setup = Setup()
    setup.votes.send.return_value = sent_message()

    run_command(setup.cog, setup.cog.open_vote, setup.ctx(), text="Ban? | Yes | No")

    assert setup.votes.send.call_args.args[0] == f"🗳️ Vote by <@{AUTHOR_ID}>"
    setup.polls.send.assert_not_called()
    stored = setup.store.get(500)
    assert stored is not None and stored.kind == "vote"


def test_configured_channel_id_is_used_instead_of_name():
    setup = Setup()
    setup.general.send.return_value = sent_message()
    configured = dataclasses.replace(POLL, channel_id=GENERAL_ID)

    asyncio.run(setup.cog._open(cast(Any, setup.ctx()), configured, "Q | a | b"))

    setup.general.send.assert_awaited_once()
    setup.polls.send.assert_not_called()


def test_open_poll_reports_missing_channel():
    setup = Setup(channels=())
    ctx = setup.ctx()

    run_command(setup.cog, setup.cog.open_poll, ctx, text="Q | a | b")

    assert reply_text(ctx) == "I can't find the #active_polls channel."


def test_open_poll_shows_usage_for_invalid_input():
    setup = Setup()
    ctx = setup.ctx()

    run_command(setup.cog, setup.cog.open_poll, ctx)

    assert "Usage: `?open_poll Question | Option 1 | Option 2`" in reply_text(ctx)
    setup.polls.send.assert_not_called()


def test_open_poll_requires_a_server():
    setup = Setup()
    ctx = setup.ctx(guild=False)

    run_command(setup.cog, setup.cog.open_poll, ctx, text="Q | a | b")

    assert reply_text(ctx) == "Polls and votes only work in a server."


def test_open_poll_in_its_channel_deletes_the_command_instead_of_replying():
    setup = Setup()
    setup.polls.send.return_value = sent_message()
    ctx = setup.ctx(channel=setup.polls)

    run_command(setup.cog, setup.cog.open_poll, ctx, text="Q | a | b")

    ctx.message.delete.assert_awaited_once()
    ctx.reply.assert_not_called()


def test_failed_send_saves_nothing():
    setup = Setup()
    setup.polls.send.side_effect = make_http_error(discord.Forbidden, 403)
    ctx = setup.ctx()

    run_command(setup.cog, setup.cog.open_poll, ctx, text="Q | a | b")

    assert "couldn't post your poll" in reply_text(ctx)
    assert setup.store.get(500) is None


def test_failed_save_removes_the_posted_poll(monkeypatch):
    setup = Setup()
    message = sent_message()
    setup.polls.send.return_value = message

    def fail(_record):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(setup.store, "add", fail)
    ctx = setup.ctx()

    run_command(setup.cog, setup.cog.open_poll, ctx, text="Q | a | b")

    message.delete.assert_awaited_once()
    assert "so I removed it" in reply_text(ctx)


# Closing


def prompt_close(setup, command, ctx):
    run_command(setup.cog, command, ctx)
    return ctx.reply.call_args.kwargs.get("view")


def test_close_without_open_items_says_so():
    setup = Setup()
    setup.add(500, kind="vote")
    ctx = setup.ctx()

    view = prompt_close(setup, setup.cog.close_poll, ctx)

    assert view is None
    assert reply_text(ctx) == "You don't have any open polls."


def test_close_menu_lists_only_the_callers_open_items_of_that_kind():
    setup = Setup()
    setup.add(500)
    setup.add(501)
    setup.add(502, kind="vote")
    setup.add(503, author_id=OTHER_ID)
    ctx = setup.ctx()

    async def prompt():
        await setup.cog._prompt_close(cast(Any, ctx), POLL)
        return ctx.reply.call_args.kwargs["view"]

    view = asyncio.run(prompt())

    assert reply_text(ctx) == "Which poll do you want to close?"
    assert [option.value for option in view.select.options] == ["501", "500"]
    assert view.select.options[0].label == "Question 501?"
    assert view.message is ctx.reply.return_value


def test_close_menu_rejects_other_members():
    setup = Setup()

    async def check(user_id):
        view = CloseMenu(setup.cog, POLL, AUTHOR_ID, [setup.add(500 + user_id)])
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        allowed = await view.interaction_check(cast(Any, interaction))
        return allowed, interaction.response.send_message

    allowed, send = asyncio.run(check(OTHER_ID))
    assert not allowed
    send.assert_awaited_once_with("This menu isn't yours.", ephemeral=True)

    allowed, send = asyncio.run(check(AUTHOR_ID))
    assert allowed
    send.assert_not_called()


def select_in_menu(setup, record):
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=AUTHOR_ID),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )

    async def select():
        view = CloseMenu(setup.cog, POLL, AUTHOR_ID, [record])
        await view.close_selected(cast(Any, interaction), record.message_id)
        return view

    view = asyncio.run(select())
    interaction.response.defer.assert_awaited_once()
    assert view.is_finished()
    kwargs = interaction.edit_original_response.call_args.kwargs
    assert kwargs["view"] is None
    return kwargs["content"]


def test_selecting_a_poll_ends_it_without_reporting_to_mods():
    setup = Setup()
    record = setup.add(500)
    message = setup.poll_message(record, answers(2, 1))

    result = select_in_menu(setup, record)

    message.end_poll.assert_awaited_once()
    assert result == f"Closed your poll **Question 500?**: {message.jump_url}"
    assert setup.store.list_open(GUILD_ID, AUTHOR_ID, "poll") == []
    setup.mod.send.assert_not_called()


def test_selecting_a_vote_reports_results_to_mod_chat():
    setup = Setup()
    record = setup.add(500, kind="vote")
    message = setup.poll_message(record, answers(3, 1))

    result = select_in_menu(setup, record)

    message.end_poll.assert_awaited_once()
    setup.mod.send.assert_awaited_once()
    embed = setup.mod.send.call_args.kwargs["embed"]
    assert embed.title == "🗳️ Vote closed: Question 500?"
    assert embed.url == message.jump_url
    assert "**Option 1**: 3 votes (75%)" in embed.description
    assert "**Option 2**: 1 vote (25%)" in embed.description
    assert "Total: 4 votes" in embed.description
    assert embed.fields[0].value == f"<@{AUTHOR_ID}>"
    assert embed.fields[1].value == "Winner: Option 1"
    assert embed.footer.text == "Closed by its author"
    assert result.endswith(f"The results were posted in <#{MOD_ID}>.")


def test_vote_close_without_mod_chat_tells_the_member():
    setup = Setup(channels=("active_votes",))
    record = setup.add(500, kind="vote")
    setup.poll_message(record, answers(1, 1))

    result = select_in_menu(setup, record)

    assert "couldn't post the results in #mod_chat" in result
    assert setup.store.list_open(GUILD_ID, AUTHOR_ID, "vote") == []


def test_closing_an_already_closed_item_is_rejected():
    setup = Setup()
    record = setup.add(500)
    setup.store.claim_close(500)

    result = asyncio.run(setup.cog.close_item(record))

    assert result == "That poll is already closed."
    setup.polls.fetch_message.assert_not_called()


def test_closing_a_deleted_poll_keeps_it_closed():
    setup = Setup()
    record = setup.add(500)
    setup.polls.fetch_message = AsyncMock(
        side_effect=make_http_error(discord.NotFound, 404)
    )

    result = asyncio.run(setup.cog.close_item(record))

    assert "was deleted" in result
    assert setup.store.list_open(GUILD_ID, AUTHOR_ID, "poll") == []


def test_failed_end_poll_reopens_the_item():
    setup = Setup()
    record = setup.add(500)
    message = setup.poll_message(record, answers(1, 1))
    message.end_poll.side_effect = make_http_error()

    result = asyncio.run(setup.cog.close_item(record))

    assert result == "I couldn't close that poll. Please try again."
    assert setup.store.list_open(GUILD_ID, AUTHOR_ID, "poll") == [record]


def test_failed_end_poll_on_a_poll_that_just_expired_still_closes_it():
    setup = Setup()
    record = setup.add(500, kind="vote")
    message = setup.poll_message(record, answers(1, 0))
    message.end_poll.side_effect = make_http_error()
    expired_message = SimpleNamespace(
        id=500,
        jump_url=message.jump_url,
        poll=SimpleNamespace(answers=answers(1, 0), expires_at=PAST),
    )
    setup.votes.fetch_message = AsyncMock(side_effect=[message, expired_message])

    result = asyncio.run(setup.cog.close_item(record))

    assert result.startswith("Closed your vote")
    embed = setup.mod.send.call_args.kwargs["embed"]
    assert embed.footer.text == "Ended automatically after 32 days"


def test_expired_poll_is_not_ended_again():
    setup = Setup()
    record = setup.add(500, kind="vote")
    message = setup.poll_message(record, answers(0, 0), expires_at=PAST)

    asyncio.run(setup.cog.close_item(record))

    message.end_poll.assert_not_called()
    embed = setup.mod.send.call_args.kwargs["embed"]
    assert embed.fields[1].value == "No votes"
    assert embed.footer.text == "Ended automatically after 32 days"


def test_fetch_failure_reopens_the_item():
    setup = Setup()
    record = setup.add(500)
    setup.polls.fetch_message = AsyncMock(side_effect=make_http_error())

    result = asyncio.run(setup.cog.close_item(record))

    assert result == "I couldn't reach that poll. Please try again."
    assert setup.store.list_open(GUILD_ID, AUTHOR_ID, "poll") == [record]


# Expiry and cleanup


def poll_result_message(message_id):
    return cast(
        discord.Message,
        SimpleNamespace(
            type=discord.MessageType.poll_result,
            reference=SimpleNamespace(message_id=message_id),
        ),
    )


def test_poll_result_message_closes_an_expired_vote_once():
    setup = Setup()
    record = setup.add(500, kind="vote")
    setup.poll_message(record, answers(2, 2), expires_at=PAST)

    asyncio.run(setup.cog.on_message(poll_result_message(500)))
    asyncio.run(setup.cog.on_message(poll_result_message(500)))

    setup.mod.send.assert_awaited_once()
    embed = setup.mod.send.call_args.kwargs["embed"]
    assert embed.fields[1].value == "Tie: Option 1, Option 2"


def test_poll_result_for_a_poll_closed_by_command_is_ignored():
    setup = Setup()
    record = setup.add(500, kind="vote")
    setup.poll_message(record, answers(1, 0))
    select_in_menu(setup, record)

    asyncio.run(setup.cog.on_message(poll_result_message(500)))

    setup.mod.send.assert_awaited_once()


def test_other_messages_are_ignored():
    setup = Setup()
    setup.add(500, kind="vote")
    message = cast(
        discord.Message,
        SimpleNamespace(
            type=discord.MessageType.default,
            reference=SimpleNamespace(message_id=500),
        ),
    )

    asyncio.run(setup.cog.on_message(message))

    assert setup.store.list_open(GUILD_ID, AUTHOR_ID, "vote") != []


def test_close_expired_closes_overdue_items():
    setup = Setup()
    overdue = setup.add(500, kind="vote", expires_at=PAST)
    setup.add(501, kind="vote")
    setup.poll_message(overdue, answers(1, 0), expires_at=PAST)

    asyncio.run(setup.cog.close_expired())

    setup.bot.wait_until_ready.assert_awaited_once()
    setup.mod.send.assert_awaited_once()
    assert [
        r.message_id for r in setup.store.list_open(GUILD_ID, AUTHOR_ID, "vote")
    ] == [501]


def test_deleted_messages_are_marked_closed():
    setup = Setup()
    setup.add(500)
    setup.add(501)
    setup.add(502)

    asyncio.run(
        setup.cog.on_raw_message_delete(cast(Any, SimpleNamespace(message_id=500)))
    )
    asyncio.run(
        setup.cog.on_raw_bulk_message_delete(
            cast(Any, SimpleNamespace(message_ids={501, 999}))
        )
    )

    assert [
        r.message_id for r in setup.store.list_open(GUILD_ID, AUTHOR_ID, "poll")
    ] == [502]


# Results


def test_count_votes_counts_every_voter_and_falls_back_to_vote_count():
    poll = SimpleNamespace(
        answers=[
            FakeAnswer(1, "A", [object()] * 150, vote_count=100),
            FakeAnswer(2, "B", [], vote_count=7, error=make_http_error()),
        ]
    )

    assert asyncio.run(count_votes(cast(Any, poll))) == [("A", 150), ("B", 7)]


def test_format_results_handles_no_votes():
    assert format_results([("A", 0), ("B", 0)]) == (
        "**A**: 0 votes (0%)\n**B**: 0 votes (0%)\nTotal: 0 votes"
    )


def test_format_results_rounds_percentages():
    assert format_results([("A", 1), ("B", 1), ("C", 1)]) == (
        "**A**: 1 vote (33%)\n**B**: 1 vote (33%)\n**C**: 1 vote (33%)\nTotal: 3 votes"
    )


@pytest.mark.parametrize(
    ("counts", "outcome"),
    [
        ([("A", 0), ("B", 0)], "No votes"),
        ([("A", 2), ("B", 1)], "Winner: A"),
        ([("A", 2), ("B", 2), ("C", 1)], "Tie: A, B"),
    ],
)
def test_describe_outcome(counts, outcome):
    assert describe_outcome(counts) == outcome


def test_results_embed_truncates_long_titles():
    record = PollRecord(
        500, VOTES_ID, GUILD_ID, AUTHOR_ID, "vote", "Q" * 300, NOW, FUTURE
    )

    embed = build_results_embed(record, [("A", 1), ("B", 0)], "https://x", False)

    assert embed.title is not None and len(embed.title) == 256
    assert embed.title.endswith("…")
    assert "Q" * 300 in (embed.description or "")


def test_cog_registers_the_four_commands():
    setup = Setup()

    assert {command.name for command in setup.cog.get_commands()} == {
        "open_poll",
        "open_vote",
        "close_poll",
        "close_vote",
    }
    assert VOTE.reports_to_mods and not POLL.reports_to_mods
