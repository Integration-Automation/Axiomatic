"""
Drive NovelAI image generation via WebRunner with light anti-bot evasion.

Flow:
1. Read credentials from ``auth.md``.
2. Open https://novelai.net/login, type like a human, sign in.
3. Open https://novelai.net/image.
4. Dismiss the cookie banner (Reject All).
5. Switch model to ``NAI Diffusion V5 Full``.
6. Fill the main prompt from ``prompt.md``.
7. Click ``Add Character`` twice, fill ``character1.md`` / ``character2.md``.
8. Click ``Generate`` and download images.

Multi-character loop:
- ``todo_character1.md`` holds one or more character prompts joined by the full-width
  comma ``，``.
- For each entry: set as character-1 prompt (replacing whatever was there), then
  generate 240 images, waiting 20-30 seconds between requests, and saving the
  result PNG into ``output/<character_name>/``.

Run from the repo root:

    py -3 axiomatic/webrunner_novelai.py
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

# WebRunner isn't on PyPI; we import it from a sibling directory or an explicit
# path supplied via the WEBRUNNER_PATH environment variable. Default layout:
#   <parent>/axiomatic   (this repo)
#   <parent>/WebRunner     (sibling clone)
_WR_ENV = os.environ.get("WEBRUNNER_PATH")
_WR_SIBLING = Path(__file__).resolve().parent.parent.parent / "WebRunner"
WEBRUNNER_PATH = Path(_WR_ENV) if _WR_ENV else _WR_SIBLING
if str(WEBRUNNER_PATH) not in sys.path:
    sys.path.insert(0, str(WEBRUNNER_PATH))

from selenium import webdriver  # noqa: E402
from selenium.common.exceptions import WebDriverException  # noqa: E402
from selenium.webdriver.chrome.options import Options as ChromeOptions  # noqa: E402
from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support import expected_conditions as EC  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402
from urllib3.exceptions import (  # noqa: E402
    ConnectTimeoutError as Urllib3ConnectTimeoutError,
    MaxRetryError as Urllib3MaxRetryError,
    NewConnectionError as Urllib3NewConnectionError,
    ProtocolError as Urllib3ProtocolError,
    ReadTimeoutError as Urllib3ReadTimeoutError,
)

from _batch_config import load_batch_config  # noqa: E402
# Driver-agnostic helpers shared by both webrunner variants (P6). Imported by
# name so existing call sites stay byte-identical; `ws.*` reaches the rest
# (the bulk now lives in the shared module after C1–C6).
import _webrunner_shared as ws  # noqa: E402
from _webrunner_shared import (  # noqa: E402
    # ⚠️ **裸名字匯入不是風格選擇。** `test_bot_helpers` 的接合守門認守衛的條件是
    # `isinstance(call.func, ast.Name)`，所以 `ws._is_single_path_component(x)`
    # 這個寫法它**認不得**——實測過：同一道檢查改成 `ws.` 前綴，`dirpath / current`
    # 立刻退回「沒有任何紀律守住」的站點。
    _is_single_path_component,
    human_pause,
    hide_browser_windows,
    human_type,
    read_credentials,
    with_retry,
)
# chromedriver 記錄檔的處理（封頂／保留上一份／spawn 失敗時傾印尾段）。一份放在
# 共用模組、兩個變體都用同一份——理由與實測度量寫在那邊的區段抬頭。**只匯入函式，
# 不匯入 `_CHROMEDRIVER_LOG*` 常數**：測試會把記錄路徑 monkeypatch 到 tmp 檔，
# 常數留在共用模組那一份，patch 的目標若哪天對不上就會立刻 AttributeError，而不是
# 安靜地讓修剪打到 repo 根目錄那個**正在被活著的 chromedriver 握著**的真檔。
from _webrunner_shared import (  # noqa: E402
    _dump_chromedriver_log_tail,
    _rotate_chromedriver_log,
    _trim_chromedriver_log,
)
# spawn 失敗時把例外壓成 `型別: 完整訊息`。**不可以用 `{err!r}`**——selenium 的
# 例外把訊息放在 `msg`、`args` 是空的，`repr()` 只印得出一對空括號，整段診斷
# 就沒了（這一行原本就是這樣丟掉一次真實鑑識的，理由與實測寫在那支的
# docstring）。一份放共用模組、兩個變體共用，正是因為這個缺陷當初是**兩份各寫
# 一次**才會同時存在於兩個變體。
from _webrunner_shared import full_error_detail  # noqa: E402

# Transport-level exceptions that bubble up when the chromedriver conversation
# itself fails (Chrome page hung, GPU stuck, network call inside
# execute_script blocking — or chromedriver.exe simply gone). Selenium does
# NOT wrap urllib3's exceptions; they propagate raw. Hot-path helpers wrap
# their `execute_script` in this tuple so callers' own poll / retry loops can
# recover instead of crashing the whole webrunner.
#
# **2026-09-09：這個 tuple 少了 urllib3 的連線類例外，實測代價很高。**
# `_webrunner_shared._SESSION_GONE_EXC_NAMES` 一直列著 `MaxRetryError` /
# `NewConnectionError`，但它們的 MRO 是 `RequestError → PoolError → HTTPError`
# 與 `ConnectTimeoutError → TimeoutError → HTTPError`——**兩條都不經過
# `OSError`**（urllib3 2.7.0 實測 `issubclass` 全 False）。於是那兩個名字寫在
# 分類表裡、分類程式卻永遠拿不到它們：`except port.TRANSPORT_ERRORS` 收不到 →
# `_note_transport_error` 不會被呼叫 → 沒有 `BrowserGoneError` 分類。正式環境的
# 樣子是 09-07 那三次（03:02 / 06:18 / 11:44）：`critical_error` 的 message 是
# 生的 `MaxRetryError: HTTPConnectionPool(host='localhost', …) Max retries
# exceeded`，沒有 `browser session gone during …` 前綴、沒有 `where` 標籤。
#
# **新加進來的每一個類別，都同時列在 `_SESSION_GONE_EXC_NAMES` 裡**——這是刻意
# 的不變式，也是這個改動沒有退步的理由：新被抓到的例外會立刻被
# `_note_transport_error` 升級成 `BrowserGoneError` 往上丟，而不是被當成暫時性
# 卡頓吸收掉、讓呼叫端對著一個已死的 driver 空轉到 `consecutive_fail_abort`
# （實測約 30 分鐘）。`test_variant_parity.py` 兩個方向都釘住了。
#
# 判準（不是名單，是機制）：urllib3 例外到得了我們手上，代表與 chromedriver 的
# HTTP 對話失敗。`ReadTimeoutError` ＝ 連上了、請求送出了、120 秒沒回話 ＝
# **chromedriver 還活著**，所以維持「暫時性卡頓」；其餘連線類（連不上、連線被
# 拒、連線中途斷掉）對一個 loopback 監聽器而言就是「那個埠上沒有東西在聽」＝
# chromedriver.exe 已經結束。
# 刻意**不**改成整個 `urllib3.exceptions.HTTPError` 基底類別：那會一併吸收掉
# `SSLError` / `DecodeError` / `IncompleteRead` 這些沒有列進 gone 名單的型別，
# 它們今天是快速失敗（rc → 監督者重生），改完會變成 30 分鐘空轉——正好是上面那條
# 不變式要防的退步。
_DRIVER_TRANSPORT_ERRORS = (
    WebDriverException,
    Urllib3ReadTimeoutError,        # 連上了但沒回話 → 卡頓（**不**算 gone）
    Urllib3MaxRetryError,           # 重試用盡（實測就是這一個）
    Urllib3NewConnectionError,      # 連不上（ConnectTimeoutError 的子類）
    Urllib3ConnectTimeoutError,     # 連線階段逾時
    Urllib3ProtocolError,           # 連線中途被對方斷掉
    OSError,                        # socket 層（ConnectionRefusedError…）
)

from je_web_runner import webdriver_wrapper_instance  # noqa: E402


# ---------- BrowserPort adapter (P6 C2) -------------------------------------
# DOM/driver indirection layer. Every helper reaches the browser through this
# single surface instead of touching the raw `driver` directly, so C3+ can
# lift the helpers into the shared module unchanged. Wraps the raw
# `webdriver.Chrome`; `driver` is mutable (`set_driver`) so a mid-run Chrome
# restart can re-point the SAME port at the freshly-built driver (the raw
# driver object is replaced on restart — unlike the je variant whose `wr`
# singleton is stable). Mirrors `webrunner_je_only.BrowserPort`
# method-for-method; the only deltas are the Selenium call style and `click`
# (ActionChains hover→click + JS fallback — the anti-bot pacing, NOT a plain
# click) / `mouse_wiggle` (a real ActionChains wiggle). Lifecycle methods
# (restart/quit/sync_back) are deliberately NOT on the port yet — those stay
# raw `driver.*` for now (deferred to C6); `set_driver` is the one minimal
# hook needed so the port follows the existing restart.

class BrowserPort:
    """Selenium-backed DOM/driver adapter. See module comment above."""

    # Transport-level exception tuple, exposed so shared hot-path readers can
    # `except port.TRANSPORT_ERRORS` without importing selenium / urllib3.
    TRANSPORT_ERRORS = _DRIVER_TRANSPORT_ERRORS

    def __init__(self, driver=None):
        self.driver = driver

    def set_driver(self, driver) -> None:
        """Re-point the port at a (re)created driver. Called from
        `build_stealth_driver` so the initial build AND every mid-run restart
        keep the port pointing at the live driver."""
        self.driver = driver

    def restart(self, email, password) -> None:
        """Mid-run Chrome restart (memory flush). Delegates to the per-variant
        `_restart_chrome_session`, which quits → sync_back → kill_orphan →
        rebuilds → re-runs `_setup_session`. `build_stealth_driver` re-points
        THIS port (its C2 hook calls the module-global `port.set_driver`) and
        sets `webdriver_wrapper_instance.current_webdriver` to the new driver,
        so `self.driver` is current afterwards."""
        _restart_chrome_session(self.driver, email, password)

    # ---- JS ----
    def execute_script(self, script, *args):
        return self.driver.execute_script(script, *args)

    def execute_async_script(self, script, *args):
        return self.driver.execute_async_script(script, *args)

    # ---- element lookup ----
    def find_elements_xpath(self, xpath):
        return self.driver.find_elements(By.XPATH, xpath)

    def find_element_xpath(self, xpath):
        els = self.driver.find_elements(By.XPATH, xpath)
        return els[0] if els else None

    def ancestors(self, element, depth: int = 4):
        """Return [element, parent, grandparent, …] up to `depth` ancestors
        (stops early at the document root). Absorbs the variant-specific
        ancestor-walk: selenium uses element-relative `find_element(By.XPATH,
        '..')`, je uses one execute_script round-trip — both yield the same
        chain, so `expand_character_section` can live in the shared module."""
        out = [element]
        cur = element
        for _ in range(depth):
            try:
                cur = cur.find_element(By.XPATH, "..")
                out.append(cur)
            except Exception:  # pylint: disable=broad-except
                break
        return out

    # ---- interaction ----
    def click(self, element, pause: float = 0.15):
        # novelai 既有風格：ActionChains hover→click（反偵測節奏），失敗退回 JS
        # click。pause 沿用各呼叫點原值（多數 0.15，少數 0.1 / 0.12）。刻意 NOT
        # 純 el.click()——那是 je 的風格；兩變體的點選風格不同、不可抹平。
        try:
            ActionChains(self.driver).move_to_element(element).pause(
                pause).click().perform()
        except Exception:  # pylint: disable=broad-except
            self.driver.execute_script("arguments[0].click();", element)

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
        try:
            actions = ActionChains(self.driver)
            for _ in range(random.randint(2, 3)):
                actions.move_by_offset(
                    random.randint(-60, 60), random.randint(-40, 40)
                ).pause(random.uniform(0.08, 0.2))
            actions.perform()
        except Exception:  # pylint: disable=broad-except
            pass

    def press_enter(self, element):
        element.send_keys(Keys.ENTER)

    def press_escape(self):
        """driver 層的真 Escape（`isTrusted` 的事件）。回是否送出去了。

        `_SEND_ESCAPE_JS` 造的是**合成**事件，有些前端的 focus-trap 會忽略它；
        driver 送的才是真的按鍵。novelai 這一側沿用既有的 ActionChains 風格，
        失敗才退回對焦點元素 `send_keys`。
        """
        try:
            ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
            return True
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            element = self.driver.execute_script(
                "return document.activeElement || document.body;")
            if element is not None:
                element.send_keys(Keys.ESCAPE)
                return True
        except Exception:  # pylint: disable=broad-except
            pass
        return False

    # ---- page / session ----
    def get(self, url):
        self.driver.get(url)

    def current_url(self):
        return self.driver.current_url or ""

    def get_title(self):
        return self.driver.title or ""

    def refresh(self):
        self.driver.refresh()

    def save_screenshot(self, path):
        return self.driver.save_screenshot(path)


# Module-global port. `driver` is set by `build_stealth_driver` after each
# (re)build; until then it is None (no helper runs before the first build).
port = BrowserPort()


PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_FILE = PROJECT_ROOT / "auth.md"  # main shell reads credentials before boot
# Queue / fallback files (TODO_*_FILE / PROMPT_FILE / CHARACTER2_FALLBACK_FILE /
# UNDESIRED_FILE) moved to _webrunner_shared with read_queues + run_batch (C6).
OUTPUT_ROOT = PROJECT_ROOT / "output"
EVENTS_FILE = PROJECT_ROOT / "events.ndjson"
# DOM_REQUEST_FILE 已移入 _webrunner_shared（check_dom_request 一併搬走，C3）。
# Bot 寫單張生成請求到這個檔，webrunner 在啟動時（idle 一次性 one-shot）或
# iteration 邊界（batch 進行中 in-band）poll 它，看到就只產 1 張圖、結果走
# events.ndjson 的 `single_image_done` event 回傳 bot。one-shot 圖一律存進
# output/_oneshot/<request_id>/，不碰 resume checkpoint、不參與佇列 pop / 編號。
# 詳見 serve_single_image_request / check_single_image_request（已搬入
# _webrunner_shared，C4；SINGLE_IMAGE_REQUEST_FILE 與 SINGLE_IMAGE_OUTPUT_ROOT
# 一併移入 shared）。**本變體刻意不再持有 SINGLE_IMAGE_REQUEST_FILE**：pass 3
# 之後 main() 用 ws.parse_run_mode(sys.argv) 判斷這一輪是不是單圖伺服器，不再
# 從磁碟上有沒有請求檔推論（推論錯過一次，事故紀錄在 ws.RUN_MODE_BATCH 上面
# 那一段），所以這裡沒有東西需要引用它了。
# CONSECUTIVE_FAIL_ALERT moved to _webrunner_shared with generate_loop (C5).

LOGIN_URL = "https://novelai.net/login"
IMAGE_URL = "https://novelai.net/image"
# 產圖模型的候選字面住在 `batch_config.json` 的 `model_candidates`（預設值與
# 型別檢查在 `_batch_config`），`_setup_session` 每次 setup 讀一次。
# **不要在這裡放回一份模組常數**：兩個 webrunner 變體各存一份、又必須永遠一致，
# 正是 CLAUDE.md 點名的漂移陷阱——設定檔是唯一來源，兩邊都從那裡讀。
# 站方全部候選都落空時 `ws.select_model` 會把當下看得到的選項 dump 進 log，
# 那行 log 就是「該把 `model_candidates` 改成什麼」的答案。

# ---- Opt-in isolated verification mode (default OFF; production unchanged) ----
# `verify_browser.py --full` launches THIS script as a subprocess with
# `NAI_VERIFY_MODE=setup` to exercise the real login → navigate → setup → key-
# control reachability path WITHOUT touching production. The env var is unset in
# every production launch (bot `_spawn_webrunner`, `start_webrunner.py`, idle
# one-shot), so `__main__` falls through to `main()` exactly as before — not a
# single production code path or default changes. See `_run_setup_verification`.
VERIFY_MODE_ENV = "NAI_VERIFY_MODE"          # "setup" → run the verify path
VERIFY_PROFILE_DEST_ENV = "NAI_VERIFY_PROFILE_DEST"  # isolated profile snapshot dest
VERIFY_OUTPUT_DIR_ENV = "NAI_VERIFY_OUTPUT_DIR"      # isolated output (stretch only)
VERIFY_GENERATE_ENV = "NAI_VERIFY_GENERATE"  # "1" → also generate ONE isolated image
# Set True ONLY by `_run_setup_verification` so the shared `build_stealth_driver`
# retry path does NOT nuclear-sweep Chrome during a verification run (a verify
# run must stay non-invasive — never kill a user's personal Chrome / a
# concurrent production browser). Production leaves it False → sweep behaves
# exactly as before. Teardown reaps only our own driver tree instead.
_SUPPRESS_ORPHAN_SWEEP = False

# Batch params (images_per_character / inter_image_delay_sec / schedule_limit
# _hours / rest_hours / generate_*_retries / download_max_retries) live in
# `batch_config.json` at the repo root. Loaded once per character iteration
# via `load_batch_config()` so edits take effect on the NEXT character without
# killing the run. Defaults match the historical hardcoded values; see
# `_batch_config._DEFAULT_BATCH_CONFIG`.

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
# Bot 可在「webrunner 已在跑 batch」時 in-band 插一張即時生成，或「閒置」時
# spawn 一個一次性 webrunner 來產一張。兩條路都呼叫 serve_single_image_request，
# 產出同一種 `single_image_done` event，讓 bot 把圖貼回 Discord。


# ---------- browser ---------------------------------------------------------

CHROME_PROFILE_DIR = PROJECT_ROOT / ".chrome_profile"


def hide_chrome_window(driver) -> bool:
    """確保瀏覽器視窗在螢幕外，回 True 表示找到視窗、而且沒有任何一個留在螢幕上。

    **不再最小化**（2026-09-22）：視窗開起來就在螢幕外（見 `_make_chrome_options`），
    這裡只把跑到螢幕上的搬回去。原本的「最小化，被喚回之後再最小化」會讓剛產出的圖
    在前景閃一下，理由與實測寫在 `_webrunner_shared.OFFSCREEN_WINDOW_POSITION` 上面。
    實際動作在 `_webrunner_shared.hide_browser_windows`，兩個變體共用同一份；Win32 的
    部分全部轉呼叫桌面自動化函式庫，本檔不自己開 pywin32。
    """
    del driver                       # 作業系統層那一段不需要 driver 物件
    hidden, exposed = hide_browser_windows(
        [CHROME_PROFILE_DIR, CHROME_PROFILE_SNAPSHOT])
    return hidden > 0 and exposed == 0


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
    """Kill EVERY leftover `chrome.exe` / `chromedriver.exe` on the box
    before we spawn our own Chrome.

    Closes the "舊實例" (orphan) gap that crash-respawn cycles leave behind:

    - When a run crashes (renderer "Aw, Snap!" / OOM) the webrunner python
      exits non-zero and the supervisor respawns it — but Chrome's child
      processes (renderer / GPU / utility) are reparented orphans that
      DON'T die with the python parent. They keep eating RAM, which makes
      the NEXT Chrome more likely to OOM-crash too → a death spiral where
      "崩潰後一直找不到目標而失敗".
    - The bot's `cmd_run` sweeps Chrome, but the supervisor respawn path
      (`_spawn_webrunner` in discord_bot.py) and the standalone
      `start_webrunner.py` launcher do NOT. Doing the sweep HERE makes it
      universal across every launch path and both webrunner variants.

    **三個呼叫點，而且「可以放心大殺」的理由三個都不一樣。** 這段以前只寫了第一
    個（"Safe to go nuclear: our own driver doesn't exist yet"），讀的人會以為
    整輪只掃一次；實際上 `restart_chrome_every_n_characters` 預設是 1，代表
    **每個角色邊界都會再對全機掃一次**。

    * `main()` 開頭，`build_stealth_driver()` 之前。理由就是原本那句：此刻我們
      自己的 driver 還不存在，沒有屬於我們的 Chrome 要保護。
    * `_restart_chrome_session()`（週期性的記憶體沖洗）。**在這裡那句理由是假
      的**——我們的 driver 前一秒還在。正確的理由是：`quit()` 已經先跑過，而且
      我們正要把自己的 Chrome 拆掉重建，所以即使 `quit()` 失敗、瀏覽器還活著，
      殺掉它正是這一步要達成的事。
    * `build_stealth_driver()` 的 spawn 重試之間。同樣不是「沒有我們的 Chrome」
      ——失敗的 `webdriver.Chrome(...)` 常留下半死的 chrome.exe，而**釋放它佔住
      的記憶體正是重試前必須做的**（低記憶體機器上那具屍體就是讓重試也 OOM 的
      原因）。這一點在隔離驗證模式下由 `_SUPPRESS_ORPHAN_SWEEP` 關掉。

    **代價，已知並接受，不是缺陷：** 後兩個呼叫點發生在執行途中，離使用者按下
    開始可能已經好幾小時，而 `taskkill /F /T /IM chrome.exe` 是全機範圍的。
    別人的**自動化** Chrome 不會受害——`verify_browser.py` 依 `_chrome_slot`
    契約先取槽、再讀 `webrunner.pid`，讀到活著的正式批次就讓位不開瀏覽器，而那個
    pid 檔由兩支啟動器（bot 的 `_spawn_webrunner`、`start_webrunner.py`）在整輪
    期間都持有。

    **但那個保證有前提：批次必須是由兩支啟動器之一帶起來的。** webrunner 自己既不取
    Chrome 槽、也不寫 `webrunner.pid`（見 `_run_setup_verification` 那段說明）——兩個
    訊號都是父行程給的，啟動器在 spawn 的**短臨界區**內取槽 → 寫 pid → 放槽，之後
    整輪由那個檔案當存活訊號。所以直接用 `py -3 axiomatic/webrunner_novelai.py`
    裸跑的話，槽是空的、pid 檔也不存在，同時執行的驗證程式會判定「沒有正式作業」而
    開出自己的瀏覽器，然後在下一個角色邊界被這個掃描殺掉——**症狀會長得像「瀏覽器
    自己壞了」**，而原因在另一個行程裡。`run_batch.py` 不受影響，它是轉交給
    `start_webrunner.py` 去 spawn 的。

    但**使用者自己開的 Chrome 會被一起殺掉，而且沒有預告**。
    `msedge.exe` 刻意放過就是這個設計的另一半：假設擁有者的日常瀏覽器是 Edge、
    Chrome 是我們的。要改這個假設，該改的是掃描的範圍，不是這段註解。

    Two passes because OOM is usually caused by orphans NOT dying:
    1. psutil `proc.kill()` — clean, cross-checks the name, skips self.
    2. `taskkill /F /T /IM` fallback — catches processes psutil couldn't
       (AccessDenied, or children psutil missed) and kills the whole tree
       (`/T`). Without /T, a reparented renderer survives its parent's
       death and keeps holding RAM → the next Chrome OOMs again ("Aw,
       Snap! Out of Memory"). Always prints a summary (even 0 killed) so
       the log shows the sweep actually ran.

    ⚠️ **`killed 0` 是這條路上的常態，不要拿它推論任何事。** 實測 `WEBRunner.log`
    08-24～09-10 共 38 次掃描，其中 33 次是 `killed 0`；最後一次真的殺到東西是
    08-27 06:27:49，之後 30 次連續全零。**而且「零」的理由每個呼叫點不一樣**：
    重啟那條路（24/25 是零）是 `quit()` 已經先把自家 Chrome 收乾淨了；`main()`
    開機那條路（9/12 是零）是上一輪自己的 `finally: quit()` 收乾淨了、或上一輪的
    掃描已經掃過。所以**乾淨收工與被外力砍掉，在這一行 log 上長得一模一樣**——
    2026-09-10 曾拿「四次瀏覽器死亡都伴隨 `killed 0`」當成「有外力把 Chrome 整組
    移除了」的佐證，那條推論已經收回。

    非零才帶訊息，但**它能告訴你的比看起來少**。五次非零裡，只有兩次可以歸因：
    08-25 11:38:41 的 `killed 17`（重啟路徑，前一秒才印完 sync-back ＝ `quit()`
    一秒內沒收完）與同分鐘 spawn 重試的 `killed 9`（上一次 spawn 留下的半死行程）。
    另外三次（`killed 19` / `38` / `7`）都發生在 `main()` 開機、批次自己還沒有任何
    Chrome 的時刻，所以 log **分不出**那是上一輪的孤兒還是使用者自己開的 Chrome——
    而「分不出來」正是重點：這個掃描本來就不區分，上面那段講的代價就是這個意思。
    不要把非零讀成「有外來者」，也不要讀成「一定是我們自己的」。

    Suppressed (no-op) when `_SUPPRESS_ORPHAN_SWEEP` is set — ONLY the
    isolated verification mode does that, so a `--full` verify run never
    nuclear-kills a user's personal Chrome / a concurrent production browser
    (it surgically reaps just its own driver tree on teardown instead)."""
    if _SUPPRESS_ORPHAN_SWEEP:
        print("orphan-chrome sweep: suppressed (verification mode)")
        return 0
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
    # Fallback: taskkill the whole process tree. Runs when psutil left
    # survivors, was unavailable, or just as a belt-and-braces force kill.
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


# Snapshot dir 用於 build_stealth_driver / sync-back。固定路徑（不帶 pid）
# 簡化清理；每次 spawn 前 rmtree + 重建。
CHROME_PROFILE_SNAPSHOT = PROJECT_ROOT / ".chrome_profile_snap"

# 同步回 .chrome_profile/ 的「session-critical」相對路徑。其他全部 disposable
# （Cache / GPU / Service Worker 等都可以重新生）。
#
# **cookie jar 在 `Default/Network/` 底下，不是 `Default/`。** Chrome 96
# （2021-12）把 cookie store 搬進 network service 自己的目錄。這裡原本寫的是
# `Default/Cookies` / `Default/Cookies-journal`——一個永遠不存在的路徑，於是
# sync-back 的 `if not src.exists(): continue` 每一輪都安靜跳過它們。
# 2026-09-09 實測到的代價：`.chrome_profile/Default/Network/Cookies` 的 mtime
# 停在 2026-05-20，而 08-24～09-09 這 16 天 34 次 setup **34 次都走完整
# `/login` 流程**、`session restored` 一次都沒有。
#
# **舊路徑刻意不留相容。** 留著它只會讓下面新增的分母（`synced N/M`）永遠差
# 兩個，把剛加上去的訊號變成常態雜訊——本專案已經記過「會叫狼來了的守門就是
# 會被關掉的守門」。真要支援 Chrome 96 以前的 profile，正確形狀是另一個
# 「新路徑不存在時才查」的舊路徑表，不是把它塞進分母。
#
# `-journal` 那幾筆是預期存在的：Chrome 的 SQLite 走 TRUNCATE journal mode，
# 提交後留下一個 0 byte 的 journal 檔而不是刪掉它。哪天真的不存在，下面的
# 診斷會點名，不會再靜默。
_SESSION_CRITICAL = (
    "Default/Network/Cookies",
    "Default/Network/Cookies-journal",
    "Default/Login Data",
    "Default/Login Data-journal",
    "Default/Preferences",
    "Default/Web Data",
    "Default/Web Data-journal",
    "Default/Network/Device Bound Sessions",
    "Default/Network/Device Bound Sessions-journal",
    "Local State",
)

# **登入態根本不在 cookie 裡。** 上面那份清單修好之後（2026-09-09 早上）我們仍然
# 每一輪都要重新登入，因為 cookie 從來就不是那個站放權杖的地方。逐項實測：
#
# * `.chrome_profile/Default/Network/Cookies` 對那個站只有三筆 cookie，全是
#   cookie 同意橫幅（`osano_consentmanager*`）與 Google 登入小工具的 UI 狀態
#   （`g_state`）。**沒有任何一筆是驗證用的**，而且三筆都還沒到期
#   （2026-11-16 / 2027-05-20），所以「舊 cookie 過期了」不成立。
# * 登入態在 `Default/Local Storage/leveldb/`，key 叫 `session`（旁邊還有
#   `lastLoginMethod`）。而且那是**同一個時刻**的觀察：來源 profile 的 cookie jar
#   與 leveldb 都停在 2026-05-20 20:06，也就是說在一個確定已登入的工作階段乾淨
#   關閉的當下，cookie 裡沒有權杖、localStorage 裡有。
# * 內容層的證據：來源與 snapshot 的 `000005.ldb` 裡那筆 `session` 是**同一筆**
#   （後續位元組指紋相同，序號 57）；snapshot 另外還有一筆**更新的** `session`
#   （序號 472，2026-09-09 才寫進去的 `000019.ldb`）。新的那筆會遮蓋舊的，而它
#   只存在於 snapshot——沒有任何東西把它寫回來。
#
# `_snapshot_chrome_profile` 是整份 `os.walk`，所以 localStorage **有**被帶進
# snapshot；漏的是回程。`_sync_chrome_profile_back` 只走上面那份**單檔**清單，
# 所以每一輪都拿一份 2026-05-20 的權杖去試，然後被導去 `/login`。
#
# 影響**不是吞吐量**（實測 08-24～09-09 共 35 次重新登入、每次約 13～16 秒，
# 15.7 天累計約 9 分鐘）。值得修的是另外兩件事：一個以「重複使用 session」為目的
# 的機制**從來沒有成功過一次**（`session restored` 對 36 次 Chrome 啟動 ＝ 0）卻
# 看起來正常運作了四個月；以及每一次重新登入都是一次真實的認證事件，帳號密碼被
# 反覆送出，可能觸發對方的頻率限制／驗證碼／風控標記。
#
# **這幾筆是目錄，要整組寫回，不能逐檔覆蓋**——理由見 `_sync_profile_dir_back`。
_SESSION_CRITICAL_DIRS = (
    "Default/Local Storage/leveldb",
)

# LevelDB 目錄裡**不**帶走的檔名。
#
# * `LOCK`：leveldb 自己的 advisory lock，0 bytes，DB 開著的時候在 Windows 上是
#   獨佔開啟的（實測連唯讀開啟都吃 `PermissionError`）。跳過它就等於把這個目錄
#   裡唯一會被鎖住的東西排除掉，而 leveldb 下次開啟會自己重建。
# * `LOG` / `LOG.old`：leveldb 自己的除錯文字輸出，**不被 MANIFEST 參照**，不是
#   DB 一致狀態的一部分。帶過去只會留下一份誤導的產物——實測 `.chrome_profile`
#   那份 `LOG` 裡印的還是 `D:\Work\Example_old\...`（專案改名前的路徑），一份
#   描述著另一個目錄的記錄放在這裡只會害下一個人查錯方向。
#   （順帶：那一行也順手證明了 LevelDB 的 Local Storage key 綁的是 origin、不綁
#   檔案系統路徑——同一份 DB 從舊路徑複製到 `.chrome_profile_snap` 照樣開得起來，
#   `Recovering log #11` 就是它。）
_LEVELDB_SKIP_NAMES = ("LOCK", "LOG", "LOG.old")

# Snapshot 時要跳過的 entry：lock 檔（避免 copy 撞 lock）+ 不必要的 cache。
_SNAPSHOT_IGNORE_NAMES = frozenset(
    _CHROME_LOCK_FILES + (
        "Cache", "Code Cache", "GPUCache", "Service Worker",
        "ShaderCache", "GraphiteDawnCache", "DawnGraphiteCache",
        "Crashpad",  # crash dumps, big, irrelevant
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


# Memory-conservative flags. The long run accumulates renderer memory
# (NovelAI is a heavy React SPA + image canvas) until Chrome throws
# "Aw, Snap! Out of Memory" — which then crashes the renderer and, on a
# low-RAM box, can stop the NEXT Chrome from even starting
# (SessionNotCreatedException: Chrome instance exited). These cap caches and
# trim background work WITHOUT touching GPU/rendering (disabling the GPU
# risks NovelAI's canvas not painting, which would break image-src
# detection). They reduce baseline footprint; they don't stop an unbounded
# in-page leak — for that the run still relies on the schedule rest + the
# crash-detect → respawn path.
_MEMORY_FLAGS = (
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disk-cache-size=33554432",   # 32 MiB
    "--media-cache-size=33554432",  # 32 MiB
    # Keep the minimized renderer fully active so generation doesn't stall
    # AND so Chrome doesn't keep a throttled-but-resident extra renderer.
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
)


def _make_chrome_options(profile_dir: Path) -> ChromeOptions:
    opts = ChromeOptions()
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # 開窗就放在螢幕外、固定大小，**不要**換回 `--start-maximized`：最大化的視窗一被
    # 喚回就是整個螢幕，剛產出的圖會在前景閃一下（擁有者要求 2026-09-22，理由與實測
    # 見 `_webrunner_shared.OFFSCREEN_WINDOW_POSITION`）。je 變體的 `cli_args` 是同一組。
    opts.add_argument("--window-position=-32000,-32000")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--lang=en-US")
    opts.add_argument(f"--user-agent={REAL_USER_AGENT}")
    for flag in _MEMORY_FLAGS:
        opts.add_argument(flag)
    profile_dir.mkdir(parents=True, exist_ok=True)
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return opts


def _leveldb_manifest_ok(dirpath: Path) -> bool:
    """`dirpath` 是不是一份**自洽**的 LevelDB。

    判準就是 LevelDB 自己的開啟路徑：`CURRENT` 是一行文字，內容是目前那份
    manifest 的檔名；manifest 再列出所有還活著的 `*.ldb` / `*.log`。所以
    「`CURRENT` 在，而且它指到的 MANIFEST 也在」是最小的完整性條件。

    **manifest 指到的檔案缺一個，Chrome 不會報錯，它會安靜地把整個 DB 丟掉**，
    然後從零開始——症狀就是「又要重新登入」，跟這個目錄看不出關係。所以這個判準
    用在兩個地方：換過去之前（不完整就不換，保留舊的）、以及 snapshot 之後的
    診斷（不完整就等於這一輪會被登出）。

    讀不到一律回 False：這個方向的誤判代價是多登入一次，反過來是裝一份壞掉的 DB。
    """
    try:
        current = (dirpath / "CURRENT").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not current.startswith("MANIFEST-"):
        return False
    # ⚠️ 上面那一行**不是**包含性檢查，雖然它擺在接合的正前方、讀起來很像。實測：
    # `MANIFEST-x/../../../../auth.md` 解析到 `.chrome_profile\auth.md`，八層
    # `../` 到 `D:\auth.md`（`MANIFEST-C:/…` 反而無害，`MANIFEST-C:` 不是合法磁碟
    # 機字首）。今天不是缺陷——下一行只做 `.is_file()`（回 bool，不開檔不寫檔），
    # `dirpath` 恆在我們自己的 profile 底下，`CURRENT` 是 Chrome 寫的。但**守衛的
    # 位置暗示了它沒有做的事**：把 `.is_file()` 換成 `read_bytes()` 或 `unlink()`
    # 的那一天，上面那張表就從無害變成有害，而這個接合對接合守門是隱形的（左邊是
    # 參數）——不會有任何東西變紅。所以補一道真的檢查，順便讓守門看得見它。
    # 不會誤擋：leveldb 的 `SetCurrentFile` 寫的本來就是去掉目錄前綴的 basename
    # （`MANIFEST-%06llu` ＋ `\n`），本機抽樣 402 份真的 LevelDB `CURRENT`，
    # 402/402 是 `MANIFEST-<數字>`，0 份會被擋。而被擋掉走的是 `return False`
    # ＝「這份 DB 不完整」，也就是本函式 docstring 已經寫明代價的那個便宜方向。
    if not _is_single_path_component(current):
        return False
    return (dirpath / current).is_file()


def _session_entry_present(base: Path, relpath: str) -> bool:
    """`base` 底下這一筆登入資料算不算「在」。

    單檔就是 `exists()`；目錄那幾筆要求 LevelDB 完整，因為一個 `CURRENT` 指不到
    MANIFEST 的目錄「存在」但沒有用——而「存在但沒用」正是這個專案最貴的一類
    缺陷（看起來正常，安靜地不生效）。
    """
    if relpath in _SESSION_CRITICAL_DIRS:
        return _leveldb_manifest_ok(base / relpath)
    return (base / relpath).exists()


def _reclaim_dir_sync_residue(dst: Path) -> None:
    """收掉上一次目錄交換留下的殘骸，順便救回「死在兩個 rename 之間」那一刻。

    `_sync_profile_dir_back` 的交換是兩步（`dst` → `.sync.old`、`.sync.tmp` →
    `dst`）。被砍在正中間的話 `dst` 會**不存在**，而完好的舊資料躺在 `.sync.old`
    ——這一支就是把它搬回去。沒有這一步的話，下一輪 `_snapshot_chrome_profile`
    會複製一份沒有 `Local Storage/leveldb/` 的 profile，等於整個 localStorage
    連同登入態一起消失。

    三種狀態各有各的處置：`.sync.tmp` 一律是垃圾（它從來沒有被裝上去過）；
    `.sync.old` 而 `dst` 不在 ＝ 上面那一刀，搬回去；`.sync.old` 而 `dst` 在 ＝
    交換成功了只是沒刪乾淨，刪掉。永不 raise。
    """
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

    **為什麼要整組換，不能逐檔覆蓋。** LevelDB 是一組互相參照的檔案：`CURRENT`
    指向一份 `MANIFEST-*`，manifest 列出目前活著的 `*.ldb` / `*.log`。逐檔覆蓋會
    把兩個世代混在一起——實測就看得到：來源那份的檔案是 `000005/000008/000010`
    ＋ `000011.log`，snapshot 跑了幾小時之後變成 `000005/000019/000021/000023`
    ＋ `000022.log`（leveldb 的 LOG 裡有整串 `Delete type=2 #8 #10 …` 的壓實
    紀錄）。把後者逐檔倒進前者，目的地會同時有新 manifest 與一堆它不認得的舊檔，
    而**缺一個 manifest 指到的檔案 Chrome 就會安靜地丟掉整個 DB**——症狀跟現在
    一模一樣。所以：先在旁邊組一份乾淨的，驗過完整性，再整個換過去。

    **原子性是目錄級的取捨，不是忘了照 CLAUDE.md 那條硬規則做。** 單檔可以
    「同目錄 temp → `os.replace`」一步到位；目錄不行——Windows 的 `MoveFileEx`
    對已存在的目錄不吃 `REPLACE_EXISTING`。所以這裡是最小視窗的兩步 rename
    （`dst` → `.sync.old`，`.sync.tmp` → `dst`），中間有一個「`dst` 短暫不存在」
    的窗口，寬度是兩個 rename 系統呼叫。死在那個窗口留下的殘骸由
    `_reclaim_dir_sync_residue` 在下一輪救回來（它在這一支開頭與
    `_snapshot_chrome_profile` 開頭各跑一次，後者才是真正會救到的那個時機）。

    **時序：這一支只能在 `driver.quit()` 之後跑。** 兩個呼叫端本來就是這個順序，
    不要為了「早點拿到檔案」改成在 Chrome 還活著的時候複製。Chrome 對
    localStorage 的落盤是有節流的（每筆提交延遲約 5 秒、每個 host 每小時最多 60
    次），所以活著的時候複製有機會拿到還沒 flush 的狀態——而那個失敗一樣是安靜
    的：下一輪照樣被要求重新登入，log 上什麼都看不出來。

    **不做選擇性複製。** leveldb 裡除了 `_<origin>\\x00\\x01<key>` 之外還有
    `META:` 前綴的 per-origin 中繼資料（提交時間戳等）；只挑幾個 key 搬會破壞
    它的一致性。整個目錄一起搬本來就會帶到，不需要也不應該過濾。
    """
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
    """Copy .chrome_profile/ → .chrome_profile_snap/、跳過 lock files 跟
    cache dirs。用來規避「lockfile 被某個 system process 抓住」的窘境 —
    Chrome 開的是 snapshot、跟原 profile 的 lock 無關。

    複製失敗的單檔（如另一個被鎖住的 file）會 swallow + warn，但整體成功
    繼續。回 snapshot dir 路徑。Webrunner 結束時請呼叫
    `_sync_chrome_profile_back(snapshot)` 把登入資料寫回。"""
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
    # **一定要在 walk 之前。** 上一輪若死在目錄交換的正中間，來源會是「`leveldb/`
    # 不見了、完好的資料在 `leveldb.sync.old/`」。不先救回來的話，下面這一趟會把
    # 一份**沒有 localStorage** 的 profile 複製過去，Chrome 開起來就是全新的未登入
    # 狀態——而且 `.sync.old/` 還會被當成一般目錄一起複製，看起來一切正常。
    for relpath in _SESSION_CRITICAL_DIRS:
        _reclaim_dir_sync_residue(CHROME_PROFILE_DIR / relpath)
    copied = 0
    skipped: list[str] = []
    try:
        # `copytree` with `ignore` + `dirs_exist_ok` + per-file fallback so
        # one locked file doesn't kill the whole copy.
        for root, dirs, files in os.walk(CHROME_PROFILE_DIR):
            rel_root = Path(root).relative_to(CHROME_PROFILE_DIR)
            # filter dirs in-place（os.walk 文件指定的方式）
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
    # 真正可行動的訊號：**snapshot 裡找不到**的 session-critical 檔案。少了它們
    # 就等於「這一輪會被登出」，而 `skipped N` 看不出來掉的是快取還是登入權杖。
    #
    # 判準刻意**不要求來源存在**。舊版是「來源有 and snapshot 沒有」，只抓得到
    # 「檔案有、但沒帶過去」——**一個要求來源存在的檢查，抓不到路徑寫錯**。
    # `_SESSION_CRITICAL` 那兩筆 cookie 路徑錯了四個月，這個診斷從頭到尾沒有
    # 機會說話：來源也沒有那個檔，條件第一段就是 False。
    # 來源也沒有的那幾筆會被標出來，因為那正是「路徑寫錯／還沒登入過」與
    # 「有檔案但複製失敗」的分野，兩者要做的事完全不同。
    #
    # 目錄那幾筆（localStorage 的 LevelDB）也要看，而且判準是「完整」不是「在」
    # ——一個 `CURRENT` 指不到 MANIFEST 的目錄存在，但 Chrome 會把整個 DB 丟掉。
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
    （cookie jar、Login Data、Preferences、Web Data、Local State）複製回
    `.chrome_profile/`，保留登入。其他 disposable 的 cache 不同步。
    任何單檔 copy 錯誤 swallow 不擋退出。

    **摘要一定要帶分母，而且無條件印。** 舊版只報分子（`synced N session
    files back`）而且包在 `if synced:` 裡，兩個毛病各自造成一次靜默：

    * `_SESSION_CRITICAL` 那兩筆 cookie 路徑寫錯的四個月裡，它一字不差地印了
      **29 次 `synced 6 session files back`**，而正確答案是 8。**少了兩個，看
      起來跟正常一模一樣**——沒有分母就沒有人看得出短少。
    * `if synced:` 讓最糟的結果（一個都沒寫回去 ＝ 下一輪必定重新登入）剛好是
      唯一什麼都不印的那個。`_snapshot_chrome_profile` 的摘要 2026-09-07 才因為
      同一個毛病改成無條件印，這裡漏掉了。

    來源不存在的那幾筆現在會被點名（stderr）。那是「路徑寫錯」唯一會留下的
    痕跡——迴圈的 `if not src.exists(): continue` 本身永遠不會出聲。

    **分母有兩個，而且刻意不合併成一個。** 單檔與目錄是不同的單位，把一個
    LevelDB 目錄算成「一筆」跟把一個 cookie jar 算成「一筆」擺在同一個分數裡，
    分母就再也不能回答「短少了嗎」——而那正是這整段程式碼存在的理由。所以印的是
    `synced N/M session files + D/E session dirs [<目錄>: K files]`：兩個同質的
    分數，加上目錄裡**實際帶了幾個檔案**。最後那個數字是必要的——「換上去一份空
    的 LevelDB」會讓 `1/1` 看起來完全正常，而它的後果是登入態全沒。
    """
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


def build_stealth_driver() -> webdriver.Chrome:
    """Spawn Chrome 用 snapshot profile（避開原 `.chrome_profile/`
    可能被 Defender / OneDrive / Explorer 抓住的 lockfile）。

    流程：snapshot 原 profile → 用 snapshot 開 Chrome → 失敗 retry 一次。
    Webrunner 結束時 `_sync_chrome_profile_back` 把登入寫回。

    chromedriver 的記錄寫到 `chromedriver.log`；spawn 真失敗時去
    看那個檔的最後幾行才有 root cause（Selenium 包裝的「Chrome instance
    exited」沒資訊）。

    這裡是**每一條起 driver 的路徑**的唯一收斂點（開機的 `main()`、週期性重啟的
    `_restart_chrome_session`、隔離驗證模式），所以記錄檔的封頂就掛在這裡——放在
    `ChromeService(...)` 之前、而且此刻沒有任何 chromedriver 活著（呼叫端剛跑完
    `_kill_orphan_chrome()`）。前提為什麼是硬性的見 `_trim_chromedriver_log`。
    """
    from selenium.webdriver.chrome.service import Service as ChromeService

    # 封頂與保留都要在建立 `ChromeService` 之前——它會把路徑變成 chromedriver 的
    # `--log-path=`，之後那個檔就有行程握著了，中途截斷會被補零（實測）。
    # 順序是「先封頂、再改名」，所以 `.prev` 拿到的一定是已封頂的那一份。
    _trim_chromedriver_log()
    _rotate_chromedriver_log()
    snapshot = _snapshot_chrome_profile()
    _clear_snapshot_locks(snapshot)

    # Spawn with up to 3 attempts. The critical bit between attempts is
    # `_kill_orphan_chrome()`: a failed `webdriver.Chrome(...)` can leave a
    # half-started chrome.exe alive, and on a low-RAM box that leftover is
    # exactly what makes the retry OOM too ("retries until crash"). Free its
    # RAM + rebuild a clean snapshot before trying again. Dump the
    # chromedriver.log tail on every failure so the real cause is visible.
    max_attempts = 3
    driver = None
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        # **`log_output`，不是 `log_path`。** selenium 的 `Service` 沒有
        # `log_path` 這個參數（4.41／4.44 都沒有），傳進去會一路掉進
        # `**kwargs`，最後被 `common.service.Service.__init__` 安靜地丟掉——
        # 不會警告、不會報錯。實測 `command_line_args()`：
        #   log_path=…   → ['--enable-chrome-logs', '--port=N', '--verbose']
        #   log_output=… → [… , '--verbose', '--log-path=<檔案>']
        # 也就是說 `--verbose` 一直有傳，chromedriver 一直在產生詳細記錄，然後
        # 整份丟進 DEVNULL：`chromedriver.log` **從來沒有被建立過**，而
        # `_dump_chromedriver_log_tail()` 每次都在讀一個不存在的檔、靜靜地
        # return。實證：2026-08-25 11:38:50 真的發生過一次
        # `Chrome spawn attempt 1/3 failed: SessionNotCreatedException()`——那是
        # 這份記錄唯一存在的理由，而 log 裡後面**一行 tail 都沒有**。
        # 檔案大小**不是無條件有界的**。實測 chromedriver 每次啟動會清空
        # `--log-path` 指的檔（不是附加），所以上限等於「一個 chromedriver
        # 工作階段的量」——而那個量是**工作階段的長度**，不是
        # `restart_chrome_every_n_characters` 的角色數：後者以角色為單位，設 1
        # 也可能是十幾小時（見 `_trim_chromedriver_log` 的 docstring）。
        # 2026-09-07 實測 2.15 MB/小時，一個角色（120 張 ÷ 8 張/小時 ＝ 15 小時）
        # 約 30 MB。我們自己那一層上限在 `_trim_chromedriver_log`，就掛在本函式
        # 開頭。
        #
        # **`--log-level=INFO`，不是 `--verbose`。** 2026-08-29 實測（同一個
        # 真的 spawn 失敗，兩種等級各跑一次）：
        #   平時每個 WebDriver 命令  --verbose 4370 bytes / INFO 484 bytes（9 倍）
        #   spawn 失敗時的記錄內容   1242 bytes / 1095 bytes，root cause 一字不差
        #     （`RESPONSE InitSession ERROR session not created` ＋ Chrome 那一側
        #      的原因、`Starting ChromeDriver`、完整的 `--user-data-dir`）
        # 也就是說 `--verbose` 多出來的九成全是**正常運作時**的雜訊，而這份記錄
        # 唯一的用途是「失敗之後看最後幾行」。`wait_for_new_image` 每張圖最多
        # poll 180 次、每次兩個命令，`--verbose` 一個角色可以寫到 180 MB
        # （常見情形約 20 MB），INFO 是 20 MB／2.2 MB。無人值守的機器上這是純
        # 粹的磁碟耗損。
        service = ChromeService(
            # `ws.` 前綴不是風格：記錄路徑的常數只有共用模組那一份，這裡透過屬性
            # 讀取，所以測試把它 monkeypatch 到 tmp 檔時，這個呼叫點也跟著改。
            # 變體自己再綁一份同名常數的話，patch 會失效而且**不會報錯**——修剪就
            # 會打到 repo 根目錄那個正被活著的 chromedriver 握著的真檔。
            log_output=str(ws._CHROMEDRIVER_LOG),
            service_args=["--log-level=INFO"],
        )
        try:
            driver = webdriver.Chrome(
                options=_make_chrome_options(snapshot), service=service)
            break
        except Exception as err:  # pylint: disable=broad-except
            last_err = err
            print(f"Chrome spawn attempt {attempt}/{max_attempts} failed: "
                  f"{full_error_detail(err)}", file=sys.stderr)
            _dump_chromedriver_log_tail()
            if attempt < max_attempts:
                # Reap the half-spawned chrome (frees RAM so the retry has a
                # chance), then rebuild a fresh snapshot from the real profile.
                _kill_orphan_chrome()
                time.sleep(2.0)
                snapshot = _snapshot_chrome_profile()
                _clear_snapshot_locks(snapshot)
    if driver is None:
        raise RuntimeError(
            f"Chrome failed to start after {max_attempts} attempts "
            f"(likely Out of Memory or a chromedriver/Chrome version "
            f"mismatch — see chromedriver.log above). Last error: "
            f"{full_error_detail(last_err)}"
        ) from last_err

    # 這次 session 真的接上的兩個版本，每次 spawn 記一行——見
    # `ws.log_driver_versions` 的說明（Chrome 2026-09 起兩週一版，chromedriver
    # 的主版號必須完全相符）。⚠️ 這段註解原本寫「版號不符丟出來的
    # `SessionNotCreatedException` 訊息是空的」——**2026-09-12 實測推翻，撤回**：
    # 訊息一直都在（351 字元，含兩邊版本號），空的是 `args`，被 `{err!r}` 丟掉
    # 的。這一行版本紀錄仍然值得留著（它在 spawn **成功**時就先記下來，失敗那一
    # 刻不必仰賴任何格式），但它的理由不再是「否則就沒有版本資訊」。
    try:
        ws.log_driver_versions(driver.capabilities)
    except Exception as error:  # pylint: disable=broad-except
        print(f"driver version log failed: {full_error_detail(error)}",
              file=sys.stderr)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS}
    )
    # 把 snapshot path 掛在 driver 上，main() 在 finally 裡可以拿來 sync back。
    driver._nai_snapshot_profile = snapshot  # type: ignore[attr-defined]
    # Point the module-global port at this (re)built driver so every helper's
    # `port.*` call reaches the live driver. Covers the initial boot, every
    # mid-run Chrome restart, AND the verify path — all route through here.
    port.set_driver(driver)
    return driver


# `_DEBUG_SCREENSHOTS` / `snap` and `_CHROME_CRASH_TITLE_MARKERS` /
# `_is_chrome_crash_page` moved to _webrunner_shared (C3).


# ---------- login ------------------------------------------------------------


def login_if_needed(driver, email: str, password: str) -> bool:
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
    return login(driver, email, password)


def login(driver, email: str, password: str) -> bool:
    if "/login" not in port.current_url():
        port.get(LOGIN_URL)
        human_pause(1.5, 2.5)
    # Cookie banner can overlap the submit button on /login.
    ws.reject_cookies(port, timeout=5.0)
    port.mouse_wiggle()
    wait = WebDriverWait(driver, 30)
    try:
        email_box = wait.until(
            EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[type='email'], input[name='email']")
            )
        )
    except Exception:  # pylint: disable=broad-except
        ws.snap(port, "no_email_field")
        return False
    email_box.click()
    human_pause(0.15, 0.4)
    human_type(email_box, email)
    human_pause(0.3, 0.7)

    pw_box = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
    pw_box.click()
    human_pause(0.15, 0.4)
    human_type(pw_box, password)
    human_pause(0.4, 0.9)

    # Find the form-submit button (NOT the cookie banner buttons).
    submit_btn = port.execute_script(
        """
        // Prefer input[type=submit] that lives inside a <form>.
        for (const inp of document.querySelectorAll("input[type='submit'], button[type='submit']")) {
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
    try:
        wait.until(lambda d: "/login" not in d.current_url)
        return True
    except Exception:  # pylint: disable=broad-except
        ws.snap(port, "login_failed")
        return False


# ---------- post-login actions ----------------------------------------------


# ---------- prompt helpers ---------------------------------------------------


def click_add_character(driver, gender: str = "Female", timeout: float = 8.0) -> bool:
    """加一個角色欄位。實作在共用層（`ws.click_add_character_control`），兩個
    webrunner 變體共用同一份 —— V5 把「Add Character」按鈕換成沒有文字、沒有
    aria-label 的圖示鈕，這種會跟著站方改版的邏輯只該有一份。

    `driver` 保留在簽章裡只為了不動呼叫端；共用實作走 `port`。"""
    return ws.click_add_character_control(port, gender, timeout=timeout)


def ensure_two_characters(driver) -> bool:
    """Make sure exactly two Character slots exist; trim/add as needed."""
    have = ws.count_characters(port)
    print(f"existing character slots: {have}")
    # Trim excess from the tail (Character N, Character N-1, …).
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
        click_add_character(driver, gender="Female")
        human_pause(0.8, 1.2)
        new_count = ws.count_characters(port)
        if new_count == have:
            print("  Add Character clicked but no new slot appeared; breaking")
            break
        have = new_count
        print(f"  added; now {have} slot(s)")
    return have == 2


# ---------- generation + download -------------------------------------------


def _setup_session(driver, email: str, password: str) -> bool:
    """Full pre-generation setup on a fresh driver: login, navigate to
    `/image`, reject cookies, select model, ensure two characters, set
    resolution + sampler, then minimize. Returns False on login failure
    (caller decides abort vs raise). Used both for the initial session and
    for every mid-run Chrome restart, so it is self-contained and does NOT
    fill per-pair prompts (the caller does that afterwards)."""
    if not with_retry("login", lambda: login_if_needed(driver, email, password),
                      max_attempts=3, sleep_range=(5, 10)):
        return False
    print("login ok")
    if not port.current_url().startswith(IMAGE_URL):
        port.get(IMAGE_URL)
        WebDriverWait(driver, 30).until(
            lambda d: d.current_url.startswith(IMAGE_URL)
        )
    human_pause(2.0, 3.5)
    ws.reject_cookies(port)
    if not with_retry("select_model",
                      lambda: ws.select_model(
                          port, ws.load_batch_config()["model_candidates"]),
                      max_attempts=3, sleep_range=(2, 4)):
        return False
    if not ensure_two_characters(driver):
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
    # A renderer crash during setup leaves every with_retry above failing
    # silently; catch it here before we enter / resume the generation loop.
    ws._abort_if_chrome_crashed(port, "setup")
    if hide_chrome_window(driver):
        print("browser window is off-screen; generation runs in the background")
    else:
        print("WARN: hide_chrome_window could not confirm the browser window is "
              "off-screen (continuing anyway)")
    return True


def _restart_chrome_session(old_driver, email: str, password: str):
    """Quit Chrome and bring up a fresh, fully-set-up session to flush the
    renderer's leaked memory (NovelAI's SPA grows unbounded across hundreds
    of generations → "Aw, Snap! Out of Memory"). Syncs the login profile
    back, quits, reaps orphans, rebuilds + re-runs `_setup_session`. Returns
    the new driver. Raises RuntimeError if the fresh session can't log in /
    set up (→ generate_loop aborts → supervisor respawns). The caller
    re-fills the current pair's prompts on the returned driver."""
    snap_dir = getattr(old_driver, "_nai_snapshot_profile", None)
    try:
        old_driver.quit()
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    # Sync the (possibly refreshed) login token back so the rebuilt snapshot
    # restores the session via cookie instead of a full re-login.
    if snap_dir:
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
    new_driver = build_stealth_driver()
    webdriver_wrapper_instance.current_webdriver = new_driver
    if not _setup_session(new_driver, email, password):
        raise RuntimeError("re-login failed after Chrome restart")
    print("  [memory] Chrome restarted, re-set-up, ready to resume")
    return new_driver


# ---------- isolated verification mode (opt-in; default OFF) -----------------

def _verify_capture_tree(service_pid: int | None) -> dict[int, float | None]:
    """chromedriver(service_pid) + all descendants → `{pid: create_time}`.

    Captured BEFORE quit() so reparented children can still be reaped
    surgically (verify mode only — mirrors verify_browser.py's surgical,
    NON-nuclear cleanup).

    **帶著建立時間，不是只帶 pid**（2026-09-09 實測，psutil 7.2.2）：PID 會被作業
    系統回收再發給別人，而 capture 與 reap 之間隔著一整個 `driver.quit()`——那一步
    正好讓幾十個 chrome 子行程結束、把 PID 釋放回池子。事後拿裸 pid 去
    `psutil.Process(pid).kill()`，殺的是**現在**那個 pid 的擁有者。這台機器同時跑
    著兩個正式批次的 Chrome，誤殺等於中斷一個已經跑了幾十小時的批次。

    `(pid, create_time)` 就是行程的身分（psutil 的 `Process.__eq__` 用的正是它）。
    值可能是 `None` ＝**身分無法確認**，`_verify_reap_tree` 會跳過而不是照殺：留下
    一個帶著隔離 profile 的 orphan 是無害的（`_verify_rmtree` 本來就容忍殘留），
    殺錯行程不是。與 `verify_browser._capture_own_tree` 逐條對齊，
    `test_verify_browser.py` 兩邊都釘住了。
    """
    tree: dict[int, float | None] = {}
    if not service_pid:
        return tree
    tree[service_pid] = None
    try:
        import psutil  # type: ignore
    except ImportError:
        return tree
    try:
        parent = psutil.Process(service_pid)
        try:
            tree[service_pid] = parent.create_time()
        except Exception:  # pylint: disable=broad-except
            pass
        for child in parent.children(recursive=True):
            try:
                tree[child.pid] = child.create_time()
            except Exception:  # pylint: disable=broad-except
                tree.setdefault(child.pid, None)
    except Exception:  # pylint: disable=broad-except
        pass
    return tree


def _verify_reap_tree(tree: dict[int, float | None]) -> int:
    """Kill ONLY our own driver tree. Never a nuclear /IM sweep.

    殺之前**先對身分**：`create_time()` 與 capture 當下記下的不一樣，代表這個 PID
    已經被回收給別的行程，跳過（理由見 `_verify_capture_tree`）。對不起來或讀不到
    一律跳過——「不確定就不殺」。

    沒有 psutil 時退回 `taskkill /F /T /PID`：仍然只動指定的 PID、不是 `/IM` 全殺，
    但那條路**沒辦法對身分**（`taskkill` 只認 pid）。可以接受是因為 psutil 是必要
    相依（`CLAUDE.md`），實務上走不到；也**不會**為了「至少殺一點」去放寬 psutil
    那條路的身分檢查。Best-effort；回傳殺掉的數量。
    """
    if not tree:
        return 0
    killed = 0
    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore
    if psutil is not None:
        for pid, born in tree.items():
            try:
                proc = psutil.Process(pid)
                # `born is None`（身分沒抓到）也走這一條：任何真實的建立時間都不等於
                # `None`，所以它自然落到「跳過」。刻意**不**另外寫一個 `if born is
                # None: continue` ——那在這裡是死碼。`verify_browser._reap_pids` 有那
                # 條分支，是因為它要印兩句不同的診斷（沒抓到 vs 被回收）；這裡的
                # 驗證模式不印，多一條分支只會讓兩份抄本看起來不一樣。
                if proc.create_time() != born:
                    continue
                proc.kill()
                killed += 1
            except Exception:  # pylint: disable=broad-except
                continue
        return killed
    if os.name == "nt":
        import subprocess
        for pid in tree:
            try:
                r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=10, check=False)
                if r.returncode == 0:
                    killed += 1
            except Exception:  # pylint: disable=broad-except
                continue
    return killed


def _verify_rmtree(path: Path) -> None:
    """Best-effort recursive delete of an isolated verify dir (profile /
    output). Chrome may still hold a handle right after a kill, so retry a
    few times, then ignore_errors as a backstop."""
    import shutil
    for _ in range(6):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.5)
    shutil.rmtree(path, ignore_errors=True)


