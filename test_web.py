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
# Cross-references only become Discord links when there is a guild to link into.
os.environ["DISCORD_GUILD_ID"] = "1000000000"
os.environ["CI_CHANNEL_ID"] = "1537700084681154660"

import aiohttp

import mermaid
import store
import tables
from web import server

import discord

SENT: list[tuple[int, dict]] = []
ARCHIVED: list[bool] = []
EDITED: list[dict] = []
RENAMED: list[str] = []


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
        title = next((f.value for f in embed.fields if f.name == "📝 標題"), None)
        EDITED.append({
            "status": status,
            "title": title,
            "description": embed.description,
            "changes": set(changes),
        })
        # The card the webhook reads back is the one it just wrote.
        self.embeds = [embed]


class StubParent:
    def __init__(self, message):
        self._message = message

    async def fetch_message(self, _message_id):
        return self._message


class StubThread:
    def __init__(self, channel_id, announcement):
        self.id = channel_id
        self.parent = StubParent(announcement)
        self.name = "#42 原本的標題"

    async def send(self, **kwargs):
        SENT.append((self.id, kwargs))
        return StubPosted(self.id)

    async def edit(self, **kwargs):
        if "archived" in kwargs:
            ARCHIVED.append(kwargs["archived"])
        if "name" in kwargs:
            self.name = kwargs["name"]
            RENAMED.append(kwargs["name"])


class StubPosted:
    """What `send` hands back, so callers that record the message can."""

    _next = iter(range(9_000_001, 9_000_999))

    def __init__(self, channel_id):
        self.id = next(StubPosted._next)
        self.channel_id = channel_id


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
        self.threads = {}

    def get_channel(self, channel_id):
        # One instance per id: a rename has to still be there on the next look,
        # or the "already says that, do nothing" guards cannot be tested.
        return self.threads.setdefault(channel_id, StubThread(channel_id, self.announcement))

    async def fetch_channel(self, channel_id):
        return self.get_channel(channel_id)

    def get_cog(self, _name):
        return StubCog()


