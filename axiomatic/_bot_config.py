"""Shared loader for `bot_config.json`.

Loaded once by `discord_bot.py` at import time (before any handlers run).
Changes require `!restart` to take effect — most values wire into things
set up before the message dispatcher starts (channel_id, presence target,
probe interval task), so live reload would require restarting Discord
client tasks anyway.

Missing / malformed file → return defaults, never raise. Wrong-type
values fall back to the per-key default with a stderr warning, and a key this
module does not recognise (a typo) gets its own one-off warning — the two look
identical from the user's side (the setting silently does nothing).

**那句「with a stderr warning」在 2026-09-09 之前是假的**：整個模組只有兩個
`print`，兩個都是**整份檔案**讀不到／解不開，逐鍵的警告一個都沒有。症狀是使用者
手動編輯 `bot_config.json`、某個值型別打錯 → 安靜退回預設 → 而這個檔案的變更本來
就**需要重啟才生效**，所以他重啟了、以為設定生效了，其實跑的是預設值，手上沒有
任何線索（要發現只能自己拿 `_DEFAULT_BOT_CONFIG` 對帳）。現在扁平鍵全部走
`_take()`／`_COERCERS`，巢狀區段走各自的小表，所以「新增一個設定卻忘了加警告」
不再是「記得要加」而是不可能發生。

**上面那句「Loaded once ... at import time」只對 `discord_bot.BOT_CONFIG` 成立，
對這個函式本身不成立**——`_external_apis._user_agent()` 每一次對外 HTTP 請求都會
呼叫一次 `load_bot_config()`（讀 `api_contact`），`dorossi_backend` 另外有自己的
一份，`/config reload` 還會再重載。所以重複的抱怨必須由 `_warn_once` 收斂，否則
一個放著沒改的錯字會用同一行文字洗掉整份記錄檔（`discord_bot.log` 已經為了另一
件事發生過一次：96% 的行是同一句話）。
"""
from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

# `float()` 轉得過去的上限。與超大 int 比較不會溢位，所以拿它當「這個數字轉得成
# float 嗎」的守門（見 `_is_finite_number`）。
_FLOAT_MAX = sys.float_info.max

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOT_CONFIG_FILE = _PROJECT_ROOT / "bot_config.json"

_DEFAULT_SUPERVISOR: dict = {
    "fallback_window_sec": 300,
    "respawn_backoff_min_sec": 5.0,
    "respawn_backoff_max_sec": 300.0,
    "healthy_threshold_sec": 60.0,
    # Rapid-fail giveup: if webrunner exits in < rapid_fail_threshold_sec
    # `rapid_fail_giveup_count` times in a row, supervisor posts an alert
    # and stops respawning. User must `!run` to retry. Prevents the
    # "Chrome can't init → infinite respawn loop redoing first-time setup
    # → channel spam" scenario.
    "rapid_fail_threshold_sec": 30.0,
    "rapid_fail_giveup_count": 5,
    # Zero-progress giveup: rapid-fail only catches runs that die FAST. When
    # generation itself is blocked (the site pops a purchase / account dialog),
    # every run burns the whole `consecutive_fail_abort` budget first — minutes,
    # not seconds — so it never looks rapid and the supervisor respawns forever.
    # This counts consecutive runs that exited rc=3 (attempted generation, saved
    # nothing) and stops after `zero_progress_giveup_count` of them. 2 = respawn
    # once, then stop; 1 = never respawn a zero-output run.
    "zero_progress_giveup_count": 2,
    # `/gen image` one-shot 的重起閥門。**跟上面那幾個是不同的東西**：上面那些是
    # `_watch_for_fallback`（批次監督者）的參數，這三個是 `_reap_oneshot_webrunner`
    # 收屍後重新驅動佇列那條路的。one-shot 刻意不掛批次監督者（它 rc=0 idle 退出是
    # 正常收場，不該觸發 je→selenium 切換／backoff respawn），於是 2026-09-11 之前
    # 那條路上**一個上限都沒有**：背景程式起來就死、收屍、再起，而同一筆請求永遠不會
    # 被服務完，所以再驅動的條件永遠成立（合成環境實測每秒約 9100 圈，每圈都跑一次
    # nuclear sweep 把整台機器的 chrome 殺光）。
    # giveup_count ＝ 同一筆請求連續死幾次就放棄（1 ＝ 完全不重試）。
    "oneshot_retry_giveup_count": 3,
    "oneshot_retry_backoff_min_sec": 5.0,
    # 刻意比 respawn_backoff_max_sec（300）小：那個是保護數小時的無人值守批次，
    # 這個底下有個人正盯著佔位訊息看。
    "oneshot_retry_backoff_max_sec": 120.0,
}

_DEFAULT_GUI_CONTROL: dict = {
    # `!launch <name>` 只接受出現在這個清單裡的程式。case-insensitive；
    # 比對策略：(1) 完整字串 (含/不含 .exe) (2) basename 比對。空清單
    # → `!launch` 全面停用（回 "no programs whitelisted"）。把 .exe
    # 直接寫在這裡，或寫絕對路徑（路徑就照原樣 launch）。即使是
    # CHANNEL_ID 限定的 `!` 也要走這條，因為一旦 token 外洩，channel
    # 攻擊者能任意起程式風險太大。
    "launch_whitelist": [],
    # 別名 → launch target。Target 可以是 exe 路徑、`steam://`
    # rungameid URI、任何能被 `os.startfile()` 處理的東西。`!launch
    # <key>` 先比對這個 dict（case-insensitive）再 fall through 到
    # whitelist。讓使用者打 `!launch mygame` 而不用記
    # `steam://rungameid/394360`。
    "launch_aliases": {},
}


_DEFAULT_USER_ROLES: dict = {
    # Optional permission split for `!` commands. Empty lists preserve the
    # historical channel-gated behavior. OWNER_USER_ID is always admin in
    # discord_bot.py even when these are empty.
    "admin_user_ids": [],
    "operator_user_ids": [],
    "viewer_user_ids": [],
}


_DEFAULT_DAILY_HEALTH_REPORT: dict = {
    "enabled": False,
    "channel_id": 0,
    # Local bot process time, HH:MM, checked once per minute.
    "time": "09:00",
}


_DEFAULT_DASHBOARD: dict = {
    "host": "127.0.0.1",
    "port": 8765,
}


# 每日的後端模型目錄檢查（2026-09-23）。掛在既有的每分鐘健康迴圈上，自己用
# `interval_hours` 節流，上次檢查的時刻存在模型目錄檔裡，所以重啟不會重跑。
# `announce_channel_id` 0 ＝沿用健康報告的頻道（它自己 0 ＝ `channel_id`）。
_DEFAULT_DOROSSI_MODEL_CHECK: dict = {
    "enabled": True,
    "interval_hours": 24.0,
    "announce_channel_id": 0,
}


