"""A repository's own vocabulary — the labels and milestones it already has.

Read on every keystroke of an autocomplete, which is what makes this worth its
own module. The version this replaces went through PyGithub and cost two round
trips: one to fetch a repository object whose fields were then never used
(measured at ~800 ms), and one for the list itself. Worse, PyGithub's paginated
lists fetch lazily, so the HTTP actually happened while iterating the result —
outside the executor, on the event loop, stopping the whole bot for the
duration.

Discord gives an autocomplete three seconds and then simply shows nothing, so
this was not merely slow; it was close to broken.

Caching is safe here in a way it usually is not, because the vocabulary can only
be changed from the GitHub side and GitHub says when it happens: the `label` and
`milestone` webhooks call `forget()`. The bot itself never adds to the
vocabulary — `/label` and `/milestone` only ever apply what already exists, on
purpose — so there is no second writer to miss.

The TTL is a backstop for a delivery that never arrives, not the main mechanism.
"""

import logging
import os
import time

import aiohttp

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

#: Long enough that a burst of keystrokes costs one request, short enough that a
#: missed webhook heals itself before anybody files a bug about it.
TTL = 300

#: One page. A repository with more than a hundred labels has a bigger problem
#: than this autocomplete, and Discord only shows twenty-five choices anyway.
PER_PAGE = 100

TIMEOUT = aiohttp.ClientTimeout(total=5)

#: (repo, kind) -> (fetched_at, names)
_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}


def forget(repo: str) -> None:
    """Drops a repository's cached vocabulary, both kinds."""
    dropped = [key for key in _cache if key[0] == repo.lower()]
    for key in dropped:
        del _cache[key]
    if dropped:
        logger.info("vocabulary for %s changed; re-reading it next time", repo)


async def _fetch(repo: str, kind: str, params: dict, field: str) -> list[str] | None:
    """The live list, or `None` if GitHub could not be asked."""
    token = os.getenv("GITHUB_BOT_TOKEN")
    if not token:
        return None
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(
                f"{GITHUB_API}/repos/{repo}/{kind}",
                params={"per_page": str(PER_PAGE), **params},
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
            ) as response:
                if response.status != 200:
                    logger.warning("GitHub answered %s for %s of %s",
                                   response.status, kind, repo)
                    return None
                found = await response.json()
    except Exception as error:  # noqa: BLE001
        logger.warning("could not read %s of %s: %s", kind, repo, error)
        return None
    if not isinstance(found, list):
        return None
    return [item[field] for item in found if isinstance(item, dict) and item.get(field)]


async def _names(repo: str, kind: str, params: dict, field: str) -> list[str]:
    key = (repo.lower(), kind)
    cached = _cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < TTL:
        return cached[1]

    fresh = await _fetch(repo, kind, params, field)
    if fresh is None:
        # Stale beats empty. An autocomplete that suddenly offers nothing reads
        # as "there are no labels", which sends people off to check GitHub over
        # what is usually a hiccup.
        return cached[1] if cached is not None else []

    _cache[key] = (time.monotonic(), fresh)
    return fresh


async def labels(repo: str) -> list[str]:
    return await _names(repo, "labels", {}, "name")


async def milestones(repo: str) -> list[str]:
    """Open ones only — a closed milestone is not something to file work into."""
    return await _names(repo, "milestones", {"state": "open"}, "title")
