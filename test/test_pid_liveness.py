"""Windows 上的 PID 存活探測（`CLAUDE.md` 的跨領域硬規則）的靜態防線。

規則寫得很明確——「**絕對不要**在 Windows 上用 ``os.kill(pid, 0)`` 當存活探測」，
而且連理由都列了：``signal.CTRL_C_EVENT == 0``，所以 CPython 的 Windows ``os.kill``
會走 ``GenerateConsoleCtrlEvent`` 分支，把 ``pid`` 當成 **console process group
id**，本機實測（Windows 11 / CPython 3.14.4）兩個方向都會錯：剛結束的子行程讀成
「還活著」，而不在本 console group 的活行程會丟例外、讀成「已死」。更糟的是它**不
是純探測**——真的對上某個 console process group 時會送出一個貨真價實的 Ctrl+C，
而 `run_batch.py` → `start_webrunner.py` → webrunner 這條鏈正好共用同一個 console，
等於直接打斷正在跑的批次產圖。

但這條規則**沒有任何東西在檢查**，只靠三份 `_pid_alive` 的 docstring 互相提醒，而
`CLAUDE.md` 自己就寫著「a fourth copy must too」——第四份會由誰擋下來，原本沒有答
案。而且它防的又是安靜的錯誤結果：探測回錯的答案不會拋例外，只會讓 `_load_pid`
砍掉有效的 PID 檔、讓 `_webrunner_alive()` 說謊、讓取鎖端樂觀地開出第二個 Chrome
stack 疊在同一個 profile 上。

四道檢查，都是機械可判定的：

1. 任何 ``os.kill(x, 0)`` 都必須被同一個函式裡「先攔截 Windows 並 return」的分支
   擋在前面（POSIX 專用路徑才合法）；
2. 每一份 `_pid_alive` 都要有 Windows 走得通的探測（psutil／`_nt_pid_alive`／
   明確地保守回 True），不能只有 POSIX 那條；
3. 用 ctypes 退路的地方一定要自己釘 ``argtypes``／``restype``——預設 restype 是
   ``c_int``，64-bit 的 HANDLE 會被截斷，而截斷後的 handle 照樣「不是 0」，
   於是 `GetExitCodeProcess` 對著垃圾 handle 失敗，錯誤靜靜地被吞掉；
4. 三份副本對「判不出來」的答案**刻意不同**，這個分歧本身要被釘住——不然有人
   會順手「統一」它們，而那正是 `CLAUDE.md` 要求依情境選邊的地方。

**第五道是 2026-09-20 補的，而且它不是靜態的。** 上面四道問的都是「形狀對不對」
——有沒有擋在 `os.kill` 前面、有沒有釘 `argtypes`、有沒有 Windows 那條路。沒有一道
問過「**答案對不對**」，而規則自己列的頭號失敗模式（HANDLE 被截斷）長出來的形狀是
**完全正確**的：`argtypes` 釘了、Windows 分支在、`os.kill` 一次都沒出現，只是每個
pid 都回「活著」。量出來的證據：`verify_browser._nt_pid_alive` 的 27 行在這天之前
**一行都沒有被執行過**，而 `_process_control` 那份同樣的 ctypes 主體早就有一整套
行為測試（live／dead／與 psutil 對帳／不得送訊號／壞掉要保守）。同一條規則的兩份
抄本，一份量過、一份沒有——正是這個檔案存在要擋的那種漂移。所以第五道把行為測試
**從 `_DOCUMENTED_COPIES` 推導出來**跑在每一份有 ctypes 退路的抄本上，第四份抄本
會自動入列，不必有人想起來。
"""
import ast
import re
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PKG_ROOT.parent

# `CLAUDE.md` 明列的三份副本。第四份是**允許**的（規則自己這麼說），但要連同
# 這裡與 `CLAUDE.md` 的列舉一起更新——上面 1～3 會自動蓋到它，這一格只是逼人
# 回去把文件補齊，免得規則的正本停留在「目前有三份」。
_DOCUMENTED_COPIES = {
    "_process_control.py", "_chrome_slot.py", "verify_browser.py"}

# 「判不出來」時各自回什麼——`CLAUDE.md` 說這個分歧是刻意的，判準是「這個錯該往
# 哪邊倒」。問「我該不該拒絕啟動／讓位？」的那兩支要保守的 True，否則會樂觀地
# 開出第二個 Chrome stack；`_process_control` 是「這個 PID 檔還算數嗎」，倒向
# False 只是多清一次 PID 檔。**注意這個分歧活在 POSIX 那條分支**（``except
# OSError``）——Windows 那條三份都是保守的 True。
_UNDECIDABLE_POSIX_ANSWER = {
    "_process_control.py": False,
    "_chrome_slot.py": True,
    "verify_browser.py": True,
}

# Windows 上可接受的探測方式。`_chrome_slot` 是第三種：沒有 psutil 就乾脆不探測、
# 直接保守回 True，由時間 staleness 當 backstop——那也算「沒有把 signal 0 送到
# Windows 上」，所以合格。
_WINDOWS_SAFE_PROBES = ("psutil.pid_exists", "pid_exists", "_nt_pid_alive")

_CTYPES_FUNCS = ("OpenProcess", "GetExitCodeProcess", "CloseHandle")


# 掃到的檔案數下限。空清單跟乾淨的結果長得一模一樣：`_live_stack_sources()` 回
# `[]` 的話，下面每一支「掃過全部、沒有違規」的測試都會綠，而它們其實一個檔案都
# 沒讀。
_SCAN_FLOOR = 25


def _live_stack_sources(pkg_root: Path | None = None,
                        repo_root: Path | None = None) -> list[Path]:
    """live stack 的原始碼。測試檔本身排除——它們可以合法地示範壞寫法。

    **repo root 這一半刻意是 `*.py` 而不是 `start_*.py`。** 這條規則（CLAUDE.md
    的「Windows PID 存活」）是跨領域的，**一個模組都沒提到**——它管的是「專案裡任何
    地方都不准把 signal 0 送到 Windows」。而 2026-09-10 之前這裡寫的是
    `glob("start_*.py")` ＋ 明列的 `run_batch.py`，於是 `install_autostart.py`
    ——一支貨真價實的正式腳本，工作是裝上「開機自動啟動」的排程任務——從來沒有被
    掃過。它今天是乾淨的（實測 `os.kill` 0 處），所以這是**趁乾淨鎖範圍**，不是修
    缺陷。

    發現的方式值得記下來：**同一族的兩道守門對「範圍是什麼」給了不同答案。**
    `test_exception_handlers._REPO_ROOT_SCRIPTS` 列了四支 repo root 腳本，這裡只
    看得到三支，而兩條規則都沒有指名任何模組。族內不一致本身就是訊號，比逐檔盤點
    便宜得多。

    參數化是為了讓範圍**自己可以被測**：真實資料乾淨時，寬的範圍與窄的範圍測起來
    完全一樣，所以範圍需要自己的釘樁——餵一個 `tmp_path`，裡面放一個任何寫死清單都
    不可能包含的檔名。
    """
    package_dir = PKG_ROOT if pkg_root is None else pkg_root
    root_dir = REPO_ROOT if repo_root is None else repo_root
    # `test/` 照同一個判準過濾：`conftest.py` 在 2026-09-22 之前住在套件裡、在範圍內，
    # 搬家之後照舊。從 `root_dir` 推，範圍釘樁換掉 root 時它跟著換。
    package = [p for p in (*sorted(package_dir.glob("*.py")),
                           *sorted((root_dir / "test").glob("*.py")))
               if not p.stem.lstrip("_").startswith("test_")]
    root = [p for p in sorted(root_dir.glob("*.py"))
            if not p.stem.lstrip("_").startswith("test_")]
    return package + root