_DEFAULT_BOT_CONFIG: dict = {
    # 限頻道指令生效的那個頻道 ID。**沒有可用的預設值**：0 ＝還沒設定，bot 在
    # 啟動時就會擋下來並叫使用者去複製 `bot_config.example.json`。
    "channel_id": 0,
    # 擁有者的 Discord 使用者 ID。操作主機的指令群（`_OWNER_ONLY_GROUPS`）與
    # Dorossi 都只認這一個 ID。0 ＝沒設定；Discord 的使用者 ID 不可能是 0，所以
    # 這個預設值是 fail-closed 的——那些指令一律拒絕，而不是對任何人開放。
    "owner_user_id": 0,
    # 允許顯示完整主機路徑的頻道 ID（擁有者裁示 2026-08-25）。「Discord 不出現
    # 主機路徑」的硬性規定原本只對 1:1 私訊開一個口；這個清單把同一個口擴到
    # 具名頻道，讓擁有者在自己的頻道裡看得到完整工作目錄而不是只有末段名稱。
    # 空清單（預設）＝維持私訊限定的原狀，其餘頻道一律只給 `_dorossi_dir_leaf`
    # 的末段名稱。這是**表面**閘不是身分閘：清單裡的頻道，同頻道的其他人也看
    # 得到那些路徑，所以只放擁有者自己控制的頻道。
    "path_reveal_channel_ids": [],
    # 外部 API 的聯絡方式，會被接進 User-Agent（`_external_apis._user_agent`）。
    # 「說得出自己是誰」與「聯絡得到人」是兩件事，而且有站台只吃後者：實測
    # 2026-08-30，Wikimedia 的 REST API 對純描述式 UA 一樣回 403，訊息是
    # 「Please respect our robot policy … Contact bot-traffic@wikimedia.org」；
    # 補上一個 URL 或 email 就變 200。
    # 空字串（預設）＝不附聯絡方式，那些站台會繼續 403。要放什麼上去是擁有者的
    # 決定，不是程式該自作主張的事——這個字串會被送到第三方站台。
    "api_contact": "",
    # 要鏡像哪個使用者的 Discord 狀態；空字串（預設）＝不鏡像。
    "target_presence_username": "",
    "default_help_lang": "zh-tw",
    "presence_probe_interval_sec": 8.0,
    "event_poll_seconds": 5.0,
    # Discord user ID to @-mention on critical webrunner alerts (0 = no ping).
    "alert_user_id": 0,
    # Minimum free disk (GB) on the output drive; gates !run + mid-run
    # monitoring. 0 = disabled.
    "min_free_disk_gb": 5.0,
    # 批次監督期間（含斷網後等網路回來的那段）與 Dorossi 工作進行中，bot 持有電源要求
    # （`_power_request`，`PowerRequestExecutionRequired`），Modern Standby 時 bot 這個
    # 行程不被暫停——否則批次在待命中崩潰要等主機醒來才有人重生它。保的是 bot 行程
    # 本身，不是「系統不進入待命」，也不涵蓋子行程；DC 電源下系統會在睡眠逾時後 5 分鐘
    # 撤銷它。預設開；false ＝回到 bot 什麼都不持有的舊行為。
    "keep_bot_awake": True,
    # Backend for the `@bot Dorossi` command. "claude_code" shells out to the
    # local Claude Code CLI (`claude -p`) so it rides the host login's plan
    # (e.g. a Pro/Max subscription) instead of metered API billing; "api"
    # calls the Anthropic API directly via the SDK.
    "dorossi_backend": "claude_code",
    # `@bot Dorossi` 的 claude_code 後端工具模式。"off"（預設）＝純聊天，停用
    # 所有工具（傳空的工具白名單 `--tools ""`，再加列舉的 --disallowedTools 當第二層；
    # 不加 bypassPermissions），最安全；
    # "full" ＝完整 agent（--permission-mode bypassPermissions ＋ 所有工具皆開啟，
    # 可在主機執行 shell／讀寫檔案，需由擁有者自行授權）。僅在 backend 為
    # claude_code 時生效；api 後端不受影響。
    "dorossi_cc_tools": "off",
    # Dorossi claude_code 後端的「硬性牆鐘看門狗上限」(秒)，依工具模式分開：
    #   *_off  ── 純聊天 (dorossi_cc_tools="off")：答案有界、無工具、卡死機率低，
    #             維持較緊的預設 900s (15 分鐘)。
    #   *_full ── 完整 agent (dorossi_cc_tools="full")：擁有者會跑長 agentic 任務
    #             (多 subagent 編排、等背景 subagent、跑整套測試)。2026-09-19 擁有者
    #             反映「等待太短，任務一直被殺掉」（09-18 22:50 一輪在 3600s 被硬上限
    #             砍掉，當時還有一個工具在跑），預設從 3600s 放大到 10800s (3 小時)。
    #             但仍有限——硬上限不能移除，因為 full 模式 bypassPermissions 下
    #             卡死的工具會讓 idle tier 永遠不觸發，必須有牆鐘上限保底；又因
    #             為佇列鎖，一輪最久就是握鎖時間，所以這個上限也是佇列前進的保證。
    #             自走模式也借用它：背景工作還在時，輸出沉默不砍，但只撐到這個秒數。
    # 兩值都會 clamp 到不小於 DOROSSI_CC_HARD_LIMIT_FLOOR_SEC，避免被設成 0／負數
    # 把保護關掉。
    "dorossi_cc_hard_limit_off_sec": 900.0,
    "dorossi_cc_hard_limit_full_sec": 10800.0,
    # Dorossi claude_code 單輪問答的「閒置上限」(秒)：這麼久完全沒有輸出、**而且**沒有
    # 工具在跑、CLI 也沒有回報背景工作，才當成卡住砍掉。有工具或背景工作在跑時它不開火，
    # 由上面的硬上限收尾。2026-09-19 之前寫死 300s、不能設定；擁有者反映等待太短之後
    # 改成可設定、預設 600s。clamp 到不小於 DOROSSI_CC_HARD_LIMIT_FLOOR_SEC——太小會把
    # 每一個需要先想一下的回答砍掉。**設得比硬上限還大也安全**：每次等待都是
    # min(閒置, 離硬上限剩餘)，硬上限永遠先到，閒置那一層只是不再有機會開火。
    # 自走模式不使用這個值（那裡是下面的沉默上限）。
    "dorossi_cc_idle_limit_sec": 600.0,
    # Dorossi 「自走模式」每一輪的「輸出沉默 (output-silence) backstop」(秒)。自走
    # 模式不設回合數上限、也不套用上面的硬性牆鐘上限——改用這個沉默上限保底：一輪
    # 若超過此秒數完全沒有任何新輸出，就一律終止該輪（交給迴圈的沉默重試；即使仍有
    # 前景工具在執行——這是與一般 idle tier 的關鍵差異，避免卡死的工具讓無人值守的
    # 迴圈永遠卡住）。**唯一的例外是 CLI 回報了背景工作**（背景 subagent、背景 shell、
    # 監看工作）：那段沉默是在等它，不砍，但只撐到 dorossi_cc_hard_limit_full_sec。
    # 預設 2026-09-19 從 600s 放寬到 1800s（等背景 subagent 的前景工具最多就會阻塞
    # 600s，剛好撞上舊值）；同樣 clamp 到不小於 DOROSSI_CC_HARD_LIMIT_FLOOR_SEC，
    # 避免被設成 0／負數而把這層保護關掉。
    "dorossi_loop_silence_limit_sec": 1800.0,
    # Dorossi 自走模式撞上「方案用量上限」時的等待策略。後端的用量是每 5 小時滾動
    # 重設的，舊行為（撞到就停掉整個迴圈、留 loop_pending 等人工 `/dorossi session
    # continue`）代表無人值守的長任務每天要人接好幾次，實質上跑不完。現在改成睡到
    # 額度回來再自己續跑。三個鍵：
    #   fallback ── 拿不到機器可讀的重設時刻（只有 `resets 3:45pm` 這種沒時區的鐘點、
    #               或根本沒提）時，第一次等待的秒數；之後每連續再撞一次就加倍。
    #   max      ── **單次**等待的上限。預設 6 小時，略大於 5 小時的滾動視窗，所以
    #               一次等待就足以覆蓋一個完整視窗；後端若報了更遠的時刻（例如週上限）
    #               也最多睡這麼久就再探一次——探測便宜，睡過頭是不可逆的浪費。
    #   max_consecutive ── 連續等待幾次都沒有任何一輪成功就放棄整個迴圈。
    #               **0 ＝不設限（預設）**，符合擁有者「不得有回合／花費類上限」的
    #               裁決；等待本身不花錢，所以預設就讓它一直等下去。
    # 下限（60s）與緩衝（60s）是程式常數、不開放設定：那是誤判成用量上限時的空轉
    # 防護，見 dorossi_backend.DOROSSI_USAGE_WAIT_MIN_SEC。
    "dorossi_usage_wait_fallback_sec": 900.0,
    "dorossi_usage_wait_max_sec": 21600.0,
    "dorossi_usage_wait_max_consecutive": 0,
    # 連續幾次「伺服器暫時性故障」（529 Overloaded／5xx）都沒有一輪成功就停下來。
    # 預設 20：以指數退避（30s 起、封頂 15 分）算，大約等於撐過三小時的服務中斷，
    # 之後仍然停不下來就不像「等一下就好」了。0 ＝不設限。
    "dorossi_transient_max_consecutive": 20,
    # 非預期錯誤的重試上限。無人值守的長任務不該被一次偶發失敗（後端行程被殺、
    # 網路抖動、沒預期到的例外）終結；但重試無限次也只是把「壞掉」變成「安靜地
    # 一直壞」。3 次搭配 20s→40s→80s 的退避夠吸收偶發，又不會拖太久。0 ＝不重試。
    "dorossi_error_retry_max": 3,
    # 輸出靜默（後端這一輪卡住）的重試上限。卡住的多半是那個行程，重生一次常常
    # 就過了；連續卡住才代表不是偶發。0 ＝不重試（維持舊行為）。
    "dorossi_silence_retry_max": 2,
    # 自走迴圈「跨 bot 重啟自動接續」。等額度回來解決的是「後端擋住」，這一組解決
    # 的是「行程沒了」——重啟、主機當機都會讓迴圈連同它的等待一起蒸發，舊行為只留
    # 一個 loop_pending 等人工接。bot 起來時會自己把還「活著」的標記接回去。
    #   max_age_sec ── 標記心跳離現在多久以內才自動接（秒）。**0 ＝關閉自動接續**，
    #                  回到純人工 `/dorossi session continue`。預設 86400（24 小時）：
    #                  蓋得住「一次用量等待（最多 6 小時）＋一段主機停機」，又不會
    #                  在一週後突然跑起一個擁有者早忘了的任務。
    #   max_tries   ── 連續自動接續幾次都沒有任何一輪跑完就不再自動接（當機迴圈的
    #                  斷路器）。任何一輪跑完就歸零，健康的長任務累加不到。0 ＝不設限。
    # 只有「上一個行程是被砍死的」才會自動接：迴圈自願結束（abort／沉默 backstop／
    # 例外／放棄）都會在 finally 裡把標記寫成非 live，而 finally 在行程被砍時不會跑。
    "dorossi_loop_autoresume_max_age_sec": 86400.0,
    "dorossi_loop_autoresume_max_tries": 5,
    # Dorossi claude_code 後端「每一次 `claude -p` invocation（即自走的每一輪、單輪
    # 問答的每一次呼叫）」的美元花費上限，透過 CLI 的 --max-budget-usd 帶入。這是
    # 「每次呼叫」的花費閘，不是回合數上限。0 ＝停用（不帶該旗標）。
    # **預設停用（擁有者裁決）**：擁有者明確裁決「不應該有除了後端本身用量上限以外
    # 的上限限制」（實際撞到 error_max_budget_usd、單輪被舊的 5.0 預設攔下）。此鍵
    # 保留給未來想自行設限的人手動覆寫；不要再把非零預設加回來。
    "dorossi_max_budget_usd": 0.0,
    # Dorossi「自走模式」的「週期性壓縮」觸發門檻——治本：自走無回合上限，resume 會
    # 把整段成長中的對話每輪重送，token ~O(N²)。每隔幾輪／或「自上次壓縮以來」累積
    # 花費越過門檻時，插入一輪 in-place `/compact`（同 session id，保留任務／待辦脈絡，
    # 壓掉舊歷史），讓之後 resume 重送的前綴大幅變小。壓的是 context、不是回合數（與
    # 「自走無回合上限」裁決相容）。兩個門檻任一達到即觸發；各自 0 ＝停用該條。
    #   *_rounds ── 每這麼多「工作輪」壓縮一次。壓縮有損（會摘要掉細節）且會讓快取
    #               失配一次，故預設取較保守（較不頻繁）的 10。
    #   *_cost_usd ─ 「自上次壓縮以來」累積美元花費達此值就壓縮（補足輪數抓不到的
    #               「少數幾輪就燒很兇」情形）。
    "dorossi_loop_compact_every_rounds": 10,
    "dorossi_loop_compact_cost_usd": 10.0,
    # Dorossi 的「脈絡過大就自動壓縮」門檻（token），單輪問答與自走迴圈共用同一把。
    # 某一輪送進後端的脈絡大小 ≈ fresh input ＋ cache_read ＋ cache_creation；越過此
    # 門檻就對該工作階段插入一次 in-place `/compact`（同 session id、保留任務／待辦脈絡、
    # 壓掉舊歷史），讓之後 resume 重送的前綴大幅變小。壓的是脈絡、不是回合數（與「自走
    # 無回合上限」裁決相容）。這是擁有者裁定用來降 token 的唯一手段——不動 effort／
    # 模型／工具設定。預設 300000；0 ＝停用此條。自走迴圈另有輪數／花費兩條觸發，三者
    # 任一達到即壓縮。
    "dorossi_compact_context_tokens": 300000,
    # Dorossi 單輪 session 衛生（保守）：active session 超過這麼多天沒用就在下一輪自動
    # 清空脈絡（從新對話開始），避免長壽單輪 session 無限長大。靜默失憶體驗不好、且
    # `@bot session` 已可手動重置，所以門檻取「明顯過舊」的保守值；0 ＝停用。
    "dorossi_session_max_age_days": 14.0,
    # `api` 後端是**無狀態**的：每一輪都把整份對話歷史重送一次。不設界限的話輸入
    # token 隨輪數線性成長、總成本是輪數的平方；長到超過脈絡窗之後會拿到 400
    # （`invalid_request_error`），而 400 不是暫時性錯誤——重試三次都會用同一份過長
    # 的歷史失敗，那個工作階段從此每一輪都以同樣的方式壞掉，除非有人知道要下
    # `/new`。所以帶「最後這麼多則」就好；0 ＝不限制（與 `dorossi_max_budget_usd`
    # 同慣例）。`claude_code` 那一側不受影響（它有 `/compact`），`codex` 的脈絡在
    # 後端、也不由我們攜帶——這條只約束 `api`。
    "dorossi_api_history_max_msgs": 40,
    # Dorossi 自走的「後端自判進迴圈」總開關。True（預設）＝保留混合觸發（明確片語＋
    # 後端自判）；False ＝只關掉「自判」這條路徑、保留「明確片語」觸發，讓擁有者能單獨
    # 驗證自判是不是頻率放大器。不影響片語快速路徑。
    "dorossi_self_judge_enabled": True,
    # Dorossi 後端「同時在跑的回合數」上限。並行化後，不同 session（實務上＝不同使用者
    # 的 active session，或擁有者切到另一個 slot 的互動回合）可以並行；這個號誌壓住同時
    # 跑的後端 `claude -p` 回合數，避免一次噴太多主機資源／API 併發。同一 session 仍靠
    # per-session 鎖序列化（`--resume` 正確性硬需求），與此上限彼此獨立。clamp 到 ≥1
    # （0／負數／非整數 → 預設）。預設 3。
    "dorossi_cc_max_parallel": 3,
    # 「同時進行的自走迴圈數」操作性上限。這是**行程數操作閥**（防止同時 spawn 的
    # 後端子行程數失控），**不是花費上限**——擁有者已裁決不得有花費類上限，此閥與
    # 花費無關。0 ＝不設限（可選）；預設 3（寬鬆）。與 dorossi_cc_max_parallel
    # （互動回合的號誌上限）互相獨立：自走迴圈豁免於該號誌（abort 即時性優先），
    # 改由這個計數閥控制同時在跑的迴圈數。
    "dorossi_max_parallel_loops": 3,
    "webrunner_supervisor": _DEFAULT_SUPERVISOR,
    "gui_control": _DEFAULT_GUI_CONTROL,
    "user_roles": _DEFAULT_USER_ROLES,
    "daily_health_report": _DEFAULT_DAILY_HEALTH_REPORT,
    "dashboard": _DEFAULT_DASHBOARD,
    "dorossi_model_check": _DEFAULT_DOROSSI_MODEL_CHECK,
}

