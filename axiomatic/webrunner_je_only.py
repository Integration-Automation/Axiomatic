"""
webrunner_je_only.py — pure je_web_runner port of webrunner_novelai.py.

Functionally equivalent to ``webrunner_novelai.py`` but routes EVERY browser
interaction through ``webdriver_wrapper_instance``. No ``from selenium ...
import webdriver`` / ``ActionChains`` calls. The only selenium symbol used
is ``Keys`` (constants only — no driver coupling).

Why this file exists
====================

Until je_web_runner 0.x grew the advanced WebDriverWrapper APIs, a pure-
wrapper port could not pass NovelAI's anti-bot filter on a cold start. The
two blockers were:

* ``Options.add_experimental_option("excludeSwitches", ["enable-automation"])``
  and ``add_experimental_option("useAutomationExtension", False)`` — required
  to hide ``navigator.webdriver`` and suppress the automation infobar.
* ``Page.addScriptToEvaluateOnNewDocument`` — required so stealth patches
  run BEFORE NovelAI's React bootstrap snapshots the globals.

Both are now exposed via:

* ``set_driver(..., experimental_options={...})``
* ``execute_cdp_cmd(method, params)`` /
  ``add_script_to_evaluate_on_new_document(source)``

So a pure-wrapper driver is finally on par with the hand-rolled selenium
build in webrunner_novelai.py. This file is the working proof.

How to run
==========

From the repo root::

    py -3 axiomatic/webrunner_je_only.py

It reads/writes the same on-disk contracts as ``webrunner_novelai.py``
(``auth.md``, ``prompt.md``, ``todo_*.md``, ``output/``, ``events.ndjson``,
``debug_*.png``, ``.chrome_profile/``) so the Discord bot keeps working.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Locate the je_web_runner repository (sibling clone, or WEBRUNNER_PATH env).
_WR_ENV = os.environ.get("WEBRUNNER_PATH")
_WR_SIBLING = Path(__file__).resolve().parent.parent.parent / "WebRunner"
WEBRUNNER_PATH = Path(_WR_ENV) if _WR_ENV else _WR_SIBLING
if str(WEBRUNNER_PATH) not in sys.path:
    sys.path.insert(0, str(WEBRUNNER_PATH))

from selenium.common.exceptions import WebDriverException  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402 — constants only
from urllib3.exceptions import (  # noqa: E402
    ConnectTimeoutError as Urllib3ConnectTimeoutError,
    MaxRetryError as Urllib3MaxRetryError,
    NewConnectionError as Urllib3NewConnectionError,
    ProtocolError as Urllib3ProtocolError,
    ReadTimeoutError as Urllib3ReadTimeoutError,
)

# Driver-agnostic helpers shared by both webrunner variants (P6). Imported by
# name so existing call sites stay byte-identical; `ws.*` reaches the rest
# (the bulk now lives in the shared module after C1–C6).
import _webrunner_shared as ws  # noqa: E402
from _webrunner_shared import (  # noqa: E402
    # 裸名字匯入是必要的，不是風格：接合守門認不得 `ws.` 前綴的呼叫。理由與實測
    # 寫在 `webrunner_novelai.py` 同一個匯入區塊。
    _is_single_path_component,
    human_pause,
    human_type,
    hide_browser_windows,
    read_credentials,
    with_retry,
)
# chromedriver 記錄檔的處理，與 selenium 變體共用同一份（實測度量與「為什麼是一份
# 不是兩份」寫在共用模組那個區段的抬頭）。**只匯入函式，不匯入 `_CHROMEDRIVER_LOG*`
# 常數**——理由同 `webrunner_novelai.py` 那一側：常數只留在共用模組，測試 patch 的
# 目標對不上就會立刻 AttributeError，而不是安靜地讓修剪打到 repo 根目錄那個真檔。
from _webrunner_shared import (  # noqa: E402
    _dump_chromedriver_log_tail,
    _rotate_chromedriver_log,
    _trim_chromedriver_log,
)
# spawn 失敗時的例外格式，與 selenium 變體共用同一份——`{err!r}` 會把 selenium
# 的訊息整段丟掉（`args` 是空的）。完整理由與實測寫在那支的 docstring。
from _webrunner_shared import full_error_detail  # noqa: E402

# Transport-level exceptions when the chromedriver conversation itself fails
# (Chrome page hung, GPU stuck, in-page fetch blocking past selenium's 120s
# HTTP timeout — or chromedriver.exe simply gone). Selenium does NOT wrap
# urllib3's exceptions; they propagate raw and would crash the whole webrunner
# mid-batch. Hot-path helpers wrap their script calls in this tuple so caller
# poll / retry loops can recover.
#
# **必須與 `webrunner_novelai.py` 的那一份逐項相同**（`test_variant_parity.py`
# 釘住）。完整的理由、實測資料與「新加進來的每一個類別都同時列在
# `_webrunner_shared._SESSION_GONE_EXC_NAMES` 裡」這條不變式寫在那一份的註解裡，
# 這裡不重抄一遍，免得兩份說明各自漂移。一句話版本：`MaxRetryError` /
# `NewConnectionError` 的 MRO **不經過 `OSError`**，所以它們雖然一直列在 gone
# 名單裡，卻永遠到不了 `_note_transport_error` 手上（2026-09-09 查出，正式環境
# 有 09-07 三次未分類的 `critical_error` 為證）。
_DRIVER_TRANSPORT_ERRORS = (
    WebDriverException,
    Urllib3ReadTimeoutError,        # 連上了但沒回話 → 卡頓（**不**算 gone）
    Urllib3MaxRetryError,           # 重試用盡（實測就是這一個）
    Urllib3NewConnectionError,      # 連不上（ConnectTimeoutError 的子類）
    Urllib3ConnectTimeoutError,     # 連線階段逾時
    Urllib3ProtocolError,           # 連線中途被對方斷掉
    OSError,                        # socket 層（ConnectionRefusedError…）
)

from je_web_runner import (  # noqa: E402
    TestObject,
    webdriver_wrapper_instance,
)

# Short alias — every "driver.X" in webrunner_novelai.py becomes "wr.X" here.
wr = webdriver_wrapper_instance


# ---------- BrowserPort adapter (P6 C2) -------------------------------------
# DOM/driver indirection layer. Every helper reaches the browser through this
# single surface instead of touching `wr` / the old `jss`/`xp`/`xps` shims
# directly, so C3+ can lift the helpers into the shared module unchanged.
# Wraps the module-global `wr` singleton — its underlying driver is (re)bound
# by `wr.set_driver` / `wr.quit` in the lifecycle code, so the port always
# follows the live driver with no re-pointing needed (unlike the selenium
# variant, whose raw driver object is replaced on restart). Mirrors
# `webrunner_novelai.BrowserPort` method-for-method; the only deltas are the
# je wrapper call style and `click` (plain `el.click()` + JS fallback, NOT
# novelai's ActionChains hover) / `mouse_wiggle` (no-op — the je login never
# wiggled). Lifecycle methods (restart/quit/sync_back) are deliberately NOT
# on the port yet — those stay raw `wr.*` for now (deferred to C6).

class BrowserPort:
    """je_web_runner-backed DOM/driver adapter. See module comment above."""

    # Transport-level exception tuple, exposed so shared hot-path readers can
    # `except port.TRANSPORT_ERRORS` without importing selenium / urllib3.
    TRANSPORT_ERRORS = _DRIVER_TRANSPORT_ERRORS

    def restart(self, email, password) -> None:
        """Mid-run Chrome restart (memory flush). Delegates to the per-variant
        `_restart_chrome_session`, which drives the module-global `wr` singleton
        (quit → sync_back → kill_orphan → start_driver → _setup_session). This
        port wraps `wr`, so it automatically follows the new driver."""
        _restart_chrome_session(email, password)

    # ---- JS ----
    def execute_script(self, script, *args):
        return wr.execute_script(script, *args)

    def execute_async_script(self, script, *args):
        return wr.execute_async_script(script, *args)

    # ---- element lookup (lowercase 'xpath' = W3C By.XPATH) ----
    def find_elements_xpath(self, xpath):
        return wr.find_elements(TestObject(xpath, "xpath")) or []

    def find_element_xpath(self, xpath):
        return wr.find_element(TestObject(xpath, "xpath"))

    def ancestors(self, element, depth: int = 4):
        """Return [element, parent, …] up to `depth` ancestors via one
        execute_script round-trip. Mirror of `webrunner_novelai.BrowserPort.
        ancestors` (which uses element-relative find_element) — both yield the
        same chain so `expand_character_section` lives in the shared module."""
        out = wr.execute_script(
            """
            const el = arguments[0], depth = arguments[1];
            const out = [el];
            let cur = el;
            for (let i = 0; i < depth; i++) {
              if (!cur.parentElement) break;
              cur = cur.parentElement;
              out.push(cur);
            }
            return out;
            """,
            element, depth,
        )
        return out or [element]

    # ---- interaction ----
    def click(self, element, pause: float = 0.15):
        # je 既有風格：純 el.click()，失敗退回 JS click。刻意 NOT ActionChains
        # ——那是 novelai 的反偵測 hover；兩變體的點選風格不同、不可抹平。
        # `pause` 參數只為與 novelai port.click 簽章一致（shared 葉子會帶 pause
        # 呼叫）；je 純 click 不需要 hover 停頓，故忽略此值。
        try:
            element.click()
        except Exception:  # pylint: disable=broad-except
            wr.execute_script("arguments[0].click();", element)

    def click_native(self, element):
        """driver 層的**真**點選，不做任何退路——按不動就 raise。

        與 `click` 的分工刻意不同，兩件事都要記住：

        1. **不退回合成點選。** `click` 失敗時會安靜地改用
           `execute_script("arguments[0].click();")`，於是外面分不出「真的按到了」
           與「其實是合成的」。額度對話框那條路需要分得出來：站方第二層對話框不吃
           合成點選，而「退回合成」正好會把那個事實藏起來（本專案已經記過四次
           「log 報的是嘗試不是結果」）。階梯改由 `_webrunner_shared.
           _click_dismiss_target` 一階一階走，每一階都出聲。
        2. **走 W3C 的 element click，不走 ActionChains。** ActionChains 是低階
           指標動作，不做遮擋檢查，會直接打在座標上——換句話說它可能點到蓋在目標
           上面的東西。`element.click()` 會先檢查「收到這個點選的是不是目標本身或
           它的後代」，不是就丟 `ElementClickInterceptedException`。在購買對話框上
           這個檢查就是安全性質本身（我們自己已經不再由 JS 決定點誰）；而那個例外
           連帶會指名蓋在上面的元素，正好是我們要的診斷。所以這一支**兩個變體實作
           相同**，不像 `click` 那樣分成 ActionChains 與純 click 兩種風格。
        """
        element.click()

    def mouse_wiggle(self):
        # je 的 login 從不做滑鼠抖動；保留 no-op 以與 novelai 介面對稱。
        return None

    def press_enter(self, element):
        element.send_keys(Keys.ENTER)

    def press_escape(self):
        """driver 層的真 Escape（`isTrusted` 的事件）。回是否送出去了。

        鏡像 `webrunner_novelai.BrowserPort.press_escape`；差別是這一側**不用
        ActionChains**（本變體刻意只用 `Keys` 常數、不碰 selenium 的驅動 API），
        改成對焦點元素 send_keys。
        """
        try:
            element = wr.execute_script(
                "return document.activeElement || document.body;")
            if element is not None:
                element.send_keys(Keys.ESCAPE)
                return True
        except Exception:  # pylint: disable=broad-except
            pass
        return False

    # ---- page / session ----
    def get(self, url):
        wr.to_url(url)

    def current_url(self):
        return wr.get_current_url() or ""

    def get_title(self):
        return wr.get_title()

    def refresh(self):
        wr.refresh()

    def save_screenshot(self, path):
        return wr.save_screenshot(path)


# Module-global port. Stateless wrapper over the `wr` singleton, so a single
# instance is valid for the whole process (including across Chrome restarts).
port = BrowserPort()


# ---------- config (must match webrunner_novelai.py) ------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_FILE = PROJECT_ROOT / "auth.md"  # main shell reads credentials before boot
# Queue / fallback files (TODO_*_FILE / PROMPT_FILE / CHARACTER2_FALLBACK_FILE /
# UNDESIRED_FILE) moved to _webrunner_shared with read_queues + run_batch (C6).
# `OUTPUT_ROOT` 已於 2026-09-11 從本變體移除：它在這個檔裡**只有寫、沒有讀**
# （實測：一個 `Store`、零個 `Load`），輸出資料夾的決定權在
# `_webrunner_shared.allocate_output_dir`。留著的害處是 grep 的人會看到三份定義
# （兩個變體 ＋ shared）對到一個活的用途。novelai 那份還在，因為它真的被讀
# （`OUTPUT_ROOT / "_verify"`，隔離驗證模式用）。
EVENTS_FILE = PROJECT_ROOT / "events.ndjson"
# DOM_REQUEST_FILE 已移入 _webrunner_shared（check_dom_request 一併搬走，C3）。
# 單張生成請求 — 必須與 webrunner_novelai.py 完全對應。bot 寫請求進
# single_image_request.json，webrunner 啟動時（idle one-shot）或 iteration
# 邊界（batch 進行中 in-band）poll 它，只產 1 張、結果走 events.ndjson 的
# `single_image_done` event 回傳 bot。one-shot 圖一律存進
# output/_oneshot/<request_id>/。serve/check 已搬入 _webrunner_shared（C4），
# SINGLE_IMAGE_REQUEST_FILE 與 SINGLE_IMAGE_OUTPUT_ROOT 一併移入 shared。
# **本變體刻意不再持有 SINGLE_IMAGE_REQUEST_FILE**：pass 3 之後 main() 用
# ws.parse_run_mode(sys.argv) 判斷這一輪是不是單圖伺服器，不再從磁碟上有沒有
# 請求檔推論（推論錯過一次，事故紀錄在 ws.RUN_MODE_BATCH 上面那一段）。
CHROME_PROFILE_DIR = PROJECT_ROOT / ".chrome_profile"


def hide_chrome_window() -> bool:
    """確保瀏覽器視窗在螢幕外，回 True 表示找到視窗、而且沒有任何一個留在螢幕上。

    與 `webrunner_novelai.hide_chrome_window` 同構，只差這支沒有 driver 參數
    （這個變體的 driver 狀態在模組層）。實際動作在
    `_webrunner_shared.hide_browser_windows`，兩邊共用一份。**不再最小化**
    （2026-09-22）：理由見 `_webrunner_shared.OFFSCREEN_WINDOW_POSITION`。
    """
    hidden, exposed = hide_browser_windows(
        [CHROME_PROFILE_DIR, CHROME_PROFILE_SNAPSHOT])
    return hidden > 0 and exposed == 0


# CONSECUTIVE_FAIL_ALERT moved to _webrunner_shared with generate_loop (C5).
LOGIN_URL = "https://novelai.net/login"
IMAGE_URL = "https://novelai.net/image"
# 產圖模型的候選字面住在 `batch_config.json` 的 `model_candidates`（預設值與
# 型別檢查在 `_batch_config`），`_setup_session` 每次 setup 讀一次。
# **不要在這裡放回一份模組常數**：兩個 webrunner 變體各存一份、又必須永遠一致，
# 正是 CLAUDE.md 點名的漂移陷阱——設定檔是唯一來源，兩邊都從那裡讀。
# 站方全部候選都落空時 `ws.select_model` 會把當下看得到的選項 dump 進 log，
# 那行 log 就是「該把 `model_candidates` 改成什麼」的答案。

# Batch params live in `batch_config.json` at repo root; loaded once per
# character iteration via `load_batch_config()` so edits hot-reload to the
# NEXT character without killing the run. Defaults in `_batch_config
# ._DEFAULT_BATCH_CONFIG` match the historical hardcoded values.

REAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
  window.navigator.permissions.query = (params) =>
    params && params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : origQuery(params);
}
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
"""


