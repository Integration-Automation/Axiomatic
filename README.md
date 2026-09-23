# Axiomatic

自動產圖 RPA。**兩個長駐行程**——一支用瀏覽器自動化跑批次產圖，一支 Discord bot
遠端編輯佇列與啟停排程——兩者**只透過磁碟檔溝通**，永遠不互相 import。

> 📖 **完整文件（Sphinx / Read the Docs 格式，繁體中文）在 [`docs/`](docs/index.md)**
> ・**架構總覽**在 [`architecture.md`](architecture.md)
> ・**指令總表**在 [`COMMANDS.md`](COMMANDS.md)
> ・**逐指令參考（一個指令群一檔，含參數、值域與權限）在
> [`commands/`](commands/README.md)**
>
> 本機建置文件：
> `pip install -r docs/requirements.txt && py -3 -m sphinx -b html docs docs/_build/html`

> **撰寫慣例**：散文以泛稱指涉外部相依（出圖服務／目標網站／網頁介面／後端 AI），
> 檔名、指令名、設定鍵、程式符號則照實寫。

---

## 目錄

- [架構速覽](#架構速覽)
- [快速開始](#快速開始)
- [相依套件](#相依套件)
- [設定檔](#設定檔)
- [佇列與 prompt 檔](#佇列與-prompt-檔)
- [Discord bot 指令](#discord-bot-指令)
- [背景產圖行為](#背景產圖行為)
- [監督與重啟](#監督與重啟)
- [狀態鏡像與 Rich Presence](#狀態鏡像與-rich-presence)
- [開發](#開發)
- [檔案結構](#檔案結構)

---

## 架構速覽

```
         使用者（Discord）
                │
                ▼
   ┌────────────────────────┐        ┌──────────────────────────┐
   │  discord_bot.py        │  磁碟  │  webrunner_novelai.py    │
   │  （指令 / 佇列編輯 /   │◄──────►│  或 webrunner_je_only.py │
   │    監看 / 產圖排程）   │  檔案  │  （瀏覽器批次產圖）      │
   └────────────────────────┘        └──────────────────────────┘
```

- **bot** 是意圖層：接指令、編輯佇列檔、spawn 並監督 webrunner、回報結果。
- **webrunner** 是執行層：開瀏覽器、登入、填提示詞、按產生、下載圖檔。
- **中間只有檔案**。任一邊重啟、崩潰或被殺，另一邊都不會壞——狀態全在磁碟上。

唯一被允許的第三條路是**被動共用模組**（`_batch_config` / `_queue_consume` /
`_webrunner_shared` …）：兩邊都可以 import 它們，因為它們是獨立的純模組。

### 元件

**長駐主體**

| 檔案 | 用途 |
|---|---|
| `axiomatic/discord_bot.py` | Discord bot：13 個頂層 slash 指令 ＋ 25 個指令群（合計 268 個斜線子指令）、webrunner 監督、Dorossi 編排、presence、單張產圖佇列 |
| `axiomatic/webrunner_novelai.py` | 批次產圖器（Selenium 變體，正式預設） |
| `axiomatic/webrunner_je_only.py` | 批次產圖器（wrapper 變體，備援；`/run` 先試這支） |
| `axiomatic/_webrunner_shared.py` | 兩支變體的共用核心：DOM 操作 ＋ 純函式 ＋ 批次主迴圈（driver-agnostic） |

**被動共用模組**（bot 與 webrunner 皆可 import）

| 檔案 | 用途 |
|---|---|
| `_batch_config.py` | `batch_config.json` 載入／原子寫回（webrunner 每個角色熱重載） |
| `_bot_config.py` | `bot_config.json` 載入（bot ＋ 兩支啟動器 ＋ dashboard） |
| `_queue_consume.py` | 動態佇列消耗決策、`pair_todos` 配對、`end` 哨符（純函式） |
| `_run_progress.py` | 角色中斷續跑檢查點 |
| `_supervisor.py` | 純退避數學 `restart_backoff` |
| `_chrome_slot.py` | 跨行程「單一瀏覽器槽」諮詢鎖 |
| `_power_request.py` | 參考計數的電源要求（待命時不暫停本行程；bot 在批次監督期間持有） |
| `_connectivity.py` | 「主機連得上網路嗎」——兩個批次監督者用它分辨斷網與崩潰 |

**bot 側模組**

| 檔案 | 用途 |
|---|---|
| `_process_control.py` | 行程探查／終止原語（PID 存活、psutil 掃描、全機清掃） |
| `_gui_control.py` | 桌面自動化的門面：參數解析、環境政策、錯誤去識別化。實作全在函式庫端，這裡不重寫 |
| `_external_apis.py` | 外部圖庫／web API 抓取與解析（含模糊 tag 解析器） |
| `_help_strings.py` | 說明文字純資料（限頻道與跨頻道兩套 × 三語） |
| `_bot_prompts.py` | `bot_prompts/` 外部化 prompt 文字載入器 |
| `dorossi_backend.py` | 後端 AI 叫用、工作階段持久化、看門狗、用量／壓縮判定 |
| `presence_probe.py` | 本機 OS 探測（遊戲／音樂／開發工具） |
| `discord_rpc.py` | 自製本機 Rich Presence client（純 stdlib IPC） |

**啟動器與工具**

| 檔案 | 用途 |
|---|---|
| `start_discord_bot.py` | bot 監督啟動器（無限重啟 ＋ 單一實例保護） |
| `start_webrunner.py` | webrunner 監督啟動器（退避重啟 ＋ 快速失敗放棄）；預設 selenium 變體，帶 `je` 換變體 |
| `run_batch.py` | 一鍵批次入口：前置檢查 → 印 run-plan → 交棒給 `start_webrunner.py` |
| `install_autostart.py` | 在 Windows 工作排程器註冊兩支監督者，登入時自動啟動（`--install`／`--remove`／`--status`） |
| `wake_autostart.py` | 直接叫醒工作排程器裡已註冊的監督者：預設 bot 與批次都叫，`--bot-only`／`--batch-only` 只叫一支；已經在跑的略過。bot 沒有監督者、`/sys restart` 只會把它關掉時用這支拉回來 |
| `axiomatic/dashboard_server.py` | 本機唯讀狀態儀表板（stdlib HTTP） |
| `axiomatic/verify_browser.py` | 隔離瀏覽器驗證（smoke / full，opt-in） |

---

## 快速開始

### 1. 安裝相依

```powershell
py -3 -m pip install -r requirements.txt
```

### 2. 取得瀏覽器自動化套件

`je_web_runner` 由兩支 webrunner 透過 `sys.path` 從**同層目錄的 `WebRunner/`**
（即 `<parent>/WebRunner`）import，或由環境變數 `WEBRUNNER_PATH` 指定：

```
<parent>/
├── Axiomatic/     ← 本 repo
└── WebRunner/       ← 同層 clone
```

### 3. 填憑證

憑證與設定**都不在版本庫裡**——repo 帶的是範本，第一步是複製它：

```powershell
copy auth.example.md               auth.md
copy discord_bot_token.example.md  discord_bot_token.md
copy bot_config.example.json       bot_config.json
```

| 檔案 | 內容 |
|---|---|
| `auth.md` | 出圖服務登入帳密，兩行：`username: ...` / `password: ...`（以第一個冒號分隔） |
| `discord_bot_token.md` | Discord bot token（整個檔案就是 token，或一行 `Token: xxx`） |

不要把它們加回版本庫；commit 時**逐檔 `git add`**，不要 `git add -A`。

### 4. 設定 bot

編輯 `bot_config.json`，至少填兩個值（Discord 設定 → 進階 → 開發者模式打開後，
右鍵就能複製 ID）：

- `channel_id` — 限頻道指令只在這個頻道生效；
- `owner_user_id` — 你自己的使用者 ID，操作主機的指令群與 `/dorossi` 只認它。
  留 `0` 的話那些指令對所有人一律拒絕。

兩個都沒填時 bot 會在啟動時印一段說明並乾淨結束，不是 traceback。

Discord Developer Portal → 你的 Application → Bot → Privileged Gateway Intents，
把 **Message Content**、**Presence** 與 **Server Members** 三個 intent 打開，
否則 bot 啟動會丟 `PrivilegedIntentsRequired`。

完整的逐步安裝說明（含 OAuth2 邀請連結、選用元件）在
[`docs/setup.md`](docs/setup.md)。

### 5. 放佇列內容

最少只要有一個佇列有東西就能跑——其餘三個會自動用 `prompt.md` / `character1.md`
/ `character2.md` / `undesired.md` 當 fallback。這幾個 fallback 一樣不在版本庫裡，
範本是 `prompt.example.md` 之類，複製過來再填：

```powershell
copy prompt.example.md      prompt.md
copy character1.example.md  character1.md
copy undesired.example.md   undesired.md
```

佇列本身通常不用手建——在對話平台用 `/todo char1 add <角色描述>` 就會寫出來。
格式是**一行一筆**、不切逗號；`todo_character2.md` 是位置對應的，空行代表
「這一對不要第二個角色」，要保留。

### 6. 開跑

三種方式，擇一：

```powershell
py -3 start_discord_bot.py     # 起 bot，之後在對話平台打 /run
py -3 run_batch.py             # 本機一鍵批次（不需要 bot）
py -3 start_webrunner.py       # 直接跑監督迴圈（跳過前置檢查）
```

> ⚠️ **不要同時用 bot 的 `/run` 與本機啟動器**——兩邊都會 spawn webrunner、
> 搶同一份瀏覽器設定檔鎖。

（選用）另開一個 console 起儀表板：

```powershell
py -3 axiomatic\dashboard_server.py     # 預設 http://127.0.0.1:8765/
```

儀表板沒有任何認證，所以只認 **IP 位址**或 `localhost` 連進來（這是在擋 DNS
rebinding：外部網頁可以讓自己的網域解析到 `127.0.0.1`，藉此讀走這個埠的內容）。
如果你把 `dashboard.host` 改成非 loopback、又想用**主機名稱**連，要把那個名稱
一併寫進 `dashboard.host`，否則會收到 403；被擋時 console 會印出原因。

---

## 相依套件

| 套件 | 用途 |
|---|---|
| `selenium` | Selenium 變體的瀏覽器驅動 |
| `je_web_runner` | wrapper 變體的瀏覽器驅動 |
| `urllib3` | 兩支變體直接接 `ReadTimeoutError`（selenium 不包這個例外） |
| `discord.py` | bot 事件迴圈 |
| `aiohttp` | 外部 API 呼叫（直接 import，不靠 discord.py 的傳遞相依） |
| `psutil` | 行程存活探測、遊戲白名單比對（**必要**，見「Windows PID 探測」） |
| `je-auto-control` | 桌面自動化的**唯一實作**：滑鼠／鍵盤／視窗／剪貼簿／文字辨識／圖片定位。`_gui_control.py` 只做參數解析與錯誤去識別化，同一件事不在本專案再寫一份 |
| `pytesseract` | `/locate text find\|click\|wait` 的文字辨識 wrapper。**只裝這個 pip 套件不夠**——辨識引擎要另外裝並放進 PATH；沒裝時那三個指令回泛用提示，不會炸 |
| `comtypes` | `/locate ui …` 的元素定位（作業系統層級的 UI 樹）。裝不起來時該組指令回泛用提示，其餘定位方式照常 |
| `Pillow` | `/grid` 拼 2×2 圖 |
| `pyfiglet` | `/fun ascii` |
| `simpleeval` | `/fun calc`（安全運算，不 eval 任意程式碼） |
| `anthropic` | Dorossi 的 `api` 後端（選用；缺了 bot 仍可啟動，該路徑降級回報）。憑證來自主機的 `ANTHROPIC_API_KEY`／`ANTHROPIC_AUTH_TOKEN`；這兩個變數**刻意不交給** CLI 後端的子行程——交給它會讓 CLI 安靜地從訂閱方案改成按用量計費（見 `docs/troubleshooting.md`） |
| `matplotlib` | 舊版 token 圖表 helper 的相容性測試 |

> **`pywin32` 已整個移除**：視窗查詢與操作全部轉呼叫桌面自動化
> 函式庫，本專案不再有任何 `import win32*`。要再用到 Win32 的東西，補進函式庫，
> 不要在這裡長回來（`test_gui_facade.py` 有守門）。

---

## 設定檔

### `batch_config.json` — 產圖批次參數

**熱重載**：webrunner 在**每個角色迴圈開頭重讀**，改了不必重啟，下個角色就生效。
bot 在 `/gen progress` / `/eta` / `/gen preview` 讀。可用 `/config show` /
`/config set` / `/config reset` 直接改。

| 鍵 | 預設 | 說明 |
|---|---|---|
| `images_per_character` | 240 | 每個角色產幾張 |
| `inter_image_delay_sec` | `[20, 30]` | 圖間隨機延遲（秒）。兩值都建議 > 5，避免被限流 |
| `schedule_limit_hours` | 16 | 工作時數門檻。**只在角色與角色之間檢查**，且計時包含等額度的時間，所以實際工作視窗會比這個值長（量過一輪：16 → 24.5 小時） |
| `rest_hours` | 6 | 休息時數，之後計時器歸零繼續。`0` = 不休息、只歸零 |
| `generate_max_retries` | 4 | 單張產生失敗的重試次數 |
| `generate_retry_delay_sec` | `[25, 30]` | 產生失敗後的退避（與正常圖間延遲分開） |
| `download_max_retries` | 3 | 下載失敗的重試次數 |
| `consecutive_fail_abort` | 10 | 連續失敗到此數就 raise，讓監督者重生瀏覽器 |
| `restart_chrome_every_n_characters` | 1 | 每 N 個角色重啟瀏覽器沖記憶體；0 = 停用 |
| `min_save_ratio` | 0.9 | 存到目標張數的多少比例才算完成、pop 佇列 |
| `debug_screenshots` | `false` | 是否寫 `debug_*.png`。**webrunner import 時讀一次**，改了要 `/stop` ＋ `/run` |

### `bot_config.json` — bot runtime 設定

開機時 load，多數值改了要 `/sys restart`；roles / alert / disk / launch /
daily report / dashboard 可用 `/config reload` 熱重載。

| 分組 | 鍵 |
|---|---|
| 基本 | `channel_id`、`target_presence_username`、`default_help_lang`（`zh-tw` / `zh-cn` / `en`）、`presence_probe_interval_sec`、`event_poll_seconds` |
| 告警 / 閘門 | `alert_user_id`（0 = 不 ping）、`min_free_disk_gb`（0 = 停用） |
| 電源 | `keep_bot_awake`（預設 `true`：批次監督期間與 Dorossi 工作進行中 bot 持有電源要求，主機待命時 bot 不會被暫停；保的是 bot 這個行程，不是讓主機不睡，用電池時系統會在睡眠逾時後 5 分鐘收回） |
| 路徑顯示 | `path_reveal_channel_ids`（可顯示完整主機路徑的頻道 ID 清單；空 = 只有 1:1 私訊看得到完整工作目錄，其餘表面只給末段目錄名） |
| 監督者 | `webrunner_supervisor.{fallback_window_sec, respawn_backoff_min_sec, respawn_backoff_max_sec, healthy_threshold_sec, rapid_fail_threshold_sec, rapid_fail_giveup_count}` |
| 權限 | `user_roles.{admin_user_ids, operator_user_ids, viewer_user_ids}`（皆空 = 沿用純頻道閘） |
| GUI 控制 | `gui_control.{launch_whitelist, launch_aliases}`（兩者皆空 = `/proc launch` 全面停用） |
| 其他 | `daily_health_report.{enabled, channel_id, time}`、`dashboard.{host, port}` |
| Dorossi | `dorossi_backend`、`dorossi_cc_tools`、看門狗上限、壓縮門檻、並行上限（見 [Dorossi 段落](#dorossi-僅限特定-uid)） |

**兩個載入器的共同契約**：缺檔／格式錯誤 → 回傳預設值，**絕不 raise**；
鍵存在但型別錯／超出範圍 → 用該鍵的預設並印一行 stderr 警告。

### presence 設定（皆 hot-reload，每次 probe 重讀）

| 檔案 | 內容 |
|---|---|
| `presence_games.json` | 遊戲白名單。key = exe 檔名（lowercase），value = 顯示名稱；`*` 前綴 = priority（多遊戲同跑時贏） |
| `presence_music.json` | 音樂偵測規則（媒體來源白名單、瀏覽器 AUMID hint、視窗標題 hint、前景視窗 regex）。regex 壞掉會跳過該條並印 stderr |
| `presence_rpc.json` | 本機 Rich Presence 設定 ＋ 開發工具偵測開關。`enabled:false` 或無 `client_id` → 整段停用 |

三個檔都用 `utf-8-sig` 讀，所以存成帶 BOM 的 UTF-8 也不會整份解析失敗。

### `bot_prompts/` — 外部化 prompt 文字

10 個純文字檔（Dorossi 人設、自走迴圈各段守則、單張產圖的預設畫風後綴），
**開機時讀取一次**。缺檔／空白／讀取出錯一律回退到程式內建預設值，所以 fresh clone
即使目錄不完整也一定能啟動。

---

## 佇列與 prompt 檔

### 佇列 / fallback 對照

| 佇列 | fallback | 說明 |
|---|---|---|
| `todo_prompt.md` | `prompt.md` | 主提示詞 |
| `todo_character1.md` | `character1.md` | 角色 1 |
| `todo_character2.md` | `character2.md` | 角色 2 |
| `todo_undesired.md` | `undesired.md` | 負面提示詞 |

另有 `default_prompt.md`（範本，`/todo prompt default` 一鍵推進佇列）與選用的
`templates/<名稱>.md`（`/todo prompt template <名稱>`；目錄不存在時指令會提示建立）。

### 配對規則

四個佇列配對成四元組。較短的清單用最後一筆重複填充；空清單用 fallback 填補：

| todo_prompt | todo1 | todo2 | 跑的組合 |
|---|---|---|---|
| `[P]` | `[a, b, c]` | `[x, y, z]` | `(P,a,x) (P,b,y) (P,c,z)` |
| `[P, Q]` | `[a, b, c]` | `[x]` | `(P,a,x) (Q,b,x) (Q,c,x)` |
| *(空)* + `prompt.md` | `[a, b, c]` | `[x]` | `(fb,a,x) (fb,b,x) (fb,c,x)` |
| `[P]` | `[a, b, c]` | *(空)* + `character2.md` | `(P,a,fb) (P,b,fb) (P,c,fb)` |

**fallback 不被 pop**：`prompt.md` / `character1.md` / `character2.md` /
`undesired.md` 被當 fallback 使用時，跑完一組**不會**從佇列移除任何東西——fallback
是持久預設。

**真實的角色 2 條目是一次性的**：`todo_character2.md` 抽乾之後，後續組合拿到的是
空的角色 2（或設定的 fallback），**不會**重複沿用最後一筆。

### 檔案 parser 規則

四個 todo 檔共用同一個 parser：

| 規則 | 行為 |
|---|---|
| 分隔符 | **只有換行**。條目內可以合法包含 `,` `，` `::` `(` `)`——絕不會被切碎 |
| 空白行 | 跳過…… **但 `todo_character2.md` 例外**（見下） |
| NBSP (`\xa0`) | 讀寫時都自動換成普通空格，送出的提示詞不含隱形垃圾 |
| 寫入 | 每筆一行；非空時結尾恰好一個換行，空清單寫成空檔 |

> ⚠️ **`todo_character2.md` 是位置性的**：它的空行**有意義**——代表「該配對移除／
> 停用角色 2」，所以讀寫時都必須保留。其他三個佇列的空行仍然無效、會被跳過。

### `end` 終止標記

`todo_prompt.md` 裡某一行若是 `end`（不分大小寫、前後空白忽略），webrunner 跑到那筆
時就**停止後續產圖**——該筆與其後全部都不產，乾淨結束（rc=0，不觸發監督者重啟）。

處理方式是**只消耗 `end` 這一行**（從佇列 pop 掉），保留它後面的條目；下次 `/run`
會接著跑 `end` 之後沒做完的部分。只看主提示詞佇列，其他三個不參與判斷。

用 `/todo prompt end` 追加、`/todo prompt unend` 移除全部、
`/todo prompt insert` 插在中間。

---

## Discord bot 指令

指令**一律是原生斜線指令**：打 `/` 就會自動補全，參數在送出前就有型別與值域
檢查。共 **13 個直接指令 ＋ 25 個指令群**（＝37 個頂層
指令，平台上限 100），底下合計 **268 個斜線子指令**。

指令群只佔一個頂層額度、群內子指令不計，所以把低頻指令收進群裡是唯一能長期
擴充的作法。逐項說明看 [`COMMANDS.md`](COMMANDS.md) 或直接打 `/help`。

- 🔒 **限頻道**：只在 `bot_config.json` 的 `channel_id` 所指頻道回應
  （**擁有者可跨頻道**）。
- 🌐 **跨頻道**：bot 看得到的任何頻道都能用。
- 🔑 **限擁有者**：操作 bot 那台電腦的指令，**不看 `user_roles`**。

> 🔑 **主機控制一律只有擁有者能用。** `/input`、`/screen`、`/win`、`/clip`、
> `/locate`、`/macro`、`/watch`、`/proc`、`/host` **整群**受閘（群組制，新增
> 子指令自動受保護），另加 `/sys restart` 等會動到主機或設定的零散指令。
> 閘門在派發之前，斜線、`!`、mention 三條路徑共用同一組規則，且**排在角色閘
> 之前**——`user_roles` 三份清單都空時（預設）角色閘等於停用，把桌面控制掛在
> 它下面等於沒有保護。

> **所有回覆字串一律泛用**：不揭露服務名、本機路徑、檔名、PID 或原始錯誤文字。
> 詳細內容只寫 stderr / log。

### 🔒 限頻道

| 指令 | 說明 |
|---|---|
| `/eta` | 預估完成時間（有終止標記只算到標記為止） |
| `/latest` | 上傳最近 N 張產出圖 |
| `/queue` | 各佇列剩餘筆數與實際會跑的配對數 |
| `/run` | 啟動背景產圖（可排程：in 90m / at 02:00 / cancel） |
| `/status` | 背景產圖的執行狀態與變體 |
| `/stop` | 停止背景產圖 |

| 家族 | 子指令 |
|---|---|
| **產圖佇列** | `/todo dedupe\|duplicate\|find\|move\|shuffle\|swap`<br>`/todo char1 add\|addx3\|clear\|list\|pop\|remove`<br>`/todo char2 add\|clear\|default\|list\|pop\|remove`<br>`/todo negp add\|clear\|list\|pop\|remove`<br>`/todo prompt add\|clear\|default\|end\|insert\|list\|pop\|remove\|template\|unend` |
| **佇列空時的預設值** | `/preset info`<br>`/preset main append\|clear\|set`<br>`/preset neg append\|clear\|set` |
| **批次控制** | `/gen current\|image\|image_queue\|pause\|plan\|preview\|progress\|resume` |
| **產出檢視** | `/out debug_show\|history\|latest_for\|rate\|sample\|stats` |
| **收藏** | `/fav clear\|list\|remove\|show` |
| **執行紀錄** | `/log clear\|errors\|grep\|size\|tail` |
| **維運與診斷** | `/sys audit\|backfill_paths\|cleanup_debug\|dashboard\|disk\|doctor\|git_pull\|health\|introspect_dom\|metrics\|probe_status\|restart\|undo\|update_check` |
| **行程控制** | `/proc kill\|launch\|list` |
| **批次參數** | `/config reload\|reset\|set\|show` |
| **螢幕** | `/screen all\|gif\|info\|main\|pixel\|region\|text\|window` |
| **視窗** | `/win focus\|grid\|list\|move\|pos\|snap\|state\|wait`<br>`/win layout list\|remove\|restore\|save` |
| **鍵盤與滑鼠** | `/input click\|hotkey\|type`<br>`/input key clear\|down\|press\|status\|up`<br>`/input mouse click\|dclick\|down\|drag\|move\|pos\|scroll\|up` |
| **剪貼簿** | `/clip files\|formats\|image\|paste\|read\|set\|setimage` |
| **畫面定位** | `/locate gone\|pixel`<br>`/locate image click\|find\|wait`<br>`/locate text click\|find\|wait`<br>`/locate ui click\|find\|gone\|read\|tree\|wait` |
| **巨集** | `/macro delete\|edit\|insert\|list\|record\|rm_line\|run\|save\|show\|stop` |
| **主機指令與檔案進出** | `/host get\|panic\|put`<br>`/host job clear\|eof\|list\|log\|run\|send\|stop`<br>`/host sh cd\|run\|stop` |
| **條件監看** | `/watch clip\|job\|list\|pixel\|port\|process\|stop\|text\|ui\|window` |
| **定時排程** | `/schedule add\|list\|remove\|run` |
| **獨立監督者** | `/launcher start\|status\|stop` |

### 🌐 跨頻道

| 指令 | 說明 |
|---|---|
| `/booru` | 圖庫搜圖：tag 隨機一張（可模糊；不帶 tag 用預設圖） |
| `/e621` | furry 取向圖庫隨機一張（預設 NSFW；加 rating:safe 限 SFW） |
| `/grid` | 圖庫最新 4 張拼 2x2 上傳（等同 /booru 同時開 latest 與 grid） |
| `/help` | 指令說明（tw / cn / en） |
| `/iqdb` | 跨圖庫反向圖搜（回前幾名來源＋相似度 %） |
| `/nsfw` | 圖庫搜圖 NSFW 捷徑（自動補 rating:explicit） |
| `/safebooru` | 全站 SFW 圖庫隨機一張 |

| 家族 | 子指令 |
|---|---|
| **對話後端**（🔑 限擁有者） | `/dorossi abort\|ai\|ask\|compact\|effort\|errors\|fullmode\|health\|logs\|model\|retry\|running\|status\|tokens\|workspace_clean`<br>`/dorossi allowdir add\|list\|remove`<br>`/dorossi queue clear\|detail\|failed_clear\|move\|remove\|retry_failed\|show\|undo`<br>`/dorossi session archive\|continue\|delete\|export\|list\|new\|rename\|reset\|switch` |
| **圖庫 tag 工具** | `/tag autocomplete\|count\|suggest\|wiki` |
| **趣味 / 隨機** | `/fun 8ball\|ascii\|calc\|choose\|coinflip\|rand\|reverse\|roll\|rps\|timer` |
| **編碼與小工具** | `/tool base64\|color\|hash\|qr\|say\|unbase64\|urldecode\|urlencode` |
| **資訊與元資料** | `/info avatar\|channel\|ping\|server\|uptime\|version` |
| **公開資料查詢** | `/web anime\|cat\|crypto\|dict\|dog\|fact\|github\|joke\|quote\|wiki\|xkcd` |

### `@bot <文字>` — 自由提問

不接子指令的 mention 是自由提問入口，**刻意保留 mention 形式**而不是改成斜線
指令：一則訊息帶得動多行內容、附件與回覆脈絡，斜線的選項輸入框帶不動；而且
互動 token 只有 15 分鐘，一個回合的上限卻可以到 10800 秒，用斜線撐長回合會在
中途斷掉。空 mention 會回一張預設圖庫圖片。

### Reaction 控制（限指定頻道）

對 bot 上傳的圖按 ⭐ 加入收藏、🗑️ 刪檔。寫入走備份機制，`/sys undo` 可回復。

### 相容性

舊的 `!` 前綴與 `@bot <子指令>` 仍然可用，但**不再是對外介面**、也不寫進文件：
它們留著是為了手機打字、多行貼上與回覆脈絡這些斜線輸入框做不到的情境。新功能
一律只加斜線指令。

---

## 背景產圖行為

### 一次完整 setup（每次啟動與每次瀏覽器重啟都跑）

| 步驟 | 行為 |
|---|---|
| 1. 開瀏覽器 | 用 `.chrome_profile/` 持久化 session、stealth flag、虛擬 user-agent |
| 2. 登入 | session 還在直接跳過；否則模擬人類 typing 登入 |
| 3. 模型 | 切到 `TARGET_MODEL` 指定的目標模型 |
| 4. 角色欄位 | 確保剛好 2 個（多則刪、少則加） |
| 5. 解析度 | Normal Landscape |
| 6. 取樣器 | Sampler=Euler Ancestral、Steps=23、Guidance=6、Guidance Rescale=0（Variety+ 已不存在） |
| 7. 最小化 | 視窗最小化，產圖在背景跑 |

每個步驟最多重試 3 次（`with_retry()`）。主提示詞**不在**這裡填——改為在角色迴圈內、
每組開始前才填（內容跟上次相同就跳過）。

### 動態佇列消耗

批次**不是**啟動時拍一次快照就照跑。每產完一個角色就**重讀磁碟上的四個佇列**，
交給 `_queue_consume.decide` 決定下一步。這表示 run 進行中對「還沒輪到的」佇列所做的
新增／重排／刪除，**這一輪就生效**。

- 未達完成門檻的組合會**保留在佇列前端**、游標（`skip`）越過它，留待下輪重試。
- 真實佇列全空但有 fallback 且本輪還沒產過任何角色 → 只產一個 fallback 角色就收工
  （避免用長度 1 的 fallback 把已抽乾的佇列延長成幽靈角色）。

> bot 的 `/gen plan` / `/gen preview` / `/queue` / `/eta` 是**時間點快照**預覽——它只引用純
> 配對原語，不跑動態消費決策。所以預覽與實際執行在「run 中途編輯佇列」時可能不同，
> 這是刻意的設計。

### 角色迴圈（對每組配對）

| 步驟 | 行為 |
|---|---|
| 1. 填主提示詞 | 跟上次相同就跳過 |
| 2. 填角色 1 提示詞 | — |
| 3. 填角色 2 提示詞 | 跟上次相同就跳過；空條目代表停用角色 2 |
| 4. 填負面提示詞 | 跟上次相同就跳過 |
| 5. 產 N 張 | `images_per_character`，每張間隔 `inter_image_delay_sec` 隨機 |
| 6. 下載 | 用頁內 `fetch` 抓 blob 編碼回 Python，寫到 `output/<角色>/<角色>_NNNN_時間.png` |
| 7. 達門檻才 pop | 見下 |

### 完成門檻與 pop

存到 `ceil(images_per_character × min_save_ratio)` 張（預設 90%）才算完成、才把該筆
從佇列移除。

**為什麼不是要求精確相等**：`generate_loop` 一定會走完整個張數，只有渲染器崩潰或
連續失敗中止才會 raise（那兩種都繞過 pop）。所以正常返回就代表角色跑完了，但零星的
暫時性失敗可能讓 saved 差一點點（例如 239/240）。要求精確相等會讓該筆留在佇列裡、
下輪整個角色重產，佇列計數也會逐漸失準。

### 中斷續跑

批次中途被打斷（`/stop`、崩潰、瀏覽器重生）時佇列**不會**被 pop，所以下次啟動的第一
組正好就是被打斷的那組。`_run_progress` 把「進行中那組」的完整身分寫成一個小檢查點
（`webrunner_progress.json`），下輪認出「同一組」就**續用同一個輸出資料夾**、只補產
還缺的張數，而不是配一個 `<name>_2` 從第 1 張重來。

- 四個欄位（主提示／角色 1／角色 2／負面）**全等**才算同一組——佇列被編輯過就不續跑，
  從頭開始（否則圖會混進錯的資料夾）。
- 角色 pop（完成）當下**立即清除**檢查點——這讓「相鄰完全相同的提示詞」安全。
- 損毀的檢查點一律降級為「不續跑」，絕不讓一輪崩潰或卡住。
- `/run` 啟動時會印出續跑判定；沒續跑時會逐欄列出歧異在哪（佇列被編輯？fallback
  翻轉？夾帶 NBSP？）。

### 定期重啟瀏覽器

目標網站是單頁應用，數百次產生後渲染器記憶體會累積到崩潰（低記憶體機器上接著連新
driver 都建不起來）。所以每 `restart_chrome_every_n_characters` 個角色，在**角色邊界**
（不是產圖中途）重啟瀏覽器並重跑完整 setup，成本約 30–60 秒。

> 這救不了「單一角色自己跑到一半就 OOM」——那要調低 `images_per_character`。

### 失敗處理

| 觸發條件 | 動作 |
|---|---|
| 點產生後圖片來源沒變化 | 退避 `generate_retry_delay_sec` 後重試，最多 `generate_max_retries` 次 |
| 下載失敗 | 短退避後重試，最多 `download_max_retries` 次 |
| 連續失敗 5 次 | 發 `consecutive_failures` 告警事件（不中止） |
| 連續失敗 `consecutive_fail_abort` 次 | raise，讓監督者從頭重生瀏覽器 |
| 偵測到渲染器崩潰頁 | 先試重新整理救回；救不回就中止該輪 |
| 累計執行 > `schedule_limit_hours` | 跑完當前角色後休息 `rest_hours`，計時器歸零繼續。休息期間仍服務單圖請求與暫停標記 |

### 回傳碼

| rc | 意義 |
|---|---|
| `0` | 乾淨收工（佇列跑完 / `end` 哨符 / 單張伺服器閒置逾時）。監督者**不**重生 |
| `1` | 前置檢查判定無事可做（不開瀏覽器） |
| `2` | session setup 失敗 |
| `3` | 零產出 backstop——走完整輪卻一張都沒存，讓監督者重生瀏覽器 |

### 事件串流

webrunner 把進度寫進 `events.ndjson`，bot 輪詢後回報頻道：
`character_start`、`character_done`、`chrome_restart`、`consecutive_failures`、
`critical_error`、`dom_result`、`paused`、`resumed`、`single_image_done`、`todo_done`。

---

## 監督與重啟

三個監督者，策略對齊、參數共用同一份 `bot_config.json` → `webrunner_supervisor`：

| 監督者 | 監督對象 | 策略 |
|---|---|---|
| `start_discord_bot.py` | `discord_bot.py` | 無限重啟。退避 5s → 300s 上限；上次活滿 60s 就 reset。子行程回報「已經有另一個實例在跑」時**不重試**，直接收工（重試永遠不會成功） |
| `start_webrunner.py` | webrunner 變體 | 退避重啟 ＋ **快速失敗放棄**：連續 `rapid_fail_giveup_count` 次在 `rapid_fail_threshold_sec` 內崩潰就停手（避免「瀏覽器根本起不來 → 無限重生 → 一直重做首次登入」）。**斷網不算**：失敗當下主機連不上網路，那一次不計數，等網路回來就立刻重生、從進度接續（沒有等待上限，Ctrl+C 或 `/launcher stop` 結束） |
| bot 的 `_watch_for_fallback` | bot 自己 spawn 的 webrunner | **兩段**：(a) 先試 wrapper 變體，5 分鐘啟動視窗內非零退出就自動切到 selenium 變體並公告；(b) 視窗過後同一變體崩潰就退避重生。`/stop` 或 rc=0 退出監督。**另外**：bot 重啟後會把還在跑的批次接回監督（獨立監督者在跑時讓位）；失敗當下主機斷網就停下來等，網路回來自動從進度接續並在頻道說一聲（沒有等待上限，`/stop` 取消）；監督期間持有電源要求（`keep_bot_awake`） |

三個啟動器的**直譯器探索順序**都是：本地 `.venv` → `py -3` → `sys.executable`。
fresh clone 依賴這個順序，**不可更動**。

### 單一實例保護

兩層，各鎖各的檔案（OS 檔案鎖，行程一消失就自動釋放，硬砍也不會留下殘留）：

| 誰 | 鎖檔 | 擋掉什麼 |
|---|---|---|
| `start_discord_bot.py` | `.discord_bot_supervisor.lock` | 啟動器被點兩次 |
| `discord_bot.py` | `.discord_bot.lock` | **繞過啟動器直接執行 bot 本體** |

第二個實例會印一行說明後結束，不會安靜地跑起來。兩個實例同時跑不會有明顯徵兆
——兩套各自跑背景任務、各自消耗佇列，而且都「看起來正常」。

> 用 `.venv` 執行時，工作管理員裡每個 Python 行程都會**成對出現**（一個轉接殼、
> 一個本體，命令列一模一樣）。那是 virtualenv 在 Windows 上的正常結構，**不是**
> 開了兩份——要分辨請看父行程關係。

### 跨行程瀏覽器槽鎖

長久不變量：**同時只有一個瀏覽器 stack 在跑**。bot 與 `verify_browser.py` 都可能開
瀏覽器，重疊的後果是全機清掃會誤殺驗證中的瀏覽器、或兩者搶同一份登入設定檔。

`chrome_slot.lock` 是諮詢鎖（`O_CREAT|O_EXCL` 原子建立）。持有者行程已死或持有超過
600s 即視為 stale、可被搶。**bot 只在「清掃 ＋ spawn」的短臨界區握鎖**，webrunner
起來就釋放；驗證端則靠讀 `webrunner.pid` 決定讓位。

---

## 狀態鏡像與 Rich Presence

Bot 把目標使用者（`target_presence_username`）的活動鏡像到**自己的** presence。
兩個訊號來源同時跑：

1. **遠端** `on_presence_update`——目標使用者廣播的 activity；隱身時完全收不到。
2. **本機 OS probe（優先）**——每 8 秒掃一次，從系統媒體工作階段抓正在播的音樂、
   從 psutil 行程清單比對 `presence_games.json` 抓正在跑的遊戲。命中時直接 override
   遠端來源，所以目標使用者隱身也能反映實況。

同一份訊號餵給**兩套不同優先序**（媒體只探測一次）：

| 通道 | 優先序 | 音樂判定 |
|---|---|---|
| **bot 鏡像**（套到 bot 自己） | 遊戲 > 音樂 > 開發工具（墊底） | 串流來源白名單 |
| **本機 RPC**（套到你自己帳號） | **開發工具 > 遊戲 > 音樂** | 嚴格白名單 |

也就是 bot 鏡像沒遊戲、沒核准串流音樂時才墊開發工具；RPC 則只要偵測到就一律優先秀。

> ⚠️ **bot 鏡像只接受核准的串流音樂來源**。一般本機播放器與瀏覽器影片都不會變成
> Listening。系統媒體 API 只提供來源、標題、作者與不可靠的 PlaybackType，沒有媒體
> URI 或「本機檔案」旗標，所以核准的串流 app 若播本機檔案 probe 無法可靠區分——
> 來源白名單是 API 限制下的 fail-closed 作法。

**YouTube Music 在一般瀏覽器分頁播也算**（不必裝 PWA）：當媒體來源純粹是瀏覽器名時，
會掃所有可見視窗，若有視窗標題同時包含 `YouTube Music` **與**回傳的歌名才認列。
比「無條件接受瀏覽器來源」安全，比「必須裝 PWA」寬鬆。備援還有前景視窗 pattern
（`<曲名> - <歌手> - YouTube Music` 這類精確結尾）。

**遠端鏡像只接音樂類與真正的遊戲類**——會刻意 ignore 白名單以外的隨機 activity
（不會出現「Watching VSCode」），也會略過「我們自己用 RPC 灌上去的 activity」
（`application_id` 相符），避免鏡像鏡到自己。

### 本機 Rich Presence

上面是寫到 **bot 自己**——因為 API **不允許** bot 改另一個使用者的狀態。要讓「你本人
帳號」顯示，得走另一條官方通道：桌面 client 開的本機 IPC（Windows named pipe
`\\.\pipe\discord-ipc-N`），照 Rich Presence 協定呼叫 `SET_ACTIVITY`。這是遊戲顯示
「Playing X」用的同一套機制，**不是 self-bot、不碰 user token**。

實作在 `discord_rpc.py`（純 stdlib、零新相依），由 probe loop 每 8 秒餵同一份偵測
結果，blocking 的 IPC I/O 丟到 worker thread。

**能寫 / 不能寫（平台限制）**

- ✅ Rich Presence Activity：粗體 name、details / state 兩行、大小圖示、經過時間軸。
- ✅ 粗體 name **預設用執行中的應用程式名**（實測桌面 client 會吃 activity 的 `name`）。
- ❌ Custom Status 泡泡文字 / emoji、線上／閒置／勿擾／隱身的狀態燈——那些是帳號專屬
  設定，任何程式都改不了。
- 圖片**預設關閉**；要圖片得先上傳到該 app 的 Art Assets 再把 asset key 填進
  `kinds.*.large_image`。

**啟用步驟**（`presence_rpc.json`，hot-reload，**預設 `enabled: false`**）：

1. Developer Portal 建一個 Application（只是要拿 ID 當 `client_id`）。
2. 把 Application ID 填進 `client_id`。
3. `enabled` 改 `true`，並確保桌面 app 開著且已登入。
4. **在桌面 app 的「活動隱私」設定裡打開分享活動的開關**（設定 → 活動隱私 →
   分享偵測到的活動／把目前的活動顯示為狀態訊息）。**少了這一步，前面三步全部
   做對也不會有任何效果**——見下面的疑難排解。
5. 用 `/sys probe_status` 看連線狀態，**並確認最後一行「對外廣播」是 ✅**。
6.（選用）到該 App 的 Rich Presence → Art Assets 上傳圖片，命名對應 `kinds.*.large_image`。

`kinds` 下每個偵測種類各自設定 name / details / state / 圖示 / 是否顯示時間軸；
文字可用 `{name}` 佔位符。桌面 app 沒開時整段安靜 no-op，下次有變化自動重連。

**疑難排解：狀態沒變 → 先看「對外廣播」那一行**

`/sys probe_status` 最後一行「對外廣播」是唯一分得出「有沒有真的生效」的地方：

| 顯示 | 意思 |
|---|---|
| ✅ 別人真的看得到這張活動卡片 | 整條路都通了 |
| ⚠️ 收下了卻沒有廣播出去 | **第 4 步那個開關沒開**（其餘每一項仍會顯示正常） |
| ➖ 判斷不出來 | 目前沒有要顯示的活動，或 bot 在共同的伺服器裡找不到你 |

這一行是 bot 從**另一側**對出來的——它讀你在共同伺服器裡廣播出去的 activities，
看裡面有沒有 `application_id` 等於你的 `client_id` 那一張。之所以需要這樣繞，是因為
桌面 app 的活動隱私開關關掉時，它**照樣收下** `SET_ACTIVITY`、**照樣回成功**（連圖片
asset key 都會被解析成真實 asset id），只是不把 activity 廣播出去——從程式這一側完全
看不出差別。

另外注意 `apply` 的結果字：`ok` 是「對方收下了」，`rejected` 是「對方明確拒絕這個
activity」（欄位無效、asset key 不存在等，細節在 log），`not-connected` 是桌面 app
沒開。三者都不保證「別人看得到」——那取決於上面那個開關。

---

## 開發

### 跑測試

```powershell
py -3 -m pytest                    # 全部（pytest.ini 的 testpaths 指向 test/）
py -3 test\test_bot_helpers.py     # 單檔（每個測試檔都自帶 runner）
```

| 檔案 | 守住什麼 |
|---|---|
| `test_bot_helpers.py` | bot 側純 helper ＋ 佇列預覽鏡像不得與 webrunner 失同步 |
| `test_webrunner_shared.py` | 共用模組**不得** import driver（原始碼靜態防線）＋ 純函式語義 |
| `test_dynamic_consume.py` | 動態消耗 vs 舊快照模型的等價性、run 中途編輯的行為 |
| `test_run_progress.py` | 續跑檢查點的原子性與容錯（截斷 JSON → 不 raise） |
| `test_bot_prompts.py` | 外部化 prompt 檔與內建回退值不得 drift |
| `test_supervisor.py` | 退避政策 |

`_test_presence_e2e.py` 是手動 e2e（需要真實桌面工作階段），`_test_` 前綴讓 pytest
不收集它。

### 驗證瀏覽器啟動

```powershell
py -3 axiomatic\verify_browser.py            # smoke（暫時 profile、headless）
py -3 axiomatic\verify_browser.py --full     # 端到端（持鎖、隔離、需已登入 profile）
```

**絕不碰正式 profile**——每次都開 `tempfile.mkdtemp()` 的用完即丟目錄。
**清理只殺自己這次起的行程樹**，禁止全機清掃（會誤殺正在產圖的瀏覽器）。
開瀏覽器前先取瀏覽器槽鎖，取不到或發現有活著的 webrunner 就讓位。

### Definition of Done

每次改動 commit 前都必須滿足：

1. **冒煙 import**：repo root 執行
   `py -3 -c "import sys; sys.path.insert(0, 'axiomatic'); import discord_bot, webrunner_novelai; print('OK')"`
   要印 `OK` 且無 traceback。
2. 新斜線指令必須同步寫進三語 help、本 README、`COMMANDS.md` 與
   `docs/commands_*.md`（`test_docs_sync.py` 會逐一比對）。
3. 新 `@bot` 指令必須同步寫進 `MENTION_HELP_SECTIONS`（三語）與本 README。
4. 新增到 `requirements.txt` 的相依必須在當次變更中真的 `pip install` 過。
5. 啟動器的直譯器探索順序（`.venv` → `py -3` → `sys.executable`）必須完整保留。
6. 不得破壞 todo 檔的磁碟格式。

### 硬規則速查

| 規則 | 重點 |
|---|---|
| **模組邊界** | bot 與 webrunner 絕不互相 import。跨行程狀態全走磁碟 |
| **原子寫入** | 任何「一邊寫、另一邊輪詢」的檔案都必須 sibling temp → `os.replace`。懲罰不是崩潰而是**靜默的錯誤結果**（半寫入的佇列讀起來是空的、批次乾淨結束 rc=0） |
| **Windows PID 探測** | **絕不可**用 `os.kill(pid, 0)`——`signal.CTRL_C_EVENT == 0`，那會送出真正的 Ctrl+C 而非探測，且兩個方向都會誤判。用 `psutil.pid_exists`，退回 ctypes `OpenProcess` ＋ `GetExitCodeProcess`（必須明確給 `argtypes`/`restype`，64-bit HANDLE 會被截斷） |
| **兩支變體同步** | 共用的進 `_webrunner_shared.py`；變體專屬的兩邊都要改 |
| **回覆保密** | 送往 Discord 的字串不得含服務名、本機路徑、PID 或原始例外文字 |
| **繁體用詞** | 使用者／檔案／程式／**行程**／執行／資料／設定／預設／訊息／佇列／快取…… |
| **commit 訊息** | 不得透露 AI 作者身分；逐檔 `git add`，不要 `git add -A` |

完整的硬規則正本在 [`CLAUDE.md`](CLAUDE.md)；架構總覽在
[`architecture.md`](architecture.md)。

---

## 檔案結構

```
Axiomatic/
├── axiomatic/
│   ├── discord_bot.py            # Discord bot 事件迴圈
│   ├── webrunner_novelai.py      # 批次產圖器（Selenium 變體，正式預設）
│   ├── webrunner_je_only.py      # 批次產圖器（wrapper 變體，備援）
│   ├── _webrunner_shared.py      # 兩變體共用核心（driver-agnostic）
│   ├── _queue_consume.py         # 動態佇列消耗決策（純函式）
│   ├── _run_progress.py          # 角色中斷續跑檢查點
│   ├── _batch_config.py          # batch_config.json 載入器（熱重載）
│   ├── _bot_config.py            # bot_config.json 載入器
│   ├── _bot_prompts.py           # bot_prompts/ 文字載入器
│   ├── _supervisor.py            # 純退避數學
│   ├── _chrome_slot.py           # 跨行程瀏覽器槽鎖
│   ├── _power_request.py         # 參考計數的電源要求
│   ├── _connectivity.py          # 主機連得上網路嗎
│   ├── _process_control.py       # 行程探查／終止原語（bot 側）
│   ├── _gui_control.py           # 桌面自動化門面（bot 側）
│   ├── _external_apis.py         # 外部 API 抓取／解析（bot 側）
│   ├── _help_strings.py          # 說明文字純資料（三語）
│   ├── dorossi_backend.py        # 後端 AI 叫用 ＋ 工作階段
│   ├── presence_probe.py         # 本機遊戲／音樂／開發工具偵測
│   ├── discord_rpc.py            # 自製本機 Rich Presence client
│   ├── verify_browser.py         # 隔離瀏覽器驗證（opt-in）
│   ├── dashboard_server.py       # 本機唯讀狀態儀表板
│   └── __init__.py
├── test/                         # 測試（repo 根目錄，不在套件裡）
│   ├── conftest.py               # 全套共用的守門夾具
│   ├── test_*.py                 # 單元測試（pytest 收集）
│   └── _test_*_e2e.py            # 手動 e2e（不被 pytest 收集）
├── pytest.ini                    # testpaths = test、pythonpath、逾時與警告閘
├── start_discord_bot.py          # bot 監督啟動器
├── start_webrunner.py            # webrunner 監督啟動器
├── run_batch.py                  # 一鍵批次入口
├── install_autostart.py          # 在工作排程器註冊兩支監督者
├── wake_autostart.py             # 直接叫醒工作排程器裡的監督者
│
│   # ---- 版本庫帶的範本（複製成同名正式檔再填）----
├── auth.example.md               # → auth.md
├── discord_bot_token.example.md  # → discord_bot_token.md
├── bot_config.example.json       # → bot_config.json
├── presence_*.example.json       # → presence_games/music/rpc.json
├── prompt.example.md             # → prompt.md（character1/2、undesired、todo_* 同理）
├── batch_config.json             # 產圖批次參數（唯一追蹤的設定檔；每角色熱重載）
├── bot_prompts/                  # 外部化 prompt 文字
│
│   # ---- 憑證與使用者內容（gitignored）----
├── auth.md                       # 出圖服務帳密
├── discord_bot_token.md          # bot token
├── bot_config.json               # bot 設定（開機載入；/sys restart 生效）
├── presence_games.json           # 遊戲白名單（hot-reload）
├── presence_music.json           # 音樂偵測規則（hot-reload）
├── presence_rpc.json             # 本機 RPC 設定（hot-reload；預設 enabled:false）
├── todo_prompt.md                # 主提示詞佇列（含 end 哨符）
├── todo_character1.md            # 角色 1 佇列
├── todo_character2.md            # 角色 2 佇列（位置性——空行有意義）
├── todo_undesired.md             # 負面提示詞佇列
├── prompt.md                     # 主提示詞 fallback
├── character1.md                 # 角色 1 fallback
├── character2.md                 # 角色 2 fallback
├── undesired.md                  # 負面提示詞 fallback
├── default_prompt.md             # 範本（/todo prompt default）
├── templates/                    # 具名範本目錄（選用；/todo prompt template）
├── ocr_tessdata/                 # 文字辨識語言資料（引擎安裝目錄無寫入權，所以放專案內）
├── dorossi_workspace/            # Dorossi 後端的工作目錄
│
│   # ---- 文件 ----
├── README.md                     # 本檔
├── CLAUDE.md                     # 常駐硬規則（工程規範正本）
├── COMMANDS.md                   # 完整指令參考
├── architecture.md               # 架構總覽
├── commands/                     # 逐指令參考（一個指令群一檔；由指令樹產生）
├── docs/                         # Sphinx 文件原始碼
│
│   # ---- 執行期產物（gitignored）----
├── output/                       # 產出圖檔
├── .chrome_profile/              # 持久化瀏覽器 session
├── .backup/                      # /sys undo 的寫入備份
├── macros/                       # /macro 錄製的巨集
├── window_layouts/               # /win layout save 的視窗版面
├── webrunner.{log,pid,pause}     # log / PID / 暫停標記
├── webrunner_progress.json       # 續跑檢查點
├── chrome_slot.lock              # 瀏覽器槽鎖
├── .discord_bot.lock             # bot 本體的單一實例鎖（內容為空）
├── .discord_bot_supervisor.lock  # 啟動器的單一實例鎖（**刻意用不同檔**）
├── single_image_request.json     # 單張產圖請求（單槽）
├── dom_request.json              # DOM 探測請求
├── schedules.json                # /schedule 定時排程表
├── network_resume.json           # 斷網停下來的批次，網路回來就接續
├── events.ndjson                 # webrunner → bot 事件串流
├── dorossi_{session.json,usage.ndjson,events.ndjson}
├── dorossi_queue{,_failed}.ndjson
├── generate_history.ndjson       # 單張產圖歷史
├── audit.ndjson                  # 指令稽核（append-only）
├── favorites.json                # 收藏
├── recent_image_msgs.json        # reaction 策展的 msg → path 快取
└── debug_*.png                   # 除錯截圖
```
