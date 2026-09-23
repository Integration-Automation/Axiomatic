"""`except` 的順序——Python 對這件事完全不出聲。

把 `except Exception` 排到具名 handler 前面，後面那些一個都不會被執行到：沒有
錯誤、沒有警告、型別檢查也不會紅，原始碼看起來一字未改，所有既有測試照樣綠。
2026-09-05 在自走迴圈上實測過一次：三個具名 handler（用量上限／暫時性過載／輸出
靜默）整段變成死碼，後果是「後端還沒放行就放棄」「過載不再走長退避」「靜默不再
重生」——而唯一的訊號是無人值守的任務半夜停了。

當時補的守門只釘住那一個函式的那一個 `try`。這一支把同一個判準推到整個套件：
判準本身跟那個函式無關，而下一個踩到的人不會知道有那條規則。

三道檢查，各自擋不同的洞：

1. **泛用 handler 後面不得再有 handler。** 不需要知道後面那個是什麼類別——只要
   前面接走了一切，後面就是死碼。唯一的例外是「繼承 BaseException 但不繼承
   Exception」的那幾個（`KeyboardInterrupt`、`asyncio.CancelledError` …），
   它們排在 `except Exception` 後面仍然活著。
2. **窄 handler 不得被更寬的同族 handler 蓋住**（`except OSError` 排在
   `except FileNotFoundError` 前面）。這一道用真的類別解析，只認內建例外與一小份
   標準函式庫名單——解析不到的不猜。
3. **專案自訂的例外都必須繼承 Exception。** 這道不是潔癖，是在**驗證第 1 道的
   前提**：第 1 道對解析不到的名字保守假設「它繼承 Exception」。哪天有人寫了
   `class X(BaseException)`，那個假設就不成立，而第 1 道會誤報。與其等它亂叫，
   不如讓這一支先講清楚。

另外釘住「沒有人**吞掉**取消訊號」。在一個 asyncio bot 裡吞掉 `CancelledError`
不是小事：工作被取消時那個例外就是取消的機制本身，吞掉它等於任務取消不掉——
`/dorossi` 的中止、關機流程都靠它。

**這一道 2026-09-09 從「關鍵字」改成「機制」。** 原本的判準是「原始碼裡不准出現
bare `except:` 或 `except BaseException`」，而那跟規則的名字不是同一件事：一個
攔下 `BaseException`、做完同步收尾、再**原樣 `raise` 出去**的 handler 什麼都沒吞。
`dorossi_backend._dorossi_via_claude_code` 正需要這個形狀——取消路徑上必須同步把
後端行程砍掉（不保證還跑得完任何 `await`），而 `CancelledError` 是 `BaseException`，
`except Exception` 接不到。舊判準會把它報成「吞掉取消訊號」，而它做的正好相反。
現在由 `_reraises_unconditionally()` 依出口分析放行，判準寫在那支的 docstring 裡。
真正會吞的仍然零筆，所以 `_ALLOWED_BASE_HANDLERS` 還是空的。
"""
from __future__ import annotations

import ast
import builtins
import importlib
import io
import re
import sys
import tokenize
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"

# 允許 import 來解析例外類別的模組。刻意窄：解析不到的一律不猜，
# 由第 1 道的保守假設接手。
_RESOLVABLE_MODULES = frozenset({
    "asyncio", "json", "subprocess", "shutil", "re", "socket", "ssl",
    "importlib", "urllib", "sqlite3",
})

# 繼承 BaseException 但**不**繼承 Exception：`except Exception` 抓不到它們，
# 所以排在泛用 handler 後面仍然活著。標準函式庫那幾個分開列，是為了讓下面的對帳
# 只問「專案自己寫的那些」——內建名字不會出現在本專案的 `class` 宣告裡。**分開列
# 不等於抄兩份**：整份清單由這一份聯集出來，所以只有一個來源。
_STDLIB_CATCH_ALL_SURVIVORS = frozenset({
    "KeyboardInterrupt", "SystemExit", "GeneratorExit", "BaseException",
    "asyncio.CancelledError",
})

_SURVIVES_CATCH_ALL = _STDLIB_CATCH_ALL_SURVIVORS | frozenset({
    # `test_webrunner_shared._ScriptCaptured`：**刻意**繼承 `BaseException`。
    # 它是「把送給 `port.execute_script` 的實際字串抓出來」用的訊號，而被測的
    # 那些函式外層是 `except Exception`——繼承 `Exception` 的話訊號會被受測程式
    # 自己吞掉，測試就變成永遠綠。同一條在 `discord-bot-expert.md` 記過：會吞
    # 例外的函式，測試替身要用「記錄」不能用「爆炸」；這裡是它的另一面，替身
    # 必須爆在受測程式抓不到的層級。
    "_ScriptCaptured",
    # 背景迴圈的停止哨兵，同一條理由的第二、三個實例。要測一個 `while True` 的
    # 背景迴圈，就得從外面把它停下來（假的 sleep／假的回合丟一個哨兵）；而那種
    # 迴圈**一定**有 `except Exception`，因為它的職責就是撐過一個壞掉的 tick。
    # 哨兵若繼承 `Exception`：
    #   * `_LoopDone`（`test_bot_presence_logging`，§8.102）——迴圈體裡三段
    #     `except Exception`，而那組測試要抓的變異有一半正是「try 的範圍被改寬
    #     了」，於是那個變異會**連哨兵一起吃掉**，測試從紅變成**掛住**；
    #   * `_LoopHalt`（`test_dorossi_loop`，§8.104）——更硬，因為自走迴圈的泛用
    #     `except Exception` 不只吃掉，它**還會重試**：哨兵會被當成偶發失敗、
    #     退避、重跑、再撞到用完的腳本、再被吃掉，**無限**。
    # 一個要靠被測程式配合才停得下來的測試，會在它最該變紅的那一天停不下來。
    "_LoopDone",
    "_LoopHalt",
})

_CATCH_ALL_NAMES = frozenset({"<bare>", "Exception", "BaseException"})

# bare `except:` / `except BaseException` 的例外清單。空的——目前全專案零筆。
# 真的需要就寫進來並附理由（會吞掉 `CancelledError` 與 `KeyboardInterrupt`）。
#
# **注意這份清單的語意**：列在這裡＝「這個 handler 真的會吞掉取消訊號，而我們接受」。
# 一個**會把例外原樣再拋出去**的 handler 不屬於這裡——它根本沒有吞任何東西，寫進來
# 反而是把它記錄成一件它沒做的事。那一類由 `_reraises_unconditionally()` 自動放行。
_ALLOWED_BASE_HANDLERS: dict[str, str] = {}


