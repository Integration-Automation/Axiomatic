"""共用的純函式層（driver-agnostic）給兩支 webrunner 變體。

`webrunner_novelai.py`（Selenium 變體）與 `webrunner_je_only.py`
（je_web_runner 變體）原本逐函式同構，只差「DOM／driver 怎麼被呼叫」。本模組
收斂兩變體「**根本不碰 driver**」的純函式／純 I/O（todo 檔讀寫、prompt 文字
處理、計時、retry、輸出資料夾配置、事件輸出）與其所需常數，讓兩變體
`from _webrunner_shared import ...` 共用同一份，避免日後失同步。

模組邊界（硬規則）：
- 本模組必須 **driver-agnostic**：不可 import selenium / je_web_runner，也不
  建立 / 持有任何 driver。所有需要 driver 的東西留在各變體（P6 後續的 C 系列
  會以 adapter 注入）。
- 屬於 CLAUDE.md 允許的「被動共用模組」（地位同 `_queue_consume` /
  `_run_progress`）：兩變體都 import 它，但兩變體仍**不互相** import、也不
  import `discord_bot`。
- 只用 stdlib。

這是 P6 重構的第一刀（C1）：純函式抽取、零 adapter。Chrome 生命週期家族
（snapshot / orphan kill / lock 清理）因與 verify 模式的 module 全域
（`CHROME_PROFILE_SNAPSHOT` / `_SUPPRESS_ORPHAN_SWEEP`）糾纏，刻意留在各變體，
待後續 commit 連同 verify 接線一起搬。
"""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import traceback
from collections.abc import Container
from pathlib import Path, PureWindowsPath

from _batch_config import load_batch_config  # permitted passive shared loader
import _run_progress  # permitted passive shared module (resume checkpoint)
import _queue_consume  # permitted passive shared module (dynamic-consume decisions)
import _code_fingerprint  # permitted passive shared module (啟動時的程式碼指紋)
# rc 契約與兩支監督者共用一份（`_supervisor` 同為被動共用模組、純 stdlib）。
from _supervisor import (  # noqa: E402
    RC_GENERATION_BLOCKED,
    RC_SETUP_INCOMPLETE,
    RC_ZERO_PROGRESS,
    trim_log,
)
# `pair_todos` 的單一來源在 _queue_consume（消費語彙的自然家）；這裡 re-export，
# 讓本模組與兩變體（以及 ws.pair_todos 測試）沿用同一份，避免失同步（P7）。
from _queue_consume import pair_todos  # noqa: F401  (re-exported single source)
# `CLAUDE.md` 允許的被動共用模組。只用它的 `_pid_alive`——不要再寫第四份
# （`test_pid_liveness` 對副本數量有對帳），而且要的正是它「判不出來回 True」
# 的那個方向，見 `claim_liveness_signal`。
import _chrome_slot  # noqa: E402

# 本檔位於 `<repo>/axiomatic/_webrunner_shared.py`，`.parent.parent` 即 repo
# 根；與兩變體各自的 PROJECT_ROOT 算法相同、值相等（且都不會被重新賦值）。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# After this many in-a-row image failures `generate_loop` emits a
# `consecutive_failures` alert event (does NOT abort — the hard abort is the
# batch_config `consecutive_fail_abort`). Used only by generate_loop (C5).
CONSECUTIVE_FAIL_ALERT = 5
# `wait_for_new_image` 等圖的時候，每隔這麼久回頭問一次「畫面上是不是跳了
# 購買／方案對話框」。額度用完的時候圖永遠不會來，所以這一題原本要等整個
# 180 秒 timeout 燒完、回到 `generate_one_image` 才會問——實測 2026-08-24～
# 08-27 的 log：65 次被擋，每一次都空等 180~182 秒，合計 3 小時 16 分，而且
# 使用者的通知也跟著慢 3 分鐘。
BLOCK_PROBE_INTERVAL_SEC = 2.0
# ……但**按下 Generate 之後的前這麼久不問**。理由是這個探測會誤殺一種情況：
# 對話框已經在畫面上、而站方同時還在算一張真的圖。同一份 log 的 410 次成功
# 生成：p50=5 秒、p90=9 秒，尾巴拉到 80 秒。抓 p90 當寬限期——真的在算的圖
# 幾乎都會在這之前落地，而落地就會在迴圈裡先被收下（圖片檢查排在探測之前，
# 「有圖為大」的既有優先順序不變）。誤殺最壞的後果也只是那張圖等額度回補後
# 重產一次，不會產生壞資料。順帶：一般 5 秒就好的生成根本不會觸發探測，
# happy path 一次 round-trip 都沒有多花。
BLOCK_PROBE_GRACE_SEC = 10.0
EVENTS_FILE = PROJECT_ROOT / "events.ndjson"
# 「有批次在跑」的長期訊號。三個寫入端：兩支啟動器（父行程），以及沒有父行程
# 時的 webrunner 自己（`claim_liveness_signal`）。跨行程檔案 → 原子寫入。
WEBRUNNER_PID_FILE = PROJECT_ROOT / "webrunner.pid"
OUTPUT_ROOT = PROJECT_ROOT / "output"
# Bot 寫 DOM-introspection 請求到這個檔，webrunner 在 iteration 邊界 poll。
DOM_REQUEST_FILE = PROJECT_ROOT / "dom_request.json"
# 單張生成請求檔 + one-shot 輸出根（serve_single_image_request 用，C4 搬入）。
# one-shot 圖一律存進 output/_oneshot/<request_id>/，不碰 resume checkpoint /
# 佇列 pop / 編號。**兩支變體不再各自抄一份這個常數**：pass 3 之後「我這個行程
# 是不是單圖伺服器」由 argv 旗標宣告（見下面那一段），變體的 main() 不必再碰
# 這個檔，只有這裡與 bot 各持有一份。
SINGLE_IMAGE_REQUEST_FILE = PROJECT_ROOT / "single_image_request.json"
WEBRUNNER_PAUSE_FILE = PROJECT_ROOT / "webrunner.pause"
# Queue / fallback files (read_queues + run_batch pop, C6). Newline-only,
# NBSP-normalised, trailing-newline-iff-nonempty contract lives in CLAUDE.md.
# `AUTH_FILE` stays per-variant (main shell reads credentials before boot).
TODO_PROMPT_FILE = PROJECT_ROOT / "todo_prompt.md"
TODO_FILE_1 = PROJECT_ROOT / "todo_character1.md"
TODO_FILE_2 = PROJECT_ROOT / "todo_character2.md"
TODO_UNDESIRED_FILE = PROJECT_ROOT / "todo_undesired.md"
PROMPT_FILE = PROJECT_ROOT / "prompt.md"               # main-prompt fallback
CHARACTER1_FALLBACK_FILE = PROJECT_ROOT / "character1.md"  # char1 fallback
CHARACTER2_FALLBACK_FILE = PROJECT_ROOT / "character2.md"  # char2 fallback
UNDESIRED_FILE = PROJECT_ROOT / "undesired.md"         # undesired fallback
SINGLE_IMAGE_OUTPUT_ROOT = OUTPUT_ROOT / "_oneshot"

# ---------- 這一輪是「批次」還是「單圖伺服器」：用宣告的，不用推論的 ----------
# **2026-06-27 的正式事故**，證據在 `webrunner.log` 第 1–125 行：一次 `/run` 帶著
# 57 組佇列配對進來（第 2 行 `todo quadruples: 57`，後面逐筆列出 57 個角色），而第
# 78 行印出來的卻是 `startup single-image request detected; serving one-shot`——磁碟
# 上剛好留著一個單圖請求檔，於是 `run_batch` 把整輪批次換成了單圖伺服器：服務兩張
# 一次性圖（第 122 行的輸出檔名 `oneshot_20260627_082159.png` 就是那個日期），第
# 124 行閒置收工、`return 0`。整段窗裡**一行批次生成都沒有**，也沒有發 `todo_done`。
# 而 rc=0 在兩支監督者眼裡就是「乾淨跑完」，所以沒有人重生，那 57 組配對整批沒跑。
#
# 病灶是**模式靠推論**：`SINGLE_IMAGE_REQUEST_FILE.exists()` 回答的是「磁碟上有沒有
# 人排了一張單圖」，卻被拿去回答另一個完全不同的問題「我這個行程是不是一個單圖
# 伺服器」。兩個問題的答案只有在 bot 剛好趁 idle spawn 我們的時候才一致；只要一個
# 請求檔跟一輪 `/run` 在時間上重疊，答案就相反，而且失敗方向是最壞的那個（安靜地
# 跳過整條佇列，外加一個假的成功 rc）。
#
# 修法是把模式**宣告**出來。閘門（pass 3，2026-09-12）現在就是這條規則：
#     **旗標決定模式；檔案決定「已經在跑的批次下一個要服務什麼」。**
# 也就是 `SINGLE_IMAGE_REQUEST_FILE` 保留它原本的 in-band 角色（批次在配對邊界與
# 圖與圖之間 poll 它、順手服務掉），但它**不再**決定這個行程的身分。
#
# **為什麼是 argv 而不是環境變數。** `discord_bot._spawn_webrunner` 沒有傳 `env=`，
# 子行程直接繼承父行程的環境。所以一個為了單圖而設、之後沒有清掉的環境變數，會把
# 後面**每一輪**批次都轉成伺服器——同一類缺陷原封不動，只是從磁碟搬到行程狀態，而且
# 更難查：請求檔看得到也刪得掉，環境變數在 log 與工作管理員裡都看不到。argv 反過來
# 是每一次 spawn 都得自己重講一次，講錯了也會原樣印在啟動那一行裡。
RUN_MODE_BATCH = "batch"
RUN_MODE_SINGLE_IMAGE_SERVER = "single-image-server"
# 兩支變體的 `main()` 用 `parse_run_mode(sys.argv)` 認這個旗標；把它接上 argv 的
# 是 bot 的 `_spawn_webrunner(..., single_image_server=True)`。字面值只有這一份，
# bot 是 import 過去的——抄第二份的話改名之後 spawn 照樣成功，只是那個行程不再
# 知道自己是誰（就是上面那次事故的形狀）。
SINGLE_IMAGE_SERVER_FLAG = "--single-image-server"

# Chrome's renderer-crash interstitial title markers (URL `chrome-error://` is
# the definitive marker; titles vary by locale / crash flavour). Kept here so
# both webrunner variants share ONE list — keep in sync with any bot mirror.
_CHROME_CRASH_TITLE_MARKERS = (
    "aw, snap", "aw,snap", "he's dead", "he's just resting", "out of memory",
)

# ---------- dead browsing context（chrome 中途被截斷）------------------------
# 「視窗／分頁／session 已經沒了」的指紋，與 hot-path reader 已經用 None /
# False 吸收掉的**暫時性** chromedriver 卡頓分開。兩者需要相反的反應：卡頓值得
# continue poll（Chrome 只是忙），但 session 沒了之後**每一次**後續呼叫都會丟出
# 一模一樣的錯，poll 只是把整個重試預算燒光（實測：一次視窗被關掉要空轉到
# `consecutive_fail_abort` 觸發，約 30 分鐘）而瀏覽器始終是死的。
# 訊息 marker 以小寫子字串比對 `str(error)`；類別名比對整條 MRO，所以 selenium
# 的子類別也算。刻意用純字串而非 import selenium——本模組必須 driver-agnostic。
_SESSION_GONE_MESSAGE_MARKERS = (
    "no such window",                        # 視窗／分頁在執行中被關掉
    "target window already closed",          # 使用者實際回報的那一行
    "web view not found",                    # 它的 `from unknown error:` 第二行
    "invalid session id",                    # chromedriver 把 session 丟掉了
    "session deleted because of page crash",
    "not connected to devtools",             # devtools socket 斷了
    "chrome not reachable",                  # 瀏覽器行程整個不見
    "unable to connect to renderer",
    "tab crashed",
)
#
# **這份名單只有在對應的類別真的被 `port.TRANSPORT_ERRORS` 抓得到時才有作用。**
# 2026-09-09 之前 `MaxRetryError` / `NewConnectionError` 就是反例：它們寫在這裡，
# 但兩個變體的 tuple 只有 `(WebDriverException, ReadTimeoutError, OSError)`，而這
# 兩個類別的 MRO **不經過 `OSError`**，所以 hot path 的 `except` 收不到 →
# `_note_transport_error` 不會被呼叫 → 這份表永遠沒機會發言。正式環境的樣子是
# 09-07 那三次 `critical_error`：message 是生的 `MaxRetryError`，沒有
# `browser session gone during …` 前綴。
# `test_variant_parity.test_every_session_gone_name_is_actually_catchable` 現在
# 把「表」與「捕捉子」對拉起來——往這裡加名字，就要確認那個類別進得了 tuple。
_SESSION_GONE_EXC_NAMES = frozenset({
    "NoSuchWindowException",       # selenium：視窗／target 已關閉
    "InvalidSessionIdException",   # selenium：session 已刪除
    "NoSuchDriverException",       # selenium 4.x：driver 不見了
    # 以下是 urllib3 那一側。判準是**連線有沒有建立起來**：連不上／被拒／中途被
    # 斷掉，對一個 loopback 監聽器而言就是「那個埠上沒有東西在聽」＝
    # chromedriver.exe 已經結束。相對地 `ReadTimeoutError`（連上了、請求送出了、
    # 120 秒沒回話）代表 chromedriver 還活著，所以**刻意不列**——它是「卡頓」的
    # 正典，列進來會讓一次忙碌變成一次重生。
    "MaxRetryError",               # urllib3：連 chromedriver 的 socket 被拒
    "NewConnectionError",
    "ConnectTimeoutError",         # 連線階段逾時（NewConnectionError 的基底）
    "ProtocolError",               # 連線中途被對方斷掉（RemoteDisconnected）
    "ConnectionRefusedError",      # chromedriver 行程已死
})
# JS 探針：回傳這個字面值才算「一次完整的 driver round-trip 成功了」。
# 用**回傳值**（而不是「沒有丟例外」）當存活判準是刻意的——je 變體的 wrapper 把
# 每一個 driver 例外吞掉、一律回 None，在那條路徑上「沒丟例外」完全不代表瀏覽器
# 還活著。
_ALIVE_PROBE_TOKEN = "je-alive"
_ALIVE_PROBE_JS = f"return '{_ALIVE_PROBE_TOKEN}';"

# 共用的可見性判準。**不要再用 `offsetParent === null` 判對話框／吐司**。
#
# `offsetParent` 對 `position: fixed` 的元素**一律回 null**（規範如此），而 modal
# 遮罩與吐司幾乎都是 fixed——於是「畫面正中央那個擋住一切的對話框」在掃描裡讀作
# 「不存在」。失敗形態是**安靜的**：偵測回 None、關閉一個都沒點、診斷清單空白，
# 三層一起瞎掉，log 上只看得到「生成逾時」。
#
# 實證（2026-08-29，`verify_quota_dialog.py` 真 DOM）：把 `role="dialog"` 加上
# `position:fixed` 之後，Tier 1／Tier 2／dismiss／控制項清單**全部**回空，而同一
# 份 DOM 只要拿掉 fixed 就全部正常。
#
# 換成 `getClientRects()`：`display:none`（含祖先）回空陣列，fixed 的可見元素回
# 得到矩形。再補一次 computed style，順手把舊判準漏掉的 `visibility: hidden` 也
# 擋下來——那一種 `offsetParent` 是**非** null 的，等於以前會誤判成「有對話框」。
#
# 只套用在對話框／吐司這幾段。頁面上一般的按鈕、輸入框沿用 `offsetParent`：那些
# 元素不會是 fixed，而 `offsetParent` 便宜得多（本檔另有約五十處）。
#
# **兩個判準，用在不同層，因為兩種誤判的代價相反：**
# - `onScreen`（容器：對話框、吐司）另外要求「矩形與視窗有交集」。誤判成「有對話
#   框擋著」的代價是白等一小時，所以這一層寧可嚴。站方頁面裡常留著 opacity:0 或
#   移到畫面外的 fixed modal 殼，光看 `getClientRects()` 會全部算成可見。
# - `visible`（對話框**裡面**的控制項）只要求「有佈局且沒被藏起來」。這一層漏掉
#   一個真的 Cancel 才是更大的害處——長對話框裡捲到視窗外的按鈕仍然點得到。
_JS_VISIBLE = r"""
function visible(el) {
  if (!el) return false;
  if (el.getClientRects().length === 0) return false;   // display:none / 未佈局
  const st = getComputedStyle(el);
  if (st.visibility === 'hidden' || st.display === 'none') return false;
  return parseFloat(st.opacity) !== 0;
}
function onScreen(el) {
  if (!visible(el)) return false;
  const vw = window.innerWidth || 0, vh = window.innerHeight || 0;
  for (const r of el.getClientRects()) {
    if (r.width > 0 && r.height > 0
        && r.right > 0 && r.bottom > 0 && r.left < vw && r.top < vh) return true;
  }
  return false;
}
"""

# ---------- 站方擋住生成（購買／方案資訊）------------------------------------
# 額度用完時站方會跳出購買／訂閱資訊，生成從此不會成功。這**不是**可重試的失敗：
# 監督者重生一次瀏覽器只會看到同一個對話框，於是變成無限重生（每輪還重跑一次
# 登入 ＋ setup）。偵測到就要走「乾淨停止、通知使用者、不重生」這條路。
#
# 兩層偵測，刻意分開：
#   Tier 1（`_GENERATION_BLOCK_JS`）：對話框／吐司文字命中購買語意 → 立刻停。
#   Tier 2（`has_blocking_dialog`）：**不依賴任何字面**——連續失敗到 abort 門檻
#     時，若畫面上仍有可見的 modal 對話框擋著，就當成「需要人處理」而不是崩潰。
#     這層是給「站方改寫字面 / 換語言 / 換成沒讀過的擋法」的保險。
#
# 字面清單是**猜測起點**，不是真理：每次停止都會把對話框全文寫進 log（stderr，
# 不外流），照那份實際文字回來收斂這裡的 pattern。加 pattern 時一律用**片語**、
# 不要用單字——「subscription」「purchase」單獨出現在導覽列／頁尾很常見，單字比
# 對會把正常頁面誤判成擋住。
#
# ---- 文案基準（2026-09-09 對 `WEBRunner.log` 全檔量到的）--------------------
# **這份清單裡只有一條是照實際文字寫的，其餘十條都還是猜的。** log 涵蓋 08-24
# → 09-09、259 次被擋，`[blocked] dialog text:` 的**相異內容只有一種**：
#
#   「The paint's run dry. You need a subscription or to purchase Anlas to
#     continue. Compare and pick the right plan for you.」（撇號是 U+2019）
#
# 而這份清單開頭那條 `/not enough anlas/i` 所描述的舊文案
#
#   「Not enough Anlas. Purchasing more Anlas lets you keep generating at this
#     resolution.」
#
# 在同一份 log 裡出現 **0 次**。也就是說它從來沒有在這個站台上命中過——它跟其他
# 九條一樣是當初憑想像寫的句型。**留著是對的**（站方換回類似措辭時仍然接得住，
# 而且它零誤判），但不要把它當成「驗證過的」。
#
# 實際文字命中的只有 `/(purchase|buy) (more )?(anlas|credits)/i` **一條**，餘裕
# 是零：把那句「or to purchase Anlas」換掉，整份清單就從 1/11 掉到 0/11。實測三
# 種合理改寫（「requires a subscription」語序相反／「need an active plan」／貨幣
# 詞換成 tokens）在補這兩條之前**全部 0 命中**。
#
# 漏掉的代價是**中等、不是嚴重**：Tier 2（`has_blocking_dialog`）不依賴任何字面，
# 連續失敗到 abort 門檻時只要畫面上還有 modal 就會接住，所以最壞是多燒幾次重試。
#
# **反方向（誤判）比漏掉貴，所以這裡寧可保守。** Tier 1 命中在批次路徑上不是
# 「乾淨停止」——它走的是 `wait_for_quota_recovery`，而 `quota_wait_max_sec` 預設
# 0 ＝ 無上限，所以一次誤判就是**每小時醒來一次、永不結束的等待迴圈**，而且從
# 事件串流上看起來跟真的額度用完一模一樣（照樣發 `quota_blocked` /`quota_wait`）。
# 加 pattern 之前一定要對「正常頁面文字」量一次誤判，不要只量正面命中。
#
# 照這個判準**否決掉的**兩個候選，理由記在這裡免得下次有人再提一次：
#   - `/pick the right plan/i`：邊際貢獻是零（實測每一份會命中它的文案，上面那
#     兩條新的都已經命中），而它是唯一會咬到反面語料的候選——真正的方案比較頁就
#     寫著這句話。零收益 ＋ 有誤判風險。
#   - `/subscription or to (purchase|buy)/i`：貼著這一版的措辭寫，站方把 or 改成
#     and 就沒了；「需要訂閱」那條已經涵蓋它能接的每一個情境。
# Tier 1 的容器長度上限，Python 這側的鏡像（JS 裡是字面量——那段是 raw 字串，塞不
# 進 f-string 的插值）。只拿來把 log 裡的「餘裕」算出來；兩邊不一致由
# `test_only_tier1_gates_the_dialog_text_on_length` 直接從 JS 原始碼抽出來對照。
_TIER1_TEXT_CAP = 1200

_GENERATION_BLOCK_JS = _JS_VISIBLE + r"""
const SELECTOR = [
  '[role="dialog"]', '[role="alertdialog"]', '[aria-modal="true"]',
  '[class*="modal"]', '[class*="Modal"]',
  '[class*="dialog"]', '[class*="Dialog"]',
  '[role="alert"]', '[aria-live="assertive"]', '[aria-live="polite"]',
  '[class*="toast"]', '[class*="Toast"]'
].join(',');
const PATTERNS = [
  /not enough anlas/i,
  /insufficient (anlas|credits|funds|balance)/i,
  /out of (anlas|credits|generations)/i,
  /no (anlas|credits) (left|remaining)/i,
  /(purchase|buy) (more )?(anlas|credits)/i,
  /(subscribe|subscription) (is )?(required|needed|to continue)/i,
  // ↓ 2026-09-09 照 log 裡的**實際文字**收斂進來的兩條，見下面「文案基準」。
  // 這一條補的是上一條的語序缺陷：上一條要求 subscription **後面緊接**
  // required/needed/to continue，而站方寫的是「You need a subscription **or**
  // to…」，於是接不到；「Generating **requires a** subscription」這種反向語序
  // 也一樣接不到。判準改成「需要 ←→ 訂閱」的語意配對，兩種語序都涵蓋。
  /(need|needs|requires?|required) (a |an )?(subscription|paid plan)/i,
  // 站方這一版付費牆的標題句。**這一條是站方專用的便宜保險，不是語意判準**——
  // 站方一改標題它就失效，那是預期內的，不要為了「讓它更耐改」而放寬成
  // /run dry/ 之類的單一片語。`.{0,3}` 是為了同時吃彎引號（U+2019，log 裡就是
  // 這個）、直引號與沒有引號三種寫法。
  /paint.{0,3}s run dry/i,
  /(subscription|plan) (has )?(expired|ended|lapsed|inactive)/i,
  /upgrade your (plan|subscription|account)/i,
  /renew your (subscription|plan)/i,
  /free (trial|generation|generations) [^.]{0,40}(ended|expired|over|used up)/i,
  /payment (is )?(required|failed)/i
];
for (const node of document.querySelectorAll(SELECTOR)) {
  if (!onScreen(node)) continue;
  const text = (node.innerText || node.textContent || '')
    .replace(/\s+/g, ' ').trim();
  // 太長的一定是包住半個頁面的 wrapper，不是對話框本體。**這道上限是 Tier 1
  // 專用的**：這裡的 SELECTOR 很寬（吐司、`class*=modal`、`aria-live`），需要它
  // 擋掉誤判，而 Tier 1 誤判＝無上限的等待迴圈。Tier 2 的 selector 已經收斂過，
  // 不要把這一行抄過去——理由寫在 `_BLOCKING_DIALOG_JS` 上面。
  // 數字要跟 Python 那側的 `_TIER1_TEXT_CAP` 一致（有守門測試對照）。
  if (!text || text.length > 1200) continue;
  for (const re of PATTERNS) {
    if (re.test(text)) {
      // `length` 是**全文**長度（`text` 已經被截成 400）。它存在的唯一理由是讓
      // 「離上面那道 1200 還有多少餘裕」變成量到的數字——站方多加一列方案就可能
      // 跨過去，而跨過去是靜默的（整段就當作沒有對話框）。
      return {text: text.slice(0, 400), pattern: String(re),
              length: text.length};
    }
  }
}
return null;
"""

# 關掉擋路的對話框。**這段的第一要務是「絕對不要點到會花錢的按鈕」**——
# 額度用完的對話框上，「購買」按鈕通常比「取消」更顯眼、更可能被寬鬆的選擇器
# 命中。所以規則是白名單制而非黑名單制：
#   1. 只挑**文字完全等於**已知關閉字樣的按鈕（`DISMISS`，完整比對、不是包含）。
#   2. 或 `aria-label` 含 close/dismiss **且**不含任何付款語意的按鈕。
#   3. 或「右上角的無字圖示鈕」——幾何 ＋ 語意空白，見下面 `byCorner` 的說明。
#   4. 都沒有 → 回 'escape'，由呼叫端送 Escape 鍵，一個元素都不交出去。
# 挑中就回 `{action, el}`，**這一段自己不按**——按的動作由 `_click_dismiss_target`
# 用 driver 送出（理由見下面第 2 段）。所以安全性質的判定點是**回傳值**：
# 「不會回傳會花錢的控制項」。
# `FORBIDDEN` 涵蓋的不只是 purchase/buy——`ok` / `yes` / `confirm` / `continue`
# 這種模稜兩可的字也一律禁點：在購買對話框上它們就是「確認扣款」。
#
# **第二層對話框上有一顆會造成真實損害的按鈕：`Unsubscribe`。** 這一層比第一層更
# 危險：第一層最壞是花錢，這一層最壞是**把訂閱退掉**，整條產線會停擺。
#
# **`get started` / `gift key` / `anlas` 是 2026-09-07 補進 `FORBIDDEN` 的縱深
# 防禦，不是因為它們現在擋不住。** 把兩層對話框上的每一顆拿去對 `FORBIDDEN` 掃過
# 一遍會發現保護厚薄非常不平均：
#
#   Unsubscribe / Update Payment Details / Subscribe / Pay As You Go  → 命中
#   Get Started / Anlas / Activate a Gift Key                         → **沒有命中**
#
# 後面那三顆原本**完全只靠「文字必須為空」那一條**擋住，連 `Unsubscribe` 那種
# 「剛好含 subscribe」的第二層都沒有。而那一條同時是規則 3 的功能條件，所以放寬它
# 的人不會意識到自己在動安全性質。補進來之後兩者才真的分開。
#
# **加這三個字不會影響關閉鈕的辨識**：關閉鈕要嘛文字為空（規則 3），要嘛是
# `close`／`cancel`／`×` 這類（規則 1 的 `DISMISS` 白名單）——沒有任何合理的關閉鈕
# 會叫 `Get Started` 或 `Anlas`。`anlas` 是站方的貨幣名，之後任何提到它的按鈕都值
# 得擋。寫成 `get[- ]?started` 是因為 `identityOf` 讀到的常是連字號形式
# （`data-testid="get-started"`）。
#
# **這一段只負責「挑」，不負責「按」。挑好的元素回給 Python，由 driver 送出真的
# 點選（`_click_dismiss_target`）。** 這件事直接搬動了本專案最貴的那條安全性質的
# 判定點：以前是「JS 不會 `click()` 到會花錢的按鈕」，現在是「JS 不會**回傳**會花
# 錢的按鈕」。所有反例測試都必須釘在回傳值上——釘在「有沒有 click 事件」上等於什麼
# 都沒驗（新版裡它必然是空的）。
#
# 為什麼要改成 driver 點選——經過一次來回才確定的，兩段都記著，因為兩段各自都是
# 對的、只是不完整：
#
# 1. **迴圈是需要的。** 規則 3 上線後連續五個額度週期都印
#    `could not dismiss blocking dialog via 'clicked-corner:button@805,21 32x32'`，
#    看起來像「找對了卻按不動」。推翻它的是 `describe_dialog_controls` 印出來的
#    **對話框尺寸**——它在規則 3 上線的那一刻換了：
#
#      11:44 之前：856x917、11 個候選（Subscribe / Pay As You Go / Get Started）
#      11:44 之後：420x322、5 個候選（Unsubscribe / Update Payment Details）
#
#    而那份清單是**關閉動作跑完之後**才抓的。所以合成點選確實把付費牆關掉了，
#    露出後面**第二層**對話框，`has_blocking_dialog` 看到還有 modal 就回報失敗。
#
# 2. **但迴圈解不掉第二層，合成點選對它無效。** 迴圈上線後的四個額度週期，log 的
#    形狀變成：第 1 層 @(805,21) 按下去畫面真的換了、第 2 層 @(369,21) 按下去
#    **一個字都沒變**，早停判準當場收手，仍然落到整頁 reload。
#
#    兩層那顆關閉鈕的 class 完全一樣
#    （`sc-2f2fb315-2 sc-29539429-20 sc-1336beac-0 eTBYIC jjGTfR fJg`），同一個元件、
#    同樣的視覺，卻一個吃合成點選、一個不吃。這反而支持「差別在**元素之上**」而不是
#    「元件本身不同」：`el.click()` 是**直接對那顆元素派送**，只會往上冒泡；真點選
#    的 `e.target` 是「那個座標上最上層的元素」。另外 `el.click()` 只派送 `click`
#    一種事件，真點選會走完整串 pointerdown / mousedown / mouseup / click——把關閉
#    行為掛在 `onPointerDown` / `onMouseDown` 是 modal 元件常見的寫法（避免點選
#    穿透），那一類同樣只有真點選收得到。
#
# 3. **第二層不是「需要使用者處理」的帳號異常**，這一點查清楚了才動手：
#    `chromedriver.log` 裡 `has_blocking_dialog` 的回傳原文是
#    「You are subscribed to the Opus tier! Your subscription renews around
#    2026/09/18」——就只是藏在付費牆後面的帳號管理面板。行為面也對得上：那之後每
#    小時照常回補、照常產圖（8.0 張/小時，與歷史基準 7.85 一致）。所以正確處置是
#    「關掉它」，不是「停下來叫人」。
#
# 兩段教訓一起記：**單一 log 訊息（「關不掉」）說的是結果，不是機制**，換機制之前
# 先找一個獨立的量交叉驗證（第 1 段用對話框尺寸救回一次）；但**交叉驗證證明的只是
# 它證明的那一件事**——尺寸變了只證明第一層被關掉，不證明第二層也會被關掉。
_DISMISS_DIALOG_JS = _JS_VISIBLE + r"""
const FORBIDDEN = /(purchase|buy|subscribe|subscription|upgrade|pay|payment|checkout|order|confirm|continue|proceed|accept|agree|ok|okay|yes|renew|top ?up|add funds|get more|get[- ]?started|gift[- ]?key|anlas)/i;
const DISMISS = /^(cancel|close|not now|later|maybe later|dismiss|no thanks|no,? thanks|back|×|✕|✖|x)$/i;
// 規則 3 的兩個門檻。**兩個都不能拿掉，它們擋的是不同的東西**，看起來像其中一個
// 是多餘的正是最容易犯的錯：
//   CORNER_MAX_PX  擋「在角落、但很大」——整片遮罩、對話框自己的 header 容器。
//                  它們的右上角座標與關閉鈕**完全一樣**，只有尺寸分得開。
//   CORNER_FRAC    擋「很小、但不在角落」——對話框中央／底部的無字圖示鈕。
//                  它們的尺寸與關閉鈕**完全一樣**，只有位置分得開。
// 實測依據（2026-09-03～09-07 的 `describe_dialog_controls` 清單，856x917 的購買
// 對話框、11 個候選）：唯二 text 為空的就是右上角那組 32x32 @(805,21)；會花錢的
// 那幾顆（Pay As You Go / Subscribe / Get Started / Anlas）**每一顆都有文字**，
// 其中 Anlas 只有 37x24——尺寸條件單獨擋不住它，擋住它的是「語意空白」。
const CORNER_MAX_PX = 48;
const CORNER_FRAC = 0.2;
// 候選控制項。`button` 以外還收 `[aria-label]` / `[title]` / `[tabindex]` /
// `[onclick]`：站方的關閉鈕常常是一個包著 svg 的 div，不是 <button>。
// **放寬的只有元素形狀，字面白名單一個字都沒動**，所以「絕不點
// 到會花錢的按鈕」這條性質不變。
const SELECTOR = 'button,[role="button"],a[href="#"],[aria-label],[title],'
               + '[tabindex],[onclick]';
// 三條規則都取「最內層」的命中。放寬選擇器之後，一個包著
// <button>Cancel</button> 的 wrapper 也會命中（innerText 一樣是 Cancel），而
// `el.click()` 的 target 就是那個 wrapper、不會傳給子節點，等於沒點。
function innermost(list) {
  for (const el of list) {
    if (!list.some(other => other !== el && el.contains(other))) return el;
  }
  return list[0] || null;
}
function labelOf(el) {
  return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
}
function attrOf(el, name) {
  return (el.getAttribute(name) || '').replace(/\s+/g, ' ').trim();
}
// 使用者**讀得到**的字。空 = 這是一顆純圖示鈕，也就是規則 3 的安全性質本身。
// 收的比 labelOf 廣，因為「innerText 是空的」不等於「沒有字」：
//   <input type="button" value="Purchase">  innerText 空、value 有字
//   <button><img alt="Buy"></button>        innerText 空、子節點 alt 有字
function readableOf(el) {
  const parts = [labelOf(el), attrOf(el, 'aria-label'), attrOf(el, 'title'),
                 attrOf(el, 'value'), attrOf(el, 'alt')];
  for (const kid of el.querySelectorAll('[alt],[aria-label],[title]')) {
    parts.push(attrOf(kid, 'alt'), attrOf(kid, 'aria-label'),
               attrOf(kid, 'title'));
  }
  return parts.filter(Boolean).join(' ').trim();
}
// 開發者取的識別字，給 FORBIDDEN 當縱深防禦用。**obfuscated class name 刻意
// 不收**：站方的 class 是 `sc-2f2fb315-2 eTBYIC jjGTfR` 這種隨機雜湊，撞出
// `ok` / `pay` 這種兩三個字母的機率不低，而誤判的代價是規則 3 整個安靜失效。
function identityOf(el) {
  return [attrOf(el, 'id'), attrOf(el, 'name'), attrOf(el, 'data-testid'),
          attrOf(el, 'data-test'), attrOf(el, 'data-action')]
         .filter(Boolean).join(' ').trim();
}
for (const node of document.querySelectorAll(
       '[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) {
  if (!onScreen(node)) continue;
  const controls = Array.from(node.querySelectorAll(SELECTOR))
    .filter(visible);
  const byText = controls.filter(el => {
    const label = labelOf(el);
    return DISMISS.test(label) && !FORBIDDEN.test(label);
  });
  const hitText = innermost(byText);
  if (hitText) {
    return {action: 'clicked:' + labelOf(hitText), el: hitText};
  }
  const byAria = controls.filter(el => {
    const aria = el.getAttribute('aria-label') || el.getAttribute('title') || '';
    return /close|dismiss/i.test(aria) && !FORBIDDEN.test(aria)
           && !FORBIDDEN.test(labelOf(el));
  });
  const hitAria = innermost(byAria);
  if (hitAria) {
    return {action: 'clicked-aria:' + (hitAria.getAttribute('aria-label')
                                       || hitAria.getAttribute('title') || ''),
            el: hitAria};
  }
  // 規則 3：右上角的無字圖示鈕。站方的關閉鈕語意上完全是空的（沒有文字、沒有
  // aria-label、沒有 title、連 svg 都沒有——X 是 CSS 畫的），所以規則 1 / 2 對它
  // 一律失效；實測 **218/218 次**全部落到 'escape'，而 Escape 對這個 modal 同樣
  // 無效——也就是每個額度週期都要付一次整頁 reload ＋ 重填欄位。
  // （log 裡另有 153 筆「dismissed」是舊版在驗證前就無條件印的假訊息，別拿它們
  //   算成功率；真正的判準是「每天的 reload 次數 == 當天的嘗試次數」。）
  const box = node.getBoundingClientRect();
  const byCorner = controls.filter(el => {
    // (a) 語意空白。這是規則 3 的**功能**條件：關閉鈕是純圖示鈕。
    if (readableOf(el)) return false;
    // (d) 縱深防禦。掃兩個來源，而且**刻意跟 (a) 重疊**：
    //   - `identityOf`（id / name / data-testid）——(a) 過了之後只可能由這個觸發，
    //     例如 id="purchase-more"。
    //   - `readableOf`——在 (a) 還在的時候這裡**必然是空字串**，看起來像死碼。它
    //     買的是「有人放寬 (a) 的時候仍然不會去點購買鈕」。理由：站方那兩層對話框
    //     上有三顆按鈕（`Get Started`／`Anlas`／`Activate a Gift Key`）**完全只靠
    //     (a) 擋住**，而 (a) 同時是「規則 3 能不能找到關閉鈕」的功能條件——同一個
    //     判斷兼任功能與安全兩個角色，改它的人不會意識到自己在動安全性質。分開
    //     之後，放寬 (a) 只會讓規則 3 失效（找不到關閉鈕 → 退回 reload，安全），
    //     而不會讓它去點購買鈕。
    //     守門：`test_forbidden_alone_still_blocks_every_paying_button`（把 (a)
    //     關掉之後 FORBIDDEN 仍須擋下兩層對話框上的每一顆）。
    if (FORBIDDEN.test(readableOf(el))
        || FORBIDDEN.test(identityOf(el))) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return false;      // 0 尺寸的殼
    // (b) 小。擋整片遮罩／header 容器——它們的角落座標跟關閉鈕一模一樣。
    if (r.width > CORNER_MAX_PX || r.height > CORNER_MAX_PX) return false;
    // (c) 在右上角。擋對話框別處的無字圖示鈕——它們的尺寸跟關閉鈕一模一樣。
    if (r.right < box.right - box.width * CORNER_FRAC) return false;
    if (r.top > box.top + box.height * CORNER_FRAC) return false;
    return true;
  });
  const hitCorner = innermost(byCorner);
  if (hitCorner) {
    const r = hitCorner.getBoundingClientRect();
    return {action: 'clicked-corner:' + hitCorner.tagName.toLowerCase()
                    + '@' + Math.round(r.left - box.left) + ','
                    + Math.round(r.top - box.top)
                    + ' ' + Math.round(r.width) + 'x' + Math.round(r.height),
            el: hitCorner};
  }
  return 'escape';
}
return null;
"""

# Escape 退路：對話框上沒有任何可以安全點下去的東西時用。對 React 的舊式事件
# 系統要補 `keyCode`/`which`（同 `_dismiss_autocomplete` 的理由）。
#
# **派給三個目標，不是只派給 `document`。** 事件從 `document` 只會往上冒泡到
# `window`，**不會往下傳**；把 keydown 掛在 modal 容器或焦點元素上的元件
# （focus-trap 類的函式庫很常這樣做）因此永遠收不到。實測依據：本專案 86 次額度
# 對話框，Escape 一次都沒關掉過，100% 落到「關不掉 → 重新整理」。
# 這一段送的仍是**合成**事件（`isTrusted === false`）；driver 層的真按鍵走
# `port.press_escape()`，由 `dismiss_blocking_dialog` 先試。
_SEND_ESCAPE_JS = _JS_VISIBLE + r"""
const targets = new Set([document]);
if (document.activeElement) targets.add(document.activeElement);
for (const node of document.querySelectorAll(
       '[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) {
  if (onScreen(node)) targets.add(node);
}
for (const target of targets) {
  for (const type of ['keydown', 'keyup']) {
    target.dispatchEvent(new KeyboardEvent(type, {
      key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
      bubbles: true, cancelable: true
    }));
  }
}
return targets.size;
"""

# 關不掉的時候，把對話框上**有哪些控制項**列出來。純唯讀，一個元素都不點。
#
# 為什麼需要它：`dismiss_blocking_dialog` 關失敗時只留下一句「關不掉」，看 log
# 的人無從知道是「站方根本沒放關閉鈕」還是「放了但選擇器認不出來」——而這兩者的
# 處置完全相反。沒有這份清單，這個問題就只能靠猜，而在**購買對話框**上靠猜著去
# 放寬點選規則正是最不該做的事。
#
# 排序刻意用「離對話框右上角的距離」：關閉鈕幾乎都在那個角落，於是最可疑的那個
# 會排在最前面，不會被卡在 30 筆上限外。輸出有界（最多 30 筆、每個欄位截斷），
# 因為它會寫進 WEBRunner.log。
_DIALOG_CONTROLS_DIAG_JS = _JS_VISIBLE + r"""
for (const node of document.querySelectorAll(
       '[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) {
  if (!onScreen(node)) continue;
  const box = node.getBoundingClientRect();
  const rows = [];
  for (const el of node.querySelectorAll('*')) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const style = getComputedStyle(el);
    const clickable = style.cursor === 'pointer'
      || el.tagName === 'BUTTON' || el.tagName === 'A'
      || el.getAttribute('role') === 'button'
      || el.hasAttribute('aria-label') || el.hasAttribute('title')
      || el.hasAttribute('onclick') || el.hasAttribute('tabindex');
    if (!clickable) continue;
    rows.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      aria: (el.getAttribute('aria-label') || '').slice(0, 40),
      title: (el.getAttribute('title') || '').slice(0, 40),
      text: (el.innerText || el.textContent || '')
              .replace(/\s+/g, ' ').trim().slice(0, 40),
      cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60),
      icon: el.querySelector('svg,img,path') ? 1 : 0,
      dx: Math.round(r.left - box.left),
      dy: Math.round(r.top - box.top),
      w: Math.round(r.width),
      h: Math.round(r.height),
      cursor: style.cursor,
      corner: Math.round(Math.hypot(box.right - r.right, r.top - box.top))
    });
  }
  rows.sort((a, b) => a.corner - b.corner);
  return {w: Math.round(box.width), h: Math.round(box.height),
          total: rows.length, controls: rows.slice(0, 30)};
}
return null;
"""

# Tier 2 用：畫面上有沒有「可見、有內容的 modal 對話框」。不看任何字面。
# 只認真正的 modal 語意（`role="dialog"` / `aria-modal`），不含吐司與
# `class*=modal` —— 那些太寬，正常頁面也常帶著隱藏的 modal 容器。
#
# **這裡刻意沒有長度上限；Tier 1 那道 `text.length > 1200` 不要抄過來。**
# 2026-09-09 修掉的缺陷就是「兩層共用同一行 cap」——**兩層守門共用同一個前置
# 條件，就不是兩層。** 一個 innerText 超過上限的 modal 會同時讓 Tier 1 跳過它
# **而且** Tier 2 回 None，兩層一起瞎掉。判斷「這是不是第二層」的方式不是看它有
# 沒有獨立的 selector／pattern，而是看**它會不會被同一個輸入關掉**。
#
# 拿掉的兩個理由，方向不同但結論一致：
#
# 1. **收斂已經由 selector 做完了。** Tier 1 的 SELECTOR 很寬（含
#    `[class*="modal"]`、吐司、`aria-live`），所以需要「太長的一定是包住半個頁面
#    的 wrapper」來擋掉誤判；Tier 2 只認 `role=dialog` / `alertdialog` /
#    `aria-modal` ＋ `onScreen()`，能通過的本來就只有真正的 modal。長度在這裡不做
#    任何收斂工作，純粹是從 Tier 1 抄過來的殘留。
# 2. **成本不對稱的方向是反的，所以 Tier 2 應該寬鬆。** Tier 1 誤判很貴——它走
#    `wait_for_quota_recovery`，而 `quota_wait_max_sec` 預設 0 ＝ 無上限，一次誤判
#    就是永不結束的等待迴圈——所以 Tier 1 寧可保守。Tier 2 誤判很便宜：它只在
#    `consecutive_fail_abort` 門檻（那時整個角色已經死了）與「關對話框迴圈的進度
#    判準」被問，而在後者，永遠不回 None 只會讓 `stop_reason` 落在 `same`/`still`
#    → `return False` → 呼叫端整頁 reload（安全）。**Tier 2 的漏判才貴**：
#    `dismiss_blocking_dialog` 把 None 讀成「關乾淨了」（`stop_reason = "closed"`
#    → `return True`），於是它會對著一個它根本沒碰到的對話框回報成功。那是靜默的
#    錯誤結果，不只是漏偵測。
#
# 而站方的付費牆正好是最容易超過上限的形狀：**整張定價表**（方案卡 ＋ 功能比較
# 表）。`WEBRunner.log` 08-24 → 09-09 的 260 筆 `[blocked] dialog text:` 相異內容
# 只有一種，尾巴斷在比較表的第一列——那是 `slice(0, 400)` 的截斷，全文長度從來沒
# 有人量過。餘裕現在會印出來，見 `_TIER1_TEXT_CAP`。
#
# **回傳值刻意留在 `str | None`**（見 `has_blocking_dialog`），所以全文長度用
# in-band 的方式帶出來：截斷時在尾巴附一段標記。呼叫端只拿它做**相等比較**（關
# 對話框迴圈的進度判準）與 log，兩者都不受影響；順帶還讓「前 400 字一樣、總長
# 不同」的兩層對話框分得出來。`EXCERPT` 用具名常數而不是字面量，是為了讓「有沒有
# 長度上限」這件事在原始碼層面仍然一眼看得出來（守門測試抽的是
# `text.length > <數字>` 這個形狀）。
_BLOCKING_DIALOG_JS = _JS_VISIBLE + r"""
const EXCERPT = 400;
for (const node of document.querySelectorAll(
       '[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) {
  if (!onScreen(node)) continue;
  const text = (node.innerText || node.textContent || '')
    .replace(/\s+/g, ' ').trim();
  if (!text) continue;
  return text.length > EXCERPT
    ? text.slice(0, EXCERPT) + ' …[truncated; full length ' + text.length + ']'
    : text;
}
return null;
"""

# Whether `snap()` writes `debug_*.png` at all. Read ONCE at this module's
# import (NOT hot-reloaded per character) — `snap` is called from the setup
# phase too, and toggling mid-run would give inconsistent before/after states.
# Toggle in `batch_config.json` then `!stop` + `!run` to apply. (Same single-
# read-at-import semantics as before; now shared by both variants.)
_DEBUG_SCREENSHOTS = bool(load_batch_config().get("debug_screenshots", False))

# DOM diagnostics JS — snapshots every textarea / contenteditable's key attrs.
_DOM_DIAG_JS = r"""
return Array.from(document.querySelectorAll(
    'textarea, [contenteditable="true"]'
)).map((e, i) => {
    const isInput = (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT');
    const fullText = isInput ? (e.value || '') : (e.innerText || '');
    const parentText = (e.parentElement
        ? (e.parentElement.innerText || '') : '');
    return {
        index: i,
        tag: e.tagName,
        aria_label: e.getAttribute('aria-label'),
        placeholder: e.getAttribute('placeholder')
            || e.getAttribute('data-placeholder'),
        visible: !!e.offsetParent,
        value_len: fullText.length,
        value_preview: fullText.substring(0, 60),
        parent_text: parentText.substring(0, 120).replace(/\s+/g, ' ').trim()
    };
});
"""


# ---------- 「我現在跑的是哪一版程式碼」（見 `_code_fingerprint`）-------------

def log_code_fingerprint() -> str:
    """啟動橫幅：把**此刻**載入的程式碼指紋凍住，並印出一行摘要。回傳那一行。

    兩個變體都在 `main()` 的最前面呼叫一次（import 全部跑完之後）。放在共用模組
    而不是各自複製兩行，理由與 `log_driver_versions` 完全相同：兩支變體必須同步是
    本專案的硬規則，而「只有一邊有 log」的下場就是出事那次剛好跑的是沒有 log 的
    那一支。

    **為什麼一定要在啟動當下取樣**：`_code_fingerprint.snapshot()` 之後才有東西可
    以拿來跟磁碟比。等到要查的時候才算，量到的是磁碟現況——也就是比較的另一邊——
    於是永遠回報「沒有漂移」，而且測起來全綠。這是那個模組唯一真正的失效方式。

    **不必擔心之後的延遲 import。** `_project_source_files()` 走 `sys.modules`，所以
    這之後才被 import 進來的本專案模組會出現在 `added` 裡——但 `added` **不算漂移**
    （見 `_code_fingerprint.drift_report`：那個模組是剛從磁碟載入的，它是最新的，
    正是「沒有落後」）。所以這裡沒有「呼叫端必須保證後面不再 import」那種約束，
    要在函式裡延遲 import 什麼都可以。

    **這一行值多少**：本專案的行程一次跑好幾天（webrunner 連續跑過 78.7 小時），
    而 repo 在它們跑的同時被持續編輯。事後讀 log 有兩個問題只有它答得出來——
    traceback 印出來的原始碼文字可不可信（`linecache` 是列印當下才讀磁碟的，行號
    來自載入時的 code object，檔案改過就對不起來），以及「那個修正到底有沒有在這
    個行程裡生效」（webrunner 換角色重啟的是瀏覽器、不是行程；`/version` 報的是
    git HEAD，而工作區可以帶著好幾天未提交的修改）。

    永不 raise：這是一行診斷紀錄，不得有任何機會把一次正常的啟動變成失敗。
    """
    try:
        _code_fingerprint.snapshot()
        line = _code_fingerprint.describe()
    except Exception as error:  # pylint: disable=broad-except
        # `!r` 刻意保留：`try` 裡只有 `_code_fingerprint`（純檔案雜湊），碰不到
        # driver；而 `str(OSError)` 會把被雜湊的原始碼路徑印出來。
        line = f"code fingerprint unavailable: {error!r}"
        print(f"code fingerprint failed: {error!r}", file=sys.stderr)
    print(f"  [fingerprint] {line}")
    return line


def report_code_drift() -> str:
    """週期性檢查點（角色邊界）：磁碟上的程式碼有沒有跟我啟動時載入的分岔。

    **`drifted is False` 時一個字都不印。** 這不是風格偏好，是這個函式最重要的
    性質：漂移是本專案的**常態**（repo 一直在被編輯），角色邊界每個角色都會經過
    一次，而 `discord_bot.log` 已經有 11,250/11,746 行都是同一句 `rpc apply ->`
    的前例——會被關掉的 log 等於沒有 log。所以只有真的有話要說時才出聲。

    `None`（判斷不出來）也要出聲，但措辭必須跟「有漂移」分得開：本專案在
    `_find_all_chrome_processes` / `_load_pid` / `dashboard_server` 上各踩過一次
    「失敗的掃描長得跟乾淨的掃描一模一樣」，這裡不重蹈。

    **偵測到漂移不改變任何行為**——不重啟、不中止、不拒絕產圖。漂移是常態，把它
    接進控制流程只會製造誤殺。純診斷，回傳印出去的那一行（沒印就回空字串）。

    永不 raise，理由同 `log_code_fingerprint`。
    """
    try:
        report = _code_fingerprint.drift_report()
    except Exception as error:  # pylint: disable=broad-except
        # `!r` 刻意保留：同上，`_code_fingerprint` 是純模組，碰不到 driver。
        print(f"code drift check failed: {error!r}", file=sys.stderr)
        return ""
    if report["drifted"] is False:
        return ""  # 常態，安靜通過——不要製造下一個 `rpc apply ->`。
    if report["drifted"] is None:
        line = f"  [fingerprint] 無法判斷程式碼有沒有變動：{report['why']}"
        print(line)
        return line
    # 只列真正代表「我落後了」的那兩類。`added` **不算漂移**（見
    # `_code_fingerprint.drift_report`：那是 `snapshot()` 之後才延遲 import 進來
    # 的模組，剛從磁碟載入、正是最新的），所以把它混進這一行只會指著一個沒有問題
    # 的檔名，讓讀 log 的人往錯的方向查。`describe()` 也是同樣的取捨。
    names = report["changed"] + report["removed"]
    line = (f"  [fingerprint] 磁碟上的程式碼已經和本行程啟動時載入的不一樣了"
            f"（{report['at_start']} → {report['now']}）；"
            f"**本行程跑的仍是舊版**，要等重新啟動才會換。"
            f"變動：{', '.join(names)}")
    print(line)
    return line


# ---------- critical_error 的診斷內容 ----------------------------------------
# 這個模組所在的目錄。用**套件**根而不是 `PROJECT_ROOT`：`.venv/` 就在
# `PROJECT_ROOT` 底下，拿專案根去比對的話，每一個 site-packages 的 frame 都會被
# 認成「我們的」，摘要就完全失去意義。
_PACKAGE_ROOT = Path(__file__).resolve().parent

# `critical_error.traceback` 的預算。判準是「**實測過的每一份 traceback 都要能完整
# 放進去**」，不是「大概夠用」——這個欄位存在的理由就是事後判讀，截到的那一次剛好
# 就是需要它的那一次。`WEBRunner.log` 全部 8 份（含 09-07 那三次 `MaxRetryError`
# 的例外鏈，原始 7,452 字）折疊之後最大是 **3,575** 字（那一份幾乎全是我們自己的
# frame，沒什麼可折的）。4,000 留了 12% 餘裕，同時仍然擋得住失控的遞迴
# （`RecursionError` 會產生上千個 frame）。
#
# 大一點不痛：`critical_error` 實測 76 天只發生 7 次，而 `events.ndjson` 由 bot
# 輪替。**這個數字要跟著實測走**——量法是 `_traceback_excerpt(raw, limit=10**9)`。
_TRACEBACK_BUDGET = 4000


def _traceback_excerpt(text: str, *, limit: int = _TRACEBACK_BUDGET) -> str:
    """把 traceback 摘成「**我們自己的 frame 一定留著**」的版本。

    **原本是 `traceback.format_exc()[-1500:]`，而那剛好在最需要它的那一類故障上
    把有用的部分全丟掉。** 實測全部 7 筆 `critical_error`：例外來自我們自己的程式
    時，尾段 1500 字裡有 2–5 個我們的 frame、0 個第三方的；例外來自函式庫深處時
    （09-07 那三次 `MaxRetryError`）是 **0 個我們的、4 個 urllib3 的**。

    **而且「改成留頭段」也修不好。** 同一份實測：我們的 frame 落在 7,452 字裡的
    2564–4025，**頭尾都不是**。原因是例外鏈——Python 先印最內層的成因
    （urllib3 的 `_new_conn` → `ConnectionRefusedError`），我們的 frame 在**第三段**
    traceback 的開頭。所以這裡不做頭尾截斷，而是按**來源**篩：

    * 未縮排的行（`Traceback (most recent call last):`、`During handling of the
      above exception…`、最後的例外行）一律保留——例外鏈的骨架與最終死因。
    * `File "…"` 落在本套件目錄底下的 frame，連同它的原始碼回音，一律保留。
    * 連續的第三方 frame 只留**第一個**（那是我們交棒出去的那一格，會指名是哪個
      函式庫），其餘折疊成一行 `[... 省略 N 個第三方 frame ...]`。urllib3 的重試是
      遞迴的，所以那一串本來就幾乎完全一樣。
    * 最後才套整體上限，而且是頭尾都留、中間標明省略了幾個字元——那是防遞迴爆炸的
      保險，不是主要機制。

    判讀提醒：這個欄位裡的**原始碼文字**在檔案被改過之後會騙人（`linecache` 是列印
    當下才讀磁碟的，行號來自載入時的 code object）。所以同一個事件另外帶
    `code_drift`——見 `code_drift_flag`。
    """
    kept: list[str] = []
    marker = str(_PACKAGE_ROOT)
    foreign_frames = 0
    in_our_frame = True          # 判不出來就當成「我們的」→ 寧可留著

    def flush() -> None:
        nonlocal foreign_frames
        if foreign_frames > 1:
            kept.append(f"  [... 省略 {foreign_frames - 1} 個第三方 frame ...]")
        foreign_frames = 0

    for line in text.splitlines():
        if line[:1] not in (" ", "\t"):
            flush()
            kept.append(line)
            in_our_frame = True
            continue
        if line.lstrip().startswith('File "'):
            if marker in line:
                flush()
                in_our_frame = True
                kept.append(line)
            else:
                in_our_frame = False
                foreign_frames += 1
                if foreign_frames == 1:
                    kept.append(line)
            continue
        # frame 底下的原始碼回音：只有**我們自己**的 frame 留。第三方 frame 的
        # 回音佔的位置不小（Python 3.11+ 的 `...<10 lines>...` 區塊動輒兩百字），
        # 而它要回答的問題「是哪個函式庫、哪一個函式」上面那行 `File` 已經答完了。
        if in_our_frame:
            kept.append(line)
    flush()

    out = "\n".join(kept)
    if len(out) <= limit:
        return out
    # 保險絲。尾段一定要留住：traceback 的**最後一行**就是例外型別與訊息。
    head = limit * 2 // 3
    tail = limit - head
    return (out[:head]
            + f"\n  [... 中間再省略 {len(out) - head - tail} 個字元 ...]\n"
            + out[-tail:])


def code_drift_flag() -> bool | None:
    """啟動之後磁碟上的程式碼有沒有變過（True／False／None＝判不出來）。

    只給 `critical_error` 事件用，理由是一個實際踩過的判讀陷阱：traceback 印出來的
    **原始碼文字**是列印當下才從磁碟讀的（`linecache`），而行號來自載入時的 code
    object——檔案改過之後兩者就對不起來。2026-09-07 17:10 那一筆就是實例：frame 寫
    `_note_transport_error`，印出來的文字卻是 `class BrowserGoneError(RuntimeError):`。
    沒有這個旗標的話，讀 `events.ndjson` 的人無從判斷那份 traceback 的文字可不可信
    （`report_code_drift()` 只印到 log，而 log 會被輪替掉）。

    **判讀規則：行號一律可信；原始碼文字只在 `code_drift` 為 False 時可信。**

    永不 raise：這是死亡路徑上的診斷欄位，不得再製造第二個例外。
    """
    try:
        return _code_fingerprint.drift_report()["drifted"]
    except Exception:  # pylint: disable=broad-except
        return None


# ---------- 「有批次在跑」的存活訊號 -----------------------------------------

def claim_liveness_signal() -> int | None:
    r"""沒有人認領 `webrunner.pid` 就寫自己的 pid 進去。回認領到的 pid，否則 None。

    正常情況下這個檔由**父行程**寫：bot 的 `_spawn_webrunner` 與
    `start_webrunner.py` 在 spawn 的短臨界區內（持著 Chrome 槽）寫好 pid 才放槽，
    之後整輪由這個檔當「有批次在跑」的長期訊號。⚠️ **Chrome 槽不是那個訊號**——它
    只是 spawn 前後的毫秒級臨界區（實測：批次連續跑了好幾天，`chrome_slot.lock`
    始終不存在）。

    **裸跑（`py -3 axiomatic/webrunner_novelai.py`）沒有那個父行程，所以兩個訊號
    一個都不存在。** 後果不只是「同時跑的 `verify_browser` 會開出自己的瀏覽器、
    然後在下一個角色邊界被 `_kill_orphan_chrome` 的全機掃描殺掉」（症狀看起來像
    瀏覽器自己壞了，原因卻在另一個行程裡）——更嚴重的是
    `start_webrunner.py` 與 `run_batch.py` 的「已經有批次在跑就不要再起一個」也一起
    失效，於是同一台機器上跑起**兩個** webrunner，搶同一份 `.chrome_profile_snap/`、
    互相 nuclear sweep、從同一組 `todo_*.md` 重複取件。而 `install_autostart.py`
    註冊的開機工作跑的正是 `start_webrunner.py`，所以那條路是**無人值守也到得了的**。

    所以這裡補的是「**沒有人認領就自己認領**」，不是「一律覆寫」。

    ⚠️ **與父行程的競態是良性的，兩種順序都對。** 父行程是在 `Popen` **回來之後**
    才寫的（`_supervisor.stream_child`：先 `Popen`、再 `on_spawn`），所以子行程確實
    有機會先寫到——實測 `Popen` 只花 7ms 回來，而子行程要先啟動直譯器再 import
    driver，實務上父行程一定先寫，但不能靠這個。真的反過來的話，檔案裡放的是子行程
    自己的 pid，**那同樣是一個活著、而且就是這個批次的行程**，而**把關用的那幾支
    讀取端問的都只是「有沒有批次在跑」**，不問那個 pid 是父寫的還是子寫的。收尾時
    兩邊都只刪「還記著自己那一筆」的檔（見 `release_liveness_signal` 與
    `start_webrunner._clear_pid_if_ours`），所以誰都不會誤刪對方的訊號。

    ⚠️ **上面那句以前寫的是「四個讀取端」，而那個數字既已過期、對本函式自己也不
    成立（2026-09-21 修）。** 用 AST 掃過整份產品碼，讀 `webrunner.pid` 的函式有
    **八個**，本函式就是其中之一——但它問的不是「有沒有批次在跑」，是「需不需要我
    來認領這個訊號」，所以它從來就不在那句話涵蓋的範圍內。因此這裡不再點數字，改
    成只講**把關用**的那一類。完整分類（三分法四支 ／ 刻意不是三分法的四支，每一
    支都附理由）與雙向對帳住在 `axiomatic/test_pid_file_readers.py`：掃到卻沒分
    類會紅，清單裡留著已經不讀這個檔的函式也會紅。

    注意父行程寫的是**轉接殼**的 pid，不是 webrunner 自己的 `os.getpid()`
    （`.venv\Scripts\python.exe` 是 redirector stub，實測父子 pid 不同）。兩個值都是
    有效的存活訊號，所以這裡比的是「活著沒有」，不是「是不是我」。

    ⚠️ **判不出來時要當成「有人在跑」**（`_chrome_slot._pid_alive` 判不出來回 True，
    這裡就是要那個方向）：誤判成「沒人在跑」會**覆寫掉別人有效的訊號**，而那個訊號
    正是所有下游「不要開第二套瀏覽器」判斷的唯一依據；誤判成「有人在跑」只是這一次
    裸跑沒有發布訊號，回到修正前的狀態。用 `_chrome_slot` 的那一份而不是
    `_process_control` 的，是因為後者判不出來時回 False——`CLAUDE.md` 記著那個分歧是
    刻意的，挑哪一份要看「錯了要往哪邊倒」。也刻意**不再寫第四份** `_pid_alive`。
    """
    try:
        raw = WEBRUNNER_PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raw = ""
    except (OSError, UnicodeDecodeError):
        print("webrunner.pid 讀不出來；保守起見不認領存活訊號", file=sys.stderr)
        return None
    if raw:
        try:
            other = int(raw)
        except ValueError:
            print("webrunner.pid 的內容不是數字；保守起見不認領存活訊號",
                  file=sys.stderr)
            return None
        if other == os.getpid() or _chrome_slot._pid_alive(other):
            return None          # 父行程（或別的批次）已經認領了 → no-op
    mine = os.getpid()
    if not _run_progress.atomic_write_text(WEBRUNNER_PID_FILE, str(mine)):
        print("寫不進 webrunner.pid；這一輪沒有存活訊號", file=sys.stderr)
        return None
    print(f"  [liveness] 沒有父行程發布存活訊號，本行程自行認領（pid={mine}）")
    return mine


def release_liveness_signal(claimed: int | None) -> None:
    """收尾：**只在檔案仍然記著 `claimed` 時**才刪。永不 raise。

    判準與 `start_webrunner._clear_pid_if_ours` 相同：父行程也會寫同一個檔，無條件
    刪會把**別人的**存活訊號清掉，於是驗證端會在批次還跑著的時候開第二套瀏覽器。
    """
    if claimed is None:
        return
    try:
        raw = WEBRUNNER_PID_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return
    if raw != str(claimed):
        return                   # 被父行程覆寫過 → 那筆是它的，讓它自己清
    try:
        WEBRUNNER_PID_FILE.unlink()
    except OSError:
        pass


def run_with_liveness_signal(entry) -> int:
    """跑 `entry()`，期間盡量確保磁碟上有一個「有批次在跑」的訊號。

    掛在兩個變體的 `__main__` 而不是 `main()` 裡面：涵蓋整個行程（含
    `_kill_orphan_chrome` 與建 driver 那十幾秒——正是驗證端最可能擠進來的窗），
    `main()` 不必整段縮排，而且**隔離驗證模式走的是另一條分支**
    （`_run_setup_verification`），所以它天生不會認領 pid。

    ⚠️ **認領失敗絕對不可以擋住批次。** 這是一個純粹的旁路訊號；它如果有 bug，
    最壞的後果應該是「回到修正前、沒有訊號」，而不是 webrunner 起不來——那會變成
    supervisor 一直重生、rapid-fail 放棄，也就是**用一個可靠度改善換掉整個批次**。
    所以認領包在 broad except 裡（收尾那半自己就承諾 never raise）。
    """
    try:
        claimed = claim_liveness_signal()
    except Exception as error:  # pylint: disable=broad-except
        # `!r` 刻意保留：`claim_liveness_signal` 是檔案 I/O ＋ pid 探測，碰不到
        # driver；`str(OSError)` 會把存活訊號檔的完整路徑帶出來。
        print(f"認領存活訊號時出錯，略過（不影響批次）：{error!r}", file=sys.stderr)
        claimed = None
    try:
        return entry()
    finally:
        release_liveness_signal(claimed)


# ---------- events -----------------------------------------------------------

def emit_event(event_type: str, **data) -> None:
    """Append a structured event to events.ndjson for the Discord bot watcher.

    **遙測絕不可以打斷批次。** 這是盡力而為的旁路：寫不出去就少一則通知，讓一個
    無人值守的批次死在這裡是完全不成比例的代價（同 `dorossi_backend.
    _dorossi_record_usage` 那條「記錄用量絕不能打斷一輪對話」）。

    所以 except 不能只接 `OSError`——`json.dumps` 的失敗根本不是 OSError：

    * `TypeError` — 值不可序列化（`Path`、`datetime`、`set`、selenium 的
      `WebElement`）。目前**所有**呼叫點傳的都是 str／int／float／bool／`list[str]`
      （逐一核過），所以這是潛伏而非現行的 bug。最可能先中的是
      `check_dom_request` 的 `data=diag`：那是**瀏覽器回來的資料**，
      `dump_textareas_diag` 只檢查外層 `isinstance(result, list)`、不檢查元素，
      `_DOM_DIAG_JS` 哪天多回一個 DOM 節點就會變成 `WebElement`。
    * `ValueError` — 循環參照；**以及 `UnicodeEncodeError`（ValueError 的子類）**，
      瀏覽器回來的字串帶落單代理字元（lone surrogate）時就會中，這條比循環參照
      實際得多。

    先序列化、再開檔：不可序列化的值就不會留下一個空檔，而且一行要嘛整行寫進去、
    要嘛完全沒寫——讀取端（bot 的 watcher）永遠不會讀到半行。
    """
    record = {"ts": time.time(), "type": event_type, **data}
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with EVENTS_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except (OSError, TypeError, ValueError) as error:
        # `!r` 刻意保留：這三種例外 `args` 都非空，而 `str(OSError)` 會把
        # `events.ndjson` 的完整主機路徑寫進 log（`/log tail` 會把它送出去）。
        print(f"emit_event({event_type}) failed: {error!r}", file=sys.stderr)


def wait_if_paused(label: str = "") -> None:
    """Block at safe batch boundaries while the bot's pause marker exists."""
    def _read_pause() -> dict | None:
        try:
            raw = WEBRUNNER_PAUSE_FILE.read_text(encoding="utf-8").strip()
            parsed = json.loads(raw) if raw else {"mode": "now"}
            return parsed if isinstance(parsed, dict) else {"mode": "now"}
        except Exception:  # pylint: disable=broad-except
            return {"mode": "now"}

    def _write_pause(data: dict) -> None:
        # 原子寫入（跨行程檔案的硬規則）。bot 那側寫這個檔一直是原子的，這側
        # 卻是就地覆寫——半寫入的 JSON 會讓 `_read_pause` 解析失敗、退回
        # `{"mode": "now"}`，也就是把「跑完這張再停」變成「立刻停」。
        _run_progress.atomic_write_text(
            WEBRUNNER_PAUSE_FILE, json.dumps(data, ensure_ascii=False))

    is_pair_boundary = "pair" in (label or "").lower()
    announced = False
    while True:
        marker = _read_pause() if WEBRUNNER_PAUSE_FILE.exists() else None
        if marker is None:
            break
        mode = str(marker.get("mode") or "now").lower()
        if mode == "after_current":
            if is_pair_boundary:
                marker["mode"] = "now"
                _write_pause(marker)
                mode = "now"
            else:
                break
        elif mode == "after_pairs":
            if is_pair_boundary:
                remaining = marker.get("remaining", 0)
                if not isinstance(remaining, int):
                    remaining = 0
                if remaining > 0:
                    marker["remaining"] = remaining - 1
                    _write_pause(marker)
                    break
                marker["mode"] = "now"
                _write_pause(marker)
                mode = "now"
            else:
                break
        if mode != "now":
            break
        if not announced:
            print(f"  paused by bot{f' ({label})' if label else ''}; "
                  f"waiting for resume marker removal")
            emit_event("paused", label=label)
            announced = True
        time.sleep(2.0)
    if announced:
        print("  pause cleared; resuming")
        emit_event("resumed", label=label)


# ---------- DOM diagnostics (formatting only — the capture stays per-variant
# because it calls execute_script / jss) ------------------------------------

def format_textareas_diag(diag: list[dict]) -> str:
    """把 dump_textareas_diag 結果展成多行字串，給 webrunner.log 印。"""
    if not diag:
        return "DOM diag: (no textareas / contenteditable found)"
    lines = [f"DOM diag: {len(diag)} text-fields"]
    for t in diag:
        idx = t.get("index", "?")
        tag = t.get("tag", "?")
        vis = "Y" if t.get("visible") else "N"
        n = t.get("value_len", 0)
        aria = t.get("aria_label") or ""
        ph = (t.get("placeholder") or "")[:40]
        preview = (t.get("value_preview") or "")[:40]
        parent = (t.get("parent_text") or "")[:60]
        lines.append(
            f"  [{idx}] {tag} vis={vis} len={n} "
            f"aria={aria!r} placeholder={ph!r} "
            f"preview={preview!r} parent={parent!r}"
        )
    return "\n".join(lines)


# ---------- single-image (one-shot) path helper -----------------------------

def _single_image_relative_path(save_path: Path) -> str:
    """把 one-shot 圖的絕對路徑轉成 PROJECT_ROOT 相對的 POSIX 字串（contract
    要求，例如 `output/_oneshot/<request_id>/<file>.png`）。轉相對失敗就退回
    檔名本身（仍是合法 POSIX 片段），絕不丟例外。"""
    try:
        return save_path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return save_path.name


# ---------- 瀏覽器視窗最小化（兩個變體共用） ---------------------------------
#
# 兩支 webrunner 本來各有一份**逐行相同**的實作，而且都自己掛 `win32gui` /
# `win32process`：掃 psutil 找 cmdline 含 profile 路徑的瀏覽器行程 → 列舉視窗 →
# 比對視窗所屬行程 → 最小化。同一件事兩份實作，修好的永遠只有其中一份，所以收成
# 這裡一份，Win32 的部分全部轉呼叫桌面自動化函式庫。
#
# 為什麼要靠**行程**而不是視窗標題找：瀏覽器是 multi-process，視窗標題是當下網頁
# 的標題（隨時在變），而且好幾個行程根本沒有視窗。擁有者才是穩定的鍵。

def find_browser_pids_for_profile(profile_dirs: list[Path]) -> set[int]:
    """cmdline 指向這些 profile 目錄的瀏覽器行程 pid。

    比對前把路徑正規化成小寫、正斜線——cmdline 裡的寫法與 `Path` 的字串形式不見得
    一致（反斜線 vs 正斜線、大小寫），直接字串比對會漏。

    **比對是邊界比對，不是子字串比對。** 本專案同時存在 `.chrome_profile` 與
    `.chrome_profile_snap`（前者是登入用的來源、後者是 Chrome 真正開的那一份），
    前者是後者的**嚴格前綴**，所以 `path in cmd` 會把跑在 snapshot 上的行程一併
    算成 `.chrome_profile` 的。隔離驗證模式把 `CHROME_PROFILE_SNAPSHOT` 換成
    `.chrome_profile_verify` 之後更明顯：`[.chrome_profile, .chrome_profile_verify]`
    這組 wanted 會**連正式批次的瀏覽器一起選中**。目前唯一的呼叫端是
    `hide_browser_windows`（誤配的後果只是把別人的視窗搬到螢幕外），但這支函式的
    契約是「哪些行程屬於這個 profile」，一旦有人把它接到終止那一側，多抓就等於
    **殺到不該殺的行程**——這個 repo 已經為了「殺太多」賠掉過一個跑了 78.7 小時
    的批次。所以在來源這裡收緊，而不是要求每個呼叫端自己小心。
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        return set()
    # 邊界＝字串結尾、路徑分隔符（反斜線已正規化成 `/`）、空白或引號。psutil 的
    # `cmdline()` 回的是**已經切好的 arg 清單**，所以實務上目標路徑後面只會是
    # 「這個 arg 到此為止」（→ 空白或結尾）或「還有下一層」（→ `/`）；引號留給
    # 少數會把整條命令列原封不動回傳的驅動版本。`_` 與英數字刻意**不在**邊界集合
    # 裡——`_snap` / `_verify` 這種後綴正是要擋的東西。
    wanted = [
        re.compile(re.escape(str(path).replace("\\", "/").lower())
                   + r"""(?=$|[/\s"'])""")
        for path in profile_dirs
    ]
    if not wanted:
        return set()
    found: set[int] = set()
    try:
        # `attrs=` 只取便宜的 `name`，`cmdline()` 留給通過篩選的那幾筆逐一索取。
        # 放進 `attrs=` 就是替**全機每一個行程**都讀一次 PEB，而這裡九成九會被
        # 下一行的 name 判斷丟掉。本機實測（361 個行程、13 個瀏覽器行程）：
        # 一起取 334 ms，先篩再取 121 ms。這支每次重啟瀏覽器都會跑一次，而
        # `restart_chrome_every_n_characters` 預設 1 ＝ 每個角色一次。
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() != "chrome.exe":
                    continue
                cmd = " ".join(
                    arg for arg in (proc.cmdline() or [])
                    if isinstance(arg, str)
                ).replace("\\", "/").lower()
                if any(pattern.search(cmd) for pattern in wanted):
                    found.add(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as error:  # pylint: disable=broad-except
        # `!r` 刻意保留：`try` 裡只有 psutil，碰不到 driver，而 psutil 的例外
        # （含 `RuntimeError: SystemExtendedHandleInformation buffer too big`）
        # `args` 非空，`repr()` 讀得到訊息。
        print(f"find_browser_pids_for_profile failed: {error!r}", file=sys.stderr)
    return found


# 瀏覽器視窗一律放在任何螢幕都照不到的地方，而且**不最小化**（擁有者要求
# 2026-09-22：任何時候都不得在前景看到產生中的圖片預覽）。
#
# 原本的做法是 `--start-maximized` 開窗、setup 完才最小化，圖與圖之間再最小化一次
# 「以防點選或聚焦把它喚回來」。那個做法本身就是症狀的來源：視窗真的會被喚回前景，
# 然後才被縮下去，中間那一下剛好看得到剛產出的圖；最小化／還原的系統動畫也會把
# 視窗內容從工作列按鈕縮放出來。改成開窗時就放在 (-32000, -32000)，之後不論是什麼
# 把它「喚回」、「還原」或「帶到前面」，它都還在那個座標——2026-09-22 在這台機器上
# 實測：點選元素、截圖、`ShowWindow(SW_RESTORE)`＋`SetForegroundWindow`、先最小化
# 再還原、CDP `Page.bringToFront`，視窗全程停在螢幕外，畫面照常繪製；
# `--window-position` 也蓋得過 profile 裡記住的視窗位置（連「上次是最大化」也蓋得過）。
# 繪製沒有停，靠的是 `_MEMORY_FLAGS` 裡原本就有的 `--disable-backgrounding-occluded-windows`
# 等三個旗標——螢幕外的視窗對 Chrome 來說是被遮住的視窗。
#
# 大小固定 1920×1080 而不是跟著螢幕：原本最大化時的內容區大約就是這個大小，固定下來
# 讓頁面版面不隨主機的螢幕解析度改變（DOM 流程是照這個版面寫的）。
OFFSCREEN_WINDOW_POSITION = (-32000, -32000)
OFFSCREEN_WINDOW_SIZE = (1920, 1080)
# 左上角兩個座標都不大於這個值，就當成已經在螢幕外。多螢幕配置的座標實際上在 ±數千
# 以內，所以這個門檻不會把任何一塊真的螢幕算成「螢幕外」；也不必剛好等於 -32000——
# 系統或瀏覽器把它挪動幾個像素時不必再搬一次。
_OFFSCREEN_EDGE = -20000


def hide_browser_windows(profile_dirs: list[Path]) -> tuple[int, int]:
    """把那些 profile 的瀏覽器視窗移回螢幕外，回 `(藏好的視窗數, 仍留在螢幕上的視窗數)`。

    **不最小化，也不還原。** 已經最小化的視窗本來就看不到，還原它反而會播放一次把
    內容從工作列放大出來的動畫，所以原樣留著；它日後被還原時回到的是「最小化前的
    位置」＝螢幕外。搬移用 `MoveWindow`，不會把視窗帶到前面、也不會搶走焦點。

    `(0, 0)` 代表一個視窗都沒找到（行程還沒起來、或桌面自動化函式庫不可用）——
    呼叫端不得把它讀成「藏好了」。
    """
    pids = find_browser_pids_for_profile(profile_dirs)
    if not pids:
        return 0, 0
    try:
        import je_auto_control as ac  # type: ignore
        wm = ac.windows_window_manage
    except Exception as error:  # pylint: disable=broad-except
        # `!r` 刻意保留：`try` 裡只有桌面自動化函式庫的 import，收到的是
        # `ImportError`／`AttributeError`（`args` 非空），跟 selenium 無關。
        print(f"hide_browser_windows: backend unavailable: {error!r}",
              file=sys.stderr)
        return 0, 0
    x, y = OFFSCREEN_WINDOW_POSITION
    hidden = exposed = 0
    for pid in pids:
        try:
            for hwnd, _title in ac.windows_for_process_id(pid):
                if wm.is_window_minimized(hwnd):
                    hidden += 1
                    continue
                rect = wm.get_window_rect(hwnd)
                if rect is None:
                    exposed += 1
                    continue
                left, top, right, bottom = rect
                if left <= _OFFSCREEN_EDGE and top <= _OFFSCREEN_EDGE:
                    hidden += 1
                elif wm.move_window(hwnd, x, y, right - left, bottom - top):
                    hidden += 1
                else:
                    exposed += 1
        except Exception as error:  # pylint: disable=broad-except
            # `!r` 刻意保留：桌面自動化函式庫的例外，不是 selenium 的。
            print(f"hide_browser_windows({pid}) failed: {error!r}",
                  file=sys.stderr)
            exposed += 1
    return hidden, exposed


# ---------- chromedriver 的記錄檔（spawn 失敗唯一的 root cause 來源）---------
#
# **兩個變體共用一份，而不是各抄一份。** 這一整組是純 `Path` 操作、完全不碰
# driver，而它記載的度量（成長率、上限依據、中途修剪為什麼會被補零）是實測換來
# 的——抄成兩份的話，下一次重量只會更新其中一份。同樣的理由已經讓
# `hide_browser_windows` / `find_browser_pids_for_profile` 搬到這裡。
#
# **je 變體 2026-09-09 才接上。** 在那之前它一個都沒有，於是 `/run`（bot 的預設
# 變體就是 je）遇到 Chrome 起不來時，只會拿到一句猜測——「likely Out of Memory
# or a chromedriver/Chrome version mismatch」——而 5 分鐘的 startup window 一過，
# `_watch_for_fallback` 就靜靜轉跑 selenium 變體，使用者連「je 為什麼死掉」這個
# 問題都不會問。je 這一側**可以**產生這個檔：`wr.set_driver` 的 `**kwargs` 原封
# 不動轉給 `webdriver.Chrome(...)`，所以傳一個 `service=ChromeService(
# log_output=…)` 就行（實測見 `test_je_facade.py`）。

_CHROMEDRIVER_LOG = PROJECT_ROOT / "chromedriver.log"
# 上一個 driver 工作階段的記錄。見 `_rotate_chromedriver_log`。
_CHROMEDRIVER_LOG_PREV = PROJECT_ROOT / "chromedriver.prev.log"
# `chromedriver.log` 的上限（見 `_trim_chromedriver_log`）。
#
# **觸發門檻 64 MB 的依據是「一個角色的工作階段實際會寫多少」**，2026-09-07 重量：
# - 這個檔的大小取決於**單一 chromedriver 工作階段有多長**（它只在啟動時清空）。
#   實測一段正在跑的工作階段：19:18:11 開始、00:56:41 最後一筆，5.64 小時寫了
#   12,747,290 bytes ＝ **2.15 MB/小時（37 KB/分鐘）**。
# - 工作階段長度 ＝ `images_per_character` ÷ **實際**產圖速率。實測速率 8.0 張/小時
#   （數 `output/**/*.png` 的 mtime，13 個完整小時），`images_per_character=120`
#   → **一個角色 15.0 小時 ≈ 28–32 MB**。
# - 所以門檻取 64 MB ＝ 那個量的兩倍：健康的執行不會觸發，而「一個工作階段跑成好幾
#   個角色的長度」（`restart_chrome_every_n_characters=0`，或速率掉到剩幾分之一）
#   仍然會被抓到。
#
# **先前訂 8 MB 是錯的，記下來避免重犯**：當時的依據是「高過正常單角色量 5.4 MB」，
# 但那 5.4 MB 是**某個角色跑到一半**的量，不是跑完的量。結果每個角色都會觸發一次
# 警告——正好變成當初要避免的狼來了。量「跑到一半」當成「跑完」是這裡真正的教訓。
# 順帶不再對齊兩支啟動器的 `LOG_MAX_BYTES`：那兩個管的是行導向的人看記錄，成長
# 曲線完全不同，「全專案同一個數字好記」不是挑門檻的理由。
#
# **保留 256 KB 的依據是「還原一次失敗現場要多少」，不受上面那次重算影響：**
# - 一次完整的 spawn 失敗現場在 `--log-level=INFO` 下量到 **1,090 bytes**
#   （`Starting ChromeDriver` ＋ 帶著全部 Chrome 旗標與 `--user-data-dir` 的
#   `COMMAND InitSession` ＋ `RESPONSE InitSession ERROR session not created`
#   加上 Chrome 那一側的原因）。256 KB 是它的 240 倍。
# - 唯一的讀取端 `_read_tail_text` 一次最多只讀 **64 KiB**，所以保留量必須
#   ≥ 那個視窗，否則修剪完之後 tail 讀到的是被切短的一小截。256 KB ＝ 4 倍。
# - 崩潰後的事後對帳想看的是「死掉之前那幾個 WebDriver 命令」。以上面重量到的
#   37 KB/分鐘計，256 KB ≈ 最後 7 分鐘，夠看出死因。
_CHROMEDRIVER_LOG_MAX_BYTES = 64 * 1024 * 1024
_CHROMEDRIVER_LOG_KEEP_BYTES = 256 * 1024


def _read_tail_text(path: Path, max_bytes: int = 64 * 1024) -> str:
    """讀檔案**尾端**最多 `max_bytes` 的文字，不把整個檔載進記憶體。

    `read_text()` 會讀整份。chromedriver 的記錄在一個角色之內可以長到數十 MB
    （2026-08-29 實測：`--log-level=INFO` 每個 WebDriver 命令約 484 bytes，
    `--verbose` 約 4370），而這個函式被呼叫的時機正好是**Chrome 剛剛起不來**——
    那經常就是記憶體不夠的時候。要看的只有最後幾行，沒有理由付整份的代價。

    從中間切下去第一行通常是半行，所以有 seek 過就丟掉第一行。
    """
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        offset = max(0, size - max_bytes)
        handle.seek(offset)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    if offset and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def _dump_chromedriver_log_tail(lines: int = 25) -> None:
    """Print the tail of `chromedriver.log` to stderr. Selenium's
    `SessionNotCreatedException: Chrome instance exited` carries no root
    cause — the verbose chromedriver log does (version mismatch, profile
    lock, OOM at launch …). Called when a spawn attempt fails.

    讀不到就**明講**。原本這裡是 `except OSError: return`，於是「記錄沒被寫出來」
    與「記錄是空的」都長得跟「一切正常、只是沒東西好印」一模一樣——而
    `ChromeService` 當時傳的是 selenium 根本不認得的 `log_path`，檔案從來沒被
    建立過。整整幾個月，唯一會揭穿這件事的就是這個 helper，而它選擇沉默。

    **`.prev` 也一起印。** 這次 spawn 失敗的 `chromedriver.log` 只會有「這一次
    起不來」的訊息；真正想看的常常是**上一個工作階段**怎麼死的（`_rotate_
    chromedriver_log` 保留下來的那一份）。兩份都印、而且分別標明是哪一份——不標
    的話兩段時間戳混在一起，讀的人會以為是同一個工作階段。
    """
    for path, label in ((_CHROMEDRIVER_LOG_PREV, "chromedriver.prev.log（上一個"
                         "工作階段——「跑到一半死掉」要看的是這一份）"),
                        (_CHROMEDRIVER_LOG, "chromedriver.log（這一次）")):
        try:
            text = _read_tail_text(path)
        except FileNotFoundError:
            if path is _CHROMEDRIVER_LOG_PREV:
                continue          # 第一次啟動還沒有上一份，正常，不必出聲
            print("--- chromedriver.log 讀不到（FileNotFoundError）——"
                  "driver 的詳細記錄沒有被寫出來，這次的 spawn 失敗沒有 root "
                  "cause 可看。檢查 ChromeService 的 log_output 參數。---",
                  file=sys.stderr)
            continue
        except OSError as error:
            print(f"--- {label} 讀不到（{type(error).__name__}）---",
                  file=sys.stderr)
            continue
        tail = text.splitlines()[-lines:]
        if not tail:
            print(f"--- {label} 是空的——driver 還沒來得及寫任何東西 ---",
                  file=sys.stderr)
            continue
        print(f"--- {label} (last {len(tail)} lines) ---", file=sys.stderr)
        for line in tail:
            print(line, file=sys.stderr)
        print(f"--- end {label} ---", file=sys.stderr)


def _trim_chromedriver_log() -> None:
    """把上一個 driver 工作階段留下的 `chromedriver.log` 封頂（保留尾段）。

    **硬性前提：呼叫的當下不得有任何 chromedriver 活著。** 這不是保守寫法，是
    2026-09-06 量出來的——`trim_log` 走的是 `write_bytes()`（截斷後重寫），而
    chromedriver 握著的是一個**位移會留在原地**的檔案控制代碼：

        修剪前 5,170 bytes → 修剪後 746 bytes → 它再寫幾行 → 7,572 bytes，
        其中 **4,424 個是 NUL**（作業系統把中間那段空洞補零）

    也就是說在工作階段中途修剪不但**收不回空間**（檔案立刻長回比修剪前更大），
    還會把要查的東西壓成一片亂碼——`_read_tail_text` 讀回來就是那堆補零。所以
    這支只掛在「舊 driver 已經收掉、新的還沒起來」那條接縫上，也因此**不放在
    `main()` 的 finally**：那裡的 `cur.quit()` 是包在 try/except pass 裡的，
    quit 卡住的時候 chromedriver 還活著，正好踩中上面那個情形。

    修剪點的安全性由呼叫端保證：`build_stealth_driver()` 的兩條正式路徑進來之前
    都剛跑完 `_kill_orphan_chrome()`（開機是 `main()`、週期性重啟是
    `_restart_chrome_session`），那支會 psutil ＋ `taskkill /F /T /IM` 兩輪掃掉
    每一個 `chromedriver.exe`。驗證模式那條路沒有掃（`_SUPPRESS_ORPHAN_SWEEP`），
    但 `verify_browser.py` 會先拿 `_chrome_slot` 鎖並讓位給活著的 `webrunner.pid`，
    所以那裡也不會有我們的 chromedriver 在跑。

    **為什麼健康的執行看起來像沒作用，卻還是要有這一層。**
    chromedriver 每次啟動會**清空** `--log-path` 指的檔（2026-09-06 實測：人工墊到
    50,368 bytes，下次啟動後回到 366 bytes），所以正常情況下真正在封頂的是
    chromedriver 自己，而上限等於「一個 chromedriver 工作階段的量」。這一層是把那個
    上限變成**我們自己持有**的，因為讓它消失的方式不只一種、而且每一種都是無聲的：

    1. `selenium.webdriver.common.service.Service.__init__` 裡真的有
       `if isinstance(log_output, str): self.log_output = open(log_output, "a+")`
       ——**附加模式，永遠只長不消**。目前救我們的只是 `ChromiumService.__init__`
       搶先一步把字串轉成 `--log-path=`（實測 `service.log_output` 是 -3＝DEVNULL）。
       這層攔截哪天不見了，這個檔就變成純附加。
    2. chromedriver 自己有 `--append-log`。有人為了跨重啟對帳而加上去，同樣結果。
    3. 傳的若不是字串而是檔案物件，走的也是 Python 這一側的控制代碼。
    另外，chromedriver 根本沒起來的那種失敗（Selenium Manager 解析不到、連
    埠都綁不上）不會清空這個檔，於是 `_dump_chromedriver_log_tail` 讀到的是
    **上一個工作階段**的尾巴——看起來像診斷、其實是舊資料。

    真正**還沒有**上限的是「單一工作階段內」的成長：實測約 2.15 MB/小時
    （2026-09-07：5.64 小時寫了 12,747,290 bytes）。那段中途修剪不掉（見上），
    所以這裡只能事後封頂 ＋ 講一聲。

    **警告的診斷要說對成因。** 檔案大小 ≈ 單一工作階段長度 × 那個速率，而工作階段
    長度是 `images_per_character` ÷ **實際**產圖速率——`restart_chrome_every_n_
    characters` 是以**角色**為單位、不是以時間為單位，所以它設 1 也完全可能是一個
    十幾小時的工作階段。本機實測就是這個情形：該值＝1、`images_per_character=120`、
    額度節流下 8.0 張/小時 → 一個角色 15 小時 ≈ 30 MB。
    **先前那行警告寫「多半是 restart_chrome_every_n_characters=0」是錯的**：照著
    去查會查到一個設成 1 的設定，然後找不到問題。`=0` 只是眾多讓工作階段變長的
    原因之一，而且不是本機的情況。

    順帶一提，這個檔裡含**實際打進頁面的提示詞內容**（`RESPONSE ExecuteScript
    "…"`）。它已經在 `.gitignore` 裡，但不要隨手貼出去。
    """
    try:
        size = _CHROMEDRIVER_LOG.stat().st_size
    except OSError:
        return
    if size <= _CHROMEDRIVER_LOG_MAX_BYTES:
        return
    print(
        f"chromedriver.log 上一個工作階段留下 {size / (1024 * 1024):.1f} MB"
        f"（上限 {_CHROMEDRIVER_LOG_MAX_BYTES // (1024 * 1024)} MB），"
        f"只保留尾端 {_CHROMEDRIVER_LOG_KEEP_BYTES // 1024} KB。"
        f"這個檔只在 chromedriver **啟動**時清空，所以大小 ≒ 單一 Chrome 工作"
        f"階段的長度 × 約 2 MB/小時；而工作階段長度 ＝ images_per_character ÷ "
        f"**實際**產圖速率，不是 restart_chrome_every_n_characters 的角色數。"
        f"（實測：該值＝1、images_per_character=120、速率 8 張/小時，一個角色仍是 "
        f"15 小時 ≒ 30 MB。）要查就先算那個乘積；真的異常時它會遠大於一個角色的"
        f"份量——工作階段跑成好幾個角色那麼長（例如重啟被停用），或速率掉到剩幾"
        f"分之一。",
        file=sys.stderr)
    trim_log(_CHROMEDRIVER_LOG,
             max_bytes=_CHROMEDRIVER_LOG_MAX_BYTES,
             keep_bytes=_CHROMEDRIVER_LOG_KEEP_BYTES)


def _rotate_chromedriver_log() -> None:
    """把上一個工作階段的 `chromedriver.log` 保留成 `chromedriver.prev.log`。

    **沒有這一層，這個檔就永遠沒有「跑到一半死掉」那次的證據。** chromedriver 每次
    啟動會清空 `--log-path` 指的檔，所以流程是：chromedriver 死掉 → webrunner
    rc=1 → 監督者重生 → 新的 chromedriver 開同一個路徑並**截斷它**。實測
    （2026-09-07）：11:44:54 崩潰、11:44:59 重生，而磁碟上那份 `chromedriver.log`
    的第一筆是 **11:45:02**——崩潰後 8 秒。唯一可能記載死因的東西，被下一個
    chromedriver 蓋掉了，而且沒有任何副本。

    **這推翻了 `_dump_chromedriver_log_tail` 原本的理由。** 它的 docstring 說
    「Selenium 的例外講不出成因、verbose 的 chromedriver log 講得出」——對**啟動
    失敗**成立（那份 log 就是當次失敗的），對**跑到一半死掉**完全不成立：診斷工具
    自己把它存在的目的所需要的證據銷毀了。

    位置與 `_trim_chromedriver_log` 完全相同、理由也相同：必須在
    `ChromeService(...)` **之前**，那之後這個路徑就有 chromedriver 的行程握著了。
    保留動作用 `os.replace`（同一個磁碟區的原子改名），所以不會出現「保留到一半
    被砍掉」留下半份的情形。

    **`.prev` 也納入同一個磁碟上限**：兩個檔各自最多
    `_CHROMEDRIVER_LOG_MAX_BYTES`，合計上限翻倍是刻意接受的——一份 64 MB 的上限
    本來就只有在異常時才會碰到（實測一個角色約 30 MB），而付這一倍換到的是「下一次
    不明原因的死亡有證據可查」。先修剪再改名，所以 `.prev` 拿到的一定是已封頂的。
    """
    try:
        if not _CHROMEDRIVER_LOG.exists():
            return
        os.replace(_CHROMEDRIVER_LOG, _CHROMEDRIVER_LOG_PREV)
    except OSError as error:
        # 純診斷輔助，絕不能擋住起 driver——但要出聲，否則「沒有 .prev」與
        # 「保留失敗」在磁碟上長得一模一樣。
        print(f"保留上一份 chromedriver.log 失敗（{type(error).__name__}）："
              f"這一輪若崩潰將沒有前一個工作階段的記錄可查。", file=sys.stderr)


# ---------- file helpers -----------------------------------------------------

class CredentialsError(RuntimeError):
    """憑證檔在，但讀不成一組可用的憑證。

    **型別化的理由與 `QueueDecodeError` 同源：裸的錯誤指著錯的地方。** 編碼不對
    時 traceback 最後一行是 `read_text`，看起來像「檔案讀不到」——實際上檔案好好
    的在那裡，只是編碼不對；欄位缺漏時丟的是裸 `KeyError`，只看得到一個字串，
    看不出那是憑證檔缺了欄位。而這個讀取在兩支變體的 `main()` 裡**都沒有 try**，
    所以它是整輪唯一的死因出口：訊息講不出「該做什麼」，代價就是有人得從一行
    `read_text` 反推回去。

    **這個例外的訊息絕不含憑證值，而且那不是美觀問題。** 它只帶檔名、位元組
    位置與 `error.reason`（編碼那一種），或缺漏的欄位名（欄位那一種）——不帶那個
    解不開的位元組值、不帶任何已解碼的內容、也不帶 `creds` 的任何鍵值。裸的
    `UnicodeDecodeError` 做不到這一點：它的訊息會把出問題的位元組值印出來
    （`can't decode byte 0xff in position 12`），而這個檔案**整份都是憑證**。
    訊息會進 log，而 log 送得到聊天平台，所以這條性質屬於外送邊界。

    同樣的理由，編碼那一種刻意用 `raise ... from None` 收掉例外鏈——這一點與
    `_queue_decode_error` 的 `from error` **不同**，是刻意的分歧：佇列檔的內容
    本來就不是秘密，保留鏈結換到的是更完整的診斷；憑證檔不是，而我們的訊息已經
    帶了位置與原因，也就是人要修好它所需要的全部，鏈結只多帶那一個位元組值。

    **只包「檔案在、但內容不對」。** `FileNotFoundError` 與其餘 `OSError`
    （不存在／權限）照舊原樣往外拋：那是另一件事，呼叫端的註解也是照那樣寫的。
    """


def missing_credentials_message(path: Path) -> str:
    """憑證檔不存在時要印的那段話。純函式，兩支變體共用一份。

    **它取代的是一份 traceback。** 全新 clone 第一次跑批次一定會走到這裡，而裸的
    `FileNotFoundError` 給的最後一行是 `read_text`，看起來像「檔案讀不到」——讀的
    人得自己反推「這個檔案是什麼、值要去哪裡拿、格式長什麼樣」。這三件事就是下面
    這段話的全部內容。

    訊息只帶**檔名**，不帶完整路徑：這一行會進 `webrunner.log`，而 log 送得到聊天
    平台（`/log tail`），主機路徑在那裡是禁止外送的。
    """
    example = path.name.replace(".md", ".example.md")
    return (
        f"webrunner: 找不到 {path.name}。這是出圖服務的登入帳密，repo 裡刻意沒有"
        f"這個檔案。請把 {example} 複製成 {path.name}，改成你自己的帳號：" + "\n"
        + f"    username: <你的帳號>" + "\n"
        + f"    password: <你的密碼>" + "\n"
        + "（以第一個冒號分隔，所以密碼裡可以有冒號。）詳細步驟見 docs/setup.md。")


def read_credentials(path: Path) -> tuple[str, str]:
    """讀憑證檔，回 `(username, password)`。

    **解析語意是契約的一部分，不要動**：以第一個 `:` 分隔（所以值裡可以有冒號）、
    key 取 `strip().lower()`、value 取 `strip()`、沒有冒號的行略過、重複的 key
    後者覆蓋前者。

    「檔案在、但讀不成一組憑證」一律轉成 `CredentialsError`（理由見該類別），
    「檔案不在／權限」維持原樣往外拋。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        # 只帶位置與原因，不帶 `error.object`、也不帶那個位元組的值；`from None`
        # 的理由見 `CredentialsError`（被鏈上去的原始例外會把位元組值印出來）。
        raise CredentialsError(
            f"{path.name} 不是 UTF-8：第 {error.start} 個位元組起解不開"
            f"（{error.reason}）。請把這個檔案重新存成 UTF-8 再跑。"
        ) from None
    creds: dict[str, str] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        creds[key.strip().lower()] = value.strip()
    # 只講缺了哪個欄位名——那兩個名字是寫在這裡的字面值，不是讀進來的資料，所以
    # 這句話不可能夾帶憑證內容。讀到了什麼（含其他鍵名）一個字都不印。
    missing = [field for field in ("username", "password") if field not in creds]
    if missing:
        raise CredentialsError(
            f"{path.name} 少了 {'、'.join(missing)} 欄位。請確認每一行都是"
            f"「欄位名: 值」的格式（`username` 與 `password` 兩行都要有）。"
        )
    return creds["username"], creds["password"]


class QueueDecodeError(RuntimeError):
    """佇列檔（`todo_*.md` 與它們的 fallback）不是 UTF-8，讀不出來。

    **這裡刻意跟本模組其他載入器相反：不退回空值，直接拋。** 那些載入器（設定
    檔、鎖檔、進度檢查點）退回預設是安全的，因為「預設」跟「讀到的值」在下游
    分得開。佇列不是——「空」在下游是一個**合法且會改變行為的值**：

    - `read_queues()` 看到某條佇列是空的，會替換成 fallback 檔（`prompt.md` /
      `character1.md` / `character2.md` / `undesired.md`）。所以「讀不出來」會
      安靜地變成「拿另一份內容產圖」，而且因為 fallback 模式不 pop，跑完還會
      回 **rc=0** 說一切正常。使用者看到的是「跑完了」，拿到的是錯的圖。
    - `todo_character2.md` 的空白列本身就是「這一配對不要 Character 2」；
      `undesired.md` 空字串本身就是「不設負面提示詞」。下游沒有任何辦法把
      「真的空」跟「解不開」分開。

    而且沒有任何重試救得回來——要有人把檔案重新存成 UTF-8。所以正確的形狀是
    **在開 Chrome 之前就大聲失敗**：`run_preflight()` 在 boot 之前呼叫
    `read_queues()`，行程會在一秒內死掉，監督者的 rapid-fail giveup
    （`rapid_fail_threshold_sec` 30 秒內連續 `rapid_fail_giveup_count` 5 次）
    收手並通知，所以不會變成無限重生。

    半寫入正好切在多位元組字元中間不是理論情形：`write_todo_characters` 為了讓
    編輯器靜默重載而**刻意就地覆寫、不做原子寫入**（理由見該函式 docstring），
    佇列檔正是這個 repo 裡唯一放棄寫入原子性的一類檔案。
    """


def _queue_decode_error(path: Path, error: UnicodeDecodeError) -> QueueDecodeError:
    """把裸的 `UnicodeDecodeError` 換成一個講得出「該做什麼」的錯誤。

    裸的那個 traceback 最後一行是 `read_text`，看起來像「檔案讀不到」；實際上
    檔案好好的在那裡，只是編碼不對。訊息裡帶檔名與位元組位置，人才有得查。
    """
    return QueueDecodeError(
        f"{path.name} 不是 UTF-8：第 {error.start} 個位元組起解不開"
        f"（{error.reason}）。請把這個檔案重新存成 UTF-8 再跑。"
    )


def read_text_safe(path: Path) -> str:
    """Fallback 檔的讀取端。**只吞「檔案不存在」**——不存在代表這條 fallback 沒
    設定，回空字串是正確語意。其餘失敗（權限、編碼）一律往外拋，理由見
    `QueueDecodeError`。"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError as error:
        raise _queue_decode_error(path, error) from error


def read_todo_characters(path: Path, *, preserve_blank: bool = False) -> list[str]:
    """One entry per line. Trailing punctuation (including 「，」) within an
    entry is preserved — only newlines split entries. Non-breaking spaces
    (U+00A0) that often sneak in from copy/paste are normalised to regular
    spaces so NovelAI prompts don't carry invisible junk.

    檔案不存在 → `[]`（佇列沒建立過，等同空佇列）。檔案在但不是 UTF-8 →
    **拋 `QueueDecodeError`，不回 `[]`**；為什麼那個方向才對，見該類別的
    docstring。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as error:
        raise _queue_decode_error(path, error) from error
    if raw == "":
        return []
    lines = [line.replace("\xa0", " ").strip()
             for line in raw.splitlines()]
    if preserve_blank:
        # todo_character2.md is positional: an empty row means this image must
        # remove/disable Character 2. Dropping it would shift every later
        # prompt forward and reuse the wrong character.
        return lines
    return [line for line in lines if line]


def write_todo_characters(path: Path, entries: list[str]) -> None:
    """Persist a todo list back to disk, one entry per line.

    刻意用「就地覆寫」(path.write_text 同 inode)，**不要**改回 os.replace
    原子寫入：使用者常把 todo_prompt.md 開在 IDE 裡看著佇列即時消化，
    os.replace 換掉 inode 會被 PyCharm 當成「檔案被刪掉又重建」、每次 pop 都
    跳 memory/disk 對話框；就地覆寫則讓沒有未存編輯的開啟檔靜默重載、不跳框。
    代價是放棄原子性——但 todo 檔小、寫入毫秒級，硬殺剛好命中寫入窗的機率極
    低，且 bot 來源編輯有 .backup/ + reconcile 保護。中斷續產的關鍵是 resume
    checkpoint（webrunner_progress.json），那個在 _run_progress 仍維持原子。
    On-disk 契約：非空清單以 "\n" 連接再加一個結尾 "\n"；空清單寫成 0 byte。"""
    text = "\n".join(entries)
    path.write_text(text + ("\n" if entries else ""), encoding="utf-8")


def reconcile_todo_with_disk(path: Path, remaining: list[str], *,
                             preserve_blank: bool = False) -> list[str]:
    """Re-read `path` before a pop rewrites it. If the on-disk queue diverges
    from our in-memory `remaining` (an external editor or the bot changed it
    mid-run), back up the RAW disk bytes to .backup/ and adopt the disk
    version as the new authority so we never silently clobber the edit.
    Returns the list to treat as current (disk on divergence, else
    `remaining` unchanged)."""
    # 用 read_todo_characters 讀，讓比較兩邊套用相同正規化（NBSP 轉空白；
    # 一般 queue 跳過空白行，Character2 保留位置空白），避免排版差異造成
    # 假性 divergence。
    disk = read_todo_characters(path, preserve_blank=preserve_blank)
    if disk == remaining:
        return remaining  # 常見的「沒被外部改動」路徑，純 no-op。
    # 偵測到 divergence：把磁碟上的「原始」位元組（未正規化）備份起來，
    # 採用磁碟版本當作新的權威來源，絕不蓋掉使用者 / bot 的編輯。
    #
    # 這裡走 read_bytes / write_bytes，**不解碼**。備份的職責是位元組級保真，
    # 解一次碼再編回去既沒必要又會失真（`write_text` 會把 "\n" 翻成
    # os.linesep）。更要緊的是：原本是 `read_text` + `except OSError: raw = ""`，
    # 而 `UnicodeDecodeError` 是 `ValueError` 的子類別、**不是** `OSError`——
    # 檔案不是 UTF-8 的時候那句會直接拋，正好是最需要留下備份的時候。就算把它
    # 加進 except 元組也不對：那條路會寫出一個**空**的備份檔，然後照樣印
    # 「原磁碟內容已備份到 X」——宣稱保住了、實際什麼都沒保住。備份存不下來就要
    # 講「沒存下來」。
    backup_name = ""
    try:
        raw = path.read_bytes() if path.exists() else b""
        # 與 discord_bot.py 的 _backup_path_for 同樣的命名規則（時間戳 + 毫秒），
        # 讓 bot 的 _reconstruct_undo_stack / !undo 能撿到這份備份。不可 import
        # discord_bot（webrunner 不得 import bot），故在此就地重做這段命名。
        backup_dir = PROJECT_ROOT / ".backup"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.time()
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(ts))
        ms = f"{int(ts * 1000) % 1000:03d}"
        backup = backup_dir / f"{path.name}.{stamp}.{ms}.bak"
        backup.write_bytes(raw)
        backup_name = backup.name
    except OSError as error:
        # `!r` 刻意保留：只收得到 `OSError`，而 `str()` 會把 `.backup/` 的完整
        # 主機路徑帶進 log。`repr()` 看得到 errno 與原因，看不到路徑。
        print(f"reconcile_todo_with_disk: {path.name} 備份失敗: {error!r}",
              file=sys.stderr)
    if backup_name:
        print(f"  WARN: {path.name} 在執行期間被外部修改；磁碟版本優先，"
              f"原磁碟內容已備份到 {backup_name}（不覆寫該編輯）")
    else:
        print(f"  WARN: {path.name} 在執行期間被外部修改；磁碟版本優先，"
              f"但原磁碟內容**備份失敗**，這次編輯無法用 undo 復原")
    return disk


# Windows 保留裝置名（微軟「Naming Files, Paths, and Namespaces」列的整組，含
# `COM0`／`LPT0` 與上標變體）。比對對「第一個 `.` 之前那一段」做、且大小寫不敏感
# ——文件明說「這些名字後面直接接副檔名」一樣保留。
#
# ⚠️ **本機實測的行為與直覺不同，改這一段之前先看這裡**（Windows 11、
# CPython 3.14）：`CON`／`AUX`／`PRN`／`COM1`／`NUL.txt` 當**目錄**其實都建得起來
# 也寫得進去；真正壞掉的只有 `NUL` 一個，而它的壞法是最糟的那種——
# `Path(box / "NUL").mkdir(parents=True, exist_ok=True)` **不會拋**（`exists()`
# 回 True、`is_dir()` 回 False，因為那是 null 裝置），所以 `generate_loop` 一路
# 往下跑，然後**每一張圖**的寫入都 `FileNotFoundError`。也就是說症狀不是「批次
# 當場炸掉」，是「整個角色一張都存不下來」，最後靠 `consecutive_fail_abort`
# 收場。整組都擋掉是刻意的：這是平台與版本相關的行為，不值得為了一個沒有人
# 會拿來當角色名的字串去賭下一版 Windows 還是這樣。
_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"COM{i}" for i in range(10)] + ["COM¹", "COM²", "COM³"]
    + [f"LPT{i}" for i in range(10)] + ["LPT¹", "LPT²", "LPT³"]
)


def _is_safe_folder_component(name: str) -> bool:
    """True iff `name` 可以接在 `OUTPUT_ROOT` 後面當**一層、且留在裡面**的資料夾名。

    **這是白名單斷言，不是字元黑名單**——差別就是 2026-09-10 那個缺陷的成因：
    `character_folder_name` 的 `re.sub(r'[\\\\/:*?"<>|]+', ...)` 沒有 `.`，於是
    佇列裡一行 `..` 原封不動變成資料夾名，而 `Path("output") / ".."`
    **不會**被 pathlib 正規化（`parts=('output','..')`），`allocate_output_dir`
    在那之後直接回傳它，`.resolve()` 就是 `PROJECT_ROOT`——一整個角色的圖安靜地
    寫進 repo 根目錄，沒有例外也沒有警告。字元黑名單少列一個就破功，而且破功
    時完全無聲；同樣的教訓 `discord_bot._is_unsafe_folder_name` 的 docstring
    早就記過（它漏的是磁碟機冒號），只是沒有套用到這第二份。

    各條規則**互不重疊**是刻意的（重疊的規則會互相遮蔽，變異測試會誤判成
    「有守住」），所以每一條在下面都有一個只踩它的測試輸入：

    | 規則 | 只踩這一條的輸入 |
    |---|---|
    | 非空 | **沒有**——刻意重複，理由見下 |
    | 頭尾沒有空白、尾端沒有點 | `"a."`、`"a "` |
    | 沒有 `< 0x20` 的控制字元 | `"a\\tb"` |
    | 不含 `..` | `"a..b"`、`"..foo"` |
    | 沒有磁碟機／根、剛好一層 | `"C:x"`、`"a/b"` |
    | 首段不是保留裝置名 | `"NUL"`、`"con"` |

    **控制字元那條不是順手加的**：`"a\\tb"` 在本機 `mkdir` 會拋
    `OSError 123`（ERROR_INVALID_NAME），而 `generate_loop` 的
    `out_dir.mkdir(...)` 外面沒有 try，例外會一路衝到 `run_batch` 的外層
    handler → `critical_error` → rc=1 → 監督者重生 → 下一輪讀到同一行佇列
    再炸一次。佇列裡混進一個 tab 是很平常的事（貼上來的）。

    **`..` 那條擋的不是路徑穿越**（穿越已經被「剛好一層」那條擋掉了），是
    **bot 與 webrunner 的鏡像一致性**：`discord_bot._is_unsafe_folder_name`
    用子字串 `".." in name` 判定，所以只要我們產得出 `"a..b"` 這種名字，
    webrunner 就會建一個 bot 打不開的資料夾（`/out sample` 對那個角色直接
    失敗）。把它擋在投影這一端，兩邊才對得起來。代價是角色名裡真的有連續
    兩點時會退回預設名，很罕見，而且退回是安全的。

    **「非空」那條在今天的 stdlib 下是重複的，但它有自己的測試，不要拿掉。**
    `PureWindowsPath("").parts` 在本機（CPython 3.14.4）是 `()`，所以下面「剛好
    一層」那條已經擋掉空字串了。留著它是因為那是 **stdlib 的實作細節，而且真的
    變過**：`PurePath("")` 在 3.12 之前給的是 `PurePath('.')`。哪天它再變回
    `parts == ('.',)`（長度 1），空字串就會一路通過，而 `OUTPUT_ROOT / ""`
    **就等於 `OUTPUT_ROOT` 自己**（實測 True）——整個角色的圖倒進 `output/`
    根目錄，把 `allocate_output_dir` 的編號邏輯一起弄亂，而且一樣是無聲的。
    空字串**確實到得了這裡**（`".."` 經過上面那道尾端點正規化就是空字串），
    所以這不是防禦一個不可能的輸入。

    ⚠️ **它的守門在
    `test_webrunner_shared.test_the_empty_name_rule_holds_if_pathlib_reverts_to_its_pre_312_shape`，
    做法是 monkeypatch 這個模組的 `PureWindowsPath`。** 單純斷言
    `_is_safe_folder_component("") is False` **殺不掉**「拿掉這條」的變異——變異
    套用之後那句照樣是 False，因為擋它的是別條規則。一條規則今天被別條遮住時，
    唯一問得出「它自己行不行」的方法就是把遮住它的那個前提拿掉。
    （2026-09-10 之前這裡寫的是「變異會存活，已知且列冊」——一個長期掛在變異
    報告上的 SURVIVED 就是在邀請下一個人把它刪掉，所以改成寫測試。）
    """
    if not name:
        return False
    if name != name.strip() or name != name.rstrip("."):
        # Windows 建立目錄時會**默默**砍掉尾端的點與空白（實測：`mkdir("a.")`
        # 產出的是 `a`），所以留著這種名字會讓磁碟上的名字與檢查點記的名字
        # 不一致，而且 `"a"` 與 `"a."` 會撞進同一個資料夾。
        return False
    if any(ord(ch) < 32 for ch in name):
        return False
    if ".." in name:
        return False
    pure = PureWindowsPath(name)
    if pure.drive or pure.root or len(pure.parts) != 1:
        # `PureWindowsPath` 而不是 `Path`：`Path` 在 POSIX 上不認得 `C:` 是
        # 磁碟機，同一支測試會在 Linux 上綠、在 Windows 上才紅。判定與平台
        # 無關（理由與 `_is_unsafe_folder_name` 那邊一字不差）。
        return False
    return name.split(".", 1)[0].upper() not in _RESERVED_DEVICE_NAMES


def _is_single_path_component(name: str) -> bool:
    """True iff `name` 接到任何目錄後面時仍然只是**一層**——不會改到目錄。

    這是比 `_is_safe_folder_component` **弱**的一條，刻意的。兩者的用途不同：

    | 守衛 | 問的問題 | 用在 |
    |---|---|---|
    | `_is_safe_folder_component` | 能不能當一個**資料夾**名 | `output/` 底下的角色資料夾 |
    | 這一支 | 接完之後還是不是單一元件 | 診斷截圖的**檔名**、LevelDB 的 `CURRENT` |

    對檔名套用前者會誤擋：它禁止尾端的點與 `..` 子字串，而
    `f"ready_{char_name[:30]}"` 只要剛好切在一個點後面就會被擋掉，代價是一張
    診斷截圖無聲消失——一個會亂叫的守門就是一個會被關掉的守門。

    `PureWindowsPath` 不是 `Path`：POSIX 的 `Path` 不把 `\\` 當分隔符，用 `Path`
    會讓判定跟著平台跑（同 `_is_safe_folder_component` 那邊一字不差的理由）。

    ⚠️ **它同時是接合守門認得的守衛名之一**（`test_bot_helpers._JOIN_GUARDS`）。
    抽成具名函式不只是為了好讀：守衛性是靠「這個名字有沒有被餵進某支守衛」認出來
    的，寫成行內運算式的話，接合站點會被判成沒守住。

    ⚠️ **裸的 `".."` 要自己列一條，因為底下那個 round-trip 會放它過去**
    （2026-09-11 補）。三行實測擺在一起，就看得出這裡從來沒有過政策：

        PureWindowsPath(".").name  == ""    ->  "" != "."   ->  擋掉
        PureWindowsPath("..").name == ".."  ->  原樣通過     ->  放行
        PureWindowsPath("").name   == ""    ->  擋它的是 bool(name)

    也就是說 `.` 與 `..` 的答案**都是 pathlib 正規化的副作用，只是剛好一對一
    錯**。那個不對稱不是「有人只擋到一半」——是整個判斷被外包給一個述詞，而那個
    述詞問的是「能不能原樣通過正規化」，不是「會不會改到目錄」。實測
    `base / ".."` 的 `mkdir` 與寫入**都會成功**（落在上一層），正是本函式第一句
    宣稱擋掉的事。

    今天三個呼叫點都交不出裸的 `".."`：`snap` 守的是 `f"debug_{tag}.png"`，那個
    字面前綴是結構性保證；`_leveldb_manifest_ok` 前一行有
    `startswith("MANIFEST-")` 閘。所以這是**潛伏缺陷不是現行漏洞**。修它有兩個
    理由：讓第一句的合約成真，以及讓 `_JOIN_GUARDS` 的帳目不再把它記成「已守
    住」——那支只問「名字有沒有被守衛看過」，不問看過之後怎麼處置，所以守衛自己
    的漏洞在它的帳上是綠的。

    **刻意只收裸的 `".."`。** `"..."` / `" .."` 實測會塌回 base，但接下來的寫入是
    `FileNotFoundError`——**大聲失敗**，跟 `".."` 的無聲寫到上一層不同級；
    `"a.."` / `"..a"` / `"a..b"` 留在 base 底下、寫得進去，是**合法檔名**。往那個
    方向收緊會當場擋掉 `"debug_ready_a."`，而那正是
    `test_is_single_path_component_is_weaker_than_the_folder_guard` 用來證明「這支
    比 `_is_safe_folder_component` 弱」的輸入——收緊等於把 §8.33 裁定不可合併的
    三層嚴格度合併掉。實測 61 筆語料，這次修正只改變 **1** 筆，就是裸的 `".."`。

    `"."` 不寫成明文特例，是為了不製造一個必然存活的變異（`name in (".", "..")`
    改成 `name == ".."` 照樣全綠，因為擋 `"."` 的是別的機制）。它的前提改由測試
    斷言 `PureWindowsPath(".").name != "."` 釘住——同 `_is_safe_folder_component`
    那邊 `_PWP("").parts` 那一格的作法。
    """
    if name == "..":
        return False
    return bool(name) and PureWindowsPath(name).name == name


def character_folder_name(prompt: str) -> str:
    """Use the first half-width-comma segment as the human-readable name.

    投影出來的一定是「`output/` 底下的**單一層**資料夾名」——不合格就退回預設
    的 `"character"`（由 `allocate_output_dir` 去編號）。判定在
    `_is_safe_folder_component`，那支的 docstring 有完整理由。

    ⚠️ **這個函式只能往「更嚴」的方向改。** `discord_bot._alert_mentions()`
    的 docstring 明寫警報訊息之所以不是 ping 放大器，靠的就是這裡的 `re.sub`
    順手把 `<` `>` 換成底線（角色名是使用者用 `/todo char1 add` 填的），並警告
    「哪天有人放寬它，或改成 POSIX 的字元集，這裡就會安靜地變回一個 ping 放大
    器」。所以 2026-09-10 的修正是**保留原本的替換、在後面加一層白名單斷言**，
    不是把黑名單換成一組更寬鬆的字元集。要動 regex 之前先讀那一段。

    三個消費端：`run_batch`（真正建資料夾）、事件與進度紀錄、以及 bot 的
    `/gen plan` 預覽（`discord_bot.py` 匯入這一支）。守衛只能放在這裡——放到
    `allocate_output_dir` 會讓預覽與實跑分岔，正是模組邊界規則在防的漂移。
    """
    head = prompt.split(",", 1)[0].strip() or "character"
    name = re.sub(r'[\\/:*?"<>|]+', "_", head)[:120]
    # 先正規化尾端的點與空白，再斷言。順序不能反：Windows 自己就會砍掉它們，
    # 所以砍掉之後的名字才是磁碟上真正的名字（`"Mr. Smith."` → `"Mr. Smith"`，
    # 身分保住了）。切到 120 字也可能剛好切出一個尾端點或空白。
    # 附帶效果：`"."`／`".."`／`"..."` 砍完都是空字串，一條規則就收乾淨——而且
    # 三個都是真的有害，但**害法各不相同**（本機實測，CPython 3.14／Windows 11；
    # 三個都 `resolve()` 得出乾淨的答案，差別全在「寫得進去嗎」）：
    #
    #   `output/..`   → 解析成 `PROJECT_ROOT`，**而且寫得進去**：圖直接落在
    #                   專案根目錄。這是原本那個逃逸。
    #   `output/.`    → 解析成 `output` 自己，**也寫得進去**：圖倒進 `output/`
    #                   根目錄，把 `allocate_output_dir` 的編號邏輯一起弄亂。
    #   `output/...`  → 也解析成 `output` 自己、`is_dir()` 還回 True，但
    #                   **一個位元組都寫不進去**（`FileNotFoundError`）。所以它
    #                   不是「倒進 output/」，是每一張圖都失敗，一路撞到
    #                   `consecutive_fail_abort` 把整批收掉。`"...."` 同。
    #
    # ⚠️ 這段原本把 `.` 與 `...` 寫成同一種行為。會特地訂正是因為**這個函式的
    # docstring 寫錯過兩次**（見上面的紀錄），而每一次的代價都是下一個人照著錯的
    # 敘述去推論。`is_dir()` 回 True 卻寫不進去，正是那種「看起來已經驗過了」的
    # 形狀——判準要是「寫得進去嗎」，不是「解析成什麼」。
    name = re.sub(r"[.\s]+\Z", "", name)
    return name if _is_safe_folder_component(name) else "character"


# ---------- 批次期間不要讓作業系統打斷這個行程 -------------------------------
#
# 2026-09-03 補、2026-09-20 訂正（訂正的內容在下面那一段，請連著讀）。無人值守
# 的批次要跑好幾個小時，而這台機器**只支援 S0 低電源閒置**（Modern Standby；
# `powercfg /a` 顯示 S1/S2/S3 全部不支援），系統事件記錄裡從 05-26 起有 1400 次
# 「進入待命」——平均一天 14 次。沒有任何保護的話，待命期間行程會被 PLM（行程
# 生命週期管理）暫停，批次就不會前進，而且從外面完全看不出來：行程還活著、沒有
# 崩潰、log 就只是停在那裡。
#
# **為什麼不是只用 `SetThreadExecutionState`。** 那是 S3 時代的 API；微軟的文件
# 明說它**擋不住** Modern Standby 的轉換。S0ix 機器要用電源要求物件
# （`PowerCreateRequest` ＋ `PowerSetRequest`）搭配 `PowerRequestExecutionRequired`
# ——那個要求型別本身就是「Modern standby only」。所以這裡以電源要求為主、
# `SetThreadExecutionState` 為備援（給還有 S3 的舊機器）。
#
# **訂正（2026-09-20，實測）：電源要求保的是「行程」，不是「系統」。**
# `PowerRequestExecutionRequired` 的定義是「the calling process continues to run
# instead of being suspended or terminated by process lifetime management (PLM)
# mechanisms」——它讓**本行程**在 Modern Standby 期間繼續執行；它**不會**讓系統
# 不進入 Modern Standby。只有在**傳統 S3** 機器上，一個生效中的
# `PowerRequestExecutionRequired` 才會順帶隱含 `PowerRequestSystemRequired`，
# 也就是只有那種機器上它才附帶擋住系統待命。這台機器沒有 S3，沒有那個附帶效果。
#
# 實測（2026-09-20，而且是在持有電源要求的情況下）：系統記錄
# `Microsoft-Windows-Kernel-Power` 在 `04:25:49` 記下
# 「[506] 系統正在進入現代待命 原因：Idle Timeout.」，而 `WEBRunner.log` 在那之後
# 照樣一路產圖（05:04、05:05、……、09:32），中間沒有任何「[507] 結束現代待命」。
# **系統真的睡了，批次也真的繼續跑。**
#
# **結果一樣，敘述不一樣——而那個差別會害下一個人查錯方向。** 對批次而言「照樣
# 一直產圖」正是我們要的結果，所以很容易覺得這只是措辭問題。不是：2026-09-20
# 當天就是因為這裡原本寫著電源要求連 Modern Standby 一起擋，才去追「顯示卡當機
# 是不是待命造成的」——最後是靠基準率排掉的（近三個月 1426 段已結束的待命區間
# 佔整個視窗時間的 34.6%，而 12 次可判定的當機有 4 次落在待命中；4/12 ≒ 33%，
# 正好是基準率，沒有相關性）。一句寫錯的註解換掉一次真正的除錯時間。
#
# **※ 用電池跑的時候這個要求會被系統撤銷。** 微軟文件：Modern Standby 機器在
# **DC 電源**下，`system` 與 `execution required` 兩種電源要求會在「系統睡眠逾時
# 過後 5 分鐘」被終止。也就是拔掉電源之後這個保護撐不過那 5 分鐘，行程就回到會被
# PLM 暫停的狀態——而症狀正是這整段註解當初要防的那一個：行程還在、log 只是停住、
# 從外面看不出來。要無人值守跑長批次就**插著電**；這一條程式端無解，只能寫在這裡
# 讓下一個人查得到。
#
# **刻意不要求螢幕保持開啟**（沒有 `ES_DISPLAY_REQUIRED`／`PowerRequestDisplay
# Required`）：批次不需要看得見的螢幕，而讓別人的螢幕整夜亮著是很沒禮貌的事。
#
# **ctypes 的 `argtypes`／`restype` 一定要寫**，理由與 CLAUDE.md 那條 PID 存活探測
# 完全相同：`PowerCreateRequest` 回的是 64 位元 HANDLE，預設 `c_int` 會把它截斷，
# 而**截斷後的 handle 仍然非零**，所以 `if not handle` 抓不到，整個功能會安靜地
# 退化成「以為要求成功了、其實沒有」。
_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_POWER_REQUEST_EXECUTION_REQUIRED = 3      # 保的是「本行程不被 PLM 暫停」
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class StayAwake:
    """批次期間請求作業系統不要打斷這個行程；`release()` 或行程結束就解除。

    **永不 raise**：這是加分項，不是批次的必要條件。拿不到就印一行往下跑——為了
    一個省電設定而讓整批產圖起不來，方向是反的。

    `active` 說明實際拿到了什麼：

    * `"power-request"` —— `PowerRequestExecutionRequired`。它保的是**本行程**：
      Modern Standby 期間不被 PLM 暫停或終止。它**不會**讓系統不進入 Modern
      Standby（2026-09-20 實測；完整證據與「為什麼這個差別很重要」見上面那段
      區塊註解）。只有傳統 S3 機器上它才順帶隱含 `PowerRequestSystemRequired`。
      另外：DC 電源下它會在系統睡眠逾時後 5 分鐘被系統撤銷。
    * `"execution-state"` —— 舊的 `SetThreadExecutionState` 旗標。只擋得住舊的
      S3 閒置睡眠；Modern Standby 機器上它既擋不住待命，也保不住這個行程。
    * `None` —— 兩個都沒拿到。

    兩者對批次的**結果**可能看起來一樣（照樣一直產圖），但保證的東西不同，
    所以 `run_batch` 那三行 log 會把 `active` 的字面值一起印出來。
    """

    def __init__(self) -> None:
        self.active: str | None = None
        self._handle = None
        self._kernel32 = None

    def acquire(self, reason: str = "axiomatic batch is generating") -> str | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            import ctypes.wintypes as wt
        except Exception:  # pylint: disable=broad-except
            return None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._kernel32 = kernel32

            class _Context(ctypes.Structure):
                _fields_ = [("Version", wt.ULONG),
                            ("Flags", wt.ULONG),
                            ("SimpleReasonString", wt.LPWSTR)]

            kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_Context)]
            kernel32.PowerCreateRequest.restype = wt.HANDLE   # 截斷會讓探測失效
            kernel32.PowerSetRequest.argtypes = [wt.HANDLE, ctypes.c_int]
            kernel32.PowerSetRequest.restype = wt.BOOL

            context = _Context(_POWER_REQUEST_CONTEXT_VERSION,
                               _POWER_REQUEST_CONTEXT_SIMPLE_STRING, reason)
            handle = kernel32.PowerCreateRequest(ctypes.byref(context))
            # INVALID_HANDLE_VALUE 是 -1，不是 0——只檢查 falsy 會漏掉它。
            if handle and handle != wt.HANDLE(-1).value:
                if kernel32.PowerSetRequest(
                        handle, _POWER_REQUEST_EXECUTION_REQUIRED):
                    self._handle = handle
                    self.active = "power-request"
                    return self.active
                kernel32.CloseHandle(handle)
        except Exception as error:  # pylint: disable=broad-except
            # `!r` 刻意保留：`try` 裡只有 ctypes／WinDLL，碰不到 driver。
            print(f"  [power] 電源要求拿不到（{error!r}）；改用舊 API",
                  file=sys.stderr)

        # 備援：還有 S3 的機器靠這個就夠；S0ix 機器上它既擋不住待命、也保不住
        # 行程不被 PLM 暫停，但拿著也沒有壞處。
        try:
            import ctypes
            import ctypes.wintypes as wt
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetThreadExecutionState.argtypes = [wt.DWORD]
            kernel32.SetThreadExecutionState.restype = wt.DWORD
            if kernel32.SetThreadExecutionState(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED):
                self._kernel32 = kernel32
                self.active = "execution-state"
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        return self.active

    def release(self) -> None:
        """冪等、永不 raise（會從 `finally` 被呼叫，可能已經放過了）。"""
        kernel32, handle, active = self._kernel32, self._handle, self.active
        self._handle, self.active = None, None
        if kernel32 is None:
            return
        try:
            if handle is not None:
                kernel32.PowerClearRequest(handle,
                                           _POWER_REQUEST_EXECUTION_REQUIRED)
                kernel32.CloseHandle(handle)
            elif active == "execution-state":
                kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass


# ---------- timing / anti-bot pacing ----------------------------------------

def human_pause(lo: float = 0.4, hi: float = 1.2) -> None:
    time.sleep(random.uniform(lo, hi))


def human_type(element, text: str, slow: bool = True) -> None:
    if not slow:
        element.send_keys(text)
        return
    for ch in text:
        element.send_keys(ch)
        time.sleep(random.uniform(0.02, 0.07))


# ---------- retry harness ----------------------------------------------------

def with_retry(label: str, func, max_attempts: int = 3,
               sleep_range: tuple[float, float] = (2.0, 4.0)) -> bool:
    """Run `func` (a zero-arg callable returning truthy on success); retry on
    falsey return or exception, sleeping a random interval between attempts."""
    for attempt in range(1, max_attempts + 1):
        try:
            result = func()
        except BrowserGoneError:
            raise
        except Exception as error:  # pylint: disable=broad-except
            # 視窗／session 已消失時重試是純浪費（每一步 setup 都會燒掉自己的
            # 3 次），而且會把真正的死因埋在一串 "gave up" 底下。這裡把原始例外
            # 也一併升級——`port.click()` 之類的變體 helper 會用寬 except 吞掉
            # 再由 JS fallback 重丟，raw WebDriverException 是這樣漏出來的。
            if is_browser_gone_error(error):
                raise BrowserGoneError(
                    f"browser session gone during {label} — "
                    f"{_long_error(error)}") from error
            # `_short_error`：這一行是**每個** DOM 步驟、每次重試都會印一次的
            # 重複行，所以走 180 字那一版。用 `!r` 的話 selenium 例外（`args`
            # 是空的）只會印出一對空括號——而同一個 handler 往上兩行的
            # `BrowserGoneError` 早就在用 `_long_error` 了：同一個例外物件，
            # 兩種格式，一個有訊息一個沒有。
            print(f"  [{label}] attempt {attempt}/{max_attempts} raised: "
                  f"{_short_error(error)}")
            result = False
        if result:
            if attempt > 1:
                print(f"  [{label}] succeeded on attempt {attempt}")
            return True
        if attempt < max_attempts:
            delay = random.uniform(*sleep_range)
            print(f"  [{label}] attempt {attempt}/{max_attempts} failed; "
                  f"retrying in {delay:.1f}s")
            time.sleep(delay)
    print(f"  [{label}] gave up after {max_attempts} attempts")
    return False


# ---------- output folder allocation ----------------------------------------

def _folder_belongs_to_batch(folder: Path, batch_start: float) -> bool:
    """A folder is part of the current batch when at least one file inside it
    was written after `batch_start`. Empty folders count as belonging to the
    current batch so we re-use them without numbering."""
    if not folder.exists():
        return False
    try:
        mtimes = [
            p.stat().st_mtime for p in folder.iterdir() if p.is_file()
        ]
    except OSError:
        return False
    if not mtimes:
        return True
    return max(mtimes) >= batch_start


def allocate_output_dir(base_name: str, batch_start: float) -> Path:
    """Return the output folder for this batch. If `<base_name>/` already has
    files from an earlier batch, walk through `<base_name>_2`, `_3`, … until
    a free or current-batch folder is found."""
    base = OUTPUT_ROOT / base_name
    if not base.exists() or _folder_belongs_to_batch(base, batch_start):
        return base
    for i in range(2, 1000):
        candidate = OUTPUT_ROOT / f"{base_name}_{i}"
        if not candidate.exists() or _folder_belongs_to_batch(candidate, batch_start):
            return candidate
    return base  # 1000 conflicts — give up and overwrite


# ---------- DOM leaf helpers (P6 C3 — port-based; signature `fn(port, ...)`) ----
# Lifted verbatim (logic-identical) from the two webrunner variants. All DOM
# access goes through the injected `port`; no selenium / je_web_runner import
# here (transport-error tuple + ancestor walk are reached via `port`).


def dump_textareas_diag(port) -> list[dict]:
    """Snapshot 頁面上所有 textarea / `contenteditable=true` 的關鍵屬性。
    回 list of dicts；任何例外 swallow 回空 list。"""
    try:
        result = port.execute_script(_DOM_DIAG_JS)
        return result if isinstance(result, list) else []
    except Exception as error:  # pylint: disable=broad-except
        # `_short_error`：呼叫端是 `_fill_via_native_setter` 的比對不符分支與
        # `find_undesired_textarea` 的找不到分支，兩個都是**每次填寫**都可能走到
        # 的失敗路徑，所以算重複行。
        print(f"dump_textareas_diag failed: {_short_error(error)}",
              file=sys.stderr)
        return []


def check_dom_request(port) -> None:
    """Iteration 邊界呼叫；看到 DOM_REQUEST_FILE 就 dump 一份 textarea diag
    然後 `emit_event('dom_result', ...)` 給 bot watcher 拉回 channel；
    處理完不論成功失敗都刪掉請求檔，避免下次重複觸發。"""
    if not DOM_REQUEST_FILE.exists():
        return
    try:
        # 內容暫不解析；目前只有「dump 全部」一種模式。未來可擴 cmd 欄。
        DOM_REQUEST_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # `UnicodeDecodeError` 是 `ValueError` 的子類別、不是 `OSError`，
        # 單寫 `OSError` 接不住它。這裡的內容根本沒被用到，讓一個編碼壞掉的
        # 請求檔把整輪批次炸掉毫無道理——未來真的解析 cmd 欄時，這個 except
        # 要跟著改成「回報這個請求壞掉」而不是繼續當它是有效請求。
        pass
    try:
        diag = dump_textareas_diag(port)
        emit_event("dom_result", count=len(diag), data=diag)
        print(f"check_dom_request: emitted dom_result with {len(diag)} entries")
    except Exception as error:  # pylint: disable=broad-except
        emit_event("dom_result", error=str(error))
        # `_long_error`：一個 `dom_request.json` 最多走到一次（底下那段無論成敗
        # 都把請求檔刪掉），所以是一次性診斷，不是重複行。
        print(f"check_dom_request failed: {_long_error(error)}",
              file=sys.stderr)
    try:
        DOM_REQUEST_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def snap(port, tag: str) -> None:
    if not _DEBUG_SCREENSHOTS:
        return
    name = f"debug_{tag}.png"
    if not _is_single_path_component(name):
        # `tag` 只准貢獻**檔名片段**，不准改目錄。今天 26 個呼叫端的 tag 全是字面
        # 值／整數／已消毒的角色名，所以這裡擋不掉任何一張合法的診斷截圖；它擋的是
        # 「未來某個呼叫端把外部字串接進 tag」——`request_id` 就當過那個角色，實測
        # `../../../evil` 會寫到 repo 外面（父目錄存在，所以是**寫得成功**的，不是
        # 報錯）。為什麼用 `_is_single_path_component` 而不是那支更嚴的
        # `_is_safe_folder_component`，理由寫在前者的 docstring 裡。
        print(f"[{tag}] screenshot skipped: tag is not a single filename")
        return
    shot = PROJECT_ROOT / name
    try:
        port.save_screenshot(str(shot))
        print(f"[{tag}] screenshot -> {shot}")
    except Exception as error:  # pylint: disable=broad-except
        # `_short_error`：`snap()` 掛在二十幾個錯誤分支上，含 `generate_loop`
        # 的每張圖失敗路徑——瀏覽器一死，這一行會跟著每一次失敗一起印。
        print(f"[{tag}] screenshot failed: {_short_error(error)}")


class BrowserGoneError(RuntimeError):
    """瀏覽器上下文已經沒了——不是「慢」，是「不存在」。

    當 hot-path helper 的 chromedriver 呼叫失敗、而且失敗是**永久性**的
    （視窗被關掉、target 被摧毀、session 被刪除、chromedriver 連不上）時，
    改丟這個例外而不是回平常的 None / False。理由：之後每一次呼叫都會用同樣
    方式失敗，呼叫端的 poll／retry 預算全部是白燒的。

    刻意繼承 `RuntimeError`：它會落進 `run_batch` 的外層 handler，路徑與
    `_abort_if_chrome_crashed` 的 raise 完全相同——emit `critical_error`、
    非零離開、監督者重生 Chrome（resume checkpoint 會把該角色接回去）。
    **不要**把它加進任何變體的 `TRANSPORT_ERRORS` tuple；整個設計的重點就是
    它不該被 `except port.TRANSPORT_ERRORS` 那些分支吸收掉。
    """


# 例外壓成一行時的兩個長度。分成兩個是因為兩種用途的成本完全不同：
#   `_short_error`（180）用在**會重複出現**的 log 行——poll 迴圈每轉一圈印一次，
#     長訊息在這裡就是洗版。
#   `_long_error`（400）用在**一次性**的地方：終結性的 raise（那句話會變成
#     `critical_error` 事件的 message）、以及點不下去的診斷。這兩種的關鍵資訊都在
#     訊息**尾巴**——`MaxRetryError` 的 `(Caused by NewConnectionError(... [WinError
#     10061] ...))` 與 `element click intercepted` 的 `Other element would receive
#     the click: <div class=…>`——而 180 字剛好會把兩者都切掉。
_SHORT_ERROR_LIMIT = 180
_LONG_ERROR_LIMIT = 400


# ---------------------------------------------------------------------------
# 為什麼還有 34 個站點刻意保留 `{error!r}`
#
# 2026-09-12 全面盤點過這三個檔（兩個變體 ＋ 本模組）裡「直接把例外插進字串」
# 的地方：**46 處**，其中只有 12 處換成上面這三支格式器。剩下的 34 處不是漏改，
# 判準只有一句話——
#
#     **這個 `try` 區塊有沒有可能丟出 selenium 例外？**
#
# 會的話必須改：`WebDriverException.__init__` 呼叫 `super().__init__()` 時
# **不帶參數**，所以 `args` 是空的，`repr()` 只印得出一對空括號，訊息 100% 消失
# （實測一個真的從 driver 丟回來的 `SessionNotCreatedException`：`str()` 752 字、
# `repr()` 的訊息 0 字）。
#
# 不會的話，`!r` 反而是**比較好**的那一個，理由有兩層：
#
# 1. **不會洩漏主機路徑。** 這一族大多是檔案 I/O：`repr(OSError)` 是
#    `FileNotFoundError(2, 'No such file or directory')`——**看不到路徑**；
#    `str(OSError)` 是 `[Errno 2] ...: 'D:\Work\...'`，會把完整主機路徑寫進
#    `webrunner.log`，而 `/log tail` 會把那個檔送進聊天平台（Secrecy Layer 1）。
# 2. **這些例外的 `args` 本來就非空**（`OSError` / `psutil` / `ctypes` /
#    `ImportError` / `json.JSONDecodeError`），`repr()` 讀得到訊息，改了只是雜訊。
#
# ⚠️ **判準是「try 區塊碰不碰得到 driver」，不是「except 寫的是什麼型別」。**
# 有 20 個保留站點的 handler 寫的是 `except Exception`，但 try 裡只有
# `shutil.copy2` / `os.walk` / `psutil` / `ctypes` / `emit_event`——`except` 寬不
# 代表收得到 selenium 例外。反過來說，把一個 `except OSError` 改寬成
# `except Exception`，或是在既有的 try 裡加一句碰 `port` 的程式碼，就會讓那個
# 站點越線，所以守門把「handler 的例外型別」也算進比對鍵裡。
#
# 守門：`test_selenium_facade.py` 的 `_EXCEPTION_REPR_EXEMPT`（雙向對帳，每一筆
# 都要寫理由）。新增站點預設是**紅的**，要嘛改用格式器、要嘛連理由一起登記。
# ---------------------------------------------------------------------------


def _one_line_error(error, limit: int) -> str:
    """把例外壓成一行 `型別: 訊息`，截到 `limit` 字。

    chromedriver 會在每個 WebDriverException 後面附上約 15 行的
    `Stacktrace:`（C++ 符號位址）。對一個已死的 session 每 poll 一次就印一次
    那串，就是「一次故障變成好幾頁雜訊」的來源，而且完全沒有診斷價值。完整
    細節仍會經由 `raise ... from error` 的例外鏈，被 `run_batch` 格式化進
    `critical_error` 事件的 traceback 欄位裡。
    """
    text = str(error).split("Stacktrace:")[0]
    line = " ".join(text.split())
    return f"{type(error).__name__}: {line[:limit]}"


def _short_error(error) -> str:
    """重複出現的 log 行用這個（見 `_SHORT_ERROR_LIMIT` 上面的說明）。"""
    return _one_line_error(error, _SHORT_ERROR_LIMIT)


def _long_error(error) -> str:
    """一次性的地方用這個：終結性的 raise、以及點不下去的診斷。

    **為什麼不共用 180 的那一版。** 兩種用途的關鍵資訊都在訊息尾巴：

    * `MaxRetryError` 的 `(Caused by NewConnectionError(…: [WinError 10061]
      無法連線，因為目標電腦拒絕連線。))`——「連線被拒絕」是唯一能直接回答
      「chromedriver.exe 是不是已經結束了」的證據，而且這一句會原封不動變成
      `critical_error` 事件的 message（`BrowserGoneError` 的訊息就是它）。
    * `element click intercepted` 的 `Other element would receive the click:
      <div class=…>`——唯一能直接回答「是不是有東西蓋在關閉鈕上面」的證據，也正是
      把點選從 JS 搬到 driver 換來的診斷。

    這條路一個額度週期最多跑兩次、終結性的 raise 一輪最多一次，長一點不會淹沒 log。
    """
    return _one_line_error(error, _LONG_ERROR_LIMIT)


def full_error_detail(error) -> str:
    """`型別: 完整訊息`——**不截斷、不砍 `Stacktrace:`** 的鑑識用格式。

    ⚠️ **絕對不要換回 `{error!r}`。** selenium 的 `WebDriverException` 把訊息存在
    `self.msg`，**`args` 是空的**，所以 `repr()` 只剩一對空括號。實測
    （selenium 4.48.0，2026-09-12）：

        e = SessionNotCreatedException(
            'session not created: This version of ChromeDriver only supports '
            'Chrome version 145\\nCurrent browser version is 152.0.7977.84 ...')
        e.args   -> ()
        repr(e)  -> SessionNotCreatedException()      # 訊息 100% 消失
        str(e)   -> 351 字元，含兩邊版本號與 Chrome 的實際路徑

    **兩個數字，不要混用。** 上面那 351 是**自己建構**的例外（測試語料就是它）。
    真的從 driver 丟回來的那一個是 **752 字元**——selenium 在訊息後面再接上
    `Stacktrace:` 與一段無符號的回溯位址（實測 2026-09-12，chromedriver 145
    對 Chrome 152）。兩個都對，量的是不同的物件；引用時要說清楚是哪一個。

    這不是假設性的：2026-08-25 11:38:50 的 spawn 失敗，記錄裡就只有
    `Chrome spawn attempt 1/3 failed: SessionNotCreatedException()`「別的什麼都
    沒有」，於是有人跑去翻一個當時根本還沒被建立的 `chromedriver.log`。
    **訊息從來就不是空的，是我們自己的格式把它丟掉的。**

    ⚠️ **這個缺陷用一般的 `Exception('boom')` 測不出來**：那種例外 `args` 非空，
    `repr()` 會帶上訊息，舊格式與新格式都會通過。要釘住它，語料必須是真的
    selenium 例外（`args` 空、`msg` 有值）。

    **型別名要留著。** `args` 非空的例外，`str()` 只有訊息、認不出類別，而
    `SessionNotCreatedException` 與 `TimeoutException` 的區別本身就是第一層診斷。

    **為什麼不接到 `_one_line_error` 上（那個 400 字上限會咬人）。**
    `_short_error` / `_long_error` 會截斷、也會砍掉 chromedriver 附的 C++
    `Stacktrace:`，因為它們服務的是**會重複出現**的 log 行與**會變成
    `critical_error` 事件 message** 的終結性 raise。這一支兩者都不是：它只在
    spawn 失敗時出現，一次開機最多幾行、只寫 stderr，而有用的字（兩邊版本號、
    Chrome 的實際路徑）排在 stack dump **前面**，所以那段 dump 只是可以忽略的
    尾巴、長度有界。反過來，那 400 字上限現在是**剛好夠**而不是綽綽有餘：
    `_one_line_error` 先砍掉 `Stacktrace:` 之後的整段、再把換行壓成空白，所以真實
    那 752 字進去、出來剩 **350** 字，離 400 只有 **50** 字餘裕。多一句話就會被
    切掉，而被切掉的症狀正好是這支函式要修的那一個：安靜地少掉診斷。
    **不要好心「統一」它們**——這句勸阻本身守不住任何東西（實測：把這支改成
    `return _long_error(error)`，2026-09-12 當時整份 `test_selenium_facade.py`
    41 支全綠），所以另外配了
    `test_the_forensic_format_keeps_what_the_one_line_helpers_throw_away`
    真的去咬它。

    （與 MEMORY 那條「`repr(OSError)` 藏路徑、`str` 會露出來」不衝突：那條講的是
    **送進聊天平台的字串**，這裡是寫進 stderr／log 的診斷，`CLAUDE.md` Layer 1
    明寫「完整細節寫 stderr／log」。送出端仍由 bot 的 `_owner_detail` 把關。）
    """
    return f"{type(error).__name__}: {error}"


def is_browser_gone_error(error) -> bool:
    """`error` 是否代表「視窗／session 已永久消失」。"""
    names = {cls.__name__ for cls in type(error).__mro__}
    if names & _SESSION_GONE_EXC_NAMES:
        return True
    text = str(error).lower()
    return any(marker in text for marker in _SESSION_GONE_MESSAGE_MARKERS)


def _note_transport_error(where: str, error) -> None:
    """Hot-path `except port.TRANSPORT_ERRORS` 的統一處理：記錄，必要時升級。

    暫時性卡頓 → 印一行就回去，呼叫端維持原本「回 sentinel 再 poll」的行為。
    Session 已死 → 丟 `BrowserGoneError`，讓這一輪立刻收掉，而不是對著一個
    死掉的瀏覽器把整個重試預算 poll 完。
    """
    if is_browser_gone_error(error):
        # 終結性的 raise：這句話會原封不動變成 `critical_error` 事件的
        # message，所以用留得比較長的那一版（`_long_error` 的 docstring 記著
        # 為什麼——關鍵的 `(Caused by …[WinError 10061]…)` 在訊息尾巴）。
        raise BrowserGoneError(
            f"browser session gone during {where} — {_long_error(error)}"
        ) from error
    print(f"  [warn] {where} transport error: {_short_error(error)}")


def _browser_gone_reason(port) -> str | None:
    """一次最便宜的 JS round-trip；瀏覽器沒了就回一句簡短原因，否則回 None。

    「暫時性失敗」也回 None——會走到這裡的呼叫點本來就有自己的失敗計數器，
    那條路不該被這個探針搶走。
    """
    try:
        alive = port.execute_script(_ALIVE_PROBE_JS)
    except Exception as error:  # pylint: disable=broad-except
        if is_browser_gone_error(error):
            return _short_error(error)
        return None
    if alive == _ALIVE_PROBE_TOKEN:
        return None
    # je 變體：wrapper 吞掉例外、一律回 None。這條路徑分不出「視窗被關掉」與
    # 「暫時卡住」，但所有呼叫點都在「已經連續失敗過一輪」之後——那個時點重生
    # 瀏覽器就是正確反應。
    return f"alive probe returned {alive!r}"


def _probe_browser_alive(port) -> str | None:
    """額度等待迴圈用的極輕量存活探測。活著回 None，死了回一句簡短原因。

    **絕不 raise，也絕不做任何補救。** 兩件事都是刻意的：

    - 它跑在額度等待迴圈裡，任何往上丟的例外都會取代掉原本乾淨的「等完再重試」
      路徑；所以連 `_browser_gone_reason` 自己壞掉都要吞下來（吞成一句原因，不是
      吞成沉默——探針壞掉要看得見）。
    - 偵測到死亡也**只記錄、不當場重啟**。中途重啟要重新登入並重填欄位，而那正是
      本專案最貴一次故障的路徑（欄位只被部分填回去、安靜地用錯提示詞燒掉 10 張圖
      /兩小時）。在等待中觸發它風險大於收益，交給既有復原流程在原本的時機跑。

    它唯一的產出是**時間**：把死亡時刻從「最久晚一小時」縮到 30 秒內。
    等待迴圈整整一輪（預設 3600 秒）不對 driver 下任何指令，所以瀏覽器在等待期間
    死掉的話，要到等完之後的第一個指令才會發現。實測（`WEBRunner.log` 2026-09-07
    03:02 與 06:18 兩次）長這樣：

        ConnectionRefusedError: [WinError 10061] 無法連線，因為目標電腦拒絕連線。
        supervisor: webrunner exited rc=1 after 283379s (attempt 1); restarting

    **WinError 10061 是「連線被拒絕」＝ 那個埠上沒有東西在聽 ＝ chromedriver.exe
    自己已經結束了**，不是連線逾時、也不是 session 卡住。兩次的時間戳都剛好落在
    60 分鐘等待結束後的第一個指令上，所以真正的死亡時刻在那一小時裡不可知——跟
    任何活動對照都只能用猜的。（順帶排除一個看似合理的解釋：Selenium 4 那個
    「閒置 300 秒砍掉 session」是 Grid／selenium-server 的 `--session-timeout`，
    本專案是本機直接起 `ChromeService`、沒有 Grid，不適用。）

    **2026-09-10：死因大致查出來了，而且不在瀏覽器這一側——是我們自己的測試
    工具。** 四次死亡（03:02／06:17／11:44／17:10）對得上四次事故：
    `_browser_killguard.py` 的 docstring 記了**三**次（它寫於 17:06:41，所以記不到
    第四次）。那一次是 17:07，
    驗證防線本身的 `mutate_killguard.py` 探針在「兩層 taskkill 檢查一起拿掉」那個
    變異底下真的執行了 `taskkill /IM chrome.exe`。**查這一族要三個檔案一起 grep**：
    漏掉第四次的原因，跟這條結論本身講的是同一件事——答案分散在幾個檔案裡而彼此
    沒有互相指涉。

    死亡 #1 的證據是硬的：supervisor 記的 `after 283379s` ＝ 78.72 小時，而事故 #1
    寫的是「毀掉一個已經跑了 78.7 小時的批次」——同一個批次、同一分鐘。

    **最後一次死亡是 17:07（防線自己的變異探針造成），此後零死亡。** 不要寫成
    「防線落地之後零死亡」：以 17:06 為起算點實測是 52 次 `quota_blocked` /
    50 次 `quota_resumed` / **1** 次 `critical_error`（就是 17:10:44 那一筆）；
    52/50/**0** 那組數字是從 **17:11** 起算的，接到 17:06 上就不成立。

    兩種死法的簽名不同，統計時不要合併：03:02／06:18／11:44 是
    `ConnectionRefusedError`（chromedriver.exe 自己不見了）；17:07／17:10 是
    `InvalidSessionIdException`（chromedriver.exe 還活著、chrome.exe 全部消失
    ＝ `taskkill /IM chrome.exe` 的簽名）。

    ⚠️ **連帶更正一個框架錯誤：「都死在等額度的時候」是偵測假象，不是機制。**
    四次死亡的時間戳全部落在 `quota_blocked` 之後 60.4～60.6 分鐘，也就是等待
    結束後的第一個指令——那一小時是**唯一**不碰 driver 的窗口，所以發生在裡面
    的任何一次死亡都只會在那個時刻被看見。對照組就在同一份記錄裡：另有一次死亡
    出現在 `character_start` 之後 4.6 分鐘。所以不要再從「等待期間有什麼會殺掉
    瀏覽器」出發找原因，那個相關性量到的是**我們什麼時候去看**。

    **而且這條有直接證據，不只有對照組。** 第四次死亡是唯一一次本函式已經上線
    的，`webrunner.log` 留著它抓到的那一行（全檔就這一行）：
    `[09-07 17:07:44] [quota] …瀏覽器在等額度的期間死掉了（這一輪已等 3420s…）`
    ——3420 秒 ＝ 57 分，比同一件事的 `critical_error`（17:10:44）**早三分鐘**。
    同一次死亡的兩個時間戳擺在一起，就把「60.4 分」證明成回報延遲而不是機制。
    """
    try:
        return _browser_gone_reason(port)
    except Exception as error:  # pylint: disable=broad-except
        return f"alive probe itself failed: {_short_error(error)}"


def _abort_if_browser_gone(port, where: str) -> None:
    """視窗／session 已消失就 raise `BrowserGoneError`（→ 監督者重生）。

    與 `_abort_if_chrome_crashed` 是**互補**的兩種死法：那個抓的是「session
    還活著、頁面變成 crash interstitial」（renderer 崩了），這個抓的是
    「session 本身沒了」——後者連 `port.current_url()` 都會丟例外，所以
    crash-page 偵測看到的只會是「讀不到、當作沒崩」，永遠不會替它收工。
    """
    reason = _browser_gone_reason(port)
    if reason is not None:
        raise BrowserGoneError(
            f"browser session gone during {where} — {reason}; aborting so "
            f"supervisor respawns Chrome."
        )


class GenerationBlockedError(RuntimeError):
    """站方擋住生成，而且**重試不會有結果**（額度用完、方案到期…）。

    與 `BrowserGoneError` 的差別在收工方式，不在嚴重程度：瀏覽器沒了要**重生**
    （新的 Chrome 就好了），這個要**停下來等人處理**（重生只會看到同一個對話框，
    每輪還重跑一次登入 ＋ setup，就是使用者看到的「沒有自動停止」）。

    `run_batch` 專門接住它，emit `generation_blocked` 事件並回
    `RC_GENERATION_BLOCKED`；兩個監督者都把這個 rc 當成「不要重生」。佇列與續跑
    檢查點都不動——使用者處理完打 `/run` 就從原地接回去。
    """


def get_generation_block(port) -> dict | None:
    """Tier 1：畫面上有沒有「要付錢／要處理帳號」的對話框或吐司。

    回 `{"text": ..., "pattern": ...}` 或 None。純讀取，永不 raise——
    transport 例外交給 `_note_transport_error` 分流（session 沒了就升級成
    `BrowserGoneError`，那是另一條收工路徑）。
    """
    try:
        return port.execute_script(_GENERATION_BLOCK_JS)
    except port.TRANSPORT_ERRORS as error:
        _note_transport_error("generation-block check", error)
        return None


def has_blocking_dialog(port) -> str | None:
    """Tier 2：畫面上有沒有可見的 modal 對話框擋著（**不看字面**）。

    回對話框文字（截成 400 字，超過會在尾巴附全文長度）或 None。

    **消費者有三個，不是 docstring 以前寫的一個**，而其中一個把 None 當成一個
    「是」的答案，所以這裡的漏判不是漏偵測、是靜默的錯誤結果：

    1. `dismiss_blocking_dialog` 的進度判準——**`None` ＝「關乾淨了」**
       （`stop_reason = "closed"` → `return True`）。
    2. `generate_loop` 在 `consecutive_fail_abort` 門檻上的 Tier 2 判定。
    3. `generate_loop` 的收尾防線（`saved == 0` 而畫面上還有 modal）。

    所以這一題要**寧可回答「有」**：誤判只會讓 1 落到整頁 reload（安全）、
    讓 2/3 在一個本來就已經死掉的角色上多停一次；漏判則會讓 1 對著一個它根本沒
    碰到的對話框回報成功。長度上限就是這樣被拿掉的，理由寫在
    `_BLOCKING_DIALOG_JS` 上面。
    """
    try:
        return port.execute_script(_BLOCKING_DIALOG_JS)
    except port.TRANSPORT_ERRORS as error:
        _note_transport_error("blocking-dialog check", error)
        return None


def describe_dialog_controls(port) -> str:
    """把擋路對話框上的控制項清單整理成給 log 看的文字。純唯讀，不點任何東西。

    只在關閉失敗那條路上呼叫（每次額度用完最多一次），所以成本無關緊要；happy
    path 完全不會執行到。回空字串代表拿不到（對話框剛好消失、或 JS 打不通）。
    """
    try:
        info = port.execute_script(_DIALOG_CONTROLS_DIAG_JS)
    except port.TRANSPORT_ERRORS as error:
        _note_transport_error("dialog control inventory", error)
        return ""
    if not isinstance(info, dict):
        return ""
    rows = info.get("controls") or []
    head = (f"對話框 {info.get('w')}x{info.get('h')}，可點候選 "
            f"{info.get('total')} 個（依離右上角的距離排序，列出前 {len(rows)} 個）：")
    lines = [head]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"    #{i:<2} <{r.get('tag')}> role={r.get('role')!r} "
            f"aria={r.get('aria')!r} title={r.get('title')!r} "
            f"text={r.get('text')!r} icon={r.get('icon')} "
            f"@({r.get('dx')},{r.get('dy')}) {r.get('w')}x{r.get('h')} "
            f"cursor={r.get('cursor')} class={r.get('cls')!r}")
    return "\n".join(lines)


def _click_dismiss_target(port, element) -> str:
    """把 `_DISMISS_DIALOG_JS` 挑好的那一顆按下去。回 `'real'` / `'synthetic'` /
    `''`（兩種都按不動）。

    **為什麼是 driver 層的真點選優先。** 站方會疊兩層對話框，兩層右上角關閉鈕的
    class 完全一樣，但 JS 的 `el.click()` 只關得掉第一層（實測：第 1 層按下去畫面
    真的換了、第 2 層按下去一個字都沒變，四個額度週期一致）。兩個機制上的差別都
    只有真點選補得起來：

    * `el.click()` 是**直接對那顆元素派送**，只會往上冒泡；真點選是打在座標上，
      `e.target` 是「那個座標上最上層的元素」。有東西蓋在上面時，只有真點選碰得到
      那個 handler。
    * `el.click()` 只派送 `click` 一種事件；真點選會走完整串
      pointerdown / mousedown / mouseup / click。把關閉行為掛在 `onPointerDown` /
      `onMouseDown` 是 modal 元件常見的寫法，那一類只有真點選收得到。

    **走 `click_native` 而不是 `port.click`**：後者自己就會在失敗時**安靜地**退回
    `execute_script` 的合成點選，於是「真點選成功了」與「真點選失敗、偷偷改用合成」
    在外面長得一模一樣——那正是本專案記過四次的「log 報的是嘗試不是結果」。這裡要
    的恰恰是分辨這兩者：真點選被 `ElementClickIntercepted` 擋下來，就是「有東西蓋在
    上面」的直接證據，而它會連帶指名那個元素。所以退路由這裡自己走，一階一階都出聲。

    `getattr` ＋ `callable` 是給精簡 port 用的（測試的假 port、其他驗證腳本）：
    沒有這個方法就直接走合成點選，行為退回舊版，不會炸。
    """
    if element is None:
        # JS 挑中了卻沒交出元素——只可能是 port 沒有把 WebElement 解包回來。
        print("  [quota] 關閉動作挑中了控制項，但拿不到元素；改送 Escape",
              file=sys.stderr)
        return ""
    native = getattr(port, "click_native", None)
    if callable(native):
        try:
            native(element)
            return "real"
        except Exception as error:  # pylint: disable=broad-except
            if is_browser_gone_error(error):
                raise BrowserGoneError(
                    "browser gone while dismissing a dialog: "
                    + _long_error(error)) from error
            # 這一行就是這個改動買到的診斷：`element click intercepted` 會連帶
            # 指名蓋在上面的那個元素。
            print(f"  [quota] driver 真點選按不下去（{_long_error(error)}）；"
                  "改用合成點選", file=sys.stderr)
    try:
        port.execute_script("arguments[0].click();", element)
        return "synthetic"
    except Exception as error:  # pylint: disable=broad-except
        if is_browser_gone_error(error):
            raise BrowserGoneError(
                "browser gone while dismissing a dialog: "
                + _long_error(error)) from error
        print(f"  [quota] 合成點選也按不下去（{_long_error(error)}）",
              file=sys.stderr)
    return ""


def dismiss_blocking_dialog(port, max_rounds: int = 4) -> bool:
    """關掉擋路的 modal 對話框，**關到沒有為止**。全部關乾淨（或本來就沒有）回 True。

    每一輪：JS **挑**一顆可以安全按的（文字 → aria-label → 右上角無字圖示鈕）並把
    元素交回來 → driver 真點選 → 按不動退合成點選 → 都不行（或根本沒挑中）才送
    Escape → **以結果為準**再問一次還有沒有 modal（「按了某個東西」不等於
    「關掉了」）。全部關不掉才由呼叫端退回整頁 reload。

    絕不點任何帶付款語意的控制項。**判定點在「JS 不會回傳它」**，不在「不會 click
    它」——按的動作已經搬到 Python，反例測試釘在 click 事件上等於什麼都沒驗。理由
    與階梯細節見 `_DISMISS_DIALOG_JS` 的說明與 `_click_dismiss_target`。

    **為什麼是迴圈而不是單次（2026-09-07，這是本函式最重要的一段）。**
    規則 3 上線後連續五個額度週期都印「關不掉」，看起來像「找對了卻按不動」。
    推翻那個結論的不是任何一則訊息，而是 `describe_dialog_controls` 印出來的
    **對話框尺寸**——它在規則 3 上線的那一刻換了：

        11:44 之前 856x917 / 11 個候選（Subscribe、Pay As You Go、Get Started）× 65
        11:44 之後 420x322 /  5 個候選（Unsubscribe、Update Payment Details）×  5

    而那份清單是**關閉動作跑完之後**才抓的。也就是說第一層付費牆真的被關掉了，
    露出後面**第二層**（帳號管理）對話框，而單次關閉只看「還有沒有 modal」就回報
    失敗、退化成整頁 reload。所以缺的是「再關一次」，不是換一種按法。

    **第二層對話框不吃合成點選，這是「關到沒有為止」之外還要 driver 點選的理由。**
    迴圈上線後的四個額度週期都長一樣：第 1 層 @(805,21) 按下去畫面真的換了、第 2 層
    @(369,21) 按下去**一個字都沒變**，早停判準當場收手，仍然落到整頁 reload。兩層那顆
    關閉鈕的 class 一模一樣，所以差別合理的解釋在**元素之上**（有東西蓋著）或**事件
    種類**（handler 掛在 pointerdown/mousedown），兩者都只有真點選補得起來。

    順帶把「第二層是不是帳號出問題」查掉了：`has_blocking_dialog` 的回傳原文是
    「You are subscribed to the Opus tier! Your subscription renews around
    2026/09/18」，而那之後每小時照常回補、照常產圖（8.0 張/小時，與歷史基準 7.85
    一致）。所以它只是藏在付費牆後面的帳號管理面板，正確處置是關掉它、不是叫人。

    **進度判準用對話框文字，不是「按過了沒」。** 每一輪比對 `has_blocking_dialog`
    的回傳：文字變了 ＝ 這一按有效、還有下一層可以繼續關；文字一模一樣 ＝ 這一按
    什麼都沒改變，再按十次也一樣，直接收手讓呼叫端 reload。這是同一條「以結果為準」
    的規則往前推一步——沒有它，一顆按不動的按鈕會白白吃掉 `max_rounds` 輪。

    **Escape 一輪只送一次、而且只送一次。** 對站方這兩層 modal 它實測 **218/218
    無效**（13 天、真按鍵 ＋ 合成事件、三個派送目標都試過），但它是通用退路而不是
    這個站台專用的：`verify_quota_dialog.py` 情境 12（keydown 只掛在 modal 元素上的
    focus-trap 對話框）證明它在別的形狀上真的關得掉。拿掉它會讓那個情境退回整頁
    reload；重複送則是純粹的副作用（Escape 在別的畫面上有別的意義）。

    **成功率不要用 log 訊息去統計。** `WEBRunner.log` 裡「dismissed」153 筆對
    「could not dismiss」65 筆，看起來成功率 70%——那 153 筆全是假的，來自這個函式
    以前「在驗證之前就無條件印 dismissed」的缺陷（下面那段註解記著它）。看得出真相
    的是一個**結構上的**矛盾：reload 只在 `not dismissed` 時才會發生，而每一天的
    reload 次數都剛好等於當天的嘗試次數，包括那些「成功」的日子。

    關不掉時會去抓對話框上的控制項清單（`describe_dialog_controls`）。這不是除錯
    殘留：關閉失敗會退化成整頁重新整理，而「站方沒放關閉鈕」與「放了但選擇器認不
    出來」的處置完全相反，沒有這份清單就只能靠猜——在購買對話框上靠猜著放寬點選
    規則是這裡最不該做的事。而這一次，正是那份清單（而不是任何一則 log 訊息）指出
    真正的成因是第二層對話框。清單**每次都抓**，但只在「這個形狀第一次出現」時才
    印出來（連同逐層的經過），理由與抓法見 `_report_dismiss_outcome`。

    **這個函式自己不印任何東西**，只累積事實；敘述統一由
    `_report_dismiss_outcome` 產出。加東西進來的時候請維持這條分工——它是「穩態下
    每次被擋只留一行」那條性質的實作方式。
    """
    escaped = False
    seen_before = None          # 上一輪關完之後畫面上還剩的對話框文字
    rounds: list[tuple[str, str]] = []   # (這一按是什麼, 按完的結果)
    stop_reason = "rounds"      # 迴圈跑完都沒關掉 → 用完 round
    for _round_no in range(1, max_rounds + 1):
        try:
            plan = port.execute_script(_DISMISS_DIALOG_JS)
        except port.TRANSPORT_ERRORS as error:
            _note_transport_error("dismiss dialog", error)
            return False
        if plan is None:
            # 第一輪＝本來就沒有對話框（沒有任何話要說，直接回去）；
            # 之後＝上一輪把最後一層關乾淨了，而 `_BLOCKING_DIALOG_JS` 與挑選用的
            # `_DISMISS_DIALOG_JS` 判準不同，所以會走到這裡。
            if not rounds:
                return True
            stop_reason = "closed"
            break
        # JS 只負責**挑**，按的動作在這裡。回 'escape'（字串）＝一顆都沒挑中。
        action, how = "escape", ""
        if isinstance(plan, dict):
            action = str(plan.get("action") or "clicked:?")
            how = _click_dismiss_target(port, plan.get("el"))
            # 用哪一種按法按下去的要寫進 log：真點選成功 vs 悄悄退回合成點選，
            # 是判斷「這一層到底吃不吃合成事件」唯一的觀測點。
            action = f"{action} [{how or 'unclickable -> escape'}]"
        if not how:
            if escaped:
                stop_reason = "escape-spent"
                break           # Escape 已經送過一次，再送只是副作用
            escaped = True
            # 先試 driver 層的真按鍵：合成的 KeyboardEvent 是
            # `isTrusted === false`，有些 focus-trap 函式庫會忽略它。port 沒有這個
            # 方法也不算錯（測試用的假 port、其他驗證腳本的精簡 port 都可能沒有），
            # 直接跳過往下走。
            real_escape = getattr(port, "press_escape", None)
            if callable(real_escape):
                try:
                    real_escape()
                except port.TRANSPORT_ERRORS as error:
                    _note_transport_error("dismiss dialog (real escape)", error)
            try:
                port.execute_script(_SEND_ESCAPE_JS)
            except port.TRANSPORT_ERRORS as error:
                _note_transport_error("dismiss dialog (escape)", error)
                return False
        human_pause(0.6, 1.2)
        # **判定要等驗證完才做，記錄要等整個迴圈結束才寫。** 兩件事分別修掉一個
        # 缺陷：判定曾經在驗證之前就宣告「dismissed」（回傳值一直是對的，錯的只有
        # 敘述——而看 log 的人是照敘述判斷的）；記錄則曾經每一輪印一行，於是每次被
        # 擋固定產出 `max_rounds` 筆一模一樣的 `could not dismiss …（第 N/4 層）`。
        # 現在只累積事實，敘述交給 `_report_dismiss_outcome`。
        remaining = has_blocking_dialog(port)
        if remaining is None:
            rounds.append((action, "closed"))
            stop_reason = "closed"
            break
        if seen_before is None:
            # 第一輪沒有基準可比，所以只能說「還有對話框擋著」——不能說「換了」。
            rounds.append((action, "still"))
        elif remaining == seen_before:
            # 按了東西但畫面一個字都沒變 → 這一顆沒有作用，再按也一樣。
            rounds.append((action, "same"))
            stop_reason = "same"
            break
        else:
            rounds.append((action, "changed"))
        seen_before = remaining
    _report_dismiss_outcome(port, rounds, stop_reason, max_rounds)
    return stop_reason == "closed"


_DISMISS_VERDICT_TEXT = {
    "closed": "關乾淨了",
    "still": "還有對話框擋著",
    "changed": "對話框換了（底下還有一層）",
    "same": "畫面一個字都沒變",
}
_DISMISS_STOP_TEXT = {
    "closed": "關乾淨了",
    "same": "這一按沒有改變畫面，不再重試（避免空轉整組 round）",
    "escape-spent": "Escape 已經送過一次，再送只是副作用；不再往下關",
    "rounds": "連關 {max_rounds} 層仍有對話框擋著；不再往下關",
}

# 「這個形狀我印過了」。key 是**摘要**不是原文：value 只是次數，整個 dict 不會隨
# 執行時間長大到有意義的程度。**per-process 是刻意的**——重生之後重印一次是對的，
# 那是新的行程、可能是新的程式碼、也可能是站方改版之後的第一次。
_DISMISS_LOG_SEEN: dict[str, int] = {}


def _report_dismiss_outcome(port, rounds: list[tuple[str, str]],
                            stop_reason: str, max_rounds: int) -> None:
    """關閉動作結束後**唯一**的記錄出口。第一次遇到某個形狀印全部，之後只印一行。

    **為什麼要收敘述。** 分層關閉（規則 3）上線後（09-07 17:17 起）實測 25 個被擋
    區塊 **0 次關得掉**，每一個區塊固定產出 2 行
    `could not dismiss …（第 N/4 層）`（合計 50 行）外加一整份控制項清單——完全可
    預測、資訊量為零，而且會隨 `max_rounds` 等比例長大。這正是 `discord_bot.log`
    那 13,516/14,085 行 `rpc apply -> ok`（96%）的開頭形狀，而 `trim_log` 只留尾段：
    **一個把有用訊息趕出記錄檔的記錄比不記錄還糟。**

    **收的是敘述，不是迴圈。** 迴圈是通用退路（`verify_quota_dialog.py` 情境 12
    證明它在別的對話框形狀上真的關得掉），嘗試次數一次都沒有減少。

    三條性質，缺一不可：

    1. **穩態下每次被擋只印一行**，而且行數不隨 `max_rounds` 成長。
    2. **「關掉了」與「關不掉」仍然分得出來**——兩條路都帶著 `dismissed` /
       `could not dismiss` 這兩個既有的可 grep 字串（歷史統計是照它們數的）。
    3. **第一次遇到某個形狀仍然有完整資訊**：逐層的按法與結果、收手的理由、控制項
       清單，一個都不少。

    **形狀的定義包含控制項清單，這一點是載重的。** 09-07 那次「其實關掉了第一層、
    露出第二層」的突破，靠的不是任何一則訊息，而是清單裡的**對話框尺寸**變了
    （856x917 → 420x322）。所以清單照樣每次抓（失敗路徑一個額度週期最多一次，成本
    無關緊要），只是拿它一起算形狀：站方改版 → 形狀變 → 自動恢復完整記錄。
    只有成功路徑不抓（`happy path` 不該多付一次 JS 往返）。
    """
    dismissed = stop_reason == "closed"
    head = "dismissed" if dismissed else "could not dismiss"
    # 成功就不抓清單（多一次 JS 往返沒有意義）；失敗一定抓，而且抓到的內容要算進
    # 形狀裡——見 docstring。
    inventory = "" if dismissed else describe_dialog_controls(port)
    shape = "|".join(f"{a}>{v}" for a, v in rounds) + f"#{stop_reason}#{inventory}"
    key = hashlib.sha256(shape.encode("utf-8")).hexdigest()[:16]
    seen = _DISMISS_LOG_SEEN.get(key, 0) + 1
    _DISMISS_LOG_SEEN[key] = seen
    if seen > 1:
        print(f"  [quota] {head} blocking dialog（{len(rounds)} 層；本行程第 "
              f"{seen} 次遇到同一個形狀 {key}，逐層經過與第 1 次相同，不再重印）")
        return
    print(f"  [quota] {head} blocking dialog（{len(rounds)} 層，形狀 {key}）："
          + _DISMISS_STOP_TEXT[stop_reason].format(max_rounds=max_rounds))
    for i, (action, verdict) in enumerate(rounds, 1):
        print(f"  [quota]   第 {i} 層 via {action!r} → "
              + _DISMISS_VERDICT_TEXT[verdict])
    if dismissed:
        return
    if inventory:
        print("  [quota] " + inventory)
    else:
        print("  [quota] 對話框控制項清單取不到——對話框在這一瞬間消失了，"
              "或 JS 打不通。")


def wait_for_quota_recovery(port, batch_cfg: dict, *, label: str,
                            waited_sec: float = 0.0,
                            on_reload=None, on_idle=None) -> float:
    """額度用完時：關掉對話框 → 等一段時間 → 回到呼叫端重試同一張圖。

    回傳「累計已等待的秒數」，呼叫端要把它傳回來，好讓 `quota_wait_max_sec`
    的上限跨多輪累積。呼叫端要拿「上一輪等了多久」的話，取**回傳值與傳入值的
    差**即可（`generate_loop` 就是這樣把它寫進 `quota_resumed.last_wait_sec`）
    ——那是定義上就正確的，不必在呼叫端自己再讀一次設定。

    **`quota_wait_poll_sec` 不是吞吐量旋鈕。** 見下面的實測，別再為了跑快一點
    去調它。

    設計重點（每一條都是刻意的）：
    - **不結束行程**。等待整段發生在 `generate_loop` 裡面，所以監督者看不到
      任何 rc、不會重生、不會重跑登入 ＋ setup。使用者要的「自動等到額度回復」
      只有在行程活著的時候才成立。
    - **不計入 `consecutive_fail_abort`**。被擋住不是失敗，是「還沒輪到」。
    - **輪詢間隔固定（`quota_wait_poll_sec`），而且不要改成退避。** 2026-09-07
      量過：`events.ndjson` 裡 214 個 `quota_blocked`（08-24 → 09-07）剛好橫跨
      兩個設定值——08-25 07:10 之前是約 14 分鐘，之後是約 60 分鐘。以「同一個角色
      相鄰兩次 `quota_blocked` 之間」為窗口、`image_index` 差為產出：
      短輪詢期（窗口中位 14.3 分，n=43）**7.87 張/小時**、長輪詢期（窗口中位
      67.8 分，n=151）**7.84 張/小時**——194 個窗口、兩週，差 0.4%。
      （重算時要套同一個過濾：只取長度 ≤ 2 小時的窗口。長輪詢原始有 153 個，其中
      一個長 36.5 小時只產 6 張，那是已知的 36 小時**停機**空窗、不是額度窗口；
      不濾掉會把總和法拉到 6.50、結論整個反過來。中位數不受影響。）
      全部 196 個窗口裡，**速率**的變異係數是 0.131 而**張數**是 0.427，窗口長度
      與張數的相關 r=0.118：不變的是速率、會變的是張數，這是「持續滴入」而不是
      「整點發一批」的形狀。也就是說**上限在帳號那一側，輪詢間隔完全影響不到它**。
      調短只會把同樣的圖切成更多更小的爆發，而每一次醒來都要付一次
      `dismiss_blocking_dialog`、實測 218/218 都關不掉而落到整頁 reload ＋
      `on_reload` 重填——那正是這個專案出過最嚴重的一次故障（欄位部分沒填回去、
      安靜地用錯提示詞燒掉 10 張圖 / 兩小時）的觸發路徑。從一天約 24 次醒來變成
      約 200 次，是**下檔真實、上檔為零**。
    - **等待期間照樣尊重暫停標記**（`wait_if_paused`），`/stop` 也照樣殺得掉。
    - 每一輪重新關一次對話框：站方常常在等待期間又跳一次。
    - `quota_wait_max_sec` 為 0 ＝ 無上限；設了正值且超過就丟
      `GenerationBlockedError`，退回「乾淨停止、不重生」那條路（那代表這不是
      會自己回補的額度，而是方案／帳號問題）。
    - **`on_reload` 只在真的 reload 過之後才呼叫。** reload 會把頁面狀態打回站方
      持久化的版本，剛填好、站方還沒存起來的提示詞會整段消失。呼叫端用它把欄位
      重填回去；沒 reload 就沒動到狀態，不必付這個成本。實測依據見下面。
    - **每個睡眠切片跑一次存活探測**（`_probe_browser_alive`）。這一整輪預設一小時
      不對 driver 下任何指令，所以瀏覽器在等待期間死掉的話，要到等完之後的第一個
      指令才會發現——最久晚一個小時，死亡時刻在那個區間裡完全不可知。探測把它釘在
      30 秒內。**只記錄、不當場重啟**，理由見那支的 docstring。
    - **`on_idle` 每個睡眠切片跑一次**，讓等待期間仍然服務得到插播的單圖請求。
      沒有它的話，這一整段（預設一小時）對使用者是完全沒有反應的：bot 那邊的
      `_SINGLE_IMAGE_PENDING_TTL_SEC` 只有 600 秒，十分鐘後就會把請求當成
      「webrunner 沒服務就退出了」掃掉。而額度用完在這個帳號上是常態——實測一個
      週期約 68 分鐘、其中 60 分鐘在等——等於使用者的即時產圖幾乎永遠會逾時。
    """
    poll = float(batch_cfg.get("quota_wait_poll_sec", 3600.0))
    cap = float(batch_cfg.get("quota_wait_max_sec", 0.0))
    if cap > 0 and waited_sec >= cap:
        raise GenerationBlockedError(
            f"generation still blocked after waiting {waited_sec:.0f}s "
            f"(quota_wait_max_sec={cap:.0f}); this does not look like a quota "
            f"that refills — stopping instead of respawning.")
    dismissed = dismiss_blocking_dialog(port)
    if not dismissed:
        # 關不掉：重新整理頁面。modal 是頁面狀態，reload 一定清得掉；登入用的是
        # 持久化 cookie，所以 reload 不會把我們登出。
        print("  [quota] dialog would not close; reloading the page")
        try:
            port.refresh()
        except port.TRANSPORT_ERRORS as error:
            _note_transport_error("reload after blocked dialog", error)
        time.sleep(8.0)
        # reload 之後**必須**把欄位填回去。實測（`WEBRunner.log`
        # 2026-08-24～08-27，65 次 reload）：61 次是在「這個角色已經存過圖」
        # 之後發生的，全部順利接回去；唯一一次發生在角色剛填完欄位、還沒產出
        # 任何一張圖的時候，接下來每一次 Generate 都無聲無息——沒有對話框、
        # `get_main_image_src` 整整 180 秒都回 None——連燒 10 張圖、兩個小時，
        # 直到 `consecutive_fail_abort` 才收工，而且 abort 訊息還誤指是
        # 「Chrome 崩潰」。差別就在站方有沒有來得及把剛填的提示詞持久化。
        # 這不只是卡住的問題：欄位**部分**被打回去（例如只掉了 Character 2）
        # 會安靜地產出一整批用錯提示詞的圖，那比停下來糟得多。
        if on_reload is not None:
            on_reload()
    # 事件節流：目標是「大約每小時一則」，免得長時間等待把頻道洗版。輪數要**跟著
    # `poll` 換算**、不能寫死——原本寫死每 6 輪（配 10 分鐘輪詢剛好一小時），
    # 預設一改成 1 小時就會變成六小時才回報一次。**跳過第 0 輪**——呼叫端已經在
    # 被擋的當下發過 `quota_blocked`，同一時刻再發一則等於連貼兩句一樣的話。
    #
    # 這一段的前提是**每一輪等長**，而那個前提成立：`batch_cfg` 是 `run_batch`
    # 每個角色重讀一次、再整份傳進 `generate_loop` 的，一次額度等待的累計完全
    # 落在同一個角色之內，所以 `poll` 不可能在累計途中換值。
    every = max(1, round(3600.0 / poll)) if poll > 0 else 1
    cycle = int(waited_sec // poll) if poll > 0 else 0
    if cycle > 0 and cycle % every == 0:
        emit_event("quota_wait", label=label, waited_sec=round(waited_sec, 1),
                   next_retry_sec=round(poll, 1))
    print(f"  [quota] generation blocked during {label}; waited "
          f"{waited_sec / 60:.0f} min so far, retrying in {poll / 60:.0f} min")
    # 切片睡：讓暫停標記與 `/stop` 有機會在等待中生效，而不是卡滿一整輪。
    remaining = poll
    browser_alive = True
    while remaining > 0:
        wait_if_paused(f"{label} (quota wait)")
        # 存活探測。放在切片的最前面：它是這裡最便宜的一次 round-trip，先問它才
        # 能把死亡時刻釘在 30 秒內，而不是等 `on_idle` 先吐出一堆連帶的失敗訊息。
        # **只記「活著 → 死了」那個轉換**：這個迴圈預設跑 120 圈，每圈都印一行就
        # 等於沒有訊號（本專案已經記過兩次同一條原則）。同理不發事件——那會把對話
        # 平台洗版，而且新事件型別要三邊對拉，這裡的價值是可診斷性，log 就夠了。
        gone = _probe_browser_alive(port)
        if gone is None:
            browser_alive = True
        elif browser_alive:
            browser_alive = False
            elapsed = poll - remaining
            print(f"  [quota] {time.strftime('%Y-%m-%d %H:%M:%S')} 瀏覽器在等額度"
                  f"的期間死掉了（這一輪已等 {elapsed:.0f}s，累計 "
                  f"{waited_sec + elapsed:.0f}s）：{gone}。這裡只記錄、不當場重啟"
                  f"——重啟要重新登入並重填欄位，交給既有的復原流程在原本的時機跑。",
                  file=sys.stderr)
        # 等額度的這一小時裡照樣要對使用者有反應，見 docstring 的 `on_idle`。
        if on_idle is not None:
            on_idle()
        slice_sec = min(30.0, remaining)
        time.sleep(slice_sec)
        remaining -= slice_sec
    return waited_sec + poll


def _abort_if_generation_blocked(port, where: str) -> None:
    """Tier 1 命中就 raise `GenerationBlockedError`（→ 乾淨停止、不重生）。

    對話框全文只寫進 log（stderr）——它是站方的原始字串，不受我們控制，依
    CLAUDE.md 保密規則不得送到對話平台。順帶：那行 log 就是回來收斂
    `_GENERATION_BLOCK_JS` pattern 的唯一依據。

    **文字後面那行印的是全文長度與離 `_TIER1_TEXT_CAP` 的餘裕。** 送進 log 的內容
    一個字都沒有變多（照樣是 `slice(0, 400)`），多的只有一個整數。它要回答的問題
    是「這道上限還剩多少餘裕」：站方的付費牆是整張定價表，多一列方案就可能跨過
    1200，而**跨過去是靜默的**——Tier 1 直接當作沒有對話框。餘裕變成量到的數字，
    下次就不必再猜。
    """
    hit = get_generation_block(port)
    if not hit:
        return
    text = str(hit.get("text", ""))[:400]
    print(f"  [blocked] generation is blocked by a purchase/account dialog "
          f"during {where}; matched {hit.get('pattern')!r}", file=sys.stderr)
    print(f"  [blocked] dialog text: {text!r}", file=sys.stderr)
    full_len = hit.get("length")
    if isinstance(full_len, int):
        print(f"  [blocked] dialog innerText full length {full_len} chars "
              f"(Tier 1 skips anything over {_TIER1_TEXT_CAP}; margin "
              f"{_TIER1_TEXT_CAP - full_len})", file=sys.stderr)
    raise GenerationBlockedError(
        f"generation blocked during {where} (matched {hit.get('pattern')!r}); "
        f"stopping instead of respawning — needs a human.")


def _is_chrome_crash_page(port) -> bool:
    """Detect the Chrome renderer crash interstitial (the "Aw, Snap!"
    page).

    When Chrome's tab renderer crashes the chromedriver session itself
    stays alive — `execute_script` keeps responding — but the page DOM
    is gone, replaced with the crash page. Hot-path readers
    (`get_main_image_src`, `find_generate_button` …) all return None,
    `generate_one_image` chews through its 4× retries, and the loop's
    `consecutive_fails` counter ticks. Without this detector, the run
    waits the full `consecutive_fail_abort` (~5 min default) before
    `generate_loop` raises and the supervisor can respawn Chrome.

    Detecting the interstitial directly lets us short-circuit to a
    supervisor respawn on the very first failed image, saving the
    intervening 4-5 minutes of empty retries.

    Wrapped in `port.TRANSPORT_ERRORS` because if chromedriver itself
    is also dead (not just the renderer), reading `current_url` /
    `title` would re-trigger the same transport stall we already
    mitigate elsewhere — treat the failure as "not crashed" so the
    caller falls back to its existing fail-counter path."""
    try:
        url = port.current_url() or ""
        title = (port.get_title() or "").lower()
    except port.TRANSPORT_ERRORS as error:
        # 只是卡頓 → 當作「沒崩」，照舊回落到呼叫端的失敗計數器；視窗／session
        # 真的沒了 → `_note_transport_error` 會升級成 BrowserGoneError。
        _note_transport_error("chrome crash probe", error)
        return False
    if "chrome-error://" in url:
        return True
    return any(marker in title for marker in _CHROME_CRASH_TITLE_MARKERS)


def _try_chrome_refresh(port) -> bool:
    """Best-effort `port.refresh()` after a detected crash interstitial.

    The supervisor is going to kill-and-respawn Chrome anyway (caller
    raises a `RuntimeError` right after this), so the refresh is a
    courtesy — gives Chrome a chance to clear the crashed renderer's
    state so the next user gets a cleaner profile. Failure here is
    fine; we still raise."""
    try:
        port.refresh()
        time.sleep(5.0)  # let the page settle before any post-snap
        return True
    except port.TRANSPORT_ERRORS as error:
        # 這裡刻意**不**升級成 BrowserGoneError：呼叫端正要 raise 了，refresh
        # 只是善後。壓成一行避免 chromedriver 的 Stacktrace 洗版。
        print(f"  [warn] refresh after chrome crash failed: "
              f"{_short_error(error)}")
        return False


def _abort_if_chrome_crashed(port, where: str) -> None:
    """Raise `RuntimeError` (→ supervisor respawn) if the page is currently
    the Chrome crash interstitial.

    `generate_loop` already short-circuits on the crash page inside its
    per-image fail branch, but a renderer crash during the SETUP / fill
    phase has no such guard: every `with_retry` step just burns its 3
    attempts on the dead DOM, `fill_char1` failures `continue` to the next
    pair, and the run can grind through every pair saving 0 images and then
    exit rc=0 — the supervisor sees a "clean finish" and never respawns.
    Calling this at the setup boundary and on fill failures turns that
    silent dead-end into an abort the supervisor can recover from.

    Checks TWO deaths, in order: the session being gone entirely (window
    closed / target destroyed — `_abort_if_browser_gone`), then the crash
    interstitial (session alive, DOM replaced). The order matters: on a gone
    session the crash-page probe can only report "couldn't read, assume no
    crash", so it would never fire."""
    _abort_if_browser_gone(port, where)
    if _is_chrome_crash_page(port):
        snap(port, f"chrome_crash_{where}")
        _try_chrome_refresh(port)
        raise RuntimeError(
            f"Chrome renderer crashed (interstitial detected) during "
            f"{where}; aborting so supervisor respawns Chrome."
        )


def click_via_js(port, element) -> None:
    port.execute_script("arguments[0].click();", element)


# 同意橫幅上「拒絕」那顆的字面候選，依序完全比對。
#
# 2026-08-30 用全新暫時 profile 實測站方首頁：橫幅上只有兩顆按鈕，字面是
# `Accept All` 與 `Reject Non-Essential`——**站方根本沒有 `Reject All`**。
# 原本的主要規則寫死比對 `Reject All`，所以從來沒有命中過一次；橫幅其實是靠
# 下面那條寬鬆的「含 Reject」退路關掉的。又是一個「退路每次都被走 ＝ 主要路徑
# 是死的」——差別在這次退路真的有效，所以外觀上完全正常。
#
# 為什麼還是要修：退路一旦被收緊（例如有人為了避免誤點而改成完全比對），同意
# 處理就會無聲失效，而症狀是橫幅蓋住頁面導致的隨機點選失敗，離成因很遠。
COOKIE_REJECT_LABELS = ("Reject Non-Essential", "Reject All")

_COOKIE_CONSENT_JS = r"""
const labels = arguments[0];
const btns = Array.from(document.querySelectorAll('button'));
const labelOf = b => (b.innerText || '').replace(/\s+/g, ' ').trim();
// 1) 已知字面，完全相符。
for (const want of labels) {
  for (const b of btns) {
    if (b.offsetParent !== null && labelOf(b) === want) {
      b.click();
      return {clicked: want, present: true, ready: true};
    }
  }
}
// 2) 退路：任何含 `Reject` 的按鈕。站方改字面時這條仍然接得住。
for (const b of btns) {
  const t = labelOf(b);
  if (b.offsetParent !== null && /reject/i.test(t)) {
    b.click();
    return {clicked: t, present: true, ready: true};
  }
}
// 沒點到。回報「這一頁上到底有沒有同意提示」，讓呼叫端可以早退而不必空等
// 到 timeout。只認**行動型**字面，而且 `^` 錨定是必要的：站方頁尾常駐一顆
// `Manage Cookie Preferences`，少了錨定它永遠符合，早退條件就永遠不成立，
// 這個最佳化會靜默失效（2026-08-30 實測，橫幅關掉後它仍然在）。
const ACTION = /^(reject|accept|allow|agree|decline|deny)\b/i;
let present = false;
for (const b of btns) {
  if (b.offsetParent !== null && ACTION.test(labelOf(b))) { present = true; break; }
}
return {clicked: null, present: present,
        ready: document.readyState === 'complete'};
"""


def reject_cookies(port, timeout: float = 15.0, settle: float = 2.0) -> bool:
    """關掉 cookie 同意橫幅。沒有橫幅時**盡快**回來，不要空等到 timeout。

    正式 profile 是常駐的，同意早就存過了，所以「沒有橫幅」才是常態——實測
    16/16 次都印 `cookie banner not found`，每次各燒滿 5 秒（登入頁）與 15 秒
    （產圖頁），一次 setup 白花 20 秒。

    早退條件刻意保守：要 `document.readyState === 'complete'`，而且連續
    `settle` 秒都看不到任何行動型同意按鈕才算數。橫幅存在但還不能點時
    （present=True）條件不成立，照舊等滿 `timeout`。
    """
    # 兩個都量「經過多久」→ 一律走 **單調**時鐘。`time.time()` 在本機是
    # `GetSystemTimePreciseAsFileTime()`、`adjustable=True`，NTP 的 step 修正／
    # 手動改時鐘／虛擬機快照還原都會讓它往前或往後跳（**換時區與日光節約時間
    # 不會**——它回的是 UTC epoch 秒）；拿它量間隔，往回跳一小時就
    # 是這個迴圈多轉一小時，往前跳則是 settle 還沒滿就宣告「確定沒有橫幅」。
    end = time.monotonic() + timeout
    quiet_since: float | None = None
    while time.monotonic() < end:
        state = port.execute_script(_COOKIE_CONSENT_JS,
                                    list(COOKIE_REJECT_LABELS)) or {}
        clicked = state.get("clicked")
        if clicked:
            human_pause(0.4, 0.8)
            print(f"cookie consent -> {clicked!r}")
            return True
        if state.get("ready") and not state.get("present"):
            if quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= settle:
                # 與下面的 timeout 訊息刻意不同字：一個是「確定沒有」、一個是
                # 「等到最後仍不確定」，看 log 的人要分得出是哪一種。
                print("no cookie consent prompt on this page; skipping")
                return False
        else:
            quiet_since = None
        time.sleep(0.3)
    print("cookie banner not found; skipping")
    return False


def _find_model_trigger(port):
    return port.execute_script(
        """
        const inp = document.querySelector("input[aria-label='Select the Model']");
        if (inp) {
          let el = inp;
          for (let i = 0; i < 6 && el; i++) {
            const cs = window.getComputedStyle(el);
            if (cs.cursor === 'pointer' || el.onclick || el.getAttribute('role') === 'button') {
              return el;
            }
            el = el.parentElement;
          }
          return inp.parentElement;
        }
        const candidates = [];
        for (const el of document.querySelectorAll('div, button, span')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t.startsWith('NAI Diffusion')) candidates.push([el, t.length]);
        }
        candidates.sort((a, b) => a[1] - b[1]);
        if (!candidates.length) return null;
        let el2 = candidates[0][0];
        for (let i = 0; i < 5 && el2; i++) {
          const cs = window.getComputedStyle(el2);
          if (cs.cursor === 'pointer' || el2.onclick || el2.getAttribute('role') === 'button') {
            return el2;
          }
          el2 = el2.parentElement;
        }
        return candidates[0][0];
        """
    )


def _dump_model_options(port) -> None:
    """把下拉裡當下看得到的模型選項印進 log（stderr／log 檔，不外流到對話平台）。

    站方改寫選項字面時，這行 log 是唯一能直接看出「`model_candidates` 該改成什麼」
    的證據——沒有它就只剩一張截圖要人工比對。純診斷，永不 raise。"""
    try:
        names = port.execute_script(
            """
            const out = [];
            for (const el of document.querySelectorAll('[role="option"], li, [role="listitem"], div')) {
              if (el.offsetParent === null) continue;
              const firstLine = (el.innerText || '').split('\\n')[0].trim();
              if (!firstLine || firstLine.length > 60) continue;
              if (!/Diffusion|NAI|Anime|Furry/i.test(firstLine)) continue;
              out.push(firstLine);
            }
            return Array.from(new Set(out)).slice(0, 30);
            """
        )
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [warn] could not dump model options: {type(error).__name__}")
        return
    print(f"  visible model options: {names}")


def select_model(port, target, timeout: float = 12.0) -> bool:
    """在模型下拉裡選出 `target`，成功回 True。

    `target` 可以是單一字串，也可以是**候選字面的序列**——站方偶爾改寫選項文字
    （這一輪就從 `V4.5 Full` 換成 `V5 Full`），依序嘗試、第一個命中就停，呼叫端
    因此能把新舊字面一起丟進來而不必動這裡的邏輯。

    比對維持「選項第一行**完全相等**」，刻意不做前綴比對：`NAI Diffusion V5`
    這種前綴會同時命中 Full 與 Curated，選錯模型比選不到更難察覺（照樣產得出
    圖，只是畫風不對）。

    所有候選都落空時，除了照舊 `snap()`，還會 `_dump_model_options()` 把當下看
    得到的選項寫進 log。
    """
    candidates = [target] if isinstance(target, str) else [t for t in target if t]
    # 逾時是**間隔** → 單調時鐘（理由詳見 `reject_cookies`）。牆鐘往回跳會把
    # 一次幾秒的 DOM 逾時變成幾小時的停頓——**卡住比失敗更難查**，監督者看到的
    # 是一個還活著卻什麼都不做的行程；往前跳則讓它退化成只試一次。
    end = time.monotonic() + timeout
    trigger = None
    while time.monotonic() < end:
        trigger = _find_model_trigger(port)
        if trigger:
            break
        time.sleep(0.3)
    if not trigger:
        snap(port, "no_model_selector")
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", trigger)
    print(f"model trigger tag={trigger.tag_name} text={trigger.text[:60]!r}")
    port.click(trigger)
    human_pause(0.6, 1.1)
    # 同上：選項出現的等待也是間隔 → 單調時鐘。
    option_end = time.monotonic() + timeout
    while time.monotonic() < option_end:
        clicked = port.execute_script(
            """
            const targets = arguments[0];
            const lists = Array.from(document.querySelectorAll('[role="option"], li, [role="listitem"], div'));
            for (const want of targets) {
              for (const el of lists) {
                if (el.offsetParent === null) continue;
                const firstLine = (el.innerText || '').split('\\n')[0].trim();
                if (firstLine === want) {
                  el.scrollIntoView({block:'center'});
                  el.click();
                  return want;
                }
              }
            }
            return null;
            """,
            candidates,
        )
        if clicked:
            human_pause(0.4, 0.8)
            print(f"  model -> {clicked!r}")
            return True
        time.sleep(0.3)
    print(f"  no model option matched any candidate: {candidates}")
    _dump_model_options(port)
    snap(port, "model_option_not_found")
    return False


def fill_textarea_like(port, element, text: str) -> bool:
    """Fill textarea / input / contenteditable with `text`，atomic replace。
    不走 `.send_keys(text)`、不走「先 clear 再寫」兩段式（會讓 React 在
    中間 commit 空字串、之後 re-render 蓋回我們寫的值 — 觀察到主 prompt
    填好之後又被清空就是這個 race）。

    機制：
    - textarea / input：用 `Object.getOwnPropertyDescriptor(...).value.set`
      一次寫進 value（繞過 React's value tracking）、`dispatchEvent('input')`
      + `'change'` 讓 React 看到 user-input 事件、同步 component state、
      最後 `blur()` 強制 commit。
    - contenteditable：全選現有內容 → `document.execCommand('insertText', …)`。
      這條路是 @testing-library/user-event 等框架推薦的寫法，會跑完整的
      `beforeinput` / `input` event 序列，React 的 onChange 能正常收到。
      execCommand 雖然 deprecated 但 Chrome 仍支援、且是目前最穩的方法。
      退而求其次才走 `innerText = val` + dispatchEvent fallback。

    Anti-bot 模擬由主迴圈在「不同 fill 之間」的 `human_pause(0.8, 1.5)`
    處理；單一 fill 內部不需要「逐字」假裝人類。

    流程：(1) scroll + click 拿 focus (2) atomic replace 一次寫 (3) blur
    強制 React commit (4) verify (5) 不一致就 re-click + 再寫一次 (6)
    仍不一致只印 WARN，caller 的 `with_retry` 會再給機會。
    """
    try:
        port.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", element,
        )
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    try:
        element.click()
    except Exception:  # pylint: disable=broad-except
        try:
            port.execute_script("arguments[0].focus();", element)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
    time.sleep(random.uniform(0.15, 0.35))
    # Atomic replace（內部處理 textarea / contenteditable 兩種 path）。
    _fill_via_native_setter(port, element, text)
    # 寫完先收掉 autocomplete 下拉再 blur，避免下拉殘留被後續點選誤觸。
    _dismiss_autocomplete(port, element)
    # Force React commit：blur 觸發 onBlur handler / component lifecycle。
    try:
        port.execute_script("arguments[0].blur();", element)
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    time.sleep(random.uniform(0.3, 0.6))
    actual = _read_textarea_value(port, element).strip()
    expected = text.strip()
    if actual == expected:
        return True
    print(
        f"  fill verify mismatch: got {len(actual)}/{len(expected)} chars; "
        f"retrying once"
    )
    try:
        element.click()
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    time.sleep(random.uniform(0.2, 0.4))
    _fill_via_native_setter(port, element, text)
    # retry 路徑同樣會重開下拉，一樣先 Escape 再 blur。
    _dismiss_autocomplete(port, element)
    try:
        port.execute_script("arguments[0].blur();", element)
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    time.sleep(random.uniform(0.3, 0.6))
    actual2 = _read_textarea_value(port, element).strip()
    if actual2 != expected:
        print(
            f"  WARN: fill still mismatched after retry "
            f"({len(actual2)} vs {len(expected)} chars)"
        )
        # 自動診斷：dump 一份 DOM textarea 屬性到 log，下次 debug 不必再
        # 等使用者拍 screenshot 或回報。
        for line in format_textareas_diag(dump_textareas_diag(port)).split("\n"):
            print(f"    {line}")
        return False
    return True


def _fill_via_native_setter(port, element, text: str) -> None:
    """Atomic replace via React-friendly mechanisms。一次寫 — 不分「clear」
    跟「write」兩個步驟，避免 React 在中間 re-render commit 空字串。

    - textarea / input：prototype value setter + input/change events
    - contenteditable：selectNodeContents + execCommand('insertText')，
      execCommand 失敗才 fallback 走 innerText 設定 + InputEvent

    任何 JS exception swallow + print 一行；caller 會 verify 跟 retry。
    """
    try:
        port.execute_script(
            """
            const e = arguments[0], val = arguments[1];
            try { e.focus(); } catch (_) {}
            if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') {
                const proto = e.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                setter.call(e, val);
                e.dispatchEvent(new Event('input', {bubbles: true}));
                e.dispatchEvent(new Event('change', {bubbles: true}));
            } else {
                // Contenteditable：全選 → execCommand('insertText') atomic
                // replace。execCommand 觸發完整的 beforeinput / input event
                // 鏈，React 的 onChange 才會 commit。
                try {
                    const range = document.createRange();
                    range.selectNodeContents(e);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(range);
                } catch (_) {}
                let ok = false;
                try { ok = document.execCommand('insertText', false, val); }
                catch (_) { ok = false; }
                if (!ok) {
                    // Fallback：execCommand 拿不到的環境改 innerText 一次寫。
                    e.innerText = val;
                    e.dispatchEvent(new InputEvent('input', {
                        bubbles: true, data: val, inputType: 'insertText'
                    }));
                }
            }
            """,
            element, text,
        )
    except Exception as error:  # pylint: disable=broad-except
        # `_short_error`：`fill_textarea_like` 一次填寫最多呼叫它兩次（初寫 ＋
        # 比對不符的重寫），而填寫本身又被 `with_retry` 包著重試。
        print(f"  _fill_via_native_setter exception: {_short_error(error)}",
              file=sys.stderr)


def driver_version_line(capabilities) -> str:
    """把「這次 session 真的用到的」瀏覽器與 driver 版本組成一行。永不 raise。

    版本一律從 driver 自己回報的 capabilities 取，不去查登錄檔或執行檔——要記的是
    **這次 spawn 實際接上的那兩個東西**，不是「機器上裝了什麼」。取不到就是 `?`。

    主版號不同時多加一句警告。那條路正常情況下走不到（chromedriver 拒絕驅動不同主版
    號的 Chrome，spawn 會先失敗），留著是因為它便宜、而且萬一哪天那個檢查放寬了，
    這正是當下唯一想看到的東西。

    **鍵名的來源，說清楚免得被當成已驗證**：`browserVersion` 是 W3C 的標準能力名，
    在裝著的 selenium 原始碼裡查得到（`options.py` 的 `_BaseOptionsDescriptor`）；
    `chromedriverVersion` 是 chromedriver 自己塞在 `chrome` 子字典裡回來的，
    **沒有辦法在不開瀏覽器的情況下實測**——而這台機器上正式批次幾乎總是在跑，開第二個
    Chrome 正是 `_chrome_slot` 存在的理由。所以這裡的設計是「猜錯也不會壞」：取不到就
    印 `?`，那一行本身就會告訴你鍵名該修了，而且絕不影響已經成功的 spawn。
    """
    caps = capabilities if isinstance(capabilities, dict) else {}
    browser = str(caps.get("browserVersion") or "?").strip() or "?"
    driver = "?"
    chrome = caps.get("chrome")
    if isinstance(chrome, dict):
        raw = str(chrome.get("chromedriverVersion") or "").strip()
        driver = raw.split(" ", 1)[0] or "?"
    line = f"chrome {browser} / chromedriver {driver}"
    b_major, _, _ = browser.partition(".")
    d_major, _, _ = driver.partition(".")
    if b_major.isdigit() and d_major.isdigit() and b_major != d_major:
        line += "  ** 主版號不符 **"
    return line


def log_driver_versions(capabilities) -> str:
    """印出 `driver_version_line(...)` 並回傳它。永不 raise——這是 spawn 成功之後的
    一行紀錄，不得有任何機會把成功的 spawn 變成失敗。

    **為什麼值得一行**：Chrome 從 2026 年 9 月起改成**兩週一個主版本**，而
    chromedriver 的主版號必須完全相符，否則 `webdriver.Chrome(...)` 丟
    `SessionNotCreatedException`。本專案 2026-08-25 真的踩過一次，log 裡只有
    `Chrome spawn attempt 1/3 failed: SessionNotCreatedException()`，別的什麼都沒有。
    ⚠️ **這支函式原本的理由寫著「那個例外的訊息是空的」——2026-09-12 實測推翻，
    已撤回。** 空的是 `args`，不是訊息：selenium 把訊息放在 `self.msg`，所以
    `repr(e)` 回 `SessionNotCreatedException()` 而 `str(e)` 有 351 字元、含兩邊
    版本號。那一行 log 是被**我們自己的 `{err!r}` 格式**丟掉的，2026-09-12 改由
    `full_error_detail` 處理。**這件事本身是一課**：一個「診斷資料不存在」的結論，
    在補新的診斷之前要先確認不是自己的格式化把它吃掉了——否則補上去的新機制會
    掩蓋掉原缺陷，而原缺陷還在別的路徑上活著（實測當時還有 7 個同形狀的站點）。
    這一行仍然值得留著，但理由換了：它在 spawn **成功**時就先把版本記下來，所以
    失敗那一刻不必仰賴任何例外格式化，而且 `restart_chrome_every_n_characters`
    預設 1 ＝ 每個角色都會重新記一次。這台機器無人值守跑好幾天，log 是唯一的鑑識
    紀錄。
    """
    line = driver_version_line(capabilities)
    print(f"  [driver] {line}")
    return line


def _dismiss_autocomplete(port, element) -> None:
    """收掉 NovelAI 的 tag autocomplete 下拉。

    寫完字後游標停在文字尾端，NovelAI 會對最後一個 token 彈出建議下拉；
    不關掉的話，後續的座標點選（展開 Character 區塊、按 Generate…）可能
    誤觸下拉，把一個建議 tag 插進 prompt 尾端。合成 Escape 帶 keyCode/
    which=27 以相容 React legacy 事件處理。失敗 swallow — 下拉沒收掉
    也不能炸 run，產圖前的 verify_character_prompt 是第二道防線。"""
    try:
        port.execute_script(
            """
            const e = arguments[0];
            const ev = t => new KeyboardEvent(t, {
                key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
                bubbles: true, cancelable: true});
            e.dispatchEvent(ev('keydown'));
            e.dispatchEvent(ev('keyup'));
            """,
            element,
        )
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass


def find_prompt_areas(port):
    xpath = (
        "//*[(self::textarea or @contenteditable='true')"
        " and not(@aria-label='Select a language')]"
    )
    return [el for el in port.find_elements_xpath(xpath) if el.is_displayed()]


def fill_main_prompt(port, text: str) -> bool:
    areas = find_prompt_areas(port)
    if not areas:
        snap(port, "no_prompt_area")
        return False
    main_area = areas[0]
    try:
        main_area.click()
    except Exception:  # pylint: disable=broad-except
        pass
    human_pause(0.2, 0.5)
    return fill_textarea_like(port, main_area, text)


def find_undesired_textarea(port):
    """Locate the main "Undesired Content" textarea on NovelAI.

    NovelAI 的 React DOM 把 styled-component class 名混淆掉了，每次 deploy
    都會變，所以這裡用三層 fallback 來找：
      1. textarea / contenteditable 且 `aria-label` 含 "undesired"
      2. 同樣的元素但用 `placeholder` 比對
      3. 找文字 "Undesired Content" 的 label，往下找下一個 textarea

    **排除主 prompt 元素**（`find_prompt_areas()` 第一個可見項）— 觀察到
    部分 deploy 的主 prompt placeholder 含 "undesired"（顯示「Add a prompt
    or click to choose Undesired Content...」之類的提示），會誤命中。所以
    任何 step 配對到 main prompt 那個 element 直接 reject、繼續找下一個。

    全部失敗就 `snap()` 拍張 debug 圖回 None；caller 應該把這當「跳過 fill」
    而不是 abort 整個 run（NovelAI 會沿用上一輪殘留的 undesired 值）。
    """
    main_areas = find_prompt_areas(port)
    main_area = main_areas[0] if main_areas else None

    def _acceptable(el) -> bool:
        if not el.is_displayed():
            return False
        if main_area is not None and el == main_area:
            return False
        return True

    # 1. aria-label（最穩，NovelAI 普遍會帶）
    candidates = port.find_elements_xpath(
        "//*[(self::textarea or @contenteditable='true')"
        " and contains("
        "translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'undesired')]"
    )
    for el in candidates:
        if _acceptable(el):
            return el
    # 2. placeholder
    candidates = port.find_elements_xpath(
        "//*[(self::textarea or @contenteditable='true')"
        " and contains("
        "translate(@placeholder,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
        "'undesired')]"
    )
    for el in candidates:
        if _acceptable(el):
            return el
    # 3. 'Undesired Content' label 之後第一個 textarea / contenteditable，
    # 用 XPath `following::` axis 直接抓，不必爬 DOM tree。
    candidates = port.find_elements_xpath(
        "//*[normalize-space(text())='Undesired Content']"
        "/following::*[self::textarea or @contenteditable='true'][1]"
    )
    for el in candidates:
        if _acceptable(el):
            return el
    snap(port, "no_undesired_textarea")
    # 自動診斷：找不到時直接 dump，讓 selector 調整有依據。
    print("find_undesired_textarea: no acceptable element after 3 fallbacks")
    for line in format_textareas_diag(dump_textareas_diag(port)).split("\n"):
        print(f"  {line}")
    return None


def fill_main_undesired(port, text: str) -> bool:
    """Fill NovelAI 主 undesired content（negative prompt）textarea。
    找不到時回 False、不丟例外、不 abort run — caller 應該繼續產圖、
    NovelAI 會用上一輪殘留的值。空字串 `text` 仍會去 clear 該 textarea。"""
    el = find_undesired_textarea(port)
    if el is None:
        return False
    try:
        port.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        el.click()
    except Exception:  # pylint: disable=broad-except
        pass
    human_pause(0.2, 0.4)
    return fill_textarea_like(port, el, text)


def _click_gender(port, gender: str, timeout: float = 4.0) -> bool:
    """點加角色之後跳出的性別選項（V4.5：Female／Male／…；V5：Female／Male／Other）。

    不用 JS 的 `el.click()`：那個選單跟取樣器下拉一樣可能只聽 mousedown，JS click
    會**靜默無效**（見 `robust_click`）。改成先取到元素、再走真實滑鼠點選。
    取 `outerHTML` 最短的那個，避免點到只是包住選項的外層 wrapper。
    """
    # 逾時是**間隔** → 單調時鐘（理由詳見 `reject_cookies`）。牆鐘往回跳會把
    # 一次幾秒的 DOM 逾時變成幾小時的停頓——**卡住比失敗更難查**，監督者看到的
    # 是一個還活著卻什麼都不做的行程；往前跳則讓它退化成只試一次。
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        el = port.execute_script(
            """
            const want = arguments[0];
            let best = null, bestLen = Infinity;
            for (const el of document.querySelectorAll(
                   'div, li, button, span, [role="option"]')) {
              if (el.offsetParent === null) continue;
              if ((el.innerText || '').trim() !== want) continue;
              const len = el.outerHTML.length;
              if (len < bestLen) { best = el; bestLen = len; }
            }
            return best;
            """,
            gender,
        )
        if el is not None:
            robust_click(port, el)
            human_pause(0.3, 0.6)
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# 角色卡定位（V4.5 → V5 的遷移核心）
#
# V4.5：角色名是純文字節點（`innerText === 'Character N'`），而且**只有目前
#       focus 的那張卡**會顯示 prompt 欄，所以舊碼可以直接用
#       `find_prompt_areas()[1]` 當「目前這個角色的欄位」。
# V5  ：角色名變成**可改名的 `<input>`**，名字在 `placeholder`、`innerText` 是
#       空字串 —— 所有靠 innerText 的偵測都會**靜默回 0／找不到**（不報錯）；
#       而且展開後**每張卡的 prompt 欄同時可見**，`find_prompt_areas()` 會回
#       `[主 prompt, char1, char2, …]`。
#
# 所以這裡一律改成「先定位角色卡，再取那張卡自己的 prompt 欄」。index 算術
# （`areas[1]`）在 V5 下會寫進**錯的卡**，而且錯了不會報錯、只會產出角色錯亂
# 的圖 —— 是最難從結果察覺的一類失敗。
#
# 另外：V5 的 DOM 會桌面／行動**雙渲染**，同一個角色 input 會出現兩份、其中一
# 份 `offsetParent === null`。任何掃描都必須濾掉不可見的那份，否則角色數會加倍。
# ---------------------------------------------------------------------------

# 補完整的 pointer/mouse 事件序列。給「只聽 mousedown / pointerdown」的元件當
# 備援 —— 單純的 `el.click()` 對它們是**靜默**無效。
_MOUSE_SEQUENCE_JS = """
const el = arguments[0];
const opts = {bubbles: true, cancelable: true, view: window};
for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
  const Ctor = (type.indexOf('pointer') === 0 && window.PointerEvent)
             ? window.PointerEvent : window.MouseEvent;
  try { el.dispatchEvent(new Ctor(type, opts)); } catch (e) {}
}
return true;
"""

# 角色卡相關的共用 JS。每個用到的 script 都把這段 prepend 進去（`execute_script`
# 每次都是全新的 scope，沒有跨呼叫的全域可以放）。
_JS_CHAR_HELPERS = r"""
function __charNameEls() {
  // V5 優先：可改名的 input，名字在 placeholder。
  const out = [];
  for (const el of document.querySelectorAll('input')) {
    if (el.offsetParent === null) continue;
    const ph = (el.getAttribute('placeholder') || '').trim();
    if (/^Character \d+$/.test(ph)) out.push([el, ph]);
  }
  if (out.length) return out;
  // V4.5 退路：純文字標題。
  for (const el of document.querySelectorAll('div, span, button, label')) {
    if (el.offsetParent === null) continue;
    const t = (el.innerText || '').trim();
    if (/^Character \d+$/.test(t)) out.push([el, t]);
  }
  return out;
}
function __findCharName(want) {
  const all = __charNameEls();
  for (let i = 0; i < all.length; i++) {
    if (all[i][1] === want) return all[i][0];
  }
  return null;
}
function __charHeaderRow(nameEl) {
  // 標題列 = 往上第一個含 >=3 顆按鈕的祖先（上移／下移／勾／垃圾桶／展開）。
  let el = nameEl;
  for (let i = 0; i < 10 && el; i++) {
    el = el.parentElement;
    if (!el) break;
    if (el.querySelectorAll('button').length >= 3) return el;
  }
  return null;
}
function __charCard(nameEl) {
  // 卡片 = 往上第一個「含至少一個可見 prompt 欄」的祖先；但若該祖先同時包住
  // 不只一張卡的名稱欄，代表走過頭了（那是整個 Character Prompts 區塊），
  // 回 null 讓呼叫端知道這張卡目前是收合的。
  let el = nameEl;
  for (let i = 0; i < 12 && el; i++) {
    el = el.parentElement;
    if (!el) break;
    let areas = 0;
    for (const a of el.querySelectorAll("textarea, [contenteditable='true']")) {
      if (a.offsetParent !== null) areas++;
    }
    if (!areas) continue;
    let names = 0;
    for (const n of el.querySelectorAll('input')) {
      if (n.offsetParent === null) continue;
      const ph = (n.getAttribute('placeholder') || '').trim();
      if (/^Character \d+$/.test(ph)) names++;
    }
    if (names > 1) return null;
    return el;
  }
  return null;
}
function __cardIcon(nameEl, iconName) {
  // V5 的卡片按鈕沒有 aria-label／title，但 CSS mask 的檔名有語意：
  // directional_arrow_up / directional_arrow_down / check / trash / unfold。
  // 檔名帶 content hash（trash.72ef2ba9.svg），所以只比對**基底名**。
  const row = __charHeaderRow(nameEl);
  if (!row) return null;
  for (const b of row.querySelectorAll('button')) {
    if (b.disabled || b.offsetParent === null) continue;
    const nodes = [b].concat(Array.from(b.querySelectorAll('*')));
    for (const node of nodes) {
      const st = window.getComputedStyle(node);
      const mi = ((st.maskImage || '') + ' ' + (st.webkitMaskImage || '')
                  + ' ' + (st.backgroundImage || '')).toLowerCase();
      if (mi.indexOf(iconName) !== -1) return b;
    }
  }
  return null;
}
"""


def robust_click(port, element) -> bool:
    """真實滑鼠點選；點不動就補完整 pointer/mouse 事件序列。

    為什麼需要：V5 有些控制項（取樣器下拉、加角色後的性別選單）是只聽
    mousedown/pointerdown 的元件，純 JS `el.click()` 對它們**靜默無效** ——
    不丟錯、看起來點到了、實際沒反應。呼叫端一律要自己讀回驗證。
    """
    try:
        port.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", element)
    except Exception:  # pylint: disable=broad-except
        pass
    try:
        port.click(element)
        return True
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [warn] real click failed ({type(error).__name__}); "
              f"falling back to synthetic pointer events")
    return dispatch_mouse_sequence(port, element)


def dispatch_mouse_sequence(port, element) -> bool:
    """直接補 pointerdown→mousedown→pointerup→mouseup→click。用在「真的點了但
    元件沒反應」的重試路徑。"""
    try:
        port.execute_script(_MOUSE_SEQUENCE_JS, element)
        return True
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [warn] synthetic click failed: {type(error).__name__}")
        return False


def find_character_name_element(port, label: str):
    """回傳代表某張角色卡的「名稱元素」（V5 是 input、V4.5 是文字節點）。"""
    return port.execute_script(
        _JS_CHAR_HELPERS + "return __findCharName(arguments[0]);", label)


def character_card_area(port, label: str):
    """回傳**該角色卡自己的** prompt 欄；卡片不存在或收合中回 None。

    刻意不回 `find_prompt_areas()[N]`：V5 下所有角色欄同時可見，用 index 取會
    在「Character 2 被移除」等情況下靜默取到別人的欄位。
    """
    return port.execute_script(
        _JS_CHAR_HELPERS + """
        const nameEl = __findCharName(arguments[0]);
        if (!nameEl) return null;
        const card = __charCard(nameEl);
        if (!card) return null;
        for (const a of card.querySelectorAll(
               "textarea, [contenteditable='true']")) {
          if (a.offsetParent !== null) return a;
        }
        return null;
        """,
        label,
    )


def ensure_character_expanded(port, label: str) -> bool:
    """確保某張角色卡是展開的（prompt 欄看得到），回傳是否成功。

    V5 收合的卡片不顯示 prompt 欄，要按標題列的 `unfold` 圖示展開。V4.5 則是
    點標題本身切換 focus —— 兩種都試，以「該卡的 prompt 欄是否出現」為準，
    不靠回傳值猜。
    """
    if character_card_area(port, label) is not None:
        return True
    name_el = find_character_name_element(port, label)
    if name_el is None:
        return False
    icon = port.execute_script(
        _JS_CHAR_HELPERS + "return __cardIcon(arguments[0], 'unfold');",
        name_el)
    if icon is not None:
        robust_click(port, icon)
        human_pause(0.8, 1.2)
        if character_card_area(port, label) is not None:
            return True
    # V4.5 退路：點標題列本身（那邊是 focus 語意，不是展開語意）。
    row = port.execute_script(
        _JS_CHAR_HELPERS + "return __charHeaderRow(arguments[0]);", name_el)
    if row is not None:
        robust_click(port, row)
        human_pause(0.8, 1.2)
    return character_card_area(port, label) is not None


def click_add_character_control(port, gender: str = "Female",
                                timeout: float = 8.0) -> bool:
    """按下「加一個角色」並選性別。兩個 webrunner 變體共用這一份。

    V4.5 是一顆寫著 `Add Character` 的按鈕；**V5 換成「Character Prompts」標題
    列裡一顆 44x40、沒有文字／沒有 aria-label／沒有 title 的圖示按鈕**，所以舊的
    `//button[contains(., 'Add Character')]` 永遠找不到。這裡兩種都認：先找舊按
    鈕，找不到就退到標題列的圖示鈕。性別選單 V5 是 Female／Male／Other。
    """
    # 逾時是**間隔** → 單調時鐘（理由詳見 `reject_cookies`）。牆鐘往回跳會把
    # 一次幾秒的 DOM 逾時變成幾小時的停頓——**卡住比失敗更難查**，監督者看到的
    # 是一個還活著卻什麼都不做的行程；往前跳則讓它退化成只試一次。
    end = time.monotonic() + timeout
    btn = None
    while time.monotonic() < end:
        for cand in port.find_elements_xpath(
                "//button[contains(., 'Add Character')]"):
            try:
                if cand.is_displayed() and cand.is_enabled():
                    btn = cand
                    break
            except Exception:  # pylint: disable=broad-except
                continue
        if btn is not None:
            break
        btn = port.execute_script(
            """
            // 「Character Prompts」標題列：同時含該標題文字與至少一顆可見按鈕的
            // 最小可見元素。用「最小且含 button」而不是往上走固定層數 —— 加了
            // 角色卡之後 DOM 深度會變，寫死層數會漂掉。
            let best = null, bestLen = Infinity;
            for (const el of document.querySelectorAll('div, span, section')) {
              if (el.offsetParent === null) continue;
              const t = (el.innerText || '').trim();
              if (t.indexOf('Character Prompts') !== 0) continue;
              let has = false;
              for (const b of el.querySelectorAll('button')) {
                if (b.offsetParent !== null) { has = true; break; }
              }
              if (!has) continue;
              const len = el.outerHTML.length;
              if (len < bestLen) { best = el; bestLen = len; }
            }
            if (!best) return null;
            const btns = [];
            for (const b of best.querySelectorAll('button')) {
              if (b.offsetParent !== null && !b.disabled) btns.push(b);
            }
            return btns.length ? btns[btns.length - 1] : null;
            """
        )
        if btn is not None:
            break
        time.sleep(0.3)
    if btn is None:
        print("  add-character control not found")
        snap(port, "no_add_character")
        return False
    robust_click(port, btn)
    human_pause(0.6, 1.0)
    if not _click_gender(port, gender, timeout=4.0):
        print(f"  WARN: gender option {gender!r} not clicked")
    human_pause(0.4, 0.8)
    return True


def count_characters(port) -> int:
    """Count how many Character N slots are currently shown.

    **Must return a real int — callers do `while have > N`.** Selenium's
    `execute_script` is typed `Any` and genuinely *can* return `None`
    (Chrome handing back a null script result when the page is
    mid-transition or the renderer is under memory pressure — OOM is the
    dominant failure mode on these boxes). A raw `None` here would flow
    straight into `ensure_two_characters` / `remove_all_character_slots`'s
    `while have > N`, raising
    `'>' not supported between instances of 'NoneType' and 'int'` — which,
    on the single-image idle serve path, aborted the whole request with a
    raw TypeError. Coerce defensively: a non-int result becomes 0
    ("couldn't count / nothing visible"), which the no-progress guards in
    both loops handle gracefully (trim becomes a no-op; the idle serve
    still clears residual character areas via `find_prompt_areas`)."""
    raw = port.execute_script(
        _JS_CHAR_HELPERS + """
        let max = 0;
        for (const pair of __charNameEls()) {
          const m = pair[1].match(/^Character (\\d+)$/);
          if (m) {
            const n = parseInt(m[1], 10);
            if (n > max) max = n;
          }
        }
        return max;
        """
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(f"  WARN: count_characters got non-int result {raw!r}; "
              f"treating as 0", file=sys.stderr)
        return 0


def debug_character_buttons(port, label: str) -> None:
    info = port.execute_script(
        _JS_CHAR_HELPERS + """
        const want = arguments[0];
        const header = __findCharName(want);
        if (!header) return [];
        // Walk up to a container that has many buttons (the entire character card).
        let card = header;
        for (let i = 0; i < 10 && card; i++) {
          card = card.parentElement;
          if (!card) break;
          if (card.querySelectorAll('button').length >= 5) break;
        }
        if (!card) return [];
        return Array.from(card.querySelectorAll('button')).map((b, i) => {
          const r = b.getBoundingClientRect();
          return {
            i,
            aria: b.getAttribute('aria-label'),
            title: b.getAttribute('title'),
            text: (b.innerText || '').slice(0, 30),
            disabled: b.disabled,
            x: Math.round(r.x),
            y: Math.round(r.y),
            w: Math.round(r.width),
            h: Math.round(r.height),
            html: b.outerHTML.slice(0, 200),
          };
        });
        """,
        label,
    )
    print(f"  ALL buttons in {label} card:")
    for b in info:
        print(f"    [{b['i']}] aria={b['aria']!r} title={b['title']!r} text={b['text']!r}"
              f" disabled={b['disabled']} pos=({b['x']},{b['y']},{b['w']}x{b['h']})")
        print(f"        html={b['html']!r}")


def remove_character_slot(port, label: str) -> bool:
    """Focus Character N then click its trash icon (via port.click)."""
    # Focus the character first so its trash icon is visible.
    expand_character_section(port, label)
    human_pause(0.4, 0.7)
    debug_character_buttons(port, label)
    # Find the trash button — search the whole document for aria-label / svg
    # path patterns commonly used for delete.
    btn = port.execute_script(
        _JS_CHAR_HELPERS + """
        const want = arguments[0];
        const header = __findCharName(want);
        if (!header) return null;
        // V5 快路：圖示的 CSS mask 檔名就叫 trash.<hash>.svg，直接認它。
        const direct = __cardIcon(header, 'trash');
        if (direct) return direct;
        let card = header;
        for (let depth = 0; depth < 10 && card; depth++) {
          const buttons = Array.from(card.querySelectorAll('button'));
          for (const b of buttons) {
            if (b.disabled || b.offsetParent === null) continue;
            const r = b.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            const semantic = ((b.getAttribute('aria-label') || '') + ' '
              + (b.getAttribute('title') || '')).toLowerCase();
            if (/delete|remove|trash|bin/.test(semantic)) return b;
            for (const node of [b, ...b.querySelectorAll('*')]) {
              const style = window.getComputedStyle(node);
              const icon = ((style.maskImage || '') + ' '
                + (style.webkitMaskImage || '') + ' '
                + (style.backgroundImage || '')).toLowerCase();
              if (/trash|delete|remove|bin/.test(icon)) return b;
            }
          }
          card = card.parentElement;
        }
        return null;
        """,
        label,
    )
    if not btn:
        # NovelAI icon buttons have no semantic aria/title. The card header
        # controls are ordered move-up, move-down, delete. Select the rightmost
        # visible enabled icon on that header row. The previous "last
        # descendant button" fallback could click an off-screen Position or
        # AI Choice control while reporting success.
        btn = port.execute_script(
            _JS_CHAR_HELPERS + """
            const want = arguments[0];
            const header = __findCharName(want);
            if (!header) return null;
            const hr = header.getBoundingClientRect();
            let card = header;
            for (let depth = 0; depth < 10 && card; depth++) {
              const buttons = Array.from(card.querySelectorAll('button'))
                .filter(b => {
                  if (b.disabled || b.offsetParent === null) return false;
                  const r = b.getBoundingClientRect();
                  const text = (b.innerText || '').trim();
                  return r.width > 0 && r.height > 0 && !text
                    && Math.abs((r.y + r.height / 2)
                              - (hr.y + hr.height / 2)) < 35;
                });
              // V5 的標題列順序是 上移／下移／勾／垃圾桶／**展開**，最右邊
              // 那顆是 unfold 不是刪除 —— 舊的「取最右邊」會點錯而且回報成功。
              // 先用 CSS mask 檔名排掉已知的非刪除圖示。
              const notTrash = /directional_arrow|unfold|check/;
              const filtered = buttons.filter(b => {
                const nodes = [b].concat(Array.from(b.querySelectorAll('*')));
                for (const node of nodes) {
                  const st = window.getComputedStyle(node);
                  const mi = ((st.maskImage || '') + ' '
                            + (st.webkitMaskImage || '')).toLowerCase();
                  if (notTrash.test(mi)) return false;
                }
                return true;
              });
              const pool = filtered.length ? filtered : buttons;
              if (pool.length >= 1 && buttons.length >= 2) {
                pool.sort((a, b) =>
                  b.getBoundingClientRect().x - a.getBoundingClientRect().x);
                return pool[0];
              }
              card = card.parentElement;
            }
            return null;
            """,
            label,
        )
    if not btn:
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
    port.click(btn)
    human_pause(0.4, 0.8)
    # Some UIs show a confirm dialog — click any "Confirm" / "Yes" / "Delete".
    for confirm in ("Confirm", "Yes", "Delete", "OK"):
        if _click_option_by_text(port, confirm, timeout=0.6):
            human_pause(0.3, 0.6)
            break
    return True


def set_character2_enabled(port, enabled: bool) -> bool:
    """Make the NovelAI UI contain Character 2 exactly when enabled is true.

    Empty char2 queue entries must remove the second card, not merely skip
    filling it: a still-present card can retain the preceding pair's prompt
    and leak that character into the next generation. If a later dynamic
    queue entry needs char2 again, add the card back before filling it.
    """
    have = count_characters(port)
    if enabled:
        if have >= 2:
            return True
        # 加角色的控制項在 V4.5／V5 長得完全不同，統一走共用實作。
        if not click_add_character_control(port, "Female"):
            return False
        human_pause(0.5, 0.9)
        now = count_characters(port)
        if now < 2:
            print(f"  WARN: Add Character did not create Character 2 "
                  f"(count={now})")
            snap(port, "character2_add_failed")
            return False
        print("  added Character 2 for non-empty char2 prompt")
        return True

    if have < 2:
        return True
    if not remove_character_slot(port, "Character 2"):
        snap(port, "character2_remove_failed")
        return False
    human_pause(0.5, 0.9)
    now = count_characters(port)
    if now >= 2:
        print(f"  WARN: Character 2 still present after delete (count={now})")
        snap(port, "character2_remove_failed")
        return False
    print("  removed Character 2 because char2 prompt is empty")
    return True


def remove_all_character_slots(port) -> None:
    """One-shot helper：把「所有」角色 SLOT 整個刪光（target 硬寫死為 0），讓單圖
    的生成 UI 真的剩 0 個角色框、只留主 prompt。**只在 idle one-shot 路徑呼叫**
    （in_band=False：serve 完 main() 直接 return 0、後面不再跑 batch；in-band 路徑
    刪框會讓 _refill_character_fields 重填不回去 → batch 角色剩餘的圖以缺框產生）。
    **2026-09-05 更正**：原本這裡寫「找不到 areas[1] 而靜默失敗」，那是舊的
    index 取法。`fill_character_prompt` 早就改成先定位角色卡片，而且找不到卡片時
    會印 `"{label} slot not present; cannot fill"` 並回 False——**不是靜默**。
    結論沒變（in-band 不准刪框），但別再去找一個不存在的靜默失敗。

    NovelAI 的角色框編號為 Character 1..N，且只能從「尾端」乾淨移除（與
    ensure_two_characters 同理，避免重新編號）。純 best-effort：不 raise（沿用既有
    移除流程，呼叫端再以內容驗證）。

    **NovelAI 強制至少保留 1 個角色框**：最後一框沒有 trash 按鈕，
    remove_character_slot 的 trash 搜尋找不到、退回 fallback 點到「該框最後一個
    按鈕」（不是刪除鈕），不會真的刪掉卻仍回 True；本函式的 no-progress guard
    （new_count >= have）因而在剩 1 框時 break。故本函式通常停在 1 框、且該框可能
    殘留內容 — 呼叫端（idle one-shot serve）必須在呼叫本函式之後，把殘存的角色框
    清成空字串（以 find_prompt_areas[1:] 為準、內容導向驗證），不能只靠 count==0。"""
    have = count_characters(port)
    print(f"one-shot remove-all-slots: have {have}, target 0")
    # Trim from the tail (Character N, Character N-1, …) — copy
    # ensure_two_characters's no-progress guard exactly.
    safety = 0
    while have > 0 and safety < 20:
        safety += 1
        label = f"Character {have}"
        ok = remove_character_slot(port, label)
        human_pause(0.5, 0.9)
        new_count = count_characters(port)
        if not ok or new_count >= have:
            print(f"  could not remove {label} (delete clicked={ok}, count={new_count})")
            break
        have = new_count
        print(f"  removed {label}; now {have} slot(s)")


def expand_character_section(port, label: str) -> bool:
    """讓 Character N 的 prompt 欄變成可寫（存在且展開），成功回 True。

    V4.5 的語意是「focus 這張卡」（只有 focus 的卡看得到欄位）；V5 的語意是
    「展開這張卡」（展開後所有卡的欄位同時可見）。兩者的**可觀察結果**一樣：
    這張卡自己的 prompt 欄拿得到。所以這裡一律以
    `character_card_area()` 是否拿得到為準，不去猜是哪一種語意，也不再靠
    「點了幾次／狀態有沒有變」這種脆弱訊號。
    """
    return ensure_character_expanded(port, label)


def _read_textarea_value(port, element) -> str:
    return port.execute_script(
        "const e = arguments[0];"
        "return (e.tagName==='TEXTAREA' || e.tagName==='INPUT') ? e.value : (e.innerText || '');",
        element,
    )


def fill_character_prompt(port, character_index: int, text: str) -> bool:
    """把 `text` 寫進 **Character N 自己的** prompt 欄。

    **不再用 `find_prompt_areas()[1]`。** V4.5 只顯示 focus 那張卡的欄位，所以
    index 1 剛好就是它；V5 展開後所有角色欄同時可見
    （`areas = [主 prompt, char1, char2, …]`），沿用 index 1 會在「Character 2 被
    移除」時把 **char1** 的內容洗掉——而且不會報錯，只會產出角色錯亂的圖，是最
    難從結果察覺的一類失敗。改成先定位該角色的卡片、再取卡片內的欄位。
    """
    label = f"Character {character_index}"
    if find_character_name_element(port, label) is None:
        # 卡片根本不存在（空的 todo_character2 列會讓 set_character2_enabled 把
        # 整張卡移除）。要求「清空」視為已達成（本來就沒內容）；要求寫入非空值
        # 則回 False，讓呼叫端的 retry / skip 去處理。
        print(f"  {label} slot not present; "
              + ("nothing to clear" if not text.strip() else "cannot fill"))
        return not text.strip()
    if not ensure_character_expanded(port, label):
        print(f"  {label} card present but could not be expanded")
        snap(port, f"missing_{label.lower().replace(' ', '_')}")
        return False
    el = character_card_area(port, label)
    if el is None:
        snap(port, f"missing_{label.lower().replace(' ', '_')}")
        return False
    existing = _read_textarea_value(port, el)
    # 措辭必須自己講明這是**寫入前**的讀值。這是通用的 fill 記錄，而在
    # verify → refill 這條路上它緊接在「欄位對不上，重填」後面出現，語意剛好
    # 相反：讀的人會把它當成「重填之後的狀態」，於是一個空值看起來像「重填完
    # 還是空的」——正好長得像本專案最貴那類失效（欄位被打回去，接著安靜產出
    # 一整批用錯提示詞的圖）。它實際證明的是相反的事：重填前確實是空的，所以
    # 這次重填有東西可修。留著它、只改措辭，因為那個證據本身有用。
    print(f"[{label}] card area before write: {existing[:60]!r}")
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    try:
        el.click()
    except Exception:  # pylint: disable=broad-except
        pass
    human_pause(0.2, 0.4)
    return fill_textarea_like(port, el, text)


def verify_character_prompt(port, character_index: int, expected: str) -> bool:
    """產圖前讀回 character 欄位、確認內容仍等於預期值。

    fill 當下的 verify 擋不住「fill 之後」的污染：autocomplete 下拉殘留
    被後續座標點選誤觸時，會把一個建議 tag 插進 prompt 尾端，且各條目
    最後一個 token 不同、看起來像隨機 tag。不符時重填一次；仍不符只
    WARN 繼續，不 abort（沿用現值比中斷整個 run 有用）。240 張共用同
    一份 prompt、generate 迴圈內不碰欄位，所以每個角色批次驗一次即可。

    跟 `fill_character_prompt` 一樣改成卡片導向——用 index 取欄位會在卡片數量
    變動時比對到別人的欄位，誤判成 drift 之後「重填」反而把別的角色洗掉。

    **記錄合約：每一條路都要講出結局，包含成功那一條。** 讀記錄的人不該為了
    知道「那個角色到底有沒有救回來」而去讀原始碼。三種結局各有一行：欄位空的
    （note，穩態，見下面的分流註解）、欄位被污染（WARN ＋ 快照）、重填之後仍
    然不對（WARN ＋ 快照 ＋ 回 False）。成功重填印
    `refilled OK`。
    """
    label = f"Character {character_index}"
    expected = expected.strip()
    try:
        if find_character_name_element(port, label) is None:
            # 卡片不存在（見 fill_character_prompt 的同款守衛）。
            print(f"  {label} slot not present; verify "
                  + ("satisfied (nothing to check)" if not expected
                     else "skipped (slot missing)"))
            return not expected
        if not ensure_character_expanded(port, label):
            print(f"  WARN: verify {label} — card not expandable; skipping")
            return False
        el = character_card_area(port, label)
        if el is None:
            print(f"  WARN: verify {label} — prompt area not found; skipping")
            return False
        actual = _read_textarea_value(port, el).strip()
        if actual == expected:
            return True
        # 兩種 drift 的意義完全不同，所以嚴重度**照欄位的實際內容**分開：
        #
        # * 讀回空的 ＝ 寫入沒有留在欄位裡。額度對話框關不掉時會整頁 reload，
        #   而 reload 後的重填序列本身就會讓前一個角色欄短暫被打回空白。實測
        #   （`WEBRunner.log` 2026-09-03～09-08）90 次 drift **全部**是這一種、
        #   **全部**重填一次就好（`still mismatched after refill` 0 次、
        #   `char*_drift` 快照 0 張），而且 90 次與 90 次 reload 一對一，沒有第
        #   二個來源。也就是說它是這個站台 reload 之後的**穩態**，不是異常。
        #   而「reload」本身也不是例外路徑：分層關閉（規則 3）上線之後
        #   （09-07 17:17 起）18 次被擋 **0 次關得掉**，38 次角落點選全部失敗，
        #   每一次都落到整頁 reload——所以它是這個站台目前**唯一實際發生**的
        #   路徑，那一窗的 drift 20 次、`still mismatched` 一樣 0 次。兩個時間
        #   窗、同一個結論。
        #   每個正常週期都會響一次的 WARN 等於沒有 WARN——本 repo 已經為同一課
        #   吃過一次虧（`discord_bot.log` 95.8% 的行都是同一句 `rpc apply ->`，
        #   把真正有話說的 4% 淹掉），這裡是它的鏡像：嚇人的事情只喊了一半，
        #   反而把讓人安心的那一半吞掉。所以降級成 note。
        # * 讀回**非空但不同** ＝ 欄位裡有別的內容，那才是 docstring 講的
        #   autocomplete 污染（建議 tag 被插進 prompt 尾端）。實測 0 次，真的
        #   發生時要看得見，所以維持 WARN ＋ 快照。
        #
        # 判準刻意用「欄位裡是什麼」而不是「哪個呼叫端叫的」：呼叫端傳進來的
        # context 旗標會在新增呼叫點時被忘記（症狀是嚴重度靜靜地標錯），而內容
        # 是從頁面本身讀回來的，不會跟現實脫節。降級的只有措辭——偵測、重填、
        # 失敗時的 WARN 與回傳值都不動。
        if actual:
            print(f"  WARN: {label} drifted after fill "
                  f"({len(actual)} vs {len(expected)} chars); refilling")
            snap(port, f"char{character_index}_drift")
        else:
            print(f"  note: {label} was blank after fill "
                  f"(expected {len(expected)} chars); refilling")
        fill_character_prompt(port, character_index, expected)
        human_pause(0.3, 0.6)
        el2 = character_card_area(port, label)
        if el2 is not None:
            if _read_textarea_value(port, el2).strip() == expected:
                # **成功也要出聲。** 舊版在這裡直接 return True，於是記錄停在
                # 「欄位對不上、重填了」就沒有下文，人要去讀原始碼才知道結局，
                # 而那個半截的敘述剛好跟最貴的失效長得一樣。收掉這個迴圈只要
                # 一行；沉默的成功是在要求讀記錄的人去讀程式碼。
                print(f"  {label} refilled OK ({len(expected)} chars)")
                return True
        print(f"  WARN: {label} still mismatched after refill; "
              f"continuing with current value")
        # 快照挪到這裡：真正需要現場畫面的是「重填也救不回來」，不是那個每輪都
        # 發生、每次都自己好的空白。上面非空的那一支仍然各自 snap 過了。
        snap(port, f"char{character_index}_still_mismatched")
        return False
    except Exception as error:  # pylint: disable=broad-except
        # `_long_error`：一個角色批次只跑兩次（char1／char2），不是重複行；而
        # 這裡最想看到的 `element click intercepted` 細節在訊息尾巴，180 字會切掉。
        print(f"  WARN: verify {label} failed: {_long_error(error)}")
        return False


def _click_option_by_text(port, text: str, timeout: float = 5.0) -> bool:
    """Click the visible element whose own text node equals `text`."""
    # 逾時是**間隔** → 單調時鐘（理由詳見 `reject_cookies`）。牆鐘往回跳會把
    # 一次幾秒的 DOM 逾時變成幾小時的停頓——**卡住比失敗更難查**，監督者看到的
    # 是一個還活著卻什麼都不做的行程；往前跳則讓它退化成只試一次。
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        candidates = port.find_elements_xpath(f"//*[normalize-space(text())='{text}']")
        for el in candidates:
            try:
                if not el.is_displayed():
                    continue
                port.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                port.click(el, pause=0.1)
                return True
            except Exception:  # pylint: disable=broad-except
                continue
        time.sleep(0.2)
    return False


def _open_resolution_dropdown(port) -> bool:
    """Click whatever resolution preset is currently selected to open the menu."""
    options = [
        f"{size} {shape}"
        for size in ("Small", "Normal", "Large", "Wallpaper")
        for shape in ("Portrait", "Landscape", "Square")
    ]
    # Find the smallest element whose own text matches a known preset.
    target = port.execute_script(
        """
        const want = arguments[0];
        let best = null, bestLen = Infinity;
        for (const el of document.querySelectorAll('div, span, button')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (want.includes(t)) {
            const len = el.outerHTML.length;
            if (len < bestLen) { best = el; bestLen = len; }
          }
        }
        return best;
        """,
        options,
    )
    if not target:
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
    port.click(target)
    human_pause(0.5, 1.0)
    return True


def _number_input_by_aria(port, aria: str):
    """回傳 aria-label 等於 `aria` 的可見 number input（V5 的 W／H 用）。"""
    return port.execute_script(
        """
        const want = arguments[0];
        for (const el of document.querySelectorAll("input[type='number']")) {
          if (el.offsetParent === null) continue;
          if ((el.getAttribute('aria-label') || '') === want) return el;
        }
        return null;
        """,
        aria,
    )


def _read_number_input_by_aria(port, aria: str):
    el = _number_input_by_aria(port, aria)
    if el is None:
        return None
    try:
        return float(_read_textarea_value(port, el))
    except (TypeError, ValueError):
        return None


def _click_by_aria_label(port, aria: str) -> bool:
    """點 aria-label 等於 `aria` 的可見元素（必要時往上找可點的祖先）。"""
    el = port.execute_script(
        """
        const want = arguments[0];
        for (const el of document.querySelectorAll('[aria-label]')) {
          if (el.offsetParent === null) continue;
          if ((el.getAttribute('aria-label') || '') !== want) continue;
          let node = el;
          for (let i = 0; i < 6 && node; i++) {
            const cs = window.getComputedStyle(node);
            if (node.tagName === 'BUTTON' || node.onclick
                || cs.cursor === 'pointer'
                || node.getAttribute('role') === 'button') return node;
            node = node.parentElement;
          }
          return el.parentElement || el;
        }
        return null;
        """,
        aria,
    )
    if el is None:
        return False
    return robust_click(port, el)


def _select_resolution_v5(port, group: str, item: str) -> bool:
    """V5 的解析度：類別下拉（Normal／Large／Wallpaper／Small／Custom）＋ 兩個
    aria-label 為 `W`／`H` 的數字格。

    **方向（Portrait／Landscape）在 V5 不再是選項**，只是「W 和 H 誰大」。所以
    這裡用「選類別 → 需要時按 Swap width and height」達成，刻意**不**硬記每個
    類別的像素值——那組數字會跟著站方改版變動，記死了會在改版當天靜默產出錯誤
    尺寸；比大小則永遠成立。最後一律讀回 W/H 驗證。
    """
    if not _click_by_aria_label(port, "Select a Resolution Category"):
        print("  [v5] resolution category control not found")
        return False
    human_pause(0.6, 1.0)
    picked = port.execute_script(
        """
        const want = arguments[0];
        let best = null, bestLen = Infinity;
        for (const el of document.querySelectorAll(
               '[role="option"], li, div, span, button')) {
          if (el.offsetParent === null) continue;
          if ((el.innerText || '').trim() !== want) continue;
          const len = el.outerHTML.length;
          if (len < bestLen) { best = el; bestLen = len; }
        }
        return best;
        """,
        group,
    )
    if picked is None:
        print(f"  [v5] resolution category {group!r} not in the list")
        snap(port, "resolution_category_not_found")
        return False
    robust_click(port, picked)
    human_pause(0.8, 1.2)

    width = _read_number_input_by_aria(port, "W")
    height = _read_number_input_by_aria(port, "H")
    if width is None or height is None:
        print("  [v5] W/H inputs not readable")
        snap(port, "resolution_wh_missing")
        return False
    want = item.strip().lower()
    if want == "square":
        ok = width == height
        print(f"  [v5] resolution {width:.0f}x{height:.0f} square -> {ok}")
        return ok
    if ((want == "landscape" and width < height)
            or (want == "portrait" and height < width)):
        if not _click_by_aria_label(port, "Swap width and height"):
            print("  [v5] swap width/height control not found")
            snap(port, "resolution_swap_missing")
            return False
        human_pause(0.6, 1.0)
        width = _read_number_input_by_aria(port, "W")
        height = _read_number_input_by_aria(port, "H")
        if width is None or height is None:
            return False
    ok = ((want == "landscape" and width > height)
          or (want == "portrait" and height > width))
    print(f"  [v5] resolution -> {width:.0f}x{height:.0f} "
          f"({group} {item}) verified={ok}")
    if not ok:
        snap(port, "resolution_orientation_failed")
    return ok


def select_resolution(port, group: str = "Normal", item: str = "Landscape",
                      timeout: float = 10.0) -> bool:
    """把輸出解析度設成 `group` `item`（例：Normal Landscape）。

    兩條路徑：V4.5 是「Normal Landscape」這種單一預設集下拉；**V5 拆成類別下拉
    ＋ W／H 數字格**，方向變成純粹的 W/H 大小關係。先試舊路徑（快、且在 V4.5
    下是原本驗過的行為），失敗才走 V5 路徑。
    """
    if _select_resolution_preset(port, group, item, timeout=timeout):
        return True
    print("  preset-style resolution picker not usable; trying the V5 layout")
    return _select_resolution_v5(port, group, item)


def _select_resolution_preset(port, group: str = "Normal",
                              item: str = "Landscape",
                              timeout: float = 10.0) -> bool:
    """Open the Resolution dropdown and pick the first option matching `item`.
    The dropdown shows entries like "Portrait (832x1216)" / "Landscape (1216x832)";
    items are ordered Normal → Large, so the first match is the Normal preset."""
    if not _open_resolution_dropdown(port):
        # 這在 V5 下是正常情況（沒有預設集下拉），不是錯誤——呼叫端會改走
        # `_select_resolution_v5`。
        print("  no preset-style resolution dropdown on this page")
        return False
    clicked = port.execute_script(
        """
        const item = arguments[0];
        for (const el of document.querySelectorAll('div, li, span, button')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          // Match "Landscape (...)" — the FIRST such option is Normal Landscape.
          if (t.startsWith(item + ' (')) {
            el.scrollIntoView({block:'center'});
            el.click();
            return t;
          }
        }
        return null;
        """,
        item,
    )
    if clicked:
        human_pause(0.4, 0.8)
        print(f"  resolution -> {clicked!r}")
        return True
    snap(port, "resolution_option_not_found")
    return False


def expand_advanced_settings(port) -> bool:
    """點一下 Steps/Guidance/Seed/Sampler 那一列最右邊的圖示。

    **這是一顆切換鈕（toggle），不是「展開鈕」。** 2026-08-22 實測：面板已經展開
    時再點一次會把它**收起來**（`AI Settings` 底下的 Steps 列整個消失）。所以
    **絕對不要無條件呼叫它**——面板的展開狀態會被瀏覽器 profile 記住，起始狀態
    因此是不確定的。要「確保 Rescale 看得到」請呼叫 `ensure_rescale_visible()`，
    那支是以結果為準、可安全重複呼叫的。
    """
    target = port.execute_script(
        """
        // Smallest row whose text contains both Steps & Sampler.
        let row = null, rowLen = Infinity;
        for (const el of document.querySelectorAll('div')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (!/Steps/.test(t) || !/Sampler/.test(t) || t.length > 220) continue;
          if (t.length < rowLen) { row = el; rowLen = t.length; }
        }
        if (!row) return null;
        // Among the row's svg/button children, pick the one with max x (rightmost).
        let best = null, bestX = -Infinity;
        for (const el of row.querySelectorAll('svg, button')) {
          if (el.offsetParent === null) continue;
          const r = el.getBoundingClientRect();
          if (r.width === 0) continue;
          if (r.x > bestX) { bestX = r.x; best = el; }
        }
        return best || row;
        """
    )
    if not target:
        print("advanced settings header not found")
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", target)
    port.click(target)
    human_pause(0.8, 1.4)
    return True


def _has_variety_plus(port) -> bool:
    """頁面上到底有沒有 Variety+ 這個控制項。V5 已移除，見
    `configure_sampler_settings` 裡為什麼「沒有」不等於「失敗」。"""
    return bool(port.execute_script(
        """
        for (const el of document.querySelectorAll('div, span, label')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t.length > 40) continue;
          if (t === 'Variety+' || t.indexOf('Variety+') === 0) return true;
        }
        return false;
        """
    ))


def set_variety_plus(port, enable: bool = True) -> bool:
    """Toggle the Variety+ switch in the advanced settings."""
    return port.execute_script(
        """
        const want = arguments[0];
        for (const el of document.querySelectorAll('div, span, label')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t !== 'Variety+' && !t.startsWith('Variety+')) continue;
          if (t.length > 40) continue;
          // Search around the label for a toggle / button.
          let scope = el;
          for (let i = 0; i < 5 && scope; i++) {
            const inp = scope.querySelector(
              "input[type='checkbox'], [role='switch'], button[aria-pressed], button[aria-checked]"
            );
            if (inp && inp.offsetParent !== null) {
              const isOn = inp.getAttribute('aria-pressed') === 'true'
                        || inp.getAttribute('aria-checked') === 'true'
                        || (inp.tagName === 'INPUT' && inp.checked);
              if (isOn !== want) inp.click();
              return true;
            }
            const btn = scope.querySelector('button');
            if (btn && btn.offsetParent !== null) {
              btn.click();
              return true;
            }
            scope = scope.parentElement;
          }
        }
        return false;
        """,
        enable,
    )


def set_numeric_setting(port, label: str, value) -> bool:
    """Find a setting input by its label text and set its numeric value (React-safe)."""
    return port.execute_script(
        """
        const wantLabel = arguments[0];
        const wantVal = String(arguments[1]);

        const labelEls = [];
        for (const el of document.querySelectorAll('label, div, span')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t === wantLabel || t.split('\\n')[0].trim() === wantLabel) {
            labelEls.push(el);
          }
        }
        for (const lab of labelEls) {
          let scope = lab;
          for (let i = 0; i < 5 && scope; i++) {
            const inp = scope.querySelector("input[type='number']");
            if (inp && inp.offsetParent !== null) {
              const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
              ).set;
              // First focus and clear (some controls only accept values when focused).
              inp.focus();
              setter.call(inp, '');
              inp.dispatchEvent(new Event('input', {bubbles:true}));
              setter.call(inp, wantVal);
              inp.dispatchEvent(new Event('input', {bubbles:true}));
              inp.dispatchEvent(new Event('change', {bubbles:true}));
              inp.dispatchEvent(new Event('blur', {bubbles:true}));
              return true;
            }
            scope = scope.parentElement;
          }
        }
        return false;
        """,
        label,
        value,
    )


def read_numeric_setting(port, label: str):
    return port.execute_script(
        """
        const wantLabel = arguments[0];
        for (const lab of document.querySelectorAll('label, div, span')) {
          if (lab.offsetParent === null) continue;
          const t = (lab.innerText || '').trim();
          if (t === wantLabel || t.split('\\n')[0].trim() === wantLabel) {
            let scope = lab;
            for (let i = 0; i < 5 && scope; i++) {
              const inp = scope.querySelector("input[type='number']");
              if (inp && inp.offsetParent !== null) return parseFloat(inp.value);
              scope = scope.parentElement;
            }
          }
        }
        return null;
        """,
        label,
    )


def first_present_numeric_label(port, labels):
    """回傳這個版面上**真的存在**的第一個候選標籤；一個都沒有就回 None。

    純唯讀——只讀值，一個字都不寫。判準與 `set_numeric_setting` **完全同構**
    （同一組 `label, div, span`、同樣「往上找五層拿 `input[type='number']`」），
    所以「探得到」等價於「set 找得到控制項」，不是另外一套猜法。

    為什麼需要它：候選清單裡帶冒號與不帶冒號的兩種寫法**各自對應一個站方版面**，
    不是「新舊寫法、其中一個已經死了」。實測（2026-08-30，正式 log 共 11 次
    setup）：

    * V5 版面的標籤是 `Steps` / `Prompt Guidance` / `Prompt Guidance Rescale`；
    * V4.5 版面的是 `Steps:` / `Prompt Guidance:` / `Prompt Guidance Rescale:`。

    候選清單把帶冒號的排前面，於是跑 V5 的那 10 次、每個數值設定都先讓
    `Steps:` 白跑兩輪「寫入＋等待＋驗證」才輪到 `Steps`，三個設定合計約 12 秒；
    唯一一次 V4.5 則是 `Steps:` 一發命中。**固定順序必定有一邊踩空**——把順序
    對調只是把成本換給另一個版面。問頁面才是對的做法。

    探不到（全部回 None）不算錯：呼叫端照樣把整份候選清單依序試過去，行為與
    沒有這個函式時一模一樣，最壞情況只是白花幾次純 JS 讀取。
    """
    for label in labels:
        if read_numeric_setting(port, label) is not None:
            return label
    return None


def set_numeric_setting_verified(port, label, value, retries: int = 4) -> bool:
    for attempt in range(retries):
        ok = set_numeric_setting(port, label, value)
        human_pause(0.6, 1.0)
        actual = read_numeric_setting(port, label)
        if actual is not None and abs(actual - float(value)) < 0.001:
            print(f"setting {label}={value} verified (actual {actual}) on attempt {attempt + 1}")
            return True
        # 標籤要印出來。呼叫端會依序試好幾個候選標籤，不說是哪一個的話，
        # 這行字在 log 裡就是孤兒——實機 log 裡連著兩行「失敗」接一行「成功」，
        # 看起來像同一個標籤時好時壞，其實是兩個不同的候選。
        print(f"  [{label}] attempt {attempt + 1}: set returned {ok}, "
              f"actual={actual}")
        human_pause(0.5, 1.0)
    return False


def dump_advanced_labels(port) -> None:
    labels = port.execute_script(
        """
        const out = [];
        for (const lab of document.querySelectorAll('label, div, span')) {
          if (lab.offsetParent === null) continue;
          const t = (lab.innerText || '').trim();
          if (t.length === 0 || t.length > 80) continue;
          if (t.split('\\n').length > 1) continue;
          if (/Prompt|Guidance|Step|Sampler|Seed|Rescale|Decrisper|SMEA|CFG|Variety|Noise/i.test(t)) {
            out.push(t);
          }
        }
        return Array.from(new Set(out));
        """
    )
    print(f"  visible setting labels: {labels}")
    rescale = port.execute_script(
        """
        const out = [];
        for (const el of document.querySelectorAll('div, span, label')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (/Rescale/i.test(t) && t.length < 200) {
            out.push(t.slice(0, 100));
          }
        }
        return out.slice(0, 10);
        """
    )
    print(f"  Rescale matches: {rescale}")


def _has_rescale(port) -> bool:
    return port.execute_script(
        """
        for (const e of document.querySelectorAll('div, span, label')) {
          if (e.offsetParent === null) continue;
          const t = (e.innerText || '').trim();
          if (t.startsWith('Prompt Guidance Rescale')) return true;
        }
        return false;
        """
    )


def _find_visible_text_element(port, text: str):
    """回傳可見、**innerText 完全等於** `text` 的最小元素；找不到回 None。

    跟 `_click_option_by_text` 的差別：那支用 XPath 的 `text()`，只比對**直接文字
    節點**，元素若把文字包在子節點裡就抓不到。這支比對 innerText，並取
    `outerHTML` 最短者以避開外層 wrapper。
    """
    return port.execute_script(
        """
        const want = arguments[0];
        let best = null, bestLen = Infinity;
        for (const el of document.querySelectorAll(
               'div, span, label, button, [role="button"]')) {
          if (el.offsetParent === null) continue;
          if ((el.innerText || '').trim() !== want) continue;
          const len = el.outerHTML.length;
          if (len < bestLen) { best = el; bestLen = len; }
        }
        return best;
        """,
        text,
    )


def ensure_rescale_visible(port, max_attempts: int = 6) -> bool:
    """確保 Prompt Guidance Rescale 欄位看得到，回傳是否成功。

    **以結果為準，不數點選次數**——這是本函式唯一安全的寫法。相關的兩個控制項
    都是**切換鈕**：`expand_advanced_settings()` 的圖示鈕、以及「Advanced
    Settings」那一列。面板的展開狀態被瀏覽器 profile 記住，所以每次進頁的起始
    狀態都不一定，任何「先無條件點一下」的作法都會在一半的起始狀態下**把面板關
    掉**，接著兩個目標都消失、整個 setup 失敗（2026-08-22 就是這樣壞的：
    `configure_sampler_settings` 進門先無條件 toggle 一次）。

    每一輪先看目標在不在，不在才動作，且兩種展開路徑都試：
      1. 看得到「Advanced Settings」→ 點它（Rescale 就藏在它底下）；
      2. 看不到 → 面板可能是收合的緊湊列，點 `expand_advanced_settings()` 的圖示
         把 AI Settings 面板打開，下一輪自然會走到 (1)。
    """
    for attempt in range(1, max_attempts + 1):
        if _has_rescale(port):
            if attempt > 1:
                print(f"  Rescale visible after {attempt - 1} step(s)")
            return True
        advanced = _find_visible_text_element(port, "Advanced Settings")
        if advanced is not None:
            robust_click(port, advanced)
            human_pause(0.9, 1.4)
            continue
        if expand_advanced_settings(port) is False:
            print(f"  neither Rescale nor the Advanced Settings row is "
                  f"reachable (attempt {attempt})")
            break
        human_pause(0.9, 1.4)
    ok = _has_rescale(port)
    if not ok:
        snap(port, "rescale_not_visible")
    return ok


# 取樣器目標值。跟 Steps／Guidance 一樣寫死在共用模組——兩個 webrunner 變體共用
# 同一組生成參數，只有模型字面是 per-variant 常數（因為它偶爾要跟著站方改版）。
TARGET_SAMPLER = "Euler Ancestral"

# 取樣器下拉的已知字面。用途是「認出目前選中的是哪一個」：下拉的觸發元素本身就
# 顯示當前值，站方沒給穩定的 aria-label 可抓，只能反過來用值去認元素。站方新增
# 取樣器時在這裡補字串即可，邏輯不動。
_KNOWN_SAMPLERS = (
    "Euler Ancestral", "Euler", "DPM++ 2M SDE", "DPM++ 2M", "DPM++ SDE",
    "DPM++ 2S Ancestral", "DPM2 Ancestral", "DPM2", "DPM Fast", "DDIM V3",
    "DDIM", "k_euler_ancestral", "k_euler", "k_dpmpp_2m_sde", "k_dpmpp_2m",
    "k_dpmpp_sde", "k_dpmpp_2s_ancestral", "k_dpm_2", "k_dpm_fast", "ddim_v3",
)


def read_current_sampler(port):
    """回傳目前顯示的取樣器字面；認不出來回 None（不是錯誤，見 `select_sampler`）。"""
    return port.execute_script(
        """
        const known = arguments[0];
        const pick = (root) => {
          let best = null, bestLen = Infinity;
          for (const el of root.querySelectorAll('div, span, button')) {
            if (el.offsetParent === null) continue;
            const t = (el.innerText || '').trim();
            if (known.indexOf(t) === -1) continue;
            const len = el.outerHTML.length;
            if (len < bestLen) { best = t; bestLen = len; }
          }
          return best;
        };
        for (const lab of document.querySelectorAll('label, div, span')) {
          if (lab.offsetParent === null) continue;
          const t = (lab.innerText || '').trim();
          if (t !== 'Sampler' && t !== 'Sampler:') continue;
          let scope = lab;
          for (let i = 0; i < 5 && scope; i++) {
            const found = pick(scope);
            if (found) return found;
            scope = scope.parentElement;
          }
        }
        return pick(document.body);
        """,
        list(_KNOWN_SAMPLERS),
    )


def _find_sampler_trigger(port):
    """找出「點下去會展開取樣器下拉」的元素。

    三段式，愈後面愈寬鬆：(1) `Sampler` 標籤附近、顯示已知取樣器字面的最小元素；
    (2) 全頁最小的已知取樣器字面元素（標籤被站方改名時的退路）；(3) `Sampler`
    標籤同層裡第一個可點元素（目前值不在 `_KNOWN_SAMPLERS` 裡時的最後手段）。"""
    return port.execute_script(
        """
        const known = arguments[0];
        const labels = [];
        for (const el of document.querySelectorAll('label, div, span')) {
          if (el.offsetParent === null) continue;
          const t = (el.innerText || '').trim();
          if (t === 'Sampler' || t === 'Sampler:') labels.push(el);
        }
        const pick = (root) => {
          let best = null, bestLen = Infinity;
          for (const el of root.querySelectorAll('div, span, button')) {
            if (el.offsetParent === null) continue;
            const t = (el.innerText || '').trim();
            if (known.indexOf(t) === -1) continue;
            const len = el.outerHTML.length;
            if (len < bestLen) { best = el; bestLen = len; }
          }
          return best;
        };
        for (const lab of labels) {
          let scope = lab;
          for (let i = 0; i < 5 && scope; i++) {
            const found = pick(scope);
            if (found) return found;
            scope = scope.parentElement;
          }
        }
        const anywhere = pick(document.body);
        if (anywhere) return anywhere;
        for (const lab of labels) {
          let scope = lab.parentElement;
          for (let i = 0; i < 4 && scope; i++) {
            const btn = scope.querySelector(
              "button, [role='button'], [role='combobox'], select"
            );
            if (btn && btn.offsetParent !== null) return btn;
            scope = scope.parentElement;
          }
        }
        return null;
        """,
        list(_KNOWN_SAMPLERS),
    )


def _dump_sampler_options(port) -> None:
    """診斷用：把當下看得到的取樣器字面印進 log。與 `_dump_model_options` 同理，
    站方改字面時這是唯一能直接看出該把 `TARGET_SAMPLER` 改成什麼的證據。"""
    try:
        names = port.execute_script(
            """
            const out = [];
            for (const el of document.querySelectorAll('[role="option"], li, [role="listitem"], div, span, button')) {
              if (el.offsetParent === null) continue;
              const t = (el.innerText || '').split('\\n')[0].trim();
              if (!t || t.length > 40) continue;
              if (!/euler|dpm|ddim|ancestral|heun|lms|karras|sde|native/i.test(t)) continue;
              out.push(t);
            }
            return Array.from(new Set(out)).slice(0, 30);
            """
        )
    except Exception as error:  # pylint: disable=broad-except
        print(f"  [warn] could not dump sampler options: {type(error).__name__}")
        return
    print(f"  visible sampler options: {names}")


def _find_sampler_option(port, target: str):
    """回傳下拉裡文字**完全等於** `target` 的選項元素；找不到回 None。

    兩個刻意的選擇：

    * 比對**整段 innerText 完全相等**，不是「第一行相等」。V5 把選單分成
      `RECOMMENDED` / `OTHER` 兩組，組容器的 innerText 是
      `'OTHER\\nEuler\\nDPM++ 2S Ancestral\\n…'`——用第一行比對雖然濾得掉組
      容器，但濾不掉「剛好只包一個選項」的外層 wrapper，而點 wrapper 是無效的。
    * 優先 `[role="option"]`，再退到一般元素；同一輪裡取 `outerHTML` **最短**
      的，因為最短者最貼近選項本體，外層 wrapper 一定比它長。
    """
    return port.execute_script(
        """
        const want = arguments[0];
        const pref = Array.from(document.querySelectorAll('[role="option"]'));
        const rest = Array.from(
          document.querySelectorAll('li, [role="listitem"], div, span, button'));
        for (const pool of [pref, rest]) {
          let best = null, bestLen = Infinity;
          for (const el of pool) {
            if (el.offsetParent === null) continue;
            if ((el.innerText || '').trim() !== want) continue;
            const len = el.outerHTML.length;
            if (len < bestLen) { best = el; bestLen = len; }
          }
          if (best) return best;
        }
        return null;
        """,
        target,
    )


def select_sampler(port, target: str = TARGET_SAMPLER,
                   timeout: float = 10.0) -> bool:
    """把取樣器切到 `target` 並**讀回驗證**，驗過才回 True。

    兩個細節值得記住：

    1. 比對是**完全相等**，不是 `startsWith`／`includes`——選單裡
       `Euler Ancestral` 與 `Euler` 是兩個不同的取樣器，名字卻是前綴關係，任何
       寬鬆比對都會在這兩者之間選錯。而且選錯**照樣產得出圖**、只是取樣不對，
       從結果幾乎看不出來，所以寧可比對失敗（會 dump 選項 ＋ snap）也不放寬。
    2. 已經是目標值時直接回 True，不去點開下拉：下拉觸發元素顯示的就是當前值，
       點開之後再點同一個字面等於把選單關掉，白跑一趟還可能誤觸別的控制項。
       目前的 `TARGET_SAMPLER`（`Euler Ancestral`）**剛好就是網頁自己的預設**，
       所以正常情況下每次 setup 都會走這條短路、log 印
       `sampler already 'Euler Ancestral'`。**這不代表切換邏輯沒在運作**——
       `.chrome_profile/` 會記住使用者上次選的值，站方也可能改預設，那時就會走
       完整的點選 ＋ 讀回驗證路徑。

    找不到選項／驗證失敗都會 `_dump_sampler_options()` ＋ `snap()` 留證據。
    """
    current = read_current_sampler(port)
    if current == target:
        print(f"  sampler already {target!r}")
        return True
    trigger = _find_sampler_trigger(port)
    if not trigger:
        print("  could not locate the sampler dropdown")
        _dump_sampler_options(port)
        snap(port, "no_sampler_selector")
        return False
    port.execute_script("arguments[0].scrollIntoView({block:'center'});", trigger)
    port.click(trigger)
    human_pause(0.6, 1.1)
    # 逾時是**間隔** → 單調時鐘（理由詳見 `reject_cookies`）。牆鐘往回跳會把
    # 一次幾秒的 DOM 逾時變成幾小時的停頓——**卡住比失敗更難查**，監督者看到的
    # 是一個還活著卻什麼都不做的行程；往前跳則讓它退化成只試一次。
    end = time.monotonic() + timeout
    option = None
    while time.monotonic() < end:
        option = _find_sampler_option(port, target)
        if option is not None:
            break
        time.sleep(0.3)
    if option is None:
        print(f"  sampler option {target!r} not found")
        _dump_sampler_options(port)
        snap(port, "sampler_option_not_found")
        return False
    # 真實滑鼠點選優先。**不要**用 JS 的 `el.click()`：2026-08-21 實測，那樣做
    # 選單保持開啟、取樣器完全沒變，因為這個下拉是 combobox 元件（頁面上有
    # `aria-label="Select a sampler"` 的 input），它聽的是 mousedown 而不是
    # click。JS click 只送 click，元件收不到 → 靜默無效。
    robust_click(port, option)
    human_pause(0.6, 1.0)
    if read_current_sampler(port) != target:
        # 真實點選沒生效（元件可能只聽 pointerdown）：補完整事件序列再試一次。
        dispatch_mouse_sequence(port, option)
        human_pause(0.6, 1.0)
    actual = read_current_sampler(port)
    if actual == target:
        print(f"  sampler -> {actual!r} verified")
        return True
    print(f"  sampler verify failed: wanted {target!r}, got {actual!r}")
    _dump_sampler_options(port)
    snap(port, "sampler_verify_failed")
    return False


def configure_sampler_settings(port) -> bool:
    # 直接交給 `ensure_rescale_visible()`：它以結果為準、兩種展開路徑都試、可安全
    # 重複呼叫。**不要**在這裡先無條件 `expand_advanced_settings()`——那是切換鈕，
    # 遇到「面板本來就開著」的起始狀態會反而把它收起來（2026-08-22 的實際故障）。
    all_ok = with_retry("ensure_rescale_visible",
                        lambda: ensure_rescale_visible(port),
                        max_attempts=3, sleep_range=(1, 2))
    dump_advanced_labels(port)
    # 先切取樣器、再設數值。順序是有意的：換取樣器時站方有可能把 Steps／Guidance
    # 重設成該取樣器的預設值，反過來做就會把剛設好、剛驗過的值默默洗掉。
    # 失敗與數值設定同級（一起併進 `all_ok`，會讓 `_setup_session` 整個回 False），
    # 不是可有可無的裝飾——跑錯取樣器產出的圖看起來「差不多」，最難察覺。
    sampler_ok = select_sampler(port, TARGET_SAMPLER)
    print(f"setting Sampler={TARGET_SAMPLER} final -> {sampler_ok}")
    all_ok = sampler_ok and all_ok
    human_pause(0.5, 1.0)
    # 每一組都是「同一個設定在不同站方版面下的標籤寫法」。原本這裡寫的是
    # 「collapsed 與 expanded 檢視的標籤不同」——**那個說法是錯的**，正式 log
    # 打臉了它：冒號的有無跟面板展開與否無關，跟**模型版面**有關。
    #     V4.5：'Steps:'、'Prompt Guidance:'、'Prompt Guidance Rescale:'
    #     V5  ：'Steps'、 'Prompt Guidance'、 'Prompt Guidance Rescale'
    # （2026-08-30 從 11 次 setup 的 `visible setting labels:` 直接讀出來的。）
    # 兩種寫法都還活著，所以一個都不能刪；順序則交給
    # `first_present_numeric_label` 當場問頁面，不要在這裡排死。
    setting_targets = [
        (("Steps:", "Steps"), 23),
        (("Prompt Guidance:", "Prompt Guidance", "Guidance:", "Guidance"), 6),
        (("Prompt Guidance Rescale:", "Prompt Guidance Rescale",
          "Guidance Rescale:", "Guidance Rescale", "Rescale:", "Rescale"), 0),
    ]
    for labels, value in setting_targets:
        # 先問頁面「哪一個候選真的存在」，再開始寫。候選清單混了兩個站方版面的
        # 標籤寫法，固定順序必定有一邊每次都白跑——理由與實測見
        # `first_present_numeric_label`。探不到就照原順序全部試過去。
        ordered = list(labels)
        present = first_present_numeric_label(port, labels)
        if present is not None and present != ordered[0]:
            print(f"  這個版面的標籤是 {present!r}（不是 {ordered[0]!r}）；"
                  f"跳過不存在的候選")
            ordered.remove(present)
            ordered.insert(0, present)
        ok = False
        used = None
        for label in ordered:
            if set_numeric_setting_verified(port, label, value, retries=2):
                ok, used = True, label
                break
        # 報**真正成功的那個候選**，不是第一個候選。原本印 `labels[0]`，於是
        # 正式 log 每次啟動都寫「setting Steps:=23 final -> True」——而 `Steps:`
        # 其實每次都失敗，成功的是 `Steps`。以結果為準的判定配上以嘗試為準的
        # 敘述，看 log 的人會得到相反的結論。失敗時把候選全列出來。
        print(f"setting {used or ' / '.join(labels)}={value} final -> {ok}")
        all_ok = ok and all_ok
        human_pause(0.5, 1.0)
    # Variety+：V4.5 有這個開關，**V5 已經整個移除**（2026-08-21 實機確認：
    # 展開 Advanced Settings 之後 DOM 裡完全找不到這個標籤）。控制項不存在時
    # 絕不能當成失敗——`configure_sampler_settings` 回 False 會讓
    # `_setup_session` 整個回 False，監督者就會無限重生瀏覽器。
    # 「存在但切不動」才算真失敗。
    if _has_variety_plus(port):
        variety_ok = set_variety_plus(port, enable=True)
        print("Variety+ -> ON" if variety_ok
              else "WARN: could not toggle Variety+")
    else:
        variety_ok = True
        print("Variety+ 控制項不存在（V5 已移除）；略過")
    return variety_ok and all_ok


def find_generate_button(port):
    """Locate the Generate button. Returns None on chromedriver transport
    stall (symmetric with `get_main_image_src` / `download_image`) — caller
    `click_generate` already polls for ≤15s and `generate_one_image`
    retries 4× on top, so a transient hiccup costs a retry instead of
    crashing the whole webrunner mid-batch (see `port.TRANSPORT_ERRORS`
    doc)."""
    try:
        return port.execute_script(
            """
            const btns = Array.from(document.querySelectorAll('button'));
            for (const b of btns) {
              if (b.offsetParent === null) continue;
              const t = (b.innerText || '').trim();
              if (/^Generate( \\d+ Image)?$/i.test(t)) return b;
            }
            for (const b of btns) {
              if (b.offsetParent === null) continue;
              if ((b.innerText || '').includes('Generate')) return b;
            }
            return null;
            """
        )
    except port.TRANSPORT_ERRORS as error:
        # 卡頓 → 記錄後照舊回 sentinel；session 已死 → raise BrowserGoneError。
        _note_transport_error("find_generate_button", error)
        return None


def get_main_image_src(port) -> str | None:
    """Return the src of the most recently generated image.

    NovelAI renders newly generated images via blob: / data: URLs. The example
    gallery on the right uses CDN URLs (https://...), so we ignore those.

    Returns None on chromedriver transport stalls (default 120s HTTP timeout
    to chromedriver) — bubbling the raw urllib3.ReadTimeoutError up would
    crash the whole webrunner mid-batch. The caller (`wait_for_new_image`)
    already handles None by polling again; `generate_one_image` retries 4×
    on top of that, then `generate_loop` moves on with `consecutive_fails`.
    """
    try:
        return port.execute_script(
            """
            const imgs = Array.from(document.querySelectorAll('img'));
            let best = null, bestArea = 0;
            for (const img of imgs) {
              if (img.offsetParent === null) continue;
              if (!img.src) continue;
              // Only count freshly-generated images (blob/data URLs).
              if (!/^(blob:|data:)/.test(img.src)) continue;
              const w = img.naturalWidth || img.offsetWidth;
              const h = img.naturalHeight || img.offsetHeight;
              if (w < 200 || h < 200) continue;
              const area = w * h;
              if (area > bestArea) { best = img; bestArea = area; }
            }
            return best ? best.src : null;
            """
        )
    except port.TRANSPORT_ERRORS as error:
        # 卡頓 → 記錄後照舊回 sentinel；session 已死 → raise BrowserGoneError。
        _note_transport_error("get_main_image_src", error)
        return None


def get_generation_error(port) -> dict | None:
    """Return a visible NovelAI generation error/toast, if one exists."""
    try:
        return port.execute_script(
            # 吐司是**容器**，所以用嚴的那一支 `onScreen`（吐司幾乎都是
            # position:fixed，而 `offsetParent` 對 fixed 一律回 null）。
            #
            # ⚠️ **判準直接串接 `_JS_VISIBLE`，不要在這裡再抄一份。** 內嵌 JS
            # 一樣接得到模組層常數——同檔另有五個常數就是這樣串的，`+` 一個字串
            # 在函式裡跟在模組層完全一樣。而抄一份的代價不只是「兩邊要記得一起
            # 改」：JS 的函式宣告會提升、後宣告的勝出，所以一個同名的
            # `function visible` 會**覆蓋掉常數自己的版本**，連 `onScreen` 內部
            # 呼叫到的都會變成抄本。
            _JS_VISIBLE + r"""
            const selectors = [
              '[role="alert"]', '[aria-live="assertive"]',
              '[aria-live="polite"]', '[class*="toast"]',
              '[class*="Toast"]', '[class*="notification"]',
              '[class*="Notification"]'
            ];
            const nodes = Array.from(document.querySelectorAll(
              selectors.join(',')));
            window.__jeGenerationErrorRecords ||= new WeakMap();
            window.__jeGenerationErrorNextId ||= 1;
            const errorPattern = /(?:failed to generate|generation failed|error generating|an error occurred|server error|request failed|unable to generate|try again)/i;
            for (const node of nodes) {
              if (!onScreen(node)) continue;
              const text = (node.innerText || node.textContent || '')
                .replace(/\s+/g, ' ').trim();
              if (text && errorPattern.test(text)) {
                let record = window.__jeGenerationErrorRecords.get(node);
                if (!record) {
                  record = {
                    id: window.__jeGenerationErrorNextId++,
                    version: 0
                  };
                  const observer = new MutationObserver(() => {
                    record.version += 1;
                  });
                  observer.observe(node, {
                    attributes: true,
                    childList: true,
                    characterData: true,
                    subtree: true
                  });
                  window.__jeGenerationErrorRecords.set(node, record);
                }
                return {
                  id: record.id,
                  version: record.version,
                  text: text.slice(0, 300)
                };
              }
            }
            return null;
            """
        )
    except port.TRANSPORT_ERRORS as error:
        # 卡頓 → 記錄後照舊回 sentinel；session 已死 → raise BrowserGoneError。
        _note_transport_error("generation-error check", error)
        return None


def wait_for_new_image(port, previous_src: str | None,
                       timeout: float = 120.0, *,
                       baseline_error: dict | None = None,
                       seen_srcs: Container[str] = ()) -> str | None:
    """等到主圖區出現一張「還沒存過」的圖。

    `seen_srcs` 是這一輪已經接受過的 src。**沒有它這個函式會把站方自己
    換回來的舊圖當成新圖**：判準只有「跟 `previous_src` 不一樣」，而
    `get_main_image_src` 挑的是「面積最大的可見 blob:/data: 圖」——面積用的是
    `naturalWidth`，而縮圖的 naturalWidth 跟原圖一樣大，所以歷史區的縮圖跟主
    圖同分；DOM 順序一變，挑中的就換成另一張舊圖。舊圖的 blob: URL 在同一份文
    件裡是活的、而且跟當初存下來時一模一樣，所以「存過的 src 一律不算新圖」
    剛好擋得住這條路。

    實測而不是推論：2026-08-24～08-27 的 log 裡 402 次生成有 **92 次在 1 秒
    內就「拿到新圖」**——光是下面那段穩定性確認就要 0.6 秒，生成本身要 4-7
    秒，1 秒代表第一次 poll 就命中，也就是根本沒等。其中一次留下了鐵證：
    第 57 張跟第 43 張的檔案 byte 完全相同。

    **這個函式會 raise `GenerationBlockedError`**（不是只回 None）：等待期間
    週期性地探測購買／方案對話框，命中就直接把這條路收掉，見
    `BLOCK_PROBE_INTERVAL_SEC` / `BLOCK_PROBE_GRACE_SEC`。兩個呼叫路徑本來就
    都接得住這個例外——batch 走 `generate_loop` 的額度等待，單圖走
    `serve_single_image_request` 的收工回報。
    """
    # 這兩個截止時刻用**單調**時鐘。本模組還有六個同形狀的逾時迴圈刻意留在牆鐘
    # ，這一個是例外，因為牆鐘往回
    # 跳的後果在這裡跟別處**不同級**：`next_probe` 也是截止時刻，往回跳 Δ 會讓
    # 額度對話框的探測**整整停擺 Δ**，同時 `end` 也不會到——於是被擋住卻沒人在看，
    # 正好把 `BLOCK_PROBE_*` 當初要解決的那個病態（實測 65 次、合計 3 小時 16 分
    # 的空等）原封不動搬回來。其餘那六個往回跳只是「多等 Δ」而已。
    # 這裡也是全模組佔用時間最長的迴圈（每張圖都進來，正式 timeout 180 秒），
    # 時鐘跳動最可能就落在它裡面。
    end = time.monotonic() + timeout
    last = previous_src
    reported: set[str] = set()
    next_probe = time.monotonic() + BLOCK_PROBE_GRACE_SEC
    while time.monotonic() < end:
        cur = get_main_image_src(port)
        if cur and cur != previous_src:
            if cur in seen_srcs:
                # 這條 log 是「站方換回舊圖」唯一的觀測點。沒有它，這個 bug 在
                # log 裡只表現成「這張圖產得特別快」，沒人看得出來——實際上它
                # 安靜地跑了好幾天。同一個 src 只抱怨一次，別把 log 洗版。
                if cur not in reported:
                    reported.add(cur)
                    print("    the site is showing an image we already "
                          "saved; ignoring it and waiting for a new one")
            else:
                # Ensure it stays stable for >0.6s (avoid grabbing mid-load).
                time.sleep(0.6)
                cur2 = get_main_image_src(port)
                if cur2 == cur:
                    return cur
        error_text = get_generation_error(port)
        if error_text and error_text != baseline_error:
            detail = error_text.get("text", "generation failed")
            print(f"    generation failure detected in UI: {detail}")
            # 錯誤吐司本身可能就是「額度用完」。回 None 會讓呼叫端重試，對這種
            # 失敗是白費的，所以先分流：擋住的話 raise，收工不重生。
            _abort_if_generation_blocked(port, "a generation failure toast")
            return None
        # 購買／方案對話框跳出來就代表這張圖不會來了，繼續等只是空轉。理由與
        # 兩個時間常數的實測依據見 `BLOCK_PROBE_INTERVAL_SEC` / `_GRACE_SEC`。
        # raise 出去的 `GenerationBlockedError` 兩個呼叫路徑本來就都接得住：
        # batch 走 `generate_loop` 的額度等待，單圖走 `serve_single_image_
        # request` 的收工回報——這裡只是讓同一個決定早 3 分鐘發生。
        now = time.monotonic()
        if now >= next_probe:
            next_probe = now + BLOCK_PROBE_INTERVAL_SEC
            _abort_if_generation_blocked(port, "waiting for a new image")
        last = cur
        time.sleep(0.5)
    print(f"timed out waiting for new image (last src={last})")
    return None


def download_image(port, src: str, save_path: Path) -> bool:
    """Fetch the image in-page, convert to base64, write to disk.

    Returns False on chromedriver transport stalls — symmetric with
    `get_main_image_src`. `download_image_with_retry` already retries 3×
    on False, so a transient chromedriver hiccup costs ≤ ~7s + retry wait
    instead of crashing the whole webrunner.
    """
    try:
        data_url = port.execute_async_script(
            """
            const src = arguments[0];
            const done = arguments[arguments.length - 1];
            (async () => {
              try {
                const res = await fetch(src);
                const blob = await res.blob();
                const r = new FileReader();
                r.onload = () => done(r.result);
                r.onerror = () => done(null);
                r.readAsDataURL(blob);
              } catch (e) { done(null); }
            })();
            """,
            src,
        )
    except port.TRANSPORT_ERRORS as error:
        # 卡頓 → 記錄後照舊回 sentinel；session 已死 → raise BrowserGoneError。
        _note_transport_error("download_image", error)
        return False
    if not data_url or "," not in data_url:
        print(f"download failed for {src}")
        return False
    _header, b64 = data_url.split(",", 1)
    # base64 解碼 / 寫檔失敗（截斷的 data URL、磁碟滿、檔名被 AV 擋住…）以前
    # 會直接往上炸穿 download_image_with_retry / generate_loop，讓整個 run 以
    # critical_error 收場；改成回 False，交給既有的 3 次下載重試 +
    # consecutive_fail 機制處理（與 transport error 的處理方式對稱）。
    try:
        payload = base64.b64decode(b64)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(payload)
    except (ValueError, OSError) as error:
        print(f"  [warn] download_image decode/write failed: "
              f"{type(error).__name__}: {error}")
        return False
    return True


# `_serving_beat_within_sec` 用這兩個逾時算出單張產圖「服務中」訊號的承諾，所以它們
# 必須是具名常數、而且是 `generate_one_image` 真正在用的那一份——寫回字面值的話，
# 承諾會安靜地跟實際的等待長度分岔。`test_webrunner_shared` 用 AST 釘住這件事。
GENERATE_CLICK_TIMEOUT_SEC = 15.0
GENERATE_WAIT_TIMEOUT_SEC = 180.0


def click_generate(port, timeout: float = GENERATE_CLICK_TIMEOUT_SEC) -> bool:
    """Hot-path click. Wraps the whole body in `port.TRANSPORT_ERRORS`
    because the inner `scrollIntoView` / `click_via_js` calls go through
    chromedriver HTTP — if Chrome is hung, those stall selenium's 120s
    transport timeout and raw-bubble `urllib3.ReadTimeoutError`, which
    would crash the webrunner mid-batch. Return False on stall; caller
    `generate_one_image` already retries 4× with backoff."""
    # 逾時是**間隔** → 單調時鐘（理由詳見 `reject_cookies`）。牆鐘往回跳會把
    # 一次幾秒的 DOM 逾時變成幾小時的停頓——**卡住比失敗更難查**，監督者看到的
    # 是一個還活著卻什麼都不做的行程；往前跳則讓它退化成只試一次。
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        btn = find_generate_button(port)
        if btn:
            try:
                port.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn)
                human_pause(0.2, 0.5)
                try:
                    btn.click()
                except port.TRANSPORT_ERRORS:
                    click_via_js(port, btn)
            except port.TRANSPORT_ERRORS as error:
                # 卡頓 → 回 False 走重試；session 已死 → BrowserGoneError。
                _note_transport_error("click_generate", error)
                return False
            return True
        time.sleep(0.3)
    return False


def generate_one_image(port, previous_src: str | None,
                       *, max_retries: int,
                       retry_delay: tuple[float, float],
                       seen_srcs: Container[str] = (),
                       on_attempt=None) -> str | None:
    """Click Generate and wait for a NEW image. If the image src is identical
    before and after (=generation didn't happen / failed silently), sleep
    `retry_delay` seconds and retry up to `max_retries` times. Both params
    come from `batch_config.json` via `generate_loop`.

    `seen_srcs`（選填）往下傳給 `wait_for_new_image`，見那邊的 docstring。
    單圖路徑不傳，因為它只產一張、沒有「存過的」可言。

    `on_attempt`（選填）在**每一次**嘗試開始時以嘗試序號呼叫一次。只有單張產圖的
    服務路徑會傳（拿來發「服務中」訊號，見 `_emit_serving_beat`）；batch 不傳，行為
    完全不變。放在每一次嘗試的開頭、而不是只在進函式時發一次：一次服務最多跑滿
    `generate_max_retries` 次嘗試（預設 4 次、每次約 225 秒），合計遠超過 bot 原本
    那個 600 秒的 TTL，只在開頭發一次的話，中間就是十幾分鐘的沉默。"""
    for attempt in range(1, max_retries + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        print(f"    [generate attempt {attempt}/{max_retries}]")
        baseline_error = get_generation_error(port)
        # 基準要用「按下去的那一刻螢幕上是什麼」，不是「上一張產完時是什麼」。
        # 呼叫端的 `previous_src` 是上一張的結果，中間隔著 20-30 秒的圖間等待
        # 和一次 DOM 請求輪詢；額度回補那條路上還隔著一次 `port.refresh()`，
        # 而 blob: URL 是綁定文件的，reload 之後舊的那條就作廢了——拿它當基準
        # 等於宣告「畫面上任何東西都算新圖」。讀不到就退回呼叫端給的值：傳輸
        # 卡頓時 `get_main_image_src` 回 None，用 None 當基準才是真的危險。
        displayed = get_main_image_src(port)
        baseline_src = displayed or previous_src
        if not click_generate(port):
            # 「找不到 Generate 鈕」最常見的原因就是整個視窗已經沒了。先探一次
            # （一次 JS round-trip），確認的話立刻收工——否則 je 變體（wrapper
            # 吞掉例外、一律回 None）會把 4 次重試連同其間的 25-30s 等待整個
            # 燒完，才輪到 generate_loop 的計數器慢慢爬。
            _abort_if_browser_gone(port, "locating the Generate button")
            # 第二個常見原因：購買／方案對話框整個蓋住頁面，鈕還在但點不到。
            _abort_if_generation_blocked(port, "locating the Generate button")
            print(f"    [generate retry {attempt}/{max_retries}] button not found")
            if attempt < max_retries:
                time.sleep(random.uniform(*retry_delay))
            continue
        new_src = wait_for_new_image(
            port, baseline_src, timeout=GENERATE_WAIT_TIMEOUT_SEC,
            baseline_error=baseline_error, seen_srcs=seen_srcs)
        if new_src and new_src != previous_src:
            return new_src
        # 等不到新圖有兩種可能：站方這次沒產出（值得重試），或視窗已經關掉
        # （重試毫無意義）。探一次把兩者分開。
        _abort_if_browser_gone(port, "waiting for a new image")
        _abort_if_generation_blocked(port, "waiting for a new image")
        if attempt < max_retries:
            delay = random.uniform(*retry_delay)
            print(f"    [generate retry {attempt}/{max_retries}] failed; "
                  f"sleeping {delay:.1f}s before next attempt")
            time.sleep(delay)
        else:
            print(f"    [generate retry {attempt}/{max_retries}] failed; "
                  "no attempts remaining")
    return None


def _file_digest(path: Path) -> str | None:
    """存下來那張圖的內容雜湊，讀不到就回 None。

    這是**內容層**的防線，跟 `seen_srcs` 的 **URL 層**防線互補：URL 層擋的是
    「站方把舊圖的 blob URL 再端出來一次」，但擋不到「站方替同一張舊圖重新
    造了一條 blob URL」。內容一比就沒得躲。

    純診斷用途，所以讀檔失敗一律吞掉——不能為了一個旁路把產圖弄掛。
    """
    try:
        return hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
    except OSError:
        return None


def download_image_with_retry(port, src: str, save_path: Path,
                              *, max_retries: int) -> bool:
    for attempt in range(1, max_retries + 1):
        if download_image(port, src, save_path):
            return True
        print(f"    [download retry {attempt}/{max_retries}] failed")
        if attempt < max_retries:
            time.sleep(random.uniform(1.0, 2.5))
    return False


# ---------- single-image (one-shot) serve path (P6 C4 — port-based) ----------
# In-band (a batch is mid-flight) OR idle one-shot (startup): the bot writes
# single_image_request.json; this serves exactly ONE image and emits exactly
# ONE `single_image_done` event per request_id. Lifted verbatim (logic-
# identical) from both variants; all DOM goes through the injected `port`.


# 單張產圖「服務中」訊號（`single_image_serving`）承諾裡的餘裕。一次嘗試除了按鈕與
# 等圖兩個逾時之外，還有幾次 JS 探測（錯誤吐司、存活探針、購買框偵測），健康時合計
# 不到十秒。chromedriver 卡頓不在保證範圍內，見 `_serving_beat_within_sec`。
_SERVING_BEAT_MARGIN_SEC = 60.0


def _serving_beat_within_sec(batch_cfg: dict) -> float:
    """「下一則服務中訊號最晚多久會到」的承諾，單位秒。

    ＝ 按 Generate 的逾時 ＋ 等新圖的逾時 ＋ **目前生效的**重試間隔上限 ＋ 餘裕。
    兩則訊號之間最長的一段就是一次完整的嘗試（`generate_one_image` 在每一次嘗試的
    開頭發一則），所以承諾就從這幾個量算。預設設定下是 285 秒。

    **由這一側算、隨事件送過去，bot 不要自己抄一份。** 前兩個是本模組的常數、第三
    個是 `batch_config.json` 的值；bot 那側若自己寫一個「服務最多幾秒」，那就是一份
    會安靜分岔的抄本——有人調高重試間隔，bot 就開始取消正在被服務的請求，症狀正是這
    個訊號要消滅的那一種（圖產出來、沒有人收）。同 `quota_wait` 事件帶
    `next_retry_sec` 的理由：把**當時生效的值**留在事件串流裡。

    只保證健康的路徑。chromedriver 卡頓時單次 JS 呼叫最久 120 秒，疊幾次就能超過
    承諾，bot 可能提早放棄一筆最後其實會完成的請求。那是已經生病的路徑；承諾若要
    蓋住它，健康時「服務者死了」的偵測就得多等好幾倍。
    """
    delay_hi = max(float(v) for v in batch_cfg["generate_retry_delay_sec"])
    return (GENERATE_CLICK_TIMEOUT_SEC + GENERATE_WAIT_TIMEOUT_SEC + delay_hi
            + _SERVING_BEAT_MARGIN_SEC)


def _emit_serving_beat(request_id: str, in_band: bool, phase: str,
                       batch_cfg: dict) -> None:
    """發一則 `single_image_serving`：「這一筆正在被服務，而且服務者還活著」。

    bot 靠它分辨「正在服務、只是慢」與「服務者已經死了」。在這之前，背景程式從請求
    寫進磁碟到 `single_image_done` 之間什麼都不發，bot 只量得到「距離送出多久」，
    而那個量對帶內服務是錯的。

    時機（每一則都在同一筆的 `single_image_done` **之前**，之後絕不再發）：
    `start`＝過了兩道驗證、還沒碰瀏覽器；`generate`＝每一次 Generate 嘗試開始；
    `download`＝開始下載。驗證沒過的請求（空 prompt、不安全的 request_id）不發。

    **遙測絕不可以打斷批次，也絕不可以讓一張圖失敗。** `emit_event` 自己已經吞掉
    寫檔與序列化的錯；這裡再包一層 broad except，是因為這一支在服務的 `try` **裡面**
    被呼叫、還會經由 `on_attempt` 在 `generate_one_image` 裡面被呼叫——從這裡逸出的
    任何東西都會被 `serve_single_image_request` 的 broad except 接住，一次好好的服務
    就變成 `ok=false`。訊號寫不出去的代價是「bot 可能提早放棄」，拿一張圖去換不划算。
    承諾的計算也放在 `try` 裡，理由相同。
    """
    try:
        emit_event("single_image_serving", request_id=request_id,
                   in_band=bool(in_band), phase=phase,
                   beat_within_sec=_serving_beat_within_sec(batch_cfg))
    except Exception as error:  # pylint: disable=broad-except
        # `!r` 刻意保留：`try` 裡只有 `emit_event` 與設定檔算術，碰不到 driver。
        print(f"single_image_serving({phase}) failed; serve continues: "
              f"{error!r}", file=sys.stderr)


def serve_single_image_request(port, req: dict, in_band: bool = False) -> None:
    """產出「單張任意 prompt」的圖並 emit `single_image_done`。

    - 主 prompt 取自 req["prompt"]（必填）；char1 / char2 / undesired 為選填。
    - 角色框處理依 `in_band` 分兩路：
      * **idle one-shot**（in_band=False；bot 在 idle 時 spawn、serve 完 main()
        直接 return 0，後面不再跑 batch）：把「多出來」的角色框從尾端刪光，再把
        **殘存的角色框清成空字串**（不依請求的 char1 / char2 值保留任何內容）。
        NovelAI 強制至少保留 1 個角色框（最後一框沒有刪除鈕、刪不掉），所以
        「刪到 0」做不到；正確收尾是「刪到最小 + 清空殘存框」，讓生成時沒有任何
        帶內容的角色。完全不填角色內容；殘留的 batch 角色特徵不會滲進這張圖。
      * **in-band**（in_band=True；batch 進行中插一張即時生成）：絕不刪框 —
        serve 完 generate_loop 會用 _refill_character_fields 重填「當前 batch
        角色」，而那需要那些角色卡片仍存在：`fill_character_prompt` 是先定位
        卡片再寫，卡片不在就回 False（並印一行），重填等於沒發生。
        （2026-09-05 更正：原本寫「要 areas[1]」，那是改掉之前的 index 取法。）
        刪框會讓 refill 靜默失敗、batch 角色剩餘的圖以缺框產生。故 in-band
        沿用既有「清空空欄位 + verify 空字串」行為（框留著、refill 仍可用）。
    - in-band 路徑產圖前對 char1 / char2 都跑 verify_character_prompt（框都留著，
      連「清空」case 也驗，expected="" 即清空確認），保證上一個 batch 角色不會
      因一次靜默失敗的清空而被產進這張單圖。idle 路徑因為框已全刪，不驗任何角色。
    - 只產 1 張，存進 output/_oneshot/<request_id>/。
    - **絕不**碰 resume checkpoint（webrunner_progress.json）— 那是 batch 角色
      續產用的，one-shot 不該污染它。
    - 整段包在 try/except：任何失敗都 emit `ok=false` 帶簡短 error，呼叫端仍會
      刪掉請求檔，確保「每個 request_id 剛好一個 event、不重複觸發」。
    """
    request_id = str(req.get("request_id") or "")
    prompt = req.get("prompt") or ""
    char1 = req.get("char1") or ""
    char2 = req.get("char2") or ""
    undesired = req.get("undesired") or ""
    try:
        if not prompt.strip():
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="empty prompt")
            print("serve_single_image_request: empty prompt; aborting")
            return
        if not _is_safe_folder_component(request_id):
            # `request_id` 下面**直接當成輸出資料夾名**（`SINGLE_IMAGE_OUTPUT_ROOT
            # / request_id`），而它來自磁碟上的請求檔、讀取端從來沒驗過。實測
            # `C:\Windows\Temp\x` 會**整個取代 base**（`output/_oneshot` 消失）、
            # `../../..` 走得出 repo，而緊接著就是 `mkdir(parents=True)` 加上把
            # 下載的圖寫進去。今天安全靠的是唯一的寫入者只產十六進位——那是
            # **寫入端**的性質，不是這裡宣告出來的，中間還隔著一個跨行程的 JSON
            # 檔。同 `cmd_fav_show` 的 docstring：讀取端要自己講。
            #
            # 判成失敗、而不是退回 `unknown/`：路徑形狀的 id 必然不在 bot 的
            # correlation map 裡（那份的鍵是 `_generate_request_id()` 產的），
            # `_handle_single_image_done` 會直接忽略那則事件，所以那張圖不會有人
            # 收到；而額度是這條產線的瓶頸。產一張注定沒人拿的圖是純損失。
            #
            # 排在 prompt 檢查**之後**是刻意的：請求檔壞掉時 `req` 是 `{}`，
            # `request_id` 與 `prompt` 同時為空，讓它繼續報 `empty prompt`，既有
            # 的診斷語意與測試都不動。
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="invalid request id")
            print(f"serve_single_image_request: unsafe request_id "
                  f"{request_id!r}; aborting", file=sys.stderr)
            return
        # 設定在這裡就讀：「服務中」訊號要帶一個用**當時生效的**重試間隔算出來的
        # 承諾，而第一則在碰瀏覽器之前就要發。`load_batch_config` 永不拋。
        batch_cfg = load_batch_config()
        # 「開始服務」：排在兩道驗證早退之後（驗證沒過就不會被服務，不該宣稱在服務），
        # 排在第一次寫欄位之前（bot 要知道的是「有人開始處理這一筆了」）。
        _emit_serving_beat(request_id, in_band, "start", batch_cfg)
        print(f"serve_single_image_request: request_id={request_id!r} "
              f"in_band={in_band} prompt={prompt[:40]!r} char1={bool(char1)} "
              f"char2={bool(char2)} undesired={bool(undesired)}")
        # 主 prompt。
        if not with_retry("oneshot_fill_main_prompt",
                          lambda: fill_main_prompt(port, prompt),
                          max_attempts=3, sleep_range=(2, 4)):
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="main prompt replacement failed")
            return
        human_pause(0.8, 1.5)
        # 角色框：處理依 in_band 分流（見 docstring）。
        # in-band：刪框會破壞 batch refill，所以一律「填」（空值=清空），框留著。
        # idle one-shot：稍後刪掉多餘框、再把殘存框清成空字串、不填任何角色內容，
        # 故這裡完全不碰角色框（連有值的也不填，因為馬上就要刪光 + 清空）。
        if in_band:
            if not with_retry("oneshot_fill_char1",
                              lambda: fill_character_prompt(port, 1, char1),
                              max_attempts=3, sleep_range=(2, 4)):
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character 1 replacement failed")
                return
            human_pause(0.6, 1.0)
            if not with_retry("oneshot_fill_char2",
                              lambda: fill_character_prompt(port, 2, char2),
                              max_attempts=3, sleep_range=(2, 4)):
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character 2 replacement failed")
                return
            human_pause(0.6, 1.0)
        # undesired：有值就填、空就清空 textarea。找不到欄位回 False 只 WARN。
        if not with_retry("oneshot_fill_undesired",
                          lambda: fill_main_undesired(port, undesired),
                          max_attempts=3, sleep_range=(2, 4)):
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="undesired replacement failed")
            return
        human_pause(0.8, 1.5)
        if not in_band:
            # idle one-shot：把「多出來」的角色框從尾端刪光。NovelAI 強制至少保留 1 個角色框
            # （最後一框沒有 trash 按鈕、刪不掉，見 remove_all_character_slots / remove_character_slot），
            # 所以刪完通常仍剩 1 框、且該框可能殘留上一個 batch 的內容。光刪框不夠，必須再把
            # 「殘存的角色框」清成空字串，否則殘留特徵會滲進這張單圖（實測症狀：只有 Character 2
            # 被刪、Character 1 帶舊值）。
            remove_all_character_slots(port)
            human_pause(0.4, 0.8)
            # 清空殘存的角色框。不依賴「Character N」標題仍在（只剩 1 框時 NovelAI 可能不顯示
            # 標題、count_characters 讀到 0 但 textarea 仍在），改以 find_prompt_areas 為準：
            # index 0 是主 prompt，1.. 是角色框。逐框清成 ""（best-effort、不 raise）。
            clear_ok = True
            for area in find_prompt_areas(port)[1:]:
                try:
                    clear_ok = fill_textarea_like(port, area, "") and clear_ok
                    human_pause(0.2, 0.4)
                except Exception as clear_err:  # pylint: disable=broad-except
                    clear_ok = False
                    # `_short_error`：這一行在「逐個角色框清空」的迴圈裡，
                    # 一次請求可能印好幾行。
                    print("  WARN: idle one-shot clear character area failed: "
                          f"{_short_error(clear_err)}")
            # 內容導向 verify：0 個角色框，或殘存的每個角色框 strip 後都為空。
            # （count==0 是錯的成功判準 — 最後一框本來就刪不掉。）
            residual = [a for a in find_prompt_areas(port)[1:]
                        if _read_textarea_value(port, a).strip()]
            if residual or not clear_ok:
                print(f"  WARN: idle one-shot still has {len(residual)} non-empty "
                      f"character area(s) after trim+clear")
                snap(port, f"oneshot_char_not_cleared_{request_id[:24]}")
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character area clear failed")
                return
        # 產圖前驗角色值。in-band：兩個都驗（連清空 case，框都在）。
        # idle one-shot：殘存框已在上面清空 + 內容導向 verify，這裡不再逐角色驗。
        if in_band:
            if not with_retry(
                    "oneshot_verify_char1",
                    lambda: verify_character_prompt(port, 1, char1),
                    max_attempts=3, sleep_range=(2, 4)):
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character 1 verification failed")
                return
            human_pause(0.3, 0.6)
            if not with_retry(
                    "oneshot_verify_char2",
                    lambda: verify_character_prompt(port, 2, char2),
                    max_attempts=3, sleep_range=(2, 4)):
                emit_event("single_image_done", request_id=request_id, ok=False,
                           error="character 2 verification failed")
                return
        snap(port, f"oneshot_ready_{request_id[:24]}")
        # 生成 1 張。`batch_cfg` 在上面發「開始服務」訊號之前就讀好了。
        gen_max = batch_cfg["generate_max_retries"]
        gen_delay = batch_cfg["generate_retry_delay_sec"]
        dl_max = batch_cfg["download_max_retries"]
        # `or "unknown"` 已由構造死掉——上面的 `_is_safe_folder_component` 對空字串
        # 回 False，走不到這裡。拿掉它不只是清理死碼：`ROOT / (x or "y")` 的右運算元
        # 是 `ast.BoolOp`，而接合守門的站點判準是 `ast.Name`，所以那個寫法**連一列
        # 都不會產生**——不需要例外，也沒有任何地方記錄它沒被看過。改成裸名字之後
        # 這個站點才進得了帳。
        out_dir = SINGLE_IMAGE_OUTPUT_ROOT / request_id
        out_dir.mkdir(parents=True, exist_ok=True)
        previous_src = get_main_image_src(port)
        # 每一次 Generate 嘗試開始時各發一則「服務中」：一次服務可以跑滿
        # `generate_max_retries` 次嘗試（預設約 15 分鐘），只靠開頭那一則撐不住。
        new_src = generate_one_image(
            port, previous_src, max_retries=gen_max, retry_delay=gen_delay,
            on_attempt=lambda _attempt: _emit_serving_beat(
                request_id, in_band, "generate", batch_cfg))
        if not new_src:
            snap(port, f"oneshot_generate_fail_{request_id[:24]}")
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="generation failed (no new image)")
            print("serve_single_image_request: generation failed")
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        save_path = out_dir / f"oneshot_{ts}.png"
        _emit_serving_beat(request_id, in_band, "download", batch_cfg)
        if not download_image_with_retry(port, new_src, save_path,
                                         max_retries=dl_max):
            snap(port, f"oneshot_download_fail_{request_id[:24]}")
            emit_event("single_image_done", request_id=request_id, ok=False,
                       error="download failed")
            print("serve_single_image_request: download failed")
            return
        rel = _single_image_relative_path(save_path)
        emit_event("single_image_done", request_id=request_id, ok=True,
                   path=rel)
        print(f"serve_single_image_request: done -> {rel}")
    except GenerationBlockedError as error:
        # batch 可以關掉對話框慢慢等額度回補，單圖不行：使用者正在等一則回覆。
        # 但**一定要把對話框關掉**——留著會擋住後面接著跑的 batch。
        print(f"serve_single_image_request: {error}", file=sys.stderr)
        dismiss_blocking_dialog(port)
        emit_event("single_image_done", request_id=request_id, ok=False,
                   error="quota unavailable")
        return
    except BrowserGoneError as error:
        # 瀏覽器整個沒了：先把這筆請求收乾淨（呼叫端一定會刪請求檔，而每個
        # request_id 必須剛好對到一個 single_image_done 事件），再往上傳，讓
        # run_batch 的外層 handler 收工重生——吞掉它會讓後面每一筆請求／每一張
        # batch 圖都對著死瀏覽器空轉。
        emit_event("single_image_done", request_id=request_id, ok=False,
                   error="browser session lost")
        print(f"serve_single_image_request: {error}", file=sys.stderr)
        raise
    except Exception as error:  # pylint: disable=broad-except
        emit_event("single_image_done", request_id=request_id, ok=False,
                   error=str(error)[:200])
        # `_long_error`：一個請求只會走到一次，而且是那個請求的終結性回報。
        print(f"serve_single_image_request failed: {_long_error(error)}",
              file=sys.stderr)


def rest_until(port, wake_ts: float, *, slice_sec: float = 30.0) -> bool:
    """排程休息（`schedule_limit_hours` 到點後的 `rest_hours`）的**切片**睡眠。

    原本這裡是一句 `time.sleep(rest_hours * 3600)`——預設 6 小時完全不醒。實測
    2026-08-28：06:50:54 `character_done` 之後整整六小時沒有任何事件，外面看到
    的跟「卡死」一模一樣。單一 `time.sleep` 同時擋掉三件事：

    1. `wait_if_paused` — 暫停標記最久要等六小時才生效；
    2. `check_dom_request` — DOM 請求同樣要等六小時；
    3. `check_single_image_request` — 更糟，bot 的
       `_SINGLE_IMAGE_PENDING_TTL_SEC` 是 600 秒，所以休息期間送出的單圖請求
       **必定**逾時，一次都不可能成功。

    等額度那條路（`wait_for_quota_recovery`）早就是切片睡 + 每片服務一次，理由
    寫在 `_serve_pending_requests` 的 docstring 裡：睡得比 TTL 久就要插播。休息
    比等額度久六倍，卻反而沒做——照抄同一個做法。

    回傳「這段休息裡有沒有服務過 in-band 單圖請求」。有的話呼叫端必須把
    `prev_*` 重設成 None，強制下一個 pair 重填每個欄位；單圖 serve 會覆寫主
    prompt／角色／undesired，per-pair diff 會以為值沒變而跳過（同 4034 行那個
    呼叫點）。這裡**不需要** `_refill_character_fields`——休息點在兩個角色之間，
    上一個角色已經收工，沒有「當前角色的欄位」要救。

    插播失敗只記 stderr 不往上拋：閒置數小時的 Chrome 偶爾抽風是常態，為了一次
    抽風把整輪 batch 打掉不划算，而休息結束後緊接著就是
    `restart_chrome_every_n_characters` 的重啟（預設 1＝每個角色都重啟），瀏覽器
    本來就會換一份乾淨的。
    """
    served = False
    # 牆鐘目標 → **單調**截止時刻，只換算這一次。休息預設六小時，期間的 NTP 校時、
    # 手動改時間、時區／日光節約調整都會讓「還剩多久」整段偏掉：時鐘往回撥一小時就
    # 多睡一小時，往前撥就少睡一小時，而 `rest_hours` 的語意是**時長**不是「睡到某個
    # 鐘點」（呼叫端算的就是 `time.time() + rest_s`）。`wake_ts` 本身維持牆鐘值——呼叫端
    # 要拿它印「幾點醒來」，也寫進 `schedule_rest` 事件給 `/rate`、`/eta` 用——所以
    # 換算放在這裡，呼叫端不用動。
    deadline = time.monotonic() + max(0.0, wake_ts - time.time())
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        wait_if_paused("schedule rest")
        try:
            check_dom_request(port)
            if check_single_image_request(port):
                served = True
        except Exception as error:  # pylint: disable=broad-except
            # `_short_error`：休息是以 30 秒為一片切出來的，預設 6 小時 ＝ 最多
            # 720 片；瀏覽器在休息期間死掉的話，這一行會印 720 次。
            print(f"  [rest] in-band request failed: {_short_error(error)}",
                  file=sys.stderr)
        # 服務可能花掉不少時間，睡之前重新算一次剩餘，別睡過頭。
        time.sleep(max(0.0, min(slice_sec, deadline - time.monotonic())))
    return served


def check_single_image_request(port, in_band: bool = True) -> bool:
    """Iteration 邊界 poll：看到 SINGLE_IMAGE_REQUEST_FILE 就 serve 1 張、刪檔。

    回 True 表示「這圈確實服務了一個 in-band 單圖請求」，呼叫端（main 迴圈）
    收到 True 後要把 prev_prompt / prev_e2 / prev_undesired 重設為 None，讓
    batch 的下一個 pair 重填自己的所有欄位（否則 per-pair diff 會以為值沒變而
    跳過、用到 one-shot 殘留的 prompt）。處理完不論成功失敗都刪掉請求檔，避免
    下次重複觸發。**不碰 resume checkpoint**。

    `in_band`：是否在執行中的 batch 中插隊服務（預設 True，因為兩個 batch 內的
    呼叫點都是 in-band）。只有 main() 啟動時的 idle one-shot 路徑會傳 False —
    那條 serve 完直接 return 0、後面不跑 batch，可安全把所有角色框整個刪光（見
    serve_single_image_request / remove_all_character_slots）。"""
    if not SINGLE_IMAGE_REQUEST_FILE.exists():
        return False
    req: dict = {}
    try:
        text = SINGLE_IMAGE_REQUEST_FILE.read_text(encoding="utf-8")
        parsed = json.loads(text or "null")
        if isinstance(parsed, dict):
            req = parsed
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        # `UnicodeDecodeError` 與 `json.JSONDecodeError` 都是 `ValueError` 的
        # 子類別、彼此無繼承關係，所以兩個都要寫。這裡**刻意**吞掉（跟佇列讀取
        # 端相反）：req 留空 → serve 拿不到 prompt → 發一則 ok=false 的
        # `single_image_done` → finally 刪掉請求檔。一個壞掉的單圖請求只該讓那
        # 個請求失敗，不該把正在跑的整批角色一起帶走。
        print(f"check_single_image_request: bad request file: {error!r}",
              file=sys.stderr)
    try:
        serve_single_image_request(port, req, in_band=in_band)
    finally:
        try:
            SINGLE_IMAGE_REQUEST_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def _refill_character_fields(port, prompt: str, char1: str, char2: str,
                             undesired: str) -> bool:
    """In-band 單圖 serve 會覆寫主 prompt / 角色 / undesired 欄位；batch 角色
    跑到一半被插隊時，serve 完要把「當前角色」的欄位重填回去，否則這個角色
    剩下的圖會用到 one-shot 的 prompt。每個欄位（包含空字串）都必須重填並
    驗證；任何一步失敗就回 False，讓呼叫端中止，不能沿用殘留值繼續產圖。

    ⚠️ **四個 fill 必須全部做完，才輪到兩個 verify。不要把它們配對交錯。**

    這不是風格偏好，是實測出來的：**填 Character 2 會把 Character 1 清空。**
    2026-09-08 從 `WEBRunner.log` 數（09-03 誠實記錄上線之後的 90 次重填）：

    * Character 1 在 verify 時讀到不符 **90/90**，而且**全部**是 `0 vs N`
      （欄位是空的），從來不是 `N+k vs N`（被插入內容）；
    * Character 2 讀到不符 **0/90**——所以干擾是**單向**的；
    * 逐次對照：char1 在**寫入前**讀到的是正確內容、在 char2 填完之後才變空，
      **90/90**。也就是說清空發生在 char2 那一步，不是頁面重載造成的。

    現在這條路能自我修復，唯一的原因就是 verify 排在**兩個 fill 都做完之後**，
    所以它看得到最後的狀態。把它整理成「填一個、驗一個」——

        fill_char1 → verify_char1 → fill_char2 → verify_char2

    ——是任何人看到這段都會想做的整理（配對更清楚、局部性更好），但那樣
    `verify_char1` 會在 char2 把 char1 清空**之前**就通過，於是 **char1 整批空白，
    而且一行 log 都不會有**：verify 綠、`still mismatched` 不出現、`with_retry`
    也不失敗。後果正是本檔到處在警告的那一個——安靜地產出一整批用錯提示詞的圖，
    而這次連 drift 警告都沒有。`test_webrunner_shared.py` 有一支行為測試釘住它
    （用一個「填 char2 就清空 char1」的假 port 走完整條路，斷言**結束時兩個欄位
    都正確**）。

    **刻意不在 `verify_char2` 之後再驗一次 char1。** 對稱的風險（重填 char1 反過來
    清空 char2）目前有 0/90 的反證，為它多跑一輪等於為沒觀察到的方向付成本，還會讓
    步驟表看起來更沒道理。真的出現 char2 的 drift 記錄時再加，那時也才知道要加幾輪。
    """
    try:
        steps = (
            ("refill_main_prompt", lambda: fill_main_prompt(port, prompt)),
            ("refill_char1", lambda: fill_character_prompt(port, 1, char1)),
            ("refill_char2", lambda: fill_character_prompt(port, 2, char2)),
            ("refill_undesired", lambda: fill_main_undesired(port, undesired)),
            ("refill_verify_char1",
             lambda: verify_character_prompt(port, 1, char1)),
            ("refill_verify_char2",
             lambda: verify_character_prompt(port, 2, char2)),
        )
        for label, action in steps:
            if not with_retry(label, action, max_attempts=3,
                              sleep_range=(2, 4)):
                print(f"  WARN: {label} failed; aborting batch continuation")
                return False
            human_pause(0.4, 0.8)
        return True
    except BrowserGoneError:
        # 回 False 也會讓呼叫端 raise，但錯誤訊息會變成「欄位還原失敗」，把真正
        # 的死因（瀏覽器沒了）埋掉。直接往上傳，保住診斷。
        raise
    except Exception as error:  # pylint: disable=broad-except
        # `_long_error`：一次插播服務／一次額度 reload 才走到一次，不是重複行。
        print(f"  WARN: re-fill after in-band one-shot failed: "
              f"{_long_error(error)}", file=sys.stderr)
        return False


# ---------- per-character generation loop (P6 C5 — port-based) ---------------
# Generates `images_per_character` images for one character, with crash
# fast-path, consecutive-fail alert/abort, in-band single-image serve, and
# resume-checkpoint `update_saved`. `minimize_fn` is an optional zero-arg
# callback injected by each variant (its win32-augmented minimize, which
# stays per-variant — it reads the deferred Chrome-profile globals); None
# skips minimize (used by the no-browser tests).


def generate_loop(port, character_name: str, batch_cfg: dict,
                  batch_start: float, out_dir: Path | None = None,
                  resume_count: int = 0, refill: tuple | None = None,
                  minimize_fn=None,
                  seen_srcs: collections.deque | None = None,
                  seen_digests: set[str] | None = None) -> int:
    count = batch_cfg["images_per_character"]
    inter_delay = batch_cfg["inter_image_delay_sec"]
    gen_max = batch_cfg["generate_max_retries"]
    gen_delay = batch_cfg["generate_retry_delay_sec"]
    dl_max = batch_cfg["download_max_retries"]
    fail_abort = batch_cfg["consecutive_fail_abort"]
    # `out_dir` is supplied by main() when resuming an interrupted character
    # (so we continue the SAME folder); otherwise allocate fresh.
    if out_dir is None:
        out_dir = allocate_output_dir(character_name, batch_start)
    out_dir.mkdir(parents=True, exist_ok=True)
    if out_dir.name != character_name:
        print(f"  using numbered output folder: {out_dir.name}/")
    # 只用來量「這個角色跑了多久」（下面的 `character_done.elapsed_sec`），所以
    # 用**單調**時鐘：一個角色動輒數小時，中間被 NTP 校時／改時區跳一下，回報的
    # 時長就會離譜（往回跳還會變負數）。名字帶 `_mono` 是刻意的——這個值**不可以**
    # 被拿去當絕對時間戳（寫進事件的 `ts=`、跟檔案 mtime 比、跟別的行程對時），
    # monotonic 的零點每個行程都不一樣。要絕對時間點請另外取 `time.time()`。
    loop_start_mono = time.monotonic()
    emit_event("character_start", name=character_name, target=count,
               folder=out_dir.name, resumed=resume_count)
    consecutive_fails = 0
    page_recovery_tried = False
    fail_alert_sent = False
    # Tier 2 的一次性額度：不認得字面的 modal 擋住時，先安全地關掉它再給一次
    # 機會（站方偶爾會跳公告／問卷之類的東西）。同一個角色只放行一次——關掉後
    # 又立刻擋住，就是真的需要人處理，不是隨手能關的東西。
    tier2_dismissed = False
    # Seed `saved` with the images already on disk from a prior interrupted
    # run so the returned total (and the pop gate that compares it to the
    # threshold) reflects the WHOLE folder, not just this run's additions.
    saved = resume_count
    if resume_count > 0:
        _, start_index = _run_progress.folder_image_stats(out_dir)
        print(f"  resuming {character_name} from {resume_count}/{count} "
              f"(next #{start_index:04d}) in {out_dir.name}/")
    else:
        start_index = 1
    remaining = max(0, count - resume_count)
    previous_src = get_main_image_src(port)
    # 已經存過的 src。站方偶爾會把主圖區換回一張舊圖（原因見
    # `wait_for_new_image` 的 docstring），這份紀錄是唯一擋得住「把同一張圖再
    # 存一次」的東西。上限 50 是刻意的：要蓋住的只是「站方可能換回來的最近幾
    # 張」，而無上限會讓一個極罕見的巧合（同一條 blob: URL 真的被重新指派）
    # 永遠卡住那一格。
    # 這兩份「看過的東西」**要跨角色共用**——`run_batch` 會把同一份傳進來。
    # 瀏覽器文件在角色與角色之間不會重建（只有週期性的 `port.restart` 記憶體
    # 沖洗才會），所以上一個角色的圖還躺在站方的歷史區裡，一樣可能被挑中。
    # 每個角色各開一份等於每換一個角色就把防線清空一次。
    # 沒傳就自己開一份，單獨呼叫（測試、je 變體）照樣有保護。
    if seen_srcs is None:
        seen_srcs = collections.deque(maxlen=50)
    if seen_digests is None:
        seen_digests = set()
    if previous_src:
        seen_srcs.append(previous_src)
    duplicate_alert_sent = False

    def _serve_pending_requests() -> None:
        """插播 bot 的 DOM／單圖請求，服務完把 batch 欄位填回去。

        兩個呼叫點共用：圖與圖之間（原本就有），以及**等額度的那一小時裡**
        （每個睡眠切片一次）。後者是必要的——bot 的
        `_SINGLE_IMAGE_PENDING_TTL_SEC` 是 600 秒，而這裡預設睡 3600 秒。

        單圖 serve 會覆寫主 prompt／角色／undesired 欄位，所以服務完一定要把
        「當前角色」的欄位重填回去，否則這個角色剩下的圖會用到 one-shot 的
        prompt。額度沒回來的時候 serve 自己會被擋、回報失敗——那對使用者是快
        速而誠實的答案，比在對話平台上乾等十分鐘再被掃成「沒服務」好。
        """
        nonlocal previous_src
        check_dom_request(port)
        if check_single_image_request(port) and refill is not None:
            if not _refill_character_fields(port, *refill):
                raise RuntimeError(
                    "failed to restore batch fields after in-band request")
            previous_src = get_main_image_src(port)
            # 插播那張圖也要記進去，否則它會變成下一張 batch 圖的候選。
            if previous_src:
                seen_srcs.append(previous_src)

    def _restore_fields_after_reload() -> None:
        """額度那條路 reload 過頁面之後，把這個角色的欄位重填回去。

        `refill` 是 `run_batch` 傳進來的 `(prompt, char1, char2, undesired)`；
        沒傳（je 變體 / 測試單獨呼叫）就什麼都不做——沒有可信的來源可填，硬填
        反而更糟。填不回去就中止：帶著不確定的欄位繼續產圖會安靜地產出一整批
        用錯提示詞的圖，比停下來讓監督者重生 ＋ 重跑 setup 糟得多。
        """
        if refill is None:
            return
        print("  [quota] page was reloaded; refilling the character fields")
        if not _refill_character_fields(port, *refill):
            raise RuntimeError(
                f"failed to restore the fields for `{character_name}` after "
                f"the quota reload; aborting instead of generating with "
                f"unknown prompt state")

    for n in range(remaining):
        wait_if_paused(f"{character_name} image")
        i = start_index + n
        done_so_far = resume_count + n + 1
        ts = time.strftime("%Y%m%d_%H%M%S")
        target = out_dir / f"{character_name}_{i:04d}_{ts}.png"
        print(f"[{character_name}] generating {done_so_far}/{count} -> {target.name}")
        # 額度用完（站方跳出購買／方案對話框）不是失敗，是「還沒輪到」：關掉對話
        # 框、等額度回補、重試**同一張**。不計入 consecutive_fails、不結束行程，
        # 所以監督者不會介入，也不會每輪重跑一次登入 ＋ setup。
        # `quota_waited` 跨輪累積，`quota_wait_max_sec` 的上限才有意義；設 0
        # （預設）就是無上限地等下去。
        quota_waited = 0.0
        # 「上一輪等了多久」＝ 回傳值與傳入值的**差**，寫進 `quota_resumed` 的
        # `last_wait_sec`。定義上就正確，所以不必在這裡自己再讀一次
        # `quota_wait_poll_sec`——一份設定讀兩處遲早分歧。
        #
        # 固定間隔下這個值恆等於 `quota_wait_poll_sec`，看起來冗餘，但它是**唯一**
        # 把當時生效的輪詢間隔留在事件串流裡的地方。2026-09-07 要回答「短輪詢會
        # 不會比較快」時，只能靠比對相鄰事件的時間戳去反推 08-25 那次設定變更——
        # 有這個欄位就是直接讀得到。收集成本是零，省下的是下一次的考古。
        quota_last_wait = 0.0
        while True:
            try:
                new_src = generate_one_image(port, previous_src,
                                             max_retries=gen_max,
                                             retry_delay=gen_delay,
                                             seen_srcs=seen_srcs)
                break
            except GenerationBlockedError:
                if quota_waited == 0.0:
                    emit_event("quota_blocked", character=character_name,
                               image_index=i)
                before_wait = quota_waited
                quota_waited = wait_for_quota_recovery(
                    port, batch_cfg,
                    label=f"{character_name} image {i}",
                    waited_sec=quota_waited,
                    on_reload=_restore_fields_after_reload,
                    on_idle=_serve_pending_requests)
                quota_last_wait = quota_waited - before_wait
        # 「回復了」要以**結果**為準。`generate_one_image` 重試燒完是回 None、
        # 不是 raise，所以這裡照樣會 break 出來——原本無條件印「recovered」並發
        # `quota_resumed`，於是使用者在對話平台上收到「額度回復了」的同時，背景
        # 其實正要開始連續放棄十張圖。正式 log 裡 62 次「recovered」有 1 次是假
        # 的，而那一次正是後面那場兩小時空轉的開頭。
        if quota_waited > 0.0 and new_src:
            # `last_wait_sec` 一併記下去：累計值單獨看不出當時的輪詢間隔是多少，
            # 而那正是事後要判斷「這段記錄是哪個設定跑出來的」唯一需要的東西。
            print(f"  [quota] recovered after {quota_waited / 60:.0f} min "
                  f"(poll {quota_last_wait / 60:.0f} min); "
                  f"resuming `{character_name}`")
            emit_event("quota_resumed", character=character_name,
                       image_index=i, waited_sec=round(quota_waited, 1),
                       last_wait_sec=round(quota_last_wait, 1))
        elif quota_waited > 0.0:
            print(f"  [quota] waited {quota_waited / 60:.0f} min but the retry "
                  f"still produced nothing for `{character_name}` image {i}",
                  file=sys.stderr)
        if not new_src:
            print(f"  giving up on image {i} after {gen_max} retries")
            snap(port, f"generate_fail_{i:04d}")
            # Fast-path: a renderer crash ("Aw, Snap!" interstitial)
            # makes every hot-path reader return None silently. Catch it
            # on the FIRST failed image instead of waiting for
            # `consecutive_fail_abort` to tick to 10 (~5 min wasted).
            # Refresh is best-effort — supervisor respawns Chrome
            # anyway, but refresh leaves the about-to-die session in
            # cleaner shape.
            if _is_chrome_crash_page(port):
                print(f"  [chrome crash] 'Aw, Snap!' interstitial "
                      f"detected on image {i}")
                snap(port, f"chrome_crash_{i:04d}")
                _try_chrome_refresh(port)
                raise RuntimeError(
                    f"Chrome renderer crashed (interstitial detected) on "
                    f"image {i}/{count} for `{character_name}`; aborting "
                    f"so supervisor respawns Chrome."
                )
            # Fast-path #2：**這個工作階段從頭到尾沒出現過任何一張圖**就已經
            # 放棄，代表問題不是「這一次沒生成」，而是頁面根本不在能生成的狀
            # 態。實測 2026-08-24（`WEBRunner.log` 760-930 行）：站方把工作階
            # 段收掉之後，40 次嘗試每一次都是
            # `timed out waiting for new image (last src=None)`，連續放棄十張
            # 圖、燒掉 **2 小時 28 分鐘**才由 `consecutive_fail_abort` 收工。
            # 真正把它修好的是監督者重生時那一句
            # `no session - going through /login flow`。
            #
            # 判準用 `previous_src`，它已經在手上——那是「按下產生之前螢幕上那
            # 張圖」。有 blob 值 ＝ 這個工作階段確實產出過圖、app 是活的，這次
            # 失敗屬於偶發，不值得多花成本；是 None ＝ 一張都沒看過。**不要**
            # 改用 `saved`：續跑進來的角色一開始 `saved` 就大於 0，可是頁面是
            # 全新的，判斷會整個反過來。
            #
            # 動作是 reload ＋ 重填，兩者都是現成、每小時被額度那條路走一次的
            # 實作。工作階段還在 → 這只是一次便宜的復原（頁面卡住時真的救得
            # 回來）；工作階段沒了 → 重填必定失敗，
            # `_restore_fields_after_reload` 會 raise，監督者重生並重新登入。
            # 148 分鐘因此縮成一張圖的時間。
            #
            # 一個角色只做一次。做完之後 `previous_src` 仍然是 None，不設旗標
            # 的話接下來每一張失敗的圖都會再 reload 一次。
            # `refill is None`（je 變體 / 單獨測試）時整段跳過：沒有可信來源可
            # 填，reload 只會把欄位清空，比不動更糟。
            if (previous_src is None and not page_recovery_tried
                    and refill is not None):
                page_recovery_tried = True
                print(f"  [recover] no image has appeared at all in this "
                      f"session; reloading the page before spending more "
                      f"attempts on `{character_name}`", file=sys.stderr)
                emit_event("page_recovery", character=character_name,
                           image_index=i)
                _try_chrome_refresh(port)
                _restore_fields_after_reload()
            consecutive_fails += 1
            if (not fail_alert_sent
                    and consecutive_fails >= CONSECUTIVE_FAIL_ALERT):
                emit_event("consecutive_failures",
                           character=character_name,
                           count=consecutive_fails,
                           image_index=i)
                fail_alert_sent = True
            # Hard abort once we've burned through `fail_abort` images in a
            # row. Without this, a dead Chrome / lost chromedriver session
            # lets the loop silently grind through all 240 images with 0
            # saves and exit rc=0 — `start_webrunner.py` / bot supervisor
            # see "clean finish" and never respawn. Raising propagates to
            # `main()`'s outer except, emits `critical_error`, exits non-
            # zero, supervisor respawns Chrome from scratch.
            if consecutive_fails >= fail_abort:
                # Tier 2（不看字面）：連續失敗到門檻、而畫面上還有可見的 modal
                # 擋著 → 需要人處理，不是崩潰。重生只會看到同一個對話框。
                blocking = has_blocking_dialog(port)
                if blocking and not tier2_dismissed:
                    tier2_dismissed = True
                    print(f"  [blocked] unrecognised modal dialog is blocking "
                          f"the page: {blocking!r}", file=sys.stderr)
                    if dismiss_blocking_dialog(port):
                        print("  [blocked] dismissed it; giving the character "
                              "one more chance before stopping")
                        consecutive_fails = 0
                        fail_alert_sent = False
                        continue
                if blocking:
                    print(f"  [blocked] {consecutive_fails} consecutive "
                          f"failures with a modal dialog still on screen",
                          file=sys.stderr)
                    print(f"  [blocked] dialog text: {blocking!r}",
                          file=sys.stderr)
                    raise GenerationBlockedError(
                        f"{consecutive_fails} consecutive failures on "
                        f"`{character_name}` with a modal dialog blocking the "
                        f"page; stopping instead of respawning — needs a human."
                    )
                # 這裡**不要**再宣稱「多半是 Chrome 崩潰」。走到這一行的時候，
                # 那些原因已經一一被排除掉了：'Aw, Snap!' 崩潰頁在第一張失敗時
                # 就查過（`_is_chrome_crash_page`）、session 沒了會由
                # `_abort_if_browser_gone` 提早收工、modal 就是上面那個 if 的
                # 條件。把已排除的原因寫成「likely cause」實際誤導過人：
                # 2026-08-24 那次連續放棄十張圖、兩小時的空轉就是頁面狀態問題
                # （額度 reload 把剛填的提示詞清掉），log 卻一路指向 Chrome。
                raise RuntimeError(
                    f"{consecutive_fails} consecutive image failures on "
                    f"`{character_name}` (image {i}/{count}); aborting so the "
                    f"supervisor can respawn Chrome with a fresh session. "
                    f"Already ruled out: the 'Aw, Snap!' crash page, a lost "
                    f"driver session, and a modal dialog on screen — so the "
                    f"page is most likely in a state where Generate silently "
                    f"does nothing (prompt fields lost to a reload is the one "
                    f"we have actually seen)."
                )
            continue
        previous_src = new_src
        seen_srcs.append(new_src)
        if download_image_with_retry(port, new_src, target, max_retries=dl_max):
            saved += 1
            consecutive_fails = 0
            fail_alert_sent = False
            # Keep the on-disk checkpoint's `saved` in lock-step with the
            # folder so a sudden kill resumes from the true count (cheap —
            # one atomic write per inter-image delay). Only on success.
            _run_progress.update_saved(saved)
            # 內容層防線。同一個角色內出現 byte 完全相同的兩張，代表站方端出
            # 了舊結果——這件事在 2026-08-27 之前**完全沒有徵狀**，是靠事後
            # 比對輸出資料夾的雜湊才發現的。log 每次都寫，事件一個角色只發一
            # 次（一輪壞掉可能連續幾十張，別把頻道洗版）。
            digest = _file_digest(target)
            if digest and digest in seen_digests:
                print(f"  [warn] image {i} is byte-identical to one already "
                      f"saved for `{character_name}`; the site served an old "
                      f"result", file=sys.stderr)
                if not duplicate_alert_sent:
                    duplicate_alert_sent = True
                    emit_event("duplicate_image", character=character_name,
                               image_index=i)
                # 偵測到還不夠——重複的那張本來會**留在資料夾裡而且照樣算一
                # 張**，於是「120 張」其實只有 119 張不同的內容，而計數與檔案
                # 都不會說出來。2026-08-27 在正式輸出裡實際抓到一組：同一個角
                # 色的 #0043（06:50:19）與 #0057（06:57:27）完全相同的
                # 1,451,588 bytes，中間隔了 14 張、7 分鐘，src 那一層完全沒
                # 反應（站方替同一張舊圖重新造了一條 blob URL）。
                #
                # 刪掉是**保留資訊**的操作，不是破壞：被刪的是第二份、內容與
                # 留下的第一份逐 byte 相同，證據仍在（log ＋ 事件 ＋ 那張留著
                # 的圖）。
                #
                # 為什麼一定要連 `saved` 一起退回：**資料夾檔數是續跑的權威**
                # （`_run_progress.folder_image_stats`）。只退計數不刪檔，磁碟
                # 上仍是 120 張＝「這個角色做完了」，續跑就不會補；只刪檔不退
                # 計數，檢查點的 `saved` 會比實際多。兩個一起動才對得起來。
                try:
                    target.unlink()
                except OSError as error:  # pragma: no cover
                    # `!r` 刻意保留：只收得到 `OSError`，`str()` 會印出那張圖的
                    # 完整輸出路徑。
                    print(f"  [warn] could not remove the duplicate image: "
                          f"{error!r}", file=sys.stderr)
                else:
                    saved -= 1
                    _run_progress.update_saved(saved)
            elif digest:
                seen_digests.add(digest)
        else:
            snap(port, f"download_failed_{i:04d}")
            # 下載走的是 in-page fetch；視窗沒了它也只會回 False。與生成路徑
            # 對稱地探一次，別讓 fail 計數器慢慢爬到 abort。
            _abort_if_browser_gone(port, f"downloading image {i}")
            consecutive_fails += 1
            if (not fail_alert_sent
                    and consecutive_fails >= CONSECUTIVE_FAIL_ALERT):
                emit_event("consecutive_failures",
                           character=character_name,
                           count=consecutive_fails,
                           image_index=i,
                           phase="download")
                fail_alert_sent = True
            if consecutive_fails >= fail_abort:
                raise RuntimeError(
                    f"{consecutive_fails} consecutive image download "
                    f"failures on `{character_name}` (image {i}/{count}); "
                    "aborting so supervisor can respawn the browser."
                )
        if done_so_far < count:
            delay = random.uniform(*inter_delay)
            print(f"  sleep {delay:.1f}s before next image")
            time.sleep(delay)
            # 圖跟圖之間 poll DOM／單圖請求 — generate_loop 是長時間 hot
            # path，在這裡查比 main loop top 反應快多了。同一個 closure 也掛在
            # 額度等待的睡眠切片上，見 `_serve_pending_requests`。
            _serve_pending_requests()
            # 把跑到螢幕上的瀏覽器視窗搬回螢幕外（`hide_browser_windows`）。開窗時
            # 就已經在螢幕外，這裡只是保險：已經在外面的視窗不動、不最小化、不搶
            # 焦點，所以重複呼叫便宜。參數名沿用 `minimize_fn` 只是為了不動幾十個
            # 呼叫端；**不要**把最小化加回來——最小化／還原正是會讓剛產出的圖在
            # 前景閃一下的那個動作（2026-09-22）。
            if minimize_fn is not None:
                minimize_fn()
    # 收尾防線：整個角色一張都沒存，而且畫面上還有 modal 擋著。
    # 上面那道 Tier 2 掛在 `consecutive_fail_abort` 門檻上，所以
    # `images_per_character` 比門檻小的角色（或提前跑完的）根本走不到它——
    # 那種情況會安靜地回 saved=0，讓 run_batch 的零產出 backstop 慢慢兜，
    # 診斷也退化成「什麼都沒產出」而不是「有東西擋著」。
    if saved == 0 and remaining > 0:
        blocking = has_blocking_dialog(port)
        if blocking:
            print(f"  [blocked] `{character_name}` saved nothing and a modal "
                  f"dialog is still on screen: {blocking!r}", file=sys.stderr)
            raise GenerationBlockedError(
                f"`{character_name}` produced no images and a modal dialog is "
                f"blocking the page; stopping instead of respawning — needs a "
                f"human.")
    emit_event("character_done",
               name=character_name,
               saved=saved,
               target=count,
               folder=out_dir.name,
               # 送的是**差值**不是時間點：bot 的 `_seconds_per_image` 拿它算
               # `elapsed_sec / saved`，`_handle_event` 拿它 format 成時長，兩邊
               # 都沒有跟 `now` 比對，所以換成 monotonic 語意不變、而且更準。
               elapsed_sec=round(time.monotonic() - loop_start_mono, 1))
    return saved


# ---------- batch orchestration (P6 C6 — port-based) ------------------------
# `run_batch` is the whole webrunner session body: initial setup (per-variant
# `setup_fn` callback — it uses WebDriverWait/login which stay per-variant),
# the startup single-image server, and the dynamic per-character batch loop
# (re-read queues each char via `read_queues`, decide via `_queue_consume`,
# padding-aware pop, `end` sentinel, resume checkpoint). Chrome restarts go
# through `port.restart`; `minimize_fn` threads each variant's win32 minimize
# into generate_loop. The variant `main()` is a thin shell: preflight (via
# `run_preflight`, before Chrome boot) -> boot -> run_batch -> finally quit+sync.


def read_queues():
    real_p = read_todo_characters(TODO_PROMPT_FILE)
    eff_p, fb_p = list(real_p), False
    if not real_p:
        fb_text = read_text_safe(PROMPT_FILE)
        if fb_text:
            eff_p, fb_p = [fb_text], True
    real_1 = read_todo_characters(TODO_FILE_1)
    eff_1, fb_1 = list(real_1), False
    if not real_1:
        fb_text = read_text_safe(CHARACTER1_FALLBACK_FILE)
        if fb_text:
            eff_1, fb_1 = [fb_text], True
    # Character 2 is positional. A blank row explicitly means remove its UI
    # card for this pair; it must not be filtered out or replaced by fallback.
    real_2 = read_todo_characters(TODO_FILE_2, preserve_blank=True)
    eff_2, fb_2 = list(real_2), False
    if not real_2:
        fb_text = read_text_safe(CHARACTER2_FALLBACK_FILE)
        if fb_text:
            eff_2, fb_2 = [fb_text], True
    # 空 todo_undesired 沿用 undesired.md 的整段內容；undesired.md 也空就
    # 所有 entries 為空字串（fill_main_undesired 仍會把當前 textarea 清掉）。
    real_u = read_todo_characters(TODO_UNDESIRED_FILE)
    eff_u, fb_u = list(real_u), False
    if not real_u:
        fb_text = read_text_safe(UNDESIRED_FILE)
        if fb_text:
            eff_u, fb_u = [fb_text], True
    return ((real_p, real_1, real_2, real_u),
            (eff_p, eff_1, eff_2, eff_u),
            (fb_p, fb_1, fb_2, fb_u))


def parse_run_mode(argv) -> str:
    """從 argv 認出這一輪的模式（為什麼要宣告，見 `RUN_MODE_BATCH` 上面那段事故
    紀錄）。回 `RUN_MODE_SINGLE_IMAGE_SERVER` 或 `RUN_MODE_BATCH`。

    三條判定規則都是刻意的：

    * **只看 `argv[1:]`；`argv[0]` 永遠不算命中。** `argv[0]` 是被執行的腳本
      路徑、不是選項，把它一起掃進來等於多開一個「repo 剛好被放在一個名字等於這個
      旗標的路徑底下」就能翻轉模式的開關——而那是沒有人會想到要去查的地方。順帶
      也讓呼叫端與測試可以安心地餵標準形狀的 `["prog", ...]`。
    * **整個元素相等才算，不做前綴／子字串比對。** 於是 `--single-image-server=1`
      與 `--no-single-image-server` 都**不會**命中。日後要支援那些形狀是一次刻意的
      改動，不是手滑就會發生的事。
    * **重複出現不影響結果；不認得的參數一律忽略、不報錯。** 這支只回答一個是非
      題。要是連「有沒有不認得的參數」也一起扛，日後任何一個還沒接到這裡的新旗標
      都會讓 webrunner 直接死在進入點：非零 rc → 監督者重生 → 同一份 argv → 再死
      一次，把一次無害的參數漂移變成無限重生。
    """
    return (RUN_MODE_SINGLE_IMAGE_SERVER
            if SINGLE_IMAGE_SERVER_FLAG in tuple(argv)[1:]
            else RUN_MODE_BATCH)


def run_preflight(oneshot_pending: bool) -> bool:
    """Read the four queues, print the run preview, and return
    True if there is work (or a pending one-shot). False (caller
    returns rc=1) when nothing to do — checked BEFORE Chrome boot.

    `oneshot_pending` 的來源在 pass 3（2026-09-12）換過：兩支變體現在餵
    `mode == RUN_MODE_SINGLE_IMAGE_SERVER`（**宣告**出來的模式），不再餵
    `SINGLE_IMAGE_REQUEST_FILE.exists()`（**推論**）。差別是實質的：一個沒人
    清掉的舊請求檔以前會讓一輪空佇列的批次白開一次 Chrome，然後掉進啟動分支
    的劫持路徑（事故紀錄見 `RUN_MODE_BATCH` 上面那一段）。

    ⚠️ **參數名不要改。** 三個呼叫端裡有一個用的是關鍵字：repo 根目錄的
    `run_batch.py`（`run_preflight(oneshot_pending=False)`，那支是主控台入口，
    自己永遠不會是單圖伺服器——它只把工作轉交給 `start_webrunner.py`，而那支
    組 argv 時不轉發自己的參數，所以子行程也永遠看不到旗標）。改名會讓它在
    執行期丟 `TypeError`，而那是一個只有人手動跑才會走到的入口。
    """
    real0, eff0, fb0 = read_queues()
    preview_pairs = pair_todos(*eff0)
    if not preview_pairs and not any(real0) and not oneshot_pending:
        print("no entries in todo_prompt / todo_character1 / todo_character2; nothing to do")
        return False
    print(
        f"todo quadruples: {len(preview_pairs)} "
        f"(prompt={len(real0[0])}, char1={len(real0[1])}, char2={len(real0[2])}, "
        f"undesired={len(real0[3])}) [dynamic: re-read each character]"
    )
    for i, (ep, e1, e2, eu) in enumerate(preview_pairs, 1):
        print(
            f"  [{i}] prompt={(ep[:40] + '…') if len(ep) > 40 else ep!r}"
            f" char1={character_folder_name(e1) if e1 else '(none)'!r}"
            f" char2={character_folder_name(e2) if e2 else '(none)'!r}"
            f" undesired={(eu[:30] + '…') if len(eu) > 30 else eu!r}"
        )
    return True


def _serve_single_image_queue(port) -> None:
    """常駐單圖伺服迴圈：把整條單圖佇列服務完，閒置夠久才回來。

    `run_batch` 在 `mode == RUN_MODE_SINGLE_IMAGE_SERVER` 時呼叫這一支（模式由
    argv 宣告，事故紀錄與規則見 `RUN_MODE_BATCH` 上面那一段）。setup 完成後不是
    「serve 1 張就結束」：每服務掉一個請求就重設 idle 時鐘、立刻重 poll，閒置約
    `idle_timeout` 秒沒有新請求才收工。每一次 serve 都是 idle one-shot
    （`in_band=False` ＝ 刪光所有角色框、只留主 prompt），所以永遠不會把上一批的
    角色滲進一次性圖裡。

    ⚠️ **進來的時候請求檔不保證存在，而且那是常態。** bot 是先 spawn 我們、再把
    請求 pump 到磁碟上的，所以第一圈很可能什麼都撿不到——迴圈本來就會等，那個請求
    通常在幾百毫秒內落地。這也正是呼叫端的閘門**不可以**再 `and` 上
    `SINGLE_IMAGE_REQUEST_FILE.exists()` 的原因：那樣寫會讓一個真的單圖伺服器掉進
    批次迴圈去跑整條 todo 佇列。
    """
    # 常駐單圖伺服迴圈的本地常數（不新增 module 常數——這是伺服迴圈專屬的調校值）。
    idle_timeout = 120.0  # 閒置這麼久沒新請求就收工
    print(f"single-image server: serving queue, idle timeout "
          f"{idle_timeout:.0f}s")
    # idle 時鐘參考點。量的是「距離上次服務過了多久」＝間隔，所以走**單調**時鐘：
    # 牆鐘往後跳（NTP 校時、使用者改時鐘）會讓這個伺服器關不掉，往前跳則會在還有
    # 請求排隊時提早收工。
    last_served_mono = time.monotonic()
    while True:
        # check_single_image_request 只在「請求檔存在且確實服務了」時回 True。
        served = check_single_image_request(port, in_band=False)
        if served:
            last_served_mono = time.monotonic()
            continue  # 立即重 poll：可能還有排在後面的請求
        # 這一圈沒東西可服務。
        if time.monotonic() - last_served_mono >= idle_timeout:
            break
        time.sleep(random.uniform(1.0, 2.0))  # 短間隔閒置 poll
    print(f"single-image server: idle {idle_timeout:.0f}s, shutting down")
    # drain：補抓 bot 在我們最後一次 poll 與上面 break 判斷之間 os.replace 寫進來
    # 的請求（只做一次 best-effort，不迴圈）。漏掉它的話那筆請求會一路卡到 bot 自己
    # 的 inflight TTL 才自癒。
    check_single_image_request(port, in_band=False)


def run_batch(port, email, password, *, setup_fn, minimize_fn,
              mode: str = RUN_MODE_BATCH) -> int:
    """Orchestrate one webrunner session: initial setup (via the
    per-variant `setup_fn`), the startup single-image server, and the
    dynamic per-character batch loop (`_queue_consume`), then the
    end-sentinel / zero-save (rc=3) / done post-loop. Chrome restarts
    go through `port.restart`; `minimize_fn` is threaded into
    generate_loop. Returns the process rc; raises on critical error
    (caller's finally still quits + syncs the profile).

    `mode` 宣告這一輪是批次（預設）還是單圖伺服器，來源是
    `parse_run_mode(sys.argv)`，而下面那個啟動分支**只看它**（規則與事故紀錄寫在
    `RUN_MODE_BATCH` 上面那一段）。**不可以再 `and` 上
    `SINGLE_IMAGE_REQUEST_FILE.exists()`**——理由寫在那個分支旁邊，一句話是：真的
    單圖伺服器跑到那裡時檔案可能還沒落地。預設值是 `RUN_MODE_BATCH`，所以沒有
    宣告就是批次。

    不認得的 `mode` **退回批次並大聲印一行**，不丟例外。兩個理由：(a) 這個值只
    可能來自 `parse_run_mode`，而它只回得出那兩個常數，所以第三種值代表呼叫端寫
    錯了；而丟例外的時機是在 Chrome 都開起來之後，那一輪很可能已經超過
    `rapid_fail_threshold_sec`，於是「寫錯一個字串」會變成監督者永遠重生。
    (b) 退回批次是安全的那個方向：批次照樣會在配對邊界 in-band 服務掉待處理的
    單圖請求，所以誤判成批次頂多是多跑了佇列上本來就要跑的東西；反過來
    誤判成伺服器才是上面那段事故紀錄在講的「整條佇列被安靜跳過 ＋ 假的成功 rc」。
    """
    if mode not in (RUN_MODE_BATCH, RUN_MODE_SINGLE_IMAGE_SERVER):
        print(f"unknown run mode {mode!r}; falling back to {RUN_MODE_BATCH!r}",
              file=sys.stderr)
        mode = RUN_MODE_BATCH
    # 印出來是刻意的：那次事故是靠 log 重建的，而 log 裡當時**沒有**任何一行說得出
    # 「這個行程認為自己是誰」，只看得到它做了什麼。
    print(f"webrunner run mode: {mode}")
    print("webrunner shared revision: prompt-strict-retry-v5 "
          f"({Path(__file__).resolve()})")
    # 整段批次期間請求作業系統不要打斷這個行程（**不是**請求系統不要進入待命——
    # 那是兩件事，差別與實測見 `StayAwake` 上方的區塊註解）。放在 `run_batch` 而
    # 不是各變體的 `main()` 裡，是因為這裡是兩個變體共用的那一層——寫在這裡就不會
    # 有「只有一邊有」的漂移。`finally` 一定會跑到；就算沒跑到（行程被硬砍），電源
    # 要求也會隨著行程消失而由 OS 收回，不會留下一個永遠生效的要求。
    _awake = StayAwake()
    if load_batch_config().get("keep_system_awake", True):
        _got = _awake.acquire()
        # 三條分支都把 `active` 的字面值一起印出來，這是刻意的：中文敘述會隨著
        # 理解改變被重寫（這一段 2026-09-20 就被重寫過一次），而 `power-request`
        # ／`execution-state` 這兩個 token 是程式真正的判斷依據，事後 grep log 才
        # 問得出「那一輪到底拿到了哪一個」。
        if _got == "power-request":
            print("  [power] 已取得電源要求（power-request）；系統照樣會進入"
                  "待命，但這個行程不會被暫停")
        elif _got == "execution-state":
            # 這台機器只有 S0ix 的話，這條既擋不住待命、也保不住行程——講清楚比
            # 假裝成功好。
            print("  [power] 只拿到舊版執行狀態旗標（execution-state）；"
                  "Modern Standby 機器上保不住這個行程")
        else:
            print("  [power] 兩種電源要求都拿不到；待命期間這個行程可能會被暫停")
    # 這兩個計數器**必須**在 try 外面：下面的 except 會讀它們來決定「這一輪到底
    # 有沒有產出東西」，而例外可能發生在它們被指派之前（setup 階段）。
    total_saved = 0
    produced = 0
    # 整個瀏覽器工作階段共用一份（理由見 `generate_loop` 裡的註解）。
    session_srcs: collections.deque[str] = collections.deque(maxlen=50)
    session_digests: set[str] = set()
    try:
        # Full setup (login → model → characters → resolution → sampler →
        # minimize). Extracted into `_setup_session` so the mid-run Chrome
        # restart (memory flush) can re-run the exact same sequence.
        if not setup_fn():
            print("session setup failed; aborting")
            return 2

        # 啟動單圖伺服分支。**模式是宣告出來的，不是從磁碟推論的**——推論錯過一
        # 次，事故紀錄在 `RUN_MODE_BATCH` 上面那一段。整條路徑不跑 batch、永不增加
        # `produced`（走不到後面的 rc=3 zero-save backstop；這裡 return 0 在那之
        # 前）、也永不寫 resume checkpoint。
        #
        # ⚠️ **絕對不可以 `and` 上 `SINGLE_IMAGE_REQUEST_FILE.exists()`。** 它看起
        # 來比較保險，實際上是今天這個缺陷的鏡像：bot 是**先 spawn、再把請求 pump
        # 到磁碟上**的，所以一個真的單圖伺服器跑到這裡時，請求檔很可能還沒落地；
        # 那樣寫會讓它掉進下面的批次迴圈去跑整條 todo 佇列，而且一樣安靜。
        if mode == RUN_MODE_SINGLE_IMAGE_SERVER:
            print("run mode declared on argv: single-image server; "
                  "serving the one-shot queue")
            _serve_single_image_queue(port)
            return 0  # 乾淨收工，不讓 supervisor 重生
        if SINGLE_IMAGE_REQUEST_FILE.exists():
            # 批次路徑上看到請求檔**不刪**：它是帶內服務的輸入，批次迴圈頂端與圖
            # 與圖之間的 `check_single_image_request` 會把它撿走。印一行是因為
            # 「批次啟動時磁碟上已經躺著一個請求」正是 2026-06-27 那次事故的現場
            # 特徵——log 裡要看得出這一輪是**刻意**沒有把它當成身分宣告。
            print("batch mode: a single-image request is already on disk; "
                  "it stays there and will be served in-band at the first "
                  "character boundary", file=sys.stderr)

        # Count completed characters so we can cycle Chrome every N of them
        # (memory-flush — see `restart_chrome_every_n_characters`).
        chars_completed = 0
        # 排程計時器：量的是「這一輪已經工作多久」（對 `schedule_limit_hours`），
        # 純粹是間隔 → **單調**時鐘。牆鐘往後跳會讓休息被無限延後（那個休息時段
        # 是刻意的），往前跳會沒必要地提早休息。
        #
        # 對照組就在下面幾行：`iter_batch_start` 必須留在 `time.time()`，因為
        # `allocate_output_dir` 拿它跟**檔案 mtime** 比（`max(mtimes) >=
        # batch_start`），mtime 是牆鐘 epoch。判準是「這個值有沒有要離開本行程」：
        # 要落地到磁碟／事件／跟別的行程比對 → `time.time()`；只在行程內量經過多久
        # → `time.monotonic()`。兩者不可互換，monotonic 的零點每個行程都不同。
        schedule_start_mono = time.monotonic()
        # `batch_start` 是 per-iteration 的：每個 pair 在進 generate_loop 前
        # 才取 `time.time()`。這讓「同樣 char_name 但 prompt / undesired
        # 換了」的 pair 自動 fall through 到 `<name>_2` / `_3` … — 上一個
        # iteration 寫進去的檔 mtime < 本 iteration 的 batch_start，
        # `_folder_belongs_to_batch` 回 False，就會編號。
        prev_prompt: str | None = None
        prev_e2: str | None = None
        prev_undesired: str | None = None
        # 動態消耗的兩個 in-run 計數器（見 _queue_consume）：
        # - produced：本輪已實際產出的角色數，給 chrome 重啟 cadence / log。
        # - skip：本輪「未達門檻、保留在佇列前端、已嘗試過」的條目數（游標）。
        #   每圈取 pairs[skip]，pop 時對游標位置 pop（skip==0 即舊 front-pop）。
        skip = 0
        # `len(pairs)` 在動態模型下每圈不同；postloop 的事件需要一個「本輪走過的
        # pair 數」概念，用最後一圈算出的當前 pairs 數當代表。
        last_pairs_len = len(pair_todos(*read_queues()[1]))
        # Set True when an `end` sentinel in the main prompt queue stops the
        # run early; lets the post-loop code skip the zero-save backstop and
        # exit rc=0 (clean finish, no supervisor respawn).
        end_sentinel_hit = False
        while True:
            wait_if_paused("pair boundary")
            # 每圈重讀四個真實佇列、套 fallback → eff + fb 旗標。
            real, eff, fb = read_queues()
            (real_p, real_1, real_2, real_u) = real
            (eff_p, eff_1, eff_2, eff_u) = eff
            (fb_p, fb_1, fb_2, fb_u) = fb
            decision = _queue_consume.decide(
                real_p, real_1, real_2, real_u,
                eff_p, eff_1, eff_2, eff_u,
                fb_p, fb_1, fb_2, fb_u, skip, produced)
            if decision.action == _queue_consume.ACTION_BREAK:
                break
            pairs = decision.pairs
            last_pairs_len = len(pairs)
            # `fallback_single`：真實佇列全空、但有 fallback 且本輪還沒產過 →
            # 只產這一個就收工（不讓長度 1 的 fallback 把抽乾的真實佇列延長成
            # 多餘幽靈角色）。產完在 pop 區尾段 break。
            fallback_single = (
                decision.action == _queue_consume.ACTION_FALLBACK_SINGLE)
            (prompt_entry, entry1, entry2, undesired_entry) = decision.batch
            # `end` sentinel in the MAIN prompt queue terminates the run here:
            # this pair and every later pair are skipped (no generation). Per
            # the queue contract we consume ONLY the `end` line itself (pop it
            # from todo_prompt) and leave any entries after it for the next
            # run. Matched case-insensitively; read_todo_characters already
            # stripped the line. Skipped when the prompt came from the
            # prompt.md fallback (no queue line to pop).
            if not fb_p and _queue_consume.is_end_marker(prompt_entry):
                print(f"  encountered 'end' sentinel at cursor {skip} "
                      f"({len(pairs)} pair(s) this read); stopping run "
                      f"(later pairs skipped)")
                # 從磁碟重讀的 real_p 移掉第一個 end 行再寫回（real_p 即此刻
                # 磁碟內容，等同先 reconcile）。
                for idx, rp in enumerate(real_p):
                    if _queue_consume.is_end_marker(rp):
                        real_p.pop(idx)
                        write_todo_characters(TODO_PROMPT_FILE, real_p)
                        print(f"  consumed 'end' line; "
                              f"{len(real_p)} prompt entr"
                              f"{'y' if len(real_p) == 1 else 'ies'}"
                              f" remain in {TODO_PROMPT_FILE.name}")
                        break
                end_sentinel_hit = True
                break
            char_name = character_folder_name(entry1) if entry1 else character_folder_name(entry2)
            print(f"\n=== {char_name} ===")
            # 角色邊界的程式碼漂移檢查（純診斷，不改變任何行為）。放在角色橫幅
            # 底下，漂移那一行才會緊貼著它、事後好對；`drifted is False` 時完全
            # 不出聲，所以正常情況下這裡看不到任何東西。共用一份 → 兩個變體同時
            # 生效，不需要各自複製。
            report_code_drift()
            # Iteration-boundary polling 點：bot 端 `!introspect_dom` 寫請求
            # 檔後，這裡 fulfill 並 emit_event('dom_result')。
            check_dom_request(port)
            # In-band 單圖請求（batch 角色之間插一張即時生成）。serve 會覆寫
            # 主 prompt / 角色 / undesired 欄位，所以服務完把 prev_* 重設 None，
            # 強制下面這個 pair 重填自己每個欄位（否則 per-pair diff 會以為值
            # 沒變而跳過、用到 one-shot 殘留值）— 跟 chrome 重啟後的重設同理。
            if check_single_image_request(port):
                prev_prompt = None
                prev_e2 = None
                prev_undesired = None
            # `human_pause(0.8, 1.5)` 放在每個成功 fill 後面，模擬「人類點完
            # 一個欄位、停一下、再點下一個」的節奏；React component 也利用
            # 這段空檔同步 state（避免上一個 fill 的 input event 還沒處理完
            # 就接著被下個 fill 干擾）。
            if prompt_entry != prev_prompt:
                print(f"  filling main prompt ({len(prompt_entry)} chars)")
                if not with_retry(
                        "fill_main_prompt",
                        lambda p=prompt_entry: fill_main_prompt(port, p),
                        max_attempts=3, sleep_range=(2, 4)):
                    print("  skipping - main prompt replacement failed")
                    _abort_if_chrome_crashed(
                        port, f"fill_main_prompt ({char_name})")
                    skip += 1
                    continue
                prev_prompt = prompt_entry
                human_pause(0.8, 1.5)
            # Fill the undesired-content textarea whenever it changed, including
            # the first empty value. Empty is an explicit clear operation; if it
            # cannot be confirmed, skip this pair instead of inheriting stale text.
            if undesired_entry != prev_undesired:
                print(f"  filling undesired ({len(undesired_entry)} chars)")
                ok = with_retry("fill_main_undesired",
                                lambda u=undesired_entry: fill_main_undesired(port, u),
                                max_attempts=3, sleep_range=(2, 4))
                if ok:
                    prev_undesired = undesired_entry
                else:
                    print("  skipping - undesired replacement failed")
                    _abort_if_chrome_crashed(
                        port, f"fill_undesired ({char_name})")
                    skip += 1
                    continue
                human_pause(0.8, 1.5)
            else:
                # 同步 tracker 即可，不寫盤、不動 DOM。
                prev_undesired = undesired_entry
            if not with_retry(
                    "set_character2_state",
                    lambda enabled=bool(entry2): set_character2_enabled(
                        port, enabled),
                    max_attempts=3, sleep_range=(2, 4)):
                print("  skipping - Character 2 UI state could not be updated")
                _abort_if_chrome_crashed(
                    port, f"set_character2_state ({char_name})")
                skip += 1
                continue
            if not entry2:
                prev_e2 = None
            if not with_retry(
                    "fill_char1",
                    lambda e=entry1: fill_character_prompt(port, 1, e),
                    max_attempts=3, sleep_range=(2, 4)):
                print("  skipping — char1 fill failed")
                _abort_if_chrome_crashed(port, f"fill_char1 ({char_name})")
                skip += 1
                continue
            human_pause(0.8, 1.5)
            # Only re-fill character 2 when its prompt actually changed.
            if entry2 and entry2 != prev_e2:
                if not with_retry(
                        "fill_char2",
                        lambda e=entry2: fill_character_prompt(port, 2, e),
                        max_attempts=3, sleep_range=(2, 4)):
                    print("  skipping - char2 replacement failed")
                    _abort_if_chrome_crashed(
                        port, f"fill_char2 ({char_name})")
                    skip += 1
                    continue
                prev_e2 = entry2
                human_pause(0.8, 1.5)
            # fill 當下的 verify 擋不住 fill 之後的污染（autocomplete 下拉
            # 被後續座標點選誤觸、把建議 tag 插進尾端），進 generate_loop
            # 前再各驗一次。entry2 即使這輪沒重填也驗 — 欄位仍可能被上一
            # 輪的點選污染。
            character_prompts_ok = True
            for char_index, expected in ((1, entry1), (2, entry2)):
                if char_index == 2 and not expected:
                    continue
                if not with_retry(
                        f"verify_char{char_index}",
                        lambda i=char_index, e=expected:
                            verify_character_prompt(port, i, e),
                        max_attempts=3, sleep_range=(2, 4)):
                    print(f"  skipping - Character {char_index} prompt "
                          f"could not be verified")
                    _abort_if_chrome_crashed(
                        port, f"verify_char{char_index} ({char_name})")
                    character_prompts_ok = False
                    break
            if not character_prompts_ok:
                skip += 1
                continue
            snap(port, f"ready_{char_name[:30]}")
            # Per-iteration batch_start：每個 pair 各自的「現在」當基準，
            # `allocate_output_dir` 看到 same-named folder 裡有更早的檔
            # 就會編號到 `<name>_2` / `_3` …，prompt 換了就會分開存。
            iter_batch_start = time.time()
            # Hot-reload batch params per character. Edits to batch_config.json
            # made mid-run take effect on the NEXT character (not mid-batch
            # — count / inter_delay must stay stable for the 240-image loop).
            batch_cfg = load_batch_config()
            target_count = batch_cfg["images_per_character"]
            # Resume an interrupted character. Only the FIRST pending pair of
            # this run could have been mid-flight last time (later pairs never
            # started). In the dynamic model that's `produced == 0 and skip == 0`
            # (the very first batch actually generated this run, before any pop
            # or retained-skip). If the saved checkpoint describes exactly this
            # pair (prompt unchanged), continue its existing output folder
            # instead of regenerating from image 1. Any queue edit → no match.
            resume_dir: Path | None = None
            resume_count = 0
            if produced == 0 and skip == 0:
                prog = _run_progress.read_progress()
                # `folder` 必須另外驗——`matches()` 只比對四個身分欄位，根本沒
                # 看它。以前這裡直接 `OUTPUT_ROOT / prog["folder"]`，六種壞法
                # （欄位缺、None、int、list、`../`、絕對路徑）全部中招；細節與
                # 實測結果寫在 `_run_progress.resume_folder` 的 docstring。
                cand = _run_progress.resume_folder(prog, OUTPUT_ROOT)
                # 這一串 elif 的每一條都要留下「為什麼沒接續」。之前只有身分
                # 欄位不符那一條會講話，其餘全是無聲 fallthrough——2026-08-23
                # 那次 `priestess` 連開四個編號資料夾、燒掉一個半小時、最後連
                # `character_done` 都沒有，事後在 log 與 events 裡找不到半個字。
                if prog is None:
                    print(f"  resume: no checkpoint on disk; starting "
                          f"`{char_name}` from image 1")
                elif not _run_progress.matches(prog, prompt_entry, entry1,
                                               entry2, undesired_entry):
                    # Checkpoint exists but the first pair changed — DIAGNOSTIC.
                    # Don't resume (a different pair into the same folder would
                    # mix prompts), but log exactly WHICH field diverged so the
                    # real cause (queue edit / fallback flip / whitespace) is
                    # visible in the user's webrunner.log.
                    diffs = _run_progress.diagnose_mismatch(
                        prog, prompt_entry, entry1, entry2, undesired_entry)
                    print(f"  resume: checkpoint for {prog.get('folder')!r} "
                          f"does NOT match first pair `{char_name}`; starting "
                          f"fresh. Diverging fields:")
                    for line in diffs:
                        print(f"    {line}")
                    # 上面那幾行只活在 stdout。主控台會被關掉、記錄檔會被修剪，
                    # 但「這一輪為什麼沒接續」是事後唯一想知道的事，所以再發一筆
                    # 事件——events.ndjson 跨行程存活，bot 也讀得到。
                    # 只送**欄位名稱**不送內容：欄位內容是提示詞全文，沒必要
                    # 進事件檔或被轉貼出去；人看的 stored/current 留在上面的 log。
                    emit_event(
                        "resume_mismatch", name=char_name,
                        folder=prog.get("folder"),
                        fields=_run_progress.mismatch_fields(
                            prog, prompt_entry, entry1, entry2,
                            undesired_entry))
                elif cand is None:
                    # 身分欄位對得上、`folder` 卻不能用＝檢查點被改壞或被別的
                    # 東西覆寫過。從第 1 張重來是唯一安全的選擇，但這不是正常
                    # 情況，走 stderr 並發事件。
                    print(f"  resume: checkpoint matches `{char_name}` but its "
                          f"folder value is unusable "
                          f"({prog.get('folder')!r}); starting from image 1",
                          file=sys.stderr)
                    emit_event("resume_unusable", name=char_name,
                               reason="bad_folder")
                else:
                    existing, _ = _run_progress.folder_image_stats(cand)
                    # 磁碟檔數是權威，但 checkpoint 的 saved 是備援：剛被硬殺後
                    # 資料夾列舉可能短暫對不上（防毒鎖檔之類），取兩者較大值，
                    # 再以 target 封頂。
                    stored_saved = prog.get("saved", 0)
                    if not isinstance(stored_saved, int) or stored_saved < 0:
                        stored_saved = 0
                    effective = min(max(existing, stored_saved), target_count)
                    if not cand.exists():
                        # 檢查點指著的資料夾不見了——最常見是使用者自己清了
                        # output/。真有 saved>0 的話就是有工作被丟掉，值得吭聲。
                        print(f"  resume: the checkpoint folder for "
                              f"`{char_name}` is gone "
                              f"(checkpoint={stored_saved}); starting from "
                              f"image 1", file=sys.stderr)
                        emit_event("resume_unusable", name=char_name,
                                   reason="folder_missing", saved=stored_saved)
                    elif effective <= 0:
                        # 上一輪在存下第一張之前就被打斷，沒有東西可以接。這是
                        # 正常情形（不是故障），留 log、不發事件。
                        print(f"  resume: checkpoint for `{char_name}` has no "
                              f"images on disk yet; starting from image 1")
                    elif effective < target_count:
                        resume_dir = cand
                        resume_count = effective
                        print(f"  resume: `{char_name}` {effective}/"
                              f"{target_count} in {cand.name}/ (folder={existing}"
                              f", checkpoint={stored_saved}); continuing")
                    else:
                        # Completed last run but never popped (e.g. crash before
                        # the pop). Reuse the folder; generate_loop adds 0 and
                        # the pop below fires.
                        resume_dir = cand
                        resume_count = effective
                        print(f"  resume: `{char_name}` already complete "
                              f"({effective}/{target_count}) in {cand.name}/; "
                              f"will pop")
            out_dir = resume_dir if resume_dir else allocate_output_dir(
                char_name, iter_batch_start)
            # Checkpoint THIS pair before generating so a mid-character
            # interrupt can resume it next run. Written once; the folder itself
            # tracks how many are done. Cleared on a successful pop below.
            _run_progress.write_progress(prompt_entry, entry1, entry2,
                                         undesired_entry, out_dir.name,
                                         target_count)
            saved = generate_loop(port, char_name, batch_cfg,
                                     iter_batch_start, out_dir=out_dir,
                                     resume_count=resume_count,
                                     refill=(prompt_entry, entry1, entry2,
                                             undesired_entry),
                                     minimize_fn=minimize_fn,
                                     seen_srcs=session_srcs,
                                     seen_digests=session_digests)
            total_saved += saved
            produced += 1
            print(f"  saved {saved}/{target_count} images")

            # Pop consumed entries only when the target was (near) fully saved.
            # Don't touch fallback files — prompt.md / character2.md /
            # undesired.md are persistent defaults, not one-shot queue entries.
            #
            # CRITICAL — padding-aware pop. `pair_todos` pads a SHORTER list by
            # repeating its LAST entry, so e.g. todo_prompt=[P] paired with
            # todo1=[a,b,c] yields prompt column [P,P,P]. That last remaining
            # entry is still needed by every later (padded) pair, so popping it
            # the moment its first occurrence completes drains the short queue
            # ahead of the long one. On a mid-run cancel the file is then left
            # emptied (count mismatch) and a restart falls back to prompt.md for
            # the rest. Guard: only pop the cursor entry when it's NOT a padded
            # tail repeat (cursor index < len-1) OR this is the final batch
            # (nothing left to reuse the tail). `should_pop_at` captures that
            # (skip==0 reduces exactly to the old `_can_pop` front-pop).
            is_last = (len(pairs) - skip) <= 1
            # A returning generate_loop walked the FULL image count (it only
            # raises on a renderer crash / consecutive_fail_abort, both of
            # which bypass this pop via main()'s outer except), so the
            # character is finished. Requiring an exact `saved == target` was
            # too strict: one scattered transient generate/download miss left
            # saved at e.g. 239/240, the entry was retained, and the whole
            # character silently regenerated next run — the probabilistic
            # queue desync. Pop once saved clears `min_save_ratio` of target
            # (floored at 1 so a zero-save run never pops — that's the rc=3
            # backstop). 1.0 restores the old exact-target behavior.
            min_save_ratio = batch_cfg["min_save_ratio"]
            pop_threshold = max(1, math.ceil(target_count * min_save_ratio))
            if saved >= pop_threshold:
                # 動態模型下「磁碟」就是權威：每圈一開始已用 read_queues() 重讀，
                # real_* 即此刻磁碟內容。pop 前再 reconcile_todo_with_disk 一次以
                # 沿用既有「外部編輯 → 備份原磁碟內容、磁碟優先」語意（這裡的
                # reconcile 多半是 no-op，因為 real_* 才剛讀過；但若 generate_loop
                # 跑了數分鐘期間又被改，這次 reconcile 會抓到並備份）。
                # 只對「真正的佇列檔」對帳 / pop：fallback 模式（prompt.md /
                # character2.md / undesired.md）時對應的 todo_*.md 是空檔，跟記憶體
                # 裡的 [fallback] 必然不同，無條件對帳會每個角色都誤判成被外部改動、
                # 寫出多餘的空備份。守衛條件跟下方 pop 一致。
                def _pop_cursor(path, disk_list, entry, is_fb,
                                reuse_tail=True, preserve_blank=False):
                    """對單一佇列在游標位置 pop。disk_list 為此刻磁碟內容
                    （read_queues 剛讀的 real_*）。回 (新清單, 是否真的 pop 了)。"""
                    if is_fb:
                        return disk_list, False
                    cur = reconcile_todo_with_disk(
                        path, disk_list, preserve_blank=preserve_blank)
                    if not cur:
                        return cur, False
                    if (reuse_tail and not _queue_consume.should_pop_at(
                            len(cur), skip, is_last)):
                        return cur, False
                    ri = _queue_consume.pop_index(len(cur), skip)
                    # front-match 守衛：游標位置那筆要等於剛消耗的 entry 才 pop，
                    # 否則（使用者重排 / 刪除了該筆）跳過、不蓋掉編輯。
                    if cur[ri] != entry:
                        return cur, False
                    cur.pop(ri)
                    write_todo_characters(path, cur)
                    return cur, True
                new_p, popped = _pop_cursor(TODO_PROMPT_FILE, real_p,
                                            prompt_entry, fb_p)
                if popped:
                    print(f"  popped prompt entry; {len(new_p)} remaining in "
                          f"{TODO_PROMPT_FILE.name}")
                new_1, popped = _pop_cursor(TODO_FILE_1, real_1, entry1, fb_1)
                if popped:
                    print(f"  popped char1 entry; {len(new_1)} remaining in "
                          f"{TODO_FILE_1.name}")
                # Real Character 2 queue entries are one-shot. Do not retain
                # and pad its last entry into later Character 1 batches.
                new_2, popped = _pop_cursor(
                    TODO_FILE_2, real_2, entry2, fb_2, reuse_tail=False,
                    preserve_blank=True)
                if popped:
                    print(f"  popped char2 entry; {len(new_2)} remaining in "
                          f"{TODO_FILE_2.name}")
                new_u, popped = _pop_cursor(TODO_UNDESIRED_FILE, real_u,
                                            undesired_entry, fb_u)
                if popped:
                    print(f"  popped undesired entry; {len(new_u)} remaining in "
                          f"{TODO_UNDESIRED_FILE.name}")
                # Character done & popped — drop the resume checkpoint so the
                # next run doesn't mistake a finished pair for one in progress
                # (critical when adjacent pairs share identical prompts).
                _run_progress.clear_progress()
            else:
                # 未達門檻：不 pop，保留此條目在前端、游標越過（下輪 run 重試）。
                print(f"  WARN: only {saved}/{target_count} saved "
                      f"(< {pop_threshold} = {min_save_ratio:.0%} threshold); "
                      f"todo entry retained for retry")
                skip += 1

            elapsed_h = (time.monotonic() - schedule_start_mono) / 3600
            print(f"  schedule elapsed: {elapsed_h:.2f}h")
            schedule_limit_h = batch_cfg["schedule_limit_hours"]
            if elapsed_h > schedule_limit_h:
                rest_h = batch_cfg["rest_hours"]
                rest_s = rest_h * 3600
                # `rest_hours: 0` 是合法設定（＝不休息，只把計時器歸零）。零長
                # 度的休息不該發事件，否則對話平台上會出現一則「休息 0 小時」。
                if rest_s > 0:
                    # 這裡刻意**分成兩個變數**，不折衷成一個：
                    #   * `rest_started_mono` 只量「實際休了多久」＝間隔 → 單調
                    #     時鐘（休息預設 6 小時，最容易跨到一次 NTP 校時）。
                    #   * `wake_ts` 是要**離開本行程**的絕對時間點：印給人看的
                    #     「幾點醒來」、寫進 `schedule_rest` 事件給 bot 的
                    #     `_resting_until` 拿去跟它自己的 `time.time()` 比對
                    #     （`wake <= now` ＝ 休息已過期）。monotonic 的零點每個
                    #     行程都不一樣，這個值換成 monotonic 就完全沒有意義。
                    # **這是本檔 `time.time() + N` 唯一刻意保留的一處。** 其餘
                    # 七處 DOM 輪詢逾時（`select_model` 兩處、`_click_gender`、
                    # `click_add_character_control`、`_click_option_by_text`、
                    # `select_sampler`、`click_generate`）已全部改成單調時鐘；
                    # 下一個做同類掃描的人請**不要**把這一行一起「修好」——它不是
                    # 逾時判定，是要離開本行程的絕對時間點。
                    rest_started_mono = time.monotonic()
                    wake_ts = time.time() + rest_s
                    wake_at = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(wake_ts))
                    print(f"  schedule limit ({schedule_limit_h}h) exceeded; "
                          f"resting {rest_h}h (wake at {wake_at})")
                    # 休息是**刻意**的閒置，不是卡死——但外面分不出來：`/rate`
                    # 的「超過一小時沒有新圖」警告在休息的每一分鐘都會亮。發事
                    # 件讓 bot 能講清楚，也讓 `/rate` / `/eta` 有依據。
                    emit_event("schedule_rest", character=char_name,
                               rest_sec=round(rest_s, 1),
                               wake_ts=round(wake_ts, 1),
                               worked_sec=round(elapsed_h * 3600, 1))
                    if rest_until(port, wake_ts):
                        prev_prompt = None
                        prev_e2 = None
                        prev_undesired = None
                    # 實際休了多久要**量**、不要拿設定值充數：暫停標記會把休息
                    # 拉長，插播單圖也會。
                    emit_event("schedule_resumed", character=char_name,
                               rested_sec=round(
                                   time.monotonic() - rest_started_mono, 1))
                schedule_start_mono = time.monotonic()
                print("  resumed; schedule timer reset to 0")

            # `fallback_single`：真實佇列全空、本輪只產這一個 fallback 角色 →
            # 產完（已 pop / clear）即收工，不再重讀（避免長度 1 的 fallback
            # 把抽乾的真實佇列延長成幽靈角色）。放在 chrome 重啟前，省去無謂重啟。
            if fallback_single:
                break

            # Memory-leak mitigation: cycle Chrome every N completed characters
            # to flush the renderer before it OOMs on the long run. Done at the
            # character boundary (not mid-character) so no batch is interrupted.
            # Skipped when nothing is left to generate (run about to end anyway):
            # 動態模型沒有固定的「最後一批」，所以用 read_queues()+decide() 偷看
            # 下一圈會不會 BREAK，會就跳過重啟（白重啟再 teardown 浪費時間）。
            chars_completed += 1
            restart_every_chars = batch_cfg.get(
                "restart_chrome_every_n_characters", 0)
            more_pending = True
            if restart_every_chars > 0 and chars_completed % restart_every_chars == 0:
                _r, _e, _f = read_queues()
                _peek = _queue_consume.decide(
                    *_r, *_e, *_f, skip, produced)
                more_pending = _peek.action != _queue_consume.ACTION_BREAK
            if (more_pending and restart_every_chars > 0
                    and chars_completed % restart_every_chars == 0):
                print(f"  [memory] {chars_completed} character(s) done; cycling "
                      f"Chrome to flush renderer memory before the next one")
                emit_event("chrome_restart", character=char_name,
                           chars_completed=chars_completed)
                port.restart(email, password)
                # Fresh blank page → force the next pair to re-fill every field
                # (the per-pair diff against prev_* would otherwise skip an
                # unchanged value and leave the new session's field empty).
                prev_prompt = None
                prev_e2 = None
                prev_undesired = None
        # Safety net: a run that walked every pair but saved ZERO images is a
        # dead page that wasn't caught as a crash interstitial (logged-out,
        # un-mounted React app, etc.). Returning 0 here would tell the
        # supervisor "todo finished cleanly" and it would NOT respawn —
        # leaving the queue stuck. Emit a failure (not todo_done) and return
        # non-zero so the supervisor respawns Chrome.
        if end_sentinel_hit:
            # `end` sentinel stop is a clean finish, NOT a broken session —
            # bypass the zero-save backstop below (which would respawn) and
            # exit rc=0 even if nothing was saved this run.
            print(f"\nSTOPPED at 'end' sentinel — total saved {total_saved}")
            emit_event("todo_done",
                       total_saved=total_saved,
                       total_pairs=last_pairs_len,
                       stopped_by_end=True,
                       elapsed_sec=round(
                           time.monotonic() - schedule_start_mono, 1))
        elif produced > 0 and total_saved == 0:
            # rc=3 backstop：實際嘗試生成了至少一個角色（produced>0）卻 0 存檔，
            # 是沒被認成 crash interstitial 的死頁（登出 / React 沒掛上 / 維護）。
            # 回 0 會讓 supervisor 當「乾淨收工」不重生 → 佇列卡死。回非 0 重生。
            print("WARN: generated character(s) but saved 0 images — treating as "
                  "a broken session; exiting non-zero so supervisor respawns.",
                  file=sys.stderr)
            emit_event("critical_error",
                       message=f"attempted {produced} character(s) but saved 0 "
                               f"images (broken session)")
            return RC_ZERO_PROGRESS
        else:
            print(f"\nALL DONE — total saved {total_saved}")
            emit_event("todo_done",
                       total_saved=total_saved,
                       total_pairs=last_pairs_len,
                       elapsed_sec=round(
                           time.monotonic() - schedule_start_mono, 1))
    except GenerationBlockedError as error:
        # 站方擋住生成、重試不會有結果 → 乾淨停止，**不**讓監督者重生。
        # 事件刻意不帶對話框原文：那是站方的原始字串，依保密規則不得送到對話
        # 平台（bot 端只會貼一句固定的中文提示）。全文已寫進 stderr／log。
        print(f"generation blocked; stopping without respawn: {error}",
              file=sys.stderr)
        emit_event("generation_blocked", saved=total_saved, produced=produced)
        return RC_GENERATION_BLOCKED
    except Exception as error:  # pylint: disable=broad-except
        # `message` 走 `_long_error`：裸的 `str(error)` 沒有長度上限，而且會把
        # chromedriver 附的十幾行 C++ `Stacktrace:` 整段帶進事件檔。
        # `traceback` 走 `_traceback_excerpt`：舊的 `format_exc()[-1500:]` 在例外
        # 來自函式庫深處時會把我們自己的 frame 全部切掉（實測 09-07 三次，我們的
        # frame 一個都不剩）。`code_drift` 說明那份 traceback 的原始碼文字可不可信。
        emit_event("critical_error",
                   message=_long_error(error),
                   code_drift=code_drift_flag(),
                   traceback=_traceback_excerpt(traceback.format_exc()))
        # 「這一輪嘗試過生成、卻一張都沒存」＝零產出。回 rc=3（與下面走完整輪的
        # zero-save backstop 同一個值）而不是讓例外炸穿成 rc=1，監督者才有辦法
        # 數「連續幾輪零產出」並在該放棄的時候放棄——否則慢速失敗（每輪都要跑
        # 完 consecutive_fail_abort 才死）永遠碰不到 rapid-fail 那道閘。
        # 有存到圖的崩潰照舊往上炸：那是真的該無限重生的情況。
        if produced > 0 and total_saved == 0:
            print("WARN: run produced no images at all before aborting; "
                  "exiting rc=3 so the supervisor can count zero-progress "
                  "runs.", file=sys.stderr)
            return RC_ZERO_PROGRESS
        raise
    finally:
        # 每一條離開路徑都要放掉：正常結束、rc=3／rc=4、以及往上炸的例外。
        # `release()` 冪等且永不 raise，所以放在這裡不會蓋掉真正的失敗原因。
        _awake.release()
    return 0
