# 設定檔

可調行為都放在 repo 根目錄的 JSON，不寫死在 Python 常數裡。所有載入器共用
同一個形狀：缺檔 / 解析錯 / 型別錯都回退到內建預設、**永不拋例外**——一個會讓
bot 因打錯字而崩潰的設定檔就是 regression。

```{important}
除了 `batch_config.json` 以外，**這些檔案都不在版本庫裡**（它們裝的是你的頻道
ID、帳號 ID、主機路徑與本機應用程式清單）。repo 帶的是 `*.example.json` 範本，
第一步是把範本複製成同名的正式檔再填——見 {doc}`setup`。憑證檔 `auth.md` 與
`discord_bot_token.md` 同理，範本是 `*.example.md`。

`bot_config.json` 缺席或 `channel_id`／`owner_user_id` 還是 0 時，bot 會在啟動時
印一段說明並乾淨結束（回傳碼 5），不是 traceback；監督式啟動器認得這個碼並直接
收工，不會無限重生。
```

| 檔案 | 重讀時機 | 需要重啟？ |
|---|---|---|
| `batch_config.json` | webrunner 每個角色迴圈開頭重讀；bot 在 `/gen progress` / `/eta` / `/config show` 讀 | 否——下個角色生效（**例外**：`debug_screenshots` 只在 webrunner import 時讀一次，要 `/stop` + `/run`） |
| `bot_config.json` | bot 啟動時讀一次 | **是（`/sys restart`）**；缺席時 bot 拒絕啟動 |
| `presence_games.json` | presence 探針每次 tick（約 8 秒）重讀 | 否 |
| `presence_music.json` | 同上 | 否（壞 regex 印 stderr 並略過，不會崩） |
| `presence_rpc.json` | 同上 | 否（`enabled: false` 或缺 `client_id` → 整段停用） |

## batch_config.json

產圖批次參數。可用 `/config show` 檢視、`/config set` / `/config reset` 改
（見 {doc}`commands_channel`）。

| 鍵 | 預設 | 說明 |
|---|---|---|
| `images_per_character` | 240 | 每個角色批次產幾張。**版本庫帶的 `batch_config.json` 刻意覆寫成 120**：`test_selenium_facade.test_the_driver_log_ceiling_clears_a_whole_character_session` 會拿這個值推算「一個工作階段會寫多少 chromedriver.log」，240 張的估算量剛好越過 64 MB 的修剪上限，那條警告就失去鑑別力。要調高就連同那個上限一起看 |
| `inter_image_delay_sec` | `[20, 30]` | 圖與圖之間隨機延遲秒數範圍 |
| `schedule_limit_hours` | 16 | 工作時數門檻。**只在角色與角色之間檢查**，所以實際會超過（見下方說明） |
| `rest_hours` | 6 | 休息時數，之後計時歸零。`0` = 不休息、只把計時器歸零 |
| `generate_max_retries` | 4 | 單張產圖 src 沒變時的重試次數 |
| `generate_retry_delay_sec` | `[25, 30]` | 產圖失敗後的退避範圍（**與 `inter_image_delay_sec` 分開**：那個是正常節奏，這個是失敗後） |
| `download_max_retries` | 3 | 下載圖片的重試次數 |
| `consecutive_fail_abort` | 10 | 連續失敗幾次就拋例外讓監督器重生。**必須 > 5**（連續失敗警告門檻），讓警告先出現 |
| `restart_chrome_every_n_characters` | 1 | 每完成幾個角色就重啟 Chrome 沖記憶體（`0` = 關閉） |
| `min_save_ratio` | 0.9 | 角色批次「算做完、可 pop 佇列」的實存張數門檻比例（`ceil(target × ratio)`、下限 1）。容忍零星暫時性失敗（例如 239/240 仍算完成）；`1.0` 恢復精確達標才 pop。每角色熱重讀 |
| `debug_screenshots` | `false` | 開啟後 `snap()` 才會寫 `debug_*.png`。webrunner import 時讀一次，改了要 `/stop` + `/run` |

