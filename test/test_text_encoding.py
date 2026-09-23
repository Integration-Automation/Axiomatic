"""本專案自己的程式碼不得用**平台地區編碼**讀寫文字。

這台機器上 `locale.getpreferredencoding(False)` 是 **cp950**，而這個專案幾乎所有
會被讀進來的內容都是中文——佇列檔（`todo_*.md`）、提示詞、log、git commit 主旨、
子行程的輸出。Python 3.14 還沒把 UTF-8 變成預設，所以任何一個省略 `encoding=`
的文字讀寫都是在拿 cp950 解 UTF-8。

**兩種失敗都很難查**：非法位元組會丟 `UnicodeDecodeError`（常常被上層一句
`except Exception` 吞掉，只剩一個泛用錯誤訊息），合法但不同義的位元組會安靜地
變成亂碼寫回磁碟——後者會**汙染資料**，而且要等有人用眼睛看到才會發現。

而這在別的機器上完全看不出來：CI 沒有（本專案沒有 CI），開發者的機器如果是
英文地區設定就是 cp1252，寫進 repo 的程式碼在那裡跑得好好的。所以這條只能靜態
守，不能靠「跑跑看」。

## 掃描範圍是刻意收窄的

只認三種**沒有歧義**的寫法（見 `_offenders`）。曾經試過更寬的版本，六筆命中裡
四筆是假警報——`gui.read_text()` 是本專案自己的文字辨識函式（跟
`Path.read_text` 撞名）、`fake_grab.open(dest)` 是測試替身。`CLAUDE.md` 對
`test_language.py` 的判斷同樣適用：**會亂叫的守門就是會被關掉的守門**，所以寧可
漏掉幾個要人看的邊角，也不要每次改動都得先安撫它。

被刻意排除、仍然需要人看的：`某物.open()` 在看不到字面模式字串時（可能是路徑物
件，也可能是任何剛好有 `open` 方法的東西）。
"""
from __future__ import annotations

import ast
import locale
import re
import subprocess
from pathlib import Path

import pytest

# 「這個接收者根本不是檔案系統」的名單**只有一份**，在 `test_atomic_writes`。
# 本專案有兩支測試在守同一條規則（本檔掃全 repo，`test_atomic_writes` 掃跨行程
# 檔案那一組），各自帶一份豁免名單的話就會分歧：一邊加了豁免、另一邊沒加，同一
# 行程式在兩支測試裡一支綠一支紅，而下一個人只會把它加到「正在紅的那一支」，於是
# 兩份名單從此各走各的。這裡直接 import 同一個物件，讓分歧在結構上做不到。
from test_atomic_writes import _NOT_FILESYSTEM_RECEIVERS
from test_language import _DATA_MARKDOWN

REPO_ROOT = Path(__file__).resolve().parent.parent

# 不掃：虛擬環境、執行期產物、非本專案的原始碼。
_SKIP_DIRS = {".venv", ".git", "node_modules", "output", "docs",
              ".backup", ".chrome_profile", ".chrome_profile_snap"}

_SUBPROCESS_CALLS = {"run", "Popen", "check_output", "getoutput", "getstatusoutput"}
# ⚠️ **非同步那幾支刻意不在這裡**：`asyncio.create_subprocess_exec` /
# `create_subprocess_shell` 根本沒有 `text=`／`encoding=` 參數，永遠回 bytes，
# 所以「文字有沒有指定編碼」這條規則對它們沒有著力點——解碼是呼叫端自己寫的
# `.decode(...)`，而那一行離 spawn 可能很遠，靜態追過去只會開始亂叫。
# 2026-09-12 量過才這樣決定，不是漏掉：生產程式碼裡 5 處非同步 spawn
# （`discord_bot.py:9577`／`:9630`、`dorossi_backend.py:2987`／`:3519`、
# `presence_probe.py:663`），開的**全部都不是 Python 子行程**（外部 CLI 與
# PowerShell），所以 `PYTHONIOENCODING` 對它們沒有意義；而且每一處都自己拿 bytes
# 再明確 `.decode("utf-8", errors="replace")`。
# 真的哪天出現 `create_subprocess_exec(sys.executable, ...)` 再回來處理——那會是
# 一個**有實例**的問題，屆時語料才咬得住它。
_TEXT_MODE_KWARGS = {"text", "universal_newlines"}


def _project_sources() -> list[Path]:
    return sorted(p for p in REPO_ROOT.rglob("*.py")
                  if not (_SKIP_DIRS & set(p.relative_to(REPO_ROOT).parts)))


