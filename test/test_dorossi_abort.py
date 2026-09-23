"""`/dorossi abort` 的協調邏輯——38 行 docstring，65 行敘述缺 64 行。

`mcmd_abort` 是 §8.104 那個自走迴圈的另一半：迴圈那邊檢查 `st.abort`，而**設**那個
旗標、決定「設在誰身上」的是這裡。它同時還管產圖佇列的「只中止當前這一筆」，兩套
子系統共用同一個指令。

docstring 寫了 38 行，其中一整段是 **blast-radius 取捨**——同一個動作在兩種情況下
必須做**不同**的事：

* 這一筆由「專責的單張 server」服務 → 直接 kill 它，乾淨中止當前那張；
* 這一筆由一個**批次工作**順帶 in-band 服務 → **不殺行程**，只把這一筆從佇列狀態
  丟掉。殺下去會把整批一起殺掉（還會觸發 supervisor respawn），那等於對整批下
  `/stop`，而使用者要的只是中止一張圖。

弄反了不會報錯：使用者會看到「已中止目前進行中的工作」，然後發現跑了好幾天的批次
沒了。而在 2026-09-20 之前，這 65 行裡有 64 行從來沒有被任何測試執行過。

**這個檔案不碰主機上的任何正式檔案。** `SINGLE_IMAGE_REQUEST_FILE` 指到 `tmp_path`、
`_clear_pid` 換成記錄器——正式路徑上這兩個動作分別會刪掉單張請求檔與
`webrunner.pid`，而這台機器上隨時可能有一個跑了好幾天的批次正靠著它們。夾具自己
斷言封印有生效（`test_the_fixture_really_seals_the_live_files`）。
"""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "axiomatic"))

import discord_bot as b  # noqa: E402

OWNER = 424242
STRANGER = 999


class _FakeLoop:
    """`_DorossiLoopState` 的替身：只記下有沒有被要求中止。"""

    def __init__(self):
        self.aborted = 0

    def request_abort(self):
        self.aborted += 1


@pytest.fixture
def abort_env(tmp_path, monkeypatch):
    """把 `mcmd_abort` 會碰到的每一個外部動作換成替身並記帳。"""
    env = types.SimpleNamespace(
        replies=[], loops={}, state={}, pumped=0, terminated=[], cleared=0,
        request_file=tmp_path / "single_image_request.json",
        alive=True,
    )

    async def fake_safe_reply(_message, content=None, **_kwargs):
        env.replies.append(content)
        return types.SimpleNamespace(id=1)

    async def fake_pump():
        env.pumped += 1

    async def fake_terminate(proc, pid):
        env.terminated.append((proc, pid))

    monkeypatch.setattr(b, "OWNER_USER_ID", OWNER)
    monkeypatch.setattr(b, "safe_reply", fake_safe_reply)
    monkeypatch.setattr(b, "_dorossi_loops", env.loops)
    monkeypatch.setattr(b, "_dorossi_load_state", lambda: env.state)
    monkeypatch.setattr(b, "_generate_pump", fake_pump)
    monkeypatch.setattr(b, "_terminate_all_webrunner_instances", fake_terminate)
    monkeypatch.setattr(b, "SINGLE_IMAGE_REQUEST_FILE", env.request_file)
    monkeypatch.setattr(b, "_clear_pid",
                        lambda *a, **k: setattr(env, "cleared", env.cleared + 1))
    monkeypatch.setattr(b, "_webrunner_alive", lambda: env.alive)
    monkeypatch.setattr(b, "_active_pid", lambda: 4321)
    monkeypatch.setattr(b, "_generate_inflight", None, raising=False)
    monkeypatch.setattr(b, "_generate_queue", [], raising=False)
    monkeypatch.setattr(b, "_single_image_pending", {}, raising=False)
    monkeypatch.setattr(b, "_webrunner_oneshot", False, raising=False)
    monkeypatch.setattr(b, "_webrunner_proc", None, raising=False)
    monkeypatch.setattr(b, "_webrunner_pid", None, raising=False)
    monkeypatch.setattr(b, "_webrunner_variant", None, raising=False)
    monkeypatch.setattr(b, "_webrunner_oneshot_reaper_task", None,
                        raising=False)
    return env


