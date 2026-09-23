"""`mutation_harness` 自己的守門。

**這支工具的失效方向只有一個，而它剛好是最糟的那個**：把一組其實沒問題的測試判成
「守不住」，或反過來把一個真的沒被抓到的變異算成 KILLED。兩種都不會有錯誤訊息——
輸出照樣是一份看起來很正常的 KILLED／SURVIVED 清單。2026-09-08 一天之內就有兩支
手寫的骨架用**不同的方式**產出假的 SURVIVED（`-k` 選不到任何測試、錨點出現兩次被
靜靜跳過），這支工具存在就是為了把那兩條變成程式碼裡的斷言。

所以這裡測的是**那些斷言本身**，而不是「變異測試會不會跑」。

`_pytest` 與 `_collected_count` 一律換掉：真的去跑 pytest 會讓這支變慢、而且會在
pytest 裡再開一個 pytest。換掉之後 `run_mutations` 的控制流程仍然是真的在跑。
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import mutation_harness as mh  # noqa: E402

SOURCE = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
NODE = "test/test_x.py::test_y"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把 `PROJECT_ROOT` 與快照目錄都導到 tmp，並換掉兩個會開子行程的 helper。"""
    root = tmp_path / "repo"
    (root / "axiomatic").mkdir(parents=True)
    target = root / "axiomatic" / "thing.py"
    target.write_text(SOURCE, encoding="utf-8")
    # 測試檔住在 repo 根目錄的 `test/`（2026-09-22 起），「整個測試檔」那條路要找得到它。
    (root / "test").mkdir()
    (root / "test" / "test_x.py").write_text("def test_y():\n    pass\n",
                                             encoding="utf-8")
    monkeypatch.setattr(mh, "PROJECT_ROOT", root)

    state = {"collected": 1, "results": [], "calls": 0}

    def fake_collected(_tests):
        return state["collected"]

    def fake_pytest(_tests, **_kw):
        state["calls"] += 1
        if state["results"]:
            return state["results"].pop(0)
        return 0, "1 passed"

    monkeypatch.setattr(mh, "_collected_count", fake_collected)
    monkeypatch.setattr(mh, "_pytest", fake_pytest)
    state["target"] = target
    state["snapshot_dir"] = tmp_path / "snap"
    return state


def _run(sandbox, mutants):
    return mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE], mutants=mutants,
        snapshot_dir=sandbox["snapshot_dir"])


# ---------------------------------------------------------------------------
# 兩條「假 SURVIVED」的來源
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "test_x.py::test_y",             # 沒有 test/ 前綴
    "-k something",                  # 直接想用 -k
    "test/not_here.py",              # 整個檔案，但檔案不存在
])
def test_a_selector_that_is_not_a_full_node_id_is_refused(sandbox, bad):
    """`-k` 不接受；「整個檔案」可以，但檔案要真的存在。

    `-k` 打錯字時 pytest 是 `0 passed`、rc=0，於是每一個變異都顯示成 SURVIVED——
    看起來就跟「這組測試很弱」一模一樣。這是 2026-09-08 真的發生過的事。
    """
    with pytest.raises(ValueError, match="node ID"):
        mh.run_mutations(target="axiomatic/thing.py", tests=[bad],
                         mutants=[("x", "return 1", "return 9")],
                         snapshot_dir=sandbox["snapshot_dir"])


def test_a_whole_test_file_is_accepted(sandbox):
    """整個檔案要能選——**這一條是為了讓人不必手挑 node ID。**

    2026-09-10 之前這裡是被擋的，理由沿用 `-k` 那條。但手挑 node ID 正是變異分數
    灌水最容易發生的地方（§8.8(A14)：在副本上會安靜縮水的兩個變數，一個是變異
    集合，另一個就是測試集合）。擋掉整份檔案等於把人推向那個坑，所以放寬。
    """
    result = mh.run_mutations(
        target="axiomatic/thing.py",
        tests=["test/test_x.py"],               # sandbox 裡真的有這個檔
        mutants=[("x", "return 1", "return 9")],
        snapshot_dir=sandbox["snapshot_dir"])
    assert result.mutants, "整份檔案被接受了，卻一個變異都沒跑到"
    assert result.restored


def test_a_selection_that_matches_nothing_is_refused(sandbox):
    """node ID 拼對了格式但選不到東西 → 拒跑，而不是安靜地全部 SURVIVED。"""
    sandbox["collected"] = 0
    with pytest.raises(AssertionError, match="一支測試都沒選到"):
        _run(sandbox, [("x", "return 1", "return 9")])


def test_an_ambiguous_anchor_is_an_error_not_a_silent_skip(sandbox):
    """錨點出現兩次 → `ERROR`，而且**要算進存活**。

    手寫骨架常見的寫法是 `continue` 跳過並印一行 SKIP，夾在一串 KILLED 中間很容易
    被讀成「這個變異不適用」。這裡讓它進結果、讓 `all_killed` 變 False。
    """
    result = _run(sandbox, [("重複的錨點", "return", "return  # x")])
    assert [m.verdict for m in result.mutants] == [mh.ERROR]
    assert "出現 2 次" in result.mutants[0].detail
    assert not result.all_killed
    assert result.survivors == ["重複的錨點"]


def test_an_anchor_that_no_longer_exists_is_also_an_error(sandbox):
    """零筆同樣是 ERROR——那代表原文已經改了，這個變異根本沒套用上去。"""
    result = _run(sandbox, [("過期的錨點", "return 42", "return 43")])
    assert result.mutants[0].verdict == mh.ERROR
    assert "出現 0 次" in result.mutants[0].detail


# ---------------------------------------------------------------------------
# 判定本身
# ---------------------------------------------------------------------------
def test_a_failing_run_is_killed_and_a_passing_run_survives(sandbox):
    """紅 = KILLED、綠 = SURVIVED。反面一起測，否則「一律 KILLED」也會通過。"""
    sandbox["results"] = [(0, "1 passed"),      # 基準
                          (1, "1 failed"),      # 第一個變異：被抓到
                          (0, "1 passed")]      # 第二個變異：沒被抓到
    result = _run(sandbox, [("抓得到", "return 1", "return 9"),
                            ("抓不到", "return 2", "return 8")])
    assert [m.verdict for m in result.mutants] == [mh.KILLED, mh.SURVIVED]
    assert result.survivors == ["抓不到"]
    assert not result.all_killed


def test_all_killed_needs_at_least_one_mutant(sandbox):
    """一個變異都沒跑不算「全殺」——空集合不該回報成成功。"""
    result = _run(sandbox, [])
    assert not result.all_killed


def test_a_red_baseline_refuses_to_run_any_mutant(sandbox):
    """基準就紅的話整批結果沒有意義，直接拒跑。"""
    sandbox["results"] = [(1, "3 failed")]
    with pytest.raises(AssertionError, match="基準就是紅的"):
        _run(sandbox, [("x", "return 1", "return 9")])


# ---------------------------------------------------------------------------
# 還原
# ---------------------------------------------------------------------------
def test_the_target_is_byte_identical_afterwards(sandbox):
    """跑完之後目標檔案要跟原本一模一樣。"""
    _run(sandbox, [("a", "return 1", "return 9"),
                   ("b", "return 2", "return 8")])
    assert sandbox["target"].read_text(encoding="utf-8") == SOURCE