def _project_files(pkg_root=None, repo_root=None) -> list[Path]:
    """掃描範圍：套件 ＋ **repo root 的入口腳本**。

    這幾條規則的文字一個模組都沒提到——「泛用 handler 後面是死碼」「窄 handler 不得
    被父類別蓋住」「不得吞掉取消訊號」在哪個檔裡都成立。2026-09-10 之前這裡只有
    `PKG_ROOT.rglob("*.py")`，而那個 glob 的實際內容是 **89 個檔、其中 57 個是
    `test_*.py`**（`axiomatic/` 底下沒有 `.py` 子目錄，所以 `rglob` 等於 `glob`）
    ——也就是說它掃了每一支不會上線的測試，卻掃不到 `start_discord_bot.py`，
    而那支正在監督正式的 bot。

    族群是真的：repo root 四支腳本共 18 個 handler。最該管的兩個都在那裡——
    `start_discord_bot.py` 的監督者迴圈是無條件 `while True`、**只有 Ctrl+C 會
    break**，一個 bare `except:` 就讓它殺不掉；`start_webrunner._live_webrunner_pid`
    則是 `except FileNotFoundError` 排在 `except (OSError, UnicodeDecodeError)`
    前面，把兩行對調就會讓前者變死碼，而 pid 檔在讀取瞬間被刪會從「沒有批次在跑」
    變成「判不出來」——那個區別正是它回 `(pid, decidable)` 兩個值的理由。

    今天那 18 個 handler 零違規，所以這是**趁乾淨鎖範圍**；範圍本身由
    `test_the_scan_reaches_a_brand_new_repo_root_script` 另外釘（§8.8(A3)）。

    找到它的線索是同一族守門的內部不一致：`test_dorossi_teardown` 與
    `test_bot_helpers` 的 `_project_module_asts()` 一直都是「套件 ＋ repo root」。

    兩個參數只給範圍釘樁換目錄用——正式呼叫一律不帶引數。

    2026-09-22 測試從套件搬到 repo 根目錄的 `test/`，範圍照舊（套件 ＋ 測試 ＋ root
    腳本），所以 `test/` 另外列；它從 `root` 推，範圍釘樁換掉 root 時跟著換。
    """
    pkg = PKG_ROOT if pkg_root is None else pkg_root
    root = pkg.parent if repo_root is None else repo_root
    return (sorted(pkg.rglob("*.py")) + sorted((root / "test").rglob("*.py"))
            + sorted(root.glob("*.py")))


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    """一個 handler 接的所有例外名字（`except (A, B)` 算兩個）。"""
    node = handler.type
    if node is None:
        return ["<bare>"]
    if isinstance(node, ast.Tuple):
        out: list[str] = []
        for element in node.elts:
            out.extend(_handler_names(
                ast.ExceptHandler(type=element, name=None, body=[])))
        return out
    return [ast.unparse(node)]


def _resolve(name: str) -> type | None:
    """把名字解析成真的例外類別；解析不到回 None（不猜）。"""
    obj = getattr(builtins, name, None)
    if isinstance(obj, type) and issubclass(obj, BaseException):
        return obj
    if "." in name:
        module_name, _, attr = name.rpartition(".")
        if module_name.split(".")[0] in _RESOLVABLE_MODULES:
            try:
                obj = getattr(importlib.import_module(module_name), attr, None)
            except Exception:  # pylint: disable=broad-except
                return None
            if isinstance(obj, type) and issubclass(obj, BaseException):
                return obj
    return None


