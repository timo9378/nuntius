"""The HTTP side of the bot.

Two things need a public URL: GitHub's OAuth callback, and GitHub's webhook
deliveries. Both used to live in a separate FastAPI service, which is why the
bot could not be deployed on its own. They run in this process now.

No new dependency: `aiohttp` is already what discord.py speaks HTTP with.
"""

import hashlib
import hmac
import io
import logging
import os
import re
import time

import aiohttp
from aiohttp import web

import mermaid
import store
import tables
import vocabulary

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

#: Needed to build a link back into Discord. Without it, cross-references
#: stay as GitHub links — still useful, just one hop further away.
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")

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
#: Nearly empty on purpose. The human labels were renamed on GitHub to match the
#: forum's tags exactly (`enhancement` became `Feature`, and so on), so they
#: match by name and need no entry here.
#:
#: What is left is Dependabot's. Those three are created and reapplied by the
#: bot whichever way you rename them, so they cannot join the shared vocabulary
#: — and they describe dependency updates, which is backend work.
TAG_ALIASES = {
    "dependencies": "Backend",
    "github_actions": "Backend",
    "rust": "Backend",
}


#: Anything that is already a link: a markdown target, or a bare URL.
#:
#: These have to be held out of the substitutions below. A GitHub attachment
#: URL ends in a UUID, and a UUID's last group is twelve hex characters — which
#: is indistinguishable from an abbreviated commit SHA. Left unprotected, the
#: commit pass rewrites the middle of the image URL and the picture 404s.
#:
#: `\x00` is excluded from the bare-URL form because by the time it runs, the
#: masking below has already put markers of that shape into the text, and a
#: greedy `\S+` would swallow one and strand it in the output.
_ALREADY_LINKED = re.compile(r"\]\([^)]*\)|<https?://[^>]*>|https?://[^\s\x00]+")

#: Code, fenced or inline.
#:
#: Nothing inside it is ours to rewrite: Discord renders no markdown in a code
#: block, so a link put there shows as its own source. It also matters for what
#: the block *contains* — a mermaid diagram that failed to render keeps its
#: source, and diagram source is full of arrows and short hex-ish words that
#: the commit pass would happily turn into links, corrupting the one copy of
#: the diagram the reader still has.
_CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]+`", re.DOTALL)


#: A reference to an issue in the same repository, GitHub's `#12` shorthand.
#:
#: Digits only, so a CSS colour (`#fff`) and a heading (`# 標題`) are left
#: alone. Not preceded by a word character, so `abc#1` is not a reference.
_ISSUE_REF = re.compile(r"(?<![\w#])#(\d+)\b")

#: A full issue or pull request URL.
#:
#: Held off a comment permalink, which `_COMMENT_URL` below handles as a whole.
#: Without the lookahead this matches the issue part and leaves the fragment
#: stranded, so a pasted permalink became a thread link with a naked
#: `#issuecomment-2891…` trailing it.
_ISSUE_URL = re.compile(
    r"https?://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)\b(?!#issuecomment-)"
)

#: A permalink to one particular comment.
_COMMENT_URL = re.compile(
    r"https?://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)#issuecomment-(\d+)"
)


def _point_at_threads(body: str, repo: str) -> str:
    """Rewrites references to other issues so they open the Discord thread.

    Someone reading in Discord who taps `#12` should land in the conversation
    about #12, not be bounced to a browser. Where there is no thread for it the
    reference becomes an ordinary GitHub link, which is what it already meant.
    """
    def by_number(match: re.Match) -> str:
        number = int(match.group(1))
        thread = store.thread_for_issue(repo, number)
        if thread:
            return f"[#{number}](https://discord.com/channels/{GUILD_ID}/{thread})"
        return f"[#{number}](https://github.com/{repo}/issues/{number})"

    def by_url(match: re.Match) -> str:
        other, number = match.group(1), int(match.group(2))
        thread = store.thread_for_issue(other, number)
        if thread:
            return f"[{other}#{number}](https://discord.com/channels/{GUILD_ID}/{thread})"
        return match.group(0)

    def by_comment(match: re.Match) -> str:
        """A permalink lands on the message itself, not merely on the thread."""
        other, number, comment_id = match.group(1), int(match.group(2)), int(match.group(3))
        thread = store.thread_for_issue(other, number)
        message_id = store.message_for_comment(other, comment_id)
        if thread and message_id:
            return (
                f"[{other}#{number} 的留言]"
                f"(https://discord.com/channels/{GUILD_ID}/{thread}/{message_id})"
            )
        return match.group(0)

    if GUILD_ID:
        # Permalinks first: the issue pattern would otherwise eat their prefix.
        body = _COMMENT_URL.sub(by_comment, body)
        body = _ISSUE_URL.sub(by_url, body)
    return _ISSUE_REF.sub(by_number, body)