_VALID_HELP_LANGS = frozenset({"en", "zh-tw", "zh-cn"})
_VALID_DOROSSI_BACKENDS = frozenset({"api", "claude_code"})
_VALID_DOROSSI_CC_TOOLS = frozenset({"off", "full"})
# 硬上限的下限：避免把 Dorossi 看門狗硬上限設成 0／負數／過小而關掉保護。
# 60s 已遠低於任何正常用途，但保證硬 tier 永遠是個有意義的牆鐘上限。
# 看門狗的另外兩層（單輪閒置 dorossi_cc_idle_limit_sec、自走沉默
# dorossi_loop_silence_limit_sec）共用這個下限。
DOROSSI_CC_HARD_LIMIT_FLOOR_SEC = 60.0
# 用量上限等待秒數的下限。與上面同樣的理由、同樣的數字，但是**不同的東西**，所以
# 不共用常數：那一個是「一輪最久可以跑多久」，這一個是「撞牆後最短要等多久再重試」。
# 這裡的 0 不是「關掉保護」而是「熱迴圈」——用量上限的判定字樣比對得很寬，某天有
# 別的錯誤被誤判時，0 秒等待會把它變成不停 spawn 後端行程的空轉。
# dorossi_backend.DOROSSI_USAGE_WAIT_MIN_SEC 是同一個數字的執行期那一份。
DOROSSI_USAGE_WAIT_FLOOR_SEC = 60.0