def _message(author_id=OWNER):
    return types.SimpleNamespace(author=types.SimpleNamespace(id=author_id))


def _abort(rest="", author_id=OWNER):
    asyncio.run(b.mcmd_abort(_message(author_id), rest))


def _said(env):
    return "\n".join(str(x) for x in env.replies)


def _running(env, *sids, active=None):
    """讓這幾個 session 各有一個進行中的迴圈，回傳 {sid: 替身}。"""
    made = {}
    for sid in sids:
        loop = _FakeLoop()
        env.loops[b._dorossi_session_key(str(OWNER), sid)] = loop
        made[sid] = loop
    env.state[str(OWNER)] = {"active": active}
    return made


# --------------------------------------------------------------------------
# 封印
# --------------------------------------------------------------------------

def test_the_fixture_really_seals_the_live_files(abort_env, tmp_path):
    """正式路徑上這兩個動作會刪掉單張請求檔與 `webrunner.pid`。

    這台機器上隨時可能有一個跑了好幾天的批次正靠著它們，所以封印失效的代價是
    「測試把正式作業弄死」，而那不會在測試報告裡出現——只會在幾小時後被發現。
    這一支是封印自己的對照組：夾具沒生效時它就紅。
    """
    assert b.SINGLE_IMAGE_REQUEST_FILE.parent == tmp_path
    assert b.SINGLE_IMAGE_REQUEST_FILE.name == "single_image_request.json"
    b._clear_pid()
    assert abort_env.cleared == 1, "`_clear_pid` 還是真的那一支"


# --------------------------------------------------------------------------
# 閘門
# --------------------------------------------------------------------------

def test_a_stranger_gets_a_generic_refusal_and_nothing_happens(abort_env):
    """非擁有者一律泛用拒絕、**不做任何事**。

    它會終止背景行程、操作同一條 pipeline，所以閘門要在做任何事之前。回覆也不得
    說出被擋掉的是什麼——非擁有者連「這裡有自走任務在跑」都不該知道。
    """
    loops = _running(abort_env, "s1", active="s1")
    _abort(author_id=STRANGER)
    assert loops["s1"].aborted == 0, "非擁有者居然中止得了任務"
    assert abort_env.pumped == 0
    assert "僅限擁有者" in _said(abort_env)
    assert "s1" not in _said(abort_env)


# --------------------------------------------------------------------------
# 自走迴圈：指誰
# --------------------------------------------------------------------------

def test_no_argument_aborts_the_active_session(abort_env):
    """未指名時優先中止 **active session** 的迴圈，其餘完全不受影響。"""
    loops = _running(abort_env, "s1", "s2", active="s2")
    _abort()
    assert loops["s2"].aborted == 1
    assert loops["s1"].aborted == 0, "連別的 session 的任務一起中止了"
    assert "s2" in _said(abort_env)


def test_no_argument_with_only_one_loop_aborts_that_one(abort_env):
    """active 沒有迴圈、而只有一個在跑 → 就是它，不必再問一次。"""
    loops = _running(abort_env, "s3", active="s9")
    _abort()
    assert loops["s3"].aborted == 1


def test_an_ambiguous_abort_asks_instead_of_guessing(abort_env):
    """有多個在跑而 active 沒有 → 列出 id 請擁有者指定，**一個都不中止**。

    猜一個下去的代價不對稱：猜錯就是砍掉一個跑了好幾小時的無人值守任務，而
    再問一句的代價只有一次往返。
    """
    loops = _running(abort_env, "s1", "s2", active="s9")
    _abort()
    assert all(loop.aborted == 0 for loop in loops.values()), "含糊時猜了一個"
    said = _said(abort_env)
    assert "s1" in said and "s2" in said


