"""bot 這一側的背景程式監督者（`discord_bot._watch_for_fallback`）——270 行裡有
138 行敘述，其中 **78 行從來沒有被任何測試執行過**。

`test_bot_helpers` 已經有一組針對它的測試，但那一組刻意只走**一條**路：變體釘死
`selenium`、牆上時鐘會跳、rc 永遠是 1。它守的是「間隔要用單調時鐘量」與退避序列，
而且**不可以**動 `time.monotonic`（那一組的註解寫得很清楚：asyncio 的事件迴圈拿它
當自己的時鐘，餵它會跳的值會讓測試以無關的理由變紅或直接掛住）。

這一檔補的是那條路以外的每一個出口，而它們幾乎全都是「安靜地不再監督」：

* rc 正常結束、使用者按 `/stop`、Chrome 槽讓不出來 —— 該**退出**卻繼續重生；
* 站方擋住生成（需要人處理）—— 該停下來卻無限重生，每輪重跑一次登入與設定；
* je → selenium 的啟動視窗 —— 該換備援卻一直重生一個起不來的變體；
* 零產出閘 —— 死得很慢卻什麼都沒產出，rapid-fail 那道閘永遠不會響；
* spawn 期間的 `/stop` 競態 —— 新生的行程沒被殺掉，`/stop` 之後還在產圖；
* 監督者自己掛掉 —— task 靜默結束，該批次從此失去監督而沒有任何人被通知。

沒有一種會拋例外到使用者面前。**時鐘的處理方式跟那一組相反而且是刻意的**：這裡換掉
的是 `discord_bot` 命名空間裡的 `time`，不是 `time` 模組本身，所以 asyncio 的事件
迴圈用的仍然是真正的單調時鐘。同樣地 `asyncio.sleep` 只在 bot 的命名空間裡被換成
「推進假時鐘」，於是整支監督者跑完不花牆上時間，而每一個等待都看得見。
"""
import ast
import asyncio
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402
import _supervisor  # noqa: E402

BOT_SOURCE = Path(__file__).resolve().parent.parent / "axiomatic" / "discord_bot.py"
RC_BLOCKED = _supervisor.RC_GENERATION_BLOCKED
RC_ZERO = b.RC_ZERO_PROGRESS


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------

class _Clock:
    """假的單調時鐘。只有 `discord_bot` 看得到它——事件迴圈用的還是真的那個。"""

    def __init__(self):
        self.now = 10_000.0

    def monotonic(self):
        return self.now


class _Proc:
    """一個「活了 `alive` 秒然後以 `rc` 結束」的假子行程。

    第一次 `poll()` 記下起點，之後每次都拿假時鐘比對——所以「活多久」是由監督者
    自己的 `await asyncio.sleep(2)` 推進的，跟真實時間無關。
    """

    def __init__(self, rc, alive, clock):
        self.rc = rc
        self.alive = alive
        self.clock = clock
        self.returncode = None
        self.start = None
        self.terminated = 0

    def poll(self):
        if self.start is None:
            self.start = self.clock.now
        if self.clock.now - self.start >= self.alive:
            self.returncode = self.rc
            return self.rc
        return None

    def terminate(self):
        self.terminated += 1