def _assert_scan_floor(sources) -> None:
    """掃到的檔案太少就當成範圍斷了，而不是「沒有違規」。

    抽成函式是為了讓下限**自己**可以被對照組測到：直接寫在測試裡的
    `assert len(...) >= _SCAN_FLOOR` 擋不住有人把常數改成 0（`x >= 0` 永遠真）。
    要分辨得出來，對照組得餵一份**介於兩個門檻之間**的語料——非空、但少於下限。
    """
    assert len(sources) >= _SCAN_FLOOR, (
        f"只掃到 {len(sources)} 個檔案，低於下限 {_SCAN_FLOOR}——"
        "範圍可能斷了。空的掃描跟乾淨的掃描長得一模一樣。")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def _functions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _is_os_name_nt_test(node: ast.expr) -> bool:
    """`os.name == "nt"`（或 `!=`／`in ("nt",)` 之類的變形一律不算——要就寫直白的）。"""
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    if not isinstance(node.ops[0], ast.Eq):
        return False
    left, right = node.left, node.comparators[0]
    return (isinstance(left, ast.Attribute) and left.attr == "name"
            and isinstance(left.value, ast.Name) and left.value.id == "os"
            and isinstance(right, ast.Constant) and right.value == "nt")


def _signal_zero_calls(tree: ast.Module) -> list[ast.Call]:
    """`os.kill(x, 0)` 的呼叫節點（字面 0；變數就抓不到，但這裡從來只寫字面）。"""
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "kill"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == 0):
            found.append(node)
    return found


def _windows_returns_before(func, lineno: int) -> bool:
    """函式裡有沒有「`if os.name == "nt": … return`」擋在 `lineno` 前面。"""
    for node in ast.walk(func):
        if not isinstance(node, ast.If) or not _is_os_name_nt_test(node.test):
            continue
        if (node.end_lineno or node.lineno) >= lineno:
            continue  # 分支結束在探測之後 → 擋不住它
        if any(isinstance(inner, ast.Return) for inner in ast.walk(node)):
            return True
    return False


def _enclosing_function(tree: ast.Module, lineno: int):
    best = None
    for func in _functions(tree):
        if func.lineno <= lineno <= (func.end_lineno or func.lineno):
            if best is None or func.lineno > best.lineno:
                best = func
    return best


def _pid_alive_definitions() -> dict[str, ast.FunctionDef]:
    """`{檔名: 該檔的 _pid_alive 定義}`。"""
    sources = _live_stack_sources()
    _assert_scan_floor(sources)
    found = {}
    for path in sources:
        for func in _functions(_parse(path)):
            if func.name == "_pid_alive":
                found[path.name] = func
    return found


_SOURCE_IDS = [p.name for p in _live_stack_sources()]


@pytest.mark.parametrize("source", _SOURCE_IDS)
def test_signal_zero_never_reaches_windows(source):
    """每個 ``os.kill(x, 0)`` 都要被 Windows 早退分支擋在前面。"""
    path = next(p for p in _live_stack_sources() if p.name == source)
    tree = _parse(path)
    for call in _signal_zero_calls(tree):
        func = _enclosing_function(tree, call.lineno)
        assert func is not None, (
            f"{source}:{call.lineno} 在模組層呼叫 os.kill(pid, 0)——"
            "Windows 上那不是探測，是對整個 console process group 送 Ctrl+C。")
        assert _windows_returns_before(func, call.lineno), (
            f"{source}:{call.lineno}（函式 `{func.name}`）的 os.kill(pid, 0) "
            "前面沒有 `if os.name == \"nt\": … return` 把 Windows 攔下來。"
            "Windows 上 signal 0 走的是 GenerateConsoleCtrlEvent，會把 pid 當成 "
            "console process group id：判定兩頭都錯，而且真的會送出 Ctrl+C 打斷"
            "共用 console 的批次。改用 psutil.pid_exists／`_nt_pid_alive`，"
            "POSIX 分支才留 os.kill。見 CLAUDE.md「Windows PID liveness」。")


def test_every_pid_alive_copy_has_a_windows_path():
    """每一份 `_pid_alive` 都要有 Windows 走得通的探測，不能只有 POSIX 那條。"""
    definitions = _pid_alive_definitions()
    assert definitions, "找不到任何 `_pid_alive`——這支測試的抓法過期了。"
    for name, func in sorted(definitions.items()):
        body = ast.unparse(func)
        has_probe = any(probe in body for probe in _WINDOWS_SAFE_PROBES)
        conservative = any(
            _is_os_name_nt_test(node.test)
            and any(isinstance(inner, ast.Return)
                    and isinstance(inner.value, ast.Constant)
                    and inner.value.value is True
                    for inner in ast.walk(node))
            for node in ast.walk(func) if isinstance(node, ast.If))
        assert has_probe or conservative, (
            f"{name} 的 `_pid_alive` 在 Windows 上沒有可用的探測路徑："
            f"既沒有走 {_WINDOWS_SAFE_PROBES} 之一，也沒有明確地保守回 True。"
            "只留 POSIX 的 os.kill 等於在本機（Windows）永遠回錯答案。")


def test_the_win32_fallback_pins_argtypes_and_restype():
    """ctypes 退路一定要自己釘型別——預設 restype 是 c_int，64-bit HANDLE 會截斷。

    截斷之後 handle 依然「不是 0」，所以 `if not handle` 攔不住，接著
    `GetExitCodeProcess` 對著垃圾 handle 失敗、被 `except Exception` 吞掉，
    整支探測靜靜地退化成「永遠回 True」。沒有任何症狀可以觀察。
    """
    checked = 0
    sources = _live_stack_sources()
    _assert_scan_floor(sources)
    for path in sources:
        tree = _parse(path)
        for func in _functions(tree):
            body = ast.unparse(func)
            if "OpenProcess" not in body:
                continue
            checked += 1
            for win_func in _CTYPES_FUNCS:
                if f".{win_func}(" not in body:
                    continue
                for attr in ("argtypes", "restype"):
                    assert f"{win_func}.{attr} =" in body, (
                        f"{path.name} 的 `{func.name}` 呼叫了 {win_func}，"
                        f"卻沒有指定 {win_func}.{attr}。ctypes 預設 restype 是 "
                        "c_int，64-bit HANDLE 會被截斷成負數而不是 0，"
                        "`if not handle` 因此攔不住，錯誤全被吞掉。"
                        "見 CLAUDE.md「Windows PID liveness」。")
    assert checked, "找不到任何 OpenProcess 退路——這支測試的抓法過期了。"


# 指標寬度的型別。`HANDLE` 在 64-bit 是 8 bytes；`c_int` 是 4。
_HANDLE_TYPES = {"wintypes.HANDLE", "ctypes.wintypes.HANDLE", "HANDLE",
                 "ctypes.c_void_p", "c_void_p"}

# 每一支 Win32 函式身上「HANDLE 出現在哪一格」。`restype` 是 `OpenProcess` 的
# **回傳**，`argtypes[0]` 是另外兩支的**輸入**——兩個方向都會截斷。
_HANDLE_SLOTS = {
    "OpenProcess": ("restype", None),
    "GetExitCodeProcess": ("argtypes", 0),
    "CloseHandle": ("argtypes", 0),
}


def _handle_type_problems(tree) -> list[str]:
    """回報「釘成了會截斷的型別」的每一格。乾淨就回空清單。"""
    problems = []
    for func in _functions(tree):
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Attribute):
                continue
            owner = target.value
            if not isinstance(owner, ast.Attribute):
                continue
            slot = _HANDLE_SLOTS.get(owner.attr)
            if slot is None or slot[0] != target.attr:
                continue
            value = node.value
            if slot[1] is not None:
                if not isinstance(value, (ast.Tuple, ast.List)) or not value.elts:
                    continue
                value = value.elts[slot[1]]
            spelled = ast.unparse(value)
            if spelled not in _HANDLE_TYPES:
                problems.append(
                    f"{func.name}：{owner.attr}.{target.attr} 釘成 {spelled}")
    return problems


