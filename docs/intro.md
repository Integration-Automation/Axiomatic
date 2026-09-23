# 專案簡介與架構

## 系統概觀

```text
        ┌─────────────────┐   待辦 / 設定 (檔案)   ┌──────────────────────┐
        │  discord_bot.py │ ─────────────────────▶ │  webrunner_*.py      │
        │  （遠端遙控）   │                        │  （瀏覽器批次產圖）  │
        │                 │ ◀───────────────────── │                      │
        └─────────────────┘   圖片 / log / 事件     └──────────────────────┘
                 ▲                                            │
                 │ Discord 斜線指令                           │ 登入 + 產圖
                 │                                            ▼
            你（使用者）                                  出圖服務網站
```

- 你在 Discord 用斜線指令把角色、prompt 排進待辦佇列。
- webrunner 從佇列讀出工作，登入出圖服務、逐一填欄位、連續產圖、存到
  `output/<角色>/`。
- webrunner 把進度寫成事件（`events.ndjson`）與紀錄（`webrunner.log`），bot
  讀回後在頻道貼通知，並提供 `/gen progress` / `/gen current` / `/out rate` 等查詢。

## 模組邊界（重要原則）

兩個長時間執行的腳本**只透過磁碟上的檔案溝通**，永遠不互相 `import`：

| 產生者 | 檔案 | 消費者 |
|---|---|---|
| `discord_bot.py` | `todo_prompt.md`、`todo_character{1,2}.md`、`todo_undesired.md`、`prompt.md`、`undesired.md`、`webrunner.pid` | webrunner |
| `webrunner_*.py` | `output/<角色>/*.png`、`debug_*.png`、`webrunner.log`、`events.ndjson` | bot（`/log tail`、`/latest`、`/gen progress`、`/gen current`…） |

跨行程的狀態都留在磁碟，所以 bot 重啟是無害的——它會從 `webrunner.pid`
重新認領仍在跑的 webrunner，從 `.backup/` 重建 undo 堆疊，從 `events.ndjson`
的尾端接續監看。

## 兩個 webrunner 變體

| 變體 | 說明 |
|---|---|
| `webrunner_je_only.py` | 以 `je_web_runner` 套件驅動，啟動較輕 |
| `webrunner_novelai.py` | 純 Selenium / undetected driver，反偵測較強 |

兩支變體共用 `_webrunner_shared.py` 這一份核心（DOM 操作 ＋ 純函式 ＋ 批次主
迴圈），差別只在怎麼取得 driver。改動作行為要**同時**顧到兩支。

`/run` 會**先試 je 變體**，若 5 分鐘啟動視窗內非零退出（通常是 `/login` 被
反機器人擋下）就自動 fallback 到 selenium 變體並在頻道公告切換。

## 目錄結構（重點）

```text
Axiomatic/
├── axiomatic/
│   ├── webrunner_novelai.py     # Selenium 批次產圖（長時間執行）
│   ├── webrunner_je_only.py     # je_web_runner 變體
│   ├── discord_bot.py           # Discord 事件迴圈
│   ├── dorossi_backend.py       # Dorossi 問答後端（bot 端）
│   ├── _batch_config.py         # batch_config.json 載入 / 寫入
│   ├── _bot_config.py           # bot_config.json 載入
│   ├── _webrunner_shared.py     # 兩個 webrunner 變體共用的核心（run_batch）
│   ├── _queue_consume.py        # 動態佇列消耗決策（純函式）
│   ├── _run_progress.py         # 中斷角色的續跑檢查點
│   ├── _supervisor.py           # 監督器退避的純數學（兩個啟動器共用）
│   ├── _process_control.py      # 行程探查 / 終止原語（bot 端）
│   ├── _gui_control.py          # 桌面自動化門面（bot 端；實作在函式庫）
│   └── presence_probe.py        # 本機 presence（遊戲 / 音樂）偵測
├── start_webrunner.py           # webrunner 監督式啟動器（je / selenium）
├── start_discord_bot.py         # bot 監督式啟動器（無限重啟）
├── batch_config.json            # 產圖批次參數（hot-reload，每角色重讀；唯一追蹤的設定檔）
├── *.example.json / *.example.md  # 版本庫帶的範本，複製成同名正式檔再填
├── bot_config.json              # bot runtime 設定（啟動時讀一次；gitignored）
├── auth.md / discord_bot_token.md  # 憑證（gitignored）
├── prompt.md / character{1,2}.md / undesired.md  # 各佇列為空時的 fallback（gitignored）
├── todo_prompt.md / todo_character{1,2}.md / todo_undesired.md  # 待辦佇列（gitignored）
├── output/                      # 產出 PNG（gitignored）
├── .chrome_profile/             # 保存的 Chrome 登入狀態（gitignored）
└── webrunner.{log,pid} / events.ndjson         # 行程產物
```
