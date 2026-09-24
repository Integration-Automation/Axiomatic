# 指令總覽

以 bot 即時 `/help` 為準（code 是 source of truth）。此為靜態快照，方便外部
分享。對話平台不 render markdown table，全用 bullet。

想看**逐個參數的型別、值域與權限**，到 [`commands/`](commands/README.md)——那裡
一個指令群一檔，由指令樹產生。本檔是一頁看完的總覽。

**這一份是指令本身，與平台無關。** 在有原生斜線選單的平台上，它們就是斜線指令：
打 `/` 會自動補全，參數有型別與值域檢查。沒有斜線選單的平台走文字表面，指令名與
權限閘完全一樣（見 [`docs/platforms.md`](docs/platforms.md)）。

🔒 **限頻道**：只能在設定的頻道（`channel_id`）使用；擁有者可跨頻道。
🌐 **跨頻道**：bot 看得到的任何頻道都能用。
🔑 **限擁有者**：操作 bot 那台機器的指令，閘門在派發前且**不看 `user_roles`**
——`/input`、`/screen`、`/win`、`/clip`、`/locate`、`/macro`、`/watch`、
`/proc`、`/host` 整群，加上 `/sys restart|git_pull|undo|audit|cleanup_debug`
`|introspect_dom|dashboard`、`/out debug_show`、`/config set|reset|reload`、`/log clear`、
`/schedule`、`/launcher` 全部、`/gen image|image_queue`。

回覆一律泛用：不提服務名稱、不露出主機路徑、不回傳原始錯誤字串，完整細節
只進 log。這是專案的硬性保密規則，寫新指令時要沿用。

---

## 🔒 限頻道指令

### 直接指令

- `/eta` — 預估完成時間（有終止標記只算到標記為止）
- `/latest [n]` — 上傳最近 N 張產出圖
- `/queue` — 各佇列剩餘筆數與實際會跑的配對數
- `/run [when]` — 啟動背景產圖（可排程：in 90m / at 02:00 / cancel）
- `/status` — 背景產圖的執行狀態與變體
- `/stop` — 停止背景產圖

### `/todo` — 產圖佇列

產圖佇列：新增 / 列出 / 刪除 / 排序

- `/todo dedupe <queue>` — 移除重複條目（保留第一筆）
- `/todo duplicate <queue> <index> [n]` — 複製某一筆 n 次
- `/todo find <queue> <text>` — 在佇列內搜尋（不分大小寫）
- `/todo move <queue> <index> <where>` — 把某一筆搬到最前或最後
- `/todo shuffle <queue>` — 打亂佇列順序
- `/todo swap <queue> <i> <j>` — 對調兩筆的位置

**`/todo char1`** — 角色1佇列

- `/todo char1 add [text]` — 在角色1佇列後面新增一筆
- `/todo char1 addx3 [text]` — 新增到角色1 佇列並重複 ×3
- `/todo char1 clear` — 清空角色1佇列
- `/todo char1 list` — 列出角色1佇列的所有條目
- `/todo char1 pop` — 刪除角色1佇列最後一筆
- `/todo char1 remove <index>` — 刪除角色1佇列第 i 筆

**`/todo char2`** — 角色2佇列

- `/todo char2 add [text]` — 在角色2佇列後面新增一筆
- `/todo char2 clear` — 清空角色2佇列
- `/todo char2 default` — 把角色2 預設內容當一筆推進佇列
- `/todo char2 list` — 列出角色2佇列的所有條目
- `/todo char2 pop` — 刪除角色2佇列最後一筆
- `/todo char2 remove <index>` — 刪除角色2佇列第 i 筆

**`/todo negp`** — 負面提示詞佇列

- `/todo negp add [text]` — 在負面提示詞佇列後面新增一筆
- `/todo negp clear` — 清空負面提示詞佇列
- `/todo negp list` — 列出負面提示詞佇列的所有條目
- `/todo negp pop` — 刪除負面提示詞佇列最後一筆
- `/todo negp remove <index>` — 刪除負面提示詞佇列第 i 筆

**`/todo prompt`** — 主提示詞佇列

