"""**測試套件本身不得有能力破壞這台機器。**

這個 repo 的測試跑在一台**同時在做正事**的機器上：一個無人值守的批次可能已經
連續跑了好幾天，Chrome 開著，bot 連著。所以「跑一次測試」必須是一個安全動作。

2026-09-07 它不是。`test_chrome_recovery.py` 想測「沒有 psutil 時要退到
taskkill」，寫成 `sys.modules.pop("psutil", None)`。`pop` 清掉的是**快取**，被測
函式裡的 `import psutil` 於是重新載入**真的**那一個，`proc.kill()` 殺的是這台機器
上正在跑的 Chrome。一個已經連續執行 **78.7 小時**的批次因此崩潰
（`[WinError 10061] 目標電腦拒絕連線`，`rc=1 after 283379s`），監督者五秒後重啟。

**最惡劣的地方是它偽裝成無害的紅燈。** 真 psutil 把 chrome 殺光 → 重數存活得到
0 → `remaining != 0` 為 False → taskkill 整段被跳過 → `assert 0 == 2`。測試報告上
只有一行斷言失敗，沒有任何東西指向「剛剛破壞了什麼」。

那一支已經修好，`test_chrome_recovery.py` 裡也有一支檔案內的守門。**但那個坑不是
那個檔案專屬的**——任何測試檔只要用「往 `sys.modules` 塞替身」建立隔離，就同樣
會被 `pop`／`del` 拆掉。所以這一支把規則拉到整個 repo。

要模擬「套件不存在」，寫 `sys.modules["x"] = None`——那會讓 `import x` 丟
`ImportError: import of x halted; None in sys.modules`，而且不會讓真的模組回來。

**失效方向是刻意選的：寧可測試紅，不可以真的動到這台機器。**
"""
from __future__ import annotations

import ast
import builtins
import configparser
import importlib
import os
import sys
import warnings
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
# 測試與 `conftest.py` 2026-09-22 起住在 repo 根目錄的 `test/`（本檔所在的目錄），
# 不在套件裡。下面凡是搬家前掃套件 glob 就會掃到測試的地方，都把這個目錄一起列進來。
TEST_ROOT = Path(__file__).resolve().parent

# 直接對主機下手、測試裡一律不該出現的呼叫。
# `subprocess.run` **不在**這裡：好幾支測試用它做唯讀的 git 查詢與 import 檢查，
# 那是正當用途。禁的是「沒有回頭路」的那些——一個會叫的守門才有人看。
_FORBIDDEN_CALLS = {
    "os.kill": "在 Windows 上 `signal 0` 走的是主控台群組那條路，而非 0 的訊號會"
               "真的終止行程",
    "os.system": "直接把字串交給 shell 執行",
    "os.startfile": "會真的開啟外部程式",
}


# pytest 是用自己的機制載入 conftest 的，`import conftest` 不保證拿得到，所以這裡
# 直接讀磁碟上的原始碼——反正要驗的就是「那個檔案裡寫了什麼」。
_CONFTEST = TEST_ROOT / "conftest.py"
# 防線本體 2026-09-07 從 conftest 搬到這裡——`conftest.py` 只有 pytest 會載入，
# 而在 repo 外直接執行的變異探針同樣需要它（第三次事故就是這樣發生的）。
_KILLGUARD = PKG_ROOT / "_browser_killguard.py"


def _conftest_protected_images() -> set[str]:
    """從防線模組的原始碼裡取出 `_PROTECTED_IMAGES` 的內容。"""
    tree = ast.parse(_KILLGUARD.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_PROTECTED_IMAGES"
                        for t in node.targets)):
            return set(ast.literal_eval(node.value))
    raise AssertionError("`_browser_killguard.py` 裡找不到 `_PROTECTED_IMAGES`——防線被拿掉了？")


def _test_files() -> list[Path]:
    """`test/` 裡的測試與手動 e2e 腳本。

    套件目錄也照樣掃：放回套件的 `test_*.py` 雖然不會被收集（`pytest.ini` 的
    `testpaths = test`），但有人用路徑直接跑它時一樣會對這台機器下手。
    """
    return (sorted(TEST_ROOT.glob("test_*.py")) + sorted(TEST_ROOT.glob("_test_*.py"))
            + sorted(PKG_ROOT.glob("test_*.py")) + sorted(PKG_ROOT.glob("_test_*.py")))


def _function_ranges(tree: ast.AST) -> list[tuple[int, int, str]]:
    """一次走完整棵樹，回 `[(起, 迄, 函式名), …]`。

    **不要每個節點都重走一次樹。** 第一版把 `ast.walk` 放在逐節點的迴圈裡，對
    `test_bot_helpers.py`（297 KB）這種檔案就是 O(n²)，整支守門要跑 124 秒——
    一個讓套件變慢兩分鐘的靜態檢查，遲早會被人關掉，而被關掉的守門等於不存在。
    建一次範圍表之後是 0.6 秒。
    """
    return [(node.lineno, getattr(node, "end_lineno", node.lineno), node.name)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _enclosing_function(ranges: list[tuple[int, int, str]], lineno: int) -> str:
    """`lineno` 落在哪一個函式裡（取最內層＝起始行最大的那個）。"""
    best_start, best_name = -1, "<module>"
    for start, end, name in ranges:
        if start <= lineno <= end and start > best_start:
            best_start, best_name = start, name
    return best_name


def _scan(source: str, filename: str = "<test>") -> list[str]:
    """回這份原始碼裡所有違規的描述。

    **用 AST 不是子字串**：本檔的 docstring 與底下的訊息裡到處都是
    `sys.modules.pop`、`os.kill` 這些字面，子字串比對會把解釋規則的文字本身當成
    違規（本專案這個月已經踩過兩次這個坑）。
    """
    tree = ast.parse(source, filename)
    ranges = _function_ranges(tree)
    problems: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            where = _enclosing_function(ranges, node.lineno)
            if func.endswith("sys.modules.pop") and where.startswith("test_"):
                problems.append(
                    f"{filename}:{node.lineno} {where} 用了 `sys.modules.pop`")
            if func in _FORBIDDEN_CALLS:
                problems.append(
                    f"{filename}:{node.lineno} {where} 呼叫了 `{func}`"
                    f"（{_FORBIDDEN_CALLS[func]}）")
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and ast.unparse(target.value).endswith("sys.modules")):
                    where = _enclosing_function(ranges, node.lineno)
                    if where.startswith("test_"):
                        problems.append(
                            f"{filename}:{node.lineno} {where} 用了 "
                            "`del sys.modules[…]`")
    return problems


@pytest.mark.parametrize(
    "path", _test_files(), ids=lambda p: p.name)
def test_no_test_can_reach_the_real_machine(path: Path):
    """每一個測試檔都不得出現「會讓隔離失效」或「直接對主機下手」的寫法。"""
    problems = _scan(path.read_text(encoding="utf-8"), path.name)
    assert not problems, (
        "\n".join(problems)
        + "\n\n`sys.modules.pop`／`del sys.modules[…]` 清掉的只是**快取**，"
        "下一次 `import` 會從磁碟載入**真的**那個模組——如果這個檔案是靠往 "
        "`sys.modules` 塞假模組來隔離的，那就等於把安全網拆了。"
        '要模擬「套件不存在」請寫 `sys.modules["x"] = None`，'
        "那會讓 `import` 丟 ImportError。\n"
        "2026-09-07 這個寫法讓 `proc.kill()` 殺掉真的 Chrome，"
        "弄掉一個跑了 78.7 小時的正式批次。")


# --- 掃描器自己的 canary ------------------------------------------------------
# 「寫完就通過」不是好消息，是還沒被驗過。上面那一支在乾淨的 repo 上永遠是綠的，
# 所以它完全不能證明掃描器有在做事。下面餵合成的壞原始碼，逐一確認抓得到。

_BAD_SOURCES = {
    "sys.modules.pop": '''
import sys
def test_x():
    sys.modules.pop("psutil", None)
''',
    "del sys.modules[...]": '''
import sys
def test_x():
    del sys.modules["psutil"]
''',
    "os.kill": '''
import os
def test_x():
    os.kill(1234, 9)
''',
    "os.system": '''
import os
def test_x():
    os.system("taskkill /F /IM chrome.exe")
''',
}


@pytest.mark.parametrize("label,source", sorted(_BAD_SOURCES.items()))
def test_the_scanner_actually_catches_each_shape(label, source):
    """每一種違規寫法都要真的被抓到。"""
    assert _scan(source, "synthetic.py"), f"掃描器漏掉了 {label}"


_GOOD_SOURCES = {
    # 正確的模擬方式不得被誤報。
    "assigning None": '''
import sys
def test_x():
    sys.modules["psutil"] = None
''',
    # 夾具（不是 test_）在 finally 裡還原是正當的。
    "fixture teardown": '''
import sys
def _fixture():
    try:
        yield
    finally:
        sys.modules.pop("psutil", None)
''',
    # 唯讀的 git 查詢是正當用途。
    "read-only subprocess": '''
import subprocess
def test_x():
    subprocess.run(["git", "log"], capture_output=True, check=False)
''',
    # 只是提到名字的字串不算違規（這就是為什麼要用 AST）。
    "the name only in a string": '''
def test_x():
    assert "os.kill" not in open("f").read()
''',
}


@pytest.mark.parametrize("label,source", sorted(_GOOD_SOURCES.items()))
def test_the_scanner_does_not_cry_wolf(label, source):
    """會亂叫的守門是會被關掉的守門。

    特別是最後一個：`os.kill` 出現在**字串**裡不是違規。子字串比對會把它當成
    違規，AST 不會——而本專案的守門一再因為子字串比對而失效（`getsource()` 連
    docstring 一起拿，而 docstring 常常正在解釋那條規則）。
    """
    assert not _scan(source, "synthetic.py"), f"{label} 被誤報了"


def test_the_scan_actually_reached_the_test_files():
    """掃到 0 個檔案也會「全過」——那是最安靜的失效方式。"""
    files = _test_files()
    assert len(files) >= 30, (
        f"只掃到 {len(files)} 個測試檔，glob 可能壞了；"
        "掃不到東西的守門跟沒有守門一樣。")


# ---------------------------------------------------------------------------
# conftest 那道執行期防線
# ---------------------------------------------------------------------------
# 上面那些是**靜態**檢查——它們讀原始碼，看不到執行期。2026-09-07 靜態守門就位之後
# 又發生了第二次「正式瀏覽器被殺掉」，所以 `conftest.py` 另外裝了一道執行期防線：
# 任何 `psutil.Process.kill()/terminate()` 只要對象叫 chrome.exe / chromedriver.exe
# 就丟例外，任何 `taskkill` 也一樣。
#
# 那道防線在 conftest 匯入時就裝上、沒有夾具能拿掉，所以它在**這些測試裡也是活的**
# ——下面直接驗它會不會叫。**「寫完就通過」不是好消息，是還沒被驗過。**


def test_the_runtime_backstop_refuses_to_kill_a_browser(monkeypatch):
    """對一個名字是 chrome.exe 的行程呼叫 `kill()` 必須當場丟例外。

    不需要真的有 Chrome：防線判斷的依據是 `self.name()`，所以把一個無害行程
    （這支測試自己）的名字蓋成 `chrome.exe` 就能走到同一條路。**而且它必須在
    真正的 `kill()` 被呼叫之前就擋下來**——如果順序反了，這支測試會把自己殺掉，
    那本身就是最直接的失敗訊號。
    """
    psutil = pytest.importorskip("psutil")
    proc = psutil.Process(os.getpid())
    monkeypatch.setattr(proc, "name", lambda: "chrome.exe")
    with pytest.raises(AssertionError) as caught:
        proc.kill()
    assert "chrome.exe" in str(caught.value)


def test_the_runtime_backstop_refuses_taskkill():
    """`taskkill` 不得真的被執行。

    這裡刻意點名一個不存在的 image：萬一防線沒生效，`taskkill` 也不會傷到任何
    東西，測試只會因為「沒有丟例外」而紅——**一個守門的測試本身不該有破壞力**。
    """
    import subprocess  # noqa: S404 — 只用來確認它被擋下來
    with pytest.raises(AssertionError) as caught:
        subprocess.run(["taskkill", "/F", "/IM", "definitely-not-real.exe"],
                       capture_output=True, check=False)
    assert "taskkill" in str(caught.value)


def test_the_runtime_backstop_still_allows_ordinary_processes():
    """會亂叫的守門是會被關掉的守門。

    殺掉自己 spawn 出來的子行程是完全正當的（`test_supervisor.py` 就在做四次），
    所以防線只擋兩個 image 名稱。這裡確認保護清單**只有**那兩個——真的對一個
    python 行程呼叫 `kill()` 來驗「有放行」太危險，而清單本身就是判準。
    """
    names = _conftest_protected_images()
    assert names == {"chrome.exe", "chromedriver.exe"}, (
        f"保護清單變成 {sorted(names)}。放太寬會擋掉正當的子行程測試，"
        "放太窄就漏掉要保護的對象。")


def test_the_backstop_cannot_be_removed_by_a_fixture():
    """防線是在**匯入時**裝上的，不是夾具——沒有 teardown 拿得掉它。

    這一條是刻意的：出事的三次，問題都在於「該生效的隔離沒生效」。一道可以被
    還原的防線，會在最需要它的那一刻剛好不在。

    兩半都要釘：`_browser_killguard` 在模組層呼叫 `install()`，而 `conftest.py`
    在模組層匯入它。少了任何一半，pytest 底下就沒有防線。
    """
    guard_tree = ast.parse(_KILLGUARD.read_text(encoding="utf-8"))
    top_level_calls = [ast.unparse(node.value.func)
                       for node in guard_tree.body
                       if isinstance(node, ast.Expr)
                       and isinstance(node.value, ast.Call)]
    assert "install" in top_level_calls, (
        "`_browser_killguard` 不再於模組層呼叫 `install()` 了。"
        "改成要別人記得呼叫，等於讓它在最需要的那一刻剛好沒裝上。")

    conf_tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"))
    imported = [alias.name
                for node in conf_tree.body
                if isinstance(node, ast.Import)
                for alias in node.names]
    assert "_browser_killguard" in imported, (
        "`conftest.py` 不再於**模組層**匯入 `_browser_killguard` 了——"
        "pytest 底下就沒有防線了。放進夾具或函式裡都不算。")


def test_the_backstop_also_protects_scripts_run_outside_pytest():
    """**這是 2026-09-07 第三次事故的直接教訓。**

    防線原本寫在 `conftest.py` 裡，而 `conftest.py` **只有 pytest 會載入**。那次
    出事的變異探針是用 `py -3 harness.py` 直接執行、自己 import 測試模組的，完全
    在 pytest 的傘外——防線根本沒裝上，於是真的 `_kill_chrome_pids` 跑了起來。

    所以防線現在住在一個獨立模組裡，一行 `import _browser_killguard` 就生效。
    這支測試用**子行程**驗證那條路真的通：在完全沒有 pytest 的行程裡匯入它，
    然後確認 `subprocess.run` 真的被換掉了。

    用子行程而不是在這裡直接測，是因為**這個行程裡防線早就裝好了**——在已經裝好
    的行程裡「驗證匯入會裝上」，驗到的是 idempotent 而不是「會裝上」。
    """
    import subprocess
    import sys
    probe = (
        "import sys, subprocess\n"
        "before = subprocess.run\n"
        "assert 'pytest' not in sys.modules, 'probe 不該在 pytest 底下跑'\n"
        "sys.path.insert(0, %r)\n"
        "import _browser_killguard\n"
        "print('WRAPPED' if subprocess.run is not before else 'NOT-WRAPPED')\n"
    ) % str(PKG_ROOT)
    out = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert out.returncode == 0, f"探針自己就掛了：{out.stderr[-400:]}"
    assert "WRAPPED" in out.stdout, (
        f"在沒有 pytest 的行程裡匯入 `_browser_killguard` 之後，"
        f"`subprocess.run` 沒有被換掉：{out.stdout!r} / {out.stderr[-300:]}")


