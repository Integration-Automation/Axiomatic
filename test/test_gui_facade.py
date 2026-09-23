"""門面契約守門：`_gui_control` 轉呼叫的每個套件符號都要真的存在、也吃得下呼
叫端給的參數。

**為什麼需要這支測試。** 桌面自動化的實作全部在 `je_auto_control`，
`_gui_control.py` 只剩參數解析、環境政策與錯誤訊息去識別化——也就是說它有三十
幾處直接呼叫套件符號，其中辨識與 UI 元素那兩批還是 `getattr(ac, name)` 動態查
找。套件在本機是**可編輯安裝**（改原始碼立刻生效），所以：

- 套件端改個函式名或改掉某個關鍵字參數，本專案這邊**不會有任何徵兆**——
  `import discord_bot` 照樣過（DoD #1），`test_gui_control.py` 用的是假後端也
  照樣綠，只有使用者真的下指令的那一刻才炸；
- 動態查找的那五個名字連 linter 都看不到。

所以這裡不測行為，只測**契約**：把 `_gui_control.py` 用 AST 掃一遍，抽出它實際
用到的每個套件符號與每個呼叫點的參數，再拿去跟**已安裝的**套件比對。

掃描而不是手寫清單，是為了不再多一份會 drift 的東西：新增一處轉呼叫就自動納入
守備範圍，不需要有人記得回來補這張表。

套件沒安裝時整支 skip（沒有東西可以比對）；裝了就一定要一致。
"""
import ast
import inspect
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

GUI_SOURCE = Path(__file__).resolve().parent.parent / "axiomatic" / "_gui_control.py"

try:
    import je_auto_control as AC  # type: ignore
except Exception as _error:  # pylint: disable=broad-except
    AC = None
    _IMPORT_ERROR = repr(_error)
else:
    _IMPORT_ERROR = ""

pytestmark = pytest.mark.skipif(
    AC is None,
    reason=f"je_auto_control 未安裝或無法載入，沒有東西可以比對：{_IMPORT_ERROR}",
)

# `getattr(ac, name)` 這種動態查找的 helper：第一個位置參數是**符號名**，其餘才
# 是真正轉送過去的參數。
_DYNAMIC_HELPERS = ("_ocr_call", "_ui_call")

# 例外型別是用**名字字串**比對的（`type(error).__name__ == …`），刻意不 import
# ——為了一個例外型別把辨識後端在 import 時就拉進來並不划算。代價是套件端改名
# 之後這裡會靜默退化成泛用訊息（「引擎沒裝」會被講成「辨識失敗」），所以名字本
# 身也要守。
_MATCHED_EXCEPTION_NAMES = (
    "OCRBackendNotAvailableError",
    "AutoControlFlatTemplateException",
    "AccessibilityNotAvailableError",
)


class _CallSite:
    """一處轉呼叫：`load_ac().foo(a, b, key=1)` 拆成可以比對簽章的形狀。"""

    def __init__(self, base: str, attr: str, line: int,
                 positional: int, keywords: set[str], starred: bool):
        self.base = base            # "load_ac" 或 "_window_api"
        self.attr = attr            # 套件裡的符號名
        self.line = line
        self.positional = positional
        self.keywords = keywords
        self.starred = starred      # 有 `*args` / `**kwargs` 轉送就放寬檢查

    def __repr__(self) -> str:  # pytest 參數化的顯示名
        return f"{self.attr}@L{self.line}"


def _parse_gui_control() -> ast.Module:
    return ast.parse(GUI_SOURCE.read_text(encoding="utf-8"), str(GUI_SOURCE))


def _alias_map(tree: ast.Module) -> dict[str, str]:
    """找出 `ac = load_ac()` / `win = _window_api()` 這類區域別名。"""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id in ("load_ac", "_window_api"):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = value.func.id
    return aliases


def _base_of(value: ast.expr, aliases: dict[str, str]) -> str | None:
    """判斷 `X.attr` 裡的 `X` 是不是套件（直接呼叫或別名）。"""
    if isinstance(value, ast.Name) and value.id in aliases:
        return aliases[value.id]
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
            and value.func.id in ("load_ac", "_window_api"):
        return value.func.id
    return None


