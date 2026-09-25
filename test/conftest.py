"""全套測試共用的守門夾具。

前三條是守門，擋的都是「用眼睛看不出來」的失敗（還原 stdlib `time`、不得終止正式
瀏覽器、不得寫進 repo 的正式狀態）；第四條是 `ast.parse` 的剖析快取（見檔尾）。

第一條擋的是：**測試把 stdlib 的 `time.sleep` 換掉之後沒有換回來。**

`_webrunner_shared.py` 寫的是 `import time` 再呼叫 `time.sleep(...)`，所以
`ws.time` **就是** stdlib 的 `time` 模組本身（`ws.time is time` 為 True，可自行
驗證）。於是測試裡常見的

    ws.time.sleep = lambda _seconds: None

並不是「只影響 `_webrunner_shared`」——它把整個行程的 `time.sleep` 換成了 no-op，
包含其他測試模組、以及當下任何背景執行緒。只要有一個路徑漏掉還原（`try` 之外就
先指派、`finally` 少一項、斷言在還原之前就炸掉），後面所有測試就都在一個
`time.sleep` 不會真的睡的世界裡跑，而且**沒有任何症狀**——只有偶爾莫名其妙的
時序測試失敗，而且順序一換就不重現。

所以這裡在每一支測試之後比對身分。它不修理任何東西，只是讓漏掉還原當場現形。
"""
import time

import pytest

# 換掉之後沒還原會影響全行程的 stdlib 函式。要再加就照同樣的理由加。
_PRISTINE = {name: getattr(time, name)
             for name in ("sleep", "time", "monotonic", "strftime", "localtime")}


@pytest.fixture(autouse=True)
def _stdlib_time_is_left_alone():
    yield
    leaked = [name for name, original in _PRISTINE.items()
              if getattr(time, name) is not original]
    if leaked:
        for name in leaked:            # 先修好，否則後面每一支都會跟著紅
            setattr(time, name, _PRISTINE[name])
        raise AssertionError(
            f"這支測試換掉了 stdlib `time` 的 {leaked} 而且沒有換回來。"
            "`ws.time` 就是 `time` 模組本身，所以那是全行程生效的；"
            "請把還原放進 `finally`（連同**所有**指派，不要有一個留在 try 之外）。")


# ---------------------------------------------------------------------------
# 第二條：測試不得終止正式的瀏覽器
# ---------------------------------------------------------------------------
# 這個 repo 的測試跑在一台**同時在做正事**的機器上——一個無人值守的批次可能已經
# 連續跑了好幾天，Chrome 正開著。2026-09-07 一支測試用 `sys.modules.pop("psutil")`
# 想模擬「套件不存在」，結果 `import psutil` 載入真的那一個、`proc.kill()` 殺掉正在
# 跑的 Chrome，弄掉一個執行了 78.7 小時的批次；而報告上只有一行「assert 0 == 2」。
#
# `test_suite_safety.py` 已經用 AST 靜態禁掉那個寫法。**但靜態守門看不到執行期。**
# 同一天稍後又發生了第二次同樣的崩潰，而那時靜態守門已經就位——所以還有別的路徑
# （測試自己裝的替身沒蓋到、夾具順序不對、被測程式在函式內部才 import…）。
#
# 所以有一道**執行期**的最後防線，而且刻意做成**不可還原**（匯入時就換掉，沒有
# 夾具可以把它拿走）。細節與擋的範圍見 `_browser_killguard.py` 的 docstring。

# 防線本體住在 `_browser_killguard.py`，**匯入即生效**。
#
# 為什麼要搬出去：它原本就寫在這個檔案裡，而 `conftest.py` **只有 pytest 會載入**。
# 2026-09-07 第三次弄掉正式瀏覽器的那支變異探針是用 `py -3 harness.py` 直接跑、
# 自己 import 測試模組的——完全在 pytest 的傘外，防線根本沒裝上。搬成獨立模組之後，
# repo 外的探針只要一行 `import _browser_killguard` 就能得到同一道保護。
# conftest 是 pytest 最早載入的東西之一，此時套件目錄還不一定在 `sys.path` 上，
# 所以自己補進去。
#
# **補的是套件目錄，不是本檔所在的目錄**（2026-09-22 測試搬到 repo 根目錄的 `test/`）。
# 搬家之前本檔住在套件裡，`Path(__file__).parent` 剛好就是套件，而 repo 根目錄是 pytest
# 順手插的（套件有 `__init__.py`，prepend 模式插的是它的上一層）——所以測試裡的
# `import discord_bot` 與 `from axiomatic._x import y` 兩種寫法都成立。搬家之後
# `test/` 沒有 `__init__.py`，pytest 只插 `test/` 本身，兩條都要自己補，順序照舊：
# 套件在前、repo 根目錄在後。`pytest.ini` 的 `pythonpath` 也寫了同樣兩個目錄；那一行
# 只在讀得到那個 ini 的回合生效，這裡擋的是其餘的回合（`-c` 指別的設定檔之類）。
import sys as _sys
from pathlib import Path as _Path
_REPO_DIR = _Path(__file__).resolve().parent.parent
_PKG_DIR = _REPO_DIR / "axiomatic"
for _entry in (str(_REPO_DIR), str(_PKG_DIR)):      # 依序插到最前面 → 套件落在第一個
    if _entry not in _sys.path:
        _sys.path.insert(0, _entry)

import _browser_killguard  # noqa: F401,E402  匯入即生效，不要刪