def test_importing_the_mutation_harness_installs_the_backstop_too():
    """**最可能忘記那一行的人，就是最需要那道防線的人。**

    `_browser_killguard` 的殘餘缺口一直是「要記得加 `import _browser_killguard`」，
    而 2026-09-07 第三次弄掉正式瀏覽器的**正是一支變異測試探針**。所以變異骨架自己
    把防線裝上：任何探針只要 `from mutation_harness import run_mutations`，防線就在
    它有機會做任何事之前生效。

    一樣用**子行程**驗證——在防線早就裝好的這個行程裡驗「匯入會裝上」，驗到的是
    idempotent 而不是「會裝上」。而且這裡刻意**只匯入 `mutation_harness`**，模擬一支
    忘了寫那一行的探針。
    """
    import subprocess
    import sys
    probe = (
        "import sys, subprocess\n"
        "before = subprocess.run\n"
        "assert 'pytest' not in sys.modules, 'probe 不該在 pytest 底下跑'\n"
        "sys.path.insert(0, %r)\n"
        "import mutation_harness\n"          # 刻意不匯入 _browser_killguard
        "print('WRAPPED' if subprocess.run is not before else 'NOT-WRAPPED')\n"
    ) % str(PKG_ROOT)
    out = subprocess.run([sys.executable, "-c", probe],
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert out.returncode == 0, f"探針自己就掛了：{out.stderr[-400:]}"
    assert "WRAPPED" in out.stdout, (
        "只匯入 `mutation_harness` 沒有把瀏覽器防線裝上——`mutation_harness` 裡那行 "
        "`import _browser_killguard` 是不是被當成沒用的 import 刪掉了？"
        f"{out.stdout!r} / {out.stderr[-300:]}")


def test_the_guard_keeps_popen_a_class():
    """換掉 `subprocess.Popen` 的東西**必須還是類別**，不能是函式。

    2026-09-07 實測踩到：防線原本把 `subprocess.Popen` 換成一個函式，於是任何
    `class X(subprocess.Popen)` 都會炸
    `TypeError: function() argument 'code' must be code, not str`。

    這個缺陷從防線還住在 `conftest.py` 的時候就在，只是被**匯入順序**蓋住了——
    pytest 先載入自己的東西、conftest 才被匯入，所以那些繼承早就發生完了。防線一
    搬到「探針最早匯入的東西」，同一個缺陷立刻現形。

    一般化：**替換一個名字的時候要保留它原本的種類。** 類別換成函式會悄悄拿掉
    繼承與 `isinstance`，而這兩件事不會在替換的當下報錯，只會在**別人**用到時炸。
    """
    import subprocess

    assert isinstance(subprocess.Popen, type), (
        "`subprocess.Popen` 被換成非類別了（多半是函式）——"
        "任何繼承它的程式都會在 import 時炸掉。")

    class _Sub(subprocess.Popen):    # 不得丟 TypeError
        pass

    assert issubclass(_Sub, subprocess.Popen)


def test_the_guard_still_lets_ordinary_subprocesses_run():
    """反方向：擋 `taskkill` 不能變成擋住所有子行程。

    這個專案的測試真的會 spawn python 子行程（`test_supervisor.py` 就在做四次），
    所以防線一旦擋過頭，會以「一堆不相干的測試莫名其妙壞掉」的形式出現。
    """
    import subprocess
    import sys

    out = subprocess.run([sys.executable, "-c", "print('ok')"],
                         capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert out.returncode == 0 and "ok" in out.stdout, (
        f"一般的子行程被擋住了：rc={out.returncode} {out.stderr[-200:]}")


def test_installing_the_backstop_twice_does_not_stack():
    """`conftest` 與 repo 外的探針可能都匯入它——重複裝不得疊上去。

    疊起來不會壞事，但會讓「防線到底裝了幾層」變得說不清楚，而這道防線的價值有
    一半來自它的行為可以被準確描述。
    """
    import _browser_killguard as kg
    assert kg.install() is False, (
        "第二次 `install()` 回了 True——代表它又包了一層。"
        "重複匯入是預期情境，`_INSTALLED` 旗標要擋住它。")


# ---------------------------------------------------------------------------
# 第二部分：測試套件不得**安靜地把別支測試的診斷吃掉**（2026-09-09）
# ---------------------------------------------------------------------------
# 上面那一整段守的是「跑測試不可以弄壞這台機器」。這一段守的是同一個檔案該管的
# 另一半：**跑測試不可以讓另一支測試看不到它該看到的東西。** 兩者的共同點是失效
# 形態——套件本身在騙你，而報告上只有一行看起來無關的斷言。
#
# 具體的機制：`presence_probe` / `_batch_config` / `_bot_config` 各有一個
# module-level 的 `_WARNED` 集合（設定檔每個 tick、每個角色都重讀，同一句抱怨會
# 把記錄檔洗掉，所以必須去重）。副作用是**測試之間會互相汙染**——兩支測試只要觸發
# 逐字相同的警告文字，後跑的那支就什麼都收不到。
#
# 這不是假想。`test_undecodable_files.py` 裡那支查 presence 設定退路的測試有三個
# 參數化案例，三個都把設定檔寫成 `tmp_path/"cfg.json"`，三段警告逐字相同，第一個
# 印出來、後兩個被吃掉，而失敗訊息是 `assert 'failed' in ''`——完全看不出跟去重
# 有關。
#
# 修法是 `conftest.py` 的 autouse 夾具在每支測試前後清空那些集合，而它靠一份
# **手寫的名單** `_WARN_DEDUP_MODULES`。名單漏一個模組不會有任何症狀，直到某天
# 兩支測試剛好撞到同一句話，然後變成一個「換個順序就不重現」的紅燈。
#
# 所以這裡把「要記得加進名單」變成「不加就紅」。同一個 repo 這幾天已經用同一招
# 處理過兩張手寫表（`_batch_config._COERCERS` 與 `_bot_config` 的三張表）。
_WARN_DEDUP_ADVICE = (
    "在模組層宣告 `_WARNED` 的模組都必須列進 `conftest._WARN_DEDUP_MODULES`，"
    "否則它的警告會跨測試殘留。")


def _module_level_assigns(tree: ast.Module, name: str) -> list[ast.expr]:
    """模組**最外層**對 `name` 的指派，回傳被指派的值。

    只看最外層是刻意的：函式裡的區域變數同名不會跨測試殘留，把它算進來只會製造
    一個沒有意義的登記需求。
    """
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            values.append(value)
    return values


def test_the_scan_only_counts_module_level_assignments():
    """合成樣本：函式裡的同名區域變數**不算**，模組層的才算。

    沒有這一支，把 `tree.body` 換成 `ast.walk(tree)` 會全綠——因為目前沒有任何模組
    在函式裡用這個名字。那正是「守門的一半悄悄失效」的形狀：失效跟「沒有東西可抓」
    在輸出上完全一樣。而這條規則有實質內容——區域變數不會跨測試殘留，把它算進來
    只會要求別人去登記一個根本不需要清的東西。
    """
    module_level = ast.parse("_WARNED = set()\n")
    inside_a_function = ast.parse("def f():\n    _WARNED = set()\n")
    annotated = ast.parse("_WARNED: set[str] = set()\n")
    assert _module_level_assigns(module_level, "_WARNED")
    assert _module_level_assigns(annotated, "_WARNED"), (
        "帶型別註記的指派沒被認出來——`_warn_dedup._WARNED` 就是這種寫法。")
    assert not _module_level_assigns(inside_a_function, "_WARNED"), (
        "函式裡的區域變數被算成模組層的了。")


def _modules_with_a_module_level_warned() -> set[str]:
    """AST 掃出「在模組層指派 `_WARNED`」的專案模組名。

    `test/` 照同一個判準過濾（手動 e2e 腳本搬家前住在套件裡、在範圍內，照舊）。"""
    found: set[str] = set()
    for path in sorted(PKG_ROOT.glob("*.py")) + sorted(TEST_ROOT.glob("*.py")):
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        if _module_level_assigns(tree, "_WARNED"):
            found.add(path.stem)
    return found


def _registered_dedup_modules() -> set[str]:
    """從 `conftest.py` 的**原始碼**讀出 `_WARN_DEDUP_MODULES`。

    不 `import conftest`——理由與這個檔案上半部相同（pytest 用自己的機制載入它，
    一般的 import 不保證拿得到同一個物件）。
    """
    tree = ast.parse(_CONFTEST.read_text(encoding="utf-8"), str(_CONFTEST))
    values = _module_level_assigns(tree, "_WARN_DEDUP_MODULES")
    assert len(values) == 1, (
        f"`conftest.py` 裡對 `_WARN_DEDUP_MODULES` 的模組層指派有 {len(values)} "
        "處，預期剛好一處。")
    names = ast.literal_eval(values[0])
    return set(names)


def test_every_warn_dedup_cache_is_registered_in_conftest():
    """兩個方向都釘：名單不得漏掉一個模組，也不得留著已經不在的模組。"""
    scanned = _modules_with_a_module_level_warned()
    # 正面對照組：掃不到東西的話這支會變成「空集合 ⊆ 名單」，永遠通過。
    # **下限 2026-09-09 從 3 降成 1**，因為三份 `_warn_once` 合併成了共用的
    # `_warn_dedup`——下限量的是「檔案掃描有沒有在動」，不是「應該有幾份」。
    # 掃描器本身的牙齒在 `test_the_scan_only_counts_module_level_assignments`
    # （合成樣本），不是這一行；這一行只擋「glob 掃了個空」。
    assert len(scanned) >= 1, (
        f"只掃到 {sorted(scanned)} 個帶模組層 `_WARNED` 的模組，掃描器多半壞了"
        "（零命中會讓這支測試變成永遠通過）。")

    listed = _registered_dedup_modules()
    missing = sorted(scanned - listed)
    assert not missing, (
        f"這些模組有模組層的 `_WARNED`，但不在 `_WARN_DEDUP_MODULES` 裡："
        f"{missing}。{_WARN_DEDUP_ADVICE} 症狀是「只有先跑的那支看得到警告」，"
        "而且**換個順序就不重現**。")
    # 反方向。**這一條不是唯一的防線**（名單多列一個模組，夾具自己就會在取屬性時
    # 炸掉，而且是每一支測試都炸），留著是為了把「2,000 個 error」換成一行看得懂的
    # 話。實測確認過：拿掉這一句，那個情境仍然會紅，只是紅得看不出原因。
    stale = sorted(listed - scanned)
    assert not stale, (
        f"`_WARN_DEDUP_MODULES` 列了這些模組，但它們已經沒有模組層的 `_WARNED` "
        f"了：{stale}。名單留著過期項目會讓夾具在取屬性時就 `AttributeError`，"
        "而那會讓**每一支**測試都紅。")


def test_the_registered_modules_really_expose_a_clearable_cache():
    """名單上的每一個模組都要真的拿得到一個清得掉的 `_WARNED`。

    只比對名字不夠：夾具實際做的是 `importlib.import_module(name)._WARNED.clear()`。
    名字對、但那個屬性換成了 `tuple` 之類清不掉的東西，夾具會在**每一支**測試上炸，
    而這支測試會先講清楚原因。
    """
    import importlib

    for name in sorted(_registered_dedup_modules()):
        cache = getattr(importlib.import_module(name), "_WARNED", None)
        assert isinstance(cache, set), (
            f"`{name}._WARNED` 是 {type(cache).__name__}，不是 `set`——"
            "`conftest` 的夾具會對它呼叫 `.clear()`。")


def test_this_interpreter_can_actually_enforce_a_test_timeout():
    """跑套件的直譯器必須裝著 `pytest-timeout`。

    本 repo 的測試跑在一台正在做正事的機器上，而**掛住的測試比失敗的測試更糟**：
    失敗會回報，掛住只是佔著這台機器不放，而且不會有人收到通知。整套的跑法因此
    一直是 `-q --timeout=900`。

    問題是「這個直譯器裝了沒」在別的地方完全沒有訊號。2026-09-09 實測：`py -3`
    有 `pytest-timeout` 2.4.0，**正式直譯器 `.venv` 沒有**，於是同一行指令在
    `.venv` 上是
    `error: unrecognized arguments: --timeout=900`——套件一支都沒跑。而那次是接在
    `| tail -6` 後面跑的，管線的結束碼是 `tail` 的，所以它**回報成功（exit 0）**。
    兩層靜默疊在一起：套件沒跑，而且看起來跑完了。

    `requirements.txt` 抓不到這條——它宣告的是**執行期**相依，測試工具鏈不在裡面
    （也不該在，fresh clone 只裝執行期的東西就該能把 bot 跑起來）。所以這條規則
    只能由套件自己檢查自己。

    刻意用 `find_spec` 而不是 `import`：這裡要問的是「裝了沒」，不需要把外掛真的
    載進來（pytest 早就載過了，重載只是多餘的副作用）。
    """
    import importlib.util

    assert importlib.util.find_spec("pytest_timeout") is not None, (
        "這個直譯器沒有 `pytest-timeout`，所以 `--timeout` 是無法辨識的參數，"
        "整套測試會一支都不跑、而且在管線後面看起來像成功。"
        "修法：`<這個直譯器> -m pip install pytest-timeout`。"
        "本 repo 的套件在**兩個**直譯器上各跑一次，兩個都要有。")


def test_the_default_per_test_timeout_does_not_depend_on_the_command_line(
        pytestconfig):
    """**裝了外掛 ≠ 這一回合真的有逾時。** 上面那支守前者，這一支守後者。

    上面那支問的是「這個直譯器裝了 `pytest-timeout` 沒有」，而那只是必要條件。
    保護真正生效還需要有人**把 `--timeout=900` 打在命令列上**——而那是手打的，
    漏打的時候什麼事都不會發生：套件照樣全綠，只是從此沒有上限，掛住的測試會安靜
    地佔著這台機器。本專案對這種形狀已經有判語（見 `CLAUDE.md` 談主機控制為什麼
    不能用角色閘）：**預設關閉的保護不算保護。**

    所以 `pytest.ini` 用 ini 選項設了預設值，而這一支釘住它。

    ⚠️ **為什麼第一個斷言看的是 `getini` 而不是「有沒有逾時」。** 「現在有沒有
    逾時」在有人打了 `--timeout=900` 的時候**照樣是真的**，即使 `pytest.ini` 整個
    不見了——那正是這一支要防的情況，卻也正是它最容易被騙過去的地方（空過）。
    命令列與設定檔是兩個獨立來源，只斷言合成結果等於讓兩者互相遮蔽。所以先直接問
    設定檔那一半，再問合成結果。

    第二個斷言用的是外掛**自己的**解析函式，不是重寫一份優先順序。它同時涵蓋
    「ini 寫了但外掛沒讀到」（鍵名打錯、區段名打錯、rootdir 跑掉）這一類只看
    `getini` 看不出來的失效。
    """
    from_ini = pytestconfig.getini("timeout")
    assert from_ini and float(from_ini) > 0, (
        f"`pytest.ini` 的 `timeout` 讀出來是 {from_ini!r}。"
        "代表每支測試的逾時**只剩命令列那一條路**，漏打 `--timeout=900` 就完全沒有"
        "上限，而且不會有任何症狀。修法：確認 repo root 的 `pytest.ini` 有 "
        "`[pytest]` 區段與 `timeout = 900`，而且 pytest 的 rootdir 真的是 repo root。")

    # ⚠️ **刻意不 `import pytest_timeout`**，理由有兩個，都是實測踩到的：
    # ① `requirements.txt` 宣告的是**執行期**相依，測試工具鏈刻意不在裡面（正式機
    #    不需要它），所以 `test_bot_helpers.test_every_third_party_import_is_declared_in_requirements`
    #    會把這個 import 判成「未宣告的第三方套件」而轉紅——實測發生過。
    # ② 上面那支姊妹測試也刻意用 `find_spec` 而不是 `import`；沒裝外掛的直譯器碰到
    #    module-level import 會直接 ImportError，把它準備好的清楚訊息蓋掉。
    # 改讀外掛在**它自己的** `pytest_configure` 裡快取下來的那個值：每支測試的
    # `_get_item_settings` 讀的就是 `item.config._env_timeout`，所以這裡問的正是
    # 真正會生效的那一份，比重新解析一次更接近事實。
    assert hasattr(pytestconfig, "_env_timeout"), (
        "`config._env_timeout` 不存在——要嘛 `pytest-timeout` 根本沒載入（那麼上面"
        "那支姊妹測試會講得更清楚），要嘛它換掉了內部欄位名，這一支要跟著改。")
    effective = pytestconfig._env_timeout
    assert effective is not None and effective > 0, (
        f"外掛實際會套用的逾時是 {effective!r}——`pytest.ini` 寫了，但它沒讀到。"
        "最常見的原因是鍵名或區段名打錯，或 rootdir 不是 repo root。")
    # ⚠️ 這一句刻意也問 `getini`，不是問 `settings.method`——理由跟第一個斷言同源，
    # 而且是實測出來的。變異測試把 ini 改成 `timeout_method = signal` 時這支**存活**
    # 了，因為 `mutation_harness` 給每個 child 都加了
    # `--timeout=60 --timeout-method=thread`，而命令列勝過 ini，於是合成結果照樣是
    # `thread`。換句話說，那個骨架本身就是「有人在命令列補了一個」的實例，剛好示範了
    # 為什麼這兩句都要直接問設定檔。不帶那些旗標手動重現時，錯的方法是會炸的：
    # `INTERNALERROR … module 'signal' has no attribute 'SIGALRM'`、rc=3、一支都沒跑。
    assert pytestconfig.getini("timeout_method") == "thread", (
        f"`pytest.ini` 的 `timeout_method` 是 "
        f"{pytestconfig.getini('timeout_method')!r}，預期 `thread`。Windows 沒有 "
        "`SIGALRM`，signal 那種方法在這台機器上會讓整輪以 INTERNALERROR 收場。")

# ---------------------------------------------------------------------------
# `main(argv=None)` 不得偷偷去讀 pytest 自己的命令列
# ---------------------------------------------------------------------------
# 2026-09-09 在 `audit_dependencies` 上真的踩過：加了一個 `--fresh` 旗標之後，六支
# **既有**測試同時轉紅。原因是它們呼叫 `main()`（無參數），而 argparse 收到
# `argv=None` 的預設行為是去解析 `sys.argv[1:]`——在 pytest 底下那是 `-q`、
# `--timeout=300` 之類，於是 `error: unrecognized arguments` → `SystemExit(2)`。
#
# 這是**套件安全**問題而不只是風格問題，理由有二：
#   1. 症狀會指向錯的地方。紅的是六支跟那個旗標毫無關係的測試，訊息長得像被測程式
#      壞了，而真正的原因在 argparse 的預設值裡。
#   2. 它是**潛伏**的。今天所有呼叫端都乖乖傳 `[]`（別的測試就是
#      逐處傳的），所以整套是綠的；下一個人寫 `main()` 的那一刻才會炸，而那時他會
#      以為是自己剛改的東西壞了。
#
# 約定：定義端寫 `parse_args(argv or [])`，`__main__` 那邊明確傳 `sys.argv[1:]`。
# 兩半都要，缺一不可——只改定義端會讓命令列版本失去所有旗標。
_ARGV_PARAM_NAMES = frozenset({"argv", "args"})


def _argv_entry_points(tree: ast.AST):
    """`[(函式名, parse_args 的第一個引數節點 或 None)]`——只收吃 `argv` 的函式。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = [a.arg for a in node.args.posonlyargs + node.args.args]
        if not (set(params) & _ARGV_PARAM_NAMES):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "parse_args"):
                out.append((node.name, call.args[0] if call.args else None))
    return out


def _reads_process_argv(arg) -> bool:
    """這個 `parse_args(...)` 的引數會不會退回 `sys.argv`？

    會的兩種：完全不給引數（`parse_args()`），以及直接把 `argv` 這個名字傳進去
    （`argv=None` 時 argparse 就去讀 `sys.argv`）。`argv or []` 是 `ast.BoolOp`，
    不算。
    """
    if arg is None:
        return True
    return isinstance(arg, ast.Name) and arg.id in _ARGV_PARAM_NAMES


def _main_block_calls(tree: ast.AST) -> list:
    """`if __name__ == "__main__":` 區塊裡對 `main(...)` 的呼叫。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "__name__" not in test or "__main__" not in test:
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "main":
                out.append(call)
    return out


def _argv_offenders(trees):
    """`[(模組名, AST)]` → 違規描述清單。掃描與聚合分開，好讓對照組餵合成資料。"""
    out = []
    for name, tree in trees:
        entries = _argv_entry_points(tree)
        if not entries:
            continue
        for func, arg in entries:
            if _reads_process_argv(arg):
                out.append(f"{name}:{func} 把 argv 直接交給 `parse_args`")
        for call in _main_block_calls(tree):
            if not call.args:
                out.append(f"{name}:__main__ 呼叫 `main()` 卻沒有傳 `sys.argv[1:]`")
    return out


def _project_trees():
    root = PKG_ROOT.parent
    trees = []
    # `test/` 照同一個判準過濾：`conftest.py` 與手動 e2e 腳本搬家前住在套件裡、
    # 在範圍內，搬家之後照舊。
    for path in (sorted(PKG_ROOT.glob("*.py")) + sorted(TEST_ROOT.glob("*.py"))
                 + sorted(root.glob("*.py"))):
        if path.name.startswith("test_") or path.name == "__init__.py":
            continue
        try:
            trees.append((path.name,
                          ast.parse(path.read_text(encoding="utf-8"), path.name)))
        except (SyntaxError, UnicodeDecodeError):   # pragma: no cover
            continue
    return trees


def test_no_entry_point_falls_through_to_the_test_runners_command_line():
    """吃 `argv` 的進入點，`argv=None` 必須是「沒有旗標」而不是「讀 sys.argv」。"""
    trees = _project_trees()
    assert len(trees) >= 25, (
        f"只抽到 {len(trees)} 個模組，抽取器壞了——抽不到檔案時「零筆違規」跟"
        "「全部乾淨」在輸出上長得一模一樣。")
    offenders = _argv_offenders(trees)
    assert not offenders, (
        "這些進入點在無參數呼叫時會去解析**pytest 自己的命令列**："
        + "、".join(offenders)
        + "。定義端改成 `parse_args(argv or [])`，`__main__` 那邊傳 "
          "`sys.argv[1:]`。兩半都要——只改定義端會讓命令列版本失去所有旗標。")


def test_the_argv_rule_actually_covers_more_than_one_module():
    """範圍 pin：真實資料乾淨時，掃一個檔案和掃全部長得一模一樣。

    量的是「這條規則保護的族群」——有幾個模組真的有吃 `argv` 的進入點。族群一縮
    （有人把掃描改回只讀某一支）這裡就紅。
    """
    trees = _project_trees()
    owners = {name for name, tree in trees if _argv_entry_points(tree)}
    assert len(owners) >= 3, (
        f"只找到 {sorted(owners)} 有吃 argv 的進入點——抽取器或掃描範圍壞了。"
        "（當時是 `audit_dependencies` / "
        "`gen_command_docs` / `verify_browser` 四支。）")


@pytest.mark.parametrize("source, bad", [
    ("import argparse\n"
     "def main(argv=None):\n"
     "    return argparse.ArgumentParser().parse_args(argv)\n", True),
    ("import argparse\n"
     "def main(argv=None):\n"
     "    return argparse.ArgumentParser().parse_args()\n", True),
    ("import argparse\n"
     "def main(argv=None):\n"
     "    return argparse.ArgumentParser().parse_args(argv or [])\n", False),
    # `__main__` 那一半：定義端寫對了，命令列版本卻拿不到旗標。
    ("import argparse\n"
     "def main(argv=None):\n"
     "    return argparse.ArgumentParser().parse_args(argv or [])\n"
     "if __name__ == '__main__':\n"
     "    raise SystemExit(main())\n", True),
    ("import argparse\n"
     "def main(argv=None):\n"
     "    return argparse.ArgumentParser().parse_args(argv or [])\n"
     "if __name__ == '__main__':\n"
     "    raise SystemExit(main(sys.argv[1:]))\n", False),
    # 沒有吃 argv 的進入點不在管轄範圍內（那種本來就該讀 sys.argv）。
    ("import argparse\n"
     "def main():\n"
     "    return argparse.ArgumentParser().parse_args()\n", False),
])
def test_the_argv_scanner_tells_the_two_shapes_apart(source, bad):
    """合成對照組：真實原始碼現在是乾淨的，所以每一種形狀都得自己造。

    少了這一族，把 `_reads_process_argv` 改成「永遠回 False」會全綠——整道守門等於
    關掉，而輸出上看不出任何差別。
    """
    hits = _argv_offenders([("synthetic.py", ast.parse(source))])
    assert bool(hits) is bad, f"掃描器給的是 {hits}"


def test_both_argv_floors_fire_when_the_enumerator_comes_back_empty(monkeypatch):
    """兩道下限各自的對照組：抽不到模組時它們都必須真的會叫。

    這一支是變異測試的產物，而且是**兩次**：真實的 `_project_trees()` 本來就回幾十
    個檔案、裡面本來就有四支吃 `argv` 的進入點，所以 `>= 25` 與 `>= 3` 在正常情況下
    永遠成立，放寬成 0 完全看不出差別。它們存在的唯一理由是「不正常的那一天」——
    而那一天的症狀正是「零筆違規」，跟「全部乾淨」在輸出上一模一樣。

    **兩道都要各自驗一次。** 第一版只驗了主測試那一道，於是把範圍 pin 的 `>= 3`
    放寬成 `>= 0` 的變異存活了下來。一支控制測試只證得了它真的呼叫到的那一道。
    """
    monkeypatch.setattr(sys.modules[__name__], "_project_trees", lambda: [])

    with pytest.raises(AssertionError) as main_floor:
        test_no_entry_point_falls_through_to_the_test_runners_command_line()
    assert "抽取器壞了" in str(main_floor.value), main_floor.value

    with pytest.raises(AssertionError) as scope_floor:
        test_the_argv_rule_actually_covers_more_than_one_module()
    assert "抽取器或掃描範圍壞了" in str(scope_floor.value), scope_floor.value


# ---------------------------------------------------------------------------
# 「子行程根本沒起來」的假警報要當場說清楚（`conftest.py` 的 makereport 包裝器）
# ---------------------------------------------------------------------------
#
# 這一族也屬於「套件本身的安全」：它防的不是機器被弄壞，是**人被指向錯的方向**。
# 2026-09-10 一次併行跑兩個完整回合，24 支測試同時紅在 `assert 3221225794 == 0`，
# 子行程的 stdout 與 stderr 都是空的——看起來完全像是剛剛改壞了什麼，實際上
# `0xC0000142` 是 Windows 在行程初始化階段就放棄。單獨重跑那六個檔案 742 支全綠。

def _fake_report(longrepr: str, *, failed: bool = True):
    class _Report:
        def __init__(self):
            self.failed = failed
            self.longrepr = longrepr
            self.sections: list[tuple[str, str]] = []
    return _Report()


def _drive_makereport(report):
    """把 `conftest` 那支 pluggy 包裝器**真的跑一遍**，回它 return 的東西。

    不是只呼叫 `_spawn_failure_note`：包裝器自己有兩個會靜默壞掉的地方——
    `report.failed` 的判斷，以及「必須把 report 原樣 return 回去」。
    """
    import conftest

    gen = conftest.pytest_runtest_makereport(item=None, call=None)
    next(gen)                      # 跑到 `report = yield`
    try:
        gen.send(report)
    except StopIteration as stop:
        return stop.value
    raise AssertionError("包裝器在收到 report 之後沒有結束")


def test_a_child_that_never_started_gets_named_as_a_host_problem():
    report = _fake_report("E       assert 3221225794 == 0")
    returned = _drive_makereport(report)
    # 這一句是**文件**，不是唯一的防線：變異測試實測，把 `return report` 改成
    # `return None` 會讓 pytest 自己整個回合爆掉（`1 warning`、一支測試都沒跑），
    # 所以真正抓到它的是 pytest 而不是這句斷言。留著是因為它寫出了「為什麼要
    # return」，下一個人改成舊式 `hookwrapper=True` 時才知道要一起改什麼。
    assert returned is report, (
        "pluggy 的新式包裝器必須把 report 原樣 return。")
    assert report.sections, "沒有補上任何說明"
    title, body = report.sections[0]
    assert "子行程起不來" in title
    assert "3221225794" in body and "0xC0000142" in body
    assert "第二個測試回合" in body, "沒有指出最常見的原因，人還是會從自己的 diff 查起"


def test_an_ordinary_failure_is_left_alone():
    """控制組：一般的失敗不得被貼上這張標籤，否則它就是雜訊。"""
    report = _fake_report("E       AssertionError: 佇列少了一列")
    _drive_makereport(report)
    assert not report.sections, f"一般的失敗被誤貼了說明：{report.sections}"


def test_a_passing_test_is_never_annotated():
    """`failed` 那個判斷拿掉的話，這一支是唯一會叫的。"""
    report = _fake_report("assert 3221225794 == 0", failed=False)
    _drive_makereport(report)
    assert not report.sections, "通過的測試也被貼了說明"


def test_every_listed_exit_code_really_is_a_process_init_status():
    """清單是列管制：只收「行程從來沒開始執行」的那幾個。

    `0xC00000FD`（堆疊溢位）之類是**跑起來之後**才炸的，套上同一段說明會把人指向
    錯的方向——所以它必須不在清單裡，而且這一條要有人守。三個十進位值同時釘住：
    斷言訊息印出來的就是十進位，寫錯一位數這道守門就永遠不會叫。
    """
    import conftest

    codes = conftest._SPAWN_FAILED_RCS
    assert codes, "清單空了，那個包裝器等於沒有"
    assert set(codes) == {3221225794, 3221225495, 3221225781}, (
        f"清單變了：{sorted(codes)}。新增之前先確認它真的是「行程沒起來」，"
        "不是跑到一半才炸的（例如 3221225725 ＝ 0xC00000FD 堆疊溢位）。")
    for code, meaning in codes.items():
        assert 0xC0000000 <= code <= 0xCFFFFFFF, f"{code} 不在 NTSTATUS 錯誤區間"
        assert f"{code:#010X}".replace("0X", "0x") in meaning.replace("0X", "0x"), (
            f"{code} 的說明沒有寫出對應的十六進位值：{meaning}")
    assert 3221225725 not in codes, (
        "0xC00000FD 是堆疊溢位——那是行程**跑起來之後**才炸的，套這段說明"
        "會把人指向錯的方向。")


# ---------------------------------------------------------------------------
# 自我矛盾的平台 skip：跑不到的測試看起來跟通過的測試一模一樣
# ---------------------------------------------------------------------------
# 一支測試如果**自己**把平台假掉（`monkeypatch.setattr(os, "name", "posix")`），
# 那它就不需要真的跑在那個平台上。此時再掛一個 `skipif(os.name == "nt")`，結果是
# 它在**任何**機器上都不會產生覆蓋：在 Windows 上被 skip 掉，在 POSIX 上跑的也不是
# 真實平台行為（因為它把平台改掉了）。
#
# 本專案**沒有 CI**，唯一會跑這套測試的就是這台 Windows 機器，所以「在 POSIX 上會
# 跑到」不是安慰，是空頭支票。而 skip 在報告裡是一個 `s`，不是紅字——它跟通過長得
# 幾乎一樣，這是最安靜的失效方式。
#
# 2026-09-12 實際抓到兩支（`test_process_control.py` 的
# `test_posix_undecidable_reads_as_dead` 與
# `test_signalling_a_vanished_pid_does_not_abort_the_rest`），而它們守的正是
# CLAUDE.md 特別點名的那條「三份 `_pid_alive` 對『判不出來』刻意給不同答案」的分歧
# ——規則之記載說那個分歧很重要，釘住它的測試卻從來沒有執行過。兩支移除 skipif
# 之後在 Windows 上直接通過。
#
# 判準取的是**交集**（有平台 skipif **而且**自己假平台），不是「有 skipif 就算」：
# 真的需要平台的測試（ctypes 退路、`FormatMessageW`、`taskkill`）不會去假 `os.name`。
# 量過：帶平台 skipif 的共 10 支，被抓的 2 支，其餘 8 支一支都沒誤報。

_PLATFORM_ATTRS = {("os", "name"), ("sys", "platform")}


def _platform_skipif_attrs(decorator) -> set:
    """這個 decorator 是不是 `…skipif(<平台判斷>, …)`，碰到哪些平台屬性。"""
    if not isinstance(decorator, ast.Call):
        return set()
    if not ast.unparse(decorator.func).endswith(".skipif"):
        return set()
    found = set()
    for arg in decorator.args:
        for sub in ast.walk(arg):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name):
                pair = (sub.value.id, sub.attr)
                if pair in _PLATFORM_ATTRS:
                    found.add(pair)
    return found


def _faked_platform_attrs(func) -> set:
    """主體裡把平台屬性換掉的 `setattr(os, "name", …)`。

    刻意只認**指名的常數屬性名**（`args[1]` 是字串常數），不做子字串比對——
    解釋這條規則的註解本身就會提到 `os.name`，用「出現過」當判準等於讓說明文字
    自己滿足自己（本專案 2026-09-12 才剛在 `test_text_encoding` 上踩過一次）。
    """
    found = set()
    for sub in ast.walk(func):
        if not isinstance(sub, ast.Call):
            continue
        name = (sub.func.attr if isinstance(sub.func, ast.Attribute)
                else getattr(sub.func, "id", ""))
        if name != "setattr" or len(sub.args) < 2:
            continue
        target, attr = sub.args[0], sub.args[1]
        if (isinstance(target, ast.Name) and isinstance(attr, ast.Constant)
                and isinstance(attr.value, str)):
            pair = (target.id, attr.value)
            if pair in _PLATFORM_ATTRS:
                found.add(pair)
    return found


def _self_contradicting_platform_skips(source: str, filename: str) -> list:
    """回傳「自己假平台、又被平台 skip」的測試。"""
    out = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        gated = set()
        for dec in node.decorator_list:
            gated |= _platform_skipif_attrs(dec)
        if not gated:
            continue
        faked = _faked_platform_attrs(node)
        if faked:
            out.append(
                f"{filename}:{node.lineno} {node.name} 被 "
                + "／".join(f"{m}.{a}" for m, a in sorted(gated))
                + " skip 掉，但它自己又假了 "
                + "／".join(f"{m}.{a}" for m, a in sorted(faked)))
    return out


@pytest.mark.parametrize("path", _test_files(), ids=lambda p: p.name)
def test_no_test_is_skipped_by_a_platform_it_fakes(path: Path):
    """自己把平台假掉的測試，不得再被同一個平台 skip 掉。"""
    problems = _self_contradicting_platform_skips(
        path.read_text(encoding="utf-8"), path.name)
    assert not problems, (
        "\n".join(problems)
        + "\n\n這些測試在**任何**機器上都不會產生覆蓋：在被 skip 的平台上跳過，"
        "在另一個平台上跑的也不是真實平台行為（因為它把平台改掉了）。"
        "本專案沒有 CI，唯一跑測試的就是這台 Windows 機器。"
        "\n把 skipif 拿掉即可——主體已經把平台與系統呼叫都假好了。"
        "\n真的需要平台的測試不會出現在這裡，因為它們不假 `os.name`。")


def test_the_platform_skip_scan_actually_selects_something():
    """正面對照組：選不到東西跟「全部乾淨」長得一模一樣。

    這一支數的是**帶平台 skipif 的測試總數**（不是違規數）——違規數本來就該是 0，
    拿 0 當證據等於沒有證據。2026-09-12 量到 10 支；下限給 6，留一點改動空間。
    """
    seen = 0
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(_platform_skipif_attrs(d) for d in node.decorator_list):
                seen += 1
    assert seen >= 6, (
        f"只認出 {seen} 支帶平台 skipif 的測試（2026-09-12 量到 10 支）。"
        "認不出東西的掃描器，違規數永遠是 0。")


_SKIP_SCAN_CASES = {
    "假 os.name 又被 os.name skip": (
        "import os, pytest\n"
        '@pytest.mark.skipif(os.name == "nt", reason="POSIX 分支")\n'
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(os, "name", "posix")\n', True),
    # 跨家族：兩個平台屬性問的是同一件事，換一個問法一樣是死的。少了這一筆，
    # 把判準改成「同一個屬性才算」也不會有人發現。
    "假 os.name 又被 sys.platform skip": (
        "import os, sys, pytest\n"
        '@pytest.mark.skipif(sys.platform != "linux", reason="POSIX 分支")\n'
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(os, "name", "posix")\n', True),
    # 真的需要平台的測試：有 skipif，但不假平台 → 放行。
    "有 skipif 但不假平台": (
        "import os, pytest, ctypes\n"
        '@pytest.mark.skipif(os.name != "nt", reason="ctypes 退路是 Windows 專用的")\n'
        "def test_x():\n"
        "    ctypes.windll.kernel32.GetLastError()\n", False),
    # 一般的跨平台測試：假平台，但沒有 skipif → 放行（這才是正確寫法）。
    "假平台但沒有 skipif": (
        "import os\n"
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(os, "name", "posix")\n', False),
    # 近似案例：有平台 skipif，但假的是**別的**東西 → 放行。
    # 少了這一筆，「假平台那一側的 _PLATFORM_ATTRS 過濾」刪掉也不會有人發現。
    "有平台 skipif 但假的是別的東西": (
        "import os, pytest\n"
        '@pytest.mark.skipif(os.name != "nt", reason="Windows 專用")\n'
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(os, "environ", {})\n', False),
    # 近似案例：假了平台，但 skipif 判的**不是**平台 → 放行。
    # 少了這一筆，「skipif 那一側的 _PLATFORM_ATTRS 過濾」刪掉也不會有人發現。
    "skipif 判的不是平台": (
        "import os, pytest\n"
        "anthropic = None\n"
        '@pytest.mark.skipif(anthropic is None, reason="可選相依沒裝")\n'
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(os, "name", "posix")\n', False),
    # ⚠️ 上面那一筆**殺不掉** skipif 那一側的 `_PLATFORM_ATTRS` 過濾：
    # `anthropic is None` 裡根本沒有 `X.Y` 這種屬性存取，所以把過濾整個刪掉，
    # 那一筆的結果還是「找不到平台屬性」＝照樣放行。要咬得住那個變異，語料裡必須
    # 有一筆「skipif 判的是**某個屬性**，只是那個屬性不是平台」。
    # 版本閘＋假平台是完全合理的組合（在舊版 Python 上跳過，本體照樣假 os.name），
    # 把它誤報出來就是逼人把對的寫法改掉。
    "skipif 判的是別的屬性（版本）": (
        "import os, sys, pytest\n"
        '@pytest.mark.skipif(sys.version_info < (3, 15), reason="要 3.15")\n'
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(os, "name", "posix")\n', False),
    # `xfail` 不是 `skip`：它**照跑**，只是把失敗記成預期。所以「用平台條件掛
    # xfail ＋ 本體假平台」完全沒有本段要防的病（測試有執行、有覆蓋）。少了這一
    # 筆，把「decorator 名字得是 skipif」那道過濾刪掉也不會有人發現。
    "平台條件掛的是 xfail 不是 skip": (
        "import os, pytest\n"
        '@pytest.mark.xfail(os.name == "nt", reason="Windows 上還沒修")\n'
        "def test_x(monkeypatch):\n"
        '    monkeypatch.setattr(os, "name", "posix")\n', False),
    # 說明文字提到 os.name 不算——判準走 AST 的指名參數，不是子字串。
    "只有說明文字提到": (
        "import os, pytest\n"
        '@pytest.mark.skipif(os.name != "nt", reason="Windows 專用")\n'
        "def test_x():\n"
        "    # 這支不會去 setattr(os, \"name\", ...)，只是講到它。\n"
        "    assert os.name\n", False),
}


@pytest.mark.parametrize("label", sorted(_SKIP_SCAN_CASES))
def test_the_platform_skip_scan_tells_the_shapes_apart(label):
    """合成語料：該抓的抓到、不該抓的放行。

    **放行的那幾筆才是重點。** 判準裡兩道 `_PLATFORM_ATTRS` 過濾、以及「兩側都要
    有」的交集，都是**收緊**步驟——只有近似案例殺得死對應的變異。只放必抓語料的話，
    把過濾整個刪掉照樣全綠。
    """
    source, should_catch = _SKIP_SCAN_CASES[label]
    got = _self_contradicting_platform_skips(source, f"{label}.py")
    if should_catch:
        assert got, f"「{label}」應該被抓到，卻放行了"
    else:
        assert not got, f"「{label}」不該被抓，卻報了：{got}"


# ---------------------------------------------------------------------------
# `raising=False` 的替身必須還是換到真的東西
# ---------------------------------------------------------------------------
#
# `monkeypatch.setattr(mod, "name", ...)` 預設會在 `name` 不存在時**丟例外**，
# 那是一道很有用的防線：它保證替身真的接到被測的那個東西。`raising=False` 把那道
# 防線關掉——於是名字打錯、或正式碼把那個屬性改名之後，`setattr` 會安靜地**新增**
# 一個沒有人讀的屬性，測試的前置設定變成什麼都沒做。
#
# 症狀是沒有症狀：測試照樣綠，因為它驗的往往是「沒有發生某件事」，而那件事本來就
# 不會發生了。2026-09-12 本專案剛付過一次同型的代價，那次連
# `raising=False` 都不需要就漏掉了；這一支關的是更容易發生的那個版本。
#
# 判準刻意只處理**解析得到**的目標（`import x as y` 這種別名），解析不到的
# （`type(pid_file)`、區域變數）留給人看——跟編碼守門同一個理由：會亂叫的守門會被
#關掉。

# 例外：**目標屬性本來就不該存在**，`raising=False` 正是為了這件事。
# 每一筆都要寫理由。
_RAISING_FALSE_EXEMPT = {
    ("socket", "AF_UNIX"): (
        "Windows 上根本沒有 `AF_UNIX`，而這幾支測試就是要模擬 POSIX 那條路；"
        "`raising=False` 在這裡是**必要**的，不是偷懶。"),
}


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """`import discord_bot as b` → `{"b": "discord_bot"}`（含不改名的 import）。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
    return aliases


def _raising_false_sites(path: Path) -> list[tuple[int, str, str]]:
    """`(行號, 目標模組名, 屬性名)`；解析不到目標的一律跳過。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    aliases = _module_aliases(tree)
    out: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and "setattr" in ast.unparse(node.func)):
            continue
        if not any(kw.arg == "raising"
                   and isinstance(kw.value, ast.Constant)
                   and kw.value.value is False
                   for kw in node.keywords):
            continue
        if len(node.args) < 2:
            continue
        target, attr = node.args[0], node.args[1]
        if not (isinstance(target, ast.Name)
                and isinstance(attr, ast.Constant)
                and isinstance(attr.value, str)):
            continue                      # 解析不到目標 → 留給人看
        module = aliases.get(target.id)
        if module:
            out.append((node.lineno, module, attr.value))
    return out




# 掃到的站點數下限。抽成常數＋函式，是為了讓這個下限**自己**可以被對照組測到：
# 直接寫在測試裡的 `assert checked >= 4` 擋不住有人把它改成 0（`x >= 0` 永遠真）。
# 要分辨得出來，對照組得餵一份**介於兩個門檻之間**的數字——非空、但低於下限。
# （同 `test_pid_liveness._assert_scan_floor` 的處置。）
_RAISING_SCAN_FLOOR = 4


def _assert_raising_scan_floor(checked: int) -> None:
    assert checked >= _RAISING_SCAN_FLOOR, (
        f"只檢查到 {checked} 個 `raising=False` 站點，低於下限 "
        f"{_RAISING_SCAN_FLOOR}——抓法可能過期了，而抓不到東西的掃描器"
        "違規數永遠是 0。")

def _raising_false_offenders(paths, exempt) -> tuple[int, list[str]]:
    """`(檢查過幾個站點, 違規訊息)`。

    **抽成函式不是排版。** 真資料現在是乾淨的，所以「把違規組成字串」那一行在真
    資料上一次都不會執行——把 `offenders.append(...)` 換成別的，整組測試依然全綠
    （2026-09-17 變異測試實測 SURVIVED）。抽出來之後底下的合成語料才走得到這條路。
    這條判語本專案早就記著，而這支守門第一版**又
    犯了一次**——所以它值得再記一次：**凡是「違規清單」型的守門，偵測都要抽成吃
    路徑的函式，並且用種進去的違規去測它。**
    """
    checked = 0
    offenders: list[str] = []
    for path in paths:
        for lineno, module_name, attr in _raising_false_sites(path):
            if (module_name, attr) in exempt:
                continue
            # **內建名稱是合法的例外，而且必須用 `raising=False`。**
            # `monkeypatch.setattr(mod, "open", fake, raising=False)` 是在模組的
            # 命名空間裡**遮蔽內建**——模組本來就沒有 `open` 這個屬性，所以存在性
            # 檢查一定會失敗，關掉它是這個手法的前提，不是偷懶。
            # （這是這支守門第一次跑就抓到的唯一一筆，實地確認過：
            # `test_dorossi_usage_record` 那筆遮蔽 `open` 之後，下一行還補了
            # `monkeypatch.setattr("builtins.open", ...)`，後者才是真正生效的那個。）
            if hasattr(builtins, attr):
                continue
            try:
                module = importlib.import_module(module_name)
            except Exception:             # pylint: disable=broad-except
                continue                  # 匯入不了就不是這支要管的事
            checked += 1
            if not hasattr(module, attr):
                offenders.append(
                    f"{path.name}:{lineno} 把 `{module_name}.{attr}` 換掉，"
                    "但那個屬性根本不存在")
    return checked, offenders


def _stale_exemptions(exempt) -> list[str]:
    """豁免清單裡「其實已經存在」的那些——也就是不再需要的豁免。"""
    stale: list[str] = []
    for (module_name, attr), reason in exempt.items():
        if not reason.strip():
            stale.append(f"`{module_name}.{attr}` 沒有寫理由")
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception:                 # pylint: disable=broad-except
            continue
        if hasattr(module, attr):
            stale.append(
                f"`{module_name}.{attr}` 現在存在了，這筆豁免不再需要")
    return stale

def test_a_raising_false_double_still_patches_something_real():
    """關掉存在性檢查的替身，換的必須還是一個真的存在的屬性。

    不然前置設定會安靜地新增一個沒有人讀的屬性，而測試照樣綠——正式碼改名之後
    **沒有任何症狀**。
    """
    checked, offenders = _raising_false_offenders(
        sorted(TEST_ROOT.glob("test_*.py")), _RAISING_FALSE_EXEMPT)
    _assert_raising_scan_floor(checked)
    assert True, (
        f"只檢查到 {checked} 個 `raising=False` 站點（2026-09-17 量到 10 個"
        "可解析且非內建名稱）——抓法可能過期了，而抓不到東西的掃描器"
        "違規數永遠是 0。")
    assert not offenders, (
        "\n".join(offenders)
        + "\n\n`raising=False` 關掉了 monkeypatch 的存在性檢查，所以這個 "
        "`setattr` 只是**新增**了一個沒有人讀的屬性，前置設定等於沒做。"
        "\n屬性真的不該存在（例如平台專屬的常數）就加進 "
        "`_RAISING_FALSE_EXEMPT` 並寫上理由；否則把名字修好，或直接拿掉 "
        "`raising=False` 讓 monkeypatch 自己擋。")


def test_every_raising_false_exemption_is_still_needed():
    """豁免反查：被豁免的屬性如果**其實存在**，那筆豁免就該拿掉。

    一筆不再需要的豁免會繼續放行它涵蓋的東西，而且沒有任何症狀——與
    `_OWNER_ONLY_SLASH` 同一個形狀。
    """
    stale = _stale_exemptions(_RAISING_FALSE_EXEMPT)
    assert not stale, (
        "\n".join(stale)
        + "\n\n拿掉不再需要的豁免，讓那些站點回到一般規則底下。")


def test_the_raising_false_scan_tells_the_shapes_apart(tmp_path):
    """合成語料：抓得到不存在的、放行存在的。

    真資料現在是乾淨的，所以上面那支的「組違規字串」那一行在真資料上一次都不會
    執行——沒有這一支，把 `offenders.append(...)` 換成 `pass` 也不會有人發現。
    """
    bad = tmp_path / "test_bad.py"
    bad.write_text(
        "import json\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(json, 'no_such_attribute', 1, raising=False)\n",
        encoding="utf-8")
    sites = _raising_false_sites(bad)
    assert sites and sites[0][1:] == ("json", "no_such_attribute"), sites
    assert not hasattr(importlib.import_module("json"), "no_such_attribute")

    good = tmp_path / "test_good.py"
    good.write_text(
        "import json\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(json, 'dumps', 1, raising=False)\n",
        encoding="utf-8")
    sites = _raising_false_sites(good)
    assert sites and hasattr(importlib.import_module("json"), sites[0][2])

    # 必放行：解析不到目標的一律跳過（`type(x)`、區域變數），否則這支會對著
    # 一堆它看不懂的東西亂叫。
    opaque = tmp_path / "test_opaque.py"
    opaque.write_text(
        "def test_x(monkeypatch, thing):\n"
        "    monkeypatch.setattr(type(thing), 'unlink', None, raising=False)\n",
        encoding="utf-8")
    assert _raising_false_sites(opaque) == []
def test_the_raising_false_collector_runs_on_a_seeded_violation(tmp_path):
    """把違規**種進去**，確認收集器真的會收集。

    這是上面那支唯一的執行證據：真資料乾淨 ⇒ 收集那一行永遠不執行。用注入，
    不要用 grep 找形狀（本專案 §8.58 同一條判語）。
    """
    bad = tmp_path / "test_seeded_raising.py"
    bad.write_text(
        "import json\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(json, 'no_such_attribute', 1, raising=False)\n",
        encoding="utf-8")
    checked, offenders = _raising_false_offenders([bad], {})
    assert checked == 1, f"種進去的站點沒有被檢查到：{checked}"
    assert offenders and "no_such_attribute" in offenders[0], offenders

    # 必放行一：屬性真的存在。
    ok = tmp_path / "test_ok_raising.py"
    ok.write_text(
        "import json\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(json, 'dumps', 1, raising=False)\n",
        encoding="utf-8")
    assert _raising_false_offenders([ok], {})[1] == []

    # 必放行二：**沒有** `raising=False` 的 setattr 不歸這支管——monkeypatch 自己
    # 就會擋。少了這一筆，「不再過濾 raising=False」那個變異會存活。
    plain = tmp_path / "test_plain_setattr.py"
    plain.write_text(
        "import json\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(json, 'no_such_attribute', 1)\n",
        encoding="utf-8")
    checked, offenders = _raising_false_offenders([plain], {})
    assert (checked, offenders) == (0, []), (
        "沒有 `raising=False` 的 setattr 被算進來了——那種寫法 monkeypatch 自己"
        f"就會丟例外，不需要這支守門：{checked}, {offenders}")

    # 必放行三：豁免清單要真的擋得住。
    assert _raising_false_offenders(
        [bad], {("json", "no_such_attribute"): "測試用"})[1] == []


def test_a_no_longer_needed_exemption_is_reported(tmp_path):
    """豁免反查的合成對照組：指著一個**存在**的屬性就該被判定為過期。

    真資料裡唯一那筆豁免（`socket.AF_UNIX`）在這台機器上本來就不存在，所以真資料
    永遠走不到「過期」那條路——沒有這一支，把反查整個拿掉也不會有人發現。
    """
    assert _stale_exemptions({("json", "dumps"): "這個屬性其實存在"}), (
        "指著一個存在的屬性的豁免沒有被判定為過期")
    assert _stale_exemptions({("json", "no_such_attribute"): "有理由"}) == []
    assert _stale_exemptions({("json", "no_such_attribute"): "  "}), (
        "沒有寫理由的豁免應該被抓出來")


def test_the_raising_scan_floor_can_tell_too_few_from_enough():
    """下限的對照組：非空但低於下限**必須**紅。

    沒有這一支，把下限改成 0 不會有任何症狀——而那正是「掃不到東西也算過」的
    最省力寫法。餵的數字刻意介於 0 與下限之間。
    """
    _assert_raising_scan_floor(_RAISING_SCAN_FLOOR)          # 剛好夠 → 放行
    with pytest.raises(AssertionError):
        _assert_raising_scan_floor(_RAISING_SCAN_FLOOR - 1)  # 差一個 → 要紅
    with pytest.raises(AssertionError):
        _assert_raising_scan_floor(1)                        # 非空但太少 → 要紅


# ---------------------------------------------------------------------------
# 第三條：測試不得寫進 repo 裡的正式狀態（conftest 的稽核掛勾 ＋ 預設導開）
# ---------------------------------------------------------------------------
# 2026-09-19 量到的事故：佇列還原測試從 09-07 起每跑一次就往正在服役的
# `dorossi_events.ndjson` 附加假事件，到那天 1,713 行裡有 1,602 行是測試寫的。
# 下面每一支都在驗「守門真的會咬」，而不是「守門存在」。

_GUARD_PROBE = Path(PKG_ROOT.parent) / ".repo_write_guard_probe"


def _drain(guard) -> list:
    hits = list(guard.violations)
    guard.violations.clear()
    return hits


def _remove_probe_unguarded(guard) -> None:
    """清掉探針檔——**先把守門關掉再刪**。

    只有守門**部分**壞掉時才會走到這裡（例如「附加模式不算寫入」那種）：檔案被建了，
    但刪檔仍然會被擋。2026-09-19 的變異測試就是這樣在 repo 根目錄留下一個探針檔。"""
    saved, guard.test = guard.test, None
    try:
        if _GUARD_PROBE.exists():
            _GUARD_PROBE.unlink()
    finally:
        guard.test = saved


def test_a_write_into_the_repo_is_blocked_and_named(repo_write_guard, tmp_path):
    """正對照：五種寫法都要被擋下（丟 PermissionError、檔案沒被動到）並記下路徑。

    目標刻意是一個**不存在、也沒有任何程式在用**的檔名：萬一守門壞了，最壞只是
    在 repo 根目錄留下一個空檔（finally 會清掉），不會碰到任何正式資料——守門的
    測試本身不該有破壞力。"""
    assert not _GUARD_PROBE.exists(), "上一次的探針檔還在——先清掉再測"
    src = tmp_path / "src.txt"
    src.write_text("x", encoding="utf-8")        # 在 tmp 裡寫：不該被擋
    try:
        attempts = [
            ("open", lambda: open(_GUARD_PROBE, "a", encoding="utf-8").close()),
            ("os.rename", lambda: os.replace(src, _GUARD_PROBE)),
            ("open", lambda: _GUARD_PROBE.write_bytes(b"x")),
            ("os.mkdir", lambda: _GUARD_PROBE.mkdir()),
            ("os.remove", lambda: _GUARD_PROBE.unlink()),
        ]
        for event, attempt in attempts:
            with pytest.raises(PermissionError):
                attempt()
            hits = _drain(repo_write_guard)
            assert hits == [(event, _GUARD_PROBE.name)], (event, hits)
        assert not _GUARD_PROBE.exists(), "被擋下的寫入還是落地了"
        assert src.read_text(encoding="utf-8") == "x", "tmp 裡的來源檔被動到了"
    finally:
        _remove_probe_unguarded(repo_write_guard)   # 只有守門壞掉時才有東西可刪
        _drain(repo_write_guard)


@pytest.mark.parametrize("rel,protected", [
    ("dorossi_events.ndjson", True),
    ("audit.ndjson", True),
    ("events.ndjson", True),
    ("dorossi_session.json", True),
    ("todo_prompt.md", True),
    (os.path.join(".backup", "todo_prompt.md.20260101T000000.000.bak"), True),
    (os.path.join("output", "alice", "a.png"), True),
    (os.path.join("axiomatic", "scratch.py"), True),
    (os.path.join("axiomatic", "__pycache__", "x.cpython-314.pyc"), False),
    (os.path.join(".pytest_cache", "v", "cache", "lastfailed"), False),
    (".coverage", False),
])
def test_the_guard_predicate_covers_state_and_spares_tool_caches(
        repo_write_guard, rel, protected):
    """判準本身：repo 裡的正式檔案全在保護範圍，編譯快取與測試工具快取不在。"""
    full = os.path.join(str(PKG_ROOT.parent), rel)
    assert (repo_write_guard.target(full) is not None) is protected, rel


def test_the_guard_predicate_is_not_fooled_by_a_shared_prefix(repo_write_guard,
                                                              tmp_path,
                                                              monkeypatch):
    """近似案例：名字以 repo 根目錄開頭的**另一個**目錄不是 repo；tmp 不是 repo；
    而一個裸檔名在 repo 根目錄當工作目錄時**是** repo（相對路徑照工作目錄解析）。"""
    root = str(PKG_ROOT.parent)
    assert repo_write_guard.target(root + "_backup" + os.sep + "x.txt") is None
    assert repo_write_guard.target(str(tmp_path / "x.txt")) is None
    monkeypatch.chdir(root)
    assert repo_write_guard.target("dorossi_events.ndjson") == "dorossi_events.ndjson"


def test_the_side_effect_logs_point_away_from_the_repo_in_every_test(
        repo_write_guard):
    """預設導開：列在表上的每一個常數，在這支測試裡都不在 repo 裡、而且檔名沒變。

    再實際走一次正式的寫入路徑（`_dorossi_event`），確認它寫到暫存目錄、守門一次
    都沒有被觸發——那正是 09-07 起漏掉的那一條。"""
    import discord_bot as b

    for mod_name, attr in repo_write_guard.side_effect_logs:
        mod = importlib.import_module(mod_name)
        value = Path(getattr(mod, attr))
        assert repo_write_guard.target(value) is None, (
            f"{mod_name}.{attr} 在測試裡仍然指向 repo：{value}")
    before = b.DOROSSI_EVENTS_FILE
    b._dorossi_event("guard_probe", uid="0")
    assert _drain(repo_write_guard) == []
    assert before.read_text(encoding="utf-8").count("guard_probe") == 1


def _append_mode_constants(tree) -> set:
    """正式程式碼裡以附加模式開啟的模組常數：`X.open("a", …)` 與 `open(X, "a", …)`。"""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        mode = target = None
        if (isinstance(node.func, ast.Attribute) and node.func.attr == "open"
                and isinstance(node.func.value, ast.Name)):
            target = node.func.value.id
            if node.args and isinstance(node.args[0], ast.Constant):
                mode = node.args[0].value
        elif (isinstance(node.func, ast.Name) and node.func.id == "open"
              and node.args and isinstance(node.args[0], ast.Name)):
            target = node.args[0].id
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if target and target.isupper() and isinstance(mode, str) and "a" in mode:
            out.add(target)
    return out


def test_every_append_mode_log_is_redirected_by_default(repo_write_guard):
    """導開清單兩個方向對帳。

    * 正式程式碼裡每一個以附加模式開的模組常數都要在清單上——新增一個 append 的
      記錄檔而忘了列，下一支碰到它的測試就會往正式檔案寫（守門會擋、測試會紅，
      但那是事後；清單是事前）。
    * 清單上每一個常數都要真的存在——改名之後清單上的名字就什麼都不導開。

    正對照：掃描要先找得到已知的五個，否則「沒有漏列」會空轉通過。"""
    found = set()
    # `test/` 照同一個判準過濾（手動 e2e 腳本搬家前在範圍內，照舊）。
    for path in sorted(PKG_ROOT.glob("*.py")) + sorted(TEST_ROOT.glob("*.py")):
        if path.name.startswith("test_") or path.name == "conftest.py":
            continue
        for name in _append_mode_constants(
                ast.parse(path.read_text(encoding="utf-8"))):
            found.add((path.stem, name))
    assert {("discord_bot", "AUDIT_FILE"), ("discord_bot", "DOROSSI_EVENTS_FILE"),
            ("discord_bot", "GENERATE_HISTORY_FILE"),
            ("dorossi_backend", "DOROSSI_USAGE_FILE"),
            ("_webrunner_shared", "EVENTS_FILE")} <= found, found
    listed = set(repo_write_guard.side_effect_logs)
    assert found <= listed, f"這些 append 的記錄檔沒有預設導開：{sorted(found - listed)}"
    for mod_name, attr in listed:
        assert hasattr(importlib.import_module(mod_name), attr), (
            f"清單上的 {mod_name}.{attr} 已經不存在，什麼都沒導開")


def test_the_append_scan_tells_the_shapes_apart():
    """上一支的掃描器自己的對照組：兩種寫法都認得、讀取模式與小寫名字不算。"""
    src = ("def f():\n"
           "    A_FILE.open('a', encoding='utf-8')\n"
           "    open(B_FILE, 'ab')\n"
           "    open(C_FILE, mode='a')\n"
           "    D_FILE.open('r')\n"
           "    local.open('a')\n")
    assert _append_mode_constants(ast.parse(src)) == {"A_FILE", "B_FILE", "C_FILE"}


def test_leak_canary_is_only_for_the_subprocess_check(repo_write_guard):
    """**只給下面那支用的**：設了環境變數才會嘗試寫進 repo，平常直接略過。"""
    if os.environ.get("REPO_WRITE_GUARD_CANARY") != "1":
        pytest.skip("只在子行程的守門驗證裡執行")
    try:
        with open(_GUARD_PROBE, "a", encoding="utf-8") as fh:
            fh.write("leak")
    except PermissionError:
        pass                                  # 被擋下，但這支測試照樣要被判失敗
    finally:
        _remove_probe_unguarded(repo_write_guard)


def test_a_test_that_writes_into_the_repo_is_failed_even_if_it_swallows_the_error():
    """端對端：一支把 PermissionError 吞掉、本身看起來會通過的測試，必須被判失敗。

    這才是正式程式碼的常態——`_dorossi_event` 之類都把 `OSError` 吞成一行 stderr。
    光擋不報的話，那一次寫入沒發生但也沒有人知道；所以夾具在收尾時讓它紅。用子行程
    跑，因為夾具的收尾要在真的測試回合裡才看得到。"""
    import subprocess

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"{__file__}::test_leak_canary_is_only_for_the_subprocess_check"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, cwd=str(PKG_ROOT.parent),
        env={**os.environ, "PYTHONIOENCODING": "utf-8",
             "REPO_WRITE_GUARD_CANARY": "1"})
    tail = out.stdout[-1500:]
    assert out.returncode != 0 and "1 error" in tail, tail
    assert "測試不得寫進" in out.stdout or "試圖寫進 repo" in out.stdout, tail
    assert _GUARD_PROBE.name in out.stdout, tail
    assert not _GUARD_PROBE.exists()


# ---- 點名放行（`repo_write_ok`）--------------------------------------------------
# 有一支測試**必須**碰正式路徑：`test_supervisor` 的端對端鎖測試，子行程的 bot 讀的
# 是寫死的正式鎖路徑。放行只能逐條點名、寫理由；下面兩支分別釘「只放行被點名的那
# 一個」與「沒點名或沒理由就不算數」。

_GUARD_PROBE_OTHER = _GUARD_PROBE.with_name(_GUARD_PROBE.name + "_other")


@pytest.mark.repo_write_ok(
    _GUARD_PROBE.name,
    reason="守門自己的測試：驗證點名放行只放行被點名的那一個路徑。")
def test_a_named_path_is_allowed_and_nothing_else_is(repo_write_guard):
    """被點名的探針可以寫；**同一支測試裡**沒點名的另一個檔照樣被擋。

    只驗前半的話，「有標記就整支測試全放行」也會通過——那等於一個萬用豁免。"""
    assert not _GUARD_PROBE.exists() and not _GUARD_PROBE_OTHER.exists()
    try:
        with open(_GUARD_PROBE, "a", encoding="utf-8") as fh:
            fh.write("ok")
        assert _GUARD_PROBE.exists()
        assert _drain(repo_write_guard) == []
        with pytest.raises(PermissionError):
            with open(_GUARD_PROBE_OTHER, "a", encoding="utf-8") as fh:
                fh.write("leak")
        assert not _GUARD_PROBE_OTHER.exists()
        assert [rel for _event, rel in _drain(repo_write_guard)] == [
            os.path.normcase(_GUARD_PROBE_OTHER.name)]
    finally:
        saved, repo_write_guard.test = repo_write_guard.test, None
        try:
            for probe in (_GUARD_PROBE, _GUARD_PROBE_OTHER):
                if probe.exists():
                    probe.unlink()
        finally:
            repo_write_guard.test = saved


def test_the_allow_marker_demands_paths_and_a_reason(repo_write_guard):
    """沒標記 → 什麼都不放行；有標記但沒點名路徑或沒寫理由 → 不算數（夾具會讓那支
    測試失敗）；點名的路徑正規化成掛勾比對的形狀。"""
    parse = repo_write_guard.allowed_from_marker
    assert parse(None) == frozenset()
    ok = pytest.mark.repo_write_ok("sub/./x.lock", reason="理由").mark
    assert parse(ok) == {os.path.normcase(os.path.join("sub", "x.lock"))}
    for bad in (pytest.mark.repo_write_ok("x.lock").mark,
                pytest.mark.repo_write_ok("x.lock", reason="  ").mark,
                pytest.mark.repo_write_ok(reason="理由").mark):
        with pytest.raises(ValueError):
            parse(bad)


# ---- 瀏覽器函式庫的匯入期日誌 --------------------------------------------------
# 那個函式庫匯入時就以附加模式開一個相對路徑的日誌檔，而 repo 根目錄下那個名字在大小
# 寫不分的檔案系統上就是批次正在寫的正式日誌。`conftest` 在那一個子模組執行期間把
# 工作目錄換到暫存區（見那裡的說明）。

def test_the_browser_library_log_is_parked_outside_the_repo(repo_write_guard):
    """看**結果**：函式庫掛上的每一個檔案 handler 都指在 repo 外、在停放目錄裡。

    `conftest` 裡的模組路徑是函式庫的內部名稱；改名之後 finder 會安靜地不再命中，
    這支會紅，因為它不看那個字串。正對照：至少要有一個檔案 handler，否則「沒有
    handler」會被讀成「都在外面」。"""
    import importlib.util
    import logging

    if importlib.util.find_spec("je_web_runner") is None:
        pytest.skip("這個直譯器沒有裝瀏覽器函式庫")
    import je_web_runner  # noqa: F401,PLC0415

    handlers = [h for h in logging.getLogger("WEBRunner").handlers
                if isinstance(h, logging.FileHandler)]
    assert handlers, "函式庫沒有掛檔案 handler——這支的前提變了，重新量過"
    root = repo_write_guard.repo_root
    for handler in handlers:
        full = os.path.normcase(os.path.abspath(handler.baseFilename))
        assert not full.startswith(root + os.sep), handler.baseFilename
        assert os.path.samefile(os.path.dirname(full),
                                repo_write_guard.browser_log_dir), handler.baseFilename


def test_the_gui_library_log_is_parked_outside_the_repo(repo_write_guard):
    """同上，對桌面自動化函式庫。兩代寫法都要擋：套件庫的發佈版匯入時以相對路徑開
    `AutoControlGUI.log`（conftest 的 finder 換 cwd），原始碼樹 2026-09-22 起改讀
    `JE_AUTOCONTROL_LOG_FILE`、沒設就寫使用者家目錄（conftest 把變數指到同一個暫存目錄）。

    2026-09-20 以前沒停放，repo 根目錄那份正式記錄檔的 9,716 行裡大半是測試寫的
    （逐小時計數對得上跑測試的時段）。看結果、不看模組名字串，理由與上一支相同。"""
    import importlib.util
    import logging

    if importlib.util.find_spec("je_auto_control") is None:
        pytest.skip("這個直譯器沒有裝桌面自動化函式庫")
    import je_auto_control  # noqa: F401,PLC0415

    handlers = [h for h in logging.getLogger("AutoControlGUI").handlers
                if isinstance(h, logging.FileHandler)]
    assert handlers, "函式庫沒有掛檔案 handler——這支的前提變了，重新量過"
    root = repo_write_guard.repo_root
    for handler in handlers:
        full = os.path.normcase(os.path.abspath(handler.baseFilename))
        assert not full.startswith(root + os.sep), handler.baseFilename
        assert os.path.samefile(os.path.dirname(full),
                                repo_write_guard.gui_log_dir), handler.baseFilename


_BLOCKED_PYTEST_PLUGINS = ("je_auto_control", "locust", "langsmith_plugin", "faker",
                           "anyio", "rerunfailures")

# 載得進來、而且**查過之後刻意留著**的第三方外掛：鍵是 pytest11 進入點的名字，值是理由。
_DECLARED_PYTEST_PLUGINS = {
    "timeout": "`pytest.ini` 的 timeout=900 靠它；沒有它整套就沒有單支上限",
    "asyncio": "本專案**不依賴**它：整套 0 支 `async def test_`、0 個 asyncio "
               "marker，bot 那側的 async 全由測試自己的 `asyncio.run()` 驅動。"
               "留著是因為擋掉只省 0.05 秒（對帳見本檔最後一節）",
    "xdist": "平行跑整套用的（`-n 6 --dist loadfile`，450 → 118 秒）",
    "xdist.looponfail": "xdist 自己註冊的第二個進入點，跟著它一起載進來",
    "pytest_cov": "本專案沒在量覆蓋率，但擋掉會讓 `--cov` 當場變成用法錯誤；"
                  "留著等真的要量的那天",
    "random_order": "順序相依的檢查工具（`--random-order-bucket=global`）；"
                    "沒有旗標時它不洗牌",
}


def _blocked_names_in_ini(pytestconfig) -> list[str]:
    """從**這一回合真的讀到的**那個 ini 檔裡，取出 `addopts` 的每一個 `-p no:<名字>`。"""
    ini = pytestconfig.inipath
    assert ini is not None, "這一回合沒有讀到任何 ini 檔——對帳的來源不見了"
    parser = configparser.ConfigParser()
    parser.read_string(ini.read_text(encoding="utf-8"))
    tokens = parser["pytest"]["addopts"].split()
    return [value[len("no:"):] for flag, value in zip(tokens, tokens[1:])
            if flag == "-p" and value.startswith("no:")]


def test_the_unused_third_party_pytest_plugins_are_blocked(pytestconfig):
    """`pytest.ini` 的 `addopts` 要真的在**這一回合**生效，不是只寫在檔案裡。

    `je_auto_control` 那一項是正確性：它的外掛讓 pytest 一啟動就匯入整個套件，比
    conftest 的停放 finder 還早，正式的 `AutoControlGUI.log` 就被測試寫進去。其餘幾個
    只是啟動時間（2.9 → 0.9 → 0.48 秒）。正面對照：確認查得到一個**應該**在的外掛，
    否則「查不到」會被讀成「擋掉了」。

    最後那條相等是**另一個方向**的。`is_blocked` 查的是「有沒有被擋」而不是「那個外掛
    在不在」，所以把 `-p no:locust` 從 ini 裡刪掉，連沒裝 locust 的 `.venv` 都會紅（實測
    2026-09-20：紅在 `locust` 沒有被擋那一行）——tuple → ini 這個方向本來就守得住。守不住
    的是反過來：**有人在 `addopts` 多擋一個、卻沒寫進這份 tuple**，那個外掛的「為什麼擋」
    就沒有任何地方記著，而且它不會有症狀。

    刻意**沒有**查的是第三種：清單裡留著一個永遠不會被載入的名字（外掛改名或這台機器
    再也不會裝它）。那是 `CLAUDE.md` 講過的 dead ceremony，但這裡沒辦法用「必須裝得到」
    來查——兩個直譯器的套件集合本來就不一樣，那樣寫會在 `.venv` 上對 locust 誤報。"""
    pm = pytestconfig.pluginmanager
    assert pm.get_plugin("timeout") is not None, (
        "連 pytest-timeout 都查不到——這支的查法壞了，不是外掛都被擋掉了")
    for name in _BLOCKED_PYTEST_PLUGINS:
        assert pm.is_blocked(name), (
            f"`{name}` 沒有被擋：`pytest.ini` 的 addopts 沒生效或被改掉了")
        assert pm.get_plugin(name) is None, f"`{name}` 還是被載入了"
    assert sorted(_blocked_names_in_ini(pytestconfig)) == sorted(_BLOCKED_PYTEST_PLUGINS), (
        "`pytest.ini` 的 `-p no:…` 與 `_BLOCKED_PYTEST_PLUGINS` 對不起來。兩邊要一起改："
        "只改一邊的話，在沒裝那個外掛的直譯器上不會有任何症狀")


def test_every_loaded_pytest_plugin_is_declared_or_blocked(pytestconfig):
    """**在全域環境裡裝一個帶 pytest11 進入點的套件，它就會默默改變這套測試的跑法。**

    封鎖名單擋得住「已經知道的那幾個」，擋不住下一個——而那正是 `je_auto_control`
    當初的形狀：沒有人選擇要載入它，它只是裝在那裡，於是每一個測試行程都往正式的
    `AutoControlGUI.log` 寫一行。所以這一支反過來查：**這一回合真的載進來的每一個
    第三方外掛，都必須事先被宣告過**（`_DECLARED_PYTEST_PLUGINS`，每一筆附理由）。

    紅了不一定是壞事，代表有人裝了新東西：確認它對測試沒有副作用就連理由一起加進
    宣告，有副作用就加進 `_BLOCKED_PYTEST_PLUGINS` 與 `pytest.ini`。

    **刻意只比一個方向。** 「宣告了卻沒裝」是這台機器的常態而不是錯誤：兩個直譯器的
    套件集合本來就不一樣（`random_order` 只有 `py -3` 有），拿相等去比會逼人把宣告
    改寫成「當下裝了什麼」，那等於刪掉「查過、決定留著」這個資訊——同
    `CLAUDE.md` 裡「permission list 不是 as-built list」那一段。"""
    pm = pytestconfig.pluginmanager
    loaded = sorted({pm.get_name(plugin) for plugin, _dist in pm.list_plugin_distinfo()}
                    - {None})
    assert "timeout" in loaded and len(loaded) >= 2, (
        f"查到的進入點外掛只有 {loaded}——這支的查法壞了，不是環境變乾淨了")
    undeclared = [name for name in loaded if name not in _DECLARED_PYTEST_PLUGINS]
    assert not undeclared, (
        f"這些外掛載進來了卻沒有人宣告過：{undeclared}。它們現在就在改變這一套測試的"
        f"跑法（夾具、hook、收集順序都可能）。確認無害就連理由寫進 "
        f"`_DECLARED_PYTEST_PLUGINS`，不想要就加進 `_BLOCKED_PYTEST_PLUGINS` 與 "
        f"`pytest.ini` 的 addopts")
    overlap = sorted(set(_DECLARED_PYTEST_PLUGINS) & set(_BLOCKED_PYTEST_PLUGINS))
    assert not overlap, f"同一個外掛不能同時被宣告與被擋：{overlap}"


def test_the_parking_finder_restores_the_working_directory(repo_write_guard, tmp_path,
                                                          monkeypatch):
    """用一個合成套件量 finder 本身：模組本體執行時工作目錄在停放目錄，之後換回來；
    模組本體丟例外時也要換回來——換不回來的話，後面每一支用相對路徑的測試都會跑到
    暫存區裡去。"""
    # 每次一個新名字：用完不從 `sys.modules` 清掉（本檔的隔離掃描禁止 pop，理由見
    # `_scan`），同一個行程重跑時也不會撿到上一次的快取。
    pkg_name = "parkprobe_" + os.urandom(4).hex()
    src = tmp_path / "src"
    pkg = src / pkg_name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "ok.py").write_text("import os\nSEEN = os.getcwd()\n", encoding="utf-8")
    (pkg / "bad.py").write_text(
        "import os\nSEEN = os.getcwd()\nraise RuntimeError('boom')\n", encoding="utf-8")
    park = tmp_path / "park"
    monkeypatch.syspath_prepend(str(src))
    finders = [repo_write_guard.parking_finder(f"{pkg_name}.{name}", park)
               for name in ("ok", "bad")]
    before = os.getcwd()
    for finder in finders:
        sys.meta_path.insert(0, finder)
    try:
        ok = importlib.import_module(f"{pkg_name}.ok")
        assert os.path.samefile(ok.SEEN, park)
        assert os.getcwd() == before
        with pytest.raises(RuntimeError):
            importlib.import_module(f"{pkg_name}.bad")
        assert os.getcwd() == before
    finally:
        for finder in finders:
            sys.meta_path.remove(finder)


# ---------------------------------------------------------------------------
# 第四部分：`ast.parse` 的剖析快取（conftest 第四條，2026-09-20）
# ---------------------------------------------------------------------------
# 快取讓整套快很多（`test_bot_helpers.py` 單檔 146 秒 → 56 秒），代價是它替換了一個
# 全行程共用的 stdlib 函式。這幾支釘的是「快」不能換來「錯」的那幾個性質：每次拿到
# 的都是新樹（有人改了也不影響下一個人）、鍵是內容加全部參數、錯誤不被快取。
# 取得快取本體的方法是 `ast.parse.__globals__`：它就是 conftest 的模組命名空間，
# 比猜 pytest 用什麼名字登記 conftest 可靠。

def _ast_cache_globals():
    if os.environ.get("AXIOMATIC_AST_CACHE_OFF"):
        pytest.skip("剖析快取被 AXIOMATIC_AST_CACHE_OFF 關掉了")
    assert getattr(ast.parse, "__name__", "") == "_cached_ast_parse", (
        f"ast.parse 不是 conftest 的快取包裝（是 {ast.parse!r}）——快取沒有裝上，"
        "整套會慢回原本的兩倍以上，而且沒有任何紅燈。")
    return ast.parse.__globals__


def _counting_real_parse(monkeypatch, g):
    calls = []
    real = g["_REAL_AST_PARSE"]

    def counted(*args, **kwargs):
        calls.append(args[1] if len(args) > 1 else kwargs.get("filename"))
        return real(*args, **kwargs)

    monkeypatch.setitem(g, "_REAL_AST_PARSE", counted)
    return calls


def _unique_source(body: str) -> str:
    """同一段程式碼加一行獨一無二的註解：樹一樣，但一定是第一次見到的鍵。"""
    import uuid
    return f"{body}\n# {uuid.uuid4().hex}\n"


def test_the_ast_parse_cache_is_installed():
    """正面對照：沒裝上的話下面幾支會全部安靜地測到原本的 `ast.parse`。"""
    g = _ast_cache_globals()
    assert g["_REAL_AST_PARSE"] is not ast.parse


def test_a_cached_parse_hands_out_a_fresh_tree_every_time(monkeypatch):
    """命中時要還原出一棵**新的**樹；第一次（未命中）交出去的那棵被改了也不得污染快取。

    pytest 的斷言改寫就是拿 `ast.parse` 的結果**就地**改——若快取存的是樹物件本身，
    或是先交出去才存，下一支拿到的就是被改過的樹，而且症狀會在完全無關的地方出現。
    """
    g = _ast_cache_globals()
    calls = _counting_real_parse(monkeypatch, g)
    src = _unique_source("x = 1\ndef f():\n    return x")
    first = ast.parse(src, "<cache-test>")
    first.body.clear()                           # 模擬就地改寫
    second = ast.parse(src, "<cache-test>")
    third = ast.parse(src, "<cache-test>")
    assert calls == ["<cache-test>"], f"應該只真的剖析一次，實際：{calls}"
    assert second is not third
    assert len(second.body) == 2 and len(third.body) == 2, "快取被第一棵的改動污染了"
    second.body.clear()
    assert len(ast.parse(src, "<cache-test>").body) == 2, "快取被命中後拿到的那棵污染了"
    assert (ast.dump(third, include_attributes=True)
            == ast.dump(g["_REAL_AST_PARSE"](src, "<cache-test>"), include_attributes=True))


def test_the_cache_key_is_the_content_and_every_argument(monkeypatch):
    """同檔名不同內容、同內容不同參數，都要是不同的鍵。"""
    g = _ast_cache_globals()
    calls = _counting_real_parse(monkeypatch, g)
    tag = _unique_source("")
    one = ast.parse(f"a = 1{tag}", "<same-name>")
    two = ast.parse(f"a = 2{tag}", "<same-name>")
    assert ast.literal_eval(one.body[0].value) == 1
    assert ast.literal_eval(two.body[0].value) == 2, "只看檔名的鍵把第二份內容讀成第一份"
    expr = f"1 + 2{tag}".replace("\n#", "  #", 1)
    as_module = ast.parse(expr, "<mode>")
    as_expr = ast.parse(expr, "<mode>", mode="eval")
    assert isinstance(as_module, ast.Module) and isinstance(as_expr, ast.Expression), (
        "鍵沒有包含參數：同一段原始碼用不同的 mode 剖析拿到了同一種樹")
    assert len(calls) == 4, calls


def test_a_syntax_error_is_raised_every_time_and_never_cached(monkeypatch):
    g = _ast_cache_globals()
    calls = _counting_real_parse(monkeypatch, g)
    bad = _unique_source("def (:")
    for _ in range(2):
        with pytest.raises(SyntaxError):
            ast.parse(bad, "<bad>")
    assert len(calls) == 2, "剖析失敗的結果被快取了（或第二次沒有真的去剖析）"


def test_a_source_that_is_not_text_bypasses_the_cache(monkeypatch):
    """`ast.parse` 也收 AST 物件；那種呼叫不能雜湊內容，要原封不動交給原本的函式。"""
    g = _ast_cache_globals()
    calls = _counting_real_parse(monkeypatch, g)
    before = len(g["_AST_PARSE_CACHE"])
    module = ast.parse(_unique_source("y = 2"), "<text>")
    again = ast.parse(module, "<ast-object>")
    assert isinstance(again, ast.Module)
    assert calls == ["<text>", "<ast-object>"], calls
    assert len(g["_AST_PARSE_CACHE"]) == before + 1, "非文字來源被放進快取了"


def _ast_walk_globals():
    if os.environ.get("AXIOMATIC_AST_CACHE_OFF"):
        pytest.skip("攤平版的走訪被 AXIOMATIC_AST_CACHE_OFF 關掉了")
    assert getattr(ast.walk, "__name__", "") == "_flat_ast_walk", (
        f"ast.walk 不是 conftest 的攤平版（是 {ast.walk!r}）——最重的那個測試檔有一半"
        "時間花在走訪，沒裝上只會變慢，不會有任何紅燈。")
    return ast.walk.__globals__


def test_the_flattened_ast_walk_is_installed():
    """正面對照：下面幾支是**直接**拿 conftest 的那個函式來比的（理由見
    `flat_walk` 夾具），所以只有這一支在看「它到底有沒有接上 `ast.walk`」。"""
    g = _ast_walk_globals()
    assert g["_REAL_AST_WALK"] is not ast.walk
    assert ast.walk is g["_flat_ast_walk"]


@pytest.fixture
def flat_walk(monkeypatch):
    """回傳 conftest 的攤平版與標準庫版，**並在這支測試期間把全域的 `ast.walk`
    換回標準庫的那一個**。

    換回去不是為了比對（比對是直接拿函式物件），是為了**壞掉的時候印得出來**：
    pytest 自己的 traceback 排版也呼叫 `ast.walk`（`_pytest/_code/source.py` 的
    `get_statement_startend2`）。攤平版壞掉時，斷言失敗會在排版那一步變成
    INTERNALERROR，整輪當場結束——2026-09-20 的變異實測到的就是這個：末行停在
    `1 passed`，什麼失敗訊息都看不到，骨架只能判 `ABORTED`。**一個報不出自己失敗的
    守門，紅燈跟當機長得一樣。** 換回來之後同一個變異印得出正常的失敗訊息。

    monkeypatch 的還原在 teardown，而失敗訊息是在 call 階段結束後、teardown 之前
    排版的——順序剛好，所以這個換回去真的涵蓋得到排版那一步。"""
    g = _ast_walk_globals()
    monkeypatch.setattr(ast, "walk", g["_REAL_AST_WALK"])
    return g["_flat_ast_walk"], g["_REAL_AST_WALK"]


@pytest.mark.parametrize("name", ["discord_bot.py", "_webrunner_shared.py",
                                  "conftest.py", "test_suite_safety.py"])
def test_the_flattened_walk_visits_the_same_nodes_in_the_same_order(flat_walk, name):
    """拿**真實**的樹跟標準庫逐一比對**物件身分**，不是比 `ast.dump`。

    身分才問得出「順序對不對、有沒有漏一個節點」：兩棵結構相同的子樹 dump 起來一樣，
    但走訪順序錯了的話，任何「第一個符合的節點」邏輯就會拿到另一個。兩邊走的是**同一棵**
    樹物件（`ast.parse` 現在每次給新的一棵，所以要先剖析好再走兩次）。"""
    flat, real = flat_walk
    path = next(p for p in (PKG_ROOT / name, TEST_ROOT / name) if p.is_file())
    tree = ast.parse(path.read_text(encoding="utf-8"), name)
    mine = [id(node) for node in flat(tree)]
    theirs = [id(node) for node in real(tree)]
    assert len(theirs) > 100, f"{name} 只走出 {len(theirs)} 個節點——語料不對"
    assert mine == theirs, name


@pytest.mark.parametrize("source", [
    "global x\nx = 1",                      # 欄位是「非 AST 的 list」（名字字串）
    "def f():\n    return",                 # 欄位是 None（Return.value）
    "def f(*a, **k):\n    f(*a, **k)",      # 可選欄位散落在多個位置
    "@deco(1)\nclass C(Base, metaclass=M):\n    x: int = 0",
    "[y for x in z if x for y in x]",       # 巢狀 comprehension
    "match p:\n    case [1, *rest] if rest:\n        pass\n    case _:\n        pass",
    "try:\n    pass\nexcept* ValueError as e:\n    pass",
    "async def f():\n    async with a as b:\n        await c\n    async for i in d:\n        yield i",
])
def test_the_flattened_walk_handles_the_awkward_node_shapes(flat_walk, source):
    """標準庫的 `iter_fields` 用 `try/except AttributeError` 跳過沒設的欄位，攤平版用
    `getattr(..., None)`——兩者要在這些形狀上吐出同一串節點。"""
    flat, real = flat_walk
    tree = ast.parse(source, "<shapes>")
    assert [id(n) for n in flat(tree)] == [id(n) for n in real(tree)], source


def test_a_node_whose_field_was_never_set_is_not_a_crash(flat_walk):
    """`ast.Module()` 連 `body` 都沒有。標準庫靠 `except AttributeError` 撐過去，攤平版
    靠 `getattr` 的預設值；壞掉的話是 AttributeError，不是慢。"""
    flat, real = flat_walk
    bare = ast.Module()
    assert [id(n) for n in flat(bare)] == [id(n) for n in real(bare)] == [id(bare)]


def test_the_flattened_walk_is_still_a_lazy_generator(flat_walk):
    """攤平的另一種寫法是「先建好整串再 `iter()`」，那會更快一點，但會讓提早 `break`
    的呼叫端替整棵樹付錢——本專案有不少「找到第一個就走」的掃描。所以釘住它仍然是
    產生器，而且第一個吐出來的就是根節點。"""
    import inspect  # noqa: PLC0415

    flat, _real = flat_walk
    tree = ast.parse("x = [i for i in range(3)]", "<lazy>")
    walker = flat(tree)
    assert inspect.isgenerator(walker)
    assert next(walker) is tree


# ------------------------------------------------------- 掃描範圍的形狀 ----
# **跨領域的守門不得用檔名把 repo root 收窄。**
#
# 2026-09-20 量出來的：同一支合成違規檔放進 `axiomatic/` 會觸發 18 支測試，放到
# repo root 只剩 14。差的那幾支不是「規則不適用」，是它們的 repo root 那一半寫成
# `glob("start_*.py")`，於是 `install_autostart.py`——一支貨真價實的正式腳本——從來
# 沒有被掃過。`test_pid_liveness._live_stack_sources` 在 2026-09-10 就踩過同一個坑
# 並寫下了理由，但那次只修了自己那一支，六份複本原封不動地留著十天。
#
# 這一支釘的是**形狀**：針對 `.py` 的 glob 樣式一律要是 `*.py`，除非那個樣式本身
# 就是規則的一部分（下面三個，各自寫了理由）。**它看不到的東西也要講清楚**：用寫死
# 的檔名清單列舉 root 腳本，或用 `p.name.startswith(...)` 事後過濾，都在這支的視線
# 外。前者現在有 `test_exception_handlers.test_the_repo_root_script_list_is_still_complete`
# 對帳，後者目前不存在。
_NARROW_PY_GLOBS = {
    "test_*.py": "樣式就是規則：這些掃描問的正是「測試檔」這個子集。",
    "_test_*.py": "手動 e2e 腳本的命名慣例（`_test_presence_e2e.py`），刻意不被收集。",
    "webrunner_*.py": "兩個變體的對帳，問的就是「有幾個變體」。",
}

# 掃到的 glob 數下限。空清單跟乾淨的結果長得一模一樣。
_PY_GLOB_FLOOR = 50


def _py_glob_patterns(tree: ast.Module) -> list[tuple[int, str]]:
    """這個檔案裡每一次針對 `.py` 的 glob，回 `(行號, 樣式)`。"""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"glob", "rglob"}
                and node.args):
            continue
        first = node.args[0]
        if (isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.endswith(".py")):
            found.append((node.lineno, first.value))
    return found


def _project_python_files() -> list[Path]:
    """套件 ＋ `test/` ＋ repo root 的 `.py`。範圍本身就是這一節的主題，所以不收窄。

    `test/` 是 2026-09-22 測試搬出套件時補的：搬家前測試就在套件的 glob 裡，而本節
    掃的 glob 大半正是寫在測試裡的。"""
    return (sorted(PKG_ROOT.glob("*.py")) + sorted(TEST_ROOT.glob("*.py"))
            + sorted(PKG_ROOT.parent.glob("*.py")))


def test_no_scanner_narrows_a_python_glob_by_filename():
    """`glob("start_*.py")` 這種寫法會讓範圍安靜地少掉幾個檔案。

    失效形態是零症狀的：守門照跑、每一支都綠，只是它掃的語料少了一塊，而少掉的
    那一塊剛好是「沒有住在套件目錄裡的正式程式碼」。
    """
    seen: list[tuple[str, int, str]] = []
    for path in _project_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        seen += [(path.name, line, pattern)
                 for line, pattern in _py_glob_patterns(tree)]
    assert len(seen) >= _PY_GLOB_FLOOR, (
        f"全專案只找到 {len(seen)} 個 `.py` glob（下限 {_PY_GLOB_FLOOR}）——"
        "抽取器壞了，這一支等於沒問。")

    offenders = [(name, line, pattern) for name, line, pattern in seen
                 if pattern != "*.py" and pattern not in _NARROW_PY_GLOBS]
    assert not offenders, (
        "這些 glob 用檔名把語料收窄了：\n  "
        + "\n  ".join(f"{name}:{line} `{pattern}`" for name, line, pattern
                      in offenders)
        + "\n跨領域的規則（編碼、語言、PID 存活、psutil、模組邊界…）一個模組都沒"
          "指名，範圍就該是整個目錄。樣式真的是規則的一部分，就加進 "
          "`_NARROW_PY_GLOBS` 並寫下理由。")


def test_every_narrow_glob_exemption_is_still_in_use():
    """豁免會過期：樣式改掉之後那一筆從此對不到任何東西，而清單看起來仍有人在管。"""
    used = set()
    for path in _project_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        used.update(pattern for _line, pattern in _py_glob_patterns(tree))
    stale = sorted(set(_NARROW_PY_GLOBS) - used)
    assert not stale, (
        f"這些豁免已經沒有對應的呼叫了：{stale}。刪掉它們——一個永遠對不上的豁免"
        "只會讓下一個人以為這件事有人在管。")


@pytest.mark.parametrize("call, narrowed", [
    ('root.glob("start_*.py")', True),
    ('root.rglob("start_*.py")', True),
    ('REPO_ROOT.glob("launcher_*.py")', True),
    ('root.glob("*.py")', False),
    ('root.rglob("*.py")', False),
    ('root.glob("test_*.py")', False),      # 豁免內
    ('root.glob("*.md")', False),           # 這一節只管 `.py` 語料
])
def test_the_narrow_glob_detector_actually_bites(call, narrowed):
    """對照組。真實資料現在是乾淨的，所以上面那支把斷言刪掉也會全綠。

    `*.py` 與 `*.md` 那三筆是**放寬**那一側的 near-miss——只有它們才殺得掉「把判準
    改成一律回報」的變異。
    """
    tree = ast.parse(f"def f():\n    return {call}\n", "<synthetic>")
    hits = [pattern for _line, pattern in _py_glob_patterns(tree)]
    flagged = [p for p in hits if p != "*.py" and p not in _NARROW_PY_GLOBS]
    assert bool(flagged) is narrowed, (call, hits, flagged)


# ---------- 控制測試接不住 pytest 的 outcome ---------------------------------
# `pytest.skip()` 與 `pytest.fail()` 丟出來的是 `Skipped` / `Failed`，而這兩個繼承
# 的是 `BaseException`，不是 `Exception`。所以 `with pytest.raises(Exception)`
# **接不到它們**：outcome 直接穿出 `with`，把那支測試本身記成 skip 或 fail，而
# `with` 底下的每一句斷言都成了死碼。
#
# skip 那一半最糟，因為它不是紅的：pytest 的結束碼仍是 0，整套照樣綠，摘要只多一
# 行 SKIPPED——而那行印的是**被呼叫的那一支**的行號與訊息，讀起來像「這台機器缺某
# 個前提」，完全不像「有一支控制測試沒在跑」。
#
# 實測（2026-09-20）：`test_dependency_floors` 裡有一支控制測試正是這個形狀，它兩
# 個方向裡的第二個從來沒有執行過。而它之所以能活下來，是因為**它自己解釋了自己**
# ——docstring 早就把那一行 SKIPPED 寫成「本支刻意造成的」。症狀被描述得越完整，
# 越沒有人會再往下追一層。
#
# 所以裸 `Exception` 在這個套件裡一律禁止：它讀起來像「丟什麼都算」，實際上剛好把
# 最可能出現的兩種 outcome 排除在外。要接 outcome 就寫 `pytest.skip.Exception` /
# `pytest.fail.Exception`（或 `BaseException`）；要接一般例外就把型別寫出來——寫得
# 出型別，才證明你知道自己在等什麼。
_RAISES_FLOOR = 150

_BARE_EXCEPTION_EXEMPT = {
    "test_a_pytest_outcome_escapes_pytest_raises_exception":
        "這一支就是在示範那個形狀接不住 outcome，非寫出裸 `Exception` 不可。",
}


def _raises_expectations(tree: ast.Module) -> list[tuple[int, list[str]]]:
    """每個 `pytest.raises(...)` 的行號，與它期望的型別（原樣字串）。

    型別用 `ast.unparse` 還原成字串而不是只取 `attr`，因為
    `pytest.skip.Exception` 的 `attr` 也是 `"Exception"`——只比對 `attr` 的話，
    **正確的寫法會被當成違規**，而那正是這道禁令要人改成的寫法。
    """
    out: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        func = node.func if isinstance(node, ast.Call) else None
        if not ((isinstance(func, ast.Attribute) and func.attr == "raises")
                or (isinstance(func, ast.Name) and func.id == "raises")):
            continue
        names: list[str] = []
        if node.args:
            first = node.args[0]
            items = first.elts if isinstance(first, ast.Tuple) else [first]
            names = [ast.unparse(item) for item in items]
        out.append((node.lineno, names))
    return out


def _enclosing_def(tree: ast.Module, lineno: int) -> str:
    best = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                best = node.name
    return best


def test_no_test_expects_a_bare_exception_from_pytest_raises():
    """`pytest.raises(Exception)` 一律不收——它接不住 skip 與 fail。"""
    seen = 0
    offenders: list[str] = []
    for path in _project_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        for line, names in _raises_expectations(tree):
            seen += 1
            if "Exception" not in names:
                continue
            if _enclosing_def(tree, line) in _BARE_EXCEPTION_EXEMPT:
                continue
            offenders.append(f"{path.name}:{line} -> pytest.raises({names})")
    assert seen >= _RAISES_FLOOR, (
        f"全專案只掃到 {seen} 個 `pytest.raises(...)`（下限 {_RAISES_FLOOR}）——"
        "抓法過期了，這支於是在對著空集合做檢查。")
    assert not offenders, (
        f"這些地方等的是裸 `Exception`：{offenders}。`Skipped` 與 `Failed` 繼承"
        "`BaseException`，接不到；真的被跳過時那支測試會被記成 skip，結束碼 0、"
        "整套照樣綠，底下的斷言全部變死碼。改成 `pytest.skip.Exception` / "
        "`pytest.fail.Exception` / `BaseException`，或直接寫出你在等的型別。")


def test_every_bare_exception_exemption_is_still_in_use():
    """豁免會過期：函式改名或改寫之後那一筆再也對不到任何東西。"""
    live = set()
    for path in _project_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        for line, names in _raises_expectations(tree):
            if "Exception" in names:
                live.add(_enclosing_def(tree, line))
    stale = sorted(set(_BARE_EXCEPTION_EXEMPT) - live)
    assert not stale, (
        f"這些豁免已經沒有對應的寫法了：{stale}。刪掉它們——一個永遠對不上的豁免"
        "只會讓下一個人以為這件事有人在管。")


def test_a_pytest_outcome_escapes_pytest_raises_exception():
    """上面那道禁令的前提，用量的而不是用記的。

    如果哪一版 pytest 把 outcome 改成繼承 `Exception`，這條禁令的理由就消失了，
    那時候應該是**這一支**先紅，而不是繼續禁一件已經不成立的事。

    兩個層次都驗：型別關係，以及實際行為——因為真正會傷人的是行為。
    """
    assert not issubclass(pytest.skip.Exception, Exception), (
        "`Skipped` 變成 `Exception` 的子類了——上面那道禁令的理由需要重寫。")
    assert not issubclass(pytest.fail.Exception, Exception), (
        "`Failed` 變成 `Exception` 的子類了——同上。")

    escaped = False
    try:
        with pytest.raises(Exception):
            pytest.skip("這一句不該被上面那個 raises 接住")
    except pytest.skip.Exception:
        escaped = True
    assert escaped, (
        "`pytest.raises(Exception)` 居然接住了 `pytest.skip()`——禁令的前提變了。")


@pytest.mark.parametrize("call, flagged", [
    ("pytest.raises(Exception)", True),
    ("pytest.raises((ValueError, Exception))", True),
    ("pytest.raises(Exception, match='x')", True),
    ("pytest.raises(AssertionError)", False),
    ("pytest.raises(BaseException)", False),
    ("pytest.raises(pytest.skip.Exception)", False),
    ("pytest.raises(pytest.fail.Exception)", False),
    ("pytest.raises((ValueError, KeyError))", False),
])
def test_the_bare_exception_detector_tells_the_shapes_apart(call, flagged):
    """對照組。真實資料現在是乾淨的，所以上面那支把斷言刪掉也會全綠。

    `pytest.skip.Exception` 那兩筆是**放寬**那一側的 near-miss：只有它們殺得掉
    「把判準從 `ast.unparse` 改回 `attr`」的變異，因為那個寫法的 `attr` 也叫
    `Exception`。
    """
    tree = ast.parse(f"def f():\n    with {call}:\n        pass\n", "<synthetic>")
    names = [n for _line, ns in _raises_expectations(tree) for n in ns]
    assert bool("Exception" in names) is flagged, (call, names)


# --------------------------------------------- 宣告過的外掛，理由也要是真的 ----
# 上面兩支守的是名單：「載進來的都宣告過」與「擋掉的真的擋住了」。**兩支都沒有看
# 「理由」那一欄**，而 2026-09-20 量出來 `asyncio` 那一筆是錯的——它寫著「bot 那側
# 大量的 async 測試靠它的事件迴圈夾具」，實際上整棵樹 0 支 `async def test_`、0 個
# `@pytest.mark.asyncio`，bot 那側的 async 全部是測試自己用 `asyncio.run()` 驅動的
# 同步測試（光 `test_bot_helpers.py` 就 56 處）。
#
# 這不是整潔問題。那份 dict 的整個作用是「有人查過、決定留著」，理由一旦說謊，下一個
# 人讀到「大量測試靠它」就不會再查。而這裡的真相剛好是**反過來**的：本專案完全不依賴
# 這個外掛，所以 pytest-asyncio 的破壞性改版（1.0 拿掉 `event_loop` 夾具）與這套測試
# 無關——那正是升級時真正想知道的事實，被一句聽起來很有依賴的話蓋掉了。
#
# 順手量掉、寫下來省得下次重問的兩件事：
#   * `-p no:asyncio` 只省 **0.047 秒**（`anyio` 是 0.17 秒），所以**不擋**。留著的成本
#     幾乎是零，擋掉反而會讓未來第一支 async 測試死在一個看不懂的地方。
#   * pytest 9 對「沒有人認領的 coroutine 測試」是**直接紅**（`Failed: async def
#     functions are not natively supported`），載不載入這個外掛都一樣（實測 `py -3`
#     的 pytest 9.0.3，兩種跑法結束碼都是 1）。所以「寫了 async 測試卻忘了 marker」不是
#     靜默通過的風險。這一條值得記著：不必為一個不存在的危害再蓋一道守門。
#
# 判準用 AST 不是字串比對，而且樹裡**現在就有**一筆 near-miss——
# 有些測試的合成原始碼字串裡就寫著 `async def test_...`。

# 量出來的前提：整套有幾支測試**真的**需要 pytest-asyncio。上面那筆理由整個建立在
# 這個數字是 0 上面，所以它跟理由要一起改。
_ASYNCIO_DEPENDENT_TESTS = 0

# 掃到的測試檔數下限。空的選擇跟乾淨的結果長得一模一樣。
_ASYNCIO_SCAN_FILE_FLOOR = 40


def _asyncio_plugin_uses(tree: ast.Module) -> list[tuple[int, str]]:
    """這個檔案裡「非 pytest-asyncio 不可」的東西，回 `(行號, 說明)`。

    只認 pytest **真的會收集**的位置：模組層級與 `Test*` 類別體內的 `async def
    test_*`，以及任何 `pytest.mark.asyncio` 裝飾器。刻意**不**用 `ast.walk`——同步
    測試裡巢狀的 `async def` 是它自己用 `asyncio.run()` 驅動的協程，不是一支測試。
    """
    found: list[tuple[int, str]] = []
    bodies = [tree.body]
    bodies += [node.body for node in tree.body if isinstance(node, ast.ClassDef)]
    for body in bodies:
        for node in body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_"):
                found.append((node.lineno, f"async def {node.name}"))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for deco in node.decorator_list:
                    text = ast.unparse(deco)
                    if text.startswith("pytest.mark.asyncio"):
                        found.append((node.lineno, f"@{text} on {node.name}"))
    return found


def test_the_asyncio_plugin_declaration_is_still_true():
    """`_DECLARED_PYTEST_PLUGINS["asyncio"]` 的理由要跟這棵樹對得上。

    失效形態是零症狀的：名單那兩支照跑、照綠，因為它們比的是**鍵**。理由那一欄是
    純散文，沒有任何東西看它——於是它可以在原地說謊一年，而讀的人正是為了不必重查
    才去讀它的。

    **這一支守得住的範圍**：數字那一半是機械的（樹裡冒出第一支 async 測試就紅，訊息
    會說「順便把理由改掉」）；散文那一半只釘一個樞紐詞——數字是 0 時理由必須講「不
    依賴」，數字不是 0 時必須不講。換個措辭重新宣稱同一件事它擋不住，那要靠讀的人。
    """
    uses: list[str] = []
    files = 0
    for path in _project_python_files():
        if not path.name.startswith("test_"):
            continue
        files += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        uses += [f"{path.name}:{line} {what}"
                 for line, what in _asyncio_plugin_uses(tree)]
    assert files >= _ASYNCIO_SCAN_FILE_FLOOR, (
        f"只掃到 {files} 個測試檔（下限 {_ASYNCIO_SCAN_FILE_FLOOR}）——這一支的取檔"
        "壞了，不是樹變乾淨了。")

    assert len(uses) == _ASYNCIO_DEPENDENT_TESTS, (
        f"真的需要 pytest-asyncio 的地方有 {len(uses)} 處，紀錄上寫的是 "
        f"{_ASYNCIO_DEPENDENT_TESTS}：\n  " + "\n  ".join(uses)
        + f"\n把 `_ASYNCIO_DEPENDENT_TESTS` 改成 {len(uses)}，**並且**同步改掉 "
          "`_DECLARED_PYTEST_PLUGINS` 裡 asyncio 那一筆的理由——那句話現在說本專案"
          "不依賴它。")

    reason = _DECLARED_PYTEST_PLUGINS["asyncio"]
    assert ("不依賴" in reason) is (_ASYNCIO_DEPENDENT_TESTS == 0), (
        f"理由與數字對不起來。`_ASYNCIO_DEPENDENT_TESTS` 是 "
        f"{_ASYNCIO_DEPENDENT_TESTS}，理由卻寫著：{reason}")


@pytest.mark.parametrize("label, source, hits", [
    ("模組層級的 async 測試", "async def test_x():\n    pass\n", 1),
    ("類別裡的 async 測試",
     "class TestThing:\n    async def test_x(self):\n        pass\n", 1),
    ("asyncio marker",
     "@pytest.mark.asyncio\nasync def test_x():\n    pass\n", 2),
    ("同步測試掛了 marker",
     "@pytest.mark.asyncio\ndef test_x():\n    pass\n", 1),
    ("不是測試的 async 函式", "async def helper():\n    pass\n", 0),
    ("同步測試裡巢狀的協程",
     "def test_x():\n    async def test_inner():\n        pass\n"
     "    asyncio.run(test_inner())\n", 0),
    ("字串裡的 async 測試",
     'SRC = "async def test_x():\\n    pass\\n"\n', 0),
    ("別的 marker", "@pytest.mark.timeout(5)\ndef test_x():\n    pass\n", 0),
    ("別的 marker 的參數裡提到 asyncio",
     '@pytest.mark.parametrize(\"asyncio\", [1])\ndef test_x():\n    pass\n', 0),
])
def test_the_asyncio_use_detector_tells_the_shapes_apart(label, source, hits):
    """對照組。真實資料現在是空的，所以上面那支把斷言刪掉也會全綠。

    最後幾筆是**放寬**那一側的 near-miss，缺一不可：巢狀那一筆殺的是「把 `tree.body`
    換成 `ast.walk`」，字串那一筆殺的是「退回字串比對」——而它不是假想的，
    有些測試裡真的有一份那樣的合成原始碼。
    """
    assert len(_asyncio_plugin_uses(ast.parse(source, "<synthetic>"))) == hits, label


# ------------------------------------------- 同一份 parametrize 裡的重複案例 ----
# **一筆重複的案例＝一筆你以為測到、其實沒測到的案例。** 症狀是零：pytest 不會去重，
# 兩筆都會跑、都會過，報告上還多一個綠點，看起來像覆蓋率變好了。
#
# 2026-09-20 在 `test_audit_dependencies.py` 抓到一組：
# `("1.0.8", "<1.0.8", False)` 與 `("1.0.7", "<1.0.8", True)` 被貼了第二次，上面還掛著
# 一句「少了這兩筆，『一律回 None』也會全綠」——那句話是假的，它們跟前面第 3、第 4 筆
# 一字不差，刪掉不會少測到任何東西。本來想補的是「早退規則的邊界」，補成了複製貼上，
# 而那正是**看起來最像有人想過**的失效形態：註解在，理由在，案例是空的。
#
# 判準用 `ast.unparse` 正規化過的文字比對，所以引號風格與空白不算差異（§8.90 的教訓：
# 引號是原始碼細節，不是語意）。`pytest.param(..., id=...)` 的 id 算差異——刻意區分過的
# 兩筆不是重複。讀不到字面清單（變數、推導式、函式呼叫）就跳過，那是這一支看不見的
# 範圍，不假裝看得見。
#
# **刻意沒有豁免清單。** 真的想要兩筆值相同的案例，正確寫法是給它們不同的 `id=`（否則
# 連失敗訊息都分不出是哪一筆），而那樣寫本來就不會被這一支判成重複。

# 掃到的 parametrize 呼叫數下限。空的選擇跟乾淨的結果長得一模一樣。
_PARAMETRIZE_FLOOR = 300


def _parametrize_cases(tree: ast.Module) -> list[tuple[int, list[str]]]:
    """每一個 `@pytest.mark.parametrize`，回 `(行號, 正規化後的案例文字清單)`。

    只看第二個引數是**字面**清單／tuple 的呼叫；其他形狀回空清單（讀不到就是讀不到）。
    """
    out: list[tuple[int, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else "")
        if name != "parametrize" or len(node.args) < 2:
            continue
        values = node.args[1]
        if not isinstance(values, (ast.List, ast.Tuple)):
            out.append((node.lineno, []))
            continue
        out.append((node.lineno, [ast.unparse(item) for item in values.elts]))
    return out


def _duplicate_cases(cases: list[str]) -> list[str]:
    """重複出現的案例文字（第二次以後的每一筆算一個）。"""
    seen: set[str] = set()
    repeats: list[str] = []
    for case in cases:
        if case in seen:
            repeats.append(case)
        else:
            seen.add(case)
    return repeats


def test_no_parametrize_repeats_a_case():
    """重複的案例不會紅、不會跳過、不會有任何訊號——它只是白跑一次。

    失效形態跟 `_OWNER_ONLY_SLASH` 那種過期豁免同一族：**多出來的東西看起來像多出來
    的保護**。一筆重複案例通常是「想補一個 near-miss，貼成了已經有的那一筆」，於是原本
    要殺的那個變異活著，而清單長了兩行、註解也寫了，讀的人不會再查。
    """
    seen = 0
    offenders: list[str] = []
    for path in _project_python_files():
        if not path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        for lineno, cases in _parametrize_cases(tree):
            seen += len(cases)
            for repeat in _duplicate_cases(cases):
                offenders.append(f"{path.name}:{lineno} 重複案例 {repeat}")
    assert seen >= _PARAMETRIZE_FLOOR, (
        f"全專案只讀到 {seen} 筆 parametrize 案例（下限 {_PARAMETRIZE_FLOOR}）——"
        "抽取器壞了，這一支等於沒問。")
    assert not offenders, (
        "這些 parametrize 裡有一字不差的重複案例：\n  " + "\n  ".join(offenders)
        + "\n重複案例不會紅也不會跳過，它只是白跑一次，而清單看起來變長了。"
          "本來想補的那個 near-miss 還缺著——把它補上，或刪掉重複的那一筆。")


@pytest.mark.parametrize("label, source, repeats", [
    ("一字不差的重複",
     '@pytest.mark.parametrize("a", [(1, 2), (1, 2)])\ndef test_x(a):\n    pass\n', 1),
    ("三筆一樣的算兩個重複",
     '@pytest.mark.parametrize("a", [1, 1, 1])\ndef test_x(a):\n    pass\n', 2),
    ("只差一個值就不是重複",
     '@pytest.mark.parametrize("a", [(1, 2), (1, 3)])\ndef test_x(a):\n    pass\n', 0),
    ("引號風格不算差異",
     '@pytest.mark.parametrize("a", ["x", \'x\'])\ndef test_x(a):\n    pass\n', 1),
    ("空白不算差異",
     '@pytest.mark.parametrize("a", [(1, 2), ( 1,2 )])\ndef test_x(a):\n    pass\n', 1),
    ("id 不同就不是重複",
     '@pytest.mark.parametrize("a", [pytest.param(1, id="a"), '
     'pytest.param(1, id="b")])\ndef test_x(a):\n    pass\n', 0),
    ("marks 不同也不是重複",
     '@pytest.mark.parametrize("a", [pytest.param(1), '
     'pytest.param(1, marks=pytest.mark.xfail)])\ndef test_x(a):\n    pass\n', 0),
    ("讀不到字面清單就跳過",
     '@pytest.mark.parametrize("a", CASES)\ndef test_x(a):\n    pass\n', 0),
    ("別的呼叫不管，就算它的第二個引數是帶重複的清單",
     '@mymark.cases("a", [1, 1])\ndef test_x(a):\n    pass\n', 0),
])
def test_the_duplicate_case_detector_tells_the_shapes_apart(label, source, repeats):
    """對照組。真實資料修乾淨之後，上面那支把斷言刪掉也會全綠。

    「引號風格」「空白」兩筆是**收緊**那一側的 near-miss：改用原始碼片段比對就抓不到
    它們。「id 不同」「marks 不同」「讀不到字面清單」「別的呼叫」是**放寬**那一側。
    最後那一筆原本寫成 `@pytest.mark.usefixtures(...)`，那是**死的**——第二個引數
    不是清單，所以判準再怎麼放寬它都回 0。換成一個第二引數真的是帶重複清單的別種
    呼叫之後，「拿掉『呼叫的是不是 parametrize』這個檢查」才有東西殺得掉它。
    """
    found = sum(len(_duplicate_cases(cases))
                for _line, cases in _parametrize_cases(ast.parse(source, "<synthetic>")))
    assert found == repeats, label


# ------------------------------- 用 try/except 斷言「一定會丟」時的那條空路 ----
# 這個形狀在本專案很常見，而且大多數寫得是對的：
#
#     try:
#         thing_that_must_raise()
#     except SomeError as error:
#         assert "..." in str(error), error
#     else:
#         raise AssertionError("它一定要丟")
#
# **少了那個 `else`（或 `try` 結尾那句 `raise`），什麼都沒丟的時候整段安靜通過。**
# 兩個 except 都不會進，裡面的 assert 從此是死的，而這一段的整個重點正是「它一定
# 要丟」。同 `feedback: A negative assertion passes once the subject is gone`。
#
# 2026-09-20 實測：全樹 7 個這種區塊，**6 個是對的，1 個沒有**——
# `test_generate_loop_stops_on_a_modal_it_cannot_close` 裡「沒有 modal 就該是普通
# RuntimeError」那一段。當天那條例外路徑仍然活著（補上 `else` 之後照樣綠），所以它是
# 個**潛在**缺口而不是已經壞掉的斷言：`generate_loop` 哪天改成安靜回 0，這支測試不會紅。
#
# 判準刻意比 ruff 的 `PT017` 窄。`PT017` 會把上面那個**寫對的**形狀也一起報出來
# （它主張改用 `pytest.raises`），實測 13 筆全是這樣——「會喊狼來了的守門就是會被關掉的
# 守門」。這裡只問一件事：**什麼都沒丟的時候，有沒有東西會讓測試紅。**

# 掃到的 try/except 斷言區塊下限。空的選擇跟乾淨的結果長得一模一樣。
_MUST_RAISE_BLOCK_FLOOR = 5


def _raises_or_fails(stmt: ast.stmt) -> bool:
    """這一句會不會讓測試紅（`raise` 或 `pytest.fail(...)`）。"""
    if isinstance(stmt, ast.Raise):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
        func = stmt.value.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else "")
        return name == "fail"
    return False


def _must_raise_blocks(tree: ast.Module) -> list[tuple[int, bool]]:
    """每一個「用 try/except 檢查例外」的區塊，回 `(行號, 沒丟的時候會不會紅)`。"""
    out: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        checks = any(isinstance(inner, ast.Assert)
                     for handler in node.handlers
                     for stmt in handler.body
                     for inner in ast.walk(stmt))
        if not checks:
            continue
        covered = (any(_raises_or_fails(stmt) or isinstance(stmt, ast.Assert)
                       for stmt in node.orelse)
                   or bool(node.body) and _raises_or_fails(node.body[-1]))
        out.append((node.lineno, covered))
    return out


def test_every_must_raise_block_fails_when_nothing_is_raised():
    """用 try/except 斷言「一定會丟」時，沒丟的那條路也要有人管。

    失效形態是零症狀的：被測的東西改成安靜回傳之後，兩個 except 都不會進，裡面的
    assert 一句都不執行，測試照樣綠——而它原本就是為了那件事存在的。
    """
    seen = 0
    offenders: list[str] = []
    for path in _project_python_files():
        if not path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        for lineno, covered in _must_raise_blocks(tree):
            seen += 1
            if not covered:
                offenders.append(f"{path.name}:{lineno}")
    assert seen >= _MUST_RAISE_BLOCK_FLOOR, (
        f"全專案只找到 {seen} 個 try/except 斷言區塊（下限 "
        f"{_MUST_RAISE_BLOCK_FLOOR}）——抽取器壞了，這一支等於沒問。")
    assert not offenders, (
        "這些 try/except 在「什麼都沒丟」的時候會安靜通過：\n  "
        + "\n  ".join(offenders)
        + "\n補一個 `else: raise AssertionError(...)`，或把 `try` 的最後一句寫成 "
          "`raise`。少了它，被測的東西改成安靜回傳就沒有任何東西會紅。")


@pytest.mark.parametrize("label, source, covered", [
    ("有 else raise",
     "try:\n    f()\nexcept E as e:\n    assert 'x' in str(e)\nelse:\n"
     "    raise AssertionError('must raise')\n", True),
    ("else 裡是 pytest.fail",
     "try:\n    f()\nexcept E as e:\n    assert 'x' in str(e)\nelse:\n"
     "    pytest.fail('must raise')\n", True),
    ("else 裡是 assert",
     "try:\n    f()\nexcept E as e:\n    assert 'x' in str(e)\nelse:\n"
     "    assert False\n", True),
    ("try 結尾自己 raise",
     "try:\n    f()\n    raise AssertionError('must raise')\n"
     "except E as e:\n    assert 'x' in str(e)\n", True),
    ("什麼都沒有",
     "try:\n    f()\nexcept E as e:\n    assert 'x' in str(e)\n", False),
    ("else 裡只有 pass",
     "try:\n    f()\nexcept E as e:\n    assert 'x' in str(e)\nelse:\n"
     "    pass\n", False),
    ("raise 不在最後一句",
     "try:\n    raise AssertionError('x')\n    f()\n"
     "except E as e:\n    assert 'x' in str(e)\n", False),
])
def test_the_must_raise_detector_tells_the_shapes_apart(label, source, covered):
    """對照組。真實資料修乾淨之後，上面那支把斷言刪掉也會全綠。

    「else 裡只有 pass」與「raise 不在最後一句」是**放寬**那一側的 near-miss：只看
    `orelse` 存不存在、或在整個 `try` 主體裡找 `raise`，都會在這兩筆上放行。
    """
    blocks = _must_raise_blocks(ast.parse(source, "<synthetic>"))
    assert len(blocks) == 1, (label, blocks)
    assert blocks[0][1] is covered, label


@pytest.mark.parametrize("label, source", [
    ("except 裡沒有 assert（純清理）",
     "try:\n    f()\nexcept E:\n    cleanup()\n"),
    ("try/finally 沒有 handler",
     "try:\n    f()\nfinally:\n    cleanup()\n"),
])
def test_the_must_raise_detector_ignores_the_other_shapes(label, source):
    """不是「在檢查例外」的 try 一律不管——會喊狼來了的守門就是會被關掉的守門。"""
    assert _must_raise_blocks(ast.parse(source, "<synthetic>")) == [], label


# ---------------------------------------------------------------------------
# 沒有被 await 的 coroutine：這一套最容易「全綠但什麼都沒驗到」的形狀
# ---------------------------------------------------------------------------

# 沒有被 await 的 coroutine 要變紅，**這兩條缺一不可**；為什麼寫在 `pytest.ini` 裡。
_COROUTINE_GATE = ("error::RuntimeWarning",
                   "error::pytest.PytestUnraisableExceptionWarning")
# 與上面無關的第三條：把 `CLAUDE.md` 那條「`-X warn_default_encoding` 跑一次」的手動掃描
# 接上閘門。沒帶那個旗標時它空轉（警告根本不會發），帶了就是紅燈而不是摘要裡的一行。
_ENCODING_GATE = ("error::EncodingWarning",)
# `pytest.ini` 的 `filterwarnings` 必須逐字是這幾條，**連順序**。
_WARNING_GATE = _COROUTINE_GATE + _ENCODING_GATE

# 子行程用的語料：一支**本來會通過**的測試，唯一的問題是掉了一個沒有被 await 的
# coroutine。`gc.collect()` 是為了讓回收落在這一支自己的區間裡——不然警告會算到
# 下一支頭上，而那是一種更難讀的紅。
_UNAWAITED_PROBE = """\
import gc


async def _never_awaited():
    return 1


def test_drops_a_coroutine_on_the_floor():
    _never_awaited()
    gc.collect()
"""


def _filterwarnings_in_ini(pytestconfig) -> list[str]:
    """從**這一回合真的讀到的**那個 ini 檔裡取出 `filterwarnings` 的每一行。"""
    ini = pytestconfig.inipath
    assert ini is not None, "這一回合沒有讀到任何 ini 檔——對帳的來源不見了"
    parser = configparser.ConfigParser()
    parser.read_string(ini.read_text(encoding="utf-8"))
    return parser["pytest"]["filterwarnings"].split()


def test_a_runtime_warning_is_an_error_in_this_run():
    """正面對照：`RuntimeWarning` 在**這一回合**真的會當場丟出來。

    只查 ini 檔的內容不夠：命令列的 `-W` 蓋得過 ini，某支測試的
    `warnings.catch_warnings` 沒還原也會讓它失效，而這兩種情況下 ini 檔長得一模
    一樣。所以這裡直接發一個真的警告，看它會不會變成例外。"""
    with pytest.raises(RuntimeWarning):
        warnings.warn("正面對照：這一句應該當場變成例外", RuntimeWarning)


def test_an_unraisable_exception_is_an_error_in_this_run():
    """同一件事的另一半，**而且這一半才是真正讓 coroutine 變紅的那個**。

    `error::RuntimeWarning` 只負責把警告升級成例外；沒有被 await 的 coroutine 是在
    回收時從「無法向外丟的例外」那條路發出來的，升級後的例外沒有人接得住，pytest 的
    unraisable 外掛就把它再包成 `PytestUnraisableExceptionWarning`——測試於是通過。
    少了這一條，整件事的輸出跟完全沒設一模一樣。"""
    with pytest.raises(pytest.PytestUnraisableExceptionWarning):
        warnings.warn("正面對照：這一句應該當場變成例外",
                      pytest.PytestUnraisableExceptionWarning)


def test_an_encoding_warning_is_an_error_in_this_run():
    """第三條的正面對照，而它**平常一定不會自己發生**。

    `EncodingWarning` 只在直譯器帶著 `-X warn_default_encoding`（或
    `PYTHONWARNDEFAULTENCODING=1`）時才會被發出來，平常跑整套一輩子碰不到。所以
    這一條的閘門**只有在有人跑那條掃描命令列的時候**才有作用——而那正是它存在的
    理由：`CLAUDE.md` 把那次掃描寫成一條命令列，命令列是慣例不是閘門。

    這裡不模擬那個旗標，只證明 filter 本身是活的：直接發一個 `EncodingWarning`，
    它必須當場變成例外。"""
    with pytest.raises(EncodingWarning):
        warnings.warn("正面對照：這一句應該當場變成例外", EncodingWarning)


def test_the_ini_declares_exactly_the_filters_the_suite_relies_on(pytestconfig):
    """`pytest.ini` 與 `_WARNING_GATE` 兩個方向都要對得起來，連順序。

    上面那幾支查的是「這一回合有沒有升級」，查不出**是誰讓它升級的**——開發者在自己
    的命令列上補一個 `-W` 也會讓它們綠。這一支釘的是那個設定真的長在 `pytest.ini`
    裡，所以每一個 fresh clone 都有。"""
    assert _filterwarnings_in_ini(pytestconfig) == list(_WARNING_GATE), (
        "`pytest.ini` 的 `filterwarnings` 與 `_WARNING_GATE` 對不起來。前兩條缺一不可"
        "（少了第二條，沒有被 await 的 coroutine 產生的輸出跟完全沒設一模一樣），"
        "第三條是把 `-X warn_default_encoding` 那次手動掃描接上閘門。理由都寫在 "
        "`pytest.ini`。")


@pytest.mark.parametrize(("label", "flags", "red"), [
    ("兩條都在", _COROUTINE_GATE, True),
    ("只有 RuntimeWarning——這就是那個看起來夠的寫法", ("error::RuntimeWarning",), False),
    ("兩條都沒有", (), False),
])
def test_a_dropped_coroutine_only_turns_red_when_both_halves_are_on(
        label, flags, red, tmp_path):
    """端對端：回收 → unraisablehook → 外掛 → filter → 紅燈，整條鏈路只能用子行程驗。

    在**這個**行程裡驗不了：那句警告是在測試函式回來之後、由外掛在階段之間發出來的，
    所以接不住——想在自己身上示範一次，就是讓自己紅。

    三種組合都跑，而且**後兩種必須是綠的**。只留第一種的話，「把第二條刪掉」這個變異
    不會被抓到：它只會讓第一種從紅變綠，而一支只斷言「會紅」的測試說不出那是因為少了
    哪一條。同 `CLAUDE.md` 裡「放寬的那一步只會被必須放行的案例殺掉」那一段。"""
    import subprocess

    probe = tmp_path / "test_unawaited_probe.py"
    probe.write_text(_UNAWAITED_PROBE, encoding="utf-8")
    # 子行程的 cwd 是 tmp_path，所以它碰不到 repo。擋掉的外掛與 `pytest.ini` 同一份：
    # 其中 `je_auto_control` 是正確性問題——它一載入就往 cwd 開記錄檔。
    blocked = [flag for name in _BLOCKED_PYTEST_PLUGINS
               for flag in ("-p", f"no:{name}")]
    chosen = [flag for spec in flags for flag in ("-W", spec)]
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *blocked, *chosen, str(probe)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, cwd=str(tmp_path),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    tail = out.stdout[-1200:]
    assert (out.returncode != 0) is red, f"{label}：rc={out.returncode}\n{tail}"
    if red:
        assert "1 failed" in tail, tail
    else:
        assert "1 passed" in tail and "1 warning" in tail, (
            f"{label}：子行程應該**綠著**留下一句警告。警告不見了代表這份語料根本"
            f"沒有掉 coroutine，對照就失去意義了。\n{tail}")


# ---- 稽核事件表（`_WRITE_EVENTS`）----------------------------------------------
# 防線攔的是 12 種稽核事件，而在此之前**只有 `open` 那一種真的被測過**。其餘十一種
# 每一種都有兩個各自無聲的失效：
#
#   1. **事件名打錯** → CPython 從來不會發那個名字，於是那一格永遠不會觸發。守門
#      照跑、整套照綠、`_WRITE_EVENTS` 看起來也管著那個操作。
#   2. **引數位置選錯** → 守的是**來源**而不是**目的地**。`shutil.copyfile(src, dst)`
#      取 index 0 的話，「從 repo 複製出去」會被擋（誤報），而「複製進 repo 覆蓋掉
#      正式檔」會放行（漏報）——漏報的那一邊正是這道防線存在的理由。
#
# 兩個都靠**實際跑一次那個操作、錄下 CPython 真的發了什麼**來對帳，不是比對字串。

_AUDIT_PROBE = """
# 在子行程裡錄下每一種寫入操作真正發出的稽核事件。
#
# 裝 audit hook 是**不可逆**的（`sys.addaudithook` 沒有對應的移除），所以這一段跑在
# 子行程裡：在測試行程內多裝一個 hook，會讓之後每一個稽核事件都多付一次 Python
# 呼叫，而整套的 I/O 量很大。
import json
import os
import shutil
import sys

SEEN = []


def _hook(event, args):
    SEEN.append((event, [a if isinstance(a, str) else repr(a) for a in args]))


sys.addaudithook(_hook)

root = sys.argv[1]
src = os.path.join(root, "src.txt")
dst = os.path.join(root, "dst.txt")
sub = os.path.join(root, "sub")


def _try(label, fn):
    try:
        fn()
    except Exception as error:          # 平台不支援就記下來，由母測試決定跳不跳
        SEEN.append(("!failed:" + label, [repr(error)]))


with open(src, "w", encoding="utf-8") as fh:
    fh.write("x")
_try("os.mkdir", lambda: os.mkdir(sub))
_try("os.utime", lambda: os.utime(src))
_try("os.chmod", lambda: os.chmod(src, 0o644))
_try("os.truncate", lambda: os.truncate(src, 0))
_try("shutil.copyfile", lambda: shutil.copyfile(src, dst))
_try("os.link", lambda: os.link(src, os.path.join(root, "hard.txt")))
_try("os.symlink", lambda: os.symlink(src, os.path.join(root, "soft.txt")))
_try("os.rename", lambda: os.rename(dst, os.path.join(root, "moved.txt")))
_try("os.replace", lambda: os.replace(os.path.join(root, "moved.txt"), dst))
_try("os.remove", lambda: os.remove(dst))
_try("os.rmdir", lambda: os.rmdir(sub))
_try("shutil.rmtree", lambda: (os.mkdir(sub), shutil.rmtree(sub)))

print("@@" + json.dumps(SEEN))
"""


def _audit_trace(tmp_path):
    """跑那支探針，回 `[(event, args), …]`。"""
    # 這個檔的慣例是就地 import（模組層只留 stdlib 的幾個），照著走。
    import json
    import subprocess
    probe = tmp_path / "audit_probe.py"
    probe.write_text(_AUDIT_PROBE, encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    out = subprocess.run(
        [sys.executable, str(probe), str(work)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, cwd=str(tmp_path),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert out.returncode == 0, out.stdout[-800:] + out.stderr[-800:]
    line = next((ln for ln in out.stdout.splitlines() if ln.startswith("@@")),
                None)
    assert line, f"探針沒有輸出事件表：{out.stdout[-400:]}"
    return [tuple(row) for row in json.loads(line[2:])]


def test_every_declared_write_event_is_one_python_actually_raises(
        tmp_path, repo_write_guard):
    """表裡的每一個事件名，都要是 CPython 真的會發出來的那個名字。

    打錯一個字母的後果是**那一格永遠不會觸發**，而且完全沒有症狀：守門照跑、整套
    照綠，`_WRITE_EVENTS` 讀起來也像是管著那個操作。這跟 `_OWNER_ONLY_SLASH` 的
    失效形狀一樣，差別只在這裡的代價是正式檔案被測試改掉。

    `os.rename` 特別要緊：它同時是 `os.replace` 發的事件，而 `os.replace` 正是整個
    專案做原子寫入的手段——漏掉它等於整類跨行程檔案的保護消失。
    """
    trace = _audit_trace(tmp_path)
    raised = {event for event, _args in trace}
    failed = {event[len("!failed:"):] for event, _args in trace
              if event.startswith("!failed:")}
    declared = set(repo_write_guard.write_events)
    # 平台不支援的（Windows 沒開發者模式時的 symlink、跨磁碟的 hard link）跳過，但
    # 要把跳過的列出來——靜靜跳過等於少測一格而沒有人知道。
    unsupported = declared & failed
    missing = declared - raised - unsupported
    assert not missing, (
        f"這些事件名 CPython 從來沒有發出來過，那幾格永遠不會觸發：{sorted(missing)}"
        f"（這一輪平台不支援而跳過的：{sorted(unsupported)}）")
    # 正面對照：探針真的做了事，不是一份空的追蹤。
    assert len(raised) > len(declared), (
        f"只錄到 {len(raised)} 種事件，探針八成沒跑到——這一支會在一份空的清單上"
        "「通過」")
    assert "os.rename" in raised and "os.rename" not in unsupported, (
        "`os.replace`／`os.rename` 這一格沒有被驗到，而原子寫入全靠它")


# 探針裡「這次操作真正改動到的那一個」的檔名。宣告的位置必須指到它。
_AUDIT_EXPECTED = {
    "os.mkdir": {"sub"},
    "os.utime": {"src.txt"},
    "os.chmod": {"src.txt"},
    "os.truncate": {"src.txt"},
    "shutil.copyfile": {"dst.txt"},
    "os.link": {"hard.txt"},
    "os.symlink": {"soft.txt"},
    # rename 兩邊都宣告了：來源會消失、目的地會被蓋掉，兩個都算改動。
    "os.rename": {"dst.txt", "moved.txt"},
    "os.remove": {"dst.txt"},
    "os.rmdir": {"sub"},
    "shutil.rmtree": {"sub"},
}


def test_each_declared_position_is_the_path_that_gets_written(
        tmp_path, repo_write_guard):
    """每一個位置索引指到的，必須是**會被改動**的那個路徑。

    選錯位置是**漏報**：`shutil.copyfile(src, dst)` 若取 index 0，「把東西複製進
    repo 蓋掉正式檔」會直接放行，而「從 repo 複製出去」反而被擋——兩個方向都錯，
    但只有前者會造成損失，而且它不會有任何症狀。

    判準不是比對字串，是拿探針**實際**跑那個操作時 CPython 給的引數。
    """
    assert set(_AUDIT_EXPECTED) == set(repo_write_guard.write_events), (
        "這份對照表跟 `_WRITE_EVENTS` 對不起來了——新增一個事件時這裡也要加，"
        "否則新的那一格不會被驗到")
    trace = _audit_trace(tmp_path)
    seen = {}
    for event, args in trace:
        if event in repo_write_guard.write_events:
            seen.setdefault(event, args)
    checked = 0
    for event, positions in repo_write_guard.write_events.items():
        args = seen.get(event)
        if args is None:
            continue                      # 平台不支援，上一支已經點名列出
        picked = {os.path.basename(args[i]) for i in positions
                  if i < len(args) and isinstance(args[i], str)}
        assert picked == _AUDIT_EXPECTED[event], (
            f"{event} 宣告的位置 {positions} 指到 {sorted(picked)}，"
            f"而這次操作真正改動的是 {sorted(_AUDIT_EXPECTED[event])}")
        checked += 1
    assert checked >= 8, f"只驗到 {checked} 個事件，其餘全被跳過了"


@pytest.mark.parametrize("event, args, blocked", [
    # 目的地在 repo 裡 → 擋。
    ("shutil.copyfile", ("/tmp/outside.txt", "axiomatic/victim.py"), True),
    # 來源在 repo 裡、目的地在外面 → **不**擋。從 repo 複製出去不改動任何東西。
    ("shutil.copyfile", ("axiomatic/discord_bot.py", "/tmp/copy.py"), False),
    ("os.remove", ("axiomatic/victim.py",), True),
    ("os.rename", ("axiomatic/a.py", "/tmp/b.py"), True),
    ("os.rename", ("/tmp/a.py", "axiomatic/b.py"), True),
    ("os.rmdir", ("axiomatic/somedir",), True),
    # 表裡沒有的事件一律不管——否則任何一個稽核事件都會被誤判成寫入。
    ("os.listdir", ("axiomatic",), False),
    ("compile", ("x", "axiomatic/x.py"), False),
])
def test_the_hook_blocks_the_declared_position_and_only_that(
        repo_write_guard, event, args, blocked):
    """直接呼叫掛勾本體：不碰檔案系統，但走的是真正的那段程式碼。

    **直接呼叫是唯一看得見這段的方式。** CPython 在 audit hook 的回呼裡關掉追蹤，
    所以 coverage 對 hook body 永遠回報「沒跑過」。靠覆蓋率排名挑測試目標
    時，這一段會一直排在最前面而其實早就測過；反過來說，它真正的保障必須來自像這
    一支的直接呼叫，而不是靠排名有一天輪到它。
    """
    saved = repo_write_guard.test
    repo_write_guard.test = "audit-position-probe"
    repo_write_guard.violations.clear()
    try:
        if blocked:
            with pytest.raises(PermissionError):
                repo_write_guard.hook(event, args)
        else:
            repo_write_guard.hook(event, args)     # 不該丟
    finally:
        repo_write_guard.test = saved
        repo_write_guard.violations.clear()


def test_the_hook_is_asleep_until_a_test_claims_it(repo_write_guard):
    """反面對照：沒有測試在跑（`test is None`）時掛勾什麼都不做。

    少了它，把那道早退拿掉也會綠——而拿掉的後果是**收集期**與 pytest 自己的內部
    寫入全部被擋下，整個回合連收集都跑不完。
    """
    saved = repo_write_guard.test
    repo_write_guard.test = None
    try:
        repo_write_guard.hook("os.remove", ("axiomatic/victim.py",))
    finally:
        repo_write_guard.test = saved


@pytest.mark.parametrize("mode, flags, is_write", [
    ("r", None, False), ("rb", None, False),
    ("w", None, True), ("wb", None, True), ("a", None, True),
    ("x", None, True), ("r+", None, True), ("w+b", None, True),
    # `os.open` 那一半走 flags；`O_RDONLY` 是 0，所以唯讀不能被算成寫入。
    (None, os.O_RDONLY, False),
    (None, os.O_WRONLY | os.O_CREAT, True),
    (None, os.O_RDWR, True),
    (None, os.O_APPEND, True),
])
def test_only_a_writing_open_counts_as_a_write(repo_write_guard, mode, flags,
                                               is_write):
    """`open` 那一格要分得出讀與寫，兩個方向都要。

    太鬆的話整套會被自己的讀取擋死（這個 repo 的測試讀了非常多自己的原始碼）；
    太緊的話——`"r+"` 或 `os.O_RDWR` 被當成唯讀——正式檔案就從那個缺口被改掉。
    `os.O_RDONLY == 0` 是這裡唯一的陷阱：位元遮罩對它永遠是 0，所以「唯讀」是靠
    「什麼旗標都沒中」表示的，不是靠一個自己的位元。
    """
    targets = repo_write_guard.write_targets("open", ("some/path", mode, flags))
    assert bool(targets) is is_write, (mode, flags, targets)


def test_a_path_that_merely_shares_the_repo_prefix_is_outside(
        repo_write_guard):
    """跟 repo 同名開頭的**兄弟目錄**不算在 repo 裡。

    判準要帶上路徑分隔符。少了它，任何一個兄弟目錄（備份、worktree、`_old`）都會
    被當成 repo 內部——而那是誤報，誤報久了就會有人把整道防線關掉。
    """
    root = repo_write_guard.repo_root
    assert repo_write_guard.target(root + "_backup" + os.sep + "x.py") is None
    inside = os.path.join(root, "axiomatic", "x.py")
    assert repo_write_guard.target(inside) is not None, (
        "連真的在裡面的路徑都認不出來，上面那一句就沒有意義了")
