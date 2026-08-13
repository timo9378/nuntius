# nuntius

在 Discord 和 GitHub issue 之間傳話的機器人。

在 Discord 開一個開發任務,它幫你在 GitHub 開 issue;之後那個討論串裡講的每一句都會同步成 issue 留言,GitHub 上的回覆也會回到討論串裡。任務結束時整段對話打包成 Gist、貼回 issue、關單。

名字是拉丁文的「信使」。

## 它做什麼

| 指令 | 做的事 |
| --- | --- |
| `/login` | 綁定你的 GitHub 帳號。綁了之後你在 Discord 講的話會以**你的**身分出現在 GitHub,沒綁就以 bot 的身分代發。 |
| `/issue <任務> [repo] [labels] [milestone]` | 在公告頻道貼出任務,附「建立 Issue」「我想協作」「我有問題」三顆鈕。標籤用逗號分隔;標籤和里程碑都是先查再套,打錯的會被略過而不是讓整張單開不出來。 |
| `/link <編號> [repo]` | 把 GitHub 上**已經存在**的 issue 接進來,建公告和討論串。給那些不是從 Discord 開的單用的。 |
| `/close [說明]` | 在任務討論串裡執行。算總耗時、把整串對話存成私密 Gist、在 issue 留下連結、關閉 issue、封存討論串。給了說明就多發一則留言,沒給就只是關單。 |
| `/milestone [標題]` | 在討論串裡設定對應 Issue 的里程碑,留空就是拿掉。標題會先查再套,打錯會列出現有的給你看。 |
| `/resync [repo] [limit]` | 管理者限定。把儲存庫裡所有開著的 issue 一次接進來,已經有討論串的會跳過,所以重跑是安全的。**不會刪任何東西。** |
| `/reopen [理由]` | 反過來。重新開啟 issue,討論串解除封存、公告改回「開發中」。給了理由就多發一則留言,並且貼回討論串讓其他人看得到。 |

`/close` 和 `/reopen` 只負責改 GitHub 上的狀態,Discord 這邊的封存和公告是等 GitHub 的 webhook 回來才動的 —— 跟直接在 GitHub 網頁上按關閉走同一條路,兩邊不會各做各的。

雙向同步是自動的,不用下指令:

- **Discord → GitHub** — 討論串裡的訊息(含附件)變成 issue 留言。圖片用 markdown 語法送出,GitHub 會自己抓下來快取,所以 Discord 的 CDN 連結過期之後圖還在
- **GitHub → Discord** — issue 的新留言回到討論串。GitHub 編輯器貼的圖是原始 HTML,會被拆成 Discord 看得懂的形式
- **在 GitHub 開的新 issue** — 自動在公告頻道長出一張卡和一個討論串,不用手動 `/link`
- **論壇頻道** — `DEV_ANNOUNCE_CHANNEL_ID` 指向論壇的話就用論壇的方式:一則貼文一張單,GitHub 標籤對應到論壇標籤(`enhancement` → `Feature` 這類名字對不上的有別名表)
- **狀態** — issue 關閉時討論串封存、公告改成「已完成」並附上耗時;重新開啟則反過來
- **標籤** — 雙向。GitHub 加減 label 會改論壇貼文的標籤,反過來也是
- **里程碑** — 顯示在卡片上,GitHub 那邊改了會跟著更新
- **互相連結** — 在 Discord 貼別的討論串連結,到 GitHub 變成 `#12` 引用(所以兩張單的時間軸都看得到);GitHub 的 `#12` 到 Discord 變成討論串連結
- **刪除** — 雙向。刪掉 Discord 訊息會刪掉對應的 GitHub 留言,反過來也是

雙向的部分靠「結果已經一樣就不動」來收斂,而不是記住自己剛做過什麼。改一次會有一次回音,而回音發現狀態已經對了就停下 —— 沒有要維護的狀態,而且 bot 沒開著的時候別人改的東西,下次也對得起來。