# ---------------------------------------------------------------------------
# 第三條：測試不得寫進 repo 裡的正式狀態（2026-09-19）
# ---------------------------------------------------------------------------
# 量到的事故：`test_recovery_paths` 的佇列還原測試從 2026-09-07 起，每跑一次就往
# **正在服役的** `dorossi_events.ndjson` 附加幾行假事件（假 uid "7"、以及同一支測試
# 擁有者那一半的 `restore_error`）。到 09-19 那個檔 1,713 行裡有 1,602 行是測試寫的，
# 而 `/dorossi logs` 與儀表板讀的就是它。那支測試把佇列檔導開了，**沒有導開事件檔**
# ——同一個檔案的夾具少列一個常數，沒有任何東西會發現。
#
# 兩層，缺一不可：
#
# 1. **預設就導開**（`_side_effect_logs_go_to_tmp`）：`_SIDE_EFFECT_LOGS` 列的是
#    正式程式碼「順手」append 的記錄檔——稽核、事件、產圖歷史、Dorossi 事件與用量。
#    任何一條被測到的程式碼路徑都可能寫它們，靠每個測試作者自己記得列就是這次的
#    事故形狀。所以每一支測試開始前都先指到暫存目錄（檔名不變）。
# 2. **其餘一律大聲失敗**（`_no_writes_into_the_repo` ＋ 稽核掛勾）：測試進行中，
#    任何寫入型的檔案操作（開檔寫入／附加、改名與取代、刪除、建目錄、截斷、複製）
#    只要落在 repo 裡，就**當場擋下**（丟 `PermissionError`，所以正式程式碼的
#    `except OSError` 會把它當成「磁碟說不」處理，不會真的寫進去），並在這支測試
#    結束時讓它**失敗**、點名路徑。擋下是為了保護正式資料，失敗是為了讓人知道——
#    正式程式碼幾乎都把 `OSError` 吞成一行 stderr，光擋不報就又是一次安靜的事故。
#
# 掛勾用 `sys.addaudithook`，因為它**看得到每一條路**：不管是哪個模組、常數有沒有
# 別名、路徑是不是在函式裡才組出來的，只要最後落到 repo 裡的某個路徑就擋得到。
# 掛勾裝上之後拿不掉（跟 `_browser_killguard` 一樣是刻意的），只有測試進行中才會
# 生效（`_REPO_WRITE_GUARD.test` 不是 None）。
#
# **殘餘缺口**：已經開好的檔案 handle 繼續寫不會觸發稽核事件（只有「開」會），
# 子行程也不在這個行程的掛勾底下。
import errno as _errno
import os as _os

_REPO_ROOT = _os.path.normcase(str(_Path(__file__).resolve().parent.parent))

# 這些東西本來就會在 repo 裡寫，而且不是正式狀態：編譯快取與測試工具自己的快取。
_WRITE_EXEMPT_PARTS = {"__pycache__"}
_WRITE_EXEMPT_TOP = {".pytest_cache"}


class _RepoWriteGuard:
    """目前是哪一支測試在跑，以及它被擋下來的寫入。"""

    def __init__(self) -> None:
        self.test: str | None = None
        self.violations: list[tuple[str, str]] = []
        # 目前這支測試用 `repo_write_ok` 標記點名放行的相對路徑（normcase 過）。
        self.allowed: frozenset[str] = frozenset()


_REPO_WRITE_GUARD = _RepoWriteGuard()


def _repo_write_target(path) -> str | None:
    """`path` 落在 repo 裡、而且不在豁免清單上 → 回相對路徑；否則回 None。

    純函式、不碰磁碟。相對路徑依目前的工作目錄解析——pytest 的工作目錄通常就是
    repo 根目錄，所以一個裸檔名的寫入也會被認出來。"""
    try:
        raw = _os.fspath(path)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw:
        return None
    full = _os.path.normcase(_os.path.abspath(raw))
    if not full.startswith(_REPO_ROOT + _os.sep):
        return None
    rel = full[len(_REPO_ROOT) + 1:]
    parts = rel.split(_os.sep)
    if parts[0] in _WRITE_EXEMPT_TOP or _WRITE_EXEMPT_PARTS.intersection(parts):
        return None
    if parts[-1].startswith(".coverage"):
        return None
    return rel


_WRITE_OPEN_FLAGS = (_os.O_WRONLY | _os.O_RDWR | _os.O_APPEND | _os.O_CREAT
                     | _os.O_TRUNC)
# 事件名 → 哪幾個引數是會被改動的路徑。
_WRITE_EVENTS = {
    "os.rename": (0, 1),           # os.rename 與 os.replace 都發這個事件
    "os.remove": (0,),             # os.remove／os.unlink／Path.unlink
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.truncate": (0,),
    "os.link": (1,),
    "os.symlink": (1,),
    "os.utime": (0,),
    "os.chmod": (0,),
    "shutil.copyfile": (1,),
    "shutil.rmtree": (0,),
}


def _write_targets(event: str, args) -> list:
    """一個稽核事件會改動哪些路徑（不是寫入就回空清單）。"""
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(mode, str):
            writing = any(ch in mode for ch in "wax+")
        else:
            writing = isinstance(flags, int) and bool(flags & _WRITE_OPEN_FLAGS)
        return [path] if writing else []
    positions = _WRITE_EVENTS.get(event)
    if positions is None:
        return []
    return [args[i] for i in positions if i < len(args)]


