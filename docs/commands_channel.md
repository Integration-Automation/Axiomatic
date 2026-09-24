# 🔒 限頻道指令

這些指令**只能在 `bot_config.json` 設定的頻道（`channel_id`）使用**；擁有者
可以跨頻道使用。全部都是原生斜線指令——打 `/` 就會自動補全。

```{note}
即使在限定頻道內，bot 的回覆字串也**一律泛用**：不提產圖服務名、不露出佇列
檔名或本機路徑（狀態回覆用通用標籤對應各佇列），原始錯誤只進 log。這是專案的
硬性保密規則，寫新指令時要沿用。
```

## 直接指令

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

- `/out debug_show [name]` — 列出或上傳除錯截圖
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