def _tries(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    except SyntaxError as exc:  # pragma: no cover - 壞檔案由別的測試報
        pytest.fail(f"{path.name} 解析失敗：{exc}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, ast.TryStar)):
            yield node


@pytest.mark.parametrize("path", _project_files(), ids=lambda p: p.name)
def test_no_handler_sits_behind_a_catch_all(path):
    """泛用 handler 之後的 handler 全是死碼。

    這一道刻意**不需要**解析後面那個類別：只要前面接走了一切，後面就進不去。
    這很重要——這個專案真正踩到的那次，被蓋住的是自訂例外
    （`_DorossiUsageLimitError` 之類），任何靠「認得那個類別」的檢查都會漏掉。
    """
    dead = []
    for node in _tries(path):
        catch_all: tuple[str, int] | None = None
        for handler in node.handlers:
            names = _handler_names(handler)
            if catch_all is not None:
                for name in names:
                    if name in _SURVIVES_CATCH_ALL:
                        continue
                    resolved = _resolve(name)
                    if resolved is not None and not issubclass(resolved, Exception):
                        continue
                    dead.append((handler.lineno, name, catch_all))
            if catch_all is None:
                hit = next((n for n in names if n in _CATCH_ALL_NAMES), None)
                if hit is not None:
                    catch_all = (hit, handler.lineno)
    assert not dead, "\n".join(
        f"{path.name}:{line} 的 `except {name}` 永遠不會被執行到"
        f"——第 {ca_line} 行的 `except {ca_name}` 先把一切接走了。"
        for line, name, (ca_name, ca_line) in dead)


@pytest.mark.parametrize("path", _project_files(), ids=lambda p: p.name)
def test_no_handler_is_shadowed_by_a_wider_relative(path):
    """`except OSError` 排在 `except FileNotFoundError` 前面 → 後者是死碼。

    只比對**不同** handler 之間：同一個 `except (OSError, shutil.Error)` 裡的重複
    只是冗贅，不是缺陷，把它一起報會讓這支測試變成會亂叫的那種。
    """
    shadowed = []
    for node in _tries(path):
        seen: list[tuple[str, type, int]] = []
        for handler in node.handlers:
            current: list[tuple[str, type]] = []
            for name in _handler_names(handler):
                resolved = _resolve(name)
                if resolved is None:
                    continue
                for prev_name, prev_cls, prev_line in seen:
                    if issubclass(resolved, prev_cls):
                        shadowed.append(
                            (handler.lineno, name, prev_name, prev_line))
                current.append((name, resolved))
            seen.extend((n, c, handler.lineno) for n, c in current)
    assert not shadowed, "\n".join(
        f"{path.name}:{line} 的 `except {name}` 進不去"
        f"——第 {prev_line} 行的 `except {prev_name}` 是它的父類別。"
        for line, name, prev_name, prev_line in shadowed)


def test_every_project_exception_inherits_from_exception():
    """驗證上面第一道測試的前提。

    那一道對解析不到的名字保守假設「它繼承 Exception」，所以排在 `except
    Exception` 後面就算死碼。專案裡真的出現一個 `class X(BaseException)` 時那個
    假設就壞了，而症狀會是**誤報**——一支會亂叫的守門最後會被人關掉。與其等它亂
    叫，不如在這裡先講清楚：要嘛別這樣寫，要嘛把它加進 `_SURVIVES_CATCH_ALL`。
    """
    offenders = []
    for path in _project_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        local: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                local[node.name] = [ast.unparse(b) for b in node.bases]
        for name, bases in local.items():
            if not any(b.endswith(("Error", "Exception")) for b in bases):
                continue        # 不是例外類別
            # ⚠️ 這一句到 2026-09-10 才補上，而**失敗訊息從一開始就叫人去改
            # `_SURVIVES_CATCH_ALL`**——那份清單先前只被上面那道死碼分析讀，
            # 這裡根本沒看過它。也就是說照著訊息做**不會讓測試變綠**。
            # 一個指向無效補救的錯誤訊息比沒有訊息更糟：它讓人在對的地方
            # 做了對的事，然後懷疑自己。
            if name in _SURVIVES_CATCH_ALL:
                continue
            # 順著同檔案裡的繼承鏈往上找，看會不會走到 BaseException。
            chain, cursor, guard = list(bases), list(bases), 0
            while cursor and guard < 20:
                guard += 1
                nxt = []
                for base in cursor:
                    nxt.extend(local.get(base, []))
                chain.extend(nxt)
                cursor = nxt
            if "BaseException" in chain:
                offenders.append(f"{path.name}:{name} <- {bases}")
    assert not offenders, (
        "這些例外類別直接掛在 `BaseException` 底下，"
        "`except Exception` 抓不到它們，而 "
        "`test_no_handler_sits_behind_a_catch_all` 會把它們誤報成死碼："
        f"{offenders}。請改成繼承 `Exception`，或把名字加進 `_SURVIVES_CATCH_ALL`。")


def _has_escape(node, in_loop: bool = False) -> bool:
    """這棵子樹裡有沒有「離開 handler 但不重拋」的出口（`return` / 逃出去的
    `break` / `continue`）。

    不進入巢狀的函式／lambda／class——它們的 `return` 是自己的出口，跟外面這個
    handler 無關。`break` / `continue` 只有在**不被 handler 內部的迴圈綁住**時才
    算逃出去（handler 自己寫了一個 `for`，那個 `break` 只是跳出那個 `for`）。

    `in_loop` 講的是「**這個 node 的子節點**在不在迴圈裡」，所以旗標要在進入本層
    時就依 `node` 自己的型別更新——寫成「依 child 的型別決定要不要往下帶」會差一
    層：`for` 迴圈**自己的 body** 那一層還是 False，於是迴圈裡的 `break` 被誤判成
    逃出 handler。（第一版就是這樣寫的，合成對照組當場抓到。）
    """
    in_loop = in_loop or isinstance(node, (ast.For, ast.AsyncFor, ast.While))
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(child, ast.Return):
            return True
        if isinstance(child, (ast.Break, ast.Continue)) and not in_loop:
            return True
        if _has_escape(child, in_loop):
            return True
    return False


def _reraises_unconditionally(handler: ast.ExceptHandler) -> bool:
    """這個 handler 的每一條出口都是**裸的 `raise`**——也就是它什麼都沒吞。

    判準是機制：一個例外要被「吞掉」，只能是 (a) 執行流程走到 handler 尾端就自然
    結束、(b) `return` / 逃出去的 `break`／`continue`、或 (c) `raise <別的東西>`
    ——最後這個會把 `CancelledError` 換成一個普通例外，取消一樣失效，所以**必須是
    裸的** `raise`。反過來說，**巢狀的 `try/except` 吞不掉外層那個例外**：它只管
    自己 try 區塊裡新丟出來的東西。所以
    `except BaseException: try: kill() except Exception: pass; raise` 是乾淨的。

    這件事有實際重量。`dorossi_backend._dorossi_via_claude_code` 需要在取消路徑上
    **同步**把後端行程砍掉（取消路徑不保證還跑得完任何 `await`，而 `proc.kill()`
    不是 coroutine），而 `CancelledError` 是 `BaseException`，`except Exception`
    接不到它——所以那裡非 `except BaseException` 不可，然後原樣重拋。純文字判準會
    把它報成「吞掉取消訊號」，而它做的正好相反。**一支會亂叫的守門最後會被人關掉**
    （本檔 `test_every_project_exception_inherits_from_exception` 的存在理由同此），
    所以判準跟著規則的名字走，不跟著關鍵字走。
    """
    if not handler.body:
        return False
    last = handler.body[-1]
    if not (isinstance(last, ast.Raise) and last.exc is None):
        return False
    return not any(_has_escape(stmt) for stmt in handler.body)


def _cancellation_swallowers(trees) -> list[str]:
    """`[(模組名, AST)]` → 會吞掉取消訊號的 handler 清單。

    掃描這一半跟真實資料那一半分開，是因為**真實資料乾淨時這支測不出東西**：
    一份零違規的原始碼會讓「掃描器正常」與「掃描器整個壞掉」產出一模一樣的結果。
    所以下面每一種形狀都有自己的合成對照組。
    """
    out = []
    for module_name, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                for name in _handler_names(handler):
                    if name not in ("<bare>", "BaseException"):
                        continue
                    if _reraises_unconditionally(handler):
                        continue
                    key = f"{module_name}:{handler.lineno}"
                    if key in _ALLOWED_BASE_HANDLERS:
                        continue
                    out.append(f"{key} except {name}")
    return out


def test_nothing_swallows_cancellation():
    """bare `except:` 與 `except BaseException` **吞掉**取消訊號的那一種。

    在 asyncio 裡 `CancelledError` **就是**取消機制本身：吞掉它，被取消的工作會
    若無其事地繼續跑下去。這個 bot 靠取消收工的地方不只一處（`/dorossi` 的中止、
    關機時的背景任務收尾）。`KeyboardInterrupt` 同理——Ctrl+C 按下去沒反應。

    **「攔下來再原樣拋出去」不算吞**，理由與判準寫在 `_reraises_unconditionally`。
    目前全專案零筆真正的吞噬者，所以 `_ALLOWED_BASE_HANDLERS` 是空的。
    """
    trees = [(path.name, ast.parse(path.read_text(encoding="utf-8"), str(path)))
             for path in _project_files()]
    assert len(trees) >= 25, (
        f"只抽到 {len(trees)} 個模組，抽取器壞了——抽不到檔案時「零筆違規」跟"
        "「全部乾淨」在輸出上長得一模一樣。")
    offenders = _cancellation_swallowers(trees)
    assert not offenders, (
        f"這些 handler 會連 `CancelledError` / `KeyboardInterrupt` 一起吞掉："
        f"{offenders}。改成 `except Exception`；真的需要攔 `BaseException` 就在"
        "最後原樣 `raise` 出去（那不算吞）；真的要吞才進 "
        "`_ALLOWED_BASE_HANDLERS` 並寫理由。")


def test_the_module_floor_fires_when_the_enumerator_comes_back_empty(monkeypatch):
    """下限自己的對照組：把來源換成空的，`test_nothing_swallows_cancellation`
    必須紅。

    沒有這一支的話，那行 `>= 25` 是**無法被測出來的**——變異測試實測把它放寬成
    `>= 0` 時，全部 11 支照樣綠。理由很直接：真實的 `_project_files()` 本來就回
    幾十個檔案，所以下限在正常情況下永遠成立，改不改都看不出差別。而下限存在的
    唯一理由正是「不正常的那一天」：抽不到檔案時 `offenders` 是空的，測試會通過，
    輸出跟「全專案乾淨」一模一樣。
    """
    monkeypatch.setattr(sys.modules[__name__], "_project_files", lambda: [])
    with pytest.raises(AssertionError) as excinfo:
        test_nothing_swallows_cancellation()
    assert "抽取器壞了" in str(excinfo.value), excinfo.value


def test_the_reraising_exemption_is_actually_being_used():
    """範圍的 pin：真的有 handler 是靠「原樣重拋」這條放行的。

    真實資料乾淨時，「有這條放行」與「沒有這條放行」的違規清單都是空的——兩者
    在輸出上分不出來。所以直接斷言它今天**有**在作用：拿掉那條放行時，這支會紅。
    """
    trees = [(path.name, ast.parse(path.read_text(encoding="utf-8"), str(path)))
             for path in _project_files()]
    reraisers = []
    for module_name, tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                if not any(n in ("<bare>", "BaseException")
                           for n in _handler_names(handler)):
                    continue
                if _reraises_unconditionally(handler):
                    reraisers.append(f"{module_name}:{handler.lineno}")
    assert reraisers, (
        "全專案沒有任何「攔 BaseException 再原樣重拋」的 handler，"
        "`_reraises_unconditionally` 這條放行現在等於死碼——"
        "要嘛有人把它改掉了，要嘛掃描器壞了。")


@pytest.mark.parametrize("body, swallows, why", [
    ("pass", True, "什麼都不做就走到尾端＝吞掉"),
    ("print('x')", True, "只記一行然後走到尾端，一樣是吞掉"),
    ("return None", True, "`return` 是最直接的吞法"),
    ("raise RuntimeError('boom')", True,
     "換一個例外拋出去：取消訊號一樣不見了，所以**必須是裸的** raise"),
    ("raise", False, "原樣重拋＝沒吞"),
    ("kill()\n        raise", False, "先做同步收尾再原樣重拋（正是 dorossi 那個形狀）"),
    ("try:\n            kill()\n        except Exception:\n            pass\n"
     "        raise", False,
     "巢狀 try 只管它自己 try 區塊裡的例外，吞不掉外層那個"),
    ("for x in y:\n            if x:\n                break\n        raise", False,
     "`break` 被 handler 自己的迴圈綁住，沒有逃出 handler"),
    ("if x:\n            return\n        raise", True,
     "有一條出口是 `return`——最後一行是 raise 不代表每條路都是"),
])
def test_the_scanner_tells_swallowing_apart_from_reraising(body, swallows, why):
    """合成對照組：真實資料乾淨，所以每一種形狀都得自己造。

    少了這一族，把 `_reraises_unconditionally` 改成「永遠回 True」會全綠——那等於
    整道守門被關掉，而輸出上看不出任何差別。
    """
    source = f"try:\n    work()\nexcept BaseException:\n        {body}\n"
    hits = _cancellation_swallowers([("synthetic.py", ast.parse(source))])
    assert bool(hits) is swallows, f"{why}；掃描器給的是 {hits}"


# 「名字像在找一堆東西」的判準。刻意只認列舉型函式：只影響單一值的函式回
# `None` 通常還有別的線索，而**列舉**的空集合最容易被讀成「就是沒有」。
_ENUMERATING_TOKENS = ("find_", "_find", "scan", "list_", "_all_", "gather",
                       "collect", "search", "discover", "enumerate")

# 已知且暫時接受的既有案例。每一筆都要寫「失敗會被誤讀成什麼」——
# 這份清單的用途是**擋住第六筆**，不是宣告這五筆沒問題。
_ALLOWED_SILENT_SCANS = {
    "_webrunner_shared.py:find_browser_pids_for_profile":
        "掃不成會被讀成「沒有殘留的瀏覽器」→ 少清一輪。屬 webrunner 領域。",
    "_gui_control.py:list_window_layouts":
        "讀不到會被讀成「沒有存過版面」。使用者看得出不對勁、重試即可，不會"
        "造成錯誤的自動化決策。",
    "_gui_control.py:list_macros":
        "同上，讀不到會被讀成「沒有存過巨集」。",
    "discord_bot.py:_gather_output_images":
        "掃不成會被讀成「還沒有任何圖」，影響 `/rate` 的顯示；不會驅動任何"
        "破壞性動作。",
}


def _swallows(handler: ast.ExceptHandler) -> bool:
    """這個 handler 有沒有把失敗吞掉——沒 raise、也沒自己 return。"""
    return not any(isinstance(n, (ast.Raise, ast.Return))
                   for n in ast.walk(handler))


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    return any(n.id in ("Exception", "BaseException", "OSError")
               for n in ast.walk(handler.type) if isinstance(n, ast.Name))


def _silent_enumerators(tree: ast.AST) -> list[tuple[str, int]]:
    """`[(函式名, 行號), …]`——回容器、名字像列舉、而且會吞掉失敗的函式。"""
    out: list[tuple[str, int]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(tok in fn.name.lower() for tok in _ENUMERATING_TOKENS):
            continue
        if fn.returns is None:
            continue
        ret = ast.unparse(fn.returns)
        if not any(k in ret for k in ("list", "set", "dict", "tuple")):
            continue
        # 只看**這個函式自己**的 handler，不要把巢狀函式的算進來——
        # 變數的作用域是函式區域的，靜態分析的範圍也必須跟著對齊。
        nested = {id(n) for f in ast.walk(fn)
                  if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and f is not fn
                  for n in ast.walk(f)}
        if any(isinstance(n, ast.ExceptHandler) and id(n) not in nested
               and _is_broad(n) and _swallows(n) for n in ast.walk(fn)):
            out.append((fn.name, fn.lineno))
    return out


@pytest.mark.parametrize("path", _project_files(), ids=lambda p: p.name)
def test_a_failed_enumeration_is_not_reported_as_an_empty_one(path):
    """列舉函式把失敗吞掉之後回空集合——呼叫端分辨不出「沒找到」與「沒掃成」。

    2026-09-07 實測過這個形狀的代價。`_find_all_chrome_processes` 原本就是
    `except Exception: print(到 stderr)` 之後照樣 `return found`。於是「psutil
    列舉一開始就炸、機器上其實有 3 個 chrome」與「機器真的很乾淨」送給使用者的
    訊息**一字不差**——都什麼都不說、什麼都不殺。使用者以為清乾淨了，而症狀要到
    下一次啟動瀏覽器撞上殘留的 singleton lock 才出現，換了時間也換了地方。

    **錯誤有進 log 不算數。** 那一版就有 `print(..., file=sys.stderr)`，而它擋不住
    任何事：做決定的是呼叫端，而呼叫端拿到的只有那個空集合。**要讓呼叫端做對事，
    訊號就必須走在回傳值上。**

    修法是回 `(結果, 這次問得完不完整)`。丟例外也可以，但那會逼每個呼叫端都處理
    ——對「盡力而為」的清掃來說通常過頭了。

    界線要畫對：**單一項目在列舉途中消失是常態，不算掃描失敗**（行程會結束、
    檔案會被刪）。把它算成失敗的話警告會天天出現，而天天出現的警告等於沒有警告。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    offenders = [
        f"{path.name}:{name}（第 {lineno} 行）"
        for name, lineno in _silent_enumerators(tree)
        if f"{path.name}:{name}" not in _ALLOWED_SILENT_SCANS
    ]
    assert not offenders, (
        f"這些列舉函式把失敗吞掉之後回空集合，呼叫端分辨不出「沒找到」與"
        f"「沒掃成」：{offenders}。改成連同「這次問得完不完整」一起回傳"
        f"（範本：`_process_control._find_all_chrome_processes`），"
        f"或列進 `_ALLOWED_SILENT_SCANS` 並寫清楚失敗會被誤讀成什麼。")


def test_the_silent_scan_allow_list_has_no_stale_entries():
    """清單裡的每一筆都要還存在——否則它只是在放行一個不存在的東西。

    比「多守一點」更重要的是**清單不能變成擺設**：函式改名或修好之後那一筆會
    永遠留著，下一個剛好同名的新函式就自動被放行了。
    """
    live = set()
    for path in _project_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for name, _lineno in _silent_enumerators(tree):
            live.add(f"{path.name}:{name}")
    stale = sorted(set(_ALLOWED_SILENT_SCANS) - live)
    assert not stale, (
        f"這幾筆豁免已經不對應任何實際的函式（改名了、或已經修好了）："
        f"{stale}。請從 `_ALLOWED_SILENT_SCANS` 刪掉。")


def test_the_silent_scan_detector_catches_the_shape_it_was_written_for():
    """偵測器自己的 canary。

    這一支在修好之後的 repo 上永遠是綠的，所以完全不能證明它有在做事。
    這裡餵合成原始碼：三種**該抓**的與三種**不該抓**的。
    """
    bad = {
        "吞掉失敗回部分結果": (
            "def _find_things() -> list[int]:\n"
            "    found = []\n"
            "    try:\n"
            "        for x in stuff():\n"
            "            found.append(x)\n"
            "    except Exception as error:\n"
            "        print(error)\n"
            "    return found\n"),
        "bare except": (
            "def scan_things() -> set[int]:\n"
            "    out = set()\n"
            "    try:\n"
            "        out.add(1)\n"
            "    except:\n"
            "        pass\n"
            "    return out\n"),
        "OSError 也算寬": (
            "def list_files() -> list[str]:\n"
            "    out = []\n"
            "    try:\n"
            "        out.append('a')\n"
            "    except OSError:\n"
            "        pass\n"
            "    return out\n"),
    }
    good = {
        "失敗會往上丟": (
            "def _find_things() -> list[int]:\n"
            "    try:\n"
            "        return list(stuff())\n"
            "    except Exception:\n"
            "        raise\n"),
        "handler 自己 return，呼叫端分得出來": (
            "def _find_things() -> tuple[list[int], bool]:\n"
            "    found = []\n"
            "    try:\n"
            "        for x in stuff():\n"
            "            found.append(x)\n"
            "    except Exception:\n"
            "        return found, False\n"
            "    return found, True\n"),
        "不是列舉型的名字": (
            "def read_one_value() -> dict:\n"
            "    out = {}\n"
            "    try:\n"
            "        out = load()\n"
            "    except Exception:\n"
            "        pass\n"
            "    return out\n"),
        "巢狀函式的 handler 不算在外層頭上": (
            "def _find_things() -> list[int]:\n"
            "    def helper():\n"
            "        try:\n"
            "            go()\n"
            "        except Exception:\n"
            "            pass\n"
            "    return [helper()]\n"),
    }
    for label, src in bad.items():
        assert _silent_enumerators(ast.parse(src)), f"該抓卻沒抓到：{label}"
    for label, src in good.items():
        assert not _silent_enumerators(ast.parse(src)), f"誤報了：{label}"


def test_the_scan_actually_finds_something():
    """守門的自我檢查：確認掃描真的看到了 `try`，而不是靜靜地掃了個空。

    這類靜態守門最典型的失效方式是「路徑或解析壞掉 → 掃到 0 個目標 → 永遠綠」。
    """
    total = sum(len(list(_tries(p))) for p in _project_files())
    assert total > 200, f"只掃到 {total} 個 try——掃描本身可能壞了"
    assert _resolve("OSError") is OSError
    assert _resolve("asyncio.CancelledError") is not None
    assert _resolve("dorossi_backend._DorossiUsageLimitError") is None, (
        "解析範圍應該只涵蓋內建與白名單模組")


# ---------------------------------------------------------------------------
# 範圍本身的釘子（§8.8(A3)：族群乾淨時，寬範圍與窄範圍在輸出上一模一樣）
# ---------------------------------------------------------------------------

_REPO_ROOT_SCRIPTS = ("start_discord_bot.py", "start_webrunner.py",
                      "run_batch.py", "install_autostart.py",
                      "wake_autostart.py")
_REPO_ROOT_HANDLER_FLOOR = 15


def _repo_root_handler_count(names=None) -> int:
    total = 0
    for name in (_REPO_ROOT_SCRIPTS if names is None else names):
        path = PKG_ROOT.parent / name
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), name)
        total += sum(len(n.handlers) for n in ast.walk(tree)
                     if isinstance(n, ast.Try))
    return total


def test_the_scan_reaches_a_brand_new_repo_root_script(tmp_path):
    """釘住「範圍是算出來的」，不只是「今天的答案剛好對」。

    repo root 那 18 個 handler 今天零違規，所以把 `_project_files()` 縮回
    `PKG_ROOT.rglob` 之後整個檔案照樣全綠。唯一分辨得出來的辦法是餵它一個
    **任何寫死的清單都不可能有**的名字。
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "in_package.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "brand_new_supervisor.py").write_text("y = 2\n",
                                                      encoding="utf-8")
    names = {p.name for p in _project_files(pkg_root=pkg, repo_root=tmp_path)}
    assert "brand_new_supervisor.py" in names, (
        f"repo root 的新腳本沒被掃到（看到的是 {sorted(names)}）——範圍被縮回"
        "只掃套件了，而兩支監督者就住在那裡。")
    assert "in_package.py" in names, "套件那一半也不見了"


def test_the_scan_really_covers_the_supervisors():
    """族群 pin：這條規則保護的對象裡，最重要的兩個住在 repo root。

    刻意**不共用** `_project_files()` 去數 handler——範圍釘樁跟被釘的東西共用
    列舉器的話，只會證明它等於它自己（§8.8(A6)）。
    """
    scanned = {p.name for p in _project_files()}
    missing = [n for n in _REPO_ROOT_SCRIPTS if n not in scanned]
    assert not missing, f"這些 repo root 腳本不在掃描範圍裡：{missing}"
    handlers = _repo_root_handler_count()
    assert handlers >= _REPO_ROOT_HANDLER_FLOOR, (
        f"repo root 只剩 {handlers} 個 except handler（下限 "
        f"{_REPO_ROOT_HANDLER_FLOOR}）——族群縮了，這幾支測試可能已經沒有在保護"
        "任何東西（2026-09-10 實測 18 個）。")


def test_the_supervisor_population_pin_fires_when_the_population_is_empty(
        monkeypatch):
    """控制組：族群下限在真實資料上永遠成立，所以它自己量不出來。

    §8.8(A4)：把前提直接打壞（餵一份不存在的腳本清單），下限必須紅——而且要紅在
    **族群那一句**上，不是紅在前面的 `missing` 那一句。所以這裡直接呼叫計數器，
    不走整支測試。
    """
    assert _repo_root_handler_count(names=("__no_such_script__.py",)) == 0
    monkeypatch.setattr(sys.modules[__name__], "_repo_root_handler_count",
                        lambda names=None: 0)
    with pytest.raises(AssertionError) as excinfo:
        test_the_scan_really_covers_the_supervisors()
    assert "族群縮了" in str(excinfo.value), (
        f"紅的不是族群那一句，而是：{excinfo.value}")


# ---------------------------------------------------------------------------
# `# nosec` 必須指到一個**真的會在那一行觸發**的檢查
# ---------------------------------------------------------------------------
# 這幾支不是潔癖，理由跟 `_OWNER_ONLY_SLASH` 對帳、跟 `_ALLOWED_COMMAND_NAME_WORDS`
# 那次一模一樣：**一個抑制不到任何東西的豁免，讀起來卻像有人審過並接受了**，而它
# 失效的方向是 fail-open。
#
# B110（try/except/pass）在 bandit 1.9.4 預設組態下要**三個條件同時成立**才會觸發
# （直接讀 `bandit/plugins/try_except_pass.py` 抄的，不是從文件推的）：
#   1. handler 主體只有一個陳述；
#   2. 那個陳述是 `pass`；
#   3. handler 型別是**裸的**或**恰好是名字 `Exception`**
#      （`getattr(node.type, "id", None) != "Exception"` → tuple 沒有 `.id`，
#       所以 `except (CancelledError, Exception):` 不算）。
# B112（try/except/continue）的實作逐字相同，只差主體要是 `continue`。
#
# ⚠️ **危險在於它對準的正好是那一次編輯。** 把 `except OSError:` 放寬成
# `except Exception:` 正是讓 B110 第一次成立的改動——而那一刻，這個早就躺在那裡、
# 今天什麼都沒做的 `# nosec B110` 會讓掃描器**安靜下來**，而不是說話。今天無害的
# 註解，在最需要它出聲的那一刻變成消音器。
#
# ⚠️ 還有更糟的一種：**編號寫錯**。`except Exception: continue` 掛 `# nosec B110`
# 抑制不到任何東西，而真正會報的 B112 一路穿過去——寫的人以為處理掉了，掃描結果
# 裡那一筆卻還在。2026-09-12 實際抓到兩筆（`discord_bot.py` 的兩處掃描迴圈）。
#
# 2026-09-12 全專案量到：活的 B110 抑制 73 筆、死的 18 筆
# （10 筆型別是 `OSError`、4 筆主體是 `return`、2 筆是 `continue`、
#   1 筆型別是 tuple、1 筆主體是一般陳述）。
#
# 判準走 **`tokenize` 的 COMMENT token**，不是逐行子字串：本檔自己的合成語料裡就
# 寫著 `# nosec B110`，那是**字串常數**不是註解，逐行掃會把測試自己抓出來
# （同一個坑 `test_text_encoding` 才剛踩過：說明文字自己滿足自己）。走 token 之後
# 範圍反而可以放到全專案——測試檔裡真的有三筆 `# nosec B110`，而且都是活的。
#
# **刻意不管沒有編號的裸 `# nosec`**（抑制一切）。那是另一條規則，而且全專案目前
# 零筆；現在寫下來只會是一個永遠不會執行到的分支。

_NOSEC_RE = re.compile(r"#\s*nosec\b(.*)$")
_NOSEC_ID_RE = re.compile(r"\bB\d{3}\b")
# 反引號裡的東西是**引用**，不是指令。走 `tokenize` 已經擋掉字串常數裡的同樣文字，
# 但擋不掉散文**註解**引用它自己——而本段上面那幾行說明就寫著 `` `# nosec B110` ``。
# 2026-09-12 第一版少了這一步，守門當場把自己的說明文字抓出來報了三筆。
# 這正是本專案反覆踩的那個形狀（「解釋規則的註解一直匹配到規則自己」），而這次是
# 在寫那條規則的當下踩的。判準用反引號是因為這個 repo 的慣例就是「程式碼引用一律
# 加反引號」——真正的指令不會被反引號包起來。
_BACKTICKED_RE = re.compile(r"`[^`]*`")

# bandit 的 handler 類檢查：編號 → 主體必須是哪一種陳述。
_HANDLER_CHECK_BODY = {"B110": ast.Pass, "B112": ast.Continue}


def _nosec_comments(source: str, filename: str) -> dict[int, list[str]]:
    """行號 → 那一行 `# nosec` 註解裡列出的編號。

    只認 `tokenize.COMMENT`。字串常數裡的同樣文字**不算**——本檔的合成語料就是
    那種形狀。
    """
    out: dict[int, list[str]] = {}
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as error:
        pytest.fail(f"{filename} 無法 tokenize：{error!r}")
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        m = _NOSEC_RE.search(_BACKTICKED_RE.sub("", tok.string))
        if not m:
            continue
        ids = _NOSEC_ID_RE.findall(m.group(1))
        if ids:
            out.setdefault(tok.start[0], []).extend(ids)
    return out


def _handler_check_fires(handler: ast.ExceptHandler, check_id: str) -> bool:
    """bandit 的 B110／B112 會不會在這個 handler 上觸發（預設組態）。"""
    body_type = _HANDLER_CHECK_BODY[check_id]
    if len(handler.body) != 1:
        return False
    if (handler.type is not None
            and getattr(handler.type, "id", None) != "Exception"):
        return False
    return isinstance(handler.body[0], body_type)


def _why_handler_check_cannot_fire(handler: ast.ExceptHandler,
                                   check_id: str) -> str:
    if (handler.type is not None
            and getattr(handler.type, "id", None) != "Exception"):
        shape = ("型別是 tuple" if isinstance(handler.type, ast.Tuple)
                 else f"型別是 `{ast.unparse(handler.type)}`")
        return (f"{shape}——bandit 預設只認裸的 `except:` 或恰好 `Exception`，"
                "所以這個編號在這一行永遠不會觸發")
    if len(handler.body) != 1:
        return f"主體有 {len(handler.body)} 個陳述，bandit 只看單一陳述的 handler"
    got = type(handler.body[0]).__name__
    hint = ""
    for other, body_type in _HANDLER_CHECK_BODY.items():
        if other != check_id and isinstance(handler.body[0], body_type):
            hint = f"（主體是 `{body_type.__name__.lower()}`，你要的多半是 {other}）"
    need = _HANDLER_CHECK_BODY[check_id].__name__.lower()
    return f"主體是 `{got}` 不是 `{need}`{hint}"


def _dead_nosec_ids(source: str, filename: str) -> list[str]:
    """那一行的 `# nosec` 編號在那一行結構上不可能觸發。"""
    tree = ast.parse(source, filename)
    handlers = {h.lineno: h for h in ast.walk(tree)
                if isinstance(h, ast.ExceptHandler)}
    problems: list[str] = []
    for lineno, ids in sorted(_nosec_comments(source, filename).items()):
        for check_id in ids:
            if check_id not in _HANDLER_CHECK_BODY:
                continue  # 這一支只模型化 handler 類的檢查
            handler = handlers.get(lineno)
            if handler is None:
                problems.append(
                    f"{filename}:{lineno} 掛著 `# nosec {check_id}`，"
                    "但這一行不是 `except` 子句——那個編號只會在 handler 上觸發")
            elif not _handler_check_fires(handler, check_id):
                problems.append(
                    f"{filename}:{lineno} 的 `# nosec {check_id}` 抑制不到任何"
                    f"東西：{_why_handler_check_cannot_fire(handler, check_id)}")
    return problems


@pytest.mark.parametrize("path", _project_files(), ids=lambda p: p.name)
def test_no_nosec_names_a_check_that_cannot_fire_there(path: Path):
    """死掉的 `# nosec` 會在最需要它出聲的那一刻變成消音器。"""
    problems = _dead_nosec_ids(path.read_text(encoding="utf-8"), path.name)
    assert not problems, (
        "\n".join(problems)
        + "\n\n這些抑制今天什麼都沒抑制到，但它們**讀起來像有人審過**。"
        "\n更糟的是時機：把 `except OSError:` 放寬成 `except Exception:` 正是讓"
        "B110 第一次成立的那一次編輯，而那一刻這個註解會讓掃描器安靜下來。"
        "\n把編號拿掉（其餘編號與說明文字保留）；真的要抑制就先確認編號對得上。")


def test_the_nosec_scan_actually_sees_the_live_suppressions():
    """正面對照組：違規數本來就該是 0，拿 0 當證據等於沒有證據。

    數的是**活的** handler 類抑制總數。2026-09-12 量到 73 筆（B110），下限給 40。
    認不出註解的掃描器（例如有人把 `tokenize` 換成不會匹配的東西），違規數會永遠
    是 0 而且看起來完全正常。
    """
    live = 0
    for path in _project_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, path.name)
        handlers = {h.lineno: h for h in ast.walk(tree)
                    if isinstance(h, ast.ExceptHandler)}
        for lineno, ids in _nosec_comments(source, path.name).items():
            for check_id in ids:
                if check_id not in _HANDLER_CHECK_BODY:
                    continue
                handler = handlers.get(lineno)
                if handler is not None and _handler_check_fires(handler, check_id):
                    live += 1
    assert live >= 40, (
        f"只認出 {live} 筆活的 `# nosec` handler 抑制（2026-09-12 量到 73 筆）。"
        "認不出東西的掃描器，違規數永遠是 0。")


_NOSEC_CASES = {
    # 必抓：五種死法，各對應判準裡的一個條件。
    "型別是具名例外": (
        "import os\n"
        "def f():\n"
        "    try:\n"
        "        os.unlink('x')\n"
        "    except OSError:  # nosec B110\n"
        "        pass\n", True),
    "型別是 tuple": (
        "import asyncio, os\n"
        "def f():\n"
        "    try:\n"
        "        os.unlink('x')\n"
        "    except (asyncio.CancelledError, Exception):  # nosec B110\n"
        "        pass\n", True),
    "主體是 return": (
        "import os\n"
        "def f():\n"
        "    try:\n"
        "        os.unlink('x')\n"
        "    except Exception:  # nosec B110\n"
        "        return None\n", True),
    "編號寫錯（continue 該用 B112）": (
        "import os\n"
        "def f():\n"
        "    for _ in range(3):\n"
        "        try:\n"
        "            os.unlink('x')\n"
        "        except Exception:  # nosec B110\n"
        "            continue\n", True),
    "掛在不是 handler 的行上": (
        "import os\n"
        "def f():\n"
        "    os.unlink('x')  # nosec B110\n", True),
    # 必放行。**這幾筆才是重點**：判準裡每一道條件都是收緊步驟，只有近似案例殺得死。
    "活的 B110": (
        "import os\n"
        "def f():\n"
        "    try:\n"
        "        os.unlink('x')\n"
        "    except Exception:  # nosec B110\n"
        "        pass\n", False),
    "活的 B110（裸 except）": (
        "import os\n"
        "def f():\n"
        "    try:\n"
        "        os.unlink('x')\n"
        "    except:  # nosec B110\n"
        "        pass\n", False),
    "活的 B112": (
        "import os\n"
        "def f():\n"
        "    for _ in range(3):\n"
        "        try:\n"
        "            os.unlink('x')\n"
        "        except Exception:  # nosec B112\n"
        "            continue\n", False),
    # 本支只模型化 handler 類的檢查；其他編號一律不猜，否則會對
    # `# nosec B603` 這種完全正當的抑制亂叫。
    "沒有模型化的編號": (
        "import subprocess\n"
        "def f(args):\n"
        "    subprocess.run(args, check=False)  # nosec B603 B607\n", False),
    # ⚠️ **散文註解引用這條規則不算。** 這一筆是 2026-09-12 寫這道守門的當下真的
    # 踩到的：上面那段說明裡寫著加了反引號的 nosec 編號，第一版把自己的說明文字
    # 報了三筆。少了它，把反引號那一步刪掉不會有人發現——直到下一個人在註解裡提到
    # 這個編號，而那正是寫這種規則的人一定會做的事。
    "散文註解裡用反引號引用": (
        "import os\n"
        "def f():\n"
        "    # 為什麼不掛 `# nosec B110`：型別是具名的，根本不會觸發。\n"
        "    try:\n"
        "        os.unlink('x')\n"
        "    except OSError:\n"
        "        pass\n", False),
    # 同一行的 COMMENT token 裡先有別的工具指令，再接 nosec——真實寫法。
    "前面還掛著 pylint 指令": (
        "import os\n"
        "def f():\n"
        "    try:\n"
        "        os.unlink('x')\n"
        "    except Exception:  # pylint: disable=broad-except  # nosec B110\n"
        "        pass\n", False),
    # 字串常數裡的同樣文字不算——本檔自己就是這個形狀。
    "只是字串裡提到": (
        "SAMPLE = '''\n"
        "    except OSError:  # nosec B110\n"
        "        pass\n"
        "'''\n", False),
}


@pytest.mark.parametrize("label", sorted(_NOSEC_CASES))
def test_the_nosec_scan_tells_the_shapes_apart(label):
    """合成語料：該抓的抓到、不該抓的放行。"""
    source, should_catch = _NOSEC_CASES[label]
    got = _dead_nosec_ids(source, f"{label}.py")
    if should_catch:
        assert got, f"「{label}」應該被抓到，卻放行了"
    else:
        assert not got, f"「{label}」不該被抓，卻報了：{got}"


def test_the_wrong_id_case_says_which_id_was_meant():
    """編號寫錯時，訊息要直接講出正確的編號。

    這一支釘的是**訊息內容**不是判定結果：`except Exception: continue` 掛
    `# nosec B110` 會被上面那支抓到，但如果錯誤訊息只說「抑制不到任何東西」，
    讀的人最可能的下一步是把註解刪掉——而那一行**真的**有一個 B112 在報。
    正確的下一步是改編號，訊息必須說得出來。
    """
    source, _ = _NOSEC_CASES["編號寫錯（continue 該用 B112）"]
    problems = _dead_nosec_ids(source, "wrong_id.py")
    assert problems and "B112" in problems[0], (
        f"訊息裡沒有點名 B112：{problems}")


def test_the_repo_root_script_list_is_still_complete():
    """`_REPO_ROOT_SCRIPTS` 是寫死的四個名字，而寫死的清單 **fail-open**。

    repo root 多一支正式腳本時，這裡不會有任何症狀：掃描照跑、下限照過，新腳本的
    handler 一個都沒被數到，而「這條規則涵蓋 repo root」這句話仍然讀起來像真的。
    這是 `CLAUDE.md` 記載過好幾次的同一個形狀（`_OWNER_ONLY_SLASH`、
    `_CROSS_PROCESS_CONSTANTS`、`_pid_alive` 的列舉），所以兩個方向都對帳。

    形狀守門看不到這一筆——`test_suite_safety.test_no_scanner_narrows_a_python_glob_by_filename`
    掃的是 glob 樣式，而這裡根本沒有 glob。列舉要靠列舉自己的對帳。

    ⚠️ repo root 放一支臨時腳本會讓這一支變紅。那是刻意的：一次性的探測腳本本來
    就該住在 repo 外面（同 `test_text_encoding` 的 rglob）。
    """
    root = PKG_ROOT.parent
    actual = {p.name for p in root.glob("*.py")
              if not p.stem.lstrip("_").startswith("test_")}
    assert actual, "repo root 一支 `.py` 都沒掃到——抓法過期了"
    missing = sorted(actual - set(_REPO_ROOT_SCRIPTS))
    stale = sorted(set(_REPO_ROOT_SCRIPTS) - actual)
    assert not missing and not stale, (
        f"`_REPO_ROOT_SCRIPTS` 對不上 repo root 的實況：漏列 {missing}、"
        f"列了但不存在 {stale}。漏列那一邊是 fail-open——那支腳本的 `except` "
        "從來沒有被這一族守門看過。")
# --------------------------------------------------------------------------
# `_SURVIVES_CATCH_ALL` 的專案自訂項目，兩個方向都要對得上
# --------------------------------------------------------------------------

def _class_bases_across_project() -> dict[str, list[str]]:
    """全專案的 `{類別名: [base 的原始字串]}`（同名的以先掃到的為準）。"""
    table: dict[str, list[str]] = {}
    for path in _project_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                table.setdefault(node.name, [ast.unparse(b) for b in node.bases])
    return table


def _ancestor_names(name: str, table: dict[str, list[str]]) -> list[str]:
    """順著 `table` 把 `name` 的祖先攤平成一串名字（跟不到的 base 就停在那裡）。"""
    chain: list[str] = []
    cursor, guard = list(table.get(name, [])), 0
    while cursor and guard < 20:
        guard += 1
        chain.extend(cursor)
        cursor = [nxt for base in cursor for nxt in table.get(base, [])]
    return chain


def _escapes_a_catch_all(name: str, table: dict[str, list[str]]) -> bool:
    chain = _ancestor_names(name, table)
    return "BaseException" in chain and "Exception" not in chain


@pytest.mark.parametrize(
    "name", sorted(_SURVIVES_CATCH_ALL - _STDLIB_CATCH_ALL_SURVIVORS))
def test_a_hand_written_survivor_really_escapes_a_catch_all(name: str):
    """豁免清單裡專案自己寫的那些，必須真的逃得過 `except Exception`。

    這份清單被讀兩次，而其中一次是**放行**：`except _X` 排在 `except Exception`
    後面時，只要 `_X` 在清單裡就不算死碼。所以清單裡的名字哪天改成繼承
    `Exception`（或者整個類別被刪掉、改名），那個放行會**繼續生效而沒有任何
    症狀**——真正的死碼從此看不見，而守門看起來照常在跑。

    本專案已經在 `_OWNER_ONLY_SLASH`、`.tmp` 姊妹檔清單、`_pid_alive` 列舉上各踩
    過一次同一個形狀，所以這裡兩個方向都對：名字必須找得到，而且找到的那個類別
    必須真的只繼承 `BaseException`。
    """
    table = _class_bases_across_project()
    assert name in table, (
        f"`{name}` 在 `_SURVIVES_CATCH_ALL` 裡，但全專案找不到這個類別宣告"
        "——它可能被改名或刪掉了，而那筆豁免會繼續放行一個不存在的名字。")
    assert _escapes_a_catch_all(name, table), (
        f"`{name}` 的繼承鏈是 {_ancestor_names(name, table)}；"
        "它逃不過 `except Exception`，所以那筆豁免現在是在遮蔽真正的死碼。")


@pytest.mark.parametrize("bases, escapes", [
    (["BaseException"], True),
    (["Exception"], False),
    (["RuntimeError"], False),
    ([], False),
])
def test_the_survivor_check_can_tell_the_two_shapes_apart(bases, escapes):
    """上面那支的對照組：真實資料全乾淨時，它測不出自己還會不會動。

    `RuntimeError` 那一格是近似案例——名字結尾就是 `Error`，一個只看名字的判準
    會把它當成「逃得過」，而它其實在 `Exception` 底下。
    """
    table = {"_Sentinel": bases, "RuntimeError": ["Exception"],
             "Exception": ["BaseException"]}
    assert _escapes_a_catch_all("_Sentinel", table) is escapes
def test_the_survivor_reconciliation_has_something_to_check():
    """對照組的對照組：空的 parametrize 在 pytest 裡長得像「跳過」，不像「壞掉」。

    上面那支是對真實資料跑的，而它的輸入是一個減法。把
    `_STDLIB_CATCH_ALL_SURVIVORS` 寫寬一點（或者把兩筆專案自訂項目拿掉），減出來
    就是空集合——parametrize 變成一個 `SKIPPED`，整份報告仍然是綠的。本專案在
    `_banned_words_in` 的豁免減法上剛踩過一模一樣的形狀。
    """
    assert _STDLIB_CATCH_ALL_SURVIVORS <= _SURVIVES_CATCH_ALL
    project_owned = _SURVIVES_CATCH_ALL - _STDLIB_CATCH_ALL_SURVIVORS
    assert len(project_owned) >= 2, (
        f"專案自訂的豁免只剩 {sorted(project_owned)}；上面那支對帳沒有東西可以驗，"
        "而它會以「跳過」的樣子安靜消失。")