```{note}
`schedule_limit_hours` **不是硬性上限，也不會在到點的那一刻停下來。** 它只在
**角色與角色之間**檢查一次，跑到一半的角色一定會跑完；而計時用的是**牆上時間**，
所以額度用完在等的那段也照算。

實測過一輪（設定 16 / 6）：跑到 **24.51 小時**才進休息，比設定值多了 53%。那
24.51 小時裡有 **14.00 小時是在等額度回補**，真正在產圖的只有 10.51 小時。

所以真正的工作視窗長度是「`schedule_limit_hours` ～ `schedule_limit_hours` ＋
一個角色的時間」。要把它收窄，調小 `images_per_character` 比調小
`schedule_limit_hours`有效。
```
| `quota_wait_poll_sec` | 3600 | 額度用完時，關掉購買視窗後每隔幾秒重試一次（預設 1 小時）。等待期間**不算失敗**、也不會結束背景程式，所以監督器不會介入、不會重跑登入。**⚠️ 這不是吞吐量旋鈕，調小不會讓批次跑更快。** 實測跨兩個設定值、214 次被擋：約 14 分鐘輪詢 **7.87 張/小時**、約 60 分鐘輪詢 **7.84 張/小時**——差 0.4%。上限在帳號那一側，我們這一側的輪詢間隔影響不到它；調小只是把同樣的圖切成更多更小的爆發，而每次醒來都要多付一次「關購買視窗 → 關不掉 → 整頁重新整理 → 重填欄位」 |
| `model_candidates` | `["NAI Diffusion V5 Full", "NAI Diffusion V5", "Diffusion V5 Full"]` | 產圖模型的候選字面，**依序**嘗試、第一個在下拉選單裡命中的就採用。站方改寫選項字面時不必動碼，補一個候選即可；全部落空時 log 會 dump 當下看得到的選項，那行就是該填什麼的答案。**只能編輯檔案**——不在 `/config show` / `/config set` 的清單裡，因為值是外部服務的模型名，印進頻道會踩到對外揭露規則。每次 setup 重讀（`restart_chrome_every_n_characters: 1` 時等於每個角色一次）。⚠️ 換模型會改變畫質，也可能改變吞吐量：站方的方案政策對不同模型可能套不同的使用量上限 |
| `quota_wait_max_sec` | 0 | 等待總秒數上限，**0 = 一直等到額度回復**（預設）。設正值時超過就停止並通知——那代表不是會自己回補的額度，而是方案／帳號問題 |

```{tip}
`/config set` 寫入時只更新你指定的那一個鍵、保留其他既有覆寫，並把檔案維持
精簡（不會把所有預設值都寫進去）。值會在寫入前驗證型別 / 範圍，不合法直接拒絕。
```

## bot_config.json

bot runtime 設定，**啟動時讀一次**，改了要 `/sys restart` 才生效（這些值在事件
迴圈啟動前就 wire 進 dispatcher / presence 任務 / 頻道過濾器）。常見鍵：

- `channel_id` — 限頻道指令唯一生效的頻道（擁有者可跨頻道）
- `owner_user_id` — **你自己的 Discord 使用者 ID**。操作主機的指令群與
  `/dorossi` 只認這一個 ID。`0`（預設）＝沒設定，那些指令對所有人一律拒絕
  （fail-closed）
- `path_reveal_channel_ids` — 允許顯示**完整主機路徑**的頻道 ID 清單。
  Dorossi 的工作目錄顯示在這些頻道與 1:1 私訊給完整路徑，
  其餘表面只給末段目錄名。空清單（預設）＝私訊限定。這是**表面**閘不是身分閘
  ——列進去的頻道，同頻道的其他人也看得到那些路徑
- `user_roles` — `admin_user_ids` / `operator_user_ids` / `viewer_user_ids`。
  斜線與文字兩條路徑走**同一道閘**（頻道 → 主機控制 → 角色 → 計數 → 稽核），
  且**預設 fail-closed**：沒有明確標成公開的指令一律受閘。三份清單都空
  = 角色閘停用、只剩頻道閘。
  ⚠️ **所以不要把「這個指令很危險」的保護只寫進角色表**——三份清單都空是預設
  值，那時角色表等於沒有作用。操作主機的指令另外硬綁擁有者（見下）
- `alert_user_id` — 告警要 ping 誰（`0` = 不 ping）
- `min_free_disk_gb` — 磁碟餘量門檻（`0` = 停用）
- `keep_bot_awake` — 批次監督期間（含斷網後等網路回來的那段）與 Dorossi 回合／自走迴圈進行中，bot 要不要持有電源要求
  （預設 `true`）。保的是 **bot 這個行程**在主機待命時不被暫停，不是讓主機不睡，也不涵蓋
  bot 啟動的子行程；用電池時系統會在睡眠逾時後 5 分鐘收回。`false` = bot 什麼都不持有