def _replying_to(repo: str, body: str) -> int | None:
    """The Discord message a GitHub comment is answering, if it says which.

    GitHub's issue comments have no threading — only a pull request's *review*
    comments carry an `in_reply_to_id`. So the only thing that can be read as a
    reply is a permalink to another comment, which is what this bot writes when
    it carries a Discord reply the other way, and what GitHub's own "Copy link"
    gives a person.

    The "Quote reply" button is deliberately not handled: it copies the text of
    the comment and nothing that identifies it, so matching it back would mean
    guessing from the prose. A wrong reply arrow is worse than none.
    """
    match = _COMMENT_URL.search(body)
    if match is None or match.group(1).lower() != repo.lower():
        return None
    return store.message_for_comment(repo, int(match.group(3)))


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

    # Things are lifted out and put back afterwards, so nothing gets rewritten
    # inside them.
    kept: list[str] = []

    def stash(match: re.Match) -> str:
        kept.append(match.group(0))
        return f"\x00{len(kept) - 1}\x00"

    # Code first, and before the cross-references too: a `#12` written inside a
    # code block is part of the code, not a reference to an issue.
    masked = _CODE.sub(stash, body)

    # Cross-references next, while the URLs are still bare — masking them
    # would otherwise hide them from this pass.
    masked = _point_at_threads(masked, repo)

    # Then the links that pass just made, alongside any that were already there.
    masked = _ALREADY_LINKED.sub(stash, masked)

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


def _attachments(rendered: list[tuple[str, bytes]]) -> list:
    """Rendered diagrams as Discord uploads.

    Uploaded rather than linked. Pointing an embed at the renderer's URL would
    put a host only this network can see in front of Discord's image proxy, and
    would make every old diagram go blank the day that container is retired.
    """
    import discord

    return [discord.File(io.BytesIO(data), filename=name) for name, data in rendered]


