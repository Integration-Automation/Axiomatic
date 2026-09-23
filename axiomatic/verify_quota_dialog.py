"""真瀏覽器驗證：把新的偵測／關閉 JS 丟進真的 DOM 跑。

單元測試只能驗 Python 那一側（假 port 回什麼就是什麼）；`_GENERATION_BLOCK_JS`、
`_BLOCKING_DIALOG_JS`、`_DISMISS_DIALOG_JS` 三段**真正的 JS** 一行都沒被執行過。
這支把它們放進真的 Chrome、真的 DOM 裡跑，重點是那條最貴的性質：

    關閉動作絕對不能按到會花錢的按鈕。

**站方會疊第二層對話框**（2026-09-07）：關掉付費牆之後露出一層帳號管理
（`Unsubscribe` / `Update Payment Details` / `Activate a Gift Key`），所以
`dismiss_blocking_dialog` 是「關到沒有為止」的有上限迴圈。那一層比第一層更危險
——第一層最壞是花錢，這一層最壞是**把訂閱退掉**，整條產線會停擺。

**判定點搬家了（2026-09-07 下午）。** `_DISMISS_DIALOG_JS` 現在只**挑**、不按：
它把元素交回 Python，由 driver 送出真的點選。所以那條最貴的性質從
「JS 不會 `click()` 到會花錢的按鈕」變成「JS 不會**回傳**會花錢的按鈕」，
每一個反例情境都斷言在 `_pick()` 的回傳值上——斷言在 `__clicks` 上等於什麼都沒驗
（新版裡那段 JS 一次都不按，click 記錄必然是空的）。

click 記錄仍然留著，但改問一個**不同**的問題：`_clicks_landed_within()`——「每一次
真的點擊有沒有落在我們交出去的那顆元素（或它的後代）裡面」。真點選的 `e.target` 是
「那個座標上最上層的元素」，所以交出 `<button>` 而記錄到它裡面那層 `<div>` 是**正確**
的；會出事的是落到那棵子樹**外面**，而那正好就是 W3C element click 的遮擋檢查要擋的
事。兩個問題都要問：回傳值管「挑對了沒」，落點管「按對了沒」。

沿用 `verify_browser.py` 的隔離規則：拋棄式 temp profile、先取 Chrome 槽、
**絕不** nuclear sweep，只精準回收自己那棵行程樹。不連任何外部網站
（about:blank ＋ JS 造 DOM），所以不會動到登入狀態、也不會花到額度。

跑法：`py -3 axiomatic/verify_quota_dialog.py`
結果行是機器可讀的：`VERIFY-QUOTA-DIALOG: OK (N checks)` / `... FAIL (n/N)`。

**JS 一律從 `_webrunner_shared` 匯入，不要在這裡另抄一份**——抄一份就只是在測
抄本，而這支存在的理由正是「真正上線的那段 JS 在真 DOM 裡的行為」。
（`_webrunner_shared` 是 CLAUDE.md 允許的被動共用模組、純 stdlib，所以這支仍然
沒有 import 任何一支 webrunner 變體，`verify_browser.py` 的獨立性不受影響。）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_browser as vb                                   # noqa: E402
import _webrunner_shared as ws                                # noqa: E402
from selenium import webdriver                                # noqa: E402
from selenium.common.exceptions import WebDriverException      # noqa: E402
from selenium.webdriver.chrome.service import Service         # noqa: E402
from selenium.webdriver.common.action_chains import ActionChains  # noqa: E402
from selenium.webdriver.common.keys import Keys               # noqa: E402
from urllib3.exceptions import (                              # noqa: E402
    ConnectTimeoutError as Urllib3ConnectTimeoutError,
    MaxRetryError as Urllib3MaxRetryError,
    NewConnectionError as Urllib3NewConnectionError,
    ProtocolError as Urllib3ProtocolError,
    ReadTimeoutError as Urllib3ReadTimeoutError,
)

FAILURES = []
CHECKS = [0]

# 機器可讀結果行的前綴。呼叫端靠這個 grep 出單行結論，所以它是**契約**，
# 不是排版——單一來源，不要在 f-string 裡再抄一次（`verify_browser.RESULT_PREFIX`
# 同一個理由）。
RESULT_PREFIX = "VERIFY-QUOTA-DIALOG:"


def check(ok, label, detail=""):
    CHECKS[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f" -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{label} ({detail})")


class Port:
    """最小的 BrowserPort：只給被測的那幾個函式用得到的東西。"""

    # **這一份必須跟上線那兩份（`webrunner_*.py` 的 `_DRIVER_TRANSPORT_ERRORS`）
    # 逐一相同。** 這支腳本存在的唯一理由是「把上線那份邏輯放進真 Chrome 真 DOM
    # 跑」，而被驅動的每一支 helper（`get_generation_block` /
    # `has_blocking_dialog` / `dismiss_blocking_dialog` /
    # `describe_dialog_controls` / `_is_chrome_crash_page`）都有
    # `except port.TRANSPORT_ERRORS`——tuple 不一樣，驗證跑的就是**另一份
    # transport 契約**。
    #
    # 2026-09-09 之前這裡是 `(WebDriverException, OSError)`，也就是修正前的形狀：
    # `MaxRetryError` / `NewConnectionError` / `ConnectTimeoutError` /
    # `ProtocolError` 的 MRO 都**不經過 `OSError`**（selenium 4.48.0 ／
    # urllib3 2.7.0 實測 `issubclass` 全 False），所以驗證期間 chromedriver 死掉
    # 時得到的是生的 urllib3 traceback，而不是分類過的 `BrowserGoneError`——
    # 操作者因此分不出「驗證工具自己掉了瀏覽器」與「被驗的 JS 壞了」，正好是這支
    # 腳本 SKIP／FAIL 分野在乎的那件事。
    #
    # **為什麼只能是第三份字面 tuple（不要「順手重構」成 import）：**
    #   * 不能 import 任何一支 webrunner 變體——本模組的 docstring 明寫它刻意保持
    #     獨立，`verify_browser.py` 的隔離性靠這一點；
    #   * 不能搬進 `_webrunner_shared`——那個模組是刻意 driver-agnostic 的（純
    #     stdlib，CLAUDE.md 的被動共用模組條款），`test_webrunner_shared.
    #     test_shared_is_driver_agnostic` 會直接紅。
    # 所以抄本是必要的，守門才是防線：`test_variant_parity` 掃全專案**非測試**
    # 模組裡每一個類別層的 `TRANSPORT_ERRORS`，逐一驗它抓得到
    # `_SESSION_GONE_EXC_NAMES` 的每一個名字。
    #
    # `ReadTimeoutError` 在 tuple 裡但**刻意不在** gone 名單裡：連上了、請求送出
    # 了、只是沒回話 ＝ chromedriver 還活著 ＝ 卡頓的正典。
    TRANSPORT_ERRORS = (
        WebDriverException,
        Urllib3ReadTimeoutError,        # 連上了但沒回話 → 卡頓（**不**算 gone）
        Urllib3MaxRetryError,           # 重試用盡（實測就是這一個）
        Urllib3NewConnectionError,      # 連不上（ConnectTimeoutError 的子類）
        Urllib3ConnectTimeoutError,     # 連線階段逾時
        Urllib3ProtocolError,           # 連線中途被對方斷掉
        OSError,                        # socket 層（ConnectionRefusedError…）
    )

    def __init__(self, driver):
        self.driver = driver

    def execute_script(self, script, *args):
        return self.driver.execute_script(script, *args)

    def current_url(self):
        return self.driver.current_url or ""

    def get_title(self):
        return self.driver.title or ""

    def refresh(self):
        self.driver.refresh()

    def click_native(self, element):
        # 鏡像兩個變體的 `BrowserPort.click_native`：W3C 的 element click。
        # **這支腳本存在的理由就是「真 DOM 裡的真行為」**，所以這條路一定要照跑，
        # 不能省成 `execute_script("arguments[0].click();")` 的合成點選——那樣測到
        # 的就是舊行為，而新舊差異正是要驗的東西。
        element.click()

    def press_escape(self):
        # 鏡像 `webrunner_novelai.BrowserPort.press_escape`：driver 層的真按鍵，
        # `isTrusted === true`。這支腳本存在的理由就是「真 DOM 裡的真行為」，所以
        # 這條路一定要照跑，不能省成合成事件。
        ActionChains(self.driver).send_keys(Keys.ESCAPE).perform()
        return True


# ---- 造 DOM 的 JS ----------------------------------------------------------
# 點選記錄器：dismiss 用的是 `el.click()`，會派發真的 click 事件，所以攔得到。
# 監聽器只能註冊**一次**：`document` 不會被 innerHTML 清掉，重複註冊會讓同一次
# 點選被記錄好幾筆（第一版就是這樣，看起來像「點了兩次 Cancel」）。
_SETUP = r"""
document.body.innerHTML = '';
window.__clicks = [];
window.__clickTags = [];
window.__clickProbes = [];
window.__clickTargets = [];
if (!window.__clickHook) {
  // 刻意**不用** closest('button')：dismiss 的候選已經放寬到 div / span，
  // 只認 button 的記錄器會把「按到了一個不是 button 的東西」記成沒按——那正好
  // 是這一版要驗的行為。改記 e.target 自己的標籤。
  //
  // （2026-09-07 一度往上收斂到控制項那一層，因為當時要改成 driver 層的真點選、
  //   而真點選的 `e.target` 是「那個座標上最上層的元素」。後來查出「關不掉」的
  //   真正成因是第二層對話框、合成點選其實有效，就退回來了。哪天真的要換成
  //   driver 點選，這一段要一起改回去。）
  window.__clickHook = (e) => {
    const el = e.target;
    // 元素本身也留著：真點選的 target 是「那個座標上最上層的元素」，所以要問的
    // 不是「target 是不是我們交出去的那顆」，而是「有沒有落在那顆的子樹裡面」。
    (window.__clickTargets = window.__clickTargets || []).push(el);
    (window.__clickTags = window.__clickTags || []).push(el.tagName);
    // `data-probe` 是**測試專用的身分標籤**，三條規則一個都不讀它
    // （`readableOf` 只讀 alt/aria-label/title，`identityOf` 只讀
    // id/name/data-testid/data-test/data-action），所以加了不會改變行為。
    // 需要它是因為要驗的那顆關閉鈕語意上完全是空的——沒有它，「按到了 #1」與
    // 「一個都沒按」在 `__clicks` 裡長得一模一樣（都是空字串）。
    (window.__clickProbes = window.__clickProbes || []).push(
      el.getAttribute('data-probe') || '?');
    window.__clicks.push(
      (el.innerText || el.getAttribute('aria-label')
       || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim());
  };
  document.addEventListener('click', window.__clickHook, true);
}
"""

_BUILD_DIALOG = """
const [text, buttons] = arguments;
const dlg = document.createElement('div');
dlg.setAttribute('role', 'dialog');
dlg.setAttribute('aria-modal', 'true');
const p = document.createElement('p');
p.textContent = text;
dlg.appendChild(p);
for (const b of buttons) {
  const btn = document.createElement('button');
  if (b.aria) btn.setAttribute('aria-label', b.aria);
  btn.textContent = b.text || '';
  dlg.appendChild(btn);
}
document.body.appendChild(dlg);
return true;
"""

_CLEAR = ("document.body.innerHTML = ''; window.__clicks = [];"
          " window.__clickTags = []; window.__clickProbes = [];"
          " window.__clickTargets = []; return true;")

# 「每一次真的點擊都落在這幾顆之中某一顆的子樹裡」。真點選打的是座標，`e.target`
# 會是那個座標上**最上層**的元素——交出 <button> 而記錄到它裡面那層 <div> 是對的。
# 真正的危險是落到子樹**外面**（有東西蓋在上面），這一段就是在問那件事。
_TARGETS_WITHIN = """
const roots = arguments[0].map(s => document.querySelector(s)).filter(Boolean);
const seen = window.__clickTargets || [];
if (!roots.length) return {ok: seen.length === 0, roots: 0, clicks: seen.length};
const ok = seen.every(t => roots.some(r => r === t || r.contains(t)));
return {ok: ok, roots: roots.length, clicks: seen.length};
"""

# 任意 HTML 的對話框。`_BUILD_DIALOG` 只造得出 <button>，而 2026-08-29 查出來的
# 問題正是「站方的關閉鈕不是 button」——只用 button 造的 DOM 永遠測不到它。
_BUILD_HTML_DIALOG = """
const html = arguments[0];
const dlg = document.createElement('div');
dlg.setAttribute('role', 'dialog');
dlg.setAttribute('aria-modal', 'true');
dlg.style.position = 'fixed';
dlg.style.left = '0px';
dlg.style.top = '0px';
dlg.style.width = '640px';
dlg.style.height = '420px';
dlg.innerHTML = html;
document.body.appendChild(dlg);
return true;
"""

# 尺寸可指定的對話框。情境 16 要**原樣重現**正式 log 裡那份 856x917 的購買對話
# 框——規則 3 的兩個門檻（尺寸上限、右上角範圍）都是相對於對話框自己的方框算的，
# 用固定 640x420 的殼重現不了那些比例。
#
# `position:fixed` ＋ `padding/border/margin: 0` 兩件事都是必要的：fixed 讓對話框
# 成為子元素 absolute 定位的 containing block，於是 HTML 裡寫的 left/top 就等於
# `describe_dialog_controls` 印出來的 dx/dy；歸零 padding/border 讓兩者不差幾像素。
_BUILD_SIZED_DIALOG = """
const [html, w, h] = arguments;
const dlg = document.createElement('div');
dlg.setAttribute('role', 'dialog');
dlg.setAttribute('aria-modal', 'true');
dlg.style.cssText = 'position:fixed;left:0px;top:0px;margin:0;padding:0;'
                  + 'border:0;background:#fff;'
                  + 'width:' + w + 'px;height:' + h + 'px';
dlg.innerHTML = html;
document.body.appendChild(dlg);
return true;
"""

# 「點了就把對話框移除」——真的關閉鈕的行為。用它來驗端到端：規則 3 命中之後
# `dismiss_blocking_dialog` 應該回 True，也就是**不必再退回整頁 reload**。
_ARM_CLOSER = """
const el = document.querySelector(arguments[0]);
el.addEventListener('click', () => {
  document.querySelectorAll('[role="dialog"]').forEach(n => n.remove());
});
return true;
"""
_PROBES = "return window.__clickProbes || [];"


def _pick(js):
    """跑**上線的那一份** `_DISMISS_DIALOG_JS`，回 `(action, element)`。

    這就是安全性質的判定點：JS 不再自己按，它把元素交回 Python，所以「絕不點到會
    花錢的控制項」現在等於「絕不**回傳**會花錢的控制項」。回 `'escape'` ＝ 一顆都
    沒挑中，呼叫端拿不到任何元素可按。
    """
    plan = js(ws._DISMISS_DIALOG_JS)
    if isinstance(plan, dict):
        return plan.get("action"), plan.get("el")
    return plan, None


def _ident(el):
    """挑中的是誰。優先 `data-probe`（測試專用標籤），否則使用者讀得到的字。"""
    if el is None:
        return None
    probe = el.get_attribute("data-probe")
    if probe:
        return probe
    return (el.text or el.get_attribute("aria-label")
            or el.get_attribute("title") or "").strip()


def _clicks_landed_within(js, *selectors):
    """每一次真的點選有沒有落在 `selectors` 其中一顆的子樹裡。回 `(ok, detail)`。"""
    info = js(_TARGETS_WITHIN, list(selectors))
    return bool(info.get("ok")), (
        f"roots={info.get('roots')} clicks={info.get('clicks')}")


def _abs_ctl(tag, probe, x, y, w, h, text="", attrs="", inner=""):
    """造一顆絕對定位的控制項，座標／尺寸直接對應 log 裡的 dx,dy 與 wxh。"""
    style = (f"position:absolute;left:{x}px;top:{y}px;width:{w}px;height:{h}px;"
             f"margin:0;padding:0;border:0;box-sizing:border-box;"
             f"cursor:pointer")
    return (f"<{tag} data-probe='{probe}' style='{style}' {attrs}>"
            f"{inner}{text}</{tag}>")


def _nested_ctl(outer_probe, inner_probe, x, y, w, h, text="",
                inner_attrs=""):
    """外層 <button> 包一層同尺寸的 <div>——正式 log 裡 #1/#2 與 #5/#6 的形狀。

    判斷「誰包誰」的依據：`describe_dialog_controls` 走
    `querySelectorAll('*')`（document pre-order）再用**穩定**排序照 corner 距離
    排，兩者 corner 完全相同 → 平手時保持 document 順序。log 裡 <button> 一律排在
    同座標的 <div> 前面，所以 button 是祖先、div 是後代。
    """
    inner = (f"<div data-probe='{inner_probe}' {inner_attrs} "
             f"style='display:block;width:100%;height:100%;margin:0;padding:0;"
             f"border:0;cursor:pointer'>{text}</div>")
    return _abs_ctl("button", outer_probe, x, y, w, h, inner=inner)


# **兩份文案都要留著，不是二選一**——它們守的是不同的 pattern。
#
# `QUOTA_TEXT` 是舊的猜想文案，守 `/not enough anlas/i`。2026-09-09 對
# `WEBRunner.log` 全檔量過：這句話在這個站台上**出現 0 次**，它從來沒有真的命中
# 過。留著是因為它零誤判、而且站方換回類似措辭時仍然接得住；但它是**猜的**，別
# 拿它當「驗證過的正式文案」。
QUOTA_TEXT = ("Not enough Anlas. Purchasing more Anlas lets you keep "
              "generating at this resolution.")

# `QUOTA_TEXT_REAL` 才是站方實際端出來的那一份：log 涵蓋 08-24 → 09-09、259 次
# 被擋，`[blocked] dialog text:` 的相異內容**只有這一種**。撇號是 U+2019（彎引
# 號）不是 ASCII `'`——直接打字很容易寫成直的，而 `/paint.{0,3}s run dry/i` 用
# `.{0,3}` 正是為了同時吃彎的／直的／沒有三種寫法。
QUOTA_TEXT_REAL = ("The paint’s run dry. You need a subscription or to "
                   "purchase Anlas to continue. Compare and pick the right "
                   "plan for you.")

# 正式 log（2026-09-03～09-07，每次額度用完印一次，內容完全一致）裡那份 856x917
# 的購買對話框，11 個可點候選原樣重現。#1/#2 就是那顆語意完全空白的關閉鈕。
#
# 說明文字用 `QUOTA_TEXT_REAL`：這份 fixture 的賣點就是「原樣重現 log 裡那一
# 份」，而先前它配的是**站方從來沒有端出來過的**舊猜想文案——按鈕幾何是真的、
# 文字是假的。改成真的那一份之後，這個情境才真的是端到端重現。
_REAL_DIALOG_W, _REAL_DIALOG_H = 856, 917
_REAL_DIALOG_HTML = (
    "<p style='position:absolute;left:40px;top:120px'>"
    + QUOTA_TEXT_REAL + "</p>"
    # #1 <button> / #2 <div>：右上角 32x32，text/aria/title 全空、連 svg 都沒有。
    + _nested_ctl("#1", "#2", 805, 21, 32, 32)
    + _abs_ctl("button", "#3", 410, 244, 126, 38, "Pay As You Go")
    + _abs_ctl("button", "#4", 306, 244, 100, 38, "Subscribe")
    + _nested_ctl("#5", "#6", 571, 513, 197, 42, "Get Started")
    + _nested_ctl("#7", "#8", 331, 514, 199, 42, "Get Started")
    # #9/#10 的 Anlas 只有 37x24——**尺寸條件單獨擋不住它們**，擋住的是「有文字」。
    + _abs_ctl("button", "#9", 57, 669, 37, 24, "Anlas")
    + _abs_ctl("button", "#10", 103, 763, 37, 24, "Anlas")
    + _abs_ctl("button", "#11", 21, 981, 108, 31, "Activate a Gift Key")
)
_PAYING_LABELS = ("Pay As You Go", "Subscribe", "Get Started", "Anlas",
                  "Activate a Gift Key")
_PAYING_PROBES = ("#3", "#4", "#5", "#6", "#7", "#8", "#9", "#10", "#11")

# 站方的**第二層**對話框（2026-09-07 16:10:22 的清單，420x322 / 5 個候選）。關掉
# 付費牆之後就露出這一層——而它比第一層更危險：第一層最壞是花錢，這一層最壞是
# **把訂閱退掉**，整條產線會停擺。右上角的關閉鈕形狀與第一層一模一樣。
_ACCOUNT_DIALOG_W, _ACCOUNT_DIALOG_H = 420, 322
_ACCOUNT_DIALOG_HTML = (
    "<p style='position:absolute;left:21px;top:60px'>Account</p>"
    + _nested_ctl("a1", "a2", 369, 21, 32, 32)
    + _abs_ctl("button", "a3", 21, 145, 378, 45, "Unsubscribe")
    + _abs_ctl("button", "a4", 21, 201, 378, 45, "Update Payment Details")
    + _abs_ctl("button", "a5", 21, 256, 378, 45, "Activate a Gift Key")
)
_ACCOUNT_LABELS = ("Unsubscribe", "Update Payment Details",
                   "Activate a Gift Key")

# 只把 keydown 監聽器掛在 **modal 元素本身**（focus-trap 類的函式庫很常這樣做）。
# 舊版 `_SEND_ESCAPE_JS` 只派給 `document`，事件只會往上冒泡到 window、不會往下
# 傳，所以這個監聽器永遠收不到——這就是 86 次全部關不掉的其中一個候選成因。
_ESC_CLOSES_ON_DIALOG = """
const dlg = document.querySelector('[role="dialog"]');
window.__escSeen = 0;
dlg.setAttribute('tabindex', '-1');
dlg.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { window.__escSeen++; dlg.remove(); }
});
return true;
"""
_CLICKS = "return window.__clicks || [];"

# 反面語料：正常頁面／非付費牆的文字，**一條 pattern 都不准命中**。
# 這一半跟正面一樣重要。Tier 1 誤判在批次路徑上不是「乾淨停止」——它走
# `wait_for_quota_recovery`，而 `quota_wait_max_sec` 預設 0 ＝ 無上限，所以一次
# 誤判就是每小時醒來一次、永不結束的等待迴圈，而且從事件串流上看起來跟真的額度
# 用完一模一樣。最後一筆是 log 實測的第二層帳號對話框（不是猜的）。
NON_BLOCKING_TEXTS = (
    ("導覽列",
     "Home Generate Image Text Adventure Shop Subscription Settings "
     "Account Log Out Anlas 1,234"),
    ("頁尾",
     "About Terms of Service Privacy Policy Refund Policy Contact Support "
     "Careers Subscription FAQ Anlas FAQ Shop © 2026 All rights reserved"),
    ("帳號設定頁",
     "Account Settings Email Change Password Two-Factor Authentication "
     "Subscription Tier Opus Renews on 2026-10-01 Manage Subscription "
     "Payment Method Visa ending 4242 Update Payment Details Billing History"),
    ("產圖介面",
     "Prompt Undesired Content Character 1 Character 2 Model Resolution "
     "Steps Guidance Sampler Seed Generate Anlas 1,234 "
     "Free generations remaining today: 26"),
    ("一般吐司",
     "Image saved to your gallery. Settings updated successfully. "
     "Prompt copied to clipboard."),
    # 方案比較頁：這一份是 `/pick the right plan/i` 被否決掉的直接理由——真正的
    # 方案頁就寫著那句話，而它的邊際貢獻是零。
    ("方案比較頁",
     "Explore Our Plans Tablet $10 /mo USD Opus $25 /mo USD Unlimited Images "
     "Image Gen Access Access to our image generation features. Compare plans "
     "and features side by side. Pick the right plan for you. "
     "Anlas Purchase Discount"),
    ("第二層帳號對話框（log 實測）",
     "Unsubscribe Update Payment Details Activate a Gift Key"),
)


def scenarios(port, drv):
    js = drv.execute_script

    # --- 1. 乾淨頁面：三個偵測都不得誤判 -----------------------------------
    print("\n1) 乾淨頁面（不得誤判）")
    js(_SETUP)
    check(js(ws._ALIVE_PROBE_JS) == ws._ALIVE_PROBE_TOKEN, "存活探針回 token")
    check(ws.get_generation_block(port) is None, "Tier 1 不誤判")
    check(ws.has_blocking_dialog(port) is None, "Tier 2 不誤判")
    check(ws.dismiss_blocking_dialog(port) is True, "沒有對話框時 dismiss 回 True")
    check(js(_CLICKS) == [], "沒有對話框時一個鍵都不點", js(_CLICKS))
    check(ws._is_chrome_crash_page(port) is False, "正常頁不是崩潰頁")

    # --- 2. 導覽列有 Purchase / Subscription 字樣，但不是對話框 -------------
    print("\n2) 頁面有 Purchase / Subscription 字樣但不在對話框裡（不得誤判）")
    js(_CLEAR)
    js("""
       document.body.innerHTML =
         '<nav><a href="#">Subscription</a><a href="#">Purchase Anlas</a>'
         + '<span>Not enough Anlas</span></nav>';
       return true;
       """)
    check(ws.get_generation_block(port) is None,
          "純頁面文字不算被擋", str(ws.get_generation_block(port))[:80])
    check(ws.has_blocking_dialog(port) is None, "沒有 modal 就不算被擋")

    # --- 3. 真實形狀的購買對話框：Purchase + Cancel ------------------------
    print("\n3) 購買對話框（Purchase Anlas / Cancel）")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_DIALOG, QUOTA_TEXT,
       [{"text": "Purchase Anlas"}, {"text": "Cancel"}])
    hit = ws.get_generation_block(port)
    check(bool(hit) and "Anlas" in (hit or {}).get("text", ""),
          "Tier 1 偵測到", str(hit)[:80])
    check(ws.has_blocking_dialog(port) is not None, "Tier 2 也偵測到")
    # 安全性質先驗**挑中了誰**——那是新的判定點。
    action, el = _pick(js)
    check(_ident(el) == "Cancel",
          "★ 挑中的是 Cancel，不是 Purchase Anlas",
          f"action={action!r} picked={_ident(el)!r}")
    closed = ws.dismiss_blocking_dialog(port)
    clicks = js(_CLICKS)
    check("Purchase Anlas" not in clicks,
          "★ 全程絕不按到 Purchase Anlas", f"clicks={clicks}")
    check(clicks == ["Cancel"], "只按了 Cancel", f"clicks={clicks}")
    # dismiss 之後對話框還在（這頁沒有人真的把它移除），所以回 False 是對的：
    # 「點了東西」不等於「關掉了」——這正是以結果為準那條規則。
    check(closed is False,
          "對話框沒真的消失時據實回 False", f"got {closed}")

    # --- 4. 只有付款按鈕：一個字都不能點 -----------------------------------
    print("\n4) 只有 Purchase 按鈕（必須改送 Escape、不點任何東西）")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_DIALOG, QUOTA_TEXT, [{"text": "Purchase Anlas"}])
    ws.dismiss_blocking_dialog(port)
    clicks = js(_CLICKS)
    check(clicks == [], "★ 沒有安全選項時一個鍵都不點", f"clicks={clicks}")

    # --- 5. 模稜兩可的 OK：在購買框上等於確認扣款，一樣禁點 ----------------
    print("\n5) 只有 OK 按鈕（模稜兩可 = 禁點）")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_DIALOG, QUOTA_TEXT, [{"text": "OK"}])
    ws.dismiss_blocking_dialog(port)
    clicks = js(_CLICKS)
    check(clicks == [], "★ 不點 OK", f"clicks={clicks}")

    # --- 6. aria-label 的關閉鈕（沒有文字）--------------------------------
    print("\n6) 只有 aria-label='Close' 的 × 鈕")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_DIALOG, QUOTA_TEXT,
       [{"text": "Purchase Anlas"}, {"text": "", "aria": "Close dialog"}])
    action, el = _pick(js)
    check(_ident(el) == "Close dialog",
          "★ 挑中 aria-label 的關閉鈕，不是 Purchase",
          f"action={action!r} picked={_ident(el)!r}")
    ws.dismiss_blocking_dialog(port)
    clicks = js(_CLICKS)
    check(clicks == ["Close dialog"],
          "按了 aria-label 的關閉鈕", f"clicks={clicks}")
    check("Purchase Anlas" not in clicks, "★ 仍然沒按 Purchase")

    # --- 7. 關掉之後偵測要跟著清空 ----------------------------------------
    print("\n7) 對話框真的被移除後，偵測要回 None")
    js("Array.from(document.querySelectorAll('[role=dialog]'))"
       ".forEach(n => n.remove()); return true;")
    check(ws.get_generation_block(port) is None, "Tier 1 已清空")
    check(ws.has_blocking_dialog(port) is None, "Tier 2 已清空")
    check(ws.dismiss_blocking_dialog(port) is True, "dismiss 回 True")

    # --- 8. 隱藏的對話框不算 ----------------------------------------------
    print("\n8) 隱藏（display:none）的對話框不算被擋")
    js(_CLEAR)
    js(_BUILD_DIALOG, QUOTA_TEXT, [{"text": "Cancel"}])
    js("document.querySelector('[role=dialog]')"
       ".style.display = 'none'; return true;")
    check(ws.get_generation_block(port) is None, "隱藏的不算 Tier 1")
    check(ws.has_blocking_dialog(port) is None, "隱藏的不算 Tier 2")

    # --- 9. 關閉鈕不是 <button>：包著 svg 的 div ---------------------------
    # 2026-08-29 的實測動機：86 次額度對話框沒有一次關得掉。舊選擇器只認
    # `button,[role=button],a[href="#"]`，站方的 × 是 div 就整個看不到。
    print("\n9) 關閉鈕是 <div aria-label='Close'>（不是 button）")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_HTML_DIALOG,
       "<p>" + QUOTA_TEXT + "</p>"
       "<button>Purchase Anlas</button>"
       "<div data-probe='closer' aria-label='Close' "
       "style='position:absolute;right:8px;top:8px;"
       "width:28px;height:28px'><svg width='16' height='16'></svg></div>")
    action, el = _pick(js)
    check(_ident(el) == "closer", "★ 挑得到非 button 的關閉鈕",
          f"action={action!r} picked={_ident(el)!r}")
    ws.dismiss_blocking_dialog(port)
    # 真點選打的是座標，這顆 div 的中心剛好被裡面那個 <svg> 蓋住，所以 `e.target`
    # 是 svg——那是**對的**（svg 是它的後代，事件照樣冒泡到 div 的 handler）。
    # 拿 `clicks == ["Close"]` 當斷言會在這裡誤紅：要問的是落點在不在子樹裡面。
    ok, detail = _clicks_landed_within(js, "[data-probe='closer']")
    check(ok, "★ 每一次點選都落在關閉鈕的子樹裡", detail)
    check("Purchase Anlas" not in js(_CLICKS), "★ 仍然沒按 Purchase")

    # --- 10. 外面包了一層的 Cancel：必須點到最內層 -------------------------
    print("\n10) <div tabindex><button>Cancel</button></div>：要點最內層")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_HTML_DIALOG,
       "<p>" + QUOTA_TEXT + "</p>"
       "<div tabindex='0'><button>Cancel</button></div>")
    # wrapper 的 innerText 一樣是 "Cancel"。合成點選的 target 就是 wrapper、不會
    # 往下傳，等於沒點；真點選雖然打得到裡面，但**交出去的元素本身**仍然要是最內層
    # ——W3C 的遮擋檢查是拿它當基準的。所以這一條釘在挑中的那顆上。
    action, el = _pick(js)
    check(el is not None and el.tag_name.lower() == "button",
          "★ 挑的是最內層的 <button>，不是外面那層 wrapper",
          f"picked tag={el.tag_name if el is not None else None!r}")
    ws.dismiss_blocking_dialog(port)
    clicks = js(_CLICKS)
    check(clicks == ["Cancel"], "按到最內層的 Cancel", f"clicks={clicks}")

    # --- 11. 放寬選擇器之後，付款控制項一樣一個都不准點 --------------------
    print("\n11) 付款控制項換成 div / span（放寬後仍不得點）")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_HTML_DIALOG,
       "<p>" + QUOTA_TEXT + "</p>"
       "<div aria-label='Purchase Anlas' tabindex='0'>買</div>"
       "<span onclick='void 0' title='Buy more Anlas'>Buy more Anlas</span>"
       "<div role='button'>Subscribe</div>"
       "<div tabindex='0'>Get Started</div>")
    action, el = _pick(js)
    check(action == "escape" and el is None,
          "★ 放寬元素形狀之後，付款控制項仍然一個都不挑",
          f"action={action!r} picked={_ident(el)!r}")
    ws.dismiss_blocking_dialog(port)
    clicks = js(_CLICKS)
    check(clicks == [], "★ 全程一個鍵都沒按", f"clicks={clicks}")

    # --- 12. Escape 監聽器掛在 modal 上（不是 document）--------------------
    print("\n12) keydown 監聽器只掛在 modal 元素上")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_HTML_DIALOG,
       "<p>" + QUOTA_TEXT + "</p><button>Purchase Anlas</button>")
    js(_ESC_CLOSES_ON_DIALOG)
    closed = ws.dismiss_blocking_dialog(port)
    check(js("return window.__escSeen || 0;") >= 1,
          "★ Escape 送得到掛在 modal 上的監聽器",
          "舊版只派給 document，事件不會往下傳，這個監聽器永遠收不到")
    check(closed is True, "對話框真的關掉了，不必退回整頁重新整理",
          f"got {closed}")
    check(js(_CLICKS) == [], "全程一個鍵都沒點", f"clicks={js(_CLICKS)}")

    # --- 13. 關不掉時的控制項清單：要有內容，而且不點任何東西 --------------
    print("\n13) 關不掉時列出控制項清單（純唯讀）")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_HTML_DIALOG,
       "<p>" + QUOTA_TEXT + "</p>"
       "<div role='button'>Subscribe</div>"
       "<div tabindex='0' title='Explore Our Plans'>Explore Our Plans</div>")
    inventory = ws.describe_dialog_controls(port)
    check(bool(inventory), "清單拿得到", repr(inventory)[:120])
    check("Subscribe" in inventory, "清單裡看得到控制項的文字",
          repr(inventory)[:200])
    check("cursor=" in inventory and "@(" in inventory,
          "清單有 cursor 與位置欄位（判斷哪個像關閉鈕要靠它們）",
          repr(inventory)[:200])
    check(js(_CLICKS) == [], "★ 列清單是唯讀的，一個都沒點", f"{js(_CLICKS)}")
    check(ws.has_blocking_dialog(port) is not None, "列完清單對話框還在")


    # --- 14. position:fixed 的對話框（舊判準對它整個瞎掉）------------------
    # `offsetParent` 對 `position: fixed` **一律回 null**，而真實站台的 modal
    # 幾乎都是 fixed。這一支是 2026-08-29 在真 DOM 裡撞出來的：情境 9～13 原本
    # 全部回空，因為 `_BUILD_HTML_DIALOG` 造的對話框是 fixed。
    print("\n14) position:fixed 的對話框仍然偵測得到")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_HTML_DIALOG, "<p>" + QUOTA_TEXT + "</p><button>Cancel</button>")
    check(js("return getComputedStyle("
             "document.querySelector('[role=dialog]')).position;") == "fixed",
          "情境本身確實是 fixed")
    check(js("return document.querySelector('[role=dialog]').offsetParent;")
          is None,
          "★ 舊判準（offsetParent）在這裡會回 null——也就是完全看不到對話框")
    check(ws.get_generation_block(port) is not None, "Tier 1 仍偵測得到")
    check(ws.has_blocking_dialog(port) is not None, "Tier 2 仍偵測得到")

    # --- 15. 藏起來的 fixed modal 殼不得誤判成「被擋住」---------------------
    # 放寬可見性判準的代價：站方頁面常留著 opacity:0 / 移到畫面外的 modal 殼。
    # 誤判成「有對話框」＝ 白等一小時，所以容器那一層用的是 onScreen。
    print("\n15) opacity:0 與移到畫面外的 fixed modal 不算被擋")
    for label, style in (("opacity:0", "opacity:0"),
                         ("移到畫面外", "left:-4000px;top:-4000px"),
                         ("visibility:hidden", "visibility:hidden")):
        js(_CLEAR)
        js(_BUILD_HTML_DIALOG, "<p>" + QUOTA_TEXT + "</p>")
        js("document.querySelector('[role=dialog]').style.cssText += "
           "arguments[0]; return true;", ";" + style)
        check(ws.get_generation_block(port) is None,
              f"{label} 的 modal 不算 Tier 1",
              str(ws.get_generation_block(port))[:80])
        check(ws.has_blocking_dialog(port) is None,
              f"{label} 的 modal 不算 Tier 2")

    # --- 16. 正式 log 裡那份 856x917 購買對話框，11 個控制項原樣重現 --------
    # 動機：2026-09-07 的實測是 **218/218 次**全部關不掉（13 天；log 裡另有 153 筆
    # 「dismissed」是舊版在驗證前就無條件印的假訊息，看得出真相的是「每天的 reload
    # 次數 == 當天的嘗試次數」這個結構矛盾）。`describe_dialog_controls`
    # 的清單已經回答了「站方有沒有放關閉鈕」——有，就是 #1/#2 那顆右上角 32x32，
    # 只是它語意上完全是空的（text/aria/title 全空、icon=0，X 是 CSS 畫的），
    # 規則 1（文字）與規則 2（aria-label）對它一律失效。
    print("\n16) 正式對話框（856x917 / 11 個控制項）：規則 3 要點到右上角那顆")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_SIZED_DIALOG, _REAL_DIALOG_HTML, _REAL_DIALOG_W, _REAL_DIALOG_H)
    check(ws.get_generation_block(port) is not None, "Tier 1 偵測得到")
    # 先確認情境本身真的重現了 log 裡的座標——重現不了的話，下面每一條斷言都是在
    # 測一個跟正式站台無關的 DOM。
    geo = js("const b = document.querySelector('[role=dialog]')"
             ".getBoundingClientRect();"
             "return ['#1', '#2'].map(p => {"
             "  const r = document.querySelector(\"[data-probe='\" + p + \"']\")"
             "    .getBoundingClientRect();"
             "  return [Math.round(r.left - b.left), Math.round(r.top - b.top),"
             "          Math.round(r.width), Math.round(r.height)];"
             "});")
    check(geo == [[805, 21, 32, 32], [805, 21, 32, 32]],
          "情境重現了 log 裡的 @(805,21) 32x32，而且 #1/#2 完全重疊",
          f"geo={geo}")
    check(js("return document.querySelector(\"[data-probe='#1']\")"
             ".contains(document.querySelector(\"[data-probe='#2']\"));")
          is True,
          "#1 是 #2 的祖先（log 的 document 順序推出來的巢狀關係）")
    action, el = _pick(js)
    check(isinstance(action, str) and action.startswith("clicked-corner:"),
          "規則 3 命中（不再落到 escape）", f"action={action!r}")
    check(_ident(el) == "#1",
          "★★ 交出去的是右上角那顆 32x32 @(805,21)",
          f"picked={_ident(el)!r} action={action!r}")
    check(el is not None and el.tag_name.lower() == "button",
          "★ 同座標同尺寸的 <button>/<div> 之中，交出去的是 <button>",
          f"tag={el.tag_name if el is not None else None!r}"
          " —— #2 是沒有任何屬性的裸 div，本來就不在 SELECTOR 裡")
    for probe in _PAYING_PROBES:
        check(_ident(el) != probe, f"★★ 絕不交出 {probe}",
              f"picked={_ident(el)!r}")
    for label in _PAYING_LABELS:
        check((el.text or "").strip() != label, f"★★ 絕不交出 {label}",
              f"picked text={(el.text or '').strip()!r}")

    print("\n16b) 同一份對話框，關閉鈕真的會關 → 端到端不必再 reload")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_SIZED_DIALOG, _REAL_DIALOG_HTML, _REAL_DIALOG_W, _REAL_DIALOG_H)
    js(_ARM_CLOSER, "[data-probe='#1']")
    check(ws.dismiss_blocking_dialog(port) is True,
          "★ 對話框真的關掉了——`wait_for_quota_recovery` 不會再走 reload 那條路")
    # 真點選打的是座標，`e.target` 會是最上層的元素——#1 裡面那層與它完全重疊的
    # 裸 <div>（#2）。**記錄到 #2 是對的**，事件照樣冒泡到 #1 的 handler（所以上一
    # 條端到端才會成立）。要問的是落點有沒有跑出 #1 的子樹。
    ok, detail = _clicks_landed_within(js, "[data-probe='#1']")
    check(ok, "★★ 每一次點選都落在關閉鈕的子樹裡（沒有被別的元素接走）", detail)
    check(js(_PROBES) in (["#1"], ["#2"]), "只按了那一下",
          f"probes={js(_PROBES)}")

    print("\n16c) 內層 div 也是候選時，innermost 挑內層，點選仍冒泡到外層 handler")
    # 站方哪天給內層 div 加上 tabindex，innermost 就會改挑內層。那仍然是對的：
    # click 會**往上**冒泡到外層 button 的 handler。（會出事的是相反方向——點到
    # wrapper 不會往下傳，所以 innermost 才存在。）
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_SIZED_DIALOG,
       "<p>" + QUOTA_TEXT + "</p>"
       + _nested_ctl("outer", "inner", 805, 21, 32, 32,
                     inner_attrs="tabindex='0'"),
       _REAL_DIALOG_W, _REAL_DIALOG_H)
    js(_ARM_CLOSER, "[data-probe='outer']")
    action, el = _pick(js)
    check(_ident(el) == "inner", "挑的是最內層", f"picked={_ident(el)!r}")
    check(ws.dismiss_blocking_dialog(port) is True,
          "★ 按內層 div，外層 button 的 handler 照樣收得到")
    ok, detail = _clicks_landed_within(js, "[data-probe='inner']")
    check(ok, "點選落在內層那顆裡面", detail)

    # --- 17. 近似但不該點：每一條件各自擋掉什麼 ----------------------------
    # 規則 3 是幾何 ＋ 語意空白的組合。這一組把條件逐一放到臨界值上，證明**每一
    # 條都在做事**——少了任何一條，下面就有一個情境會被點下去。
    print("\n17) 近似但不該點的反例")
    near_misses = [
        # (說明, HTML, 是哪一條擋住的)
        ("整片遮罩：右上角、無文字，但 856x917",
         _abs_ctl("button", "overlay", 0, 0, 856, 917),
         "尺寸上限 CORNER_MAX_PX"),
        # 中央那顆水平垂直**都**違反，所以它證明不了任一個方向單獨有效——下面
        # 「左上角」「右下角」兩顆才是各自只違反一個方向的隔離案例。兩道守門互相
        # 遮蔽的話，拿掉任一個方向都還是不會被點，變異測試會誤判成有守住。
        ("對話框正中央的無文字圖示鈕 32x32",
         _abs_ctl("button", "centre", 412, 442, 32, 32),
         "右上角範圍 CORNER_FRAC（水平與垂直都違反）"),
        ("右上角、無文字，但 aria-label='Buy more Anlas'",
         _abs_ctl("button", "aria-buy", 805, 21, 32, 32,
                  attrs="aria-label='Buy more Anlas'"),
         "語意空白（readableOf）"),
        ("右上角、無文字，但 title='Purchase Anlas'",
         _abs_ctl("button", "title-buy", 805, 21, 32, 32,
                  attrs="title='Purchase Anlas'"),
         "語意空白（readableOf）"),
        # tabindex 是為了讓它真的成為候選——沒有它，`<input>` 根本不在 SELECTOR
        # 裡，這一條就會因為「不是候選」而通過，測不到 readableOf 收 value。
        ("右上角、innerText 空，但 <input value='Purchase'>",
         _abs_ctl("input", "input-buy", 805, 21, 32, 32,
                  attrs="type='button' value='Purchase' tabindex='0'"),
         "語意空白（readableOf 收 value）"),
        ("右上角、無文字，但子節點 <img alt='Buy Anlas'>",
         _abs_ctl("button", "img-buy", 805, 21, 32, 32,
                  inner="<img alt='Buy Anlas' width='16' height='16'>"),
         "語意空白（readableOf 收子節點 alt）"),
        ("右上角、無文字，但 id='purchase-more'",
         _abs_ctl("button", "id-buy", 805, 21, 32, 32,
                  attrs="id='purchase-more'"),
         "FORBIDDEN 對 identityOf（縱深防禦）"),
        ("右上角、無文字，但 data-testid='subscribe-cta'",
         _abs_ctl("button", "testid-buy", 805, 21, 32, 32,
                  attrs="data-testid='subscribe-cta'"),
         "FORBIDDEN 對 identityOf（縱深防禦）"),
        ("右上角、無文字，但 49x49（剛好越過尺寸上限）",
         _abs_ctl("button", "too-big", 788, 21, 49, 49),
         "尺寸上限 CORNER_MAX_PX"),
        ("左上角、無文字 32x32（垂直過關、水平不過）",
         _abs_ctl("button", "top-left", 12, 21, 32, 32),
         "右上角範圍 CORNER_FRAC（水平）"),
        ("右下角、無文字 32x32（水平過關、垂直不過）",
         _abs_ctl("button", "bottom-right", 805, 860, 32, 32),
         "右上角範圍 CORNER_FRAC（垂直）"),
        ("右上角、無文字，但 1x1（看不見的殼）",
         _abs_ctl("button", "hairline", 836, 21, 1, 1),
         "最小尺寸下限"),
        # 2026-09-07 補進 FORBIDDEN 的三個字（`get started` / `gift key` /
        # `anlas`）。站方那兩層對話框上有三顆按鈕原本**完全只靠「文字必須為空」**
        # 擋住——連 `Unsubscribe` 那種「剛好含 subscribe」的第二層都沒有。這裡走的
        # 是 `identityOf` 那條路（id / data-testid），也就是**文字為空時**唯一還
        # 有作用的一層；文字那條路由 18／18b 的實際按鈕涵蓋。
        ("右上角、無文字，但 id='get-started'",
         _abs_ctl("button", "id-getstarted", 805, 21, 32, 32,
                  attrs="id='get-started'"),
         "FORBIDDEN 對 identityOf（2026-09-07 新增 get[- ]?started）"),
        ("右上角、無文字，但 data-testid='buy-anlas'",
         _abs_ctl("button", "id-anlas", 805, 21, 32, 32,
                  attrs="data-testid='buy-anlas'"),
         "FORBIDDEN 對 identityOf（anlas 是站方貨幣名）"),
        ("右上角、無文字，但 name='gift_key_activate'",
         _abs_ctl("button", "id-giftkey", 805, 21, 32, 32,
                  attrs="name='gift key activate'"),
         "FORBIDDEN 對 identityOf（2026-09-07 新增 gift[- ]?key）"),
    ]
    for label, html, blocked_by in near_misses:
        js(_CLEAR)
        js(_SETUP)
        js(_BUILD_SIZED_DIALOG, "<p>" + QUOTA_TEXT + "</p>" + html,
           _REAL_DIALOG_W, _REAL_DIALOG_H)
        # 判準是「JS **回傳**了什麼」。回 'escape' ＝ 一顆都沒挑中，呼叫端不會拿到
        # 任何元素可按——這才是新版的安全性質；「沒有 click 事件」在新版裡必然成立，
        # 拿它當斷言等於什麼都沒驗。
        plan = js(ws._DISMISS_DIALOG_JS)
        check(plan == "escape",
              f"★ 不挑中：{label}",
              f"應由「{blocked_by}」擋掉，實際回傳={plan!r}")

    print("\n17b) 正面對照：邊界之內的關閉鈕仍然挑得到")
    positives = [
        ("右上角 48x48（尺寸上限之內）",
         _abs_ctl("button", "ok48", 789, 21, 48, 48)),
        ("靠近右上角範圍邊界（right/top 都接近 20% 的界線）",
         _abs_ctl("button", "edge", 670, 175, 22, 22)),
        ("關閉鈕是 <div tabindex>，不是 button",
         _abs_ctl("div", "divx", 805, 21, 32, 32, attrs="tabindex='0'")),
    ]
    for label, html in positives:
        js(_CLEAR)
        js(_SETUP)
        js(_BUILD_SIZED_DIALOG, "<p>" + QUOTA_TEXT + "</p>" + html,
           _REAL_DIALOG_W, _REAL_DIALOG_H)
        plan = js(ws._DISMISS_DIALOG_JS)
        ok = (isinstance(plan, dict)
              and str(plan.get("action", "")).startswith("clicked-corner:")
              and plan.get("el") is not None)
        check(ok, f"挑得到：{label}", f"回傳={plan!r}")

    print("\n17c) 規則優先序：有明確的 Cancel 就走規則 1，不走規則 3")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_SIZED_DIALOG,
       "<p>" + QUOTA_TEXT + "</p>"
       + _abs_ctl("button", "corner", 805, 21, 32, 32)
       + _abs_ctl("button", "cancel", 300, 400, 100, 38, "Cancel"),
       _REAL_DIALOG_W, _REAL_DIALOG_H)
    plan = js(ws._DISMISS_DIALOG_JS)
    picked = (plan.get("el").get_attribute("data-probe")
              if isinstance(plan, dict) and plan.get("el") is not None
              else None)
    check(isinstance(plan, dict) and plan.get("action") == "clicked:Cancel"
          and picked == "cancel",
          "明確的文字命中優先於幾何猜測", f"回傳={plan!r} picked={picked!r}")

    print("\n18) 站方的第二層對話框（帳號管理）——比第一層更危險")
    # 2026-09-07 實測：規則 3 把 856x917 的付費牆關掉之後，露出一層 420x322 的
    # 帳號管理對話框，右上角同樣有一顆 @(369,21) 32x32 的無字關閉鈕。
    # 這一層最壞的後果不是花錢，是**把訂閱退掉**——整條產線會停擺。
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_SIZED_DIALOG, _ACCOUNT_DIALOG_HTML,
       _ACCOUNT_DIALOG_W, _ACCOUNT_DIALOG_H)
    action, el = _pick(js)
    check(isinstance(action, str) and action.startswith("clicked-corner:"),
          "規則 3 在第二層也認得出右上角那顆", f"action={action!r}")
    check(_ident(el) == "a1", "★ 交出去的是 @(369,21) 32x32 那顆",
          f"picked={_ident(el)!r}")
    picked, clicks = _ident(el), js(_CLICKS)
    for probe, label, why in (
            ("a3", "Unsubscribe", "FORBIDDEN 含 subscribe（**剛好**擋到，"
                                  "不是特意擋的——縮窄 FORBIDDEN 前要先想到它）"),
            ("a4", "Update Payment Details", "FORBIDDEN 含 payment"),
            ("a5", "Activate a Gift Key", "不含任何 FORBIDDEN 字眼；擋住它的是"
                                          "白名單制本身（DISMISS 要完整比對、"
                                          "規則 3 要語意空白）")):
        check(picked != probe and label not in clicks,
              f"★★ 絕不交出 {label}", f"（{why}）picked={picked!r} clicks={clicks}")

    print("\n18a) 縱深防禦：把「文字必須為空」拿掉之後，FORBIDDEN 仍要擋住每一顆")
    # 這一段驗的是**新增的價值**，不是現狀。(a)「文字必須為空」同時是規則 3 的
    # **功能**條件（關閉鈕是純圖示鈕），所以放寬它的人不會意識到自己在動安全性質。
    # 這裡在真 DOM 上把 (a) 拿掉，確認 FORBIDDEN 自己接得住兩層對話框上的每一顆。
    # 2026-09-07 補 `get started` / `gift key` / `anlas` 進 FORBIDDEN 之前，這一段
    # 會紅——那三顆原本完全只靠 (a) 擋著。
    no_empty_rule = ws._DISMISS_DIALOG_JS.replace(
        "    if (readableOf(el)) return false;\n", "")
    check(no_empty_rule != ws._DISMISS_DIALOG_JS,
          "找得到「文字必須為空」那一行（找不到的話這一段就什麼都沒驗）")
    for label, w, h, html in (
            ("第一層付費牆", _REAL_DIALOG_W, _REAL_DIALOG_H, _REAL_DIALOG_HTML),
            ("第二層帳號管理", _ACCOUNT_DIALOG_W, _ACCOUNT_DIALOG_H,
             _ACCOUNT_DIALOG_HTML)):
        js(_CLEAR)
        js(_SETUP)
        js(_BUILD_SIZED_DIALOG, html, w, h)
        # 把右上角那顆合法的關閉鈕拿掉，逼規則 3 在沒有 (a) 的情況下只剩會花錢的
        # 按鈕可挑；不拿掉的話它會挑中關閉鈕，這一段就驗不到 FORBIDDEN。
        js("document.querySelectorAll(\"[data-probe='#1'],[data-probe='#2'],"
           "[data-probe='a1'],[data-probe='a2']\").forEach(n => n.remove());"
           "return true;")
        # 全部搬到右上角、縮成 32x32，讓幾何條件也不再幫忙擋——這樣唯一還站著的
        # 就只有 FORBIDDEN。
        js("let i = 0;"
           "for (const el of document.querySelectorAll('[data-probe]')) {"
           "  el.style.left = '" + str(w - 35) + "px';"
           "  el.style.top = (2 + (i++) * 3) + 'px';"
           "  el.style.width = '32px'; el.style.height = '32px';"
           "} return true;")
        # 判準只看**回傳值**。`js(_CLICKS) == []` 在新版裡必然成立（那段 JS 一次
        # 都不按），拿它當斷言就是自我安慰。
        plan = js(no_empty_rule)
        check(plan == "escape",
              f"★★ {label}：拿掉「文字必須為空」之後仍然一顆都不交出去",
              f"回傳={plan!r}——代表那一顆只剩單一條件保護，FORBIDDEN 需要補字")

    print("\n18b) 疊起來的兩層：要關到沒有為止，不是關一層就回報失敗")
    # 這是 2026-09-07 那五次「關不掉」的真正成因：第一層真的被關掉了，
    # `has_blocking_dialog` 看到第二層就回報失敗，退化成整頁 reload。
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_SIZED_DIALOG, _REAL_DIALOG_HTML, _REAL_DIALOG_W, _REAL_DIALOG_H)
    # 按下第一層的關閉鈕 → 移除第一層、長出第二層（站方的實際行為）。
    js("""
       const html = arguments[0], w = arguments[1], h = arguments[2];
       const first = document.querySelector('[role="dialog"]');
       const closer = document.querySelector("[data-probe='#1']");
       closer.addEventListener('click', () => {
         first.remove();
         const second = document.createElement('div');
         second.setAttribute('role', 'dialog');
         second.setAttribute('aria-modal', 'true');
         second.style.cssText = 'position:fixed;left:0px;top:0px;margin:0;'
           + 'padding:0;border:0;background:#fff;width:' + w + 'px;height:'
           + h + 'px';
         second.innerHTML = html;
         document.body.appendChild(second);
         const c2 = second.querySelector("[data-probe='a1']");
         c2.addEventListener('click', () => second.remove());
       });
       return true;
       """, _ACCOUNT_DIALOG_HTML, _ACCOUNT_DIALOG_W, _ACCOUNT_DIALOG_H)
    check(ws.dismiss_blocking_dialog(port) is True,
          "★ 兩層都關掉了——不必再退回整頁 reload ＋ 重填欄位")
    probes, clicks = js(_PROBES), js(_CLICKS)
    # 第二層是被第一層的 handler 建出來的，所以第一顆的子樹在第二輪已經從 DOM 拿掉
    # 了，`_clicks_landed_within` 這裡查不到 root——改用 probe：每一下都落在兩顆
    # 關閉鈕（或它們裡面那層裸 div）之中。
    check(len(probes) == 2 and set(probes) <= {"#1", "#2", "a1", "a2"},
          "★ 兩下都落在各自那層的右上角關閉鈕上", f"probes={probes}")
    check(probes[0] in ("#1", "#2") and probes[1] in ("a1", "a2"),
          "★ 順序正確：先第一層、再第二層", f"probes={probes}")
    for label in _PAYING_LABELS + _ACCOUNT_LABELS:
        check(label not in clicks, f"★★ 全程絕不按 {label}", f"clicks={clicks}")
    check(ws.has_blocking_dialog(port) is None, "畫面上真的沒有對話框了")

    print("\n18c) 按不動的關閉鈕：畫面沒變就立刻收手，不要空轉整組 round")
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_SIZED_DIALOG, _REAL_DIALOG_HTML, _REAL_DIALOG_W, _REAL_DIALOG_H)
    check(ws.dismiss_blocking_dialog(port) is False,
          "關不掉就要據實回 False")
    probes = js(_PROBES)
    check(len(probes) == 2 and set(probes) <= {"#1", "#2"},
          "★ 第 2 輪就該發現「畫面一個字都沒變」並收手（總共只按 2 次）",
          f"probes={probes}——沒有這個判準的話會白按滿 max_rounds 輪，"
          "每一輪還多付一次 human_pause")
    ok, detail = _clicks_landed_within(js, "[data-probe='#1']")
    check(ok, "兩下都落在關閉鈕的子樹裡", detail)

    # --- 19. 文案漂移：兩份文案都要接得住，正常頁面文字一條都不准命中 --------
    # 這一段跑的是**真的瀏覽器 + 真的 innerText**，跟 `test_webrunner_shared` 那
    # 支純 regex 的守門互補：那一支證明「pattern 對字串的判定」，這一支證明
    # 「pattern 在真 DOM 上讀到的文字」也一樣——`innerText` 會做空白正規化、會受
    # CSS 影響，兩者不是同一件事。
    print("\n19) 文案漂移：兩份文案都接得住，正常頁面文字不誤判")
    for label, text in (("舊文案（猜的，log 裡 0 次）", QUOTA_TEXT),
                        ("實際文案（log 裡 259 次）", QUOTA_TEXT_REAL)):
        js(_CLEAR)
        js(_SETUP)
        js(_BUILD_DIALOG, text, [{"text": "Cancel"}])
        hit = ws.get_generation_block(port)
        check(hit is not None, f"★ Tier 1 接得住{label}", str(hit)[:120])

    # 反面：同一段文字放進**真的對話框**裡（不是放在頁面上），連容器條件都幫不了
    # 忙，只剩 pattern 自己說話。誤判的代價是每小時醒來一次、永不結束的等待迴圈。
    for label, text in NON_BLOCKING_TEXTS:
        js(_CLEAR)
        js(_SETUP)
        js(_BUILD_DIALOG, text, [{"text": "Close"}])
        hit = ws.get_generation_block(port)
        check(hit is None, f"★ 不誤判：{label}",
              f"命中了 {(hit or {}).get('pattern')}")

    # 正面對照：上面那七筆全部不命中，可能是因為 pattern 全被改壞了（那樣也會
    # 「零誤判」）。所以緊接著再確認偵測本身仍然是活的。
    js(_CLEAR)
    js(_SETUP)
    js(_BUILD_DIALOG, QUOTA_TEXT_REAL, [{"text": "Cancel"}])
    check(ws.get_generation_block(port) is not None,
          "正面對照：偵測仍然是活的（否則上面七個「不誤判」全是假的）")

    # --- 20. 超過 1200 字元的整張定價表：兩層守門不可以共用同一個上限 --------
    # 2026-09-09 修掉的缺陷：Tier 1 與 Tier 2 **兩邊都有**同一行
    # `if (!text || text.length > 1200) continue;`。於是一個 innerText 超過 1200
    # 的 modal 會同時讓 Tier 1 跳過它、Tier 2 回 None——兩層一起瞎掉，而「兩層」
    # 正是這個設計的全部價值。Tier 2 的漏判還不只是漏偵測：
    # `dismiss_blocking_dialog` 把 None 讀成「關乾淨了」，所以它會對著一個它根本
    # 沒碰到的對話框回報成功。
    #
    # 站方的付費牆正好是最容易超過上限的形狀（整張定價表）。這一段跑的是**真的
    # innerText**——長度是瀏覽器算的，不是 Python 算的，這正是純 regex 那一支守門
    # 驗不到的部分。
    print("\n20) 超過 1200 字元的付費牆：Tier 1 略過它，Tier 2 仍須看得見")
    js(_CLEAR)
    js(_SETUP)
    _long_text = QUOTA_TEXT_REAL + " " + (
        "Explore Our Plans Tablet $10 /mo USD Get Started Opus $25 /mo USD "
        "Get Started Unlimited Images Image Gen Access Access to our image "
        "generation features. Anlas Purchase Discount The amount taken off "
        "our on-demand Anlas purchases. Text Gen Access Custom AI Modules "
        "Image Director Tools Included Included " * 6)
    js(_BUILD_DIALOG, _long_text, [{"text": "Subscribe"}])
    _measured = js("return document.querySelector('[role=\"dialog\"]')"
                   ".innerText.replace(/\\s+/g, ' ').trim().length;")
    check(_measured > 1200,
          "fixture 的真 innerText 真的超過 1200（否則下面全是空驗）",
          f"實測 {_measured} 字元")
    check(ws.get_generation_block(port) is None,
          "Tier 1 因為長度略過它（刻意保留：它的 selector 很寬，需要這道上限）",
          str(ws.get_generation_block(port))[:80])
    _seen = ws.has_blocking_dialog(port)
    check(_seen is not None,
          "★ Tier 2 仍然看得見它——它的 selector 已經收斂過，長度不做收斂工作")
    check(bool(_seen) and "full length" in str(_seen)
          and str(_measured) in str(_seen),
          "★ 截斷時把**全文**長度帶出來，餘裕才是量到的數字",
          repr(_seen)[:160])
    check(ws.dismiss_blocking_dialog(port) is False,
          "★ 關不掉的長對話框不得回報「關乾淨了」——那會讓呼叫端跳過整頁 reload")
    check(js(_CLICKS) == [],
          "整張定價表上只有付款按鈕，一個都不准點", str(js(_CLICKS))[:120])


def run():
    import tempfile
    profile = tempfile.mkdtemp(prefix="verify_quota_profile_")
    print(f"暫時 profile：{profile}")
    drv = None
    own = set()
    try:
        drv = webdriver.Chrome(service=Service(),
                               options=vb._make_options(profile, True))
        own = vb._capture_own_tree(
            getattr(getattr(drv, "service", None), "process", None)
            and drv.service.process.pid)
        drv.set_page_load_timeout(vb.PAGE_LOAD_TIMEOUT_SEC)
        drv.set_script_timeout(vb.SCRIPT_TIMEOUT_SEC)
        # headless 的預設視窗只有 800x600，而情境 16 重現的是 856x917 的對話框。
        # `visible()` 不看視窗範圍（長對話框裡捲出去的按鈕仍要點得到），所以這不是
        # 正確性需求；但讓對話框整個在視窗裡，重現出來的畫面才跟正式那份一致。
        try:
            drv.set_window_size(1280, 1024)
        except Exception:                          # noqa: BLE001
            pass
        drv.get("about:blank")
        scenarios(Port(drv), drv)
    finally:
        if drv is not None:
            try:
                drv.quit()
            except Exception:                      # noqa: BLE001
                pass
        vb._reap_pids(own)
        vb._rm_profile(profile, 1)
    print(f"\n{'=' * 60}")
    if FAILURES:
        print(f"{RESULT_PREFIX} FAIL ({len(FAILURES)}/{CHECKS[0]})")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"{RESULT_PREFIX} OK ({CHECKS[0]} checks)")
    return 0


def main() -> int:
    """跑一輪並回結束碼。**硬性契約：一定要印一行 `RESULT_PREFIX` 開頭的結論。**

    `run()` 自己會在正常收尾時印那一行，但它逃得掉的路不只一條——Chrome 起不來、
    `_run_with_slot` 取不到槽、selenium 丟非預期例外。那些情況下原本的寫法
    （`sys.exit(vb._run_with_slot(...))`）給呼叫端的是 traceback ＋ 非零結束碼、
    **沒有結果行**，於是「那段 JS 有問題」與「這台機器起不了瀏覽器」在輸出上長得
    一模一樣。這支存在的理由就是回答前者，所以那個混淆特別貴。

    這條與 `verify_browser.main()` 是同一條契約、同樣的理由，措辭刻意一致。
    """
    try:
        return vb._run_with_slot("quota-dialog", run)
    except Exception as err:                       # noqa: BLE001
        # 例外文字用 `!r`：這一行會被貼進工作紀錄，而 `str(OSError)` 會把完整的
        # 主機路徑帶出來（本專案記過這條）。
        print(f"{RESULT_PREFIX} FAIL (unexpected exception: {err!r})")
        return 1


if __name__ == "__main__":
    # 先硬化主控台再做任何事：這支的契約是一定要印出結論那一行，而一個編不出來
    # 的字元會讓行程死在半路（見 `verify_browser._harden_console`）。
    vb._harden_console()
    sys.exit(main())
