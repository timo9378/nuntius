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
| `/status <欄位值> [field]` | 移動這張單在 GitHub Projects 看板上的位置。選項會自動完成,直接列出看板自己的欄位值。預設動 `Status`,`Priority` 和 `Size` 也可以。 |
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
- **標題和內文** — 雙向。在 GitHub 改標題,討論串跟著改名、卡片跟著改;把討論串改名,issue 的標題跟著改。內文改了卡片也重寫(含圖表和表格)
- **PR 的 review** — approve 和「要求修改」會進討論串並點名發 PR 的人;程式碼上的行內 review 留言也會,附上檔名和行號。GitHub 唯一有真 threading 的地方就是這裡(`in_reply_to_id`),所以那些回覆到 Discord 是**原生回覆**,不用靠連結繞
- **PR 的生命週期** — 草稿轉正式、轉回草稿、被請求 review(會點名)。PR 的標籤、負責人、里程碑也會更新卡片 —— 這些發的是 `pull_request` 事件而不是 `issues`,以前整條沒接上,所以 PR 的卡片建好之後就是死的
- **CI** — **只報失敗**,而且點名發 PR 的人,附上執行的連結。綠燈不報:預期中的結果每次都講,只會訓練大家忽略這個頻道,而那正是紅燈需要的注意力。取消的執行也不報,那幾乎都是故意的
- **標籤** — 雙向。GitHub 加減 label 會改論壇貼文的標籤,反過來也是
- **里程碑** — 顯示在卡片上,GitHub 那邊改了會跟著更新
- **互相連結** — 在 Discord 貼別的討論串連結,到 GitHub 變成 `#12` 引用(所以兩張單的時間軸都看得到);GitHub 的 `#12` 到 Discord 變成討論串連結
- **刪除** — 雙向。刪掉 Discord 訊息會刪掉對應的 GitHub 留言,反過來也是
- **圖表** — GitHub 的 ` ```mermaid ` 在 Discord 渲染成圖片附上去。Discord 的 markdown 根本不看 fence 的語言,所以不畫成圖就只是一堆箭頭。見下面〈圖表〉
- **表格** — Discord 的 markdown 沒有表格,`|` 對它是普通字元。所以表格會先排成等寬再量寬度:放得下就包進 code block,放不下就攤成條列。見下面〈表格〉
- **回覆** — 雙向,但兩邊的形狀不一樣。Discord 回覆某則訊息,到 GitHub 變成引用區塊加上那則留言的永久連結;而 GitHub 留言裡只要有這種連結,到 Discord 就變成真正的回覆。GitHub 的 issue 留言沒有回覆功能(只有 PR 的 review 留言有 `in_reply_to_id`),連結是唯一能兩邊都表達「在回誰」的東西

雙向的部分靠「結果已經一樣就不動」來收斂,而不是記住自己剛做過什麼。改一次會有一次回音,而回音發現狀態已經對了就停下 —— 沒有要維護的狀態,而且 bot 沒開著的時候別人改的東西,下次也對得起來。

**討論串一律封存,不會刪除。** Gist 和 issue 留著紀錄,但刪掉討論串會連帶讓所有貼過的連結失效,而且沒有復原。封存不用成本,想繼續談在裡面發言就會自動解除。

## 跑起來

```bash
cp .env.example .env    # 填一填,每一項都有說明
docker compose up -d
```

需要一個能被 GitHub 連到的公開 HTTPS 網址,指向這個容器的 8080 埠。兩個地方會用到它:

1. **GitHub OAuth App**(<https://github.com/settings/developers>)—— callback 設成 `https://你的網域/github/callback`
2. **Repo 的 webhook** —— payload URL 設成 `https://你的網域/github/webhook`,content type 選 `application/json`,secret 填 `GITHUB_WEBHOOK_SECRET`,事件勾這六個:

   | 事件 | 沒勾的話 |
   | --- | --- |
   | Issues | GitHub → Discord 整個方向都不會動 |
   | Issue comments | 留言不會回到討論串 |
   | Pull requests | PR 不會有卡片,標籤和負責人也不會更新 |
   | Pull request reviews | approve 和「要求修改」在 Discord 看不到 |
   | Pull request review comments | 程式碼上的行內 review 看不到 |
   | Workflow runs | CI 失敗不會通知 |

   沒勾的那些不會有任何錯誤訊息 —— 只是安靜地不同步。

完全沒設 webhook 的話,GitHub → Discord 整個方向都不會動。