- `/todo prompt add [text]` — 在主提示詞佇列後面新增一筆
- `/todo prompt clear` — 清空主提示詞佇列
- `/todo prompt default` — 把預設提示詞當一筆推進佇列
- `/todo prompt end` — 追加終止標記，跑到那筆就乾淨收工
- `/todo prompt insert <index> <text>` — 把內容插進主提示詞佇列第 i 筆
- `/todo prompt list` — 列出主提示詞佇列的所有條目
- `/todo prompt pop` — 刪除主提示詞佇列最後一筆
- `/todo prompt remove <index>` — 刪除主提示詞佇列第 i 筆
- `/todo prompt template [name]` — 把指定範本當一筆推進佇列
- `/todo prompt unend` — 移除所有終止標記，讓佇列跑完整批

### `/preset` — 佇列空時的預設值

佇列空時使用的提示詞預設值

- `/preset info` — 列出兩個預設值與四個佇列的內容

**`/preset main`** — 主提示詞預設

- `/preset main append [text]` — 在主提示詞預設後面追加文字
- `/preset main clear` — 清空主提示詞預設
- `/preset main set [text]` — 覆寫主提示詞預設

**`/preset neg`** — 負面提示詞預設

- `/preset neg append [text]` — 在負面提示詞預設後面追加文字
- `/preset neg clear` — 清空負面提示詞預設
- `/preset neg set [text]` — 覆寫負面提示詞預設

### `/gen` — 批次控制

產圖批次：暫停 / 恢復 / 預覽 / 進度 / 單張

- `/gen current` — 目前正在產的角色與進度
- `/gen image <prompt> [character1] [character2] [negative]` — 依提示詞產生單張圖（限擁有者）
- `/gen image_queue [args]` — 單張產圖佇列（限擁有者）
- `/gen pause [after]` — 在安全邊界暫停背景產圖
- `/gen plan [n]` — 完整跑批計畫（會依序跑的所有配對）
- `/gen preview` — 預覽下一輪要跑的三元組
- `/gen progress` — 各角色對目標張數的進度條
- `/gen resume` — 恢復已暫停的批次

### `/out` — 產出檢視

產出圖檔：統計 / 抽樣 / 歷史 / 吞吐量

- `/out debug_show [name]` — 列出或上傳除錯截圖（限擁有者）
- `/out history [n]` — 近期產出時間表
- `/out latest_for [character]` — 指定角色的最新一張
- `/out rate` — 產圖吞吐量（近 1、6、24 小時的張數與平均）
- `/out sample <character> [n]` — 隨機抽幾張
- `/out stats` — 各輸出資料夾的圖檔數

### `/fav` — 收藏

收藏：總覽 / 移除 / 上傳 / 清空

- `/fav clear <character>` — 清空收藏（`*` = 全部角色）
- `/fav list [character]` — 收藏總覽，或某角色的編號清單
- `/fav remove <character> <index>` — 移除該角色第 i 筆收藏
- `/fav show <character> [n]` — 上傳該角色的前 N 張收藏

### `/log` — 執行紀錄

執行紀錄：尾巴 / 搜尋 / 錯誤 / 大小 / 清空

- `/log clear` — 清空紀錄檔
- `/log errors` — 列出最近的警告與錯誤行
- `/log grep <text>` — 在紀錄檔內搜尋（不分大小寫）
- `/log size` — 紀錄檔大小與行數
- `/log tail [n]` — 紀錄檔最後 N 行

### `/sys` — 維運與診斷

健檢 / 診斷 / 稽核 / 磁碟 / 更新 / 重啟

- `/sys audit [n] [grep]` — 最近的操作稽核紀錄
- `/sys backfill_paths [limit] [apply]` — 把本頻道舊訊息裡的目錄補成完整顯示
- `/sys cleanup_debug` — 刪除所有除錯截圖
- `/sys dashboard` — 本機唯讀儀表板的網址
- `/sys disk` — 輸出與設定檔目錄的磁碟用量
- `/sys doctor` — 非破壞式排障摘要
- `/sys git_pull [noreboot]` — 拉取更新（預設成功後重啟）
- `/sys health [mode]` — 綜合健康檢查
- `/sys introspect_dom` — 要求背景程式 dump 目前頁面的輸入欄位
- `/sys metrics` — 執行期指標（uptime / 次數 / 錯誤）
- `/sys probe_status` — 狀態偵測的根因摘要
- `/sys restart` — 結束行程讓監督者重新啟動（沒有監督者時拒絕，免得只是把 bot 關掉）
- `/sys undo` — 撤回最近一次破壞性寫入
- `/sys update_check` — 比對本機與遠端的版本落差

### `/proc` — 行程控制