def test_the_target_is_restored_even_when_the_run_blows_up(sandbox,
                                                           monkeypatch):
    """`_pytest` 中途丟例外，檔案照樣要還原——不然下一次會從壞掉的基準開始。"""
    calls = {"n": 0}

    def exploding(_tests, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0, "1 passed"          # 基準
        raise RuntimeError("子行程炸了")

    monkeypatch.setattr(mh, "_pytest", exploding)
    with pytest.raises(RuntimeError):
        _run(sandbox, [("x", "return 1", "return 9")])
    assert sandbox["target"].read_text(encoding="utf-8") == SOURCE


def test_a_write_that_fails_halfway_still_leaves_the_target_intact(
        sandbox, monkeypatch):
    """寫入變異的那一步**自己**炸掉時，檔案也要還原。

    這是最外層那個 `finally` 唯一守得到、而內層守不到的情況：內層的 `try` 是從
    「變異已經寫進去了」才開始的，所以 `write_text` 在**截斷之後、寫完之前**失敗
    （磁碟滿、防毒攔截）留下的半截檔案，只有最外層接得住。
    少了它，下一次執行會拿一個半截的檔案當基準——而基準是綠是紅都不能信。
    """
    real_write = Path.write_text

    def truncating_write(self, data, *args, **kwargs):
        if self.name == "thing.py":
            real_write(self, "已經截斷了", *args, **kwargs)   # 先毀掉
            raise OSError("寫到一半失敗")
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", truncating_write)
    with pytest.raises(OSError):
        _run(sandbox, [("x", "return 1", "return 9")])
    monkeypatch.undo()
    assert sandbox["target"].read_text(encoding="utf-8") == SOURCE, (
        "寫到一半失敗之後檔案沒有被還原")


def test_a_leftover_mutation_is_healed_before_the_baseline_runs(sandbox):
    """上一次被砍掉、留下變異中的檔案 → 這一次啟動要先還原。

    這是靠 `finally` 還原**擋不住**的情況——行程被殺掉時 `finally` 根本不會跑，
    所以「進行中」標記會留在原地，而那正是可以還原的憑據。
    """
    _run(sandbox, [("x", "return 1", "return 9")])          # 建立快照
    # 模擬「上一次被砍掉」：檔案留在變異狀態，而且標記還在。
    sandbox["target"].write_text(SOURCE.replace("return 1", "return 999"),
                                 encoding="utf-8")
    (sandbox["snapshot_dir"] / "thing.py.inprogress").write_text(
        "x", encoding="utf-8")
    _run(sandbox, [("y", "return 2", "return 8")])
    assert sandbox["target"].read_text(encoding="utf-8") == SOURCE, (
        "殘留的變異沒有被 heal 掉——這一輪的基準是錯的")


def test_an_ordinary_edit_between_runs_is_never_reverted(sandbox):
    """**沒有標記時不可以還原。** 這是這支工具自己咬過的那個 bug 的回歸測試。

    第一版的 heal 判斷是「檔案跟快照不一樣就還原」，那分不出兩件事：
    「上一次被砍掉留下的變異」與「這段期間有人正常地編輯了這個檔案」。
    2026-09-08 寫這支的當天就被咬到：改完 `mutation_harness.py` 之後拿它自己跑自我
    驗證，heal 拿三分鐘前的快照把剛寫好的修正**整段還原掉，而且什麼都沒說**——症狀
    只是「基準莫名其妙變紅」。一個會安靜退掉別人修改的工具，比沒有這個工具糟。
    """
    _run(sandbox, [("x", "return 1", "return 9")])          # 建立快照
    edited = SOURCE + "\n\ndef h():\n    return 3\n"
    sandbox["target"].write_text(edited, encoding="utf-8")   # 正常的編輯
    _run(sandbox, [("y", "return 2", "return 8")])
    assert sandbox["target"].read_text(encoding="utf-8") == edited, (
        "兩次執行之間的正常編輯被 heal 退掉了")


def test_a_clean_run_leaves_no_in_progress_marker(sandbox):
    """乾淨收工要把標記刪掉，否則下一次會誤判成「上一次沒跑完」。"""
    _run(sandbox, [("x", "return 1", "return 9")])
    assert not (sandbox["snapshot_dir"] / "thing.py.inprogress").exists()


def test_a_target_that_is_not_utf8_is_refused_with_a_clear_message(sandbox):
    """目標檔不是 UTF-8 → 講清楚並拒跑，不要丟一個裸的 `UnicodeDecodeError`。

    **刻意不用 `errors="replace"` 硬讀下去**：替代字元會讓錨點比對錯位，於是變異
    看起來套用成功、實際上改到別的地方——那又是一個「假的 KILLED／SURVIVED」來源，
    而這支工具存在的理由正是消滅那一類。
    """
    sandbox["target"].write_bytes(
        "def f():\n    return 1  # 中文註解\n".encode("cp950"))
    with pytest.raises(AssertionError, match="不是 UTF-8"):
        _run(sandbox, [("x", "return 1", "return 9")])


def test_the_report_names_the_survivors(sandbox):
    """報表要看得出誰活著；`all_killed` 是給呼叫端直接斷言用的。"""
    sandbox["results"] = [(0, "1 passed"), (0, "1 passed")]
    result = _run(sandbox, [("活下來的", "return 1", "return 9")])
    text = result.report()
    assert "活下來的" in text and mh.SURVIVED in text
    assert "0/1 殺掉" in text


# ---------------------------------------------------------------------------
# 時間上限（2026-09-10 補）
#
# 起因是真的踩到：變異 M5 讓 child pytest 跑了 31 分鐘牆鐘、只用掉 39 秒 CPU，
# 而 `_pytest` 當時沒有任何 `timeout=`，所以 `run_mutations` 會**永遠**等下去——
# 而且是在目標檔已經被寫入變異的狀態下等。掛住比失敗糟，掛住又同時汙染著一個
# 長命行程會重新匯入的模組更糟。
# ---------------------------------------------------------------------------


def test_a_child_that_never_finishes_is_a_timeout_not_a_survivor(sandbox):
    """逾時要有自己的判定，而且**不能**被算成存活。

    `_pytest` 逾時回的 rc 是 `None`（不是某個假的非零值），呼叫端因此沒辦法把它
    誤讀成一般的失敗。
    """
    sandbox["results"] = [(0, "1 passed"),          # 基準
                          (None, "child 超過上限")]  # 變異：跑不完
    result = _run(sandbox, [("x", "return 1", "return 9")])
    assert result.mutants[0].verdict == mh.TIMEOUT, result.report()
    assert result.survivors == [], "逾時被算成存活了——測試明明對它有反應"
    assert result.timeouts == ["x"]
    assert "跑不完" in result.report(), "報告沒有把逾時跟乾淨的擊殺分開講"


def test_the_child_run_is_actually_bounded(monkeypatch):
    """`_pytest` 真的把上限傳下去了——常數存在但沒接上是最沒用的一種修法。"""
    seen = {}

    class _Done:
        returncode = 0
        stdout = "1 passed"

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _Done()

    monkeypatch.setattr(mh.subprocess, "run", fake_run)
    mh._pytest(["test/test_x.py::test_y"])
    assert seen["timeout"] == mh.CHILD_RUN_TIMEOUT_SEC, (
        f"child 沒有牆鐘上限（timeout={seen['timeout']}）")
    assert any(str(a).startswith("--timeout=") for a in seen["cmd"]), (
        "沒有把 pytest-timeout 的每支測試上限傳下去。那道才是會**指名**卡住的是"
        f"哪一支測試的：{seen['cmd']}")


def test_a_timeout_is_reported_even_though_it_is_not_a_survivor(sandbox):
    """反面：正常的擊殺不能被誤標成逾時。"""
    sandbox["results"] = [(0, "1 passed"),   # 基準
                          (1, "1 failed")]   # 變異：被斷言抓到
    result = _run(sandbox, [("x", "return 1", "return 9")])
    assert result.mutants[0].verdict == mh.KILLED
    assert result.timeouts == []
    assert "跑不完" not in result.report()


# ---------------------------------------------------------------------------
# 「child 真的跑過嗎」（2026-09-10 補）
#
# 這一條是用一次假的滿分換來的。一次背景變異執行印出 10/10 殺掉；牆鐘卻只有
# 2 分 08 秒，而基準自己就報了 114 秒——十個 child 平分不到九秒，每一個的 stdout
# 都是空的。它們是被 job object 連帶殺掉的（上一個宿主行程結束），不是被斷言抓到
# 的。事後單獨重跑同一個變異，跑滿 108 秒、**存活**。
#
# 假的 KILLED 正是本模組 docstring 說它要防的那個方向，而既有的三條規則全都在守
# 「測試選得對不對」，沒有一條守「child 有沒有跑起來」。基準有正面對照組，變異那
# 一輪沒有。
# ---------------------------------------------------------------------------


def test_a_child_that_never_ran_is_not_a_kill(sandbox):
    """rc 非零、但什麼都沒回報 → 沒有結論，不是擊殺。"""
    sandbox["results"] = [(0, "1 passed"),        # 基準
                          (1, "(沒有輸出)")]      # 變異：child 當場死掉
    result = _run(sandbox, [("x", "return 1", "return 9")])
    assert result.mutants[0].verdict == mh.NO_EVIDENCE, result.report()
    assert result.no_evidence == ["x"]
    assert not result.all_killed, (
        "沒有證據的一輪被算成擊殺了——這正是那次假滿分的成因")
    assert "沒跑到測試" in result.report()


def test_the_real_pytest_summaries_are_all_recognised():
    """正面對照：真的跑過的輸出必須被認得，否則這道閘會逢跑必擋。

    一道老是誤報的閘就是一道會被關掉的閘（`CLAUDE.md` 的 Language / 文字編碼兩節
    都寫著同一句）。這些字串都是這台機器上 `-q --no-header` 真的印過的最後一行。
    """
    for line in ["600 passed, 5 warnings in 108.97s (0:01:48)",
                 "222 passed, 5 warnings in 20.15s",
                 "1 failed, 599 passed in 110.02s (0:01:50)",
                 "3 failed in 0.44s",
                 "1 error in 0.34s",
                 "2 passed, 1 skipped in 0.12s",
                 "no tests ran in 0.11s"]:
        assert mh._looks_like_a_test_run(line), f"認不得真的跑過的輸出：{line!r}"


def test_the_silent_shapes_are_all_rejected():
    """反面對照：child 沒跑起來時的樣子，一個都不能被當成跑過。"""
    for line in ["", "(沒有輸出)", "   ",
                 "（stdout 是空的）stderr 末行：ImportError while loading conftest",
                 "（stdout 是空的）stderr 末行：ERROR: file or directory not found"]:
        assert not mh._looks_like_a_test_run(line), (
            f"把「沒跑起來」認成跑過了：{line!r}")


def test_stderr_is_kept_when_stdout_is_empty(monkeypatch):
    """child 死在 stdout 之前時，把 stderr 留下來——那通常正好寫著原因。"""

    class _Done:
        returncode = 4
        stdout = ""
        stderr = "ImportError while loading conftest '/x/conftest.py'.\n"

    monkeypatch.setattr(mh.subprocess, "run", lambda cmd, **kw: _Done())
    rc, tail = mh._pytest([NODE])
    assert rc == 4
    assert "conftest" in tail, f"stderr 被丟掉了：{tail!r}"
    assert not mh._looks_like_a_test_run(tail), (
        "conftest 掛掉被認成一次正常的測試執行")


# ---------------------------------------------------------------------------
# 「無能變異」：讓程式碼載不起來的那一種（2026-09-10 補）
#
# pytest 對「收集期就爆掉」回的是 `1 error`、rc 非零——跟「斷言抓到變異」在回傳值
# 上一模一樣。混在一起算的後果是**變異分數被高估**：越笨拙的變異越容易「殺掉」。
# 變異測試的文獻把它獨立成一格並從分母拿掉（Stryker 的 CompileError／RuntimeError
# 是 excluded，分數是 `detected / valid`）。這裡照做，但**不照做**它的 fail-open：
# 這一格照樣算存活，否則一組全部寫壞的變異會印出「0/0，沒有存活者」。
# ---------------------------------------------------------------------------


def test_a_mutant_that_breaks_the_import_is_not_a_kill(sandbox):
    """`1 error`＋rc 非零，但一支測試都沒執行到 → 不是擊殺。"""
    sandbox["results"] = [(0, "1 passed"),          # 基準
                          (2, "1 error in 0.34s")]  # 變異：收集期就爆
    result = _run(sandbox, [("壞掉的變異", "return 1", "return 9")])
    assert result.mutants[0].verdict == mh.INCOMPETENT, result.report()
    assert result.incompetents == ["壞掉的變異"]
    assert "載不起來" in result.report()


def test_an_incompetent_mutant_leaves_the_denominator(sandbox):
    """不計分：分母是**有效**變異，不是變異總數。

    兩個變異，一個真的被殺、一個載不起來 → `1/1`，不是 `1/2`。把後者算進分母會
    讓一組寫壞的變異看起來像「測試不夠好」，算成擊殺則會讓它看起來像滿分；兩種
    都是把「這一格沒有結論」講成了一個結論。
    """
    sandbox["results"] = [(0, "1 passed"),           # 基準
                          (1, "1 failed in 0.20s"),  # 真的被抓到
                          (2, "1 error in 0.34s")]   # 載不起來
    result = _run(sandbox, [("真的被殺", "return 1", "return 9"),
                            ("壞掉的變異", "return 1", "return 9")])
    text = result.report()
    assert "1/1 殺掉" in text, text
    assert "另有 1 個不計分" in text, text


def test_an_incompetent_mutant_still_counts_as_a_survivor(sandbox):
    """**刻意不照 Stryker**：不計分，但不算「沒有存活者」。

    純粹排除分母是 fail-open 的——全部寫壞的一批會得到「0/0」，而那跟「守門很穩」
    在輸出上一模一樣。`empty selection looks like a clean result` 的同一個形狀。
    """
    sandbox["results"] = [(0, "1 passed"), (2, "1 error in 0.34s")]
    result = _run(sandbox, [("壞掉的變異", "return 1", "return 9")])
    assert not result.all_killed, (
        "一個載不起來的變異被算成「這組守門很穩」了")


def test_no_tests_ran_is_not_a_kill_either(sandbox):
    """`no tests ran` 也是一支都沒執行到——同一格。

    這一格單獨要一支測試：`no tests ran` 符合「有回報摘要」的形狀（所以過得了
    NO-EVIDENCE 那道），卻一支測試都沒跑。少了這一支，把選擇器打壞成選不到東西
    而又剛好 rc 非零的那一輪，會被算成擊殺。
    """
    sandbox["results"] = [(0, "1 passed"), (5, "no tests ran in 0.11s")]
    result = _run(sandbox, [("選不到測試", "return 1", "return 9")])
    assert result.mutants[0].verdict == mh.INCOMPETENT, result.report()


def test_the_executed_predicate_separates_ran_from_reported():
    """正反兩面各一組，理由同 `_looks_like_a_test_run` 那兩支。

    這兩個判準**刻意不同**：`_looks_like_a_test_run` 問「child 有沒有講話」，
    `_executed_any_test` 問「有沒有測試真的跑」。合成一個就會把「載不起來」跟
    「被殺掉的 child」混為一談，而那兩者要做的事不一樣（前者改變異、後者重跑）。
    """
    for line in ["600 passed, 5 warnings in 108.97s (0:01:48)",
                 "1 failed, 599 passed in 110.02s (0:01:50)",
                 "3 failed in 0.44s",
                 "1 xfailed in 0.10s",
                 "1 error, 2 passed in 0.44s"]:
        assert mh._executed_any_test(line), f"認不得真的跑過測試：{line!r}"
    for line in ["1 error in 0.34s",
                 "2 errors in 1.20s",
                 "no tests ran in 0.11s",
                 "3 skipped in 0.05s",
                 "5 deselected in 0.08s",
                 ""]:
        assert not mh._executed_any_test(line), (
            f"把「一支都沒跑」認成跑過了：{line!r}")


# ---------------------------------------------------------------------------
# 「跑到一半停掉」：執行數少於**選到**的數（2026-09-20 補）
#
# 上面兩格問的是「有沒有講話」與「有沒有跑到測試」，都只要**一支**就算數。中間還
# 漏著一種：跑了幾支之後整輪倒掉。實例是 conftest 把 `ast.walk` 換成攤平版之後對它
# 做變異——pytest 自己的 traceback 排版也用 `ast.walk`，於是斷言失敗、pytest 在印那
# 個失敗時 INTERNALERROR，末行停在 `1 passed`、rc 非零，被判成一次乾淨的 KILLED。
# 分母（基準那行的「選到 N 支」）一直都在手上，只是沒拿來比。
# ---------------------------------------------------------------------------


def test_a_run_that_stops_halfway_is_not_counted_as_a_kill(sandbox):
    """15 支選到、只跑了 1 支就停 → `ABORTED`，不是 KILLED。"""
    sandbox["collected"] = 15
    sandbox["results"] = [(0, "15 passed in 3.15s"),
                          (3, "1 passed, 5 warnings in 3.31s")]
    result = _run(sandbox, [("把執行器自己弄垮", "return 1", "return 9")])
    assert result.mutants[0].verdict == mh.ABORTED, result.report()
    assert result.aborted == ["把執行器自己弄垮"]
    assert "只執行到 1/15" in result.report()
    assert not result.all_killed, "沒跑完的一輪被算成「這組守門很穩」了"
    assert "0/0 殺掉（另有 1 個不計分）" in result.report()


def test_a_run_that_finished_with_failures_is_still_a_clean_kill(sandbox):
    """**必須放行的那一半。** 少了這一支，上面那條會把每一次正常的擊殺也擋下來，
    而「只擋不放」的語料殺不掉「把 `<` 寫成 `<=`」之類的變異。"""
    sandbox["collected"] = 15
    sandbox["results"] = [(0, "15 passed in 3.15s"),
                          (1, "12 failed, 3 passed, 5 warnings in 3.81s")]
    result = _run(sandbox, [("真的被斷言抓到", "return 1", "return 9")])
    assert result.mutants[0].verdict == mh.KILLED, result.report()
    assert result.all_killed


def test_a_setup_error_still_counts_as_having_executed(sandbox):
    """`10 passed, 1 error` 是 11 支都算執行到。`error` 多半是 fixture 爆掉，那一支
    在 `--collect-only` 的數字裡；不算它的話，每一次 fixture 出錯都會被誤判成中途停掉。"""
    sandbox["collected"] = 11
    sandbox["results"] = [(0, "11 passed"), (1, "10 passed, 1 error in 0.5s")]
    result = _run(sandbox, [("fixture 爆掉", "return 1", "return 9")])
    assert result.mutants[0].verdict == mh.KILLED, result.report()


def test_the_executed_count_reads_every_outcome_word():
    """純函式的正反面。`deselected` 刻意不算：那是被選掉、沒有執行。"""
    assert mh._executed_count("12 failed, 3 passed, 5 warnings in 3.81s") == 15
    assert mh._executed_count("10 passed, 1 error in 0.5s") == 11
    assert mh._executed_count("2 passed, 4 skipped, 1 xfailed, 1 xpassed in 1s") == 8
    assert mh._executed_count("2 errors in 1.20s") == 2
    assert mh._executed_count("3 passed, 12 deselected in 1s") == 3
    assert mh._executed_count("no tests ran in 0.11s") == 0
    assert mh._executed_count("") == 0


# ---------------------------------------------------------------------------
# 被砍掉之後：樹上還躺著一個變異
# ---------------------------------------------------------------------------
# `finally` 的還原在行程被砍掉時不會跑。骨架自己下一次啟動會 heal，但**被砍掉之後
# 最可能的下一件事是有人跑一次完整測試**，那時樹還是壞的。2026-09-10 實際發生過：
# 一輪三個變異跑到第二個時 host 行程結束，`test_bot_helpers.py` 的別名不動點那八行
# 被換成 `pass` 留在原地，標記也還在。
#
# `interrupted_targets` 就是給 `conftest.py` 在收集前問的那一句。它的價值在於
# **平常完全安靜**，所以「不該叫的時候不要叫」跟「該叫的時候要叫」一樣重要。


def _mark(sandbox, name="axiomatic/thing.py"):
    """建立快照 + 進行中標記，模擬「跑到一半」的狀態。"""
    snap_dir = sandbox["snapshot_dir"]
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "thing.py.orig").write_text(SOURCE, encoding="utf-8")
    (snap_dir / "thing.py.inprogress").write_text(name, encoding="utf-8")
    return snap_dir


