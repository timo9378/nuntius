"""The two JSON files this bot keeps, in one place.

Both used to be opened by path from three different modules — the bot, the
OAuth callback and the webhook receiver — which is how the webhook ended up
reading the wrong one. Everything goes through here now.

The files are deliberately still flat JSON: there is one instance, the whole
dataset is a few kilobytes, and a database would be a dependency to back up.
"""

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "data")

#: Appended to every GitHub comment this bot creates from a Discord message.
#:
#: The loop guard used to compare the comment's author against the bot's own
#: GitHub account, which breaks the moment the bot posts under a real person's
#: token — every comment that person writes on GitHub then looks like an echo
#: and never reaches Discord. That is not an edge case: a one-person instance
#: uses its owner's PAT, so it is the *default*.
#:
#: An HTML comment because GitHub renders markdown — it is invisible in the
#: issue and survives a round trip through the API unchanged.
SYNC_MARKER = "<!-- nuntius:from-discord -->"

#: Discord user id -> {access_token, github_username, timestamp}
#:
#: Written by the OAuth callback, read when deciding whose GitHub identity a
#: synced comment is posted under.
USER_MAPPINGS_FILE = os.path.join(DATA_DIR, "user_github_mappings.json")

#: Discord thread id -> {issue_number: int, repo: "owner/name"}
#:
#: Written when a dev thread is created, read in both directions: a Discord
#: message needs the issue to comment on, and a GitHub comment needs the thread
#: to post into.
THREAD_MAPPINGS_FILE = os.path.join(DATA_DIR, "thread_issue_mappings.json")

#: Discord message id -> {repo, comment_id}
#:
#: Only needed so that a reaction added in Discord can find the GitHub comment
#: that message became. Kept separate from the thread mappings because it grows
#: per message rather than per task, and losing it costs nothing but reactions.
COMMENT_MAPPINGS_FILE = os.path.join(DATA_DIR, "message_comment_mappings.json")

#: How many message→comment pairs to keep.
#:
#: This file is the only one that grows without bound — one entry per synced
#: message, forever. Reactions arrive within minutes of a message in practice,
#: so the oldest entries are dead weight; trimming keeps the file small enough
#: that rewriting it on every message stays cheap.
COMMENT_MAPPING_LIMIT = 2000

# Both files are read and written from the Discord event loop *and* from the
# aiohttp handlers. The writes are small and rare, so one lock is cheaper than
# reasoning about which of them can interleave.
_lock = threading.Lock()


def _read(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        logger.error("%s is not valid JSON; treating it as empty", path)
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Written beside the target and renamed: a crash mid-write would otherwise
    # leave a truncated file, and the next read would log a JSON error and
    # silently start from nothing — losing every thread mapping at once.
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=4, ensure_ascii=False)
    os.replace(temporary, path)


def read_users() -> dict:
    with _lock:
        return _read(USER_MAPPINGS_FILE)


def write_users(data: dict) -> None:
    with _lock:
        _write(USER_MAPPINGS_FILE, data)


def read_threads() -> dict:
    with _lock:
        return _read(THREAD_MAPPINGS_FILE)


def write_threads(data: dict) -> None:
    with _lock:
        _write(THREAD_MAPPINGS_FILE, data)


def remember_comment(
    message_id: int, repo_full_name: str, issue_number: int, comment_id: int
) -> None:
    """Records which GitHub comment a Discord message became.

    The issue number is stored alongside because PyGithub reaches a comment
    through its issue, not through the repository.
    """
    with _lock:
        data = _read(COMMENT_MAPPINGS_FILE)
        data[str(message_id)] = {
            "repo": repo_full_name,
            "issue_number": issue_number,
            "comment_id": comment_id,
        }
        if len(data) > COMMENT_MAPPING_LIMIT:
            # dicts keep insertion order, and ids only ever increase, so the
            # front of the file is the oldest.
            for stale in list(data)[: len(data) - COMMENT_MAPPING_LIMIT]:
                del data[stale]
        _write(COMMENT_MAPPINGS_FILE, data)


def comment_for_message(message_id: int) -> dict | None:
    """The GitHub comment a Discord message became, if it is still recorded."""
    return _read(COMMENT_MAPPINGS_FILE).get(str(message_id))


def message_for_comment(repo_full_name: str, comment_id: int) -> int | None:
    """The Discord message a GitHub comment was mirrored into.

    A linear scan rather than a second index: the file is capped at a couple of
    thousand rows, and a deletion is rare enough that a second copy of the data
    to keep in step would cost more than it saves.
    """
    for message_id, record in _read(COMMENT_MAPPINGS_FILE).items():
        if record.get("comment_id") == comment_id and record.get("repo") == repo_full_name:
            return int(message_id)
    return None


def forget_message(message_id: int) -> None:
    """Drops a message→comment pair, once one side of it is gone."""
    with _lock:
        data = _read(COMMENT_MAPPINGS_FILE)
        if data.pop(str(message_id), None) is not None:
            _write(COMMENT_MAPPINGS_FILE, data)


def thread_for_issue(repo_full_name: str, issue_number: int) -> str | None:
    """Finds the Discord thread mirroring a GitHub issue.

    The inverse of what the bot writes. Two things to be careful about, both of
    which the version this was ported from got wrong:

    * the mapping lives in the *thread* file, not the user file — the user file
      holds OAuth tokens and has no issue numbers in it at all;
    * `issue_number` is stored as an `int`, so comparing it against a string
      never matches.

    The repository is compared too. Without it, two repositories that both have
    an issue #12 would answer for each other.
    """
    for thread_id, mapping in read_threads().items():
        if not isinstance(mapping, dict):
            continue
        if int(mapping.get("issue_number", -1)) != int(issue_number):
            continue
        # Older rows predate the repo field; a number-only match is the best
        # that can be done for those, and is what the previous behaviour was.
        recorded = mapping.get("repo")
        if recorded and recorded.lower() != repo_full_name.lower():
            continue
        return thread_id
    return None