def _repo_write_hook(event, args):
    if _REPO_WRITE_GUARD.test is None:
        return
    if event != "open" and event not in _WRITE_EVENTS:
        return
    for target in _write_targets(event, args):
        rel = _repo_write_target(target)
        if rel is None or rel in _REPO_WRITE_GUARD.allowed:
            continue
        _REPO_WRITE_GUARD.violations.append((event, rel))
        raise PermissionError(
            _errno.EACCES,
            f"測試不得寫進 repo（{event}）：{rel}。把那個路徑常數導到 tmp_path。",
            rel)


_sys.addaudithook(_repo_write_hook)

# ---- 瀏覽器函式庫的匯入期日誌 ------------------------------------------------
# 批次那一側依賴的瀏覽器自動化函式庫在**匯入時**就建一個 `RotatingFileHandler`，
# 檔名是相對路徑 `WEBRunner.log`，依當下的工作目錄解析，`maxBytes > 0` 所以一律附加
# 模式。pytest 的工作目錄是 repo 根目錄，而在大小寫不分的檔案系統上那個檔**就是**
# 正式的 `webrunner.log`——批次正在寫的那一份。於是任何在測試回合裡匯入批次模組的
# 行程都會把正式日誌以附加模式開著直到回合結束，期間函式庫記下的任何 WARNING 以上
# 的紀錄都寫進去。
#
# 守門擋得到**測試進行中**的那一次開檔（2026-09-19 第一次整套就抓到），擋不到
# 收集期的——大多數測試檔在模組層就匯入批次模組，那時守門還沒上膛。所以根治在
# 這裡：只在那一個子模組執行的期間把工作目錄換到暫存區，handler 的路徑在建立時就
# 被 `os.path.abspath` 固定下來，之後換回來也不會跟著變。
#
# 刻意做成**延遲**的（一個 meta_path finder），而不是在這裡直接匯入：直接匯入每個
# 回合多付約 0.7 秒，而且那個函式庫匯入時會把 root logger 設成 DEBUG，會讓只跑幾支
# 測試的回合跟整套回合的全域狀態不一樣。延遲的做法除了日誌落點之外什麼都不改。
# 模組路徑是函式庫的內部名稱，改名的話這裡會安靜失效——
# `test_suite_safety.test_the_browser_library_log_is_parked_outside_the_repo` 看的是
# 結果（handler 最後指到哪裡），不是這個字串，所以會紅。
import importlib.abc as _importlib_abc
import importlib.machinery as _importlib_machinery
import tempfile as _tempfile

_BROWSER_LIB_LOG_MODULE = "je_web_runner.utils.logging.loggin_instance"
_BROWSER_LIB_LOG_DIR = _Path(_tempfile.gettempdir()) / "axiomatic_pytest_browser_log"


class _CwdParkingLoader(_importlib_abc.Loader):
    """把真正的 loader 包起來：執行模組本體的那段期間工作目錄在 `where`。"""

    def __init__(self, inner, where) -> None:
        self._inner = inner
        self._where = where

    def create_module(self, spec):
        return self._inner.create_module(spec)

    def exec_module(self, module) -> None:
        previous = _os.getcwd()
        try:
            _os.makedirs(self._where, exist_ok=True)
            _os.chdir(self._where)
        except OSError as error:            # 換不過去就照原樣匯入，守門會出聲
            print(f"[conftest] 無法把匯入期日誌移出 repo：{error!r}", file=_sys.stderr)
            self._inner.exec_module(module)
            return
        try:
            self._inner.exec_module(module)
        finally:
            _os.chdir(previous)

    def __getattr__(self, name):            # get_source 之類轉給真正的 loader
        return getattr(self._inner, name)


class _CwdParkingFinder(_importlib_abc.MetaPathFinder):
    """只認一個模組名；找到之後把它的 loader 換成 `_CwdParkingLoader`。"""

    def __init__(self, module: str, where) -> None:
        self.module = module
        self.where = where

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.module:
            return None
        spec = _importlib_machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _CwdParkingLoader(spec.loader, self.where)
        return spec


_sys.meta_path.insert(0, _CwdParkingFinder(_BROWSER_LIB_LOG_MODULE, _BROWSER_LIB_LOG_DIR))

# **桌面自動化函式庫是同一個形狀，之前一直漏掉**（2026-09-20）。它的日誌模組匯入時
# 就以相對路徑 `AutoControlGUI.log` 開一個 `RotatingFileHandler`（`mode="w"` 寫在參數上，
# 但 `maxBytes > 0` 時 CPython 一律改成附加），而且一匯入就記一行 `Load Windows Setting`。
# 於是 repo 根目錄那份**正式**記錄檔（bot 的 GUI 指令寫它）一半以上是測試寫的：逐小時計數
# 跟跑測試的時段一一對得上——09-19 02 點用量上限、什麼都沒跑的那一小時是 1 行，跑整套的
# 小時是幾十到上百行，檔案累積到 9,716 行。上面那道 repo 寫入守門看不到它：檔案在任何
# 測試開始之前就開好了，之後的寫入走已經開著的 handle，不發稽核事件。
# ⚠️ **光有這個 finder 不夠**：那個函式庫用 pytest11 註冊了外掛，pytest 一啟動就匯入
# 整個套件，比 conftest 還早。所以 `pytest.ini` 另外用 `-p no:je_auto_control` 擋掉外掛，
# 這裡攔的是之後被測程式碼的延遲匯入。兩半缺一不可。
_GUI_LIB_LOG_MODULE = "je_auto_control.utils.logging.logging_instance"
_GUI_LIB_LOG_DIR = _Path(_tempfile.gettempdir()) / "axiomatic_pytest_gui_log"
_sys.meta_path.insert(0, _CwdParkingFinder(_GUI_LIB_LOG_MODULE, _GUI_LIB_LOG_DIR))
# **原始碼樹 2026-09-22 起換了寫法**（AutoControlGUI 5714a59）：位置改由環境變數
# `JE_AUTOCONTROL_LOG_FILE` 決定，沒設就寫使用者家目錄的 `~/.je_auto_control/logs/`——絕對
# 路徑，上面那個換 cwd 的 finder 對它不起作用，測試會改寫進那份跨行程共用的正式檔。所以在
# 任何匯入之前把它指到同一個暫存目錄。**覆寫，不是 setdefault**：開發者的 shell 若剛好設了
# 一個正式路徑，setdefault 會讓測試寫進去。新版是第一筆記錄時才開檔，所以目錄要先建好。
# finder 照樣留著：fresh clone 從套件庫裝到的發佈版（0.0.222）還是以相對路徑寫 cwd，兩代都要擋。
_os.makedirs(_GUI_LIB_LOG_DIR, exist_ok=True)
_os.environ["JE_AUTOCONTROL_LOG_FILE"] = str(_GUI_LIB_LOG_DIR / "AutoControlGUI.log")

