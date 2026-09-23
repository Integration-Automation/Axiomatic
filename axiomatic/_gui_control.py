"""桌面自動化原語（bot-only helper，被動模組）。

`discord_bot.py` 的 GUI 控制指令（滑鼠 / 視窗 / 剪貼簿 / 螢幕 / 文字與圖片
定位 / 巨集 / shell）把「怎麼做」放在這裡，只留「怎麼回話」在 bot。

設計約束：

* **自動化本身不在這裡實作。** 打字、文字辨識與跨詞比對、樣板比對、UI 元素樹、
  截圖的 DPI 換算——全部轉呼叫桌面自動化函式庫（`je_auto_control`，本機以可編輯
  模式指向原始碼樹）。函式庫缺什麼就補進函式庫，不要在這裡繞過去：同一件事有
  兩份實作，修好的永遠只有其中一份。本模組留下的是**參數解析**、**本機環境
  政策**（辨識引擎路徑、語言資料位置、選語言）、**去識別化**，以及需要 event
  loop 才能做的**中止與逾時**。
* **不 import `discord`、不 import `discord_bot`**（後者會循環）。本模組只用
  stdlib ＋ 選用的 `je_auto_control` / `psutil` / `Pillow`，所以
  可以直接被測試載入、也可以被別的行程重用。
* **全部是同步阻塞函式。** bot 是單一 event loop，呼叫端必須用
  `asyncio.to_thread` 包起來；`wait_text` / `wait_window` / `run_shell` 動輒
  數秒到數分鐘，直接在 loop 上跑會讓整個 bot 失去回應。
* **錯誤一律轉成 `GuiError`，訊息由本模組自己寫死**（泛用、不含主機路徑 /
  外部服務名 / 原始例外文字），呼叫端可以直接把 `str(error)` 回給使用者而不
  違反 CLAUDE.md 的 Secrecy 硬性規定。真正的細節由呼叫端自行寫 stderr。

**多螢幕陷阱**：`je_auto_control.screen_size()` 只回主螢幕解析度，
`PIL.ImageGrab.grab()` 預設也只截主螢幕，但滑鼠座標是整個虛擬桌面的座標
（副螢幕在右邊時 x 會超過主螢幕寬度）。所以 `screen_info()` 同時回報主螢幕與
虛擬桌面範圍，截圖預設仍是主螢幕、要整個虛擬桌面得明講 `all`。
"""
from __future__ import annotations

import json
import math
import ntpath
import os
import re
import subprocess  # nosec B404 — `!sh` 的執行後端，呼叫端限擁有者
import sys
import time
from pathlib import Path
from typing import Any, Callable, NamedTuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MACRO_DIR = PROJECT_ROOT / "macros"


class GuiError(Exception):
    """使用者可見的失敗。訊息保證是本模組寫死的泛用字串，可直接回給對話平台。"""


class GuiAborted(GuiError):
    """使用者主動要求中止。

    刻意繼承 `GuiError`：不知道有這個型別的呼叫端照樣接得到、照樣拿得到一句安全
    的訊息。知道的呼叫端（巨集執行器）則分開處理——「被停下來」不是「失敗」，
    回報成失敗會讓 `!macro stop` 每次都印一行紅字。
    """


# --------------------------------------------------------------------------
# 後端載入
# --------------------------------------------------------------------------
# je_auto_control 的 import 有明顯成本（會拉 cv2 / numpy），而且在沒有桌面
# session 的環境會直接爆。所以延遲載入 ＋ 快取，並把失敗折成 GuiError。
_AC: Any = None
_AC_TRIED = False


def load_ac() -> Any:
    """回傳 je_auto_control 模組；載不進來就丟 `GuiError`。"""
    global _AC, _AC_TRIED  # pylint: disable=global-statement
    if not _AC_TRIED:
        _AC_TRIED = True
        try:
            import je_auto_control as _module  # type: ignore
            _AC = _module
        except Exception as error:  # pylint: disable=broad-except
            print(f"[gui] je_auto_control unavailable: {error!r}", file=sys.stderr)
            _AC = None
    if _AC is None:
        raise GuiError("桌面控制功能未安裝或無法在此環境使用。")
    return _AC


def _window_api():
    """回傳套件的視窗操作模組；不在 Windows 或載不進來就丟 `GuiError`。

    這裡曾經改走 pywin32，因為套件的 `list_windows()` 回的 hwnd 是
    `ctypes.LP_c_long` 指標物件而不是 int（`int(hwnd)` 會丟 `ValueError`），
    而且 `close_window_by_title` 做的其實是最小化。兩個都已在套件端修掉
    （回呼原型改成 `HWND`、`close` 改送 `WM_CLOSE` 並另開 `minimize`），
    所以這裡回到單一實作。
    """
    try:
        from je_auto_control.wrapper import auto_control_window  # type: ignore
        return auto_control_window
    except ImportError as error:
        raise GuiError("視窗控制功能未安裝（僅 Windows 可用）。") from error


# --------------------------------------------------------------------------
# 參數解析
# --------------------------------------------------------------------------
COORD_MAX = 65535
HOTKEY_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_]+$")
MACRO_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

MOUSE_BUTTONS = {
    "left": "mouse_left",
    "l": "mouse_left",
    "right": "mouse_right",
    "r": "mouse_right",
    "middle": "mouse_middle",
    "m": "mouse_middle",
    # 側鍵。很多程式把「上一頁 / 下一頁」綁在這兩顆上，少了它們就有一類操作
    # 只能繞路用快捷鍵模擬。
    "x1": "mouse_x1",
    "back": "mouse_x1",
    "x2": "mouse_x2",
    "forward": "mouse_x2",
}


