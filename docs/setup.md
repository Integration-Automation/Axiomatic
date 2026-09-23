# 安裝與啟動

這一頁是**全新 clone 到 bot 上線**的完整路徑。照順序做完就會有一個能用的機器人。

## 0. 先決條件

| 需要 | 為什麼 | 沒有的話 |
|---|---|---|
| **Windows 10/11** | 桌面自動化、Chrome 視窗、工作排程器都是 Windows API | 其他平台上大部分功能不會動 |
| **Python 3.10+** | 建議用 `py -3` 啟動器 | — |
| **Google Chrome** | webrunner 用 Selenium 驅動真的瀏覽器 | 批次產圖用不了 |
| **一個 Discord application ＋ bot token** | bot 本體 | bot 起不來 |
| **出圖服務的帳號** | webrunner 要登入才能產圖 | 批次產圖用不了 |

下面三項是**選用**的，缺了只會少一組功能，不影響其他部分：

| 選用 | 給哪個功能 | 怎麼裝 |
|---|---|---|
| 對話後端 CLI（`claude` 之類） | `/dorossi` 對話問答的 `claude_code` 後端 | 裝好它自己的 CLI 並登入；或改用 `api` 後端（`ANTHROPIC_API_KEY` 環境變數） |
| Tesseract OCR | `/screen ocr`、`/locate text` 之類的畫面文字辨識 | 裝 Tesseract，或設 `TESSERACT_CMD` 指到它的 exe |
| Discord 桌面版 | 本機 Rich Presence（把你自己的狀態換成正在跑的遊戲／音樂） | 開著並登入，再填 `presence_rpc.json` |

## 1. 取得原始碼並安裝相依套件