- `target_presence_username` — presence 鏡像的目標使用者
- `default_help_lang` — `/help` 預設語言（`zh-tw` / `zh-cn` / `en`）
- probe / event poll 間隔
- `daily_health_report` — `enabled` / `channel_id` / `time`
- `dorossi_model_check` — 每日一次的後端模型目錄檢查（掛在健康迴圈上，但有自己
  的開關，關掉健康報告不會連它一起關掉）
  - `enabled`（預設 `true`）— 關掉的話模型清單就固定在程式內建的那幾筆
  - `interval_hours`（預設 `24`，必須大於 `0`）— 隔多久檢查一次。上次檢查的時刻
    存在執行期的模型目錄檔裡，所以重啟 bot 不會重跑
  - `announce_channel_id`（預設 `0`）— 發現新模型時公告到哪個頻道；`0` = 沿用
    `daily_health_report.channel_id`（它自己 `0` 就是 `channel_id`）
  - 檢查的做法是拿「族別名」叫一次後端、讀回它解析成哪一個完整模型，讀到就把子
    行程收掉——所以**一次檢查不消耗任何額度**，約十秒。結果寫進 repo 根目錄的執行
    期檔案，載入時併回內建的模型表，`/dorossi model` 因此會自己跟上新模型。公告
    只講別名
- `dashboard` — `host` / `port`（唯讀狀態儀表板）
- `webrunner_supervisor` — 監督器退避參數（`respawn_backoff_min_sec`、
  `respawn_backoff_max_sec`、`healthy_threshold_sec`、`rapid_fail_threshold_sec`、
  `rapid_fail_giveup_count`、`zero_progress_giveup_count`），bot 的 `/run`
  watchdog 與 `start_webrunner.py` 共用同一組參數。
  `zero_progress_giveup_count`（預設 2）是 `rapid_fail_*` 的慢速版：連續幾輪
  「有嘗試產圖卻一張都沒存」就停止重啟。快速失敗（開不起來）由前者擋，慢速
  失敗（每輪跑很久卻毫無產出）由後者擋
- `dorossi_*` — Dorossi 問答功能的整組設定（後端選擇、工具模式、看門狗
  上限、後端回合與自走任務的並行上限等），完整清單與各鍵語意見 README /
  COMMANDS.md
- `gui_control` — `launch_whitelist`（exe 名稱 / 路徑）與 `launch_aliases`
  （自訂名稱 → 目標），`/proc launch` 用。兩者皆空時 `/proc launch` 停用（防 token 外洩）

## 主機控制：硬綁擁有者

操作 bot 那台電腦的指令**不經過上面的角色系統**，一律只有擁有者能用：

| 受閘範圍 | 內容 |
|---|---|
| 整群（新增子指令自動受閘） | `/input`、`/screen`、`/win`、`/clip`、`/locate`、`/macro`、`/watch`、`/proc`、`/host` |
| 零散指令 | `/sys restart`、`/sys git_pull`、`/sys undo`、`/sys audit`、`/sys cleanup_debug`、`/sys introspect_dom`、`/sys dashboard`、`/config set`、`/config reset`、`/config reload`、`/log clear`、`/schedule add`、`/schedule list`、`/schedule remove`、`/schedule run`、`/gen image`、`/gen image_queue` |

不在此列的（產圖佇列、批次控制、唯讀診斷、跨頻道工具）仍走頻道＋角色閘。

```{warning}
這道閘**排在角色閘之前**，而且刻意不看 `user_roles`。三份角色清單都空是預設
值，那時角色閘整個停用——把桌面控制掛在一個預設關閉的機制下面，等於沒有保護。
```

## presence_games.json / presence_music.json / presence_rpc.json

presence 用的偵測規則，presence 探針每次 tick 重讀、不必重啟。三個檔都用
`utf-8-sig` 讀，所以存成帶 BOM 的 UTF-8 也不會整份解析失敗。

- `presence_games.json`：遊戲白名單。key 是 `<exe>.lower()`、value 是顯示名稱；
  `*<exe>` 前綴代表 priority（多遊戲同時跑時贏）。
- `presence_music.json`：串流音樂偵測規則（SMTC source 白名單、本機檔案播放器
  預設排除、瀏覽器 AUMID hint、
  視窗標題 hint、前景視窗 regex）。壞掉的 regex 會印 stderr 警告並略過該條，
  不會讓探針崩潰。
- `presence_rpc.json`：本機 Rich Presence 設定與開發工具偵測開關。
  `enabled: false` 或沒有 `client_id` → 整段停用（預設就是停用）。

三個檔都**不在版本庫裡**，範本是 `presence_games.example.json` 等。整份不存在時
對應的偵測就安靜停用，其他功能不受影響。

`presence_probe.py` 自己讀這幾個檔（沒有獨立載入器）。用 `/sys probe_status` 看
目前偵測到什麼、為什麼狀態是這樣——最後一行的「對外廣播」是判斷「收下 ≠ 廣播」
的關鍵，見 {doc}`troubleshooting`。