# 「這個值被拒了」的精準判定。
#
# 下面每一個**扁平**的 `_coerce_*` 都只在拒絕時回傳它的 `default` 引數，而且沒有
# 一個會去讀那個引數的內容——所以傳一個唯一的 sentinel 進去、再用 `is` 比對回傳值，
# 就能 100% 分辨「被拒絕」與「接受了一個剛好等於預設的值」。2026-09-09 對九個
# helper 的十八種輸入逐一實測過。
#
# **刻意不用「比對值」來判定。** `_coerce_help_lang` 會把 `" ZH-TW "` 正規化成
# `"zh-tw"`、`_coerce_dorossi_cc_tools` 會把 `"FULL "` 正規化成 `"full"`——那些是
# **正當的正規化**，不是拒絕。拿值去比會把它們全部誤報成「你的設定被丟掉了」，而
# 一個會亂叫的守門遲早被人關掉（本專案已經為了同一個理由收窄過 `test_language`
# 與 `test_text_encoding` 的掃描範圍）。反方向同樣真實：`presence_probe_interval_sec`
# 寫成字串 `"8"` 會被拒、退回的預設剛好**也是** 8.0，比對值時完全看不出來。
#
# 巢狀區段的五個 coercer 不適用：它們會讀 `default["..."]`／`dict(default)`，
# 塞 sentinel 進去會直接 `TypeError`。那五段改用各自的小表逐欄位走 `_take`。
_REJECTED = object()

# 設定檔的抱怨只印一次。理由見模組 docstring 最後一段（`load_bot_config()` 沒有
# 快取，而且它在每一次對外 HTTP 請求的路徑上）。
#
# 警告去重搬到 `_warn_dedup`（`CLAUDE.md` 允許的被動共用模組：純標準函式庫、
# 不 import 專案裡的任何東西）。原本這裡有一份逐字相同的六行實作，`_batch_config`
# 的註解寫著「若出現第三份就該提成共用模組」——第三份出現了。別名成 `_warn_once`
# 是為了讓既有呼叫端一個字都不用改。
# **雙形狀匯入，不要收回成單獨一行裸名。** 裸名只在「`axiomatic/` 自己在
# `sys.path` 上」時成立——跑 `webrunner_*.py` / `discord_bot.py` 這種腳本時
# `sys.path[0]` 正好就是它們所在的那個目錄，所以本機怎麼跑都對。但
# `start_webrunner.py` 住在 repo root、**刻意**走套件路徑
# `from axiomatic._bot_config import ...`（理由見它自己的註解：裸名版本只有執行期
# 才成立，靜態分析器看不到 `sys.path.insert`）。走那條路徑時 `axiomatic/` 不在
# `sys.path` 上，裸名就是 `ModuleNotFoundError`，而且是在啟動器 import 期炸掉——
# 整支 webrunner 起不來，rc=1，重啟幾次都一樣。2026-09-12 實際發生過。
# `_external_apis` 從一開始就是這個形狀。
try:
    from _warn_dedup import warn_once as _warn_once   # noqa: E402
except ImportError:  # 套件路徑（`from axiomatic import _bot_config`）
    from axiomatic._warn_dedup import warn_once as _warn_once  # type: ignore  # noqa: E402