def test_a_mutant_left_on_the_tree_is_reported(sandbox):
    """標記還在，而且檔案跟快照不一樣 → 這就是「上一輪沒跑完」。"""
    snap_dir = _mark(sandbox)
    sandbox["target"].write_text(SOURCE.replace("return 1", "pass"),
                                 encoding="utf-8")
    assert mh.interrupted_targets(snap_dir) == ["axiomatic/thing.py"]


def test_a_clean_tree_is_not_reported_even_with_the_marker_still_there(sandbox):
    """還原成功、但還沒刪掉標記的那個空隙**不算髒**。

    這個視窗是真的存在的：`run_mutations` 只有在確認位元組相同之後才刪標記，而
    行程可能正好死在中間；還原失敗那條路徑更是**刻意**把標記留著。那些情況下樹是
    好的，擋下整套測試只會讓人學會忽略這道訊息。
    """
    snap_dir = _mark(sandbox)
    assert mh.interrupted_targets(snap_dir) == []


def test_an_edited_file_without_a_marker_is_not_reported(sandbox):
    """沒有標記就不算——那多半是有人正常地編輯了這個檔案。

    這正是 heal 本身踩過的雷：第一版用「檔案跟快照不一樣就還原」，結果把合法的
    修改靜靜地退掉。判準要兩個條件同時成立，少一個就會開始冤枉人。
    """
    snap_dir = sandbox["snapshot_dir"]
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "thing.py.orig").write_text(SOURCE, encoding="utf-8")
    sandbox["target"].write_text(SOURCE.replace("return 1", "pass"),
                                 encoding="utf-8")
    assert mh.interrupted_targets(snap_dir) == []