def _verify_generate_one(driver, output_dir: Path) -> tuple[bool, str]:
    """STRETCH: generate exactly ONE image from an isolated throwaway prompt
    into `output_dir`, reusing the production generate/download helpers. Does
    NOT touch generate_loop / batch / pop / checkpoint / todo files / the real
    output tree. Returns (ok, detail)."""
    batch_cfg = load_batch_config()
    prompt = "1girl, masterpiece, best quality"
    if not ws.with_retry(
            "verify_fill_main_prompt",
            lambda: ws.fill_main_prompt(port, prompt),
            max_attempts=3, sleep_range=(2, 4)):
        return False, "無法填入隔離 prompt"
    human_pause(0.8, 1.5)
    previous_src = ws.get_main_image_src(port)
    try:
        new_src = ws.generate_one_image(
            port, previous_src,
            max_retries=batch_cfg["generate_max_retries"],
            retry_delay=batch_cfg["generate_retry_delay_sec"])
    except ws.GenerationBlockedError:
        # 額度用完／購買對話框。這條路**不等**（驗證要有界的執行時間，
        # `verify_browser.py` 有 240s 的總期限），但要回一個看得懂的原因，
        # 而不是讓例外炸成 `VERIFY-SETUP: FAIL <repr>`。順手把對話框關掉，
        # 免得它留在畫面上干擾同一個 session 後面的檢查。
        ws.dismiss_blocking_dialog(port)
        return False, "額度不足或站方要求處理帳號，無法產圖"
    if not new_src:
        return False, "Generate 後影像未更新（重試耗盡）"
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"verify_{time.strftime('%Y%m%d_%H%M%S')}.png"
    if not ws.download_image_with_retry(
            port, new_src, target,
            max_retries=batch_cfg["download_max_retries"]):
        return False, "影像下載失敗"
    if not target.exists() or target.stat().st_size <= 0:
        return False, "影像檔未生成或為空"
    return True, f"{target.name} ({target.stat().st_size} bytes)"


