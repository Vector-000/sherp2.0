import asyncio
import contextlib
import datetime
import logging
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, cast

import discord
from discord.ext import commands

from helper import get_config

from .helpers.poll_store import PollRecord, PollStore

logger = logging.getLogger(__name__)

__cfg = get_config().get("polls", None) or {}
# Channel IDs; 0 means "use the channel with the default name".
POLLS_CHANNEL_ID = __cfg.get("polls_channel", 0)
VOTES_CHANNEL_ID = __cfg.get("votes_channel", 0)
MOD_CHANNEL_ID = __cfg.get("mod_channel", 0)
MOD_CHANNEL_NAME = "mod_chat"

# Discord's limits for native polls.
QUESTION_MAX_LENGTH = 300
OPTION_MAX_LENGTH = 55
MIN_OPTIONS = 2
MAX_OPTIONS = 10
POLL_DURATION = datetime.timedelta(hours=768)  # 32 days, the longest Discord allows

MENU_LIMIT = 25  # Discord's maximum number of options in a select menu
MENU_TIMEOUT = 120
VOTERS_LIMIT = 100_000
NOT_IN_GUILD_MESSAGE = "Polls and votes only work in a server."


@dataclass(frozen=True)
class PollKind:
    key: str
    emoji: str
    channel_name: str
    channel_id: int
    reports_to_mods: bool

    @property
    def title(self) -> str:
        return self.key.capitalize()

    @property
    def open_command(self) -> str:
        return f"open_{self.key}"


POLL = PollKind(
    key="poll",
    emoji="📊",
    channel_name="active_polls",
    channel_id=POLLS_CHANNEL_ID,
    reports_to_mods=False,
)
VOTE = PollKind(
    key="vote",
    emoji="🗳️",
    channel_name="active_votes",
    channel_id=VOTES_CHANNEL_ID,
    reports_to_mods=True,
)
KINDS = {kind.key: kind for kind in (POLL, VOTE)}


class PollInputError(ValueError):
    pass


def parse_poll_input(text: str) -> Tuple[str, List[str]]:
    # Parses "Question | Option 1 | Option 2" into the question and options.
    parts = [part.strip() for part in text.split("|")]
    question = parts[0]
    options = [part for part in parts[1:] if part]

    if not question:
        raise PollInputError("Start with the question, then add the options.")
    if len(question) > QUESTION_MAX_LENGTH:
        raise PollInputError(
            f"The question can be at most {QUESTION_MAX_LENGTH} characters."
        )
    if len(options) < MIN_OPTIONS:
        raise PollInputError(f"Add at least {MIN_OPTIONS} options.")
    if len(options) > MAX_OPTIONS:
        raise PollInputError(f"You can add at most {MAX_OPTIONS} options.")
    if any(len(option) > OPTION_MAX_LENGTH for option in options):
        raise PollInputError(
            f"Each option can be at most {OPTION_MAX_LENGTH} characters."
        )
    if len({option.casefold() for option in options}) != len(options):
        raise PollInputError("Each option can only be listed once.")
    return question, options


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def format_results(counts: Sequence[Tuple[str, int]]) -> str:
    total = sum(count for _, count in counts)
    lines = []
    for text, count in counts:
        percent = round(100 * count / total) if total else 0
        lines.append(
            f"**{discord.utils.escape_markdown(text)}**: "
            f"{plural(count, 'vote')} ({percent}%)"
        )
    lines.append(f"Total: {plural(total, 'vote')}")
    return "\n".join(lines)


def describe_outcome(counts: Sequence[Tuple[str, int]]) -> str:
    top = max((count for _, count in counts), default=0)
    if top == 0:
        return "No votes"
    leaders = [text for text, count in counts if count == top]
    if len(leaders) == 1:
        return f"Winner: {leaders[0]}"
    return "Tie: " + ", ".join(leaders)


