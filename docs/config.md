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
| `bot_config.json` | bot 啟動時讀一次 | **是（`/sys restart`）**；缺席時 bot 拒絕啟動。少數幾個鍵可以用 `/config reload` 當場重讀，見下 |
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
| `quota_wait_poll_sec` | 3600 | 額度用完時，關掉購買視窗後每隔幾秒重試一次（預設 1 小時）。等待期間**不算失敗**、也不會結束背景程式，所以監督器不會介入、不會重跑登入。**⚠️ 這不是吞吐量旋鈕，調小不會讓批次跑更快。** 實測跨兩個設定值、214 次被擋：約 14 分鐘輪詢 **7.87 張/小時**、約 60 分鐘輪詢 **7.84 張/小時**——差 0.4%。上限在帳號那一側，我們這一側的輪詢間隔影響不到它；調小只是把同樣的圖切成更多更小的爆發，而每次醒來都要多付一次「關購買視窗 → 關不掉 → 整頁重新整理 → 重填欄位」 |
| `model_candidates` | `["NAI Diffusion V5 Full", "NAI Diffusion V5", "Diffusion V5 Full"]` | 產圖模型的候選字面，**依序**嘗試、第一個在下拉選單裡命中的就採用。站方改寫選項字面時不必動碼，補一個候選即可；全部落空時 log 會 dump 當下看得到的選項，那行就是該填什麼的答案。**只能編輯檔案**——不在 `/config show` / `/config set` 的清單裡，因為值是外部服務的模型名，印進頻道會踩到對外揭露規則。每次 setup 重讀（`restart_chrome_every_n_characters: 1` 時等於每個角色一次）。⚠️ 換模型會改變畫質，也可能改變吞吐量：站方的方案政策對不同模型可能套不同的使用量上限 |
| `quota_wait_max_sec` | 0 | 等待總秒數上限，**0 = 一直等到額度回復**（預設）。設正值時超過就停止並通知——那代表不是會自己回補的額度，而是方案／帳號問題 |

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

```{tip}
`/config set` 寫入時只更新你指定的那一個鍵、保留其他既有覆寫，並把檔案維持
精簡（不會把所有預設值都寫進去）。值會在寫入前驗證型別 / 範圍，不合法直接拒絕。
```

## bot_config.json

bot runtime 設定，**啟動時讀一次**，改了要 `/sys restart` 才生效（這些值在事件
迴圈啟動前就 wire 進 dispatcher / presence 任務 / 頻道過濾器）。例外是 `/config reload`
（限擁有者）會當場重讀的那幾個：`user_roles`、`alert_user_id`、`min_free_disk_gb`、
`gui_control`、`daily_health_report`、`dashboard`、`dorossi_model_check`。

範本是 repo 根目錄的 `bot_config.example.json`，每個鍵旁邊都有一段 `_..._comment`
說明。`_` 開頭的鍵是註解，載入器會略過；**認不得的鍵會被略過並印一行警告**——鍵名
打錯的症狀跟沒生效一模一樣，所以對照範本寫。值的型別或範圍不對時，那一個鍵退回
預設值，其他鍵照常生效。

### 一般

- `channel_id`（必填，預設 `0`）— 限頻道指令唯一生效的頻道（擁有者可跨頻道）。
  `0` ＝還沒設定，bot 在啟動時擋下來
- `owner_user_id`（必填，預設 `0`）— **你自己的使用者 ID**。操作主機的指令群與
  `/dorossi` 只認這一個 ID。`0` ＝沒設定，那些指令對所有人一律拒絕（fail-closed）
- `path_reveal_channel_ids`（預設 `[]`）— 允許顯示**完整主機路徑**的頻道 ID 清單。
  Dorossi 的工作目錄顯示在這些頻道與 1:1 私訊給完整路徑，
  其餘表面只給末段目錄名。空清單＝私訊限定。這是**表面**閘不是身分閘
  ——列進去的頻道，同頻道的其他人也看得到那些路徑
