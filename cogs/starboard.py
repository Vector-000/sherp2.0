import logging
from typing import List, Optional, cast
import discord
from discord.ext import commands

from helper import get_config

logger = logging.getLogger(__name__)

__DEFAULT_CHANNEL_ID = 1133260871049691257
__DEFAULT_ONPHONE_EMOJI_STR = "<:OnPhone:1062142401973588039>"
__DEFAULT_ONPHONE_THRESHOLD = 3
__DEFAULT_OTHER_THRESHOLD = 5
PROPAGANDA_GIF_URL = "https://media1.tenor.com/m/zdkG7NnnREoAAAAd/propaganda.gif"

__cfg = get_config().get("starboard", None)
STARBOARD_CHANNEL_ID = (
    __cfg.get("channel", __DEFAULT_CHANNEL_ID) if __cfg else __DEFAULT_CHANNEL_ID
)
STARBOARD_ONPHONE_EMOJI_STR = (
    __cfg.get("onphone_emoji", __cfg.get("emoji", __DEFAULT_ONPHONE_EMOJI_STR))
    if __cfg
    else __DEFAULT_ONPHONE_EMOJI_STR
)

STARBOARD_ONPHONE_THRESHOLD = (
    __cfg.get("onphone_threshold", __DEFAULT_ONPHONE_THRESHOLD)
    if __cfg
    else __DEFAULT_ONPHONE_THRESHOLD
)

STARBOARD_OTHER_THRESHOLD = (
    __cfg.get("other_threshold", __DEFAULT_OTHER_THRESHOLD)
    if __cfg
    else __DEFAULT_OTHER_THRESHOLD
)