def test_every_handle_type_is_pointer_wide():
    """**釘了**不等於**釘對了**——而釘錯的那一個正是規則自己列的缺陷。

    上面那支只檢查 `OpenProcess.restype =` 這串字在不在。把它釘成
    `ctypes.c_int`（也就是 ctypes 的預設、docstring 裡指名的那個）照樣通過，
    而那就是 64-bit HANDLE 被截斷的寫法本人。2026-09-20 實測：在樹上做這個變異，
    **整個檔案的測試一支都沒紅**——包括下面那些真的去探測行程的行為測試，因為這台
    機器的 handle 值小到截斷後數值不變。所以這一格只有靜態看得到，兩邊都不可省。
    """
    sources = _live_stack_sources()
    _assert_scan_floor(sources)
    problems = []
    for path in sources:
        problems += [f"{path.name} 的 {item}"
                     for item in _handle_type_problems(_parse(path))]
    assert not problems, (
        "HANDLE 被釘成不是指標寬度的型別：" + "；".join(problems) +
        "。64-bit 的 HANDLE 在 c_int 下會被截斷成負數而不是 0，"
        "`if not handle` 攔不住，錯誤全被吞掉。見 CLAUDE.md「Windows PID "
        "liveness」。")


@pytest.mark.parametrize("spelled, is_a_problem", [
    ("ctypes.c_int", True),                  # ctypes 的預設，正是那個缺陷
    ("ctypes.c_long", True),
    ("wintypes.DWORD", True),                # 32-bit，一樣截斷
    ("wintypes.HANDLE", False),
    ("ctypes.c_void_p", False),
])
def test_the_handle_width_check_can_tell_the_near_misses_apart(spelled,
                                                               is_a_problem):
    """樹上是乾淨的，所以回報違規那段程式碼在真實資料上永遠不執行。

    合成語料同時餵**該擋**與**該放**：只有該放的那幾格能殺掉「一律回報違規」
    這個變異，而只有該擋的那幾格能殺掉「什麼都不回報」。
    """
    source = (
        "def probe(pid):\n"
        "    kernel32.OpenProcess.argtypes = (wintypes.DWORD,)\n"
        f"    kernel32.OpenProcess.restype = {spelled}\n"
        f"    kernel32.GetExitCodeProcess.argtypes = ({spelled}, ptr)\n"
        "    return kernel32.OpenProcess(0x1000, False, pid)\n")
    problems = _handle_type_problems(ast.parse(source))
    assert bool(problems) is is_a_problem, f"{spelled} -> {problems}"
    if is_a_problem:
        # 兩格都要被點名（回傳一格、輸入一格），不是只抓到先撞見的那一個。
        assert len(problems) == 2, problems


def test_the_undecidable_answers_stay_the_way_the_doc_says():
    """三份副本對「判不出來」刻意給不同答案，這個分歧本身要被釘住。

    分歧活在 **POSIX** 那條 ``except OSError``（Windows 那條三份都是保守的 True）。
    有人「順手統一」時，這裡會紅，並要求他回去改 `CLAUDE.md` ——那份文件才是
    這條規則的正本。
    """
    definitions = _pid_alive_definitions()
    for name, expected in sorted(_UNDECIDABLE_POSIX_ANSWER.items()):
        func = definitions.get(name)
        assert func is not None, (
            f"{name} 不再定義 `_pid_alive`——CLAUDE.md 與這裡的列舉要一起更新。")
        answer = _oserror_answer(func)
        assert answer is expected, (
            f"{name} 的 `_pid_alive` 在 POSIX 判不出來（except OSError）時回 "
            f"{answer}，CLAUDE.md 寫的是 {expected}。這個分歧是刻意的："
            "問「我該不該拒絕啟動／讓位？」的要保守的 True，否則會樂觀地開出第二個 "
            "Chrome stack；要改就連 CLAUDE.md 一起改。")


def _oserror_answer(func):
    """`_pid_alive` 裡 ``except OSError`` 直接 return 的常數（找不到回 None）。"""
    for node in ast.walk(func):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = {n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)}
        if "OSError" not in names:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Return) and isinstance(
                    stmt.value, ast.Constant):
                return stmt.value.value
    return None


# ---------------------------------------------------------------------------
# `CLAUDE.md` 的兩份列舉 ↔ 上面那兩個常數
#
# 上面 `_DOCUMENTED_COPIES` 的註解寫著「要連同這裡與 `CLAUDE.md` 的列舉一起更新」。
# **那是一句請求，不是一道檢查**：那個常數是 `CLAUDE.md` 的**謄本**，而在這幾支出現
# 之前，謄本從來沒有跟正本比對過。下面那支
# `test_the_documented_copies_are_still_the_real_ones` 比的是
# 「謄本 ↔ 磁碟」，所以改了 `CLAUDE.md` 的那兩句話（或反過來，改了謄本卻沒改文件）
# 完全無聲——而 `CLAUDE.md` 是**唯一的規則來源**，冷啟動的 session 與 subagent 讀的
# 就是它，程式錯了會有測試叫，規則來源錯了沒有任何症狀。
#
# 特別值得守的是「判不出來回什麼」那一份：`CLAUDE.md` 記著這個分歧是**刻意**的，
# 並且告訴下一個人怎麼挑（問「我該不該讓位」的要保守的 True）。本檔已經有一支擋
# 「在程式裡把三份harmonise 掉」，但沒有任何東西擋「在文件裡把它 harmonise 掉」，
# 而那一份才是別人照著做的依據。
# ---------------------------------------------------------------------------
_DOC_COUNT_RE = re.compile(
    r"There are (\w+) `_pid_alive` copies —(.+?)— and a fourth", re.DOTALL)
_DOC_DIVERGE_RE = re.compile(
    r"\*\*The copies deliberately differ on the \"can't tell\" answer.+?"
    r"On Windows all three", re.DOTALL)
