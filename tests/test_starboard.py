import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STARBOARD_PATH = ROOT / "cogs" / "starboard.py"
spec = importlib.util.spec_from_file_location("starboard", STARBOARD_PATH)
assert spec is not None
assert spec.loader is not None
starboard_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(starboard_module)
Starboard = starboard_module.Starboard
discord = starboard_module.discord


class FakeChannel:
    mention = "#general"

    def __init__(self, nsfw=False, channel_id=456):
        self._nsfw = nsfw
        self.id = channel_id
        self.send = AsyncMock()

    def is_nsfw(self):
        return self._nsfw


class FakeBot:
    def get_channel(self, _channel_id):
        return SimpleNamespace()


def make_reaction(
    count, emoji="<:OnPhone:1062142401973588039>", channel=None, reactions=None
):
    return SimpleNamespace(
        emoji=emoji,
        count=count,
        message=SimpleNamespace(
            id=123,
            channel=channel or FakeChannel(),
            reactions=reactions or [],
        ),
    )


def make_forbidden():
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(
        response,
        {"code": 50013, "message": "Missing Permissions"},
    )


def test_starboard_requires_three_matching_reactions_before_posting():
    starboard = Starboard(FakeBot())
    starboard.create_starboard_post = AsyncMock()
    starboard.update_reaction_count = AsyncMock()

    asyncio.run(starboard.on_reaction_add(make_reaction(1), SimpleNamespace()))
    asyncio.run(starboard.on_reaction_add(make_reaction(2), SimpleNamespace()))

    starboard.create_starboard_post.assert_not_called()
    starboard.update_reaction_count.assert_not_called()

    asyncio.run(starboard.on_reaction_add(make_reaction(3), SimpleNamespace()))

    starboard.create_starboard_post.assert_awaited_once()
    starboard.update_reaction_count.assert_not_called()


def test_starboard_ignores_other_emoji_under_five_reactions():
    starboard = Starboard(FakeBot())
    starboard.create_starboard_post = AsyncMock()
    starboard.update_reaction_count = AsyncMock()

    asyncio.run(
        starboard.on_reaction_add(make_reaction(3, emoji="👍"), SimpleNamespace())
    )

    starboard.create_starboard_post.assert_not_called()
    starboard.update_reaction_count.assert_not_called()


def test_starboard_posts_other_emoji_at_five_reactions():
    starboard = Starboard(FakeBot())
    starboard.create_starboard_post = AsyncMock()
    starboard.update_reaction_count = AsyncMock()

    asyncio.run(
        starboard.on_reaction_add(make_reaction(4, emoji="👍"), SimpleNamespace())
    )

    starboard.create_starboard_post.assert_not_called()
    starboard.update_reaction_count.assert_not_called()

    asyncio.run(
        starboard.on_reaction_add(make_reaction(5, emoji="👍"), SimpleNamespace())
    )

    starboard.create_starboard_post.assert_awaited_once()
    starboard.update_reaction_count.assert_not_called()


def test_starboard_title_includes_all_qualifying_reactions():
    starboard = Starboard(FakeBot())
    reactions = [
        SimpleNamespace(emoji="<:OnPhone:1062142401973588039>", count=3),
        SimpleNamespace(emoji="👍", count=5),
        SimpleNamespace(emoji="👎", count=4),
    ]
    react = make_reaction(5, emoji="👍", reactions=reactions)

    assert (
        starboard._get_title(react)
        == "<:OnPhone:1062142401973588039> x **3** 👍 x **5** |#general"
    )