def _imported_module_names(tree: ast.Module) -> set[str]:
    """這個檔案裡「名字指向一個模組」的那些名字。

    用來排掉撞名：`gui.read_text()` 的 `gui` 是 `import _gui_control as gui`，
    那是本專案自己的辨識函式，不是 `Path.read_text`。
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _root_receiver(func: ast.Attribute) -> str | None:
    node = func.value
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _literal_mode(call: ast.Call, *, positional: int) -> str | None:
    """看得見的字面模式字串；看不見就回 None（＝不下判斷）。"""
    if len(call.args) > positional and isinstance(call.args[positional], ast.Constant):
        value = call.args[positional].value
        return value if isinstance(value, str) else None
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            value = kw.value.value
            return value if isinstance(value, str) else None
    return None


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    modules = _imported_module_names(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if "encoding" in kwargs:
            continue
        is_attr = isinstance(func, ast.Attribute)
        name = func.attr if is_attr else getattr(func, "id", None)
        receiver = _root_receiver(func) if is_attr else None

        # 1) 裸的 `open(...)`，文字模式。這一定是 builtins 的 open。
        if name == "open" and not is_attr:
            mode = _literal_mode(node, positional=1)
            if mode is None or "b" not in mode:
                out.append(f"{path.name}:{node.lineno} open() 沒給 encoding")

        # 2) `某物.open('r'...)` —— 只在**看得到**字面文字模式時才算。看不到模式
        #    的 `.open(x)` 太可能是測試替身或影像函式庫，不下判斷。
        elif (name == "open" and is_attr and receiver not in modules
                and receiver not in _NOT_FILESYSTEM_RECEIVERS):
            mode = _literal_mode(node, positional=0)
            if mode is not None and "b" not in mode:
                out.append(f"{path.name}:{node.lineno} .open({mode!r}) 沒給 encoding")

        # 3) `Path.read_text` / `write_text`。排掉「接收者是模組」的撞名，以及
        #    `_NOT_FILESYSTEM_RECEIVERS` 裡那些「名字撞到但根本不是檔案」的接收者。
        elif (name in ("read_text", "write_text") and receiver not in modules
                and receiver not in _NOT_FILESYSTEM_RECEIVERS):
            out.append(f"{path.name}:{node.lineno} {name}() 沒給 encoding")

        # 4) 子行程要文字輸出卻沒指定編碼。
        elif name in _SUBPROCESS_CALLS and (_TEXT_MODE_KWARGS & kwargs):
            out.append(f"{path.name}:{node.lineno} "
                       f"{name}(text=…) 沒給 encoding")
    return out


@pytest.mark.parametrize(
    "source", [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for p in _project_sources()])
def test_text_io_never_uses_the_locale_codec(source):
    """每一個文字讀寫／子行程輸出都要明寫 `encoding`。

    修法是補 `encoding="utf-8"`。讀**外部**來源（子行程輸出、第三方回應）時再加
    `errors="replace"`：那些內容本專案控制不了，寧可拿到一個帶替代字元的字串，
    也不要讓整條路徑因為一個位元組而丟例外。
    """
    problems = _offenders(REPO_ROOT / source)
    assert not problems, (
        "這些地方會用**平台地區編碼**（本機是 "
        f"{locale.getpreferredencoding(False)}）處理文字：\n  "
        + "\n  ".join(problems)
        + '\n改法：補上 `encoding="utf-8"`（讀外部來源再加 '
          '`errors="replace"`）。二進位就明寫 `"rb"`／`"wb"`。')


def test_the_scanner_actually_catches_the_shapes_it_claims_to(tmp_path):
    """守門自己要能認出那四種寫法——否則它只是一支永遠綠的測試。

    這比看起來重要：`_offenders` 是一串 `elif`，任何一支的條件寫錯（打錯屬性名、
    `not in modules` 寫反）都會讓它安靜地永遠回空集合，而全套測試照樣全綠。

    樣本放在 tmp_path（2026-09-19 前是寫進套件目錄再刪掉）：`_offenders` 只讀檔，
    位置無關；寫進 repo 的話，它存在的那段時間裡別的掃全樹的測試會看到它，這支若在
    刪掉之前被砍掉，就留下一個假的 `.py` 在套件裡。conftest 的 repo 寫入守門抓到的。
    """
    sample = tmp_path / "_encoding_scanner_sample.py"
    sample.write_text(
        "import subprocess\n"
        "from pathlib import Path\n"
        "import _gui_control as gui\n"
        "\n"
        "def bad_open(p):\n"
        "    return open(p).read()\n"
        "def bad_path_open(p):\n"
        "    return Path(p).open('r').read()\n"
        "def bad_read_text(p):\n"
        "    return Path(p).read_text()\n"
        "def bad_subprocess():\n"
        "    return subprocess.run(['git'], text=True)\n"
        "def ok_binary(p):\n"
        "    return open(p, 'rb').read()\n"
        "def ok_explicit(p):\n"
        "    return Path(p).read_text(encoding='utf-8')\n"
        "def ok_not_a_path():\n"
        "    return gui.read_text()\n",
        encoding="utf-8")
    try:
        found = _offenders(sample)
    finally:
        sample.unlink()
    kinds = " ".join(found)
    assert len(found) == 4, f"應該剛好抓到 4 筆，實際：{found}"
    assert "open() 沒給 encoding" in kinds
    assert ".open('r')" in kinds
    assert "read_text() 沒給 encoding" in kinds
    assert "run(text=…)" in kinds
    # 反方向：三種正確寫法一個都不能被誤報。`gui.read_text()` 尤其重要——它是
    # 本專案自己的文字辨識函式，跟 `Path.read_text` 撞名。
    assert not any("ok_" in f for f in found)


def test_the_scanner_sees_every_python_file_in_the_live_stack():
    """掃描範圍不得被 `_SKIP_DIRS` 悄悄挖空。

    這條守的是「有人為了讓測試變綠，把某個目錄加進排除清單」。排除清單只該有
    非本專案原始碼的目錄。
    """
    scanned = {p.name for p in _project_sources()}
    for must in ("discord_bot.py", "webrunner_novelai.py", "webrunner_je_only.py",
                 "_webrunner_shared.py", "dashboard_server.py",
                 "start_discord_bot.py", "start_webrunner.py",
                 "verify_browser.py"):
        assert must in scanned, f"{must} 沒被掃到——`_SKIP_DIRS` 是不是挖太寬了？"


# ---------------------------------------------------------------------------
# 第五條：`encoding=` 只管**解碼端**，子行程的**編碼端**要另外交代
#
# 上面那四條問的是「我們這一端有沒有指名編碼」。但一個 Python 子行程把 stdout 接到
# **管線**時，CPython 用的是 locale 編碼（本機 cp950），不是 UTF-8——父行程寫
# `encoding="utf-8"` 對**子行程怎麼編碼**沒有任何作用。兩邊不一致的結果是：
# `errors="replace"` 把它靜靜換成一串 U+FFFD，沒有例外、沒有紅字。
#
# 實測（2026-09-12，乾淨環境）：子行程印一句 15 字的中文，父行程照專案原本的寫法
# 讀回來是 **14 個 U+FFFD**；改成跑一支中文斷言失敗的 child pytest，**60 個**。
#
# ⚠️ **這個缺陷在我自己的殼裡量不到。** 開發用的殼設了
# `PYTHONIOENCODING=utf-8:surrogateescape`，於是子行程剛好是 UTF-8、一切正常；
# 把 `PYTHON*` 變數清掉才看得到 cp950。所以「我跑過沒問題」在這條規則上完全不算數，
# 而這也正是它必須是**靜態**守門的理由。
#
# 專案裡原本只有 `_supervisor.stream_child` 做對（它從一開始就設
# `PYTHONIOENCODING`，docstring 也寫明了理由）。其餘 16 處沒有——其中兩處還帶著
# 「Windows 上 `text=True` 預設走 cp950」的註解，也就是**診斷對了、藥下在另一端**。
#
# 補一句給後來的人（2026-09-12）：那唯一「做對」的一處其實只對了一半——它寫的是
# `env.setdefault(...)`（讓步），docstring 卻寫著「強制」，所以呼叫端環境已經帶著
# 一個錯的值時是那個錯的值贏。同日改成覆寫。**不要拿「它本來就寫對」當成
# `setdefault` 可以接受的先例。**
#
# ⚠️ **`stream_child` 本身掃不到，而那不是漏洞。** 它的命令列是呼叫端給的變數
# （`subprocess.Popen(cmd, ...)`），靜態上看不出那是不是 Python 子行程，所以
# `_spawns_a_python_child` 不選它。它由**兩支行為測試**守著：
# `test_supervisor.test_the_child_gets_utf8_io_encoding` 會先
# `os.environ.pop("PYTHONIOENCODING")` 再量，理由白紙黑字寫在它的 docstring
# 裡——「不然這支在『開發者的殼剛好已經設了』的機器上會永遠綠，量到的是環境而不是
# 程式碼」；而 `test_a_wrong_pythonioencoding_in_the_parent_is_overridden` 量的是
# 另一半（環境裡帶著 `cp950`）。**兩半都要**：變數不在時「覆寫」與「讓步」的行為
# 一模一樣，所以只有後者殺得掉「退回 `setdefault`」那個變異（2026-09-12 實測 4/4
# 全殺，退回 `setdefault` 那一個正是只有後者紅）。
# 要加第四種寫法之前先看它；不要為了讓這支靜態守門「涵蓋全部」去改 `stream_child`
# 的簽名。
# ---------------------------------------------------------------------------
_PY_CHILD_TELLS = ("sys.executable", "'py'", '"py"')
_CHILD_ENCODING_ENV = "PYTHONIOENCODING"


# 這幾支**本來就會**擷取 stdout，呼叫端不必寫 `capture_output=`／`stdout=PIPE`。
# 漏掉它們的話 `subprocess.check_output([sys.executable, ...], encoding="utf-8")`
# 整個逃得掉（2026-09-12 自己複查時抓到的範圍漏洞）。
_ALWAYS_CAPTURING = {"check_output", "getoutput", "getstatusoutput"}


def _captures_child_text(call: ast.Call) -> bool:
    """這個呼叫有沒有「把子行程的輸出當文字收回來」。

    兩個條件都要：**收得到**而且**當文字處理**（`encoding=` 或
    `text=`／`universal_newlines=`）。只看其中一個會誤報——`stdout=DEVNULL` 的呼叫
    沒有人讀，編碼怎樣都無所謂。

    「收得到」有三種：`capture_output=True`、把 `stdout`／`stderr` 導到 `PIPE`
    （stderr 也算，子行程的 stderr 一樣走 locale 編碼），以及**函式本身就會擷取**
    的那幾支（`_ALWAYS_CAPTURING`）。
    """
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords if kw.arg}
    as_text = ("encoding" in kwargs) or bool(_TEXT_MODE_KWARGS & set(kwargs))
    func = call.func
    fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    captured = (kwargs.get("capture_output") == "True"
                or "PIPE" in kwargs.get("stdout", "")
                or "PIPE" in kwargs.get("stderr", "")
                or fname in _ALWAYS_CAPTURING)
    return as_text and captured


def _spawns_a_python_child(call: ast.Call) -> bool:
    """第一個引數看起來是不是在叫另一個 Python。

    只認得出**寫在呼叫點上**的那些；用變數組好的命令列看不出來，那是刻意的——
    會亂叫的守門遲早被人關掉，而這條規則只在「我們自己開 Python 子行程」時成立
    （對 `git`、`schtasks` 這類外部工具，正確答案是別的編碼，不是 UTF-8）。
    """
    if not call.args:
        return False
    first = ast.unparse(call.args[0])
    return any(tell in first for tell in _PY_CHILD_TELLS)


def _sets_the_child_encoding(node: ast.AST | None) -> bool:
    """這段 AST 裡有沒有**正好等於** `PYTHONIOENCODING` 的字串常數。

    ⚠️ **判準是相等，不是包含，而且刻意走 AST。** 第一版用 `"PYTHONIOENCODING" in
    ast.unparse(函式)` 做子字串比對，結果是這支守門對 `_supervisor.stream_child`
    ——全專案唯一本來就寫對的那一處——**完全瞎掉**：那支的 docstring 自己在解釋
    `PYTHONIOENCODING` 是什麼，於是把 `env.setdefault(...)` 那一行整個刪掉，守門
    照樣放行（2026-09-12 變異實測，SURVIVED）。改成比對 `ast.Constant` 的**值**
    之後，docstring 的值是一整段說明、不等於那個名字，註解則根本不在 AST 裡。
    """
    if node is None:
        return False
    return any(isinstance(sub, ast.Constant) and sub.value == _CHILD_ENCODING_ENV
               for sub in ast.walk(node))


def _child_encoding_offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    # 節點 -> 包著它的函式；外加模組層函式的名字表，給 `env=_helper()` 那種寫法。
    enclosing: dict[int, ast.AST] = {}
    by_name: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            by_name[node.name] = node
            for inner in ast.walk(node):
                enclosing.setdefault(id(inner), node)

    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in _SUBPROCESS_CALLS:
            continue
        if not (_spawns_a_python_child(node) and _captures_child_text(node)):
            continue

        env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
        if env is None:
            out.append(f"{path.name}:{node.lineno} 開 Python 子行程收文字，卻沒給 env")
            continue
        # 三種寫得到的位置：字面 dict、外層函式裡先組好、指名的 helper。
        places = [env, enclosing.get(id(node))]
        if isinstance(env, ast.Call) and isinstance(env.func, ast.Name):
            places.append(by_name.get(env.func.id))
        if not any(_sets_the_child_encoding(p) for p in places):
            out.append(f"{path.name}:{node.lineno} env 裡沒有 {_CHILD_ENCODING_ENV}")
    return out


@pytest.mark.parametrize(
    "source", [str(p.relative_to(REPO_ROOT)).replace("\\", "/")
               for p in _project_sources()])
def test_a_python_child_is_told_to_speak_utf8(source):
    """開 Python 子行程並把輸出當文字收回來時，要把子行程也設成 UTF-8。"""
    problems = _child_encoding_offenders(REPO_ROOT / source)
    assert not problems, (
        "這些地方只設了**解碼端**：\n  " + "\n  ".join(problems)
        + f'\n改法：加上 `env={{**os.environ, "{_CHILD_ENCODING_ENV}": "utf-8"}}`。'
          "父行程的 `encoding=` 管不到子行程怎麼編碼——管線上 CPython 走的是 "
          "locale 編碼（本機 cp950），不一致的結果是一串 U+FFFD，不是例外。")


def test_the_child_encoding_scan_actually_selects_something():
    """正面對照組：選不到東西跟「全部乾淨」長得一模一樣。

    下限是量出來的（2026-09-12 全專案 17 處合格呼叫），刻意寫得比實測低一截，
    免得刪掉一支合理的探針就讓這支亂叫。
    """
    qualifying = 0
    for path in _project_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None))
            if name in _SUBPROCESS_CALLS and _spawns_a_python_child(node) \
                    and _captures_child_text(node):
                qualifying += 1
    assert qualifying >= 10, (
        f"全專案只掃到 {qualifying} 處「開 Python 子行程收文字」的呼叫——"
        "抽取器大概壞了，而空的抽取結果跟乾淨的結果長得一模一樣。")


def test_the_child_encoding_scan_catches_a_violation_and_allows_the_fix(tmp_path):
    """合成對照組：必須擋的要擋，必須放的要放。

    ⚠️ 兩面都要測。只放「必須擋」的語料時，把 `_captures_child_text` 改成永遠回
    True 這種**放寬**的相反——也就是**收緊**——不會被抓到；而收緊正是這支守門最
    可能的壞法（它一亂叫，下一個人就把它關掉）。所以合規的三種寫法各一筆。
    """
    bad = tmp_path / "bad_probe.py"
    bad.write_text(
        "import subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'print(1)'],\n"
        "               capture_output=True, text=True, encoding='utf-8')\n",
        encoding="utf-8")
    assert _child_encoding_offenders(bad), "沒有 env 的違規沒被抓到"

    # 有 env、但沒設那個變數——跟「完全沒給 env」是**不同的分支**。少了這一筆，
    # 把整個判準關掉（`if False and not any(...)`）在真實資料上一樣全綠，因為
    # 現在每一個站點都已經修好了（2026-09-12 變異實測，那個變異原本 SURVIVED）。
    env_without = tmp_path / "env_without.py"
    env_without.write_text(
        "import os, subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'print(1)'],\n"
        "               capture_output=True, text=True, encoding='utf-8',\n"
        "               env={**os.environ, 'SOMETHING_ELSE': '1'})\n",
        encoding="utf-8")
    assert _child_encoding_offenders(env_without), (
        "給了 env 卻沒設 PYTHONIOENCODING 沒被抓到——這是第二個分支，"
        "不會被「完全沒給 env」那一筆覆蓋到。")

    # 只在 docstring 裡**講到**那個變數不算設定。這一筆釘住「比對 AST 常數的值，
    # 不是對整份原始碼做子字串比對」：第一版就是栽在這裡，而栽的對象正好是全專案
    # 唯一本來就寫對的 `_supervisor.stream_child`（它的 docstring 在解釋這件事）。
    doc_only = tmp_path / "doc_only.py"
    doc_only.write_text(
        "import os, subprocess, sys\n"
        "def go():\n"
        '    """這裡本來應該設 PYTHONIOENCODING，但只是講講而已。"""\n'
        "    env = dict(os.environ)\n"
        "    return subprocess.run([sys.executable, '-c', 'print(1)'],\n"
        "                          capture_output=True, text=True,\n"
        "                          encoding='utf-8', env=env)\n",
        encoding="utf-8")
    assert _child_encoding_offenders(doc_only), (
        "docstring 提到那個變數就放行了——守門被自己的說明文字騙過去。")

    # 1) 字面 dict
    ok_literal = tmp_path / "ok_literal.py"
    ok_literal.write_text(
        "import os, subprocess, sys\n"
        "subprocess.run([sys.executable, '-c', 'print(1)'],\n"
        "               capture_output=True, text=True, encoding='utf-8',\n"
        "               env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})\n",
        encoding="utf-8")
    assert not _child_encoding_offenders(ok_literal), "字面 dict 被誤報"

    # 2) 在外層函式裡先組好再傳（`_supervisor.stream_child` 就是這個形狀）
    ok_local = tmp_path / "ok_local.py"
    ok_local.write_text(
        "import os, subprocess, sys\n"
        "def go():\n"
        "    env = dict(os.environ)\n"
        "    env.setdefault('PYTHONIOENCODING', 'utf-8')\n"
        "    return subprocess.run([sys.executable, '-c', 'print(1)'],\n"
        "                          capture_output=True, text=True,\n"
        "                          encoding='utf-8', env=env)\n",
        encoding="utf-8")
    assert not _child_encoding_offenders(ok_local), "區域變數形狀被誤報"

    # 3) 指名的 helper（`mutation_harness._child_env` 就是這個形狀）
    ok_helper = tmp_path / "ok_helper.py"
    ok_helper.write_text(
        "import os, subprocess, sys\n"
        "def _child_env():\n"
        "    return {**os.environ, 'PYTHONIOENCODING': 'utf-8'}\n"
        "def go():\n"
        "    return subprocess.run([sys.executable, '-c', 'print(1)'],\n"
        "                          capture_output=True, text=True,\n"
        "                          encoding='utf-8', env=_child_env())\n",
        encoding="utf-8")
    assert not _child_encoding_offenders(ok_helper), "helper 形狀被誤報"

    # 4) 沒人讀輸出的呼叫不在管轄範圍——這一條是**放寬**步驟，要有語料釘住
    devnull = tmp_path / "devnull_probe.py"
    devnull.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'pass'],\n"
        "                 stdout=subprocess.DEVNULL, text=True,\n"
        "                 encoding='utf-8')\n",
        encoding="utf-8")
    assert not _child_encoding_offenders(devnull), (
        "`stdout=DEVNULL` 沒有人讀，編碼怎樣都無所謂；抓它就是亂叫。")

    # 5) 走**位元組**的管線也不在管轄範圍——沒有 `text=`／`encoding=` 就沒有解碼，
    #    呼叫端自己決定怎麼解。這一筆是**放寬**步驟的語料：少了它，把判準收緊成
    #    只看 `captured` 在真實資料上一樣全綠（2026-09-12 變異實測，SURVIVED），
    #    而收緊正是這支最可能的壞法——它一亂叫，下一個人就把它關掉。
    binary_pipe = tmp_path / "binary_pipe.py"
    binary_pipe.write_text(
        "import subprocess, sys\n"
        "out = subprocess.run([sys.executable, '-c', 'print(1)'],\n"
        "                     capture_output=True).stdout.decode('utf-8')\n",
        encoding="utf-8")
    assert not _child_encoding_offenders(binary_pipe), (
        "沒有 `text=`／`encoding=` 的呼叫拿到的是 bytes，這條規則管不到它。")

    # 6) `check_output` 不必寫 `capture_output=`／`stdout=PIPE` 就會擷取 stdout。
    #    少了這一筆，整個 `_ALWAYS_CAPTURING` 分支刪掉也不會有人發現——目前樹上
    #    沒有任何一處是這個形狀，所以真實資料咬不到它（2026-09-12 複查時補）。
    check_output = tmp_path / "check_output_probe.py"
    check_output.write_text(
        "import subprocess, sys\n"
        "out = subprocess.check_output([sys.executable, '-c', 'print(1)'],\n"
        "                              text=True, encoding='utf-8')\n",
        encoding="utf-8")
    assert _child_encoding_offenders(check_output), (
        "`check_output` 本來就會擷取 stdout，沒有 env 一樣是這個缺陷。")

    # 7) 只導 stderr 也算——子行程的 stderr 同樣走 locale 編碼。
    stderr_only = tmp_path / "stderr_only_probe.py"
    stderr_only.write_text(
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'print(1)'],\n"
        "                     stderr=subprocess.PIPE, text=True,\n"
        "                     encoding='utf-8')\n",
        encoding="utf-8")
    assert _child_encoding_offenders(stderr_only), (
        "只導 stderr 一樣是把子行程的文字收回來。")

    # 8) 外部工具不在管轄範圍——它們的正確答案是 OEM，不是 UTF-8
    external = tmp_path / "external_probe.py"
    external.write_text(
        "import subprocess\n"
        "subprocess.run(['schtasks', '/Query'], capture_output=True,\n"
        "               text=True, encoding='oem')\n",
        encoding="utf-8")
    assert not _child_encoding_offenders(external), (
        "主控台工具不是 Python 子行程，`PYTHONIOENCODING` 對它沒有意義。")


def test_every_always_capturing_name_can_actually_reach_the_predicate():
    """`_ALWAYS_CAPTURING` 的每一筆都必須是 `_SUBPROCESS_CALLS` 的成員。

    `_child_encoding_offenders` **先**用 `_SUBPROCESS_CALLS` 過濾，過不了的呼叫
    根本走不到 `_captures_child_text`。所以在 `_ALWAYS_CAPTURING` 裡寫一個不在
    前者的名字，那一筆是**永遠碰不到的**——但清單看起來有在管事，而且沒有任何
    症狀：不會紅、不會少掃到既有的東西，只是那個形狀從此不設防。

    `getstatusoutput` 正是這個形狀的活例：它在兩份清單裡都有，少掉任何一邊
    都沒人會發現。
    """
    assert _ALWAYS_CAPTURING, "空集合＝整個分支是裝飾品"
    unreachable = _ALWAYS_CAPTURING - _SUBPROCESS_CALLS
    assert not unreachable, (
        f"這些名字掃描根本走不到：{sorted(unreachable)}。"
        "`_child_encoding_offenders` 先過 `_SUBPROCESS_CALLS`，"
        "所以兩份清單要一起改。")


def test_the_two_encoding_guards_share_one_exemption_list():
    """兩支守同一條規則的測試，豁免名單必須是**同一個物件**。

    本專案有兩處在掃「文字 I/O 有沒有指定 encoding」：本檔掃全 repo，
    `test_atomic_writes` 掃跨行程檔案那一組。掃描邏輯不同是刻意的，但「這個接收者
    根本不是檔案系統」是同一個事實，各留一份就會分歧——而分歧的症狀特別容易被
    誤讀：同一行程式在一支測試裡綠、另一支裡紅，下一個人只會把豁免補進**正在紅
    的那一支**，於是兩份名單從此各走各的。

    比 `is` 而不是比內容：內容相等只代表「此刻剛好一樣」，`is` 才排除得掉「有人
    又抄了一份、目前內容湊巧相同」。
    """
    import test_atomic_writes

    assert _NOT_FILESYSTEM_RECEIVERS is (
        test_atomic_writes._NOT_FILESYSTEM_RECEIVERS), (
        "本檔又自己留了一份豁免名單。要豁免就加進 `test_atomic_writes."
        "_NOT_FILESYSTEM_RECEIVERS`（連理由一起寫），兩邊自動一致。")
    assert _NOT_FILESYSTEM_RECEIVERS, "豁免名單是空的——正面對照組失效了"
    # 每一筆都要寫理由：沒有理由的豁免下一個人不敢刪，也不知道還成不成立。
    for receiver, reason in _NOT_FILESYSTEM_RECEIVERS.items():
        assert isinstance(reason, str) and len(reason) > 10, (
            f"`{receiver}` 的豁免沒有寫清楚理由：{reason!r}")


# ---------------------------------------------------------------------------
# 印出去的字，主控台的地區編碼必須編得出來
# ---------------------------------------------------------------------------
# 本檔上半部整條規則都在講**解碼**：讀進來的位元組要用對的編碼。這一段是它的
# **另外一半**——寫出去的字元，那一端編不編得出來。
#
# `print()` 在 Windows 上有兩種完全不同的下場，而它們的差別正好等於「開發時看不到、
# 自動化時致命」：
#   * stdout 是**真的主控台**→ CPython 走 `_WindowsConsoleIO`／`WriteConsoleW`，
#     Unicode 直接送進主控台，代碼頁不參與，什麼字都印得出來。
#   * stdout 是**管線或檔案**（被重新導向、被工具擷取、被啟動器 spawn）→ 走地區
#     編碼，本機是 **cp950**。編不出來的字元不是印成亂碼，是
#     **`UnicodeEncodeError`——行程直接死掉**。
#
# 實測（2026-09-12，乾淨環境、剝掉 `PYTHON*`、stdout 接成管線）：
# `sys.stdout.encoding` ＝ `cp950`，`print("警告")` 正常（Big5 `\xc4\xb5\xa7i`），
# `print("⚠️")` → `UnicodeEncodeError: 'cp950' codec can't encode character
# '⚠'`、**rc=1**。
#
# ⚠️ **為什麼到今天才發現，兩層遮蔽疊在一起**：(1) 手動在主控台裡跑永遠是對的
# （上面第一種），(2) 開發者的殼 export 了 `PYTHONIOENCODING=utf-8`，所以連管線
# 那條也是對的。兩層都不在正式情境裡：排程工作、啟動器、`> out.txt`、或任何擷取
# 輸出的工具都是第二種而且沒有那個環境變數。
#
# 2026-09-12 全專案量到 1433 個列印呼叫、**3 個**編不出來，而三個之中有兩個落在
# 「這個失效本來就沒有其他症狀」的診斷路徑上：同步工具的「寫回去了但
# 還有幾列沒改成功」、`discord_bot` 的「slash 指令只同步了一半」。第三個是
# chromedriver.log 超過上限的警告。**這不是巧合**：`⚠️` 正是寫警告時最順手的字元，
# 而警告正是平常永遠不會執行到的那幾行——那一行唯一會被執行的時刻，就是它自己
# 把行程炸掉的時刻。
#
# 判準只看**字串常數**（f-string 的插值部分是執行期資料，靜態管不到，而且那一端由
# 資料來源決定）。範圍只有 `print` / `_progress` / `sys.std*.write`——送到聊天平台的
# 字串滿滿都是表情符號，那條路是 UTF-8，完全不受這條規則管。
#
# ⚠️ **這條規則綁的是這台機器的代碼頁，而且刻意如此。** 本專案沒有 CI、只有這一台
# 機器在跑東西，而它的 OEM 代碼頁是 950。換一台英文地區的機器，代碼頁是 437，那裡
# 連「檔案」兩個字都印不出來——那是另一個量級的問題，不是這條守門要解的。

_CONSOLE_CODEC = "cp950"
_PRINTER_NAMES = {"print", "_progress"}


def _is_console_write(call: ast.Call) -> bool:
    """這個呼叫是不是「把字印到 stdout／stderr」。"""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _PRINTER_NAMES
    if isinstance(func, ast.Attribute):
        if func.attr in _PRINTER_NAMES:
            return True
        if func.attr == "write":
            return "std" in ast.unparse(func.value)
    return False


def _console_unencodable(source: str, filename: str) -> list[str]:
    """印出去、但 `_CONSOLE_CODEC` 編不出來的字串常數。"""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source, filename)):
        if not (isinstance(node, ast.Call) and _is_console_write(node)):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Constant)
                    and isinstance(sub.value, str)):
                continue
            try:
                sub.value.encode(_CONSOLE_CODEC)
            except UnicodeEncodeError:
                offenders = sorted({ch for ch in sub.value
                                    if not _encodable(ch)})
                codes = " ".join(f"U+{ord(c):04X}" for c in offenders)
                problems.append(
                    f"{filename}:{node.lineno} 印出去的字串有 {_CONSOLE_CODEC} "
                    f"編不出來的字元：{''.join(offenders)!r}（{codes}）")
    return problems


def _encodable(ch: str) -> bool:
    try:
        ch.encode(_CONSOLE_CODEC)
        return True
    except UnicodeEncodeError:
        return False


@pytest.mark.parametrize("path", _project_sources(), ids=lambda p: p.name)
def test_nothing_printed_is_unencodable_on_this_console(path: Path):
    """印出去的字元，地區編碼編不出來就不是亂碼而是例外。"""
    problems = _console_unencodable(
        path.read_text(encoding="utf-8"), path.name)
    assert not problems, (
        "\n".join(problems)
        + f"\n\nstdout 接成管線時（被重新導向、被工具擷取、被啟動器 spawn）"
        f"CPython 用地區編碼 {_CONSOLE_CODEC} 編輸出，編不出來就是 "
        "`UnicodeEncodeError` ——**行程死掉**，不是印成亂碼。"
        "\n手動在主控台裡跑不會重現（那條路走 `WriteConsoleW`，代碼頁不參與），"
        "開發者的殼又 export 了 `PYTHONIOENCODING`，所以兩層都會騙過你。"
        f"\n換成 {_CONSOLE_CODEC} 編得出來的寫法：`⚠️`→`※`(U+203B)、"
        "`≈`→`≒`(U+2252)。送到聊天平台的字串不受這條限制。")


def test_the_console_encodability_scan_actually_sees_the_print_calls():
    """正面對照組：違規數本來就該是 0，拿 0 當證據等於沒有證據。

    數的是**掃到的列印呼叫總數**。2026-09-12 量到 1433 個，下限給 600。
    `_is_console_write` 一旦認不出東西（例如有人把 `_PRINTER_NAMES` 改名），
    違規數會永遠是 0 而且看起來完全正常。
    """
    seen = 0
    for path in _project_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_console_write(node):
                seen += 1
    assert seen >= 600, (
        f"只認出 {seen} 個列印呼叫（2026-09-12 量到 1433 個）。"
        "認不出東西的掃描器，違規數永遠是 0。")


_CONSOLE_CASES = {
    # 必抓
    "print 裡的警告表情符號": (
        'print("⚠️ 有三列沒改成功")\n', True),
    "_progress 裡的約等於": (
        '_progress(f"大小 ≈ {n} MB")\n', True),
    "sys.stderr.write": (
        'import sys\nsys.stderr.write("⚠️ 壞了\\n")\n', True),
    "f-string 的字面部分": (
        'print(f"{count} 列沒改成功 ⚠️")\n', True),
    # 必放行——**這幾筆才是重點**，判準裡的每一道縮限都只有近似案例殺得死。
    "print 裡的繁體中文": (
        'print("佇列已清空，行程結束")\n', False),
    "print 裡的全形箭頭與乘除": (
        'print("A → B，長度 × 2 ÷ 3")\n', False),
    # 送到聊天平台的字串滿滿都是表情符號，那條路是 UTF-8。少了這一筆，
    # 把範圍從「列印呼叫」放大成「所有字串常數」也不會有人發現。
    "送到聊天平台的表情符號": (
        'async def f(ch):\n    await ch.send("✅ 完成了")\n', False),
    "一般變數裡的表情符號": (
        'LABEL = "⚠️ 警告"\n', False),
    # 註解與 docstring 不是印出去的東西。這一段規則的說明文字本身就會用到 ⚠️。
    "docstring 裡的表情符號": (
        'def f():\n    """⚠️ 這裡只是說明。"""\n    return 1\n', False),
    # 插值進來的是執行期資料，靜態管不到；把它算進來只會開始亂叫。
    "插值變數（靜態看不到內容）": (
        'def f(x):\n    print(f"值是 {x}")\n', False),
}


@pytest.mark.parametrize("label", sorted(_CONSOLE_CASES))
def test_the_console_encodability_scan_tells_the_shapes_apart(label):
    """合成語料：該抓的抓到、不該抓的放行。"""
    source, should_catch = _CONSOLE_CASES[label]
    got = _console_unencodable(source, f"{label}.py")
    if should_catch:
        assert got, f"「{label}」應該被抓到，卻放行了"
    else:
        assert not got, f"「{label}」不該被抓，卻報了：{got}"


def test_the_replacement_characters_are_actually_encodable():
    """失敗訊息建議的替代字元必須真的編得出來。

    一個建議了**同樣編不出來**的替代字元的錯誤訊息，比沒有建議還糟：讀的人會照做、
    測試會繼續紅，然後開始懷疑守門本身壞了。（本專案 2026-09-12 量過一輪：
    `※`、`≒`、`→`、`×`、`÷` 都在 cp950 裡，`⚠`、`≈`、`⏎` 都不在。）
    """
    for ch in ("※", "≒", "→", "×", "÷"):
        assert _encodable(ch), f"建議的替代字元 {ch!r} 自己就編不出來"
    for ch in ("⚠", "≈", "⏎"):
        assert not _encodable(ch), (
            f"{ch!r} 現在編得出來了——這台機器的代碼頁變了？"
            "整段規則的前提要重新量。")


# ---------------------------------------------------------------------------
# 為 stdout 組出來、但由**別人**去印的文字
# ---------------------------------------------------------------------------
#
# 上面那支守門問的是「`print(...)` 的引數裡有沒有編不出來的字面值」。它結構上
# 看不到的形狀是：字面值先被組成一段文字**回傳**，由呼叫端去印。
# `mutation_harness.Result.report()` 就是這個形狀，而且它三層都躲掉了：
#
#   1. 它住在一個**沒有 `__main__`** 的模組裡，所以「這是不是 CLI 工具」那種
#      以模組為單位的判準看不到它；
#   2. 它唯一真正會 `print()` 它的呼叫端**在 repo 外面**——本專案的硬規則是
#      一次性的驅動／探測腳本不要放進樹裡（放進去會被 `rglob` 掃到），所以
#      「誰印了它」這個問題在樹內永遠查無此人；
#   3. 樹內唯一的 `print(result.report())` 寫在模組 docstring 的用法範例裡，
#      那是字串，不是呼叫。
#
# 量到的東西（2026-09-12）：`report()` 裡有三個 cp950 編不出來的字元
# （`⛔` 一個、`⚠️` 兩個），而它們分別在 NO-EVIDENCE／INCOMPETENT／逾時三條
# 分支上——也就是「這一輪的分數不可用，重跑」那幾句。平常跑不到，一旦跑到就是
# 行程死在 `print()`，連前面已經算完的判決一起賠掉。與本專案 §8.53 記過的那筆
# 同一個形狀：**會警告的那一行，正好是會爆的那一行。**
#
# 這裡用兩道，因為兩道各自補對方的洞：
#   * `_TEXT_FOR_STDOUT` 是**指名**的（同 CLAUDE.md 對 `stream_child` /
#     `run_full` 的處置：掃描器結構上看不到的，就指名一個守門），附理由，
#     並且反查那個名字還在不在——改名之後豁免會繼續生效而沒有症狀。
#   * 底下那道是**推導**的：非 bot 模組裡「回傳 join 出來的文字」的函式。
#     2026-09-12 量到 31 支、違規 0（唯一有表情符號的是
#     `discord_bot._dorossi_render_session_list`，那是送到聊天平台的，走 UTF-8，
#     本來就不在範圍內）。它免費，而且下一支 `report()` 兄弟不必有人記得加名單。

_TEXT_FOR_STDOUT = {
    # 模組 -> {函式名: 理由}
    "mutation_harness.py": {
        "report": (
            "變異骨架的公開輸出面。呼叫端一律是 repo 外的驅動腳本"
            "（本專案不把一次性腳本放進樹裡），所以樹內沒有任何 "
            "`print(result.report())` 可以被掃到。"),
    },
}

_STDOUT_TEXT_EXEMPT_MODULES = {
    # 送到聊天平台的字串走 UTF-8，表情符號滿天飛，那是**刻意**在範圍外的。
    # 這一條與上面 `_CONSOLE_CASES` 裡「送到聊天平台的表情符號」同一個判準。
    "discord_bot.py",
    # 產生 `.md`，用 `encoding="utf-8"` 寫檔，不是印到主控台。
    "gen_command_docs.py",
}


def _non_docstring_literals(fn: ast.AST) -> list[ast.Constant]:
    """函式裡所有不是它自己 docstring 的字串常數。

    docstring 與註解不會被印出去，而解釋這條規則的說明文字本身就會用到 `⚠️`
    ——不排掉的話這支守門會被自己的說明打紅。
    """
    body = getattr(fn, "body", None)
    doc = None
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        doc = body[0].value
    return [n for n in ast.walk(fn)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n is not doc]


def _returns_joined_text(fn: ast.AST) -> bool:
    """這支函式會不會 `return "...".join(...)`？

    「組多行文字回傳給別人印」的機械式代理判準。不完美（用字串相加組出來的
    就看不到），但它是**推導**的，所以新寫的診斷函式不必有人記得登記。
    """
    for node in ast.walk(fn):
        if (isinstance(node, ast.Return) and node.value is not None
                and isinstance(node.value, ast.Call)
                and ".join" in ast.unparse(node.value.func)):
            return True
    return False


def _stdout_text_functions() -> list[tuple[Path, ast.AST]]:
    """要檢查的函式：指名的 ∪ 推導出來的（扣掉豁免模組）。"""
    out: list[tuple[Path, ast.AST]] = []
    for path in _project_sources():
        named = _TEXT_FOR_STDOUT.get(path.name, {})
        if path.name in _STDOUT_TEXT_EXEMPT_MODULES and not named:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), path.name)
        except SyntaxError:      # pragma: no cover - 樹裡不該有這種檔
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in named or _returns_joined_text(node):
                out.append((path, node))
    return out


def _stdout_text_problems(source: str, filename: str) -> list[str]:
    """這份原始碼裡，組給 stdout 的文字有哪些編不出來的字元。

    **偵測邏輯刻意跟測試本體分開**，理由是本專案記過的那條：樹目前是乾淨的，
    所以「組訊息」那幾行在真資料上一次都不會執行——把 `problems.append(...)`
    整個拿掉、或把掃描範圍縮到只看第一個字，五支測試依然全綠（2026-09-12
    變異測試量到 SURVIVED）。抽成函式之後，底下的合成語料會**真的**走過這條路，
    那兩個變異才殺得死。`_console_unencodable` 早就是這個形狀，這裡跟上。
    """
    problems: list[str] = []
    named = _TEXT_FOR_STDOUT.get(filename, {})
    if filename in _STDOUT_TEXT_EXEMPT_MODULES and not named:
        return problems
    for fn in ast.walk(ast.parse(source, filename)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (fn.name in named or _returns_joined_text(fn)):
            continue
        for node in _non_docstring_literals(fn):
            offenders = sorted({ch for ch in node.value if not _encodable(ch)})
            if not offenders:
                continue
            codes = " ".join(f"U+{ord(c):04X}" for c in offenders)
            problems.append(
                f"{filename}:{node.lineno} 在 {fn.name}() 裡組給 stdout 的"
                f"文字有 {_CONSOLE_CODEC} 編不出來的字元："
                f"{''.join(offenders)!r}（{codes}）")
    return problems


def test_text_built_for_stdout_is_encodable_even_when_its_printer_is_elsewhere():
    """組給 stdout 的文字，編不出來一樣會讓行程死掉——即使 `print` 在別的檔案。

    `print(...)` 那道守門只看得到寫在呼叫引數裡的字面值。這一支補的是
    「字面值在 A、`print` 在 B」那半，見本段最上面的說明。
    """
    problems: list[str] = []
    for path in _project_sources():
        problems += _stdout_text_problems(
            path.read_text(encoding="utf-8"), path.name)
    assert not problems, (
        "\n".join(problems)
        + "\n\n這段文字是**回傳給呼叫端去印**的，所以上面那道以 `print(...)` "
        "引數為準的掃描看不到它；但它最後還是會走到 `print()`，而管線上那一步"
        f"用地區編碼 {_CONSOLE_CODEC}，編不出來就是行程死掉。"
        "\n換成編得出來的寫法：`⚠️`→`※`(U+203B)、`≈`→`≒`(U+2252)、"
        "`⛔`→`■`(U+25A0)。")


def test_the_named_stdout_text_surfaces_still_exist():
    """指名的豁免／登記必須還指得到東西。

    改名之後那筆登記會變成一個永遠對不上的字串，守門照跑、測試全綠、
    那個輸出面卻再也沒被檢查過——本專案對 `_OWNER_ONLY_SLASH` 記的是同一條。
    """
    for module, entries in _TEXT_FOR_STDOUT.items():
        path = REPO_ROOT / "axiomatic" / module
        assert path.exists(), f"`{module}` 不在了，登記該一起改"
        tree = ast.parse(path.read_text(encoding="utf-8"), module)
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name, reason in entries.items():
            assert name in defined, (
                f"`{module}` 裡沒有 `{name}()` 了——登記的理由是「{reason}」，"
                "函式改名的話這一筆要跟著改，否則它再也擋不到任何東西。")
            assert reason.strip(), f"`{module}::{name}` 沒有寫理由"


def test_the_stdout_text_scan_actually_selects_functions():
    """正面對照組：選不到東西的掃描器，違規數永遠是 0。

    數的是**選到的函式數**。2026-09-12 量到 31 支（推導 30 ＋ 指名 1），
    下限給 12。`_returns_joined_text` 一旦認不出東西，上面那支會永遠綠。
    """
    picked = _stdout_text_functions()
    assert len(picked) >= 12, (
        f"只選到 {len(picked)} 支函式（2026-09-12 量到 31 支）。"
        "選不到東西的掃描器，違規數永遠是 0。")
    names = {fn.name for _p, fn in picked}
    assert "report" in names, (
        "指名的 `mutation_harness.report` 沒有被選進來——"
        "`_TEXT_FOR_STDOUT` 這條路斷了。")


def test_the_named_registration_selects_on_its_own(monkeypatch):
    """指名那條路要**自己**選得到東西，不能靠推導判準順便撈到。

    2026-09-12 變異測試量到的：把 `node.name in named` 整條拿掉，四支測試**全綠**。
    原因是 `mutation_harness.report()` 剛好兩條判準都符合——它自己就是
    `return "\\n".join(lines)`——所以推導那半把指名那半整個遮住了，而一份沒有作用
    的登記清單讀起來像這件事有人在管（同 `_ALLOWED_COMMAND_NAME_WORDS` 那筆的
    形狀）。本專案的判語是：兩道守門會互相遮蔽時，要造一個**只**踩到其中一條的
    輸入。這裡把推導判準關掉再選一次，剩下的就只有指名那條路。

    指名清單不能因為「目前是多餘的」就刪掉：`_returns_joined_text` 是機械式代理，
    用字串相加、或先存進屬性再回傳的診斷函式它都看不到，那時候就只剩這條路。
    """
    monkeypatch.setitem(globals(), "_returns_joined_text", lambda _fn: False)
    picked = {fn.name for _p, fn in _stdout_text_functions()}
    assert "report" in picked, (
        "把推導判準關掉之後就選不到 `report()` 了——代表 `_TEXT_FOR_STDOUT` "
        "這份指名清單目前一點作用都沒有，而它的用處正是接住推導判準看不到的"
        "形狀（字串相加組出來的診斷文字）。")
    assert len(picked) <= 4, (
        f"關掉推導判準之後還選到 {len(picked)} 支——那代表推導那半根本沒被關掉，"
        "這支測試就沒有隔離到指名那條路。")


def test_the_stdout_text_scan_does_not_read_docstrings_or_the_chat_surface():
    """兩道縮限各有一個近似案例，因為放寬的那一步只有必放行案例殺得死。

    第一，docstring 要排掉：解釋這條規則的文字本身就會寫 `⚠️`，不排掉的話
    這支守門會被自己的說明打紅，而「修好」它的最省力做法就是把它關掉。
    第二，聊天平台那一面要排掉：那條路是 UTF-8，`discord_bot.py` 裡有幾百個
    表情符號，把它算進來等於讓這支守門一出生就是紅的。
    """
    tree = ast.parse(
        'def f():\n'
        '    """說明裡有 \u26a0\ufe0f 這個字。"""\n'
        '    return "\\n".join(["\u7d50\u675f"])\n', "synthetic.py")
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    assert not [n for n in _non_docstring_literals(fn)
                if any(not _encodable(ch) for ch in n.value)], (
        "docstring 被算進去了——這支守門會被自己的說明文字打紅。")

    assert "discord_bot.py" in _STDOUT_TEXT_EXEMPT_MODULES, (
        "聊天平台那一面不在豁免裡，這支守門會因為幾百個表情符號永遠是紅的。")
    picked = {path.name for path, _fn in _stdout_text_functions()}
    assert "discord_bot.py" not in picked, (
        "豁免沒有生效——`discord_bot.py` 的字串走 UTF-8 送到聊天平台，"
        "不受主控台代碼頁限制。")


_STDOUT_TEXT_CASES = {
    # 必抓
    "回傳 join 出來的警告字": (
        'def report():\n    return "\\n".join(["\u26a0\ufe0f \u58de\u4e86"])\n',
        "tool.py", True),
    # **這一筆專門殺「只看前幾個字」那種範圍縮水的變異**：第一個字編得出來，
    # 不可編碼的那個在最後面。只拿第一個字去判的實作會放它過去。
    "不可編碼的字在字串最後面": (
        'def report():\n'
        '    return "\\n".join(["\u9032\u5ea6 100% \u2248"])\n',
        "tool.py", True),
    "指名登記的函式，即使不回傳 join": (
        'def report():\n    out = "\u26a0\ufe0f \u58de\u4e86"\n    return out\n',
        "mutation_harness.py", True),
    # 必放行——**這幾筆才是重點**，判準裡的每一道縮限都只有近似案例殺得死。
    "回傳 join 的繁體中文": (
        'def report():\n'
        '    return "\\n".join(["\u4f47\u5217\u5df2\u6e05\u7a7a\uff0c'
        '\u884c\u7a0b\u7d50\u675f"])\n',
        "tool.py", False),
    "docstring 裡的警告字": (
        'def report():\n'
        '    """\u26a0\ufe0f \u9019\u88e1\u53ea\u662f\u8aaa\u660e\u3002"""\n'
        '    return "\\n".join(["\u597d"])\n',
        "tool.py", False),
    # 不組多行文字、也沒登記的函式不在範圍內。把範圍放大到「所有函式」的話，
    # 這一筆會開始亂叫，而會亂叫的守門就是會被關掉的守門。
    "沒登記也不回傳 join 的一般函式": (
        'def helper():\n    return "\u26a0\ufe0f \u58de\u4e86"\n',
        "tool.py", False),
    # 聊天平台那一面走 UTF-8，表情符號是**刻意**在範圍外的。
    "聊天平台那一面": (
        'def report():\n    return "\\n".join(["\u26a0\ufe0f"])\n',
        "discord_bot.py", False),
}


@pytest.mark.parametrize("label", sorted(_STDOUT_TEXT_CASES))
def test_the_stdout_text_scan_tells_the_shapes_apart(label):
    """合成語料：該抓的抓到、不該抓的放行。

    這一支同時是上面那個偵測函式的**唯一**執行證據——真資料目前全乾淨，
    所以組訊息那幾行在真資料上一次都跑不到。
    """
    source, filename, should_catch = _STDOUT_TEXT_CASES[label]
    got = _stdout_text_problems(source, filename)
    if should_catch:
        assert got, f"「{label}」應該被抓到，卻放行了"
    else:
        assert not got, f"「{label}」不該被抓，卻報了：{got}"


# ------------------------------------- 文字檔裡不該有的控制字元（2026-09-22） ----
# 在 Bash 的 heredoc 裡寫路徑，反斜線會被吃掉一層：`\f` 變成換頁字元、`\r` 變成單獨一個 CR。
# 這不只是顯示錯——`str.splitlines()` 會在這兩個字元那裡斷行，而佇列讀取用的就是它：一個這樣寫進
# 檔案的路徑會安靜地被讀成兩筆，沒有任何測試會紅。所有被追蹤的文字檔一個這種字元都沒有，所以這道
# 門不會喊狼來了。資料檔（佇列、提示詞、憑證）是使用者打的內容，不在範圍內。
_STRAY_CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\r(?!\n)")
_TEXT_SUFFIXES = (".py", ".md", ".txt", ".json", ".ini", ".toml", ".yml", ".yaml",
                  ".cfg", ".rst", ".bat", ".ps1")
# 2026-09-22 量到 212 個；下限是為了「清單抽不到東西」時不會讀成「全部乾淨」。
_TRACKED_TEXT_FLOOR = 150


def _stray_controls(raw: bytes) -> list[str]:
    """`raw` 裡每一個不該出現在文字檔裡的控制字元，回 `行號: 字元`。Tab、LF、CRLF 不算。"""
    hits = []
    for match in _STRAY_CONTROL.finditer(raw):
        line = raw.count(b"\n", 0, match.start()) + 1
        hits.append(f"{line}: {match.group()!r}")
    return hits


def _tracked_text_files() -> list[str] | None:
    """`git ls-files` 裡的文字檔（repo 相對），資料檔除外；讀不到 git 回 None。"""
    try:
        done = subprocess.run(["git", "-c", "core.quotepath=off", "ls-files"], cwd=REPO_ROOT,
                              capture_output=True, encoding="utf-8", errors="replace", check=False)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    return [name for name in done.stdout.splitlines()
            if name.lower().endswith(_TEXT_SUFFIXES) and Path(name).name not in _DATA_MARKDOWN]


def test_no_tracked_text_file_carries_a_stray_control_character():
    names = _tracked_text_files()
    if names is None:
        pytest.skip("讀不到 `git ls-files`（沒裝 git／不是 repo）")
    assert len(names) >= _TRACKED_TEXT_FLOOR, f"只掃到 {len(names)} 個文字檔，清單大概抽壞了"
    problems = []
    for name in names:
        path = REPO_ROOT / name
        if path.is_file():
            problems += [f"{name}:{hit}" for hit in _stray_controls(path.read_bytes())]
    assert not problems, (
        "這些檔案裡有 Tab／換行以外的控制字元：\n  " + "\n  ".join(problems)
        + "\n多半是在 heredoc 或 `sed` 裡寫了反斜線（`\\f`、`\\r`、`\\b`…）被吃掉一層。"
        "改用寫檔工具或 Python 腳本重寫那一段。")


@pytest.mark.parametrize("raw, caught", [
    (b"a\x0cb", True),            # 換頁：2026-09-22 真的發生過的那一個
    (b"a\rb\r\n", True),         # 單獨的 CR（後面沒接 LF）
    (b"a\x0bb", True),            # 垂直定位
    (b"a\x00b", True),
    (b"a\x1b[31mb", True),        # ANSI 跳脫
    (b"a\x7fb", True),
    (b"a\tb\r\nc\nd", False),   # Tab、CRLF、LF 都是正常的
    ("繁體中文".encode("utf-8"), False),  # 多位元組 UTF-8 不含 C0 位元組
], ids=["form-feed", "lone-cr", "vertical-tab", "nul", "escape", "del", "tab-crlf-lf", "cjk"])
def test_the_stray_control_scan_tells_the_shapes_apart(raw, caught):
    """合成語料：真資料目前全乾淨，組訊息那幾行只在這裡跑得到。"""
    assert bool(_stray_controls(raw)) is caught, (raw, _stray_controls(raw))