def parse_coord(raw: str, label: str) -> int:
    """把單一座標字串轉成 int，超出 ±COORD_MAX 就丟 `GuiError`。

    **允許負數**：座標是整個虛擬桌面的座標，副螢幕擺在主螢幕左邊或上面時，
    上面那半的 x / y 本來就是負的（本機實測虛擬桌面從 y = -164 起算）。把下限
    釘在 0 會讓那些位置永遠點不到。
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as error:
        raise GuiError(f"{label} 必須是整數。") from error
    if not -COORD_MAX <= value <= COORD_MAX:
        raise GuiError(f"{label} 必須介於 -{COORD_MAX} 到 {COORD_MAX}。")
    return value


def parse_size(raw: str, label: str) -> int:
    """寬 / 高：必須是正整數。"""
    value = parse_coord(raw, label)
    if value <= 0:
        raise GuiError(f"{label} 必須大於 0。")
    return value


def parse_xy(parts: list[str]) -> tuple[int, int]:
    """`["500", "300"]` → `(500, 300)`。"""
    if len(parts) != 2:
        raise GuiError("需要兩個座標值：`<x> <y>`。")
    return parse_coord(parts[0], "x"), parse_coord(parts[1], "y")


def parse_button(raw: str) -> str:
    """把 `left` / `r` / `middle` 之類轉成 je_auto_control 的按鍵名。"""
    key = (raw or "left").strip().lower()
    if key not in MOUSE_BUTTONS:
        raise GuiError(
            "滑鼠鍵只能是 `left` / `right` / `middle` / `back` / `forward`。")
    return MOUSE_BUTTONS[key]


# 底層鍵名表用的是 Win32 虛擬鍵的原始名稱，跟一般人（與本專案所有說明文件）
# 寫的名字對不上：表裡**沒有** `ctrl`／`alt`／`enter`／`esc`／`win`／
# `backspace`，只有 `control`／`menu`／`return`／`escape`／`lwin`／`back`。
# 沒有這層對應的話，`!hotkey ctrl+s`、`!hotkey alt+f4`、巨集裡的 `hotkey enter`
# ——也就是文件裡每一個範例——全部會失敗。
KEY_ALIASES = {
    "ctrl": "control", "lctrl": "lcontrol", "rctrl": "rcontrol",
    "alt": "menu", "lalt": "lmenu", "ralt": "rmenu",
    "enter": "return",
    "esc": "escape",
    "win": "lwin", "super": "lwin", "cmd": "lwin", "meta": "lwin",
    "backspace": "back", "bksp": "back",
    "del": "delete", "ins": "insert",
    "pgup": "prior", "pageup": "prior",
    "pgdn": "next", "pagedown": "next",
    "caps": "capital", "capslock": "capital",
    "printscreen": "snapshot", "prtsc": "snapshot", "prtscr": "snapshot",
    "scrolllock": "scroll",
    "numpad0": "num0", "numpad1": "num1", "numpad2": "num2",
    "numpad3": "num3", "numpad4": "num4", "numpad5": "num5",
    "numpad6": "num6", "numpad7": "num7", "numpad8": "num8",
    "numpad9": "num9",
    # 這四個的目標在 `_EXTRA_KEY_CODES`（底層表沒有）。微軟對這四顆的定義是「任何
    # 國家／地區都是 `+` `,` `-` `.` 那顆鍵」，所以取這種好讀的名字不會說謊；
    # `oem_1` 那一類依鍵盤配置而異，**刻意不給**別名，見 `_EXTRA_KEY_CODES`。
    "plus": "oem_plus", "comma": "oem_comma",
    "minus": "oem_minus", "period": "oem_period",
}

# 底層鍵名表（192 筆）**叫不出名字**的虛擬鍵。值是微軟 "Virtual-Key Codes
# (Winuser.h)" 的官方定義（2026-09-21 對過）；函式庫的 `press_keyboard_key` /
# `release_keyboard_key` / `hotkey` / `post_key_to_window` 都收整數鍵碼，所以
# 本專案內部一律用名字（巨集文字、`_HELD_INPUTS`、回覆），**只在交給函式庫的那
# 一刻**由 `_library_key` 換成整數。
#
# 沒有這張表的後果（2026-09-21 實測）：錄製時按著 ctrl 按 `=`，轉步驟時反查不到
# 名字 → 產出 `# 未知按鍵` → 存檔前被丟掉，錄製回報「N 步」、重播少一步；而且
# `parse_key_name` 查同一張表，所以 `hotkey ctrl+=`（縮放）、`ctrl+/`（註解）這些
# 組合連手寫都寫不出來。
#
# 取名規則是**誠實優先**：
# * `oem_plus` / `oem_comma` / `oem_minus` / `oem_period`：微軟寫「For any
#   country/region」，與鍵盤配置無關；
# * `oem_1`～`oem_8`、`oem_102`：微軟寫「It can vary by keyboard」——**不要**取成
#   `semicolon` / `slash` / `backtick` / `lbracket`，那些名字在非美式配置上會說謊
#   （同一顆鍵印出來的是別的字）；
# * `oem_clear`：底層表也沒有；
# * `launch_app2`：底層表**有**，但只有大寫的 `LAUNCH_APP2`，而 `parse_key_name`
#   會先轉小寫，所以那顆鍵從來寫不出來、錄到也會被丟掉；
# * `browser_home`：底層表缺了這一顆（它前後的 `browser_favorites`、
#   `volume_mute` 都在），多媒體鍵盤上那顆「首頁」錄到會被丟掉。
#
# 名字必須全小寫、只含英數與底線（`parse_key_name` 的字面規則），而且不得跟底層表
# 同名卻指向別的鍵——兩條都有測試。
_EXTRA_KEY_CODES: dict[str, int] = {
    "oem_1": 0xBA,
    "oem_plus": 0xBB,
    "oem_comma": 0xBC,
    "oem_minus": 0xBD,
    "oem_period": 0xBE,
    "oem_2": 0xBF,
    "oem_3": 0xC0,
    "oem_4": 0xDB,
    "oem_5": 0xDC,
    "oem_6": 0xDD,
    "oem_7": 0xDE,
    "oem_8": 0xDF,
    "oem_102": 0xE2,
    "oem_clear": 0xFE,
    "launch_app2": 0xB7,
    "browser_home": 0xAC,
}

# 底層表**有**、但指向錯的鍵的名字 → 正確的虛擬鍵碼。跟 `_EXTRA_KEY_CODES` 是兩回事：
# 那張補的是底層表叫不出來的鍵，這張蓋掉的是底層表叫錯的鍵。
#
# `down`：底層把滑鼠事件常數併進了鍵盤表，`"down"` 是 `0x80`（滑鼠側鍵按下的事件
# 旗標）——而 `0x80` 同時是 `VK_F17`。方向鍵「下」在那張表裡只叫 `vk_down`
# （`0x28`），其餘三個方向 `up`／`left`／`right` 都是對的。所以手寫的
# `hotkey down`、`keydown down`、`/input key press down` 一直**安靜地**按成 F17。
# F17 本身照樣寫得出來（`f17`）。
#
# 真正的修法在上游那張表（`<AutoControlGUI 的本機 checkout>` 的
# `wrapper/_platform_windows.py`，那是另一個專案，**不要**去改它）。上游 2026-09-22 在
# 原始碼樹修好了，但**套件庫的發佈版還沒有**（0.0.222 仍是 0x80），而 fresh clone 裝的是
# 發佈版——所以這一筆要留到發佈之後，那時連同測試的 `_FIXED_UPSTREAM_AWAITING_RELEASE`
# 那一筆一起刪掉。在修好的表上它與底層一致（都是 0x28），留著不會改變任何行為。
#
# 2026-09-21 掃過底層表裡全部 19 個不是虛擬鍵的名字（滑鼠事件、`KEYEVENTF_*` 旗標、
# `MapVirtualKey` 的型別常數），只有 `down` 是一般人會當成按鍵打出來的名字；其餘
# （`move`、`leftup`、`wheel`、`xbutton1`…）不是任何鍵盤上的鍵，刻意不動。
_LIBRARY_NAME_OVERRIDES: dict[str, int] = {
    "down": 0x28,
}


def _library_key(key: str | int) -> str | int:
    """本專案的鍵名 → 交給函式庫的鍵碼。**所有**送鍵給函式庫的地方都要經過這裡。

    覆寫表與補充表裡的名字換成整數（前者底層表叫錯、後者底層表查不到）；其餘原樣
    交出去，讓函式庫自己查表——那條路本來就會動，也保留它原本的錯誤行為。整數原樣
    通過（`write()` 補救路徑拿到的鍵碼本來就是函式庫表裡的值）。

    覆寫表**一定要先查**：被覆寫的名字底層表裡也有（只是指錯），原樣交出去就會被
    底層查成錯的那個鍵。
    """
    if isinstance(key, str):
        if key in _LIBRARY_NAME_OVERRIDES:
            return _LIBRARY_NAME_OVERRIDES[key]
        return _EXTRA_KEY_CODES.get(key, key)
    return key


def _library_keys(keys: list[str]) -> list[str | int]:
    """`_library_key` 的清單版（組合鍵用）。"""
    return [_library_key(key) for key in keys]


def parse_hotkey_tokens(raw: str) -> list[str]:
    """`"ctrl+shift+t"` → `["control", "shift", "t"]`（已套用別名並驗證）。

    每個 token 都會查鍵名表——不驗的話，打錯的鍵名要等到真的送出去才失敗，
    而且錯誤訊息會變成底層那句無法預期的內部字串。
    """
    tokens = [t.strip() for t in (raw or "").strip().lower().split("+") if t.strip()]
    if not tokens:
        raise GuiError("請給按鍵組合，例如 `ctrl+s`。")
    return [parse_key_name(token) for token in tokens]


def parse_duration(raw: str, *, maximum: float) -> float:
    """秒數字串 → float，並套用上限（避免一個 `wait 99999` 綁住工作執行緒）。

    **`nan` 必須在這裡擋掉，不能只靠下面那兩道範圍檢查。** `float("nan")` 是一次
    合法的轉換，而 nan 的所有比較都回 False，所以 `value < 0` 與 `value > maximum`
    **兩道都放行**——同一個形狀已經記在 `_batch_config._is_finite_number`。放行的
    後果不是「等很久」，是**永遠不會結束**：本模組每個等待迴圈都寫成
    `deadline = time.monotonic() + timeout` 配 `if time.monotonic() >= deadline`，
    而 `x >= nan` 恆為 False，逾時那一行永遠不成立。2026-09-08 實測
    `wait_window(…, nan)` 輪詢四十次仍未逾時。

    走 bot 那一側更糟：`/win wait`、`/locate text wait`、`/locate ui wait` 的秒數
    同樣經過這裡，而它們是 `asyncio.to_thread` 且**不帶中止回呼**——卡住的工作
    執行緒沒有任何人收得回來，`/macro stop` 也搆不到。

    `inf` 本來就被上限那道擋掉（`inf > maximum` 是 True），漏的只有 nan。
    """
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as error:
        raise GuiError("秒數必須是數字。") from error
    if not math.isfinite(value):
        raise GuiError("秒數必須是有限的數字。")
    if value < 0:
        raise GuiError("秒數不能是負的。")
    if value > maximum:
        raise GuiError(f"秒數上限是 {maximum:g} 秒。")
    return value


def parse_region(parts: list[str]) -> list[int]:
    """`["x", "y", "w", "h"]` → `[left, top, right, bottom]`（PIL bbox 格式）。

    使用者輸入用「左上角 ＋ 寬高」，因為那是看著螢幕最直覺的寫法；PIL 要的是
    兩個角，所以這裡換算。寬高必須為正，否則 `ImageGrab` 會回一張 0 像素的圖
    然後在存檔時才爆掉。
    """
    if len(parts) != 4:
        raise GuiError("區域需要四個值：`<x> <y> <寬> <高>`。")
    left = parse_coord(parts[0], "x")
    top = parse_coord(parts[1], "y")
    width = parse_size(parts[2], "寬")
    height = parse_size(parts[3], "高")
    return [left, top, left + width, top + height]


# --------------------------------------------------------------------------
# 中止感知的等待
# --------------------------------------------------------------------------
# 每一個 `wait_*` 都是「輪詢 → 睡一下 → 再輪詢」。沒有這一段的話，`!macro stop`
# 說是「在步驟之間生效」，實際上要等目前這個等待步驟跑到逾時才停得下來——
# `MACRO_MAX_WAIT_SEC` 是 120 秒，等於中止指令有兩分鐘完全沒有反應。
#
# 睡眠切成小段而不是一次睡完：中止的反應時間由 `_ABORT_POLL_SEC` 決定，跟等待
# 步驟自己的輪詢間隔無關（`wait_text` 一輪要三秒，不該連帶讓中止慢三秒）。
_ABORT_POLL_SEC = 0.2


def _check_abort(should_abort: Callable[[], bool] | None) -> None:
    """被要求中止就丟 `GuiAborted`。`None` 代表沒有人管，直接放行。"""
    if should_abort is not None and should_abort():
        raise GuiAborted("已依要求中止。")


def _sleep_abortable(seconds: float,
                     should_abort: Callable[[], bool] | None) -> None:
    """睡 `seconds` 秒，中途每 `_ABORT_POLL_SEC` 檢查一次中止。"""
    if should_abort is None:
        time.sleep(max(0.0, seconds))
        return
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        _check_abort(should_abort)
        rest = deadline - time.monotonic()
        if rest <= 0:
            return
        time.sleep(min(_ABORT_POLL_SEC, rest))


# --------------------------------------------------------------------------
# 螢幕
# --------------------------------------------------------------------------
def virtual_bounds() -> tuple[int, int, int, int] | None:
    """虛擬桌面的 `(x, y, 寬, 高)`，**邏輯（DPI 虛擬化後）座標**。

    這一組數字就是滑鼠 API 用的座標空間。取不到（非 Windows）時回 None，
    呼叫端退回只看主螢幕。
    """
    try:
        from je_auto_control.utils.monitor_layout import (  # type: ignore
            logical_virtual_rect,
        )
        return logical_virtual_rect()
    except Exception:  # pylint: disable=broad-except
        return None


def screen_info() -> dict[str, Any]:
    """主螢幕解析度 ＋ 虛擬桌面範圍 ＋ 螢幕數量。

    多螢幕時 `!click` 的座標是虛擬桌面座標，所以使用者需要看得到虛擬桌面的
    範圍才知道副螢幕的 x 從哪裡開始。取不到虛擬桌面資訊時只回主螢幕。
    """
    ac = load_ac()
    try:
        primary = list(ac.screen_size())
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("讀取螢幕資訊失敗。") from error
    info: dict[str, Any] = {
        "primary": (int(primary[0]), int(primary[1])),
        "virtual": virtual_bounds(),
        "monitors": 1,
    }
    try:
        from je_auto_control.utils.monitor_layout import (  # type: ignore
            enumerate_monitors,
        )
        info["monitors"] = max(1, len(enumerate_monitors()))
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass  # 列舉不到（非 Windows 等）：只回主螢幕就好
    return info


def _grab_virtual_logical():
    """整個虛擬桌面的截圖，**換算成滑鼠座標的那個空間**。

    換算本身在函式庫的 `grab_logical` 裡（OCR 與樣板比對共用同一個原語）。它處理
    的坑是：`ImageGrab.grab(all_screens=True)` 會先把自己設成 DPI-aware 再抓，
    回來的是**實體像素**，而本行程 DPI-unaware，滑鼠 API 用的是虛擬化後的邏輯
    像素——本機實測邏輯 3456×1244 vs 截圖 3840×1244（副螢幕 125% 縮放），照截圖
    數出來的 x=2500 點下去會差 116 px。截圖存在的意義就是讓人找出要點哪裡，對不
    上等於這個功能是壞的。
    """
    return _logical_frame(None)[0]


GIF_MAX_SECONDS = 15.0
GIF_MAX_FPS = 5.0
GIF_MAX_EDGE = 960


def capture_gif(dest: Path, *, seconds: float = 5.0, fps: float = 3.0,
                region: list[int] | None = None) -> int:
    """連拍一段時間存成動畫 GIF，回影格數。

    單張截圖看不出**過程**：進度條有沒有在動、動畫卡在哪一格、按下去之後畫面
    閃了什麼。這裡補上那一段。

    三個上限是刻意的：時間、影格率、以及把長邊縮到 `GIF_MAX_EDGE`。整個桌面
    3456×1244 拍 45 張不縮圖是好幾十 MB，送不出去也沒人想看——會動比清晰重要，
    真要看細節本來就該用 `!screen` 截那一塊。
    """
    try:
        from PIL import Image  # type: ignore  # noqa: F401
    except ImportError as error:
        raise GuiError("截圖功能未安裝。") from error
    span = max(0.5, min(float(seconds), GIF_MAX_SECONDS))
    rate = max(1.0, min(float(fps), GIF_MAX_FPS))
    interval = 1.0 / rate
    frames = []
    deadline = time.monotonic() + span
    try:
        while time.monotonic() < deadline:
            started = time.monotonic()
            image, _ox, _oy = _logical_frame(region)
            if max(image.width, image.height) > GIF_MAX_EDGE:
                scale = GIF_MAX_EDGE / max(image.width, image.height)
                image = image.resize(
                    (max(1, int(image.width * scale)),
                     max(1, int(image.height * scale))))
            frames.append(image.convert("P", palette=1))
            rest = interval - (time.monotonic() - started)
            if rest > 0:
                time.sleep(rest)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("連拍失敗。") from error
    if not frames:
        raise GuiError("沒有拍到任何影格。")
    try:
        frames[0].save(str(dest), save_all=True, append_images=frames[1:],
                       duration=int(interval * 1000), loop=0, optimize=True)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("連拍存檔失敗。") from error
    return len(frames)


def capture(dest: Path, *, region: list[int] | None = None,
            all_screens: bool = False) -> None:
    """截圖存成 PNG。`region` 是 `[left, top, right, bottom]`（邏輯座標）。

    擷取走函式庫的 `grab_logical`：它涵蓋所有螢幕、把畫面換算回點選座標空間，
    區域裁切也在**換算完的圖上**做（`ImageGrab` 的 `bbox` 是在實體像素空間裁的，
    縮放過的螢幕上會裁錯位置）。不走 `je_auto_control.screenshot()`：那個固定
    `ImageGrab.grab()`（只有主螢幕）又多繞一趟 cv2 色彩空間轉換。
    """
    try:
        image, _origin_x, _origin_y = load_ac().grab_logical(
            _region_xywh(region), all_screens=(all_screens or region is not None))
        image.save(str(dest))
    except GuiError:
        raise                      # 後端載不進來的訊息比「截圖失敗」有用
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] capture failed: {error!r}", file=sys.stderr)
        raise GuiError("截圖失敗。") from error


def pixel_color(x: int, y: int) -> tuple[int, int, int]:
    """取單點顏色，回 `(r, g, b)`。

    底層在不同平台回傳的型別不一致（Windows 是 COLORREF int、其他平台是
    tuple），所以這裡統一正規化再回傳。
    """
    ac = load_ac()
    try:
        raw = ac.get_pixel(x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("讀取像素顏色失敗。") from error
    if isinstance(raw, (tuple, list)) and len(raw) >= 3:
        return int(raw[0]), int(raw[1]), int(raw[2])
    if isinstance(raw, int):
        # Win32 COLORREF 是 0x00BBGGRR
        return raw & 0xFF, (raw >> 8) & 0xFF, (raw >> 16) & 0xFF
    raise GuiError("讀取像素顏色失敗。")


# --------------------------------------------------------------------------
# 滑鼠 / 鍵盤
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 送出去的輸入到得了嗎
# --------------------------------------------------------------------------
# 送出去的滑鼠鍵盤事件可能**安靜地消失**：API 回成功、實際上什麼都沒發生。這是
# 這個專案最在意的那種失敗——不是崩潰，是靜默的錯誤結果，而下指令的人不在電腦
# 前面，只會看到「已點選 (500, 300)」然後納悶為什麼沒反應。
#
# 兩種成因要用兩種偵測，函式庫兩個都有：
#
# * **工作站鎖定／UAC 安全桌面**：`input_desktop_available()` 問輸入桌面，免費
#   且沒有副作用，所以輸入原語每次送出前都問（函式庫那邊快取兩秒）。
# * **有東西在過濾注入的輸入**（前景是有防作弊的遊戲時會這樣）：
#   `input_reaches_system()` 得**真的送一個鍵**去試，所以只放在診斷指令上，不放
#   進每個輸入原語——每次點選前都送一個鍵比問題本身還糟。本機實測過：那種情況下
#   `OpenInputDesktop` 回正常、完整性等級也跟我們一樣（都是 Medium），只有實際
#   送鍵才看得出來。
def input_desktop_available() -> bool:
    """現在送得進滑鼠鍵盤事件嗎（工作站沒鎖、不在安全桌面上）。"""
    try:
        return bool(load_ac().input_desktop_available())
    except Exception as error:  # pylint: disable=broad-except
        # 查不出來就當成可用：這只是為了給出更好的錯誤訊息，不該反過來擋住操作。
        print(f"[gui] input desktop probe failed: {error!r}", file=sys.stderr)
        return True


def input_reaches_system() -> bool:
    """送出去的鍵盤事件真的進得了系統嗎。**這會送出一個按鍵**（F13）。

    只給診斷用（`!doctor` / `!screen info`）。回 True 也可能只是「測不出來」。
    """
    try:
        return bool(load_ac().input_reaches_system())
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] input reach probe failed: {error!r}", file=sys.stderr)
        return True


def _require_input_desktop() -> None:
    """送輸入之前確認桌面收得到。**放開類的操作不呼叫這個**——那是復原路徑。"""
    if not input_desktop_available():
        raise GuiError("電腦目前是鎖定狀態，送不進滑鼠鍵盤操作；請先解鎖。")


def mouse_position() -> tuple[int, int]:
    ac = load_ac()
    try:
        pos = ac.get_mouse_position()
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("讀取滑鼠座標失敗。") from error
    if not pos:
        raise GuiError("讀取滑鼠座標失敗。")
    return int(pos[0]), int(pos[1])


def mouse_move(x: int, y: int) -> None:
    _require_input_desktop()
    ac = load_ac()
    try:
        ac.set_mouse_position(x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("移動滑鼠失敗。") from error


def mouse_click(button: str, x: int | None = None, y: int | None = None,
                *, times: int = 1, interval: float = 0.06) -> None:
    """點選；`times=2` 就是雙擊。

    雙擊刻意用「同一個座標連點兩次 ＋ 短間隔」而不是找底層的 double-click
    API —— je_auto_control 沒有雙擊原語，而 Windows 判定雙擊只看兩次點選的
    時間差與位移，連點就夠。間隔取 60ms，遠低於預設的 500ms 判定閾值。
    """
    _require_input_desktop()
    ac = load_ac()
    try:
        for index in range(max(1, times)):
            if index:
                time.sleep(interval)
            if x is None or y is None:
                ac.click_mouse(button)
            else:
                ac.click_mouse(button, x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("滑鼠點選失敗。") from error


def mouse_drag(x1: int, y1: int, x2: int, y2: int, button: str = "mouse_left",
               *, steps: int = 24, settle: float = 0.08) -> None:
    """按住起點拖到終點再放開。

    中間刻意分成多段慢慢移動：很多應用程式（檔案總管、繪圖軟體、遊戲）用
    滑鼠移動事件判定拖曳，一次瞬移到終點會被當成「按下又放開」而不是拖曳。
    """
    _require_input_desktop()
    ac = load_ac()
    try:
        ac.set_mouse_position(x1, y1)
        time.sleep(settle)
        ac.press_mouse(button, x1, y1)
        time.sleep(settle)
        for index in range(1, steps + 1):
            ac.set_mouse_position(
                int(x1 + (x2 - x1) * index / steps),
                int(y1 + (y2 - y1) * index / steps),
            )
            time.sleep(0.012)
        time.sleep(settle)
        ac.release_mouse(button, x2, y2)
    except Exception as error:  # pylint: disable=broad-except
        # 拖曳中途失敗會把滑鼠鍵卡在按下狀態，整個桌面等於被鎖住。這裡盡力
        # 補放開，失敗也不再往上冒（原始錯誤比較重要）。
        try:
            ac.release_mouse(button, x2, y2)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        raise GuiError("滑鼠拖曳失敗。") from error


def mouse_scroll(amount: int, x: int | None = None, y: int | None = None) -> None:
    """滾輪。正值往上、負值往下（跟大多數 API 的慣例一致）。

    注意底層會把 x/y 夾在**主螢幕**範圍內，所以副螢幕上的定點捲動請先
    `mouse move` 過去再不帶座標捲動。
    """
    _require_input_desktop()
    ac = load_ac()
    try:
        if x is None or y is None:
            ac.mouse_scroll(int(amount))
        else:
            ac.mouse_scroll(int(amount), x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("滾輪捲動失敗。") from error


# --------------------------------------------------------------------------
# 打字
# --------------------------------------------------------------------------
def type_text(text: str) -> None:
    """把文字當成鍵盤輸入送到目前焦點視窗。

    直接交給底層的 `write()`。它會逐字元決定路徑：鍵名表裡有的送虛擬鍵、沒有的
    （標點、中日文、表情符號）送 Unicode 字元事件、換行與 Tab 送真正的 Enter /
    Tab 鍵。**這一段刻意不在本專案重做**——同一件事只該有一份實作，而它屬於桌面
    自動化函式庫，不屬於這個 bot。
    """
    _require_input_desktop()
    if not text:
        return
    ac = load_ac()
    try:
        ac.write(text)
    except Exception as error:  # pylint: disable=broad-except
        # 失敗時補一輪放開，理由與 `press_hotkey` 完全相同：底層的
        # `type_keyboard` 是「按下 → 放開」而中間**沒有 finally**，所以按下之後
        # 若放開那一步失敗，那個鍵就留在按下的狀態。這條路徑同樣繞過本模組宣稱
        # 的三層保險（`_HELD_INPUTS` 登記／`release_all_inputs`／逾時自動放開只
        # 掛在 `key_down`／`key_up` 上），所以 `/input key status` 會說什麼都沒按
        # 住、`/input key clear` 也放不掉。
        #
        # 這一側比組合鍵更難自己恢復：卡住的是**一般字元鍵**，Windows 會持續自動
        # 重複，畫面上就是那個字被無限打出來，而下指令的人不在電腦前面。
        # （組合鍵那條卡的是修飾鍵；這條 `write(is_shift=False)` 不碰 shift。）
        stuck = _undo_write_press(ac, text)
        raise GuiError(
            "鍵盤輸入失敗（這段文字含有無法直接鍵入的字元）；"
            f"已重新放開這段文字會用到的 {len(stuck)} 個按鍵，鍵盤不會卡住。"
        ) from error


# `write()` 把換行／Tab／退格轉成真正的按鍵，不是打出那個控制字元。這份對照要跟
# 底層的 `WRITE_CONTROL_KEYS` 一致；讀得到就用它的，讀不到才用這份備份（那是內部
# 常數，不保證一直在，但即使漂掉了，最壞情況也只是少放開一個鍵）。
_WRITE_CONTROL_KEYS_FALLBACK = {"\n": "return", "\r": "return",
                                "\t": "tab", "\x08": "back"}

# 一段文字最多回收幾個相異按鍵。正常 ASCII 文字的相異字元遠小於這個數；設上限
# 只是不讓一段病態的長字串把「補救」本身變成幾百次輸入事件。
_UNDO_WRITE_MAX_KEYS = 64


def _write_control_keys(ac: Any) -> dict:
    """底層的「控制字元 → 按鍵名」對照，讀不到就用備份。永不 raise。"""
    for holder in (ac, getattr(ac, "wrapper", None)):
        table = getattr(holder, "WRITE_CONTROL_KEYS", None)
        if isinstance(table, dict) and table:
            return table
    try:
        from je_auto_control.wrapper.auto_control_keyboard import (  # type: ignore
            WRITE_CONTROL_KEYS,
        )
        if isinstance(WRITE_CONTROL_KEYS, dict) and WRITE_CONTROL_KEYS:
            return WRITE_CONTROL_KEYS
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    return _WRITE_CONTROL_KEYS_FALLBACK


def keys_a_write_could_press(ac: Any, text: str) -> list:
    """這段文字交給 `write()` 時，**可能被按下**的那些鍵（去重、有上限）。

    純查表、不送任何輸入事件，所以測得起來。順序沿用文字裡第一次出現的順序，
    讓失敗訊息與 log 讀起來可預期。

    對照的是底層 `write()` 的分支：控制字元 → `WRITE_CONTROL_KEYS`；表裡有的
    字元 → 鍵碼；表裡沒有的 → 走 Unicode 事件（**不按任何鍵**，所以不列入）；
    再不行的空白 → `space`。
    """
    control = _write_control_keys(ac)
    table = getattr(ac, "keyboard_keys_table", None)
    if not isinstance(table, dict):
        table = {}
    out: list = []
    seen = set()

    def _add(key):
        if key is None or key in seen:
            return
        seen.add(key)
        out.append(key)

    for char in text or "":
        if len(out) >= _UNDO_WRITE_MAX_KEYS:
            break
        mapped = control.get(char)
        if mapped is not None and mapped in table:
            _add(mapped)
        elif char in table:
            _add(table[char])
        elif char.isspace():
            # Unicode 那條不按鍵，所以只有「表裡沒有、但是空白」才會走 space。
            _add("space")
    return out


def _undo_write_press(ac: Any, text: str) -> list:
    """`write()` 送到一半失敗時，把這段文字可能還按著的鍵放開一輪。回實際放掉的。

    與 `_undo_hotkey_press` 同一個立場：**只放這一次呼叫自己可能按下的鍵**，不做
    「順手把所有修飾鍵都放掉」的大掃除——使用者可能正握著 ctrl 站在鍵盤前面。
    放開一個沒按住的鍵是安全的（作業系統對已彈起的鍵不做事，`key_up` 的 docstring
    是同一個立場），所以不需要知道失敗前打到第幾個字。
    """
    released: list = []
    try:
        candidates = keys_a_write_could_press(ac, text)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] write undo: cannot resolve keys: {error!r}",
              file=sys.stderr)
        return released
    for key in candidates:
        try:
            ac.release_keyboard_key(_library_key(key))
            released.append(key)
        except Exception as error:  # pylint: disable=broad-except
            print(f"[gui] write undo release {key!r} failed: {error!r}",
                  file=sys.stderr)
    return released


def _undo_hotkey_press(ac: Any, tokens: list[str]) -> list[str]:
    """組合鍵送到一半失敗時，把可能還按著的鍵反向放開一輪。回真的放掉了哪些。

    只放**我們自己要求按下的那幾個鍵**，不做「順手把所有修飾鍵都放掉」的大掃除
    ——使用者可能正握著 ctrl 在鍵盤前面，替他放開是另一種靜默的錯。

    放開一個沒按住的鍵是安全的：底層送的是放開事件，作業系統對已經彈起的鍵不做
    事（`key_up` 的 docstring 也是同一個立場）。所以這裡不需要知道失敗前到底按到
    第幾個，全部倒著放一次就好。
    """
    released: list[str] = []
    for token in reversed(tokens):
        try:
            ac.release_keyboard_key(_library_key(token))
            released.append(token)
        except Exception as error:  # pylint: disable=broad-except
            print(f"[gui] hotkey undo release {token!r} failed: {error!r}",
                  file=sys.stderr)
    return released


def press_hotkey(tokens: list[str]) -> None:
    """送出組合鍵（依序按下、再反向放開）。

    **失敗時一定要補一輪反向放開。** 底層 `je_auto_control.hotkey` 是
    「for 按下 → for 放開」而中間**沒有 finally**，2026-08-30 在這台機器上實測
    （把底層 press/release 換成假的記錄器，完全不碰真實桌面）：三鍵組合在第二個
    鍵按下時丟例外，呼叫序列只有 `press ctrl` / `press shift`，**一次 release 都
    沒有**——ctrl 與 shift 就這樣留在按下的狀態。

    這條路徑會繞過本模組宣稱的三層保險，因為那三層（`_HELD_INPUTS` 登記、
    `release_all_inputs`、逾時自動放開）只掛在 `key_down` / `key_up` 上：組合鍵
    從頭到尾不登記，所以 `/input key status` 會說什麼都沒按住、`/input key clear`
    放不掉、也沒有任何逾時計時器。後果是整台電腦像壞掉一樣（alt 卡住之後每個按鍵
    都變成選單快捷鍵），而下指令的人不在電腦前面。

    而且這不是只有打錯鍵名才會發生：鍵名先經過 `parse_key_name`，真正的觸發是
    作業系統層在兩次按下之間失敗（UAC 畫面插進來、輸入被安全桌面擋掉）。
    """
    _require_input_desktop()
    ac = load_ac()
    try:
        ac.hotkey(_library_keys(tokens))
    except Exception as error:  # pylint: disable=broad-except
        _undo_hotkey_press(ac, tokens)
        raise GuiError("送出按鍵組合失敗（可能是不存在的鍵名）；"
                       "已把這組鍵重新放開，鍵盤不會卡住。") from error


# --------------------------------------------------------------------------
# 按住 / 放開（修飾鍵＋點選、遊戲、長按）
# --------------------------------------------------------------------------
# `press_hotkey` 是「按下馬上放開」，做不到「按住 W 走三秒」「按住 ctrl 連點五個
# 檔案」「按住左鍵畫一條線再放開」。這裡把 down / up 拆開。
#
# 代價是**狀態會留在主機上**：一個沒放開的鍵會讓整台電腦像壞掉一樣（alt 卡住之
# 後每個按鍵都變成選單快捷鍵），而且下指令的人不在電腦前面、看不到卡住了。所以
# 三層保險，缺一不可：
#
# 1. 每次按住都登記進 `_HELD_INPUTS`，`held_inputs()` 隨時查得到；
# 2. `release_all_inputs()` 一次全放（`!key clear`、巨集結束都會呼叫）；
# 3. 呼叫端另外掛逾時自動放開（`release_input_if_stale`）——按下去就忘了是常態。
#
# 三層都靠同一個不變式撐著：**一筆登記只有在「放開真的送出去了」之後才會離開
# `_HELD_INPUTS`**（`_release_keys`）。放開失敗的那一筆照樣留著、按下時間不變，所
# 以 `/input key status` 看得到它、下一次 `/input key clear`／`/host panic`／巨集收尾／
# 逾時重試都會再放一次。
# 在 2026-09-21 之前是「不管成功與否都拿掉」，放開失敗的鍵於是從三層保險裡一起消
# 失——正是這段註解要防的那個「卡住了卻看不到」。
_HELD_INPUTS: dict[tuple[str, str], float] = {}

# 沒有人主動放開時，多久之後自動放開。由呼叫端負責排程（本模組沒有 event loop）。
INPUT_HOLD_MAX_SEC = 300.0

# 逾時自動放開**失敗**時再試幾次、間隔多久（呼叫端排程，理由見 bot 的
# `_auto_release_input`）。放開會失敗的主要成因是作業系統當下不收合成輸入（函式庫
# 在 `SendInput` 回 0 時丟例外：輸入被別的執行緒擋住、安全桌面），那是會自己過去的
# 狀態，所以值得重試；但不設上限的話，一個永遠放不掉的鍵會變成一個永遠活著、每隔
# 一段時間就寫一行 log 的背景工作。試完還放不掉就留在 `/input key status` 裡給人處理。
INPUT_RELEASE_RETRIES = 3
INPUT_RELEASE_RETRY_SEC = 60.0


def parse_key_name(raw: str) -> str:
    """驗證鍵名。不合法就丟 `GuiError`，不讓任意字串進到底層鍵名表。

    後端載不進來時只做字面檢查、不擋——這個函式也被巨集的**存檔驗證**呼叫，
    在沒有桌面 session 的環境（測試、CI）存一個巨集不該因為缺後端而失敗。

    `_EXTRA_KEY_CODES` 與 `_LIBRARY_NAME_OVERRIDES` 的名字不必問底層表（前者那張表
    本來就沒有、後者那張表裡的值是錯的），回傳的仍是**名字**；換成整數是
    `_library_key` 在送出那一刻才做的事。
    """
    key = (raw or "").strip().lower()
    if not key or not HOTKEY_TOKEN_RE.match(key):
        raise GuiError("鍵名只能是英數字與底線。")
    key = KEY_ALIASES.get(key, key)
    if key in _EXTRA_KEY_CODES or key in _LIBRARY_NAME_OVERRIDES:
        return key
    try:
        table = getattr(load_ac(), "keyboard_keys_table", None)
    except GuiError:
        return key
    if isinstance(table, dict) and key not in table:
        raise GuiError("不認得這個鍵名。")
    return key


def key_down(name: str) -> str:
    """按住某個鍵不放。"""
    key = parse_key_name(name)
    _require_input_desktop()
    ac = load_ac()
    try:
        ac.press_keyboard_key(_library_key(key))
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("按下按鍵失敗。") from error
    _HELD_INPUTS[("key", key)] = time.monotonic()
    return key


def key_up(name: str) -> str:
    """放開某個鍵。**沒按住也照樣送放開事件**——重點是讓卡住的鍵能被解掉，
    而不是堅持狀態一致（bot 重啟後 `_HELD_INPUTS` 是空的，但鍵還按著）。"""
    key = parse_key_name(name)
    ac = load_ac()
    try:
        ac.release_keyboard_key(_library_key(key))
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("放開按鍵失敗。") from error
    _HELD_INPUTS.pop(("key", key), None)
    return key


def mouse_button_down(button: str, x: int | None = None,
                      y: int | None = None) -> None:
    _require_input_desktop()
    ac = load_ac()
    try:
        if x is None or y is None:
            ac.press_mouse(button)
        else:
            ac.press_mouse(button, x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("按下滑鼠鍵失敗。") from error
    _HELD_INPUTS[("mouse", button)] = time.monotonic()


def mouse_button_up(button: str, x: int | None = None,
                    y: int | None = None) -> None:
    ac = load_ac()
    try:
        if x is None or y is None:
            ac.release_mouse(button)
        else:
            ac.release_mouse(button, x, y)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("放開滑鼠鍵失敗。") from error
    _HELD_INPUTS.pop(("mouse", button), None)


def held_inputs() -> list[tuple[str, str, float]]:
    """目前按住不放的鍵 / 滑鼠鍵：`[(kind, 名稱, 已按住秒數), …]`。"""
    now = time.monotonic()
    return sorted(
        ((kind, name, now - since) for (kind, name), since in _HELD_INPUTS.items()),
        key=lambda row: -row[2],
    )


def input_pressed_at(kind: str, name: str) -> float | None:
    """某個鍵是什麼時候按下去的；沒按住回 None。給呼叫端排逾時自動放開用。"""
    return _HELD_INPUTS.get((kind, name))


def held_snapshot() -> set[tuple[str, str]]:
    """目前按住的集合。給「只放開自己按下去那些」用（見 `release_added_since`）。"""
    return set(_HELD_INPUTS)


def release_all_inputs() -> list[str]:
    """全部放開，回被放開的名稱。單一失敗不中斷——重點是盡量把桌面解鎖。

    放不掉的那幾個**不在回傳值裡、也不會從登記裡消失**；要知道有幾個放不掉，用
    `release_all_inputs_report`。
    """
    return _release_keys(list(_HELD_INPUTS))[0]


def release_all_inputs_report() -> tuple[list[str], list[str]]:
    """同 `release_all_inputs`，另外回放不掉的那些：`(已放開, 放不掉)`。

    給要**對人回報**的呼叫端（`/input key clear`、`/host panic`）：只回「已放開 N 個」
    的話，放不掉的那一個就安靜地少算一個，而下指令的人正好看不到鍵盤。
    """
    return _release_keys(list(_HELD_INPUTS))


def release_added_since(snapshot: set[tuple[str, str]]) -> list[str]:
    """只放開 snapshot 之後才按下去的。

    巨集結束時用這個而不是 `release_all_inputs`：使用者可能在跑巨集之前就自己
    `!key down ctrl` 按著，巨集不該替他放開。放不掉的那幾個同樣留在登記裡。
    """
    return _release_keys([k for k in _HELD_INPUTS if k not in snapshot])[0]


def release_input_if_stale(kind: str, name: str, pressed_at: float) -> bool:
    """按住超過上限時自動放開；回傳有沒有真的放。

    比對 `pressed_at` 是刻意的：使用者可能放開後又按同一個鍵，這時舊的逾時計時
    器不該把新的那次放掉。時間戳不同就代表不是同一次按住。

    放開**失敗**時回 False，而那一筆的按下時間原封不動——所以同一個計時器拿同一個
    `pressed_at` 再叫一次就是重試（呼叫端據此分辨「不是同一次按住」與「放開失敗」：
    前者之後 `input_pressed_at` 已經不等於 `pressed_at`，後者還相等）。
    """
    if _HELD_INPUTS.get((kind, name)) != pressed_at:
        return False
    return bool(_release_keys([(kind, name)])[0])


def _release_keys(keys: list[tuple[str, str]]
                  ) -> tuple[list[str], list[str]]:
    """逐一放開，回 `(已放開的名稱, 放不掉的名稱)`。

    **只有放開成功的才從 `_HELD_INPUTS` 拿掉。** 放開丟例外時那個鍵很可能還按著，
    這時把登記拿掉等於讓它從三層保險裡一起消失：`/input key status` 說沒有按住任何
    鍵、`/input key clear` 的計數少一個、逾時計時器再也找不到它。留著的那一筆按下時
    間不變，逾時計時器的比對因此照樣成立。

    載不進後端時**什麼都不拿掉**、全部算放不掉。那一支在正式執行時其實走不到：登記
    只在按下**成功之後**才寫入，按得下去代表 `load_ac()` 早就成功過，而它把模組快取
    在 `_AC`、之後不會再變回 None。刻意不寫成 `clear()`（原本是）：走得到的唯一情形是
    有人重設了快取，那時候這些鍵照樣可能按著，清掉就是同一個「卡住了卻看不到」。
    """
    try:
        ac = load_ac()
    except GuiError:
        return [], [name for _kind, name in keys]
    released: list[str] = []
    failed: list[str] = []
    for kind, name in keys:
        try:
            if kind == "key":
                ac.release_keyboard_key(_library_key(name))
            else:
                ac.release_mouse(name)
        except Exception as error:  # pylint: disable=broad-except
            print(f"[gui] release {kind} {name!r} failed: {error!r}", file=sys.stderr)
            failed.append(name)
            continue
        released.append(name)
        _HELD_INPUTS.pop((kind, name), None)
    return released, failed


def paste_text(text: str) -> None:
    """把文字放進剪貼簿再送 `ctrl+v`。

    `type_text` 走的是逐字元的鍵盤模擬，打不出中日文與大多數非 ASCII 字元；
    要輸入這些內容唯一可靠的路徑就是剪貼簿 ＋ 貼上。
    """
    set_clipboard(text)
    time.sleep(0.05)
    # 一定要經過別名層：底層鍵名表**沒有** `ctrl`（只有 `control`），而組合鍵會
    # 原樣交給函式庫查表。這裡原本寫死 `["ctrl", "v"]`，所以每一次貼上都在函式庫
    # 查表那一步失敗（2026-09-21 對真表實測：`_resolve_keycode("ctrl")` 丟
    # `AutoControlCantFindKeyException`）；測試用的假後端什麼名字都收，看不出來。
    press_hotkey(parse_hotkey_tokens("ctrl+v"))


# --------------------------------------------------------------------------
# 剪貼簿
# --------------------------------------------------------------------------
def get_clipboard() -> str:
    ac = load_ac()
    try:
        return ac.get_clipboard() or ""
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("讀取剪貼簿失敗。") from error


def set_clipboard(text: str) -> None:
    ac = load_ac()
    try:
        ac.set_clipboard(text)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("寫入剪貼簿失敗。") from error


def get_clipboard_image(dest: Path) -> bool:
    """剪貼簿裡若是圖片就存到 `dest`，回 True；不是圖片回 False。

    「截了圖貼到剪貼簿」是很常見的一步，但 `get_clipboard()` 只看得到文字，
    圖片對它來說等於空的——使用者會以為剪貼簿是空的。

    套件回的是 PNG 位元組，`dest` 一律用 `.png`。
    """
    try:
        payload = load_ac().get_clipboard_image()
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("讀取剪貼簿圖片失敗。") from error
    if not payload:
        return False        # 空的，或剪貼簿裡是「檔案」而不是圖片本身
    try:
        dest.write_bytes(payload)
    except OSError as error:
        raise GuiError("剪貼簿圖片存檔失敗。") from error
    return True


def set_clipboard_image(source: Path) -> None:
    """把一張圖放進剪貼簿，之後就能在任何程式裡直接貼上。

    函式庫的 `set_clipboard_image` 同時吃 PNG 位元組與檔案路徑，這裡給路徑。
    """
    try:
        load_ac().set_clipboard_image(str(source))
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("寫入剪貼簿圖片失敗。") from error


def clipboard_file_list() -> list[str]:
    """在檔案總管按了「複製」之後，剪貼簿裡的檔案路徑清單。

    第三種剪貼簿內容。文字看不到它、圖片也看不到它——複製了一批檔案再打
    `!clip` 會得到「空的」，跟當初圖片那個誤導完全同一類。

    **回的是主機上的完整路徑**，呼叫端只能拿它算數量與副檔名，不可以整串送出去
    （Secrecy Layer 1）。
    """
    try:
        return list(load_ac().get_clipboard_files() or [])
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("讀取剪貼簿檔案清單失敗。") from error


def clipboard_kinds() -> dict[str, Any]:
    """剪貼簿目前**有哪幾種**內容：`{categories, has_text, has_image, has_files}`。

    格式清單（`formats`）刻意不往外傳：那是 Win32 的格式名稱，對使用者沒有意義，
    而且有些程式會把自訂格式名取成含路徑或產品內部代號的字串。
    """
    try:
        summary = load_ac().clipboard_formats() or {}
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("讀取剪貼簿格式失敗。") from error
    return {
        "categories": list(summary.get("categories") or []),
        "has_text": bool(summary.get("has_text")),
        "has_image": bool(summary.get("has_image")),
        "has_files": bool(summary.get("has_files")),
    }


# --------------------------------------------------------------------------
# 視窗
# --------------------------------------------------------------------------
def list_windows() -> list[tuple[int, str]]:
    """所有可見、有標題的最上層視窗，依 z-order（最前面的在最前）。"""
    api = _window_api()
    try:
        return api.list_windows(titled_only=True)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("列舉視窗失敗。") from error


def match_windows(needle: str) -> list[tuple[int, str]]:
    """標題含 `needle` 的視窗（不分大小寫）。"""
    key = (needle or "").strip().lower()
    if not key:
        raise GuiError("請給視窗標題的一段文字。")
    return [(hwnd, title) for hwnd, title in list_windows() if key in title.lower()]


# `!win <動作>` → Win32 `ShowWindow` 的 cmd 值。`close` 不在這裡，它走 WM_CLOSE。
WINDOW_SHOW_ACTIONS = {
    "min": 6,        # SW_MINIMIZE
    "minimize": 6,
    "max": 3,        # SW_MAXIMIZE
    "maximize": 3,
    "restore": 9,    # SW_RESTORE
    "hide": 0,       # SW_HIDE
    "show": 5,       # SW_SHOW
}


def window_show(needle: str, action: str) -> tuple[int, str, int]:
    """對第一個命中的視窗做 min / max / restore / hide / show。

    回 `(hwnd, title, 命中數)`；呼叫端只該把命中數回給使用者，**標題不可外送**
    （IDE / 編輯器慣例會把絕對主機路徑寫在標題列）。
    """
    api = _window_api()
    cmd_show = WINDOW_SHOW_ACTIONS.get((action or "").strip().lower())
    if cmd_show is None:
        raise GuiError("視窗動作只能是 `min` / `max` / `restore` / `show` / `hide` / `close`。")
    matched = match_windows(needle)
    if not matched:
        raise GuiError("找不到符合的視窗。")
    hwnd, title = matched[0]
    try:
        api.show_window_by_title(needle, cmd_show)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("切換視窗狀態失敗。") from error
    return hwnd, title, len(matched)


def window_close(needle: str) -> tuple[int, str, int]:
    """送 `WM_CLOSE` 給第一個命中的視窗（等同按右上角的關閉鈕）。

    刻意不用 `TerminateProcess`：`WM_CLOSE` 讓程式跑自己的收尾（存檔提示、
    設定寫回），要硬殺行程使用者本來就有 `!kill`。
    """
    api = _window_api()
    matched = match_windows(needle)
    if not matched:
        raise GuiError("找不到符合的視窗。")
    hwnd, title = matched[0]
    try:
        api.close_window_by_title(needle)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("關閉視窗失敗。") from error
    return hwnd, title, len(matched)


def window_focus(needle: str) -> tuple[int, str, int]:
    """還原（如果被最小化）再拉到前景。"""
    api = _window_api()
    matched = match_windows(needle)
    if not matched:
        raise GuiError("找不到符合的視窗。")
    hwnd, title = matched[0]
    try:
        # 套件的 focus_window 會在視窗被最小化時先還原再拉到前景。
        api.focus_window(needle)
    except Exception as error:  # pylint: disable=broad-except
        # SetForegroundWindow 在 alt-tab 鎖 / 前景鎖定的情況會 access denied。
        raise GuiError("找到視窗了，但系統不允許把它拉到前景。") from error
    return hwnd, title, len(matched)


def foreground_window() -> tuple[int, str] | None:
    """目前最前面的視窗 `(hwnd, 標題)`；取不到回 None。"""
    try:
        return _window_api().foreground_window()
    except Exception:  # pylint: disable=broad-except
        return None


def window_rect(needle: str) -> tuple[int, str, tuple[int, int, int, int], int]:
    """第一個命中視窗的 `(hwnd, 標題, (x, y, 寬, 高), 命中數)`。

    座標跟滑鼠是**同一個空間**（本行程 DPI-unaware，`GetWindowRect` 回的是虛擬化
    後的邏輯座標），所以量到什麼就能直接拿去 `!click`。

    這個原語是「可靠地點到某個視窗裡面」的前提：沒有它，使用者只能截圖數像素，
    而視窗一移動座標就全錯。
    """
    api = _window_api()
    matched = match_windows(needle)
    if not matched:
        raise GuiError("找不到符合的視窗。")
    hwnd, title = matched[0]
    rect = api.window_rect(needle)
    if rect is None:
        raise GuiError("讀取視窗位置失敗。")
    left, top, right, bottom = rect
    return hwnd, title, (left, top, right - left, bottom - top), len(matched)


def window_move(needle: str, x: int, y: int, width: int | None = None,
                height: int | None = None) -> tuple[int, str, int]:
    """把第一個命中的視窗搬到 `(x, y)`，可選同時改成 `width × height`。

    底層走 `MoveWindow`，它只改位置與大小，不動 z-order 也不搶焦點——只想擺位置
    的時候把視窗拉到最上面又搶走焦點，會打斷使用者正在做的事，也會讓接下來的鍵盤
    操作打錯地方。省略寬高時由套件讀出目前大小沿用，不會把視窗縮成 0。
    """
    api = _window_api()
    matched = match_windows(needle)
    if not matched:
        raise GuiError("找不到符合的視窗。")
    hwnd, title = matched[0]
    try:
        moved = api.move_window_by_title(needle, x, y, width, height)
    except Exception as error:  # pylint: disable=broad-except
        raise GuiError("搬移視窗失敗。") from error
    if not moved:
        raise GuiError("搬移視窗失敗。")
    return hwnd, title, len(matched)


def wait_window(needle: str, timeout: float, poll: float = 0.5, *,
                should_abort: Callable[[], bool] | None = None
                ) -> tuple[int, str]:
    """輪詢等視窗出現。逾時丟 `GuiError`，被中止丟 `GuiAborted`。"""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        matched = match_windows(needle)
        if matched:
            return matched[0]
        if time.monotonic() >= deadline:
            raise GuiError("等到逾時仍沒有出現符合的視窗。")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# 視窗版面（存 / 還原 / 靠邊 / 排格）
# --------------------------------------------------------------------------
# 遠端操作時最花時間的往往不是「點哪裡」，而是把畫面重新排成看得懂的樣子。實作
# 全部在函式庫（`window_capture` 的 `save_window_layout` / `restore_window_layout`
# / `snap_window` / `arrange_grid`），這裡只做名稱驗證、落地位置與錯誤訊息。
LAYOUT_DIR = PROJECT_ROOT / "window_layouts"
LAYOUT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def layout_path(name: str) -> Path:
    """版面檔路徑。名稱限英數 / `_` / `-`，擋掉 `../` 之類的路徑穿越。"""
    key = (name or "").strip()
    if not LAYOUT_NAME_RE.match(key):
        raise GuiError("版面名稱只能是英數字、底線與連字號，長度 1..40。")
    return LAYOUT_DIR / f"{key}.json"


def save_window_layout(name: str) -> int:
    """把目前每個有標題的視窗的位置與大小存起來，回存了幾個。"""
    path = layout_path(name)
    ac = load_ac()
    try:
        LAYOUT_DIR.mkdir(parents=True, exist_ok=True)
        entries = ac.save_window_layout(str(path))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] save_window_layout failed: {error!r}", file=sys.stderr)
        raise GuiError("儲存視窗版面失敗。") from error
    return len(entries or [])


def restore_window_layout(name: str) -> int:
    """把視窗擺回存檔時的位置，回實際搬動了幾個。

    存檔之後關掉的視窗不會被重開——套件對找不到的標題就跳過，所以回傳值比存檔
    時少是正常的，呼叫端要照實講而不是報成功。
    """
    path = layout_path(name)
    if not path.is_file():
        raise GuiError("找不到這個版面。")
    ac = load_ac()
    try:
        return int(ac.restore_window_layout(str(path)))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] restore_window_layout failed: {error!r}", file=sys.stderr)
        raise GuiError("還原視窗版面失敗。") from error


def list_window_layouts() -> list[tuple[str, int, float]]:
    """`[(名稱, 視窗數, 修改時間), …]`，新的在前。"""
    out: list[tuple[str, int, float]] = []
    try:
        paths = sorted(LAYOUT_DIR.glob("*.json"))
    except OSError:
        return out
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            count = len(data) if isinstance(data, list) else 0
            out.append((path.stem, count, path.stat().st_mtime))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda row: row[2], reverse=True)
    return out


def delete_window_layout(name: str) -> None:
    path = layout_path(name)
    if not path.is_file():
        raise GuiError("找不到這個版面。")
    try:
        path.unlink()
    except OSError as error:
        raise GuiError("刪除版面失敗。") from error


# 位置名稱必須跟函式庫的 `_snap_rect` 對得上——實測 `maximize` 不是它的合法值
# （它叫 `max`），送過去只會丟 ValueError 然後被折成一句泛用失敗。這裡的清單就是
# 它支援的九種，另外把好記的 `maximize` 收成 `max` 的別名。
SNAP_POSITIONS = ("left", "right", "top", "bottom", "top-left", "top-right",
                  "bottom-left", "bottom-right", "max")
SNAP_ALIASES = {"maximize": "max", "full": "max", "tl": "top-left",
                "tr": "top-right", "bl": "bottom-left", "br": "bottom-right"}


def snap_window(needle: str, position: str) -> tuple[int, str]:
    """把第一個命中的視窗靠到螢幕的某一半／某一角。回 `(hwnd, 標題)`。"""
    key = (position or "left").strip().lower()
    key = SNAP_ALIASES.get(key, key)
    if key not in SNAP_POSITIONS:
        raise GuiError(
            "位置只能是 `left` / `right` / `top` / `bottom` / `top-left` / "
            "`top-right` / `bottom-left` / `bottom-right` / `max`。")
    matched = match_windows(needle)
    if not matched:
        raise GuiError("找不到符合的視窗。")
    hwnd, title = matched[0]
    ac = load_ac()
    try:
        ok = ac.snap_window(title, key)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] snap_window failed: {error!r}", file=sys.stderr)
        raise GuiError("靠邊排列失敗。") from error
    if not ok:
        raise GuiError("靠邊排列失敗。")
    return hwnd, title


def grid_windows(needles: list[str], *, gap: int = 0) -> int:
    """把幾個視窗排成方格，回實際排了幾個。"""
    titles: list[str] = []
    for needle in needles:
        matched = match_windows(needle)
        if not matched:
            raise GuiError(f"找不到符合「{needle}」的視窗。")
        titles.append(matched[0][1])
    ac = load_ac()
    try:
        return int(ac.arrange_grid(titles, gap=max(0, int(gap))))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] arrange_grid failed: {error!r}", file=sys.stderr)
        raise GuiError("排列視窗失敗。") from error


# --------------------------------------------------------------------------
# 背景視窗輸入（不搶焦點）
# --------------------------------------------------------------------------
# 一般的 `!click` / `!key` 走系統層輸入，一定作用在**前景**視窗，所以遠端操作會
# 打斷使用者當下在做的事。這條路徑改用 `PostMessage` 把訊息直接投遞給目標視窗。
#
# **代價要講清楚**：投遞訊息不是真的輸入。很多程式（遊戲、要求 raw input 的程式、
# 自己檢查前景狀態的程式）會直接忽略，而 `PostMessage` 成功只代表「訊息排進佇列
# 了」，不代表對方處理了——這正是本專案最在意的「靜默成功」形態，所以呼叫端**必須**
# 告訴使用者這條路是盡力而為，沒反應就改用前景操作。
#
# 走套件的 `post_key_to_window` / `post_click_to_window`，**不是**較舊的
# `send_key_event_to_window`：後者投遞給頂層視窗，而鍵盤訊息是送給**有焦點的子
# 控制項**的。實測（字元對應表）投遞給外框什麼都沒發生、投遞給焦點控制項字才進得
# 去，所以舊路徑對任何有子控制項的程式都等於沒作用——又是一種靜默成功。
def send_key_to_window(needle: str, key: str) -> tuple[int, str, str]:
    """把一次按鍵（按下＋放開）投遞給命中的視窗，不搶焦點。

    回 `(hwnd, 標題, 正規化後的鍵名)`。
    """
    name = parse_key_name(key)
    matched = match_windows(needle)
    if not matched:
        raise GuiError("找不到符合的視窗。")
    hwnd, title = matched[0]
    ac = load_ac()
    try:
        posted = ac.post_key_to_window(title, _library_key(name))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] post_key_to_window failed: {error!r}", file=sys.stderr)
        raise GuiError("送出背景按鍵失敗。") from error
    if not posted:
        raise GuiError("送出背景按鍵失敗。")
    return hwnd, title, name


def send_click_to_window(needle: str, button: str, x: int, y: int
                         ) -> tuple[int, str]:
    """把一次點選投遞給命中的視窗，不搶焦點。座標是**視窗內相對座標**。"""
    key = parse_button(button)
    matched = match_windows(needle)
    if not matched:
        raise GuiError("找不到符合的視窗。")
    hwnd, title = matched[0]
    ac = load_ac()
    try:
        posted = ac.post_click_to_window(title, key, int(x), int(y))
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] post_click_to_window failed: {error!r}", file=sys.stderr)
        raise GuiError("送出背景點選失敗。") from error
    if not posted:
        raise GuiError("送出背景點選失敗。")
    return hwnd, title


# --------------------------------------------------------------------------
# 等待條件：連接埠 / 行程 / 剪貼簿
# --------------------------------------------------------------------------
# 這三個都轉呼叫函式庫的 `smart_waits`：它同時擁有探測與輪詢，這裡只把「等一小段
# 時間」的結果折成一個 bool，讓 bot 的監看迴圈照自己的節奏問。
def parse_host_port(raw: str) -> tuple[str, int]:
    """`8080` / `127.0.0.1:8080` / `example.com:443` → `(host, port)`。"""
    text = (raw or "").strip()
    if not text:
        raise GuiError("需要連接埠，例如 `8080` 或 `127.0.0.1:8080`。")
    host = "127.0.0.1"
    port_text = text
    if ":" in text:
        host, _, port_text = text.rpartition(":")
        host = host.strip() or "127.0.0.1"
    try:
        port = int(port_text)
    except ValueError as error:
        raise GuiError("連接埠必須是整數。") from error
    if not 0 < port <= 65535:
        raise GuiError("連接埠必須介於 1 到 65535。")
    return host, port


def port_open(host: str, port: int, *, timeout: float = 1.5) -> bool:
    """那個連接埠現在接不接得上。"""
    ac = load_ac()
    try:
        outcome = ac.wait_until_port(
            host, int(port), timeout_s=max(0.2, float(timeout)),
            poll_interval_s=0.2, connect_timeout_s=1.0)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] wait_until_port failed: {error!r}", file=sys.stderr)
        return False
    return bool(outcome.succeeded)


def process_running(name: str, *, timeout: float = 1.0) -> bool:
    """有沒有這個名字的行程在跑（比對是套件那邊做的，不分大小寫的包含比對）。"""
    target = (name or "").strip()
    if not target:
        raise GuiError("需要行程名稱，例如 `notepad.exe`。")
    ac = load_ac()
    try:
        outcome = ac.wait_until_process(
            target, present=True, timeout_s=max(0.2, float(timeout)),
            poll_interval_s=0.2)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] wait_until_process failed: {error!r}", file=sys.stderr)
        return False
    return bool(outcome.succeeded)


def clipboard_changed(baseline: str, *, contains: str = "",
                      timeout: float = 1.0) -> bool:
    """剪貼簿變了沒；給了 `contains` 就改判「內容是否含那段字」。"""
    ac = load_ac()
    try:
        outcome = ac.wait_until_clipboard_changes(
            baseline=baseline, target=contains or None,
            contains=bool(contains), timeout_s=max(0.2, float(timeout)),
            poll_interval_s=0.2)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] wait_until_clipboard_changes failed: {error!r}",
              file=sys.stderr)
        return False
    return bool(outcome.succeeded)


# --------------------------------------------------------------------------
# 文字辨識（OCR）
# --------------------------------------------------------------------------
# **辨識與定位本身交給桌面自動化函式庫**（`find_text_matches` /
# `read_text_in_region` / `group_lines`）。這裡曾經自己實作過一份，因為當時函式庫
# 回的座標在這台主機上是錯的，而且錯得看不出來（點下去偏一點點，像是「有時候會
# 失敗」而不是「壞了」）：全桌面截圖是**實體像素**（本機 3840×1244）而滑鼠座標是
# 邏輯像素（3456×1244，副螢幕 125% 縮放），而且圖內座標被直接當成螢幕座標回傳，
# 忽略了虛擬桌面原點（本機 y 從 −164 起算）。這兩個缺陷連同「只比對單一辨識詞、
# 跨詞的目標永遠找不到」都已經在函式庫那邊修掉，所以本專案不再留第二份實作。
#
# 這一段剩下的是**本機環境政策**，那本來就不屬於函式庫：
#
# * 引擎執行檔的位置（安裝檔不會把自己加進 PATH）；
# * 語言資料放在專案內的 `ocr_tessdata/`（`TESSDATA_PREFIX` 指過去）而不是引擎的
#   安裝目錄——安裝目錄在 `Program Files` 底下，沒有系統管理員權限寫不進去，要求
#   使用者提權只為了補一個語言檔並不合理；
# * 依目標字串自動選辨識語言（見 `ocr_lang_for`）。
LOCAL_TESSDATA = PROJECT_ROOT / "ocr_tessdata"

# 安裝檔預設**不會**把自己加進 PATH（本機實測：裝完 `where tesseract` 仍然找不
# 到）。只查 PATH 會讓這個功能在多數主機上默默不能用，所以補上慣例安裝位置。
TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# 中日韓字元；用來決定預設辨識語言。
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")

_OCR: Any = None
_OCR_TRIED = False
_OCR_CONFIGURED = False
_OCR_REASON = "文字辨識未啟用：辨識引擎無法使用。"
_OCR_PROBED_CMD: str | None = None


def tesseract_cmd() -> str | None:
    """辨識引擎執行檔的絕對路徑；找不到回 None。

    順序：環境變數覆寫 → PATH → 慣例安裝位置。
    """
    override = (os.environ.get("TESSERACT_CMD") or "").strip()
    if override and Path(override).exists():
        return override
    import shutil
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in TESSERACT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def _configure_ocr() -> None:
    """把本機的引擎路徑與語言資料位置告訴桌面自動化函式庫（成功之後只做一次）。

    這兩件事是**本機環境政策**，不是辨識邏輯：引擎安裝檔不會把自己加進 PATH，
    而語言資料放在專案內是因為引擎安裝目錄需要系統管理員權限。辨識本身交給函式
    庫，本模組不再自己跑一份。

    ⚠️ **沒找到執行檔時不要把「已設定過」記下來。** 這個旗標原本無條件設在函式
    開頭，所以在一台還沒裝引擎的主機上，第一次呼叫就記成「設定過了」卻什麼都沒
    設定；使用者之後把引擎裝好（bot 是被監督者長期執行的行程，不會自己重啟），
    函式庫就**永遠**拿不到那個路徑——而實際的辨識是走函式庫，不是走本模組的
    `_OCR`。失敗形態是安靜的：`ocr_status()` 說可用，每個辨識指令卻回泛用的
    「文字辨識失敗。」。實測（2026-09-11）：晚裝引擎的情形下
    `set_tesseract_cmd` 被呼叫 **0** 次，而乾淨啟動的對照組是 1 次。
    """
    global _OCR_CONFIGURED  # pylint: disable=global-statement
    if _OCR_CONFIGURED:
        return
    if LOCAL_TESSDATA.is_dir() and any(LOCAL_TESSDATA.glob("*.traineddata")):
        # setdefault：使用者已經自己指定過就尊重他的設定。
        os.environ.setdefault("TESSDATA_PREFIX", str(LOCAL_TESSDATA))
    command = tesseract_cmd()
    if not command:
        return
    try:
        load_ac().set_tesseract_cmd(command)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] set_tesseract_cmd failed: {error!r}", file=sys.stderr)
        return
    _OCR_CONFIGURED = True


def _load_ocr() -> Any:
    """回傳設定好的 `pytesseract` 模組；不可用就丟 `GuiError`。

    只剩**語言清單查詢**還需要它（函式庫的門面沒有這個入口）。實際的辨識與定位
    走 `_ocr_call`，那是轉呼叫函式庫。

    ⚠️ **失敗的原因記在 `_OCR_REASON`，不要回頭呼叫 `ocr_status()` 去要那句話。**
    這裡原本寫 `raise GuiError(ocr_status()[1])`，而 `ocr_status()` 當時的第三道
    檢查就是呼叫 `_load_ocr()`——**例外的引數在 raise 之前就會被求值**，所以那個
    `except GuiError` 永遠等不到，先撞的是 `RecursionError`（實測 1000 層）。順帶
    把「辨識引擎執行檔無法執行。」那句話變成一次都沒回出去過的死碼。呼叫方向現在
    是單向的：`ocr_status()`（回報）→ `_load_ocr()`（探測），而探測自己說得出理由。
    `test_bot_helpers.py` 有一支跨專案的 AST 守門（`raise` 的引數不得走得回 raise
    所在的函式）盯著這條邊不得反向——**行為測試抓不到**「只把其中一個 raise 改回
    去」的回歸，因為下面的重探會讓那個環在第二層自己收斂，變異實測驗證過。

    ⚠️ **失敗的結果只快取到「探測當下看到的執行檔路徑」為止。** 只記一個
    `_OCR_TRIED` 的話，一台還沒裝引擎的主機上，第一個辨識指令就把 `_OCR=None` 記
    死；使用者之後把引擎裝好，這裡永遠不重試。而那正是上面那個遞迴最容易被觸發的
    路徑：實測「沒裝引擎 → 下過一次文字指令 → 把引擎裝好」之後，這個長命行程的
    `ocr_status()` 與 `_load_ocr()` 就永久 `RecursionError`，不需要壞掉的安裝、
    不需要架構不符、也不需要錯的 `TESSERACT_CMD`。成功的結果**永久快取**，而且在
    `tesseract_cmd()`（會掃 PATH，本機實測 4.0 ms）之前就早退——`wait_text` 每
    0.5 秒輪詢一次 `find_text`，那條路不能有多餘的成本（實測早退 0.0001 ms/次）。

    殘留（兩者都要重啟 bot，而且都比原本的 `RecursionError` 好）：在**同一個路徑**
    上把壞掉的引擎原地修好、以及行程跑起來之後才 pip 裝上那個 Python 套件。
    """
    global _OCR, _OCR_TRIED, _OCR_REASON, _OCR_PROBED_CMD  # pylint: disable=global-statement
    if _OCR is not None:
        return _OCR
    command = tesseract_cmd()
    if _OCR_TRIED and command == _OCR_PROBED_CMD:
        raise GuiError(_OCR_REASON)
    _OCR_TRIED = True
    _OCR_PROBED_CMD = command
    try:
        import pytesseract  # type: ignore
    except ImportError as error:
        print(f"[gui] pytesseract unavailable: {error!r}", file=sys.stderr)
        _OCR_REASON = "文字辨識未啟用：缺少辨識用的 Python 套件。"
    else:
        _configure_ocr()
        if not command:
            # 沒有執行檔就不要去問版本——問了也只會失敗，然後把「沒裝」報成
            # 「無法執行」，指向錯誤的那一半。
            _OCR_REASON = "文字辨識未啟用：辨識引擎執行檔未安裝。"
        else:
            pytesseract.pytesseract.tesseract_cmd = command
            try:
                pytesseract.get_tesseract_version()
                _OCR = pytesseract
            except Exception as error:  # pylint: disable=broad-except
                print(f"[gui] tesseract binary unusable: {error!r}",
                      file=sys.stderr)
                _OCR_REASON = "文字辨識未啟用：辨識引擎執行檔無法執行。"
    if _OCR is None:
        raise GuiError(_OCR_REASON)
    return _OCR


def _ocr_no_language_data(langs: list[str], known: bool) -> bool:
    """「引擎答得出語言清單，而且清單真的是空的」。

    ⚠️ **這條判準有兩個地方在用，但只有一個真的呼叫本函式。** `ocr_status` 直接
    呼叫它（回報整個功能不可用）；`ocr_lang_for` 表達的是同一個退化情形，但**不能**
    改寫成呼叫本函式——它的白名單還要擋「清單非空、但這個代碼不在裡面」，那是更寬
    的一條規則，`known and not langs` 只是它的一個特例。所以兩邊是**語意耦合、不是
    程式碼共用**：其中一邊改掉不會有任何症狀（`/sys doctor` 說可用而每個辨識指令都
    硬失敗，正好是這條判準要消滅的不一致），只有對帳測試會事後變紅。因此兩邊都留
    了指向對方的註解——那是唯一在**事前**提醒的東西。
    `test_gui_control.py` 餵同一份 `(langs, known)` 語料給兩個呼叫端比答案。

    `known is False`（問不到）**不算**沒有語言資料：那半的成因幾乎都是引擎本身載
    不起來，而那件事由前面的探測報得更準；為一次列舉打嗝就宣告整個功能不可用，
    正是 `ocr_lang_for` 的白名單刻意不做的事（見那裡的兩個 ⚠️）。
    """
    return known and not langs


def ocr_status() -> tuple[bool, str]:
    """`(可用嗎, 說明)`。說明是寫死的泛用字串，可直接回給使用者。

    文字辨識需要**三**件東西，所以分開回報，使用者才知道要補哪一塊：Python 套件、
    **另外安裝**的辨識引擎執行檔、以及至少一個語言資料檔。前兩塊由 `_load_ocr()`
    探測、而且由它自己說出理由（見那裡的兩個 ⚠️；`ocr_status` 不得再自己複製一份
    判斷，否則兩邊會各說各話——原本那三道重複的檢查就是遞迴的來源）。

    第三塊是 2026-09-11 補的：`ocr_lang_for` 從那天起會在「引擎答了、語言清單是空
    的」時擋下 `--lang`，而沒有 `--lang` 的那條路會回 `eng`（同樣沒安裝）讓引擎自
    己丟錯，所以那種機器上 `/locate text find` / `/locate text click` /
    `/locate text wait` 三個指令**都不可能成功**——而 `/sys doctor` 當時還是報綠燈。
    doctor 對這一格的建議句本來就寫「那三個指令都關了，圖片定位仍然可用」，在零語
    言的機器上那句話是誠實的，所以 doctor 那一側不需要改。

    **刻意接受的取捨**：原本第一道 `import pytesseract` 是每次重算的，現在跟著
    `_load_ocr` 的 `_OCR_PROBED_CMD` 走，所以「行程跑起來之後才 pip 裝上套件」要
    重啟 bot 才會被看見。換到的是「晚裝引擎會自動被重探」，那個情境常見得多。
    """
    try:
        _load_ocr()
    except GuiError as error:
        # `_load_ocr` 只會丟本模組寫死的三個字串之一（`_OCR_REASON`），所以直接
        # 轉述不會踩 Secrecy Layer 1；原始例外只進 stderr。
        return False, str(error)
    langs, known = ocr_languages()
    if _ocr_no_language_data(langs, known):
        return False, "文字辨識未啟用：缺少語言辨識資料。"
    return True, "文字辨識可用。"


def ocr_languages() -> tuple[list[str], bool]:
    """`(已安裝的語言, 問得到嗎)`。

    ⚠️ **兩個值不能合成一個。** `([], True)` ＝ 引擎答了、**真的一個語言都沒有**；
    `([], False)` ＝ **問不到**（引擎沒裝／列舉本身失敗）。合成之後這兩件事在呼叫端
    長得一模一樣，而它們該有**相反**的處置——`ocr_lang_for` 的白名單就是這樣安靜
    失效的：`t for t in wanted if available and t not in available`，空清單讓
    `available and …` 短路掉，整道檢查消失而沒有任何症狀。
    同型別的做法本專案已經有三處：`_process_control._find_all_webrunner_pids`、
    `discord_bot._load_pid`、`discord_bot._webrunner_liveness`。

    `([], True)` 是**真的到得了**，不是理論上的：實測把 `TESSDATA_PREFIX` 指到一個
    空目錄或不存在的目錄，引擎 rc=0、`get_languages(config="")` 回 `[]`，不丟例外。

    ⚠️ **不要傳 `cached=True` 給 `get_languages`。** 底層是 `@run_once`，而它只在
    收到那個旗標時才快取（`pytesseract.py:162`：
    `if not kwargs.pop('cached', False) or wrapper._result is wrapper`）。現在每次
    都是真的重問，所以使用者補上語言檔之後不必重啟 bot；加上快取的話，一次早期的
    `[]` 會凍結整個行程的壽命，而在下面那條新規則底下那等於永久硬擋。
    """
    try:
        return sorted(_load_ocr().get_languages(config="")), True
    except Exception:  # pylint: disable=broad-except
        return [], False


def ocr_lang_for(target: str, explicit: str | None = None) -> str:
    """決定辨識語言。

    預設會**看目標字串自動選**：使用者要找「確定」卻用英文模型辨識，結果是永遠
    找不到而且沒有任何線索說明為什麼。要求每次都打 `--lang chi_tra` 才是真正的
    陷阱，所以含中日韓字元就自動掛上中文模型。

    目標是空的（`read_text` 那種「畫面上寫了什麼」的用法）時同樣掛上中文模型：
    不知道要讀到什麼，就用涵蓋最廣的那組。

    `--lang` 的白名單**問不到清單時放行、清單真的是空的時擋下**。這兩種以前都是
    空清單、走同一條路（都放行），而它們該有相反的處置：

    * **問不到（`known is False`）→ 放行。** 最常見的成因就是引擎根本沒裝，而那件事
      由下游的 `_ocr_call` 報成「文字辨識未啟用：…」——一句使用者補得了的話。在這裡
      擋下只會把它換成「沒有安裝這個語言的辨識資料」，指向錯誤的那一半。代價也完全
      不對稱：列舉打個嗝就讓整個文字辨識不能用，而放行的下場只是引擎自己丟一個明確
      的錯（實測給沒安裝的語言 → `TesseractError`，已由 `_ocr_call` 折成泛用訊息，
      所以也不會把 tessdata 路徑洩到聊天室）。
    * **引擎答了、清單是空的（`known and not langs`）→ 擋。** 這種機器上任何辨識都
      不會成功，明講「已安裝：（無）」比讓引擎丟一個泛用的「文字辨識失敗」好。
      這條在修好之前是**死碼**：空清單時 `missing` 也一定是空的，`or "（無）"` 那一
      支永遠走不到。
      ⚠️ 已知的近似案例，是**選定的取捨不是漏看**：底層用 `LANG_PATTERN`
      （`^[a-z_]+$`，`pytesseract.py:51`）過濾語言名，所以只裝了大寫 script 模型
      （`Latin` / `script/Han`）的機器也會回空清單，於是連 `--lang Latin` 一起擋掉。
      那種機器本來就只剩這一條路能用——自動選語言那一段會回 `eng`，而 `eng` 沒裝。

    自動選語言那一段直接用 `langs`：它在上面兩種情況下都是 `[]`，而「不知道有什麼就
    退回 eng」本來就是它的政策，不是被跳過的檢查。
    """
    langs, known = ocr_languages()
    if explicit:
        wanted = [t.strip() for t in explicit.split("+") if t.strip()]
        if not wanted or any(not HOTKEY_TOKEN_RE.match(t) for t in wanted):
            raise GuiError("語言代碼只能是英數字，用 `+` 分隔。")
        # ⚠️ `langs` 是空的時候每個代碼都會落進 `missing`——那個退化情形就是
        # `_ocr_no_language_data`，`ocr_status` 用它回報整個功能不可用。這裡
        # **不能**改成呼叫它（白名單還要擋「清單非空但代碼不在裡面」，那是更寬
        # 的一條規則），所以兩邊是語意耦合、不是程式碼共用；有對帳測試盯著。
        # `if known:` 刻意單獨一行：跳過白名單是一個**看得見的決定**。寫成
        # `if known and any(...)` 會重現這個缺陷原本的形狀（一行 `X and Y`，
        # 其中一個運算元為假就把整道檢查靜靜關掉）。
        if known:
            missing = [t for t in wanted if t not in langs]
            if missing:
                listed = " / ".join(langs) or "（無）"
                raise GuiError(f"沒有安裝這個語言的辨識資料。已安裝：{listed}")
        return "+".join(wanted)
    if not (target or "").strip() or _CJK_RE.search(target or ""):
        for chinese in ("chi_tra", "chi_sim", "jpn"):
            if chinese in langs:
                return f"{chinese}+eng" if "eng" in langs else chinese
    return "eng" if not langs or "eng" in langs else langs[0]


def _logical_frame(region: list[int] | None):
    """`(圖, 原點x, 原點y)`——圖上的一個像素 = 一個點選座標。

    轉呼叫函式庫的 `grab_logical`。限定區域**不只是過濾結果**，是真的只截那一
    塊：辨識與比對的成本跟像素數成正比，實測 600×400 的區域比整個桌面快 4 倍。
    """
    try:
        return load_ac().grab_logical(_region_xywh(region))
    except GuiError:
        raise
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] grab_logical failed: {error!r}", file=sys.stderr)
        raise GuiError("截圖失敗。") from error


def _region_xywh(region: list[int] | None) -> tuple[int, int, int, int] | None:
    """本模組用 bbox `[左, 上, 右, 下]`，函式庫要 `(x, y, 寬, 高)`。"""
    if region is None:
        return None
    left, top, right, bottom = region
    return left, top, right - left, bottom - top


def _ocr_call(name: str, *args, **kwargs) -> Any:
    """呼叫函式庫的辨識入口，並把它的例外折成本模組的泛用 `GuiError`。

    「引擎沒裝」與「辨識失敗」分開回報：前者使用者補得了（去裝引擎），後者只能
    看 log。型別用名字比對而不是 import，避免為了一個例外型別把辨識後端的模組
    在 import 時就拉進來。
    """
    ac = load_ac()
    _configure_ocr()
    try:
        return getattr(ac, name)(*args, **kwargs)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] ocr {name} failed: {error!r}", file=sys.stderr)
        if type(error).__name__ == "OCRBackendNotAvailableError":
            raise GuiError(ocr_status()[1]) from error
        raise GuiError("文字辨識失敗。") from error


def read_text(*, region: list[int] | None = None, lang: str | None = None,
              min_confidence: float = 60.0) -> list[dict[str, Any]]:
    """把畫面（或指定區域）上的文字整段讀出來，一行一筆。

    辨識與座標換算都在函式庫裡；這裡只做「把詞併成行」的呈現。行的判定用函式庫
    的 `group_lines`（依垂直重疊分組），而不是引擎自己的行編號——不是每個辨識後端
    都會回報行編號。
    """
    ac = load_ac()
    words = _ocr_call("read_text_in_region", region=_region_xywh(region),
                      lang=lang or "eng", min_confidence=min_confidence)
    out: list[dict[str, Any]] = []
    for line in ac.group_lines(words):
        if not line:
            continue
        out.append({
            "text": " ".join(word.text for word in line),
            "x": int(sum(word.center[0] for word in line) / len(line)),
            "y": int(sum(word.center[1] for word in line) / len(line)),
            "confidence": min(word.confidence for word in line),
        })
    return sorted(out, key=lambda row: (row["y"], row["x"]))


def find_text(target: str, *, region: list[int] | None = None,
              min_confidence: float = 60.0, lang: str | None = None,
              case_sensitive: bool = False) -> list[dict[str, Any]]:
    """在螢幕上找文字，回 `[{text, x, y, confidence}, …]`（點選座標）。

    比對與座標換算都在函式庫裡：它會在同一行內找「最短的一段連續詞，串起來之後
    包含目標」——辨識引擎把一行切成很多塊，只比對單一塊會讓「另存新檔」這種很
    正常的目標永遠找不到。本模組只保留**選語言**這層政策（見 `ocr_lang_for`），
    以及把結果轉成呼叫端用的欄位名。
    """
    if not (target or "").strip():
        raise GuiError("請給要尋找的文字。")
    resolved = ocr_lang_for(target, lang)
    matches = _ocr_call("find_text_matches", target, resolved,
                        _region_xywh(region), min_confidence, case_sensitive)
    rows = [{"text": match.text, "x": match.center[0], "y": match.center[1],
             "confidence": match.confidence} for match in matches]
    return sorted(rows, key=lambda row: (row["y"], row["x"]))


def click_text(target: str, *, button: str = "mouse_left",
               region: list[int] | None = None,
               min_confidence: float = 60.0,
               lang: str | None = None) -> tuple[int, int]:
    """找到文字就點下去，回實際點選的座標。命中多處時點畫面上最前面那一處。"""
    matches = find_text(target, region=region, min_confidence=min_confidence,
                        lang=lang)
    if not matches:
        raise GuiError("畫面上找不到那段文字。")
    x, y = matches[0]["x"], matches[0]["y"]
    mouse_click(button, x, y)
    return x, y


def wait_text(target: str, timeout: float, *, region: list[int] | None = None,
              poll: float = 0.5, min_confidence: float = 60.0,
              lang: str | None = None,
              should_abort: Callable[[], bool] | None = None) -> tuple[int, int]:
    """等文字出現在螢幕上，回它的中心座標。逾時丟 `GuiError`。"""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        matches = find_text(target, region=region, min_confidence=min_confidence,
                            lang=lang)
        if matches:
            return matches[0]["x"], matches[0]["y"]
        if time.monotonic() >= deadline:
            raise GuiError("等到逾時仍沒有在畫面上看到那段文字。")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# 圖片定位
# --------------------------------------------------------------------------
# 比對本身交給桌面自動化函式庫的 `match_template_all`：它會回**相似度分數**、
# 用 NMS 併掉同一個目標周圍那一整片高分點、擋掉單色樣板，而且擷取走
# `grab_logical`（涵蓋所有螢幕、換算回點選座標空間）。這幾件事本專案曾經各寫過
# 一份，現在單一來源在函式庫那邊。
#
# 這裡剩下的是**錯誤訊息的去識別化**：函式庫的訊息是英文、而且會帶樣板圖的路徑，
# 兩者都不能直接送進對話平台。
LOCATE_MAX_HITS = 20


def _locate_all(image_path: str, threshold: float,
                region: list[int] | None = None) -> list[tuple[int, int, float]]:
    """樣板比對，回 `[(中心x, 中心y, 相似度), …]`，相似度高的在前。"""
    ac = load_ac()
    try:
        matches = ac.match_template_all(
            str(image_path), region=_region_xywh(region),
            min_score=float(threshold), max_results=LOCATE_MAX_HITS)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] match_template_all failed: {error!r}", file=sys.stderr)
        # 單色樣板是使用者改得了的（重截一塊有圖案的），值得講清楚；其餘只回
        # 泛用句。用型別名稱比對，避免為了一個例外型別多一個 import。
        if type(error).__name__ == "AutoControlFlatTemplateException":
            raise GuiError(
                "樣板圖幾乎是單一顏色，無法定位；請截一塊有圖案或文字的區域。"
            ) from error
        if isinstance(error, (OSError, ValueError)):
            raise GuiError("樣板圖讀取失敗（格式不支援？）。") from error
        raise GuiError("圖片比對失敗。") from error
    return [(match.center[0], match.center[1], match.score) for match in matches]


def locate_image(image_path: str, *, threshold: float = 0.9,
                 region: list[int] | None = None) -> tuple[int, int]:
    """在螢幕上找一張樣板圖，回最相似那一處的中心座標。"""
    hits = _locate_all(image_path, threshold, region)
    if not hits:
        raise GuiError("畫面上找不到這張圖。")
    return hits[0][0], hits[0][1]


def locate_image_all(image_path: str, *, threshold: float = 0.9,
                     region: list[int] | None = None
                     ) -> list[tuple[int, int, float]]:
    """找出所有命中處。"""
    return _locate_all(image_path, threshold, region)


def click_image(image_path: str, *, button: str = "mouse_left",
                threshold: float = 0.9,
                region: list[int] | None = None) -> tuple[int, int]:
    """找到樣板圖就點它的中心，回座標。"""
    x, y = locate_image(image_path, threshold=threshold, region=region)
    mouse_click(button, x, y)
    return x, y


def wait_image(image_path: str, timeout: float, *, poll: float = 0.6,
               threshold: float = 0.9, region: list[int] | None = None,
               should_abort: Callable[[], bool] | None = None
               ) -> tuple[int, int]:
    """等一張圖出現在畫面上，回它的中心座標。逾時丟 `GuiError`。

    辨識引擎沒裝時，這是唯一能用畫面內容做同步點的方法（`wait_text` 不可用）。
    """
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        hits = _locate_all(image_path, threshold, region)
        if hits:
            return hits[0][0], hits[0][1]
        if time.monotonic() >= deadline:
            raise GuiError("等到逾時仍沒有在畫面上看到這張圖。")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# 等待「消失」與等待顏色
# --------------------------------------------------------------------------
# 等東西**出現**只解決一半的同步問題。另一半更常見：等載入轉圈圈消失、等對話框
# 關掉、等「處理中」變成別的東西。沒有這一組的話，那些情況只能盲等一個猜出來的
# 秒數，而猜太短就在畫面還沒好的時候亂點。
def wait_text_gone(target: str, timeout: float, *,
                   region: list[int] | None = None, poll: float = 0.5,
                   min_confidence: float = 60.0, lang: str | None = None,
                   should_abort: Callable[[], bool] | None = None) -> None:
    """等某段文字從畫面上消失。逾時丟 `GuiError`。"""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        if not find_text(target, region=region, min_confidence=min_confidence,
                         lang=lang):
            return
        if time.monotonic() >= deadline:
            raise GuiError("等到逾時，那段文字還在畫面上。")
        _sleep_abortable(poll, should_abort)


def wait_window_gone(needle: str, timeout: float, poll: float = 0.5, *,
                     should_abort: Callable[[], bool] | None = None) -> None:
    """等符合的視窗消失（關掉）。逾時丟 `GuiError`。"""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        if not match_windows(needle):
            return
        if time.monotonic() >= deadline:
            raise GuiError("等到逾時，那個視窗還在。")
        _sleep_abortable(poll, should_abort)


def wait_image_gone(image_path: str, timeout: float, *, poll: float = 0.6,
                    threshold: float = 0.9, region: list[int] | None = None,
                    should_abort: Callable[[], bool] | None = None) -> None:
    """等一張圖從畫面上消失。逾時丟 `GuiError`。"""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        if not _locate_all(image_path, threshold, region):
            return
        if time.monotonic() >= deadline:
            raise GuiError("等到逾時，那張圖還在畫面上。")
        _sleep_abortable(poll, should_abort)


COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(raw: str) -> tuple[int, int, int]:
    """`"#1E90FF"` / `"1e90ff"` / `"30,144,255"` → `(30, 144, 255)`。"""
    text = (raw or "").strip()
    match = COLOR_RE.match(text)
    if match:
        value = match.group(1)
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    parts = [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
    if len(parts) == 3:
        try:
            channels = [int(p) for p in parts]
        except ValueError as error:
            raise GuiError("顏色格式不對；用 `#RRGGBB` 或 `r,g,b`。") from error
        if all(0 <= c <= 255 for c in channels):
            return channels[0], channels[1], channels[2]
    raise GuiError("顏色格式不對；用 `#RRGGBB` 或 `r,g,b`。")


PIXEL_TOLERANCE_MAX = 255


def parse_tolerance(raw: str) -> int:
    """顏色容差字串 → `0..PIXEL_TOLERANCE_MAX` 的整數（每個色版允許的誤差）。

    **巨集的存檔驗證與 `if_pixel` 的執行必須共用這一支。** 2026-09-21 實測到的
    缺陷：驗證端把 `if_pixel` 的第四個參數當秒數（跟 `wait_pixel` 共用一個分支），
    執行端卻當整數容差讀——於是 `if_pixel 10 10 #ffffff 2.5` 存得起來、重播到那一行
    才丟出沒有理由的錯誤，而 `… 150` 這種合法容差反而在存檔時被「秒數上限」擋掉。
    兩邊各寫一份解析就一定會再漂開，所以只留這一份。
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as error:
        raise GuiError("顏色容差必須是整數。") from error
    if not 0 <= value <= PIXEL_TOLERANCE_MAX:
        raise GuiError(f"顏色容差必須介於 0 到 {PIXEL_TOLERANCE_MAX}。")
    return value