async def _post_to_thread(request: web.Request, thread_id: str, **kwargs):
    """Sends into a Discord thread — or any channel — using the connected bot.

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

    try:
        await _dispatch(request, repo, event, payload)
        # After the main handling, because a referenced issue's thread should
        # already exist by the time it is told about the reference.
        await _references_from(request, repo, event, payload.get("action") or "", payload)
    except Exception as error:  # noqa: BLE001
        # Answered as accepted even so, and deliberately. A 500 makes GitHub
        # redeliver, and these handlers post to Discord *before* they finish —
        # so a retry does not repair anything, it just says everything twice.
        # The delivery arrived and was understood; the bug is ours to find in
        # the log, not GitHub's to keep knocking about.
        logger.error("%s/%s blew up: %s", event, payload.get("action"), error, exc_info=True)

    return web.json_response({"message": "accepted"})


async def _dispatch(request: web.Request, repo: str, event: str, payload: dict) -> None:
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
    elif event == "issues" and action in ("milestoned", "demilestoned"):
        await _handle_milestone(request, repo, payload)
    elif event == "issues" and action in ("assigned", "unassigned"):
        await _handle_assignees(request, repo, payload)
    elif event == "issue_comment" and action == "edited":
        await _handle_comment_edited(request, repo, payload)
    elif event == "issue_comment" and action == "deleted":
        await _handle_comment_deleted(request, repo, payload)
    elif event == "issues" and action == "edited":
        await _handle_issue_edited(request, repo, payload)
    elif event == "pull_request":
        await _handle_pull_request(request, repo, payload, action)
    elif event == "pull_request_review" and action == "submitted":
        await _handle_review(request, repo, payload)
    elif event == "pull_request_review_comment":
        await _handle_review_comment(request, repo, payload, action)
    elif event == "workflow_run" and action == "completed":
        await _handle_workflow_run(request, repo, payload)
    elif event in ("label", "milestone"):
        # Not mirrored anywhere — the repository's vocabulary just changed, and
        # the autocompletes may be holding a copy of the old one.
        #
        # Said out loud whether or not anything was cached: this delivery is the
        # only evidence that the subscription is wired up at all, and a cache
        # that silently never invalidates looks exactly like one that does.
        logger.info("%s %s on %s", event, action, repo)
        vocabulary.forget(repo)


#: The embed field milestones live in. A constant because two places have to
#: agree on it — the one that writes the card and the one that later edits it.
MILESTONE_FIELD = "🎯 里程碑"

#: The embed field assignees live in.
ASSIGNEE_FIELD = "👤 負責人"


def _assignee_text(issue: dict) -> str:
    """Who is on the hook, as GitHub knows them.

    Deliberately the GitHub handles rather than Discord mentions: not everyone
    on an issue has run `/login`, and a card that names some people and pings
    others reads as if the unpinged ones matter less.
    """
    names = [user["login"] for user in issue.get("assignees", []) if user]
    return ", ".join(f"`{name}`" for name in names) if names else "*未指定*"


def _milestone_text(issue: dict) -> str:
    milestone = issue.get("milestone")
    if not milestone:
        return "*未指定*"
    due = milestone.get("due_on")
    return f"**{milestone['title']}**" + (f" · 到期 {due[:10]}" if due else "")


async def _card_for(bot, thread_id: str):
    """The message carrying an issue's card, whichever channel shape it is in.

    A forum post's card is the post's own opening message, and a text channel's
    is the announcement the thread hangs off. Both share the thread's id, which
    is why one lookup covers both.
    """
    import discord

    thread = bot.get_channel(int(thread_id))
    if thread is None:
        return None

    parent = getattr(thread, "parent", None)
    try:
        if isinstance(parent, discord.ForumChannel):
            return thread.starter_message or await thread.fetch_message(thread.id)
        return await parent.fetch_message(thread.id)
    except Exception as error:  # noqa: BLE001
        logger.warning("cannot read the card for thread %s: %s", thread_id, error)
        return None


async def _update_card_field(
    request: web.Request, repo: str, payload: dict, name: str, text: str
) -> None:
    """Rewrites one field of an issue's card, or leaves it alone if unchanged."""
    issue = payload["issue"]
    thread_id = store.thread_for_issue(repo, issue["number"])
    if not thread_id:
        return

    card = await _card_for(request.app["bot"], thread_id)
    if card is None or not card.embeds:
        return

    embed = card.embeds[0].copy()
    index = next((i for i, f in enumerate(embed.fields) if f.name == name), -1)
    if index != -1 and embed.fields[index].value == text:
        logger.info("%s on %s#%s is already %s", name, repo, issue["number"], text)
        return

    if index == -1:
        embed.add_field(name=name, value=text, inline=False)
    else:
        embed.set_field_at(index, name=name, value=text, inline=False)

    try:
        await card.edit(embed=embed)
        logger.info("%s for %s#%s -> %s", name, repo, issue["number"], text)
    except Exception as error:  # noqa: BLE001
        logger.warning("could not update %s on thread %s: %s", name, thread_id, error)


async def _handle_milestone(request: web.Request, repo: str, payload: dict) -> None:
    await _update_card_field(
        request, repo, payload, MILESTONE_FIELD, _milestone_text(payload["issue"])
    )


async def _handle_assignees(request: web.Request, repo: str, payload: dict) -> None:
    issue = payload["issue"]
    await _update_card_field(request, repo, payload, ASSIGNEE_FIELD, _assignee_text(issue))

    # Assignment is the one field change worth interrupting somebody for: being
    # put on a task is a request, not a detail. The others (labels, milestone)
    # update the card silently, because nobody needs to be told the moment a
    # card gets a tag.
    thread_id = store.thread_for_issue(repo, issue["number"])
    if not thread_id:
        return

    who = (payload.get("assignee") or {}).get("login")
    if not who:
        return

    # Pinged where the handle is known, named otherwise — a mention only
    # reaches someone who has run `/login`.
    by_handle = {
        (record.get("github_username") or "").lower(): discord_id
        for discord_id, record in store.read_users().items()
        if discord_id.isdigit() and record.get("github_username")
    }
    discord_id = by_handle.get(who.lower())
    mention = f"<@{discord_id}>" if discord_id else f"`{who}`"

    assigned = payload.get("action") == "assigned"
    await _post_to_thread(
        request,
        thread_id,
        content=(
            f"👤 {mention} 被指派了這張單。" if assigned else f"👤 {mention} 不再負責這張單。"
        ),
    )


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
    if thread is None:
        logger.info("labels on %s#%s: thread %s is not in cache", repo, issue["number"], thread_id)
        return
    if not isinstance(getattr(thread, "parent", None), discord.ForumChannel):
        logger.info("labels on %s#%s: %s is not a forum post, nothing to tag",
                    repo, issue["number"], thread_id)
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
        logger.info(
            "labels on %s#%s already match the post's tags (%s), nothing to do",
            repo, issue["number"], [tag.name for tag in wanted] or "none",
        )
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


