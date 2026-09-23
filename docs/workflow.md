# 跑批工作流程

這一章說明 webrunner 怎麼把待辦佇列變成一張張圖——配對規則、fallback、
`end` 終止標記、排程節奏。`/gen plan` / `/gen preview` / `/queue` / `/eta` 都是依這套
規則計算的。

## 四個待辦佇列

| 檔案 | 用途 | 為空時的 fallback |
|---|---|---|
| `todo_prompt.md` | 主 prompt 佇列 | `prompt.md` |
| `todo_character1.md` | 角色 1 prompt 佇列 | `character1.md` |
| `todo_character2.md` | 角色 2 prompt 佇列 | `character2.md` |
| `todo_undesired.md` | 負向 prompt 佇列 | `undesired.md` |

每個檔案**一行一筆 entry**，只用換行切分；entry 內可含任意字元（含 `,` /
`，` / `::` / 括號）。空白行會被略過，NBSP（`\xa0`）會被正規化成一般空白。

## 配對規則

四個佇列會被「壓成」一筆筆要跑的 quadruple。較短的佇列用**最後一筆重複填充**，
空佇列則用 fallback 檔。

| todo_prompt | todo1 | todo2 | 實際會跑 |
|---|---|---|---|
| `[P]` | `[a, b, c]` | `[x, y, z]` | `(P,a,x) (P,b,y) (P,c,z)` |
| `[P, Q]` | `[a, b, c]` | `[x]` | `(P,a,x) (Q,b,x) (Q,c,x)` |
| _(空)_ + `prompt.md` | `[a, b, c]` | `[x]` | `(fallback,a,x) …` |
| `[P, Q]` | _(空)_ + `character1.md` | `[x]` | `(P,fallback,x) (Q,fallback,x)` |

每個 quadruple 就是一次「角色批次」，預設產 **240 張**（`images_per_character`）。

```{tip}
跑之前用 `/gen plan` 看完整配對計畫、`/gen preview` 看下一筆——兩者都套用上述
fallback 規則與下方的 `end` 截斷，所見即所跑。
```

## `end` 終止標記

`todo_prompt.md` 裡若有一行是 `end`（不分大小寫、前後空白會忽略），webrunner
跑到那一筆時就**停止後續產圖**：該筆與其後所有 quadruple 都不產，乾淨結束
（回傳碼 0，不會觸發監督器重生）。

- 只看**主 prompt 佇列**；`todo_character1/2`、`todo_undesired` 不參與判斷。
- 命中時**只消耗 `end` 這一行**（從 `todo_prompt.md` 移除），保留它後面的
  entry；下次 `/run` 會接著跑 `end` 之後沒做完的部分。

相關指令：

- `/todo prompt end` — 在佇列末尾追加一個 `end` 標記。
- `/todo prompt unend` — 移除所有 `end` 標記，讓佇列恢復跑完整批。
- `/queue` / `/eta` / `/gen plan` / `/gen preview` 都會在 `end` 處截斷並標示停止點。

```{note}
webrunner 是**動態消耗**佇列的：每個角色批次開始前都會重新讀取四個待辦檔、
重算配對。所以跑批途中放進去的 `end`（`/todo prompt end`）會在**下一個角色邊界**生效、
乾淨收尾——不必等整批跑完。要「立刻」停止（連當前角色一起放棄）才需要 `/stop`。
```

## 動態佇列消耗

webrunner **不是**開跑時把配對讀死一次，而是每個角色批次開始前**重新讀取
四個待辦檔**、重算配對再決定下一步。因此：

- 跑批途中用 `/todo prompt add` / `/todo char1 add` 等**加進**還沒跑到的 entry，這一輪就會被跑到。
- 途中直接**編輯檔案**（換順序、刪 entry）也安全：webrunner 回寫前會先和
  磁碟內容比對（reconcile），不一致時以磁碟為準、原內容備份到 `.backup/`
  （`/sys undo` 可回復），不會蓋掉你的編輯。

一個角色批次完成後，實際存下的張數需達 `images_per_character ×
min_save_ratio`（預設 240 × 0.9）才算「做完」、從對應的 todo 檔 pop 掉該筆
並回寫；低於門檻的 entry 會**留在佇列**，下次重跑。pop 有兩個保護：

- **padding 感知**：較短佇列被重複填充的「尾筆」在後面的配對還要用，不會
  提前 pop 掉——中途 `/stop` 後留在檔案裡的內容剛好重現「還沒做完」的配對。
- **中斷續跑**：進行中的角色會把進度存到 `webrunner_progress.json`，中斷後
  重啟會從既有輸出接續，不會從第 1 張重產。

fallback 檔（`prompt.md` / `character1.md` / `character2.md` /
`undesired.md`）是持久預設，
**永遠不會被 pop**；所有真實佇列都空、只剩 fallback 時，只會產**一個**角色
批次就收尾（不會被 fallback 無限復活）。

## 排程節奏（工作 / 休息）

webrunner 累計工作時間達 `schedule_limit_hours`（預設 16h）後，會睡
`rest_hours`（預設 6h）再繼續，計時歸零。這是為了模擬人類節奏、避免被限流。

## 記憶體洩漏緩解：定期重啟 Chrome

出圖服務的網頁介面是單頁式應用，跨數百張產圖會持續吃記憶體直到 Chrome 出現
「Aw, Snap! — Out of Memory」。因此 webrunner 會在**角色邊界**（不是產圖中途）
每完成 `restart_chrome_every_n_characters` 個角色就重啟 Chrome 沖掉記憶體
（預設 1 = 每個角色後都重啟，`0` = 關閉）。重啟會重跑完整 setup（用保存的
cookie 登入 → 選模型 / 角色 / 解析度 / 取樣器），約 30–60 秒。

## 產圖重試與失敗處理

- 單張產圖若 src 沒變（產圖未完成）會重試最多 `generate_max_retries` 次
  （預設 4），每次間隔 `generate_retry_delay_sec`（預設 25–30 秒）。這組退避
  **與正常的圖間延遲 `inter_image_delay_sec` 分開**——一個是節奏，一個是失敗後。
- 連續失敗達 5 次（`CONSECUTIVE_FAIL_ALERT`）會發 `consecutive_failures`
  事件，bot 貼警告，但**不**中止。
- 連續失敗達 `consecutive_fail_abort`（預設 10）會**拋例外**讓 `main()`
  非零退出，監督器重生 Chrome。這個值必須 > 5（讓警告先出現）。
- 偵測到 Chrome renderer 崩潰（「Aw, Snap!」）會短路直接讓監督器重生，不空轉。
