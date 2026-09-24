"""Unit tests for the bot-side pure helpers + queue-preview mirrors.

The bot deliberately re-implements the webrunner's pairing logic to *preview*
a run (CLAUDE.md module-boundary rule: no bot↔webrunner import), so the two
can drift. These tests lock the mirror against the webrunner's own pure
pairing (`_queue_consume.pair_todos`) and cover the helpers added for the
scheduling / ETA / disk / presence work.

Run with pytest (several tests use the `monkeypatch` fixture):
    py -3 -m pytest test/test_bot_helpers.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
import math
import os
import random
import sys
import tempfile
import time
import types
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import _queue_consume  # noqa: E402
import _webrunner_shared as ws  # noqa: E402
import discord  # noqa: E402
import pytest  # noqa: E402

import _help_strings as _help  # noqa: E402
import discord_bot as b  # noqa: E402
import dorossi_backend as db  # noqa: E402
import presence_probe as pp  # noqa: E402


@pytest.fixture(autouse=True)
def _never_touch_live_bot_state(monkeypatch, tmp_path):
    """把 bot 會**寫到磁碟**的 repo-root 狀態檔導向 tmp，套用到本檔每一個測試。

    為什麼是 autouse 而不是各測試自己處理：斜線指令的閘門測試會呼叫真正的
    `_tree_check`，而那道閘除了判權限還會**寫稽核紀錄**。2026-08-23 實測發現
    `audit.ndjson` 裡 5,287 筆有 5,281 筆是測試留下的假紀錄（`user_name` 是
    `namespace(id=…)`、`channel_id` 是設定檔裡那個），真正的使用者指令只有 6 筆
    ——`/sys audit` 因此形同失效，而且**差點讓人把「設定的頻道有 5,281 筆流量」
    誤讀成那個頻道還活著**。一次 pytest 就多 110 筆。

    新增測試不必記得自己隔離，這一條自動蓋到。要再擋別的檔案就加在這裡。"""
    monkeypatch.setattr(b, "AUDIT_FILE", tmp_path / "audit.ndjson")


# ---------------------------------------------------------------------------
# tiny harness (mirrors test_dynamic_consume.py's style)
# ---------------------------------------------------------------------------
_PASSED = 0


def _unregistered_note() -> str:
    """standalone runner 沒跑到幾支？回一句給結尾訊息用（永不 raise）。

    差額幾乎全是要吃 pytest fixture 的測試，叫不動是應該的；會出問題的是**沒有講**。
    """
    try:
        import ast as _ast

        declared = sum(
            1 for node in _ast.parse(
                Path(__file__).read_text(encoding="utf-8")).body
            if isinstance(node, _ast.FunctionDef)
            and node.name.startswith("test_"))
    except Exception:  # pylint: disable=broad-except
        return ""
    skipped = declared - _PASSED
    if skipped <= 0:
        return ""
    return (f"（另有 {skipped} 支需要 pytest fixture，"
            f"standalone 跑不到；完整結果請跑 pytest）")


def _group(name, fn):
    global _PASSED
    print(f"{name}:")
    fn()
    print("  PASS\n")
    _PASSED += 1


def _eq(actual, expected, label=""):
    assert actual == expected, f"{label}: got {actual!r}, want {expected!r}"
    print(f"  OK {label} -> {actual!r}")


def _elapsed_eq(actual, expected, label="", slack=5.0):
    """時間差**不能**拿去做逐一相等比較，這裡比的是一個單邊的窗。

    `_parse_run_schedule` 自己讀一次 `time.time()`，測試在呼叫它**之前**先讀了一次，
    兩次之間的任何停頓都會整個進到差值裡。原本寫的是 `round(r - now) == 5400`，
    而 `round` 只要撐過半秒就翻面——2026-09-20 實測紅過一次：整套在覆蓋率＋`-n 6`
    底下跑，這一支報 5401，而它單獨跑一萬次都不會錯。那不是缺陷，是這個比較法自己
    的下限，**而且紅落在改了別的東西的人身上**（`CLAUDE.md` 講行數棘輪時的同一個
    形狀）。這台機器上更不能這樣寫：它同時在跑好幾天不中斷的批次。

    窗是**單邊**的，這點才是重點：函式讀時鐘一定比測試晚，所以差值只會偏大，不會
    偏小。寫成 `abs(actual - expected) <= slack` 會把「少算了幾秒」也一起放行，而那
    才是真的缺陷會長出來的方向。5400 對 5401 放行、5400 對 5399 照樣紅。
    """
    assert expected <= actual < expected + slack, (
        f"{label}: got {actual!r}, want [{expected}, {expected + slack})")
    print(f"  OK {label} -> {actual!r}")


# ---------------------------------------------------------------------------
def test_parse_run_schedule():
    now = time.time()

    def delta(arg):
        r = b._parse_run_schedule(arg)
        return None if r is None else r - now

    _elapsed_eq(delta("in 90m"), 5400, "in 90m")
    _elapsed_eq(delta("in 2h"), 7200, "in 2h")
    _elapsed_eq(delta("in 30s"), 30, "in 30s")
    _elapsed_eq(delta("in 5"), 300, "bare number = minutes")
    # whitespace tolerance
    _elapsed_eq(delta("in  90 m"), 5400, "in  90 m")
    # invalid forms
    for bad in ("soon", "", "in", "in -3m", "in 0", "at 99:99",
                "at 12", "at xx:yy", "cancel"):
        _eq(b._parse_run_schedule(bad), None, f"invalid {bad!r}")
    # 'at HH:MM' lands on the right wall-clock minute, in the future
    r = b._parse_run_schedule("at 23:59")
    assert r is not None and r > now, "at 23:59 should be a future epoch"
    lt = time.localtime(r)
    _eq((lt.tm_hour, lt.tm_min), (23, 59), "at 23:59 wall-clock")


def _local_epoch(day: int, hour: int, minute: int, second: int = 0) -> float:
    return time.mktime((2026, 9, day, hour, minute, second, 0, 0, -1))


@pytest.mark.parametrize("asker_is_owner", [False, True], ids=["member", "owner"])
def test_the_delete_reaction_names_files_only_to_the_owner(monkeypatch, tmp_path, asker_is_owner):
    """🗑️ 的回覆原本把被刪的檔名、錯誤型別直接貼進頻道，給任何按了反應的人看；⭐ 那條旁邊
    就寫著「檔名是主機上的命名慣例，不外流」。擁有者照舊拿到檔名（身分閘）。"""
    folder = tmp_path / "some character"
    folder.mkdir()
    kept = folder / "some character_0001_20260923_010203.png"
    kept.write_bytes(b"png")
    gone = folder / "some character_0002_20260923_010204.png"
    monkeypatch.setattr(b, "FAVORITES_FILE", tmp_path / "favorites.json")
    monkeypatch.setattr(b, "RECENT_IMAGE_MSGS_FILE", tmp_path / "recent_image_msgs.json")

    def _no_save(_favs):
        raise AssertionError("這兩張都不在收藏裡，不該寫收藏檔")

    monkeypatch.setattr(b, "_save_favorites", _no_save)
    monkeypatch.setitem(b._RECENT_IMAGE_MSGS, 555, [kept, gone])
    sent: list[str] = []

    class _Channel:
        async def send(self, text):
            sent.append(text)

    monkeypatch.setattr(b.client, "get_channel", lambda _cid: _Channel())
    uid = b.OWNER_USER_ID if asker_is_owner else b.OWNER_USER_ID + 1
    payload = types.SimpleNamespace(message_id=555, channel_id=b.CHANNEL_ID, user_id=uid)
    asyncio.run(b._react_delete(payload, [kept, gone]))

    assert not kept.exists()
    assert len(sent) == 1, sent
    assert "刪除 1 個檔案" in sent[0], sent
    assert (kept.name in sent[0]) is asker_is_owner, sent
    assert (gone.name in sent[0]) is asker_is_owner, sent
    assert "some character" not in sent[0] or asker_is_owner, sent


class _FakeAncestor:
    """`psutil.Process` 的替身：只有 `pid`、`name()`、`cmdline()`、`parents()`。"""

    def __init__(self, pid, cmdline, parents=(), error=None, name="python.exe"):
        self.pid = pid
        self._cmdline = cmdline
        self._parents = parents
        self._error = error
        self._name = name

    def name(self):
        if self._error is not None:
            raise self._error
        return self._name

    def cmdline(self):
        if self._error is not None:
            raise self._error
        return self._cmdline

    def parents(self):
        if isinstance(self._parents, Exception):
            raise self._parents
        return list(self._parents)


_LAUNCHER_CMDLINE = [r"D:\repo\.venv\Scripts\python.exe", r"D:\repo\start_discord_bot.py"]


def _chain(*ancestors):
    return _FakeAncestor(1, ["python.exe", "discord_bot.py"], parents=ancestors)


def test_the_launcher_is_found_among_the_ancestors():
    """實測（2026-09-23）：啟動器是 bot 的**祖父**——中間隔著 `.venv` 的轉接殼——再往上是
    工作排程器的系統行程，讀它們的命令列會被拒絕。"""
    import psutil
    me = _chain(
        _FakeAncestor(2, [r"D:\repo\.venv\Scripts\python.exe", "-u", "discord_bot.py"]),
        _FakeAncestor(3, _LAUNCHER_CMDLINE),
        _FakeAncestor(4, [], error=psutil.AccessDenied(4)),
    )
    assert b._find_bot_supervisor(me) == (True, 3)


def test_no_launcher_among_the_ancestors_is_a_definite_no():
    """從編輯器直接執行：往上只有編輯器與系統行程。系統行程讀不到命令列不是「不知道」——
    它不會是我們的啟動器——所以照樣判定為沒有監督者。"""
    import psutil
    me = _chain(
        _FakeAncestor(2, ["code.exe", r"D:\repo\start_discord_bot.py"], name="code.exe"),
        _FakeAncestor(3, [], error=psutil.AccessDenied(3)),
        _FakeAncestor(4, [], error=psutil.NoSuchProcess(4)),
    )
    assert b._find_bot_supervisor(me) == (True, None)


def test_an_ancestry_that_cannot_be_read_is_undetermined():
    import psutil
    me = _FakeAncestor(1, [], parents=psutil.AccessDenied(1))
    assert b._find_bot_supervisor(me) == (False, None)


@pytest.mark.parametrize("found, closes", [
    ((True, 30924), True), ((True, None), False), ((False, None), False),
], ids=["supervised", "unsupervised", "undetermined"])
def test_git_pull_auto_restarts_only_when_something_will_bring_the_bot_back(
        monkeypatch, found, closes):
    """`/sys git_pull` 拉成功之後會自動重啟，走的是跟 `/sys restart` 同一個「結束自己等重生」。
    沒有監督者時 pull 照樣算數，只是不自動重啟。`_git` 整個換掉：這台機器上真的 pull 會動到
    正在跑的 repo，所以任何沒預期到的 git 指令一律丟例外。"""
    answers = {
        tuple(b._git_pull_status_args()): (0, "", ""),
        ("pull", "--ff-only", "origin", "main"): (0, "Updating abc..def\nFast-forward\n", ""),
        ("log", "--format=%h", "-n", "10", "ORIG_HEAD..HEAD"): (0, "def1234\n", ""),
    }

    def _fake_git(args, timeout=15.0):
        return answers[tuple(args)]

    closed: list[bool] = []

    async def _close():
        closed.append(True)

    async def _no_sleep(_seconds):
        return None

    sent: list[str] = []

    async def _reply(_message, text=None, **_kw):
        sent.append(text)

    monkeypatch.setattr(b, "_git", _fake_git)
    monkeypatch.setattr(b.client, "close", _close)
    monkeypatch.setattr(b.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_find_bot_supervisor", lambda: found)
    asyncio.run(b.cmd_git_pull(types.SimpleNamespace(), ""))
    assert closed == ([True] if closes else []), closed
    assert len(sent) == 1 and "def1234" in sent[0], sent
    assert ("不重啟" in sent[0]) is (not closes), sent


_LEAKY_GIT_ERR = ("fatal: unable to access 'https://user:ghp_notarealtoken@example.invalid/"
                  "repo.git/': Could not resolve host\n")


@pytest.mark.parametrize("status, pull, expected", [
    ((128, "", _LEAKY_GIT_ERR), None, "git status 失敗（rc=128）"),
    ((0, "", ""), (1, "", _LEAKY_GIT_ERR), "失敗（rc=1）"),
    ((0, "", ""), (0, "Already up to date.\n", ""), "已是最新"),
], ids=["status-failed", "pull-failed", "up-to-date"])
def test_a_git_pull_that_brought_nothing_never_restarts(monkeypatch, status, pull, expected):
    """三條提早離開的路都**不得**重啟：沒拉到東西就重啟，是讓 bot 無故斷線一次；拉失敗還
    重啟，更可能讓一個半套的工作樹被載入。

    git 的錯誤輸出只進 stderr：它常常帶著遠端網址，而網址裡可能就是憑證（這裡刻意放一個
    假的）。`_git` 換成只認得預期那幾個指令的替身，任何其他 git 指令都會丟例外。"""
    answers = {tuple(b._git_pull_status_args()): status}
    if pull is not None:
        answers[("pull", "--ff-only", "origin", "main")] = pull

    def _fake_git(args, timeout=15.0):
        return answers[tuple(args)]

    closed: list = []
    sent: list = []

    async def _close():
        closed.append(True)

    async def _reply(_message, text=None, **_kw):
        sent.append(text)

    monkeypatch.setattr(b, "_git", _fake_git)
    monkeypatch.setattr(b.client, "close", _close)
    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_find_bot_supervisor", lambda: (True, 30924))
    asyncio.run(b.cmd_git_pull(types.SimpleNamespace(), ""))
    assert closed == []
    assert len(sent) == 1 and expected in sent[0], sent
    assert "ghp_" not in sent[0] and "example.invalid" not in sent[0], sent


def test_the_git_pull_dirty_check_ignores_only_the_queue_files(tmp_path, monkeypatch):
    """在一個真的 git 倉庫裡量 `_git_pull_status_args()`：批次一直在改的佇列檔不算改動，
    其他檔案照算。用真的 git 是因為 pathspec 的排除語法寫錯時 git 不會報錯，只會什麼都不排除
    （或什麼都排除），字串比對的測試看不出來。"""
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    names = ["todo_prompt.md", "todo_character1.md", "todo_character2.md",
             "todo_undesired.md", "notes.md"]
    for name in names:
        (tmp_path / name).write_text("a\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace").stdout

    git("init", "-q")
    git("add", *names)
    git("commit", "-q", "-m", "x")
    monkeypatch.setattr(b, "PROJECT_ROOT", tmp_path)
    for attr, name in (("TODO_PROMPT_FILE", "todo_prompt.md"), ("TODO_FILE_1", "todo_character1.md"),
                       ("TODO_FILE_2", "todo_character2.md"),
                       ("TODO_UNDESIRED_FILE", "todo_undesired.md")):
        monkeypatch.setattr(b, attr, tmp_path / name)
    for name in names[:4]:
        (tmp_path / name).write_text("b\n", encoding="utf-8")
    assert git(*b._git_pull_status_args()) == ""
    (tmp_path / "notes.md").write_text("b\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("x = 1\n", encoding="utf-8")
    dirty = sorted(line[3:] for line in git(*b._git_pull_status_args()).splitlines())
    assert dirty == ["new.py", "notes.md"], dirty


def test_a_dirty_tree_refusal_does_not_suggest_discarding_changes(monkeypatch):
    """拒絕時原本建議「先 `git checkout --`」——在這台機器上有改動的幾乎都是佇列檔，照做
    就把佇列丟掉了。現在只報數量、請使用者自己處理，不給會丟資料的指令。"""
    def _fake_git(args, timeout=15.0):
        assert tuple(args) == tuple(b._git_pull_status_args()), args
        return 0, " M notes.md\n?? new.py\n", ""

    sent: list[str] = []

    async def _reply(_message, text=None, **_kw):
        sent.append(text)

    monkeypatch.setattr(b, "_git", _fake_git)
    monkeypatch.setattr(b, "safe_reply", _reply)
    asyncio.run(b.cmd_git_pull(types.SimpleNamespace(), ""))
    assert len(sent) == 1 and "**2**" in sent[0], sent
    assert "checkout" not in sent[0] and "stash" not in sent[0], sent
    assert "notes.md" not in sent[0], sent


@pytest.mark.parametrize("found, closes, wording", [
    ((True, 30924), True, "restarting"),
    ((True, None), False, "沒有監督者"),
    ((False, None), False, "無法確認"),
], ids=["supervised", "unsupervised", "undetermined"])
def test_restart_only_exits_when_something_will_bring_the_bot_back(
        monkeypatch, found, closes, wording):
    """`/sys restart` 的做法是結束自己、等啟動器重生。沒有啟動器時（例如從編輯器直接執行），
    它只會把 bot 關掉——2026-09-22 擁有者就這樣把 bot 關掉了。判定不了時也不結束：一次沒執行的
    重啟可以再下一次，一個消失的 bot 在對話平台上叫不回來。"""
    closed: list[bool] = []

    async def _close():
        closed.append(True)

    async def _no_sleep(_seconds):
        return None

    sent: list[str] = []

    async def _reply(_message, text=None, **_kw):
        sent.append(text)

    monkeypatch.setattr(b.client, "close", _close)
    monkeypatch.setattr(b.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_find_bot_supervisor", lambda: found)
    asyncio.run(b.mcmd_restart(types.SimpleNamespace()))
    assert closed == ([True] if closes else []), closed
    assert len(sent) == 1 and wording in sent[0], sent


@pytest.mark.parametrize("now, spec, expected", [
    (_local_epoch(23, 15, 0), "at 09:30", _local_epoch(24, 9, 30)),
    (_local_epoch(23, 15, 0), "at 15:00", _local_epoch(24, 15, 0)),
    (_local_epoch(23, 15, 0), "at 15:01", _local_epoch(23, 15, 1)),
    (_local_epoch(23, 23, 59, 30), "at 00:00", _local_epoch(24, 0, 0)),
], ids=["earlier-today", "exactly-now", "one-minute-ahead", "midnight"])
def test_run_at_a_time_already_past_today_means_tomorrow(monkeypatch, now, spec, expected):
    """`/run at HH:MM` 的「今天已經過了就排到明天」在整個套件裡從來沒跑過（2026-09-22
    分支覆蓋率）：上面那支只用 `at 23:59`，除非剛好在最後一分鐘跑，否則永遠是今天。
    拿掉它的話，一個已經過了的時間會變成**過去的**時間點，排程迴圈立刻啟動批次，而使用者
    以為排的是明天。剛好等於現在也算已經過了（`<=`）。"""
    monkeypatch.setattr(b.time, "time", lambda: now)
    assert b._parse_run_schedule(spec) == expected


def test_the_elapsed_window_is_one_sided():
    """上面那個窗是**放寬**步驟，所以殺得掉它的只有 must-allow 樣本。

    只餵「該紅的」永遠驗不出窗被刪掉（退回逐一相等比較）——那個版本對 5400 照樣綠。
    所以三個方向都要釘：偏大一點點要放行（那是機器忙，不是缺陷）、**偏小**一定要紅
    （函式不可能比測試更早讀到時鐘，少算就是真的算錯）、差一個量級一定要紅。
    """
    _elapsed_eq(5401.0, 5400, "慢了一秒才讀到時鐘")   # must-allow
    for bad, why in ((5399.0, "偏小＝真的少算了"),
                     (5406.0, "超出窗外"),
                     (7200.0, "差一個量級")):
        with pytest.raises(AssertionError):
            _elapsed_eq(bad, 5400, why)


def test_parse_schedule_when():
    """`!schedule` 的四種時間格式。"""
    # 2026-08-15 (Sat) 10:00 local
    now = time.mktime((2026, 8, 15, 10, 0, 0, 0, 0, -1))

    _eq(b._parse_schedule_when("09:30"), ("at", "09:30"), "daily HH:MM")
    _eq(b._parse_schedule_when("9:05"), ("at", "09:05"), "zero-padded")
    _eq(b._parse_schedule_when("every 30m"), ("every", "1800"), "every 30m")
    _eq(b._parse_schedule_when("every 2h"), ("every", "7200"), "every 2h")

    # weekdays: names, ranges, lists, groups — all collapse to tm_wday numbers
    _eq(b._parse_schedule_when("mon-fri 09:30"),
        ("weekly", "0,1,2,3,4|09:30"), "mon-fri")
    _eq(b._parse_schedule_when("sat,sun 23:00"),
        ("weekly", "5,6|23:00"), "sat,sun")
    _eq(b._parse_schedule_when("weekends 08:00"),
        ("weekly", "5,6|08:00"), "weekends group")
    # a range that wraps past Sunday still walks the calendar in order
    _eq(b._parse_schedule_when("fri-mon 07:00"),
        ("weekly", "0,4,5,6|07:00"), "fri-mon wraps")

    # one-shot stores an ABSOLUTE epoch: comparing wall-clock time would make
    # it fire again at the same minute the next day.
    kind, value = b._parse_schedule_when("once at 21:30", now)
    _eq(kind, "once", "once kind")
    _eq(time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value))),
        "2026-08-15 21:30", "once later today")
    # a time that already passed today means tomorrow, not "immediately"
    _, value = b._parse_schedule_when("once 08:00", now)
    _eq(time.strftime("%Y-%m-%d %H:%M", time.localtime(float(value))),
        "2026-08-16 08:00", "once rolls to tomorrow")

    for bad in ("", "soon", "25:00", "every 10s", "every 0m", "every 30d",
                "once at 99:99", "notaday 09:00"):
        try:
            b._parse_schedule_when(bad)
            raise AssertionError(f"{bad!r} should be rejected")
        except b._GuiError:
            print(f"  OK rejected {bad!r}")


@pytest.mark.parametrize("when, expected", [
    ("mon,,wed 09:00", ("weekly", "0,2|09:00")),     # 多打一個逗號
    ("mon, 09:00", ("weekly", "0|09:00")),            # 結尾留一個逗號
    ("mon、wed 09:00", ("weekly", "0,2|09:00")),      # 中文頓號
])
def test_a_stray_separator_in_the_weekday_list_is_ignored(when, expected):
    assert b._parse_schedule_when(when) == expected


@pytest.mark.parametrize("when", ["mon-xyz 09:00", "xyz-fri 09:00", "-fri 09:00"])
def test_a_range_with_an_unknown_end_is_refused_as_a_format_error(when):
    """範圍的某一端不認得時要回「看不懂時間格式」。不擋的話更糟：結尾不認得時
    `while index != end` 永遠等不到 `None`，**無窮迴圈卡死整個事件迴圈**（bot 不再回應任何人）；
    開頭不認得時 `None + 1` 丟 `TypeError`，那不是 `_GuiError`，使用者只拿到泛用的失敗訊息。"""
    with pytest.raises(b._GuiError):
        b._parse_schedule_when(when)


def test_schedule_due_catches_up_after_a_missed_minute():
    """漏跑一天是靜默失效，比一開始就沒有這個功能更糟。"""
    day = (2026, 8, 15, 0, 0, 0, 0, 0, -1)

    def at(hour, minute):
        return time.mktime((*day[:3], hour, minute, 0, 0, 0, -1))

    entry = {"id": 1, "when_kind": "at", "when_value": "09:00",
             "last_run": 0.0, "last_date": ""}
    _eq(b._schedule_due(entry, at(8, 59)), False, "before the time -> no")
    _eq(b._schedule_due(entry, at(9, 0)), True, "on the minute -> yes")
    # the whole point: the 30s tick missed 09:00, so 09:04 must still fire
    _eq(b._schedule_due(entry, at(9, 4)), True, "missed the minute -> catch up")
    # but not arbitrarily late — a night job must not wake up next morning
    late = b.SCHEDULE_CATCHUP_MAX_SEC / 3600 + 1
    _eq(b._schedule_due(entry, at(9, 0) + late * 3600), False,
        "past the catch-up window -> no")
    # already ran today -> not again
    ran = dict(entry, last_date="2026-08-15")
    _eq(b._schedule_due(ran, at(9, 30)), False, "same day -> once only")
    _eq(b._schedule_due(dict(entry, last_date="2026-08-14"), at(9, 30)), True,
        "yesterday's marker does not block today")


def test_schedule_due_weekly_and_once():
    sat = time.mktime((2026, 8, 15, 9, 30, 0, 0, 0, -1))   # Saturday
    mon = time.mktime((2026, 8, 17, 9, 30, 0, 0, 0, -1))   # Monday
    weekly = {"id": 2, "when_kind": "weekly", "when_value": "0,1,2,3,4|09:00",
              "last_run": 0.0, "last_date": ""}
    _eq(b._schedule_due(weekly, sat), False, "mon-fri does not fire on Sat")
    _eq(b._schedule_due(weekly, mon), True, "mon-fri fires on Mon")

    once = {"id": 3, "when_kind": "once", "when_value": f"{sat:.0f}",
            "last_run": 0.0, "last_date": ""}
    _eq(b._schedule_due(once, sat - 60), False, "one-shot before its moment")
    _eq(b._schedule_due(once, sat + 60), True, "one-shot at its moment")
    _eq(b._schedule_due(dict(once, last_run=sat), sat + 60), False,
        "one-shot never fires twice")
    _eq(b._schedule_expired(once, sat + b.SCHEDULE_CATCHUP_MAX_SEC + 60), True,
        "stale one-shot is dropped, not left lying around")
    _eq(b._schedule_expired(once, sat + 60), False, "still inside the window")

    # a corrupt entry must not take the whole tick down with it
    _eq(b._schedule_due({"id": 4, "when_kind": "at", "when_value": "oops"}, sat),
        False, "corrupt entry -> not due")


def test_schedule_when_text_is_human_readable():
    _eq(b._schedule_when_text({"when_kind": "at", "when_value": "09:30"}),
        "每天 09:30", "daily")
    _eq(b._schedule_when_text({"when_kind": "every", "when_value": "1800"}),
        "每 30 分鐘", "every minutes")
    _eq(b._schedule_when_text({"when_kind": "every", "when_value": "7200"}),
        "每 2 小時", "every hours")
    _eq(b._schedule_when_text(
        {"when_kind": "weekly", "when_value": "0,1,2,3,4|09:30"}),
        "週一、週二、週三、週四、週五 09:30", "weekly")


# ---------------------------------------------------------------------------
# 排程檔的並行寫入（`schedules.json` 有兩個 read-modify-write 寫入者）
# ---------------------------------------------------------------------------
def _schedule_test_message(monkeypatch, channel_id=4242, author_id=7):
    """一個剛好餵得飽 `cmd_schedule` 的假訊息（不碰真的聊天平台）。"""
    monkeypatch.setattr(b, "_is_owner", lambda _m: True)
    sent: list[str] = []

    async def _fake_reply(_message, content=None, **_kw):
        sent.append(str(content))
        return None

    monkeypatch.setattr(b, "safe_reply", _fake_reply)
    message = types.SimpleNamespace(
        channel=types.SimpleNamespace(id=channel_id),
        author=types.SimpleNamespace(id=author_id))
    return message, sent


@pytest.mark.parametrize("payload, expected", [
    ("add sometime sh echo hi", "看不懂時間格式"),
    ("add 25:99 sh echo hi", "看不懂時間格式"),
    ("add 09:30 del C:\\x", "用法"),
    ("add 09:30 sh", "用法"),
    ("add 09:30", "看不懂時間格式"),
], ids=["no-time", "bad-time", "unknown-kind", "no-command", "nothing"])
def test_a_malformed_schedule_is_refused_before_anything_is_saved(
        monkeypatch, tmp_path, payload, expected):
    """排程是「之後在沒人看著時自己跑」的東西，所以壞的要在建立的這一刻擋掉：時間看不懂、
    種類不是 `macro`／`sh`、沒給要跑什麼。存進去的話，它會在某個沒人預期的時刻失敗
    （或更糟，被解讀成別的東西）。`_save_schedules` 換成記錄用的絆線。"""
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    saves: list = []
    monkeypatch.setattr(b, "_save_schedules", lambda data: saves.append(data))
    message, sent = _schedule_test_message(monkeypatch)
    asyncio.run(b.cmd_schedule(message, payload))
    assert saves == [], saves
    assert len(sent) == 1 and expected in sent[0], sent
    assert "已建立" not in sent[0]

    # 對照組：同一組替身，寫對的那一筆要存得進去。
    sent.clear()
    asyncio.run(b.cmd_schedule(message, "add 09:30 sh echo hi"))
    assert len(saves) == 1 and "已建立排程" in sent[0], sent


async def _one_schedule_tick(saved: asyncio.Event):
    """跑 `_schedule_loop` 剛好一輪（它存過檔就收工）。

    直接叫真的迴圈而不是抄一份：這裡要驗的就是那個函式的臨界區，抄一份等於
    在驗抄本。
    """
    task = asyncio.create_task(b._schedule_loop())
    try:
        await asyncio.wait_for(saved.wait(), timeout=5.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _schedule_save_spy(monkeypatch, first_write_delay: float = 0.0,
                       ready_after: int = 1) -> asyncio.Event:
    """讓 `_save_schedules` 照常寫檔，但可以把**第一次**寫入拖慢、並在第 N 次
    寫完之後通知一聲。

    拖慢第一次是為了撐開「已經讀到、還沒寫進去」那個視窗——真實世界裡它就是
    兩次執行緒交接的長度，短到不好瞄準，但它一直都在。測試只是讓它變得可以
    重現，不是在製造一個不存在的狀況。
    """
    saved = asyncio.Event()
    real = b._save_schedules
    calls: list[int] = []

    def _spy(data):
        calls.append(1)
        if len(calls) == 1 and first_write_delay:
            time.sleep(first_write_delay)
        real(data)
        if len(calls) >= ready_after:
            saved.set()

    monkeypatch.setattr(b, "_save_schedules", _spy)
    return saved


def test_schedule_loop_does_not_clobber_a_concurrent_add(monkeypatch, tmp_path):
    """情境一：迴圈在臨界區裡 await 回報，這段空檔裡新增的排程被寫回去蓋掉。

    `_schedule_loop` 讀檔 → 發現一筆過期的一次性排程 → await 回報（真的網路
    往返，幾百毫秒）→ 存檔。asyncio 是協作式的，那個 await 就是 `cmd_schedule`
    插進來的縫：它新增第 2 筆並把 `next_id` 推到 3，然後迴圈醒來把**過期的
    快照**存回去——那筆整個消失、`next_id` 退回 2，而使用者已經被告知「已建立
    排程」。全程沒有任何錯誤訊息，這是靜默的資料遺失。
    """
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    monkeypatch.setattr(b, "_SCHEDULE_TICK_SEC", 0.0)
    message, sent = _schedule_test_message(monkeypatch)

    now = time.time()
    b._save_schedules({
        "version": 1, "next_id": 2,
        "entries": [{
            "id": 1, "when_kind": "once",
            # 早就過了補跑窗口 → 迴圈會回報並移除它（那條路徑有 await ＋ 存檔）
            "when_value": f"{now - b.SCHEDULE_CATCHUP_MAX_SEC - 3600:.0f}",
            "kind": "sh", "payload": "true", "channel_id": 1, "user_id": 7,
            "last_run": 0.0, "last_date": "",
        }],
    })
    saved = _schedule_save_spy(monkeypatch)

    async def _body():
        in_critical = asyncio.Event()

        async def _slow_report(_entry, _text):
            in_critical.set()
            await asyncio.sleep(0.05)      # 模擬送訊息的網路往返

        monkeypatch.setattr(b, "_schedule_report", _slow_report)
        tick = asyncio.create_task(_one_schedule_tick(saved))
        await asyncio.wait_for(in_critical.wait(), timeout=5.0)
        # 迴圈此刻手上是一份「還沒有第 2 筆」的快照
        await b.cmd_schedule(message, "add 09:30 sh echo hi")
        await tick
        return b._load_schedules()

    data = asyncio.run(_body())
    ids = sorted(int(e["id"]) for e in data.get("entries", []))
    assert any("2" in s for s in sent), f"測試前提壞了：沒有建立第 2 筆（{sent}）"
    assert ids == [2], (
        f"新增的排程被排程迴圈的過期快照蓋掉了：entries={data.get('entries')}。"
        "兩個寫入者必須共用同一把鎖，而且回報要移到臨界區外面再送。")
    _eq(int(data.get("next_id", 0)), 3, "next_id 沒有被倒退回去")
    print("  OK 迴圈的存檔沒有蓋掉並行的新增")


def test_schedule_loop_does_not_clobber_an_add_that_is_mid_write(monkeypatch,
                                                                 tmp_path):
    """情境二：反方向——迴圈在新增指令「已讀到、還沒寫進去」的視窗裡整輪跑完。

    `cmd_schedule add` 就算自己乖乖重讀，它的臨界區裡仍然有 await（讀檔與寫檔
    都丟給工作執行緒）。迴圈若不拿同一把鎖，就會在那兩次執行緒交接之間把整輪
    read-modify-write 跑完（它是同步的，一氣呵成）：它讀到的是**還沒有新排程**
    的檔案，寫上 `last_run` 存檔；接著新增那一邊的存檔落地，把 `last_run` 洗掉
    ——那筆排程會再跑一次，而且沒有任何錯誤。

    所以這一支釘的是**迴圈那一側也必須拿鎖**；把鎖拿掉、只留「回報移到臨界區
    外面」是不夠的。
    """
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    monkeypatch.setattr(b, "_SCHEDULE_TICK_SEC", 0.0)
    message, sent = _schedule_test_message(monkeypatch)

    b._save_schedules({
        "version": 1, "next_id": 2,
        "entries": [{
            "id": 1, "when_kind": "every", "when_value": "60",
            "kind": "sh", "payload": "true", "channel_id": 1, "user_id": 7,
            "last_run": 0.0, "last_date": "",
        }],
    })
    # 第一次寫入（新增那一邊的）拖慢；等第二次（迴圈的）寫完才算收工。
    saved = _schedule_save_spy(monkeypatch, first_write_delay=0.25,
                               ready_after=2)

    # 到期的那筆不要真的去跑外部指令——這裡驗的是檔案，不是執行。
    async def _no_run(_entry, **_kw):
        return None

    async def _report(_entry, _text):
        return None

    monkeypatch.setattr(b, "_run_schedule_entry", _no_run)
    monkeypatch.setattr(b, "_schedule_report", _report)

    async def _body():
        add = asyncio.create_task(
            b.cmd_schedule(message, "add 09:30 sh echo hi"))
        await asyncio.sleep(0.05)          # 讓 add 讀完檔、卡在寫入的工作執行緒裡
        tick = asyncio.create_task(_one_schedule_tick(saved))
        await add
        await tick
        return b._load_schedules()

    data = asyncio.run(_body())
    by_id = {int(e["id"]): e for e in data.get("entries", [])}
    assert sent, "測試前提壞了：新增指令沒有回覆"
    assert 2 in by_id, f"新增的排程不見了：{data.get('entries')}"
    assert float(by_id[1].get("last_run") or 0.0) > 0.0, (
        "排程迴圈在新增指令的寫入視窗裡插了一輪，它寫上的 `last_run` 被蓋掉了"
        f"——那筆排程會再跑一次。entries={data.get('entries')}")
    print("  OK 迴圈沒有插進新增指令的寫入視窗")


def test_every_schedule_file_access_holds_the_lock_and_the_snapshot_is_on_the_loop(
        monkeypatch, tmp_path):
    """排程檔的每一次讀寫都拿著 `_schedule_lock`；唯讀快照在事件迴圈上（2026-09-19）。

    原本 `cmd_schedule` 開頭的唯讀快照不拿鎖、丟工作執行緒，而 `_schedule_loop` 在
    迴圈上 `os.replace` 存檔。執行緒裡開著讀的 handle（連 `stat` 都算）會讓那次存檔
    失敗，剛寫上的 `last_run` 沒落地、那一筆下一輪**再跑一次**。

    兩個方向：
    * 快照必須在迴圈上、拿著鎖——`list` 的整張表只准有 `("loop", True)`。
    * `add`／`remove` 的重讀與存檔**刻意**留在工作執行緒裡（那把鎖與
      `test_schedule_loop_does_not_clobber_an_add_that_is_mid_write` 都以那次執行緒
      交接為前提），所以這個檔不能靠「全部留在迴圈上」排開讀寫，只能靠鎖：那兩條
      的表必須剛好是 `{("loop", True), ("thread", True)}`——每一次都拿著鎖。

    替身把呼叫轉給真的檔案，回覆內容與最後的檔案內容就是正面對照。
    """
    class _LockPath(_RecordedPath):
        def _note(self):
            self._seen.setdefault(self._label, set()).add(
                (_where_called(), b._schedule_lock.locked()))

    real = tmp_path / "sched.json"
    monkeypatch.setattr(b, "SCHEDULE_FILE", real)
    b._save_schedules({
        "version": 1, "next_id": 2,
        "entries": [{
            "id": 1, "when_kind": "every", "when_value": "3600",
            "kind": "sh", "payload": "true", "channel_id": 1, "user_id": 7,
            "last_run": time.time(), "last_date": "",
        }],
    })
    message, sent = _schedule_test_message(monkeypatch)

    def run(payload):
        seen: dict[str, set] = {}
        monkeypatch.setattr(b, "SCHEDULE_FILE", _LockPath("file", seen, real))
        sent.clear()
        asyncio.run(b.cmd_schedule(message, payload))
        return seen.get("file", set()), list(sent)

    listed, replies = run("list")
    assert any("排程 1 筆" in r for r in replies), replies
    assert listed == {("loop", True)}, listed

    added, replies = run("add 09:30 sh echo hi")
    assert any("已建立排程" in r for r in replies), replies
    assert added == {("loop", True), ("thread", True)}, added

    removed, replies = run("remove 1")
    assert any("已刪除排程" in r for r in replies), replies
    assert removed == {("loop", True), ("thread", True)}, removed

    left = json.loads(real.read_text(encoding="utf-8"))
    assert [int(e["id"]) for e in left["entries"]] == [2], left


def _schedule_file_access_outside_lock(tree) -> list[int]:
    """在 `_schedule_lock` 的 `async with` 之外碰到 `_load_schedules`／
    `_save_schedules` 的行號（兩支自己的定義除外）。

    用**識別字**比對（`ast.Name`），不看呼叫節點：丟工作執行緒的寫法裡它們是
    `to_thread(_load_schedules)` 的引數，不是呼叫。
    """
    import ast as _ast

    names = {"_load_schedules", "_save_schedules"}
    bad: list[int] = []

    def visit(node, locked):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and node.name in names:
            return
        if isinstance(node, _ast.AsyncWith) and any(
                "_schedule_lock" in _ast.dump(item.context_expr)
                for item in node.items):
            locked = True
        if isinstance(node, _ast.Name) and node.id in names and not locked:
            bad.append(node.lineno)
        for child in _ast.iter_child_nodes(node):
            visit(child, locked)

    visit(tree, False)
    return bad


def test_the_schedule_lock_extractor_sees_what_it_should():
    """上一支的判準自己要有對照組：乾淨的樹會讓「沒有違規」空轉通過。"""
    import ast as _ast

    def find(src):
        return _schedule_file_access_outside_lock(_ast.parse(src))

    assert find(
        "async def f():\n"
        "    data = await asyncio.to_thread(_load_schedules)\n") == [2], (
        "丟工作執行緒的寫法（函式是引數不是呼叫）沒被抓到")
    assert find("async def f():\n    _save_schedules({})\n") == [2]
    assert find(
        "async def f():\n"
        "    async with other_lock:\n"
        "        _load_schedules()\n") == [3], "別把鎖不算"
    assert find(
        "async def f():\n"
        "    async with _schedule_lock:\n"
        "        data = _load_schedules()\n"
        "        await asyncio.to_thread(_save_schedules, data)\n") == []
    assert find(
        "def _load_schedules():\n    return _load_schedules\n") == [], (
        "定義本身不算碰檔")


def test_every_schedule_file_access_in_the_bot_is_inside_the_lock():
    """行為測試只走得到 `list`／`add`／`remove`；這一支用 AST 蓋住其餘每一條路徑
    （`run`、排程迴圈、以後新增的子指令）。"""
    import ast as _ast

    src = (Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py").read_text(
        encoding="utf-8")
    tree = _ast.parse(src)
    refs = [n for n in _ast.walk(tree) if isinstance(n, _ast.Name)
            and n.id in ("_load_schedules", "_save_schedules")]
    assert len(refs) >= 5, f"擷取器看不到排程檔的讀寫了（{len(refs)} 處）"
    assert _schedule_file_access_outside_lock(tree) == [], (
        "有一處在 `_schedule_lock` 外面讀寫排程檔（行號見上）。這個檔的寫入者有的"
        "在迴圈上、有的在工作執行緒裡，讀與 `os.replace` 只能靠這把鎖排開。")


# ---------------------------------------------------------------------------
def _schedule_rmw_violations(tree, load_name="_load_schedules",
                             save_name="_save_schedules",
                             lock_name="_schedule_lock"):
    """`load → … → save` 中間有沒有**會抵達那個 save** 的 await 而且沒被鎖包住。

    三個容易寫錯的地方，前兩個都實際踩過：

    1. 把函式當引數丟進工作執行緒的寫法裡，`_save_schedules` 是 `ast.Name`
       不是 `ast.Call`——只掃 Call 會整個漏掉 `cmd_schedule`。所以這裡比對的是
       **識別字**（Name / Attribute），不是呼叫節點。
    2. 早退分支（回一句話之後就 `return`）裡的 await 抵達不了 save，算進去會
       誤報——佇列編輯那一族全部是這個形狀。但剪枝必須**針對這一個 save**：
       `cmd_schedule` 的 remove 與 add 是互斥分支、各有一個 save，用「這個分支
       裡有沒有 save」當條件會讓 remove 那半把 add 那半整個遮掉。
    3. `continue` / `break` **不算**離開函式——它們回到迴圈，迴圈跑完後面的
       save 照樣會執行。把它們當成早退，`_schedule_loop` 的兩處回報就會被剪掉，
       守門對真正的缺陷保持全綠。
    """
    import ast

    def _idents(node):
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                out.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                out.add(sub.attr)
        return out

    def _lines_of(node, name):
        out = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == name:
                out.append(sub.lineno)
            elif isinstance(sub, ast.Attribute) and sub.attr == name:
                out.append(sub.lineno)
        return out

    def _span(stmts):
        return (min(s.lineno for s in stmts),
                max(getattr(s, "end_lineno", None) or s.lineno for s in stmts))

    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        loads = sorted(set(_lines_of(fn, load_name)))
        saves = sorted(set(_lines_of(fn, save_name)))
        if not (loads and saves):
            continue

        awaits, exit_spans, lock_spans = [], [], []
        for node in ast.walk(fn):
            if isinstance(node, ast.Await):
                awaits.append(node.lineno)
            elif isinstance(node, ast.AsyncWith) and any(
                    lock_name in _idents(item.context_expr)
                    for item in node.items):
                lock_spans.append(_span(node.body))
            blocks = []
            if isinstance(node, ast.If):
                blocks = [node.body, node.orelse]
            elif isinstance(node, ast.Try):
                blocks = ([node.body] + [h.body for h in node.handlers]
                          + [node.orelse, node.finalbody])
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                blocks = [node.body, node.orelse]
            for block in blocks:
                # 只有 return / raise 真的離開函式；continue / break 不算。
                if block and isinstance(block[-1], (ast.Return, ast.Raise)):
                    exit_spans.append(_span(block))
        awaits = sorted(set(awaits))

        for save_line in saves:
            before = [ln for ln in loads if ln < save_line]
            if not before:
                continue
            load_line = max(before)
            if any(lo <= load_line and save_line <= hi for lo, hi in lock_spans):
                continue                      # 整段 load→save 被同一把鎖包住
            for await_line in awaits:
                if not load_line < await_line < save_line:
                    continue                  # load / save 那一句自己的 await 不算
                if any(lo <= await_line <= hi and not lo <= save_line <= hi
                       for lo, hi in exit_spans):
                    continue                  # 走不到這個 save 的早退分支
                found.append((fn.name, load_line, await_line, save_line))
                break
    return found


def test_the_schedule_file_has_no_read_modify_write_race():
    """排程檔的兩個寫入者不得在 load→save 中間讓對方插進來。

    asyncio 是協作式的：read 與 write 之間**沒有 await** 時整段等於原子操作，
    一旦中間有 await（送訊息是真的網路往返、丟進工作執行緒也會讓出控制權），
    另一個 task 就插得進來，兩邊各自把自己那份過期快照存回去——先寫的那筆整個
    消失，而使用者已經收到「已建立」的回覆。沒有例外、沒有 log，只有資料不見。

    所以每一段 load→save 只有兩種合法形狀：中間完全沒有 await，或者整段被
    `_schedule_lock` 包住。
    """
    import ast
    tree, _text = _bot_ast()

    # 守門的自我檢查。比對條件被改壞時，真實原始碼因為已經修好而照樣是空的
    # ——測試會保持綠，等於守門被無聲拆掉。所以先拿合成原始碼證明每一條判準
    # 都真的會命中／真的會放行。
    def _hits(src):
        return [h[0] for h in _schedule_rmw_violations(ast.parse(src))]

    assert _hits(
        "async def offender():\n"
        "    data = _load_schedules()\n"
        "    await report()\n"
        "    _save_schedules(data)\n") == ["offender"], (
        "守門的比對條件壞了：直白的 load→await→save 應該要被抓到")

    assert _hits(
        "async def offender2(m):\n"
        "    data = await asyncio.to_thread(_load_schedules)\n"
        "    await m.reply('x')\n"
        "    await asyncio.to_thread(_save_schedules, data)\n") == ["offender2"], (
        "守門漏掉了把函式當引數丟進工作執行緒的寫法——那裡的 `_save_schedules` "
        "是 ast.Name 不是 ast.Call，只掃 Call 的版本會整個看不到 `cmd_schedule`")

    assert _hits(
        "async def offender3():\n"
        "    data = _load_schedules()\n"
        "    for entry in data['entries']:\n"
        "        if entry['x']:\n"
        "            await report(entry)\n"
        "            continue\n"
        "    _save_schedules(data)\n") == ["offender3"], (
        "`continue` 不是離開函式——它回到迴圈，後面的 save 照樣會執行。把它當成"
        "早退，`_schedule_loop` 的兩處回報就會被剪掉、守門對真正的缺陷全綠")

    assert _hits(
        "async def offender4(m, sub):\n"
        "    data = await asyncio.to_thread(_load_schedules)\n"
        "    if sub == 'remove':\n"
        "        del data['entries'][0]\n"
        "        await asyncio.to_thread(_save_schedules, data)\n"
        "        return\n"
        "    if sub == 'add':\n"
        "        await validate()\n"
        "        data['entries'].append(1)\n"
        "        await asyncio.to_thread(_save_schedules, data)\n"
        "        return\n") == ["offender4"], (
        "互斥分支各有一個 save 時，剪枝必須**針對這一個 save**：用「這個分支裡"
        "有沒有 save」當條件，乾淨的 remove 那半會把有問題的 add 那半整個遮掉")

    assert _hits(
        "async def offender5():\n"
        "    async with _other_lock:\n"
        "        data = _load_schedules()\n"
        "        await report()\n"
        "        _save_schedules(data)\n") == ["offender5"], (
        "只有 `_schedule_lock` 算數——包在別把鎖裡不會讓兩個寫入者互斥")

    assert not _hits(
        "def fine():\n"
        "    data = _load_schedules()\n"
        "    data['x'] = 1\n"
        "    _save_schedules(data)\n"), "沒有 await 的不該被抓"

    assert not _hits(
        "async def fine2(m):\n"
        "    data = _load_schedules()\n"
        "    if not data:\n"
        "        await m.reply('empty')\n"
        "        return\n"
        "    _save_schedules(data)\n"), "早退分支裡的 await 抵達不了 save，不該誤報"

    assert not _hits(
        "async def fine3():\n"
        "    async with _schedule_lock:\n"
        "        data = await asyncio.to_thread(_load_schedules)\n"
        "        await something()\n"
        "        await asyncio.to_thread(_save_schedules, data)\n"), (
        "整段被鎖包住就是合法的")

    # 鎖之外的第二條性質：迴圈那一側的臨界區要維持**零 await**。鎖已經保證了
    # 資料正確，這一條保證的是「別把整個排程系統卡在一次網路往返上」——回報一旦
    # 搬回鎖裡，每一則都會讓 `/schedule add`／`remove` 陪等。
    loop_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_schedule_loop")
    critical = [n for n in ast.walk(loop_fn)
                if isinstance(n, ast.AsyncWith)
                and any("_schedule_lock" in ast.dump(item.context_expr)
                        for item in n.items)]
    assert critical, "`_schedule_loop` 沒有拿 `_schedule_lock`——兩個寫入者不互斥了"
    stray = sorted({sub.lineno for block_ in critical
                    for stmt in block_.body
                    for sub in ast.walk(stmt)
                    if isinstance(sub, ast.Await)})
    assert not stray, (
        f"`_schedule_loop` 的臨界區裡出現 await（第 {stray} 行）。回報請收進 "
        "`pending`、出了鎖再送；臨界區裡等網路往返會讓每一個 `/schedule` 指令陪等。")

    offenders = _schedule_rmw_violations(tree)
    assert not offenders, (
        "這些函式在讀取排程檔與寫回之間 await 了，另一個寫入者會插進來、"
        "兩邊各存一份過期快照（先寫的那筆靜默消失）："
        + "、".join(f"`{fn}`（load 第 {ld} 行、await 第 {aw} 行、"
                    f"save 第 {sv} 行）"
                    for fn, ld, aw, sv in offenders)
        + "。修法：把整段 load→mutate→save 包進 `_schedule_lock`，"
          "並把送訊息之類的回報移到臨界區外面再送。")
    print("  OK 排程檔的 load→save 之間沒有會抵達 save 的裸 await")


def test_option_helpers_for_gui_commands():
    """`--shot` 旗標與吃到句尾的 `--win` / `--then`。"""
    _eq(b._take_flag("500 300 --shot", "shot"), ("500 300", True), "flag found")
    _eq(b._take_flag("500 300", "shot"), ("500 300", False), "flag absent")

    # 視窗標題常常帶空白，所以這個選項吃到句尾
    _eq(b._take_trailing_option("40 12 --win 未命名 - 記事本", "win"),
        ("40 12", "未命名 - 記事本"), "trailing win")
    _eq(b._take_trailing_option("40 12", "win"), ("40 12", ""), "no win")
    # `--window` 不該被 `--win` 命中
    _eq(b._take_trailing_option("x --windowless y", "win"),
        ("x --windowless y", ""), "prefix is not a match")
    # `--run … --then …` 兩個都寫得出來（`--then` 先拆，它在最後面）
    rest, cmd = b._take_trailing_option("text 完成 --run m a --then echo hi", "then")
    _eq((rest, cmd), ("text 完成 --run m a", "echo hi"), "then split")
    _eq(b._take_trailing_option(rest, "run"), ("text 完成", "m a"), "run split")


def test_seconds_per_image():
    # 沒有 ts 的事件 → 沒有「新鮮」樣本 → 退回最近 3 筆（這裡就是全部兩筆）。
    evs = [
        {"type": "character_done", "saved": 10, "elapsed_sec": 500},
        {"type": "character_done", "saved": 5, "elapsed_sec": 250},
        {"type": "character_start", "saved": 0},  # ignored
    ]
    spi, measured, basis, _v = b._seconds_per_image(evs)
    _eq(measured, True, "has history -> measured")
    _eq(basis, "recent", "no ts -> recent-N fallback")
    _eq(round(spi, 4), round(750 / 15, 4), "measured rate")
    # no usable history -> pessimistic constant
    _eq(b._seconds_per_image([]),
        (float(b.ETA_SECONDS_PER_IMAGE), False, "default", True),
        "empty -> fallback")
    # saved=0 / elapsed=0 rows are skipped, not div-by-zero
    bad = [{"type": "character_done", "saved": 0, "elapsed_sec": 500},
           {"type": "character_done", "saved": 5, "elapsed_sec": 0}]
    _eq(b._seconds_per_image(bad),
        (float(b.ETA_SECONDS_PER_IMAGE), False, "default", True),
        "saved/elapsed 0 -> skipped -> fallback")


def _restart_events():
    """重現 2026-09-07 的實際形狀：一個角色橫跨兩次重啟，另一個角色乾乾淨淨。

    回 `(events, done_ember, done_clean)`。時間戳照抄實測的相對關係：`ember`
    在 09-06 19:19 開跑（`resumed=0`），03:03 崩一次重生（`resumed=59`）、
    06:19 再崩一次重生（`resumed=84`），10:39 完成，`elapsed_sec` 只有 4.33
    小時而 `saved` 是 120。乾淨的那一筆是 `last rite`：一次跑完 120 張、
    9.20 小時。
    """
    t0 = 1_700_000_000.0
    clean_el = 33120.0        # 9.20h，實測的乾淨樣本
    ember_el = 15578.9        # 4.33h，實測的被污染樣本
    clean_start = t0
    clean_done = clean_start + clean_el
    ember_run1 = clean_done + 60.0          # 上一個角色結束後接著開跑
    ember_run2 = ember_run1 + 7.75 * 3600   # 第一次重生
    ember_run3 = ember_run2 + 3.25 * 3600   # 第二次重生
    ember_done = ember_run3 + ember_el
    evs = [
        {"type": "character_start", "name": "last rite (arknights)",
         "ts": clean_start, "target": 120, "folder": "last rite (arknights)",
         "resumed": 0},
        {"type": "character_done", "name": "last rite (arknights)",
         "ts": clean_done, "saved": 120, "elapsed_sec": clean_el},
        {"type": "character_start", "name": "ember (arknights)",
         "ts": ember_run1, "target": 120, "folder": "ember (arknights)",
         "resumed": 0},
        {"type": "critical_error", "ts": ember_run2 - 60.0},
        {"type": "character_start", "name": "ember (arknights)",
         "ts": ember_run2, "target": 120, "folder": "ember (arknights)",
         "resumed": 59},
        {"type": "critical_error", "ts": ember_run3 - 60.0},
        {"type": "character_start", "name": "ember (arknights)",
         "ts": ember_run3, "target": 120, "folder": "ember (arknights)",
         "resumed": 84},
        {"type": "character_done", "name": "ember (arknights)",
         "ts": ember_done, "saved": 120, "elapsed_sec": ember_el},
    ]
    return evs, evs[-1], evs[1]


def _done(**kw):
    """一筆乾淨的 `character_done`，欄位可覆寫。"""
    ev = {"type": "character_done", "name": "x", "ts": 1_700_000_000.0 - 100.0,
          "elapsed_sec": 300.0, "saved": 10}
    ev.update(kw)
    return ev


_NOW = 1_700_000_000.0


def _spi(events):
    return b._seconds_per_image(events, now=_NOW)


def test_an_infinite_elapsed_time_cannot_become_a_measured_estimate():
    """`elapsed_sec > 0` 對 `nan` 是 False（擋得掉），對 **`inf` 是 True**。

    2026-09-08 實測舊版：`elapsed_sec: Infinity` → `/eta` 回 **`inf` 秒/張，而且
    `measured=True`**。那比誠實回預設值糟——`measured` 會讓呼叫端據實轉述成「根據
    最近幾輪量到的」，也就是把一個無意義的數字包裝成量測結果。

    來源是真的：`events.ndjson` 是磁碟上的 JSON，而 `json.loads` 預設就吃
    `Infinity`／`NaN`；`1e400` 這種看起來完全正常的字面值 parse 出來也是 `inf`。
    """
    spi, measured, basis, _v = _spi([_done(elapsed_sec=float("inf"))])
    assert spi == float(b.ETA_SECONDS_PER_IMAGE)
    assert (measured, basis) == (False, "default"), (
        f"`inf` 被收成樣本了：{spi} 秒/張 measured={measured}")


def test_an_infinite_image_count_cannot_become_a_measured_estimate():
    """反方向，而且更難看出來：`saved: Infinity` 會讓分母變 `inf`，相除得到
    **0.0 秒/張**且 `measured=True`——`/eta` 會說整個佇列不用時間就跑得完。

    `inf` 至少一眼看得出壞掉，`0` 看起來像「快好了」。
    """
    spi, measured, _b, _v = _spi([_done(saved=float("inf"))])
    assert (spi, measured) == (float(b.ETA_SECONDS_PER_IMAGE), False), (
        f"`saved=inf` 被收成樣本了：{spi} 秒/張")


def test_one_corrupt_sample_does_not_poison_the_healthy_ones():
    """總和是加起來的，所以一筆 `inf` 會把整批污染掉——不是少一筆樣本而已。"""
    good = _done()
    spi, measured, _b, _v = _spi([good, _done(elapsed_sec=float("inf"))])
    assert measured is True, "健康的那一筆也被丟掉了"
    assert spi == pytest.approx(good["elapsed_sec"] / good["saved"]), (
        f"壞掉的那一筆污染了總和：{spi}")


@pytest.mark.parametrize("field,value", [
    ("elapsed_sec", float("inf")), ("elapsed_sec", float("nan")),
    ("elapsed_sec", float("-inf")), ("elapsed_sec", int("9" * 400)),
    ("saved", float("inf")), ("saved", float("nan")), ("saved", int("9" * 400)),
    # 非有限之外，非正數也一樣不能當分子——`0` 會讓每張圖看起來不花時間，
    # 負數會產出負的秒/張。
    ("elapsed_sec", 0), ("elapsed_sec", -5.0),
])
def test_no_unusable_number_ever_reaches_the_division(field, value):
    """大到轉不成 float 的 int 是同一家族的第三種：JSON 的整數沒有上限，而
    `float(10**400)` 與 `math.isfinite(10**400)` **都丟 `OverflowError`**。舊版
    在 `float(saved)` 那一行就會炸，等於 `/eta` 整支消失。"""
    spi, measured, basis, _v = _spi([_done(**{field: value})])
    assert (spi, measured, basis) == (
        float(b.ETA_SECONDS_PER_IMAGE), False, "default")


@pytest.mark.parametrize("bad_elapsed", [0, -5.0])
def test_a_non_positive_elapsed_must_be_dropped_before_the_sum_not_after(
        bad_elapsed):
    """單獨餵一筆壞的驗不到這道守門——要配一筆好的才分得開。

    只餵 `elapsed_sec=0`（或負數）時，函式最後那道
    `total_elapsed <= 0` **也會**擋下來並回預設值，於是把取樣層的 `elapsed > 0`
    整個刪掉照樣是綠的（實測：那個變異存活）。這是本專案反覆出現的「兩道防護互相
    遮蔽」。

    配一筆健康的樣本就分得開了：總和變成正數，底下那道守門過得去，而壞掉的那筆
    會**稀釋**估計值——`/eta` 回一個看起來正常、實際上偏樂觀一倍的數字，還宣稱
    `measured=True`。這才是真正的危害，不是除以零。
    """
    good = _done()
    spi, measured, _b, _v = _spi([good, _done(elapsed_sec=bad_elapsed)])
    honest = good["elapsed_sec"] / good["saved"]
    assert measured is True
    assert spi == pytest.approx(honest), (
        f"`elapsed_sec={bad_elapsed}` 被算進總和，估計值被稀釋成 {spi}（應為 "
        f"{honest}）")


def test_the_pairing_helper_rejects_an_unusable_saved_count_on_its_own():
    """守門要一支一支測得出來。

    這一支只戳 `_images_this_run`：把 `_seconds_per_image` 那一側的有限性檢查
    刪掉，這支仍然綠——反之亦然。兩道守門擋同一個輸入時會互相遮蔽，於是刪掉任何
    一道都測不出來，所以每一道都要有一筆**只踩它**的測資。
    """
    _eq(b._images_this_run(_done(saved=float("inf")), []), (0.0, False),
        "`saved=inf` 要當成「這筆沒得用」")
    _eq(b._images_this_run(_done(saved=float("nan")), []), (0.0, False))
    _eq(b._images_this_run(_done(saved=int("9" * 400)), []), (0.0, False),
        "超大 int 要擋在 `float()` 之前，不是讓它丟 OverflowError")


def test_an_unusable_resumed_count_is_reported_as_unknown_not_as_zero():
    """`resumed` 不能用時要回「查不到」（沿用 `saved` ＋ False），不是硬當成 0。

    當成 0 是把「不知道有沒有跨越重啟」講成「沒有跨越」，也就是靜默退回那個已知
    偏樂觀的答案——`_images_this_run` 的第二個回傳值存在的理由就是這個。
    """
    def start(resumed):
        return {"type": "character_start", "name": "x",
                "ts": _NOW - 400.0, "resumed": resumed}

    _eq(b._images_this_run(_done(), [start(float("inf"))]), (10.0, False))
    _eq(b._images_this_run(_done(), [start(float("nan"))]), (10.0, False))
    _eq(b._images_this_run(_done(), [start(4)]), (6.0, True),
        "正常的 `resumed` 不得被這道檢查誤傷")


def test_an_infinite_timestamp_cannot_fake_a_pairing():
    """有限性在配對這一段是**正確性**的前提，不只是防呆。

    `ts` 或 `elapsed_sec` 是 `inf` 時 `run_start` 會變成 `nan`，而
    `gap = abs(sts - nan)` 也是 `nan`——於是 `gap > tolerance` 恆為 False，時間
    容差整個失效，第一筆同名的 `character_start` 就會被當成配對成功並且宣稱
    「查得到」。配錯人比配不到糟：配不到會誠實回報。
    """
    far_away = {"type": "character_start", "name": "x",
                "ts": _NOW - 9_999_999.0, "resumed": 7}
    got, verified = b._images_this_run(
        _done(ts=float("inf")), [far_away])
    assert verified is False, "時間戳不可用時還宣稱配得到"
    assert got == 10.0, "應該沿用 `saved`"


def test_a_finite_event_number_keeps_ordinary_values_untouched():
    """反面：這道檢查不得誤傷正常資料——包含 0、負數與整數型別。"""
    for raw, want in [(0, 0.0), (7, 7.0), (-3.5, -3.5), (1e308, 1e308)]:
        assert b._finite_event_number(raw) == want
    for bad in (True, False, None, "5", [], {}, float("nan"), float("inf")):
        assert b._finite_event_number(bad) is None, f"{bad!r} 應該被擋掉"


def test_a_character_that_crossed_a_restart_is_not_counted_as_fast():
    """`elapsed_sec` 是**本輪**的，`saved` 是**累計**的——直接相除等於用錯分母。

    2026-09-07 量到的那一筆：`ember` saved=120、elapsed=4.33h ＝ 129.8 秒/張
    ＝ 27.7 張/小時。這個帳號的額度滴入速率只有約 7.85 張/小時，所以那個數字
    **在物理上不可能**。真相是那個角色橫跨了兩次重啟，`saved` 從頭算、
    `elapsed_sec` 只從最後一次重生算。

    這跟已經修好的快/慢相位偏差是**兩個獨立的偏差**，而且這個更大：相位修正
    落地之後 `/eta` 反而比修正前更樂觀（202.9 vs 276.0 秒/張），因為視窗裡多
    出來的第二筆正是這筆被污染的樣本。

    兩個方向都要釘住：
    - 拿掉修正（直接用 `saved`）→ 紅；
    - 把修正套到沒被污染的樣本上（`resumed=0` 那一筆被動到、或配對配錯人）
      → 紅。
    """
    evs, ember, clean = _restart_events()
    starts = [e for e in evs if e["type"] == "character_start"]

    # --- 1. 被污染的那一筆：用**本輪**產出當分母 -------------------------
    _eq(b._images_this_run(ember, starts), (36.0, True),
        "跨越重啟 -> 120 - 84 = 36 張")
    _eq(round(ember["elapsed_sec"] / 36.0, 1), 432.7, "修正後 432.7 秒/張")
    assert round(ember["elapsed_sec"] / ember["saved"], 1) == 129.8, (
        "前提變了：這筆樣本原本並不偏樂觀，這支測試要重寫")

    # --- 2. 沒被污染的一律不得改動（反方向）-----------------------------
    _eq(b._images_this_run(clean, starts), (120.0, True),
        "resumed=0 -> 一張都不扣")

    # --- 3. 配不到 `character_start` 要走安全退路，而且**說得出來** ------
    # 事件檔會輪替，尾端最舊的那幾筆 done 的 start 可能已經被切掉。這時只能
    # 沿用 `saved`（＝修正前的行為），但不可以靜默——所以第二個回傳值是 False。
    # 這一筆同時是「配對配錯人」的反方向守門：同名的 start 還在（resumed=59
    # 與 84），只是時間對不上；少了時間容差就會抓錯一筆並且宣稱查得到。
    orphan = dict(ember)
    orphan["ts"] = ember["ts"] + 40 * 3600      # 自己那筆 start 已經被切掉
    _eq(b._images_this_run(orphan, starts), (120.0, False),
        "配不到 -> 沿用 saved，但誠實回報查不到")
    _eq(b._images_this_run(ember, []), (120.0, False), "完全沒有 start 可配")

    # --- 4. `saved - resumed <= 0` 不得炸，也不得產出負的秒/張 -----------
    nothing_new = [
        {"type": "character_start", "name": "x", "ts": 1000.0, "resumed": 120},
        {"type": "character_done", "name": "x", "ts": 1600.0,
         "saved": 120, "elapsed_sec": 600.0},
    ]
    _eq(b._images_this_run(nothing_new[1], [nothing_new[0]]), (0.0, True),
        "本輪一張都沒產出")
    _eq(b._seconds_per_image(nothing_new, now=1700.0),
        (float(b.ETA_SECONDS_PER_IMAGE), False, "default", True),
        "本輪產出 0 -> 跳過該樣本，不是 ZeroDivisionError")

    # --- 5. 端到端：兩筆一起算 -------------------------------------------
    now = evs[-1]["ts"] + _HOUR
    spi, measured, basis, verified = b._seconds_per_image(evs, now=now)
    _eq(measured, True, "有歷史資料")
    _eq(verified, True, "兩筆都配得到 start")
    expected = (clean["elapsed_sec"] + ember["elapsed_sec"]) / (120 + 36)
    _eq(round(spi, 3), round(expected, 3), "本輪產出當分母")
    naive = (clean["elapsed_sec"] + ember["elapsed_sec"]) / (120 + 120)
    assert round(spi, 1) != round(naive, 1), (
        f"退回用 saved 當分母了（{naive:.1f} 秒/張）——那正是要修掉的偏差")
    assert spi > naive, "修正只會讓估計變慢，不會變快"

    # --- 6. 配不到的樣本要讓 `verified` 一路傳到呼叫端 -------------------
    no_starts = [e for e in evs if e["type"] != "character_start"]
    _, _m, _b, verified2 = b._seconds_per_image(no_starts, now=now)
    _eq(verified2, False, "沒有 start 可配 -> 呼叫端必須知道這個數字沒查過")

    # --- 7. 呼叫端真的接上了 ---------------------------------------------
    import inspect
    eta = inspect.getsource(b.cmd_eta)
    assert "verified" in eta, (
        "/eta 沒有接住 `verified`——「查不到」會靜默地退回那個已知偏樂觀的答案，"
        "而使用者看到的 footer 依然一副很有把握的樣子")
    spi_src = inspect.getsource(b._seconds_per_image)
    assert "_images_this_run" in spi_src, (
        "`_seconds_per_image` 不再走配對修正；跨越重啟的角色會再次被算成很快")


def _quiet_diagnostic(monkeypatch, tmp_path, scan):
    """把 `_failure_diagnostic_summary` 的其他子項全部靜音，只留行程掃描那一項。"""
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", tmp_path / "nope.marker")
    monkeypatch.setattr(b, "_free_disk_gb", lambda: None)
    monkeypatch.setattr(b, "_single_image_request_pending_on_disk", lambda: False)
    monkeypatch.setattr(b, "_recent_log_error_count", lambda *a, **k: None)
    monkeypatch.setattr(b, "_active_pid", lambda: None)
    monkeypatch.setattr(b, "_find_all_webrunner_pids", lambda *a, **k: scan)
    return b._failure_diagnostic_summary("runtime")


def test_a_failed_process_scan_is_not_reported_as_a_clean_machine(
        monkeypatch, tmp_path):
    """掃不到漏網的背景程式，跟「真的沒有漏網的」，在**使用者看得到的地方**
    必須說得不一樣。

    這份摘要是在已經出事的時候貼出來的，而「是不是有第二個實例在搶同一個瀏覽器
    設定檔」正是那種時候最該懷疑的原因之一。修之前兩種情況送出的文字一字不差
    （都只有一句 "no obvious local blocker"），所以那個最該被懷疑的原因反而是
    唯一不會被提起的。

    兩個方向都要釘：掃不成要說，掃成了就不准多話——否則把警告寫死成永遠出現
    也會讓上半段通過，而永遠出現的警告跟永遠沉默一樣沒有資訊。
    """
    clean = _quiet_diagnostic(monkeypatch, tmp_path, ([], True))
    broken = _quiet_diagnostic(monkeypatch, tmp_path, ([], False))
    assert "no obvious local blocker" in clean, clean
    assert clean != broken, (
        "掃描失敗與乾淨機器送出同一段文字，使用者無從分辨")
    assert "no obvious local blocker" not in broken, (
        f"掃不成卻還宣稱「沒有明顯的問題」：{broken!r}")
    # 有掃到東西的時候，那一句本來就會出現，不該再被掃描狀態影響。
    found = _quiet_diagnostic(monkeypatch, tmp_path,
                              ([(4242, "webrunner_novelai.py")], True))
    assert "untracked background processes" in found, found
    # 洩漏規則第 1 層：這段文字會直接貼進對話平台。
    for banned in ("4242", "webrunner", "novelai", ".py", "D:\\"):
        assert banned not in broken, f"診斷摘要洩漏了 {banned!r}：{broken!r}"


def test_an_unreadable_pid_file_is_never_deleted(monkeypatch, tmp_path):
    """讀不出來的存活訊號**不可以刪**——刪掉會讓所有下游的正確判斷一起失效。

    `UnicodeDecodeError` 是 `ValueError` 的**子類**，所以「檔案不是 UTF-8」會落進
    舊版的 `except (ValueError, OSError)` → bot 把啟動器寫的訊號刪掉 → 回「沒有
    批次在跑」。連鎖是這樣長的：檔案現在**真的**不存在了，於是另外兩個讀取端
    （驗證瀏覽器、啟動器）**正確地**判定「真的沒有批次」，然後在正式批次旁邊開
    第二套瀏覽器搶同一份登入設定檔。它們的判斷沒錯，錯的是證據被毀了。

    三個方向都要釘：殘檔要刪（不然殘檔永遠留著）、讀不出來不准刪、正常的要讀得到。
    """
    pid_file = tmp_path / "webrunner.pid"
    monkeypatch.setattr(b, "WEBRUNNER_PID_FILE", pid_file)

    # 1. 檔案不存在 -> 判定得出來：真的沒有。
    _eq(b._load_pid(), (None, True), "沒有檔案")

    # 2. 內容不是 UTF-8（Big5 的中文，實際會發生：本機 locale 是 cp950）。
    pid_file.write_bytes("中文".encode("big5"))
    _eq(b._load_pid(), (None, False), "解不開的位元組 -> 判不出來")
    assert pid_file.exists(), (
        "讀不出來就把跨行程的存活訊號刪掉了——下游會據此開第二套瀏覽器")

    # 3. 內容是文字但不是數字。
    pid_file.write_text("not a pid", encoding="utf-8")
    _eq(b._load_pid(), (None, False), "解析不出來 -> 判不出來")
    assert pid_file.exists(), "解析失敗也不可以刪"

    # 4. 讀得出來、但那個行程已經死了 -> 這才是殘檔，刪掉是對的。
    monkeypatch.setattr(b, "_pid_alive", lambda pid: False)
    pid_file.write_text("4242", encoding="utf-8")
    _eq(b._load_pid(), (None, True), "死掉的 pid")
    assert not pid_file.exists(), "真正的殘檔沒有被清掉"

    # 5. 讀得出來而且還活著。
    monkeypatch.setattr(b, "_pid_alive", lambda pid: True)
    pid_file.write_text("4242", encoding="utf-8")
    _eq(b._load_pid(), (4242, True), "活著的 pid")
    assert pid_file.exists()

    # 6. 刪不掉（權限不足）不得往上炸——`_clear_pid` 被十幾處收尾路徑呼叫。
    def _boom(*_a, **_kw):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(type(pid_file), "unlink", _boom, raising=False)
    monkeypatch.setattr(b, "_pid_alive", lambda pid: False)
    _eq(b._load_pid(), (None, True), "刪不掉也要回得出答案")
    b._clear_pid()          # 不可以拋


class _LiveChild:
    pid = 31337

    def __init__(self, exited=None):
        self.exited = exited

    def poll(self):
        return self.exited


def test_a_live_batch_counts_as_running_even_without_its_pid_file(monkeypatch, tmp_path):
    """存活訊號只是**跨行程**的那一半；bot 自己開的子行程（或上一輪記下、還活著的 pid）
    才是第一手答案。訊號檔沒寫成（寫檔失敗、剛被別人刪掉）時，只看磁碟會說「沒在跑」，
    接著某條路徑就再開一套瀏覽器、跟正在跑的那一套搶同一份登入設定檔。

    訊號檔刻意指到一個不存在的位置：磁碟那一條回「確定沒在跑」，答案只能來自前兩條。"""
    monkeypatch.setattr(b, "WEBRUNNER_PID_FILE", tmp_path / "absent.pid")
    alive_pids: list = []
    monkeypatch.setattr(b, "_pid_alive", lambda pid: alive_pids.append(pid) or pid == 4242)

    monkeypatch.setattr(b, "_webrunner_proc", _LiveChild())
    monkeypatch.setattr(b, "_webrunner_pid", None)
    _eq(b._webrunner_liveness(), (True, True), "自己的子行程還在跑")
    _eq(b._active_pid(), 31337, "要終止的是那個子行程")

    monkeypatch.setattr(b, "_webrunner_proc", _LiveChild(exited=0))
    monkeypatch.setattr(b, "_webrunner_pid", 4242)
    _eq(b._webrunner_liveness(), (True, True), "記下的 pid 還活著")
    _eq(b._active_pid(), 4242, "子行程已經結束，改指那個 pid")

    # 對照組：兩條都說沒在跑、磁碟也沒有訊號 → 真的沒在跑。
    monkeypatch.setattr(b, "_webrunner_pid", 5151)
    _eq(b._webrunner_liveness(), (False, True), "都不在")
    _eq(b._active_pid(), None, "沒有東西可以終止")
    assert 5151 in alive_pids


def test_an_undecidable_pid_read_is_treated_as_still_running(
        monkeypatch, tmp_path):
    """判不出來的時候要**保守當成在跑**，而且顯示端要說出「不確定」。

    代價完全不對稱：說成「沒在跑」會讓某條路徑再開一套瀏覽器、跟正在跑的那一套
    搶同一份登入設定檔；說成「在跑」最多是某個指令拒絕動作，重試一次就好。

    反方向一樣要測——永遠回 True 的話這個函式就沒有資訊了。
    """
    pid_file = tmp_path / "webrunner.pid"
    monkeypatch.setattr(b, "WEBRUNNER_PID_FILE", pid_file)
    monkeypatch.setattr(b, "_webrunner_proc", None)
    monkeypatch.setattr(b, "_webrunner_pid", None)

    _eq(b._webrunner_liveness(), (False, True), "真的沒在跑")
    _eq(b._webrunner_alive(), False, "真的沒在跑 -> False")

    pid_file.write_bytes("中文".encode("big5"))
    monkeypatch.setattr(b, "_webrunner_pid", None)
    _eq(b._webrunner_liveness(), (True, False), "判不出來 -> 保守當成在跑")
    _eq(b._webrunner_alive(), True, "保守")

    # 而且**不能**因為保守就把「不確定」吞掉：顯示端要看得到差別。
    import inspect
    for name in ("cmd_status", "cmd_health"):
        source = inspect.getsource(getattr(b, name))
        assert "_webrunner_liveness" in source, (
            f"{name} 只問了 `_webrunner_alive()`——那會把一個保守推定顯示成"
            "查證過的事實")


def _run_reply(monkeypatch, coro_factory):
    """跑一個會 `safe_reply` 的 handler，把它送出的文字抓回來。"""
    import asyncio as _asyncio
    box = {}

    async def _recorder(_message, content=None, **kw):
        box["content"] = content
        box.update(kw)

    monkeypatch.setattr(b, "safe_reply", _recorder)
    _asyncio.run(coro_factory())
    return box.get("content") or ""


def _health_background_line(monkeypatch, scan, alive=False, decided=True):
    monkeypatch.setattr(b, "_webrunner_liveness", lambda: (alive, decided))
    monkeypatch.setattr(b, "_webrunner_alive", lambda: alive)
    monkeypatch.setattr(b, "_active_pid", lambda: 4242 if alive else None)
    monkeypatch.setattr(b, "_find_all_webrunner_pids", lambda *a, **k: scan)
    text = _run_reply(monkeypatch, lambda: b.cmd_health(object()))
    for line in text.splitlines():
        if "背景產圖程式" in line:
            return line
    raise AssertionError(f"health 沒有背景產圖程式那一行：{text!r}")


def test_health_does_not_report_a_failed_scan_as_nothing_running(
        monkeypatch, tmp_path):
    """`⏸️ not running` 是一個結論；掃不成的時候它沒有根據。

    這是四個呼叫點裡最危險的一格：使用者看到「沒有在跑」就會直接下 `/gen run`，
    而 sweep 那一輪同樣掃不成、什麼都沒殺，於是兩個背景產圖程式同時搶同一個
    瀏覽器設定檔。修之前這兩種情況送出的字**一模一樣**。

    三個方向都要測：掃得成且乾淨、掃不成、以及真的掃到孤兒——中間那個單獨測的話，
    把訊息寫死成永遠帶警告也會過。
    """
    clean = _health_background_line(monkeypatch, ([], True))
    broken = _health_background_line(monkeypatch, ([], False))
    found = _health_background_line(
        monkeypatch, ([(4242, "webrunner_novelai.py")], True))
    assert "not running" in clean, clean
    assert clean != broken, "掃描失敗與「真的沒在跑」送出同一行，使用者無從分辨"
    assert "not running" not in broken, f"掃不成卻宣稱沒在跑：{broken!r}"
    assert "orphan" in found, found
    # 掃到孤兒**而且**沒掃完整：數字本身是對的，但「就這些了」不成立。少了這一句
    # 使用者會以為殺掉這幾個就乾淨了。
    found_broken = _health_background_line(
        monkeypatch, ([(4242, "webrunner_novelai.py")], False))
    assert found_broken != found, (
        "掃到孤兒的那一半把掃描狀態丟掉了：完整與不完整送出同一行")
    # 執行中那一半也要說——「running + 0 個孤兒」同樣是個沒有根據的結論。
    alive_ok = _health_background_line(monkeypatch, ([], True), alive=True)
    alive_broken = _health_background_line(monkeypatch, ([], False), alive=True)
    assert alive_ok != alive_broken, (
        "執行中那一半把掃描狀態丟掉了：兩種情況送出同一行")
    # 存活訊號讀不出來時「running」是保守推定、不是查證結果，也要說出來。
    undecided = _health_background_line(monkeypatch, ([], True), alive=True,
                                        decided=False)
    assert undecided != alive_ok, (
        "把一個保守推定顯示成查證過的事實——見 `_webrunner_liveness`")
    # 洩漏規則第 1 層。
    for banned in ("4242", "webrunner", "novelai", ".py", "D:\\"):
        assert banned not in broken and banned not in alive_broken, banned


def _quiet_doctor(monkeypatch, tmp_path):
    """把 `cmd_doctor` 的每一個探測都換成「沒事」，讓一支測試只動它要量的那一格。

    兩個理由，第二個比較要緊：

    1. 決定性。真的探測讀的是這台機器的佇列、log、排程工作與登錄檔，輸出每天不同。
    2. **副作用。** 桌面解鎖時，真的 `_gui.input_reaches_system()` 會送一個 F13
       按鍵到前景視窗（見 `cmd_doctor` 的 docstring）。這個夾具出現以前，
       `test_doctor_does_not_pass_a_check_it_could_not_run` 每跑一次就對這台機器
       按兩次鍵，還順帶真的去查排程工作與登錄檔——而跑測試的人通常正坐在前面。

    換的是 `_gui` 模組上的屬性，不是 bot 模組上的名字：doctor 透過 `_gui.x` 取用，
    換這一層才打得到。安靜的程度由 `test_the_quiet_doctor_harness_is_quiet` 對帳。
    """
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", tmp_path / "no-pause-marker")
    # 門檻釘死：正式設定改成 0（停用）的話，剩餘空間那一格會整個不跑，底下量它的
    # 測試就等於沒測。
    monkeypatch.setattr(b, "MIN_FREE_DISK_GB", 5.0)
    monkeypatch.setattr(b, "_free_disk_gb", lambda: 999.0)
    monkeypatch.setattr(b, "read_todo_entries", lambda *a, **k: ["entry"])
    monkeypatch.setattr(b, "_single_image_request_pending_on_disk", lambda: False)
    monkeypatch.setattr(b, "_active_pid", lambda: None)
    monkeypatch.setattr(b, "_find_all_webrunner_pids", lambda *a, **k: ([], True))
    monkeypatch.setattr(b, "_recent_log_error_count", lambda *a, **k: None)
    monkeypatch.setattr(b, "find_stale_components", lambda *a, **k: [])
    monkeypatch.setattr(b, "autostart_recovery_status",
                        lambda *a, **k: {"gap": None})
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: {})
    monkeypatch.setattr(b, "DOROSSI_CC_TOOLS", "off")
    monkeypatch.setattr(b, "_MACRO_RECORDING", None)
    monkeypatch.setattr(b, "_missing_dependencies", lambda: [])
    monkeypatch.setattr(b._gui, "input_desktop_available", lambda: True)
    monkeypatch.setattr(b._gui, "ocr_status", lambda: (True, ""))
    monkeypatch.setattr(b._gui, "input_reaches_system", lambda: True)
    monkeypatch.setattr(b._gui, "held_inputs", lambda: [])
    monkeypatch.setattr(b._gui, "job_list", lambda: [])


_QUIET_DOCTOR_REPLY = "**doctor**\n- OK no obvious blocker found."


def test_the_quiet_doctor_harness_is_quiet(monkeypatch, tmp_path):
    """夾具自己的正面對照：換完之後報告必須**只剩**結論那一行。

    底下幾支都靠「其餘各格都安靜」才分得出它們要量的那一格；夾具漏換一個探測的話，
    它們量到的會是這台機器當下的狀態，綠或紅都不代表被測的東西。
    """
    _quiet_doctor(monkeypatch, tmp_path)
    assert _run_reply(monkeypatch, lambda: b.cmd_doctor(object())) == \
        _QUIET_DOCTOR_REPLY


def test_doctor_does_not_pass_a_check_it_could_not_run(monkeypatch, tmp_path):
    """doctor 的工作是「我全部查過了」。查不成就不可以沉默地讓那一項過關。"""
    _quiet_doctor(monkeypatch, tmp_path)

    def _run(scan):
        monkeypatch.setattr(b, "_find_all_webrunner_pids", lambda *a, **k: scan)
        return _run_reply(monkeypatch, lambda: b.cmd_doctor(object()))

    clean = _run(([], True))
    broken = _run(([], False))
    assert clean != broken, "掃不成與查過了送出同一份報告"
    assert "not conclusive" in broken, broken
    assert "not conclusive" not in clean, clean
    for banned in ("webrunner", "novelai", "D:\\"):
        assert banned not in broken, banned


def test_the_doctor_tells_how_to_stop_a_manual_restart_being_vetoed(
        monkeypatch, tmp_path):
    """`restart_vetoable` 那一格：講得出原因與三個做法，而且只是 NOTE、不動 `ok`。

    2026-09-19 擁有者從開始功能表重開，被兩個常駐程式擋下、主機根本沒重開；症狀
    （「重開了但自動登入沒反應」）長得像另一個問題，所以這句要把做法一起講清楚。
    `test_process_control.test_every_recovery_gap_has_a_doctor_message` 只對帳「有沒
    有分支」，這支量的是那個分支**送出的東西**。
    """
    _quiet_doctor(monkeypatch, tmp_path)
    monkeypatch.setattr(
        b, "autostart_recovery_status",
        lambda *a, **k: {"gap": "restart_vetoable", "autologon": True,
                         "expiry": None, "auto_end_tasks": False})
    text = _run_reply(monkeypatch, lambda: b.cmd_doctor(object()))
    note = next((line for line in text.splitlines()
                 if "AutoEndTasks" in line), "")
    assert note.startswith("- NOTE "), text
    for needle in ("常駐程式", "設成 1", "仍然重新啟動", "shutdown /r /f"):
        assert needle in note, (needle, note)
    # 不動 `ok`：跟 `needs_logon`／`autologon_expires` 同級——鏈路今天是完整的。
    assert text.rstrip().endswith("- OK no obvious blocker found."), text
    # 反面對照：沒有這個缺口時一個字都不多講。
    monkeypatch.setattr(b, "autostart_recovery_status",
                        lambda *a, **k: {"gap": None})
    assert "AutoEndTasks" not in _run_reply(
        monkeypatch, lambda: b.cmd_doctor(object()))


def test_the_doctor_reply_says_how_many_findings_it_dropped():
    """截斷要看得見：只在行與行之間切、最後一行講清楚少了幾項、總長守得住上限。

    原本是 `"\\n".join(findings)[:1900]`：同時冒出很多條時，後面的發現——包括最後
    那一行結論——安靜地消失，最後一行還可能被切在字中間，讀起來像一份完整的報告。
    """
    limit = b._DOCTOR_REPLY_LIMIT
    # 放得下就一個字都不動；剛好等於上限也算放得下（邊界）。
    short = ["**doctor**", "- WARN a", "- OK b"]
    assert b._join_findings_within(short) == "\n".join(short)
    exact = ["x" * 50, "y" * 49]
    assert b._join_findings_within(exact, limit=100) == "\n".join(exact)

    findings = ["**doctor**"] + [
        f"- WARN finding {i:02d} " + "x" * 60 for i in range(60)]
    assert len("\n".join(findings)) > limit, "語料沒有超過上限，下面等於沒測"
    out = b._join_findings_within(findings)
    assert len(out) <= limit, len(out)
    *kept, tail = out.split("\n")
    assert kept == findings[:len(kept)], "只能整行截，而且要照原本的順序"
    omitted = len(findings) - len(kept)
    assert omitted >= 1
    assert tail == b._doctor_omitted_line(omitted), (
        f"註記講的數字不對：{tail!r}，實際少了 {omitted} 項")
    # 貪婪：還放得下的不可以先截掉。多收一行（註記跟著少算一項）就必須超過上限。
    one_more = "\n".join(findings[:len(kept) + 1]
                         + [b._doctor_omitted_line(omitted - 1)])
    assert len(one_more) > limit, "還放得下的那一行被截掉了"

    # 單一一行就超過上限：整行拿掉，不是切成半行。
    huge = ["**doctor**", "- WARN " + "z" * 3000, "- OK no obvious blocker found."]
    assert b._join_findings_within(huge) == \
        "**doctor**\n" + b._doctor_omitted_line(2)

    # 上限的保證不附帶前提：比註記本身還短的上限也不可以超出去。
    assert len(b._join_findings_within(findings, limit=10)) <= 10


@pytest.mark.parametrize("asker, named", [(None, False), ("owner", True), ("other", False)])
def test_doctor_names_missing_dependencies_only_to_the_owner(monkeypatch, tmp_path,
                                                             asker, named):
    """缺哪些套件：擁有者看得到名字，其他人只看得到筆數——清單裡有對話後端的 SDK 與瀏覽器
    自動化函式庫，名字本身就說明了這台機器在跑什麼。"""
    _quiet_doctor(monkeypatch, tmp_path)
    monkeypatch.setattr(b, "_missing_dependencies", lambda: ["anthropic", "selenium"])
    message = (object() if asker is None else types.SimpleNamespace(author=types.SimpleNamespace(
        id=b.OWNER_USER_ID if asker == "owner" else b.OWNER_USER_ID + 1)))
    text = _run_reply(monkeypatch, lambda: b.cmd_doctor(message))
    assert "2 declared dependenc(ies) missing" in text, text
    assert ("anthropic" in text) is named and ("selenium" in text) is named, text


def test_an_overlong_doctor_report_says_it_was_cut(monkeypatch, tmp_path):
    """同一件事在 handler 那一層量一次：接上的是那支 helper，不是另一段切片。"""
    _quiet_doctor(monkeypatch, tmp_path)
    pause = tmp_path / "pause"
    pause.write_text("", encoding="utf-8")
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", pause)
    # 一行就超過單則訊息上限的發現，而且排在最後——舊寫法會把它切在字中間。
    monkeypatch.setattr(b, "_missing_dependencies",
                        lambda: [f"dep_{i:03d}" for i in range(300)])
    # 套件名只給擁有者看，所以要擁有者問，那一行才會長到放不下。
    owner = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))
    text = _run_reply(monkeypatch, lambda: b.cmd_doctor(owner))
    assert len(text) <= 2000, len(text)
    assert "pause marker exists" in text, "截斷點之前的發現不見了"
    assert text.splitlines()[-1] == b._doctor_omitted_line(1), text[-200:]
    assert "dep_000" not in text, "放不下的那一行被切成半行送出去了"


@pytest.mark.parametrize("owner, attr, generic, affects_ok", [
    ("gui", "input_desktop_available",
     "could not check whether the desktop takes input", True),
    ("gui", "input_reaches_system",
     "could not check whether sent input reaches the system", True),
    ("gui", "held_inputs",
     "could not check for keys/buttons still held down", True),
    ("gui", "job_list", "could not list background jobs", False),
    ("bot", "_missing_dependencies",
     "could not check the declared dependencies", True),
    # 2026-09-19 從事件迴圈上搬進執行緒的兩個，外加留在迴圈上、但一樣不可以把
    # 報告帶走的佇列讀取。
    ("bot", "_free_disk_gb", "could not read the free disk space", True),
    ("bot", "_recent_log_error_count", "could not scan the recent log", True),
    ("bot", "read_todo_entries", "could not read the todo queues", True),
])
def test_one_failing_doctor_probe_does_not_take_the_report_down(
        monkeypatch, tmp_path, capsys, owner, attr, generic, affects_ok):
    """一個探測丟例外只能變成一行泛用的發現，其餘各項照常回報。

    斷言都放在**可觀察的輸出**上：`cmd_doctor` 現在有一道寬的 `except`，所以
    「沒有例外逸出」什麼都證明不了——替身根本沒被叫到也會是那樣。這裡要求：
    替身真的被叫到（stderr 有它的原始訊息）、失敗那一格有自己的泛用句、它前後的
    發現都還在、原始例外文字沒有進回覆，以及 `ok` 的方向跟那一格平常一致。
    """
    marker = "boom-4242"
    calls = []

    def _boom(*_a, **_k):
        calls.append(attr)
        raise RuntimeError(f"D:\\secret\\host {marker}")

    _quiet_doctor(monkeypatch, tmp_path)
    monkeypatch.setattr(b._gui if owner == "gui" else b, attr, _boom)

    # 1. `ok` 的方向：其餘各格都安靜時，只有這一格決定結論那一行在不在。
    alone = _run_reply(monkeypatch, lambda: b.cmd_doctor(object()))
    assert calls, f"替身沒被叫到——{attr} 沒有經過被測的那條路"
    assert generic in alone, alone
    assert ("OK no obvious blocker found" in alone) is (not affects_ok), (
        f"{attr} 查不成時的結論方向不對：{alone!r}")
    assert marker in capsys.readouterr().err, "原始例外應該寫進 stderr"

    # 2. 報告的其餘部分：失敗那一格前後各放一個發現，兩個都要在。
    pause = tmp_path / "pause"
    pause.write_text("", encoding="utf-8")
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", pause)
    monkeypatch.setattr(b, "_MACRO_RECORDING", object())
    text = _run_reply(monkeypatch, lambda: b.cmd_doctor(object()))
    assert text.startswith("**doctor**\n"), text
    assert "pause marker exists" in text, "失敗那一格之前的發現不見了"
    assert "macro recording is still capturing input" in text, (
        "失敗那一格之後的發現不見了——報告在那裡就斷了")
    assert generic in text, text
    for banned in (marker, "secret", "RuntimeError", "D:\\"):
        assert banned not in text, f"原始例外文字進了回覆：{banned!r}"


def test_the_doctor_docstring_admits_its_key_press():
    """`cmd_doctor` 的 docstring 原本寫「Non-destructive」，而它在桌面解鎖時會真的
    送一個 F13 到前景視窗——`_gui_control.input_reaches_system` 有寫，這裡沒寫。

    綁在呼叫上而不是綁在字串上：doctor 還在叫那個探測，docstring 就得講出按的是
    哪個鍵；哪天拿掉了，docstring 也不該再這樣講。鍵名從 `_gui_control` 那支函式的
    docstring 抽，不在這裡寫死——那是這個專案對「送的是哪個鍵」唯一的紀錄。
    """
    import ast as _ast
    import re as _re

    here = Path(b.__file__).resolve().parent
    doctor = next(
        n for n in _ast.walk(_ast.parse(
            (here / "discord_bot.py").read_text(encoding="utf-8")))
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        and n.name == "cmd_doctor")
    probe = next(
        n for n in _ast.parse(
            (here / "_gui_control.py").read_text(encoding="utf-8")).body
        if isinstance(n, _ast.FunctionDef) and n.name == "input_reaches_system")
    keys = _re.findall(r"\bF\d{1,2}\b", _ast.get_docstring(probe) or "")
    assert keys, "`_gui_control.input_reaches_system` 的 docstring 沒寫送哪個鍵了"

    calls_probe = any(
        isinstance(n, _ast.Attribute) and n.attr == "input_reaches_system"
        and isinstance(n.value, _ast.Name) and n.value.id == "_gui"
        for n in _ast.walk(doctor))
    doc = _ast.get_docstring(doctor) or ""
    assert (keys[0] in doc) is calls_probe, (
        f"doctor {'會' if calls_probe else '不會'}送 {keys[0]}，docstring 卻"
        f"{'沒講' if calls_probe else '還在講'}。")
    assert "non-destructive" not in doc.lower(), (
        "docstring 又說自己沒有副作用了——它會按一個真的鍵。")


def _where_called() -> str:
    """呼叫當下在哪一條執行緒上：有正在跑的事件迴圈 ＝ `"loop"`，否則 `"thread"`。

    跟 `test_the_output_scan_does_not_run_on_the_event_loop` 同一個判斷法。
    """
    import asyncio as _asyncio
    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        return "thread"
    return "loop"


def _spy(label, seen, result):
    """回傳固定值的替身，順便把「在哪條執行緒上被叫」記進 `seen[label]`。

    記成集合而不是單一值：同一個東西一半在迴圈上、一半在執行緒上被叫，也要看得出來。
    """
    def fn(*_a, **_k):
        seen.setdefault(label, set()).add(_where_called())
        return result
    return fn


class _SpyPath:
    """只回答 `exists()` 的假路徑；被問的時候記下是在哪條執行緒上。"""

    def __init__(self, label, seen):
        self.label, self.seen = label, seen

    def exists(self):
        self.seen.setdefault(self.label, set()).add(_where_called())
        return False


def test_doctor_reads_what_the_bot_replaces_on_the_loop_and_the_rest_off_it(
        monkeypatch, tmp_path):
    """`cmd_doctor` 的磁碟檢查分兩邊，判準是「bot 自己會不會在迴圈上用 `os.replace`
    換掉那個檔」（2026-09-19）。

    會的——暫停標記、單張請求檔、四個佇列檔——必須留在迴圈上：丟到執行緒，連一個
    `exists()` 都可能讓 bot 自己同一瞬間的存檔失敗（本機實測：一條執行緒不斷 `stat`
    時 `os.replace` 約 7% 失敗，對照組 0/3000）。不會的——剩餘空間、整份 log——必須
    丟執行緒：log 約 670 KB，是 doctor 最貴的一次讀取。

    兩個方向都要測。只測「log 不在迴圈上」的話，把佇列一起丟進執行緒（這個待辦最初
    就是這樣寫的）也會全綠；只測「佇列在迴圈上」的話，把整份 log 搬回迴圈也會綠。
    """
    _quiet_doctor(monkeypatch, tmp_path)
    seen: dict[str, set] = {}
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", _SpyPath("pause", seen))
    monkeypatch.setattr(b, "read_todo_entries", _spy("todo", seen, ["entry"]))
    monkeypatch.setattr(b, "_single_image_request_pending_on_disk",
                        _spy("request", seen, False))
    monkeypatch.setattr(b, "_free_disk_gb", _spy("disk free", seen, 999.0))
    monkeypatch.setattr(b, "_recent_log_error_count", _spy("log", seen, None))

    text = _run_reply(monkeypatch, lambda: b.cmd_doctor(object()))
    assert text == _QUIET_DOCTOR_REPLY, "替身回的全是「沒事」，報告卻不安靜"
    assert seen == {
        "pause": {"loop"}, "todo": {"loop"}, "request": {"loop"},
        "disk free": {"thread"}, "log": {"thread"},
    }, seen


def test_doctor_does_not_read_an_unknown_free_space_as_enough(monkeypatch, tmp_path):
    """讀不到剩餘空間是「查不成」，不是「空間夠」。

    `_free_disk_gb()` 讀不到時回 None，`/run` 把 None 當成「不擋」是對的——那是在
    決定要不要開跑。doctor 的工作卻是「我全部查過了」，所以查不成要講出來、結論那
    一行不可以出現（跟 `orphan_scan_ok` 同一條規則）。三個方向都要釘：門檻關掉時
    整格不跑、真的低於門檻時仍然是原本那一句（別把「空間不夠」也講成「查不成」）。
    """
    _quiet_doctor(monkeypatch, tmp_path)
    monkeypatch.setattr(b, "_free_disk_gb", lambda: None)
    unknown = _run_reply(monkeypatch, lambda: b.cmd_doctor(object()))
    assert "could not read the free disk space" in unknown, unknown
    assert "OK no obvious blocker found" not in unknown, unknown

    monkeypatch.setattr(b, "MIN_FREE_DISK_GB", 0.0)
    assert _run_reply(monkeypatch, lambda: b.cmd_doctor(object())) == \
        _QUIET_DOCTOR_REPLY, "門檻是 0（停用）時這一格不該有任何話"

    monkeypatch.setattr(b, "MIN_FREE_DISK_GB", 5.0)
    monkeypatch.setattr(b, "_free_disk_gb", lambda: 1.0)
    low = _run_reply(monkeypatch, lambda: b.cmd_doctor(object()))
    assert "below threshold" in low, low
    assert "could not read" not in low, low


def _isolate_full_health(monkeypatch, seen):
    """把 `cmd_health`（完整版）每一段的輸入都釘死；會碰磁碟的地方換成會記錄
    執行緒的替身。

    釘死是為了讓兩次 render 可以逐行比較：uptime、RSS、log 大小在兩次之間都會動
    （正式批次一直在寫 log），不釘的話「截斷點之前的行都沒被動到」就量不出來。
    """
    monkeypatch.setattr(b, "_webrunner_liveness", lambda: (False, True))
    monkeypatch.setattr(b, "_find_all_webrunner_pids", lambda *a, **k: ([], True))
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    monkeypatch.setattr(b, "_format_duration", lambda *_a, **_k: "1s")
    monkeypatch.setattr(b, "_fmt_size", lambda *_a, **_k: "1 B")
    for name in ("WEBRUNNER_LOG", "EVENTS_FILE", "AUDIT_FILE", "FAVORITES_FILE",
                 "RECENT_IMAGE_MSGS_FILE"):
        monkeypatch.setattr(b, name, _SpyPath(name, seen))
    monkeypatch.setattr(b, "_load_favorites", _spy("favorites", seen, {}))
    monkeypatch.setattr(b, "_health_disk_bits", _spy("disk", seen, ["`x`=`1 B`"]))
    monkeypatch.setattr(b, "_health_backup_count", _spy("backup", seen, 3))
    monkeypatch.setattr(b, "_health_log_scan", _spy("log", seen, (0, 0, 0)))
    monkeypatch.setattr(b, "_presence_probe_task", None)
    monkeypatch.setattr(b, "_local_probed_activity", None)


def test_health_reads_what_the_bot_replaces_on_the_loop_and_walks_off_it(
        monkeypatch):
    """`cmd_health` 跟 doctor 同一條判準（2026-09-19）。

    留在迴圈上：五個被問大小的檔（每一個都會被 bot 自己在迴圈上 `os.replace`
    掉——log 的 spawn 輪替、兩份 ndjson 輪替、收藏的 `_safe_write`、圖片訊息快取的
    原子寫入）以及收藏的內容讀取。丟執行緒：三棵目錄樹的遞迴走訪（實測約 50 ms，
    原本是 health 裡最大的一塊迴圈阻塞）、備份計數、整份 log 的掃描。
    """
    seen: dict[str, set] = {}
    _isolate_full_health(monkeypatch, seen)
    text = _run_reply(monkeypatch, lambda: b.cmd_health(object()))
    assert "- **disk**: `x`=`1 B`" in text and "3 `.bak` files" in text, (
        f"替身沒有接上，下面等於沒測：{text!r}")
    loop_side = ("WEBRUNNER_LOG", "EVENTS_FILE", "AUDIT_FILE", "FAVORITES_FILE",
                 "RECENT_IMAGE_MSGS_FILE", "favorites")
    thread_side = ("disk", "backup", "log")
    assert seen == {**{k: {"loop"} for k in loop_side},
                    **{k: {"thread"} for k in thread_side}}, seen


class _RecordedPath:
    """一個真的路徑，外加「每一次被碰都記下是在哪條執行緒上」。

    `_SpyPath` 只回答 `exists()`、而且永遠說不存在，所以只驗得到「問存不存在」那一步；
    這一支把呼叫轉給真的 `Path`，handler 會照常跑完——讀到真的內容、回覆裡看得到那些
    內容（那就是正面對照：替身有沒有接上，看回覆就知道）——同時把讀檔、`stat`、走訪
    （`rglob`，以及 `os.walk` 走的 `__fspath__`）各發生在哪條執行緒上記進 `seen`。
    """

    def __init__(self, label, seen, real):
        self._label, self._seen, self._real = label, seen, Path(real)

    def _note(self):
        self._seen.setdefault(self._label, set()).add(_where_called())

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            self._note()
            return attr(*a, **k)
        return call

    def __fspath__(self):
        self._note()
        return os.fspath(self._real)

    def __str__(self):
        return str(self._real)


# `/log` 那四個指令，與各自在正常情況下回覆裡一定看得到的字（正面對照）。
_LOG_COMMANDS = (
    ("tail", lambda msg: b.cmd_tail(msg, "5"), "needle here"),
    ("grep", lambda msg: b.cmd_log_grep(msg, "NEEDLE"), "needle here"),
    ("errors", lambda msg: b.cmd_errors(msg), "Traceback boom"),
    ("size", lambda msg: b.cmd_log_size(msg), "3 lines"),
)


def test_the_log_commands_read_the_log_off_the_loop(monkeypatch, tmp_path):
    """`/log tail|grep|errors|size` 讀整份 log，一律丟工作執行緒（2026-09-19）。

    這個檔可以丟：同一個行程裡唯一會 `os.replace` 它的輪替，改名失敗本來就退回
    「複製＋原地截斷」（`test_a_reader_holding_the_log_does_not_break_the_rotation`）。
    這四個指令**沒有任何東西必須留在迴圈上**——它們不碰 bot 自己換的檔——所以另一個
    方向寫成「整張表只准有 log、而且只在執行緒上」：把事件檔、暫停標記、pid 檔換成
    會記錄的替身，任何一個被順手拖進搬走的 helper 都會讓表多出一格。

    三種情況都跑：讀得到、還不存在、存在卻讀不出來（目錄佔住那個名字）。後兩種走
    的是 `to_thread` 把例外帶回迴圈的那條路；讀不出來時非擁有者只能拿到泛用句。
    """
    log = tmp_path / "webrunner.log"
    log.write_text("ok one\nneedle here\nTraceback boom\n", encoding="utf-8")
    missing = tmp_path / "not-yet.log"
    blocked = tmp_path / "blocked.log"
    blocked.mkdir()

    for name, run, expected in _LOG_COMMANDS:
        for real, want in ((log, expected), (missing, "尚不存在"),
                           (blocked, "讀取 log 失敗")):
            seen: dict[str, set] = {}
            monkeypatch.setattr(b, "WEBRUNNER_LOG", _RecordedPath("log", seen, real))
            for other in ("EVENTS_FILE", "WEBRUNNER_PAUSE_FILE", "WEBRUNNER_PID_FILE"):
                monkeypatch.setattr(b, other, _SpyPath(other, seen))
            text = _run_reply(monkeypatch, lambda: run(object()))
            assert want in text, f"/log {name} on {real.name}: {text!r}"
            assert "Error" not in text and str(tmp_path) not in text, (
                f"/log {name} 把原始例外或主機路徑送給非擁有者了：{text!r}")
            assert seen == {"log": {"thread"}}, (name, real.name, seen)


def test_the_disk_report_walks_off_the_loop(monkeypatch, tmp_path):
    """`/sys disk` 的兩棵樹走訪丟工作執行緒（2026-09-19，原本整段在迴圈上，約 50 ms）。

    bot 不在產出資料夾或瀏覽器設定檔裡 `os.replace` 任何東西（它自己換的檔全在 repo
    根目錄），所以這個指令也**沒有東西必須留在迴圈上**。另一個方向同樣寫成「整張表
    只准有這兩棵樹、而且只在執行緒上」。

    回覆裡的數字是正面對照：產出那棵真的放了 2 MiB，設定檔那棵不存在。
    """
    seen: dict[str, set] = {}
    output = tmp_path / "output" / "somebody"
    output.mkdir(parents=True)
    (output / "a.png").write_bytes(b"x" * (2 * 1024 * 1024))
    monkeypatch.setattr(b, "OUTPUT_ROOT",
                        _RecordedPath("output", seen, tmp_path / "output"))
    monkeypatch.setattr(b, "CHROME_PROFILE_DIR",
                        _RecordedPath("profile", seen, tmp_path / "no-profile"))
    for other in ("EVENTS_FILE", "WEBRUNNER_PAUSE_FILE", "WEBRUNNER_PID_FILE"):
        monkeypatch.setattr(b, other, _SpyPath(other, seen))

    text = _run_reply(monkeypatch, lambda: b.cmd_disk(object()))
    assert "2.00 MB" in text and "(missing)" in text, (
        f"替身沒有接上，下面等於沒測：{text!r}")
    assert seen == {"output": {"thread"}, "profile": {"thread"}}, seen


def test_the_health_log_scan_counts_the_tail_and_tells_missing_from_unreadable(
        monkeypatch, tmp_path):
    """`_health_log_scan` 與 `_recent_log_error_count` 同一個讀取合約（2026-09-19 兩支都
    改走 `_webrunner_log_lines`）：只看最後 `RECENT_LOG_SCAN_LINES` 行、不存在回 None、
    存在卻讀不出來往外丟。原本只有替身在測它，本體一行都沒被執行過。"""
    log = tmp_path / "webrunner.log"
    monkeypatch.setattr(b, "WEBRUNNER_LOG", log)
    assert b._health_log_scan() is None
    lines = (["WARN old", "CRITICAL old"] * 3 + ["ok"] * b.RECENT_LOG_SCAN_LINES
             + ["WARN new", "fill verify mismatch here", "Traceback x", "ok"])
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert b._health_log_scan() == (1, 1, 1)
    log.unlink()
    log.mkdir()
    with pytest.raises(OSError):
        b._health_log_scan()


def test_the_api_credentials_hint_says_it_does_not_reach_the_cli_backend(capsys):
    """api 後端缺憑證時那一行 stderr 要講清楚：那兩個變數**只給 api 後端**，CLI 後端
    的子行程刻意拿不到（`dorossi_backend._DOROSSI_CC_DROPPED_ENV`，2026-09-19）。

    只講「去設 ANTHROPIC_API_KEY」會讓人以為兩個後端都吃得到。對外回覆照舊是泛用句；
    那一行要能在 cp950 主控台印出來（管線上印不出來是整個行程死掉，不是亂碼）。
    """
    reply = b._dorossi_error_hint("api", RuntimeError("missing api_key"))
    err = capsys.readouterr().err
    line = next(ln for ln in err.splitlines() if "no usable credentials" in ln)
    assert "ANTHROPIC_API_KEY" in line and "api backend only" in line, line
    assert "does not receive" in line and "_DOROSSI_CC_DROPPED_ENV" in line, line
    line.encode("cp950")
    assert reply == "Dorossi 暫時無法回應，請稍後再試。", reply
    assert set(db._DOROSSI_CC_DROPPED_ENV) >= {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}, (
        "後端不再拿掉這兩個變數了，那這一行 stderr 說的就不是事實")


_MIB = 1024 * 1024


def test_the_upload_limit_prefers_what_the_server_reports():
    """`_upload_limit_bytes`：斜線看互動回報的數字、伺服器取 max、私訊用預設（2026-09-19）。

    預設原本寫 8 MB，平台 2026-09-03 已調到 20 MiB；函式庫的 `Guild.filesize_limit`
    對沒加成的伺服器給的是它自己寫死的 10 MiB，也是舊的——所以伺服器那一條要取 max，
    而互動回報的數字（伺服器算好、含加成）照單全收，比預設小也一樣。
    """
    NS = types.SimpleNamespace

    def proxy(reported, guild_limit=10 * _MIB):
        return b._InteractionMessageProxy(NS(
            user=NS(id=1), channel=NS(id=2), guild=NS(filesize_limit=guild_limit),
            id=3, filesize_limit=reported))

    assert b.DISCORD_FILE_LIMIT == 20 * _MIB
    assert b._upload_limit_bytes(proxy(25 * _MIB)) == 25 * _MIB
    assert b._upload_limit_bytes(proxy(8 * _MIB)) == 8 * _MIB, (
        "伺服器回報的數字比預設小也要照用——它才是權威")
    assert b._upload_limit_bytes(proxy(None)) == 20 * _MIB, "沒回報時退到伺服器那一條"
    assert b._upload_limit_bytes(proxy(True)) == 20 * _MIB, "bool 不是上限"
    stale_guild = NS(guild=NS(filesize_limit=10 * _MIB))
    assert b._upload_limit_bytes(stale_guild) == 20 * _MIB, (
        "函式庫寫死的 10 MiB 比平台現在的預設舊，要取 max")
    boosted = NS(guild=NS(filesize_limit=50 * _MIB))
    assert b._upload_limit_bytes(boosted) == 50 * _MIB
    assert b._upload_limit_bytes(NS(guild=None)) == 20 * _MIB, "私訊"
    assert b._upload_limit_bytes(object()) == 20 * _MIB, "什麼都取不到"
    # 一般訊息也有 `_interaction`（那是 MessageInteraction，沒有 filesize_limit）——
    # 不可以因為「有這個屬性」就被當成斜線代理而拿不到伺服器那一條。
    real_message_shape = NS(_interaction=NS(name="x"),
                            guild=NS(filesize_limit=50 * _MIB))
    assert b._upload_limit_bytes(real_message_shape) == 50 * _MIB


def test_a_file_between_the_old_and_new_limit_is_uploaded_and_the_cap_text_is_real(
        monkeypatch, tmp_path):
    """處理器那一層：12 MiB 的圖原本會被「超過 8 MB」擋掉，現在要真的上傳；超過上限時
    訊息裡的數字要是實際採用的上限，不是寫死的 8。"""
    folder = tmp_path / "output" / "alice"
    folder.mkdir(parents=True)
    monkeypatch.setattr(b, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(b, "_remember_image_msg", lambda *_a, **_k: None)
    picture = folder / "a.png"

    def run(size, guild_limit):
        with open(picture, "wb") as f:      # 稀疏檔，不必真的寫這麼多位元組
            f.truncate(size)
        box: dict = {}

        async def _recorder(_message, content=None, **kw):
            box["content"], box["file"] = content, kw.get("file")
            if box["file"] is not None:
                box["file"].close()

        monkeypatch.setattr(b, "safe_reply", _recorder)
        message = types.SimpleNamespace(guild=types.SimpleNamespace(
            filesize_limit=guild_limit))
        asyncio.run(b.cmd_latest_for(message, "alice"))
        return box

    sent = run(12 * _MIB, 10 * _MIB)
    assert sent["file"] is not None, f"12 MiB 在 20 MiB 預設之下卻沒上傳：{sent}"
    refused = run(25 * _MIB, 10 * _MIB)
    assert refused["file"] is None and "> 20 MB cap" in refused["content"], refused
    boosted = run(25 * _MIB, 50 * _MIB)
    assert boosted["file"] is not None, f"加成伺服器的 50 MiB 上限沒被採用：{boosted}"


def test_every_upload_site_asks_the_helper_for_its_limit():
    """`DISCORD_FILE_LIMIT` 只准在 `_upload_limit_bytes` 裡被讀（2026-09-19）。

    行為測試只走得到 `/out latest_for`；另外六個會上傳檔案的地方（`/latest`、
    `/out debug_show`、`/out sample`、`/fav show`、Dorossi 的圖、單張產圖的回報）若有
    一處直接比對常數，就會在加成伺服器上把傳得上去的圖擋掉，而且沒有任何測試會紅。
    """
    import ast as _ast

    tree = _ast.parse((Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py")
                      .read_text(encoding="utf-8"))
    readers: list[str] = []
    helper_calls = 0
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for node in _ast.walk(fn):
            if isinstance(node, _ast.Name) and node.id == "DISCORD_FILE_LIMIT":
                readers.append(fn.name)
            if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name)
                    and node.func.id == "_upload_limit_bytes"):
                helper_calls += 1
    assert helper_calls >= 7, f"只掃到 {helper_calls} 個呼叫——擷取器壞了或有處理器不再問它"
    assert set(readers) == {"_upload_limit_bytes"}, (
        f"這些函式直接讀了 `DISCORD_FILE_LIMIT`：{sorted(set(readers) - {'_upload_limit_bytes'})}"
        "——改成 `_upload_limit_bytes(message)`，伺服器回報的上限才會生效。")


def test_dir_size_matches_the_old_rglob_total_without_opening_a_single_file(
        monkeypatch, tmp_path):
    """`_dir_size` 改成 `os.scandir` 走訪（2026-09-19），兩件事一起釘住。

    1. **總數跟舊的 `rglob("*")` ＋ `is_file()` ＋ `stat()` 一樣**——同一棵真的樹上
       兩種算法並排比，樹裡有隱藏檔、空目錄、巢狀目錄，以及（Windows 上）一個
       junction：舊寫法會走進 junction，新寫法也要。
    2. **走訪期間一個檔都不開。** 舊寫法對每個檔 `os.stat` 一次，那是一個會擋住
       別人 `os.replace` 的 handle——批次同步登入狀態時就是在這棵樹裡換檔。這裡把
       `Path.stat`／`Path.lstat`／`Path.is_file`／`os.stat`／`os.lstat` 全換成「被叫到就
       記下來並丟 `AssertionError`」的替身：丟的不是 `OSError`，所以走訪裡的
       `except OSError` 吞不掉它。替身有沒有真的裝上，先用正面對照確認。
    """
    import subprocess

    root = tmp_path / "tree"
    (root / "a" / "b").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "one.bin").write_bytes(b"1" * 1000)
    (root / ".hidden").write_bytes(b"h" * 7)
    (root / "a" / "two.bin").write_bytes(b"2" * 2000)
    (root / "a" / "b" / "three.bin").write_bytes(b"3" * 3000)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "far.bin").write_bytes(b"f" * 50_000)
    junction = root / "junction"
    made_junction = os.name == "nt" and subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, text=True, encoding="oem", errors="replace",
    ).returncode == 0
    try:
        old = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        assert old == 6007 + (50_000 if made_junction else 0), old
        assert b._dir_size(root) == old

        calls: list[str] = []

        def _refuse(name):
            def fn(*_a, **_k):
                calls.append(name)
                raise AssertionError(f"{name} 被叫了——走訪開了檔")
            return fn

        with monkeypatch.context() as m:
            for owner, name in ((Path, "stat"), (Path, "lstat"), (Path, "is_file"),
                                (os, "stat"), (os, "lstat")):
                m.setattr(owner, name, _refuse(f"{owner.__name__}.{name}"))
            for probe in (lambda: root.stat(), lambda: root.is_file(),
                          lambda: os.stat(root)):
                with pytest.raises(AssertionError):
                    probe()
            assert len(calls) == 3, f"替身沒有裝上，下面等於沒測：{calls}"
            calls.clear()
            total = b._dir_size(root)
            missing = b._dir_size(tmp_path / "gone")
        assert calls == [], calls
        assert total == old and missing == 0, (total, missing)
    finally:
        if made_junction:
            os.rmdir(junction)


def test_health_quick_reads_the_events_file_on_the_loop_and_walks_off_it(
        monkeypatch, tmp_path):
    """健康報告那幾行的磁碟 I/O，兩個方向一起斷言（2026-09-19）。

    原本產量那一行的兩件 I/O 包在一起丟執行緒，其中一件是讀事件檔——而那個檔會被
    `_spawn_webrunner` 在迴圈上用 `os.replace` 輪替掉。執行緒裡開著讀的 handle（連
    `stat` 都算）會讓那次輪替失敗，正是判準反過來的那種風險，而且當時沒有守門。

    留在迴圈上：事件檔、暫停標記、pid 檔（都是 bot 自己換的）。丟執行緒：剩餘空間、
    整份 log、產出樹走訪。只測一邊的話，把事件檔一起丟回執行緒、或把產出樹搬回迴圈，
    各有一個會全綠。

    正面對照：事件檔裡放一則還沒醒的休息、產出樹放三張 24 小時內的圖，兩件都要出現
    在回覆裡——替身沒接上的話，兩個執行緒集合都會是空的，而空的 `seen` 看起來像乾淨。
    """
    _isolate_health(monkeypatch, tmp_path)
    seen: dict[str, set] = {}
    now = time.time()
    events = tmp_path / "events.ndjson"
    events.write_text(
        '{"type": "schedule_rest", "wake_ts": ' + repr(now + 4 * 3600.0) + '}\n',
        encoding="utf-8")
    root = _fake_output_tree(tmp_path / "output", [1.0, 2.0, 3.0], now=now)
    monkeypatch.setattr(b, "EVENTS_FILE", _RecordedPath("events", seen, events))
    monkeypatch.setattr(b, "OUTPUT_ROOT", _RecordedPath("output", seen, root))
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", _SpyPath("pause", seen))
    monkeypatch.setattr(b, "_webrunner_alive", _spy("pid", seen, True))
    monkeypatch.setattr(b, "_free_disk_gb", _spy("disk free", seen, 42.0))
    monkeypatch.setattr(b, "_recent_log_error_count", _spy("log", seen, 0))

    text = asyncio.run(b._health_quick_text())
    assert "- images produced (24h): `3` (scheduled rest," in text, text
    assert "`42.0 GB`" in text, text
    assert seen == {
        "events": {"loop"}, "pause": {"loop"}, "pid": {"loop"},
        "output": {"thread"}, "disk free": {"thread"}, "log": {"thread"},
    }, seen


class _NamedActivity:
    def __init__(self, name):
        self.name = name


def test_an_overlong_health_report_is_cut_on_a_line_and_says_so(monkeypatch):
    """`/sys health` 原本是 `text[:1900] + "\\n…"`：切在行中間、不說少了什麼。

    現在走 doctor 那支 `_join_findings_within`：只在行與行之間切，最後一行用
    health 自己的句子講清楚少了幾項。會變長的是最後那一段——presence 那一行內插
    的是偵測到的活動名稱，長度不由 bot 決定。

    比兩次 render：短名稱那次是完整報告，長名稱那次必須是「完整報告去掉最後一行，
    再加上註記」——截斷點之前的每一行一個字都不能動。
    """
    seen: dict[str, set] = {}
    _isolate_full_health(monkeypatch, seen)
    full = _run_reply(monkeypatch, lambda: b.cmd_health(object()))
    assert len(full) <= b._DOCTOR_REPLY_LIMIT, "語料本身就放不下，下面等於沒測"
    assert full.split("\n")[-1].startswith("- **presence probe**"), full[-200:]

    monkeypatch.setattr(b, "_local_probed_activity", _NamedActivity("z" * 3000))
    cut = _run_reply(monkeypatch, lambda: b.cmd_health(object()))
    assert len(cut) <= b._DOCTOR_REPLY_LIMIT < 2000, len(cut)
    *kept, tail = cut.split("\n")
    assert kept == full.split("\n")[:-1], "截斷點之前的行被動到了"
    assert tail == b._health_omitted_line(1), (
        f"最後一行不是 health 的截斷註記（或數字不對）：{tail!r}")
    assert "zzz" not in cut, "放不下的那一行被切成半行送出去了"


def test_the_recent_log_count_reads_only_the_tail_and_tells_missing_from_unreadable(
        monkeypatch, tmp_path):
    """`_recent_log_error_count` 是 doctor、健康報告、失敗摘要三處共用的計數。

    三件事：只算最後 `RECENT_LOG_SCAN_LINES` 行；檔案不存在（還沒跑過）回 None；
    存在卻讀不出來**往外丟**——三個呼叫端對「查不成」各有說法（doctor 講出來、
    健康報告寫 `?`），吞成 None 的話它們全都會把查不成講成還沒跑過。
    """
    log = tmp_path / "webrunner.log"
    monkeypatch.setattr(b, "WEBRUNNER_LOG", log)
    assert b._recent_log_error_count() is None

    # 開頭五行 WARN 在視窗外、只有倒數第二行在視窗內：算頭而不是算尾會得到 5。
    lines = (["WARN old"] * 5 + ["ok"] * b.RECENT_LOG_SCAN_LINES
             + ["Traceback new", "ok"])
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert b._recent_log_error_count() == 1

    log.unlink()
    log.mkdir()          # 存在、但讀不出來（Windows 上是 PermissionError）
    with pytest.raises(OSError):
        b._recent_log_error_count()


def _thread_offloaded_names(tree) -> list[tuple[str, int]]:
    """丟進工作執行緒的呼叫裡，碰到 session 存放區的名字：`[(名字, 行號), …]`。

    「丟進執行緒」的判準：`asyncio.to_thread` / `run_in_executor` /
    `threading.Thread`，外加**模組裡任何把自己的參數交給 `to_thread` 的函式**
    （`_doctor_probe` 就是）——這一類用推的，不列名字，下一支同形狀的包裝會自動
    進來。碰到的判準取遞移閉包：直接叫存放區函式的函式也算。
    """
    import ast as _ast

    touching = {"_dorossi_load_state", "_dorossi_save_state",
                "_dorossi_state_rmw"}
    fns = {n.name: n for n in _ast.walk(tree)
           if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}

    def _names(node):
        return {n.id for n in _ast.walk(node) if isinstance(n, _ast.Name)}

    changed = True
    while changed:
        changed = False
        for name, fn in fns.items():
            if name not in touching and _names(fn) & touching:
                touching.add(name)
                changed = True

    offload = {"to_thread", "run_in_executor", "Thread"}
    for name, fn in fns.items():
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        for call in _ast.walk(fn):
            if (isinstance(call, _ast.Call)
                    and isinstance(call.func, _ast.Attribute)
                    and call.func.attr == "to_thread"
                    and any(isinstance(a, _ast.Name) and a.id in params
                            for a in call.args)):
                offload.add(name)

    hits = []
    for call in _ast.walk(tree):
        if not isinstance(call, _ast.Call):
            continue
        func = call.func
        callee = (func.attr if isinstance(func, _ast.Attribute)
                  else func.id if isinstance(func, _ast.Name) else None)
        if callee not in offload:
            continue
        for arg in list(call.args) + [k.value for k in call.keywords]:
            for name in sorted(_names(arg) & touching):
                hits.append((name, call.lineno))
    return hits


def test_the_session_store_is_never_touched_from_a_worker_thread():
    """`dorossi_session.json` 的讀寫只能同步跑在事件迴圈上（2026-09-19）。

    `/sys doctor` 的待辦要把 `_dorossi_load_state()` 丟進 `asyncio.to_thread`，查下去
    發現**不能**：這台機器上 `os.replace` 碰到「目的檔正被另一個 handle 開著讀」會
    `PermissionError`（下一支釘住這個前提），而 `_dorossi_save_state` 把那個錯吞成
    一行 stderr——那一次存檔安靜地不見，例如某一輪剛推進的 `cc_session_id`。今天每一
    次讀寫都同步跑在迴圈上，所以兩者不可能交錯；任何一個丟進執行緒的讀者都會打開
    那段空檔。讀到毀損檔時的隔離搬檔也一樣，搬走的可能是剛存好的新檔。
    """
    import ast as _ast

    tree = _ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    hits = _thread_offloaded_names(tree)
    assert not hits, (
        "這幾處把 session 存放區的讀寫丟進了工作執行緒：%s。那會跟事件迴圈上的"
        "`_dorossi_save_state` 交錯，讓存檔在 Windows 上安靜地失敗。改回在迴圈上"
        "同步呼叫（檔案只有幾 KB）。" % hits)

    # 正面對照：推導本身要咬得到——直接丟、包一層 helper 丟、經自訂的轉手函式丟、
    # 用 lambda 丟，四種都要看得到；同步呼叫則不算。
    synthetic = _ast.parse(
        "import asyncio\n"
        "async def probe(label, fn):\n"
        "    return await asyncio.to_thread(fn)\n"
        "def helper():\n"
        "    return _dorossi_load_state()\n"
        "async def a():\n"
        "    await asyncio.to_thread(_dorossi_load_state)\n"
        "async def c():\n"
        "    await asyncio.to_thread(helper)\n"
        "async def d():\n"
        "    await probe('x', helper)\n"
        "async def e():\n"
        "    await asyncio.to_thread(lambda: _dorossi_save_state({}))\n"
        "async def fine():\n"
        "    _dorossi_load_state()\n"
        "    await asyncio.to_thread(len, [])\n")
    got = {(name, line) for name, line in _thread_offloaded_names(synthetic)}
    assert got == {("_dorossi_load_state", 7), ("helper", 9), ("helper", 11),
                   ("_dorossi_save_state", 13)}, sorted(got)


@pytest.mark.skipif(os.name != "nt", reason="這個前提講的是 Windows 的檔案共用模式")
def test_replacing_a_file_open_for_reading_fails_on_this_platform(tmp_path):
    """上一支的前提：Python 開著讀的檔，別人不能 `os.replace` 蓋掉它。

    這一支哪天變綠以外的顏色（例如 Python 開檔時開始帶 FILE_SHARE_DELETE），代表
    「讀者會擋住存檔」那半個理由沒了——但毀損檔的隔離搬檔那半還在，所以不要直接把
    上一支拿掉，先重新想一次。
    """
    dst = tmp_path / "state.json"
    staged = tmp_path / "state.json.tmp"
    dst.write_text("old", encoding="utf-8")
    staged.write_text("new", encoding="utf-8")
    with open(dst, "r", encoding="utf-8"):
        with pytest.raises(PermissionError):
            os.replace(staged, dst)
    os.replace(staged, dst)
    assert dst.read_text(encoding="utf-8") == "new"


def test_scheduled_rest_is_not_mistaken_for_a_stall():
    """計畫中的休息不可以被當成卡死，被砍掉的休息也不可以被當成還在休息。

    產圖程式跑滿 `schedule_limit_hours` 會刻意睡 `rest_hours`（預設 16h / 6h
    ＝**每 22 小時有 27% 的時間**是計畫性閒置）。`/rate` 的「超過一小時沒有新
    圖」在那整段時間都會亮，也就是超過四分之一的時間在謊報異常。

    反方向同樣重要：如果只看「有 `schedule_rest`、沒有 `schedule_resumed`」，
    那麼產圖程式在休息途中被砍掉（`schedule_resumed` 永遠不會寫進去）就會讓
    bot 永久停在「休息中」——假警報換成真異常被消音，比原本更糟。所以
    `wake_ts` 過期就必須恢復報警。
    """
    now = 1_800_000_000.0
    def ev(t, ts, **kw):
        return dict(type=t, ts=ts, **kw)

    # 1. 休息中（wake_ts 還在未來）→ 回醒來時間。
    resting = [ev("character_done", now - 100),
               ev("schedule_rest", now - 100, wake_ts=now + 3600)]
    _eq(b._scheduled_rest_until(resting, now=now), now + 3600, "休息中")

    # 2. 休息已結束 → None，警告恢復正常。
    done = resting + [ev("schedule_resumed", now - 10, rested_sec=90)]
    _eq(b._scheduled_rest_until(done, now=now), None, "休息結束")

    # 3. 有 schedule_rest、沒有 schedule_resumed，但 wake_ts 已經過期
    #    （＝休息途中被砍掉）→ None，不能永久消音。
    stale = [ev("schedule_rest", now - 99999, wake_ts=now - 60)]
    _eq(b._scheduled_rest_until(stale, now=now), None, "過期的休息")

    # 4. 完全沒有休息事件 → None。
    _eq(b._scheduled_rest_until([ev("character_done", now)], now=now), None,
        "沒有休息事件")

    # 5. wake_ts 缺漏或型別不對 → 當作沒在休息（fail-open 到「照常報警」，
    #    寧可多叫一次也不要把真的卡死消音）。
    for bad in ({}, {"wake_ts": None}, {"wake_ts": "later"}):
        _eq(b._scheduled_rest_until([ev("schedule_rest", now - 5, **bad)],
                                     now=now), None, f"壞的 wake_ts {bad}")

    # 7. 兩個消費端都必須真的問過它。純函式測得再漂亮，呼叫點沒接上就等於沒
    #    修——而這兩處正是使用者唯一看得到差別的地方。
    import inspect
    rate = inspect.getsource(b.cmd_rate)
    assert "_scheduled_rest_until" in rate, (
        "/rate 的停滯警告必須先問過『現在是不是排程休息』")
    assert (rate.index("_scheduled_rest_until") < rate.index("gap > 3600")), (
        "要先判斷休息、再判斷停滯；順序反過來就等於沒判")
    eta = inspect.getsource(b.cmd_eta)
    assert "_scheduled_rest_until" in eta, (
        "/eta 必須把『現在這段休息還剩多久』算進總時間，否則最多少報 rest_hours")
    cur = inspect.getsource(b.cmd_current)
    assert "_scheduled_rest_until" in cur, (
        "/current 是使用者問『它現在在幹嘛』的指令；休息中只回「角色之間」"
        "等於把一段可預期的閒置講得像出了事")

    # 6. 休息 → 恢復 → 又休息：最後那一段說了算。
    twice = [ev("schedule_rest", now - 9000, wake_ts=now - 8000),
             ev("schedule_resumed", now - 8000, rested_sec=1000),
             ev("schedule_rest", now - 50, wake_ts=now + 1200)]
    _eq(b._scheduled_rest_until(twice, now=now), now + 1200, "第二段休息")


def test_seconds_per_image_ignores_a_stale_regime():
    """速率換了制度之後，舊樣本不可以再參與平均。

    這是照著正式資料的形狀寫的（`events.ndjson`，2026-08-27）：08-21～08-23
    共 14 筆是 26～45 秒／張，08-24 起因為站方的使用量上限開始咬，變成
    101.8 → 169.7 → **253.1** 秒／張，單調惡化。原本的「全部平均」給出
    62.1 秒／張，也就是 `/eta` 少算 **4.1 倍**——10 個角色的佇列它會說 20.7
    小時，實際 84 小時。

    有趨勢的時候最近的樣本才是預測，跨制度平均只會被一個已經不存在的世界的
    舊資料主導。所以取「最近 24 小時」而不是「取多幾筆求平均」。"""
    now = 1_000_000.0
    day = 24 * 3600
    old_regime = [
        {"type": "character_done", "ts": now - 5 * day + i * 3600,
         "saved": 120, "elapsed_sec": 120 * 30}
        for i in range(14)
    ]
    new_regime = [
        {"type": "character_done", "ts": now - 3 * day, "saved": 120,
         "elapsed_sec": 120 * 101.8},
        {"type": "character_done", "ts": now - 2 * day, "saved": 120,
         "elapsed_sec": 120 * 169.7},
        {"type": "character_done", "ts": now - 3600, "saved": 120,
         "elapsed_sec": 120 * 253.1},          # 唯一落在 24 小時視窗內的
    ]
    spi, measured, basis, _v = b._seconds_per_image(
        old_regime + new_regime, now=now)
    _eq(measured, True, "has history")
    _eq(basis, "window", "24h 內有樣本就用視窗")
    _eq(round(spi, 1), 253.1, "只採計視窗內那一筆")
    # 對照：全部平均會給出什麼——這個數字就是這條測試要擋掉的東西。
    everything = old_regime + new_regime
    naive = (sum(e["elapsed_sec"] for e in everything)
             / sum(e["saved"] for e in everything))
    # naive 會落在**舊制度**那一帶（14 筆舊的壓過 3 筆新的），而不是現實的
    # 253——這正是「全部平均」的病灶。不釘死一個數字：關鍵是它離現實有多遠。
    assert naive < 70, f"樣本形狀跑掉了，naive={naive} 應落在舊制度帶"
    assert spi > naive * 3, (
        f"視窗版必須明顯高於全部平均，否則這條測試沒在測東西："
        f"{spi} vs {naive}")
    # 視窗內完全沒有樣本時，退回最近 3 筆（不論多舊），而且要**說出來**。
    spi2, measured2, basis2, _v2 = b._seconds_per_image(
        old_regime + new_regime[:2], now=now)
    _eq(basis2, "recent", "視窗內沒樣本 -> recent")
    _eq(measured2, True, "仍然算 measured")
    # 最近 3 筆 = 舊制度最後一筆(30) + 101.8 + 169.7
    _eq(round(spi2, 1), round((120 * 30 + 120 * 101.8 + 120 * 169.7) / 360, 1),
        "recent 取最後三筆")
    # 未來時間戳（時鐘倒退／手改事件檔）不得被當成新鮮樣本。
    future = [{"type": "character_done", "ts": now + 10 * day, "saved": 120,
               "elapsed_sec": 120 * 9999}]
    _eq(b._seconds_per_image(old_regime + future, now=now)[2], "recent",
        "未來的 ts 不算在視窗內")


_HOUR = 3600.0


def _cycle_events(t0: float, phases: str, *, imgs: int = 120,
                  burn: float = 276.0, drip: float = 456.0,
                  rest: float = 6 * _HOUR):
    """把正式資料裡那個快/慢交替的形狀合成出來（**不要**改成隨機數字——這條測試
    要看得出它在防哪一種現實）。

    `phases` 是一串相位字母，按時間順序：

      `B` = burn，休息後的第一個角色。前面會自動插入一段排程休息
            （`schedule_rest` + `schedule_resumed`），因為額度在休息期間照樣
            累積，所以這個角色是在**燒庫存**，跑得快。
      `D` = drip，庫存燒完之後的角色，等於純滴入速率，跑得慢。

    預設的 276 / 456 秒每張就是 2026-09-06 那兩筆的實測值（9.20h / 15.20h，
    每個角色 120 張）。回傳 `(events, done_ts)`，`done_ts` 讓呼叫端挑 `now`。
    """
    evs: list[dict] = []
    done_ts: list[float] = []
    t = t0
    for phase in phases:
        if phase == "B":
            evs.append({"type": "schedule_rest", "ts": t, "wake_ts": t + rest})
            t += rest
            evs.append({"type": "schedule_resumed", "ts": t, "rested_sec": rest})
        elapsed = imgs * (burn if phase == "B" else drip)
        t += elapsed
        evs.append({"type": "character_done", "ts": t, "saved": imgs,
                    "elapsed_sec": elapsed})
        done_ts.append(t)
    return evs, done_ts


def test_eta_sampling_must_cover_a_whole_quota_cycle():
    """視窗非空還不夠——樣本必須涵蓋完整的一個快/慢週期。

    額度是連續滴入的（實測 ~7.85 張／小時），而排程休息期間它照樣累積，所以
    **休息後的第一個角色在燒庫存**（實測 276 秒／張），下一個角色沒有庫存可燒
    就等於純滴入速率（456 秒／張）。一個角色 9～15 小時，24 小時的視窗裡通常
    只有 1～2 筆——**在一個有已知週期的系統裡，n=1 不是「樣本少」，是「保證有
    偏」**：一筆樣本必然只落在其中一個相位。

    2026-09-07 實測就是這樣：視窗裡只剩 276.0 那一筆快的，`/eta` 對 33 對的
    佇列少報 **4 天**（411.6h vs 507.7h）。而且它不是穩定地錯，視窗滑過去的
    時候有時抓到 {快}、有時 {慢}、有時 {快,慢}。

    兩個方向都要釘住，否則刪掉任一邊都能過：只有一筆時不得直接採用，**而樣本
    足夠時仍然要用視窗**（改成永遠走 fallback 等於退回「全部平均」，那是
    08-27 已經修掉的錯）。
    """
    t0 = 1_700_000_000.0
    burn_el, drip_el = 120 * 276.0, 120 * 456.0
    one_cycle = (drip_el + burn_el) / 240          # 366.0，一個完整週期的平均

    # --- 1. 回報的那個缺陷：視窗裡只剩「休息後」那一筆快的 -----------------
    # 相位順序照抄 09-04～09-06 的實測：慢、休息、快、慢、休息、快。
    evs, done = _cycle_events(t0, "DBDB")
    now = done[-1] + 12 * _HOUR                    # 實測時 now 距最後一筆 11.8h
    in_window = [e for e in evs if e["type"] == "character_done"
                 and 0 <= now - e["ts"] <= b.ETA_SAMPLE_WINDOW_SEC]
    _eq(len(in_window), 1, "視窗裡真的只有一筆（這就是缺陷的前提）")
    _eq(round(in_window[0]["elapsed_sec"] / in_window[0]["saved"], 1), 276.0,
        "而且是快的那一相")
    spi, measured, basis, _v = b._seconds_per_image(evs, now=now)
    _eq(measured, True, "有歷史資料")
    _eq(basis, "cycle", "只有一相 -> 往回補到完整週期")
    _eq(round(spi, 1), round(one_cycle, 1), "補完之後 = 一個完整週期的平均")
    assert spi > 276.0 * 1.2, (
        f"沒有補到就等於沒修：{spi} 還停在燒庫存那一相的 276 秒／張，"
        f"`/eta` 會少報 4 天")

    # --- 2. 反方向：視窗裡只剩「穩態」那一筆慢的，一樣有偏（會多報）--------
    now2 = done[2] + 12 * _HOUR                    # 只蓋到第二個 D
    in_window2 = [e for e in evs if e["type"] == "character_done"
                  and 0 <= now2 - e["ts"] <= b.ETA_SAMPLE_WINDOW_SEC]
    _eq(len(in_window2), 1, "反方向也只有一筆")
    _eq(round(in_window2[0]["elapsed_sec"] / in_window2[0]["saved"], 1), 456.0,
        "而且是慢的那一相")
    spi2, _m2, basis2, _v2 = b._seconds_per_image(evs, now=now2)
    _eq(basis2, "cycle", "慢的那一相單獨一筆也要補")
    _eq(round(spi2, 1), round(one_cycle, 1), "補完一樣是完整週期的平均")

    # --- 3. 視窗裡樣本夠了就**不准**再動它 --------------------------------
    # 這一條擋的是「修成永遠走 fallback」——那等於退回全部平均。
    evs3, done3 = _cycle_events(t0, "DBD")
    now3 = done3[-1] + 1 * _HOUR                   # 視窗蓋到 B 與第二個 D
    spi3, _m3, basis3, _v3 = b._seconds_per_image(evs3, now=now3)
    _eq(basis3, "window", "兩相都在視窗裡 -> 用視窗，不要往回補")
    _eq(round(spi3, 1), round(one_cycle, 1), "視窗自己就是一個完整週期")
    everything = (2 * drip_el + burn_el) / 360     # 396.0，三筆全拿的答案
    assert abs(spi3 - everything) > 20, (
        f"這一條必須分得出「用視窗」跟「全部拿來平均」：{spi3} vs {everything}")

    # --- 4. 沒有排程休息 = 沒有兩相，n=1 是合法的 -------------------------
    # 08-27 那條迴歸（舊制度樣本不可以再參與平均）走的就是這條路。這裡刻意
    # 用**間隔很大**的樣本：實測 2026-08-27 那一筆 253.1 前面有 42.85 小時的
    # 閒置，而它是那個時期最慢的一筆——所以「用時間間隔推算休息」會誤觸發，
    # 判定必須看事件檔裡明寫的 `schedule_resumed`。
    day = 24 * _HOUR
    no_rest = [
        {"type": "character_done", "ts": t0 + 0 * day, "saved": 120,
         "elapsed_sec": 120 * 101.8},
        {"type": "character_done", "ts": t0 + 1 * day, "saved": 120,
         "elapsed_sec": 120 * 169.7},
        {"type": "character_done", "ts": t0 + 3 * day, "saved": 120,
         "elapsed_sec": 120 * 253.1},
    ]
    spi4, _m4, basis4, _v4 = b._seconds_per_image(
        no_rest, now=t0 + 3 * day + _HOUR)
    _eq(basis4, "window", "沒有休息事件 -> 不補，維持視窗")
    _eq(round(spi4, 1), 253.1, "只採計最近那一筆（08-27 的迴歸）")

    # --- 5. 一個工作段跑得完 3 個穩態角色時，比例仍然要是 1:3 -------------
    # 單純「補到兩相為止」在這裡會停在 1:1，把快的那一相灌水三倍。
    evs5, done5 = _cycle_events(t0, "BDDDB", imgs=60)
    now5 = done5[-1] + 20 * _HOUR
    _eq(len([e for e in evs5 if e["type"] == "character_done"
             and 0 <= now5 - e["ts"] <= b.ETA_SAMPLE_WINDOW_SEC]), 1,
        "視窗裡只有最後那個 B")
    spi5, _m5, basis5, _v5 = b._seconds_per_image(evs5, now=now5)
    _eq(basis5, "cycle", "只有一相 -> 補")
    exact = (60 * 276.0 + 3 * 60 * 456.0) / 240     # 411.0，1 快 3 慢
    _eq(round(spi5, 1), round(exact, 1), "補出來的比例 = 長期比例 1:3")
    one_to_one = (60 * 276.0 + 60 * 456.0) / 120    # 366.0，停在第一個穩態
    assert abs(spi5 - one_to_one) > 20, (
        f"停在「補到兩相為止」會得到 {one_to_one}，那是把快的灌水三倍")
    both_posts = (2 * 60 * 276.0 + 3 * 60 * 456.0) / 300   # 384.0
    assert abs(spi5 - both_posts) > 20, (
        f"連前一個週期的起點也收進來會得到 {both_posts}，那是多了半個週期")

    # --- 6. 每一個角色都是「休息後」時，本來就沒有偏差，不准動它 ----------
    # （一個工作段只跑得完一個角色時就是這樣。這時候 n=1 是**無偏**的。）
    evs6, done6 = _cycle_events(t0, "BBB", imgs=60)
    now6 = done6[-1] + 20 * _HOUR
    spi6, _m6, basis6, _v6 = b._seconds_per_image(evs6, now=now6)
    _eq(basis6, "window", "全部同相 -> 無偏 -> 維持視窗")
    _eq(round(spi6, 1), 276.0, "就用那一筆")

    # --- 7. 往回補也有天花板：太舊的另一相不准拉回來 ----------------------
    # 理由跟 08-27 那條一樣——跨制度平均會被一個已經不存在的世界主導。
    now7 = t0 + 200 * _HOUR
    stale = [
        {"type": "character_done", "ts": now7 - 100 * _HOUR, "saved": 120,
         "elapsed_sec": drip_el},
        {"type": "schedule_rest", "ts": now7 - 100 * _HOUR,
         "wake_ts": now7 - 94 * _HOUR},
        {"type": "schedule_resumed", "ts": now7 - 94 * _HOUR},
        {"type": "character_done", "ts": now7 - 10 * _HOUR, "saved": 120,
         "elapsed_sec": burn_el},
    ]
    assert 100 * _HOUR > b.ETA_CYCLE_LOOKBACK_SEC > 10 * _HOUR, (
        "測資是照著回溯上限挑的，上限改了要跟著改")
    spi7, _m7, basis7, _v7 = b._seconds_per_image(stale, now=now7)
    _eq(basis7, "window", "另一相超出回溯上限 -> 不補")
    _eq(round(spi7, 1), 276.0, "維持視窗裡那一筆")

    # --- 8. 相位旗標必須被壞掉的那一筆消耗掉 ------------------------------
    # 休息之後的第一個 `character_done` 就算資料不完整（`saved=0`）被跳過，那次
    # 休息的庫存也已經由它燒掉了；旗標留給下一個角色會把一個**穩態**角色誤標成
    # 「休息後」。測資必須讓這個誤標真的改變答案——前面要另外有一個真的休息後
    # 角色，否則誤標與否都只是「單相」，兩條路的結果一樣，這條測試就白寫了。
    evs8, done8 = _cycle_events(t0, "BD")
    tail = done8[-1]
    evs8 += [
        {"type": "schedule_rest", "ts": tail, "wake_ts": tail + 6 * _HOUR},
        {"type": "schedule_resumed", "ts": tail + 6 * _HOUR,
         "rested_sec": 6 * _HOUR},
        {"type": "character_done", "ts": tail + 6 * _HOUR + burn_el,
         "saved": 0, "elapsed_sec": burn_el},           # 壞資料，會被跳過
        {"type": "character_done",
         "ts": tail + 6 * _HOUR + burn_el + drip_el,
         "saved": 120, "elapsed_sec": drip_el},         # 這一筆是**穩態**
    ]
    now8 = tail + 6 * _HOUR + burn_el + drip_el + 12 * _HOUR
    spi8, _m8, basis8, _v8 = b._seconds_per_image(evs8, now=now8)
    _eq(basis8, "cycle", "視窗裡只有那一筆穩態 -> 往回補")
    correct = (burn_el + 2 * drip_el) / 360            # 396.0
    _eq(round(spi8, 1), round(correct, 1),
        "旗標被壞掉的那一筆消耗掉 -> 最後一筆算穩態 -> 補到真正的休息後角色")
    misread = (2 * drip_el) / 240                      # 456.0
    assert abs(spi8 - misread) > 20, (
        f"把最後一筆誤標成「休息後」會停在 {misread}，等於一個穩態都沒補到")


def _quota_ev(t, ts, **kw):
    return dict(type=t, ts=ts, **kw)


def test_a_quota_wait_is_not_mistaken_for_a_stall():
    """等額度回補是可預期的閒置，不是卡住——但被砍掉的等待不可以永久消音。

    `quota_wait_poll_sec` 的預設值 **3600** 跟 `/rate` 的停滯門檻 **3600** 是
    同一個數字，所以每一個額度週期都會讓「超過一小時沒有新圖」亮一次。用
    `output/` 最近 3 天的檔案 mtime 實測（568 張圖、70.9 小時、以 1 分鐘為刻度
    取樣 4255 點）：**15.7% 的時刻 `gap > 3600` 成立**；55 個超過 60 分鐘的間隔
    裡只有 2 個是排程休息，其餘 53 個全是 61～65 分鐘的額度等待。

    反方向跟排程休息一模一樣重要：`quota_resumed` 只在「重試真的產出了圖」時才
    發，所以重試燒完沒產出、被 `/stop`、瀏覽器連線掛掉這三種收場都不會寫下結束
    事件（實測 218 個 `quota_blocked` 對 212 個 `quota_resumed`）。少了時間上界
    就會從「假警報」換成**真異常被消音**，比原本更糟。
    """
    now = 1_800_000_000.0
    poll = 3600.0

    # 1. 正在等：`quota_blocked` 之後還沒有結束事件 -> 回下次重試的時刻。
    waiting = [_quota_ev("character_start", now - 3000, name="x"),
               _quota_ev("quota_blocked", now - 1800, character="x",
                         image_index=60)]
    _eq(b._quota_wait_until(waiting, now=now, poll_sec=poll), now - 1800 + poll,
        "等額度中 -> 下次重試時刻")

    # 2. 等完了 -> None，警告恢復正常。
    done = waiting + [_quota_ev("quota_resumed", now - 60, character="x",
                                waited_sec=3600.0, last_wait_sec=3600.0)]
    _eq(b._quota_wait_until(done, now=now, poll_sec=poll), None, "額度回來了")

    # 3. 有 `quota_blocked`、沒有任何結束事件，但已經過了兩倍輪詢間隔
    #    （＝等待途中被砍掉）-> None，不可以永久消音。
    killed = [_quota_ev("quota_blocked", now - 3 * 3600, character="x")]
    _eq(b._quota_wait_until(killed, now=now, poll_sec=poll), None,
        "過期的等待")
    # 邊界：剛好在上界內還算數，超過就不算。
    _eq(b._quota_wait_until([_quota_ev("quota_blocked", now - 2 * poll)],
                            now=now, poll_sec=poll) is not None, True, "上界內")
    _eq(b._quota_wait_until([_quota_ev("quota_blocked", now - 2 * poll - 1)],
                            now=now, poll_sec=poll), None, "剛好超出上界")

    # 4. 等待期間**照樣**會出現的事件不算結束——產圖程式每個睡眠切片都會跑一次
    #    暫停檢查與「插播 bot 的請求」。把它們當結束的話，只要使用者在等待中問
    #    一次單圖，警告就又亮起來了。
    with_noise = [_quota_ev("quota_blocked", now - 1800, character="x")]
    noise_types = ("single_image_done", "single_image_serving", "dom_result",
                   "paused", "resumed")
    # 反方向：往正式那份 frozenset 加一個名字、卻沒加到這裡，在此之前是全綠的。
    assert set(noise_types) == set(b._QUOTA_WAIT_NEUTRAL_EVENTS), (
        "中性事件清單跟正式那份對不起來（兩個方向都要對）")
    for noise in noise_types:
        with_noise.append(_quota_ev(noise, now - 900))
    _eq(b._quota_wait_until(with_noise, now=now, poll_sec=poll),
        now - 1800 + poll, "插播與暫停事件不算等待結束")

    # 5. 其他任何事件都當成「這一輪走完了」——fail-open 到照常報警。
    for ender in ("character_start", "character_done", "critical_error",
                  "chrome_restart", "todo_done", "generation_blocked"):
        moved_on = [_quota_ev("quota_blocked", now - 1800, character="x"),
                    _quota_ev(ender, now - 300)]
        _eq(b._quota_wait_until(moved_on, now=now, poll_sec=poll), None,
            f"{ender} 之後就不算還在等")

    # 6. `quota_wait` 會重新計時，而且它帶的 `next_retry_sec` 就是當時生效的
    #    輪詢間隔——優先於設定檔（設定可能在等待中被改過）。
    multi = [_quota_ev("quota_blocked", now - 7000, character="x"),
             _quota_ev("quota_wait", now - 1800, waited_sec=3600.0,
                       next_retry_sec=1800.0)]
    _eq(b._quota_wait_until(multi, now=now), now - 1800 + 1800.0,
        "quota_wait 重新計時並帶出當時的輪詢間隔")

    # 7. 壞掉的 / 缺漏的 ts -> 當作沒在等（跟 `_scheduled_rest_until` 同一個
    #    立場：寧可多叫一次，也不要把真的卡死消音）。
    for bad in ({}, {"ts": None}, {"ts": "soon"}, {"ts": float("nan")}):
        _eq(b._quota_wait_until([dict(type="quota_blocked", **bad)], now=now,
                                poll_sec=poll), None, f"壞的 ts {bad}")
    # 未來的時間戳（時鐘被調過／手改事件檔）也不算。
    _eq(b._quota_wait_until([_quota_ev("quota_blocked", now + 600)], now=now,
                            poll_sec=poll), None, "未來的時間戳")

    # 8. 完全沒有額度事件 -> None。
    _eq(b._quota_wait_until([_quota_ev("character_done", now - 60)], now=now,
                            poll_sec=poll), None, "沒有額度事件")

    # 9. 呼叫端真的問過它，而且問在停滯判斷**之前**。
    import inspect
    rate = inspect.getsource(b.cmd_rate)
    assert "_quota_wait_until" in rate, (
        "/rate 的停滯警告必須先問過『現在是不是在等額度回補』")
    assert rate.index("_quota_wait_until") < rate.index("gap > 3600"), (
        "要先判斷等額度、再判斷停滯；順序反過來就等於沒判")


def _rate_stall_line(monkeypatch, events, gap_sec):
    """跑真的 `cmd_rate`，把「newest image」那一欄的文字挖出來。

    純函式測得再漂亮，使用者看到的還是這一行——而「把整段警告刪掉」也能讓純函式
    測試全綠，所以這一層一定要有。
    """
    import asyncio as _asyncio
    captured = {}

    async def _recorder(_message, *_a, **kw):
        captured.update(kw)

    monkeypatch.setattr(b, "safe_reply", _recorder)
    monkeypatch.setattr(b, "_read_events_tail", lambda *a, **k: events)
    monkeypatch.setattr(
        b, "_gather_output_images",
        lambda *a, **k: [(time.time() - gap_sec, "x.png")])
    _asyncio.run(b.cmd_rate(object()))
    embed = captured.get("embed")
    assert embed is not None, "cmd_rate 沒有送出 embed"
    for field in embed.fields:
        if field.name == "newest image":
            return field.value
    raise AssertionError(f"找不到 newest image 欄位：{embed.fields!r}")


def test_rate_only_warns_when_the_idle_is_not_explained(monkeypatch):
    """使用者看得到的那一行，三個方向都要對。

    只測「等額度時不得報停滯」的話，把整段警告刪掉也會過——所以「真的卡住時仍然
    要報」與「等待過期之後要恢復報警」都得各測一次。
    """
    now = time.time()
    gap = 3700.0                      # 已經超過一小時沒有新圖

    # 1. 真的卡住 -> 照常警告。
    plain = _rate_stall_line(monkeypatch, [], gap)
    assert "no new image in over an hour" in plain, plain

    # 2. 正在等額度 -> 不得警告，而且要說明是在等什麼。
    waiting = _rate_stall_line(
        monkeypatch, [_quota_ev("quota_blocked", now - 1800, character="x")],
        gap)
    assert "no new image in over an hour" not in waiting, waiting
    assert "quota" in waiting, waiting

    # 3. 等待已經過期（產圖程式在等待中被砍掉）-> 必須恢復警告。
    killed = _rate_stall_line(
        monkeypatch, [_quota_ev("quota_blocked", now - 5 * 3600, character="x")],
        gap)
    assert "no new image in over an hour" in killed, killed

    # 4. 排程休息優先於等額度（休息中本來就不會有額度事件在等，但兩者同時
    #    出現時要以休息為準——它有明確的醒來時刻，資訊比較多）。
    both = _rate_stall_line(monkeypatch, [
        _quota_ev("quota_blocked", now - 1800, character="x"),
        _quota_ev("schedule_rest", now - 600, wake_ts=now + 3600),
    ], gap)
    assert "scheduled rest" in both, both

    # 5. 洩漏規則第 1 層：這一行會貼進對話平台。
    for banned in ("webrunner", "novelai", ".py", "D:\\", "output/"):
        assert banned not in waiting, f"洩漏了 {banned!r}：{waiting!r}"


def test_end_marker_and_pair_count():
    _eq(b._is_end_marker("end"), True, "end")
    _eq(b._is_end_marker("  END "), True, "padded END")
    _eq(b._is_end_marker("the end"), False, "the end")
    _eq(b._end_marker_index(["a", "b", "end", "c"]), 2, "end idx")
    _eq(b._end_marker_index(["a", "b"]), None, "no end idx")
    # no marker: max of the three lengths
    _eq(b._effective_pair_count(["p1", "p2"], ["a"], []), (2, None), "no end")
    # marker at index 1 caps the run to 1 pair
    _eq(b._effective_pair_count(["p", "end", "q"], ["a", "b", "c"], []),
        (1, 1), "end caps to idx")
    # marker first -> 0 pairs
    _eq(b._effective_pair_count(["end", "p"], ["a"], []), (0, 0), "end first")


def test_pairing_matches_webrunner():
    """Drift guard: the bot's effective pair count MUST equal the webrunner's
    own pure pairing (`_queue_consume.pair_todos`) for the same lists (no end
    marker). If someone changes one side's pairing, this fails.

    **必須四條佇列都餵**（含 undesired）。這個守衛曾經有盲點：shapes 只生成
    prompt/char1/char2 三條、`pair_todos` 也只傳三個引數，於是兩邊吃到同樣殘缺的
    輸入、當然一致 —— 卻測不出 `_effective_pair_count` 少算 undesired 的真實漂移。
    `pair_todos` 是 `*todos` varargs、`n = max(len(t) for t in todos)`，傳三個和傳
    四個語意本來就不同；`decide` 的 `real_nonempty` 也含 undesired。所以
    `todo_undesired.md` 比其他三條長時，少算會讓 `!queue` / `!eta` 回報的批數低於
    實際執行。下面刻意保留 undesired 最長、以及 undesired 是唯一非空佇列的形狀。"""
    shapes = [
        ([], [], [], []),
        (["p"], [], [], []),
        (["p"], ["a", "b", "c"], ["x"], []),
        (["p1", "p2"], ["a"], ["x", "y", "z"], ["u"]),
        (["p1", "p2", "p3"], ["a", "b"], ["x", "y", "z", "w"], ["u"]),
        # --- undesired 主導的形狀：舊版三參數守衛完全看不到的區域 ---
        # undesired 比其他三條長 -> 它決定批數。
        (["p"], ["a"], ["x"], ["u1", "u2", "u3", "u4"]),
        (["p1", "p2"], ["a"], [], ["u1", "u2", "u3"]),
        # undesired 是唯一非空的佇列 -> 三參數版會算成 0。
        ([], [], [], ["u1", "u2"]),
        ([], [], [], ["u"]),
    ]
    rng = random.Random(1234)
    for _ in range(200):
        shapes.append((
            ["p"] * rng.randint(0, 5),
            ["a"] * rng.randint(0, 5),
            ["x"] * rng.randint(0, 5),
            ["u"] * rng.randint(0, 5),
        ))
    for p, c1, c2, u in shapes:
        bot_count = b._effective_pair_count(p, c1, c2, u)[0]
        wr_count = len(_queue_consume.pair_todos(p, c1, c2, u))
        assert bot_count == wr_count, (
            f"DRIFT: bot={bot_count} webrunner={wr_count} for {p, c1, c2, u}")
    print(f"  OK bot pair count == _queue_consume.pair_todos over "
          f"{len(shapes)} 4-queue shapes")

    # 向後相容：`undesired` 是選用參數（預設 None），既有的三參數呼叫端不可壞掉，
    # 且必須等同「undesired 為空」。
    for p, c1, c2, _u in shapes:
        three = b._effective_pair_count(p, c1, c2)
        assert three == b._effective_pair_count(p, c1, c2, []), (
            f"3-arg call must equal empty-undesired for {p, c1, c2}")
        assert three[0] == len(_queue_consume.pair_todos(p, c1, c2)), (
            f"3-arg call must still match 3-queue pairing for {p, c1, c2}")
    print("  OK 3-arg back-compat (undesired defaults to empty) preserved")


def test_p7_pair_todos_single_source():
    """P7: `pair_todos` has ONE definition (`_queue_consume`); `_webrunner_shared`
    re-exports it (identity) and the bot preview calls the same function. Behaviour
    must be identical across queue shapes (the bot used to inline its own
    fill/zip)."""
    _eq(ws.pair_todos is _queue_consume.pair_todos, True, "ws re-exports qc")
    shapes = [
        ([], [], [], []),
        (["P"], ["a", "b", "c"], [], []),
        (["P1", "P2"], ["a"], ["x", "y", "z"], ["u"]),
        (["p1", "p2", "p3"], ["a", "b"], ["x", "y", "z", "w"], []),
        ([], ["a"], [], []),
    ]
    for p, c1, c2, u in shapes:
        _eq(_queue_consume.pair_todos(p, c1, c2, u),
            ws.pair_todos(p, c1, c2, u), f"qc==ws {p, c1, c2, u}")


def test_p7_plan_char_name_single_source():
    """P7: the bot's `_plan_char_name` converged onto
    `_webrunner_shared.character_folder_name`. Identity check + hard-coded pins
    of the OLD bot behaviour (first half-width-comma segment, sanitise, [:120])."""
    _eq(b._plan_char_name is ws.character_folder_name, True, "bot aliases shared")
    cases = [
        ("Alice, blue hair, smile", "Alice"),
        ("a/b:c, tag", "a_b_c"),
        ("", "character"),
        ("   ", "character"),
        ("Name<>|?*", "Name_"),
        ("x" * 200, "x" * 120),
    ]
    for prompt, want in cases:
        _eq(b._plan_char_name(prompt), want, f"name({prompt[:10]!r})")


def test_p7_is_end_marker_single_source():
    """P7: `END_SENTINEL` + `is_end_marker` single-sourced in `_queue_consume`;
    the bot aliases both. Pin against the literal the webrunner used to inline
    (`s.strip().lower() == "end"`)."""
    _eq(b._is_end_marker is _queue_consume.is_end_marker, True, "bot aliases shared")
    _eq(b.END_SENTINEL, _queue_consume.END_SENTINEL, "same constant")
    for s, want in [("end", True), (" END ", True), ("End", True),
                    ("the end", False), ("ended", False), ("", False)]:
        _eq(_queue_consume.is_end_marker(s), s.strip().lower() == "end",
            f"matches literal {s!r}")
        _eq(b._is_end_marker(s), want, f"bot {s!r}")


def test_p7_compute_run_plan_regression():
    """P7 (user-facing): `_compute_run_plan` must emit byte-identical
    `(pairs, fb)` after converging onto `_queue_consume.pair_todos`. Drive it via
    on-disk queues in a tmp dir and assert exact output for representative shapes
    (empty / single / padding / end-present / fallbacks). `!plan` is what the user
    sees, so this must not change."""
    d = Path(tempfile.mkdtemp())
    paths = {
        "TODO_PROMPT_FILE": d / "todo_prompt.md",
        "TODO_FILE_1": d / "todo_character1.md",
        "TODO_FILE_2": d / "todo_character2.md",
        "TODO_UNDESIRED_FILE": d / "todo_undesired.md",
        "PROMPT_FILE": d / "prompt.md",
        # 每一個 fallback 常數都要導進 tmp。漏一個的話測試會讀到 repo root 那份
        # **有內容**的真檔，於是「空佇列」的案例悄悄變成「有 fallback」。
        "CHARACTER1_FILE": d / "character1.md",
        "CHARACTER2_FILE": d / "character2.md",
        "UNDESIRED_FILE": d / "undesired.md",
    }
    saved = {k: getattr(b, k) for k in paths}
    for k, v in paths.items():
        setattr(b, k, v)
    NO_FB = {"prompt": False, "char1": False, "char2": False,
             "undesired": False}
    try:
        def run(p, c1, c2, u, fb_p="", fb_c1="", fb_c2="", fb_u=""):
            for path in paths.values():
                if path.exists():
                    path.unlink()

            def w(path, lst):
                if lst:
                    path.write_text("\n".join(lst) + "\n", encoding="utf-8")
            w(paths["TODO_PROMPT_FILE"], p)
            w(paths["TODO_FILE_1"], c1)
            w(paths["TODO_FILE_2"], c2)
            w(paths["TODO_UNDESIRED_FILE"], u)
            for raw, key in ((fb_p, "PROMPT_FILE"), (fb_c1, "CHARACTER1_FILE"),
                             (fb_c2, "CHARACTER2_FILE"),
                             (fb_u, "UNDESIRED_FILE")):
                if raw:
                    paths[key].write_text(raw, encoding="utf-8")
            return b._compute_run_plan()

        _eq(run([], [], [], []), ([], dict(NO_FB)), "all empty -> []")
        _eq(run(["P"], ["a"], [], []),
            ([("P", "a", "", "")], dict(NO_FB)), "single")
        _eq(run(["P1", "P2"], ["a", "b"], ["", "C2"], []),
            ([("P1", "a", "", ""), ("P2", "b", "C2", "")],
             dict(NO_FB)), "character2 positional blank")
        _eq(run(["P"], ["a", "b", "c"], [], []),
            ([("P", "a", "", ""), ("P", "b", "", ""), ("P", "c", "", "")],
             dict(NO_FB)), "short-prompt padding")
        # `_compute_run_plan` itself does NOT truncate at `end` (cmd_plan does);
        # the `end` entry must survive in the raw pairs.
        _eq(run(["P1", "end", "P3"], ["a", "b", "c"], [], []),
            ([("P1", "a", "", ""), ("end", "b", "", ""), ("P3", "c", "", "")],
             dict(NO_FB)), "end not truncated by plan")
        _eq(run([], ["a", "b"], [], [], fb_p="FB"),
            ([("FB", "a", "", ""), ("FB", "b", "", "")],
             {"prompt": True, "char1": False, "char2": False,
              "undesired": False}),
            "prompt fallback")
        _eq(run(["P1", "P2"], ["a", "b"], [], [], fb_c2="C2", fb_u="U"),
            ([("P1", "a", "C2", "U"), ("P2", "b", "C2", "U")],
             {"prompt": False, "char1": False, "char2": True,
              "undesired": True}),
            "char2 + undesired fallback")
        # 角色1 fallback（2026-08-23 接上）：空的角色1 佇列改吃 character1.md，
        # 而且**不會**把 pair 數撐大——n 仍由最長的真實佇列決定。
        _eq(run(["P1", "P2"], [], [], [], fb_c1="C1"),
            ([("P1", "C1", "", ""), ("P2", "C1", "", "")],
             {"prompt": False, "char1": True, "char2": False,
              "undesired": False}),
            "char1 fallback pads without adding pairs")
        _eq(run([], [], [], [], fb_p="FB", fb_c1="C1", fb_c2="C2", fb_u="U"),
            ([("FB", "C1", "C2", "U")],
             {"prompt": True, "char1": True, "char2": True,
              "undesired": True}),
            "all fallback -> exactly 1 pair")
    finally:
        for k, v in saved.items():
            setattr(b, k, v)


def _plan_reply(monkeypatch, prompts: list[str], payload: str = "") -> str:
    """跑一次 `cmd_plan`，回傳它送出的那一則訊息。佇列直接給配好的 pairs，不經磁碟。"""
    pairs = [(prompt, f"c{i}", "", "") for i, prompt in enumerate(prompts, 1)]
    monkeypatch.setattr(b, "_compute_run_plan", lambda: (pairs, {
        "prompt": False, "char1": False, "char2": False, "undesired": False}))
    sent: list[str] = []

    async def _reply(_message, text=None, **_kw):
        sent.append(text)

    monkeypatch.setattr(b, "safe_reply", _reply)
    asyncio.run(b.cmd_plan(types.SimpleNamespace(), payload))
    assert len(sent) == 1, sent
    return sent[0]


@pytest.mark.parametrize("prompts, runs, end_note, showing", [
    # `end` 落在顯示上限（預設 25）之後：2026-09-23 之前迴圈先被上限截斷、根本走不到
    # `end`，於是標題報 40 筆（連 `end` 之後的都算進去）而且不提 `end`。
    ([f"p{i}" for i in range(1, 30)] + ["end"] + [f"q{i}" for i in range(10)],
     29, "#30", "showing 25 of 29"),
    (["p1", "p2", "end", "q1", "q2"], 2, "#3", None),
    ([f"p{i}" for i in range(1, 41)], 40, None, "showing 25 of 40"),
], ids=["end-past-the-cap", "end-within-the-cap", "no-end"])
def test_the_plan_counts_only_the_pairs_before_end_whatever_the_cap(
        monkeypatch, prompts, runs, end_note, showing):
    """`/gen plan` 的標題數字是「這次 `/run` 會跑幾筆」。`_compute_run_plan` 本身不在
    `end` 截斷（上面那支釘住了），所以截斷只能在這裡做，而且不能跟顯示上限綁在同一個
    迴圈裡——`end` 在上限之後時，使用者會被告知一個比實際多的數字，也看不到 `end` 在哪。"""
    reply = _plan_reply(monkeypatch, prompts)
    assert f"{runs} pair(s) will run" in reply, reply
    if end_note is None:
        assert "`end` marker" not in reply, reply
    else:
        assert f"`end` marker at pair {end_note}" in reply, reply
    if showing is None:
        assert "showing" not in reply, reply
    else:
        assert showing in reply, reply
    assert "q1" not in reply, "`end` 之後的筆數不該出現在清單裡"


def _fences_balanced(chunk: str) -> bool:
    return chunk.count("```") % 2 == 0


def test_a_plain_long_reply_is_split_on_line_breaks_and_loses_nothing():
    lines = [f"line {i} " + "x" * 60 for i in range(80)]
    original = "\n".join(lines)
    chunks = b._chunk_for_discord(original, limit=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks), [len(c) for c in chunks]
    assert "\n".join(chunks) == original


def test_a_code_block_cut_in_two_is_closed_and_reopened_with_its_language():
    """一則長回覆裡的程式碼區塊跨過切點時，舊的切法讓第一段停在區塊裡、第二段以程式碼
    開頭卻沒有開頭的 fence——第二段的程式碼被當成一般 markdown 排版，而它的收尾 fence
    反過來**開**了一個新區塊，把後面的說明文字整段吞進去（2026-09-23）。"""
    code = "\n".join(f"    value_{i} = compute({i})" for i in range(60))
    original = f"說明在前。\n```python\n{code}\n```\n說明在後。"
    chunks = b._chunk_for_discord(original, limit=600)
    assert len(chunks) > 2
    assert all(len(chunk) <= 600 for chunk in chunks), [len(c) for c in chunks]
    assert all(_fences_balanced(chunk) for chunk in chunks), chunks
    for chunk in chunks[1:-1]:
        assert chunk.startswith("```python\n"), chunk[:30]
    assert chunks[-1].endswith("說明在後。")
    # 拿掉補上去的 fence 之後，內容與原文一字不差。
    rebuilt = "\n".join(chunks).replace("\n```\n```python\n", "\n")
    assert rebuilt == original


def test_a_fence_that_opens_and_closes_on_one_line_is_not_carried_over():
    original = "\n".join(["用 ```x``` 這種寫法" + "y" * 50] * 30)
    chunks = b._chunk_for_discord(original, limit=400)
    assert len(chunks) > 1
    assert not any(chunk.startswith("```") for chunk in chunks), chunks
    assert "\n".join(chunks) == original


def test_a_line_longer_than_the_limit_is_still_cut_inside_a_code_block():
    original = "```\n" + "z" * 1500 + "\n```"
    chunks = b._chunk_for_discord(original, limit=600)
    assert all(len(chunk) <= 600 for chunk in chunks), [len(c) for c in chunks]
    assert all(_fences_balanced(chunk) for chunk in chunks), chunks
    assert "".join(chunks).count("z") == 1500


def test_the_progress_message_leaves_plain_text_alone():
    short = "還在想。\n第二行"
    assert b._dorossi_progress_render(short) == short
    long_text = "\n".join(f"line {i} " + "x" * 50 for i in range(80))
    shown = b._dorossi_progress_render(long_text)
    assert shown.startswith(b.DOROSSI_STREAM_TRUNC_PREFIX)
    assert long_text.endswith(shown[len(b.DOROSSI_STREAM_TRUNC_PREFIX):])
    assert "```" not in shown


def test_the_progress_tail_reopens_a_code_block_its_cut_landed_in():
    """進度訊息只留最新的尾段。切點落在程式碼區塊裡時，尾段以程式碼開頭卻沒有開頭的 fence——
    自走任務跑好幾分鐘，擁有者一路看到的都是排版壞掉的訊息（2026-09-23）。"""
    code = "\n".join(f"    value_{i} = compute({i})" for i in range(120))
    text = f"說明。\n```python\n{code}\n```\n收尾說明。"
    shown = b._dorossi_progress_render(text)
    assert len(shown) <= 2000
    body = shown[len(b.DOROSSI_STREAM_TRUNC_PREFIX):]
    assert body.startswith("```python\n"), body[:40]
    assert body.count("```") % 2 == 0, body[-80:]
    assert body.endswith("收尾說明。")


def test_a_block_still_being_written_is_closed_for_display():
    """串流到一半，模型正寫在程式碼區塊裡：訊息結尾有開頭的 fence、沒有收尾的。"""
    shown = b._dorossi_progress_render("看這段：\n```py\nx = 1\ny =")
    assert shown == "看這段：\n```py\nx = 1\ny =\n```"
    code = "\n".join(f"    value_{i} = compute({i})" for i in range(120))
    long_open = f"說明。\n```python\n{code}"
    shown = b._dorossi_progress_render(long_open)
    assert len(shown) <= 2000
    body = shown[len(b.DOROSSI_STREAM_TRUNC_PREFIX):]
    assert body.startswith("```python\n") and body.endswith("\n```"), (body[:20], body[-20:])


@pytest.mark.parametrize("command, payload, stub", [
    ("cmd_clip", "get", ("get_clipboard", lambda text: (lambda: text))),
    ("cmd_read_text", "", ("read_text", lambda text: (
        lambda **_kw: [{"text": line} for line in text.split("\n")]))),
], ids=["clip", "screen-text"])
def test_content_shown_inside_a_code_block_cannot_close_it(
        monkeypatch, command, payload, stub):
    """剪貼簿與畫面文字是不受控的內容，這兩支自己把每一段包進 ``` 裡。內容本身若含
    ```，會提前關掉外面那個區塊，後面的內容就以 markdown 排版送出。"""
    name, make = stub
    content = "before\n```\nafter"
    monkeypatch.setattr(b._gui, name, make(content))
    monkeypatch.setattr(b._gui, "ocr_lang_for", lambda *_a: "eng", raising=False)
    sent: list[str] = []

    async def _reply(_message, text=None, **_kw):
        sent.append(text)

    monkeypatch.setattr(b, "safe_reply", _reply)
    asyncio.run(getattr(b, command)(types.SimpleNamespace(), payload))
    body = "\n".join(sent)
    assert "before" in body and "after" in body, sent
    assert body.count("```") == 2, sent


_FENCE_ESCAPERS = frozenset({"_fence_escape", "_fence_preview"})

# 包進 ``` 卻沒有在包裝點看得到跳脫的送出點。每一筆都要寫理由；對不上任何包裝點的條目會被
# 報成過期——一筆永遠對不上的豁免會安靜失效，而掃描看起來照常在跑。
_FENCE_WRAPS_SAFE_BY_CONSTRUCTION = {
    ("mcmd_base64", "encoded"): "base64 的字母表沒有反引號",
    ("cmd_clip", "chunks[0]"): "切段前整段已經過 `_fence_escape`，切段函式因此不會替內容重開 fence",
    ("cmd_clip", "chunk"): "同上",
    ("cmd_read_text", "chunks[0]"): "切段前整段已經過 `_fence_escape`，切段函式因此不會替內容重開 fence",
    ("cmd_read_text", "chunk"): "同上",
}


def _is_fence_escape_call(node) -> bool:
    """只認 `_fence_escape` / `_fence_preview`。手寫的 `.replace("```", …)` 不算——替身字元
    只留一個來源，寫錯替身（或換成另一個會被當成 fence 的字）的那一份就不會被這裡放行。"""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _FENCE_ESCAPERS)


def _fence_wraps(source: str) -> list[tuple[str, str, bool]]:
    """`(函式, 運算式, 有沒有跳脫)`：f-string 裡緊接在開頭 ```（後面換行）之後的每一個內插。

    有跳脫＝內插本身就是跳脫呼叫，或它的根名稱在**同一個函式**裡被指派過一個跳脫呼叫的
    結果。以函式為範圍，不是整個檔案——別的函式裡同名的 `body` 跳脫過，跟這裡無關。"""
    tree = ast.parse(source)
    found: set[tuple[str, str, bool]] = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        escaped_names = set()
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and _is_fence_escape_call(node.value):
                escaped_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        for node in ast.walk(func):
            if not isinstance(node, ast.JoinedStr):
                continue
            for before, value in zip(node.values, node.values[1:]):
                if not (isinstance(before, ast.Constant) and isinstance(before.value, str)
                        and before.value.endswith("```\n")
                        and isinstance(value, ast.FormattedValue)):
                    continue
                expr = value.value
                root = expr
                while isinstance(root, (ast.Subscript, ast.Attribute)):
                    root = root.value
                escaped = _is_fence_escape_call(expr) or (
                    isinstance(root, ast.Name) and root.id in escaped_names)
                found.add((func.name, ast.unparse(expr), escaped))
    return sorted(found)


def test_the_fence_wrap_scan_tells_the_shapes_apart():
    """掃描自己的對照組：真實資料全乾淨時，偵測那一半從來沒有在真資料上跑過。"""
    source = """
def direct(body):
    return f"```\\n{_fence_escape(body)}\\n```"

def assigned(body):
    safe = _fence_preview(body)
    return f"```\\n{safe[:10]}\\n```"

def legacy(body):
    safe = body.replace("```", "x")
    return f"```\\n{safe}\\n```"

def other_scope(body):
    safe = _fence_escape(body)
    return safe

def unescaped(body):
    safe = body.strip()
    return f"head\\n```\\n{safe}\\n```"
"""
    got = {(func, expr): escaped for func, expr, escaped in _fence_wraps(source)}
    assert got == {
        ("direct", "_fence_escape(body)"): True,
        ("assigned", "safe[:10]"): True,
        ("legacy", "safe"): False,
        ("unescaped", "safe"): False,
    }, got


def test_the_preview_helper_escapes_as_well_as_truncates():
    """上面那道掃描**信任** `_fence_preview` 與 `_fence_escape` 的結果，所以兩者本身要有行為測試：
    拿掉 `_fence_preview` 裡的跳脫，掃描照樣全綠，而六個佇列預覽會一起漏掉。"""
    assert b._fence_escape("```py\nx\n```") == "ʼʼʼpy\nx\nʼʼʼ"
    assert b._fence_preview("a```b") == "aʼʼʼb"
    assert b._fence_preview("x" * 130) == "x" * 120 + "…"
    assert "```" not in b._fence_preview("y" * 110 + "```" + "z" * 30)


def test_every_code_block_wrap_escapes_its_content():
    """把內容包進 ``` 的送出點，內容裡的 ``` 會提前關掉外面那個區塊。2026-09-23 量到十四個
    包裝點沒有跳脫，其中佇列項目（`/todo find`）、錄下來的巨集、指令輸出（`/sh`、`/job`、監看與
    排程的 shell 動作）都是不受控的內容。新的包裝點請走 `_fence_escape`；真的不可能含反引號
    的，加進 `_FENCE_WRAPS_SAFE_BY_CONSTRUCTION` 並寫理由。"""
    source = Path(b.__file__).read_text(encoding="utf-8")
    wraps = _fence_wraps(source)
    assert len(wraps) >= 25, f"只找到 {len(wraps)} 個包裝點——掃描壞了，不是變乾淨了"
    unescaped = {(func, expr) for func, expr, escaped in wraps if not escaped}
    registered = set(_FENCE_WRAPS_SAFE_BY_CONSTRUCTION)
    assert unescaped - registered == set(), (
        f"這些包裝點沒有跳脫內容：{sorted(unescaped - registered)}")
    assert registered - unescaped == set(), (
        f"這些豁免已經對不上任何未跳脫的包裝點，請刪掉：{sorted(registered - unescaped)}")


def test_streaming_media_only():
    """Presence accepts approved streaming sources, never raw local players."""
    spotify = {"title": "song", "artist": "artist", "source": "Spotify",
               "playback_type": "Music"}
    _eq(pp._filter_smtc_media(spotify), spotify,
        "approved Spotify source -> accepted")

    for source in ("vlc", "MusicBee", "foobar2000", "AIMP", "Winamp",
                   "Groove Music", "Windows.MediaPlayer", "unknown-player"):
        raw = {"title": "local.mp3", "artist": "", "source": source,
               "playback_type": "Music"}
        _eq(pp._filter_smtc_media(raw), None,
            f"local/unapproved source {source} -> rejected")

    saved = (pp.probe_game_process, pp.probe_smtc_raw_async,
             pp.probe_foreground_music, pp.probe_claude_code)

    async def fake_raw(_timeout=6.0):
        return {"title": "local.mp3", "artist": "", "source": "vlc",
                "playback_type": "Music"}

    try:
        pp.probe_game_process = lambda: None
        pp.probe_smtc_raw_async = fake_raw
        pp.probe_foreground_music = lambda: None
        pp.probe_claude_code = lambda: None
        signals = asyncio.run(pp.probe_signals_async())
        _eq(signals["music_strict"], None, "VLC strict signal -> none")
        _eq(signals["music_any"], None,
            "VLC never falls through to bot music_any")
        _eq(pp.bot_activity_from_signals(signals), None,
            "VLC local file does not become bot activity")
    finally:
        (pp.probe_game_process, pp.probe_smtc_raw_async,
         pp.probe_foreground_music, pp.probe_claude_code) = saved


def test_rotate_ndjson_tail():
    d = Path(tempfile.mkdtemp())
    big = d / "events.ndjson"
    with big.open("w", encoding="utf-8") as f:
        i = 0
        # plain ints (no leading zeros) so each line is valid JSON
        while big.stat().st_size < 600_000:
            f.write('{"ts":1,"type":"x","n":%d}\n' % i)
            i += 1
    new_size = b._rotate_ndjson_tail(big)
    assert new_size is not None and new_size <= b._NDJSON_ROTATE_KEEP_BYTES, \
        f"rotated size {new_size} should be <= keep bytes"
    txt = big.read_text(encoding="utf-8")
    _eq(txt.startswith("{"), True, "kept tail starts at a whole line")
    import json
    assert all(json.loads(line) for line in txt.splitlines()), \
        "every kept line must parse as JSON"
    print("  OK oversized file trimmed to whole-line tail")
    # small file: untouched
    small = d / "audit.ndjson"
    small.write_text('{"a":1}\n', encoding="utf-8")
    _eq(b._rotate_ndjson_tail(small), None, "small file -> no rotation")
    # missing file: no crash
    _eq(b._rotate_ndjson_tail(d / "nope.ndjson"), None, "missing -> None")


def test_dorossi_parse_session_new():
    """`/dorossi session new` 的參數文法：標籤是自由文字，工作目錄只認 `cwd=`。

    重點在兩個容易踩的地方：標籤可含空格（不能靠位置切）、`cwd=` 之後整段都是
    路徑（可含空格、`:` 與反斜線，而且永不 lower()）。"""
    _eq(db._dorossi_parse_session_new(None), (None, None), "None -> 空")
    _eq(db._dorossi_parse_session_new("   "), (None, None), "空白 -> 空")
    _eq(db._dorossi_parse_session_new("我的專案"), ("我的專案", None), "只有標籤")
    _eq(db._dorossi_parse_session_new("我的 專案 筆記"),
        ("我的 專案 筆記", None), "標籤含空格全留給標籤")
    _eq(db._dorossi_parse_session_new(r"cwd=D:\Work\Foo"),
        (None, r"D:\Work\Foo"), "只有目錄")
    _eq(db._dorossi_parse_session_new(r"標籤 cwd=D:\Work\My Project"),
        ("標籤", r"D:\Work\My Project"), "目錄含空格不被切斷")
    _eq(db._dorossi_parse_session_new(r"標籤 CWD=D:\Work\Foo"),
        ("標籤", r"D:\Work\Foo"), "鍵大小寫不拘、路徑維持原樣")
    _eq(db._dorossi_parse_session_new("標籤 cwd="), ("標籤", None), "空目錄 -> None")
    _eq(db._dorossi_parse_session_new("標籤 dir=D:/x"),
        ("標籤 dir=D:/x", None), "`dir=` 不在這條文法裡，整段當標籤")


def test_token_round_info_split():
    """`_dorossi_cc_round_info` SPLITS input into fresh/cr/cc (not folded) and
    keeps `cost_usd` (B2's compaction trigger depends on it)."""
    ev = {"type": "result", "total_cost_usd": 0.0734,
          "usage": {"input_tokens": 40, "cache_read_input_tokens": 3000,
                    "cache_creation_input_tokens": 12000, "output_tokens": 250}}
    _eq(db._dorossi_cc_round_info(ev),
        {"cost_usd": 0.0734, "in": 40, "cr": 3000, "cc": 12000, "out": 250},
        "split round-info")
    # B2 invariant: cost_usd key always present
    _eq("cost_usd" in db._dorossi_cc_round_info({}), True, "cost_usd present")
    _eq(db._dorossi_cc_round_info({}),
        {"cost_usd": 0.0, "in": 0, "cr": 0, "cc": 0, "out": 0}, "empty -> zeros")
    # bool is an int subclass; must NOT be counted
    _eq(db._dorossi_cc_round_info(
        {"usage": {"cache_read_input_tokens": True}})["cr"], 0, "bool guard")


def test_dorossi_context_tokens():
    """`_dorossi_context_tokens` estimates the resumed-context size as in+cr+cc
    (fresh input + cache read + cache creation), excluding output. Defensive:
    missing keys / bool / non-dict → 0 (reuses `_dorossi_usage_int`)."""
    _eq(db._dorossi_context_tokens(
        {"in": 40, "cr": 3000, "cc": 12000, "out": 250}), 15040, "in+cr+cc")
    _eq(db._dorossi_context_tokens({"in": 10}), 10, "missing cr/cc -> 0")
    _eq(db._dorossi_context_tokens({}), 0, "empty -> 0")
    _eq(db._dorossi_context_tokens(None), 0, "non-dict -> 0")
    # bool is an int subclass; must NOT be counted
    _eq(db._dorossi_context_tokens({"in": True, "cr": 5}), 5, "bool guard")
    # output tokens are NOT part of the resumed-prefix estimate
    _eq(db._dorossi_context_tokens({"out": 99999}), 0, "out excluded")


def test_dorossi_context_compaction_due():
    """Token-threshold trigger: boundary is `>=`, below is False, and 0 disables
    the whole condition (the shared single-turn / loop compaction key)."""
    orig = db.DOROSSI_COMPACT_CONTEXT_TOKENS
    try:
        db.DOROSSI_COMPACT_CONTEXT_TOKENS = 300000
        _eq(db._dorossi_context_compaction_due(299999), False, "below")
        _eq(db._dorossi_context_compaction_due(300000), True, "at threshold (>=)")
        _eq(db._dorossi_context_compaction_due(300001), True, "above")
        db.DOROSSI_COMPACT_CONTEXT_TOKENS = 0
        _eq(db._dorossi_context_compaction_due(10 ** 9), False, "0 = disabled")
    finally:
        db.DOROSSI_COMPACT_CONTEXT_TOKENS = orig


def test_dorossi_loop_compaction_due_token_condition():
    """`_dorossi_loop_compaction_due` ORs the new token condition on top of the
    round/cost conditions; `context_tokens` is keyword-defaulted so old 2-arg
    callers are a no-op for it."""
    o_rounds = db.DOROSSI_LOOP_COMPACT_EVERY_ROUNDS
    o_cost = db.DOROSSI_LOOP_COMPACT_COST_USD
    o_ctx = db.DOROSSI_COMPACT_CONTEXT_TOKENS
    try:
        db.DOROSSI_LOOP_COMPACT_EVERY_ROUNDS = 10
        db.DOROSSI_LOOP_COMPACT_COST_USD = 10.0
        db.DOROSSI_COMPACT_CONTEXT_TOKENS = 300000
        _eq(db._dorossi_loop_compaction_due(3, 2.0, 100), False, "none met")
        _eq(db._dorossi_loop_compaction_due(3, 2.0, 300000), True, "token met")
        # old 2-arg call: token condition defaults to 0 -> no-op
        _eq(db._dorossi_loop_compaction_due(3, 2.0), False, "2-arg default no-op")
        _eq(db._dorossi_loop_compaction_due(10, 0.0), True, "rounds still fire")
        _eq(db._dorossi_loop_compaction_due(0, 10.0), True, "cost still fires")
        # token disabled -> huge context no longer triggers by itself
        db.DOROSSI_COMPACT_CONTEXT_TOKENS = 0
        _eq(db._dorossi_loop_compaction_due(3, 2.0, 10 ** 9), False,
            "token disabled")
    finally:
        db.DOROSSI_LOOP_COMPACT_EVERY_ROUNDS = o_rounds
        db.DOROSSI_LOOP_COMPACT_COST_USD = o_cost
        db.DOROSSI_COMPACT_CONTEXT_TOKENS = o_ctx


def test_token_record_read_roundtrip():
    """Record (new split schema) → read round-trips every bucket; malformed and
    blank lines are skipped; `limit` returns the LAST n; missing file -> []."""
    orig = db.DOROSSI_USAGE_FILE
    tmp = Path(tempfile.mkdtemp()) / "usage.ndjson"
    db.DOROSSI_USAGE_FILE = tmp
    try:
        db._dorossi_record_usage(
            {"in": 40, "cr": 3000, "cc": 12000, "out": 250, "cost_usd": 0.07})
        db._dorossi_record_usage({"in": 10, "out": 20})  # cr/cc default 0
        with tmp.open("a", encoding="utf-8") as f:
            f.write("not json\n\n")  # garbage + blank
        recs = db._dorossi_read_usage(50)
        _eq(len(recs), 2, "garbage/blank skipped")
        r0 = recs[0]
        _eq((r0["in"], r0["cr"], r0["cc"], r0["out"]), (40, 3000, 12000, 250),
            "row0 buckets")
        _eq(all(k in r0 for k in ("ts", "in", "cr", "cc", "out", "cost_usd")),
            True, "full schema written")
        r1 = recs[1]
        _eq((r1["in"], r1["cr"], r1["cc"], r1["out"]), (10, 0, 0, 20),
            "row1 missing cache -> 0")
        _eq(len(db._dorossi_read_usage(1)), 1, "limit returns last n")
        _eq(db._dorossi_read_usage(1)[0]["in"], 10, "last n is newest")
        tmp.unlink()
        _eq(db._dorossi_read_usage(10), [], "missing file -> []")
    finally:
        db.DOROSSI_USAGE_FILE = orig


def test_token_record_split_backward_compat():
    """`_token_record_split` detects new vs LEGACY records. A legacy `in`
    (undecomposed total, no cr/cc keys) goes to the neutral `legacy` bucket — it
    is NOT misreported as fresh input. Garbage never crashes."""
    new = {"ts": 2.0, "in": 40, "cr": 3000, "cc": 12000, "out": 250,
           "cost_usd": 0.07}
    legacy = {"ts": 1.0, "in": 15040, "out": 250, "cost_usd": 0.07}
    sn = b._token_record_split(new)
    _eq(sn["is_new"], True, "new detected")
    _eq((sn["fresh"], sn["cr"], sn["cc"], sn["legacy"]), (40, 3000, 12000, 0),
        "new buckets")
    sl = b._token_record_split(legacy)
    _eq(sl["is_new"], False, "legacy detected")
    _eq((sl["fresh"], sl["cr"], sl["cc"], sl["legacy"]), (0, 0, 0, 15040),
        "legacy -> neutral bucket, not fresh")
    _eq(sl["cost"], 0.07, "cost parsed")
    # garbage / non-dict -> all zeros, no crash
    _eq(b._token_record_split({"ts": 1})["legacy"], 0, "no in -> 0")
    _eq(b._token_record_split("nope")["out"], 0, "non-dict -> 0")
    _eq(b._token_record_split({"in": True})["legacy"], 0, "bool guard")


def test_token_render_png():
    """`_render_token_usage_png` returns a valid PNG for mixed legacy+new,
    all-legacy, all-new, and single-record inputs (no crash on any shape)."""
    new = {"in": 40, "cr": 3000, "cc": 12000, "out": 250, "cost_usd": 0.07}
    legacy = {"in": 15040, "out": 250, "cost_usd": 0.07}
    sig = b"\x89PNG\r\n\x1a\n"
    for label, recs in [("mixed", [legacy, new, {"in": 5, "cr": 100, "cc": 0,
                                                 "out": 7, "cost_usd": 0.001}]),
                        ("all-new", [new] * 5),
                        ("all-legacy", [legacy] * 4),
                        ("single", [new])]:
        png = b._render_token_usage_png(recs)
        assert png[:8] == sig and len(png) > 1000, f"{label}: bad PNG"
        print(f"  OK render {label} -> {len(png)} bytes")


# ---------------------------------------------------------------------------
def test_dorossi_session_key_and_by_id():
    """per-session 並行原語：session key 形狀、按 id 取 slot，以及「slot 消失 →
    退回 active」的 fallback（並行回合路徑靠它在每次 state 重載後重抓正確 slot，
    不持有 live slot 參照跨越後端呼叫）。"""
    _eq(b._dorossi_session_key("u1", "s1"), ("u1", "s1"), "session_key")
    state: dict = {}
    rec = b._dorossi_user_record(state, "u1")
    s1 = b._dorossi_new_session(rec)
    s2 = b._dorossi_new_session(rec)
    got_sid, got = b._dorossi_session_by_id(state, "u1", s1)
    _eq(got_sid, s1, "by_id exact")
    assert got is rec["sessions"][s1], "by_id must return the live slot"
    fb_sid, _fb = b._dorossi_session_by_id(state, "u1", "s_does_not_exist")
    _eq(fb_sid, rec["active"], "by_id fallback -> active")
    print(f"  OK by_id fallback active={fb_sid} (s1={s1}, s2={s2})")


def test_dorossi_session_lock_refcount():
    """ref-count per-session 鎖：同 key 多次 acquire 共用同一把鎖並累加 ref；
    release 未歸零時保留鎖，歸零才回收鎖物件與該 key 的 waiter 佇列（避免多
    session 下 map 無限長大）。"""
    key = ("__test_uid__", "__test_sid__")
    for m in (b._dorossi_session_locks, b._dorossi_session_lock_refs,
              b._dorossi_waiters):
        m.pop(key, None)
    lk1 = b._dorossi_acquire_session_lock(key)
    lk2 = b._dorossi_acquire_session_lock(key)
    assert lk1 is lk2, "same key must reuse one lock"
    _eq(b._dorossi_session_lock_refs[key], 2, "refs after 2 acquire")
    b._dorossi_waiters[key] = ["x"]  # 模擬有 waiter，確認歸零時一併回收
    b._dorossi_release_session_lock(key)
    _eq(b._dorossi_session_lock_refs.get(key, 0), 1, "refs after 1 release")
    assert key in b._dorossi_session_locks, "lock kept while refs remain"
    b._dorossi_release_session_lock(key)
    assert key not in b._dorossi_session_lock_refs, "refs cleared at 0"
    assert key not in b._dorossi_session_locks, "idle lock reclaimed at 0"
    assert key not in b._dorossi_waiters, "waiter queue reclaimed at 0"
    print("  OK refcount acquire/release reclamation")


def test_dorossi_per_session_lock_serialises():
    """行為級驗證（本功能核心承諾）：同一 (uid,sid) 的兩個回合被 per-session 鎖
    『序列化』（同時最多 1 個在臨界區）；不同 key 的兩個回合可『並行』（同時達 2
    個）；回合結束後鎖與 ref-count 都乾淨回收、不外洩。"""
    import asyncio

    async def _run(keys):
        for k in set(keys):  # 防禦：清掉前次殘留
            for m in (b._dorossi_session_locks, b._dorossi_session_lock_refs,
                      b._dorossi_waiters):
                m.pop(k, None)
        state = {"inside": 0, "peak": 0}

        async def worker(key):
            lock = b._dorossi_acquire_session_lock(key)
            try:
                async with lock:
                    state["inside"] += 1
                    state["peak"] = max(state["peak"], state["inside"])
                    await asyncio.sleep(0.02)  # 逼出 yield 讓另一個有機會插入
                    state["inside"] -= 1
            finally:
                b._dorossi_release_session_lock(key)

        await asyncio.gather(*(worker(k) for k in keys))
        return state["peak"]

    same = ("uL", "sX")
    _eq(asyncio.run(_run([same, same])), 1, "same key serialised (peak inside)")
    _eq(asyncio.run(_run([("uL", "sA"), ("uL", "sB")])), 2,
        "different keys parallel (peak inside)")
    for k in (same, ("uL", "sA"), ("uL", "sB")):
        assert k not in b._dorossi_session_lock_refs, f"refs leaked for {k}"
        assert k not in b._dorossi_session_locks, f"lock leaked for {k}"
    print("  OK per-session serialise / cross-session parallel + clean reclaim")


def test_dorossi_resolve_cc_workdir():
    """每個 session 各自的隔離工作目錄（讓不同 session 的 claude -p 行程可安全平行——
    Claude Code 以 cwd-hash 索引 --resume store，同一 cwd 兩個 agent 會互相污染）。
    驗證：全新 session → 專屬隔離目錄並持久化到 cc_workdir；既有 session（已有
    cc_session_id、尚無 cc_workdir）→ 沿用共用預設目錄（grandfather，保住既有 resume）；
    已指定過就重用；使用者顯式 cc_cwd override 優先且不覆寫 cc_workdir；不同 slot →
    不同目錄。"""
    shared = str(b.DOROSSI_CC_WORKDIR)
    # 全新 session：隔離目錄、寫入 cc_workdir。
    fresh: dict = {}
    wd1 = b._dorossi_resolve_cc_workdir(fresh, "u1", "s1")
    _eq(wd1, b.dorossi_session_workdir("u1", "s1"), "fresh -> isolated dir")
    _eq(fresh.get("cc_workdir"), wd1, "persisted cc_workdir")
    assert wd1 != shared, "fresh must NOT use the shared dir"
    # 重用：cc_workdir 已設 → 原樣回。
    _eq(b._dorossi_resolve_cc_workdir({"cc_workdir": "X"}, "u1", "s1"), "X", "reuse")
    # grandfather：已有 cc_session_id、無 cc_workdir → 共用目錄（保 resume）。
    old = {"cc_session_id": "cc-OLD"}
    _eq(b._dorossi_resolve_cc_workdir(old, "u1", "s2"), shared, "grandfather->shared")
    _eq(old.get("cc_workdir"), shared, "grandfather persisted")
    # override 優先、且不寫 cc_workdir。
    ov = {"cc_cwd": "/tmp/mydir"}
    _eq(b._dorossi_resolve_cc_workdir(ov, "u1", "s1"), "/tmp/mydir", "override wins")
    assert "cc_workdir" not in ov, "override must not persist cc_workdir"
    # 不同 slot → 不同隔離目錄。
    assert (b.dorossi_session_workdir("u1", "s1")
            != b.dorossi_session_workdir("u1", "s2")), "distinct slots distinct dirs"
    print("  OK per-session workdir isolation + grandfather + override")


def test_dorossi_persist_advance_fail_closed():
    """回合結束存檔一律 fail-closed（並行安全）：目標 slot 若在後端呼叫期間被刪掉，
    存檔必須是 no-op、回 False，且**絕不**把本回合的 cc_session_id 寫進別的（此時已
    成為 active 的）slot——否則那個 slot 下次會 `--resume` 到錯的對話。slot 還在時
    正常寫入並回 True。turn 路徑與自走迴圈共用這個單一來源。"""
    state: dict = {}
    rec = b._dorossi_user_record(state, "u1")
    s1 = b._dorossi_new_session(rec)
    s2 = b._dorossi_new_session(rec)  # 之後會成為 active
    # 正常路徑：slot 還在 → 寫入並回 True。
    ok = b._dorossi_persist_advance(
        state, "u1", s1, new_sid="cc-XYZ",
        new_hist=[{"role": "user"}], codex_sid="cx-XYZ")
    assert ok is True, "existing slot must persist"
    _eq(rec["sessions"][s1]["cc_session_id"], "cc-XYZ", "wrote cc into s1")
    _eq(rec["sessions"][s1]["api_history"], [{"role": "user"}], "wrote hist s1")
    _eq(rec["sessions"][s1]["codex_session_id"], "cx-XYZ", "wrote codex id s1")
    # fail-closed：slot 被刪 → no-op、回 False、不得污染 active(s2)。
    del rec["sessions"][s1]
    before = rec["sessions"][s2].get("cc_session_id")
    ok2 = b._dorossi_persist_advance(state, "u1", s1, new_sid="cc-LEAK")
    assert ok2 is False, "vanished slot must fail closed (return False)"
    _eq(rec["sessions"][s2].get("cc_session_id"), before,
        "must NOT pollute the now-active slot")
    print("  OK persist_advance fail-closed (no cross-slot pollution)")


def test_collect_codex_images_is_scoped_and_incremental():
    """Only new image files in the exact thread directory are attachable."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        folder = root / "thread_12345678"
        folder.mkdir()
        old = folder / "old.png"
        old.write_bytes(b"old")
        # Use explicit, well-separated mtimes.  On Windows the filesystem may
        # quantize a freshly written file's mtime below a time.time_ns() cutoff,
        # which makes this security-boundary test nondeterministic.
        cutoff = time.time_ns()
        os.utime(old, ns=(cutoff - 2_000_000_000, cutoff - 2_000_000_000))
        new = folder / "new.webp"
        new.write_bytes(b"new")
        os.utime(new, ns=(cutoff + 2_000_000_000, cutoff + 2_000_000_000))
        junk = folder / "secret.txt"
        junk.write_text("no", encoding="utf-8")
        got = db._collect_codex_images(
            "thread_12345678", cutoff, root=root)
        _eq(got, [str(new)], "new image only")
        _eq(db._collect_codex_images("../escape", 0, root=root), [],
            "reject unsafe thread id")


def test_collect_codex_images_rejects_a_thread_folder_that_links_outside():
    """`base not in folder.parents` 那一道的**專屬**對照組。

    ⚠️ 為什麼不能靠既有那一支：它唯一的拒絕案例是 `"../escape"`，而那個字串在
    `re.fullmatch(r"[A-Za-z0-9_-]{8,80}", ...)` 就被擋掉了（`.` 與 `/` 都不在字元類
    裡），**根本走不到 containment 那一行**。也就是說把 `base not in folder.parents`
    整條刪掉，那支測試照樣綠——兩道守門互相遮蔽，刪掉被遮住的那道沒有任何症狀。

    所以這裡的 thread id 是**合法的**（regex 過得了），逃逸靠的是一個目錄連結。
    那正是 regex **結構上不可能**擋住、而 containment 檢查唯一存在理由的那一種：
    `.resolve()` 之後 folder 落在 root 外面。

    實測（2026-09-11，本機）拿掉 containment 檢查的後果不是抽象的：連結目標裡的
    `.png` 會被 `iterdir()` 撿起來，變成送進聊天室的附件。
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "codexroot"
        root.mkdir()
        # ⚠️ 目標刻意取成**字首相同的兄弟目錄**，不是隨便一個 `outside/`。
        # `base not in folder.parents` 與那個經典錯法
        # `str(folder).startswith(str(base))` 在「目標字首不同」時答案一樣，所以用
        # `outside/` 當目標的話，兩種寫法都會過，等於沒測到 `in parents` 的價值
        # （實測：變異 C2 就是這樣存活的）。`codexroot_evil` 以 `codexroot` 為字首，
        # 只有正確的那個寫法擋得住。
        outside = Path(td) / "codexroot_evil"
        outside.mkdir()
        (outside / "stolen.png").write_bytes(b"x")
        assert str(outside).startswith(str(root)), (
            "這個測試的價值全靠『目標以 base 為字首』，這一行是它的自我檢查——"
            "哪天有人改了目錄名而失去這個性質，C2 那種變異會靜靜地復活。")

        thread_id = "escapelink"          # 8–80 碼、全合法字元：regex 一定放行

        # 正對照組先跑，而且它做兩件事：
        # (1) 同一個 id、真的目錄時**必須**撈得到圖——沒有這一段的話，下面的
        #     `== []` 與「這個函式永遠回空 list」是同一個結果。
        # (2) 順帶證明這個 id **確實通過了 regex**（通不過的話這裡就回空 list 了）。
        #     所以這支不需要、也不該自己再抄一份 regex——抄一份就是多一個會漂移的
        #     地方，而且抄錯的方向剛好是「測試退化成在測 regex」。
        real = root / thread_id
        real.mkdir()
        (real / "ok.png").write_bytes(b"y")
        got = db._collect_codex_images(thread_id, 0, root=root)
        _eq(got, [str(real / "ok.png")], "正對照：真的目錄要撈得到圖")

        # 換成指向外面的連結
        for path in real.iterdir():
            path.unlink()
        real.rmdir()
        # `_dir_link` 定義在本檔後段（`test_within_allowed_roots_resolves_the_root_too`
        # 附近）。刻意共用而不是再寫一支：同一個檔案裡兩支同義的輔助函式，下一個人
        # 只會改到其中一支。
        if not _dir_link(real, outside):
            pytest.skip("這個環境造不出目錄連結（symlink 與 junction 都失敗）")

        try:
            assert db._collect_codex_images(thread_id, 0, root=root) == [], (
                "thread 目錄是一個指向 root 外面的連結，`_collect_codex_images` 卻"
                "還是回傳了檔案——`base not in folder.parents` 那一道沒有生效。"
                "連結目標裡的圖會被當成附件送進聊天室。")
        finally:
            try:
                real.rmdir()
            except OSError:
                pass

        # --- 另一道守門的專屬對照組（遮蔽是雙向的）---------------------------
        # 既有那支唯一的拒絕案例 `"../escape"` 被**兩道**守門同時擋住，所以把 regex
        # 放寬之後 containment 會接手、結果不變——regex 這一道從來沒有被單獨測過
        # （實測：變異 C4 存活）。要殺它，輸入必須是「regex 擋、containment 會放行」：
        # 字元全合法、目錄真的在 root 底下，只是**太短**（下限 8 碼）。
        short = root / "ab"
        short.mkdir()
        (short / "ok.png").write_bytes(b"z")
        assert db._collect_codex_images("ab", 0, root=short.parent) == [], (
            "`ab` 的每個字元都合法、目錄也真的在 root 底下，擋它的只有 regex 的"
            "長度下限。這一行紅掉代表 `[A-Za-z0-9_-]{8,80}` 被放寬了，而放寬它"
            "等於讓控制字元與超長字串走到路徑接合那一步。")
    print("  OK codex thread folder cannot link outside the root")


def test_dorossi_cc_max_parallel_clamp():
    """安全不變式：`dorossi_cc_max_parallel` 決定並行後端號誌的 permit 數，必須恆
    ≥1——若被設成 0／負數／非整數而讓 asyncio.Semaphore 拿到 0，每一個 Dorossi 回合
    都會永久卡死（號誌永遠放行不了）。驗證 loader 對每種壞值都退回安全預設（≥1）、
    合法值原樣通過，且實際載入與 bot 模組實際採用的值都 ≥1。"""
    import _bot_config as bc
    d = bc._DEFAULT_BOT_CONFIG["dorossi_cc_max_parallel"]
    assert isinstance(d, int) and d >= 1, "default itself must be a positive int"
    # 壞值（0／負數／非整數／bool）一律退回預設（仍 ≥1）——bool 要在 int 之前擋掉。
    for bad in (0, -1, -999, 1.5, "3", None, True, False, [3]):
        got = bc._coerce_int(bad, d, min_value=1)
        _eq(got, d, f"bad {bad!r} -> default")
        assert got >= 1, f"clamp broken for {bad!r}: {got}"
    # 合法值原樣通過。
    for good in (1, 2, 5, 64):
        _eq(bc._coerce_int(good, d, min_value=1), good, f"good {good}")
    # 端到端：不管磁碟上的 bot_config 內容為何，實際 load 出來的值恆為正整數；bot
    # 模組實際用來建號誌的 DOROSSI_CC_MAX_PARALLEL 也必須 ≥1（否則號誌會卡死一切）。
    live = bc.load_bot_config()["dorossi_cc_max_parallel"]
    assert isinstance(live, int) and live >= 1, f"live config unsafe: {live!r}"
    assert b.DOROSSI_CC_MAX_PARALLEL >= 1, "bot semaphore size must be >= 1"
    print(f"  OK cc_max_parallel clamp (default={d}, live={live}, "
          f"sem={b.DOROSSI_CC_MAX_PARALLEL})")


# ---------------------------------------------------------------------------
def test_dorossi_parse_turn_flags():
    """`_dorossi_parse_turn_flags`：微調指令（`/effort`、`/model`）只認提問
    「開頭」的 token、解析後剝除、無效值記進 errors；`/model` 的合法值＝
    DOROSSI_MODEL_CHOICES 的 allowlist key（後端模型別名——擁有者裁決的窄範圍保密
    例外）或 "default"。**allowlist 驗證不可拿掉**：使用者輸入永不原樣進 CLI。"""
    parse_full = db._dorossi_parse_turn_flags

    def parse(text):
        """既有斷言用的 4-tuple 檢視 (effort, model, cleaned, errors)。

        順便把「這些輸入都不該解析出 session」釘住——`/session` 的正向行為在下面
        自己的區塊測。"""
        eff, mod, sid, rest, errs = parse_full(text)
        assert sid is None, f"unexpected /session in {text!r}: {sid!r}"
        return eff, mod, rest, errs

    # 無指令：原文原樣、無 error。
    _eq(parse("今天天氣如何"), (None, None, "今天天氣如何", []), "no flags")
    _eq(parse(""), (None, None, "", []), "empty")
    # 單一指令 ＋ 剝除。
    _eq(parse("/effort high 問題內容"), ("high", None, "問題內容", []),
        "effort stripped")
    _eq(parse("/model sonnet 問題"), (None, "sonnet", "問題", []),
        "model returns validated allowlist key")
    # 兩者並用、順序不拘。
    _eq(parse("/effort max /model sonnet Q"),
        ("max", "sonnet", "Q", []), "both, order A")
    _eq(parse("/model opus /effort low Q"),
        ("low", "opus", "Q", []), "both, order B")
    # 大小寫不拘（指令與值都 lower 後比對）。
    _eq(parse("/EFFORT High /Model OPUS q"),
        ("high", "opus", "q", []), "case-insensitive")
    # 全部合法 effort 值／全部 allowlist 模型別名都收。
    for lv in db.DOROSSI_EFFORT_LEVELS:
        _eq(parse(f"/effort {lv} q")[0], lv, f"effort {lv}")
    for mk in db.DOROSSI_MODEL_CHOICES:
        _eq(parse(f"/model {mk} q")[1], mk, f"model {mk}")
    # `default` ＝清除該 session 覆寫的合法字面值（兩個指令都收）。
    _eq(parse("/effort default q"), ("default", None, "q", []),
        "effort default accepted")
    _eq(parse("/model DEFAULT q"), (None, "default", "q", []),
        "model default accepted (case-insensitive)")
    # 重複指令：後者覆蓋前者。
    _eq(parse("/effort low /effort max q")[0], "max", "duplicate last-wins")
    # 無效值 → errors 記 (kind, 原始值)，token 照樣被消耗、其餘照常解析。
    eff, mod, rest, errs = parse("/effort turbo /model haiku 問題")
    _eq((eff, mod, rest), (None, "haiku", "問題"), "invalid effort consumed")
    _eq(errs, [("effort", "turbo")], "invalid effort recorded")
    eff, mod, rest, errs = parse("/model bogus q")  # allowlist 外一律拒絕
    _eq((eff, mod, rest), (None, None, "q"), "non-allowlist value rejected")
    _eq(errs, [("model", "bogus")], "invalid model recorded")
    # 舊版通用階層 key 只在「讀取 store」時遷移（見 session_tuning 測試）；當
    # 「輸入」打舊 key 一律無效，錯誤訊息會列出新的 allowlist 值。
    _eq(parse("/model fast q")[3], [("model", "fast")],
        "legacy tier key rejected as input")
    # 指令在句尾沒帶值 → error、cleaned 為空。
    _eq(parse("/effort"), (None, None, "", [("effort", "")]), "missing value")
    # 只有指令沒有提問內容 → cleaned 為空字串（呼叫端當純設定更新處理）。
    _eq(parse("/effort high"), ("high", None, "", []), "flags only")
    # 指令出現在句中（非開頭）不誤判、原文原樣保留。
    mid = "請解釋 /effort high 是什麼意思"
    _eq(parse(mid), (None, None, mid, []), "mid-prompt untouched")
    # 開頭遇到非指令 token 就停止——之後的指令屬於提問內容。
    _eq(parse("/effort high 先答這題 /model opus"),
        ("high", None, "先答這題 /model opus", []), "stop at first non-flag")
    # reset 關鍵字不是微調指令：`/new` 原樣通過（交給既有 reset 解析）。
    _eq(parse("/new"), (None, None, "/new", []), "reset keyword passes through")
    # --- `/session <id>`：本輪級指定，不寫 store、不改 active ----------------
    _eq(parse_full("/session s3 問題"), (None, None, "s3", "問題", []),
        "session parsed and stripped")
    _eq(parse_full("/SESSION S12 q"), (None, None, "s12", "q", []),
        "session case-insensitive, normalised to lower")
    # 與其他指令順序不拘、可混用。
    _eq(parse_full("/effort high /session s2 /model opus Q"),
        ("high", "opus", "s2", "Q", []), "session mixes with other flags")
    _eq(parse_full("/session s2 /effort low Q"),
        ("low", None, "s2", "Q", []), "session first")
    # 重複：後者覆蓋前者。
    _eq(parse_full("/session s1 /session s9 q")[2], "s9",
        "duplicate session last-wins")
    # 非法代號 → error、token 照樣消耗；`new` 是保留字，不能當代號。
    _eq(parse_full("/session nope q"), (None, None, None, "q",
                                        [("session", "nope")]),
        "non-id rejected")
    _eq(parse_full("/session new q"), (None, None, None, "q",
                                       [("session", "new")]),
        "reserved word `new` is not a session id")
    _eq(parse_full("/session"), (None, None, None, "", [("session", "")]),
        "session missing value")
    # 句中的 `/session` 不誤判。
    mid_s = "請問 /session s3 是什麼"
    _eq(parse_full(mid_s), (None, None, None, mid_s, []),
        "mid-prompt /session untouched")
    # 只打 `/session` 不打提問 → cleaned 為空（呼叫端當純設定更新／無提問處理）。
    _eq(parse_full("/session s4"), (None, None, "s4", "", []), "session only")
    # Allowlist 完整性：非空、值皆為字串、預設模型本身在表內（`/model <預設>` 也
    # 要能明確指定）；legacy 對照的目標必須全是現行 allowlist key。
    assert db.DOROSSI_MODEL_CHOICES, "model allowlist must be non-empty"
    assert all(isinstance(v, str) and v
               for v in db.DOROSSI_MODEL_CHOICES.values())
    assert db.DOROSSI_CC_MODEL in db.DOROSSI_MODEL_CHOICES, \
        "default model must be selectable via /model"
    assert all(v in db.DOROSSI_MODEL_CHOICES
               for v in db.DOROSSI_LEGACY_MODEL_KEYS.values()), \
        "legacy keys must map onto current allowlist keys"
    # "default" 不可撞到任何合法值／allowlist key／legacy key（否則清除語法會
    # 遮蔽正常設定或遷移）。
    assert db.DOROSSI_TUNE_DEFAULT not in db.DOROSSI_EFFORT_LEVELS
    assert db.DOROSSI_TUNE_DEFAULT not in db.DOROSSI_MODEL_CHOICES
    assert db.DOROSSI_TUNE_DEFAULT not in db.DOROSSI_LEGACY_MODEL_KEYS


# ---------------------------------------------------------------------------
class _FakeAuthor:
    def __init__(self, uid):
        self.id = uid


class _FakeMessage:
    """最小 message 替身：閘門只讀 `author.id`。"""

    def __init__(self, uid):
        self.author = _FakeAuthor(uid)


def test_dorossi_loop_intent_phrases():
    """自走（循環）模式的「片語快速路徑」：擁有者用**本專案自己的稱呼**下令時必須
    命中。回歸守則——清單少了「循環模式／迴圈模式」這類指稱本模式的講法時，`@bot
    幫我…，進入循環模式` 會靜靜地退回單輪問答（外觀上就是「沒進入循環模式」）。
    同時守住刻意保留的保守面：裸詞「循環／迴圈」不可命中，否則 full 模式下問程式
    迴圈的問題會誤觸。"""
    m = db._dorossi_matches_loop_intent
    # 本模式的專案稱呼（擁有者實際會打的）。
    jargon = ("進入循環模式", "循環模式", "用循環模式做完",
              "幫我把測試補齊，進入循環模式", "迴圈模式",
              "進入自走模式", "自走", "無人值守做到完成",
              "loop mode", "run in autonomous mode")
    for p in jargon:
        assert m(p), f"loop intent must match project jargon: {p!r}"
    print(f"  OK project-jargon phrases all match ({len(jargon)} cases)")
    # 既有泛用講法不可回歸。
    for p in ("不要問我，做到完成", "一直循環", "自走循環", "持續推進",
              "don't ask me, just finish it", "keep looping until done"):
        assert m(p), f"existing loop phrase regressed: {p!r}"
    print("  OK generic phrases still match")
    # 保守面：裸詞與一般請求不可誤觸。
    # 保守面：裸詞、描述「程式行為」的講法、一般請求都不可誤觸（誤觸＝白跑好幾輪、
    # 燒 token）。
    for p in ("這個 for 迴圈有 bug 嗎", "這段循環怎麼寫", "幫我修好這個 bug",
              "解釋一下事件循環", "程式進入循環後就卡住", "這裡會自動循環嗎",
              "從這裡開始循環對嗎", "", "今天天氣如何"):
        assert not m(p), f"must NOT trip loop intent: {p!r}"
    print("  OK bare 循環/迴圈, program-behavior wording and ordinary asks do not trip")


def _loop_intent_single_hits(prompt: str) -> list[str]:
    """單一片語表裡命中 `prompt` 的那些（跟比對器同一種大小寫處理）。"""
    low = prompt.lower()
    return [s for s in db._DOROSSI_LOOP_INTENT_SUBSTRINGS if s in prompt or s in low]


def test_dorossi_loop_intent_does_not_fire_on_ordinary_questions():
    """誤觸的代價是一個在主機上無人值守的迴圈，所以一般問句一律不能命中。

    2026-09-22 以前配對表是「兩個字串出現在任何位置、任何順序」就算，下面前五句全部
    命中——其中「先做到這裡為止就好」的意思正好相反。整個套件沒有發現，是因為有配對的
    測試句都先被單一片語命中，配對那條分支一次都沒有回過 True；所以正面語料這裡**先斷言
    單一片語表一個都沒命中**，證明命中的真的是配對規則。"""
    m = db._dorossi_matches_loop_intent
    only_pairs = ("一直做到全部測試都完成", "做到全部綠燈為止", "持續補測試直到完成",
                  "做到沒有錯誤為止", "持續修到完成", "持續到完成",
                  "做到全部測試通過、lint 也乾淨為止")
    for phrase in only_pairs:
        assert not _loop_intent_single_hits(phrase), (
            f"{phrase!r} is meant to exercise the PAIR rule, but single phrases "
            f"{_loop_intent_single_hits(phrase)} already match it")
        assert m(phrase), f"true loop intent must still match: {phrase!r}"
    print(f"  OK {len(only_pairs)} pair-only intents match through the pair rule")
    ordinary = (
        # 2026-09-21 實測誤觸的五句
        "你目前為止做到哪裡了？", "先做到這裡為止就好", "到此為止，做到這樣就夠了",
        "持續整合的設定完成了嗎", "這個工作持續多久才完成？",
        # 同一類的變形：講停在哪裡、講過去、問句
        "做到今天為止", "我一直做到半夜才完成", "做到哪裡為止？", "到現在為止做到第幾項了",
        "至今為止做到的部分有哪些", "一直做到這裡就完成了", "持續性的工作做到一半為止",
        "持續整合跑到一半完成了嗎", "做到這一步為止。然後完成報告",
        # 只有「持續…完成」中間要有「到」擋得住的一句
        "持續一週的活動完成了嗎",
        # 單一片語的誤觸：編輯器術語、描述程式卡住的講法、程式問題的英文講法
        "VS Code 的自動完成怎麼關掉", "這段程式為什麼會不斷循環",
        "為什麼一直迴圈停不下來", "動畫反覆循環播放要怎麼關",
        "how do I loop until the list is empty in python", "why does this loop forever",
        "why does it keep looping", "how to install without asking for confirmation",
        "what have you done so far?", "stop here for now",
    )
    hits = [phrase for phrase in ordinary if m(phrase)]
    assert not hits, f"ordinary questions must NOT start the autonomous loop: {hits}"
    print(f"  OK {len(ordinary)} ordinary questions do not trip the loop")


def test_dorossi_loop_mode_names_in_a_question_are_about_the_mode():
    """本模式的名字出現在問句裡是在**問**這個功能——第一句是 2026-09-21 真的起了一個
    自走迴圈的原文。但問句形式的客氣下令（名字前面緊接著啟動動詞）照樣要算，因為本機
    關掉了後端自判，這裡漏掉就不會進迴圈。"""
    m = db._dorossi_matches_loop_intent
    asking = (
        "現在是否已經支援 Discord 上的平行多個執行 /dorossi ask 或自走模式",
        "自走模式可以平行跑嗎？", "自走模式怎麼開？", "要怎麼進入自走模式？",
        "循環模式跟一般模式差在哪？", "迴圈模式有沒有回合上限", "自走循環會不會自己停",
        "what is autonomous mode?", "is loop mode supported?",
        "how do I enter loop mode?",
    )
    hits = [p for p in asking if m(p)]
    assert not hits, f"asking ABOUT the mode must not start it: {hits}"
    commanding = (
        "開啟自走模式繼續完成 Progress", "可以進入自走模式幫我把測試補完嗎？",
        "用循環模式把 Progress 做完", "進入自走模式", "自走", "自走循環",
        "幫我把測試補齊，進入循環模式", "能不能用迴圈模式跑完這一批",
        "can you run in autonomous mode until the suite is green?", "loop mode",
    )
    misses = [p for p in commanding if not m(p)]
    assert not misses, f"a command naming the mode must still start it: {misses}"
    print(f"  OK {len(asking)} questions about the mode stay single-turn, "
          f"{len(commanding)} commands still loop")


def test_dorossi_loop_mode_name_rule_synthetic_control():
    """名字規則本身，一條一條拆開測：不是問句就算；問句裡有「怎麼／如何」就不算；問句裡
    名字前面緊接著啟動動詞才算，隔開就不算；同一句裡第二次出現的名字也要看。"""
    counts = db._dorossi_loop_mode_name_counts
    assert counts("進入自走模式", "自走")
    assert not counts("自走可以嗎", "自走")
    assert counts("可以進入自走嗎", "自走")
    assert not counts("可以進入到自走嗎", "自走"), "the verb must sit right before the name"
    assert not counts("要怎麼進入自走嗎", "自走"), "a how-question is about the mode"
    assert counts("自走好嗎，先開自走嗎", "自走"), (
        "a later occurrence in the SAME question sentence can carry the verb")
    assert counts("自走怎麼用？進入自走模式", "自走"), (
        "a how-question sentence is skipped, not the verdict for the whole text")
    assert counts("我不知道為什麼測試會紅？進入自走模式把它修好", "自走"), (
        "the question mark and 為什麼 belong to the first sentence, not the command")
    assert counts("進入自走模式。為什麼上次停了？", "自走"), "a statement sentence counts"
    assert not counts("自走嗎？為什麼", "自走")
    assert counts("先說明一下。可以開自走嗎", "自走"), "verb adjacency inside the question sentence"
    for marker in db._DOROSSI_QUESTION_MARKERS:
        assert not counts(f"說明自走{marker}", "自走"), f"{marker!r} must mark a question"
    print("  OK mode-name rule: statement / how-question / verb adjacency / later occurrence")


def test_dorossi_loop_intent_masks_hide_unrelated_compounds():
    """`_DOROSSI_LOOP_INTENT_MASKS` 裡的每一個複合詞都要有一句**只有遮罩擋得住**的
    例句——不然刪掉那一筆遮罩照樣全綠。同一句另外寫的真正片語照樣要命中。"""
    m = db._dorossi_matches_loop_intent
    only_the_mask_stops = {
        "自走砲": "HOI4 的自走砲要怎麼配",
        "自走炮": "自走炮和牽引炮差在哪",
        "自走式": "自走式割草機推薦哪一台",
        "持續整合": "持續整合跑到完成要多久",
        "持續時間": "持續時間到完成大概多久",
        "持續性": "持續性檢查跑到完成了嗎",
    }
    assert set(only_the_mask_stops) == set(db._DOROSSI_LOOP_INTENT_MASKS), (
        "every mask needs a sentence only it can stop, and every sentence here "
        "must name a real mask")
    for mask, sentence in only_the_mask_stops.items():
        assert not m(sentence), f"{mask!r} must hide {sentence!r}"
    # 反證：拿掉全部遮罩之後每一句都要命中，否則那一句根本不需要遮罩、測不到它。
    saved = db._DOROSSI_LOOP_INTENT_MASKS
    try:
        db._DOROSSI_LOOP_INTENT_MASKS = ()
        unmasked = [s for s in only_the_mask_stops.values() if not m(s)]
    finally:
        db._DOROSSI_LOOP_INTENT_MASKS = saved
    assert not unmasked, (
        f"these sentences would not trip the loop even without masks, so they "
        f"cannot prove their mask works: {unmasked}")
    for mask in db._DOROSSI_LOOP_INTENT_MASKS:
        triggers = list(db._DOROSSI_LOOP_INTENT_SUBSTRINGS) + [
            w for head, tail, _ in db._DOROSSI_LOOP_INTENT_PAIRS for w in (head, tail)]
        assert any(t in mask for t in triggers), (
            f"mask {mask!r} contains no trigger word, so it can never change a result")
    assert m("自走砲的模板弄好之後進入自走模式"), "a real phrase next to a masked word still counts"
    print(f"  OK {len(only_the_mask_stops)} masks each stop exactly their own sentence")


def test_dorossi_loop_pair_rule_synthetic_control():
    """配對規則本身，用一組**不在表裡**的開頭／結尾測——這條分支在真實語料上曾經一次都
    沒有被走到，所以它要有自己的對照組，不能只靠真實片語順便覆蓋。"""
    hit = db._dorossi_loop_pair_hit
    gap = db._DOROSSI_LOOP_PAIR_MAX_GAP
    cases = [
        ("甲X乙", (), True, "baseline"),
        ("乙X甲", (), False, "tail before head"),
        ("甲" + "X" * gap + "乙", (), True, "gap exactly at the limit"),
        ("甲" + "X" * (gap + 1) + "乙", (), False, "gap one past the limit"),
        ("甲X。乙", (), False, "crosses a sentence"),
        ("甲X\n乙", (), False, "crosses a line"),
        ("甲這裡乙", (), False, "deictic endpoint in the gap"),
        ("甲X才乙", (), False, "narrative 才 in the gap"),
        ("甲X乙", ("到",), False, "gap lacks the required word"),
        ("甲X到乙", ("到",), True, "gap has the required word"),
        ("甲這裡乙，甲X乙", (), True, "a later head is tried after an earlier one fails"),
        ("甲乙", (), True, "empty gap"),
        ("甲X", (), False, "no tail at all"),
    ]
    for text, needs, expected, why in cases:
        assert hit(text, "甲", "乙", needs) is expected, (why, text, needs)
    print(f"  OK pair rule: {len(cases)} synthetic cases")


def test_dorossi_loop_pair_endpoints_each_block_a_real_sentence():
    """`_DOROSSI_LOOP_PAIR_ENDPOINTS` 的每一個詞都要有一句**只有它擋得住**的真實
    說法：拿掉那一個詞，那句就會起迴圈。只拿表本身跑迴圈的寫法抓不到「有人把表裡
    的某個詞清掉」，因為迴圈也跟著少一圈。"""
    m = db._dorossi_matches_loop_intent
    only_this_word_stops = {
        "這裡": "先做到這裡為止就好", "這邊": "今天做到這邊為止",
        "這兒": "做到這兒為止吧", "這樣": "做到這樣為止就夠了",
        "這步": "做到這步為止", "這一步": "先做到這一步為止",
        "這個階段": "做到這個階段為止", "此": "先做到此為止",
        "那裡": "做到那裡為止就停", "那邊": "做到那邊為止",
        "哪": "要做到哪裡為止？", "目前": "做到目前為止的進度如何",
        "現在": "做到現在為止累了嗎", "至今": "一路做到至今為止的成果",
        "今天": "做到今天為止", "一半": "做到一半為止就好",
        "才": "我一直做到半夜才完成",
    }
    assert set(only_this_word_stops) == set(db._DOROSSI_LOOP_PAIR_ENDPOINTS), (
        "every endpoint word needs a sentence only it stops, and every sentence "
        "here must name a real endpoint word")
    saved = db._DOROSSI_LOOP_PAIR_ENDPOINTS
    try:
        for word, sentence in only_this_word_stops.items():
            db._DOROSSI_LOOP_PAIR_ENDPOINTS = saved
            assert not m(sentence), f"{word!r} must stop {sentence!r}"
            db._DOROSSI_LOOP_PAIR_ENDPOINTS = tuple(w for w in saved if w != word)
            assert m(sentence), (
                f"{sentence!r} is stopped by something other than {word!r}, so it "
                f"cannot prove {word!r} is needed")
    finally:
        db._DOROSSI_LOOP_PAIR_ENDPOINTS = saved
    print(f"  OK {len(only_this_word_stops)} endpoint words each stop their own sentence")


def test_dorossi_loop_gate_composition():
    """`_dorossi_should_loop` ＝ `_dorossi_loop_gate_open`（擁有者 ＋
    claude_code/codex ＋ 工具 full）∧ 片語命中。閘門讀的是 **discord_bot 自己**
    命名空間裡的 `DOROSSI_CC_TOOLS`（from-import 的副本），所以測試改 `b.` 上的
    值；正式執行時它由 bot_config.json 決定、`!restart` 才生效。"""
    owner = b.OWNER_USER_ID
    saved = b.DOROSSI_CC_TOOLS
    try:
        b.DOROSSI_CC_TOOLS = "full"
        msg = _FakeMessage(owner)
        _eq(b._dorossi_should_loop(msg, "claude_code", "進入循環模式"), True,
            "owner + claude_code + full + phrase")
        _eq(b._dorossi_should_loop(msg, "codex", "進入循環模式"), True,
            "codex backend also loops")
        _eq(b._dorossi_should_loop(msg, "api", "進入循環模式"), False,
            "api backend never loops")
        _eq(b._dorossi_should_loop(msg, "claude_code", "幫我修好這個 bug"), False,
            "no phrase -> single turn (the self-judge path, when enabled, handles the rest)")
        _eq(b._dorossi_should_loop(_FakeMessage(owner + 1), "claude_code",
                                   "進入循環模式"), False,
            "non-owner never loops")
        b.DOROSSI_CC_TOOLS = "off"
        _eq(b._dorossi_should_loop(msg, "claude_code", "進入循環模式"), False,
            "tools=off never loops")
    finally:
        b.DOROSSI_CC_TOOLS = saved


# ---------------------------------------------------------------------------
def test_dorossi_session_tuning():
    """Session 級微調持久化的純邏輯：`_dorossi_apply_turn_tuning`（本輪指令 >
    已存值；"default" 清除）＋ `_dorossi_session_tuning`（store 存 allowlist
    key，讀出時查表取實際送 CLI 的值；舊階層 key 讀取時無感遷移；壞值退回預設、
    永不 raise）＋ reset 一併清掉。"""
    apply_t = db._dorossi_apply_turn_tuning
    read_t = db._dorossi_session_tuning
    choices = db.DOROSSI_MODEL_CHOICES
    # 空 slot：未設定 → 全預設。
    sess: dict = {}
    _eq(read_t(sess), (None, None), "empty -> defaults")
    # 套用兩者 → 持久鍵寫入（model 存 allowlist KEY），讀出時查表取送 CLI 的值。
    _eq(apply_t(sess, "high", "sonnet"), True, "apply returns changed")
    _eq(sess.get("tune_effort"), "high", "effort stored")
    _eq(sess.get("tune_model"), "sonnet", "model stored as allowlist KEY")
    _eq(read_t(sess), ("high", choices["sonnet"]),
        "read maps key -> CLI value")
    # 本輪沒打指令（None, None）→ 不動既存值。
    _eq(apply_t(sess, None, None), False, "no directive -> unchanged")
    _eq(read_t(sess), ("high", choices["sonnet"]), "value kept")
    # 覆蓋：本輪指令 > 已存值。
    apply_t(sess, "low", "opus")
    _eq(read_t(sess), ("low", choices["opus"]), "override wins")
    # "default" 只清指定的那一項，另一項保留。
    _eq(apply_t(sess, db.DOROSSI_TUNE_DEFAULT, None), True, "clear effort")
    assert "tune_effort" not in sess, "effort key removed"
    _eq(read_t(sess), (None, choices["opus"]),
        "effort cleared, model kept")
    apply_t(sess, None, db.DOROSSI_TUNE_DEFAULT)
    _eq(read_t(sess), (None, None), "model cleared -> defaults")
    # 舊版通用階層 key（fast/standard/max）讀取時經 DOROSSI_LEGACY_MODEL_KEYS
    # 無感遷移成現行 allowlist 的值——既有 session 的設定不因裁決改版而失效。
    for legacy, alias in db.DOROSSI_LEGACY_MODEL_KEYS.items():
        _eq(read_t({"tune_model": legacy}), (None, choices[alias]),
            f"legacy '{legacy}' migrates on read")
    # store 被手改／表已移除該 key／型別亂寫 → 退回預設、不 raise。
    _eq(read_t({"tune_effort": "turbo", "tune_model": "nope"}), (None, None),
        "unknown stored values -> defaults")
    _eq(read_t({"tune_effort": 123, "tune_model": ["x"]}), (None, None),
        "garbage types -> defaults, no raise")
    # reset（`/new`／`/reset`／過舊自動重置）一併清掉微調：新脈絡從預設開始。
    sess2 = {"cc_session_id": "cc-1", "codex_session_id": "cx-1",
             "tune_effort": "max",
             "tune_model": "opus", "label": "L"}
    db._dorossi_reset_session(sess2)
    assert "tune_effort" not in sess2 and "tune_model" not in sess2, \
        "reset drops tuning overrides"
    assert "codex_session_id" not in sess2, "reset drops Codex continuity"
    _eq(sess2.get("label"), "L", "reset keeps label")


def test_dorossi_session_list_shows_provider_model():
    """Session list must distinguish the two isolated continuities — but WITHOUT
    naming the backend vendor/product.

    硬性規定：送往對話平台的字串不得出現供應商／產品名。窄範圍例外只放行模型
    **別名**（`DOROSSI_MODEL_CHOICES` 的 key），不含供應商前綴、不含完整模型 ID。
    所以這裡斷言「別名有出現、供應商名沒出現、兩條脈絡仍可區分」。"""
    state = {"u1": {"active": "s1", "sessions": {
        "s1": {"created_at": 1, "ai_provider": "claude",
               "tune_model": "sonnet"},
        "s2": {"created_at": 2, "ai_provider": "codex"},
    }}}
    rendered = b._dorossi_render_session_list(state, "u1")
    assert "模型 sonnet" in rendered
    # 供應商／產品名一律不得外洩
    for vendor in ("Claude", "Codex", "claude", "codex", "Anthropic", "OpenAI"):
        assert vendor not in rendered, f"vendor name leaked: {vendor}"
    # 兩條脈絡的顯示仍必須不同，否則使用者無法分辨
    s1_line = [ln for ln in rendered.splitlines() if "sonnet" in ln]
    assert s1_line, "s1 model line missing"
    assert "後端預設模型" in rendered, "s2 falls back to a generic model label"


def test_paths_visible_here_surface_gate(monkeypatch):
    """完整主機路徑只能出現在「可露路徑的表面」：1:1 私訊，或設定列出的頻道。

    這是**表面**閘（訊息落在哪裡），不是身分閘——指令本身另有擁有者限制，但具名
    頻道裡的其他人看得見回覆，所以清單之外一律收成末段名稱。兩個 fail-closed
    方向都釘住：沒列進清單的頻道、以及根本拿不到 channel／id 的物件。"""
    monkeypatch.setattr(b, "PATH_REVEAL_CHANNEL_IDS", frozenset({4242}))
    full = "D:" + chr(92) + "Work" + chr(92) + "Somewhere" + chr(92) + "MyProject"

    listed = types.SimpleNamespace(channel=types.SimpleNamespace(id=4242))
    other = types.SimpleNamespace(channel=types.SimpleNamespace(id=99))
    headless = types.SimpleNamespace()
    dm = types.SimpleNamespace(
        channel=discord.DMChannel.__new__(discord.DMChannel))

    assert b._paths_visible_here(listed) is True, "列入清單的頻道要露完整路徑"
    assert b._paths_visible_here(dm) is True, "1:1 私訊一律可露"
    assert b._paths_visible_here(other) is False, "沒列入的頻道一律收起"
    assert b._paths_visible_here(headless) is False, "拿不到 channel → fail-closed"

    _eq(b._dorossi_dir_display(full, listed), full, "清單頻道給完整路徑")
    _eq(b._dorossi_dir_display(full, other), "MyProject", "其餘只給末段名稱")
    _eq(b._dorossi_dir_display(full, headless), "MyProject", "fail-closed 收末段")
    # 清空清單 → 回到私訊限定的原狀，具名頻道不再放行。
    monkeypatch.setattr(b, "PATH_REVEAL_CHANNEL_IDS", frozenset())
    assert b._paths_visible_here(listed) is False, "空清單＝私訊限定"


def test_dorossi_known_dir_map_refuses_to_guess(monkeypatch):
    """末段名 → 完整路徑的對照表：同名對到兩條路徑時**整筆剔除**。

    這張表是回填舊訊息的唯一依據，猜錯的代價不是「沒改到」而是「改成錯的目錄」
    ——後者無法從訊息本身看出來，所以寧可留著末段名。"""
    monkeypatch.setattr(b, "DOROSSI_CC_WORKDIR", Path("D:/ws/default"))
    state = {
        "u1": {"sessions": {
            "s1": {"cc_cwd": "D:/Work/Example"},
            "s2": {"cc_cwd": "E:/Backup/Example"},   # 同末段名、不同路徑
            "s3": {"cc_cwd": "D:/Work/Notes",
                   "cc_extra_dir": "D:/Work/Extra"},
            "s4": "壞掉的 session 資料",
        }},
        "u2": "壞掉的使用者資料",
    }
    dirmap = b._dorossi_known_dir_map(state)
    assert "Example" not in dirmap, "同一末段名對到兩條路徑時不得猜"
    _eq(dirmap.get("Notes"), "D:/Work/Notes", "唯一對應要收進表裡")
    _eq(dirmap.get("Extra"), "D:/Work/Extra", "額外可存取目錄也算來源")
    assert dirmap.get("default"), "預設工作目錄一定在表裡"


def test_backfill_dir_text_rewrites_only_known_leaves():
    """回填只動「目錄：<認得的末段名>」，其餘一字不改，而且可以重複執行。"""
    dirmap = {"Axiomatic": "D:/Work/Example", "notes": "D:/Docs/notes"}

    listing = ("你的 session：\n• `s1`（使用中）\n"
               "    目錄：Axiomatic\n    額外可存取：notes")
    rewritten, hits = b._backfill_dir_text(listing, dirmap)
    assert "目錄：D:/Work/Example" in rewritten
    assert "額外可存取：D:/Docs/notes" in rewritten
    _eq(hits, {"Axiomatic", "notes"}, "回報實際用到的末段名")

    # allowdir 的單行寫法：反引號要留著，句號也不能被吃掉。
    one = b._backfill_dir_text("`s1` 額外可存取目錄：`notes`。", dirmap)
    _eq(one[0], "`s1` 額外可存取目錄：`D:/Docs/notes`。", "反引號與句號原樣保留")

    # 已經是完整路徑 → 不動（重複跑不會越改越糟）。
    assert b._backfill_dir_text("    目錄：D:/Work/Example", dirmap) is None
    # 認不得的值不猜，沒有目錄的訊息也不動。
    assert b._backfill_dir_text("    目錄：SomethingElse", dirmap) is None
    assert b._backfill_dir_text("沒有目錄的一般訊息", dirmap) is None
    assert b._backfill_dir_text("    目錄：Axiomatic", {}) is None


def test_format_codex_account_usage():
    rendered = b._format_codex_account_usage(
        {"summary": {"lifetimeTokens": 123456},
         "dailyUsageBuckets": [
             {"startDate": "2026-07-01", "tokens": 100},
             {"startDate": "2026-07-02", "tokens": 250}]},
        {"rateLimits": {
            "primary": {"usedPercent": 12, "resetsAt": 1800000000},
            "secondary": {"usedPercent": 34}}})
    assert "今日 tokens：250" in rendered
    assert "近 7 日 tokens：350" in rendered
    assert "累計 tokens：123,456" in rendered
    assert "主要額度：已用 12%" in rendered
    assert "<t:1800000000:R>" in rendered
    assert "次要額度：已用 34%" in rendered


def test_the_codex_usage_query_cannot_hang_on_a_pipe_nobody_closes(monkeypatch):
    """`_query_codex_account_usage` 的 `finally` 不得 `await stderr_task`。

    `stderr_task` 是 `proc.stderr.read()`——讀到 **EOF** 為止，而 EOF 要等管線的
    **所有**寫端 handle 都關掉。子行程若生了一個繼承這條 stderr 的孫行程（背景
    helper／language server 之類），孫行程會一直抱著一個寫端，於是 `kill()` ＋
    `await proc.wait()` 之後 EOF **還是不會來**。實測（真的起一個抱著管線睡著的
    孫行程）：`await stderr_task` 12 秒沒回來，`cancel()` ＋ `gather` 0.00 秒就回。

    主路徑本身是有界的（`_read_json_response` 的 15／30 秒），**只有這段收尾沒有**
    ——所以失敗形態是「那一則用量查詢永遠不回覆、coroutine 洩漏」，而不是報錯。

    ⚠️ 這支一定要包 `wait_for`：回歸時要**變紅**，不能變成掛住。一個掛住的測試比
    一個失敗的測試糟——它不會告訴你哪裡錯，只會讓整輪跑不完。
    """
    import asyncio as _asyncio

    # ⚠️ 這兩個替身的 `await _asyncio.sleep(0)` 是**必要的，不是裝飾**。
    #
    # 沒有它們的第一版寫成 `async def drain(self): pass`，於是整條路徑上沒有任何
    # 真正的暫停點——事件迴圈從頭到尾沒拿到控制權，`stderr_task` 連第一步都沒跑
    # 過（`reads` 是 0）。那會讓下面兩句斷言測到的是「替身有多同步」而不是被測
    # 系統的行為。真實的 subprocess I/O 一定會讓出控制權，所以替身也要讓。
    class _Stdin:
        def write(self, _data):
            pass

        async def drain(self):
            await _asyncio.sleep(0)

    class _Stdout:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            await _asyncio.sleep(0)
            return self._lines.pop(0) if self._lines else b""

    class _Stderr:
        """`read()` 永遠不結束——孫行程還抱著寫端，EOF 不會來。"""

        def __init__(self):
            self.reads = 0
            self.cancelled = False

        async def read(self):
            self.reads += 1
            try:
                await _asyncio.Event().wait()      # 永遠不會被 set
            except _asyncio.CancelledError:
                self.cancelled = True
                raise
            return b""

    class _Proc:
        def __init__(self):
            self.stdin = _Stdin()
            self.stdout = _Stdout([
                b'{"id":1,"result":{}}\n',
                b'{"id":2,"result":{"summary":{"lifetimeTokens":123456}}}\n',
                b'{"id":3,"result":{"rateLimits":'
                b'{"primary":{"usedPercent":12}}}}\n',
            ])
            self.stderr = _Stderr()
            self.returncode = None
            self.killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    proc = _Proc()

    async def _fake_exec(*_a, **_kw):
        return proc

    monkeypatch.setattr(b, "find_codex_executable", lambda: "codex.exe")
    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _fake_exec)

    async def _run():
        # 5 秒遠大於這條路徑該花的時間（全是替身，沒有真的 I/O）。
        return await _asyncio.wait_for(b._query_codex_account_usage(), 5)

    out = _asyncio.run(_run())

    # 正面對照組一：**答案還在**。否則「收尾一律短逾時、把結果丟掉」也會綠，
    # 而那只是把「不回覆」換成「回覆查詢失敗」。
    assert "累計 tokens：123,456" in out, out
    # 正面對照組二：抽乾 stderr 的那個 task 真的被建出來了。少了這句，日後把
    # `stderr_task` 整個拿掉會讓這支測試變成空轉——而沒有人抽乾管線時，子行程
    # 寫 stderr 寫到管線滿了就會塞住。
    assert proc.stderr.reads == 1, "沒有人去抽乾 stderr 管線"
    # 機制：是被 **cancel** 掉的，不是碰巧自己結束的。
    assert proc.stderr.cancelled, "stderr_task 沒有被 cancel——收尾在等它自己結束"


# ---------------------------------------------------------------------------
def test_dorossi_loop_registry_and_abort():
    """多迴圈並行的 per-session 狀態機：`_DorossiLoopState`（abort／proc／每迴圈
    注入緩衝）＋ abort 目標選擇純函式 `_dorossi_select_abort_target` ＋
    `dorossi_max_parallel_loops` 操作閥的 loader 形狀（0 ＝不設限、可選）。"""
    # --- _DorossiLoopState ---------------------------------------------------
    st = b._DorossiLoopState("u1", "s1")
    _eq((st.uid, st.sid, st.abort, st.proc), ("u1", "s1", False, None),
        "fresh state")

    class _FakeProc:  # 小型 stub：只驗證 kill 有被呼叫
        killed = False

        def kill(self):
            self.killed = True

    p = _FakeProc()
    st.set_proc(p)
    _eq(st.proc is p, True, "set_proc records handle")
    st.injections.append("a")
    st.injections.append("b")
    _eq(st.drain_injections(), ["a", "b"], "drain returns copy in order")
    _eq(st.injections, [], "drain clears buffer")
    _eq(st.drain_injections(), [], "second drain empty")
    st.request_abort()
    _eq((st.abort, p.killed), (True, True), "request_abort flags + kills")
    st.proc = None
    st.request_abort()  # proc 為 None 也不能炸
    _eq(st.abort, True, "abort idempotent without proc")
    # --- _dorossi_select_abort_target -----------------------------------------
    sel = b._dorossi_select_abort_target
    k1, k2 = ("u1", "s1"), ("u1", "s2")
    _eq(sel([], "", None), ("none", None), "no loops -> none")
    _eq(sel([k1, k2], "all", k1), ("all", [k1, k2]), "all")
    _eq(sel([k1, k2], "全部", None), ("all", [k1, k2]), "all (zh)")
    _eq(sel([k1, k2], "s2", k1), ("one", k2), "named id")
    _eq(sel([k1, k2], "s9", k1), ("none", None), "named id not running")
    _eq(sel([k1, k2], "", k1), ("one", k1), "bare -> active's loop")
    _eq(sel([k2], "", k1), ("one", k2), "bare, active idle, single -> it")
    _eq(sel([k1, k2], "", ("u1", "s3")), ("ask", [k1, k2]),
        "bare, multiple, active idle -> ask")
    _eq(sel([k1, k2], "", None), ("ask", [k1, k2]), "bare, no active -> ask")
    # --- 並行數操作閥的 loader 形狀（0＝不設限可選；壞值退預設） ---------------
    import _bot_config as bc
    d = bc._DEFAULT_BOT_CONFIG["dorossi_max_parallel_loops"]
    assert isinstance(d, int) and d >= 0, "default must be a non-negative int"
    _eq(bc._coerce_int(0, d, min_value=0), 0, "0 (no limit) allowed")
    _eq(bc._coerce_int(-3, d, min_value=0), d, "negative -> default")
    _eq(bc._coerce_int(True, d, min_value=0), d, "bool -> default")
    live = bc.load_bot_config()["dorossi_max_parallel_loops"]
    assert isinstance(live, int) and live >= 0, f"live config unsafe: {live!r}"
    assert isinstance(b.DOROSSI_MAX_PARALLEL_LOOPS, int) \
        and b.DOROSSI_MAX_PARALLEL_LOOPS >= 0, "bot valve must be >= 0"
    print(f"  OK loop valve shape (default={d}, live={live})")


# ---------------------------------------------------------------------------
def test_dorossi_loop_pending_resume():
    """「未完成自走任務」標記與續跑計畫的純邏輯：mark（保留舊任務描述）／clear／
    `_dorossi_loop_resume_plan`（continue vs fresh vs 無）／reset 一併清掉。"""
    sess: dict = {}
    _eq(db._dorossi_loop_resume_plan(sess), None, "no marker -> nothing")
    # mark：記下任務描述；後端脈絡在 → continue 接續。
    db._dorossi_mark_loop_pending(sess, "整理測試")
    assert isinstance(sess.get("loop_pending"), dict), "marker written"
    _eq(sess["loop_pending"]["task"], "整理測試", "task stored")
    sess["cc_session_id"] = "cc-1"
    _eq(db._dorossi_loop_resume_plan(sess), ("continue", None),
        "context alive -> resume-continue")
    sess.pop("cc_session_id")
    sess["codex_session_id"] = "cx-1"
    _eq(db._dorossi_loop_resume_plan(sess), ("continue", None),
        "Codex context alive -> resume-continue")
    # 脈絡被清（保留標記）→ 用原任務文字 fresh 重跑。
    sess.pop("codex_session_id")
    _eq(db._dorossi_loop_resume_plan(sess), ("fresh", "整理測試"),
        "context gone + task -> fresh restart")
    # 再次 mark 而任務描述為空 → 保留舊描述（接續不清掉原始任務文字）。
    db._dorossi_mark_loop_pending(sess, "")
    _eq(sess["loop_pending"]["task"], "整理測試", "empty re-mark keeps task")
    # clear → 沒東西可接續；重複清除安全。
    db._dorossi_clear_loop_pending(sess)
    _eq(db._dorossi_loop_resume_plan(sess), None, "cleared -> nothing")
    db._dorossi_clear_loop_pending(sess)
    # 標記形狀被手改壞 → 一律當「沒有可接續」，不 raise。
    _eq(db._dorossi_loop_resume_plan({"loop_pending": "yes"}), None,
        "garbage marker -> nothing")
    _eq(db._dorossi_loop_resume_plan({"loop_pending": {"task": 3}}), None,
        "garbage task -> nothing")
    # 有標記但既無脈絡也無任務描述 → 無從接續。
    _eq(db._dorossi_loop_resume_plan({"loop_pending": {"ts": 1.0}}), None,
        "marker without context/task -> nothing")
    # reset（`/new`／`/reset`）一併清掉標記：新脈絡沒有可續跑的任務。
    sess2 = {"cc_session_id": "cc-2",
             "loop_pending": {"ts": 1.0, "task": "t"}, "label": "L"}
    db._dorossi_reset_session(sess2)
    assert "loop_pending" not in sess2, "reset drops loop_pending"
    _eq(db._dorossi_loop_resume_plan(sess2), None, "post-reset -> nothing")


def test_dorossi_prompt_queue_limit_is_20():
    """`@bot <prompt>` 同 session waiter queue 上限需求：允許積壓到 20 筆。"""
    _eq(b.DOROSSI_MAX_WAITING, 20, "Dorossi prompt waiter queue limit")
    _eq(b.DOROSSI_LOOP_INJECT_MAX, 20, "Dorossi loop injection queue limit")
    _eq(b._GENERATE_MAX_WAITING, 20, "single-image queue limit")


def test_dorossi_queue_persistence_roundtrip():
    """Dorossi waiter queue 落盤：新增、移除、依 live waiter 順序重排。"""
    old_file = b.DOROSSI_QUEUE_FILE
    old_waiters = dict(b._dorossi_waiters)
    try:
        with tempfile.TemporaryDirectory() as td:
            b.DOROSSI_QUEUE_FILE = Path(td) / "dorossi_queue.ndjson"
            b._dorossi_queue_add({"id": "a", "uid": "u1", "sid": "s1",
                                  "prompt": "one"})
            b._dorossi_queue_add({"id": "b", "uid": "u1", "sid": "s1",
                                  "prompt": "two"})
            b._dorossi_queue_add({"id": "c", "uid": "u1", "sid": "s1",
                                  "prompt": "three"})
            _eq([r["id"] for r in b._dorossi_queue_read()],
                ["a", "b", "c"], "queue add order")

            key = b._dorossi_session_key("u1", "s1")
            b._dorossi_waiters[key] = [
                b._DorossiWaiter(None, "b"),
                b._DorossiWaiter(None, "a"),
            ]
            b._dorossi_queue_reorder_for_waiters(key)
            _eq([r["id"] for r in b._dorossi_queue_read()],
                ["b", "a", "c"], "queue reordered from waiters")

            b._dorossi_queue_remove("a")
            _eq([r["id"] for r in b._dorossi_queue_read()],
                ["b", "c"], "queue remove")
    finally:
        b.DOROSSI_QUEUE_FILE = old_file
        b._dorossi_waiters.clear()
        b._dorossi_waiters.update(old_waiters)


def test_permission_roles_and_command_tiers():
    old_roles = b.USER_ROLES
    try:
        b.USER_ROLES = {
            "admin_user_ids": [1],
            "operator_user_ids": [2],
            "viewer_user_ids": [3],
        }
        _eq(b._roles_configured(), True, "roles configured")
        _eq(b._user_role(1), "admin", "admin role")
        _eq(b._user_role(2), "operator", "operator role")
        _eq(b._user_role(3), "viewer", "viewer role")
        _eq(b._command_required_role("!health"), "viewer", "health viewer")
        _eq(b._command_required_role("!run"), "operator", "run operator")
        _eq(b._command_required_role("!kill"), "admin", "kill admin")
        assert b._role_allows("operator", "viewer"), "operator >= viewer"
        assert not b._role_allows("viewer", "operator"), "viewer !>= operator"
        b.USER_ROLES = {
            "admin_user_ids": [],
            "operator_user_ids": [],
            "viewer_user_ids": [],
        }
        _eq(b._roles_configured(), False, "empty roles preserve legacy")
    finally:
        b.USER_ROLES = old_roles


def test_run_label_parser():
    _eq(b._extract_run_label("label 夜間批次"),
        ("夜間批次", ""), "label only")
    _eq(b._extract_run_label("label 夜間批次 in 90m"),
        ("夜間批次", "in 90m"), "label + in")
    _eq(b._extract_run_label("label 早班 at 02:00"),
        ("早班", "at 02:00"), "label + at")
    _eq(b._extract_run_label("in 5m"), ("", "in 5m"), "no label")


def test_bot_config_new_blocks_shape():
    import _bot_config as bc
    cfg = bc.load_bot_config()
    for key in ("user_roles", "daily_health_report", "dashboard"):
        assert key in cfg, f"{key} exists"
    _eq(sorted(cfg["user_roles"]), ["admin_user_ids", "operator_user_ids",
                                    "viewer_user_ids"], "role keys")
    _eq("enabled" in cfg["daily_health_report"], True, "daily enabled key")
    _eq("port" in cfg["dashboard"], True, "dashboard port key")
    _eq("dorossi_compact_context_tokens" in cfg, True,
        "compact context token key present")
    _eq(isinstance(cfg["dorossi_compact_context_tokens"], int), True,
        "compact context token is int")


def test_slash_top_level_count_guard():
    # Discord caps top-level application commands at 100. Dorossi additions must
    # stay under the /dorossi group instead of adding more top-level commands.
    count = len(b.tree.get_commands())
    assert count <= 100, f"top-level slash commands {count} exceeds Discord cap"
    print(f"  OK slash top-level count -> {count}")


# ---------------------------------------------------------------------------
def main():
    _group("test_parse_run_schedule", test_parse_run_schedule)
    _group("test_seconds_per_image", test_seconds_per_image)
    _group("test_seconds_per_image_ignores_a_stale_regime",
           test_seconds_per_image_ignores_a_stale_regime)
    _group("test_a_character_that_crossed_a_restart_is_not_counted_as_fast",
           test_a_character_that_crossed_a_restart_is_not_counted_as_fast)
    _group("test_eta_sampling_must_cover_a_whole_quota_cycle",
           test_eta_sampling_must_cover_a_whole_quota_cycle)
    _group("test_scheduled_rest_is_not_mistaken_for_a_stall",
           test_scheduled_rest_is_not_mistaken_for_a_stall)
    _group("test_a_quota_wait_is_not_mistaken_for_a_stall",
           test_a_quota_wait_is_not_mistaken_for_a_stall)
    _group("test_end_marker_and_pair_count", test_end_marker_and_pair_count)
    _group("test_pairing_matches_webrunner", test_pairing_matches_webrunner)
    _group("test_p7_pair_todos_single_source", test_p7_pair_todos_single_source)
    _group("test_p7_plan_char_name_single_source",
           test_p7_plan_char_name_single_source)
    _group("test_p7_is_end_marker_single_source",
           test_p7_is_end_marker_single_source)
    _group("test_p7_compute_run_plan_regression",
           test_p7_compute_run_plan_regression)
    _group("test_streaming_media_only", test_streaming_media_only)
    _group("test_rotate_ndjson_tail", test_rotate_ndjson_tail)
    _group("test_token_round_info_split", test_token_round_info_split)
    _group("test_dorossi_context_tokens", test_dorossi_context_tokens)
    _group("test_dorossi_context_compaction_due",
           test_dorossi_context_compaction_due)
    _group("test_dorossi_loop_compaction_due_token_condition",
           test_dorossi_loop_compaction_due_token_condition)
    _group("test_token_record_read_roundtrip", test_token_record_read_roundtrip)
    _group("test_token_record_split_backward_compat",
           test_token_record_split_backward_compat)
    _group("test_token_render_png", test_token_render_png)
    _group("test_dorossi_session_key_and_by_id",
           test_dorossi_session_key_and_by_id)
    _group("test_dorossi_session_lock_refcount",
           test_dorossi_session_lock_refcount)
    _group("test_dorossi_resolve_cc_workdir",
           test_dorossi_resolve_cc_workdir)
    _group("test_dorossi_persist_advance_fail_closed",
           test_dorossi_persist_advance_fail_closed)
    _group("test_collect_codex_images_is_scoped_and_incremental",
           test_collect_codex_images_is_scoped_and_incremental)
    _group("test_dorossi_cc_max_parallel_clamp",
           test_dorossi_cc_max_parallel_clamp)
    _group("test_dorossi_per_session_lock_serialises",
           test_dorossi_per_session_lock_serialises)
    _group("test_dorossi_parse_turn_flags", test_dorossi_parse_turn_flags)
    _group("test_dorossi_loop_intent_phrases", test_dorossi_loop_intent_phrases)
    _group("test_dorossi_loop_gate_composition",
           test_dorossi_loop_gate_composition)
    _group("test_dorossi_session_tuning", test_dorossi_session_tuning)
    _group("test_dorossi_session_list_shows_provider_model",
           test_dorossi_session_list_shows_provider_model)
    _group("test_format_codex_account_usage",
           test_format_codex_account_usage)
    _group("test_dorossi_loop_registry_and_abort",
           test_dorossi_loop_registry_and_abort)
    _group("test_dorossi_loop_pending_resume", test_dorossi_loop_pending_resume)
    _group("test_dorossi_prompt_queue_limit_is_20",
           test_dorossi_prompt_queue_limit_is_20)
    _group("test_dorossi_queue_persistence_roundtrip",
           test_dorossi_queue_persistence_roundtrip)
    _group("test_permission_roles_and_command_tiers",
           test_permission_roles_and_command_tiers)
    _group("test_run_label_parser", test_run_label_parser)
    _group("test_parse_schedule_when", test_parse_schedule_when)
    _group("test_schedule_due_catches_up_after_a_missed_minute",
           test_schedule_due_catches_up_after_a_missed_minute)
    _group("test_schedule_due_weekly_and_once",
           test_schedule_due_weekly_and_once)
    _group("test_schedule_when_text_is_human_readable",
           test_schedule_when_text_is_human_readable)
    _group("test_the_schedule_file_has_no_read_modify_write_race",
           test_the_schedule_file_has_no_read_modify_write_race)
    _group("test_option_helpers_for_gui_commands",
           test_option_helpers_for_gui_commands)
    _group("test_bot_config_new_blocks_shape",
           test_bot_config_new_blocks_shape)
    _group("test_slash_top_level_count_guard",
           test_slash_top_level_count_guard)
    _group("test_every_dorossi_sub_command_reaches_an_owner_gate",
           test_every_dorossi_sub_command_reaches_an_owner_gate)
    _group("test_the_dorossi_gate_scan_actually_found_the_commands",
           test_the_dorossi_gate_scan_actually_found_the_commands)
    _group("test_the_three_spellings_of_the_dorossi_gate_are_one_person",
           test_the_three_spellings_of_the_dorossi_gate_are_one_person)
    _group("test_the_dorossi_gate_refuses_a_stranger",
           test_the_dorossi_gate_refuses_a_stranger)
    _group("test_the_bot_keeps_no_write_only_module_state",
           test_the_bot_keeps_no_write_only_module_state)
    # 這支不吃 fixture（純 AST 掃描），所以 standalone 也叫得動；同組的兩支行為
    # 測試要 `monkeypatch`，只走 pytest。
    _group("test_no_wall_clock_interval_measurement_survives_anywhere",
           test_no_wall_clock_interval_measurement_survives_anywhere)
    # 這個 runner 是**逐支具名註冊**的，因為檔案裡有一大票測試要吃 pytest fixture
    # （`tmp_path` / `monkeypatch` / `capsys`），沒有 fixture 就叫不動——所以它們
    # 只走 pytest，這是刻意的。
    #
    # 但「只走 pytest」不等於可以不講：原本這裡印的是一句不帶條件的
    # `ALL N TEST GROUPS PASSED`，而 N 少算了九十幾支——一個看起來完全正常的綠燈。
    # 現在把差額一起印出來，standalone 的結果就不會被讀成「全部都過了」。
    # （數量從 AST 現算，不寫死，才不會自己漂掉。）
    print(f"ALL {_PASSED} TEST GROUPS PASSED"
          f"{_unregistered_note()}")
    return 0




# --------------------------------------------------------------------------
# 相依落差（DoD #4 的自動守門）
# --------------------------------------------------------------------------
def test_declared_dependencies_are_read_from_requirements_not_hardcoded():
    """清單來自 `requirements.txt`，不是寫死的第二份。

    版本修飾與環境標記要剝掉，套件名與 import 名不同的那幾個要換過來，
    否則這個檢查會對著本來就裝好的東西喊「缺了」。
    """
    names = b._declared_dependencies()
    assert "je_auto_control" in names          # je-auto-control
    assert "discord" in names                  # discord.py
    assert "PIL" in names                      # Pillow
    assert all(";" not in name and "<" not in name for name in names)


def test_this_interpreter_has_every_declared_dependency():
    """DoD #4：宣告了就必須真的裝在**跑得到它**的直譯器上。

    可選相依全部是 `try: import … except: None`，所以少裝一個不會讓 bot 起不來
    ——只會讓某個指令回「沒安裝」，log 也不會有異常。實際踩過：啟動器優先用的
    專案內環境少了三個宣告過的套件（`@bot calc`、ASCII 指令、`api` 後端因此
    不可用），而煙霧測試用的是另一個直譯器，所以一直沒人發現。
    """
    missing = b._missing_dependencies()
    assert not missing, (
        f"這個直譯器（{sys.executable}）缺少宣告過的相依：{missing}。"
        "`pip install` 到**這個**直譯器裡；啟動器優先用專案內的 .venv，"
        "不一定是你跑測試的那一個。")


# --------------------------------------------------------------------------
# 相依落差的**反方向**：import 得到 ≠ 宣告過
# --------------------------------------------------------------------------
# `requirements.txt` 刻意沒有 dev/test 區塊，所以測試自己 import 的東西不算漏
# 宣告。每一筆都要寫理由，不要當成「加進來就不會紅」的垃圾桶。
_TEST_ONLY_IMPORTS = {
    # 只有 `test_*.py` import。宣告進 `requirements.txt` 反而會害
    # `_missing_dependencies()` 與 `/sys doctor` 在正式機上喊缺——正式機不需要它。
    "pytest",
    # `test_dependency_floors.py` 用它比對版本號。**不是**開發機碰巧裝著就算數：
    # `pytest` 自己的 metadata 寫著 `Requires-Dist: packaging>=22`（兩個直譯器上
    # 都查得到），所以只要跑得動這套測試，它就一定在。版本比對非用它不可——
    # 手寫字串比對會判定 `3.9.0 > 3.14.3`，那正是下限守門最不能出錯的地方。
    "packaging",
}


def _live_stack_sources() -> list[Path]:
    """會被 live stack import 的原始碼檔案。

    `legacy/` 不掃：它是 reference-only，live stack 完全不 import 它，
    它自己的相依（例如 `requests`）沒有理由拖著正式安裝。

    repo root 收的是**所有** `*.py`，不是 `start_*.py`。原本那個 glob 是**列舉
    式**的選取，漏掉了 `run_batch.py` 與 `install_autostart.py`——兩支都是使用者
    會直接執行的進入點，正好是這支守門要防的「fresh clone 一跑就
    ModuleNotFoundError」的第一現場。列舉式選取的失效沒有症狀：下一支叫別的名字
    的 root 進入點會自動落在守門之外，而測試依然全綠。
    （一次性的探測／補丁腳本本來就不該放在 repo 裡——`test_text_encoding` 的
    rglob 同樣會掃到它們，這條慣例兩邊一致。）

    測試檔也在範圍內（`_TEST_ONLY_IMPORTS` 就是為它們寫的）；2026-09-22 起它們住在
    repo 根目錄的 `test/`，不在套件裡，所以要另外列進來，範圍才跟搬家前一樣。
    """
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    root = package.parent
    tests = Path(__file__).resolve().parent
    return (sorted(package.glob("*.py")) + sorted(tests.glob("*.py"))
            + sorted(root.glob("*.py")))


def _local_module_names() -> set[str]:
    """本專案自己的模組名（含套件名本身與 `test/` 裡的測試模組，例如 `conftest`）。"""
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    root = package.parent
    names = {path.stem for path in package.glob("*.py")}
    names |= {path.stem for path in Path(__file__).resolve().parent.glob("*.py")}
    names |= {path.stem for path in root.glob("*.py")}
    names.add(package.name)          # `from axiomatic._x import y` 的自我參照
    return names


# ---------------------------------------------------------------------------
# `raise X(f(...))` 的引數不得回到 raise 所在的函式
# ---------------------------------------------------------------------------
#
# 2026-09-11 實測出來的缺陷形狀：`_gui_control._load_ocr()` 尾端是
# `raise GuiError(ocr_status()[1])`，而 `ocr_status()` 的第三道檢查是
# `try: _load_ocr() ... except GuiError`。**例外的引數在 raise 之前就會被求值**，
# 所以那個 `except GuiError` 永遠等不到——先撞 `RecursionError`，而它不是
# `GuiError`，會一路往上竄，順帶把「辨識引擎執行檔無法執行」那句話變成死碼。
#
# 這個陷阱靠讀程式碼看不出來：兩支函式各自都很正常，`try/except` 也寫對了，
# 環是**跨函式**的，而且藏在引數求值的順序裡。實測用 `TESSERACT_CMD` 指到任何
# 存在但不是引擎的檔案就會重現（`tesseract_cmd()` 只檢查
# `Path(override).exists()`），完全不需要 monkeypatch。
#
# 規則：`raise X(...)` 的引數裡呼叫到的同模組函式，不得沿同模組呼叫圖走得回那個
# raise 所在的函式。全專案實測 7 條這種邊，只有上面那一條在環上。
#
# 只連**同一個模組內**的邊：跨模組要解析 import，雜訊會蓋掉訊號，而這個陷阱本來
# 就最容易在「一對互相解釋彼此」的姊妹函式之間長出來。
#
# ⚠️ **現成的 linter 抓不到這一條，這是量出來的、不是猜的。** 2026-09-11 拿真的
# 缺陷去問過兩邊：`ruff 0.16.6 --select ALL`（開它全部 900+ 條規則）掃
# `_gui_control.py`，關於遞迴一個字都沒有（`PL*` 命中的全是 `global` 與魔術數字）；
# `pylint 4.0.8`（裝在 repo 外的拋棄式 venv，不動兩個正式直譯器）預設全開也沒有
# 任何 recursion／cyclic 命中，而且它根本沒有 `recursive-call` 這個訊息——那是
# 第三方擴充的，何況只認**直接**自我遞迴，跨函式的環一樣看不到。
# 所以這支守門不是在重造輪子；要拿掉它之前，先用同樣的方式證明別人接手了。


def _module_level_functions(tree) -> dict:
    """module-level 的 `def` / `async def`。巢狀 def 的呼叫算在外層身上。"""
    import ast as _ast

    return {node.name: node for node in tree.body
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))}


def _same_module_call_graph(tree) -> dict:
    """`{函式名: 它呼叫到的同模組函式名}`。只認 `f(...)` 這種裸名字呼叫。"""
    import ast as _ast

    fns = _module_level_functions(tree)
    graph = {}
    for name, node in fns.items():
        callees = set()
        for sub in _ast.walk(node):
            if (isinstance(sub, _ast.Call)
                    and isinstance(sub.func, _ast.Name)
                    and sub.func.id in fns):
                callees.add(sub.func.id)
        graph[name] = callees
    return graph


def _raise_argument_calls(tree) -> list:
    """`raise X(...)` 的**引數**裡呼叫到的同模組函式。

    回 `[(raise 所在的函式, 被呼叫的函式, 行號)]`。位置引數與關鍵字引數都看，
    而且是遞迴走進整個引數運算式——真實現場長得像 `GuiError(ocr_status()[1])`，
    那個呼叫藏在一層 Subscript 底下。
    """
    import ast as _ast

    fns = _module_level_functions(tree)
    out = []
    for name, node in fns.items():
        for sub in _ast.walk(node):
            if not (isinstance(sub, _ast.Raise)
                    and isinstance(sub.exc, _ast.Call)):
                continue
            args = list(sub.exc.args) + [kw.value for kw in sub.exc.keywords]
            for arg in args:
                for inner in _ast.walk(arg):
                    if (isinstance(inner, _ast.Call)
                            and isinstance(inner.func, _ast.Name)
                            and inner.func.id in fns):
                        out.append((name, inner.func.id, sub.lineno))
    return out


def _reaches(graph: dict, start: str, goal: str) -> bool:
    """同模組呼叫圖上從 `start` 走得到 `goal` 嗎（`start == goal` 也算）。"""
    seen, stack = {start}, [start]
    while stack:
        cur = stack.pop()
        if cur == goal:
            return True
        for nxt in graph.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def _raise_argument_reentries(tree) -> list:
    """引數求值會再回到 raise 所在函式的那些邊（＝這條規則的違反者）。"""
    graph = _same_module_call_graph(tree)
    return [(fn, callee, lineno)
            for fn, callee, lineno in _raise_argument_calls(tree)
            if _reaches(graph, callee, fn)]


def test_no_raise_argument_can_re_enter_the_function_that_raises():
    """`raise X(f(...))` 的 `f` 不得走得回那個 raise 所在的函式。

    走得回去就是無限遞迴，而且**接不到**：引數在 raise 之前求值，所以包在外面的
    `except X` 那一層根本還沒開始。使用者拿到的是 `RecursionError`，不是那句
    寫好的泛用訊息。
    """
    import ast as _ast

    edges, offenders = [], []
    for path in _live_stack_sources():
        try:
            tree = _ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        edges += [(path.name, *row) for row in _raise_argument_calls(tree)]
        offenders += [(path.name, *row)
                      for row in _raise_argument_reentries(tree)]
    # 正面對照先跑：抽不到任何邊跟「乾淨」長得一模一樣，而前者是抽取器壞了。
    # 2026-09-11 實測 7 條邊；門檻放 5 是給重構留餘裕，不是量出來的上限。
    assert len(edges) >= 5, (
        f"`raise X(f(...))` 這種邊只抽到 {len(edges)} 條，抽取器壞了")
    assert not offenders, (
        "raise 的引數會回到 raise 所在的函式（無限遞迴，而且 except 接不到）：\n"
        + "\n".join(f"  {name}:{lineno} {fn} -> {callee} -> … -> {fn}"
                    for name, fn, callee, lineno in offenders))


_REENTRY_CORPUS = '''
class E(Exception):
    pass


def describe():
    try:
        load()
    except E:
        return "unusable"
    return "ok"


def load():
    if not _ready():
        raise E(describe()[0])
    return 1


def _ready():
    return False


def safe_reason():
    return "plain"


def other():
    raise E(safe_reason())
'''


def test_the_raise_reentry_detector_actually_bites():
    """合成對照組：抓得到真的環，也**不會**把每一條 raise 引數邊都當成環。

    負面那一半才是重點。少了它，`_reaches` 直接 `return True` 也照樣全綠——
    整支守門會退化成「不准在 raise 的引數裡呼叫同模組函式」，那是另一條規則，
    而且會誤殺 `_webrunner_shared` 裡呼叫 `_long_error()` 的那四條邊
    （`with_retry` / `_note_transport_error` / `_click_dismiss_target` ×2）以及
    `dorossi_backend._dorossi_via_api` 那條——它們都合法，因為被呼叫的那支不會
    再走回來。
    """
    import ast as _ast

    tree = _ast.parse(_REENTRY_CORPUS)
    edges = {(fn, callee) for fn, callee, _ in _raise_argument_calls(tree)}
    assert edges == {("load", "describe"), ("other", "safe_reason")}, edges
    hits = {(fn, callee) for fn, callee, _ in _raise_argument_reentries(tree)}
    assert hits == {("load", "describe")}, hits


def _top_level_imports(path: Path) -> set[str]:
    """一支檔案 import 到的**頂層**模組名。

    函式內與 `try:` 內的 import 一樣要算——可選相依正是寫成
    `try: import comtypes except ImportError: None`，漏宣告的話 fresh clone
    只會靜靜地少一組指令，不會有任何錯誤訊息。
    相對匯入（`from . import x`）跳過，那本來就是本專案自己的東西。
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module.split(".")[0])
    return names


def _undeclared_third_party(scanned, declared, *, local, exempt) -> dict:
    """`scanned` 是 `(檔名, 該檔 import 到的頂層名集合)`，挑出沒被宣告的那些。

    抽成純函式是因為**這條規則本身也要被測到**。現況是乾淨的，所以下面那支
    真實資料的測試就算把主斷言整句刪掉也照樣全綠——真正的牙齒在
    `test_the_undeclared_import_scanner_would_see_one`，它拿合成資料餵這支。
    """
    undeclared: dict[str, set[str]] = {}
    for name_of_file, names in scanned:
        for name in names:
            if (name in sys.stdlib_module_names or name in local
                    or name in declared or name in exempt):
                continue
            undeclared.setdefault(name, set()).add(name_of_file)
    return undeclared


def test_the_undeclared_import_scanner_would_see_one():
    """合成資料的對照組：四種「該放行」與一種「該抓到」各驗一次。

    沒有這支的話，`_undeclared_third_party` 直接 `return {}` 也會全綠。
    """
    scanned = [
        ("a.py", {"os", "discord", "mymodule", "pytest", "sneaky"}),
    ]
    got = _undeclared_third_party(
        scanned, {"discord"}, local={"mymodule"}, exempt={"pytest"})
    assert got == {"sneaky": {"a.py"}}, (
        f"合成資料應該只抓到 `sneaky`，實際 {got}")


def test_every_third_party_import_is_declared_in_requirements():
    """反向守門：`import` 得到**不等於**宣告過。

    既有的 `test_this_interpreter_has_every_declared_dependency` 只看
    「宣告了有沒有裝」。反方向沒人看：某支模組 `import` 了一個剛好裝在開發機上、
    但 `requirements.txt` 沒宣告的套件，本機一切正常，**fresh clone 會在
    import 期就 `ModuleNotFoundError`**——啟動器隨即 rapid-fail、supervisor 放棄，
    而 `requirements.txt` 看起來完全正常。這是安裝說明說謊的那一類錯，
    不是跑起來以後才發現的錯。
    """
    declared = set(b._declared_dependencies())
    local = _local_module_names()
    sources = _live_stack_sources()
    scanned = [(path.name, _top_level_imports(path)) for path in sources]

    # 正面對照組，兩側都要。掃不到檔案、或抽不出 import 名，`undeclared` 都會是
    # 空的，而空的看起來跟「乾淨」一模一樣。
    assert len(sources) >= 60, (
        f"只掃到 {len(sources)} 支原始碼（root 4 支 ＋ 套件 80+ 支），"
        "檔案選取壞了——這種情況下面那句斷言會空轉通過")
    assert len(declared) >= 10, (
        f"`requirements.txt` 只解析出 {len(declared)} 筆宣告，剖析壞了")
    seen = set().union(*(names for _, names in scanned))
    assert {"discord", "psutil"} <= seen, (
        f"連 `discord` / `psutil` 都沒從原始碼抽到，import 抽取壞了："
        f"抽到 {len(seen)} 個名字")

    undeclared = _undeclared_third_party(
        scanned, declared, local=local, exempt=_TEST_ONLY_IMPORTS)
    assert not undeclared, (
        "這些第三方套件被 import 了但 `requirements.txt` 沒宣告，fresh clone 會"
        f"直接 ImportError：{ {k: sorted(v) for k, v in undeclared.items()} }。"
        "補進 `requirements.txt`（順便寫清楚為什麼要它），套件名與 import 名"
        "不同的話同時補 `discord_bot._IMPORT_NAME_OVERRIDES`。")


def test_import_name_overrides_only_lists_entries_that_really_differ():
    """對照表只該收「套件名 `-` 換 `_` 之後仍然對不上 import 名」的那幾筆。

    收了名實相符的項目不會壞事，只會讓下一個人以為那個套件有什麼特殊之處。
    """
    for package_name, import_name in b._IMPORT_NAME_OVERRIDES.items():
        assert package_name.replace("-", "_") != import_name, (
            f"`{package_name}` 換掉 `-` 之後就是 `{import_name}`，"
            "不需要列進 `_IMPORT_NAME_OVERRIDES`。")


# --------------------------------------------------------------------------
# `api` 後端與實際裝著的 SDK 對得起來（平常沒人走的那條路）
# --------------------------------------------------------------------------
def _api_backend_call_kwargs() -> set[str]:
    """AST 取出 `_dorossi_via_api` 真正帶給 `messages.create(...)` 的關鍵字。

    不寫死第二份清單：寫死的那份不會跟著呼叫端改，而這個檢查存在的理由正是
    「呼叫端帶的東西 SDK 還收不收」。
    """
    import ast

    source = Path(db.__file__).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "create"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "messages"):
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError(
        "在 dorossi_backend 裡找不到 `….messages.create(…)`——"
        "`api` 後端被改寫了的話，這個守門也要跟著改。")


@pytest.mark.skipif(db.anthropic is None,
                    reason="anthropic SDK 未安裝（可選相依）")
def test_the_api_backend_still_fits_the_installed_anthropic_sdk():
    """`api` 後端平常沒人走，SDK 的破壞性改版會安靜地躺到有人用它那天才爆。

    2026-08-27 實測：`anthropic` 0.122 → 1.1.0 是 major bump——移掉了
    `temperature` / `top_p` / `top_k`（1.x 傳了直接 `TypeError`）、Text
    Completions、`LegacyAPIResponse`，HTTP 層換成 `httpx2`，Python 下限提到
    3.10。本專案這條路徑只帶 model／max_tokens／system／messages，四個都還在，
    所以不受影響——但那是**驗過**才知道的。`requirements.txt` 刻意不釘版本，
    fresh clone 拿到的就是當下最新版，所以這條要一直有人看。
    """
    import inspect

    from anthropic.resources.messages import AsyncMessages

    accepted = set(inspect.signature(AsyncMessages.create).parameters)
    passed = _api_backend_call_kwargs()
    assert passed, "呼叫端一個關鍵字都沒帶，八成是抽取寫錯了"
    unsupported = passed - accepted
    assert not unsupported, (
        f"`api` 後端帶的這些關鍵字，裝著的 anthropic "
        f"{db.anthropic.__version__} 已經不收：{sorted(unsupported)}。"
        "查該版的 MIGRATION.md；1.x 移掉的是 temperature／top_p／top_k。")


@pytest.mark.skipif(db.anthropic is None,
                    reason="anthropic SDK 未安裝（可選相依）")
def test_the_sdk_error_classes_the_api_backend_names_still_exist():
    """`_dorossi_api_is_usage_limit` 用 `getattr(anthropic, …, ())` 取類別。

    取不到時它退成空 tuple，`isinstance(exc, ())` 永遠是 False——**不會報錯，只會
    讓額度用完的錯誤被當成一般失敗**，使用者拿到泛用訊息而不是「額度用完、幾點
    回補」。那是安靜的降級，正是要靠測試看住的形態。
    """
    for name in ("RateLimitError", "AuthenticationError"):
        assert isinstance(getattr(db.anthropic, name, None), type), (
            f"`anthropic.{name}` 不見了——`_dorossi_api_is_usage_limit` 與 "
            "`discord_bot` 的認證錯誤分流會安靜地失效。")


@pytest.mark.skipif(db.anthropic is None,
                    reason="anthropic SDK 未安裝（可選相依）")
def test_the_api_client_pins_its_own_timeout_instead_of_inheriting_the_default(
        monkeypatch):
    """`api` 這條路沒有串流，SDK 的逾時**就是**唯一的時間界限。

    claude_code 那套兩層 watchdog（閒置 ＋ 硬性牆鐘）在這裡一層都用不上：
    `messages.create()` 就是一次 await。而 `anthropic` 在 `requirements.txt` 裡
    刻意不釘版本，所以「界限」如果沿用預設值，就等於「這一版 SDK 說了算」——
    無人值守的自走迴圈最不該有的，就是一個會跟著相依套件升版無聲改變的時間上限。

    2026-09-05 實測 anthropic 1.3.0 的預設是 read 600s ＋ 重試 2 次，所以明寫這兩個
    值當下不改變任何行為；它擋的是**之後**的漂移。2026-09-09 升到 1.4.0 後重量一次
    ——`Timeout(connect=5.0, read=600, write=600, pool=600)`、`max_retries=2`，沒有
    變。**重量比推論便宜**：這一行的價值就在於它是量出來的，不是抄上一版的。
    """
    recorded = {}

    class _Recorder:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(db, "AsyncAnthropic", _Recorder)
    monkeypatch.setattr(db, "_dorossi_client", None)
    client = db._get_dorossi_client()
    assert client is not None

    assert "timeout" in recorded and "max_retries" in recorded, (
        "`AsyncAnthropic()` 沒有明寫逾時／重試次數，等於把自走迴圈的時間界限"
        f"交給 SDK 的當前預設值決定。實際帶的關鍵字：{sorted(recorded)}")
    assert recorded["timeout"] == db.DOROSSI_API_TIMEOUT_SEC
    assert recorded["max_retries"] == db.DOROSSI_API_MAX_RETRIES


def test_the_api_bound_is_a_real_positive_number():
    """canary：常數被設成 None／0／負數，等於把界限拿掉，而上面那支照樣會過。"""
    assert isinstance(db.DOROSSI_API_TIMEOUT_SEC, (int, float))
    assert not isinstance(db.DOROSSI_API_TIMEOUT_SEC, bool)
    assert db.DOROSSI_API_TIMEOUT_SEC > 0
    assert db.DOROSSI_API_TIMEOUT_SEC == db.DOROSSI_API_TIMEOUT_SEC  # 不是 nan
    assert db.DOROSSI_API_TIMEOUT_SEC != float("inf")
    assert isinstance(db.DOROSSI_API_MAX_RETRIES, int)
    assert not isinstance(db.DOROSSI_API_MAX_RETRIES, bool)
    assert db.DOROSSI_API_MAX_RETRIES >= 0


# --------------------------------------------------------------------------
# 解碼外部圖片的兩道護欄（大小上限 ＋ 格式白名單）
# --------------------------------------------------------------------------
# `/booru --grid` 是全 repo 唯一一處把**外部來源的位元組**餵給影像解碼器的地方，
# 而那些位元組來自一個公開的使用者上傳站。兩個失敗形態都不會拋例外：
#   - 沒有大小上限 → `r.read()` 把整份回應吃進記憶體。HTTP 那邊的
#     `ClientTimeout` 管的是時間不是大小，擋不到。
#   - 沒有格式白名單 → `Image.open()` 會嗅探全部外掛。Pillow 12.0.0–12.2.0 的
#     EPS 解析器吃得下負的 byte count，`Image.open()` 就地無限迴圈
#     （CVE-2026-59203）。而它是**同步**呼叫、跑在事件迴圈上，卡住的是整個 bot。
# 兩者都被「順手簡化」回去的成本極高，所以在這裡釘住。


def _grid_image_open_call():
    """AST 取出 `discord_bot` 裡那個 `Image.open(...)` 呼叫節點。"""
    import ast

    source = Path(b.__file__).read_text(encoding="utf-8")
    found = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "open"
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "Image"]
    assert len(found) == 1, (
        f"`Image.open(...)` 的呼叫點有 {len(found)} 個，這支測試假設只有一個"
        "（拼圖下載）。新增了另一處就要一起判斷它吃的是不是外部位元組。")
    return found[0]


def test_external_image_decoding_pins_the_format_allowlist():
    """`Image.open()` 必須帶 `formats=`，否則 EPS 那條無限迴圈搆得到。"""
    call = _grid_image_open_call()
    keywords = {kw.arg for kw in call.keywords if kw.arg}
    assert "formats" in keywords, (
        "`Image.open()` 沒有帶 `formats=`。預設會嗅探全部外掛，包含 EPS——"
        "Pillow 12.0.0–12.2.0 的 EPS 解析器碰到負的 byte count 會就地無限迴圈"
        "（CVE-2026-59203），而這是同步呼叫、跑在事件迴圈上，卡住的是整個 bot。"
        "用 `GRID_ALLOWED_IMAGE_FORMATS`。")


def test_a_tiny_file_declaring_a_huge_image_is_refused_before_decoding():
    """位元組上限擋不住壓縮得很小、宣告尺寸很大的圖。9000×9000 的單色 PNG 只有幾百 KB，
    在 Pillow 的預設門檻以下，`convert("RGB")` 卻要約 240 MB。判斷要在解碼之前做。"""
    import io

    from PIL import Image

    def _png(size):
        buffer = io.BytesIO()
        Image.new("L", size, 0).save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    bomb = _png((9000, 9000))
    assert len(bomb) < b.GRID_MAX_IMAGE_BYTES, "前提：位元組上限擋不住它"
    assert 9000 * 9000 < Image.MAX_IMAGE_PIXELS, "前提：Pillow 自己的門檻也不會擋"
    assert b._grid_open_image(bomb) is None
    ok = b._grid_open_image(_png((600, 400)))
    assert ok is not None and ok.mode == "RGB" and ok.size == (600, 400)
    edge = int(b.GRID_MAX_IMAGE_PIXELS ** 0.5)
    assert b._grid_open_image(_png((edge, edge))) is not None, "剛好在上限內要收"


def test_the_format_allowlist_really_shuts_the_eps_plugin_out():
    """行為驗證：白名單要真的擋掉 EPS、又不能擋掉正常的圖。

    只檢查「有沒有帶 `formats=`」不夠——帶了一個沒有效果的值一樣會過。這裡拿
    裝著的 Pillow 實際跑一遍，所以之後換版本也還算數。
    """
    import io

    from PIL import Image, UnidentifiedImageError

    allowed = b.GRID_ALLOWED_IMAGE_FORMATS
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, format="PNG")
    png = buffer.getvalue()
    assert Image.open(io.BytesIO(png), formats=allowed).size == (8, 8), (
        "白名單把正常的 PNG 也擋掉了——那會讓拼圖整個失效。")

    eps = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n"
           b"%%EndComments\n%%EOF\n")
    with pytest.raises(UnidentifiedImageError):
        Image.open(io.BytesIO(eps), formats=allowed)
    # 反面：沒有白名單時 EPS 外掛真的搆得到（證明這道護欄不是裝飾）。
    try:
        Image.open(io.BytesIO(eps))
    except Exception:  # pylint: disable=broad-except
        pytest.skip("這個 Pillow 版本連無白名單都不收這份 EPS，反面對照無從驗證")


def test_external_image_download_has_a_size_cap():
    """下載端要有上限，而且是**讀取時**就攔，不能只看回應宣告的長度。

    **判準在 2026-09-07 換過一次，不是變弱。** 這裡原本要求原始碼裡出現
    `GRID_MAX_IMAGE_BYTES + 1`——那是舊寫法「多讀一個位元組才分得出剛好到上限與
    超過上限」的痕跡。而那個寫法本身是錯的：`aiohttp.StreamReader.read(n)` 是
    *read up to n*，單次呼叫會把 chunked 回應**截斷**（實測 38043 / 91898 bytes），
    圖片這一側的症狀是 Pillow 開不起來、那一格安靜地從拼圖裡消失。

    改成 `_external_apis.read_capped_body()` 迴圈讀到 EOF 之後，「剛好」與「超過」
    的界線由它內部的 `total > cap` 維持，語意不變；邊界本身由
    `test_external_apis.py::test_the_shared_reader_accepts_exactly_the_cap` 顧著。
    所以這裡改成驗**上限真的有被交給那支讀取器**——比原本的字串比對更嚴：字串在
    「有出現但沒被用到」時照樣是綠的。
    """
    import ast

    assert isinstance(b.GRID_MAX_IMAGE_BYTES, int)
    assert 0 < b.GRID_MAX_IMAGE_BYTES <= 64 * 1024 * 1024, (
        "上限離譜——這個值要小到足以擋住記憶體耗盡，又大到裝得下正常的 sample 圖。")
    source = Path(b.__file__).read_text(encoding="utf-8")
    assert "await r.read()" not in source, (
        "拼圖下載又變回無上限的 `await r.read()`。那會把整份回應吃進記憶體，"
        "而位元組來自公開的使用者上傳站。改用 "
        "`read_capped_body(r.content, GRID_MAX_IMAGE_BYTES)`。")

    tree = ast.parse(source)
    grid = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_send_danbooru_grid"]
    assert grid, "找不到 `_send_danbooru_grid`（改名了？）"
    capped = [c for c in ast.walk(grid[0])
              if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
              and c.func.id == "read_capped_body"]
    assert capped, (
        "拼圖下載沒有走 `read_capped_body`。手寫的單次 `r.content.read(cap + 1)` "
        "會截斷 chunked 回應——那不是「上限失效」，是**合法的圖被讀壞**，而且看起來"
        "像間歇性故障（截在哪裡取決於區塊邊界與網路時序）。")
    passed_cap = {a.id for call in capped for a in call.args
                  if isinstance(a, ast.Name)}
    assert "GRID_MAX_IMAGE_BYTES" in passed_cap, (
        f"`read_capped_body` 沒有拿到上限（實際傳入 {sorted(passed_cap)}）。"
        "名字有出現不等於值有被用到——沒有上限時，位元組來自公開的使用者上傳站。")


# --------------------------------------------------------------------------
# 核心架構不變量：bot 與 webrunner 只透過磁碟上的檔案溝通
# --------------------------------------------------------------------------
# `CLAUDE.md` 把這條列為 core architecture invariant，但在這幾條測試出現之前
# **沒有任何東西在檢查**。它的失敗形態不是當場壞掉——`from webrunner_novelai
# import X` 在開發機上 import 得到，測試也照過；壞掉的是**重啟不再是無害的**：
# 跨行程狀態一旦活在記憶體裡而不是磁碟上，任何一邊被 supervisor 重啟就會拿到
# 不一致的世界，而那要到正式跑批次時才看得出來。
#
# 「bot 從動態消化模組只拿純快照原語」那一條同理：把 `decide` / `simulate`
# 拉進 bot 不會報錯，只會讓預覽從「時間點快照」悄悄變成「第二份消化實作」，
# 然後兩份實作各自演化。
_WEBRUNNER_PREFIX = "webrunner"
# bot 可以拿的純快照原語（無狀態、不做逐角色重讀）。`ACTION_FALLBACK_SINGLE`
# 是一個常數字串，`/gen plan` 的預覽用它標示「這一對會退回單張」。
_BOT_ALLOWED_FROM_QUEUE_CONSUME = frozenset({
    "pair_todos", "END_SENTINEL", "is_end_marker", "ACTION_FALLBACK_SINGLE",
})
# 動態消化決策，**webrunner 專屬**。bot 碰到就是在複製第二份消化邏輯。
_WEBRUNNER_ONLY_CONSUMPTION = frozenset({"decide", "simulate"})


def _names_a_module(name: str) -> bool:
    """磁碟上有沒有一個叫這個名字的模組（套件內或 repo root 的 `.py` / 目錄）。

    `from <pkg> import <name>` 只有在 `<name>` 真的是模組時才等於「把那個模組載
    進來」；是函式或常數的話，被載入的是 `<pkg>`。分不開的話
    `from _supervisor import webrunner_exit_needs_human` 會被誤判成 import 了一個
    `webrunner_*` 模組。

    `test/` 也看：測試模組（`conftest`、`test_*`）在 2026-09-22 之前住在套件裡。
    """
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    for root in (package, package.parent, Path(__file__).resolve().parent):
        if (root / f"{name}.py").is_file():
            return True
        if (root / name).is_dir() and name not in ("__pycache__",):
            return True
    return False


def _module_imports(path: Path) -> set[str]:
    """一支檔案 import 到的**本專案模組**名。

    四種寫法都要看得到，而 2026-09-10 之前**第四種是隱形的**：

    | 寫法 | 名字藏在哪 |
    |---|---|
    | `import webrunner_novelai` | `Import.names[].name` |
    | `import axiomatic.webrunner_novelai` | 同上，取最後一段 |
    | `from axiomatic.webrunner_novelai import X` | `ImportFrom.module` 的最後一段 |
    | **`from axiomatic import webrunner_novelai`** | **`ImportFrom.names[].name`** |

    舊版只讀 `node.module`，所以最後一種只會抽到 `"axiomatic"`——模組本身完全
    看不見。那不是假想的寫法：`start_webrunner.py` 就是這樣寫的
    （`from axiomatic import _chrome_slot`），也就是說它是這個 repo 的既有慣用法。

    後果是三道守門一起失效，其中一道是 `CLAUDE.md` 的**核心架構不變量**：
    bot 只要寫 `from axiomatic import webrunner_novelai`，「bot 不得 import
    webrunner」與「套件模組不得 import `discord_bot`」兩條都不會叫。

    `asname` 也要收：`from axiomatic import webrunner_novelai as w` 之後那個模組
    仍然被載入了，改名不改變事實。

    **`ImportFrom.names` 那一半要過濾成「真的是模組的名字」。** 第一版把每一個
    alias 都收進來，於是 `from _supervisor import webrunner_exit_needs_human`
    看起來像 import 了一個 `webrunner_*` 模組——這道守門當場誤報。一個會亂叫的
    守門最後會被人關掉，而被關掉的守門等於不存在（本專案已經為這條收窄過
    `test_language` 與 `test_text_encoding` 的判準）。判準是「磁碟上找得到同名的
    `.py` 或套件目錄」，因為 `from <pkg> import <name>` 只有在 `<name>` 真的是
    模組時才等於「載入那個模組」；是函式或常數的話，載入的是 `<pkg>` 本身。
    """
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[-1])
            for alias in node.names:
                if _names_a_module(alias.name):
                    names.add(alias.name)
    return names


def _package_sources() -> list[Path]:
    """live stack 的模組。測試檔排除——`_test_presence_e2e.py`（手動 e2e，檔名
    是 `_test_` 開頭）本來就該 import bot 來驅動它，那不是循環相依。

    `test/` 也照同一個判準過濾：2026-09-22 之前 `conftest.py` 住在套件裡、在這個
    範圍內，搬到 `test/` 之後範圍照舊。"""
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    tests = Path(__file__).resolve().parent
    return sorted(p for p in (*package.glob("*.py"), *tests.glob("*.py"))
                  if not p.stem.lstrip("_").startswith("test_"))


def _repo_root_sources() -> list[Path]:
    """repo root 的腳本——它們也是 live stack，只是不住在套件目錄裡。

    **`*.py` 而不是 `start_*.py`。** 這裡撐的規則（不得循環 import bot）沒有指名
    任何模組，所以收窄成啟動腳本會讓 `install_autostart.py` 這種正式腳本整個落在
    視線外。
    """
    root = Path(__file__).resolve().parent.parent
    return sorted(root.glob("*.py"))


def test_the_bot_and_the_webrunners_never_import_each_other():
    """跨行程狀態留在磁碟上，重啟才會是無害的。

    規則寫在 `CLAUDE.md` 的 Module boundaries。這裡兩個方向都看：bot 不得
    import 任何 `webrunner_*`，`webrunner_*` 也不得 import bot。
    """
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    bot_imports = _module_imports(package / "discord_bot.py")
    smuggled = {name for name in bot_imports
                if name.startswith(_WEBRUNNER_PREFIX)}
    assert not smuggled, (
        f"`discord_bot` import 了 {sorted(smuggled)}——跨行程狀態必須留在磁碟上，"
        "不能靠 import 共享。要共用純邏輯的話，抽進被動共用模組。")
    for path in package.glob(f"{_WEBRUNNER_PREFIX}*.py"):
        assert "discord_bot" not in _module_imports(path), (
            f"`{path.stem}` import 了 `discord_bot`——同一條規則的反方向。")


# bot 與兩個 webrunner 變體**同時**直接 import 的專案模組。實測（2026-09-11）就是
# 這兩個。這是一個**審查閘**，不是一個 bug 偵測器：多一個共用模組會讓它變紅，而那
# 正是目的——核心架構不變量說兩側只能透過磁碟溝通，被動共用模組是唯一的例外通道，
# 所以「新增一條通道」這件事應該要有人看過，而不是某次重構的副產品。
#
# 為什麼不拿 `CLAUDE.md` 的 Permitted third channel 那份清單來對帳：那是一份**許可**
# 清單（兩側**可以**import 的），不是 as-built 清單，所以「文件列了但目前沒共用」是
# 正常狀態（實測 **12** 個名字裡有 **10** 個現在沒有真的共用）。拿它做等式比對只會
# 逼人把清單改成 as-built，而那會**刪掉**許可資訊。要守的只有反方向。
#
# ⚠️ **這兩個數字原本寫的是「18 個裡有 16 個」，兩個都錯**（2026-09-11 量出來的），
# 而且同一組錯數字**同時**寫在這裡與 `CLAUDE.md`。18 是把整段裡每個反引號名字都算
# 了——12 個許可通道 ＋ 四個 bot-only helper ＋ `_gui_control` ＋ `discord_bot`，而
# 緊接著的下一句明文說後面那六個**不是**通道。沒有任何東西在查它，跟 `_pid_alive`
# 那個數字詞一模一樣的形狀，所以現在
# `test_the_permitted_channel_sentence_counts_its_own_list` 把兩個數字都從「那個括號
# 裡的清單」與本常數推導出來。**清單本身仍然刻意不對帳**（理由見上）；被查的只有
# 「那句話自己算得對不對」。
_SHARED_CHANNELS_IN_USE = frozenset({"_batch_config", "_webrunner_shared"})


def _permitted_channel_claim(text: str) -> tuple[set[str], int | None, int | None]:
    """CLAUDE.md 那一段的（括號裡的清單, 分子, 分母）。

    刻意只抓 `**Permitted third channel:** passive shared modules (…)` 那一對括號
    裡的東西。段落後面還有四個 bot-only helper、`_gui_control` 與 `discord_bot`，
    它們**不是**通道——把整段的反引號名字一起收就會數到 18，而那正是原本那句話
    出錯的方式。
    """
    import re
    listed = re.search(
        r"\*\*Permitted third channel:\*\* passive shared modules \((.+?)\)",
        text, re.DOTALL)
    claim = re.search(r"\((\d+) of its (\d+) names are not shared", text)
    names = (set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", listed.group(1)))
             if listed else set())
    if not claim:
        return names, None, None
    return names, int(claim.group(1)), int(claim.group(2))


def _channel_claim_errors(names, shared, numerator, denominator) -> list[str]:
    """那句話自己算得對不對。抽成純函式的理由同 `_channel_drift`。

    修好之後真實資料上這支回空清單，所以呼叫端那句斷言**在乾淨資料上恆真**——
    刪掉它不會有任何測試變紅。下面
    `test_the_permitted_channel_arithmetic_actually_bites` 用合成資料釘住這支
    函式自己的邏輯。
    """
    errors = []
    if not names:
        errors.append("抽不到 Permitted third channel 那個括號裡的清單")
        return errors
    if denominator != len(names):
        errors.append(
            f"分母寫 {denominator}，但那個括號裡有 {len(names)} 個名字")
    not_shared = len(names - set(shared))
    if numerator != not_shared:
        errors.append(
            f"分子寫 {numerator}，但清單裡目前沒有共用的是 {not_shared} 個")
    return errors


def test_the_permitted_channel_sentence_counts_its_own_list():
    """`CLAUDE.md` 那句「N of its M names are not shared」要跟它自己的清單一致。

    **這一支當初就會抓到**：那句話寫的是「16 of its 18」，而括號裡只有 12 個名字、
    其中 10 個目前沒有共用。18 是把整段裡每個反引號名字都算了（含四個 bot-only
    helper 與 `_gui_control`、`discord_bot`），而緊接著的下一句明文說那些**不是**
    通道。整套測試那時是全綠的。

    ⚠️ **這不是在對帳那份清單**——那是刻意不做的（許可清單拿去做等式比對，會逼人
    把它改寫成 as-built，而那會刪掉許可資訊）。這裡只查「那句話自己算得對不對」，
    跟 `test_pid_liveness.test_the_documented_copy_count_matches_its_own_list`
    對「There are **three**」那個數字詞做的事完全同型。

    為什麼值得查：那句話的工作就是**說服下一個人不要去對帳那份清單**。它自己的
    數字站不住的時候，那個論述就跟著站不住，而最省事的反應是去改清單。
    """
    text = (Path(b.__file__).resolve().parent.parent
            / "CLAUDE.md").read_text(encoding="utf-8")
    names, numerator, denominator = _permitted_channel_claim(text)
    assert numerator is not None and denominator is not None, (
        "抽不到那句「N of its M names are not shared」——句子形狀變了，"
        "下面等於沒在查。")
    errors = _channel_claim_errors(
        names, _SHARED_CHANNELS_IN_USE, numerator, denominator)
    assert not errors, (
        "`CLAUDE.md` 那句話跟它自己的清單對不起來：\n  " + "\n  ".join(errors)
        + "\n加減一個許可通道時，那兩個數字要一起改。")


def test_the_permitted_channel_arithmetic_actually_bites():
    """合成對照：兩個數字各自要真的查得到，抽不到清單也要吵。

    沒有這一支，上面那句斷言是不可證偽的：數字對的時候
    `_channel_claim_errors` 永遠回空清單，刪掉它不會有測試變紅。
    """
    names = {"a", "b", "c", "d"}
    shared = {"a"}
    assert _channel_claim_errors(names, shared, 3, 4) == []
    assert len(_channel_claim_errors(names, shared, 3, 5)) == 1   # 分母錯
    assert len(_channel_claim_errors(names, shared, 4, 4)) == 1   # 分子錯
    assert len(_channel_claim_errors(names, shared, 4, 5)) == 2   # 兩個都錯
    # 抽不到清單看起來會跟「分母是 0」一樣，所以要單獨吵
    assert _channel_claim_errors(set(), shared, 0, 0) == [
        "抽不到 Permitted third channel 那個括號裡的清單"]


def _channel_drift(shared, declared):
    """回傳 `(未登記的, 已消失的, 比對過幾個名字)`。

    抽成 helper 的理由跟 `_stale_entries`（`test_secrecy`）完全一樣：真實資料上
    兩個方向**都是空集合**，所以直接寫在測試裡的話，把任一個 assert 刪掉、或把
    比對縮成只看第一個名字，結果都還是空的——變異會存活，而那不是「守門很穩」，
    是「守門沒有被測到」。合成資料的正控制才問得出這個問題。

    第三個回傳值是**分母**：呼叫端要能釘住「真的每個名字都比過了」，否則把輸入
    截短成一個名字同樣看不出來。
    """
    names = set(shared) | set(declared)
    undeclared = sorted(n for n in names if n in shared and n not in declared)
    gone = sorted(n for n in names if n in declared and n not in shared)
    return undeclared, gone, len(names)


def test_the_channel_drift_comparison_actually_bites():
    """正控制：合成資料餵進去，兩個方向都要叫，而且分母要對。

    這支存在的唯一理由是上面那個 helper 在真實資料上永遠回傳兩個空 list。沒有這支
    的話，`_channel_drift` 整個換成 `return [], [], 0` 也會全綠。
    """
    undeclared, gone, checked = _channel_drift(
        shared={"_batch_config", "_sneaked_in"},
        declared={"_batch_config", "_removed_long_ago"})
    assert undeclared == ["_sneaked_in"], (
        "合成的「兩側都 import 但沒登記」沒有被抓到，helper 的正方向壞了。")
    assert gone == ["_removed_long_ago"], (
        "合成的「登記了但已經不共用」沒有被抓到，helper 的反方向壞了。")
    assert checked == 3, (
        f"只比對了 {checked} 個名字，應該是 3——分母不對表示比對被截短了，"
        "而截短在真實（乾淨）資料上是完全看不出來的。")


def test_no_undeclared_channel_between_the_bot_and_the_webrunners():
    """兩側同時 import 的模組，必須是已經被記錄過的那幾個。

    `CLAUDE.md` 的核心架構不變量：`discord_bot` 與兩個 webrunner 變體只透過磁碟上的
    檔案溝通，唯一的例外是被動共用模組。既有的守門用**排除法**擋住了最糟的形狀
    （bot import `webrunner_*`、或反過來），而那是對的——新模組自動受管。但排除法
    看不到這一格：抽一段邏輯進一個新的共用模組、兩邊都 import，四道守門一個都不會
    叫，而通道數量就這樣多了一條，沒有人決定過。

    變紅的時候多半**不是 bug**：確認那個模組真的是無狀態／不碰 driver 的純邏輯，
    然後連同理由加進這裡與 `CLAUDE.md` 的 Permitted third channel。重點是那個「連同
    理由」——這支存在的唯一目的，就是讓那一步不能被跳過。
    """
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    local = _local_module_names()
    bot = _module_imports(package / "discord_bot.py") & local
    assert len(bot) >= 5, (
        f"只從 `discord_bot.py` 抽到 {len(bot)} 個專案 import（{sorted(bot)}），抽取器多半壞了"
        "——抽不到東西的話交集會是空的，這支會永遠通過。")
    variants = sorted(package.glob(f"{_WEBRUNNER_PREFIX}*.py"))
    assert len(variants) >= 2, (
        f"只找到 {len(variants)} 個 webrunner 變體，檔名慣例大概變了。")
    webrunner: set[str] = set()
    for path in variants:
        webrunner |= _module_imports(path) & local
    assert webrunner, "webrunner 那側一個專案 import 都沒抽到，抽取器壞了。"

    # `discord_bot` 有自己的循環守門；套件名本身（`axiomatic`）不是一條通道——
    # `from axiomatic import _chrome_slot` 這個**本 repo 自己的慣用寫法**會同時
    # 記下套件名與模組名，而有意義的訊號是後者。不排掉的話，哪天 webrunner 那側
    # 也用了這個寫法，這支就會指著套件名喊「多了一條通道」——一個誤報，而誤報最
    # 省事的修法是放寬比對，那會把整個閘門變成裝飾。
    not_a_channel = {"discord_bot", package.name}
    shared = (bot & webrunner) - not_a_channel
    undeclared, gone, checked = _channel_drift(shared, _SHARED_CHANNELS_IN_USE)
    assert checked >= len(_SHARED_CHANNELS_IN_USE), (
        f"只比對了 {checked} 個名字，但登記表就有 {len(_SHARED_CHANNELS_IN_USE)} 個"
        "——比對被截短了。")
    assert not undeclared, (
        f"這些模組現在同時被 bot 與 webrunner import，但沒有登記過："
        f"{undeclared}。被動共用模組是**唯一**的例外通道（`CLAUDE.md` 的 Module "
        "boundaries），多一條要有人決定過：確認它是無狀態、不碰 driver 的純邏輯，"
        "然後連同理由加進 `_SHARED_CHANNELS_IN_USE` 與 `CLAUDE.md` 的 "
        "Permitted third channel。")
    assert not gone, (
        f"`_SHARED_CHANNELS_IN_USE` 列了這些、但兩側已經不再同時 import 了："
        f"{gone}。少一條通道是好事，把它從這裡拿掉即可——留著的話這個閘門會比"
        "實際鬆，而下一次真的多一條時，紅字會少一個對照。")


def test_nothing_in_the_package_imports_the_bot():
    """`discord_bot` 是相依圖的頂端；任何模組回頭 import 它都會變成循環。

    `_external_apis`／`_help_strings`／`dorossi_backend`／`discord_rpc`／
    `_gui_control` 都是 bot-only helper，不是邊界通道——`CLAUDE.md` 明寫它們
    不得 import `discord_bot`。這條用**排除法**寫，所以以後新增的模組自動受管。
    """
    offenders = [path.name for path in _package_sources()
                 if path.name != "discord_bot.py"
                 and "discord_bot" in _module_imports(path)]
    assert not offenders, (
        f"這些模組 import 了 `discord_bot`，會造成循環：{offenders}。"
        "需要的常數／helper 往下搬到被動共用模組，不要往上 import。")


def test_the_bot_never_runs_the_dynamic_consumption_decisions():
    """預覽是**時間點快照**，消化決策留在 webrunner——這是刻意的分工。

    把 `decide` / `simulate` 拉進 bot 不會報錯，只會讓 `/gen plan` 從快照悄悄
    變成第二份消化實作，然後兩份各自演化。底下的原語是單一來源，所以改原語只要
    改一處；`CLAUDE.md` 的 Module boundaries 把這條寫成 consequence。
    """
    import ast

    source = (Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    used: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "_queue_consume"):
            used.add(node.attr)
        elif isinstance(node, ast.ImportFrom) and node.module and (
                node.module.split(".")[-1] == "_queue_consume"):
            used.update(alias.name for alias in node.names)
    assert used, "抓不到任何 `_queue_consume` 用法，八成是抽取寫錯了"
    dynamic = used & _WEBRUNNER_ONLY_CONSUMPTION
    assert not dynamic, (
        f"`discord_bot` 用到了 webrunner 專屬的動態消化決策 {sorted(dynamic)}。"
        "預覽要維持成時間點快照；需要的話把**純**原語抽出來共用。")
    extra = used - _BOT_ALLOWED_FROM_QUEUE_CONSUME
    assert not extra, (
        f"`discord_bot` 從 `_queue_consume` 多拿了 {sorted(extra)}。"
        "確認它真的是無狀態的純原語（不做逐角色重讀），是的話連同理由加進 "
        "`_BOT_ALLOWED_FROM_QUEUE_CONSUME`，並更新 `CLAUDE.md` 的那份列舉。")


# ---------------------------------------------------------------------------
# `CLAUDE.md` 的邊界列舉 ↔ 上面那兩個常數
#
# 上一支測試的訊息說「並更新 `CLAUDE.md` 的那份列舉」，而 `CLAUDE.md` 那段自己也寫
# 著「Widening the allowlist means editing the enumeration here too — **the test
# message says so**」。**訊息說了不等於有人在檢查**：在這幾支出現之前，加寬
# `_BOT_ALLOWED_FROM_QUEUE_CONSUME` 而不動 `CLAUDE.md`（或反過來）完全無聲——守門
# 照跑、集合還在、測試全綠，只有那份**唯一的規則來源**變成了假話。冷啟動的 session
# 與 subagent 讀的就是它。`_OWNER_ONLY_GROUPS` 那組對帳是模板（見本檔下方）。
#
# 另外一半更安靜：`_WEBRUNNER_ONLY_CONSUMPTION` 是**拒絕**清單，它的條目若因為改名
# 而對不到任何東西，那道禁令就只是一個永遠不會命中的字串。2026-09-11 在
# `test_verify_browser` 抓到過一模一樣的東西（`"write_pid_file"` 全 repo 不存在），
# 所以這裡順手把兩份常數的**存活性**也釘住。
# ---------------------------------------------------------------------------
_BOUNDARY_HEADING = "**The boundary line inside that channel:**"
_BOUNDARY_SPLIT = "**dynamic consumption decisions**"


def _boundary_paragraph() -> str:
    import re
    text = (Path(b.__file__).resolve().parent.parent
            / "CLAUDE.md").read_text(encoding="utf-8")
    match = re.search(
        re.escape(_BOUNDARY_HEADING) + r"(.+?)\n\n", text, re.DOTALL)
    return match.group(1) if match else ""


def _documented_boundary_names() -> tuple[set[str], set[str]]:
    """CLAUDE.md 那段裡的（允許名, webrunner 專屬名）。

    只認反引號裡的識別字：斜線指令（`/gen plan`）跳過，`_queue_consume.decide`
    這種帶模組前綴的取最後一段。
    """
    import re
    head, _, tail = _boundary_paragraph().partition(_BOUNDARY_SPLIT)

    def names(chunk: str) -> set[str]:
        found = set()
        for token in re.findall(r"`([^`]+)`", chunk):
            token = token.strip()
            if token.startswith("/"):
                continue
            token = token.rsplit(".", 1)[-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
                found.add(token)
        return found

    return names(head), names(tail)


def _public_api(path: Path) -> set[str]:
    """模組層級宣告出來的公開名字（函式／類別／常數），不含它 import 進來的。

    刻意不用 `dir(module)`：那會把 `NamedTuple`、`annotations` 這些 import 進來的
    東西也算成這個模組的 API，於是「這個名字還在不在」就問錯了對象。
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            if not node.name.startswith("_"):
                found.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) \
                        and not target.id.startswith("_"):
                    found.add(target.id)
        elif isinstance(node, ast.AnnAssign) \
                and isinstance(node.target, ast.Name) \
                and not node.target.id.startswith("_"):
            found.add(node.target.id)
    return found


def _pkg_root() -> Path:
    return Path(b.__file__).resolve().parent


def test_the_boundary_enumeration_is_extractable_from_claude_md():
    """正對照：抽不到的話，下面四支全部退化成空集合比較，永遠通過。"""
    paragraph = _boundary_paragraph()
    assert paragraph, (
        f"在 CLAUDE.md 找不到「{_BOUNDARY_HEADING}」那一段——"
        "那段被改寫或搬走了，下面的對帳等於沒在比。")
    allow, deny = _documented_boundary_names()
    assert len(allow) >= 4 and len(deny) >= 2, (
        f"只從 CLAUDE.md 抽到 allow={sorted(allow)} deny={sorted(deny)}，"
        "擷取器多半壞了（或那段的寫法變了）。")


def test_every_documented_boundary_name_is_a_real_symbol():
    """文件點名的每個符號，都要真的存在於某個被允許的共用模組裡。

    `character_folder_name` 住在 `_webrunner_shared`、其餘住在 `_queue_consume`，
    所以這裡問的是「兩個加起來有沒有」。一個打錯或改名的符號會讓那句規則變成
    考古題，而規則來源出錯比程式出錯更難發現——沒有任何症狀。
    """
    allow, deny = _documented_boundary_names()
    known = (_public_api(_pkg_root() / "_queue_consume.py")
             | _public_api(_pkg_root() / "_webrunner_shared.py"))
    assert len(known) >= 20, (
        f"只抽到 {len(known)} 個公開符號，擷取器壞了。")
    ghosts = sorted((allow | deny) - known)
    assert not ghosts, (
        f"CLAUDE.md 的邊界那段點名了這些符號，但兩個共用模組裡都沒有：{ghosts}。"
        "多半是改名之後忘了改規則來源；規則來源錯了不會有任何症狀。")


def test_the_documented_allowlist_matches_the_code_constant():
    """CLAUDE.md 的允許名（取住在 `_queue_consume` 的那些）↔ 程式常數，兩個方向。"""
    allow, _deny = _documented_boundary_names()
    in_queue_consume = allow & _public_api(_pkg_root() / "_queue_consume.py")
    assert in_queue_consume, "交集是空的——擷取器壞了，下面等於沒在比。"
    missing = sorted(in_queue_consume - _BOT_ALLOWED_FROM_QUEUE_CONSUME)
    stale = sorted(_BOT_ALLOWED_FROM_QUEUE_CONSUME - in_queue_consume)
    assert not missing, (
        f"CLAUDE.md 允許這些、但 `_BOT_ALLOWED_FROM_QUEUE_CONSUME` 沒有：{missing}。")
    assert not stale, (
        f"`_BOT_ALLOWED_FROM_QUEUE_CONSUME` 有這些、CLAUDE.md 沒寫：{stale}。"
        "CLAUDE.md 是唯一的規則來源，加寬允許清單要同時改那一句——"
        "不然規則來源描述的是一個比實際窄的閘門，而且不會有任何症狀。")


def test_the_documented_webrunner_only_names_match_the_code_constant():
    """`decide` / `simulate` 那半也要兩個方向對得起來。"""
    _allow, deny = _documented_boundary_names()
    missing = sorted(deny - _WEBRUNNER_ONLY_CONSUMPTION)
    stale = sorted(_WEBRUNNER_ONLY_CONSUMPTION - deny)
    assert not missing, (
        f"CLAUDE.md 說這些是 webrunner 專屬、但 `_WEBRUNNER_ONLY_CONSUMPTION` "
        f"沒有：{missing}。少一個就是少一道禁令。")
    assert not stale, (
        f"`_WEBRUNNER_ONLY_CONSUMPTION` 有這些、CLAUDE.md 沒寫：{stale}。")


def test_the_boundary_constants_still_name_real_queue_consume_symbols():
    """兩份常數裡的名字都要還是 `_queue_consume` 真的有的東西。

    ⚠️ 這一支守的是**拒絕清單會靜靜失效**那個形狀。`_queue_consume.decide` 改名之
    後，`_WEBRUNNER_ONLY_CONSUMPTION` 裡的 `"decide"` 就變成一個永遠不會命中的字串
    ——禁令沒了，而守門照跑、測試照綠。2026-09-11 在 `test_verify_browser` 抓到過
    一模一樣的東西：deny-list 裡的 `"write_pid_file"` **全 repo 不存在**，所以那條
    禁令從來沒有被真的檢查過。判準很便宜：黑名單裡的每個名字都要在某處真的找得到。
    """
    api = _public_api(_pkg_root() / "_queue_consume.py")
    assert len(api) >= 8, f"只抽到 {sorted(api)}，擷取器壞了。"
    for label, names in (("_BOT_ALLOWED_FROM_QUEUE_CONSUME",
                          _BOT_ALLOWED_FROM_QUEUE_CONSUME),
                         ("_WEBRUNNER_ONLY_CONSUMPTION",
                          _WEBRUNNER_ONLY_CONSUMPTION)):
        ghosts = sorted(set(names) - api)
        assert not ghosts, (
            f"`{label}` 裡的這些名字在 `_queue_consume` 已經不存在了：{ghosts}。"
            "對允許清單來說那只是條目多餘；對拒絕清單來說那是**禁令失效**——"
            "改名之後那個字串永遠不會命中，而測試照綠。")


def test_the_two_boundary_constants_are_disjoint():
    """同一個名字不可能既是 bot 可以拿的、又是 webrunner 專屬的。

    重疊的話 `test_the_bot_never_runs_the_dynamic_consumption_decisions` 的兩道
    斷言會互相矛盾：一個重疊的名字同時觸發「多拿了」與「碰到動態決策」，而先撞到
    哪一條取決於敘述順序。這種不一致要在來源處擋掉，不是在症狀處。

    （寫這段的時候第一版把上面那支的名字記錯了，引用到一個不存在的測試。抓到它的
    是 `test_no_comment_claims_a_guard_that_does_not_exist`。留這句話當提醒：
    **一句「有守門在看著」的註解本身就是一個會過期的斷言**，而且它過期的時候沒有
    症狀——讀的人只會更安心。順帶一提，把錯名字寫進這段說明來「記取教訓」同樣會被
    那支擋下來，那也是對的：它分不出、也不該分「我是在引用」與「我是在舉例」。）
    """
    overlap = sorted(_BOT_ALLOWED_FROM_QUEUE_CONSUME
                     & _WEBRUNNER_ONLY_CONSUMPTION)
    assert not overlap, (
        f"這些名字同時列在允許清單與 webrunner 專屬清單：{overlap}。")


# --------------------------------------------------------------------------
# 監看的新條件（port / process / clip）
# --------------------------------------------------------------------------
def test_every_watch_kind_has_a_poll_interval():
    """`cmd_watch` 用 `_WATCH_POLL` 當合法種類的白名單。

    漏掉一筆的後果不是報錯，是那個種類**永遠被當成打錯字**退回用法說明。
    """
    for kind in ("text", "window", "ui", "pixel", "job", "port", "process", "clip"):
        assert kind in b._WATCH_POLL, kind
    # 辨識一次要數秒，不能用兩秒去輪詢它
    assert b._WATCH_POLL["text"] >= b._WATCH_POLL["pixel"]


def test_watch_clip_compares_against_the_baseline_taken_at_creation(monkeypatch):
    """基準值必須是**建立監看那一刻**的內容。

    跟上一輪比的話，使用者複製走再複製回原樣就永遠不會觸發——而他只會看到監看
    一直沒成立，不會知道為什麼。
    """
    seen = {}

    def _changed(baseline, *, contains="", timeout=1.0):
        seen["baseline"] = baseline
        seen["contains"] = contains
        return True

    monkeypatch.setattr(b._gui, "clipboard_changed", _changed)
    got = asyncio.run(b._watch_condition_met(
        "clip", "", {"clip_baseline": "原本的內容"}))
    assert got is True
    assert seen == {"baseline": "原本的內容", "contains": ""}


def test_watch_clip_with_a_target_waits_for_that_text(monkeypatch):
    monkeypatch.setattr(
        b._gui, "clipboard_changed",
        lambda baseline, *, contains="", timeout=1.0: contains == "訂單編號")
    assert asyncio.run(b._watch_condition_met(
        "clip", "訂單編號", {"clip_baseline": ""})) is True


def test_watch_port_accepts_bare_port_and_host_port(monkeypatch):
    seen = []
    monkeypatch.setattr(b._gui, "port_open",
                        lambda host, port: seen.append((host, port)) or True)
    assert asyncio.run(b._watch_condition_met("port", "8080")) is True
    assert asyncio.run(b._watch_condition_met("port", "example.test:443")) is True
    assert seen == [("127.0.0.1", 8080), ("example.test", 443)]


def test_watch_probe_failure_reads_as_not_yet(monkeypatch):
    """探測失敗只代表「還沒成立」，不能讓整個監看死掉。"""
    def _boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(b._gui, "process_running", _boom)
    assert asyncio.run(b._watch_condition_met("process", "notepad.exe")) is False


# --------------------------------------------------------------------------
# 監看的目標：建立時驗、探測時讀，必須是同一支解析
#
# 2026-09-21 實測：建立時只驗 `port`，`pixel` / `job` 的目標卻在**每一輪探測**裡才
# 解析、而且包在「任何失敗＝還沒成立」的 `except` 裡。於是打錯的目標建立成功、每
# 一輪都解析失敗、兩小時後安靜過期，一句錯誤都沒有。
# --------------------------------------------------------------------------
def _create_watch(monkeypatch, payload, *, jobs=None):
    """跑一次 `cmd_watch` 的建立路徑。

    回傳 `(回覆, 登記下來的監看, 交給 _watch_loop 的位置參數)`。`_watch_loop` 換成
    只記錄的替身——這裡驗的是「建不建得起來」，不是輪詢。`jobs` 是
    `{編號: 還在跑嗎}`，模擬作業表；查不到的編號丟跟正式那一支一樣的 `GuiError`。
    """
    replies: list = []
    loops: list = []

    async def _rec(_message, content=None, **kwargs):
        replies.append(content)

    async def _fake_loop(*args, **kwargs):
        loops.append(args)

    def _job_log(job_id, lines=40):
        if not jobs or job_id not in jobs:
            raise b._GuiError("找不到這個作業編號。")
        return {"id": job_id, "running": jobs[job_id]}

    monkeypatch.setattr(b, "safe_reply", _rec)
    monkeypatch.setattr(b, "_watch_loop", _fake_loop)
    monkeypatch.setattr(b, "_WATCHES", {})
    monkeypatch.setattr(b, "_WATCH_NEXT_ID", 1)
    monkeypatch.setattr(b._gui, "job_log", _job_log)
    author = types.SimpleNamespace(id=b.OWNER_USER_ID, mention="<@1>")
    message = types.SimpleNamespace(author=author)

    async def _body():
        await b.cmd_watch(message, payload)
        await asyncio.sleep(0)          # 讓假的 `_watch_loop` task 跑完

    asyncio.run(_body())
    return replies, dict(b._WATCHES), loops


@pytest.mark.parametrize("payload", [
    "pixel 10 10 red",           # 斜線 `/watch pixel` 的 color 是自由字串，這是最常見的一種
    "pixel 10 10",               # 少一個值：舊探測是 IndexError
    "pixel 10 10 #ffffff 20",    # 多一個值：舊探測默默丟掉
    "pixel x 10 #ffffff",
    "pixel 10, 10 #ffffff",      # 逗號後面有空白 → 拆出一個空的座標
    "job abc",
    "job 0",
    "job 424242",                # 格式對、作業不存在
    "port notaport",
])
def test_a_bad_watch_target_is_rejected_at_creation(monkeypatch, payload):
    """打錯的目標要**現在**講，不是等兩小時後才回一句「一直沒成立」。

    作業表刻意放了編號 0：否則 `job 0` 會同時被「格式」與「不存在」兩道擋下，
    拿掉格式那一道照樣綠（變異實測 SURVIVED）。放進去之後只剩格式那一道擋得住它。
    """
    replies, watches, loops = _create_watch(
        monkeypatch, payload, jobs={0: True, 7: True})

    assert watches == {}, f"{payload!r} 被登記成監看了：{watches}"
    assert loops == [], f"{payload!r} 起了輪詢"
    assert len(replies) == 1 and replies[0].startswith("❌ "), replies
    # 回的是那一支解析自己的泛用句，不是用法說明、也不是原始例外文字。
    assert "Error" not in replies[0], replies[0]
    assert "用法" not in replies[0], replies[0]


@pytest.mark.parametrize("payload, target", [
    ("pixel 10 10 #ffffff", "10 10 #ffffff"),
    ("pixel 10,10 #ffffff", "10,10 #ffffff"),     # 跟 `/locate pixel` 一樣收 `x,y`
    ("pixel -5 20 30,144,255", "-5 20 30,144,255"),  # 負座標 ＋ `r,g,b`
    ("port 8080", "8080"),
    ("job 7", "7"),                                # 還在跑
    ("job 8", "8"),                                # 已經結束：照收，第一輪就成立
    ("text 存檔", "存檔"),                          # 自由文字，沒有解析
])
def test_a_valid_watch_target_still_creates_a_watch(monkeypatch, payload, target):
    """反方向：收緊之後合法的寫法照樣建得起來。

    少了這一支，「一律拒絕」也會讓上面那支全綠。
    """
    replies, watches, loops = _create_watch(
        monkeypatch, payload, jobs={7: True, 8: False})

    assert len(watches) == 1, (payload, replies)
    assert any("已開始監看" in (r or "") for r in replies), replies
    assert len(loops) == 1
    # 交給輪詢的仍是原本的目標字串——解析結果不偷渡，探測自己用同一支再解析。
    assert loops[0][1:3] == (payload.split()[0], target), loops[0]


def test_watch_creation_and_probe_use_the_same_parser(monkeypatch):
    """建立與探測從同一張表拿解析——換掉表裡那一格，兩邊都得跟著走。

    兩邊各寫一份就是這次缺陷的成因：建立端照收的字串，探測端讀不懂。
    """
    seen: list = []

    def _spy(raw):
        seen.append(raw)
        return 3, 4, (1, 2, 3)

    monkeypatch.setitem(b._WATCH_TARGET_PARSERS, "pixel", _spy)
    replies, watches, _ = _create_watch(monkeypatch, "pixel 任意寫法")
    assert len(watches) == 1, replies
    assert seen == ["任意寫法"], "建立時沒有走表裡的解析"

    probed: list = []
    monkeypatch.setattr(b._gui, "pixel_color",
                        lambda x, y: probed.append((x, y)) or (1, 2, 3))
    assert asyncio.run(b._watch_condition_met("pixel", "任意寫法")) is True
    assert seen == ["任意寫法", "任意寫法"], "探測時沒有走表裡的解析"
    assert probed == [(3, 4)], "探測沒有用解析出來的座標"


def test_watch_job_existence_is_checked_with_the_parsed_id(monkeypatch):
    """作業存在與否是拿**解析後**的編號去問，不是拿原字串。

    解析換成「任何字都回 7」：作業表裡有 7 就建得起來、沒有就被擋——兩格都成立，
    才證明問作業表的是解析結果（拿原字串去問的話，兩格都會被擋）。
    """
    monkeypatch.setitem(b._WATCH_TARGET_PARSERS, "job", lambda raw: 7)

    replies, watches, _ = _create_watch(monkeypatch, "job 第七號", jobs={7: True})
    assert len(watches) == 1, replies

    replies, watches, _ = _create_watch(monkeypatch, "job 第七號", jobs={8: True})
    assert watches == {}, watches
    assert replies and replies[0].startswith("❌ "), replies


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "7 8", "", "7.5"])
def test_the_watch_job_parser_rejects_a_non_positive_or_malformed_id(raw):
    with pytest.raises(b._GuiError):
        b._parse_watch_job_target(raw)


def test_the_watch_job_parser_accepts_a_positive_id():
    assert b._parse_watch_job_target("7") == 7
    assert b._parse_watch_job_target(" 12 ") == 12


def test_watch_pixel_probe_reads_the_comma_coordinate_form(monkeypatch):
    """`10,10 #ffffff` 以前建得起來、每一輪都 `x 必須是整數`。"""
    probed: list = []
    monkeypatch.setattr(b._gui, "pixel_color",
                        lambda x, y: probed.append((x, y)) or (250, 255, 245))
    assert asyncio.run(b._watch_condition_met("pixel", "10,10 #ffffff")) is True
    assert probed == [(10, 10)]


def test_a_watch_target_parse_failure_is_not_swallowed_as_not_yet(monkeypatch):
    """解析失敗每一輪都一樣，吞成「還沒成立」＝一個永遠不會觸發的監看。

    正常路徑上建立時就擋掉了；萬一有別條路繞過它，例外要往上走到 `_watch_loop`
    （它會告訴使用者監看異常結束），不能在探測裡消失。
    """
    monkeypatch.setattr(b._gui, "pixel_color", lambda x, y: (0, 0, 0))
    with pytest.raises(b._GuiError):
        asyncio.run(b._watch_condition_met("pixel", "10 10 red"))
    with pytest.raises(b._GuiError):
        asyncio.run(b._watch_condition_met("job", "abc"))


def test_a_transient_pixel_probe_failure_still_reads_as_not_yet(monkeypatch):
    """反方向：解析過了、探測本身失敗（桌面暫時拿不到）照舊當「還沒成立」。"""
    def _locked(x, y):
        raise b._GuiError("桌面目前無法操作。")

    monkeypatch.setattr(b._gui, "pixel_color", _locked)
    assert asyncio.run(b._watch_condition_met("pixel", "10 10 #ffffff")) is False


@pytest.mark.parametrize("rows, expected", [
    ([{"id": 7, "running": True}], False),     # 還在跑
    ([{"id": 7, "running": False}], True),     # 結束了
    ([{"id": 8, "running": True}], True),      # 被清掉了：紀錄只在結束後才會消失
    ([], True),
])
def test_watch_job_probe(monkeypatch, rows, expected):
    """作業紀錄只有**結束之後**才會被清掉（`_job_prune` / `job_clear`）。

    所以建立時存在、現在查不到＝已經結束。舊的探測在查不到時丟「找不到」，被吞成
    「還沒成立」——作業一結束就被 `/host job clear` 掉的話，監看永遠等不到。
    """
    monkeypatch.setattr(b._gui, "job_list", lambda: rows)
    assert asyncio.run(b._watch_condition_met("job", "7")) is expected


def test_the_job_record_is_only_ever_dropped_after_it_finishes():
    """上面那支「查不到＝結束」的前提：作業表只在兩個地方刪東西，兩處都只刪已結束的。

    這是 `_gui_control` 的性質，不是 bot 的；哪天有人加了一條會刪掉**還在跑**的
    作業的路徑，監看就會把它誤報成已結束（`--then` 會提早執行），所以前提要釘住。
    """
    import ast

    source = Path(b._gui.__file__).read_text(encoding="utf-8")
    functions = {fn.name: fn for fn in ast.walk(ast.parse(source))
                 if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _drops_a_job(node):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("pop", "clear", "popitem")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_JOBS"):
            return True
        return isinstance(node, ast.Delete) and any(
            isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
            and t.value.id == "_JOBS" for t in node.targets)

    def _filters_on_finished(fn):
        # `…["finished"] is not None`，用 AST 比，不比原始碼子字串（註解也會命中）。
        return any(
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Subscript)
            and isinstance(node.left.slice, ast.Constant)
            and node.left.slice.value == "finished"
            and [type(op) for op in node.ops] == [ast.IsNot]
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is None
            for node in ast.walk(fn))

    droppers = {name for name, fn in functions.items()
                if any(_drops_a_job(node) for node in ast.walk(fn))}
    assert droppers == {"_job_prune", "job_clear"}, droppers
    for name in sorted(droppers):
        assert _filters_on_finished(functions[name]), (
            f"`{name}` 刪作業紀錄時不再只挑已結束的——監看的「查不到＝結束」不成立了")


def test_expand_coord_pair_only_splits_a_single_comma_first_token():
    """`/locate pixel` 與 `/watch pixel` 共用的座標寫法。"""
    assert b._expand_coord_pair(["100,200", "#fff000"]) == ["100", "200", "#fff000"]
    # 兩個逗號是 `r,g,b`，不是座標對
    assert b._expand_coord_pair(["30,144,255"]) == ["30,144,255"]
    assert b._expand_coord_pair(["10", "10,20"]) == ["10", "10,20"]
    assert b._expand_coord_pair([]) == []


# --------------------------------------------------------------------------
# virtualenv 轉接殼：同一個背景程式在 cmdline 掃描裡會出現兩次
# --------------------------------------------------------------------------
def test_stub_and_real_process_collapse_into_one_entry():
    r"""`.venv\Scripts\python.exe` 是轉接殼，會 spawn base 直譯器當子行程並活到
    它結束，所以掃描一定撞到兩筆（cmdline 相同、父子關係）。不併的話，一個
    正常執行中的背景程式會被永久誤報成「多了一個孤兒」。"""
    import _process_control as pc

    raw = [(100, 50, "webrunner_novelai.py"),   # 轉接殼（Popen.pid 是它）
           (101, 100, "webrunner_novelai.py")]  # 本尊（pid 檔寫的是它）
    _eq(pc.collapse_interpreter_stub_pairs(raw), [(100, "webrunner_novelai.py")],
        "一對併成一筆，留外層")


def test_exclude_pid_recognises_both_halves_of_the_pair():
    """呼叫端手上的 pid 可能是外層（Popen.pid）也可能是內層（pid 檔）。
    只認一半就等於少排除一筆——那一筆會被當成孤兒報出去。"""
    import _process_control as pc

    raw = [(100, 50, "webrunner_novelai.py"),
           (101, 100, "webrunner_novelai.py")]
    _eq(pc.collapse_interpreter_stub_pairs(raw, exclude_pid=100), [], "給外層")
    _eq(pc.collapse_interpreter_stub_pairs(raw, exclude_pid=101), [], "給內層")


def test_a_genuine_orphan_still_shows_up_next_to_a_tracked_pair():
    """併對不可以把真的孤兒一起吃掉——那才是這個掃描存在的理由。"""
    import _process_control as pc

    raw = [(100, 50, "webrunner_novelai.py"),
           (101, 100, "webrunner_novelai.py"),
           (700, 1, "webrunner_je_only.py")]   # 上次 crash 留下的，無父無母
    _eq(pc.collapse_interpreter_stub_pairs(raw, exclude_pid=100),
        [(700, "webrunner_je_only.py")], "只剩真孤兒")


def test_a_lone_process_without_a_stub_is_untouched():
    """不是每個直譯器都有轉接殼（`py -3` 直接跑就沒有），單筆不可以被吃掉。"""
    import _process_control as pc

    raw = [(700, 1, "webrunner_novelai.py")]
    _eq(pc.collapse_interpreter_stub_pairs(raw),
        [(700, "webrunner_novelai.py")], "單筆保留")


# --------------------------------------------------------------------------
# 本機 Rich Presence：「收下了」不等於「廣播出去了」
# --------------------------------------------------------------------------
class _FakeActivity:
    def __init__(self, application_id=None):
        self.application_id = application_id


class _FakeMember:
    def __init__(self, activities):
        self.activities = activities


def _patch_rpc_client_id(monkeypatch, client_id):
    monkeypatch.setattr(
        b.discord_rpc, "load_rpc_config", lambda: {"client_id": client_id})


def test_broadcast_check_sees_our_own_activity_card(monkeypatch):
    _patch_rpc_client_id(monkeypatch, "500000000000000002")
    monkeypatch.setattr(b, "_find_target_member", lambda: _FakeMember(
        [_FakeActivity(999), _FakeActivity(500000000000000002)]))
    _eq(b._rpc_broadcast_state(True),
        {"state": "broadcast", "index": 1, "total": 2},
        "找得到自己那張，並回報它排第幾")


def test_broadcast_check_reports_rank_not_just_yes_no(monkeypatch):
    """「有廣播」還不夠：排第一張與排在別人後面是兩種完全不同的處置。

    平台自己的偵測器會送它自己的活動，我們的優先序管不到那張——所以「送出去了
    卻不是帳號上顯示的那張」必須跟「根本沒廣播」分得開，否則只能靠翻行程開啟
    時間考古（2026-08-23 就這樣浪費了一輪）。"""
    _patch_rpc_client_id(monkeypatch, "500000000000000002")
    # 我們那張排第一 → 就是顯示的那張。
    monkeypatch.setattr(b, "_find_target_member", lambda: _FakeMember(
        [_FakeActivity(500000000000000002), _FakeActivity(999)]))
    _eq(b._rpc_broadcast_state(True),
        {"state": "broadcast", "index": 0, "total": 2}, "排第一")
    # 唯一一張 → index 0、total 1。
    monkeypatch.setattr(b, "_find_target_member", lambda: _FakeMember(
        [_FakeActivity(500000000000000002)]))
    _eq(b._rpc_broadcast_state(True),
        {"state": "broadcast", "index": 0, "total": 1}, "唯一一張")
    # 前面卡了兩張別人的 → index 2。
    monkeypatch.setattr(b, "_find_target_member", lambda: _FakeMember(
        [_FakeActivity(1), _FakeActivity(2),
         _FakeActivity(500000000000000002)]))
    _eq(b._rpc_broadcast_state(True),
        {"state": "broadcast", "index": 2, "total": 3}, "被兩張擋在前面")


def test_broadcast_check_ignores_other_apps_activities(monkeypatch):
    """使用者身上有**別的** app 的活動卡片，不能被當成「我們的有廣播」。
    少了這條，比對寫反了也不會有測試變紅。"""
    _patch_rpc_client_id(monkeypatch, "500000000000000002")
    monkeypatch.setattr(b, "_find_target_member",
                        lambda: _FakeMember([_FakeActivity(999)]))
    _eq(b._rpc_broadcast_state(True),
        {"state": "not-broadcast", "index": None, "total": 1},
        "別人的卡片不算我們有廣播")


def test_broadcast_check_catches_accepted_but_never_broadcast(monkeypatch):
    """這正是 2026-08-17 卡住的形態：送出那一側全部正常，對外卻空的。"""
    _patch_rpc_client_id(monkeypatch, "500000000000000002")
    monkeypatch.setattr(b, "_find_target_member", lambda: _FakeMember([]))
    _eq(b._rpc_broadcast_state(True),
        {"state": "not-broadcast", "index": None, "total": 0},
        "送出那側全綠、對外卻空的")


def test_broadcast_check_stays_quiet_when_there_is_nothing_to_show(monkeypatch):
    """沒有要顯示的活動時看不到卡片本來就是對的，不可以報成警告。"""
    _patch_rpc_client_id(monkeypatch, "500000000000000002")
    monkeypatch.setattr(b, "_find_target_member", lambda: _FakeMember([]))
    assert b._rpc_broadcast_state(False) is None


def test_broadcast_check_is_undecided_when_the_user_is_not_visible(monkeypatch):
    """找不到成員資料 ≠ 沒廣播。分不出來就要回 None，不能誣賴開關沒開。"""
    _patch_rpc_client_id(monkeypatch, "500000000000000002")
    monkeypatch.setattr(b, "_find_target_member", lambda: None)
    assert b._rpc_broadcast_state(True) is None


def test_broadcast_check_needs_a_numeric_client_id(monkeypatch):
    """沒設定 client_id 時無從比對，一樣回 None。"""
    _patch_rpc_client_id(monkeypatch, "")
    monkeypatch.setattr(b, "_find_target_member", lambda: _FakeMember([]))
    assert b._rpc_broadcast_state(True) is None


# ---------------------------------------------------------------------------
# help 分塊：單一段落過長時必須切得動
# ---------------------------------------------------------------------------
# 2026-08-19 之前 `_send_help` 只在段落之間切，所以一個超過上限的段落會原封不
# 動送出去、被平台以 2000 字元拒絕（50035）。三種語言都會中，而且沒有任何測試
# 守著——`!help` 會送出前幾塊之後就掛掉。
_HELP_LIMIT = 1900


def _help_chunks(sections):
    """重現 `_send_help` 的分塊，只是不真的送出去。"""
    chunks, buf = [], ""
    for section in sections:
        for piece in b._split_help_section(section):
            addition = piece if not buf else "\n" + piece
            if len(buf) + len(addition) > _HELP_LIMIT:
                if buf:
                    chunks.append(buf)
                buf = piece
            else:
                buf += addition
    if buf:
        chunks.append(buf)
    return chunks


def test_short_sections_are_returned_untouched():
    """一般情況不該被切——切了會讓排版無故斷開。"""
    section = "## 標題\n- `/queue`\n- `/eta`\n"
    assert b._split_help_section(section) == [section]


def test_an_oversized_section_is_split_below_the_limit():
    section = "\n".join("- " + "x" * 90 for _ in range(60))
    pieces = b._split_help_section(section)
    assert len(pieces) > 1
    assert all(len(p) <= _HELP_LIMIT for p in pieces)
    # 內容不得遺失（只是換行處被拿來當切點）。
    assert "".join(pieces).replace("\n", "") == section.replace("\n", "")


def test_a_single_line_longer_than_the_limit_is_hard_split():
    """病態情況：連一行都塞不下。送不出去比切在奇怪的位置糟得多。"""
    pieces = b._split_help_section("y" * 5000)
    assert all(len(p) <= _HELP_LIMIT for p in pieces)
    assert "".join(pieces) == "y" * 5000


@pytest.mark.parametrize("lang", ["zh-tw", "zh-cn", "en"])
def test_every_help_chunk_fits_the_platform_message_limit(lang):
    """真正的迴歸測試：三語 help 實際切出來的每一塊都要送得出去。

    **驗證過會紅**：把 `_split_help_section` 改成直接 `return [section]`
    （也就是修好之前的行為）並塞一個長段落進語料，這一筆就會失敗。
    """
    pack = _help.HELPS[lang]
    chunks = _help_chunks(list(pack["channel"]) + list(pack["mention"]))
    assert chunks, "help 語料是空的"
    oversized = [(i, len(c)) for i, c in enumerate(chunks) if len(c) > 2000]
    assert not oversized, (
        f"{lang} help 有 {len(oversized)} 塊超過平台的 2000 字元上限："
        f"{oversized}。超過的那一塊會被拒絕（50035），使用者會收到前幾塊之後"
        "就斷掉。")


# ---------------------------------------------------------------------------
# 斜線指令的全域閘門 `_tree_check`
# ---------------------------------------------------------------------------
# 這是 2026-08-19 之後**所有**斜線指令都會經過的唯一一道閘。它同時做四件事：
# 頻道閘、角色閘、指令計數、稽核紀錄；順序與 `on_message` 的 `!` 派發器一致
# （拒絕的請求不計數也不稽核）。改壞這裡等於一次改壞 261 個指令的權限。
class _FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, **kwargs):
        self.sent.append((content, kwargs))


class _FakeInteraction:
    """`_tree_check` 只碰得到的那幾個屬性。"""

    def __init__(self, command, uid, channel_id, itype):
        self.command = command
        self.user = types.SimpleNamespace(id=uid)
        self.channel = types.SimpleNamespace(id=channel_id)
        self.channel_id = channel_id
        self.guild = None
        self.id = 12345
        self.type = itype
        self.response = _FakeResponse()
        self.extras = {}
        self.namespace = types.SimpleNamespace()


def _fake_command(qualified, extras):
    return types.SimpleNamespace(qualified_name=qualified, extras=extras)


def _run_gate(command, uid, channel_id, itype=None):
    if itype is None:
        itype = discord.InteractionType.application_command
    interaction = _FakeInteraction(command, uid, channel_id, itype)
    allowed = asyncio.run(b._tree_check(interaction))
    return allowed, interaction


_KILL = None  # built lazily so the module-level constants are read at call time


def _kill_cmd():
    return _fake_command("proc kill", {"bang": "!kill"})


def test_autocomplete_short_circuits_before_metrics_and_audit(monkeypatch):
    """⚠️ `interaction_check` 在 `CommandTree._call` 的**第一行**執行，而自動補全
    分支在二十幾行之後——所以每敲一個字都會進到這個鉤子。

    少了早退，使用者每個按鍵都會寫一筆稽核、加一次計數，而且對補全 interaction
    呼叫 `send_message` 會直接錯。**驗證過會紅**：拿掉鉤子第一行的早退，這一筆
    立刻失敗。
    """
    audit = []
    monkeypatch.setattr(b, "_audit_log", lambda m, h, r: audit.append(h))
    before = b._METRICS_CMD_COUNTS.get("/proc.kill", 0)
    allowed, interaction = _run_gate(
        _kill_cmd(), 999_999_999, 111_111_111,
        discord.InteractionType.autocomplete)
    assert allowed is True
    assert not audit, "自動補全不該寫稽核紀錄"
    assert b._METRICS_CMD_COUNTS.get("/proc.kill", 0) == before
    assert not interaction.response.sent, "自動補全 interaction 不能回訊息"


def test_public_commands_work_outside_the_configured_channel(monkeypatch):
    monkeypatch.setattr(b, "_audit_log", lambda m, h, r: None)
    allowed, interaction = _run_gate(
        _fake_command("web wiki", {"public": True}), 999_999_999, 111_111_111)
    assert allowed is True
    assert not interaction.response.sent


def test_channel_gated_commands_are_refused_elsewhere(monkeypatch):
    monkeypatch.setattr(b, "_audit_log", lambda m, h, r: None)
    allowed, interaction = _run_gate(_kill_cmd(), 999_999_999, 111_111_111)
    assert allowed is False
    assert interaction.response.sent, "被拒時要給使用者回覆"
    assert interaction.response.sent[0][1].get("ephemeral") is True


def test_the_owner_keeps_the_cross_channel_bypass(monkeypatch):
    """與 `on_message` 的閘門同一條例外，不能只在一邊成立。"""
    monkeypatch.setattr(b, "_audit_log", lambda m, h, r: None)
    allowed, interaction = _run_gate(_kill_cmd(), b.OWNER_USER_ID, 111_111_111)
    assert allowed is True
    assert not interaction.response.sent


def test_allowed_invocations_are_counted_audited_and_carry_a_proxy(monkeypatch):
    """計數與稽核原本**只有** `!` 路徑有；斜線成為唯一介面後不補上，
    `/sys metrics` 會永遠是空的、`/host put` 也不會留下任何紀錄。

    用**沒有被主機控制閘鎖住**的指令：`/proc kill` 現在非擁有者一律拒絕，
    拿它當「放行」案例會測不到計數與稽核。
    """
    audit = []
    command = _fake_command("todo prompt add", {"bang": "!todo_prompt_add"})
    monkeypatch.setattr(b, "_audit_log", lambda m, h, r: audit.append(h))
    before = b._METRICS_CMD_COUNTS.get("/todo.prompt.add", 0)
    allowed, interaction = _run_gate(command, 999_999_999, b.CHANNEL_ID)
    assert allowed is True
    assert b._METRICS_CMD_COUNTS.get("/todo.prompt.add", 0) == before + 1
    assert audit == ["/todo.prompt.add"]
    assert isinstance(interaction.extras.get("proxy"),
                      b._InteractionMessageProxy)


def test_owner_still_gets_counted_and_audited_on_a_locked_command(monkeypatch):
    """主機控制閘擋的是「非擁有者」，不是「這個指令」——擁有者跑它照樣留痕。"""
    audit = []
    monkeypatch.setattr(b, "_audit_log", lambda m, h, r: audit.append(h))
    before = b._METRICS_CMD_COUNTS.get("/proc.kill", 0)
    allowed, _ = _run_gate(_kill_cmd(), b.OWNER_USER_ID, b.CHANNEL_ID)
    assert allowed is True
    assert b._METRICS_CMD_COUNTS.get("/proc.kill", 0) == before + 1
    assert audit == ["/proc.kill"]


def test_role_keys_reuse_the_existing_bang_permission_tables():
    """`extras={"bang": …}` 讓 `_VIEWER_COMMANDS` / `_ADMIN_COMMANDS` 原封不動
    繼續生效——權限分級沒有第二份定義。"""
    assert b._slash_required_role(_kill_cmd()) == "admin"
    assert b._slash_required_role(
        _fake_command("queue", {"bang": "!queue"})) == "viewer"
    assert b._slash_required_role(
        _fake_command("web wiki", {"public": True})) == "none"
    # 沒對應的落到 operator——與 `!` 端未列表指令的預設一致，不放鬆。
    assert b._slash_required_role(_fake_command("x y", {})) == "operator"


def test_the_generated_permission_lines_match_the_runtime_gate():
    """`commands/*.md` 每支指令底下那一行「限擁有者／權限：X 以上」是產生器**讀原始碼**
    推出來的，而真正擋人的是執行期的 `_is_owner_only_slash` 與 `_slash_required_role`。
    兩邊是同一條規則的兩份實作，各自的測試都綠，但沒有東西比對它們：哪天某個權限表
    或判斷多了一條規則，文件會安靜地告訴使用者錯的權限。這支拿**活的指令樹**逐一比對。

    「閘門在指令內部」那種限擁有者不在比對內——它藏在函式本體裡，執行期沒有可以問的
    述詞；那幾支只檢查它們沒被寫成別的權限。
    """
    import gen_command_docs  # noqa: PLC0415
    from discord import app_commands  # noqa: PLC0415

    surface = gen_command_docs.Surface()
    generated = {c["qualified"]: c for c in surface.commands}
    live = [c for c in b.tree.walk_commands() if isinstance(c, app_commands.Command)]
    text_for = {"none": None, "viewer": "權限：viewer 以上",
                "operator": "權限：operator 以上", "admin": "權限：admin 以上"}
    seen, mismatches = set(), []
    for command in live:
        name = command.qualified_name
        doc = generated.get(name)
        if doc is None:
            mismatches.append(f"/{name}：產生器沒有這支")
            continue
        owner = surface.owner_line(doc)
        if b._is_owner_only_slash(name) != (owner == gen_command_docs.OWNER_PRE):
            mismatches.append(f"/{name}：執行期限擁有者={b._is_owner_only_slash(name)}，"
                              f"文件寫 {owner!r}")
        if owner:
            continue
        role = b._slash_required_role(command)
        seen.add(role)
        if surface.role_line(doc) != text_for[role]:
            mismatches.append(f"/{name}：執行期要 {role}，文件寫 {surface.role_line(doc)!r}")
    # 對照組：空的指令樹、或只剩一種權限，比對起來會跟「全部一致」長得一樣。
    assert len(live) >= 250, len(live)
    assert {"none", "viewer", "operator"} <= seen, seen
    assert not mismatches, "\n".join(mismatches)


# ---------------------------------------------------------------------------
# 主機控制閘：控制 bot 那台電腦的指令只有擁有者能用
# ---------------------------------------------------------------------------
# 這一組守的是一個**設定無關**的性質。原本桌面控制掛在 role 系統下，而
# `_roles_configured()` 在三份 `user_roles` 清單都空時回 False——那是預設值，也是
# 本機當下的狀態——於是角色閘整個停用，「能在設定頻道發言」就等於「能敲鍵盤、
# 截主機螢幕、讀剪貼簿、殺行程」。所以下面每一筆都刻意**不設定角色**。
def _bot_ast():
    import ast
    source = Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py"
    text = source.read_text(encoding="utf-8")
    return ast.parse(text, str(source)), text


def _project_module_asts() -> list[tuple[str, "object"]]:
    """專案自己的**非測試**模組 → `[(檔名, AST)]`。

    給那些「規則跟模組無關」的守門用。列舉一份模組清單是 fail-open 的（下一個新
    模組不會自動被蓋到），所以這裡用 glob，而呼叫端要自己斷言抽到的數量下限——
    抽不到檔案時「零筆違規」跟「全部乾淨」在輸出上一模一樣。

    `legacy/` 不含在內（CLAUDE.md：唯讀參考，沒有任何東西 import 它）。

    `test/` 照同一個判準過濾：2026-09-22 之前 `conftest.py` 與手動 e2e 腳本住在套件
    裡、在這個範圍內，搬到 `test/` 之後範圍照舊。
    """
    import ast
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    root = package.parent
    tests = Path(__file__).resolve().parent
    out = []
    for path in (sorted(package.glob("*.py")) + sorted(tests.glob("*.py"))
                 + sorted(root.glob("*.py"))):
        if path.name.startswith("test_") or path.name == "__init__.py":
            continue
        try:
            out.append((path.name,
                        ast.parse(path.read_text(encoding="utf-8"), path.name)))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
    return out


_SLASH_HANDLER_QUALIFIED: dict[str, str] = {}


def _slash_declarations():
    """-> {qualified name: extras dict}，直接從宣告抽，不手寫清單。

    順帶填 `_SLASH_HANDLER_QUALIFIED`（處理函式名 → qualified name）。刻意當成
    副作用而不是另寫一支：那會把群組巢狀解析（`path_of`）抄第二份，而抄本會漂移。
    """
    import ast
    tree, _text = _bot_ast()
    groups = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.targets[0], ast.Name)):
            continue
        func = node.value.func
        if not getattr(func, "attr", getattr(func, "id", "")).endswith("Group"):
            continue
        info = {"name": None, "parent": None}
        for kw in node.value.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                info["name"] = kw.value.value
            elif kw.arg == "parent" and isinstance(kw.value, ast.Name):
                info["parent"] = kw.value.id
        if info["name"]:
            groups[node.targets[0].id] = info

    def path_of(var):
        info = groups[var]
        parent = info["parent"]
        if parent and parent in groups:
            return path_of(parent) + " " + info["name"]
        return info["name"]

    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) \
                    or getattr(dec.func, "attr", "") != "command":
                continue
            name, extras = None, {}
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name = kw.value.value
                elif kw.arg == "extras" and isinstance(kw.value, ast.Dict):
                    extras = {k.value: v.value
                              for k, v in zip(kw.value.keys, kw.value.values)
                              if isinstance(k, ast.Constant)
                              and isinstance(v, ast.Constant)}
            if not name:
                continue
            owner = getattr(dec.func.value, "id", "")
            prefix = (path_of(owner) + " ") if owner in groups else ""
            found[prefix + name] = extras
            # 順手記下「處理函式 → qualified name」。要問「這支處理函式受不受
            # 擁有者閘保護」時，唯一正確的答案是把 qualified name 餵給
            # `b._is_owner_only_slash`（production 的判準本身）——比對裝飾器的
            # 文字會在**巢狀群組**上答錯：`/host sh cd` 掛的是 `host_sh.command`
            # 而不是 `host_group.command`，但它的 qualified name 是 `host sh cd`，
            # 開頭仍然是 `host`，所以它其實是有閘的。`path_of` 已經把巢狀解開了。
            _SLASH_HANDLER_QUALIFIED[node.name] = prefix + name
    return found


SLASH_DECLS = _slash_declarations()
LOCKED_SLASH = {q for q in SLASH_DECLS if b._is_owner_only_slash(q)}


def test_the_locked_set_actually_covers_the_desktop_surface():
    """抽取本身要站得住腳——空集合會讓底下每一筆都空轉通過。"""
    assert len(SLASH_DECLS) >= 250, len(SLASH_DECLS)
    assert len(LOCKED_SLASH) >= 100, len(LOCKED_SLASH)
    for expected in ("input key press", "screen main", "clip read",
                     "win focus", "locate ui click", "macro run",
                     "watch process", "proc kill", "host sh run",
                     "sys restart", "config set", "schedule add",
                     "log clear", "gen image"):
        assert expected in LOCKED_SLASH, expected


def test_locked_commands_reject_a_non_owner_even_in_the_configured_channel():
    """**驗證過會紅**：拿掉 `_tree_check` 的主機控制閘，這一筆立刻失敗。

    刻意用設定好的頻道 ＋ 沒設定任何角色——預設狀態下最寬鬆的情況。
    """
    assert not b._roles_configured(), "這一筆的前提是角色系統停用（預設）"
    for qualified in sorted(LOCKED_SLASH):
        command = _fake_command(qualified, SLASH_DECLS[qualified])
        allowed, interaction = _run_gate(command, 999_999_999, b.CHANNEL_ID)
        assert allowed is False, qualified
        assert interaction.response.sent, qualified


def test_locked_commands_allow_the_owner():
    for qualified in sorted(LOCKED_SLASH):
        allowed, _ = _run_gate(_fake_command(qualified, SLASH_DECLS[qualified]),
                               b.OWNER_USER_ID, b.CHANNEL_ID)
        assert allowed is True, qualified


def test_the_gate_is_group_based_so_a_new_sub_command_is_covered_by_default():
    """fail-closed：往 `_OWNER_ONLY_GROUPS` 任一群新增子指令要**自動**受閘。

    寫死名單的話新指令會靜默地不受保護，而那正是這道閘存在的理由——所以這一支
    **不自己抄一份群名**，直接從 `_OWNER_ONLY_GROUPS` 推。

    ⚠️ 2026-09-10：原本這裡列的是寫死的九個群名。`schedule` 同日從列舉清單升級成
    第十個群組規則，這份名單沒有跟著動，於是**唯一走得到 `_gui.run_shell`（主機上
    的任意指令執行）的那一群，正好是這支 fail-closed 測試沒問到的那一群**。一支
    在講「別寫死名單」的測試，自己就是那份會過期的寫死名單。從來源推就不會再有
    下一次。

    下面三筆是**具名的機制對照**，上面那個迴圈涵蓋不到，所以要另外寫死：
    `host sh brand_new` 是**巢狀子群**（掛在 `host_sh.command` 上，閘認的是根群
    `host`，不是 `sh`）；另外兩筆釘住「子指令名稱從來沒有被宣告過也照樣受閘」，
    也就是 fail-closed 本身。
    """
    groups = sorted(b._OWNER_ONLY_GROUPS)
    # 正面對照：集合被清空的話下面的迴圈會跑零圈、然後**空集合通過**——那跟
    # 「每一群都受閘」長得一模一樣。先釘住真的問到了東西。
    assert len(groups) >= 5, f"群組規則塌了，這一支等於沒問：{groups}"
    for group in groups:
        invented = f"{group} brand_new_thing"
        assert b._is_owner_only_slash(invented), invented
    for invented in ("host sh brand_new", "clip exfiltrate", "proc spawn"):
        assert b._is_owner_only_slash(invented), invented


def _stale_lock_entries(declared, locked):
    """列出 `locked` 裡對不上 `declared` 的項目（`declared` 是指令樹抽出來的全名）。

    抽成純函式是為了讓它能有**自己的**正面對照組。清單乾淨的時候，把下面那支
    主測試裡的斷言整個拿掉本來就不會有人紅——所以「這個偵測器還看得見東西嗎」
    必須另外拿合成資料問一次，見 `test_the_stale_detector_would_see_a_rename`。
    """
    return sorted(set(locked) - set(declared))


def test_the_slash_lock_list_has_no_stale_entries():
    """`_OWNER_ONLY_SLASH` 的每一筆都要對得上指令樹裡真的存在的指令。

    這是 `CLAUDE.md` 明文承認的**列舉清單例外**：主機控制那九群走群組制所以
    fail-closed，零散的個別指令只能逐一列名。而列舉是 fail-**open** 的——把
    `/sys backfill_paths` 改名、或把 `/gen image` 搬進別的群，清單裡那一筆就變
    成一個永遠比對不到任何東西的字串，那個指令從此**沒有擁有者閘**。

    症狀是零：閘還在（`_is_owner_only_slash` 照常被呼叫）、集合還在、測試全綠，
    只是那一筆再也不會命中。旁邊的 `LOCKED_SLASH` 幫不上忙——它是拿
    `_is_owner_only_slash` 去**篩** `SLASH_DECLS`，過期項目只會讓它安靜地少一
    筆。`test_the_locked_set_actually_covers_the_desktop_surface` 也只點名了 18
    筆裡的 5 筆，其餘 13 筆改壞了沒有任何東西會紅。

    2026-09-09 補這一道時清單是乾淨的（18/18 全對得上），所以這不是在救火——
    是趁清單還乾淨的時候把守門補上，那是最便宜的時機。
    """
    # 正面對照組要兩邊都做。抽取器壞掉時「18 筆全部對不上」與「抽不到指令」在
    # 輸出上長得一模一樣，所以先問指令數；而清單被清空時 `stale` 會是空的、斷言
    # 永遠通過，所以也要問清單本身還在不在。
    assert len(SLASH_DECLS) >= 250, (
        "只從指令樹抽到 %d 個指令，抽取邏輯壞了——底下那句斷言的紅色會是假的"
        % len(SLASH_DECLS))
    assert len(b._OWNER_ONLY_SLASH) >= 10, (
        "`_OWNER_ONLY_SLASH` 只剩 %d 筆。清單被清空的話下面那句永遠通過。"
        % len(b._OWNER_ONLY_SLASH))

    stale = _stale_lock_entries(SLASH_DECLS, b._OWNER_ONLY_SLASH)
    assert not stale, (
        "`_OWNER_ONLY_SLASH` 這些項目在指令樹裡找不到對應的指令：%s。"
        "被改名或搬家的指令會**安靜地**失去擁有者閘——閘還在、集合還在、"
        "只是那一筆永遠不會命中。請對照 `discord_bot.py` 的宣告修正名稱，"
        "或在指令真的移進 `_OWNER_ONLY_GROUPS` 涵蓋的群之後把它刪掉。" % stale)


def test_the_group_lock_list_has_no_stale_entries():
    """`_OWNER_ONLY_GROUPS` 的每一筆也要對得上真的存在的指令群。

    這一筆現況**有**人守——但守得很脆弱，而且是意外守到的：靠的是
    `test_the_locked_set_actually_covers_the_desktop_surface` 那份手寫樣本剛好
    九群各點名了一個子指令（`win focus`、`proc kill`…）。第十群一旦加進來而沒人
    補樣本，把那一群改名就沒有任何東西會紅——跟 `_OWNER_ONLY_SLASH` 原本的洞是
    同一個形狀，只是還沒發生。這裡把它換成結構性的：群名從指令樹推出來，不靠
    樣本。

    群名取自葉指令完整名的第一段，所以不必再寫第三份抽取器；巢狀子群的完整名是
    「群 子群 指令」，第一段一樣是頂層群名。
    """
    declared_groups = {q.split(" ", 1)[0] for q in SLASH_DECLS if " " in q}
    assert len(declared_groups) >= 20, (
        "只推得出 %d 個指令群，抽取邏輯壞了" % len(declared_groups))
    assert len(b._OWNER_ONLY_GROUPS) >= 5, "群組清單空了，這一筆會永遠通過"

    stale = _stale_lock_entries(declared_groups, b._OWNER_ONLY_GROUPS)
    assert not stale, (
        "`_OWNER_ONLY_GROUPS` 這些群在指令樹裡不存在：%s。群被改名之後那一筆就"
        "不再命中，整群的子指令會**安靜地**失去擁有者閘。" % stale)


def test_the_stale_detector_would_see_a_rename():
    """偵測器自己的正面對照組：拿合成資料確認它真的看得見過期項目。

    需要這一支的理由跟 `test_the_scanner_sees_every_way_of_smuggling_a_name_out`
    一樣：上面那支主測試在清單乾淨時，**把它的斷言整個拿掉也不會紅**。真正在做
    事的是 `_stale_lock_entries`，所以牙齒要長在這裡。
    """
    declared = {"sys restart", "sys backfill_paths"}
    assert _stale_lock_entries(declared, frozenset(declared)) == []
    # 改名：清單留著舊名，指令樹只有新名。
    assert _stale_lock_entries(
        {"sys backfill_path"}, frozenset({"sys backfill_paths"})
    ) == ["sys backfill_paths"]
    # 搬家：`gen image` 移進別的群之後，舊的完整名不再存在。
    assert _stale_lock_entries(
        {"tool image"}, frozenset({"gen image"})) == ["gen image"]
    # 只往一個方向看：指令樹裡有、清單裡沒有的指令**不是**過期項目（絕大多數
    # 指令本來就不該被鎖）。把方向寫反的話這一筆會紅。
    assert _stale_lock_entries(
        {"sys restart", "web wiki"}, frozenset({"sys restart"})) == []


def test_the_slash_lock_list_does_not_shadow_the_group_rule():
    """列舉清單不得重複群組規則已經蓋到的東西。

    重複本身無害（兩個條件是 or），但它是一個訊號：寫的人不知道
    `_OWNER_ONLY_GROUPS` 已經**自動**涵蓋那一群現在與未來的所有子指令。留著會
    讓下一個人以為「這裡的做法是逐條列舉」，而 `CLAUDE.md` 明文禁止把群組規則
    換成列舉清單——那正是新子指令靜默失去保護的那條路。

    現況是零重疊（清單裡只有 `sys`／`config`／`log`／`gen` 這幾個**部分**受閘的
    群）。真的出現重疊時**不要**為了讓這一筆變綠就去刪清單裡的項目：先確認那一
    群是不是本來就該整群受閘，是的話刪的是清單、不是群組規則。

    ⚠️ 2026-09-10 之前這句話把 `schedule` 也列在「部分受閘」裡，而它當時是 4/4
    ——**全樹唯一整群都被列舉鎖住的群**。那不是筆誤造成的小事：這一支只看得見
    「清單重複了群組規則」（降級方向），看不見「整群都被列舉了、卻沒升級成群組
    規則」（升級方向），所以那個狀態可以一直是綠的。升級方向現在由
    `test_a_fully_enumerated_group_should_be_a_group_rule` 顧。
    """
    assert len(b._OWNER_ONLY_SLASH) >= 10, "清單空了，這一筆會永遠通過"
    shadowed = sorted(q for q in b._OWNER_ONLY_SLASH
                      if q.split(" ", 1)[0] in b._OWNER_ONLY_GROUPS)
    assert not shadowed, (
        "這些指令的群已經整群受 `_OWNER_ONLY_GROUPS` 保護，不需要再列進 "
        "`_OWNER_ONLY_SLASH`：%s。多餘的列舉會讓人誤以為這裡的做法是逐條列名。"
        % shadowed)


def _fully_enumerated_groups(declared, groups, listed):
    """列出「整群的子指令都被逐一列舉了、卻還沒升級成群組規則」的群。

    `declared` 是指令樹抽出來的完整名集合，`groups` 是 `_OWNER_ONLY_GROUPS`，
    `listed` 是 `_OWNER_ONLY_SLASH`。

    抽成純函式的理由跟 `_stale_lock_entries` 一模一樣，而且這一支更需要：
    **修好之後真實資料就變乾淨了**，於是主測試那句斷言在真實資料上問不出任何
    問題——把它整個刪掉會存活。牙齒因此長在這裡，由
    `test_the_upgrade_detector_would_see_a_fully_enumerated_group` 拿合成資料問。
    """
    subs = {}
    for qualified in declared:
        head, _, rest = qualified.partition(" ")
        if rest:  # 頂層指令沒有群，不會有「整群」可言
            subs.setdefault(head, set()).add(qualified)
    listed = set(listed)
    return sorted(group for group, members in subs.items()
                  if group not in groups and members <= listed)


def test_a_fully_enumerated_group_should_be_a_group_rule():
    """升級方向：一個群的**每一個**子指令都被列舉了 → 它就該是群組規則。

    這是 `test_the_slash_lock_list_does_not_shadow_the_group_rule` 的反面，而且
    是**危險的那一面**。那一支看的是降級方向（清單重複了群組規則已經蓋到的東
    西），重複本身無害；這一支看的是「該用群組規則卻用了列舉」，而列舉是
    fail-**open** 的——多一個子指令就預設沒有閘。

    2026-09-10 之前 `schedule` 正是這個狀態：4 個子指令 4 個都在
    `_OWNER_ONLY_SLASH` 裡，是全樹唯一整群都被列舉鎖住的群，而且沒有任何東西
    在看這個方向。它特別貴：`_run_schedule_entry` 會把排程項目的 payload 直接
    交給 `_gui.run_shell`，也就是主機上的任意指令執行。

    沒有豁免名單，因為想不出正當的例外：整群都是擁有者專屬時，改成群組規則的
    行為完全相同（`_is_owner_only_slash` 是 or），只是未來新增的子指令會**預設
    受閘**。哪天那一群真的要開一個公開子指令，正確的做法是明確把群組規則拿掉
    ——那是一個看得見的動作，而不是一次沉默的遺漏。
    """
    # 正面對照組要三邊都做：指令樹抽壞了、清單被清空、群集合被清空，三種情況
    # 底下那句斷言都會空轉通過（`members <= set()` 對非空的 members 恆為假）。
    assert len(SLASH_DECLS) >= 250, (
        "只從指令樹抽到 %d 個指令，抽取邏輯壞了" % len(SLASH_DECLS))
    assert len(b._OWNER_ONLY_SLASH) >= 10, (
        "`_OWNER_ONLY_SLASH` 只剩 %d 筆——清單被清空的話這一支永遠通過。"
        % len(b._OWNER_ONLY_SLASH))
    assert len(b._OWNER_ONLY_GROUPS) >= 5, "群組清單空了"

    should_upgrade = _fully_enumerated_groups(
        SLASH_DECLS, b._OWNER_ONLY_GROUPS, b._OWNER_ONLY_SLASH)
    assert not should_upgrade, (
        "這幾群的子指令**全部**列在 `_OWNER_ONLY_SLASH` 裡，卻沒有寫進 "
        "`_OWNER_ONLY_GROUPS`：%s。整群都要鎖就用群組規則——`CLAUDE.md` 明文"
        "禁止把群組規則換成列舉清單，因為列舉是 fail-open 的：往那一群多加一個"
        "子指令就**預設沒有閘**，而且沒有任何症狀。做法是把群名加進 "
        "`_OWNER_ONLY_GROUPS`，並把那幾筆從 `_OWNER_ONLY_SLASH` 刪掉。"
        % should_upgrade)


def test_the_upgrade_detector_would_see_a_fully_enumerated_group():
    """偵測器自己的正面對照組——**這一支才是有牙齒的那一支**。

    上面那支主測試在真實資料乾淨之後（`schedule` 已升級）就再也問不出問題了：
    把它的斷言整個刪掉不會有人紅。所以「這個偵測器還看得見東西嗎」必須拿合成
    資料另外問一次，跟 `test_the_stale_detector_would_see_a_rename` 同一個做法。
    """
    # 整群都被列舉 → 要升級。
    assert _fully_enumerated_groups(
        {"sch add", "sch run"}, frozenset(), frozenset({"sch add", "sch run"})
    ) == ["sch"]
    # 只列了一半 → 不是這一支要管的事（那是正常的「部分受閘」）。
    assert _fully_enumerated_groups(
        {"sch add", "sch run"}, frozenset(), frozenset({"sch add"})) == []
    # 已經是群組規則 → 不重複回報（那是 shadow 那一支的職責）。
    assert _fully_enumerated_groups(
        {"sch add", "sch run"}, frozenset({"sch"}),
        frozenset({"sch add", "sch run"})) == []
    # 只有一個子指令的群一樣要回報：那正是 fail-open 的形狀——第二個子指令加
    # 進來的時候沒有任何東西會提醒任何人。
    assert _fully_enumerated_groups(
        {"solo only"}, frozenset(), frozenset({"solo only"})) == ["solo"]
    # 頂層指令（沒有群）不得憑空長出一個群。`gen image` 在清單裡，但如果指令樹
    # 裡只有一個叫 `image` 的頂層指令，`image` 不是群，不該被當成「整群受閘」。
    assert _fully_enumerated_groups(
        {"image"}, frozenset(), frozenset({"image"})) == []
    # 清單為空時不得回報：`members <= set()` 對非空的 members 是假，但空群的
    # 情況要確認不會冒出幽靈條目。
    assert _fully_enumerated_groups(
        {"sch add"}, frozenset(), frozenset()) == []


# --------------------------------------------------------------------------
# `CLAUDE.md` 那句話裡的群名，也要跟程式對得起來
#
# 規則之書自己會過期，而且**沒有症狀**：閘照跑、測試全綠，只有那句話少了一個群。
# 2026-09-10 實際發生——`schedule` 當天升級成群組規則，`CLAUDE.md` 那個括號裡還是
# 九個名字。同一天同一個失效方式出現了三次（守門的 docstring 寫「九個群名」、這一
# 句、還有一段用條目編號當交叉引用的散文），所以這裡不再手動修第四次，改成對帳。
#
# 為什麼值得守：`CLAUDE.md` 是**唯一的規則來源**，冷啟動的 session 與 subagent 都
# 照它辦事。它少列一個群，下一個人就會以為那個群不受閘——而那正是本專案最貴的那類
# 錯誤（安靜的、看起來很正常的錯誤結論）。
# --------------------------------------------------------------------------
def _claude_md_group_names(text: str) -> list[str]:
    """`CLAUDE.md` 那句話括號裡列出的群名。

    抽成純函式是為了讓它有**自己的**合成對照：真實資料一修好就永遠對得上，那時
    把主測試裡的比較整個刪掉也不會有人紅。

    刻意要求 `_OWNER_ONLY_GROUPS` 後面**緊接著**括號（中間只准有空白／換行）。
    同一份檔案裡還有別的地方提到這個常數，例如「`_OWNER_ONLY_GROUPS` has the same
    shape (a renamed group)」——那也是「常數名 … 括號」，但中間隔著字，所以不會被
    誤抓。命中不是剛好一次就當場失敗，不要猜。
    """
    import re as _re

    hits = _re.findall(r"`_OWNER_ONLY_GROUPS`\s*\(([^)]*)\)", text)
    assert len(hits) == 1, (
        f"在 CLAUDE.md 裡找到 {len(hits)} 段「`_OWNER_ONLY_GROUPS` (…)」，"
        "必須剛好一段。那句話的形狀改了就要一起改這支擷取器——"
        "抓不到會讓下面的比較變成空集合，而空集合跟『完全一致』長得一模一樣。")
    return _re.findall(r"`([a-z_]+)`", hits[0])


def test_the_rule_of_record_lists_the_same_groups_as_the_code():
    """`CLAUDE.md` 括號裡的群名 ↔ `_OWNER_ONLY_GROUPS`，**兩個方向都要對**。"""
    repo_root = Path(b.__file__).resolve().parent.parent
    text = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    documented = set(_claude_md_group_names(text))
    actual = set(b._OWNER_ONLY_GROUPS)

    # 正面對照：擷取器回空集合的話，下面兩個差集都會是「全部」或「空」，而
    # 「空」會安靜地通過。先釘住真的抓到東西了。
    assert len(documented) >= 5, (
        f"從 CLAUDE.md 只抽到 {sorted(documented)}——擷取器壞了，下面等於沒在比。")

    missing = sorted(actual - documented)
    assert not missing, (
        f"`_OWNER_ONLY_GROUPS` 有 {missing}，但 CLAUDE.md 那句話沒列到。"
        "規則之書少列一個群，下一個人就會以為那一群不受閘——去把那個括號補上。")

    stale = sorted(documented - actual)
    assert not stale, (
        f"CLAUDE.md 列了 {stale}，但 `_OWNER_ONLY_GROUPS` 裡沒有。"
        "群改名或移除了？兩邊要一起改。")


def test_the_rule_of_record_detector_sees_both_directions():
    """合成對照——**牙齒長在這一支**。

    真實資料修好之後上面那支就問不出問題了（兩個差集永遠是空的），把它的斷言整個
    刪掉會存活。所以「這個偵測器還看得見東西嗎」必須拿假資料另外問一次，而且
    **兩個方向都要問**：只問一邊，正是這一整節在講的那個毛病。
    """
    nine = ("**fail-closed:** `_OWNER_ONLY_GROUPS`\n"
            "(`input`, `screen`, `win`, `clip`, `locate`, `macro`, `watch`,\n"
            "`proc`, `host`) covers every current AND future sub-command.")
    assert _claude_md_group_names(nine) == [
        "input", "screen", "win", "clip", "locate", "macro", "watch",
        "proc", "host"]
    # 方向一：程式有、文件沒有（2026-09-10 真正發生的那一種）。
    assert set(b._OWNER_ONLY_GROUPS) - set(_claude_md_group_names(nine)), (
        "這段九個群名的語料應該要少掉至少一個現有的群，否則它證明不了什麼")
    # 方向二：文件有、程式沒有（改名或移除）。
    renamed = nine.replace("`macro`", "`macros`")
    assert "macros" in set(_claude_md_group_names(renamed)) - set(
        b._OWNER_ONLY_GROUPS)
    # 別的地方提到同一個常數但後面不是緊接著括號，不得被誤抓。
    noise = (nine + "\n\n`_OWNER_ONLY_GROUPS` has the same shape "
             "(a renamed group) as the list above.")
    assert _claude_md_group_names(noise) == _claude_md_group_names(nine)


# --------------------------------------------------------------------------
# `/proc kill` 的系統關鍵行程名單
#
# 這份名單是**安全**清單：命中就拒絕，沒命中就真的送 terminate/kill。2026-09-09
# 之前它一支測試都沒有——`grep _KILL_DENY` 全 repo 只有兩處，宣告與那一行 `if`，
# 沒有任何測試提到它，也沒有任何測試提到裡面任何一個行程名。
#
# 而它當時有 **5/15 筆永遠比對不到**（詳見 `_kill_is_denied` 的 docstring）。
# 兩件事是同一件事：沒有人問過「這份名單真的擋得住嗎」。
# --------------------------------------------------------------------------
def test_every_kill_denylist_entry_is_actually_reachable():
    """名單裡的每一筆，都要真的能被 `_kill_is_denied` 命中。

    **這一支就是當初會抓到那個缺陷的那一支。** 沒有副檔名的那五筆
    （`system`／`registry`／`secure system`／`memory compression`／
    `system idle process`）在正規化補上 `.exe` 之後永遠對不上自己，於是名單裡
    最危險的那一半是裝飾品。一筆擋不住的安全清單比沒有清單更糟——它會讓下一個
    人以為那件事已經有人管了。
    """
    assert len(b._KILL_DENY) >= 10, (
        "名單只剩 %d 筆，下面那個迴圈會空轉通過" % len(b._KILL_DENY))
    unreachable = sorted(e for e in b._KILL_DENY if not b._kill_is_denied(e))
    assert not unreachable, (
        "這些項目寫在 `_KILL_DENY` 裡，但 `_kill_is_denied` 永遠命中不了：%s。"
        "名單裡有它，實際上不擋——而且沒有任何症狀。" % unreachable)


def test_the_denylist_still_covers_the_extensionless_windows_names():
    """回歸釘子：這五個名字沒有 `.exe`，正是踩過的那一坑。

    另外釘住大小寫與前後空白——使用者是從 `/proc list` 的輸出複製貼上的。
    """
    for name in ("system", "registry", "secure system",
                 "memory compression", "system idle process"):
        assert name in b._KILL_DENY, name
        assert b._kill_is_denied(name), name
        assert b._kill_is_denied("  " + name.upper() + "  "), name


def test_the_denylist_does_not_creep_into_ordinary_programs():
    """守住反面：拒絕的範圍不得蔓延，不然 `/proc kill` 就沒用了。"""
    for name in ("notepad", "notepad.exe", "chrome", "chrome.exe",
                 "python.exe", "code.exe", "systemsettings.exe"):
        assert not b._kill_is_denied(name), name


def test_an_empty_or_bare_extension_target_is_denied():
    """空字串補成 `.exe` 會比對到每一個 exe——所以它必須被擋。"""
    for bad in ("", "   ", ".exe", "  .EXE  "):
        assert b._kill_is_denied(bad), repr(bad)


def test_cmd_kill_refuses_a_critical_process_before_touching_psutil(monkeypatch):
    """行為面：拒絕要發生在**掃描行程之前**。

    **驗證過會紅**：把判斷改成永遠回 False（等於那個分支不存在），這一支立刻失敗。
    用假 psutil 而不是斷言「沒有呼叫」，好處是就算判斷真的壞了，這支測試也
    **不可能**真的去終止主機上的行程。

    但要知道它實際怎麼紅的：`cmd_kill` 有一個很寬的 `except Exception` 包住掃描
    迴圈，所以假 psutil 丟的 `AssertionError` **會被吞掉**，變成一句泛用的
    「行程掃描失敗」。也就是說**這一支是靠回覆文字判定的**，不是靠例外冒出來。
    放在 `except` 後面的爆炸性替身不會爆炸——它會變成錯誤路徑。所以斷言要盯著
    「回覆說了拒絕」，不能只盯著「有沒有丟例外」。
    """
    import asyncio as _asyncio
    import sys as _sys

    class _ExplodingPsutil:
        @staticmethod
        def process_iter(*_a, **_k):
            raise AssertionError(
                "cmd_kill 在拒絕系統關鍵行程之前就去掃描行程了")

    replies = []

    async def _recorder(_message, text=None, **_kw):
        replies.append(text)

    monkeypatch.setitem(_sys.modules, "psutil", _ExplodingPsutil)
    monkeypatch.setattr(b, "safe_reply", _recorder)

    for name in sorted(b._KILL_DENY):
        replies.clear()
        _asyncio.run(b.cmd_kill(object(), name))
        assert replies and "拒絕關閉" in (replies[0] or ""), (name, replies)


# --------------------------------------------------------------------------
# `/proc kill` 的第二層：**類別**保證
#
# 上面那幾支量的是 `_KILL_DENY` 這份**列舉**。下面這幾支量的是另一層：比對用的
# 字串一定帶 `.exe`，所以名字沒有副檔名的行程在結構上碰不到——而 Windows 核心
# 偽行程正好是那一類。兩層要分開量，否則其中一層失效時另一層會把它遮起來
# （這個 repo 已經在別處踩過好幾次「兩道防護互相遮蔽」）。
# --------------------------------------------------------------------------
class _KillFakeProc:
    """只夠 `cmd_kill` 用的假行程：記下 terminate/kill，**絕不真的動手**。"""

    def __init__(self, pid, name):
        self.info = {"pid": pid, "name": name}
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def is_running(self):
        return False

    def kill(self):                                  # pragma: no cover - 保險
        self.killed = True


class _KillFakePsutil:
    """假 psutil。**故意不繼承、不轉呼叫真的那一個**——這台機器上跑著長命的正式
    行程，測試裡任何一條通往真 `psutil.Process.terminate` 的路都不可接受。"""

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def __init__(self, procs):
        self._procs = procs

    def process_iter(self, attrs=None):
        return list(self._procs)

    def Process(self, pid):                          # pragma: no cover - 保險
        raise self.NoSuchProcess(pid)


def _run_cmd_kill(monkeypatch, payload, procs):
    """跑一次 `cmd_kill`，回傳 (回覆文字們, 假行程們)。"""
    import asyncio as _asyncio
    import sys as _sys

    replies = []

    async def _recorder(_message, text=None, **_kw):
        replies.append(text)

    monkeypatch.setitem(_sys.modules, "psutil", _KillFakePsutil(procs))
    monkeypatch.setattr(b, "safe_reply", _recorder)
    _asyncio.run(b.cmd_kill(object(), payload))
    return replies, procs


def test_kill_can_never_reach_a_process_whose_name_has_no_extension(monkeypatch):
    """類別保證：`/proc kill` 碰不到任何名字沒有副檔名的行程。

    `_normalise_kill_target` 保證比對用的字串一定以 `.exe` 結尾，而 Windows 的
    核心偽行程（`system`／`registry`／`secure system`／`memcompression`／
    `system idle process`）名字全都沒有副檔名——所以擋住那一整個**類別**的是機制，
    不是 `_KILL_DENY` 那份列舉。

    **假名字刻意不在 `_KILL_DENY` 裡**（斷言第一行就釘住這件事）。用名單裡的名字
    會同時踩到兩層防護，於是拆掉任何一層都還是綠的——這個 repo 已經在別處踩過那個
    形狀好幾次。這一支只量機制那一層。

    **這支紅掉代表**：有人把掃描迴圈放寬成也比對使用者原本打的字。在做那件事之前
    先確認 `_KILL_DENY` 對每一種 Windows 組態都寫全了——2026-09-09 實測 15 筆裡就
    有一筆名字是錯的（列了顯示名 `memory compression`，實際映像名是
    `memcompression`），所以那份列舉在當時並不可靠。
    """
    fake_name = "notarealsystemthing"
    assert fake_name not in b._KILL_DENY, (
        "這支測試要量的是**機制**那一層，所以假名字必須不在名單裡；"
        "在名單裡的話兩層會互相遮蔽，拆掉任何一層都還是綠的")
    assert not b._kill_is_denied(fake_name), fake_name

    procs = [_KillFakeProc(4321, fake_name), _KillFakeProc(9, "")]
    replies, procs = _run_cmd_kill(monkeypatch, fake_name, procs)

    assert not any(p.terminated or p.killed for p in procs), (
        "`/proc kill %s` 選中了一個沒有副檔名的行程。比對用的字串應該永遠帶 "
        "`.exe`——這一層一旦放寬，擋住 Windows 核心偽行程的就只剩 `_KILL_DENY` "
        "那份列舉了。" % fake_name)
    assert replies, "什麼都沒回"


def test_kill_says_something_true_when_the_name_has_no_executable_name(monkeypatch):
    """訊息面：`/proc list` 印得出來的名字，`/proc kill` 不能回「沒在跑」。

    `/proc list` 直接列行程名，裡面就有 `system`、`registry` 這些沒有副檔名的。
    使用者從清單複製一個貼進 `/proc kill`，舊版會補成 `xxx.exe`、比對不到，回一句
    「目前沒有 `xxx.exe` 在跑」——**而他明明剛從清單上看到它**。那是一句會讓人以為
    指令壞掉的話，而且它指名了一個系統上根本不存在的檔案。
    """
    fake_name = "notarealsystemthing"
    procs = [_KillFakeProc(4321, fake_name)]
    replies, _ = _run_cmd_kill(monkeypatch, fake_name, procs)

    assert len(replies) == 1, replies
    text = replies[0] or ""
    assert fake_name + ".exe" not in text, (
        "回覆指名了一個不存在的檔案 `%s.exe`：%r" % (fake_name, text))
    assert "系統層" in text and "無法從這裡關閉" in text, text


def test_kill_still_reports_a_genuinely_absent_program_plainly(monkeypatch):
    """反面：真的沒在跑的時候，還是要講「沒在跑」。

    少了這一支，把上面那句話改成**無條件**送出也會全綠——然後每一次打錯程式名都
    會被告知「那是系統層的行程」。正反兩面要各一支，理由同上：一個輸入同時踩到兩
    條路的話，拆掉任一條都不會紅。
    """
    procs = [_KillFakeProc(4321, "somethingelse.exe"), _KillFakeProc(9, "")]
    replies, procs = _run_cmd_kill(monkeypatch, "notepad", procs)

    assert not any(p.terminated or p.killed for p in procs), "殺錯行程了"
    assert len(replies) == 1, replies
    text = replies[0] or ""
    assert "目前沒有 `notepad.exe` 在跑" == text, text


def test_kill_still_terminates_an_ordinary_program(monkeypatch):
    """再一個反面：擋歸擋，一般程式還是要真的關得掉。

    只有前面那些「不准殺」的測試時，把 `cmd_kill` 整支改成永遠不殺也是全綠的。
    """
    victim = _KillFakeProc(4321, "notepad.exe")
    procs = [victim, _KillFakeProc(9, ""), _KillFakeProc(7, "system")]
    replies, _ = _run_cmd_kill(monkeypatch, "notepad", procs)

    assert victim.terminated, "一般程式應該要被關掉，不然這個指令就沒用了"
    assert any("已關閉" in (t or "") for t in replies), replies


def test_kill_never_terminates_the_bot_itself_even_when_the_name_matches(monkeypatch):
    """`pid == own_pid` 那一行的成立邊在整個套件裡從來沒有跑過（2026-09-22 分支覆蓋率）。

    `test_process_scan_self_skip` 只**靜態**確認那一行在；這一支確認它真的擋得住。名字
    刻意用一個不在 `_KILL_DENY` 裡的——bot 自己的映像名是 `python.exe`，而 `/proc kill
    python` 是合法的指令，擋住 bot 自殺的就只剩 pid 這一層。同名的另一個行程是對照：
    把整支改成「什麼都不殺」不能也是綠的。"""
    assert not b._kill_is_denied("notepad.exe"), "名字進了名單，兩層會互相遮蔽"
    me = _KillFakeProc(os.getpid(), "notepad.exe")
    other = _KillFakeProc(4321, "notepad.exe")
    replies, _ = _run_cmd_kill(monkeypatch, "notepad", [me, other])

    assert not (me.terminated or me.killed), "`/proc kill` 把 bot 自己也關了"
    assert other.terminated, "對照：同名的另一個行程照樣要關"
    assert any("已關閉 **1**" in (t or "") for t in replies), replies


def test_the_denylist_names_the_real_image_name_not_only_the_display_name():
    """名單裡要有 Windows **實際回報**的映像名，不能只有工作管理員的顯示名。

    2026-09-09 實測：工作管理員顯示「Memory Compression」，而 psutil 拿到的
    `name()` 是 `memcompression`。名單當時只寫了顯示名，所以那一筆從來沒有對應到
    任何真的行程——第三種失效，跟「正規化讓它比對不到」是不同的病。

    兩種寫法都要留：映像名是給「這一筆真的擋得到東西嗎」用的，顯示名是給「使用者
    照著工作管理員打得出來」用的。這裡釘的是前者。
    """
    for image_name in ("memcompression", "system", "registry",
                       "system idle process", "secure system"):
        assert image_name in b._KILL_DENY, (
            "`%s` 是 Windows 實際回報的行程名，名單裡沒有它" % image_name)
    # `lsaiso.exe`（Credential Guard 隔離出來的 LSA）與 `lsass.exe` 同級關鍵，
    # 名單原本只有後者。
    assert "lsaiso.exe" in b._KILL_DENY


def test_pipeline_commands_are_not_locked():
    """鎖定範圍不得蔓延——佇列編輯、批次控制與公開工具仍該給其他人用。"""
    for open_command in ("todo prompt add", "todo char1 list", "preset info",
                         "run", "stop", "status", "queue", "eta", "latest",
                         "gen plan", "gen preview", "out rate", "fav list",
                         "sys health", "sys doctor", "sys disk",
                         "log tail", "config show", "dorossi ask", "web wiki"):
        assert not b._is_owner_only_slash(open_command), open_command
        assert open_command in SLASH_DECLS, "%s 不存在了？" % open_command


def test_every_locked_slash_command_has_all_its_bang_aliases_locked():
    """三個表面不得漏接：漏一個 `!` 別名就是一條完整的繞道。

    別名是 `head in ("!config_set", "!cfg_set")` 這種派發器寫法，宣告上看不到，
    所以這裡直接讀派發鏈。**驗證過會紅**：從 `_OWNER_ONLY_BANGS` 拿掉
    `!cfg_set`，這一筆指名失敗。
    """
    import ast
    tree, _text = _bot_ast()
    locked_bangs = {SLASH_DECLS[q]["bang"] for q in LOCKED_SLASH
                    if SLASH_DECLS[q].get("bang")}
    assert locked_bangs, "抽不到任何被鎖指令的 bang，抽取邏輯要跟著改"

    dispatcher = next(n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == "on_message")
    with_aliases = set()
    for node in ast.walk(dispatcher):
        if not isinstance(node, ast.Compare) \
                or not isinstance(node.left, ast.Name) \
                or node.left.id != "head" or not node.comparators:
            continue
        target = node.comparators[0]
        if isinstance(target, (ast.Tuple, ast.List)):
            names = [e.value for e in target.elts
                     if isinstance(e, ast.Constant)]
        elif isinstance(target, ast.Constant):
            names = [target.value]
        else:
            names = []
        if names and names[0] in locked_bangs:
            with_aliases |= set(names)

    missing = sorted(with_aliases - set(b._OWNER_ONLY_BANGS))
    assert not missing, (
        "這些 `!` 指令（含別名）對應到被鎖的斜線指令，卻不在 "
        "`_OWNER_ONLY_BANGS` 裡，等於留了一條繞過主機控制閘的路：%s" % missing)


def test_no_stale_entries_in_the_bang_lock_set():
    """反向：集合裡列的必須真的是派發器認得的指令，否則是拼錯字的空閘。"""
    import ast
    tree, _text = _bot_ast()
    dispatcher = next(n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == "on_message")
    known = set()
    for node in ast.walk(dispatcher):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) \
                and node.left.id == "head" and node.comparators:
            target = node.comparators[0]
            if isinstance(target, (ast.Tuple, ast.List)):
                known |= {e.value for e in target.elts
                          if isinstance(e, ast.Constant)}
            elif isinstance(target, ast.Constant):
                known.add(target.value)
    stale = sorted(set(b._OWNER_ONLY_BANGS) - known)
    assert not stale, "這些不是派發器認得的 `!` 指令（拼錯或已移除）：%s" % stale


def test_mention_surface_gates_the_host_commands_it_exposes():
    """⚠️ `_handle_mention` 跑在 `on_message` 的**頻道閘之前**、原本完全沒有閘，
    而 `mcmd_restart` 內部也沒有擁有者檢查——所以 `@bot restart` 曾經是
    「bot 看得到的任何伺服器、任何人」都叫得動的。"""
    import ast
    tree, _text = _bot_ast()
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "_handle_mention")
    assert handler is not None
    exposed = set(_dict_dispatch_map(tree))
    for name in ("generate", "genqueue", "restart"):
        assert name in exposed, "%s 不再由 mention 暴露？請更新這一筆" % name
        assert name in b._OWNER_ONLY_MENTIONS, name
    stale = sorted(set(b._OWNER_ONLY_MENTIONS) - exposed)
    assert not stale, "mention 閘列了不存在的子指令：%s" % stale


def test_the_mention_dispatch_map_was_actually_read():
    """正面對照：**抽取器有沒有讀到那個派發器**——這一問不依賴任何東西碰得到桌面。

    這是 mention 那一格唯一能拿正式資料問的問題。「有幾條路徑通到桌面控制」不能
    拿來當下限，因為今天正確的答案就是 **0**（實測：`_handle_mention` 摸得到的
    52 個名字，跟「摸得到 `_gui.*` 的 142 支函式」交集是空的），而一個對正確狀態
    亮紅燈的下限會被下一個人調成 0，那一格就變裝飾品。

    所以拆成兩問：這一支問「讀到了嗎」（正式資料），
    `test_the_gui_reachability_extractor_sees_a_three_hop_chain` 的合成語料問
    「讀到之後歸得了類嗎」。
    """
    tree, _text = _bot_ast()
    mapping = _dict_dispatch_map(tree)
    assert len(mapping) >= 20, (
        f"`_handle_mention` 的派發表只抽到 {len(mapping)} 個子指令"
        f"（實測 2026-09-11 是 22 個）：{sorted(mapping)}。派發改寫法了？"
        "抽不到的話 mention 那一格會回到「零路徑」，而那跟「這個表面沒被讀過」"
        "在輸出上一模一樣。")
    assert all(mapping.values()), (
        "有子指令對不到任何被呼叫的函式：%s。值的形狀變了（本來全是 lambda），"
        "只抽鍵不抽值的話，歸類那一半等於沒做。"
        % sorted(k for k, v in mapping.items() if not v))
    # 反向對帳：擁有者閘列的每一個子指令都必須真的還在派發表裡。列舉是 fail-open
    # 的——改名之後閘裡那個字串永遠不再命中，而閘還在跑、測試還是綠的。
    missing = sorted(set(b._OWNER_ONLY_MENTIONS) - set(mapping))
    assert not missing, (
        f"`_OWNER_ONLY_MENTIONS` 裡的 {missing} 在 `_handle_mention` 的派發表裡"
        "找不到——那幾個子指令改名或搬走了，閘就這樣安靜地失去作用。")


def test_both_text_gates_sit_before_the_role_gate():
    """順序是規則的一部分：角色閘預設停用，排在它後面等於沒有閘。"""
    import ast
    tree, _text = _bot_ast()

    def first_statement_using(func, name):
        """回傳「用到 `name` 的第一個陳述式」在函式 body 裡的序位。

        **不能拿原始碼字串 index 比**——註解與 docstring 裡也會出現這些名字
        （這道閘的註解本身就在解釋角色閘），比到的會是註解而不是程式碼。
        """
        for i, stmt in enumerate(func.body):
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and node.id == name:
                    return i
                if isinstance(node, ast.Attribute) and node.attr == name:
                    return i
        return None

    for func_name, gate, role_gate in (
            ("_tree_check", "_is_owner_only_slash", "_roles_configured"),
            ("on_message", "_OWNER_ONLY_BANGS", "_check_command_permission")):
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == func_name)
        at_gate = first_statement_using(func, gate)
        at_role = first_statement_using(func, role_gate)
        assert at_gate is not None, "%s 少了主機控制閘" % func_name
        assert at_role is not None, "%s 找不到角色閘，比對邏輯要跟著改" % func_name
        assert at_gate < at_role, (
            "%s 的主機控制閘排在角色閘之後——角色系統預設停用，那等於沒有保護"
            % func_name)


# ---------------------------------------------------------------------------
# 擁有者無限制揭露（owner ruling, 2026-08-27）
# ---------------------------------------------------------------------------
def _asker(uid):
    return types.SimpleNamespace(author=types.SimpleNamespace(id=uid))


def test_owner_error_gives_the_owner_the_real_exception():
    """擁有者拿到例外**類別 ＋ 訊息**，其他人拿到泛用句。

    類別名不能省：`str(OSError(2, ...))` 只有 `[Errno 2] …`，少了類別就分不出是
    哪一種失敗，而這整條裁定的目的就是讓擁有者 debug 得動。
    """
    error = OSError(2, "No such file or directory")
    generic = "讀取 log 失敗，請查看主機 log。"

    detailed = b._owner_error(_asker(b.OWNER_USER_ID), error, generic)
    assert "FileNotFoundError" in detailed
    assert "No such file or directory" in detailed
    assert detailed != generic

    assert b._owner_error(_asker(b.OWNER_USER_ID + 1), error, generic) == generic


def test_owner_error_is_fail_closed_and_never_raises():
    """取不到提問者 → 泛用句；`raw` 求值爆炸 → 泛用句，不得把 handler 一起炸掉。"""
    generic = "泛用句"
    assert b._owner_error(types.SimpleNamespace(), OSError("x"), generic) == generic
    assert b._owner_error(None, OSError("x"), generic) == generic

    class Boom(Exception):
        def __str__(self):
            raise RuntimeError("boom")

    assert b._owner_error(_asker(b.OWNER_USER_ID), Boom(), generic) == generic


def _lambdas_closing_over_an_except_name(source: str) -> list[tuple[int, str]]:
    """回 `(行號, 例外變數名)`：在 `except ... as X` 區塊裡建立、而且**函式本體**
    自由引用 `X` 的 lambda。純函式，好讓合成資料問得到它。

    ⚠️ **只走 `Lambda.body`，不走整個節點。** 預設引數（`lambda err=error: …`）
    住在 `Lambda.args.defaults`，它是在**外層**求值的——那正是這個坑的修法，把它
    一起算成違規的話，守門會紅在唯一正確的寫法上。lambda 自己的參數名也要當成
    已綁定。
    """
    import ast as _ast

    out = []
    for handler in _ast.walk(_ast.parse(source)):
        if not isinstance(handler, _ast.ExceptHandler) or not handler.name:
            continue
        for node in _ast.walk(handler):
            if not isinstance(node, _ast.Lambda):
                continue
            bound = {a.arg for a in (
                node.args.posonlyargs + node.args.args + node.args.kwonlyargs)}
            if node.args.vararg:
                bound.add(node.args.vararg.arg)
            if node.args.kwarg:
                bound.add(node.args.kwarg.arg)
            free = {n.id for n in _ast.walk(node.body)
                    if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load)}
            if handler.name in free - bound:
                out.append((node.lineno, handler.name))
    return sorted(set(out))


def test_no_lambda_closes_over_an_except_bound_name():
    """`except ... as e` 綁的名字在區塊結束時會被隱式 `del`，所以任何**延後求值**
    的閉包抓著它都是一顆定時炸彈。

    這不是理論。2026-09-10 `_watch_loop` 就有一個
    `lambda: f"…{error!r}"` 交給 `_owner_detail`。今天能動，因為
    `_owner_detail` 是同步求值的、呼叫點還在 except 區塊裡；本機 CPython 3.14.4
    實測：同一個 lambda 帶出區塊之後再呼叫就是
    `NameError: cannot access free variable 'error'`。

    **失效方向是最惡劣的那種：完全無聲。** `_owner_detail` 自己有
    `except Exception: return generic`，所以 `NameError` 會被吃掉，擁有者拿到的
    是泛用句——那個決策點存在的唯一目的（讓擁有者看到原始例外）被反過來，而且
    沒有任何錯誤訊息。

    修法是預設引數（`lambda err=error:`），在**定義時**綁值。另外 29 個 except
    區塊走的是 `_owner_error(source, error, generic)`，它把 error 當引數傳，
    結構上不會有這個坑——`_owner_error` 的 docstring 早就寫了「每處各寫一次
    只是噪音，而噪音正是有人漏掉一處的原因」，這支就是把那句話變成守門。
    """
    offenders = []
    for path in _join_scan_sources():
        for lineno, name in _lambdas_closing_over_an_except_name(
                path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}:{lineno}（as {name}）")
    assert not offenders, (
        f"{offenders}：lambda 在 except 區塊裡自由引用了例外變數。Python 在區塊"
        "結束時會 `del` 那個名字，所以只要求值被延後一步就是 `NameError`，而它"
        "多半會被某個 `except Exception` 吃掉、變成一則泛用訊息。改成預設引數"
        "（`lambda err=error:`）在定義時綁值，或直接走 `_owner_error`。")


def test_the_except_closure_detector_actually_bites():
    """正面對照組 ＋ 近似反例。上面那支斷言的是空集合，它自己分不出「沒有違規」
    與「抽取器壞了」。而近似反例這裡格外重要：**唯一正確的修法長得跟違規幾乎
    一樣**，判錯的話守門會紅在對的寫法上。
    """
    bad = ("try:\n    pass\n"
           "except Exception as error:\n"
           "    f(lambda: str(error))\n")
    assert _lambdas_closing_over_an_except_name(bad) == [(4, "error")], (
        "連最直接的形狀都沒抓到")

    for benign, why in (
            ("try:\n    pass\nexcept Exception as error:\n"
             "    f(lambda err=error: str(err))\n",
             "預設引數在定義時綁值——這是**修法**，不是違規"),
            ("try:\n    pass\nexcept Exception as error:\n"
             "    f(lambda error: str(error))\n",
             "同名的 lambda 參數把外層那個遮住了，本體引用的是參數"),
            ("try:\n    pass\nexcept Exception:\n"
             "    f(lambda: str(other))\n",
             "`except` 沒有 `as`，沒有會被 del 的名字"),
            ("try:\n    pass\nexcept Exception as error:\n"
             "    f(str(error))\n",
             "不是 lambda，就地求值沒有問題"),
            ("try:\n    pass\nexcept Exception as error:\n"
             "    g(error, lambda: 1)\n",
             "lambda 沒有引用那個名字"),
    ):
        assert _lambdas_closing_over_an_except_name(benign) == [], why


def test_list_label_reveals_the_real_filename_only_to_the_owner():
    label = b._list_label(b.TODO_PROMPT_FILE, _asker(b.OWNER_USER_ID))
    assert label == b.TODO_PROMPT_FILE.name

    for source in (_asker(b.OWNER_USER_ID + 1), None):
        generic = b._list_label(b.TODO_PROMPT_FILE, source)
        assert generic != b.TODO_PROMPT_FILE.name
        assert ".md" not in generic


def test_every_bound_exception_reply_offers_the_owner_the_detail():
    """`except … as error:` 底下的泛用回覆一律要經過 `_owner_error()`。

    綁了例外變數就代表細節在手上；直接送一個字串常數等於把它丟掉，擁有者永遠
    看不到真正的原因——那正是這條裁定要解決的問題。**沒有**綁變數的
    `except ValueError:`（用法提示那種）不在守備範圍，因為那裡本來就沒有細節。

    新增送出點時若真的不該給細節，把它列進 `allowed` 並寫理由。
    """
    import ast

    allowed: set[tuple[str, str]] = set()   # (函式名, 訊息開頭) — 目前沒有例外

    source = Path(b.__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"), str(source))

    enclosing = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                enclosing[id(node)] = func.name

    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    bare = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            if dotted(node.func).rsplit(".", 1)[-1] not in (
                    "reply", "send", "send_message", "safe_reply"):
                continue
            for arg in node.args:
                if not (isinstance(arg, ast.Constant)
                        and isinstance(arg.value, str)):
                    continue
                where = (enclosing.get(id(node), "<module>"), arg.value[:24])
                if where not in allowed:
                    bare.append("L%d %s: %r" % (node.lineno, where[0], where[1]))

    assert not bare, (
        "這些送出點在 `except … as error:` 裡直接送泛用字串，擁有者拿不到細節：\n  "
        + "\n  ".join(bare)
        + "\n改法：包成 `_owner_error(message, error, \"原本的泛用句\")`。")


# 平台對**每一個頂層指令**的合計上限：name ＋ description ＋ 所有 option 的
# name/description ＋ 所有 choice 的 name/value，全部加起來不得超過 8000 字元
# （官方文件 2026-08-27 查證）。超過的後果與描述超長一樣是 `tree.sync()` **整批**
# 被拒，不是只拒那一個群。
SLASH_COMMAND_CHARACTER_BUDGET = 8000


def _slash_command_weight(command) -> int:
    total = (len(getattr(command, "name", "") or "")
             + len(getattr(command, "description", "") or ""))
    for child in getattr(command, "commands", None) or []:
        total += _slash_command_weight(child)
    for option in getattr(command, "parameters", None) or []:
        total += len(option.name or "") + len(option.description or "")
        for choice in (option.choices or []):
            total += len(str(choice.name)) + len(str(choice.value))
    return total


def test_no_top_level_command_exceeds_the_character_budget():
    """每個頂層指令的 name/description/choice 合計 ≤ 8000 字元。

    `test_docs_sync` 那邊已經逐項驗長度上限，但**合計**這條它量不準：它走 AST，
    看不到 `choices=_CONFIG_KEY_CHOICES` 這種在執行期才長出來的選項表。實測
    `/config` 靜態算是 316、接上真的 choices 後是 1016——差三倍。所以這一條放在
    這裡，用真正建好的 `tree`。

    現況離上限還很遠（最大是 `/todo` 的 1198），這條是為了「哪天有人往大群裡塞
    一張長 choice 表」而存在——那種改動看起來很無害，失敗卻是全部斜線指令一起
    消失，而且要到下一次 sync 才發現。
    """
    weights = sorted(((_slash_command_weight(command), command.name)
                      for command in b.tree.get_commands()), reverse=True)
    assert weights, "指令樹是空的，抽取邏輯要跟著改"
    over = [(name, weight) for weight, name in weights
            if weight > SLASH_COMMAND_CHARACTER_BUDGET]
    assert not over, (
        "這些頂層指令超過平台的 %d 字元合計上限，`tree.sync()` 會**整批**被拒："
        % SLASH_COMMAND_CHARACTER_BUDGET
        + "".join("\n  /%s：%d" % (name, weight) for name, weight in over)
        + "\n改法：縮短該群的說明文字，或把長 choice 表改成自動補全（autocomplete "
          "不計入這個預算）。")


# ---------------------------------------------------------------------------
# mention 基準線：回聲出去的使用者輸入不得變成 ping 放大器
# ---------------------------------------------------------------------------
# `discord.Client` 不設 `allowed_mentions` 時，函式庫**整個欄位都不放進送出的
# payload**，平台端就套用舊行為：從內容用正規表示式解析 `@everyone` / `@here` /
# 身分組 / 使用者 mention 並真的發通知。這個 bot 有大量把使用者輸入回聲出去的
# 送出點，其中 `/fun calc` 還是跨頻道的公開指令——`'<@123>'` 是合法的
# simpleeval 運算式，算出來就是 `<@123>`，於是 bot 幫任何人 ping 任何人。
#
# 修法是在 client 層設基準線而不是逐一在送出點補：逐一補等於「漏一個就破功」，
# 而且新增送出點的人不會知道有這條規則。下面三支測試分別釘住
# 「基準線存在」「基準線真的關掉解析」「要 ping 的地方有明確 opt-in」。
def _client_call_node():
    """AST 取出 `discord.Client(...)` 那個呼叫節點。"""
    import ast
    source = Path(b.__file__).read_text(encoding="utf-8")
    found = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "Client"]
    assert len(found) == 1, f"預期只有一個 discord.Client(...)，找到 {len(found)}"
    return found[0]


def test_the_client_is_constructed_with_a_mention_baseline():
    """漏掉這個關鍵字，整段防護就等於不存在——而且從程式碼上看不出來。"""
    import ast
    node = _client_call_node()
    keywords = {kw.arg for kw in node.keywords}
    assert "allowed_mentions" in keywords, (
        "`discord.Client(...)` 沒有帶 `allowed_mentions=`。不帶的話函式庫不會把"
        "這個欄位放進 payload，平台就會解析內容裡的 `@everyone` / `@here` / "
        "`<@id>` 並真的發通知——而這個 bot 到處在回聲使用者輸入。")
    value = next(ast.unparse(kw.value) for kw in node.keywords
                 if kw.arg == "allowed_mentions")
    assert value == "DEFAULT_MENTIONS", (
        f"基準線應該走單一來源的 `DEFAULT_MENTIONS`，實際是 `{value}`")


def test_the_baseline_really_stops_the_platform_parsing_mentions():
    """行為驗證，而且是在**真正送出去的 payload** 那一層。

    不是檢查我們建了什麼物件，是把內容交給 discord.py 自己的
    `handle_message_parameters`（`Messageable.send` 用的就是它）產出 payload，
    再看 `allowed_mentions` 欄位長什麼樣。`parse: []` = 叫平台不要從內容解析任何
    mention。
    """
    import discord
    import discord.http

    def payload(content, per_call=None):
        params = discord.http.handle_message_parameters(
            content,
            allowed_mentions=(discord.utils.MISSING if per_call is None
                              else per_call),
            previous_allowed_mentions=b.client._connection.allowed_mentions)
        return params.payload.get("allowed_mentions")

    evil = "= **@everyone @here <@123456789> <@&987654321>**"

    # 1) 一般送出點（沒有明講 allowed_mentions）：內容裡什麼都不解析。
    assert payload(evil) == {"replied_user": True, "parse": []}, (
        "基準線沒生效——回聲使用者輸入的送出點還是能 ping 人")

    # 2) 警報送出點明確 opt-in，但範圍**只到警報對象那一個人**。
    #    先前這裡是 `users=True`（＝內容裡任何使用者 mention 都放行），而警報訊息
    #    裡同時有佇列來的角色名字，等於把基準線擋掉的 ping 放大器在警報這條路上
    #    重新開了一半。
    monkey_id = 42
    saved_alert_id = b.ALERT_USER_ID
    try:
        b.ALERT_USER_ID = monkey_id
        scoped_alert = payload("<@42> 磁碟不足 <@999> 也想被 ping",
                               b._alert_mentions())
    finally:
        b.ALERT_USER_ID = saved_alert_id
    assert scoped_alert == {"users": [42], "replied_user": True, "parse": []}, (
        f"警報的 mention 範圍沒有縮到警報對象，實際 payload：{scoped_alert}")

    # 3) 範圍限縮：訊息裡同時有 bot 要 ping 的人與使用者自己夾帶的 mention 時，
    #    只有前者會被 ping。
    class _User:
        id = 555

    scoped = payload("⏰ <@555> timer: <@999> 也想被 ping", b._mentions_for(_User))
    assert scoped == {"users": [555], "replied_user": True, "parse": []}, (
        f"範圍限縮失效，實際 payload：{scoped}")
    assert b._mentions_for(None).to_dict() == {"parse": []}, (
        "拿不到對象時要 fail-closed（全關），不是退回 `users=True`")

    # 4) 既有那些明講 `AllowedMentions.none()` 的送出點不受影響。
    assert payload(evil, discord.AllowedMentions.none()) == {"parse": []}


# ---------------------------------------------------------------------------
# safe_reply —— 觸發訊息被刪掉時的重送路徑
#
# 這支包裝被呼叫 **409 次**，是本模組最常用的送出點，而它整條 fallback 路徑原本
# 一支行為測試都沒有。它存在的理由本身就是「只有在別的東西已經出錯時才會跑」的那
# 一類：使用者在 bot 回答之前把訊息刪掉，Discord 以 `50035 … message_reference:
# Unknown message` 打回，於是改用 `channel.send` 重送。
#
# 2026-09-06 發現那條重送路徑**送不出附件**，而且兩種壞法都是靜默的：
#
#   * `discord.File(str(path))`（`_owner=True`）→ `MultipartParameters.__exit__`
#     不論成敗都 `close()`，第二次送出讀它會 `ValueError: read of closed file`，
#     而那個例外會被 `safe_reply` 最外層的 except 吞掉 → **使用者連文字都收不到**。
#   * `discord.File(io.BytesIO(...))`（`_owner=False`）→ fp 沒被關但停在 EOF，
#     而 `HTTPClient.request` 只在重試迴圈裡 `reset(seek=tries)`、新 request 的
#     `tries` 從 0 開始（falsy）→ 不 seek → **上傳 0 位元組的附件**，看起來成功。
#
# 兩種變體在正式程式碼裡都有呼叫點（截圖／錄影／剪貼簿／取檔是路徑型，長輸出改附檔
# 與工作階段匯出是緩衝區型），所以這不是理論問題。
#
# 這裡刻意**走 discord.py 真正的 `handle_message_parameters`** 來驗附件內容，不是
# 只檢查「有沒有把 file 這個 kwarg 傳下去」——後者正是這個 bug 躲過所有既有檢查的
# 方式：kwarg 一直都在，壞掉的是那個物件的狀態。
# ---------------------------------------------------------------------------

class _ResendChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return "sent-via-channel"


class _ResendMessage:
    """`message.reply` 可以被指定成成功、或丟出某個例外。"""

    def __init__(self, *, reply_error=None, channel=None):
        self._reply_error = reply_error
        self.channel = _ResendChannel() if channel is None else channel
        self.replies = []
        self.author = types.SimpleNamespace(id=1234)

    async def reply(self, content=None, **kwargs):
        self.replies.append((content, kwargs))
        if self._reply_error is not None:
            raise self._reply_error
        return "sent-via-reply"


def _unknown_reference_error():
    """討論中的那個 50035：`message_reference` 指向一則已經不存在的訊息。"""
    err = discord.HTTPException.__new__(discord.HTTPException)
    err.args = ("400 Bad Request (error code: 50035): Invalid Form Body\n"
                "In message_reference: Unknown message",)
    return err


def _bytes_actually_uploaded(file_obj) -> bytes:
    """把 File 交給 discord.py 自己的 multipart 組裝流程，讀出真正會上傳的位元組。

    順帶把 `__exit__` 的 `close()` 也跑到——那正是讓第二次送出壞掉的那一步。
    """
    import discord.http
    with discord.http.handle_message_parameters(
            content="x", file=file_obj) as params:
        return params.multipart[1]["value"].read()


def test_a_real_message_is_replied_to_with_a_reference_that_may_vanish():
    """真的 `discord.Message`：一次 `channel.send`，引用帶 `fail_if_not_exists=False`。

    觸發訊息已經被刪掉時，平台會照這個欄位改成一般訊息送出——不必失敗一次、不必重建
    附件，也不必靠錯誤文字判斷。送出去的 payload 用 discord.py 自己的組裝流程確認，
    `fail_if_not_exists` 真的在裡面（它是 `None` 時根本不會被序列化）。"""
    import discord.http

    sent = []

    class _Chan:
        id = 4242

        async def send(self, content=None, **kwargs):
            sent.append((content, kwargs))
            return "sent-via-channel"

    msg = discord.Message.__new__(discord.Message)
    msg.id, msg.channel, msg.guild, msg._state = 555, _Chan(), None, None
    got = asyncio.run(b.safe_reply(msg, "hello", mention_author=True))

    assert got == "sent-via-channel"
    assert len(sent) == 1, sent
    content, kwargs = sent[0]
    assert content == "hello" and "mention_author" not in kwargs, kwargs
    reference = kwargs["reference"]
    assert isinstance(reference, discord.MessageReference), reference
    assert (reference.message_id, reference.channel_id) == (555, 4242)
    with discord.http.handle_message_parameters(
            content, message_reference=reference.to_message_reference_dict()) as params:
        wire = params.payload["message_reference"]
    assert wire.get("fail_if_not_exists") is False, wire


def _real_message(mid: int, channel) -> discord.Message:
    msg = discord.Message.__new__(discord.Message)
    msg.id, msg.channel, msg.guild, msg._state = mid, channel, None, None
    return msg


def test_the_reply_reference_helper_only_rewrites_a_real_message():
    """`_reply_reference`：真的訊息 → 帶 `fail_if_not_exists=False` 的引用；其他原樣傳回。"""
    chan = types.SimpleNamespace(id=4242)
    ref = b._reply_reference(_real_message(777, chan))
    assert isinstance(ref, discord.MessageReference), ref
    assert (ref.message_id, ref.channel_id, ref.fail_if_not_exists) == (777, 4242, False)
    stand_in = types.SimpleNamespace(id=1)
    assert b._reply_reference(stand_in) is stand_in
    assert b._reply_reference(ref) is ref


@pytest.mark.parametrize("make", [
    lambda chan, anchor: b._DorossiRestoredMessage(chan, types.SimpleNamespace(id=1), "", 9, anchor),
    lambda chan, anchor: b._DorossiInteractionTrigger(anchor, types.SimpleNamespace(id=1), 9, chan),
], ids=["restored-message", "interaction-trigger"])
def test_an_anchor_proxy_replies_with_a_reference_that_may_vanish(make):
    """重啟後接續的回覆掛在找回來的錨點底下——離當初那則訊息最久，錨點被刪的機會也最大。
    兩個代理都要經過 `_reply_reference`，讓平台自己把死掉的引用丟掉。"""
    sent = []

    class _Chan:
        id = 4242
        guild = None

        async def send(self, content=None, **kwargs):
            sent.append(kwargs.get("reference"))

    chan = _Chan()
    proxy = make(chan, _real_message(888, chan))
    asyncio.run(proxy.reply("hi", mention_author=True))
    (ref,) = sent
    assert isinstance(ref, discord.MessageReference), ref
    assert (ref.message_id, ref.fail_if_not_exists) == (888, False)


@pytest.mark.parametrize("error", [
    "400 Bad Request (error code: 50035): Invalid Form Body\nIn embeds.0.description: "
    "Must be 4096 or fewer in length.",
    "400 Bad Request (error code: 50006): Cannot send an empty message",
], ids=["bad-embed-50035", "empty-50006"])
def test_a_different_rejection_is_not_resent_without_its_reference(error):
    """別種被打回（例如 embed 太長的 50035）要原樣丟回去，不能當成「訊息被刪掉」拿掉
    引用重送。這一邊在分支覆蓋率裡從來沒有成立過（2026-09-22）——每一個測到的
    `HTTPException` 都是引用錯誤。"""
    err = discord.HTTPException.__new__(discord.HTTPException)
    err.args = (error,)
    assert not b._is_unknown_reference_error(err)
    msg = _ResendMessage(reply_error=err)
    with pytest.raises(discord.HTTPException):
        asyncio.run(b.safe_reply(msg, "hello"))
    assert msg.channel.sent == [], "被打回的原因不是引用，卻拿掉引用重送了"


def test_only_a_platform_error_can_be_a_reference_error():
    """文字對得上但型別不是 `HTTPException`（例如我們自己的程式碼丟出來的）不算。"""
    assert not b._is_unknown_reference_error(
        RuntimeError("In message_reference: Unknown message"))
    assert b._is_unknown_reference_error(_unknown_reference_error())


def test_a_normal_reply_does_not_touch_the_channel_fallback():
    msg = _ResendMessage()
    got = asyncio.run(b.safe_reply(msg, "hello"))
    assert got == "sent-via-reply"
    assert msg.replies == [("hello", {})]
    assert msg.channel.sent == [], "正常路徑不該去碰 channel.send"


def test_an_unrelated_error_is_re_raised_not_swallowed():
    """只有「訊息被刪掉」該走 fallback。把別的錯誤一起吞掉，等於所有送出失敗都
    變成靜默——那比原本的例外難查得多。"""
    boom = RuntimeError("something else entirely")
    msg = _ResendMessage(reply_error=boom)
    with pytest.raises(RuntimeError):
        asyncio.run(b.safe_reply(msg, "hello"))
    assert msg.channel.sent == []


def test_a_deleted_message_falls_back_to_the_channel():
    msg = _ResendMessage(reply_error=_unknown_reference_error())
    got = asyncio.run(b.safe_reply(msg, "hello"))
    assert got == "sent-via-channel"
    assert len(msg.channel.sent) == 1
    assert msg.channel.sent[0][0] == "hello"


def test_a_missing_channel_does_not_explode():
    """`message.channel` 取不到時要安靜收工，不能讓例外逸出到 on_message。"""
    msg = _ResendMessage(reply_error=_unknown_reference_error(), channel=None)
    msg.channel = None
    assert asyncio.run(b.safe_reply(msg, "hello")) is None


def test_a_path_backed_attachment_survives_the_fallback(tmp_path):
    """**本節的重點之一。** 路徑型附件在修正前會讓整則訊息消失。

    修正前的行為：第一次送出後 fp 被關掉 → `channel.send` 讀它丟 `ValueError`
    → 被最外層的 `except Exception: return None` 吞掉 → 使用者什麼都沒收到。
    """
    payload = b"PNG-CONTENT-1234567890"
    shot = tmp_path / "shot.png"
    shot.write_bytes(payload)

    original = discord.File(str(shot), filename="screen.png")
    # 第一次送出（並失敗）——這一步會把 fp 關掉，跟正式路徑一模一樣。
    assert _bytes_actually_uploaded(original) == payload
    assert original.fp.closed, "前提變了：discord.py 不再於離開時關檔"

    msg = _ResendMessage(reply_error=_unknown_reference_error())
    asyncio.run(b.safe_reply(msg, "shot", file=original))

    assert len(msg.channel.sent) == 1
    resent = msg.channel.sent[0][1].get("file")
    assert isinstance(resent, discord.File), "附件被丟掉了"
    assert resent is not original, "重送的必須是新的 File，不是同一個"
    assert _bytes_actually_uploaded(resent) == payload


def test_a_buffer_backed_attachment_is_not_resent_empty():
    """**本節的重點之二。** 緩衝區型的壞法更陰險：送得出去，但是空的。

    所以這一支斷言的是**位元組數不為 0**且內容相符，而不是「有沒有附件」——
    後者在修正前也是綠的。
    """
    payload = b"BUFFER-CONTENT-ABCDEF"
    original = discord.File(io.BytesIO(payload), filename="output.txt")
    assert _bytes_actually_uploaded(original) == payload
    assert not original.fp.closed
    assert original.fp.tell() == len(payload), "前提變了：fp 沒有停在 EOF"

    msg = _ResendMessage(reply_error=_unknown_reference_error())
    asyncio.run(b.safe_reply(msg, "out", file=original))

    resent = msg.channel.sent[0][1].get("file")
    assert isinstance(resent, discord.File), "附件被丟掉了"
    data = _bytes_actually_uploaded(resent)
    assert len(data) > 0, "重送的是一個 0 位元組的空檔"
    assert data == payload


def test_an_unrebuildable_attachment_still_sends_the_text(tmp_path):
    """重建不出來時（例如暫存檔已經被清掉）也不能靜默失敗。

    「有文字沒附件」遠好過「什麼都沒有」——後者是使用者根本不知道發生過什麼事。
    """
    gone = tmp_path / "gone.png"
    gone.write_bytes(b"x" * 8)
    original = discord.File(str(gone), filename="gone.png")
    _bytes_actually_uploaded(original)
    gone.unlink()

    msg = _ResendMessage(reply_error=_unknown_reference_error())
    asyncio.run(b.safe_reply(msg, "這是正文", file=original))

    assert len(msg.channel.sent) == 1
    content, kwargs = msg.channel.sent[0]
    assert "file" not in kwargs, "重建不出來就不該再把壞掉的 File 送出去"
    assert "這是正文" in (content or ""), "正文不見了"


def test_the_dropped_attachment_notice_never_leaks_a_host_path(tmp_path):
    """Layer 1：附件重建失敗的說明不得帶出主機路徑或原始例外文字。"""
    gone = tmp_path / "secret_folder" / "gone.png"
    gone.parent.mkdir()
    gone.write_bytes(b"x")
    original = discord.File(str(gone), filename="gone.png")
    _bytes_actually_uploaded(original)
    gone.unlink()

    msg = _ResendMessage(reply_error=_unknown_reference_error())
    asyncio.run(b.safe_reply(msg, "正文", file=original))
    content = msg.channel.sent[0][0] or ""
    assert str(tmp_path) not in content
    assert "secret_folder" not in content
    assert "\\" not in content and "/" not in content.replace("\n", "")


def test_the_owner_is_told_which_attachment_was_dropped(tmp_path):
    """非擁有者拿泛用句，**擁有者要拿到檔名**——否則那個補充說明等於沒寫。

    上面那支只驗了「不會洩漏」那一半。兩支都要有：只驗不洩漏的話，把
    `_owner_detail` 接反（或整段拿掉細節）仍然是綠的，而擁有者從此看不出是哪個
    附件掉了。`_asker_id` 取不到 UID 時 fail-closed 回泛用句，所以這支同時證明
    了取得得到。
    """
    gone = tmp_path / "screen.png"
    gone.write_bytes(b"x")
    original = discord.File(str(gone), filename="screen.png")
    _bytes_actually_uploaded(original)
    gone.unlink()

    msg = _ResendMessage(reply_error=_unknown_reference_error())
    msg.author = types.SimpleNamespace(id=b.OWNER_USER_ID)
    asyncio.run(b.safe_reply(msg, "正文", file=original))

    content = msg.channel.sent[0][0] or ""
    assert "screen.png" in content, (
        f"擁有者沒有被告知是哪個附件掉了：{content!r}")


def test_the_multi_file_kwarg_is_rebuilt_too(tmp_path):
    """`files=`（複數）目前沒有呼叫點，但它是從 `**kwargs` 進來的——將來有人用
    就會中同一個招，所以一起守。"""
    one = tmp_path / "a.txt"
    two = tmp_path / "b.txt"
    one.write_bytes(b"AAA")
    two.write_bytes(b"BBB")
    originals = [discord.File(str(one)), discord.File(str(two))]
    for f in originals:
        _bytes_actually_uploaded(f)

    msg = _ResendMessage(reply_error=_unknown_reference_error())
    asyncio.run(b.safe_reply(msg, "two", files=originals))

    resent = msg.channel.sent[0][1].get("files")
    assert resent and len(resent) == 2
    assert [_bytes_actually_uploaded(f) for f in resent] == [b"AAA", b"BBB"]


# ---------------------------------------------------------------------------
# `_dorossi_state_rmw` 的兩條契約
#
# 工作階段檔的每一次改動都走這一支：拿短鎖 → 重讀 → `mutate(state)` → 存檔。
# 它的 docstring 把兩件事寫成硬規則，但兩件事原本都沒有守門，而且違反的下場都很安靜。
# ---------------------------------------------------------------------------

def _rmw_mutators(tree):
    """交給 `_dorossi_state_rmw` 的那些函式名稱。"""
    import ast
    handed = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_dorossi_state_rmw" and node.args):
            arg = node.args[0]
            if isinstance(arg, ast.Name):
                handed.add(arg.id)
    return handed


def test_every_state_mutator_is_synchronous():
    """交給 `_dorossi_state_rmw` 的 mutator 必須是同步函式、內部不得 `await`。

    失敗形態是**完全靜默的**：`result = mutate(state)` 對 `async def` 只會拿到一個
    coroutine 物件，永遠不會被 await——於是那次改動根本沒發生，而 `_dorossi_save_state`
    照樣把**沒改過的** state 存回去。唯一的線索是一句 'coroutine was never awaited'
    的 RuntimeWarning，而它會淹沒在其他輸出裡。使用者看到的是「指令回覆說成功了，
    但設定沒有變」。

    真正會踩到的路徑很具體：有人想在 mutator 裡多做一件需要 await 的事（送個通知、
    查一下後端），順手把 `def` 改成 `async def`——那一刻起這個 slot 的所有改動就
    再也不會被寫進磁碟。
    """
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    handed = _rmw_mutators(tree)
    assert len(handed) >= 10, (
        f"只找到 {len(handed)} 個 mutator——掃描可能壞了（實際有二十幾個呼叫點）")

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in handed:
            continue
        if isinstance(node, ast.AsyncFunctionDef):
            offenders.append((node.name, node.lineno, "async def"))
            continue
        awaits = [n.lineno for n in ast.walk(node)
                  if isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))]
        if awaits:
            offenders.append((node.name, node.lineno, f"await 於 {awaits}"))
    assert not offenders, (
        f"{offenders} 被交給 `_dorossi_state_rmw`，但它不是同步的。"
        "`mutate(state)` 不會 await 回傳值，所以那次改動根本不會發生，"
        "而 state 照樣被存回去——指令回覆說成功了，設定卻沒有變。")


def test_the_state_lock_is_never_held_across_an_await():
    """短鎖只能包住「重讀 → 改 → 存」，中間不得有任何 `await`。

    docstring 寫的是「The lock is held ONLY for this microsecond RMW — NEVER
    across a backend call — so it can't reintroduce global serialisation」。
    在鎖裡 await 一次後端呼叫（動輒數十秒到數分鐘），所有其他工作階段的每一次狀態
    改動都會排在後面——**多工作階段並行整個失效**，而症狀只是「bot 有時候很慢」，
    不會有任何錯誤。
    """
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef)
               and n.name == "_dorossi_state_rmw"), None)
    assert fn is not None, "函式改名了——這支守門要跟著改"

    holds = [n for n in ast.walk(fn) if isinstance(n, ast.AsyncWith)]
    assert holds, "`_dorossi_state_rmw` 裡沒有 `async with`——鎖不見了"
    inner_awaits = [n.lineno for w in holds for n in ast.walk(w)
                    if isinstance(n, ast.Await)]
    assert not inner_awaits, (
        f"第 {inner_awaits} 行在狀態鎖裡 `await`。那把鎖只能包住「重讀 → 改 → 存」"
        "這幾微秒；跨到任何 I/O 上都會讓所有工作階段重新變成序列執行，"
        "而症狀只是『有時候很慢』，不會有錯誤。")


def test_every_session_lock_is_released_in_a_finally():
    """`_dorossi_acquire_session_lock` 必須配一個 `finally` 裡的 release。

    它的 docstring 就是這樣寫的（"Pair with `_dorossi_release_session_lock` in a
    finally"），但原本沒有守門。漏掉的下場是**慢性的、無聲的**：ref-count 永遠回不
    到 0，於是那把鎖與它的佇列項目永遠不會被回收——多開幾個工作階段就多留幾份，
    而且沒有任何錯誤。更糟的是「release 有寫、但不在 finally」：正常路徑看起來完全
    正確，只有在那一輪拋例外時才漏，也就是**只在出事的時候才會再壞一次**。

    所以這裡要求的是 `finally`，不只是「有沒有呼叫」。
    """
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))

    def _calls(scope, name) -> list:
        return [n.lineno for n in ast.walk(scope)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == name]

    checked, offenders = 0, []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _calls(fn, "_dorossi_acquire_session_lock"):
            continue
        checked += 1
        in_finally = [
            line for node in ast.walk(fn) if isinstance(node, ast.Try)
            for stmt in node.finalbody
            for line in _calls(stmt, "_dorossi_release_session_lock")]
        if not in_finally:
            anywhere = _calls(fn, "_dorossi_release_session_lock")
            offenders.append(
                (fn.name, fn.lineno,
                 f"release 在 {anywhere} 但不在 finally" if anywhere
                 else "完全沒有 release"))
    assert checked >= 4, (
        f"只找到 {checked} 個取鎖的函式——掃描可能壞了（實際有五個）")
    assert not offenders, (
        f"{offenders}：取了 per-session 鎖卻沒有在 `finally` 裡放掉。"
        "ref-count 回不到 0，鎖與佇列項目就永遠不會被回收，而且不會有任何錯誤。")


def test_every_attachment_reply_goes_through_safe_reply():
    """帶附件的**回覆**一律走 `safe_reply`，不要自己 `message.reply(file=...)`。

    這條規則之前是反過來的，而且寫在 `discord-bot-expert.md` 裡：因為
    `safe_reply` 的退路會重送同一個已經用過的 `discord.File`，當時的正確做法確實是
    繞過它。`_rebuild_send_kwargs` 進來之後那個理由消失了，而繞過的代價是實打實的
    ——觸發訊息被刪掉時，例外會被 `_handle_mention` / on_message 的外層 guard 安靜
    吞掉，**使用者連文字都收不到**。

    只管 `.reply()`：它會帶 `message_reference`，那正是會被 50035 打回的東西。
    `channel.send(files=...)` 沒有 reference（例如 `_dorossi_send_images` 是在頻道
    裡新開一則，不是回覆），本來就不需要退路。
    """
    import ast

    def _attachment_replies(tree) -> list:
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "reply"):
                continue
            if {kw.arg for kw in node.keywords} & {"file", "files"}:
                found.append((node.lineno, ast.unparse(node.func)))
        return found

    # 守門的自我檢查。把上面那個比對條件改壞（例如永遠不記錄），真實原始碼因為
    # 已經沒有違規而照樣是空的——測試會保持綠，等於守門被無聲拆掉。變異測試當場
    # 抓到這一點，所以先拿一段合成原始碼證明比對真的會命中。
    probe = ast.parse(
        "async def f(m):\n"
        "    await m.reply(content='x', file=1)\n"
        "    await m.reply('plain')\n"
        "    await m.channel.send(files=[1])\n")
    hits = _attachment_replies(probe)
    assert [h[1] for h in hits] == ["m.reply"], (
        f"比對條件壞了：合成樣本應該只命中 `m.reply(file=...)`，實際 {hits}")

    offenders = _attachment_replies(
        ast.parse(Path(b.__file__).read_text(encoding="utf-8")))
    assert not offenders, (
        f"{offenders} 直接用 `.reply(file=...)` 送附件。改走 "
        "`safe_reply(message, content, file=...)`：觸發訊息被刪掉時它會重建附件並"
        "改用 `channel.send` 重送，繞過它則是整則訊息安靜消失。")


def test_no_handler_replies_without_the_deleted_message_fallback():
    """`discord_bot.py` 裡**唯一**允許呼叫 `.reply()` 的地方是 `safe_reply` 自己。

    這條比 `test_every_attachment_reply_goes_through_safe_reply` 寬得多——那一條
    只管帶附件的回覆，因為附件出事最明顯（整則訊息消失）。但沒有附件的回覆一樣會
    被 50035 打回：使用者送了指令、等得不耐煩把訊息刪掉，處理完的回覆就落在一則
    不存在的訊息上，例外往上冒，外層 guard 安靜吞掉，使用者什麼都收不到。

    判準是「視窗」不是「重要性」：只要**使用者有時間在回覆送達前刪掉訊息**，這個
    處理器就需要退路。慢的處理器視窗大，但快的也不是零——訊息刪除是使用者說了算，
    不是我們說了算。既然全部轉完了，就用「一個都不准剩」把它釘住；留下例外名單
    等於留下一條「下次順手加一個」的路。

    `safe_reply` 內部那一個是它的實作本體，轉掉會無窮遞迴，所以是唯一豁免。
    `channel.send(...)` 不在本規則內：它不帶 `message_reference`，沒有東西可以被
    打回。`_DorossiRestoredMessage.reply` / `_InteractionMessageProxy.reply` 是
    **定義**不是呼叫，掃不到也不該掃到——兩者都能接 `safe_reply` 的呼叫形狀。
    """
    import ast
    tree, _text = _bot_ast()

    enclosing = {}

    def _walk(node, fn):
        for child in ast.iter_child_nodes(node):
            nxt = child.name if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            enclosing[child] = nxt
            _walk(child, nxt)

    def _bare_replies(root):
        enclosing.clear()
        _walk(root, "<module>")
        out = []
        for node in ast.walk(root):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "reply"):
                continue
            if enclosing.get(node) == "safe_reply":
                continue
            out.append((node.lineno, ast.unparse(node.func)))
        return out

    # 守門的自我檢查。比對條件被改壞（例如把 `attr == "reply"` 寫死成永不成立）
    # 時，真實原始碼因為已經清乾淨而照樣是空的，測試會保持綠——等於守門被無聲
    # 拆掉。先拿一段合成原始碼證明它真的會命中，而且真的會放過該放過的。
    probe = ast.parse(
        "async def safe_reply(message, content=None, **kwargs):\n"
        "    return await message.reply(content, **kwargs)\n"
        "async def handler(message):\n"
        "    await message.reply('boom')\n"
        "    await message.channel.send('fine')\n"
        "    await safe_reply(message, 'fine')\n")
    hits = _bare_replies(probe)
    assert [h[1] for h in hits] == ["message.reply"], (
        f"比對條件壞了：合成樣本應該只命中 handler 裡那一個，實際 {hits}")

    offenders = _bare_replies(tree)
    assert not offenders, (
        f"這些地方直接呼叫 `.reply()`：{offenders}。改走 "
        "`safe_reply(message, ...)`——觸發訊息被刪掉時它會拿掉那個死掉的 "
        "reference 改用 `channel.send` 重送，繞過它則是使用者連文字都收不到。")


def test_safe_reply_is_the_one_place_that_calls_reply():
    """上一條的另一半：`safe_reply` 內部那一個 `message.reply` 不准消失。

    只驗「沒有人呼叫 `.reply()`」的話，把 `safe_reply` 的實作整個換成
    `channel.send` 也會是綠的——但那樣就沒有回覆串接了，每則回應都變成頻道裡
    一則孤立的新訊息。豁免存在的前提是它真的還在做回覆。
    """
    import ast
    tree, _text = _bot_ast()
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "safe_reply"),
              None)
    assert fn is not None, "safe_reply 改名了——上面那條守門的豁免要跟著改"
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "reply"]
    assert len(calls) == 1, (
        f"`safe_reply` 裡有 {len(calls)} 個 `.reply()` 呼叫，預期剛好一個")


def test_no_call_site_hand_rolls_the_reference_fallback():
    """重送邏輯只能有一份。

    `_dorossi_export_session` 原本自己抄了一份（接 50035、`fp.seek(0)`、再
    `channel.send` 一次），而且註解寫著「safe_reply 會重送同一個已在 EOF 的
    BytesIO，所以這裡自己處理」——那句話在附件重建進到 `safe_reply` 之後就不成立
    了，但**沒有任何測試會因此變紅**。抄一份的代價不只是重複：那一份沒有
    `channel` 取不到時的保護，也沒有「重建不出來就至少送出文字」。
    """
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    # 名單制，每一筆都要寫理由——這是本專案既有的作法（`_ALLOWED_WRITE_ONLY`、
    # `_ALLOWED_BASE_HANDLERS` 同樣形狀）。判準是「有沒有自己再送一次」，不是
    # 「有沒有提到這個判定函式」：只是把 50035 吞掉、不重送的，不在本規則之內。
    allowed = {
        "safe_reply": "重送路徑本人",
        "_is_unknown_reference_error": "判定函式本人",
        # `@bot` 這條路在 on_message 沒有外層保護，逸出去只會變成一行
        # 「Ignoring exception in on_message」。這裡是**只吞不重送**的兜底：
        # 觸發訊息被刪掉不是真的失敗，所以不計進錯誤數，也不重送。
        "_handle_mention": "兜底：只吞掉、不自己重送，也不動錯誤計數",
    }
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in allowed:
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_is_unknown_reference_error"):
                offenders.append((node.name, call.lineno))
    assert not offenders, (
        f"{offenders} 自己判斷了「訊息被刪掉」這件事。那條重送路徑只能有一份，"
        "在 `safe_reply` 裡——它會重建附件、會處理 channel 取不到、"
        "會在重建不出來時至少把文字送出去。")


# ---------------------------------------------------------------------------
# 四個長命背景迴圈：排程、事件監看、每日健檢、presence 探測
#
# 它們原本是全專案唯一沒接上 `_bg_task_done` 的 task。後果分兩層，而且兩層都安靜：
#
#   * 沒有名字也沒人取例外 → asyncio 只會在 task 被 GC 時補一句泛用的
#     'Task exception was never retrieved'，看不出是哪個功能死了；
#   * 復原掛在**重新連線**上（`_ensure_background_tasks_alive` 只有 `on_ready`
#     與 `on_resumed` 會叫），所以連線穩定時，一個死掉的迴圈可能躺好幾個小時。
#
# 這不是理論問題：`presence_probe._media_to_activity` 的 docstring 就記著 SMTC 回
# `{"title": None}` 時 `None[:128]` 會 TypeError，「這個例外會從 bot 的 presence
# 迴圈逸出、讓 presence 靜默凍結在最後狀態」。那個**成因**修掉了，結構性的缺口還在。
# ---------------------------------------------------------------------------

def test_a_crashed_background_loop_leaves_a_named_line(capsys):
    """`_bg_task_done` 要把例外**取走**並印出帶名字的一行。

    取走這件事本身有意義：asyncio 因此不會再在 GC 時補那句泛用訊息，而我們換到的
    是一行當場、指名道姓的紀錄。
    """
    async def _boom():
        raise ValueError("presence probe exploded")

    async def _drive():
        task = asyncio.get_running_loop().create_task(_boom(), name="presence-probe")
        task.add_done_callback(b._bg_task_done)
        try:
            await task
        except ValueError:
            pass
        await asyncio.sleep(0)      # 讓 done callback 跑完
        return task

    task = asyncio.run(_drive())
    line = capsys.readouterr().err
    assert "presence-probe" in line, f"沒有印出 task 名字：{line!r}"
    assert "ValueError" in line, f"沒有印出原因：{line!r}"
    assert task.exception() is not None


def test_a_cancelled_loop_is_not_reported_as_a_crash(capsys):
    """關機時這四個都會被取消。取消不是崩潰，不該吵。

    而且 `if task.cancelled(): return` 那道**是承重的，不是省事**：對一個被取消的
    task 呼叫 `task.exception()` 會丟 `CancelledError`，而它從 3.8 起繼承的是
    `BaseException`——底下那個 `except Exception` **接不住**。少了這道，回呼自己會
    往外拋，asyncio 只好交給 loop 的例外處理器，於是每次關機都多一則
    'Exception in callback' 的雜訊。所以這裡除了確認沒印出崩潰訊息，也裝一個 loop
    例外處理器，確認回呼本身沒有拋出去——只驗前者的話，把那道拿掉是驗不出來的。
    """
    seen: list = []

    async def _forever():
        await asyncio.sleep(3600)

    async def _drive():
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
        task = loop.create_task(_forever(), name="schedule-loop")
        task.add_done_callback(b._bg_task_done)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    asyncio.run(_drive())
    assert "crashed" not in capsys.readouterr().err
    assert not seen, (
        "`_bg_task_done` 自己在被取消的 task 上拋了例外："
        f"{[c.get('message') for c in seen]}。"
        "`task.exception()` 對取消中的 task 會丟 `CancelledError`，"
        "那是 `BaseException` 的子類別，`except Exception` 接不住。")


def test_every_long_lived_loop_is_started_through_the_supervised_helper():
    """`_ensure_background_tasks_alive` 裡不得再出現裸的 `create_task`。

    這一支釘的是「新增第五個背景迴圈的人不會知道有這條規則」。裸的 `create_task`
    看起來完全正常、測試也不會紅，而代價是那個迴圈死掉時沒有任何線索。

    **數的是意圖，不是呼叫點。** 原本這支直接數 `_start_supervised_task(` 出現
    幾次，要求 ≥4。2026-09-07 有人把四份重複的 try/except 收斂成一個巢狀 helper
    （`_revive`），於是 `_start_supervised_task` 只剩**一個**字面呼叫點、四個
    `_revive(...)` 呼叫——**守門紅了，但程式碼變好了**。那是守門的錯不是重構的錯：
    一支只認得某一種寫法的守門，會把「把重複收斂掉」懲罰成違規，而那正是本專案
    到處在做的事。所以改成先找出「本身會呼叫 `_start_supervised_task` 的巢狀
    helper」，再把對它的呼叫一起算進來。
    """
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_ensure_background_tasks_alive"), None)
    assert fn is not None, "函式改名了——這支守門要跟著改"

    # 巢狀 helper：它自己會呼叫 `_start_supervised_task`，所以呼叫它＝走了受監督
    # 的建立方式。
    helpers, helper_nodes = set(), []
    for node in ast.walk(fn):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node is fn:
            continue
        if any(isinstance(c, ast.Call)
               and ast.unparse(c.func) == "_start_supervised_task"
               for c in ast.walk(node)):
            helpers.add(node.name)
            helper_nodes.append(node)
    # helper 內部那一次不算——算的是「有幾條迴圈」，不是「helper 怎麼實作」。
    # **只排除計數，不排除裸 `create_task` 的掃描**：一個同時呼叫
    # `_start_supervised_task` 與裸 `create_task` 的 helper，如果整段被跳過，就會
    # 帶著一條沒人看管的迴圈溜過去。
    inside_helper = {id(c) for h in helper_nodes for c in ast.walk(h)
                     if isinstance(c, ast.Call)}

    bare, supervised = [], 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name.endswith("create_task") or name.endswith("ensure_future"):
            bare.append(node.lineno)       # helper 裡面的也算
            continue
        if id(node) in inside_helper:
            continue
        if name == "_start_supervised_task" or name in helpers:
            supervised += 1
    assert not bare, (
        f"第 {bare} 行用了裸的 `create_task`。長命背景迴圈要走 "
        "`_start_supervised_task`，否則它安靜死掉時只會留下 asyncio 那句沒有名字的"
        "'Task exception was never retrieved'，而且要等 GC 才出現。")
    assert supervised >= 4, (
        f"只有 {supervised} 個迴圈走了受監督的建立方式，預期至少 4 個"
        "（排程／事件監看／每日健檢／presence 探測）。"
        f"（認得的受監督 helper：{sorted(helpers) or '無'}）")


def test_the_supervised_helper_names_and_watches_the_task():
    """helper 本身必須做兩件事：給名字、掛上 `_bg_task_done`。少任何一件，
    上面那支 AST 守門就只是在確認呼叫了一個什麼都沒做的函式。"""
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_start_supervised_task"), None)
    assert fn is not None
    body = ast.unparse(fn)
    assert "name=label" in body, "沒有把 label 傳成 task 名字"
    assert "add_done_callback(_bg_task_done)" in body, "沒有掛上崩潰回報"


def test_the_alert_scope_follows_a_config_reload():
    """`_alert_mentions()` 必須是函式，不能是開機時算好的常數。

    `ALERT_USER_ID` 會被 `cmd_config_reload()` 改掉（`!config_set` 之後那次重載）。

    因果方向要講清楚，因為它跟直覺相反：**舊的 `users=True` 對重載是免疫的**
    （`True` 裡沒有任何 id，重載後照樣放行新的那個人）。是「改成允許清單」這件事
    本身讓這個值變得會過期。而且失敗形態不是「ping 到舊的人」——`_alert_prefix()`
    是函式、會產出 `<@新id>`，清單卻留著舊 id，兩邊對不起來的結果是**誰都不會被
    ping**。警報安靜地不叫人，是這條路上最糟的失敗形態。
    所以寫成函式不是順手的好習慣，是允許清單這個修正的必要配套。
    """
    saved = b.ALERT_USER_ID
    try:
        b.ALERT_USER_ID = 111
        assert b._alert_mentions().to_dict()["users"] == [111]
        b.ALERT_USER_ID = 222
        assert b._alert_mentions().to_dict()["users"] == [222], (
            "換了設定之後範圍沒跟著換——`_alert_mentions` 被當成常數算了")
    finally:
        b.ALERT_USER_ID = saved


def test_no_alert_user_means_nothing_gets_pinged():
    """沒設定警報對象時 `_alert_prefix()` 回空字串，本來就沒有要 ping 的人。

    **不能**把 0 直接塞進去：`discord.Object(id=0)` 產出的是 `users: [0]`，那是
    一個「允許清單」而不是「全關」，語意上剛好相反。這是目前的實際狀態
    （`bot_config.json` 的 `alert_user_id` 是 0），所以這一支測的是現在跑著的那條路。
    """
    saved = b.ALERT_USER_ID
    try:
        b.ALERT_USER_ID = 0
        assert b._alert_mentions().to_dict() == {"parse": []}, (
            "沒設定警報對象時要 fail-closed（全關）")
        assert b._alert_prefix() == ""
    finally:
        b.ALERT_USER_ID = saved


def test_an_alert_carrying_queue_text_cannot_ping_a_third_party():
    """兩則警報會把**佇列來的角色名字**印出來，而 `todo` 不在
    `_OWNER_ONLY_GROUPS` 裡——能在頻道講話的人就能塞一筆進去。

    今天沒有實際破口，但擋著的是別的模組的巧合：`character_folder_name()` 的
    `re.sub` 為了 Windows 檔名合法性把 `<` `>` 換成 `_`，順手讓 `<@123>` 湊不成一個
    mention。那條 regex 的用途是檔名不是 mention 安全，放寬它這裡就會安靜地變回
    ping 放大器。這一支直接在 mention 那一層驗，不依賴上游那個巧合。
    """
    import discord.http

    saved = b.ALERT_USER_ID
    try:
        b.ALERT_USER_ID = 777
        params = discord.http.handle_message_parameters(
            f"{b._alert_prefix()}⚠️ **<@&1234567890>／<@1234567890>** "
            "第 3 張的內容跟先前存過的完全相同",
            allowed_mentions=b._alert_mentions(),
            previous_allowed_mentions=b.client._connection.allowed_mentions)
        got = params.payload.get("allowed_mentions")
    finally:
        b.ALERT_USER_ID = saved
    assert got == {"users": [777], "replied_user": True, "parse": []}, got


def test_no_send_site_uses_a_blanket_users_true():
    """`AllowedMentions(users=True)` ＝「內容裡出現的任何使用者 mention 都放行」。

    這個 bot 到處都在回聲使用者輸入，所以那個寫法只有在「訊息完全由 bot 自己組
    成」時才安全——而那個前提會隨著訊息被加字而失效，且不會有任何訊號。要 ping
    特定的人請用 `_mentions_for(user)` 或 `_alert_mentions()`，兩者都是允許清單。
    """
    import ast
    source = Path(b.__file__).read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func) if node.func else ""
        if not name.endswith("AllowedMentions"):
            continue
        for kw in node.keywords:
            if (kw.arg in ("users", "roles", "everyone")
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True):
                offenders.append((kw.arg, node.lineno))
    assert not offenders, (
        f"discord_bot.py {offenders}：mention 開成了無條件放行。"
        "要 ping 特定對象請給允許清單（`users=[...]`），不要給 `True`。")


def test_every_alert_send_opts_into_pinging_explicitly():
    """帶 `_alert_prefix()` 的送出點必須自己把 `allowed_mentions` 講出來。

    基準線把使用者 mention 關掉了，所以警報那句 `<@alert_user_id>` 在基準線下
    **不會發通知**——警報安靜地失效是最糟的失敗形態（要出事的時候才發現沒人被
    叫醒）。新增警報送出點時這支測試會紅。

    比對的是**值**，不是「有沒有這個關鍵字」。原本只檢查關鍵字存不存在，於是在
    警報送出點寫 `allowed_mentions=DEFAULT_MENTIONS`（或 `AllowedMentions.none()`）
    會讓兩支守門都綠——這一支看到關鍵字在，另一支只抓 `users=True`——而警報從此
    安靜地不叫人，正好是這支測試自己說要防的那個失敗形態。
    """
    import ast
    source = Path(b.__file__).read_text(encoding="utf-8")
    wrong = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Attribute)
                and node.func.attr in ("send", "reply")):
            continue
        if "_alert_prefix()" not in ast.unparse(node):
            continue
        value = next((ast.unparse(kw.value) for kw in node.keywords
                      if kw.arg == "allowed_mentions"), None)
        if value != "_alert_mentions()":
            wrong.append((node.lineno, value))
    assert not wrong, (
        "這些警報送出點的 `allowed_mentions` 不是 `_alert_mentions()`，"
        f"`<@alert_user_id>` 不會發通知：{wrong}")

# ---------------------------------------------------------------------------
# webrunner log 的輪替：崩潰重生不得把「記錄崩潰原因的那份 log」洗掉
#
# `_spawn_webrunner` 會截斷 `webrunner.log`（每輪重來，刻意的），但**監督者的
# respawn 也走同一條路**。也就是說行程一崩潰，重生做的第一件事就是洗掉剛剛記下
# 崩潰原因的那份 log；連續失敗時每輪再洗一次，等到有人去看只剩最後那一輪——
# 而那通常是最短、資訊最少的一輪。事後可讀性正好在最需要的時候歸零。
# ---------------------------------------------------------------------------


def _with_temp_logs(func):
    """把兩個 log 常數指到一個暫時目錄，跑完還原。"""
    import tempfile
    import shutil
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="logrotate_test_"))
    saved = (b.WEBRUNNER_LOG, b.WEBRUNNER_LOG_PREV)
    b.WEBRUNNER_LOG = tmp / "webrunner.log"
    b.WEBRUNNER_LOG_PREV = tmp / "webrunner.prev.log"
    try:
        return func(b.WEBRUNNER_LOG, b.WEBRUNNER_LOG_PREV)
    finally:
        b.WEBRUNNER_LOG, b.WEBRUNNER_LOG_PREV = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_respawn_keeps_the_previous_log_instead_of_wiping_it():
    """輪替之後，上一輪的內容必須完整留在 `.prev` 裡。

    這正是崩潰之後唯一還讀得到成因的地方。
    """
    def body(live, prev):
        live.write_text("crash evidence line\n", encoding="utf-8")
        b._rotate_webrunner_log()
        assert prev.exists(), (
            "輪替沒有留下上一輪的 log——監督者一 respawn，崩潰原因就永遠消失了。")
        assert prev.read_text(encoding="utf-8") == "crash evidence line\n"
        assert not live.exists(), (
            "改名之後原檔還在？那接下來的 `open(..., 'w')` 會截斷一份**新**的檔，"
            "而 `.prev` 是複本不是搬移——兩份會漸漸對不起來。")
    _with_temp_logs(body)


def test_rotation_keeps_exactly_one_generation():
    """只留一代：第二次輪替要覆蓋掉第一代，不得無限累積。

    原本的設計是「每輪截斷」，體積因此有界；輪替不可以把這個性質弄丟。
    """
    def body(live, prev):
        live.write_text("run 1\n", encoding="utf-8")
        b._rotate_webrunner_log()
        live.write_text("run 2\n", encoding="utf-8")
        b._rotate_webrunner_log()
        assert prev.read_text(encoding="utf-8") == "run 2\n", (
            "第二次輪替沒有覆蓋掉第一代。")
        siblings = sorted(p.name for p in prev.parent.iterdir())
        assert siblings == ["webrunner.prev.log"], (
            f"目錄裡多出了別的世代：{siblings}。只保留一代是刻意的——"
            "原本每輪截斷所以體積有界，輪替不能把這個性質弄丟。")
    _with_temp_logs(body)


def test_an_empty_or_missing_log_does_not_produce_a_useless_prev():
    """沒東西可留就不要留。

    第一次啟動時 log 根本不存在；把一個空檔改名成 `.prev` 只會讓人以為
    「上一輪沒留下任何線索」，而真相是根本沒有上一輪。
    """
    def body(live, prev):
        b._rotate_webrunner_log()                   # 檔案不存在
        assert not prev.exists(), "不存在的 log 也被輪替了"
        live.write_text("", encoding="utf-8")       # 存在但是空的
        b._rotate_webrunner_log()
        assert not prev.exists(), "空的 log 也被輪替了"
        assert live.exists(), "空的 log 不該被搬走"
    _with_temp_logs(body)


def test_rotation_survives_a_real_lock_instead_of_losing_the_log():
    """檔案被別的 handle 握著時，輪替要退到「複製」——**不能只是放棄**。

    這條是實測出來的，不是防禦性想像。`webrunner.log` 平常就被別的行程握著：
    `start_webrunner.py` 這支獨立監督者用 append 模式 tee 子行程輸出，而它的
    壽命跨越每一次 respawn；webrunner 自己也握著同一個檔（`WEBRunner.log` 與
    `webrunner.log` 在 NTFS 上是同一個檔）。2026-08-30 在這台機器上量到的三件事：

        os.replace       → PermissionError [WinError 32]（被擋）
        shutil.copyfile  → 成功
        open(…, "w")     → **也成功**

    第三行是關鍵。原本的寫法是「改名失敗就放棄輪替、讓呼叫端照舊截斷」，但截斷
    偏偏不會失敗——於是這個函式在**最常見的設定下等於什麼都沒做**，log 照樣被
    洗掉，而且是安靜的，stderr 上還留著一句看起來像刻意讓步的 "rotate skipped"。

    這支測試用一個真的 handle 去鎖（同一個行程持有一樣擋得住改名，實測確認），
    不是 monkeypatch 一個例外：要驗的是真實的作業系統行為，不是我對它的假設。
    """
    def body(live, prev):
        live.write_text("crash evidence" + chr(10), encoding="utf-8")
        holder = open(live, "a", encoding="utf-8")   # 真的鎖住
        try:
            b._rotate_webrunner_log()               # 不得拋出
            assert prev.exists(), (
                "被鎖住時輪替什麼都沒留下。呼叫端接下來的 `open(..., 'w')` "
                "**不會**因為鎖而失敗，所以「放棄輪替」等於「log 照樣被洗掉」。")
            assert prev.read_text(encoding="utf-8") == "crash evidence" + chr(10)
            assert live.exists(), "輪替退到複製之後，原本的 log 不該消失"
        finally:
            holder.close()
    _with_temp_logs(body)


def test_a_reader_holding_the_log_does_not_break_the_rotation():
    """`_recent_log_error_count` 可以丟進工作執行緒的前提（2026-09-19）。

    丟到執行緒，就可能在 `_spawn_webrunner` 輪替的同一瞬間正開著這個檔在讀。上一支
    用的是 append handle（監督者那種）；這一支用的是**讀取** handle（`read_text`
    那種）。改名一樣會被擋，所以要驗的是後面兩步：輪替退到複製、以及 spawn 緊接著
    做的截斷重開，在讀者還開著時都要成功——否則把 log 讀取丟進執行緒，就會讓某次
    spawn 丟掉上一輪的 log。
    """
    def body(live, prev):
        live.write_text("crash evidence\n", encoding="utf-8")
        reader = open(live, "r", encoding="utf-8")
        try:
            reader.read(5)
            b._rotate_webrunner_log()               # 不得拋出
            assert prev.read_text(encoding="utf-8") == "crash evidence\n", (
                "讀者開著時輪替沒有留下上一輪的 log")
            with open(live, "w", encoding="utf-8") as fresh:   # spawn 的下一步
                fresh.write("new run\n")
        finally:
            reader.close()
        assert live.read_text(encoding="utf-8") == "new run\n"
    _with_temp_logs(body)


def test_rotation_still_never_stops_the_spawn_when_even_the_copy_fails():
    """改名與複製都失敗時，仍然只記 stderr，不得往上拋。

    **能繼續寫 log 比留住歷史重要**——輪替失敗絕不能反過來讓 `_spawn_webrunner`
    整個炸掉、批次起不來。
    """
    import os

    def boom(*_a, **_k):
        raise PermissionError(32, "being used by another process")

    def body(live, prev):
        live.write_text("locked" + chr(10), encoding="utf-8")
        saved_replace = os.replace
        saved_copy = b._shutil.copyfile
        os.replace = boom
        b._shutil.copyfile = boom
        try:
            b._rotate_webrunner_log()          # 不得拋出
        finally:
            os.replace = saved_replace
            b._shutil.copyfile = saved_copy
        assert live.exists(), "兩條路都失敗之後，原本的 log 不該消失"
        assert not prev.exists(), "複製失敗了卻留下一個 .prev？"
    _with_temp_logs(body)


def test_clearing_the_log_also_drops_the_previous_generation():
    """使用者說「清空 log」時，不該還留著一份上一輪的複本。

    留著的話既不符合預期，回報的「釋出 N KB」也是不實的。
    """
    def body(live, prev):
        prev.write_text("x" * 2048, encoding="utf-8")
        freed = b._discard_prev_log()
        assert freed == 2048, freed
        assert not prev.exists()
        assert b._discard_prev_log() == 0, "已經不存在時要回 0，不能再算一次"
    _with_temp_logs(body)


def test_the_spawn_path_actually_calls_the_rotation():
    """反面：helper 寫好了卻沒有被 `_spawn_webrunner` 呼叫的話，一切照舊而且無聲。

    用 AST 確認呼叫點就在**截斷之前**——順序反了等於沒做。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(b._spawn_webrunner))
    rotate_line = open_line = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_rotate_webrunner_log"):
            rotate_line = node.lineno
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "open"
                and any(isinstance(a, ast.Constant) and a.value == "w"
                        for a in node.args)):
            open_line = node.lineno
    assert rotate_line is not None, (
        "`_spawn_webrunner` 沒有呼叫 `_rotate_webrunner_log()`——"
        "輪替寫好了但沒接上，行為跟修之前一模一樣而且完全無聲。")
    assert open_line is not None, (
        "找不到 `open(..., \"w\")`；截斷的寫法改過的話這支測試要跟著改。")
    assert rotate_line < open_line, (
        "輪替跑在截斷**之後**——那會把剛清空的檔案留成 `.prev`，"
        "上一輪的內容照樣不見。")


# ---------------------------------------------------------------------------
# repo root 的每一個 append-only ndjson 都必須有上界
#
# `_spawn_webrunner` 的註解自己宣告了意圖（"the append-only ndjson logs would
# otherwise grow forever"），但那一段是**手列**的，而手列的清單一定會過期：
# `generate_history.ndjson` 與 `dorossi_events.ndjson` 從加進來的那天起就不在裡面。
# 失敗形態是安靜的——檔案照長，沒有任何錯誤訊息，只有 `/gen history`、
# `/dorossi logs` 與儀表板的每一次輪詢在**逐行讀整份**，代價隨檔案線性上升。
#
# 分類**從機制推出來**，不是另一張手列清單：
#   * 有人對它 `open("a")` → append-only → 必須被 `_rotate_ndjson_tail` 接上，
#     或列進 `_NDJSON_SELF_LIMITING` 並寫下理由；
#   * 沒有 append（整檔重寫，例如 `dorossi_queue{,_failed}.ndjson` 的
#     `tmp.write_text` + `os.replace`）→ 行數由內容本身決定，自我設限，不必登記。
#     哪天有人替它加了一條 append，它就會**自動**掉進上面那一類然後變紅。
# ---------------------------------------------------------------------------

_NDJSON_ROOT_NAMES = frozenset({"PROJECT_ROOT", "_PROJECT_ROOT", "REPO_ROOT"})

# 唯一的豁免：append-only 但自己修剪。理由要寫得夠具體，好讓下一個人**驗證得了**
# 它是不是還成立（見 `test_the_usage_ledger_exemption_still_earns_it`）。
_NDJSON_SELF_LIMITING = {
    "dorossi_usage.ndjson": (
        "自己修剪：`dorossi_backend._dorossi_record_usage` 每次 append 之後呼叫 "
        "`_dorossi_trim_usage_file`，把行數壓回 `_DOROSSI_USAGE_MAX_LINES`"
        "（要過 `_DOROSSI_USAGE_TRIM_AT` 才動，所以是 hysteresis、不是每次重寫）。"),
}


def _ndjson_literal_of(node, consts: dict):
    """把一個 AST 節點解成 repo root 的 `<x>.ndjson` 檔名，解不出來回 None。

    兩種形狀都要認：綁成常數的（`EVENTS_FILE`）與就地寫的
    （`PROJECT_ROOT / "x.ndjson"`）。只認前者的話，一個 inline 的 append 會完全
    隱形——而「隱形」正是這支守門要消滅的東西。

    **第三種形狀：包了一層單引數呼叫。** 逐平台的狀態檔寫成
    `_platform_state(PROJECT_ROOT / "x.ndjson")`，實體檔案落在
    `state/<平台>/<平台>.x.ndjson`，但**基底檔名仍然是那個字面值**，而這支守門問的
    「這個 append-only 紀錄檔有沒有上界」跟它住在哪個目錄無關。不認的話那幾個檔會
    一夕之間全部從掃描裡消失——而「零筆無上界」跟「全部都有上界」長得一模一樣。
    """
    import ast
    if (isinstance(node, ast.Call) and len(node.args) == 1
            and not node.keywords):
        node = node.args[0]
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name)
            and node.left.id in _NDJSON_ROOT_NAMES
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
            and node.right.value.endswith(".ndjson")):
        return node.right.value
    return None


def _ndjson_constants_in(tree) -> dict:
    """{常數名: 檔名}，只收 `<root> / "<x>.ndjson"` 的賦值。"""
    import ast
    out: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        literal = _ndjson_literal_of(node.value, {})
        if literal is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = literal
    return out


def _opens_in_append_mode(call) -> bool:
    """這個 `open` 呼叫是不是附加模式？位置引數與 `mode=` 兩種寫法都看。"""
    import ast
    modes = [a.value for a in call.args
             if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    modes += [k.value.value for k in call.keywords
              if k.arg == "mode" and isinstance(k.value, ast.Constant)
              and isinstance(k.value.value, str)]
    return any("a" in mode for mode in modes)


def _classify_ndjson_logs(modules) -> tuple[dict, dict]:
    """modules: [(標籤, ast.Module)]。回 (被 append 的, 被輪替的)，都是 檔名→{標籤}。"""
    import ast
    appended: dict = {}
    rotated: dict = {}
    for label, tree in modules:
        consts = _ndjson_constants_in(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if (isinstance(node.func, ast.Attribute)
                    and node.func.attr == "open"
                    and _opens_in_append_mode(node)):
                target = node.func.value          # `X.open("a", …)`
            elif (isinstance(node.func, ast.Name) and node.func.id == "open"
                  and node.args and _opens_in_append_mode(node)):
                target = node.args[0]             # `open(X, "a", …)`
            if target is not None:
                literal = _ndjson_literal_of(target, consts)
                if literal:
                    appended.setdefault(literal, set()).add(label)
            if (isinstance(node.func, ast.Name)
                    and node.func.id == "_rotate_ndjson_tail" and node.args):
                literal = _ndjson_literal_of(node.args[0], consts)
                if literal:
                    rotated.setdefault(literal, set()).add(label)
    return appended, rotated


def _project_ndjson_modules() -> list:
    """要掃的原始碼：套件內的非測試模組 ＋ 根目錄的啟動器。

    `conftest.py` 在 2026-09-22 之前住在套件裡、在這個範圍內；搬到 `test/` 之後照舊
    列進來。"""
    import ast
    package = Path(b.__file__).resolve().parent
    repo = package.parent
    tests = Path(__file__).resolve().parent
    paths = [p for p in (*sorted(package.glob("*.py")), *sorted(tests.glob("*.py")))
             if not p.name.startswith(("test_", "_test_"))]
    paths += [repo / name for name in ("start_discord_bot.py",
                                       "start_webrunner.py", "run_batch.py",
                                       "install_autostart.py")]
    out = []
    for path in paths:
        if not path.exists():
            continue
        out.append((path.name,
                    ast.parse(path.read_text(encoding="utf-8"), str(path))))
    return out


def test_every_append_only_ndjson_log_is_bounded():
    """新增一個 append-only 的 ndjson 就必須選邊站——fail-closed 的那一半。

    `generate_history.ndjson` 是這條規則遲來的證據：`_spawn_webrunner` 那段註解
    宣告要輪替「所有 append-only 的 ndjson」，實際只列了兩個，而漏掉的那個一筆
    可以好幾百位元組（`main_prompt` / `char1` / `char2` / `undesired` 全存）。
    """
    appended, rotated = _classify_ndjson_logs(_project_ndjson_modules())
    unbounded = sorted(set(appended) - set(rotated) - set(_NDJSON_SELF_LIMITING))
    assert not unbounded, (
        "這些 ndjson 是 append-only 卻沒有上界："
        + str({n: sorted(appended[n]) for n in unbounded})
        + "。請在 `_spawn_webrunner` 的輪替區塊加一行 `_rotate_ndjson_tail(<常數>)`，"
          "或（只有在它自己會修剪的時候）把它加進 `test_bot_helpers."
          "_NDJSON_SELF_LIMITING` 並寫下**可驗證**的理由。無上界的 append-only "
          "紀錄檔不會壞掉、只會愈讀愈慢，所以沒有人會發現。")


def test_the_ndjson_exemption_list_has_no_stale_entries():
    """一個豁免必須有人證明它還在做它被豁免去做的事，否則它會活得比理由久。"""
    appended, rotated = _classify_ndjson_logs(_project_ndjson_modules())
    ghosts = sorted(set(_NDJSON_SELF_LIMITING) - set(appended))
    assert not ghosts, (
        f"{ghosts} 被豁免、但現在根本沒有人對它 append（改成整檔重寫了？檔案沒了？）。"
        "豁免的前提已經不成立，請把它從 `_NDJSON_SELF_LIMITING` 刪掉。")
    redundant = sorted(set(_NDJSON_SELF_LIMITING) & set(rotated))
    assert not redundant, (
        f"{redundant} 同時被豁免又被輪替，豁免已經是死條目——刪掉它，"
        "免得下一個人以為那個檔的上界靠的是自我修剪。")


def test_the_usage_ledger_exemption_still_earns_it():
    """豁免的機制檢查：append 完之後**真的**還接著修剪。

    只驗「`_dorossi_trim_usage_file` 這個函式還在」是不夠的——它可以還在、卻沒有
    任何人呼叫，而那樣的檔案照樣無限長大。所以驗的是那條線本身。
    """
    import ast
    tree = ast.parse(Path(db.__file__).read_text(encoding="utf-8"))
    recorder = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "_dorossi_record_usage"), None)
    assert recorder is not None, (
        "`_dorossi_record_usage` 不見了——`dorossi_usage.ndjson` 的豁免是靠它"
        "接著呼叫修剪才成立的，請重新確認上界並更新 `_NDJSON_SELF_LIMITING`。")
    called = {n.func.id for n in ast.walk(recorder)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_dorossi_trim_usage_file" in called, (
        "append 之後沒有再呼叫 `_dorossi_trim_usage_file` 了。"
        "`dorossi_usage.ndjson` 的豁免理由就是這條線，它斷了 ＝ 那個檔現在無上界，"
        "要嘛把修剪接回去，要嘛把它改成走 `_rotate_ndjson_tail`。")


def test_the_ndjson_scan_sees_every_shape_that_matters():
    """守門的自我檢查：比對條件被寫死成永不成立時，真實原始碼會照樣是「乾淨」的。

    所以先拿一段合成原始碼證明每一個判斷條件都真的會動——四個檔各自只踩一個
    條件，任何一條被拿掉都會有一個斷言變紅（兩道防護互相遮蔽是這一族守門反覆
    出現的病）。
    """
    import ast
    probe = ast.parse("\n".join([
        'A_FILE = PROJECT_ROOT / "a.ndjson"',    # append（屬性寫法）＋ 有輪替
        'B_FILE = PROJECT_ROOT / "b.ndjson"',    # append（open() 寫法）＋ 沒輪替
        'C_FILE = PROJECT_ROOT / "c.ndjson"',    # 整檔重寫 → 不算 append
        'D_FILE = PROJECT_ROOT / "d.json"',      # 不是 ndjson → 完全不看
        'def w():',
        '    with A_FILE.open("a", encoding="utf-8") as fh:',
        '        fh.write("x")',
        '    with open(B_FILE, "a", encoding="utf-8") as fh:',
        '        fh.write("x")',
        '    C_FILE.write_text("x", encoding="utf-8")',
        '    with D_FILE.open("a", encoding="utf-8") as fh:',
        '        fh.write("x")',
        'def r():',
        '    _rotate_ndjson_tail(A_FILE)',
    ]))
    appended, rotated = _classify_ndjson_logs([("probe", probe)])
    _eq(sorted(appended), ["a.ndjson", "b.ndjson"], "兩種 append 寫法都要看見")
    _eq(sorted(rotated), ["a.ndjson"], "輪替只認真的有被呼叫的那一個")

    # 整條管線真的產得出違規（否則「永遠沒有違規」也會是綠的）。
    unbounded = sorted(set(appended) - set(rotated) - set(_NDJSON_SELF_LIMITING))
    _eq(unbounded, ["b.ndjson"], "沒輪替又沒豁免的要被抓出來")

    # inline 形狀：常數繞過去也要被看見。
    inline = ast.parse("\n".join([
        'def w():',
        '    with (PROJECT_ROOT / "z.ndjson").open("a", encoding="utf-8") as fh:',
        '        fh.write("x")',
    ]))
    inline_appended, _ = _classify_ndjson_logs([("inline", inline)])
    _eq(sorted(inline_appended), ["z.ndjson"],
        "沒綁成常數的 append 一樣要看見")


# ---------------------------------------------------------------------------
# 輪替必須由**時間**驅動，不能只掛在 spawn 路徑上
#
# 在這之前每一次輪替都發生在 `_spawn_webrunner()`。那在這台機器上看不出問題
# （批次天天在跑），但那是**環境的性質，不是程式的性質**：一台只用 `/dorossi`
# 的機器從來不 spawn 背景產圖程式，於是一個檔案都不會被輪替。而自走迴圈正是把
# `dorossi_events.ndjson` 撐大最快的路徑——`usage_wait` 那條刻意不設次數上限。
#
# 所以下面刻意有**兩種**守門，而且缺一不可：AST 那支只證明「有寫這行程式碼」，
# 行為那支才證明「這條路真的走得到」。原本的缺陷就是「程式碼寫得好好的、但只
# 在某個不相干的條件成立時才會執行」。
# ---------------------------------------------------------------------------

def _rotate_calls_inside(func_name: str) -> set:
    """`<func_name>` 裡 `_rotate_ndjson_tail(<常數>)` 打到的檔名集合。"""
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    consts = _ndjson_constants_in(tree)
    target = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == func_name), None)
    assert target is not None, f"`{func_name}` 不見了。"
    out = set()
    for node in ast.walk(target):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_rotate_ndjson_tail" and node.args):
            literal = _ndjson_literal_of(node.args[0], consts)
            if literal:
                out.add(literal)
    return out


def test_every_spawn_rotated_log_is_also_rotated_on_a_timer():
    """spawn 那張手列清單與定時那張必須對得起來（`events.ndjson` 除外）。

    需求從**機制**推出來而不是再抄一張清單：凡是 spawn 路徑認為需要輪替的檔，
    定時路徑也要顧到，否則一台不 spawn 的機器就漏掉它。加第五個檔進 spawn 區塊
    卻忘了定時那半，這支會紅。
    """
    spawn = _rotate_calls_inside("_spawn_webrunner")
    assert spawn, "`_spawn_webrunner` 裡一個 `_rotate_ndjson_tail` 都找不到了。"
    # 比的是**基底檔名**：逐平台的狀態檔實際叫 `state/<平台>/<平台>.x.ndjson`，而
    # `spawn` 那一側是 AST 抽到的字面值。拿執行期的 `.name` 去對，兩邊永遠對不上。
    periodic = {p.name.split(".", 1)[-1]
                if p.name.startswith(b.ACTIVE_PLATFORM + ".") else p.name
                for p in b._PERIODIC_ROTATE_FILES}
    missing = sorted(spawn - {"events.ndjson"} - periodic)
    assert not missing, (
        f"{missing} 只在 spawn 路徑輪替。只用 `/dorossi` 的機器從來不 spawn，"
        "那些檔會無限長大而且完全沒有訊號——請把它加進 "
        "`discord_bot._PERIODIC_ROTATE_FILES`。")


def test_events_ndjson_is_deliberately_left_out_of_the_timer():
    """`events.ndjson` 的排除是決定，不是遺漏——所以要釘住，免得有人「順手補上」。

    兩個獨立理由，任一個成立就不能定時輪替它：
      1. 它唯一的寫端是**另一個行程**（背景產圖程式的 `emit_event`）。
         `_rotate_ndjson_tail` 是「讀尾段 → 整檔換掉」，中間被 append 進去的行
         會被消滅；掉一則 `single_image_done` 會讓 `/gen image` 的閘門卡到 600
         秒 TTL。spawn 那側安全是因為它跑在 teardown 與 spawn 之間。
      2. 只有它有 offset 追蹤（`_event_offset`）。檔案變小時 `_poll_events_once`
         會 reset 成 0，把保留的整段尾巴**重播**到 Discord。
    而排除它不花任何代價：不 spawn 的機器根本沒有人寫這個檔。
    """
    names = {p.name for p in b._PERIODIC_ROTATE_FILES}
    assert "events.ndjson" not in names, (
        "`events.ndjson` 被加進定時輪替了。它有跨行程的寫端，也有 offset 追蹤，"
        "定時輪替會吃掉事件並把保留的尾段重播到 Discord。理由見本測試的 docstring。")
    assert names, "定時輪替的清單空了——等於這個修正被整個拿掉。"


def test_the_time_driven_rotation_actually_runs_without_a_spawn(
        tmp_path, monkeypatch):
    """行為守門：**不 spawn** 也要真的輪替得到。

    這支才是那個缺陷本身。AST 那支只看得到「有人寫了 `_rotate_ndjson_tail(X)`」，
    看不到「那一行只在 `_spawn_webrunner` 裡」。所以這裡跑真正的
    `_daily_health_loop` 一輪，一次 spawn 都沒有。

    健康報告刻意設成 **disabled**：輪替與「有沒有要送報告」無關，縮排錯一格就會
    變成「關掉報告 ＝ 連輪替也一起關掉」——那正是同一個「只在某個不相干的條件
    成立時才做事」的病，只是換一個條件。
    """
    keep = b._NDJSON_ROTATE_KEEP_BYTES
    line = b"x" * 63 + b"\n"
    body = line * ((keep * 3) // len(line))     # 遠超過 2 倍 keep_bytes
    paths = []
    for name in ("audit.ndjson", "generate_history.ndjson",
                 "dorossi_events.ndjson"):
        path = tmp_path / name
        path.write_bytes(body)
        paths.append(path)
    # 用**真實**的門檻，不去 monkeypatch 它：`_rotate_ndjson_tail` 的 `keep_bytes`
    # 是預設引數，在 def 當下就綁定了，改模組常數對它沒有作用——照著改會得到一支
    # 「看起來設了門檻、其實沒設」的測試。
    monkeypatch.setattr(b, "_PERIODIC_ROTATE_FILES", tuple(paths))
    monkeypatch.setattr(b, "DAILY_HEALTH_REPORT", {"enabled": False})
    # 同一條迴圈也掛著每日的後端模型目錄檢查，那個會起真正的 CLI 子行程。
    monkeypatch.setattr(b, "DOROSSI_MODEL_CHECK", {"enabled": False})
    # 這條迴圈也跑單張請求的服務看門狗；它只在本行程有 inflight 時才碰磁碟，
    # 把那個前提釘成 None，並把請求檔導開，免得動到 repo root 的活檔案。
    monkeypatch.setattr(b, "_generate_inflight", None)
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE", tmp_path / "req.json")
    before = [q.stat().st_size for q in paths]

    async def _one_tick():
        # 迴圈是 `while True` + `await asyncio.sleep(60)`，所以第一輪的本體會先
        # 跑完。逾時是**必要**的上限：等真實時間的測試若沒有上限，一旦回歸就會
        # 變成整個回合卡住，而卡住比紅色更難查。
        try:
            await asyncio.wait_for(b._daily_health_loop(), timeout=2.0)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    asyncio.run(_one_tick())

    after = [q.stat().st_size for q in paths]
    assert all(a < before[i] for i, a in enumerate(after)), (
        f"定時輪替沒有真的發生（before={before} after={after}）。"
        "只在 spawn 路徑輪替的話，一台只跑 Dorossi 的機器永遠不會輪替。")
    assert all(a <= keep for a in after), f"輪替後仍超過保留量：{after}"


def test_the_periodic_rotation_still_refuses_to_touch_a_small_file(tmp_path,
                                                                  monkeypatch):
    """反方向：**還不夠大就一個位元組都不准動。**

    輪替是**破壞性**的（只留尾端 256 KB，前面的歷史全部消失），所以「什麼時候
    該輪替」判斷錯的代價是不可逆的。而定時那條路每分鐘醒一次——判錯的話不是出
    一次錯，是每分鐘再砍一次，直到有人發現檔案永遠只有幾行。

    只釘「夠大時要輪替」是不夠的：把 `_rotate_periodic_ndjson_logs` 改成繞過大小
    門檻（例如 `keep_bytes=1`）**實測會從所有既有守門裡活著出來**——正方向那支照
    樣綠（它只斷言「有變小」），`test_rotate_ndjson_tail` 也照樣綠（它直接測那支
    primitive，看不到定時路徑傳了什麼）。所以這一支專門守「兩個條件都成立才動
    手」裡的大小那一半。

    三個案例合在一起才有意義，尤其是最後那個：只驗「小檔沒被動」的話，「整個輪
    替被拿掉」也會通過——那是本專案一再踩到的「兩道防護互相遮蔽」。加上「剛好
    多一個位元組就要輪替」，才分得出「正確地按兵不動」與「根本不會動」。
    """
    keep = b._NDJSON_ROTATE_KEEP_BYTES
    line = b"x" * 63 + b"\n"                  # 64 bytes/line，整除 keep
    assert keep % len(line) == 0, "測資假設 keep_bytes 是行長的整數倍"

    small = tmp_path / "audit.ndjson"
    small.write_bytes(line * 8)               # 512 bytes，遠低於門檻

    # 邊界：`_rotate_ndjson_tail` 是 `size <= keep_bytes * 2` 才放過，所以「剛好
    # 兩倍」必須原封不動。這一格同時釘住那個 `<=` 不能被改成 `<`。
    exact = tmp_path / "generate_history.ndjson"
    exact.write_bytes(line * ((keep * 2) // len(line)))
    assert exact.stat().st_size == keep * 2

    # 剛好多一行 → 必須真的輪替。沒有這一格，上面兩格在「輪替被整個拿掉」時也會綠。
    over = tmp_path / "dorossi_events.ndjson"
    over.write_bytes(line * ((keep * 2) // len(line) + 1))

    before = {p: p.read_bytes() for p in (small, exact)}
    over_before = over.stat().st_size

    # 用**真實**門檻，不 monkeypatch `_NDJSON_ROTATE_KEEP_BYTES`：`keep_bytes` 是
    # 預設引數，在 def 當下就綁定了，改模組常數對它沒有作用。
    monkeypatch.setattr(b, "_PERIODIC_ROTATE_FILES", (small, exact, over))
    b._rotate_periodic_ndjson_logs()

    for path, original in before.items():
        assert path.read_bytes() == original, (
            f"{path.name} 還沒到門檻就被輪替了（{len(original)} bytes，門檻是 "
            f"{keep * 2}）。定時輪替每分鐘跑一次，繞過大小判斷等於每分鐘把歷史"
            "砍掉一次，而且不可逆。")
    assert over.stat().st_size < over_before, (
        "超過門檻的檔案沒有被輪替——那上面兩個「沒被動」不是因為門檻正確，"
        "而是因為輪替根本沒在做事。")


def test_the_timer_rotated_logs_are_only_appended_synchronously():
    """定時輪替能成立的**全部**理由：那三個檔的 append 端都是同步函式。

    `_rotate_ndjson_tail` 是「讀尾段 → 整檔換掉」。這對併發的 appender 並不安全，
    而 `events.ndjson` 被排除在定時輪替之外正是因為它的寫端在**另一個行程**。剩下
    三個之所以可以定時輪替，唯一的依據是它們由 bot 這個行程自己 append、而且那三支
    **是同步函式、內部沒有 await**——同一條 event loop 上，兩段沒有 await 的同步碼
    不可能交錯。

    所以只要有人把其中一支改成 `async def` 並在讀寫之間 await，輪替就能插進「讀尾
    段」與「整檔換掉」之間，把那段期間 append 的行消滅掉——跟排除 `events.ndjson`
    的成因一模一樣，只是寫端從別的行程換成自己。失敗形態一樣安靜：沒有例外、沒有
    log，只是資料少了幾行。這是那段註解裡最容易在別人改動 append 路徑時失效、而且
    失效之後完全沒有訊號的一句，所以用測試釘住而不是只寫在註解裡。
    """
    import ast
    src = Path(b.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    consts = _ndjson_constants_in(tree)
    # **比的是基底檔名，不是磁碟上的檔名。** 逐平台的狀態檔實際叫
    # `state/<平台>/<平台>.x.ndjson`，而 AST 那一側只看得到字面值 `x.ndjson`。拿
    # 執行期的 `.name` 去對，兩邊永遠對不上，而紅字會講成「找不到 append 端」——
    # 一個看起來像掃描器壞掉、其實只是命名規則的假訊號。
    targets = {p.name.split(".", 1)[-1] if p.name.startswith(b.ACTIVE_PLATFORM + ".")
               else p.name
               for p in b._PERIODIC_ROTATE_FILES}

    # 先把「誰對這幾個檔做 append」機械地找出來，不要手抄一張函式名清單——手抄的
    # 清單會在有人換掉 helper 名字的那天無聲過期（這段註解自己就寫錯過兩個名字）。
    owners = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "open"):
                continue
            literal = _ndjson_literal_of(inner.func.value, consts)
            modes = [a.value for a in inner.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if literal in targets and any("a" in m for m in modes):
                owners.setdefault(literal, set()).add(node)

    missing = sorted(targets - set(owners))
    assert not missing, (
        f"{missing} 在定時輪替清單裡，卻找不到任何 append 端——掃描器可能壞了，"
        "而壞掉的掃描器會讓這支測試對真正的問題保持全綠。")

    bad = []
    for name, funcs in sorted(owners.items()):
        for fn in funcs:
            if isinstance(fn, ast.AsyncFunctionDef) or any(
                    isinstance(x, (ast.Await, ast.AsyncFor, ast.AsyncWith))
                    for x in ast.walk(fn)):
                bad.append(f"{name} <- {fn.name}")
    assert not bad, (
        f"{bad} 是非同步的 append 端。定時輪替（`_rotate_periodic_ndjson_logs`）"
        "是讀尾段再整檔換掉，一旦 append 端會讓出控制權，中途寫進去的行就會被"
        "消滅。要嘛把它改回同步，要嘛把那個檔從 "
        "`discord_bot._PERIODIC_ROTATE_FILES` 拿掉（像 `events.ndjson` 那樣）。")


def test_the_periodic_rotation_survives_an_os_error(tmp_path, monkeypatch,
                                                    capsys):
    """它由背景迴圈呼叫，所以一次磁碟錯誤絕不能把那條迴圈帶走。

    迴圈死掉的外顯症狀是「狀態不再更新」，跟「現在沒事發生」分不出來——這正是
    本專案一再點名的那種靜默失效。
    """
    def _boom(_path, *_a, **_kw):
        raise OSError("disk on fire")

    monkeypatch.setattr(b, "_PERIODIC_ROTATE_FILES",
                        (tmp_path / "audit.ndjson",))
    monkeypatch.setattr(b, "_rotate_ndjson_tail", _boom)
    b._rotate_periodic_ndjson_logs()          # 不得拋
    assert "periodic rotate" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 拒絕寫入必須在**每一個**表面變成一句說得清楚的話
#
# `_safe_write` 讀不出原檔時會丟 `_UndoBackupUnavailable`（拒絕，而不是「照樣寫、
# 只印 stderr」——默默取消 undo 備份正是本專案一再點名的「宣稱做到了卻沒做到」）。
# 但「拒絕」只有在使用者**收得到理由**時才算完成：落到各表面的泛用 handler 會變成
# 「指令發生內部錯誤（_UndoBackupUnavailable）」，那句話沒有任何可行動的資訊，而
# 使用者真正需要知道的是「這次編輯沒有生效、原檔請改存成 UTF-8」。
# ---------------------------------------------------------------------------

_UNDO_REFUSAL_EXC = "_UndoBackupUnavailable"

# 動態派發的兩個表面：`_handle_mention` 從 dict 取 handler、`_slash_run` 收
# handler 當引數，所以**靜態呼叫圖看不到它們通往 `_safe_write`**。它們必須用列舉
# 的方式釘住；`@client.event` 那一半則是機械推出來的（見下面那支）。
_DYNAMIC_DISPATCHERS = {"_handle_mention", "_slash_run"}


def _bot_functions() -> dict:
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


# 把協程交給另一個 task 跑的呼叫。`_schedule_coro(f())` 裡的 `f()` 只是**建立**協程
# 物件，本體在另一個 task 裡執行——呼叫端的 `except` 永遠看不到它的例外。
_TASK_HANDOFF_CALLS = frozenset({
    "_schedule_coro", "create_task", "_start_supervised_task", "ensure_future"})


def _call_name(call) -> str | None:
    import ast
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _handed_off_calls(node) -> set:
    """`node` 裡被當成協程交給別的 task 的那些呼叫節點（的 id）。"""
    import ast
    out = set()
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and _call_name(sub) in _TASK_HANDOFF_CALLS
                and sub.args and isinstance(sub.args[0], ast.Call)):
            out.add(id(sub.args[0]))
    return out


def _task_bodies(funcs: dict) -> set:
    """被交給別的 task 跑的函式名（`_schedule_coro(f())`、`create_task(f())`…）。"""
    import ast
    out = set()
    for node in funcs.values():
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and _call_name(sub) in _TASK_HANDOFF_CALLS
                    and sub.args and isinstance(sub.args[0], ast.Call)
                    and isinstance(sub.args[0].func, ast.Name)):
                out.add(sub.args[0].func.id)
    return out


def _callers_reaching(funcs: dict, target: str, *,
                      through_handoff: bool = True) -> set:
    """靜態呼叫圖上能（遞移地）走到 `target` 的函式名。

    `through_handoff=False` 時，交給別的 task 的協程（`_TASK_HANDOFF_CALLS` 的第一
    個引數）不算一條邊——問「這個函式的 `except` 接不接得到」時要這樣問，因為那條
    路上的例外發生在另一個 task 裡。預設 True 維持原本的過度近似。"""
    import ast
    calls: dict = {}
    for name, node in funcs.items():
        called = set()
        skip = set() if through_handoff else _handed_off_calls(node)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and id(sub) not in skip:
                if isinstance(sub.func, ast.Name):
                    called.add(sub.func.id)
                elif isinstance(sub.func, ast.Attribute):
                    called.add(sub.func.attr)
        calls[name] = called
    reached = {target}
    changed = True
    while changed:
        changed = False
        for name, called in calls.items():
            if name not in reached and called & reached:
                reached.add(name)
                changed = True
    return reached - {target}


def _catches(funcs: dict, name: str, exc: str) -> bool:
    import ast
    node = funcs.get(name)
    if node is None:
        return False
    for sub in ast.walk(node):
        if not isinstance(sub, ast.ExceptHandler) or sub.type is None:
            continue
        types_ = (sub.type.elts if isinstance(sub.type, ast.Tuple)
                  else [sub.type])
        if any(ast.unparse(t) == exc for t in types_):
            return True
    return False


def _client_event_names(funcs: dict) -> set:
    import ast
    out = set()
    for name, node in funcs.items():
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Attribute) and dec.attr == "event"
                    and isinstance(dec.value, ast.Name)
                    and dec.value.id == "client"):
                out.add(name)
    return out


def test_safe_write_actually_refuses_instead_of_writing_anyway():
    """機制檢查：`_safe_write` 真的還在丟那個例外。

    只驗「各表面有接」是不夠的——把 `_safe_write` 裡的 `raise` 換成一行 `print`
    之後，每一個 except 分支都還在、每一支表面測試都還是綠的，而 undo 備份已經
    被默默放棄了。
    """
    import ast
    node = _bot_functions()["_safe_write"]
    raised = {ast.unparse(n.exc.func) for n in ast.walk(node)
              if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)}
    assert _UNDO_REFUSAL_EXC in raised, (
        f"`_safe_write` 不再丟 `{_UNDO_REFUSAL_EXC}` 了。備份拿不到卻照樣寫入 ＝ "
        "默默取消這一次的 undo，而使用者以為有——本專案一再點名的那種失敗。")


def test_every_surface_that_can_reach_safe_write_explains_the_refusal():
    """每一個走得到 `_safe_write` 的表面都要有自己的「拒絕」分支。

    `@client.event` 那一半是**機械**推出來的（靜態呼叫圖 ∩ 事件處理器），所以
    以後有人接一個新的事件、而它輾轉呼叫到佇列寫入，這支會自動要求它處理。
    動態派發的兩個表面靜態看不見，只能列舉——所以另外釘住它們存在（見下一支）。
    """
    funcs = _bot_functions()
    reaching = _callers_reaching(funcs, "_safe_write")
    events = _client_event_names(funcs)
    required = (events & reaching) | _DYNAMIC_DISPATCHERS
    missing = sorted(n for n in required
                     if not _catches(funcs, n, _UNDO_REFUSAL_EXC))
    assert not missing, (
        f"{missing} 走得到 `_safe_write`，卻沒有 `except {_UNDO_REFUSAL_EXC}`。"
        "它會落到那個表面的泛用 handler，使用者只看到「指令發生內部錯誤」——"
        "而實際發生的是「你的編輯沒有生效」，兩者的後續動作完全不同。")


def test_the_surface_scan_is_not_vacuous():
    """守門的自我檢查：上面那支的機械那一半必須真的有抓到東西。

    呼叫圖的建法一旦寫壞（例如只看 `ast.Name` 而漏掉方法呼叫），`events &
    reaching` 會變成空集合，而那支測試照樣全綠——「空清單上的斷言」是這一族守門
    反覆出現的病。所以這裡指名兩個**已知**會走到 `_safe_write` 的事件處理器。
    """
    funcs = _bot_functions()
    reaching = _callers_reaching(funcs, "_safe_write")
    events = _client_event_names(funcs)
    assert "on_message" in events & reaching, (
        "`on_message` 應該走得到 `_safe_write`（`!preset main set` 那一族）——"
        "抓不到表示呼叫圖壞了，上面那支會變成空轉的綠燈。")
    assert "on_raw_reaction_add" in events & reaching, (
        "`on_raw_reaction_add` 應該走得到 `_safe_write`"
        "（⭐/🗑️ → `_save_favorites`）。")
    for name in _DYNAMIC_DISPATCHERS:
        assert name in funcs, (
            f"`{name}` 不見了（改名？）。它是動態派發、靜態呼叫圖看不到，"
            "所以只能列舉——名字對不上就等於那個表面沒人守。")


# ---------------------------------------------------------------------------
# 監督者的存活時間必須用**單調**時鐘量
#
# `_watch_for_fallback` 量出來的 `alive_for` 同時驅動三個重啟決策：backoff 要不
# 要 reset、rapid-fail 計數要不要累加、以及那道「連續 N 次秒崩就放棄重啟」的閘。
# 拿可調整的牆上時鐘去量，兩個方向都會**靜默**壞掉，而且壞掉的正好是那道閘存在
# 的理由。下面兩支各釘一個方向。
# ---------------------------------------------------------------------------


class _FakeSupervisedProc:
    """一個「一 poll 就已經死了」的假子行程。

    真正的重點是 `on_poll`：監督者是在讀 `spawn_time` 與讀 `alive_for` 兩次之間
    呼叫 `poll()` 的，所以那裡是唯一能塞進「這一輪期間時鐘跳了」的位置。
    """

    def __init__(self, rc, on_poll):
        self.returncode = rc
        self._on_poll = on_poll

    def poll(self):
        self._on_poll()
        return self.returncode


class _WallClockShift:
    """讓 `time.time()` 每輪往前或往後跳，`time.monotonic()` 完全不碰。

    **不要**順手把 `time.monotonic` 也 patch 掉：`asyncio` 的事件迴圈就是拿它當
    自己的時鐘（`loop.time()`），餵它一個會跳的值會讓 `wait_for` 的逾時提早引爆
    或永遠不醒——測試會用一個跟被測行為無關的理由變紅，或者直接掛住。
    """

    def __init__(self, monkeypatch, wall_step):
        self.offset = 0.0
        self.wall_step = wall_step
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + self.offset)

    def step(self):
        self.offset += self.wall_step


def _run_supervisor_rounds(monkeypatch, clock, max_spawns, thresholds=None):
    """跑 `_watch_for_fallback`，回傳 (送出的訊息, 重生次數)。

    `max_spawns` 是**逃生口**：修好之前其中一條路徑會無限重生（rapid-fail 計數
    被時鐘跳躍一直歸零），沒有上限的話這支測試不會變紅、會直接掛住。假的 spawner
    超過上限就回 False，監督者看到 spawn 失敗就退出。
    """
    sent = []
    spawns = {"n": 0}

    class _Channel:
        async def send(self, content, **_kwargs):
            sent.append(content)

    # 簽章跟著真的那支走（keyword-only 的 `single_image_server`）。這裡不是可有可無
    # 的整潔：替身少一個參數的話，監督者重生會丟 `TypeError` 而不是重生，然後被上游
    # 的廣域 except 吞掉——測試量到「重生次數 0」，而好幾種期望值剛好就是 0。
    # 順手斷言它是 False：監督者重生的**必定**是批次，帶上旗標會讓每一次退避重生都
    # 變成單張伺服器，整條佇列從此不產圖。
    def _fake_spawn(_variant, *, single_image_server=False):
        assert single_image_server is False, (
            "監督者重生帶了單張伺服器旗標——重生出來的行程不會跑批次佇列。")
        spawns["n"] += 1
        if spawns["n"] > max_spawns:
            return False, "stop"
        b._webrunner_proc = _FakeSupervisedProc(1, clock.step)
        return True, "已啟動背景產圖程式"

    async def _fake_acquire(_label):
        return True

    monkeypatch.setattr(b, "_spawn_webrunner", _fake_spawn)
    monkeypatch.setattr(b, "_acquire_chrome_slot_for_respawn", _fake_acquire)
    monkeypatch.setattr(b, "_release_chrome_slot", lambda: None)
    monkeypatch.setattr(b, "_clear_pid", lambda: None)
    monkeypatch.setattr(b, "_alert_prefix", lambda: "")
    monkeypatch.setattr(b, "_alert_mentions", lambda: None)

    # 失敗時監督者會探一次網路；固定成「網路在」，否則結果取決於跑測試的機器當下
    # 有沒有網路（斷網那條路的測試在 `test_batch_recovery.py`）。
    async def _network_up():
        return False

    monkeypatch.setattr(b, "_batch_network_is_down", _network_up)
    monkeypatch.setattr(b, "KEEP_BOT_AWAKE", False)
    # backoff 壓到趨近於零：這幾支驗的是判斷，不是等待。留著預設值等於實跑
    # 75 秒。⚠️ **不可以寫 0.0**——`restart_backoff` 對 `minimum <= 0` 會丟
    # `ValueError`（那道檢查是故意的，見 `_supervisor`），而它會被 supervisor 的
    # blanket handler 吞掉，於是這幾支會以「監督機制異常結束」的形式壞掉，訊息
    # 看起來跟 backoff 毫無關係。`test_recovery_paths` 那一組同理用 0.01。
    monkeypatch.setattr(b, "WEBRUNNER_RESPAWN_BACKOFF_MIN_SEC", 0.001)
    monkeypatch.setattr(b, "WEBRUNNER_RESPAWN_BACKOFF_MAX_SEC", 0.001)
    for name, value in (thresholds or {}).items():
        monkeypatch.setattr(b, name, value)
    monkeypatch.setattr(b, "_webrunner_stop_requested", False)
    # "selenium" 而不是 "je"：這幾支要隔離的是 rapid-fail 那道閘，不是 je →
    # selenium 的啟動視窗（那條路有它自己的時鐘讀取）。
    monkeypatch.setattr(b, "_webrunner_variant", "selenium")
    monkeypatch.setattr(b, "_webrunner_proc",
                        _FakeSupervisedProc(1, clock.step))
    monkeypatch.setattr(b, "_webrunner_pid", None)

    async def _body():
        # `time.monotonic` 沒被動過，所以這個逾時是真的牆上 30 秒——只是最後一道
        # 保險，正常情況下迴圈是被 `max_spawns` 結束的。
        await asyncio.wait_for(b._watch_for_fallback(_Channel()), timeout=30)

    asyncio.run(_body())
    return sent, spawns["n"]


def test_a_clock_jump_forward_cannot_disarm_the_rapid_fail_giveup(monkeypatch):
    """牆上時鐘往前跳，不能把「開機就崩」洗成一次健康 run。

    情境：背景程式每輪都在兩秒內死掉（Chrome 根本開不起來），但期間 NTP 校時把
    系統時鐘往前撥了一小時。用 `time.time()` 量的話 `alive_for` ≈ 3600，
    `>= healthy_threshold` → backoff reset ＋ **rapid-fail 計數歸零**，於是那道
    「連續 N 次秒崩就放棄」的閘永遠不會響，變成無限重生一個秒崩的行程。
    """
    clock = _WallClockShift(monkeypatch, wall_step=3600.0)
    giveup = b.WEBRUNNER_RAPID_FAIL_GIVEUP_COUNT
    sent, spawns = _run_supervisor_rounds(
        monkeypatch, clock, max_spawns=giveup + 6)

    assert any("放棄重啟" in m for m in sent), (
        "時鐘往前跳之後 rapid-fail 放棄閘沒有觸發——存活時間被時鐘的跳躍灌大成"
        "一次「健康 run」，計數一直被歸零。間隔要用 `time.monotonic()` 量。")
    # 閘在第 giveup 輪響（那一輪不重生），所以重生只會發生 giveup-1 次。
    assert spawns == giveup - 1, (
        f"重生了 {spawns} 次，預期 {giveup - 1} 次；放棄閘沒有在該響的那一輪響。")


def _announced_waits(sent) -> list:
    """從監督者送出的重生通知裡撈出「將在 Ns 後重新啟動」的 N。

    這個通知在**等待之前**送出，所以測試讀得到序列而不必真的等完。
    """
    import re as _re
    out = []
    for message in sent:
        found = _re.search(r"將在 (\d+)s 後重新啟動", message)
        if found:
            out.append(int(found.group(1)))
    return out


def test_the_first_unhealthy_respawn_waits_the_minimum_not_double_it(monkeypatch):
    """第一次失敗要等 `MIN`，不是 `2 × MIN`。

    2026-09-11 之前這裡手抄了一份退避，而那份**先加倍再等**，所以第一次實際等的是
    `2 × MIN`。當時有三個地方說的是另一回事——本函式的 docstring（「5s 起跳」）、
    `/run` 的回覆（「{MIN}s 起跳」）、以及 `_run_supervisor_rounds` 自己的註解
    （「留著預設值等於實跑 75 秒」，而手抄版是 150 秒）。**三份文字一致、程式碼落單，
    就是程式碼錯**，所以這一支釘的是文件那一邊。

    實測序列：手抄版 `[10, 20, 40, 80]`（合計 150s 才放棄），
    `restart_backoff` `[5, 10, 20, 40]`（合計 75s）。

    MIN=1／MAX=100 是挑過的：兩種實作在**第一輪**就分得開（1 vs 2），而且通知是在
    等待之前送出的，所以這支測試的成本是兩輪的 1s＋2s，不是 15 秒。
    """
    clock = _WallClockShift(monkeypatch, wall_step=0.0)
    sent, _spawns = _run_supervisor_rounds(
        monkeypatch, clock, max_spawns=1,
        thresholds={"WEBRUNNER_RESPAWN_BACKOFF_MIN_SEC": 1.0,
                    "WEBRUNNER_RESPAWN_BACKOFF_MAX_SEC": 100.0})

    waits = _announced_waits(sent)
    assert waits[:2] == [1, 2], (
        f"宣告的等待序列是 {waits[:2]}，預期 [1, 2]。"
        "如果是 [2, 4]，代表退避又變成「先加倍再等」——第一次的 MIN 被跳過了，"
        "而 docstring 與 `/run` 的回覆都說是 MIN 起跳。")


def test_a_backwards_backoff_config_does_not_kill_the_supervisor(monkeypatch):
    """`max < min` 的設定不可以讓監督者整個死掉。

    `_bot_config` 對 `respawn_backoff_min_sec` / `respawn_backoff_max_sec` 是**各自**
    用 `_coerce_positive_num` 檢查的，沒有任何地方比對 max 與 min，所以
    `{min: 300, max: 5}` 是設定得出來的。而 `restart_backoff` 在 `maximum < minimum`
    時會丟 `ValueError`（那道檢查是故意的）。

    ⚠️ 那個例外會被監督者的 blanket handler 接住，於是**批次繼續跑，但從此沒有監督**
    ——不會有任何人被通知這件事。所以呼叫點要夾一次 `maximum`，而這一支就是在守那道夾子。
    夾在呼叫點而不是常數定義處，是為了讓這支測試 monkeypatch 那兩個常數之後仍然走得到它。
    """
    clock = _WallClockShift(monkeypatch, wall_step=0.0)
    giveup = b.WEBRUNNER_RAPID_FAIL_GIVEUP_COUNT
    sent, spawns = _run_supervisor_rounds(
        monkeypatch, clock, max_spawns=giveup + 6,
        thresholds={"WEBRUNNER_RESPAWN_BACKOFF_MIN_SEC": 0.01,
                    "WEBRUNNER_RESPAWN_BACKOFF_MAX_SEC": 0.001})

    assert any("放棄重啟" in m for m in sent), (
        "設定反了之後監督者沒有走到 rapid-fail 放棄閘——很可能是 `restart_backoff` "
        "丟了 `ValueError`、被 blanket handler 吞掉，於是批次失去監督而且沒人知道。"
        f"實際送出的訊息：{sent}")
    assert spawns == giveup - 1, (
        f"重生了 {spawns} 次，預期 {giveup - 1} 次。")


def test_no_hand_rolled_doubling_backoff_survives_anywhere():
    """靜態守門：全專案只准有**一份**「加倍＋封頂」的實作。

    2026-09-11 補的。起因是同一條規則當時有兩份實作——`_supervisor.restart_backoff`
    與 `_watch_for_fallback` 手抄的那份——而兩份的語意其實不一樣（見
    `test_the_first_unhealthy_respawn_waits_the_minimum_not_double_it`）。第三份就是
    這樣長出來的，所以把它結構性地擋住。

    判準：同一個函式裡出現 `min(<變數> * <常數>, …)`。**兩個條件都要**——只看
    「乘法 ＋ 常數」會誤報 `min(total, 7 * 86400.0)` 這種單位換算（實測 `dorossi_backend`
    就有一筆），要求其中一邊是變數才能把它排掉。實測收斂到剛好 2 筆：正典那一份
    ＋ 當時的違規者；修好之後只剩正典。

    ⚠️ 這一支跟上面那支行為測試**不是重複的**，各殺不同的變異：
      * 有人在新函式／新模組再抄第四份 → 只有這一支會紅。
      * 加倍順序被改回「先加倍再等」→ 只有那支行為測試會紅。
    """
    import ast
    trees = _project_module_asts()
    # 正對照組：抽不到檔案時「零筆違規」跟「全部乾淨」在輸出上一模一樣。
    assert len(trees) >= 25, f"只抽到 {len(trees)} 個模組，抽取器壞了"

    def _doubling_caps(tree):
        found = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "min"
                        and len(node.args) == 2):
                    continue
                for arg in node.args:
                    if not (isinstance(arg, ast.BinOp)
                            and isinstance(arg.op, ast.Mult)):
                        continue
                    sides = (arg.left, arg.right)
                    if (any(isinstance(s, (ast.Name, ast.Attribute))
                            for s in sides)
                            and any(isinstance(s, ast.Constant)
                                    and isinstance(s.value, (int, float))
                                    and s.value > 1 for s in sides)):
                        found.append((func.name, node.lineno,
                                      ast.unparse(node)))
        return found

    # 合成語料：偵測器真的咬得到嗎（而且不咬單位換算）？
    probe = ast.parse(
        "def hand_rolled(d, cap):\n"
        "    return min(d * 2, cap)\n"
        "def unit_conversion(total):\n"
        "    return min(total, 7 * 86400.0)\n"
        "def plain_cap(d, cap):\n"
        "    return min(d, cap)\n")
    hits = {name for name, _line, _src in _doubling_caps(probe)}
    assert hits == {"hand_rolled"}, (
        f"偵測器咬到的是 {hits}，預期只有 hand_rolled。"
        "咬到 unit_conversion 表示「要有變數運算元」那一半掉了；一個都沒咬到表示"
        "偵測器整個壞了，而那會讓下面的真實斷言永遠是綠的。")

    # 正典那一份是唯一的例外，而例外本身也要對帳（過期的例外是 fail-open）。
    canonical = ("_supervisor.py", "restart_backoff")
    offenders = []
    seen_canonical = False
    for name, tree in trees:
        for func, line, src in _doubling_caps(tree):
            if (name, func) == canonical:
                seen_canonical = True
                continue
            offenders.append(f"{name}:{line} in {func}()  {src}")
    assert seen_canonical, (
        f"在 {canonical[0]} 的 {canonical[1]}() 裡找不到正典那一份了——它改名／"
        "搬家／被改寫了嗎？例外名單指不到東西就等於這道守門少了一個方向。")
    assert not offenders, (
        "這些地方自己手抄了一份「加倍＋封頂」，請改呼叫 "
        "`_supervisor.restart_backoff`（注意它在 `maximum < minimum` 時會丟 "
        "`ValueError`，所以要在呼叫點夾一次）：\n  " + "\n  ".join(offenders))


def test_a_clock_jump_backward_cannot_fake_a_rapid_fail(monkeypatch):
    """牆上時鐘往後跳，不能把一個跑很久的健康 run 誣賴成秒崩。

    反方向：系統時鐘被往回撥了兩小時，用 `time.time()` 量出來的存活時間會是
    **負數** → `< rapid_fail_threshold` → 累加計數，累積幾輪就提早放棄一個其實
    正常的批次。

    兩道門檻設成 0 是刻意的：這樣「任何真的經過的時間」都算健康，只有**負數**
    ——也就是只有時鐘倒退才生得出來的值——會落進 rapid-fail。兩個時鐘因此被乾淨
    地分開，不必去猜這一輪實際跑了幾微秒。
    """
    clock = _WallClockShift(monkeypatch, wall_step=-7200.0)
    giveup = b.WEBRUNNER_RAPID_FAIL_GIVEUP_COUNT
    cap = giveup + 3
    sent, spawns = _run_supervisor_rounds(
        monkeypatch, clock, max_spawns=cap,
        thresholds={"WEBRUNNER_HEALTHY_THRESHOLD_SEC": 0.0,
                    "WEBRUNNER_RAPID_FAIL_THRESHOLD_SEC": 0.0})

    assert not any("放棄重啟" in m for m in sent), (
        "時鐘往後跳把一個健康的 run 算成 rapid fail，監督者提早放棄了一個其實"
        "正常的批次。間隔要用 `time.monotonic()` 量。")
    assert spawns == cap + 1, (
        f"只重生了 {spawns} 次；監督者應該一路重生到假 spawner 的上限為止。")


def test_no_wall_clock_interval_measurement_survives_anywhere():
    """靜態守門：**專案裡任何模組**都不准用可調整的時鐘量間隔。

    兩種形狀都掃——
      `t0 = time.time()` … `time.time() - t0`（存活時間）
      `deadline = time.time() + n` … `while time.time() < deadline`（等待）
    兩者都會被 NTP 校時／使用者改時鐘／換時區／虛擬機還原無聲地扭曲。

    **界線**：這條只針對「量間隔」。落地成磁碟時間戳、或要跟別的行程比對的絕對
    時間（事件檔的 `ts`、排程的 `when_ts`、`last_used`…）必須留在 `time.time()`
    ——`monotonic` 的零點每個行程都不一樣。所以守門只認上面那兩種**同一個函式內
    先取後減／先取後比**的形狀，而不是「禁用 `time.time()`」。

    **2026-09-09 從只掃 `discord_bot.py` 放寬成掃全部模組。** 規則本身跟模組無關
    ——同一段程式碼搬進 `_webrunner_shared` 就不再被守，那是範圍造成的漏洞，不是
    判準造成的。本專案自己的教訓第 3 條：**能結構性就不要列舉**。放寬時先量過：
    36 個非測試模組全部乾淨（0 筆），所以這是「趁乾淨把範圍鎖起來」，不是在修東西。

    測試檔**不在**範圍內：`test_webrunner_shared.py` 有兩處用牆鐘量「這個呼叫有沒有
    很快回來」，那是量測本身的一部分，形狀相同但風險不同（最壞是時鐘跳動讓某支測試
    偶發紅／綠，不是正式行為出錯）。混進來只會讓這支守門吵，而會吵的守門遲早被關掉。
    """
    import ast
    trees = _project_module_asts()
    # 正面對照組：抽不到檔案時，「零筆違規」跟「全部乾淨」在輸出上一模一樣。
    assert len(trees) >= 25, f"只抽到 {len(trees)} 個模組，抽取器壞了"

    def _is_wall(node):
        return (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"
                and not node.args and not node.keywords)

    def _offenders(root):
        out = []
        for fn in ast.walk(root):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            anchors, deadlines = {}, {}
            for node in ast.walk(fn):
                if not isinstance(node, ast.Assign):
                    continue
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if not names:
                    continue
                value = node.value
                # `x = time.time()`，以及 `x = time.time() if cond else None`
                candidates = ([value.body, value.orelse]
                              if isinstance(value, ast.IfExp) else [value])
                if any(_is_wall(c) for c in candidates):
                    for name in names:
                        anchors.setdefault(name, node.lineno)
                # `deadline = time.time() + n`
                if (isinstance(value, ast.BinOp)
                        and isinstance(value.op, ast.Add)
                        and (_is_wall(value.left) or _is_wall(value.right))):
                    for name in names:
                        deadlines.setdefault(name, node.lineno)
            for node in ast.walk(fn):
                if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)
                        and _is_wall(node.left)
                        and isinstance(node.right, ast.Name)
                        and node.right.id in anchors):
                    out.append((fn.name, node.lineno, node.right.id))
                if isinstance(node, ast.Compare) and (
                        _is_wall(node.left)
                        or any(_is_wall(c) for c in node.comparators)):
                    for inner in ast.walk(node):
                        if (isinstance(inner, ast.Name)
                                and inner.id in deadlines):
                            out.append((fn.name, node.lineno, inner.id))
        return out

    # 守門的自我檢查。真實原始碼現在是乾淨的，所以比對條件被改壞（例如把
    # `attr == "time"` 寫成永不成立）時，下面那句斷言照樣會過——守門被無聲拆掉。
    # 先拿一段合成原始碼證明兩種形狀都真的會命中，而且該放過的都放過。
    probe = ast.parse(
        "def bad_alive():\n"
        "    t0 = time.time()\n"
        "    return time.time() - t0\n"
        "def bad_wait():\n"
        "    deadline = time.time() + 5\n"
        "    while time.time() < deadline:\n"
        "        pass\n"
        # 條件式賦值也要抓——`_watch_for_fallback` 的 je 啟動視窗就長這樣，
        # 而它正是原始回報漏掉的那一處。
        "def bad_maybe(flag):\n"
        "    t0 = time.time() if flag else None\n"
        "    return time.time() - t0\n"
        "def good_alive():\n"
        "    t0 = time.monotonic()\n"
        "    return time.monotonic() - t0\n"
        "def good_absolute(when_ts):\n"
        "    record = {'ts': time.time()}\n"
        "    return record, when_ts - time.time()\n")
    hit_fns = sorted({fn for fn, _ln, _n in _offenders(probe)})
    assert hit_fns == ["bad_alive", "bad_maybe", "bad_wait"], (
        f"比對條件壞了：合成樣本應該只命中那三個 bad_*，實際 {hit_fns}。"
        "（`good_absolute` 拿的是磁碟／排程用的絕對時間戳，是對的，不准被抓。）")

    offenders = []
    for module_name, tree in trees:
        offenders.extend(
            (f"{module_name}:{fn}", ln, n) for fn, ln, n in _offenders(tree))
    assert not offenders, (
        "這些地方用可調整的牆上時鐘量間隔："
        + "、".join(f"`{fn}`（第 {ln} 行，錨點 `{n}`）" for fn, ln, n in offenders)
        + "。改用 `time.monotonic()`：`time.time()` 會被 NTP 校時、使用者改時鐘、"
          "換時區、虛擬機還原往前或往後撥，量出來的秒數兩個方向都會錯，而且完全"
          "無聲。要留在牆上時鐘的只有**會落地或跨行程比對**的絕對時間戳——那種"
          "不會長成上面兩個形狀。")


# ---------------------------------------------------------------------------
# fire-and-forget task：一定要走 `_schedule_coro`
#
# 這幾支釘的是「答應過的事沒發生，而且一個字都不印」那一類。背景 task 全是這種
# 承諾：計時器要 ping、按住的鍵鼠要放開、錄製要自己停、排程要真的跑。
# ---------------------------------------------------------------------------

def _discarded_task_offenders(trees):
    """`[(模組名, AST)]` → `[(模組名, 行號, 錨點)]`。

    掃描那一半（哪些呼叫算「丟掉了」）在 `_discarded_task_calls_in_tree`，聚合
    那一半在這裡——分開是因為兩半各自要有對照組：掃描器要證明它抓得到，聚合器
    要證明它會把**模組名**帶出來（不然多模組掃描的錯誤訊息說不出是哪個檔）。
    """
    out = []
    for module_name, tree in trees:
        out.extend((module_name, line, name)
                   for line, name in _discarded_task_calls_in_tree(tree))
    return out


def _discarded_task_calls(source: str):
    """同 `_discarded_task_calls_in_tree`，但吃原始碼字串（給對照組用）。"""
    import ast
    return _discarded_task_calls_in_tree(ast.parse(source))


def _discarded_task_calls_in_tree(tree):
    """`create_task` / `ensure_future` 的回傳值被直接丟掉的位置。

    判準是 AST 上的 `Expr` 陳述式，值就是那個呼叫本身——也就是「叫了、然後什麼
    都沒接」。`task = ...` / `pool.add(asyncio.create_task(...))` /
    `await asyncio.create_task(...)` 都不算，那些都留著參考或當場等它。
    """
    import ast
    names = {"create_task", "ensure_future"}
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr in names:
            hits.append((node.lineno, func.attr))
        elif isinstance(func, ast.Name) and func.id in names:
            hits.append((node.lineno, func.id))
    return hits


def test_no_background_task_is_created_without_keeping_a_reference():
    """裸的 `asyncio.create_task(x())` 一律不准，走 `_schedule_coro`。

    理由不是「怕被 GC」——那條 2026-08-30 實測沒重現，完整說明寫在 `_BG_TASKS`
    上面。留著這道門是為了**例外的能見度**：沒人取走的例外只會在 GC 時由 asyncio
    記一行泛用的 'Task exception was never retrieved'，看不出是哪個功能死了；
    `_bg_task_done` 會當場印出帶名字的一行。同一個池子也讓「有哪些背景工作還掛
    著」變成看得到的東西。

    **掃的是專案裡每一個非測試模組，不只 bot。** 原本這支只讀
    `inspect.getsource(b)`，可是規則本身跟模組無關——沒人取走的例外在哪個檔裡都
    一樣看不到。`dorossi_backend.py` 同樣會開背景 task，卻整個在守門範圍外。
    補救的**手段**確實是 bot 專屬的（`_schedule_coro` 住在 `discord_bot.py`，而
    模組邊界不准別的模組 import bot），但「不准把回傳值丟掉」這一半是通用的，
    所以先把通用的那一半擴到全專案，錯誤訊息再依模組給出對應的補救方式。
    """
    trees = _project_module_asts()
    assert len(trees) >= 25, (
        f"只抽到 {len(trees)} 個模組，抽取器壞了——抽不到檔案時「零筆違規」跟"
        "「全部乾淨」在輸出上長得一模一樣。")
    offenders = _discarded_task_offenders(trees)
    assert offenders == [], (
        "這些地方把 task 的回傳值丟掉了："
        + "、".join(f"`{mod}` 第 {line} 行（`{name}`）"
                    for mod, line, name in offenders)
        + "。`discord_bot.py` 裡改用 `_schedule_coro(coro, label=\"...\")`"
          "——它會放進 `_BG_TASKS`、掛上 `_bg_task_done`，task 炸掉時才會有一行"
          "帶名字的 stderr。其他模組不准 import bot（模組邊界），所以自己留住 "
          "handle，並在收尾時 `cancel()` ＋ "
          "`await asyncio.gather(t, return_exceptions=True)` 把它收乾淨。")


def test_the_reference_scan_would_actually_catch_a_bare_create_task():
    """反面：掃描器本身要真的抓得到，不然上面那支是永遠會綠的裝飾。"""
    bad = (
        "import asyncio\n"
        "async def f():\n"
        "    asyncio.create_task(g())\n"
        "    client.loop.create_task(h())\n"
        "    kept = asyncio.create_task(i())\n"
        "    pool.add(asyncio.create_task(j()))\n"
        "    await asyncio.ensure_future(k())\n"
    )
    hits = _discarded_task_calls(bad)
    assert [line for line, _ in hits] == [3, 4], (
        f"掃描器抓到 {hits}；應該只抓第 3、4 行（被丟掉的那兩個），"
        "assign / 放進容器 / 當場 await 都不算。")

    # 聚合器那一半也要有對照組：它必須把模組名帶出來，而且要能吃**任何**模組的
    # AST，不是只認得 bot。今天全專案零違規，所以真實資料證不了這件事。
    import ast
    agg = _discarded_task_offenders([("some_other_module.py", ast.parse(bad))])
    assert agg == [("some_other_module.py", 3, "create_task"),
                   ("some_other_module.py", 4, "create_task")], (
        f"聚合器輸出 {agg}；應該把模組名、行號、錨點三樣都帶齊。")


def test_the_discarded_task_rule_is_enforced_outside_the_bot_module():
    """反面：守門的**範圍**要真的比 `discord_bot.py` 寬。

    這支釘的不是規則而是範圍。上面那支今天是零違規，所以把掃描縮回只讀
    `discord_bot.py` 它照樣全綠——一支永遠會過的守門，和一支範圍正確的守門，
    在輸出上看不出差別。這裡改成量「掃描範圍裡實際有幾個模組在開背景 task」，
    範圍一縮就會紅。
    """
    import ast
    names = {"create_task", "ensure_future"}
    users = set()
    for module_name, tree in _project_module_asts():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = (func.attr if isinstance(func, ast.Attribute) else
                    func.id if isinstance(func, ast.Name) else "")
            if attr in names:
                users.add(module_name)
                break
    assert "discord_bot.py" in users, (
        f"抽取器壞了：bot 一定有背景 task，實際抽到 {sorted(users)}。")
    assert users - {"discord_bot.py"}, (
        "掃描範圍裡除了 `discord_bot.py` 之外，沒有任何模組在開背景 task。"
        "要嘛 `_project_module_asts()` 被縮窄了，要嘛開背景 task 的模組被搬走"
        "了；兩種都要人看一眼，因為上面那支守門會因此退化成永遠會過。")


def test_schedule_coro_keeps_the_task_until_it_finishes():
    """排上去的 task 要進池子、帶著看得懂的名字，跑完自己離開。"""
    async def _body():
        seen = {}

        async def _work():
            seen["ran"] = True

        b._schedule_coro(_work(), label="probe-label")
        names = {t.get_name() for t in b._BG_TASKS}
        assert "probe-label" in names, (
            f"task 沒進池子或沒帶名字：{names}。名字只在它炸掉時看得到，"
            "所以要能一眼認出是哪個功能。")
        for _ in range(5):
            await asyncio.sleep(0)
        assert seen.get("ran") is True, "task 根本沒跑"
        assert not [t for t in b._BG_TASKS if t.get_name() == "probe-label"], (
            "跑完了還留在池子裡——那會變成一個只進不出的洩漏。")

    asyncio.run(_body())


def test_a_crashed_background_task_names_itself_on_stderr():
    """背景 task 炸掉要**當場**印出帶名字的一行，而且不得往外拋。

    對照組是 asyncio 自己的行為：例外沒人取的話，只會在 GC 時由 loop 的 handler
    記一句泛用的 'Task exception was never retrieved'——時間點不確定、也不會說是
    哪個功能。這支順便確認例外真的被取走了（loop handler 不該再收到）。
    """
    import contextlib
    import io

    async def _body():
        handled = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, ctx: handled.append(ctx))

        async def _boom():
            raise RuntimeError("kaboom-marker")

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            b._schedule_coro(_boom(), label="crash-probe")
            for _ in range(6):
                await asyncio.sleep(0)
        return buffer.getvalue(), handled

    text, handled = asyncio.run(_body())
    assert "crash-probe" in text, (
        f"stderr 沒有 task 名字：{text!r}。沒名字的話跟 asyncio 預設那句"
        "泛用訊息一樣沒用。")
    assert "kaboom-marker" in text, f"stderr 沒有例外內容：{text!r}"
    assert not handled, (
        f"例外沒被取走，loop 的 handler 還是收到了：{handled}。"
        "`_bg_task_done` 應該呼叫 `task.exception()` 把它取走。")


def test_scheduling_without_a_running_loop_closes_the_coroutine():
    """沒有 running loop 時（import / 測試環境）要把 coroutine 關掉。

    放著不管會在 GC 時噴 'coroutine was never awaited' RuntimeWarning——那是一句
    看起來像 bug 的雜訊，而這條路是刻意的。
    """
    import warnings

    async def _work():
        return 1

    coro = _work()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        b._schedule_coro(coro)          # 沒有 running loop
        del coro
        import gc
        gc.collect()
    never_awaited = [w for w in caught
                     if "never awaited" in str(w.message)]
    assert not never_awaited, (
        f"coroutine 沒被關掉，噴了 {[str(w.message) for w in never_awaited]}")


# ---------------------------------------------------------------------------
# 每次叫用的 token 用量：來源要跟金額對得起來
#
# 頂層 `usage` 只算主模型，`total_cost_usd` 卻是**所有**模型的總和。2026-08-30 用
# 真的 CLI 量到一次一般問答輪：`usage.input_tokens` = 2，而 `modelUsage` 裡除了主
# 模型還有一個小模型吃掉 897 input / 9 output、花掉 $0.000942，金額把兩者都算了。
# 只讀頂層 `usage` 的話，`dorossi_usage.ndjson` 那份紀錄的用途（拿 token 對帳金額、
# 分辨便宜的 cache 命中與昂貴的 cache 重建）從一開始就成立不了。
# ---------------------------------------------------------------------------

# 2026-08-30 從真的 CLI 抓下來的一則 result 事件（只留這裡用得到的欄位）。
_REAL_RESULT_EVENT = {
    "type": "result", "subtype": "success", "is_error": False,
    "total_cost_usd": 0.071011,
    "usage": {"input_tokens": 2, "cache_creation_input_tokens": 5942,
              "cache_read_input_tokens": 21078, "output_tokens": 4},
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 897, "outputTokens": 9, "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0, "costUSD": 0.000942},
        "claude-opus-5[1m]": {
            "inputTokens": 2, "outputTokens": 4, "cacheReadInputTokens": 21078,
            "cacheCreationInputTokens": 5942, "costUSD": 0.070069},
    },
}


def test_round_info_counts_every_model_because_the_cost_does():
    """`modelUsage` 優先：金額算了所有模型，token 也必須算所有模型。

    只讀頂層 `usage` 的話這一輪會記成 `in=2`，但實際送進去的是 899——差 450 倍，
    而且是**安靜的**：那一列看起來完全正常，只是拿它算「每塊錢多少 token」會得到
    離譜的數字。
    """
    info = db._dorossi_cc_round_info(_REAL_RESULT_EVENT)
    _eq(info["in"], 899, "fresh input = 897 + 2（兩個模型都要算）")
    _eq(info["out"], 13, "output = 9 + 4")
    _eq(info["cr"], 21078, "cache read")
    _eq(info["cc"], 5942, "cache creation")
    _eq(info["cost_usd"], 0.071011, "cost_usd 不變（B2 的壓縮觸發依賴它）")
    # 金額本身就是每模型 costUSD 的總和——這正是 token 也要跟著加總的理由。
    per_model = sum(m["costUSD"]
                    for m in _REAL_RESULT_EVENT["modelUsage"].values())
    assert abs(per_model - info["cost_usd"]) < 1e-9, (
        f"金額 {info['cost_usd']} 不等於每模型加總 {per_model}——那這條理由就不成立，"
        "要重新確認資料形狀。")


def test_round_info_falls_back_to_the_top_level_usage():
    """`modelUsage` 缺席時退回頂層 `usage`，行為與改動前一致。"""
    without = {k: v for k, v in _REAL_RESULT_EVENT.items() if k != "modelUsage"}
    _eq(db._dorossi_cc_round_info(without),
        {"cost_usd": 0.071011, "in": 2, "cr": 21078, "cc": 5942, "out": 4},
        "fallback 保持原本的解析")


def test_model_usage_totals_are_defensive():
    """壞形狀不得炸掉，也不得被算進去。整塊不可用就回 None（讓呼叫端退回 usage）。"""
    _eq(db._dorossi_model_usage_totals({}), None, "沒有 modelUsage -> None")
    _eq(db._dorossi_model_usage_totals({"modelUsage": {}}), None, "空 dict -> None")
    _eq(db._dorossi_model_usage_totals({"modelUsage": []}), None, "不是 dict -> None")
    _eq(db._dorossi_model_usage_totals({"modelUsage": {"m": "nope"}}), None,
        "每一筆都不是 dict -> None")
    # bool 是 int 的子類別，不得被當成 token 數。
    _eq(db._dorossi_model_usage_totals(
        {"modelUsage": {"m": {"inputTokens": True, "outputTokens": 5}}}),
        {"in": 0, "cr": 0, "cc": 0, "out": 5}, "bool guard")


def test_round_info_says_so_when_there_is_cost_but_no_tokens(capsys):
    """有花費卻一個 token 都讀不到 → 印一行診斷，不要安靜記 0。

    安靜記 0 的下場已經發生過：`dorossi_usage.ndjson` 56 筆新格式裡有 16 筆是
    「cost>0、四個 token 欄位全 0」，佔 10.6% 的花費，而沒有任何線索指出是哪一種
    result 事件造成的。診斷只印**鍵名**與 subtype，不印值——`result` 欄位裡什麼都
    可能有，而這一行會進 log。
    """
    info = db._dorossi_cc_round_info(
        {"total_cost_usd": 2.0, "subtype": "success",
         "result": "使用者的原始文字，不該被印出來"})
    _eq(info["cost_usd"], 2.0, "金額照記")
    err = capsys.readouterr().err
    assert "no usage" in err, f"沒印診斷：{err!r}"
    assert "subtype" in err, f"診斷沒說是哪一種 result 事件：{err!r}"
    assert "使用者的原始文字" not in err, (
        f"診斷把 result 的內容印出來了：{err!r}。那一行會進 log，只該有鍵名。")


def test_the_zero_token_diagnostic_carries_the_stderr_tail(capsys):
    """「有花費卻讀不到 token」那條診斷要一起帶出子行程的 stderr 尾巴。

    這是為了一個還沒破的案子：`dorossi_usage.ndjson` 有 16/56 筆是「cost>0、四個
    token 欄位全 0」，成因不明。所有**失敗**路徑（沉默砍、硬砍、閒置砍、rc!=0）
    早就會印 stderr 尾巴——只有**成功**那一輪把它丟掉，而這個異常剛好只出現在成功
    的那一輪。也就是說唯一可能有線索的一輪，正好是唯一不看 stderr 的一輪。
    """
    db._dorossi_cc_round_info(
        {"total_cost_usd": 2.0, "subtype": "success"},
        stderr_tail="cli-warning-marker")
    err = capsys.readouterr().err
    assert "cli-warning-marker" in err, (
        f"stderr 尾巴沒被帶進診斷：{err!r}")


def test_the_stderr_tail_is_truncated(capsys):
    """尾巴要截斷：子行程的 stderr 可以很長，而這行會進 log。"""
    db._dorossi_cc_round_info(
        {"total_cost_usd": 2.0}, stderr_tail="x" * 5000)
    err = capsys.readouterr().err
    assert len(err) < 1200, f"診斷太長（{len(err)} 字），沒截斷"
    assert "xxx" in err, "截斷過頭，什麼都沒留下"


def test_the_stderr_tail_never_shows_up_on_a_normal_round(capsys):
    """反面：正常的一輪不得因為有 stderr 就印東西。"""
    db._dorossi_cc_round_info(_REAL_RESULT_EVENT, stderr_tail="noise")
    assert capsys.readouterr().err == ""


# --- CLI 斜線指令那一輪（「有花費、0 token」的成因，2026-08-30 破案）-----------
#
# 帳本裡那些「cost>0、四個 token 欄位全 0」的資料點是自走迴圈的 `/compact` 維護輪。
# 兩條證據：67 筆裡 19 筆長這樣，而且 **19 筆全部**緊接在一筆正常工作輪之後（0 個
# 例外，中位間隔 184 秒、花費中位數是前一輪的 0.13 倍）；`dorossi_loop_compact_cost_usd`
# 是 10.0，而那段期間幾乎每一輪都超過，所以每個工作輪後面都跟著一輪 `/compact`。
#
# 然後**實跑兩次**把 result 事件抓下來看（見 `_COMPACT_RESULT_EVENT`）：`/compact`
# 那一輪**有** `usage` 這個鍵，但裡面五個數字全是 0；真正的數字只在 `modelUsage`。
# 先前記為「已排除 /compact——實測 usage 完整」的判斷只看了鍵在不在、沒看值。
#
# 本檔今天改成優先讀 `modelUsage` 之後這一類就讀得到了，所以**診斷不該對維護輪閉嘴**
# ——閉嘴會蓋掉「連 modelUsage 都讀不到」這個真正的回歸。標記的用途是把維護輪的花費
# 跟工作輪分開（實測那段期間壓縮佔總花費 9.9%），不是消音。

# 實測捕捉到的 `/compact` result 事件（兩次獨立執行，形狀與數字一致）。
_COMPACT_RESULT_EVENT = {
    "type": "result", "subtype": "success", "is_error": False,
    "total_cost_usd": 0.06089749999999999,
    # `usage` 這個鍵**在**，值全是 0——這正是先前誤判「已排除 /compact」的地方。
    "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0, "output_tokens": 0,
              "output_tokens_details": {"thinking_tokens": 0}},
    "modelUsage": {"claude-opus-5[1m]": {
        "inputTokens": 2063, "outputTokens": 1661,
        "cacheReadInputTokens": 18115, "cacheCreationInputTokens": 0,
        "costUSD": 0.06089749999999999}},
}


def test_a_real_compact_round_is_accounted_from_model_usage():
    """實測形狀的回歸釘：`/compact` 那一輪的 `usage` 全是 0，數字只在 `modelUsage`。

    這支測試就是那 19 筆帳的答案。線上 bot 是 2026-08-26 起的行程、還沒載到
    「優先讀 `modelUsage`」那段，所以舊資料仍是 0；重啟之後就不該再出現。
    """
    info = db._dorossi_cc_round_info(_COMPACT_RESULT_EVENT)
    _eq(info["in"], 2063, "fresh input 沒從 modelUsage 讀到")
    _eq(info["out"], 1661, "output 沒從 modelUsage 讀到")
    _eq(info["cr"], 18115, "cache read 沒從 modelUsage 讀到")
    _eq(info["cc"], 0, "cache creation")
    _eq(round(info["cost_usd"], 6), 0.060897, "花費")


def test_the_zeroed_usage_block_is_not_what_gets_believed(capsys):
    """反面：頂層 `usage` 全 0 而 `modelUsage` 有值時，不得採信前者、也不得報異常。"""
    db._dorossi_cc_round_info(_COMPACT_RESULT_EVENT)
    _eq(capsys.readouterr().err, "",
        "維護輪被誤報成異常了——`modelUsage` 明明讀得到")


def test_a_compact_round_with_no_numbers_anywhere_is_still_an_anomaly(capsys):
    """把 `modelUsage` 拿掉就只剩全 0 的 `usage`——那是真的回歸，必須出聲，
    **就算它是一輪 `/compact` 也一樣**。標記的用途是分類，不是消音。"""
    broken = dict(_COMPACT_RESULT_EVENT)
    broken.pop("modelUsage")
    db._dorossi_cc_round_info(broken, cli_command="compact")
    assert "no usage" in capsys.readouterr().err, (
        "維護輪被消音了——那樣就看不出 `modelUsage` 解析壞掉")

@pytest.mark.parametrize("prompt,expected", [
    ("/compact 請保留以下脈絡以便無縫接續", "compact"),
    ("/compact", "compact"),
    ("  /clear  ", "clear"),
    ("/Compact X", "compact"),          # 指令名大小寫不敏感
    ("hello /compact", ""),             # 不在開頭就不是 CLI 指令
    ("normal question", ""),
    ("/", ""),
    ("//x", ""),
    ("/compact/nested", ""),            # 後面不是空白也不是結尾
    ("", ""),
])
def test_a_cli_command_prompt_is_recognised(prompt, expected):
    _eq(db._dorossi_cli_command_of(prompt), expected, f"prompt={prompt!r}")


@pytest.mark.parametrize("prompt", [None, 123, [], {}])
def test_a_non_string_prompt_is_not_a_cli_command(prompt):
    """永不 raise：這條在每一次 `_dorossi_via_claude_code` 收尾時都會跑。"""
    _eq(db._dorossi_cli_command_of(prompt), "", f"prompt={prompt!r}")


def test_a_cli_command_round_is_labelled_and_still_billed(capsys):
    info = db._dorossi_cc_round_info(
        _COMPACT_RESULT_EVENT, cli_command="compact")
    _eq(info.get("kind"), "compact", "維護輪要標記")
    _eq(round(info["cost_usd"], 6), 0.060897, "維護輪的花費照樣要記")
    capsys.readouterr()


def test_a_normal_round_is_never_labelled():
    info = db._dorossi_cc_round_info(_REAL_RESULT_EVENT)
    assert "kind" not in info, f"一般工作輪不該有標記：{info!r}"


def test_the_label_lands_in_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE",
                        tmp_path / "dorossi_usage.ndjson")
    db._dorossi_round_info_and_record(
        {"total_cost_usd": 2.0}, cli_command="compact")
    db._dorossi_round_info_and_record(_REAL_RESULT_EVENT)
    rows = db._dorossi_read_usage(10)
    _eq(len(rows), 2, "兩列都要寫進去")
    _eq(rows[0].get("k"), "compact",
        "維護輪沒標記——那一列就又變回一筆無法解釋的『有花費、0 token』")
    assert "k" not in rows[1], f"一般工作輪不該有 k 欄位：{rows[1]!r}"
    _eq(rows[0]["cost_usd"], 2.0, "維護輪的花費照樣要記帳")


def test_a_hostile_label_cannot_bloat_the_ledger(tmp_path, monkeypatch):
    """`kind` 進得了磁碟，所以長度要有上限——正常路徑已由 regex 限死，
    這裡守的是「有人繞過 regex 直接塞 info」。"""
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE",
                        tmp_path / "dorossi_usage.ndjson")
    db._dorossi_record_usage({"cost_usd": 1.0, "kind": "x" * 5000})
    row = db._dorossi_read_usage(1)[0]
    assert len(row["k"]) <= 32, f"標記沒截斷：{len(row['k'])} 字"


def test_a_non_string_label_is_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE",
                        tmp_path / "dorossi_usage.ndjson")
    db._dorossi_record_usage({"cost_usd": 1.0, "kind": 123})
    assert "k" not in db._dorossi_read_usage(1)[0]


def test_the_recording_wrapper_passes_the_cli_command_through(
        tmp_path, monkeypatch):
    """包裝函式漏掉這個關鍵字＝帳本上的維護輪跟工作輪又長得一樣了。"""
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE",
                        tmp_path / "dorossi_usage.ndjson")
    info = db._dorossi_round_info_and_record(
        _COMPACT_RESULT_EVENT, stderr_tail="noise", cli_command="compact")
    _eq(info.get("kind"), "compact", "包裝函式沒把 cli_command 傳下去")
    _eq(db._dorossi_read_usage(1)[0].get("k"), "compact", "標記沒落到帳本上")


def test_both_return_paths_label_their_round():
    """兩個 `_dorossi_round_info_and_record` 呼叫端都要帶 `cli_command`。

    一個是預算閘 graceful 收尾、一個是 rc==0 的正常收尾；漏掉任一個，那條路的維護輪
    就會退回成無法解釋的資料點。用 AST 檢查而不是字串比對——參數順序換了不該紅。
    """
    import ast

    source = (Path(__file__).resolve().parent.parent / "axiomatic" / "dorossi_backend.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_dorossi_round_info_and_record"
    ]
    assert calls, (
        "整個 `dorossi_backend.py` 都找不到 `_dorossi_round_info_and_record` "
        "的呼叫——維護輪的帳完全不會被記下來了")
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "cli_command" in kwargs, (
            f"第 {call.lineno} 行的呼叫沒帶 `cli_command`——那條路的 `/compact` "
            "維護輪會退回成一筆無法解釋的『有花費、0 token』資料點")

    # **預算閘那條路必須走得到記錄。**
    #
    # 原本這裡寫的是 `len(calls) == 2`，因為當時「rc==0 正常收尾」與「預算閘
    # graceful 收尾」是兩個各自呼叫記錄的 return。2026-09-08 的重構把判定抽成純
    # 函式 `_claude_stream_verdict`（raise 或回傳 `"ok"`／`"budget"`），兩種正常
    # 收尾因此走**同一條** return——呼叫端剩一個，而數量守門就誤紅了。
    #
    # 數量從來不是重點，**「預算那條路有沒有被記錄」才是**。併成一條之後，只要有人
    # 把 verdict 對 budget 改成 `raise`，那條路就會繞過記錄直接往上拋，而呼叫端
    # 數量**完全看不出來**。所以改成直接釘那個行為。
    verdict = next((n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "_claude_stream_verdict"), None)
    assert verdict is not None, (
        "找不到 `_claude_stream_verdict`——這支測試綁在它身上，改名了要一起改")
    returns_budget = any(
        isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
        and n.value.value == "budget"
        for n in ast.walk(verdict))
    assert returns_budget, (
        "`_claude_stream_verdict` 不再回傳 `\"budget\"` 了。如果預算閘那條路改成"
        "拋例外，它就會**繞過** `_dorossi_round_info_and_record`，那一輪的花費不會"
        "進帳本——而這正是把兩個 return 併成一個之後，唯一還會出錯的方向。")


def test_no_stderr_means_no_empty_stderr_clause(capsys):
    """沒有 stderr 時不要印一段空的「stderr tail: ''」。

    看起來只是版面問題，實際上會誤導：讀 log 的人看到那一句，會以為子行程真的
    什麼都沒說，而不是「這一輪根本沒把 stderr 傳進來」。這兩件事要查的方向不同。
    """
    db._dorossi_cc_round_info({"total_cost_usd": 2.0})
    err = capsys.readouterr().err
    assert "cost" in err, "前提檢查：這一輪本來就該印診斷"
    assert "stderr tail" not in err, (
        f"沒有 stderr 卻印了一段空的尾巴：{err!r}")


def test_the_recording_wrapper_passes_the_stderr_tail_through(capsys,
                                                              tmp_path,
                                                              monkeypatch):
    """`_dorossi_round_info_and_record` 要把 stderr 尾巴往下傳。

    正式路徑上沒有人直接叫 `_dorossi_cc_round_info`——兩個 return 都走這個包裝。
    只測裡層的話，包裝把參數吃掉這種改動不會被抓到，而那等於整條線索又斷了。
    """
    monkeypatch.setattr(db, "DOROSSI_USAGE_FILE", tmp_path / "usage.ndjson")
    capsys.readouterr()
    db._dorossi_round_info_and_record(
        {"total_cost_usd": 2.0, "subtype": "success"},
        stderr_tail="wrapper-marker")
    err = capsys.readouterr().err
    assert "wrapper-marker" in err, (
        f"包裝沒把 stderr 尾巴傳下去：{err!r}")


def test_round_info_stays_quiet_on_a_normal_round(capsys):
    """反面：正常的一輪不得印診斷，否則那行變成雜訊、沒人會再看它。"""
    db._dorossi_cc_round_info(_REAL_RESULT_EVENT)
    db._dorossi_cc_round_info({})            # 完全沒事件：cost 也是 0，不算異常
    assert capsys.readouterr().err == "", "正常路徑印了診斷"


# ---------------------------------------------------------------------------
# `_is_unsafe_folder_name` — 把使用者輸入接到 base 目錄上之前的唯一一道閘
# ---------------------------------------------------------------------------
#
# 2026-08-30：舊版是 `"/" in name or "\\" in name or ".." in name`，而它自己的
# docstring 舉的例子是 `'C:/Windows'`——**那個字串剛好帶斜線**，所以被擋住了。
# 但錨定到磁碟機靠的是冒號不是斜線，無斜線的版本一路通過。
# 後果不是理論的：`/out sample <資料夾>` 命中的圖片會被**上傳到頻道**，而
# `out` / `debug` 都不在 `_OWNER_ONLY_GROUPS`，角色閘門在預設（三份 user_roles
# 全空）狀態下等於不存在。

# 2026-08-30 在本機實測會逃出 OUTPUT_ROOT、而舊版判定為「安全」的四筆。
_DRIVE_ANCHORED_ESCAPES = ["C:", "C:Windows", "c:", "C:$MFT"]


@pytest.mark.parametrize("name", _DRIVE_ANCHORED_ESCAPES)
def test_a_drive_anchored_name_is_unsafe(name):
    assert b._is_unsafe_folder_name(name), (
        f"{name!r} 被判定為安全，但 `OUTPUT_ROOT / {name!r}` 會**取代**整個 base。"
        "`/out sample` 會把命中的圖片上傳到頻道。")


@pytest.mark.parametrize("name", _DRIVE_ANCHORED_ESCAPES)
def test_the_escape_is_real_not_hypothetical(name):
    """反面：先證明這些輸入真的逃得出去，否則上一支測的只是一個沒有威脅的規則。"""
    base = PureWindowsPath("D:/Work/Example/output")
    joined = str(base / name).lower()
    assert not joined.startswith(str(base).lower()), (
        f"{name!r} 其實沒有逃出 base（{joined}）——前提變了，這組測試要重寫")


@pytest.mark.parametrize("name", [
    "..", "a/b", r"a\b", "/etc/passwd", "//server/share", r"\\server\share",
    ".", "C:/Windows", "C:\\Windows",
])
def test_the_classic_traversals_are_still_unsafe(name):
    assert b._is_unsafe_folder_name(name), name


@pytest.mark.parametrize("name", [
    "columbina (genshin impact)",
    "pramanix the prerita (arknights)_2",     # `allocate_output_dir` 產得出來的
    "necrass (arknights)",
    "debug_after_setup",
    "kei (student) (blue archive)",
    "rulership (monochrome gm) (umamusume)",
    "名字裡有中文",
])
def test_real_folder_names_are_not_rejected(name):
    """反向邊界：守衛收緊之後，`character_folder_name` 產得出來的名字一個都不能被擋。

    這些全是 `WEBRunner.log` / `output/` 裡真的出現過的形狀。
    """
    assert not b._is_unsafe_folder_name(name), name


def test_an_empty_name_is_not_a_path_problem():
    """`cmd_debug_show` 用空字串代表「沒給參數，列出可用的檔案」。
    守衛必須讓它通過，否則列表模式整個壞掉。"""
    assert b._is_unsafe_folder_name("") is False


# --------------------------------------------------------------------------
# 把變數接到 base 目錄上，每一次接合都要自己被守衛過
#
# 這支守門的**前一版有兩個獨立的盲點**，而 2026-09-09 的
# `cmd_fav_show` 就是同時踩中兩個才溜過去的：
#
#   folder = OUTPUT_ROOT / character     # ← 舊版只看得到這一行
#   p      = folder / filename           # ← 第二跳，左邊不是 OUTPUT_ROOT
#
# 1. **只看第一跳。** 條件寫死 `n.left.id == "OUTPUT_ROOT"`，所以任何「先接一層、
#    再拿結果去接第二層」都在視線外。而多層接合正是真實程式碼的常態寫法。
# 2. **判準是 per-function 不是 per-join**：「這個函式裡有沒有呼叫過守衛」。
#    `cmd_fav_show` 對 `character` 呼叫過，於是**同一個函式裡其他每一個接合都
#    自動免疫**——連把上面第 1 點修好都還是綠的。這正是本 repo 反覆出現的
#    「兩道防護互相遮蔽」，只是這次發生在守門自己身上。
#
# 現在的判準：跟著推導走到不動點（`OUTPUT_ROOT` 推導出 `folder`，`folder` 再推導
# 出下一個），而且**每一個**接合的右運算元都必須自己被餵進 `_is_unsafe_folder_name`
# 過。牙齒長在純函式 `_unguarded_derived_joins` 上，另外用合成原始碼問它「還看得
# 見東西嗎」——現況乾淨時，把主測試的斷言整個刪掉本來就不會紅。
# --------------------------------------------------------------------------
# base 目錄的名字**從模組自己推**，不要手寫。手寫的那一版有三個問題，而三個在
# 乾淨資料上長得跟正確的一模一樣：
#
#   * `DEBUG_DIR` 是**死條目**——全專案搜過，只有 `discord_bot.py` 的一句 docstring
#     跟這一行提到它，**沒有任何符號叫這個名字**。它從來沒有命中過任何東西，而底下
#     那道 `len(_BASE_ROOTS) >= 3` 的下限**把它算進去**，所以連下限都沒發現。失效
#     方式與 `CLAUDE.md` 記載的 `_OWNER_ONLY_SLASH` 一模一樣：改名之後那一筆變成
#     永遠不再命中的字串，守門照跑、集合還在、全綠，保護卻沒了。
#   * `TEMPLATES_DIR` / `CHROME_PROFILE_DIR` 是真的存在的目錄常數，卻不在清單裡。
#     補上前者就會看見 `_template_path`（`TEMPLATES_DIR / name`，`name` 來自使用者
#     輸入），它在此之前對這道守門完全隱形。
#   * 名字集合綁在「有人記得維護」上，而不是綁在模組實際定義了什麼——§8.8 第八個
#     實例的形狀（同一個檔案裡的 `_NDJSON_ROOT_NAMES` 早就為了同一件事收了三個
#     別名，隔了一千多行的這一份卻沒有）。
#
# 判準：模組層的大寫名字，值是 `Path(...)`、`.parent` / `.resolve()` 鏈、或
# `<X> / "<沒有副檔名的字面值>"`。檔案常數（`todo_prompt.md` 之類）自然被排除，
# 而且就算誤收也沒有代價——偵測器只看右邊是**變數**的接合。
_EXTRA_BASE_ROOTS: set[str] = set()      # 推不出來但確實是目錄的，寫這裡並附理由


def _module_dir_constants(tree) -> set[str]:
    """模組層級被綁到「目錄」上的大寫名字。

    ⚠️ **兩種寫法一度整組隱形（2026-09-10 修）**，而且同一個名字兩種都中：

    * `X: Path = …` 是 `ast.AnnAssign`，不是 `ast.Assign`。只讀後者就看不到
      `_gui_control.py` 的 `_SHELL_CWD: Path = PROJECT_ROOT`——連帶
      `set_shell_cwd` 裡那個 `_SHELL_CWD / expanded` 從來沒有被這道守門問過。
      型別註記是這個 repo 到處在用的寫法，所以那不是一個特例。**而且註記本身
      就是最強的訊號**：宣告成 `Path` 的東西不必再從右邊猜。
    * `X = 另一個已知的根`（單純別名）要跑**不動點**才跟得到——右邊是不是根，
      取決於左邊那些先被認出來沒有。

    這是 §8.8 家族的第三種形狀：規則對、範圍也涵蓋了那個檔，**但擷取器認不得那種
    寫法**，於是「這個檔很乾淨」與「這個檔沒被看見」在輸出上一模一樣。
    """
    import ast as _ast
    import pathlib as _pathlib

    # (名字, 值, 註記)——`Assign` 與 `AnnAssign` 都收。
    bindings: list[tuple] = []
    for node in tree.body:
        if (isinstance(node, _ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], _ast.Name)):
            bindings.append((node.targets[0].id, node.value, None))
        elif (isinstance(node, _ast.AnnAssign) and node.value is not None
              and isinstance(node.target, _ast.Name)):
            bindings.append((node.target.id, node.value, node.annotation))

    out: set[str] = set()
    aliases: list[tuple[str, str]] = []
    for name, value, annotation in bindings:
        if not name.lstrip("_").isupper():
            continue
        # **包了一層單引數呼叫的也算。** 逐平台的狀態檔寫成
        # `_platform_state(PROJECT_ROOT / "dorossi_workspace")`，而不剝掉那層的話
        # `DOROSSI_CC_WORKDIR` 就不再是「根」——於是 `dorossi_session_workdir` 裡那個
        # `ROOT / "sessions" / f"{uid}_{sid}"` 一整個**從掃描裡消失**，而消失與乾淨
        # 在輸出上一模一樣（那筆具名例外會被報成「已經不會被標出來」，這是唯一的訊號）。
        if (isinstance(value, _ast.Call) and len(value.args) == 1
                and not value.keywords
                and isinstance(value.func, _ast.Name)
                and value.func.id != "Path"):
            value = value.args[0]
        # 註記必須**就是** `Path`，不是「裡面提到 Path」。`"Path" in unparse(...)`
        # 這種寫法會把 `_UNDO_STACK: list[tuple[Path, str]]` 一起收進來——一個裝
        # 路徑的容器不是一個根，而多收的名字會變成永遠不命中的幽靈條目
        # （`test_the_base_roots_contain_no_name_that_does_not_exist` 當場抓到 4 個）。
        if (annotation is not None
                and _ast.unparse(annotation).replace(" ", "") in {
                    "Path", "Path|None", "Optional[Path]", "pathlib.Path"}):
            out.add(name)
        elif (isinstance(value, _ast.Call) and isinstance(value.func, _ast.Name)
                and value.func.id == "Path"):
            out.add(name)
        elif isinstance(value, _ast.Attribute) and value.attr in ("parent",
                                                                  "parents"):
            out.add(name)
        elif (isinstance(value, _ast.Call)
              and isinstance(value.func, _ast.Attribute)
              and value.func.attr in ("resolve", "absolute")):
            out.add(name)
        elif (isinstance(value, _ast.BinOp) and isinstance(value.op, _ast.Div)
              and isinstance(value.right, _ast.Constant)
              and isinstance(value.right.value, str)
              and not _pathlib.PurePosixPath(value.right.value).suffix):
            out.add(name)
        elif isinstance(value, _ast.Name):
            aliases.append((name, value.id))

    # 別名的不動點：`A = ROOT` 之後 `B = A` 也是根。
    for _ in range(8):
        grew = False
        for name, source in aliases:
            if source in out and name not in out:
                out.add(name)
                grew = True
        if not grew:
            break
    return out | _EXTRA_BASE_ROOTS


# 實測 2026-09-10：整個專案 30 個檔、22 個接合站點、**21** 個相異 base 目錄。
#
# 下限刻意抓在實測值的一半左右而不是 21——一個貼著現況的下限會對合法輸入亂叫
# （刪掉一個模組就紅），而會亂叫的正面對照組只會被下一個人調小到綠為止，然後
# 這一格就永遠是裝飾品（`feedback-a-positive-control-can-cry-wolf`）。
#
# ⚠️ 但**純數字答不出這一格真正要問的問題**。2026-09-10 之前這裡是 4，而
# `discord_bot.py` 一個檔就推得出 5 個根——也就是說「掃描範圍縮回只剩 bot」
# 既過得了下限，也過得了當時唯一的具名金絲雀 `TEMPLATES_DIR`（它就在
# `discord_bot.py` 裡）。所以下面另有一個**在 bot 以外**的具名金絲雀。
_BASE_ROOT_FLOOR = 12
# 紀律 (a) 認得的守衛名。**是集合而不是單一名字，因為模組邊界逼出了第二份實作**：
# `_is_unsafe_folder_name` 住在 `discord_bot.py`，而 webrunner 側不得 import bot，
# 所以同一條判準在 `_webrunner_shared.py` 有刻意的第二份。少了它，webrunner 那側
# **任何**用自家守衛寫出來的正確修法都會被判成沒守住（2026-09-10 實測：六種候選
# 寫法全跑過一遍才確定這件事）。
#
# `_is_single_path_component` 是第三支，比前兩支**弱**：它只問「接完之後還是不是
# 單一元件」，用在檔名而不是資料夾名。用更嚴的那支會誤擋合法的診斷截圖檔名。
#
# ⚠️ 這支檢查器問的是「這個名字有沒有被守衛**看過**」，不是「看過之後怎麼處置」。
# 三支的極性並不一致（`_is_unsafe_folder_name` 是 True＝不安全，另兩支相反），把
# 判定寫反照樣過得了這一關——那是既有性質，不是這次放寬新開的洞。
#
# ⚠️ **名字從一個變成三個之後，過期的代價變小了、因此更危險。** 只有一個名字時，
# 改名會讓**所有**站點一起變紅（很吵，所以馬上會被發現）；三個名字時，改掉其中
# 一個只會讓那一支守住的站點安靜地失去辨識，其餘照常綠。所以下面另有一支對帳，
# 確認每個名字都還真的解析得到一個函式——同 CLAUDE.md 對 `_OWNER_ONLY_SLASH`
# 的說法。
#
# ⚠️ **名字是唯一的認人依據，所以「同名兩份實作」是這份清單最便宜的破法。** 在別
# 的模組寫一支同名但更寬鬆的 helper，它守住的每一個站點都會安靜地繼承這裡的信任，
# 而清單本身、所有既有測試、對帳測試全部照樣綠——對帳問的是「這個名字解析得到
# 函式嗎」，同名兩份的答案是「解析得到」。所以下面另有一支測試要求每個名字在整個
# 掃描語料裡**只被定義一次**。第四份實作（`_run_progress`）因此刻意取了不同名。
_JOIN_GUARDS = frozenset({
    "_is_unsafe_folder_name",       # discord_bot.py：正典，單一層相對元件
    "_is_safe_folder_component",    # _webrunner_shared.py：模組邊界逼出的第二份
    "_is_single_path_component",    # _webrunner_shared.py：檔名用的弱化版
    "_is_unsafe_folder_component",  # _run_progress.py：第四份，循環 import 逼出來的
})
# 第二種紀律：不檢查右運算元，改檢查**接合後的結果**落不落在允許的根底下。
# 兩種紀律的合法值形狀相反，所以缺一不可——把單一元件那支套到多層／絕對路徑上
# 會把每一個合法值都擋掉，那正是 `_handle_single_image_done` 當初只能列具名例外
# 的原因。認得兩種之後那筆例外才拿得掉。
_CONTAINMENT_GUARD = "_within_allowed_roots"


# --------------------------------------------------------------------------
# 掃描範圍：**整個專案**，不是 `discord_bot.py` 一個檔案。
#
# 2026-09-10 之前這支守門只 `ast.parse` 了 `discord_bot.py`。而它守的規則——
# 「不要把一個變數直接接到基準目錄上，除非證明過那是單一安全成分」——**文字裡沒有
# 提到任何模組**。同一條規則在 webrunner 那一側被違反過：`character_folder_name`
# 讓佇列裡一行 `..` 原封不動變成資料夾名，而 `Path("output") / ".."` 不會被
# pathlib 正規化，一整個角色的圖就無聲寫進 repo 根目錄。那個缺陷離這支守門只有
# 一個 `ast.parse` 的距離，而它從來沒有被問過。
#
# 這是 §8.8(A) 那個缺陷家族：**規則的文字沒有提到任何模組，掃描範圍卻寫死成一份
# 清單**。CLAUDE.md 已經記過同家族的另外兩個實例（atomic-writes 掃描的六元組漏掉
# `webrunner.pid`，因為它唯一的寫入者住在 repo root；`_module_imports` 看不到
# `ImportFrom.names`）。
#
# **repo root 一起掃**，理由與 atomic-writes 那一筆完全相同：兩支監督者住在那裡，
# 而「範圍窄」與「範圍寬」在乾淨資料上長得一模一樣。
_JOIN_SCAN_SKIP_FILES = {
    # 由人在終端機手動執行的開發工具。**判準不是「traceback 好不好看」**（那是
    # `test_undecodable_files._SCAN_SKIP_FILES` 的判準，兩份清單刻意不共用），
    # 是「輸入有沒有跨過一道權限邊界」：這幾支的路徑成分來自敲指令的那個人自己、
    # 或來自 repo 自己的原始碼，而那個人本來就有一個 shell。長命行程（bot／背景
    # 產圖／監督者／啟動器）一律不得列入——它們的路徑成分可能來自對話平台的使用者
    # 或一份磁碟上的資料檔，那才是這道守門存在的理由。
    "gen_command_docs.py",      # 產生 commands/*.md，py -3 手動跑
    "mutation_harness.py",      # 變異測試骨架，由探針腳本／測試呼叫
    "install_autostart.py",     # 註冊工作排程器，py -3 install_autostart.py --install
    "wake_autostart.py",        # 叫醒排程工作，py -3 wake_autostart.py；只吃固定旗標、不組路徑
}

_JOIN_SCAN_FILE_FLOOR = 25


def _join_scan_sources(pkg_root=None, repo_root=None) -> tuple:
    """每一個非測試、非手動工具的專案模組——**含 repo root 的啟動器**。

    兩個目錄可以換掉，就是為了讓範圍本身測得出「這是 glob 算出來的，不是今天剛好
    對」（§8.8(A3)）。
    """
    package = Path(b.__file__).resolve().parent if pkg_root is None else pkg_root
    root = package.parent if repo_root is None else repo_root
    out = []
    for path in sorted(package.glob("*.py")) + sorted(root.glob("*.py")):
        if path.name.startswith(("test_", "_test_")) or path.name == "conftest.py":
            continue
        if path.name in _JOIN_SCAN_SKIP_FILES:
            continue
        out.append(path)
    return tuple(out)


# --------------------------------------------------------------------------
# 三種**結構性**紀律：右運算元不必被守衛函式碰過，因為它的形狀本身就保證了性質。
#
# 加這三種的理由是「不要用具名例外去換取安靜」。列舉式的例外 fail-open（改名之後
# 變成一個永遠不再命中的字串），而這三種形狀是可以直接從語法問出來的事實。10 個
# webrunner 站點裡有 8 個屬於這三種——把它們寫成例外等於替未來的回歸先發 8 張
# 免死金牌。
def _is_literal_string_sequence(value) -> bool:
    """這棵運算式是不是「全部都是字串字面值的 tuple／list／set」？

    兩個呼叫端共用同一個判準：`_module_literal_sequences`（綁到模組層名字的那種）
    與 `_structurally_safe_components` 規則 (3) 的行內版（直接寫在 `for … in` 那一
    行的那種）。分成兩份寫的話兩邊會各自漂，而它們要問的是同一件事。
    """
    import ast as _ast

    return (isinstance(value, (_ast.Tuple, _ast.List, _ast.Set))
            and bool(value.elts)
            and all(isinstance(e, _ast.Constant) and isinstance(e.value, str)
                    for e in value.elts))


def _module_literal_sequences(tree) -> set[str]:
    """模組層被綁到「全部都是字串字面值的 tuple／list／set」的名字。

    ⚠️ 這**不是**「單一路徑成分」的保證——`_SESSION_CRITICAL` 的元素長得像
    `"Default/Network/Cookies"`，帶著分隔符。這裡的保證是另一種、而且更強的：
    整個值是**本 repo 自己原始碼裡的字面值**，沒有任何外來輸入需要消毒。這與偵測器
    本來就放行的「右邊是字面值」是同一件事，只是隔了一層 `for … in`。
    """
    import ast as _ast

    return {node.targets[0].id for node in tree.body
            if isinstance(node, _ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], _ast.Name)
            and _is_literal_string_sequence(node.value)}


def _structurally_safe_components(fn, literal_seqs) -> set[str]:
    """這個函式裡，哪些本地名字**由形狀本身**保證可以安全地當右運算元。

    (1) `x = <expr>.relative_to(<base>)`：不帶 `walk_up=True` 時，
        `relative_to` 要嘛丟 `ValueError`、要嘛回一個**沒有 `..`、不是絕對路徑**
        的相對路徑，其成分全部來自原本那條路徑。所以把它接回任何一個根底下，
        結果一定還在那個根裡面。
        ⚠️ `walk_up=True`（3.12 起）**會**產出 `..`，保證就沒了——所以那個關鍵字
        出現時這一條不成立，必須照樣標出來。
    (2) `for a, b, c in os.walk(...)` 的 `b` / `c` 逐項：`os.walk` 給的是
        `os.listdir` 語意的**裸名字**，不含分隔符，也永遠不會是 `.` 或 `..`。
    (3) `for x in <字面值序列>`：見 `_module_literal_sequences`。序列**綁到模組
        層的名字**或**直接寫在 `for` 那一行**都算——兩種寫法的保證一模一樣（整
        個值來自本 repo 的原始碼，沒有外來輸入），而行內那種其實**更強**：沒有
        任何地方能把那個名字重新綁掉。2026-09-18 之前只認前者，於是規則獎勵
        「先拉成模組層常數」、處罰行內寫法，而那個差別與安全性無關。實際代價是
        一筆假紅：`audit_simplified_chars.sources()` 的 `REPO_ROOT / folder`
        被報成違規，而那個 `folder` 迭代的是同一行上的五個字串常數。

    **刻意不做跨函式推導。** 參數是否安全取決於呼叫端，而呼叫端可能在別的模組——
    在一個模組裡看不到全部呼叫端就下結論，是 fail-open 的。那一類（目前只有
    `_sync_profile_dir_back`）走具名例外，並且**例外自己的前提要有測試驗**
    （見 `test_the_dir_sync_exemption_still_only_ever_sees_constants`）。
    """
    import ast as _ast

    safe: set[str] = set()
    # (1) relative_to
    for n in _ast.walk(fn):
        if (isinstance(n, _ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], _ast.Name)
                and isinstance(n.value, _ast.Call)
                and isinstance(n.value.func, _ast.Attribute)
                and n.value.func.attr == "relative_to"
                and not any(kw.arg == "walk_up" for kw in n.value.keywords)):
            safe.add(n.targets[0].id)
    # (2) os.walk 的 dirs / files
    walk_lists: set[str] = set()
    for n in _ast.walk(fn):
        if not isinstance(n, _ast.For):
            continue
        it = n.iter
        is_walk = (isinstance(it, _ast.Call) and (
            (isinstance(it.func, _ast.Attribute) and it.func.attr == "walk")
            or (isinstance(it.func, _ast.Name) and it.func.id == "walk")))
        if not is_walk:
            continue
        if isinstance(n.target, _ast.Tuple) and len(n.target.elts) == 3:
            for elt in n.target.elts[1:]:
                if isinstance(elt, _ast.Name):
                    walk_lists.add(elt.id)
    # (2) + (3)：`for x in <那個序列>`。iter 可以是一個名字（`os.walk` 的
    # dirs／files，或模組層的字面值序列），也可以是直接寫在這一行的字面值序列。
    for n in _ast.walk(fn):
        if not (isinstance(n, _ast.For) and isinstance(n.target, _ast.Name)):
            continue
        by_name = (isinstance(n.iter, _ast.Name)
                   and (n.iter.id in walk_lists or n.iter.id in literal_seqs))
        if by_name or _is_literal_string_sequence(n.iter):
            safe.add(n.target.id)
    return safe


_SHAPE_SAFE_CALLS = {"int", "len", "id", "abs", "ord"}
_SHAPE_SAFE_METHODS = {"strftime", "time", "monotonic", "hex"}
_SHAPE_SAFE_ATTRS = {"name", "stem"}
# 字面值片段裡只要出現這些，整條就判不安全。`..` 用**逐片段**比對，不可以把片段
# 串起來再看：`f"{p.name}.{stamp}.{ms}.bak"` 的片段是 `.`、`.`、`.bak`，串起來是
# `...bak`（含 `..`），那會誤殺一個今天就在用的合法形狀。
_SEPARATORS_IN_LITERAL = ("/", "\\", ":", "..")


def _literal_segments_are_clean(node) -> bool:
    """f-string 夾在插值之間的**字面值**片段，自己有沒有帶路徑語意。

    這一格是照著今天的語料寫規則最容易漏的地方：真實語料裡的 f-string 接合長得像
    `f"_shot_{int(time.time() * 1000)}.png"`，字面值剛好都無害，於是「只檢查插值」
    的規則在真實語料上 100% 正確。實測 `f"{int(t)}/evil"`、`f"../{int(t)}"`、
    `f"C:{int(t)}"` 三種都會被那版規則判成安全。
    """
    import ast as _ast

    for value in node.values:
        if isinstance(value, _ast.Constant) and isinstance(value.value, str):
            if any(bad in value.value for bad in _SEPARATORS_IN_LITERAL):
                return False
    return True


def _shape_safe_operand(node, safe_names) -> bool:
    """這個右運算元的**形狀本身**保證了「接上去不會改到目錄」嗎？

    紀律 (c) 的運算式版。`_structurally_safe_components` 收的是**名字**，這一支問
    的是一棵運算式——放寬抽取器去收 f-string 之後才需要它。

    ⚠️ **format spec 也要檢查，而且同一條規則就夠。** `format("a", "/>10")` 是
    `"/////////a"`——fill 字元會直接進到結果字串裡，所以 `f"{p.name:{sep}>10}"` 的
    值端雖然安全，整體仍然不安全。實測確認：AST 裡的 format spec **永遠**是
    `JoinedStr`（即使寫死成 `:03d` 也是），所以上面那條「字面值片段不得含分隔符」
    自動也蓋到 spec，不需要另一條規則。今天的語料裡沒有這種寫法——正因為如此才要
    現在就寫對：一條只在今天的資料上成立的規則，是照著例子寫出來的規則。
    """
    import ast as _ast

    if isinstance(node, _ast.Constant):
        return True
    if isinstance(node, _ast.Name):
        return node.id in safe_names
    if isinstance(node, _ast.Attribute):
        return node.attr in _SHAPE_SAFE_ATTRS
    if isinstance(node, _ast.Call):
        if isinstance(node.func, _ast.Name):
            return node.func.id in _SHAPE_SAFE_CALLS
        if isinstance(node.func, _ast.Attribute):
            return node.func.attr in _SHAPE_SAFE_METHODS
        return False
    if isinstance(node, _ast.BinOp):
        return (_shape_safe_operand(node.left, safe_names)
                and _shape_safe_operand(node.right, safe_names))
    if isinstance(node, _ast.JoinedStr):
        if not _literal_segments_are_clean(node):
            return False
        for value in node.values:
            if not isinstance(value, _ast.FormattedValue):
                continue
            if not _shape_safe_operand(value.value, safe_names):
                return False
            if (value.format_spec is not None
                    and not _shape_safe_operand(value.format_spec, safe_names)):
                return False
        return True
    return False


def _otherwise_bound_names(fn) -> set:
    """在這支函式裡被**非單純指派**綁定過的名字——一律當不可知。

    參數、`for` 目標、`with as`、`except as`、walrus、`AugAssign`/`AnnAssign`、
    拆包指派。這些的值不是一句可以看的運算式，所以沒有辦法判形狀。
    """
    import ast as _ast

    bound = {arg.arg for arg in _ast.walk(fn) if isinstance(arg, _ast.arg)}
    for node in _ast.walk(fn):
        if isinstance(node, (_ast.For, _ast.AsyncFor)):
            bound |= {n.id for n in _ast.walk(node.target)
                      if isinstance(n, _ast.Name)}
        elif isinstance(node, _ast.withitem) and node.optional_vars is not None:
            bound |= {n.id for n in _ast.walk(node.optional_vars)
                      if isinstance(n, _ast.Name)}
        elif isinstance(node, _ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, _ast.NamedExpr) and isinstance(node.target, _ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, (_ast.AugAssign, _ast.AnnAssign)):
            if isinstance(node.target, _ast.Name):
                bound.add(node.target.id)
        elif isinstance(node, _ast.Assign):
            for target in node.targets:
                if not isinstance(target, _ast.Name):
                    bound |= {n.id for n in _ast.walk(target)
                              if isinstance(n, _ast.Name)}
    return bound


def _shape_safe_local_names(fn) -> set:
    """本地名字 → **每一次**指派都形狀安全才算安全。

    ⚠️ 「看到一個安全的指派就收進來」是 fail-open 的，而那正是隔壁
    `_structurally_safe_components` 的寫法。放寬抽取器讓那個形狀第一次真的碰到
    f-string 那一類站點，所以在同一次改掉：

        stamp = time.strftime(...)   # 安全
        stamp = payload              # 不安全的再指派
        p = ROOT / f"{stamp}.png"    # 只看第一次的話 → 判成安全

    順序無關（先不安全後安全也一樣擋），因為判準是「所有指派」而不是「最後一次
    指派」——後者要做資料流分析，而這支檢查器刻意只做語法層的保守推導。
    """
    import ast as _ast

    unknown = _otherwise_bound_names(fn)
    assigns: dict = {}
    for node in _ast.walk(fn):
        if (isinstance(node, _ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], _ast.Name)):
            assigns.setdefault(node.targets[0].id, []).append(node.value)
    names: set = set()
    for _ in range(3):
        grew = False
        for name, values in assigns.items():
            if name in names or name in unknown:
                continue
            if all(_shape_safe_operand(value, names) for value in values):
                names.add(name)
                grew = True
        if not grew:
            break
    return names - unknown


_ANNOTATION_CONTAINERS = frozenset({
    "list", "tuple", "set", "frozenset", "dict",
    "List", "Tuple", "Set", "FrozenSet", "Dict",
    "Sequence", "Iterable", "Iterator", "Collection", "Mapping",
})


def _is_path_annotation(node) -> bool:
    """這個型別註記代表的是**一個路徑**嗎？

    ⚠️ 判準刻意不是「註記的文字裡出現 `Path`」。`list[Path]` 裡有 `Path`，但
    裝一堆路徑的容器**本身不是路徑**；把它當成接合的根，等於憑空製造一類根本不
    存在的站點。所以要遞迴進 `Subscript`（先排掉容器名）、`BinOp|`
    （`Path | None`）、`Attribute`（`pathlib.Path`）與字串註記（`"Path"`）。

    ⚠️ 而規則是照**型別的意義**寫的，不是照今天的語料寫的。本 repo 實測只有三種
    寫法（`Path` 62、`Path | None` 5、`list[Path]` 5），照著這三種寫一個
    `text in ("Path", "Path | None")` 今天會全綠——然後明天有人寫
    `Optional[Path]` 或 `pathlib.Path`，那個站點就悄悄消失。同一輪的 f-string
    形狀規則第一版就是從今天的資料長出來的，對合成的近似案例六題錯三題。
    """
    import ast as _ast

    if node is None:
        return False
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        try:
            node = _ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return False
    if isinstance(node, _ast.Name):
        return node.id == "Path"
    if isinstance(node, _ast.Attribute):
        return node.attr == "Path"
    if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.BitOr):
        return (_is_path_annotation(node.left)
                or _is_path_annotation(node.right))
    if isinstance(node, _ast.Subscript):
        head = node.value
        name = (head.id if isinstance(head, _ast.Name)
                else head.attr if isinstance(head, _ast.Attribute) else "")
        if name in _ANNOTATION_CONTAINERS:
            return False
        return _is_path_annotation(node.slice)
    if isinstance(node, _ast.Tuple):
        return any(_is_path_annotation(elt) for elt in node.elts)
    return False


def _path_typed_params(fn) -> set:
    """這支函式裡帶 `Path` 型別註記的參數名——它們也是接合的根。

    ⚠️ **逐函式收，不可以整個模組聯集。** 第一版把整個模組的 Path 參數名聯集起來
    當根，於是 `_gui_control._atomic_write(path: Path, …)` 讓 `path` 在**整個模組**
    都變成根，`write_host_file` 裡那個 `path / safe_basename(...)`（那裡的 `path`
    是本地變數、不是參數）就被誤報成新站點。

    ⚠️ **也不可以用 `base_roots=` 加 `ast.Module(body=[fn])` 來偽裝逐函式。**
    `_derived_join_sites` 裡的 `_module_literal_sequences(tree)` 需要**整棵樹**才
    找得到 `_CHROME_LOCK_FILES` / `_SESSION_CRITICAL` 這些模組層字面值序列；只餵
    一支函式進去，紀律 (3) 就失效，自動守住的站點會從 5 個掉到 1 個，看起來像是
    憑空多出四個違規。正確做法是在既有的 `for fn` 迴圈裡把該函式的參數加進
    `derived`——本函式就是給那裡用的。

    `*args` / `**kwargs` 不收：`*paths: Path` 綁的是一個 tuple、`**kw: Path` 綁的
    是一個 dict，跟 `list[Path]` 同一個道理。
    """
    import ast as _ast

    args = fn.args
    every = (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs))
    return {a.arg for a in every
            if isinstance(a, _ast.arg) and _is_path_annotation(a.annotation)}


_PATH_PRESERVING_METHODS = frozenset({"resolve", "expanduser", "absolute"})
_PATH_PRESERVING_ATTRS = frozenset({"parent"})


def _path_returning_functions(tree) -> frozenset:
    """回傳型別註記是 `Path` 的模組層函式名。

    這些函式的回傳值**就是路徑**，所以 `p = resolve_host_path(x)` 之後的
    `p / y` 是一個接合站點。判準沿用 `_is_path_annotation`（同一支，所以
    `-> Path | None` / `-> pathlib.Path` 也認得，而 `-> list[Path]` 不算）。
    實測 2026-09-11：全專案 14 支。
    """
    import ast as _ast

    return frozenset(
        node.name for node in _ast.walk(tree)
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        and _is_path_annotation(node.returns))


def _denotes_derived(node, derived: set, path_fns=frozenset()) -> bool:
    """這個**運算式**指的是不是「從 base 根推導出來的路徑」。

    比「裸 `Name` 在 `derived` 裡」寬，因為路徑在這個 repo 裡常常先過一手才被接：

    * `(root or CODEX_IMAGE_ROOT).resolve()` ── `Call`，`dorossi_backend` 的
      `_collect_codex_images` 就是這個形狀；
    * `ROOT / "sessions" / f"{uid}_{sid}"` ── **鏈式接合**：外層 `/` 的左邊是另一個
      `BinOp`，不是 `Name`。這一類在 2026-09-11 之前**整組隱形**，因為抽取器要求
      `isinstance(n.left, ast.Name)`；
    * `base.parent`、`p.expanduser()`；
    * `resolve_host_path(x)` ── 呼叫一支**回傳型別註記是 `Path`** 的函式
      （`path_fns`）。`_gui_control.write_host_file` 就是這個形狀，而它的右運算元
      也是 `Call`（`safe_basename(...)`）——**兩邊同時隱形**，所以先前三次放寬
      一次都救不到它。

    ⚠️ `or` 與三元要求**每一個**分支都是路徑，不是任一個。`root or "字串"` 只要有
    一邊過就放行的話，這支就從「認得路徑」退化成「看到 `or` 就點頭」。
    """
    import ast as _ast

    if isinstance(node, _ast.Name):
        return node.id in derived
    if isinstance(node, _ast.Call):
        func = node.func
        if isinstance(func, _ast.Name):
            return func.id in path_fns
        return (isinstance(func, _ast.Attribute)
                and func.attr in _PATH_PRESERVING_METHODS
                and _denotes_derived(func.value, derived, path_fns))
    if isinstance(node, _ast.Attribute):
        return (node.attr in _PATH_PRESERVING_ATTRS
                and _denotes_derived(node.value, derived, path_fns))
    if isinstance(node, _ast.BoolOp) and isinstance(node.op, _ast.Or):
        return all(_denotes_derived(v, derived, path_fns) for v in node.values)
    if isinstance(node, _ast.IfExp):
        return (_denotes_derived(node.body, derived, path_fns)
                and _denotes_derived(node.orelse, derived, path_fns))
    if isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Div):
        return _denotes_derived(node.left, derived, path_fns)
    return False


def _derived_join_sites(tree, base_roots=None):
    """回 `(函式名, 行號, "left / right", 有沒有被守住)`：每一次接到 base 目錄
    （或其推導物）上、而右運算元是**變數**的接合。

    三種紀律各自足夠：
      (a) 右運算元被餵進 `_is_unsafe_folder_name`（必須是單一層元件）；
      (b) 這次接合的**賦值目標**被餵進 `_within_allowed_roots`（解析後必須落在
          允許的根底下）；
      (c) 右運算元的**形狀本身**就保證了性質（見 `_structurally_safe_components`）。

    (b) 的粒度刻意是 **per-join**：對 `folder` 做包含性檢查**不會**讓
    `folder / filename` 免疫，因為後者的賦值目標是 `p` 而不是 `folder`。這正是
    `cmd_fav_show` 溜過去的形狀，只是換成另一種守衛——「兩道防護互相遮蔽」在這支
    檢查器自己身上已經發生過一次，不要再讓它發生。

    純函式，好讓合成資料問得到它。**不做字串層級的推導**——`cmd_debug_show` 的
    `candidate = f"debug_{name}"` 追不到，那種要靠具名例外處理。
    """
    import ast as _ast

    literal_seqs = _module_literal_sequences(tree)
    path_fns = _path_returning_functions(tree)
    out = []
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        # 哪些本地名字是「由 base 目錄推導出來的路徑」。做到不動點，所以
        # folder = ROOT / a; sub = folder / b 兩層都跟得到。
        derived = set(_module_dir_constants(tree)
                      if base_roots is None else base_roots)
        # 2026-09-11 放寬：帶 `Path` 註記的**參數**也是根。在此之前整個
        # 「呼叫端傳路徑進來、被呼叫端往上接東西」的族群一列都不會產生——沒有站點
        # 就沒有例外，也就沒有任何地方記錄「這裡沒被看過」。
        derived |= _path_typed_params(fn)
        for _ in range(8):
            grew = False
            for n in _ast.walk(fn):
                if not (isinstance(n, _ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], _ast.Name)):
                    continue
                value = n.value
                if (n.targets[0].id not in derived
                        and _denotes_derived(value, derived, path_fns)):
                    derived.add(n.targets[0].id)
                    grew = True
            if not grew:
                break
        guarded_names = {
            arg.id for call in _ast.walk(fn)
            if isinstance(call, _ast.Call)
            and isinstance(call.func, _ast.Name)
            and call.func.id in _JOIN_GUARDS
            for arg in call.args if isinstance(arg, _ast.Name)}
        guarded_names |= _structurally_safe_components(fn, literal_seqs)
        # 紀律 (b)：只認**第一個位置引數**（被檢查的那個路徑）。第二個引數是允許
        # 的根清單，把它一起收進來會讓 `_within_allowed_roots(p, roots)` 裡的
        # `roots` 也變成「檢查過的名字」，等於憑空發免死金牌。
        contained_targets = {
            call.args[0].id for call in _ast.walk(fn)
            if isinstance(call, _ast.Call)
            and isinstance(call.func, _ast.Name)
            and call.func.id == _CONTAINMENT_GUARD
            and call.args and isinstance(call.args[0], _ast.Name)}
        # 紀律 (b) 的第二種拼法：`if base not in folder.parents: return`。這是
        # `dorossi_backend._collect_codex_images` 用的寫法，語意跟
        # `_within_allowed_roots` 一樣（接合後的結果要落在某個根底下），只是沒有
        # 具名函式，所以在 2026-09-11 之前這支檢查器看不見它——那個站點會被列成
        # 「沒有任何紀律守住」，而它其實有兩道（regex ＋ 這一道）。
        #
        # ⚠️ **必須同時要求 `.resolve()`，而且理由比「不夠嚴」更糟：少了 resolve，
        # 這道檢查是 fail-open 的。** `Path.parents` 走的是**字面的**元件，所以
        # 實測 `base in (base / "../../etc/passwd").parents` 是 **True**——它對
        # 每一個穿越都回答「有包含」，而那個路徑 resolve 之後在 `D:\Work\etc`。
        # 加上 `.resolve()` 之後同一個輸入回 False。也就是說沒有 resolve 的版本
        # 不是「弱一點的守衛」，是一個**看起來在把關、實際上永遠點頭**的守衛。
        # 只認 `resolve` 一個方法名，`absolute()` 不算（它只把 cwd 接到前面，
        # `..` 原封不動）。前提由
        # `test_the_parents_containment_discipline_requires_a_resolve` 釘住。
        parents_checked = set()
        for node in _ast.walk(fn):
            if not (isinstance(node, _ast.Compare) and len(node.ops) == 1
                    and isinstance(node.ops[0], (_ast.In, _ast.NotIn))):
                continue
            right = node.comparators[0]
            if (isinstance(right, _ast.Attribute) and right.attr == "parents"
                    and isinstance(right.value, _ast.Name)):
                parents_checked.add(right.value.id)
        # 這次接合被指派給哪個名字。用節點身分（`id()`）而不是行號——同一行可以有
        # 好幾個接合。
        assign_target_of = {}
        resolved_targets = set()
        for n in _ast.walk(fn):
            if not (isinstance(n, _ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], _ast.Name)):
                continue
            value = n.value
            # `folder = (base / x).resolve()`：接合被包在 `.resolve()` 裡面，
            # 值不是 `BinOp` 而是 `Call`，所以沒有這一步就對不到賦值目標。
            #
            # ⚠️ **只認 `resolve`。** 下面第二種包含性檢查（`base not in
            # folder.parents`）走的是**字面的**元件，所以沒有 `.resolve()` 的話它
            # 對每一個穿越都回答「有包含」（實測 `base in
            # (base / "../../etc/passwd").parents` 是 True）。`absolute()` 只是把
            # cwd 接到前面，`..` 原封不動，**不算**。
            if (isinstance(value, _ast.Call)
                    and isinstance(value.func, _ast.Attribute)
                    and value.func.attr == "resolve"):
                value = value.func.value
                if isinstance(value, _ast.BinOp):
                    resolved_targets.add(n.targets[0].id)
            if isinstance(value, _ast.BinOp):
                assign_target_of[id(value)] = n.targets[0].id
        # 紀律 (c) 的名字集。形狀規則要看得到「已經過守衛的名字」，否則**正確的**
        # 寫法（`ROOT / f"debug_{guarded_name}.png"`）會變紅，而消紅最省事的做法是
        # 開一筆例外——那正是這一整套守門在防的「最便宜的錯誤修法」。
        shape_safe = guarded_names | _shape_safe_local_names(fn)
        contained_targets |= parents_checked & resolved_targets
        for n in _ast.walk(fn):
            if not (isinstance(n, _ast.BinOp) and isinstance(n.op, _ast.Div)
                    and _denotes_derived(n.left, derived, path_fns)):
                continue
            if isinstance(n.right, _ast.Constant):
                continue          # 字面值：由構造保證安全，本來就不是站點
            if isinstance(n.right, _ast.Name):
                guarded = (n.right.id in shape_safe
                           or assign_target_of.get(id(n)) in contained_targets)
                expr = "%s / %s" % (_ast.unparse(n.left), n.right.id)
            else:
                # 2026-09-11 放寬：右運算元不是裸 `Name` 的接合。在此之前這一整類
                # **連一列都不會產生**——不需要例外，也沒有任何地方記錄「這裡沒被
                # 看過」，是 fail-open 的版本。實測放寬讓站點從 25 變 37。
                guarded = _shape_safe_operand(n.right, shape_safe)
                expr = _ast.unparse(n)
            out.append((fn.name, n.lineno, expr, guarded))
    return sorted(set(out))


def _unguarded_derived_joins(tree, base_roots=None):
    """`_derived_join_sites` 裡沒有被任何一種紀律守住的那些。"""
    return sorted({row[:3] for row in _derived_join_sites(tree, base_roots)
                   if not row[3]})


def _scan_derived_joins(sources=None):
    """跑遍整個專案。回 `(rows, roots_by_file)`。

    `rows` 是 `(檔名, 函式名, 行號, "left / right", 有沒有被守住)`——**含已守住
    的**，因為正面對照組要數的是「掃到幾個接合」而不是「剩幾個違規」。剩下的違規
    會被修到零，用它當下限的話，下限就只能是 0，而 0 跟「抽取器回空集合」長得一模
    一樣。
    """
    import ast as _ast

    sources = _join_scan_sources() if sources is None else sources
    rows = []
    roots_by_file = {}
    for path in sources:
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        roots_by_file[path.name] = _module_dir_constants(tree)
        for fn_name, lineno, expr, guarded in _derived_join_sites(tree):
            rows.append((path.name, fn_name, lineno, expr, guarded))
    return rows, roots_by_file


# 掃到的接合總數（含已守住的）下限。**實測 2026-09-11：30 個檔案、55 個接合站點**
# （同日**三次**放寬疊起來的：先收「右運算元不是裸 `Name`」的接合 25 → 37，再收
#   「帶 `Path` 註記的**參數**也算根」37 → 53，最後收「左運算元是 `Call`／鏈式接合」
#   53 → 55，最後收「`Call` 到一支回傳型別註記是 `Path` 的函式」55 → 56。
#   第三次多出來的 2 個：`_collect_codex_images` 的
#   `(root or CODEX_IMAGE_ROOT).resolve()`，以及 `dorossi_session_workdir` 的
#   `ROOT / 'sessions' / f'…'`——後者的左邊是另一個 `BinOp`，所以**每一個鏈式接合**
#   在此之前都是隱形的。第四次多出來的 1 個是 `_gui_control.write_host_file` 的
#   `path / safe_basename(default_name)`：左邊是 `resolve_host_path(...)`、右邊是
#   `safe_basename(...)`，**兩邊同時隱形**，所以前三次一次都救不到它，而它接的正是
#   使用者完全控制的附件檔名。先前這一行寫 55、53、37、22、21。這種數字要跟著實測
#   一起改，不要照抄——它自己底下那段就是在講這件事）。
#
# ⚠️ **這個數字擋不住「放寬被 revert」**，而那正是最可能發生的回頭路。下限 25 剛好
# 等於第一次放寬**之前**的實測值，所以把形狀規則整條拿掉、站點掉回 25，這一行照樣
# 綠。同理現在的下限也擋不住掉回 37。所以真正在守這兩條放寬的是
# `test_every_join_onto_a_base_root_is_guarded_per_join` 裡的兩個**具名**金絲雀
# （`shape_only` 與 `param_rooted`），它們問的是「有沒有站點是**只**靠這條規則才
# 看得見的」——那是數字答不出來的問題。
# 下限刻意抓得比實測低一截——它要抓的是「抽取器回空集合」，不是「今天剛好幾個」。
#
# ⚠️ 這一行原本寫的是「34 個檔案、90 個接合站點」、下限 45，而那個數字**在這支
# 抽取器上從來沒有成立過**——照著它跑就是紅的。差距的來源是「接合」的定義：這支
# 只收**右運算元是變數**的接合（`ROOT / name`），字面值的那些（`ROOT / "batch.json"`）
# 由構造保證安全、不是站點。90 是把字面值一起數進去才會有的量級。
#
# 記在這裡是因為它本身就是一個教訓：**一個沒有跟著實測一起寫下的下限，等於一個
# 沒有人驗過的斷言**。而且它的失效方向剛好是最吵的那一種——永遠紅，於是下一個人
# 最省事的動作是把數字調小到綠為止，那就把正面對照組整個廢掉了。所以下面除了這個
# 數字之外，另有兩個**具名**的金絲雀（`TEMPLATES_DIR` 與 `_webrunner_shared.py`
# 的那一筆），它們問的是「範圍有沒有真的跨出 `discord_bot.py`」——那才是 2026-09-10
# 這次放寬要守住的東西，而一個純數字答不出這個問題。
_JOIN_SITE_FLOOR = 40


# 具名例外，鍵是 `(檔名, 函式名)`，值是 `(遮住幾個站點, 理由)`。每一筆都要寫理由。
#
# ⚠️ **鍵一定要含檔名。** 把掃描從一個檔案拉開到整個專案之後，只用函式名當鍵就是
# 一個 fail-open 的洞：`main` / `_sync_profile_dir_back` 這種名字在多個模組裡都有，
# 替其中一個開的例外會安靜地蓋到另一個頭上。
#
# ⚠️ **鍵的粒度是函式，不是站點——所以要另外記數量。**（2026-09-11 新增）上面那條
# 講的是「同名函式跨模組」，這條是同一個 fail-open 的另一個軸：一筆為了 A 站點寫的
# 例外，會自動蓋住同一支函式裡的 B 站點，**包含明天才加進去的 B**。這不是假想——
# 這次放寬抽取器就讓 `allocate_output_dir` 從 1 個站點變成 2 個，而第二個
# （`OUTPUT_ROOT / f"{base_name}_{i}"`）會就這樣滑進既有例外底下，沒有任何人看過。
# 實測那個滑進來的站點**不是**靠後綴變安全的：`base_name="C:/Windows/Temp/x"` 加上
# `_2` 之後仍然是 `C:\Windows\Temp\x_2`，磁碟機字首照樣整個換掉 base；它的安全性
# 跟第一個站點完全一樣、一分不多，全靠上游的 `character_folder_name`。所以這個
# 數字是**承重的**，不是記帳：`test_the_join_guard_exemptions_are_not_stale` 會
# 比對它，數量對不上就當場變紅，訊息直接說「有新站點滑進既有例外」。
#
# **列舉是 fail-open 的**，所以下面另有對帳測試確認這裡沒有過期條目（名字改掉之後，
# 例外會變成一個永遠不再命中的字串，而那個函式就這樣安靜地失去檢查）。兩筆
# 2026-09-10 新增的例外**各自還有一支前提測試**——CLAUDE.md 對 `_OWNER_ONLY_SLASH`
# 寫得很清楚：一筆沒有人驗前提的例外，活得比它的理由久。
_JOIN_GUARD_EXEMPT = {
    ("discord_bot.py", "_count_folder_images"):
        (1,
         "收的是 bot 自己從事件與佇列算出來的角色名，而且只回一個數字、"
        "不送檔案，也不讀檔案內容。"),
    ("discord_bot.py", "cmd_debug_show"):
        (1,
         "`candidate` 是**已經過守衛**的 `name` 前後各接一段字面值（`debug_` 與 "
        "`.png`）。前綴與後綴都是純追加，變不出 `/`、`\\`、`..` 或磁碟機字首，"
        "所以單一元件的性質是保留的。追不到是因為這支檢查器刻意不做字串層級的"
        "推導（那會遠比路徑推導脆弱）。"),
    ("_gui_control.py", "write_host_file"):
        (1,
         "`path / safe_basename(default_name)`，其中 `default_name` 是 `/host put` "
        "的**附件檔名**——使用者完全控制。這個站點在 2026-09-11 之前是**雙重隱形**："
        "左邊 `path` 來自 `resolve_host_path(...)`（`Call`），右邊也是 `Call`，所以"
        "前三次放寬一次都救不到它；要等到第四次（收「回傳型別註記是 `Path` 的函式"
        "呼叫」當根）才現形。`safe_basename` 是這條規則的**第三份私有拼法**"
        "（`os.path.basename` 之後再擋 `\"\"` / `.` / `..`，2026-09-11 補上 "
        "`_reject_reserved_filename`），比 `_is_unsafe_folder_name` 弱：沒有磁碟機"
        "字首錨定。**但實測沒有逃逸**：24 個惡意輸入（`../evil`／`..\\\\evil`／"
        "`a/../../evil`／`/etc/passwd`／`C:/Windows/Temp/x`／`C:evil`／"
        "`\\\\\\\\server\\\\share\\\\x`／`..`／`.`／空字串／`NUL`／`COM1`／`x.`／"
        "`a\\\\tb`／`...` 等）逐一接到目的資料夾後面，**逃出去的有 0 個**——"
        "`os.path.basename` 在把 `\\\\` 正規化成 `/` 之後是可靠的。跟共用守衛真正的"
        "分歧只有全點名字（`...` / `....`），而它們寫檔時是 errno 13 **大聲**失敗，"
        "不會靜靜寫錯地方。前提由 `test_the_attachment_name_guard_lets_nothing_"
        "escape` 對帳——那一支是行為測試不是註解，改弱 `safe_basename` 會當場變紅。"),
    ("dorossi_backend.py", "dorossi_session_workdir"):
        (1,
         "`DOROSSI_CC_WORKDIR / 'sessions' / f'{uid}_{sid}'`——2026-09-11 放寬"
        "「左邊可以是鏈式接合」之後才看得見的。函式自己的 docstring 早就寫著「`uid` "
        "是數字 id、`sid` 是 `s<N>` 槽位 id，所以這個接合是安全的」，而那是一句"
        "**對呼叫端的假設**，正是這道守門存在的理由。實測支持它：全專案只有一個正式"
        "呼叫端（`discord_bot._dorossi_session_cwd`），而 `sid` 的唯一產生處是 "
        "`_dorossi_new_session` 的 `sid = f's{seq}'`（`seq` 是 int）。這個前提由 "
        "`test_the_session_slot_id_is_built_from_an_int` 對帳——沒有那一支，這段"
        "理由就跟 docstring 一樣只是一句沒人查的話。"
        "⚠️ 殘留風險寫清楚：槽位 id 也會**寫進磁碟的 session 狀態再讀回來**，手改過"
        "的狀態檔可以帶進任意字串（同 `_run_progress.resume_folder` 那一類）。今天"
        "不列為缺陷，因為改得動那個檔的人已經有主機寫入權；真的要收緊的話，正確的"
        "位置是這支函式本身（它是 `(uid, sid)` 變成路徑的唯一入口），而不是呼叫端。"),
    ("_process_control.py", "newest_dependency_mtime"):
        (1,
         "`rel` 逐一走過 `rel_paths`，而 `rel_paths` 是參數、不是模組層字面值序列，"
        "所以紀律 (3) 看不到它。實測全專案只有一個呼叫端"
        "（`_process_control.py` 的 `find_stale_components`），餵的是 "
        "`STALE_COMPONENTS` 這個模組層 dict 裡的字面值元組；`components` 參數可"
        "注入是為了讓判定邏輯測得起來，注入的值也還是同一支行程裡的常數。"
        "接合的結果只餵給 `.stat().st_mtime`——讀 metadata，不開檔、不寫檔，"
        "而且整支函式包在 `except OSError: return None` 裡（它是診斷用的）。"),
    ("_webrunner_shared.py", "allocate_output_dir"):
        (2,
         "真正的守衛在上游的 `character_folder_name`（`_is_safe_folder_component`）"
        "——那支的 docstring 明寫守衛只能放在那裡，放到這裡會讓 bot 的 `/gen plan` "
        "預覽與實跑分岔，正是模組邊界規則在防的漂移。前提由 "
        "`test_the_output_dir_exemption_still_points_at_a_real_guard` 對帳。"),
    ("_gui_control.py", "set_shell_cwd"):
        (1,
         "同 `resolve_host_path` 那一筆的理由，而且更直接：`/host sh cd` 的用途"
        "就是把工作目錄切到主機上任何地方，限制在專案目錄底下等於讓這個指令失去"
        "意義。**唯一的寫入者**是 `cmd_sh`，而它第一行就自己比對 `OWNER_USER_ID`"
        "（不只靠派發層的閘）。讀的人比較多——`run_shell`（`/host sh run`、"
        "`/watch` 的 shell 動作、`/schedule` 的排程項目）與 `job_run`"
        "（`/host job run`），`host` / `watch` / `schedule` 都在 "
        "`_OWNER_ONLY_GROUPS`，所以整圈都在同一道閘後面。"
        "閘門一樣是呼叫端的身分"
        "檢查。它到 2026-09-10 為止**完全沒被這道守門看見**——`_SHELL_CWD: Path "
        "= PROJECT_ROOT` 是 `ast.AnnAssign`，而擷取器只讀 `ast.Assign`。"
        "前提由 `test_the_host_path_exemption_still_has_its_owner_gate` 對帳。"),
    ("_gui_control.py", "resolve_host_path"):
        (1,
         "刻意不做沙箱：這條路是擁有者專屬的「用對話操作整台電腦」，限制在專案目錄"
        "內等於讓那句話名不副實。真正的閘門是呼叫端的身分檢查（`host` 群組在 "
        "`_OWNER_ONLY_GROUPS` 裡）。前提由 "
        "`test_the_host_path_exemption_still_has_its_owner_gate` 對帳。"),
    ("webrunner_novelai.py", "_sync_profile_dir_back"):
        (2,
         "`relpath` 是**參數**，安全性取決於呼叫端，而跨函式推導在只看得到一個模組"
        "的情況下是 fail-open 的（別的模組也可能呼叫它），所以刻意不做成紀律。"
        "前提——每一個呼叫端傳的都是模組層字面值序列的迴圈變數——由 "
        "`test_the_dir_sync_exemption_still_only_ever_sees_constants` 對帳。"
        "**兩個站點的右運算元是同一個 `relpath`**（`src = snapshot / relpath` 與 "
        "`dst = CHROME_PROFILE_DIR / relpath`），所以數字 2 不代表兩個獨立的前提。"
        "前者是 2026-09-11 放寬「帶 `Path` 註記的參數也算根」之後才看見的——變的是"
        "**左邊的根**，不是右邊的運算元。那個根同樣不是外來輸入："
        "`snapshot` 來自 `_sync_chrome_profile_back(snapshot: Path)`，而它三個呼叫"
        "端傳的是 `_snapshot_chrome_profile()` 的回傳值或模組層全域 "
        "`_CURRENT_SNAPSHOT_PROFILE`。對帳測試**只驗 `relpath`**，那是刻意的"
        "（根的來源是另一個問題）；寫在這裡是為了讓數字 2 解釋得通。"),
    ("webrunner_je_only.py", "_sync_profile_dir_back"):
        (2,
         "同 webrunner_novelai 那一筆；兩個變體必須同步，站點數也一樣是 2。"),
    ("_webrunner_shared.py", "generate_loop"):
        (1,
         "同一個站點有**兩個**追不到的運算元，理由不同，**兩個都要看**——只處置其中"
        "一個，站點照樣是 offender（實測過）。(1) `character_name` 是參數，真正的"
        "守衛在上游的 `character_folder_name`（`_is_safe_folder_component`）："
        "正式呼叫端只有 `run_batch` 一個，那裡的 `char_name` 只被指派一次、值就是"
        "那支投影的輸出。實測 18 個對抗性佇列輸入（`..`／`../../../..`／`a/b`／"
        "`/etc/passwd`／`C:/Windows/Temp/x`／`NUL`／`con`／`a\\tb`／`a..b`／`.`／"
        "`...`／空字串／`\\\\server\\share` …）接合後**一律是兩層、沒有一個含 "
        "`..`**。守衛只能放在投影那一端（那支的 docstring 寫了理由：放到下游會讓 "
        "bot 的 `/gen plan` 預覽與實跑分岔），而跨函式推導 fail-open，所以走例外。"
        "(2) `i` 是 `start_index + n`，形狀規則不模擬 `BinOp Add`——但 `:04d` 這個 "
        "format spec 自己就是保證：實測 `format('..', '04d')`、`format('1', '04d')`、"
        "`format(1.5, '04d')` 全部 `ValueError`，非整數根本展不出來。"
        "(3) `ts` 走 `strftime`，已由紀律 (c) 放行。"),
    ("webrunner_novelai.py", "_session_entry_present"):
        (2,
         "`base` 與 `relpath` 都是**參數**，跨函式推導在只看得到一個模組時是 "
        "fail-open 的（同 `_sync_profile_dir_back` 那兩筆）。這一筆另有一個量得出來"
        "的上限：兩個站點的結果**只被讀、不被寫**——一支是 `.exists()`，另一支經 "
        "`_leveldb_manifest_ok` 只做 `read_text('CURRENT')` ＋ `.is_file()`——而回傳"
        "值唯一的消費端是 `_snapshot_chrome_profile` 那則 stderr 診斷訊息，所以答錯"
        "的代價是印錯一行字。呼叫端每個變體恰好 2 個，都在 `_snapshot_chrome_"
        "profile`：`base` 傳模組層常數（`CHROME_PROFILE_SNAPSHOT` / "
        "`CHROME_PROFILE_DIR`），`relpath` 是 `_SESSION_CRITICAL + "
        "_SESSION_CRITICAL_DIRS` 兩個模組層字面值 tuple 串接後的推導式變數。"
        "⚠️ 紀律 (c) 追不到有**兩個獨立原因**，只補一個沒有用："
        "`_structurally_safe_components` 的第 (3) 條只走 `ast.For` 陳述而這裡是"
        "推導式，且 `_module_literal_sequences` 只收模組層的字面值指派而 `entries` "
        "是函式內的 `BinOp Add`。"),
    ("webrunner_je_only.py", "_session_entry_present"):
        (2,
         "同 webrunner_novelai 那一筆；兩個變體的這一支實測**程式碼完全相同**"
        "（只有 docstring 依慣例縮短成指路），必須同步。"),
    ("_bot_prompts.py", "load_prompt"):
        (1,
         "`filename` 是**參數**，同 `_sync_profile_dir_back` 那兩筆的理由——跨函式"
        "推導在只看得到一個模組時是 fail-open 的。這一筆守不成紀律還有第二個"
        "理由：守衛 `_is_unsafe_folder_name` 住在 `discord_bot.py`，而 "
        "`_bot_prompts` 是 passive、stdlib-only 的共用模組，模組邊界不准它 import "
        "bot——照做只會多出第四份同判準的拷貝。前提（所有非測試呼叫端傳的都是字串"
        "字面值）由 `test_the_prompt_loader_exemption_still_only_ever_sees_literals` "
        "對帳。"),
    # `_template_path` 這一筆已於 2026-09-10 拿掉：它原本手寫第三份紀律（字元
    # 黑名單 ＋ `relative_to`），現在改走具名的那兩支，檢查器認得了。留著就是替
    # 未來的回歸先開好一張免死金牌。當初的病值得記住——黑名單那一半正是
    # `_is_unsafe_folder_name` 已經換掉的錯判準（漏掉磁碟機字首錨定），今天不出事
    # 只因為 `relative_to` 那一半剛好補上，也就是**安全來自巧合**。
    # `test_the_template_blacklist_alone_would_let_a_drive_anchor_through`
    # 把那個示範釘住了。
    # `_handle_single_image_done` 這一筆已於 2026-09-09 拿掉：它改用紀律 (b)
    # （`_within_allowed_roots(img_path, (OUTPUT_ROOT,))`），檢查器認得了，所以
    # 例外變成「永遠不再命中」——留著就是替未來的回歸先開好一張免死金牌，而
    # `test_the_join_guard_exemptions_are_not_stale` 的反向斷言正是為此存在。
}


# 第二類例外：右邊的名字由一支**私有白名單 regex** 守著，而那個字元集嚴格落在共用
# 守衛裡面。鍵同上，值是 `(遮住幾個站點, 理由)`。
#
# ⚠️ **為什麼是第二個 dict，而不是往 `_JOIN_GUARD_EXEMPT` 裡加兩筆。** 那個 dict
# 的前提是「守衛放不進來，閘門在別的地方」，而 `test_the_host_path_exemption_
# still_has_its_owner_gate` 就是去驗那個前提的——它把**所有** `_gui_control.py` 的
# 例外都當成「閘門在呼叫端身分檢查」那一族。這兩筆的前提不是那個，是「私有 regex
# 比共用守衛嚴」。塞進同一個 dict 會變成：兩種前提共用一支只驗第一種前提的對帳
# 測試，而寫下來的理由跟被驗的東西不是同一件事。那正是這個 repo 一直在修的
# 單向對帳形狀，只是換到「一個 dict 兩種語意」這個軸上。
#
# ⚠️ **理由不可以寫「共用守衛在 `discord_bot`，模組邊界不准 import，照做會多出
# 第四份拷貝」。** 那句話是錯的，而且三十秒就能被推翻：`_webrunner_shared` 是
# CLAUDE.md 明列的被動共用模組，`_gui_control` 匯入它完全合法，而那裡就有
# `_is_safe_folder_component` 與 `_is_single_path_component`。真正的理由要用量的
# （見下面那支測試的 docstring）：一支接受範圍一模一樣（換了等於空儀式），另一支
# 更嚴、換過去今天就會擋掉 32 個合法輸入。
_JOIN_GUARD_PRIVATE_WHITELIST = {
    ("_gui_control.py", "layout_path"):
        (1,
         "`key` 由私有白名單 `LAYOUT_NAME_RE`（`^[A-Za-z0-9_-]{1,40}$`）守著，"
         "字元集裡沒有分隔符、`:`、`.`，長度也有上限。前提——**這支 regex 放行的"
         "東西，`_is_unsafe_folder_name` 必須也放行**——由 "
         "`test_the_gui_name_whitelists_stay_inside_the_shared_guard` 對帳。"),
    ("_gui_control.py", "macro_path"):
        (1,
         "同 `layout_path`，白名單是 `MACRO_NAME_RE`，同一個字元集。兩支共用一支"
         "前提測試，而那支測試是從原始碼抽 regex 的，所以只改其中一支也會被抓到。"),
    ("_platform_runtime.py", "state_dir"):
        (1,
         "接上去的是**平台名**，而它先過 `normalise_platform()`——私有白名單 "
         "`_VALID_NAME`（`^[a-z][a-z0-9_-]{0,31}$`），字元集裡沒有分隔符、`:`、`.`，"
         "長度也有上限，不合法的一律退回預設平台名（不是原樣放行）。前提由 "
         "`test_platform_processes.test_a_platform_name_can_never_become_a_path` "
         "與 `test_the_platform_name_whitelist_stays_inside_the_shared_guard` 兩向釘住。"),
    ("_platform_runtime.py", "platform_file"):
        (1,
         "`leaf` 是 `<平台名>.<基底檔名>` 或 `.<平台名>.<基底檔名>`：平台名同上，"
         "基底檔名來自呼叫端的 `Path(base).name`（`name` 依定義只有一層，`..` 與"
         "分隔符都進不來）。兩段都是單一元件，接起來還是單一元件。"),
    ("_platform_runtime.py", "token_file"):
        (1,
         "`f\"{normalise_platform(platform)}_bot_token.md\"`——平台名同上，前後各接"
         "一段字面值，純追加變不出 `/`、`\\\\`、`..` 或磁碟機字首（與 "
         "`cmd_debug_show` 那筆同一個形狀）。"),
}


def _gui_name_whitelists(source) -> dict:
    """`{建構函式名: (regex 字面值, 接在 key 後面的字面值後綴)}`，用 AST 抽。

    **不 import `_gui_control`**：那會把桌面自動化函式庫一起拉進來，而這裡只需要
    兩個字串。後綴也**不在測試裡再寫一次 `".json"`**——哪天有人改了副檔名，寫死的
    前提測試會繼續驗一個不存在的組合，看起來還是綠的（[[空選集看起來像乾淨結果]]
    的同一個形狀，只是換成「驗錯對象」）。
    """
    import ast as _ast

    tree = _ast.parse(source)
    patterns = {}
    for node in tree.body:
        if (isinstance(node, _ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], _ast.Name)
                and isinstance(node.value, _ast.Call)
                and isinstance(node.value.func, _ast.Attribute)
                and node.value.func.attr == "compile"
                and node.value.args
                and isinstance(node.value.args[0], _ast.Constant)):
            patterns[node.targets[0].id] = node.value.args[0].value

    wanted = {fn for _f, fn in _JOIN_GUARD_PRIVATE_WHITELIST}
    out = {}
    for fn in _ast.walk(tree):
        if not isinstance(fn, _ast.FunctionDef) or fn.name not in wanted:
            continue
        used = [n.func.value.id for n in _ast.walk(fn)
                if isinstance(n, _ast.Call)
                and isinstance(n.func, _ast.Attribute)
                and n.func.attr in ("match", "fullmatch")
                and isinstance(n.func.value, _ast.Name)
                and n.func.value.id in patterns]
        suffixes = ["".join(v.value for v in n.values
                            if isinstance(v, _ast.Constant))
                    for n in _ast.walk(fn) if isinstance(n, _ast.JoinedStr)]
        if len(used) == 1 and len(suffixes) == 1:
            out[fn.name] = (patterns[used[0]], suffixes[0])
    return out


def test_the_join_shape_rule_rejects_the_near_misses():
    """`_shape_safe_operand` 的合成對照組。

    真實語料裡**每一個** f-string 接合都是安全的（9/9），所以整條規則放寬到「只看
    插值、不看字面值」在語料上照樣 100% 全綠。這一格就是那個盲點的對照組：規則的
    否定面在 repo 裡沒有任何資料在踩，不寫合成案例的話，把 `_literal_segments_are_
    clean` 整支刪掉不會有東西變紅。

    ⚠️ 前四個「危險」案例是我自己第一版規則**真的判錯**的（實測 3/6 錯），不是
    假想的近似反例。
    """
    import ast as _ast

    cases = [
        # (原始碼, 已過守衛的名字, 應該安全嗎, 說明)
        ('f"{int(t)}.png"', set(), True, "int() 插值 + 乾淨副檔名"),
        ('f"_shot_{int(time.time() * 1000)}.png"', set(), True, "真實語料原句"),
        ('f"{int(ts * 1000) % 1000:03d}"', set(), True, "spec 是無害的常數"),
        ('f"{p.name:>10}"', set(), True, ".name + 只有對齊的 spec"),
        ('f"{p.stem}_2"', set(), True, ".stem 也是單一層元件"),
        ('f"{len(xs)}"', set(), True, "len() 只會是數字"),
        ('f"debug_{name}.png"', {"name"}, True, "已過守衛的名字 + 純追加前後綴"),
        ('f"{int(t)}/evil"', set(), False, "字面值帶正斜線"),
        ('f"{int(t)}\\\\evil"', set(), False, "字面值帶反斜線"),
        ('f"../{int(t)}"', set(), False, "字面值帶上層"),
        ('f"C:{int(t)}"', set(), False, "字面值帶磁碟機字首"),
        ('f"{int(t)}..{int(u)}"', set(), False, "字面值含兩點"),
        ('f"{p.name:/>10}"', set(), False, "spec 拿斜線當 fill 字元"),
        ('f"{p.name:{sep}>10}"', set(), False, "spec 是巢狀插值"),
        ('f"{name}/{other}"', {"name", "other"}, False,
         "兩個插值都過了守衛，但字面值自己帶分隔符"),
        ('f"debug_{name}.png"', set(), False, "同一句，但名字沒過守衛"),
        ('f"{key}.json"', set(), False, "`_gui_control` 那兩個站點的形狀"),
        ('f"{base_name}_{i}"', set(), False, "`allocate_output_dir` 的第二個站點"),
        ('(rid or "unknown")', set(), False, "BoolOp 不在安全形狀裡"),
        ('parts[0]', set(), False, "Subscript 不在安全形狀裡"),
        ('f"{socket.gethostname()}"', set(), False, "不認得的方法呼叫"),
    ]
    wrong = [
        (src, want, note) for src, guarded, want, note in cases
        if _shape_safe_operand(_ast.parse(src, mode="eval").body, guarded) != want
    ]
    assert not wrong, (
        "形狀規則對這些案例的答案不對（`(原始碼, 應該是, 說明)`）：%s。\n"
        "`format('a', '/>10')` 是 `'/////////a'`——fill 字元會直接進到結果字串，"
        "所以 spec 那一半也要檢查。" % wrong)


def test_the_literal_sequence_rule_covers_both_spellings_and_nothing_more():
    """規則 (3) 的合成對照組：字面值序列的兩種拼法都要放行，近似案例都要擋。

    2026-09-18 之前規則只認「先綁到模組層名字」那一種，行內
    `for folder in ("docs", "commands"):` 被判成不可知。兩者的保證完全相同——值
    整個來自本 repo 的原始碼——差別只在有沒有先取個名字，而那與安全性無關。

    ⚠️ **兩個方向的殺傷力差很多，而且都不是「看起來那樣」。**
    **must-allow（放行）這半，真實語料今天確實殺得掉**——實測過：放寬之前整套跑
    出來的那一筆紅就是它。但那只靠**一格**語料（`audit_simplified_chars.sources()`
    的 `REPO_ROOT / folder`），而那一格隨時可能因為一次完全無關的重構而消失；一旦
    消失，把這條規則刪掉就沒有任何東西會紅——語料只會變得更保守，每一個下限照樣過。
    **must-block（擋下）這半則是語料永遠答不出來的**：把判準放寬成「只要是 Tuple
    就算」，違規只會變**少**，整套照樣全綠。判準必須是「元素全部是字串字面值」，
    不是「iter 長得像一個序列」——`sorted(("a", "b"))` 與 `list(NAMES)` 的內容都是
    看不到的。
    """
    import ast as _ast

    cases = [
        # (iter 的原始碼, 模組層字面值序列的名字, 應該安全嗎, 說明)
        ('("docs", "commands")', set(), True, "行內 tuple 字面值"),
        ('["docs", "commands"]', set(), True, "行內 list 字面值"),
        ('{"docs", "commands"}', set(), True, "行內 set 字面值"),
        ('("Default/Network/Cookies",)', set(), True,
         "帶分隔符也算：保證是「來自原始碼」，不是「單一層元件」"),
        ('NAMES', {"NAMES"}, True, "模組層字面值序列（原本就認得的那一種）"),
        ('NAMES', set(), False, "同一個名字，但模組層沒有對應的字面值序列"),
        ('()', set(), False, "空序列：沒有元素可判，維持原本 `value.elts` 的要求"),
        ('("docs", 3)', set(), False, "元素不全是字串"),
        ('("docs", NAME)', set(), False, "有一個元素不是字面值"),
        ('(f"{x}", "b")', set(), False, "f-string 不是 `Constant`"),
        ('list(NAMES)', set(), False, "呼叫的回傳值——內容看不到"),
        ('sorted(("a", "b"))', set(), False, "包進呼叫裡就看不到了"),
        ('NAMES + EXTRA', set(), False, "運算式不是字面值序列"),
    ]
    wrong = []
    for src, seqs, want, note in cases:
        fn = _ast.parse(
            "def f():\n    for item in %s:\n        pass\n" % src).body[0]
        if ("item" in _structurally_safe_components(fn, seqs)) != want:
            wrong.append((src, want, note))
    # 拆包目標不是裸 `Name`，整條規則不適用。
    unpack = _ast.parse(
        'def f():\n    for a, b in (("x", "y"),):\n        pass\n').body[0]
    if _structurally_safe_components(unpack, set()) & {"a", "b"}:
        wrong.append(("for a, b in ((...),)", False, "拆包目標不是裸 `Name`"))
    assert not wrong, (
        "字面值序列規則對這些案例的答案不對（`(iter, 應該是, 說明)`）：%s。" % wrong)


def test_the_join_shape_name_derivation_is_not_fail_open():
    """`_shape_safe_local_names` 只要有**一次**不安全的指派就不可以放行。

    「看到一個安全的指派就把名字收進來」是隔壁 `_structurally_safe_components` 的
    寫法，而放寬抽取器讓那個形狀第一次真的碰到 f-string 那一類站點。這支釘住的是
    收緊之後的判準——實測收緊影響 152 支函式的名字集。
    """
    import ast as _ast

    cases = [
        ("單次安全指派", "    stamp = time.strftime('%Y')\n", True),
        ("安全在前、不安全的再指派在後",
         "    stamp = time.strftime('%Y')\n    stamp = payload\n", False),
        ("不安全在前、安全在後（判準是所有指派，不是最後一次）",
         "    stamp = payload\n    stamp = time.strftime('%Y')\n", False),
        ("名字同時是 for 目標",
         "    stamp = time.strftime('%Y')\n    for stamp in items:\n        pass\n",
         False),
        ("名字同時被 except as 綁",
         "    stamp = time.strftime('%Y')\n    try:\n        g()\n"
         "    except OSError as stamp:\n        pass\n", False),
        ("walrus", "    if (stamp := payload):\n        pass\n", False),
        ("拆包指派", "    stamp, other = pair\n", False),
    ]
    wrong = []
    for label, body, want in cases:
        fn = _ast.parse(f"def f(payload, items, pair):\n{body}    return 1\n").body[0]
        if ("stamp" in _shape_safe_local_names(fn)) != want:
            wrong.append((label, want))
    # 參數自己也不可以算安全（它的值取決於呼叫端，而呼叫端可能在別的模組）。
    param_fn = _ast.parse("def f(stamp):\n    return 1\n").body[0]
    if "stamp" in _shape_safe_local_names(param_fn):
        wrong.append(("名字是參數", False))
    assert not wrong, (
        "形狀名字推導的答案不對（`(案例, 應該是)`）：%s。放行方向錯的話，"
        "`stamp = time.strftime(...)` 之後被重新指派成使用者輸入，接合仍然會被"
        "判成安全。" % wrong)


def test_the_path_annotation_filter_answers_by_type_not_by_spelling():
    """`_is_path_annotation` 的合成近似案例：正反都問。

    這支過濾器決定「哪些參數算接合的根」。放太寬（`list[Path]` 也算）會製造出一
    整類不存在的站點；放太窄（只認裸 `Path`）會讓一整類真站點隱形。本 repo 今天
    只寫得出三種註記，所以**真實語料對這支過濾器幾乎沒有鑑別力**——`Path` 與
    `Path | None` 都對、`list[Path]` 都錯的爛規則（`"Path" in text`）在真實語料上
    只差 5 個站點，很容易看成雜訊。合成案例才問得出「規則是照型別的意義寫的，還是
    照今天的語料寫的」。
    """
    import ast as _ast

    cases = [
        ("Path", True),
        ("Path | None", True),
        ("None | Path", True),
        ("Optional[Path]", True),
        ("pathlib.Path", True),
        ("'Path'", True),                 # 字串註記
        ("'Path | None'", True),
        ("Union[str, Path]", True),
        ("list[Path]", False),            # 容器：一堆路徑不是一個路徑
        ("tuple[Path, ...]", False),
        ("Sequence[Path]", False),
        ("Iterable[Path]", False),
        ("dict[str, Path]", False),
        ("set[Path]", False),
        ("List[Path]", False),            # typing 的大寫拼法也要擋
        ("str", False),
        ("int", False),
        ("float", False),
        ("None", False),
        ("PurePath", False),              # 不是 `Path` 就不是
        ("'不是合法語法['", False),        # 字串註記 parse 不動 → 保守判否
    ]
    wrong = []
    for text, want in cases:
        node = _ast.parse(f"x: {text}", mode="exec").body[0].annotation
        if _is_path_annotation(node) != want:
            wrong.append((text, want))
    assert not wrong, (
        "`_is_path_annotation` 這幾個案例答錯了（`(註記, 應該是)`）：%s。"
        % wrong)
    # 反向：一支沒有註記的參數不可以被當成根，否則 `count / total` 這種**算術**
    # 會被標成路徑穿越。一個把算術判成穿越的守門就是會被關掉的守門。
    fn = _ast.parse("def f(root, n: int, p: Path):\n    return 1\n").body[0]
    assert _path_typed_params(fn) == {"p"}, _path_typed_params(fn)
    # `*args` / `**kwargs` 綁的是容器，跟 `list[Path]` 同一個道理。
    star = _ast.parse("def f(*a: Path, **k: Path):\n    return 1\n").body[0]
    assert _path_typed_params(star) == set(), _path_typed_params(star)


def _param_join_left_operands(tree):
    """回 `(函式名, 行號, 參數名, 註記原文或 None)`：接合的**左**運算元正好是這支
    函式自己的參數。去重（同一行同一個名字只算一次）。"""
    import ast as _ast

    out = set()
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        args = fn.args
        by_name = {a.arg: a for a in (list(args.posonlyargs) + list(args.args)
                                      + list(args.kwonlyargs))}
        for n in _ast.walk(fn):
            if not (isinstance(n, _ast.BinOp) and isinstance(n.op, _ast.Div)
                    and isinstance(n.left, _ast.Name)
                    and n.left.id in by_name):
                continue
            ann = by_name[n.left.id].annotation
            out.add((fn.name, n.lineno, n.left.id,
                     _ast.unparse(ann) if ann is not None else None))
    return sorted(out, key=lambda r: (r[0], r[1], r[2]))


def _param_rooted_join_sites(tree) -> set:
    """回 `{(函式名, 行號)}`：**只有**因為「帶 `Path` 註記的參數也算根」才看得見的
    那些接合站點。金絲雀用——一個純數字的下限抓不到「這條放寬被revert」。"""
    import ast as _ast

    out = set()
    for fn_name, lineno, _param, ann in _param_join_left_operands(tree):
        if ann is None:
            continue
        node = _ast.parse(f"x: {ann}").body[0].annotation
        if _is_path_annotation(node):
            out.add((fn_name, lineno))
    return out


def test_a_parameter_used_as_a_path_declares_itself_one():
    """把 `Path` 註記當過濾器，就等於把「沒寫註記」變成一個 fail-open 的方向。

    ⚠️ 這是上面那支過濾器的**代價**，而且它安靜：一個沒有註記的參數，被拿去接
    `param / rel`，抽取器判不出它是路徑還是分母，於是整個站點消失——沒有紅燈、沒有
    例外、沒有任何地方記下「這裡沒被看過」。**這正是這一整輪放寬要消滅的形狀，不能
    在修掉它的同時又開一個新的。**

    可以直接關掉這個方向，是因為判準完全不靠命名慣例：**被拿去當 `/` 左運算元的
    參數，就是被當成路徑（或分母）在用**——結構上看得到，跟它叫 `root` 還是 `base`
    無關。（同一輪剛拆掉一個 `startswith(("cmd_", "mcmd_"))` 的可見性過濾器，理由
    一模一樣：守門的視野不可以取決於別人怎麼取名。）

    所以規則是「拿去除的參數要有型別註記」。註記成 `int` / `float` 的照樣過——實測
    這裡有 4 個（`_fmt_size` 的 `n / 1024` ×3、`wait_for_quota_recovery` 的
    `waited_sec / 60`），它們**應該**被過濾器排除，這支測試不是在逼人寫 `Path`。
    它只是要求「你得說它是什麼」。
    """
    import ast as _ast

    rows = []
    for path in _join_scan_sources():
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        for fn_name, lineno, param, ann in _param_join_left_operands(tree):
            rows.append((path.name, fn_name, lineno, param, ann))
    # 正面對照：先確定真的掃到東西。一個回空集合的抽取器，下面那句斷言也會過。
    # 實測 2026-09-11：23 個（其中 0 個沒有註記）。
    assert len(rows) >= 15, (
        f"只掃到 {len(rows)} 個「左運算元是參數」的接合，太少了——抽取器大概是"
        "壞掉或掃描語料縮水了，而不是專案真的沒有這種寫法。")
    naked = [r for r in rows if r[4] is None]
    assert not naked, (
        "這些參數被拿去當 `/` 的左運算元，卻沒有型別註記：%s。"
        "接合守門的根過濾器認的是 `Path` 註記，所以它們對守門是**隱形的**——"
        "既不會被列成站點，也不會出現在任何例外清單裡。補上註記即可："
        "是路徑就寫 `Path`（站點會現形，再依實際情況處置或列例外），"
        "是數字就寫 `int` / `float`（過濾器會正確地略過它）。" % (naked,))


def test_the_parameter_annotation_rule_actually_bites():
    """上一支的合成對照組：它的語料今天是乾淨的（0 個沒註記），所以**把整段刪掉
    也會綠**。一個永遠不會失敗的斷言不是守門，是裝飾。

    這裡直接餵一個沒有註記的參數進去，確認同一支抽取器抓得到；再餵一個有註記的，
    確認它不會把每一個參數都當成違規（那樣就變成一支狼來了的守門）。
    """
    import ast as _ast

    bad = _ast.parse("def f(root, rel):\n    return root / rel\n")
    got = _param_join_left_operands(bad)
    assert got == [("f", 2, "root", None)], got
    good = _ast.parse("def f(root: Path, rel):\n    return root / rel\n")
    assert _param_join_left_operands(good) == [("f", 2, "root", "Path")]
    # 左運算元是**本地變數**而不是參數的，不歸這支管（它由 `_derived_join_sites`
    # 那條路處理）。抓進來就會變成一支對所有 `/` 開火的守門。
    local = _ast.parse("def f(x):\n    root = X / 'a'\n    return root / x\n")
    assert _param_join_left_operands(local) == []


def _join_guard_definitions(trees) -> dict:
    """`{守衛名: ["檔名:行號", …]}`——`trees` 是 `{檔名: ast.Module}`。

    抽成 helper 是為了讓合成對照組問得到它：真實語料今天**沒有同名兩份**，所以
    「找不到重複」與「這段檢查被刪掉」在輸出上一模一樣（§8.8(A3)）。
    """
    import ast as _ast

    where: dict = {}
    for filename, tree in trees.items():
        for node in _ast.walk(tree):
            if (isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                    and node.name in _JOIN_GUARDS):
                where.setdefault(node.name, []).append(
                    f"{filename}:{node.lineno}")
    return where


def _join_guard_duplicates(where: dict) -> list:
    """哪些守衛名字有不只一份實作。"""
    return sorted((name, sorted(spots))
                  for name, spots in where.items() if len(spots) > 1)


def test_the_duplicate_guard_detector_actually_bites():
    """上一支的合成對照組。**它不是裝飾，它補的是一個實際發現的漏洞**：

    2026-09-11 變異測試把重複偵測的門檻從 `len(spots) > 1` 改成 `> 99`，整組測試
    **照樣全綠**——因為今天真的沒有同名兩份，於是「偵測失效」與「語料乾淨」的輸出
    一模一樣（§8.8(A3)）。加上這一支之後同一個變異才死得掉。
    """
    import ast as _ast

    name = sorted(_JOIN_GUARDS)[0]
    one = {"a.py": _ast.parse(f"def {name}(x):\n    return True\n")}
    assert _join_guard_duplicates(_join_guard_definitions(one)) == []
    # 同一個名字出現在兩個檔案裡 → 必須抓到。
    two = dict(one, **{"b.py": _ast.parse(f"def {name}(x):\n    return False\n")})
    got = _join_guard_duplicates(_join_guard_definitions(two))
    assert got == [(name, ["a.py:1", "b.py:1"])], got
    # 同一個檔案裡重複定義（後者靜靜蓋掉前者）也算——那更難看出來。
    same = {"a.py": _ast.parse(
        f"def {name}(x):\n    return True\n\n\ndef {name}(x):\n    return False\n")}
    assert _join_guard_duplicates(_join_guard_definitions(same)) == [
        (name, ["a.py:1", "a.py:5"])]
    # 不在清單裡的名字不歸它管，否則它會對整個專案的同名 helper 亂叫。
    other = {"a.py": _ast.parse("def helper(x):\n    return 1\n"),
             "b.py": _ast.parse("def helper(x):\n    return 2\n")}
    assert _join_guard_duplicates(_join_guard_definitions(other)) == []


def test_each_join_guard_name_resolves_to_exactly_one_implementation():
    """`_JOIN_GUARDS` 用**名字**認人，所以「同名兩份實作」是它最便宜的破法。

    在任何一個被掃描的模組裡寫一支同名、但更寬鬆（甚至恆回 False）的 helper，
    它守住的每一個站點都會安靜地繼承這份清單的信任。既有的對帳測試問的是「這個
    名字解析得到函式嗎」——同名兩份的答案是「解析得到」，所以它照樣綠。

    順帶把反向也釘住：清單裡的每個名字都必須**至少**有一份實作，否則就是一筆改名
    之後永遠對不到東西的死字串（同 CLAUDE.md 對 `_OWNER_ONLY_SLASH` 的說法）。
    """
    import ast as _ast

    trees = {path.name: _ast.parse(path.read_text(encoding="utf-8"), str(path))
             for path in _join_scan_sources()}
    where = _join_guard_definitions(trees)
    missing = sorted(set(_JOIN_GUARDS) - set(where))
    assert not missing, (
        f"`_JOIN_GUARDS` 這幾個名字在掃描語料裡找不到任何實作：{missing}。"
        "改名或刪除時要一起更新這份清單——不然它就是一筆永遠對不到東西的死字串，"
        "而守門仍然在跑、所有測試仍然綠。")
    duplicated = _join_guard_duplicates(where)
    assert not duplicated, (
        "這些守衛名字有不只一份實作：%s。`_JOIN_GUARDS` 認的是名字，所以其中一份"
        "會安靜地繼承另一份的信任——一支寬鬆的同名 helper 就能把整條紀律架空。"
        "要嘛共用一份，要嘛取不同的名字並各自列進清單（第四份實作"
        "`_run_progress._is_unsafe_folder_component` 就是為了這個才不同名："
        "`_webrunner_shared` 自己 import `_run_progress`，反過來 import 是循環）。"
        % (duplicated,))


_HOSTILE_ATTACHMENT_NAMES = (
    "../evil", "..\\evil", "../../evil", "a/../../evil", "a\\..\\..\\evil",
    "/etc/passwd", "\\Windows\\System32\\x", "C:/Windows/Temp/x", "C:evil",
    "\\\\server\\share\\x", "//server/share/x", "..", ".", "...", "....",
    "", "   ", "NUL", "COM1", "x.", "a\tb", "a\x00b", "./../x", "~/x",
)


def test_the_derived_root_rule_answers_the_near_misses():
    """`_denotes_derived` 的合成近似案例。

    ⚠️ **這一支補的是兩個變異存活的洞。** 2026-09-11 的變異測試把
    `all(...)` 改成 `any(...)`（`or` 只要有一邊是路徑就放行）——**整組照樣全綠**，
    因為真實語料裡唯一的 `or` 站點是 `root or CODEX_IMAGE_ROOT`，兩邊都是路徑，
    `any` 跟 `all` 給一樣的答案。一個沒有鑑別力的語料，讓 docstring 裡那句「要求
    每一個分支」變成一句沒人驗的話（§8.8(A3)）。
    """
    import ast as _ast

    derived = {"ROOT", "p"}
    fns = frozenset({"resolve_host_path"})
    cases = [
        ("ROOT", True),                       # 裸名字
        ("other", False),
        ("ROOT.resolve()", True),
        ("ROOT.expanduser().resolve()", True),
        ("ROOT.parent", True),
        ("ROOT.name", False),                 # `.name` 是字串不是路徑
        ("ROOT.read_text()", False),          # 不在保留清單裡的方法
        ("resolve_host_path(x)", True),       # 回傳註記是 Path 的函式
        ("some_other_call(x)", False),
        ("ROOT / 'a' / 'b'", True),           # 鏈式接合
        ("ROOT / x / y", True),
        ("other / x", False),
        ("(p or ROOT)", True),                # 兩邊都是路徑
        ("(p or 'literal')", False),          # ⚠️ any/all 的分水嶺
        ("('literal' or p)", False),          # 順序反過來也要擋
        ("(p if c else ROOT)", True),
        ("(p if c else 'literal')", False),   # 三元的分水嶺
        ("'literal'", False),
        ("x", False),
    ]
    wrong = []
    for text, want in cases:
        node = _ast.parse(text, mode="eval").body
        if _denotes_derived(node, derived, fns) != want:
            wrong.append((text, want))
    assert not wrong, (
        "`_denotes_derived` 這幾個案例答錯了（`(運算式, 應該是)`）：%s。"
        "放太寬會憑空製造站點（而且把算術也拖進來），放太窄會讓一整族接合隱形。"
        % wrong)


def test_the_parents_containment_discipline_requires_a_resolve():
    """`base not in folder.parents` 只有在 `folder` 經過 `.resolve()` 時才算數。

    ⚠️ **這一支補的是第二個存活的變異。** 把 `parents_checked & resolved_targets`
    改成 `parents_checked`（不要求 resolve）——**整組照樣全綠**，因為真實語料裡唯一
    用這道紀律的地方（`dorossi_backend._collect_codex_images`）本來就有 resolve。

    機制（實測，而且方向跟直覺相反）：`Path.parents` 走的是**字面的**元件，所以
    `base in (base / "../../etc/passwd").parents` 是 **True**——少了 `.resolve()`，
    這道檢查對**每一個穿越都回答「有包含」**。它不是「弱一點的守衛」，是一個看起來
    在把關、實際上永遠點頭的守衛。加上 `.resolve()` 之後同一個輸入回 False。

    ⚠️ 我第一版把理由寫成「`parents` 裡沒有 `root`，所以那道檢查問錯了東西」，
    **那是反的**，而且合成對照組當場把它推翻。留在這裡當紀錄：一個聽起來合理的
    機制敘述，跟一個量過的機制敘述，差別可能是整個結論的正負號。
    """
    import ast as _ast
    from pathlib import PurePosixPath as _PurePosix

    # 機制本身先釘住：這是整條規則的前提，而它是平台無關的。
    lexical = _PurePosix("root") / "../../etc/passwd"
    assert _PurePosix("root") in lexical.parents, (
        "`Path.parents` 開始正規化 `..` 了——那整條「必須先 resolve」的理由就要"
        "重寫（現在的理由是「不 resolve 的話它對每個穿越都說有包含」）。")

    def guarded_of(source: str) -> dict:
        tree = _ast.parse(source)
        return {fn: g for fn, _ln, _expr, g in _derived_join_sites(tree)}

    common = "ROOT = Path('/a')\n\n\n"
    with_resolve = common + (
        "def f(x):\n"
        "    folder = (ROOT / x).resolve()\n"
        "    if ROOT not in folder.parents:\n"
        "        return None\n"
        "    return folder\n")
    without_resolve = common + (
        "def f(x):\n"
        "    folder = ROOT / x\n"
        "    if ROOT not in folder.parents:\n"
        "        return None\n"
        "    return folder\n")
    assert guarded_of(with_resolve) == {"f": True}, guarded_of(with_resolve)
    assert guarded_of(without_resolve) == {"f": False}, guarded_of(without_resolve)
    # `absolute()` 只把 cwd 接到前面，`..` 原封不動——**不算**。
    with_absolute = without_resolve.replace("ROOT / x", "(ROOT / x).absolute()")
    assert guarded_of(with_absolute) == {"f": False}, guarded_of(with_absolute)
    # 反向：有 resolve 但**沒有**那道包含性檢查，仍然不算守住。
    resolve_only = common + (
        "def f(x):\n"
        "    folder = (ROOT / x).resolve()\n"
        "    return folder\n")
    assert guarded_of(resolve_only) == {"f": False}, guarded_of(resolve_only)


def test_the_attachment_name_guard_lets_nothing_escape(tmp_path):
    """前提測試：`("_gui_control.py", "write_host_file")` 那筆例外的理由是
    「`safe_basename` 比共用守衛弱，但實測 0 逃逸」。

    ⚠️ **這是行為測試，不是把註解再抄一遍。** 那筆例外的理由如果只寫在字串裡，
    把 `safe_basename` 改弱（例如拿掉 `base in (".", "..")` 那一行）**不會有任何
    東西變紅**——例外還在、接合守門照樣把它算成「已處置」。這一支直接餵敵意輸入，
    所以改弱會當場死。

    判準是「接合之後解析出來的路徑，**沒有跑到目的資料夾外面**」——那才是這道守衛
    真正要保證的事，比「回傳值長什麼樣」更接近它的目的。`GuiError` 算通過
    （擋下來了）。

    ⚠️ **判準不可以寫成「必須嚴格落在 dest 底下」**，那會對兩個正確的案例亮紅燈：
    Windows 會**去掉結尾的點**，所以 `...` 與 `....` 解析之後就等於 dest 自己
    （不是 dest 的子項）。那不是逃逸——實測 `os.replace` 到那個目標會丟
    `PermissionError` errno 13，**大聲**失敗。下面另有一段把這個行為釘住，因為它
    正是 `safe_basename` 與共用守衛唯一真正的分歧。
    """
    import posixpath as _posixpath

    import _gui_control as _gui

    # **目的資料夾必須真的存在。** `Path.resolve()` 只有對存在的路徑才會去問作業
    # 系統，而「Windows 去掉結尾的點」正是作業系統的行為——指一個不存在的目錄，
    # `...` 不會被收掉，下面那一段就變成在驗一個假的前提。
    dest = (tmp_path / "output")
    dest.mkdir()
    dest = dest.resolve()
    escaped = []
    for raw in _HOSTILE_ATTACHMENT_NAMES:
        try:
            base = _gui.safe_basename(raw)
        except _gui.GuiError:
            continue                       # 擋下來了，這是正確答案之一
        except Exception as error:         # pylint: disable=broad-except
            escaped.append((raw, f"非預期的例外 {type(error).__name__}"))
            continue
        try:
            landed = (dest / base).resolve()
        except OSError as error:
            escaped.append((raw, f"解析不了：{error!r}"))
            continue
        if landed != dest and dest not in landed.parents:
            escaped.append((raw, str(landed)))
    assert not escaped, (
        "這些附件檔名接到目的資料夾之後跑出去了（`(輸入, 落點)`）：%s。"
        "`safe_basename` 是這條規則的第三份私有拼法，實測 2026-09-11 的結果是"
        "**0 逃逸**——這一支就是那個結果的存放處。" % (escaped,))
    # 正面對照組：語料要真的含得住東西，否則一個恆回 `"x"` 的 `safe_basename`
    # 也會全綠（§8.8(A3)）。至少要有一筆被明確擋下、一筆被正規化成單一元件。
    blocked = sum(1 for raw in _HOSTILE_ATTACHMENT_NAMES
                  if _blocked_by_safe_basename(_gui, raw))
    assert blocked >= 4, f"只有 {blocked} 筆被擋下——語料或守衛變了"
    assert _gui.safe_basename("../evil") == "evil", "路徑穿越沒有被正規化掉"
    assert _gui.safe_basename("a\\..\\..\\evil") == "evil"
    # ⚠️ 上面那一行**在這台機器上證明不了 `.replace("\\", "/")` 有用**：Windows 的
    # `os.path.basename` 本來就把 `\` 當分隔符，所以拿掉那個 replace，變異測試
    # 照樣全綠（2026-09-11 實測，SURVIVED）。那不是測試太弱，是**這個平台上它真的
    # 是多餘的**——它守的是 POSIX。所以改成釘機制，而不是假裝這一跑驗過了：
    assert _posixpath.basename("a\\..\\..\\evil") == "a\\..\\..\\evil", (
        "POSIX 的 basename 開始認反斜線了——`safe_basename` 裡那個 replace 的"
        "存在理由要重寫。")
    assert _posixpath.basename("a\\..\\..\\evil".replace("\\", "/")) == "evil", (
        "先把反斜線換成斜線之後，POSIX 的 basename 就取得到最後一段——這正是"
        "`safe_basename` 那個 replace 在做的事。少了它，同一份程式碼在 POSIX 上"
        "會回一個**含分隔符**的「檔名」。")
    # 上面兩句釘的是「為什麼需要那個 replace」，但**在這台機器上它拿掉也沒有可觀察
    # 的差別**，所以行為測試殺不掉那個變異。這一段用結構把它釘住——同 `_pid_liveness`
    # 對「Windows 分支必須存在」的做法：平台條件式的性質，只能靠結構檢查。
    import ast as _ast

    gui_tree = _ast.parse(
        Path(_gui.__file__).read_text(encoding="utf-8"), _gui.__file__)
    fn = next(n for n in _ast.walk(gui_tree)
              if isinstance(n, _ast.FunctionDef) and n.name == "safe_basename")
    replaces = [_ast.unparse(c) for c in _ast.walk(fn)
                if isinstance(c, _ast.Call)
                and isinstance(c.func, _ast.Attribute)
                and c.func.attr == "replace"
                and len(c.args) == 2
                and all(isinstance(a, _ast.Constant) for a in c.args)
                and c.args[0].value == "\\" and c.args[1].value == "/"]
    assert replaces, (
        "`safe_basename` 不再把 `\\` 正規化成 `/` 了。在 Windows 上這**沒有可觀察"
        "的差別**（`os.path.basename` 本來就認反斜線），所以沒有任何行為測試會變紅"
        "——但在 POSIX 上它會讓 `a\\..\\..\\evil` 原樣通過，變成一個含分隔符的"
        "「檔名」。這是結構檢查，不是形式主義。")
    # 唯一真正的分歧，明寫出來免得下次被當成新發現：`safe_basename` 放行全點名字，
    # 共用守衛（`_is_safe_folder_component`）會擋。實測那條路的下場是**大聲失敗**，
    # 不是寫錯地方——Windows 去掉結尾的點之後目標就是 dest 自己，`os.replace` 丟
    # `PermissionError` errno 13。（同樣地 `x.` 會被檔案系統寫成 `x`，名字跟使用者
    # 送的不一樣，但仍在 dest 裡面。）
    for dots in ("...", "...."):
        assert _gui.safe_basename(dots) == dots, (
            f"`safe_basename` 現在會處理 {dots!r} 了——上面那筆例外的理由"
            "（「唯一的分歧是全點名字，而且它大聲失敗」）要跟著重寫。")
        assert (dest / dots).resolve() == dest, (
            f"{dots!r} 不再收斂到目的資料夾自己了——這個平台可能不去掉結尾的點，"
            "那上面「不是逃逸」的論證就要重驗。")


def _blocked_by_safe_basename(gui_module, raw: str) -> bool:
    """`safe_basename` 有沒有明確擋下這個名字（丟 `GuiError`）。"""
    try:
        gui_module.safe_basename(raw)
    except gui_module.GuiError:
        return True
    except Exception:  # pylint: disable=broad-except
        return False
    return False


def test_the_session_slot_id_is_built_from_an_int():
    """前提測試：`dorossi_session_workdir` 那筆例外的理由是「`sid` 一定長成
    `s<數字>`」，而那句話今天只寫在 docstring 裡。

    **一筆沒有人驗前提的例外，活得比它的理由久**（CLAUDE.md 對 `_OWNER_ONLY_SLASH`
    的說法）。這一支用 AST 問三件事：槽位 id 的產生處還在、它還是用 int 去組的、
    而且產生處只有一個。任何一件變了，那筆例外的理由就該重寫。
    """
    import ast as _ast
    import re as _re
    from pathlib import Path as _Path

    import dorossi_backend as _db

    tree = _ast.parse(
        _Path(_db.__file__).read_text(encoding="utf-8"), _db.__file__)
    # `sid = f"s{...}"` 的指派。用 AST 而不是 `"f\"s{" in source`——後者會命中
    # 解釋這條規則的註解本身，這個 repo 已經踩過。
    builds = []
    for node in _ast.walk(tree):
        if not (isinstance(node, _ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], _ast.Name)
                and node.targets[0].id == "sid"
                and isinstance(node.value, _ast.JoinedStr)):
            continue
        parts = node.value.values
        literal = "".join(p.value for p in parts
                          if isinstance(p, _ast.Constant)
                          and isinstance(p.value, str))
        builds.append((node.lineno, literal, _ast.unparse(node.value)))
    assert builds, (
        "`dorossi_backend` 裡找不到任何 `sid = f\"s…\"` 的指派了——槽位 id 換了"
        "產生方式，`dorossi_session_workdir` 那筆接合例外的理由要跟著重寫。")
    bad = [b for b in builds if b[1] != "s"]
    assert not bad, (
        "槽位 id 的字面值部分不再只是 `s`：%s。例外的理由（「`s<數字>` 接不出"
        "路徑分隔符」）就不成立了。" % (bad,))
    # 產生處只有一個：多一個就代表有第二條路徑可以決定 `sid` 長什麼樣。
    assert len({b[0] // 1000 for b in builds}) == 1 and len(builds) <= 2, (
        f"`sid` 的產生處變多了（{[b[0] for b in builds]}）——例外的理由假設只有 "
        "`_dorossi_new_session` 一處在決定它的形狀。")
    # 值域對照：實際跑一次產生器，確認它真的產得出 `s<數字>` 而不是別的。
    rec: dict = {"next_seq": 3, "sessions": {}}
    sid = _db._dorossi_new_session(rec)
    assert _re.fullmatch(r"s\d+", sid), sid


def test_the_gui_name_whitelists_stay_inside_the_shared_guard():
    """前提測試：**私有 regex 放行的名字，共用守衛也必須放行。**

    `_JOIN_GUARD_PRIVATE_WHITELIST` 那兩筆的理由是「私有白名單比共用守衛嚴」。
    例外條目是記帳，這一支才是保護：把 `MACRO_NAME_RE` 放寬（例如為了版本化巨集
    允許 `.`），在這支之前**不會有任何東西變紅**。

    **為什麼比對的是 `_is_unsafe_folder_name`（住在 bot、`_gui_control` 匯不到），
    而不是隨手可用的那兩支。** 全枚舉 regex 自己的 64 字元字元集、長度 1..3
    共 266,304 個 key，對 `f"{key}{suffix}"` 實測：

    | 守衛 | 說「不行」的個數 |
    |---|---|
    | `_is_unsafe_folder_name`（bot） | **0** |
    | `_is_single_path_component`（共用） | **0** |
    | `_is_safe_folder_component`（共用） | **32** |

    * `_is_single_path_component` 匯得到，但接受範圍一模一樣——真的改去呼叫它，
      行為一個位元都不會變，它會存在純粹是為了讓靜態抽取器看到這個站點。那正是
      「被空儀式滿足的守門」。
    * `_is_safe_folder_component` 匯得到，但**更嚴**：那 32 筆全是保留裝置名的大小
      寫變體（`NUL`/`CON`/`AUX`/`PRN` 各 8 種）。換過去是**行為改變**，`/win layout
      save NUL` 當天開始失敗。而且那份嚴格在這裡買不到東西：實測在目錄裡
      `NUL.json` / `CON.json` / `AUX.json` 都是寫得進、列得到、讀得回的普通檔案，
      只有**裸** `NUL` 才是裝置，而 regex 加上必然的副檔名讓裸保留名根本構造不出來。

    **比對的對象是 `f"{key}{suffix}"` 不是 `key`**——被接到目錄上的是前者。

    **涵蓋範圍只有「安靜」那一類。** 實測：regex 若放寬到含 `/`、`\\`、`:`，共用
    守衛會擋 → 這支變紅。含 `*`、`?`、`"`、`<`、`>`、`|` 則兩邊都放行、這支抓不到，
    但那一類在 Windows 上寫檔時是 `OSError` errno 22，**大聲**失敗，不需要測試守。
    含 `.`、` ` 兩邊也都放行，而那兩個本來就是合法檔名字元。

    三種 key 變體是因為守衛的答案**跟位置有關**，一個位置只問得到守衛的一部分
    規則：`:` 要在第 2 個字元以後才會被當磁碟機字首（單字元的 `:` 放行），而全部
    是點的名字才會被當上層目錄（`a.b` 放行，且那是正確的——中間有點是合法檔名）。
    ⚠️ 實測 2026-09-11：**今天「三連」剛好單獨就抓得到那四個字元**，所以不要寫
    「只用一種變體等於半個守門」——我第一版就是那樣寫的，量完才發現不成立。
    """
    import re as _re

    source = (Path(b.__file__).resolve().parent
              / "_gui_control.py").read_text(encoding="utf-8")
    whitelists = _gui_name_whitelists(source)

    # **只看 `_gui_control.py` 那幾筆。** 這份例外清單後來也收了別的模組的私有
    # 白名單（`_platform_runtime` 的平台名），而它們有自己的前提測試；不篩的話
    # 這支會把「別的模組多了一筆」報成「抽取器壞了」——一個看起來很嚇人、其實
    # 完全無關的假訊號。
    expected = {fn for f, fn in _JOIN_GUARD_PRIVATE_WHITELIST
                if f == "_gui_control.py"}
    assert set(whitelists) == expected, (
        f"抽到的白名單建構函式是 {sorted(whitelists)}，例外清單說的是 "
        f"{sorted(expected)}。抽取器抽不到就等於這支測試空轉——**而空轉跟全部"
        "合規在輸出上一模一樣**。函式改名、regex 改成別的寫法、或副檔名不再是"
        "單一 f-string，都會走到這裡。")

    variants = {
        "單字元": lambda c: c,
        "三連": lambda c: c * 3,
        "夾中間": lambda c: "a" + c + "b",
    }
    for fn_name, (pattern, suffix) in sorted(whitelists.items()):
        alphabet = [c for c in map(chr, range(32, 127)) if _re.match(pattern, c)]
        # 正面對照：字元集塌掉（例如 regex 被改成 `^$`）會讓下面的迴圈空轉。
        assert len(alphabet) >= 30, (
            f"`{fn_name}` 的白名單 {pattern!r} 只接受 {len(alphabet)} 種字元"
            f"（{alphabet}）——低到不像是這個用途的白名單，抽錯了還是被改壞了？"
            "下限刻意遠低於實測的 64。")
        offenders = [
            (label, make(ch)) for ch in alphabet
            for label, make in variants.items()
            if b._is_unsafe_folder_name(f"{make(ch)}{suffix}")
        ]
        assert not offenders, (
            f"`_gui_control.{fn_name}` 的私有白名單 {pattern!r} 放行了共用守衛"
            f"`_is_unsafe_folder_name` 會擋的名字：{offenders[:10]}。\n"
            f"`_JOIN_GUARD_PRIVATE_WHITELIST` 那一筆的理由（「私有 regex 比共用"
            "守衛嚴」）**當場不成立了**，所以那個接合現在是真的沒人在看。要嘛把"
            "字元集改回去，要嘛改成真的呼叫一支共用守衛並拿掉那筆例外。")


def test_the_gui_whitelist_premise_test_actually_bites():
    """正面對照：上面那支斷言的是空集合，它自己分不出「規則成立」與「抽取器抽不到」。

    所以把原始碼變形之後再問一次——抽取器要跟得上（副檔名換掉），而放寬 regex 要
    真的讓前提破掉（允許 `.` 之後，`...json` 這種全點名字會被共用守衛擋下）。
    """
    import re as _re

    source = (Path(b.__file__).resolve().parent
              / "_gui_control.py").read_text(encoding="utf-8")

    renamed = source.replace('f"{key}.json"', 'f"{key}.yaml"')
    got = _gui_name_whitelists(renamed)
    assert got and all(suffix == ".yaml" for _p, suffix in got.values()), (
        f"副檔名改掉之後抽取器沒跟上：{got}。它要是寫死 `.json`，改副檔名的那天"
        "這支測試會繼續驗一個不存在的組合，而且是綠的。")

    widened = source.replace(
        'LAYOUT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")',
        'LAYOUT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")')
    assert widened != source, "變形沒套用——`LAYOUT_NAME_RE` 的寫法改了？"
    pattern, suffix = _gui_name_whitelists(widened)["layout_path"]
    assert "." in pattern, f"變形後的 pattern 不含點：{pattern!r}"
    alphabet = [c for c in map(chr, range(32, 127)) if _re.match(pattern, c)]
    escaped = [c * 3 for c in alphabet
               if b._is_unsafe_folder_name(f"{c * 3}{suffix}")]
    assert escaped, (
        "把 `.` 加進白名單之後，共用守衛竟然還是全部放行——那表示這支前提測試"
        "抓不到「regex 被放寬」這件事，而那正是它唯一要抓的東西。")


def test_every_join_onto_a_base_root_is_guarded_per_join():
    """靜態：**整個專案**每一次接到 base 目錄（或其推導物）上的接合，右邊都要過
    守衛，或者形狀本身就保證了性質。

    `cmd_fav_show` 是這條規則的來歷：`character` 有守衛、往下兩行的 `filename`
    沒有，而對 `filename` 成立的論證與對 `character` 的**一字不差**——今天安全
    是因為唯一的寫入者 `_react_fav` 寫的是 `p.name`，那是**寫入端**的性質，不是
    讀取端宣告出來的。而 `favorites.json` 是磁碟上一份普通的 JSON。

    2026-09-10 把檔案軸拉開：規則的文字沒有提到任何模組，掃描範圍卻只有
    `discord_bot.py` 一個檔案。同一條規則在 webrunner 那一側被違反過
    （`character_folder_name` 讓一行 `..` 變成資料夾名），而那個缺陷離這支守門
    只有一個 `ast.parse` 的距離。
    """
    sources = _join_scan_sources()
    assert len(sources) >= _JOIN_SCAN_FILE_FLOOR, (
        f"只掃到 {len(sources)} 個檔案（下限 {_JOIN_SCAN_FILE_FLOOR}）——範圍"
        "壞了，下面等於沒在檢查。")
    rows, roots_by_file = _scan_derived_joins(sources)

    # 正面對照組（§8.8(A3)）：抽取器壞掉回空集合時，「零筆違規」跟「全部都對」在
    # 輸出上一模一樣。所以先確認它**真的看得見東西**。
    all_roots = set()
    for names in roots_by_file.values():
        all_roots |= names
    assert len(all_roots) >= _BASE_ROOT_FLOOR, (
        f"整個專案只推出 {sorted(all_roots)} 這幾個 base 目錄（下限 "
        f"{_BASE_ROOT_FLOOR}）——推導壞了，下面等於沒在檢查。")
    assert "TEMPLATES_DIR" in roots_by_file.get("discord_bot.py", set()), (
        "`TEMPLATES_DIR` 沒被推出來。推導若退回 2026-09-10 之前那份手寫的四元組"
        "就會少掉它，而那正是 `_template_path` 一直隱形的原因。")
    # 第二個具名金絲雀，**刻意在 `discord_bot.py` 以外、而且在套件以外**。
    # 上面那個 `TEMPLATES_DIR` 住在 bot 裡，所以它答不出「範圍有沒有縮回一個檔」；
    # 而只掃套件（漏掉 repo root 的啟動腳本）也一樣答不出來——那正是
    # `_CROSS_PROCESS_CONSTANTS` 漏掉 `webrunner.pid` 的同一個形狀：它唯一的寫入者
    # `start_webrunner.py` 在 repo 根目錄，而當時的掃描範圍是一份寫死的六元組。
    assert "AXIOMATIC_DIR" in roots_by_file.get("start_webrunner.py", set()), (
        "`start_webrunner.py` 的 `AXIOMATIC_DIR` 沒被推出來——掃描範圍可能縮回"
        "只剩套件內部，repo 根目錄的啟動腳本整批消失了。純數字看不出這件事："
        "少掉那幾個檔之後相異根只從 21 掉到 19，照樣過得了下限。")
    assert len(rows) >= _JOIN_SITE_FLOOR, (
        f"整個專案只抽到 {len(rows)} 個接合站點（下限 {_JOIN_SITE_FLOOR}）——"
        "抽取器壞了。這個下限數的是**所有**接合（含已守住的），因為違規會被修到"
        "零，用違規數當下限就只能是 0，而 0 跟空集合長得一模一樣。")

    # 具名金絲雀：範圍有沒有真的跨出 `discord_bot.py`。純數字答不出這個問題——
    # 光是 bot 一個檔就足以撐過上面那個下限，於是「範圍縮回一個檔」會安靜地通過。
    #
    # ⚠️ **兩個問題要分開問，因為它們的穩定度差很多。**（2026-09-10 覆核修正）
    # 原本兩件事都綁在「novelai 必須**有接合站點**」上，而它那 5 個站點全部是同一
    # 份 Chrome 設定檔快照的程式碼（`_snapshot_chrome_profile` ×3、
    # `_sync_chrome_profile_back`、`_sync_profile_dir_back`）。把那份合併進
    # `_webrunner_shared.py`——**本 repo 讓兩個變體同步的既有做法**——之後 novelai
    # 會變成 0 站點，金絲雀就為一個完全正確的重構亮紅燈，而它要證明的事（範圍跨出
    # bot）那時仍然成立，站點只是搬家了。又一個「對合法改動亂叫」的對照組。
    #
    # 所以：「這個檔有沒有被解析到」問 `roots_by_file`（**每一個被解析的檔都有
    # key，即使 0 站點**——實測 30 個 key 對 6 個有站點的檔）；「bot 以外真的抽到
    # 接合了嗎」問 rows，但**不指定是哪一個檔**。
    for outside in ("_webrunner_shared.py", "webrunner_novelai.py",
                    "webrunner_je_only.py"):
        assert outside in roots_by_file, (
            f"`{outside}` 根本沒被解析——2026-09-10 這次放寬要守住的就是「規則的"
            "文字沒有提到模組，掃描卻只看 bot 一個檔」，範圍縮回去的話這支守門"
            "就退回原狀了。")
    assert {row[0] for row in rows} - {"discord_bot.py"}, (
        "所有接合站點都落在 `discord_bot.py`——抽取器可能只剩 bot 那一個檔在出力。")
    # 三種紀律各自都要真的有在放行東西，否則「紀律被拿掉」與「今天剛好沒有那種
    # 形狀」在輸出上一樣（那正是 §8.8(A3)）。
    assert any(guarded for _f, _fn, _ln, _e, guarded in rows), (
        "一個接合都沒被判定為已守住——守衛偵測那一半壞了。")

    # 紀律 (c) 自己也要有金絲雀：形狀規則被拿掉、與「今天剛好沒有那種形狀」在
    # 輸出上一模一樣。實測 2026-09-11 有 9 個站點是**只**靠形狀規則放行的。
    # 判準：右運算元**不是**裸 `Name`（那種的 expr 一律長成 `LEFT / RIGHT`，兩邊
    # 都是識別字），而且被判成已守住——那就只可能是形狀規則放行的。
    import re as _re

    # ⚠️ 判準看的是**右**運算元是不是裸識別字，不是整個運算式。2026-09-11 放寬
    # 「左邊可以是 Call／鏈式接合」之後，`ROOT / 'sessions' / name` 的整串就不再
    # 長成 `\w+ / \w+`，用整串比對會把它誤算成「形狀規則放行的」——那會讓這個
    # 金絲雀在形狀規則真的失效時仍然抓得到東西，也就是變成裝飾。
    shape_only = [row[:4] for row in rows
                  if row[4] and not _re.fullmatch(
                      r"\w+", row[3].rsplit(" / ", 1)[-1])]
    assert shape_only, (
        "沒有任何一個接合是靠**形狀規則**（紀律 (c) 的運算式版）放行的——"
        "`_shape_safe_operand` 可能已經失效，而所有站點都改由守衛放行，於是"
        "把那條規則整個刪掉也不會有東西變紅。")

    # 「參數也算根」那條放寬的具名金絲雀。**下限數字抓不到 revert**：把
    # `_path_typed_params` 那一行從 `_derived_join_sites` 拿掉，站點會從 53 掉回
    # 37，而 37 遠高於下限，一切照樣綠——同一個形狀在 `_JOIN_SITE_FLOOR` 自己的
    # 註解裡已經寫過一次（下限 25 剛好等於放寬前的實測，所以它擋不住回頭路）。
    # 實測 2026-09-11：16 個站點是**只有**靠這條才看得見的。
    import ast as _ast

    param_rooted = set()
    for path in sources:
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        param_rooted |= {(path.name, fn, ln)
                         for fn, ln in _param_rooted_join_sites(tree)}
    seen_keys = {(row[0], row[1], row[2]) for row in rows}
    assert param_rooted & seen_keys, (
        "沒有任何一個站點是靠「帶 `Path` 註記的參數也算根」才看見的——"
        "`_derived_join_sites` 裡那句 `derived |= _path_typed_params(fn)` 可能被"
        "拿掉了。那條放寬照亮的是「呼叫端傳路徑進來、被呼叫端往上接東西」一整族"
        "（`_leveldb_manifest_ok` / `_session_entry_present` / "
        "`_sync_profile_dir_back` / `generate_loop` …），沒有它，那一族**一列都不會"
        "產生**——不是變紅，是整個消失，連例外清單裡都不會留下痕跡。")

    # 第三條放寬的具名金絲雀：左運算元不是裸 `Name`（`Call`／鏈式接合）。同樣的
    # 理由——下限抓不到 revert（55 掉回 53 照樣綠）。判準是「`/` 的左邊在原始碼裡
    # 不是單一識別字」，實測 2026-09-11 有 2 個這種站點。
    chain_rooted = [row[:4] for row in rows
                    if not _re.fullmatch(r"\w+", row[3].rsplit(" / ", 1)[0])]
    assert chain_rooted, (
        "沒有任何一個站點的左運算元是 `Call` 或鏈式接合——`_denotes_derived` 可能被"
        "退回成「左邊必須是裸 `Name` 且在 `derived` 裡」。那樣一來 "
        "`(root or CONST).resolve() / x` 與 `ROOT / '子目錄' / x` 這兩整類會**一列都"
        "不產生**，不是變紅，是消失。")

    exempt = set(_JOIN_GUARD_EXEMPT) | set(_JOIN_GUARD_PRIVATE_WHITELIST)
    offenders = [row[:4] for row in rows
                 if not row[4] and (row[0], row[1]) not in exempt]
    assert not offenders, (
        "這些接合把一個變數接到 base 目錄（或其推導物）上，而那個變數既沒有被 "
        "`%s` 檢查過，形狀也不保證安全：%s。\n"
        "注意判準是 **per-join** 不是 per-function——同一個函式裡別的接合有守衛"
        "不算數，那正是 `cmd_fav_show` 溜過去的原因。"
        % (" / ".join(sorted(_JOIN_GUARDS)), offenders))


def test_the_join_scan_covers_the_repo_root_not_just_the_package():
    """範圍的釘子：兩個目錄都要用 glob 走，而且豁免在**兩邊**都生效。

    §8.8(A3)：真實資料乾淨時，寫死的範圍和算出來的範圍在輸出上一模一樣。所以拿
    合成目錄問它。repo root 住的正是兩支**監督者**與 `run_batch.py`——CLAUDE.md
    記過同一個家族的實例：atomic-writes 掃描的六元組漏掉 `webrunner.pid`，因為
    它唯一的寫入者住在 repo root。
    """
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pkg = root / "pkg"
        pkg.mkdir()
        (pkg / "some_module.py").write_text("x = 1\n", encoding="utf-8")
        (pkg / "test_not_a_product_module.py").write_text("x = 1\n",
                                                          encoding="utf-8")
        (pkg / "gen_command_docs.py").write_text("x = 1\n", encoding="utf-8")
        (root / "brand_new_supervisor.py").write_text("x = 1\n",
                                                      encoding="utf-8")
        (root / "install_autostart.py").write_text("x = 1\n", encoding="utf-8")
        names = {p.name for p in _join_scan_sources(pkg_root=pkg, repo_root=root)}

    assert "some_module.py" in names
    assert "brand_new_supervisor.py" in names, (
        "repo root 新出現的腳本沒有被算進來——那一半是寫死的清單，不是 glob。")
    assert "test_not_a_product_module.py" not in names, "測試檔不該進掃描"
    assert "gen_command_docs.py" not in names, "豁免在套件那一半失效了"
    assert "install_autostart.py" not in names, "豁免在 repo root 那一半失效了"


def test_the_join_scan_skip_list_only_holds_human_run_tools():
    """整個檔案不掃是最粗的豁免，所以它必須小、說得出理由、而且前提有人驗。

    這裡的前提是「沒有任何長命行程會執行到這幾個檔案」——前提一旦破掉，後果不是
    抽象的：那個檔案裡未經檢查的接合會**自動**豁免於這道守門。
    """
    import ast as _ast

    package = Path(b.__file__).resolve().parent
    root = package.parent
    for name in _JOIN_SCAN_SKIP_FILES:
        assert (package / name).exists() or (root / name).exists(), (
            f"{name} 不在了，請把它從 `_JOIN_SCAN_SKIP_FILES` 刪掉——"
            "一個對不到檔案的豁免只是留著一個永遠不命中的字串。")

    wanted = {name[:-3] for name in _JOIN_SCAN_SKIP_FILES}
    importers = {}
    for path in _join_scan_sources():
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                hits = {a.name.split(".")[-1] for a in node.names} & wanted
            elif isinstance(node, _ast.ImportFrom):
                hits = ({node.module.split(".")[-1]} & wanted
                        if node.module else set())
                hits |= {a.name for a in node.names} & wanted
            else:
                continue
            for hit in hits:
                importers.setdefault(hit, set()).add(path.name)
    assert not importers, (
        "這些被整檔豁免的手動工具已經被產品端模組 import 了："
        + str({k: sorted(v) for k, v in importers.items()})
        + "。豁免的前提（只有人在終端機手動跑）不成立了——把它從 "
        "`_JOIN_SCAN_SKIP_FILES` 拿掉，改成逐點登記。")


def test_the_join_guard_can_still_see_a_violation():
    """對照組：拿合成原始碼問檢查器「你還看得見東西嗎」。

    現況乾淨，所以上面那支把斷言整個刪掉**也不會紅**。牙齒在這裡。前三個案例對應
    前一版守門的兩個盲點與一個必須保留的正面行為。
    """
    import ast as _ast

    # (1) 第二跳：舊版只比對 `n.left.id == "OUTPUT_ROOT"`，看不到這一行。
    two_hop = _ast.parse(
        "def f(a, b):\n"
        "    folder = OUTPUT_ROOT / a\n"
        "    p = folder / b\n"
        "    return p\n")
    hits = _unguarded_derived_joins(two_hop, {"OUTPUT_ROOT"})
    assert [row[2] for row in hits] == ["OUTPUT_ROOT / a", "folder / b"], hits

    # (2) per-join：一個接合有守衛，不代表同一個函式裡另一個也有。這是
    #     `cmd_fav_show` 的真實形狀——舊版對這段是**綠的**。
    one_guarded = _ast.parse(
        "def f(a, b):\n"
        "    if _is_unsafe_folder_name(a):\n"
        "        return None\n"
        "    folder = OUTPUT_ROOT / a\n"
        "    p = folder / b\n"
        "    return p\n")
    hits = _unguarded_derived_joins(one_guarded, {"OUTPUT_ROOT"})
    assert [row[2] for row in hits] == ["folder / b"], hits

    # (3) 反面：兩個都守衛過就不該被標，否則這支守門會逼人加沒有意義的例外。
    both_guarded = _ast.parse(
        "def f(a, b):\n"
        "    if _is_unsafe_folder_name(a) or _is_unsafe_folder_name(b):\n"
        "        return None\n"
        "    folder = OUTPUT_ROOT / a\n"
        "    return folder / b\n")
    assert _unguarded_derived_joins(both_guarded, {"OUTPUT_ROOT"}) == []

    # (4) 反面：字面值不是使用者輸入，不該被標。
    literal = _ast.parse(
        "def f():\n"
        "    return PROJECT_ROOT / '.chrome_profile'\n")
    assert _unguarded_derived_joins(literal, {"OUTPUT_ROOT"}) == []

    # (5) 反面：紀律 (b)。右運算元是多層相對路徑，套不上「單一元件」那支守衛；
    #     改成檢查接合**結果**的包含性，這一筆就不該再被標。這是
    #     `_handle_single_image_done` 的真實形狀。
    contained = _ast.parse(
        "def f(rel):\n"
        "    img = PROJECT_ROOT / rel\n"
        "    if not _within_allowed_roots(img, (OUTPUT_ROOT,)):\n"
        "        return None\n"
        "    return img\n")
    assert _unguarded_derived_joins(contained, {"OUTPUT_ROOT"}) == []

    # (6) **per-join 粒度，紀律 (b) 版**：對 `folder` 做包含性檢查，不得讓
    #     `folder / filename` 跟著免疫。這是 (2) 的同構案例——那次的教訓是
    #     per-function 判準會讓同一個函式裡別的接合自動免疫，換一種守衛不代表
    #     那個教訓失效。少了這一格，「只要函式裡有出現包含性檢查就全放行」這種
    #     實作會是綠的。
    contained_first_hop_only = _ast.parse(
        "def f(a, b):\n"
        "    folder = OUTPUT_ROOT / a\n"
        "    if not _within_allowed_roots(folder, (OUTPUT_ROOT,)):\n"
        "        return None\n"
        "    p = folder / b\n"
        "    return p\n")
    hits = _unguarded_derived_joins(contained_first_hop_only, {"OUTPUT_ROOT"})
    assert [row[2] for row in hits] == ["folder / b"], hits

    # (7) 反面：包含性檢查的**第二個**引數（允許的根清單）不是「被檢查的路徑」，
    #     不得因為出現在引數位置就變成一張免死金牌。
    roots_arg_is_not_a_pass = _ast.parse(
        "def f(a, roots):\n"
        "    if not _within_allowed_roots(a, roots):\n"
        "        return None\n"
        "    return OUTPUT_ROOT / roots\n")
    hits = _unguarded_derived_joins(roots_arg_is_not_a_pass, {"OUTPUT_ROOT"})
    assert [row[2] for row in hits] == ["OUTPUT_ROOT / roots"], hits

    # (8) 反面：**參數**不是安全形狀。跨函式推導在只看得到一個模組時是 fail-open
    #     的，所以刻意不做——這一格釘住「不做」，否則哪天有人順手加上去，
    #     `_sync_profile_dir_back` 那兩筆例外會安靜地變成永遠不命中的字串。
    bare_param = _ast.parse(
        "def f(relpath):\n"
        "    return CHROME_PROFILE_DIR / relpath\n")
    hits = _unguarded_derived_joins(bare_param, {"CHROME_PROFILE_DIR"})
    assert [row[2] for row in hits] == ["CHROME_PROFILE_DIR / relpath"], hits


def test_the_structural_disciplines_recognise_the_real_shapes():
    """三種結構性紀律的正反面。

    這三種是為了**不要**替 webrunner 的 8 個站點發 8 張免死金牌而加的。所以每一種
    都要有反面案例：只有正面的話，一個「無條件放行」的實作也會全綠。
    """
    import ast as _ast

    # (1) relative_to：`_snapshot_chrome_profile` 的真實形狀。
    rel = _ast.parse(
        "def f():\n"
        "    for root, dirs, files in os.walk(SRC_DIR):\n"
        "        rel_root = Path(root).relative_to(SRC_DIR)\n"
        "        dst_root = DST_DIR / rel_root\n")
    assert _unguarded_derived_joins(rel, {"SRC_DIR", "DST_DIR"}) == []

    # (1) 反面：`walk_up=True`（3.12 起）**會**產出 `..`，保證就沒了。
    walk_up = _ast.parse(
        "def f():\n"
        "    rel_root = Path(root).relative_to(SRC_DIR, walk_up=True)\n"
        "    return DST_DIR / rel_root\n")
    hits = _unguarded_derived_joins(walk_up, {"SRC_DIR", "DST_DIR"})
    assert [row[2] for row in hits] == ["DST_DIR / rel_root"], hits

    # (2) os.walk 的 files / dirs 是裸名字。
    walked = _ast.parse(
        "def f():\n"
        "    for root, dirs, files in os.walk(SRC_DIR):\n"
        "        for fname in files:\n"
        "            dst = DST_DIR / fname\n"
        "        for dname in dirs:\n"
        "            sub = DST_DIR / dname\n")
    assert _unguarded_derived_joins(walked, {"SRC_DIR", "DST_DIR"}) == []

    # (2) 反面：`os.walk` 的**第一個**元素是完整路徑，不是裸名字。逐項迴圈也不是
    #     那三個名字就不該放行。
    not_a_walk_name = _ast.parse(
        "def f(names):\n"
        "    for root, dirs, files in os.walk(SRC_DIR):\n"
        "        pass\n"
        "    for other in names:\n"
        "        dst = DST_DIR / other\n")
    hits = _unguarded_derived_joins(not_a_walk_name, {"SRC_DIR", "DST_DIR"})
    assert [row[2] for row in hits] == ["DST_DIR / other"], hits

    # (3) 模組層字面值序列的迴圈變數。注意元素**帶分隔符**——這一條保證的不是
    #     「單一成分」，是「整個值都來自本 repo 的原始碼」。
    consts = _ast.parse(
        "CRITICAL = (\n"
        "    'Default/Network/Cookies',\n"
        "    'Local State',\n"
        ")\n"
        "def f():\n"
        "    for relpath in CRITICAL:\n"
        "        dst = PROFILE_DIR / relpath\n")
    assert _unguarded_derived_joins(consts, {"PROFILE_DIR"}) == []

    # (3) 反面：序列裡有一個非字面值，整個序列就不再是「純原始碼」。
    tainted = _ast.parse(
        "CRITICAL = ('Local State', user_supplied)\n"
        "def f():\n"
        "    for relpath in CRITICAL:\n"
        "        dst = PROFILE_DIR / relpath\n")
    hits = _unguarded_derived_joins(tainted, {"PROFILE_DIR"})
    assert [row[2] for row in hits] == ["PROFILE_DIR / relpath"], hits

    # (3) 反面：函式**裡面**的序列不算模組層常數（它可以由參數組出來）。
    local_seq = _ast.parse(
        "def f(x):\n"
        "    names = ('a', 'b')\n"
        "    for relpath in names:\n"
        "        dst = PROFILE_DIR / relpath\n")
    hits = _unguarded_derived_joins(local_seq, {"PROFILE_DIR"})
    assert [row[2] for row in hits] == ["PROFILE_DIR / relpath"], hits


def test_the_dir_constant_extractor_sees_both_annotated_and_aliased_roots():
    """合成對照：`_module_dir_constants` 的四條規則各問一次，正反都問。

    2026-09-10 之前它只讀 `ast.Assign`，於是 `_gui_control.py` 的
    `_SHELL_CWD: Path = PROJECT_ROOT`（`ast.AnnAssign`）推不出來，
    `set_shell_cwd` 裡那個 `_SHELL_CWD / expanded` 接合**整個隱形**——
    「這個檔很乾淨」與「這個檔沒被看見」在輸出上一模一樣。

    ⚠️ **這支必須用合成語料，不能只靠真檔案。** `_SHELL_CWD` 同時符合「註記是
    `Path`」與「別名到已知的根」兩條，所以拿它當唯一證據時，把任一條拿掉都還是
    綠的——`feedback-two-guards-can-mask-each-other` 的形狀。下面每一格刻意只踩
    一條。
    """
    import ast as _ast

    tree = _ast.parse(
        "from pathlib import Path\n"
        "PROJECT_ROOT = Path(__file__).resolve().parent\n"
        "ANNOTATED: Path = PROJECT_ROOT\n"          # 只有註記那條認得
        "ALIAS_ONLY = PROJECT_ROOT\n"               # 只有別名那條認得
        "SECOND_HOP = ALIAS_ONLY\n"                 # 別名要跑到不動點
        "CONTAINER: list[tuple[Path, str]] = []\n"  # 反面：裝路徑的容器不是根
        "PLAIN: str = 'abc'\n"                      # 反面：不是 Path
        "lower_case = PROJECT_ROOT\n")              # 反面：不是大寫名字
    roots = _module_dir_constants(tree) - _EXTRA_BASE_ROOTS

    for name in ("PROJECT_ROOT", "ANNOTATED", "ALIAS_ONLY", "SECOND_HOP"):
        assert name in roots, f"`{name}` 沒被認出來：{sorted(roots)}"
    for name in ("CONTAINER", "PLAIN", "lower_case"):
        assert name not in roots, (
            f"`{name}` 被誤認成 base 目錄了。多收的名字會變成永遠不命中的幽靈"
            f"條目，而 `test_the_base_roots_contain_no_name_that_does_not_exist` "
            f"會把它報成過期——那是一次假的紅燈：{sorted(roots)}")


def test_every_recognised_join_guard_still_names_a_real_function():
    """`_JOIN_GUARDS` 的每個名字都要真的解析得到一個專案裡的函式。

    ⚠️ **先講清楚這支「不是」在防什麼，因為直覺的說法是錯的。** 直覺會說「名字
    過期 → 那一支守住的站點安靜地失去檢查」。**實測不成立**：守衛改名之後，呼叫
    端寫的是新名字，`_JOIN_GUARDS` 認不得，那些站點會判成沒守住而**變紅**。也就
    是說改名本身是 fail-**closed** 的，很吵，當場就會被發現。

    這支防的是**那個紅燈最省事的錯誤修法**：把變紅的站點加進
    `_JOIN_GUARD_EXEMPT` 讓它閉嘴，而不是去更新 `_JOIN_GUARDS`。那樣一來紅燈消
    失、舊名字永遠留在集合裡，而那些站點**從此真的沒有人在看**——例外清單本來
    就是「我們決定不查這裡」。同一個形狀在 `_JOIN_GUARD_EXEMPT` 自己的註解裡寫
    過：「留著就是替未來的回歸先開好一張免死金牌」。所以這支的價值在**訊息**：
    它指著真正的原因，讓下一個人改對地方，而不是把症狀蓋掉。

    2026-09-10 之前 `_JOIN_GUARD` 是單一字串，沒有這個問題可談（改名會讓每一個
    站點一起紅）。放寬成三個名字才讓「只改一半」變成一種可能的狀態。

    只驗「名字存在」這一半。反方向（專案裡每一支路徑守衛都要列進來）**刻意不
    驗**：那需要先定義「什麼算守衛」，而那個判準本身就會是第四份私有拼法。
    """
    import ast as _ast

    defined: dict[str, str] = {}
    for path in _join_scan_sources():
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                defined.setdefault(node.name, path.name)

    # 正面對照組：抽取器回空 dict 時下面那句會全紅，看起來像「三個守衛全被改名」
    # ——一個把人指往完全錯誤方向的訊息。所以先問它到底看見了多少函式。
    #
    # **實測 2026-09-10：30 個檔案、1434 個 module-level 與巢狀的 `def`／
    # `async def`**（`ast.walk` 全走，不只模組層）。下限刻意壓在七分之一左右：
    # 它要抓的是「抽取器回空集合」，不是「今天剛好幾支函式」，而任何 ≥1 的值都
    # 抓得到迴圈跑了零次。壓低是為了不讓一次正常的重構把它變紅——一個會亂叫的
    # 正面對照組，最省事的修法是把數字調小，那就等於把它整個廢掉。
    assert len(defined) >= 200, (
        f"整個掃描語料只抽到 {len(defined)} 個函式定義——抽取器壞了，"
        "下面那句等於沒在檢查。")

    missing = sorted(name for name in _JOIN_GUARDS if name not in defined)
    assert not missing, (
        f"`_JOIN_GUARDS` 裡的 {missing} 在專案裡找不到同名函式。守衛被改名或搬走"
        "了，而這個名字現在誰也對不上——被它守住的接合站點會安靜地變回「沒守住」"
        "，或者更糟：如果那些站點同時被列進 `_JOIN_GUARD_EXEMPT`，就完全沒人在看"
        "了。改名的話這裡要一起改。")


def test_the_join_guard_exemptions_are_not_stale():
    """具名例外是 fail-open 的：函式改名（或整個檔案被跳過）之後，例外會變成一個
    永遠不再命中的字串，而那個函式就這樣安靜地失去檢查。所以兩個方向都要對帳。
    """
    import ast as _ast

    sources = {p.name: p for p in _join_scan_sources()}
    # 正面對照組：抽取器回空集合時，「沒有過期條目」與「全部都對」一模一樣。
    assert len(sources) >= _JOIN_SCAN_FILE_FLOOR, (
        f"只抽到 {len(sources)} 個檔案，範圍壞了")
    assert _JOIN_GUARD_EXEMPT, "例外清單被清空了，下面的迴圈會空轉通過"

    missing = []
    seen_total = 0
    for filename, funcname in sorted(_JOIN_GUARD_EXEMPT):
        path = sources.get(filename)
        if path is None:
            missing.append(f"{filename} 已經不在掃描範圍裡（改名？被豁免了？）")
            continue
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        names = {node.name for node in _ast.walk(tree)
                 if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
        # 逐檔的正面對照只問「這個檔真的被解析出函式了嗎」。原本這裡寫的是
        # `len(names) >= 5`，那個數字暗自假設「會列例外的都是大模組」——
        # `_bot_prompts.py` 只有一支公開函式，於是一筆完全正確的新例外把守門弄紅
        # 了。**一個會對合法輸入亂叫的正面對照組比沒有對照組更糟**：下一個人只會
        # 把數字調小到綠為止，然後這一格就永遠是裝飾品。真正的「抽取器壞了」由
        # 迴圈外那個跨檔案的總數來擋。
        assert names, f"{filename} 一個函式都沒抽到——抽取器壞了"
        seen_total += 1
        if funcname not in names:
            missing.append(f"{filename} 裡沒有 `{funcname}`")
    # ⚠️ **這裡不可以寫常數下限**，而且上面那段註解就是為了同一個毛病寫的——
    # 原本這一行是 `seen_total >= 5`，跟它警告的 `len(names) >= 5` 是同一個錯誤，
    # 只是隔了十行沒被看見。`_JOIN_GUARD_EXEMPT` 是**要縮的**清單（這一輪才拿掉
    # 兩筆，目前 8 筆），常數下限會在它縮到 5 以下時、為了一個**好**改動變紅，
    # 而下一個人最省事的動作就是把數字調小 → 這一格變裝飾品。
    #
    # 綁在清單自己的長度上就沒有這個問題：它問的是「每一筆例外的檔案都真的在
    # 掃描語料裡」，這件事在清單怎麼縮都成立。（順帶修掉訊息的單位：`seen_total`
    # 是**逐條目**累加的，原本卻寫「個檔案」——實測 8 條目分佈在 6 個檔案。）
    assert seen_total == len(_JOIN_GUARD_EXEMPT), (
        f"{len(_JOIN_GUARD_EXEMPT) - seen_total} 筆例外的**檔案**不在掃描語料裡"
        "（改名？被加進跳過清單？）——範圍壞了，下面的對帳等於沒在跑。")
    assert not missing, (
        "`_JOIN_GUARD_EXEMPT` 這幾筆對不到東西：%s。改名或刪除時要一起更新，"
        "否則例外只是留著一個永遠不命中的字串。" % missing)

    # 另一個方向：例外必須真的還在被需要，否則它是在遮蔽一個已經修好的東西。
    rows, _roots = _scan_derived_joins()
    still_flagged = {(f, fn) for f, fn, _ln, _e, guarded in rows if not guarded}
    pointless = sorted(k for k in _JOIN_GUARD_EXEMPT if k not in still_flagged)
    assert not pointless, (
        "這些已經不會被標出來了，例外可以拿掉：%s。留著等於替未來的回歸"
        "先開好一張免死金牌。" % pointless)

    # 第三個方向：**數量**。上面兩個都是 per-function 的，所以一支已經有例外的
    # 函式再長出一個新站點時，兩個都不會說話——新站點直接繼承那張免死金牌。
    # 2026-09-11 放寬抽取器時實際發生過：`allocate_output_dir` 從 1 變 2。
    actual_counts = {}
    for filename, funcname, _ln, _expr, guarded in rows:
        if not guarded:
            key = (filename, funcname)
            actual_counts[key] = actual_counts.get(key, 0) + 1
    drifted = sorted(
        (key, declared, actual_counts.get(key, 0))
        for key, (declared, _reason) in _JOIN_GUARD_EXEMPT.items()
        if declared != actual_counts.get(key, 0))
    assert not drifted, (
        "例外遮住的站點數跟宣告的對不上（`(鍵, 宣告, 實際)`）：%s。\n"
        "變多＝有**新的**接合滑進了一筆為了別的站點寫的例外，沒有人看過它——"
        "去看那支函式新增的那一行，確認原本的理由真的也適用於它，確認完再把"
        "數字改上來。變少＝那筆理由可能只剩一半還成立，順便覆核。" % drifted)


def test_the_output_dir_exemption_still_points_at_a_real_guard():
    """`allocate_output_dir` 的例外前提：守衛在上游的 `character_folder_name`。

    這不是形式主義。2026-09-10 的缺陷就是那支上游守衛**當時還不存在**：佇列裡一行
    `..` 原封不動變成資料夾名，而 `Path("output") / ".."` 不會被 pathlib 正規化，
    一整個角色的圖無聲寫進 repo 根目錄。把 `_is_safe_folder_component` 從那支拿掉
    （或讓 `allocate_output_dir` 不再是唯一消費端），這筆例外的理由就沒了——而
    `test_the_join_guard_exemptions_are_not_stale` 只看得到「函式還在」。
    """
    import ast as _ast

    src = (Path(b.__file__).resolve().parent / "_webrunner_shared.py")
    tree = _ast.parse(src.read_text(encoding="utf-8"), str(src))
    fn = None
    for node in _ast.walk(tree):
        if (isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and node.name == "character_folder_name"):
            fn = node
    assert fn is not None, (
        "`character_folder_name` 不見了——`allocate_output_dir` 的例外靠它，"
        "改名的話這支守門與那筆例外的理由都要跟著改。")
    called = {n.func.id for n in _ast.walk(fn)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)}
    assert "_is_safe_folder_component" in called, (
        "`character_folder_name` 不再呼叫 `_is_safe_folder_component` 了。"
        "`allocate_output_dir` 在 `_JOIN_GUARD_EXEMPT` 裡的理由就是「守衛在上游」"
        "——上游沒有守衛的話，那筆例外正在遮蔽一個真的洞。")


def test_the_host_path_exemption_still_has_its_owner_gate():
    """`resolve_host_path` 的例外前提：沒有沙箱，閘門在呼叫端的身分檢查。

    所以前提有兩半，兩半都要驗：那些檔案進出的 helper，每一個呼叫端都必須**只**從
    一個已登記的擁有者閘後面到得了；而那些閘本身還在。任何一半破掉，「不做沙箱」
    就從刻意的設計變成一個任意檔案讀寫。

    ⚠️ **「只有 `host` 群組到得了」是錯的前提，寫成那樣會誤報。** CLAUDE.md 明寫這
    套有**三個**表面，而且三條派發路徑拿到的識別字不同（斜線是 qualified name、
    `!` 是 head、mention 是 head_lower），所以三個閘是三份各自的集合。`cmd_get` /
    `cmd_put` 除了 `/host get`・`/host put` 之外，也從 `on_message` 的 `!get` /
    `!put` 到得了——那是**另一道已登記的閘**（`_OWNER_ONLY_BANGS`），不是繞道。
    只認斜線那一道會把它判成違規，而「會亂叫的守門就是會被關掉的守門」。

    所以這裡按表面分類，逐一問「這個表面的閘收了它沒有」，並且**不認得的表面一律
    算違規**（fail-closed）——新開一條第四種派發路徑會在這裡變紅，那正是需要有人
    去補閘的時刻。
    """
    import ast as _ast

    assert "host" in b._OWNER_ONLY_GROUPS, (
        "`host` 不在 `_OWNER_ONLY_GROUPS` 裡了。`resolve_host_path` 刻意不做沙箱"
        "，唯一的閘門就是這個——拿掉它等於把任意主機路徑讀寫開給每一個能在指定"
        "頻道發言的人。")

    tree = _ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    helpers = {"read_host_file", "write_host_file", "resolve_host_path",
               # `set_shell_cwd` 走的是同一筆例外的同一個理由（刻意不做沙箱），
               # 所以它的呼叫端也必須在同一道身分閘後面。2026-09-10 補進來：
               # 在那之前它連接合守門都看不見（`ast.AnnAssign` 的盲點）。
               "set_shell_cwd"}

    # ── `helpers` 自己是一份列舉，所以它自己也 fail-open（2026-09-10 實測）。
    # 拿掉 `"set_shell_cwd"` 之後整個檔案 **355 passed**——`assert callers` 那道
    # 正面對照組擋不住，因為別的名字還在，集合照樣非空。這正是 CLAUDE.md 對
    # `_OWNER_ONLY_SLASH` 寫的形狀：一筆消失的列舉條目沒有任何症狀。
    #
    # 兩個方向各補一道，兩道的判準不同、缺一不可：
    #   (1) `_JOIN_GUARD_EXEMPT` 裡每一筆 `_gui_control.py` 的例外都必須列在這裡
    #       ——「不做沙箱，閘門在呼叫端的身分檢查」這句話就是它們的前提，而這支
    #       測試是唯一在驗那句話的地方。新增例外卻忘了接線 → 紅。
    #   (2) 這裡的每一筆都必須真的**貢獻到** `callers`，或者明說它是「透過另一支
    #       helper 間接涵蓋」。少了這一道，`"read_host_file"` 這種不在例外清單裡
    #       的名字被拿掉仍然沒有症狀。
    gui_src = (Path(b.__file__).resolve().parent / "_gui_control.py").read_text(
        encoding="utf-8")
    gui_tree = _ast.parse(gui_src)
    gui_fns = {n.name: n for n in _ast.walk(gui_tree)
               if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
    gui_defs = set(gui_fns)

    gui_exempt = {fn for filename, fn in _JOIN_GUARD_EXEMPT
                  if filename == "_gui_control.py"}
    assert gui_exempt, "`_gui_control.py` 一筆例外都抽不到——鍵的形狀改了？"
    # 檔案進出那一半**用推的**，不要再手寫第二份列舉：`_gui_control.py` 裡每一支
    # 呼叫 `resolve_host_path` 的函式，依定義就是「把使用者打的路徑變成主機路徑」
    # 的那一族。今天推出 `read_host_file` / `write_host_file`；哪天多一支
    # `append_host_file`，它會**自動**進到這裡而不是等人記得補。
    path_consumers = {name for name, fn in gui_fns.items()
                      if any(isinstance(c, _ast.Call)
                             and isinstance(c.func, _ast.Name)
                             and c.func.id == "resolve_host_path"
                             for c in _ast.walk(fn))}
    assert len(path_consumers) >= 2, (
        f"`_gui_control.py` 只有 {sorted(path_consumers)} 在呼叫 "
        "`resolve_host_path`——推導壞了，下面的對帳等於沒在檢查。")
    required = gui_exempt | path_consumers
    # 組合本身要釘住。`helpers` 今天已經含了這兩族，所以把 `path_consumers` 從
    # `required` 拿掉在真實資料上**量不出來**（實測：那個變異存活）——它只在
    # 「同時有人從 `helpers` 拿掉一個名字」時才會顯現，而那正是兩道防護互相遮蔽。
    assert path_consumers <= required and gui_exempt <= required, (
        "`required` 少了其中一族。兩族的來源不同、缺一不可：例外那一族來自 "
        "`_JOIN_GUARD_EXEMPT`（前提是「閘門在呼叫端」），檔案進出那一族是從 "
        "`resolve_host_path` 的呼叫端推出來的。")
    missing = sorted(required - helpers)
    assert not missing, (
        f"這幾支主機路徑 helper 沒有被這支前提測試涵蓋：{missing}。"
        "`_JOIN_GUARD_EXEMPT` 那幾筆例外的理由都是「閘門在呼叫端的身分檢查」，"
        "而這裡是唯一在驗那句話的地方——沒接上就等於那筆例外沒有人驗前提；"
        "而檔案進出那一族是推出來的，漏掉就是一條沒人問過的任意檔案讀寫路徑。")

    # `resolve_host_path` 的呼叫端全在 `_gui_control.py` 內部（`read_host_file` /
    # `write_host_file` 各叫一次），bot 從來不直接叫它。所以它在 `helpers` 裡是
    # 一筆**間接涵蓋**的條目，不是死條目——但這件事必須寫下來並且對帳，否則它跟
    # 一筆真的死掉的條目在輸出上一模一樣（`DEBUG_DIR` 就是那樣活了很久）。
    _INDIRECT_VIA = {"resolve_host_path": tuple(sorted(path_consumers))}

    per_helper = {}
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for n in _ast.walk(fn):
            if isinstance(n, _ast.Attribute) and n.attr in helpers:
                per_helper.setdefault(n.attr, set()).add(fn.name)
    callers = set()
    for names in per_helper.values():
        callers |= names
    assert callers, "bot 端一個呼叫端都找不到——抽取器壞了"

    dead = []
    for helper in sorted(helpers):
        assert helper in gui_defs, (
            f"`{helper}` 在 `_gui_control.py` 裡沒有這個函式了——一個對不到符號的"
            "名字永遠不會命中任何呼叫端，等於這一格保護不存在。")
        if per_helper.get(helper):
            continue
        parents = _INDIRECT_VIA.get(helper, ())
        # 間接涵蓋要驗兩件事：`_gui_control.py` 裡那條呼叫線還在，而且那些父項
        # 自己真的被 bot 叫到。少驗任何一件，這個豁免就只是一句沒人查的話。
        reached = False
        for parent in parents:
            parent_fn = gui_fns.get(parent)
            if parent_fn is None or not per_helper.get(parent):
                continue
            if any(isinstance(c, _ast.Call) and isinstance(c.func, _ast.Name)
                   and c.func.id == helper for c in _ast.walk(parent_fn)):
                reached = True
                break
        if not reached:
            dead.append(helper)
    assert not dead, (
        f"`helpers` 裡這幾筆從 bot 那一側完全到不了：{dead}。直接呼叫端不見了"
        "（改名／改成別的寫法），或者間接涵蓋那條線斷了——兩種都讓這一格靜靜地"
        "變窄，而集合非空所以 `assert callers` 看不出來。")

    # 每一個呼叫端從哪些函式到得了。
    reached_from = {}
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        used = {n.id for n in _ast.walk(fn) if isinstance(n, _ast.Name)}
        for name in used & callers:
            decorators = " ".join(_ast.unparse(d) for d in fn.decorator_list)
            reached_from.setdefault(name, set()).add((fn.name, decorators))

    assert _SLASH_HANDLER_QUALIFIED, (
        "抽不到任何「處理函式 → qualified name」的對應——下面的斜線那一格會把每個"
        "呼叫端都判成違規，或（更糟）判成不認得的表面。")

    # 三個表面各一道閘，識別字各不相同。不認得的表面一律算違規。
    bad = {}
    for name, sites in sorted(reached_from.items()):
        for fn_name, decorators in sorted(sites):
            qualified = _SLASH_HANDLER_QUALIFIED.get(fn_name)
            if qualified is not None:
                # 斜線面。**用 production 的判準本身**，不要比對裝飾器的文字——
                # `/host sh cd` 掛的是 `host_sh.command`，比字串會把一個有閘的
                # 巢狀子指令判成違規。
                if not b._is_owner_only_slash(qualified):
                    bad[f"{name}（/{qualified}）"] = ["斜線面沒有擁有者閘"]
                continue
            if fn_name == "on_message":
                heads = _heads_reaching(tree, "on_message", name)
                assert heads, (
                    f"`on_message` 用到了 `{name}`，卻抽不到任何 `head == …` 的"
                    "分支——抽取器跟不上派發器的寫法了，下面等於沒在檢查。")
                unlocked = sorted(heads - set(b._OWNER_ONLY_BANGS))
                if unlocked:
                    bad[f"{name}（`!` 面）"] = unlocked
                continue
            if fn_name == "_handle_mention":
                heads = _heads_reaching(tree, "_handle_mention", name)
                assert heads, (
                    f"`_handle_mention` 用到了 `{name}`，卻抽不到任何路由到它的"
                    "子指令——抽取器跟不上派發器的寫法了。⚠️ 這一面用的是 dict "
                    "字面值派發，不是 `if head_lower == …`；在 2026-09-11 之前"
                    "這支抽取器**只讀 `ast.If`**，於是這裡恆為空集合，這句斷言"
                    "會以一個誤導的理由失敗（實際上是抽取器瞎了，不是派發器變了）。")
                unlocked = sorted(heads - set(b._OWNER_ONLY_MENTIONS))
                if unlocked:
                    bad[f"{name}（mention 面）"] = unlocked
                continue
            bad[f"{name}（{fn_name}）"] = [decorators or "（沒有裝飾器）"]
    assert not bad, (
        "這幾支檔案進出的 handler 從沒有擁有者閘的地方也到得了：%s。"
        "`resolve_host_path` 的例外前提是「閘門在呼叫端的身分檢查」——三個表面"
        "（斜線／`!`／mention）各有各的集合，任何一條路徑沒被收進去就是繞道。" % bad)

    # 正面對照組（§8.8(A3)）：上面全部是「不准出現」的形狀，分類器只要通通歸到
    # 第一類就永遠綠。所以明確要求 `!` 那一面**真的**被走到過至少一次——今天
    # `cmd_get` / `cmd_put` 就是這樣，而它正是這支測試一開始誤判成違規的那一筆。
    bang_covered = {name for name, sites in reached_from.items()
                    if any(fn == "on_message" for fn, _d in sites)}
    assert bang_covered, (
        "沒有任何一個檔案進出 handler 是從 `!` 面到得了的——分類器可能整個沒被"
        "走到，或者派發器改寫過了。這一支測試的價值就在那一格。")


# --------------------------------------------------------------------------
# 桌面控制（`_gui.*`）的可達性 ── 「每一條碰得到主機的使用者路徑都在擁有者閘
# 後面」這句話，2026-09-10 之前只寫在 `set_shell_cwd` 例外的**散文**裡。
#
# 散文守不住東西，而且那一段散文本身就被發現寫錯過一次（它說 `schedule *` 在
# `_OWNER_ONLY_SLASH`，那一句在 schedule 升級成群組規則之後就過期了）。上面那支
# `test_the_host_path_exemption_still_has_its_owner_gate` 只問四支**檔案進出**的
# helper；桌面控制的表面比那大一個量級——實測 41 支函式直接碰 `_gui.*`，遞移展開
# 之後 142 支函式到得了它，收斂成 125 條使用者派發路徑。
#
# ⚠️ **兩跳不夠，要遞移閉包。** 真實的鏈是三跳：`_gui.mouse_click` 在
# `cmd_click` 裡 → `cmd_click` 被 `slash_input_click` 呼叫 → 後者才是掛裝飾器的
# 那一支。只看兩跳的話 `cmd_click` 會被歸成「不是派發面」的內部函式，於是整批
# 桌面指令從分類器底下走過去——**而輸出會長得跟「全部都有閘」一模一樣**。
# --------------------------------------------------------------------------
def _functions_reaching_module_attr(tree, alias):
    """回傳「呼叫得到 `<alias>.<attr>` 的函式名」的**遞移**閉包。

    先找直接出現 `alias.<attr>` 的函式，再沿著「誰引用了這些名字」往呼叫端方向
    做不動點。方向是往上（往呼叫端）而不是往下，所以 `safe_reply` 這種被大家呼叫
    的共用 helper 不會被拉進來——閉包的成長是收斂的，不是爆炸的。
    """
    import ast as _ast

    fns = {}
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            fns.setdefault(node.name, node)
    reaching = {
        name for name, fn in fns.items()
        if any(isinstance(n, _ast.Attribute) and isinstance(n.value, _ast.Name)
               and n.value.id == alias for n in _ast.walk(fn))}
    refs = {name: {n.id for n in _ast.walk(fn) if isinstance(n, _ast.Name)}
            & set(fns) for name, fn in fns.items()}
    for _ in range(64):  # 不動點；上界只是防呆
        grew = False
        for name, used in refs.items():
            if name not in reaching and used & reaching:
                reaching.add(name)
                grew = True
        if not grew:
            break
    return reaching


# `on_message` 是 `!` 派發器、`_handle_mention` 是 mention 派發器。兩者都吃使用者
# 輸入，但它們的閘是**逐個 head** 判的，所以底下另外拆開來看，不能整支放行。
# 派發器 → 它代表的表面。**不要拆成兩份平行的 tuple 再 `zip`**（2026-09-20 修）：
# 這裡原本是 `zip(_KNOWN_DISPATCHERS, ("bang", "mention"))`，寫在兩個地方。多一個
# 派發器的時候 `zip` 會**安靜地把它丟掉**——它的進入點從此沒有人歸屬，而這份對帳照樣
# 全綠。下面那支測試的 docstring 自己就寫著「『沒有違規』與『沒看見』在輸出上一模一樣」，
# 而那正是舊寫法留下的缺口。改成一份 dict 之後，新增派發器就非得同時寫出它的表面不可。
_DISPATCHER_SURFACE = {"on_message": "bang", "_handle_mention": "mention"}
_KNOWN_DISPATCHERS = tuple(_DISPATCHER_SURFACE)


def _heads_reaching(tree, dispatcher_name: str, target: str) -> set:
    """`dispatcher_name` 裡，哪些 head 值路由得到 `target`。

    **兩種派發寫法都要讀**，因為這個 repo 兩種都有用：

    | 派發器 | 寫法 | `ast.If` 判準看得到的 head 值 |
    |---|---|---|
    | `on_message`（`!` 面） | `elif head == "x": …` | **154**（實測 2026-09-11） |
    | `_handle_mention`（`@bot` 面） | `handlers = {...}` ＋ `.get(head_lower)` | **0** |

    ⚠️ 那個 0 不是「mention 面沒有任何子指令」，是**這個表面根本沒被讀過**——而兩者
    在輸出上分不出來（§8.8(A3)）。實測 dict 那一路看得到 22 個 key、23 支被呼叫的
    函式，與 `ast.If` 那一路的交集是**空的**：兩條路完全不重疊，所以少讀一條就是整
    個表面消失。

    ⚠️ **這支原本巢狀在 `test_the_host_path_exemption_still_has_its_owner_gate` 裡
    面。** 巢狀的後果不只是「不好測」：它跟 `_dispatch_entry_points` 用同一條判準，
    而 `_dispatch_entry_points` 在 2026-09-11 已經接上 dict 派發、這一支沒有——
    「同一條規則的兩份實作，各自有自己的綠測試，卻沒有任何東西在比對它們」。
    提到模組層之後才有辦法拿同一份合成語料同時問兩支
    （`test_the_two_dispatch_extractors_agree_on_one_corpus`）。

    今天兩邊都綠，是因為 mention 面沒有任何東西碰得到 `_gui.*`。也就是說這個分歧
    **完全沒有症狀**——一如既往。
    """
    import ast as _ast

    dispatcher = next(
        (n for n in _ast.walk(tree)
         if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
         and n.name == dispatcher_name), None)
    assert dispatcher is not None, f"派發器 `{dispatcher_name}` 不見了"
    found = set()
    for node in _ast.walk(dispatcher):
        if not isinstance(node, _ast.If):
            continue
        test = node.test
        if not (isinstance(test, _ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], (_ast.Eq, _ast.In))
                and isinstance(test.left, _ast.Name)
                and test.left.id in ("head", "head_lower")):
            continue
        right = test.comparators[0]
        elts = (right.elts if isinstance(right, (_ast.Tuple, _ast.List, _ast.Set))
                else [right])
        names = {e.value for e in elts
                 if isinstance(e, _ast.Constant) and isinstance(e.value, str)}
        if not names:
            continue
        calls = {c.func.id for stmt in node.body for c in _ast.walk(stmt)
                 if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Name)}
        if target in calls:
            found |= names
    # 第二條路：dict 字面值派發。走共用的抽取器，不要在這裡再寫一份——這支測試
    # 的存在理由就是「同一條判準不要有兩份實作」。
    for head, called in _dict_dispatch_map(tree, dispatcher_name).items():
        if target in called:
            found.add(head)
    return found


def _dict_dispatch_map(tree, dispatcher_name: str = "_handle_mention") -> dict:
    """`dispatcher_name` 裡的 dict 字面值派發 → `{子指令: {被呼叫的函式名}}`。

    **`@bot <子指令>` 那一面不是用 `if head_lower == …` 派發的**，是一個 dict
    字面值加 `handlers.get(head_lower)`。所以只認 `ast.If` 的抽取器在這個表面上
    永遠抽到零——不是「零條路徑」，是**這個表面根本沒被讀過**，而兩者在輸出上
    分不出來。實測 2026-09-11：dict 1 個、鍵 22 個、值全部是 `ast.Lambda`。

    ⚠️ `_handle_mention` 裡確實有一個 `ast.If` 提到 `head_lower`，但那是**閘門
    本身**（`head_lower in _OWNER_ONLY_MENTIONS and …`，一個 `BoolOp`），不是
    路由分支。所以「抽到 1 個分支」這個數字會讓下一個人去找一個不存在的東西——
    在這支抽取器的判準下，正確的數字是 **0**。

    抽成共用函式而不是各自再寫一份：`test_mention_surface_gates_the_host_commands_
    it_exposes` 本來就在走同一個 dict，而「同一條判準的兩份實作」是本專案反覆
    踩到的形狀。2026-09-11 第三個消費端 `_heads_reaching` 也接上來了——它原本
    只讀 `ast.If`，於是整個 mention 面對它是隱形的。

    `dispatcher_name` 可以指定，是為了讓這支不必知道「今天只有 mention 面用 dict
    派發」。`on_message` 走這條會回空 dict（它是 `elif` 鏈），這是正確答案，不是
    抽不到——所以呼叫端可以無條件把兩條路 union 起來。
    """
    import ast as _ast

    handler = next((n for n in _ast.walk(tree)
                    if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                    and n.name == dispatcher_name), None)
    if handler is None:
        return {}
    out: dict = {}
    for node in _ast.walk(handler):
        if not isinstance(node, _ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, _ast.Constant)
                    and isinstance(key.value, str)):
                continue
            called = {call.func.id for call in _ast.walk(value)
                      if isinstance(call, _ast.Call)
                      and isinstance(call.func, _ast.Name)}
            out.setdefault(key.value, set()).update(called)
    return out


_PARITY_CORPUS = '''
async def on_message(message):
    head = message.content.split()[0]
    if head == "alpha":
        do_thing()
    elif head in ("gamma", "delta"):
        do_thing()
    await _handle_mention(message)


async def _handle_mention(message):
    head_lower = message.content.lower()
    handlers = {
        "beta": lambda: do_thing(),
        "quiet": lambda: _something_else(),
    }
    if head_lower in _OWNER_ONLY_MENTIONS and message.author.id != OWNER:
        return
    handler = handlers.get(head_lower)
    if handler:
        handler()
'''


def test_the_two_dispatch_extractors_agree_on_one_corpus():
    """同一條規則的兩份實作，餵**同一份**合成語料，答案必須一致。

    ⚠️ 這支測試在真實語料上沒有鑑別力，所以它必須用合成語料。理由是實測出來的：
    `_handle_mention` 的 `ast.If` 判準抽到 **0** 個 head 值、dict 判準抽到 22 個，
    兩者交集為**空**——但今天 mention 面沒有任何東西碰得到 `_gui.*`，所以兩支抽取器
    的分歧**一個症狀都沒有**。拿正式資料比對，兩邊都會回空集合而「一致」。

    ⚠️ **語料必須含一個「只有其中一支原本認得」的形狀**，否則這支測試今天就是裝飾
    （§8.8(A3)）。`"beta"` 只寫在 dict 字面值裡，而 `_handle_mention` 裡**一個
    `ast.If` 路由分支都沒有**（下面直接斷言這件事）——只讀 `ast.If` 的版本對它恆為
    空集合。`"alpha"` / `"gamma"` / `"delta"` 走 `elif` 鏈，兩支本來就都認得，它們
    在這裡是「不要為了修 mention 而弄壞 `!` 面」的反向對照。
    """
    import ast as _ast

    tree = _ast.parse(_PARITY_CORPUS)
    mention_fn = next(n for n in _ast.walk(tree)
                      if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                      and n.name == "_handle_mention")
    routing_ifs = [n for n in _ast.walk(mention_fn)
                   if isinstance(n, _ast.If)
                   and isinstance(n.test, _ast.Compare)]
    assert not routing_ifs, (
        "合成語料的 `_handle_mention` 長出了 `if head_lower == …` 路由分支——"
        "那樣 `\"beta\"` 就不再是「只有 dict 那一路看得到」的形狀，這支測試會退化成"
        "裝飾（兩支抽取器本來就都認得 `ast.If`）。")

    # 兩支各自的答案。
    heads = {d: _heads_reaching(tree, d, "do_thing") for d in _KNOWN_DISPATCHERS}
    rows = _dispatch_entry_points(tree, {"do_thing"}, {})
    surface_of = _DISPATCHER_SURFACE
    projected = {d: {ident for surface, ident, fn in rows
                     if surface == surface_of[d] and fn == "do_thing"}
                 for d in _KNOWN_DISPATCHERS}

    assert heads == projected, (
        "兩支抽取器對同一份語料給出不同答案（`_heads_reaching` vs "
        "`_dispatch_entry_points`）：\n  _heads_reaching = %s\n  entry_points = %s\n"
        "它們用同一條判準，分頭維護的下場就是其中一支被擴充、另一支沒有——"
        "2026-09-11 之前就是這樣：`_dispatch_entry_points` 讀得到 dict 字面值派發，"
        "`_heads_reaching` 讀不到，而兩邊的測試都是綠的。" % (heads, projected))
    # 正面對照：答案本身要是對的，不能兩支一起錯成空集合。
    assert heads == {"on_message": {"alpha", "gamma", "delta"},
                     "_handle_mention": {"beta"}}, heads
    # 反向：`unattributed` 那條 fail-closed 出口不可以被這次改動誤觸發。
    assert not [r for r in rows if r[0] == "unattributed"], rows


def _dispatch_entry_points(tree, reaching, slash_qualified):
    """把 `reaching` 收斂成使用者派發面的進入點。

    回傳 `[(surface, identifier, function_name)]`，`surface` ∈
    `{"slash", "bang", "mention", "event", "unattributed"}`。後兩種是
    **fail-closed** 的出口，呼叫端一律當成違規：

    * `event` ── 帶參數的 `@client.event` 處理函式。帶參數就代表它收得到平台送來
      的使用者輸入（`on_message(message)`、`on_raw_reaction_add(payload)`），
      而**零參數**的生命週期事件（`on_ready()` / `on_resumed()`）收不到任何使用者
      輸入，所以不是派發面。這個判準是從機制推出來的，不是一份手寫白名單——
      新開一條第四種派發路徑（例如 `on_interaction(interaction)`）會自動變紅，
      那正是需要有人去補閘的時刻。
    * `unattributed` ── 派發器引用得到、卻對不到任何 `head == …` 分支的目標。
      抽取器跟不上派發器的寫法時，「沒有違規」與「沒看見」在輸出上一模一樣，
      所以這裡把它變成看得見的紅色。
    """
    import ast as _ast

    fns = {}
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            fns.setdefault(node.name, node)

    out = []
    for name in sorted(reaching):
        qualified = slash_qualified.get(name)
        if qualified is not None:
            out.append(("slash", qualified, name))

    for dispatcher, surface in _DISPATCHER_SURFACE.items():
        node = fns.get(dispatcher)
        if node is None:
            out.append(("unattributed", f"派發器 `{dispatcher}` 不見了",
                        dispatcher))
            continue
        attributed = {}
        for branch in _ast.walk(node):
            if not isinstance(branch, _ast.If):
                continue
            test = branch.test
            if not (isinstance(test, _ast.Compare) and len(test.ops) == 1
                    and isinstance(test.ops[0], (_ast.Eq, _ast.In))
                    and isinstance(test.left, _ast.Name)
                    and test.left.id in ("head", "head_lower")):
                continue
            right = test.comparators[0]
            elts = (right.elts
                    if isinstance(right, (_ast.Tuple, _ast.List, _ast.Set))
                    else [right])
            heads = {e.value for e in elts if isinstance(e, _ast.Constant)
                     and isinstance(e.value, str)}
            if not heads:
                continue
            for stmt in branch.body:
                for call in _ast.walk(stmt):
                    if (isinstance(call, _ast.Call)
                            and isinstance(call.func, _ast.Name)):
                        attributed.setdefault(call.func.id, set()).update(heads)
        referenced = {n.id for n in _ast.walk(node)
                      if isinstance(n, _ast.Name)} & reaching
        if dispatcher == "_handle_mention":
            # 這一面走 dict 字面值派發，不是 `if head_lower == …`。見
            # `_dict_dispatch_map` 的 docstring。
            for head, called in _dict_dispatch_map(tree).items():
                for target in called:
                    attributed.setdefault(target, set()).add(head)
        for target in sorted(referenced):
            heads = attributed.get(target)
            if heads:
                for head in sorted(heads):
                    out.append((surface, head, target))
            else:
                # 派發器摸得到一支會碰桌面的函式，卻對不到任何 head。
                #
                # ⚠️ **這裡原本有一個 `target.startswith(("cmd_", "mcmd_"))` 的
                # 條件，而它是一個以命名慣例為準的 fail-open。** 2026-09-11 實測：
                # 兩份除了函式名之外一模一樣的合成派發器，叫 `mcmd_shoot` 的會產生
                # 一列違規（紅），叫 `_reply_shoot` 的**一列都沒有**（綠、隱形）。
                # 而真實的 `_handle_mention` 就有四個不合前綴的被呼叫者
                # （`_reply_ping` / `_reply_uptime` / `_resolve_lang` /
                # `_send_help`）——今天沒有一個碰得到 `_gui`，所以不是活的漏洞，
                # 但「安全性取決於有沒有照命名慣例取名」不是一道守門。
                # 拿掉之後實測**多出 0 列**，所以是純收緊、不會吵。
                out.append(("unattributed", f"{dispatcher} → {target}", target))

    for name in sorted(reaching):
        node = fns.get(name)
        if node is None or name in _KNOWN_DISPATCHERS:
            continue
        if not any("client.event" in _ast.unparse(d)
                   for d in node.decorator_list):
            continue
        if node.args.args or node.args.posonlyargs or node.args.kwonlyargs:
            out.append(("event", name, name))
    return out


# 這條路上**唯一**的登記例外。`/sys doctor`・`!doctor` 刻意公開——
# `test_pipeline_commands_are_not_locked` 明文釘住它不該被鎖。前提由
# `test_the_doctor_exemption_only_ever_reports_counts_and_booleans` 對帳：它碰
# `_gui.*` 沒錯，但送出去的只有 `len(...)` 與布林分支，細節一律叫人去看擁有者
# 專屬的 `/host job list`。
_GUI_UNGATED_EXEMPT = {
    ("slash", "sys doctor"): "健康檢查，只回報計數與布林，細節導向擁有者指令。",
    ("bang", "!doctor"): "同 `/sys doctor`，同一支 `cmd_doctor`。",
}
# 實測 2026-09-10：125 條派發路徑（123 有閘 ＋ 2 條 doctor）。下限一樣抓在一半
# 左右——貼著現況的下限會對合法輸入亂叫。
_GUI_ENTRY_POINT_FLOOR = 60


def test_every_user_path_that_can_touch_the_desktop_is_owner_gated():
    """桌面控制的可達性：每一條到得了 `_gui.*` 的**使用者派發路徑**都要在一道
    已登記的擁有者閘後面。

    `CLAUDE.md` 的「Host control is owner-only」是硬規則，而它的守門到 2026-09-10
    為止都只問「名字在不在集合裡」——`_OWNER_ONLY_GROUPS` 的那些群名、
    `_OWNER_ONLY_SLASH` 的那幾筆。（原本這裡寫「九個群名」，`schedule` 同日升級
    成群組規則之後就變成十個——散文裡的計數會過期，所以這裡不寫數字。要數字請
    看下面斷言用的下限常數，那些有測試盯著。）**沒有任何東西反過來問「碰得到主機的東西，是不
    是都在那些集合裡」。** 那兩個問題不一樣：前者在有人新增一條繞過群組命名慣例
    的路徑時完全沉默（例如把桌面 helper 接進 `/sys` 或 `/gen` 底下的某支指令）。

    三個表面各一道閘、識別字各不相同（斜線是 qualified name、`!` 是 head、
    mention 是 head_lower），所以這裡按表面分類再套**production 的判準本身**
    ——比對裝飾器文字會在巢狀子群上答錯（`/host sh cd` 掛的是 `host_sh.command`）。
    """
    import ast as _ast

    tree = _ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    reaching = _functions_reaching_module_attr(tree, "_gui")
    entries = _dispatch_entry_points(tree, reaching, _SLASH_HANDLER_QUALIFIED)

    # 正面對照組（§8.8(A3)）：底下全是「不准出現」的形狀，抽取器回空集合時
    # 「零筆違規」與「全部都有閘」在輸出上一模一樣。
    assert len(reaching) >= 40, (
        f"只推出 {len(reaching)} 支碰得到 `_gui.*` 的函式——遞移閉包壞了。"
        "只做一跳的話會掉到 41 支直接接觸者，而整批桌面指令的裝飾器包裝"
        "（`slash_input_click` 之流）會整個消失。")
    assert len(entries) >= _GUI_ENTRY_POINT_FLOOR, (
        f"只收斂出 {len(entries)} 條派發路徑（下限 {_GUI_ENTRY_POINT_FLOOR}）"
        "——抽取器壞了。")
    assert _SLASH_HANDLER_QUALIFIED, (
        "抽不到「處理函式 → qualified name」的對應，斜線那一格會全部誤判。")
    # 兩個表面都要真的被走到過，否則分類器把東西通通歸進某一格也永遠是綠的。
    surfaces = {surface for surface, _ident, _fn in entries}
    for expected in ("slash", "bang"):
        assert expected in surfaces, (
            f"沒有任何一條路徑被歸成 `{expected}` 面——分類器沒被走到。")
    # 具名金絲雀：桌面控制最核心的那條路必須真的在清單裡。
    assert ("slash", "input key press", "slash_input_key_press") in entries, (
        "`/input key press` 沒被推導出來——`_gui` 的可達性推導斷了。")

    bad = {}
    for surface, identifier, fn_name in entries:
        if (surface, identifier) in _GUI_UNGATED_EXEMPT:
            continue
        if surface == "slash":
            gated = b._is_owner_only_slash(identifier)
        elif surface == "bang":
            gated = identifier in b._OWNER_ONLY_BANGS
        elif surface == "mention":
            gated = identifier in b._OWNER_ONLY_MENTIONS
        else:
            gated = False  # `event` / `unattributed`：不認得就是違規
        if not gated:
            bad.setdefault(f"{surface}:{identifier}", set()).add(fn_name)

    assert not bad, (
        "這幾條使用者路徑碰得到桌面控制（`_gui.*`），卻不在任何一道已登記的擁有者"
        "閘後面：%s。\n"
        "`CLAUDE.md`：碰得到 bot 執行所在那台機器的指令，一律在派發前就閘到 "
        "`OWNER_USER_ID`，而且**不能**靠 `user_roles`——三份 `user_roles` 都空的"
        "時候角色閘等於不存在，那正是預設狀態。\n"
        "修法是把它放進 `_OWNER_ONLY_GROUPS` 涵蓋的群（首選，fail-closed），"
        "或補進 `_OWNER_ONLY_SLASH` / `_OWNER_ONLY_BANGS` / `_OWNER_ONLY_MENTIONS`"
        "。`event` 與 `unattributed` 兩種表面代表出現了這支守門不認得的派發路徑"
        "——那要先想清楚那條路怎麼閘，不要直接加進例外。" % {
            k: sorted(v) for k, v in sorted(bad.items())})

    # 例外清單自己也 fail-open：doctor 改名或改掉表面之後，那兩筆會變成永遠不再
    # 命中的字串，而集合還在、測試還是綠的。所以反向也要對帳。
    live = {(surface, identifier) for surface, identifier, _fn in entries}
    pointless = sorted(k for k in _GUI_UNGATED_EXEMPT if k not in live)
    assert not pointless, (
        "`_GUI_UNGATED_EXEMPT` 這幾筆對不到任何實際路徑了：%s。指令改名／改群／"
        "不再碰 `_gui.*` 的話就把它刪掉——留著等於替未來的回歸先開好一張免死"
        "金牌。" % pointless)


def test_the_doctor_exemption_only_ever_reports_counts_and_booleans():
    """`/sys doctor` 例外的前提：它碰 `_gui.*`，但送出去的只有計數與布林。

    這不是形式主義。`_gui.held_inputs()` 回的是**主機上按著的按鍵名**、
    `_gui.job_list()` 回的是**背景工作的指令列**——兩個都是主機內容，直接內插進
    回覆就同時違反 Layer 1（不得外送主機內容）與這一整套擁有者閘的用意，而
    `/sys doctor` 是公開指令。今天安全是因為每一處都寫成 `len(held)` /
    `len(running_jobs)`，那是**寫法**的性質，不是宣告出來的。

    ⚠️ 前提測試必須驗前提本身，不能只驗「那個函式還在」——那是
    `test_the_join_guard_exemptions_are_not_stale` 已經記過的教訓。
    """
    import ast as _ast

    tree = _ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    doctor = next((n for n in _ast.walk(tree)
                   if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                   and n.name == "cmd_doctor"), None)
    assert doctor is not None, (
        "`cmd_doctor` 不見了——`_GUI_UNGATED_EXEMPT` 那兩筆的前提靠它。")

    # 從 `_gui.*` 綁出來的名字（`x = await asyncio.to_thread(_gui.f)` 也算）。
    def _touches_gui(node):
        return any(isinstance(n, _ast.Attribute) and isinstance(n.value, _ast.Name)
                   and n.value.id == "_gui" for n in _ast.walk(node))

    tainted = set()
    for node in _ast.walk(doctor):
        if isinstance(node, (_ast.Assign, _ast.AnnAssign)) and node.value is not None:
            if not _touches_gui(node.value):
                continue
            targets = node.targets if isinstance(node, _ast.Assign) else [node.target]
            for target in targets:
                for sub in _ast.walk(target):
                    if isinstance(sub, _ast.Name):
                        tainted.add(sub.id)
    assert tainted, (
        "抽不到任何從 `_gui.*` 綁出來的名字——抽取器跟不上 `cmd_doctor` 的寫法了，"
        "下面等於沒在檢查。")

    # 那些名字只准以 `len(...)` 的引數身分出現在送出的字串裡。
    leaked = []
    for node in _ast.walk(doctor):
        if not isinstance(node, _ast.JoinedStr):
            continue
        safe = {id(arg) for call in _ast.walk(node)
                if isinstance(call, _ast.Call) and isinstance(call.func, _ast.Name)
                and call.func.id == "len"
                for arg in call.args}
        for sub in _ast.walk(node):
            if (isinstance(sub, _ast.Name) and sub.id in tainted
                    and id(sub) not in safe):
                leaked.append(f"{sub.id}（第 {sub.lineno} 行）")
    assert not leaked, (
        "`cmd_doctor` 把從 `_gui.*` 拿到的值直接內插進送出的字串：%s。"
        "`/sys doctor` 是**公開**指令，而 `held_inputs` 是主機上按著的按鍵、"
        "`job_list` 是背景工作的指令列——那是主機內容。它在 "
        "`_GUI_UNGATED_EXEMPT` 裡的理由就是「只回報計數與布林」，這一行讓那個"
        "理由不成立了。要嘛改回 `len(...)`，要嘛這條路不該再是公開的。" % leaked)


def test_the_gui_reachability_extractor_sees_a_three_hop_chain():
    """兩支推導函式自己的正面對照組——**牙齒在這裡**。

    上面那支主測試在真實資料乾淨時（今天就是）把斷言整個刪掉不會有人紅，而且
    它更脆弱的地方在推導：閉包退化成一跳、或分類器少認一種表面，輸出都會是
    「零筆違規」。所以拿合成語料把每一條規則各問一次，正反都問。
    """
    import ast as _ast

    tree = _ast.parse(
        "@client.event\n"
        "async def on_message(message):\n"
        "    if head == '!click':\n"
        "        await cmd_click(message)\n"
        "    if head in ('!doctor', '!dr'):\n"
        "        await cmd_doctor(message)\n"
        "    if head == '!weird':\n"
        "        await cmd_weird(message)\n"
        "@client.event\n"
        "async def on_ready():\n"
        "    _watch_loop()\n"
        "@client.event\n"
        "async def on_interaction(interaction):\n"
        "    helper()\n"
        "def helper():\n"
        "    _gui.mouse_click()\n"
        "async def cmd_click(message):\n"
        "    helper()\n"
        "async def cmd_doctor(message):\n"
        "    helper()\n"
        "async def cmd_weird(message):\n"
        "    helper()\n"
        "def _watch_loop():\n"
        "    helper()\n"
        "async def slash_input_click(interaction):\n"
        "    await cmd_click(interaction)\n"
        "async def slash_fun_roll(interaction):\n"
        "    await cmd_roll(interaction)\n"
        "async def cmd_roll(interaction):\n"
        "    pass\n")

    reaching = _functions_reaching_module_attr(tree, "_gui")
    # 三跳：`helper` 直接碰 → `cmd_click` 呼叫它 → `slash_input_click` 再包一層。
    for name in ("helper", "cmd_click", "slash_input_click", "_watch_loop"):
        assert name in reaching, (
            f"`{name}` 沒被推出來：{sorted(reaching)}。閉包若退化成一跳，"
            "`slash_input_click` 這種裝飾器包裝會整批消失。")
    # 反面：沒碰到的東西不得被拉進來。閉包若寫成「往下走」（被呼叫者也算），
    # `cmd_roll` 會被錯拉進來，於是每一個公開指令都變成假違規。
    for name in ("cmd_roll", "slash_fun_roll"):
        assert name not in reaching, (
            f"`{name}` 不碰 `_gui`，卻被推進來了：{sorted(reaching)}")

    entries = _dispatch_entry_points(
        tree, reaching,
        {"slash_input_click": "input click", "slash_fun_roll": "fun roll"})

    assert ("slash", "input click", "slash_input_click") in entries
    assert ("bang", "!click", "cmd_click") in entries
    # 一個分支列了兩個 head，兩個都要各自出現——只認第一個的話 `!dr` 會靜靜地
    # 沒有閘。
    assert ("bang", "!doctor", "cmd_doctor") in entries
    assert ("bang", "!dr", "cmd_doctor") in entries
    # 不碰 `_gui` 的斜線指令不得出現，否則主測試會對每一個公開指令亂叫。
    assert not [e for e in entries if e[2] == "slash_fun_roll"], entries

    surfaces = {(s, i) for s, i, _fn in entries}
    # fail-closed 之一：帶參數的 `@client.event` 是不認得的派發面。
    assert ("event", "on_interaction") in surfaces, (
        "帶參數的 `@client.event` 沒被標成不認得的表面——第四條派發路徑會"
        "靜靜地繞過整套閘。")
    # 反面：零參數的生命週期事件收不到使用者輸入，不是派發面。這一格若誤報，
    # `on_ready` / `on_resumed` 會讓主測試永遠紅，而「會亂叫的守門就是會被關掉
    # 的守門」。
    assert ("event", "on_ready") not in surfaces, (
        "零參數的 `on_ready` 被當成使用者派發面了——它收不到任何使用者輸入。")
    # fail-closed 之二：派發器摸得到、卻對不到 head 分支的指令處理函式。
    tree2 = _ast.parse(
        "@client.event\n"
        "async def on_message(message):\n"
        "    if message.content:\n"
        "        await cmd_sneaky(message)\n"
        "async def cmd_sneaky(message):\n"
        "    _gui.mouse_click()\n")
    reaching2 = _functions_reaching_module_attr(tree2, "_gui")
    entries2 = _dispatch_entry_points(tree2, reaching2, {})
    assert [e for e in entries2 if e[0] == "unattributed"], (
        "派發器呼叫得到一支碰 `_gui` 的處理函式、卻沒有任何 `head == …` 分支，"
        "而這沒有被標成 `unattributed`——抽取器跟不上派發器寫法時，"
        "「沒有違規」與「沒看見」會長得一模一樣。")

    # mention 那一面走 dict 字面值派發。正式資料裡沒有任何一條 mention 路徑碰得到
    # `_gui`（今天正確的答案就是 0），所以這一格**只能**用合成語料問，正反都問。
    def _mention_tree(handler_name, body):
        return _ast.parse(
            "async def _handle_mention(message):\n"
            "    head_lower = 'shoot'\n"
            f"    handlers = {{'shoot': lambda: {handler_name}(message)}}\n"
            "    handler = handlers.get(head_lower)\n"
            "    if handler is not None:\n"
            "        await handler()\n"
            f"async def {handler_name}(message):\n"
            f"    {body}\n"
            "async def on_message(message):\n"
            "    pass\n")

    hot = _mention_tree("mcmd_shoot", "_gui.mouse_click()")
    entries3 = _dispatch_entry_points(hot, _functions_reaching_module_attr(
        hot, "_gui"), {})
    assert ("mention", "shoot", "mcmd_shoot") in entries3, (
        f"dict 字面值派發的 mention 子指令沒有被歸類到 `mention` 這一格："
        f"{entries3}。`_handle_mention` 不是用 `if head_lower == …` 派發的，"
        "只認 `ast.If` 的抽取器在這個表面上永遠抽到零——而「零條路徑」與"
        "「這個表面沒被讀過」在輸出上分不出來。")

    cold = _mention_tree("mcmd_harmless", "return 1")
    entries4 = _dispatch_entry_points(cold, _functions_reaching_module_attr(
        cold, "_gui"), {})
    assert not [e for e in entries4 if e[0] == "mention"], (
        f"一個**不碰** `_gui` 的 mention 子指令也被當成進入點：{entries4}。"
        "那會讓主測試對每一個 mention 指令都亂叫，而會亂叫的守門就是會被關掉的"
        "守門。")

    # 前綴那一格：兩份除了函式**名字**之外一模一樣的派發器，答案必須相同。
    # 這裡刻意用 `on_message`（`if head` 那一面），因為那個 fail-open 兩面都有。
    def _bang_tree(handler_name):
        return _ast.parse(
            "@client.event\n"
            "async def on_message(message):\n"
            "    head = message.content\n"
            "    if message.content:\n"
            f"        await {handler_name}(message)\n"
            f"async def {handler_name}(message):\n"
            "    _gui.mouse_click()\n")

    verdicts = {}
    for name in ("mcmd_shoot", "_reply_shoot"):
        tree5 = _bang_tree(name)
        rows = _dispatch_entry_points(
            tree5, _functions_reaching_module_attr(tree5, "_gui"), {})
        verdicts[name] = [r for r in rows if r[2] == name]
    assert verdicts["mcmd_shoot"] and verdicts["_reply_shoot"], (
        f"同一個形狀、只換函式名字，答案卻不一樣：{verdicts}。\n"
        "這正是 2026-09-11 拿掉的那個 fail-open——原本的 "
        "`target.startswith((\"cmd_\", \"mcmd_\"))` 讓「有沒有被看見」取決於"
        "**有沒有照命名慣例取名**，而命名慣例不是一道守門。")


def test_the_dir_sync_exemption_still_only_ever_sees_constants():
    """`_sync_profile_dir_back` 的例外前提：`relpath` 這個參數，每一個呼叫端傳的
    都是「模組層字面值序列」的迴圈變數。

    為什麼是例外而不是紀律：跨函式推導在只看得到**一個模組**時是 fail-open 的
    （別的模組也可能呼叫它）。所以這裡把那個推導寫成一支**針對這一筆例外**的前提
    檢查——它同時證明了那個限制：它只在同一個模組裡找呼叫端，而 `_` 開頭的模組
    私有函式才適用。兩個 webrunner 變體都要驗。
    """
    import ast as _ast

    package = Path(b.__file__).resolve().parent
    checked = 0
    for filename, funcname in sorted(_JOIN_GUARD_EXEMPT):
        if funcname != "_sync_profile_dir_back":
            continue
        checked += 1
        path = package / filename
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        literal_seqs = _module_literal_sequences(tree)
        assert literal_seqs, f"{filename} 抽不到任何模組層字面值序列——抽取器壞了"

        target = None
        for node in _ast.walk(tree):
            if (isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                    and node.name == funcname):
                target = node
        assert target is not None, f"{filename} 裡沒有 `{funcname}`"
        params = [a.arg for a in target.args.args]
        assert "relpath" in params, (
            f"{filename} 的 `{funcname}` 沒有 `relpath` 參數了——前提檢查對不到"
            "東西，等於沒有在檢查。")
        index = params.index("relpath")

        # 呼叫端：整個模組裡每一個 `_sync_profile_dir_back(...)`。
        call_sites = 0
        for fn in _ast.walk(tree):
            if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            safe_here = _structurally_safe_components(fn, literal_seqs)
            for call in _ast.walk(fn):
                if not (isinstance(call, _ast.Call)
                        and isinstance(call.func, _ast.Name)
                        and call.func.id == funcname):
                    continue
                call_sites += 1
                arg = None
                if len(call.args) > index:
                    arg = call.args[index]
                else:
                    for kw in call.keywords:
                        if kw.arg == "relpath":
                            arg = kw.value
                assert isinstance(arg, _ast.Name) and arg.id in safe_here, (
                    f"{filename}:{call.lineno} 傳給 `{funcname}` 的 relpath 不是"
                    "模組層字面值序列的迴圈變數了。這筆例外的前提就是那個，"
                    "前提沒了的話它正在遮蔽一個真的洞。")
        assert call_sites == 1, (
            f"{filename} 裡 `{funcname}` 的呼叫端從 1 個變成 {call_sites} 個。"
            "多一個呼叫端就多一條要驗的路徑——不是壞事，但要有人看過。")
    assert checked == 2, (
        f"只驗到 {checked} 個 `_sync_profile_dir_back` 例外，應該是兩個變體各一。"
        "兩個 webrunner 變體必須同步。")


def _load_prompt_filename_args(tree):
    """`load_prompt(...)` 每一個呼叫端的**檔名引數**：`[(行號, AST 節點)]`。

    ⚠️ **`ast.Name` 與 `ast.Attribute` 兩種呼叫形式都要收。** 只認前者的版本對
    `_bot_prompts.load_prompt(算出來的名字)` 是**完全看不見**的，而那支測試的
    下限（`call_sites >= 4`）**擋不住它**：既有的十個 `from … import load_prompt`
    呼叫端還在，計數照樣過關。2026-09-10 實測——把那道下限整個歸零，整份
    `test_bot_helpers.py` 355 支照樣全綠，也就是說它只擋得住「全部一起換成
    Attribute 形式」，擋不住「新增一個」。正面對照組只證得了它真的執行到的那一格。

    抽成模組層純函式（而不是把同一段邏輯在測試裡抄第二份當對照組）是刻意的：
    抄本會讓對照組驗到抄本、驗不到真正在用的那一份。
    """
    import ast as _ast

    out = []
    for call in _ast.walk(tree):
        if not isinstance(call, _ast.Call):
            continue
        func = call.func
        name = (func.id if isinstance(func, _ast.Name)
                else func.attr if isinstance(func, _ast.Attribute) else None)
        if name != "load_prompt":
            continue
        arg = call.args[0] if call.args else None
        if arg is None:
            for kw in call.keywords:
                if kw.arg == "filename":
                    arg = kw.value
        out.append((call.lineno, arg))
    return out


def test_the_prompt_loader_extractor_sees_both_call_forms():
    """抽取器自己的合成對照組。

    真實原始碼今天**一個 Attribute 形式都沒有**，所以把那一半刪掉之後
    `test_the_prompt_loader_exemption_still_only_ever_sees_literals` 照樣全綠——
    這一支就是為了那一格而存在（§8.8(A3)）。
    """
    import ast as _ast

    tree = _ast.parse(
        "import _bot_prompts\n"
        "A = load_prompt('a.md', '')\n"
        "B = _bot_prompts.load_prompt('b.md', '')\n"
        "C = load_prompt(filename='c.md', default='')\n"
        "def handler(user_text):\n"
        "    return _bot_prompts.load_prompt(user_text + '.md', '')\n")
    found = _load_prompt_filename_args(tree)
    literal = [a for _ln, a in found
               if isinstance(a, _ast.Constant) and isinstance(a.value, str)]
    computed = [a for _ln, a in found
                if not (isinstance(a, _ast.Constant)
                        and isinstance(a.value, str))]
    assert len(literal) == 3, (
        f"字面值那幾筆抽到 {len(literal)} 個（應該 3）——`load_prompt(...)`、"
        "`模組.load_prompt(...)`、關鍵字 `filename=` 三種都要看得見。")
    assert len(computed) == 1, (
        f"算出來的檔名抽到 {len(computed)} 個（應該 1）。抽取器只認 `ast.Name` "
        "的話，`_bot_prompts.load_prompt(user_text + '.md')` 會整個消失——而真實"
        "原始碼乾淨時那看起來完全正常。")


def test_the_prompt_loader_exemption_still_only_ever_sees_literals():
    """`_bot_prompts.load_prompt` 的例外前提：`filename` 每一個呼叫端傳的都是
    **字串字面值**。

    這一筆是 2026-09-10 把掃描的檔案軸從 `discord_bot.py` 拉開到整個專案之後才
    浮出來的——也就是說在那之前，`BOT_PROMPTS_DIR / filename` 這個接合從來沒有被
    問過。今天沒有洞（十個呼叫端全是開機時用字面值讀固定檔名），但那是**呼叫端**
    的性質，不是這支載入器宣告出來的，跟 `cmd_fav_show` 當初的形狀一模一樣。

    為什麼是例外而不是紀律，有兩個理由，第二個才是這一筆特有的：
      1. `filename` 是參數，跨函式推導在只看得到一個模組時是 fail-open 的
         （同 `_sync_profile_dir_back`）。
      2. 守衛 `_is_unsafe_folder_name` 住在 `discord_bot.py`，而 `_bot_prompts`
         是 passive、stdlib-only 的共用模組——模組邊界不准它 import bot。真的要
         「守起來」只能在那裡再抄一份同判準的字元檢查，那是本專案已經有三份
         `_pid_alive` 的那種病，而且抄本會自己漂移。

    所以守的是前提：**沒有任何呼叫端把一個算出來的字串餵進去。** 哪天有人要加一個
    「使用者指定 prompt 檔名」的功能，這一支會在那一刻變紅，而那正是需要有人重新
    判斷的時刻。
    """
    import ast as _ast

    package = Path(b.__file__).resolve().parent
    call_sites = 0
    # 掃**跟接合守門同一份語料**（套件 ＋ repo root，去掉測試與手動工具）。原本
    # 只有 `package.glob("*.py")`，於是 repo root 的腳本在視線外——同一個「規則
    # 的文字沒有提到任何模組，掃描範圍卻寫死」的家族，而這一輪才剛為了它把接合
    # 守門拉開。用同一支函式就不會再分岔一次。
    for path in _join_scan_sources():
        tree = _ast.parse(path.read_text(encoding="utf-8"), str(path))
        for lineno, arg in _load_prompt_filename_args(tree):
            call_sites += 1
            assert (isinstance(arg, _ast.Constant)
                    and isinstance(arg.value, str)), (
                f"{path.name}:{lineno} 傳給 `load_prompt` 的檔名不是字串"
                "字面值了。`_JOIN_GUARD_EXEMPT` 裡那筆例外的前提就是這個——"
                "前提沒了的話 `BOT_PROMPTS_DIR / filename` 就是一個沒人守的接合。")

    # 正面對照組（§8.8(A3)）：抽取器比對不到任何呼叫端時，「全部都是字面值」與
    # 「一個都沒看到」在輸出上一模一樣。`load_prompt` 整支改名的話，上面的迴圈
    # 就會靜靜地跑 0 圈。（改成 Attribute 形式由 `_load_prompt_filename_args`
    # 自己的合成對照收掉——見那支的測試。）
    # ⚠️ 下限刻意**遠低於**實測值（2026-09-10 實測 10 個呼叫端），因為這一格的職責
    # 只有「迴圈有沒有跑 0 圈」——任何 ≥1 的值都做得到，而數字愈貼近實測值，它就
    # 愈容易為了一個**好**改動變紅：把 verify ＋ tooling 兩份 guidance 併成一份是
    # 很自然的整併，卻會把呼叫端往下帶。原本寫 8（只有 2 格餘裕）是本檔這一輪
    # 抓到的第三個「正面對照組會對合法改動亂叫」，另外兩個是
    # `test_the_join_guard_exemptions_are_not_stale` 的 `seen_total >= 5` 與
    # novelai 那個金絲雀。**下限要擋的是抽取器壞掉，不是清單變短。**
    assert call_sites >= 4, (
        f"整個專案只找到 {call_sites} 個 `load_prompt` 呼叫端（下限 4）——"
        "抽取器壞了，上面的迴圈等於沒在檢查。")

    # 反向：例外本身不得過期（同 `test_the_join_guard_exemptions_are_not_stale`
    # 的判準，但那支對帳的是「函式還在不在」，這裡確認的是那個接合還在不在——
    # 接合被拿掉之後例外就該跟著拿掉，留著是替未來的回歸先發免死金牌）。
    prompts = _ast.parse((package / "_bot_prompts.py").read_text(encoding="utf-8"))
    joins = [n for n in _ast.walk(prompts)
             if isinstance(n, _ast.BinOp) and isinstance(n.op, _ast.Div)
             and isinstance(n.left, _ast.Name)
             and n.left.id == "BOT_PROMPTS_DIR"]
    assert joins, (
        "`_bot_prompts.py` 裡已經沒有 `BOT_PROMPTS_DIR / …` 的接合了——"
        "請把 `_JOIN_GUARD_EXEMPT` 裡的 `('_bot_prompts.py', 'load_prompt')` 刪掉。")


def test_fav_show_skips_a_poisoned_filename_instead_of_uploading_it(
        monkeypatch, tmp_path):
    """行為面：`favorites.json` 裡一筆被動過手腳的檔名，不能變成一次上傳。

    命中就上傳到頻道，所以這是**外流**，不只是讀檔。`/fav` 不在
    `_OWNER_ONLY_GROUPS` 裡，而角色閘在三份 `user_roles` 都空的預設狀態下等於
    不存在——實際門檻只有「能在指定頻道發言」。
    """
    import asyncio as _asyncio

    output_root = tmp_path / "output"
    (output_root / "someone").mkdir(parents=True)
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    monkeypatch.setattr(b, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(b, "_load_favorites",
                        lambda: {"someone": ["../secret.png"]})

    replies = []
    files_sent = []

    async def _recorder(_message, text=None, **kwargs):
        replies.append(text)
        files_sent.extend(kwargs.get("files") or [])
        return None

    monkeypatch.setattr(b, "safe_reply", _recorder)
    _asyncio.run(b.cmd_fav_show(object(), "someone"))

    assert not files_sent, "一個被動過手腳的收藏檔名變成了一次上傳"
    assert replies and "沒有可上傳" in (replies[0] or ""), replies
    # 那個字串本身不得回聲到頻道（它依定義是可疑的路徑）。
    assert "secret" not in (replies[0] or ""), replies


def test_fav_show_rejects_a_poisoned_character_key(monkeypatch, tmp_path):
    """行為面：角色 key 那一半也要真的擋得住。

    **這一支是變異測出來的。** 靜態那支守門用 AST 問「這個名字有沒有被餵進
    `_is_unsafe_folder_name`」，所以把守衛改成 `if False and
    _is_unsafe_folder_name(character):` 對它是**隱形**的——呼叫還在，名字還在。
    只有行為測試不在乎那個語法形狀。
    """
    import asyncio as _asyncio

    output_root = tmp_path / "output"
    output_root.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    monkeypatch.setattr(b, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(b, "_load_favorites",
                        lambda: {"../elsewhere": ["secret.png"]})

    replies = []
    files_sent = []

    async def _recorder(_message, text=None, **kwargs):
        replies.append(text)
        files_sent.extend(kwargs.get("files") or [])
        return None

    monkeypatch.setattr(b, "safe_reply", _recorder)
    monkeypatch.setattr(b, "_remember_image_msg", lambda *_a, **_k: None)
    _asyncio.run(b.cmd_fav_show(object(), "../elsewhere"))

    assert not files_sent, "一個被動過手腳的角色 key 變成了一次上傳"
    assert replies and "invalid name" in (replies[0] or ""), replies


def test_fav_show_still_uploads_a_legitimate_favourite(monkeypatch, tmp_path):
    """反面：擋歸擋，正常的收藏還是要送得出去。

    少了這一支，把 `cmd_fav_show` 改成永遠不上傳也會全綠。
    """
    import asyncio as _asyncio

    output_root = tmp_path / "output"
    folder = output_root / "someone"
    folder.mkdir(parents=True)
    (folder / "someone_0001.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    monkeypatch.setattr(b, "OUTPUT_ROOT", output_root)
    monkeypatch.setattr(b, "_load_favorites",
                        lambda: {"someone": ["someone_0001.png"]})
    monkeypatch.setattr(b, "_remember_image_msg", lambda *_a, **_k: None)

    files_sent = []

    async def _recorder(_message, text=None, **kwargs):
        files_sent.extend(kwargs.get("files") or [])
        return None

    monkeypatch.setattr(b, "safe_reply", _recorder)
    _asyncio.run(b.cmd_fav_show(object(), "someone"))
    assert len(files_sent) == 1, files_sent


# ---------------------------------------------------------------------------
# 包含性檢查：解析後必須落在允許的根底下
#
# `_is_unsafe_folder_name` 的姊妹。兩者的合法值形狀相反、不能互換——那支要求「單
# 一層相對元件」，套到多層／絕對路徑上會把每一個合法值都擋掉，所以那兩處磁碟字串
# 需要的是另一種形狀。
#
# 這一族守的是兩個**真的會動到東西**的出口，都不是唯讀：
#   `_load_image_msgs`          → `_react_delete` → `p.unlink()`（刪檔）
#   `_handle_single_image_done` → `discord.File(...)`（上傳到頻道＝外流）
# ---------------------------------------------------------------------------

def test_within_allowed_roots_rejects_a_literal_dotdot_escape(tmp_path):
    """**這一支是整個函式的牙齒。**

    `Path.parents` 是純字彙運算，它不知道 ``..`` 是什麼意思。所以字面版的
    `root in p.parents` 對一個逃出去的字串會**回 True**——寫成那樣的實作看起來
    完全正常、而且對所有正常輸入都給對的答案。這裡先把那個反直覺的事實本身釘住，
    再斷言真正的函式沒有踩到它。
    """
    root = tmp_path / "output"
    root.mkdir()
    escaped = root / ".." / ".." / "Windows" / "x.png"

    # 先證明陷阱是真的存在（否則下面那句斷言可能只是碰巧成立）。
    assert root in escaped.parents, (
        "字彙版的 parents 竟然沒有含 root——這支測試的前提沒了，"
        "請重新確認 pathlib 的行為再改斷言。")
    assert b._within_allowed_roots(escaped, (root,)) is False


def test_within_allowed_roots_rejects_an_absolute_path_that_replaced_the_base(
        tmp_path):
    """Windows 上帶磁碟機的絕對值會**取代**整個 base，不是接在後面。"""
    root = tmp_path / "output"
    root.mkdir()
    assert b._within_allowed_roots(root / "C:/Windows/x.png", (root,)) is False


def test_within_allowed_roots_accepts_files_under_either_root(tmp_path):
    """正面對照組：兩個合法的根底下都要放行。

    少了這一支，「一律回 False」也會讓上面那幾支全綠——而那個實作會靜靜關掉
    🗑️／⭐ 與單張產圖的上傳。
    """
    out_root = tmp_path / "output"
    codex_root = tmp_path / "codex"
    (out_root / "someone").mkdir(parents=True)
    (codex_root / "thread1").mkdir(parents=True)
    roots = (out_root, codex_root)

    assert b._within_allowed_roots(
        out_root / "someone" / "a.png", roots) is True
    assert b._within_allowed_roots(
        codex_root / "thread1" / "b.png", roots) is True
    # 直屬子項也算（深度沒有要求，只要求落在底下）。
    assert b._within_allowed_roots(out_root / "c.png", roots) is True


def _dir_link(link: Path, target: Path) -> bool:
    """在 `link` 造一個指向 `target` 的**目錄**連結。造不出來就回 False。

    `os.symlink` 在這台機器上會丟 `WinError 1314`（沒有建立符號連結的權限，而且
    那需要開發人員模式或系統管理員），但 **junction 不需要任何特殊權限**，所以
    Windows 上退回 `mklink /J`。刻意不帶 `text=`：不解碼就不必煩惱主控台的 OEM
    字碼頁（本專案的硬規則之一），而這裡只需要結束碼。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        return False
    import subprocess
    try:
        done = subprocess.run(  # nosec B603 B607 - 固定參數，無使用者輸入
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, check=False)
    except OSError:
        return False
    return done.returncode == 0 and link.is_dir()


def test_within_allowed_roots_resolves_the_root_too(tmp_path):
    """**根目錄自己也要解析**，否則合法的路徑會被默默拒絕。

    這一條是變異測試找出來的：把 `Path(root).resolve()` 改成 `Path(root)` 之後，
    上面那批測試**全部照樣綠**（2026-09-09）。函式的 docstring 早就寫著「根目錄
    也要 resolve」，但沒有任何東西在驗它。

    為什麼這不是假想的：`output/` 被 junction 到另一顆磁碟，在這台機器上完全合理
    ——產出目錄有數 GB 的圖，而本專案自己就在檢查磁碟餘量。真的 junction 之後，
    存進 `recent_image_msgs.json` 的字串解析起來會落在**目標**磁碟上，而未解析的
    根還停在連結那一側，兩個字串永遠對不上。失敗方向是**安靜地拒絕**——🗑️／⭐
    整個失效，沒有任何錯誤訊息。
    """
    real = tmp_path / "real"
    (real / "someone").mkdir(parents=True)
    picture = real / "someone" / "a.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")

    link = tmp_path / "output"
    linked = _dir_link(link, real)
    try:
        if linked:
            root = link
            path = link / "someone" / "a.png"
            # 正面對照組：連結真的通了，而且解析後確實換了一條路徑。沒換的話
            # 這支測的就不是「有沒有解析根」，而是什麼都沒測。
            assert path.exists(), "連結造出來了但走不進去"
            assert path.resolve() != path, "解析前後一樣，這條連結沒有作用"
        else:
            # 造不出目錄連結（權限／檔案系統）就退回**同一條分界線**的可攜版本：
            # 根目錄裡帶一段 `..`。故事沒那麼真實，但打中的是同一行 `resolve()`，
            # 所以這支測試在任何機器上都不會退化成空轉。
            root = tmp_path / "real" / ".." / "real"
            path = picture
            assert Path(root).resolve() != Path(root)
        assert b._within_allowed_roots(path, (root,)) is True, (
            "合法的路徑被拒絕了——根目錄沒有被 resolve()")
    finally:
        if linked:
            # junction 要自己拆：`shutil.rmtree` 碰到它會拋（Windows 上
            # `os.path.islink` 對 junction 回 True），留著會讓 tmp 目錄清不掉。
            with contextlib.suppress(OSError):
                os.rmdir(link)


def test_within_allowed_roots_does_not_accept_the_root_itself(tmp_path):
    """根目錄自己不算「在底下」。呼叫端要的都是根底下的**檔案**，放行根本身
    沒有任何用途，只是多一個表面。"""
    root = tmp_path / "output"
    root.mkdir()
    assert b._within_allowed_roots(root, (root,)) is False


def test_within_allowed_roots_fails_closed_when_resolve_raises(tmp_path):
    """`resolve()` 丟例外時回 False（fail-closed），不得往外拋。

    在本機這個版本上 `resolve()` 對 null byte／無效磁碟機其實**不會**丟例外
    （它對不存在的路徑是純字彙運算，2026-09-09 實測），所以不能靠餵字串來驗
    這條路徑——那樣寫出來的測試會在「有沒有接住例外」兩種情況下都通過。改用一個
    `__fspath__` 會丟 `ValueError` 的替身，直接打在 `Path(...)` 的強制轉換上。
    """
    root = tmp_path / "output"
    root.mkdir()

    class _BadPath:
        def __fspath__(self):
            raise ValueError("embedded null byte")

    assert b._within_allowed_roots(_BadPath(), (root,)) is False
    # 反過來：壞掉的**根**只跳過那個根，不連坐其他根。
    assert b._within_allowed_roots(
        root / "a.png", (_BadPath(), root)) is True


def test_the_codex_image_root_is_single_sourced():
    """bot 拿的必須是後端模組那一份常數，不是自己抄的字面值。

    兩份實作其中一份遲早會漂掉，而漂掉的症狀是**靜默的**：後端產的圖在下次啟動時
    被默默判成 out-of-tree 丟掉，🗑️／⭐ 就此失效，沒有任何錯誤。
    """
    import dorossi_backend as _db
    assert b.CODEX_IMAGE_ROOT is _db.CODEX_IMAGE_ROOT
    assert _db.CODEX_IMAGE_ROOT in b._IMAGE_MSG_ROOTS
    assert b.OUTPUT_ROOT in b._IMAGE_MSG_ROOTS
    # `root=` 是測試的注入點，必須保留（拿掉的話下面那支既有測試會失去替身）。
    import inspect
    assert "root" in inspect.signature(_db._collect_codex_images).parameters


def test_load_image_msgs_drops_an_out_of_tree_path(monkeypatch, tmp_path):
    """行為面：`recent_image_msgs.json` 裡一筆逃出去的路徑不得進記憶體。

    這是**刪除**路徑不是唯讀路徑——那些 Path 會流到 `_react_delete` 的
    `p.unlink()`。
    """
    out_root = tmp_path / "output"
    (out_root / "someone").mkdir(parents=True)
    good = out_root / "someone" / "a.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n")
    outside = tmp_path / "precious.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    cache = tmp_path / "recent_image_msgs.json"
    cache.write_text(json.dumps({
        "111": [str(outside)],
        "222": [str(good)],
    }), encoding="utf-8")

    monkeypatch.setattr(b, "OUTPUT_ROOT", out_root)
    monkeypatch.setattr(b, "_IMAGE_MSG_ROOTS", (out_root,))
    monkeypatch.setattr(b, "RECENT_IMAGE_MSGS_FILE", cache)
    monkeypatch.setattr(b, "_RECENT_IMAGE_MSGS", {})

    b._load_image_msgs()

    assert 111 not in b._RECENT_IMAGE_MSGS, "逃出去的路徑被載入了"
    # 正面對照組：否則「全部都不載入」也會綠，而那等於默默關掉 🗑️／⭐。
    assert b._RECENT_IMAGE_MSGS.get(222) == [good], b._RECENT_IMAGE_MSGS
    # 被拒絕也要觸發重新 dump，把毒條目清出磁碟——只擋記憶體那一份的話，
    # 那筆條目每次啟動都會再被拒絕一次，等於把問題永久留著。
    on_disk = json.loads(cache.read_text(encoding="utf-8"))
    assert "111" not in on_disk, on_disk
    assert "222" in on_disk, on_disk


def test_load_image_msgs_skips_an_empty_string_path(monkeypatch, tmp_path):
    """`""` 不得變成一個「指向工作目錄」的快取條目。

    `Path("")` 是 `Path(".")`，`resolve()` 解成**行程的工作目錄**（實測＝ repo
    根），而 `.exists()` 對目錄回 True——所以少了那道 `if not p_str` 就會有一筆
    目錄條目被收進快取，而這條路一路通到 `_react_delete` 的 `p.unlink()`。

    今天不是活的缺陷：工作目錄不在 `_IMAGE_MSG_ROOTS` 任何一個根底下，
    `_within_allowed_roots(Path(""), …)` 實測回 False。這支是縱深防禦，並把兩個
    讀取端的不一致釘住——`_load_favorites` 對同一類輸入本來就寫 `if x`。

    斷言刻意分兩層：**進不去記憶體**（安全），以及**不得被算成 rejected**
    （語意——那個計數的意思是「磁碟上的資料被動過」，一個空字串只是壞資料，
    不該每次啟動都印一行看起來像路徑攻擊的警告）。
    """
    out_root = tmp_path / "output"
    (out_root / "someone").mkdir(parents=True)
    good = out_root / "someone" / "a.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n")

    cache = tmp_path / "recent_image_msgs.json"
    cache.write_text(json.dumps({"111": ["", str(good)]}), encoding="utf-8")

    monkeypatch.setattr(b, "OUTPUT_ROOT", out_root)
    monkeypatch.setattr(b, "_IMAGE_MSG_ROOTS", (out_root,))
    monkeypatch.setattr(b, "RECENT_IMAGE_MSGS_FILE", cache)
    monkeypatch.setattr(b, "_RECENT_IMAGE_MSGS", {})

    # 先證明陷阱是真的（否則下面的斷言可能只是碰巧成立）。
    assert Path("") == Path("."), "Path('') 不再是 Path('.')——前提沒了"
    assert Path("").resolve().is_dir(), "Path('').resolve() 不是目錄——前提沒了"

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        b._load_image_msgs()
    diag = err.getvalue()

    # 空字串不得進記憶體；合法那筆要留著（正面對照組——否則「整批丟掉」也會綠）。
    assert b._RECENT_IMAGE_MSGS.get(111) == [good], b._RECENT_IMAGE_MSGS
    for p in b._RECENT_IMAGE_MSGS.get(111, []):
        assert p.resolve() != Path("").resolve(), (
            f"空字串變成了指向工作目錄的條目：{p!r}")
    # 語意：被「跳過」而不是被「拒絕」。
    assert "rejected" not in diag, (
        f"空字串被算成 out-of-tree 攻擊而不是壞資料：{diag!r}")


def test_a_poisoned_reaction_cache_cannot_delete_a_file(monkeypatch, tmp_path):
    """**這一支是真正的判準**：走完整條路，確認目標檔案還在。

    毒過的 `recent_image_msgs.json` → `_load_image_msgs` → 觸發 🗑️。上面那支
    只看記憶體字典；這支看磁碟。兩者缺一——把檢查搬到別的地方、或改成只記錄不
    阻擋，字典那支未必抓得到。
    """
    import asyncio as _asyncio
    from types import SimpleNamespace

    out_root = tmp_path / "output"
    out_root.mkdir(parents=True)
    outside = tmp_path / "precious.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    cache = tmp_path / "recent_image_msgs.json"
    cache.write_text(json.dumps({"111": [str(outside)]}), encoding="utf-8")

    monkeypatch.setattr(b, "OUTPUT_ROOT", out_root)
    monkeypatch.setattr(b, "_IMAGE_MSG_ROOTS", (out_root,))
    monkeypatch.setattr(b, "RECENT_IMAGE_MSGS_FILE", cache)
    monkeypatch.setattr(b, "_RECENT_IMAGE_MSGS", {})
    monkeypatch.setattr(b, "_load_favorites", lambda: {})
    monkeypatch.setattr(b, "_save_favorites", lambda *_a, **_k: None)
    monkeypatch.setattr(b.client, "get_channel", lambda *_a, **_k: None)

    b._load_image_msgs()

    payload = SimpleNamespace(channel_id=b.CHANNEL_ID, message_id=111,
                              user_id=1, emoji=b.DEL_EMOJI)
    _asyncio.run(b.on_raw_reaction_add(payload))

    assert outside.exists(), (
        "一筆被動過手腳的快取條目讓 🗑️ 刪掉了產出樹外的檔案")


def test_the_trash_reaction_still_deletes_a_legitimate_file(
        monkeypatch, tmp_path):
    """反面：擋歸擋，🗑️ 對合法的圖還是要真的刪得掉。

    少了這一支，把 `_load_image_msgs` 改成一律不載入也會全綠。
    """
    import asyncio as _asyncio
    from types import SimpleNamespace

    out_root = tmp_path / "output"
    (out_root / "someone").mkdir(parents=True)
    target = out_root / "someone" / "a.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")

    cache = tmp_path / "recent_image_msgs.json"
    cache.write_text(json.dumps({"111": [str(target)]}), encoding="utf-8")

    monkeypatch.setattr(b, "OUTPUT_ROOT", out_root)
    monkeypatch.setattr(b, "_IMAGE_MSG_ROOTS", (out_root,))
    monkeypatch.setattr(b, "RECENT_IMAGE_MSGS_FILE", cache)
    monkeypatch.setattr(b, "_RECENT_IMAGE_MSGS", {})
    monkeypatch.setattr(b, "_load_favorites", lambda: {})
    monkeypatch.setattr(b, "_save_favorites", lambda *_a, **_k: None)
    monkeypatch.setattr(b.client, "get_channel", lambda *_a, **_k: None)

    b._load_image_msgs()
    assert b._RECENT_IMAGE_MSGS.get(111) == [target]

    payload = SimpleNamespace(channel_id=b.CHANNEL_ID, message_id=111,
                              user_id=1, emoji=b.DEL_EMOJI)
    _asyncio.run(b.on_raw_reaction_add(payload))

    assert not target.exists(), "合法的圖沒有被刪掉，🗑️ 壞了"


# ---------------------------------------------------------------------------
# `_template_path` —— 曾經是本檔的第三份手寫路徑紀律（2026-09-10 收掉）
#
# 舊版：字元黑名單（斜線、反斜線、`..`、開頭的 `.`）＋
#       `candidate.relative_to(TEMPLATES_DIR.resolve())`。
# 黑名單那一半正是 `_is_unsafe_folder_name` 已經換掉的錯判準——錨定到磁碟機靠的
# 是**冒號**不是斜線。所以安全來自巧合：黑名單放行 `C:foo`，只有 `relative_to`
# 那一半攔得住。哪天有人「簡化」掉後半（看起來只是少一次 resolve），前半一個磁碟
# 機字首都擋不住，而且完全無聲。
#
# 換成共用的兩支之後，4368 筆差分實測：**0 筆放寬**、0 筆換目標，唯一的差異是
# `D:foo` 這種與 base 同磁碟機的磁碟機相對名字（舊版默默當成 `foo` 開啟，新版
# 拒絕）——收緊不是放寬。
# ---------------------------------------------------------------------------

def _old_template_blacklist(name: str) -> bool:
    """舊版**字元黑名單那一半**，一字不差。

    這不是死碼：下面那支「安全來自巧合」的測試要拿它證明**前半真的擋不住**，
    否則「黑名單不夠」只是斷言，不是示範。
    """
    return ("/" in name or "\\" in name or ".." in name
            or name.startswith("."))


def _other_drive_prefix() -> str:
    """跟 TEMPLATES_DIR **不同**的磁碟機字首。

    不可以寫死 `C:`——同磁碟機時 pathlib 把 `D:foo` 當成該磁碟機的相對路徑，
    根本逃不出去（那是本 repo 剛好放在 D: 的性質，不是這條規則的性質）。
    repo 搬到 C: 之後寫死的測試會**變成綠的假象**。
    """
    here = PureWindowsPath(str(b.TEMPLATES_DIR.resolve())).drive.upper()
    return "C:" if here != "C:" else "E:"


def test_the_template_blacklist_alone_would_let_a_drive_anchor_through():
    """示範「安全來自巧合」：舊版前半放行的輸入，接上去真的會**取代整個 base**。

    這一支是這次改動的全部理由。少了它，「黑名單漏掉磁碟機字首」只是一句話。
    """
    drive = _other_drive_prefix()
    for probe in (f"{drive}foo", f"{drive}", f"{drive}$MFT"):
        # 前半：黑名單放行。
        assert not _old_template_blacklist(probe), (
            f"{probe!r} 竟然被舊黑名單擋下來了——這組測試的前提變了，要重寫")
        # 而且它真的逃得出去（否則上一行只是在測一個沒有威脅的規則）。
        joined = str(b.TEMPLATES_DIR / (probe + ".md")).lower()
        assert not joined.startswith(str(b.TEMPLATES_DIR).lower()), (
            f"{probe!r} 其實沒有逃出 TEMPLATES_DIR（{joined}）")
        # 共用守衛認得它。
        assert b._is_unsafe_folder_name(probe), probe


@pytest.mark.parametrize("suffix", ["foo", "", "$MFT", "Windows"])
def test_template_path_rejects_a_drive_anchored_name(suffix):
    """行為面：`/todo prompt template C:foo` 不得解析成產出樹外的檔案。"""
    assert b._template_path(_other_drive_prefix() + suffix) is None


@pytest.mark.parametrize("name", [
    "..", "../x", r"..\x", "sub/x", r"sub\x", ".", "/etc/passwd",
    r"\\server\share", "C:/Windows/x", "C:\\Windows\\x",
])
def test_template_path_rejects_the_classic_traversals(name):
    assert b._template_path(name) is None


@pytest.mark.parametrize("name", [".hidden", ".hidden.md", ".md", ".a"])
def test_template_path_still_refuses_a_dotfile_name(name):
    """**這一支擋的是「順手清乾淨」造成的放寬。**

    開頭的 `.` 不是路徑紀律（Windows 上 dotfile 沒有特殊意義，`..` 由
    `_is_unsafe_folder_name` 擋掉），是本指令自己的命名政策。`.hidden` 是一個
    完全合法的**單一層元件**，所以共用守衛依定義會放行它——把這一行併進共用守衛
    就等於放寬。4368 筆差分實測：拿掉它會讓 166 筆原本被拒的輸入變成可接受。
    """
    assert b._template_path(name) is None


@pytest.mark.parametrize("name,expect", [
    ("portrait", "portrait.md"),          # 自動補 `.md`
    ("portrait.md", "portrait.md"),       # 已經有就不重複補
    ("PORTRAIT.MD", "PORTRAIT.MD"),       # 副檔名比對不分大小寫
    ("my template", "my template.md"),    # 空白是合法的
    ("角色1", "角色1.md"),
    ("a.b", "a.b.md"),
])
def test_template_path_keeps_the_user_facing_shape(name, expect):
    """正面對照組：守衛收緊之後，合法名字一個都不能被擋，而且落點不能變。

    少了這一支，把 `_template_path` 改成一律回 None 也會全綠——那會讓
    `/todo prompt template <名稱>` 整個功能消失，只回一句「invalid template name」。
    """
    got = b._template_path(name)
    assert got is not None, name
    assert got == (b.TEMPLATES_DIR / expect).resolve(), got


def test_template_path_treats_a_blank_name_as_no_argument():
    """空字串／全空白＝「沒給名稱」，由呼叫端處理（`cmd_tp_template` 用它列清單）。"""
    assert b._template_path("") is None
    assert b._template_path("   ") is None


def test_template_path_applies_both_disciplines_not_just_one():
    """兩道紀律都要**真的被套用**，不是其中一道順便擋住就算。

    這正是舊版的病：前半形同虛設，全靠後半。所以分別把另一半停用，確認剩下的
    那一半自己就攔得住——`C:foo` 兩道都認得，是唯一同時測得到兩邊的輸入。

    （本來想用 symlink 造一個「(a) 放行、(b) 攔下」的真實輸入——`templates/x.md`
    指向樹外——但本機沒有建立 symlink 的權限（實測 WinError 1314），而 junction
    只能指向目錄、名字裡必然帶分隔符，(a) 先擋掉了。所以這裡改用停用法。）
    """
    probe = _other_drive_prefix() + "foo"

    # (b) 停用 → (a) 自己要攔得住。
    saved = b._within_allowed_roots
    try:
        b._within_allowed_roots = lambda *_a, **_k: True
        assert b._template_path(probe) is None, (
            "包含性檢查停用之後就守不住了——單一元件那道紀律沒有被套用")
    finally:
        b._within_allowed_roots = saved

    # (a) 停用 → (b) 自己要攔得住。
    saved = b._is_unsafe_folder_name
    try:
        b._is_unsafe_folder_name = lambda *_a, **_k: False
        assert b._template_path(probe) is None, (
            "單一元件檢查停用之後就守不住了——包含性那道紀律沒有被套用")
    finally:
        b._is_unsafe_folder_name = saved


def test_template_command_keeps_its_two_user_facing_messages(
        monkeypatch, tmp_path):
    """端到端：拒絕訊息與「不存在」訊息都不得變，而且都不得帶主機路徑。

    這兩句是使用者唯一看得到的東西。純函式測 `_template_path` 看不到它們——
    把回傳值從 None 改成丟例外，上面每一支都還是綠的，而使用者會收到一句泛用
    內部錯誤。
    """
    monkeypatch.setattr(b, "TEMPLATES_DIR", tmp_path / "templates")

    rejected = _run_reply(
        monkeypatch,
        lambda: b.cmd_tp_template(object(), _other_drive_prefix() + "foo"))
    assert rejected == "invalid template name", rejected

    missing = _run_reply(
        monkeypatch, lambda: b.cmd_tp_template(object(), "nope"))
    assert "nope" in missing and "不存在" in missing, missing
    # Secrecy Layer 1：非擁有者看得到的字串不得帶主機路徑。回聲的只有使用者
    # 自己打的名字，不是解析出來的落點。
    for text in (rejected, missing):
        assert str(tmp_path) not in text, text
        assert str(b.TEMPLATES_DIR) not in text, text


def _single_image_done_files(monkeypatch, tmp_path, rel):
    """跑一次 `_handle_single_image_done`，回傳 (送出的檔案, 回覆文字)。"""
    import asyncio as _asyncio

    files_sent = []
    replies = []

    class _Chan:
        async def send(self, content=None, **kwargs):
            replies.append(content)
            if kwargs.get("file") is not None:
                files_sent.append(kwargs["file"])
            return None

        async def fetch_message(self, _mid):
            raise RuntimeError("no reference message")

    monkeypatch.setattr(b, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(b, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(b, "_generate_append_history", lambda *_a, **_k: None)

    def _drop_coro(coro=None, *_a, **_k):
        """把排進來的 coroutine **關掉**，不是丟掉。

        `lambda *_a, **_k: None` 會讓那個 coroutine 物件沒被 await 就被回收，
        於是每跑一次就多一行 `RuntimeWarning: coroutine '_generate_pump' was
        never awaited`。那是雜訊，而雜訊唯一的作用是訓練人忽略警告。
        """
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(b, "_schedule_coro", _drop_coro)
    monkeypatch.setattr(b, "_single_image_pending",
                        {"rid": {"channel_id": None, "message_id": None}})
    monkeypatch.setattr(b.client, "get_channel", lambda *_a, **_k: None)

    _asyncio.run(b._handle_single_image_done(
        _Chan(), {"request_id": "rid", "ok": True, "path": rel}))
    return files_sent, replies


def test_single_image_done_refuses_an_out_of_tree_path(monkeypatch, tmp_path):
    """行為面：事件檔裡一個逃出去的 `path` 不得變成一次上傳。

    命中就 `discord.File(...)` 送進頻道，所以這是**外流**。事件檔是另一個行程
    寫的，bot 這一側沒有任何理由假設它的內容是乾淨的。

    逃逸的目標**真的存在、而且真的上傳得出去**（是一張合法大小的 png，就放在
    專案根目錄底下、產出根之外）。這一點很要緊：如果那個檔案不存在，後面的
    `exists()` 本來就會擋下來，這支測試就會在「有沒有包含性檢查」兩種情況下都
    通過——那是一支看起來很有道理、實際上什麼都沒驗的測試。
    """
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    assert secret.exists() and secret.stat().st_size < b.DISCORD_FILE_LIMIT

    files_sent, replies = _single_image_done_files(
        monkeypatch, tmp_path, "secret.png")

    assert not files_sent, "逃出產出樹的路徑被上傳了"
    assert replies and "路徑無效" in (replies[0] or ""), replies
    # Secrecy Layer 1：原值不得回聲到頻道。
    assert "secret" not in (replies[0] or ""), replies


def test_single_image_done_still_uploads_a_legitimate_result(
        monkeypatch, tmp_path):
    """反面：合法的 one-shot 產出仍然送得出去。

    少了這一支，把檢查改成一律拒絕也會全綠——而那等於 `/gen image` 再也不回圖。
    """
    good = tmp_path / "output" / "_oneshot" / "rid" / "oneshot_1.png"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    files_sent, replies = _single_image_done_files(
        monkeypatch, tmp_path, "output/_oneshot/rid/oneshot_1.png")

    assert len(files_sent) == 1, (files_sent, replies)


def test_a_slash_generate_result_replies_under_the_bots_ack_and_still_falls_back(
        monkeypatch, tmp_path):
    """單張產圖的結果要掛在原本那一次請求底下（2026-09-19）。

    斜線 `/gen image` 存下來的 `message_id` 是 interaction id，`fetch_message` 必定
    404——在這之前每一張斜線產的圖都白打一次 API，然後不掛參照地送出。現在走
    `_resolve_trigger_message`：斜線掛在 bot 自己回覆那一次互動的訊息底下、`@bot`
    照舊掛在發問那則底下、兩者都找不到就照舊直接送。三個方向都要測，少一個就有
    一種退化會全綠。第四格（同日後續）：ctx 裡已經有佔位訊息時直接掛在它底下，
    一次平台查詢都不打——那是常見情況，查詢只給沒有佔位訊息的時候用。
    """
    import asyncio as _asyncio
    from types import SimpleNamespace as NS

    bot_id, interaction_id = 999_000_111, 600000000000000003
    good = tmp_path / "output" / "_oneshot" / "rid" / "oneshot_1.png"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    class _Msg:
        def __init__(self, author, meta=None):
            self.author, self.interaction_metadata = NS(id=author), meta
            self.replies: list = []

        async def reply(self, content=None, **kwargs):
            self.replies.append(content)
            if kwargs.get("file") is not None:
                kwargs["file"].close()
            return self

    class _Chan:
        def __init__(self, fetched=None, history=()):
            self.fetched, self.items = fetched, list(history)
            self.sent: list = []
            self.history_calls = 0
            self.fetch_calls = 0

        async def fetch_message(self, _mid):
            self.fetch_calls += 1
            if self.fetched is None:
                raise discord.NotFound(NS(status=404, reason="Not Found"),
                                         {"code": 10008, "message": "Unknown"})
            return self.fetched

        def history(self, **_kw):
            self.history_calls += 1
            items = self.items

            async def _gen():
                for item in items:
                    yield item
            return _gen()

        async def send(self, content=None, **kwargs):
            self.sent.append(content)
            if kwargs.get("file") is not None:
                kwargs["file"].close()

    def _drop(coro=None, *_a, **_k):
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(b, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(b, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(b, "_generate_append_history", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_schedule_coro", _drop)
    monkeypatch.setattr(b, "client", NS(user=NS(id=bot_id),
                                        get_channel=lambda *_a, **_k: None))

    def run(channel, message_id, placeholder=None):
        ctx = {"channel_id": None, "message_id": message_id}
        if placeholder is not None:
            ctx["placeholder"] = placeholder
        monkeypatch.setattr(b, "_single_image_pending", {"rid": ctx})
        _asyncio.run(b._handle_single_image_done(channel, {
            "request_id": "rid", "ok": True,
            "path": "output/_oneshot/rid/oneshot_1.png"}))

    ack = _Msg(bot_id, NS(id=interaction_id, user=NS(id=7),
                          type=discord.InteractionType.application_command))
    slash = _Chan(history=[_Msg(7), ack])
    run(slash, interaction_id)
    assert ack.replies == ["🖼️ 你要的圖來了。"] and slash.sent == [], (
        "斜線產的圖沒有掛在 bot 回覆那一次互動的訊息底下")

    asked = _Msg(7)
    at_bot = _Chan(fetched=asked, history=[ack])
    run(at_bot, 222)
    assert asked.replies == ["🖼️ 你要的圖來了。"] and at_bot.history_calls == 0

    lost = _Chan(history=[_Msg(7)])
    run(lost, interaction_id)
    assert lost.sent == ["🖼️ 你要的圖來了。"], "找不到可以掛的訊息時要照舊直接送"

    # 常見情況：`_generate_pump` 已經把佔位訊息存在 ctx 裡——直接掛在它底下，一次
    # 平台查詢都不打（歷史與抓訊息都是 0）。上面三格沒有佔位訊息，走的是查詢那一條。
    placeholder = _Msg(bot_id)
    held = _Chan(fetched=asked, history=[ack])
    run(held, interaction_id, placeholder=placeholder)
    assert placeholder.replies == ["🖼️ 你要的圖來了。"], placeholder.replies
    assert (held.fetch_calls, held.history_calls, held.sent) == (0, 0, []), (
        "手上已經有佔位訊息，卻還去問平台")


def test_a_generated_image_pings_the_at_bot_asker_and_slash_stays_on_the_placeholder(
        monkeypatch, tmp_path):
    """`@bot` 起的產圖，結果回在**發問那一則**底下，而且那一則回覆會 ping 發問的人；
    斜線照舊回在佔位訊息底下；兩條都不問平台（2026-09-20）。

    產圖常常要排隊好幾分鐘，回覆預設 ping 被回覆的人（`DEFAULT_MENTIONS.replied_user`），
    那個 ping 就是「圖好了」的通知。09-19 改成優先回在 bot 自己的佔位訊息底下之後，
    被回覆的是 bot，`@bot` 發問的人就收不到了。

    走**真的提交路徑**（`mcmd_generate` → 真的 `_generate_pump`），不是直接往 ctx 塞：
    只驗結果處理那一半的話，把 `mcmd_generate` 存觸發訊息那一行拿掉也是綠的。
    `@bot` 那一格用真的 `discord.Message` 類別（只填它會碰到的欄位），因為判準就是
    `isinstance(message, discord.Message)`；斜線那一格用正式的
    `_InteractionMessageProxy`，它的 `.id` 是 interaction id。ping 驗在**送出去的
    payload** 那一層：把回覆交給 discord.py 自己的 `handle_message_parameters`，
    配上 client 的基準線。
    """
    import asyncio as _asyncio
    from types import SimpleNamespace as NS

    import discord.http

    baseline = b.client._connection.allowed_mentions
    owner, bot_id, interaction_id = b.OWNER_USER_ID, 999_000_111, 600000000000000003
    out = tmp_path / "output" / "_oneshot" / "rid" / "oneshot_1.png"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)

    class _BotMsg:
        """bot 自己送出的訊息（佔位訊息／斜線的回覆）。"""

        def __init__(self):
            self.edits: list = []
            self.replies: list = []

        async def edit(self, content=None, **_kw):
            self.edits.append(content)

        async def reply(self, content=None, **kwargs):
            self.replies.append(content)
            if kwargs.get("file") is not None:
                kwargs["file"].close()
            return self

    class _Chan:
        def __init__(self, cid):
            self.id, self.guild = cid, None
            self.sent: list = []           # (content, kwargs)
            self.fetch_calls = self.history_calls = 0
            self.placeholder = _BotMsg()

        async def send(self, content=None, **kwargs):
            if kwargs.get("file") is not None:
                kwargs["file"].close()
            self.sent.append((content, kwargs))
            return self.placeholder     # 第一則是佔位訊息；之後的回傳值沒人用

        async def fetch_message(self, _mid):
            self.fetch_calls += 1
            raise AssertionError("手上已經有回覆目標，不該再問平台")

        def history(self, **_kw):
            self.history_calls += 1
            raise AssertionError("手上已經有回覆目標，不該再翻歷史")

    monkeypatch.setattr(b, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(b, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE",
                        tmp_path / "single_image_request.json")
    monkeypatch.setattr(b, "_generate_append_history", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_sweep_stale_single_image_state", lambda *_a, **_k: None)

    async def _no_server():
        return None

    def _drop(coro=None, *_a, **_k):
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(b, "_generate_ensure_server", _no_server)
    monkeypatch.setattr(b, "_schedule_coro", _drop)
    monkeypatch.setattr(b, "client", NS(user=NS(id=bot_id),
                                        get_channel=lambda *_a, **_k: None))

    def submit_and_finish(message, channel):
        monkeypatch.setattr(b, "_generate_queue", [])
        monkeypatch.setattr(b, "_generate_inflight", None)
        monkeypatch.setattr(b, "_single_image_pending", {})
        _asyncio.run(b.mcmd_generate(message, "a cat"))
        assert len(b._single_image_pending) == 1, "提交沒有走到排隊那一步"
        (rid, ctx), = b._single_image_pending.items()
        assert b._generate_inflight == rid, "幫浦沒有把這一筆提升成在途"
        _asyncio.run(b._handle_single_image_done(channel, {
            "request_id": rid, "ok": True,
            "path": "output/_oneshot/rid/oneshot_1.png"}))
        return ctx

    # ---- @bot：回在發問那一則底下，而且會 ping 發問的人 ----
    at_chan = _Chan(4242)
    asker = discord.Message.__new__(discord.Message)
    asker.id, asker.channel, asker.guild = 222_333, at_chan, None
    asker._state = None      # `to_reference` 會讀它；真的訊息一定有
    asker.author = NS(id=owner)
    ctx = submit_and_finish(asker, at_chan)
    assert ctx.get("trigger_message") is asker, "提交時沒有把發問那一則存起來"
    results = [(c, kw) for c, kw in at_chan.sent if c == "🖼️ 你要的圖來了。"]
    assert len(results) == 1, at_chan.sent
    content, kwargs = results[0]
    reference = kwargs.get("reference")
    assert isinstance(reference, discord.MessageReference), kwargs
    assert (reference.message_id, reference.channel_id) == (asker.id, at_chan.id), (
        "結果沒有回在發問那一則底下——被回覆的不是發問的人，他收不到通知")
    assert at_chan.placeholder.replies == [], "結果回在 bot 的佔位訊息底下了"
    # 跟 `Messageable.send` 一樣：參照先轉成 dict，再交給 `handle_message_parameters`。
    with discord.http.handle_message_parameters(
            content,
            message_reference=kwargs["reference"].to_message_reference_dict(),
            allowed_mentions=kwargs.get("allowed_mentions", discord.utils.MISSING),
            mention_author=kwargs.get("mention_author"),
            previous_allowed_mentions=baseline) as params:
        payload = params.payload
    assert payload["message_reference"]["message_id"] == asker.id, payload
    assert payload["allowed_mentions"].get("replied_user") is True, (
        "回覆沒有帶 replied_user——發問的人收不到「圖好了」的通知", payload)
    assert (at_chan.fetch_calls, at_chan.history_calls) == (0, 0)

    # ---- 斜線：代理物件不是訊息，結果回在佔位訊息（bot 回覆那一次互動）底下 ----
    sl_chan = _Chan(4343)
    ack = _BotMsg()

    async def _followup_send(content=None, **_kw):
        return ack

    interaction = NS(user=NS(id=owner), channel=sl_chan, guild=None,
                     id=interaction_id, followup=NS(send=_followup_send))
    proxy = b._InteractionMessageProxy(interaction)
    assert not isinstance(proxy, discord.Message), "代理物件被當成真的訊息了"
    ctx = submit_and_finish(proxy, sl_chan)
    assert "trigger_message" not in ctx, "斜線的代理物件被存成觸發訊息"
    assert ctx.get("placeholder") is ack
    assert ack.replies == ["🖼️ 你要的圖來了。"], ack.replies
    assert [c for c, _kw in sl_chan.sent] == [], sl_chan.sent
    assert (sl_chan.fetch_calls, sl_chan.history_calls) == (0, 0)

    # ---- 別的平台：`ChatMessage` 也是真的收到的訊息，結果回在發問那一則底下 ----
    # （2026-09-24 以前只收 `discord.Message`，於是別的平台一律回在佔位訊息底下——
    # 而佔位訊息當時連 `reply` 都沒有，整張圖不見。）
    import _chat_platform as cp
    delivered: list = []

    class _Platform(cp.ChatTransport):
        name = "stubplat"

        @property
        def capabilities(self):
            return cp.PlatformCapabilities(reply_reference=True)

        async def run(self):
            return None

        async def deliver(self, channel, content=None, **kwargs):
            delivered.append((content, kwargs.get("reply_to")))
            return cp.SentChatMessage(channel, f"m{len(delivered)}", str(content))

        async def revise(self, sent, content, **kwargs):
            return None

    conv = cp.ChatConversation(_Platform(), "c9", uid=-9, is_direct=True,
                               is_command_chat=True)
    far_asker = cp.ChatMessage(
        author=cp.ChatUser(owner, "u1", "owner", is_owner=True), channel=conv,
        content="a cat", message_id=-4242, platform="stubplat",
        platform_message_id="p77")
    primary = _Chan(4444)
    ctx = submit_and_finish(far_asker, primary)
    assert ctx.get("trigger_message") is far_asker, "別的平台的發問沒有被存成觸發訊息"
    assert ("🖼️ 你要的圖來了。", "p77") in delivered, delivered
    assert primary.sent == [], "結果跑到既有平台的頻道去了"


# ---------------------------------------------------------------------------
# `/dorossi` 整族的擁有者閘——fail-closed 版本
#
# 主機控制那幾群走的是 `_OWNER_ONLY_GROUPS`（群為單位、自動涵蓋未來的子指令）。
# `/dorossi` **刻意不走那條路**：它的 handler 同時服務斜線與 `@bot` 兩個表面，所以
# 閘寫在 handler 內部，斜線包裝不再加一層（`test_pipeline_commands_are_not_locked`
# 也釘住了「`dorossi ask` 不在 `_is_owner_only_slash` 裡」這個決定）。
#
# 代價是：這一族的保護變成**逐個 handler 手寫的一行**，而且有三種寫法
# （`_dorossi_owner_only(message)`、`message.author.id != DOROSSI_USER_ID`、
# `!= OWNER_USER_ID`）。2026-09-05 盤點：32 個子指令全部都有閘、而且都在任何副作用
# 之前——但**沒有任何測試在看**。新增一個子指令時忘記寫那一行不會讓任何東西變紅。
#
# 這一族值得這道守門的理由比 `/host` 還強：`dorossi_cc_tools="full"` 會移除核准
# 關卡，等於在主機上無確認執行 shell 與讀寫檔案。
#
# 判定是**可達性**不是「有沒有出現」：閘必須是 top-level 的 `if`、body 裡要 return，
# 而且它前面只准有 docstring／`del`／`global` 這種沒有副作用的陳述式。把閘搬到
# 副作用後面、或改成不 return 的寫法，都會被抓到。
# ---------------------------------------------------------------------------

_DOROSSI_GATE_NAMES = {"_dorossi_owner_only", "DOROSSI_USER_ID", "OWNER_USER_ID"}


def _dorossi_slash_delegates():
    """{(群變數, 子指令): 委派的 handler 名稱} — 從指令樹抽，不手寫清單。"""
    import ast
    tree, _text = _bot_ast()
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "command"
                    and isinstance(dec.func.value, ast.Name)
                    and dec.func.value.id.startswith("dorossi")):
                continue
            sub = next((kw.value.value for kw in dec.keywords
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant)),
                       node.name)
            delegate = None
            for call in [n for n in ast.walk(node) if isinstance(n, ast.Call)]:
                for arg in call.args:
                    if isinstance(arg, ast.Name) and arg.id.startswith(
                            ("mcmd_", "cmd_")):
                        delegate = arg.id
                if isinstance(call.func, ast.Name) and call.func.id.startswith(
                        ("mcmd_", "cmd_")):
                    delegate = call.func.id
            out[(dec.func.value.id, sub)] = delegate
    return out


def _owner_gate_position(fn):
    """回 `(閘的索引, 閘前面的陳述式, 閘裡有沒有 return)`；沒有閘則索引為 None。"""
    import ast
    before = []
    for i, stmt in enumerate(fn.body):
        if isinstance(stmt, ast.If):
            used = {n.id for n in ast.walk(stmt.test) if isinstance(n, ast.Name)}
            if used & _DOROSSI_GATE_NAMES:
                has_return = any(isinstance(s, ast.Return) for s in ast.walk(stmt))
                return i, before, has_return
        before.append(stmt)
    return None, before, False


def _is_inert(stmt) -> bool:
    """這個陳述式擺在閘前面是安全的嗎（沒有副作用、不會先做事）。"""
    import ast
    if isinstance(stmt, (ast.Global, ast.Nonlocal, ast.Delete,
                         ast.Import, ast.ImportFrom, ast.Pass)):
        return True
    # 只有 docstring 算——任何其他 `Expr`（含 await）都不算。
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def test_every_dorossi_sub_command_reaches_an_owner_gate():
    """`/dorossi` 每一個子指令委派到的 handler 都要先過擁有者閘才做事。

    這是這一族唯一的保護（斜線層刻意不加閘），而且是逐個 handler 手寫的，所以
    漏掉一個不會有任何症狀——直到有人在別的伺服器叫得動它。
    """
    import ast
    tree, _text = _bot_ast()
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, node)

    delegates = _dorossi_slash_delegates()
    problems = []
    for (group, sub), delegate in sorted(delegates.items()):
        label = f"/{group.replace('_group', '').replace('dorossi_', 'dorossi ')} {sub}"
        if delegate is None or delegate not in funcs:
            problems.append(f"{label}：找不到它委派的 handler（{delegate!r}）")
            continue
        idx, before, has_return = _owner_gate_position(funcs[delegate])
        if idx is None:
            problems.append(f"{label} → `{delegate}` 完全沒有擁有者閘")
            continue
        if not has_return:
            problems.append(f"{label} → `{delegate}` 的閘沒有 return，擋不住任何人")
        noisy = [type(s).__name__ for s in before if not _is_inert(s)]
        if noisy:
            problems.append(
                f"{label} → `{delegate}` 的閘前面有會做事的陳述式 {noisy}，"
                "非擁有者已經觸發副作用了")
    assert not problems, "\n".join(problems)


def test_the_dorossi_gate_scan_actually_found_the_commands():
    """canary：抽取器壞掉就會回空 dict，上面那支會安靜地全過。"""
    delegates = _dorossi_slash_delegates()
    assert len(delegates) >= 25, (
        f"只抽到 {len(delegates)} 個 `/dorossi` 子指令，抽取邏輯壞了"
        f"（抽到的是 {sorted(delegates)}）")
    assert all(v for v in delegates.values()), (
        "有子指令抽不出委派對象："
        + repr(sorted(k for k, v in delegates.items() if not v)))


def test_the_three_spellings_of_the_dorossi_gate_are_one_person():
    """三種寫法必須指向同一個人，否則這一族的閘會安靜地分岔成兩套。

    `_dorossi_owner_only` 比的是 `DOROSSI_USER_ID`，另外幾支 handler 直接比
    `OWNER_USER_ID`。今天 `OWNER_USER_ID = DOROSSI_USER_ID`，所以三種寫法等價；
    哪天有人把 `OWNER_USER_ID` 改指到別人，`/dorossi` 就會變成一半一個人管。
    """
    assert b.OWNER_USER_ID == b.DOROSSI_USER_ID, (
        "`OWNER_USER_ID` 與 `DOROSSI_USER_ID` 不再是同一個人——"
        "`/dorossi` 的擁有者閘有三種寫法，這一刻起它們不再等價。")


def test_the_dorossi_gate_refuses_a_stranger():
    """行為版：閘本身要真的擋得住，不是只有形狀對。"""
    class _Author:
        def __init__(self, uid):
            self.id = uid

    class _Msg:
        def __init__(self, uid):
            self.author = _Author(uid)

    assert b._dorossi_owner_only(_Msg(b.DOROSSI_USER_ID)) is True
    assert b._dorossi_owner_only(_Msg(b.DOROSSI_USER_ID + 1)) is False
    assert b._dorossi_owner_only(_Msg(0)) is False



# ---------------------------------------------------------------------------
# 模組層不得留下「寫了沒人讀」的狀態
#
# 2026-09-05 掃出兩筆：`SINGLE_IMAGE_REQUEST_ID_LEN`（定義了、零引用）與
# `_scheduled_run_label`（被賦值四次、讀零次——標籤在解析當下就寫進磁碟的標籤檔，
# 排程到點時是重新讀那個檔）。
#
# 死常數只是雜訊，**寫了沒人讀的變數是陷阱**：它長得像一個有意義的狀態，下一個人
# 會讀它、以為它反映目前的排程，然後基於一個永遠沒有讀者、因此也從來沒被驗證過的
# 值做判斷。四個賦值點還會讓人以為「有人在維護它」。
#
# 判準刻意窄：只看**模組層**的賦值，而且只在「整個專案（含測試與文件字串）都沒有
# 以 Load 出現過」時才算。`__all__`、被 `getattr` 動態取用、只在 `.md` 裡被提到的，
# 都不會誤報。會誤報的情況（例如刻意先放著待用的常數）請寫進 `_ALLOWED_WRITE_ONLY`
# 並附理由——清單變長就是這條規則在鬆動的訊號。
# ---------------------------------------------------------------------------

_ALLOWED_WRITE_ONLY: dict[str, str] = {
    # 目前沒有例外。加一筆就要在這裡寫清楚「為什麼它沒有讀者卻該留著」。
}


def _module_level_assigned_names(tree):
    """模組層被賦值的名稱 → 行號（第一次出現）。"""
    import ast
    out = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        for name in targets:
            out.setdefault(name, node.lineno)
    return out


def test_the_bot_keeps_no_write_only_module_state():
    import ast as _ast
    tree, _text = _bot_ast()

    loaded = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name) and isinstance(node.ctx, _ast.Load):
            loaded.add(node.id)
        elif isinstance(node, _ast.Attribute):
            loaded.add(node.attr)
        elif isinstance(node, _ast.Constant) and isinstance(node.value, str):
            # 字串裡點名的（`getattr` / AST 測試的字面值）也算有讀者
            for token in node.value.replace("`", " ").replace("(", " ").split():
                loaded.add(token.strip(".,;:'\"()"))

    # 其他模組與測試也可能讀它（測試 2026-09-22 起住在 `test/`）
    package = Path(__file__).resolve().parent.parent / "axiomatic"
    tests = Path(__file__).resolve().parent
    for other in sorted(package.glob("*.py")) + sorted(tests.glob("*.py")):
        if other.name == "discord_bot.py":
            continue
        for node in _ast.walk(_ast.parse(other.read_text(encoding="utf-8"),
                                         str(other))):
            if isinstance(node, _ast.Name):
                loaded.add(node.id)
            elif isinstance(node, _ast.Attribute):
                loaded.add(node.attr)
            elif isinstance(node, _ast.Constant) and isinstance(node.value, str):
                for token in node.value.replace("`", " ").split():
                    loaded.add(token.strip(".,;:'\"()"))

    assigned = _module_level_assigned_names(tree)
    orphans = sorted(
        (name, lineno) for name, lineno in assigned.items()
        if name not in loaded
        and not name.startswith("__")
        and name not in _ALLOWED_WRITE_ONLY)
    assert not orphans, (
        "這些模組層名稱被賦值卻沒有任何地方讀它："
        + "、".join(f"`{n}`（第 {ln} 行）" for n, ln in orphans)
        + "。死常數是雜訊，寫了沒人讀的**變數**是陷阱——它長得像一個有意義的"
          "狀態，下一個人會基於一個從來沒被驗證過的值做判斷。刪掉它，或者"
          "接上真正的讀者。")


# ---------------------------------------------------------------------------
# 每日健康報告的「產量」那一行
#
# 這一行是報告裡**唯一**的產出型指標；其餘五行都是狀態，而狀態在「行程活著但
# 什麼都沒產出」的時候不會變色——那正是實測最貴的失效形態。所以這一組測試的
# 重點不是「有沒有這一行」，是「**產量真的掉到 0 的時候它會不會變**」。
# ---------------------------------------------------------------------------


def _fake_output_tree(root: Path, ages_hours: list[float], *,
                      now: float | None = None) -> Path:
    """造一棵假的產出樹，每張圖的 mtime 是「幾小時前」。回傳那個根目錄。"""
    if now is None:
        now = time.time()
    folder = root / "somebody (somewhere)"
    folder.mkdir(parents=True, exist_ok=True)
    for i, age in enumerate(ages_hours):
        path = folder / f"img_{i:04d}.png"
        path.write_bytes(b"x")
        stamp = now - age * 3600.0
        os.utime(path, (stamp, stamp))
    return root


def _isolate_health(monkeypatch, tmp_path) -> None:
    """把 `_health_quick_text` 除了產量以外的每一項都釘成固定值。

    兩個目的。一是讓兩次 render 的差異**只**可能來自產量，測試才證明得了「產量
    掉到 0 時這一行會變」而不是「有這一行」。二是跟正在跑的正式 bot 隔開——不讀
    真的 pid 檔、不讀真的 log、不讀真的事件檔。
    """
    monkeypatch.setattr(b, "_webrunner_alive", lambda: True)
    monkeypatch.setattr(b, "_free_disk_gb", lambda: 100.0)
    monkeypatch.setattr(b, "_recent_log_error_count", lambda *a, **k: 0)
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", tmp_path / "no-such.marker")
    monkeypatch.setattr(b, "EVENTS_FILE", tmp_path / "events.ndjson")
    monkeypatch.setattr(b, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(b, "_dorossi_waiters", {})
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_generate_queue", [])
    monkeypatch.setattr(b, "_generate_inflight", None)


def test_the_production_line_moves_when_output_actually_stops(monkeypatch, tmp_path):
    """停產時**那一行的內容要真的變**——不是「報告裡有那一行」就算數。

    兩個世界唯一的差別是「最近有沒有新圖」：背景程式照樣回報 running、磁碟一樣、
    log 一樣、佇列一樣。這正是 webrunner 死掉而監督者放棄、或佇列空了沒人發現時
    使用者會看到的畫面。
    """
    _isolate_health(monkeypatch, tmp_path)
    now = time.time()

    busy_root = _fake_output_tree(tmp_path / "busy", [0.5, 3.0, 9.0, 20.0], now=now)
    monkeypatch.setattr(b, "OUTPUT_ROOT", busy_root)
    busy = asyncio.run(b._health_quick_text())

    # 同一台機器、同樣的狀態，只是最近 24 小時一張都沒出來（舊圖還在，所以這不是
    # 「資料夾空了」那種一眼看得出來的情況）。
    stalled_root = _fake_output_tree(tmp_path / "stalled", [30.0, 48.0, 200.0], now=now)
    monkeypatch.setattr(b, "OUTPUT_ROOT", stalled_root)
    stalled = asyncio.run(b._health_quick_text())

    assert busy != stalled, (
        "產量掉到 0 的時候整份健康報告一個字都沒變——那就等於沒加這一行。")
    assert "- images produced (24h): `4`" in busy
    assert "- images produced (24h): `0`" in stalled

    # 其餘五行在這兩個世界裡**完全一樣**。這就是原本的報告偵測不到停產的原因，
    # 也是為什麼非加一行產出不可；順便保證上面那個 `busy != stalled` 是產量造成
    # 的，不是別的東西剛好在抖。
    other = lambda text: [ln for ln in text.splitlines()
                          if "images produced" not in ln]
    assert other(busy) == other(stalled)


def test_the_production_count_only_counts_the_recent_window(monkeypatch, tmp_path):
    """視窗邊界，外加「不是圖的檔案不算」。"""
    now = time.time()
    root = _fake_output_tree(tmp_path / "output", [1.0, 23.9, 24.1, 100.0], now=now)
    stray = root / "somebody (somewhere)" / "notes.txt"
    stray.write_bytes(b"x")
    os.utime(stray, (now, now))
    monkeypatch.setattr(b, "OUTPUT_ROOT", root)

    assert b._images_produced_since(now - b.HEALTH_PRODUCTION_WINDOW_SEC) == (2, True)
    # 視窗本身要比一段排程休息長，否則「休息」就可能把數字歸零，而歸零正是這一行
    # 的警報訊號——那會讓它每 22 小時假警報一次然後被讀的人自動略過。
    assert b.HEALTH_PRODUCTION_WINDOW_SEC > 6 * 3600


def test_a_failed_scan_reports_unknown_instead_of_zero(monkeypatch, tmp_path):
    """掃描失敗時**不可以**報 0——0 是警報訊號，把「讀不到」講成「沒產出」就是
    假警報。`os.walk` 預設會安靜地跳過讀不到的子資料夾，所以這條路真的存在。"""
    root = _fake_output_tree(tmp_path / "output", [1.0, 2.0], now=time.time())
    monkeypatch.setattr(b, "OUTPUT_ROOT", root)

    def _walk_that_cannot_read(top, onerror=None, **kwargs):
        if onerror is not None:
            onerror(OSError("unreadable"))
        return iter(())

    monkeypatch.setattr(os, "walk", _walk_that_cannot_read)
    count, scanned_ok = b._images_produced_since(time.time() - 86400)
    assert count == 0 and scanned_ok is False, (
        "掃描讀不到東西時第二個回傳值必須是 False，否則呼叫端分不出"
        "「真的沒產出」和「我沒查成」。")


def test_an_unknown_scan_renders_a_question_mark_not_a_zero(monkeypatch, tmp_path):
    """接續上一支：`scanned_ok=False` 必須真的走到畫面上，而不是被丟掉。"""
    _isolate_health(monkeypatch, tmp_path)
    monkeypatch.setattr(b, "_images_produced_since", lambda cutoff: (0, False))
    text = asyncio.run(b._health_quick_text())
    assert "- images produced (24h): `?`" in text
    assert "images produced (24h): `0`" not in text, (
        "掃描失敗被渲染成 0 ＝ 一個看起來像停產的假警報。")


def test_scheduled_rest_is_annotated_and_leaves_the_number_alone(monkeypatch, tmp_path):
    """休息中要照實報數字＋註明原因，**不做正規化或補償**。

    補償出來的是估計值，而這一行的全部價值就在於它取自實際產出。註記負責解釋
    「為什麼今天比較低」，這樣休息就不會被誤讀成停產（也就不會變成雜訊）。
    """
    _isolate_health(monkeypatch, tmp_path)
    now = time.time()
    root = _fake_output_tree(tmp_path / "output", [1.0, 2.0, 3.0], now=now)
    monkeypatch.setattr(b, "OUTPUT_ROOT", root)

    working = asyncio.run(b._health_quick_text())
    assert "- images produced (24h): `3`" in working
    assert "scheduled rest" not in working

    (tmp_path / "events.ndjson").write_text(
        '{"type": "schedule_rest", "wake_ts": ' + repr(now + 4 * 3600.0) + '}\n',
        encoding="utf-8")
    resting = asyncio.run(b._health_quick_text())
    assert "- images produced (24h): `3` (scheduled rest," in resting, (
        f"數字被休息改動或註記沒出現：{resting!r}")


def test_the_output_scan_does_not_run_on_the_event_loop(monkeypatch, tmp_path):
    """產出樹的掃描是同步 I/O，必須在工作執行緒上跑。

    `_health_quick_text` 跑在 `_daily_health_loop` 裡，而那條迴圈跟 gateway 心跳
    共用事件迴圈。判斷方式是在被呼叫的那一刻問「我這條執行緒上有沒有正在跑的事件
    迴圈」——有 ＝ 它是直接被 inline 呼叫的，那就是回歸。
    """
    _isolate_health(monkeypatch, tmp_path)
    seen: dict[str, bool] = {}

    def _spy(cutoff):
        try:
            asyncio.get_running_loop()
            seen["on_loop_thread"] = True
        except RuntimeError:
            seen["on_loop_thread"] = False
        return 7, True

    monkeypatch.setattr(b, "_images_produced_since", _spy)
    text = asyncio.run(b._health_quick_text())
    assert "`7`" in text, "spy 沒被呼叫到，這支測試等於沒測"
    assert seen["on_loop_thread"] is False, (
        "產出樹的掃描跑在事件迴圈上了——檔案數會隨時間長大，這是會卡住心跳的"
        "那一類同步 I/O。要走 `asyncio.to_thread`。")


def test_health_quick_scans_the_log_off_the_loop_and_says_when_it_could_not(
        monkeypatch, tmp_path):
    """健康報告的 log 計數：丟執行緒，而且讀不出來寫 `?` 不寫 0（2026-09-19）。

    原本走 `_read_log_lines()`，在事件迴圈上把整份 log（約 670 KB）讀進來，讀失敗
    時回 None 而計數留在 0——跟「沒有任何警告」長得一模一樣。這是跟產量那一行
    （`test_an_unknown_scan_renders_a_question_mark_not_a_zero`）同一條規則。
    三個方向：讀得到、還沒有 log、讀不出來。
    """
    _isolate_health(monkeypatch, tmp_path)
    seen: dict[str, set] = {}
    monkeypatch.setattr(b, "_recent_log_error_count", _spy("log", seen, 4))
    assert "- recent log warnings/errors: `4`" in asyncio.run(b._health_quick_text())
    assert seen == {"log": {"thread"}}, seen

    monkeypatch.setattr(b, "_recent_log_error_count", lambda *a, **k: None)
    assert "- recent log warnings/errors: `0`" in asyncio.run(b._health_quick_text())

    def _unreadable(*_a, **_k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(b, "_recent_log_error_count", _unreadable)
    text = asyncio.run(b._health_quick_text())
    assert "- recent log warnings/errors: `?`" in text, text
    assert "denied" not in text


def test_a_non_finite_cutoff_cannot_count_everything_or_nothing(monkeypatch, tmp_path):
    """`cutoff` 是拿去**比較**的，浮點特殊值會讓那道比較整組失效。

    沒有有限性檢查的話：`nan` 讓 `mtime >= cutoff` 恆為 False → 回 0 → 假警報；
    `-inf` 讓它恆為 True → 回總張數 → 真的停產被消音。兩個方向都錯，而且都不會
    丟例外。這是本 repo 第五次踩到同一類，所以直接在入口擋掉。
    """
    now = time.time()
    root = _fake_output_tree(tmp_path / "output", [1.0, 2.0, 500.0], now=now)
    monkeypatch.setattr(b, "OUTPUT_ROOT", root)
    assert b._images_produced_since(now - 86400) == (2, True)      # 對照組
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert b._images_produced_since(bad) == (0, False), bad


def test_a_non_finite_wake_ts_cannot_pin_the_bot_in_scheduled_rest(monkeypatch, tmp_path):
    """`wake_ts` 不是有限的數時不可以被當成「休息中」。

    `_scheduled_rest_until` 的守門「`wake_ts` 還在未來」是用比較寫的，而
    `json.loads` **預設就吃** `Infinity` / `NaN`，`1e400` 這種看起來完全正常的
    字面值 parse 出來也是 `inf`——三種都不必有人故意手打。修好之前三種都會回一個
    不是 `None` 的值，也就是**永久休息中**：`/rate` 的停滯警告被永遠消音、`/eta`
    永遠多算一段休息，而健康報告的產量那一行會永遠掛著「休息中」的解釋，把真正的
    停產也一起解釋掉。
    """
    now = 1_000_000.0
    events = tmp_path / "events.ndjson"
    monkeypatch.setattr(b, "EVENTS_FILE", events)
    for raw in ("Infinity", "-Infinity", "NaN", "1e400"):
        events.write_text('{"type": "schedule_rest", "wake_ts": ' + raw + '}\n',
                          encoding="utf-8")
        parsed = b._read_events_tail()
        assert parsed and not math.isfinite(parsed[0]["wake_ts"]), (
            f"這個測試的前提壞了：{raw} 應該要 parse 成非有限的浮點數")
        assert b._scheduled_rest_until(now=now) is None, (
            f"wake_ts={raw} 被當成「休息中」——那會永久消音真正的停產。")

    # 正常值兩個方向都要照舊。
    assert b._scheduled_rest_until(
        [{"type": "schedule_rest", "wake_ts": now + 10}], now=now) == now + 10
    assert b._scheduled_rest_until(
        [{"type": "schedule_rest", "wake_ts": now - 10}], now=now) is None


# ---------------------------------------------------------------------------
# `/web dict`：逾時不是「查無此字」
# ---------------------------------------------------------------------------
def _run_dict(monkeypatch, status, data):
    """跑一次 `mcmd_dict`，把外送換掉。回 (回覆內容, 那次呼叫用的 timeout)。"""
    seen = {}

    async def fake_get(url, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return status, data

    monkeypatch.setattr(b, "_http_get_json", fake_get)
    replies = []

    async def fake_reply(_message, content=None, **_kw):
        replies.append(content)

    monkeypatch.setattr(b, "safe_reply", fake_reply)
    asyncio.run(b.mcmd_dict(object(), "serendipity"))
    return (replies[0] if replies else None), seen.get("timeout")


def test_a_dictionary_timeout_does_not_claim_the_word_does_not_exist(
        monkeypatch):
    """`status == -1`（逾時／連不上）必須跟「查無此字」分開回答。

    合起來講的後果是**給出一個錯的答案**，而且使用者會相信它：2026-09-08 之前
    這個指令對**每一個字**都回 `not found`——因為端點穩定要 20 秒而預設逾時是
    15 秒，所以它從來沒有成功過。沒有答案只是沒用，錯的答案是有害的。
    """
    reply, _ = _run_dict(monkeypatch, -1, None)
    assert "逾時" in reply, reply
    assert "not found" not in reply, "逾時被講成查無此字"


def test_a_real_miss_still_says_not_found(monkeypatch):
    """反面：真的查不到要照舊講 not found，否則上面那支用「一律回逾時」也會綠。"""
    reply, _ = _run_dict(monkeypatch, 404, None)
    assert "not found" in reply, reply
    # 200 但空清單也算查不到。
    reply2, _ = _run_dict(monkeypatch, 200, [])
    assert "not found" in reply2, reply2


@pytest.mark.parametrize("status", [522, 500, 503, 429, 403])
def test_a_dictionary_outage_is_not_reported_as_a_missing_word(monkeypatch, status):
    """只有 404 可以說「查無此字」；其他非 200 都是「查不成」（2026-09-19）。

    當天實測字典服務的源站掛了：沒快取過的字等約 20 秒回 HTTP 522，於是指令對**大部分
    的字**回 `not found`。回覆必須是泛用句：不得說查無此字、不得點名服務（第 1 層）。
    """
    reply, _ = _run_dict(monkeypatch, status, None)
    assert reply == "字典服務暫時無法使用，請稍後再試。", (status, reply)
    assert "not found" not in reply and "serendipity" not in reply


def test_a_dictionary_answer_is_read_only_when_it_is_one(monkeypatch):
    """反面與邊界：404 照舊講查無此字、正常的 200 照舊給定義；200 但形狀讀不懂
    （不是清單）是「查不成」，不是「查無此字」。

    404 那一格是必要的：少了它，「一律回泛用句」也會讓上面那支全綠。
    """
    missing, _ = _run_dict(monkeypatch, 404, None)
    assert missing == "`serendipity` not found", missing
    found, _ = _run_dict(monkeypatch, 200, [{
        "word": "serendipity",
        "meanings": [{"partOfSpeech": "noun",
                      "definitions": [{"definition": "a happy accident"}]}]}])
    assert "a happy accident" in found and "_noun_" in found, found
    odd, _ = _run_dict(monkeypatch, 200, {"title": "unexpected"})
    assert odd == "字典服務暫時無法使用，請稍後再試。", odd


def test_the_dictionary_call_asks_for_more_time_than_the_default(monkeypatch):
    """這個呼叫必須明確指定一個**比預設長**的逾時，否則它又會全部失敗。

    這一支守的是「有人把那個看起來多餘的 `timeout=` 參數清掉」——清掉之後不會有
    任何測試變紅（外送本來就被換掉了），指令會安靜地退回每次都逾時。
    """
    import _external_apis as ex

    _, used = _run_dict(monkeypatch, 200, [{"word": "x", "meanings": []}])
    assert used is not None, "沒有指定 timeout，會沿用 15 秒的預設而每次逾時"
    assert used > ex._HTTP_TIMEOUT_SEC, (
        f"指定的 {used} 秒沒有比預設的 {ex._HTTP_TIMEOUT_SEC} 秒長；"
        "實測那個端點穩定要 20 秒左右。")


# ---------------------------------------------------------------------------
# 保留 handle 的背景 task：最外層必須自己接住例外
#
# 不打算 await 的 coroutine 走 `_schedule_coro`（掛 `_bg_task_done`，炸掉時印一行
# 帶名字的 stderr），上面那支 `test_no_background_task_is_created_without_keeping_
# a_reference` 守著它。但**保留 handle 的那幾個 `asyncio.create_task` 不在那條規則
# 的保護傘下**——它們沒有掛 `_bg_task_done`，所以只能靠 coroutine 自己在最外層接。
# 沒接的後果是「沒有人會取那個例外」：唯一的訊號是 asyncio 在 GC 時補的那句泛用
# 'Task exception was never retrieved'，功能已經死了而使用者不知道。
#
# ⚠️ 這兩支的偵測器都是 **recorder**（記下送出去的訊息），不是「有沒有拋例外」。
# 修好之後這兩個函式最外層都有 blanket `except`，例外會被吞掉，拿它當偵測器的
# 測試會永遠是綠的。
# ---------------------------------------------------------------------------
def _drive_scheduled_run(monkeypatch, outcome):
    """跑一次「排定時間已到」的排程 task，回傳頻道實際收到的訊息。

    `outcome` 是要塞給 `_do_webrunner_run` 的行為：例外實例＝啟動失敗。"""
    sent: list[str] = []

    class _Channel:
        async def send(self, content=None, **kwargs):
            sent.append(content)
            return None

    async def _fake_run(_channel):
        if isinstance(outcome, BaseException):
            raise outcome

    monkeypatch.setattr(b, "_do_webrunner_run", _fake_run)
    monkeypatch.setattr(b, "_get_batch_label", lambda: "")

    async def _body():
        task = asyncio.create_task(
            b._scheduled_run_loop(_Channel(), time.time() - 1))
        await asyncio.wait([task], timeout=5)
        # 例外在這裡取走只是為了不留下 'never retrieved' 雜訊；**斷言看的是
        # `sent`**，所以就算有人把 handler 拿掉，紅的也是下面的斷言而不是這裡。
        if task.done() and not task.cancelled():
            task.exception()
        return task.done()

    finished = asyncio.run(_body())
    return sent, finished


def test_a_scheduled_run_that_fails_to_start_says_so_in_the_channel(monkeypatch):
    """排定的批次啟動失敗時，頻道裡必須有一則訊息說它沒起來。

    這條路徑**依定義沒有人看著**（設好就走人），而 `_spawn_webrunner` 有未保護的
    raise 點（log 開檔被鎖住的 PermissionError、Popen 的 OSError、寫 pid 檔的
    OSError），`_do_webrunner_run` 走到 spawn 那一段時外面只有 `try/finally`。

    實測（2026-09-08，修掉之前）：頻道只收到「scheduled time reached — starting
    run」**一則**，然後什麼都沒有。所以症狀不是單純的沉默，是**一句確認之後的
    沉默**——比從頭到尾沒消息更容易被讀成「正在跑」。而 `finally` 已經把
    `_scheduled_run_ts` 清掉，`/run cancel` 也查不到。
    """
    sent, finished = _drive_scheduled_run(
        monkeypatch, PermissionError(22, "log file locked by another process"))

    assert finished, "排程 task 沒有結束，測試無法判定"
    # 正面對照：先確認真的走到了呼叫點（第一則「時間到了」有送出去），
    # 否則下面那個斷言可能只是因為整段根本沒執行。
    assert len(sent) >= 1, "連『排定時間到了』那一則都沒送出，測試沒走到該走的路"
    assert len(sent) >= 2, (
        "啟動失敗之後頻道沒有任何訊息——使用者收到『開始執行』然後就沒下文了。"
        f"實際送出：{sent!r}")

    failure = sent[-1]
    assert "沒有順利開始" in failure or "沒有開始" in failure, failure
    # DoD #3：不得教使用者打 `!cmd`。
    assert "!run" not in failure, failure
    # Secrecy Layer 1：原始例外文字、主機路徑一律不得外送。
    assert "PermissionError" not in failure, failure
    assert "log file locked" not in failure, failure


def test_a_scheduled_run_that_starts_cleanly_posts_no_failure_notice(monkeypatch):
    """反方向：正常啟動時不得有失敗訊息。

    少了這一支，「無論如何都補一則警告」也會讓上面那支變綠——而那會在每一次
    正常的排程啟動後面多貼一則假警報。
    """
    sent, finished = _drive_scheduled_run(monkeypatch, None)

    assert finished
    assert len(sent) == 1, f"正常啟動卻多送了訊息：{sent!r}"
    assert "沒有順利開始" not in sent[0], sent[0]


def _drive_watch(monkeypatch, condition, author_id):
    """跑一次 `_watch_loop`，回傳 `safe_reply` 收到的訊息與殘留的 `_WATCHES`。"""
    replies: list[str] = []

    async def _rec(_message, content=None, **kwargs):
        replies.append(content)
        return None

    async def _condition(_kind, _target, _state=None):
        if isinstance(condition, BaseException):
            raise condition
        return condition

    monkeypatch.setattr(b, "safe_reply", _rec)
    monkeypatch.setattr(b, "_watch_condition_met", _condition)
    monkeypatch.setattr(b, "_WATCH_POLL", {"text": 0.0})
    monkeypatch.setattr(b, "_WATCHES", {})

    author = types.SimpleNamespace(id=author_id, mention=f"<@{author_id}>")
    message = types.SimpleNamespace(author=author)
    b._WATCHES[77] = {"label": "t", "started": time.monotonic(),
                      "task": None}

    async def _body():
        task = asyncio.create_task(
            b._watch_loop(77, "text", "target", message, "測試監看"))
        await asyncio.wait([task], timeout=5)
        if task.done() and not task.cancelled():
            task.exception()
        return task.done()

    finished = asyncio.run(_body())
    return replies, finished, dict(b._WATCHES)


def test_a_crashed_watch_tells_the_user_instead_of_only_stderr(monkeypatch):
    """監看崩掉時使用者必須收得到訊息。

    `cmd_watch` 對使用者的承諾是「成立時會 @ 你」。崩掉時 `finally` 會把它從
    `_WATCHES` 移掉，所以 `/watch list` 也查不到——只印 stderr 等於讓人永遠等一個
    不會來的通知，而且完全查不出為什麼。
    """
    replies, finished, left = _drive_watch(
        monkeypatch, RuntimeError("screen probe exploded"), b.OWNER_USER_ID)

    assert finished, "監看 task 沒有結束，測試無法判定"
    assert replies, "監看崩掉時使用者一則訊息都沒收到"
    assert "77" in replies[-1], replies[-1]
    # 崩掉之後真的會從 `/watch list` 消失——這正是「只印 stderr 不夠」的理由。
    assert 77 not in left


def test_a_crashed_watch_hides_the_raw_reason_from_a_non_owner(monkeypatch):
    """Secrecy Layer 1：原始例外文字只有擁有者拿得到，其他人拿泛用句。

    `watch` 目前在 `_OWNER_ONLY_GROUPS` 裡，所以實務上發起人就是擁有者；這一支
    釘的是**萬一那道閘日後放寬**時仍然 fail-closed（走 `_owner_detail` 而不是在
    這裡自己寫一次 `== OWNER_USER_ID` 的理由）。
    """
    owner_replies, _, _ = _drive_watch(
        monkeypatch, RuntimeError("screen probe exploded"), b.OWNER_USER_ID)
    other_replies, _, _ = _drive_watch(
        monkeypatch, RuntimeError("screen probe exploded"),
        b.OWNER_USER_ID + 1)

    assert "screen probe exploded" in owner_replies[-1], owner_replies[-1]
    assert "screen probe exploded" not in other_replies[-1], other_replies[-1]
    assert "查看 log" in other_replies[-1], other_replies[-1]


def test_a_stopped_watch_neither_checks_nor_fires(monkeypatch):
    """`/watch stop` 只把監看從 `_WATCHES` 拿掉，不取消 task——停下來靠的是迴圈每次醒來
    先看自己還在不在。少了那一句，被停掉的監看照樣在條件成立時 @ 人，還會跑它掛著的
    動作（按鍵、點選、巨集），也就是擁有者剛剛叫它不要做的事。

    條件刻意設成「一問就成立」，動作與條件判定都是記錄用的絆線：這支測試失敗時什麼都
    不會真的發生。"""
    replies: list = []
    checked: list = []
    ran: list = []

    async def _rec(_message, content=None, **_kwargs):
        replies.append(content)

    async def _condition(*args):
        checked.append(args)
        return True

    async def _action(*args):
        ran.append(args)

    monkeypatch.setattr(b, "safe_reply", _rec)
    monkeypatch.setattr(b, "_watch_condition_met", _condition)
    monkeypatch.setattr(b, "_watch_run_action", _action)
    monkeypatch.setattr(b, "_WATCH_POLL", {"text": 0.0})
    monkeypatch.setattr(b, "_WATCHES", {})       # 77 已經被 `/watch stop` 拿掉
    author = types.SimpleNamespace(id=b.OWNER_USER_ID, mention="<@1>")
    message = types.SimpleNamespace(author=author)
    asyncio.run(asyncio.wait_for(
        b._watch_loop(77, "text", "target", message, "測試監看",
                      action={"kind": "key", "target": "enter"}), 5))
    assert checked == [] and ran == [] and replies == []

    # 對照組：同一組替身，監看還在時要成立、要跑動作——否則上面三個空清單證明不了什麼。
    b._WATCHES[77] = {"label": "t", "started": time.monotonic(), "task": None}
    asyncio.run(asyncio.wait_for(
        b._watch_loop(77, "text", "target", message, "測試監看",
                      action={"kind": "key", "target": "enter"}), 5))
    assert len(checked) == 1 and len(ran) == 1 and "🔔" in replies[0]


def test_a_watch_that_fires_normally_posts_no_crash_notice(monkeypatch):
    """反方向：條件正常成立時不得出現「異常結束」。

    少了這一支，「不管怎樣都補一則崩潰訊息」也會讓上面兩支變綠。
    """
    replies, finished, _ = _drive_watch(monkeypatch, True, b.OWNER_USER_ID)

    assert finished
    assert replies, "條件成立卻沒有通知"
    assert all("異常結束" not in (r or "") for r in replies), replies
    assert "🔔" in replies[0], replies[0]


def test_the_base_root_derivation_is_not_a_list_that_matches_today():
    """釘住「根目錄名字是**推**出來的」，不是「今天的答案剛好對」。

    餵一個合成模組，裡面的目錄常數叫一個**任何手寫清單都不可能有**的名字
    （§8.8(A3)：真實資料乾淨時，寫死的範圍和算出來的範圍在輸出上一模一樣）。
    順便釘住負面方向——檔案常數不該被當成目錄，否則清單會膨脹到沒有意義。
    """
    import ast as _ast

    tree = _ast.parse(
        "from pathlib import Path\n"
        "MEDIA_DIR = Path('/tmp') / 'media'\n"
        "NESTED_DIR = MEDIA_DIR / 'sub'\n"
        "HERE = Path(__file__).resolve()\n"
        "PARENT_DIR = HERE.parent\n"
        "SOME_FILE = MEDIA_DIR / 'notes.md'\n"
        "lowercase_dir = Path('/tmp')\n"
        "def show(name):\n"
        "    return MEDIA_DIR / name\n")
    roots = _module_dir_constants(tree)
    assert "MEDIA_DIR" in roots, (
        f"推導在一個全新模組上回 {sorted(roots)}——它退回寫死的名字清單了。")
    assert {"NESTED_DIR", "HERE", "PARENT_DIR"} <= roots, (
        f"推導漏了 `X / 'literal'` / `Path(...)` / `.parent` 其中一種：{sorted(roots)}")
    assert "SOME_FILE" not in roots, "帶副檔名的檔案常數被當成目錄了"
    assert "lowercase_dir" not in roots, "小寫名字不是模組層常數的形狀"

    hits = _unguarded_derived_joins(tree)
    assert [row[2] for row in hits] == ["MEDIA_DIR / name"], (
        f"推導出根名字之後，接合卻沒被抓到：{hits}")


def test_the_base_root_floor_fires_when_the_derivation_comes_back_empty(
        monkeypatch):
    """下限的對照組。

    真實模組本來就推得出五個根，所以 `>= 4` 是量不出來的——把它放寬成 0，上面那
    支照樣全綠。這裡把前提直接打壞，並斷言**是哪一句在叫**（§8.8(A4)：控制測試
    只證得了它真的執行到的那一行，而這支測試裡下限之後還有別的斷言）。
    """
    monkeypatch.setattr(sys.modules[__name__], "_module_dir_constants",
                        lambda tree: set())
    with pytest.raises(AssertionError) as excinfo:
        test_every_join_onto_a_base_root_is_guarded_per_join()
    assert "推導壞了" in str(excinfo.value), (
        f"紅的不是下限那一句，而是：{excinfo.value}")


def test_the_base_roots_contain_no_name_that_does_not_exist():
    """對帳：推出來的每個名字都必須真的在 `discord_bot.py` 裡被定義。

    這一支存在的理由是它抓到過東西：2026-09-10 之前那份手寫清單裡的 `DEBUG_DIR`
    **全專案沒有任何符號叫這個名字**——只有一句 docstring 跟那一行提到它。它從來
    沒有命中過任何東西，而當時的下限 `>= 3` 還把它算進去，所以連下限都沒發現。
    """
    import ast as _ast

    source = Path(b.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(source)
    roots = _module_dir_constants(tree)
    # ⚠️ **用這個檔案裡既有的 `_module_level_assigned_names`，不要在這裡手寫一份
    # 只讀 `ast.Assign` 的窄版。** 那支（給 write-only 狀態那支測試用的，隔了
    # 五百行）本來就同時讀 `Assign` 與 `AnnAssign`，而窄版會讓這支對帳**倒過來叫
    # 錯人**：`_module_dir_constants` 從 2026-09-10 起把 `X: Path = …` 也當成根，
    # 於是下一個人在 `discord_bot.py` 寫出 `SNAPSHOT_DIR: Path = PROJECT_ROOT /
    # "snap"` 的那一刻，一個**完全正確**的常數會被報成「對不到符號的幽靈」。
    # （`discord_bot.py` 模組層已經有 44 個 `AnnAssign`，只是今天沒有一個註記成
    # `Path`，所以窄版今天也是綠的——實測驗證過那個誤報。）
    assigned = set(_module_level_assigned_names(tree))
    ghosts = sorted(roots - assigned - _EXTRA_BASE_ROOTS)
    assert not ghosts, (
        f"這些 base 目錄名在 `discord_bot.py` 裡沒有對應的模組層指派：{ghosts}。"
        "一個對不到符號的名字永遠不會命中任何接合，等於那條保護不存在。")
    assert "DEBUG_DIR" not in roots, (
        "`DEBUG_DIR` 又回來了——全專案沒有這個符號，它是 2026-09-10 清掉的死條目。")

    # 合成對照：真實原始碼今天沒有任何 `X: Path = …` 的根，所以
    # `_module_level_assigned_names` 的 `AnnAssign` 那一半在真實資料上是量不出來
    # 的——整段刪掉照樣全綠，然後誤報會在很久以後、由一個完全無關的改動觸發。
    # 對照組**必須呼叫真正在用的那支函式**，不可以在這裡抄一份同樣的邏輯：抄本
    # 會讓對照組驗到抄本（實測——抄本版本的變異存活了）。
    synthetic = _ast.parse(
        "from pathlib import Path\n"
        "PROJECT_ROOT = Path('/tmp')\n"
        "SNAPSHOT_DIR: Path = PROJECT_ROOT / 'snap'\n")
    syn_roots = _module_dir_constants(synthetic)
    syn_assigned = set(_module_level_assigned_names(synthetic))
    assert "SNAPSHOT_DIR" in syn_roots, (
        "推導看不到 `X: Path = …` 了——那是 `_SHELL_CWD` 一直隱形的原因。")
    assert not (syn_roots - syn_assigned), (
        f"`X: Path = …` 被推成根、卻對不到指派：{sorted(syn_roots - syn_assigned)}。"
        "對帳只讀 `ast.Assign` 的話就是這個結果——一個完全正確的常數被報成幽靈，"
        "而會亂叫的守門就是會被關掉的守門。")


@pytest.mark.parametrize("source, label", [
    ("import webrunner_novelai", "裸 import"),
    ("import axiomatic.webrunner_novelai", "套件限定 import"),
    ("from axiomatic.webrunner_novelai import pair_todos",
     "from 套件.模組 import 名字"),
    ("from axiomatic import webrunner_novelai", "from 套件 import 模組"),
    ("from axiomatic import webrunner_novelai as wr", "同上，加 as"),
    ("import webrunner_novelai as wr", "裸 import 加 as"),
])
def test_the_import_extractor_sees_every_way_to_reach_a_module(source, label,
                                                               tmp_path):
    """守門的 canary：**每一種**寫得出來的 import 形式都要抽得到那個模組名。

    這一支存在的理由是它抓到過東西：2026-09-10 之前
    `from axiomatic import webrunner_novelai` 只會抽到 `"axiomatic"`，於是
    `CLAUDE.md` 的核心架構不變量（bot 不得 import webrunner）、循環相依那道、
    以及 `legacy/` 那道**三條一起**對這個寫法是瞎的。而它不是假想的寫法：
    `start_webrunner.py` 本來就是這樣 import `_chrome_slot` 的。

    真實 repo 上沒有任何違規，所以主守門永遠是綠的——牙齒只能在這裡。
    """
    probe = tmp_path / "probe.py"
    probe.write_text(source + "\n", encoding="utf-8")
    seen = _module_imports(probe)
    assert "webrunner_novelai" in seen, (
        f"「{label}」這種寫法抽不到 `webrunner_novelai`（抽到 {sorted(seen)}）"
        "——這個形式對模組邊界守門是隱形的。")


def test_the_import_extractor_does_not_invent_module_names(tmp_path):
    """反方向：沒有 import 到的東西不得出現，否則守門會開始亂叫。

    一個會誤報的守門最後會被人關掉，而被關掉的守門等於不存在——本專案已經為這條
    寫過好幾次（`test_language` / `test_text_encoding` 的判準都刻意收窄）。
    """
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "# import webrunner_novelai  ← 註解不算\n"
        "TEXT = 'from axiomatic import discord_bot'  # 字串也不算\n",
        encoding="utf-8")
    seen = _module_imports(probe)
    assert "webrunner_novelai" not in seen, "註解被當成 import 了"
    assert "discord_bot" not in seen, "字串字面值被當成 import 了"
    # `Path` 是類別不是模組，收窄之後**刻意**不再出現——這正是新判準要的行為。
    # 這一行本來寫成期待它在裡面，是照著加寬版寫的；收窄之後它就成了錯的期待。
    assert {"json", "pathlib"} <= seen, f"正常的 import 沒抽到：{sorted(seen)}"
    assert "Path" not in seen, "類別名被當成模組了"

    # 名字長得像模組、其實是函式的，不得被誤判——這是加寬時真的誤報過的那一筆。
    probe.write_text(
        "from _supervisor import webrunner_exit_needs_human\n", encoding="utf-8")
    got = _module_imports(probe)
    assert "webrunner_exit_needs_human" not in got, (
        f"函式名被當成模組了：{sorted(got)}。`_supervisor` 是合法的被動共用模組，"
        "從它 import 一個判 rc 的函式不是跨越 bot↔webrunner 邊界。")
    assert "_supervisor" in got, "真正被 import 的模組反而漏了"

    # repo root 的腳本也是 live stack 的一部分——`_names_a_module` 會同時看套件
    # 目錄與 repo root，而那第二半沒有這一行就沒有任何測試在釘（變異實測存活過）。
    probe.write_text("from axiomatic import run_batch\n", encoding="utf-8")
    root_side = _module_imports(probe)
    assert "run_batch" in root_side, (
        f"repo root 的模組認不出來：{sorted(root_side)}。`run_batch.py` /"
        "`start_*.py` 一樣是 live stack，只是不住在套件目錄裡。")


# ---------------------------------------------------------------------------
# 單張伺服器模式：spawn 時**宣告**，不是從磁碟推論
# ---------------------------------------------------------------------------
# 2026-06-27 的實際事故：一次帶著 57 組佇列配對的 `/run`，因為磁碟上剛好躺著一筆
# 單圖請求，在走進批次迴圈**之前**就把自己當成單張伺服器，服務兩張就閒置收工
# rc=0。兩支監督者都把 rc=0 讀成「乾淨跑完」，`todo_done` 沒發出去，所以頻道連
# 🏁/🛑 都沒有——整個無人值守批次安靜地停掉。修法是把模式改成 spawn 時宣告：bot
# 在 argv 帶旗標，背景程式讀 argv，磁碟上的請求檔只回答「下一個要服務什麼」。
#
# 這一組守的是 bot 這一側。**反方向才是更貴的那個**：無條件帶旗標會讓 `/run` 每
# 次都變成單張伺服器——一樣安靜，而且整條佇列從此一張都不產。所以下面同時釘住
# 「該帶的有帶」與「不該帶的一個都沒帶」。
_ONESHOT_SPAWN_CALLER = "_spawn_oneshot_webrunner"


def _spawn_webrunner_call_sites() -> list[tuple[str, str | None]]:
    """`[(呼叫它的函式名, single_image_server 引數的原始碼或 None)]`。

    用遞迴下降而不是 `ast.walk(函式節點)`：後者會把巢狀函式裡的呼叫同時算到外層
    函式頭上，於是一個呼叫變成兩筆、歸屬還是錯的。
    """
    import ast
    tree, _text = _bot_ast()
    sites: list[tuple[str, str | None]] = []

    def visit(node, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "_spawn_webrunner"):
                flag = None
                for keyword in child.keywords:
                    if keyword.arg == "single_image_server":
                        flag = ast.unparse(keyword.value)
                sites.append((owner, flag))
            visit(child, owner)

    visit(tree, "<module>")
    return sites


def test_only_the_one_shot_spawn_declares_single_image_server_mode():
    """旗標只准出現在 one-shot 那一條路上，而且那一條非帶不可。"""
    sites = _spawn_webrunner_call_sites()
    # 正面對照：抽不到東西的擷取器，輸出跟「全部都對」一模一樣。實測是 4 個呼叫
    # 點（one-shot 一個、備援一個、監督者重生一個、`/run` 一個），下限刻意設在
    # 真實數字上而不是 0。
    assert len(sites) >= 4, (
        f"只找到 {len(sites)} 個 `_spawn_webrunner` 呼叫點：{sites}。"
        "擷取器多半壞了——一個什麼都比對不到的擷取器會安靜地通過下面每一條。")

    declared = sorted(owner for owner, flag in sites
                      if flag not in (None, "False"))
    assert declared == [_ONESHOT_SPAWN_CALLER], (
        f"帶著單張伺服器旗標 spawn 的是 {declared}，應該只有 "
        f"`{_ONESHOT_SPAWN_CALLER}`。多一個就是把 `/run` 的批次變成單張伺服器："
        "服務完 rc=0 收工、監督者讀成乾淨跑完、`todo_done` 不發，整條佇列一張都"
        "不產而且沒有任何錯誤訊息。")

    oneshot = [flag for owner, flag in sites if owner == _ONESHOT_SPAWN_CALLER]
    assert oneshot == ["True"], (
        f"`{_ONESHOT_SPAWN_CALLER}` 傳的是 {oneshot}，應該是 `True`。少了它，"
        "單張伺服器會被 spawn 成一個批次，去跑真正的佇列。")


def test_the_argv_flag_is_single_sourced_not_copied():
    """旗標的字面值只有一份（住在共用模組），bot 這邊用 import 取得。

    抄第二份的漂移是無聲的：改名之後 spawn 照樣成功，只是那個行程不知道自己是單
    張伺服器，於是跑起整條批次佇列。這個 repo 的既有教訓是「能推導就不要對帳」，
    而 import 是推導、抄一份字面值只能靠對帳。
    """
    import ast
    tree, _text = _bot_ast()
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "_webrunner_shared"
        and any(alias.name == "SINGLE_IMAGE_SERVER_FLAG"
                for alias in node.names)
        for node in ast.walk(tree))
    assert imported, (
        "`discord_bot` 沒有從 `_webrunner_shared` import "
        "`SINGLE_IMAGE_SERVER_FLAG` 了——旗標變成兩份各自維護的字面值。")

    copies = [node for node in ast.walk(tree)
              if isinstance(node, ast.Constant)
              and node.value == ws.SINGLE_IMAGE_SERVER_FLAG]
    assert not copies, (
        f"`discord_bot` 裡出現了 {len(copies)} 份旗標字面值 "
        f"（行號 {[n.lineno for n in copies]}）。單一來源在 `_webrunner_shared`。")
    assert b.SINGLE_IMAGE_SERVER_FLAG == ws.SINGLE_IMAGE_SERVER_FLAG


def test_the_declared_mode_reaches_the_child_process_argv(monkeypatch, tmp_path):
    """真的走一次 `_spawn_webrunner`，看那個旗標有沒有進到子行程的 argv。

    上面那支 AST 守門只看得到「呼叫端傳了什麼關鍵字」，看不到那個關鍵字有沒有真
    的翻譯成 argv；這一支補的就是那一段。同時釘住 `_webrunner_oneshot` 跟 argv 來
    自同一個參數——那兩件事一旦能各自指派，就能無聲地對不起來。

    ⚠️ 這條路徑上**每一個**檔案常數都是活的狀態，一個都不能漏掉：`WEBRUNNER_LOG`
    會被以 `"w"` 開啟（正在跑的那個批次的 log 會被截斷）、四個 ndjson 會被輪替
    整檔改寫、pid 檔會被覆寫。所以下面把它們全部導去 `tmp_path`，並把
    `subprocess.Popen` 換成錄音機——不然這支測試會去動正式作業。
    """
    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

    recorded: list[list[str]] = []

    def _fake_popen(argv, **_kwargs):
        recorded.append(list(argv))
        return _FakeProc()

    for name in ("WEBRUNNER_LOG", "WEBRUNNER_LOG_PREV", "EVENTS_FILE",
                 "AUDIT_FILE", "GENERATE_HISTORY_FILE", "DOROSSI_EVENTS_FILE"):
        monkeypatch.setattr(b, name, tmp_path / getattr(b, name).name)
    monkeypatch.setattr(b, "_save_pid", lambda *a, **k: None)
    monkeypatch.setattr(b.subprocess, "Popen", _fake_popen)
    for name, value in (("_webrunner_stop_requested", False),
                        ("_webrunner_log_handle", None),
                        ("_webrunner_proc", None),
                        ("_webrunner_pid", None),
                        ("_webrunner_variant", None),
                        ("_webrunner_spawned_mono", 0.0),
                        ("_webrunner_oneshot", False)):
        monkeypatch.setattr(b, name, value)

    try:
        ok, _status = b._spawn_webrunner("je", single_image_server=True)
        assert ok and len(recorded) == 1, f"spawn 沒成功：{recorded}"
        assert ws.SINGLE_IMAGE_SERVER_FLAG in recorded[0], (
            f"單張伺服器的 argv 裡沒有旗標：{recorded[0]}。那個行程會去跑整條"
            "批次佇列，而使用者只要了一張圖。")
        assert b._webrunner_oneshot is True, (
            "argv 說是單張伺服器，`_webrunner_oneshot` 卻說不是——`/run` / `/stop`"
            " 與 reaper 都靠它判斷要不要走 one-shot 收尾路徑。")

        ok, _status = b._spawn_webrunner("je")
        assert ok and len(recorded) == 2, f"spawn 沒成功：{recorded}"
        assert ws.SINGLE_IMAGE_SERVER_FLAG not in recorded[1], (
            f"批次的 argv 裡混進了旗標：{recorded[1]}。這是更貴的那個方向——"
            "`/run` 會每次都變成單張伺服器，整條佇列從此不產圖。")
        assert b._webrunner_oneshot is False, (
            "批次 spawn 之後 `_webrunner_oneshot` 仍是 True。")
    finally:
        handle = b._webrunner_log_handle
        if handle is not None and not handle.closed:
            handle.close()


def test_the_spawned_webrunner_is_told_to_speak_utf8(monkeypatch, tmp_path):
    """`_spawn_webrunner` 開的是 Python 子行程，所以**編碼端**也要設。

    `open(WEBRUNNER_LOG, "w", encoding="utf-8")` 只管 **bot 自己**往那個 handle
    寫的幾行。子行程拿到的是 **fd**，照自己的 `sys.stdout.encoding` 編碼，而
    stdout 接到**檔案**（不是主控台）時 CPython 用系統地區編碼——本機 cp950。
    結果是一份**混合編碼**的 `webrunner.log`，而讀它的三個地方都是
    `errors="replace"`，所以繁中進度行只會變成一串 U+FFFD 送進聊天平台：沒有
    例外、沒有紅字、沒有任何東西會變紅。

    實測（乾淨環境，2026-09-12）：兩行繁中 → **27 個 U+FFFD**，strict UTF-8
    解碼失敗在第 54 個位元組（正好是子行程輸出的起點）；補上 env 之後 0 個。

    ⚠️ `test_text_encoding.test_a_python_child_is_told_to_speak_utf8` **結構上
    掃不到這一站**：它靠 `call.args[0]` 判斷「開的是不是 Python」，而這裡 argv
    是變數（`argv = [sys.executable, ...]` 再 `Popen(argv, ...)`）。所以要具名
    守，跟 `_supervisor.stream_child` 與 `verify_browser.run_full` 同理。

    ⚠️ **兩半都要量。** 只量「環境裡沒有」那一半的話，`setdefault`（讓步）與
    覆寫的行為一模一樣，把強制改成讓步的變異會活下來——而讓步的方向剛好是安靜
    壞掉的那一邊：呼叫端環境帶著 `PYTHONIOENCODING=cp950` 時子行程照那個編、
    我們照 utf-8 解。開發者的殼本身就 export 了這個變數（本機是
    `utf-8:surrogateescape`），所以第一半也必須先把它刪掉再量，否則永遠綠。
    """
    class _FakeProc:
        pid = 4243

        def poll(self):
            return None

    recorded: list[dict] = []

    def _fake_popen(argv, **kwargs):
        recorded.append(dict(kwargs))
        return _FakeProc()

    for name in ("WEBRUNNER_LOG", "WEBRUNNER_LOG_PREV", "EVENTS_FILE",
                 "AUDIT_FILE", "GENERATE_HISTORY_FILE", "DOROSSI_EVENTS_FILE"):
        monkeypatch.setattr(b, name, tmp_path / getattr(b, name).name)
    monkeypatch.setattr(b, "_save_pid", lambda *a, **k: None)
    monkeypatch.setattr(b.subprocess, "Popen", _fake_popen)
    for name, value in (("_webrunner_stop_requested", False),
                        ("_webrunner_log_handle", None),
                        ("_webrunner_proc", None),
                        ("_webrunner_pid", None),
                        ("_webrunner_variant", None),
                        ("_webrunner_spawned_mono", 0.0),
                        ("_webrunner_oneshot", False)):
        monkeypatch.setattr(b, name, value)

    try:
        # 第一半：環境裡**沒有**那個變數（fresh clone / 服務帳號的常態）。
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
        ok, _status = b._spawn_webrunner("je")
        assert ok and len(recorded) == 1, f"spawn 沒成功：{recorded}"
        env = recorded[0].get("env")
        assert env is not None, (
            "`_spawn_webrunner` 的 `Popen` 沒有帶 `env=`。父行程的 "
            "`encoding=` 管不到子行程怎麼編碼，少了它 `webrunner.log` 會是"
            "混合編碼，而讀它的每一處都是 `errors=\"replace\"`——不會拋例外，"
            "只會把中文吃成 U+FFFD。")
        assert env.get("PYTHONIOENCODING") == "utf-8", (
            "子行程沒有被告知用 UTF-8 寫 stdout："
            f"PYTHONIOENCODING={env.get('PYTHONIOENCODING')!r}。")

        # 第二半：環境裡帶著一個**錯的**值。`setdefault` 會讓步、覆寫不會。
        recorded.clear()
        monkeypatch.setenv("PYTHONIOENCODING", "cp950")
        for name, value in (("_webrunner_log_handle", None),
                            ("_webrunner_proc", None)):
            monkeypatch.setattr(b, name, value)
        ok, _status = b._spawn_webrunner("je")
        assert ok and len(recorded) == 1, f"spawn 沒成功：{recorded}"
        env = recorded[0].get("env")
        assert env is not None and env.get("PYTHONIOENCODING") == "utf-8", (
            "呼叫端環境帶著 `PYTHONIOENCODING=cp950` 時子行程跟著用了 cp950，"
            f"實際傳下去的是 {(env or {}).get('PYTHONIOENCODING')!r}。這是"
            "`setdefault`（讓步）而不是覆寫——解碼端是寫死的 utf-8，讓步等於"
            "安靜地寫出 mojibake。")
    finally:
        handle = b._webrunner_log_handle
        if handle is not None and not handle.closed:
            handle.close()


# ---------------------------------------------------------------------------
# 載入端要逐筆過濾：四個磁碟儲存裡，只有 `schedule.json` 停在容器那一層
# ---------------------------------------------------------------------------
# bot 有三個「讀一份 JSON、交出一堆條目」的載入器。`_load_favorites` 與
# `_load_image_msgs` 都逐筆檢查型別（後者連路徑根都檢查，docstring 還寫著
# 「唯一的寫入者是我們自己的程式……那是**寫入端**的性質，不是讀取端宣告出來的」）；
# `_load_schedules` 只驗容器——`isinstance(data, dict)` 且 `entries` 是 list——
# 然後就把裡面的東西原樣交出去。
#
# 後果實測過，而且比「丟一個例外」難看得多：`schedule.json` 的 `entries` 裡混進
# 一筆不是 dict 的東西（手改、或將來的格式變更），`_schedule_loop` 的第一句
# `entry.get("id", 0)` 就 `AttributeError`，被它自己的 broad except 接住、印一行
# 到 stderr——然後**那一輪在這裡就結束了**。於是：
#
# * 排在壞條目後面的每一筆排程都不會被檢查，一次都不會；
# * `data` 沒有存回去，所以什麼狀態都不會前進；
# * 下一輪再來一次，永遠。
#
# 而使用者面那一側同樣塌掉，但塌得沒有一句話說得清楚：`cmd_schedule` 的
# `list` / `remove` / `run` 三支都會在 `entry.get(...)` 上丟 `AttributeError`
# （實測），於是全部變成「指令發生內部錯誤（AttributeError），請查看 log。」。
# **只有 `add` 還能用**——所以使用者可以一直新增永遠不會執行的排程，卻列不出來、
# 也刪不掉那一筆害到大家的條目。唯一的修法是回到那台機器上手動編檔，而這整組
# 指令存在的理由正是「下指令的人不在電腦前面」。
#
# 這個意圖其實早就寫下來了——上面 `test_schedule_due_weekly_and_once` 裡那一行
# 註解就是「a corrupt entry must not take the whole tick down with it」。它守的是
# 壞掉的**欄位**（`when_value: "oops"`），而壞掉的**條目**在 `_schedule_due` 被呼叫
# 之前就已經把整輪打斷了。規則對，只是攔在下游一層。


_DISK_LOADERS = {
    "_load_favorites": "FAVORITES_FILE",
    "_load_image_msgs": "RECENT_IMAGE_MSGS_FILE",
    "_load_schedules": "SCHEDULE_FILE",
}

# 刻意不算在內的，每一筆都要寫理由（不然下次有人會「順手」把它加進去）。
_NOT_A_CONTAINER = {
    "_load_pid": "回的是 `(pid, 判得出來嗎)` 兩元組，不是一堆條目；"
                 "它自己的三分法由 `test_pid_file_readers.py` 對帳",
}


def _derived_disk_loaders() -> set[str]:
    """`discord_bot` 裡「名字是 `_load_*`、而且讀了某個 `*_FILE` 常數」的函式。

    推導而不是抄一份：新增第四個載入器時要有人被逼著決定它算不算容器。
    """
    import ast as _ast
    tree = _ast.parse(Path(b.__file__).read_text(encoding="utf-8"), b.__file__)
    found: set[str] = set()
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_load_"):
            continue
        for sub in _ast.walk(node):
            if (isinstance(sub, _ast.Name) and sub.id.endswith("_FILE")
                    and sub.id.isupper()):
                found.add(node.name)
                break
    return found


def test_the_disk_loader_registry_is_not_stale():
    """兩份登記合起來要**剛好**等於推導出來的集合，兩個方向都檢查。

    只檢查一個方向的話另一個方向永遠是綠的——本 repo 已經為這個形狀吃過數次虧
    （`_OWNER_ONLY_SLASH`、`_pid_alive` 的列舉、原子寫入的常數清單）。
    """
    derived = _derived_disk_loaders()
    assert len(derived) >= 3, f"推導只找到 {sorted(derived)}，語料像是空的"
    registered = set(_DISK_LOADERS) | set(_NOT_A_CONTAINER)
    assert derived == registered, (
        f"少登記的：{sorted(derived - registered)}；"
        f"登記了但已經不存在的：{sorted(registered - derived)}")
    for name, why in _NOT_A_CONTAINER.items():
        assert len(why) >= 10, f"{name} 的理由太短，寫清楚為什麼它不是容器"
    for name, const in _DISK_LOADERS.items():
        assert hasattr(b, const), f"{name} 登記的常數 {const} 不存在了"


def _items_after_loading(name, tmp_path, monkeypatch, payload):
    """把 `payload` 寫成那個載入器的檔案，讀回來，回傳「它交出去的條目」。"""
    path = tmp_path / "store.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(b, _DISK_LOADERS[name], path)
    if name == "_load_image_msgs":
        monkeypatch.setattr(b, "_RECENT_IMAGE_MSGS", {})
        b._load_image_msgs()
        return list(b._RECENT_IMAGE_MSGS.items())
    got = getattr(b, name)()
    if name == "_load_schedules":
        return list(got.get("entries", []))
    return list(got.items())


# 每一個載入器的「一份混了壞條目的檔案」＋「活下來的條目應該長什麼樣」。
_HOSTILE_STORES = {
    "_load_favorites": (
        {"好角色": ["a.png"], "壞的": "不是清單", "數字": 3, "空的": None},
        lambda item: (isinstance(item[0], str) and isinstance(item[1], list)
                      and all(isinstance(x, str) for x in item[1])),
    ),
    "_load_image_msgs": (
        {"123": ["不存在的路徑.png"], "abc": ["x.png"], "456": "不是清單",
         "789": [7, None]},
        lambda item: (isinstance(item[0], int) and isinstance(item[1], list)),
    ),
    "_load_schedules": (
        {"version": 1, "next_id": 9,
         # ⚠️ 下面四筆「是 dict、但編號拿不出來」是 2026-09-21 補的。在那之前語料
         # 裡的壞條目**全都不是 dict**，於是過濾器只留 `isinstance(entry, dict)`
         # 那一半也全綠（變異測試當場 SURVIVED）。`id` 是這個檔的主鍵——
         # `remove` / `run` 都靠它定位——所以「留下一筆認不出編號的條目」等於留下
         # 一筆使用者刪不掉的東西。
         "entries": ["oops", None, 3, [],
                     {"id": "abc", "when_kind": "at", "when_value": "09:30"},
                     {"id": None, "when_kind": "at", "when_value": "09:30"},
                     {"id": True, "when_kind": "at", "when_value": "09:30"},
                     {"id": float("inf"), "when_kind": "at",
                      "when_value": "09:30"},
                     {"when_kind": "at", "when_value": "09:30"},
                     {"id": 1, "when_kind": "at", "when_value": "09:30"}]},
        # 用產品自己的判定當預期值**只在這裡**可以：這一格問的是「載入器有沒有
        # 真的把它套上去」，不是「它判得對不對」——後者由下面那張真值表單獨釘，
        # 兩邊一起被同一個變異改掉的話那張表會先紅。
        lambda item: (isinstance(item, dict)
                      and b._schedule_entry_id(item) is not None),
    ),
}


@pytest.mark.parametrize("name", sorted(_HOSTILE_STORES))
def test_every_disk_loader_filters_the_items_it_hands_back(
        name, tmp_path, monkeypatch):
    """載入器交出去的每一筆都要是它宣告的形狀——檔案裡混了什麼都一樣。

    這三份是同一條紀律的三份實作，而在此之前只有兩份做到。比較的是**交出去的
    東西**而不是「有沒有丟例外」：`_load_schedules` 從來不丟例外，它只是把壞條目
    原封不動交給下游，然後下游在一個 broad except 裡靜靜死掉。
    """
    assert set(_HOSTILE_STORES) <= set(_DISK_LOADERS), "語料表比登記表大"
    payload, ok = _HOSTILE_STORES[name]
    items = _items_after_loading(name, tmp_path, monkeypatch, payload)
    bad = [item for item in items if not ok(item)]
    assert not bad, f"{name} 把壞掉的條目原樣交出去了：{bad!r}"


def test_the_hostile_store_corpus_is_actually_hostile():
    """對照組：語料裡真的每一份都含有至少一筆壞條目。

    沒有這一格，把三份語料改成乾淨的檔案，上面那支照樣全綠——一個沒有壞資料的
    語料跟一個過濾得很好的載入器長得一模一樣。
    """
    for name, (payload, ok) in _HOSTILE_STORES.items():
        raw = (payload.get("entries") if name == "_load_schedules"
               else list(payload.items()))
        rejected = [item for item in raw if not ok(item)]
        assert rejected, f"{name} 的語料裡沒有半筆壞條目"
    # `_load_schedules` 另外要求：壞條目不得**清一色**是「不是 dict」。少了這一
    # 行，語料退回 2026-09-21 之前那個樣子（四筆全是非 dict）時上面照樣全綠，而
    # 過濾器驗編號的那一半就再也沒有人在看了。
    entries = _HOSTILE_STORES["_load_schedules"][0]["entries"]
    assert [e for e in entries if isinstance(e, dict)
            and b._schedule_entry_id(e) is None], (
        "`_load_schedules` 的語料裡沒有半筆「是 dict、但編號拿不出來」的條目——"
        "過濾器只留 `isinstance(entry, dict)` 也會全綠")


# `_schedule_entry_id` 的真值表。**預期值是手寫的，不是拿產品函式算出來的**：
# 上面那格用它當預期值，所以它自己要有一張獨立的表，否則一個「永遠回 0」的變異
# 會讓過濾器與預期值一起放行，兩邊都綠。
#
# 三個例外型別缺一不可（`int("abc")` 是 `ValueError`、`int(None)` 是 `TypeError`、
# `int(float("inf"))` 是 `OverflowError`），而 `OverflowError` 那一格 2026-09-21
# 的變異測試量到是 SURVIVED——`1e400` 連手改檔案都不必，一個夠大的數字
# `json.loads` 出來就是 `inf`。
_SCHEDULE_ID_CASES = [
    ("正常整數", 7, 7),
    ("零", 0, 0),
    ("字串數字", "5", 5),              # 讀得出來就算數，不強迫型別
    ("截斷的浮點", 3.7, 3),
    ("布林 true", True, None),         # `int(True)` 是 1，會跟真的 #1 撞號
    ("布林 false", False, None),
    ("純字串", "abc", None),
    ("None", None, None),
    ("清單", [1], None),
    ("字典", {"a": 1}, None),
    ("inf", float("inf"), None),
    ("nan", float("nan"), None),
]


@pytest.mark.parametrize("label,raw,want", _SCHEDULE_ID_CASES,
                         ids=[row[0] for row in _SCHEDULE_ID_CASES])
def test_the_schedule_id_reader_tells_the_shapes_apart(label, raw, want):
    """認不出來的編號要回 `None`，不是 0——0 會讓所有壞條目撞在同一個主鍵上。"""
    assert b._schedule_entry_id({"id": raw}) == want, label


def test_a_missing_id_key_is_not_an_id_of_zero():
    """整個 `id` 鍵不見了，也算「拿不出編號」。

    `entry.get("id", 0)` 與 `entry.get("id")` 在這裡差很多：前者會讓每一筆沒有編
    號的條目都變成 #0，而 `/schedule remove 0` 就會刪到其中隨便一筆。
    """
    assert b._schedule_entry_id({}) is None
    assert b._schedule_entry_id({"when_kind": "at"}) is None


def test_every_schedule_subcommand_survives_a_corrupt_entry(monkeypatch,
                                                            tmp_path):
    """壞掉的一筆不得讓 `/schedule` 整組指令變成「內部錯誤」。

    `add` 之外的三支都會走過 `entries` 裡的每一筆，所以它們是**修復路徑**——
    使用者要靠 `list` 看見問題、靠 `remove` 把它拿掉。修復路徑被它要修的資料
    弄壞，是這一類缺陷裡最難受的形狀。
    """
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    message, sent = _schedule_test_message(monkeypatch)
    b._save_schedules({
        "version": 1, "next_id": 3,
        "entries": [
            "這一筆不是 dict",
            {"id": 2, "kind": "sh", "payload": "true", "when_kind": "at",
             "when_value": "09:30", "channel_id": 1, "user_id": 7,
             "last_run": 0.0, "last_date": ""},
        ],
    })
    for sub in ("list", "remove 2"):
        sent.clear()
        try:
            asyncio.run(b.cmd_schedule(message, sub))
        except Exception as error:       # noqa: BLE001  這正是要抓的東西
            raise AssertionError(
                f"`/schedule {sub}` 被一筆壞條目打成 "
                f"{type(error).__name__}——使用者只會看到一句「指令發生內部"
                "錯誤」，而這正是他要用來修好它的那條路") from error
        assert sent, f"`/schedule {sub}` 什麼都沒回"
    print("  OK 壞掉的條目沒有讓修復路徑跟著壞掉")


def test_a_corrupt_entry_does_not_stop_every_other_schedule(monkeypatch,
                                                            tmp_path):
    """壞掉的一筆不得讓整輪停擺——而這是靜默的：`/schedule list` 照常顯示。

    判定用的是「這一輪有沒有跑完」（＝有沒有存檔）與「到期那筆有沒有被處理」，
    不是「有沒有丟例外」：`_schedule_loop` 自己有 broad except，所以例外永遠不會
    冒出來，只會變成 stderr 上一行、然後每一輪重複一次。
    """
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    monkeypatch.setattr(b, "_SCHEDULE_TICK_SEC", 0.0)
    _schedule_test_message(monkeypatch)

    now = time.time()
    b._save_schedules({
        "version": 1, "next_id": 3,
        "entries": [
            "這一筆不是 dict",                    # 手改過的檔案
            {"id": 2, "when_kind": "once",
             # 早就過了補跑窗口 → 這一筆本來會被回報並移除
             "when_value": f"{now - b.SCHEDULE_CATCHUP_MAX_SEC - 3600:.0f}",
             "kind": "sh", "payload": "true", "channel_id": 1, "user_id": 7,
             "last_run": 0.0, "last_date": ""},
        ],
    })
    saved = _schedule_save_spy(monkeypatch)

    async def _body():
        async def _quiet_report(_entry, _text):
            return None

        monkeypatch.setattr(b, "_schedule_report", _quiet_report)
        try:
            await _one_schedule_tick(saved)
        except asyncio.TimeoutError:
            return None
        return b._load_schedules()

    data = asyncio.run(_body())
    assert data is not None, (
        "排程迴圈被一筆壞條目打斷，整輪沒有跑完——排在它後面的排程永遠不會執行，"
        "而 `/schedule list` 還是照常把它們列出來")
    ids = [e.get("id") for e in data.get("entries", []) if isinstance(e, dict)]
    assert 2 not in ids, (
        f"到期的那筆排程沒有被處理：entries={data.get('entries')}")
    print("  OK 壞掉的條目沒有讓整輪停擺")


# --------------------------------------------------------------------------
# 衍生守門：建立時放行的監看目標，探測每一輪都要走得到真正的探測（2026-09-21）
# --------------------------------------------------------------------------
# 上面那幾支釘的是 `port` / `pixel` / `job` 三個；這一支釘的是**形狀**，`_WATCH_POLL`
# 裡每一個 kind（包括以後新增的）自動納入。舊的缺陷是探測在 `try` 裡面自己解析目標，
# 失敗就被吞成「還沒成立」——一個永遠不會觸發、兩小時後才安靜過期的監看。判準不看
# 錯誤訊息的文字：`_gui` 換成一個代理，解析函式照用真的，其餘一律記錄；**建立時放行
# 的目標，探測必須至少走到一次真正的探測呼叫**。在半路失敗而被吞掉的，一次都走不到。

_WATCH_TARGET_POOL = ("abc", "記事本", "10 10", "10 10 #ffffff", "10,10 #ffffff",
                      "8080", "localhost:8080", "7", "0", "@@@")


class _WatchGuiProxy:
    """解析函式與大寫開頭的名字（例外類別、常數）照用真的；其餘一律記錄、回替身值。

    替身值只給形狀有要求的兩個（`job_list` 要能迭代、`pixel_color` 要能解包），其他
    回 None——探測都用 `bool()` 收，None 就是「還沒成立」。
    """

    _RETURNS = {"job_list": [{"id": 7, "running": True}], "pixel_color": (0, 0, 0)}

    def __init__(self, real) -> None:
        self._real = real
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("parse_") or name[:1].isupper():
            return getattr(self._real, name)
        self.calls.append(name)
        value = self._RETURNS.get(name)
        return lambda *args, **kwargs: value


def _watch_probe_problems(kinds, parsers, probe, pool):
    """回 `(每個 kind 放行了幾個目標, 問題清單)`。

    `probe(kind, target)` 回傳那一次探測記錄到的真正探測呼叫。建立時會被 `parsers`
    擋掉的目標跳過（那正是建立時該擋的）；放行的目標若一次探測呼叫都沒有，就是在
    半路失敗了。每個 kind 只留第一個例子。
    """
    accepted: dict[str, int] = {}
    problems: list[str] = []
    for kind in sorted(kinds):
        accepted[kind] = 0
        parser = parsers.get(kind)
        for target in pool:
            if parser is not None:
                try:
                    parser(target)
                except b._GuiError:
                    continue
            accepted[kind] += 1
            if not probe(kind, target):
                problems.append(
                    f"`{kind}` 的目標 {target!r} 建立時放行，探測卻一次都沒走到真正的"
                    "探測呼叫——它在半路失敗、被吞成「還沒成立」了")
                break
    return accepted, problems


def test_the_watch_probe_check_can_actually_see_a_swallowed_parse():
    """合成對照組。真實樹是乾淨的，所以「報出問題」那幾行在真實資料上從來不執行。"""
    def only_digits(target: str) -> int:
        if not target.isdigit():
            raise b._GuiError("不是數字。")
        return int(target)

    def probe(kind: str, target: str) -> list[str]:
        if kind == "inline":
            # 舊的 `pixel` 就是這個形狀：在 try 裡自己解析，失敗就當「還沒成立」。
            try:
                int(target)
            except ValueError:
                return []
        return ["a_real_probe"]

    accepted, problems = _watch_probe_problems(
        {"inline", "free", "parsed"}, {"parsed": only_digits}, probe, ("abc", "7"))
    assert accepted == {"free": 2, "inline": 1, "parsed": 1}, accepted
    assert len(problems) == 1, problems
    assert "`inline`" in problems[0] and "'abc'" in problems[0], problems


def test_every_watch_target_accepted_at_creation_reaches_a_real_probe(monkeypatch):
    real = b._gui

    def probe(kind: str, target: str) -> list[str]:
        proxy = _WatchGuiProxy(real)
        monkeypatch.setattr(b, "_gui", proxy)
        try:
            asyncio.run(b._watch_condition_met(kind, target, {"clip_baseline": ""}))
        finally:
            monkeypatch.setattr(b, "_gui", real)
        return proxy.calls

    stale = sorted(set(b._WATCH_TARGET_PARSERS) - set(b._WATCH_POLL))
    assert not stale, f"`_WATCH_TARGET_PARSERS` 有不存在的監看種類：{stale}"
    accepted, problems = _watch_probe_problems(
        set(b._WATCH_POLL), b._WATCH_TARGET_PARSERS, probe, _WATCH_TARGET_POOL)
    silent = sorted(kind for kind, count in accepted.items() if count == 0)
    assert not silent, (
        f"目標池生不出這些監看種類的任何一個合法目標，等於沒檢查：{silent}；"
        "把它需要的目標形狀加進 `_WATCH_TARGET_POOL`。")
    assert len(accepted) >= 8, accepted
    assert not problems, (
        "建立時放行、探測每一輪卻都失敗的監看目標（建立與探測要走同一支"
        "`_WATCH_TARGET_PARSERS`）：\n" + "\n".join(problems))


# --------------------------------------------------------------------------
# 「現在接受、之後才執行」的巨集：建立時就驗代入後的整個程式（2026-09-21）
#
# 排程與監看原本只檢查巨集**存不存在**，參數則存起來、等觸發時才代入。於是
# `/schedule add 09:30 macro fill a 150`（`wait_ui $2 …` 的上限是 120 秒）被收下，
# 明天九點才回一句失敗。現在兩個入口跟 `/macro run` 都走 `_gui.check_macro_program`
# ——跟重播的事前檢查是同一支。
# --------------------------------------------------------------------------
_FILL_STEPS = ["click 1 1", "type $1", "wait_ui $2 存檔"]


@pytest.fixture(name="fill_macro")
def _fill_macro_fixture(monkeypatch, tmp_path):
    """一個存在暫存目錄裡的 `fill` 巨集；桌面動作一律換成只記錄的替身。"""
    monkeypatch.setattr(b._gui, "MACRO_DIR", tmp_path / "macros")
    b._gui.save_macro("fill", list(_FILL_STEPS))
    actions: list = []
    for name in ("mouse_click", "type_text", "ui_wait"):
        monkeypatch.setattr(
            b._gui, name,
            lambda *a, _n=name, **k: actions.append((_n, a)) or {"x": 1, "y": 2})
    return actions


@pytest.mark.parametrize("args, hint", [
    ("", "第 2 行"),              # `type $1` 沒有參數
    (" a 150", "秒數上限"),        # `wait_ui $2` 代進去超過 120 秒
])
def test_schedule_creation_rejects_a_bad_substituted_program(
        monkeypatch, tmp_path, fill_macro, args, hint):
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    message, sent = _schedule_test_message(monkeypatch)
    asyncio.run(b.cmd_schedule(message, f"add 09:30 macro fill{args}"))
    assert sent and sent[-1].startswith("❌ "), sent
    assert hint in sent[-1], sent[-1]
    assert not b._load_schedules().get("entries"), "壞參數的排程被存下來了"
    assert fill_macro == [], "建立排程時碰了桌面"


def test_schedule_creation_still_accepts_a_valid_substituted_program(
        monkeypatch, tmp_path, fill_macro):
    """反方向：合法的參數照樣建得起來，存下來的 payload 就是使用者打的那一串。"""
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    message, sent = _schedule_test_message(monkeypatch)
    asyncio.run(b.cmd_schedule(message, "add 09:30 macro fill a 12"))
    assert any("已建立排程" in s for s in sent), sent
    entries = b._load_schedules().get("entries")
    assert [e["payload"] for e in entries] == ["fill a 12"], entries
    assert fill_macro == []


def test_schedule_creation_still_rejects_a_missing_macro(monkeypatch, tmp_path,
                                                         fill_macro):
    del fill_macro
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    message, sent = _schedule_test_message(monkeypatch)
    asyncio.run(b.cmd_schedule(message, "add 09:30 macro ghost"))
    assert sent[-1].startswith("❌ ") and "找不到這個巨集" in sent[-1], sent


@pytest.mark.parametrize("run_spec, created", [
    ("fill a 150", False),
    ("fill", False),
    ("ghost a 12", False),
    ("fill a 12", True),
])
def test_watch_creation_validates_the_stored_macro_arguments(
        monkeypatch, fill_macro, run_spec, created):
    replies, watches, loops = _create_watch(
        monkeypatch, f"window 記事本 --run {run_spec}")
    if created:
        assert len(watches) == 1 and len(loops) == 1, replies
        action = loops[0][5]
        assert action == {"macro": "fill", "args": ["a", "12"]}, action
    else:
        assert watches == {} and loops == [], replies
        assert replies[-1].startswith("❌ `--run` 的巨集有問題："), replies
    assert fill_macro == [], "建立監看時碰了桌面"


def _macro_run_harness(monkeypatch):
    """跑 `cmd_macro run` 需要的替身：回覆、閘門、直譯器都只記錄。"""
    sent: list = []
    gate: list = []
    ran: list = []

    async def _reply(_message, content=None, **_kw):
        sent.append(str(content))
        return None

    def _gate(label):
        gate.append(label)
        return None

    def _run(steps, **kwargs):
        ran.append((list(steps), kwargs.get("args")))
        return ["ok"]

    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_macro_gate_acquire", _gate)
    monkeypatch.setattr(b, "_macro_gate_release", lambda: False)
    monkeypatch.setattr(b._gui, "run_macro_program", _run)
    return types.SimpleNamespace(author=None), sent, gate, ran


def test_macro_run_rejects_a_bad_substituted_program_before_taking_the_gate(
        monkeypatch, fill_macro):
    """壞掉的程式不佔鍵鼠、不進直譯器，回覆講的是哪個巨集的第幾行。"""
    message, sent, gate, ran = _macro_run_harness(monkeypatch)
    asyncio.run(b.cmd_macro(message, "run fill a 150"))
    assert sent == ["❌ 巨集 `fill` 第 3 行：秒數上限是 120 秒。"], sent
    assert gate == [] and ran == [], (gate, ran)
    assert fill_macro == []


def test_macro_run_still_runs_a_valid_substituted_program(monkeypatch, fill_macro):
    del fill_macro
    message, sent, gate, ran = _macro_run_harness(monkeypatch)
    asyncio.run(b.cmd_macro(message, "run fill a 12"))
    assert gate == ["fill"], gate
    assert ran == [(_FILL_STEPS, ["a", "12"])], ran
    assert any(s.startswith("✅") for s in sent), sent


def _schedule_entry_text(monkeypatch, entry: dict) -> str:
    """跑一次 `_run_schedule_entry`，回它要送回頻道的那一句。"""
    reported: list = []

    async def _report(_entry, text):
        reported.append(text)

    monkeypatch.setattr(b, "_schedule_report", _report)
    asyncio.run(b._run_schedule_entry(entry))
    assert len(reported) == 1, reported
    return reported[0]


def test_a_schedule_macro_failure_reports_the_safe_message(monkeypatch, fill_macro):
    """`GuiError` 的訊息是本專案寫的泛用句，照 `/macro run` 的做法直接回。

    建立時驗過，但巨集檔之後被改壞（或參數在建立之後才變得不合法）時，這一句
    是使用者唯一的線索；原本一律改寫成「詳情請查看 log」，人要回到主機前面才
    知道是哪一行——而排程存在的理由就是人不在。這裡走真的重播路徑，所以也順便
    證明事前檢查在任何動作之前就擋下了。
    """
    text = _schedule_entry_text(
        monkeypatch, {"id": 5, "kind": "macro", "payload": "fill a 150"})
    assert text == "❌ 排程 `#5` 的巨集失敗：第 3 行：秒數上限是 120 秒。", text
    assert fill_macro == [], "排程在失敗之前已經碰了桌面"


def test_a_schedule_shell_guierror_reports_the_safe_message(monkeypatch):
    def _shell(*_a, **_k):
        raise b._GuiError("指令逾時，已結束。")

    monkeypatch.setattr(b._gui, "run_shell", _shell)
    text = _schedule_entry_text(
        monkeypatch, {"id": 6, "kind": "sh", "payload": "true"})
    assert text == "❌ 排程 `#6` 的指令失敗：指令逾時，已結束。", text


def test_a_schedule_unexpected_error_still_hides_the_raw_text(monkeypatch,
                                                            fill_macro):
    """反方向：不是 `GuiError` 的例外照舊只寫 log——原文可能夾帶主機路徑。"""
    del fill_macro

    def _boom(*_a, **_k):
        raise RuntimeError(r"C:\Users\someone\secret.txt 讀不到")

    monkeypatch.setattr(b._gui, "run_macro_program", _boom)
    text = _schedule_entry_text(
        monkeypatch, {"id": 7, "kind": "macro", "payload": "fill a 12"})
    assert text == "❌ 排程 `#7` 執行失敗，詳情請查看 log。", text


def test_a_schedule_abort_is_not_reported_as_a_failure(monkeypatch, fill_macro):
    """`GuiAborted` 是 `GuiError` 的子類別，但「被停下來」不是失敗。"""
    del fill_macro

    def _stopped(*_a, **_k):
        raise b._gui.GuiAborted("已依要求中止。")

    monkeypatch.setattr(b._gui, "run_macro_program", _stopped)
    text = _schedule_entry_text(
        monkeypatch, {"id": 8, "kind": "macro", "payload": "fill a 12"})
    assert text == "⏹️ 排程 `#8` 已中止。", text


def _watch_action_replies(monkeypatch, action: dict) -> list:
    """跑一次 `_watch_run_action`，回它送出的每一句；鍵鼠閘門用真的那一支。"""
    sent: list = []

    async def _reply(_message, content=None, **_kw):
        sent.append(str(content))

    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_MACRO_RUNNING", None)
    monkeypatch.setattr(b, "_MACRO_RECORDING", None)
    asyncio.run(b._watch_run_action(types.SimpleNamespace(author=None), 9, action))
    assert b._MACRO_RUNNING is None, "監看跑完沒有放開鍵鼠閘門"
    return sent


def test_a_watch_macro_abort_is_not_reported_as_a_failure(monkeypatch, fill_macro):
    """與排程那支同一個理由：`GuiAborted` 是 `GuiError` 的子類別，但被停下來不是失敗。"""
    del fill_macro

    def _stopped(*_a, **_k):
        raise b._gui.GuiAborted("已依要求中止。")

    monkeypatch.setattr(b._gui, "run_macro_program", _stopped)
    sent = _watch_action_replies(monkeypatch, {"macro": "fill", "args": ["a", "12"]})
    assert sent == ["⏹️ 監看 `#9`：巨集 `fill` 已中止。"], sent


def test_a_watch_macro_guierror_is_still_reported_as_a_failure(monkeypatch,
                                                              fill_macro):
    """反方向：真的 `GuiError`（這裡是代入後超過秒數上限）照舊是紅字的失敗。

    少了這一支，把中止那一支寫成 `except _GuiError` 會讓每一種失敗都被講成「已中止」
    ——而上面那支照樣是綠的。
    """
    sent = _watch_action_replies(monkeypatch, {"macro": "fill", "args": ["a", "150"]})
    assert sent == ["❌ 監看 `#9` 的巨集失敗：第 3 行：秒數上限是 120 秒。"], sent
    assert fill_macro == [], "事前檢查沒擋住，巨集碰了桌面"


# ---------------------------------------------------------------------------
# `/run in`／`/run at`：解析器的有限性、排工作之前先組回覆、以及跨重啟還原
#
# 三件事共用一套夾具（`_sr_env`）：落地檔、標籤檔、暫停標記一律導到 tmp_path；
# `_do_webrunner_run` 換成記錄器——**這台機器上跑著正式批次，真的那一支會先把整台
# 機器的瀏覽器行程殺光**；三個排程全域用 monkeypatch 設回初始值，測試結束自動還原。
# 事件、稽核檔由 conftest 導開。
#
# ⚠️ 偵測器都是記錄器（送出的訊息、被呼叫的次數、檔案在不在），不是「有沒有拋
# 例外」：`_restore_scheduled_run_once` 與 `_scheduled_run_loop` 最外層都有
# blanket `except`，拿例外當偵測器的測試會永遠是綠的。
# ---------------------------------------------------------------------------
class _SrChannel:
    """記下 `send` 內容的假頻道。"""

    def __init__(self, cid: int):
        self.id = cid
        self.sent: list = []

    async def send(self, content=None, **_kw):
        self.sent.append(content)
        return None


@pytest.fixture
def _sr_env(monkeypatch, tmp_path):
    """延後啟動測試的共用環境；回傳一個記錄用的命名空間。"""
    env = types.SimpleNamespace(
        file=tmp_path / "scheduled_run.json", runs=[], replies=[],
        run_error=None)

    async def _fake_run(channel):
        env.runs.append(channel)
        if env.run_error is not None:
            raise env.run_error

    async def _reply(_message, content=None, **_kw):
        env.replies.append(content)
        return None

    monkeypatch.setattr(b, "SCHEDULED_RUN_FILE", env.file)
    monkeypatch.setattr(b, "BATCH_LABEL_FILE", tmp_path / "batch_label.txt")
    monkeypatch.setattr(b, "WEBRUNNER_PAUSE_FILE", tmp_path / "webrunner.pause")
    monkeypatch.setattr(b, "_do_webrunner_run", _fake_run)
    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_scheduled_run_task", None)
    monkeypatch.setattr(b, "_scheduled_run_ts", None)
    monkeypatch.setattr(b, "_scheduled_run_restored", False)
    # `/status` 會問存活狀態，而那一支讀的是**正式的** pid 檔。
    monkeypatch.setattr(b, "_webrunner_liveness", lambda: (False, True))
    monkeypatch.setattr(b, "_webrunner_proc", None)
    return env


def _sr_message(channel_id: int = 4242):
    channel = _SrChannel(channel_id)
    return types.SimpleNamespace(
        channel=channel, author=types.SimpleNamespace(id=1)), channel


def _sr_run(coro, timeout: float = 10.0):
    """跑一段 async 測試本體；有牆鐘上限——回歸時要變紅，不能掛住。"""
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def _sr_settle(task, timeout: float = 5.0) -> None:
    """等一個排程工作結束（成功、失敗或被取消都算），並取走它的例外。"""
    await asyncio.wait([task], timeout=timeout)
    assert task.done(), "排程工作在時限內沒有結束"
    if not task.cancelled():
        task.exception()


_SR_BAD_DELAYS = ("in nan", "in -nan", "in inf", "in -inf", "in 1e400",
                  "in 1e10h", "in 8d", "in 604801s", "in 168.01h", "in 10081",
                  "in nanh", "in infs")


@pytest.mark.parametrize("spec", _SR_BAD_DELAYS)
def test_run_schedule_rejects_non_finite_and_out_of_range_delays(spec):
    """`float()` 收得下 `nan`／`inf`／`1e400`，而回傳值會流到 `time.localtime`、
    `asyncio.sleep` 與落地檔——每一站都會丟例外。解析器是唯一的閘。"""
    assert b._parse_run_schedule(spec) is None, spec


@pytest.mark.parametrize("spec", ("in 604800s", "in 168h", "in 10080"))
def test_run_schedule_accepts_exactly_the_horizon(spec):
    """上限是含的：剛好七天要收下（邊界另一側在上面那支）。"""
    now = time.time()
    got = b._parse_run_schedule(spec)
    assert got is not None, spec
    _elapsed_eq(got - now, b.SCHEDULE_HORIZON_SEC, spec)


def test_run_schedule_and_the_schedule_interval_share_one_horizon():
    """`/run in` 的最長延遲與 `/schedule every` 的最長間隔是同一個常數。

    只驗其中一邊的話，有人把 `_parse_schedule_when` 那一行改回寫死的數字（或反過來）
    不會有任何東西變紅——兩個上限就會各自漂。"""
    hours = b.SCHEDULE_HORIZON_SEC // 3600
    assert b.SCHEDULE_HORIZON_SEC == 7 * 24 * 3600
    assert b._parse_run_schedule(f"in {hours}h") is not None
    assert b._parse_run_schedule(f"in {hours + 1}h") is None
    assert b._parse_schedule_when(f"every {hours}h") == (
        "every", str(b.SCHEDULE_HORIZON_SEC))
    with pytest.raises(b._GuiError):
        b._parse_schedule_when(f"every {hours + 1}h")


@pytest.mark.parametrize("spec", _SR_BAD_DELAYS)
def test_a_rejected_run_schedule_arms_nothing_and_status_still_renders(_sr_env, spec):
    """被擋下的延遲：回用法說明、什麼都沒排、沒寫檔，而且 `/status` 照常出得來。

    修掉之前：`cmd_run` 先排工作才格式化回覆，回覆那行一丟例外，工作已經排上了
    ——`nan` 會在 `asyncio.sleep(nan)` 炸掉並 @ 擁有者，`inf` 類則變成永遠不會到的
    幽靈排程，期間每一次 `/status` 都在 `time.localtime` 爆掉。"""
    message, _channel = _sr_message()

    async def _body():
        await b.cmd_run(message, spec)
        armed = b._scheduled_run_task
        ts = b._scheduled_run_ts
        await b.cmd_status(message)
        return armed, ts

    armed, ts = _sr_run(_body())
    assert armed is None and ts is None, (spec, armed, ts)
    assert not _sr_env.file.exists(), spec
    assert _sr_env.replies, "連用法說明都沒有回"
    assert "couldn't parse" in _sr_env.replies[0], _sr_env.replies[0]
    # `/status` 有回（存活那一行），而且沒有提到排程。
    assert len(_sr_env.replies) == 2, _sr_env.replies
    assert "scheduled" not in _sr_env.replies[1], _sr_env.replies[1]
    assert _sr_env.runs == []


def test_a_valid_run_schedule_arms_writes_the_file_and_shows_in_status(_sr_env):
    """正面對照：上面那支的「什麼都沒排」要有意義，合法的延遲就必須真的排上、
    寫檔，並出現在 `/status`。"""
    message, _channel = _sr_message(4242)

    async def _body():
        before = time.time()
        await b.cmd_run(message, "in 90m")
        task, ts = b._scheduled_run_task, b._scheduled_run_ts
        record = json.loads(_sr_env.file.read_text(encoding="utf-8"))
        await b.cmd_status(message)
        task.cancel()
        await _sr_settle(task)
        return before, ts, record

    before, ts, record = _sr_run(_body())
    _elapsed_eq(ts - before, 5400, "in 90m")
    assert record == {"due": ts, "channel_id": 4242}, record
    assert "scheduled `/run`" in _sr_env.replies[0], _sr_env.replies
    assert "a run is scheduled" in _sr_env.replies[-1], _sr_env.replies
    # 回覆裡不得出現落地檔名（Layer 1）。
    assert not any("scheduled_run.json" in str(r) for r in _sr_env.replies)


def test_a_schedule_that_could_not_be_saved_says_so(_sr_env, monkeypatch):
    """落地失敗時排程照樣在記憶體裡，但回覆要講明它撐不過重啟——否則又是那種
    「收到確認、之後安靜消失」。"""
    def _boom(*_a, **_k):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(b, "_atomic_write_text", _boom)
    message, _channel = _sr_message()

    async def _body():
        await b.cmd_run(message, "in 90m")
        task = b._scheduled_run_task
        armed = task is not None and not task.done()
        task.cancel()
        await _sr_settle(task)
        return armed

    assert _sr_run(_body()), "落地失敗不該連記憶體裡的排程都不排"
    reply = _sr_env.replies[0]
    assert "沒能存下來" in reply, reply
    assert "denied" not in reply and "PermissionError" not in reply, reply


def test_cmd_run_formats_the_reply_before_it_arms_the_task(_sr_env, monkeypatch):
    """萬一有一個壞時間戳繞過解析器，格式化失敗也不得留下一個已經排上的工作。

    用替身讓解析器回 nan，模擬「未來有人放寬了解析器」：`time.localtime(nan)` 丟
    ValueError，而此時什麼都還沒排、也還沒寫檔。"""
    monkeypatch.setattr(b, "_parse_run_schedule", lambda _arg: math.nan)
    message, _channel = _sr_message()

    async def _body():
        with pytest.raises(ValueError):
            await b.cmd_run(message, "in 5m")
        return b._scheduled_run_task, b._scheduled_run_ts

    task, ts = _sr_run(_body())
    assert task is None and ts is None, (task, ts)
    assert not _sr_env.file.exists()


def test_run_cancel_deletes_the_scheduled_run_file(_sr_env):
    message, _channel = _sr_message()

    async def _body():
        await b.cmd_run(message, "in 90m")
        task = b._scheduled_run_task
        assert _sr_env.file.exists(), "排程沒有寫檔，下面的斷言沒有意義"
        await b.cmd_run(message, "cancel")
        await _sr_settle(task)
        return task

    task = _sr_run(_body())
    assert task.cancelled()
    assert not _sr_env.file.exists(), "取消之後落地檔還在，重啟會把它接回來"
    assert b._scheduled_run_task is None
    assert "cancelled" in _sr_env.replies[-1], _sr_env.replies


def test_run_cancel_deletes_a_file_that_no_task_holds(_sr_env):
    """還原時找不到頻道會把檔案留著；那時記憶體裡沒有工作，取消仍要清掉它。"""
    _sr_env.file.write_text(json.dumps({"due": time.time() + 600,
                                        "channel_id": 1}), encoding="utf-8")
    message, _channel = _sr_message()
    _sr_run(b.cmd_run(message, "cancel"))
    assert not _sr_env.file.exists()


def test_an_immediate_run_deletes_the_scheduled_run_file(_sr_env):
    message, channel = _sr_message()

    async def _body():
        await b.cmd_run(message, "in 90m")
        task = b._scheduled_run_task
        assert _sr_env.file.exists()
        await b.cmd_run(message, "")
        await _sr_settle(task)
        return task

    task = _sr_run(_body())
    assert task.cancelled()
    assert not _sr_env.file.exists(), "立即 /run 之後落地檔還在，重啟會再跑一次"
    assert _sr_env.runs == [channel]


@pytest.mark.parametrize("fails", (False, True), ids=("starts", "fails-to-start"))
def test_the_scheduled_start_deletes_the_file_when_it_fires(_sr_env, fails):
    """到點觸發就刪檔，不論啟動成功與否——否則下一次重啟會再啟動一次。"""
    if fails:
        _sr_env.run_error = PermissionError(13, "log file locked")
    message, channel = _sr_message()

    async def _body():
        await b.cmd_run(message, "in 0.05s")
        task = b._scheduled_run_task
        assert _sr_env.file.exists()
        await _sr_settle(task)

    _sr_run(_body())
    assert _sr_env.runs == [channel], "排程沒有觸發"
    assert not _sr_env.file.exists(), "到點之後落地檔還在"
    assert b._scheduled_run_task is None
    if fails:
        assert "沒有順利開始" in channel.sent[-1], channel.sent
        assert "log file locked" not in channel.sent[-1]


def test_a_scheduled_run_blocked_by_a_non_utf8_queue_says_which_list(_sr_env, tmp_path):
    """到點時佇列檔不是 UTF-8：講清楚是哪一份清單、怎麼修，並刪檔。

    這條路原本只拿得到泛用的「沒有順利開始」——派發層接了這個例外，但排程是另一個
    task，那些 `except` 看不到它。到點時沒有提問者，所以只能是泛用標籤、不含檔名。"""
    error = b._QueueFileNotUtf8(
        tmp_path / "todo_character1.md",
        UnicodeDecodeError("utf-8", b"\xb3\x44", 0, 1, "invalid start byte"))
    _sr_env.run_error = error
    message, channel = _sr_message()

    async def _body():
        await b.cmd_run(message, "in 0.05s")
        await _sr_settle(b._scheduled_run_task)

    _sr_run(_body())
    assert _sr_env.runs == [channel], "排程沒有觸發"
    assert not _sr_env.file.exists()
    last = channel.sent[-1]
    assert "角色1 佇列" in last and "UTF-8" in last, channel.sent
    assert "沒有順利開始" not in last, "落到了泛用的失敗訊息"
    for banned in ("todo_character1.md", str(tmp_path), "invalid start byte",
                   "UnicodeDecodeError"):
        assert banned not in last, f"洩漏了 {banned!r}（Layer 1：沒有提問者＝非擁有者）"


def test_shutdown_cancellation_keeps_the_scheduled_run_file(_sr_env):
    """關機（事件迴圈取消所有工作）時**不得**刪檔——那正是要留給下一個行程的情形。

    這是整個功能最容易被「順手整理」掉的一點：把刪檔搬進 `finally` 看起來很乾淨，
    而那樣每一次 `/sys restart` 都會把排程刪掉。

    ⚠️ 取消之前一定要讓工作先跑到它的第一個 await。**還沒開始跑的 task 被取消時，
    協程本體一行都不會執行**——`CancelledError` 在進入 `try` 之前就丟進去了，
    `finally` 根本走不到。第一版就是這樣寫的，於是「把刪檔搬進 `finally`」這個變異
    活了下來（2026-09-21 實測 SURVIVED）。最後那句「全域被清掉了」是正面對照：
    它只有在 `finally` 真的跑過時才成立。"""
    message, _channel = _sr_message()

    async def _body():
        await b.cmd_run(message, "in 90m")
        task = b._scheduled_run_task
        await asyncio.sleep(0)  # 讓它跑到 `asyncio.sleep(...)` 那一行
        task.cancel()          # 不經過 `/run cancel`：這是關機時的取消
        await _sr_settle(task)

    _sr_run(_body())
    assert b._scheduled_run_task is None, (
        "工作被取消時還沒開始跑，finally 沒走到——這支測試沒有測到它要測的東西")
    assert _sr_env.file.exists(), "關機取消把落地檔刪了，重啟後什麼都還原不了"
    assert _sr_env.runs == []


def test_a_superseded_schedule_does_not_delete_the_current_file(_sr_env):
    """刪檔只在「自己仍是目前的排程」時做：一個不是目前排程的工作觸發了，也不得
    刪掉現在那一份（它可能是新排程剛寫好的）。"""
    _sr_env.file.write_text(json.dumps({"due": time.time() + 600,
                                        "channel_id": 1}), encoding="utf-8")
    channel = _SrChannel(1)

    async def _body():
        stray = asyncio.create_task(
            b._scheduled_run_loop(channel, time.time() - 1))
        await _sr_settle(stray)

    _sr_run(_body())
    assert _sr_env.runs == [channel], "替身沒有被叫到，測試沒走到觸發那一步"
    assert _sr_env.file.exists(), "不是目前排程的工作把落地檔刪了"


def _sr_client(monkeypatch, *, known=(), fetchable=(), fetch_error=None):
    """把 `b.client` 的頻道查詢換成替身；回傳 id → 假頻道。"""
    channels = {cid: _SrChannel(cid) for cid in (*known, *fetchable)}

    def _get(cid):
        return channels.get(cid) if cid in known else None

    async def _fetch(cid):
        if fetch_error is not None:
            raise fetch_error
        if cid in fetchable:
            return channels[cid]
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="nf"),
                               "unknown channel")

    monkeypatch.setattr(b.client, "get_channel", _get)
    monkeypatch.setattr(b.client, "fetch_channel", _fetch)
    return channels


def _sr_write(env, due: float, channel_id: int = 4242) -> None:
    env.file.write_text(json.dumps({"due": due, "channel_id": channel_id}),
                        encoding="utf-8")


def _sr_no_leak(text: str) -> None:
    assert "scheduled_run" not in text and "\\" not in text, text
    assert "!run" not in text, text


def test_restore_rearms_a_schedule_that_is_not_due_yet(_sr_env, monkeypatch):
    """模擬重啟：檔案在、記憶體是空的 → 照原時間重新排，並在原頻道說一聲。"""
    channels = _sr_client(monkeypatch, known=(4242,))
    due = time.time() + 3600
    _sr_write(_sr_env, due)

    async def _body():
        await b._restore_scheduled_run_once()
        task, ts = b._scheduled_run_task, b._scheduled_run_ts
        assert task is not None and not task.done(), "沒有重新排上"
        await asyncio.sleep(0)  # 先讓它跑起來，取消才會走到 finally（見上面那支）
        task.cancel()          # 收尾；同時驗關機取消不刪檔
        await _sr_settle(task)
        assert b._scheduled_run_task is None, "取消時工作還沒開始跑"
        return ts

    ts = _sr_run(_body())
    assert ts == due
    assert _sr_env.file.exists(), "重新排上的排程要保留落地檔"
    assert _sr_env.runs == []
    sent = channels[4242].sent
    assert len(sent) == 1 and "仍然有效" in sent[0], sent
    _sr_no_leak(sent[0])


def test_restore_starts_a_run_missed_within_the_catchup_window(_sr_env, monkeypatch):
    """重啟期間到點、還在補啟動窗口內 → 現在啟動，而且頻道只收到**一則**講清楚
    晚了多久的訊息（不是再多一句「scheduled time reached」）。"""
    channels = _sr_client(monkeypatch, known=(4242,))
    _sr_write(_sr_env, time.time() - b.SCHEDULED_RUN_CATCHUP_SEC + 30)

    async def _body():
        await b._restore_scheduled_run_once()
        task = b._scheduled_run_task
        assert task is not None, "沒有排上補啟動"
        await _sr_settle(task)

    _sr_run(_body())
    assert _sr_env.runs == [channels[4242]], "沒有補啟動"
    assert not _sr_env.file.exists(), "補啟動之後落地檔還在"
    sent = channels[4242].sent
    assert len(sent) == 1, sent
    assert "補啟動" in sent[0] and "scheduled time reached" not in sent[0], sent
    _sr_no_leak(sent[0])


def test_restore_reports_a_run_missed_beyond_the_window(_sr_env, monkeypatch):
    """過了補啟動窗口 → 不啟動、在原頻道講「錯過了」、刪檔。"""
    channels = _sr_client(monkeypatch, known=(4242,))
    _sr_write(_sr_env, time.time() - b.SCHEDULED_RUN_CATCHUP_SEC - 30)

    _sr_run(b._restore_scheduled_run_once())
    assert _sr_env.runs == [], "過了窗口還是啟動了"
    assert b._scheduled_run_task is None and b._scheduled_run_ts is None
    assert not _sr_env.file.exists(), "錯過的排程沒有刪檔，每次重啟都會再講一次"
    sent = channels[4242].sent
    assert len(sent) == 1 and "錯過" in sent[0], sent
    _sr_no_leak(sent[0])


@pytest.mark.parametrize("raw", (
    b"", b"not json", b"[]", b"null", b'"x"',
    b'{"due": "soon", "channel_id": 1}',
    b'{"due": NaN, "channel_id": 1}',
    b'{"due": Infinity, "channel_id": 1}',
    b'{"due": -Infinity, "channel_id": 1}',
    b'{"due": 1e400, "channel_id": 1}',
    b'{"due": 1' + b"0" * 400 + b', "channel_id": 1}',
    b'{"due": true, "channel_id": 1}',
    b'{"due": -5, "channel_id": 1}',
    b'{"due": 0, "channel_id": 1}',
    b'{"due": 99999999999, "channel_id": 1}',
    b'{"due": 1789000000}',
    b'{"due": 1789000000, "channel_id": true}',
    b'{"due": 1789000000, "channel_id": 0}',
    b'{"due": 1789000000, "channel_id": "4242"}',
    b"\xff\xfe\x00garbage",
), ids=lambda raw: raw[:24].decode("latin-1"))
def test_restore_discards_a_malformed_file_without_crashing(_sr_env, monkeypatch, raw):
    """壞掉的落地檔：記 stderr、刪檔、不排任何東西、不送任何訊息、不往上炸。

    這支在 `on_ready` 底下跑；跟 `schedules.json` 同一條規則——修理路徑不能被它要修
    的資料弄壞。巨大整數那一格是實際量到的洞：`float()` 對它丟 `OverflowError`，
    不是 ValueError。"""
    channels = _sr_client(monkeypatch, known=(1, 4242, b.CHANNEL_ID))
    _sr_env.file.write_bytes(raw)

    _sr_run(b._restore_scheduled_run_once())
    assert not _sr_env.file.exists(), "壞掉的落地檔沒有被刪"
    assert b._scheduled_run_task is None and b._scheduled_run_ts is None
    assert _sr_env.runs == []
    assert all(not ch.sent for ch in channels.values()), channels


def test_the_malformed_file_parser_accepts_a_well_formed_record():
    """正面對照：上面那支的「全部刪掉」要有意義，合法的紀錄必須解析得出來。"""
    now = 1_789_000_000.0
    due, cid, origin = b._parse_scheduled_run_record(
        json.dumps({"due": now + 60, "channel_id": 4242}), now)
    assert (due, cid, origin) == (now + 60, 4242, {})
    # 上限是含的，與解析器一致。
    assert b._parse_scheduled_run_record(json.dumps(
        {"due": now + b.SCHEDULE_HORIZON_SEC, "channel_id": 1}), now)[0] == (
            now + b.SCHEDULE_HORIZON_SEC)
    with pytest.raises(ValueError):
        b._parse_scheduled_run_record(json.dumps(
            {"due": now + b.SCHEDULE_HORIZON_SEC + 1, "channel_id": 1}), now)


def test_a_negative_channel_id_is_valid_only_with_a_platform_origin():
    """別的平台的私訊在這個 bot 裡是負數號碼。帶著來源的紀錄要收（找回對話靠來源），沒有
    來源的負數照舊是壞資料——少了後半，這條放寬就變成「任何負數都收」。"""
    now = 1_789_000_000.0
    origin = {"platform": "stubplat", "platform_chat_id": "777"}
    got = b._parse_scheduled_run_record(
        json.dumps({"due": now + 60, "channel_id": -5, **origin}), now)
    assert got == (now + 60, -5, origin)
    for bad in ({}, {"platform": "", "platform_chat_id": "777"},
                {"platform": "stubplat", "platform_chat_id": "  "},
                {"platform": "stubplat", "platform_chat_id": 777}):
        with pytest.raises(ValueError):
            b._parse_scheduled_run_record(
                json.dumps({"due": now + 60, "channel_id": -5, **bad}), now)
    with pytest.raises(ValueError):
        b._parse_scheduled_run_record(json.dumps(
            {"due": now + 60, "channel_id": True, **origin}), now)


def test_a_run_scheduled_on_another_platform_is_restored_there(_sr_env, monkeypatch):
    """重啟後讀回：經那個平台找回對話，「仍然有效」講在那裡；既有平台一個字都沒收到。
    找不回來（平台沒開）就保留落地檔、只寫 log，**不**退回既有平台的設定頻道。"""
    import _chat_platform as cp
    delivered: list = []

    class _Platform(cp.ChatTransport):
        name = "stubplat"

        @property
        def capabilities(self):
            return cp.PlatformCapabilities()

        async def run(self):
            return None

        async def deliver(self, channel, content=None, **kwargs):
            delivered.append(content)

        def conversation_for(self, platform_chat_id):
            if platform_chat_id != "777":
                return None
            return cp.ChatConversation(self, "777", uid=-5, is_direct=True,
                                       is_command_chat=False)

    channels = _sr_client(monkeypatch, known=(4242, b.CHANNEL_ID))
    due = time.time() + 3600
    record = {"due": due, "channel_id": -5, "platform": "stubplat",
              "platform_chat_id": "777"}

    monkeypatch.setattr(b, "_chat_transports", [])
    _sr_env.file.write_text(json.dumps(record), encoding="utf-8")
    _sr_run(b._restore_scheduled_run_once())
    assert b._scheduled_run_task is None and _sr_env.file.exists()
    assert delivered == [] and all(not ch.sent for ch in channels.values())

    monkeypatch.setattr(b, "_scheduled_run_restored", False)
    monkeypatch.setattr(b, "_chat_transports", [_Platform()])

    async def _body():
        await b._restore_scheduled_run_once()
        task = b._scheduled_run_task
        assert task is not None, "沒有重新排上"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _sr_run(_body())
    assert len(delivered) == 1 and "仍然有效" in delivered[0], delivered
    assert all(not ch.sent for ch in channels.values()), channels


def test_run_in_from_another_platform_records_its_conversation(_sr_env, monkeypatch):
    """`/run in` 從別的平台下：落地檔要帶上那個對話的來源，重啟後才找得回去。"""
    import _chat_platform as cp

    class _Platform(cp.ChatTransport):
        name = "stubplat"

        @property
        def capabilities(self):
            return cp.PlatformCapabilities()

        async def run(self):
            return None

        async def deliver(self, channel, content=None, **kwargs):
            return None

    conv = cp.ChatConversation(_Platform(), "777", uid=-5, is_direct=True,
                               is_command_chat=False)
    message = types.SimpleNamespace(channel=conv, author=types.SimpleNamespace(id=1))

    async def _body():
        await b.cmd_run(message, "in 90m")
        task = b._scheduled_run_task
        record = json.loads(_sr_env.file.read_text(encoding="utf-8"))
        task.cancel()
        await _sr_settle(task)
        return record

    record = _sr_run(_body())
    assert (record["platform"], record["platform_chat_id"], record["channel_id"]) == (
        "stubplat", "777", -5), record


def test_scheduling_a_run_from_another_platform_stores_where_it_came_from(
        monkeypatch, tmp_path):
    target = tmp_path / "scheduled_run.json"
    monkeypatch.setattr(b, "SCHEDULED_RUN_FILE", target)
    origin = {"platform": "stubplat", "platform_chat_id": "777"}
    assert b._save_scheduled_run(1_900_000_000.0, -5, origin) is True
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert stored == {"due": 1_900_000_000.0, "channel_id": -5, **origin}
    assert b._save_scheduled_run(1_900_000_000.0, 4242) is True
    assert set(json.loads(target.read_text(encoding="utf-8"))) == {"due", "channel_id"}


def test_restore_runs_only_once_per_process(_sr_env, monkeypatch):
    """`on_ready` 在完整 re-identify 時會重跑；第二次不得再還原一次。"""
    channels = _sr_client(monkeypatch, known=(4242,))

    async def _body():
        await b._restore_scheduled_run_once()          # 沒有檔案：什麼都不做
        _sr_write(_sr_env, time.time() - b.SCHEDULED_RUN_CATCHUP_SEC - 30)
        await b._restore_scheduled_run_once()

    _sr_run(_body())
    assert _sr_env.file.exists(), "第二次呼叫又還原了一次"
    assert not channels[4242].sent


def test_restore_leaves_a_newer_in_memory_schedule_alone(_sr_env, monkeypatch):
    """這個行程裡已經有人排了新的：它寫檔時已經蓋掉舊的，還原不得再排一份。"""
    channels = _sr_client(monkeypatch, known=(4242,))
    message, _channel = _sr_message(4242)

    async def _body():
        await b.cmd_run(message, "in 90m")
        task = b._scheduled_run_task
        await b._restore_scheduled_run_once()
        same = b._scheduled_run_task is task
        task.cancel()
        await _sr_settle(task)
        return same

    assert _sr_run(_body()), "還原把新排程換掉了"
    assert not channels[4242].sent
    assert _sr_env.file.exists()


def test_restore_falls_back_to_the_configured_channel(_sr_env, monkeypatch):
    """原頻道查不到（快取沒有、抓也抓不到）→ 退回設定的頻道說話。"""
    channels = _sr_client(monkeypatch, known=(b.CHANNEL_ID,))
    _sr_write(_sr_env, time.time() - b.SCHEDULED_RUN_CATCHUP_SEC - 30,
              channel_id=987654)

    _sr_run(b._restore_scheduled_run_once())
    sent = channels[b.CHANNEL_ID].sent
    assert len(sent) == 1 and "錯過" in sent[0], sent
    assert not _sr_env.file.exists()


def test_restore_uses_a_fetched_channel_before_falling_back(_sr_env, monkeypatch):
    """快取裡沒有、但抓得到（例如私訊頻道）→ 用抓到的那個，不退回設定頻道。"""
    channels = _sr_client(monkeypatch, known=(b.CHANNEL_ID,), fetchable=(555,))
    _sr_write(_sr_env, time.time() - b.SCHEDULED_RUN_CATCHUP_SEC - 30,
              channel_id=555)

    _sr_run(b._restore_scheduled_run_once())
    assert len(channels[555].sent) == 1, channels[555].sent
    assert not channels[b.CHANNEL_ID].sent


def test_restore_keeps_the_file_when_no_channel_can_be_reached(_sr_env, monkeypatch):
    """一個能說話的頻道都沒有 → 保留檔案（刪了就是靜默消失），不往上炸。"""
    _sr_client(monkeypatch, known=(), fetch_error=discord.HTTPException(
        types.SimpleNamespace(status=503, reason="down"), "unavailable"))
    _sr_write(_sr_env, time.time() - b.SCHEDULED_RUN_CATCHUP_SEC - 30)

    _sr_run(b._restore_scheduled_run_once())
    assert _sr_env.file.exists()
    assert b._scheduled_run_task is None
    assert _sr_env.runs == []


def test_a_schedule_whose_wait_fails_is_cancelled_and_its_file_removed(_sr_env):
    """等待本身就失敗（壞時間戳繞過了解析器時，`asyncio.sleep(nan)` 丟 ValueError）
    → 排程取消、提醒擁有者，而且落地檔要一起刪——否則重啟又把這個壞排程接回來。

    這條路只能靠這一支走到：一般的啟動失敗發生在到點之後，到點那一步已經刪過檔了。"""
    _sr_env.file.write_text(json.dumps({"due": time.time() + 600,
                                        "channel_id": 1}), encoding="utf-8")
    channel = _SrChannel(1)

    async def _body():
        task = asyncio.create_task(b._scheduled_run_loop(channel, math.nan))
        b._scheduled_run_task = task      # 讓它是「目前的排程」
        await _sr_settle(task)

    _sr_run(_body())
    assert _sr_env.runs == [], "等待失敗還是啟動了"
    assert channel.sent and "沒有順利開始" in channel.sent[-1], channel.sent
    assert not _sr_env.file.exists(), "取消的排程沒有刪檔"


def test_on_ready_schedules_the_scheduled_run_restore():
    """還原函式要真的從 `on_ready` 排出去；少了這一行，其餘測試全綠而功能不存在。"""
    import ast as _ast

    tree = _ast.parse((Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py")
                      .read_text(encoding="utf-8"))
    on_ready = [n for n in tree.body if isinstance(n, _ast.AsyncFunctionDef)
                and n.name == "on_ready"]
    assert len(on_ready) == 1, "找不到 on_ready"
    called = {n.func.id for n in _ast.walk(on_ready[0])
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)}
    assert "_ensure_background_tasks_alive" in called, "擷取器沒讀到 on_ready 的呼叫"
    assert "_restore_scheduled_run_once" in called, (
        "on_ready 沒有排出延後啟動的還原——重啟之後排程就安靜消失了")


# ---------------------------------------------------------------------------
# `/dorossi workspace_clean <days>`：非有限天數一律回用法說明
#
# 原本是 `max(0.0, float(...))`，而 `max(0.0, nan)` 回 `0.0`——打錯的 `nan` 變成
# 截止時間＝現在，所有沒在跑的工作目錄全部刪掉；`-inf` 同樣夾成 0。工作目錄根一律
# 導到 tmp_path：這個 repo 的 `dorossi_workspace/` 是正在用的工作區。
# ---------------------------------------------------------------------------
@pytest.fixture
def _wc_env(monkeypatch, tmp_path):
    """工作目錄根、session 狀態檔、落地佇列、四份「正在用」的記憶體結構，全部導到
    tmp_path 或換成空的新物件。**這個 repo 的 `dorossi_workspace/` 是正在用的工作區**
    （跑這個測試的工作階段本身就在裡面），所以唯一的根是 tmp_path——連「把擁有者閘
    拿掉」那個變異跑起來，能刪到的也只有這裡。"""
    root = tmp_path / "ws"
    sessions = root / "sessions"
    now = time.time()
    old = now - 90 * 86400

    def make(leaf: str, *, mtime: float = old) -> Path:
        path = sessions / leaf
        path.mkdir(parents=True)
        (path / "note.txt").write_text("x", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    dirs = [make("1_s1"), make("1_s2")]
    replies: list = []

    async def _reply(_message, content=None, **_kw):
        replies.append(content)
        return None

    state_file = tmp_path / "dorossi_session.json"
    queue_file = tmp_path / "dorossi_queue.ndjson"
    failed_file = tmp_path / "dorossi_queue_failed.ndjson"

    def write_state(state: dict) -> None:
        state_file.write_text(json.dumps(state), encoding="utf-8")

    monkeypatch.setattr(b, "DOROSSI_CC_WORKDIR", root)
    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_dorossi_loops", {})
    monkeypatch.setattr(b, "_dorossi_session_locks", {})
    monkeypatch.setattr(b, "_dorossi_session_lock_refs", {})
    monkeypatch.setattr(b, "_dorossi_waiters", {})
    monkeypatch.setattr(b, "DOROSSI_QUEUE_FILE", queue_file)
    # 失敗佇列也是「正在用」的訊號（2026-09-21）；不導開的話，測試會讀到正式的那一份。
    monkeypatch.setattr(b, "DOROSSI_FAILED_QUEUE_FILE", failed_file)
    monkeypatch.setattr(b, "_dorossi_workspace_purging", set())
    monkeypatch.setattr(db, "DOROSSI_SESSION_FILE", state_file)
    # 導開的是 `db` 的常數，所以 bot 呼叫的載入器必須就是 `db` 那一支——否則上面那行
    # 導的是一份沒人讀的常數，測試會安靜地讀到正式的狀態檔。
    assert b._dorossi_load_state is db._dorossi_load_state
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.DOROSSI_USER_ID))
    return types.SimpleNamespace(
        dirs=dirs, replies=replies, message=message, root=root, sessions=sessions,
        now=now, old=old, make=make, write_state=write_state,
        queue_file=queue_file, failed_file=failed_file, tmp=tmp_path)


@pytest.mark.parametrize("rest", ("nan", "inf", "-inf", "1e400", "-nan",
                                  "dry nan", "dry -inf"))
def test_workspace_clean_rejects_non_finite_days(_wc_env, rest):
    _sr_run(b.mcmd_workspace_clean(_wc_env.message, rest))
    assert all(path.exists() for path in _wc_env.dirs), (
        f"`{rest}` 刪了工作目錄——非有限天數被夾成 0，等於「全部清掉」")
    assert _wc_env.replies == ["用法：`/dorossi workspace_clean [dry] [days]`"], (
        _wc_env.replies)


@pytest.mark.parametrize("rest", ("abc", "dry xyz"))
def test_workspace_clean_rejects_a_non_number(_wc_env, rest):
    """解析不了的天數照舊回用法說明（`float()` 丟 ValueError 那條路）。"""
    _sr_run(b.mcmd_workspace_clean(_wc_env.message, rest))
    assert all(path.exists() for path in _wc_env.dirs)
    assert _wc_env.replies == ["用法：`/dorossi workspace_clean [dry] [days]`"], (
        _wc_env.replies)


def test_workspace_clean_still_removes_old_dirs_for_a_real_age(_wc_env):
    """正面對照：夾具裡的目錄對合法天數是真的會被刪的，所以上面那支的「沒刪」有意義。"""
    _sr_run(b.mcmd_workspace_clean(_wc_env.message, "30"))
    assert not any(path.exists() for path in _wc_env.dirs), _wc_env.replies
    assert "已清理 2/2" in _wc_env.replies[-1], _wc_env.replies


# ---------------------------------------------------------------------------
# `/dorossi workspace_clean`：正在用、等著接續、最近用過的工作目錄都不可以刪（2026-09-21）
#
# 原本只跳過有自走迴圈的目錄、只看目錄本身的 mtime。一般回合正在跑或排隊、留有
# `loop_pending`、以及「頂層 mtime 很舊但 `last_used` 是剛剛」的目錄都會被刪。而且
# 那兩道既有的 `continue`（連結指到外面、自走迴圈在跑）整套測試從來沒有執行過。
#
# 每一個訊號都**單獨**擺一格：兩個訊號同時成立時，拿掉任何一個都還有另一個擋著，
# 那種變異測不出來。所以「正在用」的每一格都把時間戳設成舊的，「最近用過」的每一格
# 都不碰記憶體結構。
# ---------------------------------------------------------------------------
def _wc_session(env, **fields) -> dict:
    """一個 slot：預設兩個時間戳都是舊的（不會因為「最近用過」而被留下）。"""
    sess = {"created_at": env.old, "last_used": env.old}
    sess.update(fields)
    return sess


def _wc_state(sessions: dict, uid: str = "1") -> dict:
    return {uid: {"active": next(iter(sessions), None), "next_seq": 99,
                  "sessions": sessions}}


def _wc_run(env, *, dry: bool, days: str = "30"):
    _sr_run(b.mcmd_workspace_clean(env.message, f"dry {days}" if dry else days))


def _wc_size_text(n_leaves: int) -> str:
    """每個夾具目錄裡只有一個 1 位元組的檔。"""
    return b._fmt_size(n_leaves)


def _wc_make_link(link: Path, target: Path) -> str:
    """在 `link` 建一個指到 `target` 的目錄連結；回用了哪一種。

    Windows 上先用 junction（`_winapi.CreateJunction`，一般使用者就能建），不行才退
    符號連結（需要開發人員模式或權限）；兩種都建不出來才跳過。"""
    try:
        import _winapi  # noqa: PLC0415  只有 Windows 有

        _winapi.CreateJunction(str(target), str(link))
        kind = "junction"
    except (ImportError, AttributeError, OSError):
        try:
            os.symlink(target, link, target_is_directory=True)
            kind = "symlink"
        except (OSError, NotImplementedError) as error:
            pytest.skip(f"cannot create a directory link here: {error!r}")
    assert link.resolve() == target.resolve(), (kind, link.resolve(), target)
    return kind


@pytest.mark.parametrize("dry", (True, False), ids=("dry", "real"))
@pytest.mark.parametrize("signal", ("lock_ref", "locked_lock", "waiter",
                                    "persisted_queue", "failed_queue", "live_loop",
                                    "real_turn"))
def test_workspace_clean_skips_a_session_that_is_in_use(_wc_env, signal, dry):
    """`1_s1` 正在用（每一格只有一個訊號）、`1_s2` 閒著；兩個都一樣舊、都不在 state 裡。

    `live_loop` 那一格釘的是原本就有的自走迴圈跳過——它在這之前從來沒被執行過。
    `real_turn` 走真的取鎖函式、在持有鎖的期間下指令，也就是「一個回合正在跑」的原樣。
    `failed_queue` 那一格：失敗佇列裡的列是擁有者可以 `/dorossi queue retry_failed`
    重跑的回合，工作目錄被清掉的話重跑的回合會拿到空目錄（2026-09-21 補）。
    """
    key = ("1", "s1")
    in_use, idle = _wc_env.dirs

    async def _body():
        if signal == "lock_ref":
            b._dorossi_session_lock_refs[key] = 1
        elif signal == "locked_lock":
            lock = asyncio.Lock()
            await lock.acquire()
            b._dorossi_session_locks[key] = lock      # 參照數故意留空
        elif signal == "waiter":
            b._dorossi_waiters[key] = [b._DorossiWaiter(None, "q1")]
        elif signal == "persisted_queue":
            _wc_env.queue_file.write_text(json.dumps(
                {"id": "q1", "uid": "1", "sid": "s1", "prompt": "x"}) + "\n",
                encoding="utf-8")
        elif signal == "failed_queue":
            _wc_env.failed_file.write_text(json.dumps(
                {"id": "q1", "uid": "1", "sid": "s1", "prompt": "x",
                 "failed_at": _wc_env.old, "error": "RuntimeError"}) + "\n",
                encoding="utf-8")
        elif signal == "live_loop":
            b._dorossi_loops[key] = b._DorossiLoopState("1", "s1")
        elif signal == "real_turn":
            lock = b._dorossi_acquire_session_lock(key)
            async with lock:
                await b.mcmd_workspace_clean(
                    _wc_env.message, "dry 30" if dry else "30")
            b._dorossi_release_session_lock(key)
            return
        await b.mcmd_workspace_clean(_wc_env.message, "dry 30" if dry else "30")

    _sr_run(_body())
    note = "（另有 1 個正在使用，已跳過）"
    if dry:
        assert _wc_env.replies == [
            f"將清理 1 個 workspace，約 `{_wc_size_text(1)}`。{note}"], _wc_env.replies
        assert in_use.exists() and idle.exists()
    else:
        assert _wc_env.replies == [
            f"已清理 1/1 個 workspace，約 `{_wc_size_text(1)}`。{note}"], _wc_env.replies
        assert in_use.exists(), f"正在用的工作目錄被刪了（{signal}）"
        assert (in_use / "note.txt").exists()
        assert not idle.exists(), "閒著的那一個沒有被刪——這一格的「沒刪」就沒有意義"


@pytest.mark.parametrize("dry", (True, False), ids=("dry", "real"))
def test_workspace_clean_skips_a_session_waiting_to_resume(_wc_env, dry):
    """`loop_pending` 還在＝這個對話等著自動接續（或人手動 continue）。時間戳全是舊的，
    記憶體裡也沒有任何東西在跑，唯一擋著的就是這個標記。"""
    _wc_env.write_state(_wc_state({
        "s1": _wc_session(_wc_env, loop_pending={"task": "x", "live": True}),
        "s2": _wc_session(_wc_env),
    }))
    _wc_run(_wc_env, dry=dry)
    kept, gone = _wc_env.dirs
    note = "（另有 1 個正在使用，已跳過）"
    verb = "將清理 1 個" if dry else "已清理 1/1 個"
    assert _wc_env.replies == [
        f"{verb} workspace，約 `{_wc_size_text(1)}`。{note}"], _wc_env.replies
    assert kept.exists()
    assert gone.exists() == dry


@pytest.mark.parametrize("fields, kept", [
    pytest.param({"last_used": "recent"}, True, id="old_mtime_recent_last_used"),
    pytest.param({"last_used": None, "created_at": "recent"}, True,
                 id="no_last_used_falls_back_to_created_at"),
    pytest.param({"last_used": 0, "created_at": "recent"}, True,
                 id="zero_last_used_falls_back"),
    pytest.param({"last_used": True, "created_at": "recent"}, True,
                 id="bool_last_used_is_not_a_time"),
    pytest.param({"last_used": "garbage", "created_at": "recent"}, True,
                 id="string_last_used_is_not_a_time"),
    pytest.param({"last_used": math.inf}, False, id="infinite_last_used_is_not_a_time"),
    pytest.param({}, False, id="old_both_is_removed"),
])
def test_workspace_clean_uses_the_sessions_last_use_as_well_as_the_mtime(
        _wc_env, fields, kept):
    """目錄 mtime 只在直接子項變動時才更新；在子資料夾裡工作的對話，頂層 mtime 會一直
    很舊。state 的 `last_used`（退回 `created_at`）任一個說最近用過就要留下。

    不是時間的值（bool、字串、0、無限大）一律當作沒有、退回下一個欄位——bool 是 int
    的子類別，`True` 會變成 1970 年的第 1 秒；無限大這一格的 `created_at` 是舊的，所以
    「當作沒有」的結果是刪掉，把它當成時間的結果則是永遠留著。"""
    recent = _wc_env.now - 3600
    sess = _wc_session(_wc_env)
    for name, value in fields.items():
        if value is None:
            sess.pop(name, None)
        else:
            sess[name] = recent if value == "recent" else value
    _wc_env.write_state(_wc_state({"s1": sess}))
    _wc_run(_wc_env, dry=False)
    target, other = _wc_env.dirs
    assert target.exists() == kept, (fields, _wc_env.replies)
    assert not other.exists(), "不在 state 裡的舊目錄照舊要被刪"
    removed = 1 if kept else 2
    assert _wc_env.replies == [
        f"已清理 {removed}/{removed} 個 workspace，約 `{_wc_size_text(removed)}`。"], (
        _wc_env.replies)


def test_workspace_clean_without_a_state_entry_is_mtime_only(_wc_env):
    """對應不到任何對話的目錄照舊只看 mtime：舊的刪、新的留（也是「或」不是「且」的
    另一半——新目錄沒有 state 可以救它，只有 mtime）。"""
    fresh = _wc_env.make("1_s3", mtime=_wc_env.now - 60)
    _wc_env.write_state(_wc_state({"s9": _wc_session(_wc_env)}))
    _wc_run(_wc_env, dry=False)
    assert fresh.exists()
    assert not any(path.exists() for path in _wc_env.dirs), _wc_env.replies


@pytest.mark.parametrize("field", ("cc_cwd", "cc_workdir"))
@pytest.mark.parametrize("why", ("recent", "live"))
def test_workspace_clean_follows_the_directory_a_session_actually_runs_in(
        _wc_env, field, why):
    """`s9` 用 `/new <路徑>` 在舊的 `1_s1` 目錄裡工作（`1_s1` 自己已經不在 state 裡）。
    那個目錄的「最近用過／正在用」要跟著實際在裡面跑的 `s9` 走，不是只看目錄名。"""
    target, other = _wc_env.dirs
    sess = _wc_session(_wc_env, **{field: str(target)})
    if why == "recent":
        sess["last_used"] = _wc_env.now - 60
    else:
        b._dorossi_session_lock_refs[("1", "s9")] = 1
    _wc_env.write_state(_wc_state({"s9": sess}))
    _wc_run(_wc_env, dry=False)
    assert target.exists(), (field, why, _wc_env.replies)
    assert not other.exists()
    note = "（另有 1 個正在使用，已跳過）" if why == "live" else ""
    assert _wc_env.replies == [
        f"已清理 1/1 個 workspace，約 `{_wc_size_text(1)}`。{note}"], _wc_env.replies


def test_workspace_clean_ignores_a_session_directory_outside_the_root(_wc_env):
    """反方向：slot 記的工作目錄在 sessions 根外面，就不影響任何一個 sessions 子目錄。"""
    outside = _wc_env.tmp / "elsewhere"
    outside.mkdir()
    b._dorossi_session_lock_refs[("1", "s9")] = 1
    _wc_env.write_state(_wc_state({"s9": _wc_session(
        _wc_env, cc_cwd=str(outside), last_used=_wc_env.now)}))
    _wc_run(_wc_env, dry=False)
    assert not any(path.exists() for path in _wc_env.dirs), _wc_env.replies
    assert outside.exists()


def test_workspace_leaf_maps_only_paths_inside_a_sessions_child(tmp_path):
    """`_dorossi_workspace_leaf` 的契約：落在某個直接子目錄之內（含它本身）→ 那個名字；
    根本身、根外面、不同磁碟機 → None。純字串，路徑不必存在。"""
    root = tmp_path / "ws" / "sessions"
    key = os.path.normcase(str(root))
    leaf = b._dorossi_workspace_leaf
    assert leaf(str(root / "1_s6"), key) == os.path.normcase("1_s6")
    assert leaf(str(root / "1_s6" / "deep" / "er"), key) == os.path.normcase("1_s6")
    assert leaf(str(root), key) is None
    assert leaf(str(root.parent), key) is None
    assert leaf(str(root.parent / "sessionsX" / "1_s6"), key) is None
    assert leaf(str(tmp_path / "elsewhere"), key) is None
    for bad in ("", "   ", None, 7, ["x"]):
        assert leaf(bad, key) is None, bad
    if os.name == "nt":
        other = "Z:\\" if not key.startswith("z:") else "Y:\\"
        assert leaf(other + "1_s6", key) is None


@pytest.mark.parametrize("dry", (True, False), ids=("dry", "real"))
def test_workspace_clean_never_follows_a_link_out_of_the_root(_wc_env, dry):
    """一個子項是指到 sessions 根外面的目錄連結：不列入、不刪，連結的目標也不能被動到。

    沒有這道閘時連結會被算進清單（dry-run 數字多一個、`_dir_size` 走進目標去量），
    真的刪的那一步則是 `shutil.rmtree` 自己拒絕連結——所以斷言看的是回覆的數字，
    不是只看目標還在不在（目標在兩種情況下都還在）。"""
    outside = _wc_env.tmp / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("precious", encoding="utf-8")
    os.utime(outside, (_wc_env.old, _wc_env.old))
    link = _wc_env.sessions / "1_s5"
    kind = _wc_make_link(link, outside)
    _wc_run(_wc_env, dry=dry)
    verb = "將清理 2 個" if dry else "已清理 2/2 個"
    assert _wc_env.replies == [
        f"{verb} workspace，約 `{_wc_size_text(2)}`。"], (kind, _wc_env.replies)
    assert os.path.lexists(link), f"{kind} 本身被刪了"
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "precious"
    assert all(path.exists() for path in _wc_env.dirs) == dry


def test_workspace_clean_leaves_a_link_inside_the_root_where_it_is(_wc_env):
    """sessions 根裡一個指到**根裡面**的目錄連結（這裡指到正在用的 `1_s1`）。連結的名字
    不是任何對話的，而 `child.stat()` 跟著連結讀到目標的舊 mtime，所以清單會列它。

    改名那一步必須跳過連結：`os.rename` 搬走的是連結本身，搬過去的墓碑 `shutil.rmtree`
    又拒絕刪（連結），於是原本那個名字上的連結不見了、換來一個每次清理都刪不掉的墓碑。
    舊的同步寫法在這裡的結果是「那一個沒清掉、連結原地不動」，改名之後要照舊。
    目標裡的東西兩種情況都不會被刪——所以斷言看的是連結還在不在、有沒有多出墓碑。"""
    in_use, idle = _wc_env.dirs
    link = _wc_env.sessions / "1_s5"
    kind = _wc_make_link(link, in_use)
    b._dorossi_session_lock_refs[("1", "s1")] = 1
    _wc_run(_wc_env, dry=False)
    assert os.path.lexists(link), f"{kind} 被搬走了"
    assert _wc_tombstones(_wc_env) == [], f"{kind} 被改名成一個刪不掉的墓碑"
    assert (in_use / "note.txt").exists()
    assert not idle.exists()
    # 清單兩個（閒著的那一個 ＋ 連結），刪掉一個；大小是清單上兩個的總和（連結量到的是
    # 目標裡的 1 位元組），跟原本「刪之前把整份清單量一遍」同一個數字。
    assert _wc_env.replies == [
        f"已清理 1/2 個 workspace，約 `{_wc_size_text(2)}`。"
        "（另有 1 個正在使用，已跳過）"], (kind, _wc_env.replies)


@pytest.mark.parametrize("dry", (True, False), ids=("dry", "real"))
def test_workspace_clean_skips_a_plain_file(_wc_env, dry):
    stray = _wc_env.sessions / "1_s8"
    stray.write_text("not a workspace", encoding="utf-8")
    os.utime(stray, (_wc_env.old, _wc_env.old))
    _wc_run(_wc_env, dry=dry)
    verb = "將清理 2 個" if dry else "已清理 2/2 個"
    assert _wc_env.replies == [
        f"{verb} workspace，約 `{_wc_size_text(2)}`。"], _wc_env.replies
    assert stray.read_text(encoding="utf-8") == "not a workspace"


def test_workspace_clean_refuses_a_non_owner_and_deletes_nothing(_wc_env):
    _wc_env.message.author.id = b.DOROSSI_USER_ID + 1
    _wc_run(_wc_env, dry=False, days="0")
    assert _wc_env.replies == ["此指令僅限擁有者使用。"]
    assert all((path / "note.txt").exists() for path in _wc_env.dirs)


def test_workspace_clean_survives_a_sessions_root_it_cannot_list(_wc_env):
    """`sessions` 存在卻列不出來（這裡是一個檔）：原本 `iterdir()` 在 try 外面，例外
    直接往上拋——而 mention 那一面沒有外層保護。現在回「0 個」、什麼都不刪。"""
    for path in _wc_env.dirs:
        (path / "note.txt").unlink()
        path.rmdir()
    _wc_env.sessions.rmdir()
    _wc_env.sessions.write_text("x", encoding="utf-8")
    _wc_run(_wc_env, dry=False)
    assert _wc_env.replies == [f"已清理 0/0 個 workspace，約 `{_wc_size_text(0)}`。"]
    assert _wc_env.sessions.read_text(encoding="utf-8") == "x"


def test_workspace_clean_dry_run_and_real_run_agree(_wc_env):
    """同一個狀態先 dry 再正式：數字要一樣，刪掉的正好是 dry 說要刪的那幾個。"""
    busy = _wc_env.make("1_s3")
    recent = _wc_env.make("1_s4")
    b._dorossi_session_lock_refs[("1", "s3")] = 1
    _wc_env.write_state(_wc_state({"s4": _wc_session(_wc_env, last_used=_wc_env.now)}))
    note = "（另有 1 個正在使用，已跳過）"
    _wc_run(_wc_env, dry=True)
    _wc_run(_wc_env, dry=False)
    assert _wc_env.replies == [
        f"將清理 2 個 workspace，約 `{_wc_size_text(2)}`。{note}",
        f"已清理 2/2 個 workspace，約 `{_wc_size_text(2)}`。{note}",
    ]
    assert busy.exists() and recent.exists()
    assert not any(path.exists() for path in _wc_env.dirs)


def _wc_yields_between_plan_and_retire(fn) -> list[int]:
    """`mcmd_workspace_clean` 的正式執行那條路上，從「算出清單」到「改名迴圈結束」
    之間的讓出點行號（await／async for／async with）。dry-run 那個以 `return` 結尾的
    分支不算——它讀完清單就回覆、不刪東西。

    改名迴圈 ＝ 呼叫 `_dorossi_workspace_retire` 的那個 `for`。改名是不可回頭的那一步；
    它之後的 await（執行緒裡刪墓碑）不在這個範圍裡——墓碑名不是任何對話推得出來的
    路徑，刪它不會跟回合碰到同一棵樹。"""
    import ast as _ast

    body = fn.body
    plan = [i for i, stmt in enumerate(body)
            if any(isinstance(n, _ast.Name) and n.id == "_dorossi_workspace_clean_plan"
                   for n in _ast.walk(stmt))]
    loop = [i for i, stmt in enumerate(body)
            if isinstance(stmt, _ast.For)
            and any(isinstance(n, _ast.Name) and n.id == "_dorossi_workspace_retire"
                    for n in _ast.walk(stmt))]
    assert len(plan) == 1 and len(loop) == 1 and plan[0] < loop[0], (plan, loop)
    lines = []
    for stmt in body[plan[0]:loop[0] + 1]:
        if (isinstance(stmt, _ast.If) and isinstance(stmt.test, _ast.Name)
                and stmt.test.id == "dry_run"
                and isinstance(stmt.body[-1], _ast.Return)):
            continue
        lines += [n.lineno for n in _ast.walk(stmt)
                  if isinstance(n, (_ast.Await, _ast.AsyncFor, _ast.AsyncWith))]
    return lines


_WC_DELETE_CALLS = frozenset({"rmtree", "remove", "unlink", "rmdir", "removedirs"})


def _wc_direct_deletes(fn) -> list[int]:
    """`mcmd_workspace_clean` 本體裡直接刪東西的呼叫行號。刪除只准經過
    `_dorossi_workspace_purge`（只刪墓碑、在執行緒裡跑）。"""
    import ast as _ast

    return [n.lineno for n in _ast.walk(fn)
            if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
            and n.func.attr in _WC_DELETE_CALLS]


def _wc_mcmd(source: str):
    import ast as _ast

    tree = _ast.parse(source)
    found = [n for n in tree.body if isinstance(n, _ast.AsyncFunctionDef)
             and n.name == "mcmd_workspace_clean"]
    assert len(found) == 1
    return found[0]


def test_workspace_clean_does_not_yield_between_the_plan_and_the_retire():
    """「沒有在用」是那一刻的快照。中間只要讓出一次事件迴圈，一個新回合就可能取得
    那個對話的鎖、在剛判定為閒置的目錄裡起後端——所以清單與改名之間不得有 await，
    本體也不得自己刪任何東西（要刪只能刪改名後的墓碑，經 `_dorossi_workspace_purge`，
    而且要交給執行緒——那正是這次要把它從事件迴圈上搬走的理由）。

    這支原本叫 `…_between_the_plan_and_the_delete`，那時 rmtree 本身就在事件迴圈上。
    改成「先改名、執行緒裡刪墓碑」之後，要釘的不變式是改名那一步，不是刪除。"""
    import ast as _ast

    real = _wc_mcmd((Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py")
                    .read_text(encoding="utf-8"))
    assert _wc_yields_between_plan_and_retire(real) == []
    assert _wc_direct_deletes(real) == []
    # 正面對照：函式裡確實有 await（最後那次回覆），擷取器不是什麼都看不到。
    assert any(isinstance(n, _ast.Await) for n in _ast.walk(real))
    handed = [n for n in _ast.walk(real)
              if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
              and n.func.attr == "to_thread" and n.args
              and isinstance(n.args[0], _ast.Name)
              and n.args[0].id == "_dorossi_workspace_purge"]
    assert len(handed) == 1, "刪墓碑沒有交給執行緒——幾 GB 的目錄會卡住心跳"
    synthetic = _wc_mcmd(
        "async def mcmd_workspace_clean(message, rest=''):\n"
        "    candidates, skipped = _dorossi_workspace_clean_plan(r, c, s, k)\n"
        "    if dry_run:\n"
        "        await safe_reply(message, 'x')\n"
        "        return\n"
        "    await asyncio.to_thread(print)\n"
        "    for path in candidates:\n"
        "        tomb = _dorossi_workspace_retire(path)\n"
        "    _shutil.rmtree(tomb)\n"
        "    await safe_reply(message, 'y')\n")
    assert _wc_yields_between_plan_and_retire(synthetic) == [6]
    assert _wc_direct_deletes(synthetic) == [9]


def _wc_tombstones(env) -> list[str]:
    return sorted(p.name for p in env.sessions.iterdir()
                  if p.name.startswith(b._DOROSSI_WORKSPACE_TOMBSTONE))


def test_a_turn_that_starts_during_the_threaded_delete_keeps_its_new_directory(
        _wc_env, monkeypatch):
    """刪除在執行緒裡跑的期間，事件迴圈要照常轉（心跳），而且那段時間裡開始的回合
    不能被波及：它在原路徑上建的新目錄與新檔（後端 spawn 前對受管 cwd 做的就是
    `mkdir(parents=True, exist_ok=True)`）要原封不動，被刪掉的只有舊的內容。

    改名之前就在執行緒裡刪原路徑的寫法，在這裡會把回合剛寫的 `turn.txt` 一起刪掉。"""
    import threading

    started = threading.Event()
    release = threading.Event()
    real_purge = b._dorossi_workspace_purge

    def _slow_purge(*args, **kwargs):
        started.set()
        assert release.wait(10), "測試沒有放行執行緒"
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(b, "_dorossi_workspace_purge", _slow_purge)
    leaf = _wc_env.dirs[0]
    ticks = 0

    async def _body():
        nonlocal ticks
        task = asyncio.create_task(b.mcmd_workspace_clean(_wc_env.message, "30"))
        for _ in range(1000):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set(), "刪除沒有進到執行緒"
        # 執行緒卡在刪除裡的這段時間，事件迴圈要還在轉。
        for _ in range(5):
            await asyncio.sleep(0)
            ticks += 1
        assert not leaf.exists(), "原路徑上的目錄在交給執行緒之前就該改名走了"
        leaf.mkdir(parents=True, exist_ok=True)
        (leaf / "turn.txt").write_text("new", encoding="utf-8")
        release.set()
        await asyncio.wait_for(task, 10)

    _sr_run(_body(), timeout=20)
    assert ticks == 5
    assert (leaf / "turn.txt").read_text(encoding="utf-8") == "new", (
        "刪除波及了期間開始的回合")
    assert not (leaf / "note.txt").exists(), "舊的內容沒有被刪"
    assert not _wc_env.dirs[1].exists()
    assert _wc_tombstones(_wc_env) == [], "墓碑沒有刪乾淨"
    assert b._dorossi_workspace_purging == set(), "「正在刪」的登記沒有清掉"
    assert _wc_env.replies == [f"已清理 2/2 個 workspace，約 `{_wc_size_text(2)}`。"]


@pytest.mark.skipif(os.name != "nt", reason="「樹裡有開著的 handle 就改不了名」是 NTFS 的行為")
def test_workspace_clean_leaves_a_directory_something_still_holds_untouched(_wc_env):
    """樹裡有人開著檔（bot 不知道的行程：上一個 bot 留下的後端、後端開的背景殼）時，
    作業系統拒絕改名，整個目錄**原封不動**——包括沒被開著的那個檔。

    直接 rmtree 的寫法在同樣的情況下會刪掉它刪得動的部分（`other.txt`），只在開著的
    那個檔上失敗，留下一個被刪了一半的工作目錄。"""
    held, idle = _wc_env.dirs
    (held / "sub").mkdir()
    (held / "sub" / "other.txt").write_text("keep me", encoding="utf-8")
    os.utime(held, (_wc_env.old, _wc_env.old))
    with open(held / "note.txt", "r", encoding="utf-8"):
        _wc_run(_wc_env, dry=False)
    assert (held / "sub" / "other.txt").read_text(encoding="utf-8") == "keep me"
    assert (held / "note.txt").exists()
    assert not idle.exists()
    assert _wc_tombstones(_wc_env) == []
    assert _wc_env.replies == [f"已清理 1/2 個 workspace，約 `{_wc_size_text(2 + 7)}`。"]


@pytest.mark.parametrize("dry", (True, False), ids=("dry", "real"))
def test_workspace_clean_sweeps_leftover_tombstones_without_counting_them(_wc_env, dry):
    """前一次沒刪完的墓碑（唯讀檔、被短暫開著）由之後的正式執行收掉；它不是工作
    目錄，所以不算進回覆的數字，dry-run 也不碰它。"""
    leftover = _wc_env.sessions / f"{b._DOROSSI_WORKSPACE_TOMBSTONE}deadbeef-1_s7"
    leftover.mkdir()
    (leftover / "junk.bin").write_bytes(b"12345")
    # 舊的 mtime：不靠「太新」被排除，排除它的只能是「墓碑不是工作目錄」那一條。
    os.utime(leftover, (_wc_env.old, _wc_env.old))
    _wc_run(_wc_env, dry=dry)
    verb = "將清理 2 個" if dry else "已清理 2/2 個"
    assert _wc_env.replies == [
        f"{verb} workspace，約 `{_wc_size_text(2)}`。"], _wc_env.replies
    assert leftover.exists() == dry
    assert b._dorossi_workspace_purging == set()


def test_workspace_clean_leaves_a_tombstone_another_cleanup_is_deleting(_wc_env):
    """同時跑的另一次清理正在執行緒裡刪的墓碑（登記在 `_dorossi_workspace_purging`）
    不收第二次；收掉的只有沒人在刪的。"""
    busy = _wc_env.sessions / f"{b._DOROSSI_WORKSPACE_TOMBSTONE}cafe0001-1_s7"
    stale = _wc_env.sessions / f"{b._DOROSSI_WORKSPACE_TOMBSTONE}cafe0002-1_s8"
    for path in (busy, stale):
        path.mkdir()
        (path / "junk.bin").write_bytes(b"x")
    b._dorossi_workspace_purging.add(os.path.normcase(busy.name))
    _wc_run(_wc_env, dry=False)
    assert busy.exists(), "另一次清理正在刪的墓碑被收了第二次"
    assert not stale.exists()
    assert b._dorossi_workspace_purging == {os.path.normcase(busy.name)}, (
        "別人的登記被這一次清掉了")


def test_workspace_clean_leaves_a_tombstone_a_conversation_points_into(_wc_env):
    """`/new <路徑>` 可以指定任意存在的目錄，包括一個沒刪完的墓碑（想把裡面的檔拿回來）。
    那個墓碑之後就不是沒人要的東西：收掉它，執行緒裡的 rmtree 就會跟在裡面跑的回合碰在
    同一棵樹上。

    兩個欄位各擺一格（`cc_cwd` 指進墓碑的子目錄、`cc_workdir` 指墓碑本身），另擺一個
    沒人指著的墓碑當正面對照——它要被收掉，否則「一律不收」也會通過。兩個對話本身
    閒著而且很舊：這條判準刻意不看在不在用。"""
    tomb = b._DOROSSI_WORKSPACE_TOMBSTONE
    by_cwd = _wc_env.sessions / f"{tomb}aaaa0001-1_s7"
    by_workdir = _wc_env.sessions / f"{tomb}aaaa0002-1_s8"
    stale = _wc_env.sessions / f"{tomb}aaaa0003-1_s9"
    for path in (by_cwd, by_workdir, stale):
        (path / "sub").mkdir(parents=True)
        (path / "sub" / "work.txt").write_text("x", encoding="utf-8")
        os.utime(path, (_wc_env.old, _wc_env.old))
    _wc_env.write_state(_wc_state({
        "s5": _wc_session(_wc_env, cc_cwd=str((by_cwd / "sub").resolve())),
        "s6": _wc_session(_wc_env, cc_workdir=str(by_workdir.resolve())),
    }))
    _wc_run(_wc_env, dry=False)
    assert (by_cwd / "sub" / "work.txt").exists(), "收掉了一個對話的 cc_cwd 指進去的墓碑"
    assert (by_workdir / "sub" / "work.txt").exists(), (
        "收掉了一個對話的 cc_workdir 指著的墓碑")
    assert not stale.exists(), "沒人指著的墓碑沒有被收"
    # 墓碑不算進回覆的數字；兩個夾具目錄照常清掉。
    assert _wc_env.replies == [f"已清理 2/2 個 workspace，約 `{_wc_size_text(2)}`。"]
    assert not any(path.exists() for path in _wc_env.dirs)


def test_the_workspace_purge_refuses_anything_that_is_not_a_tombstone(_wc_env, capsys):
    """刪除那一支跑在執行緒裡、跟回合同時進行，它碰原路徑就等於重新打開競態——所以
    名字不是墓碑的一律拒絕，不管呼叫端傳了什麼。"""
    live, _other = _wc_env.dirs
    tomb = _wc_env.sessions / f"{b._DOROSSI_WORKSPACE_TOMBSTONE}0badf00d-1_s9"
    tomb.mkdir()
    (tomb / "junk.bin").write_bytes(b"xyz")
    total, removed = b._dorossi_workspace_purge([live, tomb], [live], [])
    assert (live / "note.txt").exists(), "執行緒刪了一個不是墓碑的目錄"
    assert not tomb.exists()
    assert removed == 1
    assert total == 1 + 3
    assert "refusing to delete 1_s1, not a tombstone" in capsys.readouterr().err


@pytest.mark.parametrize("dry", (True, False), ids=("dry", "real"))
def test_workspace_clean_survives_an_unreadable_failed_queue(_wc_env, capsys, dry):
    """失敗佇列整檔不是 UTF-8：指令不能因此變成一句泛用的內部錯誤——當作沒有列、
    原因只寫 stderr，回覆一字不差。stderr 只印型別與位置：`repr(UnicodeDecodeError)`
    會把正在解碼的位元組（排隊的提問原文）一起印出來。"""
    _wc_env.failed_file.write_bytes(
        b'{"id": "q1", "uid": "1", "sid": "s1", "prompt": "\xff secret-prompt"}\n')
    _wc_run(_wc_env, dry=dry)
    verb = "將清理 2 個" if dry else "已清理 2/2 個"
    assert _wc_env.replies == [
        f"{verb} workspace，約 `{_wc_size_text(2)}`。"], _wc_env.replies
    assert all(path.exists() for path in _wc_env.dirs) == dry
    err = capsys.readouterr().err
    assert "failed restore queue is unreadable" in err, err
    assert "secret-prompt" not in err, err


def test_workspace_clean_reads_the_failed_rows_past_a_garbage_line(_wc_env):
    """逐行壞掉的 JSON 由讀取函式自己略過，後面那一列照樣算數。"""
    _wc_env.failed_file.write_text(
        "not json\n" + json.dumps({"id": "q1", "uid": "1", "sid": "s1"}) + "\n"
        + json.dumps(["not", "a", "row"]) + "\n", encoding="utf-8")
    _wc_run(_wc_env, dry=False)
    kept, gone = _wc_env.dirs
    assert kept.exists() and not gone.exists()
    assert _wc_env.replies == [
        f"已清理 1/1 個 workspace，約 `{_wc_size_text(1)}`。"
        "（另有 1 個正在使用，已跳過）"], _wc_env.replies


# ---------------------------------------------------------------------------
# 按住的鍵放不掉、錄製被截斷：對話回覆要講出來（2026-09-21）
#
# `_gui_control` 那一側的修正讓放不掉的鍵留在登記裡、讓錄製回報截掉與略過了多少；
# 這裡釘的是 bot 這一側有沒有**真的講給人聽**。走的是真的 `_gui_control` 函式，
# 只把最底層的函式庫換成假的，所以兩層任何一層退回去都會紅。
# ---------------------------------------------------------------------------
class _StuckKeyBackend:
    """放開某些鍵會丟例外的假函式庫（只有放開那兩支；按下由測試直接寫登記）。

    `fail_left` 讓某個鍵只失敗前幾次（模擬會自己過去的狀態），`on_fail` 在每次
    失敗時被叫一次（模擬「失敗的同時使用者又按了一次」）。
    """

    def __init__(self):
        self.stuck: set[str] = set()
        self.fail_left: dict[str, int] = {}
        self.on_fail = None
        self.attempts: list[str] = []

    def _release(self, name):
        self.attempts.append(name)
        if name in self.stuck or self.fail_left.get(name, 0) > 0:
            if name in self.fail_left:
                self.fail_left[name] -= 1
            if self.on_fail is not None:
                self.on_fail()
            raise OSError("SendInput returned 0")

    def release_keyboard_key(self, key):
        self._release(key)

    def release_mouse(self, button, x=None, y=None):
        self._release(button)


@pytest.fixture(name="_held_env")
def _held_env_fixture(monkeypatch):
    backend = _StuckKeyBackend()
    monkeypatch.setattr(b._gui, "_AC", backend)
    monkeypatch.setattr(b._gui, "_AC_TRIED", True)
    held: dict = {}
    monkeypatch.setattr(b._gui, "_HELD_INPUTS", held)
    replies: list = []

    async def _reply(_message, content=None, **_kw):
        replies.append(content)

    monkeypatch.setattr(b, "safe_reply", _reply)
    message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))
    return types.SimpleNamespace(backend=backend, held=held, replies=replies,
                                 message=message)


def test_key_clear_says_how_many_keys_it_could_not_release(_held_env):
    """`/input key clear`：放不掉的不算進「已放開」，另外講有幾個、去哪裡看；
    後端恢復之後再下一次就真的放掉，回覆也回到原本那一句。"""
    _held_env.held.update({("key", "shift"): 1.0, ("key", "control"): 2.0,
                           ("mouse", "mouse_left"): 3.0})
    _held_env.backend.stuck = {"control"}
    _sr_run(b.cmd_key(_held_env.message, "clear"))
    assert _held_env.replies == [
        "⌨️ 已放開 2 個按鍵；另有 1 個放不掉，仍列在 `/input key status`"]
    assert list(_held_env.held) == [("key", "control")]

    _held_env.backend.stuck.clear()
    _held_env.replies.clear()
    _sr_run(b.cmd_key(_held_env.message, "clear"))
    assert _held_env.replies == ["⌨️ 已放開 1 個按鍵"]
    assert _held_env.held == {}


def _quiet_panic(monkeypatch) -> None:
    """`/host panic` 除了放開按鍵之外的每一條路都換成「沒事」。"""
    monkeypatch.setattr(b, "_MACRO_RUNNING", None)
    monkeypatch.setattr(b, "_MACRO_RECORDING", None)
    monkeypatch.setattr(b, "_WATCHES", {})
    monkeypatch.setattr(b._gui, "job_stop_all", lambda: 0)
    monkeypatch.setattr(b._gui, "shell_stop_all", lambda: 0)


@pytest.mark.parametrize("stuck, released_line, stuck_line", [
    ({"control"}, "• 已放開 1 個卡住的按鍵／滑鼠鍵",
     "• 另有 1 個按鍵／滑鼠鍵放不掉，仍列在 `/input key status`"),
    # 一個都沒放掉：沒有「已放開」那一行，但放不掉的那一行一定要有，而且不能退回
    # 「目前沒有任何自動化動作在跑」——那句話在鍵還按著的時候是錯的。
    ({"control", "shift"}, None,
     "• 另有 2 個按鍵／滑鼠鍵放不掉，仍列在 `/input key status`"),
    (set(), "• 已放開 2 個卡住的按鍵／滑鼠鍵", None),
])
def test_panic_says_how_many_keys_it_could_not_release(_held_env, monkeypatch,
                                                       stuck, released_line,
                                                       stuck_line):
    _quiet_panic(monkeypatch)
    _held_env.held.update({("key", "shift"): 1.0, ("key", "control"): 2.0})
    _held_env.backend.stuck = set(stuck)
    _sr_run(b.cmd_panic(_held_env.message))
    reply = _held_env.replies[-1]
    lines = reply.splitlines()
    if released_line is None:
        assert not any("已放開" in line for line in lines), reply
    else:
        assert released_line in lines, reply
    if stuck_line is None:
        assert "放不掉" not in reply, reply
    else:
        assert stuck_line in lines, reply
    assert "目前沒有任何自動化動作在跑" not in reply, reply
    assert sorted(name for _kind, name in _held_env.held) == sorted(stuck)


@pytest.fixture(name="_fast_release_timer")
def _fast_release_timer_fixture(monkeypatch):
    monkeypatch.setattr(b._gui, "INPUT_HOLD_MAX_SEC", 0.0)
    monkeypatch.setattr(b._gui, "INPUT_RELEASE_RETRY_SEC", 0.0)
    monkeypatch.setattr(b._gui, "INPUT_RELEASE_RETRIES", 3)


def test_a_failed_auto_release_is_retried_until_it_works(_held_env,
                                                        _fast_release_timer, capsys):
    """逾時自動放開失敗兩次、第三次成功：要真的試到第三次，而且最後登記是空的。"""
    _held_env.held[("key", "w")] = 5.0
    _held_env.backend.fail_left = {"w": 2}
    _sr_run(b._auto_release_input("key", "w", 5.0))
    assert _held_env.backend.attempts == ["w", "w", "w"]
    assert _held_env.held == {}
    assert "auto-released stale key 'w'" in capsys.readouterr().err


def test_an_auto_release_that_keeps_failing_stops_and_stays_visible(
        _held_env, _fast_release_timer, capsys):
    """一直放不掉：試滿 1 ＋ `INPUT_RELEASE_RETRIES` 次就停（不是永遠活著的計時器），
    登記照樣留著、按下時間不變，stderr 講清楚它留在清單裡。"""
    _held_env.held[("key", "w")] = 5.0
    _held_env.backend.stuck = {"w"}
    _sr_run(b._auto_release_input("key", "w", 5.0))
    assert _held_env.backend.attempts == ["w"] * 4
    assert _held_env.held == {("key", "w"): 5.0}
    err = capsys.readouterr().err
    assert "failed 4 time(s); left registered" in err, err
    assert "auto-released" not in err


def test_an_auto_release_stops_retrying_once_the_key_was_pressed_again(
        _held_env, _fast_release_timer, capsys):
    """放開失敗的同時使用者又按了一次：那已經不是這個計時器的那一次按住，不得再試
    ——否則舊計時器會把新的那次按住放掉——也不得寫「一直放不掉、留在清單裡」那一行：
    留在清單裡的是新的那次按住，它有自己的計時器。

    `release_input_if_stale` 自己也會擋掉按下時間不符的那一次，所以只看放開次數的話，
    拿掉計時器裡「已經不是這一次按住」那道檢查照樣綠；分得出來的是 stderr。"""
    _held_env.held[("key", "w")] = 5.0
    _held_env.backend.stuck = {"w"}
    _held_env.backend.on_fail = lambda: _held_env.held.__setitem__(("key", "w"), 9.0)
    _sr_run(b._auto_release_input("key", "w", 5.0))
    assert _held_env.backend.attempts == ["w"]
    assert _held_env.held == {("key", "w"): 9.0}
    assert "left registered" not in capsys.readouterr().err


def _recorded_clicks(count, *, bogus_after=False):
    """每秒一次點選（按下即放）；`bogus_after` 在最後補一次重播不了的點選。"""
    events = []
    for index in range(count):
        for kind in ("mdown", "mup"):
            events.append({"kind": kind, "t": float(index), "button": "left",
                           "x": index * 20, "y": 0})
    if bogus_after:
        for kind in ("mdown", "mup"):
            events.append({"kind": kind, "t": 99.0, "button": "bogus",
                           "x": 500, "y": 500})
    return events


@pytest.fixture(name="_record_env")
def _record_env_fixture(monkeypatch):
    """錄製的停止路徑：事件由測試給、存檔換成記錄器、步數上限縮成 4。"""
    saved: list = []
    replies: list = []

    async def _reply(_message, content=None, **_kw):
        replies.append(content)

    def _events(events):
        monkeypatch.setattr(b._gui, "record_stop", lambda: list(events))

    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b._gui, "save_macro",
                        lambda name, steps, **_kw: saved.append((name, list(steps))))
    monkeypatch.setattr(b._gui, "_record_char_table", lambda: {})
    monkeypatch.setattr(b._gui, "MACRO_MAX_STEPS", 4)
    monkeypatch.setattr(b, "_MACRO_RECORDING", "rec1")
    message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))
    return types.SimpleNamespace(saved=saved, replies=replies, events=_events,
                                 message=message)


def test_stopping_a_recording_says_what_the_step_cap_cut(_record_env):
    """五次點選 ＋ 一次重播不了的，上限 4 步：存了 3 步，回覆要講後面 3 個動作沒存到、
    另有 1 步略過——修正前只回「已存成巨集（3 步）」。"""
    _record_env.events(_recorded_clicks(5, bogus_after=True))
    _sr_run(b._macro_record(_record_env.message, "stop"))
    assert _record_env.saved == [("rec1", ["click 0 0", "wait 1", "click 20 0"])]
    assert _record_env.replies == [
        "⏹️ 錄製結束，已存成巨集 `rec1`（3 步）。\n"
        "超過 4 步上限，後面還有 3 個動作沒有存到。\n"
        "另有 1 步無法重播，已略過。\n"
        "用 `/macro show rec1` 檢查內容、`/macro run rec1` 重播。"]


def test_stopping_a_recording_that_lost_nothing_reads_as_before(_record_env):
    """反方向：什麼都沒丟時回覆一字不差是原本那一句（少了這支，「永遠附一句」也會綠）。"""
    _record_env.events(_recorded_clicks(2))
    _sr_run(b._macro_record(_record_env.message, "stop"))
    assert _record_env.replies == [
        "⏹️ 錄製結束，已存成巨集 `rec1`（3 步）。\n"
        "用 `/macro show rec1` 檢查內容、`/macro run rec1` 重播。"]


def test_stopping_a_recording_with_nothing_replayable_says_what_was_skipped(_record_env):
    _record_env.events(_recorded_clicks(0, bogus_after=True))
    _sr_run(b._macro_record(_record_env.message, "stop"))
    assert _record_env.saved == []
    assert _record_env.replies == [
        "⏹️ 錄製結束，但沒有錄到可重播的操作，因此沒有存檔。\n"
        "另有 1 步無法重播，已略過。"]


def test_panic_stopping_a_recording_says_what_the_step_cap_cut(_record_env, monkeypatch):
    _quiet_panic(monkeypatch)
    monkeypatch.setattr(b, "_MACRO_RECORDING", "rec1")
    monkeypatch.setattr(b._gui, "release_all_inputs_report", lambda: ([], []))
    _record_env.events(_recorded_clicks(5, bogus_after=True))
    _sr_run(b.cmd_panic(_record_env.message))
    assert ("• 已停止錄製並存成 `rec1`（3 步；超過 4 步上限，後面還有 3 個動作沒有存到；"
            "另有 1 步無法重播，已略過）") in _record_env.replies[-1].splitlines()
    assert _record_env.saved == [("rec1", ["click 0 0", "wait 1", "click 20 0"])]


def test_panic_stopping_an_empty_recording_says_what_was_skipped(_record_env, monkeypatch):
    _quiet_panic(monkeypatch)
    monkeypatch.setattr(b, "_MACRO_RECORDING", "rec1")
    monkeypatch.setattr(b._gui, "release_all_inputs_report", lambda: ([], []))
    _record_env.events(_recorded_clicks(0, bogus_after=True))
    _sr_run(b.cmd_panic(_record_env.message))
    assert ("• 已停止錄製（沒有錄到可重播的操作；另有 1 步無法重播，已略過）"
            in _record_env.replies[-1].splitlines())
    assert _record_env.saved == []


def test_the_record_watchdog_logs_what_the_step_cap_cut(_record_env, monkeypatch, capsys):
    """自動停下來那一條的 stderr 一定要有兩個數字：它現在也會在頻道講一聲（下面那一組），
    但頻道找不到、送不出去的時候，這一行是唯一的紀錄。沒記到頻道 id 時退回設定的頻道。"""
    monkeypatch.setattr(b._gui, "RECORD_MAX_SEC", 0.0)
    _record_env.events(_recorded_clicks(5, bogus_after=True))
    channels = _sr_client(monkeypatch, known=(b.CHANNEL_ID,))
    _sr_run(b._macro_record_watchdog("rec1", seq=b._MACRO_RECORDING_SEQ))
    assert len(channels[b.CHANNEL_ID].sent) == 1, channels[b.CHANNEL_ID].sent
    err = capsys.readouterr().err
    assert ("with 3 step(s); 3 action(s) cut by the 4-step cap, "
            "1 unrecordable step(s) skipped") in err, err
    assert _record_env.saved == [("rec1", ["click 0 0", "wait 1", "click 20 0"])]
    assert b._MACRO_RECORDING is None


# ---------------------------------------------------------------------------
# 錄製到點自動停止：在開始錄製的頻道講一聲（2026-09-21）
#
# 自動停止那一條沒有觸發它的訊息可回，所以原本使用者完全不會知道巨集已經存了、
# 也不知道上限截掉了多少（兩個數字只寫 stderr）。開始錄製時記下頻道與發起人，
# 到點時送一句泛用的話；找不到頻道就退回設定的頻道，都找不到只寫 stderr。
# ---------------------------------------------------------------------------
class _ShellEnv:
    """`/host sh run` 的替身：`_gui` 的四個入口全換成記錄用的，**絕不真的跑指令**。"""

    def __init__(self, monkeypatch, output="", rc=0, timed_out=False):
        self.runs: list = []
        self.stops = 0
        self.cwds: list = []
        self.replies: list = []
        self.files: list = []
        self.deleted = 0
        env = self

        class _Status:
            async def delete(self):
                env.deleted += 1

        async def _reply(_message, content=None, **kwargs):
            env.replies.append(content)
            if kwargs.get("file") is not None:
                env.files.append(kwargs["file"])
            return _Status()

        def _run(command, *, timeout):
            env.runs.append((command, timeout))
            return {"rc": rc, "timed_out": timed_out, "elapsed": 0.5, "output": output}

        def _stop_all():
            env.stops += 1
            return 2

        monkeypatch.setattr(b, "safe_reply", _reply)
        monkeypatch.setattr(b._gui, "run_shell", _run)
        monkeypatch.setattr(b._gui, "shell_stop_all", _stop_all)
        monkeypatch.setattr(b._gui, "set_shell_cwd", lambda target: env.cwds.append(target))
        self.message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))

    def run(self, payload):
        _sr_run(b.cmd_sh(self.message, payload))
        return self.replies[-1]


@pytest.mark.parametrize("payload, command, timeout", [
    ("echo hi", "echo hi", "default"),
    ("--timeout 5 echo hi", "echo hi", 5.0),
    ("--timeout=7.5 echo hi", "echo hi", 7.5),
    ("--TIMEOUT 5 echo hi", "echo hi", 5.0),
    ("--timeout 999999 echo hi", "echo hi", "max"),
    ("--timeout5 echo hi", "--timeout5 echo hi", "default"),   # 沒有分隔 → 那是指令的一部分
])
def test_the_shell_command_timeout_is_parsed_and_capped(monkeypatch, payload, command, timeout):
    env = _ShellEnv(monkeypatch, output="ok")
    env.run(payload)
    expected = {"default": b._gui.SHELL_DEFAULT_TIMEOUT_SEC,
                "max": b._gui.SHELL_MAX_TIMEOUT_SEC}.get(timeout, timeout)
    assert env.runs == [(command, expected)], env.runs


@pytest.mark.parametrize("payload, expected", [
    ("", "用法"), ("   ", "用法"), ("--timeout 5 ", "用法"),
])
def test_a_shell_command_with_nothing_to_run_runs_nothing(monkeypatch, payload, expected):
    env = _ShellEnv(monkeypatch)
    assert expected in env.run(payload)
    assert env.runs == [] and env.stops == 0 and env.cwds == []


def test_stop_and_cd_are_handled_by_the_bot_not_by_a_shell(monkeypatch):
    """`stop` 中止執行中的指令；`cd` 要「記住」才有意義（每次都是獨立行程，真的跑下去等於
    沒發生），回覆不含路徑。兩個都**不得**變成一次真的指令執行。"""
    env = _ShellEnv(monkeypatch)
    assert "已中止 2 個" in env.run("STOP")
    reply = env.run(r"cd C:\Users\someone\secret")
    assert env.cwds == [r"C:\Users\someone\secret"]
    assert "someone" not in reply and "已切換" in reply
    assert "回到專案目錄" in env.run("cd")
    assert env.runs == [] and env.stops == 1


def test_shell_output_is_scrubbed_before_it_is_sent(monkeypatch):
    """輸出原文只進 stderr；送出去的是刷過的版本（主機路徑、帳號信箱）。"""
    leaky = "done\nC:\\Users\\someone\\secret.txt\nuser someone@example.com\n"
    env = _ShellEnv(monkeypatch, output=leaky, rc=3)
    reply = env.run("type secret.txt")
    assert reply.startswith("⚠️ rc=3"), reply
    assert "someone" not in reply and "done" in reply, reply
    assert env.deleted == 1, "「執行中…」那一則沒有收掉"


def test_shell_output_cannot_break_out_of_its_code_block(monkeypatch):
    env = _ShellEnv(monkeypatch, output="```\n@everyone\n```")
    reply = env.run("echo")
    assert reply.count("```") == 2, reply


@pytest.mark.parametrize("output, timed_out, head, attached", [
    ("", False, "✅ rc=0", False),
    ("x" * 1800, False, "✅ rc=0", True),
    ("partial", True, "⏱️ rc=0", False),
])
def test_shell_results_are_shaped_by_size_and_outcome(monkeypatch, output, timed_out,
                                                       head, attached):
    """沒有輸出要講「沒有輸出」；長輸出改附檔（切成一堆程式碼區塊會洗版、撞速率限制）；
    逾時要講明已中止。"""
    env = _ShellEnv(monkeypatch, output=output, timed_out=timed_out)
    reply = env.run("do it")
    assert reply.startswith(head), reply
    assert bool(env.files) is attached
    if not output:
        assert "沒有輸出" in reply
    if timed_out:
        assert "逾時已中止" in reply


class _JobEnv:
    """`/host job` 的替身：`_gui` 的背景作業入口全換成記錄用的，**絕不真的起行程**。"""

    def __init__(self, monkeypatch, *, rows=(), log=None, fail=None):
        self.calls: list = []
        self.replies: list = []
        self.files: list = []
        env = self

        async def _reply(_message, content=None, **kwargs):
            env.replies.append(content)
            if kwargs.get("file") is not None:
                env.files.append(kwargs["file"])

        def _rec(name, result):
            def _fn(*args, **kwargs):
                env.calls.append((name, args, kwargs))
                if fail is not None:
                    raise fail
                return result
            return _fn

        monkeypatch.setattr(b, "safe_reply", _reply)
        monkeypatch.setattr(b, "_is_owner", lambda _m: True)
        for name, result in (("job_start", 7), ("job_send", None),
                             ("job_close_input", None), ("job_list", list(rows)),
                             ("job_log", log), ("job_stop", True), ("job_clear", 3)):
            monkeypatch.setattr(b._gui, name, _rec(name, result))
        self.message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))

    def run(self, payload):
        _sr_run(b.cmd_job(self.message, payload))
        return self.replies[-1]


@pytest.mark.parametrize("payload, expected_call, reply_has", [
    ("run pip install x", ("job_start", ("pip install x",), {"interactive": False}), "#7"),
    ("run --stdin python -i", ("job_start", ("python -i",), {"interactive": True}), "job send 7"),
    ("run --STDIN python -i", ("job_start", ("python -i",), {"interactive": True}), "job send 7"),
    ("run", None, "用法"),
    ("run --stdin", None, "用法"),
    ("send 3 yes please", ("job_send", (3, "yes please"), {}), "已送出 10 字"),
    ("send 3", None, "用法"),
    ("send x hi", None, "必須是整數"),
    ("send", None, "用法"),
    ("eof 3", ("job_close_input", (3,), {}), "輸入端已關閉"),
    ("eof x", None, "必須是整數"),
    ("stop 3", ("job_stop", (3,), {}), "已中止"),
    ("stop", None, "用法"),
    ("stop x", None, "必須是整數"),
    ("log x", None, "必須是整數"),
    ("log 3 many", None, "必須是整數"),
    ("log", None, "用法"),
    ("clear", ("job_clear", (), {}), "3 筆"),
    ("", None, "用法"),
    ("launch rockets", None, "用法"),
])
def test_background_jobs_only_act_on_a_well_formed_request(monkeypatch, payload,
                                                           expected_call, reply_has):
    """每一個子指令：格式對才動到背景作業，格式不對只回用法或原因——尤其不能把 `stop`
    沒給編號當成 `#0`（以前會回一句「早就結束了」，忘了打編號的人以為要停的已經停了）。"""
    env = _JobEnv(monkeypatch)
    reply = env.run(payload)
    assert env.calls == ([expected_call] if expected_call else []), env.calls
    assert reply_has in reply, reply


def test_the_job_list_scrubs_and_labels_each_job(monkeypatch):
    rows = [
        {"id": 1, "running": True, "stopped": False, "rc": None, "elapsed": 5,
         "lines": 2, "command": r"type C:\Users\someone\secret.txt"},
        {"id": 2, "running": False, "stopped": True, "rc": None, "elapsed": 9,
         "lines": 0, "command": "sleep 100"},
        {"id": 3, "running": False, "stopped": False, "rc": 4, "elapsed": 1,
         "lines": 1, "command": "x" * 200},
    ]
    env = _JobEnv(monkeypatch, rows=rows)
    reply = env.run("list")
    assert "執行中" in reply and "已中止" in reply and "結束 rc=4" in reply, reply
    assert "someone" not in reply, "作業的指令列原文進了回覆"
    assert "x" * 61 not in reply, "指令列沒有截短"
    assert "目前沒有背景作業" in _JobEnv(monkeypatch).run("list")


@pytest.mark.parametrize("text, dropped, attached, expect", [
    ("", 0, False, "目前沒有輸出"),
    ("C:\\Users\\someone\\a.txt done", 0, False, "done"),
    ("```\n@everyone", 0, False, "@everyone"),
    ("y" * 1800, 0, True, "改以檔案附上"),
    ("tail", 12, False, "前 12 行已因長度捨棄"),
])
def test_a_job_log_is_scrubbed_fenced_and_shaped(monkeypatch, text, dropped, attached, expect):
    log = {"id": 3, "running": False, "stopped": False, "rc": 0, "elapsed": 2,
           "shown": 1, "total": 1, "dropped": dropped, "text": text}
    env = _JobEnv(monkeypatch, log=log)
    reply = env.run("log 3 5")
    assert env.calls == [("job_log", (3, 5), {})]
    assert expect in reply and "someone" not in reply, reply
    assert bool(env.files) is attached
    if not attached and text:
        assert reply.count("```") == 2, reply


def test_a_job_failure_is_reported_in_the_libraries_own_generic_words(monkeypatch):
    env = _JobEnv(monkeypatch, fail=b._GuiError("找不到這個作業。"))
    assert env.run("stop 9") == "❌ 找不到這個作業。"


class _ImageQueueEnv:
    """`/gen image_queue` 的替身：佇列與對照表換成新的，佔位訊息的編輯、幫浦、重算位置都只記錄。"""

    def __init__(self, monkeypatch, ids=("r1", "r2", "r3"), history=()):
        self.replies: list = []
        self.edits: list = []
        self.pumps = 0
        self.resubmitted: list = []
        env = self

        async def _reply(_message, content=None, **_kw):
            env.replies.append(content)

        async def _edit(placeholder, text):
            env.edits.append((placeholder, text))

        async def _pump():
            env.pumps += 1

        async def _nothing():
            return None

        async def _generate(_message, text):
            env.resubmitted.append(text)

        self.reqs = [b._GenerateRequest(rid, {"prompt": rid}, {"submitted_mono": 0.0},
                                        f"ph-{rid}") for rid in ids]
        monkeypatch.setattr(b, "safe_reply", _reply)
        monkeypatch.setattr(b, "_generate_edit_placeholder", _edit)
        monkeypatch.setattr(b, "_generate_pump", _pump)
        monkeypatch.setattr(b, "_generate_refresh_queue", _nothing)
        monkeypatch.setattr(b, "mcmd_generate", _generate)
        monkeypatch.setattr(b, "_generate_history_tail", lambda n: list(history)[-n:])
        monkeypatch.setattr(b, "_generate_queue", list(self.reqs))
        monkeypatch.setattr(b, "_single_image_pending", {r.request_id: r.ctx for r in self.reqs})
        monkeypatch.setattr(b, "_generate_queue_undo", type(b._generate_queue_undo)(maxlen=10))
        monkeypatch.setattr(b, "_generate_inflight", None)
        self.message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID))

    def run(self, rest):
        _sr_run(b.mcmd_generate_queue(self.message, rest))
        return self.replies[-1]

    def order(self):
        return [r.request_id for r in b._generate_queue]


@pytest.mark.parametrize("rest, expected", [
    ("remove", "用法"), ("remove x", "必須是整數"), ("remove 0", "只有 3 筆"),
    ("remove 4", "只有 3 筆"), ("remove -1", "只有 3 筆"),
    ("front", "用法"), ("front two", "必須是整數"), ("front 9", "只有 3 筆"),
    ("undo", "沒有可復原"), ("retry", "用法"), ("retry nope", "找不到"),
    ("explode", "用法"),
])
def test_an_image_queue_command_that_does_not_fit_changes_nothing(monkeypatch, rest,
                                                                   expected):
    env = _ImageQueueEnv(monkeypatch)
    assert expected in env.run(rest)
    assert env.order() == ["r1", "r2", "r3"] and env.edits == [] and env.resubmitted == []
    assert set(b._single_image_pending) == {"r1", "r2", "r3"}


def test_removing_and_promoting_move_the_right_request(monkeypatch):
    """位置是從 1 算的——少了 `- 1` 會刪錯那一個人的請求。"""
    env = _ImageQueueEnv(monkeypatch)
    env.run("front 3")
    assert env.order() == ["r3", "r1", "r2"]
    env.run("rm 2")
    assert env.order() == ["r3", "r2"]
    assert env.edits == [("ph-r1", "已從產圖佇列取消。")]
    assert "r1" not in b._single_image_pending


def test_clearing_the_image_queue_can_be_undone_in_order(monkeypatch):
    """清掉之後復原：同樣的順序回到**最前面**，對照表也要回來——少了對照表，那幾張圖
    做完之後找不到要回給誰。"""
    env = _ImageQueueEnv(monkeypatch)
    assert "已取消 3 筆" in env.run("clear")
    assert env.order() == [] and b._single_image_pending == {}
    assert [p for p, _t in env.edits] == ["ph-r1", "ph-r2", "ph-r3"]
    b._generate_queue.append(b._GenerateRequest("r9", {}, {}, None))
    assert "已復原 3 筆" in env.run("undo")
    assert env.order() == ["r1", "r2", "r3", "r9"]
    assert set(b._single_image_pending) == {"r1", "r2", "r3"}
    assert env.pumps == 1
    assert "沒有可復原" in env.run("undo")


def test_an_empty_clear_leaves_nothing_to_undo(monkeypatch):
    env = _ImageQueueEnv(monkeypatch, ids=())
    assert "已取消 0 筆" in env.run("clear")
    assert "沒有可復原" in env.run("undo")


def test_retry_resubmits_a_past_request_with_its_character_fields(monkeypatch):
    history = [
        {"request_id": "old", "main_prompt": "a cat", "char1": "", "char2": "",
         "undesired": "", "ok": False, "ts": 1.0},
        {"request_id": "full", "main_prompt": "a dog", "char1": "c1", "char2": "",
         "undesired": "blurry", "ok": True, "ts": 2.0},
    ]
    env = _ImageQueueEnv(monkeypatch, history=history)
    _sr_run(b.mcmd_generate_queue(env.message, "retry old"))
    _sr_run(b.mcmd_generate_queue(env.message, "retry full"))
    assert env.resubmitted == ["a cat", "a dog | c1 |  | blurry"], env.resubmitted


def test_the_image_queue_status_lists_at_most_ten(monkeypatch):
    env = _ImageQueueEnv(monkeypatch, ids=tuple(f"r{i}" for i in range(12)))
    reply = env.run("")
    assert "waiting: `12/" in reply and "id=`r9`" in reply and "id=`r10`" not in reply
    assert "… 2 more" in reply


def _eta(monkeypatch, queues, *, spi=60.0, rest_until=None, cfg=None):
    """跑一次 `/eta`，回 `(文字回覆, embed 的欄位 dict, footer)`。佇列、設定、速率全是替身。"""
    import time as _time
    files = {b.TODO_PROMPT_FILE: queues[0], b.TODO_FILE_1: queues[1],
             b.TODO_FILE_2: queues[2], b.TODO_UNDESIRED_FILE: queues[3]}
    sent: list = []

    async def _reply(_message, content=None, **kwargs):
        sent.append((content, kwargs.get("embed")))

    config = {"images_per_character": 10, "schedule_limit_hours": 1.0, "rest_hours": 2.0}
    config.update(cfg or {})
    monkeypatch.setattr(b, "read_todo_entries", lambda path: list(files[path]))
    monkeypatch.setattr(b, "load_batch_config", lambda: config)
    monkeypatch.setattr(b, "_seconds_per_image", lambda: (spi, True, "window", True))
    monkeypatch.setattr(b, "_scheduled_rest_until",
                        lambda: None if rest_until is None else _time.time() + rest_until)
    monkeypatch.setattr(b, "safe_reply", _reply)
    _sr_run(b.cmd_eta(types.SimpleNamespace()))
    content, embed = sent[-1]
    if embed is None:
        return content, {}, ""
    return content, {f.name: f.value for f in embed.fields}, embed.footer.text


def test_eta_with_nothing_queued_says_so(monkeypatch):
    content, fields, _ = _eta(monkeypatch, ([], [], [], []))
    assert "empty" in content and not fields
    content, fields, _ = _eta(monkeypatch, (["end", "a"], ["x", "y"], [], []))
    assert "`end`" in content and not fields


def test_eta_adds_work_the_rests_ahead_and_the_rest_in_progress(monkeypatch):
    """3 對 × 10 張 × 60 秒 ＝ 30 分鐘工作；每小時工作要休 2 小時，30 分鐘用不滿一個
    週期，所以沒有未來的休息。此刻若正在休息（還剩 1 小時），那一小時要加進總時間——
    少了它 ETA 會短報整段休息。"""
    queues = (["p1", "p2", "p3"], ["a", "b", "c"], [], [])
    _content, fields, _ = _eta(monkeypatch, queues)
    assert fields["pairs"] == "3" and fields["rest periods"] == "0 × 2.0h"
    assert fields["raw work"] == fields["total wall time"]
    _content, resting, _ = _eta(monkeypatch, queues, rest_until=3600)
    assert "resting now" in resting["rest periods"]
    assert resting["total wall time"] != resting["raw work"]


def test_eta_counts_one_rest_per_full_work_window(monkeypatch):
    """6 對 × 10 張 × 60 秒 ＝ 1 小時；`schedule_limit_hours` 是 0.25 → 4 個完整窗口、4 次休息。"""
    queues = ([f"p{i}" for i in range(6)], [f"c{i}" for i in range(6)], [], [])
    _content, fields, _ = _eta(monkeypatch, queues, cfg={"schedule_limit_hours": 0.25})
    assert fields["rest periods"] == "4 × 2.0h", fields
    assert fields["raw work"] == b._format_duration(3600), fields
    assert fields["total wall time"] == b._format_duration(3600 + 4 * 2 * 3600), fields


def test_eta_says_where_an_end_marker_caps_the_estimate(monkeypatch):
    queues = (["p1", "p2", "end", "p4"], ["a", "b", "c", "d"], [], [])
    _content, fields, footer = _eta(monkeypatch, queues)
    assert fields["pairs"] == "2" and "entry #3" in footer, (fields, footer)


class _AllowdirEnv:
    """`/dorossi allowdir` 的替身：工作階段狀態只在記憶體裡；目錄驗證用真的那一支（它只
    問「是不是一個存在的目錄」），餵的都是 `tmp_path` 底下的目錄。"""

    def __init__(self, monkeypatch):
        self.uid = str(b.DOROSSI_USER_ID)
        self.state = {self.uid: {"active": "s1", "sessions": {"s1": {}, "s2": {}}}}
        self.replies: list = []
        env = self

        async def _reply(_message, content=None, **_kw):
            env.replies.append(content)

        async def _rmw(mutate):
            return mutate(env.state)

        monkeypatch.setattr(b, "safe_reply", _reply)
        monkeypatch.setattr(b, "_dorossi_state_rmw", _rmw)
        monkeypatch.setattr(b, "_dorossi_load_state", lambda: env.state)
        self.message = types.SimpleNamespace(
            author=types.SimpleNamespace(id=b.DOROSSI_USER_ID),
            channel=types.SimpleNamespace(id=1))

    def run(self, rest):
        _sr_run(b.mcmd_allowdir(self.message, rest))
        return self.replies[-1]

    def extra(self, sid):
        return self.state[self.uid]["sessions"][sid].get("cc_extra_dir")


def test_allowdir_add_parses_a_trailing_session_and_a_path_with_spaces(monkeypatch, tmp_path):
    """路徑本身可以有空白；最後一個詞只有在**是** session id 時才被拆成 session。"""
    spaced = tmp_path / "my dir"
    spaced.mkdir()
    env = _AllowdirEnv(monkeypatch)
    env.run(f"add {spaced} s2")
    assert env.extra("s2") == str(spaced.resolve()) and env.extra("s1") is None
    env.run(f'add "{spaced}"')
    assert env.extra("s1") == str(spaced.resolve()), "帶引號的路徑沒有被剝掉引號"
    odd = tmp_path / "dir s9x"
    odd.mkdir()
    env.run(f"add {odd}")
    assert env.extra("s1") == str(odd.resolve()), "不是 session id 的尾巴被拆掉了"


@pytest.mark.parametrize("rest", ["add", "add   ", "grant /", "explode"])
def test_allowdir_with_nothing_usable_changes_nothing(monkeypatch, rest):
    env = _AllowdirEnv(monkeypatch)
    assert "用法" in env.run(rest)
    assert env.extra("s1") is None and env.extra("s2") is None


def test_allowdir_refuses_a_directory_that_does_not_exist_without_echoing_it(
        monkeypatch, tmp_path):
    """放行一個不存在的目錄沒有意義，而且回覆不回聲那串路徑——它也可能根本不是路徑。"""
    env = _AllowdirEnv(monkeypatch)
    missing = tmp_path / "nope-secret-name"
    reply = env.run(f"add {missing}")
    assert reply == "指定的目錄無法使用。"
    assert env.extra("s1") is None
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    assert env.run(f"add {a_file}") == "指定的目錄無法使用。"
    assert env.extra("s1") is None


def test_allowdir_remove_says_whether_there_was_anything(monkeypatch, tmp_path):
    env = _AllowdirEnv(monkeypatch)
    env.run(f"add {tmp_path}")
    assert "已清除" in env.run("remove")
    assert env.extra("s1") is None and "cc_extra_dir" not in env.state[env.uid]["sessions"]["s1"]
    assert "確認沒有" in env.run("rm")
    assert "沒有額外可存取目錄" in env.run("list")


class _DorossiQueueCmdEnv:
    """`/dorossi queue` 的失敗佇列與復原那幾支：磁碟上的兩份佇列換成記憶體，重新排入只記錄。"""

    def __init__(self, monkeypatch, failed=(), undo=()):
        self.failed = [dict(r) for r in failed]
        self.scheduled: list = []
        self.readded: list = []
        self.replies: list = []
        env = self

        async def _reply(_message, content=None, **_kw):
            env.replies.append(content)

        def _remove(queue_id):
            env.failed = [r for r in env.failed if r.get("id") != queue_id]

        def _write(rows):
            env.failed = list(rows)

        monkeypatch.setattr(b, "safe_reply", _reply)
        monkeypatch.setattr(b, "_dorossi_failed_queue_read", lambda: [dict(r) for r in env.failed])
        monkeypatch.setattr(b, "_dorossi_failed_queue_remove", _remove)
        monkeypatch.setattr(b, "_dorossi_failed_queue_write", _write)
        monkeypatch.setattr(b, "_dorossi_schedule_requeued_groups",
                            lambda grouped: env.scheduled.append(grouped))
        monkeypatch.setattr(b, "_dorossi_queue_add", lambda row: env.readded.append(row))
        monkeypatch.setattr(b, "_dorossi_event", lambda *_a, **_k: None)
        undo_stack = type(b._dorossi_queue_undo)(maxlen=b._dorossi_queue_undo.maxlen)
        for rows in undo:
            undo_stack.append(list(rows))
        monkeypatch.setattr(b, "_dorossi_queue_undo", undo_stack)
        self.message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.DOROSSI_USER_ID))

    def run(self, rest):
        _sr_run(b.mcmd_queue(self.message, rest))
        return self.replies[-1]


_FAILED_ROWS = [
    {"id": "q1", "uid": "7", "sid": "s1", "prompt": "a", "failed_at": 1.0, "error": "anchor"},
    {"id": "q2", "uid": "7", "sid": "s2", "prompt": "b", "failed_at": 2.0, "error": "channel"},
]


def test_retrying_one_failed_restore_touches_only_that_row(monkeypatch):
    """重新排入的列要把失敗的痕跡（`failed_at`／`error`）拿掉——留著的話它會以「失敗過」的
    樣子再進一次還原，下一次 `detail` 也分不出新舊。其他失敗列不動。"""
    env = _DorossiQueueCmdEnv(monkeypatch, failed=_FAILED_ROWS)
    assert "已重新排入 1 筆" in env.run("retry_failed q2")
    assert [r["id"] for r in env.failed] == ["q1"]
    (grouped,) = env.scheduled
    assert list(grouped) == [("7", "s2")]
    (row,) = grouped[("7", "s2")]
    assert "failed_at" not in row and "error" not in row and row["prompt"] == "b"


def test_retrying_all_failed_restores_groups_them_by_session(monkeypatch):
    env = _DorossiQueueCmdEnv(monkeypatch, failed=_FAILED_ROWS)
    assert "已重新排入 2 筆" in env.run("retry_failed")
    assert env.failed == [] and set(env.scheduled[0]) == {("7", "s1"), ("7", "s2")}


@pytest.mark.parametrize("failed, rest, expected", [
    ((), "retry_failed", "目前沒有"),
    (_FAILED_ROWS, "retry_failed q9", "找不到"),
    ((), "undo", "沒有可復原"),
])
def test_a_queue_retry_or_undo_with_nothing_to_act_on_changes_nothing(monkeypatch, failed,
                                                                      rest, expected):
    env = _DorossiQueueCmdEnv(monkeypatch, failed=failed)
    assert expected in env.run(rest)
    assert env.scheduled == [] and env.readded == [] and len(env.failed) == len(failed)


def test_undo_puts_a_parked_row_back_to_wait_instead_of_running_it(monkeypatch):
    """還沒到點的停放列（等用量重設）復原時要回磁碟繼續等；現在跑只會再撞一次同一面牆。
    一般的列與已經到點的停放列照常重新排入。"""
    import time as _time
    later = {"id": "p1", "uid": "7", "sid": "s1", "prompt": "wait",
             "status": b._DOROSSI_PARKED_STATUS, "run_at": _time.time() + 3600}
    due = dict(later, id="p2", run_at=_time.time() - 5)
    plain = {"id": "q3", "uid": "7", "sid": "s1", "prompt": "now"}
    env = _DorossiQueueCmdEnv(monkeypatch, undo=[[later, due, plain]])
    assert "已復原 3 筆" in env.run("undo")
    assert [r["id"] for r in env.readded] == ["p1"]
    (grouped,) = env.scheduled
    assert [r["id"] for r in grouped[("7", "s1")]] == ["p2", "q3"]
    assert "沒有可復原" in env.run("undo")


def test_clearing_failed_restores_reports_how_many(monkeypatch):
    env = _DorossiQueueCmdEnv(monkeypatch, failed=_FAILED_ROWS)
    assert "已清除 2 筆" in env.run("failed_clear") and env.failed == []


def _audit(monkeypatch, tmp_path, lines, payload=""):
    path = tmp_path / "audit.ndjson"
    if lines is not None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(b, "AUDIT_FILE", path)
    sent: list = []

    async def _reply(_message, content=None, **_kw):
        sent.append(content)

    monkeypatch.setattr(b, "safe_reply", _reply)
    _sr_run(b.cmd_audit(types.SimpleNamespace(author=types.SimpleNamespace(id=5)), payload))
    return sent[-1]


def _audit_row(i, head="run", rest=""):
    return json.dumps({"ts": 1_789_000_000 + i, "user_id": 5, "user_name": f"u{i}",
                       "channel_id": 1, "head": head, "rest": rest})


def test_the_audit_view_scrubs_what_people_typed(monkeypatch, tmp_path):
    """稽核記錄存的是原樣打的參數（`/host sh run` 的指令、`/host get` 的主機路徑）。存在磁碟上
    沒問題，貼回頻道就是外洩——送出前要刷過。"""
    reply = _audit(monkeypatch, tmp_path, [
        _audit_row(1, "sh", r"type C:\Users\someone\secret.txt"),
        _audit_row(2, "get", "mail someone@example.com"),
    ])
    assert "someone" not in reply, reply
    assert "`sh`" in reply and "`get`" in reply


@pytest.mark.parametrize("payload, shown", [("", 20), ("3", 3), ("0", 1), ("-4", 1),
                                           ("999", 50)])
def test_the_audit_view_shows_a_clamped_number_of_recent_entries(monkeypatch, tmp_path,
                                                                  payload, shown):
    reply = _audit(monkeypatch, tmp_path, [_audit_row(i) for i in range(60)], payload)
    assert f"最近 {shown} 筆（共 60 筆）" in reply, reply
    assert "u59" in reply, "顯示的不是最近的那幾筆"


def test_audit_grep_filters_on_the_command_head_only(monkeypatch, tmp_path):
    rows = [_audit_row(1, "run", "stop"), _audit_row(2, "stop"), _audit_row(3, "Stop")]
    reply = _audit(monkeypatch, tmp_path, rows, "grep STOP")
    assert "共 2 筆 match" in reply and "u1" not in reply, reply
    assert "沒有 head 含" in _audit(monkeypatch, tmp_path, rows, "grep nothing")
    assert "usage" in _audit(monkeypatch, tmp_path, rows, "grep")
    assert "usage" in _audit(monkeypatch, tmp_path, rows, "many")


def test_the_audit_view_survives_junk_lines_and_a_missing_file(monkeypatch, tmp_path):
    """一行壞掉的記錄不得讓整個稽核檢視掛掉——那是唯一看得到它的指令。合法 JSON 但不是
    物件的那幾種（`[]`、數字、字串）以前會讓下面的 `.get` 丟例外。"""
    reply = _audit(monkeypatch, tmp_path,
                   ["{not json", "", _audit_row(1), "[]", "5", '"text"', "null"])
    assert "共 1 筆" in reply, reply
    assert "還不存在" in _audit(monkeypatch, tmp_path / "gone", None)


@pytest.mark.parametrize("ts", ["yesterday", 1e30, -5, 0, float("nan"), None, True, [1]])
def test_a_log_view_survives_a_timestamp_it_cannot_read(monkeypatch, tmp_path, ts):
    """時間戳壞掉的那一行照樣列出來，時間印 `?`——`time.localtime` 對字串丟 `TypeError`、
    對超出範圍的數字丟 `OverflowError`／`OSError`，以前整個檢視就跟著掛掉。"""
    bad = json.dumps({"ts": ts, "user_name": "u9", "head": "run"}) if ts == ts else (
        '{"ts": NaN, "user_name": "u9", "head": "run"}')
    reply = _audit(monkeypatch, tmp_path, [_audit_row(1), bad])
    assert "`?` `u9`" in reply, reply
    assert b._log_clock(1_789_000_000).count(":") == 2


def test_the_dorossi_log_views_survive_junk_lines(monkeypatch, tmp_path):
    """`/dorossi logs` 與 `/dorossi errors` 讀同一份事件檔：合法 JSON 但不是物件的行、時間戳
    壞掉的行，都不得讓整個檢視丟例外。"""
    path = tmp_path / "dorossi_events.ndjson"
    path.write_text("\n".join([
        "[]", "5", '"text"', "null", "{broken",
        json.dumps({"ts": "later", "type": "error", "sid": "s1"}),
        json.dumps({"ts": 1_789_000_000, "type": "queue_full", "sid": "s2"}),
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(b, "DOROSSI_EVENTS_FILE", path)
    sent: list = []

    async def _reply(_message, content=None, **_kw):
        sent.append(content)

    monkeypatch.setattr(b, "safe_reply", _reply)
    message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.DOROSSI_USER_ID))
    _sr_run(b.mcmd_logs(message, "50"))
    assert "last 2" in sent[-1] and "`?` `error`" in sent[-1], sent[-1]
    _sr_run(b.mcmd_dorossi_errors(message, ""))
    assert "queue_full" in sent[-1] and "error" in sent[-1], sent[-1]


def test_a_long_audit_view_is_cut_with_a_pointer_to_grep(monkeypatch, tmp_path):
    rows = [_audit_row(i, "x" * 40, "y" * 60) for i in range(50)]
    reply = _audit(monkeypatch, tmp_path, rows, "50")
    assert len(reply) < 2000 and "截斷" in reply, len(reply)


def test_debug_screenshots_are_owner_only_on_every_surface():
    """除錯截圖的清單印的是專案根目錄的檔名，上傳的是外部服務網頁的畫面——兩樣都是 Layer 1
    對非擁有者禁止的。它原本在「檢視」那一級，而 `user_roles` 沒設定時那一級等於頻道裡的
    任何人。閘在派發前，斜線與 `!` 兩個表面都要鎖（mention 面沒有這個指令）。"""
    assert b._is_owner_only_slash("out debug_show")
    assert "!debug_show" in b._OWNER_ONLY_BANGS
    assert "!debug_show" not in b._VIEWER_COMMANDS
    # 同一群的其他檢視指令不受影響（群組規則以外，逐一列舉的閘不外溢）。
    assert not b._is_owner_only_slash("out latest_for")


_RW_CHANNEL = 4242
_RW_USER = 77


def _record_watchdog_run(env, monkeypatch, events, *, known=(_RW_CHANNEL,),
                         fetch_error=None, seq=None):
    """跑一次到點的自動停止；回 id → 假頻道。上限縮成 0.01 秒（`:g` 印成 `0.01`）。"""
    monkeypatch.setattr(b._gui, "RECORD_MAX_SEC", 0.01)
    env.events(events)
    channels = _sr_client(monkeypatch, known=known, fetch_error=fetch_error)
    _sr_run(b._macro_record_watchdog(
        "rec1", seq=b._MACRO_RECORDING_SEQ if seq is None else seq,
        channel_id=_RW_CHANNEL, user_id=_RW_USER))
    return channels


def test_a_recording_started_on_another_platform_reports_back_there(
        _record_env, monkeypatch):
    """從別的平台開錄：自動結束的通知要回到那個對話。那邊的 id 在既有平台上找不到
    （私訊是負數；允許清單裡的對話等於設定頻道），拿 id 去找只會落到既有平台的設定
    頻道——這裡讓 id 故意撞上一個既有平台的頻道，那個頻道一個字都不能收到。"""
    import _chat_platform as cp
    delivered: list = []

    class _Platform(cp.ChatTransport):
        name = "stubplat"

        @property
        def capabilities(self):
            return cp.PlatformCapabilities()

        async def run(self):
            return None

        async def deliver(self, channel, content=None, **kwargs):
            delivered.append(content)

        async def revise(self, sent, content, **kwargs):
            return None

    conv = cp.ChatConversation(_Platform(), "c9", uid=_RW_CHANNEL, is_direct=False,
                               is_command_chat=True)
    scheduled: list = []
    monkeypatch.setattr(b, "safe_reply", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(b, "_MACRO_RECORDING", None)
    monkeypatch.setattr(b, "_MACRO_RUNNING", None)
    monkeypatch.setattr(b._gui, "record_start", lambda: None)
    monkeypatch.setattr(b, "_schedule_coro", lambda coro, **_kw: scheduled.append(coro))
    message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID),
                                    channel=conv)
    _sr_run(b._macro_record(message, "rec1"))
    (watchdog,) = scheduled
    monkeypatch.setattr(b._gui, "RECORD_MAX_SEC", 0.01)
    _record_env.events(_recorded_clicks(2))
    channels = _sr_client(monkeypatch, known=(_RW_CHANNEL,))
    _sr_run(watchdog)
    assert channels[_RW_CHANNEL].sent == [], "通知落到既有平台的頻道了"
    assert len(delivered) == 1 and "自動結束並存成巨集" in delivered[0], delivered


def test_the_record_watchdog_tells_the_recording_channel_what_it_saved(
        _record_env, monkeypatch):
    """存了 3 步、上限截掉 3 個動作、1 步略過：三件事都要講，存檔要記在發起人名下
    （手動停那條本來就帶 `author_id`，自動停原本沒帶）。"""
    authors: list = []

    def _save(name, steps, **kwargs):
        _record_env.saved.append((name, list(steps)))
        authors.append(kwargs.get("author_id"))

    monkeypatch.setattr(b._gui, "save_macro", _save)
    channels = _record_watchdog_run(
        _record_env, monkeypatch, _recorded_clicks(5, bogus_after=True))
    assert channels[_RW_CHANNEL].sent == [
        "⏹️ 巨集錄製 `rec1` 已達 0.01 秒上限，自動結束並存成巨集（3 步）。\n"
        "超過 4 步上限，後面還有 3 個動作沒有存到。\n"
        "另有 1 步無法重播，已略過。\n"
        "用 `/macro show rec1` 檢查內容、`/macro run rec1` 重播。"]
    assert _record_env.saved == [("rec1", ["click 0 0", "wait 1", "click 20 0"])]
    assert authors == [_RW_USER]
    assert b._MACRO_RECORDING is None


def test_the_record_watchdog_says_when_nothing_was_replayable(_record_env, monkeypatch):
    channels = _record_watchdog_run(
        _record_env, monkeypatch, _recorded_clicks(0, bogus_after=True))
    assert channels[_RW_CHANNEL].sent == [
        "⏹️ 巨集錄製 `rec1` 已達 0.01 秒上限，自動結束，但沒有錄到可重播的操作，"
        "因此沒有存檔。\n另有 1 步無法重播，已略過。"]
    assert _record_env.saved == []


def test_the_record_watchdog_falls_back_to_the_configured_channel(
        _record_env, monkeypatch):
    """開始錄製的頻道找不回來（快取裡沒有、查詢也 404）：退回設定的頻道。"""
    channels = _record_watchdog_run(
        _record_env, monkeypatch, _recorded_clicks(2), known=(b.CHANNEL_ID,))
    assert channels[b.CHANNEL_ID].sent == [
        "⏹️ 巨集錄製 `rec1` 已達 0.01 秒上限，自動結束並存成巨集（3 步）。\n"
        "用 `/macro show rec1` 檢查內容、`/macro run rec1` 重播。"]


def test_the_record_watchdog_with_no_reachable_channel_only_logs(
        _record_env, monkeypatch, capsys):
    """一個能說話的頻道都沒有：巨集照樣存、不往上炸，只寫 stderr（兩個數字那一行
    也要在——那是唯一的紀錄）。"""
    channels = _record_watchdog_run(
        _record_env, monkeypatch, _recorded_clicks(5, bogus_after=True), known=(),
        fetch_error=discord.HTTPException(
            types.SimpleNamespace(status=503, reason="down"), "unavailable"))
    assert channels == {}
    assert _record_env.saved == [("rec1", ["click 0 0", "wait 1", "click 20 0"])]
    err = capsys.readouterr().err
    assert "macro record auto-stop: no channel to report to" in err, err
    assert "3 action(s) cut by the 4-step cap" in err, err


def test_the_record_watchdog_survives_a_channel_that_refuses_the_message(
        _record_env, monkeypatch, capsys):
    """頻道找得到、但送不出去（權限被拿掉、平台暫時斷線）：巨集照樣存、計時器不往上炸，
    原因只寫 stderr，而且**只試一次**。

    回報函式跑在計時器的收尾路徑上，也在計時器自己的例外處理裡——它漏出來的例外會先
    被計時器當成「自動結束出了問題」再送一次，第二次再漏就沒有人接了。"""
    class _Refusing(_SrChannel):
        async def send(self, content=None, **_kw):
            self.sent.append(content)
            raise discord.Forbidden(
                types.SimpleNamespace(status=403, reason="forbidden"), "missing access")

    channel = _Refusing(_RW_CHANNEL)
    monkeypatch.setattr(b._gui, "RECORD_MAX_SEC", 0.01)
    monkeypatch.setattr(b.client, "get_channel",
                        lambda cid: channel if cid == _RW_CHANNEL else None)
    _record_env.events(_recorded_clicks(2))
    _sr_run(b._macro_record_watchdog(
        "rec1", seq=b._MACRO_RECORDING_SEQ, channel_id=_RW_CHANNEL, user_id=_RW_USER))
    assert len(channel.sent) == 1, channel.sent
    assert channel.sent[0].startswith("⏹️ 巨集錄製 `rec1` 已達"), channel.sent
    assert _record_env.saved == [("rec1", ["click 0 0", "wait 1", "click 20 0"])]
    err = capsys.readouterr().err
    assert "macro record auto-stop report failed" in err, err
    assert "_macro_record_watchdog failed" not in err, err


def test_the_record_watchdog_reports_a_save_failure(_record_env, monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise b._GuiError("巨集儲存失敗。")

    monkeypatch.setattr(b._gui, "save_macro", _refuse)
    channels = _record_watchdog_run(_record_env, monkeypatch, _recorded_clicks(2))
    assert channels[_RW_CHANNEL].sent == [
        "❌ 巨集錄製 `rec1` 自動結束，但沒有存成：巨集儲存失敗。"]


def test_the_record_watchdog_keeps_an_unexpected_failure_generic(
        _record_env, monkeypatch, capsys):
    """非 `GuiError` 的例外：原文只進 stderr，頻道裡只有一句泛用的話（Layer 1）。"""
    def _explode():
        raise RuntimeError(r"C:\secret\hook.dll went away")

    monkeypatch.setattr(b._gui, "RECORD_MAX_SEC", 0.01)
    monkeypatch.setattr(b._gui, "record_stop", _explode)
    channels = _sr_client(monkeypatch, known=(_RW_CHANNEL,))
    _sr_run(b._macro_record_watchdog(
        "rec1", seq=b._MACRO_RECORDING_SEQ, channel_id=_RW_CHANNEL, user_id=_RW_USER))
    assert channels[_RW_CHANNEL].sent == [
        "⚠️ 巨集錄製 `rec1` 自動結束時出了問題，請查看 log。"]
    assert "hook.dll" in capsys.readouterr().err


def test_a_record_watchdog_whose_stop_fails_still_frees_the_input_gate(
        _record_env, monkeypatch, capsys):
    """`record_stop` 丟例外（函式庫停不掉錄製）：閘門一定要放開——否則之後每個鍵鼠
    指令都被「巨集錄製 `rec1`」擋住，直到有人想到要手動停。頻道只收到一句泛用的話，
    細節只進 stderr。"""
    def _cannot_stop():
        raise b._GuiError("停止錄製失敗。")

    monkeypatch.setattr(b._gui, "RECORD_MAX_SEC", 0.01)
    monkeypatch.setattr(b._gui, "record_stop", _cannot_stop)
    assert b._macro_gate_holder() == "巨集錄製 `rec1`"
    channels = _sr_client(monkeypatch, known=(_RW_CHANNEL,))
    _sr_run(b._macro_record_watchdog(
        "rec1", seq=b._MACRO_RECORDING_SEQ, channel_id=_RW_CHANNEL, user_id=_RW_USER))
    assert b._MACRO_RECORDING is None
    assert b._macro_gate_holder() is None
    assert channels[_RW_CHANNEL].sent == [
        "⚠️ 巨集錄製 `rec1` 自動結束時出了問題，請查看 log。"]
    assert "停止錄製失敗" in capsys.readouterr().err


def test_a_record_watchdog_does_not_clear_a_recording_started_while_it_stopped(
        _record_env, monkeypatch):
    """`record_stop` 在執行緒裡跑的那段時間，有人手動停掉 `rec1`、又用**同一個名字**
    開了一次：計時器收尾時不能把新那一次的狀態清掉——清掉之後手動停說「沒有在錄」、新那一次
    自己的計時器又認不得它，那次錄製就永遠停不下來。同名是刻意的：只比名字的話認不出來。"""
    seq = b._MACRO_RECORDING_SEQ

    def _stop_while_a_new_one_starts():
        b._MACRO_RECORDING = "rec1"
        b._MACRO_RECORDING_SEQ = seq + 1
        return []

    monkeypatch.setattr(b, "_MACRO_RECORDING_SEQ", seq)
    monkeypatch.setattr(b._gui, "RECORD_MAX_SEC", 0.01)
    monkeypatch.setattr(b._gui, "record_stop", _stop_while_a_new_one_starts)
    _sr_client(monkeypatch, known=(_RW_CHANNEL,))
    _sr_run(b._macro_record_watchdog(
        "rec1", seq=seq, channel_id=_RW_CHANNEL, user_id=_RW_USER))
    assert b._MACRO_RECORDING == "rec1"
    assert b._MACRO_RECORDING_SEQ == seq + 1


def test_a_stale_record_watchdog_leaves_a_newer_recording_alone(_record_env, monkeypatch):
    """停掉 `rec1`、馬上又開一次 `rec1`：第一次的計時器到點時，名字對得上但序號不同，
    不能把第二次錄到一半的停掉，更不能宣稱「已達上限」。"""
    stops: list = []
    monkeypatch.setattr(b._gui, "RECORD_MAX_SEC", 0.01)
    monkeypatch.setattr(b._gui, "record_stop", lambda: stops.append(1) or [])
    monkeypatch.setattr(b, "_MACRO_RECORDING_SEQ", 8)
    channels = _sr_client(monkeypatch, known=(_RW_CHANNEL,))
    _sr_run(b._macro_record_watchdog(
        "rec1", seq=7, channel_id=_RW_CHANNEL, user_id=_RW_USER))
    assert stops == [], "舊的計時器停掉了新的一次錄製"
    assert b._MACRO_RECORDING == "rec1"
    assert channels[_RW_CHANNEL].sent == []
    # 正面對照：序號對得上時同一個計時器真的會停。
    _sr_run(b._macro_record_watchdog(
        "rec1", seq=8, channel_id=_RW_CHANNEL, user_id=_RW_USER))
    assert stops == [1]
    assert b._MACRO_RECORDING is None


@pytest.mark.parametrize("recording, running, name, expected", [
    ("rec1", None, "rec2", "已經在錄 `rec1`"),
    (None, "排程 #3", "rec2", "正在佔用鍵鼠"),
    (None, None, "", "用法"),
    (None, None, "stop", "目前沒有在錄製"),
], ids=["already-recording", "gate-held", "no-name", "stop-when-idle"])
def test_a_recording_that_cannot_start_or_stop_touches_nothing(
        monkeypatch, recording, running, name, expected):
    """錄製會在主機上掛全域的鍵鼠監聽。已經在錄時再開一次會蓋掉前一次的名字（兩次錄到的
    東西存成同一個檔）；別的巨集／排程正在送合成輸入時開錄，會把那些輸入錄成使用者的操作。
    兩條拒絕的訊息刻意分開斷言：「已經在錄」那一條拿掉的話，閘門那一條也會擋下，只是講錯
    原因——只看有沒有擋，那一條拿掉會照樣綠。監聽的開與關都是記錄用的絆線。"""
    replies: list = []
    hooks: list = []

    async def _reply(_message, content=None, **_kw):
        replies.append(content)

    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_MACRO_RECORDING", recording)
    monkeypatch.setattr(b, "_MACRO_RUNNING", running)
    monkeypatch.setattr(b._gui, "record_start", lambda: hooks.append("start"))
    monkeypatch.setattr(b._gui, "record_stop", lambda: hooks.append("stop") or [])
    monkeypatch.setattr(b, "_schedule_coro", lambda coro, **_kw: hooks.append("watchdog"))
    message = types.SimpleNamespace(author=types.SimpleNamespace(id=b.OWNER_USER_ID),
                                    channel=types.SimpleNamespace(id=_RW_CHANNEL))
    _sr_run(b._macro_record(message, name))
    assert hooks == [], hooks
    assert len(replies) == 1 and expected in replies[0], replies
    assert b._MACRO_RECORDING == recording and b._MACRO_RUNNING == running


def test_starting_a_recording_hands_the_watchdog_its_channel_owner_and_sequence(
        monkeypatch):
    """開始錄製的那一刻要把「講給誰聽」與序號交給計時器——之後就沒有訊息物件了。"""
    replies: list = []
    scheduled: list = []

    async def _reply(_message, content=None, **_kw):
        replies.append(content)

    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_MACRO_RECORDING", None)
    monkeypatch.setattr(b, "_MACRO_RUNNING", None)
    monkeypatch.setattr(b, "_MACRO_RECORDING_SEQ", 41)
    monkeypatch.setattr(b._gui, "record_start", lambda: None)
    monkeypatch.setattr(b, "_macro_record_watchdog",
                        lambda name, **kwargs: ("watchdog", name, kwargs))
    monkeypatch.setattr(b, "_schedule_coro",
                        lambda coro, **_kw: scheduled.append(coro))
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID),
        channel=types.SimpleNamespace(id=_RW_CHANNEL))
    _sr_run(b._macro_record(message, "rec2"))
    assert scheduled == [("watchdog", "rec2", {
        "seq": 42, "channel_id": _RW_CHANNEL, "user_id": b.OWNER_USER_ID,
        "conversation": None})]
    assert b._MACRO_RECORDING == "rec2"
    assert b._MACRO_RECORDING_SEQ == 42
    assert replies and replies[0].startswith("⏺️ 開始錄製 `rec2`")


# ---------------------------------------------------------------------------
# 分支覆蓋率盤點（2026-09-21）：判斷有做、卻從來沒擋下過任何東西的三道守門
# ---------------------------------------------------------------------------
# ⭐/🗑️ 反應的頻道閘與「bot 自己的反應」閘，以及 `/out latest_for` 的路徑穿越閘，
# 在整套測試裡一次都沒走過拒絕那一邊。🗑️ 會刪主機上的檔、`latest_for` 會把檔案上傳
# 出去，所以這三道擋的都是不可逆的事。

def _reaction_env(monkeypatch, tmp_path):
    out_root = tmp_path / "output"
    (out_root / "someone").mkdir(parents=True)
    target = out_root / "someone" / "a.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(b, "OUTPUT_ROOT", out_root)
    monkeypatch.setattr(b, "_IMAGE_MSG_ROOTS", (out_root,))
    monkeypatch.setattr(b, "RECENT_IMAGE_MSGS_FILE", tmp_path / "recent_image_msgs.json")
    monkeypatch.setattr(b, "_RECENT_IMAGE_MSGS", {111: [target]})
    monkeypatch.setattr(b, "_load_favorites", lambda: {})
    monkeypatch.setattr(b, "_save_favorites", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "client", types.SimpleNamespace(
        user=types.SimpleNamespace(id=777_000_777),
        get_channel=lambda *_a, **_k: None))
    return target


def test_a_trash_reaction_in_another_channel_deletes_nothing(monkeypatch, tmp_path):
    target = _reaction_env(monkeypatch, tmp_path)
    elsewhere = types.SimpleNamespace(channel_id=b.CHANNEL_ID + 1, message_id=111,
                                      user_id=1, emoji=b.DEL_EMOJI)
    asyncio.run(b.on_raw_reaction_add(elsewhere))
    assert target.exists(), "別的頻道裡的 🗑️ 刪掉了主機上的檔"
    # 正面對照：同一個反應放在設定的頻道就要刪得掉，否則「一律什麼都不做」也會通過。
    here = types.SimpleNamespace(channel_id=b.CHANNEL_ID, message_id=111,
                                 user_id=1, emoji=b.DEL_EMOJI)
    asyncio.run(b.on_raw_reaction_add(here))
    assert not target.exists()


def test_the_bots_own_trash_reaction_deletes_nothing(monkeypatch, tmp_path):
    target = _reaction_env(monkeypatch, tmp_path)
    own = types.SimpleNamespace(channel_id=b.CHANNEL_ID, message_id=111,
                                user_id=777_000_777, emoji=b.DEL_EMOJI)
    asyncio.run(b.on_raw_reaction_add(own))
    assert target.exists(), "bot 自己加的 🗑️ 被當成使用者的刪除要求"


@pytest.mark.parametrize("name", ["../secret", "..\\secret", "C:secret"])
def test_latest_for_refuses_a_folder_outside_the_output_root(monkeypatch, tmp_path, name):
    """`OUTPUT_ROOT / "../secret"` 會走出產出樹，而這個指令會把找到的圖**上傳出去**。
    擋下來的回覆不得帶附件；正面對照放在同一支，否則「一律回 invalid name」也會通過。"""
    out_root = tmp_path / "output"
    (out_root / "alice").mkdir(parents=True)
    (out_root / "alice" / "ok.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "private.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(b, "OUTPUT_ROOT", out_root)
    monkeypatch.setattr(b, "_remember_image_msg", lambda *_a, **_k: None)
    sent: list = []

    async def _recorder(_message, content=None, **kw):
        sent.append((content, kw.get("file")))
        if kw.get("file") is not None:
            kw["file"].close()

    monkeypatch.setattr(b, "safe_reply", _recorder)
    message = types.SimpleNamespace(guild=None)
    asyncio.run(b.cmd_latest_for(message, name))
    assert sent == [("invalid name", None)], sent
    sent.clear()
    asyncio.run(b.cmd_latest_for(message, "alice"))
    assert len(sent) == 1 and sent[0][1] is not None, sent


@pytest.mark.parametrize("payload, expected", [
    ("alice", ("alice", 1)),
    ("alice 3", ("alice", 3)),
    ("alice 99", ("alice", 5)),
    ("alice 0", ("alice", 1)),
    ("a b 2", ("a b", 2)),
    ("3", ("", 3)),
    ("", ("", 1)),
    ("alice \u00b2", ("alice \u00b2", 1)),
    ("alice \u0663", ("alice \u0663", 1)),
])
def test_split_trailing_count(payload, expected):
    """結尾是 ASCII 數字才算張數；上標或其他文字的數字不算，否則 `int()` 會丟例外。"""
    assert b._split_trailing_count(payload, 5) == expected


def test_isdigit_is_never_the_gate_in_front_of_int():
    """`'²'.isdigit()` 是 True、`int('²')` 丟 ValueError。守門用 `isdecimal()`
    （或先 `isascii()`）——前者保證 `int()` 吃得下。以 AST 找「同一個函式裡既有
    `X.isdigit()` 又有 `int(...)`」的地方。"""
    import ast
    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        has_int = any(isinstance(c.func, ast.Name) and c.func.id == "int" for c in calls)
        bare_isdigit = [
            c for c in calls
            if isinstance(c.func, ast.Attribute) and c.func.attr == "isdigit"]
        if has_int and bare_isdigit:
            guarded = any(
                isinstance(c.func, ast.Attribute) and c.func.attr == "isascii"
                for c in calls)
            if not guarded:
                offenders.append(fn.name)
    assert offenders == [], offenders


def test_fav_show_needs_a_character(monkeypatch):
    """`!fav show 3` 的名稱是空的：回用法，而不是「`` 沒有收藏」。"""
    sent: list = []

    async def _recorder(_message, content=None, **_kw):
        sent.append(content)

    monkeypatch.setattr(b, "safe_reply", _recorder)
    monkeypatch.setattr(b, "_load_favorites", lambda: {"": ["x.png"]})
    asyncio.run(b.cmd_fav_show(types.SimpleNamespace(guild=None), "3"))
    assert len(sent) == 1 and sent[0].startswith("usage"), sent


def test_sample_needs_a_character_and_reads_only_ascii_counts(monkeypatch, tmp_path):
    """`!sample 3` 的名稱是空字串，而空字串被 `_is_unsafe_folder_name` 刻意放行——
    不擋的話抽的是產出根目錄本身。`²` 讓 `isdigit()` 回 True、`int()` 丟例外。"""
    out_root = tmp_path / "output"
    (out_root / "alice").mkdir(parents=True)
    (out_root / "alice" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (out_root / "stray.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(b, "OUTPUT_ROOT", out_root)
    monkeypatch.setattr(b, "_remember_image_msg", lambda *_a, **_k: None)
    sent: list = []

    async def _recorder(_message, content=None, **kw):
        files = kw.get("files") or []
        sent.append((content, len(files)))
        for f in files:
            f.close()

    monkeypatch.setattr(b, "safe_reply", _recorder)
    message = types.SimpleNamespace(guild=None)
    asyncio.run(b.cmd_sample(message, "3"))
    assert len(sent) == 1 and sent[0][1] == 0 and "usage" in sent[0][0], sent
    sent.clear()
    asyncio.run(b.cmd_sample(message, "alice \u00b2"))
    assert sent == [("folder `alice ²/` not found", 0)], sent
    sent.clear()
    asyncio.run(b.cmd_sample(message, "alice 2"))
    assert len(sent) == 1 and sent[0][1] == 1, sent


# ---------------------------------------------------------------------------
# 平台斷線重連：一次重連一行 log，不是一整段 traceback（2026-09-22）
# ---------------------------------------------------------------------------
def _reconnect_log_output(raise_it, message=None) -> str:
    """在掛了過濾器的 `discord.client` 記錄器上記一筆「重連失敗」，回格式化後的輸出。"""
    import logging

    logger = logging.getLogger("discord.client")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    saved_filters, saved_level, saved_propagate = list(logger.filters), logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        b._install_reconnect_log_filter()
        try:
            raise_it()
        except Exception:  # noqa: BLE001 -- the record needs a live exc_info
            logger.exception(message or b._RECONNECT_LOG_MESSAGE, 8.78)
    finally:
        logger.removeHandler(handler)
        logger.filters[:] = saved_filters
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate
    return stream.getvalue()


def _raise_dns_failure():
    raise OSError(11001, "getaddrinfo failed")


def test_a_reconnect_after_a_network_error_is_one_log_line():
    out = _reconnect_log_output(_raise_dns_failure)
    assert out.count("\n") == 1, out
    assert "Attempting a reconnect in 8.78s after OSError: " in out, out
    assert "getaddrinfo failed" in out, out
    assert "Traceback" not in out, out


def test_a_reconnect_after_the_real_library_dns_error_is_one_log_line():
    """函式庫實際丟的是 `ClientConnectorDNSError`，建構方式跟著版本變——用真的那一個，
    不然摘要的形狀只在替身上成立。"""
    def _raise():
        conn_key = b.aiohttp.client_reqrep.ConnectionKey
        key = conn_key(**{**dict.fromkeys(conn_key._fields),
                          "host": "gateway.example", "port": 443,
                          "is_ssl": True, "ssl": True})
        raise b.aiohttp.ClientConnectorDNSError(key, OSError(11001, "getaddrinfo failed"))

    out = _reconnect_log_output(_raise)
    assert out.count("\n") == 1, out
    assert "after ClientConnectorDNSError: Cannot connect to host gateway.example:443" in out, out


def test_an_unrecognised_reconnect_error_keeps_its_traceback():
    """不在網路例外清單裡的型別：照舊整段印出來——那是函式庫換了行為的唯一線索。"""
    def _raise():
        raise RuntimeError("something new")

    out = _reconnect_log_output(_raise)
    assert "Traceback" in out and "RuntimeError: something new" in out, out


def test_other_records_with_a_network_error_keep_their_traceback():
    """過濾器只碰「重連失敗」那一句：同一個記錄器上的其他例外紀錄不能被順手壓掉。"""
    out = _reconnect_log_output(_raise_dns_failure, message="Some other failure %.2f")
    assert "Traceback" in out, out


def test_the_reconnect_filter_is_installed_once():
    import logging

    logger = logging.getLogger("discord.client")
    saved = list(logger.filters)
    try:
        b._install_reconnect_log_filter()
        b._install_reconnect_log_filter()
        mine = [f for f in logger.filters if isinstance(f, b._ReconnectTracebackFilter)]
        assert len(mine) == 1, logger.filters
    finally:
        logger.filters[:] = saved


def test_the_reconnect_filter_matches_the_library_contract():
    """記錄器名稱與訊息字串都是函式庫的：它改了，過濾器就安靜地什麼都不做，所以直接對著
    它的原始碼釘。"""
    import inspect
    import logging

    import discord.client

    assert discord.client._log is logging.getLogger("discord.client")
    source = inspect.getsource(discord.client.Client.connect)
    assert f"_log.exception('{b._RECONNECT_LOG_MESSAGE}'" in source \
        or f'_log.exception("{b._RECONNECT_LOG_MESSAGE}"' in source, (
            "the library no longer logs the reconnect with this message")


def test_main_installs_the_reconnect_filter_before_connecting(monkeypatch):
    """過濾器要在 `client.run` 之前掛上——之後才掛，第一段斷線照樣整段印。"""
    import logging

    logger = logging.getLogger("discord.client")
    saved = list(logger.filters)
    seen: list = []

    class _Lock:
        # `InstanceLock` 的替身。`degraded` 不是可有可無的——`main()` 取的鎖有兩把
        # （本體的單一實例鎖、批次的監督權），兩把都會讀它。
        degraded = False

        def release(self):
            pass

    def _run(_token):
        seen.append(any(isinstance(f, b._ReconnectTracebackFilter) for f in logger.filters))

    try:
        logger.filters[:] = [f for f in saved if not isinstance(f, b._ReconnectTracebackFilter)]
        monkeypatch.setattr(b, "acquire_single_instance_lock", lambda _p: _Lock())
        monkeypatch.setattr(b, "_log_code_fingerprint", lambda: None)
        # 啟動前的設定檢查另有自己的測試；這一支問的是過濾器掛在哪一步，所以把它
        # 換掉。不換的話，一份沒有 `bot_config.json` 的工作目錄會讓 `main()` 在讀
        # token 之前就回 `RC_SETUP_INCOMPLETE`，而這支測試會報一個跟它無關的原因。
        monkeypatch.setattr(b, "check_setup", lambda: None)
        monkeypatch.setattr(b, "read_token", lambda _p: "token")
        monkeypatch.setattr(b.client, "run", _run)
        monkeypatch.setattr(b._gui, "job_stop_all", lambda: 0)
        monkeypatch.setattr(b._gui, "release_all_inputs", lambda: None)
        assert b.main() == 0
    finally:
        logger.filters[:] = saved
    assert seen == [True], seen


# ---------------------------------------------------------------------------
# 處理函式自己的擁有者檢查：拒絕那一邊要真的跑過（2026-09-22）
#
# 派發層的 `_OWNER_ONLY_GROUPS` 是第一道、有完整的守門；這十幾個處理函式開頭各自還有一道
# `if not _is_owner(message)`。分支覆蓋率量到其中四道（`mcmd_generate`、`_macro_record`、
# `cmd_schedule`、`cmd_panic`）的**拒絕那一邊從來沒有執行過**——刪掉照樣全綠。清單用 AST
# 推導，新的處理函式加了同樣的檢查就自動納管。
#
# 第一版只認 `_is_owner(`／`OWNER_USER_ID`，於是 Dorossi 那一組（`_dorossi_owner_only(`、
# `DOROSSI_USER_ID`，共 16 個）整組不在推導裡，其中 `mcmd_dorossi` 自己的拒絕邊也從來沒跑過——
# 而它是能在主機上跑指令的那個入口。兩個 id 現在是同一個值，但名字不同，推導是照名字找的。
# ---------------------------------------------------------------------------
_OWNER_DENIALS = frozenset({
    b.OWNER_ONLY_DENIED, "❌ 你沒有權限使用這個指令。",
    "Dorossi 僅限特定使用者使用。", "此功能僅限特定使用者。",
})
_INLINE_OWNER_CHECK_MARKERS = ("_is_owner(", "_dorossi_owner_only(", "OWNER_USER_ID", "DOROSSI_USER_ID")


def _inline_owner_gated_handlers() -> list[str]:
    import ast

    tree = ast.parse(Path(b.__file__).read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if not (isinstance(node, ast.AsyncFunctionDef) and node.args.args
                and node.args.args[0].arg == "message"):
            continue
        # docstring 與 `global` 不算「開頭」：`mcmd_abort` 的檢查前面是三行 `global`，照句數算就掉出去了。
        real = [s for s in node.body
                if not isinstance(s, (ast.Global, ast.Nonlocal))
                and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        for stmt in real[:3]:
            if isinstance(stmt, ast.If):
                test = ast.unparse(stmt.test)
                if any(marker in test for marker in _INLINE_OWNER_CHECK_MARKERS):
                    names.append(node.name)
                    break
    return names


# 餵給每個處理函式的參數。**不要改回 "stop" 這類動詞**：檢查一旦失效（真的退化，或變異測試），
# `cmd_launcher("stop")` 會掃到主機上正在跑的獨立監督者並把它們結束掉——那是正式批次的監督者。
# 一個沒有任何處理函式認得的字串，最壞只會換來一句用法說明。
_OWNER_GATE_PROBE_ARG = "__owner_gate_probe__"


def _install_host_tripwires(monkeypatch, name: str, touched: list) -> None:
    """把生子行程與結束行程的入口換成「記下來再丟」的替身。

    記下來是因為處理函式裡多半有 `except Exception`，只丟的替身會被吞掉。`taskkill` 走
    `subprocess.run`，所以 `Popen` 一個就攔得住；psutil 與 `os.kill` 是另外兩條路。"""
    import subprocess

    import psutil

    def _no(label):
        def _hit(*_a, **_kw):
            touched.append(label)
            raise AssertionError(f"{name} tried {label} for a non-owner")
        return _hit

    for owner, attr in ((asyncio, "create_subprocess_exec"), (asyncio, "create_subprocess_shell"),
                        (subprocess, "Popen"), (os, "startfile"), (os, "kill"),
                        (psutil.Process, "terminate"), (psutil.Process, "kill")):
        label = f"{getattr(owner, '__name__', owner)}.{attr}"
        monkeypatch.setattr(owner, attr, _no(label), raising=False)


def _redirect_root_state_files(monkeypatch, tmp_path) -> list[str]:
    """`discord_bot`／`dorossi_backend` 裡每一個落在 repo 根目錄的 `Path` 常數都導到 tmp。

    檢查失效時 `mcmd_dorossi` 會一路寫到正式的 `dorossi_session.json`（量過：三次，全靠
    conftest 那道「不准寫進 repo」擋下來）。逐一點名會漏，所以照「在根目錄底下」推導。

    **`state/<平台>/` 也算「根目錄底下」。** bot 自己的狀態檔改成逐平台之後就搬進那個
    子目錄了，而這支原本的條件是「父目錄剛好等於 repo root」——於是
    `dorossi_session.json`、`dorossi_queue.ndjson`、`schedules.json` 一夕之間全部落在
    推導之外，而失敗的方式是**測試自己去寫正式檔案**，不是一句話說不清的紅字。
    範圍刻意只放寬到 `state/` 這一層，不是「root 底下的任何東西」：`WEBRUNNER_SCRIPT`
    之類指向套件檔案的常數導去暫存區只會讓下游 spawn 指向一個不存在的檔。"""
    moved = []
    for mod in (b, db):
        root = Path(mod.PROJECT_ROOT).resolve()
        state_root = root / "state"
        for attr, value in list(vars(mod).items()):
            if not (attr.isupper() and attr != "PROJECT_ROOT"
                    and isinstance(value, Path)):
                continue
            parent = value.parent.resolve()
            if parent != root and state_root not in parent.parents \
                    and parent != state_root:
                continue
            monkeypatch.setattr(mod, attr, tmp_path / value.name)
            moved.append(f"{mod.__name__}.{attr}")
    return moved


def test_the_host_tripwires_catch_every_spawn_and_kill_form(monkeypatch):
    """替身本身的對照：每一種寫法都被攔下、記下，而且被瞄準的行程真的還活著。

    瞄準的是這支測試自己生的子行程，所以替身萬一失效，受害的也只有它。"""
    import subprocess

    import psutil

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        touched: list = []
        real_kill = os.kill
        _install_host_tripwires(monkeypatch, "control", touched)
        attempts = (
            lambda: subprocess.run([sys.executable, "-c", "pass"], check=False),
            lambda: psutil.Process(child.pid).terminate(),
            lambda: psutil.Process(child.pid).kill(),
            lambda: asyncio.create_subprocess_exec(sys.executable, "-c", "pass"),
            lambda: asyncio.create_subprocess_shell("exit 0"),
        )
        for attempt in attempts:
            with pytest.raises(AssertionError):
                attempt()
        assert len(touched) == len(attempts), touched
        # `os.kill` 刻意不直接呼叫：`test_suite_safety` 禁止測試裡出現它——替身萬一沒裝上，
        # 那一行就是真的訊號。改確認它換成了同一個工廠做出來的替身（記錄行為上面已經量過）。
        assert os.kill is not real_kill, "os.kill 沒有被換掉"
        assert os.kill.__qualname__.endswith("_no.<locals>._hit"), os.kill.__qualname__
        monkeypatch.undo()
        assert child.poll() is None, "a tripwire let a kill through"
    finally:
        child.kill()
        child.wait(timeout=10)


def test_the_inline_owner_check_derivation_finds_the_handlers():
    """推導抽不到東西時，下面那支參數化測試是「0 筆」而不是紅燈——先釘下限與幾個一定在的。"""
    names = _inline_owner_gated_handlers()
    assert len(names) >= 29, names
    assert {"mcmd_generate", "_macro_record", "cmd_schedule", "cmd_panic", "cmd_sh",
            "mcmd_dorossi", "mcmd_fullmode", "mcmd_allowdir", "mcmd_session", "mcmd_abort"} <= set(names)


@pytest.mark.parametrize("name", _inline_owner_gated_handlers())
def test_every_inline_owner_check_actually_denies(name, monkeypatch, tmp_path):
    """非擁有者呼叫：第一句、也是唯一一句回覆就是拒絕，而且碰不到桌面自動化、生不出也殺不了行程。

    替身是「記下來再丟」而不是只丟：處理函式裡多半有 `except Exception`，只丟的替身會被
    吞掉，檢查失效時就只剩回覆數量那一條在擋。行程那幾個入口也換掉——檢查一旦失效，
    `cmd_sh` 會真的開 shell、`mcmd_dorossi` 會往後端走，測試本身不能變成副作用。"""
    import inspect

    replies: list = []
    touched: list = []

    async def _reply(_message, content=None, **_kw):
        replies.append(content)

    class _Tripwire:
        def __getattr__(self, attr):
            touched.append(f"_gui.{attr}")
            raise AssertionError(f"{name} reached _gui.{attr} for a non-owner")

    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_gui", _Tripwire())
    _install_host_tripwires(monkeypatch, name, touched)
    moved = _redirect_root_state_files(monkeypatch, tmp_path)
    assert {"dorossi_backend.DOROSSI_SESSION_FILE", "discord_bot.DOROSSI_QUEUE_FILE",
            "discord_bot.SCHEDULE_FILE", "discord_bot.TODO_PROMPT_FILE"} <= set(moved), moved
    assert db.DOROSSI_SESSION_FILE.parent == tmp_path  # 名字記下了，路徑也真的換了
    stranger = max(b.OWNER_USER_ID, b.DOROSSI_USER_ID) + 1  # 兩個 id 分開之後也不會撞到其中一個
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(id=stranger), attachments=[],
        guild=None, channel=types.SimpleNamespace(id=b.CHANNEL_ID))
    handler = getattr(b, name)
    extra = len(inspect.signature(handler).parameters) - 1
    _sr_run(handler(message, *([_OWNER_GATE_PROBE_ARG] * extra)))
    assert not touched, (name, touched)
    assert replies and replies[0] in _OWNER_DENIALS, (name, replies)
    assert len(replies) == 1, (name, replies)


def test_the_owner_is_an_admin_even_when_the_role_lists_do_not_name_them(monkeypatch):
    """`_user_role` 的第一行（擁有者＝admin）在整個套件裡從來沒有成立過。角色清單全空
    是預設、也是現況，擁有者照樣要是 admin。"""
    monkeypatch.setattr(b, "_role_ids", lambda _key: set())
    assert b._user_role(b.OWNER_USER_ID) == "admin"
    assert b._user_role(b.OWNER_USER_ID + 1) != "admin"


# ---------------------------------------------------------------------------
# 上限的拒絕那一邊（2026-09-22 分支覆蓋率）
#
# 產圖的三道上限、監看與排程的數量上限，拒絕那一邊在整個套件裡**一次都沒有成立過**：把
# 任何一道刪掉、或把 `>=` 改成 `>`，照樣全綠。每一道都量兩格——剛好滿（拒絕）與差一格
# （放行）——後者擋的是差一的改法，也證明拒絕是那道上限給的，不是別的條件。
# ---------------------------------------------------------------------------
class _PastTheGenerateGuards(Exception):
    """「三道上限都放行了」的記號：`_generate_request_id` 是上限之後第一個被呼叫的東西。"""


def _generate_guard_outcome(monkeypatch, pending_users, queued: int) -> str:
    replies: list = []

    async def _reply(_message, content=None, **_kw):
        replies.append(content)

    def _past():
        raise _PastTheGenerateGuards

    monkeypatch.setattr(b, "safe_reply", _reply)
    monkeypatch.setattr(b, "_sweep_stale_single_image_state", lambda *_a, **_k: None)
    monkeypatch.setattr(b, "_generate_request_id", _past)
    monkeypatch.setattr(b, "_single_image_pending",
                        {f"r{i}": {"user_id": uid} for i, uid in enumerate(pending_users)})
    monkeypatch.setattr(b, "_generate_queue", [object()] * queued)
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID), id=1, attachments=[],
        guild=None, channel=types.SimpleNamespace(id=b.CHANNEL_ID))
    try:
        _sr_run(b.mcmd_generate(message, "a cat"))
    except _PastTheGenerateGuards:
        assert replies == [], replies
        return "放行"
    else:
        # 沒走到記號就一定是被某道上限擋下，而且只回了那一句。
        assert len(replies) == 1, replies
        return replies[0]


_ME = b.OWNER_USER_ID


@pytest.mark.parametrize("pending_users, queued, expected", [
    ((), 0, "放行"),
    ((_ME,), 0, "你已經有一張圖在處理中了"),
    ((_ME + 1,), 0, "放行"),                                   # 別人的那一筆不算我的
    ((), b._GENERATE_MAX_WAITING, "目前產圖佇列已滿"),
    ((), b._GENERATE_MAX_WAITING - 1, "放行"),
    ((_ME + 1,) * b._SINGLE_IMAGE_PENDING_MAX, 0, "目前同時處理的請求過多"),
    ((_ME + 1,) * (b._SINGLE_IMAGE_PENDING_MAX - 1), 0, "放行"),
], ids=["empty", "mine-pending", "someone-elses", "queue-full", "queue-one-short",
        "map-full", "map-one-short"])
def test_each_generate_cap_refuses_exactly_at_its_limit(monkeypatch, pending_users, queued, expected):
    got = _generate_guard_outcome(monkeypatch, pending_users, queued)
    if expected == "放行":
        assert got == "放行", got
    else:
        assert expected in got, (expected, got)


@pytest.mark.parametrize("existing", [b.WATCH_MAX, b.WATCH_MAX - 1])
def test_the_watch_cap_refuses_one_more_and_admits_up_to_it(monkeypatch, existing):
    replies: list = []

    async def _rec(_message, content=None, **_kw):
        replies.append(content)

    async def _fake_loop(*_a, **_kw):
        return None

    monkeypatch.setattr(b, "safe_reply", _rec)
    monkeypatch.setattr(b, "_watch_loop", _fake_loop)
    monkeypatch.setattr(b, "_WATCHES", {
        i: {"label": "x", "started": 0.0, "task": None} for i in range(1, existing + 1)})
    monkeypatch.setattr(b, "_WATCH_NEXT_ID", existing + 1)
    message = types.SimpleNamespace(
        author=types.SimpleNamespace(id=b.OWNER_USER_ID, mention="<@1>"))

    async def _body():
        await b.cmd_watch(message, "text 完成")
        await asyncio.sleep(0)

    asyncio.run(_body())
    assert len(replies) == 1, replies
    if existing >= b.WATCH_MAX:
        assert f"同時最多 {b.WATCH_MAX} 個監看" in replies[0], replies
        assert len(b._WATCHES) == existing
    else:
        assert "同時最多" not in replies[0], replies
        assert len(b._WATCHES) == existing + 1, "差一格的時候應該要收下"


def _seeded_schedules(count: int) -> dict:
    return {"version": 1, "next_id": count + 1, "entries": [
        {"id": i, "when_kind": "every", "when_value": "3600", "kind": "sh",
         "payload": "true", "channel_id": 1, "user_id": 7, "last_run": 0.0, "last_date": ""}
        for i in range(1, count + 1)]}


@pytest.mark.parametrize("existing", [b.SCHEDULE_MAX, b.SCHEDULE_MAX - 1])
def test_the_schedule_cap_refuses_one_more_and_admits_up_to_it(monkeypatch, tmp_path, existing):
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    b._save_schedules(_seeded_schedules(existing))
    message, sent = _schedule_test_message(monkeypatch)

    asyncio.run(b.cmd_schedule(message, "add 09:30 sh echo hi"))
    count = len(b._load_schedules()["entries"])
    if existing >= b.SCHEDULE_MAX:
        assert sent == [f"❌ 排程最多 {b.SCHEDULE_MAX} 筆"], sent
        assert count == existing
    else:
        assert "已建立排程" in sent[-1], sent
        assert count == existing + 1


def test_a_full_schedule_says_so_before_it_validates_the_macro(monkeypatch, tmp_path):
    """鎖外那道快速檢查的可觀察差別：排程已滿時，就算巨集也打錯了，先講「滿了」。

    只看拒絕句的話，拿掉這道照樣全綠——鎖裡那道重讀會回同一句話，兩道互相遮蔽。真正的
    差別是順序：快速檢查在巨集驗證**之前**，所以使用者收到的是真正擋住他的那件事，而不是
    修好巨集名稱之後才發現原來早就滿了。"""
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    b._save_schedules(_seeded_schedules(b.SCHEDULE_MAX))
    checked: list = []

    def _bad_macro(name, args):
        checked.append(name)
        raise b._GuiError("找不到這個巨集。")

    monkeypatch.setattr(b, "_check_stored_macro", _bad_macro)
    message, sent = _schedule_test_message(monkeypatch)

    asyncio.run(b.cmd_schedule(message, "add 09:30 macro nosuch"))
    assert sent == [f"❌ 排程最多 {b.SCHEDULE_MAX} 筆"], sent
    assert checked == [], "排程已滿還去驗巨集"


def test_the_schedule_cap_is_rechecked_on_the_copy_read_under_the_lock(monkeypatch, tmp_path):
    """頂端那次檢查用的是鎖外的快照；巨集驗證跑在工作執行緒上的那段空檔，排程檔可能已經
    滿了。作數的是鎖裡重讀的那一份——這一支讓第一次讀到 19 筆、第二次讀到 20 筆。"""
    monkeypatch.setattr(b, "SCHEDULE_FILE", tmp_path / "sched.json")
    reads: list = []
    saves: list = []

    def _load():
        reads.append(1)
        return _seeded_schedules(b.SCHEDULE_MAX - 1 if len(reads) == 1 else b.SCHEDULE_MAX)

    monkeypatch.setattr(b, "_load_schedules", _load)
    monkeypatch.setattr(b, "_save_schedules", saves.append)
    message, sent = _schedule_test_message(monkeypatch)

    asyncio.run(b.cmd_schedule(message, "add 09:30 sh echo hi"))
    assert len(reads) == 2, f"前提：頂端讀一次、鎖裡再讀一次，實際 {len(reads)} 次"
    assert sent == [f"❌ 排程最多 {b.SCHEDULE_MAX} 筆"], sent
    assert saves == [], "鎖裡重讀已經滿了，還是寫回去了"



def test_the_events_tail_of_a_big_file_keeps_only_whole_recent_lines(monkeypatch, tmp_path):
    """`_read_events_tail` 超過位元組預算時只讀尾端——那條路徑在整個套件裡從來沒跑過。

    每行都帶中文，讓起跳點有機會落在一個多位元組字元的中間（文字模式、`errors="replace"`
    要吞得下那半個字）。斷言的是：回來的是**最後面一段連續的完整事件**、而且放得進預算。"""
    path = tmp_path / "events.ndjson"
    lines = [json.dumps({"type": "image_saved", "seq": i, "label": "角色名稱"},
                        ensure_ascii=False) for i in range(500)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(b, "EVENTS_FILE", path)

    budget = 1500
    seqs = [ev["seq"] for ev in b._read_events_tail(max_bytes=budget)]
    assert seqs and seqs[-1] == 499, seqs
    assert seqs == list(range(seqs[0], 500)), f"不是連續的尾段：{seqs}"
    assert seqs[0] > 0, "整個檔都讀進來了——預算沒有生效"
    used = sum(len(line.encode("utf-8")) + 1 for line in lines[seqs[0]:])
    assert used <= budget, (used, budget)
    # 對照：預算夠大時一行不少，證明上面那段短是預算造成的。
    assert len(b._read_events_tail(max_bytes=10**7)) == 500


@pytest.mark.parametrize("channel_id, expected", [
    (4242, 4242),
    (True, None), (False, None),        # bool 是 int 的子類別：`isinstance(True, int)` 成立
    (None, None), ("4242", None), (4242.0, None),
])
def test_a_scheduled_run_never_stores_a_non_integer_channel(monkeypatch, tmp_path, channel_id, expected):
    """延後啟動落地時，頻道 id 不是真的 int 就換成預設頻道。

    `bool` 那一條是重點：少了它，`True` 會以 `true` 落地，重啟後讀回來的是一個布林值——
    `isinstance(x, int) and …` 這種檢查只有 `True`／`False` 溜得過去，而隨手測通常不會想到
    餵它。這一邊在分支覆蓋率裡從來沒有成立過（2026-09-22）。"""
    target = tmp_path / "scheduled_run.json"
    monkeypatch.setattr(b, "SCHEDULED_RUN_FILE", target)
    assert b._save_scheduled_run(1_900_000_000.0, channel_id) is True
    stored = json.loads(target.read_text(encoding="utf-8"))["channel_id"]
    assert type(stored) is int, (channel_id, stored)
    assert stored == (b.CHANNEL_ID if expected is None else expected), (channel_id, stored)


if __name__ == "__main__":
    sys.exit(main())
