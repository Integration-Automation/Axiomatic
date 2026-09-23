# 疑難排解與監控

## 先看這三個

| 想知道 | 用什麼 |
|---|---|
| webrunner 還活著嗎、在跑哪個角色 | `/status` / `/gen current` |
| 最近有沒有產圖、速率多少 | `/out rate` |
| 最近有沒有錯誤 | `/log errors`（一次列出 WARN / ERROR / CRITICAL / Traceback / Chrome OOM） |

`/sys health` 是綜合健檢，一次看 webrunner / 監督器 / bot / 檔案大小 / 磁碟 /
log 警告計數 / favorites 合法性 / `.backup` 累積 / presence 狀態。

## 跑批好像卡住了

1. `/gen current` — 看當前角色與「最新一張多久前」。
2. `/out rate` — 若「最新一張」超過 1 小時前會標 ⚠️。
3. `/log errors` / `/log tail 50` — 看 log 尾巴與錯誤行。
4. 必要時 `/out debug_show` 看最近的除錯截圖（需先把 `debug_screenshots` 設為
   `true` 並 `/stop` + `/run`）。

## Chrome 一直崩潰 / OOM

長時間跑批最常見的失敗是 Chrome「Aw, Snap! — Out of Memory」，在低記憶體機器
上接著下一次 spawn 會以 `SessionNotCreatedException` 死掉。緩解措施都已內建：

- **定期重啟 Chrome**（`restart_chrome_every_n_characters`）是真正的 OOM 解方
  ——確認的主因是出圖服務單頁應用的頁內記憶體洩漏，靠 flag 只能壓低基線。
  若單一角色自己的 240 張中途就 OOM，調低 `images_per_character`。
- **啟動時清剿孤兒 Chrome**：每次 webrunner 啟動會先 kill 所有殘留的
  `chrome.exe` / `chromedriver.exe`（msedge 不動），打破「崩潰後一直找不到
  目標」的死亡螺旋。
- **spawn 前清 profile 鎖檔**：殺掉 Chrome 不一定釋放 `.chrome_profile/` 裡的
  SingletonLock 等鎖檔，webrunner 會在 `webdriver.Chrome(...)` 前刪掉它們
  （只刪鎖檔，cookies / 登入不動）。

第一個診斷步驟永遠是讀 `chromedriver.log`（spawn 失敗的真正原因在那裡，
selenium 的「Chrome instance exited」本身沒有原因）。

## 中斷後沒有接續，反而開了 `<角色>_2` 資料夾

被中斷的角色本來會接續：`webrunner_progress.json` 記著「上一輪跑到哪個配對、
存在哪個資料夾」，下一輪開頭若**四個欄位**（主提示詞／角色一／角色二／排除詞）
與現在佇列裡的第一個配對完全相同，就會沿用同一個資料夾、只補剩下的張數。

四欄是**全等比對**，所以只要佇列被改過就不算同一個配對——包含在編輯器裡存一次
檔而動到空白或換行這種看不出來的改動。這時它會判定成「不同的配對」，另外配一個
編號資料夾從第 1 張開始，避免不同提示詞的圖混進同一個資料夾。

判斷是哪一種情況：

- Discord 會收到一則「進度紀錄與目前佇列對不起來」的通知，並指出是哪一類欄位不同。
- `/log tail` 或 `/log grep resume` 找 `resume:` 開頭的行，裡面有每個欄位的
  舊值／新值前 60 字。啟動器與 `/run` 都會把這些寫進紀錄檔，關掉主控台也還在。

如果不是你改的，最常見的原因是編輯器存檔時動到行尾空白；把該行改回原樣，或直接
讓它重跑（前一個編號資料夾裡的圖不會被刪）。

## 登入失敗 / 一直 fallback 到 selenium

`/run` 先試 je 變體，5 分鐘內非零退出（通常是 `/login` 被反機器人擋）會自動
切到 selenium 變體並公告。若兩個變體都在啟動階段就快速失敗，監督器的
rapid-fail giveup 會在連續 5 次（`rapid_fail_giveup_count`）短命崩潰後貼警告
並停止重生，避免無限迴圈。