_WORD_TO_INT = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_BARE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def _claude_md() -> str:
    return (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def _backticked_module_names(chunk: str) -> list[str]:
    """一段文字裡、反引號包起來、看起來像模組名的東西（去掉 `.py`）。

    只收「純識別字」是有理由的：這兩段話裡同時有 `` `except OSError` `` 與
    `` `os.kill(pid, 0)` ``，它們帶空白與括號，會被這個條件濾掉。濾不掉的話
    `_process_control` 那一側會混進兩個假名字，而混進去的結果是**誤報**，
    然後最省事的修法是放寬比對——那會讓這組對帳失去意義。
    """
    found = []
    for token in re.findall(r"`([^`]+)`", chunk):
        token = token.strip()
        if token.endswith(".py"):
            token = token[:-3]
        if _BARE_IDENT_RE.match(token):
            found.append(token)
    return found


def _documented_copies() -> tuple[int | None, set[str]]:
    """CLAUDE.md 那句話裡的（數字詞, 檔名集合）。"""
    match = _DOC_COUNT_RE.search(_claude_md())
    if not match:
        return None, set()
    return (_WORD_TO_INT.get(match.group(1).lower()),
            set(_backticked_module_names(match.group(2))))


def _documented_undecidable() -> dict[str, bool]:
    """CLAUDE.md 記的「POSIX 判不出來時各自回什麼」。"""
    match = _DOC_DIVERGE_RE.search(_claude_md())
    if not match:
        return {}
    head, sep, rest = match.group(0).partition("**False**")
    if not sep:
        return {}
    middle, sep2, _tail = rest.partition("**True**")
    if not sep2:
        return {}
    answers = {name: False for name in _backticked_module_names(head)}
    answers.update({name: True for name in _backticked_module_names(middle)})
    return answers


def _stem(name: str) -> str:
    return name[:-3] if name.endswith(".py") else name


def test_the_claude_md_pid_alive_enumerations_are_extractable():
    """正對照：抽不到的話，下面每一支都退化成空集合比較，永遠通過。"""
    count, copies = _documented_copies()
    assert count is not None and len(copies) >= 3, (
        f"從 CLAUDE.md 抽到 count={count} copies={sorted(copies)}——"
        "「There are N `_pid_alive` copies — … — and a fourth」那句話的形狀變了，"
        "下面的對帳等於沒在比。")
    answers = _documented_undecidable()
    assert len(answers) >= 3 and set(answers.values()) == {True, False}, (
        f"從 CLAUDE.md 抽到的判不出來對照是 {answers}——那段話的形狀變了。"
        "兩個值都要出現，不然那個分歧已經在文件裡被 harmonise 掉了。")


def test_the_documented_copy_count_matches_its_own_list():
    """「There are **three**」的那個數字詞，要跟它自己列出來的份數一致。

    加第四份的時候，最容易漏的不是清單而是**數字詞**——改了清單、忘了 three，
    那句話就自相矛盾，而句子的前半（「有三份」）正是下一個人會記住的那半。
    """
    count, copies = _documented_copies()
    assert count == len(copies), (
        f"CLAUDE.md 說有 {count} 份 `_pid_alive`，但同一句話列了 "
        f"{len(copies)} 個：{sorted(copies)}。加減副本時兩邊要一起改。")


def test_the_claude_md_copy_list_matches_the_local_transcription():
    """CLAUDE.md 的三份 ↔ `_DOCUMENTED_COPIES`，兩個方向。"""
    _count, documented = _documented_copies()
    local = {_stem(name) for name in _DOCUMENTED_COPIES}
    assert local, "`_DOCUMENTED_COPIES` 是空的——下面等於沒在比。"
    missing = sorted(documented - local)
    stale = sorted(local - documented)
    assert not missing, (
        f"CLAUDE.md 列了這些 `_pid_alive` 副本，但本檔的 `_DOCUMENTED_COPIES` "
        f"沒有：{missing}。")
    assert not stale, (
        f"`_DOCUMENTED_COPIES` 有這些、CLAUDE.md 那句話沒列：{stale}。"
        "CLAUDE.md 是規則的正本，本檔只是謄本——兩邊要一起改。")


def test_the_claude_md_undecidable_answers_match_the_local_transcription():
    """「判不出來回什麼」的分歧，文件與謄本要逐一對得起來。

    ⚠️ 這一支守的是**文件那一側**被 harmonise 掉。本檔已經有一支擋「在程式裡把三份
    的答案統一」，但那支看的是程式；而下一個人寫第四份時，挑 True 還是 False 是照著
    `CLAUDE.md` 挑的。正本被改壞，程式的守門一個都不會叫。
    """
    documented = _documented_undecidable()
    local = {_stem(name): value
             for name, value in _UNDECIDABLE_POSIX_ANSWER.items()}
    assert local, "`_UNDECIDABLE_POSIX_ANSWER` 是空的——下面等於沒在比。"
    mismatched = sorted(
        f"{name}: CLAUDE.md={documented.get(name, '（沒列）')!r} "
        f"本檔={local.get(name, '（沒列）')!r}"
        for name in set(documented) | set(local)
        if documented.get(name, "missing") != local.get(name, "missing"))
    assert not mismatched, (
        "CLAUDE.md 與本檔對「POSIX 判不出來時回什麼」的記載對不起來：\n  "
        + "\n  ".join(mismatched)
        + "\n這個分歧是刻意的，判準是「錯了要往哪邊倒」；兩份記載不一致的時候，"
          "下一個人會照著錯的那一份寫第四份副本。")


def test_every_documented_pid_alive_copy_is_a_real_file():
    """CLAUDE.md 點名的每個副本都要是磁碟上真的有的模組。

    改名之後那句話會變成考古題，而規則來源出錯沒有任何症狀——上面那支「謄本 ↔
    磁碟」比得到改名，但如果謄本與文件**一起**沒改，比對的是兩份同樣過期的記載。
    """
    _count, copies = _documented_copies()
    ghosts = sorted(
        name for name in copies | set(_documented_undecidable())
        if not ((PKG_ROOT / f"{name}.py").exists()
                or (REPO_ROOT / f"{name}.py").exists()))
    assert not ghosts, (
        f"CLAUDE.md 的 `_pid_alive` 段落點名了這些模組，但磁碟上沒有：{ghosts}。")


def test_the_documented_copies_are_still_the_real_ones():
    """`CLAUDE.md` 列的三份要都還在，多出來的第四份也要補進文件。"""
    on_disk = set(_pid_alive_definitions())
    missing = sorted(_DOCUMENTED_COPIES - on_disk)
    assert not missing, (
        f"CLAUDE.md 列了這些 `_pid_alive` 副本，但檔案裡找不到：{missing}。"
        "重構掉一份是可以的，把文件的列舉一起改掉。")
    extra = sorted(on_disk - _DOCUMENTED_COPIES)
    assert not extra, (
        f"多了 `_pid_alive` 副本：{extra}。多一份是允許的（CLAUDE.md 自己寫了 "
        "「a fourth copy must too」），但要把它補進 CLAUDE.md 的列舉、"
        "本檔的 `_DOCUMENTED_COPIES` 與 `_UNDECIDABLE_POSIX_ANSWER`——"
        "「判不出來時回什麼」要當場選邊，不是抄一份就算。")


# ---------------------------------------------------------------------------
# 掃描範圍本身（2026-09-10 補）
#
# 這條規則的文字沒有指名任何模組，所以掃描也不該只看得到一部分 repo root。
# 加寬當天量到 0 筆違規——所以下面這三支釘的是**範圍**，不是違規。
# ---------------------------------------------------------------------------

def test_every_repo_root_script_is_scanned():
    """repo root 的每一支非測試 `.py` 都要在掃描範圍內。

    反向對帳：漏掉一支的症狀是**沒有症狀**——守門照跑、測試全綠，只是那個檔案
    從來沒被讀過。2026-09-10 之前漏的就是 `install_autostart.py`。
    """
    scanned = {p.name for p in _live_stack_sources()}
    on_disk = {p.name for p in REPO_ROOT.glob("*.py")
               if not p.stem.lstrip("_").startswith("test_")}
    missing = sorted(on_disk - scanned)
    assert not missing, (
        f"repo root 有 {len(missing)} 支腳本不在掃描範圍內：{missing}。"
        "這條規則管的是整個專案，不是被挑出來的幾支。")


def test_the_scope_is_derived_not_a_list_that_matches_today(tmp_path):
    """範圍必須是**算出來的**，不是剛好等於今天的答案。

    釘法：造一個真實清單不可能包含的檔名。寫死清單的版本會漏掉它，
    `glob("*.py")` 不會。
    """
    pkg = tmp_path / "pkg"
    root = tmp_path / "root"
    pkg.mkdir()
    root.mkdir()
    (pkg / "_thing.py").write_text("x = 1\n", encoding="utf-8")
    novel = root / "zz_no_hardcoded_list_would_contain_this.py"
    novel.write_text("y = 2\n", encoding="utf-8")
    # 舊範圍是 `start_*.py`；這個名字既不是 start_ 開頭也不是 run_batch.py
    assert not novel.name.startswith("start_")

    found = {p.name for p in _live_stack_sources(pkg_root=pkg, repo_root=root)}
    assert novel.name in found, (
        f"repo root 的新腳本沒有被掃到：{sorted(found)}。範圍是寫死的，"
        "不是從目錄推導的。")
    assert "_thing.py" in found, sorted(found)


def test_test_files_stay_out_of_the_scope(tmp_path):
    """反面：測試檔本身必須排除——它們可以合法地示範壞寫法。

    沒有這一格，把過濾拿掉會讓範圍「變大」而測試依然全綠，然後本檔自己就會
    被自己掃出違規。
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "test_demo.py").write_text("import os\nos.kill(1, 0)\n",
                                       encoding="utf-8")
    (root / "_test_helper.py").write_text("z = 3\n", encoding="utf-8")
    (root / "real.py").write_text("z = 4\n", encoding="utf-8")
    found = {p.name for p in _live_stack_sources(pkg_root=root, repo_root=root)}
    assert found == {"real.py"}, sorted(found)


def test_an_empty_derivation_yields_nothing_to_scan(tmp_path):
    """前提：空目錄真的會讓範圍變成空的（下面那支對照組的前提）。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not _live_stack_sources(pkg_root=empty, repo_root=empty)


@pytest.mark.parametrize("count,should_fire", [
    (0, True),                     # 空的
    (_SCAN_FLOOR - 1, True),       # 非空、但不夠——**這一格才釘得住常數本身**
    (_SCAN_FLOOR, False),          # 剛好夠
    (_SCAN_FLOOR + 5, False),      # 有餘裕
])
def test_the_scan_floor_fires_on_a_corpus_between_the_thresholds(
        count, should_fire):
    """族群下限的對照組，語料**跨過**門檻兩側。

    ⚠️ 只有空語料是不夠的：`0 >= 0` 與 `0 >= 25` 一個真一個假，所以空語料只證明
    得了「下限大於 0」。把 `_SCAN_FLOOR` 改成 0 之後，空語料那一格會直接翻面
    ——而中間那一格（非空但少於下限）才是唯一會抓到它的。本專案這一輪已經在四道
    下限上踩過同一個坑。
    """
    corpus = [Path(f"fake_{i}.py") for i in range(count)]
    if should_fire:
        with pytest.raises(AssertionError, match="低於下限"):
            _assert_scan_floor(corpus)
    else:
        _assert_scan_floor(corpus)


def test_the_real_scan_clears_its_own_floor():
    """正面：真實資料要真的超過下限，否則上面那組對照是在保護一個死條件。"""
    _assert_scan_floor(_live_stack_sources())


# 兩個真的會走遍全部原始碼的地方。加一個新的掃描就把它加進來——這份清單本身由
# 下面那支測試反向對帳（列了不存在的名字會紅）。
_GATED_SCANS = ("_pid_alive_definitions",
                "test_the_win32_fallback_pins_argtypes_and_restype")

_GATED_SCAN_FLOOR = 2


def _gating_errors(gated_names, by_name) -> list[str]:
    """`_GATED_SCANS` 與實際程式碼對不上的地方，一筆一句。

    **抽成純函式是為了讓這個比對自己有對照組。** 寫在測試裡的話，把
    `assert … in calls` 放寬成 `or True`、把死條目清單改成 `[]`、或把下限改成
    `>= 0`，全都**不會有任何測試變紅**——實測過，四個變異全部存活。真實資料是乾淨
    的，所以唯一能分辨的辦法是餵合成的壞輸入。
    """
    errors = []
    if len(gated_names) < _GATED_SCAN_FLOOR:
        errors.append(
            f"`_GATED_SCANS` 只剩 {len(gated_names)} 筆（下限 "
            f"{_GATED_SCAN_FLOOR}）。清單被清空的話反向對帳會空轉然後通過，"
            "等於沒有守門。")
    for name in gated_names:
        node = by_name.get(name)
        if node is None:
            errors.append(
                f"`_GATED_SCANS` 列了不存在的名字 `{name}`。改名之後這一筆會變成"
                "永遠對不上任何東西的字串，而守門照跑、測試全綠。")
            continue
        calls = {ast.unparse(call.func) for call in ast.walk(node)
                 if isinstance(call, ast.Call)}
        if "_assert_scan_floor" not in calls:
            errors.append(
                f"`{name}` 遍歷了全部原始碼卻沒有先過族群下限。"
                "範圍斷掉時它會掃 0 個檔案然後通過。")
        if "_live_stack_sources" not in calls:
            errors.append(
                f"`{name}` 不再遍歷原始碼了——它還該留在 `_GATED_SCANS` 裡嗎？")
    return errors


def _functions_by_name(source: str) -> dict:
    tree = ast.parse(source, "<gating>")
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)}


