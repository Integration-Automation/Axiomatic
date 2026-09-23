"""三語 help 字串（純資料）。

**這個模組不得 import discord_bot**——那會造成循環相依。它只放資料：不要在這裡
放 f-string、`.format()` 或任何會執行的東西。

指令表面是**斜線指令**；`!` 與 `@bot` 只留作不宣傳的相容路徑，所以這裡不提。
唯一的例外是 `@bot <文字>` 自由提問入口，那條刻意保留 mention 形式。

**zh-CN 段落刻意使用簡體**（給中國大陸使用者），不要「修正」成繁體——
CLAUDE.md 的語言硬規則對這一段有明文例外。

**這個檔案是手寫維護的，沒有產生器。** 原本這裡寫著「清單由 `scratchpad/gen_help.py`
從指令樹產生」——2026-09-09 查證：那支檔案不存在，連 `scratchpad/` 這個目錄都沒有。
一句指向幽靈檔案的說明比沒有說明更糟：讀的人會以為手改會被下一次重新產生蓋掉
（於是不敢改，或改了不當一回事），而隔壁的 `commands/*.md` **真的**是產生的
（`gen_command_docs.py`，DoD #2 明文禁止手改），兩者很容易混為一談。

真正在守這個檔案的是 `test_docs_sync.py`，它從指令樹用 AST 抽出每個指令、雙向比對
（有指令沒寫到 → 紅；寫了樹上沒有的 → 也紅）；`a|b|c` 併寫在比對時會展開。
群描述另有逐字回聲測試。所以**手改是正當做法**，改完跑 `test_docs_sync.py` 即可。

要不要補一支產生器是**已經評估過、刻意先不做**的。
"""

