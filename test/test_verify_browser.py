"""`verify_browser.py` 的結論詞彙契約：「沒驗到」不得跟「驗了、壞了」混為一談。

`verify_browser.py` 是本專案**規定**用來實證瀏覽器／driver 改動的入口。它印一行機器可讀結論給呼叫端 grep，而呼叫端
是一個自走迴圈。

問題出在：有兩條路**根本沒有開瀏覽器**——槽被別人佔住、以及讀到活著的
`webrunner.pid` 而讓位——原本它們跟真正的驗證失敗一樣印 `FAIL`、exit 1。於是同
一個結論同時代表三件事：「瀏覽器壞了」「driver 解析不到」「正式作業在跑」。

而在這台機器上，**讓位是最常見的結果**（正式批次幾乎總是在跑）。混在一起只有
兩種下場，兩種都很糟：看到 FAIL 的人以為自己的改動把瀏覽器弄壞了，去追一個不存
在的問題；或者學會「這支的 FAIL 不用理」，於是真的壞掉那一次也一起被忽略。第二
種尤其致命，因為它把整條驗證守則變成裝飾。

這支測試釘住三件事：三個結論彼此互斥、SKIP 仍然是非零（沒驗到不得回報成功）、
以及那兩個讓位點真的走 `_emit_skip`——最後一條用 AST 掃，因為「改回 `_emit_fail`」
是一個看起來完全正常的一行修改。
"""
from __future__ import annotations

import ast
import os
import contextlib
import io
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "axiomatic"))

import verify_browser as vb  # noqa: E402
# 「哪些按鈕會花錢」的判準（上線的 `FORBIDDEN` 正規式）與單元測試那側的對照清單都
# 住在這裡。**刻意 import 而不是另抄一份**——本檔要做的正是四份清單的對帳，自己再
# 生一份第五份就本末倒置了。
import test_webrunner_shared as tws  # noqa: E402