def test_every_real_scan_is_gated_by_the_floor():
    """會遍歷全部原始碼的地方，都必須先過 `_assert_scan_floor`。

    為什麼要單獨釘：把那一行拿掉**不會**讓任何測試變紅（下限自己還有對照組），
    但範圍斷掉時，紅的會是別支測試，而這幾支會**安靜地掃 0 個檔案然後通過**。
    實測過：拿掉之後 10 支全綠。這正是 §8.7 的形狀——有東西描述了規則，卻沒有
    任何東西執行它。
    """
    by_name = _functions_by_name(Path(__file__).read_text(encoding="utf-8"))
    errors = _gating_errors(_GATED_SCANS, by_name)
    assert not errors, "\n".join(errors)


_GATING_CASES = [
    ("清單被清空", (), "gated", "下限"),
    ("只剩一筆（非空但不夠——這一格才釘得住下限常數本身）",
     ("gated_a",), "gated", "下限"),
    ("列了不存在的名字", ("gated_a", "no_such_function"), "gated", "不存在"),
    ("有列到，但那支沒過閘", ("gated_a", "ungated"), "gated", "沒有先過族群下限"),
    ("有列到，但那支根本不掃原始碼了", ("gated_a", "no_scan"), "gated",
     "不再遍歷原始碼"),
]


@pytest.mark.parametrize("label,names,_unused,expected",
                         _GATING_CASES, ids=[c[0] for c in _GATING_CASES])
def test_the_gating_check_can_still_see_a_violation(label, names, _unused,
                                                    expected):
    """合成對照組：每一種違規都要被 `_gating_errors` 講出來。

    真實資料永遠是乾淨的，所以**正式那支測試無法證明這個比對還活著**——刪掉比對
    本身它照樣綠。這裡餵的是必定違規的輸入。
    """
    source = (
        "def gated_a():\n"
        "    sources = _live_stack_sources()\n"
        "    _assert_scan_floor(sources)\n"
        "def gated_b():\n"
        "    sources = _live_stack_sources()\n"
        "    _assert_scan_floor(sources)\n"
        "def ungated():\n"
        "    for p in _live_stack_sources():\n"
        "        pass\n"
        "def no_scan():\n"
        "    _assert_scan_floor([])\n")
    errors = _gating_errors(names, _functions_by_name(source))
    assert errors, f"{label}：這種違規沒有被抓到"
    assert any(expected in e for e in errors), (
        f"{label}：抓到了，但講的不是那件事：{errors}")


def test_the_gating_check_passes_on_a_clean_synthetic_corpus():
    """反面：上面那組不能是靠「永遠回錯誤」才綠的。"""
    source = (
        "def gated_a():\n"
        "    sources = _live_stack_sources()\n"
        "    _assert_scan_floor(sources)\n"
        "def gated_b():\n"
        "    sources = _live_stack_sources()\n"
        "    _assert_scan_floor(sources)\n")
    assert _gating_errors(("gated_a", "gated_b"),
                          _functions_by_name(source)) == []


