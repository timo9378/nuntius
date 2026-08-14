"""Exercises the Discord-reply-to-GitHub-blockquote rendering."""

import asyncio
import os
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("GITHUB_BOT_TOKEN", "x")

import discord

import store
from cogs.devflow import DevFlow

OK = [0, 0]


def check(label, actual, expected):
    good = actual == expected
    OK[0] += 1
    OK[1] += good
    print(f"  {'PASS' if good else 'FAIL'}  {label}: {actual!r}" + ("" if good else f"\n        != {expected!r}"))


class Author:
    def __init__(self, uid, name="某人", bot=False):
        self.id = uid
        self.display_name = name
        self.bot = bot

    def __eq__(self, other):
        return getattr(other, "id", None) == self.id


class Msg:
    def __init__(self, content="", author=None, embeds=(), reference=None,
                 mtype=discord.MessageType.default, channel=None):
        self.content = content
        self.clean_content = content
        self.author = author or Author(1)
        self.embeds = list(embeds)
        self.reference = reference
        self.type = mtype
        self.channel = channel
        self.mentions = []
        self.role_mentions = []
        self.channel_mentions = []


class Channel:
    def __init__(self, target=None):
        self.id = 42
        self._target = target

    async def fetch_message(self, _id):
        if self._target is None:
            raise discord.NotFound(_Resp(), "gone")
        return self._target


class _Resp:
    status = 404
    reason = "Not Found"


class Ref:
    def __init__(self, message_id):
        self.message_id = message_id
        self.resolved = None


class Bot:
    user = Author(9999, "nuntius", bot=True)


cog = DevFlow.__new__(DevFlow)
cog.bot = Bot()
cog.user_mappings = {"1": {"github_username": "koimsurai", "access_token": "t"}}
cog.user_mappings_file = store.USER_MAPPINGS_FILE
cog.pending_oauth_states = {}


async def main():
    print("\n不是回覆的訊息")
    plain = Msg("普通留言")
    check("沒有引用", await cog._quoted_reply(plain), "")

    print("\n回覆一則已同步的訊息")
    store.remember_comment(777, "Limatura/tessera", 3, 555)
    target = Msg("就繼續issue開了都不回 這repo沒人維護了", author=Author(1))
    channel = Channel(target)
    reply = Msg("你用AI回一堆這不算啦", author=Author(2, "Æ"),
                reference=Ref(777), mtype=discord.MessageType.reply, channel=channel)
    quoted = await cog._quoted_reply(reply)
    check("引用有連到那則留言",
          "https://github.com/Limatura/tessera/issues/3#issuecomment-555" in quoted, True)
    check("用的是 GitHub 帳號而不是暱稱", "@koimsurai" in quoted, True)
    check("引文帶進來", "就繼續issue開了都不回" in quoted, True)
    check("是 blockquote", quoted.startswith("> "), True)
    check("後面空一行", quoted.endswith("\n\n"), True)

    print("\n完整的 _render_for_github")
    body = await cog._render_for_github(reply)
    check("引用在前、本文在後", body.endswith("你用AI回一堆這不算啦"), True)
    check("quote=False 就沒有引用",
          await cog._render_for_github(reply, quote=False), "你用AI回一堆這不算啦")

    print("\n回覆一則沒同步過的訊息")
    lonely = Msg("沒同步過", author=Author(1))
    reply2 = Msg("回它", author=Author(2), reference=Ref(31337),
                 mtype=discord.MessageType.reply, channel=Channel(lonely))
    quoted = await cog._quoted_reply(reply2)
    check("還是有引用", "沒同步過" in quoted, True)
    check("但沒有連結", "issuecomment" not in quoted, True)

    print("\n回覆一則從 GitHub 鏡過來的訊息")
    embed = discord.Embed(description="這是 GitHub 上的原文")
    embed.set_author(name="octocat 在 GitHub 留言")
    mirrored = Msg("", author=Bot.user, embeds=[embed])
    store.remember_comment(888, "Limatura/tessera", 3, 666)
    reply3 = Msg("我回它", author=Author(2), reference=Ref(888),
                 mtype=discord.MessageType.reply, channel=Channel(mirrored))
    quoted = await cog._quoted_reply(reply3)
    check("認出 GitHub 作者", "@octocat" in quoted, True)
    check("引文取自 embed", "這是 GitHub 上的原文" in quoted, True)
    check("連到那則 GitHub 留言", "#issuecomment-666" in quoted, True)

    print("\n被回覆的訊息已經被刪掉")
    reply4 = Msg("回它", author=Author(2), reference=Ref(12345),
                 mtype=discord.MessageType.reply, channel=Channel(None))
    quoted = await cog._quoted_reply(reply4)
    check("不會炸,只是沒有引文", quoted, "> **先前的留言** 說：\n\n")

    print("\n引文過長會截斷")
    long_target = Msg("啊" * 500, author=Author(1))
    reply5 = Msg("嗯", author=Author(2), reference=Ref(999),
                 mtype=discord.MessageType.reply, channel=Channel(long_target))
    quoted = await cog._quoted_reply(reply5)
    check("截到上限", quoted.count("啊"), cog.QUOTE_LIMIT)
    check("有省略號", "…" in quoted, True)

    print("\n顯示名稱裡的方括號")
    weird = Msg("內容", author=Author(3, "[管理員]阿明"))
    reply6 = Msg("嗨", author=Author(2), reference=Ref(4242),
                 mtype=discord.MessageType.reply, channel=Channel(weird))
    quoted = await cog._quoted_reply(reply6)
    check("方括號被跳脫", "\\[管理員\\]阿明" in quoted, True)

    print(f"\n{OK[1]}/{OK[0]} 通過")
    return 0 if OK[1] == OK[0] else 1


sys.exit(asyncio.run(main()))
