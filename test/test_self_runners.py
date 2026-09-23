"""自帶 runner 的測試檔，`if __name__ == "__main__"` 必須是檔案的最後一段。

好幾支測試檔都刻意支援 `py -3 test/test_x.py` 直接執行（模組 docstring 明寫著
「自帶 runner」），因為在沒有 pytest 的環境、或想單看一支檔案的時候比較快。那個 runner
是在**模組執行到那一行的當下**去找測試的——不論是掃 `globals()` 還是逐支具名註冊，
都只看得到「已經 bind 好的名字」。

於是只要有人在那一段**後面**再加測試，它們就會被安靜地略過，而畫面上照樣印
`ALL N TEST GROUPS PASSED`。2026-08-30 實測，兩支檔案都中了：

| 檔案 | 印出來的 | 檔案裡實際有的 |
|---|---|---|
| `test_webrunner_shared.py` | `ALL 107 TEST GROUPS PASSED` | 112（少跑的 5 支正好是最新加的版面探測）|
| `test_bot_helpers.py` | `ALL 45 TEST GROUPS PASSED` | 140 |

`test_bot_helpers` 的差額**是刻意的**——它有一大票測試要吃 pytest fixture
（`tmp_path` / `monkeypatch` / `capsys`），沒有 fixture 根本叫不動。問題不在少跑，
而在**沒有講**：一句不帶條件的「ALL … PASSED」會被讀成「全部都過了」。兩件事分開修了
——差額照實印出來，main 區塊移到檔尾。

`test_webrunner_shared` 那 5 支不吃 fixture，純粹是被位置卡掉的，移到檔尾之後就跑得到。

本檔守兩件事：

1. **位置**——main 區塊必須在檔尾，因為位置就是那個陷阱本身：擺在檔尾，就不可能有
   測試落在它後面。
2. **誠實**（2026-09-03 補）——位置對了數量仍可能不對。上面那條對「掃 `globals()`」
   的 runner 夠用，對「逐支具名註冊」的不夠：`test_bot_prompts.py` 的 main 區塊移到
   檔尾之後，那份手寫的九行列舉照舊只跑九支，而檔案裡已經有 17 支；同日量到
   `test_supervisor.py` 是 30/34。差額本身可以存在（吃 pytest fixture／parametrize
   的測試 standalone 叫不動），不可以的是**不講**——一句不帶條件的
   `ALL N TEST GROUPS PASSED` 會被讀成「全部都過了」。所以要求 runner 自己從原始碼
   算出「檔案宣告了幾支」，再決定是丟出來還是把差額印在結論那一行。

委派給 pytest 的（`pytest.main(...)`）兩條都不受限制——pytest 收整個檔案，跟位置和
註冊方式都無關。注意委派可能藏在 `main()` 裡（`test_todo_format.py` 的區塊只是
`raise SystemExit(main())`），所以判定要看整個檔案，不能只看區塊。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
# 測試 2026-09-22 起住在 repo 根目錄的 `test/`（本檔所在的目錄），不在套件裡。
TEST_ROOT = Path(__file__).resolve().parent


def _self_runner_files() -> list[Path]:
    """自帶 runner 的測試檔。

    **判定用 AST，不是字串比對（2026-09-20 修）。** 舊版找的是字面的
    `__name__ == "__main__"`——**雙引號**。單引號那個寫法在 Python 裡完全等價，
    卻會讓整支檔案在這裡消失：它不出現在下面幾條規則的 parametrize 裡，而且沒有
    任何症狀，因為「沒被選到」跟「選到了而且通過」在報告上長得一模一樣。實測當天
    樹裡 29 個 `__main__` 區塊剛好都是雙引號，所以這是趁乾淨把範圍鎖住，不是在修
    一個正在發作的缺陷——跟那六份
    `glob("start_*.py")` 同一個形狀。

    換成 AST 還順手解掉兩個偽陽性：有兩支檔案只是在 docstring 裡**提到**那串字，
    舊版把它們選進來、再讓兩條規則各自 `pytest.skip`，於是整套測試每一輪都掛著
    四筆看起來像壞消息的 skipped。
    """
    out = []
    for path in sorted(TEST_ROOT.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        if _main_block(tree) is not None:
            out.append(path)
    return out


def _main_block(tree: ast.Module) -> ast.If | None:
    """模組**最外層**的 `if __name__ == "__main__":`。

    `__name__` 可以落在比較式的任一側（`"__main__" == __name__` 一樣合法），而
    引號 style 在 AST 這一層根本不存在——這就是選檔也改走這裡的理由。
    只看 `tree.body` 是刻意的：藏在函式裡的同名判斷不是模組的 runner 入口。
    """
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare):
            continue
        if any(isinstance(side, ast.Name) and side.id == "__name__"
               for side in (test.left, *test.comparators)):
            return node
    return None


def _delegates_to_pytest(node: ast.If) -> bool:
    """`pytest.main(...)` ＝ 交給 pytest 收檔，位置無所謂。"""
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "main"
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "pytest"):
            return True
    return False


def _delegates_anywhere(tree: ast.AST) -> bool:
    """整個檔案裡有沒有 `pytest.main(...)`。

    判定要看**整個檔案**而不是只看 main 區塊：`test_todo_format.py` 的區塊是
    `raise SystemExit(main())`，真正的 `pytest.main(...)` 在下一層的 `main()`
    裡。只看區塊會把它誤判成「自己跑」。
    """
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "main"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "pytest"
        for n in ast.walk(tree))


def test_there_are_self_runner_files_to_check():
    """空清單通過是最沒用的綠燈——先證明掃描器真的掃到東西。"""
    files = _self_runner_files()
    assert len(files) >= 5, (
        f"只找到 {len(files)} 支自帶 runner 的測試檔，太少了；掃描條件可能壞了。")


@pytest.mark.parametrize("path", _self_runner_files(), ids=lambda p: p.name)
def test_the_main_block_is_the_last_thing_in_the_file(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    block = _main_block(tree)
    assert block is not None, (
        f"{path.name} 被選進來了，卻找不到最外層的 `__main__` 區塊——"
        "選檔與判定用的是同一個 `_main_block`，對不上就代表抓法壞了。"
        "這裡刻意不是 `pytest.skip`：跳過看起來像環境問題，紅燈才看得出來。")
    if _delegates_to_pytest(block):
        return          # pytest 收整個檔案，位置不影響
    trailing = [n for n in tree.body
                if getattr(n, "lineno", 0) > block.lineno
                and not isinstance(n, ast.If)]
    tests_after = [n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name.startswith("test_")
                   and n.lineno > block.lineno]
    assert not tests_after, (
        f"{path.name}：`if __name__ == \"__main__\"` 在第 {block.lineno} 行，"
        f"後面還定義了 {len(tests_after)} 支測試（{tests_after[:3]}…）。"
        "自帶 runner 是在執行到那一行的當下找測試的，所以這些會被**安靜地略過**，"
        "而畫面上照樣印 `ALL N TEST GROUPS PASSED`。把 main 區塊移到檔尾。")
    assert not trailing, (
        f"{path.name}：main 區塊後面還有 {len(trailing)} 段最外層程式碼。"
        "現在還沒有測試落在後面，但下一個人加測試時就會落在那裡——"
        "把 main 區塊移到檔尾，這個陷阱就不存在了。")


def test_the_delegating_exemption_is_real():
    """反面：豁免不能被誤用。委派給 pytest 的檔案，main 區塊裡要真的有 `pytest.main`。"""
    exempt = []
    for path in _self_runner_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        block = _main_block(tree)
        if block is not None and _delegates_to_pytest(block):
            exempt.append(path.name)
            source = ast.get_source_segment(
                path.read_text(encoding="utf-8"), block) or ""
            assert "pytest.main" in source, (
                f"{path.name} 被判定為 pytest 委派，但區塊裡看不到 `pytest.main`")
    # 目前只有 `test_gui_control.py` 走這條；多一支不是錯，這裡只是把現況釘住，
    # 免得有人把豁免當成繞過位置規則的後門。
    assert len(exempt) <= 2, (
        f"pytest 委派的檔案變多了：{exempt}。不是不行，但要確認每一支都是真的"
        "委派、而不是拿 `pytest.main` 當幌子繞過位置規則。")


# ---------------------------------------------------------------------------
# 位置對了，數量還是可能不對
#
# 2026-09-03 補。原本這支**只**守位置（見模組 docstring：「守的是位置，不是數量」），
# 理由是「main 區塊在檔尾，就不可能有測試落在它後面」。那對**掃 `globals()`** 的
# runner 成立，對**逐支具名註冊**的 runner 不成立：`test_bot_prompts.py` 的 main
# 區塊移到檔尾之後，它那份手寫的九行列舉照舊只跑九支，而檔案裡已經有 17 支，畫面上
# 印的仍是一句不帶條件的 `ALL 9 TEST GROUPS PASSED`。同一天量到 `test_supervisor.py`
# 是 30/34。
#
# 所以再補一條，守的是**誠實**而不是數量本身：差額可以存在（吃 pytest fixture／
# parametrize 的測試 standalone 本來就叫不動），但 runner 必須自己從原始碼算出
# 「檔案裡宣告了幾支」，然後要嘛當場丟出來、要嘛把差額印在結論那一行。
# 不准的是「少跑了卻說 ALL … PASSED」。
# ---------------------------------------------------------------------------


def _reachable_from_main(tree: ast.AST, block: ast.If) -> list[ast.AST]:
    """從 main 區塊出發，可達的模組層函式（傳遞閉包，有界）。

    **只掃 runner 自己的呼叫圖，不掃整個模組**——第一版掃了整份檔案，於是任何一支
    測試裡不相干的 `ast.parse(... __file__ ...)`（例如用 AST 去檢查 `discord_bot.py`
    的守門）都會讓這條檢查通過。變異測試當場抓到：把 runner 的數量計算拿掉，整份
    檔案裡仍有別的 `parse(__file__)`，守門就啞了。
    """
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    seen: set[str] = set()
    out: list[ast.AST] = [block]
    pending = [block]
    while pending:
        node = pending.pop()
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = (call.func.id if isinstance(call.func, ast.Name)
                    else getattr(call.func, "attr", None))
            if name in funcs and name not in seen:
                seen.add(name)
                out.append(funcs[name])
                pending.append(funcs[name])
    return out


def _runner_counts_declared(tree: ast.AST, block: ast.If) -> bool:
    """runner 有沒有從**自己的原始碼**算出宣告的測試數。

    判準是 `ast.parse(... __file__ ...)`——合規的檔案都是這樣做的。故意不比對函式名
    或訊息文字：那會變成在守一種寫法，而不是守那件事。
    """
    for scope in _reachable_from_main(tree, block):
        for node in ast.walk(scope):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "parse"
                    and "__file__" in ast.unparse(node)):
                return True
    return False


@pytest.mark.parametrize("path", _self_runner_files(), ids=lambda p: p.name)
def test_the_runner_knows_how_many_tests_the_file_declares(path: Path):
    """不准「少跑了卻說 ALL … PASSED」。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, str(path))
    block = _main_block(tree)
    assert block is not None, (
        f"{path.name} 被選進來了，卻找不到最外層的 `__main__` 區塊——"
        "選檔與判定用的是同一個 `_main_block`，對不上就代表抓法壞了。"
        "這裡刻意不是 `pytest.skip`：跳過看起來像環境問題，紅燈才看得出來。")
    # 委派判定要看**整個檔案**，不是只看 main 區塊：`test_todo_format.py` 的
    # 區塊是 `raise SystemExit(main())`，真正的 `pytest.main(...)` 在下一層的
    # `main()` 裡。只看區塊會把它誤判成「自己跑」而要求它數數，那是多餘的——
    # 只要檔案裡真的有 `pytest.main(...)`，pytest 就會自己收整個檔案、不會漏。
    if _delegates_to_pytest(block) or _delegates_anywhere(tree):
        return          # pytest 自己收檔案，數量不會漏

    declared = sum(1 for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name.startswith("test_"))
    assert _runner_counts_declared(tree, block), (
        f"{path.name}：自帶 runner 沒有從原始碼算出「檔案宣告了幾支測試」"
        f"（目前 {declared} 支）。逐支具名註冊的 runner 一定會落後於檔案，"
        "而一句不帶條件的 `ALL N TEST GROUPS PASSED` 會被讀成「全部都過了」。"
        "請照 `test_bot_helpers.py` / `test_supervisor.py` 的做法：用 "
        "`ast.parse(Path(__file__)...)` 算出宣告數，差額印在結論那一行；"
        "或改成掃 `globals()` 並在數量對不上時直接 `raise SystemExit`。")