def test_starboard_updates_existing_post_when_new_emoji_qualifies():
    starboard = Starboard(FakeBot())
    starboarded_msg = SimpleNamespace(edit=AsyncMock())
    starboard_channel = SimpleNamespace(
        fetch_message=AsyncMock(return_value=starboarded_msg)
    )
    starboard.starboard_msgs[123] = {
        "post_id": 789,
        "emoji": "<:OnPhone:1062142401973588039>",
        "channel": starboard_channel,
    }
    reactions = [
        SimpleNamespace(emoji="<:OnPhone:1062142401973588039>", count=3),
        SimpleNamespace(emoji="👍", count=5),
    ]

    asyncio.run(
        starboard.on_reaction_add(
            make_reaction(5, emoji="👍", reactions=reactions),
            SimpleNamespace(),
        )
    )

    starboarded_msg.edit.assert_awaited_once_with(
        content="<:OnPhone:1062142401973588039> x **3** 👍 x **5** |#general"
    )


def test_starboard_removes_reaction_from_title_when_it_drops_below_threshold():
    starboard = Starboard(FakeBot())
    starboarded_msg = SimpleNamespace(edit=AsyncMock())
    starboard_channel = SimpleNamespace(
        fetch_message=AsyncMock(return_value=starboarded_msg)
    )
    starboard.starboard_msgs[123] = {
        "post_id": 789,
        "emoji": "<:OnPhone:1062142401973588039>",
        "channel": starboard_channel,
    }
    reactions = [
        SimpleNamespace(emoji="<:OnPhone:1062142401973588039>", count=2),
        SimpleNamespace(emoji="👍", count=5),
    ]

    asyncio.run(
        starboard.on_reaction_remove(
            make_reaction(
                2, emoji="<:OnPhone:1062142401973588039>", reactions=reactions
            ),
            SimpleNamespace(),
        )
    )

    starboarded_msg.edit.assert_awaited_once_with(content="👍 x **5** |#general")


def test_starboard_deletes_post_when_no_reactions_still_qualify():
    starboard = Starboard(FakeBot())
    starboarded_msg = SimpleNamespace(delete=AsyncMock())
    starboard_channel = SimpleNamespace(
        fetch_message=AsyncMock(return_value=starboarded_msg)
    )
    starboard.starboard_msgs[123] = {
        "post_id": 789,
        "emoji": "<:OnPhone:1062142401973588039>",
        "channel": starboard_channel,
    }
    reactions = [SimpleNamespace(emoji="<:OnPhone:1062142401973588039>", count=2)]

    asyncio.run(
        starboard.on_reaction_remove(
            make_reaction(
                2, emoji="<:OnPhone:1062142401973588039>", reactions=reactions
            ),
            SimpleNamespace(),
        )
    )

    starboarded_msg.delete.assert_awaited_once()
    assert starboard.starboard_msgs == {}


def test_starboard_ignores_reactions_in_starboard_channel():
    starboard = Starboard(FakeBot())
    starboard.create_starboard_post = AsyncMock()
    starboard.update_reaction_count = AsyncMock()

    asyncio.run(
        starboard.on_reaction_add(
            make_reaction(
                5,
                emoji="👍",
                channel=FakeChannel(channel_id=starboard.starboard_channel_id),
            ),
            SimpleNamespace(),
        )
    )

    starboard.create_starboard_post.assert_not_called()
    starboard.update_reaction_count.assert_not_called()


def test_starboard_reacts_to_post_with_starboarded_emoji():
    starboard = Starboard(FakeBot())
    starboard._build_embeds = AsyncMock(return_value=[])
    starboard._get_open_msg_view = AsyncMock(return_value=SimpleNamespace())
    starboarded_msg = SimpleNamespace(id=789, add_reaction=AsyncMock())
    starboard.starboard_channel = SimpleNamespace(
        send=AsyncMock(return_value=starboarded_msg)
    )

    asyncio.run(starboard.create_starboard_post(make_reaction(5, emoji="👍")))

    starboarded_msg.add_reaction.assert_awaited_once_with("👍")
    assert starboard.starboard_msgs[123] == {
        "post_id": 789,
        "emoji": "👍",
        "channel": starboard.starboard_channel,
    }