def test_a_marker_pointing_at_something_that_is_not_there_is_not_reported(sandbox):
    """標記指到一個不存在的檔案 → 沒有證據，不要擋人。"""
    snap_dir = _mark(sandbox, name="axiomatic/vanished.py")
    assert mh.interrupted_targets(snap_dir) == []


def test_a_missing_snapshot_dir_is_not_reported(tmp_path):
    """快照目錄根本不在（例如換一台機器）→ 安靜。"""
    assert mh.interrupted_targets(tmp_path / "nope") == []


def test_a_finished_run_leaves_nothing_for_the_guard_to_find(sandbox):
    """正常跑完一輪之後，這道查詢必須是空的。

    反面對照組：上面那些都是人工擺出來的狀態，這一支確認**真的跑過一輪**
    `run_mutations` 之後不會留下誤報——否則每跑一次變異就會擋掉下一次測試。
    """
    result = _run(sandbox, [("換掉回傳值", "return 1", "return 9")])
    assert result.restored, result.report()
    assert mh.interrupted_targets(sandbox["snapshot_dir"]) == []


def test_the_child_env_carries_the_flag_and_the_rest_of_the_environment():
    """child 要拿得到旗標，**而且**要拿得到原本的環境。

    只傳那一個變數的話 child 在 Windows 上根本起不來（少了 `SYSTEMROOT`
    連 socket 都初始化不了），而那會表現成一批 NO-EVIDENCE，不是一個明顯的錯誤。
    """
    env = mh._child_env()
    assert env[mh.CHILD_ENV_FLAG] == "1"
    missing = [name for name in os.environ if name not in env]
    assert not missing, f"child 的環境掉了這些：{missing[:5]}"