CHANNEL_HELP_SECTIONS: list[str] = [
    (
        "# Channel-only commands\n"
        "Everything here is a native slash command. Type `/` and the client will complete it for you.\n"
        "These only work in the configured channel.\n"
        "Replies are always generic: no service names, no host paths, no raw error text. Full detail goes to the log only.\n"
    ),
    (
        "## Direct commands\n"
        "- `/eta` — 預估完成時間（有終止標記只算到標記為止）\n"
        "- `/latest` — 上傳最近 N 張產出圖\n"
        "- `/queue` — 各佇列剩餘筆數與實際會跑的配對數\n"
        "- `/run` — 啟動背景產圖（可排程：in 90m / at 02:00 / cancel）\n"
        "- `/status` — 背景產圖的執行狀態與變體\n"
        "- `/stop` — 停止背景產圖\n"
    ),
    (
        "## `/todo` — Generation queues\n"
        "產圖佇列：新增 / 列出 / 刪除 / 排序\n"
        "- `/todo dedupe|duplicate|find|move|shuffle|swap`\n"
        "- `/todo char1 add|addx3|clear|list|pop|remove`\n"
        "- `/todo char2 add|clear|default|list|pop|remove`\n"
        "- `/todo negp add|clear|list|pop|remove`\n"
        "- `/todo prompt add|clear|default|end|insert|list|pop|remove|template|unend`\n"
    ),
    (
        "## `/preset` — Queue fallbacks\n"
        "佇列空時使用的提示詞預設值\n"
        "- `/preset info`\n"
        "- `/preset main append|clear|set`\n"
        "- `/preset neg append|clear|set`\n"
    ),
    (
        "## `/gen` — Batch control\n"
        "產圖批次：暫停 / 恢復 / 預覽 / 進度 / 單張\n"
        "- `/gen current|image|image_queue|pause|plan|preview|progress|resume`\n"
    ),
    (
        "## `/out` — Output browsing\n"
        "產出圖檔：統計 / 抽樣 / 歷史 / 吞吐量\n"
        "- `/out debug_show|history|latest_for|rate|sample|stats`\n"
    ),
    (
        "## `/fav` — Favorites\n"
        "收藏：總覽 / 移除 / 上傳 / 清空\n"
        "- `/fav clear|list|remove|show`\n"
    ),
    (
        "## `/log` — Runtime log\n"
        "執行紀錄：尾巴 / 搜尋 / 錯誤 / 大小 / 清空\n"
        "- `/log clear|errors|grep|size|tail`\n"
    ),
    (
        "## `/sys` — Maintenance & diagnostics\n"
        "健檢 / 診斷 / 稽核 / 磁碟 / 更新 / 重啟\n"
        "- `/sys audit|backfill_paths|cleanup_debug|dashboard|disk|doctor|git_pull|health|introspect_dom|metrics|probe_status|restart|undo|update_check`\n"
    ),
    (
        "## `/proc` — Process control\n"
        "行程（限擁有者）：列出 / 結束 / 啟動\n"
        "- `/proc kill|launch|list`\n"
    ),
    (
        "## `/config` — Batch settings\n"
        "批次參數：顯示 / 修改 / 還原 / 重載\n"
        "- `/config reload|reset|set|show`\n"
    ),
    (
        "## `/screen` — Screen\n"
        "螢幕（限擁有者）：截圖 / 區域 / 動畫 / 取色 / 讀字\n"
        "- `/screen all|gif|info|main|pixel|region|text|window`\n"
    ),
    (
        "## `/win` — Windows\n"
        "視窗（限擁有者）：列出 / 焦點 / 大小位置 / 版面\n"
        "- `/win focus|grid|list|move|pos|snap|state|wait`\n"
        "- `/win layout list|remove|restore|save`\n"
    ),
    (
        "## `/input` — Keyboard & mouse\n"
        "鍵盤與滑鼠輸入（限擁有者）\n"
        "- `/input click|hotkey|type`\n"
        "- `/input key clear|down|press|status|up`\n"
        "- `/input mouse click|dclick|down|drag|move|pos|scroll|up`\n"
    ),
    (
        "## `/clip` — Clipboard\n"
        "剪貼簿（限擁有者）：讀 / 寫 / 貼上 / 圖片 / 檔案清單\n"
        "- `/clip files|formats|image|paste|read|set|setimage`\n"
    ),
    (
        "## `/locate` — On-screen targeting\n"
        "用文字 / 圖片 / 元素名稱在畫面上定位（限擁有者）\n"
        "- `/locate gone|pixel`\n"
        "- `/locate image click|find|wait`\n"
        "- `/locate text click|find|wait`\n"
        "- `/locate ui click|find|gone|read|tree|wait`\n"
    ),
    (
        "## `/macro` — Macros\n"
        "巨集（限擁有者）：存 / 錄 / 跑 / 編輯\n"
        "- `/macro delete|edit|insert|list|record|rm_line|run|save|show|stop`\n"
    ),
    (
        "## `/host` — Host commands & file transfer\n"
        "主機（限擁有者）：執行指令 / 背景作業 / 檔案進出 / 全停\n"
        "- `/host get|panic|put`\n"
        "- `/host job clear|eof|list|log|run|send|stop`\n"
        "- `/host sh cd|run|stop`\n"
    ),
    (
        "## `/watch` — Condition watches\n"
        "條件成立時主動通知，或直接接手做事（限擁有者）\n"
        "- `/watch clip|job|list|pixel|port|process|stop|text|ui|window`\n"
    ),
    (
        "## `/schedule` — Scheduled tasks\n"
        "定時排程（限擁有者），落盤後撐得過重啟\n"
        "- `/schedule add|list|remove|run`\n"
    ),
    (
        "## `/launcher` — Standalone supervisor\n"
        "獨立批次監督者（限擁有者），跟 bot 分開執行、bot 重啟也不受影響\n"
        "- `/launcher start|status|stop`\n"
    ),
]