行程（限擁有者）：列出 / 結束 / 啟動 / 占用

- `/proc kill <name>` — 強制結束指定程式
- `/proc launch <name>` — 啟動白名單或別名上的程式
- `/proc list [substr]` — 列出正在執行的程式
- `/proc usage` — 本專案占用的資源與行程數（依角色分組）

### `/config` — 批次參數

批次參數：顯示 / 修改 / 還原 / 重載

- `/config reload` — 重載執行期可熱更新的設定
- `/config reset <key>` — 還原某項設定，或 all 全部還原
- `/config set <key> <value>` — 修改一項設定
- `/config show` — 顯示目前各項設定

### `/screen` — 螢幕

螢幕（限擁有者）：截圖 / 區域 / 動畫 / 取色 / 讀字

- `/screen all` — 整個虛擬桌面（多螢幕時唯一看得到副螢幕的方式）
- `/screen gif [seconds] [fps]` — 連拍成一小段動畫
- `/screen info` — 解析度、虛擬桌面範圍、螢幕數、是否鎖定
- `/screen main` — 主螢幕截圖
- `/screen pixel <x> <y>` — 取某一點的顏色（RGB ＋ hex）
- `/screen region <x> <y> <width> <height>` — 指定區域截圖
- `/screen text [region] [lang]` — 把畫面上的文字整段讀出來
- `/screen window <title>` — 只截指定視窗的矩形

### `/win` — 視窗

視窗（限擁有者）：列出 / 焦點 / 大小位置 / 版面

- `/win focus <substr>` — 把視窗拉到前景
- `/win grid <windows>` — 把多個視窗排成方格
- `/win list [substr]` — 列出可見視窗
- `/win move <x> <y> <substr> [width] [height]` — 搬位置，可順便改大小
- `/win pos <substr>` — 位置 / 大小 / 中心點
- `/win snap <position> <substr>` — 靠到螢幕的某一半或某一角
- `/win state <action> <substr>` — 關閉 / 最小化 / 最大化 / 還原 / 顯示 / 隱藏
- `/win wait <substr> [seconds]` — 等視窗出現

**`/win layout`** — 整個桌面的視窗版面：存檔與還原

- `/win layout list` — 列出已存的版面
- `/win layout remove <name>` — 刪除已存的版面
- `/win layout restore <name>` — 還原已存的版面
- `/win layout save <name>` — 把目前版面存成名稱

### `/input` — 鍵盤與滑鼠

鍵盤與滑鼠輸入（限擁有者）

- `/input click <x> <y> [button] [window] [background] [shot]` — 在指定座標按一下
- `/input hotkey <combo>` — 送出快捷鍵組合
- `/input type <text>` — 把文字原樣輸入到焦點視窗

**`/input key`** — 按住不放（狀態留在主機上，300 秒自動放開）

- `/input key clear` — 放開全部按住的鍵
- `/input key down <name>` — 按住某個鍵不放
- `/input key press <name> [window]` — 按一下某個鍵
- `/input key status` — 目前按住哪些鍵、各多久
- `/input key up <name>` — 放開某個鍵

**`/input mouse`** — 滑鼠

- `/input mouse click [button] [x] [y]` — 按一下（不給座標就點目前位置）
- `/input mouse dclick <x> <y> [button]` — 雙擊
- `/input mouse down [button] [x] [y]` — 按住滑鼠鍵不放
- `/input mouse drag <x1> <y1> <x2> <y2> [button]` — 拖曳
- `/input mouse move <x> <y>` — 移動游標
- `/input mouse pos` — 目前游標座標
- `/input mouse scroll <amount> [x] [y]` — 捲動（正值往上）
- `/input mouse up [button] [x] [y]` — 放開滑鼠鍵

### `/clip` — 剪貼簿

剪貼簿（限擁有者）：讀 / 寫 / 貼上 / 圖片 / 檔案清單

- `/clip files` — 剪貼簿裡的檔案清單（只回數量與副檔名）
- `/clip formats` — 剪貼簿現在放著哪幾種內容
- `/clip image` — 讀剪貼簿裡的圖片
- `/clip paste <text>` — 寫入後直接貼上（輸入中日文只能走這條）
- `/clip read` — 讀剪貼簿文字
- `/clip set <text>` — 寫入剪貼簿
- `/clip setimage <image>` — 把附件圖片寫進剪貼簿

### `/locate` — 畫面定位

用文字 / 圖片 / 元素名稱在畫面上定位（限擁有者）