def test_starboard_does_not_fallback_when_only_add_reaction_fails():
    starboard = Starboard(FakeBot())
    starboard._build_embeds = AsyncMock(return_value=[])
    starboard._get_open_msg_view = AsyncMock(return_value=SimpleNamespace())
    starboarded_msg = SimpleNamespace(
        id=789,
        add_reaction=AsyncMock(side_effect=make_forbidden()),
    )
    starboard.starboard_channel = SimpleNamespace(
        send=AsyncMock(return_value=starboarded_msg)
    )
    source_channel = FakeChannel()

    asyncio.run(
        starboard.create_starboard_post(
            make_reaction(5, emoji="👍", channel=source_channel)
        )
    )

    starboard.starboard_channel.send.assert_awaited_once()
    source_channel.send.assert_not_called()
    assert starboard.starboard_msgs[123] == {
        "post_id": 789,
        "emoji": "👍",
        "channel": starboard.starboard_channel,
    }


def test_starboard_forbidden_onphone_case_posts_fallback_to_source_channel():
    starboard = Starboard(FakeBot())
    original_embed = discord.Embed(description="original message")
    starboard._build_embeds = AsyncMock(return_value=[original_embed])
    starboard._get_open_msg_view = AsyncMock(return_value=SimpleNamespace())
    starboard.starboard_channel = SimpleNamespace(
        send=AsyncMock(side_effect=make_forbidden())
    )
    fallback_msg = SimpleNamespace(id=789, add_reaction=AsyncMock())
    source_channel = FakeChannel()
    source_channel.send = AsyncMock(return_value=fallback_msg)

    asyncio.run(
        starboard.create_starboard_post(make_reaction(3, channel=source_channel))
    )

    source_channel.send.assert_awaited_once()
    _, kwargs = source_channel.send.call_args
    assert len(kwargs["embeds"]) == 2
    petition_embed = kwargs["embeds"][1]
    assert (
        "Please <:OnPhone:1062142401973588039> my petition"
        in petition_embed.description
    )
    assert "tenor.com" not in petition_embed.description
    assert (
        petition_embed.to_dict()["image"]["url"]
        == "https://media1.tenor.com/m/zdkG7NnnREoAAAAd/propaganda.gif"
    )
    fallback_msg.add_reaction.assert_awaited_once_with("<:OnPhone:1062142401973588039>")
    assert starboard.starboard_msgs[123] == {
        "post_id": 789,
        "emoji": "<:OnPhone:1062142401973588039>",
        "channel": source_channel,
    }


def test_starboard_forbidden_other_emoji_posts_fallback_to_source_channel():
    starboard = Starboard(FakeBot())
    original_embed = discord.Embed(description="original message")
    starboard._build_embeds = AsyncMock(return_value=[original_embed])
    starboard._get_open_msg_view = AsyncMock(return_value=SimpleNamespace())
    starboard.starboard_channel = SimpleNamespace(
        send=AsyncMock(side_effect=make_forbidden())
    )
    fallback_msg = SimpleNamespace(id=789, add_reaction=AsyncMock())
    source_channel = FakeChannel()
    source_channel.send = AsyncMock(return_value=fallback_msg)

    asyncio.run(
        starboard.create_starboard_post(
            make_reaction(5, emoji="👍", channel=source_channel)
        )
    )

    source_channel.send.assert_awaited_once()
    _, kwargs = source_channel.send.call_args
    assert len(kwargs["embeds"]) == 2
    petition_embed = kwargs["embeds"][1]
    assert (
        "Please <:OnPhone:1062142401973588039> my petition"
        in petition_embed.description
    )
    assert (
        petition_embed.to_dict()["image"]["url"]
        == "https://media1.tenor.com/m/zdkG7NnnREoAAAAd/propaganda.gif"
    )
    fallback_msg.add_reaction.assert_awaited_once_with("👍")
    assert starboard.starboard_msgs[123] == {
        "post_id": 789,
        "emoji": "👍",
        "channel": source_channel,
    }
