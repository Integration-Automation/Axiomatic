"""`webrunner.pid` 的讀取端：把關的那幾支必須給出**同一個**答案。

這個檔是跨行程協調的唯一存活訊號，而讀它的地方散在六個模組裡，每一處都自己寫了
一次讀取邏輯。`discord_bot._load_pid` 的 docstring 把規則寫得很清楚（三分法：
檔案不存在／讀得出來／讀不出來，最後一種**不得刪檔**），磁碟契約
§5.2 也抄了一份。**但那條規則從來沒有被對拉過**——四支（實際上不只四支）各有各的
測試、各自全綠，而「兩份各自綠的實作，中間沒有任何東西在比較它們」正是本 repo
記過一次的坑。

2026-09-21 第一次把它們放到同一組語料上，量到兩件事：

1. **清單本身早就過期了。** 三個 docstring 加一節架構文件都寫著「四個獨立的
   讀取端」，而實際去掃，產品碼裡讀這個檔的函式有 **八個**。多出來的四個不是
   缺陷——每一個都寫了理由說明自己為什麼刻意不用三分法（唯讀前置檢查、認領存活
   訊號、兩支擁有權比對）——錯的是那句「第五個要遵守同樣的三分法」：第五、六、
   七、八個早就在了，而且都**不**遵守，還沒有任何人被要求回來想一次。
2. **四支三分法讀取端裡有一支真的不一樣。** 檔案在 `exists()` 與 `read_text()`
   之間被別人刪掉時，`verify_browser` / `start_webrunner` / `dashboard_server`
   三支都回「等同不存在」，只有 `discord_bot._load_pid` 回「判不出來」——而它自己
   docstring 的那張表第一列寫的就是「檔案不存在 → `(None, True)`」。後果是
   `_webrunner_liveness()` 回 `(True, False)`，bot 於是在批次剛結束的那一瞬間拒絕
   `/run`，說有東西在跑。

所以這支測試守三件事：**清單不得過期**（推導 vs 登記，雙向）、**四支的答案要
一致**（同一組語料）、**唯讀的那幾支不得刪檔**。
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "axiomatic"
REPO_ROOT = PACKAGE_ROOT.parent
for _p in (str(PACKAGE_ROOT), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dashboard_server as ds  # noqa: E402
import discord_bot as b  # noqa: E402
import verify_browser as vb  # noqa: E402


def _load_launcher():
    """把 `start_webrunner.py` 當模組載進來（不執行 `main()`）。

    與 `test_supervisor._load_launcher` 同一個做法：它在 repo root、不是套件的一
    部分，所以只能用 importlib 從路徑載。
    """
    path = REPO_ROOT / "start_webrunner.py"
    spec = importlib.util.spec_from_file_location("start_webrunner", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sw = _load_launcher()


# ---------------------------------------------------------------------------
# 1. 清單不得過期：推導出來的讀取端 vs 登記在案的
# ---------------------------------------------------------------------------

# 把關用的三分法讀取端。共同契約：回 `(pid, 判定得出來嗎)`，而且
#   * 檔案不存在（含「讀的瞬間才剛被刪掉」）→ 判定得出來
#   * 讀不出來／解析不出來 → 判不出來，而且**不得刪檔**
_THREE_WAY_READERS = {
    ("verify_browser.py", "_live_webrunner_pid"),
    ("start_webrunner.py", "_live_webrunner_pid"),
    ("discord_bot.py", "_load_pid"),
    ("dashboard_server.py", "_read_pid"),
}

# 讀同一個檔、但**刻意不用**三分法的。每一筆都要寫理由——這份清單的用途是逼人
# 在加第九個讀取端時回來想一次「我這支該不該是三分法」，理由欄空著就失去意義。
_NOT_THREE_WAY = {
    ("run_batch.py", "_bot_spawned_pid"):
        "唯讀前置檢查，不 spawn 任何東西。判不出來時樂觀往下跑是安全的，因為真正"
        "把關的是它交棒的 `start_webrunner.py` 子行程，那支持著 Chrome 槽會再判一"
        "次。函式自己的 docstring 寫了這個前提，也寫了前提失效時要改成三分法。",
    ("_webrunner_shared.py", "claim_liveness_signal"):
        "問的不是「有沒有批次在跑」而是「需不需要我來認領這個訊號」，所以回的是"
        "`int | None` 不是三分法的 tuple。判不出來時回 None（＝不認領），方向與三"
        "分法一致——誤判成「沒人在跑」會覆寫掉別人有效的訊號。",
    ("start_webrunner.py", "_clear_pid_if_ours"):
        "擁有權比對：問的是「檔案裡還記著我寫進去的那個 pid 嗎」，不是存活判定。"
        "讀不出來就不刪，本來就是保守的那一邊。",
    ("_webrunner_shared.py", "release_liveness_signal"):
        "同上，webrunner 那一側的擁有權比對。",
    ("mutation_harness.py", "_batch_is_running"):
        "開發工具，不在產品路徑上。讀不出來時回 False（＝允許變異），但它上面還有"
        "一層 `_refuse_if_a_live_batch_would_reload`，而且變異腳本本來就只在人盯著"
        "的時候跑。",
}

_ALL_DOCUMENTED = _THREE_WAY_READERS | set(_NOT_THREE_WAY)


def _pid_readers_in(tree: ast.AST) -> set[str]:
    """一棵樹裡「讀 `webrunner.pid`」的函式名。

    兩步：先找出所有被綁到 `… / "webrunner.pid"` 的名字（模組常數或區域變數都
    算——`mutation_harness` 用的就是區域變數），再找出哪些函式對那些名字呼叫了
    `read_text` / `read_bytes` / `open`。

    刻意用 AST 而不是 `"webrunner.pid" in source`：這個檔名在註解與 docstring 裡
    出現了幾十次，字串比對會把每一個講到它的函式都算成讀取端。
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.BinOp)
                and isinstance(node.value.op, ast.Div)):
            continue
        if "webrunner.pid" not in ast.unparse(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound.add(target.id)
    if not bound:
        return set()
    found: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in bound
                    and func.attr in ("read_text", "read_bytes", "open")):
                found.add(fn.name)
                break
    return found