# ---------------------------------------------------------------------------
# 位置對了、數量也講清楚了，runner 仍然可能**根本跑不起來**
#
# 2026-09-10 補。上面兩條守的都是「runner 有沒有漏掉測試、有沒有謊報」，兩條都
# 假設 runner 至少跑得完。實測那天六支自帶 runner 裡有**兩支**當場 `TypeError`：
#
# | 檔案 | 症狀 |
# |---|---|
# | `test_webrunner_shared.py` | 掃 `globals()` 後 `t()`，撞上唯一一支收 `tmp_path` 的測試就死 |
# | `test_run_progress.py`     | 同上，19 支收 fixture 的測試，第一支就死 |
#
# 而 `test_self_runners.py` 全綠——因為當掉既不是「漏跑」也不是「謊報」。
#
# `test_run_progress.py` 那支還更值得記：它的「另有 N 支 standalone 跑不到」那段
# 是從**逐支具名註冊**的 runner 抄過來的，但它自己是掃 `globals()` 的，
# `len(tests)` 與 `declared` **恆等**，所以差額恆為 0、那句話永遠不會印。上面那條
# 誠實檢查刻意只看「有沒有 `ast.parse(__file__)`」（不比對函式名或訊息文字，免得
# 變成在守一種寫法），於是這段恆為死碼的儀式就把它滿足了。**守門被一段死碼滿足，
# 而真正的缺陷在它前面兩行。**
#
# 怎麼守而不必付整份測試的錢：把模組匯進來，把每一支 `test_*` 換成**保留簽名**的
# 替身，再叫 runner。替身用 `sig.bind(*args, **kwargs)`，所以少餵參數一樣在同一個
# 位置丟同一種 `TypeError`；測試本體則一毛錢都不花。實測六支合計約 6 秒，真的跑
# 是約 48 秒。
#
# 替身**不能**寫成 `lambda *a, **k: None`——那會照單全收，缺陷當場消失，而守門
# 依舊全綠。這正是這一整段要防的形狀，所以下面配了一支合成的正面對照把它釘住。
# ---------------------------------------------------------------------------