async def _handle_comment_edited(request: web.Request, repo: str, payload: dict) -> None:
    """Rewrites the Discord copy when a GitHub comment is edited.

    Skipped for comments this bot wrote: those are a Discord message already,
    and rewriting the Discord side from them would fight the edit that caused
    them.
    """
    import discord

    comment = payload["comment"]
    body = comment["body"] or ""
    if store.SYNC_MARKER in body:
        return

    message_id = store.message_for_comment(repo, comment["id"])
    if message_id is None:
        return

    thread_id = store.thread_for_issue(repo, payload["issue"]["number"])
    if not thread_id:
        return
    thread = request.app["bot"].get_channel(int(thread_id))
    if thread is None:
        return

    text, rendered = await mermaid.diagrams(body)
    text = tables.convert(text)
    text, image = _readable_in_discord(text)
    text = _link_github_syntax(text, repo)
    embed = discord.Embed(
        description=text[:4000],
        url=comment["html_url"],
        colour=0x2DA44E,
    )
    embed.set_author(
        name=f"{comment['user']['login']} 在 GitHub 留言（已編輯）",
        url=comment["html_url"],
        icon_url=comment["user"].get("avatar_url"),
    )
    embed.set_footer(text=f"{repo}#{payload['issue']['number']}")
    if image:
        embed.set_image(url=image)

    try:
        message = await thread.fetch_message(message_id)
        # Attachments are replaced wholesale, which is what makes editing a
        # comment on GitHub the way to redraw a diagram that predates this — and
        # the only way, since nothing goes back over old messages.
        await message.edit(embed=embed, attachments=_attachments(rendered))
        logger.info("updated the Discord copy of %s comment %s", repo, comment["id"])
    except Exception as error:  # noqa: BLE001
        logger.warning("could not edit message %s: %s", message_id, error)


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
        channel = cog.announce_channel_for(repo) if cog else None
        if channel is None:
            return
        await announce_issue(bot, channel, repo, {**pull, "kind": "pull"})
        return

    thread_id = store.thread_for_issue(repo, number)
    if not thread_id:
        return

    # A pull request's labels, assignees and milestone arrive as `pull_request`
    # events, never as `issues` ones — so without this routing the card gets
    # built and then never updates, even though the handlers already exist.
    # They read `payload["issue"]`, and a pull request is an issue here.
    if action in ("labeled", "unlabeled"):
        await _handle_labels(request, repo, {**payload, "issue": pull})
        return
    if action in ("assigned", "unassigned"):
        await _handle_assignees(request, repo, {**payload, "issue": pull})
        return
    if action in ("milestoned", "demilestoned"):
        await _handle_milestone(request, repo, {**payload, "issue": pull})
        return
    if action == "edited":
        await _handle_issue_edited(request, repo, {**payload, "issue": pull})
        return

    if action == "review_requested":
        # The same reasoning as being assigned: this is a request aimed at one
        # person, not a detail about the pull request.
        who = (payload.get("requested_reviewer") or {}).get("login")
        team = (payload.get("requested_team") or {}).get("name")
        if who:
            await _post_to_thread(
                request, thread_id, content=f"👀 {_name_or_mention(who)} 被請求 review 這個 PR。"
            )
        elif team:
            await _post_to_thread(request, thread_id, content=f"👀 `{team}` 被請求 review 這個 PR。")
        return

    if action in ("ready_for_review", "converted_to_draft"):
        await _post_to_thread(
            request,
            thread_id,
            content=(
                f"🟢 `{repo}#{number}` 準備好被 review 了。"
                if action == "ready_for_review"
                else f"📝 `{repo}#{number}` 改回草稿。"
            ),
        )
        return

    if action not in ("closed", "reopened"):
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


def _discord_id_for(login: str) -> str | None:
    """The Discord account behind a GitHub handle, where there is one."""
    if not login:
        return None
    for discord_id, record in store.read_users().items():
        if not discord_id.isdigit():
            continue
        if (record.get("github_username") or "").lower() == login.lower():
            return discord_id
    return None


def _name_or_mention(login: str) -> str:
    """Pings where the handle is known, names where it is not.

    A mention only reaches someone who has run `/login`. Falling back to the
    handle in backticks keeps the sentence true for everybody else rather than
    leaving a dead `@`.
    """
    discord_id = _discord_id_for(login)
    return f"<@{discord_id}>" if discord_id else f"`{login}`"