# DOM diagnostics（_DOM_DIAG_JS / dump_textareas_diag / format_textareas_diag）
# 已移入 _webrunner_shared（C3）。


# ---------- single-image (one-shot) request ---------------------------------
# 跟 webrunner_novelai 對應函式 isomorphic、只差用 je_web_runner 的 module-level
# driver state（沒 driver arg）。詳見對應函式 docstring。


# ---------- je_web_runner thin helpers --------------------------------------

def wait_until(predicate, timeout: float = 30.0, poll: float = 0.3,
               description: str = "predicate") -> bool:
    """Poll-until-true with timeout. Returns True if predicate became truthy,
    False on timeout. Swallows predicate exceptions (treated as falsey)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except Exception:  # pylint: disable=broad-except
            pass
        time.sleep(poll)
    print(f"  wait_until: timed out waiting for {description}")
    return False


# `_DEBUG_SCREENSHOTS` / `snap` and `_CHROME_CRASH_TITLE_MARKERS` /
# `_is_chrome_crash_page` moved to _webrunner_shared (C3).


# ---------- driver setup ----------------------------------------------------

# Chrome 在 user-data-dir 留下的 singleton lock 檔名。兩個用途：
# `_SNAPSHOT_IGNORE_NAMES`（複製 profile 時跳過）與 `_clear_snapshot_locks`。
#
# **不要照抄「殘留的 lock 檔會擋住下一個 Chrome」那句話。** 那是 POSIX 的行為；
# 2026-09-07 在這台機器上量過，Windows 不是這樣：
#   * 五個名字裡只有 `lockfile` 會真的出現，而且它的行為與
#     `CreateFile(dwShareMode=0, FILE_FLAG_DELETE_ON_CLOSE)` 逐項相符——實測用一個
#     跟 Chrome 無關的子行程重現過三件事：持有者活著時檔案在、另一個行程連唯讀
#     開啟都拿到 `PermissionError 13`、**持有者被硬砍之後檔案自己消失**。活著的
#     `.chrome_profile_snap/lockfile` 對第二點的反應一字不差。
#   * 對照觀察：`.chrome_profile/`（Chrome 2026-05-20 之後就沒再從那裡開過）與
#     使用者自己的 Chrome user-data-dir，**兩邊五個名字一個都沒有**——而本專案的
#     `_kill_orphan_chrome` 這一輪就對全機硬砍過 30 次。
#   * 另外四個是 POSIX 那一側的構造，兩個 profile 目錄從來沒有出現過。
# 也就是說「殘留的 lock 檔擋住啟動」在這個平台上不是一個可到達的狀態。整段推論
# 與它砍掉了什麼，見 `_clear_snapshot_locks` 的 docstring。
_CHROME_LOCK_FILES = (
    "SingletonLock", "SingletonCookie", "SingletonSocket",
    "lockfile", "RunningChromeVersion",
)


def _kill_orphan_chrome() -> int:
    """Kill every leftover `chrome.exe` / `chromedriver.exe` before we spawn
    our own. Mirrors `webrunner_novelai._kill_orphan_chrome` — see that
    docstring for the full rationale (orphan renderer/GPU children survive a
    crash-respawn cycle, pile up, and OOM the next Chrome — the "舊實例"
    spiral). Two passes: psutil kill, then `taskkill /F /T` fallback for
    survivors (AccessDenied, reparented children). msedge spared. Always
    prints a summary so the log shows the sweep ran.

    **本變體也是三個呼叫點，理由各自不同**（跟 selenium 變體一一對應）：
    `main()` 開頭（我們的 driver 還不存在）、`_restart_chrome_session()`
    （`wr.quit()` 之後，我們正要拆掉自己的 Chrome，殺掉它就是目的）、
    `start_driver()` 的 spawn 重試之間（要釋放半死的 chrome.exe 佔住的記憶體，
    否則低記憶體機器上重試也會 OOM）。後兩者發生在執行途中、而且是全機範圍的
    掃描——使用者自己開的 Chrome 會一起被殺，沒有預告；`msedge.exe` 刻意放過就是
    這個假設的另一半。完整的代價分析寫在 selenium 變體那份 docstring。

    ⚠️ **但那份 docstring 的最後一段不適用於本變體。** 它講的是
    `_SUPPRESS_ORPHAN_SWEEP`——`verify_browser.py --full` 在隔離環境裡跑時用來
    關掉這個全機掃描的抑制開關，好讓一次驗證執行不會核彈式殺掉使用者自己的
    Chrome。**本變體沒有那個旗標，也沒有隔離驗證的進入點**（`verify_browser`
    目前接的是 selenium 變體），所以沒有東西需要被抑制。這不是漏掉的功能，是
    範圍不同。

    將來若要把驗證入口接到這個變體，這一段就是那時候要先處理的事：在跑
    `main()` 之前必須有一個等價的抑制開關，否則一次「驗證」會把使用者桌面上
    所有的 Chrome 一起關掉。"""
    targets = {"chrome.exe", "chromedriver.exe"}
    own_pid = os.getpid()
    killed = 0
    remaining = 0
    psutil_note = ""
    try:
        import psutil  # type: ignore
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                if proc.info["pid"] == own_pid:
                    continue
                if (proc.info.get("name") or "").lower() not in targets:
                    continue
                proc.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        # Recount survivors so we know whether the taskkill fallback is needed.
        for proc in psutil.process_iter(attrs=["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() in targets:
                    remaining += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        # psutil unavailable → rely entirely on taskkill below.
        remaining = -1
        psutil_note = " (psutil unavailable → taskkill only)"
    except Exception as error:  # pylint: disable=broad-except
        # psutil 掃描自己炸掉 → **這正是後援存在的理由**。舊版在這裡只印一行
        # 就讓 `remaining` 停在 0，於是底下的 `remaining != 0` 是 False、整段
        # taskkill 被跳過，摘要還照樣印「killed 0 via psutil」——跟「這台機器
        # 很乾淨」一字不差。孤兒行程原封不動留著，下一個 Chrome 照樣 OOM。
        # psutil 出錯是「psutil 掃不到東西」最強的證據，不是跳過後援的理由。
        print(f"orphan-chrome sweep: psutil pass errored: {error!r}",
              file=sys.stderr)
        remaining = -1
        psutil_note = " (psutil pass failed → taskkill only)"
    if os.name == "nt" and (remaining != 0):
        import subprocess
        for image in ("chrome.exe", "chromedriver.exe"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/IM", image],
                    capture_output=True, timeout=20, check=False,
                )
            except Exception as error:  # pylint: disable=broad-except
                # `!r` 刻意保留：`try` 裡只有 `subprocess.run(['taskkill', ...])`，
                # 收到的是 `TimeoutExpired` / `OSError`，跟 selenium 無關。
                print(f"orphan-chrome sweep: taskkill {image} failed: "
                      f"{error!r}", file=sys.stderr)
    print(f"orphan-chrome sweep: killed {killed} via psutil"
          + (f", {remaining} survived → taskkill /T fallback"
             if remaining > 0 else "")
          + psutil_note)
    if killed or remaining != 0:
        time.sleep(2.0)  # let the OS release file handles / profile locks + RAM
    return killed


# Snapshot 設計：跟 webrunner_novelai 對稱 — 每次 spawn 前 copy
# .chrome_profile/ → .chrome_profile_snap/、Chrome 用 snapshot 完全避開
# 原 profile 的 lockfile。跑完 _sync_chrome_profile_back 同步登入。
CHROME_PROFILE_SNAPSHOT = PROJECT_ROOT / ".chrome_profile_snap"
_CURRENT_SNAPSHOT_PROFILE: Path | None = None  # set by start_driver, read by main finally
# cookie jar 在 `Default/Network/` 底下（Chrome 96 起），不是 `Default/`。
# 為什麼舊路徑不留相容、以及那四個月的實測代價，見 webrunner_novelai 對應常數。
_SESSION_CRITICAL = (
    "Default/Network/Cookies", "Default/Network/Cookies-journal",
    "Default/Login Data", "Default/Login Data-journal",
    "Default/Preferences",
    "Default/Web Data", "Default/Web Data-journal",
    "Default/Network/Device Bound Sessions",
    "Default/Network/Device Bound Sessions-journal",
    "Local State",
)
# **登入態不在 cookie 裡，在 localStorage。** 完整的實測證據鏈（那個站只有三筆
# cookie、全不是驗證用的；`session` key 在 leveldb；snapshot 有更新的一筆而來源
# 停在 2026-05-20）寫在 webrunner_novelai 對應常數上方。
_SESSION_CRITICAL_DIRS = (
    "Default/Local Storage/leveldb",
)
# LevelDB 目錄裡不帶走的檔名（lock ＋ leveldb 自己的除錯文字輸出）。理由見
# webrunner_novelai 對應常數。
_LEVELDB_SKIP_NAMES = ("LOCK", "LOG", "LOG.old")
_SNAPSHOT_IGNORE_NAMES = frozenset(
    _CHROME_LOCK_FILES + (
        "Cache", "Code Cache", "GPUCache", "Service Worker",
        "ShaderCache", "GraphiteDawnCache", "DawnGraphiteCache",
        "Crashpad",
    )
)


def _clear_snapshot_locks(snapshot: Path) -> None:
    """把 `snapshot/` 裡的 singleton lock 檔案刪掉，best-effort，永不 raise。

    **目標一定要是 Chrome 真正開的那個目錄**（`--user-data-dir` ＝ snapshot），
    不是 `.chrome_profile/`。這裡以前有一整條「檔案鎖復原鏈」
    （`_cleanup_chrome_locks` → `_force_unlink` → `_kill_chrome_holders_of` →
    `_diagnose_file_lock`，兩個變體各一份、有測試、**沒有任何呼叫端**），而它清
    的正是 `.chrome_profile/`——自 snapshot 架構上線以來 Chrome 就沒有再從那裡
    開過，所以那條鏈就算接上去也等於沒接。2026-09-07 移除，證據：

    * `.chrome_profile/` 現在一個 lock 檔都沒有；`.chrome_profile_snap/` 在
      Chrome 活著的時候有一個 `lockfile`。lock 檔只出現在後者。
    * 那個 `lockfile` 是 delete-on-close 的，不可能變成「殘留」——見
      `_CHROME_LOCK_FILES` 上方那段實測。
    * 反過來，持有者還活著的時候那個檔連唯讀開啟都會 `PermissionError`（實測），
      所以再長的刪除階梯在那個情境下也一樣失敗，而且那個情境我們本來就不該刪。
    * 那條鏈最後兩階（psutil 找 handle 持有者 / 印診斷）在這台機器上還是**空轉**
      的：`psutil.Process.open_files()` 對 330 個行程裡的 141 個丟
      `RuntimeError: SystemExtendedHandleInformation buffer too big`，而那個型別
      不在它們的 `except (NoSuchProcess, AccessDenied, OSError)` 裡，會一路掀翻
      整個掃描迴圈；跑完一輪還要超過兩分鐘。

    所以這個呼叫實務上是 no-op。留著的理由是它盯的是**正確的**目錄、成本只有五次
    `exists()`，而且萬一上面的分析哪天在別的平台不成立，這裡是唯一正確的位置。
    刪不掉會印一行（報結果不報嘗試）——那代表有行程還握著它，是真正該看的訊號。
    """
    for fname in _CHROME_LOCK_FILES:
        path = snapshot / fname
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError as error:
            # `!r` 刻意保留：只收得到 `OSError`，而 `str()` 會印出 snapshot
            # profile 的完整主機路徑。
            print(f"snapshot lock `{fname}` 刪不掉（有行程還握著它？）: "
                  f"{error!r}", file=sys.stderr)


def _leveldb_manifest_ok(dirpath: Path) -> bool:
    """`dirpath` 是不是一份自洽的 LevelDB（`CURRENT` 在、它指到的 MANIFEST 也
    在）。缺一個被 manifest 參照的檔案，Chrome 會安靜地丟掉整個 DB。詳見
    webrunner_novelai 對應函式。"""
    try:
        current = (dirpath / "CURRENT").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not current.startswith("MANIFEST-"):
        return False
    # `startswith` 不是包含性檢查（`MANIFEST-` ＋ `../` 就能走出去）；今天無害的
    # 三個理由、以及「402 份真 LevelDB 抽樣 0 份誤擋」的實測，寫在
    # `webrunner_novelai.py` 對應的那一段。
    if not _is_single_path_component(current):
        return False
    return (dirpath / current).is_file()


def _session_entry_present(base: Path, relpath: str) -> bool:
    """`base` 底下這一筆登入資料算不算「在」。目錄那幾筆要求 LevelDB 完整。"""
    if relpath in _SESSION_CRITICAL_DIRS:
        return _leveldb_manifest_ok(base / relpath)
    return (base / relpath).exists()


def _reclaim_dir_sync_residue(dst: Path) -> None:
    """收掉上一次目錄交換的殘骸，並救回「死在兩個 rename 之間」那一刻。詳見
    webrunner_novelai 對應函式。"""
    import shutil
    tmp = dst.with_name(dst.name + ".sync.tmp")
    old = dst.with_name(dst.name + ".sync.old")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    if not old.exists():
        return
    if dst.exists():
        shutil.rmtree(old, ignore_errors=True)
        return
    try:
        os.replace(old, dst)
        print(f"dir-sync: 從 {old.name}/ 救回 {dst.name}/"
              f"（上一輪死在交換的那一刻）", file=sys.stderr)
    except OSError as error:
        # `!r` 刻意保留：只收得到 `OSError`，而 `str()` 會把 profile 的完整
        # 主機路徑帶進 log。
        print(f"dir-sync: {old.name}/ 救不回 {dst.name}/：{error!r}。"
              f"這一輪的 localStorage 是空的 → 要重新登入。", file=sys.stderr)


def _sync_profile_dir_back(snapshot: Path, relpath: str) -> tuple[bool, int]:
    """把 snapshot 裡的一整個 LevelDB 目錄寫回 `.chrome_profile/`。
    回 `(有沒有換過去, 帶了幾個檔案)`，永不 raise。

    為什麼要整組換而不能逐檔覆蓋、目錄級原子性的取捨怎麼選、為什麼一定要在
    `driver.quit()` 之後跑、為什麼不做選擇性複製——四段理由都寫在
    webrunner_novelai 對應函式的 docstring 裡。"""
    import shutil
    src = snapshot / relpath
    dst = CHROME_PROFILE_DIR / relpath
    _reclaim_dir_sync_residue(dst)
    if not src.is_dir():
        return False, 0
    tmp = dst.with_name(dst.name + ".sync.tmp")
    old = dst.with_name(dst.name + ".sync.old")
    copied = 0
    try:
        tmp.mkdir(parents=True)
        for entry in sorted(src.iterdir()):
            if entry.name in _LEVELDB_SKIP_NAMES or not entry.is_file():
                continue
            shutil.copy2(entry, tmp / entry.name)
            copied += 1
        if not _leveldb_manifest_ok(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"dir-sync {relpath}: 組出來的 LevelDB 不完整（帶了 {copied} "
                  f"個檔案，CURRENT 指到的 MANIFEST 不在）→ 不換，保留舊的。"
                  f"下一輪要重新登入。", file=sys.stderr)
            return False, copied
        if dst.exists():
            os.replace(dst, old)
        os.replace(tmp, dst)
        shutil.rmtree(old, ignore_errors=True)
        return True, copied
    except (OSError, shutil.Error) as error:
        # `!r` 刻意保留：`shutil.Error` 也是 `OSError` 的子類別，這裡收不到
        # selenium 例外；`str()` 會印出 profile 的完整主機路徑。
        print(f"dir-sync {relpath} failed: {error!r}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        _reclaim_dir_sync_residue(dst)
        return False, copied


def _snapshot_chrome_profile() -> Path:
    """Copy .chrome_profile/ → .chrome_profile_snap/、跳過 lock 檔跟 cache
    dir。詳見 webrunner_novelai 對應函式。"""
    import shutil
    # 先砍舊 snapshot
    if CHROME_PROFILE_SNAPSHOT.exists():
        try:
            shutil.rmtree(CHROME_PROFILE_SNAPSHOT, ignore_errors=True)
        except OSError:
            pass
    CHROME_PROFILE_SNAPSHOT.mkdir(parents=True, exist_ok=True)
    if not CHROME_PROFILE_DIR.exists():
        print(f"snapshot profile: 來源 {CHROME_PROFILE_DIR.name}/ 不存在"
              f"（首次執行？）→ snapshot 是空的，這一輪要重新登入")
        return CHROME_PROFILE_SNAPSHOT
    # 一定要在 walk 之前：上一輪若死在目錄交換正中間，來源的 `leveldb/` 會不見、
    # 資料在 `leveldb.sync.old/`。不先救回來就會複製一份沒有 localStorage 的
    # profile 過去，而且看起來一切正常。
    for relpath in _SESSION_CRITICAL_DIRS:
        _reclaim_dir_sync_residue(CHROME_PROFILE_DIR / relpath)
    copied = 0
    skipped: list[str] = []
    try:
        for root, dirs, files in os.walk(CHROME_PROFILE_DIR):
            rel_root = Path(root).relative_to(CHROME_PROFILE_DIR)
            dirs[:] = [d for d in dirs if d not in _SNAPSHOT_IGNORE_NAMES]
            dst_root = CHROME_PROFILE_SNAPSHOT / rel_root
            dst_root.mkdir(parents=True, exist_ok=True)
            for fname in files:
                if fname in _SNAPSHOT_IGNORE_NAMES:
                    continue
                src = Path(root) / fname
                dst = dst_root / fname
                try:
                    shutil.copy2(src, dst)
                    copied += 1
                except (OSError, shutil.Error) as error:
                    # `!r` 刻意保留：這一行會被收進 `skipped` 再印出來，`str()`
                    # 會讓每一筆都帶上完整主機路徑。
                    skipped.append(f"{rel_root}/{fname}: {error!r}")
    except Exception as error:  # pylint: disable=broad-except
        # `!r` 刻意保留：`try` 裡只有 `os.walk` ＋ `shutil.copy2`，碰不到 driver
        # （這一步跑在 Chrome 起來**之前**）；`str(OSError)` 會印出完整路徑。
        print(f"_snapshot_chrome_profile walk failed: {error!r}",
              file=sys.stderr)
    # **無條件報結果。** 摘要原本包在 `if copied:` 裡，於是最嚴重的結果
    # （一個檔案都沒複製到 → Chrome 用一份空 profile 開機 → 這一輪必然重新
    # 登入）剛好是唯一一個什麼都不印的。
    print(f"snapshot profile: copied {copied} files to "
          f"{CHROME_PROFILE_SNAPSHOT.name}/"
          + (f"; skipped {len(skipped)} (locked / inaccessible)"
             if skipped else ""))
    # 真正可行動的訊號：**snapshot 裡找不到**的 session-critical 檔案。判準刻意
    # 不要求來源存在——一個要求來源存在的檢查抓不到路徑寫錯（實測見
    # webrunner_novelai 對應函式）。來源也沒有的那幾筆會被標出來。
    # 目錄那幾筆的判準是「完整」不是「在」——`CURRENT` 指不到 MANIFEST 的目錄
    # 存在，但 Chrome 會把整個 DB 丟掉。
    entries = _SESSION_CRITICAL + _SESSION_CRITICAL_DIRS
    missing = [rel for rel in entries
               if not _session_entry_present(CHROME_PROFILE_SNAPSHOT, rel)]
    if missing:
        detail = ", ".join(
            rel if _session_entry_present(CHROME_PROFILE_DIR, rel)
            else f"{rel}（來源也沒有）"
            for rel in missing)
        print(f"snapshot profile: {len(missing)}/{len(entries)} 個登入"
              f"相關檔案不在 snapshot 裡 → 這一輪很可能要重新登入: {detail}",
              file=sys.stderr)
    return CHROME_PROFILE_SNAPSHOT


def _sync_chrome_profile_back(snapshot: Path) -> None:
    """Webrunner 結束時呼叫：把 snapshot 內的 session-critical 檔案
    複製回 .chrome_profile/，保留登入。詳見 webrunner_novelai 對應函式——
    摘要為什麼一定要帶分母、而且無條件印，以及為什麼單檔與目錄的分母刻意分成
    兩個不合併，都寫在那一邊的 docstring。"""
    import shutil
    if not snapshot.exists():
        print(f"sync-back: snapshot {snapshot.name}/ 不存在 → "
              f"0/{len(_SESSION_CRITICAL)} 個登入相關檔案 + "
              f"0/{len(_SESSION_CRITICAL_DIRS)} 個登入相關目錄寫回，"
              f"下一輪要重新登入", file=sys.stderr)
        return
    synced: list[str] = []
    absent: list[str] = []
    for relpath in _SESSION_CRITICAL:
        src = snapshot / relpath
        if not src.exists():
            absent.append(relpath)
            continue
        dst = CHROME_PROFILE_DIR / relpath
        # **同目錄 temp → os.replace。** `shutil.copy2` 是「開目標為 wb 立刻
        # 截斷」，所以中途被砍（/stop、taskkill /F、當機、機器睡著）留在磁碟上
        # 的是**半個 Cookies**——下一輪 Chrome 不會報錯，它會安靜地以未登入狀態
        # 開機，然後整輪重新登入。CLAUDE.md 的跨行程原子寫入硬規則講的「不是
        # 崩潰而是安靜的錯誤結果」就是這個；範本見 `_run_progress._atomic_write`。
        tmp = dst.with_name(dst.name + ".sync.tmp")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            synced.append(relpath)
        except OSError as error:
            # `!r` 刻意保留：只收得到 `OSError`，而 `str()` 會印出登入 profile
            # 的完整主機路徑。
            print(f"sync-back {relpath} failed: {error!r}",
                  file=sys.stderr)
            # os.replace 成功會自己移除 temp，失敗才要善後——留著就是一份登入
            # 資料的殘骸躺在磁碟上。
            try:
                tmp.unlink()
            except OSError:
                pass
    dirs_ok = 0
    dir_notes: list[str] = []
    for relpath in _SESSION_CRITICAL_DIRS:
        swapped, files = _sync_profile_dir_back(snapshot, relpath)
        dirs_ok += 1 if swapped else 0
        dir_notes.append(f"{relpath}: {files} files"
                         + ("" if swapped else " NOT swapped"))
    print(f"synced {len(synced)}/{len(_SESSION_CRITICAL)} session files "
          f"+ {dirs_ok}/{len(_SESSION_CRITICAL_DIRS)} session dirs "
          f"[{'; '.join(dir_notes)}] back to {CHROME_PROFILE_DIR.name}/")
    if absent:
        print(f"sync-back: {len(absent)} 個登入相關檔案在 snapshot 裡找不到，"
              f"沒有寫回去（路徑寫錯？Chrome 沒有建出來？）: "
              f"{', '.join(absent)}", file=sys.stderr)


def start_driver() -> None:
    """Boot Chrome via WebDriverWrapper with full anti-bot stealth + snapshot
    profile（避開 .chrome_profile/ 可能被 Defender / OneDrive / Explorer
    抓住的 lockfile）。

    **chromedriver 的記錄檔在這一側也會產生（2026-09-09 接上）。** 在那之前這個
    變體完全沒有它，於是 spawn 失敗只留下本函式結尾那句猜測；而 `/run` 的預設變體
    就是 je，`_watch_for_fallback` 又會在 5 分鐘的 startup window 內靜靜轉跑
    selenium 變體，所以「je 為什麼起不來」在正式環境是**查不到**的。

    做得到的關鍵是 `wr.set_driver` 把 `**kwargs` **原封不動**轉給
    `webdriver.Chrome(...)`（`webdriver_wrapper.py`：
    `webdriver_value(options=driver_options, **kwargs)`），所以傳一個
    `service=ChromeService(log_output=…)` 就會變成 chromedriver 的
    `--log-path=`。sibling clone 與 site-packages 兩份都實測過，守門在
    `test_je_facade.py`——**上游哪天把 `**kwargs` 收掉，那支會紅**，而不是等到某次
    無人值守的 spawn 失敗才發現記錄又是空的。

    順序與 selenium 變體相同、理由也相同（見 `_trim_chromedriver_log`）：封頂與
    改名都必須在 `ChromeService(...)` **之前**，那之後那個檔就有 chromedriver
    握著，中途截斷會被作業系統補零。
    """
    from selenium.webdriver.chrome.service import Service as ChromeService

    _trim_chromedriver_log()
    _rotate_chromedriver_log()
    snapshot = _snapshot_chrome_profile()
    _clear_snapshot_locks(snapshot)

    # Memory-conservative flags — see webrunner_novelai._MEMORY_FLAGS for the
    # rationale (cap caches + keep the minimized renderer active to reduce the
    # "Aw, Snap! Out of Memory" crash on long runs). GPU left ON so NovelAI's
    # canvas still paints (image-src detection depends on it).
    cli_args = [
        "--disable-blink-features=AutomationControlled",
        # 開窗就在螢幕外、固定大小；**不要**換回 `--start-maximized`（理由見
        # `_webrunner_shared.OFFSCREEN_WINDOW_POSITION`）。與 selenium 變體同一組。
        "--window-position=-32000,-32000",
        "--window-size=1920,1080",
        "--lang=en-US",
        f"--user-agent={REAL_USER_AGENT}",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--disk-cache-size=33554432",
        "--media-cache-size=33554432",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--disable-background-timer-throttling",
        f"--user-data-dir={snapshot}",
    ]
    experimental = {
        "excludeSwitches": ["enable-automation"],
        "useAutomationExtension": False,
    }

    # Up to 3 attempts. `_kill_orphan_chrome()` between attempts frees the
    # RAM held by a half-spawned chrome.exe — on a low-RAM box that leftover
    # is what makes the retry OOM too ("retries until crash").
    max_attempts = 3
    last_err: Exception | None = None
    ok = False
    for attempt in range(1, max_attempts + 1):
        # `log_output=`，**不是** `log_path=`。selenium 的 `Service` 沒有
        # `log_path` 這個參數，傳進去會一路掉進 `**kwargs` 被最底層安靜丟掉——
        # 不警告、不報錯，記錄檔從此不存在。這個坑在 selenium 變體上真的踩過幾個
        # 月（見 `test_selenium_facade.py` 的模組 docstring），所以兩個變體的
        # `ChromeService(...)` 關鍵字都由那支測試對著**裝著的** selenium 簽名比對。
        # `--log-level=INFO` 而不是 `--verbose`：實測 root cause 一字不差，但平時
        # 的雜訊少九成（4370 → 484 bytes/命令）。
        #
        # `ws._CHROMEDRIVER_LOG` 用屬性讀取而不是綁一份同名常數進本模組——理由見
        # 檔頭那段 import 的註解。
        service = ChromeService(
            log_output=str(ws._CHROMEDRIVER_LOG),
            service_args=["--log-level=INFO"],
        )
        try:
            wr.set_driver("chrome", options=cli_args,
                          experimental_options=experimental,
                          service=service)
            ok = True
            break
        except Exception as err:  # pylint: disable=broad-except
            last_err = err
            # je_web_runner 包裝層可能吞掉底層訊息；連 __cause__ 一起印，
            # 才看得到真正的 selenium / webdriver_manager 錯誤。
            # ⚠️ `__cause__` 是**最可能**放著真的 selenium 例外的位置（包裝層在
            # 外面再包一層），所以這裡用 `{...!r}` 的代價比外層那個還大。
            cause = (f" (cause: {full_error_detail(err.__cause__)})"
                     if err.__cause__ else "")
            print(f"Chrome spawn attempt {attempt}/{max_attempts} failed: "
                  f"{full_error_detail(err)}{cause}", file=sys.stderr)
            # 包裝層那句 `set_driver failed: …` 同樣沒有 root cause。傾印必須在
            # 下一次嘗試**之前**——chromedriver 每次啟動會清空 `--log-path` 指的
            # 檔，所以這一次的內容在 attempt+1 建立 service 時就沒了。
            _dump_chromedriver_log_tail()
            if attempt < max_attempts:
                _kill_orphan_chrome()
                time.sleep(2.0)
                snapshot = _snapshot_chrome_profile()
                # selenium 變體的重試路徑一直有這一步，je 這邊漏掉了——
                # 2026-09-07 補上，讓兩個變體的重試行為一致。
                _clear_snapshot_locks(snapshot)
                cli_args[-1] = f"--user-data-dir={snapshot}"
    if not ok:
        raise RuntimeError(
            f"Chrome failed to start after {max_attempts} attempts "
            f"(likely Out of Memory or a chromedriver/Chrome version "
            f"mismatch — see chromedriver.log above). Last error: "
            f"{full_error_detail(last_err)}"
        ) from last_err
    # 把 snapshot 路徑紀錄到 module 層級，main() 結束時拿來 sync back。
    global _CURRENT_SNAPSHOT_PROFILE
    _CURRENT_SNAPSHOT_PROFILE = snapshot

    # 與 selenium 變體同步：每次 spawn 記一行版本。`current_webdriver` 是包裝層
    # 底下那個真的 selenium driver。
    try:
        ws.log_driver_versions(wr.current_webdriver.capabilities)
    except Exception as error:  # pylint: disable=broad-except
        print(f"driver version log failed: {full_error_detail(error)}",
              file=sys.stderr)

    # Pre-document stealth — runs before any page script on every new document.
    try:
        wr.add_script_to_evaluate_on_new_document(STEALTH_JS)
    except Exception as error:  # pylint: disable=broad-except
        print("add_script_to_evaluate_on_new_document failed: "
              f"{full_error_detail(error)}", file=sys.stderr)


# ---------- login ------------------------------------------------------------


def login_if_needed(email: str, password: str) -> bool:
    """Visit /login; if NovelAI redirects us elsewhere, we're already signed in.

    **判準是「`/login` 會不會轉址」這個結構性訊號，不是頁面上的字。**
    這裡曾經有一個對照組 `_webrunner_shared.is_logged_in`：它
    `return port.execute_script(...)`，JS 掃 `document.body.innerText` 找
    `Anlas:` / `End Session`（有就算已登入）與 `Anonymous Trial` / `Sign Up`
    （有就算沒登入）。2026-09-08 刪除，三個理由都記在這裡——**任何想用頁面文字
    判狀態的新程式都會踩到同一組**：

    1. **裸的 `execute_script` 回傳值不可以直接當布林。** 頁面切換中或記憶體吃緊
       時它真的會回 `None`（不是丟例外），`None` 讀成 False ＝「沒登入」，於是
       觸發一次多餘的完整登入流程。**je 那一側更絕對**：
       `webdriver_wrapper.execute_script` 吞掉所有 driver 例外並回 `None`，所以
       瀏覽器一死就必定判成「沒登入」。同一個形狀在 `set_variety_plus` /
       `_has_rescale` 也有。
    2. **失敗的探測長得跟乾淨的探測一模一樣。** 三態（是／否／判斷不出來）被壓成
       兩態，呼叫端沒有辦法分辨「站方說沒登入」與「我根本讀不到」。本專案已經在
       `_find_all_chrome_processes` / `_load_pid` / `dashboard_server._read_pid`
       各踩過一次。
    3. **寫死的站方字面會過期，而且是靜默過期。** 那四個字面是 2026-05-20 寫的，
       之後沒有再對過實機。手上唯一的實機文字證據是 `WEBRunner.log` 裡額度對話框
       的 `innerText` 傾印（08-24～09-08）：`Anonymous Trial` / `Sign Up` /
       `End Session` **各 0 次**，`Anlas` 出現 472 次但**一次都沒有帶冒號**。
       那份傾印只涵蓋對話框、不是整頁，所以這不構成「字面已經失效」的證明——但它
       也完全沒有支持那四個字面還活著，而其中一個正面證據（`End Session`）依它
       自己的註解是在**帳號選單**裡，收合的選單通常不會進 `innerText`。寫死字面
       靜默失效在這個專案有前科：同意橫幅那顆 `Reject All` 站方根本沒有這個字面，
       從來沒命中過一次，是靠寬鬆退路才看起來正常的。

    轉址判準沒有這三個問題：它問的是**導覽結果**不是頁面上的字，讀不到會丟例外而
    不是安靜回 False，站方改文案也不影響它。要新增登入狀態檢查請沿用它，不要再寫
    一個掃 `innerText` 的版本。
    """
    port.get(LOGIN_URL)
    human_pause(3.0, 4.5)
    if "/login" not in port.current_url():
        print(f"session restored — redirected to {port.current_url()}")
        return True
    print("no session — going through /login flow")
    return _do_login(email, password)


def _do_login(email: str, password: str) -> bool:
    if "/login" not in port.current_url():
        port.get(LOGIN_URL)
        human_pause(1.5, 2.5)
    ws.reject_cookies(port, timeout=5.0)
    if not wait_until(
            lambda: port.find_element_xpath("//input[@type='email' or @name='email']") is not None,
            timeout=30, description="email field"):
        ws.snap(port, "no_email_field")
        return False
    email_box = port.find_element_xpath("//input[@type='email' or @name='email']")
    if email_box is None:
        ws.snap(port, "no_email_field")
        return False
    email_box.click()
    human_pause(0.15, 0.4)
    human_type(email_box, email)
    human_pause(0.3, 0.7)

    pw_box = port.find_element_xpath("//input[@type='password']")
    if pw_box is None:
        ws.snap(port, "no_password_field")
        return False
    pw_box.click()
    human_pause(0.15, 0.4)
    human_type(pw_box, password)
    human_pause(0.4, 0.9)

    submit_btn = port.execute_script(
        """
        for (const inp of document.querySelectorAll(
                "input[type='submit'], button[type='submit']")) {
          if (inp.offsetParent === null) continue;
          if (inp.closest('form')) return inp;
        }
        return null;
        """
    )
    if submit_btn:
        port.execute_script("arguments[0].scrollIntoView({block:'center'});", submit_btn)
        port.click(submit_btn)
    else:
        port.press_enter(pw_box)
    if wait_until(lambda: "/login" not in port.current_url(),
                  timeout=30, description="login redirect"):
        return True
    ws.snap(port, "login_failed")
    return False


# ---------- model + cookies + prompt areas ----------------------------------


# ---------- prompt fill helpers ---------------------------------------------


def click_add_character(gender: str = "Female", timeout: float = 8.0) -> bool:
    """加一個角色欄位。實作在共用層（`ws.click_add_character_control`），兩個
    webrunner 變體共用同一份 —— V5 把「Add Character」按鈕換成沒有文字、沒有
    aria-label 的圖示鈕，這種會跟著站方改版的邏輯只該有一份。"""
    return ws.click_add_character_control(port, gender, timeout=timeout)


def ensure_two_characters() -> bool:
    have = ws.count_characters(port)
    print(f"existing character slots: {have}")
    safety = 0
    while have > 2 and safety < 20:
        safety += 1
        label = f"Character {have}"
        ok = ws.remove_character_slot(port, label)
        human_pause(0.5, 0.9)
        new_count = ws.count_characters(port)
        if not ok or new_count >= have:
            print(f"  could not remove {label} (delete clicked={ok}, count={new_count})")
            break
        have = new_count
        print(f"  removed {label}; now {have}")
    while have < 2:
        click_add_character(gender="Female")
        human_pause(0.8, 1.2)
        new_count = ws.count_characters(port)
        if new_count == have:
            print("  Add Character clicked but no new slot appeared; breaking")
            break
        have = new_count
        print(f"  added; now {have} slot(s)")
    return have == 2


# ---------- resolution + sampler --------------------------------------------


# ---------- generation + download -------------------------------------------


def _setup_session(email: str, password: str) -> bool:
    """Full pre-generation setup (login → model → characters → resolution →
    sampler → minimize) on the freshly-booted module-global `wr` driver.
    Returns False on login failure. Used for the initial session AND every
    mid-run Chrome restart, so it fills no per-pair prompts (caller does)."""
    if not with_retry("login", lambda: login_if_needed(email, password),
                      max_attempts=3, sleep_range=(5, 10)):
        return False
    print("login ok")
    if not port.current_url().startswith(IMAGE_URL):
        port.get(IMAGE_URL)
        wait_until(lambda: port.current_url().startswith(IMAGE_URL),
                   timeout=30, description="navigate to /image")
    human_pause(2.0, 3.5)
    ws.reject_cookies(port)
    if not with_retry("select_model",
                      lambda: ws.select_model(
                          port, ws.load_batch_config()["model_candidates"]),
                      max_attempts=3, sleep_range=(2, 4)):
        return False
    if not ensure_two_characters():
        print("character slot setup failed")
        return False
    if not with_retry("select_resolution",
                      lambda: ws.select_resolution(port, "Normal", "Landscape"),
                      max_attempts=3, sleep_range=(2, 4)):
        return False
    if not ws.configure_sampler_settings(port):
        print("sampler setup failed")
        return False
    ws.snap(port, "after_setup")
    # Catch a renderer crash during setup before entering / resuming the loop.
    ws._abort_if_chrome_crashed(port, "setup")
    if hide_chrome_window():
        print("browser window is off-screen; generation runs in the background")
    else:
        print("WARN: hide_chrome_window could not confirm the browser window is "
              "off-screen (continuing anyway)")
    return True


def _restart_chrome_session(email: str, password: str) -> None:
    """Quit Chrome + relaunch a fresh, fully-set-up session to flush the
    renderer's leaked memory (NovelAI's SPA grows unbounded across hundreds
    of generations → "Aw, Snap! Out of Memory"). Syncs the login profile
    back, quits, reaps orphans, re-runs `start_driver` + `_setup_session`
    (rebinding the module-global `wr` driver). Raises RuntimeError if the
    fresh session can't log in / set up. Caller re-fills the current pair's
    prompts afterwards."""
    snap_dir = _CURRENT_SNAPSHOT_PROFILE
    try:
        wr.quit()
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    if snap_dir is not None:
        try:
            _sync_chrome_profile_back(snap_dir)
        except Exception as error:  # pylint: disable=broad-except
            # `!r` 刻意保留。⚠️ 這一站曾被列為「該換格式器」的候選，但 `try` 裡
            # 只有 `_sync_chrome_profile_back(...)` ＝ 純檔案 I/O：Chrome 在上面
            # 幾行已經 `quit()` 了，這裡碰不到 driver。而 `str(OSError)` 會把
            # 登入 profile 的完整主機路徑寫進 log。
            print(f"  sync-back during restart failed: {error!r}",
                  file=sys.stderr)
    _kill_orphan_chrome()
    start_driver()  # rebinds the wr driver + resets _CURRENT_SNAPSHOT_PROFILE
    if not _setup_session(email, password):
        raise RuntimeError("re-login failed after Chrome restart")
    print("  [memory] Chrome restarted, re-set-up, ready to resume")


# ---------- main (thin shell — orchestration lives in ws.run_batch, P6 C6) --

def main() -> int:
    print("===== webrunner_je_only — pure je_web_runner driver =====")
    # 啟動當下的程式碼指紋，凍在這裡（import 都跑完了）。這一行是事後判讀
    # traceback 原始碼文字、以及回答「那個修正在這個行程裡生效了沒」的錨點——
    # 見 `ws.log_code_fingerprint`。必須在這裡呼叫、不能等要查時才算。
    ws.log_code_fingerprint()
    try:
        email, password = read_credentials(AUTH_FILE)
    except FileNotFoundError:
        # 「檔案不在」與「檔案在但內容不對」在 `read_credentials` 裡是刻意分開的
        # 兩件事（見 `ws.CredentialsError`），這裡兩種都收，因為對**使用者**來說
        # 處置一樣：去把憑證填好。回 `RC_SETUP_INCOMPLETE` 讓監督者直接收工——
        # 缺檔案重生一百次也不會變成功。
        print(ws.missing_credentials_message(AUTH_FILE), file=sys.stderr)
        return ws.RC_SETUP_INCOMPLETE
    except ws.CredentialsError as error:
        print(f"webrunner: {error}", file=sys.stderr)
        return ws.RC_SETUP_INCOMPLETE
    # 這一輪是批次還是單圖伺服器，由 spawn 端在 argv 上**宣告**（規則與 2026-06-27
    # 的事故紀錄見 ws.RUN_MODE_BATCH 上面那一段）。不要改回從磁碟上的請求檔推論
    # ——那個檔回答的是「有沒有人排了一張單圖」，不是「我這個行程是誰」。
    mode = ws.parse_run_mode(sys.argv)
    # Preflight BEFORE Chrome boot: nothing to do (and this run was not spawned
    # as a single-image server) → rc=1 without spawning Chrome (preserves the
    # old "don't boot for empty"). 空佇列 ＋ 磁碟上躺著一個舊請求檔，現在不會
    # 再讓我們白開一次 Chrome。
    if not ws.run_preflight(mode == ws.RUN_MODE_SINGLE_IMAGE_SERVER):
        return 1
    # Reap orphan Chrome from any prior crashed / killed run before we spawn
    # our own (the "舊實例" spiral — see _kill_orphan_chrome).
    _kill_orphan_chrome()
    try:
        start_driver()
    except Exception as error:  # pylint: disable=broad-except
        # `full_error_detail`：這是 spawn 路徑的終結性回報（下一行就 return 1）。
        # 今天 `start_driver` 把三次嘗試都失敗的情況轉成 `RuntimeError`，那種例外
        # `args` 非空、`!r` 讀得到訊息，所以現況不是活著的缺陷——但那是
        # `start_driver` **內部**的轉換，不是這裡的保證：迴圈外面任何一個
        # `wr.*` 呼叫漏出一個 selenium 例外，`!r` 就會把訊息整段丟掉。
        print(f"set_driver failed: {full_error_detail(error)}", file=sys.stderr)
        return 1
    try:
        return ws.run_batch(
            port, email, password,
            setup_fn=lambda: _setup_session(email, password),
            minimize_fn=hide_chrome_window,
            mode=mode)
    finally:
        try:
            wr.quit()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        # quit 後 chrome 釋放 snapshot 的 file handle，再 sync back 登入。
        if _CURRENT_SNAPSHOT_PROFILE is not None:
            try:
                _sync_chrome_profile_back(_CURRENT_SNAPSHOT_PROFILE)
            except Exception as error:  # pylint: disable=broad-except
                # `!r` 刻意保留，理由同 `_restart_chrome_session` 裡那一站：
                # 這是 `finally` 的收尾，driver 已經 `quit()`，`try` 裡只剩檔案
                # I/O，而 `str(OSError)` 會帶出登入 profile 的完整路徑。
                print(f"_sync_chrome_profile_back failed: {error!r}",
                      file=sys.stderr)


if __name__ == "__main__":
    # 裸跑時沒有父行程發布存活訊號——見 `ws.claim_liveness_signal`。
    sys.exit(ws.run_with_liveness_signal(main))