@pytest.fixture
def sup_env(monkeypatch):
    """把監督者的每一個對外動作換成替身，回傳可腳本化的 env。

    `env.script` 是**之後每一次重生**要拿到的行程，一個元素一次：`(rc, alive)`。
    腳本用完 → 假的 spawner 回 `(False, …)`，監督者看到 spawn 失敗就退出。**迴圈
    的終止是結構性的**，不靠哨兵例外：這支函式最外層有一個 blanket `except
    Exception`，丟例外只會變成「監督機制異常結束」，把真正的斷言藏起來。
    """
    env = types.SimpleNamespace(
        clock=_Clock(), sent=[], spawns=[], sleeps=[], cleared=0,
        acquired=[], released=0, script=[], max_spawns=12,
        acquire_result=True, spawn_raises_at=None, procs=[],
        spawn_reports_success_without_a_process=False,
    )

    class _Channel:
        id = 42

        async def send(self, content=None, **_kwargs):
            if env.send_all_raise or (env.send_raises
                                      and env.send_raises in str(content)):
                raise RuntimeError("送不出去")
            env.sent.append(content)

    env.send_raises = None
    env.send_all_raise = False
    env.on_sleep = None
    env.channel = _Channel()

    fake_time = types.SimpleNamespace(monotonic=env.clock.monotonic,
                                      time=time.time, sleep=time.sleep)

    class _AsyncioProxy:
        """只換掉 `sleep`，其餘（`CancelledError`／`current_task`）走真的 asyncio。"""

        def __getattr__(self, name):
            return getattr(asyncio, name)

        async def sleep(self, delay):
            env.sleeps.append(delay)
            env.clock.now += max(0.0, delay)
            if env.on_sleep is not None:
                env.on_sleep(delay)

    def fake_spawn(variant, *, single_image_server=False):
        if env.spawn_reports_success_without_a_process:
            env.spawns.append(variant)
            return True, "已啟動（但沒有行程）"
        # 簽章跟著真的那支走。替身少一個參數的話，重生會丟 `TypeError` 而不是重生，
        # 然後被最外層的 blanket except 吞掉——量到的是「重生 0 次」，而好幾種期望
        # 值剛好就是 0。
        assert single_image_server is False, (
            "監督者重生的必定是批次，帶上單張伺服器旗標會讓整條佇列從此不產圖")
        env.spawns.append(variant)
        if env.spawn_raises_at is not None and len(env.spawns) >= env.spawn_raises_at:
            raise OSError("log 開不起來")
        if not env.script or len(env.spawns) > env.max_spawns:
            return False, "沒有更多腳本了"
        rc, alive = env.script.pop(0)
        proc = _Proc(rc, alive, env.clock)
        env.procs.append(proc)
        b._webrunner_proc = proc
        b._webrunner_pid = 9000 + len(env.spawns)
        b._webrunner_variant = variant
        return True, f"已啟動背景產圖程式（{variant}）"

    async def fake_acquire(label):
        env.acquired.append(label)
        return env.acquire_result

    monkeypatch.setattr(b, "time", fake_time)
    monkeypatch.setattr(b, "asyncio", _AsyncioProxy())
    monkeypatch.setattr(b, "_spawn_webrunner", fake_spawn)
    monkeypatch.setattr(b, "_acquire_chrome_slot_for_respawn", fake_acquire)
    monkeypatch.setattr(b, "_release_chrome_slot",
                        lambda: setattr(env, "released", env.released + 1))
    monkeypatch.setattr(b, "_clear_pid",
                        lambda: setattr(env, "cleared", env.cleared + 1))
    monkeypatch.setattr(b, "traceback",
                        types.SimpleNamespace(print_exc=lambda: None))
    monkeypatch.setattr(b, "_webrunner_stop_requested", False)
    monkeypatch.setattr(b, "_webrunner_proc", None)
    monkeypatch.setattr(b, "_webrunner_pid", None)
    monkeypatch.setattr(b, "_webrunner_variant", None)
    monkeypatch.setattr(b, "_webrunner_fallback_task", None)
    # 失敗的當下監督者會問一次「網路還在嗎」（`_batch_network_is_down`）。這裡一律
    # 回「在」：真的去探測的話，測試結果會隨著跑測試那台機器當下有沒有網路而變，
    # 斷網的機器上整組會走進「停下來等網路」那條路而掛住。斷網那條路有自己的測試
    # （`test_batch_recovery.py`）。電源要求同理關掉——它的行為也在那一檔測。
    async def _network_up():
        return False

    monkeypatch.setattr(b, "_batch_network_is_down", _network_up)
    monkeypatch.setattr(b, "KEEP_BOT_AWAKE", False)
    # 這幾支測試驗的是判斷，不是等待——但**不可以**壓到 0：`restart_backoff` 對
    # `minimum <= 0` 會丟 `ValueError`，而它會被最外層的 blanket handler 吞掉，
    # 於是失敗會以「監督機制異常結束」的形式出現，看起來跟退避毫無關係。
    monkeypatch.setattr(b, "WEBRUNNER_RESPAWN_BACKOFF_MIN_SEC", 2.0)
    monkeypatch.setattr(b, "WEBRUNNER_RESPAWN_BACKOFF_MAX_SEC", 16.0)
    monkeypatch.setattr(b, "WEBRUNNER_HEALTHY_THRESHOLD_SEC", 60.0)
    monkeypatch.setattr(b, "WEBRUNNER_RAPID_FAIL_THRESHOLD_SEC", 30.0)
    monkeypatch.setattr(b, "WEBRUNNER_RAPID_FAIL_GIVEUP_COUNT", 3)
    monkeypatch.setattr(b, "WEBRUNNER_ZERO_PROGRESS_GIVEUP_COUNT", 2)
    monkeypatch.setattr(b, "WEBRUNNER_FALLBACK_WINDOW_SEC", 300)
    return env


def _start(env, rc=1, alive=0.0, variant="selenium"):
    """擺好「第一個正在跑的行程」，也就是監督者被掛上去的那一刻的狀態。"""
    proc = _Proc(rc, alive, env.clock)
    env.procs.append(proc)
    b._webrunner_proc = proc
    b._webrunner_pid = 8888
    b._webrunner_variant = variant
    return proc


def _run(env, own_task=True):
    """跑完整個監督者。回傳它結束時的三個全域值。"""
    async def _body():
        if own_task:
            b._webrunner_fallback_task = asyncio.current_task()
        await b._watch_for_fallback(env.channel)
        return (b._webrunner_proc, b._webrunner_pid, b._webrunner_variant)

    result = asyncio.run(_body())
    assert len(env.sleeps) < 5000, (
        f"監督者睡了 {len(env.sleeps)} 次——迴圈大概沒有停下來")
    return result


def _said(env):
    return "\n".join(str(x) for x in env.sent)


# --------------------------------------------------------------------------
# 一、該退出的時候要退出
# --------------------------------------------------------------------------

def test_a_clean_exit_ends_the_supervision_without_respawning(sup_env):
    """rc == 0 ＝ 佇列跑完了，不是崩潰。

    當成崩潰處理的話，每一次**成功**跑完的批次都會被重生一次：佇列已經空了，
    新的行程開起來、登入、setup、發現沒事做、結束——然後再被重生一次。
    """
    _start(sup_env, rc=0, alive=100.0)
    _run(sup_env)
    assert sup_env.spawns == []
    assert sup_env.sent == []


def test_a_user_stop_during_the_wait_ends_the_supervision(sup_env):
    """`/stop` 期間不可以重生——那正是使用者剛剛要求停掉的東西。

    這個旗標在**行程還活著**的時候就要看：`cmd_stop` 設旗標之後才去殺行程，
    所以監督者會先看到旗標、再看到行程死掉。晚一步檢查就是「按了停止，它又
    自己開起來了」。

    ⚠️ 斷言看的是**有沒有等**，不是「有沒有重生」。這支函式裡有三道停止檢查
    （行程還活著時、行程死掉之後、退避等待中），三道都會擋下重生——所以
    「重生次數 0」在拿掉任何**一道**之後仍然成立，三個變異全部活下來。分得開
    這一道的唯一觀察是：它在**第一次輪詢**就退出，一秒都沒有等。
    """
    proc = _start(sup_env, rc=1, alive=10.0)

    original_poll = proc.poll

    def _poll_then_stop():
        b._webrunner_stop_requested = True
        return original_poll()

    proc.poll = _poll_then_stop
    _run(sup_env)
    assert sup_env.spawns == []
    assert sup_env.sent == []
    assert sup_env.sleeps == [], (
        "停止旗標已經設起來了，監督者卻還在等這個行程死掉："
        f"{sup_env.sleeps}。這一道檢查就是為了不要等。")