- `user_roles` — `admin_user_ids` / `operator_user_ids` / `viewer_user_ids`。
  斜線與文字兩條路徑走**同一道閘**（頻道 → 主機控制 → 角色 → 計數 → 稽核），
  且**預設 fail-closed**：沒有明確標成公開的指令一律受閘。三份清單都空
  = 角色閘停用、只剩頻道閘；`owner_user_id` 永遠算 admin。
  ⚠️ **所以不要把「這個指令很危險」的保護只寫進角色表**——三份清單都空是預設
  值，那時角色表等於沒有作用。操作主機的指令另外硬綁擁有者（見下）
- `alert_user_id`（預設 `0`）— 批次出重大狀況時（嚴重錯誤、快速失敗放棄、連續失敗、
  磁碟快滿）要 @ 誰。`0` ＝不 @ 人，通知照樣貼到頻道。通常填成跟 `owner_user_id` 一樣
- `min_free_disk_gb`（預設 `5.0`）— 產出資料夾所在磁碟的最低可用空間（GB）。低於這個值
  `/run` 拒絕啟動，跑到一半掉下去會通知。`0` ＝停用這項檢查
- `keep_bot_awake`（預設 `true`）— 批次監督期間（含斷網後等網路回來的那段）與 Dorossi
  回合／自走迴圈進行中，bot 要不要持有電源要求。保的是 **bot 這個行程**在主機待命時
  不被暫停，不是讓主機不睡，也不涵蓋 bot 啟動的子行程；用電池時系統會在睡眠逾時後
  5 分鐘收回。`false` = bot 什麼都不持有
- `target_presence_username`（預設空字串）— 要把哪位使用者的線上狀態鏡像到 bot 身上
  （使用者名稱，不分大小寫）。空字串＝不鏡像；本機的遊戲／音樂偵測不受影響
- `presence_probe_interval_sec`（預設 `8.0`）— presence 探針多久跑一次（秒），必須大於 0
- `event_poll_seconds`（預設 `5.0`）— bot 多久讀一次批次寫的事件檔（秒），通知的延遲
  大約就是這個數，必須大於 0
- `api_contact`（預設空字串）— 放進對外 User-Agent 的聯絡方式（網址或信箱）。有些外部
  站台拒絕只有描述的 User-Agent、回 403，直到裡面有聯絡得到人的方式；空字串＝不帶，
  那些站台就繼續 403。這個字串會送到第三方，所以刻意留給你自己填
- `default_help_lang`（預設 `zh-tw`）— `/help` 預設語言（`zh-tw` / `zh-cn` / `en`）
- `daily_health_report` — 選用的每日健康報告（見下面的巢狀區段）
- `dorossi_model_check` — 每日一次的後端模型目錄檢查（掛在健康迴圈上，但有自己
  的開關，關掉健康報告不會連它一起關掉）。檢查的做法是拿「族別名」叫一次後端、
  讀回它解析成哪一個完整模型，讀到就把子行程收掉——所以**一次檢查不消耗任何額度**，
  約十秒。結果寫進執行期的模型目錄檔，載入時併回內建的模型表，`/dorossi model`
  因此會自己跟上新模型。公告只講別名
- `dashboard` — 本機唯讀狀態儀表板的位址（見 {doc}`dashboard`）
- `webrunner_supervisor` — 批次子行程的監督參數，bot 的 `/run` 監督與
  `start_webrunner.py` 共用同一組（見下面的巢狀區段）
- `dorossi_*` — Dorossi 問答功能的整組設定，逐鍵說明見下面的「Dorossi」一節
- `gui_control` — `/proc launch` 能啟動哪些程式（見下面的巢狀區段）
- `platforms` — 其他對話平台的開關與身分設定（見下面的「platforms」一節）

### 巢狀區段