# 正式程式碼「順手」append 的記錄檔：(模組名, 常數名)。每一支測試開始前都先導到
# 暫存目錄。`test_suite_safety` 兩個方向對帳這張表：列的常數要真的存在，而正式
# 程式碼裡每一個以附加模式開啟的模組常數都要列在這裡。
_SIDE_EFFECT_LOGS = (
    ("discord_bot", "AUDIT_FILE"),
    ("discord_bot", "EVENTS_FILE"),
    ("discord_bot", "DOROSSI_EVENTS_FILE"),
    ("discord_bot", "GENERATE_HISTORY_FILE"),
    ("dorossi_backend", "DOROSSI_USAGE_FILE"),
    ("_webrunner_shared", "EVENTS_FILE"),
)


@pytest.fixture(autouse=True)
def _side_effect_logs_go_to_tmp(tmp_path_factory):
    """每一支測試開始前，把 `_SIDE_EFFECT_LOGS` 指到一個新的暫存目錄。

    **先把那幾個模組匯入**，再換常數。只換「已經匯入的」會漏掉在測試本體裡才
    `import discord_bot` 的那種寫法——第一版就是這樣，自己的測試當場抓到。匯入只在
    整個回合的第一次付成本，之後是字典查詢。同一個檔案以兩個名字載入（`discord_bot`
    與套件路徑下的那一份）也都換掉。

    ※ **不要改成用 `monkeypatch`。** autouse 夾具是依名字排序建立的，這一支排在
    `_stdlib_time_is_left_alone` 前面；在這裡要 `monkeypatch`，它就會比那道檢查
    **更早建立、更晚拆掉**，於是任何測試用 `monkeypatch` 換掉的 `time.time` 在那道
    檢查執行時都還沒還原——第一版就這樣讓兩支測試假紅。所以自己換、自己還。
    """
    import importlib as _importlib

    target_dir = tmp_path_factory.mktemp("state")
    wanted = {name for name, _attr in _SIDE_EFFECT_LOGS}
    for name in wanted:
        _importlib.import_module(name)
    saved = []
    for mod_name, mod in list(_sys.modules.items()):
        short = mod_name.rsplit(".", 1)[-1]
        if short not in wanted or mod is None:
            continue
        for name, attr in _SIDE_EFFECT_LOGS:
            if name == short and hasattr(mod, attr):
                original = getattr(mod, attr)
                saved.append((mod, attr, original))
                setattr(mod, attr, target_dir / _Path(original).name)
    try:
        yield
    finally:
        for mod, attr, original in reversed(saved):
            setattr(mod, attr, original)


# 擁有者 UID 是**設定值**（`bot_config.json` 的 `owner_user_id`），套件預設 `0`
# ＝「還沒設定」。那個預設是 fail-closed 的：`_owner_unrestricted` 直接回 False，
# 所以每一道身分閘都拒絕所有人，包括「擁有者」那一條路。
#
# 整套測試要問的卻是**設定好之後**的行為——「擁有者拿到原始細節、其他人拿到泛用
# 句」。在 `0` 底下跑的話，`_owner_detail` 的兩條路會**同時**走到非擁有者那一邊，
# 於是每一支「擁有者看得到 X」的測試都變成拿同一條路徑跟自己比，而且是綠的那一種
# ——正是本專案最怕的形狀。所以整套測試在一個固定的合成 UID 底下跑。
#
# **這裡刻意是模組層的一次性設定，不是 autouse 夾具。** 好幾支測試在自己的模組層
# 就讀了這個常數（`_ME = b.OWNER_USER_ID`，用來組參數化的清單），那比任何夾具都早
# ——夾具版會讓那些清單拿到 `0`，而測試本體拿到設定後的值，兩邊對不起來。
#
# 要驗「沒設定時一律拒絕」的測試，自己 `monkeypatch.setattr(b, "OWNER_USER_ID", 0)`。
_TEST_OWNER_USER_ID = 400000000000000001

# 兩個名字今天指向同一個人，但語意不同（`DOROSSI_USER_ID` 是「誰能用對話後端」，
# `OWNER_USER_ID` 是「誰能操作主機」），所以兩個都要設。實際的指派在
# `pytest_configure`——那一步排在收集之前，所以測試模組層讀到的已經是設定後的值。
_OWNER_UID_CONSTANTS = ("DOROSSI_USER_ID", "OWNER_USER_ID")