def test_a_stop_that_lands_while_the_process_is_dying_is_still_honoured(sup_env):
    """行程已經死了、旗標才設起來——第二道檢查就是為了這半秒。

    少了它，`/stop` 與一次自然崩潰撞在一起時會重生一個新行程，而 `cmd_stop`
    早就跑完了，沒有人會再去殺它。

    ⚠️ 同樣不能只看「重生次數」：下游的退避等待裡還有一道停止檢查會攔下來。
    分得開這一道的是**有沒有宣告**——少了它，使用者在按下停止之後還會收到一則
    「supervisor 將在 Ns 後重新啟動」，而那句話是假的。
    """
    _start(sup_env, rc=1, alive=0.0)
    b._webrunner_stop_requested = True
    sup_env.script = [(1, 0.0)]
    _run(sup_env)
    assert sup_env.spawns == []
    assert sup_env.sent == [], (
        f"按了停止之後還宣告要重新啟動：{sup_env.sent}")


def test_no_process_to_watch_ends_immediately(sup_env):
    """沒有行程可看就直接結束——不是空轉一個永遠不會有結果的迴圈。"""
    b._webrunner_proc = None
    _run(sup_env)
    assert sup_env.spawns == [] and sup_env.sent == []


# --------------------------------------------------------------------------
# 二、站方擋住生成：停下來等人，不要重生
# --------------------------------------------------------------------------

def test_a_blocked_generation_stops_instead_of_respawning(sup_env):
    """需要人處理的結束碼 ＝ 重生一定沒用，每輪只會看到同一個對話框。

    這是最貴的一條：重生會**每輪重跑一次登入與整套設定**（模型／角色／解析度／
    取樣器），而結果一定是同一個。使用者看到的是頻道被刷屏，而真正該做的事
    （去瀏覽器處理帳號）沒有人告訴他。
    """
    _start(sup_env, rc=RC_BLOCKED, alive=100.0)
    sup_env.script = [(1, 0.0)]
    proc, pid, variant = _run(sup_env)
    assert sup_env.spawns == [], "需要人處理的結束碼不得重生"
    assert "需要你先到瀏覽器處理帳號相關事項" in _said(sup_env)
    assert (proc, pid, variant) == (None, None, None), "停下來就要把狀態清乾淨"
    assert sup_env.cleared == 1, "pid 檔沒清掉的話，下一次 `/run` 會以為還有人在跑"


def test_the_blocked_notice_failing_still_clears_the_state(sup_env):
    """通知送不出去不可以讓清理跳過。

    送失敗而狀態沒清的結果是：行程已經死了，但 bot 以為它還活著——`/run` 會說
    「已經在跑了」，而使用者眼前什麼都沒有發生。
    """
    _start(sup_env, rc=RC_BLOCKED, alive=100.0)
    sup_env.send_raises = "需要你先到瀏覽器"
    proc, pid, variant = _run(sup_env)
    assert (proc, pid, variant) == (None, None, None)
    assert sup_env.cleared == 1


# --------------------------------------------------------------------------
# 三、je → selenium 的啟動視窗
# --------------------------------------------------------------------------

def test_a_je_crash_inside_the_startup_window_switches_to_the_fallback(sup_env):
    """主要模式在啟動視窗內死掉 → 換備援模式，而不是一直重生同一個。

    少了這條，一台裝不起主要模式的機器會無限重生它——每一輪都花完整的啟動時間，
    然後死在同一個地方，而備援模式明明可以跑。
    """
    _start(sup_env, rc=1, alive=10.0, variant="je")
    sup_env.script = [(0, 100.0)]
    _run(sup_env)
    assert sup_env.spawns == ["selenium"]
    assert "改用備援模式重啟" in _said(sup_env)


def test_a_je_crash_after_the_startup_window_is_an_ordinary_respawn(sup_env,
                                                                    monkeypatch):
    """視窗外的崩潰是普通的崩潰——那時候主要模式已經證明自己能跑了。

    不分視窗內外的話，一個跑了三天的主要模式批次在崩一次之後會被換成備援模式，
    而換掉的理由（「它起不來」）根本不成立。

    ※ 視窗的起點是**監督者被掛上去的那一刻**（`je_started_at` 在函式進入時才讀），
    所以「讓視窗過去」只能靠把視窗調小，事先推進時鐘沒有用——第一版就是這樣寫的，
    結果量到的是視窗**內**的行為。
    """
    monkeypatch.setattr(b, "WEBRUNNER_FALLBACK_WINDOW_SEC", 5)
    _start(sup_env, rc=1, alive=10.0, variant="je")
    sup_env.script = [(0, 100.0)]
    _run(sup_env)
    assert sup_env.spawns == ["je"], "視窗外不該換變體"
    assert "改用備援模式" not in _said(sup_env)


