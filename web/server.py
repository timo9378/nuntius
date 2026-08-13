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
import re
import time

import aiohttp
from aiohttp import web

import store

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

#: How long to wait on GitHub. A webhook delivery times out at 10 seconds on
#: their side, so there is no point holding one open for longer than that.
TIMEOUT = aiohttp.ClientTimeout(total=8)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# ──────────────────────────────────────────────────────────────────────
# OAuth
# ──────────────────────────────────────────────────────────────────────


async def github_callback(request: web.Request) -> web.Response:
    """Completes the OAuth dance `/login` starts.

    The bot generated a random `state` and remembers which Discord user it
    belongs to. GitHub sends it back here alongside a `code`, which this
    exchanges for a token and files against that person — see the comment at
    the write for why it resolves the state here rather than leaving it.
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

    record = {
        "access_token": token,
        "github_username": username,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Filed under the Discord id straight away.
    #
    # The version this was ported from wrote it under the OAuth `state` and left
    # the bot to translate that to a person on its next read. That only worked
    # if the bot still remembered which login the state belonged to — and it
    # remembers in memory, so any restart in the window between clicking the
    # link and the next message lost the login. Worse, it did not lose it
    # quietly: the loader treats a key it cannot explain as junk and deletes it.
    #
    # The callback now runs in the same process as the bot, so it can just ask.
    cog = request.app["bot"].get_cog("DevFlow")
    discord_id = cog.pending_oauth_states.pop(state, None) if cog else None

    users = store.read_users()
    users[discord_id or state] = record
    store.write_users(users)

    if cog is not None and discord_id:
        cog.user_mappings[discord_id] = record
    logger.info(
        "stored a GitHub token for %s (%s)",
        username,
        f"discord {discord_id}" if discord_id else "unclaimed — the bot restarted mid-login",
    )

    return web.Response(
        content_type="text/html",
        text=(
            "<!doctype html><meta charset=utf-8>"
            "<title>已授權</title>"
            "<body style='font:16px/1.6 system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
            f"<h1>已授權</h1><p>GitHub 帳號 <b>{username}</b> 已經和你的 Discord 綁定。</p>"
            "<p>回 Discord 就可以用 <code>/issue</code> 了,這一頁可以關掉。</p>"
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────────────────────────────


#: GitHub's own editor emits raw HTML when you paste a picture into a comment,
#: rather than markdown. Discord renders neither, so the tag has to be taken
#: apart here or the reader sees `<img width="467" … />` as literal text.
_HTML_IMAGE = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")

#: A GitHub @mention. Handles are letters, digits and single hyphens.
_MENTION = re.compile(r"(?<![\w/])@([A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38})\b")

#: A bare commit SHA. Seven characters is GitHub's own shortest abbreviation.
#:
#: At least one `a`–`f` is required, which rules out plain numbers — a run of
#: seven digits is far more likely to be a quantity, an id, or a year range than
#: a commit. `@` is excluded from the left so that a handle like `@deadbeef` is
#: left for the mention pass.
_COMMIT = re.compile(r"(?<![\w/@])(?=[0-9a-f]{7,40}(?![\w/]))([0-9]*[a-f][0-9a-f]*)", re.IGNORECASE)


#: GitHub label -> forum tag, where the two vocabularies differ.
#:
#: Only for the pairs that mean the same thing under different words. Anything
#: not here still matches by name, which is the common case.
TAG_ALIASES = {
    "enhancement": "Feature",
    "feature": "Feature",
    "bug": "Bug",
    "question": "Question",
    "documentation": "Question",
    "github_actions": "Backend",
}


#: Anything that is already a link: a markdown target, or a bare URL.
#:
#: These have to be held out of the substitutions below. A GitHub attachment
#: URL ends in a UUID, and a UUID's last group is twelve hex characters — which
#: is indistinguishable from an abbreviated commit SHA. Left unprotected, the
#: commit pass rewrites the middle of the image URL and the picture 404s.
_ALREADY_LINKED = re.compile(r"\]\([^)]*\)|<https?://[^>]*>|https?://\S+")


def _link_github_syntax(body: str, repo: str) -> str:
    """Turns GitHub's own shorthand into something Discord can act on.

    GitHub renders `@somebody` as a link and a bare SHA as a commit link. Discord
    renders neither, so a comment arrives as a wall of flat text — the very
    things worth clicking are the ones that stop working.

    Mentions become real Discord pings where the person has run
    `/login`; the rest become links to their GitHub profile, which is
    still better than a word.
    """
    by_handle = {
        (record.get("github_username") or "").lower(): discord_id
        for discord_id, record in store.read_users().items()
        if discord_id.isdigit() and record.get("github_username")
    }

    def mention(match: re.Match) -> str:
        handle = match.group(1)
        discord_id = by_handle.get(handle.lower())
        return f"<@{discord_id}>" if discord_id else f"[@{handle}](https://github.com/{handle})"

    def commit(match: re.Match) -> str:
        sha = match.group(1)
        return f"[`{sha[:7]}`](https://github.com/{repo}/commit/{sha})"

    # Existing links are lifted out and put back afterwards, so nothing gets
    # rewritten inside a URL.
    kept: list[str] = []

    def stash(match: re.Match) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    masked = _ALREADY_LINKED.sub(stash, body)

    # Commits first. A Discord mention is `<@` followed by a long run of digits,
    # so substituting mentions first hands the commit pass a snowflake id that
    # looks exactly like a SHA — and it duly turns the middle of the ping into a
    # commit link, leaving `<@[`4349523`](…)>`.
    masked = _MENTION.sub(mention, _COMMIT.sub(commit, masked))

    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], masked)


def _readable_in_discord(body: str) -> tuple[str, str | None]:
    """Rewrites a GitHub comment for a Discord embed.

    Returns the text and, if there was one, the first image URL — which the
    caller hangs off the embed so the picture itself shows rather than only its
    address.

    A private repository's attachments need a GitHub session to fetch, so
    Discord cannot load them and simply omits the image. The link is left in the
    text for exactly that case.
    """
    found: list[str] = []

    def take_html(match: re.Match) -> str:
        found.append(match.group(1))
        return f"[🖼️ 圖片]({match.group(1)})"

    def take_markdown(match: re.Match) -> str:
        found.append(match.group(2))
        return f"[🖼️ {match.group(1) or '圖片'}]({match.group(2)})"

    body = _HTML_IMAGE.sub(take_html, body)
    body = _MARKDOWN_IMAGE.sub(take_markdown, body)
    return body, (found[0] if found else None)


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


async def _post_to_thread(request: web.Request, thread_id: str, **kwargs):
    """Sends into a Discord thread, using the bot already connected.

    Returns the message, so callers that need to remember it can.
    """
    bot = request.app["bot"]
    channel = bot.get_channel(int(thread_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(thread_id))
        except Exception as error:  # noqa: BLE001 - deleted thread, lost access, anything
            logger.warning("cannot reach Discord thread %s: %s", thread_id, error)
            return None
    return await channel.send(**kwargs)


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

    action = payload.get("action")
    if event == "issue_comment" and action == "created":
        # GitHub reports a pull request's conversation comments as
        # `issue_comment` too, so this covers both without extra work.
        await _handle_comment(request, repo, payload)
    elif event == "issues" and action == "opened":
        await _handle_issue_opened(request, repo, payload)
    elif event == "issues" and action in ("closed", "reopened"):
        await _handle_issue_state(request, repo, payload, closed=action == "closed")
    elif event == "issues" and action in ("labeled", "unlabeled"):
        await _handle_labels(request, repo, payload)
    elif event == "issue_comment" and action == "deleted":
        await _handle_comment_deleted(request, repo, payload)
    elif event == "pull_request":
        await _handle_pull_request(request, repo, payload, action)

    return web.json_response({"message": "accepted"})


def tags_for_labels(channel, labels: list[str]) -> list:
    """The forum tags that correspond to a set of GitHub labels.

    Shared by the code that creates a post and the code that keeps its tags in
    step afterwards, so the two cannot disagree about what a label means.
    """
    available = {tag.name.lower(): tag for tag in channel.available_tags}
    tags = []
    for label in labels:
        for candidate in (label, TAG_ALIASES.get(label.lower(), "")):
            tag = available.get(candidate.lower())
            if tag is not None and tag not in tags:
                tags.append(tag)
                break
    return tags[:5]


async def _handle_labels(request: web.Request, repo: str, payload: dict) -> None:
    """Mirrors a label change onto the forum post's tags."""
    import discord

    issue = payload["issue"]
    thread_id = store.thread_for_issue(repo, issue["number"])
    if not thread_id:
        return

    bot = request.app["bot"]
    thread = bot.get_channel(int(thread_id))
    if thread is None or not isinstance(getattr(thread, "parent", None), discord.ForumChannel):
        return

    labels = [label["name"] for label in issue.get("labels", [])]
    wanted = tags_for_labels(thread.parent, labels)

    # Nothing to do when the tags already say what the labels say. This is the
    # loop guard for the whole two-way arrangement: a change made on either
    # side settles after one hop, because the echo finds the state already
    # correct and stops. Comparing the result beats remembering what we just
    # did — no bookkeeping to get out of step, and it also copes with a change
    # made while the bot was down.
    if {tag.id for tag in wanted} == {tag.id for tag in thread.applied_tags}:
        return

    if not wanted and thread.parent.flags.require_tag:
        logger.info("%s#%s has no matching tags but the forum demands one; leaving it alone",
                    repo, issue["number"])
        return

    try:
        await thread.edit(applied_tags=wanted)
        logger.info("tags for %s#%s -> %s", repo, issue["number"], [t.name for t in wanted])
    except Exception as error:  # noqa: BLE001
        logger.warning("could not set tags on thread %s: %s", thread_id, error)