def _configure_the_test_owner() -> None:
    """把兩個擁有者 UID 常數設成 `_TEST_OWNER_USER_ID`（理由見上面那段）。

    **用 `importlib` 而不是 `import discord_bot`**：本檔落在
    `test_bot_helpers.test_nothing_in_the_package_imports_the_bot` 的掃描範圍內，
    那道守門禁止任何非 bot 模組靜態 import `discord_bot`（防循環）。這裡要的不是
    循環，是在收集之前把一個設定值填好，所以走動態匯入——與
    `_side_effect_logs_go_to_tmp` 同一個寫法、同一個理由。
    """
    import importlib

    module = importlib.import_module("discord_bot")
    for name in _OWNER_UID_CONSTANTS:
        setattr(module, name, _TEST_OWNER_USER_ID)


def _allowed_repo_writes(marker) -> frozenset:
    """`repo_write_ok` 標記 → 放行的相對路徑集合（normcase 過，跟掛勾比對的形狀一樣）。

    沒有標記 → 空集合。有標記卻沒點名路徑、或沒寫 `reason=` → `ValueError`。"""
    if marker is None:
        return frozenset()
    if not marker.args or not str(marker.kwargs.get("reason") or "").strip():
        raise ValueError("`repo_write_ok` 必須點名至少一個相對路徑，並用 reason= 寫出"
                         "為什麼這支測試非碰正式路徑不可。")
    return frozenset(_os.path.normcase(_os.path.normpath(str(p))) for p in marker.args)


@pytest.fixture(autouse=True)
def _no_writes_into_the_repo(request):
    """這支測試進行中，任何落在 repo 裡的寫入都被擋下；結束時被擋過就讓它失敗。

    例外只有一種寫法：在那一支測試上標 `@pytest.mark.repo_write_ok(<相對路徑>…,
    reason="…")`，**逐條點名**、**寫出理由**。沒有萬用豁免，也不按檔名樣式放行——
    那正是會讓下一個洩漏安靜通過的形狀。"""
    try:
        allowed = _allowed_repo_writes(request.node.get_closest_marker("repo_write_ok"))
    except ValueError as error:
        pytest.fail(str(error), pytrace=False)
    _REPO_WRITE_GUARD.violations.clear()
    _REPO_WRITE_GUARD.allowed = allowed
    _REPO_WRITE_GUARD.test = request.node.nodeid
    try:
        yield
    finally:
        _REPO_WRITE_GUARD.test = None
        _REPO_WRITE_GUARD.allowed = frozenset()
    hits = list(_REPO_WRITE_GUARD.violations)
    _REPO_WRITE_GUARD.violations.clear()
    if hits:
        shown = "、".join(f"{rel}（{event}）" for event, rel in hits[:8])
        pytest.fail(
            f"這支測試試圖寫進 repo 裡的正式檔案，已被擋下：{shown}"
            f"{'……' if len(hits) > 8 else ''}。正式程式碼多半把 OSError 吞掉，所以"
            "這一次沒寫進去純粹是因為有這道防線——請把對應的路徑常數用 "
            "monkeypatch 導到 tmp_path。", pytrace=False)


@pytest.fixture
def repo_write_guard():
    """讓守門自己的測試看得到掛勾的狀態、判準與導開清單（`conftest` 不保證
    import 得到，所以走夾具）。"""
    _REPO_WRITE_GUARD.target = _repo_write_target
    # 掛勾本體與它的兩張表。**直接呼叫 `hook` 是唯一看得見這段程式碼的方式**：
    # CPython 在 audit hook 的回呼裡關掉追蹤，所以 coverage 對 hook body 一律回報
    # 「從來沒跑過」——2026-09-20 實測，三種 tracer core 一致。那不是沒測到，是量不到。
    _REPO_WRITE_GUARD.hook = _repo_write_hook
    _REPO_WRITE_GUARD.write_targets = _write_targets
    _REPO_WRITE_GUARD.write_events = _WRITE_EVENTS
    _REPO_WRITE_GUARD.side_effect_logs = _SIDE_EFFECT_LOGS
    _REPO_WRITE_GUARD.repo_root = _REPO_ROOT
    _REPO_WRITE_GUARD.allowed_from_marker = _allowed_repo_writes
    _REPO_WRITE_GUARD.parking_finder = _CwdParkingFinder
    _REPO_WRITE_GUARD.browser_log_dir = _BROWSER_LIB_LOG_DIR
    _REPO_WRITE_GUARD.gui_log_dir = _GUI_LIB_LOG_DIR
    return _REPO_WRITE_GUARD


# 有 `_warn_once` 去重集合的模組。**新增一個 `_WARNED` 就要加進這裡**，否則那個
# 模組的測試會開始出現「只有先跑的那支看得到警告」這種依順序才發生的失敗。
#
# 這條 2026-09-09 之前只是一句叮嚀。現在
# `test_suite_safety.test_every_warn_dedup_cache_is_registered_in_conftest`
# 用 AST 兩個方向都釘住它：漏登記會紅，登記了一個已經沒有 `_WARNED` 的模組也會紅。
# 2026-09-09：三份實作合併成 `_warn_dedup` 之後，這裡只剩一個入口。名單留著
# 不是為了「現在有幾個」，是為了**下一個**——任何模組再宣告自己的 `_WARNED`
# 都要登記進來，否則那個模組的測試會開始出現依順序才發生的失敗。
_WARN_DEDUP_MODULES = ("_warn_dedup",)