def wait_pixel(x: int, y: int, color: tuple[int, int, int], timeout: float, *,
               poll: float = 0.3, tolerance: int = 12, match: bool = True,
               should_abort: Callable[[], bool] | None = None
               ) -> tuple[int, int, int]:
    """等某一點的顏色變成（或不再是）指定的顏色，回最後看到的顏色。

    `tolerance` 是每個色版允許的誤差——抗鋸齒、色彩管理與影片壓縮都會讓「同一個
    顏色」差個幾階，要求完全相等的話這個功能在真實畫面上幾乎不會成立。

    比截圖便宜非常多：一個像素 vs 一張圖 ＋ 一次辨識，所以適合放在密集輪詢的
    同步點上。
    """
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        current = pixel_color(x, y)
        close = all(abs(a - b) <= tolerance for a, b in zip(current, color))
        if close == match:
            return current
        if time.monotonic() >= deadline:
            raise GuiError("等到逾時，那個點的顏色沒有變成預期的樣子。"
                           if match else "等到逾時，那個點的顏色還是原來那個。")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# UI 元素樹（無障礙介面 / UI Automation）
# --------------------------------------------------------------------------
# 到這裡為止的三種定位方式——座標、文字辨識、樣板比對——**全都是像素層的猜測**：
# 座標怕視窗移動，辨識怕字型與縮放，樣板圖怕主題換色。作業系統其實知道畫面上有
# 哪些按鈕、叫什麼名字、在哪個矩形，那是無障礙介面的資料。
#
# 所以這一層是**首選**，不是替代：問得到就用它（精確矩形、被遮住也找得到、不受
# 縮放影響），問不到再退回原本那三種。不是每個程式都支援得一樣好——老式視窗程式、
# 遊戲、部分跨平台框架的樹可能很稀疏甚至空的，那是常態不是錯誤。
#
# 走訪與比對都在函式庫裡（`list_accessibility_elements` / `find_accessibility_
# elements` / `control_get_state`）。本模組只留三件事：型別名稱的驗證與提示、
# 把函式庫的欄位轉成呼叫端用的名字，以及**點選仍走滑鼠座標**。
#
# **限定視窗（`window`）不是過濾，是換一個搜尋起點。** 不指定視窗現在也只要約
# 2 秒（曾經是 61 秒），但限定單一視窗是 0.03 秒——而且不會漏掉排在後面的視窗。
# 那三個 60 秒的成因都在函式庫那邊修掉了：桌面全樹是一次不能中斷的呼叫、單一視窗
# 的走訪也是原子的、以及作業系統會等應用程式回應（一個全螢幕遊戲從不回應，讓單一
# 次查詢卡了 60 秒）。
# 回給使用者的命中數上限，以及「為了找它們最多看幾個元素」。這兩個是**不同的
# 數字**：把命中上限拿去當掃描上限，等於「只看畫面上前 40 個元素」，那幾乎什麼
# 都找不到。
UI_MAX_RESULTS = 40
UI_SCAN_LIMIT = 1500