async def _handle_comment_deleted(request: web.Request, repo: str, payload: dict) -> None:
    """Removes the Discord copy of a comment deleted on GitHub."""
    comment_id = payload["comment"]["id"]
    message_id = store.message_for_comment(repo, comment_id)
    if message_id is None:
        return

    thread_id = store.thread_for_issue(repo, payload["issue"]["number"])
    bot = request.app["bot"]
    thread = bot.get_channel(int(thread_id)) if thread_id else None
    if thread is not None:
        try:
            message = await thread.fetch_message(message_id)
            await message.delete()
            logger.info("deleted the Discord copy of %s comment %s", repo, comment_id)
        except Exception as error:  # noqa: BLE001
            logger.warning("could not delete message %s: %s", message_id, error)
    store.forget_message(message_id)


async def _handle_pull_request(request: web.Request, repo: str, payload: dict, action: str) -> None:
    """Mirrors a pull request the same way as an issue.

    A PR *is* an issue as far as numbering and comments go, so it reuses the
    same mapping and the same comment sync. Only the announcement differs, and
    only in wording — a merged PR and a closed one are not the same news.
    """
    pull = payload["pull_request"]
    number = pull["number"]

    if action == "opened":
        if store.thread_for_issue(repo, number):
            return
        bot = request.app["bot"]
        cog = bot.get_cog("DevFlow")
        channel = getattr(cog, "dev_announce_channel", None) if cog else None
        if channel is None:
            return
        await announce_issue(bot, channel, repo, {**pull, "kind": "pull"})
        return

    if action not in ("closed", "reopened"):
        return

    thread_id = store.thread_for_issue(repo, number)
    if not thread_id:
        return

    merged = bool(pull.get("merged"))
    closed = action == "closed"
    if closed:
        note = (
            f"🟣 `{repo}#{number}` 已合併,討論串封存了。"
            if merged
            else f"🔴 `{repo}#{number}` 被關閉但沒有合併,討論串封存了。"
        )
    else:
        note = f"🔓 `{repo}#{number}` 重新開啟,討論串解除封存。"

    await _post_to_thread(request, thread_id, content=note)

    bot = request.app["bot"]
    thread = bot.get_channel(int(thread_id))
    if thread is None:
        return
    await _update_announcement(bot, thread, closed=closed)
    try:
        await thread.edit(archived=closed)
    except Exception as error:  # noqa: BLE001
        logger.warning("could not set archived=%s on thread %s: %s", closed, thread_id, error)