def test_the_fallback_only_happens_once(sup_env):
    """換過一次之後就不再是「第一次跑主要模式」了。

    旗標沒清的話，備援模式自己崩一次也會再「fallback」一次——而它已經是備援了，
    等於在原地繞圈，每一圈都重跑一次登入。

    ⚠️ 擋下第二次的是**兩道**條件——`je_fallback_pending` 與 `current_variant ==
    "je"`——而量過之後：**單獨拿掉任何一道，這支測試都還是綠的**。兩道在這支函式裡
    是同一件事的兩種寫法（旗標只在變體是 je 時為真，而唯一會換變體的兩條路都同時
    把旗標關掉），所以沒有任何輸入分得開它們。

    先前這段註解寫的是「承重的是變體那一半」——那是**猜的，而且猜錯了**：把變體那
    一半拿掉，測試照樣綠。分不開就是分不開，正確的記法是「承重的是這一對」，而變異
    測試裡對應的也是把兩道一起拿掉的那一個。
    """
    _start(sup_env, rc=1, alive=10.0, variant="je")
    sup_env.script = [(1, 10.0), (0, 100.0)]
    _run(sup_env)
    assert sup_env.spawns == ["selenium", "selenium"]
    assert _said(sup_env).count("改用備援模式重啟") == 1


def test_the_fallback_waits_for_the_browser_slot_and_gives_it_back(sup_env):
    """先拿 Chrome 槽再 spawn；拿不到就退出，而且一定要還。

    不拿槽的話，瀏覽器驗證正在跑時會開出第二個瀏覽器 stack，兩邊搶同一個設定檔；
    不還的話，之後每一次要用瀏覽器的工作都會永遠等下去。
    """
    _start(sup_env, rc=1, alive=10.0, variant="je")
    sup_env.script = [(0, 100.0)]
    _run(sup_env)
    assert sup_env.acquired == ["webrunner-fallback"]
    assert sup_env.released == 1


def test_a_refused_browser_slot_ends_the_supervision_without_spawning(sup_env):
    """槽讓不出來（`/stop` 期間）→ 退出，不 spawn。

    忽略這個回傳值就是在使用者按下停止的當下又開一個瀏覽器出來。
    """
    _start(sup_env, rc=1, alive=10.0, variant="je")
    sup_env.acquire_result = False
    sup_env.script = [(0, 100.0)]
    _run(sup_env)
    assert sup_env.spawns == []
    assert sup_env.released == 0, "沒拿到就不該去還"


# --------------------------------------------------------------------------
# 四、一般重生與退避
# --------------------------------------------------------------------------

def test_an_ordinary_crash_respawns_the_same_variant(sup_env, monkeypatch):
    """重生的是**同一個**變體——監督者不會順手換掉使用者選的模式。"""
    monkeypatch.setattr(b, "WEBRUNNER_FALLBACK_WINDOW_SEC", 5)
    _start(sup_env, rc=1, alive=100.0, variant="je")
    sup_env.script = [(0, 100.0)]
    _run(sup_env)
    assert sup_env.spawns == ["je"]
    assert sup_env.acquired == ["webrunner-respawn"]


def test_the_backoff_doubles_and_a_healthy_run_resets_it(sup_env):
    """退避加倍，而「活得夠久」要把它打回起點。

    不 reset 的話，一個每天崩一次、但每次都跑滿二十小時的健康批次，會在幾天之內
    把重啟間隔推到上限——崩一次之後要等五分鐘才回來，而它其實非常健康。

    每一輪的存活時間刻意挑在 rapid-fail 門檻（30s）與健康門檻（60s）**之間**：
    低於 30s 會先撞上放棄閘、迴圈提早結束，量到的序列就只剩兩項。
    """
    _start(sup_env, rc=1, alive=40.0)
    sup_env.script = [(1, 40.0), (1, 40.0), (1, 200.0), (1, 40.0), (0, 100.0)]
    _run(sup_env)
    waits = [float(m.split("將在 ")[1].split("s 後")[0])
             for m in sup_env.sent if "將在 " in m]
    assert waits[:3] == [2.0, 4.0, 8.0], waits
    assert waits[3] == 2.0, f"健康 run 之後沒有打回起點：{waits}"


def test_the_backoff_is_capped(sup_env):
    """封頂——否則連續崩潰幾十次之後，重啟間隔會長到跟「放棄」沒有分別。"""
    _start(sup_env, rc=1, alive=40.0)
    sup_env.script = [(1, 40.0)] * 6 + [(0, 100.0)]
    _run(sup_env)
    waits = [float(m.split("將在 ")[1].split("s 後")[0])
             for m in sup_env.sent if "將在 " in m]
    assert max(waits) <= b.WEBRUNNER_RESPAWN_BACKOFF_MAX_SEC, waits


def test_a_stop_during_the_backoff_wait_is_noticed_within_half_a_second(
        sup_env, monkeypatch):
    """退避不是一次 `sleep(backoff)`，而是每 0.5 秒看一次旗標。

    一次睡完的話，`/stop` 要等整段退避結束才生效——上限是五分鐘，而使用者按的是
    「立刻停止」。

    退避調成 3 秒，跟行程輪詢用的 2 秒區隔開：**沒有任何一次 3 秒的等待**就是
    「它不是一口氣睡完」的證據，而那正是唯一分得開兩種寫法的地方。
    """
    monkeypatch.setattr(b, "WEBRUNNER_RESPAWN_BACKOFF_MIN_SEC", 3.0)
    monkeypatch.setattr(b, "WEBRUNNER_RESPAWN_BACKOFF_MAX_SEC", 3.0)
    _start(sup_env, rc=1, alive=40.0)
    sup_env.script = [(0, 100.0)]
    polls = []

    def _stop_after_two_polls(delay):
        if delay <= 0.5:
            polls.append(delay)
            if len(polls) >= 2:
                b._webrunner_stop_requested = True

    sup_env.on_sleep = _stop_after_two_polls
    _run(sup_env)
    assert sup_env.spawns == [], "退避期間按了停止還是重生了"
    assert len(polls) == 2, f"旗標不是每 0.5 秒看一次：{sup_env.sleeps}"
    assert 3.0 not in sup_env.sleeps, (
        f"退避被一口氣睡完了，`/stop` 要等它醒來才生效：{sup_env.sleeps}")