def _warn_unknown_keys(raw: dict, known, *, source: str) -> list:
    """設定檔裡有、但本模組不認得的頂層鍵 → 出聲一次。回傳那些鍵（給測試看）。

    **打錯鍵名的症狀跟打錯值的型別一模一樣：設定沒生效。** 而這個檔案本來就要重啟
    才生效，所以使用者重啟完只會以為生效了。上面每一個 `_take` 都為「值不對」出聲，
    但在補這一支之前**沒有任何東西**為「鍵名不對」出聲——載入器只走自己認得的鍵，
    `raw` 裡多出來的東西連讀都沒讀到。

    `_` 開頭的鍵是本專案在 JSON 裡寫註解的慣例（`bot_config.json` 現在有 16 個
    `*_comment`），不算未知。

    **只印鍵名，不印值。** 值可能是使用者填的主機路徑（`gui_control.launch_aliases`
    就是這種），而 stderr 會進 log、`/log tail` 會把 log 送進對話平台——同一條理由
    讓 `_shown()` 對容器只印型別名。鍵名本身也截斷、數量也設上限：一個貼壞的 JSON
    不該把整份記錄洗掉。
    """
    unknown = sorted(key for key in raw
                     if key not in known and not str(key).startswith("_"))
    if unknown:
        shown = ", ".join(str(k)[:40] for k in unknown[:8])
        more = "" if len(unknown) <= 8 else f"（另有 {len(unknown) - 8} 個）"
        _warn_once(
            f"{source}: 不認得這些設定鍵，已忽略：{shown}{more}。"
            "鍵名打錯的話設定不會生效，而且沒有其他症狀——請對照預設值表確認拼字。")
    return unknown


def _shown(value) -> str:
    """訊息裡怎麼呈現「收到的值」。

    容器**只印型別名、不印內容**：`gui_control.launch_aliases` 的值是主機路徑與
    URI，而 stderr 會進 `discord_bot.log`，那個檔案有 `/log tail` 這條使用者面的
    出口。那條路徑確實會過 `_redact_for_discord`，但「安全性靠下游某個 scrubber
    才成立」正是本專案一再吃虧的形狀——把保證留在本地。純量沒有這個問題：能被拒絕
    的純量依定義就不是合法值（`api_contact` 只有在**不是**非空字串時才會被拒，所以
    警告裡永遠不可能出現一個真的 email）。
    """
    if isinstance(value, (list, tuple, dict, set)):
        return type(value).__name__
    return repr(value)


def _coerce_bool(value, default: bool) -> bool:
    """Accept only an actual JSON boolean; anything else (incl. "true"/1) falls
    back to the default, matching the wrong-type → default pattern."""
    if isinstance(value, bool):
        return value
    return default