_ARITY_PROBE = r"""
import contextlib, importlib, inspect, io, sys

pkg_root, module_name, entry_names = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, pkg_root)
mod = importlib.import_module(module_name)


def _stub(original):
    signature = inspect.signature(original)

    def double(*args, **kwargs):
        signature.bind(*args, **kwargs)     # 還原真實呼叫的 arity 檢查
        return None

    # 簽名要一起帶過去：runner 若自己 `inspect.signature(t)` 決定要餵什麼
    # （`test_webrunner_shared._run_all` 就是），看到替身的 `(*args, **kwargs)`
    # 會做出與真實情況完全不同的決定，探針就不再是在量原本那件事了。
    double.__name__ = original.__name__
    double.__signature__ = signature
    return double


stubbed = 0
for key, value in list(vars(mod).items()):
    if key.startswith("test_") and inspect.isfunction(value):
        setattr(mod, key, _stub(value))
        stubbed += 1

runner = None
for candidate in entry_names.split(","):
    found = getattr(mod, candidate, None)
    if callable(found):
        runner = found
        break
if runner is None:
    print("PROBE-NOENTRY")
    raise SystemExit(2)
if stubbed == 0:
    print("PROBE-NOSTUBS")
    raise SystemExit(2)

buffer = io.StringIO()
try:
    with contextlib.redirect_stdout(buffer):
        result = runner()
except SystemExit as exc:
    if exc.code not in (0, None):
        print(f"PROBE-SYSTEMEXIT {exc.code!r}")
        raise SystemExit(1)
    result = 0
print(f"PROBE-OK stubbed={stubbed} entry={runner.__name__} result={result!r}")
"""


