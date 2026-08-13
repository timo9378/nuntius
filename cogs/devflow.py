import discord
from discord.ext import commands
from discord import app_commands, Embed, Interaction, ButtonStyle, TextStyle, ui
import os
import datetime
from github import Github, GithubException, InputFileContent # Keep this for type hinting if needed, actual calls via executor
import logging
import re
import json
import uuid 
import asyncio
import functools

import store
from web import server

logger = logging.getLogger(__name__)

#: A link to a Discord channel, thread or message.
_DISCORD_LINK = re.compile(r"https?://(?:\w+\.)?discord\.com/channels/(\d+)/(\d+)(?:/(\d+))?")


def _reference(mapped: dict, message) -> str:
    """A GitHub reference to the issue a thread mirrors.

    `#12` when it is the same repository as the issue being commented on, and
    the fully qualified `owner/repo#12` otherwise — the short form silently
    means the wrong issue when the repositories differ.

    Written as a reference rather than left as a Discord URL because GitHub
    records `#12` on both issues' timelines, so the link is visible from the
    other end too. A URL is just a URL.
    """
    here = store.issue_for_thread(getattr(message.channel, "id", 0)) or {}
    same_repo = here.get("repo") == mapped["repo"]
    return f"#{mapped['issue_number']}" if same_repo else f"{mapped['repo']}#{mapped['issue_number']}"


def _thread_url_as_reference(match: re.Match, message) -> str:
    """Turns a pasted Discord link into an issue reference where one exists."""
    # The second id is the channel; a third, if present, is a single message
    # inside it, which still identifies the same thread.
    mapped = store.issue_for_thread(match.group(2))
    return _reference(mapped, message) if mapped else match.group(0)

class DevTaskView(ui.View):
    def __init__(self, cog_instance, original_interaction: Interaction, task_description: str,
                 repo: str = None, labels: str = None, milestone: str = None):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.original_interaction = original_interaction
        self.task_description = task_description
        self.repo_override = repo
        self.labels = labels
        self.milestone = milestone
        self.github_issue_url = None
        self.github_issue_number = None

    @ui.button(label="📦 在 GitHub 建立 Issue", style=ButtonStyle.primary, custom_id="create_github_issue_button")
    async def create_github_issue_button_callback(self, interaction: Interaction, button: ui.Button):
        await self.cog.handle_create_github_issue(interaction, button, self, self.task_description, self.repo_override)

    @ui.button(label="✋ 我想協作", style=ButtonStyle.secondary, custom_id="collaborate_button")
    async def collaborate_button_callback(self, interaction: Interaction, button: ui.Button):
        await self.cog.handle_collaboration_request(interaction, button, self, self.task_description)

    @ui.button(label="🤔 我有問題", style=ButtonStyle.secondary, custom_id="ask_question_button")
    async def ask_question_button_callback(self, interaction: Interaction, button: ui.Button):
        await self.cog.handle_ask_question(interaction, button, self, self.task_description)