CHANNEL_HELP_SECTIONS_ZH_TW: list[str] = [
    (
        "# 限頻道指令\n"
        "全部都是原生斜線指令——打 `/` 就會自動補全。\n"
        "只有在設定的頻道裡才能使用。\n"
        "回覆一律泛用：不提服務名稱、不露出主機路徑、不回傳原始錯誤字串，完整細節只進 log。\n"
    ),
    (
        "## 直接指令\n"
        "- `/eta` — 預估完成時間（有終止標記只算到標記為止）\n"
        "- `/latest` — 上傳最近 N 張產出圖\n"
        "- `/queue` — 各佇列剩餘筆數與實際會跑的配對數\n"
        "- `/run` — 啟動背景產圖（可排程：in 90m / at 02:00 / cancel）\n"
        "- `/status` — 背景產圖的執行狀態與變體\n"
        "- `/stop` — 停止背景產圖\n"
    ),
    (
        "## `/todo` — 產圖佇列\n"
        "產圖佇列：新增 / 列出 / 刪除 / 排序\n"
        "- `/todo dedupe|duplicate|find|move|shuffle|swap`\n"
        "- `/todo char1 add|addx3|clear|list|pop|remove`\n"
        "- `/todo char2 add|clear|default|list|pop|remove`\n"
        "- `/todo negp add|clear|list|pop|remove`\n"
        "- `/todo prompt add|clear|default|end|insert|list|pop|remove|template|unend`\n"
    ),
    (
        "## `/preset` — 佇列空時的預設值\n"
        "佇列空時使用的提示詞預設值\n"
        "- `/preset info`\n"
        "- `/preset main append|clear|set`\n"
        "- `/preset neg append|clear|set`\n"
    ),
    (
        "## `/gen` — 批次控制\n"
        "產圖批次：暫停 / 恢復 / 預覽 / 進度 / 單張\n"
        "- `/gen current|image|image_queue|pause|plan|preview|progress|resume`\n"
    ),
    (
        "## `/out` — 產出檢視\n"
        "產出圖檔：統計 / 抽樣 / 歷史 / 吞吐量\n"
        "- `/out debug_show|history|latest_for|rate|sample|stats`\n"
    ),
    (
        "## `/fav` — 收藏\n"
        "收藏：總覽 / 移除 / 上傳 / 清空\n"
        "- `/fav clear|list|remove|show`\n"
    ),
    (
        "## `/log` — 執行紀錄\n"
        "執行紀錄：尾巴 / 搜尋 / 錯誤 / 大小 / 清空\n"
        "- `/log clear|errors|grep|size|tail`\n"
    ),
    (
        "## `/sys` — 維運與診斷\n"
        "健檢 / 診斷 / 稽核 / 磁碟 / 更新 / 重啟\n"
        "- `/sys audit|backfill_paths|cleanup_debug|dashboard|disk|doctor|git_pull|health|introspect_dom|metrics|probe_status|restart|undo|update_check`\n"
    ),
    (
        "## `/proc` — 行程控制\n"
        "行程（限擁有者）：列出 / 結束 / 啟動\n"
        "- `/proc kill|launch|list`\n"
    ),
    (
        "## `/config` — 批次參數\n"
        "批次參數：顯示 / 修改 / 還原 / 重載\n"
        "- `/config reload|reset|set|show`\n"
    ),
    (
        "## `/screen` — 螢幕\n"
        "螢幕（限擁有者）：截圖 / 區域 / 動畫 / 取色 / 讀字\n"
        "- `/screen all|gif|info|main|pixel|region|text|window`\n"
    ),
    (
        "## `/win` — 視窗\n"
        "視窗（限擁有者）：列出 / 焦點 / 大小位置 / 版面\n"
        "- `/win focus|grid|list|move|pos|snap|state|wait`\n"
        "- `/win layout list|remove|restore|save`\n"
    ),
    (
        "## `/input` — 鍵盤與滑鼠\n"
        "鍵盤與滑鼠輸入（限擁有者）\n"
        "- `/input click|hotkey|type`\n"
        "- `/input key clear|down|press|status|up`\n"
        "- `/input mouse click|dclick|down|drag|move|pos|scroll|up`\n"
    ),
    (
        "## `/clip` — 剪貼簿\n"
        "剪貼簿（限擁有者）：讀 / 寫 / 貼上 / 圖片 / 檔案清單\n"
        "- `/clip files|formats|image|paste|read|set|setimage`\n"
    ),
    (
        "## `/locate` — 畫面定位\n"
        "用文字 / 圖片 / 元素名稱在畫面上定位（限擁有者）\n"
        "- `/locate gone|pixel`\n"
        "- `/locate image click|find|wait`\n"
        "- `/locate text click|find|wait`\n"
        "- `/locate ui click|find|gone|read|tree|wait`\n"
    ),
    (
        "## `/macro` — 巨集\n"
        "巨集（限擁有者）：存 / 錄 / 跑 / 編輯\n"
        "- `/macro delete|edit|insert|list|record|rm_line|run|save|show|stop`\n"
    ),
    (
        "## `/host` — 主機指令與檔案進出\n"
        "主機（限擁有者）：執行指令 / 背景作業 / 檔案進出 / 全停\n"
        "- `/host get|panic|put`\n"
        "- `/host job clear|eof|list|log|run|send|stop`\n"
        "- `/host sh cd|run|stop`\n"
    ),
    (
        "## `/watch` — 條件監看\n"
        "條件成立時主動通知，或直接接手做事（限擁有者）\n"
        "- `/watch clip|job|list|pixel|port|process|stop|text|ui|window`\n"
    ),
    (
        "## `/schedule` — 定時排程\n"
        "定時排程（限擁有者），落盤後撐得過重啟\n"
        "- `/schedule add|list|remove|run`\n"
    ),
    (
        "## `/launcher` — 獨立監督者\n"
        "獨立批次監督者（限擁有者），跟 bot 分開執行、bot 重啟也不受影響\n"
        "- `/launcher start|status|stop`\n"
    ),
]