@pytest.fixture(autouse=True)
def _reset_warn_dedup():
    """每支測試都從「還沒警告過任何事」開始。

    這幾個模組的 `_warn_once` 用 **module-level 的集合**去重（理由見各自的實作：
    設定檔每個 tick／每個角色都重讀，同一句抱怨會洗掉整份記錄檔）。副作用是測試
    之間會互相汙染：**兩支測試只要觸發同一段警告文字，後跑的那支就什麼都看不到。**

    這不是假想。`test_undecodable_files.test_the_presence_configs_fall_back` 有三個
    參數化案例，三個都把設定檔寫成 `tmp_path/"cfg.json"`——於是三段警告是**逐字
    相同**的，第一個印出來、後兩個被吃掉，而失敗訊息是 `assert 'failed' in ''`，
    完全看不出跟去重有關。

    夾具放在 `conftest.py` 而不是各測試檔，是因為這條會**跟著模組跑**：任何碰到
    那些設定的測試檔都可能踩到，寫在單一檔案裡等於等下一個人再中一次。
    模組層的可變狀態要有一個重設入口，而那個入口只該有一份。

    在函式內才 import：conftest 是 pytest 最早載入的東西，不需要為了一個夾具讓
    每個 session 都付這些模組的匯入成本。
    """
    import importlib

    caches = [importlib.import_module(name)._WARNED
              for name in _WARN_DEDUP_MODULES]
    for cache in caches:
        cache.clear()
    # 同一個形狀：SMTC 探測的連續逾時退避也是模組層狀態。一支測試讓它逾時，同一個
    # worker 之後跑的 `probe_signals_async` 測試就會跳過它們換上的假探測。
    presence_probe = importlib.import_module("presence_probe")
    presence_probe._smtc_backoff_reset()
    yield
    for cache in caches:
        cache.clear()
    presence_probe._smtc_backoff_reset()



@pytest.fixture(autouse=True)
def _power_requests_stay_off_the_os():
    """測試不向作業系統要電源要求。

    bot 的 Dorossi 回合／自走迴圈與批次監督都會經 `_power_request` 拿一份，測試一跑
    就會在本機的 `powercfg /requests` 留下痕跡。這裡把行程內的計數器換成一個沒有
    後端的：計數照算、`status()` 照樣可讀，只是不碰作業系統。要看真實呼叫的測試
    （`test_power_request.py`）自己建 `_WindowsBackend` 或換掉 `_MANAGER`。

    ※ 與 `_side_effect_logs_go_to_tmp` 同一個理由，**不要改成用 `monkeypatch`**：這一支
    排在 `_stdlib_time_is_left_alone` 前面，要了 `monkeypatch` 就會讓測試換掉的
    `time.monotonic` 在那道檢查執行時還沒還原（第一版就這樣讓 `test_chrome_slot` 七支
    假紅）。所以自己換、自己還。
    """
    import _power_request as _power

    saved = _power._MANAGER
    _power._MANAGER = _power._Manager(backend_factory=lambda: None)
    try:
        yield
    finally:
        _power._MANAGER = saved


# ---------------------------------------------------------------------------
# 「子行程根本沒起來」的失敗要當場說清楚是主機資源，不是這次的改動
# ---------------------------------------------------------------------------
#
# 2026-09-10 花了不少時間在一個假警報上：整批 24 支測試同時紅，全部集中在
# `test_gui_control` / `test_supervisor` / `test_presence_probe` /
# `test_je_facade` / `test_variant_parity` / `test_suite_safety`，訊息長這樣：
#
#     assert 3221225794 == 0
#
# `3221225794` 是 `0xC0000142` ＝ `STATUS_DLL_INIT_FAILED`：Windows 在**行程初始化
# 階段**就放棄了，子行程連 `main` 都沒跑到，所以 stdout 與 stderr **都是空的**。
# 原因是同時有兩個完整的測試回合在跑（本機的桌面堆積／控制代碼撐不住那個併發量），
# 單獨重跑那六個檔案 742 支全綠。
#
# 值得寫成程式碼而不是只寫進文件，是因為這個失敗形態**看起來完全像是自己剛改壞的**
# ——斷言指著業務邏輯、子行程什麼都沒印、每一支都是「應該最穩」的那種測試。下一個人
# 會從自己的 diff 開始查，而答案在別的行程裡。
# 只收「行程**從來沒有開始執行**」的 NTSTATUS。像 `0xC00000FD`（堆疊溢位）那種
# 是**跑起來之後**才炸的，套同一段說明只會把人指向錯的方向。三段文字是本機用
# `FormatMessageW` ＋ `ntdll` 實際問出來的，不是背的。
_SPAWN_FAILED_RCS = {
    # 十進位的樣子最重要：斷言訊息印出來的就是這個數字。
    3221225794: "0xC0000142，DLL 初始化失敗",
    3221225495: "0xC0000017，虛擬記憶體或分頁檔配額不足",
    3221225781: "0xC0000135，載入時找不到某個 DLL",
}

_SPAWN_FAILED_HINT = (
    "這個離開碼代表**子行程從來沒有開始執行**（Windows 在行程初始化階段就放棄，"
    "所以它的 stdout／stderr 都是空的），不是被測邏輯回傳的值。\n"
    "最常見的原因是**同時有第二個測試回合在跑**——兩批各自狂開子行程，主機的"
    "桌面堆積／控制代碼撐不住。先確認沒有別的 pytest 在跑，再單獨重跑這幾支；"
    "全綠就代表這是主機資源，不是你的改動。")