**討論串一律封存,不會刪除。** Gist 和 issue 留著紀錄,但刪掉討論串會連帶讓所有貼過的連結失效,而且沒有復原。封存不用成本,想繼續談在裡面發言就會自動解除。

## 跑起來

```bash
cp .env.example .env    # 填一填,每一項都有說明
docker compose up -d
```

需要一個能被 GitHub 連到的公開 HTTPS 網址,指向這個容器的 8080 埠。兩個地方會用到它:

1. **GitHub OAuth App**(<https://github.com/settings/developers>)—— callback 設成 `https://你的網域/github/callback`
2. **Repo 的 webhook** —— payload URL 設成 `https://你的網域/github/webhook`,content type 選 `application/json`,secret 填 `GITHUB_WEBHOOK_SECRET`,事件勾 **Issues** 和 **Issue comments**

沒設 webhook 的話 GitHub → Discord 那個方向就是不會動,而且不會有任何錯誤訊息 —— 只是安靜地不同步。

## 資料

兩個 JSON 檔,都在 `data/`:

- `thread_issue_mappings.json` —— 討論串 ↔ issue 的對應。**這個掉了,所有進行中的討論串就跟 GitHub 斷了。**
- `user_github_mappings.json` —— OAuth token。掉了大家要重新 `/login`。

compose 已經把 `./data` 掛進去了。備份的話備這個目錄。

## Docker 監控(選用)

`WATCH_CONTAINER` 留空的話,這一整個 cog 不會載入,也不需要下面的東西。

要用的話:

```bash
docker compose --profile docker-watch up -d
```

會多起一個 `docker-socket-proxy`。bot 本身**不會**碰 `/var/run/docker.sock` —— 它只跟 proxy 講話,而 proxy 只放行 `GET /containers/*`。夠拿日誌和狀態,不夠 start、stop、exec 或跑 privileged container。這切斷了「bot 被 RCE → 主機 root」那條路。

功能:容器啟動/停止/被移除時通知、日誌裡的 4xx/5xx 和 error 行轉發、`!control_panel` 的重啟鈕。

重啟鈕預設是關的,要開得**同時**做兩件事:設 `ALLOW_RESTART=1`,並讓 proxy 允許 `POST=1`。分成兩個開關是故意的 —— 開了就等於把上面那條路重新打開,不該手滑。

## 這包從哪來

原本是 [ntust-im-iov/Discord-Bot](https://github.com/ntust-im-iov/Discord-Bot),某個專題的伺服器機器人。搬過來時改了這些:

- OAuth callback 和 GitHub webhook **從外部的 API 服務搬進 bot 本身**。原本那兩塊住在專題的 FastAPI 裡,所以 bot 沒辦法自己部署。現在跑一個容器就完整了,而且沒有新增相依 —— `aiohttp` 本來就是 discord.py 用的。
- 修好 issue → 討論串的查表。原本它讀的是存 OAuth token 的那個檔案去找 `issue_number`,而那個檔案裡沒有這個欄位;而且它拿 `int` 去跟 `str` 比。現在讀對檔案、比對型別,也一併比對 repo,免得兩個專案的 #42 互相認親。
- webhook 簽章改成只認 SHA-256。GitHub 為了相容還是會送 SHA-1 的 `X-Hub-Signature`,接受它等於讓偽造者自己挑比較弱的那個。
- 加了迴圈防護。bot 把 Discord 的話貼到 GitHub,GitHub 再通知我們有新留言 —— 沒擋的話每句話都會變兩句,而且複製品掛在 GitHub 名下。
- 容器名稱改成設定,不再寫死;整個 Docker cog 沒設就不載入。
- 重啟鈕改走 Docker API。原本是 `subprocess` 呼叫 `docker` 指令,而 image 裡根本沒有那個執行檔。
- 對應檔案改成先寫暫存再 rename。中途斷電原本會留下截斷的檔案,下次啟動只會記一行 JSON 錯誤然後從零開始 —— 一次掉光所有討論串的對應。