def _collect_call_sites() -> list[_CallSite]:
    tree = _parse_gui_control()
    aliases = _alias_map(tree)
    sites: list[_CallSite] = []
    for node in ast.walk(tree):
        # 1. 一般轉呼叫：load_ac().foo(...) / ac.foo(...) / win.foo(...)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = _base_of(node.func.value, aliases)
            if base:
                sites.append(_CallSite(
                    base, node.func.attr, node.lineno,
                    positional=len(node.args),
                    keywords={k.arg for k in node.keywords if k.arg},
                    starred=any(isinstance(a, ast.Starred) for a in node.args)
                    or any(k.arg is None for k in node.keywords),
                ))
        # 2. 動態查找的 helper：_ocr_call("find_text_matches", …)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _DYNAMIC_HELPERS:
            if node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                sites.append(_CallSite(
                    "load_ac", node.args[0].value, node.lineno,
                    positional=len(node.args) - 1,
                    keywords={k.arg for k in node.keywords if k.arg},
                    starred=any(isinstance(a, ast.Starred)
                                for a in node.args[1:])
                    or any(k.arg is None for k in node.keywords),
                ))
    return sites


def _collect_attribute_reads() -> set[tuple[str, str]]:
    """`getattr(load_ac(), "keyboard_keys_table", None)` 這種只讀不呼叫的。"""
    tree = _parse_gui_control()
    aliases = _alias_map(tree)
    reads: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr" and len(node.args) >= 2:
            base = _base_of(node.args[0], aliases)
            name = node.args[1]
            if base and isinstance(name, ast.Constant) \
                    and isinstance(name.value, str):
                reads.add((base, name.value))
        if isinstance(node, ast.Attribute):
            base = _base_of(node.value, aliases)
            if base:
                reads.add((base, node.attr))
    return reads


def _collect_submodule_imports() -> dict[str, set[str]]:
    """`from je_auto_control.utils.monitor_layout import …` 的模組與名字。"""
    tree = _parse_gui_control()
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module \
                and node.module.startswith("je_auto_control"):
            found.setdefault(node.module, set()).update(
                alias.name for alias in node.names)
    return found


def _resolve(base: str):
    """`load_ac` → 套件門面；`_window_api` → 視窗操作子模組。"""
    if base == "load_ac":
        return AC
    from je_auto_control.wrapper import auto_control_window  # type: ignore
    return auto_control_window


CALL_SITES = _collect_call_sites() if AC is not None else []
ATTRIBUTE_READS = _collect_attribute_reads() if AC is not None else set()
# 動態查找的名字（`_ocr_call` / `_ui_call` 的第一個引數）在原始碼裡是字串，不是
# 屬性存取，所以上面那支掃不到；補進來讓它們也吃到「符號存在」那條的說明訊息。
ATTRIBUTE_READS |= {(site.base, site.attr) for site in CALL_SITES}
SUBMODULE_IMPORTS = _collect_submodule_imports() if AC is not None else {}


# --------------------------------------------------------------------------
# 掃描本身要先站得住腳：抓不到東西的話，上面每一個測試都會「全過」而什麼都沒守
# --------------------------------------------------------------------------
def test_scanner_actually_found_the_call_sites():
    # 數字放寬（新增轉呼叫是常態），但為 0 或個位數就代表 AST 掃描壞了。
    assert len(CALL_SITES) >= 30, (
        f"只掃到 {len(CALL_SITES)} 處轉呼叫，_gui_control.py 的寫法可能變了"
        "（例如換成別的載入方式），掃描邏輯要跟著改，否則這支測試等於沒作用。")
    assert any(site.base == "_window_api" for site in CALL_SITES)
    assert {"find_text_matches", "find_accessibility_elements"} <= {
        site.attr for site in CALL_SITES}, "動態查找的名字沒被掃到"


# --------------------------------------------------------------------------
# 符號存在
# --------------------------------------------------------------------------
@pytest.mark.parametrize("base,attr", sorted(ATTRIBUTE_READS))
def test_symbol_exists_in_installed_package(base, attr):
    target = _resolve(base)
    assert hasattr(target, attr), (
        f"`_gui_control` 用到 `{attr}`，但已安裝的套件裡沒有這個符號。"
        "套件端改名的話，這邊的轉呼叫要一起改（不要在本專案重寫一份實作）。")