def signed(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


async def _answer(value):
    """A ready-made result, for standing in where a coroutine is expected."""
    return value


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

    print("\nmermaid.extract —— 把圖表從留言裡挑出來")
    _, found = mermaid.extract("前言\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\n後記")
    results.append(check("抓到 fence 裡的內容", found, ["flowchart LR\n  A --> B"]))
    results.append(check("一則留言裡的兩張都抓到",
                         mermaid.extract("```mermaid\nA\n```\n中間\n```mermaid\nB\n```")[1], ["A", "B"]))
    results.append(check("別的語言的 fence 不碰", mermaid.extract("```python\nx = 1\n```")[1], []))
    # An unterminated fence would otherwise swallow the rest of the comment.
    results.append(check("沒有收尾的 fence 留在原地", mermaid.extract("```mermaid\nA\n忘了收尾")[1], []))
    results.append(check("波浪號也是 fence", mermaid.extract("~~~mermaid\nA\n~~~")[1], ["A"]))
    results.append(check("語言名稱不分大小寫", mermaid.extract("```Mermaid\nA\n```")[1], ["A"]))

    print("\nmermaid.diagrams —— 渲染不成就退回原始碼")
    os.environ.pop("MERMAID_URL", None)
    text, files = await mermaid.diagrams("看圖\n\n```mermaid\nflowchart LR\n  A --> B\n```")
    results.append(check("沒設定 MERMAID_URL 時沒有附件", files, []))
    results.append(check("圖表原封不動留著", "```mermaid\nflowchart LR\n  A --> B\n```" in text, True))
    # Nothing listens on port 1. The point is that an unreachable renderer
    # degrades to the old behaviour instead of losing the diagram.
    os.environ["MERMAID_URL"] = "http://127.0.0.1:1"
    text, files = await mermaid.diagrams("```mermaid\nflowchart LR\n  A --> B\n```")
    results.append(check("渲染器連不上時沒有附件", files, []))
    results.append(check("渲染器連不上時退回原始碼", text.strip().startswith("```mermaid"), True))
    os.environ.pop("MERMAID_URL", None)

    print("\ntables.convert —— Discord 沒有表格,所以換一種形狀")
    narrow = tables.convert(
        "| 旗標 | 預設 | 說明 |\n| --- | --- | --- |\n| --length | 16 | 產生的長度 |"
    )
    results.append(check("窄的表格排成等寬", narrow.startswith("```"), True))
    # The property that matters: a CJK cell and an ASCII cell in the same column
    # must push the next column to the same place. Comparing whole-line widths
    # would not catch it — trailing padding is stripped.
    mixed = tables.convert(
        "| 名稱 | 說明 |\n| --- | --- |\n| 中文很寬 | 甲 |\n| ascii | 乙 |"
    ).split("\n")
    starts = {tables.width(line[: line.index("甲") if "甲" in line else line.index("乙")])
              for line in mixed if "甲" in line or "乙" in line}
    results.append(check("中文和英數混排時第二欄對齊", len(starts), 1))
    wide = tables.convert(
        "| 情況 | 結果 |\n| --- | --- |\n"
        "| 少了 --username / 旗標沒給值 / 不認得的旗標 | 印出各自的訊息然後退出 |"
    )
    results.append(check("寬的表格攤成條列", wide.startswith("**"), True))
    results.append(check("條列不會有對不齊的網格", "```" in wide, False))
    # Every line has to fit, not just the average one: one wrapped line in a
    # code block destroys the alignment of all of them.
    fits = all(tables.width(line) <= tables._limit() for line in narrow.split("\n"))
    results.append(check("留下來的等寬表每一行都在寬度內", fits, True))

    # A shell pipeline is not a table, and a table drawn inside a code block was
    # put there on purpose.
    fenced = "```sh\ncat a | grep b\n| 這 | 不是 |\n| --- | --- |\n| 表 | 格 |\n```"
    results.append(check("code block 裡的管線符號不碰", tables.convert(fenced), fenced))
    results.append(check("只是含有管線符號的句子不碰",
                         tables.convert("a | b 這只是一句話"), "a | b 這只是一句話"))
    # Without the delimiter row there is nothing separating a table from prose.
    results.append(check("沒有分隔列就不是表格",
                         tables.convert("| 一 | 二 |\n| 三 | 四 |"), "| 一 | 二 |\n| 三 | 四 |"))

    print("\n程式碼區塊裡的東西不該被改寫")
    linked = server._link_github_syntax(
        "看 #42\n\n```\nissue #42 deadbeef\n```\n\n行內 `#42 deadbeef`", "Limatura/tessera"
    )
    results.append(check("區塊外的 #42 有連結", "discord.com/channels" in linked, True))
    results.append(check("圍籬內的 #42 原封不動", "```\nissue #42 deadbeef\n```" in linked, True))
    results.append(check("行內程式碼也原封不動", "`#42 deadbeef`" in linked, True))

    print("\n留言連結 —— 回覆的兩個方向靠它接起來")
    store.remember_comment(777000, "Limatura/tessera", 42, 555)
    results.append(check(
        "permalink 找得回 Discord 訊息",
        server._replying_to("Limatura/tessera", "回 https://github.com/Limatura/tessera/issues/42#issuecomment-555"),
        777000,
    ))
    results.append(check(
        "別的 repo 的 permalink 不認",
        server._replying_to("Limatura/tessera", "https://github.com/other/repo/issues/42#issuecomment-555"),
        None,
    ))
    results.append(check("沒有連結就不是回覆",
                         server._replying_to("Limatura/tessera", "一般留言"), None))
    # The regression: `_ISSUE_URL` used to match the issue part of a permalink
    # and leave `#issuecomment-555` stranded after the rewritten link.
    rewritten = server._point_at_threads(
        "看 https://github.com/Limatura/tessera/issues/42#issuecomment-555", "Limatura/tessera"
    )
    results.append(check("permalink 指到那一則訊息", "/555000111/777000" in rewritten, True))
    results.append(check("permalink 沒有被切成兩半", "#issuecomment-" in rewritten, False))

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
                        "id": 8001,
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
                        "id": 8002,
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
                        "id": 8003,
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

            # GitHub has no reply of its own, so a permalink is what carries one.
            # 777000 was recorded above as the Discord copy of comment 555.
            answering = json.dumps(
                {
                    "action": "created",
                    "repository": {"full_name": "Limatura/tessera"},
                    "issue": {"number": 42},
                    "comment": {
                        "id": 8004,
                        "body": "回 https://github.com/Limatura/tessera/issues/42#issuecomment-555 這則",
                        "html_url": "https://github.com/Limatura/tessera/issues/42#issuecomment-4",
                        "user": {"login": "octocat"},
                    },
                }
            ).encode()
            async with session.post(
                f"{base}/github/webhook",
                data=answering,
                headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": signed(answering)},
            ) as response:
                results.append(check("帶留言連結的留言被接受", response.status, 200))
            reference = SENT[-1][1].get("reference")
            results.append(check("送出時帶著回覆對象",
                                 getattr(reference, "message_id", None), 777000))
            # A reply whose parent has been deleted must still be posted.
            results.append(check("找不到對象時不會整則丟掉",
                                 getattr(reference, "fail_if_not_exists", None), False))

            print("\nissues edited —— 卡片和討論串名稱以前會永遠停在建立當下")

            async def deliver(event, body):
                raw = json.dumps(body).encode()
                async with session.post(
                    f"{base}/github/webhook",
                    data=raw,
                    headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signed(raw)},
                ) as response:
                    return response.status

            status = await deliver("issues", {
                "action": "edited",
                "repository": {"full_name": "Limatura/tessera"},
                "issue": {"number": 42, "title": "改過的標題", "body": "改過的內文"},
                "changes": {"title": {"from": "原本的標題"}},
            })
            results.append(check("標題編輯事件被接受", status, 200))
            results.append(check("討論串跟著改名", RENAMED[-1:], ["#42 改過的標題"]))
            results.append(check("卡片的標題欄也改了", EDITED[-1]["title"], "改過的標題"))

            before = len(RENAMED)
            await deliver("issues", {
                "action": "edited",
                "repository": {"full_name": "Limatura/tessera"},
                "issue": {"number": 42, "title": "改過的標題", "body": "改過的內文"},
                "changes": {"title": {"from": "原本的標題"}},
            })
            # The convergence guard: the echo finds the name already right.
            results.append(check("名稱已經對了就不再改一次", len(RENAMED), before))

            await deliver("issues", {
                "action": "edited",
                "repository": {"full_name": "Limatura/tessera"},
                "issue": {"number": 42, "title": "改過的標題", "body": "全新的內文"},
                "changes": {"body": {"from": "舊的"}},
            })
            results.append(check("內文編輯會重寫卡片", EDITED[-1]["description"], "全新的內文"))

            print("\npull_request —— 標籤和負責人以前根本沒接上")
            before = len(SENT)
            results.append(check("PR 被指派的事件被接受", await deliver("pull_request", {
                "action": "assigned",
                "repository": {"full_name": "Limatura/tessera"},
                "pull_request": {"number": 42, "assignees": [{"login": "octocat"}]},
                "assignee": {"login": "octocat"},
            }), 200))
            results.append(check("PR 的指派有通知討論串", len(SENT) - before, 1))

            before = len(SENT)
            await deliver("pull_request", {
                "action": "review_requested",
                "repository": {"full_name": "Limatura/tessera"},
                "pull_request": {"number": 42},
                "requested_reviewer": {"login": "octocat"},
            })
            results.append(check("被請求 review 會說出來",
                                 "被請求 review" in SENT[-1][1].get("content", ""), True))

            before = len(SENT)
            await deliver("pull_request", {
                "action": "ready_for_review",
                "repository": {"full_name": "Limatura/tessera"},
                "pull_request": {"number": 42},
            })
            results.append(check("草稿轉正式會說出來", len(SENT) - before, 1))

            print("\npull_request_review —— PR 的 review 以前在 Discord 完全看不到")
            await deliver("pull_request_review", {
                "action": "submitted",
                "repository": {"full_name": "Limatura/tessera"},
                "pull_request": {"number": 42, "user": {"login": "timo9378"}},
                "review": {"state": "approved", "body": "", "html_url": "https://x.invalid/r1",
                           "user": {"login": "octocat"}},
            })
            results.append(check("approve 有報出來", "通過了這個 PR" in SENT[-1][1]["content"], True))

            before = len(SENT)
            await deliver("pull_request_review", {
                "action": "submitted",
                "repository": {"full_name": "Limatura/tessera"},
                "pull_request": {"number": 42, "user": {"login": "timo9378"}},
                "review": {"state": "commented", "body": "", "html_url": "https://x.invalid/r2",
                           "user": {"login": "octocat"}},
            })
            # An empty `commented` review is just the envelope the inline
            # comments arrived in; relaying it would double every review.
            results.append(check("沒有內容的 comment review 不轉發", len(SENT) - before, 0))

            print("\npull_request_review_comment —— GitHub 唯一有真 threading 的地方")
            await deliver("pull_request_review_comment", {
                "action": "created",
                "repository": {"full_name": "Limatura/tessera"},
                "pull_request": {"number": 42},
                "comment": {"id": 9100, "body": "這一行會爆", "path": "auth.py", "line": 12,
                            "html_url": "https://x.invalid/c1", "user": {"login": "octocat"}},
            })
            first = SENT[-1][1]["embed"]
            results.append(check("帶上檔名和行號", first.footer.text.endswith("auth.py:12"), True))
            parent_message = store.message_for_comment("Limatura/tessera", 9100, kind="review")
            results.append(check("記下來了,而且分得出是 review 留言", parent_message is not None, True))
            # The id space is separate from issue comments'; asking for the
            # wrong kind must not find it.
            results.append(check("不會被當成一般 issue 留言",
                                 store.message_for_comment("Limatura/tessera", 9100), None))

            await deliver("pull_request_review_comment", {
                "action": "created",
                "repository": {"full_name": "Limatura/tessera"},
                "pull_request": {"number": 42},
                "comment": {"id": 9101, "body": "不會,那邊有擋", "path": "auth.py", "line": 12,
                            "in_reply_to_id": 9100,
                            "html_url": "https://x.invalid/c2", "user": {"login": "timo9378"}},
            })
            results.append(check("in_reply_to_id 變成真正的 Discord 回覆",
                                 getattr(SENT[-1][1].get("reference"), "message_id", None),
                                 parent_message))

            print("\nworkflow_run —— 只報失敗")
            before = len(SENT)
            await deliver("workflow_run", {
                "action": "completed",
                "repository": {"full_name": "Limatura/tessera"},
                "workflow_run": {"name": "CI", "conclusion": "success",
                                 "html_url": "https://x.invalid/run1", "pull_requests": []},
            })
            results.append(check("成功的執行不吵人", len(SENT) - before, 0))

            before = len(SENT)
            await deliver("workflow_run", {
                "action": "completed",
                "repository": {"full_name": "Limatura/tessera"},
                "workflow_run": {"name": "CI", "conclusion": "cancelled",
                                 "html_url": "https://x.invalid/run2", "pull_requests": []},
            })
            results.append(check("取消的執行也不吵人", len(SENT) - before, 0))

            # The lookup needs GitHub; the handler's own logic is what is under
            # test, so the pull request is handed to it directly.
            original = server._pull_for_run
            server._pull_for_run = lambda repo, run: _answer(
                {"number": 42, "user": {"login": "timo9378"}}
            )
            try:
                await deliver("workflow_run", {
                    "action": "completed",
                    "repository": {"full_name": "Limatura/tessera"},
                    "workflow_run": {"name": "build", "conclusion": "failure",
                                     "html_url": "https://x.invalid/run3",
                                     "pull_requests": [{"number": 42}]},
                })
            finally:
                server._pull_for_run = original
            channel, kwargs = SENT[-1]
            content = kwargs.get("content", "")
            results.append(check("送進 CI 頻道而不是討論串", channel, 1537700084681154660))
            results.append(check("說是哪個 workflow", "**build** 失敗" in content, True))
            results.append(check("點名發 PR 的人", "timo9378" in content, True))
            results.append(check("附上執行的連結", "run3" in content, True))
            results.append(check("連回 PR 的討論串", "/555000111" in content, True))

            # No pull request behind it — a push straight to a branch. Still
            # reported, just with nobody in particular to point at.
            before = len(SENT)
            server._pull_for_run = lambda repo, run: _answer(None)
            try:
                await deliver("workflow_run", {
                    "action": "completed",
                    "repository": {"full_name": "Limatura/tessera"},
                    "workflow_run": {"name": "build", "conclusion": "failure",
                                     "head_branch": "main",
                                     "html_url": "https://x.invalid/run4", "pull_requests": []},
                })
            finally:
                server._pull_for_run = original
            results.append(check("沒有 PR 的失敗也會報", len(SENT) - before, 1))
            results.append(check("改成指出是哪個分支", "`main`" in SENT[-1][1]["content"], True))

            # A handler that throws must not answer 500: GitHub redelivers on
            # one, and these handlers post to Discord before they finish, so a
            # retry says everything twice instead of repairing anything.
            before = len(SENT)
            results.append(check("壞掉的 payload 仍然回 200", await deliver("issues", {
                "action": "edited",
                "repository": {"full_name": "Limatura/tessera"},
            }), 200))
            results.append(check("而且沒有貼出半截東西", len(SENT) - before, 0))

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