- `/locate gone <kind> <target> [seconds]` — 等某個東西從畫面上消失
- `/locate pixel <x> <y> <color> [invert] [seconds] [tolerance]` — 等某一點變成（或不再是）指定顏色

**`/locate image`** — 用樣板圖片定位（不需辨識引擎）

- `/locate image click <template> [threshold]` — 找到樣板圖片就點它的中心
- `/locate image find <template> [threshold]` — 找出畫面上所有命中處，不點下去
- `/locate image wait <template> [seconds] [threshold]` — 等樣板圖片出現在畫面上

**`/locate text`** — 用畫面文字定位（需辨識引擎）

- `/locate text click <text> [lang] [shot]` — 找到畫面上的文字就點下去
- `/locate text find <text> [region] [lang]` — 找文字並回座標與信心值
- `/locate text wait <text> [seconds] [lang]` — 等某段文字出現在畫面上

**`/locate ui`** — 用元素名稱定位（首選：問作業系統要精確矩形）

- `/locate ui click <name> [type] [window] [shot]` — 找到元素就點下去
- `/locate ui find <name> [type] [window]` — 找元素並回精確矩形
- `/locate ui gone <name> [seconds] [type] [window]` — 等元素消失
- `/locate ui read <name> [type] [window]` — 讀元素現在的值
- `/locate ui tree [window]` — 列出可點的元素
- `/locate ui wait <name> [seconds] [type] [window]` — 等元素出現

### `/macro` — 巨集

巨集（限擁有者）：存 / 錄 / 跑 / 編輯

- `/macro delete <name>` — 刪除巨集
- `/macro edit <name> <line> <step>` — 改寫巨集的第 N 行
- `/macro insert <name> <line> <step>` — 在第 N 行前插入一步
- `/macro list` — 列出所有巨集
- `/macro record <action> [name]` — 錄製操作成巨集（限擁有者）
- `/macro rm_line <name> <line>` — 刪掉巨集的第 N 行
- `/macro run <name> [args] [shot]` — 執行巨集
- `/macro save <name> [steps] [file]` — 存一份巨集
- `/macro show <name>` — 顯示巨集內容
- `/macro stop` — 中止正在跑的巨集

### `/host` — 主機指令與檔案進出

主機（限擁有者）：執行指令 / 背景作業 / 檔案進出 / 全停

- `/host get <path>` — 把主機上的檔案取回（限擁有者）
- `/host panic` — 一次全停（限擁有者）
- `/host put <file> <path> [force]` — 把附件寫到主機上（限擁有者）

**`/host job`** — 背景作業（限擁有者）

- `/host job clear` — 清掉已結束的作業紀錄
- `/host job eof <id>` — 關掉某個作業的輸入端
- `/host job list` — 列出背景作業
- `/host job log <id> [lines]` — 看某個作業的輸出
- `/host job run <command> [keep_stdin]` — 丟到背景跑，不等它結束
- `/host job send <id> <text>` — 送一行文字給互動式作業
- `/host job stop <id>` — 中止某個作業

**`/host sh`** — 在主機上執行一行指令（限擁有者）

- `/host sh cd [path]` — 切換工作目錄（記住到重啟）
- `/host sh run <command> [timeout]` — 執行一行指令
- `/host sh stop` — 中止所有還在跑的指令

### `/watch` — 條件監看

條件成立時主動通知，或直接接手做事（限擁有者）

- `/watch clip [text] [run] [then]` — 等剪貼簿變動（給文字就等它含那段）
- `/watch job <id> [run] [then]` — 等某個背景作業結束
- `/watch list` — 列出進行中的監看
- `/watch pixel <x> <y> <color> [run] [then]` — 等某一點變成指定顏色
- `/watch port <target> [run] [then]` — 等某個埠接得上
- `/watch process <name> [run] [then]` — 等某個程式開始執行
- `/watch stop <id>` — 停掉某個監看
- `/watch text <text> [run] [then]` — 等某段文字出現在畫面上
- `/watch ui <name> [run] [then]` — 等某個元素出現
- `/watch window <substr> [run] [then]` — 等某個視窗出現

### `/schedule` — 定時排程

定時排程（限擁有者），落盤後撐得過重啟

- `/schedule add <when> <kind> <what>` — 新增一筆排程
- `/schedule list` — 列出所有排程
- `/schedule remove <id>` — 刪掉某筆排程
- `/schedule run <id>` — 立刻跑一次某筆排程