async def _handle_issue_edited(request: web.Request, repo: str, payload: dict) -> None:
    """Carries an edited title or body through to the card and the thread.

    Without this the card keeps the text the issue had when it was filed, for
    good — and an issue's body is usually the thing that gets rewritten as the
    task becomes clearer. Two sides saying different things is the failure this
    whole bot exists to prevent, so it should not be the bot doing it.
    """
    issue = payload["issue"]
    changes = payload.get("changes") or {}
    thread_id = store.thread_for_issue(repo, issue["number"])
    if not thread_id:
        return

    bot = request.app["bot"]

    if "title" in changes:
        # The thread carries the title too, and a stale thread name is the more
        # visible half — it is what people scroll past in the channel list.
        thread = bot.get_channel(int(thread_id))
        wanted = f"#{issue['number']} {issue['title']}"[:100]
        if thread is not None and thread.name != wanted:
            try:
                await thread.edit(name=wanted)
                logger.info("renamed thread %s to %r", thread_id, wanted)
            except Exception as error:  # noqa: BLE001
                logger.warning("could not rename thread %s: %s", thread_id, error)
        await _update_card_field(request, repo, payload, "📝 標題", issue["title"][:1000])

    if "body" not in changes:
        return

    card = await _card_for(bot, thread_id)
    if card is None or not card.embeds:
        return

    text, rendered = await mermaid.diagrams(issue.get("body") or "")
    text = tables.convert(text)
    text, image = _readable_in_discord(text)
    text = _link_github_syntax(text, repo)

    embed = card.embeds[0].copy()
    embed.description = text[:1500] or "*（沒有內文）*"
    if image:
        embed.set_image(url=image)

    try:
        # Attachments go wholesale, so a diagram added in the edit appears and
        # one removed in the edit goes away.
        await card.edit(embed=embed, attachments=_attachments(rendered))
        logger.info("updated the card body for %s#%s", repo, issue["number"])
    except Exception as error:  # noqa: BLE001
        logger.warning("could not update the card for thread %s: %s", thread_id, error)


async def _handle_review(request: web.Request, repo: str, payload: dict) -> None:
    """A submitted pull request review.

    Approving and requesting changes are both addressed *at* the author — one
    unblocks them, the other asks for work — so both ping. A plain comment
    review does not; it is usually just the envelope the inline comments came
    in, and those arrive on their own.
    """
    review = payload["review"]
    pull = payload["pull_request"]
    thread_id = store.thread_for_issue(repo, pull["number"])
    if not thread_id:
        return

    state = (review.get("state") or "").lower()
    reviewer = review["user"]["login"]
    author = (pull.get("user") or {}).get("login", "")
    body = (review.get("body") or "").strip()

    if state == "approved":
        headline = f"✅ **{reviewer}** 通過了這個 PR"
        ping = _name_or_mention(author) if author else ""
    elif state == "changes_requested":
        headline = f"🔴 **{reviewer}** 要求修改"
        ping = _name_or_mention(author) if author else ""
    else:
        # `commented`, and anything GitHub adds later. Only worth relaying when
        # it actually carries words.
        if not body:
            return
        headline = f"💬 **{reviewer}** 留下了 review 意見"
        ping = ""

    import discord

    embed = discord.Embed(
        description=_link_github_syntax(tables.convert(body), repo)[:4000] if body else "",
        url=review.get("html_url"),
        colour=0x2DA44E if state == "approved" else 0xD1242F if state == "changes_requested" else 0x57606A,
    )
    embed.set_author(
        name=f"{reviewer} 在 GitHub review",
        url=review.get("html_url"),
        icon_url=review["user"].get("avatar_url"),
    )
    embed.set_footer(text=f"{repo}#{pull['number']}")

    await _post_to_thread(
        request, thread_id, content=f"{headline}{' ' + ping if ping else ''}", embed=embed
    )


