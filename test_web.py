"""Exercises the HTTP side without a Discord connection.

Not a unit-test suite — a smoke test that the ported endpoints answer, that a
forged delivery is refused, and that a real one finds the thread the previous
implementation could not.
"""

import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile

WORKDIR = tempfile.mkdtemp()
os.environ["DATA_DIR"] = WORKDIR
os.environ["GITHUB_WEBHOOK_SECRET"] = "test-secret"

import aiohttp

import store
from web import server

import discord

SENT: list[tuple[int, dict]] = []
ARCHIVED: list[bool] = []
EDITED: list[dict] = []


def _announcement_embed() -> discord.Embed:
    embed = discord.Embed(title="🚀 新開發任務已啟動！")
    embed.add_field(name="📊 狀態", value="🟢 開發中", inline=False)
    embed.add_field(name="⏱️ 開始時間", value="<t:1755000000:F>", inline=False)
    return embed


class StubMessage:
    """The task announcement the thread hangs off."""

    def __init__(self):
        self.embeds = [_announcement_embed()]
        # Enough for `_disable_buttons` to find something to disable.
        self.components = []

    async def edit(self, **changes):
        embed = changes.get("embed")
        status = next((f.value for f in embed.fields if f.name == "📊 狀態"), None)
        EDITED.append({"status": status, "changes": set(changes)})


class StubParent:
    def __init__(self, message):
        self._message = message

    async def fetch_message(self, _message_id):
        return self._message


class StubThread:
    def __init__(self, channel_id, announcement):
        self.id = channel_id
        self.parent = StubParent(announcement)

    async def send(self, **kwargs):
        SENT.append((self.id, kwargs))

    async def edit(self, **kwargs):
        if "archived" in kwargs:
            ARCHIVED.append(kwargs["archived"])


class StubCog:
    """Only the two methods the webhook borrows from the real cog."""

    def _update_embed_for_completion(self, embed):
        copy = embed.copy()
        index = next((i for i, f in enumerate(copy.fields) if f.name == "📊 狀態"), -1)
        copy.set_field_at(index, name="📊 狀態", value="✅ 已完成", inline=False)
        return copy

    def _disable_buttons(self, _message):
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="我想協作", disabled=True, custom_id="x"))
        return view


class StubBot:
    def __init__(self):
        self.announcement = StubMessage()

    def get_channel(self, channel_id):
        return StubThread(channel_id, self.announcement)

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)

    def get_cog(self, _name):
        return StubCog()


def signed(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def check(label: str, actual, expected) -> bool:
    ok = actual == expected
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual!r}" + ("" if ok else f"  (預期 {expected!r})"))
    return ok