# --------------------------------------------------------------------------
# 五、兩道放棄閘：死得太快，與死得很慢卻什麼都沒產出
# --------------------------------------------------------------------------

def test_repeated_instant_crashes_give_up_instead_of_churning(sup_env):
    """連續秒崩 N 次 → 放棄重啟。

    沒有這道閘，一台 Chrome 根本開不起來的機器會無限重生：頻道被刷屏，而每一輪
    都重跑一次首次登入與整套設定。
    """
    _start(sup_env, rc=1, alive=0.0)
    sup_env.script = [(1, 0.0)] * 10
    proc, pid, variant = _run(sup_env)
    assert sup_env.spawns == ["selenium"] * (b.WEBRUNNER_RAPID_FAIL_GIVEUP_COUNT - 1)
    assert "supervisor 放棄重啟" in _said(sup_env)
    assert (proc, pid, variant) == (None, None, None)
    # 每一次重生前會清一次 pid 檔，放棄的那一次再清一次——所以是重生次數 ＋ 1。
    assert sup_env.cleared == len(sup_env.spawns) + 1


def test_a_healthy_run_clears_the_rapid_fail_count(sup_env):
    """中間活得夠久的一輪要把計數歸零——那道閘抓的是「連續」秒崩。

    不歸零的話，一個跑了好幾個月、偶爾崩一下的批次會在累積到門檻的那一天被放棄，
    而那幾次崩潰之間隔著好幾百小時。
    """
    _start(sup_env, rc=1, alive=0.0)
    sup_env.script = [(1, 0.0), (1, 100.0), (1, 0.0), (1, 0.0), (0, 100.0)]
    _run(sup_env)
    assert "放棄重啟" not in _said(sup_env), _said(sup_env)
    assert len(sup_env.spawns) == 5


def test_a_slow_but_unhealthy_death_also_clears_the_rapid_fail_count(sup_env):
    """計數有**兩處**會歸零，而只有這種存活時間分得開它們。

    一處在 `if healthy:` 裡，一處在「這次不是 rapid fail」的 `else` 裡。活得夠久
    （≥ 健康門檻）的一輪同時滿足兩者，所以拿掉任何一處都還是會歸零——上一版的測試
    正是用那種輪次，於是「`else` 那半被拿掉」的變異活了下來。

    唯一分得開的輸入是「死得不快，但也還不算健康」（介於 rapid-fail 門檻與健康門檻
    之間）：這種輪次只有 `else` 那一處會歸零。漏掉它的後果是一個偶爾慢慢死掉的批次
    會把 rapid-fail 計數一路累積上去，最後被當成「一直秒崩」而放棄。
    """
    _start(sup_env, rc=1, alive=0.0)
    sup_env.script = [(1, 0.0), (1, 40.0), (1, 0.0), (1, 0.0), (0, 100.0)]
    _run(sup_env)
    assert "放棄重啟" not in _said(sup_env), _said(sup_env)
    assert len(sup_env.spawns) == 5


def test_a_slow_death_never_counts_as_a_rapid_fail(sup_env):
    """死得慢就不是 rapid fail——這道閘的判準是**多快**死的，不是死了幾次。"""
    _start(sup_env, rc=1, alive=40.0)
    sup_env.script = [(1, 40.0)] * 6 + [(0, 100.0)]
    _run(sup_env)
    assert "放棄重啟" not in _said(sup_env)
    assert "rapid-fail" not in _said(sup_env)


def test_repeated_zero_progress_rounds_give_up(sup_env):
    """連續幾輪「跑完了卻一張都沒產出」→ 停下來讓人看。

    這道閘與 rapid-fail **互補**：生成被擋住時，每一輪都要跑完連續失敗上限才結束，
    遠超過秒崩的門檻，所以那道閘永遠不會響。少了這道，bot 會用完整的速度，一輪
    一輪地什麼都不產出，而使用者以為它在工作。
    """
    _start(sup_env, rc=RC_ZERO, alive=200.0)
    sup_env.script = [(RC_ZERO, 200.0)] * 5
    proc, pid, variant = _run(sup_env)
    assert len(sup_env.spawns) == b.WEBRUNNER_ZERO_PROGRESS_GIVEUP_COUNT - 1
    assert "都沒有產出任何圖" in _said(sup_env)
    assert (proc, pid, variant) == (None, None, None)
    assert sup_env.cleared == len(sup_env.spawns) + 1


def test_one_productive_round_clears_the_zero_progress_count(sup_env):
    """中間有一輪不是零產出就要歸零——同樣是「連續」。"""
    _start(sup_env, rc=RC_ZERO, alive=200.0)
    sup_env.script = [(1, 200.0), (RC_ZERO, 200.0), (0, 100.0)]
    _run(sup_env)
    assert "都沒有產出任何圖" not in _said(sup_env)
    assert len(sup_env.spawns) == 3


def test_the_two_giveup_gates_count_separately(sup_env):
    """兩道閘各數各的：慢速零產出不該累積 rapid-fail，秒崩也不該累積零產出。

    共用一個計數器的話，兩種完全不同的故障會互相提前觸發對方的放棄——而訊息會
    告訴使用者一個錯的原因，那比沒有訊息更貴。
    """
    _start(sup_env, rc=RC_ZERO, alive=200.0)
    sup_env.script = [(RC_ZERO, 200.0), (0, 100.0)]
    _run(sup_env)
    assert "rapid-fail" not in _said(sup_env), "慢速的零產出不是 rapid fail"


# --------------------------------------------------------------------------
# 六、spawn 之後的停止競態
# --------------------------------------------------------------------------