## `/run` 說「nothing would run」

代表實際會跑的 pair 為 0：

- **佇列全空且無 fallback** → 用 `/todo prompt add` / `/todo char1 add` 等先排工作。
- **第一筆就是 `end`** → 用 `/todo prompt unend` 移除終止標記再 `/run`。

`/gen plan` 可先看完整會跑什麼，`/gen preview` 看下一筆。

## 產圖明明在跑但 Discord 狀態卡住

presence 鏡像有去重，網路斷線重連後快取可能過期。`on_resumed` 會清掉去重 key
並重新套用一次。用 `/sys probe_status` 確認本機偵測到什麼。

## 本機 Rich Presence 的活動卡片沒出現

先看 `/sys probe_status` **最後一行的「對外廣播」**，不要從送出那一側判斷：
送出那一側**每一項都會顯示正常**（偵測到活動、activity 組得出來、套用回成功、
連線 connected），因為桌面端的活動隱私開關關著時是**照收、照回成功、不外送**。
那一行是拿別人看得到的成員資料回頭比對，才分得出「收下」與「廣播」。

- ✅ → 真的有廣播出去，看不到卡片是別的原因（對方的顯示設定、快取）。
- ⚠️ → 收下了但沒廣播，先去桌面 app 打開活動隱私開關。
- 沒有這一行 → **判斷不出來**（沒有要顯示的活動、找不到成員資料、`client_id`
  不是數字）。這是刻意保持安靜：把不確定報成警告只會製造新的假線索。

## Dorossi 每一輪都回「暫時無法回應」

在主機上看 bot 的紀錄檔（repo 根目錄的 `discord_bot.log`），找 `[dorossi]` 開頭的行：

- **`has no usable sign-in`**：後端 CLI 沒有可用的登入（沒登入、憑證失效、或被迫進了不讀登入的精簡
  模式）。重試不會自己好，所以自走任務會直接停下、對話**不會**被丟掉重開。在主機上互動式開一次 CLI
  完成登入，再用 `/dorossi session continue` 接回自走任務。同一段裡若有 `looks like bare mode`，代表 CLI
  是以精簡模式啟動的——查主機上有沒有設 `CLAUDE_CODE_SIMPLE`。
- **`is set; not passing it`**：主機環境裡有 `ANTHROPIC_API_KEY`、
  `ANTHROPIC_AUTH_TOKEN` 或 `CLAUDE_CODE_SIMPLE`。這三個變數**刻意不交給** CLI 後端：前兩個是給 `api`
  後端用的，交給 CLI 會讓它安靜地從訂閱方案改成按用量計費、而且沒有方案上限；第三個會讓每一輪都以
  「沒登入」失敗。`api` 後端照樣讀得到前兩個，這一行只是告知，不必處理。
- **`is authenticating with … instead of the subscription login`**：CLI 自己的設定檔（env 區塊或
  apiKeyHelper）指定了 API key，計費已經不走訂閱方案。到 CLI 的設定檔把它拿掉。

## 待辦檔格式注意事項

- 一行一筆，**只用換行切分**；entry 內可含 `,` / `，` / `::` / 括號。
- 空白行會被略過；NBSP（`\xa0`）會被換成一般空白。
- 不要改成逗號 / JSON / YAML 格式——on-disk 格式是固定的。

## 想停整批

- **即時停止**：`/stop`（殺掉所有 webrunner 與 Chrome）。
- **跑到某處自然停**：在 `todo_prompt.md` 想停的位置放 `end`（`/todo prompt end` 或
  `/todo prompt insert <i> end`），webrunner 跑到那裡會乾淨結束。佇列是每個角色邊界
  動態重讀的，所以**跑批途中放 `end` 也會生效**（最晚下個角色邊界收尾）。
  見 {doc}`workflow`。

## 兩個監督器不要同時跑

不要同時用 `start_webrunner.py` 和 bot 的 `/run`——會搶 `.chrome_profile/`
的鎖，且 bot 的全機 Chrome 清剿不知道獨立啟動器的子行程。