CHANNEL_HELP_SECTIONS_ZH_CN: list[str] = [
    (
        "# 限频道命令\n"
        "全部都是原生斜杠命令——输入 `/` 就会自动补全。\n"
        "只有在设置的频道里才能使用。\n"
        "回复一律通用：不提服务名称、不暴露主机路径、不返回原始错误字符串，完整细节只进日志。\n"
    ),
    (
        "## 直接命令\n"
        "- `/eta` — 预估完成时间（有终止标记只算到标记为止）\n"
        "- `/latest` — 上传最近 N 张产出图\n"
        "- `/queue` — 各队列剩余条数与实际会跑的配对数\n"
        "- `/run` — 启动后台生成（可定时：in 90m / at 02:00 / cancel）\n"
        "- `/status` — 后台生成的运行状态与变体\n"
        "- `/stop` — 停止后台生成\n"
    ),
    (
        "## `/todo` — 生成队列\n"
        "生成队列：新增 / 列出 / 删除 / 排序\n"
        "- `/todo dedupe|duplicate|find|move|shuffle|swap`\n"
        "- `/todo char1 add|addx3|clear|list|pop|remove`\n"
        "- `/todo char2 add|clear|default|list|pop|remove`\n"
        "- `/todo negp add|clear|list|pop|remove`\n"
        "- `/todo prompt add|clear|default|end|insert|list|pop|remove|template|unend`\n"
    ),
    (
        "## `/preset` — 队列为空时的默认值\n"
        "队列为空时使用的提示词默认值\n"
        "- `/preset info`\n"
        "- `/preset main append|clear|set`\n"
        "- `/preset neg append|clear|set`\n"
    ),
    (
        "## `/gen` — 批次控制\n"
        "生成批次：暂停 / 恢复 / 预览 / 进度 / 单张\n"
        "- `/gen current|image|image_queue|pause|plan|preview|progress|resume`\n"
    ),
    (
        "## `/out` — 产出查看\n"
        "产出图片：统计 / 抽样 / 历史 / 吞吐量\n"
        "- `/out debug_show|history|latest_for|rate|sample|stats`\n"
    ),
    (
        "## `/fav` — 收藏\n"
        "收藏：总览 / 移除 / 上传 / 清空\n"
        "- `/fav clear|list|remove|show`\n"
    ),
    (
        "## `/log` — 运行日志\n"
        "运行日志：末尾 / 搜索 / 错误 / 大小 / 清空\n"
        "- `/log clear|errors|grep|size|tail`\n"
    ),
    (
        "## `/sys` — 运维与诊断\n"
        "健康检查 / 诊断 / 审计 / 磁盘 / 更新 / 重启\n"
        "- `/sys audit|backfill_paths|cleanup_debug|dashboard|disk|doctor|git_pull|health|introspect_dom|metrics|probe_status|restart|undo|update_check`\n"
    ),
    (
        "## `/proc` — 进程控制\n"
        "进程（限拥有者）：列出 / 结束 / 启动\n"
        "- `/proc kill|launch|list`\n"
    ),
    (
        "## `/config` — 批次参数\n"
        "批次参数：显示 / 修改 / 还原 / 重新加载\n"
        "- `/config reload|reset|set|show`\n"
    ),
    (
        "## `/screen` — 屏幕\n"
        "屏幕（限拥有者）：截图 / 区域 / 动画 / 取色 / 读字\n"
        "- `/screen all|gif|info|main|pixel|region|text|window`\n"
    ),
    (
        "## `/win` — 窗口\n"
        "窗口（限拥有者）：列出 / 焦点 / 大小位置 / 布局\n"
        "- `/win focus|grid|list|move|pos|snap|state|wait`\n"
        "- `/win layout list|remove|restore|save`\n"
    ),
    (
        "## `/input` — 键盘与鼠标\n"
        "键盘与鼠标输入（限拥有者）\n"
        "- `/input click|hotkey|type`\n"
        "- `/input key clear|down|press|status|up`\n"
        "- `/input mouse click|dclick|down|drag|move|pos|scroll|up`\n"
    ),
    (
        "## `/clip` — 剪贴板\n"
        "剪贴板（限拥有者）：读 / 写 / 粘贴 / 图片 / 文件列表\n"
        "- `/clip files|formats|image|paste|read|set|setimage`\n"
    ),
    (
        "## `/locate` — 画面定位\n"
        "用文字 / 图片 / 元素名称在屏幕上定位（限拥有者）\n"
        "- `/locate gone|pixel`\n"
        "- `/locate image click|find|wait`\n"
        "- `/locate text click|find|wait`\n"
        "- `/locate ui click|find|gone|read|tree|wait`\n"
    ),
    (
        "## `/macro` — 宏\n"
        "宏（限拥有者）：保存 / 录制 / 运行 / 编辑\n"
        "- `/macro delete|edit|insert|list|record|rm_line|run|save|show|stop`\n"
    ),
    (
        "## `/host` — 主机命令与文件传输\n"
        "主机（限拥有者）：执行命令 / 后台任务 / 文件收发 / 全停\n"
        "- `/host get|panic|put`\n"
        "- `/host job clear|eof|list|log|run|send|stop`\n"
        "- `/host sh cd|run|stop`\n"
    ),
    (
        "## `/watch` — 条件监视\n"
        "条件成立时主动通知，或直接接手处理（限拥有者）\n"
        "- `/watch clip|job|list|pixel|port|process|stop|text|ui|window`\n"
    ),
    (
        "## `/schedule` — 定时计划\n"
        "定时任务（限拥有者），落盘后能扛过重启\n"
        "- `/schedule add|list|remove|run`\n"
    ),
    (
        "## `/launcher` — 独立监督者\n"
        "独立批次监督者（限拥有者），跟 bot 分开运行、bot 重启也不受影响\n"
        "- `/launcher start|status|stop`\n"
    ),
]