# 型別名稱只用來驗證與給提示；真正的比對由函式庫做（它同時吃 `button` 與底層的
# `ControlType_50000` 兩種寫法）。
UI_CONTROL_TYPES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox",
    50004: "edit", 50005: "hyperlink", 50006: "image", 50007: "listitem",
    50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar",
    50015: "slider", 50016: "spinner", 50017: "statusbar", 50018: "tab",
    50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group",
    50027: "thumb", 50028: "datagrid", 50029: "dataitem", 50030: "document",
    50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header",
    50035: "headeritem", 50036: "table", 50037: "titlebar",
    50038: "separator",
}
_UI_TYPE_CODES = {name: code for code, name in UI_CONTROL_TYPES.items()}


def ui_status() -> tuple[bool, str]:
    """`(可用嗎, 說明)`；說明是寫死的泛用字串。"""
    if os.name != "nt":
        return False, "UI 元素定位僅 Windows 可用。"
    try:
        ok, reason = load_ac().accessibility_status()
    except GuiError as error:
        return False, str(error)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] accessibility status probe failed: {error!r}",
              file=sys.stderr)
        return False, "UI 元素定位功能無法在此環境使用。"
    # 原因字串來自函式庫（英文、可能含安裝指示），不外送；只記進 stderr。
    if not ok:
        print(f"[gui] accessibility unavailable: {reason}", file=sys.stderr)
        return False, "UI 元素定位功能無法在此環境使用。"
    return True, "UI 元素定位可用。"