async def _handle_issue_opened(request: web.Request, repo: str, payload: dict) -> None:
    """Gives an issue opened on GitHub the same Discord presence as one opened
    from `/issue`.

    Without this the sync is only half a loop: anything a collaborator files
    directly on GitHub is invisible in Discord, which is the gap the retired
    `discord-sync.yml` workflow used to cover.
    """
    issue = payload["issue"]
    number = issue["number"]

    if store.SYNC_MARKER in (issue.get("body") or ""):
        logger.debug("%s#%s came from /issue; it already has a thread", repo, number)
        return
    if store.thread_for_issue(repo, number):
        logger.debug("%s#%s already has a thread", repo, number)
        return

    bot = request.app["bot"]
    cog = bot.get_cog("DevFlow")
    channel = getattr(cog, "dev_announce_channel", None) if cog else None
    if channel is None:
        logger.warning("no announce channel configured; %s#%s gets no thread", repo, number)
        return

    thread = await announce_issue(bot, channel, repo, issue)
    if thread is not None:
        logger.info("announced %s#%s as thread %s", repo, number, thread.id)


async def announce_issue(bot, channel, repo: str, issue: dict, history: list[dict] | None = None):
    """Posts a task card for a GitHub issue and opens a thread under it.

    Shared with `/link`, so an issue pulled in by hand and one that
    arrived over the webhook look identical in the channel.

    The mapping is keyed by the thread id, which for a thread started from a
    message is that message's id — the same shape `/issue` writes.
    """
    import discord

    number = issue["number"]
    title = issue.get("title") or f"#{number}"
    body = issue.get("body") or ""
    author = issue.get("user", {}).get("login", "unknown")

    is_pull = issue.get("kind") == "pull"
    text, image = _readable_in_discord(body)
    text = _link_github_syntax(text, repo)
    embed = discord.Embed(
        title="🔀 GitHub 上開了一個 PR" if is_pull else "🐙 GitHub 上開了一張單",
        description=text[:1500] or "*（沒有內文）*",
        colour=0x8250DF if is_pull else 0x2DA44E,
    )
    embed.add_field(name="📝 標題", value=title[:1000], inline=False)
    embed.add_field(name="🎯 目標儲存庫", value=f"`{repo}`", inline=False)
    embed.add_field(name="📊 狀態", value="🟢 開發中", inline=False)
    embed.add_field(
        name="🔗 GitHub Issue",
        value=f"[#{number}]({issue['html_url']}) · 由 {author} 開立",
        inline=False,
    )
    labels = [label["name"] for label in issue.get("labels", [])]
    if labels:
        embed.add_field(name="🏷️ 標籤", value=", ".join(f"`{name}`" for name in labels), inline=False)
    if image:
        embed.set_image(url=image)

    thread_name = f"#{number} {title}"[:100]
    try:
        if isinstance(channel, discord.ForumChannel):
            # A forum is the right shape for a backlog: one post per issue,
            # searchable, sortable by activity, and filterable by tag. A text
            # channel can only be scrolled, which stops working at about twenty
            # open issues.
            #
            # Labels map onto forum tags by name where the names line up.
            # Discord allows five per post.
            tags = tags_for_labels(channel, labels)

            # A forum can be configured to demand a tag on every post, and an
            # issue with no labels then cannot be posted at all — which is how
            # a bulk import loses every unlabelled issue in one go, with only a
            # 400 in the log to show for it. Fall back to a named default, or
            # failing that the channel's first tag.
            if not tags and channel.flags.require_tag and channel.available_tags:
                named = os.getenv("DEFAULT_FORUM_TAG", "").lower()
                by_name = {tag.name.lower(): tag for tag in channel.available_tags}
                tags = [by_name.get(named) or channel.available_tags[0]]
            created = await channel.create_thread(name=thread_name, embed=embed, applied_tags=tags)
            thread = created.thread
        else:
            announcement = await channel.send(embed=embed)
            thread = await announcement.create_thread(name=thread_name, auto_archive_duration=10080)
    except Exception as error:  # noqa: BLE001
        logger.warning("could not announce %s#%s: %s", repo, number, error)
        return None

    mappings = store.read_threads()
    mappings[str(thread.id)] = {"issue_number": number, "repo": repo}
    store.write_threads(mappings)

    # The cog keeps its own copy in memory and only re-reads on a miss, so it
    # would not see this until the next restart.
    cog = bot.get_cog("DevFlow")
    if cog is not None:
        cog.thread_issue_mappings[str(thread.id)] = {"issue_number": number, "repo": repo}

    # An issue pulled in by hand usually already has a conversation on it. A
    # thread that starts empty makes you go and read GitHub anyway, which
    # defeats the point of linking it.
    for comment in history or []:
        text, image = _readable_in_discord(comment.get("body") or "")
        text = _link_github_syntax(text, repo)
        past = discord.Embed(description=text[:4000] or "*（空留言）*", colour=0x57606A)
        past.set_author(name=f"{comment.get('author', 'unknown')} · 既有留言")
        if image:
            past.set_image(url=image)
        try:
            await thread.send(embed=past)
        except Exception as error:  # noqa: BLE001
            logger.warning("could not replay a comment into thread %s: %s", thread.id, error)
            break

    return thread