| 鍵 | 預設 | 說明 |
|---|---|---|
| `user_roles.admin_user_ids` | `[]` | 可以 kill／launch／restart／git_pull／config reload 的人 |
| `user_roles.operator_user_ids` | `[]` | 可以編佇列、啟停批次的人 |
| `user_roles.viewer_user_ids` | `[]` | 只能下唯讀的狀態／健康／log 指令的人 |
| `daily_health_report.enabled` | `false` | 每日健康報告的總開關 |
| `daily_health_report.channel_id` | `0` | 報告貼到哪個頻道；`0` ＝沿用 `channel_id` |
| `daily_health_report.time` | `"09:00"` | bot 所在時區的 `HH:MM`，每分鐘檢查一次 |
| `dashboard.host` | `"127.0.0.1"` | 儀表板綁定的位址。保持 loopback，除非你清楚自己在把狀態開給誰看 |
| `dashboard.port` | `8765` | 儀表板的連接埠 |
| `dorossi_model_check.enabled` | `true` | 關掉的話模型清單就固定在程式內建的那幾筆 |
| `dorossi_model_check.interval_hours` | `24.0` | 隔多久檢查一次，必須大於 `0`。上次檢查的時刻存在模型目錄檔裡，所以重啟 bot 不會重跑 |
| `dorossi_model_check.announce_channel_id` | `0` | 發現新模型時公告到哪個頻道；`0` ＝沿用 `daily_health_report.channel_id`（它自己 `0` 就是 `channel_id`） |
| `webrunner_supervisor.fallback_window_sec` | `300` | `/run` 先試 wrapper 變體；它在這麼多秒內非零結束就當成啟動失敗，自動改跑 Selenium 變體。過了這個視窗之後的崩潰不再切換 |
| `webrunner_supervisor.respawn_backoff_min_sec` | `5.0` | 崩潰後重生的退避起點（秒） |
| `webrunner_supervisor.respawn_backoff_max_sec` | `300.0` | 退避上限（秒） |
| `webrunner_supervisor.healthy_threshold_sec` | `60.0` | 活超過這麼久才算健康，退避重設回起點 |
| `webrunner_supervisor.rapid_fail_threshold_sec` | `30.0` | 活不到這麼久就算「快速失敗」 |
| `webrunner_supervisor.rapid_fail_giveup_count` | `5` | 連續快速失敗幾次就停止重生並通知，要人重下 `/run`（擋「瀏覽器起不來 → 無限重生」） |
| `webrunner_supervisor.zero_progress_giveup_count` | `2` | 快速失敗的慢速版：連續幾輪「有嘗試產圖卻一張都沒存」就停。`2` ＝重生一次再停，`1` ＝不重生 |
| `webrunner_supervisor.oneshot_retry_giveup_count` | `3` | `/gen image` 單張伺服器專用：同一筆請求連續死掉幾次就放棄（`1` ＝不重試）。跟批次監督無關 |
| `webrunner_supervisor.oneshot_retry_backoff_min_sec` | `5.0` | 單張伺服器重試的退避起點（秒） |
| `webrunner_supervisor.oneshot_retry_backoff_max_sec` | `120.0` | 單張伺服器重試的退避上限（秒），刻意比批次的小——下面有人在等那一則訊息 |
| `gui_control.launch_whitelist` | `[]` | 可以啟動的 exe 名稱或絕對路徑，比對不分大小寫（含／不含 `.exe`、只比檔名都算） |
| `gui_control.launch_aliases` | `{}` | 自訂名稱 → 目標。目標可以是路徑或 URI（例如遊戲平台的 `rungameid` 連結），URI 交給系統開啟 |

`gui_control` 兩者皆空時 `/proc launch` 全面停用（範本帶了幾個系統內建的小程式當例子）。
**shell 類（命令列、PowerShell、終端機）刻意不要放進去**：配合 `/input type` 等於遠端
任意程式碼執行。

### Dorossi（`dorossi_*`）

