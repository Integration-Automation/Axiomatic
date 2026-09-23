"""`presence_probe.py` 的守門測試。

711 行、每幾秒被 bot 的 presence 迴圈叫一次，而在此之前只有零星幾處被別的測試檔
順帶碰到。它的每一條路徑都用「回 None」表示失敗，而 presence 的「回 None」跟
「現在真的沒在玩遊戲／沒在聽音樂」**長得一模一樣**——所以這個模組壞掉的預設症狀
就是「安靜地什麼都不顯示」，沒有人會發現。

這一點跟 `_external_apis` 那兩次事故是同一種病：失敗與空結果無法區分，於是外部
依賴一漂移就靜默失效。這裡釘的是兩件事——**失敗要吭聲**，以及**設定檔的解析不得
把空值當成有效值**（那會反過來卡在一個假狀態上）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))

import presence_probe as pp  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# `_warn_once` 的去重集合由 `conftest.py` 的 autouse 夾具在每支測試前後清掉——
# 那條規則跟著**模組**跑，不是跟著這個檔案跑，所以入口只該有一份。理由見那裡。


# ---------------------------------------------------------------------------
# SMTC 子行程：失敗必須留下痕跡
# ---------------------------------------------------------------------------

def test_a_failing_smtc_probe_says_why(capsys, monkeypatch):
    """子行程非 0 結束時要把它的 stderr 印出來。

    原本的寫法是 `stdout, _ = await proc.communicate()` 然後直接 `return None`
    ——stderr **被接了管線然後丟掉**，那比不接還糟：不接的話錯誤訊息至少會落到
    主控台，接了再丟就是誰都看不到。

    而這條路依賴的東西不少（PowerShell 5.1、WinRT 的 `Windows.Media.Control`、
    `Add-Type`、執行原則）。任何一個變了，症狀都只是「音樂狀態永遠是空的」，跟
    「現在沒在播音樂」無法區分——也就是這個 repo 今天已經踩過兩次的那種靜默失效。
    """
    monkeypatch.setattr(pp, "_PS_PROBE", 'throw "deliberate probe failure"')
    capsys.readouterr()
    assert _run(pp.probe_smtc_raw_async(timeout=30.0)) is None
    err = capsys.readouterr().err
    assert "rc=" in err, f"沒說子行程失敗了：{err!r}"
    assert "deliberate probe failure" in err, (
        f"子行程的 stderr 被丟掉了，只留下一個狀態碼：{err!r}")


def test_a_timed_out_smtc_probe_says_so(capsys, monkeypatch):
    """逾時也要吭聲，而且要把子行程收乾淨。

    這支每幾秒跑一次，逾時卻只 `kill()` 不 `wait()` 的話，殭屍會累積。
    """
    capsys.readouterr()
    assert _run(pp.probe_smtc_raw_async(timeout=0.001)) is None
    assert "timed out" in capsys.readouterr().err


def test_a_healthy_probe_stays_quiet(capsys):
    """反面：正常的一次探測不得印任何東西。

    每幾秒一行的話這條診斷會變成雜訊，然後就沒有人會再看它——回到靜默失效的原點。
    沒在播音樂時腳本會回 `{}`、rc=0，那是**正常**，不是故障。
    """
    capsys.readouterr()
    _run(pp.probe_smtc_raw_async(timeout=30.0))
    assert capsys.readouterr().err == "", "正常路徑印了東西"


def test_a_timed_out_child_is_actually_reaped(monkeypatch, capsys):
    """逾時之後不只要 `kill()`，還要 `wait()` 把它收掉。

    `kill()` 只是送出訊號；不 await 的話子行程會停在已結束但沒被回收的狀態。
    這支探測每幾秒跑一次，所以「偶爾漏收一個」累積起來是真的。用一個假的子行程
    觀察呼叫順序——真的去數作業系統的行程表太脆弱，而要釘住的本來就是這兩步都有做。
    """
    calls: list[str] = []

    class _HangingChild:
        returncode = None

        async def communicate(self):
            calls.append('communicate')
            await asyncio.sleep(30)     # 一定會逾時

        def kill(self):
            calls.append('kill')

        async def wait(self):
            calls.append('wait')
            return -1

    async def fake_exec(*args, **kwargs):
        return _HangingChild()

    monkeypatch.setattr(pp.asyncio, 'create_subprocess_exec', fake_exec)
    assert _run(pp.probe_smtc_raw_async(timeout=0.05)) is None
    assert 'kill' in calls, f'逾時沒有 kill 子行程：{calls}'
    assert 'wait' in calls, (
        f'kill 之後沒有 wait——子行程會留成殭屍，而這支每幾秒跑一次：{calls}')
    assert calls.index('kill') < calls.index('wait'), (
        f'順序反了，要先 kill 再 wait：{calls}')


# ---------------------------------------------------------------------------
# 收屍必須有上限——上面那支的假子行程 `wait()` 立刻返回，所以它證不到這一半
# ---------------------------------------------------------------------------
_FAST_REAP = 0.25


def _run_bounded(coro, deadline=_FAST_REAP * 12):
    """跑一段 coroutine 並**一定**帶牆鐘上限。

    上面的 `_run` 是裸的 `asyncio.run`。這一族測的正是「會不會永遠不返回」，而
    **一個掛住的測試比一個紅的測試更糟**（整輪停在那裡，沒有人知道是哪一支）。
    所以這裡另外包一層，逾時訊息直接指名這是回歸而不是機器慢。
    """
    async def _bounded():
        try:
            return await asyncio.wait_for(coro, timeout=deadline)
        except (asyncio.TimeoutError, TimeoutError):
            raise AssertionError(
                f"收屍在 {deadline:.1f}s 內沒有返回——收尾路徑上又出現無上限的等待了"
            ) from None

    return asyncio.run(_bounded())


class _PipeHeldChild:
    """`wait()` 等的是**管線**、`returncode` 卻與管線無關——實測到的平台語意。

    這個替身模擬的是「平台」不是「被測程式」：`_reap_timed_out_probe` 的行為不能
    拿來當它自己的規格（抄本永遠是綠的）。語意來源見 `_reap_timed_out_probe` 的
    docstring（`_try_finish` 要求 `all(p.disconnected)`，而 `_process_exited` 與
    管線無關）。
    """

    def __init__(self, *, rc_after: float | None = None):
        self.returncode = None
        self.waits = 0
        self._rc_after = rc_after
        self._started = None

    async def wait(self):
        self.waits += 1
        await asyncio.get_running_loop().create_future()   # 永遠不完成
        raise AssertionError("unreachable")

    def _tick(self):
        pass


def test_the_reap_gives_up_instead_of_hanging_when_the_pipes_are_held(
        monkeypatch):
    """孫行程握著管線 → `wait()` 永遠不返回。收屍必須在上限內放棄。

    這是舊寫法真正會出事的那一格，而上面
    `test_a_timed_out_child_is_actually_reaped` 的假子行程 `wait()` 立刻返回，
    所以它對這一半完全無感——把 `_reap_timed_out_probe` 換回裸的
    `await proc.wait()`，那一支照樣綠。

    後果不是「這一輪沒抓到音樂」，是 `_presence_probe_loop` 從此永久停住。
    """
    monkeypatch.setattr(pp, "_SMTC_REAP_TIMEOUT_SEC", _FAST_REAP)
    proc = _PipeHeldChild()
    leftovers = []

    async def _scenario():
        mine = asyncio.current_task()
        await pp._reap_timed_out_probe(proc)
        leftovers.extend(t for t in asyncio.all_tasks()
                         if t is not mine and not t.done())

    _run_bounded(_scenario())
    assert proc.waits == 1, f"根本沒有去 wait：{proc.waits}"
    assert leftovers == [], (
        f"逾時之後把 `wait()` 那個 task 丟著不管：{leftovers}。"
        "只加上限不收任務，等於把「永遠卡住」換成「每 8 秒多一個孤兒任務」。")


def test_the_reap_returns_as_soon_as_the_returncode_appears(monkeypatch):
    """行程死了但管線被握著 → `returncode` 問得到，不該白等滿整個上限。

    量的是**牆鐘時間**而不只是「有沒有返回」：只斷言「會返回」的話，把那一半
    「回頭看 returncode」拿掉照樣綠（它最後還是會逾時返回），而代價是每次收屍都
    多花一個上限，落在一個每幾秒跑一次的迴圈上。
    """
    monkeypatch.setattr(pp, "_SMTC_REAP_TIMEOUT_SEC", _FAST_REAP)
    proc = _PipeHeldChild()

    async def _scenario():
        loop = asyncio.get_running_loop()
        loop.call_later(0.02, lambda: setattr(proc, "returncode", 7))
        started = loop.time()
        await pp._reap_timed_out_probe(proc)
        return loop.time() - started

    elapsed = _run_bounded(_scenario())
    assert elapsed < _FAST_REAP, (
        f"收屍花了 {elapsed:.2f}s（上限 {_FAST_REAP}s）——看起來是等到逾時才返回，"
        "「同時盯 returncode」那一半不見了")


def test_the_reap_is_a_no_op_when_the_child_is_already_gone():
    """`returncode` 已經有了就**完全不碰事件迴圈**：不建 task、不 await。

    判準是「有沒有讓出控制權」而不是「`wait()` 被叫了幾次」，這是變異測試逼出來
    的：把提前返回拿掉之後，`ensure_future(proc.wait())` 建出來的 task 會在跑第
    一步之前就被 `finally` 取消掉，所以 `waits` **仍然是 0**——用次數去斷言，那個
    變異會存活。真正變掉的是「這支從同步返回變成要繞事件迴圈一圈」，而它落在一個
    每 8 秒跑一次、且收尾時間會直接加進週期的迴圈上。

    量法：先排一個 `call_soon` 回呼。被測程式若一次都沒 await，回呼就沒有機會執行。
    **回傳的是快照不是那個 list 本身**——第一版寫 `return ticks`，而排在佇列裡的
    回呼會在 `asyncio.run` 收工前跑掉，把同一個 list 改掉，於是乾淨的程式碼也讀到
    `[1]`。要量的是「返回的那一瞬間」，所以在返回點就複製。
    """
    proc = _PipeHeldChild()
    proc.returncode = 0

    async def _scenario():
        ticks = []
        asyncio.get_running_loop().call_soon(ticks.append, 1)
        await pp._reap_timed_out_probe(proc)
        return list(ticks)

    ticks = _run_bounded(_scenario())
    assert ticks == [], (
        "行程已經結束了，收屍卻還是繞了事件迴圈一圈（多建一個 task 再取消）。"
        "正常路徑要走那個提前返回。")
    assert proc.waits == 0, "行程已經結束了還真的去 wait()"


# ---------------------------------------------------------------------------
# 設定檔解析：空值不得被當成有效值
# ---------------------------------------------------------------------------

@pytest.fixture
def games_file(tmp_path, monkeypatch):
    path = tmp_path / "presence_games.json"
    monkeypatch.setattr(pp, "GAMES_FILE", path)
    return path


def test_an_empty_key_never_becomes_a_whitelist_entry(games_file):
    """空的 exe key 不得進白名單。

    這條有具體的後果，不是潔癖：`psutil` 取不到名稱的行程（權限不足）算出來的
    name 也是 `""`，於是白名單裡的 `""` 會**命中每一個存取被拒的行程**，presence
    就卡在一個根本沒在跑的假遊戲上。`"*"` 只有 priority 標記、剝掉之後也是空的，
    同樣要擋。
    """
    games_file.write_text(json.dumps({
        "": "Ghost Game", "*": "Also Ghost", "  ": "Whitespace Ghost",
        "real.exe": "Real Game",
    }), encoding="utf-8")
    wl = pp._load_game_whitelist()
    assert "" not in wl, f"空 key 進白名單了：{wl}"
    assert wl == {"real.exe": "Real Game"}


def test_an_empty_display_name_never_becomes_a_whitelist_entry(games_file):
    """顯示名稱是空的／不是字串也要跳過，否則狀態會顯示成 `Playing None`。"""
    games_file.write_text(json.dumps({
        "a.exe": "", "b.exe": None, "c.exe": [], "d.exe": "OK",
    }), encoding="utf-8")
    assert pp._load_game_whitelist() == {"d.exe": "OK"}


def test_priority_and_whitelist_normalise_identically(games_file):
    """priority 與白名單的正規化必須一模一樣，否則 priority 會靜默失效。

    `"* endfield.exe"`（星號後面多一個空白）在兩邊算出不同字串的話，priority
    清單裡的名字永遠對不上白名單的鍵，那個遊戲就再也贏不了音樂偵測——而且不會有
    任何錯誤訊息。
    """
    games_file.write_text(json.dumps({
        "* endfield.exe": "Endfield", "*SkyrimSE.EXE": "Skyrim",
        "plain.exe": "Plain",
    }), encoding="utf-8")
    wl = pp._load_game_whitelist()
    priority = pp._load_priority_games()
    assert priority == ["endfield.exe", "skyrimse.exe"]
    for name in priority:
        assert name in wl, (
            f"priority 的 {name!r} 在白名單裡找不到——兩邊的正規化不一致，"
            f"priority 會靜默失效。白名單：{sorted(wl)}")


def test_priority_keeps_the_json_order(games_file):
    """priority 依 JSON 出現順序決定誰贏，所以順序不能被排序掉。"""
    games_file.write_text(json.dumps({
        "*zzz.exe": "Z", "*aaa.exe": "A",
    }), encoding="utf-8")
    assert pp._load_priority_games() == ["zzz.exe", "aaa.exe"]


def test_a_broken_games_file_is_not_fatal(games_file, capsys):
    """壞掉的 JSON 要回空 dict 並留一行，不得讓 presence 迴圈整條死掉。"""
    games_file.write_text("{ this is not json", encoding="utf-8")
    capsys.readouterr()
    assert pp._load_game_whitelist() == {}
    assert "parse" in capsys.readouterr().err


def test_a_missing_games_file_is_silent(games_file, capsys):
    """反面：檔案不存在是**正常**狀態（使用者沒設定遊戲），不得吵。"""
    capsys.readouterr()
    assert pp._load_game_whitelist() == {}
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("raw,expected", [
    ("  hello  ", "hello"),
    (5, "5"),
    (5.5, "5.5"),
    (True, ""),          # bool 是 int 的子類別，但它不是文字
    (None, ""),
    ([], ""),
    ({}, ""),
    (b"bytes", ""),
])
def test_as_text_is_the_only_string_gate(raw, expected):
    """外部來源（JSON／SMTC／視窗標題）的值都要經過 `_as_text`。

    直接對外部值呼叫 `.strip()` 在型別不如預期時會拋例外，而本模組的例外會讓
    presence 迴圈少掉一次 tick。非文字一律回空字串，讓呼叫端用 `if not text:`
    就能 fail-closed。
    """
    assert pp._as_text(raw) == expected


def test_a_bom_does_not_break_the_config(games_file):
    """帶 BOM 的 JSON 要讀得起來。

    Windows 上用 PowerShell `Set-Content -Encoding UTF8` 或舊編輯器存檔就會產生
    BOM；用 `utf-8` 讀會整份解析失敗然後靜默退回預設值，使用者只會覺得設定沒生效。
    """
    games_file.write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"a.exe": "A"}).encode("utf-8"))
    assert pp._load_game_whitelist() == {"a.exe": "A"}


# ===========================================================================
# 兩條優先序——這裡決定的是**別人看得到什麼**
#
# `bot_activity_from_signals` 餵的是 bot 帳號的 presence（任何人看得到），
# `rpc_activity_from_signals` 餵的是擁有者**自己帳號**的 Rich Presence。
# 兩條順序刻意不同，而且不是隨手排的：
#
#   bot：遊戲 > 核准的串流音樂 > Claude   —— Claude 墊底
#   RPC：Claude > 遊戲 > 音樂（**嚴格**）  —— Claude 最高
#
# 順序被「整理成一致」就是把一個刻意的決定改掉，而且改完之後**測試不會紅、行為
# 也不會報錯**——只是顯示的東西變了，而看到的人不是我們。
#
# 還有一個更容易被抹掉的不對稱：bot 讀 `music_any`（放寬，含瀏覽器分頁備援），
# RPC 讀 `music_strict`（嚴格，只認核准的串流 app）。兩個鍵名長得很像。
# ===========================================================================

def test_the_bot_puts_a_game_first():
    assert pp.bot_activity_from_signals(
        {"game": "Foo", "music_any": {"title": "T"}, "claude": "C"}
    ) == {"kind": "playing", "name": "Foo"}


def test_the_bot_falls_back_to_music_then_claude():
    assert pp.bot_activity_from_signals(
        {"music_any": {"title": "T", "artist": "A"}, "claude": "C"}
    ) == {"kind": "listening", "name": "T – A"}
    assert pp.bot_activity_from_signals({"claude": "C"}) == {
        "kind": "claude", "name": "C"}


def test_the_owner_account_puts_claude_first():
    """RPC 的順序跟 bot **相反**，這是刻意的。"""
    assert pp.rpc_activity_from_signals(
        {"game": "Foo", "music_strict": {"title": "T"}, "claude": "C"}
    ) == {"kind": "claude", "name": "C"}


def test_the_owner_account_falls_back_to_game_then_music():
    assert pp.rpc_activity_from_signals(
        {"game": "Foo", "music_strict": {"title": "T"}}
    ) == {"kind": "playing", "name": "Foo"}
    assert pp.rpc_activity_from_signals(
        {"music_strict": {"title": "T"}}
    ) == {"kind": "listening", "name": "T"}


def test_the_two_priorities_really_are_different():
    """同一份訊號餵給兩條路，答案必須不一樣——否則其中一條被抄成另一條了。"""
    signals = {"game": "Foo", "music_strict": {"title": "T"},
               "music_any": {"title": "T"}, "claude": "C"}
    assert pp.bot_activity_from_signals(signals) != pp.rpc_activity_from_signals(
        signals)


def test_the_bot_reads_music_any_and_the_owner_reads_music_strict():
    """兩個鍵名長得很像，抄錯的話會**放寬擁有者自己帳號的判定**。

    放寬的方向是「把普通 YouTube 影片也當成在聽音樂」——那會顯示在擁有者自己的
    個人檔案上，而不是 bot 上。
    """
    only_any = {"music_any": {"title": "T"}}
    only_strict = {"music_strict": {"title": "T"}}
    assert pp.bot_activity_from_signals(only_any) is not None
    assert pp.bot_activity_from_signals(only_strict) is None
    assert pp.rpc_activity_from_signals(only_strict) is not None
    assert pp.rpc_activity_from_signals(only_any) is None


@pytest.mark.parametrize("signals", [{}, {"game": ""}, {"claude": None},
                                     {"game": "   "}, {"music_any": None}])
def test_no_signal_means_no_activity(signals):
    """回 None 才會讓呼叫端清掉 presence。回一個空名字的 activity 會卡住不動。"""
    assert pp.bot_activity_from_signals(signals) is None
    assert pp.rpc_activity_from_signals(signals) is None


def test_a_malformed_media_signal_falls_through_instead_of_showing_blank():
    """音樂訊號畸形時要往下一個優先序掉，不是回一個名稱為空的 activity。"""
    assert pp.bot_activity_from_signals(
        {"music_any": {"title": None, "artist": None}, "claude": "C"}
    ) == {"kind": "claude", "name": "C"}


@pytest.mark.parametrize("media, expected", [
    ({"title": "T", "artist": "A"}, "T – A"),
    ({"title": "T"}, "T"),
    ({"artist": "A"}, "A"),
    ({"title": "T", "artist": None}, "T"),
])
def test_media_display_shapes(media, expected):
    assert pp._media_to_activity(media)["name"] == expected


@pytest.mark.parametrize("media", [None, "x", 1, {}, {"title": None},
                                   {"title": "", "artist": ""}])
def test_a_media_signal_with_nothing_in_it_is_none(media):
    assert pp._media_to_activity(media) is None


@pytest.mark.parametrize("fn", ["bot_activity_from_signals",
                                "rpc_activity_from_signals"])
def test_the_name_is_capped_at_the_platform_limit(fn):
    """對話平台會拒收過長的 activity 名稱，而那個拒收會從 presence 迴圈冒出來。"""
    out = getattr(pp, fn)({"claude": "x" * 500, "game": "y" * 500})
    assert len(out["name"]) == 128


def test_a_long_song_title_is_capped_too():
    assert len(pp._media_to_activity(
        {"title": "x" * 300, "artist": "y" * 300})["name"]) == 128


# ===========================================================================
# 串流音樂白名單——`_filter_smtc_media`
#
# SMTC 會把**任何**在播的東西端出來，包含普通 YouTube 影片、線上會議、教學影片。
# 這一層決定哪些會變成「正在聽」。放太寬的代價不是功能壞掉，是把擁有者實際在看
# 什麼廣播出去。
# ===========================================================================

def test_an_approved_streaming_source_is_kept():
    raw = {"title": "T", "artist": "A", "source": "Spotify.exe",
           "playback_type": "music"}
    assert pp._filter_smtc_media(raw) == raw


def test_an_unknown_source_is_dropped():
    """本機播放器、會議軟體、其他來源一律不算。"""
    assert pp._filter_smtc_media(
        {"title": "T", "artist": "", "source": "SomeLocalPlayer",
         "playback_type": ""}) is None


def test_nothing_playing_stays_nothing():
    assert pp._filter_smtc_media(None) is None


def test_a_browser_tab_needs_a_matching_window_title(monkeypatch):
    """瀏覽器分頁只有在**看得到的視窗標題**同時含歌名與音樂服務標記時才算。

    只看 AUMID 的話，普通 YouTube 影片、Meet 通話都會被當成在聽音樂。
    """
    monkeypatch.setattr(
        pp, "_visible_windows",
        lambda: [(1, "My Song - YouTube Music - Chrome")])
    out = pp._filter_smtc_media(
        {"title": "My Song", "artist": "A", "source": "MSEdge",
         "playback_type": ""})
    assert out is not None and "tab" in out["source"]


def test_a_browser_tab_without_the_marker_is_dropped(monkeypatch):
    monkeypatch.setattr(
        pp, "_visible_windows",
        lambda: [(1, "My Song - YouTube - Chrome")])   # 沒有 "music"
    assert pp._filter_smtc_media(
        {"title": "My Song", "artist": "A", "source": "MSEdge",
         "playback_type": ""}) is None


def test_a_browser_tab_whose_song_is_not_in_any_window_is_dropped(monkeypatch):
    monkeypatch.setattr(
        pp, "_visible_windows",
        lambda: [(1, "Something Else - YouTube Music")])
    assert pp._filter_smtc_media(
        {"title": "My Song", "artist": "A", "source": "Chrome",
         "playback_type": ""}) is None


@pytest.mark.parametrize("title", ["", " ", "a", " a "])
def test_a_one_character_song_title_never_matches_a_window(monkeypatch, title):
    """一兩個字的歌名會在幾乎任何視窗標題裡命中，那是雜訊不是證據。"""
    monkeypatch.setattr(pp, "_visible_windows",
                        lambda: [(1, "a whole lot of youtube music here")])
    assert pp._smtc_song_in_browser_music_window(title) is None


def test_the_source_match_is_case_insensitive():
    assert pp._source_is_music("SPOTIFY.EXE")
    assert pp._source_looks_like_browser("MSEdge_8wekyb3d8bbwe!MSEDGE")


# ===========================================================================
# `presence_music.json` 的解析——設定檔是手改的，型別檢查就是全部的防線
# ===========================================================================

@pytest.fixture
def music_file(tmp_path, monkeypatch):
    path = tmp_path / "presence_music.json"
    monkeypatch.setattr(pp, "MUSIC_FILE", path)
    return path


def test_a_missing_music_file_falls_back_silently(music_file, capsys):
    rules = pp._load_music_rules()
    assert rules["smtc_source_substrings"] == pp._DEFAULT_SMTC_SUBSTRINGS
    assert capsys.readouterr().err == ""


def test_a_misspelled_music_key_says_so(music_file, capsys):
    """鍵名打錯要被點名，而且拼對的那個鍵**照樣生效**。

    後半句是正面對照組：少了它，「一律當成壞掉、整份退回預設」也會讓前半句通過。

    這個檔案的失效特別難察覺——`smtc_source_substring`（少一個 s）被忽略之後，
    使用者看到的是「音樂偵測沒反應」，而那跟「現在真的沒在播」長得一模一樣。
    """
    music_file.write_text(json.dumps({
        "smtc_source_substring": ["Spotify"],          # 少一個 s
        "smtc_source_substrings": ["Foobar2000"],      # 拼對的
    }), encoding="utf-8")
    rules = pp._load_music_rules()
    err = capsys.readouterr().err
    assert "smtc_source_substring" in err, f"沒點名打錯的鍵：{err!r}"
    # `_clean_str_list` 會轉小寫（比對本來就不分大小寫），所以這裡比小寫。
    assert "foobar2000" in rules["smtc_source_substrings"], "拼對的那個鍵沒生效"


def test_a_correct_music_file_says_nothing_about_unknown_keys(music_file,
                                                              capsys):
    """全對的設定必須完全安靜——每個 tick 都響的警告等於沒有警告。

    `_load_music_rules()` 在 presence 迴圈裡會被反覆呼叫，而這份 log 已經為了同一
    課吃過虧（96% 的行是同一句 `rpc apply ->`）。`_` 開頭的註解鍵也不算未知：
    正式的 `presence_music.json` 現在有 5 個。
    """
    music_file.write_text(json.dumps({
        "_comment": "說明文字",
        "_smtc_comment": "另一段說明",
        "smtc_source_substrings": ["Foobar2000"],
    }), encoding="utf-8")
    pp._load_music_rules()
    assert "不認得" not in capsys.readouterr().err


def test_the_music_key_whitelist_covers_every_key_the_loader_reads():
    """白名單與**載入器實際會讀的鍵**必須一致，兩個方向都要。

    這一支才是真正防漂的：白名單漏一筆 → 那個合法鍵每次都被當成未知、警告每次都
    響；白名單多一筆 → 打錯成那個名字時不會被抓到。兩種都無症狀。

    判準用 AST 從 `_load_music_rules` 的 `data.get("...")` 抽出來，不是再抄一份
    清單——抄一份的話這支測試只是在驗「兩份手抄的一不一樣」。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(pp._load_music_rules))
    read = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "data"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            read.add(node.args[0].value)
    # 正面對照組：抽不到東西的話下面兩句都會空轉通過。
    assert len(read) >= 4, f"只從載入器抽到 {sorted(read)}，抽取邏輯壞了"
    assert read == set(pp._MUSIC_KNOWN_KEYS), (
        f"白名單與載入器實際讀的鍵對不上：只在載入器裡的 "
        f"{sorted(read - set(pp._MUSIC_KNOWN_KEYS))}、"
        f"只在白名單裡的 {sorted(set(pp._MUSIC_KNOWN_KEYS) - read)}")