async def _handle_comment(request: web.Request, repo: str, payload: dict) -> None:
    comment = payload["comment"]
    author = comment["user"]["login"]
    body = comment["body"] or ""

    # The bot posts Discord messages *to* GitHub, and GitHub then tells us about
    # the comment we just made. Without a guard, every message is duplicated and
    # the copy is attributed to GitHub.
    #
    # Matched on the marker the bot writes, not on the author. Comparing authors
    # is what the first version did, and it fails exactly where it matters: the
    # bot posts under a real person's token, so that person's own GitHub
    # comments look like echoes and never reach Discord.
    if store.SYNC_MARKER in body:
        logger.debug("skipping a comment this bot wrote on %s#%s", repo, payload["issue"]["number"])
        return

    issue_number = payload["issue"]["number"]
    thread_id = store.thread_for_issue(repo, issue_number)
    if not thread_id:
        logger.info("no Discord thread mirrors %s#%s", repo, issue_number)
        return

    import discord

    text, image = _readable_in_discord(body)
    text = _link_github_syntax(text, repo)
    embed = discord.Embed(
        description=text[:4000],
        url=comment["html_url"],
        colour=0x2DA44E,
    )
    if image:
        embed.set_image(url=image)
    embed.set_author(
        name=f"{author} 在 GitHub 留言",
        url=comment["html_url"],
        icon_url=comment["user"].get("avatar_url"),
    )
    embed.set_footer(text=f"{repo}#{issue_number}")
    posted = await _post_to_thread(request, thread_id, embed=embed)

    # So that reacting to this message in Discord lands on the GitHub comment it
    # came from. Only messages going the *other* way were recorded before, which
    # meant reactions worked on your own words and silently did nothing on
    # everybody else's — the half you are more likely to want to react to.
    if posted is not None:
        store.remember_comment(posted.id, repo, issue_number, comment["id"])


