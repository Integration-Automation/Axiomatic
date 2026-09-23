"""Shared loader for `batch_config.json`.

Both `discord_bot.py` and the two webrunner scripts import this. The bot
and webrunners do NOT import each other (per CLAUDE.md module boundaries
they communicate only through files); importing a passive helper module
like this one is fine.

Webrunner re-loads at the start of every character iteration so changes
take effect on the *next* character without killing the run. Bot reads on
every `!progress` / `!eta` / `!preview` call. No caching — disk hits are
trivial vs the work these guards run before.

Missing / malformed file → return defaults, never raise. Keys present but
wrong type / out of range → use the per-key default and warn on stderr so the
issue shows up in `WEBRunner.log`.

**那條警告在 2026-09-09 之前只對 `model_candidates` 成立**，其餘十四個鍵是靜默
退回的——也就是這段 docstring 描述了一個不存在的行為。現在十五個鍵全部走
`_take()`／`_COERCERS`，所以「新增一個設定卻忘了加警告」不再可能發生。
重複的抱怨由 `_warn_once` 收斂：本函式沒有快取，webrunner 每個角色、bot 每次
查詢都會重讀一次。
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

# `float()` 轉得過去的上限。與超大 int 比較不會溢位（int↔float 的比較是精確的），
# 所以拿它當「這個數字轉得成 float 嗎」的守門。
_FLOAT_MAX = sys.float_info.max

# Repo root resolution: this file lives at `<repo>/axiomatic/_batch_config.py`,
# so `.parent.parent` is the repo root regardless of who imports.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
BATCH_CONFIG_FILE = _PROJECT_ROOT / "batch_config.json"
# Sibling temp written then `os.replace`d onto the real file (same directory →
# same filesystem → atomic on Windows and POSIX). See `_atomic_write_config`.
_BATCH_CONFIG_TMP = BATCH_CONFIG_FILE.with_suffix(".json.tmp")

_DEFAULT_BATCH_CONFIG: dict = {
    "images_per_character": 240,
    "inter_image_delay_sec": (20, 30),
    "schedule_limit_hours": 16,
    "rest_hours": 6,
    "generate_max_retries": 4,
    # Back off after a failed generation before clicking Generate again.
    # Keep this separate from inter_image_delay_sec: the latter is normal
    # pacing after a successful image, while this protects the failure path.
    "generate_retry_delay_sec": (25.0, 30.0),
    "download_max_retries": 3,
    # After this many in-a-row image failures, `generate_loop` raises so
    # `main()` exits non-zero and the supervisor respawns Chrome from
    # scratch. Without this, a dead Chrome would let the loop silently
    # run all 240 iterations with 0 saves and exit rc=0, fooling the
    # supervisor into thinking the schedule finished cleanly. 2× the
    # alert threshold (5) by default — gives NovelAI server hiccups
    # some headroom without wasting hours on a truly broken session.
    "consecutive_fail_abort": 10,
    # Quit + relaunch Chrome after every N completed characters to flush the
    # renderer's accumulated memory. NovelAI is a single-page app that leaks
    # across hundreds of generations until Chrome throws "Aw, Snap! — Out of
    # Memory" (and on a low-RAM box the next spawn then dies with
    # SessionNotCreatedException). The restart happens at the character
    # boundary in `main()` (NOT mid-character): each restart re-runs full
    # setup (login via persisted cookie + model / characters / resolution /
    # sampler), and the next character re-fills its prompts normally. Costs
    # ~30-60s per restart. 1 = restart after every character (default);
    # 2 = every other character; 0 = disable. Hot-reloaded per character.
    # NOTE: this restarts BETWEEN characters, so it cannot rescue a single
    # character whose own `images_per_character` run OOMs mid-way — lower
    # `images_per_character` if that happens.
    "restart_chrome_every_n_characters": 1,
    # Fraction of `images_per_character` that must actually be saved for a
    # completed character to count as "done" and have its todo entries popped
    # from the queue. `generate_loop` always walks the full image count and
    # only RAISES on a renderer crash / `consecutive_fail_abort` (which bypass
    # the pop), so a normal return means the character finished — but a few
    # scattered transient generate/download failures can leave `saved` just
    # under target (e.g. 239/240). Requiring an exact match would then retain
    # the entry and silently regenerate the whole character next run, and the
    # queue counts drift out of sync. Pop threshold = ceil(target * ratio),
    # floored at 1 so a zero-save run never pops (that's the rc=3 backstop's
    # job). Default 0.9 = tolerate up to 10% transient misses. 1.0 restores
    # the old exact-target behavior. Hot-reloaded per character.
    "min_save_ratio": 0.9,
    # When False, `snap()` is a no-op in both webrunner variants — no
    # `debug_*.png` files are written, the bot's `!debug_show` /
    # `!cleanup_debug` commands keep working but won't find recent
    # output. Default off to keep the working directory clean; set true
    # in `batch_config.json` and restart the webrunner when actually
    # diagnosing a selector / setup issue. Read once at webrunner
    # module import (NOT hot-reloaded per character), so toggling it
    # mid-run requires `!stop` + `!run`.
    "debug_screenshots": False,
    # 額度用完（站方跳出購買／方案對話框）時的等待策略。行為：關掉對話框 → 等
    # `quota_wait_poll_sec` 秒 → 重試同一張圖；恢復了就無縫接著跑。等待期間**不**
    # 計入 `consecutive_fail_abort`，行程也不結束，所以監督者不會介入、也不會重跑
    # 登入 ＋ setup。
    #
    # 為什麼預設 1 小時——**結論不變，但原本寫的理由是錯的**（2026-08-30 用
    # 128.5 小時、881 張圖、101 個 block→recover 週期的正式記錄實測）。
    # ⚠️ 下面那段的「1 小時好 23%」**也已經被 2026-09-07 的複驗推翻**（分母混進了
    # 與輪詢無關的開銷），先看本註解最後那一段再決定要不要動這個值。
    #
    # 原本寫「額度回補是以小時計的量級，十分鐘戳一次只是白點 Generate，額度並
    # 不會因此早一點回來」。**額度其實是連續回補的**：記錄裡有一段是用約 10 分鐘
    # 的輪詢在跑，44 個週期**每一個**都在等 10 分鐘之後真的產出了圖。若真是整點
    # 才補，那 44 次重試應該全部立刻再撞一次對話框、一張都產不出來。
    #
    # 實測（已扣掉排程休息的 12 小時；36 小時的停機空窗不在任一區間內）：
    #   約 10 分輪詢  202 張 / 25.3 實跑小時 = 7.97 張/小時（44 個週期）
    #   60 分輪詢     513 張 / 52.5 實跑小時 = 9.77 張/小時（57 個週期）
    #
    # 也就是說 1 小時**確實比較好，好 23%**，但原因不是「額度還沒回來」，而是
    # **每個週期都要付一次固定成本**：撞上對話框要先等滿 180 秒的產圖逾時，再
    # reload、再把提示詞欄位重填回去，一次約 4～5 分鐘。輪詢越短、週期越多，這
    # 筆固定成本占的比例就越高（101 個週期 × 180 秒 ＝ 5.05 小時）。
    #
    # ── 2026-09-07 複驗：**這個鍵根本不是吞吐量旋鈕，別再為了跑快一點去調它。**
    #
    # 上面那個「1 小時好 23%」的數字有一個混淆因子：分母「實跑小時」是整段牆鐘，
    # 裡面混進了 Chrome 週期性重啟、setup、停機空窗——那些跟輪詢間隔無關。改用
    # 只看額度本身的窗口就乾淨了：以**同一個角色相鄰兩次 `quota_blocked` 之間**
    # 為一個窗口，長度取時間戳差、產出取 `image_index` 差。
    #
    # 資料是 `events.ndjson` 裡 214 個 `quota_blocked`（08-24 → 09-07），剛好橫跨
    # 兩個設定值（08-25 07:10 之前約 14 分鐘、之後約 60 分鐘），同一個帳號：
    #   短輪詢期  窗口中位 14.3 分（n=43）  **7.87 張/小時**（中位 8.38、CV 0.165）
    #   長輪詢期  窗口中位 67.8 分（n=151） **7.84 張/小時**（中位 7.94、CV 0.089）
    # **194 個窗口、兩週，差 0.4%。**
    #
    # **重算時務必套同一個過濾條件：只取長度 ≤ 2 小時的窗口。** 長輪詢那一組原始
    # 有 153 個，其中一個是 08-25 18:30 起、長 **36.5 小時只產 6 張** 的窗口——那是
    # 上面提過的 36 小時**停機**空窗，機器根本沒在跑，不是額度窗口。不濾掉的話
    # 「總張數 ÷ 總時數」會被它一個人拉到 **6.50**，看起來像是短輪詢比較好，結論
    # 整個反過來。（中位數不受影響，仍是 7.94——這也是為什麼兩個統計量都列出來。）
    #
    # 而且形狀也對得上「持續滴入」而非「整點發一批」：全部 196 個窗口裡，**速率**
    # 的變異係數 0.131、**張數**的變異係數 0.427，窗口長度與張數的相關 r=0.118。
    # 不變的是速率，會變的是張數。上限在**帳號那一側**，我們這一側的輪詢間隔完全
    # 影響不到它——調短只是把同樣的圖切成更多更小的爆發。
    #
    # 所以「未被擋住時 97 張/小時 vs 有效 8 張/小時」那個落差**不是可回收的浪費**，
    # 那個落差就是帳號的回補速率本身。
    #
    # **不要改成退避（backoff）。** 收益為零（上面），成本卻是真的：每一次醒來都
    # 要跑一次 `dismiss_blocking_dialog`，而在 2026-09-07 Rule 3 上線之前，實測
    # **218/218** 次都關不掉、全部落到整頁 reload ＋ `on_reload` 重填欄位——那正是
    # 這個專案出過最嚴重那次故障的觸發路徑（欄位只填回一部分，接著安靜地用錯提示詞
    # 燒掉 10 張圖 / 兩小時）。從一天約 24 次醒來變成約 200 次，是下檔真實、上檔為零。
    #
    # （數字別再引用舊的 109——那只是某個子區間。另外注意當時 log 裡有 153 筆
    # 「dismissed」是舊版**在驗證之前**就無條件印的假訊息，真正的判準是「每天的
    # reload 次數 == 當天的嘗試次數」。）
    #
    # 2026-09-07 後續：Rule 3（右上角無字圖示鈕）上線之後關閉**其實成功了**，只是
    # 後面還有第二層帳號對話框，所以仍然走 reload；已改成有上限的關閉迴圈。這不
    # 改變上面的結論——退避的成本是「醒來的次數」，跟關得掉關不掉無關。
    #
    # 每一次 `quota_resumed` 事件都帶 `last_wait_sec`（＝當時生效的輪詢間隔），
    # 所以要重驗上面這件事不必再從時間戳反推設定變更點，直接讀那個欄位即可。
    "quota_wait_poll_sec": 3600.0,
    # 等待總時數上限（秒）。**0 = 無上限**（預設，符合「自動等到額度回復」）。
    # 設成正值時，超過就當成「不是會自己回補的額度問題」（方案到期、帳號被停），
    # 走 rc=4 乾淨停止、讓監督者不要重生。
    "quota_wait_max_sec": 0.0,
    # 批次期間請求系統不要進入待命（Windows）。預設開啟：無人值守的批次要跑好幾
    # 個小時，而這台開發機只支援 S0 低電源閒置（Modern Standby），系統記錄裡平均
    # 一天進入待命 14 次——待命期間批次不會前進，而且從外面看不出來（行程還活著、
    # 沒有崩潰、log 就是停著）。實作見 `_webrunner_shared.StayAwake`。
    # 取捨：筆電靠電池跑時這會讓系統不自動睡，比較耗電。要關就設成 false。
    # 注意它**不會**阻止使用者自己合上蓋子或手動睡眠，只擋閒置轉換。
    "keep_system_awake": True,
    # 產圖模型的候選字面，**依序**嘗試，第一個在下拉選單裡命中的就採用。站方
    # 偶爾會改寫選項字面，多給幾個候選就不必為了小改動而動碼；全部落空時
    # `select_model` 會把當下看得到的選項 dump 進 log，那行 log 就是「該把這個
    # 鍵改成什麼」的答案。
    #
    # 這個鍵**不在** `discord_bot._BATCH_SETTERS` 裡，所以 `/config show` 不會
    # 印它、`/config set` 也改不了它——值是外部服務的模型名，送進對話平台會踩
    # 到 Secrecy Layer 1（`quota_wait_*` 兩個鍵也是同樣的檔案限定作法）。要改
    # 就直接編輯 `batch_config.json`。
    #
    # 為什麼值得做成設定：這是整套系統裡吞吐量差距最大的一個選擇。站方 2026 年
    # 的方案政策把**使用量上限只加在 V5**，其餘模型對頂級訂閱維持不限量；上限
    # 是「連續回補」的電池——2026-08-30 用 101 個 block→recover 週期複驗過這個
    # 說法：短輪詢時每次只等 10 分鐘就有圖產得出來，所以是連續回補而不是整點
    # 補；等 6 小時再回來也沒有看到容量上限的跡象。
    # 排空後的實際產能：**約 9.8 張／小時**（60 分輪詢、513 張／52.5 實跑小時，
    # 已扣掉排程休息）。這個數字比先前記的 4.65 高，是因為那次把停機空窗與排程
    # 休息也算進了分母；要比較的是「額度限制下的產能」，所以只該算實跑時間。
    # 換成沒有上限的模型就沒有這件事。這是畫質與吞吐量的取捨，屬於使用者的
    # 決定，所以預設不動。
    "model_candidates": ("NAI Diffusion V5 Full", "NAI Diffusion V5",
                         "Diffusion V5 Full"),
}


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


def _coerce_pair(value, default: tuple) -> tuple:
    """Accept `[lo, hi]` or `(lo, hi)`; reject anything else."""
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(_is_finite_number(v) for v in value)):
        lo, hi = float(value[0]), float(value[1])
        if 0 <= lo <= hi:
            return (lo, hi)
    return default


def _coerce_int(value, default: int, *, min_value: int = 1) -> int:
    # `_is_finite_number` 也擋掉 bool（`int` 的子類）與轉不成 float 的超大 int。
    if _is_finite_number(value) and isinstance(value, int) and value >= min_value:
        return value
    return default


def _coerce_positive_num(value, default: float) -> float:
    if _is_finite_number(value) and value > 0:
        return float(value)
    return default


def _coerce_non_negative_num(value, default: float) -> float:
    """Like `_coerce_positive_num` but 0 is a legal, meaningful value
    (`quota_wait_max_sec: 0` = 無上限)."""
    if _is_finite_number(value) and value >= 0:
        return float(value)
    return default


def _coerce_ratio(value, default: float) -> float:
    """Accept a number in the half-open range (0, 1]; reject anything else
    (0 would make the pop threshold meaningless, >1 is impossible to reach)."""
    if _is_finite_number(value) and 0 < value <= 1:
        return float(value)
    return default


def _coerce_str_list(value, default: tuple) -> tuple:
    """接受一串非空字串（list 或 tuple），逐項 strip 後丟掉空的；剩下空的就退回
    `default`。任何其他型別（單一字串、數字、含非字串的清單）一律退回 default。

    **單一字串刻意不接受**：`"NAI Diffusion V5 Full"` 是可迭代的，若順手放行，
    使用者以為設了一個候選，實際上會被拆成 22 個單字元候選、一個都不會命中，
    而且 log 只會說「模型選不到」。寧可退回預設並在 stderr 講一聲。"""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return default
    items = tuple(v.strip() for v in value if isinstance(v, str) and v.strip())
    return items or default


def _coerce_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


# 「這個值被拒了」的精準判定。
#
# 上面每一個 `_coerce_*` 都**只**在拒絕時回傳它的 `default` 引數，而且沒有一個會去
# 讀那個引數的內容——所以傳一個唯一的 sentinel 進去，再用 `is` 比對回傳值，就能
# 100% 分辨「被拒絕」與「接受了一個剛好等於預設的值」。2026-09-09 對七個 helper
# 的十四種輸入逐一實測過。
#
# **刻意不用「比對值」來判定。** `_coerce_pair` 會把 `[1, 2]` 正規化成 `(1.0, 2.0)`、
# `_coerce_str_list` 會 strip 掉前後空白——那些是**正當的正規化**，不是拒絕。拿值去
# 比會把它們全部誤報成「你的設定被丟掉了」，而一個會亂叫的守門，遲早被人關掉
# （本專案已經為這件事收窄過 `test_language` 與 `test_text_encoding` 的掃描範圍）。
_REJECTED = object()

# 設定檔的抱怨只印一次。`load_batch_config()` **沒有快取**：webrunner 每個角色開頭
# 重讀一次、bot 每次 `/progress`／`/eta`／`/preview` 也重讀一次，所以一個放著沒改的
# 錯字會用同一行文字反覆寫進 `WEBRunner.log`。同一份專案裡已經有過一次教訓——
# `discord_bot.log` 有 96% 的行是同一句話，把真正有用的診斷擠了出去。
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
except ImportError:  # 套件路徑（`from axiomatic import _batch_config`）
    from axiomatic._warn_dedup import warn_once as _warn_once  # type: ignore  # noqa: E402


# key -> (coercion 函式, 額外關鍵字)。**十五個鍵全部走同一條路**，不再每個鍵手寫
# 一行——新增設定時漏掉警告這件事因此變成不可能，而不是「記得要加」。
# `test_config_numbers.py::test_every_default_key_goes_through_a_coercer` 另外釘住
# 這張表要涵蓋 `_DEFAULT_BATCH_CONFIG` 的每一個鍵。（這行原本指著一支從來不存在的
# 測試；照著去找的人只會得到「這條沒人守」的結論，而實際上是有人守的。）
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


_COERCERS: dict = {
    "images_per_character": (_coerce_int, {}),
    "inter_image_delay_sec": (_coerce_pair, {}),
    "schedule_limit_hours": (_coerce_positive_num, {}),
    "rest_hours": (_coerce_positive_num, {}),
    "generate_max_retries": (_coerce_int, {}),
    "generate_retry_delay_sec": (_coerce_pair, {}),
    "download_max_retries": (_coerce_int, {}),
    "consecutive_fail_abort": (_coerce_int, {}),
    "restart_chrome_every_n_characters": (_coerce_int, {"min_value": 0}),
    "min_save_ratio": (_coerce_ratio, {}),
    "debug_screenshots": (_coerce_bool, {}),
    "keep_system_awake": (_coerce_bool, {}),
    "quota_wait_poll_sec": (_coerce_positive_num, {}),
    # `quota_wait_max_sec` 允許 0（＝無上限），所以不能用 `_coerce_positive_num`。
    "quota_wait_max_sec": (_coerce_non_negative_num, {}),
    "model_candidates": (_coerce_str_list, {}),
}


def _take(raw: dict, cfg: dict, key: str):
    """讀 `raw[key]`、套用它的 coercion，**退回預設時說一聲**。

    「這個鍵根本沒寫」是正常情況，必須完全安靜——所以先看 `key in raw`，不能用
    `raw.get(key)` 之後再判斷（那樣寫成 `null` 的鍵會跟沒寫的鍵長得一樣）。
    """
    if key not in raw:
        return cfg[key]
    coerce, kwargs = _COERCERS[key]
    got = coerce(raw[key], _REJECTED, **kwargs)
    if got is _REJECTED:
        # 退回預設是**靜默**的失敗：使用者以為換了設定，實際上跑的還是原來那個，
        # 而且要等到看見產出才會發現（`model_candidates` 那條原本的註解就是這樣
        # 寫的——現在十五個鍵都適用同一句話）。
        _warn_once(f"_batch_config: `{key}` 的值不合用（收到 {raw[key]!r}），"
                   f"已忽略、沿用預設 {cfg[key]!r}")
        return cfg[key]
    return got


def load_batch_config() -> dict:
    """Return a fully-populated batch config dict, using defaults for any
    missing / malformed key. Always succeeds; never raises."""
    raw: dict = {}
    try:
        text = BATCH_CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return dict(_DEFAULT_BATCH_CONFIG)
        # `UnicodeDecodeError` 是 `ValueError` 的子類別、**不是** `OSError`：
        # 一個被別的編輯器另存成 Big5 的設定檔就會從這裡逸出，而本機 locale
        # 正是 cp950、檔案內容幾乎都含中文。不用 `errors="replace"` 靜靜吞掉——
        # 那會把亂碼當成有效值用下去，比退回預設糟。
    except (OSError, UnicodeDecodeError) as error:
        print(f"_batch_config: read failed: {error!r}", file=sys.stderr)
        return dict(_DEFAULT_BATCH_CONFIG)
    try:
        parsed = json.loads(text or "{}")
        if isinstance(parsed, dict):
            raw = parsed
    except json.JSONDecodeError as error:
        print(f"_batch_config: parse failed: {error!r}", file=sys.stderr)
        return dict(_DEFAULT_BATCH_CONFIG)

    _warn_unknown_keys(raw, _DEFAULT_BATCH_CONFIG, source="_batch_config")

    cfg = dict(_DEFAULT_BATCH_CONFIG)
    for key in _COERCERS:
        cfg[key] = _take(raw, cfg, key)
    return cfg


def read_raw_batch_config() -> dict:
    """Return the raw on-disk JSON dict WITHOUT merging defaults (so callers
    that rewrite the file don't materialise every default key). Missing /
    malformed → empty dict, never raises."""
    try:
        text = BATCH_CONFIG_FILE.read_text(encoding="utf-8")
    # 解碼失敗與讀不到一樣回空 dict（見 `load_batch_config` 的說明）。
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _atomic_write_config(raw: dict) -> None:
    """Write the whole config back atomically.

    A plain `write_text` truncates first and then writes, so a concurrent
    reader can observe a 0-byte or half-written file. That matters here because
    the readers are hot paths on the *other* side of the process boundary: the
    webrunner re-loads this file at the start of EVERY character, and the bot
    reads it on `!progress` / `!eta` / `!preview`. Hitting the truncate window
    makes `load_batch_config` parse `{}` (or fail) and silently fall back to
    EVERY built-in default for that character — e.g. `images_per_character`
    jumping back to 240 from a configured 60, or `min_save_ratio` reverting —
    with only a stderr line to show for it.

    Writing a sibling temp then `os.replace`-ing it makes readers see either
    the old file or the new one, never a partial one (same directory → same
    filesystem → atomic on Windows and POSIX). Mirrors `_run_progress
    ._atomic_write`. Only a genuine write failure raises (OSError), matching
    the previous contract."""
    # `allow_nan=False`：預設會把 inf/nan 寫成裸的 `Infinity` / `NaN`，那**不是合法
    # JSON**——Python 讀得回來，`jq`、瀏覽器、編輯器的 JSON 檢查一律讀不了，而這個檔
    # 案是明擺著給人手動編輯的。寫進去之後才發現等於資料已經壞在磁碟上，所以擋在寫入
    # 這一側。呼叫端傳的值本來就該先過 `_coerce_*`／bot 的 `_parse_cfg_*`，走到這裡還
    # 帶著 inf/nan 表示驗證被繞過了，此時丟 `ValueError` 比默默寫壞檔案好。
    _BATCH_CONFIG_TMP.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.replace(_BATCH_CONFIG_TMP, BATCH_CONFIG_FILE)
    except OSError:
        # `os.replace` only removes the temp on success; clean up then re-raise
        # so the caller still sees the failure.
        try:
            _BATCH_CONFIG_TMP.unlink()
        except OSError:
            pass
        raise


def save_batch_config(updates: dict) -> dict:
    """Merge `updates` into batch_config.json (creating it if absent),
    preserving any keys not mentioned, and write pretty JSON. Returns the new
    effective (defaults-merged, validated) config via `load_batch_config()`.

    Only the write itself can raise (OSError) — the caller decides how to
    surface that. Values are NOT validated here; pass already-coerced values
    (the loader will defensively re-coerce on the next read anyway, so a bad
    value can never crash the consumers, but it would silently revert)."""
    raw = read_raw_batch_config()
    raw.update(updates)
    _atomic_write_config(raw)
    return load_batch_config()


def reset_batch_config_keys(keys) -> tuple[dict, list[str]]:
    """Remove `keys` (an iterable of key names) from batch_config.json so each
    falls back to its built-in default. Returns `(effective_config, removed)`
    where `removed` lists the keys that were actually present on disk. Writes
    only if something changed; never raises except on a genuine write failure
    (OSError)."""
    raw = read_raw_batch_config()
    removed = [k for k in keys if k in raw]
    if not removed:
        return load_batch_config(), []
    for k in removed:
        raw.pop(k, None)
    _atomic_write_config(raw)
    return load_batch_config(), removed