def _emit(func, *args):
    """跑一個結論發射器，回 `(exit code, 印出來的字)`。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = func(*args)
    return code, buffer.getvalue()


def test_the_three_verdicts_are_mutually_exclusive():
    """OK／FAIL／SKIP 的字面與結束碼都不得重疊。

    呼叫端有兩種讀法——grep 結論行、或只看結束碼——兩種都要分得出來。
    """
    ok_code, ok_out = _emit(vb._emit_ok)
    fail_code, fail_out = _emit(vb._emit_fail, "browser exploded")
    skip_code, skip_out = _emit(vb._emit_skip, "webrunner is running")

    codes = [ok_code, fail_code, skip_code]
    assert codes == [vb.EXIT_OK, vb.EXIT_FAIL, vb.EXIT_SKIP], codes
    assert len(set(codes)) == 3, (
        f"三個結論的結束碼撞在一起了：{codes}。只看結束碼的呼叫端會分不出"
        "「瀏覽器壞了」和「正式作業在跑」。")

    for out in (ok_out, fail_out, skip_out):
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) == 1, f"結論必須剛好一行：{out!r}"
        assert lines[0].startswith(vb.RESULT_PREFIX), lines[0]

    words = [out.split()[1] for out in (ok_out, fail_out, skip_out)]
    assert words == ["OK", "FAIL", "SKIP"], words


def test_skip_is_still_a_non_zero_exit():
    """沒驗到**不得**回報成功。

    這是刻意保留的：把「讓位」降成 exit 0 會讓呼叫端把一次根本沒發生的驗證記成
    綠燈，那比印錯字更危險。分辨是加出來的，不是拿安全換來的——原本檢查 `== 0`
    或 `!= 0` 的呼叫端行為完全沒變。
    """
    code, _ = _emit(vb._emit_skip, "slot busy")
    assert code != 0, (
        "SKIP 變成 exit 0 了——呼叫端會把「什麼都沒驗」讀成「驗過了、沒問題」。")


def test_skip_does_not_collide_with_argparse():
    """SKIP 不能是 2：那是 argparse 打錯參數時自己用的碼。

    而且打錯參數那條路**一行 `VERIFY-BROWSER:` 都不會印**。共用 2 的話，只看結束
    碼的自走迴圈會把「指令打錯」讀成「正式作業在跑、晚點重試」，然後對一個永遠
    不可能成功的指令重試到天荒地老。
    """
    assert vb.EXIT_SKIP != 2, (
        "`EXIT_SKIP` 撞到 argparse 的參數錯誤碼（2）了。")
    assert vb.EXIT_SKIP not in (vb.EXIT_OK, vb.EXIT_FAIL)


def _slot_verdicts():
    """`_run_with_slot` 裡每個 `_emit_*(...)` 呼叫的 `(函式名, 原因字面)`。

    連原因一起抽出來，是因為位置比對不夠精準：這個函式裡有**三**個結論點，其中
    「`import _chrome_slot` 失敗」是真的壞了（repo 自己的模組載不進來），本來就
    該是 FAIL。用原因字面對位才分得出誰是誰。
    """
    import inspect
    import textwrap

    def literal(node):
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value for v in node.values
                           if isinstance(v, ast.Constant))
        return ""

    tree = ast.parse(textwrap.dedent(inspect.getsource(vb._run_with_slot)))
    return [(node.func.id, literal(node.args[0]) if node.args else "")
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.startswith("_emit_")]


def test_both_stand_aside_paths_report_skip_not_fail():
    """槽被佔住、以及偵測到正式作業而讓位——兩條都必須是 SKIP。

    用 AST 掃而不是跑一次：這兩條路要真的觸發，得先有一個活著的正式 webrunner
    或一把被佔住的鎖，測試裡兩者都不該去製造。而「把 `_emit_skip` 改回
    `_emit_fail`」是一個看起來完全正常的一行修改，沒有守門就會安靜地退回去。
    """
    verdicts = _slot_verdicts()
    assert verdicts, (
        "`_run_with_slot` 裡一個 `_emit_*` 呼叫都掃不到——函式改寫過的話這支"
        "測試要跟著改，否則它會安靜地什麼都不檢查。")

    def verdict_for(needle):
        hits = [name for name, reason in verdicts if needle in reason]
        assert len(hits) == 1, (
            f"用 {needle!r} 對不到剛好一個結論點：{verdicts}")
        return hits[0]

    for needle in ("slot busy", "讓位", "無法判定"):
        assert verdict_for(needle) == "_emit_skip", (
            f"{needle!r} 那條路用了 `_emit_fail`：{verdicts}。它**沒有開過"
            "瀏覽器**，報 FAIL 等於宣稱瀏覽器壞了。")

    # 反面：真的壞掉的那條不得被一起「和諧」成 SKIP。repo 自己的共用模組載不
    # 進來是環境壞了，晚點重試不會變好——那正是 FAIL 該用的地方。
    assert verdict_for("_chrome_slot") == "_emit_fail", (
        f"`import _chrome_slot` 失敗被降成 SKIP 了：{verdicts}。"
        "那不是「晚點再來」，是真的壞了。")


def _pid_file(monkeypatch, tmp_path, content):
    """把 `WEBRUNNER_PID_FILE` 指到暫存檔；`content` 是 bytes，None ＝ 不建檔。"""
    p = tmp_path / "webrunner.pid"
    if content is not None:
        p.write_bytes(content)
    monkeypatch.setattr(vb, "WEBRUNNER_PID_FILE", p)
    return p


def test_a_missing_pid_file_means_there_is_really_no_batch(monkeypatch, tmp_path):
    """檔案不存在是**判定得出來**的答案：真的沒有正式作業，可以開瀏覽器。"""
    _pid_file(monkeypatch, tmp_path, None)
    assert vb._live_webrunner_pid() == (None, True)


def test_a_live_pid_is_reported_as_live(monkeypatch, tmp_path):
    _pid_file(monkeypatch, tmp_path, str(os.getpid()).encode("utf-8"))
    pid, decided = vb._live_webrunner_pid()
    assert pid == os.getpid() and decided is True


def test_a_dead_pid_reads_as_no_batch(monkeypatch, tmp_path):
    """死掉的 pid 是**判定得出來**的「沒有正式作業」——這條不能跟「讀不出來」混。"""
    _pid_file(monkeypatch, tmp_path, b"999999")
    monkeypatch.setattr(vb, "_pid_alive", lambda _p: False)
    assert vb._live_webrunner_pid() == (None, True)


def test_an_undecodable_pid_file_is_undecidable_not_empty(monkeypatch, tmp_path):
    """`webrunner.pid` 不是合法 UTF-8 時，不得被當成「沒有正式作業」。

    兩件事一起釘：

    1. `UnicodeDecodeError` 是 `ValueError` 的子類、**不是** `OSError`，所以原本的
       `except (FileNotFoundError, OSError)` 接不到它——這個函式會直接往上炸。
    2. 就算接住了，回「沒有正式作業」也是**錯的方向**：呼叫端會據此開瀏覽器，
       而這是唯一擋住「在正式批次旁邊另開一套瀏覽器」的關卡（正式產圖程式並
       **不會**去搶 Chrome 槽，所以槽在這裡沒有保護作用）。判不出來就要讓位。
    """
    _pid_file(monkeypatch, tmp_path, b"\xff\xfe\x00\x80")
    pid, decided = vb._live_webrunner_pid()   # 不得 raise
    assert pid is None
    assert decided is False, (
        "讀不出內容卻回報「判定得出來、沒有正式作業」——呼叫端會據此開瀏覽器")


def test_a_non_numeric_pid_file_is_undecidable(monkeypatch, tmp_path):
    """內容讀得出來但不是數字，同樣是判不出來，不是「沒有作業」。"""
    _pid_file(monkeypatch, tmp_path, "不是數字".encode("utf-8"))
    assert vb._live_webrunner_pid() == (None, False)


def test_an_unreadable_pid_file_is_undecidable(monkeypatch, tmp_path):
    """讀取本身丟 OSError（權限、檔案被鎖）也要走保守的那一邊。"""
    p = _pid_file(monkeypatch, tmp_path, b"123")

    class _Boom:
        def exists(self):
            return True

        def read_text(self, *a, **k):
            raise PermissionError("locked")

    monkeypatch.setattr(vb, "WEBRUNNER_PID_FILE", _Boom())
    assert vb._live_webrunner_pid() == (None, False)
    assert p.exists()      # 沒有動到真的檔案


def test_the_undecidable_branch_is_wired_into_the_caller():
    """光是函式回對值沒有用——呼叫端要真的因此讓位。

    用 AST 檢查 `_run_with_slot` 有沒有讀第二個回傳值並據此早退。沒有這一支的話，
    把呼叫端改成 `live_pid, _ = _live_webrunner_pid()` 就會安靜地退回舊行為，而
    上面那些函式層的測試**全部照樣綠**。
    """
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(vb._run_with_slot))
    tree = ast.parse(src)
    unpacks = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and isinstance(n.targets[0], ast.Tuple)
               and isinstance(n.value, ast.Call)
               and getattr(n.value.func, "id", "") == "_live_webrunner_pid"]
    assert unpacks, "`_run_with_slot` 沒有把 `_live_webrunner_pid` 的兩個回傳值都接下來"
    names = [e.id for e in unpacks[0].targets[0].elts if isinstance(e, ast.Name)]
    assert len(names) == 2 and not names[1].startswith("_"), (
        f"第二個回傳值被丟掉了（{names}）——那等於退回「判不出來就開瀏覽器」")
    decided = names[1]
    used = [n for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == decided
            and isinstance(n.ctx, ast.Load)]
    assert used, f"`{decided}` 接下來了卻沒有被用到"


class _FakeSlot:
    """夠像 `_chrome_slot` 的替身：一定拿得到槽，並記錄有沒有被釋放。"""

    def __init__(self):
        self.released = []

    def acquire(self, owner, *, timeout=0.0, label=""):
        return True

    def release(self, owner):
        self.released.append(owner)


def _run_slot_with(monkeypatch, tmp_path, pid_bytes):
    """在假的槽底下跑 `_run_with_slot`，回 `(exit_code, run_fn 有沒有被呼叫)`。"""
    fake = _FakeSlot()
    monkeypatch.setitem(sys.modules, "_chrome_slot", fake)
    _pid_file(monkeypatch, tmp_path, pid_bytes)
    called = []

    def run_fn():
        called.append(True)
        return vb.EXIT_OK

    code = vb._run_with_slot("probe", run_fn)
    assert fake.released == [vb.SLOT_OWNER], (
        f"槽沒有被釋放（或釋放了不只一次）：{fake.released}")
    return code, bool(called)


def test_an_undecidable_pid_file_never_opens_a_browser(monkeypatch, tmp_path):
    """**這是這整組修改真正要保證的性質**：判不出來就不准開瀏覽器。

    刻意用行為測試而不是 AST 掃描。原本這裡是一支 AST 守門，只檢查
    `_live_webrunner_pid()` 的第二個回傳值有被接下來、而且那個名字在函式裡出現過
    ——變異測試當場證明它太弱：把 `if not decided:` 改成 `if False and not decided:`
    之後，那個名字**仍然出現**，守門照樣是綠的，而行為已經退回「判不出來就開
    瀏覽器」。

    **「這個名字有被用到」跟「這個判斷真的擋得住事情」是兩回事。** 凡是能改寫成
    恆假條件就繞過的性質，都要用行為去釘。
    """
    code, ran = _run_slot_with(monkeypatch, tmp_path, b"\xff\xfe\x00\x80")
    assert not ran, "pid 檔讀不出來，卻還是開了瀏覽器"
    assert code == vb.EXIT_SKIP, (
        f"讓位應該是 SKIP（exit {vb.EXIT_SKIP}），實際回 {code}")


def test_a_live_batch_never_opens_a_browser(monkeypatch, tmp_path):
    """既有的讓位路徑也用行為釘一次（原本只有 AST）。"""
    code, ran = _run_slot_with(monkeypatch, tmp_path,
                               str(os.getpid()).encode("utf-8"))
    assert not ran, "偵測到活著的正式作業，卻還是開了瀏覽器"
    assert code == vb.EXIT_SKIP


def test_a_clean_machine_does_open_the_browser(monkeypatch, tmp_path):
    """反方向：沒有 pid 檔就該真的往下跑。

    少了這一支，把 `_run_with_slot` 改成「一律 SKIP」也會讓上面兩支通過——
    那樣驗證工具就永遠不做事，而且看起來完全正常。
    """
    code, ran = _run_slot_with(monkeypatch, tmp_path, None)
    assert ran, "沒有正式作業在跑，卻沒有進入驗證流程"
    assert code == vb.EXIT_OK


def test_the_total_deadline_survives_a_clock_jump(monkeypatch):
    """總時長上限量的是**間隔**，不得被牆鐘的跳動影響。

    往回撥（NTP step、手動改時鐘、虛擬機快照還原）會讓以 `time.time()` 算的截止
    時刻永遠不觸發，於是 `run_smoke` docstring 承諾的「全程時間有界」直接失效；
    往前撥則讓它立刻觸發、只試一次就放棄。**換時區與日光節約時間不會**造成這件事
    ——`time.time()` 回的是 UTC epoch 秒——所以這裡模擬的是真正會發生的那種跳動。

    判準與 `_chrome_slot.acquire` 同一條：值不離開本行程就該用 `time.monotonic()`。
    """
    attempts = []

    def fake_attempt(url, headless, attempt):
        attempts.append(attempt)
        return False, "boom"

    monkeypatch.setattr(vb, "_smoke_attempt", fake_attempt)
    monkeypatch.setattr(vb.time, "sleep", lambda _s: None)

    # 牆鐘每次被問就往回跳一大段；monotonic 正常前進。
    jumped = {"n": 0}

    def rewinding_time():
        jumped["n"] += 1
        return 1_000_000.0 - jumped["n"] * 3600.0

    monkeypatch.setattr(vb.time, "time", rewinding_time)
    monkeypatch.setattr(vb.time, "monotonic", lambda: 0.0)

    vb.run_smoke("about:blank", True)
    assert attempts == list(range(1, vb.MAX_ATTEMPTS + 1)), (
        f"牆鐘往回跳影響了重試次數：{attempts}")

    # 反方向：monotonic 真的走過上限時，必須停下來。
    #
    # **不能把 monotonic 釘成常數**——`deadline` 是用第一次讀到的值加上去的，
    # 釘成常數等於 deadline 也跟著移動，條件永遠不成立。（我第一版就是這樣寫的，
    # 測試當場紅了：這正是「假的時鐘要會走，否則量不到任何跟時間有關的東西」。）
    attempts.clear()
    clock = {"t": 0.0}

    def advancing_monotonic():
        now = clock["t"]
        clock["t"] += vb.TOTAL_DEADLINE_SEC + 1.0   # 第一次之後就越過上限
        return now

    monkeypatch.setattr(vb.time, "monotonic", advancing_monotonic)
    vb.run_smoke("about:blank", True)
    assert attempts == [], (
        f"已經超過總時長上限，卻還是跑了嘗試：{attempts}。"
        "上限沒有生效等於這支工具沒有時間界線。")


def test_the_module_docstring_still_teaches_the_contract():
    """契約寫在模組開頭給人看；改了行為就要改那裡，不然文件會反過來誤導人。"""
    doc = vb.__doc__ or ""
    for token in ("VERIFY-BROWSER: OK", "VERIFY-BROWSER: FAIL",
                  "VERIFY-BROWSER: SKIP"):
        assert token in doc, f"模組 docstring 沒提到 {token!r}"
    assert "exit 3" in doc, "docstring 沒說 SKIP 的結束碼是 3"


def test_no_docstring_attributes_fail_to_a_stand_aside_path():
    """行為有守門了，**敘述**沒有——而 2026-09-01 抓到的就是敘述那一半。

    `_run_with_slot` 的 docstring 當時寫著「拿不到 → FAIL『slot busy』、exit 1」與
    「→ 釋放並 FAIL『讓位』、exit 1」，程式碼卻一直是 `_emit_skip`／exit 3。上面那支
    `test_both_stand_aside_paths_report_skip_not_fail` 掃的是呼叫，掃不到散文，所以
    這段假話可以無限期活著——偏偏它正是模組 docstring 花一整段在防的那個誤會：讓位
    是最常見的結果，把它當成 FAIL 的人不是去追一個不存在的瀏覽器問題，就是學會
    「這支的 FAIL 不用理」。看這個函式的人讀的是它自己的 docstring，不是模組開頭。

    判準刻意窄：只抓「條件 → 結論」那個句型（同一行同時有 `→`、讓位/slot busy 的
    字眼、以及 FAIL／exit 1），而且同一行出現 SKIP 就放行。散文裡把兩者並列講道理
    （「混在 FAIL 裡的話…」）是對的、不該被吵——會亂叫的守門最後會被人關掉。
    """
    tree = ast.parse(Path(vb.__file__).read_text(encoding="utf-8"))
    holders = [tree] + [n for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                          ast.ClassDef))]
    stand_aside = ("讓位", "slot busy")
    seen, bad = 0, []
    for node in holders:
        doc = ast.get_docstring(node)
        if not doc:
            continue
        name = getattr(node, "name", "<module>")
        for line in doc.splitlines():
            if not any(tok in line for tok in stand_aside):
                continue
            seen += 1
            if "→" not in line or "SKIP" in line:
                continue
            if "FAIL" in line or "exit 1" in line:
                bad.append(f"{name}: {line.strip()}")

    assert seen, (
        "所有 docstring 裡一句提到讓位／slot busy 的都掃不到——敘述被改寫過的話"
        "這支測試要跟著改，否則它會安靜地什麼都不檢查。")
    assert not bad, (
        "有 docstring 把「讓位／slot busy」說成 FAIL／exit 1，但程式碼送的是 SKIP／"
        "exit 3：\n  " + "\n  ".join(bad) +
        "\n讓位**沒有開過瀏覽器**，說它 FAIL 等於宣稱瀏覽器壞了。")


def test_the_verdict_line_survives_a_multi_line_reason():
    """結論**必須**是單行——呼叫端 grep 的是一行。

    原因字串來源包含例外文字，那裡什麼都可能有（換行、超長訊息）。
    """
    code, out = _emit(vb._emit_fail,
                      "boom\nsecond line\n\tthird\n" + "x" * 500)
    assert code == vb.EXIT_FAIL
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 1, f"多行原因把結論撐成好幾行了：{out!r}"
    assert len(lines[0]) < 260, f"結論行沒有被截短：{len(lines[0])}"


# ---------------------------------------------------------------------------
# 隔離規則：兩個驗證入口都不准碰正式登入態、不准 nuclear sweep
#
# `verify_quota_dialog.py`（397 行）到今天為止**一支測試都沒有**。它會真的開一個
# Chrome，而它的隔離規則全部只寫在 docstring 裡：拋棄式 temp profile、先取 Chrome
# 槽、只精準回收自己那棵行程樹、JS 一律從 `_webrunner_shared` 匯入。四條裡任何一條
#被改掉都沒有東西會發現——而第一條改掉的後果是拿**正式登入 profile** 去跑驗證。
# 那不是假設性的：`.chrome_profile_verify/`（登入 profile 的副本）曾經因為沒被
# gitignore 而躺在工作樹裡，見 CLAUDE.md。
# ---------------------------------------------------------------------------


def _tree_of(name: str) -> ast.Module:
    return ast.parse((REPO_ROOT / "axiomatic" / name).read_text(encoding="utf-8"),
                     name)


def _dotted_name(node) -> str:
    bits = []
    while isinstance(node, ast.Attribute):
        bits.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        bits.append(node.id)
    return ".".join(reversed(bits))


def _calls(tree, *names):
    wanted = set(names)
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and (_dotted_name(n.func) in wanted
                 or _dotted_name(n.func).split(".")[-1] in wanted)]


def _names_sourced_from(tree, func_name: str) -> set:
    """所有「值最終來自 `func_name(...)`」的區域名字（含間接改名）。

    只問「檔案裡有沒有出現 mkdtemp」擋不住「有出現、但傳給 Chrome 的是別的東西」。
    走機制：從指派追到不動點，得到一組真的可以宣稱「這是拋棄式 profile」的名字。
    """
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    found: set = set()
    for _ in range(len(assigns) + 1):
        grown = set(found)
        for node in assigns:
            targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if any(_dotted_name(c.func).split(".")[-1] == func_name
                   for c in ast.walk(node.value) if isinstance(c, ast.Call)) \
                    or any(isinstance(x, ast.Name) and x.id in found
                           for x in ast.walk(node.value)):
                grown |= targets
        if grown == found:
            break
        found = grown
    return found


# ---------------------------------------------------------------------------
# 隔離規則的**範圍**：不是「這兩個檔案」，是「會起瀏覽器的每一個模組」
#
# 2026-09-10 之前這一段掃的是寫死的 `_VERIFY_MODULES = ("verify_browser.py",
# "verify_quota_dialog.py")`。而上面三條規則的**文字**一個模組都沒提到——「絕不可
# 用正式 profile」「清理只准回收自己那棵樹」「整支跑在跨行程 Chrome 槽裡」對**任何
# 一個與正式批次並存的 Chrome** 都成立，不是只對那兩個檔名成立。這正是
# 又是那個家族：規則的敘述沒有邊界，掃描的範圍卻有。
#
# 實測（2026-09-10，全專案 37 個非測試模組）：**Chrome 啟動點總共 4 個**，另外兩個
# 是正式批次的兩個變體，它們**刻意**做相反的事——`_kill_orphan_chrome()` 在自己的
# driver 還不存在時無條件殺光全機 chrome／chromedriver，profile 用的是正式登入態的
# snapshot。所以加寬**新增 0 筆違規**：這是「趁乾淨鎖範圍」，不是在修一個活的缺陷。
# 值得做的理由寫在本區塊開頭那段警語裡——`verify_quota_dialog.py` 真的開 Chrome，
# 而它零測試地活了好幾個月，沒有任何東西會在第三個入口出現時提醒任何人。
#
# 三條規則的判準各自抽成**純函式**（`_profile_isolation_errors` /
# `_nuclear_sweep_errors` / `_slot_and_reap_errors`），因為現實資料是乾淨的：主斷言
# 整個刪掉本來就不會紅。牙齒長在那三支純函式的合成對照組上，跟本檔
# `_dialog_controls` 那一族同一個作法。
# ---------------------------------------------------------------------------

# 掃描時要略過的目錄名。判準是「這裡面的東西不是本專案在維護的原始碼」：點開頭的
# 目錄（`.venv` / `.git` / `.backup` / `.chrome_profile*` / `.claude`）一律略過，外加
# 下面這幾個。`legacy` 依 CLAUDE.md 是唯讀參考、不在上線路徑上（2026-09-10 實測：
# `legacy/` 三個模組裡 `Chrome` / `webdriver` / `set_driver` 出現 **0 次**）；
# `output` 與 `dorossi_workspace` 都在 `.gitignore` 裡、是執行期產物。
# 漏列一個目錄的代價是**吵**（某個 vendored 套件被掃到 → 加進來就好）；多列一個的
# 代價是**安靜漏掉一個啟動點**，所以這份清單要保持最小。
#
# ⚠️ 已知的取捨：repo 根目錄底下的**臨時腳本**會被掃到（`test_text_encoding` 的
# rglob 也一樣）。本專案的約定本來就是「一次性的探測腳本放 repo 外面」。
_LAUNCHER_SKIP_DIRS = frozenset({
    "venv", "__pycache__", "node_modules", "output", "dorossi_workspace",
    "legacy",
})

# 「這個模組會不會真的起一個瀏覽器」的機制判準，三種形式取**聯集**。
# 分開列是因為它們各自漏掉不同的東西，而本專案四個啟動點剛好把兩種主要形式都用到了：
#   * `webdriver.Chrome(...)`   → verify_browser / verify_quota_dialog / webrunner_novelai
#   * `wr.set_driver("chrome",…)` → webrunner_je_only（**完全沒有** `webdriver.Chrome`）
# 只用前者會漏掉 je 變體；只用 `ChromeService` 會漏掉「連 Service 都不建」的最天真
# 寫法（`webdriver.Chrome()` 本身就跑得起來）。四個模組現在各自命中**兩個**訊號，
# 那個冗餘是刻意的：重構掉其中一個，另一個還在。
_DRIVER_CONSTRUCTORS = frozenset({
    "Chrome", "Chromium", "ChromiumEdge", "Edge", "Firefox", "Safari",
    "Remote", "WebDriver", "Driver", "SB",
})
_DRIVER_SERVICES = frozenset({"Service", "ChromeService", "ChromiumService"})
# 綁定來源：只有從這些套件 import 進來的名字才算數。`Service` / `Driver` 這種通用字
# 沒有這道閘就會亂叫——實測 `test_selenium_facade.py` 建了三個 `ChromeService`，只為了
# 讀 `command_line_args()`，根本不開瀏覽器。會亂叫的守門最後會被人關掉。
_BROWSER_PACKAGES = frozenset({
    "selenium", "undetected_chromedriver", "seleniumbase", "je_web_runner",
})
# 包裝層的啟動形式：`set_driver(<瀏覽器名字面>, …)`。第一個引數必須是字面的瀏覽器
# 名——`webrunner_novelai` 的 `port.set_driver(driver)` 傳的是一個**已經存在**的
# driver 物件，那是重新指向、不是啟動。分不開的話每個用 `BrowserPort` 的模組都會被
# 誤判成啟動器。
_WRAPPER_LAUNCHERS = frozenset({"set_driver", "build_stealth_chrome_driver"})
_BROWSER_LITERALS = frozenset({"chrome", "chromium", "edge", "firefox", "safari"})


def _bound_browser_names(tree) -> set:
    """從瀏覽器自動化套件 import 進本模組的名字（含 `as` 別名）。

    `ast.walk` 所以函式內的延遲 import 也算——本專案四個啟動點裡有三個是那樣寫的
    （`verify_browser` / `webrunner_novelai` / `webrunner_je_only` 都在函式裡才
    `from selenium.webdriver.chrome.service import Service as ChromeService`）。
    """
    out: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _BROWSER_PACKAGES:
                for alias in node.names:
                    out.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BROWSER_PACKAGES:
                    out.add(alias.asname or alias.name.split(".")[0])
    return out


def _launch_sites(tree) -> list:
    """`[(行號, 呼叫名, 種類)]` — 這個模組裡每一個會起瀏覽器／driver 的呼叫。

    **抓得到**：`webdriver.Chrome(...)`、`uc.Chrome(...)`、`webdriver.Remote(...)`、
    seleniumbase 的 `Driver(...)` / `SB(...)`、任何從上列套件 import 進來的
    `*Service(...)`（Selenium Manager 解析 chromedriver 的那一步），以及
    `set_driver("chrome", …)` 這種包裝層啟動。
    **抓不到**（已知、刻意）：直接 `subprocess.Popen(["chrome.exe", …])`、playwright /
    puppeteer（本專案沒有這兩者的相依），以及把啟動藏進一個從**本專案別的模組**匯入
    的 helper（那個 helper 所在的模組會被抓到，呼叫端不會——而隔離責任本來就落在
    定義它的那個模組上）。
    """
    bound = _bound_browser_names(tree)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func)
        if not dotted:
            continue
        head, tail = dotted.split(".")[0], dotted.split(".")[-1]
        if tail in _DRIVER_CONSTRUCTORS and (head in bound or tail in bound):
            hits.append((node.lineno, dotted, "constructor"))
        elif tail in _DRIVER_SERVICES and (head in bound or tail in bound):
            hits.append((node.lineno, dotted, "service"))
        elif (tail in _WRAPPER_LAUNCHERS and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).lower() in _BROWSER_LITERALS):
            hits.append((node.lineno, dotted, "wrapper"))
    return sorted(hits)


def _source_modules(roots) -> dict:
    """`{相對路徑: Path}` — 這些目錄底下本專案自己的非測試 `.py`。

    **參數化不是為了好看**：§8.8(A3)——現實資料乾淨的時候，寬範圍與窄範圍的輸出
    一模一樣，所以範圍本身要有自己的釘子，而釘它的唯一辦法是餵一個合成目錄進來。
    """
    out = {}
    for root in roots:
        root = Path(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".")
                           and d not in _LAUNCHER_SKIP_DIRS]
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                # 測試模組不算：它們用假 port／假 psutil，不會真的開瀏覽器，而且
                # 有些為了讀 `command_line_args()` 真的會建一個 `ChromeService`。
                if filename.startswith("test_") or filename == "conftest.py":
                    continue
                path = Path(dirpath) / filename
                out[path.relative_to(root).as_posix()] = path
    return out


# 這一族會被十幾支測試各掃一次，而 `webrunner_novelai.py` 之類的大檔重複 `ast.parse`
# 很貴（實測：不快取 13 秒，快取 1.2 秒）。key 帶 mtime 與大小，所以**內容變了就
# 自動失效**——不是把結果記起來的 memo，那種寫法會讓「先掃一次、再寫檔、再掃一次」
# 的測試拿到過期答案。
_TREE_CACHE: dict = {}
_SITES_CACHE: dict = {}


def _stat_key(path):
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _cached_tree(path, label=None):
    key = _stat_key(path)
    tree = _TREE_CACHE.get(key)
    if tree is None:
        tree = _TREE_CACHE[key] = ast.parse(
            path.read_text(encoding="utf-8"), label or str(path))
    return tree


def _chrome_launchers(roots) -> dict:
    """`{相對路徑: [啟動點]}` — 上面那些模組裡真的會起瀏覽器的那些。"""
    found = {}
    for rel, path in _source_modules(roots).items():
        try:
            key = _stat_key(path)
        except OSError:
            continue
        sites = _SITES_CACHE.get(key)
        if sites is None:
            try:
                sites = _launch_sites(_cached_tree(path, rel))
            except (SyntaxError, UnicodeDecodeError, OSError):
                sites = []
            _SITES_CACHE[key] = sites
        if sites:
            found[rel] = sites
    return found


def _scan_roots():
    return [REPO_ROOT]


# 例外名單：**只給「刻意做相反的事」的正式批次**，不是給新入口的逃生門。
# 兩支正式產圖程式在自己的 driver 生出來之前無條件殺光全機 chrome／chromedriver
# （那一刻沒有自家瀏覽器要保護），並且刻意用正式登入 profile 的 snapshot——這正是
# 上面三條隔離規則的反面，而且是對的。新的驗證／探測入口**不該**列進來：它要嘛滿足
# 那三條，要嘛就是一個會在正式批次旁邊誤殺東西的工具。
#
# 這份名單自己也要對帳：一筆過期的例外是 **fail-open**——檔案改名或不再開瀏覽器
# 之後，那個字串就再也對不到任何東西，守門照跑、測試全綠、而它原本豁免的模組已經
# 不在受檢集合裡，差別是零（CLAUDE.md 對 `_OWNER_ONLY_SLASH` 記的是同一個形狀）。
_LAUNCHER_EXEMPT = {
    "axiomatic/webrunner_novelai.py":
        "正式批次（selenium 變體）。刻意 nuclear sweep：`_kill_orphan_chrome()` 在 "
        "`webdriver.Chrome(...)` 之前無條件殺光全機 chrome／chromedriver，因為那一刻"
        "自家 driver 還不存在；profile 用的是正式登入態的 snapshot，不是 mkdtemp。"
        "**這個變體同時也是 `verify_browser.py` 的驗證入口**（`WEBRUNNER_SCRIPT` "
        "指到它，由它的 `_run_setup_verification` 進去），而那條路的保證跟上面這段"
        "剛好相反——不 nuclear sweep（`_SUPPRESS_ORPHAN_SWEEP`）、profile 指到隔離"
        "的暫時目錄。兩條路共用一個檔，所以這段理由只涵蓋 `main` 那一條；驗證那條"
        "由本檔 `_VERIFY_ENTRY` 那一組各自釘住。",
    "axiomatic/webrunner_je_only.py":
        "正式批次（je_web_runner 變體）。與 selenium 變體同一套 Chrome 生命週期，"
        "同樣 nuclear sweep、同樣用登入 profile 的 snapshot；啟動走 "
        "`wr.set_driver(\"chrome\", …)` 而不是 `webdriver.Chrome(...)`。",
}


def _exemption_errors(exempt, launchers) -> list:
    """例外名單的對帳判準。**純函式**——理由同 `_dialog_controls`：現況乾淨，所以把
    主測試裡的斷言整條刪掉本來就不會紅，牙齒得長在一個合成語料問得到的地方。
    """
    errors = []
    for rel, reason in exempt.items():
        if rel not in launchers:
            errors.append(
                f"例外名單裡的 {rel!r} 已經不是一個 Chrome 啟動點了（檔案改名、或"
                f"那段啟動被拿掉）。目前掃到的是：{sorted(launchers)}")
        if len(str(reason).strip()) < 20:
            errors.append(f"{rel} 的例外沒有寫下夠具體的理由：{reason!r}")
    return errors


def _population_errors(launchers, exempt, *, min_total=4, min_checked=2) -> list:
    """族群下限。**純函式**，好讓合成語料能各別觸發其中一條。

    §8.8(A4)：空語料**測不動** `>=` 下限——`0 >= 2` 與 `0 >= 4` 都是假，第一條先炸、
    第二條根本沒被證明過。所以兩條下限各要一個「只違反它自己」的對照語料，見
    `test_the_population_floors_each_fire_on_their_own_corpus`。
    """
    errors = []
    if len(launchers) < min_total:
        errors.append(
            f"全專案只掃到 {len(launchers)} 個 Chrome 啟動點（下限 {min_total}）："
            f"{sorted(launchers)}。偵測器壞掉時「沒有違規」跟「什麼都沒掃到」"
            "在輸出上一模一樣。")
    checked = sorted(set(launchers) - set(exempt))
    if len(checked) < min_checked:
        errors.append(
            f"扣掉例外之後只剩 {len(checked)} 個模組要受檢（下限 {min_checked}）："
            f"{checked}。例外名單長到把要檢查的東西吃光時，底下每一支都會空轉通過。")
    return errors


# 受檢模組數的下限。三支主測試各自再斷言一次——變異實測：把 `_isolated_launchers`
# 的 return 換成 `return {}`，它內部那道 `_population_errors` 已經跑完了、攔不住，
# 於是三支主測試全部空轉通過，而且一個字都不會說。
_MIN_ISOLATED = 2


def _isolated_launchers() -> dict:
    """真正受這三條隔離規則管的模組：會起瀏覽器、而且不在例外名單裡。

    **呼叫端請用 `_isolated_launchers_checked()`**，理由見 `_MIN_ISOLATED`。
    """
    launchers = _chrome_launchers(_scan_roots())
    problems = _population_errors(launchers, _LAUNCHER_EXEMPT)
    assert not problems, "\n".join(problems)
    return {rel: path for rel, path in _source_modules(_scan_roots()).items()
            if rel in launchers and rel not in _LAUNCHER_EXEMPT}


def _isolated_floor_error(checked) -> str:
    """`_MIN_ISOLATED` 的判準，**抽成純函式**才有辦法用合成語料驗它。

    直接寫成 `assert len(...) >= _MIN_ISOLATED` 的話，把 `_MIN_ISOLATED` 調成 0 不會
    讓任何測試變紅——今天真的有兩個受檢模組，所以下限放寬與否在輸出上分不出來。
    §8.8(A4) 的同一條：下限這種「只在別的東西壞掉時才說話」的斷言也要自己的對照組。
    """
    if len(checked) < _MIN_ISOLATED:
        return (f"受檢模組只剩 {len(checked)} 個（下限 {_MIN_ISOLATED}）："
                f"{sorted(checked)}。空的受檢集合會讓每一支主測試空轉通過，"
                "而輸出跟「全部合規」一模一樣。")
    return ""


def _isolated_launchers_checked() -> dict:
    checked = _isolated_launchers()
    assert not _isolated_floor_error(checked), _isolated_floor_error(checked)
    return checked


def _tree_at(path) -> ast.Module:
    return _cached_tree(Path(path), str(path))


def _docstring_ids(tree) -> set:
    """所有 docstring 節點的 `id()`。

    兩支掃描都要排除說明文字——`verify_browser` 自己的 docstring 就寫著「永遠**指定
    PID**、不是 nuclear `/IM` 全殺」（L266、L593），兩支正式批次的 docstring 也提到
    `--user-data-dir`（L631／L499）。抓到自己的說明文字是這個檔案早就記下來的坑。
    """
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and body \
                and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant):
            out.add(id(body[0].value))
    return out


# --- 規則 1：profile 隔離 -------------------------------------------------
# 追到「真的把目錄交給瀏覽器」的那個運算式，不是追一個 helper 的名字。

_PROFILE_FLAG = "--user-data-dir"
# 「把 profile 目錄包成 Options」的 helper。它們是 sink 的第二種形式：
# `verify_quota_dialog` 自己一個 `--user-data-dir` 都沒有，它把目錄交給
# `vb._make_options(profile, True)`，所以只掃字面旗標會漏掉它。
_PROFILE_BUILDERS = frozenset({"_make_options", "_make_chrome_options"})


def _flat_literal(node) -> str:
    """f-string／字面／`+` 串接 → 它的字面部分。"""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flat_literal(node.left) + _flat_literal(node.right)
    return ""


def _profile_sinks(tree) -> list:
    """`[(行號, 種類, [名字])]` — 每個把 profile 目錄送進瀏覽器的運算式。

    判準刻意是 `lstrip().startswith("--user-data-dir")`，不是「字串裡有這幾個字」：
    兩支正式批次的 docstring 就含這個旗標。f-string 命中之後它的子節點要一起吃掉，
    否則裡面那個 `"--user-data-dir="` Constant 會再被算一次、而且不帶任何名字＝
    看起來像違規（實測過，`verify_browser` 會因此誤判）。
    """
    docs = _docstring_ids(tree)
    consumed: set = set()
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.JoinedStr, ast.BinOp)) and id(node) not in consumed:
            if _flat_literal(node).lstrip().startswith(_PROFILE_FLAG):
                for kid in ast.walk(node):
                    consumed.add(id(kid))
                out.append((node.lineno, "flag",
                            [n.id for n in ast.walk(node)
                             if isinstance(n, ast.Name)]))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in consumed and id(node) not in docs \
                and node.value.lstrip().startswith(_PROFILE_FLAG):
            out.append((node.lineno, "flag", []))
        elif isinstance(node, ast.Call) \
                and _dotted_name(node.func).split(".")[-1] in _PROFILE_BUILDERS \
                and node.args:
            arg = node.args[0]
            out.append((node.lineno,
                        f"builder:{_dotted_name(node.func).split('.')[-1]}",
                        [arg.id] if isinstance(arg, ast.Name) else []))
    return sorted(out)


def _throwaway_closure(tree, func_name: str = "mkdtemp") -> set:
    """`_names_sourced_from` ＋ 參數傳遞：值最終來自 `mkdtemp()` 的每一個名字。

    多出來的那一半是實測逼出來的：`verify_browser` 的 `--user-data-dir=` f-string
    內插的是 `_make_options` 的**參數** `profile_dir`，不是任何一個指派。少了參數
    傳遞，本專案唯一那個「照著契約寫」的模組會被判成違規。傳遞條件是「**每一個**
    呼叫點的第 i 個引數都是拋棄式」——只要有一個呼叫點傳別的東西，那個參數就不算，
    否則一個被誤用一次的 helper 會把整條結論洗白（`any` 版本的變異最初存活，因為
    合成語料裡沒有「同一個 helper 被好壞兩種引數各叫一次」的案例）。
    """
    names = _names_sourced_from(tree, func_name)
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    sites: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            sites.setdefault(_dotted_name(node.func).split(".")[-1], []).append(node)
    for _ in range(len(funcs) + 1):
        grown = set(names)
        for fname, fnode in funcs.items():
            calls = sites.get(fname, [])
            if not calls:
                continue
            for i, param in enumerate([a.arg for a in fnode.args.args]):
                passed = [isinstance(c.args[i], ast.Name) and c.args[i].id in grown
                          for c in calls if len(c.args) > i]
                if passed and all(passed):
                    grown.add(param)
        if grown == names:
            break
        names = grown
    return names


def _profile_isolation_errors(tree) -> list:
    """規則 1 的判準，抽成純函式好讓合成語料問得到它。回一串違規說明。"""
    errors = []
    sinks = _profile_sinks(tree)
    if not sinks:
        errors.append(
            "會起瀏覽器，卻找不到任何 profile 目錄的去向（`--user-data-dir=` 或 "
            "profile-builder helper）。不指定時 chromedriver 會自己開一個暫存 "
            "profile，那確實是隔離的——但這條規則要的是**明示**的隔離："
            "請顯式傳一個 `tempfile.mkdtemp()` 目錄進去。")
        return errors
    throwaway = _throwaway_closure(tree)
    if not throwaway:
        errors.append("整個模組沒有 `tempfile.mkdtemp()`")
    for lineno, kind, names in sinks:
        if not (names and all(n in throwaway for n in names)):
            errors.append(
                f"L{lineno} 的 profile 目錄（{kind}，用到 {names or '無名字'}）"
                f"追不到 `mkdtemp()`。可追溯的拋棄式名字：{sorted(throwaway)}")
    return errors


# --- 規則 2：不得 nuclear sweep --------------------------------------------

def _nuclear_sweep_errors(tree) -> list:
    """規則 2 的判準。三個訊號，涵蓋同一件事的三種寫法。

    1. 呼叫 `_kill_orphan_chrome`（照抄那一份）；
    2. `taskkill /IM`（依映像名全殺）。舊版只看「Call 裡面的 Constant 剛好等於
       `"/IM"`」——那是照著**現有寫法**（`subprocess.run([..., "/IM", ...])`）寫出來
       的判準，不是照著威脅寫的：一個模組層的 `ARGS = ["taskkill", "/F", "/IM", …]`
       常數、或一整條 `"taskkill /F /IM chrome.exe"` 字串都繞得過去。現在掃**所有**
       非 docstring 字串常數、按空白切 token、不分大小寫；
    3. 出現 `"chrome.exe"` / `"chromedriver.exe"` 字面——**依映像名指認行程，本質上
       就分不出「我的 Chrome」與「正式批次的 Chrome」**，所以在受檢的模組裡沒有安全
       的用法。這一條才是真正的機制，前兩條是它的兩個具體形狀。

    三條在現實資料上的判別力都是滿的（2026-09-10 實測：兩個驗證模組 0 命中，兩支
    正式批次每支 4 個映像名字面 ＋ 1 個 `/IM`）。
    """
    errors = []
    if _calls(tree, "_kill_orphan_chrome"):
        errors.append("呼叫了 `_kill_orphan_chrome`（nuclear sweep）")
    docs = _docstring_ids(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            continue
        if "/im" in node.value.lower().split():
            errors.append(
                f"L{node.lineno} 用了 `taskkill /IM`（依映像名全殺）："
                f"{node.value[:60]!r}")
        if node.value.lower() in ("chrome.exe", "chromedriver.exe"):
            errors.append(
                f"L{node.lineno} 出現映像名 {node.value!r}。依映像名指認行程分不出"
                "自家瀏覽器與正式批次的瀏覽器——要回收的是 "
                "`driver.service.process.pid` 那棵樹，用 `_capture_own_tree` / "
                "`_reap_pids`。")
    return errors


# --- 規則 3：外科式回收 ＋ 取 Chrome 槽 ------------------------------------

# 這三個名字是**唯一實作**，所以規則 3 刻意「照名字叫」，跟規則 1 的處置相反。
# 理由：「讓位」的順序契約（先取槽 → 讀 `webrunner.pid` → 有活著的正式作業就釋放並
# SKIP）只寫在 `verify_browser._run_with_slot` 裡；自己去 `_chrome_slot.acquire()`
# 拿槽的新入口**不會**讓位，於是它在正式批次跑著的時候照樣開瀏覽器——那是這三條
# 規則裡代價最高的一種違反（正式批次一次可以跑好幾天）。
_BLESSED_ISOLATION_HELPERS = ("_capture_own_tree", "_reap_pids", "_run_with_slot")


def _slot_and_reap_errors(tree) -> list:
    """規則 3 的判準。"""
    errors = []
    for fn in ("_capture_own_tree", "_reap_pids"):
        if not _calls(tree, fn):
            errors.append(
                f"沒有呼叫 `{fn}`——清理必須是「quit 之前抓下自己那棵行程樹、比對 "
                "`create_time` 身分之後只殺它」，不得自己另寫一套。"
                "另寫一套就是誤殺別人 Chrome 的那條路。")
    if not _calls(tree, "_run_with_slot"):
        errors.append(
            "沒有透過 `verify_browser._run_with_slot` 取 Chrome 槽。直接 "
            "`_chrome_slot.acquire()` 不算：讓位給活著的 `webrunner.pid` 那段順序"
            "契約只在 `_run_with_slot` 裡，繞過它等於在正式批次旁邊開瀏覽器。")
    return errors


# --- 三支主測試 ------------------------------------------------------------

def test_every_isolated_chrome_launcher_opens_a_throwaway_profile():
    """交給瀏覽器的 profile 目錄必須追得到 `tempfile.mkdtemp()`。

    正式 profile 帶著登入態與 singleton lock；拿它跑驗證會污染登入狀態，最壞情況是
    把正在跑的正式批次的 profile 搞壞。不比對字串（`.chrome_profile` 這幾個字在
    docstring 裡本來就會出現，比字串會抓到說明自己的那句話），而是追指派來源。

    **2026-09-10 從「每個模組都要呼叫 `_make_options`」改成追機制。** 舊版釘的是
    兩個驗證模組**自己的 helper 名字**，於是範圍一加寬，任何自己組 Options 的新入口
    都會撞上「沒有任何 `_make_options` 呼叫」——而那不是它的錯：`_make_options` 刻意
    不照抄正式那套 stealth 旗標（見它自己的 docstring），另一個用途的驗證本來就可能
    需要不同的旗標。照著名字叫只會教人去加例外，而例外是 fail-open 的。現在問的是
    **目錄怎麼走到瀏覽器手上**：字面 `--user-data-dir=`，或交給一個 profile-builder。
    """
    bad = {rel: _profile_isolation_errors(_tree_at(path))
           for rel, path in _isolated_launchers_checked().items()}
    bad = {rel: errs for rel, errs in bad.items() if errs}
    assert not bad, "\n".join(f"{rel}: {e}" for rel, errs in bad.items()
                              for e in errs)


def test_no_isolated_launcher_nuclear_sweeps():
    """清理只准回收「自己這次起的那棵行程樹」。

    webrunner 的 `_kill_orphan_chrome()` 會無條件殺光全機的 chrome／chromedriver
    ——驗證工具若照抄，跑一次驗證就會把使用者手開的瀏覽器、以及正在跑的正式批次一起
    殺掉。三個訊號與它們各自防的寫法，見 `_nuclear_sweep_errors`。
    """
    bad = {rel: _nuclear_sweep_errors(_tree_at(path))
           for rel, path in _isolated_launchers_checked().items()}
    bad = {rel: errs for rel, errs in bad.items() if errs}
    assert not bad, "\n".join(f"{rel}: {e}" for rel, errs in bad.items()
                              for e in errs)


def test_every_isolated_launcher_reaps_only_its_own_tree_and_takes_the_slot():
    """正面條件：抓自己那棵樹、回收它、而且整支跑在跨行程 Chrome 槽裡面。

    槽是「bot 不會在我們跑的時候 nuclear-sweep 掉我們」的唯一保證，也是
    `verify_browser` 讓位邏輯的掛載點。少了它，驗證會在正式批次啟動的瞬間被砍掉，
    然後印出一個看起來像「瀏覽器壞了」的 FAIL。這一支刻意「照名字叫」，理由見
    `_BLESSED_ISOLATION_HELPERS` 上面那段。
    """
    bad = {rel: _slot_and_reap_errors(_tree_at(path))
           for rel, path in _isolated_launchers_checked().items()}
    bad = {rel: errs for rel, errs in bad.items() if errs}
    assert not bad, "\n".join(f"{rel}: {e}" for rel, errs in bad.items()
                              for e in errs)


# --- 對帳與範圍的釘子 ------------------------------------------------------

def test_the_blessed_isolation_helpers_still_exist():
    """規則 3 照名字叫的三個 helper 必須真的還在 `verify_browser` 裡。

    名字一改，那三個 `_calls(...)` 就變成永遠對不到的字串——而失敗訊息會說「你沒有
    呼叫它」，把讀的人指往完全錯誤的方向。這是 `_OWNER_ONLY_SLASH` 那個 fail-open
    形狀的同一族，差別只在這一份是 fail-loud-but-misleading。
    """
    # 空的清單會讓下面整個迴圈跑 0 圈然後通過——一份完全合法的「三個都還在」報告。
    # 實測：`_BLESSED_ISOLATION_HELPERS = ()` 這個變異原本活得好好的。
    assert len(_BLESSED_ISOLATION_HELPERS) == 3, (
        f"`_BLESSED_ISOLATION_HELPERS` 有 {len(_BLESSED_ISOLATION_HELPERS)} 筆，"
        "應該是三個。清單被清空的話這支測試會空轉然後通過，等於沒有對帳。")

    tree = _tree_of("verify_browser.py")
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for fn in _BLESSED_ISOLATION_HELPERS:
        assert fn in defined, (
            f"`verify_browser.{fn}` 不見了（改名？）——規則 3 會用一個對不到任何"
            "東西的名字去叫，訊息還會說「你沒有呼叫它」。")


_SLOT_RULE_CASES = [
    ("三個都在", "_capture_own_tree()\n_reap_pids([])\n_run_with_slot(main)", 0, ""),
    ("沒抓自己那棵樹", "_reap_pids([])\n_run_with_slot(main)", 1, "_capture_own_tree"),
    ("沒回收", "_capture_own_tree()\n_run_with_slot(main)", 1, "_reap_pids"),
    ("沒取槽", "_capture_own_tree()\n_reap_pids([])", 1, "Chrome 槽"),
    ("繞過 _run_with_slot 自己取鎖",
     "_capture_own_tree()\n_reap_pids([])\n_chrome_slot.acquire()", 1, "Chrome 槽"),
    ("一個都沒有", "pass", 3, "_capture_own_tree"),
]


@pytest.mark.parametrize("label,body,count,expected", _SLOT_RULE_CASES,
                         ids=[c[0] for c in _SLOT_RULE_CASES])
def test_the_slot_and_reap_rule_can_still_see_a_violation(label, body, count,
                                                          expected):
    """規則 3 的合成對照組——每一種違規都要被講出來。

    ⚠️ **這一支是實測補上的，不是預防性的。** 真實資料永遠是乾淨的（兩個受檢模組
    都照規則寫），所以三支主測試**證明不了這個判準還活著**：把
    `_slot_and_reap_errors` 整個換成 `return []`，全部 13 支相關測試照樣全綠
    （2026-09-10 用專案自己的變異工具對**真檔案**跑出來的結果；設計當下對記憶體
    副本跑的那一輪沒有抓到，因為那一輪沒有把這個函式當成可變異的目標）。

    `_chrome_slot.acquire()` 那一格是重點：它**有**取槽，但繞過了
    `_run_with_slot` 裡「讓位給活著的 `webrunner.pid`」那段順序契約——所以它必須
    仍然算違規，否則規則 3 名字綁定的整個理由就不成立。
    """
    errors = _slot_and_reap_errors(ast.parse(body, "<synthetic>"))
    assert len(errors) == count, f"{label}: {errors}"
    if expected:
        assert any(expected in e for e in errors), f"{label}: {errors}"


def test_a_service_only_launcher_is_still_detected(tmp_path):
    """只用 `ChromeService(...)`、沒有建構子也沒有包裝層的模組也算啟動點。

    ⚠️ 也是實測補上的：`_DRIVER_SERVICES = frozenset()` 這個變異原本存活——今天
    四個啟動點**每一個**都同時命中建構子或包裝層訊號，所以 service 那一路在真實
    資料上是完全冗餘的，拿掉沒有任何測試看得出來。而它不是裝飾：Selenium Manager
    解析 chromedriver 的那一步就在 `Service(...)`，一個只建 Service、把 driver
    交給別處的模組確實會起一個 chromedriver 行程。
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "service_only.py").write_text(
        "from selenium.webdriver.chrome.service import Service\n"
        "def start():\n"
        "    return Service(log_output='x')\n",
        encoding="utf-8")
    found = _chrome_launchers((root,))
    assert "service_only.py" in found, (
        f"只建 Service 的模組沒有被當成啟動點：{sorted(found)}")
    kinds = {kind for _line, _name, kind in found["service_only.py"]}
    assert kinds == {"service"}, kinds


