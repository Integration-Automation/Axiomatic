# -*- coding: utf-8 -*-
"""執行期防線：擋住「測試／探針殺掉正式瀏覽器」。**匯入即生效。**

這台機器上通常有一個已經跑了好幾天的無人值守批次，Chrome 一直開著。2026-09-07
一天之內因為測試工具而弄掉它**三次**：

1. 一支測試用 `sys.modules.pop("psutil")` 想模擬「套件不存在」，結果
   `import psutil` 載入真的那一個、`proc.kill()` 殺掉正在跑的 Chrome，毀掉一個
   執行了 78.7 小時的批次——而報告上只有一行 `assert 0 == 2`。
2. 補了 AST 靜態守門（`test_suite_safety.py`）之後**又**發生一次。**靜態守門看不到
   執行期**：被測程式在函式內部才 import、夾具順序、替身裝錯層級，都繞得過原始碼
   比對。所以才有了這道執行期防線。
3. 一支變異測試探針用 `ns = dict(module.__dict__)` 建立變異模組——那是個**快照**，
   於是測試打的 monkeypatch 對它無效，呼叫到的是**真的** `_kill_chrome_pids`。

**第三次暴露的正是這個模組存在的理由。** 那道防線原本寫在 `conftest.py` 裡，而
`conftest.py` **只有 pytest 會載入**。那支探針是用 `py -3 harness.py` 直接執行、
自己 import 測試模組的，完全在 pytest 的傘外——防線根本沒裝上。

所以現在它住在一個獨立模組裡，任何人都能用**一行**啟用：

    import _browser_killguard   # noqa: F401  匯入即生效

`conftest.py` 會匯入它（涵蓋所有 pytest 執行）。**在 repo 外自己跑的變異探針
必須自己加上這一行**，而且要放在 import 專案模組**之前**。

擋什麼：

* 任何 `psutil.Process.kill()` / `.terminate()`，只要對象叫 `chrome.exe` 或
  `chromedriver.exe`；
* 任何 `subprocess.run` / `Popen` 執行 `taskkill`。

**範圍刻意開得很窄**：殺自己 spawn 出來的 python 子行程是完全正當的
（`test_supervisor.py` 就在做），所以只擋這兩個 image 名稱與 `taskkill` 這一個
命令。一個會亂叫的守門是會被關掉的守門。

失效方向：**寧可測試紅，不可以真的動到這台機器。**

**2026-09-10：最危險的那一類探針不再需要記得。** `mutation_harness.py` 自己在模組層
匯入這道防線，所以任何探針只要 `from mutation_harness import run_mutations`，防線就
在它有機會做任何事之前裝好了——而 2026-09-07 第三次事故的肇事者正是一支變異探針，
也就是說「最可能忘記那一行的人」與「最需要那道防線的人」本來就是同一個人。
`test_suite_safety.test_importing_the_mutation_harness_installs_the_backstop_too`
用子行程釘住這條路（刻意只匯入 `mutation_harness`），所以那行 import 被當成沒用的
import 刪掉時會變紅。

**剩下的殘餘缺口（誠實記下來）**：不經過變異骨架、又自己動手殺行程的探針仍然要記得
加那一行。自己寫的那一行也仍然值得寫——它涵蓋「還沒匯入骨架就先動手」的情況，而且
是唯一能保證**順序**的寫法（`from subprocess import run` 這種寫法會在防線裝上之前
就把原函式綁走）。防線是縱深，不是替代品；**第一道仍然是讓替身真的生效**（變異模組
要 exec 進真的 `__dict__`，不要用 `dict(...)` 快照）。
"""
from __future__ import annotations

_PROTECTED_IMAGES = {"chrome.exe", "chromedriver.exe"}

# 匯入兩次不要包兩層。`conftest.py` 與探針可能都匯入它。
_INSTALLED = False