def _production_sources() -> list[Path]:
    """產品碼：套件內 ＋ repo root 的 `.py`，扣掉測試檔本身。

    `test/`（本檔所在的目錄）照同一個判準過濾：`conftest.py` 在 2026-09-22 之前住在
    套件裡、在範圍內，搬家之後照舊。"""
    found = [p for p in (*sorted(PACKAGE_ROOT.glob("*.py")),
                         *sorted(Path(__file__).resolve().parent.glob("*.py")))
             if not p.name.startswith(("test_", "_test_"))]
    found += sorted(REPO_ROOT.glob("*.py"))
    return found


def _derived_readers() -> set[tuple[str, str]]:
    readers: set[tuple[str, str]] = set()
    for path in _production_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for name in _pid_readers_in(tree):
            readers.add((path.name, name))
    return readers


def test_every_pid_file_reader_is_classified():
    """掃得到的每一個讀取端都要登記在上面兩份清單的其中一份。

    方向一（推導 → 登記）：新增第九個讀取端而沒有在這裡分類，就會紅。這是這支
    測試存在的主要理由——原本的規則之書說「第五個要遵守同樣的三分法」，而第五到
    第八個早就在了，沒有任何人被要求回來想一次。
    """
    derived = _derived_readers()
    assert len(derived) >= 8, (
        f"只掃到 {len(derived)} 個讀取端，語料看起來沒吃到東西——"
        "推導失效時這支測試會變成空的比較，看起來跟通過一模一樣。")
    unclassified = sorted(derived - _ALL_DOCUMENTED)
    assert not unclassified, (
        f"這些函式讀了 `webrunner.pid`，但沒有被分類：{unclassified}。\n"
        "把它加進 `_THREE_WAY_READERS`（如果它在回答「我該不該啟動／讓位」）"
        "或 `_NOT_THREE_WAY`（並寫下理由）。")