def parse_ui_type(raw: str) -> str:
    """`"button"` → 驗證過的型別名稱；空字串回 `""`（不限型別）。"""
    key = (raw or "").strip().lower()
    if not key:
        return ""
    if key in _UI_TYPE_CODES:
        return key
    listed = " / ".join(sorted(_UI_TYPE_CODES)[:12])
    raise GuiError(f"不認得的元素型別。常用的有：{listed} …")


def _ui_call(func: str, *args, **kwargs) -> Any:
    """呼叫函式庫的無障礙介面入口，把它的例外折成泛用的 `GuiError`。

    第一個參數叫 `func` 不叫 `name`：這些入口本身就有一個 `name=` 關鍵字參數。
    """
    ac = load_ac()
    try:
        return getattr(ac, func)(*args, **kwargs)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] accessibility {func} failed: {error!r}", file=sys.stderr)
        if type(error).__name__ == "AccessibilityNotAvailableError":
            # 「找不到那個視窗」與「這台機器沒有這個功能」都走這個型別，但對使用者
            # 是兩件事——前者他改得了。
            if "window title" in str(error):
                raise GuiError("找不到符合的視窗。") from error
            raise GuiError("UI 元素定位功能無法在此環境使用。") from error
        raise GuiError("讀取 UI 元素失敗。") from error


def _ui_row(element: Any) -> dict[str, Any]:
    """函式庫的元素 → 呼叫端用的欄位名。"""
    left, top, width, height = element.bounds
    return {
        "name": element.name,
        "type": _ui_type_name(element.role),
        "x": element.center[0],
        "y": element.center[1],
        "left": left,
        "top": top,
        "width": width,
        "height": height,
        "enabled": bool(element.enabled),
    }


def _ui_type_name(role: str) -> str:
    """底層型別代碼 → 本模組用的小寫短名（`ControlType_50000` → `button`）。

    函式庫刻意保留原始代碼（翻譯是獨立的一步），但回給使用者的必須是看得懂的字。
    """
    try:
        return str(load_ac().humanize_role(role) or "").lower()
    except Exception:  # pylint: disable=broad-except
        return str(role or "").lower()


def _ui_visible(element: Any) -> bool:
    """有實際版面嗎。零面積的元素點不到，列出來只會誤導人。"""
    _left, _top, width, height = element.bounds
    return width > 0 and height > 0


def ui_find(name: str, *, control_type: str = "", window: str = "",
            exact: bool = False) -> list[dict[str, Any]]:
    """找名稱含 `name` 的 UI 元素，回 `[{name, type, x, y, …}, …]`。

    比對預設用「包含」而不是「完全相等」：真實介面的名稱常常帶快捷鍵標記或後綴
    （`儲存(&S)`、`確定 `），要求完全相等會讓一半的目標找不到。函式庫會把完全
    相等的排在最前面。
    """
    if not (name or "").strip():
        raise GuiError("請給要尋找的元素名稱。")
    found = _ui_call(
        "find_accessibility_elements", name=name,
        role=parse_ui_type(control_type) or None,
        window_title=window or None, contains=not exact,
        max_results=UI_MAX_RESULTS, scan_limit=UI_SCAN_LIMIT)
    return [_ui_row(element) for element in found if _ui_visible(element)]


def ui_value(name: str, *, control_type: str = "", window: str = "",
             exact: bool = False, limit: int = 5) -> list[dict[str, Any]]:
    """找元素並附上它現在的值，回 `[{name, type, x, y, value?, toggle?, …}, …]`。

    **不提供設值。** 寫入跟無障礙介面的 Invoke 是同一個問題：程式收得到新值，但
    收不到「有人真的在這裡打了字」的那串事件（焦點、逐字變更、離開焦點），所以
    驗證與連動邏輯不會跑。要填輸入框就照人的方式做——點進去再 `!type` /
    `!clip paste`。
    """
    rows = ui_find(name, control_type=control_type, window=window, exact=exact)
    out: list[dict[str, Any]] = []
    for row in rows[:max(1, limit)]:
        merged = dict(row)
        # 帶著 `window` 一起問：讀值要再走一次樹，沒有限定視窗的話那一步是整個
        # 桌面（本機實測數十秒）。
        state = _ui_call("control_get_state", name=row["name"],
                         window_title=window or None)
        merged.update(state or {})
        out.append(merged)
    return out


def ui_tree(window: str = "", limit: int = 60) -> list[dict[str, Any]]:
    """列出（某個視窗底下的）UI 元素，給人看清楚有哪些東西可以點。"""
    found = _ui_call("list_accessibility_elements",
                     window_title=window or None, max_results=max(1, limit))
    return [_ui_row(element) for element in found
            if _ui_visible(element) and element.name]


def ui_click(name: str, *, button: str = "mouse_left", control_type: str = "",
             window: str = "") -> tuple[int, int]:
    """找到元素就點它的中心，回座標。

    刻意**用滑鼠點座標**而不是呼叫無障礙介面的 Invoke：Invoke 不需要元素在畫面
    上，但也因此繞過了程式對「真的有人點了這裡」的判斷（hover 狀態、焦點、拖放）。
    這個專案要模擬的是人的操作，所以只用它拿精確位置，動作還是走滑鼠。
    """
    matches = ui_find(name, control_type=control_type, window=window)
    if not matches:
        raise GuiError("找不到這個名稱的 UI 元素。")
    target = matches[0]
    mouse_click(button, target["x"], target["y"])
    return target["x"], target["y"]


def ui_wait(name: str, timeout: float, *, control_type: str = "",
            window: str = "", poll: float = 0.5, gone: bool = False,
            should_abort: Callable[[], bool] | None = None
            ) -> dict[str, Any] | None:
    """等某個 UI 元素出現（或消失）。逾時丟 `GuiError`。"""
    deadline = time.monotonic() + timeout
    while True:
        _check_abort(should_abort)
        matches = ui_find(name, control_type=control_type, window=window)
        if gone and not matches:
            return None
        if not gone and matches:
            return matches[0]
        if time.monotonic() >= deadline:
            raise GuiError("等到逾時，那個 UI 元素還在。" if gone
                           else "等到逾時仍沒有出現這個 UI 元素。")
        _sleep_abortable(poll, should_abort)


# --------------------------------------------------------------------------
# 檔案進出主機
# --------------------------------------------------------------------------
# 沒有這一段，「用對話完成任何操作」就少一塊：指令執行做得到「動」，但**把一個
# 檔案拿回來看**或**把一個檔案放上去**做不到。`!sh` 的輸出會被去識別化又有長度
# 上限，拿它當檔案傳輸管道並不可行。
#
# 上限分開設：拿回來受對話平台的附件大小限制，放上去只受磁碟限制。
GET_MAX_BYTES = 20 * 1024 * 1024
PUT_MAX_BYTES = 64 * 1024 * 1024


def unquote_path(raw: str | None) -> str:
    """把使用者貼進來的路徑字串正規化：去前後空白，再脫掉**成對**的引號一層。

    存在的理由是檔案總管的「複製路徑」（Shift ＋右鍵）自帶雙引號，貼進來就是
    `"D:\\Work\\Foo"`——那不是任何一個存在的路徑。

    只脫**成對**的一層。原本這裡（以及另外兩處）寫的是
    `.strip('"').strip("'")`，那會把頭尾**所有**引號字元一路刮掉：真的叫 `'foo'`
    的目錄（`'` 在 Windows 檔名裡是合法的）會被悄悄改成 `foo`，於是
    `/host put`、`/host cd` 指向另一個地方——**安靜地指錯位置比乾脆被拒更糟**，
    因為這條路是限擁有者的「用對話操作整台電腦」，寫錯地方就是真的寫錯地方。
    引號沒配對時原樣留著、讓它自然解析失敗，也不要猜使用者的意思。

    與 `dorossi_backend._dorossi_unquote_dir` 是同一條判準的兩份實作（模組邊界
    不同，不互相 import）。改其中一份時記得看另一份。
    """
    text = (raw or "").strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text[0] == quote and text[-1] == quote:
            return text[1:-1].strip()
    return text


def resolve_host_path(raw: str) -> Path:
    """把使用者打的路徑轉成絕對路徑。相對路徑以專案根目錄為基準。

    刻意**不做沙箱**：這個能力的呼叫端限擁有者，而限制在專案目錄內等於讓「用
    對話操作整台電腦」這件事名不副實。真正的閘門在呼叫端的身分檢查。
    """
    text = unquote_path(raw)
    if not text:
        raise GuiError("請給檔案路徑。")
    expanded = os.path.expandvars(os.path.expanduser(text))
    path = Path(expanded)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return Path(os.path.normpath(str(path)))


# Windows 保留名稱的判準**單一來源在標準函式庫**（`ntpath.isreserved`，3.13+）。
# 刻意不自己抄那 30 個裝置名：那份資料會隨 Windows 版本長大（`COM¹`／`COM²`／
# `COM³` 是 Windows 11 才加的），抄本不會——本 repo 已經有三份 `_pid_alive` 的前例。
# 用 `ntpath` 而不是 `os.path`：`os.path.isreserved` 只在 Windows 上存在，而寫入
# 目標永遠是 Windows 主機，判準不該跟著「跑測試的平台」走。
#
# 取成模組層別名是為了讓缺少它的直譯器**在匯入時**就炸掉。寫在函式裡的話，
# `AttributeError` 會被 `cmd_put` 的 broad except 折成一句「檔案寫入失敗」——守衛
# 安靜消失，而那正是這道守衛存在的理由。
_isreserved_name = ntpath.isreserved


def _reject_reserved_filename(name: str) -> None:
    """`name` 會被 Windows 特殊對待的話就丟 `GuiError`。

    `ntpath.isreserved` 一次回答三件事，這三件的共同點是**寫下去不會得到使用者
    要的那個檔案**：

    1. DOS 裝置名（`NUL`／`CON`／`COM1`…，不分大小寫，`NUL.md` 一樣算中）；
    2. 保留字元 `*?"<>/\\:|` 與 ASCII 控制字元；
    3. 結尾的點與空白。

    **只剝掉結尾的點與空白再問**，不是取「第一個點之前的字段」。兩種寫法都能繞開
    第 3 條（本專案已裁定全點名字要原樣通過，見下），但取字段會讓第 2 條**只看得到
    第一個點之前**。實測差 8 筆，而且差的那些正是最該擋的：`report.txt:secret`
    寫下去會產生一個 NTFS 替代資料流——`os.listdir` 看不到它，`report.txt` 本身是
    **0 位元組**，而呼叫端回報成功。回報成功、資料看不見，是本專案最在意的那種
    無聲錯誤。同族的還有 `a.txt\\tb`、`a.b<c`、`a.b|c`。

    剝掉結尾點空白之後，`...` / `....` / `x.` 會變成 `` / `x`，兩者都不保留，所以
    原樣通過——那是既有裁定，不是疏漏：
    `test_bot_helpers.test_the_attachment_name_guard_lets_nothing_escape` 釘著全點
    名字要通過，理由是它寫檔時 `os.replace` 丟 errno 13 **大聲**失敗，不會靜靜寫錯
    地方。要改那個決定請去改那一支。

    ⚠️ **這道守衛刻意比「這台機器上真的會出事的名字」寬，不要照著實測收窄。**
    2026-09-11 以 `CreateFileW` ＋ `GetFileType` 量過（Windows 11 build 26200）：
    只有 `NUL` 家族真的被路徑剖析器改寫成 `\\\\.\\NUL`，`CON`／`PRN`／`AUX`／
    `COM1`／`LPT1`／`CONIN$` 全部產生 `type=DISK` 的**普通檔案**。舊版 Windows 不是
    這樣，而標準函式庫自己的註解就寫著「規則複雜且隨版本不同，保守起見一律回
    True」。在這台機器上試出 `CON` 沒事就把它拿掉，等於讓 `write_host_file` 的行為
    跟著主機的 Windows 版本跑——那是 fresh clone 會踩到而本機永遠看不到的坑。
    """
    if _isreserved_name(name.rstrip(". ")):
        raise GuiError(
            "這個檔名 Windows 會特殊對待（`NUL`／`CON`／`COM1` 這類裝置名，或含有 "
            "`: * ? \" < > |` 與控制字元），寫下去不會得到你要的那個檔案——"
            "資料可能消失或跑進一個看不見的資料流。請換一個檔名。")


def safe_basename(name: str) -> str:
    """從使用者 / 附件給的名稱取出乾淨的檔名，擋掉路徑穿越與保留名稱。"""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    if not base or base in (".", ".."):
        raise GuiError("檔名不合法。")
    _reject_reserved_filename(base)
    return base


def read_host_file(raw: str) -> tuple[Path, bytes]:
    """讀主機上的檔案，回 `(路徑, 內容)`。"""
    path = resolve_host_path(raw)
    if path.is_dir():
        raise GuiError("那是一個資料夾，不是檔案。")
    if not path.exists():
        raise GuiError("找不到這個檔案。")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise GuiError("檔案讀取失敗。") from error
    if size > GET_MAX_BYTES:
        raise GuiError(f"檔案太大（上限 {GET_MAX_BYTES // (1024 * 1024)} MB）。")
    try:
        return path, path.read_bytes()
    except OSError as error:
        raise GuiError("檔案讀取失敗。") from error


def write_host_file(raw: str, data: bytes, *, default_name: str = "",
                    overwrite: bool = False) -> tuple[Path, int]:
    """把內容寫到主機上，回 `(路徑, 位元組數)`。

    * 目的地是既有資料夾（或以斜線結尾）時，用 `default_name` 當檔名；
    * 上層資料夾不存在就直接失敗，**不自動建立整棵樹**——打錯一個字就多出一串
      空目錄，而下指令的人不在電腦前面看不到；
    * 已存在的檔案要 `overwrite` 才蓋掉；
    * 寫入走同目錄 temp ＋ `os.replace`（跨行程檔案一律原子寫入，見 CLAUDE.md）。
    """
    if len(data) > PUT_MAX_BYTES:
        raise GuiError(f"檔案太大（上限 {PUT_MAX_BYTES // (1024 * 1024)} MB）。")
    # 正規化只有一份（`unquote_path`）。`resolve_host_path` 自己也會叫它——
    # 冪等，所以叫兩次無害；這裡需要正規化後的字串是為了下一行的 `endswith`。
    text = unquote_path(raw)
    path = resolve_host_path(text)
    if path.is_dir() or text.endswith(("/", "\\")):
        if not default_name:
            raise GuiError("目的地是資料夾，請給完整檔名。")
        path = path / safe_basename(default_name)
    # 明寫路徑那條（`/host put D:\x\NUL`）**不經過** `safe_basename`，所以這裡再問
    # 一次——冪等，跟上面 `unquote_path` 叫兩次同一個理由。
    #
    # ⚠️ 順序是承重的：要擋在 `path.exists()` **前面**。`Path(r"…\NUL").exists()`
    # 是 True，所以擋晚了的話使用者先拿到「目的檔已經存在；要覆寫請加上 `--force`」
    # ——一句把他導向 `--force`、而 `--force` 之後只會拿到「檔案寫入失敗。」
    # （2026-09-11 實測的實際下場：兩句都是錯的答案，而這是一個擁有者拿來操作一台
    # 他不在現場的機器的指令）。
    _reject_reserved_filename(path.name)
    if not path.parent.is_dir():
        raise GuiError("目的資料夾不存在。")
    if path.exists() and not overwrite:
        raise GuiError("目的檔已經存在；要覆寫請加上 `--force`。")
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as error:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise GuiError("檔案寫入失敗。") from error
    return path, len(data)


# --------------------------------------------------------------------------
# 巨集
# --------------------------------------------------------------------------
MACRO_MAX_STEPS = 200
MACRO_MAX_WAIT_SEC = 120.0
MACRO_SCHEMA_VERSION = 1

# 每個動詞 → 需要的參數個數 `(最少, 最多)`；`None` 代表不限。
# `sh` 刻意**不在**這張表裡：巨集是存在磁碟上、任何有權下指令的人都能重播的
# 東西，把任意指令執行藏在巨集裡等於做出一個「存起來的遠端執行後門」，繞過
# `!sh` 的擁有者閘門。要跑指令就自己打 `!sh`。
# 條件動詞 → `(判斷函式, 是否取反)`。八個是機械式的兩兩成對，實作走同一條路。
# **成本差很多**，說明文件要講清楚：`if_pixel` 幾乎免費、`if_window` / `if_ui`
# 是毫秒到一秒、`if_text` 要跑一次辨識（全桌面 3 秒起跳）。放在 `repeat` 裡面的
# 條件用錯一個，整個巨集會從幾秒變成幾分鐘。
MACRO_CONDITIONS: dict[str, tuple[str, bool]] = {
    "if_text": ("text", False), "if_no_text": ("text", True),
    "if_window": ("window", False), "if_no_window": ("window", True),
    "if_ui": ("ui", False), "if_no_ui": ("ui", True),
    "if_pixel": ("pixel", False), "if_no_pixel": ("pixel", True),
}

MACRO_VERBS: dict[str, tuple[int, int | None]] = {
    # --- 控制流 ---
    "repeat": (1, 1),       # 次數
    "else": (0, 0),
    "end": (0, 0),
    "stop": (0, 0),         # 提早結束整個巨集（不算失敗）
    "call": (1, None),      # 巨集名稱 [參數…]
    **{verb: ((3, 4) if verb.endswith("pixel") else (1, None))
       for verb in MACRO_CONDITIONS},
    # --- 動作 ---
    "click": (2, 3),        # x y [button]
    "move": (2, 2),         # x y
    "drag": (4, 5),         # x1 y1 x2 y2 [button]
    "scroll": (1, 3),       # amount [x y]
    "dclick": (2, 3),       # x y [button]
    "type": (1, None),      # text…
    "paste": (1, None),     # text…
    "hotkey": (1, 1),       # combo
    "keydown": (1, 1),      # 鍵名（按住不放）
    "keyup": (1, 1),        # 鍵名
    "release_keys": (0, 0),  # 全部放開
    "focus": (1, None),     # 視窗標題片段
    "win": (2, None),       # action 視窗標題片段
    "wait": (1, 1),         # 秒
    "wait_window": (1, None),   # [timeout] 標題片段 —— 見 parse 說明
    "wait_text": (1, None),
    "click_text": (1, None),
    "clip": (1, None),      # set <text>
    "ui_click": (1, None),      # 元素名稱
    "wait_ui": (1, None),       # [秒] 元素名稱
    "wait_gone_text": (1, None),
    "wait_gone_window": (1, None),
    "wait_pixel": (3, 4),       # x y 顏色 [秒]
}