class DevFlow(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("DevFlow cog: Initializing...")
        self.oauth_client_id = os.getenv('GITHUB_OAUTH_CLIENT_ID')
        self.oauth_callback_url = os.getenv('GITHUB_OAUTH_CALLBACK_URL')
        self.repo_owner = os.getenv('GITHUB_REPO_OWNER')
        self.repo_name = os.getenv('GITHUB_REPO_NAME')
        self.dev_announce_channel_id_str = os.getenv('DEV_ANNOUNCE_CHANNEL_ID')
        self.dev_announce_channel = None

        logger.info(f"DevFlow cog init: GITHUB_OAUTH_CLIENT_ID_IS_SET: {bool(self.oauth_client_id)}")
        logger.info(f"DevFlow cog init: GITHUB_REPO_OWNER: '{self.repo_owner}'")
        logger.info(f"DevFlow cog init: GITHUB_REPO_NAME: '{self.repo_name}'")
        logger.info(f"DevFlow cog init: DEV_ANNOUNCE_CHANNEL_ID_STR: '{self.dev_announce_channel_id_str}'")

        self.user_mappings_file = store.USER_MAPPINGS_FILE
        self.pending_oauth_states = {} 
        self.user_mappings = {} 
        self._load_and_process_mappings() 

        self.thread_issue_mappings_file = store.THREAD_MAPPINGS_FILE
        self.thread_issue_mappings = {}
        self._load_thread_mappings()
        
        # For Discord -> GitHub sync, assuming a bot-specific token is available
        self.github_bot_token = os.getenv('GITHUB_BOT_TOKEN')
        if not self.github_bot_token:
            logger.warning("GITHUB_BOT_TOKEN is not set. Sync from Discord to GitHub will be disabled.")
        
        logger.info("DevFlow cog: __init__ complete.")

    def _load_thread_mappings(self):
        """Loads thread-to-issue mappings from the JSON file."""
        logger.info(f"Attempting to load thread mappings from '{self.thread_issue_mappings_file}'")
        if os.path.exists(self.thread_issue_mappings_file):
            try:
                with open(self.thread_issue_mappings_file, 'r', encoding='utf-8') as f:
                    self.thread_issue_mappings = json.load(f)
                logger.info(f"Successfully loaded {len(self.thread_issue_mappings)} thread-issue mappings.")
            except (IOError, json.JSONDecodeError) as e:
                logger.error(f"Error reading thread mappings file '{self.thread_issue_mappings_file}': {e}", exc_info=True)
                self.thread_issue_mappings = {}
        else:
            logger.info(f"Thread mappings file '{self.thread_issue_mappings_file}' not found. Starting with empty mappings.")
            self.thread_issue_mappings = {}

    def _save_thread_mappings(self):
        """Saves the current thread-to-issue mappings to the JSON file."""
        try:
            data_dir = os.path.dirname(self.thread_issue_mappings_file)
            if data_dir and not os.path.exists(data_dir):
                os.makedirs(data_dir)
            with open(self.thread_issue_mappings_file, 'w', encoding='utf-8') as f:
                json.dump(self.thread_issue_mappings, f, indent=4)
            logger.info(f"Thread mappings successfully saved to '{self.thread_issue_mappings_file}'.")
        except IOError as e:
            logger.error(f"Error saving thread mappings file '{self.thread_issue_mappings_file}': {e}", exc_info=True)

    def _add_thread_mapping(self, message_id: str, issue_number: int, repo_full_name: str):
        """Adds a new message_id-to-issue mapping and saves it."""
        self.thread_issue_mappings[message_id] = {"issue_number": issue_number, "repo": repo_full_name}
        self._save_thread_mappings()

    def _run_sync(self, func, *args, **kwargs):
        return self.bot.loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

    #: How long a "done" message stays before it removes itself.
    ACK_SECONDS = 8

    async def _ack(self, interaction: Interaction, text: str):
        """Confirms an action, then clears the confirmation away.

        A slash command has to answer something, but "✅ done" sitting in the
        channel with *只有您能看到這個 · 刪除這則訊息* under it is a permanent
        smudge for a fact that stopped being interesting the moment it was read
        — and the real result is already visible as a thread, an embed or a
        state change. Errors do not go through here: those are worth keeping
        until they are dealt with.
        """
        await interaction.followup.send(text, ephemeral=True)
        await asyncio.sleep(self.ACK_SECONDS)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass  # already gone, or the interaction token expired

    def _load_and_process_mappings(self):
        logger.info(f"Attempting to load and process mappings from '{self.user_mappings_file}'")
        raw_data_from_file = {}
        data_dir = os.path.dirname(self.user_mappings_file)
        if data_dir and not os.path.exists(data_dir):
            try:
                os.makedirs(data_dir)
                logger.info(f"Created data directory: {data_dir}")
            except OSError as e:
                logger.error(f"Failed to create data directory {data_dir}: {e}", exc_info=True)
                return

        if os.path.exists(self.user_mappings_file):
            try:
                with open(self.user_mappings_file, 'r', encoding='utf-8') as f:
                    raw_data_from_file = json.load(f)
                logger.info(f"Successfully read {len(raw_data_from_file)} entries from '{self.user_mappings_file}'.")
            except (IOError, json.JSONDecodeError) as e:
                logger.error(f"Error reading user mappings file '{self.user_mappings_file}': {e}", exc_info=True)
        else:
            logger.info(f"User mappings file '{self.user_mappings_file}' not found. Will be created if mappings are saved.")
            raw_data_from_file = {} 
        
        needs_resave = False
        self.user_mappings.clear() 
        
        processed_keys_for_saving = list(raw_data_from_file.keys())

        for key_in_file in list(raw_data_from_file.keys()): # Iterate over a copy of keys
            data_value = raw_data_from_file[key_in_file]
            # Case 1: The key from the file is a 'state' we are waiting for from a pending OAuth flow.
            if key_in_file in self.pending_oauth_states: 
                discord_id = self.pending_oauth_states.pop(key_in_file)
                if isinstance(data_value, dict) and "access_token" in data_value and "github_username" in data_value:
                    # This is a successful, new login. We map the discord_id to the new data.
                    self.user_mappings[discord_id] = data_value
                    logger.info(f"Processed new login via state '{key_in_file}' for Discord ID '{discord_id}', mapped to GitHub user '{data_value['github_username']}'.")
                    needs_resave = True # Mark that we need to save the cleaned-up mappings.
                else:
                    logger.warning(f"State-keyed entry '{key_in_file}' from file is invalid and will be discarded: {data_value}")
            # Case 2: The key is already a Discord ID, representing a valid, existing mapping.
            elif key_in_file.isdigit() and isinstance(data_value, dict) and "access_token" in data_value: 
                self.user_mappings[key_in_file] = data_value # Keep this valid mapping.
            # Case 3: a key that is neither a pending state nor a Discord id.
            #
            # Kept, not discarded, as long as it carries a token. This used to
            # delete them, which quietly destroyed real logins: a login filed
            # under its OAuth state stops being explainable the moment the bot
            # restarts, because the state lives in memory. The callback files by
            # Discord id now, so this only catches leftovers from before that —
            # and throwing away somebody's authorisation is a much worse outcome
            # than keeping a stray line in a file.
            elif isinstance(data_value, dict) and "access_token" in data_value:
                self.user_mappings[key_in_file] = data_value
                logger.warning(
                    f"Keeping an unclaimed login under key '{key_in_file}' "
                    f"(github: {data_value.get('github_username')}); ask them to run /login again."
                )
            else:
                logger.warning(f"Discarding malformed entry with key '{key_in_file}': {data_value}")
                needs_resave = True

        # After iterating through the entire file, if we processed a new login or discarded old data,
        # we save the now-clean `self.user_mappings` back to the file.
        if needs_resave:
            self._save_mappings_to_file_internal(self.user_mappings)
        
        logger.info(f"Loaded {len(self.user_mappings)} final user mappings into memory.")

    def _save_mappings_to_file_internal(self, mappings_to_save: dict):
        """Internal helper to save the provided dictionary to the JSON file."""
        try:
            data_dir = os.path.dirname(self.user_mappings_file)
            if data_dir and not os.path.exists(data_dir):
                os.makedirs(data_dir)
            with open(self.user_mappings_file, 'w', encoding='utf-8') as f:
                json.dump(mappings_to_save, f, indent=4)
            logger.info(f"Mappings successfully saved to '{self.user_mappings_file}'.")
        except IOError as e:
            logger.error(f"Error saving user mappings file '{self.user_mappings_file}': {e}", exc_info=True)

    def _update_and_save_user_mapping(self, discord_id: str, github_data_to_update: dict):
        """Updates a specific user's mapping and saves."""
        if discord_id not in self.user_mappings:
            self.user_mappings[discord_id] = {}
        
        self.user_mappings[discord_id].update(github_data_to_update)
        self._save_mappings_to_file_internal(self.user_mappings)

    async def _get_user_github_data(self, discord_id_str: str) -> dict | None:
        if discord_id_str not in self.user_mappings:
            logger.info(f"User {discord_id_str} not in current in-memory mappings. Reloading and processing from file.")
            self._load_and_process_mappings() 
        return self.user_mappings.get(discord_id_str)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"DevFlow cog loaded and ready.")
        if self.dev_announce_channel_id_str and self.dev_announce_channel_id_str.isdigit():
            channel_id = int(self.dev_announce_channel_id_str)
            self.dev_announce_channel = self.bot.get_channel(channel_id)
            if not self.dev_announce_channel:
                logger.error(f"DevFlow cog: Could not find dev announce channel ID: {channel_id}")
            else:
                logger.info(f"DevFlow cog: Dev announce channel '{self.dev_announce_channel.name}' set.")
        else:
            logger.error(f"DevFlow cog: DEV_ANNOUNCE_CHANNEL_ID ('{self.dev_announce_channel_id_str}') is not set or invalid.")

    @app_commands.command(name="login", description="授權 Bot 代表您訪問 GitHub。")
    async def github_login(self, interaction: Interaction):
        if not self.oauth_client_id or not self.oauth_callback_url:
            await interaction.response.send_message("❌ GitHub OAuth 設定不完整。請聯絡管理員。", ephemeral=True)
            return
        state = str(uuid.uuid4())
        self.pending_oauth_states[state] = str(interaction.user.id) 
        # Added "repo" scope to list repositories
        scopes = "repo read:user gist" 
        auth_url = (f"https://github.com/login/oauth/authorize?"
                    f"client_id={self.oauth_client_id}&"
                    f"redirect_uri={self.oauth_callback_url}&"
                    f"scope={scopes.replace(' ', '%20')}&"
                    f"state={state}")
        embed = Embed(
            title="🔗 GitHub 授權", 
            description=(
                "請點擊下方連結授權 Bot 訪問您的 GitHub 帳號，以便能：\n"
                "- **讀取儲存庫列表** (用於在指令中顯示選項)\n"
                "- **以您的名義建立 Issue**\n"
                "- **為您指派任務**\n"
                "- **將對話紀錄歸檔為私密 Gist**\n\n"
                f"**重要：** 您的後端回呼 API (`{self.oauth_callback_url}`) 在成功處理授權後，應負責將包含 `state`、`access_token` 和 `github_username` 的記錄寫入 `{self.user_mappings_file}` 供 Bot 讀取。"
            ),
            color=discord.Color.blue()
        )
        view_auth = ui.View()
        view_auth.add_item(ui.Button(label="前往 GitHub 授權", url=auth_url))
        await interaction.response.send_message(embed=embed, view=view_auth, ephemeral=True)
        logger.info(f"User {interaction.user} (ID: {interaction.user.id}) initiated OAuth with state: {state} and scopes: '{scopes}'")

    @app_commands.command(name="setmygithub", description="[備用] 手動設定您的 GitHub 用戶名 (OAuth登入是首選)。")
    @app_commands.describe(username="您的 GitHub 用戶名。")
    async def set_my_github_username(self, interaction: Interaction, username: str):
        discord_id_str = str(interaction.user.id)
        cleaned_username = username.strip()
        if not cleaned_username:
            await interaction.response.send_message("❌ GitHub 用戶名不能為空。", ephemeral=True)
            return
        user_data_to_update = {"github_username": cleaned_username}
        self._update_and_save_user_mapping(discord_id_str, user_data_to_update)
        logger.info(f"User {interaction.user} (ID: {discord_id_str}) manually set GitHub username to: {cleaned_username}")
        await interaction.response.send_message(f"✅ GitHub 用戶名已手動設為：`{cleaned_username}`。要啟用以您名義發布Issue等功能，請使用 `/login`。", ephemeral=True)

    async def repo_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        discord_id_str = str(interaction.user.id)
        user_gh_data = await self._get_user_github_data(discord_id_str)
        
        if not user_gh_data or "access_token" not in user_gh_data:
            return [app_commands.Choice(name="⚠️ 請先使用 /login 授權", value="")]

        token = user_gh_data["access_token"]
        try:
            gh = await self._run_sync(Github, token)
            user = await self._run_sync(gh.get_user)
            
            # Fetch user's own repos and repos from organizations
            all_repos = await self._run_sync(user.get_repos, affiliation='owner,organization_member')
            
            choices = []
            for repo in all_repos:
                if current.lower() in repo.full_name.lower():
                    choices.append(app_commands.Choice(name=repo.full_name, value=repo.full_name))
                if len(choices) >= 25:  # Discord's limit for choices
                    break
            return choices
        except Exception as e:
            logger.error(f"Repo autocomplete failed for user {discord_id_str}: {e}", exc_info=True)
            return [app_commands.Choice(name="❌ 無法獲取儲存庫列表", value="")]

    @app_commands.command(name="issue", description="宣告一個新的開發任務。")
    @app_commands.describe(
        task="任務的詳細描述。",
        repo="要建立 Issue 的儲存庫，留空則使用預設儲存庫。",
        labels="標籤，逗號分隔，例如 bug,enhancement。",
        milestone="里程碑的標題。",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def start_dev(self, interaction: Interaction, task: str, repo: str = None,
                        labels: str = None, milestone: str = None):
        if not self.dev_announce_channel:
            if self.dev_announce_channel_id_str and self.dev_announce_channel_id_str.isdigit():
                channel_id = int(self.dev_announce_channel_id_str)
                refetched_channel = self.bot.get_channel(channel_id)
                if refetched_channel: self.dev_announce_channel = refetched_channel
                else:
                    logger.error(f"start-dev: Failed to re-fetch channel ID: {channel_id}.")
                    await interaction.response.send_message(f"錯誤：無法找到開發公告頻道 ID: {channel_id}。", ephemeral=True)
                    return
            else:
                logger.error(f"start-dev: DEV_ANNOUNCE_CHANNEL_ID not correctly set.")
                await interaction.response.send_message("錯誤：開發公告頻道 ID 未設定。", ephemeral=True)
                return
        if not self.dev_announce_channel: 
            await interaction.response.send_message("錯誤：開發公告頻道最終未能設定。", ephemeral=True)
            return
        await interaction.response.send_message("處理中...", ephemeral=True)
        embed = Embed(title="🚀 新開發任務已啟動！", color=discord.Color.blue())
        embed.add_field(name="🧑‍💻 開發者", value=interaction.user.display_name, inline=False)
        embed.add_field(name="📝 任務內容", value=task, inline=False)
        
        # Determine the target repo and display it
        target_repo_str = repo if repo else f"{self.repo_owner}/{self.repo_name}"
        if target_repo_str:
             embed.add_field(name="🎯 目標儲存庫", value=f"`{target_repo_str}`", inline=False)
        
        embed.add_field(name="📊 狀態", value="🟢 開發中", inline=False)
        embed.add_field(name="⏱️ 開始時間", value=f"<t:{int(datetime.datetime.now(datetime.timezone.utc).timestamp())}:F>", inline=False)
        embed.set_footer(text=f"任務發起人 ID: {interaction.user.id}")
        
        view_buttons = DevTaskView(cog_instance=self, original_interaction=interaction,
                                   task_description=task, repo=repo,
                                   labels=labels, milestone=milestone)
        try:
            await self.dev_announce_channel.send(embed=embed, view=view_buttons)
            await interaction.followup.send(f"✅ 開發任務公告已發布於 {self.dev_announce_channel.mention}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in start_dev sending message: {e}", exc_info=True)
            await interaction.followup.send(f"錯誤：發布公告時發生問題。", ephemeral=True)

    async def handle_create_github_issue(self, interaction: Interaction, button: ui.Button, view: DevTaskView, task_description: str, repo_override: str = None):
        if interaction.user.id != view.original_interaction.user.id:
            await interaction.response.send_message("抱歉，只有任務發起者才能建立 GitHub Issue。", ephemeral=True)
            return
        
        discord_id_str = str(view.original_interaction.user.id)
        user_gh_data = await self._get_user_github_data(discord_id_str)
        if not user_gh_data or "access_token" not in user_gh_data:
            await interaction.response.send_message("❌ 您尚未透過 `/login` 授權或授權資料不完整。請先完成授權。", ephemeral=True)
            return
        
        user_token = user_gh_data["access_token"]
        github_username = user_gh_data.get("github_username")
        await interaction.response.defer(ephemeral=True)

        # Determine the target repository path
        repo_path = repo_override if repo_override else f"{self.repo_owner}/{self.repo_name}"
        if not repo_path or '/' not in repo_path:
            await interaction.followup.send(f"❌ 無法確定目標儲存庫。請檢查您的指令或伺服器設定 (預設: {self.repo_owner}/{self.repo_name})。", ephemeral=True)
            return

        try:
            gh = await self._run_sync(Github, user_token)
            repo = await self._run_sync(gh.get_repo, repo_path)
        except GithubException as e:
            logger.error(f"Failed to get repo '{repo_path}' with user token: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 無法存取倉庫 `{repo_path}`。請檢查倉庫是否存在、您是否有權限，或重新執行 `/login`。\n錯誤: {e.status} - {e.data.get('message', 'Unknown error')}", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Failed to init Github or get repo '{repo_path}' with user token: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 無法使用您的 GitHub 憑證初始化或存取倉庫 `{repo_path}`。請重新執行 `/login` 或檢查 Token 權限。", ephemeral=True)
            return
        issue_title = task_description[:50]
        # The marker matters here as well as on comments: GitHub reports this
        # issue back through the `issues.opened` webhook a moment later, and
        # without it the bot would announce in Discord a task that already has
        # an announcement — the one the button was pressed on.
        issue_body = (
            f"{task_description}\n\n---\n"
            f"*此 Issue 由 Discord 使用者 {view.original_interaction.user.display_name} "
            f"(ID: {view.original_interaction.user.id}) 透過 Bot 自動建立。*\n\n"
            f"{store.SYNC_MARKER}"
        )
        assignees = [github_username] if github_username else []

        # Labels and a milestone, when `/issue` was given them. Both are
        # looked up rather than passed through: GitHub refuses the whole
        # `create_issue` call if a label does not exist, so a typo would lose
        # the issue entirely instead of just the label.
        extra = {}
        wanted = [name.strip() for name in (view.labels or "").split(",") if name.strip()]
        if wanted:
            try:
                available = {label.name.lower(): label.name for label in await self._run_sync(repo.get_labels)}
                matched = [available[name.lower()] for name in wanted if name.lower() in available]
                missing = [name for name in wanted if name.lower() not in available]
                if matched:
                    extra["labels"] = matched
                if missing:
                    logger.info("ignoring labels that do not exist on %s: %s", repo.full_name, missing)
            except GithubException as error:
                logger.warning("could not read labels for %s: %s", repo.full_name, error)

        if view.milestone:
            try:
                for candidate in await self._run_sync(repo.get_milestones, state="all"):
                    if candidate.title.lower() == view.milestone.strip().lower():
                        extra["milestone"] = candidate
                        break
                else:
                    logger.info("no milestone called %r on %s", view.milestone, repo.full_name)
            except GithubException as error:
                logger.warning("could not read milestones for %s: %s", repo.full_name, error)

        try:
            created_issue = await self._run_sync(
                repo.create_issue, title=issue_title, body=issue_body, assignees=assignees, **extra
            )
            view.github_issue_url = created_issue.html_url
            view.github_issue_number = created_issue.number
            original_embed = interaction.message.embeds[0].copy()
            field_updated = False
            for i, field in enumerate(original_embed.fields):
                if field.name == "🔗 GitHub Issue":
                    original_embed.set_field_at(i, name="🔗 GitHub Issue", value=f"[#{created_issue.number} 已建立]({created_issue.html_url})", inline=False)
                    field_updated = True
                    break
            if not field_updated:
                original_embed.add_field(name="🔗 GitHub Issue", value=f"[#{created_issue.number} 已建立]({created_issue.html_url})", inline=False)
            button.label = "✅ Issue 已建立"
            button.style = ButtonStyle.success
            button.disabled = True
            await interaction.message.edit(embed=original_embed, view=view)
            
            # Add the mapping between the original message and the new issue
            self._add_thread_mapping(str(interaction.message.id), created_issue.number, repo.full_name)

            # The thread may already exist — somebody pressed 我有問題 and the
            # conversation started before anyone got round to opening an issue.
            # Everything said in that window used to be dropped, because the
            # sync listener has nothing to sync to until this mapping exists.
            backfilled = await self._backfill_thread(interaction.message, repo, created_issue)

            assign_msg = f"並已嘗試將您 (`{github_username}`) 指派。" if github_username else ""
            if backfilled:
                assign_msg += f" 討論串裡先前的 {backfilled} 則訊息也補上去了。"
            await self._ack(interaction, f"✅ 成功在 GitHub 建立 Issue #{created_issue.number}！ {assign_msg}")
        except Exception as e:
            logger.error(f"Failed to create/assign GitHub Issue for {view.original_interaction.user}: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 建立或指派 GitHub Issue 時發生錯誤: {e}", ephemeral=True)

    #: Discord emoji to the eight reactions GitHub understands.
    #:
    #: GitHub takes no others, so anything not listed here is simply left in
    #: Discord — silently, because reacting with 🍕 is not an error worth
    #: telling somebody about.
    REACTIONS = {
        "👍": "+1", "👎": "-1", "😄": "laugh", "😆": "laugh",
        "😕": "confused", "❤️": "heart", "❤": "heart",
        "🎉": "hooray", "🚀": "rocket", "👀": "eyes",
    }

    async def _github_for_reaction(self, payload):
        """The GitHub comment and reaction name a Discord reaction refers to.

        `None` when there is nothing to do: an unmapped emoji, a message that
        was never synced, or a reaction the bot itself added (it marks synced
        messages with ✅, which is not something to mirror).
        """
        name = str(payload.emoji)
        content = self.REACTIONS.get(name)
        if not content:
            return None

        recorded = store.comment_for_message(payload.message_id)
        if not recorded:
            return None

        data = await self._get_user_github_data(str(payload.user_id))
        token = (data or {}).get("access_token") or self.github_bot_token
        if not token:
            return None

        try:
            gh = await self._run_sync(Github, token)
            repo = await self._run_sync(gh.get_repo, recorded["repo"])
            # Through the issue: `Repository` has no way to fetch a single
            # issue comment by id — `get_comment` there is for commit comments.
            issue = await self._run_sync(repo.get_issue, number=recorded["issue_number"])
            comment = await self._run_sync(issue.get_comment, recorded["comment_id"])
        except GithubException as error:
            logger.warning("cannot read comment %s: %s", recorded["comment_id"], error)
            return None
        return comment, content

    async def _github_issue_for_thread_id(self, thread_id: int):
        """The PyGithub issue a thread mirrors, using the bot's own token."""
        mapping = self.thread_issue_mappings.get(str(thread_id))
        if not mapping or not self.github_bot_token:
            return None
        try:
            gh = await self._run_sync(Github, self.github_bot_token)
            repo = await self._run_sync(gh.get_repo, mapping["repo"])
            return await self._run_sync(repo.get_issue, number=mapping["issue_number"])
        except GithubException as error:
            logger.warning("cannot read %s#%s: %s", mapping["repo"], mapping["issue_number"], error)
            return None

    @commands.Cog.listener()
    async def on_raw_thread_update(self, payload):
        """Mirrors a forum post's tags back onto the issue's labels.

        The other half of the label sync. Both directions settle after one hop
        because each side checks whether the change it is about to make has
        already been made — see the webhook's label handler for why that is the
        guard rather than remembering what we just did.
        """
        thread = self.bot.get_channel(payload.thread_id)
        if thread is None or not isinstance(getattr(thread, "parent", None), discord.ForumChannel):
            return

        issue = await self._github_issue_for_thread_id(payload.thread_id)
        if issue is None:
            return

        # Tag name -> GitHub label, via the alias table read backwards. A tag
        # with no counterpart is left alone rather than invented on GitHub:
        # creating labels from Discord would let a typo permanently add one to
        # the repository.
        reverse = {value.lower(): key for key, value in server.TAG_ALIASES.items()}
        existing = {label.name.lower(): label.name for label in await self._run_sync(issue.get_labels)}
        try:
            available = {
                label.name.lower(): label.name
                for label in await self._run_sync(issue.repository.get_labels)
            }
        except GithubException:
            return

        wanted = []
        for tag in thread.applied_tags:
            for candidate in (tag.name, reverse.get(tag.name.lower(), "")):
                if candidate and candidate.lower() in available:
                    wanted.append(available[candidate.lower()])
                    break

        if {name.lower() for name in wanted} == set(existing):
            return

        try:
            await self._run_sync(issue.set_labels, *wanted)
            logger.info("labels for #%s -> %s", issue.number, wanted)
        except GithubException as error:
            logger.warning("could not set labels on #%s: %s", issue.number, error)

    async def _delete_github_comment(self, message_id: int):
        """Removes the GitHub comment a deleted Discord message became."""
        recorded = store.comment_for_message(message_id)
        if not recorded:
            # Only synced messages have a counterpart. Said out loud because
            # otherwise "I deleted it and nothing happened" is indistinguishable
            # from a broken listener — and the usual cause is that the message
            # predates the mapping being kept at all.
            logger.debug("message %s has no GitHub comment recorded", message_id)
            return
        if not self.github_bot_token:
            logger.warning("no GITHUB_BOT_TOKEN; cannot delete comment %s", recorded["comment_id"])
            return
        try:
            gh = await self._run_sync(Github, self.github_bot_token)
            repo = await self._run_sync(gh.get_repo, recorded["repo"])
            issue = await self._run_sync(repo.get_issue, number=recorded["issue_number"])
            comment = await self._run_sync(issue.get_comment, recorded["comment_id"])
            await self._run_sync(comment.delete)
            logger.info("deleted GitHub comment %s", recorded["comment_id"])
        except GithubException as error:
            # 404 is the ordinary case: it was already deleted on GitHub, and
            # that is what told us to delete this side.
            if error.status != 404:
                logger.warning("could not delete comment %s: %s", recorded["comment_id"], error)
        finally:
            store.forget_message(message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        await self._delete_github_comment(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        """Clearing a stretch of a thread at once.

        Serially, with a pause between each: GitHub's secondary rate limit is
        specifically about bursts of writes, and firing forty deletions
        concurrently gets the whole batch throttled — after some of them have
        already gone through, which is the worst place to stop.
        """
        for message_id in payload.message_ids:
            if store.comment_for_message(message_id) is None:
                continue
            await self._delete_github_comment(message_id)
            await asyncio.sleep(1)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.bot.user.id:
            return
        found = await self._github_for_reaction(payload)
        if not found:
            return
        comment, content = found
        try:
            await self._run_sync(comment.create_reaction, content)
            logger.info("mirrored %s onto comment %s", content, comment.id)
        except GithubException as error:
            logger.warning("could not add reaction %s: %s", content, error)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        found = await self._github_for_reaction(payload)
        if not found:
            return
        comment, content = found

        # GitHub deletes reactions by their own id, not by name, so the list has
        # to be walked to find the one this account left.
        data = await self._get_user_github_data(str(payload.user_id))
        whoami = (data or {}).get("github_username")
        try:
            for reaction in await self._run_sync(comment.get_reactions):
                if reaction.content != content:
                    continue
                if whoami and reaction.user and reaction.user.login != whoami:
                    continue
                await self._run_sync(reaction.delete)
                logger.info("removed %s from comment %s", content, comment.id)
                break
        except GithubException as error:
            logger.warning("could not remove reaction %s: %s", content, error)

    async def _render_for_github(self, message: discord.Message) -> str:
        """A Discord message as it should read on GitHub.

        `clean_content` turns `<@123>` into the person's Discord nickname, which
        on GitHub is just a word — it notifies nobody, and a nickname full of
        decorative characters does not even identify them. Anyone who has run
        `/login` is substituted for their GitHub handle instead, so the
        mention actually reaches them.
        """
        content = message.content or ""

        for user in message.mentions:
            data = await self._get_user_github_data(str(user.id))
            handle = (data or {}).get("github_username")
            replacement = f"@{handle}" if handle else f"@{user.display_name}"
            content = re.sub(rf"<@!?{user.id}>", replacement, content)

        # Whatever is left of Discord's own syntax would otherwise show up as
        # raw ids on GitHub.
        for role in message.role_mentions:
            content = content.replace(f"<@&{role.id}>", f"@{role.name}")
        for channel in message.channel_mentions:
            # A channel mention that happens to be a mirrored thread is a
            # reference to that issue, and saying so gets GitHub to record the
            # link on both issues' timelines. Everything else is just a channel.
            mapped = store.issue_for_thread(channel.id)
            if mapped:
                content = content.replace(f"<#{channel.id}>", _reference(mapped, message))
            else:
                content = content.replace(f"<#{channel.id}>", f"#{channel.name}")

        return _DISCORD_LINK.sub(lambda m: _thread_url_as_reference(m, message), content)

    async def _thread_exists(self, thread_id: str) -> bool:
        """Whether a recorded thread is still there.

        A mapping outlives the thread it points at, and a stale one is worse
        than none: it makes the bot refuse to relink an issue while pointing at
        a channel nobody can open.
        """
        if self.bot.get_channel(int(thread_id)) is not None:
            return True
        try:
            await self.bot.fetch_channel(int(thread_id))
        except (discord.NotFound, discord.Forbidden):
            return False
        except discord.HTTPException:
            # Transient — better to keep the mapping than to drop a live thread
            # because Discord hiccuped.
            return True
        return True

    async def _issue_from_thread(self, interaction: Interaction):
        """The GitHub issue this thread mirrors, or `None` after explaining why not.

        Shared by the commands that act on an existing task, so they all refuse
        in the same words instead of each inventing their own.
        """
        mapping = self.thread_issue_mappings.get(str(interaction.channel.id))
        if not mapping:
            await interaction.followup.send(
                "❌ 這個討論串沒有對應的 GitHub Issue。\n"
                "如果那張單是直接在 GitHub 上開的,先用 `/link` 把它接進來。",
                ephemeral=True,
            )
            return None, None

        data = await self._get_user_github_data(str(interaction.user.id))
        token = (data or {}).get("access_token") or self.github_bot_token
        if not token:
            await interaction.followup.send(
                "❌ 沒有可用的 GitHub 憑證。請先 `/login`。", ephemeral=True
            )
            return None, None

        try:
            gh = await self._run_sync(Github, token)
            repo = await self._run_sync(gh.get_repo, mapping["repo"])
            issue = await self._run_sync(repo.get_issue, number=mapping["issue_number"])
        except GithubException as e:
            await interaction.followup.send(
                f"❌ 讀不到 `{mapping['repo']}#{mapping['issue_number']}`：{e.data.get('message', e)}",
                ephemeral=True,
            )
            return None, None
        return issue, mapping

    @app_commands.command(name="link", description="把 GitHub 上已經存在的 Issue 接進 Discord。")
    @app_commands.describe(
        number="Issue 編號。",
        repo="儲存庫，格式 owner/name，留空用預設。",
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def link_issue(self, interaction: Interaction, number: int, repo: str = None):
        """For issues that were filed on GitHub rather than through `/issue`.

        Deliberately on demand rather than a bulk import: a backlog of
        twenty-odd issues would become twenty-odd announcements and twenty-odd
        threads nobody is reading. The GitHub list is the tracker; a Discord
        thread is for the one you are actually talking about.
        """
        await interaction.response.defer(ephemeral=True)

        repo_path = repo or f"{self.repo_owner}/{self.repo_name}"
        data = await self._get_user_github_data(str(interaction.user.id))
        token = (data or {}).get("access_token") or self.github_bot_token
        if not token:
            await interaction.followup.send("❌ 沒有可用的 GitHub 憑證。請先 `/login`。", ephemeral=True)
            return

        try:
            gh = await self._run_sync(Github, token)
            gh_repo = await self._run_sync(gh.get_repo, repo_path)
            issue = await self._run_sync(gh_repo.get_issue, number=number)
        except GithubException as e:
            await interaction.followup.send(
                f"❌ 讀不到 `{repo_path}#{number}`：{e.data.get('message', e)}", ephemeral=True
            )
            return

        existing = store.thread_for_issue(repo_path, number)
        if existing:
            # Only if the thread is still there. A mapping outlives the thread —
            # delete the thread, or delete and recreate the issue, and this used
            # to answer with a link to nothing (`#不明`) and refuse to relink,
            # with no way out short of editing the file by hand.
            if await self._thread_exists(existing):
                await self._ack(interaction, f"ℹ️ `{repo_path}#{number}` 已經有討論串了：<#{existing}>")
                return

            mappings = store.read_threads()
            mappings.pop(existing, None)
            store.write_threads(mappings)
            self.thread_issue_mappings.pop(existing, None)
            logger.info("dropped the mapping for thread %s; it no longer exists", existing)

        if not self.dev_announce_channel:
            await interaction.followup.send("❌ 公告頻道沒設定好。", ephemeral=True)
            return

        # The same shape the webhook hands over, so both routes produce an
        # identical card.
        payload = {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body or "",
            "html_url": issue.html_url,
            "user": {"login": issue.user.login if issue.user else "unknown"},
            "labels": [{"name": label.name} for label in issue.labels],
        }
        # The conversation so far, so the thread does not open empty on an issue
        # that has been discussed for a week. Capped because a long-running
        # issue would otherwise post fifty embeds in a row.
        try:
            existing = await self._run_sync(lambda: list(issue.get_comments())[-20:])
        except GithubException:
            existing = []
        history = [
            {"author": c.user.login if c.user else "unknown", "body": c.body or ""}
            for c in existing
            if store.SYNC_MARKER not in (c.body or "")
        ]

        thread = await server.announce_issue(
            self.bot, self.dev_announce_channel, repo_path, payload, history
        )
        if thread is None:
            await interaction.followup.send("❌ 建立討論串失敗，看一下 bot 的 log。", ephemeral=True)
            return
        await self._ack(interaction, f"✅ `{repo_path}#{number}` 接進來了：<#{thread.id}>")

    @app_commands.command(name="resync", description="[管理者] 把儲存庫裡所有開著的 Issue 都接進來。")
    @app_commands.describe(
        repo="儲存庫，格式 owner/name，留空用預設。",
        limit="最多處理幾張，預設 50。",
    )
    async def resync(self, interaction: Interaction, repo: str = None, limit: int = 50):
        """Bulk-links every open issue that does not already have a thread.

        Owner-only, and skips anything already mapped, so running it twice is
        safe. It does *not* delete anything: posts left behind by a previous
        integration are not this bot's to remove, and a command that quietly
        clears a channel is not one to hand out.
        """
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("權限不足。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        repo_path = repo or f"{self.repo_owner}/{self.repo_name}"
        data = await self._get_user_github_data(str(interaction.user.id))
        token = (data or {}).get("access_token") or self.github_bot_token
        if not token:
            await interaction.followup.send("❌ 沒有可用的 GitHub 憑證。", ephemeral=True)
            return
        if not self.dev_announce_channel:
            await interaction.followup.send("❌ 公告頻道沒設定好。", ephemeral=True)
            return

        try:
            gh = await self._run_sync(Github, token)
            gh_repo = await self._run_sync(gh.get_repo, repo_path)
            issues = await self._run_sync(lambda: list(gh_repo.get_issues(state="open"))[:limit])
        except GithubException as e:
            await interaction.followup.send(
                f"❌ 讀不到 `{repo_path}`：{e.data.get('message', e)}", ephemeral=True
            )
            return

        created, skipped, failed = 0, 0, 0
        for issue in issues:
            # `get_issues` returns pull requests too — they are issues as far as
            # the API is concerned, and they get their own announcement when
            # their webhook arrives.
            if issue.pull_request is not None:
                continue

            mapped = store.thread_for_issue(repo_path, issue.number)
            if mapped:
                # Unless the thread is gone. Without this, deleting the posts to
                # start over leaves the mappings behind and every issue is
                # "skipped" — the import quietly does nothing and there is no
                # way to redo it short of editing the file.
                if await self._thread_exists(mapped):
                    skipped += 1
                    continue
                mappings = store.read_threads()
                mappings.pop(mapped, None)
                store.write_threads(mappings)
                self.thread_issue_mappings.pop(mapped, None)

            payload = {
                "number": issue.number,
                "title": issue.title,
                "body": issue.body or "",
                "html_url": issue.html_url,
                "user": {"login": issue.user.login if issue.user else "unknown"},
                "labels": [{"name": label.name} for label in issue.labels],
            }
            # The conversation so far, same as `/link`. A thread that opens
            # empty on an issue with eight comments on it is not a mirror of
            # anything — you still have to go and read GitHub.
            history = []
            if issue.comments:
                try:
                    recent = await self._run_sync(lambda: list(issue.get_comments())[-20:])
                    history = [
                        {"author": c.user.login if c.user else "unknown", "body": c.body or ""}
                        for c in recent
                        if store.SYNC_MARKER not in (c.body or "")
                    ]
                except GithubException as error:
                    logger.warning("could not read comments on #%s: %s", issue.number, error)

            thread = await server.announce_issue(
                self.bot, self.dev_announce_channel, repo_path, payload, history
            )
            if thread is None:
                failed += 1
                continue
            created += 1
            # Discord allows a handful of thread creations per few seconds; a
            # burst of fifty without this gets rate-limited into a crawl and
            # discord.py sleeps through it in the middle of the loop.
            await asyncio.sleep(2)

        await interaction.followup.send(
            f"✅ `{repo_path}` 同步完成：新建 **{created}**、已存在跳過 **{skipped}**"
            + (f"、失敗 **{failed}**" if failed else "")
            + "\n\n舊的貼文不會被動到 —— 那是別的整合留下的,要清掉請自己刪。",
            ephemeral=True,
        )

    @app_commands.command(name="reopen", description="重新開啟這個討論串對應的 Issue。")
    @app_commands.describe(comment="重開的理由，會變成 Issue 上的一則留言。留空就只是重開。")
    async def reopen_dev(self, interaction: Interaction, comment: str = None):
        """The counterpart to `/close`.

        Only reopens the issue on GitHub. Unarchiving the thread and putting the
        announcement back to "開發中" happen when the `issues.reopened` webhook
        comes back, which is the same path a reopen done on the GitHub website
        takes — one implementation, so the two cannot drift apart.
        """
        await interaction.response.defer(ephemeral=True)

        issue, mapping = await self._issue_from_thread(interaction)
        if issue is None:
            return

        if issue.state == "open":
            await self._ack(interaction, f"ℹ️ `{mapping['repo']}#{issue.number}` 本來就是開著的。")
            return

        try:
            # The reason first, so it is above the state change in the issue's
            # timeline rather than under it.
            if comment:
                await self._run_sync(
                    issue.create_comment,
                    f"{comment}\n\n*— {interaction.user.display_name}，於 Discord 重新開啟此任務時留下*"
                    f"\n\n{store.SYNC_MARKER}",
                )
            await self._run_sync(issue.edit, state="open")
        except GithubException as e:
            await interaction.followup.send(
                f"❌ 重新開啟失敗：{e.data.get('message', e)}", ephemeral=True
            )
            return

        issue_url = f"https://github.com/{mapping['repo']}/issues/{issue.number}"
        await interaction.followup.send(
            f"🔓 [{mapping['repo']}#{issue.number}]({issue_url}) 已重新開啟。\n"
            "討論串和公告會在 GitHub 的通知回來時一起更新。",
            ephemeral=True,
        )
        # In the thread, not just to whoever typed: the slash command's own
        # reply is ephemeral, so without this nobody else sees why the task came
        # back — which is the whole point of writing a reason.
        if comment:
            try:
                await interaction.channel.send(
                    f"🔓 **{interaction.user.display_name}** 重新開啟了這張單：\n>>> {comment}"
                )
            except Exception as error:  # noqa: BLE001
                logger.warning("could not echo the reopen reason into the thread: %s", error)

    async def _backfill_thread(self, announcement: discord.Message, repo, issue) -> int:
        """Copies anything already said in the thread onto the freshly made issue.

        Posted as one comment rather than one per message: a dozen separate
        comments would bury the issue, and the ordering is what matters here,
        not who gets attributed on GitHub — the names are in the text.

        Returns how many messages were carried over.
        """
        thread = getattr(announcement, "thread", None)
        if thread is None:
            return 0

        try:
            history = [message async for message in thread.history(limit=200, oldest_first=True)]
        except Exception as error:  # noqa: BLE001
            logger.warning("could not read thread %s for backfill: %s", thread.id, error)
            return 0

        lines = []
        for message in history:
            if message.author.bot or not (message.clean_content or message.attachments):
                continue
            lines.append(f"**{message.author.display_name}：** {await self._render_for_github(message)}")
            for attachment in message.attachments:
                lines.append(f"- [{attachment.filename}]({attachment.url})")

        if not lines:
            return 0

        body = (
            "📜 **開 Issue 之前討論串裡已經說過的話**\n\n"
            + "\n".join(lines)
            + f"\n\n{store.SYNC_MARKER}"
        )
        try:
            await self._run_sync(issue.create_comment, body[:65000])
        except Exception as error:  # noqa: BLE001
            logger.warning("could not backfill %s#%s: %s", repo.full_name, issue.number, error)
            return 0
        logger.info("backfilled %d messages onto %s#%s", len(lines), repo.full_name, issue.number)
        return len(lines)

    async def handle_collaboration_request(self, interaction: Interaction, button: ui.Button, dev_task_view: DevTaskView, task_description: str):
        original_task_author = dev_task_view.original_interaction.user
        original_author_display_name = original_task_author.display_name
        original_task_author_mention = original_task_author.mention
        task_title_preview = task_description[:30]
        thread_name = f"關於「{task_title_preview}...」的協作"
        assign_message = ""
        
        mapping_info = self.thread_issue_mappings.get(str(interaction.message.id))
        
        if dev_task_view.github_issue_number and mapping_info and mapping_info.get("repo"):
            repo_full_name = mapping_info.get("repo")
            collaborator_discord_id_str = str(interaction.user.id)
            collaborator_gh_data = await self._get_user_github_data(collaborator_discord_id_str)

            if collaborator_gh_data and collaborator_gh_data.get("access_token") and collaborator_gh_data.get("github_username"):
                collaborator_token = collaborator_gh_data["access_token"]
                collaborator_username = collaborator_gh_data["github_username"]
                try:
                    gh_collab = await self._run_sync(Github, collaborator_token)
                    repo_collab = await self._run_sync(gh_collab.get_repo, repo_full_name)
                    gh_issue = await self._run_sync(repo_collab.get_issue, number=dev_task_view.github_issue_number)
                    current_assignees_logins = await self._run_sync(lambda: [a.login for a in gh_issue.assignees])
                    if collaborator_username not in current_assignees_logins:
                        await self._run_sync(gh_issue.add_to_assignees, collaborator_username)
                        assign_message = f"\n✅ 已將您 (`{collaborator_username}`) 指派給 GitHub Issue #{dev_task_view.github_issue_number}。"
                        logger.info(f"User {interaction.user} (GitHub: {collaborator_username}) assigned to Issue #{dev_task_view.github_issue_number} in repo {repo_full_name}.")
                    else:
                        assign_message = f"\nℹ️ 您 (`{collaborator_username}`) 已是該 Issue 的指派者。"
                except Exception as e:
                    assign_message = f"\n❌ 嘗試指派您到 GitHub Issue 時發生錯誤。"
                    logger.error(f"Error assigning {collaborator_username} to Issue #{dev_task_view.github_issue_number} in repo {repo_full_name}: {e}", exc_info=True)
            else:
                assign_message = f"\n⚠️ 您尚未透過 `/login` 授權或您的 GitHub 用戶名未儲存。請先授權後再試。"
        else:
            assign_message = "\nℹ️ GitHub Issue 尚未建立，無法指派。"
        try: 
            if interaction.message.thread:
                thread = interaction.message.thread
                await interaction.response.send_message(f"此任務已有討論串：{thread.mention}。{assign_message}", ephemeral=True)
            else:
                await interaction.response.defer(ephemeral=True)
                thread = await interaction.message.create_thread(name=thread_name, auto_archive_duration=1440)
                await thread.send(f"👋 {interaction.user.mention} 想協作 **{original_author_display_name}** 的任務「{task_description[:50]}...」。\n{original_task_author_mention}{assign_message}")
                await interaction.followup.send(f"已為此任務建立協作討論串：{thread.mention}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error creating/sending to collaboration thread: {e}", exc_info=True)
            await interaction.followup.send(f"建立討論串時出錯。{assign_message}", ephemeral=True)
            
    async def handle_ask_question(self, interaction: Interaction, button: ui.Button, dev_task_view: DevTaskView, task_description: str):
        if not interaction.message:
            await interaction.response.send_message("錯誤：找不到原始訊息。", ephemeral=True)
            return
        original_task_author = dev_task_view.original_interaction.user
        original_author_display_name = original_task_author.display_name
        original_task_author_mention = original_task_author.mention
        task_title_preview = task_description[:30]
        thread_name = f"關於「{task_title_preview}...」的提問"
        try:
            if interaction.message.thread:
                thread = interaction.message.thread
                await interaction.response.send_message(f"此任務已有討論串：{thread.mention}，請直接加入提問。", ephemeral=True)
            else:
                await interaction.response.defer(ephemeral=True)
                thread = await interaction.message.create_thread(name=thread_name, auto_archive_duration=1440)
                await thread.send(f"❓ {interaction.user.mention} 對 **{original_author_display_name}** 的任務「{task_description[:50]}...」有疑問。\n請在此討論。 {original_task_author_mention}")
                await interaction.followup.send(f"已為此任務建立提問討論串：{thread.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("錯誤：我沒有權限在此訊息下建立討論串。", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"錯誤：建立討論串時發生問題：{e}", ephemeral=True)
            logger.error(f"Error creating question thread: {e}", exc_info=True)
            
    # --- `finish-dev` and its helper methods ---

    async def _get_original_message(self, interaction: Interaction) -> discord.Message | None:
        """Finds the original task announcement message from a thread or reply."""
        # Case 1: Command is used in a thread within the dev announce channel.
        if self.dev_announce_channel and isinstance(interaction.channel, discord.Thread) and interaction.channel.parent_id == self.dev_announce_channel.id:
            try:
                return await self.dev_announce_channel.fetch_message(interaction.channel.id)
            except discord.NotFound:
                logger.warning(f"finish-dev: Original message for thread {interaction.channel.id} not found in dev_announce_channel.")
            except Exception as e:
                logger.error(f"Error fetching original message in finish_dev (thread context): {e}", exc_info=True)

        # Case 2: Command is a reply to the bot's announcement message.
        if interaction.message and interaction.message.reference and interaction.message.reference.message_id:
            try:
                ref_msg_id = interaction.message.reference.message_id
                # Try fetching from current channel, then dev_announce_channel if different.
                try:
                    referenced_message = await interaction.channel.fetch_message(ref_msg_id)
                except discord.NotFound:
                    if self.dev_announce_channel and interaction.channel.id != self.dev_announce_channel.id:
                        referenced_message = await self.dev_announce_channel.fetch_message(ref_msg_id)
                    else:
                        raise
                
                # Validate if it's the correct message type.
                if referenced_message.author == self.bot.user and referenced_message.embeds and referenced_message.embeds[0].title == "🚀 新開發任務已啟動！":
                    return referenced_message
            except discord.NotFound:
                logger.warning(f"finish-dev: Replied-to message {interaction.message.reference.message_id} not found.")
            except Exception as e:
                logger.error(f"Error fetching original message in finish_dev (reply context): {e}", exc_info=True)
        
        return None

    def _validate_user_permission(self, interaction: Interaction, embed: Embed) -> bool:
        """Checks if the interacting user is the original author of the task."""
        footer_text = embed.footer.text
        if not footer_text:
            logger.error("finish-dev: Cannot validate user, embed footer is empty.")
            return False
        
        match = re.search(r'ID:\s*(\d+)', footer_text)
        if not match:
            logger.error(f"finish-dev: Could not parse author_id from footer: '{footer_text}'")
            return False
            
        try:
            original_author_id = int(match.group(1))
            return interaction.user.id == original_author_id
        except (ValueError, IndexError) as e:
            logger.error(f"finish-dev: Error converting parsed author_id to int. Footer: '{footer_text}'. Error: {e}", exc_info=True)
            return False

    def _update_embed_for_completion(self, original_embed: Embed) -> Embed:
        """Updates the embed to reflect task completion and adds duration."""
        embed_to_edit = original_embed.copy()
        
        # Find fields by name for robustness
        status_field_index = next((i for i, f in enumerate(embed_to_edit.fields) if f.name == "📊 狀態"), -1)
        start_time_field = next((f for f in embed_to_edit.fields if f.name == "⏱️ 開始時間"), None)

        # Update status
        if status_field_index != -1:
            embed_to_edit.set_field_at(status_field_index, name="📊 狀態", value="✅ 已完成", inline=False)
        else:
            embed_to_edit.add_field(name="📊 狀態", value="✅ 已完成", inline=False)

        # Calculate and add duration
        if start_time_field and start_time_field.value:
            timestamp_match = re.search(r"<t:(\d+):F>", start_time_field.value)
            if timestamp_match:
                try:
                    start_timestamp = int(timestamp_match.group(1))
                    start_time_dt = datetime.datetime.fromtimestamp(start_timestamp, tz=datetime.timezone.utc)
                    end_time_dt = datetime.datetime.now(datetime.timezone.utc)
                    duration = end_time_dt - start_time_dt
                    
                    total_seconds = max(0, int(duration.total_seconds()))
                    days, rem = divmod(total_seconds, 86400)
                    hours, rem = divmod(rem, 3600)
                    minutes, seconds = divmod(rem, 60)

                    parts = []
                    if days > 0: parts.append(f"{days} 天")
                    if hours > 0: parts.append(f"{hours} 小時")
                    if minutes > 0: parts.append(f"{minutes} 分鐘")
                    if total_seconds == 0 or seconds > 0: parts.append(f"{seconds} 秒")
                    
                    duration_str = " ".join(parts) if parts else "0 秒"
                    embed_to_edit.add_field(name="⏱️ 總耗時", value=duration_str, inline=False)
                except Exception as e:
                    logger.error(f"Error calculating duration for finish-dev: {e}", exc_info=True)
                    embed_to_edit.add_field(name="⏱️ 總耗時", value="計算失敗", inline=False)
        
        return embed_to_edit

    def _disable_buttons(self, original_message: discord.Message) -> ui.View | None:
        """Creates a new view with all buttons from the original message disabled."""
        if not original_message.components:
            return None
            
        disabled_view = ui.View(timeout=None)
        for action_row in original_message.components:
            for component in action_row.children:
                if isinstance(component, ui.Button):
                    new_button = ui.Button(
                        label=component.label,
                        style=component.style,
                        custom_id=component.custom_id,
                        disabled=True,
                        emoji=component.emoji,
                        url=component.url
                    )
                    disabled_view.add_item(new_button)
        return disabled_view if disabled_view.children else None

    @app_commands.command(name="close", description="標記一個開發任務為已完成。")
    @app_commands.describe(comment="收尾說明，會變成 Issue 上的一則留言。留空就只是關單。")
    async def finish_dev(self, interaction: Interaction, comment: str = None):
        # Step 1: Find the original task message
        original_message = await self._get_original_message(interaction)
        if not original_message:
            await interaction.response.send_message("請在相關的開發任務公告訊息的討論串中，或直接回覆該公告訊息來使用此指令。", ephemeral=True)
            return

        if not original_message.embeds:
            await interaction.response.send_message("錯誤：無法識別目標開發任務公告 (無嵌入內容)。", ephemeral=True)
            return
        
        original_embed = original_message.embeds[0]

        # Step 2: Validate user permission
        if not self._validate_user_permission(interaction, original_embed):
            await interaction.response.send_message("抱歉，只有最初發起此開發任務的使用者才能將其標記為完成。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # --- New GitHub Sync and Close Logic ---
        github_sync_success = False
        issue_closed_success = False
        error_messages = []

        # Get the user's GitHub token to perform actions on their behalf
        discord_id_str = str(interaction.user.id)
        user_gh_data = await self._get_user_github_data(discord_id_str)
        user_token = user_gh_data.get("access_token") if user_gh_data else None

        # Find the associated GitHub issue
        mapping_key = str(original_message.id)
        mapping_info = self.thread_issue_mappings.get(mapping_key)
        
        # Get the thread from the original message, which is more robust
        thread_to_process = original_message.thread

        if mapping_info and thread_to_process:
            issue_number = mapping_info.get("issue_number")
            repo_full_name = mapping_info.get("repo")

            if not user_token:
                error_messages.append("❌ 您尚未透過 `/login` 授權，無法以您的名義同步並關閉 Issue。")
            elif issue_number and repo_full_name:
                try:
                    # 1. Fetch history and generate markdown content
                    messages = [msg async for msg in thread_to_process.history(limit=None, oldest_first=True)]
                    summary_header = f"# 📜 Discord Thread Archive: \"{thread_to_process.name}\"\n\n"
                    summary_header += f"**Issue:** `#{issue_number}`\n"
                    summary_header += f"**Closed by:** `{interaction.user.display_name}`\n"
                    summary_header += f"**Archived on:** `{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}`\n\n---\n\n"
                    
                    summary_body = ""
                    for msg in messages:
                        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                        summary_body += f"### 👤 `{msg.author.display_name}` at `{timestamp}`\n"
                        summary_body += f"{msg.clean_content}\n\n"
                        if msg.attachments:
                            summary_body += "**Attachments:**\n"
                            for att in msg.attachments:
                                summary_body += f"- [{att.filename}]({att.url})\n"
                            summary_body += "\n"
                        summary_body += "---\n"
                    
                    archive_content = summary_header + (summary_body if summary_body else "*This thread had no text messages.*")

                    # 2. Initialize GitHub client and get repo
                    gh = await self._run_sync(Github, user_token)
                    authed_user = await self._run_sync(gh.get_user)
                    repo = await self._run_sync(gh.get_repo, repo_full_name)
                    issue = await self._run_sync(repo.get_issue, number=issue_number)

                    # 3. Create a secret Gist with the archive content
                    gist_filename = f"issue-{issue_number}-archive.md"
                    gist_description = f"Discord conversation archive for {repo_full_name}#{issue_number}"
                    
                    # The create_gist method expects a dictionary of InputFileContent objects
                    gist_files = {gist_filename: InputFileContent(archive_content)}
                    
                    created_gist = await self._run_sync(
                        authed_user.create_gist,
                        public=False,  # Creates a secret Gist
                        files=gist_files,
                        description=gist_description
                    )
                    archive_url = created_gist.html_url
                    github_sync_success = True

                    # 4. Whatever the person typed, as its own comment — before
                    # the archive note, so the reason for closing reads above
                    # the bookkeeping rather than under it.
                    if comment:
                        await self._run_sync(
                            issue.create_comment,
                            f"{comment}\n\n*— {interaction.user.display_name}，於 Discord 關閉此任務時留下*"
                            f"\n\n{store.SYNC_MARKER}",
                        )

                    # 5. Post link to archive in the issue
                    final_comment = f"📜 **Discord Conversation Archived**\n\nThe full conversation from the associated Discord thread has been archived as a secret Gist and can be viewed here:\n\n➡️ **[View Archive]({archive_url})**\n\n*This issue was closed by {interaction.user.display_name} upon task completion.*\n\n{store.SYNC_MARKER}"
                    await self._run_sync(issue.create_comment, final_comment)

                    # 6. Close the issue
                    await self._run_sync(issue.edit, state='closed')
                    issue_closed_success = True

                except GithubException as e:
                    logger.error(f"GitHub API Error during archive/sync/close for issue {repo_full_name}#{issue_number}: {e}", exc_info=True)
                    scopes = e.headers.get("X-OAuth-Scopes", "未提供")
                    error_messages.append(f"❌ 發生 GitHub API 錯誤: {e.status} {e.data.get('message', '')}")
                    error_messages.append(f"ℹ️ 您目前的 Token 權限為: `{scopes}`。請確保其包含 `gist` 權限。若無，請重新執行 `/login`。")
                except Exception as e:
                    logger.error(f"Unexpected error during GitHub file archive/sync/close for issue {repo_full_name}#{issue_number} by user {discord_id_str}: {e}", exc_info=True)
                    error_messages.append(f"❌ 歸檔或關閉 Issue 時發生未預期的錯誤: {e}")
            else:
                error_messages.append("⚠️ 找不到對應的 GitHub Issue 資訊，無法同步。")
        
        # --- Original Logic: Update Embed and Disable Buttons ---
        completed_embed = self._update_embed_for_completion(original_embed)
        disabled_view = self._disable_buttons(original_message)
        await original_message.edit(embed=completed_embed, view=disabled_view)
        
        # --- Build the report BEFORE deleting the thread ---
        task_field = next((f for f in completed_embed.fields if f.name == "📝 任務內容"), None)
        task_name_preview = f"{task_field.value[:30]}..." if task_field else "此任務"
        
        final_report = [f"🎉 任務「{task_name_preview}」已被標記為已完成！"]
        # Archived, not deleted. The Gist and the issue hold the record, but a
        # deleted thread also takes with it every link anyone ever pasted to it,
        # and there is no undo — while an archive costs nothing and can be
        # reopened by posting in it. Closing the issue from GitHub already
        # behaved this way, so both routes now leave the same thing behind.
        will_archive_thread = thread_to_process and github_sync_success and issue_closed_success

        if mapping_info and thread_to_process:
            issue_url = f"https://github.com/{repo_full_name}/issues/{issue_number}"
            if github_sync_success: final_report.append(f"✅ 對話紀錄已歸檔至 [GitHub Issue #{issue_number}]({issue_url})。")
            if issue_closed_success: final_report.append(f"✅ GitHub Issue #{issue_number} 已關閉。")
            if will_archive_thread: final_report.append("✅ Discord 討論串即將封存（不會刪除，紀錄留著）。")

        if error_messages:
            final_report.extend(error_messages)

        # --- Send the final report BEFORE archiving, because an archived thread
        #     cannot be posted into ---
        await self._ack(interaction, "\n".join(filter(None, final_report)))

        # --- Archive Thread ---
        if will_archive_thread:
            try:
                await thread_to_process.edit(archived=True)
            except Exception as e:
                logger.error(f"Failed to archive thread {thread_to_process.id}: {e}", exc_info=True)
                try:
                    await interaction.user.send(f"⚠️ 任務「{task_name_preview}」的討論串封存失敗，請手動封存。錯誤：{e}")
                except discord.Forbidden:
                    logger.error(f"Failed to send DM to user {interaction.user.id} about thread archival failure.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listener to sync messages from Discord threads to GitHub issues."""
        if message.author == self.bot.user or not isinstance(message.channel, discord.Thread):
            return

        # The thread's ID is the original message's ID that started the thread.
        mapping_key = str(message.channel.id)
        if mapping_key not in self.thread_issue_mappings:
            return

        mapping_info = self.thread_issue_mappings[mapping_key]
        issue_number = mapping_info.get("issue_number")
        repo_full_name = mapping_info.get("repo")

        if not issue_number or not repo_full_name:
            logger.warning(f"Missing issue_number or repo_full_name for thread {mapping_key}")
            return

        # Determine which token and identity to use
        discord_id_str = str(message.author.id)
        user_gh_data = await self._get_user_github_data(discord_id_str)
        token_to_use = None
        comment_body = ""
        using_user_token = False

        if user_gh_data and user_gh_data.get("access_token"):
            token_to_use = user_gh_data["access_token"]
            comment_body = await self._render_for_github(message)
            using_user_token = True
        else:
            token_to_use = self.github_bot_token
            comment_body = (
                f"**來自 Discord 使用者 `{message.author.display_name}` 的留言：**\n\n"
                f"{await self._render_for_github(message)}"
            )

        if not token_to_use:
            logger.warning(f"No token available to sync message from {discord_id_str}. User not logged in and no bot token set.")
            await message.add_reaction("⚠️")
            try:
                await message.author.send(
                    f"您的留言無法同步到 GitHub，因為您尚未透過 `/login` 授權，且系統未設定備用同步方案。",
                    suppress_embeds=True
                )
            except discord.Forbidden:
                pass  # Can't send DMs
            return

        if message.attachments:
            comment_body += "\n\n**附件：**"
            for attachment in message.attachments:
                # Images go in as markdown rather than as a bare link, and that
                # is not cosmetic: GitHub rewrites image sources through its own
                # camo proxy, which fetches and caches the bytes. A Discord CDN
                # URL is signed and expires within about a day, so a plain link
                # is dead by the time anyone reads the issue — the picture only
                # survives if GitHub is made to copy it.
                if (attachment.content_type or "").startswith("image/"):
                    comment_body += f"\n\n![{attachment.filename}]({attachment.url})"
                else:
                    comment_body += f"\n- [{attachment.filename}]({attachment.url})"

        # See `store.SYNC_MARKER`: this is what stops the comment coming straight
        # back through the webhook as if somebody on GitHub had written it.
        comment_body += f"\n\n{store.SYNC_MARKER}"

        try:
            gh = await self._run_sync(Github, token_to_use)
            repo = await self._run_sync(gh.get_repo, repo_full_name)
            issue = await self._run_sync(repo.get_issue, number=issue_number)
            
            created = await self._run_sync(issue.create_comment, comment_body)
            # Remembered so that a reaction added to this Discord message later
            # can find the comment it turned into.
            store.remember_comment(message.id, repo_full_name, issue_number, created.id)

            identity_log = "as user" if using_user_token else "as bot"
            logger.info(f"Synced message from Discord user {message.author.id} ({identity_log}) to GitHub Issue {repo_full_name}#{issue_number}")
            await message.add_reaction("✅")

        except GithubException as e:
            logger.error(f"GitHub API error while syncing message to issue {repo_full_name}#{issue_number}: {e}", exc_info=True)
            await message.add_reaction("❌")
        except Exception as e:
            logger.error(f"An unexpected error occurred while syncing message to GitHub: {e}", exc_info=True)
            await message.add_reaction("❌")

    @app_commands.command(name="sync-github-comment", description="[內部使用] 將 GitHub 留言同步到 Discord 討論串。")
    @app_commands.describe(
        thread_id="目標 Discord 討論串的 ID。",
        github_author="留言的 GitHub 使用者名稱。",
        comment_body="留言的內容。",
        comment_url="留言的永久連結。"
    )
    async def sync_github_comment(self, interaction: Interaction, thread_id: str, github_author: str, comment_body: str, comment_url: str):
        # 簡易權限檢查：只允許擁有特定角色的使用者或機器人擁有者執行
        # 您應根據您的 API 驗證機制調整此處
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("權限不足。", ephemeral=True)
            return

        if not thread_id.isdigit():
            await interaction.response.send_message("錯誤：無效的 thread_id。", ephemeral=True)
            return

        try:
            thread = await self.bot.fetch_channel(int(thread_id))
            if not isinstance(thread, discord.Thread):
                await interaction.response.send_message(f"錯誤：找不到 ID 為 {thread_id} 的討論串。", ephemeral=True)
                return
        except discord.NotFound:
            await interaction.response.send_message(f"錯誤：找不到 ID 為 {thread_id} 的討論串。", ephemeral=True)
            return
        except Exception as e:
            await interaction.response.send_message(f"獲取討論串時發生錯誤：{e}", ephemeral=True)
            logger.error(f"Error fetching thread {thread_id} in sync_github_comment: {e}", exc_info=True)
            return

        embed = Embed(
            description=comment_body,
            color=discord.Color.green()
        )
        embed.set_author(name=f"來自 GitHub 的新留言 (由 {github_author} 發布)", url=comment_url, icon_url="https://github.githubassets.com/images/modules/logos_page/GitHub-Mark.png")
        
        try:
            await thread.send(embed=embed)
            await interaction.response.send_message("同步成功。", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to send GitHub comment to thread {thread_id}: {e}", exc_info=True)
            await interaction.response.send_message(f"同步訊息到討論串時發生錯誤：{e}", ephemeral=True)

    @app_commands.command(name="archive-dev-thread", description="[內部使用] 歸檔一個開發任務討論串。")
    @app_commands.describe(thread_id="要歸檔的 Discord 討論串 ID。")
    async def archive_dev_thread(self, interaction: Interaction, thread_id: str):
        # 同樣，加入權限檢查
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("權限不足。", ephemeral=True)
            return

        if not thread_id.isdigit():
            await interaction.response.send_message("錯誤：無效的 thread_id。", ephemeral=True)
            return

        try:
            thread = await self.bot.fetch_channel(int(thread_id))
            if not isinstance(thread, discord.Thread):
                await interaction.response.send_message(f"錯誤：找不到 ID 為 {thread_id} 的討論串。", ephemeral=True)
                return
        except discord.NotFound:
            await interaction.response.send_message(f"錯誤：找不到 ID 為 {thread_id} 的討論串。", ephemeral=True)
            return
        except Exception as e:
            await interaction.response.send_message(f"獲取討論串時發生錯誤：{e}", ephemeral=True)
            logger.error(f"Error fetching thread {thread_id} in archive_dev_thread: {e}", exc_info=True)
            return

        try:
            await thread.send("✅ 此任務已在 GitHub 上關閉，本討論串將自動歸檔。")
            await thread.edit(archived=True)
            await interaction.response.send_message(f"討論串 {thread.mention} 已成功歸檔。", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("錯誤：我沒有權限歸檔此討論串。", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to archive thread {thread_id}: {e}", exc_info=True)
            await interaction.response.send_message(f"歸檔討論串時發生錯誤：{e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DevFlow(bot))