def test_a_stop_that_lands_during_the_spawn_kills_the_new_process(sup_env):
    """spawn 是同步的，期間使用者可能剛好按下停止。

    那一瞬間 `cmd_stop` 看到的 `_webrunner_proc` 還是 None，所以它殺不到新生的
    行程；這道重檢就是唯一的補救。少了它，`/stop` 回報成功，而一個新的批次正在
    背景產圖。
    """
    _start(sup_env, rc=1, alive=1.0)
    sup_env.script = [(1, 100.0)]
    real_spawn = b._spawn_webrunner

    def _spawn_then_stop(variant, **kwargs):
        result = real_spawn(variant, **kwargs)
        b._webrunner_stop_requested = True
        return result

    b._spawn_webrunner = _spawn_then_stop
    proc, pid, variant = _run(sup_env)
    assert sup_env.procs[-1].terminated == 1, "新生的行程沒有被殺掉"
    assert (proc, pid, variant) == (None, None, None)
    assert sup_env.cleared >= 1


# --------------------------------------------------------------------------
# 七、監督者自己掛掉
# --------------------------------------------------------------------------

def test_the_supervisor_announces_its_own_death(sup_env):
    """`_spawn_webrunner` 裡有幾個沒有保護的 raise 點（開檔被鎖、Popen、寫 pid 檔）。

    例外從這裡逸出的話 task 會**靜默**結束，該批次從此失去監督：再崩不會重生、
    放棄閘也不再計數，而使用者完全不知道保護已經沒了。
    """
    _start(sup_env, rc=1, alive=1.0)
    sup_env.spawn_raises_at = 1
    _run(sup_env)
    assert "監督機制異常結束" in _said(sup_env)


def test_the_browser_slot_is_returned_even_when_the_spawn_explodes(sup_env):
    """spawn 炸掉也要還槽——不還的話之後每一次要用瀏覽器的工作都永遠等下去。"""
    _start(sup_env, rc=1, alive=1.0)
    sup_env.spawn_raises_at = 1
    _run(sup_env)
    assert sup_env.released == 1


def test_a_cancel_is_re_raised_rather_than_reported_as_a_crash(sup_env):
    """`/stop` 用的是 cancel，那不是「監督者掛了」。

    被 blanket handler 當成崩潰吃掉的話，每一次正常的停止都會送出一則「監督機制
    異常結束」的警告——而一個會喊狼來了的警告，最後沒有人會讀。
    """
    _start(sup_env, rc=1, alive=1.0)
    sup_env.script = [(0, 100.0)]

    async def _cancel(content=None, **_kwargs):
        raise asyncio.CancelledError

    sup_env.channel.send = _cancel
    with pytest.raises(asyncio.CancelledError):
        _run(sup_env)
    assert "監督機制異常結束" not in _said(sup_env)


# --------------------------------------------------------------------------
# 八、收尾只清自己那一份註冊
# --------------------------------------------------------------------------

def test_the_finished_supervisor_clears_only_its_own_registration(sup_env):
    """結束時把自己的 task 參考清掉——但只有當那個參考真的是自己時。

    `cmd_stop` 是 cancel-without-await，所以「舊的 task 正在收尾」與「新的 task
    已經註冊上去」會重疊。無條件清就會抹掉**新** task 的參考，而新的那個還在跑：
    之後 `/stop` 找不到它，也不會有人知道。
    """
    _start(sup_env, rc=0, alive=1.0)
    _run(sup_env, own_task=True)
    assert b._webrunner_fallback_task is None

    sentinel = object()
    _start(sup_env, rc=0, alive=1.0)
    b._webrunner_fallback_task = sentinel
    _run(sup_env, own_task=False)
    assert b._webrunner_fallback_task is sentinel, (
        "清掉了別人的註冊——那個 task 還在跑，而 `/stop` 之後找不到它")


# --------------------------------------------------------------------------
# 九、頻道送不出去的時候
#
# 這一節的每一支都只在「別的東西已經壞了」的時候才會執行，所以它們是最不可能被
# 手動試出來的。監督者的每一則通知都包在自己的 try 裡，而那些 try 保護的不是通知
# 本身，是**通知後面那幾行**——清狀態、換變體、放棄重啟。少一個就變成「頻道剛好
# 抽風的那一刻，監督者順便忘了收尾」。
# --------------------------------------------------------------------------

def test_a_dead_channel_does_not_stop_the_fallback(sup_env):
    """通知送不出去，備援模式還是要換過去。

    換變體的那幾行排在通知之後。通知沒有被保護的話，一次送出失敗就會讓整支監督者
    跳到最外層的 blanket handler——主要模式起不來，而備援根本沒被試過。
    """
    sup_env.send_all_raise = True
    _start(sup_env, rc=1, alive=10.0, variant="je")
    sup_env.script = [(0, 100.0)]
    _run(sup_env)
    assert sup_env.spawns == ["selenium"]


def test_a_dead_channel_does_not_stop_the_rapid_fail_giveup(sup_env):
    """放棄閘的清理排在通知之後——通知失敗不可以讓它跳過。

    跳過的話，行程已經死透了而 bot 以為還有人在跑：`/run` 會說「已經在跑了」，
    使用者眼前什麼都沒有發生，而且沒有任何訊息解釋這件事。
    """
    sup_env.send_all_raise = True
    _start(sup_env, rc=1, alive=0.0)
    sup_env.script = [(1, 0.0)] * 6
    proc, pid, variant = _run(sup_env)
    assert (proc, pid, variant) == (None, None, None)
    assert len(sup_env.spawns) == b.WEBRUNNER_RAPID_FAIL_GIVEUP_COUNT - 1


def test_a_dead_channel_does_not_stop_the_zero_progress_giveup(sup_env):
    """零產出閘同理——它的清理也排在自己那則通知之後。"""
    sup_env.send_all_raise = True
    _start(sup_env, rc=RC_ZERO, alive=200.0)
    sup_env.script = [(RC_ZERO, 200.0)] * 4
    proc, pid, variant = _run(sup_env)
    assert (proc, pid, variant) == (None, None, None)
    assert len(sup_env.spawns) == b.WEBRUNNER_ZERO_PROGRESS_GIVEUP_COUNT - 1


