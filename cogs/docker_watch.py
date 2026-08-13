import discord
from discord.ext import commands
import os
import asyncio
# import docker # 改用 python-on-whales
# from docker.errors import DockerException # 改用 python-on-whales 的異常
from python_on_whales import DockerClient as PythonOnWhalesDockerClient # 避免與可能的其他 DockerClient 衝突
from python_on_whales.exceptions import DockerException, NoSuchContainer # python-on-whales 的異常
import re
import time
from discord import ButtonStyle, Interaction
from discord.ui import View, Button
import logging

logger = logging.getLogger(__name__)

#: 要盯的容器。沒設就等於整個 cog 不掛載 —— 見 setup()。
WATCH_CONTAINER = os.getenv("WATCH_CONTAINER", "")

#: 重啟鈕預設關著。監控只需要 GET /containers/*,重啟需要 POST,
#: 而把 POST 開給 proxy 等於把「容器 RCE → 能操作其他容器」這條路重新打開。
#: 要用就同時設這個和 proxy 的 POST=1,兩個都是刻意的動作。
ALLOW_RESTART = os.getenv("ALLOW_RESTART", "").lower() in ("1", "true", "yes")

# 載入 .env 以便 Cog 獨立獲取其配置 (如果需要)
# from dotenv import load_dotenv
# load_dotenv() # 主 bot.py 已經載入

# --- 控制面板視圖 (從原 bot.py 遷移) ---
class ControlPanelView(View):
    def __init__(self, *, timeout=180):
        super().__init__(timeout=timeout)
        # self.restart_button.disabled = False # Button decorator 會處理初始狀態
        # Button 的 custom_id 應該在 decorator 中定義，或者在 __init__ 中手動添加 item

    @discord.ui.button(label="重新啟動 API", style=ButtonStyle.danger, custom_id="docker_restart_api_button") # custom_id 最好加上 Cog 前綴避免衝突
    async def restart_button(self, interaction: Interaction, button: Button):
        await interaction.response.defer(ephemeral=True) # 延遲回應，設為僅發起者可見
        docker_container_name = WATCH_CONTAINER

        original_button_label = button.label
        button.label = "處理中..."
        button.disabled = True
        await interaction.edit_original_response(view=self)

        # Refused here rather than by hiding the button, so that pressing it
        # says *why* instead of doing nothing.
        if not ALLOW_RESTART:
            await interaction.followup.send(
                "❌ 這個實例沒有開放重啟。\n"
                "要開的話得同時做兩件事:設 `ALLOW_RESTART=1`,並讓 docker-socket-proxy 允許 `POST=1`。"
                "預設關著是因為監控只需要唯讀權限,而開放 POST 等於讓這個 bot 能操作主機上的其他容器。",
                ephemeral=True,
            )
            return

        try:
            await interaction.followup.send(f"正在重新啟動 `{docker_container_name}`…", ephemeral=True)

            # Through the Docker API rather than the `docker` CLI. The previous
            # version shelled out, which cannot work in this image — there is no
            # `docker` binary in it — and would not have reached the daemon
            # anyway, because `DOCKER_HOST` points at the socket proxy. Going
            # through the library means a proxy that forbids POST answers with a
            # clear permission error instead of "command not found".
            docker = PythonOnWhalesDockerClient()
            # In an executor because python-on-whales is blocking, and blocking
            # the event loop here would stall every other Discord interaction
            # for as long as the restart takes.
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, lambda: docker.container.restart(docker_container_name)
                ),
                timeout=60,
            )
            await interaction.followup.send(f"✅ `{docker_container_name}` 已重新啟動。", ephemeral=False)

        except asyncio.TimeoutError:
            await interaction.followup.send(f"⚠️ 重啟 `{docker_container_name}` 超時。", ephemeral=False)
        except NoSuchContainer:
            await interaction.followup.send(f"❌ 找不到容器 `{docker_container_name}`。", ephemeral=True)
        except DockerException as e:
            await interaction.followup.send(
                f"❌ Docker 拒絕了這個操作:\n```\n{e}\n```\n"
                "如果訊息裡有 403,那是 socket-proxy 擋下來的 —— 它預設只允許 GET。",
                ephemeral=True,
            )
            logger.error("restart refused for %s: %s", docker_container_name, e)
        except Exception as e:
            await interaction.followup.send(f"❌ 執行 Docker 指令時發生未預期的錯誤：\n```\n{e}\n```", ephemeral=True)
            logger.error(f"執行 docker restart 時出錯: {e}", exc_info=True)
        finally:
            await asyncio.sleep(5)
            if not self.is_finished():
                 button.label = original_button_label
                 button.disabled = False
                 try:
                     await interaction.edit_original_response(view=self)
                 except discord.NotFound:
                     logger.warning("嘗試重新啟用按鈕時，原始訊息已刪除 (ControlPanelView)。")
                 except Exception as e:
                     logger.error(f"重新啟用按鈕時發生錯誤 (ControlPanelView): {e}", exc_info=True)

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, Button):
                item.disabled = True
        logger.info("控制面板視圖已超時 (ControlPanelView)。")


