"""會殺人的行程掃描，必須先把**自己**排除掉。

這個 repo 有十五個地方走 `psutil.process_iter`。其中七個的結果最後會被 `kill()`
／`terminate()`／`taskkill`，而那七個都寫了一行「`pid == os.getpid()` 就跳過」。
另外八個是唯讀的（presence 探測、健康報告的元件版本、最小化視窗），沒有那一行也
沒關係。

**這個分類在 2026-09-21 之前完全沒有人在維護**，而它的兩個方向各有各的失效方式：

* 新增一個會殺人的掃描卻忘了自我排除 → 那支掃描有機會砍掉**執行它的那個行程**。
  今天多數副本靠名稱白名單擋著（我們是 python 行程，過不了 `chrome.exe` 那關），
  所以那一行是**為了將來有人放寬白名單**而存在的——也就是說它壞掉的那天不會有
  任何症狀，直到真的出事。
* 分類表留著一個已經改名／刪掉的函式 → 一筆永遠不會命中的登記，而表看起來還在
  維護。跟 `_OWNER_ONLY_SLASH` 同一個形狀。

分支覆蓋率量出來的起點：`_process_control._find_all_chrome_processes` 與
`find_launcher_pids` 這兩支的自我排除**從來沒有被任何測試走過**——而同一條規則在
兩個 webrunner 變體的 `_kill_orphan_chrome` 上早就有一支專門的測試，連 docstring
都寫了「它守的是將來有人放寬名稱白名單那一天」。同一條規則、四份實作、只有一份
被驗過。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PACKAGE_ROOT.parent
sys.path.insert(0, str(PACKAGE_ROOT))

import _process_control as pc  # noqa: E402
from test_process_control import _FakeProc, _install  # noqa: E402

# `other_launcher_pids` 把自我排除做成可注入的參數，所以參數名也算一個來源。
_SELF_PID_PARAM = "self_pid"


def _self_pid_names(node: ast.AST) -> set[str]:
    """這支函式裡，哪些名字裝著「我自己的 pid」？

    兩個來源：`x = os.getpid()` 綁出來的名字，以及叫 `self_pid` 的參數。
    """
    names: set[str] = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                and isinstance(sub.targets[0], ast.Name)
                and isinstance(sub.value, ast.Call)
                and getattr(sub.value.func, "attr", "") == "getpid"):
            names.add(sub.targets[0].id)
    args = getattr(node, "args", None)
    if args is not None:
        every = list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs)
        if any(a.arg == _SELF_PID_PARAM for a in every):
            names.add(_SELF_PID_PARAM)
    return names


def _has_self_pid_comparison(node: ast.AST) -> bool:
    """真的拿某個 pid 去跟「我自己的 pid」比對了嗎？

    ⚠️ **不可以只問字串在不在。** 第一版是 `"own_pid" in ast.unparse(node)`，而
    變異實測：把 `own_pid = os.getpid()` 那一行刪掉（`if pid == own_pid` 留著，
    執行期會是 `NameError`）**照樣全綠**——因為那個字串還在下面那一行裡。這正是
    本 repo 記過的「用 AST 而不是子字串」與「守門可以被死掉的儀式滿足」。
    """
    bound = _self_pid_names(node)
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Compare):
            continue
        for side in [sub.left] + list(sub.comparators):
            if isinstance(side, ast.Name) and side.id in bound:
                return True
            if (isinstance(side, ast.Call)
                    and getattr(side.func, "attr", "") == "getpid"):
                return True
    return False


# ---------------------------------------------------------------------------
# 推導：誰在走 `process_iter`
# ---------------------------------------------------------------------------

def _trees() -> list[tuple[Path, ast.Module]]:
    out = []
    # `test/`（本檔所在的目錄）照同一個判準過濾：`conftest.py` 在 2026-09-22 之前住在
    # 套件裡、在範圍內，搬家之後照舊。
    sources = [p for p in (*sorted(PACKAGE_ROOT.glob("*.py")),
                           *sorted(Path(__file__).resolve().parent.glob("*.py")))
               if not p.name.startswith(("test_", "_test_"))]
    sources += sorted(REPO_ROOT.glob("*.py"))
    for path in sources:
        try:
            out.append((path, ast.parse(path.read_text(encoding="utf-8"),
                                        str(path))))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
    return out


def _scanners_in(tree: ast.Module) -> dict[str, ast.AST]:
    """走 `process_iter` 的**最外層**函式。

    巢狀的 helper（`find_stale_components` 裡的 `_iter`、`other_launcher_pids` 裡
    的同名那支）算在包著它的那支頭上：分類與自我排除是整支函式的性質，把內層單獨
    列出來只會讓分類表多兩筆沒有意義的東西。
    """
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and getattr(sub.func, "attr", "") == "process_iter"):
                found[node.name] = node
                break
    return found


def _all_scanners() -> dict[tuple[str, str], ast.AST]:
    out: dict[tuple[str, str], ast.AST] = {}
    for path, tree in _trees():
        for name, node in _scanners_in(tree).items():
            out[(path.name, name)] = node
    return out


# 掃到的 pid 最後會被殺掉——必須自我排除。
_KILLS_WHAT_IT_FINDS = {
    ("_process_control.py", "_find_all_chrome_processes"):
        "結果餵給 `_kill_chrome_pids`／taskkill 後援。",
    ("_process_control.py", "_find_all_webrunner_pids"):
        "結果餵給 `_terminate_all_webrunner_instances`。",
    ("_process_control.py", "find_launcher_pids"):
        "結果餵給停止監督者的路徑；被掃的是 **python** 行程，而做這件事的也是"
        "python 行程——這一支的自我排除不是防禦性的。",
    ("_supervisor.py", "other_launcher_pids"):
        "啟動器用它決定「有沒有別人在跑」，重複的那個會被收掉。自我排除做成"
        "`self_pid` 參數，因為呼叫端有時要排除的不是自己。",
    ("discord_bot.py", "cmd_kill"):
        "`/proc kill` 的名稱比對，直接殺。",
    ("webrunner_novelai.py", "_kill_orphan_chrome"):
        "孤兒 Chrome 清掃。",
    ("webrunner_je_only.py", "_kill_orphan_chrome"):
        "同上，另一個變體。",
}

# 唯讀掃描：結果只用來顯示或判斷，不會有人被殺。每一筆都要寫理由。
_READ_ONLY_SCANS = {
    ("_process_control.py", "find_stale_components"):
        "健康報告用的「跑著的元件比磁碟上的程式碼舊嗎」，只回報不動手。",
    ("_webrunner_shared.py", "find_browser_pids_for_profile"):
        "只餵給 `hide_browser_windows`——把視窗搬到螢幕外，不是殺行程。",
    ("presence_probe.py", "probe_priority_game"):
        "presence 用：比對 exe 名稱決定要顯示哪個遊戲，只讀不動手。",
    ("presence_probe.py", "probe_game_process"):
        "presence 用：同一族的次要比對，一樣只讀 exe 名稱。",
    ("presence_probe.py", "list_running_processes"):
        "診斷用的行程列表，端給人看，沒有任何呼叫端拿它去殺東西。",
    ("presence_probe.py", "probe_claude_code"):
        "presence 用，偵測開發工具。",
}

_CLASSIFIED = set(_KILLS_WHAT_IT_FINDS) | set(_READ_ONLY_SCANS)


def _scans_without_self_exclusion(scanners: dict, must_exclude: set) -> list:
    """會殺人的掃描裡，哪幾支看不到自我排除？

    抽成一支的理由跟本 repo 其他守門一樣：樹是乾淨的，所以這段判斷在真實資料上
    永遠回空 list，整段刪掉照樣全綠。底下用合成語料驗它真的會開火。
    """
    missing = []
    for key in sorted(must_exclude):
        node = scanners.get(key)
        if node is None:
            continue                     # 不存在的由 stale 那支去報
        if not _has_self_pid_comparison(node):
            missing.append(key)
    return missing


def test_every_process_scan_is_classified():
    """掃得到的每一支都要登記在兩份清單的其中一份。

    新增一支會殺人的掃描而沒有分類就會紅，那正是這支存在的理由——分類這個動作
    本身逼人回答「我掃到的東西會不會被殺」。
    """
    scanners = _all_scanners()
    assert len(scanners) >= 12, (
        f"只掃到 {len(scanners)} 支走 `process_iter` 的函式（{sorted(scanners)}）"
        "——推導失效時這支會變成空的比較，看起來跟通過一模一樣。")
    unclassified = sorted(set(scanners) - _CLASSIFIED)
    assert not unclassified, (
        f"這些函式走了 `psutil.process_iter`，但沒有被分類：{unclassified}。\n"
        "掃到的 pid 會被殺 → 加進 `_KILLS_WHAT_IT_FINDS`（並確認它有自我排除）；"
        "只是讀來看的 → 加進 `_READ_ONLY_SCANS` 並寫下理由。")


def _stale_entries(classified: set, scanners: set) -> list:
    """登記了卻已經不走 `process_iter` 的那幾筆。

    抽成一支：樹是乾淨的，所以這個差集在真實資料上永遠是空的，整行刪掉照樣全綠
    （變異實測，第一版就是這樣活下來的）。
    """
    return sorted(classified - scanners)


def test_the_stale_check_actually_fires():
    """對照組：改過名的登記要被抓到，還在的不能被誤報。"""
    scanners = {("a.py", "still_here")}
    classified = {("a.py", "still_here"), ("a.py", "was_renamed")}
    assert _stale_entries(classified, scanners) == [("a.py", "was_renamed")]
    assert _stale_entries(scanners, scanners) == []


def test_the_classification_has_no_stale_entries():
    """反方向：清單裡不得留下已經改名或刪掉的函式。"""
    scanners = set(_all_scanners())
    stale = _stale_entries(_CLASSIFIED, scanners)
    assert not stale, (
        f"這些登記在案的掃描已經不走 `process_iter` 了：{stale}。"
        "從清單裡拿掉，或把掃描補回去。")


def _thin_reasons(registry: dict) -> list:
    """理由欄太短的那幾筆。抽成一支才有辦法在合成語料上驗它會開火。"""
    return sorted(key for key, why in registry.items()
                  if len(why.strip()) < 10)


def test_every_read_only_scan_states_its_reason():
    """唯讀那一份的理由欄不得留白——沒有理由的豁免，下一個人只會照抄。

    這道下限第一次跑就抓到我自己寫的兩筆（「同上。」與「診斷用的列表。」），
    而「同上」正是最會爛掉的那種寫法：它指的那一筆被改掉之後，它就什麼都沒說了。
    """
    thin = _thin_reasons(_READ_ONLY_SCANS)
    assert not thin, f"這些唯讀掃描沒有寫理由：{thin}"


def test_the_reason_floor_actually_fires():
    """對照組：樹上已經沒有太短的理由了，所以這道下限在真實資料上不會執行。"""
    assert _thin_reasons({("a.py", "f"): "同上。"}) == [("a.py", "f")]
    assert _thin_reasons({("a.py", "f"): "只讀 exe 名稱，沒有人拿它去殺東西。"}) == []


def test_every_killing_scan_excludes_itself():
    """會殺人的掃描，原始碼裡要看得到自我排除。

    靜態這一半守的是「有沒有那一行」；底下的行為測試守的是「那一行真的有效」。
    兩半都要，理由與 `test_pid_liveness` 的靜態／行為兩半相同：名稱白名單今天會
    把我們自己擋在外面，所以拿掉自我排除**行為上也可能看不出來**，而放寬白名單
    的那一天就會出事。
    """
    missing = _scans_without_self_exclusion(_all_scanners(),
                                            set(_KILLS_WHAT_IT_FINDS))
    assert not missing, (
        f"這些會殺人的掃描看不到自我排除：{missing}。"
        "要有一個裝著自己 pid 的名字（`x = os.getpid()`，或名為 `self_pid` 的"
        "參數），而且真的拿它去比對。")


# ---------------------------------------------------------------------------
# 行為：真的餵自己的 pid 進去
# ---------------------------------------------------------------------------

_OURS = 424242
_OTHER = 424243


def test_the_chrome_scan_never_returns_its_own_process(monkeypatch):
    """`_find_all_chrome_processes` 要跳過自己。

    分支覆蓋率量到這一格從來沒有被走過（2026-09-21）。餵一個名字剛好是
    `chrome.exe`、pid 剛好是自己的行程——這正是「有人放寬名稱白名單」那天的樣子。
    """
    _install(monkeypatch, [
        _FakeProc(_OURS, "chrome.exe", []),
        _FakeProc(_OTHER, "chrome.exe", []),
    ])
    monkeypatch.setattr(os, "getpid", lambda: _OURS)
    found, scan_ok = pc._find_all_chrome_processes()
    assert scan_ok is True
    assert [pid for pid, _name in found] == [_OTHER], (
        f"掃描把執行它的那個行程也列進去了：{found}")


def test_the_launcher_scan_never_returns_its_own_process(monkeypatch):
    """`find_launcher_pids` 要跳過自己，而這一支**不是**防禦性的。

    它找的是「正在跑某支腳本的 python 行程」，而呼叫它的也是 python 行程：名稱
    白名單在這裡擋不住自己，靠的就是這一行。分支覆蓋率量到它同樣從來沒被走過。
    """
    script = "start_webrunner.py"
    _install(monkeypatch, [
        _FakeProc(_OURS, "python.exe", ["python", f"D:/x/{script}"]),
        _FakeProc(_OTHER, "python.exe", ["python", f"D:/x/{script}"]),
    ])
    monkeypatch.setattr(os, "getpid", lambda: _OURS)
    pids, scan_ok = pc.find_launcher_pids(script)
    assert scan_ok is True
    assert pids == [_OTHER], f"掃描把執行它的那個行程也列進去了：{pids}"


def test_the_launcher_scan_still_finds_the_others(monkeypatch):
    """近似反例：自我排除不得變成「什麼都不回」。

    少了這一格，把整段掃描改成 `return [], True` 也是綠的。
    """
    script = "start_webrunner.py"
    _install(monkeypatch, [
        _FakeProc(_OTHER, "python.exe", ["python", f"D:/x/{script}"]),
    ])
    monkeypatch.setattr(os, "getpid", lambda: _OURS)
    pids, scan_ok = pc.find_launcher_pids(script)
    assert (pids, scan_ok) == ([_OTHER], True)


# ---------------------------------------------------------------------------
# 對照組——樹是乾淨的，所以上面每一段「回報問題」的程式碼都不會在真實資料上執行
# ---------------------------------------------------------------------------

_CORPUS = '''
def kills_things():
    for proc in psutil.process_iter(attrs=["pid"]):
        proc.kill()

def nested_only():
    def _iter():
        for proc in psutil.process_iter():
            yield proc
    return list(_iter())

def unrelated():
    return sorted(range(3))
'''


def test_the_scanner_derivation_sees_nested_helpers_once():
    """巢狀 helper 算在外層頭上，而且不另外列一筆。"""
    found = _scanners_in(ast.parse(_CORPUS))
    assert set(found) == {"kills_things", "nested_only"}, sorted(found)


def test_the_self_exclusion_check_fires_on_a_scanner_without_it():
    """對照組：判斷本身要在合成語料上開火，而且只對「該有卻沒有」的那一支。"""
    bare = ast.parse(
        "def sweeps():\n"
        "    for proc in psutil.process_iter():\n"
        "        proc.kill()\n").body[0]
    guarded = ast.parse(
        "def careful():\n"
        "    own_pid = os.getpid()\n"
        "    for proc in psutil.process_iter():\n"
        "        if proc.pid == own_pid:\n"
        "            continue\n"
        "        proc.kill()\n").body[0]
    scanners = {("x.py", "sweeps"): bare, ("x.py", "careful"): guarded}
    assert _scans_without_self_exclusion(
        scanners, {("x.py", "sweeps"), ("x.py", "careful")}) == [("x.py", "sweeps")]
    # 沒有被列進「會殺人」那一份的，即使沒有自我排除也不報。
    assert _scans_without_self_exclusion(scanners, {("x.py", "careful")}) == []


_ACCEPTED_SPELLINGS = [
    ("直接比 os.getpid()",
     "def sweeps():\n"
     "    for proc in psutil.process_iter():\n"
     "        if proc.pid == os.getpid():\n"
     "            continue\n"),
    ("先綁到一個名字再比",
     "def sweeps():\n"
     "    own = os.getpid()\n"
     "    for proc in psutil.process_iter():\n"
     "        if proc.pid == own:\n"
     "            continue\n"),
    ("做成 self_pid 參數",
     "def sweeps(script, *, self_pid=None):\n"
     "    for proc in psutil.process_iter():\n"
     "        if proc.pid == self_pid:\n"
     "            continue\n"),
]


@pytest.mark.parametrize("label,source", _ACCEPTED_SPELLINGS,
                         ids=[row[0] for row in _ACCEPTED_SPELLINGS])
def test_each_accepted_spelling_of_the_self_exclusion_is_recognised(label, source):
    """三種寫法都要認得——認漏一種就會對一支其實正確的掃描叫狼來了。"""
    assert _has_self_pid_comparison(ast.parse(source).body[0]), label


_BROKEN_SPELLINGS = [
    ("綁了名字卻沒拿去比",
     "def sweeps():\n"
     "    own = os.getpid()\n"
     "    for proc in psutil.process_iter():\n"
     "        proc.kill()\n"),
    ("比了一個沒有被綁到 getpid 的名字",
     "def sweeps():\n"
     "    for proc in psutil.process_iter():\n"
     "        if proc.pid == own_pid:\n"
     "            continue\n"),
    ("什麼都沒有",
     "def sweeps():\n"
     "    for proc in psutil.process_iter():\n"
     "        proc.kill()\n"),
]


@pytest.mark.parametrize("label,source", _BROKEN_SPELLINGS,
                         ids=[row[0] for row in _BROKEN_SPELLINGS])
def test_a_half_removed_self_exclusion_is_not_accepted(label, source):
    """⚠️ 第二格是變異抓出來的。

    把 `own_pid = os.getpid()` 那一行刪掉、`== own_pid` 留著——執行期是
    `NameError`，而第一版的判斷（`"own_pid" in ast.unparse(node)`）**照樣放行**，
    因為那個字串還在下面那一行裡。用 AST 不用子字串，而且要問「這個名字是從
    `getpid()` 來的嗎」，不是「這幾個字出現過嗎」。
    """
    assert not _has_self_pid_comparison(ast.parse(source).body[0]), label