# 一次執行最多跑幾步。`repeat` 可以巢狀，光靠來源行數上限擋不住無窮迴圈——
# 一個 `repeat 1000` 裡面包 `repeat 1000` 只有四行卻要跑一百萬步。
MACRO_MAX_EXECUTED = 5000
MACRO_MAX_REPEAT = 1000
MACRO_MAX_CALL_DEPTH = 3
# `$1`..`$9` 是參數，`$$` 是一個字面的 `$`；語法全文見 `substitute_macro_args`。
MACRO_ARG_RE = re.compile(r"\$(\$|[1-9])")

# 事前檢查（`check_macro_program`）最多驗幾步。以 `(巨集名, 參數)` 做記憶化之後，
# 「參數原樣往下傳」這種常見寫法的工作量是「不同巨集數 × 步數」；但記憶化**不是**
# 上界：每一層都能用字面參數分出不同的鍵（`call b 1` … `call b 200`，`b` 裡再
# `call c $1 1` … `call c $1 200`），三層就是八百萬個不同的鍵、十幾億次驗證。執行
# 端有 `MACRO_MAX_EXECUTED` 擋著，事前檢查要有自己的上限，否則檢查會比執行還久，
# 而且卡在工作執行緒裡沒人收得回來。兩萬步 ＝ 一百個不同的滿載巨集，正常用法碰
# 不到。2026-09-21 實測：四層各 200 行、參數原樣往下傳的扇出，記憶化後驗 1,400 次
# （不記憶化是十六億次）、0.03 秒；三層字面參數的扇出撞到這個上限，0.07 秒結束。
MACRO_MAX_CHECKED = 20000

# 開頭可以帶一個逾時秒數的動詞 → 沒給時的預設秒數。**存檔驗證與執行共用這張表**：
# `run_macro_step` 從這裡拿預設值交給 `split_timeout`，`validate_macro_step` 對同一
# 批動詞用同一支 `split_timeout` 先解析一次。兩邊各列一份名單的時候，驗證端只記得
# `wait_window` / `wait_text`，於是 `wait_ui 150 確定`、`wait_gone_text -1 x`、
# `click_text nan x` 都存得起來、重播到那一行才炸（2026-09-21 實測）。
# `click_text` 的秒數會被丟掉（一次性辨識沒有等待迴圈），但它照樣經過解析，所以
# 一樣要在存檔時擋掉壞值。
MACRO_TIMEOUT_DEFAULTS: dict[str, float] = {
    "wait_window": 15.0,
    "wait_text": 15.0,
    "click_text": 0.0,
    "wait_ui": 15.0,
    "wait_gone_text": 15.0,
    "wait_gone_window": 15.0,
}


def parse_macro_steps(raw: str) -> list[str]:
    """把使用者貼的多行文字轉成正規化後的步驟清單，順便驗證。

    每行一步，`#` 開頭與空行忽略。驗證在**存檔時**就做一次，這樣壞掉的巨集
    不會等到重播到一半才炸；`load_macro` 會再驗一次，因為檔案在磁碟上是可以
    被手動編輯的。
    """
    steps: list[str] = []
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        # 讓使用者可以直接把 `!` 指令貼進來，不用逐行去掉驚嘆號
        if text.startswith("!"):
            text = text[1:].strip()
        try:
            validate_macro_step(text)
        except GuiError as error:
            raise GuiError(f"第 {line_no} 行：{error}") from error
        steps.append(text)
        if len(steps) > MACRO_MAX_STEPS:
            raise GuiError(f"一個巨集最多 {MACRO_MAX_STEPS} 步。")
    if not steps:
        raise GuiError("巨集內容是空的。")
    macro_block_map(steps)   # 區塊平衡在存檔時就驗，不要留到重播到一半才爆
    return steps


def macro_block_map(steps: list[str]) -> dict[int, tuple[int | None, int]]:
    """把每個 `repeat` / `if_*` 對到它的 `else`（若有）與 `end`。

    區塊結構在**存檔時**就驗一次：少一個 `end` 是很容易犯的錯，而等到重播跑到
    一半才發現，前面那些步驟已經在真實桌面上做過了，收不回來。

    回 `{開頭行號: (else 行號 或 None, end 行號)}`。
    """
    stack: list[tuple[int, str]] = []
    blocks: dict[int, tuple[int | None, int]] = {}
    elses: dict[int, int] = {}
    for index, step in enumerate(steps):
        verb = (step or "").split()[0].lower() if (step or "").split() else ""
        if verb == "repeat" or verb in MACRO_CONDITIONS:
            stack.append((index, verb))
        elif verb == "else":
            if not stack or stack[-1][1] not in MACRO_CONDITIONS:
                raise GuiError(f"第 {index + 1} 行：`else` 必須在 `if_…` 區塊裡。")
            if stack[-1][0] in elses:
                raise GuiError(f"第 {index + 1} 行：同一個 `if_…` 只能有一個 `else`。")
            elses[stack[-1][0]] = index
        elif verb == "end":
            if not stack:
                raise GuiError(f"第 {index + 1} 行：多出來的 `end`。")
            opener, _kind = stack.pop()
            blocks[opener] = (elses.get(opener), index)
    if stack:
        line = stack[-1][0] + 1
        raise GuiError(f"第 {line} 行的區塊沒有對應的 `end`。")
    return blocks


def validate_macro_step(step: str) -> tuple[str, list[str]]:
    """驗證單一步驟，回 `(動詞, 參數)`。不合法就丟 `GuiError`。"""
    parts = (step or "").split()
    if not parts:
        raise GuiError("空的步驟。")
    verb = parts[0].lower()
    args = parts[1:]
    if verb not in MACRO_VERBS:
        allowed = " / ".join(sorted(MACRO_VERBS))
        raise GuiError(f"不認得的動作 `{verb}`。可用：{allowed}")
    low, high = MACRO_VERBS[verb]
    if len(args) < low or (high is not None and len(args) > high):
        raise GuiError(f"`{verb}` 的參數個數不對。")
    # 逐動詞的細部檢查——存檔時就擋掉，不要留到重播才失敗
    if verb in ("click", "move", "dclick"):
        parse_xy(args[:2])
        if len(args) > 2:
            parse_button(args[2])
    elif verb == "drag":
        parse_xy(args[:2])
        parse_xy(args[2:4])
        if len(args) > 4:
            parse_button(args[4])
    elif verb == "scroll":
        try:
            int(args[0])
        except ValueError as error:
            raise GuiError("`scroll` 的第一個參數必須是整數。") from error
        if len(args) == 3:
            parse_xy(args[1:3])
        elif len(args) == 2:
            raise GuiError("`scroll` 的座標要嘛不給、要嘛給兩個。")
    elif verb == "hotkey":
        parse_hotkey_tokens(args[0])
    elif verb in ("keydown", "keyup"):
        parse_key_name(args[0])
    elif verb == "repeat":
        try:
            count = int(args[0])
        except ValueError as error:
            raise GuiError("`repeat` 的次數必須是整數。") from error
        if not 0 <= count <= MACRO_MAX_REPEAT:
            raise GuiError(f"`repeat` 的次數必須介於 0 到 {MACRO_MAX_REPEAT}。")
    elif verb == "call":
        macro_path(args[0])          # 名稱合法性（也擋路徑穿越）
    elif verb in ("if_pixel", "if_no_pixel"):
        # 第四個參數是**顏色容差**（整數），不是秒數——跟 `eval_macro_condition`
        # 走同一支 `parse_tolerance`，不要併回下面 `wait_pixel` 那一支。
        parse_xy(args[:2])
        parse_color(args[2])
        if len(args) > 3:
            parse_tolerance(args[3])
    elif verb == "wait_pixel":
        # 這裡的第四個參數才是逾時秒數（見 `run_macro_step`）
        parse_xy(args[:2])
        parse_color(args[2])
        if len(args) > 3:
            parse_duration(args[3], maximum=MACRO_MAX_WAIT_SEC)
    elif verb == "wait":
        parse_duration(args[0], maximum=MACRO_MAX_WAIT_SEC)
    elif verb == "win":
        action = args[0].lower()
        if action != "close" and action not in WINDOW_SHOW_ACTIONS:
            raise GuiError("`win` 的動作只能是 `min` / `max` / `restore` / `show` / `hide` / `close`。")
    elif verb in MACRO_TIMEOUT_DEFAULTS:
        # 第一個參數如果是數字就當逾時秒數，剩下的是目標。直接呼叫執行端用的
        # 那一支 `split_timeout`，不要在這裡再寫一次「像不像數字」的判斷。
        split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
    elif verb == "clip":
        if args[0].lower() != "set" or len(args) < 2:
            raise GuiError("巨集裡的 `clip` 只能是 `clip set <文字>`。")
    return verb, args


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def run_macro_step(step: str, *,
                   should_abort: Callable[[], bool] | None = None) -> str:
    """執行單一步驟，回一句可以直接顯示給使用者的敘述（**同步阻塞**）。

    `should_abort` 會往下傳給所有會等待的動作。沒有它的話「中止在步驟之間生效」
    等於「最久要等 `MACRO_MAX_WAIT_SEC` 才生效」——單一 `wait_text 120 …` 就能讓
    `!macro stop` 兩分鐘沒有反應。
    """
    verb, args = validate_macro_step(step)
    if verb == "click":
        x, y = parse_xy(args[:2])
        button = parse_button(args[2]) if len(args) > 2 else "mouse_left"
        mouse_click(button, x, y)
        return f"點選 ({x}, {y})"
    if verb == "dclick":
        x, y = parse_xy(args[:2])
        button = parse_button(args[2]) if len(args) > 2 else "mouse_left"
        mouse_click(button, x, y, times=2)
        return f"雙擊 ({x}, {y})"
    if verb == "move":
        x, y = parse_xy(args[:2])
        mouse_move(x, y)
        return f"移動到 ({x}, {y})"
    if verb == "drag":
        x1, y1 = parse_xy(args[:2])
        x2, y2 = parse_xy(args[2:4])
        button = parse_button(args[4]) if len(args) > 4 else "mouse_left"
        mouse_drag(x1, y1, x2, y2, button)
        return f"拖曳 ({x1}, {y1}) → ({x2}, {y2})"
    if verb == "scroll":
        amount = int(args[0])
        if len(args) == 3:
            x, y = parse_xy(args[1:3])
            mouse_scroll(amount, x, y)
        else:
            mouse_scroll(amount)
        return f"捲動 {amount}"
    if verb == "type":
        text = " ".join(args)
        type_text(text)
        return f"輸入 {len(text)} 字"
    if verb == "paste":
        text = " ".join(args)
        paste_text(text)
        return f"貼上 {len(text)} 字"
    if verb == "hotkey":
        tokens = parse_hotkey_tokens(args[0])
        press_hotkey(tokens)
        return f"按鍵 {' + '.join(tokens)}"
    if verb == "keydown":
        return f"按住 {key_down(args[0])}"
    if verb == "keyup":
        return f"放開 {key_up(args[0])}"
    if verb == "release_keys":
        # 要回報人看的步驟敘述，所以走 `_report`：放不掉的鍵不算進「放開」，也不能就此
        # 不提——它們還列在 `/input key status`、巨集結束時會再試一次。
        released, stuck = release_all_inputs_report()
        detail = f"{len(released)} 個"
        if stuck:
            detail += f"；另有 {len(stuck)} 個放不掉"
        return f"放開全部按鍵（{detail}）"
    if verb == "focus":
        _hwnd, _title, count = window_focus(" ".join(args))
        return f"聚焦視窗（命中 {count} 個）"
    if verb == "win":
        action = args[0].lower()
        needle = " ".join(args[1:])
        if action == "close":
            _hwnd, _title, count = window_close(needle)
        else:
            _hwnd, _title, count = window_show(needle, action)
        return f"視窗 {action}（命中 {count} 個）"
    if verb == "wait":
        seconds = parse_duration(args[0], maximum=MACRO_MAX_WAIT_SEC)
        _sleep_abortable(seconds, should_abort)
        return f"等待 {seconds:g} 秒"
    if verb == "wait_window":
        timeout, needle = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        wait_window(needle, timeout, should_abort=should_abort)
        return "視窗已出現"
    if verb == "wait_text":
        timeout, target = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        x, y = wait_text(target, timeout, should_abort=should_abort)
        return f"文字已出現於 ({x}, {y})"
    if verb == "click_text":
        timeout, target = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        del timeout
        x, y = click_text(target)
        return f"點選文字於 ({x}, {y})"
    if verb == "clip":
        text = " ".join(args[1:])
        set_clipboard(text)
        return f"寫入剪貼簿 {len(text)} 字"
    if verb == "ui_click":
        x, y = ui_click(" ".join(args))
        return f"點選 UI 元素於 ({x}, {y})"
    if verb == "wait_ui":
        timeout, target = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        found = ui_wait(target, timeout, should_abort=should_abort)
        return f"UI 元素已出現於 ({found['x']}, {found['y']})"
    if verb == "wait_gone_text":
        timeout, target = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        wait_text_gone(target, timeout, should_abort=should_abort)
        return "文字已消失"
    if verb == "wait_gone_window":
        timeout, needle = split_timeout(args, default=MACRO_TIMEOUT_DEFAULTS[verb])
        wait_window_gone(needle, timeout, should_abort=should_abort)
        return "視窗已關閉"
    if verb == "wait_pixel":
        x, y = parse_xy(args[:2])
        color = parse_color(args[2])
        timeout = (parse_duration(args[3], maximum=MACRO_MAX_WAIT_SEC)
                   if len(args) > 3 else 15.0)
        wait_pixel(x, y, color, timeout, should_abort=should_abort)
        return f"({x}, {y}) 已變成指定顏色"
    raise GuiError(f"不認得的動作 `{verb}`。")


def eval_macro_condition(verb: str, args: list[str]) -> bool:
    """判斷一個 `if_…` 條件。"""
    kind, negate = MACRO_CONDITIONS[verb]
    if kind == "text":
        result = bool(find_text(" ".join(args)))
    elif kind == "window":
        result = bool(match_windows(" ".join(args)))
    elif kind == "ui":
        result = bool(ui_find(" ".join(args)))
    else:
        x, y = parse_xy(args[:2])
        color = parse_color(args[2])
        # 與 `validate_macro_step` 同一支解析；存檔時放行的值這裡一定讀得懂
        tolerance = parse_tolerance(args[3]) if len(args) > 3 else 12
        current = pixel_color(x, y)
        result = all(abs(a - b) <= tolerance for a, b in zip(current, color))
    return (not result) if negate else result


def split_timeout(args: list[str], *, default: float) -> tuple[float, str]:
    """`["10", "存檔"]` → `(10.0, "存檔")`；`["存檔"]` → `(default, "存檔")`。"""
    if len(args) > 1 and _looks_numeric(args[0]):
        return parse_duration(args[0], maximum=MACRO_MAX_WAIT_SEC), " ".join(args[1:])
    return default, " ".join(args)


def macro_path(name: str) -> Path:
    """巨集檔路徑。名稱限英數 / `_` / `-`，擋掉 `../` 之類的路徑穿越。"""
    key = (name or "").strip()
    if not MACRO_NAME_RE.match(key):
        raise GuiError("巨集名稱只能是英數字、底線與連字號，長度 1..40。")
    return MACRO_DIR / f"{key}.json"


def _atomic_write(path: Path, content: str) -> None:
    """同目錄 temp → `os.replace`（跨行程檔案一律原子寫入，見 CLAUDE.md）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        raise


def _macro_author_id(raw: Any) -> int:
    """把磁碟上的 `author_id` 正規化成一個普通的非負 `int`；壞掉一律回 0。

    `save_macro` 寫出去的是五個欄位，而 `load_macro` 的 docstring 雖然寫著「重新
    驗證每一步」，實際上只重驗了 `steps`。`author_id` 會被 `edit_macro` 拿去
    `int(...)`，而巨集檔在磁碟上是可以被手改壞的：`"abc"` → `ValueError`、
    `[1, 2]` / `{"a": 1}` → `TypeError`、`Infinity` → `OverflowError`、`NaN` →
    `ValueError`。`/macro` 那一側只 `except GuiError`，所以這些會一路冒到派送層
    的泛用失敗句，使用者**再也沒辦法用 bot 把那個巨集改回來**——而這整組指令
    存在的理由正是「下指令的人不在電腦前面」。

    `1e400` 那一種甚至不必有人手改檔案：一個夠大的數字 `json.loads` 出來就是
    `inf`（本專案已經為同一個數值家族寫過一整批守門）。

    壞掉的值退回 0（＝不知道是誰）而**不是**丟 `GuiError`：`steps` 是行為、壞了
    不能執行；`author_id` 只是註記——為了一個註記拒收整份巨集，等於把修復路徑跟
    著關掉，而這正是要修的那個缺陷本身的形狀。

    `bool` 要單獨排除：`isinstance(True, int)` 是 True，所以 `true` 會穿過一個天
    真的 `isinstance(x, int)` 閘，然後把作者安靜地記成使用者 1（一個真的存在的 id）。
    這是本專案同一個陷阱的第四個實例。

    刻意**不設上限**：平台的使用者 id 是 64-bit，`400000000000000001` 是合法值，
    隨手加一個上限就會把真的作者砍成 0。
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if raw >= 0 else 0


def save_macro(name: str, steps: list[str], *, author_id: int = 0) -> Path:
    path = macro_path(name)
    payload = {
        "version": MACRO_SCHEMA_VERSION,
        "name": path.stem,
        "created": time.time(),
        "author_id": _macro_author_id(author_id),
        "steps": list(steps),
    }
    try:
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError as error:
        raise GuiError("巨集存檔失敗。") from error
    return path


def load_macro(name: str) -> dict[str, Any]:
    """讀巨集並**重新驗證**每一步——檔案在磁碟上是可以被手動改壞的。"""
    path = macro_path(name)
    if not path.exists():
        raise GuiError("找不到這個巨集。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GuiError("巨集檔讀取失敗或格式損毀。") from error
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not steps:
        raise GuiError("巨集檔內容不合法。")
    if len(steps) > MACRO_MAX_STEPS:
        raise GuiError("巨集步驟數超過上限。")
    clean: list[str] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, str):
            raise GuiError(f"巨集第 {index} 步不是文字。")
        try:
            validate_macro_step(step)
        except GuiError as error:
            raise GuiError(f"巨集第 {index} 步不合法：{error}") from error
        clean.append(step)
    macro_block_map(clean)
    data["steps"] = clean
    # `steps` 以外的欄位也是手改得了的。`version` / `name` / `created` 每次寫回
    # 去都由 `save_macro` 重新產生，壞了不會流到下游；`author_id` 不同——它會被
    # 原值帶回 `save_macro`，所以在**讀取端**就正規化，每一個呼叫端都受惠。
    data["author_id"] = _macro_author_id(data.get("author_id"))
    return data


def edit_macro(name: str, line: int, step: str | None, *,
               insert: bool = False) -> list[str]:
    """改／插入／刪掉巨集的某一行，回改完的步驟清單。

    `step is None` 代表刪除。行號從 1 開始（跟 `/macro show` 顯示的一致）。
    改完會**整份重驗**（單行合法性 ＋ 區塊平衡）才寫回：只驗那一行的話，一個
    刪掉的 `end` 會讓整個巨集在下次重播時才爆。
    """
    data = load_macro(name)
    steps = list(data["steps"])
    if insert:
        if not 1 <= line <= len(steps) + 1:
            raise GuiError(f"行號必須介於 1 到 {len(steps) + 1}。")
    elif not 1 <= line <= len(steps):
        raise GuiError(f"行號必須介於 1 到 {len(steps)}。")
    if step is None:
        steps.pop(line - 1)
    elif insert:
        steps.insert(line - 1, step.strip())
    else:
        steps[line - 1] = step.strip()
    if not steps:
        raise GuiError("巨集不能變成空的；要整個刪掉請用 `/macro delete`。")
    if len(steps) > MACRO_MAX_STEPS:
        raise GuiError(f"一個巨集最多 {MACRO_MAX_STEPS} 步。")
    for index, entry in enumerate(steps, start=1):
        try:
            validate_macro_step(entry)
        except GuiError as error:
            raise GuiError(f"第 {index} 步不合法：{error}") from error
    macro_block_map(steps)
    # `load_macro` 已經把它正規化成非負 `int` 了，這裡不必再 `int(...)` 一次。
    save_macro(name, steps, author_id=data.get("author_id", 0))
    return steps


def list_macros() -> list[tuple[str, int, float]]:
    """`[(名稱, 步驟數, mtime), …]`，依名稱排序。壞掉的檔跳過不擋列表。"""
    if not MACRO_DIR.exists():
        return []
    out: list[tuple[str, int, float]] = []
    for path in sorted(MACRO_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            steps = data.get("steps") if isinstance(data, dict) else []
            count = len(steps) if isinstance(steps, list) else 0
            out.append((path.stem, count, path.stat().st_mtime))
        except (OSError, ValueError):
            continue
    return out


def delete_macro(name: str) -> None:
    path = macro_path(name)
    if not path.exists():
        raise GuiError("找不到這個巨集。")
    try:
        path.unlink()
    except OSError as error:
        raise GuiError("刪除巨集失敗。") from error


# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------
# 正在跑的 `run_shell` 子行程。`!sh` 是同步阻塞的，沒有這份名單就沒辦法中止。
_SHELL_PROCS: set = set()

SHELL_DEFAULT_TIMEOUT_SEC = 60.0
SHELL_MAX_TIMEOUT_SEC = 900.0
SHELL_MAX_OUTPUT_CHARS = 200_000

# --------------------------------------------------------------------------
# PowerShell 的編碼：從源頭統一，不要在讀的那一端挑解碼器
# --------------------------------------------------------------------------
# 同一次 `/host sh run` 的輸出流裡**可以同時有兩種編碼**，所以「挑一個解碼器」這條
# 路本來就走不通（實測，不是推論）：
#
# * PowerShell **自己**的輸出（`Write-Output '佇列已清空，行程結束'`）走**主控台
#   代碼頁**，本機是 cp950——`pwsh` 7.6.6 與內建的 `powershell` 5.1 都一樣。照
#   `encoding="utf-8"` 讀，那 10 個中文字變成 **11 個 U+FFFD**。
# * **原生命令**的輸出（`git log --format=%s`）是 PowerShell **原樣穿透**的位元組，
#   git 吐 UTF-8，所以那一半用 utf-8 讀是對的、用 `"oem"` 讀會整個解不開。
#
# 所以改成叫 PowerShell 兩端都用 UTF-8。`errors="replace"` 保證這個缺陷永遠不會
# 以例外的形式出現，只會安靜地把中文吃掉——沒有紅字、沒有 traceback。
#
# ⚠️ **`InputEncoding` 那一句不是順手加的，輸入端本來就是壞的。**
# `job_start(interactive=True)` 的 stdin 我們用 UTF-8 編碼寫進去，PowerShell 那端
# 卻用主控台代碼頁解。實測餵 `測試輸入`（4 個字元、UTF-8 是 12 個位元組）進去，
# `Read-Host` 收到的是 **6 個字元**（U+769C U+7948 U+5CAB U+981B U+8A68 U+F16F）。
# **而回顯會把這個傷害藏起來**：那串 mojibake 再用同一個代碼頁編碼寫回 stdout，
# 位元組跟原本的 UTF-8 一模一樣，我們照 utf-8 解就「看起來完全正確」。要量的是
# `$x.Length`，不是回顯長什麼樣。
#
# ⚠️⚠️ **這兩個 setter 打的是「共用的主控台」，而且不會還原。** 它們底下呼叫
# `SetConsoleOutputCP` / `SetConsoleCP`，改的是**呼叫者**那個主控台的代碼頁。少了
# 下面那個 `_SHELL_CREATIONFLAGS`，跑一次 `/host sh run` 就把 bot 主控台的代碼頁從
# 950 改成 65001（實測：跑前 950、跑後 65001，而且**後續沒有加前綴的呼叫也跟著吐
# UTF-8**），之後每一個子行程都受影響——包括兩處刻意用 `encoding="oem"` 的
# `schtasks` 查詢（`_process_control` 與 `install_autostart`）。那種壞法的症狀會
# 出現在**別的功能**上、完全不會指回這裡。
#
# 前綴與旗標是**一組**：只加旗標不加前綴，U+FFFD 仍然是 11（做事的是前綴）；
# 只加前綴不加旗標，輸出對了但主控台被污染（收住副作用的是旗標）。
_PS_UTF8_PRELUDE = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "[Console]::InputEncoding=[Text.Encoding]::UTF8; "
)