### `/launcher` — 獨立監督者

獨立批次監督者（限擁有者），跟 bot 分開執行、bot 重啟也不受影響

- `/launcher start` — 啟動獨立監督者
- `/launcher status` — 看獨立監督者在不在跑
- `/launcher stop` — 停止獨立監督者

---

## 🌐 跨頻道指令

### 直接指令

- `/booru [tags] [latest] [grid]` — 圖庫搜圖：tag 隨機一張（可模糊；不帶 tag 用預設圖）
- `/e621 <tags>` — furry 取向圖庫隨機一張（預設 NSFW；加 rating:safe 限 SFW）
- `/grid <tags>` — 圖庫最新 4 張拼 2x2 上傳（等同 /booru 同時開 latest 與 grid）
- `/help [lang]` — 指令說明（tw / cn / en）
- `/iqdb <url>` — 跨圖庫反向圖搜（回前幾名來源＋相似度 %）
- `/nsfw <tags>` — 圖庫搜圖 NSFW 捷徑（自動補 rating:explicit）
- `/safebooru <tags>` — 全站 SFW 圖庫隨機一張

### `@bot <文字>` — 自由提問

不接子指令的 mention 是自由提問入口。它**刻意**保留 mention 形式而不是改成
斜線指令：一則訊息帶得動多行內容、附件與回覆脈絡，斜線的選項輸入框帶不動；
而且互動 token 只有 15 分鐘，一個回合的上限卻可以到 10800 秒。

空 mention（`@bot` 後面什麼都不接）會回一張預設圖庫圖片。

提問文字的**最前面**可以加兩個微調 token。它們不是斜線指令，送出前會被剝掉，
剩下的才是真正送出去的問題：

- `@bot /model <opus|sonnet|haiku|fable|default> <提問>` — 指定這個工作階段用哪個模型
- `@bot /effort <low|medium|high|xhigh|max> <提問>` — 指定思考力度

兩個都是**工作階段層級**：設過之後同一個工作階段每一輪都沿用，`default` 清除
覆寫；要釘住版本就照同樣寫法指定，例如 `<opus-5|sonnet-4.6>`。順序不拘、大小寫不拘，
兩個可以同時給，但必須連續放在最前面——句中出現的同名字樣會原樣保留。只打
token 不接問題就只是改設定，不會送出一輪。

同一串寫法貼進 `/dorossi ask` 的 `prompt` 欄位開頭也一樣有效；「改設定」與
「提問」在同一次送出裡完成是 token 形式唯一做得到的事。只想改設定不提問的
話，用 `/dorossi model`、`/dorossi effort` 這兩個斜線指令。

### `/dorossi` — 對話後端

對話後端（限擁有者）：提問 / 工作階段 / 佇列 / 狀態

- `/dorossi abort [target]` — 中止進行中的工作；也能取消「斷線停下來、等著自動接續」的任務
- `/dorossi ai [provider]` — 顯示或切換這個工作階段的後端
- `/dorossi ask <prompt> [session]` — 提問（可能跑很久）；`session` 只影響這一次，不會切換目前的工作階段，可用來同時對多個工作階段發問；欄位會列出你的工作階段（代號與標籤）供挑選，和提問開頭指定的工作階段不一致時整輪不送出
- `/dorossi compact [session]` — 壓縮對話脈絡（可能跑數分鐘）
- `/dorossi effort [effort]` — 顯示或設定這個工作階段的思考力度
- `/dorossi errors` — 最近的警告與錯誤事件
- `/dorossi fullmode` — 顯示目前的工具模式
- `/dorossi health` — 健康檢查
- `/dorossi logs [n]` — 最近的執行期事件
- `/dorossi model [model]` — 顯示或設定這個工作階段的模型
- `/dorossi retry [session]` — 重試上一個提問（可能跑很久）
- `/dorossi running` — 正在執行的工作與同時執行上限（誰在跑、跑多久、在等空位、在等網路恢復還是在跑）
- `/dorossi status` — 執行期狀態
- `/dorossi tokens` — 查詢帳號用量
- `/dorossi workspace_clean [dry] [days]` — 清理舊的工作目錄

**`/dorossi allowdir`** — 工作階段可額外存取的目錄

- `/dorossi allowdir add <path> [session]` — 新增一個可存取的目錄
- `/dorossi allowdir list [session]` — 列出額外可存取的目錄
- `/dorossi allowdir remove [session]` — 移除額外可存取的目錄