def _run_setup_verification() -> int:
    """Opt-in, ISOLATED, NON-production setup-reachability verification.

    Triggered ONLY by the `NAI_VERIFY_MODE` env var (unset in every production
    launch → this never runs; main()/batch/single-image/supervisor defaults are
    untouched). `verify_browser.py --full` runs it as a SUBPROCESS while holding
    the cross-process Chrome slot, so the bot won't sweep us. It re-uses the
    EXACT production `_setup_session` (login → navigate → reject cookies →
    model → two characters → resolution → sampler → minimize) and then confirms
    the key generation controls (main prompt area + Generate button) are
    reachable. Optionally (NAI_VERIFY_GENERATE=1) it also generates ONE image.

    Isolation guarantees (must never pollute production):
      * Profile: snapshots `.chrome_profile/` into an ISOLATED dest
        (NAI_VERIFY_PROFILE_DEST, default `.chrome_profile_verify/`), NEVER
        `.chrome_profile_snap/`, and NEVER syncs back to `.chrome_profile/`.
      * Writes NO `webrunner.pid`. Note the reason changed on 2026-09-11: it is
        no longer "the webrunner never does" — a bare `main()` run now claims the
        signal itself when no parent published one
        (`_webrunner_shared.claim_liveness_signal`). What keeps THIS path clean is
        structural: `__main__` dispatches to `_run_setup_verification()` on its own
        branch, before the `run_with_liveness_signal(main)` call, so the claim is
        never reached. Pinned by
        `test_webrunner_shared.test_the_isolated_verification_path_never_claims_the_signal`
        and the deny-list in `test_verify_browser`.
      * Touches NO todo_*.md / prompt.md / undesired.md (never read here).
      * Any image output goes to an isolated dir, removed afterwards.
      * Never nuclear-sweeps Chrome (`_SUPPRESS_ORPHAN_SWEEP`) — surgical
        teardown of only our own driver tree instead.

    Returns 0 on success, non-zero on failure, and prints a single machine-
    readable result line (`VERIFY-SETUP: OK` / `VERIFY-SETUP: FAIL <reason>`)
    plus progress lines (the caller's output-silence backstop must not kill a
    legitimately-working verify)."""
    global CHROME_PROFILE_SNAPSHOT, _SUPPRESS_ORPHAN_SWEEP

    def _vp(msg: str) -> None:
        print(f"[verify-setup] {msg}", flush=True)

    do_generate = os.environ.get(VERIFY_GENERATE_ENV) == "1"
    dest_env = os.environ.get(VERIFY_PROFILE_DEST_ENV)
    verify_profile = Path(dest_env) if dest_env else (
        PROJECT_ROOT / ".chrome_profile_verify")
    out_env = os.environ.get(VERIFY_OUTPUT_DIR_ENV)
    verify_output = Path(out_env) if out_env else (OUTPUT_ROOT / "_verify")

    # Point the shared snapshot dest at our isolated dir so build_stealth_driver
    # copies the login profile THERE (never .chrome_profile_snap), and suppress
    # the nuclear orphan sweep for this process.
    CHROME_PROFILE_SNAPSHOT = verify_profile
    _SUPPRESS_ORPHAN_SWEEP = True

    _vp(f"隔離模式啟動（generate={do_generate}）")
    _vp(f"隔離 profile 目的地：{verify_profile}")
    if do_generate:
        _vp(f"隔離輸出目錄：{verify_output}")

    try:
        email, password = read_credentials(AUTH_FILE)
    except Exception as err:  # pylint: disable=broad-except
        # `!r` 刻意保留：`try` 裡只有 `read_credentials(AUTH_FILE)`，碰不到
        # driver。收得到的只有兩種，兩種都不含憑證值本身：`OSError`（檔案不在／
        # 權限，`str()` 會印出憑證檔的完整路徑）與 `CredentialsError`（檔案在但
        # 內容不對，該類別的合約就是訊息只帶檔名、位元組位置或缺漏的欄位名）。
        # ⚠️ 這個列舉 2026-09-21 之前漏了 `UnicodeDecodeError`——它是 `ValueError`
        # 不是 `OSError`，而它的 `repr()` 會把解不開的那個位元組值印出來。現在
        # `read_credentials` 已經把它轉成 `CredentialsError`，所以列舉是完整的；
        # 哪天有人把那層轉換拿掉，這一行就會跟著變成假的。
        print(f"VERIFY-SETUP: FAIL 讀取憑證失敗：{err!r}", flush=True)
        return 2

    driver = None
    own_tree: dict[int, float | None] = {}
    try:
        _vp("建立隔離 driver（快照登入 profile、Selenium Manager 解析 driver）…")
        driver = build_stealth_driver()  # writes snapshot into verify_profile
        webdriver_wrapper_instance.current_webdriver = driver
        try:
            own_tree = _verify_capture_tree(driver.service.process.pid)
        except Exception:  # pylint: disable=broad-except
            own_tree = {}

        _vp("執行 production setup（登入 → 導航 → 模型 → 角色 → 解析度 → 取樣器）…")
        if not _setup_session(driver, email, password):
            print("VERIFY-SETUP: FAIL 登入失敗（profile 未登入或憑證無效）",
                  flush=True)
            return 4

        _vp("setup 完成；確認關鍵產圖控制項可達…")
        areas = ws.find_prompt_areas(port)
        _vp(f"  prompt 輸入區數量：{len(areas)}")
        if not areas:
            print("VERIFY-SETUP: FAIL 找不到主 prompt 輸入區（產圖 DOM 不可達）",
                  flush=True)
            return 5
        gen_btn = ws.find_generate_button(port)
        _vp(f"  Generate 按鈕：{'找到' if gen_btn else '找不到'}")
        if gen_btn is None:
            print("VERIFY-SETUP: FAIL 找不到 Generate 按鈕（產圖介面不可達）",
                  flush=True)
            return 6

        if do_generate:
            _vp("STRETCH：以隔離 prompt 產 1 張圖到隔離目錄…")
            ok, detail = _verify_generate_one(driver, verify_output)
            if not ok:
                print(f"VERIFY-SETUP: FAIL 隔離產圖失敗：{detail}", flush=True)
                return 7
            _vp(f"  隔離產圖成功：{detail}")

        print("VERIFY-SETUP: OK", flush=True)
        return 0
    except Exception as err:  # pylint: disable=broad-except
        # `full_error_detail`：這個 try 的第一個陳述就是 `build_stealth_driver()`，
        # 所以 spawn 失敗會落在這裡——而 selenium 的 `WebDriverException` 把訊息
        # 存在 `self.msg`、`args` 是空的，`!r` 印出來就是一對空括號。這是隔離驗證
        # 的鑑識出口：跑一次、不在任何迴圈裡、輸出由人／agent 直接讀，所以要完整
        # 訊息連同 `Stacktrace:`，不要截斷。
        print(f"VERIFY-SETUP: FAIL {full_error_detail(err)}", flush=True)
        return 1
    finally:
        # Surgical teardown: quit, then reap only OUR own driver tree (never a
        # nuclear sweep), and remove the isolated profile / output. We NEVER
        # call `_sync_chrome_profile_back` — the login profile must stay clean.
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # pylint: disable=broad-except  # nosec B110
                pass
        reaped = _verify_reap_tree(own_tree)
        if reaped:
            _vp(f"清理：回收本次 driver 行程樹殘留 {reaped} 個")
        _verify_rmtree(verify_profile)
        if do_generate:
            _verify_rmtree(verify_output)