def test_the_reader_registry_does_not_rot():
    """方向二（登記 → 推導）：清單裡不得留下已經改名或刪掉的函式。

    與 `_OWNER_ONLY_SLASH` 同一個形狀：一筆對不上任何東西的登記會安靜失效，而
    清單看起來照常在維護。
    """
    derived = _derived_readers()
    stale = sorted(_ALL_DOCUMENTED - derived)
    assert not stale, (
        f"這些登記在案的讀取端已經不讀 `webrunner.pid` 了：{stale}。"
        "從清單裡拿掉，或把讀取邏輯補回去。")


def test_every_deliberate_exception_states_its_reason():
    """`_NOT_THREE_WAY` 的理由欄不得留白。"""
    thin = sorted(key for key, why in _NOT_THREE_WAY.items()
                  if len(why.strip()) < 20)
    assert not thin, (
        f"這些例外沒有寫理由：{thin}。一份沒有理由的例外清單，下一個人只會照抄。")


def test_the_scan_sees_a_planted_reader():
    """對照組：推導真的在推導。

    合成一份語料，裡面有一支新的讀取端、一支只是**提到**這個檔名的函式，以及一
    支讀別的檔的。只有第一支該被找到。
    """
    source = (
        'PID = ROOT / "webrunner.pid"\n'
        'OTHER = ROOT / "batch_label.txt"\n'
        "def a_new_reader():\n"
        "    return PID.read_text(encoding='utf-8')\n"
        "def only_mentions_it():\n"
        '    """讀 webrunner.pid 的人要小心。"""\n'
        "    return 0\n"
        "def reads_something_else():\n"
        "    return OTHER.read_text(encoding='utf-8')\n"
    )
    assert _pid_readers_in(ast.parse(source)) == {"a_new_reader"}


def test_the_derivation_really_walks_the_corpus(tmp_path, monkeypatch):
    """`_derived_readers()` 要真的去走語料，不是把登記表抄一份回來。

    ⚠️ 變異實測（2026-09-21）：把 `_derived_readers()` 的本體換成
    `set(_ALL_DOCUMENTED)`，上面兩支雙向對帳**與那道 `>= 8` 的下限全部照樣綠**
    ——推導與登記一旦變成同一個來源，對帳就是自己跟自己比，而下限看到的是登記表的
    長度。那是本 repo 記過的形狀：**一個取代了某支函式的東西，測不到那支函式自己**。

    所以這一格把它**讀的語料**換掉，不是把它換掉：餵一個只有一支讀取端的合成目錄，
    斷言它回的是語料裡的那一支。抄登記表的實作會回九筆，當場紅。
    """
    fake = tmp_path / "some_module.py"
    fake.write_text(
        'PID = ROOT / "webrunner.pid"\n'
        "def a_reader():\n"
        "    return PID.read_text(encoding='utf-8')\n",
        encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_production_sources",
                        lambda: [fake])
    assert _derived_readers() == {("some_module.py", "a_reader")}


def test_the_scan_needs_the_binding_not_just_the_call():
    """對照組的另一半：沒有綁到這個檔名的 `read_text` 不算。"""
    source = (
        'PID = ROOT / "some_other.pid"\n'
        "def reader():\n"
        "    return PID.read_text(encoding='utf-8')\n"
    )
    assert _pid_readers_in(ast.parse(source)) == set()


# ---------------------------------------------------------------------------
# 2. 四支三分法讀取端，同一組語料，同一個答案
# ---------------------------------------------------------------------------

_LIVE_PID = 4242
_DEAD_PID = 4243


class _RaisesOnRead:
    """`exists()` 說有、`read_text()` 丟指定的例外。

    真的做不出來的狀態就用替身：檔案在 `exists()` 與 `read_text()` 之間被**別的
    行程**刪掉（TOCTOU），以及權限不足。兩種都不可能用 `tmp_path` 穩定重現。
    """

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.unlinked = 0

    def exists(self) -> bool:
        return True

    def read_text(self, encoding: str = "utf-8") -> str:
        raise self._error

    def unlink(self, missing_ok: bool = False) -> None:
        self.unlinked += 1


