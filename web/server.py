"""The HTTP side of the bot.

Two things need a public URL: GitHub's OAuth callback, and GitHub's webhook
deliveries. Both used to live in a separate FastAPI service, which is why the
bot could not be deployed on its own. They run in this process now.

No new dependency: `aiohttp` is already what discord.py speaks HTTP with.
"""

import hashlib
import hmac
import logging
import os
import time

import aiohttp
from aiohttp import web

import store

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

#: How long to wait on GitHub. A webhook delivery times out at 10 seconds on
#: their side, so there is no point holding one open for longer than that.
TIMEOUT = aiohttp.ClientTimeout(total=8)


def _bot_github_login(app: web.Application) -> str | None:
    return app.get("bot_github_login")


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# ──────────────────────────────────────────────────────────────────────
# OAuth
# ──────────────────────────────────────────────────────────────────────


async def github_callback(request: web.Request) -> web.Response:
    """Completes the OAuth dance `/github-login` starts.

    The bot generated a random `state` and remembers which Discord user it
    belongs to. GitHub sends it back here alongside a `code`; this exchanges the
    code for a token and files it under that same `state`, which is the key the
    bot looks for on its next read.

    Keeping that contract rather than tidying it is deliberate — the cog's
    existing lookup works, and changing both halves at once would mean nothing
    left to compare against if the move went wrong.
    """
    code = request.query.get("code")
    state = request.query.get("state")
    if not code or not state:
        return web.Response(status=400, text="missing code or state")

    client_id = os.getenv("GITHUB_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GITHUB_OAUTH_CLIENT_SECRET")
    redirect_uri = os.getenv("GITHUB_OAUTH_CALLBACK_URL")
    if not (client_id and client_secret and redirect_uri):
        logger.error("GitHub OAuth is not configured; cannot complete a login")
        return web.Response(status=500, text="this instance has no GitHub OAuth configured")

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        ) as response:
            payload = await response.json()

        token = payload.get("access_token")
        if not token:
            # GitHub answers 200 with an `error` field rather than a status, so
            # this is the only place the failure shows up.
            logger.error("GitHub refused the code exchange: %s", payload.get("error", payload))
            return web.Response(status=502, text="GitHub would not exchange that code")

        async with session.get(
            f"{GITHUB_API}/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        ) as response:
            username = (await response.json()).get("login")

    users = store.read_users()
    users[state] = {
        "access_token": token,
        "github_username": username,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    store.write_users(users)
    logger.info("stored a GitHub token for %s", username)

    return web.Response(
        content_type="text/html",
        text=(
            "<!doctype html><meta charset=utf-8>"
            "<title>已授權</title>"
            "<body style='font:16px/1.6 system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
            f"<h1>已授權</h1><p>GitHub 帳號 <b>{username}</b> 已經和你的 Discord 綁定。</p>"
            "<p>回 Discord 就可以用 <code>/start-dev</code> 了,這一頁可以關掉。</p>"
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────────────────────────────


def _signature_matches(secret: str, body: bytes, header: str | None) -> bool:
    """Whether the delivery really came from GitHub.

    SHA-256 only. GitHub still sends the SHA-1 `X-Hub-Signature` for
    compatibility, but accepting it would mean an attacker gets to pick the
    weaker of the two.
    """
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def _post_to_thread(request: web.Request, thread_id: str, **kwargs) -> None:
    """Sends into a Discord thread, using the bot already connected."""
    bot = request.app["bot"]
    channel = bot.get_channel(int(thread_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(thread_id))
        except Exception as error:  # noqa: BLE001 - deleted thread, lost access, anything
            logger.warning("cannot reach Discord thread %s: %s", thread_id, error)
            return
    await channel.send(**kwargs)


async def github_webhook(request: web.Request) -> web.Response:
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret:
        logger.error("GITHUB_WEBHOOK_SECRET is unset; refusing to trust a delivery")
        return web.Response(status=500, text="webhook secret not configured")

    body = await request.read()
    if not _signature_matches(secret, body, request.headers.get("X-Hub-Signature-256")):
        return web.Response(status=401, text="bad signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return web.json_response({"message": "pong"})

    payload = await request.json()
    repo = payload.get("repository", {}).get("full_name", "")

    if event == "issue_comment" and payload.get("action") == "created":
        await _handle_comment(request, repo, payload)
    elif event == "issues" and payload.get("action") == "closed":
        await _handle_issue_closed(request, repo, payload)

    return web.json_response({"message": "accepted"})


async def _handle_comment(request: web.Request, repo: str, payload: dict) -> None:
    comment = payload["comment"]
    author = comment["user"]["login"]

    # The bot posts Discord messages *to* GitHub. Without this, GitHub tells us
    # about that comment and we post it straight back into the thread it came
    # from — every message duplicated, and the duplicate attributed to GitHub.
    if author == _bot_github_login(request.app):
        logger.debug("skipping our own comment on %s#%s", repo, payload["issue"]["number"])
        return

    issue_number = payload["issue"]["number"]
    thread_id = store.thread_for_issue(repo, issue_number)
    if not thread_id:
        logger.info("no Discord thread mirrors %s#%s", repo, issue_number)
        return

    import discord

    embed = discord.Embed(
        description=comment["body"][:4000],
        url=comment["html_url"],
        colour=0x2DA44E,
    )
    embed.set_author(
        name=f"{author} 在 GitHub 留言",
        url=comment["html_url"],
        icon_url=comment["user"].get("avatar_url"),
    )
    embed.set_footer(text=f"{repo}#{issue_number}")
    await _post_to_thread(request, thread_id, embed=embed)


async def _handle_issue_closed(request: web.Request, repo: str, payload: dict) -> None:
    issue_number = payload["issue"]["number"]
    thread_id = store.thread_for_issue(repo, issue_number)
    if not thread_id:
        return

    await _post_to_thread(
        request,
        thread_id,
        content=f"🔒 `{repo}#{issue_number}` 已在 GitHub 關閉,這個討論串封存了。",
    )
    bot = request.app["bot"]
    channel = bot.get_channel(int(thread_id))
    if channel is not None:
        try:
            await channel.edit(archived=True)
        except Exception as error:  # noqa: BLE001
            logger.warning("could not archive thread %s: %s", thread_id, error)


# ──────────────────────────────────────────────────────────────────────


async def start(bot, host: str, port: int) -> web.AppRunner:
    """Runs the HTTP side on the bot's own event loop."""
    app = web.Application()
    app["bot"] = bot

    # Asked once, so that every webhook delivery does not cost a round trip to
    # find out whether the comment is one of ours.
    token = os.getenv("GITHUB_BOT_TOKEN")
    if token:
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.get(
                    f"{GITHUB_API}/user",
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    app["bot_github_login"] = (await response.json()).get("login")
            logger.info("posting to GitHub as %s", app["bot_github_login"])
        except Exception as error:  # noqa: BLE001
            logger.warning("could not identify the GitHub bot account: %s", error)

    app.add_routes(
        [
            web.get("/health", health),
            web.get("/github/callback", github_callback),
            web.post("/github/webhook", github_webhook),
        ]
    )

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("HTTP listening on %s:%s", host, port)
    return runner