MENTION_HELP_SECTIONS: list[str] = [
    (
        "# Cross-channel commands\n"
        "These work in any channel the bot can see.\n"
        "`@bot <text>` (no subcommand) is the free-text question entry — it stays a mention because a chat message can carry multi-line text, attachments and reply context that a slash option box cannot.\n"
        "Replies are always generic: no service names, no host paths, no raw error text. Full detail goes to the log only.\n"
    ),
    (
        "## Direct commands\n"
        "- `/booru` — 圖庫搜圖：tag 隨機一張（可模糊；不帶 tag 用預設圖）\n"
        "- `/e621` — furry 取向圖庫隨機一張（預設 NSFW；加 rating:safe 限 SFW）\n"
        "- `/grid` — 圖庫最新 4 張拼 2x2 上傳（等同 /booru 同時開 latest 與 grid）\n"
        "- `/help` — 指令說明（tw / cn / en）\n"
        "- `/iqdb` — 跨圖庫反向圖搜（回前幾名來源＋相似度 %）\n"
        "- `/nsfw` — 圖庫搜圖 NSFW 捷徑（自動補 rating:explicit）\n"
        "- `/safebooru` — 全站 SFW 圖庫隨機一張\n"
    ),
    (
        "## `/dorossi` — Assistant backend\n"
        "對話後端（限擁有者）：提問 / 工作階段 / 佇列 / 狀態\n"
        "- `/dorossi abort|ai|ask|compact|effort|errors|fullmode|health|logs|model|retry|running|status|tokens|workspace_clean`\n"
        "- `/dorossi allowdir add|list|remove`\n"
        "- `/dorossi queue clear|detail|failed_clear|move|remove|retry_failed|show|undo`\n"
        "- `/dorossi session archive|continue|delete|export|list|new|rename|reset|switch`\n"
        "- Session-level tuning tokens at the start of a question (set once, reused by every later turn of that session): `@bot /model <opus|sonnet|haiku|fable|default> <text>`, `@bot /effort <low|medium|high|xhigh|max> <text>`; pinned versions are written the same way, e.g. `<opus-5|sonnet-4.6>`\n"
    ),
    (
        "## `/tag` — Gallery tag tools\n"
        "圖庫 tag 工具：post 數 / wiki / 建議 / 補全\n"
        "- `/tag autocomplete|count|suggest|wiki`\n"
    ),
    (
        "## `/fun` — Fun & random\n"
        "趣味 / 隨機小工具\n"
        "- `/fun 8ball|ascii|calc|choose|coinflip|rand|reverse|roll|rps|timer`\n"
    ),
    (
        "## `/tool` — Encoding & utilities\n"
        "編碼 / 雜項工具\n"
        "- `/tool base64|color|hash|qr|say|unbase64|urldecode|urlencode`\n"
    ),
    (
        "## `/info` — Info & metadata\n"
        "頭貼 / server / channel 資訊\n"
        "- `/info avatar|channel|ping|server|uptime|version`\n"
    ),
    (
        "## `/web` — Public data lookups\n"
        "百科 / 字典 / 漫畫 / 隨機內容 / 公開資料查詢\n"
        "- `/web anime|cat|crypto|dict|dog|fact|github|joke|quote|wiki|xkcd`\n"
    ),
]