def _write(tmp_path: Path, payload: bytes) -> Path:
    target = tmp_path / "webrunner.pid"
    target.write_bytes(payload)
    return target


# (名稱, 怎麼做出這個狀態, 判定得出來嗎, 讀得到的 pid)
# 最後一欄是「如果這支不過濾死掉的行程，它會回什麼 pid」；None 代表沒有 pid。
_STATES = (
    ("檔案不存在", lambda tmp: tmp / "webrunner.pid", True, None),
    ("讀之前才剛被別人刪掉",
     lambda tmp: _RaisesOnRead(FileNotFoundError(2, "gone")), True, None),
    ("權限不足",
     lambda tmp: _RaisesOnRead(PermissionError(13, "denied")), False, None),
    ("內容不是合法 UTF-8",
     lambda tmp: _write(tmp, b"\xff\xfe\x00"), False, None),
    ("內容不是數字", lambda tmp: _write(tmp, b"not-a-pid"), False, None),
    ("空檔", lambda tmp: _write(tmp, b""), False, None),
    ("只有空白", lambda tmp: _write(tmp, b"   \r\n"), False, None),
    ("活著的 pid",
     lambda tmp: _write(tmp, str(_LIVE_PID).encode()), True, _LIVE_PID),
    ("死掉的 pid",
     lambda tmp: _write(tmp, str(_DEAD_PID).encode()), True, _DEAD_PID),
)


def _fake_alive(pid: int) -> bool:
    return pid == _LIVE_PID


def _ask_verify_browser(target, monkeypatch):
    monkeypatch.setattr(vb, "WEBRUNNER_PID_FILE", target)
    monkeypatch.setattr(vb, "_pid_alive", _fake_alive)
    return vb._live_webrunner_pid()


def _ask_start_webrunner(target, monkeypatch):
    monkeypatch.setattr(sw, "WEBRUNNER_PID_FILE", target)
    # 整個模組換成替身而不是改真的 `_chrome_slot._pid_alive`：那一份是共用模組，
    # 別的測試（與別的 worker）正在用它。
    fake_slot = type(sw._chrome_slot)("_chrome_slot")
    fake_slot._pid_alive = _fake_alive
    monkeypatch.setattr(sw, "_chrome_slot", fake_slot)
    return sw._live_webrunner_pid()


def _ask_discord_bot(target, monkeypatch):
    monkeypatch.setattr(b, "WEBRUNNER_PID_FILE", target)
    monkeypatch.setattr(b, "_pid_alive", _fake_alive)
    return b._load_pid()


def _ask_dashboard(target, monkeypatch):
    monkeypatch.setattr(ds, "WEBRUNNER_PID_FILE", target)
    return ds._read_pid()


_ASKERS = (
    ("verify_browser._live_webrunner_pid", _ask_verify_browser),
    ("start_webrunner._live_webrunner_pid", _ask_start_webrunner),
    ("discord_bot._load_pid", _ask_discord_bot),
    ("dashboard_server._read_pid", _ask_dashboard),
)


def _ask_everyone(build, tmp_path, monkeypatch):
    """四支讀取端各問一次，回 `(答案表, 每一支各自用的那份檔案狀態)`。

    ⚠️ **每一支都要拿到一份新造的狀態。** 第一版讓四支共用同一個 `target`，於是
    `discord_bot._load_pid` 在「殘檔」那一格把檔案刪掉之後，排在它後面的
    `dashboard_server._read_pid` 讀到的是「檔案不存在」——測試報出一個產品碼裡根
    本不存在的不一致。共用狀態的讀取端排在同一個函式裡時，前面那支的副作用就是
    後面那支的輸入。

    存活判定一律換成替身：這組測試**絕不碰真的 `webrunner.pid`**，也不去探測真的
    行程——本機常有跑了幾十小時的批次正在用那個檔。
    """
    answers: dict[str, tuple[int | None, bool]] = {}
    targets = []
    for name, ask in _ASKERS:
        target = build(tmp_path)
        targets.append(target)
        answers[name] = ask(target, monkeypatch)
    return answers, targets