def _spawn_failure_note(text: str) -> str | None:
    """失敗文字裡有沒有「子行程起不來」的離開碼；有的話回一段說明。"""
    for rc, meaning in _SPAWN_FAILED_RCS.items():
        if str(rc) in text or f"{rc:#x}" in text.lower():
            return f"離開碼 {rc}＝{meaning}。\n{_SPAWN_FAILED_HINT}"
    return None


# ---------------------------------------------------------------------------
# 樹上還躺著一個變異就不要跑整套
# ---------------------------------------------------------------------------
#
# `mutation_harness` 會**直接改實體檔**，還原靠 `finally`——而 `finally` 在行程被
# 砍掉時不會跑。它自己有解法（快照放 repo 外、下一次啟動先 heal），但那個 heal
# **只有骨架自己下一次啟動才會發生**。被砍掉之後最可能的下一件事不是再跑一次變異，
# 是有人跑一次完整測試。
#
# 2026-09-10 就是這樣：一輪三個變異跑到第二個時 host 行程結束，
# `test_bot_helpers.py` 的別名不動點那八行被換成 `pass` 留在原地。那個狀態下跑整套
# 會紅，而且斷言指著業務邏輯、diff 看起來是自己剛寫的程式碼——完全沒有線索指向
# 「這不是你的改動」。
#
# 所以在**收集之前**問一次。判準與訊息都在 `mutation_harness.interrupted_targets`
# ——標記是那支工具的契約，查詢就該跟契約放在一起（而且 `conftest.py` 是 pytest
# 自己載入的，`import conftest` 不保證拿得到，測試也搆不到寫在這裡的函式）。


def pytest_configure(config):
    """樹上有變異殘留就當場停下來，不要讓它變成一批假紅。

    **骨架自己開的 child 要放行**：那時候樹是被故意改壞的，那正是它要量的東西。
    兩者在磁碟上完全一樣，只有環境變數分得出來。
    """
    import os

    import mutation_harness

    # 收集之前就要做完：測試模組層有人直接讀這個常數來組參數化清單。
    _configure_the_test_owner()

    # 必須排在下面兩個會提早結束的分支前面，否則那些回合會對這個標記發警告。
    config.addinivalue_line(
        "markers",
        "repo_write_ok(*paths, reason): 這支測試**刻意**碰 repo 裡的這幾個正式路徑"
        "（逐條點名、必須寫理由），repo 寫入守門對它們放行")
    if os.environ.get(mutation_harness.CHILD_ENV_FLAG):
        return
    dirty = mutation_harness.interrupted_targets()
    if not dirty:
        return
    # **先問「是不是有人正在跑」再給補救建議。** 這兩種情況在磁碟上一模一樣，但
    # 下一步完全相反：被中斷要還原，正在跑要不要碰。2026-09-12 這道訊息舊版寫的
    # 補救方式（複製 `.orig` 回去並刪掉標記）被套用在一輪**還活著**的變異上，結果
    # 是還原被下一個變異蓋掉，而且標記沒了之後偵測器直接瞎掉。
    live = mutation_harness.live_mutation_runs()
    if live:
        raise pytest.UsageError(
            "現在**真的有一輪變異正在跑**："
            + "、".join(f"{target}（pid {pid}）" for target, pid in live)
            + "。\n這時候的樹是被**故意**改壞的，那正是它要量的東西。"
            "\n※ 不要還原、不要刪 `.inprogress` 標記——那個標記是唯一在說"
            "「有一輪在飛」的東西，刪掉之後這道檢查會回報乾淨而樹是髒的。"
            "\n等它跑完再來（這也是『不要在別人改樹的時候讀樹』那一條）。")
    raise pytest.UsageError(
        "變異測試的進行中標記還在，而且這些檔案現在跟快照不一樣："
        f"{'、'.join(dirty)}。\n"
        "代表上一輪變異**沒有跑完**（行程被砍掉，`finally` 的還原沒跑），樹上"
        "還躺著一個故意改壞的版本。現在跑整套會得到一批假紅，而且斷言會指著"
        "業務邏輯，看起來像是你自己剛改壞的。\n"
        "已經確認過沒有任何一輪正在跑（見上面那條分支）。用 "
        "`mutation_harness.run_mutations` 跑任何一輪讓它 heal，或直接把 "
        f"`{mutation_harness.DEFAULT_SNAPSHOT_DIR}` 裡對應的 `.orig` 複製回去"
        "並刪掉 `.inprogress` 標記。\n"
        "※ 複製回去之後請**逐位元組**跟 `.orig` 比對，不要用 grep 找那一行："
        "變異可以保留那一行而把語意反過來（實例：把 "
        "`{**os.environ, K: V}` 對調成 `{K: V, **os.environ}`）。")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """失敗報告裡看到那幾個離開碼，就補一段「這是主機資源」的說明。

    用 pluggy 的**新式** `wrapper=True`（`report = yield`），不是舊的
    `hookwrapper=True` ＋ `(yield).get_result()`——本機是 pytest 9 / pluggy 1.6。
    包裝器一定要把 `report` 原樣 `return` 回去，否則後面的 hook 會收到 `None`。
    """
    report = yield
    if report.failed:
        note = _spawn_failure_note(str(report.longrepr))
        if note:
            report.sections.append(("子行程起不來（主機資源，不是這次的改動）", note))
    return report