def test_the_progress_callback_sees_each_mutant_as_it_finishes(sandbox):
    """每跑完一個就回報一次——被砍掉時已經付過代價的那幾筆才留得住。"""
    seen = []
    mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE],
        mutants=[("一", "return 1", "return 9"),
                 ("二", "return 2", "return 8")],
        snapshot_dir=sandbox["snapshot_dir"], progress=seen.append)
    assert [m.label for m in seen] == ["一", "二"]


def test_a_progress_callback_that_explodes_does_not_break_the_restore(sandbox):
    """回報用的 callback 是呼叫端給的，它爆掉不可以連累還原。

    報告是次要的，把樹還原回去才是主要的。這支測的就是優先順序。
    """
    def boom(_mutant):
        raise RuntimeError("報告壞了")

    result = mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE],
        mutants=[("一", "return 1", "return 9")],
        snapshot_dir=sandbox["snapshot_dir"], progress=boom)
    assert result.restored, result.report()
    assert mh.interrupted_targets(sandbox["snapshot_dir"]) == []


# ---------------------------------------------------------------------------
# 沿用上一次的結果（resume）
# ---------------------------------------------------------------------------
# 一輪動輒幾十分鐘，被砍掉就全部重來——mutmut（`.mutmut-cache`）與 Cosmic Ray
# （session 資料庫）都因此做了續跑。**但快取正是這類工具最容易產出「看起來很正常
# 的錯誤結論」的地方**：mutmut 最有名的抱怨（boxed/mutmut#104）就是「我修好測試再
# 跑一次，它還是報上次的結果」。錯的不是快取，是**看不見的**快取。
#
# 所以這裡兩件事都要測：沿用要真的省掉那一輪，而且任何可能讓結論改變的事都要讓整份
# 紀錄作廢。


def test_resume_reuses_the_previous_verdict_without_running_it_again(sandbox):
    """同一組跑第二次：不再開 child，而且結果標成「沿用」。"""
    mutants = [("換掉回傳值", "return 1", "return 9")]
    first = _run(sandbox, mutants)
    assert not first.reused, first.report()
    before = sandbox["calls"]

    second = mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE], mutants=mutants,
        snapshot_dir=sandbox["snapshot_dir"], resume=True)
    assert second.reused == ["換掉回傳值"], second.report()
    assert second.mutants[0].verdict == first.mutants[0].verdict
    # 只多了基準那一次，變異那一輪被跳過了。
    assert sandbox["calls"] == before + 1, (
        f"沿用了卻還是跑了 child：{sandbox['calls'] - before} 次")


def test_resume_is_off_by_default(sandbox):
    """預設不沿用。沿用要自己開——這是最容易產生假結論的地方。"""
    mutants = [("換掉回傳值", "return 1", "return 9")]
    _run(sandbox, mutants)
    again = _run(sandbox, mutants)
    assert again.reused == [], again.report()


def test_the_report_says_which_rows_were_reused(sandbox):
    """沿用必須看得見。mutmut#104 的教訓是「靜悄悄的快取」，不是「快取」。"""
    mutants = [("換掉回傳值", "return 1", "return 9")]
    _run(sandbox, mutants)
    second = mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE], mutants=mutants,
        snapshot_dir=sandbox["snapshot_dir"], resume=True)
    assert "（沿用）" in second.report(), second.report()


def test_a_changed_project_source_throws_the_whole_journal_away(monkeypatch,
                                                                sandbox):
    """指紋不同就整份作廢——「我改了測試想看它現在抓不抓得到」正是這一格。

    如果指紋只涵蓋被變異的那個檔，這種改動看不見，於是沿用一筆過期的結論而且
    毫無徵兆。那就是 mutmut#104 的形狀。
    """
    mutants = [("換掉回傳值", "return 1", "return 9")]
    _run(sandbox, mutants)
    monkeypatch.setattr(mh, "_project_fingerprint", lambda: "別的樹")
    before = sandbox["calls"]
    second = mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE], mutants=mutants,
        snapshot_dir=sandbox["snapshot_dir"], resume=True)
    assert second.reused == [], second.report()
    assert sandbox["calls"] == before + 2, "指紋變了卻還是沿用了"


def test_a_different_test_selection_throws_the_journal_away(sandbox):
    """換一組測試就是換一個問題，上一次的答案不算數。"""
    mutants = [("換掉回傳值", "return 1", "return 9")]
    _run(sandbox, mutants)
    other = "test/test_x.py::test_other"
    second = mh.run_mutations(
        target="axiomatic/thing.py", tests=[other], mutants=mutants,
        snapshot_dir=sandbox["snapshot_dir"], resume=True)
    assert second.reused == [], second.report()


def test_editing_a_mutant_makes_only_that_row_re_run(sandbox):
    """紀錄的鍵包含變異文字：改了哪一個就重跑哪一個，其他照舊沿用。"""
    first = [("一", "return 1", "return 9"), ("二", "return 2", "return 8")]
    _run(sandbox, first)
    edited = [("一", "return 1", "return 9"), ("二", "return 2", "return 7")]
    second = mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE], mutants=edited,
        snapshot_dir=sandbox["snapshot_dir"], resume=True)
    assert second.reused == ["一"], second.report()