def test_a_named_session_aborts_exactly_that_one(abort_env):
    loops = _running(abort_env, "s1", "s2", active="s1")
    _abort("s2")
    assert loops["s2"].aborted == 1 and loops["s1"].aborted == 0


@pytest.mark.parametrize("word", ["all", "全部", "ALL"])
def test_all_aborts_every_running_loop(abort_env, word):
    """`全部` 是 `all` 的同義詞，而參數是先 lower 過的——大小寫不該有差。"""
    loops = _running(abort_env, "s1", "s2", active="s1")
    _abort(word)
    assert [loop.aborted for loop in loops.values()] == [1, 1]
    assert "2 個" in _said(abort_env)


def test_a_named_session_that_is_not_running_does_not_fall_through(abort_env):
    """指名形式**只**針對自走任務：指名的 id 沒在跑，就到此為止。

    往下掉到產圖那條路的話，一個打錯 session id 的 abort 會去中止一張正在算的圖
    ——使用者要的是停掉某個對話的任務，結果停掉的是完全不相干的東西。
    """
    _running(abort_env, "s1", active="s1")
    b._generate_inflight = "rid-1"
    _abort("s7")
    assert abort_env.pumped == 0, "掉進產圖路徑了"
    assert b._generate_inflight == "rid-1", "動到了正在進行的產圖工作"
    assert "沒有進行中的任務" in _said(abort_env)


def test_a_named_session_with_no_loops_at_all_still_does_not_fall_through(
        abort_env):
    """一個迴圈都沒有、但指名了 id——同一條規則，走的是另一段程式碼。"""
    b._generate_inflight = "rid-1"
    _abort("s7")
    assert abort_env.pumped == 0
    assert b._generate_inflight == "rid-1"


def test_a_running_loop_takes_priority_over_an_image_job(abort_env):
    """自走迴圈與產圖是兩套獨立子系統，兩邊都在跑時**這一次**只處理自走那邊。"""
    loops = _running(abort_env, "s1", active="s1")
    b._generate_inflight = "rid-1"
    _abort()
    assert loops["s1"].aborted == 1
    assert b._generate_inflight == "rid-1", "順手把產圖那一筆也中止了"
    assert abort_env.pumped == 0


# --------------------------------------------------------------------------
# 產圖：blast radius
# --------------------------------------------------------------------------

def test_nothing_running_at_all_says_so_and_does_nothing(abort_env):
    _abort()
    assert "沒有正在進行的工作" in _said(abort_env)
    assert abort_env.pumped == 0 and abort_env.terminated == []


def test_a_dedicated_single_image_server_is_killed(abort_env):
    """專責的單張 server：kill 它就是乾淨中止當前那張，佇列其餘的繼續。

    reaper 要先 cancel——它若在我們清 globals 的同時 re-drive，兩邊會搶同一組
    全域變數。**不**設停止旗標：佇列要繼續，下一筆的 spawn 必須放行。
    """
    reaper = types.SimpleNamespace(done=lambda: False, cancelled=0)
    reaper.cancel = lambda: setattr(reaper, "cancelled", reaper.cancelled + 1)
    b._webrunner_oneshot = True
    b._webrunner_proc = object()
    b._webrunner_pid = 4321
    b._webrunner_oneshot_reaper_task = reaper
    b._generate_inflight = "rid-1"
    b._single_image_pending["rid-1"] = "corr"
    abort_env.request_file.write_text("{}", encoding="utf-8")

    _abort()

    assert abort_env.terminated, "專責 server 沒有被終止"
    assert reaper.cancelled == 1, "reaper 沒有先 cancel，會跟我們搶 globals"
    assert b._webrunner_proc is None and b._webrunner_pid is None
    assert b._webrunner_oneshot is False
    assert abort_env.cleared == 1
    assert not abort_env.request_file.exists(), "磁碟上的請求檔還在，會被撿回來"
    assert b._generate_inflight is None
    assert "rid-1" not in b._single_image_pending
    assert abort_env.pumped == 1, "沒有把佇列的下一筆推上去"