def _refuse(what: str) -> AssertionError:
    return AssertionError(
        f"測試試圖{what}。這台機器上可能有正式批次正在跑，殺掉它的瀏覽器會讓一個"
        "可能已經執行好幾天的無人值守工作當場崩潰（2026-09-07 發生過三次）。\n"
        "要測終止行程的邏輯，請注入假的 psutil／假的 `subprocess.run`——"
        '模擬「套件不存在」用 `sys.modules["x"] = None`，**不要**用 '
        "`sys.modules.pop`（那只清快取，下一次 import 會載入真的那一個）。\n"
        "如果你是在寫變異測試探針：變異模組要 exec 進**真的** `__dict__`，"
        "不要用 `dict(module.__dict__)` 快照——快照會讓 monkeypatch 對它無效，"
        "於是呼叫到真的終止函式（那正是第三次事故的成因）。\n"
        "見 `axiomatic/_browser_killguard.py`。")


def _argv_head(args) -> str:
    if isinstance(args, (list, tuple)) and args:
        first = args[0]
    elif isinstance(args, str):
        first = args.split()[0] if args.split() else ""
    else:
        return ""
    return str(first).rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower()


def install() -> bool:
    """裝上防線。已經裝過就直接回 False。匯入本模組時會自動呼叫一次。"""
    global _INSTALLED
    if _INSTALLED:
        return False
    _INSTALLED = True

    import subprocess

    _real_run = subprocess.run
    _real_popen = subprocess.Popen

    # `run` 與 `Popen` 兩邊都檢查 `taskkill`，這個冗餘是**刻意的**。
    # `subprocess.run` 內部一定會建一個 `Popen`，所以單看功能，`run` 這一層是多餘
    # 的——實測：只拿掉 `run` 的檢查，`taskkill` 仍然被 `_GuardedPopen` 擋住。
    # 留著的理由是這個 repo 的測試**很常 monkeypatch `subprocess`**：某支測試把
    # `Popen` 換回原版（或換成自己的替身）之後，`run` 這一層就是唯一還站著的。
    # 副作用是「只拿掉其中一個」的變異會被另一個遮住而看起來像逃掉——那不是守門
    # 有洞，是兩道防護互相遮蔽。變異要**成對**做，見
    # `<repo 外的暫存目錄>\mutate_killguard.py`。
    def _guarded_run(*args, **kwargs):
        if args and _argv_head(args[0]).startswith("taskkill"):
            raise _refuse("執行 `taskkill`")
        return _real_run(*args, **kwargs)

    # **`Popen` 必須用子類別包，不能用函式。** 它是**類別**，而外面真的有人寫
    # `class Popen(subprocess.Popen)`（標準函式庫與測試框架都有）。換成函式之後
    # 那種繼承會炸 `TypeError: function() argument 'code' must be code, not str`。
    #
    # 這個缺陷本來就在（防線還住在 `conftest.py` 的時候就是函式），只是被**匯入
    # 順序**蓋住了：pytest 先載入自己的東西、conftest 才被匯入，所以那些繼承早就
    # 發生完了。防線一搬到「探針最早匯入的東西」，同一個缺陷立刻現形。
    # 教訓：**替換一個名字的時候，要保留它原本的種類**（類別要還是類別，才撐得住
    # 繼承與 `isinstance`）。
    class _GuardedPopen(_real_popen):
        def __init__(self, args, *rest, **kwargs):
            if _argv_head(args).startswith("taskkill"):
                raise _refuse("執行 `taskkill`")
            super().__init__(args, *rest, **kwargs)

    subprocess.run = _guarded_run
    subprocess.Popen = _GuardedPopen

    try:
        import psutil
    except ImportError:
        return True

    def _guard(method_name):
        original = getattr(psutil.Process, method_name)

        def _guarded(self, *args, **kwargs):
            try:
                name = (self.name() or "").lower()
            except Exception:      # pylint: disable=broad-except
                name = ""          # 問不到名字就放行——它已經不是 chrome 了
            if name in _PROTECTED_IMAGES:
                raise _refuse(f"對 `{name}`（pid={self.pid}）呼叫 "
                              f"`{method_name}()`")
            return original(self, *args, **kwargs)

        setattr(psutil.Process, method_name, _guarded)

    _guard("kill")
    _guard("terminate")
    return True


install()