def test_all_three_modules_use_the_same_unknown_key_rule():
    """三個模組各有一份 `_warn_unknown_keys`，**規則**必須一致。

    重複是刻意延後的架構決定。重複可以，分歧不行——分歧的話「哪個設定檔會抓到我的
    錯字」就變成要看運氣。這裡拿同一組輸入問三邊。
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "axiomatic"))
    import _bot_config as _bo
    import _batch_config as _bc

    known = {"a": 1}
    for case in ({"a": 1}, {"b": 2}, {"_c": 3}, {"b": 1, "d": 2}, {}):
        results = [mod._warn_unknown_keys(case, known, source=label)
                   for mod, label in ((pp, "pp"), (_bo, "bo"), (_bc, "bc"))]
        assert results[0] == results[1] == results[2], (case, results)


def test_a_broken_music_file_falls_back_and_says_why(music_file, capsys):
    """壞設定要留一行。靜默退回預設值的話，改了設定沒生效的人查不出原因。"""
    music_file.write_text("{ not json", encoding="utf-8")
    rules = pp._load_music_rules()
    assert rules["smtc_source_substrings"] == pp._DEFAULT_SMTC_SUBSTRINGS
    assert "parse" in capsys.readouterr().err


@pytest.mark.parametrize("payload", [
    "[]", "null", '"text"', "123",
    '{"smtc_source_substrings": "spotify"}',      # 字串不是清單
])
def test_a_wrongly_typed_section_falls_back_to_the_default(music_file, payload):
    """整份檔案或整個鍵的型別不對 → 退回預設。半套用比整個退回糟。

    ⚠️ **`{"smtc_source_substrings": ["ok", 1]}` 刻意不在這裡了**（2026-09-08）。
    它原本靠 `all(isinstance(s, str) for s in smtc)` 一票否決，也就是「清單裡有
    一筆寫壞，其餘全部連坐失效，而且一個字都沒印」——那不是半套用，那是**整個
    設定安靜地沒生效**。現在改成逐筆清理＋說一聲，由
    `test_one_bad_entry_no_longer_throws_away_the_whole_list` 接手。
    要加回來之前先讀那一支。
    """
    music_file.write_text(payload, encoding="utf-8")
    assert pp._load_music_rules()["smtc_source_substrings"] == (
        pp._DEFAULT_SMTC_SUBSTRINGS)


def test_a_valid_section_replaces_the_default_and_is_lowercased(music_file):
    """比對時是拿小寫的 source 去比，表裡留著大寫就永遠比不中。"""
    music_file.write_text('{"smtc_source_substrings": ["MyPlayer"]}',
                          encoding="utf-8")
    assert pp._load_music_rules()["smtc_source_substrings"] == ("myplayer",)


def test_window_hints_drop_only_the_malformed_entries(music_file):
    music_file.write_text(
        '{"browser_window_hints": ['
        '{"window_substring": "AAA", "label": "L"},'
        '{"window_substring": 1, "label": "L"},'
        '{"label": "no substring"},'
        '"not a dict"]}', encoding="utf-8")
    assert pp._load_music_rules()["browser_window_hints"] == (("aaa", "L"),)


def test_the_file_is_read_on_every_call(music_file):
    """沒有快取是刻意的——改了 JSON 下一個 probe tick（約 8 秒）就生效。"""
    music_file.write_text('{"smtc_source_substrings": ["one"]}',
                          encoding="utf-8")
    assert pp._load_music_rules()["smtc_source_substrings"] == ("one",)
    music_file.write_text('{"smtc_source_substrings": ["two"]}',
                          encoding="utf-8")
    assert pp._load_music_rules()["smtc_source_substrings"] == ("two",)


def test_a_bom_does_not_break_the_music_config(music_file):
    """記事本存 UTF-8 會加 BOM。編碼是 `utf-8-sig`，這一支釘住它。"""
    music_file.write_text('\ufeff{"smtc_source_substrings": ["one"]}',
                          encoding="utf-8")
    assert pp._load_music_rules()["smtc_source_substrings"] == ("one",)


def test_a_bad_regex_is_skipped_not_fatal(capsys):
    """設定檔裡一個寫壞的樣式不該讓其餘全部失效，但要留一行。

    測資裡的兩個好樣式**必須帶 `song` group**：`_compile_patterns` 現在會擋掉
    沒有它的樣式（見 `test_a_pattern_without_the_song_group_is_refused`），
    原本的 `"ok"` / `"also ok"` 只是佔位字串，會被新的檢查一起丟掉。
    """
    good_a = "^(?P<song>.+) - A$"
    good_b = "^(?P<song>.+) - B$"
    out = pp._compile_patterns([good_a, "(unclosed", 123, good_b])
    assert [p.pattern for p in out] == [good_a, good_b]
    assert "bad regex" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# presence_games.json 的 key 正規化
# ---------------------------------------------------------------------------
# 這個檔案是**給人手動編輯**的，而且每 ~8 秒熱重載、不必重啟 bot。所以「多打一個
# 空白」是完全會發生的事——而它不會有任何錯誤訊息，只會讓那一筆安靜地永遠對不上。


@pytest.mark.parametrize("key,expect_priority", [
    ("*endfield.exe", True),
    ("* endfield.exe", True),      # 星號**後**有空白
    (" *endfield.exe", True),      # 星號**前**有空白 ← 2026-09-07 之前是壞的
    ("  *  endfield.exe  ", True),
    ("\t*\tendfield.exe", True),
    ("endfield.exe", False),
    ("  Endfield.EXE ", False),
])
def test_whitespace_never_changes_which_game_a_key_means(
        games_file, key, expect_priority):
    """空白與大小寫怎麼擺，都要指到同一個 exe，priority 標記也要跟著對。

    **`*` 前面的空白原本是唯一會壞的位置。** 兩個載入器都寫
    `k.lstrip("*").strip().lower()`——先剝 `*` 再 strip。對 `"* endfield.exe"`
    這是對的；對 `" *endfield.exe"` 就整個垮掉：`lstrip("*")` 一遇到開頭的空白就
    停手，算出的 key 是 `"*endfield.exe"`，一個**永遠不可能**對上任何行程名稱的
    字串；同時 `startswith("*")` 也是 False，priority 也沒了。**兩個功能一起靜默
    死掉，而 JSON 看起來完全正常。**

    這種「只在一個位置成立的例外」是人複查看不出來的——非 priority 的
    `"  Endfield.EXE "` 前後空白都好好的，所以看起來空白明明有處理。
    """
    games_file.write_text(json.dumps({key: "Endfield"}, ensure_ascii=False),
                          encoding="utf-8")
    whitelist = pp._load_game_whitelist()
    assert list(whitelist) == ["endfield.exe"], (
        f"key {key!r} 算出來是 {list(whitelist)}，對不上任何真的行程名稱。")
    assert whitelist["endfield.exe"] == "Endfield"
    priority = pp._load_priority_games()
    assert (priority == ["endfield.exe"]) is expect_priority, (
        f"key {key!r} 的 priority 判定錯了（得到 {priority}）。")


def test_every_priority_entry_also_exists_in_the_whitelist(games_file):
    """**這是兩個載入器之間唯一真正重要的不變量。**

    `probe_priority_game` 的最後一步是 `whitelist.get(exe)`——priority 清單裡的
    字串如果不在白名單裡，那一筆就永遠贏不了，而且回的是 `None`（＝「沒有
    priority 遊戲在跑」），跟「真的沒在跑」完全分不出來。

    兩份各自實作的正規化規則遲早會分歧，所以這裡不是比對實作，是直接釘結果。
    """
    games_file.write_text(json.dumps({
        "*endfield.exe": "Endfield",
        " *examplegame.exe": "Example Game",
        "*  cs2.exe  ": "Counter-Strike 2",
        "maplestoryn.exe": "MapleStory N",
    }, ensure_ascii=False), encoding="utf-8")
    whitelist = pp._load_game_whitelist()
    missing = [exe for exe in pp._load_priority_games() if exe not in whitelist]
    assert not missing, (
        f"這些 priority 條目不在白名單裡：{missing}。"
        "`probe_priority_game` 最後會 `whitelist.get(exe)` 拿到 None，"
        "於是那個遊戲永遠贏不過音樂偵測——而且沒有任何錯誤訊息。")


def test_comment_keys_are_actually_ignored(games_file):
    """JSON 自己說 `_` 開頭的 key 會被忽略——那句話以前是假的。

    `presence_games.json` 的 `_comment` 寫著「Comment keys (prefix '_') are
    ignored by the matcher」，但實際上兩個 `_comment` 條目照樣被收進白名單，
    顯示名稱是整段說明文字。實測不到任何行程所以沒造成故障，但**一份自己說謊的
    資料契約，下一個人會照著它寫程式**——例如新增一個 `_note` 之後以為它是安全的。

    2026-09-11 補兩格：`_` 前面帶空白的說明 key，以及「有空白但**不是**說明」的
    反面案例。原本的測資對 `_is_comment_key` 的 `.strip()` 是盲的——實測把那一步
    拿掉，這支照樣全綠（兩種寫法都算出 `['endfield.exe']`）。
    """
    games_file.write_text(json.dumps({
        "_comment": "這是說明，不是遊戲",
        "_priority_comment": "這也是",
        # `_` **前面**多打空白也還是說明。`_is_comment_key` 的 `.strip()` 就是為了
        # 這一格，而它在此之前沒有任何覆蓋——這個檔案是給人手動編輯、每 ~8 秒熱
        # 重載的，多打一個空白不會有任何錯誤訊息。
        "  _spaced_comment": "前面多打了空白，一樣是說明",
        # 反面：有空白但**不是**說明的 key 必須照樣留下（正規化成 `endfield.exe`）。
        # 少了這一格，一個把「前後有空白就當成說明」的過寬版本也會全綠，而那會
        # 靜默吃掉正當條目。
        "  Endfield.EXE ": "Endfield",
    }, ensure_ascii=False), encoding="utf-8")
    whitelist = pp._load_game_whitelist()
    assert list(whitelist) == ["endfield.exe"], (
        f"註解 key 混進白名單了：{list(whitelist)}")
    assert pp._load_priority_games() == []


def test_both_loaders_share_one_normaliser():
    """兩個載入器都必須走 `_normalise_game_key`，不得自己再寫一份。

    這一支釘的是**原因**而不是症狀：上面那些測試釘的是「兩邊算出來一樣」，但只要
    有人在其中一邊重新寫一次 `lstrip`／`strip`／`lower`，就又是兩份平行規則，
    而下一次只會有一份被改到。用 AST 檢查呼叫關係，不用字串比對——`getsource()`
    連 docstring 一起拿，而這裡的 docstring 正好在解釋這條規則。
    """
    import ast
    tree = ast.parse(Path(pp.__file__).read_text(encoding="utf-8"))
    for name in ("_load_game_whitelist", "_load_priority_games"):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert fn is not None, f"{name} 改名了——這支守門要跟著改"
        calls = {ast.unparse(n.func) for n in ast.walk(fn)
                 if isinstance(n, ast.Call)}
        assert "_normalise_game_key" in calls, (
            f"{name} 沒有走 `_normalise_game_key`，自己另寫了一份正規化。"
            "兩份平行的規則遲早分歧，而分歧的症狀是 priority 靜默失效。")
        assert not {c for c in calls if c.endswith(".lstrip")}, (
            f"{name} 裡還有 `lstrip`——正規化只能有一個來源。")


# ---------------------------------------------------------------------------
# `_load_claude_detection` —— 2026-09-08 補。
#
# 這支載入器在此之前**一行都沒被執行過**（實測 coverage：`_load_claude_detection`
# 29/35 行未覆蓋、`probe_claude_code` 16/21，而這個檔案裡 44 支測試沒有一支提到
# 它們）。它值得測不是因為行數，是因為它守的是一個**隱私退出開關**：
# `{"claude": {"enabled": false}}` 沒生效的話，「正在用 Claude Code」會繼續被廣播
# 到聊天平台，而使用者以為自己已經關掉了——而且那個失效跟「現在真的沒在用」
# 長得一模一樣（本檔開頭那條病）。
# ---------------------------------------------------------------------------


def _claude_cfg(monkeypatch, tmp_path, payload):
    """把 `presence_rpc.json` 換成暫存檔。`payload` 是 str 就原樣寫入（用來測
    壞掉的 JSON），是其他物件就 `json.dumps`。回傳那個路徑。"""
    path = tmp_path / "presence_rpc.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(pp, "RPC_CONFIG_FILE", path)
    return path


def test_claude_detection_defaults_when_the_file_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(pp, "RPC_CONFIG_FILE", tmp_path / "nope.json")
    assert pp._load_claude_detection() == (True, pp._DEFAULT_CLAUDE_PROCESSES)


def test_turning_it_off_actually_turns_it_off(monkeypatch, tmp_path):
    """**這支是整組裡最重要的一條。**

    `enabled: false` 是使用者唯一的退出開關。它壞掉的話不會有任何錯誤訊息，
    只會繼續把「正在用 Claude Code」廣播出去。
    """
    _claude_cfg(monkeypatch, tmp_path, {"claude": {"enabled": False}})
    enabled, _ = pp._load_claude_detection()
    assert enabled is False


def test_probe_returns_none_when_disabled_without_touching_psutil(
        monkeypatch, tmp_path):
    """關掉時必須**在掃行程之前**就收工。

    只測「回 None」是不夠的：沒有行程符合時也回 None，兩者分不出來。所以這裡
    讓 `psutil` 一被碰到就爆炸，證明那條路根本沒走到。
    """
    _claude_cfg(monkeypatch, tmp_path, {"claude": {"enabled": False}})

    class Boom:
        def __getattr__(self, _name):
            raise AssertionError("關掉了還是去掃了行程")

    monkeypatch.setitem(sys.modules, "psutil", Boom())
    assert pp.probe_claude_code() is None


def test_process_names_are_stripped_and_lowercased(monkeypatch, tmp_path):
    _claude_cfg(monkeypatch, tmp_path,
                {"claude": {"process_names": ["  Claude.EXE ", "Node.exe"]}})
    _, names = pp._load_claude_detection()
    assert names == ("claude.exe", "node.exe")


def test_junk_entries_are_dropped_but_a_usable_one_still_wins(
        monkeypatch, tmp_path):
    _claude_cfg(monkeypatch, tmp_path,
                {"claude": {"process_names": [1, None, "", "  ", "X.exe"]}})
    _, names = pp._load_claude_detection()
    assert names == ("x.exe",)


def test_an_all_junk_process_list_keeps_the_defaults_and_says_so(
        monkeypatch, tmp_path, capsys):
    """清單裡沒有半個可用字串時**不可以**採用空 tuple。

    採用空 tuple 會讓比對永遠不命中，效果等同偷偷把偵測關掉——而那跟「設定沒生
    效」長得一模一樣。退回預設**並且吭聲**才對。
    """
    _claude_cfg(monkeypatch, tmp_path, {"claude": {"process_names": ["", 5]}})
    enabled, names = pp._load_claude_detection()
    assert (enabled, names) == (True, pp._DEFAULT_CLAUDE_PROCESSES)
    assert "process_names" in capsys.readouterr().err


def test_a_mistyped_opt_out_is_ignored_but_not_silently(
        monkeypatch, tmp_path, capsys):
    """`"enabled": "false"`（字串）依全專案慣例退回預設，**但必須留下痕跡**。

    「型別不符 → 退回預設」是 `_bot_config._coerce_bool` 明文定下的慣例，這裡跟著
    走、不另立規矩。但這個鍵的退回方向是「繼續廣播」，而使用者的本意是關掉——兩種
    錯法的代價不對稱，所以至少要在 stderr 留一行，讓人查得到為什麼沒生效。
    """
    for bad in ("false", 0, None, []):
        _claude_cfg(monkeypatch, tmp_path, {"claude": {"enabled": bad}})
        capsys.readouterr()
        enabled, _ = pp._load_claude_detection()
        assert enabled is True, f"{bad!r} 竟然被當成有效的 boolean"
        assert "claude.enabled" in capsys.readouterr().err, (
            f"{bad!r} 被靜默忽略了——使用者會以為自己關掉了")


def test_an_absent_key_is_not_a_warning(monkeypatch, tmp_path, capsys):
    """沒寫這個鍵是**正常情況**，不可以吭聲。

    會亂叫的警告最後會被人關掉（`test_language` 記過同一個教訓），而這條路每次
    presence 迴圈都會走到——多印一行就是下一個 `rpc apply ->`（`discord_bot.log`
    有 95.8% 是同一行雜訊）。
    """
    _claude_cfg(monkeypatch, tmp_path, {"claude": {}})
    capsys.readouterr()
    assert pp._load_claude_detection() == (True, pp._DEFAULT_CLAUDE_PROCESSES)
    assert capsys.readouterr().err == ""


def test_broken_or_odd_json_falls_back_without_raising(monkeypatch, tmp_path):
    """檔案壞掉、頂層不是物件、`claude` 不是物件——三種都要退回預設且不得 raise。

    這條路跑在 presence 迴圈裡，raise 出去就是整個 presence 掛掉。
    """
    for payload in ("{ not json", "null", "[]", '{"claude": 5}',
                    '{"claude": null}', ""):
        _claude_cfg(monkeypatch, tmp_path, payload)
        assert pp._load_claude_detection() == (
            True, pp._DEFAULT_CLAUDE_PROCESSES), f"payload={payload!r}"


def test_undecodable_bytes_fall_back_instead_of_exploding(monkeypatch, tmp_path):
    """不是合法 UTF-8 的位元組要退回預設。

    `UnicodeDecodeError` 是 `ValueError` 的子類、**不是** `OSError`——本專案在別的
    檔案上因為這件事踩過反方向的錯，所以這裡明確釘住。
    """
    path = tmp_path / "presence_rpc.json"
    path.write_bytes(b'{"claude": {"enabled": \xff\xfe false}}')
    monkeypatch.setattr(pp, "RPC_CONFIG_FILE", path)
    assert pp._load_claude_detection() == (True, pp._DEFAULT_CLAUDE_PROCESSES)


def _fake_psutil(monkeypatch, names, *, boom=None):
    """裝一個假的 `psutil`，`process_iter` 吐出指定的行程名。

    用 `monkeypatch.setitem` 塞進 `sys.modules`，**不是** `sys.modules.pop`——後者
    清掉的只是快取，下一次 import 會把**真的** psutil 載回來，而本專案就是因為這個
    寫法在 2026-09-07 殺掉一個跑了 78.7 小時的正式批次（`test_suite_safety` 現在
    明文禁止它）。teardown 也由 monkeypatch 自動還原，測試中途丟例外一樣還原。
    """
    class _Proc:
        def __init__(self, name):
            self.info = {"name": name}

    class _Fake:
        # `sys.modules` 的值不必真的是 module 物件——`import psutil` 只是把這個
        # 物件綁到那個名字上。用普通類別省掉一個 import。
        @staticmethod
        def process_iter(attrs=None):
            if boom is not None:
                raise boom
            return [_Proc(n) for n in names]

    monkeypatch.setitem(sys.modules, "psutil", _Fake())


def test_claude_is_detected_by_the_configured_process_name(
        monkeypatch, tmp_path):
    _claude_cfg(monkeypatch, tmp_path, {"claude": {"process_names": ["mine.exe"]}})
    _fake_psutil(monkeypatch, ["explorer.exe", "MINE.EXE", "chrome.exe"])
    assert pp.probe_claude_code() == "Claude Code", (
        "行程名比對要不分大小寫——設定值已被轉成小寫，行程名也必須轉")


def test_an_unrelated_process_list_is_not_a_match(monkeypatch, tmp_path):
    """沒命中要回 None。

    這支與上一支必須成對存在：只測「有命中」的話，一個「永遠回 'Claude Code'」的
    實作也會全綠，而那會讓使用者的狀態永遠卡在寫程式。
    """
    _claude_cfg(monkeypatch, tmp_path, {"claude": {"process_names": ["mine.exe"]}})
    _fake_psutil(monkeypatch, ["explorer.exe", "chrome.exe", "mine.exe.bak"])
    assert pp.probe_claude_code() is None


def test_a_process_with_no_name_does_not_crash_the_scan(monkeypatch, tmp_path):
    """`proc.info["name"]` 可能是 None（行程剛結束時很常見）。"""
    _claude_cfg(monkeypatch, tmp_path, {"claude": {"process_names": ["mine.exe"]}})
    _fake_psutil(monkeypatch, [None, "", "mine.exe"])
    assert pp.probe_claude_code() == "Claude Code"


def test_a_scan_failure_is_reported_not_swallowed(monkeypatch, tmp_path, capsys):
    """掃描炸掉時回 None，**但要在 stderr 留話**。

    這個模組每一條失敗路徑都用「回 None」表示，而「回 None」跟「現在真的沒在用」
    長得一模一樣（見本檔開頭）。所以失敗一定要吭聲，否則偵測靜默失效沒人會發現。
    """
    _claude_cfg(monkeypatch, tmp_path, {"claude": {"process_names": ["mine.exe"]}})
    _fake_psutil(monkeypatch, [], boom=RuntimeError("psutil 壞了"))
    assert pp.probe_claude_code() is None
    assert "claude scan failed" in capsys.readouterr().err


def test_no_psutil_means_no_detection_and_no_crash(monkeypatch, tmp_path):
    """沒有 psutil 時安靜地回 None。

    用 `sys.modules["psutil"] = None` 讓 `import psutil` 丟 `ImportError`——這是本
    專案指定的「模擬套件不存在」寫法。
    """
    _claude_cfg(monkeypatch, tmp_path, {"claude": {"process_names": ["mine.exe"]}})
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert pp.probe_claude_code() is None


def test_a_non_list_process_names_is_ignored_but_not_silently(
        monkeypatch, tmp_path, capsys):
    """`process_names` 寫成字串（而不是清單）是最容易犯的錯，也要吭聲。

    `"process_names": "claude.exe"` 看起來完全合理。它會被忽略、沿用預設——這一次
    剛好還是對的，但如果使用者想改成別的行程名，設定就是靜默沒生效。
    """
    for bad in ("claude.exe", 5, {"a": 1}, True):
        _claude_cfg(monkeypatch, tmp_path, {"claude": {"process_names": bad}})
        capsys.readouterr()
        enabled, names = pp._load_claude_detection()
        assert (enabled, names) == (True, pp._DEFAULT_CLAUDE_PROCESSES)
        assert "process_names" in capsys.readouterr().err, (
            f"{bad!r} 被靜默忽略了")


# ---------------------------------------------------------------------------
# `probe_game_process` 的三層優先序
# ---------------------------------------------------------------------------
# 這一族補在 2026-09-08：實測 `probe_game_process` 46 行裡 35 行、
# `probe_priority_game` 28 行裡 23 行從來沒被執行過——也就是**三層優先序一層都
# 沒被驗過**，而它每幾秒就跑一次、決定「使用者現在在玩什麼」。
#
# 這一族刻意每一層都配一個**反面**案例：只驗「priority 會贏」的話，一個永遠回
# priority 的實作也會全綠，而那會讓 presence 卡在一個早就關掉的遊戲上。


def _fake_psutil_with_pids(monkeypatch, procs, *, boom=None):
    """假 psutil，`process_iter` 吐出 `(pid, name)`。

    既有的 `_fake_psutil` 只給 name，而前景視窗那一層是**用 pid 比對**的
    （標題是程式想顯示什麼就顯示什麼，行程不是——見 `_foreground_window_pid`
    的 docstring），所以這裡需要一個帶 pid 的版本。
    照舊用 `monkeypatch.setitem`，不用 `sys.modules.pop`。
    """
    class _Proc:
        def __init__(self, pid, name):
            self.info = {"pid": pid, "name": name}

    class _Fake:
        @staticmethod
        def process_iter(attrs=None):
            if boom is not None:
                raise boom
            return [_Proc(pid, name) for pid, name in procs]

    monkeypatch.setitem(sys.modules, "psutil", _Fake())


def _games(games_file, mapping):
    """寫一份 presence_games.json；key 前面的 `*` 就是 priority 標記。"""
    games_file.write_text(json.dumps(mapping, ensure_ascii=False),
                          encoding="utf-8")


def _foreground(monkeypatch, pid):
    monkeypatch.setattr(pp, "_foreground_window_pid", lambda: pid)


def test_a_priority_game_beats_the_game_in_the_foreground(
        games_file, monkeypatch):
    """Priority 遊戲在跑就贏，**連前景視窗都不看**。

    這是這個功能存在的理由：邊聽音樂邊玩 Endfield 時要看到 'Playing Endfield'，
    而且就算當下前景是別的遊戲視窗也一樣。
    """
    _games(games_file, {"*prio.exe": "Priority Game", "other.exe": "Other Game"})
    _fake_psutil_with_pids(monkeypatch, [(10, "prio.exe"), (20, "other.exe")])
    _foreground(monkeypatch, 20)          # 前景是**另一個**遊戲
    assert pp.probe_game_process() == "Priority Game"


def test_without_a_priority_game_the_foreground_one_wins(
        games_file, monkeypatch):
    """反面：沒有 priority 在跑時，前景視窗那一層才是贏家。

    少了這一支，「永遠回第一個命中」也會讓上一支通過。
    """
    _games(games_file, {"*prio.exe": "Priority Game",
                        "first.exe": "First Game", "fg.exe": "Foreground Game"})
    # priority 那個**沒有**在跑
    _fake_psutil_with_pids(monkeypatch, [(10, "first.exe"), (20, "fg.exe")])
    _foreground(monkeypatch, 20)
    assert pp.probe_game_process() == "Foreground Game"


def test_the_first_hit_is_the_fallback_when_nothing_is_in_the_foreground(
        games_file, monkeypatch):
    """第三層：前景查不到（或前景不是遊戲）就退回第一個命中。"""
    _games(games_file, {"first.exe": "First Game", "second.exe": "Second Game"})
    # 第一個刻意是**不在白名單**的行程：真實的行程表絕大多數都是這種，而且它同時
    # 釘住「不相關的行程要被跳過」——少了那道 `continue`，`whitelist[name]` 會丟
    # KeyError、被外層 except 收成 None，整支 probe 靜默失效。
    _fake_psutil_with_pids(
        monkeypatch, [(5, "explorer.exe"), (10, "first.exe"), (20, "second.exe")])
    _foreground(monkeypatch, None)        # 查不到前景視窗
    assert pp.probe_game_process() == "First Game"
    # 前景是一個**不在白名單**的行程，結果一樣是退回第一個命中。
    _foreground(monkeypatch, 999)
    assert pp.probe_game_process() == "First Game"


def test_a_foreground_pid_of_zero_is_not_treated_as_a_match(
        games_file, monkeypatch):
    """`fg_pid` 是 0 時不可以拿去比對。

    程式碼寫的是 `if fg_pid and proc.info.get("pid") == fg_pid`——那個 `and` 不是
    多餘的。少了它，pid 0 會跟任何回報 pid 0 的行程比中（`psutil` 在取不到 pid
    時就給得出 0／None），於是「查不到前景視窗」會被誤判成「前景正是這個遊戲」，
    第二層與第三層的差別靜默消失。
    """
    _games(games_file, {"first.exe": "First Game", "zero.exe": "Zero Game"})
    # 順序很關鍵：pid 0 的那個**不可以**同時是第一個命中，否則兩種實作會給出
    # 一樣的答案而這支測試什麼都證明不了（變異測試實測過：第一版就是那樣，
    # 拿掉守衛照樣全綠）。first.exe 先出現 → 它是 first_hit；zero.exe 帶 pid 0。
    _fake_psutil_with_pids(monkeypatch, [(10, "first.exe"), (0, "zero.exe")])
    _foreground(monkeypatch, 0)
    # 有守衛：0 不算前景命中 → 第二層落空 → 第三層回 first_hit。
    # 沒守衛：`0 == 0` 會讓 zero.exe 變成前景命中 → 第二層回 "Zero Game"。
    assert pp.probe_game_process() == "First Game"


def test_priority_order_decides_between_two_running_priority_games(
        games_file, monkeypatch):
    """兩個 priority 遊戲同時在跑 → 依 JSON 的出現順序，第一個贏。

    順序是**使用者可控的設定**（JSON 的鍵序），不是隨便哪個先掃到。
    """
    _games(games_file, {"*a.exe": "Game A", "*b.exe": "Game B"})
    # psutil 的順序刻意跟 JSON 相反，證明贏家是 JSON 決定的
    _fake_psutil_with_pids(monkeypatch, [(10, "b.exe"), (20, "a.exe")])
    _foreground(monkeypatch, None)
    assert pp.probe_game_process() == "Game A"
    assert pp.probe_priority_game() == "Game A"


def test_probe_priority_game_ignores_a_priority_game_that_is_not_running(
        games_file, monkeypatch):
    """反面：priority 遊戲沒在跑就回 None，不能因為它列在設定裡就回報。"""
    _games(games_file, {"*prio.exe": "Priority Game", "other.exe": "Other"})
    _fake_psutil_with_pids(monkeypatch, [(10, "other.exe")])
    assert pp.probe_priority_game() is None


def test_probe_priority_game_returns_none_without_any_priority_entry(
        games_file, monkeypatch):
    """完全沒有 `*` 前綴的設定 → 這一層不該做任何事（也不該掃行程）。"""
    _games(games_file, {"other.exe": "Other"})

    class _Boom:
        @staticmethod
        def process_iter(attrs=None):
            raise AssertionError("沒有 priority 項目時不該去掃行程")

    monkeypatch.setitem(sys.modules, "psutil", _Boom())
    assert pp.probe_priority_game() is None


def test_a_failed_process_scan_returns_none_and_says_so(
        games_file, monkeypatch, capsys):
    """掃描炸掉 → 回 None **並在 stderr 留話**（兩支 probe 都要）。

    這個模組每一條失敗路徑都用「回 None」表示，而「回 None」跟「現在真的沒在玩」
    長得一模一樣。所以失敗一定要吭聲，否則偵測靜默失效沒人會發現。
    """
    _games(games_file, {"*prio.exe": "Priority Game", "first.exe": "First"})
    _fake_psutil_with_pids(monkeypatch, [], boom=OSError("拒絕存取"))
    _foreground(monkeypatch, None)

    assert pp.probe_game_process() is None
    assert "拒絕存取" in capsys.readouterr().err

    assert pp.probe_priority_game() is None
    assert "拒絕存取" in capsys.readouterr().err


def test_an_empty_whitelist_short_circuits_before_psutil(
        games_file, monkeypatch):
    """白名單是空的就直接回 None，連 psutil 都不要碰。

    presence 迴圈每幾秒跑一次；沒有設定任何遊戲的使用者不該為此付一次全機行程掃描。
    """
    _games(games_file, {})
    scanned = []

    class _Recorder:
        # **不要用丟例外當偵測器**：`probe_game_process` 有一個包山包海的
        # `except Exception`，會把 AssertionError 收成「掃描失敗」然後照樣回 None
        # ——於是短路被拿掉了測試還是綠的（變異測試實測過）。改成記錄器。
        @staticmethod
        def process_iter(attrs=None):
            scanned.append(True)
            return []

    monkeypatch.setitem(sys.modules, "psutil", _Recorder())
    assert pp.probe_game_process() is None
    assert not scanned, (
        "白名單是空的時候還是掃了一次全機行程——presence 迴圈每幾秒跑一次，"
        "沒設定任何遊戲的使用者不該為此付這個成本。")


def test_both_probes_survive_psutil_being_unavailable(games_file, monkeypatch):
    """沒有 psutil 就回 None，不可以往外拋。

    用 `setitem(..., None)` 模擬「裝不到」——`import psutil` 對 `None` 會丟
    `ImportError`。**不要用 `sys.modules.pop`**：那只是清快取，下一次 import 會把
    真的 psutil 載回來（本專案 2026-09-07 就因為這個寫法殺掉一個 78.7 小時的批次）。
    """
    _games(games_file, {"*prio.exe": "Priority Game", "first.exe": "First"})
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert pp.probe_game_process() is None
    assert pp.probe_priority_game() is None


# ===========================================================================
# 音樂設定的清單：**不得安靜地把偵測整個關掉**
# ===========================================================================
# 這一組守的是本模組最典型的失效方式，而它在 `_load_music_rules` 裡出現了四次。
# 四個鍵（`smtc_source_substrings`／`browser_aumid_substrings`／
# `browser_window_hints`／`foreground_window_patterns`）全都是白名單，而**空的
# 白名單會讓比對永遠不命中**——效果等同偷偷關掉音樂偵測，使用者看到的症狀卻是
# 「現在沒在播音樂」，跟真的沒在播無法區分。
#
# 2026-09-08 實測，修之前五種寫法全部無聲：
#   `[]`                        → 白名單變空 → `_source_is_music` 對什麼都回 False
#   `["spotify", 123]`          → `all(isinstance(...))` 一票否決，五個全不生效
#   hints 每筆都寫錯 key        → hints 變成空 tuple
#   所有 regex 都編不過         → pattern 清單變空
#   regex 少了 `song` group     → 收進清單，然後永遠不會命中
#
# 這條規則不是新發明的：`_load_claude_detection` 的 `process_names` 已經這樣裁定
# 過（「採用空 tuple 等於偷偷關掉偵測」），這裡是把同一條補到剩下四個鍵上。

def _music(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_a_configured_list_is_actually_adopted(music_file, capsys):
    """**正面對照組，先跑。**

    少了這支，一個「永遠回預設值」的實作會讓下面每一支都變綠——那正是「設定改了
    沒生效」，也就是這一整組要防的東西本身。
    """
    _music(music_file, {"smtc_source_substrings": ["Foobar2000", "  MPD  "]})
    rules = pp._load_music_rules()
    assert rules["smtc_source_substrings"] == ("foobar2000", "mpd"), (
        "設定檔裡合法的清單沒有生效")
    assert capsys.readouterr().err == "", "一份完全正常的設定不該印任何東西"


def test_an_empty_list_does_not_silently_disable_music_detection(music_file,
                                                                 capsys):
    """`[]` → 沿用預設並說一聲，而不是把白名單清空。"""
    _music(music_file, {"smtc_source_substrings": []})
    rules = pp._load_music_rules()
    assert rules["smtc_source_substrings"] == pp._DEFAULT_SMTC_SUBSTRINGS
    # 從**效果**那一端再確認一次：只斷言 tuple 相等的話，看不出使用者實際會遇到
    # 什麼——空白名單真正的症狀是「YouTube Music 不再被認得」。
    assert pp._source_is_music("YouTube Music") is True
    assert "smtc_source_substrings" in capsys.readouterr().err


def test_one_bad_entry_no_longer_throws_away_the_whole_list(music_file, capsys):
    """一筆寫壞不該讓其餘幾筆一起失效。

    舊寫法是 `all(isinstance(s, str) for s in smtc)` 一票否決：五個來源裡有一個
    打成數字，五個就**全部**沒生效，而且一個字都沒印。
    """
    _music(music_file, {"smtc_source_substrings": ["foobar2000", 123, ""]})
    rules = pp._load_music_rules()
    assert rules["smtc_source_substrings"] == ("foobar2000",), (
        "可用的那一筆被連坐丟掉了")
    assert "2 筆" in capsys.readouterr().err, "被跳過的筆數要講出來"


def test_a_key_that_is_not_a_list_keeps_the_defaults_and_says_so(music_file,
                                                                 capsys):
    _music(music_file, {"browser_aumid_substrings": "chrome"})
    rules = pp._load_music_rules()
    assert rules["browser_aumid_substrings"] == \
        pp._DEFAULT_BROWSER_AUMID_SUBSTRINGS
    assert "不是清單" in capsys.readouterr().err


def test_hints_that_all_fail_validation_keep_the_defaults(music_file, capsys):
    """每一筆都寫錯 key → 沿用預設，不要變成空 tuple。"""
    _music(music_file, {"browser_window_hints": [
        {"substring": "music", "name": "YT"}]})       # 兩個 key 都拼錯
    rules = pp._load_music_rules()
    assert rules["browser_window_hints"] == pp._DEFAULT_BROWSER_WINDOW_HINTS
    assert "browser_window_hints" in capsys.readouterr().err


def test_hints_keep_the_good_entries_and_report_the_dropped_ones(music_file,
                                                                 capsys):
    _music(music_file, {"browser_window_hints": [
        {"window_substring": "Tidal", "label": "TIDAL"},
        {"window_substring": "oops"},                 # 缺 label
    ]})
    rules = pp._load_music_rules()
    assert rules["browser_window_hints"] == (("tidal", "TIDAL"),)
    assert "1 筆" in capsys.readouterr().err


def test_patterns_that_all_fail_to_compile_keep_the_defaults(music_file,
                                                             capsys):
    """全部編不過 → 沿用預設。修之前 pattern 清單會變成空的，前景備援整個沒了。"""
    _music(music_file, {"foreground_window_patterns": ["(((("]})
    rules = pp._load_music_rules()
    assert rules["foreground_window_patterns"] == pp._compile_patterns(
        pp._DEFAULT_FG_PATTERN_STRINGS)
    err = capsys.readouterr().err
    assert "bad regex" in err and "沿用預設" in err


def test_a_pattern_without_the_song_group_is_refused(music_file, capsys):
    """`probe_foreground_music` 的 docstring 寫著 pattern **必須**含 `song`
    group，而在這之前沒有任何東西在檢查它。

    少了那個 group 的 pattern 編譯得過、被收進清單，然後比對時
    `groups.get("song")` 回 `None` → `song` 是空字串 → 那一筆被跳過。結果是一個
    **看起來設定好了、實際上永遠不會命中**的 pattern，完全無聲。
    一條寫在 docstring 裡、沒有任何東西執行的規則，等於沒有這條規則。
    """
    _music(music_file, {"foreground_window_patterns": [
        "^(?P<track>.+) - YouTube Music$"]})          # 用了 track，不是 song
    rules = pp._load_music_rules()
    assert rules["foreground_window_patterns"] == pp._compile_patterns(
        pp._DEFAULT_FG_PATTERN_STRINGS), "沒有 song group 的 pattern 被採用了"
    assert "song" in capsys.readouterr().err


def test_a_pattern_with_the_song_group_is_accepted(music_file, capsys):
    """反面：正確的自訂 pattern 一定要收得下，否則上一支用「一律拒絕」也會過。"""
    _music(music_file, {"foreground_window_patterns": [
        "^(?P<song>.+) - (?P<artist>.+) - Foobar$"]})
    rules = pp._load_music_rules()
    assert len(rules["foreground_window_patterns"]) == 1
    assert rules["foreground_window_patterns"][0].match(
        "Nightcall - Kavinsky - Foobar")
    assert capsys.readouterr().err == ""


def test_a_non_string_pattern_says_so_instead_of_vanishing(music_file, capsys):
    """型別寫錯的 pattern 原本是靜默 `continue`——壞掉的 regex 會出聲、寫錯型別
    的卻不會，沒有道理。"""
    _music(music_file, {"foreground_window_patterns": [
        123, "^(?P<song>.+) - Foobar$"]})
    rules = pp._load_music_rules()
    assert len(rules["foreground_window_patterns"]) == 1
    assert "不是字串" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 警告本身不得變成雜訊
# ---------------------------------------------------------------------------

def test_a_config_complaint_is_printed_once_not_every_tick(music_file, capsys):
    """設定檔的抱怨每個 presence tick 印一次的話，就是下一個 `rpc apply ->`。

    `_load_music_rules` 沒有快取，每個 tick（約 8 秒）重讀一次；一個放著沒改的
    錯字一天會印一萬多行**同一句話**。而 `discord_bot.log` 由 `trim_log` 只留
    尾段，於是真正有用的診斷會被自己的警告擠出去——這不是假想，同一份 log 現在
    就有 96% 的行是同一句 `rpc apply ->`。一個把有用訊息趕出去的警告比不警告還糟。
    """
    _music(music_file, {"smtc_source_substrings": []})
    for _ in range(5):
        pp._load_music_rules()
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 1, f"同一段警告印了 {len(lines)} 次：{lines}"


def test_a_different_complaint_still_gets_through(music_file, capsys):
    """去重的鍵是**完整的訊息文字**，不是「警告種類」。

    使用者改了設定、換成另一種錯法時那是一段新文字，必須再看得到一次；否則第一
    個錯字會把之後所有的診斷永久靜音。
    """
    _music(music_file, {"smtc_source_substrings": []})
    pp._load_music_rules()
    _music(music_file, {"browser_aumid_substrings": []})
    pp._load_music_rules()
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    assert len(lines) == 2, f"第二種錯法被吃掉了：{lines}"


# ===========================================================================
# 音樂偵測「成功的那一條路」
# ===========================================================================
# 2026-09-08 量到的事：SMTC 的解析段與整支 `probe_foreground_music`
# **一行都沒有被測試執行過**——也就是這個功能會不會動，從來沒有人驗過；被驗過的
# 只有它的各種失敗路徑。而失敗路徑全部回 `None`，跟「現在真的沒在聽音樂」長得
# 一模一樣，所以就算成功那一條整個壞掉，症狀也只是安靜地什麼都不顯示。

class _FakeWindowApi:
    def __init__(self, title):
        self._title = title

    def foreground_window(self):
        return None if self._title is None else (1234, self._title)


def _foreground_title(monkeypatch, title):
    """把前景視窗的**標題**換掉。

    ⚠️ 不要叫 `_foreground`——這個檔案裡已經有一支同名的 helper，換掉的是前景
    視窗的 **pid**。同名的 module-level def 會**安靜地**蓋掉前面那個（Python 不
    會警告），於是既有測試的意思被改掉而寫的人毫無所覺。這裡就發生過一次，是
    `test_without_a_priority_game_the_foreground_one_wins` 變紅才抓到的。
    """
    monkeypatch.setattr(pp, "_window_api", lambda: _FakeWindowApi(title))


def test_a_music_shaped_window_title_is_parsed(music_file, monkeypatch):
    """成功那一條：標題符合設定的樣式 → 拆出曲名與歌手。"""
    _music(music_file, {"foreground_window_patterns": [
        "^(?P<song>.+) - (?P<artist>.+) - YouTube Music$"]})
    _foreground_title(monkeypatch, "Nightcall - Kavinsky - YouTube Music")
    assert pp.probe_foreground_music() == {
        "title": "Nightcall", "artist": "Kavinsky", "source": "foreground"}


def test_an_ordinary_window_title_is_never_reported_as_music(music_file,
                                                             monkeypatch):
    """**這是這個模組對使用者的隱私承諾，而在此之前沒有任何東西在守它。**

    模組 docstring 第一段就寫著「前景視窗只在嚴格的音樂標題 pattern 會被當訊號
    用，其他一般視窗標題不會被當成 activity」。這條要是壞掉，症狀不是「少顯示
    東西」，而是**把使用者正在看的任何視窗標題廣播到聊天平台上**——錯的方向
    完全相反，而且沒有人會發現自己在廣播。
    """
    _music(music_file, {"foreground_window_patterns": [
        "^(?P<song>.+) - (?P<artist>.+) - YouTube Music$"]})
    for title in ("Untitled - Notepad",
                  "私人文件.docx - Word",
                  "某某銀行 - 網路銀行 - Chrome",
                  "Discord"):
        _foreground_title(monkeypatch, title)
        assert pp.probe_foreground_music() is None, (
            f"一般視窗標題 {title!r} 被當成音樂了")


def test_the_first_matching_pattern_wins(music_file, monkeypatch):
    """有多個樣式時依設定檔的順序，第一個命中的就決定答案。"""
    _music(music_file, {"foreground_window_patterns": [
        "^(?P<song>.+) - First$", "^(?P<song>.+)$"]})
    _foreground_title(monkeypatch, "Song - First")
    assert pp.probe_foreground_music()["title"] == "Song"


def test_a_pattern_that_matches_with_an_empty_song_keeps_looking(music_file,
                                                                 monkeypatch):
    """命中但 `song` 抓到空字串 → 不算數，繼續看下一個樣式。

    少了 `if song:` 那道，activity 會變成一個空標題的「正在聽」。
    """
    _music(music_file, {"foreground_window_patterns": [
        "^(?P<song>z*)Track - (?P<artist>.+)$",    # 先命中，但 song 抓到空字串
        "^(?P<song>.+) - (?P<artist>.+)$"]})
    _foreground_title(monkeypatch, "Track - Kavinsky")
    assert pp.probe_foreground_music() == {
        "title": "Track", "artist": "Kavinsky", "source": "foreground"}


def test_no_foreground_window_is_not_an_error(music_file, monkeypatch):
    _foreground_title(monkeypatch, None)
    assert pp.probe_foreground_music() is None
    assert pp.probe_foreground_window_raw() is None


def test_a_window_api_that_raises_is_not_fatal(music_file, monkeypatch):
    """視窗查詢丟例外時要回 None，不可以往外拋——這支掛在每個 presence tick 上。"""
    class _Boom:
        def foreground_window(self):
            raise RuntimeError("視窗查詢炸了")

    monkeypatch.setattr(pp, "_window_api", lambda: _Boom())
    assert pp.probe_foreground_window_raw() is None
    assert pp.probe_foreground_music() is None


def test_the_smtc_payload_is_actually_parsed(capsys, monkeypatch):
    """SMTC 成功那一條：真的跑一次 PowerShell，讓它吐出一份 JSON。

    跟同檔其他 SMTC 測試一樣換掉 `_PS_PROBE` 而不是換掉整個 `create_subprocess_exec`
    ——這樣連「子行程的 stdout 怎麼解碼」都一起走過一遍。
    """
    payload = ('{"title":"Nightcall","artist":"Kavinsky",'
               '"source":"Spotify.exe","playbackType":"Music"}')
    monkeypatch.setattr(pp, "_PS_PROBE", f"Write-Output '{payload}'")
    capsys.readouterr()
    assert _run(pp.probe_smtc_raw_async(timeout=60.0)) == {
        "title": "Nightcall", "artist": "Kavinsky",
        "source": "Spotify.exe", "playback_type": "Music"}
    assert capsys.readouterr().err == ""


def test_a_non_string_smtc_field_does_not_kill_the_tick(capsys, monkeypatch):
    """欄位型別不對時要安靜降級，不可以丟 `AttributeError`。

    原本這四個欄位寫的是 `(data.get(k) or "").strip()`，而 `123 or ""` 的結果是
    `123`——`.strip()` 當場拋 `AttributeError`。資料來自**另一個行程**的 stdout，
    本模組對這種輸入的既定作法是 `_as_text()`（它的 docstring 自稱「本模組唯一的
    字串入口」），這裡是那條規則唯一沒被遵守的地方。
    """
    payload = '{"title":123,"artist":null,"source":true,"playbackType":[]}'
    monkeypatch.setattr(pp, "_PS_PROBE", f"Write-Output '{payload}'")
    capsys.readouterr()
    got = _run(pp.probe_smtc_raw_async(timeout=60.0))
    # `123` 是數字，`_as_text` 會字串化；其餘非文字值一律變空字串。
    assert got == {"title": "123", "artist": "", "source": "",
                   "playback_type": ""}


@pytest.mark.parametrize("payload", [
    '[1, 2]',                                   # 合法 JSON，但不是物件
    '"Nightcall"',
    '{"title": "", "artist": "Kavinsky", "source": "Spotify.exe"}',
    '{"artist": "Kavinsky", "source": "Spotify.exe"}',
])
def test_a_payload_without_a_usable_title_reads_as_nothing_playing(payload, capsys,
                                                                   monkeypatch):
    """不是物件的 JSON 會在 `data.get` 丟 `AttributeError`，少掉一個 presence tick；
    沒有標題的結果若照樣回傳，狀態列會顯示一首空白的歌。兩者都要當成「沒在播」。"""
    monkeypatch.setattr(pp, "_PS_PROBE", f"Write-Output '{payload}'")
    capsys.readouterr()
    assert _run(pp.probe_smtc_raw_async(timeout=60.0)) is None
    assert capsys.readouterr().err == ""