def test_a_batch_serving_in_band_is_never_killed(abort_env):
    """**這一條是整支函式最貴的一行。**

    這一筆若是由一個批次工作順帶 in-band 服務，kill 會把整批一起殺掉（還會觸發
    supervisor respawn）——那等於對整批下 `/stop`，而使用者按的是「中止這一張」。
    所以這種情況只把這一筆從佇列狀態丟掉，讓批次的下一輪 in-band poll 找不到它
    而跳過。

    弄反了不會報錯：使用者會看到「已中止目前進行中的工作」，然後發現跑了好幾天
    的批次沒了。
    """
    b._webrunner_oneshot = False          # 批次，不是專責單張 server
    b._webrunner_proc = object()
    b._webrunner_pid = 4321
    b._generate_inflight = "rid-1"
    b._single_image_pending["rid-1"] = "corr"
    abort_env.request_file.write_text("{}", encoding="utf-8")

    _abort()

    assert abort_env.terminated == [], "把整個批次殺掉了"
    assert abort_env.cleared == 0, "清掉了批次的 pid 檔"
    assert b._webrunner_pid == 4321, "動到了批次的追蹤狀態"
    assert b._generate_inflight is None, "沒有把這一筆從佇列狀態丟掉"
    assert not abort_env.request_file.exists()
    assert abort_env.pumped == 1


def test_a_dead_oneshot_server_is_not_killed_either(abort_env):
    """旗標說是專責 server，但它已經死了——沒有東西可以殺，也不該去動 pid 檔。"""
    abort_env.alive = False
    b._webrunner_oneshot = True
    b._generate_inflight = "rid-1"
    _abort()
    assert abort_env.terminated == [] and abort_env.cleared == 0
    assert b._generate_inflight is None and abort_env.pumped == 1


@pytest.mark.parametrize("queued, inflight_after, expect", [
    (0, False, "已無其他項目"),
    (2, False, "剩餘的 2 項"),
    (2, True, "剩餘的 3 項"),
])
def test_the_reply_counts_what_is_left(abort_env, monkeypatch, queued,
                                       inflight_after, expect):
    """收尾那句話的數字＝剛被 pump 推上去的那筆（若有）＋ 仍在排隊的數量。

    它是擁有者判斷「我只停掉一張，還是把整條佇列清掉了」的唯一依據——`/stop` 才是
    清空整條佇列的指令，這裡不是。
    """
    async def fake_pump():
        abort_env.pumped += 1
        if inflight_after:
            b._generate_inflight = "rid-next"

    monkeypatch.setattr(b, "_generate_pump", fake_pump)
    b._generate_queue.extend(range(queued))
    b._generate_inflight = "rid-1"
    _abort()
    assert expect in _said(abort_env)


def test_a_failure_while_aborting_is_reported_without_raising(abort_env,
                                                              monkeypatch):
    """這一支是從對話平台呼叫的，往外拋只會變成一個沒有回覆的指令。

    使用者按了中止、什麼都沒收到，而背景狀態已經被改了一半——比明說「出問題了」
    糟得多。

    而**這條路的提問者一定是擁有者**（上面那道閘門擋掉了其他人），所以 2026-08-27
    的擁有者裁定生效：送出去的是原始例外文字，不是泛用句。第一版這支斷言的是泛用
    句、當場紅了——泛用化是給**非擁有者**的，而非擁有者根本走不到這裡。錯的是測試
    的期待，不是程式碼。
    """
    async def _boom():
        raise RuntimeError("pump 炸了")

    monkeypatch.setattr(b, "_generate_pump", _boom)
    b._generate_inflight = "rid-1"
    _abort()                       # 不得往外拋
    assert abort_env.replies, "出錯了卻一則回覆都沒送"
    assert "pump 炸了" in _said(abort_env), (
        "擁有者應該看到真正的原因（2026-08-27 裁定），而不是一句泛用的話")
    assert b._generate_inflight is None, (
        "例外發生在狀態已經改了一半之後，佇列狀態不該回到「還在進行中」")