async def _handle_issue_state(
    request: web.Request, repo: str, payload: dict, *, closed: bool
) -> None:
    """Mirrors an issue being closed or reopened onto the Discord side.

    Two things move, not one. Archiving the thread is the visible half, but the
    announcement in the channel is what people actually scroll past — leaving it
    saying "開發中" under a closed issue is how a board stops being trusted.
    """
    issue_number = payload["issue"]["number"]
    thread_id = store.thread_for_issue(repo, issue_number)
    if not thread_id:
        return

    await _post_to_thread(
        request,
        thread_id,
        content=(
            f"🔒 `{repo}#{issue_number}` 已在 GitHub 關閉,這個討論串封存了。"
            if closed
            else f"🔓 `{repo}#{issue_number}` 在 GitHub 被重新開啟,討論串解除封存。"
        ),
    )

    bot = request.app["bot"]
    thread = bot.get_channel(int(thread_id))
    if thread is None:
        return

    await _update_announcement(bot, thread, closed=closed)

    try:
        # Last, because a thread cannot be posted into once it is archived —
        # doing this first would silently drop the message above.
        await thread.edit(archived=closed)
    except Exception as error:  # noqa: BLE001
        logger.warning("could not set archived=%s on thread %s: %s", closed, thread_id, error)


async def _update_announcement(bot, thread, *, closed: bool) -> None:
    """Rewrites the task announcement the thread hangs off.

    A thread started from a message shares that message's id, so the thread id
    is also the announcement's id — no second mapping needed.
    """
    parent = getattr(thread, "parent", None)
    if parent is None:
        return

    try:
        announcement = await parent.fetch_message(thread.id)
    except Exception as error:  # noqa: BLE001
        logger.warning("cannot read the announcement for thread %s: %s", thread.id, error)
        return

    if not announcement.embeds:
        return

    cog = bot.get_cog("DevFlow")
    embed = announcement.embeds[0]
    if closed and cog is not None:
        # The same routine `/close` uses, so a task closed from GitHub and
        # one closed from Discord end up looking identical — including the
        # elapsed time.
        embed = cog._update_embed_for_completion(embed)  # noqa: SLF001
    else:
        embed = embed.copy()
        index = next((i for i, f in enumerate(embed.fields) if f.name == "📊 狀態"), -1)
        value = "🟢 開發中" if not closed else "✅ 已完成"
        if index != -1:
            embed.set_field_at(index, name="📊 狀態", value=value, inline=False)

    # Buttons are greyed out on close — pressing "我想協作" on a finished task
    # opens a thread nobody is watching — and left alone on reopen.
    #
    # `view` is omitted rather than passed as None in the reopen case, because
    # discord.py reads an explicit None as "remove the components", which would
    # strip the buttons off a task that just came back to life.
    changes = {"embed": embed}
    if closed and cog is not None:
        disabled = cog._disable_buttons(announcement)  # noqa: SLF001
        if disabled is not None:
            changes["view"] = disabled

    try:
        await announcement.edit(**changes)
    except Exception as error:  # noqa: BLE001
        logger.warning("could not update the announcement for thread %s: %s", thread.id, error)


# ──────────────────────────────────────────────────────────────────────


async def start(bot, host: str, port: int) -> web.AppRunner:
    """Runs the HTTP side on the bot's own event loop."""
    app = web.Application()
    app["bot"] = bot

    # Only to say so in the log. Which account the bot posts under is the first
    # thing you want to know when comments show up attributed to the wrong
    # person, and it is not otherwise visible from outside.
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