async def main() -> int:
    # The shape the bot actually writes: issue_number is an int.
    store.write_threads({"555000111": {"issue_number": 42, "repo": "Limatura/tessera"}})

    results = []
    print("store.thread_for_issue —— 這是舊版永遠回 None 的地方")
    results.append(check("int 型別的 issue 找得到", store.thread_for_issue("Limatura/tessera", 42), "555000111"))
    results.append(check("字串形式也找得到", store.thread_for_issue("Limatura/tessera", "42"), "555000111"))
    results.append(check("別的 repo 的 #42 不會誤中", store.thread_for_issue("other/repo", 42), None))
    results.append(check("不存在的 issue 回 None", store.thread_for_issue("Limatura/tessera", 99), None))

    runner = await server.start(StubBot(), "127.0.0.1", 18099)
    base = "http://127.0.0.1:18099"
    try:
        async with aiohttp.ClientSession() as session:
            print("\nHTTP 端點")
            async with session.get(f"{base}/health") as response:
                results.append(check("GET /health", response.status, 200))

            body = json.dumps({"zen": "hi"}).encode()
            async with session.post(
                f"{base}/github/webhook",
                data=body,
                headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": signed(body)},
            ) as response:
                results.append(check("簽章正確的 ping", response.status, 200))

            async with session.post(
                f"{base}/github/webhook",
                data=body,
                headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": "sha256=" + "0" * 64},
            ) as response:
                results.append(check("偽造的簽章被擋", response.status, 401))

            async with session.post(
                f"{base}/github/webhook",
                data=body,
                headers={"X-GitHub-Event": "ping"},
            ) as response:
                results.append(check("完全沒簽章被擋", response.status, 401))

            # The SHA-1 header GitHub still sends. Accepting it would let a
            # forger pick the weaker algorithm.
            sha1 = "sha1=" + hmac.new(b"test-secret", body, hashlib.sha1).hexdigest()
            async with session.post(
                f"{base}/github/webhook",
                data=body,
                headers={"X-GitHub-Event": "ping", "X-Hub-Signature": sha1},
            ) as response:
                results.append(check("只帶 SHA-1 的簽章不被接受", response.status, 401))

            print("\nissue_comment → Discord")
            comment = json.dumps(
                {
                    "action": "created",
                    "repository": {"full_name": "Limatura/tessera"},
                    "issue": {"number": 42},
                    "comment": {
                        "body": "來自 GitHub 的留言",
                        "html_url": "https://github.com/Limatura/tessera/issues/42#issuecomment-1",
                        "user": {"login": "sao-coding", "avatar_url": "https://example.invalid/a.png"},
                    },
                }
            ).encode()
            async with session.post(
                f"{base}/github/webhook",
                data=comment,
                headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": signed(comment)},
            ) as response:
                results.append(check("留言被接受", response.status, 200))
            results.append(check("送進正確的討論串", [cid for cid, _ in SENT], [555000111]))

            # The loop guard. Keyed on the marker the bot writes, not on the
            # author — the regression this pins is a human comment being
            # swallowed because the bot posts under that same human's token.
            echoed = json.dumps(
                {
                    "action": "created",
                    "repository": {"full_name": "Limatura/tessera"},
                    "issue": {"number": 42},
                    "comment": {
                        "body": f"這句是從 Discord 來的\n\n{store.SYNC_MARKER}",
                        "html_url": "https://github.com/Limatura/tessera/issues/42#issuecomment-2",
                        "user": {"login": "timo9378"},
                    },
                }
            ).encode()
            async with session.post(
                f"{base}/github/webhook",
                data=echoed,
                headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": signed(echoed)},
            ) as response:
                results.append(check("bot 自己同步過去的留言不會被貼回來", response.status, 200))
            results.append(check("討論串沒有第二則訊息", len(SENT), 1))

            # The bug this whole round is about: the bot's GitHub account and
            # the person's are the same one, and the person's own comment must
            # still come through.
            human = json.dumps(
                {
                    "action": "created",
                    "repository": {"full_name": "Limatura/tessera"},
                    "issue": {"number": 42},
                    "comment": {
                        "body": "我在 GitHub 上手打的",
                        "html_url": "https://github.com/Limatura/tessera/issues/42#issuecomment-3",
                        "user": {"login": "timo9378"},
                    },
                }
            ).encode()
            async with session.post(
                f"{base}/github/webhook",
                data=human,
                headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": signed(human)},
            ) as response:
                results.append(check("同一個帳號手打的留言仍然會同步", response.status, 200))
            results.append(check("討論串收到它", len(SENT), 2))

            print("\nissues closed / reopened")
            for action, expected_archived, label in (
                ("closed", True, "關閉"),
                ("reopened", False, "重新開啟"),
            ):
                body = json.dumps(
                    {
                        "action": action,
                        "repository": {"full_name": "Limatura/tessera"},
                        "issue": {"number": 42},
                    }
                ).encode()
                before = len(SENT)
                async with session.post(
                    f"{base}/github/webhook",
                    data=body,
                    headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": signed(body)},
                ) as response:
                    results.append(check(f"{label}事件被接受", response.status, 200))
                results.append(check(f"{label}有通知討論串", len(SENT) - before, 1))
                results.append(check(f"{label}後 archived", ARCHIVED[-1], expected_archived))
                results.append(check(f"{label}有改公告 embed", EDITED[-1]["status"],
                                     "✅ 已完成" if action == "closed" else "🟢 開發中"))
                # Removing the components on reopen would strip the buttons off
                # a task that just came back to life.
                results.append(check(f"{label}時 view 有沒有被動到",
                                     "view" in EDITED[-1]["changes"], action == "closed"))
    finally:
        await runner.cleanup()

    print(f"\n{sum(results)}/{len(results)} 通過")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