def build_results_embed(
    record: PollRecord,
    counts: Sequence[Tuple[str, int]],
    jump_url: str,
    expired: bool,
) -> discord.Embed:
    question = discord.utils.escape_markdown(record.question)
    embed = discord.Embed(
        title=truncate(f"{VOTE.emoji} Vote closed: {record.question}", 256),
        url=jump_url,
        description=f"**{question}**\n\n{format_results(counts)}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Opened by", value=f"<@{record.author_id}>")
    embed.add_field(
        name="Result",
        value=discord.utils.escape_markdown(describe_outcome(counts)),
    )
    embed.set_footer(
        text="Ended automatically after 32 days" if expired else "Closed by its author"
    )
    return embed


async def count_votes(poll: discord.Poll) -> List[Tuple[str, int]]:
    # Counts each answer's voters. ``voters()`` is given an explicit limit
    # because by default it stops at ``vote_count``, which Discord only
    # guarantees to be exact once the poll's results are finalised.
    counts = []
    for answer in poll.answers:
        try:
            count = 0
            async for _ in answer.voters(limit=VOTERS_LIMIT):
                count += 1
        except discord.HTTPException:
            logger.warning(
                "Failed to fetch poll voters answer_id=%s; using vote_count",
                answer.id,
                exc_info=True,
            )
            count = answer.vote_count
        counts.append((answer.text, count))
    return counts


def has_ended(poll: discord.Poll) -> bool:
    expires_at = poll.expires_at
    return expires_at is not None and expires_at <= discord.utils.utcnow()