def test_a_service_from_a_non_browser_package_is_not_a_launcher(tmp_path):
    """反面：`Service` 這個名字太常見，來源不是瀏覽器套件就不算。

    沒有這一格，上面那支可以靠「看到 Service 就算」通過，而那會讓每一個有
    `Service` 這個字的模組都變成啟動點。
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "unrelated.py").write_text(
        "from mycompany.rpc import Service\n"
        "def start():\n"
        "    return Service(port=1)\n",
        encoding="utf-8")
    assert _chrome_launchers((root,)) == {}


def test_every_launcher_exemption_still_names_a_real_launcher():
    """例外名單對帳：過期的例外是 **fail-open**。

    檔案改名、或那個模組不再開瀏覽器之後，名單裡那個字串就再也對不到任何東西：
    守門照跑、測試全綠、而它原本豁免的模組**已經不在受檢集合裡**——差別是零。
    所以兩個方向都要問：每一筆都還指著一個真的啟動點，而且每一筆都寫了理由。
    """
    launchers = _chrome_launchers(_scan_roots())
    assert _LAUNCHER_EXEMPT, "例外名單空了，這一支就沒有東西在對帳"
    assert len(launchers) >= 4, (
        f"只掃到 {len(launchers)} 個啟動點：{sorted(launchers)}——先修偵測器")
    problems = _exemption_errors(_LAUNCHER_EXEMPT, launchers)
    assert not problems, "\n".join(problems)


# 名字通用到不能單看名字就算數的：`acquire` / `release` 這種東西滿街都是，只有在
# receiver 真的解析得到 `_chrome_slot` 時才算。其餘（`try_acquire` /
# `held_by_live_other` / `read_holder`）是槽專屬的字，任何 receiver 都算。
_SLOT_GENERIC_ATTRS = {"acquire", "release", "close", "open", "read", "write"}
# 不在 `_chrome_slot` 公開 API 裡、但語意上就是「包著槽跑一段」的名字。
_SLOT_EXTRA_ATTRS = {"_run_with_slot"}
_SLOT_VERBS = ("acquire", "release", "with_slot")


def _slot_public_api() -> set:
    """`_chrome_slot` 的模組層公開函式名，**從原始碼抽出來而不是寫死一份清單**。

    寫死的話就會變成 `_OWNER_ONLY_SLASH` 那個形狀：以後 `_chrome_slot` 多一個公開
    函式，這裡不會知道，而漏掉的方向是 fail-open。抽取當然也會壞，所以
    `test_the_slot_api_extraction_has_a_floor` 釘住它至少要含哪幾個名字——空的抽取
    結果跟「乾淨」長得一模一樣。
    """
    import ast as _ast

    source = (REPO_ROOT / "axiomatic" / "_chrome_slot.py").read_text(
        encoding="utf-8")
    return {n.name for n in _ast.parse(source).body
            if isinstance(n, _ast.FunctionDef) and not n.name.startswith("_")}


def _slot_wrapper_name(name: str) -> bool:
    """裸呼叫的名字看起來是不是「Chrome 槽」的包裝函式。

    ⚠️ 不能只問 `"slot" in name`——這個專案有**三種** slot。實測整包掃出來的四個裸
    名字裡有三個跟 Chrome 槽無關：`remove_character_slot` /
    `remove_all_character_slots` 是角色欄位，`_dorossi_resolve_turn_slot` 是對話輪次。
    亂叫的守門最後會被人關掉（同 `test_text_encoding` / `test_language` 的取捨），
    所以判準要嘛點名 `chrome_slot`，要嘛配一個真的在講取／放槽的動詞。
    """
    low = name.lower()
    if "chrome_slot" in low:
        return True
    return "slot" in low and any(verb in low for verb in _SLOT_VERBS)


def _slot_module_aliases(tree):
    """檔案裡指向 `_chrome_slot` 的名字：模組別名，以及直接 import 進來的函式名。

    `from axiomatic import _chrome_slot` 也要認——那是本 repo 自己的慣用寫法
    （`start_webrunner.py` 就這樣寫），也正是 2026-09-10 `_module_imports` 補上
    `ImportFrom.names` 之前漏掉的同一個形狀。
    """
    import ast as _ast

    mod_aliases, func_names = set(), set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == "_chrome_slot":
                    mod_aliases.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, _ast.ImportFrom):
            if (node.module or "").split(".")[-1] == "_chrome_slot":
                for alias in node.names:
                    func_names.add(alias.asname or alias.name)
            else:
                for alias in node.names:
                    if alias.name == "_chrome_slot":
                        mod_aliases.add(alias.asname or alias.name)
    return mod_aliases, func_names


def _slot_protocol_calls(source: str) -> set:
    """原始碼裡「**參與** Chrome 槽協定」的呼叫。**純函式**，理由同
    `_exemption_errors`：現況是乾淨的，所以牙齒得長在一個合成語料問得到的地方。

    用 AST 不用子字串——本檔與被掃的檔案裡，說明文字提到 `_chrome_slot` 的地方比
    真的呼叫還多（`webrunner_novelai.py` 唯一一次出現就是在註解裡）。

    **2026-09-11：判準從「取槽」放寬成「參與槽協定」。** 原本只認
    `acquire` / `try_acquire` / `_run_with_slot`，所以 `held_by_live_other()`
    （「問問槽現在被誰拿著」）整個從守門底下穿過去。這很重要，因為豁免名單裡白紙
    黑字寫的理由是「那一刻自家 driver 還不存在，所以**無條件**殺光全機 chrome」
    ——只要那個模組去問了槽一句，那個「無條件」就已經是假的了，而舊判準看不到。
    「確認現有分支都對」不等於「涵蓋完整」，這是 §8.8 那一族的形狀。
    """
    import ast as _ast

    tree = _ast.parse(source)
    mod_aliases, func_names = _slot_module_aliases(tree)
    watched = _slot_public_api() | _SLOT_EXTRA_ATTRS
    found = set()
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        if isinstance(func, _ast.Attribute):
            if func.attr not in watched:
                continue
            recv = getattr(func.value, "id", None)
            resolved = recv in mod_aliases
            slotty = recv is not None and "chrome_slot" in recv.lower()
            if resolved or slotty or func.attr not in _SLOT_GENERIC_ATTRS:
                found.add(f"{recv or '?'}.{func.attr}")
        elif isinstance(func, _ast.Name):
            if func.id in func_names or _slot_wrapper_name(func.id):
                found.add(func.id)
    return found


# 抽取結果**至少**要含這幾個名字。`held_by_live_other` 正是 2026-09-11 補上的那個
# 缺口，釘在這裡，未來誰把它從 `_chrome_slot` 改名或改成私有，測試會先叫。
_SLOT_API_FLOOR = frozenset({"acquire", "try_acquire", "held_by_live_other"})


def _api_shortfall(api, floor):
    """`floor` 裡沒出現在 `api` 的名字，加上**分母**（比對了幾個）。

    抽成 helper 的理由跟 `_channel_drift`（`test_bot_helpers`）一模一樣：真實資料上
    這個比對永遠回空 list，所以把它直接寫在測試裡的話，**掏空它不會變紅**。這不是
    假設——變異 S7 把 `missing` 換成 `set() - api`，整組測試照樣全綠，floor 就這樣
    變成裝飾品。分母是第二個洞：只比對第一個名字同樣看不出來。
    """
    missing = sorted(name for name in floor if name not in api)
    return missing, len(floor)


def test_the_api_floor_comparison_actually_bites():
    """正控制。沒有這一支，`_api_shortfall` 整個換成 `return [], 0` 也會全綠。"""
    missing, checked = _api_shortfall({"acquire"}, _SLOT_API_FLOOR)
    assert missing == ["held_by_live_other", "try_acquire"], (
        f"合成的缺漏沒有被抓到（得到 {missing}），比對壞了。")
    assert checked == 3, f"分母是 {checked}，應該是 3——比對被截短了。"
    assert _api_shortfall(_SLOT_API_FLOOR, _SLOT_API_FLOOR)[0] == [], (
        "完整的 api 被誤報成有缺漏，判準太緊。")


def test_the_slot_api_extraction_has_a_floor():
    """`_slot_public_api()` 抽不到東西的話，下面整組偵測都會安靜地失效。

    空的抽取結果與「這個模組沒有公開函式」是同一個值，而後者是假的。
    """
    api = _slot_public_api()
    missing, checked = _api_shortfall(api, _SLOT_API_FLOOR)
    assert checked == len(_SLOT_API_FLOOR) >= 3, (
        f"只比對了 {checked} 個名字，而 floor 清單有 {len(_SLOT_API_FLOOR)} 個。")
    assert not missing, (
        f"`_chrome_slot` 的公開 API 抽取結果少了 {missing}（抽到的是 "
        f"{sorted(api)}）。要嘛抽取器壞了，要嘛那些函式改名／改成私有了——"
        "兩種情況下槽協定偵測器都會安靜地漏掉東西。")


def test_the_slot_detector_actually_bites():
    """正對照組。沒有這一支，下面那支在乾淨的現況上「掃到 0 個」與「偵測器整個壞掉」
    是同一個結果——空的選取看起來永遠像是通過。

    反面對照組跟正面一樣重要，而且**數量要夠**：這個判準的兩端都會出事。太窄就漏
    （`held_by_live_other` 原本就是這樣漏掉的），太寬就亂叫（`acquire` / `release`
    是通用字，`"slot" in name` 會掃到角色欄位與對話輪次），而亂叫的守門最後會被人
    關掉——那比漏掉還糟，因為連剩下那些正確的分支也一起沒了。
    """
    # --- 正面：模組別名、未解析的 receiver、包裝函式 ---
    assert _slot_protocol_calls(
        "import _chrome_slot\ndef f():\n    _chrome_slot.acquire('webrunner')\n"
    ) == {"_chrome_slot.acquire"}
    assert _slot_protocol_calls(
        "def f():\n    cs.try_acquire('webrunner')\n") == {"cs.try_acquire"}
    assert _slot_protocol_calls(
        "def f():\n    return _run_with_slot('x', run)\n") == {"_run_with_slot"}
    assert _slot_protocol_calls(
        "def f():\n    return vb._run_with_slot('x', run)\n"
    ) == {"vb._run_with_slot"}, (
        "`verify_quota_dialog.py` 真的就是這樣寫的（`vb._run_with_slot(...)`），"
        "所以屬性形式的包裝函式也要認得，不能只認裸呼叫。")
    assert _slot_protocol_calls(
        "import _chrome_slot as cs\ndef f():\n    cs.acquire('w')\n"
    ) == {"cs.acquire"}, "`import ... as` 的模組別名要解析得到"
    assert _slot_protocol_calls(
        "from axiomatic import _chrome_slot\n"
        "def f():\n    _chrome_slot.release('w')\n"
    ) == {"_chrome_slot.release"}, "`from axiomatic import X` 是本 repo 的慣用寫法"
    # ⚠️ 上面那一個**沒有鑑別力**，別以為它在測 import 解析：receiver 字面上就叫
    # `_chrome_slot`，所以就算解析整段拿掉，`"chrome_slot" in recv` 那條啟發式照樣
    # 接住它（變異 S5 就是這樣存活的）。要真的測到解析，別名必須**不含**
    # `chrome_slot`——下面這個才是那支變異的殺手。
    assert _slot_protocol_calls(
        "from axiomatic import _chrome_slot as cs\n"
        "def f():\n    cs.release('w')\n"
    ) == {"cs.release"}, (
        "別名不含 `chrome_slot` 時，唯一能認出它的就是 import 解析——"
        "`release` 是通用字，啟發式不會、也不該接住它。")

    # --- 正面：2026-09-11 補上的缺口，「問槽」也算參與槽協定 ---
    assert _slot_protocol_calls(
        "import _chrome_slot\n"
        "def f():\n    return _chrome_slot.held_by_live_other()\n"
    ) == {"_chrome_slot.held_by_live_other"}
    assert _slot_protocol_calls(
        "from _chrome_slot import held_by_live_other\n"
        "def f():\n    return held_by_live_other()\n") == {"held_by_live_other"}
    assert _slot_protocol_calls(
        "def f():\n    return x.read_holder()\n") == {"x.read_holder"}

    # --- 反面：只是提到名字、或是別的 acquire 都不算 ---
    assert _slot_protocol_calls("# _chrome_slot.acquire 在註解裡\nx = 1\n") == set()
    assert _slot_protocol_calls('S = "_chrome_slot.acquire"\n') == set()
    assert _slot_protocol_calls(
        "import threading\nL = threading.Lock()\n"
        "def f():\n    L.acquire()\n    L.release()\n"
    ) == set(), "一般的鎖不是 Chrome 槽——通用字要靠 receiver 解析，不能只看名字"
    assert _slot_protocol_calls(
        "def f(p):\n    h = p.open()\n    return h.read()\n"
    ) == set(), "`open` / `read` 也在通用字清單裡，同樣不能只看名字"
    assert _slot_protocol_calls(
        "def f():\n    remove_character_slot(3)\n"
        "    remove_all_character_slots()\n"
        "    _dorossi_resolve_turn_slot()\n"
    ) == set(), (
        "這三個是**真的存在於本專案**的名字，而且都有 `slot`，都跟 Chrome 槽無關"
        "（前兩個是角色欄位、第三個是對話輪次）。判準若退回 `\"slot\" in name`，"
        "這一行就會紅——那正是它要擋的。")


def test_an_exempt_production_launcher_still_does_not_take_the_slot():
    """例外名單對帳的**另一個方向**，而這個方向原本是永遠綠的。

    `_exemption_errors` 只問兩件事：這一筆還是不是一個 Chrome 啟動點、理由夠不夠
    具體。兩件都不會因為「被豁免的模組自己跑去取槽了」而變色。實測：今天把
    `_chrome_slot.acquire(...)` 塞進 `build_stealth_driver`，整套照樣全綠，而例外
    裡白紙黑字寫的理由——「那一刻自家 driver 還不存在，所以無條件殺光全機 chrome」
    ——已經變成假的。這正是 CLAUDE.md 記在 `_OWNER_ONLY_SLASH` 上的 fail-open 形狀。

    **為什麼「不取槽」才是對的**（不是疏漏，別好心補上去）：正式批次的互斥靠的是
    第二個訊號 `webrunner.pid`，不是槽。父行程（`start_webrunner.py` / bot）在 spawn
    的**短臨界區**內取槽 → 寫 pid → 放槽，之後整輪都由 pid 檔當存活訊號，
    `verify_browser` 讀到活著的 pid 就讓位（exit 3）。在重啟路徑上再加一組 acquire
    只會買到三個壞處：每個角色邊界可能卡住、與父行程同一個行程樹時會踩到
    `test_a_nested_release_drops_the_outer_hold` 釘住的無計次 release、而且它本來就
    是 fail-open 的，逾時後照樣往下走——排除不了任何東西。
    """
    for rel in sorted(_LAUNCHER_EXEMPT):
        path = REPO_ROOT / rel
        assert path.exists(), f"例外名單指到一個不存在的檔案：{rel}"
        calls = _slot_protocol_calls(path.read_text(encoding="utf-8"))
        assert not calls, (
            f"{rel} 被列在 `_LAUNCHER_EXEMPT` 裡（理由是「自家 driver 還不存在、"
            f"所以無條件 nuclear sweep」），但它現在會去取 Chrome 槽：{sorted(calls)}。\n"
            f"要嘛把它從例外移出來受檢，要嘛把那段拿掉——不要讓例外的理由變成假的。\n"
            f"注意判準是「**參與**槽協定」不只是「取槽」：連 `held_by_live_other()` "
            f"這種『問問槽被誰拿著』都算，因為理由裡寫的是「**無條件**」。")


def test_the_launcher_scan_finds_a_module_no_hardcoded_list_could_name(tmp_path):
    """範圍的釘子：餵一個**全新的**啟動器進去，它必須被撿起來。

    §8.8(A3)——現實資料是乾淨的，所以寬範圍與窄範圍的輸出一模一樣，「範圍真的加寬
    了」這件事沒有任何現實資料能證明。唯一的辦法是餵合成語料，而且檔名要是任何寫死
    清單都不可能包含的。三種形式都放（selenium 直接、je 包裝層、`uc.Chrome` 而且埋在
    子目錄裡），**兩個**反面對照組一起放：只是 import selenium、沒有真的起 driver
    的模組不可以被算進來（否則「一律回全部」也會讓這一支通過），以及一個從別的套件
    import `Service` / `Driver` 的模組——那兩個是通用字，判別靠的是 import 來源，
    而少了那道閘，誤判的方向是「亂叫」，亂叫的守門最後會被人關掉。
    """
    (tmp_path / "zz_new_probe_9f3c2b.py").write_text(
        "from selenium import webdriver\n"
        "def go():\n"
        "    return webdriver.Chrome()\n", encoding="utf-8")
    (tmp_path / "zz_new_je_probe_9f3c2b.py").write_text(
        "from je_web_runner import webdriver_wrapper_instance as wr\n"
        "def go():\n"
        "    wr.set_driver('chrome', options=[])\n", encoding="utf-8")
    (tmp_path / "zz_not_a_launcher_9f3c2b.py").write_text(
        "from selenium.webdriver.common.keys import Keys\n"
        "ESC = Keys.ESCAPE\n"
        "def helper(port):\n"
        "    return port.execute_script('return 1;')\n", encoding="utf-8")
    (tmp_path / "zz_generic_service_9f3c2b.py").write_text(
        "from myframework.rpc import Service, Driver\n"
        "svc = Service(port=8080)\n"
        "dev = Driver('printer')\n", encoding="utf-8")
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "zz_deep_probe_9f3c2b.py").write_text(
        "import undetected_chromedriver as uc\n"
        "d = uc.Chrome()\n", encoding="utf-8")

    assert set(_chrome_launchers([tmp_path])) == {
        "zz_new_probe_9f3c2b.py",
        "zz_new_je_probe_9f3c2b.py",
        "nested/zz_deep_probe_9f3c2b.py"}, sorted(_chrome_launchers([tmp_path]))


def test_the_launcher_scan_skips_tests_and_reference_only_code(tmp_path):
    """反面：測試模組、`legacy/`、點開頭的目錄、gitignore 的產物目錄都不算。

    測試模組**真的**會建 `ChromeService`（`test_selenium_facade.py` 為了讀
    `command_line_args()` 建了三個），但它們不開瀏覽器；把它們算進來就是會亂叫的
    守門，而會亂叫的守門最後會被人關掉。`legacy/` 依 CLAUDE.md 是唯讀參考、不在
    上線路徑上（2026-09-10 實測：裡面 `webdriver` 出現 0 次）。
    """
    launcher = "from selenium import webdriver\nd = webdriver.Chrome()\n"
    for rel in ("test_zz_probe.py", "conftest.py", "zz_real.py"):
        (tmp_path / rel).write_text(launcher, encoding="utf-8")
    for sub in ("legacy", ".venv", "output", "dorossi_workspace"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "zz_hidden.py").write_text(launcher, encoding="utf-8")
    assert set(_chrome_launchers([tmp_path])) == {"zz_real.py"}, \
        sorted(_chrome_launchers([tmp_path]))


def test_the_scan_cache_invalidates_when_a_file_changes(tmp_path):
    """掃描快取的 key 帶 mtime 與大小，所以**內容變了就要重掃**。

    這一支釘的不是效能，是正確性。把 key 換成單純的路徑（也就是把它從「同一份內容
    不必重 parse」變成一個記結果的 memo）之後，任何「先掃一次、再寫檔、再掃一次」的
    測試都會安靜地拿到過期答案——而那正是這個區塊的合成語料最常見的形狀。變異實測
    存活過，所以這支是補上來的。
    """
    probe = tmp_path / "zz_cache_probe_4a71.py"
    probe.write_text("x = 1\n", encoding="utf-8")
    assert _chrome_launchers([tmp_path]) == {}
    probe.write_text("from selenium import webdriver\nd = webdriver.Chrome()\n",
                     encoding="utf-8")
    assert set(_chrome_launchers([tmp_path])) == {"zz_cache_probe_4a71.py"}, (
        "改寫過的檔案沒有被重新掃描——快取的 key 必須帶 mtime／大小，"
        "否則它就是一個記結果的 memo，會回過期答案。")


def test_the_wrapper_form_is_not_confused_with_repointing_a_port(tmp_path):
    """`set_driver` 有兩個意思，只有帶瀏覽器名字面的那個是啟動。

    `webrunner_je_only` 的 `wr.set_driver("chrome", …)` **起**一個 Chrome；
    `webrunner_novelai` 的 `port.set_driver(driver)` 只是把 port 重新指向一個已經
    存在的 driver。分不開的話，任何用了 `BrowserPort` 的模組都會被誤判成啟動器。
    """
    (tmp_path / "zz_launch.py").write_text(
        "def go(wr):\n    wr.set_driver('chrome', options=[])\n", encoding="utf-8")
    (tmp_path / "zz_repoint.py").write_text(
        "def go(port, driver):\n    port.set_driver(driver)\n", encoding="utf-8")
    assert set(_chrome_launchers([tmp_path])) == {"zz_launch.py"}


def test_the_population_floors_each_fire_on_their_own_corpus():
    """兩條下限各要一個「只違反它自己」的語料。

    §8.8(A4)：空語料證明不了第二條——`0 >= 2` 與 `0 >= 4` 都是假，第一條先炸，第二
    條從來沒有被執行過。所以下限邏輯抽成純函式，這裡用合成輸入分別觸發。
    """
    four = {f"m{i}.py": [(1, "webdriver.Chrome", "constructor")] for i in range(4)}
    assert _population_errors(four, {}) == []

    # 只違反「總數」：3 個啟動點、0 個例外 → 受檢 3 >= 2，總數 3 < 4。
    errors = _population_errors({k: v for k, v in list(four.items())[:3]}, {})
    assert len(errors) == 1 and "全專案只掃到 3" in errors[0], errors

    # 只違反「受檢數」：4 個啟動點、3 個例外 → 總數 4 >= 4，受檢 1 < 2。
    errors = _population_errors(four, dict.fromkeys(list(four)[:3], "reason"))
    assert len(errors) == 1 and "只剩 1 個模組要受檢" in errors[0], errors

    # 兩條一起違反時兩條都要出聲（合併成一句話會讓人只修一半）。
    assert len(_population_errors({}, {})) == 2

    # 第三條下限（`_MIN_ISOLATED`）守的是**另一個**失效：`_isolated_launchers` 自己
    # 回空 dict——那時它內部的 `_population_errors` 已經跑完了、攔不住。它同樣需要
    # 對照組，否則把 `_MIN_ISOLATED` 調成 0 不會讓任何測試變紅（變異實測存活過）。
    assert _isolated_floor_error({}), "空的受檢集合沒有出聲"
    assert _isolated_floor_error({"a": 1}), "只剩一個受檢模組沒有出聲"
    assert not _isolated_floor_error({"a": 1, "b": 2})


def test_the_exemption_reconciliation_can_still_see_a_stale_entry():
    """例外對帳的對照組：現況兩筆都有效，所以主斷言刪掉不會紅。

    兩個方向各一個合成語料——指不到啟動點的死條目（**fail-open**：它豁免的模組已經
    不在受檢集合裡，差別是零），以及寫不出理由的條目（例外是這整組唯一的逃生門，
    「為什麼」必須留在原始碼裡）。
    """
    launchers = {"a.py": [(1, "webdriver.Chrome", "constructor")]}
    assert _exemption_errors({"a.py": "x" * 30}, launchers) == []
    stale = _exemption_errors({"renamed.py": "x" * 30}, launchers)
    assert len(stale) == 1 and "已經不是一個 Chrome 啟動點" in stale[0], stale
    thin = _exemption_errors({"a.py": "因為"}, launchers)
    assert len(thin) == 1 and "沒有寫下夠具體的理由" in thin[0], thin


# --- 三條規則各自的合成對照組 ----------------------------------------------
# 現實資料全綠，所以上面三支主斷言整個刪掉本來就不會紅。牙齒在這裡。

def test_the_profile_rule_can_still_see_every_way_of_breaking_it():
    """規則 1 的對照組：合規、四種違規、以及三個實測踩過的邊。"""
    good = ast.parse(
        "import tempfile\n"
        "def _make_options(profile_dir, headless):\n"
        "    '''--user-data-dir 這幾個字出現在說明裡不算數。'''\n"
        "    opts.add_argument(f'--user-data-dir={profile_dir}')\n"
        "    return opts\n"
        "def go():\n"
        "    d = tempfile.mkdtemp()\n"
        "    return _make_options(d, True)\n")
    assert _profile_isolation_errors(good) == []
    # 邊 1：說明文字裡的旗標不算 sink；邊 2：f-string 不得被重複計；
    # 邊 3：helper 的**參數**要繼承呼叫端的拋棄式來源。
    assert [(k, n) for _l, k, n in _profile_sinks(good)] == [
        ("flag", ["profile_dir"]), ("builder:_make_options", ["d"])]
    assert {"d", "profile_dir"} <= _throwaway_closure(good)

    # 違規 1：拿正式 profile。
    bad = ast.parse(
        "import tempfile\n"
        "PROD = '.chrome_profile'\n"
        "def go():\n"
        "    _ = tempfile.mkdtemp()\n"
        "    opts.add_argument(f'--user-data-dir={PROD}')\n")
    assert _profile_isolation_errors(bad), "拿正式 profile 沒有被抓到"

    # 違規 2：一個 sink 都沒有（靠 chromedriver 的隱含暫存 profile）。
    assert _profile_isolation_errors(ast.parse(
        "from selenium import webdriver\nd = webdriver.Chrome()\n")), \
        "完全沒有明示 profile 的啟動器沒有被抓到"

    # 違規 3：連 mkdtemp 都沒有。
    assert _profile_isolation_errors(ast.parse(
        "opts.add_argument('--user-data-dir=/prod/profile')\n"))

    # 違規 4：**同一個 helper 被好、壞兩種引數各叫一次**。參數傳遞若寫成 `any`，
    # 這個語料會被判成合規。
    #
    # ⚠️ 這個語料的第一版**沒有隔離到那個變異**：helper 叫 `_make_options`，於是壞的
    # 那次呼叫本身就是一個 sink、直接被抓到，`any` 與 `all` 都紅。要隔離就得讓 helper
    # **不在** `_PROFILE_BUILDERS` 裡，這樣唯一的 sink 是它內部那個 f-string，判別就
    # 只剩參數傳遞那一條。「兩道檢查互相遮蔽」在本專案是反覆出現的形狀。
    mixed = ast.parse(
        "import tempfile\n"
        "def _build_opts(profile_dir):\n"
        "    opts.add_argument(f'--user-data-dir={profile_dir}')\n"
        "    return opts\n"
        "def clean():\n"
        "    d = tempfile.mkdtemp()\n"
        "    return _build_opts(d)\n"
        "def dirty():\n"
        "    return _build_opts(PROD_PROFILE)\n")
    assert _profile_isolation_errors(mixed), (
        "同一個 options helper 被正式 profile 叫過一次，卻仍判成合規——"
        "參數傳遞必須要求**每一個**呼叫點都是拋棄式")

    # 違規 5：一個 sink 裡**混**了拋棄式與正式來源。成員檢查若寫成 `any` 就會放行，
    # 而那正是「暫存目錄底下再拼一段正式 profile 路徑」會長出來的形狀。
    blended = ast.parse(
        "import tempfile\n"
        "def go():\n"
        "    d = tempfile.mkdtemp()\n"
        "    opts.add_argument(f'--user-data-dir={d}{PROD_SUFFIX}')\n")
    assert _profile_isolation_errors(blended), (
        "profile 路徑裡混進一個追不到 mkdtemp 的名字卻放行——成員檢查必須是 `all`")


def test_the_nuclear_sweep_rule_reads_more_than_the_shape_it_was_written_from():
    """規則 2 的對照組：三種寫法都要看得見，而說明文字不算。

    舊版的判準是「Call 裡面的 Constant 剛好等於 `/IM`」——那是照著**現有寫法**寫出來
    的，不是照著威脅寫的。前兩個合成語料就是它漏掉的：模組層的常數陣列、以及整條
    命令寫成一個字串。第三個是真正的機制（依映像名指認行程）。反面那一半同樣重要：
    `verify_browser` 自己的 docstring 就寫著「不是 nuclear `/IM` 全殺」。
    """
    assert _nuclear_sweep_errors(ast.parse(
        "def go():\n    _kill_orphan_chrome()\n"))
    assert _nuclear_sweep_errors(ast.parse(
        'ARGS = ["taskkill", "/F", "/T", "/IM", "chrome.exe"]\n'))
    assert _nuclear_sweep_errors(ast.parse(
        'subprocess.run("taskkill /F /IM chrome.exe", shell=True)\n'))
    assert _nuclear_sweep_errors(ast.parse(
        'for p in psutil.process_iter():\n'
        '    if p.name() == "chromedriver.exe":\n        p.kill()\n'))
    # 說明文字裡提到不算——這正是 `verify_browser` L266／L593 的形狀。
    assert _nuclear_sweep_errors(ast.parse(
        'def f():\n'
        '    """永遠**指定 PID**、不是 nuclear `/IM` 全殺，也不碰 chrome.exe。"""\n'
        '    return 1\n')) == []
    # 指定 PID 的 taskkill 是合規的，不得被一起掃掉。
    assert _nuclear_sweep_errors(ast.parse(
        'subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)])\n')) == []


def test_the_slot_rule_can_still_see_a_launcher_that_skips_it():
    """規則 3 的對照組：少任何一個 blessed helper 都要被抓到。

    現實資料兩個模組三個都有，所以主斷言刪掉不會紅；而「直接
    `_chrome_slot.acquire()`」正是最可能被寫出來、也最貴的那個違反。
    """
    good = ast.parse(
        "def run():\n"
        "    own = _capture_own_tree(pid)\n"
        "    _reap_pids(own)\n"
        "if __name__ == '__main__':\n"
        "    _run_with_slot('probe', run)\n")
    assert _slot_and_reap_errors(good) == []
    assert _slot_and_reap_errors(ast.parse(
        "import _chrome_slot\n"
        "def run():\n"
        "    _chrome_slot.acquire('verify')\n"
        "    own = _capture_own_tree(pid)\n"
        "    _reap_pids(own)\n")), "繞過 `_run_with_slot` 沒有被抓到"
    assert _slot_and_reap_errors(ast.parse(
        "def run():\n"
        "    _reap_pids({})\n"
        "_run_with_slot('probe', run)\n")), "少了 `_capture_own_tree` 沒有被抓到"
    assert _slot_and_reap_errors(ast.parse(
        "def run():\n"
        "    _capture_own_tree(pid)\n"
        "_run_with_slot('probe', run)\n")), "少了 `_reap_pids` 沒有被抓到"


def test_the_two_production_runners_would_fail_the_isolation_rules():
    """例外名單是**載重的**，不是禮貌性的：把它清空，兩支正式批次會當場變紅。

    這一支是整組裡唯一用**現實資料**證明主斷言有牙齒的（其餘都是合成語料）。若哪天
    正式批次也改成 mkdtemp、或不再 nuclear sweep，這支會紅——那時該做的是把它從
    例外名單移出來受檢，不是把這支刪掉。
    """
    launchers = _chrome_launchers(_scan_roots())
    modules = _source_modules(_scan_roots())
    for rel in _LAUNCHER_EXEMPT:
        assert rel in launchers
        tree = _tree_at(modules[rel])
        assert _profile_isolation_errors(tree), (
            f"{rel} 的 profile 現在追得到 mkdtemp 了——正式批次不再用登入 profile 的"
            "話，它就該從 `_LAUNCHER_EXEMPT` 移出來受檢，而不是繼續掛在例外裡。")
        assert _nuclear_sweep_errors(tree), (
            f"{rel} 不再 nuclear sweep 了——同上，該重新評估它的例外資格。")


def test_the_quota_verifier_does_not_keep_its_own_copy_of_the_js():
    """被驗的三段 JS 必須是**上線的那一份**，不是抄本。

    這支工具存在的唯一理由就是「真正上線的那段 JS 在真 DOM 裡的行為」。在這裡另存
    一份 `_..._JS` 常數，測到的就只是抄本——而抄本永遠是綠的。
    """
    tree = _tree_of("verify_quota_dialog.py")
    local_js = [t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets
                if isinstance(t, ast.Name) and t.id.upper().endswith("_JS")]
    assert not local_js, f"verify_quota_dialog 自己抄了一份 JS：{local_js}"
    for fn in ("get_generation_block", "has_blocking_dialog",
               "dismiss_blocking_dialog"):
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and _dotted_name(n.func) == f"ws.{fn}"]
        assert calls, f"verify_quota_dialog 沒有透過 `ws.{fn}` 呼叫上線的那一份"


def test_the_quota_verifier_never_leaves_about_blank():
    """DOM 全部由 JS 造，不連任何外部網站——所以不會動到登入狀態，也不花額度。"""
    tree = _tree_of("verify_quota_dialog.py")
    gets = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and _dotted_name(n.func).split(".")[-1] == "get"
            and _dotted_name(n.func).startswith("drv")]
    assert gets, "verify_quota_dialog 沒有任何 `drv.get(...)`"
    for call in gets:
        arg = call.args[0] if call.args else None
        assert isinstance(arg, ast.Constant) and arg.value == "about:blank", (
            f"verify_quota_dialog:{call.lineno} 連到了 about:blank 以外的地方")


# ---------------------------------------------------------------------------
# `_pid_alive` 的行為測試（2026-09-08 補）
#
# 這是三份 `_pid_alive` 副本裡**風險最高**的一份，理由寫在它自己的 docstring 裡：
# 被探測的 pid 來自 `webrunner.pid`，而 `run_batch.py` → `start_webrunner.py` →
# webrunner 這條鏈**共用同一個 console**。Windows 上 `os.kill(pid, 0)` 不是探測，
# 是 `GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`——真的送出 Ctrl+C，會直接打斷
# 正在跑的批次產圖，也就是這支發誓絕不做的事。
#
# 那條規則此前只有 `test_pid_liveness.py` 的**靜態**掃描在守。靜態守門守不住
# 「有沒有真的生效」（本專案踩過 `if False and not decided:` 那次），所以這裡從
# 行為上證明：沒有 psutil 的 Windows 路徑上，`os.kill` **一次都沒被呼叫**。
# ---------------------------------------------------------------------------

class _KillSpy:
    """假的 `os`：只提供 `name` 與 `kill`，並記錄 `kill` 有沒有被呼叫。"""

    def __init__(self, name: str, raises: BaseException | None = None):
        self.name = name
        self.calls: list = []
        self._raises = raises

    def kill(self, pid, sig):
        self.calls.append((pid, sig))
        if self._raises is not None:
            raise self._raises


def _no_psutil(monkeypatch):
    """讓函式內的 `import psutil` 丟 ImportError。

    用 `sys.modules["psutil"] = None`（`monkeypatch.setitem` 會自動還原），
    **不是** `sys.modules.pop`——`pop` 清掉的只是快取，下一次 import 會把真的
    psutil 載回來，2026-09-07 就是那個寫法殺掉一個跑了 78.7 小時的正式批次。
    `test_suite_safety.py` 現在明文禁止 `pop`。
    """
    monkeypatch.setitem(sys.modules, "psutil", None)


def test_pid_alive_rejects_a_nonsense_pid_without_probing(monkeypatch):
    spy = _KillSpy("posix")
    monkeypatch.setattr(vb, "os", spy)
    _no_psutil(monkeypatch)
    for pid in (None, 0, -1):
        assert vb._pid_alive(pid) is False, pid
    assert spy.calls == [], "對不合法的 pid 還是去探測了"


def test_pid_alive_passes_through_psutil(monkeypatch):
    class _P:
        @staticmethod
        def pid_exists(_pid):
            return False

    monkeypatch.setitem(sys.modules, "psutil", _P)
    assert vb._pid_alive(1234) is False


def test_a_psutil_failure_reads_as_alive(monkeypatch):
    """判斷不出來要倒向「活著」＝「有正式作業在跑，我讓位」。

    這支與 `_process_control` 的答案**刻意相反**（那邊映成 False），因為兩者問的
    問題不同：這裡問「我該不該開第二個 Chrome stack」，答錯成「沒人在跑」的代價是
    賠掉一個已經跑了好幾天的批次。
    """
    class _Boom:
        @staticmethod
        def pid_exists(_pid):
            raise RuntimeError("psutil 壞了")

    monkeypatch.setitem(sys.modules, "psutil", _Boom)
    assert vb._pid_alive(1234) is True


def test_on_windows_without_psutil_it_uses_ctypes_and_never_os_kill(monkeypatch):
    """**整組裡最重要的一支。**

    Windows 上必須走 `_nt_pid_alive`（純查詢的 ctypes），**絕不能碰 `os.kill`**。
    只斷言回傳值不夠：一個「先 kill 再回答」的實作也會回出正確的值，而它每次探測
    都會對共用 console 的整組行程送出一次真正的 Ctrl+C——正在跑的批次會當場被打斷。
    """
    spy = _KillSpy("nt")
    monkeypatch.setattr(vb, "os", spy)
    _no_psutil(monkeypatch)
    seen: list = []
    monkeypatch.setattr(vb, "_nt_pid_alive",
                        lambda pid: (seen.append(pid), True)[1])
    assert vb._pid_alive(1234) is True
    assert seen == [1234], f"沒有走 `_nt_pid_alive`：{seen}"
    assert spy.calls == [], (
        "Windows 上呼叫了 `os.kill`——signal 0 會對共用 console 的整組行程送出"
        "真正的 Ctrl+C，正在跑的批次會被打斷。這是 CLAUDE.md 的跨領域硬規則")


def test_on_posix_without_psutil_it_probes_with_signal_zero(monkeypatch):
    spy = _KillSpy("posix")
    monkeypatch.setattr(vb, "os", spy)
    _no_psutil(monkeypatch)
    assert vb._pid_alive(4321) is True
    assert spy.calls == [(4321, 0)], f"探測方式不對：{spy.calls}"


def test_posix_probe_errors_keep_the_conservative_direction(monkeypatch):
    """POSIX 的三種例外：只有「確定不存在」才回 False，其餘一律保守回 True。"""
    for error, expected in ((ProcessLookupError(), False),
                            (PermissionError(), True),
                            (OSError(), True)):
        spy = _KillSpy("posix", raises=error)
        monkeypatch.setattr(vb, "os", spy)
        _no_psutil(monkeypatch)
        assert vb._pid_alive(4321) is expected, type(error).__name__
        assert spy.calls == [(4321, 0)]


# ---------------------------------------------------------------------------
# 「絕不按到會花錢的按鈕」——四份清單描述同一個現實，而沒有人對帳
#
# `verify_quota_dialog.py` 的模組 docstring 把本專案**最貴**的一條性質寫成一句話：
#
#     關閉動作絕對不能按到會花錢的按鈕。
#
# 而「哪些按鈕會花錢」現在同時記在**四個地方**：
#
#   1. `_webrunner_shared._DISMISS_DIALOG_JS` 的 `FORBIDDEN` 正規式——上線的判準。
#   2. `test_webrunner_shared._REAL_PAYING_LABELS`——縱深防禦的對照組。
#   3. `verify_quota_dialog._PAYING_LABELS` / `_ACCOUNT_LABELS`——真瀏覽器驗證
#      拿來斷言的字。
#   4. `verify_quota_dialog._REAL_DIALOG_HTML` / `_ACCOUNT_DIALOG_HTML`——真的長
#      出那些按鈕的 fixture，外加 `_PAYING_PROBES` 這份探針編號清單。
#
# 2026-09-09 逐一比對過：**四份現在完全一致**。要釘的不是現況，是「沒有任何東西
# 讓它保持一致」，而每一個漂移方向都是安靜的：
#
#   * fixture 加了一顆新的付費鈕、`_PAYING_PROBES` 沒跟上 → 那顆按鈕在真瀏覽器裡
#     確實長出來了，但沒有任何一條斷言問「你有沒有挑中它」。驗證照樣印 OK。
#   * verify 端的清單加了字、`FORBIDDEN` 沒跟上 → 單元測試那側有守門，但它守的是
#     **它自己那份**清單，verify 端加的字不在它的視線裡。
#   * 兩份清單各自漂移 → 兩邊都綠，各自測著自己的一半。
#
# 判準全部走 AST。每一支都先斷言抽取器真的抽到東西——空集合的「沒有不一致」跟
# 「完全一致」在輸出上一模一樣，而現況乾淨代表把主斷言整個刪掉本來就不會紅，所以
# 牙齒長在 `_dialog_controls` 這支純函式上，另外用合成原始碼問它看不看得見漂移。
# ---------------------------------------------------------------------------

def _str_tuple(tree, name: str) -> tuple:
    """`NAME = ("a", "b")` → `("a", "b")`。抽不到就 raise（fail-closed）。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name
                   for t in node.targets):
            continue
        if isinstance(node.value, (ast.Tuple, ast.List)):
            return tuple(e.value for e in node.value.elts
                         if isinstance(e, ast.Constant))
    raise AssertionError(f"抽不到 {name}——常數改名了，這支對帳就成了空轉")