全部在 bot 啟動時讀一次，改了要 `/sys restart`。「後端」指 Dorossi 實際叫用的對話後端
（`/dorossi ai` 可以逐個工作階段切換）。範本只列了常調的幾個，其餘不寫就是預設值。
用法見 {doc}`dorossi`。

| 鍵 | 預設 | 說明 |
|---|---|---|
| `dorossi_backend` | `"claude_code"` | 預設那一家走哪條路：`claude_code`（本機 CLI，用量走那台機器登入帳號的方案）或 `api`（直接連線、沒有工具，憑證由 SDK 從環境變數讀，repo 裡不放）。兩條都是選用的：都沒裝，`/dorossi` 只是用不了，其他功能不受影響 |
| `dorossi_cc_tools` | `"off"` | CLI 後端的工具模式。`off`＝純聊天、停用所有工具；`full`＝完整代理人，可以在主機上**無確認**執行指令與讀寫檔案，只有擁有者的提問會這樣跑。第三個後端做不到純聊天，`off` 時拒絕執行 |
| `dorossi_cc_max_parallel` | `3` | 所有工作階段加起來同時在跑的後端回合上限（限制同時起的子行程數，是資源閥不是花費上限）。最小 1 |
| `dorossi_max_parallel_loops` | `3` | 同時進行的自走迴圈上限（每個工作階段至多一個）。`0`＝不設限。自走迴圈不佔上一鍵的名額 |
| `dorossi_cc_idle_limit_sec` | `600.0` | 這麼久完全沒有輸出、沒有工具在跑、CLI 也沒回報背景工作，才當成卡住（最小 60） |
| `dorossi_cc_hard_limit_off_sec` | `900.0` | 一輪的絕對上限（`off` 模式，最小 60）。它也是同一個對話佇列一定會前進的保證 |
| `dorossi_cc_hard_limit_full_sec` | `10800.0` | 一輪的絕對上限（`full` 模式，給長的代理人任務，最小 60） |
| `dorossi_loop_silence_limit_sec` | `1800.0` | 自走迴圈裡一輪完全沒有輸出多久就重生那一輪（工具還在跑也算；CLI 回報背景工作時例外）。最小 60，不能關 |
| `dorossi_silence_retry_max` | `2` | 輸出沉默的重試上限。`0`＝不重試 |
| `dorossi_error_retry_max` | `3` | 非預期錯誤的重試上限（退避 20→40→80 秒）。`0`＝不重試 |
| `dorossi_transient_max_consecutive` | `20` | 伺服器暫時性故障（過載、5xx）連續幾次才放棄；用指數退避等，跟用量上限分開。`0`＝不設限 |
| `dorossi_usage_wait_fallback_sec` | `900.0` | 撞到方案用量上限、又拿不到機器可讀的重設時刻時，第一次等多久；之後每再撞一次加倍，到下一鍵為止（最小 60） |
| `dorossi_usage_wait_max_sec` | `21600.0` | 單次等待的上限。就算後端說三天後才重設，也最多等這麼久就再試一次（最小 60） |
| `dorossi_usage_wait_max_consecutive` | `0` | 連續等幾次都沒有任何一輪成功就放棄。`0`＝不設限 |
| `dorossi_loop_autoresume_max_age_sec` | `86400.0` | 被打斷（bot 重啟、斷線）的自走任務，最後一次心跳在多久以內才自動接回來。`0`＝只能手動 `/dorossi session continue`。撞到用量上限、存檔等重設的任務不受這個開關影響 |
| `dorossi_loop_autoresume_max_tries` | `5` | 連續自動接續幾次都沒有任何一輪跑完就不再自動接（防止「一接就讓 bot 當掉」的無限重啟）。`0`＝不設限 |
| `dorossi_loop_compact_every_rounds` | `10` | 自走迴圈每隔幾輪插入一次原地壓縮，讓之後續談送的前綴變小。`0`＝停用這條 |
| `dorossi_loop_compact_cost_usd` | `10.0` | 自上次壓縮以來累積的估算花費達到這個美元數也壓縮一次。`0`＝停用這條 |
| `dorossi_compact_context_tokens` | `300000` | 某一輪送進後端的脈絡超過這麼多 token 就壓縮一次（單輪與自走共用）。`0`＝停用這條 |
| `dorossi_session_max_age_days` | `14.0` | 目前的工作階段超過這麼多天沒用，下一輪就自動清空脈絡。`0`＝停用 |
| `dorossi_api_history_max_msgs` | `40` | `api` 後端自己保存對話歷史時最多留幾則。`0`＝不設限 |
| `dorossi_max_budget_usd` | `0.0` | CLI 後端每一次叫用的花費上限（美元）。`0`＝不設；設了非零值，超出時那一輪安靜收尾、當成沒有產出 |
| `dorossi_self_judge_enabled` | `true`（範本填 `false`） | 一般提問要不要讓後端自己判斷「這需要多輪自主完成」而轉進自走迴圈。`false`＝只有明確措辭才進自走，省 token |