# `dashboard_server._read_pid` 刻意**不**過濾死掉的行程：它是唯讀顯示層的底層讀
# 取，存活判定由上面的 `build_status()` 做，而那一層要的是三態（在跑／沒在跑／判
# 不出來），把死掉的 pid 在這裡塌成 None 會讓它分不出後兩者。
_FILTERS_DEAD_PROCESSES = {
    "verify_browser._live_webrunner_pid": True,
    "start_webrunner._live_webrunner_pid": True,
    "discord_bot._load_pid": True,
    "dashboard_server._read_pid": False,
}


def _parity_problems(answers: dict[str, tuple[int | None, bool]],
                     expect_decided: bool,
                     expect_pid: int | None) -> list[str]:
    """比較的邏輯自己一支，才有辦法對它做對照組。

    樹是乾淨的時候「回報不一致」那幾行一次都不會執行——整段刪掉照樣全綠，這是本
    repo 的老朋友。所以判斷抽出來，底下用合成的答案表驗它真的會開火。
    """
    problems: list[str] = []
    for name, (pid, decided) in sorted(answers.items()):
        if decided is not expect_decided:
            problems.append(
                f"{name} 回「判定{'得' if decided else '不'}出來」，"
                f"其他人說「判定{'得' if expect_decided else '不'}出來」")
        wanted = expect_pid
        if wanted is not None and _FILTERS_DEAD_PROCESSES.get(name, True):
            wanted = expect_pid if expect_pid == _LIVE_PID else None
        if pid != wanted:
            problems.append(f"{name} 回 pid={pid}，應該是 {wanted}")
    return problems


@pytest.mark.parametrize("label,build,decided,pid",
                         _STATES, ids=[s[0] for s in _STATES])
def test_the_three_way_readers_agree(label, build, decided, pid,
                                     tmp_path, monkeypatch):
    """同一份檔案狀態，四支讀取端要給同一個答案。

    四支各有各的測試、各自全綠——但在這支之前沒有任何東西在比較它們。第一次跑就
    抓到一個不一致：檔案在 `exists()` 與 `read_text()` 之間被刪掉時，只有
    `discord_bot._load_pid` 回「判不出來」，而它自己 docstring 的表寫的是
    「檔案不存在 → `(None, True)`」。
    """
    answers, _targets = _ask_everyone(build, tmp_path, monkeypatch)
    assert len(answers) == 4, "少問了人，這個比較就不成立"
    problems = _parity_problems(answers, decided, pid)
    assert not problems, (
        f"「{label}」這個狀態下四支讀取端的答案不一致：\n  "
        + "\n  ".join(problems)
        + "\n四支都在回答同一個問題（磁碟上這個訊號說了什麼），"
          "所以規則只能有一份。")


def test_the_parity_comparison_actually_fires():
    """對照組：把一個不一致的答案餵進去，判斷要抓得到。"""
    answers = {
        "a._read": (None, True),
        "b._read": (None, False),          # 把「不存在」講成「判不出來」
    }
    problems = _parity_problems(answers, True, None)
    assert any("b._read" in p for p in problems), (
        f"判斷放過了一個真的不一致：{problems}")
    assert not any("a._read" in p for p in problems)


def test_the_parity_comparison_also_watches_the_pid_value():
    """第二個對照組：`decided` 一致、pid 不一致也要抓。

    兩個斷言各要有自己的破綻，否則拿掉其中一個還是綠的。
    """
    answers = {"a._read": (_LIVE_PID, True), "b._read": (None, True)}
    problems = _parity_problems(answers, True, _LIVE_PID)
    assert any("b._read" in p and "pid" in p for p in problems), problems