async def _handle_review_comment(request: web.Request, repo: str, payload: dict, action: str) -> None:
    """An inline review comment, on a line of the diff.

    The one place GitHub has real threading: a reply carries `in_reply_to_id`,
    which maps straight onto a Discord reply with nothing invented in between.
    """
    import discord

    comment = payload["comment"]
    pull = payload["pull_request"]
    number = pull["number"]
    thread_id = store.thread_for_issue(repo, number)
    if not thread_id:
        return

    message_id = store.message_for_comment(repo, comment["id"], kind="review")

    if action == "deleted":
        if message_id is None:
            return
        thread = request.app["bot"].get_channel(int(thread_id))
        if thread is not None:
            try:
                await (await thread.fetch_message(message_id)).delete()
            except Exception as error:  # noqa: BLE001
                logger.warning("could not delete message %s: %s", message_id, error)
        store.forget_message(message_id)
        return

    body = comment.get("body") or ""
    text, rendered = await mermaid.diagrams(body)
    text = tables.convert(text)
    text, image = _readable_in_discord(text)
    text = _link_github_syntax(text, repo)

    # Which line is being talked about is most of the meaning of an inline
    # comment; without it the reader has no idea what "this looks wrong" is about.
    where = comment.get("path") or ""
    line = comment.get("line") or comment.get("original_line")
    if where and line:
        where = f"{where}:{line}"

    embed = discord.Embed(description=text[:4000], url=comment["html_url"], colour=0x8250DF)
    embed.set_author(
        name=f"{comment['user']['login']} 在 GitHub review 了程式碼",
        url=comment["html_url"],
        icon_url=comment["user"].get("avatar_url"),
    )
    embed.set_footer(text=f"{repo}#{number} · {where}" if where else f"{repo}#{number}")
    if image:
        embed.set_image(url=image)

    if action == "edited":
        if message_id is None:
            return
        thread = request.app["bot"].get_channel(int(thread_id))
        if thread is None:
            return
        try:
            message = await thread.fetch_message(message_id)
            await message.edit(embed=embed, attachments=_attachments(rendered))
        except Exception as error:  # noqa: BLE001
            logger.warning("could not edit message %s: %s", message_id, error)
        return

    extra = {}
    if rendered:
        extra["files"] = _attachments(rendered)

    parent = comment.get("in_reply_to_id")
    if parent:
        answering = store.message_for_comment(repo, int(parent), kind="review")
        if answering is not None:
            extra["reference"] = discord.MessageReference(
                message_id=answering, channel_id=int(thread_id), fail_if_not_exists=False
            )

    posted = await _post_to_thread(request, thread_id, embed=embed, **extra)
    if posted is not None:
        store.remember_comment(posted.id, repo, number, comment["id"], kind="review")


#: Conclusions worth waking somebody for.
#:
#: `cancelled` is left out because it is nearly always deliberate — somebody
#: pushed again, or stopped the run — and reporting it turns a normal working
#: rhythm into alarms.
CI_FAILURES = {"failure", "timed_out", "action_required"}


async def _pull_for_run(repo: str, run: dict) -> dict | None:
    """The pull request a workflow run belongs to.

    `pull_requests` in the payload is empty more often than the shape suggests
    — notably for runs on forks — so the branch is the fallback. Fetched rather
    than guessed because the run does not carry who opened the pull request,
    and that is who needs telling.
    """
    token = os.getenv("GITHUB_BOT_TOKEN")
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    numbers = [p["number"] for p in run.get("pull_requests") or [] if p.get("number")]
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            if not numbers:
                branch = run.get("head_branch")
                if not branch:
                    return None
                owner = repo.split("/")[0]
                async with session.get(
                    f"{GITHUB_API}/repos/{repo}/pulls",
                    params={"head": f"{owner}:{branch}", "state": "open"},
                    headers=headers,
                ) as response:
                    found = await response.json()
                if not isinstance(found, list) or not found:
                    return None
                return found[0]
            async with session.get(
                f"{GITHUB_API}/repos/{repo}/pulls/{numbers[0]}", headers=headers
            ) as response:
                pull = await response.json()
        return pull if isinstance(pull, dict) and pull.get("number") else None
    except Exception as error:  # noqa: BLE001
        logger.warning("could not look up the pull request for a workflow run: %s", error)
        return None


async def _handle_workflow_run(request: web.Request, repo: str, payload: dict) -> None:
    """Reports a failed CI run into the CI channel.

    Failures only. A green run is the expected outcome, and saying so every
    time trains people to scroll past the channel — which costs exactly the
    attention the red ones need.

    Its own channel rather than the pull request's thread, so that the feed is
    complete in one place and can be muted as a unit. The person who opened the
    pull request is pinged by name, and a mention reaches them wherever it is
    written, so nothing is lost by keeping it out of the thread.
    """
    run = payload.get("workflow_run") or {}
    if (run.get("conclusion") or "").lower() not in CI_FAILURES:
        return

    channel_id = os.getenv("CI_CHANNEL_ID", "")
    if not channel_id.isdigit():
        logger.info("CI_CHANNEL_ID is unset; a failed run goes unreported")
        return

    conclusion = (run.get("conclusion") or "").lower()
    verb = {"timed_out": "逾時", "action_required": "需要處理"}.get(conclusion, "失敗")
    name = run.get("name") or "CI"

    pull = await _pull_for_run(repo, run)
    if pull is None:
        # A run with no pull request behind it — nearly always a push straight
        # to the default branch. Still worth reporting, and arguably the most
        # worth reporting; there is just nobody in particular to point at.
        where = f"`{run.get('head_branch') or '?'}`"
        who = ""
    else:
        number = pull["number"]
        thread_id = store.thread_for_issue(repo, number)
        # Linked into Discord where the conversation already is, so the channel
        # is somewhere to act from rather than only somewhere to be told.
        where = (
            f"[{repo}#{number}](https://discord.com/channels/{GUILD_ID}/{thread_id})"
            if thread_id and GUILD_ID
            else f"`{repo}#{number}`"
        )
        author = (pull.get("user") or {}).get("login", "")
        who = f" {_name_or_mention(author)}" if author else ""

    await _post_to_thread(
        request,
        channel_id,
        content=f"❌ **{name}** {verb} · {where}{who}\n<{run.get('html_url', '')}>",
    )