# ---------------------------------------------------------------------------
# 「Windows 判斷」在這個專案裡只有一種拼法，而且測試替身必須換到同一個東西
# ---------------------------------------------------------------------------
#
# 兩條規則，一個共同的理由：**平台判斷是一個接縫，而接縫分岔的時候沒有症狀。**
#
# 2026-09-12 真的發生了一次，代價是四支假綠的測試：`discord_rpc._open_transport`
# 的述詞從 `sys.platform == "win32"` 改成 `os.name == "nt"`，而四支模擬 POSIX 的
# 測試還在 `monkeypatch.setattr(sys, "platform", "linux")`。替身換掉的接縫已經
# 不是正式碼在讀的那一個，於是 Windows 具名管道那條分支照跑、連上真的聊天桌面
# 程式、回傳一個真的 transport。**而且它會看人臉色**：桌面程式沒開的時候，那條
# 分支每一條管道都開失敗、乖乖回 None，四支全綠。同一份程式碼，答案取決於當下
# 桌面上開了什麼。
#
# 所以：
#   1. 正式碼判斷 Windows 一律寫 `os.name == "nt"`（`sys.platform` 只保留給
#      **別的**平台，例如 `== "darwin"`）。這也讓 `_is_os_name_nt_test` 那幾道
#      守門看得懂——它刻意只認這一種拼法。
#   2. 測試不准把 `sys.platform` 換成 Windows／POSIX 的值來模擬平台。沒有任何
#      正式碼的 Windows 分支在讀它，所以那種替身**保證**接不上。要模擬就換
#      `os.name`（正式碼真的在讀），或換一個具名接縫
#      （`discord_rpc._running_on_windows` 就是為此而生）。
#
# 兩條各自帶正面對照組：掃不到東西的掃描器，違規數永遠是 0。

# 拿來判「是不是 Windows／POSIX」的 `sys.platform` 值。`darwin` 不在裡面——
# 那是在判**別的**平台，是這條規則的合法例外（`discord_bot` 有一處）。
_PLATFORM_VALUES_MEANING_WINDOWS_OR_POSIX = frozenset({
    "win32", "win", "cygwin", "linux", "linux2", "posix",
})


def _platform_string_compares(tree: ast.Module) -> list[tuple[int, str]]:
    """`sys.platform` 拿來跟 Windows／POSIX 字串比對的地方。"""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            text = ast.unparse(node)
            if "sys.platform" not in text:
                continue
            for comparand in node.comparators:
                if (isinstance(comparand, ast.Constant)
                        and isinstance(comparand.value, str)
                        and comparand.value
                        in _PLATFORM_VALUES_MEANING_WINDOWS_OR_POSIX):
                    out.append((node.lineno, text))
        elif isinstance(node, ast.Call):
            text = ast.unparse(node)
            if not text.startswith("sys.platform.startswith"):
                continue
            for arg in node.args:
                if (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value
                        in _PLATFORM_VALUES_MEANING_WINDOWS_OR_POSIX):
                    out.append((node.lineno, text))
    return out


def _spelling_offenders(paths) -> list[str]:
    """這些檔案裡，用 `sys.platform` 判 Windows／POSIX 的地方。

    **抽成函式不是排版**：真資料現在是乾淨的，所以「組違規字串」那一行在真資料上
    一次都不會執行——把 `offenders.append(...)` 換成 `pass`，整組測試依然全綠
    （2026-09-12 變異測試實測 SURVIVED）。抽出來之後合成語料才走得到這條路。
    """
    offenders: list[str] = []
    for path in paths:
        for lineno, text in _platform_string_compares(_parse(path)):
            offenders.append(f"{path.name}:{lineno}  {text}")
    return offenders


def test_windows_is_detected_with_one_spelling_only():
    """正式碼判斷 Windows 只能寫 `os.name == "nt"`。

    不是風格潔癖：`test_pid_liveness` 的其他幾支守門（`_is_os_name_nt_test`）刻意
    只認這一種拼法，所以樹裡同時存在另一種看起來一樣正當的寫法，等於在邀請下一個
    人寫第四份 `_pid_alive` 時寫錯，然後撞上一個他看不懂的紅燈。
    """
    sources = _live_stack_sources()
    _assert_scan_floor(sources)
    offenders = _spelling_offenders(sources)
    assert not offenders, (
        "\n".join(offenders)
        + "\n\n本專案判斷 Windows 一律寫 `os.name == \"nt\"`。"
        "`sys.platform` 只保留給**別的**平台（例如 `== \"darwin\"`）。"
        "\n理由：其餘幾支守門只認得 `os.name == \"nt\"`，而兩種拼法並存過一次，"
        "代價是四支假綠的測試（見本段最上面的說明）。")


def _platform_patch_offenders(paths) -> list[str]:
    """這些測試檔裡，把 `sys.platform` 換成 Windows／POSIX 值當替身的地方。

    抽出來的理由與 `_spelling_offenders` 相同：真資料乾淨 ⇒ 收集那一行跑不到。
    """
    offenders: list[str] = []
    for path in paths:
        for node in ast.walk(_parse(path)):
            if not (isinstance(node, ast.Call)
                    and "setattr" in ast.unparse(node.func)):
                continue
            text = ast.unparse(node)
            if "sys, 'platform'" not in text and 'sys, "platform"' not in text:
                continue
            for arg in node.args:
                if (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)
                        and arg.value
                        in _PLATFORM_VALUES_MEANING_WINDOWS_OR_POSIX):
                    offenders.append(f"{path.name}:{node.lineno}  {text[:90]}")
    return offenders


def test_no_test_fakes_a_platform_by_patching_sys_platform():
    """測試不准用 `sys.platform` 當平台替身——沒有正式碼在讀它。

    這一條是上一條的另一半，也是 2026-09-12 那次真正造成損害的那一半：拼法統一
    之後，任何還在換 `sys.platform` 的替身都**保證**接不到正式碼，而症狀是
    「測試全綠、實際上跑的是另一條分支」。
    """
    test_files = sorted((REPO_ROOT / "test").glob("test_*.py"))
    assert len(test_files) >= 20, (
        f"只找到 {len(test_files)} 個測試檔——掃描範圍壞了，違規數會永遠是 0。")
    offenders = _platform_patch_offenders(test_files)
    assert not offenders, (
        "\n".join(offenders)
        + "\n\n沒有任何正式碼的 Windows 分支在讀 `sys.platform`，所以這個替身"
        "接不到被測的那條 `if`——測試會全綠，跑的卻是另一條分支。"
        "\n要模擬平台就換 `os.name`（正式碼真的在讀），或請正式碼開一個具名接縫"
        "再換那一支（範例：`discord_rpc._running_on_windows`）。")


def test_the_platform_spelling_scanners_actually_see_something():
    """兩支的正面對照組：合成語料要抓得到、合法案例要放行。

    真資料現在是乾淨的，所以上面兩支的「組訊息」那幾行在真資料上一次都不會執行
    ——沒有這一支的話，把判準整個掏空也不會有人發現。
    """
    caught = _platform_string_compares(ast.parse(
        'import sys\nif sys.platform == "win32":\n    pass\n'))
    assert caught, "`sys.platform == \"win32\"` 應該被抓到"
    caught = _platform_string_compares(ast.parse(
        'import sys\nif sys.platform.startswith("win"):\n    pass\n'))
    assert caught, "`sys.platform.startswith(\"win\")` 應該被抓到"

    # 必放行：判的是**別的**平台。這一筆才是唯一殺得死「把整個 sys.platform
    # 都當違規」那種過寬寫法的案例。
    allowed = _platform_string_compares(ast.parse(
        'import sys\nif sys.platform == "darwin":\n    pass\n'))
    assert not allowed, (
        "`sys.platform == \"darwin\"` 判的是別的平台，是合法的，不該被抓。")
    assert "darwin" not in _PLATFORM_VALUES_MEANING_WINDOWS_OR_POSIX

    # 真資料的正面對照：專案裡確實還有那一處 darwin，而它必須是綠的。
    bot = REPO_ROOT / "axiomatic" / "discord_bot.py"
    assert "darwin" in bot.read_text(encoding="utf-8"), (
        "`discord_bot.py` 裡那個 darwin 分支不見了——這支測試的必放行案例"
        "就不再有真資料撐著，請確認是刻意移除的。")