def test_a_dead_channel_does_not_turn_a_crash_notice_into_a_second_crash(
        sup_env):
    """「監督者自己掛了」那則通知也送不出去時，不可以再往外拋一次。

    往外拋的話，例外會從 task 逸出——而它原本要通知的就是「監督已經沒了」，
    結果連那一則都沒有，事件迴圈只留下一行沒有人在看的 traceback。
    """
    sup_env.send_all_raise = True
    sup_env.spawn_raises_at = 1
    _start(sup_env, rc=1, alive=1.0)
    _run(sup_env)          # 不得往外拋
    assert sup_env.sent == []


def test_a_terminate_that_fails_still_clears_the_state(sup_env):
    """競態補救裡的 `terminate()` 可能丟例外（行程剛好自己死了）。

    那時候該做的事沒變：把狀態清掉、退出。讓例外逸出的話會被當成「監督者崩潰」，
    而真正發生的只是一個已經死掉的行程被殺了第二次。
    """
    _start(sup_env, rc=1, alive=1.0)
    sup_env.script = [(1, 100.0)]
    real_spawn = b._spawn_webrunner

    def _spawn_then_stop(variant, **kwargs):
        result = real_spawn(variant, **kwargs)
        b._webrunner_proc.terminate = _boom
        b._webrunner_stop_requested = True
        return result

    def _boom():
        raise OSError("行程早就不在了")

    b._spawn_webrunner = _spawn_then_stop
    proc, pid, variant = _run(sup_env)
    assert (proc, pid, variant) == (None, None, None)
    assert "監督機制異常結束" not in _said(sup_env)


# --------------------------------------------------------------------------
# 十、其餘的退出口
# --------------------------------------------------------------------------

def test_a_failed_respawn_ends_the_supervision(sup_env):
    """重生失敗（`_spawn_webrunner` 回報 False）→ 退出，不要空轉重試。

    ⚠️ **這一支殺不掉「把 `if not ok: return` 拿掉」那個變異，而那是量出來的。**
    原因在 `_spawn_webrunner` 自己：它只在兩個地方回 False，兩處都**還沒碰到**
    `_webrunner_proc`（Popen 之後的失敗是往上拋，不是回 False）。所以 `ok` 是
    False 時 `_webrunner_proc` 必定是 None，迴圈繞回頂端就被那道
    `if proc is None: return` 接住——兩道守門互相遮蔽，行為上分不開。

    這道守門仍然要留（它讓「結束」發生在知道原因的地方，而不是下一圈的一個泛用
    檢查），而釘住它的是下面那支**靜態**測試。這裡留著的是「失敗之後確實停了」
    這個行為。
    """
    _start(sup_env, rc=1, alive=40.0)
    sup_env.script = []            # 第一次重生就失敗
    _run(sup_env)
    assert sup_env.spawns == ["selenium"]
    assert "沒有更多腳本了" in _said(sup_env)


def test_a_fallback_that_cannot_start_either_ends_the_supervision(sup_env):
    """連備援模式都起不來 → 到此為止。

    Case A 與 Case B 各自檢查一次 spawn 的回傳值，這是**備援那一半**。與另一半
    同樣的狀況：行為上它被頂端的 `if proc is None: return` 遮蔽（理由見
    `test_a_failed_respawn_ends_the_supervision`），所以守它的是靜態那一支。
    """
    _start(sup_env, rc=1, alive=10.0, variant="je")
    sup_env.script = []
    _run(sup_env)
    assert sup_env.spawns == ["selenium"]
    assert "改用備援模式重啟" in _said(sup_env)


def test_a_refused_browser_slot_on_an_ordinary_respawn_also_ends_it(sup_env):
    """Case A 與 Case B 各自取一次 Chrome 槽，兩條都要看回傳值。

    這是「同一條規則的第二個實例」——只修一半是這種形狀最常見的下場，而漏掉的
    那一半一樣會在 `/stop` 期間開出第二個瀏覽器 stack。
    """
    _start(sup_env, rc=1, alive=40.0)
    sup_env.acquire_result = False
    sup_env.script = [(0, 100.0)]
    _run(sup_env)
    assert sup_env.acquired == ["webrunner-respawn"]
    assert sup_env.spawns == []
    assert sup_env.released == 0


def test_a_stop_seen_only_after_the_backoff_ended_still_prevents_the_respawn(
        sup_env):
    """退避等完之後還要再看一次旗標。

    只在迴圈裡面看的話，最後那半秒設起來的旗標會被錯過——`/stop` 回報成功，然後
    一個新的批次開起來。
    """
    _start(sup_env, rc=1, alive=40.0)
    sup_env.script = [(0, 100.0)]
    polls = []

    def _stop_on_the_last_poll(delay):
        if delay <= 0.5:
            polls.append(delay)
            if len(polls) >= 4:      # 退避 2s ÷ 0.5s ＝ 最後一次
                b._webrunner_stop_requested = True

    sup_env.on_sleep = _stop_on_the_last_poll
    _run(sup_env)
    assert sup_env.spawns == []


def test_a_spawn_that_reports_success_without_a_process_ends_the_supervision(
        sup_env):
    """回報成功卻沒有行程可看 → 退出，而不是對著 None 繞圈。

    這道檢查在迴圈**頂端**，所以它守的是每一輪，不只是第一輪。少了它，
    `proc.poll()` 會對 None 丟 `AttributeError`，被最外層吃掉，然後這批
    就悄悄失去監督了。
    """
    _start(sup_env, rc=1, alive=40.0)
    sup_env.spawn_reports_success_without_a_process = True
    _run(sup_env)
    assert sup_env.spawns == ["selenium"]
    assert "監督機制異常結束" not in _said(sup_env)


