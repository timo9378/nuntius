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

SENT: list[tuple[int, dict]] = []


class StubChannel:
    def __init__(self, channel_id):
        self.id = channel_id

    async def send(self, **kwargs):
        SENT.append((self.id, kwargs))

    async def edit(self, **_kwargs):
        pass


class StubBot:
    def get_channel(self, channel_id):
        return StubChannel(channel_id)

    async def fetch_channel(self, channel_id):
        return StubChannel(channel_id)


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

            # The loop guard: our own comment must not be posted back.
            server_app_login = "nuntius-bot"
            runner.app["bot_github_login"] = server_app_login
            mine = comment.replace(b'"login": "sao-coding"', f'"login": "{server_app_login}"'.encode())
            async with session.post(
                f"{base}/github/webhook",
                data=mine,
                headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": signed(mine)},
            ) as response:
                results.append(check("自己發的留言不會被貼回來", response.status, 200))
            results.append(check("討論串沒有第二則訊息", len(SENT), 1))

            print("\nissues closed → 封存")
            closed = json.dumps(
                {
                    "action": "closed",
                    "repository": {"full_name": "Limatura/tessera"},
                    "issue": {"number": 42},
                }
            ).encode()
            async with session.post(
                f"{base}/github/webhook",
                data=closed,
                headers={"X-GitHub-Event": "issues", "X-Hub-Signature-256": signed(closed)},
            ) as response:
                results.append(check("關閉事件被接受", response.status, 200))
            results.append(check("討論串收到封存通知", len(SENT), 2))
    finally:
        await runner.cleanup()

    print(f"\n{sum(results)}/{len(results)} 通過")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