def _text_arg_index(tree, func_name: str) -> int:
    """`func_name` 的 `text` 參數排第幾個。

    **刻意不寫死成 6。** 兩支造控制項的 helper 現在剛好都把 `text` 放在第 7 個
    位置參數，但那是巧合不是契約；寫死的話，有人插一個參數進去就會讓這支安靜地
    去讀寬度、於是每一顆按鈕都變成「沒有字」＝安全，對帳全綠。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            names = [a.arg for a in node.args.args]
            if "text" in names:
                return names.index("text")
            raise AssertionError(f"{func_name} 沒有 `text` 參數了")
    raise AssertionError(f"找不到 {func_name}")


def _dialog_controls(tree, const_name: str) -> list:
    """fixture 常數裡每一顆控制項 → `[(probe ids, label)]`。

    純函式（只吃 AST），所以合成原始碼問得到它。`label` 為空字串代表那顆是
    **語意空白的純圖示鈕**——也就是關閉鈕，規則 3 唯一該挑中的東西。

    回傳照**原始碼順序**排。`ast.walk` 是廣度優先，順序跟著運算式的巢狀深度跑；
    不排的話失敗訊息會像亂數，而且對照組得寫成「順序無關」——那就順帶把「有沒有
    漏掉一顆」也一起放過了。
    """
    idx = {name: _text_arg_index(tree, name)
           for name in ("_abs_ctl", "_nested_ctl")}
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == const_name
                   for t in node.targets):
            continue
        for call in ast.walk(node.value):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in idx):
                continue
            at = idx[call.func.id]
            label = ""
            if len(call.args) > at and isinstance(call.args[at], ast.Constant):
                label = call.args[at].value
            for kw in call.keywords:
                if kw.arg == "text" and isinstance(kw.value, ast.Constant):
                    label = kw.value.value
            # `_abs_ctl(tag, probe, …)` 的探針是第 2 個引數；
            # `_nested_ctl(outer, inner, …)` 一次造兩顆，兩個都要算。
            probes = ([call.args[1].value] if call.func.id == "_abs_ctl"
                      else [call.args[0].value, call.args[1].value])
            out.append(((call.lineno, call.col_offset), probes, label))
    return [(probes, label) for _pos, probes, label in sorted(out)]


def _dangerous_labels() -> tuple[set, set]:
    """`(第一層付費牆, 第二層帳號管理)` 兩組會造成損害的字。"""
    tree = _tree_of("verify_quota_dialog.py")
    return (set(_str_tuple(tree, "_PAYING_LABELS")),
            set(_str_tuple(tree, "_ACCOUNT_LABELS")))


def test_the_fixture_and_the_probe_list_agree():
    """fixture 裡**有字**的每一顆，探針編號都必須在 `_PAYING_PROBES` 裡。

    這是四條對帳裡最要緊的一條：`_PAYING_PROBES` 是「驗證會去斷言的按鈕」清單，
    fixture 是「真瀏覽器裡真的會長出來的按鈕」。fixture 多一顆而清單沒跟上，等於
    在真 DOM 裡放了一顆沒有人看守的付費鈕。
    """
    tree = _tree_of("verify_quota_dialog.py")
    controls = _dialog_controls(tree, "_REAL_DIALOG_HTML")
    # 正面對照組：抽不到東西時，「沒有不一致」與「完全一致」長得一樣。
    assert len(controls) >= 8, f"只抽到 {len(controls)} 顆控制項，抽取器壞了"
    labelled = sorted(p for probes, label in controls if label for p in probes)
    declared = sorted(_str_tuple(tree, "_PAYING_PROBES"))
    assert labelled == declared, (
        "fixture 裡有字的探針與 `_PAYING_PROBES` 對不起來：\n"
        f"  只在 fixture：{sorted(set(labelled) - set(declared))}\n"
        f"  只在清單裡：{sorted(set(declared) - set(labelled))}")


def test_the_real_dialog_fixture_carries_the_copy_the_site_actually_shows():
    """`_REAL_DIALOG_HTML` 的說明文字必須是 `QUOTA_TEXT_REAL`，不是舊猜想文案。

    這份 fixture 的賣點是「原樣重現 log 裡那一份 856x917 的對話框」。到
    2026-09-09 為止它的**按鈕幾何是真的、文字是假的**：配的是
    `Not enough Anlas…`，而那句話在 `WEBRunner.log`（08-24 → 09-09、259 次被擋）
    裡出現 **0 次**；站方真正端出來的是 `The paint's run dry…`。

    這一支釘的是「fixture 用的是實際文案」——不是「兩份文案都存在」。舊文案照樣
    要留著（它守 `/not enough anlas/i`、零誤判），只是不該冒充成正式那一份。
    """
    tree = _tree_of("verify_quota_dialog.py")
    assigns = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    assigns[tgt.id] = node.value
    for name in ("QUOTA_TEXT", "QUOTA_TEXT_REAL", "_REAL_DIALOG_HTML",
                 "NON_BLOCKING_TEXTS"):
        assert name in assigns, f"verify_quota_dialog 少了 {name}"
    old = ast.literal_eval(assigns["QUOTA_TEXT"])
    real = ast.literal_eval(assigns["QUOTA_TEXT_REAL"])
    assert old != real, "兩份文案一樣的話，其中一份就沒有在守任何東西"
    assert "paint" in real, f"實際文案應該是 log 裡那一份：{real!r}"
    # 撇號是 U+2019。寫成 ASCII `'` 在 regex 那側仍然接得住（`.{0,3}`），但這份
    # fixture 的用途是重現，重現就要一個位元組都一樣。
    assert "’" in real, "實際文案的撇號是彎引號 U+2019，不是 ASCII 單引號"

    used = {n.id for n in ast.walk(assigns["_REAL_DIALOG_HTML"])
            if isinstance(n, ast.Name)}
    assert "QUOTA_TEXT_REAL" in used, (
        "`_REAL_DIALOG_HTML` 沒有用 `QUOTA_TEXT_REAL` 當說明文字——它宣稱重現的是"
        "正式對話框，配一份站方從來沒端出來過的文字等於重現不成立")
    assert "QUOTA_TEXT" not in used, (
        "`_REAL_DIALOG_HTML` 又配回舊的猜想文案了")

    corpus = ast.literal_eval(assigns["NON_BLOCKING_TEXTS"])
    assert len(corpus) >= 5, (
        f"反面語料只剩 {len(corpus)} 筆——誤判那一半跟正面一樣重要")


def test_the_fixture_and_the_label_lists_agree():
    """兩層 fixture 裡出現的字，必須剛好就是兩份字面清單。"""
    tree = _tree_of("verify_quota_dialog.py")
    paying, account = _dangerous_labels()
    assert paying and account, "字面清單是空的，下面等於沒在檢查"
    for const, declared in (("_REAL_DIALOG_HTML", paying),
                            ("_ACCOUNT_DIALOG_HTML", account)):
        controls = _dialog_controls(tree, const)
        assert controls, f"{const} 一顆控制項都沒抽到，抽取器壞了"
        found = {label for _probes, label in controls if label}
        assert found == declared, (
            f"{const} 裡的字與清單對不起來：\n"
            f"  只在 fixture：{sorted(found - declared)}\n"
            f"  只在清單裡：{sorted(declared - found)}")


def test_both_dialogs_still_contain_a_semantically_empty_close_button():
    """每一層 fixture 都必須留著那顆**語意完全空白**的關閉鈕。

    它是規則 3 唯一該挑中的東西，也是整組情境的正面路徑。fixture 若被改到只剩下
    危險按鈕，「一顆都沒挑」會被讀成安全，而那其實代表關閉功能整個失效——真實後果
    是每個額度週期都要付一次整頁 reload。
    """
    tree = _tree_of("verify_quota_dialog.py")
    for const in ("_REAL_DIALOG_HTML", "_ACCOUNT_DIALOG_HTML"):
        empties = [probes for probes, label in _dialog_controls(tree, const)
                   if not label]
        assert empties, f"{const} 裡沒有語意空白的關閉鈕了"


def test_the_verifier_and_the_unit_test_agree_on_the_dangerous_buttons():
    """真瀏覽器驗證與單元測試必須看著**同一份**危險按鈕清單。

    兩邊各自漂移的話兩邊都會綠，各自測著自己的一半——而那正是「兩道防護互相遮蔽」
    在本專案反覆出現的形狀。
    """
    paying, account = _dangerous_labels()
    union = paying | account
    unit = set(tws._REAL_PAYING_LABELS)
    assert len(union) >= 5 and len(unit) >= 5, (
        f"清單小到不像真的（verify={len(union)}, unit={len(unit)}）")
    assert union == unit, (
        "危險按鈕清單漂移了：\n"
        f"  只在 verify_quota_dialog：{sorted(union - unit)}\n"
        f"  只在 test_webrunner_shared：{sorted(unit - union)}")


def test_every_dangerous_label_is_blocked_by_the_shipped_forbidden_regex():
    """verify 端清單裡的每一個字，都必須被**上線的** `FORBIDDEN` 擋下。

    既有的 `test_forbidden_alone_still_blocks_every_paying_button` 問的是同一件
    事，但它拿的是**它自己那份**清單；在 verify 端新增一個字不會進它的視線。
    """
    forbidden = tws._dismiss_regexes()["FORBIDDEN"]
    paying, account = _dangerous_labels()
    labels = sorted(paying | account)
    assert labels, "清單是空的，下面的迴圈會空轉通過"
    missed = [lab for lab in labels if not forbidden.search(lab)]
    assert not missed, (
        f"這些字只靠「文字必須為空」擋著，`FORBIDDEN` 沒收：{missed}。"
        "那一條同時是規則 3 的功能條件，有人放寬它就會直接去按這幾顆")


def test_the_fixture_extractor_can_still_see_a_drift():
    """對照組：拿合成原始碼問抽取器「你還看得見漂移嗎」。

    現況四份全對，所以上面那些主斷言整個刪掉**也不會紅**。牙齒在這裡。
    """
    src = (
        "def _abs_ctl(tag, probe, x, y, w, h, text='', attrs='', inner=''):\n"
        "    return ''\n"
        "def _nested_ctl(a, b, x, y, w, h, text='', inner_attrs=''):\n"
        "    return ''\n"
        "FIX = (_nested_ctl('#1', '#2', 8, 2, 3, 3)\n"
        "       + _abs_ctl('button', '#3', 4, 2, 1, 3, 'Subscribe')\n"
        "       + _abs_ctl('button', '#4', 4, 2, 1, 3, text='Buy Now'))\n")
    tree = ast.parse(src)
    got = _dialog_controls(tree, "FIX")
    assert got == [(["#1", "#2"], ""),
                   (["#3"], "Subscribe"),
                   (["#4"], "Buy Now")], got
    # 位置參數與具名參數兩種寫法都要看得到，否則「用 keyword 寫」就能繞過對帳。
    assert {label for _p, label in got if label} == {"Subscribe", "Buy Now"}
    # 關閉鈕（label 為空）必須被辨認出來，而不是漏掉。
    assert [p for p, label in got if not label] == [["#1", "#2"]]

    # `text` 的位置是**查出來的**，不是寫死的：把參數往後挪一格之後仍要讀對。
    moved = ast.parse(
        "def _abs_ctl(tag, probe, x, y, w, h, extra='', text=''):\n"
        "    return ''\n"
        "def _nested_ctl(a, b, x, y, w, h, text=''):\n"
        "    return ''\n"
        "FIX = _abs_ctl('button', '#9', 1, 2, 3, 4, 'ignored', 'Anlas')\n")
    assert _dialog_controls(moved, "FIX") == [(["#9"], "Anlas")]


def test_the_dangerous_label_extractor_fails_closed_on_a_rename():
    """常數被改名時，抽取器必須**大聲失敗**而不是回空集合。

    回空集合的話，上面每一支對帳都會空轉通過——「沒有不一致」與「沒有資料」在
    斷言的輸出上分不出來，而這正是本專案反覆踩到的那個形狀。
    """
    tree = ast.parse("_SOMETHING_ELSE = ('a',)\n")
    for name in ("_PAYING_LABELS", "_PAYING_PROBES", "_ACCOUNT_LABELS"):
        try:
            _str_tuple(tree, name)
        except AssertionError:
            continue
        raise AssertionError(f"{name} 抽不到時沒有失敗，而是安靜地回了值")


# ---------------------------------------------------------------------------
# 外科式清理：PID 會被回收，所以「殺一個先前記下的 pid」必須連身分一起記
# ---------------------------------------------------------------------------
# 為什麼這一族存在（2026-09-09 實測，psutil 7.2.2 / CPython 3.14 / Windows）：
#
# `_capture_own_tree` 在 `driver.quit()` **之前**抓下整棵樹，`_reap_pids` 在 quit
# **之後**才動手——而 quit 那一步正好讓幾十個 chrome 子行程結束、把它們的 PID 釋放
# 回池子。原本 reap 寫的是 `psutil.Process(pid).kill()`：事後才用裸 pid 重建的
# `Process` 物件**沒有任何舊身分可比**，所以殺的是「現在」那個 pid 的擁有者。
#
# 實測把兩種寫法擺在一起看得最清楚：
#   * capture 當下建立的 `Process` 物件留著再 `kill()` → psutil 丟
#     `NoSuchProcess: process no longer exists and its PID has been reused`，
#     目標活下來（psutil 的 `Process.__eq__` 比的是 `(pid, create_time)`）；
#   * 事後用裸 pid 重建 → **一定殺得下去**。
#
# 這台機器同時跑著兩個正式批次的 Chrome，誤殺的代價是中斷一個已經跑了幾十小時的
# 批次——而這支腳本整段設計（模組 docstring 的第三條硬性不變條件）就是「寧可漏殺，
# 不可錯殺」。所以 capture 改成回 `{pid: create_time}`，reap 殺之前先對身分。
#
# 這一族刻意**不** import psutil：假的行程物件更能把「身分對不上」這件事講清楚，
# 也不會在沒有 psutil 的機器上變成跳過（跳過的測試跟通過的測試長得一模一樣）。

class _FakePsutilProcess:
    """最小的 psutil.Process 替身：認得 `create_time()` / `kill()` / `children()`。"""

    def __init__(self, table, pid):
        self._table = table
        self.pid = pid
        if pid not in table:
            raise table["__nosuch__"](pid)

    def create_time(self):
        return self._table[self.pid]["born"]

    def children(self, recursive=False):       # noqa: ARG002
        return [_FakePsutilProcess(self._table, kid)
                for kid in self._table[self.pid].get("kids", ())]

    def kill(self):
        self._table[self.pid]["killed"] = True


class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


def _fake_psutil(table):
    """做一個假的 `psutil` 模組，`import psutil` 會拿到它。"""
    import types

    module = types.ModuleType("psutil")
    table["__nosuch__"] = _FakeNoSuchProcess
    module.Process = lambda pid: _FakePsutilProcess(table, pid)
    module.NoSuchProcess = _FakeNoSuchProcess
    module.AccessDenied = _FakeAccessDenied
    return module


def _with_fake_psutil(monkeypatch, table):
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(table))


# 兩份抄本：驗證腳本自己那一份，以及背景程式 opt-in 驗證模式那一份。行為規則相同，
# 所以行為測試對兩份都跑一次——只修好一份是這一輪已經踩過的形狀
# （`verify_quota_dialog` 的 `TRANSPORT_ERRORS` 被漏掉三個月）。
# `webrunner_novelai` 在**函式內**才 import：它會拉進 selenium / je_web_runner，
# 而本檔其餘部分是純 AST、collection 很快。`test_chrome_recovery.py` 已經證明模組
# 層 import 這兩支是安全的，這裡只是不想讓本檔跟著變慢。
def _reapers():
    import webrunner_novelai as wn  # noqa: PLC0415

    return (("verify_browser", vb._capture_own_tree, vb._reap_pids),
            ("webrunner_novelai", wn._verify_capture_tree, wn._verify_reap_tree))


def test_the_capture_records_identity_not_just_a_pid(monkeypatch):
    """抓下來的必須是 `{pid: 建立時間}`，而不是一組裸 pid。

    只有一組 pid 的話，reap 那一端**沒有東西可以比對**——身分檢查不是「忘了做」，
    是根本做不到。所以這一支釘的是資料形狀，它是下面那支的前提。
    """
    for label, capture, _reap in _reapers():
        table = {100: {"born": 1.0, "kids": (101, 102)},
                 101: {"born": 2.0}, 102: {"born": 3.0}}
        _with_fake_psutil(monkeypatch, table)
        got = capture(100)
        assert got == {100: 1.0, 101: 2.0, 102: 3.0}, (label, got)


def test_a_recycled_pid_is_not_killed(monkeypatch):
    """capture 之後 PID 被回收給別的行程 → **不准**殺它。

    這是整族的重點。舊寫法在這裡會殺掉那個無關的行程，而在這台機器上那可能是一個
    正在跑的正式批次的 Chrome。
    """
    for label, capture, reap in _reapers():
        table = {100: {"born": 1.0, "kids": (101,)}, 101: {"born": 2.0}}
        _with_fake_psutil(monkeypatch, table)
        tree = capture(100)

        # 101 死了，PID 被回收給另一個行程（同一個 pid、不同的建立時間）。
        table[101] = {"born": 999.0}
        killed = reap(tree)

        assert table[100].get("killed") is True, f"{label}：自己那棵樹沒被收掉"
        assert table[101].get("killed") is not True, (
            f"{label}：殺掉了一個 PID 被回收後的陌生行程——這正是這一族要擋的事")
        assert killed == 1, (label, killed)


def test_an_unidentifiable_pid_is_skipped_rather_than_killed(monkeypatch, capsys):
    """建立時間讀不到（`None`）＝身分無法確認 → 跳過，不要「先殺再說」。

    方向是刻意的：留下一個帶著拋棄式 profile 的 orphan 是無害的
    （`_rm_profile` 本來就容忍殘留），殺錯一個行程不是。

    **也釘住診斷文字**，而且這一條是變異測試逼出來的：把 `if born is None:
    continue` 那條拿掉，行程**仍然不會**被殺（`!= born` 本來就擋得住，任何真實的
    建立時間都不等於 `None`），所以只斷言「沒被殺」的話那個變異會存活。那條分支
    真正提供的是**分得開的診斷**——「身分沒抓到」（權限問題）與「PID 被回收」
    （主機忙）是兩種要往不同方向查的狀態。
    """
    table = {100: {"born": 1.0}}
    _with_fake_psutil(monkeypatch, table)
    assert vb._reap_pids({100: None}) == 0
    assert table[100].get("killed") is not True
    out = capsys.readouterr().out
    assert "身分沒抓到" in out, (
        f"沒有印出「身分沒抓到」這一種診斷：{out!r}。"
        "它跟「已被回收給別的行程」是兩種不同的主機狀態，印同一句話等於看 log 的"
        "人分不出該查哪一邊。")
    assert "已被回收" not in out, out


def test_a_recycled_pid_says_so_instead_of_saying_it_was_unidentifiable(
        monkeypatch, capsys):
    """反方向：真的被回收時要印「已被回收」，不可以跟「身分沒抓到」混用。

    兩支對著看才擋得住「把兩條分支合成一句話」那種簡化。
    """
    table = {100: {"born": 1.0}}
    _with_fake_psutil(monkeypatch, table)
    assert vb._reap_pids({100: 999.0}) == 0
    out = capsys.readouterr().out
    assert "已被回收" in out, out
    assert "身分沒抓到" not in out, out


def test_the_reap_still_kills_the_tree_it_really_owns(monkeypatch):
    """反方向：身分對得上就要照殺。

    少了這一支，把身分比較的方向弄反（或把 reap 改成什麼都不殺）會全綠——那樣殘留
    會累積，而這兩支的清理職責就沒了。
    """
    for label, capture, reap in _reapers():
        table = {100: {"born": 1.0, "kids": (101, 102)},
                 101: {"born": 2.0}, 102: {"born": 3.0}}
        _with_fake_psutil(monkeypatch, table)
        tree = capture(100)
        assert reap(tree) == 3, (label, table)
        assert all(table[pid].get("killed") for pid in (100, 101, 102)), (
            label, table)


def test_a_process_that_already_exited_is_not_an_error(monkeypatch):
    """capture 到 reap 之間自己死掉（PID 沒被回收）→ 安靜跳過、不影響其他人。

    這是最常見的情況：`driver.quit()` 本來就會收掉大部分的樹。
    """
    for label, capture, reap in _reapers():
        table = {100: {"born": 1.0, "kids": (101,)}, 101: {"born": 2.0}}
        _with_fake_psutil(monkeypatch, table)
        tree = capture(100)
        del table[101]                      # 已經不在了
        assert reap(tree) == 1, label
        assert table[100].get("killed") is True, label


def test_both_surgical_reapers_check_identity_before_killing():
    """兩份抄本都要做身分檢查——規則跟模組無關，抄本卻有兩份。

    `verify_browser._reap_pids` 與 `webrunner_novelai._verify_reap_tree` 是同一段
    邏輯的兩份實作（一份在驗證腳本、一份在背景程式的 opt-in 驗證模式），而兩邊都
    不能單一來源：`verify_browser` 刻意不 import 任何 webrunner 變體。所以守門要
    同時盯住兩份，否則修好一份、另一份安靜地留著同一個缺陷——這正是同一天在
    `TRANSPORT_ERRORS` 上已經踩過一次的形狀（`verify_quota_dialog` 那份被漏掉了
    三個月）。

    判準是**機制**：kill 之前必須出現對 `create_time` 的比較。
    """
    targets = (("verify_browser.py", "_reap_pids"),
               ("webrunner_novelai.py", "_verify_reap_tree"))
    for module, func in targets:
        tree = _tree_of(module)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == func), None)
        assert fn is not None, f"{module} 裡找不到 {func}——改名了嗎？"
        compares = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
                    and any(isinstance(side, ast.Call)
                            and _dotted_name(side.func).split(".")[-1]
                            == "create_time"
                            for side in [n.left] + list(n.comparators))]
        assert compares, (
            f"{module}.{func} 在 kill 之前沒有比對 `create_time()`。"
            "PID 會被作業系統回收，裸 pid 殺下去可能命中一個無關的行程"
            "（這台機器上那可能是正在跑的正式批次的 Chrome）。")
        kills = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and _dotted_name(n.func).split(".")[-1] == "kill"]
        assert kills, f"{module}.{func} 根本沒有在殺東西——抽取器壞了嗎？"


# ---------------------------------------------------------------------------
# CLI 契約：模式選擇、參數傳遞，以及「一定要印一行機器可讀結論」
# ---------------------------------------------------------------------------
# 這一族補的是一個實際的空白：`main()` 與 `_build_parser()` 在 2026-09-09 之前
# **一行都沒有被執行過**（實測 coverage）。而這支腳本的整個價值就建立在那個 CLI
# 契約上——驗證守則叫人「先跑它，看那一行結論」。模式選
# 錯（`--full` 被讀成 smoke）的症狀特別壞：它會印一個看起來很正常的 `OK`，而實際上
# 端到端那一半根本沒跑。

def _run_main(monkeypatch, argv):
    """跑 `main(argv)`，把真正會開瀏覽器的兩支換成記錄器。回 `(rc, 記錄, 輸出)`。"""
    seen = {}

    def _fake_slot(label, run_fn):
        seen["label"] = label
        return run_fn()

    def _fake_smoke(url, headless):
        seen["smoke"] = {"url": url, "headless": headless}
        return vb.EXIT_OK

    def _fake_full(generate):
        seen["full"] = {"generate": generate}
        return vb.EXIT_OK

    def _fake_smoke_je(url, headless):
        seen["smoke_je"] = {"url": url, "headless": headless}
        return vb.EXIT_OK

    monkeypatch.setattr(vb, "_run_with_slot", _fake_slot)
    monkeypatch.setattr(vb, "run_smoke", _fake_smoke)
    monkeypatch.setattr(vb, "run_full", _fake_full)
    monkeypatch.setattr(vb, "run_smoke_je", _fake_smoke_je)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = vb.main(argv)
    return rc, seen, buffer.getvalue()


def test_the_default_invocation_is_a_headless_smoke_run(monkeypatch):
    """沒有參數 = smoke、headless、about:blank。這是驗證守則寫的那條預設路徑。"""
    rc, seen, _out = _run_main(monkeypatch, [])
    assert rc == vb.EXIT_OK, rc
    assert seen["label"] == "smoke", seen
    assert seen["smoke"] == {"url": "about:blank", "headless": True}, seen
    assert "full" not in seen, "預設居然走了 full"


def test_headed_and_url_reach_the_smoke_runner(monkeypatch):
    """兩個 smoke 專用旗標都要真的傳下去。

    `--headed` 特別值得釘：它在 `main` 裡是 `headless=not args.headed` 的**取反**，
    而取反寫錯不會有任何症狀——瀏覽器照樣起得來，只是驗證的環境跟正式的不一樣。
    """
    rc, seen, _out = _run_main(
        monkeypatch, ["--headed", "--url", "https://example.invalid/x"])
    assert rc == vb.EXIT_OK, rc
    assert seen["smoke"] == {"url": "https://example.invalid/x",
                             "headless": False}, seen


@pytest.mark.parametrize("argv", [["--full"], ["--mode", "full"]])
def test_both_spellings_of_full_reach_the_full_runner(monkeypatch, argv):
    """`--full` 與 `--mode full` 是同一件事——兩種寫法都在模組 docstring 的契約裡。

    只支援其中一種的症狀是最糟的那一類：另一種寫法會**安靜地跑 smoke**，印一個
    貨真價實的 `OK`，而端到端那一半根本沒發生。
    """
    rc, seen, _out = _run_main(monkeypatch, argv)
    assert rc == vb.EXIT_OK, rc
    assert seen["label"] == "full", (argv, seen)
    assert "smoke" not in seen, (argv, seen)
    assert seen["full"] == {"generate": False}, seen


def test_generate_is_off_unless_asked_for(monkeypatch):
    """`--generate` 會真的送出一次生成（花額度），所以預設必須是關的。"""
    _rc, off, _ = _run_main(monkeypatch, ["--full"])
    assert off["full"]["generate"] is False, off
    _rc, on, _ = _run_main(monkeypatch, ["--full", "--generate"])
    assert on["full"]["generate"] is True, on


def test_an_unexpected_exception_still_prints_a_result_line(monkeypatch):
    """硬性契約：這支**一定**要印一行 `VERIFY-BROWSER:`。

    `main` 的兜底 `except Exception` 就是為此存在（模組裡列了幾條會逃出來的路徑：
    `_chrome_slot.acquire` 的 OSError、`subprocess.Popen` 起不來、psutil 的非預期
    例外）。少了它，呼叫端 grep 不到結論行，會把噴 traceback 讀成「腳本靜默」。
    """
    def _boom(_label, _run_fn):
        raise OSError("slot file vanished")

    monkeypatch.setattr(vb, "_run_with_slot", _boom)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = vb.main([])
    out = buffer.getvalue()
    assert rc == vb.EXIT_FAIL, rc
    lines = [l for l in out.splitlines() if l.startswith(vb.RESULT_PREFIX)]
    assert len(lines) == 1, f"沒有印出剛好一行結論：{out!r}"
    assert lines[0].split()[1] == "FAIL", lines[0]


def test_ctrl_c_is_not_swallowed_by_the_catch_all(monkeypatch):
    """兜底刻意只接 `Exception`——`KeyboardInterrupt` 必須照常往上傳。

    反方向的對照：把它寫成 `except BaseException` 會讓 Ctrl+C 印出一行假的 FAIL
    然後正常結束，而使用者按的是「停下來」。
    """
    def _interrupt(_label, _run_fn):
        raise KeyboardInterrupt

    monkeypatch.setattr(vb, "_run_with_slot", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        vb.main([])


def test_calling_main_without_arguments_does_not_read_pytests_command_line():
    """`main()` 不得去解析**跑測試的那條命令列**。

    argparse 收到 `argv=None` 的預設行為就是讀 `sys.argv[1:]`，在 pytest 底下那是
    `-q`、`--timeout=300`… → `error: unrecognized arguments` → `SystemExit(2)`。
    這在 `audit_dependencies` 上真的踩過（六支無關的既有測試同時轉紅），所以本專案
    的約定是「`argv or []`」。這一支是它在這個檔案的行為釘子；靜態那一半在
    `test_suite_safety.test_no_entry_point_falls_through_to_the_test_runners_command_line`。
    """
    parsed = vb._build_parser().parse_args([])
    assert parsed.mode == "smoke" and parsed.url == "about:blank"
    # 直接證明 `main()` 不會因為 pytest 的旗標而 SystemExit。
    calls = []
    original = vb._run_with_slot
    try:
        vb._run_with_slot = lambda label, _fn: calls.append(label) or vb.EXIT_OK
        assert vb.main() == vb.EXIT_OK
    finally:
        vb._run_with_slot = original
    assert calls == ["smoke"], calls


@pytest.mark.parametrize("output, expected", [
    ("VERIFY-SETUP: FAIL 登入失敗（profile 未登入或憑證無效）",
     "登入失敗（profile 未登入或憑證無效）"),
    ("noise\n  VERIFY-SETUP: FAIL 導航逾時  \nmore noise", "導航逾時"),
    ("VERIFY-SETUP: OK", ""),
    ("", ""),
    ("VERIFY-SETUP: FAIL", ""),
])
def test_the_setup_failure_reason_is_extracted_from_the_child_output(
        output, expected):
    """`--full` 的原因字串來自子行程那一行，抽錯就會回報一個**空的**失敗原因。

    子行程的輸出是被轉印的，所以前後都有雜訊；抽取要認得縮排，也要在沒有那一行時
    安靜回空字串（而不是丟例外——那會讓 `run_full` 的收尾整段炸掉）。
    """
    assert vb._extract_setup_fail(output) == expected


# ---------------------------------------------------------------------------
# `verify_browser.py --full` 在子行程裡跑的那一段：`webrunner_novelai.py` 的
# `_run_setup_verification()`。
#
# **為什麼守在這裡，而不是併進上面那個啟動點掃描。** `_LAUNCHER_EXEMPT` 是
# **以檔案為單位**的，而 `axiomatic/webrunner_novelai.py` 整個檔被列為例外，理由
# 寫的是「正式批次……刻意 nuclear sweep……profile 用的是正式登入態的 snapshot」。
# 那段理由**每一句都是真的**——但它描述的是 `main()` 那條路。同一個檔裡還有
# `_run_setup_verification()`，那是一條**非正式、要求隔離**的路，它的 docstring 對
# 上面三條規則給的答案正好全部相反。於是「檔案被豁免」＝「這個函式的隔離保證沒有
# 任何東西在檢查」，而 2026-09-10 實查：全 repo 對這個函式的測試參照是 **0**。
# 這是 §8.8「守門的範圍」的又一例，形狀是新的：**例外的理由是對的，仍然錯，因為它
# 沒有涵蓋那個檔裡的全部內容。**
#
# 為什麼不是直接把掃描改成以函式為單位：那三條規則（拋棄式 mkdtemp profile、自己
# 取 Chrome 槽、自己收自己的行程樹）對這個函式**會全部誤判**——它的隔離是
# **繼承來的**（父行程 `verify_browser.py` 先取了槽），profile 目的地是父行程用環境
# 變數指定的暫時目錄而不是它自己 mkdtemp 的。照抄規則會得到三筆誤判，而本專案的
# 立場一向是「會亂叫的守門，遲早被人關掉」。所以這裡照 §8.8(A13) 把規則**改寫到
# 機制層級**，針對這條路自己的保證各釘一條。
_VERIFY_ENTRY = "_run_setup_verification"
_PRODUCTION_ENTRY = "main"
# ⚠️ 這是 `verify_browser.WEBRUNNER_SCRIPT` 的**第二份拷貝**，而在 2026-09-11 之前
# 沒有任何東西比對過它們。危險方向見 `test_the_verify_entry_points_at_the_variant_
# these_tests_check`：改接另一個變體之後，下面每一支都會繼續檢查 novelai（照樣有
# 抑制旗標）→ 全綠，而實際跑起來的變體沒有旗標。
_VERIFY_MODULE = "webrunner_novelai.py"


def _function_in(module: str, name: str):
    tree = _tree_of(module)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"`{module}` 裡找不到 `{name}`——它被改名或刪掉了，而這一組測試就會變成"
        f"「掃一個不存在的東西、什麼都沒違反」的假綠。改名的話要一起改這裡。")


def _webrunner_script_filename(source: str) -> str:
    """從 `verify_browser.py` 的原始碼抽 `WEBRUNNER_SCRIPT` 指到的檔名。

    用 AST 而不是 import：`verify_browser` 匯入時會做一堆環境探測，而這裡只需要一
    個字串。走 `/` 接合的最右邊那個字面值，所以 `Path(__file__).resolve().parent /
    "webrunner_novelai.py"` 這種寫法讀得出來。
    """
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "WEBRUNNER_SCRIPT"
                        for t in node.targets)):
            continue
        value = node.value
        while isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
            if isinstance(value.right, ast.Constant):
                return value.right.value
            value = value.left
    raise AssertionError(
        "`verify_browser.py` 裡找不到 `WEBRUNNER_SCRIPT = <...> / \"<檔名>\"` 這個"
        "形狀——常數改名或改寫法了，而下面那支對帳就會變成假綠。")


def test_the_verify_entry_points_at_the_variant_these_tests_check():
    """`verify_browser.WEBRUNNER_SCRIPT` 指到的變體，必須就是本檔在檢查的那個，
    而且它必須定義 `_SUPPRESS_ORPHAN_SWEEP`。

    **這是一個單向對帳留下的洞，不是假想的。** `_VERIFY_MODULE` 是寫死的字串，
    `verify_browser.py:99` 的 `WEBRUNNER_SCRIPT` 是另一份，兩者從來沒有被比對過。
    把驗證入口改接 je_only 之後會發生的事（實測，2026-09-11）：

    * 本檔每一支都繼續解析 `webrunner_novelai.py`，那支照樣設 `_SUPPRESS_ORPHAN_
      SWEEP = True` → **全綠**；
    * 實際被叫起來的 `webrunner_je_only.py` **模組層根本沒有那個名字**（實測：
      novelai 有、je_only 沒有），所以共用建構函式那一次 `_kill_orphan_chrome()`
      會照常執行 —— 隔離驗證變成殺光全機 chrome／chromedriver，把同時在跑的正式
      批次瀏覽器一起帶走，而批次那一側只會看到一次莫名其妙的當掉。

    `test_chrome_recovery.test_the_declared_variant_exemption_is_still_real` 只擋
    **安全方向**（旗標被加進 je → 紅）；危險方向就是這一支。
    """
    verify_src = (REPO_ROOT / "axiomatic" / "verify_browser.py").read_text(encoding="utf-8")
    pointed = _webrunner_script_filename(verify_src)

    assert pointed == _VERIFY_MODULE, (
        f"`verify_browser.WEBRUNNER_SCRIPT` 指到 `{pointed}`，但本檔的 "
        f"`_VERIFY_MODULE` 還寫著 `{_VERIFY_MODULE}`——底下每一支隔離保證的檢查"
        "都在驗一個**沒有被跑起來**的變體，而且會全部通過。兩個都要改。")

    tree = _tree_of(pointed)
    module_level = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            module_level |= {t.id for t in node.targets
                             if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_level.add(node.target.id)
    # 正面對照：抽不到模組層常數的話，下面那句斷言就變成「空集合裡沒有」的假綠。
    assert len(module_level) >= 20, (
        f"`{pointed}` 只抽到 {len(module_level)} 個模組層指派——抽取器壞了，"
        "下面的斷言等於沒在跑。")
    assert "_SUPPRESS_ORPHAN_SWEEP" in module_level, (
        f"驗證入口指到的 `{pointed}` **沒有**模組層的 `_SUPPRESS_ORPHAN_SWEEP`。"
        "隔離驗證會恢復成殺光全機 chrome／chromedriver——正式批次的瀏覽器會被"
        "一起帶走。要改接這個變體的話，得先把抑制旗標與它的判斷點一起移植過去。")


def test_the_launcher_exemption_reason_owns_up_to_the_verify_path():
    """驗證入口指到的那個變體，它的核彈式掃描豁免理由**必須自己承認**這件事。

    `_LAUNCHER_EXEMPT` 的每一筆都是在說「這個檔可以無條件殺光全機 Chrome，因為它
    是正式批次」。而驗證入口指到的那個變體同時還有第二條路，保證剛好相反（不掃、
    profile 在隔離目錄）。兩條路共用一個檔，於是一句只描述正式批次的理由，讀起來
    像是這個檔只有一種行為。

    ⚠️ **這一支的第一版survived了它自己的變異。** 原本寫的是「指到的變體要在豁免
    清單裡，而且理由含『正式批次』」——把入口改接 je_only 之後，je_only 本來就在
    清單裡、理由也含那四個字，於是全綠。一支跑不贏自己動機的測試比沒有更糟：它
    製造出有人在看的假象。改成問「理由有沒有提到驗證入口」之後才咬得住。
    """
    verify_src = (REPO_ROOT / "axiomatic"
                  / "verify_browser.py").read_text(encoding="utf-8")
    pointed = _webrunner_script_filename(verify_src)
    rel = f"axiomatic/{pointed}"
    assert rel in _LAUNCHER_EXEMPT, (
        f"驗證入口指到 `{rel}`，而它不在 `_LAUNCHER_EXEMPT` 裡——那表示它現在"
        "受核彈式掃描的檢查管，兩邊的假設對不上了，請重新確認哪一邊才對。")
    reason = _LAUNCHER_EXEMPT[rel]
    assert _VERIFY_ENTRY in reason or "verify_browser" in reason, (
        f"`{rel}` 現在是 `verify_browser.py` 的驗證入口，但它在 `_LAUNCHER_EXEMPT` "
        f"裡的理由完全沒提到這件事：{reason[:70]}…\n"
        "那句理由只描述「正式批次、刻意 nuclear sweep」，而驗證那條路的保證剛好"
        "相反。讀的人會以為這個檔只有一種行為。把第二條路寫進理由裡，並確認 "
        f"`{_VERIFY_ENTRY}` 那一組隔離檢查真的涵蓋它。")


def test_the_two_paths_this_file_contrasts_both_still_exist():
    """先釘住受檢對象本身。

    §8.8(A4)／「空集合看起來就像乾淨的結果」：下面每一支都是「某某呼叫**不准**出現」
    的形狀，而抽取器只要指到一個不存在的函式、或回一個空集合，全部都會**通過**。
    所以受檢的兩個函式先各自證明存在，抽取器的牙齒則由下一支證明。
    """
    for name in (_VERIFY_ENTRY, _PRODUCTION_ENTRY):
        node = _function_in(_VERIFY_MODULE, name)
        assert node.body, f"`{name}` 是空的？"


def test_the_extractor_can_still_see_what_it_is_looking_for():
    """**正面對照**：同一個抽取器對正式路徑 `main()` 跑，必須找得到那兩個呼叫。

    這一支是下面三支的牙齒。少了它，`_calls()` 哪天壞掉（或被改成回空 list），
    「隔離路徑沒有呼叫 X」就永遠成立，測試全綠而保護是零——本專案已經在
    `_DRIVER_SERVICES` 上踩過同一種「多餘的訊號＝沒被測到的訊號」。
    對照組是免費的：正式路徑**本來就該**呼叫這兩個，兩條路的差別正是這一組要釘的。
    """
    production = _function_in(_VERIFY_MODULE, _PRODUCTION_ENTRY)
    assert _calls(production, "_sync_chrome_profile_back"), (
        "`main()` 竟然不再把 profile 同步回去了——抽取器壞了，或正式路徑改了。"
        "兩種都要人看過，因為下面幾支的可信度全靠這一支。")
    assert _calls(production, "_kill_orphan_chrome"), (
        "`main()` 竟然不再做全機 Chrome 掃描了——同上，先確認是抽取器還是程式改了。")


def test_the_isolated_verify_path_never_syncs_the_profile_back():
    """**最重要的一條**：隔離驗證不得把它那份 profile 寫回 `.chrome_profile/`。

    `--full` 用的是正式登入態的**快照副本**。要是這條路跟著 `main()` 呼叫
    `_sync_chrome_profile_back()`，一次驗證就會把驗證期間的瀏覽器狀態蓋回正式登入
    profile——而那正是整個專案最貴、最難重建的一份資料（重建等於重新人工登入）。
    這條保證只寫在 docstring 裡，2026-09-10 之前沒有任何東西在檢查。
    """
    verify = _function_in(_VERIFY_MODULE, _VERIFY_ENTRY)
    assert not _calls(verify, "_sync_chrome_profile_back"), (
        f"`{_VERIFY_ENTRY}` 呼叫了 `_sync_chrome_profile_back()`。"
        "隔離驗證會把快照副本寫回正式登入 profile——`--full` 從此每跑一次就污染一次"
        "正式登入態，而且不會有任何錯誤訊息。")


def test_the_isolated_verify_path_suppresses_the_nuclear_sweep():
    """隔離驗證不得對全機 Chrome 硬砍——它可能跟正式批次同時在跑。

    正式路徑刻意 nuclear sweep（那一刻自家 driver 還不存在，見 `_LAUNCHER_EXEMPT`
    寫的理由）。這條路不行：它是被 `verify_browser.py` 當子行程叫起來的，殺光全機
    chrome 會連正式批次的瀏覽器一起帶走。機制是把模組層級的
    `_SUPPRESS_ORPHAN_SWEEP` 設成 True，讓共用的建構函式那一次掃描變成 no-op。
    """
    verify = _function_in(_VERIFY_MODULE, _VERIFY_ENTRY)
    suppressed = [
        node for node in ast.walk(verify)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_SUPPRESS_ORPHAN_SWEEP"
                for t in node.targets)
        and isinstance(node.value, ast.Constant) and node.value.value is True
    ]
    assert suppressed, (
        f"`{_VERIFY_ENTRY}` 不再設 `_SUPPRESS_ORPHAN_SWEEP = True` 了。"
        "隔離驗證會恢復成殺光全機 chrome／chromedriver——正式批次的瀏覽器會被"
        "一起帶走，而批次那一側只會看到一次莫名其妙的當掉。")
    assert not _calls(verify, "_kill_orphan_chrome"), (
        f"`{_VERIFY_ENTRY}` 自己直接呼叫了 `_kill_orphan_chrome()`——"
        "那支不受 `_SUPPRESS_ORPHAN_SWEEP` 保護，等於繞過上面那道抑制。")


def test_the_isolated_verify_path_repoints_the_profile_snapshot():
    """profile 的目的地必須被改指到隔離目錄，而且不能指回正式的那一個。"""
    verify = _function_in(_VERIFY_MODULE, _VERIFY_ENTRY)
    targets = [
        node for node in ast.walk(verify)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CHROME_PROFILE_SNAPSHOT"
                for t in node.targets)
    ]
    assert targets, (
        f"`{_VERIFY_ENTRY}` 不再改指 `CHROME_PROFILE_SNAPSHOT` 了——"
        "共用的建構函式會把正式登入 profile 複製到**正式的**快照目錄，"
        "隔離就沒了（而且會跟正在跑的批次搶同一個目錄）。")
    for node in targets:
        value = ast.unparse(node.value)
        assert "_snap" not in value, (
            f"`CHROME_PROFILE_SNAPSHOT` 被指到 {value!r}，看起來是正式的快照目錄。"
            "隔離驗證要有自己的目的地。")


def test_the_isolated_verify_path_touches_no_queue_and_writes_no_pid():
    """佇列與 `webrunner.pid` 都不准碰——`--full` 是可以在正式批次旁邊跑的。

    碰佇列會讓一次驗證吃掉一個真的角色；寫 pid 會讓 bot 的監督者把這個短命的子行程
    誤認成正式批次，於是對它做行程管理（而它跑完就沒了）。
    """
    verify = _function_in(_VERIFY_MODULE, _VERIFY_ENTRY)
    # ⚠️ 這份清單是 **fail-open** 的：名字打錯或被改名之後，那一筆就變成一個永遠
    # 對不上的字串，守門照跑、測試照綠、保護消失——與 `CLAUDE.md` 記著的
    # `_OWNER_ONLY_SLASH` 是同一個形狀。2026-09-11 實測抓到一筆：`"write_pid_file"`
    # **全 repo 不存在**（大概是想像中的名字），也就是「不准寫 pid」那一條從來沒有
    # 真的被檢查過。現在換成真的會出現的兩個名字。
    forbidden = ("read_todo_characters", "write_todo_characters",
                 "read_todo_entries", "pop_todo_pair",
                 "claim_liveness_signal", "run_with_liveness_signal")
    hits = sorted({_dotted_name(n.func) for n in _calls(verify, *forbidden)})
    assert not hits, (
        f"`{_VERIFY_ENTRY}` 碰到了佇列／pid：{hits}。"
        "隔離驗證會吃掉真的待辦，或讓監督者把它誤認成正式批次。")


def _explicitly_assigned_env_keys(func) -> set[str]:
    """`func` 裡**明確指派**給子行程環境的鍵。

    只認兩種形狀：`env["X"] = ...` 的**指派目標**，以及 `{**os.environ, "X": ...}`
    這種**字典字面的鍵**。

    ⚠️ **刻意不認「這個字串在函式裡出現過」。** 註解會被 `ast.parse` 丟掉，但
    docstring 不會，而解釋這條規則的文字本身一定會提到 `PYTHONIOENCODING`——
    用出現與否當判準，等於讓說明文字自己滿足自己。本專案 2026-09-12 才剛在
    `test_text_encoding` 的同一條規則上踩過這個坑（`_supervisor.stream_child`
    的 docstring 騙過了子字串版的守門，把程式碼刪掉照樣全綠）。

    也刻意不認 `env.setdefault("X", ...)`：那是「沒人設過才設」，呼叫端環境裡
    帶了一個**錯的**值時它會讓步，而解碼端是寫死的。這裡要的是強制。
    """
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)):
                    keys.add(target.slice.value)
        elif isinstance(node, ast.Dict):
            keys.update(k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str))
    return keys


# 合成語料用的鍵。**不能借用 `PYTHONIOENCODING`**——這個模組裡到處都提到它，
# 借用的話「有沒有被指派」跟「有沒有被提到」在這份語料裡會分不開，而那正是這一
# 支要分開的兩件事。
_ENV_KEY_PROBE = "PROBE_ENCODING_KEY"


def test_the_env_key_scan_tells_an_assignment_from_a_mention():
    """合成對照組：判準是**指派**，不是「這個字串在函式裡出現過」。

    **放行的那兩筆才是重點。** 只放「必抓」語料的話，把 helper 改成「回傳所有
    字串常數」會照樣全綠——因為 helper 自己的 docstring 就提到那個鍵名。同理，
    `setdefault` 那一筆殺的是「把強制寫成讓步」這個變異：呼叫端環境裡帶著一個
    **錯的** `PYTHONIOENCODING` 時，`setdefault` 會留下錯的值，而解碼端是寫死的。

    已知的窄處（刻意不釘成斷言）：鍵是變數時（`env[key] = ...`）認不出來，因為
    那要做資料流分析。真的出現那種寫法時，這支不會紅，`run_full` 那支會。
    """
    def assigns():
        env = dict(os.environ)
        env["PROBE_ENCODING_KEY"] = "utf-8"
        return env

    def dict_literal():
        return {**os.environ, "PROBE_ENCODING_KEY": "utf-8"}

    def only_mentions():
        """這個 docstring 提到 PROBE_ENCODING_KEY，但底下一個指派也沒有。"""
        # 再放一個**裸的字串常數**。`ast.parse` 會丟掉註解、但不會丟掉字串，
        # 所以「出現過就算」的抽取器在這裡才真的會上當——只靠 docstring 的話，
        # 那個字串是整句中文、不等於鍵名，子字串版以外的變異殺不掉。
        note = "PROBE_ENCODING_KEY"
        return dict(os.environ), note

    def uses_setdefault():
        env = dict(os.environ)
        env.setdefault("PROBE_ENCODING_KEY", "utf-8")
        return env

    assert _ENV_KEY_PROBE in _explicitly_assigned_env_keys(assigns), (
        "`env[\"X\"] = ...` 是最基本的那一種，認不出來的話整支守門是裝飾品")
    assert _ENV_KEY_PROBE in _explicitly_assigned_env_keys(dict_literal), (
        "`{**os.environ, \"X\": ...}` 也是合法寫法，漏掉它會逼人改寫正確的程式碼")
    assert _ENV_KEY_PROBE not in _explicitly_assigned_env_keys(only_mentions), (
        "只在 docstring 裡提到就算數的話，說明文字會自己滿足自己")
    assert _ENV_KEY_PROBE not in _explicitly_assigned_env_keys(uses_setdefault), (
        "`setdefault` 是讓步不是強制：環境裡帶著錯的值時它不會覆蓋")


def test_the_full_verification_child_is_told_to_speak_utf8():
    """full 開的是 Python 子行程，所以**編碼端**也要設。

    `Popen(..., encoding="utf-8")` 只是解碼端。子行程的 stdout 接的是管線不是
    主控台，CPython 於是用系統地區編碼（本機 cp950）編碼自己的輸出，兩端不一致
    的結果是一串 U+FFFD——`errors="replace"` 保證不會有例外，所以不會紅、不會
    當掉，只是進度行被吃光。

    實測（乾淨環境，2026-09-12）：三行繁中進度行 → **26 個 U+FFFD**；補上
    `PYTHONIOENCODING` 之後 0 個。結論行 `VERIFY-BROWSER: OK` 是純 ASCII，
    照樣讀得到，所以這支工具**看起來**完全正常。

    ⚠️ 這一站 `test_text_encoding` 的靜態掃描**掃不到**：argv 是變數
    （`cmd = [sys.executable, "-u", ...]`），沒有任何掃描器看得出它開的是
    Python。這支就是它的替代覆蓋，跟 `_supervisor.stream_child` 由
    `test_supervisor.test_the_child_gets_utf8_io_encoding` 具名守著同理。

    ⚠️ 看的是 **`_full_attempt`**，不是 `run_full`。2026-09-12 為了加「瀏覽器
    中途不見了就重試一次」把 spawn 那一段拆進 `_full_attempt`，這一支當場變紅
    （`assert 'PYTHONIOENCODING' in set()`）——那是**對的**行為：它問的是「那個
    env 還在不在」，而搬家之後它在看一個沒有 env 的函式。要是當初寫成「原始碼
    裡有沒有出現這個字串」，搬家會靜悄悄地繼續綠。斷言是「這些鍵必須在」，所以
    指到空函式一定紅，方向是 fail-closed。
    """
    keys = _explicitly_assigned_env_keys(vb._full_attempt)
    assert "PYTHONIOENCODING" in keys, (
        "`_full_attempt` 要在傳給 `Popen` 的 env 裡明確指派 "
        '`env["PYTHONIOENCODING"] = "utf-8"`。'
        f"目前明確指派的鍵只有：{sorted(keys)}。"
        "父行程的 `encoding=` 管不到子行程怎麼編碼。")
    # 順便釘住那三個隔離用的鍵還在——它們掉了的話 full 驗證會去碰正式 profile。
    for required in ("NAI_VERIFY_MODE", "NAI_VERIFY_PROFILE_DEST",
                     "NAI_VERIFY_OUTPUT_DIR"):
        assert required in keys, f"隔離用的 {required} 不見了"


# ---------------------------------------------------------------------------
# full 的「瀏覽器中途不見了」重試
# ---------------------------------------------------------------------------
# smoke 從第一天就會重試，full 一次定生死——而 full 才是最容易撞上外部 sweep／
# 瀏覽器暴斃的那一個（跑分鐘級、有頭的真瀏覽器、幾十個 driver 指令）。
# 2026-09-12 實測到代價：同一道指令相隔幾分鐘跑兩次，一次 FAIL、一次全綠。
#
# 這一族測試釘的是**三件會互相拉扯的事**：
#   1. 該重試的要重試（否則環境性失敗會被讀成「我的改動弄壞了瀏覽器」）；
#   2. **不**該重試的絕對不能重試（否則一個穩定的缺陷會看起來像「有時候會過」）；
#   3. 重試成功時要講出來，而且 `VERIFY-BROWSER:` 結果行仍然只能有一行
#      （那是別人拿來判定結果的契約）。
# 第 2 與第 3 是**收緊**步驟，只有近似案例殺得死；只測第 1 的話，把判準放寬成
# 「什麼都重試」照樣全綠。

_BROWSER_GONE_CASES = {
    # 必重試——第一筆是 2026-09-12 真的抓到的那一段，逐字貼進來。
    "實測那一次（select_model）": (
        "BrowserGoneError: browser session gone during select_model — "
        "InvalidSessionIdException: Message: invalid session id: session "
        "deleted as the browser has closed the connection from disconnected: "
        "not connected to DevTools (Session info: chrome=152.0.7977.84)", True),
    "chrome 連不上": (
        "WebDriverException: Message: chrome not reachable", True),
    "renderer 被砍掉": (
        "disconnected: unable to connect to renderer", True),
    "chromedriver 行程沒了": (
        "MaxRetryError: HTTPConnectionPool(host='localhost', port=53211): "
        "[WinError 10061] 無法建立連線", True),
    # 必**不**重試。這幾筆才是重點。
    "登入失敗": ("login failed: credentials rejected", False),
    "DOM 找不到（訊息裡也提到瀏覽器）": (
        "setup 完成但 Generate 按鈕找不到；瀏覽器仍在、頁面已載入", False),
    "子行程逾時": ("full 子行程逾時（>360s）已中止", False),
    "子行程 rc 非零": ("子行程 rc=2", False),
    "憑證讀不到": ("讀取憑證失敗：KeyError('email')", False),
    "空字串": ("", False),
    # ⚠️ **這一筆殺的是「把泛用連線字串收進判準」那個很自然的念頭。**
    # `max retries exceeded` / `connection refused` 是 urllib3 的泛用訊息，登入
    # 那一步連站台連不上時也長這樣。收進來的話判準就從「瀏覽器不見了」漂成
    # 「任何連線問題」，於是一個站台端的穩定故障會開始「有時候會過」。
    "登入時站台連不上（泛用連線字串）": (
        "login failed: Max retries exceeded with url: /login "
        "(Connection refused)", False),
}


@pytest.mark.parametrize("label", sorted(_BROWSER_GONE_CASES))
def test_the_browser_gone_predicate_tells_the_shapes_apart(label):
    """哪些失敗原因算「瀏覽器不見了」。

    ⚠️ 「DOM 找不到」那一筆刻意在訊息裡寫了「瀏覽器」兩個字：判準認的必須是
    **徵狀字串**，不是「訊息有沒有提到瀏覽器」。把判準寫成後者的話，一個穩定的
    DOM 缺陷會開始「有時候會過」，而那比直接紅還難查。
    """
    reason, should_retry = _BROWSER_GONE_CASES[label]
    assert vb._is_browser_gone(reason) is should_retry, (
        f"「{label}」的判定不對：{reason[:80]!r}")


def _stub_full_attempts(monkeypatch, outcomes):
    """讓 `_full_attempt` 依序回傳 `outcomes`，並記錄被呼叫幾次。"""
    calls: list[int] = []

    def _fake(generate, attempt):
        calls.append(attempt)
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]

    monkeypatch.setattr(vb, "_full_attempt", _fake)
    monkeypatch.setattr(vb.time, "sleep", lambda _s: None)
    return calls


def test_full_retries_once_when_the_browser_vanished(monkeypatch, capsys):
    """第一趟瀏覽器不見了、第二趟成功 → 整體 OK，而且真的跑了兩趟。"""
    gone = _BROWSER_GONE_CASES["實測那一次（select_model）"][0]
    calls = _stub_full_attempts(monkeypatch, [(False, gone), (True, "")])
    rc = vb.run_full(generate=False)
    out = capsys.readouterr().out
    assert rc == vb.EXIT_OK, out
    assert calls == [1, 2], f"應該跑兩趟，實際 {calls}"
    assert f"{vb.RESULT_PREFIX} OK" in out


def test_a_retry_that_succeeds_still_says_it_retried(monkeypatch, capsys):
    """**安靜地變綠等於把環境的不穩定藏起來。**

    第一趟真的死了一次瀏覽器，那是這台機器的狀態資訊。只回 rc=0 的話，讀的人會以為
    這一趟乾乾淨淨——而下次它變成每次都死的時候，沒有人記得它早就開始偶爾死了。
    """
    gone = _BROWSER_GONE_CASES["chrome 連不上"][0]
    _stub_full_attempts(monkeypatch, [(False, gone), (True, "")])
    vb.run_full(generate=False)
    out = capsys.readouterr().out
    assert "第 2 趟" in out and "第 1 趟" in out, (
        f"重試成功卻沒有說出來：\n{out}")


def test_full_emits_exactly_one_verdict_line_even_after_a_retry(
        monkeypatch, capsys):
    """結果行是契約：不管跑幾趟，`VERIFY-BROWSER:` 只能出現一次。

    這就是 `_full_attempt` 刻意不自己 `_emit_ok` / `_emit_fail` 的理由——讀這支
    輸出的人（含 CLAUDE.md 與驗證守則叫來的人）是用那一行判定結果的，出現兩行
    就等於兩個互相矛盾的答案。
    """
    gone = _BROWSER_GONE_CASES["renderer 被砍掉"][0]
    _stub_full_attempts(monkeypatch, [(False, gone), (True, "")])
    vb.run_full(generate=False)
    out = capsys.readouterr().out
    assert out.count(vb.RESULT_PREFIX) == 1, (
        f"結果行出現 {out.count(vb.RESULT_PREFIX)} 次：\n{out}")


@pytest.mark.parametrize(
    "label", ["登入失敗", "DOM 找不到（訊息裡也提到瀏覽器）", "子行程逾時"])
def test_full_does_not_retry_a_real_failure(label, monkeypatch, capsys):
    """真的失敗一次定生死——重跑只會讓穩定的缺陷看起來像偶發。

    逾時那一筆特別重要：子行程靜默卡死是真的訊號，重跑一次只是再等一個
    `FULL_DEADLINE_SEC`，而那是分鐘級的浪費。
    """
    reason = _BROWSER_GONE_CASES[label][0]
    calls = _stub_full_attempts(monkeypatch, [(False, reason)])
    rc = vb.run_full(generate=False)
    out = capsys.readouterr().out
    assert rc == vb.EXIT_FAIL, out
    assert calls == [1], f"「{label}」不該重試，實際跑了 {calls}"
    assert out.count(vb.RESULT_PREFIX) == 1


def test_full_gives_up_after_the_attempt_cap(monkeypatch, capsys):
    """每一趟都是瀏覽器不見了 → 用完上限就 FAIL，不會無限重試。

    這一支釘的是**上限真的有作用**：少了它，把 `FULL_MAX_ATTEMPTS` 改大（或改成
    `while True`）不會有任何測試變紅，而 full 一趟是分鐘級的。
    """
    gone = _BROWSER_GONE_CASES["chromedriver 行程沒了"][0]
    calls = _stub_full_attempts(monkeypatch, [(False, gone)])
    rc = vb.run_full(generate=False)
    out = capsys.readouterr().out
    assert rc == vb.EXIT_FAIL, out
    assert calls == list(range(1, vb.FULL_MAX_ATTEMPTS + 1)), (
        f"應該剛好跑 {vb.FULL_MAX_ATTEMPTS} 趟，實際 {calls}")


def test_the_full_attempt_cap_is_at_least_two_but_small():
    """上限的**數字本身**也要有人看著。

    1 ＝ 整個重試機制是裝飾品（而且不會有任何測試紅——上面那幾支都會照樣過，
    因為它們釘的是「有沒有重試」的行為而不是次數）；太大則是分鐘級的浪費，
    而且會把「這台機器在殺瀏覽器」這個訊號磨掉。
    """
    assert 2 <= vb.FULL_MAX_ATTEMPTS <= 3, (
        f"FULL_MAX_ATTEMPTS = {vb.FULL_MAX_ATTEMPTS}；full 一趟是分鐘級的，"
        "2 是刻意選的（smoke 的 3 是秒級才負擔得起）。")
    assert vb.FULL_TOTAL_DEADLINE_SEC >= vb.FULL_DEADLINE_SEC * 2, (
        "整體上限比兩趟的 watchdog 還短，等於第二趟永遠來不及跑。")


# ---------------------------------------------------------------------------
# 主控台硬化：印不出來的字元不該讓這支工具死掉
# ---------------------------------------------------------------------------
# 這一層與 `test_text_encoding.test_nothing_printed_is_unencodable_on_this_console`
# **不是同一件事**，兩支都要留：
#   * 那一支守「讀得出來」——`※` 比一串逃脫序列好讀，而且它在那一行**沒有被執行
#     到**的情況下也看得見（§8.53 找到的三個字元，兩個就躺在「這個失效沒有別的
#     症狀」的路徑上，平常永遠不會跑到）。
#   * 這一層守「就算有人繞過了那支守門，結論那一行還是印得完」。這支腳本的契約是
#     **一定**要印一行 `VERIFY-BROWSER:`；死在進度訊息上＝呼叫端讀到「沒有結論 ＋
#     非零結束碼」＝**一個假的 FAIL**，而它正是專案規定用來判斷瀏覽器有沒有被改壞
#     的那一支。回報錯的答案比沒有答案更貴。

_VB_PATH = REPO_ROOT / "axiomatic" / "verify_browser.py"

# 這一族要的是「一個這個編解碼器編不出來的字元」，不是「這台機器的代碼頁」。
_FAILING_CODEC = "cp950"
_UNENCODABLE_ORD = 0x26A0  # U+26A0 警告三角形——正是 §8.53 找到的那個字元


class _RecordingStream:
    """記下 `reconfigure` 收到什麼；其餘給最小的串流介面。"""

    def __init__(self):
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)

    def write(self, _text):  # pragma: no cover - 只是讓它像個串流
        return 0

    def flush(self):  # pragma: no cover
        return None


def test_hardening_asks_both_streams_for_a_non_fatal_error_handler(monkeypatch):
    """`stdout` 與 `stderr` 都要被要求換成 `backslashreplace`。

    為什麼不是 `ignore`：`ignore` 會讓那個字元**無聲消失**，而這支腳本印的每一行
    都是診斷。把診斷吃掉跟死掉一樣糟，只是更難查。
    """
    out, err = _RecordingStream(), _RecordingStream()
    monkeypatch.setattr(vb.sys, "stdout", out)
    monkeypatch.setattr(vb.sys, "stderr", err)
    vb._harden_console()
    assert out.calls == [{"errors": "backslashreplace"}], out.calls
    assert err.calls == [{"errors": "backslashreplace"}], err.calls


@pytest.mark.parametrize("broken", [
    pytest.param(object(), id="沒有 reconfigure 這個方法"),
    pytest.param(None, id="pythonw 底下 sys.stdout 是 None"),
])
def test_hardening_tolerates_a_stream_it_cannot_reconfigure(monkeypatch, broken):
    """拿不到就算了——這一層是加分項，不該讓整支工具起不來。

    兩種都真的會發生：pytest 的擷取物件不是 `TextIOWrapper`，`pythonw` 底下
    `sys.stdout` 直接是 `None`。這兩種都不是錯誤情況。
    """
    monkeypatch.setattr(vb.sys, "stdout", broken)
    monkeypatch.setattr(vb.sys, "stderr", broken)
    vb._harden_console()  # 不該拋


def test_hardening_tolerates_a_stream_that_refuses(monkeypatch):
    """已經被 detach／關掉的串流會丟 `ValueError`，一樣要吞掉。"""

    class _Refusing:
        def reconfigure(self, **_kwargs):
            raise ValueError("underlying buffer has been detached")

    monkeypatch.setattr(vb.sys, "stdout", _Refusing())
    monkeypatch.setattr(vb.sys, "stderr", _Refusing())
    vb._harden_console()  # 不該拋


def test_hardening_runs_only_from_main_never_at_import():
    """**只能**掛在 `if __name__ == "__main__":` 底下。

    import 時去動 `sys.stdout` 等於在別人的行程裡留副作用——而整套測試就是 import
    這個模組的那個行程。用 AST 判，不要用字串比對：解釋這條規則的註解與 docstring
    本身一定會提到 `_harden_console`，`in source` 那種寫法會被自己的說明餵飽
    （本專案為這件事付過代價，見 `_sets_the_child_encoding` 的 docstring）。
    """
    tree = ast.parse(_VB_PATH.read_text(encoding="utf-8"), str(_VB_PATH))

    def _calls_in(nodes) -> int:
        total = 0
        for node in nodes:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_harden_console"):
                    total += 1
        return total

    main_blocks = [n for n in tree.body
                   if isinstance(n, ast.If)
                   and "__main__" in ast.unparse(n.test)]
    assert len(main_blocks) == 1, "找不到唯一的 `__main__` 區塊"
    assert _calls_in(main_blocks) == 1, (
        "`__main__` 區塊沒有（或不只一次）呼叫 `_harden_console()`")

    others = [n for n in tree.body if n not in main_blocks]
    assert _calls_in(others) == 0, (
        "`_harden_console()` 在模組層級被呼叫了——import 這個模組的行程"
        "（包含整套測試）會被改掉 `sys.stdout`，那是別人的行程。")


def _child(code: str):
    """跑一個子行程，stdout 是**管線**、而且明確叫它用會失敗的那個編碼。

    兩件事都是刻意的：

    * **stdout 是管線。** 真主控台那條路走 `WriteConsoleW`，代碼頁根本不參與，
      所以手動跑永遠是對的——那正是 §8.53 那個缺陷躲了這麼久的第一層遮蔽。
    * **明寫 `PYTHONIOENCODING=cp950`，而不是把環境清空。** 清空的版本要靠「這台
      機器的地區編碼剛好是 cp950」才測得到東西，換一台機器就退化成永遠 skip 的
      測試。明寫之後這一族在任何平台上都測得到同一件事（`cp950` 是 CPython 內建的
      編解碼器，跟作業系統的代碼頁無關）。順帶也把開發者的殼 export 的
      `PYTHONIOENCODING=utf-8` 蓋掉——那是第二層遮蔽。

    ※ 這裡的 `PYTHONIOENCODING` **不是**在遵守「開 Python 子行程要叫它講 UTF-8」
    那條規則，而是它的反面：這一族要的就是子行程用地區編碼，然後炸掉。
    """
    import subprocess  # noqa: PLC0415  只有這一族用得到
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONIOENCODING"] = _FAILING_CODEC
    return subprocess.run(
        [sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, check=False)


def _render(setup: str = "") -> str:
    """組出子行程要跑的那幾行：正常的一行、編不出來的一行、再正常的一行。

    最後那行 `AFTER` 是重點——它回答的是「行程有沒有活著走完」，而不只是
    「那個字元印成什麼」。
    """
    lines = ["import sys",
             "sys.path.insert(0, r'" + str(REPO_ROOT / "axiomatic") + "')"]
    if setup:
        lines.append(setup.rstrip("\n"))
    lines += ["print('BEFORE')",
              "print(chr(" + str(_UNENCODABLE_ORD) + "))",
              "print('AFTER')"]
    return "\n".join(lines) + "\n"


def test_an_unencodable_line_kills_an_unhardened_tool():
    """**正控制組**：沒有硬化的話，那一行真的會讓行程死掉。

    沒有這一支，下一支就可能是在一個 stdout 本來就編得出來的情境裡永遠綠——
    「我什麼都沒量到」與「量到了而且是好的」長得一模一樣。
    """
    proc = _child(_render())
    assert proc.returncode != 0, (
        "沒硬化卻沒死（rc=0）——那下一支測不到它宣稱的東西。"
        f"stdout={proc.stdout!r}")
    assert "UnicodeEncodeError" in proc.stderr, proc.stderr[-400:]
    assert "AFTER" not in proc.stdout, (
        "行程沒死在那一行——這支控制組沒有測到它宣稱的東西")


def test_a_hardened_tool_survives_an_unencodable_line():
    """硬化之後：那一行變成逃脫序列，**後面的輸出照樣印完**。

    重點不是「壞字元印得漂亮」，是結論那一行還在。這支腳本的契約就是那一行：
    呼叫端 grep `VERIFY-BROWSER:` 拿結論，沒有結論 ＋ 非零結束碼 ＝ 假的 FAIL。
    """
    proc = _child(_render(
        "import verify_browser as _vb\n_vb._harden_console()"))
    assert proc.returncode == 0, (
        f"硬化之後仍然死掉：rc={proc.returncode}\n{proc.stderr[-400:]}")
    assert "BEFORE" in proc.stdout and "AFTER" in proc.stdout, (
        f"硬化之後輸出不完整：{proc.stdout!r}")
    assert f"{_UNENCODABLE_ORD:04x}" in proc.stdout.lower(), (
        "那個字元應該被逃脫成一個看得見的碼位而不是消失——`ignore` 會讓它無聲"
        f"消失，而這支腳本印的每一行都是診斷：{proc.stdout!r}")


def test_one_unreconfigurable_stream_does_not_stop_the_other(monkeypatch):
    """stdout 拒絕不代表 stderr 也不用硬化——迴圈要 `continue`，不是 `return`。

    前面兩支容忍測試把**兩個**串流都換成壞的，於是「跳過這個」與「整個放棄」算出
    同一個結果，拿掉 `continue` 改成 `return` 照樣全綠。這一支刻意只弄壞一邊
    （本專案記過這條：一個輸入同時觸發兩條路，刪掉任一條都活得下來，要造一個
    **只**觸發其中一條的輸入）。

    順序也是刻意的：壞的放在**前面**。反過來的話 `return` 一樣會在壞的那個之前
    把好的處理完，這支就白寫了。
    """

    class _Refusing:
        def reconfigure(self, **_kwargs):
            raise ValueError("underlying buffer has been detached")

    good = _RecordingStream()
    monkeypatch.setattr(vb.sys, "stdout", _Refusing())   # 壞的在前
    monkeypatch.setattr(vb.sys, "stderr", good)          # 好的在後
    vb._harden_console()
    assert good.calls == [{"errors": "backslashreplace"}], (
        "第一個串流拒絕之後就不管第二個了——那是 `return` 不是 `continue`，"
        f"而兩個串流只有一個可能拒絕。實際收到：{good.calls}")


# ---------------------------------------------------------------------------
# `--variant je`：je 變體的啟動路徑（2026-09-20）
# ---------------------------------------------------------------------------
# 釘四個性質，每一個失效都是安靜的：
#   1. CLI：預設仍是 selenium（行為逐字不變）、`je` 真的走到 je 的 runner、
#      `--full --variant je` 是參數錯誤（exit 2，不是 FAIL 也不是 SKIP）；
#   2. 讓位：je 那條路也走 `_run_with_slot`——正式批次在跑時一個瀏覽器都不開；
#   3. **匯入時的工作目錄**：`je_web_runner` 匯入時就用相對路徑開 `WEBRunner.log`，在
#      repo 根目錄匯入等於以附加模式開著正式批次的 `webrunner.log`。子行程入口在匯入
#      之前就要拒絕，父行程給子行程的工作目錄一定在 repo 外；
#   4. 結論：FAIL 分得出「driver 管理器安裝失敗」與「瀏覽器起不來」，結論行仍然只有一行。
#
# 第 3 條用**行為**釘，不用 AST：AST 看得到「有沒有呼叫檢查」，看不到「檢查是不是在
# 匯入之前、有沒有真的擋住」——本檔 `test_an_undecidable_pid_file_never_opens_a_browser`
# 記過同一個教訓（`if False and …` 讓名字照樣出現、守門照樣綠）。做法是放一個假的
# `je_web_runner` 進 `sys.modules`，它在被取用的那一刻記下工作目錄；真的函式庫一次都
# 不匯入，所以測試本身不會開任何記錄檔。

def test_the_variant_flag_defaults_to_selenium(monkeypatch):
    """預設 = selenium：加這個旗標之前的呼叫方式行為逐字不變。"""
    assert vb._build_parser().parse_args([]).variant == "selenium"
    rc, seen, _out = _run_main(monkeypatch, [])
    assert rc == vb.EXIT_OK
    assert seen["label"] == "smoke" and "smoke" in seen, seen
    assert "smoke_je" not in seen, f"沒加 --variant 卻走了 je：{seen}"


def test_the_je_variant_reaches_the_je_smoke_runner(monkeypatch):
    """`--variant je` 要真的走到 je 的 runner，smoke 的兩個旗標也要跟著傳下去。"""
    rc, seen, _out = _run_main(monkeypatch, ["--variant", "je"])
    assert rc == vb.EXIT_OK
    assert seen["label"] == "smoke-je", seen
    assert seen["smoke_je"] == {"url": "about:blank", "headless": True}, seen
    assert "smoke" not in seen and "full" not in seen, seen

    _rc, seen, _out = _run_main(monkeypatch, [
        "--variant", "je", "--headed", "--url", "https://example.invalid/y"])
    assert seen["smoke_je"] == {"url": "https://example.invalid/y",
                                "headless": False}, seen


@pytest.mark.parametrize("argv", [["--full", "--variant", "je"],
                                  ["--mode", "full", "--variant", "je"]])
def test_full_with_the_je_variant_is_a_usage_error(monkeypatch, capsys, argv):
    """`--full` 只接 selenium 變體，所以 `--full --variant je` 是**參數錯誤**。

    exit 2 是 argparse 的「指令打錯」，刻意跟 FAIL（1）與 SKIP（3）都不同：印成 FAIL
    會被讀成瀏覽器壞了；印成 SKIP 會讓自走迴圈晚點再重試一個永遠不可能成功的指令。
    也什麼都不能開——`_run_with_slot` 一次都不得被呼叫。
    """
    calls = []
    monkeypatch.setattr(vb, "_run_with_slot",
                        lambda label, _fn: calls.append(label) or vb.EXIT_OK)
    with pytest.raises(SystemExit) as caught:
        vb.main(argv)
    assert caught.value.code == 2, caught.value.code
    assert calls == [], f"參數錯誤還是去取了槽：{calls}"
    captured = capsys.readouterr()
    assert vb.RESULT_PREFIX not in captured.out, captured.out
    assert "selenium" in captured.err, f"錯誤訊息沒說 full 只接哪個變體：{captured.err!r}"


@pytest.mark.parametrize("argv", [["--generate"],
                                  ["--generate", "--variant", "je"],
                                  ["--mode", "smoke", "--generate"],
                                  ["--full", "--url", "https://example.invalid/"],
                                  ["--mode", "full", "--url", "https://example.invalid/"]])
def test_a_flag_outside_its_mode_is_a_usage_error(monkeypatch, capsys, argv):
    """只在某個模式有作用的旗標，出現在別的模式是**參數錯誤**，不是安靜忽略。

    忽略的後果是最糟的那一種：照跑、印 `OK`，下指令的人以為那個旗標要求的東西也驗過
    了——smoke 加 `--generate` 以為真的產過圖、full 加 `--url` 以為那個網址載入過。
    與上一支同理：exit 2、不取槽、不印結論行。
    """
    calls = []
    monkeypatch.setattr(vb, "_run_with_slot",
                        lambda label, _fn: calls.append(label) or vb.EXIT_OK)
    with pytest.raises(SystemExit) as caught:
        vb.main(argv)
    assert caught.value.code == 2, caught.value.code
    assert calls == [], f"參數錯誤還是去取了槽：{calls}"
    captured = capsys.readouterr()
    assert vb.RESULT_PREFIX not in captured.out, captured.out
    assert "--full" in captured.err, f"錯誤訊息沒說要加 --full：{captured.err!r}"


@pytest.mark.parametrize("argv, generate", [
    (["--full", "--generate"], True),
    (["--mode", "full", "--generate"], True),
    (["--full", "--url", "about:blank"], False),    # 寫出預設值不算指定網址
    (["--full", "--headed"], False),                # full 本來就有頭，刻意放行
])
def test_the_full_mode_flags_are_still_accepted(monkeypatch, argv, generate):
    """必放行的那一半：full 照常走到 full，`--generate` 真的傳下去。"""
    seen = {}
    monkeypatch.setattr(vb, "run_full",
                        lambda gen: seen.setdefault("generate", gen) or 0)
    monkeypatch.setattr(vb, "_run_with_slot",
                        lambda label, fn: seen.setdefault("label", label) and fn())
    vb.main(argv)
    assert seen == {"label": "full", "generate": generate}, seen


def test_the_je_path_stands_aside_for_a_live_batch(monkeypatch, tmp_path):
    """je 那條路也走**真的** `_run_with_slot`：正式批次在跑就 SKIP，不開瀏覽器。

    從 `main` 一路進去，槽是假的、pid 檔指向本行程（一定活著）。反面對照（沒有 pid
    檔就真的跑）少不了：把 je 那條路改成「一律 SKIP」也會讓前半通過。
    """
    ran = []
    monkeypatch.setattr(vb, "run_smoke_je",
                        lambda url, headless: ran.append(url) or vb.EXIT_OK)
    monkeypatch.setattr(vb, "run_smoke",
                        lambda url, headless: pytest.fail("走到 selenium 的 smoke 了"))
    fake = _FakeSlot()
    monkeypatch.setitem(sys.modules, "_chrome_slot", fake)

    pid_file = _pid_file(monkeypatch, tmp_path, str(os.getpid()).encode("utf-8"))
    rc, out = _emit(vb.main, ["--variant", "je"])
    assert rc == vb.EXIT_SKIP, (rc, out)
    assert ran == [], "正式批次在跑，je 的 smoke 卻還是跑了"
    assert fake.released == [vb.SLOT_OWNER], fake.released

    pid_file.unlink()
    rc, out = _emit(vb.main, ["--variant", "je"])
    assert rc == vb.EXIT_OK, (rc, out)
    assert ran == ["about:blank"], ran


class _FakeJeDriver:
    """`set_driver` 之後 `wr.current_webdriver` 的替身：夠子行程入口走完一趟。"""
    current_url = "about:blank"
    title = ""

    def set_page_load_timeout(self, _sec):
        pass

    def set_script_timeout(self, _sec):
        pass

    def get(self, _url):
        pass


class _FakeJeWrapper:
    """`je_web_runner.webdriver_wrapper_instance` 的替身：記下每一次呼叫與當下工作目錄。"""

    def __init__(self):
        self.calls = []
        self.current_webdriver = None

    def set_driver(self, name, **kwargs):
        self.calls.append(("set_driver", name, kwargs, os.getcwd()))
        self.current_webdriver = _FakeJeDriver()

    def quit(self):
        self.calls.append(("quit",))
        self.current_webdriver = None


def _run_je_child(monkeypatch, cwd, *, slot_ok=True):
    """在 `cwd` 裡跑子行程入口；`je_web_runner` 換成取用時會記下工作目錄的假模組。

    回 `(rc, 輸出, 每次取用當下的工作目錄, 假包裝層)`。
    """
    import types

    touched = []
    wrapper = _FakeJeWrapper()
    fake = types.ModuleType("je_web_runner")

    def _getattr(name):
        touched.append(os.getcwd())
        if name == "webdriver_wrapper_instance":
            return wrapper
        raise AttributeError(name)

    fake.__getattr__ = _getattr
    monkeypatch.setitem(sys.modules, "je_web_runner", fake)
    monkeypatch.setattr(sys, "path", list(sys.path))   # 入口會插入 WebRunner 路徑
    monkeypatch.setattr(vb, "_slot_held_by_an_ancestor", lambda: slot_ok)
    monkeypatch.chdir(cwd)
    rc, out = _emit(vb._je_child_main, ["about:blank", "1"])
    return rc, out, touched, wrapper


def test_the_je_child_refuses_to_import_the_library_from_inside_the_repo(monkeypatch):
    """工作目錄在 repo 裡：**匯入之前**就拒絕，函式庫一次都不取用。

    槽的守門故意設成放行，才證明得了擋下它的是工作目錄那一道。
    """
    rc, out, touched, wrapper = _run_je_child(monkeypatch, REPO_ROOT)
    assert touched == [], f"在 repo 裡取用了 je_web_runner：{touched}"
    assert wrapper.calls == [], wrapper.calls
    assert rc == vb.EXIT_FAIL
    assert f"{vb.JE_CHILD_PREFIX} FAIL refused" in out, out


def test_the_je_child_refuses_without_the_slot_in_an_ancestors_hands(
        monkeypatch, tmp_path):
    """工作目錄沒問題、但槽不在祖先手上：同樣在匯入之前拒絕。

    沒有這一道，手打內部旗標就是一條繞過讓位、在正式批次旁邊開瀏覽器的路。
    """
    rc, out, touched, wrapper = _run_je_child(monkeypatch, tmp_path, slot_ok=False)
    assert touched == [] and wrapper.calls == [], (touched, wrapper.calls)
    assert rc == vb.EXIT_FAIL
    assert f"{vb.JE_CHILD_PREFIX} FAIL refused" in out, out


def test_the_je_child_drives_set_driver_from_its_disposable_directory(
        monkeypatch, tmp_path):
    """正面對照：工作目錄在 repo 外、槽在祖先手上 → 真的走 `set_driver`。

    少了這一支，把子行程入口改成「一律拒絕」也會讓上面兩支通過。同時釘住交給
    `set_driver` 的東西都指向丟棄式目錄：profile 與 chromedriver 記錄都不是正式的。
    """
    rc, out, touched, wrapper = _run_je_child(monkeypatch, tmp_path)
    assert rc == vb.EXIT_OK, out
    assert out.strip().splitlines()[-1] == f"{vb.JE_CHILD_PREFIX} OK", out
    here = os.path.normcase(str(tmp_path))
    assert touched and all(os.path.normcase(c) == here for c in touched), touched

    kind, name, kwargs, cwd_at_call = wrapper.calls[0]
    assert (kind, name) == ("set_driver", "chrome"), wrapper.calls
    assert os.path.normcase(cwd_at_call) == here
    options = kwargs["options"]
    assert "--headless=new" in options and "--start-maximized" not in options
    profiles = [o.split("=", 1)[1] for o in options
                if o.startswith("--user-data-dir=")]
    assert len(profiles) == 1, options
    assert os.path.normcase(os.path.dirname(profiles[0])) == here, profiles
    assert kwargs["experimental_options"] == vb._je_experimental_options()
    logs = [a.split("=", 1)[1] for a in kwargs["service"].command_line_args()
            if a.startswith("--log-path=")]
    assert len(logs) == 1, logs
    assert os.path.normcase(os.path.dirname(logs[0])) == here, (
        f"chromedriver 記錄不在丟棄式目錄裡：{logs}")
    assert ("quit",) in wrapper.calls, "沒有收掉 driver"


def test_the_slot_guard_only_accepts_an_ancestor_holding_the_verify_slot(monkeypatch):
    """持有者要是**祖先**、而且 owner 是 verify。自己不算，bot 也不算。"""
    import psutil

    class _Holder:
        def __init__(self, holder):
            self.holder = holder

        def read_holder(self):
            return self.holder

    parent = psutil.Process().parent().pid
    cases = [
        ({"owner": vb.SLOT_OWNER, "pid": parent}, True),
        ({"owner": "bot", "pid": parent}, False),
        ({"owner": vb.SLOT_OWNER, "pid": os.getpid()}, False),
        ({"owner": vb.SLOT_OWNER, "pid": None}, False),
        (None, False),
    ]
    for holder, expected in cases:
        monkeypatch.setitem(sys.modules, "_chrome_slot", _Holder(holder))
        assert vb._slot_held_by_an_ancestor() is expected, holder


def test_the_je_working_directory_is_never_inside_the_repo(monkeypatch, tmp_path):
    """丟棄式工作目錄（driver 快取、記錄檔、profile 都在裡面）一定在 repo 外。

    共用前綴那一格是近似案例：字串前綴版的判斷會把 `Axiomatic_sibling` 當成 repo
    裡面。拒絕時必須**什麼都還沒建**。
    """
    assert vb._is_inside_repo(REPO_ROOT)
    assert vb._is_inside_repo(REPO_ROOT / "axiomatic" / "x")
    assert not vb._is_inside_repo(REPO_ROOT.parent / (REPO_ROOT.name + "_sibling"))
    assert not vb._is_inside_repo(tmp_path)

    made = vb._je_workdir(str(tmp_path))
    assert made and os.path.isdir(made), made
    assert os.path.normcase(os.path.dirname(made)) == os.path.normcase(str(tmp_path))

    inside = REPO_ROOT / "axiomatic"
    before = set(os.listdir(inside))
    assert vb._je_workdir(str(inside)) is None
    assert set(os.listdir(inside)) == before, "拒絕之前已經在 repo 裡建了目錄"

    # 系統暫存目錄被 `TMP`／`TEMP` 指進 repo 時，預設那條路也要拒絕，而且不開子行程。
    monkeypatch.setattr(vb.tempfile, "gettempdir", lambda: str(inside))
    spawned = []
    monkeypatch.setattr(vb.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or pytest.fail("開了子行程"))
    ok, stage, _reason = vb._je_attempt("about:blank", True, 1)
    assert (ok, stage) == (False, "refused") and spawned == []


class _FakeChild:
    """`subprocess.Popen` 回傳值的替身：吐固定幾行、以固定結束碼結束。"""

    def __init__(self, lines, rc=0):
        self.stdout = iter(line + "\n" for line in lines)
        self.returncode = rc
        self.pid = 0

    def wait(self, timeout=None):
        return self.returncode


def test_the_je_child_is_spawned_outside_the_repo_with_a_clean_environment(
        monkeypatch, capsys):
    """父行程怎麼開子行程：工作目錄、環境、指令列、收尾。

    `WDM_LOCAL` 為真時 driver 管理器把快取放在 `sys.path[0]/.wdm`，而子行程的
    `sys.path[0]` 是 `axiomatic/`——所以不管父行程環境裡有什麼都要拿掉。
    `PYTHONIOENCODING` 故意先設成錯的值：只驗「不存在時會補上」的話，`setdefault`
    這種讓步寫法也會過。
    """
    monkeypatch.setenv("WDM_LOCAL", "true")
    monkeypatch.setenv("PYTHONIOENCODING", "cp950")
    seen = {}

    def _popen(args, **kwargs):
        seen.update(args=list(args), kwargs=kwargs,
                    cwd_existed=os.path.isdir(kwargs["cwd"]))
        return _FakeChild(["progress", f"{vb.JE_CHILD_PREFIX} OK"])

    monkeypatch.setattr(vb.subprocess, "Popen", _popen)
    ok, stage, reason = vb._je_attempt("about:blank", True, 1)
    assert (ok, stage) == (True, ""), (ok, stage, reason)

    args, kwargs = seen["args"], seen["kwargs"]
    assert args == [sys.executable, "-u", str(vb._THIS_FILE), vb.JE_CHILD_FLAG,
                    "about:blank", "1"], args
    cwd = kwargs["cwd"]
    assert seen["cwd_existed"] and not vb._is_inside_repo(cwd), cwd
    assert not os.path.exists(cwd), "丟棄式工作目錄沒有被刪掉"
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert "WDM_LOCAL" not in kwargs["env"], "WDM_LOCAL 會讓快取落在 axiomatic/.wdm"
    assert kwargs.get("encoding") == "utf-8"
    assert "  | progress" in capsys.readouterr().out


@pytest.mark.parametrize("lines, rc", [
    ([f"{vb.JE_CHILD_PREFIX} OK"], 1),     # 印了 OK 卻非零結束：不可信
    (["Traceback (most recent call last):"], 1),
    (["noise"], 0),                        # 零結束但沒有結論行
])
def test_a_child_without_a_clean_verdict_is_a_failure(monkeypatch, lines, rc):
    """子行程的結論只有「印了 OK **而且** rc=0」才算數；其餘一律是 `child` 階段的失敗。

    子行程在印完 OK 之後才死（例如清理時炸掉）時，瀏覽器確實起來過，但工具本身的
    收尾壞了——回報成功會把那一半藏起來。
    """
    monkeypatch.setattr(vb.subprocess, "Popen",
                        lambda *_a, **_k: _FakeChild(lines, rc))
    ok, stage, _reason = vb._je_attempt("about:blank", True, 1)
    assert (ok, stage) == (False, "child"), (lines, rc)


def test_main_hands_the_internal_flag_to_the_child_entry(monkeypatch):
    """隱藏旗標要在 argparse **之前**分流，否則子行程會死在「不認得的參數」(exit 2)。"""
    got = []
    monkeypatch.setattr(vb, "_je_child_main",
                        lambda argv: got.append(argv) or vb.EXIT_OK)
    monkeypatch.setattr(vb, "_run_with_slot",
                        lambda *_a: pytest.fail("子行程入口去取了槽"))
    assert vb.main([vb.JE_CHILD_FLAG, "about:blank", "0"]) == vb.EXIT_OK
    assert got == [["about:blank", "0"]], got


_SITE = "C:/py/Lib/site-packages"


def _set_driver_failure(inner_file, *, chained=True):
    """造一個形狀跟 `set_driver` 丟出來的一樣的例外：原始例外在 `inner_file` 裡丟出，
    包裝層以 `raise … from error`（或只留 `__context__`）重新丟出。"""
    ns = {"EXC": ConnectionError}
    exec(compile("def boom():\n    raise EXC('offline')\n",
                 f"{_SITE}/{inner_file}", "exec"), ns)
    tail = " from error" if chained else ""
    exec(compile("def wrap(boom):\n"
                 "    try:\n"
                 "        boom()\n"
                 "    except Exception as error:\n"
                 f"        raise RuntimeError('set_driver failed'){tail}\n",
                 f"{_SITE}/je_web_runner/webdriver/webdriver_wrapper.py", "exec"), ns)
    try:
        ns["wrap"](ns["boom"])
    except RuntimeError as outer:
        return outer
    raise AssertionError("沒有丟例外")


@pytest.mark.parametrize("inner_file, chained, expected", [
    ("webdriver_manager/core/http.py", True, "install"),
    ("webdriver_manager/core/http.py", False, "install"),            # 只有 __context__
    ("selenium/webdriver/chromium/webdriver.py", True, "launch"),
    ("je_web_runner/webdriver/webdriver_wrapper.py", True, "launch"),  # 選項組裝
    ("my_webdriver_manager_notes/x.py", True, "launch"),             # 近似案例
])
def test_the_failure_stage_tells_the_driver_manager_from_the_browser(
        inner_file, chained, expected):
    """`set_driver` 把兩種失敗包成同一個例外、同一句開頭，只能看原始例外經過哪裡。

    近似案例殺的是「子字串比對」：路徑裡**有** `webdriver_manager` 這幾個字、卻不是
    那個套件。只有 `__context__` 的那一格殺的是「只看 `__cause__`」。
    """
    assert vb._je_failure_stage(
        _set_driver_failure(inner_file, chained=chained)) == expected


@pytest.mark.parametrize("line, expected", [
    ("VERIFY-JE-CHILD: OK", (True, "", "")),
    ("  VERIFY-JE-CHILD: FAIL install ConnectionError('offline')  ",
     (False, "install", "ConnectionError('offline')")),
    ("VERIFY-JE-CHILD: FAIL launch", (False, "launch", "未知原因")),
    ("[je] 清理：wr.quit() …", None),
    ("VERIFY-BROWSER: OK", None),
])
def test_the_child_verdict_line_is_parsed(line, expected):
    assert vb._parse_je_child_line(line) == expected


@pytest.mark.parametrize("line", ["VERIFY-JE-CHILD: OK maybe",
                                  "VERIFY-JE-CHILD: DONE",
                                  "VERIFY-JE-CHILD: FAIL mystery boom"])
def test_an_unrecognised_child_verdict_is_never_read_as_success(line):
    ok, stage, _reason = vb._parse_je_child_line(line)
    assert (ok, stage) == (False, "child"), line


def _stub_je_attempts(monkeypatch, outcomes):
    calls = []
    queue = list(outcomes)

    def _attempt(_url, _headless, attempt):
        calls.append(attempt)
        return queue.pop(0)

    monkeypatch.setattr(vb, "_je_attempt", _attempt)
    monkeypatch.setattr(vb.time, "sleep", lambda _s: None)
    return calls


@pytest.mark.parametrize("stage, retried", [
    ("install", True), ("launch", True), ("page", True),
    ("refused", False), ("timeout", False), ("import", False), ("child", False),
])
def test_a_je_failure_names_its_stage_and_retries_only_transient_ones(
        monkeypatch, stage, retried):
    """FAIL 要說是**哪一步**壞的，而且只有暫時性的那幾類會重試。

    `install` 也重試：正式的 `start_driver` 把整個 `set_driver` 重試三次，驗證這邊
    重試同樣次數才回答得了「正式環境會不會起不來」。拒絕／逾時／匯入失敗重跑不會變。
    """
    calls = _stub_je_attempts(monkeypatch, [(False, stage, "boom()")] * vb.MAX_ATTEMPTS)
    rc, out = _emit(vb.run_smoke_je, "about:blank", True)
    assert rc == vb.EXIT_FAIL
    verdicts = [l for l in out.splitlines() if l.startswith(vb.RESULT_PREFIX)]
    assert len(verdicts) == 1, out
    assert vb._JE_STAGE_LABELS[stage] in verdicts[0], verdicts[0]
    others = [s for s, label in vb._JE_STAGE_LABELS.items()
              if s != stage and label in verdicts[0]]
    assert others == [], f"結論行混進了別的階段：{others}"
    assert calls == (list(range(1, vb.MAX_ATTEMPTS + 1)) if retried else [1]), calls


def test_a_je_pass_after_a_retry_says_so(monkeypatch):
    """重試之後才過要講出來（哪一步、什麼原因），結論行仍然只有一行 OK。"""
    calls = _stub_je_attempts(monkeypatch, [
        (False, "install", "ConnectionError('offline')"), (True, "", "")])
    rc, out = _emit(vb.run_smoke_je, "about:blank", True)
    assert rc == vb.EXIT_OK and calls == [1, 2], (rc, calls)
    assert [l for l in out.splitlines() if l.startswith(vb.RESULT_PREFIX)] == [
        f"{vb.RESULT_PREFIX} OK"], out
    assert "第 2 趟" in out and vb._JE_STAGE_LABELS["install"] in out, out


# 刻意不同的地方（理由正本在 `verify_browser._JE_SMOKE_FLAGS` 上面那段）。
# 正式變體開在螢幕外（2026-09-22）；smoke 換成 headless，`--headed` 時換成最大化給人看。
_JE_PRODUCTION_ONLY = {"--window-position=-32000,-32000", "--window-size=1920,1080"}
_JE_PRODUCTION_ONLY_PREFIXES = {"--user-agent="}        # 對站台的偽裝
_JE_SMOKE_ONLY = {"--no-first-run", "--no-default-browser-check"}  # 全新 profile 才需要


def _production_je_launch_options():
    """`webrunner_je_only.start_driver` 的 `cli_args` 與 `experimental`（AST，不 import）。"""
    func = _function_in("webrunner_je_only.py", "start_driver")
    flags, prefixes, experimental = set(), set(), None
    for node in ast.walk(func):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        if node.targets[0].id == "cli_args" and isinstance(node.value, ast.List):
            for elt in node.value.elts:
                if isinstance(elt, ast.Constant):
                    flags.add(elt.value)
                else:
                    prefixes.add(_flat_literal(elt))
        elif node.targets[0].id == "experimental":
            experimental = ast.literal_eval(node.value)
    return flags, prefixes, experimental


def test_the_je_smoke_launches_chrome_the_way_production_does():
    """je smoke 的旗標要跟正式 je 變體**同一組**，差別只能是登記過的那幾個。

    這支 smoke 存在的理由就是驗 je 那一側的選項組裝；正式變體加了一個會讓 Chrome 起
    不來的旗標而這裡沒跟上，它就會印一個看起來很正常的 OK。兩個方向都查，例外清單
    也查過期（登記了一個正式那邊已經沒有的旗標＝那筆例外不再代表任何東西）。
    """
    flags, prefixes, experimental = _production_je_launch_options()
    assert len(flags) >= 10 and "--user-data-dir=" in prefixes, (
        f"從 start_driver 抽不到旗標，抽取器壞了：{flags} {prefixes}")
    smoke = set(vb._JE_SMOKE_FLAGS)
    missing = flags - _JE_PRODUCTION_ONLY - smoke
    extra = smoke - _JE_SMOKE_ONLY - flags
    assert not missing, f"正式 je 變體有、smoke 沒有的旗標：{sorted(missing)}"
    assert not extra, f"smoke 多出來、沒有登記理由的旗標：{sorted(extra)}"
    assert _JE_PRODUCTION_ONLY <= flags and _JE_PRODUCTION_ONLY_PREFIXES <= prefixes, (
        "例外清單過期：登記的旗標正式那邊已經沒有了")
    assert _JE_SMOKE_ONLY.isdisjoint(flags), "例外清單過期：正式那邊也加了這個旗標"
    assert prefixes - _JE_PRODUCTION_ONLY_PREFIXES == {"--user-data-dir="}, prefixes
    assert experimental == vb._je_experimental_options(), experimental

    headless, headed = vb._je_cli_args("P", True), vb._je_cli_args("P", False)
    assert "--headless=new" in headless and "--start-maximized" not in headless
    assert "--start-maximized" in headed
    assert not any(a.startswith("--headless") for a in headed), headed
    assert headless[-1] == headed[-1] == "--user-data-dir=P"


def test_the_je_child_loads_the_same_library_copy_as_production():
    """子行程要載入正式 je 變體會載入的**同一份** `je_web_runner`。

    正式變體優先用 repo 旁邊的開發簽出。這裡的規則若跟它不一樣，驗到的就是另一份程式碼。
    """
    prod = {n.targets[0].id: n.value for n in _tree_of("webrunner_je_only.py").body
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)}
    ours = {n.targets[0].id: n.value for n in _tree_of("verify_browser.py").body
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)}
    assert ast.unparse(prod["_WR_SIBLING"]) == ast.unparse(
        ours["_JE_SIBLING_WEBRUNNER"])

    def env_keys(node):
        return {c.args[0].value for c in ast.walk(node) if isinstance(c, ast.Call)
                and _dotted_name(c.func) == "os.environ.get" and c.args
                and isinstance(c.args[0], ast.Constant)}

    assert env_keys(prod["_WR_ENV"]) == {"WEBRUNNER_PATH"}, "抽取器壞了"
    assert env_keys(_function_in("verify_browser.py", "_je_child_main")) == {
        "WEBRUNNER_PATH"}


# ---------------------------------------------------------------------------
# `_run_with_slot`：槽拿不到的那條讓位路徑（2026-09-21 分支覆蓋率盤點）
# ---------------------------------------------------------------------------
# 正式作業在跑、pid 檔讀不出來、乾淨的機器——這三條上面都有行為測試（`_run_slot_with`），
# 唯獨**一開始就拿不到槽**的那條，整套測試從來沒走過：上面的替身永遠拿得到槽。
# 驗證守則把它寫成承諾（偵測到正式作業會讓位、SKIP 不是 FAIL），所以要用行為釘住：
# 回 SKIP、不開瀏覽器、也不去放一個沒拿到的槽。

class _SlotThatMayRefuse:
    def __init__(self, grant: bool):
        self._grant = grant
        self.calls: list = []

    def acquire(self, owner, *, timeout=0.0, label=""):
        self.calls.append(("acquire", owner))
        return self._grant

    def release(self, owner):
        self.calls.append(("release", owner))


def test_a_busy_slot_stands_aside_without_opening_a_browser(monkeypatch, capsys):
    slot = _SlotThatMayRefuse(False)
    monkeypatch.setitem(sys.modules, "_chrome_slot", slot)
    started: list = []

    def run_fn():
        started.append(True)
        return vb.EXIT_OK

    code = vb._run_with_slot("probe", run_fn)
    verdicts = [line for line in capsys.readouterr().out.splitlines()
                if line.startswith(vb.RESULT_PREFIX)]
    assert code == vb.EXIT_SKIP
    assert len(verdicts) == 1 and verdicts[0].split()[1] == "SKIP", verdicts
    assert started == [], "槽拿不到還是開了瀏覽器"
    assert slot.calls == [("acquire", vb.SLOT_OWNER)], (
        f"沒拿到的槽不該由這裡去放：{slot.calls}")


def test_the_slot_is_freed_even_when_the_work_explodes(monkeypatch, tmp_path):
    """驗證本身炸掉：槽一定要放掉，否則下一次正式作業會被一個已經不存在的驗證擋住。"""
    slot = _SlotThatMayRefuse(True)
    monkeypatch.setitem(sys.modules, "_chrome_slot", slot)
    _pid_file(monkeypatch, tmp_path, None)

    def run_fn():
        raise RuntimeError("browser crashed")

    with pytest.raises(RuntimeError):
        vb._run_with_slot("probe", run_fn)
    assert slot.calls[-1] == ("release", vb.SLOT_OWNER), slot.calls