def test_the_registry_of_liveness_filtering_is_not_stale(tmp_path, monkeypatch):
    """`_FILTERS_DEAD_PROCESSES` 的每一筆都要對得上實際行為。

    這份登記是上面那個比較的輸入，所以它自己錯掉的話，整組 parity 會安靜地放寬。
    直接拿「死掉的 pid」去問每一支，看它回 None 還是回那個 pid。
    """
    answers, _targets = _ask_everyone(
        lambda tmp: _write(tmp, str(_DEAD_PID).encode()), tmp_path, monkeypatch)
    assert set(answers) == set(_FILTERS_DEAD_PROCESSES), (
        "登記表與實際問到的讀取端對不起來")
    for name, (pid, _decided) in answers.items():
        filters = _FILTERS_DEAD_PROCESSES[name]
        if filters:
            assert pid is None, (
                f"{name} 登記成會過濾死掉的行程，實際上回了 pid={pid}")
        else:
            assert pid == _DEAD_PID, (
                f"{name} 登記成不過濾，實際上卻把死掉的 pid 濾掉了")


# ---------------------------------------------------------------------------
# 3. 讀不出來的時候不得刪檔
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    PermissionError(13, "denied"),
    OSError(5, "io error"),
], ids=["權限不足", "IO 錯誤"])
def test_an_unreadable_file_is_never_deleted(error, tmp_path, monkeypatch):
    """讀不出來時四支都不准刪檔——這是 2026-09-07 那條連鎖的起點。

    `discord_bot._load_pid` 當時把「內容不是 UTF-8」當成殘檔刪掉，於是下游那兩支
    剛修好的保守判斷讀到的是「檔案不存在」，**正確地**判定沒有批次，然後在正式批
    次旁邊開第二套瀏覽器。一個把跨行程訊號刪掉的錯誤處理，會讓所有下游正確的判斷
    一起失效。
    """
    answers, targets = _ask_everyone(
        lambda tmp: _RaisesOnRead(error), tmp_path, monkeypatch)
    deleted = {name: target.unlinked
               for (name, _ask), target in zip(_ASKERS, targets)
               if target.unlinked}
    assert not deleted, (
        f"讀不出來卻刪了檔：{deleted}。那份內容可能是別人有效的存活訊號，"
        "我們只知道自己這次讀不到。")
    for name, (pid, decided) in answers.items():
        assert (pid, decided) == (None, False), f"{name} 回了 {(pid, decided)}"


def test_a_dead_pid_is_still_cleaned_up_by_the_bot(tmp_path, monkeypatch):
    """反面：**確定已死**的殘檔該刪，而且只有 bot 那一支該刪。

    沒有這一格的話，上面那支測試最省事的「修法」就是讓 `_load_pid` 永遠不刪檔，
    而那會讓殘留的 pid 檔永遠擋著下一次 `/run`。
    """
    target = _write(tmp_path, str(_DEAD_PID).encode())
    monkeypatch.setattr(b, "WEBRUNNER_PID_FILE", target)
    monkeypatch.setattr(b, "_pid_alive", _fake_alive)
    assert b._load_pid() == (None, True)
    assert not target.exists(), "確定已死的殘檔應該被清掉"


def test_the_read_only_readers_do_not_touch_the_file(tmp_path, monkeypatch):
    """唯讀的三支不准刪檔，連殘檔都不准。

    `dashboard_server._read_pid` 的 docstring 明寫「不要在這裡刪檔或做任何修復動
    作」——別的行程正靠這個檔互相協調。驗證端與啟動器同理：它們問的是「我該不該
    讓位」，刪掉別人的訊號會直接製造出它們要防的那個情況。
    """
    for module, call in ((vb, lambda: vb._live_webrunner_pid()),
                         (sw, lambda: sw._live_webrunner_pid()),
                         (ds, lambda: ds._read_pid())):
        target = _write(tmp_path, str(_DEAD_PID).encode())
        monkeypatch.setattr(module, "WEBRUNNER_PID_FILE", target)
        if module is vb:
            monkeypatch.setattr(vb, "_pid_alive", _fake_alive)
        if module is sw:
            fake_slot = type(sw._chrome_slot)("_chrome_slot")
            fake_slot._pid_alive = _fake_alive
            monkeypatch.setattr(sw, "_chrome_slot", fake_slot)
        call()
        assert target.exists(), (
            f"{module.__name__} 把 pid 檔刪掉了。唯讀的讀取端不得修復這個檔——"
            "它是別人的存活訊號。")