# --------------------------------------------------------------------------
# 簽章：關鍵字吃得下、必填參數給得齊
# --------------------------------------------------------------------------
def _signature_or_none(func):
    if not callable(func):
        return None
    try:
        return inspect.signature(func)
    except (TypeError, ValueError):  # C 實作 / 沒有簽章資訊
        return None


@pytest.mark.parametrize("site", CALL_SITES, ids=repr)
def test_call_site_matches_installed_signature(site: _CallSite):
    target = _resolve(site.base)
    func = getattr(target, site.attr, None)
    assert func is not None, f"套件裡沒有 `{site.attr}`"
    signature = _signature_or_none(func)
    if signature is None:
        pytest.skip(f"`{site.attr}` 取不到簽章")

    kinds = inspect.Parameter
    params = list(signature.parameters.values())
    has_var_positional = any(p.kind is kinds.VAR_POSITIONAL for p in params)
    has_var_keyword = any(p.kind is kinds.VAR_KEYWORD for p in params)
    positional_slots = [p for p in params
                        if p.kind in (kinds.POSITIONAL_ONLY,
                                      kinds.POSITIONAL_OR_KEYWORD)]
    keyword_slots = {p.name for p in params
                     if p.kind in (kinds.POSITIONAL_OR_KEYWORD,
                                   kinds.KEYWORD_ONLY)}

    unknown = sorted(site.keywords - keyword_slots)
    assert not unknown or has_var_keyword, (
        f"`{site.attr}` 已經不吃 {unknown} 這些關鍵字參數了"
        f"（`_gui_control.py:{site.line}` 還在傳）。")

    if not site.starred:
        assert site.positional <= len(positional_slots) or has_var_positional, (
            f"`{site.attr}` 只收得下 {len(positional_slots)} 個位置參數，"
            f"`_gui_control.py:{site.line}` 給了 {site.positional} 個。")

        for index, param in enumerate(positional_slots):
            if param.default is not inspect.Parameter.empty:
                continue
            given = index < site.positional or param.name in site.keywords
            assert given, (
                f"`{site.attr}` 的必填參數 `{param.name}` 沒有被 "
                f"`_gui_control.py:{site.line}` 提供"
                "（套件端新增必填參數時會踩到這條）。")

        for param in params:
            if param.kind is not kinds.KEYWORD_ONLY:
                continue
            if param.default is not inspect.Parameter.empty:
                continue
            assert param.name in site.keywords, (
                f"`{site.attr}` 的必填關鍵字參數 `{param.name}` 沒有被 "
                f"`_gui_control.py:{site.line}` 提供。")


# --------------------------------------------------------------------------
# 子模組 import
# --------------------------------------------------------------------------
def test_submodule_imports_resolve():
    import importlib

    assert SUBMODULE_IMPORTS, "沒掃到任何 je_auto_control 子模組 import"
    for module_name, names in sorted(SUBMODULE_IMPORTS.items()):
        module = importlib.import_module(module_name)
        for name in sorted(names):
            assert hasattr(module, name), (
                f"`{module_name}` 裡沒有 `{name}`——`_gui_control.py` 直接 import "
                "子模組是為了避開門面的 import 成本，所以門面有沒有它不算數。")


# --------------------------------------------------------------------------
# 反向守門：平行實作不得長回來
# --------------------------------------------------------------------------
# 這一段不需要套件，所以刻意不受檔頭的 skipif 影響——它讀的是本專案的原始碼。
_NO_PARALLEL_IMPLEMENTATION = {
    # 模組（None = 整個 axiomatic/）→ 不得出現的 import
    None: ("win32gui", "win32con", "win32api", "win32process", "win32clipboard"),
    "_gui_control.py": ("cv2", "comtypes"),
}


def _repo_sources() -> list[Path]:
    # 套件 ＋ `test/`（本檔所在的目錄）：`conftest.py` 與手動 e2e 腳本在 2026-09-22
    # 之前住在套件裡、在範圍內，搬家之後照舊（呼叫端自己濾掉 `test_*`）。
    return (sorted(GUI_SOURCE.parent.glob("*.py"))
            + sorted(Path(__file__).resolve().parent.glob("*.py")))


