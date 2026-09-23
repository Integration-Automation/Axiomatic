"""同一個作用域裡「後面那個安靜蓋掉前面那個」的兩種寫法。

Python 兩種都不出聲：沒有錯誤、沒有警告、型別檢查也不會紅。

1. **重複的 `def` / `class`。** 第二個定義直接取代第一個，第一個整段變死碼。
2. **dict 字面值裡重複的鍵。** 後面那個贏，前面那個的值消失。

2026-09-06 這一支是被**當場踩到**才補的，而且踩的人是這個測試檔自己：在
`test_bot_helpers.py` 新增一節時，我把測試替身取名 `_FakeMessage`，而檔案前面
早就有一個同名的模組層類別。Python 安靜地讓我的版本勝出，然後**一支跟我的改動
毫無關係的既有測試**（`test_dorossi_loop_gate_composition`）掛掉，錯誤訊息是
`_FakeMessage.__init__() takes 1 positional argument but 2 were given`——指向的
位置離真正的原因一千多行。

這正是這類缺陷難查的地方：症狀出現在**別人**身上。在一個測試檔動輒兩百支測試、
替身名字又都很通用（`_FakeMessage`、`_FakeChannel`、`_Hostile`）的專案裡，撞名
只是時間問題；而 `discord_bot.py` 有一萬九千行，同名的第二個 handler 一樣不會有
任何訊號。

判準刻意排除幾種**合法**的同名重複：`@property` 配 `@x.setter` / `@x.getter` /
`@x.deleter`、`@typing.overload`、`@functools.singledispatch` 的 `.register`。
那些是語言本來就有的用法，報它們會讓這支變成會亂叫的守門。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"

# 這些裝飾器讓「同名再定義一次」變成合法用法。
_LEGITIMATE_REDEFINITION = (".setter", ".getter", ".deleter", ".register")
_LEGITIMATE_CONTAINS = ("overload", "singledispatch")


def _project_files() -> list[Path]:
    # 套件 ＋ `test/`（本檔所在的目錄）。測試 2026-09-22 之前住在套件裡、一直在這支
    # 的範圍內，搬家之後照舊。
    return (sorted(PKG_ROOT.rglob("*.py"))
            + sorted(Path(__file__).resolve().parent.rglob("*.py")))


def _is_legitimate_redefinition(node) -> bool:
    for deco in node.decorator_list:
        text = ast.unparse(deco)
        if text.endswith(_LEGITIMATE_REDEFINITION):
            return True
        if any(marker in text for marker in _LEGITIMATE_CONTAINS):
            return True
    return False


def _scopes(tree):
    """(作用域名稱, 該作用域直屬的 body)。模組／函式／類別各算一個。

    只看**直屬**的陳述句，不用 `ast.walk` 往下鑽——巢狀函式裡的同名定義是另一個
    作用域，不會互相蓋掉。
    """
    yield "<module>", tree.body
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.body


def _parse(path: Path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"), str(path))
    except SyntaxError as exc:  # pragma: no cover - 壞檔案由別的測試報
        pytest.fail(f"{path.name} 解析失敗：{exc}")


@pytest.mark.parametrize("path", _project_files(), ids=lambda p: p.name)
def test_no_definition_is_silently_replaced(path):
    """同一個作用域裡不得有兩個同名的 `def` / `class`。"""
    tree = _parse(path)
    clashes = []
    for scope_name, body in _scopes(tree):
        lines: dict[str, list[int]] = {}
        for stmt in body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            if _is_legitimate_redefinition(stmt):
                continue
            lines.setdefault(stmt.name, []).append(stmt.lineno)
        for name, at in lines.items():
            if len(at) > 1:
                clashes.append((scope_name, name, at))
    assert not clashes, "\n".join(
        f"{path.name}：`{scope_name}` 裡的 `{name}` 定義了 {len(at)} 次"
        f"（第 {'、'.join(str(x) for x in at)} 行）。最後一個會安靜地勝出，"
        "前面的整段變死碼——而症狀通常出現在**別人**身上："
        "既有的呼叫端會拿到新的那個定義，錯誤訊息指向的位置離原因很遠。"
        for scope_name, name, at in clashes)


@pytest.mark.parametrize("path", _project_files(), ids=lambda p: p.name)
def test_no_dict_literal_repeats_a_key(path):
    """dict 字面值裡不得有重複的鍵——後面那個贏，前面那個安靜消失。

    只看常數鍵（字串／數字）。變數鍵在靜態上判不出相不相等，猜了就會亂叫。
    """
    tree = _parse(path)
    repeats = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen: dict[object, int] = {}
        for key in node.keys:
            if key is None or not isinstance(key, ast.Constant):
                continue          # `**expansion` 或變數鍵——判不出來就不猜
            marker = (type(key.value).__name__, key.value)
            seen[marker] = seen.get(marker, 0) + 1
        for (_kind, value), count in seen.items():
            if count > 1:
                repeats.append((node.lineno, value, count))
    assert not repeats, "\n".join(
        f"{path.name}:{line} 的 dict 字面值把 `{value!r}` 寫了 {count} 次——"
        "只有最後一個會留下來。"
        for line, value, count in repeats)


def test_the_scan_actually_looks_at_something():
    """守門的自我檢查：確認真的掃到了定義，而不是靜靜地掃了個空。

    這類靜態守門最典型的失效方式是「路徑或解析壞掉 → 掃到 0 個目標 → 永遠綠」。
    順便釘住「合法的同名重複不會被報」——那是這支測試不亂叫的前提。
    """
    total = 0
    for path in _project_files():
        for _scope, body in _scopes(_parse(path)):
            total += sum(
                1 for s in body
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)))
    assert total > 1000, f"只掃到 {total} 個定義——掃描本身可能壞了"

    legit = ast.parse(
        "class C:\n"
        "    @property\n"
        "    def x(self): return 1\n"
        "    @x.setter\n"
        "    def x(self, v): pass\n")
    setter = legit.body[0].body[1]
    assert _is_legitimate_redefinition(setter), (
        "`@x.setter` 被當成重複定義了——這支守門會對每個 property 亂叫")


def test_the_scan_reaches_inside_classes_and_functions():
    """撞名不是只會發生在模組層。

    這一支是變異測試逼出來的：把 `_scopes` 收窄成「只 yield 模組層」時，上面兩支
    掃描測試**照樣全綠**——真實的程式碼裡剛好沒有巢狀的撞名可以抓，而自我檢查數
    的是定義總數，光模組層就已經超過門檻了。所以這裡不靠真實程式碼，直接餵一棵
    合成的樹進去驗。
    """
    tree = ast.parse(
        "def outer():\n"
        "    def inner(): pass\n"
        "    def inner(): pass\n"
        "class K:\n"
        "    def m(self): pass\n"
        "    def m(self): pass\n")
    names = {scope for scope, _body in _scopes(tree)}
    assert {"<module>", "outer", "K"} <= names, (
        f"`_scopes` 沒有走進巢狀作用域，只看到 {names}")

    # 兩個作用域各自都要抓得到自己的撞名。
    hits = []
    for scope_name, body in _scopes(tree):
        seen: dict[str, int] = {}
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                seen[stmt.name] = seen.get(stmt.name, 0) + 1
        hits.extend((scope_name, n) for n, c in seen.items() if c > 1)
    assert sorted(hits) == [("K", "m"), ("outer", "inner")], hits


# --- Python 有出聲，但沒有人在聽 ---------------------------------------------
#
# 上半場守的是「Python 兩種都不出聲」。這一節守的是相反的情況：直譯器**確實**發了
# SyntaxWarning，只是沒有人在聽——pytest 不會因為被測模組編譯時噴警告而變紅，這個
# 專案也沒有 CI，ruff 是手動跑而且習慣只開 `--select F,E9`（W605 不在裡面）。於是
# 警告寫在那裡，沒有任何一個流程會看到它。
#
# 實測（3.14.4）這一類涵蓋三種東西，三種的後果都不只是「不好看」：
#
#   1. **無效轉義序列**（`"C:\dorossi\evil"` 裡的 `\d`，docstring 裡也算）。訊息
#      本身就寫著 *"Such sequences will not work in the future"*——這是排定要變成
#      SyntaxError 的，到那天該模組會直接 import 不起來。這個專案滿地 Windows
#      路徑，寫 docstring 時忘記加 `r` 前綴是**遲早**的事，不是假設。
#   2. **`assert (cond, "訊息")`**——多一組括號，assert 的對象就變成一個永遠為真的
#      tuple。訊息是 `assertion is always true, perhaps remove parentheses?`。在一
#      個五千多支測試的 repo 裡，這等於一支**永遠不會失敗的測試**：它會被算進通過
#      數、看起來在守著某件事，實際上什麼都沒守。這是這一節真正的主角。
#   3. **`x is "foo"` / `x is 1000`**——拿 `is` 比對字面值，行為取決於直譯器的
#      interning，換個值或換個版本就變。
#
# **為什麼是靜態掃描，而不是把警告轉成錯誤。** 這是實測出來的，不是偏好：
# 上面三種警告都是在**編譯**模組時發出的，而暖的 `__pycache__` 會跳過編譯，
# 於是警告**不會**再被發出來。同一份原始碼、同一個指令，連跑三次：
#
#   | 這一次 | `__pycache__` | 有發出警告 | 結果 |
#   |---|---|---|---|
#   | 第一次 | 冷 | **有** | `1 passed, 1 warning` |
#   | 第二次 | 暖 | **沒有** | `1 passed` |
#   | 刪掉快取再跑 | 冷 | **有** | `1 passed, 1 warning` |
#
# 真實的 repo 裡快取幾乎永遠是暖的，所以寫下 `assert (x == y, "msg")` 的人只會在
# **第一次**看到那行警告（還埋在一堆輸出裡），之後永遠安靜，而那支測試永遠通過。
#
# `-W error::...` 也救不了既有的違規，這點反直覺：先用預設跑一次（`.pyc` 寫出來
# 了）再加上旗標 → `rc=0, 1 passed`，旗標沒有東西可以轉換，因為根本沒有重新編譯；
# 要先砍掉 `__pycache__` 才會變成 `rc=2`。也就是說，把旗標加進一個既有專案，
# **所有既有的違規都是看不見的**。
#
# `compile()` 直接吃原始碼字串，從來不看 `__pycache__`，所以這道守門對快取狀態
# 免疫；而且它也涵蓋**正式模組**——pytest 的 assertion rewrite 警告只作用在被收集
# 的測試檔上。
#
# 為什麼掃描範圍是 repo root ＋ 套件目錄，而不是沿用本檔的 `_project_files()`：
# 那支只看 `axiomatic/`，但 `start_webrunner.py` / `start_discord_bot.py` /
# `run_batch.py` 都在 repo root，而且正是「一支 py 壞掉就整個啟動不了」的那種檔案。
# CLAUDE.md 已經記過同一個坑（原子寫入守門那份寫死的六元組，漏掉的正好是 repo root
# 的 `start_webrunner.py`，而寬掃與窄掃在當下**看起來一模一樣**）。

_SYNTAX_SCAN_SKIP = (".venv", "legacy", "docs", "node_modules", ".git")


def _syntax_warnings_in(source: str, name: str) -> list:
    """編譯一段原始碼，回傳它發出的 SyntaxWarning。**純函式**，合成語料問得到。"""
    import warnings as _warnings

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        try:
            compile(source, name, "exec")
        except SyntaxError:
            # 連 compile 都失敗是另一回事（而且別的守門會叫），這裡不重複報。
            return []
    return [w for w in caught if issubclass(w.category, SyntaxWarning)]


def _syntax_scan_files() -> list[Path]:
    repo_root = PKG_ROOT.parent
    return sorted(
        p for p in repo_root.rglob("*.py")
        if not any(part in _SYNTAX_SCAN_SKIP for part in p.parts))


def test_the_syntax_warning_detector_actually_bites():
    """正對照組，三種各一。

    沒有這一支，現況乾淨時「掃到 0 個」與「偵測器根本沒在編譯」是同一個結果——空的
    選取看起來永遠像是通過。負對照組同樣重要：會亂叫的守門遲早被關掉。
    """
    def messages(src):
        return [str(w.message) for w in _syntax_warnings_in(src, "<synthetic>")]

    assert any("invalid escape" in m
               for m in messages('x = "C:\\dorossi\\evil"\n'))
    assert any("invalid escape" in m
               for m in messages('def f():\n    """path \\d here"""\n'))
    assert any("always true" in m
               for m in messages('def t():\n    assert (1 == 2, "msg")\n'))
    assert any('"is" with' in m
               for m in messages('def f(x):\n    return x is "foo"\n'))

    # 負對照組：正常寫法一個都不該叫
    assert messages('x = r"C:\\dorossi\\evil"\n') == []
    assert messages('def t():\n    assert 1 == 2, "msg"\n') == []
    assert messages('def f(x):\n    return x == "foo"\n') == []


def test_no_project_source_compiles_with_a_syntax_warning():
    """全專案不得有 SyntaxWarning。三種後果分別是：未來直接 import 不起來、一支
    永遠不會失敗的測試、行為取決於 interning 的比較。

    修法各自不同——路徑字串加 `r` 前綴、拿掉 assert 多出來的那組括號、`is` 換成
    `==`——但共通點是**警告訊息已經把答案寫出來了**，照做即可。
    """
    files = _syntax_scan_files()
    assert len(files) > 50, f"只掃到 {len(files)} 支 .py——掃描範圍可能壞了"

    problems = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue          # 讀不到是 test_text_encoding / conftest 的守備範圍
        for warning in _syntax_warnings_in(source, str(path)):
            problems.append(
                f"{path.relative_to(PKG_ROOT.parent)}:{warning.lineno}  "
                f"{warning.message}")

    assert not problems, (
        "這些地方編譯時會發 SyntaxWarning：\n" + "\n".join(problems))


# --- 寫得出來，但執行不到 -----------------------------------------------------
#
# 前兩節守的是「同一個名字被後面那個安靜蓋掉」與「直譯器有出聲但沒有人在聽」。
# 第三種死碼連警告都沒有：**同一個區塊裡，`return` / `raise` / `break` /
# `continue` 後面還接著陳述句**。Python 照樣編譯、執行到那個終止點就離開，後面
# 那幾行永遠不會跑；沒有錯誤、沒有警告、型別檢查也不紅。
#
# 2026-09-20 全樹掃出一筆，而它落在一個讀程式碼很難發現的位置：
# `audit_dependencies.source_repo()` 的結尾是
# `return repo_from_project_urls(candidates)` 接著一行 `return None`。兩行**單獨
# 看都正常**——第二行讀起來像「解不出倉庫就回 None」的防守，而那個語意其實已經由
# 第一行的回傳值提供了。壞掉的不是哪一行，是順序；而這正是人眼最容易跳過的形狀。
#
# 為什麼不是交給 linter：pylint 的 `W0101` 抓得到同一筆（2026-09-20 實測，兩者
# 結果一模一樣，而且都只有那一筆）。但 pylint 與 ruff 都**不在 `requirements.txt`
# 裡**，這個專案對這件事已經裁定過（CLAUDE.md 的文字編碼那一節）：把它們變成
# pytest 閘門等於「多一個 fresh clone 要裝的相依」或「一支永遠跳過的測試」，而
# 永遠跳過的測試是裝飾品。這一支只用 `ast`，兩個直譯器與 fresh clone 一律照跑。
#
# 判準刻意只收**語法上**就確定的四種終止。`sys.exit()` / `os._exit()` /
# `pytest.fail()` 這種「呼叫了就不會回來」的形狀不收：要收就得先認出那是哪一個
# 函式，而認錯會對正常的程式碼亂叫。收窄的代價是漏抓，跟 `test_language.py` 刻意
# 不收「通過／文件／程序」是同一個取捨。

_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


def _statement_blocks(tree):
    """(擁有節點, 欄位名, 陳述句串列)——每一個**自成一塊**的 body。

    只收真的裝著陳述句的串列：`Lambda.body` / `IfExp.body` 是運算式不是串列，
    `Try.handlers` 裝的是 `ExceptHandler` 而不是 `stmt`，兩種都會被濾掉。
    """
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list) or not block:
                continue
            if all(isinstance(stmt, ast.stmt) for stmt in block):
                yield node, field, block


def _unreachable_statements(tree):
    """回傳 (終止點行號, 終止點種類, 第一行到不了的陳述句行號)。

    **同一塊裡**才算，這一步是**放寬**：巢狀區塊裡的 `return` 不會讓它外面接著
    的陳述句變成死碼——那條 `if` 可能根本沒成立。所以殺得掉「拿掉同塊限制」這個
    變異的只有 must-allow 那幾格，不是 must-block。
    """
    found = []
    for _node, _field, block in _statement_blocks(tree):
        for index, stmt in enumerate(block[:-1]):
            if isinstance(stmt, _TERMINATORS):
                # 同一塊裡後面全是死的，報第一筆就夠。
                found.append((stmt.lineno, type(stmt).__name__,
                              block[index + 1].lineno))
                break
    return found


_DEAD_CODE_SAMPLES = {
    "return": "def f():\n    return 1\n    print('never')\n",
    "raise": "def f():\n    raise ValueError('x')\n    cleanup()\n",
    "break": "def f(xs):\n    for x in xs:\n        break\n        use(x)\n",
    "continue": "def f(xs):\n    for x in xs:\n        continue\n        use(x)\n",
}

_LIVE_CODE_SAMPLES = {
    # 終止點就是最後一句——正常得不能再正常。
    "terminator last": "def f():\n    work()\n    return 1\n",
    # `return` 在巢狀區塊裡，外面接著的那一行照樣跑得到。
    "return inside if": "def f(c):\n    if c:\n        return 1\n    return 2\n",
    # `break` 在 `if` 裡面，迴圈體剩下的部分在沒中斷時仍然會執行。
    "break inside if": ("def f(xs):\n    for x in xs:\n        if bad(x):\n"
                        "            break\n        use(x)\n"),
    # `raise` 在 `except` 裡，`finally` 是另一塊。
    "raise in handler": ("def f():\n    try:\n        go()\n"
                         "    except OSError:\n        raise\n"
                         "    finally:\n        close()\n"),
}


def test_the_unreachable_detector_bites_on_every_terminator():
    """正對照組四格、負對照組四格。

    現況是乾淨的，所以少了這一支，「掃到 0 筆」與「偵測器根本沒在看」是同一個
    結果——空的選取看起來永遠像通過。負對照組同樣要有：這支守門真正的風險不是
    漏抓，是對著一個 `if c: return` 亂叫，然後被關掉。
    """
    for label, source in _DEAD_CODE_SAMPLES.items():
        hits = _unreachable_statements(ast.parse(source))
        assert len(hits) == 1, f"{label}：應該報一筆，卻報了 {hits}"
        assert hits[0][1].lower() == label, f"{label}：報成了 {hits[0][1]}"

    for label, source in _LIVE_CODE_SAMPLES.items():
        hits = _unreachable_statements(ast.parse(source))
        assert hits == [], f"{label}：正常的寫法被報成死碼（{hits}）"


def test_the_block_walk_reaches_every_kind_of_body():
    """`_statement_blocks` 要走進每一種 body，不是只有模組層。

    這一格是照 `test_the_scan_reaches_inside_classes_and_functions` 的教訓寫的：
    把走訪收窄成「只看模組層」時，上面那支與下面那支**都會照樣綠**，因為合成語料
    的死碼剛好都在函式裡、而真實的樹本來就沒有東西可以報。所以直接問走訪本身。
    """
    source = (
        "class K:\n"
        "    def m(self):\n"
        "        if a:\n"
        "            x = 1\n"
        "        else:\n"
        "            y = 2\n"
        "        for i in z:\n"
        "            w = i\n"
        "        while a:\n"
        "            v = 1\n"
        "        with open('f') as fh:\n"
        "            u = fh\n"
        "        try:\n"
        "            t = 1\n"
        "        except OSError:\n"
        "            s = 2\n"
        "        else:\n"
        "            r = 3\n"
        "        finally:\n"
        "            q = 4\n")
    kinds = {f"{type(node).__name__}.{field}"
             for node, field, _block in _statement_blocks(ast.parse(source))}
    for wanted in ("Module.body", "ClassDef.body", "FunctionDef.body",
                   "If.body", "If.orelse", "For.body", "While.body",
                   "With.body", "Try.body", "ExceptHandler.body",
                   "Try.orelse", "Try.finalbody"):
        assert wanted in kinds, f"`_statement_blocks` 沒有走到 {wanted}：{kinds}"


def test_no_project_source_has_an_unreachable_statement():
    """全專案不得有「同一塊裡接在終止點後面」的陳述句。

    修法只有兩種，而且要先想清楚是哪一種：那幾行**本來就多餘**（刪掉），或是
    **順序寫反了**（搬到終止點前面）。後者是真的缺陷，前者只是雜訊——但兩者長得
    一模一樣，所以不能預設是前者。
    """
    files = _syntax_scan_files()
    assert len(files) > 50, f"只掃到 {len(files)} 支 .py——掃描範圍可能壞了"

    blocks_seen = 0
    problems = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue          # 讀不到／剖析不開是別支守門的守備範圍
        blocks_seen += sum(1 for _n, _f, _b in _statement_blocks(tree))
        for at, kind, dead_at in _unreachable_statements(tree):
            problems.append(
                f"{path.relative_to(PKG_ROOT.parent)}:{dead_at} 接在第 {at} 行的 "
                f"`{kind.lower()}` 後面，同一塊裡，永遠不會執行")

    # 範圍的正對照組：走訪壞掉時「掃到 0 筆問題」跟「一塊都沒走進去」不可分辨。
    assert blocks_seen > 5000, f"只走到 {blocks_seen} 個區塊——走訪本身可能壞了"
    assert not problems, (
        "這些陳述句永遠不會執行：\n" + "\n".join(problems))