class Starboard(commands.Cog):
    def __init__(self, bot: discord.Client):
        self.onphone_emoji_str = STARBOARD_ONPHONE_EMOJI_STR
        self.onphone_threshold = STARBOARD_ONPHONE_THRESHOLD
        self.other_threshold = STARBOARD_OTHER_THRESHOLD
        self.starboard_channel_id = STARBOARD_CHANNEL_ID
        # Starboarded Message ID -> {"post_id": bot post ID, "emoji": starboard emoji, "channel": post channel}
        self.starboard_msgs = dict()
        self.starboard_channel = bot.get_channel(self.starboard_channel_id)

    async def cog_load(self):
        await super().cog_load()
        print("Starboard Cog loaded.")

    def _get_threshold(self, emoji: str) -> int:
        if emoji == self.onphone_emoji_str:
            return self.onphone_threshold
        return self.other_threshold

    def _get_channel_id(self, channel: object) -> Optional[int]:
        return getattr(channel, "id", None)

    def _get_qualifying_reactions(
        self, msg: discord.Message, fallback_react: Optional[discord.Reaction] = None
    ) -> List[discord.Reaction]:
        reactions = list(getattr(msg, "reactions", None) or [])
        if fallback_react and all(
            str(reaction.emoji) != str(fallback_react.emoji) for reaction in reactions
        ):
            reactions.append(fallback_react)

        return [
            reaction
            for reaction in reactions
            if reaction.count >= self._get_threshold(str(reaction.emoji))
        ]

    def _get_title(self, react: discord.Reaction) -> Optional[str]:
        reactions = self._get_qualifying_reactions(react.message, react)
        if not reactions:
            return None

        counts = " ".join(
            f"{reaction.emoji} x **{reaction.count}**" for reaction in reactions
        )
        return f"{counts} |{react.message.channel.mention}"

    async def _get_open_msg_view(self, msg: discord.Message) -> discord.ui.View:
        btn = discord.ui.Button(label="Jump", url=msg.jump_url)
        v = discord.ui.View().add_item(btn)

        if msg.type != discord.MessageType.reply:
            return v

        reply = msg.reference.cached_message or await msg.channel.fetch_message(
            msg.reference.message_id
        )
        return v.add_item(discord.ui.Button(label="Context", url=reply.jump_url))

    async def update_reaction_count(self, react: discord.Reaction) -> None:
        title = self._get_title(react)
        if title is None:
            await self.delete_starboard_post(react.message.id)
            return

        record = self.starboard_msgs[react.message.id]
        msg_id = record["post_id"]
        channel = record.get("channel", self.starboard_channel)
        msg: discord.Message = await channel.fetch_message(msg_id)
        await msg.edit(content=title)

    async def delete_starboard_post(self, message_id: int) -> None:
        record = self.starboard_msgs.pop(message_id, None)
        if not record:
            return

        channel = record.get("channel", self.starboard_channel)
        try:
            msg = await channel.fetch_message(record["post_id"])
            await msg.delete()
        except discord.HTTPException:
            logger.warning(
                "Failed to delete starboard post source_message_id=%s "
                "starboard_post_id=%s starboard_channel_id=%s",
                message_id,
                record["post_id"],
                self._get_channel_id(channel),
                exc_info=True,
            )

    def _get_first_viable_attachment_url(
        self, atmnts: List[discord.Attachment]
    ) -> Optional[str]:
        for a in atmnts:
            if a.url.split("?")[0].endswith(("png", "jpeg", "jpg", "gif", "webp")):
                return a.url
        return None

    def _get_starboard_embed(self, msg: discord.Message) -> discord.Embed:
        embed = discord.Embed(
            description=msg.content or msg.system_content,
            color=discord.Color.dark_green(),
        ).set_author(name=msg.author.display_name, icon_url=msg.author.avatar.url)

        if u := self._get_first_viable_attachment_url(msg.attachments):
            embed.set_image(url=u)

        return embed

    async def _build_embeds(self, msg: discord.Message) -> List[discord.Embed]:
        main_embed = self._get_starboard_embed(msg)

        if msg.type != discord.MessageType.reply:
            return [main_embed]

        reply_to = msg.reference.cached_message or await msg.channel.fetch_message(
            msg.reference.message_id
        )
        atcmnt_url = self._get_first_viable_attachment_url(reply_to.attachments)
        # If the replied to message has no attachments, we simply add a field
        # to the main embed.
        if not atcmnt_url:
            main_embed.add_field(
                name="Reply to the message:",
                value=reply_to.content or reply_to.system_content,
            )
            return [main_embed]

        # If the replied-to message has attachments, we need another embed.
        reply_embed = discord.Embed(
            title="Reply to this message:",
            description=reply_to.content or reply_to.system_content,
            color=discord.Color.dark_green(),
        ).set_image(url=atcmnt_url)

        return [main_embed, reply_embed]

    def _get_no_starboard_access_embed(self) -> discord.Embed:
        return discord.Embed(
            description=(
                "I would love to starboard your message but I don't have access "
                f"to that channel. Please {self.onphone_emoji_str} my petition"
            ),
            color=discord.Color.gold(),
        ).set_image(url=PROPAGANDA_GIF_URL)

    async def _try_add_starboard_reaction(
        self, msg: discord.Message, emoji: discord.PartialEmoji | discord.Emoji | str
    ) -> None:
        try:
            await msg.add_reaction(emoji)
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Failed to add reaction to starboard post starboard_post_id=%s "
                "starboard_channel_id=%s emoji=%s",
                getattr(msg, "id", None),
                self._get_channel_id(getattr(msg, "channel", None)),
                emoji,
                exc_info=True,
            )

    async def _send_starboard_copy(
        self,
        channel: discord.abc.Messageable,
        react: discord.Reaction,
        embeds: List[discord.Embed],
        open_msg_view: discord.ui.View,
    ) -> None:
        title = self._get_title(react)
        if title is None:
            return

        msg: discord.Message = await channel.send(
            title, embeds=embeds, view=open_msg_view
        )
        self.starboard_msgs[react.message.id] = {
            "post_id": msg.id,
            "emoji": str(react.emoji),
            "channel": channel,
        }
        await self._try_add_starboard_reaction(msg, react.emoji)

    async def _handle_starboard_send_failure(
        self,
        react: discord.Reaction,
        embeds: List[discord.Embed],
        open_msg_view: discord.ui.View,
    ) -> None:
        fallback_embeds = embeds + [self._get_no_starboard_access_embed()]
        try:
            await self._send_starboard_copy(
                react.message.channel, react, fallback_embeds, open_msg_view
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to send starboard fallback source_message_id=%s "
                "source_channel_id=%s starboard_channel_id=%s emoji=%s "
                "reaction_count=%s",
                react.message.id,
                self._get_channel_id(react.message.channel),
                self.starboard_channel_id,
                react.emoji,
                react.count,
            )
            raise

    async def create_starboard_post(self, react: discord.Reaction):
        embeds = await self._build_embeds(react.message)
        open_msg_view = await self._get_open_msg_view(react.message)

        if self.starboard_channel is None:
            logger.warning(
                "Starboard channel unavailable; sending fallback "
                "source_message_id=%s source_channel_id=%s "
                "starboard_channel_id=%s emoji=%s reaction_count=%s",
                react.message.id,
                self._get_channel_id(react.message.channel),
                self.starboard_channel_id,
                react.emoji,
                react.count,
            )
            await self._handle_starboard_send_failure(react, embeds, open_msg_view)
            return

        starboard_channel = cast(discord.abc.Messageable, self.starboard_channel)
        try:
            await self._send_starboard_copy(
                starboard_channel, react, embeds, open_msg_view
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to send starboard post to configured channel "
                "source_message_id=%s source_channel_id=%s "
                "starboard_channel_id=%s emoji=%s reaction_count=%s",
                react.message.id,
                self._get_channel_id(react.message.channel),
                self.starboard_channel_id,
                react.emoji,
                react.count,
            )
            await self._handle_starboard_send_failure(react, embeds, open_msg_view)

    @commands.Cog.listener()
    async def on_reaction_add(self, react: discord.Reaction, _: discord.User):
        if react.message.channel.is_nsfw():
            return

        if getattr(react.message.channel, "id", None) == self.starboard_channel_id:
            return

        emoji = str(react.emoji)
        threshold = self._get_threshold(emoji)
        if react.count < threshold:
            return

        if react.message.id in self.starboard_msgs:
            await self.update_reaction_count(react)
        else:
            await self.create_starboard_post(react)

    @commands.Cog.listener()
    async def on_reaction_remove(self, react: discord.Reaction, _: discord.User):
        if react.message.id not in self.starboard_msgs:
            return

        await self.update_reaction_count(react)

    @commands.Cog.listener()
    async def on_message_delete(self, msg: discord.Message):
        await self.delete_starboard_post(msg.id)


async def setup_starboard(bot, guilds):
    await bot.add_cog(Starboard(bot), guilds=guilds)
