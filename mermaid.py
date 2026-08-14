"""Mermaid diagrams, turned into something Discord can show.

GitHub renders a ```mermaid fence by running mermaid.js in the reader's own
browser. Discord has nothing of the sort and never will — its markdown does not
look at a fence's language at all — so a diagram arrives as a wall of arrows and
`participant` lines that says nothing to anyone. The only way it survives the
trip is to rasterise it here and send the picture.

Rendering needs a browser engine, because mermaid *is* a browser library. That
is why this talks to a service rather than doing the work in-process, and why
the service is meant to be the one in `docker-compose.yml`, reachable only on an
internal network: the diagrams in a private repository *are* its architecture,
and a public renderer would be handed every one of them.

With `MERMAID_URL` unset the whole thing is off and fences are left exactly as
they arrive — which is what happened before this existed.
"""

import asyncio
import base64
import json
import logging
import os
import re
import zlib

import aiohttp

logger = logging.getLogger(__name__)

#: The opening line of a mermaid fence. Both fence characters, because GitHub
#: accepts either, and any suffix after the language because GitHub allows
#: attributes there.
_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<ticks>`{3,}|~{3,})[ \t]*mermaid\b[^\n]*$", re.IGNORECASE)

#: Where an extracted diagram sat, until it is known whether it rendered.
#:
#: `\x00` cannot appear in a GitHub comment, and the digits are on the inside so
#: this cannot be confused with the `\x00N\x00` markers `_link_github_syntax`
#: uses for its own masking.
_TOKEN = "\x00mermaid:%d\x00"
_TOKEN_RE = re.compile(r"\x00mermaid:(\d+)\x00")

#: How long the whole batch gets. Renders measure about 200 ms against the
#: bundled service, so this is not a budget anyone should reach — it is there so
#: that a wedged renderer cannot hold a webhook delivery open past the ten
#: seconds GitHub waits before calling it a failure.
BUDGET = 6

#: Discord takes ten attachments on a message. A comment with more diagrams than
#: that is not something to design for; the rest keep their source.
MAX_DIAGRAMS = 10

#: Refuse anything absurd rather than fail the upload. A mermaid PNG runs tens
#: of kilobytes, so this only catches a renderer that has gone wrong.
MAX_BYTES = 8_000_000

#: Keeps the request line comfortably inside what any proxy will carry. A
#: diagram whose source is bigger than roughly four kilobytes falls back to the
#: compressed form, and one that is *still* too big keeps its source.
MAX_PAYLOAD = 6000


def enabled() -> bool:
    return bool(os.getenv("MERMAID_URL"))


def _theme() -> str:
    return os.getenv("MERMAID_THEME", "dark")


def _background() -> str:
    """The colour behind the diagram.

    Not cosmetic and not optional: the renderer's default output is transparent,
    and dark-themed strokes on a transparent background are invisible against
    Discord's own dark background. The default is Discord's message colour, so
    the picture reads as part of the message rather than pasted onto it.
    """
    return os.getenv("MERMAID_BACKGROUND", "313338")


def _closing(line: str, ticks: str) -> bool:
    """Whether a line closes a fence opened with `ticks`."""
    stripped = line.strip()
    return len(stripped) >= len(ticks) and set(stripped) == {ticks[0]}


def _fence(source: str) -> str:
    """A diagram put back the way it arrived, for when rendering did not work."""
    return f"```mermaid\n{source}\n```"


def extract(body: str) -> tuple[str, list[str]]:
    """Lifts every mermaid fence out, leaving a token where each one was.

    Done before any of the other rewriting in `web.server`, and that ordering
    matters beyond tidiness: diagram source is full of arrows and short hex-ish
    words, and the commit-SHA and @mention passes would otherwise reach inside a
    fence and corrupt it.
    """
    # Cheap way out for the overwhelming majority of comments. Case-insensitive
    # to agree with `_FENCE`, which is — GitHub accepts ```Mermaid.
    if "mermaid" not in body.lower():
        return body, []

    lines = body.split("\n")
    kept: list[str] = []
    sources: list[str] = []
    index = 0

    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None:
            kept.append(lines[index])
            index += 1
            continue

        ticks = opening.group("ticks")
        close = next(
            (i for i in range(index + 1, len(lines)) if _closing(lines[i], ticks)), None
        )
        if close is None:
            # An unterminated fence is a typo, not a diagram. Left where it is
            # rather than swallowing the rest of the comment into it.
            kept.append(lines[index])
            index += 1
            continue

        sources.append("\n".join(lines[index + 1 : close]))
        kept.append(opening.group("indent") + _TOKEN % (len(sources) - 1))
        index = close + 1

    return "\n".join(kept), sources