# --------------------------------------------------------------------------
# 十一、間隔一律用單調時鐘（靜態）
# --------------------------------------------------------------------------

def _wall_clock_reads(func: ast.AST) -> list:
    """回傳函式裡所有 `time.time()` 的行號。

    這支函式量出來的秒數驅動三個重啟決策（啟動視窗、退避重置、放棄閘），而
    `time.time()` 是**可調整**的時鐘：往前跳會把一次秒崩算成健康 run（放棄閘永遠
    不響 → 無限重生），往後跳會把一個跑了好幾小時的健康 run 算成 rapid fail
    （提早放棄一個正常的批次）。兩個方向都是靜默的。

    行為測試守的是「時鐘跳躍之下答案仍然正確」，這一支守的是「根本沒有人去讀那個
    會跳的時鐘」——後者連「讀了但目前剛好不影響結果」的新增用法也擋得住。
    """
    hits = []
    for node in ast.walk(func):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "time"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "time"):
            hits.append(node.lineno)
    return hits


def _unchecked_spawns(func: ast.AST) -> list:
    """回傳「後面沒有 `if not ok: return`」的 `_spawn_webrunner` 呼叫行號。

    這條規則行為上守不住：`_spawn_webrunner` 回 False 的兩個地方都還沒碰到
    `_webrunner_proc`，所以迴圈繞回頂端一定會被 `if proc is None: return` 接住
    （量過，兩個變異都活下來）。守得住的是**形狀**——而形狀正是會漂的那一半：
    第三個 spawn 點長出來的時候，沒有任何行為測試會提醒它也要檢查回傳值。
    """
    spawns = sorted(n.lineno for n in ast.walk(func)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_spawn_webrunner")
    guards = [n.lineno for n in ast.walk(func)
              if isinstance(n, ast.If)
              and isinstance(n.test, ast.UnaryOp)
              and isinstance(n.test.op, ast.Not)
              and isinstance(n.test.operand, ast.Name)
              and n.test.operand.id == "ok"
              and len(n.body) == 1 and isinstance(n.body[0], ast.Return)]
    bounds = spawns[1:] + [float("inf")]
    return [line for line, nxt in zip(spawns, bounds)
            if not any(line < g < nxt for g in guards)]


def test_every_spawn_site_checks_whether_the_spawn_worked():
    """兩個 spawn 點都要在**自己那一段**檢查回傳值。"""
    func = _named_function(BOT_SOURCE.read_text(encoding="utf-8"),
                           "_watch_for_fallback")
    spawns = [n.lineno for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "_spawn_webrunner"]
    assert len(spawns) == 2, (
        f"spawn 點從 2 個變成 {len(spawns)} 個——新的那個也要檢查回傳值，"
        "並且把這個數字改過來。")
    assert _unchecked_spawns(func) == []


@pytest.mark.parametrize("source, expected", [
    ("def f():\n    ok, s = _spawn_webrunner('a')\n"
     "    if not ok:\n        return\n", 0),
    ("def f():\n    ok, s = _spawn_webrunner('a')\n    print(s)\n", 1),
    ("def f():\n    if not ok:\n        return\n"
     "    ok, s = _spawn_webrunner('a')\n", 1),
    ("def f():\n    ok, s = _spawn_webrunner('a')\n"
     "    if not ok:\n        return\n"
     "    ok, s = _spawn_webrunner('b')\n    print(s)\n", 1),
    ("def f():\n    ok, s = _spawn_webrunner('a')\n"
     "    # 中間隔著幾行\n    print(s)\n    if not ok:\n        return\n", 0),
])
def test_the_spawn_check_scan_can_tell_the_shapes_apart(source, expected):
    """合成對照組。

    第三個是**順序**的近似情形（檢查寫在呼叫之前，等於沒檢查），第四個是「第一個
    檢查了、第二個沒有」——那正是這支測試真正要抓的形狀，而只看「函式裡有沒有一個
    `if not ok`」的寫法會放它過去。第五個是必須放行的：中間隔幾行不影響。
    """
    assert len(_unchecked_spawns(_named_function(source, "f"))) == expected


def _named_function(source: str, name: str):
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            return node
    raise AssertionError(f"找不到 {name}")


def test_the_supervisor_never_measures_an_interval_on_the_wall_clock():
    """監督者裡不該有任何一次 `time.time()`。"""
    func = _named_function(BOT_SOURCE.read_text(encoding="utf-8"),
                           "_watch_for_fallback")
    hits = _wall_clock_reads(func)
    assert hits == [], (
        f"`_watch_for_fallback` 在 {hits} 讀了可調整的牆上時鐘。"
        "這裡量的是間隔，一律用 `time.monotonic()`；會落地或跨行程比對的絕對"
        "時間戳才留在 `time.time()`，而這支函式沒有那種東西。")


@pytest.mark.parametrize("source, expected", [
    ("def f():\n    a = time.monotonic()\n    return time.monotonic() - a\n", 0),
    ("def f():\n    a = time.time()\n    return time.time() - a\n", 2),
    ("def f():\n    return clock.time()\n", 0),
    ("def f():\n    return time.time\n", 0),
])
def test_the_wall_clock_scan_can_tell_the_shapes_apart(source, expected):
    """合成對照組——命中清單若永遠是空的，上面那支刪掉也不會變紅。

    第三、四個是**必須放行**的近似情形：別的物件上剛好也有 `time` 方法、以及取用
    函式本身而不是呼叫它。放行步驟只有這種案例殺得掉。
    """
    assert len(_wall_clock_reads(_named_function(source, "f"))) == expected