def _runner_entry_names(tree: ast.Module, block: ast.If) -> list[str]:
    """main 區塊直接呼叫到的模組層函式名，依出現順序。

    刻意**不**寫死 `_run_all` / `main` 這種名字清單：那會變成在守一種命名，而且
    改名就靜靜地什麼都不檢查了。`raise SystemExit(main())` 這種包一層的寫法也
    照樣抓得到，因為找的是區塊裡的 `ast.Call`，不是區塊的形狀。
    """
    module_functions = {n.name for n in tree.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    names: list[str] = []
    for node in ast.walk(block):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in module_functions
                and node.func.id not in names):
            names.append(node.func.id)
    return names


def _self_running_files() -> list[Path]:
    """真的自己跑測試的檔案——委派給 pytest 的不算。"""
    out = []
    for path in _self_runner_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        block = _main_block(tree)
        if block is None or _delegates_to_pytest(block) or _delegates_anywhere(tree):
            continue
        out.append(path)
    return out


def _run_arity_probe(pkg_root: Path, module_name: str,
                     entry_names: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _ARITY_PROBE, str(pkg_root), module_name,
         ",".join(entry_names)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def test_there_are_self_running_files_to_check():
    """空清單通過是最沒用的綠燈（與上面那支同理，這裡的過濾條件更多一層）。"""
    files = _self_running_files()
    assert len(files) >= 4, (
        f"只找到 {len(files)} 支「自己跑」的測試檔，太少了；委派判定可能把大家"
        "都濾掉了，那樣下面每一支都會變成沒有檢查任何東西的綠燈。")


@pytest.mark.parametrize("path", _self_running_files(), ids=lambda p: p.name)
def test_the_runner_can_actually_call_every_test_it_registers(path: Path):
    """runner 必須跑得完——不是「有沒有漏跑」，是「會不會當場死掉」。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    block = _main_block(tree)
    entries = _runner_entry_names(tree, block)
    assert entries, (
        f"{path.name}：`if __name__ == \"__main__\"` 區塊裡找不到任何對模組層"
        "函式的呼叫，所以這支檢查不知道要叫什麼——它會變成一個沒有檢查任何東西"
        "的綠燈。請確認 runner 真的是從那個區塊呼叫的。")
    # 探針把**測試目錄**放上 `sys.path` 再 import 這支檔案；被測模組要靠檔案自己開頭
    # 那行 `sys.path.insert` 找到套件——那正是 `py -3 test/test_x.py` 會走的路，所以
    # 這一支也順便守住「搬到 `test/` 之後，自帶 runner 仍然找得到套件」。
    proc = _run_arity_probe(TEST_ROOT, path.stem, entries)
    detail = (proc.stdout + proc.stderr).strip().splitlines()
    assert proc.returncode == 0, (
        f"{path.name} 的自帶 runner 跑不完（entry={entries}，rc={proc.returncode}）。"
        f"最後幾行：{detail[-4:]}\n"
        "測試本體已經被換成保留簽名的替身、一個都沒有真的執行，所以這裡紅掉代表"
        "**runner 自己**有問題，不是某支測試失敗。最常見的原因：runner 對收 pytest "
        "fixture（`tmp_path` / `monkeypatch` / `capsys`）或 `parametrize` 的測試"
        "直接 `t()`。兩條合規的出路——自己餵得出來的就餵（見 "
        "`test_webrunner_shared._run_all`），餵不出來的就別註冊、並把差額印在結論"
        "那一行（見 `test_supervisor.main`）。**不要**改成靜默略過。")


def test_the_arity_probe_actually_catches_a_broken_runner(tmp_path):
    """正面對照：把替身寫成照單全收，這整段就變成一個永遠綠的儀式。

    合成一支「掃 `globals()` 然後 `t()`」的 runner 配一支收 `tmp_path` 的測試——
    也就是 2026-09-10 在 `test_run_progress.py` 量到的那個形狀——探針必須紅。
    """
    module = tmp_path / "probe_broken_runner.py"
    module.write_text(
        "def test_needs_a_fixture(tmp_path):\n"
        "    assert tmp_path is not None\n"
        "\n"
        "def _run_all():\n"
        "    for t in [v for k, v in sorted(globals().items())\n"
        "              if k.startswith('test_') and callable(v)]:\n"
        "        t()\n",
        encoding="utf-8")
    proc = _run_arity_probe(tmp_path, module.stem, ["_run_all"])
    assert proc.returncode != 0, (
        "探針對一個明知壞掉的 runner 回了綠燈，所以上面那支 parametrize 的綠色"
        f"不代表任何事。輸出：{(proc.stdout + proc.stderr)[-300:]}")
    assert "TypeError" in proc.stdout + proc.stderr, (
        "探針紅了，但不是因為 arity——那表示它是為了別的理由紅的，紅燈的意義就"
        f"不是我們以為的那個。輸出：{(proc.stdout + proc.stderr)[-300:]}")


def test_the_arity_probe_passes_a_runner_that_supplies_the_fixture(tmp_path):
    """反面對照：探針不能是一支「什麼都紅」的檢查。

    同一支測試、同一個 runner，只是這次 runner 自己餵得出 `tmp_path`——必須綠。
    少了這一半，上面那支合成紅燈可以靠「探針永遠失敗」通過。
    """
    module = tmp_path / "probe_fixed_runner.py"
    module.write_text(
        "import inspect, tempfile\n"
        "from pathlib import Path\n"
        "\n"
        "def test_needs_a_fixture(tmp_path):\n"
        "    assert tmp_path is not None\n"
        "\n"
        "def _run_all():\n"
        "    for t in [v for k, v in sorted(globals().items())\n"
        "              if k.startswith('test_') and callable(v)]:\n"
        "        args = [Path(tempfile.mkdtemp())\n"
        "                for _ in inspect.signature(t).parameters]\n"
        "        t(*args)\n",
        encoding="utf-8")
    proc = _run_arity_probe(tmp_path, module.stem, ["_run_all"])
    assert proc.returncode == 0, (
        "探針把一個好的 runner 判成壞的，所以它會對每一支檔案都紅——一個永遠"
        f"紅的守門會被關掉。輸出：{(proc.stdout + proc.stderr)[-300:]}")


# ---------- 選檔本身的守門 ---------------------------------------------------


@pytest.mark.parametrize("source, selected", [
    ('if __name__ == "__main__":\n    run()\n', True),
    ("if __name__ == '__main__':\n    run()\n", True),        # 單引號，等價
    ('if "__main__" == __name__:\n    run()\n', True),        # 反過來寫
    ('"""docstring 裡提到 __name__ == \"__main__\" 而已"""\nx = 1\n', False),
    ('# 註解裡提到 __name__ == "__main__"\nx = 1\n', False),
    ('def f():\n    if __name__ == "__main__":\n        run()\n', False),
])
def test_the_selector_reads_the_ast_not_the_quote_style(source, selected):
    """對照組。真實資料現在剛好全是雙引號，所以只靠真實資料看不出差別。

    單引號與反寫那兩筆殺的是「退回字串比對」的變異；docstring／註解那兩筆殺的是
    「只要出現這串字就算」；最後一筆（藏在函式裡）殺的是「用 `ast.walk` 取代
    `tree.body`」——那會把不是模組入口的判斷也當成 runner。
    """
    assert (_main_block(ast.parse(source, "<synthetic>")) is not None) is selected