# ---------- main (thin shell — orchestration lives in ws.run_batch, P6 C6) --

def main() -> int:
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
    driver = build_stealth_driver()
    webdriver_wrapper_instance.current_webdriver = driver
    try:
        return ws.run_batch(
            port, email, password,
            setup_fn=lambda: _setup_session(driver, email, password),
            minimize_fn=lambda: hide_chrome_window(port.driver),
            mode=mode)
    finally:
        # Use the CURRENT driver (port.driver) — a mid-run port.restart replaced
        # the original `driver` local. quit, then sync the login profile back.
        cur = port.driver
        snapshot_dir = getattr(cur, "_nai_snapshot_profile", None)
        try:
            cur.quit()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        if snapshot_dir:
            try:
                _sync_chrome_profile_back(snapshot_dir)
            except Exception as error:  # pylint: disable=broad-except
                # `!r` 刻意保留，理由同 `_restart_chrome_session` 裡那一站：
                # 這是 `finally` 的收尾，driver 已經 `quit()`，`try` 裡只剩檔案
                # I/O，而 `str(OSError)` 會帶出登入 profile 的完整路徑。
                print(f"_sync_chrome_profile_back failed: {error!r}",
                      file=sys.stderr)


if __name__ == "__main__":
    # Opt-in isolated verification mode (verify_browser.py --full). Unset in
    # every production launch, so this falls through to main() unchanged.
    if os.environ.get(VERIFY_MODE_ENV):
        sys.exit(_run_setup_verification())
    # 裸跑時沒有父行程發布存活訊號——見 `ws.claim_liveness_signal`。父行程
    # 寫過的話這層是 no-op；隔離驗證走上面那條分支，所以它不會認領 pid。
    sys.exit(ws.run_with_liveness_signal(main))