# 子行程拿到**自己的**隱藏主控台，上面那兩個 setter 就改不到我們這一個。
# `CREATE_NO_WINDOW` 只存在於 Windows 的 `subprocess`，所以要包起來；傳 0 在其他
# 平台是合法的（CPython 只在 `creationflags != 0` 時才拒絕）。
# `DETACHED_PROCESS` 不能拿來代替：實測輸出會變成空字串。
_SHELL_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ANSI CSI / OSC 逸出序列。PowerShell 7 的表格輸出會夾雜顏色碼（`Get-ChildItem`
# 之類的 formatter 一定會），在對話平台上是一堆看不懂的 `[32;1m`。這些純粹是
# 終端機的呈現指令、不帶內容，所以在 `run_shell` 裡就剝掉——連寫進 log 的那份
# 也一起乾淨。（不改用 `$PSStyle.OutputRendering`：Windows 內建的 5.1 沒有那個
# 變數，`powershell` fallback 會直接報錯。）
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


# 指令執行的「目前工作目錄」。一次 `!sh` 就是一個獨立行程，`cd` 的效果不會留到
# 下一次，所以工作目錄得由本模組記著。存在**記憶體**裡而不是磁碟：這是對話的
# 情境狀態，不是跨行程契約，bot 重啟後回到專案根目錄才是預期行為。
_SHELL_CWD: Path = PROJECT_ROOT


def shell_cwd() -> Path:
    """`!sh` 目前的工作目錄。"""
    return _SHELL_CWD


def set_shell_cwd(raw: str | None) -> Path:
    """切換工作目錄；`None` / 空字串回到專案根目錄。回傳切換後的路徑。"""
    global _SHELL_CWD  # pylint: disable=global-statement
    text = unquote_path(raw)
    if not text:
        _SHELL_CWD = PROJECT_ROOT
        return _SHELL_CWD
    # 相對路徑以**目前**工作目錄為基準，這樣連續 `cd a`、`cd b` 才符合直覺
    # （`resolve_host_path` 是以專案根目錄為基準，那是給絕對定位用的）。
    expanded = Path(os.path.expandvars(os.path.expanduser(text)))
    candidate = expanded if expanded.is_absolute() else _SHELL_CWD / expanded
    candidate = Path(os.path.normpath(str(candidate)))
    if not candidate.is_dir():
        raise GuiError("找不到這個資料夾。")
    _SHELL_CWD = candidate
    return _SHELL_CWD


def shell_argv(command: str, *, interactive: bool = False) -> list[str]:
    """組出執行指令用的 argv。

    Windows 上優先用 PowerShell 7（`pwsh`）、退回內建的 `powershell`；其他平台
    走 `/bin/sh -c`。一律加上 `-NoProfile`：載入使用者 profile 會拖慢每次呼叫
    又可能改動環境。

    `-NonInteractive` 只在**非**互動模式加。它的用意是「沒有 tty 時不要停在提示
    那裡等到逾時」，但它同時會讓 `Read-Host` 直接報錯——如果呼叫端根本就打算餵
    輸入進去（`job_send`），那個旗標會讓整件事做不成。

    Windows 上使用者的指令前面會接上 `_PS_UTF8_PRELUDE`（見那裡的說明）：輸出與
    輸入兩端都改成 UTF-8，否則 PowerShell 自己的中文走主控台代碼頁、原生命令的
    輸出原樣穿透，同一次呼叫裡就有兩種編碼。前綴是**兩句**，兩句都要——只設輸出
    端的話 `job_send` 餵中文仍然壞，而那個壞法沒有任何錯誤訊息。前綴對結束碼是
    中性的（實測 `exit 3` 加不加前綴都是 rc=3）。
    """
    if os.name == "nt":
        import shutil
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        argv = [exe, "-NoProfile"]
        if not interactive:
            argv.append("-NonInteractive")
        return [*argv, "-Command", _PS_UTF8_PRELUDE + command]
    return ["/bin/sh", "-c", command]


def _kill_tree(proc: subprocess.Popen) -> None:
    """逾時後把整棵子行程樹清掉。

    只 kill 直接子行程是不夠的：`powershell -Command "some.exe"` 之下真正在跑
    的是孫行程，父死了孫子還在，逾時等於沒有生效。psutil 是必要相依，取不到
    時退回只殺直接子行程。
    """
    try:
        import psutil  # type: ignore
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except Exception:  # pylint: disable=broad-except  # nosec B110
                pass
        _gone, alive = psutil.wait_procs(children, timeout=3)
        for child in alive:
            try:
                child.kill()
            except Exception:  # pylint: disable=broad-except  # nosec B110
                pass
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass
    try:
        proc.kill()
    except Exception:  # pylint: disable=broad-except  # nosec B110
        pass


def run_shell(command: str, *, timeout: float = SHELL_DEFAULT_TIMEOUT_SEC,
              cwd: Path | None = None) -> dict[str, Any]:
    """執行一行指令，回 `{rc, output, elapsed, timed_out}`（**同步阻塞**）。

    `output` 是 stdout ＋ stderr 合併後的原始文字，**沒有做去識別化** —— 呼叫
    端負責在送進對話平台之前刷過（`_scrub_external_report`）。這裡不做是因為
    寫 log 的那一份需要原文。

    呼叫端必須自己確認權限：這個函式本身不認得誰是擁有者。
    """
    text = (command or "").strip()
    if not text:
        raise GuiError("請給要執行的指令。")
    limit = max(1.0, min(float(timeout), SHELL_MAX_TIMEOUT_SEC))
    cwd = cwd or shell_cwd()
    argv = shell_argv(text)
    started = time.monotonic()
    try:
        # nosec B603 — 由設計就是任意指令執行；閘門在呼叫端（限擁有者）。
        # shell=False：argv 直接交給直譯器，不再多經過一層命令列解析。
        proc = subprocess.Popen(  # nosec B603
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            # 子行程拿自己的隱藏主控台，`shell_argv` 那個前綴的
            # `SetConsoleOutputCP` 才改不到 bot 這一個。少了它，跑一次指令就把
            # bot 主控台的代碼頁從 950 改成 65001，兩處 `encoding="oem"` 的
            # `schtasks` 查詢跟著壞掉——而症狀出現在別的功能上。
            creationflags=_SHELL_CREATIONFLAGS,
        )
    except OSError as error:
        raise GuiError("無法啟動指令直譯器。") from error
    timed_out = False
    # 登記在案，`shell_stop_all()` 才有東西可以砍。沒有這個的話，一個打錯的
    # `!sh` 起跑之後只能等到逾時（最長 15 分鐘），中間完全沒有辦法。
    with _job_lock():
        _SHELL_PROCS.add(proc)
    try:
        output, _ = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=5)
        except Exception:  # pylint: disable=broad-except
            output = ""
    except Exception as error:  # pylint: disable=broad-except
        _kill_tree(proc)
        raise GuiError("執行指令時發生錯誤。") from error
    finally:
        with _job_lock():
            _SHELL_PROCS.discard(proc)
    output = strip_ansi(output or "")
    if len(output) > SHELL_MAX_OUTPUT_CHARS:
        output = output[:SHELL_MAX_OUTPUT_CHARS] + "\n…（輸出過長，已截斷）"
    return {
        "rc": proc.returncode,
        "output": output,
        "elapsed": time.monotonic() - started,
        "timed_out": timed_out,
    }


# --------------------------------------------------------------------------
# 動作錄製
# --------------------------------------------------------------------------
# 掛低階鍵鼠 hook、錄「放開」與滾輪、打時間戳、以及鍵碼→字元的鍵盤配置查詢，
# 全部在函式庫裡（`record` / `stop_record_timeline` / `char_table`）。這裡只留
# **把事件轉成本專案的巨集語言**——那是這個 bot 自己的 DSL，不屬於函式庫。
#
# 為什麼那三樣缺一不可（函式庫原本三樣都沒有，補上去才搬過來的）：沒有「放開」
# 則拖曳看起來只是一次點選、修飾鍵按住的狀態還原不出來；沒有滾輪則捲動完全錄不
# 到；沒有時間戳則重播時所有步驟一口氣跑完，真實介面來不及反應。
#
# 安全性：鍵盤 hook 會錄到**期間打的每一個字，包含密碼**。所以錄製限擁有者、
# 有硬性時間上限，而且 `!macro show` 送出前要過去識別化（錄下來的內容不是使用者
# 寫的，是主機上發生的事）。
RECORD_MAX_SEC = 300.0
RECORD_MIN_WAIT_SEC = 0.4       # 小於這個間隔就不插 `wait`，免得步驟被切碎
RECORD_DOUBLE_CLICK_SEC = 0.4
RECORD_DRAG_MIN_PX = 6

_RECORDING = False
_RECORD_STARTED = 0.0
_RECORD_LAYOUT: int | None = None

# 修飾鍵的虛擬鍵碼 → 巨集裡用的名字。這張表是**本專案巨集語言**的字彙，不是
# 系統知識，所以留在這裡。
_MODIFIER_VK = {
    16: "shift", 160: "shift", 161: "shift",
    17: "ctrl", 162: "ctrl", 163: "ctrl",
    18: "alt", 164: "alt", 165: "alt",
    91: "win", 92: "win",
}

# 函式庫的事件動詞 → 本模組轉步驟時用的短名。
_RECORD_OPS = {"key_down": "kdown", "key_up": "kup",
               "mouse_down": "mdown", "mouse_up": "mup", "scroll": "wheel"}


def record_start() -> None:
    """開始錄製。已經在錄就丟 `GuiError`。"""
    global _RECORDING, _RECORD_STARTED, _RECORD_LAYOUT  # pylint: disable=global-statement
    if os.name != "nt":
        raise GuiError("錄製功能僅 Windows 可用。")
    if _RECORDING:
        raise GuiError("已經在錄製中。")
    ac = load_ac()
    # 鍵盤配置在**開始錄的時候**就問：那是使用者接下來打字用的那一個。等到停止
    # 才問就晚了——那時候他人已經回到對話平台，前景視窗換了，配置也可能跟著換。
    _RECORD_LAYOUT = ac.foreground_keyboard_layout()
    try:
        ac.record()
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] record start failed: {error!r}", file=sys.stderr)
        raise GuiError("無法開始錄製（掛不上鍵鼠監聽）。") from error
    _RECORDING = True
    _RECORD_STARTED = time.monotonic()


def record_active() -> bool:
    return _RECORDING


def record_elapsed() -> float:
    return 0.0 if not _RECORDING else time.monotonic() - _RECORD_STARTED


def record_stop() -> list[dict[str, Any]]:
    """停止錄製，回本模組轉步驟用的事件清單。"""
    global _RECORDING  # pylint: disable=global-statement
    if not _RECORDING:
        raise GuiError("目前沒有在錄製。")
    _RECORDING = False
    try:
        events = load_ac().stop_record_timeline()
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] record stop failed: {error!r}", file=sys.stderr)
        raise GuiError("停止錄製失敗。") from error
    return _from_timeline(events)


def _from_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """函式庫的 `delta_ms` 事件 → 帶絕對時間 `t` 的事件。

    轉步驟時要判斷「兩個動作之間停了多久」與「連點算不算雙擊」，用累加出來的
    絕對時間比逐筆間隔好寫；函式庫回的是間隔，因為那才是重播需要的形式。
    """
    out: list[dict[str, Any]] = []
    clock = 0.0
    for event in events:
        clock += float(event.get("delta_ms", 0)) / 1000.0
        kind = _RECORD_OPS.get(str(event.get("op", "")))
        if kind is None:
            continue
        item = {key: value for key, value in event.items()
                if key not in ("op", "delta_ms")}
        item["kind"] = kind
        item["t"] = clock
        out.append(item)
    return out


# 表名 → 好讀的別名（`return` → `enter`）。錄出來的步驟是要給人看、給人改的，
# 寫成文件裡用的那組名字比寫 Win32 的原始名稱好懂；兩種都能通過驗證。
_ALIAS_REVERSE: dict[str, str] = {}
for _friendly, _canonical in KEY_ALIASES.items():
    # 先宣告的優先（`backspace` 比 `bksp` 好讀），不是最短的優先
    _ALIAS_REVERSE.setdefault(_canonical, _friendly)
del _friendly, _canonical


def _vk_to_name(vk: int) -> str | None:
    """虛擬鍵碼 → 巨集用的鍵名；表裡查不到就回 None。

    反查會撞名（滑鼠事件常數跟鍵盤共用同一張表，例如 vk 32 同時是 `space` 與
    `middledown`，vk 84 同時是 `t` 與 `T`），所以要有明確的偏好順序，否則同一次
    錄製在不同機器上會產生不一樣的字面。

    候選名字同時來自底層表與 `_EXTRA_KEY_CODES`（`parse_key_name` 認得的就是這兩
    份的聯集），同一個偏好順序一起比——所以 vk 183 的 `LAUNCH_APP2`（大寫，
    `parse_key_name` 寫不出來）與補充表的 `launch_app2` 同時在場時，一定挑小寫
    那個。補充表是本專案自己的，底層載不進來也照樣查得到。

    `_LIBRARY_NAME_OVERRIDES` 有兩條規則，兩條都跟「底層表那一筆是錯的」有關：

    * 被覆寫的名字**不拿來反查它在底層表裡的那個錯鍵碼**——vk 0x80（F17）不能錄成
      `down`，否則重播時 `down` 會被換成 0x28，錄到的 F17 變成方向鍵；
    * 覆寫的名字**明確勝出**（不靠長短碰巧），所以 vk 0x28 錄成 `down` 而不是底層
      那個 `vk_down`。覆寫表收的本來就是一般人會打的名字。
    """
    if vk in _MODIFIER_VK:
        return _MODIFIER_VK[vk]
    try:
        table = getattr(load_ac(), "keyboard_keys_table", None)
    except GuiError:
        table = None
    if not isinstance(table, dict):
        table = {}
    names = [name for name, code in table.items()
             if code == vk and name not in _LIBRARY_NAME_OVERRIDES]
    names += [name for name, code in _EXTRA_KEY_CODES.items() if code == vk]
    overriding = [name for name, code in _LIBRARY_NAME_OVERRIDES.items()
                  if code == vk]
    if overriding:
        names = overriding
    if not names:
        return None
    # 偏好順序：小寫優先 → 短的優先 → 字典序（保證跨機器一致）
    best = min(names, key=lambda n: (n != n.lower(), len(n), n))
    return _ALIAS_REVERSE.get(best, best)


def _record_char_table() -> dict[int, tuple[str, str]]:
    """錄製當下那個鍵盤配置的「按鍵→字元」對照表。

    標點符號的虛擬鍵碼在不同配置上印出不同的字，寫死一份 US 表會讓非 US 配置錄
    到的每個標點都是錯的。查詢與退路都在函式庫裡。
    """
    try:
        return load_ac().char_table(_RECORD_LAYOUT)
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] char table unavailable: {error!r}", file=sys.stderr)
        return {}


class RecordedMacro(NamedTuple):
    """錄製轉成步驟的結果，連同**沒存進去的部分有多少**。

    * `steps` —— 可以直接存檔的步驟（`record_to_steps` 回的就是這個）；
    * `truncated` —— 因為超過 `MACRO_MAX_STEPS` 而沒存到的**動作**數（不含 `wait`：
      截斷處之後的等待本來就會跟著最後一個動作一起失去意義，算進去只會讓數字看起來
      比實際損失大）；
    * `unrecordable` —— 轉出來卻重播不了、被略過的步驟數（叫不出名字的鍵、驗證不過
      的步驟）。這個數字**與步數上限無關**：截斷處之後的步驟照樣逐一驗過再分類，
      所以同一段錄製不管上限是多少，略過的數目都一樣。

    兩個數字存在的理由是回報：錄製最長可以跑 `RECORD_MAX_SEC`，一百次點選（停頓
    之後的每一次都是 `wait` ＋ `click` 兩步）就已經碰到上限，而原本回覆只寫「已存
    成巨集（200 步）」，後面被丟掉的部分使用者完全不會知道。
    """

    steps: list[str]
    truncated: int
    unrecordable: int


def record_to_steps(events: list[dict[str, Any]], *,
                    min_wait: float = RECORD_MIN_WAIT_SEC,
                    char_table: dict[int, tuple[str, str]] | None = None
                    ) -> list[str]:
    """把錄到的事件轉成巨集步驟（只要步驟；要知道丟掉多少用 `convert_recording`）。"""
    return convert_recording(events, min_wait=min_wait,
                             char_table=char_table).steps


def convert_recording(events: list[dict[str, Any]], *,
                      min_wait: float = RECORD_MIN_WAIT_SEC,
                      char_table: dict[int, tuple[str, str]] | None = None
                      ) -> RecordedMacro:
    """把錄到的事件轉成巨集步驟，回 `RecordedMacro`。

    轉換規則刻意保守——寧可多出一個看得懂的步驟，也不要猜錯：

    * 按下與放開位置差超過 `RECORD_DRAG_MIN_PX` → `drag`，否則 `click`；
    * 同一點、**同一顆滑鼠鍵**在 `RECORD_DOUBLE_CLICK_SEC` 內連點兩次 → 併成
      `dclick`（右鍵接著左鍵是兩次不同的點選，不是一次左鍵雙擊）；
    * 連續滾輪事件併成一個 `scroll`；加總為 0 的一組（往下三格又往上三格）**不出
      任何步驟**，連它自己的 `wait` 也不出——那段時間併進下一個動作前面的等待；
    * 可印出字元的按鍵（沒有 ctrl / alt / win 按著）併成一個 `type`，
      其餘按鍵出成 `hotkey`（帶上當下按著的修飾鍵）；
    * 事件之間間隔超過 `min_wait` 就補一個 `wait` —— **這是重播能不能成功的關鍵**，
      沒有它所有步驟會一口氣送出去，畫面根本來不及跟上；
    * 最多存 `MACRO_MAX_STEPS` 步，後面的動作不存、但**算進 `truncated`**；重播不了
      的步驟略過、算進 `unrecordable`（兩者都要回報給使用者，見 `RecordedMacro`）。

    鍵碼換成字元時用的是**錄製當下那個鍵盤配置**（`record_start` 就記下來了），
    問不到才退回 US 對照表——標點符號的虛擬鍵碼在不同配置上印出不同的字，寫死一
    份表會讓非 US 配置錄到的每個標點都是錯的。查表本身在函式庫裡。

    `type` 的文字經過 `escape_macro_text`（`$` → `$$`），所以錄到的 `$100` 重播
    出來還是 `$100`，不會被當成參數。這是唯一一種帶著錄到的文字的步驟：`hotkey`
    的鍵名只能是英數字與底線。

    **已知、刻意不修的失真：空白。** `type` 的參數是先按空白拆開、重播時再用單一
    空白接回去，而這裡併字時又 `.strip()` 過，所以連續空白會縮成一個、頭尾的空白
    會消失。不修是因為要修就得改 `type` 的空白語意，而那會安靜地改變每一個既有的
    手寫巨集。
    """
    printable = char_table if char_table is not None else _record_char_table()
    steps: list[str] = []
    pending_text: list[str] = []
    held: set[str] = set()
    down: dict[str, dict[str, Any]] = {}
    wheel: dict[str, Any] | None = None
    last_t: float | None = None
    # 上一次點選：`(x, y, 放開時間, 哪一顆鍵)`。鍵要一起比，否則右鍵接著左鍵會被
    # 併成一次左鍵雙擊（右鍵那一下就這樣不見了）。
    last_click: tuple[int, int, float, str] | None = None

    def _flush_text() -> None:
        if pending_text:
            text = "".join(pending_text).strip()
            pending_text.clear()
            if text:
                # 錄到的是主機上真的打進去的字，裡面的 `$` 要跳脫成 `$$`，否則
                # 重播時 `$1` 會被當成參數（`$100` → `00`）。
                steps.append(f"type {escape_macro_text(text)}")

    def _flush_wheel() -> None:
        nonlocal wheel
        if wheel is not None:
            # 函式庫回的已經是「格數」（原始值除以一格 120），不用再換算。
            #
            # 加總為 0 的一組（往下又往上捲回原處）**什麼都不出**。這裡原本寫
            # `int(...) or 1`，於是一組互相抵銷的滾動被錄成往上一格——一個從來沒
            # 發生過的動作。`scroll 0` 本來就驗得過，那個 `or 1` 不是為了驗證。
            #
            # 這一組的 `wait` 也是在這裡才補（`_gap` 從開頭延後到這裡），所以丟掉的
            # 那一組不會留下它自己的等待；`last_t` 也沒動，那段時間自然併進下一個
            # 動作前面的 `wait`，重播的總時間不會少。
            notches = int(wheel["delta"])
            if notches:
                _gap(wheel["start"])
                steps.append(f"scroll {notches}")
            wheel = None

    def _gap(now: float) -> None:
        nonlocal last_t
        if last_t is not None and now - last_t >= min_wait:
            steps.append(f"wait {min(round(now - last_t, 1), MACRO_MAX_WAIT_SEC):g}")
        last_t = now

    for event in events:
        kind = event.get("kind")
        now = float(event.get("t", 0.0))

        if kind == "wheel":
            # 連續滾動併成一步；中斷了才吐出來
            if wheel is not None and now - wheel["t"] < 0.3:
                wheel["delta"] += event.get("delta", 0)
                wheel["t"] = now
                continue
            _flush_wheel()
            _flush_text()
            wheel = {"delta": event.get("delta", 0), "t": now, "start": now}
            continue
        _flush_wheel()

        if kind == "mdown":
            down[event.get("button", "left")] = event
            continue

        if kind == "mup":
            button = event.get("button", "left")
            start = down.pop(button, None)
            if start is None:
                continue
            _flush_text()
            _gap(float(start.get("t", now)))
            x1, y1 = int(start.get("x", 0)), int(start.get("y", 0))
            x2, y2 = int(event.get("x", x1)), int(event.get("y", y1))
            suffix = "" if button == "left" else f" {button}"
            if abs(x2 - x1) >= RECORD_DRAG_MIN_PX or abs(y2 - y1) >= RECORD_DRAG_MIN_PX:
                steps.append(f"drag {x1} {y1} {x2} {y2}{suffix}")
                last_click = None
            else:
                if (last_click is not None and steps
                        and last_click[3] == button
                        and abs(last_click[0] - x2) < RECORD_DRAG_MIN_PX
                        and abs(last_click[1] - y2) < RECORD_DRAG_MIN_PX
                        and now - last_click[2] <= RECORD_DOUBLE_CLICK_SEC
                        and steps[-1].startswith("click ")):
                    steps[-1] = f"dclick {x2} {y2}{suffix}"
                    last_click = None
                else:
                    steps.append(f"click {x2} {y2}{suffix}")
                    last_click = (x2, y2, now, button)
            last_t = now
            continue

        if kind == "kdown":
            vk = int(event.get("vk", 0))
            name = _vk_to_name(vk)
            if vk in _MODIFIER_VK:
                held.add(_MODIFIER_VK[vk])
                continue
            hard = held - {"shift"}
            character = printable.get(vk)
            if not hard and character:
                if not pending_text:
                    _gap(now)
                pending_text.append(character[1] if "shift" in held else character[0])
                last_t = now
                continue
            _flush_text()
            _gap(now)
            combo = "+".join([*sorted(held), name or str(vk)])
            steps.append(f"hotkey {combo}" if name else f"# 未知按鍵 vk={vk}")
            last_t = now
            continue

        if kind == "kup":
            vk = int(event.get("vk", 0))
            if vk in _MODIFIER_VK:
                held.discard(_MODIFIER_VK[vk])

    _flush_wheel()
    _flush_text()
    # 存進去之前先驗一遍：錄下來的東西是主機上發生的事，不是使用者寫的，
    # 混進一步不合法的會讓整個巨集在 `load_macro` 時被拒收（而不是跳過那一步）。
    #
    # 超過步數上限的部分**不是直接 break**：後面的步驟照樣逐一驗過，重播得了的動作
    # 算進 `truncated`、重播不了的算進 `unrecordable`。原本是碰到上限就 break，於是
    # 錄了三百秒、存進去的只有前兩百步，回覆卻只說「已存成巨集（200 步）」。
    valid: list[str] = []
    truncated = 0
    unrecordable = 0
    for step in steps:
        if step.startswith("#"):
            # 叫不出名字的鍵（見 `_EXTRA_KEY_CODES`）。原本這裡是**完全安靜**的，
            # 錄製回報的步數就這樣少一步，連 log 都查不到；內容只有一個鍵碼數字。
            print(f"[gui] dropped unrecordable step {step!r}", file=sys.stderr)
            unrecordable += 1
            continue
        # 開頭的 `wait` 一定是多餘的：還沒做任何事，沒有東西好等。錄製一開始的
        # 那段「把手移到滑鼠上」的空檔會產生它，留著只是讓重播平白慢一拍。
        if not valid and step.startswith("wait "):
            continue
        try:
            validate_macro_step(step)
        except GuiError:
            print(f"[gui] dropped unrecordable step {step!r}", file=sys.stderr)
            unrecordable += 1
            continue
        if len(valid) >= MACRO_MAX_STEPS:
            if not step.startswith("wait "):
                truncated += 1
            continue
        valid.append(step)
    while valid and valid[-1].startswith("wait "):
        valid.pop()      # 結尾的 `wait` 同理：後面沒有步驟了
    return RecordedMacro(valid, truncated, unrecordable)