MENTION_HELP_SECTIONS_ZH_TW: list[str] = [
    (
        "# 跨頻道指令\n"
        "在 bot 看得到的任何頻道都能用。\n"
        "`@bot <文字>`（不接子指令）是自由提問入口——它保留 mention 形式，因為一則訊息帶得動多行內容、附件與回覆脈絡，斜線的選項輸入框帶不動。\n"
        "回覆一律泛用：不提服務名稱、不露出主機路徑、不回傳原始錯誤字串，完整細節只進 log。\n"
    ),
    (
        "## 直接指令\n"
        "- `/booru` — 圖庫搜圖：tag 隨機一張（可模糊；不帶 tag 用預設圖）\n"
        "- `/e621` — furry 取向圖庫隨機一張（預設 NSFW；加 rating:safe 限 SFW）\n"
        "- `/grid` — 圖庫最新 4 張拼 2x2 上傳（等同 /booru 同時開 latest 與 grid）\n"
        "- `/help` — 指令說明（tw / cn / en）\n"
        "- `/iqdb` — 跨圖庫反向圖搜（回前幾名來源＋相似度 %）\n"
        "- `/nsfw` — 圖庫搜圖 NSFW 捷徑（自動補 rating:explicit）\n"
        "- `/safebooru` — 全站 SFW 圖庫隨機一張\n"
    ),
    (
        "## `/dorossi` — 對話後端\n"
        "對話後端（限擁有者）：提問 / 工作階段 / 佇列 / 狀態\n"
        "- `/dorossi abort|ai|ask|compact|effort|errors|fullmode|health|logs|model|retry|running|status|tokens|workspace_clean`\n"
        "- `/dorossi allowdir add|list|remove`\n"
        "- `/dorossi queue clear|detail|failed_clear|move|remove|retry_failed|show|undo`\n"
        "- `/dorossi session archive|continue|delete|export|list|new|rename|reset|switch`\n"
        "- 提問開頭可加微調 token（工作階段層級，設一次之後同一個工作階段每一輪都沿用）：`@bot /model <opus|sonnet|haiku|fable|default> <提問>`、`@bot /effort <low|medium|high|xhigh|max> <提問>`；要釘住版本就照同樣寫法指定，例如 `<opus-5|sonnet-4.6>`\n"
    ),
    (
        "## `/tag` — 圖庫 tag 工具\n"
        "圖庫 tag 工具：post 數 / wiki / 建議 / 補全\n"
        "- `/tag autocomplete|count|suggest|wiki`\n"
    ),
    (
        "## `/fun` — 趣味 / 隨機\n"
        "趣味 / 隨機小工具\n"
        "- `/fun 8ball|ascii|calc|choose|coinflip|rand|reverse|roll|rps|timer`\n"
    ),
    (
        "## `/tool` — 編碼與小工具\n"
        "編碼 / 雜項工具\n"
        "- `/tool base64|color|hash|qr|say|unbase64|urldecode|urlencode`\n"
    ),
    (
        "## `/info` — 資訊與元資料\n"
        "頭貼 / server / channel 資訊\n"
        "- `/info avatar|channel|ping|server|uptime|version`\n"
    ),
    (
        "## `/web` — 公開資料查詢\n"
        "百科 / 字典 / 漫畫 / 隨機內容 / 公開資料查詢\n"
        "- `/web anime|cat|crypto|dict|dog|fact|github|joke|quote|wiki|xkcd`\n"
    ),
]