def test_the_platform_offender_collectors_run_on_a_seeded_violation(tmp_path):
    """把違規**種進去**，確認兩支收集器真的會收集。

    這是上面兩支唯一的執行證據。真資料現在是乾淨的，所以「把違規組成字串」那一行
    在真資料上一次都不會執行——實測把 `offenders.append(...)` 換成 `pass`，整組
    測試依然全綠。本專案對這個形狀的判語：**不要用 grep 去找違規的形狀，把違規
    注入進去**。
    """
    bad_src = tmp_path / "seeded_source.py"
    bad_src.write_text(
        'import sys\n'
        'def probe():\n'
        '    if sys.platform == "win32":\n'
        '        return 1\n'
        '    return 0\n', encoding="utf-8")
    got = _spelling_offenders([bad_src])
    assert got and "seeded_source.py" in got[0], (
        f"種進去的違規沒有被收集：{got}")

    bad_test = tmp_path / "test_seeded.py"
    bad_test.write_text(
        'import sys\n'
        'def test_x(monkeypatch):\n'
        '    monkeypatch.setattr(sys, "platform", "linux")\n', encoding="utf-8")
    got = _platform_patch_offenders([bad_test])
    assert got and "test_seeded.py" in got[0], (
        f"種進去的替身違規沒有被收集：{got}")

    # 必放行的那一半，一樣要種真的檔案進去測——只測必抓的話，一個「什麼都算違規」
    # 的收集器也會全綠。
    ok_src = tmp_path / "ok_source.py"
    ok_src.write_text(
        'import os, sys\n'
        'def probe():\n'
        '    if os.name == "nt":\n'
        '        return 1\n'
        '    if sys.platform == "darwin":\n'
        '        return 2\n'
        '    return 0\n', encoding="utf-8")
    assert _spelling_offenders([ok_src]) == [], (
        "`os.name == \"nt\"` 與 `sys.platform == \"darwin\"` 都是合法的，"
        "不該被收集。")

    ok_test = tmp_path / "test_ok.py"
    ok_test.write_text(
        'import os\n'
        'def test_x(monkeypatch):\n'
        '    monkeypatch.setattr(os, "name", "posix")\n', encoding="utf-8")
    assert _platform_patch_offenders([ok_test]) == [], (
        "換 `os.name` 是被允許的——正式碼真的在讀它。")


# ===========================================================================
# 第五道：行為上的對帳——靜態檢查看不到「答案是不是對的」
#
# 只有一個 **False** 能證明這條路整條走通了：退路裡每一個 `except` 都回 True
# （保守），所以「回 True」跟「整支壞掉」長得一模一樣。
# ===========================================================================

class _no_psutil:
    """讓函式內的 `import psutil` 丟 ImportError。

    `sys.modules[name] = None` 是 CPython 的既定行為：import 機制看到 None 就丟
    `ImportError`。**不可以改成 `sys.modules.pop("psutil")`** ——那不是模擬缺席，
    那是叫它重新 import 一份真的回來。

    （同一個做法在 `test_process_control.py` 也有一份；那邊測的是它自己那份抄本，
    這裡測的是「每一份抄本」，所以兩邊各留各的，不互相 import。）
    """

    def __enter__(self):
        self._saved = sys.modules.get("psutil", "__absent__")
        sys.modules["psutil"] = None
        return self

    def __exit__(self, *_exc):
        if self._saved == "__absent__":
            sys.modules.pop("psutil", None)
        else:
            sys.modules["psutil"] = self._saved
        return False