```powershell
git clone <this repo> Axiomatic
cd Axiomatic
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```{note}
兩個啟動器（`start_webrunner.py`、`start_discord_bot.py`）的直譯器探索順序是：
本機 `.venv` → `py -3` → `sys.executable`。先建好 `.venv` 能讓全新 clone
「直接就能跑」，之後也不必每次都先 activate。
```

## 2. 建立 Discord application 與 bot

1. 到 <https://discord.com/developers/applications> → **New Application**。
2. 左邊 **Bot** → **Reset Token** → 複製那一串。**它只會顯示一次。**
3. 同一頁往下，打開兩個特權 intent：
   - **MESSAGE CONTENT INTENT** — `!` 與 `@bot` 相容層要讀訊息內容；
   - **PRESENCE INTENT** ＋ **SERVER MEMBERS INTENT** — 狀態鏡像要用。
     沒開的話 bot 啟動時會直接丟 `PrivilegedIntentsRequired`。
4. 左邊 **OAuth2 → URL Generator**：scope 勾 `bot` 與 `applications.commands`
   （沒有第二個就不會有斜線指令），權限至少勾 **Send Messages**、
   **Attach Files**、**Read Message History**。用產生的網址把 bot 邀進你的
   伺服器。
5. 在 Discord 的 **設定 → 進階 → 開發者模式** 打開，之後才能右鍵複製 ID。

## 3. 複製範本、填好設定

repo **刻意不帶**任何憑證或設定實檔——它們全部在 `.gitignore` 裡。每一個都有一份
`*.example.*` 範本，第一步就是複製它：

```powershell
copy auth.example.md                auth.md
copy discord_bot_token.example.md   discord_bot_token.md
copy bot_config.example.json        bot_config.json
# 以下選用
copy presence_games.example.json    presence_games.json
copy presence_music.example.json    presence_music.json
copy presence_rpc.example.json      presence_rpc.json
```

### `discord_bot_token.md` — Discord bot token

整個檔案就是 token，或者一行 `Token: <值>`。**必填**，沒有它 bot 不會啟動
（會印一行說明叫你複製範本，不是 traceback）。

### `auth.md` — 出圖服務帳密

兩行，以**第一個冒號**分隔（所以密碼裡可以有冒號）：

```
username: your-account@example.com
password: your-password-here
```

**批次產圖需要**。缺了的話 webrunner 會印一行說明並乾淨結束（rc=5），監督者認得
這個回傳碼並直接收工，不會無限重生。

### `bot_config.json` — bot 執行期設定

範本裡每一個鍵上面都有一段 `_..._comment` 說明。**最少要填兩個**：

| 鍵 | 怎麼拿 |
|---|---|
| `channel_id` | 右鍵你要用的頻道 → 複製頻道 ID。限頻道指令只在這裡生效。 |
| `owner_user_id` | 右鍵自己的名字 → 複製使用者 ID。操作主機的指令群與 `/dorossi` 只認這個 ID。 |

兩個都留 0 的話 bot 啟動時會擋下來並告訴你缺什麼。`owner_user_id` 沒填時，
桌面控制那一整群指令對**所有人**一律拒絕——那是刻意的 fail-closed 預設。

其餘的鍵都有堪用的預設值，可以之後再調；完整清單見 {doc}`config`。

### presence 三個檔（選用）

不複製就是整組停用，不影響其他功能。細節見 {doc}`config`。

### 佇列與提示詞檔（產圖才需要）

四條佇列（`todo_prompt.md`、`todo_character1.md`、`todo_character2.md`、
`todo_undesired.md`）通常由 bot 的 `/todo add` 寫，不必手動建立。每一條佇列空掉
時，webrunner 會退回同名的 fallback 檔（`prompt.md`、`character1.md`、
`character2.md`、`undesired.md`）。想直接跑批次而不透過 bot，就把這幾個 fallback
從範本複製過來再填：

```powershell
copy prompt.example.md      prompt.md
copy character1.example.md  character1.md
copy undesired.example.md   undesired.md
```

格式是**一行一筆**，不切逗號——一筆裡面可以有逗號。`todo_character2.md` 是
**位置對應**的：空白行代表「這一對不要第二個角色」，所以它的空行要保留。

## 4. 啟動 bot

```powershell
py -3 start_discord_bot.py
```

`start_discord_bot.py` 是**監督式啟動器**：bot 崩潰就重啟，第一次等 5 秒，若
60 秒內又崩潰則退避加倍（上限 300 秒），活滿 60 秒就重置回 5 秒。不要把它改成
固定間隔重啟——壞掉的 token 在緊密迴圈裡會狂打 Discord 連線端點而被封鎖。

兩種情況啟動器會**直接收工而不重試**，因為重試不會有結果：

- **已經有一個 bot 在跑**（單一實例鎖擋下來，rc=3）；
- **設定還沒填好**（rc=5）——訊息會說缺哪一個檔、該複製哪個範本。

bot 上線後在設定的頻道打 `/help` 看完整指令清單（可選 `tw` / `cn` / `en`）。
斜線指令要幾分鐘才會在 Discord 的應用程式裡出現，這是平台端的同步延遲。

## 5. 啟動批次產圖

兩種方式**擇一**（不要同時用，會搶 Chrome profile 鎖）：

1. **從 Discord**：在設定的頻道打 `/run`。
2. **獨立啟動器**：

   ```powershell
   py -3 start_webrunner.py          # 預設 selenium 變體
   py -3 start_webrunner.py je       # je 變體
   ```

   或者用一鍵入口 `py -3 run_batch.py`，它會先做前置檢查、印出這一輪的計畫，
   再交棒給 `start_webrunner.py`。

```{warning}
不要同時跑 `start_webrunner.py` 和 bot 的 `/run`——兩者都會 spawn webrunner，
會搶 `.chrome_profile/` 的鎖；bot 的 `/run` / `/stop` 還會「全機清掉所有
Chrome」，而它不知道獨立啟動器的子行程。
```

第一次跑會開一個真的 Chrome 視窗去登入並儲存 session（`.chrome_profile/`，
gitignored）。之後就不必再登入。

## 6. 確認可以動

```powershell
# 從 repo 根目錄做 import 煙霧測試，應印出 OK
py -3 -c "import sys; sys.path.insert(0, 'axiomatic'); import discord_bot, webrunner_novelai; print('OK')"

# 隔離的瀏覽器驗證（不碰正式設定檔與正式 profile）
py -3 axiomatic/verify_browser.py

# 整套測試
py -3 -m pytest
```

## 7.（選用）開機自動啟動

```powershell
py -3 install_autostart.py --install    # 註冊（可重複執行，冪等）
py -3 install_autostart.py --status     # 看目前註冊了什麼
py -3 install_autostart.py --remove     # 移除
```

它在 Windows 工作排程器底下註冊 `\Axiomatic\Bot` 與 `\Axiomatic\Batch` 兩個
工作，觸發條件是**目前這個使用者登入時**。為什麼不是「開機時」：批次要開一個
有桌面的 Chrome 視窗，而開機觸發的工作跑在 session 0、沒有互動桌面，Chrome 在
那裡起不來。代價是機器重開後停在鎖定畫面、沒有人登入的話工作不會觸發。

已經註冊過的話，`py -3 wake_autostart.py` 可以直接從排程器叫醒兩個監督者，
拿到的環境和登入自動啟動那一份一樣。只要其中一支就加 `--bot-only` 或
`--batch-only`。

## 建置本文件（選用）

```powershell
pip install -r docs/requirements.txt
py -3 -m sphinx -b html docs docs/_build/html
```

完成後開 `docs/_build/html/index.html`。