**`/dorossi queue`** — 等待中的提問佇列

- `/dorossi queue clear [session]` — 清空等待佇列
- `/dorossi queue detail` — 列出等待中的提問（含內容）
- `/dorossi queue failed_clear` — 清掉失敗佇列
- `/dorossi queue move <source> <target> [session]` — 調整等待中提問的順序
- `/dorossi queue remove [index] [session] [id]` — 取消第 N 筆（或指定 id）等待中的提問
- `/dorossi queue retry_failed` — 重試失敗的提問
- `/dorossi queue show` — 列出等待中的提問
- `/dorossi queue undo` — 復原上一次佇列操作

**`/dorossi session`** — 多工作階段管理

- `/dorossi session archive <id>` — 封存工作階段
- `/dorossi session continue [id]` — 接續未完成的自走任務（沒有回合上限）；`id` 填 all 一次接回所有中斷的任務（你中止過的也會接回；略過正在跑的、已封存的與超過同時上限的，逐一回報）
- `/dorossi session delete <id>` — 刪除工作階段
- `/dorossi session export <id>` — 把工作階段匯出成檔案
- `/dorossi session list` — 列出所有工作階段
- `/dorossi session new [label] [workdir]` — 開一個新的工作階段並切換過去（可指定標籤與工作目錄）
- `/dorossi session rename <id> <label>` — 改工作階段的標籤
- `/dorossi session reset <id>` — 清空工作階段的對話脈絡
- `/dorossi session switch <id>` — 切換到指定工作階段

### `/tag` — 圖庫 tag 工具

圖庫 tag 工具：post 數 / wiki / 建議 / 補全

- `/tag autocomplete <prefix>` — tag 前綴補全（top 10 by post count）
- `/tag count <tag>` — 查 tag 的 post 數
- `/tag suggest <character>` — 聚合角色 30 張隨機 post 的常見搭配 tag（top 20）
- `/tag wiki <tag>` — tag 的 wiki 內文

### `/fun` — 趣味 / 隨機

趣味 / 隨機小工具

- `/fun 8ball <question>` — 魔術 8 球
- `/fun ascii <text>` — 大字 ASCII art
- `/fun calc <expression>` — 安全計算機（不執行任意程式碼）
- `/fun choose <options>` — 隨機選一個（| 分隔選項）
- `/fun coinflip` — 擲硬幣
- `/fun rand <minimum> <maximum>` — 範圍內隨機整數
- `/fun reverse <text>` — 字串反轉
- `/fun roll [dice]` — 擲骰子（NdM，預設 1d6）
- `/fun rps <move>` — 剪刀石頭布
- `/fun timer <duration> [message]` — 倒數計時，結束時 @ 提醒你

### `/tool` — 編碼與小工具

編碼 / 雜項工具

- `/tool base64 <text>` — Base64 編碼
- `/tool color <value>` — 色票圖（3 或 6 個 hex digits）
- `/tool hash <text>` — md5 / sha1 / sha256
- `/tool qr <text>` — 產生 QR code 圖
- `/tool say <text>` — 讓 bot 代為發言（限 server owner）
- `/tool unbase64 <text>` — Base64 解碼
- `/tool urldecode <text>` — URL 解碼
- `/tool urlencode <text>` — URL 編碼

### `/info` — 資訊與元資料

頭貼 / server / channel 資訊

- `/info avatar [user]` — 顯示頭貼（預設自己）
- `/info channel` — channel 元資料
- `/info ping` — 回應延遲
- `/info server` — server 元資料
- `/info uptime` — bot 已執行多久
- `/info version` — 版本（short SHA ＋ branch）

### `/web` — 公開資料查詢

百科 / 字典 / 漫畫 / 隨機內容 / 公開資料查詢

- `/web anime <name>` — 動畫資料庫查詢
- `/web cat` — 隨機貓圖
- `/web crypto <symbol>` — 加密貨幣價格（USD / TWD）
- `/web dict <word>` — 英文字典
- `/web dog` — 隨機狗圖
- `/web fact` — 隨機冷知識
- `/web github <repo>` — 程式碼平台 repo 概要
- `/web joke` — 隨機 dad joke
- `/web quote` — 隨機名言
- `/web wiki <topic>` — 百科摘要
- `/web xkcd [n]` — 網路漫畫（隨機 / 指定編號）