@pytest.mark.parametrize("verdict, tail", [
    (mh.NO_EVIDENCE, (1, "")),
    (mh.TIMEOUT, (None, "")),
])
def test_an_inconclusive_verdict_is_never_reused(sandbox, verdict, tail):
    """`NO-EVIDENCE` 與 `TIMEOUT` 不沿用。

    `NO-EVIDENCE` 的意思**就是**「這一格沒有結論，重跑」——把它快取起來等於把
    「重跑」這個指示本身快取掉。`TIMEOUT` 多半是主機當下的負載，不是這個變異的
    性質。而這兩個剛好也是「上一次被砍掉」最可能留下的值，所以續跑一定會遇到。
    """
    mutants = [("換掉回傳值", "return 1", "return 9")]
    sandbox["results"] = [(0, "1 passed"), tail]
    first = _run(sandbox, mutants)
    assert first.mutants[0].verdict == verdict, first.report()

    before = sandbox["calls"]
    second = mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE], mutants=mutants,
        snapshot_dir=sandbox["snapshot_dir"], resume=True)
    assert second.reused == [], second.report()
    assert sandbox["calls"] == before + 2, "沒有結論的那一格被沿用了"


def test_the_journal_is_written_as_each_mutant_finishes(sandbox):
    """紀錄要邊跑邊寫。收工才寫的話，被砍掉時它就是空的——而「行程活不到最後」
    正是這份紀錄存在的唯一理由。
    """
    seen = []

    def peek(_mutant):
        journal = sandbox["snapshot_dir"] / "thing.py.journal"
        seen.append(len(json.loads(journal.read_text(encoding="utf-8"))["results"])
                    if journal.exists() else 0)

    mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE],
        mutants=[("一", "return 1", "return 9"),
                 ("二", "return 2", "return 8")],
        snapshot_dir=sandbox["snapshot_dir"], progress=peek)
    assert seen == [1, 2], f"紀錄不是邊跑邊寫的：{seen}"


def test_a_corrupt_journal_falls_back_to_running_everything(sandbox):
    """紀錄壞掉要退回「重跑」，不是往上拋。這純粹是最佳化。"""
    mutants = [("換掉回傳值", "return 1", "return 9")]
    _run(sandbox, mutants)
    (sandbox["snapshot_dir"] / "thing.py.journal").write_text(
        "{ 這不是 JSON", encoding="utf-8")
    before = sandbox["calls"]
    second = mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE], mutants=mutants,
        snapshot_dir=sandbox["snapshot_dir"], resume=True)
    assert second.reused == [], second.report()
    # 基準 + 那一個變異都真的跑了。斷言「跑了幾次」而不是「判成什麼」——
    # 判成什麼是替身決定的，那樣的斷言測到的是替身不是被測程式。
    assert sandbox["calls"] == before + 2, "紀錄壞掉之後沒有重跑"


def test_the_fingerprint_moves_when_any_project_file_changes(tmp_path,
                                                             monkeypatch):
    """指紋要涵蓋**整個專案**，不只被變異的那個檔。

    正面對照組：沒有這一支的話，「指紋算對了」跟「指紋是個常數」在上面每一支
    測試裡都一樣綠。
    """
    pkg = tmp_path / "axiomatic"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "b.py").write_text("y = 2\n", encoding="utf-8")
    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "test_c.py").write_text("def test_c():\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(mh, "PACKAGE_ROOT", pkg)
    monkeypatch.setattr(mh, "PROJECT_ROOT", tmp_path)

    before = mh._project_fingerprint()
    assert mh._project_fingerprint() == before, "同一棵樹算兩次要一樣"
    (pkg / "b.py").write_text("y = 3\n", encoding="utf-8")
    assert mh._project_fingerprint() != before, (
        "改了 `b.py` 指紋卻沒動——那表示指紋沒有涵蓋整個專案，"
        "「我改了測試再跑一次」就會沿用一筆過期的結論。")
    # 測試 2026-09-22 起住在 repo 根目錄的 `test/`，不在套件裡——指紋要看得到它們，
    # 否則「改了測試再跑一次」正好是指紋看不見的那種改動。
    middle = mh._project_fingerprint()
    (tests / "test_c.py").write_text("def test_c():\n    assert 1\n",
                                     encoding="utf-8")
    assert mh._project_fingerprint() != middle, (
        "改了 `test/test_c.py` 指紋卻沒動——指紋沒有涵蓋 `test/`。")


# ---------------------------------------------------------------------------
# 「有人正在跑」與「上一輪沒跑完」必須分得開
# ---------------------------------------------------------------------------
# `interrupted_targets()` 回答的是「樹現在是不是髒的」。它**答不了**「髒是因為上一輪
# 被砍掉，還是因為有一輪正跑到一半」——兩者在磁碟上一模一樣。而這個區別決定的下一步
# 完全相反：被中斷要**還原**，正在跑要**不要碰**。
#
# 2026-09-12 為這件事付過代價。一個 subagent 把變異跑在背景、自己的回合先結束了；
# 主 agent 看到查詢回報髒，就照 `conftest.py` 訊息裡寫的補救方式做——複製 `.orig`
# 回去、**刪掉 `.inprogress` 標記**。但那一輪還活著（harness pid ＋ 子行程 pytest）：
#
#   1. 還原落在兩個變異之間，下一輪直接把新的變異寫回去；
#   2. 標記被刪掉之後查詢回 `[]`——**偵測器瞎了，而樹是髒的**。
#
# 第二次的變異更陰險：它**保留**了那一行，只把字典順序對調成
# `{"PYTHONIOENCODING": "utf-8", **os.environ}`——`os.environ` 反而贏，語意等於
# `setdefault`，正好是那個修正要防的缺陷。`grep` 照樣命中，只有逐位元組比對看得出來。


def _mark_with_pid(sandbox, pid, name="axiomatic/thing.py"):
    """兩行格式的標記：第一行目標檔、第二行 pid。"""
    snap_dir = sandbox["snapshot_dir"]
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "thing.py.orig").write_text(SOURCE, encoding="utf-8")
    (snap_dir / "thing.py.inprogress").write_text(
        f"{name}\n{pid}\n", encoding="utf-8")
    return snap_dir


def _certainly_dead_pid() -> int:
    """一個**確定**已經結束的 pid：真的開一個行程再等它結束。

    不可以隨便挑一個大數字當「死的 pid」——那是猜的，而且在 pid 回收之後會變成
    某個無辜行程。開一個自己的來才是量到的。
    """
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


def test_a_marker_written_by_a_live_process_is_a_live_run(sandbox):
    """標記記著的 pid 還活著 → 這是一輪**正在跑**的，不要動樹。"""
    snap_dir = _mark_with_pid(sandbox, os.getpid())
    assert mh.live_mutation_runs(snap_dir) == [("axiomatic/thing.py",
                                                os.getpid())]


def test_a_marker_from_a_dead_process_is_not_a_live_run(sandbox):
    """pid 已經結束 → 這是「上一輪沒跑完」，才輪得到還原。"""
    snap_dir = _mark_with_pid(sandbox, _certainly_dead_pid())
    assert mh.live_mutation_runs(snap_dir) == []


def test_an_old_single_line_marker_is_not_reported_as_live(sandbox):
    """舊格式（只有目標檔、沒有 pid）**維持 2026-09-12 之前的行為**。

    刻意不把「沒有 pid」當成「保守地假設有人在跑」：那會讓每一個舊標記都變成永遠
    不能 heal 的狀態，而它們只是過渡產物。新寫的標記一律有 pid。
    """
    snap_dir = _mark(sandbox)  # 單行
    assert mh.live_mutation_runs(snap_dir) == []
    # 但舊格式仍然要讀得出目標檔——向後相容的重點在這裡。
    sandbox["target"].write_text(SOURCE.replace("return 1", "pass"),
                                 encoding="utf-8")
    assert mh.interrupted_targets(snap_dir) == ["axiomatic/thing.py"]