@pytest.mark.parametrize("banned", _NO_PARALLEL_IMPLEMENTATION[None])
def test_no_module_reaches_for_win32_directly(banned):
    """視窗、輸入、剪貼簿的 Win32 呼叫全部在函式庫，這裡一個都不留。

    2026-08-17 之前本專案有四處 `win32gui`（presence 三處、兩支 webrunner 各一處
    的最小化），全部收回單一來源後 `pywin32` 連相依都拿掉了。這條擋的是「趕時間
    就直接 `import win32gui`」——復發成本很低，而復發之後又會回到「同一件事兩份
    實作，修好的永遠只有其中一份」。要用 Win32 的新東西，補進函式庫。
    """
    offenders = []
    for path in _repo_sources():
        if path.name.startswith("test_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] == banned for name in names):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"這些地方直接 import 了 `{banned}`：{offenders}。"
        "Win32 的實作只能有一份，在桌面自動化函式庫裡。")


def test_gui_control_does_not_import_the_automation_backends_directly():
    """這條一直是判準，但沒有東西在檢查。

    `_gui_control` 一旦自己 `import cv2` / `import comtypes`，就代表又開始在本
    專案重寫比對或走訪邏輯了——那正是 2026-08-15 收乾淨之前的狀態。
    """
    tree = ast.parse(GUI_SOURCE.read_text(encoding="utf-8"), str(GUI_SOURCE))
    banned = set(_NO_PARALLEL_IMPLEMENTATION["_gui_control.py"])
    offenders = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        hit = {name.split(".")[0] for name in names} & banned
        if hit:
            offenders.append(f"L{node.lineno}: {sorted(hit)}")
    assert not offenders, (
        f"`_gui_control.py` 直接 import 了自動化後端：{offenders}。"
        "影像比對與 UI 走訪都在函式庫，本模組只做參數解析、環境政策與去識別化。")


# --------------------------------------------------------------------------
# 用字串比對的例外型別
# --------------------------------------------------------------------------
def test_exception_names_matched_by_string_still_exist():
    package_root = Path(AC.__file__).resolve().parent
    sources = list(package_root.rglob("*.py"))
    assert len(sources) > 50, "套件原始碼掃描路徑不對"
    blob = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore") for path in sources)
    for name in _MATCHED_EXCEPTION_NAMES:
        assert re.search(rf"^class {re.escape(name)}\b", blob, re.MULTILINE), (
            f"套件裡已經沒有 `{name}` 這個例外型別了。`_gui_control` 是用"
            "**型別名字**比對來分辨『引擎沒裝』與『辨識失敗』的，名字一改就會"
            "靜默退化成泛用訊息——使用者會被告知『失敗』而不是『去裝引擎』。")


# ---------------------------------------------------------------------------
# 型別名字底下還有第二層：用**訊息子字串**分辨的那一層（2026-09-21）
# ---------------------------------------------------------------------------
#
# 上面那支守的是 `type(error).__name__ == "..."`。`_ui_call` 在那個判斷**裡面**
# 還有第二層：
#
#     if type(error).__name__ == "AccessibilityNotAvailableError":
#         if "window title" in str(error):
#             raise GuiError("找不到符合的視窗。") from error
#         raise GuiError("UI 元素定位功能無法在此環境使用。") from error
#
# 第一層有守門、第二層沒有——本 repo 同一週已經記過兩次這個形狀（DoD #3 的兩個
# 半句、§Git Commits 的五份語料）。而這一層的退化**比第一層更糟**：型別改名只是
# 把話講得比較泛，訊息改字卻會把「找不到符合的視窗」（使用者改得了：標題打錯）
# 換成「UI 元素定位功能無法在此環境使用」（死路：這台機器不支援）。使用者會去查
# 一個不存在的環境問題。
#
# 它也比型別名字更容易壞：例外**訊息**是任何函式庫裡最隨手會被改的字串，而這個
# 套件在本機是可編輯安裝，改一個字連發行流程都不必經過。
#
# 兩端都用抽的，不寫死：子字串從 `_gui_control` 自己的原始碼抽（所以我們這邊改了
# 也算數），raise 點從套件的 AST 抽。判準有兩個方向——**至少命中一個**（不然那條
# 分支是死的），**不能全部命中**（不然判別性是假的，每一種失敗都會被講成「找不到
# 視窗」）。2026-09-21 實測：1/7。
_MESSAGE_MATCHERS = {
    # `_gui_control` 的函式 → 它在哪個例外型別底下做訊息比對
    "_ui_call": "AccessibilityNotAvailableError",
}