class DockerFeatures(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("DockerFeatures Cog: Initializing...")
        self.docker_monitor_started = False
        self.target_channel_id = None

        target_channel_id_str = os.getenv('TARGET_CHANNEL_ID')
        if target_channel_id_str and target_channel_id_str.isdigit():
            self.target_channel_id = int(target_channel_id_str)
            logger.info(f"DockerFeatures Cog: TARGET_CHANNEL_ID set to {self.target_channel_id}")
        else:
            logger.warning(f"DockerFeatures Cog: TARGET_CHANNEL_ID ('{target_channel_id_str}') 未設定或格式不正確。監控訊息可能無法發送。")
        logger.info("DockerFeatures Cog: __init__ complete.")

    def _schedule_discord_message(self, channel_id: int, message: str): # Changed to sync method
        """輔助函數，用於在主事件循環中安全地安排發送 Discord 消息"""
        # logger.debug(f"DockerFeatures Cog: Scheduling message to channel {channel_id}: '{message[:50]}...'")
        
        async def send_message_coro(): # Internal coroutine for the actual sending
            # logger.debug(f"DockerFeatures Cog: Sending message to channel {channel_id}...")
            channel = self.bot.get_channel(channel_id)
            if channel:
                # logger.debug(f"DockerFeatures Cog: Found channel {channel.name} ({channel_id}), attempting to send...")
                try:
                    await channel.send(message)
                    # logger.debug(f"DockerFeatures Cog: Successfully sent message to channel {channel.name} ({channel_id}).")
                except discord.Forbidden:
                    logger.error(f"DockerFeatures Cog: Permission denied to send message to channel {channel.name} ({channel_id}).")
                except discord.HTTPException as e:
                    logger.error(f"DockerFeatures Cog: HTTP error sending message to channel {channel.name} ({channel_id}): {e}")
                except Exception as e:
                    logger.error(f"DockerFeatures Cog: Unexpected error sending message to channel {channel.name} ({channel_id}): {e}", exc_info=True)
            else:
                logger.error(f"DockerFeatures Cog: Could not find channel ID {channel_id} to send message.")
        
        # Create task in the bot's event loop from this sync method
        self.bot.loop.create_task(send_message_coro())


    def _docker_log_monitor_sync(self, loop: asyncio.AbstractEventLoop, container_name: str):
        """同步函數，包含阻塞的 Docker 日誌監聽循環"""
        if not self.target_channel_id:
            logger.error("Docker log monitor thread: target_channel_id not set. Aborting.")
            return
        
        logger.info(f"Docker log monitor thread: Starting for container '{container_name}' on channel {self.target_channel_id}.")
        log_filter_regex = re.compile(r'(\s\d{3}(\s|$)|error)', re.IGNORECASE)
        http_log_parser_regex = re.compile(r'"([A-Z]+)\s+([^"]+)\s+HTTP/\d\.\d"\s+(\d{3})')
        last_error_message_sent_time = 0
        last_sent_error_content = None
        ERROR_NOTIFICATION_COOLDOWN_SECONDS = 60
        global_exception_event_active = False
        global_exception_event_timestamp = 0
        GLOBAL_EXCEPTION_WINDOW_SECONDS = 5.0

        while True:
            client = None
            container = None
            try:
                if client is None:
                    logger.info(f"Docker log monitor thread: Attempting connection using python-on-whales (DOCKER_HOST should be set).")
                    try:
                        # python-on-whales 會自動使用 DOCKER_HOST
                        # 初始化失敗會拋出異常，無需顯式 ping
                        client = PythonOnWhalesDockerClient() 
                        logger.info(f"Docker log monitor thread: Successfully connected via python-on-whales.")
                    except DockerException as e:
                        logger.error(f"Docker log monitor thread: Connection via python-on-whales failed: {e}")
                        raise # Re-raise to be caught by the outer handler
                
                # python-on-whales 中獲取容器並串流日誌
                # logs() 返回一個生成器，可以直接迭代
                # python-on-whales 的 logs 直接返回 (stdout, stderr) 字串元組
                for log_type, line_bytes in client.container.logs(container_name, stream=True, follow=True, tail="0"):
                    try:
                        # line_bytes 已經是 bytes, 需要 decode
                        line = line_bytes.decode('utf-8', errors='ignore').strip()
                        if not line: continue

                        if log_filter_regex.search(line):
                            message_this_line = None
                            current_processing_time = time.time()

                            if (current_processing_time - global_exception_event_timestamp) > GLOBAL_EXCEPTION_WINDOW_SECONDS:
                                global_exception_event_active = False

                            if "全局異常捕獲於" in line:
                                message_this_line = "🔥 API 偵測到全局異常。詳細資訊請查看伺服器日誌。"
                                global_exception_event_active = True
                                global_exception_event_timestamp = current_processing_time
                            elif not global_exception_event_active:
                                match = http_log_parser_regex.search(line)
                                if match:
                                    method, path, status_code_str = match.groups()
                                    status_code = int(status_code_str)
                                    emoji = "✅" if 200 <= status_code < 300 else "⚠️" if 400 <= status_code < 500 else "🔥" if 500 <= status_code < 600 else "📄"
                                    if status_code >= 500 :
                                         message_this_line = f"{emoji} 偵測到 API 伺服器內部錯誤 ({status_code})。詳細資訊請查看伺服器日誌。"
                                    else:
                                         message_this_line = f"{emoji} `{method} {path} - {status_code}`"
                                elif 'error' in line.lower() and not ("uvicorn.error" in line.lower() and "info -" in line.lower()):
                                    message_this_line = "🔥 API 偵測到內部錯誤。詳細資訊請查看伺服器日誌。"
                            
                            if message_this_line:
                                if message_this_line.startswith("🔥"):
                                    if (current_processing_time - last_error_message_sent_time) > ERROR_NOTIFICATION_COOLDOWN_SECONDS or \
                                       message_this_line != last_sent_error_content:
                                        loop.call_soon_threadsafe(self._schedule_discord_message, self.target_channel_id, message_this_line)
                                        last_error_message_sent_time = current_processing_time
                                        last_sent_error_content = message_this_line
                                        if global_exception_event_active and "全局異常" in message_this_line:
                                            global_exception_event_timestamp = current_processing_time
                                else:
                                    loop.call_soon_threadsafe(self._schedule_discord_message, self.target_channel_id, message_this_line)
                    except Exception as e:
                        logger.error(f"Docker log monitor thread: Error processing log line: {e}", exc_info=True)
                        time.sleep(0.1)
                # python-on-whales 的日誌流在容器停止時會自動結束
                client = None # Reset client to re-establish connection in the next iteration
                logger.info(f"Docker log monitor thread: Log stream for '{container_name}' ended (container might have stopped). Waiting before retry.")
                time.sleep(5)
            except NoSuchContainer: # python-on-whales 的異常
                logger.warning(f"Docker log monitor thread: Container '{container_name}' not found. Retrying in 15 seconds...")
                client = None
                time.sleep(15)
            except DockerException as e: # 捕獲 python-on-whales 的通用 DockerException
                # requests.exceptions.ConnectionError 可能仍然需要，如果 DOCKER_HOST 指向一個無效的 HTTP 端點
                logger.error(f"Docker log monitor thread: Connection error or failed to get container '{container_name}': {e}. Retrying in 30 seconds...")
                client = None
                time.sleep(30)
            except Exception as e:
                logger.error(f"Docker log monitor thread: Unexpected error: {e}", exc_info=True)
                msg = f"⚠️ **Docker 日誌監控出錯**：監聽過程中發生未預期錯誤。\n錯誤：`{e}`"
                loop.call_soon_threadsafe(self._schedule_discord_message, self.target_channel_id, msg)
                client = None
                time.sleep(30)

    def _docker_event_loop_sync(self, loop: asyncio.AbstractEventLoop, container_name: str):
        """同步函數，包含阻塞的 Docker 事件監聽循環 (輪詢方式)"""
        if not self.target_channel_id:
            logger.error("Docker event loop thread: target_channel_id not set. Aborting.")
            return
        
        logger.info(f"Docker event loop thread: Starting for container '{container_name}' on channel {self.target_channel_id}.")
        client = None 
        last_status = None
        polling_interval = 5

        while True:
            current_status = None
            container_obj = None # 在 python-on-whales 中，這通常是 Container 對象
            try:
                if client is None:
                    logger.info(f"Docker event loop thread: Attempting connection using python-on-whales (DOCKER_HOST should be set).")
                    try:
                        # 初始化失敗會拋出異常，無需顯式 ping
                        client = PythonOnWhalesDockerClient()
                        logger.info(f"Docker event loop thread: Successfully connected via python-on-whales.")
                    except DockerException as e:
                        logger.error(f"Docker event loop thread: Connection via python-on-whales failed: {e}")
                        raise # Re-raise to be caught by the outer handler
                
                # python-on-whales 中獲取容器狀態
                container_obj = client.container.inspect(container_name) # 返回 ContainerInspectResult 對象
                current_status = container_obj.state.status # 例如 'running', 'exited'
            except NoSuchContainer: # python-on-whales 的異常
                current_status = "not_found" 
            except DockerException as e: # 捕獲 python-on-whales 的通用 DockerException
                logger.error(f"Docker event loop thread: Connection error or failed to get container '{container_name}': {e}. Retrying in {polling_interval * 6} seconds...")
                client = None 
                time.sleep(polling_interval * 6) 
                continue 
            except Exception as e:
                logger.error(f"Docker event loop thread: Unexpected error during polling: {e}", exc_info=True)
                msg = f"⚠️ **Docker 輪詢監控出錯**：檢查容器狀態時發生錯誤。\n錯誤：`{e}`"
                loop.call_soon_threadsafe(self._schedule_discord_message, self.target_channel_id, msg)
                time.sleep(polling_interval * 2)
                continue

            if current_status != last_status:
                logger.info(f"Docker event loop thread: Status change detected for '{container_name}': '{last_status}' -> '{current_status}'")
                msg = None
                if current_status == "running" and (last_status is not None and last_status != "running"): # status 字串可能不同，需確認
                    msg = f"✅ **容器 `{container_name}` 已啟動** (狀態: {current_status})"
                elif current_status == "exited" and (last_status is not None and last_status != "exited"):
                    # python-on-whales 中 exit_code 在 container_obj.state.exit_code
                    exit_code = container_obj.state.exit_code if container_obj and hasattr(container_obj.state, 'exit_code') else 'N/A'
                    msg = f"🛑 **容器 `{container_name}` 已停止** (狀態: {current_status}, 退出碼: {exit_code})"
                elif current_status == "not_found" and (last_status is not None and last_status != "not_found"): # 'not_found' 是我們自己定義的狀態
                    msg = f"🗑️ **容器 `{container_name}` 已被移除或找不到**"
                
                if msg:
                    loop.call_soon_threadsafe(self._schedule_discord_message, self.target_channel_id, msg)
                last_status = current_status
            time.sleep(polling_interval)

    async def listen_docker_events(self):
        """異步函數，使用 run_in_executor 啟動阻塞的 Docker 狀態和日誌監控"""
        if not self.target_channel_id:
            logger.error("Docker monitor error (DockerFeatures Cog): Target channel ID not set or not found.")
            return

        docker_container_name = WATCH_CONTAINER
        logger.info(f"DockerFeatures Cog: Preparing to start Docker status and log monitoring in executor for container '{docker_container_name}'.")

        loop = self.bot.loop

        # 狀態監控
        status_task_future = loop.run_in_executor(
            None, self._docker_event_loop_sync, loop, docker_container_name
        )
        # 日誌監控
        log_task_future = loop.run_in_executor(
            None, self._docker_log_monitor_sync, loop, docker_container_name
        )
        
        try:
            logger.info("DockerFeatures Cog: Docker monitoring tasks submitted to executor.")
        except Exception as e:
            logger.error(f"Docker monitoring task (gather) encountered an error: {e}", exc_info=True)
            channel = self.bot.get_channel(self.target_channel_id)
            if channel:
                await channel.send(f"⚠️ Docker 監控系統遇到嚴重錯誤並可能已停止: {e}")


    async def initialize_docker_monitoring(self):
        """輔助函數，確保在 Bot 準備好後啟動監控。"""
        await self.bot.wait_until_ready()
        logger.info("DockerFeatures Cog: Initializing Docker monitoring...")
        if not self.docker_monitor_started and self.target_channel_id:
            try:
                if not self.bot.loop.is_running():
                    logger.error("DockerFeatures Cog: Event loop not running, cannot start Docker monitoring.")
                    return

                self.bot.loop.create_task(self.listen_docker_events())
                self.docker_monitor_started = True
                logger.info("DockerFeatures Cog: Docker event monitoring task started.")
                # channel = self.bot.get_channel(self.target_channel_id)
                # if channel:
                #     # Consider if this message is needed every time, or only on first successful start
                #     # await channel.send("🐳 Docker 監控功能已啟動。")
                #     pass

            except Exception as e: 
                logger.error(f"DockerFeatures Cog: Unexpected error during Docker monitoring startup: {e}", exc_info=True)
                channel = self.bot.get_channel(self.target_channel_id)
                if channel:
                    await channel.send(f"⚠️ 啟動 Docker 事件監控時發生未預期錯誤：{e}")
        elif self.docker_monitor_started:
            logger.info("DockerFeatures Cog: Docker event monitoring already running.")
        elif not self.target_channel_id:
            logger.error("DockerFeatures Cog: TARGET_CHANNEL_ID not set, Docker monitoring cannot start.")


    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f"DockerFeatures Cog loaded and ready.")
        await self.initialize_docker_monitoring()

    @commands.command(name='control_panel', help='顯示 API 控制面板')
    async def control_panel(self, ctx: commands.Context):
        """發送帶有控制按鈕的訊息"""
        view = ControlPanelView(timeout=300) # 增加超時時間
        # view.message = await ctx.send(f"`{WATCH_CONTAINER}` 控制面板:", view=view) # 如果需要在 on_timeout 中編輯
        await ctx.send(f"`{WATCH_CONTAINER}` 控制面板:", view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(DockerFeatures(bot))
