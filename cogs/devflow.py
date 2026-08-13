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
import functools

import store

logger = logging.getLogger(__name__)

class DevTaskView(ui.View):
    def __init__(self, cog_instance, original_interaction: Interaction, task_description: str, repo: str = None):
        super().__init__(timeout=None)
        self.cog = cog_instance
        self.original_interaction = original_interaction
        self.task_description = task_description
        self.repo_override = repo
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
            # Case 3: The key is neither a pending state nor a valid Discord ID mapping. It's old/invalid data.
            else:
                logger.warning(f"Discarding old or malformed entry with key '{key_in_file}': {data_value}")
                needs_resave = True # Mark for resave to ensure this invalid data is purged.

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

    @app_commands.command(name="github-login", description="授權 Bot 代表您訪問 GitHub。")
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
        await interaction.response.send_message(f"✅ GitHub 用戶名已手動設為：`{cleaned_username}`。要啟用以您名義發布Issue等功能，請使用 `/github-login`。", ephemeral=True)

    async def repo_autocomplete(self, interaction: Interaction, current: str) -> list[app_commands.Choice[str]]:
        discord_id_str = str(interaction.user.id)
        user_gh_data = await self._get_user_github_data(discord_id_str)
        
        if not user_gh_data or "access_token" not in user_gh_data:
            return [app_commands.Choice(name="⚠️ 請先使用 /github-login 授權", value="")]

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

    @app_commands.command(name="start-dev", description="宣告一個新的開發任務。")
    @app_commands.describe(
        task="任務的詳細描述。",
        repo="要建立 Issue 的儲存庫，留空則使用預設儲存庫。"
    )
    @app_commands.autocomplete(repo=repo_autocomplete)
    async def start_dev(self, interaction: Interaction, task: str, repo: str = None):
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
        
        view_buttons = DevTaskView(cog_instance=self, original_interaction=interaction, task_description=task, repo=repo)
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
            await interaction.response.send_message("❌ 您尚未透過 `/github-login` 授權或授權資料不完整。請先完成授權。", ephemeral=True)
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
            await interaction.followup.send(f"❌ 無法存取倉庫 `{repo_path}`。請檢查倉庫是否存在、您是否有權限，或重新執行 `/github-login`。\n錯誤: {e.status} - {e.data.get('message', 'Unknown error')}", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Failed to init Github or get repo '{repo_path}' with user token: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 無法使用您的 GitHub 憑證初始化或存取倉庫 `{repo_path}`。請重新執行 `/github-login` 或檢查 Token 權限。", ephemeral=True)
            return
        issue_title = task_description[:50]
        issue_body = f"{task_description}\n\n---\n*此 Issue 由 Discord 使用者 {view.original_interaction.user.display_name} (ID: {view.original_interaction.user.id}) 透過 Bot 自動建立。*"
        assignees = [github_username] if github_username else []
        try:
            created_issue = await self._run_sync(repo.create_issue, title=issue_title, body=issue_body, assignees=assignees)
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
            
            assign_msg = f"並已嘗試將您 (`{github_username}`) 指派。" if github_username else ""
            await interaction.followup.send(f"✅ 成功在 GitHub 建立 Issue #{created_issue.number}！ {assign_msg}", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to create/assign GitHub Issue for {view.original_interaction.user}: {e}", exc_info=True)
            await interaction.followup.send(f"❌ 建立或指派 GitHub Issue 時發生錯誤: {e}", ephemeral=True)

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
                assign_message = f"\n⚠️ 您尚未透過 `/github-login` 授權或您的 GitHub 用戶名未儲存。請先授權後再試。"
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

    @app_commands.command(name="finish-dev", description="標記一個開發任務為已完成。")
    async def finish_dev(self, interaction: Interaction):
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
        thread_deleted_success = False
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
                error_messages.append("❌ 您尚未透過 `/github-login` 授權，無法以您的名義同步並關閉 Issue。")
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

                    # 4. Post link to archive in the issue
                    final_comment = f"📜 **Discord Conversation Archived**\n\nThe full conversation from the associated Discord thread has been archived as a secret Gist and can be viewed here:\n\n➡️ **[View Archive]({archive_url})**\n\n*This issue was closed by {interaction.user.display_name} upon task completion.*"
                    await self._run_sync(issue.create_comment, final_comment)
                    
                    # 5. Close the issue
                    await self._run_sync(issue.edit, state='closed')
                    issue_closed_success = True

                except GithubException as e:
                    logger.error(f"GitHub API Error during archive/sync/close for issue {repo_full_name}#{issue_number}: {e}", exc_info=True)
                    scopes = e.headers.get("X-OAuth-Scopes", "未提供")
                    error_messages.append(f"❌ 發生 GitHub API 錯誤: {e.status} {e.data.get('message', '')}")
                    error_messages.append(f"ℹ️ 您目前的 Token 權限為: `{scopes}`。請確保其包含 `gist` 權限。若無，請重新執行 `/github-login`。")
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
        will_delete_thread = thread_to_process and github_sync_success and issue_closed_success

        if mapping_info and thread_to_process:
            issue_url = f"https://github.com/{repo_full_name}/issues/{issue_number}"
            if github_sync_success: final_report.append(f"✅ 對話紀錄已歸檔至 [GitHub Issue #{issue_number}]({issue_url})。")
            if issue_closed_success: final_report.append(f"✅ GitHub Issue #{issue_number} 已關閉。")
            if will_delete_thread: final_report.append("✅ Discord 討論串即將刪除。")
        
        if error_messages:
            final_report.extend(error_messages)

        # --- Send the final report BEFORE deleting the thread ---
        await interaction.followup.send("\n".join(filter(None, final_report)), ephemeral=True)

        # --- Delete Thread ---
        if will_delete_thread:
            try:
                await thread_to_process.delete()
            except Exception as e:
                logger.error(f"Failed to delete thread {thread_to_process.id}: {e}", exc_info=True)
                try:
                    await interaction.user.send(f"⚠️ 任務「{task_name_preview}」的討論串刪除失敗，請手動刪除。錯誤：{e}")
                except discord.Forbidden:
                    logger.error(f"Failed to send DM to user {interaction.user.id} about thread deletion failure.")

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
            comment_body = message.clean_content
            using_user_token = True
        else:
            token_to_use = self.github_bot_token
            comment_body = (
                f"**來自 Discord 使用者 `{message.author.display_name}` 的留言：**\n\n"
                f"{message.clean_content}"
            )

        if not token_to_use:
            logger.warning(f"No token available to sync message from {discord_id_str}. User not logged in and no bot token set.")
            await message.add_reaction("⚠️")
            try:
                await message.author.send(
                    f"您的留言無法同步到 GitHub，因為您尚未透過 `/github-login` 授權，且系統未設定備用同步方案。",
                    suppress_embeds=True
                )
            except discord.Forbidden:
                pass  # Can't send DMs
            return

        if message.attachments:
            comment_body += "\n\n**附件：**"
            for attachment in message.attachments:
                comment_body += f"\n- {attachment.url}"

        try:
            gh = await self._run_sync(Github, token_to_use)
            repo = await self._run_sync(gh.get_repo, repo_full_name)
            issue = await self._run_sync(repo.get_issue, number=issue_number)
            
            await self._run_sync(issue.create_comment, comment_body)
            
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