def _literal_text(node: ast.AST) -> str:
    """常值／隱式串接／f-string 的**字面部分**併成一段可搜尋的文字。

    插值（`ast.FormattedValue`）刻意回空字串：那是執行期的值，不是套件作者寫下
    的措辭，拿它來比對子字串沒有意義。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(v) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    return ""


def _substrings_compared_in(func_name: str, tree: ast.Module) -> list[str]:
    """`func_name` 裡 `"<字面>" in str(error)` 的那些字面值。"""
    out: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == func_name):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Compare) and len(sub.ops) == 1
                    and isinstance(sub.ops[0], ast.In)):
                continue
            if not (isinstance(sub.left, ast.Constant)
                    and isinstance(sub.left.value, str)):
                continue
            right = sub.comparators[0]
            if (isinstance(right, ast.Call)
                    and isinstance(right.func, ast.Name)
                    and right.func.id == "str"):
                out.append(sub.left.value)
    return out


def _raise_messages(exc_name: str, roots) -> list[tuple[str, int, str]]:
    """套件裡 `raise <exc_name>(...)` 的第一個參數，回 `[(檔案, 行號, 字面)]`。"""
    rows: list[tuple[str, int, str]] = []
    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            try:
                tree = ast.parse(
                    path.read_text(encoding="utf-8", errors="ignore"), str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise) or node.exc is None:
                    continue
                call = node.exc
                if not isinstance(call, ast.Call) or not call.args:
                    continue
                func = call.func
                name = (func.id if isinstance(func, ast.Name)
                        else getattr(func, "attr", ""))
                if name == exc_name:
                    rows.append((path.name, node.lineno,
                                 _literal_text(call.args[0])))
    return rows


def _discrimination_problem(sub: str, rows: list, exc_name: str) -> str | None:
    """`sub` 對 `rows` 還有判別性嗎？有問題回一句話，沒問題回 `None`。

    **判斷本身抽成一支，是因為真實資料永遠只走得到一種結果**（實測 1/7），所以
    「全部命中」那一半在真樹上是永遠不會發生的事——寫在測試裡的話，把它整行刪掉
    也照樣全綠。抽出來之後就餵得進合成語料，兩種壞情況各殺一次。
    """
    hits = [row for row in rows if sub in row[2]]
    if not hits:
        return (f"套件裡已經沒有任何 `{exc_name}` 的訊息含有 {sub!r} 了。"
                "`_gui_control._ui_call` 就是靠這個子字串分辨「找不到那個視窗」與"
                "「這台機器沒有這個功能」——對不上之後，**標題打錯**會被回報成"
                "**環境不支援**，使用者會去查一個不存在的問題。")
    if len(hits) == len(rows):
        return (f"`{exc_name}` 的 {len(rows)} 個 raise 點**全部**含有 {sub!r}，"
                "這個子字串已經不具判別性了——每一種失敗都會被講成「找不到符合的"
                "視窗」。")
    return None


def test_the_message_matcher_scanner_finds_both_ends():
    """正面對照組：兩端都要真的抽到東西。

    任一端抽到空的，下面那支就會安靜地什麼都不檢查——空語料跟乾淨長得一模一樣。
    """
    tree = _parse_gui_control()
    package_root = Path(AC.__file__).resolve().parent
    for func, exc_name in _MESSAGE_MATCHERS.items():
        subs = _substrings_compared_in(func, tree)
        assert subs, (
            f"`{func}` 裡抽不到任何 `\"...\" in str(error)` 的字面值——"
            "抽取器壞了，或那段判斷改寫成別的形狀了（改寫了就把這裡一起更新）。")
        rows = _raise_messages(exc_name, [package_root])
        assert len(rows) >= 3, (
            f"套件裡只找到 {len(rows)} 個 `raise {exc_name}(...)`——"
            "掃描路徑或抽取器不對。")


def test_every_message_substring_still_discriminates():
    """兩個方向都要成立：至少命中一個，而且不能全部命中。

    只檢查「至少命中一個」的話，套件把每一則訊息都寫上那個字也會過，而那時每一種
    失敗都會被講成「找不到符合的視窗」——判別性沒了，症狀卻是零。
    """
    tree = _parse_gui_control()
    package_root = Path(AC.__file__).resolve().parent
    for func, exc_name in _MESSAGE_MATCHERS.items():
        rows = _raise_messages(exc_name, [package_root])
        for sub in _substrings_compared_in(func, tree):
            problem = _discrimination_problem(sub, rows, exc_name)
            assert problem is None, problem


def test_the_message_scanner_can_actually_tell_the_shapes_apart(tmp_path):
    """合成對照組：抽取器真的在讀 AST，而不是回一張寫死的表。

    真實資料永遠是 1/7，所以「命中數」那兩個斷言在真樹上只走得到一種結果；
    這裡把「一個都沒命中」與「全部命中」兩種壞情況各造一次。
    """
    (tmp_path / "lib.py").write_text(
        "class E(RuntimeError):\n    pass\n"
        "def a():\n    raise E('no visible window title contains x')\n"
        "def b():\n    raise E(f'UIAutomationCore.dll unavailable: {e!r}')\n"
        "def c():\n    raise E('comtypes is required' + ' for windows')\n",
        encoding="utf-8")
    rows = _raise_messages("E", [tmp_path])
    assert len(rows) == 3, rows
    assert [r for r in rows if "window title" in r[2]], "該命中的沒命中"
    assert len([r for r in rows if "window title" in r[2]]) < len(rows)
    # f-string 的插值部分不算字面：`{e!r}` 不得被當成訊息的一部分。
    assert not [r for r in rows if "e!r" in r[2]], rows
    # 三種寫法都要讀得出來，否則抽取器會漏掉整類訊息而看起來一切正常：
    # 純常值、f-string 的字面段、以及 `+` 串接。
    by_line = {r[1]: r[2] for r in rows}
    assert "no visible window title" in by_line[4], by_line
    assert "UIAutomationCore.dll" in by_line[6], by_line
    assert "for windows" in by_line[8], by_line
    # 我們這一端的抽取：只認 `"..." in str(error)`，不認別的 `in`。
    gui = ast.parse(
        "def _ui_call():\n"
        "    if 'window title' in str(error):\n        pass\n"
        "    if 'x' in {'x': 1}:\n        pass\n"
        "    if 'y' in some_list:\n        pass\n")
    assert _substrings_compared_in("_ui_call", gui) == ["window title"]


def test_the_discrimination_check_catches_both_failure_modes():
    """判別性那支的兩種壞情況，真實資料一種都到不了——所以各造一次。

    真樹永遠是「命中一個、不是全部」，於是 `_discrimination_problem` 裡兩個回報
    分支在上面那支測試裡**一次都不會執行**；刪掉任一個都照樣全綠。這一支是它們
    唯一的殺手，`None` 那一格則是 must-allow：少了它，一支「永遠回報有問題」的
    實作也會通過。
    """
    rows_mixed = [("a.py", 1, "no visible window title contains "),
                  ("b.py", 2, "comtypes is required"),
                  ("c.py", 3, "UIAutomationCore.dll unavailable: ")]
    assert _discrimination_problem("window title", rows_mixed, "E") is None

    dead = _discrimination_problem("caption", rows_mixed, "E")
    assert dead and "標題打錯" in dead, dead

    rows_all = [("a.py", 1, "window title x"), ("b.py", 2, "window title y")]
    blunt = _discrimination_problem("window title", rows_all, "E")
    assert blunt and "判別性" in blunt, blunt


def _type_names_compared_in_gui(tree: ast.Module) -> set:
    """`_gui_control` 裡拿 `__name__` 去跟字面值比對的那些型別名。

    這是 `_MATCHED_EXCEPTION_NAMES` 的**推導側**。清單那一側只回答「列著的名字還
    在不在套件裡」——那是 fail-closed 的方向。**另一個方向是 fail-open 的**：在
    `_gui_control` 多寫一個 `type(error).__name__ == "SomeNewError"` 而忘了加進
    清單，什麼都不會紅，那個新的分類從一開始就沒有被守到。本 repo 已經為這個形狀
    吃過好幾次虧（`_OWNER_ONLY_SLASH`、`_pid_alive` 的列舉、原子寫入的常數清單），
    每一次的處置都一樣：兩個方向都對。

    `==` 與 `!=` 都收：兩種都是「用字面字串認型別」，套件端改名的後果一模一樣。
    目前樹上只有 `==`，所以收 `!=` 不改變任何現況，純粹是把網子張在對的地方。
    """
    names: set = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.Eq, ast.NotEq))):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == "__name__"):
            continue
        right = node.comparators[0]
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            names.add(right.value)
    return names


def _reconcile_problem(derived: set, listed: set) -> str | None:
    """兩份名單對不上就回一句話，對得上回 `None`。

    **比較本身抽成一支，理由跟 `_discrimination_problem` 一樣**：真實資料永遠是
    「剛好相等」，所以把 `==` 放寬成 `>=`（＝只檢查「有沒有過期的」、不檢查「有
    沒有漏守的」）在真樹上完全看不出來——實測那個變異 SURVIVED。抽出來之後，
    合成語料可以餵一組 `derived ⊋ listed`，那是唯一分得開兩者的輸入。
    """
    unguarded = sorted(derived - listed)
    stale = sorted(listed - derived)
    if not unguarded and not stale:
        return None
    return (f"沒被列管、因此沒有守門的：{unguarded}；"
            f"列管了但程式碼裡已經不比對的（過期豁免）：{stale}。"
            "兩份都要動：新增一個字串比對的分類，就要把名字加進 "
            "`_MATCHED_EXCEPTION_NAMES`，否則套件端改名之後它會靜默退化成泛用訊息。")


def test_the_reconciliation_catches_both_directions():
    """兩個方向各造一次——真實資料一種都到不了。

    `None` 那一格是 must-allow：少了它，一支「永遠回報有問題」的實作也會通過。
    """
    assert _reconcile_problem({"A", "B"}, {"A", "B"}) is None
    only_derived = _reconcile_problem({"A", "B"}, {"A"})
    assert only_derived and "沒被列管" in only_derived, only_derived
    assert "'B'" in only_derived or "B" in only_derived, only_derived
    only_listed = _reconcile_problem({"A"}, {"A", "B"})
    assert only_listed and "過期豁免" in only_listed, only_listed


def test_the_matched_exception_list_is_reconciled_both_ways():
    """清單 ↔ `_gui_control` 裡真的做的比對，必須**剛好**相等。

    少了推導這一側，新增一個字串比對的分類是完全安靜的：上面那支照樣把舊的三個
    名字查一遍、照樣全綠，而新的那個從頭到尾沒有人看。2026-09-21 量測時兩邊都是
    三個、剛好一致——所以這支加進來不會有存量要清，這正是最便宜的時機。
    """
    derived = _type_names_compared_in_gui(_parse_gui_control())
    assert derived, (
        "`_gui_control` 裡一個拿 `__name__` 比字面值的地方都沒抽到——抽取器壞了，"
        "或那些分類改寫成別的形狀了（改寫了就把這裡一起更新）。")
    problem = _reconcile_problem(derived, set(_MATCHED_EXCEPTION_NAMES))
    assert problem is None, problem


def test_the_type_name_extractor_can_actually_tell_the_shapes_apart():
    """合成對照組：抽取器真的在讀 AST，而且只認「`__name__` vs 字面值」。

    沒有這一格，一個「回傳寫死的三個名字」的實作也會通過上面那支——推導被它要對帳
    的那份清單取代，兩個方向同時失效（本 repo 記過這個形狀）。
    """
    tree = ast.parse(
        "def f():\n"
        "    if type(error).__name__ == 'WantedEq':\n        pass\n"
        "    if error.__class__.__name__ != 'WantedNotEq':\n        pass\n"
        "    if type(error).__name__ == SOME_CONSTANT:\n        pass\n"
        "    if type(error).__module__ == 'not_a_name_attr':\n        pass\n"
        "    if error.args == 'not a name comparison':\n        pass\n"
        "    if type(error).__name__ in ('tuple', 'form'):\n        pass\n")
    assert _type_names_compared_in_gui(tree) == {"WantedEq", "WantedNotEq"}
