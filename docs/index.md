# Axiomatic 文件

本專案是一台**用聊天操作的自動化主機**。你人在哪個聊天平台，就從那裡對這台機器
下指令，它替你做事：桌面與視窗自動化、行程與檔案操作、定時工作、對話問答，以及
一條長時間執行的瀏覽器批次——把你排進待辦佇列的角色與 prompt 逐一登入出圖服務、
填好欄位、連續產圖並存檔。

**沒有任何單一平台是這套系統的身分。** 聊天表面是一層介接：**一個平台一個**受監督
的行程，每個平台都能各自關掉，各有自己的鎖、記錄檔與狀態。

```{note}
本文件是 **Read the Docs（Sphinx）格式** 的靜態文件，內容以程式碼為準
（bot 內建的 `/help` 才是即時的單一真實來源）。指令若與 bot 回覆不一致，
以 bot 為準。
```

## 兩種長時間執行的行程

| 行程 | 角色 | 檔案 |
|---|---|---|
| **平台行程** | 接指令、判斷是誰在問、編輯佇列檔、監督工作、回報結果。**一個聊天平台一個** | `discord_bot.py`（模組名是歷史遺留，它不綁任何平台）＋ `_chat_platform.py`、`_*_transport.py` |
| **webrunner** | 出圖這個工作負載：真正去操作瀏覽器的批次程式 | `webrunner_novelai.py` / `webrunner_je_only.py` |

兩者**只透過檔案溝通**（待辦清單、設定 JSON、輸出圖片、事件記錄），彼此不直接
`import`。因此任一邊重啟都不會弄壞另一邊的狀態；平台行程之間也一樣，它們不共用
任何可變狀態。

## 從這裡開始

```{toctree}
:maxdepth: 2
:caption: 目錄

intro
setup
workflow
commands_channel
commands_mention
platforms
config
troubleshooting
```

## 快速索引

- 第一次安裝、怎麼把 bot 跟 webrunner 跑起來 → {doc}`setup`
- 待辦佇列怎麼配對、fallback 規則、`end` 終止標記、排程節奏 → {doc}`workflow`
- 全部限頻道斜線指令（佇列、批次、維運、桌面自動化） → {doc}`commands_channel`
- 跨頻道斜線指令（圖庫／工具、Dorossi 問答） → {doc}`commands_mention`
- 在 Discord 以外的平台上用這個 bot（一個平台一個行程） → {doc}`platforms`
- `batch_config.json` / `bot_config.json` / presence 設定 → {doc}`config`
- 跑批卡住、Chrome 崩潰、登入失敗怎麼查 → {doc}`troubleshooting`