def test_a_live_run_is_still_reported_as_dirty(sandbox):
    """**兩個查詢不是二選一。** 正在跑的時候樹一樣是髒的。

    `conftest.py` 在這兩種情況下都必須拒絕收集——被中斷會得到一批假紅，而在別人
    改樹的時候讀樹同樣會（那正是本專案記過的另一條：不要在別人改樹的時候讀樹）。
    這一支釘住「加了 `live_mutation_runs` 之後 `interrupted_targets` 沒有被放寬」。
    """
    snap_dir = _mark_with_pid(sandbox, os.getpid())
    sandbox["target"].write_text(SOURCE.replace("return 1", "pass"),
                                 encoding="utf-8")
    assert mh.interrupted_targets(snap_dir) == ["axiomatic/thing.py"]
    assert mh.live_mutation_runs(snap_dir), "同一個狀態應該同時被兩個查詢看到"


def test_the_marker_records_the_pid_of_the_process_that_wrote_it(sandbox):
    """真的跑一輪，確認標記**在跑的當下**就帶著自己的 pid。

    人工擺出來的標記證明不了 `run_mutations` 會寫 pid——那正是 2026-09-12 缺的
    那一半。所以在 `progress` 回呼裡偷看磁碟上的標記（那一刻標記一定還在：它要到
    整輪確認還原成功之後才會被刪掉）。
    """
    seen: list[tuple[str, int | None]] = []

    def _peek(_result):
        marker = sandbox["snapshot_dir"] / "thing.py.inprogress"
        seen.append(mh._marker_fields(marker.read_text(encoding="utf-8")))

    result = mh.run_mutations(
        target="axiomatic/thing.py", tests=[NODE],
        mutants=[("換掉回傳值", "return 1", "return 9")],
        snapshot_dir=sandbox["snapshot_dir"], progress=_peek)
    assert result.restored, result.report()
    assert seen, "progress 回呼沒被叫到，這一支什麼都沒量到"
    target, pid = seen[0]
    assert target == "axiomatic/thing.py"
    assert pid == os.getpid(), (
        f"標記裡的 pid 是 {pid}，應該是寫它的那個行程 {os.getpid()}。"
        "沒有 pid 的話，『有人正在跑』與『上一輪沒跑完』在磁碟上完全一樣。")
    # 跑完了就不該再有任何一輪被算成在跑。
    assert mh.live_mutation_runs(sandbox["snapshot_dir"]) == []


def test_marker_fields_reads_both_formats():
    """格式剖析的合成語料。空的、單行、兩行、pid 不是數字。"""
    assert mh._marker_fields("") == ("", None)
    assert mh._marker_fields("axiomatic/a.py\n") == ("axiomatic/a.py", None)
    assert mh._marker_fields("axiomatic/a.py\n123\n") == ("axiomatic/a.py", 123)
    # 第二行不是數字 → 當作沒有 pid，不要拋。標記是在被砍掉的情境下讀的，
    # 這支查詢自己丟例外等於把「偵測不到」升級成「連測試都跑不起來」。
    assert mh._marker_fields("axiomatic/a.py\nnope\n") == ("axiomatic/a.py", None)


# ---------------------------------------------------------------------------
# 正式批次在跑時，不准變異它重生後會載入的模組
# ---------------------------------------------------------------------------

def test_a_live_batch_blocks_mutating_a_module_it_would_reload(monkeypatch):
    """批次在跑 ＋ 目標在它的匯入閉包裡 → 拒絕。

    這道防線防的是一個很安靜的失效：變異版會在磁碟上待數十秒，而監督者只要在
    這段期間重生子行程（OOM／定期重啟／瀏覽器死掉），那個**全新的 Python 行程
    會從磁碟讀進變異版**，對著真的瀏覽器與真的帳號跑起來。
    """
    monkeypatch.setattr(mh, "_batch_is_running", lambda: True)
    with pytest.raises(mh.LiveBatchConflict) as caught:
        mh._refuse_if_a_live_batch_would_reload(
            "axiomatic/_webrunner_shared.py")
    assert "allow_live_batch" in str(caught.value), (
        "錯誤訊息沒有講出路——被擋住的人要知道怎麼繼續。")


def test_a_module_the_batch_never_loads_is_not_blocked(monkeypatch):
    """**必放行的那一半**，而且它才是決定這道防線會不會被關掉的那一半。

    範圍如果是「批次在跑就什麼都不准變異」，那它會擋掉絕大多數日常工作
    （測試檔、工具、bot 那一側），於是下一個人會直接把它拿掉。所以只擋閉包內。
    """
    monkeypatch.setattr(mh, "_batch_is_running", lambda: True)
    for target in ("axiomatic/discord_rpc.py",
                   "axiomatic/gen_command_docs.py",
                   "test/test_pid_liveness.py"):
        mh._refuse_if_a_live_batch_would_reload(target)   # 不該丟


def test_no_live_batch_means_no_restriction(monkeypatch):
    """沒有批次在跑就完全不擋——這是平常的狀態。"""
    monkeypatch.setattr(mh, "_batch_is_running", lambda: False)
    assert mh.batch_modules_at_risk() == set()
    mh._refuse_if_a_live_batch_would_reload("axiomatic/_webrunner_shared.py")


def test_the_import_closure_follows_transitive_imports():
    """閉包要跟著**間接**匯入走，不是只看第一層。

    `webrunner_novelai` 直接 import `_webrunner_shared`，而後者再往下帶出別的
    模組；只看第一層的話，那些間接依賴會落在防線外——而它們一樣會被重生的行程
    載入。這裡順便釘住「只認本套件內真的存在的模組」（stdlib 不該混進來）。
    """
    closure = mh._import_closure("webrunner_novelai")
    assert "webrunner_novelai" in closure, "閉包要含自己"
    assert "_webrunner_shared" in closure, "第一層都沒進來"
    assert "_chrome_slot" in closure, (
        "間接依賴沒有被跟到——只看第一層的閉包擋不住真正的風險面")
    # **兩種 import 形式都要蓋到。** `_chrome_slot` 是 `import _chrome_slot`
    # 進來的，`_batch_config` 只有 `from _batch_config import ...` 這條路——
    # 只斷言前者的話，「把 ImportFrom 那條分支關掉」的變異會存活（實測
    # 2026-09-12 SURVIVED），而那會讓三個模組（`_batch_config`、`_supervisor`、
    # `_warn_dedup`）整個落在防線外。
    assert "_batch_config" in closure, (
        "`from X import Y` 形式的匯入沒有被跟到——這條路上有三個模組，"
        "它們會被重生的行程載入，卻不在風險面裡。")
    for stdlib in ("os", "sys", "json", "pathlib", "time"):
        assert stdlib not in closure, f"stdlib `{stdlib}` 不該出現在閉包裡"
    assert "discord_bot" not in closure, (
        "bot 跑進產圖批次的閉包了——那代表模組邊界破了（CLAUDE.md 的核心不變量）")