def test_the_selector_is_not_a_substring_scan():
    """釘住上面那個修法：`_self_runner_files` 不得退回字串比對。

    沒有這一支的話，把它改回 `'__name__ == "__main__"' in source` 在本機完全看不
    出差別（那正是它當初能存活的原因）——真實資料全是雙引號，兩種寫法選出來的
    集合一模一樣，只有單引號那天才會分岔，而那天不會有人在看這支測試。
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), __file__)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_self_runner_files")
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(
            body[0].value, ast.Constant):
        body = body[1:]                       # docstring 本來就會提到那串字
    literals = [n.value for stmt in body for n in ast.walk(stmt)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("__main__" in text for text in literals), (
        f"`_self_runner_files` 又在比對字面的 `__main__` 了：{literals}。"
        "引號 style 是原始碼的細節，不是語意；用 `_main_block` 讀 AST。")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_main_block" in called, (
        f"看不到 `_main_block` 的呼叫（實際用到：{sorted(called)}）——"
        "選檔與判定必須是同一套判準，否則被選到卻判不出區塊的檔案只能跳過。")


# ---------------------------------------------------------------------------
# 看起來像測試、pytest 卻永遠不收集的那幾支（2026-09-20）
# ---------------------------------------------------------------------------
# 上面整個檔案管的是「自帶 runner 的測試檔少跑了幾支卻說 ALL PASSED」。這一節是它的
# 極端版：`_test_*.py` 這種檔名 **pytest 一支都不收**（它只認 `test_*.py`），所以
# 那些檔案跑的次數是 **0**，而檔名讀起來像測試。實測 2026-09-20 的全樹覆蓋率：
# 兩支 `_test_*.py` 都是 **0.0%**，其中 `_test_presence_e2e.py` 不連網、不要憑證、
# 手動跑起來當場就過——它只是從來沒有人跑。
#
# 兩道守門，都很便宜：
# 1. **講清楚為什麼不收集。** 一個沒說理由的 `_test_` 前綴，下一個讀的人只會假設
#    它有在跑。`_test_broadcast_e2e.py` 一直都有寫（要憑證、要網路、要特權 intent）。
# 2. **至少 import 得起來。** 0% 覆蓋率的檔案腐爛時沒有任何訊號；import 是最便宜的
#    非零訊號，抓得到改名、刪函式、少 import 這幾種最常見的腐爛。

_UNCOLLECTED_PREFIX = "_test_"


def _uncollected_files() -> list[Path]:
    """檔名像測試、但 pytest 的預設 `python_files` 收不到的那些。

    三個地方都看：`test/`（2026-09-22 起兩支手動 e2e 住在這裡）、套件、repo root——
    後兩個是為了抓到有人把新的一支放回舊位置。
    """
    found = list(TEST_ROOT.glob(f"{_UNCOLLECTED_PREFIX}*.py"))
    found += list(PKG_ROOT.glob(f"{_UNCOLLECTED_PREFIX}*.py"))
    found += list(PKG_ROOT.parent.glob(f"{_UNCOLLECTED_PREFIX}*.py"))
    return sorted(found)


def _explains_itself(doc: str | None) -> bool:
    """docstring 裡**同一行**要同時提到前綴與 pytest。

    刻意要求同一行：分散在兩處的「`_test_`」與「pytest」可以是兩句不相干的話，
    那樣的守門會被巧合滿足（本 repo 已經在別處踩過「有做就好」的守門）。
    """
    for line in (doc or "").splitlines():
        if _UNCOLLECTED_PREFIX in line and "pytest" in line:
            return True
    return False


def test_there_are_uncollected_files_to_check():
    """對照組：空清單跟「每一支都合格」長得一模一樣。"""
    found = _uncollected_files()
    assert len(found) >= 2, f"只找到 {[p.name for p in found]}"
    collected = list(TEST_ROOT.glob("test_*.py"))
    assert collected, f"`{TEST_ROOT}` 底下一支 `test_*.py` 都沒有——測試目錄搬了？"
    assert not set(found) & set(collected), "選檔把真的測試檔也選進來了"


@pytest.mark.parametrize("path", _uncollected_files(),
                         ids=lambda p: p.name)
def test_an_uncollected_file_says_why_it_is_not_collected(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    assert _explains_itself(ast.get_docstring(tree)), (
        f"{path.name} 用了 `{_UNCOLLECTED_PREFIX}` 前綴，所以 pytest 一次都不會收集"
        "它，但 docstring 沒有說為什麼。沒說理由的話，下一個讀的人只會假設它有在跑"
        "——而它的覆蓋率是 0。請在同一行講清楚前綴與 pytest 的關係。")


@pytest.mark.parametrize("doc, explains", [
    ("`_test_` 前綴讓 pytest 不收集：要憑證。", True),
    ("手動 e2e，不連網。", False),
    ("這支叫 `_test_something`。\n\n另一段提到 pytest。", False),
    (None, False),
])
def test_the_explanation_check_can_tell_the_near_misses_apart(doc, explains):
    """樹上兩支都合格之後，回報違規那段在真實資料上就不再執行了。"""
    assert _explains_itself(doc) is explains


@pytest.mark.parametrize("path", _uncollected_files(),
                         ids=lambda p: p.name)
def test_an_uncollected_file_still_imports(path: Path):
    """0% 覆蓋率的檔案腐爛時沒有任何訊號。import 是最便宜的非零訊號。

    這兩支的模組層都只有 import 與常數，憑證與網路都在函式裡面，所以 import
    不會連線、不會讀 token。
    """
    import importlib

    module = importlib.import_module(path.stem)
    assert module.__doc__, f"{path.name} 連 docstring 都沒了"
