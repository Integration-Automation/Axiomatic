# Axiomatic 文件

本專案是一套以 **瀏覽器自動化 ＋ Discord 機器人** 組成的批次產圖自動化系統。
你把要跑的角色與 prompt 排進待辦佇列，長時間執行的 webrunner 會逐一登入
出圖服務、填好欄位、連續產圖並存檔；Discord 機器人則是你的遠端遙控器與監控
面板——排隊、啟動／停止、看進度、調參數、抓錯誤，全部用斜線指令完成。

```{note}
本文件是 **Read the Docs（Sphinx）格式** 的靜態文件，內容以程式碼為準
（bot 內建的 `/help` 才是即時的單一真實來源）。指令若與 bot 回覆不一致，
以 bot 為準。
```

## 兩個長時間執行的行程

| 行程 | 角色 | 檔案 |
|---|---|---|
| **webrunner** | 真正去出圖服務產圖的瀏覽器批次程式 | `webrunner_novelai.py` / `webrunner_je_only.py` |
| **discord_bot** | 遠端遙控 + 監控的 Discord 機器人 | `discord_bot.py` |

兩者**只透過檔案溝通**（待辦清單、設定 JSON、輸出圖片、事件記錄），彼此不直接
`import`。因此任一邊重啟都不會弄壞另一邊的狀態。

## 從這裡開始

```{toctree}
:maxdepth: 2
:caption: 目錄

intro
setup
workflow
commands_channel
commands_mention
config
troubleshooting
```

## 快速索引

- 第一次安裝、怎麼把 bot 跟 webrunner 跑起來 → {doc}`setup`
- 待辦佇列怎麼配對、fallback 規則、`end` 終止標記、排程節奏 → {doc}`workflow`
- 全部限頻道斜線指令（佇列、批次、維運、桌面自動化） → {doc}`commands_channel`
- 跨頻道斜線指令（圖庫／工具、Dorossi 問答） → {doc}`commands_mention`
- `batch_config.json` / `bot_config.json` / presence 設定 → {doc}`config`
- 跑批卡住、Chrome 崩潰、登入失敗怎麼查 → {doc}`troubleshooting`