## GitHub Projects（選用）

`/status` 需要 `GITHUB_BOT_TOKEN` 除了 `repo` 之外還有 `project` scope。Projects v2 **只有 GraphQL API**,REST 完全沒有,所以 PyGithub 碰不到 —— `projects.py` 是為此寫的最小實作。

用的是 bot 的 token 而不是每個人自己的:移動卡片是庶務,而 Projects 需要的 scope 超出 `/login` 要求的範圍,逼所有人重新授權會讓最常用看板的人反而用不到。

**反方向(在看板上拖動 → Discord 更新)還沒做。** Projects v2 的變更沒有儲存庫層級的 webhook,`projects_v2_item` 是組織層級的事件,要在組織設定裡另外掛一個。

## 圖表

Discord 不會渲染 ` ```mermaid `,而且不會有那一天 —— 它的 markdown 完全不看 fence 的語言。所以圖表得先變成圖片。畫圖要跑 mermaid.js,而 mermaid.js 是瀏覽器函式庫,所以這件事需要一整個帶 chromium 的容器,`docker compose up -d` 會一起把它帶起來。

**它是自架的,而且沒有對外網路。** 不是用 mermaid.ink 或 kroki.io 那些公開服務 —— 私有儲存庫裡的圖表就是它的架構,指向公開服務等於把每一張都交出去。`mermaid-net` 設了 `internal: true`,所以那個容器連不到網際網路;它也沒有任何密鑰、沒有對外開埠,只有 bot 連得到它。這是因為它拿有儲存庫權限的人寫的東西餵給瀏覽器引擎,是這包裡最值得關起來的一塊。

想省掉這個容器的話,把 `MERMAID_URL` 清空,圖表就會照舊以原始碼的樣子出現 —— 跟這個功能存在之前一樣。要指向公開服務就填 `https://mermaid.ink`,但上面那段請先讀完。

渲染不出來的時候(服務掛了、語法錯了、圖太大)都會退回原始碼,不會把圖表弄丟。

`MERMAID_BACKGROUND` 別留空:渲染器的預設輸出是透明底,深色線條配透明底在深色的 Discord 上等於看不見。預設值就是 Discord 深色主題的訊息底色。

**已經貼出去的訊息不會回頭重畫。** 同步是 webhook 驅動的,沒有補畫機制。要救舊留言,在 GitHub 上編輯它一下存檔就會重畫 —— `issue_comment.edited` 有接。但 issue 本文那張卡例外,webhook 沒接 `issues.edited`,所以卡片的內文從建立之後就不會再更新。

## 表格

Discord 的 markdown 沒有表格語法,`|` 對它就是普通字元。所以一張 GitHub 表格會變成一串純文字,再被中文字的寬度撐到亂折。這個沒有「修好」的選項,只有換一種形狀。

兩種形狀,選哪個是量出來的不是挑的:

- **排齊,包進 code block** —— 保住網格,而網格就是表格存在的理由。但只在每一行都放得下時成立:Discord 的 code block 是折行不是橫向捲動,折一行就毀掉所有行的對齊。
- **攤成條列** —— 失去網格,三欄以上還得重複欄名。但折行的條列就只是折行的條列,沒有對齊可以被破壞。

所以先排齊、量寬度,放得下才留著。門檻 `TABLE_WIDTH` 預設 38 —— 是手機的寬度,不是桌機的。這個不對稱是刻意的:放得下的網格只比條列好一點,放不下的網格比條列差很多,所以要對最窄的客戶端負責。

寬度以顯示格數計,中文字算兩格(`unicodedata.east_asian_width`)。用 `len()` 會讓每一張有中文的表都歪掉,也就是全部。

兩件沒有保證的事,先講:

- **等寬的對齊是近似的。** 中文字在 Discord 的等寬字型裡不保證剛好是英數字的兩倍寬,那取決於它 fallback 到哪個字型。多數 CJK 等寬字型是準的,但這件事沒辦法從程式這邊驗證。
- **38 是估計值。** Discord 沒有公開訊息區的字元寬度,而且它隨視窗、字型縮放、有沒有開側欄而變。看不對就調 `TABLE_WIDTH`。

程式碼區塊裡的表格不會被動 —— 放在那裡是刻意的,而且 shell 的 pipeline 不是表格。沒有 `| --- |` 分隔列的東西也不算表格,不然任何含有 `|` 的句子都會被當成一列。

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