# --------------------------------------------------------------------------
# 背景作業
# --------------------------------------------------------------------------
# `run_shell` 會**擋到指令跑完**，上限 900 秒。安裝、建置、下載這類動輒二十分鐘
# 的事情因此沒辦法用對話做——不是慢，是根本跑不完。
#
# 這裡起一個不等它的行程，輸出由一條讀取執行緒收進記憶體，之後用 `job_log` 取。
# 輸出**留在記憶體、不落地**：這是對話的暫時狀態，不是跨行程契約，落地反而要多
# 一套清理與檔名政策。代價是 bot 重啟就沒了（行程本身也會跟著父行程收掉），這在
# 說明裡講清楚。
JOB_MAX_KEPT = 20
JOB_LOG_MAX_LINES = 4000
JOB_LOG_LINE_MAX_CHARS = 2000

_JOBS: dict[int, dict[str, Any]] = {}
_JOB_NEXT_ID = 1
_JOB_LOCK: Any = None


def _job_lock():
    """讀取執行緒與呼叫端會同時碰 `_JOBS`，所以要鎖。延遲建立以免 import 就付出成本。"""
    global _JOB_LOCK  # pylint: disable=global-statement
    if _JOB_LOCK is None:
        import threading
        _JOB_LOCK = threading.Lock()
    return _JOB_LOCK


def _close_job_streams(job: dict[str, Any]) -> None:
    """把作業的管道明確關掉。永不 raise。

    `Popen` 的 stdout/stdin 是 `TextIOWrapper`，不關的話要等最後一個參照消失才由
    `__del__` 收——實測（20 個作業，Windows handle 計數）目前確實會在 `_JOBS` 那一筆
    被丟掉的當下歸零，所以**不是** handle 洩漏；但那是靠 CPython 的參照計數，任何一個
    參照環（例外的 traceback 抓住 frame 就夠了）就會把釋放推遲到 GC。既然行程要跑好幾
    天，明確關掉便宜又不用賭。

    附帶效果是 `-W always::ResourceWarning` 掃全套測試會變乾淨，那條掃描才用得下去
    ——會叫的守門才有人聽。
    """
    proc = job.get("proc") if isinstance(job, dict) else None
    for name in ("stdout", "stderr", "stdin"):
        # `getattr` 也在 try 裡面：它的 default 只吃 `AttributeError`，屬性本身
        # 求值爆炸（任何非 Popen 的替身都可能）會直接穿出去，而這支函式跑在
        # `finally` 的收尾路徑上，穿出去會蓋掉真正的錯誤。
        try:
            stream = getattr(proc, name, None)
            if stream is not None:
                stream.close()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass


def _job_reader(job: dict[str, Any]) -> None:
    """把子行程輸出一行一行收進 job 的環狀緩衝區，直到它結束。"""
    proc = job["proc"]
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            clean = strip_ansi(line.rstrip("\n"))[:JOB_LOG_LINE_MAX_CHARS]
            with _job_lock():
                job["lines"].append(clean)
                job["total_lines"] += 1
                # 只留最近的一段：一個話多的建置可以吐出幾十萬行，全留會把
                # bot 的記憶體吃光。丟掉的行數另外記著，才不會讓人以為看到全部。
                overflow = len(job["lines"]) - JOB_LOG_MAX_LINES
                if overflow > 0:
                    del job["lines"][:overflow]
                    job["dropped"] += overflow
    except Exception as error:  # pylint: disable=broad-except
        print(f"[gui] job {job['id']} reader failed: {error!r}", file=sys.stderr)
    finally:
        try:
            proc.wait()
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass
        with _job_lock():
            job["rc"] = proc.returncode
            job["finished"] = time.time()
        # 行程已經結束（`wait` 回來了），管道沒有人會再用——明確關掉，不要留給
        # 參照計數的時機。`job_send` 會先擋掉已結束的作業，`job_close_input` 對
        # 已關閉的串流是 no-op，所以兩個使用者面的入口都不受影響。
        _close_job_streams(job)


def job_start(command: str, *, cwd: Path | None = None,
              interactive: bool = False) -> int:
    """起一個背景作業，回作業編號。**不等它跑完。**

    `interactive=True` 才把 stdin 接成管道（之後用 `job_send` 餵輸入）。**預設
    是 `DEVNULL`**，因為那會讓讀 stdin 的程式立刻拿到 EOF 然後照常跑完；接成
    管道卻沒人寫的話，同一支程式會停在那裡等到逾時。要能回答互動式提示的人自己
    指定，其餘情況維持「不會莫名卡住」的預設。
    """
    text = (command or "").strip()
    if not text:
        raise GuiError("請給要執行的指令。")
    global _JOB_NEXT_ID  # pylint: disable=global-statement
    _job_prune()
    argv = shell_argv(text, interactive=interactive)
    try:
        # nosec B603 — 由設計就是任意指令執行；閘門在呼叫端（限擁有者）。
        proc = subprocess.Popen(  # nosec B603
            argv,
            cwd=str(cwd or shell_cwd()),
            stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            # 同 `run_shell`：圍堵 `shell_argv` 前綴的主控台副作用。這一站尤其
            # 不能漏——互動作業是唯一會**寫** stdin 的路徑，前綴的
            # `InputEncoding` 那一句就是為它加的。
            creationflags=_SHELL_CREATIONFLAGS,
        )
    except OSError as error:
        raise GuiError("無法啟動指令直譯器。") from error
    with _job_lock():
        job_id = _JOB_NEXT_ID
        _JOB_NEXT_ID += 1
        job = {
            "id": job_id,
            "command": text,
            "proc": proc,
            "started": time.time(),
            "finished": None,
            "rc": None,
            "lines": [],
            "total_lines": 0,
            "dropped": 0,
            "stopped": False,
            "interactive": bool(interactive),
        }
        _JOBS[job_id] = job
    import threading
    threading.Thread(target=_job_reader, args=(job,), daemon=True).start()
    return job_id


def _job_prune() -> None:
    """作業紀錄留最近 `JOB_MAX_KEPT` 筆，只丟已結束的。"""
    with _job_lock():
        done = sorted((j for j in _JOBS.values() if j["finished"] is not None),
                      key=lambda j: j["finished"])
        while len(_JOBS) > JOB_MAX_KEPT and done:
            _JOBS.pop(done.pop(0)["id"], None)


def job_list() -> list[dict[str, Any]]:
    """所有作業的狀態摘要（不含輸出內容），新的在前。"""
    with _job_lock():
        rows = list(_JOBS.values())
    now = time.time()
    out = []
    for job in sorted(rows, key=lambda j: -j["started"]):
        out.append({
            "id": job["id"],
            "command": job["command"],
            "running": job["finished"] is None,
            "rc": job["rc"],
            "stopped": job["stopped"],
            "elapsed": (job["finished"] or now) - job["started"],
            "lines": job["total_lines"],
        })
    return out


def _job_get(job_id: int) -> dict[str, Any]:
    with _job_lock():
        job = _JOBS.get(int(job_id))
    if job is None:
        raise GuiError("找不到這個作業編號。")
    return job


def job_log(job_id: int, lines: int = 40) -> dict[str, Any]:
    """某個作業最近 `lines` 行輸出。輸出**沒有去識別化**，呼叫端負責。"""
    job = _job_get(job_id)
    count = max(1, min(int(lines), JOB_LOG_MAX_LINES))
    with _job_lock():
        tail = job["lines"][-count:]
        return {
            "id": job["id"],
            "running": job["finished"] is None,
            "rc": job["rc"],
            "stopped": job["stopped"],
            "elapsed": (job["finished"] or time.time()) - job["started"],
            "text": "\n".join(tail),
            "shown": len(tail),
            "total": job["total_lines"],
            "dropped": job["dropped"],
        }


def shell_stop_all() -> int:
    """砍掉所有正在跑的 `run_shell` 子行程樹，回砍了幾個。

    刻意是「全部」而不是挑一個：`!sh` 是同步阻塞的，實務上不會有一堆同時在跑，
    而要使用者先查出編號才砍得掉，在「指令跑不完、我想停下來」的當下沒有意義。
    """
    with _job_lock():
        procs = list(_SHELL_PROCS)
    for proc in procs:
        _kill_tree(proc)
    return len(procs)


def job_send(job_id: int, text: str, *, newline: bool = True) -> None:
    """把一行輸入餵給互動式作業（回答安裝程式的提示那種）。"""
    job = _job_get(job_id)
    if not job.get("interactive"):
        raise GuiError("這個作業沒有開互動輸入；請用 `--stdin` 重新啟動它。")
    if job["finished"] is not None:
        raise GuiError("這個作業已經結束了。")
    stream = job["proc"].stdin
    if stream is None:
        raise GuiError("這個作業沒有可寫入的輸入。")
    try:
        stream.write(text + ("\n" if newline else ""))
        stream.flush()
    except (OSError, ValueError) as error:
        raise GuiError("寫入作業輸入失敗（它可能已經不再讀取了）。") from error


def job_close_input(job_id: int) -> None:
    """關掉作業的輸入端，讓對方讀到 EOF。"""
    job = _job_get(job_id)
    stream = job["proc"].stdin
    if stream is None:
        raise GuiError("這個作業沒有可寫入的輸入。")
    try:
        stream.close()
    except (OSError, ValueError) as error:
        raise GuiError("關閉作業輸入失敗。") from error


def job_stop(job_id: int) -> bool:
    """砍掉作業的整棵行程樹。已經結束的回 False。"""
    job = _job_get(job_id)
    if job["finished"] is not None:
        return False
    job["stopped"] = True
    _kill_tree(job["proc"])
    return True


def job_clear() -> int:
    """清掉所有已結束的作業紀錄，回清掉幾筆。"""
    with _job_lock():
        done = [k for k, j in _JOBS.items() if j["finished"] is not None]
        for key in done:
            job = _JOBS.pop(key, None)
            if job is not None:
                # 保險：reader 執行緒正常結束時已經關過了，但如果它根本沒起來
                # （`Thread.start` 失敗）就沒人關過。關第二次是 no-op。
                _close_job_streams(job)
    return len(done)


def job_stop_all() -> int:
    """砍掉所有還在跑的作業。bot 關閉時用——否則孤兒行程會留在主機上。"""
    stopped = 0
    for row in job_list():
        if row["running"]:
            try:
                if job_stop(row["id"]):
                    stopped += 1
            except GuiError:  # pragma: no cover
                continue
    return stopped


# --------------------------------------------------------------------------
# 巨集重播（同步版，測試與非 async 呼叫端用）
# --------------------------------------------------------------------------
def substitute_macro_args(step: str, args: list[str]) -> str:
    """把步驟裡的參數記號換成呼叫時給的值。

    參數化讓一個巨集能重複用在不同目標上（`/macro run open_url <網址>`），
    不必為了換一個字複製一份。語法：

    * `$1`..`$9` → 第 N 個參數；沒給的換成空字串。只看**一位數**，所以 `$10` 是
      「第 1 個參數後面接一個 `0`」。
    * `$$` → 一個字面的 `$`。沒有這條的話，巨集裡根本寫不出「`$` 後面接數字」
      （`type $100` 會打出 `00`），錄製下來的輸入也會被安靜地改掉。
    * 其餘的 `$`（結尾的 `$`、`$a`、`$0`）照原樣留著——跟加入 `$$` 之前一樣。

    **只掃一遍**：換進去的參數值不會再被掃描，所以參數裡的 `$1` / `$$` 原樣送出，
    不會變成另一個參數或被折成一個 `$`。由左往右配對，`$$$1` 是「`$` ＋ 第 1 個
    參數」。反方向是 `escape_macro_text`。
    """
    def _replace(match: re.Match) -> str:
        token = match.group(1)
        if token == "$":
            return "$"
        index = int(token) - 1
        return args[index] if 0 <= index < len(args) else ""

    return MACRO_ARG_RE.sub(_replace, step)


def escape_macro_text(text: str) -> str:
    """把一段要原樣送出的文字寫成巨集步驟：`$` → `$$`。

    `substitute_macro_args(escape_macro_text(t), args) == t` 對任何 `t` 都成立——
    每一個 `$` 都成了一對，由左往右配對時不會有落單的 `$` 去吃後面的數字。錄製
    （`record_to_steps`）產生 `type` 步驟時走這一支。
    """
    return text.replace("$", "$$")


def check_macro_program(steps: list[str], args: list[str] | None = None, *,
                        name: str | None = None, depth: int = 0) -> None:
    """重播**之前**把整個程式驗一遍：代入參數後的每一步、區塊平衡、每一個 `call`。

    `run_macro_program` 是一邊跑一邊代入 `$N`、一邊驗證的，所以
    `['click 10 10', 'type $1']` 沒給參數時，點選已經真的點下去了，才在第二行發現
    `type` 沒有參數；`call` 到不存在的巨集、遞迴超過 `MACRO_MAX_CALL_DEPTH` 也一樣
    ——外層那幾步已經在真實桌面上做完，收不回來（`macro_block_map` 講的是同一個
    原則）。這裡在任何動作之前先把整棵呼叫樹走完。

    * 每一步都驗，**不管執行時走不走得到**：走到才會壞的步驟就是壞的。
    * 每一個 `call` 都跟進去：用代入後的參數讀取並檢查被呼叫的巨集，深度上限與
      執行端丟的是同一句話。所以條件式的自我呼叫（`if_text 錯誤` / `call 自己`）
      也會被擋：它一路展開一定超過深度上限。
    * 以 `(巨集名, 參數)` 記憶化，同一個被呼叫者只驗一次；總量另有
      `MACRO_MAX_CHECKED` 當上界（記憶化本身不是上界，見該常數）。
    * 失敗丟 `GuiError`，訊息講出是哪個巨集的第幾行，而且整句都是本模組寫的泛用
      句（巨集名稱已經過 `MACRO_NAME_RE`，參數值不會出現在訊息裡）。

    `name` 只用來讓訊息指得出是哪一個巨集；不合 `MACRO_NAME_RE` 的就不印。
    建立排程／監看時也呼叫這一支，所以壞參數在**建立的時候**就被拒絕。
    """
    label = name if name and MACRO_NAME_RE.match(name) else None
    _check_macro_level(list(steps), list(args or []), label, depth,
                       memo={}, loaded={}, counter=[0])


def _check_macro_level(steps: list[str], args: list[str], name: str | None,
                       depth: int, *,
                       memo: dict[tuple[str, tuple[str, ...]], int],
                       loaded: dict[str, list[str]],
                       counter: list[int]) -> int:
    """`check_macro_program` 的一層；回這一層底下還有幾層 `call`（高度）。

    記憶化存的是**高度**而不是「驗過了」：同一個 `(巨集, 參數)` 在淺的地方驗得
    過，在深的地方不一定——深度 1 的呼叫樹高 2 沒事，同一棵樹掛在深度 2 就超過
    上限。高度跟從哪裡被呼叫無關，所以命中時只要比一次 `depth + 1 + 高度`。
    只有**成功**的子樹會進記憶，所以循環呼叫一定會一路展開到深度上限然後失敗。
    """
    if depth > MACRO_MAX_CALL_DEPTH:
        raise GuiError(f"巨集呼叫層數超過上限（{MACRO_MAX_CALL_DEPTH} 層）。")
    where = f"巨集 `{name}` " if name else ""
    try:
        macro_block_map(steps)
    except GuiError as error:
        if not name:
            raise
        raise GuiError(f"巨集 `{name}`：{error}") from error
    height = 0
    for index, step in enumerate(steps, start=1):
        counter[0] += 1
        if counter[0] > MACRO_MAX_CHECKED:
            raise GuiError(
                f"巨集展開之後要檢查的步驟超過上限（{MACRO_MAX_CHECKED} 步）；"
                "減少 `call` 的層數或分支。")
        raw = substitute_macro_args(step, args)
        try:
            verb, step_args = validate_macro_step(raw)
        except GuiError as error:
            raise GuiError(f"{where}第 {index} 行：{error}") from error
        if verb != "call":
            continue
        callee = step_args[0]          # 已經過 `macro_path` 的名稱檢查
        key = (callee, tuple(step_args[1:]))
        below = memo.get(key)
        if below is not None:
            if depth + 1 + below > MACRO_MAX_CALL_DEPTH:
                raise GuiError(
                    f"{where}第 {index} 行：巨集呼叫層數超過上限"
                    f"（{MACRO_MAX_CALL_DEPTH} 層）。")
        else:
            callee_steps = loaded.get(callee)
            if callee_steps is None:
                try:
                    callee_steps = load_macro(callee)["steps"]
                except GuiError as error:
                    raise GuiError(
                        f"{where}第 {index} 行：巨集 `{callee}`：{error}") from error
                loaded[callee] = callee_steps
            try:
                below = _check_macro_level(
                    callee_steps, list(step_args[1:]), callee, depth + 1,
                    memo=memo, loaded=loaded, counter=counter)
            except GuiError as error:
                raise GuiError(f"{where}第 {index} 行：{error}") from error
            memo[key] = below
        height = max(height, below + 1)
    return height


def run_macro_program(steps: list[str], *, args: list[str] | None = None,
                      on_step: Callable[[int, str, str], None] | None = None,
                      should_abort: Callable[[], bool] | None = None,
                      depth: int = 0,
                      budget: list[int] | None = None) -> list[str]:
    """執行一個巨集程式（含 `repeat` / `if_…` / `call`），回每一步的敘述。

    直譯器而不是逐行迴圈：有了區塊就需要程式計數器與控制堆疊。設計上的三個
    重點——

    * **執行步數有總量上限**（`MACRO_MAX_EXECUTED`）。巢狀 `repeat` 只要四行就能
      要求跑一百萬步，來源行數上限完全擋不住；沒有這個上限，一個手滑的數字會讓
      桌面被佔住幾十分鐘而且中止不掉。
    * **`call` 有深度上限**，兩個巨集互相呼叫是很自然就會寫出來的無窮遞迴。
    * **`should_abort()` 每一步都檢查**，`!macro stop` 才停得下來。
    * **第一個動作之前先把整個程式驗完**（`check_macro_program`，只在最外層做
      一次）：代入參數之後才壞掉的步驟、`call` 到不存在的巨集、遞迴超過深度上限，
      都要在滑鼠動之前講，不是做到一半才停。迴圈裡逐步的代入與驗證**照樣保留**
      ——長巨集跑的途中，被呼叫的那個巨集檔還是可能被改掉。

    結束時（含失敗 / 中止）放開**這個巨集自己按住**的鍵；使用者跑之前就按著的
    不動，那是他刻意留的狀態。
    """
    if depth > MACRO_MAX_CALL_DEPTH:
        raise GuiError(f"巨集呼叫層數超過上限（{MACRO_MAX_CALL_DEPTH} 層）。")
    if depth == 0:
        check_macro_program(steps, args)
    blocks = macro_block_map(steps)
    counters = budget if budget is not None else [0]
    snapshot = held_snapshot() if depth == 0 else None
    done: list[str] = []
    # 控制堆疊：`("loop", 開頭, end, 剩餘次數)` 或 `("if", end)`
    stack: list[tuple] = []
    pointer = 0
    try:
        while pointer < len(steps):
            if should_abort is not None and should_abort():
                break
            counters[0] += 1
            if counters[0] > MACRO_MAX_EXECUTED:
                raise GuiError(
                    f"執行步數超過上限（{MACRO_MAX_EXECUTED} 步），已中止；"
                    "檢查一下 `repeat` 的次數。")
            raw = substitute_macro_args(steps[pointer], args or [])
            verb, step_args = validate_macro_step(raw)

            if verb == "stop":
                break
            if verb == "repeat":
                count = int(step_args[0])
                _else, end = blocks[pointer]
                if count <= 0:
                    pointer = end + 1
                    continue
                stack.append(("loop", pointer, end, count - 1))
                pointer += 1
                continue
            if verb in MACRO_CONDITIONS:
                else_at, end = blocks[pointer]
                stack.append(("if", end))
                if eval_macro_condition(verb, step_args):
                    pointer += 1
                else:
                    pointer = (else_at + 1) if else_at is not None else end
                continue
            if verb == "else":
                # 走到這裡代表 if 的真分支跑完了，跳過 else 分支
                frame = stack[-1] if stack else None
                pointer = (frame[1] if frame and frame[0] == "if"
                           else pointer + 1)
                continue
            if verb == "end":
                frame = stack.pop() if stack else None
                if frame and frame[0] == "loop" and frame[3] > 0:
                    stack.append(("loop", frame[1], frame[2], frame[3] - 1))
                    pointer = frame[1] + 1
                    continue
                pointer += 1
                continue
            if verb == "call":
                data = load_macro(step_args[0])
                nested = run_macro_program(
                    data["steps"], args=step_args[1:], on_step=on_step,
                    should_abort=should_abort, depth=depth + 1, budget=counters)
                done.extend(nested)
                detail = f"呼叫巨集 `{step_args[0]}`（{len(nested)} 步）"
            else:
                try:
                    detail = run_macro_step(raw, should_abort=should_abort)
                except GuiAborted:
                    # 等待步驟被中止不是失敗，跟「在步驟之間被停下來」同一件事。
                    break

            done.append(detail)
            if on_step is not None:
                on_step(len(done), raw, detail)
            pointer += 1
    finally:
        if snapshot is not None:
            release_added_since(snapshot)
    return done


def run_macro(steps: list[str], *,
              on_step: Callable[[int, str, str], None] | None = None,
              should_abort: Callable[[], bool] | None = None) -> list[str]:
    """`run_macro_program` 的無參數版（測試與非 async 呼叫端用）。"""
    return run_macro_program(steps, on_step=on_step, should_abort=should_abort)