# ---------------------------------------------------------------------------
# 第四條：同一份原始碼只剖析一次（2026-09-20）
# ---------------------------------------------------------------------------
# 量出來的：整套 842 秒裡 **492 秒（58%）花在 `ast.parse`**——9,856 次呼叫只有 751 份
# 不同的原始碼，`discord_bot.py`（23k 行，剖析一次 1.34 秒）一個檔就被剖析了 149 次。
# 本專案的守門大多是「把整棵樹掃一遍」的靜態檢查，每一支各自 `ast.parse` 一次是它們
# 天生的形狀，所以慢的不是哪一支測試，是乘法。量法：一個只記時間、不改行為的外掛
# 包住 `ast.parse` 跑一次整套。
#
# **快取的是 pickle 之後的位元組，不是樹本身。** 每次命中都 `pickle.loads` 出一棵
# **全新的**樹（實測 0.08 秒，剖析是 1.34 秒；逐欄位含行號 `ast.dump(include_attributes=
# True)` 完全相同），所以就算某支測試或 pytest 自己的斷言改寫（它會**就地**改樹）
# 動了拿到的那一棵，也碰不到下一個人的。未命中時先存位元組、再把剛剖析的那棵交出去，
# 順序不能反——反了的話斷言改寫改過的樹會被存進快取。
#
# 鍵是**內容**的雜湊加上全部參數，不是檔名或 mtime：變異骨架改了檔案、同一個行程裡
# 有人剖析合成語料，內容一變就是新的鍵。不能雜湊的呼叫（非字串來源、參數不可雜湊）
# 直接走原本的 `ast.parse`。
#
# ⚠️ **命中時不會再發 `SyntaxWarning`**（警告只在真的剖析時發）。所以任何靠「剖析時
# 抓警告」偵測違規的守門都**必須用 `compile()`**，不要用 `ast.parse`——
# `test_shadowed_definitions._syntax_warnings_in` 就是這樣寫的，`compile` 不經過這裡。
# 這只影響測試行程；正式程式碼不載入 conftest。
import ast as _ast
import hashlib as _hashlib
import pickle as _pickle
from collections import deque as _deque

_REAL_AST_PARSE = _ast.parse
_AST_PARSE_CACHE: dict = {}


def _ast_parse_key(source, filename, args, kwargs):
    """快取鍵；回 None 表示這次呼叫不走快取。純函式。"""
    if isinstance(source, str):
        raw = source.encode("utf-8", "surrogatepass")
    elif isinstance(source, bytes):
        raw = source
    else:
        return None
    key = (_hashlib.blake2b(raw, digest_size=16).digest(), type(source).__name__,
           str(filename), args, tuple(sorted(kwargs.items())))
    try:
        hash(key)
    except TypeError:
        return None
    return key


def _cached_ast_parse(source, filename="<unknown>", *args, **kwargs):
    key = _ast_parse_key(source, filename, args, kwargs)
    if key is not None:
        blob = _AST_PARSE_CACHE.get(key)
        if blob is not None:
            return _pickle.loads(blob)
    tree = _REAL_AST_PARSE(source, filename, *args, **kwargs)
    if key is not None:
        try:
            _AST_PARSE_CACHE[key] = _pickle.dumps(tree, _pickle.HIGHEST_PROTOCOL)
        except (RecursionError, _pickle.PicklingError):
            pass    # 巢狀太深存不了就不存；這一次的結果照樣正確
    return tree


# ---------------------------------------------------------------------------
# 第五條：`ast.walk` 攤平掉兩層產生器（2026-09-20）
# ---------------------------------------------------------------------------
# 剖析快取之後再剖一次面。最重的那個測試檔（`test_bot_helpers.py`，單獨跑 114 秒）
# 裡 **59.4 秒在 `ast.walk`**：cProfile 記到 2,375 萬次節點走訪、4,798 萬次
# `iter_child_nodes`、6,312 萬次 `iter_fields`。慢的一樣不是哪一支測試，是形狀——標準庫
# 的 walk 每個節點都要穿過兩層產生器（`walk` → `iter_child_nodes` → `iter_fields`），
# 而本專案的守門幾乎都是「整棵樹掃一遍」。
#
# 這裡把那兩層攤平進一個迴圈。**它不是快取、沒有共用狀態**：每次還是真的走一遍，只是
# 少了兩層產生器的往返。實測同一棵 `discord_bot.py` 的樹 55.5 → 36.9 毫秒（1.50 倍），
# 節點的**順序與物件身分逐一相同**（廣度優先、`_fields` 的順序、list 裡的順序）。
# 照樣是產生器，所以提早 `break` 的呼叫端不會被迫付整棵樹的錢。
#
# 逃生口與剖析快取共用 `AXIOMATIC_AST_CACHE_OFF=1`（同一個 A/B 開關）。
# 守門：`test_suite_safety` 裡 `_flat_ast_walk` 那幾支，拿真實檔案逐一比對身分，
# 另加合成的邊界節點（欄位是 None、欄位是非 AST 的 list）。
_REAL_AST_WALK = _ast.walk


def _flat_ast_walk(node):
    """與 `ast.walk` 等價：同樣廣度優先、同樣的順序，只是少兩層產生器。"""
    todo = _deque([node])
    popleft = todo.popleft
    append = todo.append
    ast_type = _ast.AST
    while todo:
        node = popleft()
        for field in node._fields:
            value = getattr(node, field, None)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast_type):
                        append(item)
            elif isinstance(value, ast_type):
                append(value)
        yield node


# 逃生口：懷疑快取時設 `AXIOMATIC_AST_CACHE_OFF=1` 重跑一次比對（也是量快取效果的 A/B 開關）。
if not _os.environ.get("AXIOMATIC_AST_CACHE_OFF"):
    _ast.parse = _cached_ast_parse
    _ast.walk = _flat_ast_walk