class CloseSelect(discord.ui.Select["CloseMenu"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        if self.view is not None:
            await self.view.close_selected(interaction, int(self.values[0]))


class CloseMenu(discord.ui.View):
    # Lets the member who ran ?close_poll / ?close_vote pick one of their
    # open polls or votes to close.

    def __init__(
        self,
        cog: "Polls",
        kind: PollKind,
        author_id: int,
        records: Sequence[PollRecord],
    ):
        super().__init__(timeout=MENU_TIMEOUT)
        self.cog = cog
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self.select = CloseSelect(
            placeholder=f"Choose a {kind.key} to close",
            options=[
                discord.SelectOption(
                    label=truncate(record.question, 100),
                    value=str(record.message_id),
                    description=f"Opened {record.created_at:%b %d, %Y}",
                )
                for record in records
            ],
        )
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "This menu isn't yours.", ephemeral=True
        )
        return False

    async def close_selected(
        self, interaction: discord.Interaction, message_id: int
    ) -> None:
        self.stop()
        # Closing makes several API calls, which can take longer than the
        # 3 seconds Discord allows before an interaction must be answered.
        await interaction.response.defer()
        try:
            result = await self.cog.close_by_id(message_id)
        except sqlite3.Error:
            logger.exception("Failed to close poll message_id=%s", message_id)
            result = "Something went wrong. Please try again."
        await interaction.edit_original_response(
            content=result, view=None, allowed_mentions=discord.AllowedMentions.none()
        )

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        with contextlib.suppress(discord.HTTPException):
            await self.message.edit(
                content="This menu expired. Run the command again to close one.",
                view=None,
            )


class Polls(commands.Cog):
    def __init__(self, bot, store: PollStore):
        self.bot = bot
        self.store = store
        self._expiry_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        await super().cog_load()
        print("Polls Cog loaded.")
        # Close anything that expired while the bot was offline.
        self._expiry_task = asyncio.create_task(self.close_expired())

    async def _reply(self, ctx: commands.Context, content: str, **kwargs):
        return await ctx.reply(
            content,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            **kwargs,
        )

    def _get_channel(
        self, guild: discord.Guild, channel_id: int, name: str
    ) -> Optional[discord.TextChannel]:
        if channel_id:
            channel = self.bot.get_channel(channel_id)
        else:
            channel = discord.utils.get(guild.text_channels, name=name)
        if channel is None or not hasattr(channel, "send"):
            return None
        return cast(discord.TextChannel, channel)

    async def _open(self, ctx: commands.Context, kind: PollKind, text: str) -> None:
        guild = ctx.guild
        if guild is None:
            await self._reply(ctx, NOT_IN_GUILD_MESSAGE)
            return

        try:
            question, options = parse_poll_input(text)
        except PollInputError as error:
            await self._reply(
                ctx,
                f"{error}\nUsage: `?{kind.open_command} Question | Option 1 | "
                f"Option 2` (up to {MAX_OPTIONS} options)",
            )
            return

        channel = self._get_channel(guild, kind.channel_id, kind.channel_name)
        if channel is None:
            await self._reply(ctx, f"I can't find the #{kind.channel_name} channel.")
            return

        poll = discord.Poll(question=question, duration=POLL_DURATION)
        for option in options:
            poll.add_answer(text=option)

        try:
            message = await channel.send(
                f"{kind.emoji} {kind.title} by {ctx.author.mention}",
                poll=poll,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to post %s channel_id=%s author_id=%s",
                kind.key,
                channel.id,
                ctx.author.id,
            )
            await self._reply(
                ctx,
                f"I couldn't post your {kind.key} in {channel.mention}. I may be "
                "missing permission to send polls there.",
            )
            return

        expires_at = message.poll.expires_at if message.poll else None
        record = PollRecord(
            message_id=message.id,
            channel_id=channel.id,
            guild_id=guild.id,
            author_id=ctx.author.id,
            kind=kind.key,
            question=question,
            created_at=message.created_at,
            expires_at=expires_at or message.created_at + POLL_DURATION,
        )
        try:
            self.store.add(record)
        except sqlite3.Error:
            # An untracked poll could never be closed with the command.
            logger.exception("Failed to save %s message_id=%s", kind.key, message.id)
            with contextlib.suppress(discord.HTTPException):
                await message.delete()
            await self._reply(
                ctx,
                f"Something went wrong saving your {kind.key}, so I removed it. "
                "Please try again.",
            )
            return

        if getattr(ctx.channel, "id", None) == channel.id:
            # The poll itself is the confirmation; keep the channel tidy.
            with contextlib.suppress(discord.HTTPException):
                await ctx.message.delete()
            return

        await self._reply(
            ctx, f"Posted your {kind.key} in {channel.mention}: {message.jump_url}"
        )

    async def _prompt_close(self, ctx: commands.Context, kind: PollKind) -> None:
        guild = ctx.guild
        if guild is None:
            await self._reply(ctx, NOT_IN_GUILD_MESSAGE)
            return

        records = self.store.list_open(guild.id, ctx.author.id, kind.key)
        if not records:
            await self._reply(ctx, f"You don't have any open {kind.key}s.")
            return

        content = f"Which {kind.key} do you want to close?"
        if len(records) > MENU_LIMIT:
            content += f" (showing your {MENU_LIMIT} newest)"
        view = CloseMenu(self, kind, ctx.author.id, records[:MENU_LIMIT])
        view.message = await self._reply(ctx, content, view=view)

    async def close_by_id(self, message_id: int) -> str:
        record = self.store.get(message_id)
        if record is None:
            return "I can't find that anymore."
        return await self.close_item(record)

    async def _fetch_poll_message(self, record: PollRecord) -> discord.Message:
        channel = self.bot.get_channel(record.channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            channel = self.bot.get_partial_messageable(
                record.channel_id, guild_id=record.guild_id
            )
        messageable = cast(discord.abc.Messageable, channel)
        return await messageable.fetch_message(record.message_id)

    async def _fetch_if_ended(self, record: PollRecord) -> Optional[discord.Message]:
        # Returns the poll's message if its poll has already ended.
        try:
            message = await self._fetch_poll_message(record)
        except discord.HTTPException:
            return None
        if message.poll is None or not has_ended(message.poll):
            return None
        return message

    async def close_item(self, record: PollRecord, *, expired: bool = False) -> str:
        # Ends the Discord poll, and for a vote posts the results to mod_chat.
        # Returns a message describing the outcome for the member closing it.
        kind = KINDS[record.kind]
        if not self.store.claim_close(record.message_id):
            return f"That {kind.key} is already closed."

        try:
            message = await self._fetch_poll_message(record)
        except discord.NotFound:
            return f"That {kind.key} was deleted, so I've removed it from your list."
        except discord.HTTPException:
            logger.exception(
                "Failed to fetch %s message_id=%s", kind.key, record.message_id
            )
            self.store.reopen(record.message_id)
            return f"I couldn't reach that {kind.key}. Please try again."

        if message.poll is None:
            return f"That {kind.key} no longer has a poll, so I've removed it."

        if has_ended(message.poll):
            expired = True
        else:
            try:
                message = await message.end_poll()
            except discord.HTTPException:
                ended = await self._fetch_if_ended(record)
                if ended is None:
                    logger.exception(
                        "Failed to end %s message_id=%s", kind.key, record.message_id
                    )
                    self.store.reopen(record.message_id)
                    return f"I couldn't close that {kind.key}. Please try again."
                message, expired = ended, True  # It ran out of time meanwhile.

        poll = message.poll
        counts = await count_votes(poll) if poll is not None else []
        result = (
            f"Closed your {kind.key} **{discord.utils.escape_markdown(record.question)}**: "
            f"{message.jump_url}"
        )
        if not kind.reports_to_mods:
            return result

        mod_channel = await self._report_vote(record, counts, message.jump_url, expired)
        if mod_channel is None:
            return (
                f"{result}\nI couldn't post the results in #{MOD_CHANNEL_NAME}, so "
                "please let a moderator know."
            )
        return f"{result}\nThe results were posted in {mod_channel.mention}."

    async def _report_vote(
        self,
        record: PollRecord,
        counts: Sequence[Tuple[str, int]],
        jump_url: str,
        expired: bool,
    ) -> Optional[discord.TextChannel]:
        guild = self.bot.get_guild(record.guild_id)
        channel = (
            self._get_channel(guild, MOD_CHANNEL_ID, MOD_CHANNEL_NAME)
            if guild
            else None
        )
        if channel is None:
            logger.error(
                "Can't find #%s to report vote message_id=%s guild_id=%s",
                MOD_CHANNEL_NAME,
                record.message_id,
                record.guild_id,
            )
            return None

        try:
            await channel.send(
                embed=build_results_embed(record, counts, jump_url, expired),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to report vote message_id=%s to channel_id=%s",
                record.message_id,
                channel.id,
            )
            return None
        return channel

    async def close_expired(self) -> None:
        await self.bot.wait_until_ready()
        for record in self.store.list_expired(discord.utils.utcnow()):
            try:
                await self.close_item(record, expired=True)
            except sqlite3.Error:
                logger.exception(
                    "Failed to close expired poll message_id=%s", record.message_id
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Discord posts a poll result message when a poll ends. Polls closed
        # with the commands are already marked closed, so this only acts on
        # polls that ran out of time.
        if message.type is not discord.MessageType.poll_result:
            return
        message_id = message.reference.message_id if message.reference else None
        if message_id is None:
            return

        record = self.store.get(message_id)
        if record is not None and not record.closed:
            await self.close_item(record, expired=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # A deleted poll can't be closed anymore, so drop it from its author's list.
        self.store.claim_close(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ):
        for message_id in payload.message_ids:
            self.store.claim_close(message_id)

    @commands.command(
        name="open_poll",
        help="Post a poll in #active_polls: ?open_poll Question | Option 1 | Option 2",
    )
    async def open_poll(self, ctx: commands.Context, *, text: str = ""):
        await self._open(ctx, POLL, text)

    @commands.command(
        name="open_vote",
        help="Post a vote in #active_votes: ?open_vote Question | Option 1 | Option 2",
    )
    async def open_vote(self, ctx: commands.Context, *, text: str = ""):
        await self._open(ctx, VOTE, text)

    @commands.command(name="close_poll", help="Choose one of your open polls to close.")
    async def close_poll(self, ctx: commands.Context):
        await self._prompt_close(ctx, POLL)

    @commands.command(
        name="close_vote",
        help="Choose one of your open votes to close; its results go to #mod_chat.",
    )
    async def close_vote(self, ctx: commands.Context):
        await self._prompt_close(ctx, VOTE)


async def setup_polls(bot, guilds, store: Optional[PollStore]):
    if store is None:
        raise RuntimeError("Polls need the poll database, which failed to open")
    await bot.add_cog(Polls(bot, store), guilds=guilds)
