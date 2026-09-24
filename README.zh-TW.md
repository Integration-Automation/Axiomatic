# Axiomatic

[English](README.md) · **繁體中文** · [简体中文](README.zh-CN.md) · [日本語](README.ja.md)

**一台用聊天操作的自動化主機。** 你人在哪個聊天平台，就從那裡對這台機器下指令，
它在機器上替你做事：桌面與視窗自動化、行程與檔案操作、定時工作、可抽換後端的
對話問答，以及一條長時間執行的瀏覽器批次——依你用聊天編輯的佇列連續產圖。

**沒有任何單一平台是這個專案的身分。** 聊天表面是一層介接：**一個平台一個**受監督
的行程，每個平台都能各自關掉，各有自己的鎖、記錄檔與狀態。新增一個平台是一個
transport 模組加一段設定；移除一個平台是一個 `false`。工作負載住在介接層後面，
不知道一道指令是從哪個平台進來的。

**適合誰**：有一台機器要在人不在的時候繼續做事——那種想用手機啟動、觀察、調整、
停止的長時間無人值守工作，而且操作主機的表面要鎖在每個平台各自的擁有者身分上。

> **撰寫慣例**：散文以泛稱指涉外部相依（出圖服務、目標網站、網頁介面、對話後端）；
> 檔名、指令名、設定鍵與程式符號則照實寫。

<!-- section: contents -->
## 目錄