#: A reference to another issue, with the closing keywords GitHub itself acts on.
#:
#: Two alternatives rather than one optional prefix, because the guard that stops
#: `abc#1` and `.../issues/3#issuecomment-9` from matching is a lookbehind, and a
#: lookbehind cannot sit after an optional group that may have just consumed a
#: word character.
_REFERENCE = re.compile(
    r"(?P<closing>\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+)?"
    r"(?:(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(?P<qualified>\d+)"
    r"|(?<![\w#/])#(?P<bare>\d+))\b",
    re.IGNORECASE,
)

#: Enough for a release note that lists what went into it; past that, something
#: is generating text and every thread it names does not need telling.
MAX_REFERENCES = 10


def _references_in(body: str, repo: str) -> list[tuple[str, int, bool]]:
    """The issues a body points at, as `(repo, number, closes)`.

    Code is stripped first: a `#12` inside a fence is part of the code, and
    announcing it into somebody's thread is a false alarm about work nobody did.
    """
    found: list[tuple[str, int, bool]] = []
    seen: set[tuple[str, int]] = set()
    for match in _REFERENCE.finditer(_CODE.sub(" ", body or "")):
        number = int(match.group("qualified") or match.group("bare"))
        where = (match.group("repo") or repo).lower()
        if (where, number) in seen:
            continue
        seen.add((where, number))
        found.append((where, number, bool(match.group("closing"))))
        if len(found) >= MAX_REFERENCES:
            break
    return found


async def _announce_references(
    request: web.Request, repo: str, describe: str, url: str, number: int, body: str
) -> None:
    """Tells a thread that something elsewhere just pointed at its issue.

    GitHub records this on the issue's own timeline, and there is no webhook for
    it — `cross-referenced` is a timeline event, not a delivery. So it is read
    out of the body that *does* arrive: the pull request or comment doing the
    referencing. Being told that a PR will close your issue is the sort of thing
    the thread exists for, and without this it only ever showed up on GitHub.
    """
    for where, target, closes in _references_in(body, repo):
        if where == repo.lower() and target == number:
            continue  # an issue referring to itself, usually in a checklist
        thread_id = store.thread_for_issue(where, target)
        if not thread_id:
            continue
        await _post_to_thread(
            request,
            thread_id,
            content=(
                f"🔗 {describe} {'會在合併時關閉這張單' if closes else '提到了這張單'}\n{url}"
            ),
        )
        logger.info("%s references %s#%s", describe, where, target)