def _is_finite_number(value) -> bool:
    """數值型別檢查的共同前提：不是 bool、是 int/float、而且**轉得成有限的 float**。

    兩件事單看程式碼都不明顯，但兩件都從 JSON 進得來：

    1. **`inf` / `nan`。** `Infinity` / `NaN` 是 Python 對 JSON 的擴充，
       `json.loads` 預設就吃；而且不必有人手打 `Infinity`——`1e400` 這種看起來
       完全正常的字面值 parse 出來就是 `inf`。`inf > 0` 為真，所以任何
       「`isinstance(v, (int, float)) and v > 0`」形式的檢查都會放行。
       實測後果：`inter_image_delay_sec: [Infinity, Infinity]` →
       `random.uniform(inf, inf)` 回 **nan**（`inf + (inf-inf)*x`）→
       `time.sleep(nan)` 丟 `ValueError`，整個角色迴圈在第一次圖間等待就炸掉。
       Dorossi 的 watchdog 上限吃到 `inf` 則等於**關掉** watchdog——那正是
       `_coerce_clamped_num` 的 docstring 說絕不可以發生的事。
    2. **大到轉不成 float 的 int。** JSON 的整數沒有上限，`json.loads` 會給一個
       任意精度的 Python int，而 `float(10**400)` 與 `math.isfinite(10**400)`
       都會丟 `OverflowError`。兩個載入器的 docstring 都寫著「never raises」，
       實測卻會——`load_bot_config` 是在 bot import 時跑的，等於 bot 起不來。
       所以這裡先用**比較**（int 與 float 比大小不會溢位）擋掉，不要直接呼叫
       `math.isfinite`。

    `bool` 先排掉：它是 `int` 的子類，`True` 會一路變成 1。
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        # 只用比較，不呼叫 float()／math.isfinite()——那兩個對超大 int 會溢位。
        return -_FLOAT_MAX <= value <= _FLOAT_MAX
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _coerce_int(value, default: int, *, min_value: int = 0) -> int:
    if _is_finite_number(value) and isinstance(value, int) and value >= min_value:
        return value
    return default


def _coerce_positive_num(value, default: float) -> float:
    if _is_finite_number(value) and value > 0:
        return float(value)
    return default


def _coerce_nonneg_num(value, default: float) -> float:
    """Like `_coerce_positive_num` but allows 0 (used for thresholds where
    0 means 'disabled', e.g. min_free_disk_gb)."""
    if _is_finite_number(value) and value >= 0:
        return float(value)
    return default


def _coerce_clamped_num(value, default: float, *, min_value: float) -> float:
    """Coerce a positive number with a hard floor. Used for the Dorossi hard
    watchdog ceilings, where the limit MUST stay meaningful — a value of 0 /
    negative / absurdly small would effectively disable the protection (the
    hard tier exists precisely so a hung tool can't wedge the queue), so we
    clamp up to `min_value` rather than accepting it. A non-number / bool falls
    back to the default.

    **`inf` 走 default 而不是「照收」**：`inf >= min_value` 為真，照收等於把上限設成
    無限大、watchdog 形同關閉——正好是這個函式要防的事。"""
    if not _is_finite_number(value):
        return default
    return float(value) if value >= min_value else min_value


def _coerce_str(value, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default


def _coerce_optional_str(value, default: str) -> str:
    """字串欄位，但**空字串是有意義的合法值**，不是「沒填」。

    `api_contact` 專用：它的預設就是 `""`，語意是「不附聯絡方式」（見那個鍵的
    註解）。用 `_coerce_str` 的話 `""` 會被判成拒絕——**行為上完全相同**（拒絕後
    退回的預設剛好也是 `""`），但接上逐鍵警告之後就變成一則假警告：正式的
    `bot_config.json` 現在就寫著 `"api_contact": ""`，每次載入都要被指控一次設定
    沒生效。2026-09-09 加警告時當場量到這件事。

    修在 coercer 而不是在警告那一層加例外，是因為問題本來就在這裡：`""` 對這個鍵
    合法，說它「不合用」是錯的。一個會亂叫的守門遲早被人關掉——本專案已經為了同
    一個理由收窄過 `test_language` 與 `test_text_encoding` 的掃描範圍。

    更一般的那條規則由 `test_config_numbers` 釘住：**任何鍵的預設值本身都不得被
    它自己的 coercer 拒絕**，否則就會產生這種「照著預設寫也挨罵」的假警告。
    """
    if isinstance(value, str):
        return value
    return default


def _coerce_time_str(value, default: str) -> str:
    """`daily_health_report.time` 的 `HH:MM`。

    抽成 coercer**只是為了讓它也走 `_take`**（原本是內聯的 if，所以打錯時完全沒
    聲音）。驗證強度一個字都沒改：非字串／全空白 → 退回預設，其餘照原樣 strip。
    實際的時刻解析在 bot 那一側，這裡不重複驗一次。
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _coerce_help_lang(value, default: str) -> str:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _VALID_HELP_LANGS:
            return low
    return default


def _coerce_dorossi_backend(value, default: str) -> str:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _VALID_DOROSSI_BACKENDS:
            return low
    return default


def _coerce_dorossi_cc_tools(value, default: str) -> str:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _VALID_DOROSSI_CC_TOOLS:
            return low
    return default


def _coerce_gui_control(raw, default: dict) -> dict:
    """`gui_control` block。`launch_whitelist: list[str]` 是允許 launch
    的程式名稱／路徑；`launch_aliases: dict[str, str]` 是 `!launch
    <key>` 的 key → target 對應（target 可以是 path 或 URI）。
    每個欄位都用 per-entry validation：rejected entry 不會把整段清空。"""
    if not isinstance(raw, dict):
        return {
            "launch_whitelist": list(default["launch_whitelist"]),
            "launch_aliases": dict(default["launch_aliases"]),
        }
    wl = raw.get("launch_whitelist")
    out_wl: list[str] = []
    if isinstance(wl, list):
        for entry in wl:
            if isinstance(entry, str) and entry.strip():
                out_wl.append(entry.strip())
        if len(out_wl) != len(wl):
            # 只說幾筆、**不印內容**：這個清單裝的是主機上的執行檔路徑。
            _warn_once(f"_bot_config: `gui_control.launch_whitelist` 有 "
                       f"{len(wl) - len(out_wl)} 筆不是可用的字串，已跳過那幾筆、"
                       f"採用其餘 {len(out_wl)} 筆")
    elif wl is not None:
        _warn_once(f"_bot_config: `gui_control.launch_whitelist` 不是一個清單"
                   f"（收到 {_shown(wl)}），已忽略、沿用空清單")
    al = raw.get("launch_aliases")
    out_al: dict[str, str] = {}
    if isinstance(al, dict):
        for key, value in al.items():
            if (isinstance(key, str) and key.strip()
                    and isinstance(value, str) and value.strip()
                    and not key.startswith("_")):  # 讓 _comment 之類的 key 自動跳過
                out_al[key.strip()] = value.strip()
        # `_comment` 之類的底線開頭 key 是**刻意**跳過的，不算「被丟掉」——把它們
        # 算進去會讓每一個有註解的設定檔都收到一則假警告，那正是「會亂叫的守門」。
        droppable = sum(1 for k in al if not (isinstance(k, str)
                                              and k.startswith("_")))
        if len(out_al) != droppable:
            _warn_once(f"_bot_config: `gui_control.launch_aliases` 有 "
                       f"{droppable - len(out_al)} 筆的 key／target 不是可用的"
                       f"字串，已跳過那幾筆、採用其餘 {len(out_al)} 筆")
    elif al is not None:
        _warn_once(f"_bot_config: `gui_control.launch_aliases` 不是一個對應表"
                   f"（收到 {_shown(al)}），已忽略、沿用空對應表")
    return {"launch_whitelist": out_wl, "launch_aliases": out_al}


def _coerce_int_list(value) -> list[int]:
    out: list[int] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and item >= 0:
            out.append(item)
        elif isinstance(item, str):
            try:
                n = int(item.strip())
            except ValueError:
                continue
            if n >= 0:
                out.append(n)
    return out


def _coerce_user_roles(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return {
            "admin_user_ids": list(default["admin_user_ids"]),
            "operator_user_ids": list(default["operator_user_ids"]),
            "viewer_user_ids": list(default["viewer_user_ids"]),
        }
    return {
        key: _take_int_list(raw, key, path="user_roles.")
        for key in ("admin_user_ids", "operator_user_ids", "viewer_user_ids")
    }


def _coerce_daily_health_report(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(default)
    return _take_section(raw, default, _DAILY_HEALTH_COERCERS,
                         path="daily_health_report.")


def _coerce_dashboard(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(default)
    return _take_section(raw, default, _DASHBOARD_COERCERS, path="dashboard.")


def _coerce_dorossi_model_check(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(default)
    return _take_section(raw, default, _DOROSSI_MODEL_CHECK_COERCERS,
                         path="dorossi_model_check.")


def _coerce_supervisor(raw, default: dict) -> dict:
    if not isinstance(raw, dict):
        return dict(default)
    return _take_section(raw, default, _SUPERVISOR_COERCERS,
                         path="webrunner_supervisor.")


# key -> (coercion 函式, 額外關鍵字)。**每一個扁平鍵都走同一條路**，不再每個鍵手寫
# 一行——新增設定時漏掉警告因此變成不可能，而不是「記得要加」。
# `test_config_numbers` 另外釘住這張表 ＋ `_SECTION_KEYS` ＋ `_INT_LIST_KEYS` 三者
# 剛好不重不漏地蓋滿 `_DEFAULT_BOT_CONFIG`（兩個方向都釘）。
_COERCERS: dict = {
    "channel_id": (_coerce_int, {"min_value": 0}),
    "owner_user_id": (_coerce_int, {"min_value": 0}),
    # `""` 是這個鍵有意義的「不附聯絡方式」，所以不是 `_coerce_str`（見那支的
    # docstring：正式設定檔就寫著 `""`，用 `_coerce_str` 會每次載入都假警告一次）。
    "api_contact": (_coerce_optional_str, {}),
    "target_presence_username": (_coerce_optional_str, {}),
    "default_help_lang": (_coerce_help_lang, {}),
    "presence_probe_interval_sec": (_coerce_positive_num, {}),
    "event_poll_seconds": (_coerce_positive_num, {}),
    "alert_user_id": (_coerce_int, {"min_value": 0}),
    "min_free_disk_gb": (_coerce_nonneg_num, {}),
    "keep_bot_awake": (_coerce_bool, {}),
    "dorossi_backend": (_coerce_dorossi_backend, {}),
    "dorossi_cc_tools": (_coerce_dorossi_cc_tools, {}),
    "dorossi_cc_hard_limit_off_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_CC_HARD_LIMIT_FLOOR_SEC}),
    "dorossi_cc_hard_limit_full_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_CC_HARD_LIMIT_FLOOR_SEC}),
    "dorossi_cc_idle_limit_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_CC_HARD_LIMIT_FLOOR_SEC}),
    "dorossi_loop_silence_limit_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_CC_HARD_LIMIT_FLOOR_SEC}),
    # 用量上限等待策略。兩個秒數都 clamp 到 ≥ DOROSSI_USAGE_WAIT_FLOOR_SEC（60s）：
    # 這是空轉防護，不能被設定檔關掉——用量上限的判定字樣比對得很寬，誤判時若允許
    # 0 秒等待就會變成熱迴圈。max_consecutive 允許 0（＝不設限）。
    "dorossi_usage_wait_fallback_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_USAGE_WAIT_FLOOR_SEC}),
    "dorossi_usage_wait_max_sec": (
        _coerce_clamped_num, {"min_value": DOROSSI_USAGE_WAIT_FLOOR_SEC}),
    "dorossi_transient_max_consecutive": (_coerce_int, {"min_value": 0}),
    "dorossi_error_retry_max": (_coerce_int, {"min_value": 0}),
    "dorossi_silence_retry_max": (_coerce_int, {"min_value": 0}),
    "dorossi_usage_wait_max_consecutive": (_coerce_int, {"min_value": 0}),
    # 跨重啟自動接續的兩個閥；兩者都允許 0（＝關閉／不設限），所以是 min 0 而不是
    # clamp 到某個下限。年齡用 nonneg_num（非有限值會被打回預設）。
    "dorossi_loop_autoresume_max_age_sec": (_coerce_nonneg_num, {}),
    "dorossi_loop_autoresume_max_tries": (_coerce_int, {"min_value": 0}),
    # 0 ＝停用（與 min_free_disk_gb 同樣用 _coerce_nonneg_num，允許 0）。
    "dorossi_max_budget_usd": (_coerce_nonneg_num, {}),
    # 週期性壓縮門檻；各自 0 ＝停用該條（皆允許 0，用 nonneg / int min 0）。
    "dorossi_loop_compact_every_rounds": (_coerce_int, {"min_value": 0}),
    "dorossi_loop_compact_cost_usd": (_coerce_nonneg_num, {}),
    # 脈絡過大就壓縮的 token 門檻（單輪＋自走共用）；0 ＝停用（允許 0，min 0）。
    "dorossi_compact_context_tokens": (_coerce_int, {"min_value": 0}),
    # 0 ＝停用（明顯過舊才自動重置，保守）。
    "dorossi_session_max_age_days": (_coerce_nonneg_num, {}),
    "dorossi_api_history_max_msgs": (_coerce_int, {"min_value": 0}),
    "dorossi_self_judge_enabled": (_coerce_bool, {}),
    # 並行後端回合數上限；clamp 到 ≥1（號誌至少要能放行一個回合）。
    "dorossi_cc_max_parallel": (_coerce_int, {"min_value": 1}),
    # 自走迴圈並行數操作閥；0 ＝不設限（允許 0，min 0）。非花費上限。
    "dorossi_max_parallel_loops": (_coerce_int, {"min_value": 0}),
}

# 巢狀區段的欄位表，形狀與 `_COERCERS` 相同。做成資料是為了同一個理由：
# `test_config_numbers` 釘住每一張表要蓋滿它那份 `_DEFAULT_*`，所以新增一個區段
# 欄位卻忘了掛 coercion（＝完全不驗證、也不出聲）會直接變紅。
_SUPERVISOR_COERCERS: dict = {
    "fallback_window_sec": (_coerce_int, {}),
    "respawn_backoff_min_sec": (_coerce_positive_num, {}),
    "respawn_backoff_max_sec": (_coerce_positive_num, {}),
    "healthy_threshold_sec": (_coerce_positive_num, {}),
    "rapid_fail_threshold_sec": (_coerce_positive_num, {}),
    "rapid_fail_giveup_count": (_coerce_int, {"min_value": 1}),
    "zero_progress_giveup_count": (_coerce_int, {"min_value": 1}),
    "oneshot_retry_giveup_count": (_coerce_int, {"min_value": 1}),
    # `_coerce_positive_num` 保證 > 0，也就順手滿足了 `restart_backoff` 的
    # `0 < minimum` 那一半前提（另一半 max < min 在呼叫點夾）。
    "oneshot_retry_backoff_min_sec": (_coerce_positive_num, {}),
    "oneshot_retry_backoff_max_sec": (_coerce_positive_num, {}),
}

_DAILY_HEALTH_COERCERS: dict = {
    "enabled": (_coerce_bool, {}),
    "channel_id": (_coerce_int, {"min_value": 0}),
    "time": (_coerce_time_str, {}),
}

_DASHBOARD_COERCERS: dict = {
    "host": (_coerce_str, {}),
    "port": (_coerce_int, {"min_value": 1}),
}

_DOROSSI_MODEL_CHECK_COERCERS: dict = {
    "enabled": (_coerce_bool, {}),
    # 正數（`_coerce_positive_num`）＝不能設成 0 或負數。0 會讓檢查每分鐘跑一次，
    # 那不是「關掉」而是「一直跑」——關掉請用 `enabled`。
    "interval_hours": (_coerce_positive_num, {}),
    "announce_channel_id": (_coerce_int, {"min_value": 0}),
}

# 巢狀區段（有自己的 coercer）與 int-list 鍵（`_coerce_int_list` 沒有 `default`
# 參數，sentinel 那一招用不上）。兩者都不在 `_COERCERS` 裡，但都必須被涵蓋——
# 這兩個 tuple 就是「已經想過了」的紀錄，測試拿它們跟 `_DEFAULT_BOT_CONFIG` 對帳。
_SECTION_KEYS = ("webrunner_supervisor", "gui_control", "user_roles",
                 "daily_health_report", "dashboard", "dorossi_model_check")
_INT_LIST_KEYS = ("path_reveal_channel_ids",)


def _take(raw: dict, defaults: dict, key: str, coerce, kwargs: dict | None = None,
          *, path: str = ""):
    """讀 `raw[key]`、套用它的 coercion，**值被丟掉或被下限改掉時說一聲**。

    「這個鍵根本沒寫」是正常情況，必須完全安靜——所以先看 `key in raw`，不能用
    `raw.get(key)` 之後再判斷（那樣寫成 `null` 的鍵會跟沒寫的鍵長得一樣，而前者
    是使用者真的打錯了）。
    """
    if key not in raw:
        return defaults[key]
    value = raw[key]
    kwargs = kwargs or {}
    got = coerce(value, _REJECTED, **kwargs)
    label = path + key
    if got is _REJECTED:
        _warn_once(f"_bot_config: `{label}` 的值不合用（收到 {_shown(value)}），"
                   f"已忽略、沿用預設 {defaults[key]!r}")
        return defaults[key]
    if coerce is _coerce_clamped_num:
        # **「夾」不是「拒絕」，訊息要分得開。** 使用者明確寫了 10、實際跑 60，那
        # 不是正規化，是他的意圖被改掉了——所以一樣要出聲，但講的是另一件事：值
        # 是合法的，只是低於一個不開放關掉的保護下限。
        #
        # 判定**不比對結果與輸入**：`float(2**53 + 1) != 2**53 + 1`，那會把一個
        # 超大但合法的整數誤報成被夾。改成再問同一個 coercer 一次、只把下限拿掉
        # （`-_FLOAT_MAX` 而不是 `-inf`：`_is_finite_number` 已保證值落在這個範圍
        # 內，而且這個模組刻意不讓 inf 進到任何比較裡）——答案不一樣，就代表下限
        # 起了作用。用 coercer 自己當判準，這條就不會跟它的實作漂移。
        without_floor = coerce(value, _REJECTED,
                               **{**kwargs, "min_value": -_FLOAT_MAX})
        if got != without_floor:
            _warn_once(f"_bot_config: `{label}` 收到 {_shown(value)}，低於下限，"
                       f"已提高為 {got!r}——這是保護（不能用設定關掉），"
                       f"不是設定沒生效")
    return got


def _take_section(raw: dict, defaults: dict, table: dict, *, path: str) -> dict:
    """巢狀區段的逐欄位版本。

    從 `dict(defaults)` 出發而不是只組表裡那幾個鍵：形狀永遠與預設一致，就算哪天
    表落後於 `_DEFAULT_*` 也不會讓呼叫端吃到 `KeyError`（那個守門在測試裡，這裡是
    第二層）。
    """
    out = dict(defaults)
    for key, (coerce, kwargs) in table.items():
        out[key] = _take(raw, defaults, key, coerce, kwargs, path=path)
    return out


def _take_int_list(raw: dict, key: str, *, path: str = "") -> list[int]:
    """`path_reveal_channel_ids` 與三份 `*_user_ids` 走這條。

    `_coerce_int_list` 沒有 `default` 參數（它的「預設」永遠是空清單），所以上面
    那招 sentinel 用不上。改用兩個直接的判定，各對應一種靜默失敗：

    * **整個鍵型別錯** → 空清單。方向是安全的（fail-closed：退回「只有私訊看得到
      完整路徑」／「角色系統未設定」），但使用者以為自己開了那個權限，而 fail-closed
      的失敗正是最不會有人發現的那一種。
    * **部分項目被丟掉** → 只說**幾筆**，不列內容：這幾個鍵裝的是使用者／頻道 ID，
      逐筆列出只是雜訊。
    """
    if key not in raw:
        return []
    value = raw[key]
    out = _coerce_int_list(value)
    label = path + key
    if not isinstance(value, list):
        _warn_once(f"_bot_config: `{label}` 不是一個清單（收到 {_shown(value)}），"
                   f"已忽略、沿用空清單")
    elif len(out) != len(value):
        _warn_once(f"_bot_config: `{label}` 有 {len(value) - len(out)} 筆不是可用的"
                   f" ID，已跳過那幾筆、採用其餘 {len(out)} 筆")
    return out


def _section(raw: dict, name: str):
    """取出巢狀區段交給它的 coercer。

    鍵沒寫 → `None`（正常情況，一個字都不印）；寫了但**不是一個區段** → 說一聲，
    並一樣回 `None` 讓那個 coercer 走它既有的「整段用預設」分支。行為與原本的
    `raw.get(name)` 完全相同（非 dict 一樣落到 `not isinstance(raw, dict)`），
    差別只在有沒有出聲。
    """
    if name not in raw:
        return None
    value = raw[name]
    if isinstance(value, dict):
        return value
    _warn_once(f"_bot_config: `{name}` 不是一個設定區段（收到 {_shown(value)}），"
               f"整段已忽略、沿用預設")
    return None


def _fallback_bot_config() -> dict:
    """`bot_config.json` 讀不到／解不開時回傳的整份預設設定。

    **形狀必須與正常合併路徑一模一樣。** 少一個鍵，呼叫端就會在設定檔剛壞掉的那
    一刻吃到 `KeyError`——而那正是最不該再壞第二次的時機（設定檔壞掉時 bot 還是
    要起得來，這整條退路存在的理由就是這個）。

    原本這段字面值在三個 `except` 分支各抄了一份。加一個巢狀預設鍵要記得改三處，
    抄漏一處**不會有任何測試變紅**，而且只在設定檔壞掉時才走得到，平常永遠測不出來。

    用 `deepcopy` 而不是原本的逐鍵淺拷貝，是因為 `dict(_DEFAULT_USER_ROLES)` 只複製
    外層：三份 id 清單仍與模組常數是**同一個物件**，呼叫端只要 append 一次就永久
    污染了預設值。這一條不是理論——`_roles_configured()` 正是用「三份清單都空」判定
    角色系統沒設定，污染它等於讓權限閘門在下一次載入時憑空變成「已設定」。
    """
    return copy.deepcopy(_DEFAULT_BOT_CONFIG)


def load_bot_config() -> dict:
    """Return a fully-populated bot config dict, using defaults for any
    missing / malformed key. Always succeeds; never raises."""
    raw: dict = {}
    try:
        text = BOT_CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _fallback_bot_config()
        # `UnicodeDecodeError` 是 `ValueError` 的子類別、**不是** `OSError`：
        # 一個被別的編輯器另存成 Big5 的設定檔就會從這裡逸出，而本機 locale
        # 正是 cp950、檔案內容幾乎都含中文。不用 `errors="replace"` 靜靜吞掉——
        # 那會把亂碼當成有效值用下去，比退回預設糟。
    except (OSError, UnicodeDecodeError) as error:
        print(f"_bot_config: read failed: {error!r}", file=sys.stderr)
        return _fallback_bot_config()
    try:
        parsed = json.loads(text or "{}")
        if isinstance(parsed, dict):
            raw = parsed
    except json.JSONDecodeError as error:
        print(f"_bot_config: parse failed: {error!r}", file=sys.stderr)
        return _fallback_bot_config()

    # 從預設出發、逐鍵覆寫，形狀因此永遠完整（呼叫端不會在設定檔剛壞掉的那一刻
    # 吃到 `KeyError`）。**下面三段必須不重不漏地蓋滿 `_DEFAULT_BOT_CONFIG`**：
    # 漏掉一個鍵不只是「那個鍵不驗證」，還會讓 `cfg` 留著淺拷貝來的**同一個**
    # 模組常數容器——`_fallback_bot_config` 的 docstring 記著那個缺陷的代價
    # （呼叫端 append 一次就永久污染預設值，權限閘門會憑空變成「已設定」）。
    # `test_config_numbers` 兩個方向都釘住這件事。
    _warn_unknown_keys(raw, _DEFAULT_BOT_CONFIG, source="_bot_config")

    cfg = dict(_DEFAULT_BOT_CONFIG)
    for key, (coerce, kwargs) in _COERCERS.items():
        cfg[key] = _take(raw, _DEFAULT_BOT_CONFIG, key, coerce, kwargs)
    # 頻道 ID 清單；壞值逐筆丟掉，整個鍵缺席／型別錯 → 空清單，也就是 fail-closed
    # 回到「只有私訊看得到完整路徑」。
    for key in _INT_LIST_KEYS:
        cfg[key] = _take_int_list(raw, key)
    # 巢狀區段。`_section()` 只多做一件事：整段型別錯時說一聲（原本 `raw.get(name)`
    # 把一個寫壞的區段整段吞掉，連一個字都沒有）。
    cfg["webrunner_supervisor"] = _coerce_supervisor(
        _section(raw, "webrunner_supervisor"), _DEFAULT_SUPERVISOR)
    cfg["gui_control"] = _coerce_gui_control(
        _section(raw, "gui_control"), _DEFAULT_GUI_CONTROL)
    cfg["user_roles"] = _coerce_user_roles(
        _section(raw, "user_roles"), _DEFAULT_USER_ROLES)
    cfg["daily_health_report"] = _coerce_daily_health_report(
        _section(raw, "daily_health_report"), _DEFAULT_DAILY_HEALTH_REPORT)
    cfg["dashboard"] = _coerce_dashboard(
        _section(raw, "dashboard"), _DEFAULT_DASHBOARD)
    cfg["dorossi_model_check"] = _coerce_dorossi_model_check(
        _section(raw, "dorossi_model_check"), _DEFAULT_DOROSSI_MODEL_CHECK)
    return cfg
