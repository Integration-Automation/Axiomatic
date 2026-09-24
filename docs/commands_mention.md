# 🌐 跨頻道指令

這些指令可在 bot 所在的**任何頻道**使用。

```{important}
跨頻道回覆**一律保持泛用**：不提產圖服務、不洩漏這是產圖自動化管線、不帶出
佇列檔名／本機路徑／原始錯誤字串。高破壞力的桌面／行程操作只放在限頻道介面。
```

## 直接指令

- `/booru [tags] [latest] [grid]` — 圖庫搜圖：tag 隨機一張（可模糊；不帶 tag 用預設圖）
- `/e621 <tags>` — furry 取向圖庫隨機一張（預設 NSFW；加 rating:safe 限 SFW）
- `/grid <tags>` — 圖庫最新 4 張拼 2x2 上傳（等同 /booru 同時開 latest 與 grid）
- `/help [lang]` — 指令說明（tw / cn / en）
- `/iqdb <url>` — 跨圖庫反向圖搜（回前幾名來源＋相似度 %）
- `/nsfw <tags>` — 圖庫搜圖 NSFW 捷徑（自動補 rating:explicit）
- `/safebooru <tags>` — 全站 SFW 圖庫隨機一張

## `@bot <文字>` — 自由提問

不接子指令的 mention 是自由提問入口。它**刻意**保留 mention 形式：一則訊息帶
得動多行內容、附件與回覆脈絡，斜線的選項輸入框帶不動；而且互動 token 只有 15
分鐘，一個回合的上限卻可以到 10800 秒，用斜線撐長回合會在中途斷掉。

空 mention 會回一張預設圖庫圖片。

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
- `/dorossi ask <prompt> [session]` — 提問（可能跑很久）；`session` 只影響這一次，不會切換目前的工作階段；欄位會列出你的工作階段供挑選，和提問開頭指定的工作階段不一致時整輪不送出
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
- `/dorossi queue remove <index> [session]` — 取消第 N 筆等待中的提問
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