def _payload(source: str) -> str | None:
    """The diagram encoded for the renderer's URL.

    Plain base64 while it fits, which is the overwhelmingly common case and
    keeps the request readable in a log. Deflated only when it has to be —
    that form is longer for a short diagram because of its JSON wrapper, and
    only starts paying for itself on the large ones.
    """
    plain = base64.urlsafe_b64encode(source.encode()).decode()
    if len(plain) <= MAX_PAYLOAD:
        return plain

    wrapped = json.dumps({"code": source, "mermaid": {"theme": _theme()}}, ensure_ascii=False)
    packed = "pako:" + base64.urlsafe_b64encode(zlib.compress(wrapped.encode(), 9)).decode()
    if len(packed) <= MAX_PAYLOAD:
        return packed

    logger.info("a mermaid diagram is too large to render; leaving its source in place")
    return None


async def _render(session: aiohttp.ClientSession, source: str) -> bytes | None:
    payload = _payload(source)
    if payload is None:
        return None

    base = os.getenv("MERMAID_URL", "").rstrip("/")
    url = f"{base}/img/{payload}?type=png&theme={_theme()}&bgColor={_background()}"
    try:
        async with session.get(url) as response:
            if response.status != 200:
                logger.warning("the mermaid renderer answered %s", response.status)
                return None
            data = await response.read()
    except Exception as error:  # noqa: BLE001 - unreachable, timed out, anything
        logger.warning("could not reach the mermaid renderer: %s", error)
        return None

    # A renderer that fails on a malformed diagram answers with an error page
    # rather than a status, so the bytes are what has to be checked.
    if not data.startswith(b"\x89PNG"):
        logger.warning("the mermaid renderer returned %d bytes that are not a PNG", len(data))
        return None
    if len(data) > MAX_BYTES:
        logger.warning("a rendered diagram is %d bytes; too big to attach", len(data))
        return None
    return data


async def diagrams(body: str) -> tuple[str, list[tuple[str, bytes]]]:
    """A comment with its diagrams pulled out, and those diagrams as PNGs.

    Returns the text to show and `[(filename, png)]` to attach. Anything that
    could not be rendered — the service is down, the diagram is malformed, the
    feature is switched off — keeps its original fence, so the worst case is
    exactly the behaviour that came before.
    """
    text, sources = extract(body)
    if not sources:
        return body, []

    rendered: list[bytes | None] = [None] * len(sources)
    if enabled():
        wanted = sources[:MAX_DIAGRAMS]
        if len(sources) > MAX_DIAGRAMS:
            logger.info(
                "a comment has %d diagrams; rendering the first %d",
                len(sources), MAX_DIAGRAMS,
            )
        try:
            timeout = aiohttp.ClientTimeout(total=BUDGET)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                done = await asyncio.gather(*(_render(session, s) for s in wanted))
            rendered[: len(done)] = done
        except Exception as error:  # noqa: BLE001
            logger.warning("mermaid rendering failed outright: %s", error)

    files: list[tuple[str, bytes]] = []

    def swap(match: re.Match) -> str:
        source = sources[int(match.group(1))]
        png = rendered[int(match.group(1))]
        if png is None:
            return _fence(source)
        files.append((f"diagram-{len(files) + 1}.png", png))
        return f"📊 **圖表 {len(files)}**"

    return _TOKEN_RE.sub(swap, text), files
