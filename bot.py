"""nuntius — a messenger between Discord and GitHub issues.

Runs two things in one process: the Discord client, and the HTTP endpoints
GitHub needs to reach (the OAuth callback and webhook deliveries). Those used
to live in a separate API service, which meant this bot could not be deployed
on its own. Now it can.
"""

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

from web import server

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))

#: Settings the dev flow cannot work without. Checked at start-up rather than
#: at first use: a bot that looks healthy and then fails the first `/start-dev`
#: is worse than one that refuses to start and says which line is missing.
REQUIRED = ("DISCORD_TOKEN", "GITHUB_BOT_TOKEN", "GITHUB_REPO_OWNER", "GITHUB_REPO_NAME")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
# Not privileged, but not in the default set either — without it the reaction
# listeners never fire and mirroring reactions onto GitHub silently does
# nothing.
intents.reactions = True


class Nuntius(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.http_runner = None

    async def setup_hook(self):
        cogs = ["cogs.devflow"]

        # Only when there is something to watch. Loading it regardless would
        # mean an instance that does not use Docker still opens a connection to
        # a daemon that may not be reachable, and logs the failure every minute.
        if os.getenv("WATCH_CONTAINER"):
            cogs.append("cogs.docker_watch")
        else:
            logger.info("WATCH_CONTAINER is unset; the Docker cog stays unloaded")

        for cog in cogs:
            try:
                await self.load_extension(cog)
                logger.info("loaded %s", cog)
            except Exception as error:  # noqa: BLE001
                logger.error("could not load %s: %s", cog, error, exc_info=True)

        self.http_runner = await server.start(self, HTTP_HOST, HTTP_PORT)

        try:
            synced = await self.tree.sync()
            logger.info("synced %d application commands", len(synced))
        except Exception as error:  # noqa: BLE001
            logger.error("could not sync application commands: %s", error, exc_info=True)

    async def close(self):
        # Before the client, so an in-flight webhook is not answered by a bot
        # that has already dropped its Discord connection.
        if self.http_runner is not None:
            await self.http_runner.cleanup()
        await super().close()

    async def on_ready(self):
        logger.info("signed in as %s (%s)", self.user.name, self.user.id)


def main() -> int:
    missing = [name for name in REQUIRED if not os.getenv(name)]
    if missing:
        logger.critical("missing required settings: %s", ", ".join(missing))
        return 1

    # Not in REQUIRED because the bot is useful without them — the dev flow just
    # falls back to commenting as itself rather than as the person who typed.
    for optional, consequence in (
        ("GITHUB_OAUTH_CLIENT_ID", "/github-login will refuse"),
        ("GITHUB_WEBHOOK_SECRET", "GitHub comments will not reach Discord"),
    ):
        if not os.getenv(optional):
            logger.warning("%s is unset: %s", optional, consequence)

    try:
        asyncio.run(Nuntius().start(DISCORD_TOKEN))
    except discord.LoginFailure:
        logger.critical("Discord rejected DISCORD_TOKEN")
        return 1
    except KeyboardInterrupt:
        logger.info("interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