def test_the_risk_set_matches_the_batch_component_map():
    """同一條判準有兩份實作，這支把它們放在同一個語料上比。

    `_process_control.STALE_COMPONENTS["batch"]` 列的是產圖批次會載入的檔案，而它
    由 `test_process_control.test_the_component_map_matches_the_real_import_closure`
    對著一份**認得 `from axiomatic.<模組> import …`** 的閉包對帳。這裡的
    `_import_closure` 是另一份實作；2026-09-20 以前它對帶點的 `module` 只取第一段
    （套件名），於是 `_supervisor` 裡那行延後匯入整個看不到，`_process_control`
    明明在批次閉包裡、對它跑變異卻沒有被擋。兩份各自的測試都是綠的——沒有東西在
    比對它們。兩個方向都要：少算＝防線漏洞，多算＝擋掉不該擋的日常工作。
    """
    import _process_control as pc   # noqa: PLC0415

    script, deps = pc.STALE_COMPONENTS["batch"]
    expected = {Path(d).stem for d in deps}
    got = mh._import_closure(Path(script).stem)
    assert len(expected) > 5, f"對照表只列了 {sorted(expected)}，前提壞了"
    assert got == expected, (
        f"變異防線少算：{sorted(expected - got)}；多算：{sorted(got - expected)}")


def test_the_import_closure_reads_every_segment_of_a_dotted_import(
        tmp_path, monkeypatch):
    """正面對照：只用帶點寫法才搆得到的模組，也要進閉包。

    合成一個只有三個檔案的套件，`b` 只能經由 `from axiomatic.b import g`（函式裡的
    延後匯入，跟 `_supervisor` 那行同一個形狀）、`c` 只能經由 `import axiomatic.c`
    到達。真實語料上這個形狀目前只有一處，靠它當唯一的證據太薄。
    """
    pkg = tmp_path / "axiomatic"
    pkg.mkdir()
    (pkg / "a.py").write_text(
        "import os\n"
        "import axiomatic.c\n"
        "def f():\n"
        "    from axiomatic.b import g\n"
        "    return g\n", encoding="utf-8")
    (pkg / "b.py").write_text("g = 1\n", encoding="utf-8")
    (pkg / "c.py").write_text("h = 2\n", encoding="utf-8")
    (pkg / "unrelated.py").write_text("x = 3\n", encoding="utf-8")
    monkeypatch.setattr(mh, "PROJECT_ROOT", tmp_path)
    assert mh._import_closure("a") == {"a", "b", "c"}


def test_a_missing_or_broken_pid_file_means_no_batch(tmp_path, monkeypatch):
    """讀不到 pid 檔就當作沒有批次；判不出來就當作有。

    兩個方向刻意不同，理由跟 `_pid_alive` 那三份副本一樣是「這個錯該往哪邊倒」：
    沒有 pid 檔是**正常**狀態（平常就沒有批次），當成有的話這道防線會天天擋人、
    然後被關掉；而 psutil 拿不到時是**判不出來**，那時候要保守。
    """
    monkeypatch.setattr(mh, "PROJECT_ROOT", tmp_path)
    assert mh._batch_is_running() is False, "沒有 pid 檔卻說有批次在跑"
    (tmp_path / "webrunner.pid").write_text("not-a-number", encoding="utf-8")
    assert mh._batch_is_running() is False, "壞掉的 pid 檔卻說有批次在跑"


def test_the_guard_is_wired_into_run_mutations():
    """`run_mutations` 真的會呼叫這道防線，而且預設是開的。

    防線寫好卻沒有接上，是本專案記過的形狀（一份沒有作用的清單讀起來像有人在管）。
    用 AST 判：`allow_live_batch` 的預設必須是 False，而且函式本體要呼叫到它。
    """
    src = Path(mh.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src, mh.__file__)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "run_mutations")
    called = [ast.unparse(c.func) for c in ast.walk(fn)
              if isinstance(c, ast.Call)]
    assert "_refuse_if_a_live_batch_would_reload" in called, (
        "`run_mutations` 沒有呼叫這道防線——它等於不存在。")
    names = [a.arg for a in fn.args.kwonlyargs]
    assert "allow_live_batch" in names, "少了那個逃生口參數"
    default = fn.args.kw_defaults[names.index("allow_live_batch")]
    assert isinstance(default, ast.Constant) and default.value is False, (
        "`allow_live_batch` 的預設不是 False——防線預設就關著等於沒有防線。")


def test_a_pid_file_naming_a_live_process_means_a_batch_is_running(
        tmp_path, monkeypatch):
    """正面方向也要釘：活著的 pid 要讀成「有批次在跑」。

    只測「沒有 pid 檔 → False」的話，一個永遠回 False 的實作會全綠——而那正好
    是把整道防線關掉的那種寫法，症狀是沒有症狀。這裡用**本行程自己的 pid**，
    那是這台機器上唯一保證活著的 pid。
    """
    monkeypatch.setattr(mh, "PROJECT_ROOT", tmp_path)
    (tmp_path / "webrunner.pid").write_text(str(os.getpid()), encoding="utf-8")
    assert mh._batch_is_running() is True, (
        "pid 檔指著一個活著的行程，卻讀成沒有批次在跑——防線等於關著。")


def test_a_pid_file_naming_a_dead_process_means_no_batch(tmp_path, monkeypatch):
    """留在磁碟上的**過期** pid 檔不該永遠擋住所有人。

    批次被砍掉時 pid 檔可能留著；把它讀成「還在跑」的話，這道防線會從此擋住每
    一個人，而修法看起來像「把防線拿掉」。用 `_certainly_dead_pid()`（真的生一個
    子行程再等它結束）拿一個保證已死的 pid。
    """
    monkeypatch.setattr(mh, "PROJECT_ROOT", tmp_path)
    (tmp_path / "webrunner.pid").write_text(
        str(_certainly_dead_pid()), encoding="utf-8")
    assert mh._batch_is_running() is False, (
        "過期的 pid 檔被讀成還在跑——這道防線會永遠擋住所有人。")


def test_an_unavailable_liveness_probe_is_treated_as_a_running_batch(
        tmp_path, monkeypatch):
    """探測不到就當作「有批次在跑」——這個方向是刻意的。

    `_batch_is_running` 要靠 `_chrome_slot._pid_alive`。拿不到它的時候有兩種倒法，
    而這道防線必須倒向**保守**：判不出來就假設批次在跑、拒絕變異。倒向樂觀的話，
    正好在「我們對機器狀態一無所知」的時候把防線關掉。

    模擬缺席用 `sys.modules[...] = None`（而不是 `pop`）——本專案為此付過代價：
    `pop` 只是讓下一次 import 去**真的**載入它，等於什麼都沒模擬到。設成 `None`
    會讓 import 丟 `ImportError`，那才是「這台機器上沒有這個東西」。
    """
    monkeypatch.setattr(mh, "PROJECT_ROOT", tmp_path)
    (tmp_path / "webrunner.pid").write_text(
        str(_certainly_dead_pid()), encoding="utf-8")
    # 先確認這個 pid 在正常情況下會被判成「沒在跑」，否則下面的斷言證明不了
    # 是**探測缺席**造成的差別（一個永遠回 True 的實作也會讓它過）。
    assert mh._batch_is_running() is False

    monkeypatch.setitem(sys.modules, "_chrome_slot", None)
    assert mh._batch_is_running() is True, (
        "拿不到存活探測時倒向了樂觀——這道防線正好會在我們對機器狀態一無所知"
        "的時候關掉。")