async def _references_from(
    request: web.Request, repo: str, event: str, action: str, payload: dict
) -> None:
    """Whichever body just arrived, scanned for references to other issues."""
    if event == "issues" and action == "opened":
        issue = payload["issue"]
        describe = f"**#{issue['number']}**「{(issue.get('title') or '')[:40]}」"
    elif event == "pull_request" and action == "opened":
        issue = payload["pull_request"]
        describe = f"**PR #{issue['number']}**「{(issue.get('title') or '')[:40]}」"
    elif event == "issue_comment" and action == "created":
        body = payload["comment"].get("body") or ""
        # Comments this bot wrote are Discord messages already. The person who
        # pasted the link is looking at it, GitHub has recorded the reference on
        # its own timeline, and `/close` and the backfill both write long bodies
        # that would otherwise set off a burst of these.
        if store.SYNC_MARKER in body:
            return
        issue = payload["issue"]
        where = f"PR #{issue['number']}" if issue.get("pull_request") else f"#{issue['number']}"
        describe = f"**{payload['comment']['user']['login']}** 在 {where} 的留言"
        await _announce_references(
            request, repo, describe, payload["comment"]["html_url"], issue["number"], body
        )
        return
    else:
        return

    await _announce_references(
        request, repo, describe, issue["html_url"], issue["number"], issue.get("body") or ""
    )


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
    channel = cog.announce_channel_for(repo) if cog else None
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
    text, rendered = await mermaid.diagrams(body)
    text = tables.convert(text)
    text, image = _readable_in_discord(text)
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
    # Always present, even when empty: a field that appears and disappears
    # makes the card jump around, and "沒有" is itself worth knowing on a
    # board that plans by milestone.
    embed.add_field(name=ASSIGNEE_FIELD, value=_assignee_text(issue), inline=False)
    embed.add_field(name=MILESTONE_FIELD, value=_milestone_text(issue), inline=False)
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
            created = await channel.create_thread(
                name=thread_name, embed=embed, applied_tags=tags, files=_attachments(rendered)
            )
            thread = created.thread
        else:
            announcement = await channel.send(embed=embed, files=_attachments(rendered))
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
        said, drawn = await mermaid.diagrams(comment.get("body") or "")
        said = tables.convert(said)
        said, picture = _readable_in_discord(said)
        said = _link_github_syntax(said, repo)
        past = discord.Embed(description=said[:4000] or "*（空留言）*", colour=0x57606A)
        past.set_author(name=f"{comment.get('author', 'unknown')} · 既有留言")
        if picture:
            past.set_image(url=picture)
        try:
            await thread.send(embed=past, files=_attachments(drawn))
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

    text, rendered = await mermaid.diagrams(body)
    text = tables.convert(text)
    text, image = _readable_in_discord(text)
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

    extra = {}
    if rendered:
        extra["files"] = _attachments(rendered)

    # A comment that links to another one is answering it, so say so with a real
    # Discord reply rather than leaving the reader to follow the URL.
    answering = _replying_to(repo, body)
    if answering is not None:
        extra["reference"] = discord.MessageReference(
            message_id=answering,
            channel_id=int(thread_id),
            # The message may have been deleted since. A reply that cannot find
            # its parent should still be posted, not dropped.
            fail_if_not_exists=False,
        )

    posted = await _post_to_thread(request, thread_id, embed=embed, **extra)

    # So that reacting to this message in Discord lands on the GitHub comment it
    # came from. Only messages going the *other* way were recorded before, which
    # meant reactions worked on your own words and silently did nothing on
    # everybody else's — the half you are more likely to want to react to.
    if posted is not None:
        store.remember_comment(posted.id, repo, issue_number, comment["id"])

    # Sending may have woken an archived thread; if the issue is closed, it
    # should not have.
    await _settle_archive(request, thread_id, payload["issue"])


async def _settle_archive(request: web.Request, thread_id: str, issue: dict) -> None:
    """Puts a thread back to archived after the bot has posted into it.

    Posting into an archived thread un-archives it. That is exactly right when a
    person says something, and exactly wrong when the bot mirrors a comment that
    arrived in the same breath as the close: GitHub sends "留言並關閉" as two
    separate deliveries, they are handled concurrently, and whichever finishes
    second wins. Half the time that is the comment, which leaves a closed issue
    with an open thread and nothing in the log to say so.

    Settled by reading the issue's own state rather than by remembering what was
    just done — the same guard the labels and the title use, and it means a
    thread whose issue is closed converges on archived no matter which order the
    deliveries land in.

    Nothing here fights a person: a Discord message reaches GitHub carrying
    `SYNC_MARKER`, and the echo of it is dropped before this is ever reached.
    """
    if (issue.get("state") or "").lower() != "closed":
        return

    thread = request.app["bot"].get_channel(int(thread_id))
    if thread is None:
        return
    try:
        # Attempted rather than checked first: the cached `archived` flag is
        # what the gateway last said, and the send that just happened is
        # precisely the event it may not have caught up with. Re-archiving an
        # archived thread costs one call and changes nothing.
        await thread.edit(archived=True)
    except Exception as error:  # noqa: BLE001
        logger.warning("could not re-archive thread %s: %s", thread_id, error)


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
    """Rewrites the task card the thread hangs off.

    Through `_card_for`, because the card is in a different place depending on
    the channel: a text channel's is the announcement the thread was started
    from, while a forum post's is the post's own opening message. This used to
    reach for `parent.fetch_message` either way, which a `ForumChannel` does not
    have — so on a forum, every closed issue kept a card saying 開發中 and left
    one line in the log to say so.
    """
    card = await _card_for(bot, thread.id)
    if card is None or not card.embeds:
        return
    announcement = card

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