def _a_definitely_dead_pid() -> int:
    """起一個立刻結束的子行程，回它的 pid（已確認收屍完畢）。"""
    import subprocess
    # `subprocess.run` 回的是 `CompletedProcess`，**沒有 `.pid`**——要 pid 就得
    # 自己拿 `Popen`。`wait()` 之後子行程已經被收屍，pid 不再對應任何行程。
    proc = subprocess.Popen([sys.executable, "-c", "pass"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proc.wait(timeout=30)
    return proc.pid


class _exited_but_still_held:
    """一個**已經結束、但 handle 還被握著**的子行程的 pid。

    Windows 要等最後一個 handle 關掉才放掉那個 pid，所以這段期間 `OpenProcess`
    **會成功**（實測 handle=448、`GetLastError()`=0），而 `GetExitCodeProcess` 回
    的是真正的結束碼 `0`——也就是 `code.value == 259`（`STILL_ACTIVE`）那一格
    **唯一**走得到的輸入。

    `_a_definitely_dead_pid()` 走不到它：那裡的 `Popen` 一出函式就被回收、handle
    跟著關掉，於是 `OpenProcess` 失敗，提早從 `if not handle` 那條路回去。變異測試
    就是這樣抓到的——把 `return code.value == 259` 改成 `return True` 活了下來，
    因為整個語料**沒有一個輸入走得到那一行**。

    而這不是人造的邊角案例，反而是最常見的那一個：supervisor 手上握著 `Popen`
    問「它還活著嗎」就正好是這個狀態。
    """

    def __enter__(self) -> int:
        import subprocess
        self._proc = subprocess.Popen([sys.executable, "-c", "pass"],
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        self._proc.wait(timeout=30)
        return self._proc.pid

    def __exit__(self, *_exc):
        self._proc = None       # 放掉 handle，pid 這才真的可以被回收
        return False


def _ctypes_probes() -> dict:
    """**推導**出「每一份有 ctypes 退路的抄本 → 它的探測函式」。

    來源是 `_DOCUMENTED_COPIES`，不是手寫清單——第四份抄本一加進那個集合，下面
    每一支行為測試就自動蓋到它。`_chrome_slot.py` 會自然落選（它刻意沒有 ctypes
    退路，沒有 psutil 就乾脆不探測），而「它落選」本身由下面那支測試釘住，免得
    哪天推導壞掉、集合變空，而每一支測試都綠。
    """
    import importlib

    probes = {}
    for name in sorted(_DOCUMENTED_COPIES):
        module = importlib.import_module(Path(name).stem)
        probe = getattr(module, "_nt_pid_alive", None)
        if probe is not None:
            probes[name] = probe
    return probes


_CTYPES_COPIES = sorted(_ctypes_probes())

# 這個檔案裡每一支行為測試都要的兩件事，合成一個標記：Windows 專用，而且要有
# psutil 當對照組（它是必要相依，缺了的話對照組本身就不存在）。
_needs_windows = pytest.mark.skipif(os.name != "nt",
                                    reason="ctypes 退路是 Windows 專用的")


def test_the_behavioural_corpus_is_derived_and_not_empty():
    """對照組的自我檢查：空集合跟「每一份都通過」長得一模一樣。

    順便釘住 `_chrome_slot` 的**缺席是有理由的**，不是推導漏抓：`CLAUDE.md` 明寫
    它刻意不走 ctypes，沒有 psutil 就保守回 True、把 staleness 交給時間 backstop。
    """
    probes = _ctypes_probes()
    assert len(probes) >= 2, f"推導只找到 {sorted(probes)}——對照組是空的"
    assert "_chrome_slot.py" not in probes, (
        "`_chrome_slot` 長出 ctypes 退路了——那是刻意不做的決定，改了要先改 "
        "`CLAUDE.md`")
    assert set(probes) <= _DOCUMENTED_COPIES


@_needs_windows
@pytest.mark.parametrize("copy_name", _CTYPES_COPIES)
def test_every_ctypes_probe_sees_a_live_process(copy_name):
    with _no_psutil():
        assert _ctypes_probes()[copy_name](os.getpid()) is True


@_needs_windows
@pytest.mark.parametrize("copy_name", _CTYPES_COPIES)
def test_every_ctypes_probe_sees_a_dead_process(copy_name):
    """**這是唯一會回 False 的那條路，也是唯一能證明它真的在運作的結果。**

    規則列的頭號失敗模式是 `restype` 沒釘、HANDLE 被截斷——而截斷後的 handle
    照樣不是 0，所以 `if not handle` 攔不到，`GetExitCodeProcess` 對著垃圾 handle
    失敗，然後 `return True`。形狀完全正確，答案永遠是「活著」。只有這支會紅。
    """
    import psutil

    dead = _a_definitely_dead_pid()
    if psutil.pid_exists(dead):
        pytest.skip("pid 被回收了，這次測不到")
    with _no_psutil():
        assert _ctypes_probes()[copy_name](dead) is False


@_needs_windows
def test_every_ctypes_probe_agrees_with_psutil_and_with_each_other():
    """一份共用的語料跑過每一份抄本。

    各自都有一套綠測試，不等於有人比對過它們——這支就是那個比對。語料刻意包含
    「活著」「已死」與（若有）一個**查詢權限不足**的系統行程：最後那個是唯一會走
    到 `ERROR_ACCESS_DENIED` 特判的輸入，而那條特判錯了會把系統行程讀成已死。
    """
    import psutil

    dead = _a_definitely_dead_pid()
    if psutil.pid_exists(dead):
        pytest.skip("pid 被回收了，這次測不到")
    corpus = [os.getpid(), dead]
    if psutil.pid_exists(4):
        corpus.append(4)          # Windows 的 System 行程，查不到細節但活著
    probes = _ctypes_probes()
    for pid in corpus:
        expected = psutil.pid_exists(pid)
        with _no_psutil():
            answers = {name: probe(pid) for name, probe in probes.items()}
        assert set(answers.values()) == {expected}, (
            f"pid={pid}：psutil 說 {expected}，而各抄本說 {answers}")


@_needs_windows
def test_no_ctypes_probe_ever_signals_anything(monkeypatch):
    """探測是**查詢**，不是操作。`os.kill` 一次都不准被叫到。

    這一條在這個專案裡不是潔癖：被探測的 pid 來自 `webrunner.pid`，而那條鏈跟
    正在跑的批次**共用同一個 console**，送出去的 Ctrl+C 會直接打斷產圖。
    """
    called = []
    monkeypatch.setattr(os, "kill", lambda *args, **kwargs: called.append(args))
    dead = _a_definitely_dead_pid()
    with _no_psutil():
        for probe in _ctypes_probes().values():
            probe(os.getpid())
            probe(dead)
    assert called == [], f"探測對 pid 送了訊號：{called}"


@_needs_windows
@pytest.mark.parametrize("copy_name", _CTYPES_COPIES)
def test_a_broken_ctypes_reads_as_alive(copy_name, monkeypatch):
    """兩條路都不通時要保守回 True。

    這裡是「該不該讓位／該不該重啟」的答案來源，猜錯的方向要選在「以為還活著」
    ——那只是這次不動作；反過來會開出第二套 Chrome stack 疊在同一個 profile 上。
    """
    import ctypes

    def no_kernel32(*_args, **_kwargs):
        raise OSError("no kernel32")

    monkeypatch.setattr(ctypes, "WinDLL", no_kernel32)
    dead = _a_definitely_dead_pid()
    with _no_psutil():
        assert _ctypes_probes()[copy_name](dead) is True


class _Kernel32WhoseExitCodeQueryFails:
    """`OpenProcess` 成功、`GetExitCodeProcess` 失敗的 kernel32 替身。

    真的 kernel32 在這台機器上叫不出這個組合：拿得到 handle，查結束碼就會成功。
    但它是 API 合約裡的一種回傳（handle 權限不足、行程正在被拆掉），而這一格的答案
    決定「讓位還是重啟」，所以用替身釘住。
    """

    class _Func:
        def __init__(self, result, calls, name):
            self.result, self.calls, self.name = result, calls, name
            self.argtypes = self.restype = None

        def __call__(self, *args):
            self.calls.append(self.name)
            return self.result

    def __init__(self, *_args, **_kwargs):
        self.calls: list = []
        self.OpenProcess = self._Func(0x4D2, self.calls, "open")
        self.GetExitCodeProcess = self._Func(0, self.calls, "exit_code")
        self.CloseHandle = self._Func(1, self.calls, "close")


@_needs_windows
@pytest.mark.parametrize("copy_name", _CTYPES_COPIES)
def test_an_unreadable_exit_code_reads_as_alive_and_closes_the_handle(copy_name,
                                                                      monkeypatch):
    """查不到結束碼＝無法判定，要落在保守的「活著」（理由同上一支）；handle 已經開了，
    不管答案是什麼都要關掉——這支每幾秒就被叫一次，漏關的 handle 會一路累積。"""
    import ctypes

    kernel32 = _Kernel32WhoseExitCodeQueryFails()
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel32)
    with _no_psutil():
        assert _ctypes_probes()[copy_name](os.getpid()) is True
    assert kernel32.calls == ["open", "exit_code", "close"], kernel32.calls


@_needs_windows
@pytest.mark.parametrize("copy_name", sorted(_DOCUMENTED_COPIES))
def test_every_copy_asks_psutil_first(copy_name):
    """psutil 是正路，ctypes 只是退路——三份都要照這個順序。

    量的方式是讓 psutil 說一個**與事實相反**的答案：回傳被採信就證明它真的被問
    了。單純比對「答案正確」證明不了順序，因為兩條路平常會給出同一個答案。
    """
    import importlib

    import psutil

    module = importlib.import_module(Path(copy_name).stem)
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(psutil, "pid_exists", lambda _pid: False)
        assert module._pid_alive(os.getpid()) is False, (
            f"{copy_name}：psutil 說已死卻沒被採信——它不是第一順位")
    finally:
        monkey.undo()


@_needs_windows
@pytest.mark.parametrize("copy_name", _CTYPES_COPIES)
def test_every_ctypes_probe_reads_the_exit_code_of_a_process_still_held(
        copy_name):
    """已經結束、但 handle 還被握著——`STILL_ACTIVE` 那一格唯一走得到的輸入。

    這支不是為了多一個案例，是為了**讓那一行第一次被執行**：少了它，把
    `return code.value == 259` 改寫成 `return True` 的變異會活下來，而那個變異的
    意思是「任何開得起 handle 的 pid 都算活著」——supervisor 永遠不重啟。
    """
    with _exited_but_still_held() as pid:
        with _no_psutil():
            assert _ctypes_probes()[copy_name](pid) is False


@_needs_windows
@pytest.mark.parametrize("copy_name", sorted(_DOCUMENTED_COPIES))
def test_without_psutil_each_copy_still_answers_the_documented_way(copy_name):
    """把對照組整個拿掉，問的是 `_pid_alive` 自己（不是 `_nt_pid_alive`）。

    上面那支 `test_every_copy_asks_psutil_first` 只證明 psutil 是第一順位；psutil
    在場時，後面整條 Windows 分支都不會執行，所以「退路接得對不對」得另外問。
    分歧是**寫在 `CLAUDE.md` 裡的**：有 ctypes 退路的要答得出 False，而
    `_chrome_slot` 刻意不探測、保守回 True，把 staleness 交給時間 backstop。
    """
    import importlib

    module = importlib.import_module(Path(copy_name).stem)
    has_ctypes = getattr(module, "_nt_pid_alive", None) is not None
    with _exited_but_still_held() as pid:
        with _no_psutil():
            if copy_name == "_chrome_slot.py":
                # 這一份的 psutil 是模組層變數，擋 import 擋不到它。
                monkey = pytest.MonkeyPatch()
                try:
                    monkey.setattr(module, "psutil", None)
                    answer = module._pid_alive(pid)
                finally:
                    monkey.undo()
            else:
                answer = module._pid_alive(pid)
    assert answer is (False if has_ctypes else True), (
        f"{copy_name}：沒有 psutil 時答 {answer}，與 `CLAUDE.md` 記的不同")