### platforms（其他對話平台）

一個平台一個子區段，**除了預設平台都預設關著**；關著的平台完全不啟用，連憑證檔都不會讀。
完整的開通步驟與能力對照見 {doc}`platforms`。

| 鍵 | 預設 | 說明 |
|---|---|---|
| `platforms.discord.enabled` | `true` | 預設平台也可以關掉；關掉之後 `start_platforms.py` 不起它的行程 |
| `platforms.telegram.enabled` | `false` | 總開關。`false` = 完全不啟用，連憑證檔都不會讀 |
| `platforms.telegram.owner_user_ids` | `[]` | **這個平台上**的擁有者使用者 ID（字串陣列）。`owner_user_id` 是另一個平台上的 ID，在這裡不成立，所以要各自列。空清單 = 這個平台上沒有人過得了主機控制閘 |
| `platforms.telegram.allowed_chat_ids` | `[]` | 這個平台的「設定對話」ID（字串陣列）。等同上面的 `channel_id`：非擁有者只能在這些對話裡下指令，其餘一律安靜忽略。空清單 = 只有擁有者用得到 |
| `platforms.telegram.poll_timeout_sec` | `30` | 長輪詢一次等多久（秒）。必須大於 0；要關掉請用 `enabled` |

憑證放在 repo 根目錄的 `<平台>_bot_token.md`（範本是 `telegram_bot_token.example.md`）。
這些檔案跟 `discord_bot_token.md` 一樣**不進版本庫**（`.gitignore`）；空檔案＝沒設定，
那個平台安靜缺席。

```{warning}
`owner_user_ids` 是**這個平台上的** ID，跟其他平台的 ID 沒有任何關係。判定
fail-closed：不在清單裡、或根本取不到身分，一律不是擁有者，主機控制指令全部
拒絕。
```

## 主機控制：硬綁擁有者

操作 bot 那台電腦的指令**不經過上面的角色系統**，一律只有擁有者能用：

| 受閘範圍 | 內容 |
|---|---|
| 整群（新增子指令自動受閘） | `/input`、`/screen`、`/win`、`/clip`、`/locate`、`/macro`、`/watch`、`/proc`、`/host`、`/schedule`、`/launcher` |
| 零散指令 | `/sys restart`、`/sys git_pull`、`/sys undo`、`/sys audit`、`/sys cleanup_debug`、`/sys introspect_dom`、`/sys dashboard`、`/sys backfill_paths`、`/sys churn`、`/out debug_show`、`/config set`、`/config reset`、`/config reload`、`/log clear`、`/gen image`、`/gen image_queue` |

不在此列的（產圖佇列、批次控制、唯讀診斷、跨頻道工具）仍走頻道＋角色閘。`/dorossi`
的大部分子指令另外在指令內部只認擁有者（見 {doc}`dorossi`）。這張表與程式裡的
`_OWNER_ONLY_GROUPS`／`_OWNER_ONLY_SLASH` 由 `test_docs_sync` 兩向對帳。

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