- [這是什麼](#這是什麼)
- [需要什麼](#需要什麼)
- [安裝設定](#安裝設定)
- [跑一個或多個平台](#跑一個或多個平台)
- [設定檔](#設定檔)
- [佇列與提示詞檔](#佇列與提示詞檔)
- [指令](#指令)
- [批次行為](#批次行為)
- [監督與重啟](#監督與重啟)
- [更深入的文件在哪](#更深入的文件在哪)
- [開發](#開發)

---

<!-- section: what-this-is -->
## 這是什麼

```
    平台 A            平台 B            平台 C …
      │                 │                 │
      ▼                 ▼                 ▼
 ┌───────────┐     ┌───────────┐     ┌───────────┐
 │  bot 行程 │     │  bot 行程 │     │  bot 行程 │   一個平台一個行程
 │  自己的鎖 │     │  自己的鎖 │     │  自己的鎖 │   自己的記錄檔與狀態
 │  自己的態 │     │  自己的態 │     │  自己的態 │
 └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
       └─────────────────┼─────────────────┘
                         │  磁碟上的檔案（唯一的耦合）
        ┌────────────────┼──────────────────┬───────────────────┐
        ▼                ▼                  ▼                   ▼
   桌面與視窗       主機指令、作業        對話後端            出圖批次
     自動化           與排程            （可抽換）        （由鎖決定誰監督）
```

- **平台行程**是意圖層：接指令、判斷是誰在問、編輯磁碟上的檔案、啟動並監督工作、
  回報結果。每個行程只服務一個聊天平台，彼此不共用任何可變狀態。
- **工作負載**是執行層。桌面自動化在平台行程裡跑；出圖批次是它自己的受監督子行程，
  由它操作瀏覽器——登入、填提示詞、按產生、下載。
- **中間只有檔案。** 所有共用狀態都在磁碟上，所以任何一邊重啟都不會弄壞另一邊，
  批次也永遠不會 import bot、反之亦然。

唯一被允許的第三條路是**被動共用模組**（`_batch_config`、`_queue_consume`、
`_webrunner_shared` …）：兩邊都可以 import 它們，因為那是獨立、無狀態、與 driver
無關的純程式碼。

### 元件

**對話平台層**——讓 bot 與平台無關的那一層

| 檔案 | 用途 |
|---|---|
| `axiomatic/_chat_platform.py` | 介接接縫：身分映射、能力旗標、送出引數正規化、transport 註冊表 |
| `axiomatic/_telegram_transport.py` | 其中一個平台。之後每多一個平台就多一個 `_*_transport.py` |
| `axiomatic/_platform_runtime.py` | 這個行程服務哪一個平台，以及它自己的狀態、鎖與記錄檔放在哪 |

**指令主體**（模組名是歷史遺留，它並不綁任何平台）

| 檔案 | 用途 |
|---|---|
| `axiomatic/discord_bot.py` | 13 個頂層 slash 指令 ＋ 25 個指令群（合計 269 個斜線子指令）、身分閘、桌面與主機控制、批次監督、對話後端編排、單張產圖佇列 |
| `axiomatic/_gui_control.py` | 桌面自動化門面（滑鼠、鍵盤、視窗、剪貼簿、文字辨識、圖片定位） |
| `axiomatic/dorossi_backend.py` | 對話後端與它的工作階段——多種後端藏在同一個介面後面 |

**出圖這個工作負載**

| 檔案 | 用途 |
|---|---|
| `axiomatic/webrunner_novelai.py` | 批次產圖器，Selenium 變體（正式預設） |
| `axiomatic/webrunner_je_only.py` | 批次產圖器，wrapper 變體（備援；`/run` 先試這支） |
| `axiomatic/_webrunner_shared.py` | 兩支變體的共用核心：DOM 操作、純函式、批次主迴圈，與 driver 無關 |

**啟動器**（repo 根目錄）

| 檔案 | 用途 |
|---|---|
| `start_platforms.py` | 把每個開著的平台各起一個受監督行程 |
| `start_discord_bot.py` | **單一平台**的監督迴圈（`--platform <名稱>`） |
| `start_webrunner.py` | 出圖批次的監督迴圈 |
| `run_batch.py` | 本機一鍵批次：前置檢查、印 run-plan、交棒 |
| `install_autostart.py` | 註冊／移除 Windows 工作排程器的登入工作 |

---

<!-- section: requirements -->
## 需要什麼

Windows、Python 3.11 以上、一個瀏覽器，以及 `requirements.txt` 裡的套件。
`requirements.txt` **不釘版本**——fresh clone 拿到的是各套件當下的版本——但對
「本專案真的碰得到、而且有已知安全通報」的套件會設下限。

| 套件 | 用途 |
|---|---|
| `selenium` | Selenium 變體的瀏覽器驅動 |
| `je_web_runner` | wrapper 變體的瀏覽器驅動 |
| `urllib3` | 兩支變體直接接 `ReadTimeoutError`（selenium 不轉出這個例外） |
| `discord.py` | 有原生斜線選單那個平台的 transport 與事件迴圈 |
| `aiohttp` | 對外 API 呼叫，以及其他平台用的長輪詢 transport |
| `psutil` | 行程存活探測與視窗比對（**必要**） |
| `je-auto-control` | 桌面自動化的**唯一實作**：滑鼠、鍵盤、視窗、剪貼簿、文字辨識、圖片定位 |
| `pytesseract` | `/locate text find\|click\|wait` 的文字辨識 wrapper。**只裝這個 pip 套件不夠**——辨識引擎要另外裝並放進 PATH；沒裝時那三個指令回泛用提示，不會炸 |
| `comtypes` | `/locate ui …` 的元素定位。裝不起來時該組指令回泛用提示，其餘定位方式照常 |
| `Pillow` | `/grid` 拼 2×2 圖 |
| `pyfiglet` | `/fun ascii` |
| `simpleeval` | `/fun calc`——安全運算，不是 `eval` |
| `anthropic` | 對話後端的 `api` 那一種（選用；缺了 bot 仍可啟動，該路徑降級回報） |
| `matplotlib` | 舊版 token 圖表 helper 的相容性測試 |

瀏覽器自動化函式庫由兩支批次產圖器透過 `sys.path` 從**同層目錄**或
`WEBRUNNER_PATH` 匯入：

```
<parent>/
├── Axiomatic/    ← 本 repo
└── WebRunner/    ← 同層 clone
```

---

<!-- section: setup -->
## 安裝設定

### 一、安裝相依

```powershell
py -3 -m pip install -r requirements.txt
```

### 二、填憑證

**憑證與設定都不在版本庫裡。** repo 帶的是範本，第一步是複製它：

```powershell
copy auth.example.md                   auth.md
copy discord_bot_token.example.md      discord_bot_token.md
copy telegram_bot_token.example.md     telegram_bot_token.md
copy bot_config.example.json           bot_config.json
```

| 檔案 | 內容 |
|---|---|
| `auth.md` | 出圖服務登入帳密，兩行：`username: ...` / `password: ...`（以第一個冒號分隔） |
| `discord_bot_token.md` | 有原生斜線選單那個平台的 bot token（整個檔案就是 token，或一行 `Token: xxx`） |
| `telegram_bot_token.md` | 第二個平台的 bot token。**要用那個平台才複製**——檔案不存在或是空的，就等於那個平台關著 |

不要把它們加回版本庫；提交時**逐檔 `git add`**，不要 `git add -A`。

### 三、設定 bot

編輯 `bot_config.json`，至少填兩個值：

- `channel_id`——限頻道指令只在這個頻道生效；
- `owner_user_id`——你自己的使用者 ID。操作主機的指令群與 `/dorossi` 只認它，
  留 `0` 的話那些指令對所有人一律拒絕。

兩個都沒填時 bot 會在啟動時印一段說明並乾淨結束，不是 traceback。

在有原生斜線選單的那個平台上，要在它的開發者後台把 **Message Content**、
**Presence** 與 **Server Members** 三個特權 intent 打開，否則啟動會丟
`PrivilegedIntentsRequired`。

### 四、放佇列內容

最少只要有一個佇列有東西就能跑——其餘三個會自動用 `prompt.md`、`character1.md`、
`character2.md`、`undesired.md` 當 fallback。這幾個 fallback 一樣不在版本庫裡，
複製範本過來再填：

```powershell
copy prompt.example.md      prompt.md
copy character1.example.md  character1.md
copy undesired.example.md   undesired.md
```

佇列本身通常不用手建——`/todo char1 add <角色描述>` 就會寫出來。格式是**一行一筆**、
不切逗號；`todo_character2.md` 是位置對應的，空行代表「這一對不要第二個角色」，
要保留。

---

<!-- section: running-one-or-several-platforms -->
## 跑一個或多個平台

**一個平台一個行程。** 每個行程各有自己的單一實例鎖、自己的記錄檔與自己的狀態檔，
全部住在 `state/<平台>/`。兩個平台完全不共用可變狀態，所以其中一個崩潰、重啟或被
關掉都不會碰到其他平台。

把開著的平台全部拉起來：

```powershell
py -3 start_platforms.py
```

看哪些會起來、哪些不會、為什麼：

```powershell
py -3 start_platforms.py --list
```

只跑其中一個：

```powershell
py -3 start_discord_bot.py --platform telegram
```

完全不用 bot 的本機批次：

```powershell
py -3 run_batch.py
```

> ⚠️ **不要同時用 bot 的 `/run` 與本機啟動器**——兩邊都會生一個批次產圖器，搶同一
> 份瀏覽器設定檔鎖。

### 要同時跑兩個平台，你要做的事

1. 填好那個平台的 token 檔（`<平台>_bot_token.md`）。
2. 在 `bot_config.json` 把 `platforms.<平台>.enabled` 設成 `true`，並列出那個平台的
   `owner_user_ids` 與 `allowed_chat_ids`（是**那個平台上的** id，字串）。
3. 執行 `start_platforms.py`。

其餘都是自動的：狀態目錄、鎖、記錄檔與自動啟動工作都以平台名命名。沒填憑證的平台
是**缺席，不是壞掉**——它不會被啟動，也不會每次開機都抱怨一次；要問原因就跑
`--list`。

預設平台也能用同一個方式關掉（`platforms.discord.enabled: false`）。完全沒有那一段
時它視為開著，因為一個 fresh clone 的 bot 什麼都不做，看起來就跟「設定沒生效」
一模一樣。

### 出圖批次仍然只有一份

批次是**全機共用**的資源（一個瀏覽器、一組佇列檔、一個輸出目錄），所以它不跟著平台
分身。誰拿到批次監督鎖，誰就監督它；其他行程收到批次控制指令時會回一句「另一個行程
已經在監督」，而不是再開一套——兩套監督會互相終止、互相重生，而且兩邊的紀錄看起來
都正常。

編輯佇列不受影響：佇列本來就在磁碟上，從哪個平台編都一樣有效。只有「誰監督批次」
這一件事認鎖。

### 登入時自動啟動（Windows）

```powershell
py -3 install_autostart.py --install    # 冪等
py -3 install_autostart.py --status
py -3 install_autostart.py --remove
```

它會替每個開著的平台各註冊一筆（`\Axiomatic\Bot-<平台>`），加上批次那一筆
（`\Axiomatic\Batch`）。觸發條件是**登入**而不是開機：批次要一個真的有桌面的
工作階段才開得起瀏覽器。

---

<!-- section: configuration-files -->
## 設定檔

| 檔案 | 裝什麼 | 進版本庫？ |
|---|---|---|
| `batch_config.json` | 批次產圖參數：每對幾張、等待時間、工作／休息時段、瀏覽器定期重啟 | 是 |
| `bot_config.json` | 頻道與擁有者 ID、角色、對話後端參數、可啟動程式白名單、`platforms.*` | 否（範本：`bot_config.example.json`） |
| `presence_games.json`、`presence_music.json`、`presence_rpc.json` | 本機狀態對應 | 否（範本：`*.example.json`） |
| `bot_prompts/` | 10 個純文字檔（人設、自走迴圈各段守則、單張產圖的預設畫風後綴） | 是 |

`bot_config.json` **開機讀一次**——改完要 `/sys restart` 才生效。認不得的鍵會被忽略
並印一行警告，因為鍵名打錯的症狀跟「設定沒生效」一模一樣。

`batch_config.json` 可以用 `/config set` 線上改，批次會在兩張圖之間重讀它。

---

<!-- section: queues-and-prompt-files -->
## 佇列與提示詞檔

| 佇列 | 空的時候的 fallback |
|---|---|
| `todo_prompt.md` | `prompt.md` |
| `todo_character1.md` | `character1.md` |
| `todo_character2.md` | `character2.md`（空行代表「這一對沒有第二個角色」） |
| `todo_undesired.md` | `undesired.md` |

配對會把幾條佇列一起走，比較短的那條用它的 fallback 補齊。`todo_prompt.md` 裡的
`end` 是**終止標記**：批次跑完它前面那一對就乾淨收工，這是把一條長佇列先停下來
而不刪任何東西的作法。

讀取端只切行邊界——一筆內容本來就可能含逗號、冒號與括號——而且會把不斷行空白正規化
成一般空白。寫入端會拒絕含有行邊界的內容，因為那會被讀回成好幾筆。

---

<!-- section: commands -->
## 指令

在有原生斜線選單的平台上，指令就是**斜線指令**：打 `/` 會自動補全，參數在送出前就
有型別與值域檢查。沒有斜線選單的平台則走文字表面（見下），一份實作、一組權限閘。
共 **13 個頂層 slash 指令**與 **25 個指令群**（37 個頂層項目，平台上限 100），底下
合計 **269 個斜線子指令**。

指令群不管裝幾個子指令都只佔一個頂層額度，所以把低頻指令收進群裡是唯一能長期擴充的
作法。逐指令說明看 [`COMMANDS.md`](COMMANDS.md)、[`commands/`](commands/README.md)，
或直接打 `/help`。

- 🔒 **限頻道**：只在 `channel_id` 那個頻道回應（**擁有者可跨頻道**）。
- 🌐 **跨頻道**：bot 看得到的任何頻道都能用。
- 🔑 **限擁有者**：操作 bot 那台機器的指令，**不看 `user_roles`**。

> 🔑 **主機控制一律只有擁有者能用。** `/input`、`/screen`、`/win`、`/clip`、
> `/locate`、`/macro`、`/watch`、`/proc`、`/host` **整群**受閘（新增子指令自動受
> 保護），另加 `/sys restart` 這類會動到主機的零散指令。閘門在派發**之前**、而且排
> 在角色閘**之前**——`user_roles` 三份清單都空時（預設）角色閘等於停用，把桌面控制
> 掛在它下面等於沒有保護。

> **所有回覆一律泛用**：不揭露服務名、主機路徑、檔名、PID 或原始錯誤文字。詳細內容
> 只寫 stderr 與記錄檔。

### 🔒 限頻道

| 指令 | 說明 |
|---|---|
| `/eta` | 預估完成時間（有終止標記只算到標記為止） |
| `/latest` | 上傳最近 N 張產出圖 |
| `/queue` | 各佇列剩餘筆數與實際會跑的配對數 |
| `/run` | 啟動批次（可排程：in 90m / at 02:00 / cancel） |
| `/status` | 批次狀態與正在跑的變體 |
| `/stop` | 停止批次 |

| 家族 | 子指令 |
|---|---|
| **產圖佇列** | `/todo dedupe\|duplicate\|find\|move\|shuffle\|swap`<br>`/todo char1 add\|addx3\|clear\|list\|pop\|remove`<br>`/todo char2 add\|clear\|default\|list\|pop\|remove`<br>`/todo negp add\|clear\|list\|pop\|remove`<br>`/todo prompt add\|clear\|default\|end\|insert\|list\|pop\|remove\|template\|unend` |
| **佇列空時的預設值** | `/preset info`<br>`/preset main append\|clear\|set`<br>`/preset neg append\|clear\|set` |
| **批次控制** | `/gen current\|image\|image_queue\|pause\|plan\|preview\|progress\|resume` |
| **產出檢視** | `/out debug_show\|history\|latest_for\|rate\|sample\|stats` |
| **收藏** | `/fav clear\|list\|remove\|show` |
| **執行紀錄** | `/log clear\|errors\|grep\|size\|tail` |
| **維運與診斷** | `/sys audit\|backfill_paths\|cleanup_debug\|dashboard\|disk\|doctor\|git_pull\|health\|introspect_dom\|metrics\|probe_status\|restart\|undo\|update_check` |
| **行程控制** | `/proc kill\|launch\|list\|usage` |
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
| `/grid` | 圖庫最新 4 張拼 2×2 上傳 |
| `/help` | 指令說明（tw / cn / en） |
| `/iqdb` | 跨圖庫反向圖搜（回前幾名來源與相似度 %） |
| `/nsfw` | 圖庫搜圖的 NSFW 捷徑 |
| `/safebooru` | 全站 SFW 圖庫隨機一張 |

| 家族 | 子指令 |
|---|---|
| **對話後端**（🔑 限擁有者） | `/dorossi abort\|ai\|ask\|compact\|effort\|errors\|fullmode\|health\|logs\|model\|retry\|running\|status\|tokens\|workspace_clean`<br>`/dorossi allowdir add\|list\|remove`<br>`/dorossi queue clear\|detail\|failed_clear\|move\|remove\|retry_failed\|show\|undo`<br>`/dorossi session archive\|continue\|delete\|export\|list\|new\|rename\|reset\|switch` |
| **圖庫 tag 工具** | `/tag autocomplete\|count\|suggest\|wiki` |
| **趣味／隨機** | `/fun 8ball\|ascii\|calc\|choose\|coinflip\|rand\|reverse\|roll\|rps\|timer` |
| **編碼與小工具** | `/tool base64\|color\|hash\|qr\|say\|unbase64\|urldecode\|urlencode` |
| **資訊與元資料** | `/info avatar\|channel\|ping\|server\|uptime\|version` |
| **公開資料查詢** | `/web anime\|cat\|crypto\|dict\|dog\|fact\|github\|joke\|quote\|wiki\|xkcd` |

### `@bot <文字>`——自由提問

不接子指令的 mention 是自由提問入口。**刻意保留 mention 形式**而不是改成斜線指令：
一則訊息帶得動多行內容、附件與回覆脈絡，選項輸入框帶不動；而且互動 token 的壽命
遠短於一個長回合。

### 表情符號反應

對 bot 上傳的圖按 ⭐ 加入收藏、🗑️ 刪檔。寫入走備份機制，`/sys undo` 可回復。

### 沒有斜線選單的平台

在沒有原生斜線選單的平台上，同一組指令改從文字表面進入。那個表面只為那些平台寫在
[`docs/platforms.md`](docs/platforms.md)；有斜線選單的地方，斜線指令仍然是唯一對外
宣傳的介面。

---

<!-- section: batch-behaviour -->
## 批次行為

1. **每次啟動一次完整 setup**（每次瀏覽器重啟也是）：登入、套用設定快照、確認頁面
   在預期狀態。
2. **動態消耗佇列**：每個角色都重讀一次佇列，所以跑到一半做的編輯會在下一對生效，
   而不是下一輪。
3. **逐對迴圈**：產到設定的張數為止，邊產邊下載。
4. **完成門檻**：存到夠多張才從佇列 pop——所以中途崩潰會重跑那一對，而不是安靜跳過。
5. **續跑檢查點**：進度以原子寫入落地，任何時刻被砍掉都接得回正確位置。
6. **瀏覽器定期重啟**：長時間執行會讓瀏覽器記憶體膨脹，所以定期回收它。
7. **事件串流**：批次附加結構化事件，bot 監看並回報。

回傳碼：`0` 乾淨收工、`1` 無事可做、`2` session setup 失敗、`3` 零產出、
`4` 被擋住不重生、`5` 設定還沒填（同樣不重生）。常數在
`axiomatic/_supervisor.py`。

---

<!-- section: supervision-and-restart -->
## 監督與重啟

每支啟動器都在一個受監督的迴圈裡跑它的子行程，帶指數退避與快速失敗放棄，並把子行程
的主控台輸出 tee 進記錄檔。在啟動器視窗按 Ctrl+C 會乾淨地結束那個迴圈。

**單一實例保護**是作業系統持有的檔案鎖，所以行程一消失就釋放，沒有任何殘留旗標要清：

| 鎖 | 擋什麼 |
|---|---|
| `state/<平台>/.<平台>.discord_bot_supervisor.lock` | 那個平台的第二個監督者 |
| `state/<平台>/.<平台>.discord_bot.lock` | 那個平台的第二個 bot 行程 |
| `.webrunner_supervisor.lock` | 第二個批次監督者 |
| `.batch_supervisor.lock` | 決定哪一個 bot 行程監督批次 |
| `chrome_slot.lock` | 跨行程的瀏覽器槽，讓批次與驗證腳本不會同時開兩套瀏覽器 |

---

<!-- section: where-the-deeper-docs-are -->
## 更深入的文件在哪

| 文件 | 給誰看 |
|---|---|
| [`docs/`](docs/index.md) | 完整手冊（Sphinx／Read the Docs 格式） |
| [`docs/setup.md`](docs/setup.md) | 第一次安裝的逐步說明 |
| [`docs/config.md`](docs/config.md) | 每一個設定鍵 |
| [`docs/platforms.md`](docs/platforms.md) | 在沒有斜線選單的平台上執行 |
| [`docs/workflow.md`](docs/workflow.md) | 配對規則、fallback、終止標記 |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | 跑到一半卡住、瀏覽器崩潰、登入失敗 |
| [`COMMANDS.md`](COMMANDS.md) | 指令總表 |
| [`commands/`](commands/README.md) | 逐群參考，含參數、值域與權限（由指令樹產生） |
| [`architecture.md`](architecture.md) | 分層、進入點、主要流程、擴充點 |
| [`CLAUDE.md`](CLAUDE.md) | 要動這個 repo 的人必須遵守的常駐硬規則 |

本機建置手冊：

```powershell
py -3 -m pip install -r docs/requirements.txt
py -3 -m sphinx -b html docs docs/_build/html
```

---

<!-- section: development -->
## 開發

```powershell
py -3 -m pytest              # 全部
py -3 -m pytest test/test_platform_processes.py   # 單一檔案
```

測試住在 repo 根目錄的 `test/`，不在套件裡。整套測試有很大一部分是把本專案的硬規則
變成靜態守門：模組邊界、擁有者專屬閘、原子寫入、明寫文字編碼、繁體用詞，以及這一組
README 也在其中的文件一致性。

提交前至少要做到：

1. `py -3 -c "import sys; sys.path.insert(0, 'axiomatic'); import discord_bot, webrunner_novelai, webrunner_je_only; print('OK')"` 印出 `OK`。
2. 新的斜線指令同步寫進每一份使用者文件語料，而 `commands/*.md` 是用
   `py -3 axiomatic/gen_command_docs.py` 重新產生的，不是手改的。
3. 改動觸及 `architecture.md` 描述的東西時，同時更新它。
4. commit 主旨描述改了什麼；檔案逐個 stage。

完整清單是 [`CLAUDE.md`](CLAUDE.md) 的 Definition of Done。
