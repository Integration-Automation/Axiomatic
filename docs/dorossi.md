# Dorossi 問答與自走任務

Dorossi 是 bot 裡的問答助理：把問題交給對話後端、把答案即時串流回來。它可以多輪續談、同時開好幾個工作階段，
擁有者授權時還能在主機上動手做事（讀寫檔案、跑指令），甚至自己一輪一輪推進一個長任務直到做完。

```{note}
大部分 `/dorossi` 子指令**限擁有者**（閘門在每個指令內部）。唯一公開的是 `/dorossi model_list`，而它對非擁有者
只列模型別名。完整的指令參數見 `commands/dorossi.md`，設定鍵見 {doc}`config` 的「Dorossi」一節。
```

## 先準備後端

Dorossi 本身不帶任何後端，整組功能是選用的：沒有可用的後端時 `/dorossi` 只是用不了，其他功能不受影響。

- `bot_config.json` 的 `dorossi_backend` 決定預設那一家走哪條路：`claude_code`（叫用本機的對話後端 CLI，用量走那台
  機器上登入帳號的方案）或 `api`（直接連線，憑證由 SDK 從環境變數讀，不放在 repo 裡）。要裝什麼見
  {doc}`setup` 的相依表。
- 另外兩個後端可以逐個工作階段切換（`/dorossi ai`），一樣要先在主機上裝好並登入它們各自的 CLI。其中第三個後端
  做不到純聊天，所以只在 `dorossi_cc_tools` 是 `full` 時才會執行。
- 擁有者是 `owner_user_id`（其他平台是 `platforms.<名稱>.owner_user_ids`）。沒設定時沒有人過得了這道閘。

## 提問

- `/dorossi ask <提問> [session]`：問一題。指定 `session` 就丟到那個工作階段在背景跑，不必先切換。
- 標記 bot **不是**提問：不管後面接不接文字，都只會回一張隨機的預設圖庫圖片。沒有斜線選單的平台
  另有自己的入口，見 {doc}`platforms`。提問最前面可以加模型與思考力度的微調 token（見 {doc}`commands_mention`）。
- 同一個工作階段一次只跑一輪；後面的提問會排隊，`/dorossi queue show` 看得到。
- 答案很長時會分成好幾則；跑的過程中會一直編輯同一則訊息顯示進度。不能編輯訊息的平台改成定時送出新的一則。
- 跑到一半要它停：`/dorossi abort`（`target` 留空 = 目前這個工作階段）。後端當場被砍掉，那則訊息改成
  「已中止」，不會重試；工作階段的脈絡留著，排在後面的提問照常輪到。那一輪的後端已經起的程式（跑到一半的
  指令、測試、它放在背景的程式）也一起被砍掉；已經改掉的檔案不會還原，被打斷的動作可能留下寫到一半的
  檔案或鎖檔。

## 工作階段

每個工作階段有自己的對話脈絡、後端、模型與工作目錄。

| 指令 | 做什麼 |
|---|---|
| `/dorossi session list` | 列出你的工作階段（代號、標籤、最後使用、後端與模型） |
| `/dorossi session new [label] [workdir]` | 開新的工作階段，可以指定工作目錄 |
| `/dorossi session switch <id>` | 切換目前的工作階段 |
| `/dorossi session rename` / `archive` / `delete` / `reset` / `export` | 改名、封存、刪除、清空脈絡、匯出 |
| `/dorossi session continue [id]` | 接回一個被打斷或暫停的自走任務 |
| `/dorossi allowdir add` / `list` / `remove` | 額外授權一個目錄給這個工作階段 |

工作目錄的完整主機路徑只在 1:1 私訊與 `path_reveal_channel_ids` 列出的頻道顯示，其他地方只顯示末段目錄名。

## 後端、模型與思考力度

- `/dorossi ai [provider]`：看或切換這個工作階段的後端。提問進行中也能切：目前這一輪做完，下一輪由新的後端接手；
  自走任務會帶著原任務與工作目錄的進度繼續。
