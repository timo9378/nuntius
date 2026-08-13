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