MENTION_HELP_SECTIONS_ZH_CN: list[str] = [
    (
        "# 跨频道命令\n"
        "在 bot 能看到的任何频道都能用。\n"
        "`@bot <文字>`（不接子命令）是自由提问入口——它保留 mention 形式，因为一条消息带得动多行内容、附件与回复上下文，斜杠的选项输入框带不动。\n"
        "回复一律通用：不提服务名称、不暴露主机路径、不返回原始错误字符串，完整细节只进日志。\n"
    ),
    (
        "## 直接命令\n"
        "- `/booru` — 图库搜图：tag 随机一张（可模糊；不带 tag 用默认图）\n"
        "- `/e621` — furry 取向图库随机一张（默认 NSFW；加 rating:safe 限 SFW）\n"
        "- `/grid` — 图库最新 4 张拼 2x2 上传（等同 /booru 同时开 latest 与 grid）\n"
        "- `/help` — 命令说明（tw / cn / en）\n"
        "- `/iqdb` — 跨图库反向图搜（返回前几名来源＋相似度 %）\n"
        "- `/nsfw` — 图库搜图 NSFW 快捷方式（自动补 rating:explicit）\n"
        "- `/safebooru` — 全站 SFW 图库随机一张\n"
    ),
    (
        "## `/dorossi` — 对话后端\n"
        "对话后端（限拥有者）：提问 / 会话 / 队列 / 状态\n"
        "- `/dorossi abort|ai|ask|compact|effort|errors|fullmode|health|logs|model|retry|running|status|tokens|workspace_clean`\n"
        "- `/dorossi allowdir add|list|remove`\n"
        "- `/dorossi queue clear|detail|failed_clear|move|remove|retry_failed|show|undo`\n"
        "- `/dorossi session archive|continue|delete|export|list|new|rename|reset|switch`\n"
        "- 提问开头可加微调 token（会话层级，设一次之后同一个会话每一轮都沿用）：`@bot /model <opus|sonnet|haiku|fable|default> <问题>`、`@bot /effort <low|medium|high|xhigh|max> <问题>`；要钉住版本就照同样写法指定，例如 `<opus-5|sonnet-4.6>`\n"
    ),
    (
        "## `/tag` — 图库 tag 工具\n"
        "图库 tag 工具：post 数 / wiki / 建议 / 补全\n"
        "- `/tag autocomplete|count|suggest|wiki`\n"
    ),
    (
        "## `/fun` — 趣味 / 随机\n"
        "趣味 / 随机小工具\n"
        "- `/fun 8ball|ascii|calc|choose|coinflip|rand|reverse|roll|rps|timer`\n"
    ),
    (
        "## `/tool` — 编码与小工具\n"
        "编码 / 杂项工具\n"
        "- `/tool base64|color|hash|qr|say|unbase64|urldecode|urlencode`\n"
    ),
    (
        "## `/info` — 信息与元数据\n"
        "头像 / server / channel 信息\n"
        "- `/info avatar|channel|ping|server|uptime|version`\n"
    ),
    (
        "## `/web` — 公开数据查询\n"
        "百科 / 字典 / 漫画 / 随机内容 / 公开数据查询\n"
        "- `/web anime|cat|crypto|dict|dog|fact|github|joke|quote|wiki|xkcd`\n"
    ),
]


HELPS: dict[str, dict[str, list[str]]] = {
    "zh-tw": {
        "channel": CHANNEL_HELP_SECTIONS_ZH_TW,
        "mention": MENTION_HELP_SECTIONS_ZH_TW,
    },
    "zh-cn": {
        "channel": CHANNEL_HELP_SECTIONS_ZH_CN,
        "mention": MENTION_HELP_SECTIONS_ZH_CN,
    },
    "en": {
        "channel": CHANNEL_HELP_SECTIONS,
        "mention": MENTION_HELP_SECTIONS,
    },
}