- `/dorossi model [model]`：看或設定模型（只對這個工作階段）。擁有者會看到完整的版本號；選「預設」清除設定。
- `/dorossi model_list`：列出每個後端支援的模型、目前生效的是哪一個，以及模型目錄最後檢查的時間。
- `/dorossi effort [effort]`：思考力度。
- 每天會自動檢查一次後端有沒有新模型（`dorossi_model_check`），發現時公告（只講別名），模型選單也會自己跟上。

## 工具模式

`bot_config.json` 的 `dorossi_cc_tools`（`/dorossi fullmode` 顯示目前是哪一種）：

- `off`（預設）：純聊天，停用所有工具，Dorossi 只能對話。
- `full`：完整代理人。Dorossi 可以在主機上**無確認**執行指令與讀寫檔案，只有擁有者的提問會這樣跑。

```{warning}
`full` 模式等於把這台電腦的操作權交給後端。只在你清楚自己在交代什麼時打開；工作目錄與 `allowdir` 決定它預設在哪裡做事，
但那不是沙箱。
```

## 自走任務

明確說「持續推進直到做完」之類的話，Dorossi 會進入自走模式：一輪做完自己接下一輪，連續幾輪沒有進展才停。也可以讓
後端自己判斷「這是需要多輪的大任務」而轉進自走（`dorossi_self_judge_enabled`；程式預設開，範本填的是關，比較省 token）。

| 指令 | 做什麼 |
|---|---|
| `/dorossi running` | 正在跑的任務、每一輪在跑還是在等，以及存檔等用量重設、何時會自己接回來的任務 |
| `/dorossi abort [target]` | 中止（`all` 中止全部）；正在進行的單輪提問也中止得了 |
| `/dorossi yield [target]` | 請它提交手上的改動後讓出編輯權、暫停；之後用 `/dorossi session continue` 接回來 |
| `/dorossi session continue [id]` | 接回暫停或被打斷的任務 |

自走期間對同一個工作階段再問的話會被當成**中途補充**，下一輪帶進去。同時進行的自走任務數由
`dorossi_max_parallel_loops` 管，跟單輪提問的 `dorossi_cc_max_parallel` 分開算。

### 撞到方案用量上限

任務**不會掛著等**：它把工作階段、被打斷的那一輪與還沒帶入的補充存檔，停下來並放掉佔用的名額，重設的時間到了自動接回來、
從被打斷的那一輪接著做（等的期間 bot 重啟也會接；找不回原本那則訊息時會通知你手動接）。想提早接就用
`/dorossi session continue <id>`。單輪提問撞到上限時會停進佇列，時間到自動重跑。等多久、最多等幾次由
`dorossi_usage_wait_*` 那幾個鍵決定。

### bot 重啟或斷線

被打斷的自走任務會在 bot 起來、或連線恢復時自動接回來（有年齡與次數上限——`dorossi_loop_autoresume_max_age_sec`、
`dorossi_loop_autoresume_max_tries`——防止「一接就讓 bot 當掉」的無限重啟）。對話平台斷線時擁有者的答案會先留著，
連線回來再補送。

## 用量與維運

| 指令 | 做什麼 |
|---|---|
| `/dorossi tokens` | 本機記錄的用量（今日／近 7 日／累計的 token 與估算金額）與後端帳號額度 |
| `/dorossi status` / `health` | 狀態與健康檢查 |
| `/dorossi logs [n]` / `errors` | 最近的紀錄與錯誤 |
| `/dorossi compact [session]` | 手動壓縮一個工作階段的脈絡（脈絡過大時也會自動壓縮，門檻是 `dorossi_compact_context_tokens`） |
| `/dorossi retry [session]` | 重跑上一題 |
| `/dorossi queue …` | 看、移動、移除、清空排隊中的提問；還原失敗的可以重試 |
| `/dorossi workspace_clean` | 清掉沒在用的舊工作目錄（可以先 `dry`） |

金額是依公開費率的估算，訂閱方案的實際帳單以方案為準；有些後端拿不到實際費用，會另外標示幾輪費用未知。
